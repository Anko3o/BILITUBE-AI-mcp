#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bilitube 的后端：用户在 bilitube 播放页上看片，本服务记着「TA 现在看到哪一秒」，
AI 伴侣通过 MCP（mcp/ 里的 watching / stage）随时拉那一秒的画面、字幕和弹幕，等于和 TA 看同一块屏幕。

往聊天里推的只有很短的事件文字（用户发的私人弹幕；开映／暂停／看完默认不推），
画面和字幕不往聊天里塞，AI 要看自己来拉。推送地址见 BILITUBE_PUSH_URL，不配就只记在 outbox.log。

- 监听 127.0.0.1:18008（BILITUBE_PORT）。本服务不做登录鉴权：对外必须放在带门禁的反向代理后面
  （示例见 examples/Caddyfile，/bilitube/api/* → 本服务，去掉 /bilitube 前缀）。
- 本地片源目录 BILITUBE_FILM_DIR（默认 <仓库>/data/films），视频文件建议交给反向代理直接发（/bilitube/media/*）。
- 状态、私人弹幕、设置落盘在 BILITUBE_DATA_DIR（默认 <仓库>/data/server）。
- 海报 .posters/、抓帧 .frames/ 在片源目录下的隐藏目录里，一律由本服务自己返回。

B 站的视频不落盘：贴链接 → 问本机 bili_mcp（mcp/bili_mcp.py 的回环 REST）要 bvid/cid/标题 →
记进 bili.json 当一部虚拟片子（id 形如 bili:BV…[_p2]）；播放时现取 playurl，页面优先直连 CDN，
不通再由本服务按 Range 代理。弹幕和评论区也从 bili_mcp 拉，缓存在 DATA_DIR/dm|cm/。
B 站的登录凭据只在 bili_mcp 那头，本服务不读。
"""
import base64
import gzip
import json
import os
import re
import subprocess
import threading
import time
import zlib
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

HOST = "127.0.0.1"
PORT = int(os.environ.get("BILITUBE_PORT", "18008"))
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILM_DIR = os.environ.get("BILITUBE_FILM_DIR") or os.path.join(_REPO_ROOT, "data", "films")
DATA_DIR = os.environ.get("BILITUBE_DATA_DIR") or os.path.join(_REPO_ROOT, "data", "server")
# 页面挂在反向代理的哪个前缀下（返回给页面的 poster/src 等地址带它）
URL_PREFIX = "/" + os.environ.get("BILITUBE_URL_PREFIX", "/bilitube").strip("/")
POSTER_DIR = os.path.join(FILM_DIR, ".posters")
FRAME_DIR = os.path.join(FILM_DIR, ".frames")
NOW_FILE = os.path.join(DATA_DIR, "now.json")
NOTES_FILE = os.path.join(DATA_DIR, "notes.json")
META_FILE = os.path.join(DATA_DIR, "meta.json")
OUTBOX = os.path.join(DATA_DIR, "outbox.log")
EVENTS_LOG = os.path.join(DATA_DIR, "events.jsonl")   # 本地流水：开映/暂停/看完/私人弹幕（推没推都记）

# B 站直连
BILI_FILE = os.path.join(DATA_DIR, "bili.json")      # 虚拟片单
DM_DIR = os.path.join(DATA_DIR, "dm")                 # 弹幕缓存 <bvid>_<cid>.json
CM_DIR = os.path.join(DATA_DIR, "cm")                 # 评论缓存 <bvid>.json
TRASH_DIR = os.path.join(DATA_DIR, "trash")           # 删片时把这部的批注（notes.json）先备份到这里
BILI_MCP = os.environ.get("BILITUBE_BILI_MCP", "http://127.0.0.1:18013")
# 封面 / 头像代理的图按 bvid / mid 存一份，不会变：让浏览器记一周（空串＝回 no-store）
IMG_CACHE = os.environ.get("BILITUBE_IMG_CACHE", "private, max-age=604800")
GZIP_JSON = os.environ.get("BILITUBE_GZIP", "1") != "0"     # JSON 回信自己 gzip（反向代理别对 /api/* 整段压时也省流量）；0＝不压
STREAM_UA = os.environ.get("BILITUBE_STREAM_UA",
                           "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
STREAM_REFERER = "https://www.bilibili.com/"
STREAM_CHUNK = 64 * 1024
PLAYURL_SLACK = 60                                    # 地址到期前 60 秒就当过期
DANMAKU_MAX = 5000
COMMENT_MAX = 40

# 推送：把短事件 POST 给你自己的聊天后端（JSON，见 push_post）。不配地址就只记 outbox.log。
PUSH_URL = os.environ.get("BILITUBE_PUSH_URL", "").strip()
PUSH_TOKEN = os.environ.get("BILITUBE_PUSH_TOKEN", "").strip()             # 有就放进 Authorization: Bearer
PUSH_TOKEN_FILE = os.environ.get("BILITUBE_PUSH_TOKEN_FILE", "").strip()   # 或者从文件读（只读第一行）
PUSH_ENABLED = os.environ.get("BILITUBE_PUSH", "1") != "0" and bool(PUSH_URL)
try:                                                                       # 想给每条推送加固定字段（比如房间号）
    PUSH_EXTRA = json.loads(os.environ.get("BILITUBE_PUSH_EXTRA") or "{}")
    if not isinstance(PUSH_EXTRA, dict):
        PUSH_EXTRA = {}
except Exception:
    PUSH_EXTRA = {}
# 推送文字开头的标记，接收方可以靠它认出「这是 bilitube 推来的弹幕」并取出「」里的原话
DM_PUSH_TAG = os.environ.get("BILITUBE_DM_TAG", "【bilitube·弹幕】")
# 开映／暂停／看完三种小事件默认不推（判定照跑、照记 events.jsonl，outbox.log 里记成 [QUIET]）。
# 想推：BILITUBE_PUSH_EVENTS=1。用户发的弹幕不归这个开关管；BILITUBE_PUSH=0 是总闸，连弹幕一起不推。
PUSH_EVENTS = os.environ.get("BILITUBE_PUSH_EVENTS", "0") == "1"
# 用户发弹幕那一刻的画面要不要以 base64 附在推送里（默认不附：AI 拿最后一行的 film=… 自己 look）
DM_FRAME_ATTACH = os.environ.get("BILITUBE_DM_FRAME", "0") == "1"
# 两个人的称呼存在 prefs.json（设置页能改，见 names()）。这里只是没填时的默认值，也可以用环境变量改。
# 这些字是推给 AI 伴侣看的，所以用户默认叫「TA」（叫「你」会被模型当成它自己）。
DEFAULT_USER_NAME = os.environ.get("BILITUBE_USER_NAME") or os.environ.get("BILITUBE_HUMAN_NAME") or "TA"
DEFAULT_COMPANION_NAME = os.environ.get("BILITUBE_COMPANION_NAME") or "AI"

VIDEO_EXT = (".mp4", ".mkv", ".webm", ".m4v", ".mov")
SUB_EXT = (".srt", ".vtt")
NATIVE_EXT = (".mp4", ".webm", ".m4v")     # <video> 直接能放的
COMPANION_PEEK_MS = 20000                  # AI 看过画面后，页面上的角标亮 20 秒
STALE_MS = 15000                           # 超过这么久没心跳就算「没在播」
IDLE_STOP_MS = 300000                      # 5 分钟没动静 = TA 放下了
RESUME_GAP_MS = 120000                     # 离开超过 2 分钟再回来才算重新开映
NEAR_END_SEC = 15                          # 离片尾这么近就算看完

_lock = threading.RLock()
_state = {}        # now：{film, t, playing, at, duration, startedAt}
_notes = {}        # {filmId: [note, ...]}
_meta = {}         # {cacheKey: {duration, ...}}
_peek = None       # AI 最近看的那一帧
_bili = []         # 虚拟片单 [{id,bvid,p,cid,title,up,duration,cover,added_at}]
_playurl = {}      # {filmId: {url,backup_urls,expires_at,size,qn}}


# ── 小工具 ──

def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception as e:
        log("落盘失败", path, e)


def now_ms():
    return int(time.time() * 1000)


def fmt_t(sec):
    sec = max(0, int(sec or 0))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_cn(sec):
    sec = max(0, int(sec or 0))
    if sec < 60:
        return f"{sec}秒"
    if sec < 3600:
        return f"{sec // 60}分{sec % 60}秒"
    return f"{sec // 3600}小时{(sec % 3600) // 60}分"


def run(cmd, timeout=25):
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


# ── 片单 ──

def film_id(name):
    """片子的 id = 文件名去掉扩展名。解析时靠扫目录反查，不拼路径，天生没有穿越。"""
    return os.path.splitext(name)[0]


def list_films_raw():
    out = []
    try:
        names = sorted(os.listdir(FILM_DIR))
    except Exception:
        return out
    for n in names:
        if n.startswith("."):
            continue
        if not n.lower().endswith(VIDEO_EXT):
            continue
        p = os.path.join(FILM_DIR, n)
        if os.path.isfile(p):
            out.append((film_id(n), n, p))
    return out


def resolve(fid):
    """按 id 找回真实文件；找不到就是找不到，不做任何路径拼接。
    bili: 开头的 id 去虚拟片单里找，返回的 dict 带 kind="bili"、path=None。"""
    if not fid:
        return None
    if str(fid).startswith("bili:"):
        b = bili_find(fid)
        if not b:
            return None
        f = dict(b)
        f.update({"id": b["id"], "name": b.get("title") or b["id"], "path": None, "kind": "bili"})
        return f
    for i, n, p in list_films_raw():
        if i == fid:
            return {"id": i, "name": n, "path": p}
    return None


def is_bili(film):
    return bool(film) and film.get("kind") == "bili"


def cache_stem(film):
    """缓存文件名用的片子代号：本地片子就是 id；bili: 换成 bili_<bvid>_<cid>（冒号别进文件名）。"""
    if is_bili(film):
        return f"bili_{film.get('bvid')}_{film.get('cid')}"
    return film["id"]


def find_sub(film):
    """同名字幕：<片名>.srt / .vtt，也认 <片名>.zh.srt 这种带语言后缀的。"""
    stem = film["id"]
    try:
        names = sorted(os.listdir(FILM_DIR))
    except Exception:
        return None
    exact = [n for n in names if os.path.splitext(n)[0] == stem
             and n.lower().endswith(SUB_EXT)]
    loose = [n for n in names if n.startswith(stem + ".")
             and n.lower().endswith(SUB_EXT)]
    for n in exact + loose:
        return os.path.join(FILM_DIR, n)
    return None


def probe_duration(path):
    key = None
    try:
        st = os.stat(path)
        key = f"{os.path.basename(path)}|{st.st_size}|{int(st.st_mtime)}"
    except Exception:
        pass
    with _lock:
        if key and key in _meta:
            return _meta[key].get("duration")
    dur = None
    try:
        r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", path], timeout=30)
        if r.returncode == 0:
            dur = round(float(r.stdout.decode().strip()), 2)
    except Exception as e:
        log("ffprobe 失败", path, e)
    if key:
        with _lock:
            _meta[key] = {"duration": dur}
            save_json(META_FILE, _meta)
    return dur


def poster_path(film, make=True):
    if is_bili(film):
        return bili_cover_path(film, make=make)
    os.makedirs(POSTER_DIR, exist_ok=True)
    p = os.path.join(POSTER_DIR, film["id"] + ".jpg")
    if os.path.exists(p) and os.path.getsize(p) > 0:
        return p
    if not make:
        return None
    dur = probe_duration(film["path"]) or 0
    at = max(1, dur * 0.1) if dur else 1
    try:
        r = run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y",
                 "-ss", f"{at:.2f}", "-i", film["path"], "-frames:v", "1",
                 "-vf", "scale=480:-2", "-q:v", "4", p], timeout=60)
        if r.returncode == 0 and os.path.exists(p):
            return p
        log("抓海报失败", film["id"], r.stderr.decode()[:200])
    except Exception as e:
        log("抓海报炸了", film["id"], e)
    return None


def frame_path(film, t, width=640):
    os.makedirs(FRAME_DIR, exist_ok=True)
    t = max(0, float(t or 0))
    p = os.path.join(FRAME_DIR, f"{cache_stem(film)}-{int(round(t))}-{int(width)}.jpg")
    if os.path.exists(p) and os.path.getsize(p) > 0:
        return p
    src, pre = film["path"], []
    if is_bili(film):
        # 直连的片子：ffmpeg 直接对着 CDN 的 mp4 按 Range 拉那一秒（moov 在前，输入端 -ss 很快）
        info = stream_url(film)
        if not info:
            log("抓帧：拿不到播放地址", film["id"])
            return None
        src = info["url"]
        pre = ["-headers", f"Referer: {STREAM_REFERER}\r\nUser-Agent: {STREAM_UA}\r\n"]
    try:
        r = run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y"] + pre +
                ["-ss", f"{t:.2f}", "-i", src, "-frames:v", "1",
                 "-vf", f"scale={int(width)}:-2", "-q:v", "3", p], timeout=90)
        if r.returncode == 0 and os.path.exists(p) and os.path.getsize(p) > 0:
            return p
        log("抓帧失败", film["id"], t, r.stderr.decode()[:200])
    except Exception as e:
        log("抓帧炸了", film["id"], t, e)
    return None


# 三帧图：t−1 / t / t+1 竖着摞成一张，每帧左上角一个时间码，中间那帧（TA 停的那秒）粉框＋粉底时间码。
# jpg q7，一张二十来 KB。片头片尾够不着的那帧就少拼一帧。
# 一次 ffmpeg 三个输入（B 站视频就是三条 Range 连接），不起三个进程。
TRI_W = 440   # 每帧宽度（竖着摞，在手机上每帧都是满宽）
TRI_Q = 7
TRI_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def film_duration(film):
    if is_bili(film):
        return float(film.get("duration") or 0)
    return float(probe_duration(film["path"]) or 0)


def frame_tri_path(film, t, width=TRI_W):
    os.makedirs(FRAME_DIR, exist_ok=True)
    t = max(0.0, float(t or 0))
    width = max(160, min(640, int(width)))
    p = os.path.join(FRAME_DIR, f"{cache_stem(film)}-tri{int(round(t * 10))}-{width}.jpg")
    if os.path.exists(p) and os.path.getsize(p) > 0:
        return p
    dur = film_duration(film)
    ts = [x for x in (t - 1, t, t + 1) if x >= 0 and (not dur or x <= dur - 0.15)] or [t]
    src, pre = film["path"], []
    if is_bili(film):
        info = stream_url(film)
        if not info:
            log("抓三帧：拿不到播放地址", film["id"])
            return None
        src = info["url"]
        pre = ["-headers", f"Referer: {STREAM_REFERER}\r\nUser-Agent: {STREAM_UA}\r\n"]
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y"]
    for x in ts:
        cmd += pre + ["-ss", f"{x:.2f}", "-i", src]
    fs = max(11, width // 24)
    parts = []
    for i, x in enumerate(ts):
        mid = abs(x - t) < 1e-6
        lab = fmt_t(x).replace(":", "\\:")
        f = (f"[{i}:v]trim=end_frame=1,setpts=PTS-STARTPTS,scale={width}:-2,setsar=1,"
             f"drawtext=fontfile={TRI_FONT}:text='{lab}':x=5:y=5:fontsize={fs}:fontcolor=white:"
             f"box=1:boxcolor={'0xd0507a@0.88' if mid else 'black@0.55'}:boxborderw=3")
        if mid:
            f += ",drawbox=x=0:y=0:w=iw:h=ih:color=0xff9ec4:t=3"
        if i < len(ts) - 1:
            f += ",pad=iw:ih+4:0:0:color=0x1c1420"          # 帧和帧之间留一道 4px 的缝（竖着摞）
        parts.append(f + f"[v{i}]")
    n = len(ts)
    fc = ";".join(parts) + ";" + "".join(f"[v{i}]" for i in range(n)) + \
        (f"vstack=inputs={n}" if n > 1 else "null") + "[o]"
    cmd += ["-filter_complex", fc, "-map", "[o]", "-frames:v", "1", "-q:v", str(TRI_Q), p]
    try:
        r = run(cmd, timeout=90)
        if r.returncode == 0 and os.path.exists(p) and os.path.getsize(p) > 0:
            return p
        log("抓三帧失败", film["id"], t, r.stderr.decode()[:300])
    except Exception as e:
        log("抓三帧炸了", film["id"], t, e)
    try:
        os.remove(p)
    except Exception:
        pass
    return None


# ── 字幕 ──

def parse_ts(s):
    s = s.strip().replace(",", ".")
    m = re.match(r"^(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)$", s)
    if not m:
        return None
    h = int(m.group(1) or 0)
    return h * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def parse_cues(path):
    """解析 srt / vtt，返回 [{start,end,text}]。"""
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            raw = f.read()
    except Exception:
        return []
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    cues = []
    for block in re.split(r"\n\s*\n", raw):
        lines = [l for l in block.split("\n") if l.strip()]
        if not lines:
            continue
        if lines[0].upper().startswith("WEBVTT"):
            lines = lines[1:]
            if not lines:
                continue
        idx = 0
        if re.match(r"^\d+$", lines[0].strip()) and len(lines) > 1 and "-->" in lines[1]:
            idx = 1
        if idx >= len(lines) or "-->" not in lines[idx]:
            continue
        a, _, b = lines[idx].partition("-->")
        start, end = parse_ts(a), parse_ts(b.split()[0] if b.split() else b)
        if start is None or end is None:
            continue
        text = "\n".join(lines[idx + 1:]).strip()
        if text:
            cues.append({"start": start, "end": end, "text": text})
    cues.sort(key=lambda c: c["start"])
    return cues


def vtt_ts(sec):
    sec = max(0.0, float(sec))
    h, rem = int(sec // 3600), sec - int(sec // 3600) * 3600
    m, s = int(rem // 60), rem - int(rem // 60) * 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def to_vtt(cues):
    out = ["WEBVTT", ""]
    for i, c in enumerate(cues, 1):
        out.append(str(i))
        out.append(f"{vtt_ts(c['start'])} --> {vtt_ts(c['end'])}")
        out.append(c["text"])
        out.append("")
    return "\n".join(out)


def sub_window(film, t, window=20.0):
    sp = find_sub(film)
    if not sp:
        return None, []
    cues = parse_cues(sp)
    lo, hi = float(t) - float(window), float(t) + float(window)
    hits = [c for c in cues if c["end"] >= lo and c["start"] <= hi]
    return os.path.basename(sp), hits


def sub_at(film, t):
    sp = find_sub(film)
    if not sp:
        return None
    for c in parse_cues(sp):
        if c["start"] <= t <= c["end"] + 0.4:
            return c
    return None


# ── 往聊天里推的短事件 ──

_push_token = None


def push_token():
    global _push_token
    if _push_token is not None:
        return _push_token
    _push_token = PUSH_TOKEN
    if not _push_token and PUSH_TOKEN_FILE:
        try:
            with open(PUSH_TOKEN_FILE, "r", encoding="utf-8") as f:
                _push_token = (f.readline() or "").strip()
        except Exception as e:
            log("读不到推送钥匙文件", type(e).__name__)
            _push_token = ""
    return _push_token


def push_post(payload, label, timeout=8):
    """POST 一份 JSON 给 PUSH_URL：{"source": "bilitube", "text": …, "createdAt": …, [bilitube: {...}], …PUSH_EXTRA}。
    结果只记 outbox.log（不记钥匙）。"""
    body = dict(PUSH_EXTRA)
    body.update(payload)
    hdr = {"Content-Type": "application/json"}
    tok = push_token()
    if tok:
        hdr["Authorization"] = "Bearer " + tok
    try:
        req = urllib.request.Request(PUSH_URL, data=json.dumps(body, ensure_ascii=False).encode(),
                                     method="POST", headers=hdr)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            outbox("SENT" if 200 <= r.status < 300 else f"HTTP-{r.status}", label)
    except urllib.error.HTTPError as e:
        outbox(f"HTTP-{e.code}", label)
    except Exception as e:
        outbox("ERROR " + str(e)[:80], label)


def outbox(status, text):
    try:
        with open(OUTBOX, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} [{status}] "
                    f"{text.replace(chr(10), ' / ')[:400]}\n")
    except Exception:
        pass


def push(text):
    """开映／暂停／看完三种事件走这里（弹幕推送另走 push_user_danmaku）。播放中的心跳一律不推。"""
    text = (text or "").strip()
    if not text:
        return
    if not PUSH_ENABLED:
        outbox("DRY-RUN", text)
        return
    payload = {"source": "bilitube", "text": text, "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S")}
    threading.Thread(target=push_post, args=(payload, text), daemon=True).start()


def journal(kind, film, **extra):
    """本地流水 events.jsonl：一行一件事。小事件默认不进聊天，但都记在这里。"""
    rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), "kind": kind, "film": film}
    rec.update({k: v for k, v in extra.items() if v is not None})
    try:
        with open(EVENTS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        log("events.jsonl 写不进去", e)


def journal_read(film=None, kinds=None):
    out = []
    try:
        with open(EVENTS_LOG, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if film and r.get("film") != film:
                    continue
                if kinds and r.get("kind") not in kinds:
                    continue
                out.append(r)
    except FileNotFoundError:
        pass
    except Exception as e:
        log("events.jsonl 读不出来", e)
    return out


def push_event(kind, film, text):
    """开映／暂停／看完。先记流水，再看开关：BILITUBE_PUSH_EVENTS=1 才真进聊天。"""
    journal(kind, film)
    if not PUSH_EVENTS:
        outbox("QUIET", text)
        return
    push(text)


def title_of(fid):
    if str(fid or "").startswith("live:"):
        return live_title(fid)                # 「直播 · 主播 · 标题」
    if str(fid or "").startswith("bili:"):
        b = bili_find(fid)
        if b and b.get("title"):
            return b["title"]
    return fid


def push_open(film, t, dur, resumed=False):
    pos = f"从 {fmt_t(t)} 接着看" if resumed and t > 30 else f"{fmt_t(t)} 起"
    tail = f"（全长 {fmt_cn(dur)}）" if dur else ""
    push_event("open", film, f"【bilitube·开映】{user_name()}放起了《{title_of(film)}》，{pos}{tail}。\n"
         f"film={film}")


def push_pause(film, t, dur, auto=False):
    prog = fmt_t(t) + (f"（全长 {fmt_t(dur)}）" if dur else "")
    push_event("pause", film, f"【bilitube·暂停】{user_name()}{'一会儿没动静了，应该是走开了' if auto else '按下了暂停'}"
         f"，《{title_of(film)}》停在 {prog}。")


def push_finish(film, dur, watched_ms):
    push_event("finish", film, f"【bilitube·看完】{user_name()}把《{title_of(film)}》看完了"
         f"（全长 {fmt_cn(dur)}，这次看了 {fmt_cn(watched_ms / 1000)}）。")


# ── 心跳与「TA 在哪」 ──

def state_snapshot():
    with _lock:
        s = dict(_state)
    if not s.get("film"):
        return {"film": None}
    age = now_ms() - (s.get("at") or 0)
    playing = bool(s.get("playing")) and age < STALE_MS
    return {
        "film": s.get("film"),
        "title": title_of(s.get("film")),
        "t": round(float(s.get("t") or 0), 2),
        "duration": s.get("duration"),
        "playing": playing,
        "reportedPlaying": bool(s.get("playing")),
        "idleMs": age,
        "at": s.get("at"),
        "watchedMs": s.get("watchedMs") or 0,
    }


def beat(film_id_in, t, playing):
    """播放页每 5 秒来报一次心跳。这里只做三件事：记位置、判状态、必要时推一条事件。"""
    film = resolve(film_id_in)
    if not film:
        return {"ok": False, "error": "没有这部片子"}
    now = now_ms()
    events = []
    with _lock:
        prev = dict(_state)
        dur = prev.get("duration") if prev.get("film") == film["id"] else None
        if dur is None:
            dur = film.get("duration") if is_bili(film) else probe_duration(film["path"])
        gap = now - (prev.get("at") or 0)
        same = prev.get("film") == film["id"]
        was_playing = bool(prev.get("playing")) and same and gap < STALE_MS
        watched = prev.get("watchedMs") or 0
        if same and was_playing:
            watched += min(gap, STALE_MS)
        elif not same:
            watched = 0
        finished = bool(prev.get("finished")) and same
        # TA 把进度条往回拖了（重看 / 从头放）→ 「看完」的标记作废
        if same and float(t or 0) + 5 < float(prev.get("t") or 0):
            finished = False

        _state.clear()
        _state.update({
            "film": film["id"], "t": float(t or 0), "playing": bool(playing),
            "at": now, "duration": dur, "watchedMs": watched,
            "finished": finished,
            "startedAt": prev.get("startedAt") if same and was_playing else now,
        })

        tail = max(5.0, min(float(NEAR_END_SEC), float(dur or 0) * 0.02))
        near_end = bool(dur) and float(t or 0) >= float(dur) - tail
        # 「上次真的在播」是什么时候：播放页一打开就先报一次 playing=false 的心跳，
        # 只看 gap 会被这一下冲掉，所以单独记一个 lastPlayingAt 作为判断开映的依据。
        last_playing = prev.get("lastPlayingAt") or 0 if same else 0
        if playing:
            _state["lastPlayingAt"] = now
        else:
            _state["lastPlayingAt"] = last_playing
        if playing and not was_playing and (not same or now - last_playing > RESUME_GAP_MS):
            events.append(("open", float(t or 0), dur, same and gap > RESUME_GAP_MS))
            _state["finished"] = False
        if playing and near_end and not finished:
            _state["finished"] = True
            events.append(("finish", float(t or 0), dur, watched))
        if (was_playing and not playing and not _state["finished"]
                and float(t or 0) >= 3):
            events.append(("pause", float(t or 0), dur, False))
        save_json(NOW_FILE, dict(_state))

    for e in events:
        if e[0] == "open":
            push_open(film["id"], e[1], e[2], resumed=e[3])
        elif e[0] == "pause":
            push_pause(film["id"], e[1], e[2], auto=e[3])
        elif e[0] == "finish":
            push_finish(film["id"], e[2], e[3])
    un = upnext_get()
    if un and un.get("film") == film["id"] and playing:
        upnext_clear()                       # 排在下一个的那部已经放起来了
    return {"ok": True, **peek_info(), "finished": bool(_state.get("finished")), **cue_info()}


# ── 插播 ──
# AI 点播时可以要「立刻播放」或「下一个播放」。不另开长连接：开着的播放页本来就每 5 秒请求 /api/beat、
# 每 2 秒请求 /api/dm/say，这两个请求的响应里顺带
# cue（立刻切到这部开始放，30 秒内有效）和 upNext（排在下一个，当前这部放完自动接上，片单上带个标记）。
# TA 没开着播放页（watching() 不成立）就都不设，只把这部加进片单。
UPNEXT_FILE = os.path.join(DATA_DIR, "upnext.json")
CUE_TTL_MS = 30000
_cue = None
_cue_seq = 0


def cue_now(fid):
    global _cue, _cue_seq
    with _lock:
        _cue_seq += 1
        _cue = {"seq": _cue_seq, "film": fid, "title": title_of(fid), "at": now_ms()}
        return dict(_cue)


def upnext_get():
    d = load_json(UPNEXT_FILE, None)
    return d if isinstance(d, dict) and d.get("film") else None


def upnext_set(after, fid):
    rec = {"film": fid, "title": title_of(fid), "after": after, "afterTitle": title_of(after) if after else "",
           "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())}
    with _lock:
        save_json(UPNEXT_FILE, rec)
    return rec


def upnext_clear():
    with _lock:
        try:
            os.remove(UPNEXT_FILE)
        except FileNotFoundError:
            pass


def cue_info():
    with _lock:
        c = dict(_cue) if _cue and now_ms() - _cue["at"] < CUE_TTL_MS else None
    out = {"upNext": upnext_get()}
    if c:
        out["cue"] = c
    return out


def watchdog():
    """TA 合上电脑走开时页面不会报「暂停」，超时后由这里补一条暂停事件。"""
    while True:
        time.sleep(30)
        try:
            with _lock:
                s = dict(_state)
                if not s.get("film") or not s.get("playing"):
                    continue
                age = now_ms() - (s.get("at") or 0)
                if age < IDLE_STOP_MS:
                    continue
                _state["playing"] = False
                save_json(NOW_FILE, dict(_state))
            if not s.get("finished"):
                push_pause(s["film"], s.get("t") or 0, s.get("duration"), auto=True)
        except Exception as e:
            log("watchdog", e)


# ── AI 看画面的记号（播放页角标）──

def mark_peek(fid, t):
    global _peek
    with _lock:
        _peek = {"film": fid, "t": float(t or 0), "at": now_ms()}


def peek_info():
    with _lock:
        p = dict(_peek) if _peek else None
    if not p:
        return {"companionHere": False}
    age = now_ms() - p["at"]
    if age > COMPANION_PEEK_MS:
        return {"companionHere": False}
    return {"companionHere": True, "companionAgeMs": age, "companionT": p["t"], "companionFilm": p["film"]}


# ── 私人弹幕 ──
# AI（bili_danmaku_say）和 TA（播放页上发的弹幕）发的私人弹幕只在 bilitube 的画面上飘过，
# 一条都不发去 B 站。这里只是个 60 条的小池子：页面每 2 秒来问一次有没有新的。
# TA 没在看（心跳不新鲜）就直接丢掉，没人看的画面上不用飘。
DM_SAY_MAX = 60
DM_SAY_TTL_MS = 120000          # 超过两分钟的不再补发给页面
_dmsay = []
_dmsay_seq = 0


def watching():
    """TA 此刻是不是真开着一部片（STALE_MS 内报过心跳）。"""
    with _lock:
        s = dict(_state)
    return bool(s.get("film")) and (now_ms() - (s.get("at") or 0)) < STALE_MS


def dm_say(text, author="companion", color=None, mode="scroll"):
    """往池子里放一条。返回 (记录, None) 或 (None, 发不出去的原因)。"""
    global _dmsay_seq
    text = str(text or "").strip().replace("\n", " ")[:80]
    if not text:
        return None, "弹幕是空的"
    lw = live_watch_room()                    # TA 此刻开着直播页（直播页每两秒请求一次 /dm/say?live=）
    if not lw and not watching():
        h = live_hold_room()                  # 直播页十几秒没来问（网络在抖），片子也没在放：仍当 TA 在这间直播
        lw = h[0] if h else None
    if not watching() and not lw:
        return None, "TA 没在看"
    author = "user" if str(author).lower() in ("user", "human") else "companion"
    rec = {"at": now_ms(), "text": text, "author": author,
           "color": str(color or "")[:16] or None,
           "mode": "top" if str(mode) == "top" else "scroll"}
    with _lock:
        _dmsay_seq += 1
        rec["id"] = _dmsay_seq
        _dmsay.append(rec)
        del _dmsay[:-DM_SAY_MAX]
        snap = dict(_state)
    rec["film"] = f"live:{lw}" if lw else snap.get("film")
    rec["t"] = None if lw else snap.get("t")
    return rec, None


def dm_say_since(since):
    cut = now_ms() - DM_SAY_TTL_MS
    with _lock:
        rows = [dict(r) for r in _dmsay if r["id"] > since and r["at"] >= cut]
        last = _dmsay[-1]["id"] if _dmsay else 0
    return {"last": last, "count": len(rows), "list": rows}


SAY_FRAME_MAX = 1024 * 1024          # 页面传来的画面超过这么大就不收（长边 640、质量 0.6，正常几十 KB）


_last_user_dm = {}      # {filmId: {t, text, at}}：TA 在每部片里最近发的那条弹幕（watching where 会带出来）


def last_user_dm(fid):
    with _lock:
        r = _last_user_dm.get(fid)
    if r:
        return dict(r)
    rows = [r for r in journal_read(fid, ("danmaku",)) if r.get("author") != "companion"]   # 流水里也有 AI 的，滤掉
    if not rows:
        return None
    r = rows[-1]
    return {"t": r.get("t"), "text": r.get("text"), "at": r.get("at")}


def user_danmaku_text(fid, t, text, paused=True, cine=None):
    """用户发弹幕时推给聊天的那段纯文字。录播里发弹幕＝暂停＋定位，附那一秒前后的字幕和弹幕。
    第一行以 DM_PUSH_TAG（默认【bilitube·弹幕】）开头，接收方靠它认出这是弹幕、取「」里的原话；
    最后一行是 film=…（录播还有 t=…），AI 拿它去 watching look。"""
    if str(fid or "").startswith("live:"):
        # 直播没有时间轴，不写时间码；发弹幕那一刻存过三帧的话，最后一行带上这条弹幕的 id
        cine = cine if isinstance(cine, dict) else {}
        lid, shot = cine.get("lid") or "", cine.get("shot") or ""
        loc = f"{fid}:{lid}" if lid and shot == "ok" else fid
        # 只说谁、在哪间直播、发了什么；开头的标记留给接收方认。
        room_title = live_title(fid).split(" · ")[-1]
        lines = [f"{DM_PUSH_TAG}{user_name()}在【直播】{room_title}里发了一条弹幕：",
                 f"「{text}」",
                 *([cine["pubsum"]] if cine.get("pubsum") else []),    # TA 发弹幕前 20 秒的公开弹幕，同类合并计数
                 *([] if shot == "ok" else [f"（这一刻的画面没抓到：{shot or '没抓'}）"]),
                 f"film={loc}"]      # 只带这一条独有的编号；怎么看、怎么回写在工具说明里
        return "\n".join(lines)
    film = resolve(fid) if fid else None
    title = title_of(fid) if fid else "（没认出是哪部）"
    ts = fmt_t(t)
    lines = [f"{DM_PUSH_TAG}{user_name()}在《{title}》{ts} 发了一条弹幕"
             + ("，播放器停在这一秒等你" if paused else "") + "：",
             f"「{text}」"]
    if film:
        # 前一秒 / 这一秒 / 后一秒各一行，跟三帧图对齐
        ar, has_sub = around_lines(film, t, cine, skip=("user", t, text))
        lines.extend(ar if ar else ["（这三秒没字幕，也没人发弹幕）"])
        if not has_sub:
            lines.append("（这部没字幕）")
    lines.append(f"film={fid} t={t:.1f}")
    return "\n".join(lines)


def push_user_danmaku(text, frame_b64=None, cine=None, live_shot=None):
    """把用户发的弹幕推给聊天：拼成一段以 DM_PUSH_TAG 开头的纯文字（见 user_danmaku_text），
    payload 里另带 bilitube={title,t,paused,bvid?,cid?}，接收方想画卡片可以用。
    画面默认不附（BILITUBE_DM_FRAME=1 才附 base64），AI 拿最后一行的 film=… 自己 look。"""
    with _lock:
        snap = dict(_state)
    cine = cine if isinstance(cine, dict) else {}
    fid = snap.get("film")
    if cine.get("film") and resolve(str(cine["film"])):
        fid = str(cine["film"])
    elif re.fullmatch(r"live:\d{1,12}", str(cine.get("film") or "")):
        fid = str(cine["film"])               # 直播：别记到上一部片子头上
    try:
        t = float(cine.get("t")) if cine.get("t") not in (None, "") else float(snap.get("t") or 0)
    except Exception:
        t = float(snap.get("t") or 0)
    t = max(0.0, t)
    paused = bool(cine.get("paused")) if "paused" in cine else True
    is_live = str(fid or "").startswith("live:")
    page_shot = None
    if is_live:
        # 直播里发弹幕：不暂停、没有时间码，t 不记。lid 留给 watching look 取 TA 发弹幕那一刻存的三帧
        paused, lid = False, f"a{int(time.time() * 1000):x}"
        cine["lid"] = lid
        page_shot = live_dm_shot_page(live_shot)              # 页面截的 TA 眼前那一刻；没带或认不出就由服务端现抓
        try:
            focus_ago = round(max(0.0, min(3600.0, float(cine.get("focusAgo")))), 1) if page_shot else None
        except Exception:
            focus_ago = None
        cine["pubsum"] = ldm_summary_line(int(fid[5:]))      # 摘要取按下那一刻的（抓帧要几秒，不等它）
        ldm_sum_keep(int(fid[5:]), lid, cine["pubsum"])
        with _lock:
            c = _live_room.get(int(fid[5:]))
        li = c[1] if c else {}
        journal("danmaku", fid, text=text, author="user", id=lid, via="live",
                roomTitle=li.get("title") or None, uname=li.get("uname") or None,
                shot="page" if page_shot else None, focusAgo=focus_ago)
    else:
        journal("danmaku", fid, t=round(t, 1), text=text, author="user")
    with _lock:
        _last_user_dm[fid] = {"t": None if is_live else round(t, 1), "text": text,
                             "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())}
    bilitube = {"title": title_of(fid) if fid else "", "t": None if is_live else round(t, 1), "paused": paused}
    for k in ("bvid", "cid"):
        if cine.get(k) not in (None, ""):
            bilitube[k] = cine[k]

    def send():
        if is_live:                       # 页面带了 TA 眼前那一刻就存那张；没带才现抓最近三秒（约 2 秒；抓不到也照推，文字里注明）
            ok, why = (live_dm_shot_store(int(fid[5:]), cine["lid"], page_shot) if page_shot else (False, None))
            if not ok:
                ok, why = live_dm_shot_save(int(fid[5:]), cine["lid"])
            cine["shot"] = "ok" if ok else (why or "没抓到")
        body_text = user_danmaku_text(fid, t, text, paused, cine)
        if not PUSH_ENABLED:
            outbox("DRY-RUN", body_text)
            return
        payload = {"source": "bilitube", "text": body_text, "bilitube": bilitube,
                   "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S")}
        if frame_b64 and DM_FRAME_ATTACH:
            try:
                raw = base64.b64decode(frame_b64, validate=True)
            except Exception:
                raw = b""
            if raw and len(raw) <= SAY_FRAME_MAX and raw[:3] == b"\xff\xd8\xff":
                payload["frame"] = {"mime": "image/jpeg", "base64": base64.b64encode(raw).decode()}
        push_post(payload, body_text, timeout=15)

    threading.Thread(target=send, daemon=True).start()


# ── 批注（notes.json，GET/POST /api/notes）──

def notes_of(fid):
    with _lock:
        return sorted(_notes.get(fid, []), key=lambda n: n.get("t", 0))


def add_note(fid, t, author, text):
    author = author if author in ("user", "companion") else "user"
    note = {"id": f"{int(time.time() * 1000):x}", "t": round(float(t or 0), 2),
            "author": author, "text": str(text or "").strip()[:1000],
            "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if not note["text"]:
        return None
    with _lock:
        _notes.setdefault(fid, []).append(note)
        save_json(NOTES_FILE, _notes)
    return note


# ── 两个人的私人弹幕记录：一起看时互相发的弹幕都留着 ──
# 记录就是 events.jsonl 里 kind=danmaku 的那些行。TA 的每条都记（没有 author 字段的行一律算 TA 的）；
# AI 的记 author="companion"，带一个 id，可以删。
# AI 的弹幕有三个入口，都走 companion_dm：POST /api/dm/companion（stage danmaku）、
# POST /api/notes 且 author=companion（按弹幕记，不进 notes.json）、POST /api/dm/say（bili_danmaku_say，当场飘过）。
# AI 的弹幕不推聊天、不让播放页暂停，那一套只给 TA 的弹幕。
# TA 正在看这部片的话，顺手放进 60 条的小池子（log:true），页面收到就插进时间轴，离 TA 停的那秒近就当场飘过。
COMPANION_DM_MAX = 140


def dm_log(fid):
    """这部片两个人的私人弹幕，按片内秒数排：[{t, author, text, at, id?}]。"""
    out = []
    for r in journal_read(fid, ("danmaku",)):
        text = str(r.get("text") or "").strip()
        if not text:
            continue
        try:
            t = round(float(r.get("t") or 0), 1)
        except Exception:
            t = 0.0
        rec = {"t": t, "author": "companion" if r.get("author") == "companion" else "user",
               "text": text, "at": r.get("at")}
        if r.get("id"):
            rec["id"] = r["id"]
        out.append(rec)
    out.sort(key=lambda x: (x["t"], x.get("at") or ""))
    return out


def dm_log_films():
    """有私人弹幕的片子一览（新的在前）：[{film, title, user, companion, first, last}]。"""
    by = {}
    for r in journal_read(None, ("danmaku",)):
        fid = r.get("film")
        if not fid or not str(r.get("text") or "").strip():
            continue
        b = by.setdefault(fid, {"film": fid, "user": 0, "companion": 0, "first": r.get("at"), "last": r.get("at")})
        b["companion" if r.get("author") == "companion" else "user"] += 1
        b["last"] = r.get("at") or b["last"]
    out = sorted(by.values(), key=lambda b: b.get("last") or "", reverse=True)
    for b in out:
        b["title"] = title_of(b["film"])
    return out


def companion_dm(fid, t, text, live=False, via="dm"):
    """AI 在 fid 的第 t 秒发一条弹幕（只记录，进不进池子由调用方定）。返回 (记录, None) 或 (None, 原因)。"""
    text = str(text or "").strip().replace("\n", " ")
    if not text:
        return None, "弹幕是空的"
    cut = len(text) > COMPANION_DM_MAX
    text = text[:COMPANION_DM_MAX]
    t = round(max(0.0, float(t or 0)), 1)
    lid = f"f{int(time.time() * 1000):x}"
    journal("danmaku", fid, t=t, text=text, author="companion", id=lid, via=via)
    rec = {"t": t, "author": "companion", "text": text, "id": lid, "film": fid,
           "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), "cut": cut}
    return rec, None


def dm_pool_put(p):
    """往弹幕小池子里放一条现成的（页面每两秒来取一次）。"""
    global _dmsay_seq
    p = dict(p, at=now_ms())
    with _lock:
        _dmsay_seq += 1
        p["id"] = _dmsay_seq
        _dmsay.append(p)
        del _dmsay[:-DM_SAY_MAX]
    return p


def companion_dm_pool(rec, live=False):
    """TA 正开着这部片时放进小池子，让页面插进时间轴（live=True 的当场飘过，其余看离 TA 停的那秒多近）。"""
    global _dmsay_seq
    with _lock:
        snap = dict(_state)
    if not watching() or snap.get("film") != rec.get("film"):
        return False
    p = {"at": now_ms(), "text": rec["text"], "author": "companion", "color": None, "mode": "scroll",
         "film": rec["film"], "t": rec["t"], "log": True, "lid": rec["id"], "live": bool(live)}
    with _lock:
        _dmsay_seq += 1
        p["id"] = _dmsay_seq
        _dmsay.append(p)
        del _dmsay[:-DM_SAY_MAX]
    return True


def companion_dm_delete(fid, lid):
    """删 AI 的一条（按 id）。TA 的不归这里删。返回删掉几行。"""
    if not lid or not str(lid).startswith("f"):
        return 0
    with _lock:
        try:
            with open(EVENTS_LOG, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except FileNotFoundError:
            return 0
        keep, gone = [], 0
        for line in lines:
            try:
                r = json.loads(line)
            except Exception:
                keep.append(line)
                continue
            if (r.get("kind") == "danmaku" and r.get("author") == "companion" and r.get("id") == lid
                    and (not fid or r.get("film") == fid)):
                gone += 1
                continue
            keep.append(line)
        if gone:
            tmp = EVENTS_LOG + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(keep)
            os.replace(tmp, EVENTS_LOG)
    return gone


# ── 前一秒 / 这一秒 / 后一秒：字幕和弹幕按秒对齐；watching look（/api/around）和弹幕推送共用 ──
AR_PUB_MAX = 4       # 每秒公开弹幕最多几条
AR_PUB_LEN = 16
AR_SUB_LEN = 40
AR_OURS_LEN = 40


def _ar_subs(film, t):
    """t−1.5 … t+1.5 的字幕 [{start,end,text}]：本地片子用同名 .srt/.vtt，B 站视频用 CC 字幕（含 AI 字幕）。None＝没字幕。"""
    if not is_bili(film):
        name, hits = sub_window(film, t, 2.0)
        return [{"start": c["start"], "end": c["end"], "text": c["text"]} for c in hits] if name else None
    if not film.get("bvid"):
        return None
    try:
        d = bili_subtitle_for(film["bvid"], film.get("p") or 1, f"{t:.2f}", 2.0, timeout=20)
    except Exception as e:
        log("三秒：CC 字幕没拿到", e)
        return None
    if not d.get("has"):
        return None
    return [{"start": float(c.get("t") or 0), "end": float(c.get("end") or c.get("t") or 0),
             "text": str(c.get("text") or "")} for c in (d.get("list") or [])]


def _ar_pub(film, t, cine=None, fetch=False):
    """t 前后的公开弹幕 [{t,text,n}]：优先用页面报上来的（已经过 TA 在页面上设的屏蔽），没有就用服务端缓存。"""
    pub = cine.get("pub") if isinstance(cine, dict) else None
    if isinstance(pub, list):
        out = []
        for x in pub[:40]:
            if not isinstance(x, dict):
                continue
            try:
                out.append({"t": float(x.get("t")), "text": str(x.get("text") or "").strip().replace("\n", " "),
                            "n": max(1, int(x.get("n") or 1))})
            except Exception:
                continue
        return out
    raw = []
    try:
        if is_bili(film):
            p = os.path.join(DM_DIR, f"{film.get('bvid')}_{film.get('cid')}.json")
            if os.path.exists(p):
                raw = load_json(p, [])
            elif fetch:
                raw = bili_danmaku(film)
        else:
            raw = side_json(film, ".danmaku.json") or []
    except Exception:
        raw = []
    out = []
    for d in raw:
        try:
            dt = float(d.get("t") or 0)
        except Exception:
            continue
        if abs(dt - t) <= 1.6:
            out.append({"t": dt, "text": str(d.get("text") or "").strip(), "n": 1})
    return out


def _cut(s, n):
    s = str(s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def around_lines(film, t, cine=None, skip=None, fetch=False):
    """前一秒、这一秒、后一秒各一行：「▸0:44 字幕「…」 弹幕 a / b ×3 · 用户名：… · 伴侣名：…」。
    桶跟三帧对齐：第 k 秒的桶是 [t+k−0.5, t+k+0.5)。哪一秒什么都没有就整行省掉。
    skip=(author, t, text)：推送开头已经写了 TA 刚发的那条，这里不再重复。"""
    subs = _ar_subs(film, t)
    pubs = _ar_pub(film, t, cine, fetch)
    ours = [r for r in dm_log(film["id"]) if abs(r["t"] - t) < 1.6]
    if skip:
        ours = [r for r in ours if not (r["author"] == skip[0] and r["text"] == skip[2]
                                        and abs(r["t"] - skip[1]) < 0.6)]
    shown_sub = set()
    lines = []
    for k in (-1, 0, 1):
        c = t + k
        if c < 0:
            continue
        lo, hi = c - 0.5, c + 0.5
        bits, said = [], []
        for cue in subs or []:
            if cue["start"] < hi and cue["end"] + 0.2 >= lo and str(cue["text"]).strip():
                key = (round(cue["start"], 2), cue["text"])
                if key in shown_sub:
                    continue
                shown_sub.add(key)
                said.append(f"「{_cut(cue['text'], AR_SUB_LEN)}」")
        if said:
            bits.append("字幕" + "".join(said[:3]))
        merged = {}
        for d in pubs:
            if lo <= d["t"] < hi and d["text"]:
                m = merged.setdefault(d["text"], {"text": d["text"], "n": 0})
                m["n"] += d["n"]
        top = sorted(merged.values(), key=lambda m: -m["n"])[:AR_PUB_MAX]
        if top:
            bits.append("弹幕 " + " / ".join(_cut(m["text"], AR_PUB_LEN) + (f" ×{m['n']}" if m["n"] > 1 else "")
                                            for m in top))
        for r in [r for r in ours if lo <= r["t"] < hi][:4]:
            bits.append(((companion_name() if r["author"] == "companion" else user_name()) + "：") + _cut(r["text"], AR_OURS_LEN))
        if bits:
            lines.append(("▸" if k == 0 else "  ") + fmt_t(c) + " " + " · ".join(bits))
    return lines, subs is not None



# ── 日志去敏 ──
# B 站接口偶尔把 cookie 字段原样回在报错里；写日志、回页面之前都过一遍。
SECRET_RE = re.compile(
    r"(SESSDATA|bili_jct|DedeUserID[^=\s]*|buvid\d*|ac_time_value|refresh_token)"
    r"\s*=\s*[^;,\s\"\']+", re.I)


def scrub(t):
    t = str(t or "")
    return SECRET_RE.sub(lambda m: m.group(1) + "=…", t)


# ── B 站直连 ──
#
# 视频不落盘。bili.json 里一条记录 = 一部虚拟片子；播放地址每次现问 bili_mcp（回环 REST），
# 按 deadline 缓存；CDN 的字节由本服务按 Range 原样转发给 <video>。
# B 站的 cookie/token 全在 bili_mcp 那头，这里不读。

BV_IN_TEXT = re.compile(r"(BV[0-9A-Za-z]{10})")
AV_RE = re.compile(r"^(?:av)?(\d{1,12})$", re.I)
BAD_HINTS = ("space.bilibili.com", "/lists/", "medialist", "favlist",
             "/channel/", "/festival/", "playlist", "/opus/", "/read/")


def bili_get(path, timeout=30):
    """问本机 bili_mcp 的回环 REST；非 2xx 或 {"error"} 一律抛 RuntimeError。"""
    req = urllib.request.Request(BILI_MCP + path, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        try:
            d = json.loads(e.read().decode("utf-8", "replace") or "{}")
        except Exception:
            d = {}
        raise RuntimeError(d.get("error") or f"bili_mcp HTTP {e.code}")
    except Exception as e:
        raise RuntimeError(f"连不上 bili_mcp（{BILI_MCP} 在跑吗？）: {e}")
    if isinstance(d, dict) and d.get("error"):
        raise RuntimeError(str(d["error"]))
    return d


def bili_post(path, body, timeout=25):
    """回环 POST 给 bili_mcp（点赞 / 收藏这类轻写）。规矩同 bili_get。"""
    raw = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(BILI_MCP + path, data=raw, method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        try:
            d = json.loads(e.read().decode("utf-8", "replace") or "{}")
        except Exception:
            d = {}
        raise RuntimeError(d.get("error") or f"bili_mcp HTTP {e.code}")
    except Exception as e:
        raise RuntimeError(f"连不上 bili_mcp（{BILI_MCP} 在跑吗？）: {e}")
    if isinstance(d, dict) and d.get("error"):
        raise RuntimeError(str(d["error"]))
    return d


def parse_ref(raw):
    """把 TA 贴的东西（链接 / BV / av / b23 / 整句话）变成 (交给 bili_mcp 的 id, 第几 P)。
    分区、合集、收藏夹、空间链接一律拒绝，只收单条视频（番剧 / 影视另走 ep / ss / md）。"""
    s = str(raw or "").strip().strip("\u200b")
    if not s:
        raise ValueError("什么都没贴")
    low = s.lower()
    for bad in BAD_HINTS:
        if bad in low:
            raise ValueError("这是分区/合集/空间链接，只能直连单条视频")
    p = None
    m = re.search(r"[?&]p=(\d{1,4})", s)
    if m:
        p = int(m.group(1))
    m = BV_IN_TEXT.search(s)
    if m:
        return m.group(1), p
    m = PGC_REF_RE.search(s)
    if m:                                        # 番剧 / 影视：ep / ss / md 号、bangumi 链接
        return m.group(1).lower() + m.group(2), None
    m = AV_RE.match(s)
    if m:
        return "av" + m.group(1), p
    m = re.search(r"(?:^|/|\b)av(\d{1,12})", s, re.I)
    if m:
        return "av" + m.group(1), p
    m = re.search(r"(https?://)?(?:www\.)?(b23\.tv|bili2233\.cn)/[A-Za-z0-9]+", s)
    if m:
        return m.group(0), p
    raise ValueError("看不懂这条：只认 B 站链接或 BV 号")


def bili_id(bvid, p):
    return f"bili:{bvid}" + (f"_p{int(p)}" if p and int(p) > 1 else "")


# ── 番剧 / 影视（PGC）──
# 片子 id 是 bili:ep<数字>（每一集一条，和 bili:BV… / live:<房间> 并列）。片单里那条另带 pgc 一块：
# {season_id, ep_id, season_title, index（第N话）, type, type_name}；bvid / aid / cid 是这一集自己的，
# 所以弹幕、封面、评论区、抓帧、私人弹幕都跟普通视频一样。取流走 bili_mcp /pgc/playurl（番剧那套接口）。
# 只放登录账号本来能看的：大会员 / 付费 / 地区 / DRM，B 站不给就照实告诉页面，不绕。
PGC_REF_RE = re.compile(r"(?<![0-9A-Za-z])(ep|ss|md)(\d{1,12})(?![0-9])", re.I)
PGC_FID_RE = re.compile(r"^bili:ep(\d{1,12})$")


class PgcDenied(Exception):
    def __init__(self, msg, reason="other"):
        super().__init__(msg)
        self.reason = reason


def is_pgc(film):
    return bool(film) and bool(film.get("pgc"))


def pgc_blocked(film):
    """B 站在番剧信息里就标了「地区受限」的那一集：不再去问它的弹幕 / 评论 / 字幕（反正放不了，少发几次请求）。"""
    return is_pgc(film) and bool((film.get("pgc") or {}).get("area_limited")) and not film.get("shell_ok")


def pgc_rec_of_bvid(bvid):
    """片单里番剧那一集的 BV 号 → 那一条（字幕 / 评论区改走番剧那条路，不拿番剧的 BV 再去问 view）。"""
    with _lock:
        for b in _bili:
            if b.get("pgc") and b.get("bvid") == bvid:
                return dict(b)
    return None


def bili_get_any(path, timeout=30):
    """同 bili_get，但 4xx 也把对方的 JSON 原样还回来（番剧放不了时要拿 reason），连不上才抛。"""
    req = urllib.request.Request(BILI_MCP + path, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        try:
            d = json.loads(e.read().decode("utf-8", "replace") or "{}")
        except Exception:
            d = {}
        return d if isinstance(d, dict) and d else {"error": f"bili_mcp HTTP {e.code}"}
    except Exception as e:
        raise RuntimeError(f"连不上 bili_mcp（{BILI_MCP} 在跑吗？）: {e}")


_season_cache = {}     # "ss<id>" / "ep<id>" -> (取的时刻, /pgc/season 原样)


def pgc_season(ref, fresh=False):
    key = str(ref)
    hit = _season_cache.get(key)
    if hit and not fresh and time.time() - hit[0] < 300:
        return hit[1]
    v = bili_get("/pgc/season?ref=" + quote(key) + ("&fresh=1" if fresh else ""))
    _season_cache[key] = (time.time(), v)
    if v.get("season_id"):
        _season_cache[f"ss{v['season_id']}"] = (time.time(), v)
    return v


def pgc_rec(season, ep):
    """一集 → 片单里的一条。"""
    title = " ".join(x for x in (season.get("title") or "", ep.get("index") or "") if x).strip()
    return {"id": f"bili:ep{int(ep['ep_id'])}", "bvid": ep.get("bvid"), "aid": ep.get("aid"), "p": 1,
            "cid": ep.get("cid"), "title": (title or f"ep{ep['ep_id']}")[:120],
            "epTitle": (ep.get("long_title") or "")[:80],
            "up": (season.get("type_name") or "")[:40], "duration": ep.get("duration"),
            "cover": ep.get("cover") or season.get("cover") or "", "pages": int(season.get("total") or 0),
            "pgc": {"season_id": season.get("season_id"), "ep_id": int(ep["ep_id"]),
                    "season_title": season.get("title") or "", "index": ep.get("index") or "",
                    "type": season.get("type"), "type_name": season.get("type_name") or "",
                    "badge": ep.get("badge") or "", "area_limited": bool(ep.get("area_limited"))},
            "added_at": time.strftime("%Y-%m-%dT%H:%M:%S")}


def pgc_add(ref):
    """ep / ss / md → 片单里那一集（ss / md＝TA 上次看到的那一集，没看过就第一集）。已在片单就不重复记。"""
    m = re.match(r"^ep(\d+)$", ref)
    if m:
        old = bili_find(f"bili:ep{int(m.group(1))}")
        if old:
            return old, False
    v = pgc_season(ref, fresh=not m)            # 按整部进：现问「上次看到哪一集」
    ep = v.get("pick")
    if not ep or not ep.get("cid"):
        raise RuntimeError("这部还没有能看的正片")
    rec = pgc_rec(v, ep)
    with _lock:
        for b in _bili:
            if b.get("id") == rec["id"]:
                return dict(b), False
    why = pgc_park_reason(rec)                  # 放不了的集不进播放列表（记着，但列表里不显示）
    if why:
        rec["parked"] = why
    with _lock:
        for b in _bili:
            if b.get("id") == rec["id"]:
                return dict(b), False
        _bili.insert(0, rec)
        _prune_parked()
        save_bili()
    return rec, True


# 放不了的集（地区 / 大会员 / 付费 / DRM / 没问到）记在 bili.json 里但带 parked，不进播放列表：
# 页面照样拿得到这一条（打得开、能说清为什么放不了；套在 app 壳里的页面在 TA 设备上自己取到地址时也要靠它）。
# 真放起来了就转正：服务器这头取到地址（/api/bili/direct 成功），或页面报告 app 壳里放起来了（POST /api/bili/keep）。
PARKED_KEEP = 40


def pgc_park_reason(rec):
    """这一集现在能不能放：能 → None（顺手把地址缓存好，页面马上要）；不能 → reason。"""
    if (rec.get("pgc") or {}).get("area_limited"):
        return "area"                            # 番剧信息里就标了地区受限，不再去请求 playurl
    try:
        direct_info(dict(rec, kind="bili"))
        return None
    except PgcDenied as e:
        return e.reason or "other"
    except Exception as e:
        log("番剧入列表前没问到地址", rec.get("id"), e)
        return "unknown"


def _prune_parked():
    """调用方持锁。挂着的记录只留最近 PARKED_KEEP 条，免得 bili.json 越攒越多。"""
    n = 0
    keep = []
    for b in _bili:
        if b.get("parked"):
            n += 1
            if n > PARKED_KEEP:
                continue
        keep.append(b)
    _bili[:] = keep


def bili_unpark(fid, shell=False):
    """挂着的那条转正（进播放列表）。shell=True：是 app 壳在 TA 设备上取到地址放起来的。"""
    with _lock:
        for b in _bili:
            if b.get("id") == fid:
                changed = False
                if b.get("parked"):
                    b.pop("parked", None)
                    changed = True
                if shell and b.get("pgc") and not b.get("shell_ok"):
                    b["shell_ok"] = True         # 服务器这头仍拿不到地址，但 TA 那头能放：弹幕/评论照常去问
                    changed = True
                if changed:
                    save_bili()
                return dict(b)
    return None


def pgc_play(film, qn=None):
    """番剧这一集的地址（bili_mcp /pgc/playurl 原样）；放不了抛 PgcDenied（带 reason）。"""
    g = film.get("pgc") or {}
    d = bili_get_any(f"/pgc/playurl?ep={int(g.get('ep_id'))}&cid={int(film.get('cid'))}&qn={int(qn or 80)}")
    if d.get("error"):
        if d.get("code") == "pgc_denied":
            raise PgcDenied(d["error"], d.get("reason") or "other")
        raise RuntimeError(str(d["error"]))
    if not d.get("url"):
        raise RuntimeError("bili_mcp 没给地址")
    return d


# 分 P：片子 id 里的 `_p2` 是分 P 后缀，不是 BV 号的一部分。要先拆开再问 bili_mcp：
# 把 `BV…_p2` 整个当 bvid 递过去，那头只认前 12 位，拿回来的总是 P1 的字幕。
BILI_FID_RE = re.compile(r"^(?:bili:)?(BV[0-9A-Za-z]{10})(?:_p(\d{1,4}))?$")


def split_bili(s, p=None):
    """'bili:BV…_p2' / 'BV…_p2' / 'BV…'（+ 可选 p）→ (bvid, p)；认不出回 (None, None)。"""
    m = BILI_FID_RE.match(str(s or "").strip())
    if not m:
        return None, None
    pp = m.group(2) or p or 1
    try:
        pp = max(1, int(pp))
    except Exception:
        pp = 1
    return m.group(1), pp


_pages_cache = {}      # bvid -> (取的时刻, /cid 原样)；分 P 列表十分钟内不重复问 B 站


def bili_pages(bvid):
    hit = _pages_cache.get(bvid)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    v = bili_get("/cid?bvid=" + quote(bvid))
    _pages_cache[bvid] = (time.time(), v)
    return v


_info_cache = {}       # bvid -> (取的时刻, 回给页面的那份)；bilitube 播放页：标题下那行小字＋简介


def bili_info(bvid):
    """问 bili_mcp /video（瘦版，简介截 300 字）：UP 名、播放量、发布日、简介。十分钟内不重复问。"""
    hit = _info_cache.get(bvid)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    v = bili_get("/video?id=" + quote(bvid))
    out = {"ok": True, "bvid": bvid, "aid": v.get("aid"), "up": v.get("up") or "", "view": v.get("view"), "danmaku": v.get("danmaku"),
           "like": v.get("like"), "fav": v.get("fav"), "reply": v.get("reply"),
           "pubdate": v.get("pubdate_ts"), "desc": v.get("desc") or "",
           "descCut": int(v.get("desc_len") or 0) > len(v.get("desc") or "")}
    _info_cache[bvid] = (time.time(), out)
    return out


_state_cache = {}      # bvid -> (取的时刻, {liked, favoured, later})：TA 点没点过赞 / 收没收藏 / 在不在稍后再看


def bili_state(bvid, fresh=False, aid=None):
    """问 bili_mcp /state（只读）。缓存 60 秒；TA 在页面上点过之后 bili_state_drop 作废。问不到就空，页面保持原样。"""
    hit = _state_cache.get(bvid)
    if hit and not fresh and time.time() - hit[0] < 60:
        return hit[1]
    try:
        d = bili_get("/state?bvid=" + quote(bvid) + "&later=1" + (f"&aid={int(aid)}" if aid else ""), timeout=15)
    except Exception:
        return {}
    out = {k: bool(d[k]) for k in ("liked", "favoured", "later") if k in d}
    _state_cache[bvid] = (time.time(), out)
    return out


def bili_state_drop(vid):
    m = BV_IN_TEXT.search(str(vid or ""))
    if m:
        _state_cache.pop(m.group(1), None)


def bili_cid_of(bvid, p):
    """某一 P 的 cid：片单里有就用片单那条（不出门），没有就问 bili_mcp 的分 P 列表。"""
    b = bili_find(bili_id(bvid, p))
    if b and b.get("cid"):
        return int(b["cid"])
    pg = next((x for x in (bili_pages(bvid).get("pages") or []) if int(x.get("p") or 0) == int(p)), None)
    return int(pg["cid"]) if pg and pg.get("cid") else None


def bili_subtitle_for(bvid, p, t=None, window=6.0, timeout=30):
    """按 P 取 CC 字幕。带 p＋cid 问 bili_mcp；回来的 cid 对不上（比如 bili_mcp 版本太旧、只会给 P1）
    就当这 P 没字幕：宁可说没有，也别把 P1 的台词当成 P2 的。"""
    p = int(p or 1)
    pr = pgc_rec_of_bvid(bvid)
    if pr and pgc_blocked(pr):
        return {"bvid": bvid, "p": 1, "has": False, "count": 0, "list": [], "note": "这一集这个地区看不了"}
    if pr:                                       # 番剧那一集：按 ep 问（bili_mcp 用番剧信息里的 aid/cid 去取，并核对身份）
        u = (f"/subtitle?bvid=ep{int(pr['pgc']['ep_id'])}&window={float(window):g}"
             + (f"&t={quote(str(t))}" if t not in (None, "") else ""))
        d = bili_get(u, timeout=timeout)
        if int(d.get("cid") or 0) != int(pr.get("cid") or 0):
            return {"bvid": bvid, "p": 1, "has": False, "count": 0, "list": [], "note": "字幕身份对不上，当没有"}
        d.setdefault("p", 1)
        return d
    cid = bili_cid_of(bvid, p) if p > 1 else None
    u = f"/subtitle?bvid={quote(bvid)}&window={float(window):g}" + (f"&t={quote(str(t))}" if t not in (None, "") else "")
    if p > 1:
        if not cid:
            return {"bvid": bvid, "p": p, "has": False, "count": 0, "list": [], "note": f"没有第 {p} P"}
        u += f"&p={p}&cid={cid}"
    d = bili_get(u, timeout=timeout)
    if p > 1 and int(d.get("cid") or 0) != cid:
        return {"bvid": bvid, "p": p, "cid": cid, "has": False, "count": 0, "list": [],
                "note": "bili_mcp 回的字幕不是这一 P 的（可能版本太旧，不支持分 P 字幕），当没有"}
    d.setdefault("p", p)
    return d


def bili_find(fid):
    with _lock:
        for b in _bili:
            if b.get("id") == fid:
                return dict(b)
    return None


def save_bili():
    save_json(BILI_FILE, _bili)


# 没有「播放列表」：bili.json 只是开片要用的小底账（cid、片名、封面地址），不给人看；待看的单子只有一份＝B 站账号的稍后再看。
# 底账只留用得着的：此刻在看的、AI 排在下一个的、有两人弹幕记录的（「一起聊过」要片名封面）、还没转正的番剧集（parked），
# 再加最近开过的 BILITUBE_CATALOG_RECENT 条（来回切片不用再问 B 站）。看过什么以 B 站历史记录为准，这里不另记。
CATALOG_RECENT = int(os.environ.get("BILITUBE_CATALOG_RECENT", "10"))


def _catalog_pinned():
    keep = set()
    with _lock:
        if _state.get("film"):
            keep.add(_state["film"])
    un = upnext_get()
    if un and un.get("film"):
        keep.add(un["film"])
    try:
        keep.update(x["film"] for x in dm_log_films())
    except Exception as e:
        log("底账修剪：读弹幕记录失败，这回不剪", e)
        return None
    return keep


def prune_catalog(recent=None):
    """修底账（调用方别持锁）。recent＝除了钉住的再留最近开过的几条；返回剪掉了几条。"""
    recent = CATALOG_RECENT if recent is None else max(0, int(recent))
    keep = _catalog_pinned()
    if keep is None:
        return 0
    with _lock:
        n_recent, out = 0, []
        order = sorted(_bili, key=lambda b: b.get("seen_at") or b.get("added_at") or "", reverse=True)
        for b in order:
            if b.get("id") in keep or b.get("parked"):
                out.append(b)
            elif n_recent < recent:
                n_recent += 1
                out.append(b)
        cut = len(_bili) - len(out)
        if cut:
            ids = {b["id"] for b in out}
            _bili[:] = [b for b in _bili if b["id"] in ids]
            for fid in list(_playurl.keys()):
                if fid not in ids:
                    _playurl.pop(fid, None)
            save_bili()
    return cut


def _seen(fid):
    """开过一次就把它挪到「最近」那头（只改时间戳，不新加条）。调用方持锁。"""
    for b in _bili:
        if b.get("id") == fid:
            b["seen_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            return


def bili_add(ref):
    """解析 → 问 bili_mcp → 记进 bili.json。已经在片单里的直接返回那条（不重复）。"""
    key, p = parse_ref(ref)
    if re.match(r"^(ep|ss|md)\d+$", key):
        return pgc_add(key)
    if key.startswith("BV"):                     # 已在片单就不出门（MCP 点播、页面点开卡片时每次都会先 add）
        old = bili_find(bili_id(key, p))
        if old:
            return old, False
        old = pgc_rec_of_bvid(key)
        if old:
            return old, False
    v = bili_get("/cid?bvid=" + quote(key) + (f"&p={p}" if p else ""))
    if v.get("pgc_ep") or (v.get("pgc") or {}).get("ep_id"):     # 番剧那一集的 BV / b23 短链：改记成番剧
        return pgc_add(f"ep{int(v.get('pgc_ep') or v['pgc']['ep_id'])}")
    bvid, cid = v.get("bvid"), v.get("cid")
    if not bvid or not cid:
        raise RuntimeError("B 站没给 bvid/cid")
    pages = v.get("pages") or []
    fid = bili_id(bvid, p)
    _pages_cache[bvid] = (time.time(), {k: v.get(k) for k in ("bvid", "aid", "title", "duration", "cover", "up", "cid", "pages")})
    title = (v.get("title") or bvid).strip()
    if p and len(pages) > 1:
        part = (v.get("part") or "").strip()
        title = f"{title} · P{p}" + (f" {part}" if part and part != title else "")
    rec = {"id": fid, "bvid": bvid, "aid": v.get("aid"), "p": int(p or 1), "cid": cid,
           "title": title[:120], "up": (v.get("up") or "")[:40],
           "duration": v.get("duration"), "cover": v.get("cover") or "",
           "pages": len(pages), "added_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    with _lock:
        for b in _bili:
            if b.get("id") == fid:
                return dict(b), False
        _bili.insert(0, rec)
        save_bili()
    return rec, True


# 代理那一路用：B 站给海外服务器分的 CDN 主机（如 upos-sz-mirrorcosov）常常只有 100～200 KB/s，
# 同一条签名路径换到别的镜像主机能到 1～2 MB/s（一次实测：estgoss 1997 / hw 1134 / cos 706 / ali 419 / cosov 142，单位 KB/s）。
# 拿到 playurl 后并行测一次速：每个镜像拉 512K，谁快用谁；全都不通就用原地址。
MIRROR_HOSTS = ("upos-sz-estgoss.bilivideo.com", "upos-sz-mirrorhw.bilivideo.com", "upos-sz-mirrorcos.bilivideo.com",
                "upos-sz-mirrorali.bilivideo.com", "upos-sz-mirrorcosov.bilivideo.com", "upos-sz-mirroraliov.bilivideo.com")
def pick_fastest(url):
    import threading
    try:
        scheme, rest = url.split("://", 1)
        host, path = rest.split("/", 1)
    except ValueError:
        return url
    if not host.endswith("bilivideo.com"):
        return url
    cands = [h for h in MIRROR_HOSTS if h != host] + [host]
    results = {}
    def probe(h):
        u = f"{scheme}://{h}/{path}"
        req = urllib.request.Request(u, headers={"User-Agent": STREAM_UA, "Referer": STREAM_REFERER, "Range": "bytes=1000000-1524287"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                n = len(r.read())
            dt = max(time.time() - t0, 0.05)
            results[h] = n / dt if n > 100000 else 0
        except Exception:
            results[h] = 0
    ts = [threading.Thread(target=probe, args=(h,), daemon=True) for h in cands]
    for t in ts: t.start()
    for t in ts: t.join(timeout=9)
    best = max(results.items(), key=lambda kv: kv[1], default=(host, 0))
    if best[1] <= 0:
        return url
    log("CDN 测速", host, "→", best[0], f"{best[1]/1024:.0f}KB/s", {h: f"{v/1024:.0f}" for h, v in results.items()})
    return f"{scheme}://{best[0]}/{path}"

def stream_url(film, force=False):
    """这部虚拟片子此刻能用的 CDN 地址（按 deadline 缓存；force=True 强制重问）。"""
    fid = film["id"]
    now = int(time.time())
    with _lock:
        c = _playurl.get(fid)
        if c and not force and c.get("expires_at", 0) - PLAYURL_SLACK > now:
            return c
    try:
        if is_pgc(film):
            d = pgc_play(film)
        else:
            d = bili_get(f"/playurl?bvid={quote(str(film.get('bvid')))}&cid={int(film.get('cid'))}&qn=80")
    except Exception as e:
        log("playurl 失败", fid, e)
        return None
    if not d.get("url"):
        return None
    info = {"url": pick_fastest(d["url"]), "backup_urls": d.get("backup_urls") or [],
            "expires_at": int(d.get("expires_at") or now + 3600),
            "size": d.get("size"), "qn": d.get("qn"), "format": d.get("format")}
    with _lock:
        _playurl[fid] = info
    return info


# ══════════════════════════════════════════════════════════════
# 直连：
# 服务器只发地址、不转字节：/api/bili/direct 给一串「同一条签名路径 × 几台国内镜像主机」，
# 页面在 TA 那头各试一小口、挑最快的直接交给 <video>。这样不用绕「国内 CDN → 服务器 → TA」的远路。
# 页面带 no-referrer，CDN 回的 ACAO 是反射的，canvas 抓帧不会被污染。
# 实测：upos 路径换主机照样 206；不带 Referer 放行；带外站 Referer 时 akamai 那台 403、bilivideo 几台放行；
# 只拦 curl 这类 UA；地址有效期约 2 小时。直连不通（TA 那头的网络、签名绑 IP 等）页面自己退回 /api/bili/stream 代理。
# ══════════════════════════════════════════════════════════════
DIRECT_HOSTS = tuple(h.strip() for h in os.environ.get(
    "BILITUBE_DIRECT_HOSTS",
    "upos-sz-mirrorcos.bilivideo.com,upos-sz-mirrorali.bilivideo.com,"
    "upos-sz-mirrorhw.bilivideo.com,upos-sz-estgoss.bilivideo.com").split(",") if h.strip())
DIRECT_MIN_LEFT = 900          # 剩不到 15 分钟的地址不发给页面，现问一份新的（新的约 2 小时）
_direct_pu = {}                # {filmId: {url,backup_urls,expires_at,size,qn,format}}（不测速，原样）


_pgc_accept = {}               # {filmId: 上次 B 站给的档 [{qn,desc,vip_only}]}：页面只能在这里面挑


def direct_info(film, fresh=False, qn=None):
    """给页面直连用的地址（不做服务器这头的镜像测速：那是替代理挑的，对 TA 那头没意义）。
    番剧：qn 只认 B 站上次给过、且账号能看的档（vip_only 的不认），别的一律按默认问。"""
    fid = film["id"]
    now = int(time.time())
    if is_pgc(film):
        ok = [a["qn"] for a in (_pgc_accept.get(fid) or []) if not a.get("vip_only")]
        qn = int(qn) if qn and int(qn) in ok else None
    else:
        qn = None
    key = fid if not qn else f"{fid}@{qn}"
    with _lock:
        c = _direct_pu.get(key)
    if c and not fresh and c.get("expires_at", 0) - DIRECT_MIN_LEFT > now:
        return c
    if is_pgc(film):
        d = pgc_play(film, qn)                   # 放不了抛 PgcDenied，页面照实说
    else:
        d = bili_get(f"/playurl?bvid={quote(str(film.get('bvid')))}&cid={int(film.get('cid'))}&qn=80")
    if not d.get("url"):
        raise RuntimeError("bili_mcp 没给地址")
    info = {"url": d["url"], "backup_urls": d.get("backup_urls") or [],
            "expires_at": int(d.get("expires_at") or now + 3600),
            "size": d.get("size"), "qn": d.get("qn"), "format": d.get("format")}
    if is_pgc(film):
        info["accept"] = d.get("accept") or []
        with _lock:
            _pgc_accept[fid] = info["accept"]
    with _lock:
        _direct_pu[key] = info
    return info


def direct_reply(f, info):
    """/api/bili/direct 的回信（/api/bili/add 带 direct=1 时也原样附一份，页面省一趟来回）。"""
    return {"ok": True, "id": f["id"], "urls": direct_urls(info),
            "expiresAt": info.get("expires_at"), "size": info.get("size"),
            "qn": info.get("qn"), "format": info.get("format"),
            **({"accept": info.get("accept") or []} if is_pgc(f) else {}),
            "proxy": bili_film_entry(f)["src"], "now": int(time.time())}


def direct_urls(info):
    """同一条签名路径 × 国内镜像主机（在前）＋ B 站原给的主机 ＋ 备用地址。只要 https。"""
    out = []
    def add(u):
        if u and u.startswith("https://") and u not in out:
            out.append(u)
    try:
        path = info["url"].split("://", 1)[1].split("/", 1)[1]
    except (IndexError, KeyError):
        path = None
    if path and path.startswith(("upgcxcode/", "ugaxcode/", "v1/resource/")):   # upos 那套路径，换主机签名照认
        for h in DIRECT_HOSTS:
            add(f"https://{h}/{path}")
    add(info.get("url"))
    for u in info.get("backup_urls") or []:
        add(u)
    return out


# ══════════════════════════════════════════════════════════════
# 直播：
# 贴直播间号/链接 → /api/live/room 查房间 → /api/live/play 取 getRoomPlayInfo 的 HLS 地址
# → 页面 hls.js 直连（B 站直播 CDN 回 ACAO:*）；直连不通再走 /api/live/m3u8（本服务改写清单）＋ /api/live/seg（转发分片）。
# 直播取流可选用一份自己的 cookie 文件（BILITUBE_COOKIE；登录态才给「原画」；没有就匿名，最高「超清」）。
# cookie 的内容不进日志、不进返回、不进报错。直播间的公开弹幕见下面「直播公开弹幕」一节。
# ══════════════════════════════════════════════════════════════
LIVE_API = "https://api.live.bilibili.com"
LIVE_REFERER = "https://live.bilibili.com/"
COOKIE_FILE = os.environ.get("BILITUBE_COOKIE", "")      # 可选：直播接口用的 B 站 cookie 文件（一行 k=v; k=v），不配就匿名
LIVE_SEG_HOST_RE = re.compile(r"^https://[0-9a-z.\-]+\.(?:bilivideo\.(?:com|cn)|akamaized\.net)(?::\d+)?/", re.I)
LIVE_URL_RE = re.compile(r"live\.bilibili\.com/(?:h5/|blanc/|blackboard/[^/]*/)?(\d{1,12})", re.I)
LIVE_QN_DESC = {30000: "杜比", 20000: "4K", 15000: "2K", 10000: "原画", 400: "蓝光", 250: "超清", 150: "高清", 80: "流畅"}
_live_room = {}     # {room: (at, info)}
_live_play = {}     # {(room, qn): (at, info)}
_live_pick = {}     # {(room, qn, kind): 代理那路此刻在用的清单地址}

# 直播线路：播放地址是服务器替页面问的，B 站按「来问的人」在哪儿配节点，服务器在海外就配海外节点（ov-gotchaNN），
# 看的人在国内再去直连它就远。同一条签名路径换成同号的国内节点（cn-gotchaNN，带 b 的备机同理）有时照样认、照样出流，
# 有时 403 / 404 / 解析不到 —— 所以服务器只「列候选」，谁能用、谁快，由页面在看的人那头各试一小口再定（同点播 DIRECT_HOSTS）。
# 规则表：「原前缀>近处前缀」，逗号分隔多条；只认 [d1--]<原前缀><数字>[b].bilivideo.com 这种形态，别的主机名原样不动。
# 服务器和看的人在同一地区就设成空串（BILITUBE_LIVE_NEAR=）关掉。
LIVE_NEAR_SWAPS = tuple((a.strip(), b.strip()) for a, _, b in
                        (r.partition(">") for r in os.environ.get("BILITUBE_LIVE_NEAR", "ov-gotcha>cn-gotcha").split(","))
                        if re.fullmatch(r"[a-z\-]+", a.strip()) and re.fullmatch(r"[a-z\-]+", b.strip()))


def live_near_urls(urls, limit=2):
    """[原地址…] → [近处候选…] ＋ [原地址…]（原地址永远在里面兜底）。只换主机名，路径和签名一个字不动。"""
    near, orig = [], []
    for u in list(urls or [])[:limit]:
        if u not in orig:
            orig.append(u)
        m = re.match(r"^https://((?:d1--)?)([a-z0-9\-]+?)(\d{1,4}b?)(\.bilivideo\.com)(/.*)$", u or "", re.S)
        if not m:
            continue
        for src, dst in LIVE_NEAR_SWAPS:
            if m.group(2) == src:
                v = f"https://{m.group(1)}{dst}{m.group(3)}{m.group(4)}{m.group(5)}"
                if v not in near and v not in orig:
                    near.append(v)
    return near + orig


def _cookie_header():
    """bilitube 自己那份 cookie → 'k=v; k=v'。读不到就空串（匿名）。"""
    try:
        with open(COOKIE_FILE, encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except OSError:
        return ""
    pairs = []
    if "\t" in raw:
        for line in raw.splitlines():
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            if not line.strip() or line.startswith("#"):
                continue
            f = line.split("\t")
            if len(f) >= 7 and "bilibili" in f[0] and f[5]:
                pairs.append(f"{f[5]}={f[6].strip()}")
    else:
        for part in raw.replace("\n", ";").split(";"):
            part = part.strip()
            if "=" in part:
                pairs.append(part)
    return "; ".join(pairs)


def live_api(path, params, with_cookie=True, timeout=10):
    from urllib.parse import urlencode
    url = f"{LIVE_API}{path}?{urlencode(params)}"
    headers = {"User-Agent": STREAM_UA, "Referer": LIVE_REFERER, "Accept": "application/json"}
    ck = _cookie_header() if with_cookie else ""
    if ck:
        headers["Cookie"] = ck
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"B 站直播接口 HTTP {e.code}")
    except Exception as e:
        raise RuntimeError(f"连不上 B 站直播接口：{type(e).__name__}")
    if not isinstance(d, dict) or d.get("code") != 0:
        raise RuntimeError(f"B 站直播接口说：{scrub(str((d or {}).get('message') or (d or {}).get('msg') or (d or {}).get('code')))}")
    return d.get("data") or {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _short_target(url):
    """b23.tv 短链只看它往哪儿跳，不跟过去（live 页面本身从这台机器请求会 412）。"""
    if not url.startswith("http"):
        url = "https://" + url
    op = urllib.request.build_opener(_NoRedirect)
    try:
        r = op.open(urllib.request.Request(url, headers={"User-Agent": "Wget/1.21"}), timeout=8)
        return r.headers.get("Location") or r.geturl() or ""
    except urllib.error.HTTPError as e:
        return e.headers.get("Location") or ""
    except Exception:
        return ""


def parse_live_ref(raw):
    """直播间号 / live.bilibili.com 链接 / b23 短链 / 一句带链接的话 → 房间号（可能是短号）。"""
    s = str(raw or "").strip().strip("​")
    if not s:
        raise ValueError("什么都没贴")
    m = LIVE_URL_RE.search(s)
    if m:
        return int(m.group(1))
    m = re.search(r"(?:https?://)?(?:b23\.tv|bili2233\.cn)/[A-Za-z0-9]+", s)
    if m:
        m2 = LIVE_URL_RE.search(_short_target(m.group(0)))
        if m2:
            return int(m2.group(1))
        raise ValueError("这条短链指的不是直播间")
    m = re.fullmatch(r"\D{0,8}?(\d{1,12})\D{0,4}", s)
    if m:
        return int(m.group(1))
    raise ValueError("看不懂这条：贴直播间号，或者 live.bilibili.com 的链接")


def live_room(room, fresh=False):
    """房间号（短号也行）→ {room, shortId, uid, title, uname, face, cover, live, area, online}。缓存 30 秒。"""
    room = int(room)
    now = time.time()
    with _lock:
        c = _live_room.get(room)
    if c and not fresh and now - c[0] < 30:
        return c[1]
    init = live_api("/room/v1/Room/room_init", {"id": room}, with_cookie=False)
    real = int(init.get("room_id") or room)
    info = {"room": real, "shortId": int(init.get("short_id") or 0), "uid": int(init.get("uid") or 0),
            "live": int(init.get("live_status") or 0) == 1, "locked": bool(init.get("is_locked")),
            "encrypted": bool(init.get("encrypted")) and not init.get("pwd_verified"),
            "title": "", "uname": "", "face": "", "cover": "", "area": "", "online": 0}
    try:
        gi = live_api("/room/v1/Room/get_info", {"room_id": real}, with_cookie=False)
        info.update(title=gi.get("title") or "", cover=gi.get("user_cover") or gi.get("keyframe") or "",
                    area=gi.get("area_name") or "", online=int(gi.get("online") or 0),
                    live=int(gi.get("live_status") or 0) == 1)
    except Exception as e:
        log("直播间信息没拿到", real, e)
    if info["uid"]:
        try:
            mi = live_api("/live_user/v1/Master/info", {"uid": info["uid"]}, with_cookie=False)
            u = mi.get("info") or {}
            info.update(uname=u.get("uname") or "", face=u.get("face") or "")
        except Exception as e:
            log("主播信息没拿到", real, e)
    with _lock:
        _live_room[room] = (now, info)
        _live_room[real] = (now, info)
    return info


def live_play(room, qn=10000, fresh=False):
    """getRoomPlayInfo → 浏览器能直接放的 HLS（avc，fmp4 在前、ts 在后）。缓存 20 秒。"""
    room, qn = int(room), int(qn)
    key = (room, qn)
    now = time.time()
    with _lock:
        c = _live_play.get(key)
    if c and not fresh and now - c[0] < 20:
        return c[1]
    params = {"room_id": room, "protocol": "0,1", "format": "0,1,2", "codec": "0", "qn": qn,
              "platform": "web", "ptype": 8, "dolby": 5, "panorama": 1}
    try:
        d = live_api("/xlive/web-room/v2/index/getRoomPlayInfo", params)
    except RuntimeError:
        d = live_api("/xlive/web-room/v2/index/getRoomPlayInfo", params, with_cookie=False)
    if int(d.get("live_status") or 0) != 1:
        raise LookupError("这个直播间现在没在播")
    pu = ((d.get("playurl_info") or {}).get("playurl") or {})
    accept = [{"qn": int(x.get("qn")), "desc": x.get("desc") or LIVE_QN_DESC.get(int(x.get("qn")), str(x.get("qn")))}
              for x in (pu.get("g_qn_desc") or []) if x.get("qn") is not None]
    hls, cur_qn, acc_qn, expires = {}, None, [], 0
    flv = []      # FLV 是一条长连接，不像 HLS 每 1 秒一片都要来回请求一次（离 CDN 远时 HLS 容易追不上）
    for s in pu.get("stream") or []:
        if s.get("protocol_name") == "http_stream":
            for f in s.get("format") or []:
                for c in (f.get("codec") or []) if f.get("format_name") == "flv" else []:
                    if c.get("codec_name") != "avc":
                        continue
                    for ui in c.get("url_info") or []:
                        u = f"{ui.get('host', '')}{c.get('base_url', '')}{ui.get('extra', '')}"
                        if u.startswith("https://"):
                            flv.append(u)
                    cur_qn = cur_qn or c.get("current_qn")
                    acc_qn = acc_qn or c.get("accept_qn") or []
            continue
        if s.get("protocol_name") != "http_hls":
            continue
        for f in s.get("format") or []:
            kind = f.get("format_name")          # fmp4 / ts
            for c in f.get("codec") or []:
                if c.get("codec_name") != "avc":
                    continue
                urls = []
                for ui in c.get("url_info") or []:
                    u = f"{ui.get('host', '')}{c.get('base_url', '')}{ui.get('extra', '')}"
                    if u.startswith("https://"):
                        urls.append(u)
                        m = re.search(r"[?&]expires=(\d+)", u)
                        if m:
                            expires = max(expires, int(m.group(1)))
                if urls and kind in ("fmp4", "ts"):
                    hls[kind] = urls
                    cur_qn = c.get("current_qn") or cur_qn
                    acc_qn = c.get("accept_qn") or acc_qn
    if not hls and not flv:
        raise RuntimeError("B 站没给这间房的 HLS 流")
    order = [k for k in ("fmp4", "ts") if k in hls]
    info = {"room": room, "qn": cur_qn, "wantQn": qn,
            "accept": [a for a in accept if not acc_qn or a["qn"] in acc_qn],
            "streams": [{"kind": k, "urls": hls[k], "cands": live_near_urls(hls[k])} for k in order],
            "flv": flv[:3],
            "flvCands": live_near_urls(flv),     # 近处在前、B 站原给的在后；页面各试一小口再挑（见 LIVE_NEAR_SWAPS）
            "expiresAt": expires or int(now) + 3600}
    with _lock:
        _live_play[key] = (now, info)
    return info


# 直播也能一起看：
# 页面每次请求 /api/live/play 就记一笔「TA 最近开的直播间」（打开、重连、换清晰度、地址到期都会来），
# /api/now 带出去，AI「where」看得见；「look film=live:<房间号>」（或 film=live＝TA 最近那间）从直播流现抓，
# FLV 优先（一条长连接，B 站会先给最近一个关键帧），带超时，一次只起一个 ffmpeg，不进缓存。
_live_seen = {}                       # {"room": 房间号, "at": 秒}
_live_grab_lock = threading.Lock()
LIVE_GRAB_TIMEOUT = 15                # 每一路 ffmpeg 最多等这么久
LIVE_SEEN_MAX = 3 * 3600              # 超过这么久没来要过地址，就不当 TA「在看」了


LIVE_WATCH_SEC = 6                    # 直播页每两秒请求 /api/dm/say?live=<房间>；6 秒内来过＝TA 此刻开着这间
LIVE_HOLD_SEC = 45                    # 网络抖一下（切 Wi-Fi／流量）十几秒没来问：这么久之内仍当 TA「还在这间直播」，
                                      # AI 回的弹幕照样进池子，页面连回来（两分钟内）就飘；过了就明说没发出去，不改投别的片子
LIVE_DM_SHOT_SEC = 120                # TA 在直播里发弹幕后两分钟内，look film=live:<房间> 默认给 TA 发弹幕那一刻存的三帧
_live_dm_last = {}                    # {room: (存下的时刻, 那条弹幕的 id)}


def live_seen():
    """TA 最近开的直播间（只读缓存，不出门）：{room, agoSec, watching, title, uname, lastDm} 或 None。"""
    with _lock:
        s = dict(_live_seen)
        c = _live_room.get(s.get("room"))
        d = _live_dm_last.get(s.get("room"))
    if not s.get("room"):
        return None
    ago = int(time.time() - max(s.get("at") or 0, s.get("poll") or 0))
    if ago > LIVE_SEEN_MAX:
        return None
    i = c[1] if c else {}
    out = {"room": s["room"], "agoSec": ago, "watching": live_watch_room() == s["room"],
           "title": i.get("title") or "", "uname": i.get("uname") or ""}
    if d and time.time() - d[0] <= LIVE_DM_SHOT_SEC:
        out["lastDm"] = {"id": d[1], "agoSec": int(time.time() - d[0])}
    return out


def live_watch_room():
    """TA 此刻开着的直播间（直播页 6 秒内来问过弹幕）；没有回 None。"""
    with _lock:
        s = dict(_live_seen)
    if s.get("room") and time.time() - (s.get("poll") or 0) < LIVE_WATCH_SEC:
        return int(s["room"])
    return None


def live_hold_room():
    """TA 刚才还开着的直播间（直播页 LIVE_HOLD_SEC 秒内来问过）：(房间号, 几秒没来问了) 或 None。网络抖一下不算走开。"""
    with _lock:
        s = dict(_live_seen)
    if s.get("room") and s.get("poll"):
        ago = time.time() - s["poll"]
        if ago < LIVE_HOLD_SEC:
            return int(s["room"]), int(ago)
    return None


LIVE_FID_RE = re.compile(r"(?i)live:(\d{1,12})(?::[0-9a-z]{1,20})?")


def live_fid(raw):
    """live:<房间> 或推送里带弹幕 id 的 live:<房间>:<id> → 'live:<真房间号>'；不是直播回 None。只查缓存，不出门。"""
    m = LIVE_FID_RE.fullmatch(str(raw or "").strip())
    if not m:
        return None
    room = int(m.group(1))
    with _lock:
        c = _live_room.get(room)
    if c and c[1].get("room"):
        room = int(c[1]["room"])
    return f"live:{room}"


def live_title(fid):
    """live:<房间> → 「直播 · 主播 · 标题」：先看缓存，再看 TA 发弹幕时记下的那份，都没有就写房间号。"""
    tail = str(fid or "")[5:]
    room = int(tail) if tail.isdigit() else 0
    with _lock:
        c = _live_room.get(room)
    i = dict(c[1]) if c else {}
    if not i.get("title"):
        for r in reversed(journal_read(str(fid), ("danmaku",))):
            if r.get("roomTitle") or r.get("uname"):
                i = {"title": r.get("roomTitle"), "uname": r.get("uname")}
                break
    return " · ".join(x for x in ("直播", i.get("uname"), i.get("title") or f"房间 {room}") if x)


def live_dm_shot_path(room, lid):
    return os.path.join(FRAME_DIR, f"live-dm-{int(room)}-{re.sub(r'[^0-9a-z]', '', str(lid))[:20]}.jpg")


def live_dm_shot_save(room, lid):
    """TA 在直播里发弹幕的那一刻：服务端现抓最近三秒存一份（一天后清掉）。返回 (True, None) 或 (False, 原因)。"""
    try:
        data, why = live_frame_bytes(room, 3, TRI_W)
    except LookupError as e:
        return False, str(e)
    except Exception as e:
        return False, f"抓帧出错：{type(e).__name__}"
    if not data:
        return False, why
    return live_dm_shot_store(room, lid, data)


def live_dm_shot_page(raw_b64):
    """页面从 TA 自己的 <video> 截的三帧竖拼（TA 点进输入框那一刻＋前两秒），优先用它。
    服务端现抓有两处对不上 TA 眼前：一是晚了 TA 打字那十几秒，二是服务端连 CDN 拿到的是缓存段
    （比直播最新画面早 5～10 秒，每次不一样）。认得出是 JPEG、不超过 SAY_FRAME_MAX 才收；不收回 None。"""
    if not raw_b64 or not isinstance(raw_b64, str):
        return None
    try:
        raw = base64.b64decode(raw_b64, validate=True)
    except Exception:
        return None
    if len(raw) < 1024 or len(raw) > SAY_FRAME_MAX or raw[:3] != b"\xff\xd8\xff":
        return None
    return raw


def live_dm_shot_store(room, lid, data):
    p = live_dm_shot_path(room, lid)
    try:
        os.makedirs(FRAME_DIR, exist_ok=True)
        with open(p + ".tmp", "wb") as f:
            f.write(data)
        os.replace(p + ".tmp", p)
        with _lock:
            _live_dm_last[int(room)] = (time.time(), str(lid))
        for n in os.listdir(FRAME_DIR):
            q = os.path.join(FRAME_DIR, n)
            if n.startswith("live-dm-") and time.time() - os.path.getmtime(q) > 86400:
                os.remove(q)
    except OSError as e:
        return False, f"存不下来：{e}"
    return True, None


def live_dm_shot_bytes(room, lid=None):
    """TA 发弹幕那一刻存的三帧：给 lid 拿那条；不给就拿这间房两分钟内最近那条。没有回 None。"""
    if not lid:
        with _lock:
            c = _live_dm_last.get(int(room))
        if not c or time.time() - c[0] > LIVE_DM_SHOT_SEC:
            return None
        lid = c[1]
    try:
        with open(live_dm_shot_path(room, lid), "rb") as f:
            return f.read()
    except OSError:
        return None


# AI 主动 look 直播（不带弹幕 id）时：TA 开着这间的页面，就向页面要一张 TA 屏幕上此刻的三帧。
# 服务端现抓的画面和 TA 的播放器差几秒到十几秒，对不上 TA 眼前。做法：look 来了就挂一个请求号，
# 页面每两秒请求 /dm/say 时看到 shotReq 就当场拼三帧，传回 POST /api/live/lookshot；这边最多等 LIVE_LOOK_WAIT 秒。
# 等不到（没开着、页面不支持、切到后台、画面截不了、没回）就改用服务端现抓，并在响应头 X-Live-Shot / X-Live-Why 里写明图是哪来的。
# 页面只在被要的时候传一张，平时不传图。
LIVE_LOOK_WAIT = 4.0                  # 直播页每两秒来一次，最坏等一轮半
LIVE_LOOK_WHY = ("hidden", "tainted", "nodecode", "bad")   # 页面能回的「截不了」原因
_live_look_cv = threading.Condition(threading.Lock())
_live_look_req = {}                   # {room: {"id": 号, "at": 挂号时刻}}
_live_look_got = {}                   # {号: (收到时刻, jpeg 或 None, 原因或 None)}
_live_look_n = [0]


def live_look_pending(room):
    """这间房有没有在等页面回图：有回号，没有回 None（放进 /dm/say 的响应带给页面）。"""
    with _live_look_cv:
        r = _live_look_req.get(int(room))
        if r and time.time() - r["at"] <= LIVE_LOOK_WAIT + 1:
            return r["id"]
    return None


def live_look_put(room, rid, raw_b64=None, err=None):
    """页面回的图（或截不了的原因）。号对不上回 False。"""
    data = live_dm_shot_page(raw_b64) if raw_b64 else None
    why = None if data else (err if err in LIVE_LOOK_WHY else "bad")
    with _live_look_cv:
        r = _live_look_req.get(int(room))
        if not r or r["id"] != str(rid):
            return False
        _live_look_req.pop(int(room), None)
        now = time.time()
        for k in [k for k, v in _live_look_got.items() if now - v[0] > 30]:
            del _live_look_got[k]
        _live_look_got[str(rid)] = (now, data, why)
        _live_look_cv.notify_all()
    return True


def live_look_ask(room, wait=None):
    """向 TA 开着的页面要一张此刻的三帧。返回 (jpeg, None) 或 (None, 原因)：
    closed＝TA 没开着这间｜old＝页面不支持回图｜timeout＝没回｜hidden/tainted/nodecode/bad＝页面说截不了。"""
    room, wait = int(room), LIVE_LOOK_WAIT if wait is None else wait
    with _lock:
        s = dict(_live_seen)
    if live_watch_room() != room:
        return None, "closed"
    if not s.get("ls"):
        return None, "old"
    end = time.time() + wait
    with _live_look_cv:
        r = _live_look_req.get(room)
        if r and time.time() - r["at"] < wait:           # 已经有一个请求在等：共用它
            rid = r["id"]
        else:
            _live_look_n[0] += 1
            rid = f"{int(time.time()) % 100000:x}{_live_look_n[0]}"
            _live_look_req[room] = {"id": rid, "at": time.time()}
        while rid not in _live_look_got:
            left = end - time.time()
            if left <= 0:
                break
            _live_look_cv.wait(left)
        got = _live_look_got.get(rid)
        if not got:
            if (_live_look_req.get(room) or {}).get("id") == rid:
                _live_look_req.pop(room, None)
            return None, "timeout"
    return (got[1], None) if got[1] else (None, got[2])


def live_room_arg(raw):
    """film=live:<房间号> / live / 直播 → 真房间号；认不出回 None。"""
    s = str(raw or "").strip()
    m = re.fullmatch(r"(?i)live:(\d{1,12})", s)
    if m:
        room = int(m.group(1))
        try:
            return int(live_room(room).get("room") or room)      # 短号 → 真号
        except Exception:
            return room
    if s.lower() == "live" or s == "直播":
        ls = live_seen()
        return ls["room"] if ls else None
    return None


def live_frame_bytes(room, frames=3, width=TRI_W):
    """直播间此刻的画面：frames=3 → 最近约三秒、每秒一帧竖着摞（最下面那帧最新、粉框）；frames=1 → 单帧。
    返回 (jpg 字节, None) 或 (None, 抓不到的原因)。主播没在播抛 LookupError。"""
    n = 1 if int(frames) == 1 else 3
    width = max(160, min(1280 if n == 1 else 640, int(width)))
    if not _live_grab_lock.acquire(timeout=LIVE_GRAB_TIMEOUT):
        return None, "上一张还在抓，等一下再看"
    try:
        info = live_play(room, 400 if n == 1 else 250)
        srcs = list(info.get("flv") or [])[:2]
        srcs += [u for s in info["streams"] if s["kind"] == "fmp4" for u in s["urls"][:1]]
        if not srcs:
            return None, "B 站没给这间房能抓的流"
        fs = max(11, width // 24)
        vf = (f"fps=1,scale={width}:-2,setsar=1,"
              f"drawtext=fontfile={TRI_FONT}:text='LIVE %{{eif\\:n-{n - 1}\\:d}}s':x=5:y=5:fontsize={fs}:"
              f"fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=3,"
              f"drawbox=x=0:y=0:w=iw:h=ih:color=0xff9ec4:t=3:enable='eq(n,{n - 1})'")
        if n > 1:
            vf += f",tile=1x{n}:padding=4:color=0x1c1420"
        os.makedirs(FRAME_DIR, exist_ok=True)
        p = os.path.join(FRAME_DIR, f"live-{int(room)}-{os.getpid()}-{threading.get_ident()}.jpg")
        why = "抓帧超时"
        for src in srcs:
            cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y",
                   "-headers", f"Referer: {LIVE_REFERER}\r\nUser-Agent: {STREAM_UA}\r\n",
                   "-rw_timeout", str(LIVE_GRAB_TIMEOUT * 1000000), "-i", src,
                   "-an", "-vf", vf, "-frames:v", "1", "-q:v", str(TRI_Q), p]
            try:
                r = run(cmd, timeout=LIVE_GRAB_TIMEOUT + 3)
                if r.returncode == 0 and os.path.exists(p) and os.path.getsize(p) > 0:
                    with open(p, "rb") as f:
                        return f.read(), None
                why = "ffmpeg 没抓到画面"
                log("直播抓帧失败", room, scrub(r.stderr.decode("utf-8", "replace")[:200]))
            except subprocess.TimeoutExpired:
                why = f"{LIVE_GRAB_TIMEOUT} 秒内没从直播流里抓到画面"
                log("直播抓帧超时", room)
            except Exception as e:
                why = f"抓帧出错：{type(e).__name__}"
                log("直播抓帧炸了", room, e)
            finally:
                try:
                    os.remove(p)
                except OSError:
                    pass
        return None, why
    finally:
        _live_grab_lock.release()


# ── 直播公开弹幕（只读）──
# TA 开着某间直播时，服务器替 TA 连那间房的弹幕长连接（B 站直播的 WebSocket 弹幕协议），
# 只在内存里留最近两分钟的文字弹幕：页面每两秒请求 /api/dm/say?live=&pub= 时顺带捎回去，在画面上飘过；
# TA 发弹幕时把同类合并计数，拼一行「这会儿弹幕：…」放进推送。不落盘、不往 B 站发任何东西。
# 入场券（token＋主机）由 bili_mcp 的只读接口 /live_danmu 去取（受它的风控开关管），匿名身份；
# 连接本身是 stdlib 写的最小 WebSocket 客户端，请求 protover=2（zlib 压缩，标准库能解，不用 brotli）。
LDM_ON = os.environ.get("BILITUBE_LIVE_DM", "1") != "0"   # 0＝整套不连
LDM_RING_SEC = 120            # 环形缓冲只留最近这么多秒
LDM_RING_MAX = 4000           # 最多这么多条（热门房一秒几十条，两分钟也就几千）
LDM_SUM_WIN = 20              # TA 发弹幕时取前 20 秒到那一刻的公开弹幕做摘要
LDM_SUM_TOP = 5               # 摘要最多几句
LDM_SUM_MIN = 2               # 至少出现两次才进摘要（散句不带）
LDM_SUM_CHARS = 80            # 摘要整行上限（含前缀）
LDM_SUM_ITEM = 12             # 单句最多几个字，超了截断加「…」
LDM_POLL_MAX = 40             # 页面每问一次最多捎回几条（2 秒一问 → 每秒 20 条封顶，多的丢最旧的）
LDM_POLL_FRESH = 6.0          # 比这更旧的不捎（页面断了一阵再回来，不一次补一大摞）
LDM_TEXT_MAX = 40             # 单条弹幕超过这么长就截
LDM_LINGER_SEC = 15           # TA 离开（不再来问）这么久才断开，免得切后台一下就重连
LDM_HEARTBEAT_SEC = 30
LDM_BACKOFF = (5, 15, 45, 120, 300)   # 连不上 / 被断：第 N 次失败后等这么久再连
LDM_MAX_FAILS = 6             # 同一间房连续失败这么多次就不连了（换房或服务重启才再试）
LDM_TOKEN_TTL = 600           # 入场券缓存十分钟（同一间房切后台回来不重问）
LDM_GIVEUP_SEC = 1800         # 放弃了的那间房，半小时后 TA 还开着才再试
LDM_OP_HEARTBEAT, LDM_OP_HEARTBEAT_REPLY, LDM_OP_CMD, LDM_OP_AUTH, LDM_OP_AUTH_REPLY = 2, 3, 5, 7, 8
LDM_HOST_RE = re.compile(r"^[0-9a-z.\-]+\.bilibili\.com$")

_ldm_lock = threading.Lock()
_ldm_ring = []                # [(seq, 收到的时刻, 文字, mode, color)]
_ldm_seq = 0
_ldm_state = {"room": None, "status": "idle", "since": 0.0, "fails": 0, "gaveUp": None, "lastSeen": 0.0}
_ldm_token = {}               # {room: (问到的时刻, info)}


def ldm_norm(text):
    """归一化成「同一类」的键：全角半角统一、大小写统一、去空白；有字就把标点符号扔掉，
    再把连着重复的字（哈哈哈哈→哈）和连着重复的两到四字小段（笑死笑死→笑死）缩成一份。
    全是标点的（？？？ / ???）就在标点上做同样的缩。"""
    import unicodedata
    s = unicodedata.normalize("NFKC", str(text or "")).lower()
    s = re.sub(r"\s+", "", s)
    body = "".join(ch for ch in s if not unicodedata.category(ch).startswith(("P", "S")))
    if body:
        s = body
    s = re.sub(r"(.)\1+", r"\1", s)
    s = re.sub(r"(.{2,4}?)\1+", r"\1", s)
    return s


def ldm_summary(texts, top=LDM_SUM_TOP, min_n=LDM_SUM_MIN, limit=LDM_SUM_CHARS, item=LDM_SUM_ITEM):
    """一串公开弹幕 → 「这会儿弹幕：气死了×87 · 毁灭吧×40」；没有出现两次以上的就回空串。
    每类显示出现最多的那个原样（一样多取短的），单句截到 item 个字。"""
    groups = {}
    for t in texts:
        t = str(t or "").strip().replace("\n", " ")
        if not t:
            continue
        k = ldm_norm(t)
        if not k:
            continue
        g = groups.setdefault(k, {"n": 0, "forms": {}})
        g["n"] += 1
        g["forms"][t] = g["forms"].get(t, 0) + 1
    ranked = sorted((g for g in groups.values() if g["n"] >= min_n), key=lambda g: -g["n"])[:top]
    head, out = "这会儿弹幕：", []
    for g in ranked:
        form = sorted(g["forms"].items(), key=lambda kv: (-kv[1], len(kv[0]), kv[0]))[0][0]
        if len(form) > item:
            form = form[:item - 1] + "…"
        piece = f"{form}×{g['n']}"
        line = head + " · ".join(out + [piece])
        if len(line) > limit:
            break
        out.append(piece)
    return head + " · ".join(out) if out else ""


def ldm_texts(room, since_sec, until=None):
    """这间房 since_sec 秒前到 until（默认现在）的公开弹幕原文。连的不是这间房就回空。"""
    until = time.time() if until is None else until
    with _ldm_lock:
        if _ldm_state.get("room") != int(room or 0):
            return []
        return [r[2] for r in _ldm_ring if until - since_sec <= r[1] <= until + 0.5]


def ldm_summary_line(room, until=None):
    try:
        return ldm_summary(ldm_texts(room, LDM_SUM_WIN, until))
    except Exception as e:
        log("公开弹幕摘要出错", type(e).__name__)
        return ""


_ldm_sum_at = {}              # {(房间, TA 那条弹幕的 id): 当时那行摘要}：look 取 TA 发弹幕那一刻的三帧时配同一行（只在内存里）


def ldm_sum_keep(room, lid, line):
    with _ldm_lock:
        _ldm_sum_at[(int(room), str(lid))] = line or ""
        while len(_ldm_sum_at) > 20:
            _ldm_sum_at.pop(next(iter(_ldm_sum_at)))


def ldm_sum_for(room, lid=None):
    """给 look 用：给 lid 就是 TA 发那条弹幕时的那行；不给就是此刻前 20 秒的。没有回空串。"""
    if lid:
        with _ldm_lock:
            return _ldm_sum_at.get((int(room), str(lid)), "")
    return ldm_summary_line(room)


def ldm_since(room, since):
    """页面每两秒来取时捎回去的那几条：since 之后、不太旧的，最多 LDM_POLL_MAX 条（多了留最新的）。"""
    now = time.time()
    with _ldm_lock:
        if _ldm_state.get("room") != int(room or 0):
            return {"last": _ldm_seq, "list": [], "on": False}
        rows = [r for r in _ldm_ring if r[0] > since and now - r[1] <= LDM_POLL_FRESH]
        last = _ldm_seq
        on = _ldm_state.get("status") == "open"
    rows = rows[-LDM_POLL_MAX:]
    return {"last": last, "on": on,
            "list": [{"text": r[2], "mode": r[3], "color": r[4], "ago": int((now - r[1]) * 1000)} for r in rows]}


def _ldm_put(text, mode, color):
    global _ldm_seq
    now = time.time()
    with _ldm_lock:
        _ldm_seq += 1
        _ldm_ring.append((_ldm_seq, now, text, mode, color))
        cut = now - LDM_RING_SEC
        if len(_ldm_ring) > LDM_RING_MAX or (_ldm_ring and _ldm_ring[0][1] < cut):
            keep = [r for r in _ldm_ring[-LDM_RING_MAX:] if r[1] >= cut]
            _ldm_ring[:] = keep


def ldm_parse_danmu(obj):
    """一条 DANMU_MSG → (文字, mode, color) 或 None。只要普通文字弹幕：表情包弹幕、抽奖/节奏风暴自动发的不要。"""
    try:
        if not str(obj.get("cmd") or "").startswith("DANMU_MSG"):
            return None
        info = obj.get("info") or []
        meta = info[0] if info and isinstance(info[0], list) else []
        text = str(info[1] if len(info) > 1 else "").strip().replace("\n", " ").replace("\r", " ")
        if not text:
            return None
        if len(meta) > 12 and meta[12] not in (0, None, "0"):       # 表情包弹幕
            return None
        if len(meta) > 9 and meta[9] in (1, 2):                      # 节奏风暴 / 天选时刻自动弹幕
            return None
        mode = int(meta[1]) if len(meta) > 1 and str(meta[1]).isdigit() else 1
        mode = mode if mode in (1, 4, 5) else 1
        color = int(meta[3]) if len(meta) > 3 and str(meta[3]).isdigit() else 0xffffff
        return text[:LDM_TEXT_MAX], mode, color & 0xffffff
    except Exception:
        return None


def ldm_packets(buf):
    """拆 B 站弹幕包：16 字节头（总长 u32、头长 u16、版本 u16、操作 u32、序号 u32）＋正文。
    版本 2 的正文是 zlib 压缩的一摞包，解开再拆。返回 [(操作, 版本, 正文字节)]。"""
    import struct
    out, i = [], 0
    while i + 16 <= len(buf):
        plen, hlen, ver, op, _seq = struct.unpack(">IHHII", buf[i:i + 16])
        if plen < hlen or hlen < 16 or i + plen > len(buf):
            break
        body = buf[i + hlen:i + plen]
        i += plen
        if op == LDM_OP_CMD and ver == 2:
            try:
                out.extend(ldm_packets(zlib.decompress(body)))
            except zlib.error:
                pass
            continue
        out.append((op, ver, body))
    return out


def ldm_pack(op, body=b""):
    import struct
    if isinstance(body, str):
        body = body.encode("utf-8")
    return struct.pack(">IHHII", 16 + len(body), 16, 1, op, 1) + body


class _MiniWS:
    """够用就行的 WebSocket 客户端（wss、二进制帧、掩码、ping/pong、分片），只给弹幕长连接用。"""

    def __init__(self, host, port, path="/sub", timeout=10):
        import socket, ssl, os as _os
        raw = socket.create_connection((host, port), timeout=timeout)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        key = base64.b64encode(_os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
               f"Origin: https://live.bilibili.com\r\nUser-Agent: {STREAM_UA}\r\n\r\n")
        self.sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("握手时被断开")
            head += chunk
            if len(head) > 16384:
                raise ConnectionError("握手回得太长")
        head, self.buf = head.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        if " 101" not in status:
            raise ConnectionError("握手没成：" + status[:40])
        self.frag = b""

    def send(self, data, opcode=2):
        import os as _os, struct
        mask = _os.urandom(4)
        n = len(data)
        hdr = bytes([0x80 | opcode])
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(hdr + mask + body)

    def recv(self, wait=1.0):
        """等最多 wait 秒，回一条完整的消息（bytes）；没等到回 None；对面关了抛 ConnectionError。"""
        import socket, struct
        self.sock.settimeout(wait)
        while True:
            msg = self._take()
            if msg is not None:
                return msg
            try:
                chunk = self.sock.recv(65536)
            except (socket.timeout, TimeoutError):
                return None
            if not chunk:
                raise ConnectionError("对面关了连接")
            self.buf += chunk
            if len(self.buf) > 8 * 1024 * 1024:
                raise ConnectionError("一条消息太大")

    def _take(self):
        import struct
        b = self.buf
        while len(b) >= 2:
            fin, op, ln, i = b[0] & 0x80, b[0] & 0x0F, b[1] & 0x7F, 2
            masked = b[1] & 0x80
            if ln == 126:
                if len(b) < 4:
                    return None
                ln, i = struct.unpack(">H", b[2:4])[0], 4
            elif ln == 127:
                if len(b) < 10:
                    return None
                ln, i = struct.unpack(">Q", b[2:10])[0], 10
            if masked:
                i += 4
            if len(b) < i + ln:
                return None
            payload = b[i:i + ln]
            if masked:
                m = b[i - 4:i]
                payload = bytes(x ^ m[j % 4] for j, x in enumerate(payload))
            b = self.buf = b[i + ln:]
            if op == 0x8:
                raise ConnectionError("对面发了关闭帧")
            if op == 0x9:
                self.send(payload, opcode=0xA)
                continue
            if op == 0xA:
                continue
            self.frag += payload
            if fin:
                msg, self.frag = self.frag, b""
                return msg
        return None

    def close(self):
        try:
            self.send(b"", opcode=0x8)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


def _ldm_ticket(room):
    """问 bili_mcp 要入场券（缓存十分钟）。拿不到抛 RuntimeError。"""
    with _ldm_lock:
        c = _ldm_token.get(room)
    if c and time.time() - c[0] < LDM_TOKEN_TTL:
        return c[1]
    info = bili_get(f"/live_danmu?room={int(room)}", timeout=15)
    if not info.get("token") or not info.get("hosts"):
        raise RuntimeError("没拿到弹幕服务器")
    with _ldm_lock:
        _ldm_token.clear()
        _ldm_token[room] = (time.time(), info)
    return info


def _ldm_session(room, stop):
    """连一次：入场 → 收包 → 定时心跳，直到 stop 被设、出错或对面断开。收到过弹幕服务器的回应算连上了。"""
    info = _ldm_ticket(room)
    hosts = [h for h in info["hosts"] if LDM_HOST_RE.match(str(h.get("host") or ""))] or []
    if not hosts:
        raise RuntimeError("弹幕服务器主机不认识")
    h = hosts[_ldm_state.get("fails", 0) % len(hosts)]
    ws = _MiniWS(h["host"], int(h["wss_port"]))
    ok = False
    try:
        auth = {"uid": 0, "roomid": int(room), "protover": 2, "platform": "web", "type": 2,
                "key": info["token"]}
        if info.get("buvid"):
            auth["buvid"] = info["buvid"]
        ws.send(ldm_pack(LDM_OP_AUTH, json.dumps(auth)))
        last_hb = 0.0
        t0 = time.time()
        while not stop.is_set():
            if time.time() - last_hb >= LDM_HEARTBEAT_SEC:
                ws.send(ldm_pack(LDM_OP_HEARTBEAT, b""))
                last_hb = time.time()
            msg = ws.recv(1.0)
            if msg is None:
                if not ok and time.time() - t0 > 15:
                    raise RuntimeError("入场没回音")
                continue
            for op, ver, body in ldm_packets(msg):
                if op == LDM_OP_AUTH_REPLY:
                    try:
                        code = json.loads(body.decode("utf-8", "replace") or "{}").get("code")
                    except Exception:
                        code = None
                    if code not in (0, None):
                        raise RuntimeError(f"入场被拒（{code}）")
                    ok = True
                    with _ldm_lock:
                        _ldm_state.update(status="open", fails=0)
                elif op == LDM_OP_CMD and ver == 0:
                    try:
                        obj = json.loads(body.decode("utf-8", "replace"))
                    except Exception:
                        continue
                    d = ldm_parse_danmu(obj) if isinstance(obj, dict) else None
                    if d:
                        _ldm_put(*d)
        return ok
    finally:
        ws.close()


def _ldm_worker(room, stop):
    """一间房的连接线程：断了按退避重连，连续失败到上限就安静地放弃。"""
    while not stop.is_set():
        try:
            _ldm_session(room, stop)
            if stop.is_set():
                break
            why = "对面断开了"
        except Exception as e:
            why = f"{type(e).__name__}: {str(e)[:60]}"
        with _ldm_lock:
            if _ldm_state.get("room") != room:
                break
            _ldm_state["fails"] = n = _ldm_state.get("fails", 0) + 1
            _ldm_state["status"] = "retry"
            if n >= LDM_MAX_FAILS:
                _ldm_state.update(status="gave-up", gaveUp=(room, time.time()))
        log("直播公开弹幕断了", room, scrub(why), f"第 {n} 次")
        if n >= LDM_MAX_FAILS:
            log("直播公开弹幕：连续失败到上限，这间房不连了", room)
            return
        if stop.wait(LDM_BACKOFF[min(n, len(LDM_BACKOFF)) - 1]):
            break


def ldm_manager():
    """总管：TA 此刻开着哪间就只连那一间；走开 LDM_LINGER_SEC 秒就断；换房先断再连。"""
    cur_room, stop, th = None, None, None
    while True:
        time.sleep(1.0)
        try:
            want = live_watch_room()
            now = time.time()
            if want:
                with _ldm_lock:
                    _ldm_state["lastSeen"] = now
            else:
                with _ldm_lock:
                    seen = _ldm_state.get("lastSeen") or 0
                if cur_room and now - seen < LDM_LINGER_SEC:
                    want = cur_room
            if want == cur_room:
                continue
            if stop:
                stop.set()
            if th:
                th.join(timeout=5)
            cur_room, stop, th = want, None, None
            with _ldm_lock:
                g = _ldm_state.get("gaveUp")
                gave = g[0] if g and now - g[1] < LDM_GIVEUP_SEC else None
                _ldm_ring.clear()
                _ldm_state.update(room=want, status="idle" if not want else "connecting",
                                  since=now, fails=0 if want != gave else _ldm_state.get("fails", 0))
            if want and want != gave:
                stop = threading.Event()
                th = threading.Thread(target=_ldm_worker, args=(want, stop), daemon=True)
                th.start()
            elif want:
                with _ldm_lock:
                    _ldm_state["status"] = "gave-up"
        except Exception as e:
            log("直播公开弹幕总管出错", type(e).__name__, e)


def _live_fetch(url, timeout=12, rng=None):
    headers = {"User-Agent": STREAM_UA, "Referer": LIVE_REFERER, "Accept": "*/*", "Accept-Encoding": "identity"}
    if rng:
        headers["Range"] = rng
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout)


def live_manifest(room, qn, kind):
    """代理那一路：拿清单、把每一条分片/初始化段改写成 seg?u=…（相对地址，同目录）。
    清单地址过期（403/404）或连不上就重问一次 getRoomPlayInfo。"""
    key = (int(room), int(qn), kind)
    last = None
    for attempt in (0, 1):
        with _lock:
            chosen = _live_pick.get(key)
        cands = []
        if chosen and attempt == 0:
            cands.append(chosen)
        info = live_play(room, qn, fresh=attempt == 1)
        for s in info["streams"]:
            if s["kind"] == kind:
                cands += [u for u in s["urls"] if u not in cands]
        if not cands and info["streams"]:
            cands = list(info["streams"][0]["urls"])
        for u in cands:
            try:
                with _live_fetch(u) as r:
                    body = r.read(2 * 1024 * 1024).decode("utf-8", "replace")
                    final = r.geturl() or u
                if "#EXTM3U" not in body[:64]:
                    last = "清单不是 m3u8"
                    continue
                with _lock:
                    _live_pick[key] = u
                return rewrite_m3u8(body, final)
            except urllib.error.HTTPError as e:
                last = f"CDN 回了 {e.code}"
            except Exception as e:
                last = f"CDN 连不上：{type(e).__name__}"
        with _lock:
            _live_pick.pop(key, None)
    raise RuntimeError(last or "清单没拿到")


def _seg_ref(base, ref):
    from urllib.parse import urljoin
    return "seg?u=" + quote(urljoin(base, ref), safe="")


def rewrite_m3u8(body, base):
    out = []
    for line in body.splitlines():
        s = line.strip()
        if not s:
            out.append(line)
            continue
        if s.startswith("#"):
            if 'URI="' in s:
                s = re.sub(r'URI="([^"]+)"', lambda m: 'URI="' + _seg_ref(base, m.group(1)) + '"', s)
            out.append(s)
            continue
        out.append(_seg_ref(base, s))
    return "\n".join(out) + "\n"


BVID_RE = re.compile(r"^(?:BV[0-9A-Za-z]{10}|av\d{1,12}|ss\d{1,12})$")     # ss＝番剧整部的封面
COVER_HOST_RE = re.compile(r"^https://[0-9a-z.\-]*hdslb\.com/", re.I)


def bili_face_path(mid, url, make=True):
    """UP 主头像：同一套缓存（.posters/bili_up_<mid>.jpg），也只认 hdslb.com，不热链。"""
    mid = str(mid or "").strip()
    if not mid.isdigit() or len(mid) > 16:
        return None
    # 头像裁成 240×240 的方图（页面放进圆框）；按封面的 640×360 裁，圆框里只剩一角
    return bili_cover_path({"bvid": "up_" + mid, "cover": url, "id": "bili:up_" + mid}, make=make,
                           crop="@240w_240h_1c.jpg")


def bili_cover_any(bvid, url, make=True):
    """逛片页列表里那些还没进片单的视频：同一套 .posters/bili_<bvid>.jpg 缓存。
    只认 hdslb.com 的图，bvid 也过一层正则，免得这条路被拿去当任意抓取器。"""
    bvid = str(bvid or "").strip()
    if not BVID_RE.match(bvid):
        return None
    return bili_cover_path({"bvid": bvid, "cover": url, "id": "bili:" + bvid}, make=make)


def bili_cover_path(film, make=True, crop="@640w_360h_1c.jpg"):
    """封面代理：拉一次存进 .posters/bili_<bvid>.jpg，之后直接返回。"""
    os.makedirs(POSTER_DIR, exist_ok=True)
    p = os.path.join(POSTER_DIR, f"bili_{film.get('bvid')}.jpg")
    if os.path.exists(p) and os.path.getsize(p) > 0:
        return p
    if not make:
        return None
    url = str(film.get("cover") or "")
    if not url:
        return None
    url = url.replace("http://", "https://", 1)
    if not COVER_HOST_RE.match(url):
        return None
    cands = [url] if "@" in url else [url + crop, url]
    for cand in cands:
        try:
            req = urllib.request.Request(cand, headers={"User-Agent": STREAM_UA, "Referer": STREAM_REFERER})
            with urllib.request.urlopen(req, timeout=20) as r:
                if not (r.headers.get("Content-Type") or "").startswith("image"):
                    continue
                data = r.read(8 * 1024 * 1024)
            if data:
                with open(p + ".tmp", "wb") as f:
                    f.write(data)
                os.replace(p + ".tmp", p)
                return p
        except Exception as e:
            log("封面没拿到", film["id"], e)
    return None


SYMBOL_ONLY = re.compile(r"^[\W_]+$", re.U)


def bili_danmaku(film):
    """整部片的弹幕 → 页面用的 [{t,mode,color,text}]，缓存 dm/<bvid>_<cid>.json。
    只留滚动 / 顶部 / 底部三种、40 字以内、不全是符号的；超过 DANMAKU_MAX 条就均匀抽样。"""
    os.makedirs(DM_DIR, exist_ok=True)
    p = os.path.join(DM_DIR, f"{film.get('bvid')}_{film.get('cid')}.json")
    if os.path.exists(p):
        return load_json(p, [])
    if pgc_blocked(film):
        return []
    dur = film.get("duration") or 0
    d = bili_get(f"/danmaku_all?cid={int(film.get('cid'))}" + (f"&duration={int(dur)}" if dur else ""), timeout=120)
    out = []
    for x in d.get("list") or []:
        text = str(x.get("text") or "").strip()
        mode = int(x.get("mode") or 1)
        if mode not in (1, 4, 5) or not text or len(text) > 40 or SYMBOL_ONLY.match(text):
            continue
        out.append({"t": round(float(x.get("t") or 0), 2), "mode": mode,
                    "color": int(x.get("color") or 16777215), "text": text})
    out.sort(key=lambda x: x["t"])
    if len(out) > DANMAKU_MAX:
        step = len(out) / float(DANMAKU_MAX)
        out = [out[int(i * step)] for i in range(DANMAKU_MAX)]
    if out:
        save_json(p, out)
    return out


def bili_comments(film):
    """热评前两页 → 页面用的 [{user,text,like,time,replies:[{user,text,like}]}]，缓存 cm/<bvid>.json。"""
    os.makedirs(CM_DIR, exist_ok=True)
    p = os.path.join(CM_DIR, f"{film.get('bvid')}.json")
    if os.path.exists(p):
        return load_json(p, [])
    if pgc_blocked(film):
        return []
    who = f"av{int(film['aid'])}" if is_pgc(film) and film.get("aid") else str(film.get("bvid"))
    d = bili_get(f"/comments?bvid={quote(who)}&pages=2", timeout=60)
    out = []
    for c in d.get("list") or []:
        msg = str(c.get("message") or "").strip()
        if not msg:
            continue
        out.append({"user": str(c.get("uname") or "匿名")[:30], "text": msg[:600],
                    "like": int(c.get("like") or 0), "time": c.get("ctime") or "",
                    "replies": [{"user": str(r.get("uname") or "匿名")[:30],
                                 "text": str(r.get("message") or "").strip()[:300],
                                 "like": int(r.get("like") or 0)}
                                for r in (c.get("sub") or [])[:3] if str(r.get("message") or "").strip()]})
        if len(out) >= COMMENT_MAX:
            break
    if out:
        save_json(p, out)
    return out


def bili_film_entry(b):
    fid = b["id"]
    q = quote(fid)
    return {
        "id": fid, "title": b.get("title") or fid, "file": None, "kind": "bili",
        "bvid": b.get("bvid"), "cid": b.get("cid"), "p": b.get("p") or 1, "up": b.get("up") or "",
        "pages": b.get("pages") or 0,
        "duration": b.get("duration"), "hasSub": False, "sub": None,
        "poster": f"{URL_PREFIX}/api/bili/cover?id={q}",
        "src": f"{URL_PREFIX}/api/bili/stream?id={q}",
        "vtt": None, "playable": True, "notes": len(notes_of(fid)), "size": 0,
        "hasDanmaku": True, "hasComments": True,
        "danmaku": f"{URL_PREFIX}/api/danmaku?film={q}", "comments": f"{URL_PREFIX}/api/comments?film={q}",
        "addedAt": b.get("added_at"),
        **({"pgc": b["pgc"], "epTitle": b.get("epTitle") or "", "aid": b.get("aid")} if b.get("pgc") else {}),
        **({"parked": b["parked"]} if b.get("parked") else {}),
    }


# ── 删：片子 / 旁挂文件 / 观看记录 / 批注（批注先备份进 trash/）──

FRAME_RE_TPL = r"^{stem}-\d+-\d+\.jpg$"


def _safe_name(n):
    """只认片源目录里的裸文件名：带路径分隔符、点开头的一律拒绝。"""
    n = str(n or "")
    return bool(n) and "/" not in n and "\\" not in n and not n.startswith(".") and n not in (".", "..")


def _rm(path, removed):
    try:
        if os.path.isfile(path):
            os.remove(path)
            removed.append(os.path.basename(path))
    except Exception as e:
        log("删不掉", path, e)


def trash_notes(fid):
    """把这部片子的批注备份到 trash/<id>-<ts>.json，再从 notes.json 里拿掉。返回备份了几条。"""
    with _lock:
        ns = list(_notes.get(fid) or [])
        if not ns:
            _notes.pop(fid, None)
            return 0
        os.makedirs(TRASH_DIR, exist_ok=True)
        safe = re.sub(r"[^\w.\-]+", "_", str(fid))[:80]
        save_json(os.path.join(TRASH_DIR, f"{safe}-{int(time.time())}.json"), {"film": fid, "notes": ns})
        _notes.pop(fid, None)
        save_json(NOTES_FILE, _notes)
    return len(ns)


def clear_watch(fid=None):
    """清「TA 看到哪」：now.json 里若正是这部（或不指定）就清空；AI 看画面的记号也一起清掉。"""
    global _peek
    cleared = False
    with _lock:
        if _state.get("film") and (fid is None or _state.get("film") == fid):
            _state.clear()
            save_json(NOW_FILE, {})
            cleared = True
        if _peek and (fid is None or _peek.get("film") == fid):
            _peek = None
    return cleared


def remove_film(fid):
    film = resolve(fid)
    if not film:
        return None, "没有这部片子"
    removed = []
    if is_bili(film):
        with _lock:
            _bili[:] = [b for b in _bili if b.get("id") != fid]
            save_bili()
            _playurl.pop(fid, None)
        _rm(os.path.join(DM_DIR, f"{film.get('bvid')}_{film.get('cid')}.json"), removed)
        _rm(os.path.join(CM_DIR, f"{film.get('bvid')}.json"), removed)
        # 封面只有别的 P 都不在了才删
        if not any(b.get("bvid") == film.get("bvid") for b in _bili):
            _rm(os.path.join(POSTER_DIR, f"bili_{film.get('bvid')}.jpg"), removed)
        stem = cache_stem(film)
    else:
        name, stem = film["name"], film["id"]
        if not _safe_name(name) or not _safe_name(stem):
            return None, "这个文件名不对劲，不删"
        _rm(os.path.join(FILM_DIR, name), removed)
        try:
            names = os.listdir(FILM_DIR)
        except Exception:
            names = []
        for n in names:
            if not _safe_name(n) or n == name:
                continue
            base, ext = os.path.splitext(n)
            low = n.lower()
            # 字幕：<片名>.srt / <片名>.zh.srt；旁挂：<片名>.danmaku.json / .comments.json
            if ((base == stem or n.startswith(stem + ".")) and low.endswith(SUB_EXT)) or \
                    n in (stem + ".danmaku.json", stem + ".comments.json"):
                _rm(os.path.join(FILM_DIR, n), removed)
        _rm(os.path.join(POSTER_DIR, stem + ".jpg"), removed)
        with _lock:
            for k in [k for k in _meta if k.startswith(name + "|")]:
                _meta.pop(k, None)
            save_json(META_FILE, _meta)
    # 抓帧缓存
    try:
        pat = re.compile(FRAME_RE_TPL.format(stem=re.escape(stem)))
        for n in os.listdir(FRAME_DIR):
            if pat.match(n):
                _rm(os.path.join(FRAME_DIR, n), removed)
    except Exception:
        pass
    n_notes = trash_notes(fid)
    watched = clear_watch(fid)
    log("删片", fid, "文件", len(removed), "批注", n_notes, "观看记录", watched)
    return {"ok": True, "id": fid, "removed": removed, "notesTrashed": n_notes, "watchCleared": watched}, None


# ── 本地片子的弹幕与评论区：片源目录里同名的 <片名>.danmaku.json / <片名>.comments.json ──

def side_json(film, suffix):
    p = os.path.join(FILM_DIR, film["id"] + suffix)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def has_side(fid, suffix):
    return os.path.exists(os.path.join(FILM_DIR, fid + suffix))


# 扫码登录的二维码图片由 MCP 那头写进这个目录（BILITUBE_IMAGE_DIR），这里读出来交给页面。
UPLOADS_DIR = os.environ.get("BILITUBE_IMAGE_DIR") or os.path.join(_REPO_ROOT, "data", "images")


# ── 设置：两个人的称呼 / 自定义头像 / 自定义皮肤底图 / B 站扫码登录转发 ──
# 都在反向代理的 /bilitube/api/* 门禁后面：
#   GET  /api/prefs                  → {ok, companionName, userName, companionBio, avatars:{user,companion}, skinBg}
#                                      名字没改过是 null（页面自己退回 config 里的默认 / B 站名）；图没传过是 null
#   POST /api/prefs  {companionName?, userName?, companionBio?}   只收这三个键，空串＝改回默认
#   GET  /api/avatar/user|companion、GET /api/skin/bg            原样返回图片（没传过 404）
#   POST 同上三条：请求体是图片原始字节（jpg/png/webp/gif，认文件头不认 Content-Type）；空请求体＝删掉
#     examples/Caddyfile 给 /bilitube/api/* 的请求体封顶 256KB，所以头像 240KB、底图 250KB 封顶，页面先缩好再传。
#   B 站扫码：bili_mcp 的回环 REST 没有扫码接口，这里转发它的 MCP 工具（login：action=qr / status），
#     不碰凭据、不存任何凭据：二维码 png 读出来当 data: URL 给页面，状态从 login status 的返回文字里解析。
PREFS_FILE = os.path.join(DATA_DIR, "prefs.json")
PREF_KEYS = {"companionName": 24, "userName": 24, "companionBio": 60}
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
UPLOAD_SLOTS = {"/api/avatar/user": ("avatar-user", 240 * 1024),
                "/api/avatar/companion": ("avatar-companion", 240 * 1024),
                "/api/skin/bg": ("skin-bg", 250 * 1024)}


def _img_mime(raw):
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def _slot_paths(slot):
    return os.path.join(UPLOAD_DIR, slot + ".img"), os.path.join(UPLOAD_DIR, slot + ".json")


def _slot_meta(slot):
    img, meta = _slot_paths(slot)
    if not os.path.exists(img):
        return None
    return load_json(meta, None) or {"mime": "image/jpeg", "at": int(os.path.getmtime(img))}


_names_cache = {"t": 0.0, "d": None}


def names():
    """两个人此刻的称呼 {"user", "companion"}：读 prefs.json（缓存 5 秒），没填就用 DEFAULT_*。"""
    now = time.time()
    if _names_cache["d"] is None or now - _names_cache["t"] > 5:
        p = load_json(PREFS_FILE, {}) or {}
        _names_cache["d"] = {"user": str(p.get("userName") or "").strip() or DEFAULT_USER_NAME,
                             "companion": str(p.get("companionName") or "").strip() or DEFAULT_COMPANION_NAME}
        _names_cache["t"] = now
    return _names_cache["d"]


def user_name():
    return names()["user"]


def companion_name():
    return names()["companion"]


def prefs_get():
    p = load_json(PREFS_FILE, {}) or {}
    out = {"ok": True}
    for k in PREF_KEYS:
        out[k] = str(p.get(k) or "").strip() or None
    out["names"] = dict(names())          # 填了用填的、没填用默认，给 MCP 工具直接用
    av = {}
    for who in ("user", "companion"):
        m = _slot_meta("avatar-" + who)
        av[who] = f"{URL_PREFIX}/api/avatar/{who}?v={m.get('at', 0)}" if m else None
    out["avatars"] = av
    m = _slot_meta("skin-bg")
    out["skinBg"] = f"{URL_PREFIX}/api/skin/bg?v={m.get('at', 0)}" if m else None
    return out


def prefs_set(body):
    with _lock:
        p = load_json(PREFS_FILE, {}) or {}
        for k, n in PREF_KEYS.items():
            if k in body:
                v = re.sub(r"[\x00-\x1f<>]", "", str(body.get(k) or "")).strip()[:n]
                if v:
                    p[k] = v
                else:
                    p.pop(k, None)
        save_json(PREFS_FILE, p)
        _names_cache["d"] = None
    return prefs_get()


_login_cache = {"t": 0, "d": None}


def bili_mcp_tool(name, args, timeout=30):
    """调 bili_mcp 的一个 MCP 工具，回 (文字, 是不是报错)。只给扫码登录用。"""
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args or {}}}
    req = urllib.request.Request(BILI_MCP + "/mcp", data=json.dumps(msg).encode(),
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8", "replace") or "{}")
    res = d.get("result") or {}
    text = "".join(c.get("text", "") for c in (res.get("content") or []) if isinstance(c, dict))
    if d.get("error"):
        return str((d["error"] or {}).get("message") or d["error"]), True
    return text, bool(res.get("isError"))


def bili_login_qr():
    text, bad = bili_mcp_tool("login", {"action": "qr", "force": True}, timeout=40)
    if bad:
        return {"ok": False, "error": text[:200]}
    m = re.search(r"(att-bili-qr-\d{6,14}\.png)", text)
    if not m:
        return {"ok": False, "error": text[:200] or "bili_mcp 没给二维码"}
    p = os.path.join(UPLOADS_DIR, m.group(1))
    try:
        with open(p, "rb") as f:
            raw = f.read(512 * 1024)
    except Exception:
        return {"ok": False, "error": "二维码图没找到"}
    _login_cache["t"] = 0
    return {"ok": True, "img": "data:image/png;base64," + base64.b64encode(raw).decode()}


def bili_login_state():
    """扫码那几分钟页面每 2 秒问一次；这里 4 秒内复用上一份（登录着时 login status 会去问 B 站一下 nav）。"""
    now = time.time()
    if _login_cache["d"] and now - _login_cache["t"] < 4:
        return _login_cache["d"]
    text, bad = bili_mcp_tool("login", {"action": "status"})
    if bad:
        return {"ok": False, "error": text[:200]}
    m = re.search(r"登录：(.+?)（mid", text)
    pg = re.search(r"扫码进度：(\w+)", text)
    d = {"ok": True, "logged_in": bool(m) and "未登录" not in text, "uname": m.group(1) if m else "",
         "progress": pg.group(1) if pg else None, "paused": "暂停中" in text}
    _login_cache.update(t=now, d=d)
    return d


def bili_logout():
    """退出 B 站登录（用户在设置里点了「退出」、又在确认那一步点了才来）。
    凭据在 bili_mcp 那头：叫它的回环 POST /logout 清本地（B 站那头它尽力作废，成没成照实回 remote / note）；
    这边把按账号记的缓存一起丢掉，免得退出后还露上一个号的东西：点赞收藏稍后再看的状态、番剧「上次看到」、
    番剧能挑的清晰度档、扫码状态、用户自己的头像图。列表（推荐、关注、收藏、稍后再看、历史）这边本来就不缓存，现问。
    视频地址缓存（_playurl / _direct_pu）不动：那只是 CDN 地址、不显示什么，到期自己换，正在播的片子不被打断。"""
    try:
        me = bili_get("/me", timeout=8)
    except Exception:
        me = {}
    try:
        d = bili_post("/logout", {}, timeout=20)
    except Exception as e:
        return {"ok": False, "error": f"没退成：{e}"}
    with _lock:
        _state_cache.clear()
        _season_cache.clear()
        _pgc_accept.clear()
    _login_cache.update(t=0, d=None)
    if me.get("mid"):
        p = bili_face_path(str(me["mid"]), "", make=False)
        if p:
            try:
                os.remove(p)
            except OSError:
                pass
    log("B 站退出登录：本地已清，B 站那头", d.get("remote"), d.get("note") or "")
    return {"ok": True, "remote": d.get("remote") or "unconfirmed", "note": d.get("note") or ""}


# ── HTTP ──

class Handler(BaseHTTPRequestHandler):
    server_version = "bilitube/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        gz = (len(body) > 1400 and ctype.startswith("application/json")
              and "gzip" in (self.headers.get("Accept-Encoding") or "").lower() and GZIP_JSON)
        if gz:
            body = gzip.compress(body, 5)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if gz:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _err(self, code, msg):
        self._send(code, {"error": msg})

    def _film_arg(self, q, fallback_now=True):
        fid = (q.get("film", [""])[0] or "").strip()
        if not fid and fallback_now:
            fid = (state_snapshot().get("film") or "")
        return resolve(fid)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        path = u.path.rstrip("/") or "/"
        try:
            if path == "/health":
                return self._send(200, {"ok": True, "films": len(list_films_raw()), "bili": len(_bili)})
            if path == "/api/films":
                snap = state_snapshot()
                now = ({"film": snap["film"], "t": snap.get("t"), "at": snap.get("at"),
                        "playing": snap.get("playing")} if snap.get("film") else None)
                fl = self.films()
                un = upnext_get()
                if un:                                # 插播：排在下一个的那部带个标记
                    for x in fl:
                        if x.get("id") == un["film"]:
                            x["upNext"] = True
                return self._send(200, {"films": fl, "now": now, "upNext": un})
            if path == "/api/bili/stream":
                f = resolve((q.get("id", [""])[0] or "").strip())
                if not f or not is_bili(f):
                    return self._err(404, "没有这部片子")
                return self._stream(f)
            if path == "/api/bili/cover":
                f = resolve((q.get("id", [""])[0] or "").strip())
                if not f or not is_bili(f):
                    return self._err(404, "没有这部片子")
                p = bili_cover_path(f)
                if not p:
                    return self._err(404, "封面没拿到")
                return self._file(p, "image/jpeg", IMG_CACHE)
            # 直连：只发地址，不发字节（fresh=1 = 页面发现地址过期了，现问一份新的）
            if path == "/api/bili/direct":
                f = resolve((q.get("id", [""])[0] or "").strip())
                if not f or not is_bili(f):
                    return self._err(404, "没有这部片子")
                fresh = (q.get("fresh", [""])[0] or "") == "1"
                try:
                    info = direct_info(f, fresh=fresh, qn=(q.get("qn", [""])[0] or "").strip() or None)
                except PgcDenied as e:
                    # 番剧这一集 B 站不给放（大会员 / 付费 / 地区 / DRM）：照实说，不给代理那条路（代理也一样拿不到）
                    return self._send(200, {"ok": False, "denied": True, "reason": e.reason, "error": str(e)})
                except Exception as e:
                    log("直连地址没拿到", f["id"], e)
                    return self._send(502, {"ok": False, "error": f"拿不到播放地址：{e}",
                                            "proxy": bili_film_entry(f)["src"]})
                if f.get("parked"):
                    bili_unpark(f["id"])         # 服务器这头取到地址了＝能放（不再挂着）
                return self._send(200, direct_reply(f, info))
            # 直播：查房间 / 取 HLS 地址（直连用）/ 代理清单 / 代理分片
            if path == "/api/live/room":
                try:
                    room = parse_live_ref(q.get("ref", [""])[0] or q.get("room", [""])[0])
                except ValueError as e:
                    return self._err(400, str(e))
                try:
                    return self._send(200, {"ok": True, **live_room(room)})
                except Exception as e:
                    return self._send(502, {"ok": False, "error": str(e)})
            if path == "/api/live/play":
                try:
                    room = int(q.get("room", ["0"])[0] or 0)
                    qn = int(q.get("qn", ["10000"])[0] or 10000)
                except ValueError:
                    return self._err(400, "房间号不对")
                if room <= 0:
                    return self._err(400, "房间号不对")
                try:
                    info = live_play(room, qn, fresh=(q.get("fresh", [""])[0] or "") == "1")
                except LookupError as e:
                    return self._send(409, {"ok": False, "offline": True, "error": str(e)})
                except Exception as e:
                    log("直播地址没拿到", room, e)
                    return self._send(502, {"ok": False, "error": str(e)})
                kinds = [s["kind"] for s in info["streams"]]
                with _lock:
                    _live_seen.update(room=int(info["room"]), at=time.time())   # TA 最近开的直播间
                return self._send(200, {"ok": True, **info, "now": int(time.time()),
                                        "proxy": {k: f"{URL_PREFIX}/api/live/m3u8?room={info['room']}&qn={qn}&kind={k}"
                                                  for k in kinds}})
            if path == "/api/live/m3u8":
                try:
                    room = int(q.get("room", ["0"])[0] or 0)
                    qn = int(q.get("qn", ["10000"])[0] or 10000)
                except ValueError:
                    return self._err(400, "房间号不对")
                kind = (q.get("kind", ["fmp4"])[0] or "fmp4")
                if room <= 0 or kind not in ("fmp4", "ts"):
                    return self._err(400, "参数不对")
                try:
                    text = live_manifest(room, qn, kind)
                except LookupError as e:
                    return self._send(409, {"ok": False, "offline": True, "error": str(e)})
                except Exception as e:
                    log("直播清单没拿到", room, e)
                    return self._err(502, str(e))
                return self._send(200, text.encode("utf-8"), ctype="application/vnd.apple.mpegurl")
            if path == "/api/live/seg":
                return self._live_seg((q.get("u", [""])[0] or "").strip())
            if path == "/api/live/pubsum":         # look 配的那行公开弹幕摘要（只读内存，不出门）
                rs = (q.get("room", [""])[0] or "").strip()
                lid = re.sub(r"[^0-9a-z]", "", (q.get("lid", [""])[0] or "").strip())[:20] or None
                if not rs.isdigit():
                    return self._err(400, "房间号不对")
                with _ldm_lock:
                    st = {"room": _ldm_state.get("room"), "status": _ldm_state.get("status")}
                return self._send(200, {"ok": True, "line": ldm_sum_for(int(rs), lid), "conn": st})
            # ── 逛片页的列表：搜索 / 收藏 / 历史 / 稍后再看 / 动态 / 直播 / 推荐 / 热门 / 字幕 / 收到的回复
            #    都只是把 bili_mcp 的回环 REST 转出来 ──
            if path == "/api/bili/search":
                kw = (q.get("q", [""])[0] or "").strip()[:80]
                if not kw:
                    return self._err(400, "要搜什么呢")
                n = max(1, min(20, int(float(q.get("n", ["20"])[0] or 20))))
                page = max(1, min(50, int(float(q.get("page", ["1"])[0] or 1))))
                return self._send(200, bili_get(f"/search?q={quote(kw)}&n={n}&page={page}"))
            if path == "/api/bili/folders":
                return self._send(200, bili_get("/fav_folders"))
            if path == "/api/bili/favlist":
                mid = (q.get("media_id", [""])[0] or "").strip()
                n = max(1, min(20, int(float(q.get("n", ["20"])[0] or 20))))
                page = max(1, min(50, int(float(q.get("page", ["1"])[0] or 1))))
                return self._send(200, bili_get(f"/fav_list?n={n}&page={page}"
                                                + (f"&media_id={int(mid)}" if mid else "")))
            if path == "/api/bili/history":
                n = max(1, min(20, int(float(q.get("n", ["20"])[0] or 20))))
                u = f"/history?n={n}"
                for k in ("max", "view_at", "business"):      # 历史是游标，不是页码
                    v = (q.get(k, [""])[0] or "").strip()
                    if v:
                        u += f"&{k}={quote(v)}"
                return self._send(200, bili_get(u))
            if path == "/api/bili/watchlater":
                n = max(1, min(30, int(float(q.get("n", ["20"])[0] or 20))))
                off = max(0, min(2000, int(float(q.get("offset", ["0"])[0] or 0))))
                return self._send(200, bili_get(f"/watchlater?n={n}&offset={off}"))
            if path == "/api/bili/dynamic":
                n = max(1, min(30, int(float(q.get("n", ["20"])[0] or 20))))
                mid = (q.get("host_mid", [""])[0] or "").strip()
                off = (q.get("offset", [""])[0] or "").strip()[:80]
                page = max(1, min(50, int(float(q.get("page", ["1"])[0] or 1))))
                u = f"/dynamic?n={n}&page={page}"
                if mid:
                    u += f"&host_mid={int(mid)}"
                if off:
                    u += f"&offset={quote(off)}"
                return self._send(200, bili_get(u))
            if path == "/api/bili/live":
                n = max(1, min(30, int(float(q.get("n", ["20"])[0] or 20))))
                return self._send(200, bili_get(f"/live_following?n={n}"))
            if path == "/api/bili/recommend":
                n = max(1, min(30, int(float(q.get("n", ["12"])[0] or 12))))
                fresh = max(1, min(999, int(float(q.get("fresh", ["1"])[0] or 1))))
                return self._send(200, bili_get(f"/recommend?n={n}&fresh={fresh}"))
            if path == "/api/bili/popular":
                n = max(1, min(20, int(float(q.get("n", ["20"])[0] or 20))))
                page = max(1, min(50, int(float(q.get("page", ["1"])[0] or 1))))
                return self._send(200, bili_get(f"/popular?n={n}&page={page}"))
            if path == "/api/bili/subtitle":
                # 一起看时只取当前这一小段（省 token：默认前后 6 秒）
                # 分 P：bvid/id 可以是 BV… / BV…_p2 / bili:BV…_p2，也可以另给 p=2
                bv = (q.get("bvid", [""])[0] or q.get("id", [""])[0] or "").strip()
                if not bv:
                    return self._err(400, "没说是哪个视频")
                t = (q.get("t", [""])[0] or "").strip()
                w = max(1.0, min(60.0, float(q.get("window", ["6"])[0] or 6)))
                bvid, p = split_bili(bv, (q.get("p", [""])[0] or "").strip() or None)
                pf = bili_find(bv if bv.startswith("bili:") else "bili:" + bv) if PGC_FID_RE.match(
                    bv if bv.startswith("bili:") else "bili:" + bv) else None
                if pf and pf.get("bvid"):
                    bvid, p = pf["bvid"], 1
                if bvid:
                    return self._send(200, bili_subtitle_for(bvid, p, t or None, w))
                u = f"/subtitle?bvid={quote(bv)}&window={w:g}" + (f"&t={quote(t)}" if t else "")
                return self._send(200, bili_get(u))
            if path == "/api/bili/pages":
                # 分 P 下拉：这部一共几 P、每 P 叫什么（id=bili:BV…[_pN] 或 bvid=BV…）
                raw_id = (q.get("id", [""])[0] or q.get("bvid", [""])[0] or "").strip()
                pf = bili_find(raw_id) if PGC_FID_RE.match(raw_id) else None
                if pf and pf.get("pgc"):
                    try:
                        v = pgc_season(f"ss{int(pf['pgc']['season_id'])}")
                    except Exception as e:
                        return self._send(200, {"ok": False, "error": str(e), "pages": []})
                    eps = v.get("episodes") or []
                    cur_ep = int(pf["pgc"]["ep_id"])
                    return self._send(200, {
                        "pgc": True, "title": v.get("title") or "", "seasonId": v.get("season_id"),
                        "p": next((i + 1 for i, e in enumerate(eps) if int(e.get("ep_id") or 0) == cur_ep), 1),
                        "pages": [{"p": i + 1, "ep": e.get("ep_id"),
                                   "part": " ".join(x for x in (e.get("index"), e.get("long_title")) if x) or "正片",
                                   "duration": e.get("duration"), "badge": e.get("badge") or "",
                                   "limited": bool(e.get("area_limited"))} for i, e in enumerate(eps)]})
                bvid, p = split_bili(raw_id)
                if not bvid:
                    return self._err(400, "只认 BV 号")
                v = bili_pages(bvid)
                return self._send(200, {"bvid": bvid, "p": p, "title": v.get("title") or "",
                                        "pages": [{"p": int(x.get("p") or 0), "part": x.get("part") or "",
                                                   "duration": x.get("duration")} for x in (v.get("pages") or [])]})
            if path == "/api/bili/info":
                # bilitube 播放页：UP · 播放量 · 日期 ＋ 简介（id=bili:BV…[_pN] 或 bvid=BV…）
                raw_id = (q.get("id", [""])[0] or q.get("bvid", [""])[0] or "").strip()
                pf = bili_find(raw_id) if PGC_FID_RE.match(raw_id) else None
                if pf and pf.get("pgc"):
                    try:
                        v = pgc_season(f"ss{int(pf['pgc']['season_id'])}")
                    except Exception as e:
                        return self._send(200, {"ok": False, "error": str(e)})
                    desc = str(v.get("evaluate") or "")
                    return self._send(200, {"ok": True, "pgc": True, "up": v.get("type_name") or "",
                                            "view": v.get("views"), "danmaku": v.get("danmakus"), "pubdate": None,
                                            "desc": desc[:300], "descCut": len(desc) > 300,
                                            "total": v.get("total"), "isFinish": v.get("is_finish"),
                                            "seasonTitle": v.get("title") or ""})
                bvid, _p = split_bili(raw_id)
                if not bvid:
                    return self._err(400, "只认 BV 号")
                try:
                    out = dict(bili_info(bvid))
                except Exception as e:
                    return self._send(200, {"ok": False, "error": str(e)})
                out.update(bili_state(bvid, (q.get("fresh", [""])[0] or "") == "1", out.get("aid")))
                return self._send(200, out)
            if path == "/api/bili/pgc/search":          # 搜索结果里的番剧 / 影视那一小段
                kw = (q.get("q", [""])[0] or "").strip()[:80]
                if not kw:
                    return self._err(400, "要搜什么呢")
                return self._send(200, bili_get(f"/pgc/search?q={quote(kw)}&n=6"))
            if path == "/api/bili/pgc/follow":          # 「我的」→ 追番 / 追剧（只读）
                kind = 2 if (q.get("type", ["1"])[0] or "1") == "2" else 1
                page = max(1, min(50, int(float(q.get("page", ["1"])[0] or 1))))
                return self._send(200, bili_get(f"/pgc/follow?type={kind}&page={page}&n=15"))
            if path == "/api/bili/myreplies":
                n = max(1, min(30, int(float(q.get("n", ["20"])[0] or 20))))
                return self._send(200, bili_get(f"/my_replies?n={n}"))
            if path == "/api/bili/me":
                return self._send(200, bili_get("/me"))
            if path == "/api/bili/follow_group":         # AI 主页的「关注」：AI 专属关注分组里的人（只读）
                n = max(1, min(50, int(float(q.get("n", ["30"])[0] or 30))))
                page = max(1, min(50, int(float(q.get("page", ["1"])[0] or 1))))
                return self._send(200, bili_get(f"/follow_group?n={n}&page={page}"))
            # 续播提示：B 站记的「上次看到」（按片单这条的 bvid/cid 问 bili_mcp /progress；
            # B 站记的是别的 P 就回 t=0，页面不提示）。出错回 ok:false，页面静默。
            if path == "/api/bili/progress":
                f = resolve((q.get("id", [""])[0] or "").strip())
                if not f or not is_bili(f) or not f.get("bvid") or not f.get("cid"):
                    return self._err(404, "没有这部片子")
                if is_pgc(f):                    # 番剧：B 站记的「上次看到」在整部的信息里（哪一集、第几秒）
                    try:
                        v = pgc_season(f"ss{int(f['pgc']['season_id'])}")
                    except Exception as e:
                        return self._send(200, {"ok": False, "error": str(e)})
                    pr = v.get("progress") or {}
                    same = str(pr.get("ep_id") or "") == str(f["pgc"]["ep_id"])
                    return self._send(200, {"ok": True, "id": f["id"], "t": int(pr.get("t") or 0) if same else 0,
                                            "lastEp": pr.get("ep_id"), "sameCid": same})
                try:
                    d = bili_get(f"/progress?bvid={quote(str(f['bvid']))}&cid={int(f['cid'])}", timeout=15)
                except Exception as e:
                    return self._send(200, {"ok": False, "error": str(e)})
                t = int(d.get("t") or 0) if d.get("same_cid") is not False else 0
                return self._send(200, {"ok": True, "id": f["id"], "t": t,
                                        "lastCid": d.get("last_play_cid"), "sameCid": d.get("same_cid")})
            if path == "/api/bili/face":
                p = bili_face_path((q.get("mid", [""])[0] or "").strip(),
                                   (q.get("u", [""])[0] or "").strip())
                if not p:
                    return self._err(404, "头像没拿到")
                return self._file(p, "image/jpeg", IMG_CACHE)
            if path == "/api/bili/listcover":
                p = bili_cover_any((q.get("bvid", [""])[0] or "").strip(),
                                   (q.get("u", [""])[0] or "").strip())
                if not p:
                    return self._err(404, "封面没拿到")
                return self._file(p, "image/jpeg", IMG_CACHE)
            if path == "/api/prefs":                     # 称呼＋头像/底图地址
                return self._send(200, prefs_get())
            if path in UPLOAD_SLOTS:                     # 自定义头像 / 皮肤底图
                slot = UPLOAD_SLOTS[path][0]
                m = _slot_meta(slot)
                if not m:
                    return self._err(404, "没传过")
                return self._file(_slot_paths(slot)[0], m.get("mime") or "image/jpeg")
            if path == "/api/bili/login/state":          # 扫码进度（转发 bili_mcp 的 login status）
                try:
                    return self._send(200, bili_login_state())
                except Exception as e:
                    return self._send(200, {"ok": False, "error": f"问不到 bili_mcp：{type(e).__name__}"})
            if path == "/api/now":
                snap = state_snapshot()
                snap.update(peek_info())
                if snap.get("film"):
                    f = resolve(snap["film"])
                    if f:
                        cue = sub_at(f, snap["t"])
                        snap["subtitle"] = cue["text"] if cue else None
                    ns = notes_of(snap["film"])
                    snap["notes"] = len(ns)
                    snap["lastNote"] = ns[-1] if ns else None
                    snap["lastDanmaku"] = last_user_dm(snap["film"])   # TA 最近发的那条弹幕 {t,text,at}
                snap["live"] = live_seen()               # TA 最近开的直播间（没有就是 None）
                return self._send(200, snap)
            if path == "/api/dm/say":
                lr = (q.get("live", [""])[0] or "").strip()
                if lr.isdigit() and len(lr) <= 12:          # 直播页每两秒来一次＝TA 此刻开着这间
                    with _lock:
                        _live_seen.update(room=int(lr), poll=time.time(), at=_live_seen.get("at") or time.time(),
                                          ls=(q.get("ls", [""])[0] == "1"))   # ls=1＝页面支持按需回传画面（见 live_look_ask）
                since = max(0, int(float(q.get("since", ["0"])[0] or 0)))
                d = dm_say_since(since)
                d["watching"] = watching()
                d.update(cue_info())                 # 插播：这个每两秒一次的响应里也带上
                if lr.isdigit():
                    rq = live_look_pending(int(lr))
                    if rq:
                        d["shotReq"] = rq            # AI 在 look：页面当场拼三帧传回 /live/lookshot
                ps = (q.get("pub", [""])[0] or "").strip()
                if lr.isdigit() and ps.isdigit():    # 直播页开着公开弹幕：捎回这间房 pub 之后的几条（pub=0＝只同步序号）
                    pd = ldm_since(int(lr), int(ps))
                    if int(ps) == 0:
                        pd["list"] = []
                    d["pub"] = pd
                return self._send(200, d)
            if path == "/api/peek":
                return self._send(200, peek_info())
            if path == "/api/poster":
                f = self._film_arg(q, fallback_now=False)
                if not f:
                    return self._err(404, "没有这部片子")
                p = poster_path(f)
                if not p:
                    return self._err(404, "抓不到海报")
                return self._file(p, "image/jpeg")
            if path == "/api/frame":
                lf = (q.get("film", [""])[0] or "").strip()
                lm = re.fullmatch(r"(?i)live(?::(\d{1,12})(?::([0-9a-z]{1,20}))?)?", lf)
                if lm or lf == "直播":      # 直播：不走片单（live:<房间>:<弹幕 id>＝TA 发那条弹幕时存的三帧）
                    room = live_room_arg(f"live:{lm.group(1)}" if lm and lm.group(1) else lf)
                    if not room:
                        return self._err(404, "不知道是哪间直播间（TA 最近没开直播）：给 film=live:<房间号>")
                    single = (q.get("frames", [""])[0] or "").strip() == "1"
                    lid = lm.group(2) if lm else None
                    lwhy = "single" if single else None   # 为什么不是「TA 屏幕上此刻」那张（英文码，响应头只能 latin-1）
                    if not single and not lid:       # AI 主动 look：先向 TA 开着的页面要 TA 屏幕上此刻那张
                        page, lwhy = live_look_ask(room)
                        if page:
                            return self._send(200, page, ctype="image/jpeg", extra={"X-Live-Shot": "page"})
                    xw = {"X-Live-Why": lwhy} if lwhy else {}
                    if not single:
                        shot = live_dm_shot_bytes(room, lid)
                        if shot:
                            return self._send(200, shot, ctype="image/jpeg", extra={"X-Live-Shot": "dm", **xw})
                        if lid:
                            return self._err(404, "TA 发那条弹幕时的画面没存下来（当时没抓到，或者已经过了一天）")
                    w_raw = (q.get("w", [""])[0] or "").strip()
                    w = int(float(w_raw)) if w_raw else (960 if single else TRI_W)
                    try:
                        data, why = live_frame_bytes(room, 1 if single else 3, w)
                    except LookupError as e:
                        return self._err(409, str(e))
                    if not data:
                        return self._err(504, why)
                    return self._send(200, data, ctype="image/jpeg", extra={"X-Live-Shot": "server", **xw})
                f = self._film_arg(q)
                if not f:
                    return self._err(404, "没有这部片子")
                t = q.get("t", ["0"])[0]
                t = state_snapshot().get("t", 0) if t in ("", "当前", "now") else float(t)
                w_raw = q.get("w", [""])[0]
                w = max(160, min(1280, int(float(w_raw or 640))))
                peek = (q.get("peek", [""])[0] or "") == "companion"
                if peek:
                    mark_peek(f["id"], t)
                # 三帧拼图：frames=3 要拼图、frames=1 要单帧。不给 frames 时（兼容只传 width 的调用方），
                # AI（peek=companion）的请求按 width 当画质档：
                # 不给/640＝三帧标准（每帧 TRI_W）；<640＝三帧省一点（每帧就按这个宽，比如 320）；
                # 641–720＝三帧高清（每帧 640）；>720＝那一秒的单帧大图（比如 960、1280）。
                fr = (q.get("frames", [""])[0] or "").strip()
                tri = fr == "3" or (fr == "" and peek and w <= 720)
                if tri:
                    if fr == "3" and w_raw: tw = int(float(w_raw))
                    elif w == 640: tw = TRI_W
                    else: tw = min(w, 640)
                    p = frame_tri_path(f, t, tw)
                    if p:
                        return self._file(p, "image/jpeg")
                    log("三帧没拼成，退回单帧", f["id"], t)
                p = frame_path(f, t, w)
                if not p:
                    return self._err(500, "抓帧失败（ffmpeg）")
                return self._file(p, "image/jpeg")
            if path == "/api/sub":
                f = self._film_arg(q)
                if not f:
                    return self._err(404, "没有这部片子")
                t = q.get("t", ["0"])[0]
                t = state_snapshot().get("t", 0) if t in ("", "当前", "now") else float(t)
                w = max(1.0, min(600.0, float(q.get("window", ["20"])[0])))
                name, hits = sub_window(f, t, w)
                if name is None:
                    return self._send(200, {"film": f["id"], "hasSub": False, "cues": []})
                return self._send(200, {"film": f["id"], "hasSub": True, "sub": name,
                                        "t": t, "window": w, "cues": hits})
            if path == "/api/vtt":
                f = self._film_arg(q, fallback_now=False)
                if not f:
                    return self._err(404, "没有这部片子")
                sp = find_sub(f)
                if not sp:
                    return self._err(404, "这部片子旁边没有字幕")
                return self._send(200, to_vtt(parse_cues(sp)), "text/vtt; charset=utf-8")
            if path == "/api/notes":
                fid = (q.get("film", [""])[0] or state_snapshot().get("film") or "")
                ns = notes_of(fid)
                if (q.get("dm", ["1"])[0] or "1") != "0":
                    # AI 的批注按弹幕记（见 companion_dm），这里并进来一起返回；页面按 kind 区分
                    ns = sorted(ns + [dict(r, kind="danmaku") for r in dm_log(fid) if r["author"] == "companion"],
                                key=lambda n: n.get("t", 0))
                return self._send(200, {"film": fid, "notes": ns})
            # 两个人的私人弹幕合在一起按时间排。不给 film 就列有弹幕的片子
            if path == "/api/dm/log":
                fid = (q.get("film", [""])[0] or "").strip()
                if not fid:
                    return self._send(200, {"films": dm_log_films()})
                items = [dict(r, kind="danmaku") for r in dm_log(fid)]
                if (q.get("notes", [""])[0] or "") == "1":
                    items += [{"t": round(float(n.get("t") or 0), 1), "author": n.get("author") or "user",
                               "text": n.get("text"), "at": n.get("at"), "id": n.get("id"), "kind": "note"}
                              for n in notes_of(fid)]
                    items.sort(key=lambda x: (x["t"], x.get("at") or ""))
                return self._send(200, {"film": fid, "title": title_of(fid), "count": len(items), "items": items})
            # 前一秒 / 这一秒 / 后一秒：字幕＋公开弹幕＋两个人的私人弹幕（watching look 用）
            if path == "/api/around":
                f = self._film_arg(q)
                if not f:
                    return self._err(404, "没有这部片子")
                t = q.get("t", [""])[0]
                t = float(state_snapshot().get("t", 0) or 0) if t in ("", "当前", "now") else max(0.0, float(t))
                lines, has_sub = around_lines(f, t, fetch=True)
                return self._send(200, {"film": f["id"], "t": round(t, 2), "hasSub": has_sub, "lines": lines})
            if path == "/api/danmaku":
                f = self._film_arg(q, fallback_now=False)
                if not f:
                    return self._err(404, "没有这部片子")
                if is_bili(f):
                    try:
                        dm = bili_danmaku(f)
                    except Exception as e:
                        log("弹幕没拿到", f["id"], e)
                        dm = []
                    return self._send(200, {"film": f["id"], "danmaku": dm})
                return self._send(200, {"film": f["id"],
                                        "danmaku": side_json(f, ".danmaku.json") or []})
            if path == "/api/comments":
                f = self._film_arg(q, fallback_now=False)
                if not f:
                    return self._err(404, "没有这部片子")
                if is_bili(f):
                    try:
                        cm = bili_comments(f)
                    except Exception as e:
                        log("评论区没拿到", f["id"], e)
                        cm = []
                    return self._send(200, {"film": f["id"], "comments": cm})
                return self._send(200, {"film": f["id"],
                                        "comments": side_json(f, ".comments.json") or []})
            return self._err(404, "没有这条路")
        except Exception as e:
            log("GET 出错", path, e)
            return self._err(500, str(e))

    do_HEAD = do_GET

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        path = u.path.rstrip("/") or "/"
        if path in UPLOAD_SLOTS:                         # 请求体是图片原始字节，要在按 JSON 解析之前接走
            return self._upload_slot(path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except Exception:
            return self._err(400, "body 不是 JSON")
        try:
            if path == "/api/prefs":
                if not isinstance(body, dict):
                    return self._err(400, "要一个对象")
                return self._send(200, prefs_set(body))
            if path == "/api/bili/login/qr":             # TA 在设置里点了「扫码登录 / 重新扫码」才来
                try:
                    return self._send(200, bili_login_qr())
                except Exception as e:
                    return self._send(200, {"ok": False, "error": f"问不到 bili_mcp：{type(e).__name__}"})
            if path == "/api/bili/logout":               # 设置 → 账号 → 退出 → 确认（见 bili_logout）
                return self._send(200, bili_logout())
            if path == "/api/beat":
                return self._send(200, beat(body.get("film"), body.get("t"),
                                            bool(body.get("playing"))))
            if path in ("/api/notes", "/api/dm/companion"):
                fid = (body.get("film") or q.get("film", [""])[0]
                       or state_snapshot().get("film") or "")
                if path == "/api/dm/companion" and not (body.get("film") or q.get("film", [""])[0]) and not watching():
                    h = live_hold_room()              # 没给 film、片子没在放、TA 刚才还开着直播：回的是直播，不是上一部片子
                    if h:
                        fid = f"live:{h[0]}"
                lfid = live_fid(fid) if path == "/api/dm/companion" else None
                if lfid:
                    # AI 在直播里回 TA：记进同一份记录（没有时间码），TA 开着这间就当场飘过。
                    # 推送里的 live:<房间>:<id> 也认；TA 的直播页没连着就明说没发出去，不记、不改投别的片子
                    room = int(lfid[5:])
                    h = live_hold_room()
                    if live_watch_room() != room and not (h and h[0] == room):
                        with _lock:
                            s = dict(_live_seen)
                        ago = int(time.time() - s["poll"]) if s.get("room") == room and s.get("poll") else None
                        why = (f"TA 的直播页这会儿没连上（{ago} 秒没来问了），没发出去" if ago is not None and ago < LIVE_SEEN_MAX
                               else "TA 这会儿没开着这间直播，没发出去")
                        return self._send(409, {"ok": False, "error": why, "live": True, "film": lfid})
                    rec, err = companion_dm(lfid, 0, body.get("text"), via="dm")
                    if err:
                        return self._err(400, err)
                    dm_pool_put({"text": rec["text"], "author": "companion", "color": None, "mode": "scroll",
                                 "film": rec["film"], "t": None, "log": True, "lid": rec["id"], "live": True})
                    held = None if live_watch_room() == room else (h[1] if h else None)
                    return self._send(200, {"ok": True, "danmaku": rec, "pooled": True, "live": True, "held": held})
                f = resolve(fid)
                if not f:
                    return self._err(404, "没有这部片子")
                t = body.get("t")
                if t in (None, "", "当前", "now"):
                    t = state_snapshot().get("t", 0)
                if path == "/api/dm/companion" or (body.get("author") or "user") == "companion":
                    # AI 的批注按 AI 的私人弹幕记（POST /api/notes 带 author=companion 的也走这里）
                    try:
                        t = float(t)
                    except Exception:
                        return self._err(400, "t 不是秒数")
                    rec, err = companion_dm(f["id"], t, body.get("text"),
                                      via="dm" if path == "/api/dm/companion" else "note")
                    if err:
                        return self._err(400, err)
                    pooled = companion_dm_pool(rec)
                    out = {"ok": True, "danmaku": rec, "pooled": pooled,
                           "note": {"t": rec["t"], "author": "companion", "text": rec["text"], "id": rec["id"]}}
                    return self._send(200, out)
                note = add_note(f["id"], t, body.get("author") or "user", body.get("text"))
                if not note:
                    return self._err(400, "批注不能是空的")
                return self._send(200, {"ok": True, "note": note})
            if path == "/api/bili/add":
                ref = str(body.get("ref") or body.get("url") or q.get("ref", [""])[0] or "")[:600]
                # 插播：play=now|next，或者 ref 末尾带 #now / #next（兼容只传 ref 的调用方）
                mode = str(body.get("play") or "").strip().lower()
                m = re.search(r"\s*[#＃](now|next|立刻|下一个)\s*$", ref, re.I)
                if m:
                    ref = ref[:m.start()].strip()
                    mp = re.match(r"^(?:bili:)?(BV[0-9A-Za-z]{10})_p(\d{1,4})$", ref)   # 有的调用方会把 bili:BV…_p2#now 原样递过来
                    if mp:
                        ref = f"{mp.group(1)}?p={mp.group(2)}"
                    mode = mode or {"立刻": "now", "下一个": "next"}.get(m.group(1), m.group(1).lower())
                mode = mode if mode in ("now", "next") else ""
                try:
                    rec, fresh = bili_add(ref)
                except ValueError as e:
                    return self._err(400, str(e))
                except Exception as e:
                    return self._err(502, f"没接上：{e}")
                with _lock:
                    _seen(rec["id"])
                    save_bili()
                if fresh:
                    prune_catalog()
                out = {"ok": True, "new": fresh, "film": bili_film_entry(rec), "watching": watching(), "cued": None}
                # 页面开片时带 direct=1：顺手把直连地址一起回（地址本来就缓存），页面不用再问一趟 /api/bili/direct
                if str(body.get("direct") or "") in ("1", "true", "True") and not is_pgc(rec):
                    try:
                        out["direct"] = direct_reply(resolve(rec["id"]), direct_info(resolve(rec["id"])))
                    except Exception as e:
                        log("开片顺带的直连地址没拿到（页面会自己再问）", rec.get("id"), e)
                if mode and out["watching"]:
                    cur_film = state_snapshot().get("film")
                    if mode == "now":
                        out["cue"] = cue_now(rec["id"]); out["cued"] = "now"
                    elif cur_film != rec["id"]:
                        out["upNext"] = upnext_set(cur_film, rec["id"]); out["cued"] = "next"
                    else:
                        out["cued"] = "same"         # TA 正在看的就是这部
                journal("order", rec["id"], mode=mode or None, cued=out["cued"])
                return self._send(200, out)
            # 套在 app 壳里的页面在 TA 设备上取到番剧地址、真放起来了：页面来报一声，挂着的这一集进播放列表。
            # 只认片单里已有的番剧集；不收地址、不收任何 B 站返回，只改一个标记。
            if path == "/api/bili/keep":
                fid = str(body.get("id") or "").strip()
                f = resolve(fid) if PGC_FID_RE.match(fid) else None
                if not f or not is_pgc(f):
                    return self._err(404, "没有这一集")
                rec = bili_unpark(fid, shell=str(body.get("via") or "") == "shell")
                return self._send(200, {"ok": True, "film": bili_film_entry(rec)})
            if path == "/api/live/lookshot":             # 页面传回 AI look 要的那张（TA 屏幕上此刻的三帧）
                room, rid = str(body.get("room") or ""), str(body.get("id") or "")
                if not room.isdigit() or len(room) > 12 or not rid:
                    return self._err(400, "要 room 和 id")
                ok = live_look_put(int(room), rid[:24], body.get("shot"), str(body.get("err") or "") or None)
                return self._send(200, {"ok": ok})
            if path == "/api/dm/say":
                rec, err = dm_say(body.get("text"), body.get("author") or "companion",
                                  body.get("color"), body.get("mode") or "scroll")
                if err:
                    return self._send(200, {"ok": False, "error": err, "watching": watching()})
                if rec["author"] == "user" and isinstance(body.get("bilitube"), dict):
                    try:
                        rec["t"] = max(0.0, float(body["bilitube"].get("t")))   # 页面按下那一刻的秒数，比心跳准
                    except Exception:
                        pass
                if rec["author"] == "user" and body.get("chat") is not False:
                    push_user_danmaku(rec["text"], body.get("frame"), body.get("bilitube"), body.get("liveShot"))
                if rec["author"] == "companion" and rec.get("film"):
                    # AI 经 /api/dm/say（bili_danmaku_say）发的弹幕也记进两个人的弹幕记录（回放时到点还会飘过）
                    lg, _ = companion_dm(rec["film"], rec.get("t") or 0, rec["text"], via="say")
                    if lg:
                        with _lock:
                            rec.update({"log": True, "lid": lg["id"], "live": True, "t": lg["t"]})
                return self._send(200, {"ok": True, "id": rec["id"], "film": rec.get("film"),
                                        "t": rec.get("t"), "author": rec["author"]})
            # 心跳：播放页放 B 站视频时往 B 站报进度（历史记录 / 上次看到）。
            # bvid/cid 从片单这条取，不信页面；节流在 bili_mcp 那头。失败回 ok:false，页面静默。
            if path == "/api/bili/heartbeat":
                f = resolve(str(body.get("id") or body.get("film") or "").strip())
                if not f or not is_bili(f) or not f.get("bvid") or not f.get("cid"):
                    return self._err(404, "没有这部片子")
                send = {"bvid": f["bvid"], "cid": int(f["cid"])}
                if is_pgc(f):
                    g = f["pgc"]
                    send.update({"aid": f.get("aid"), "epid": g.get("ep_id"), "sid": g.get("season_id"),
                                 "sub_type": g.get("type") or 1})
                try:
                    send["progress"] = max(0, int(float(body.get("progress") or 0)))
                    send["duration"] = max(0, int(float(body.get("duration") or f.get("duration") or 0)))
                    send["realtime"] = max(0, int(float(body.get("realtime") or 0)))
                    send["play_type"] = int(body.get("play_type") or 0)
                except (TypeError, ValueError):
                    return self._err(400, "参数不对")
                try:
                    r = bili_post("/heartbeat", send, timeout=20)
                except Exception as e:
                    return self._send(200, {"ok": False, "error": str(e)})
                r["ok"] = True
                return self._send(200, r)
            if path == "/api/bili/watchlater":
                vid = str(body.get("id") or body.get("bvid") or "").strip()
                if not vid:
                    return self._err(400, "没说是哪个视频")
                try:
                    r = bili_post("/watchlater", {"id": vid, "remove": bool(body.get("remove"))})
                except Exception as e:
                    return self._err(502, str(e))
                bili_state_drop(vid)
                r["ok"] = True
                return self._send(200, r)
            if path in ("/api/bili/like", "/api/bili/fav"):
                vid = str(body.get("id") or body.get("bvid") or "").strip()
                if not vid:
                    return self._err(400, "没说是哪个视频")
                send = {"id": vid}
                if path.endswith("like"):
                    send["on"] = False if body.get("on") is False else True
                else:
                    if body.get("media_id"):
                        send["media_id"] = int(body["media_id"])
                    send["remove"] = bool(body.get("remove"))
                try:
                    r = bili_post("/like" if path.endswith("like") else "/fav", send)
                except Exception as e:
                    return self._err(502, str(e))
                bili_state_drop(vid)
                r["ok"] = True
                return self._send(200, r)
            if path in ("/api/remove", "/api/bili/remove"):
                fid = str(body.get("id") or body.get("film") or q.get("id", [""])[0] or "").strip()
                res, err = remove_film(fid)
                if err:
                    return self._err(404 if "没有" in err else 400, err)
                return self._send(200, res)
            if path == "/api/now/clear":
                fid = str(body.get("film") or body.get("id") or "").strip() or None
                return self._send(200, {"ok": True, "cleared": clear_watch(fid)})
            return self._err(404, "没有这条路")
        except Exception as e:
            log("POST 出错", path, e)
            return self._err(500, str(e))

    def do_DELETE(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        path = u.path.rstrip("/") or "/"
        if path in ("/api/bili", "/api/films"):
            fid = (q.get("id", [""])[0] or q.get("film", [""])[0] or "").strip()
            res, err = remove_film(fid)
            if err:
                return self._err(404 if "没有" in err else 400, err)
            return self._send(200, res)
        if path == "/api/now":
            return self._send(200, {"ok": True, "cleared": clear_watch((q.get("film", [""])[0] or "").strip() or None)})
        if path == "/api/upnext":      # 插播：撤掉「下一个播放」
            upnext_clear()
            return self._send(200, {"ok": True})
        if path == "/api/dm/log":      # 删 AI 的一条：?id=f…[&film=…]（TA 的不归这里删）
            n = companion_dm_delete((q.get("film", [""])[0] or "").strip(), (q.get("id", [""])[0] or "").strip())
            return self._send(200 if n else 404, {"ok": bool(n), "removed": n})
        return self._err(404, "没有这条路")

    def _live_seg(self, url):
        """直播代理那一路的分片 / 初始化段：只转 B 站直播 CDN 的地址（不当开放代理），整块转发。"""
        if not LIVE_SEG_HOST_RE.match(url or ""):
            return self._err(400, "这不是 B 站直播 CDN 的地址")
        try:
            up = _live_fetch(url, timeout=15, rng=self.headers.get("Range"))
        except urllib.error.HTTPError as e:
            return self._err(e.code if e.code in (403, 404, 410, 416) else 502, f"CDN 回了 {e.code}")
        except Exception as e:
            return self._err(502, f"CDN 连不上：{type(e).__name__}")
        try:
            self.send_response(up.status)
            has_len = False
            for h in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                v = up.headers.get(h)
                if v:
                    self.send_header(h, v)
                    has_len = has_len or h == "Content-Length"
            self.send_header("Cache-Control", "no-store")
            if not has_len:
                self.close_connection = True
                self.send_header("Connection", "close")
            self.end_headers()
            if self.command == "HEAD":
                return
            while True:
                chunk = up.read(STREAM_CHUNK)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            self.close_connection = True
        except Exception as e:
            log("直播分片转发中断", e)
            self.close_connection = True
        finally:
            try:
                up.close()
            except Exception:
                pass

    def _stream(self, film):
        """把 CDN 的 mp4 按 Range 原样转发给 <video>。64K 一块地流，TA 拖进度条时浏览器会断开旧连接，
        那头的 BrokenPipe 安静吞掉。CDN 说 403/404/410/412（地址过期）就重问一次 playurl。"""
        rng = self.headers.get("Range")
        up = None
        for attempt in (0, 1):
            info = stream_url(film, force=(attempt == 1))
            if not info:
                return self._err(502, "拿不到播放地址（bili_mcp 在跑吗？登录还有效吗？）")
            req = urllib.request.Request(info["url"], method="HEAD" if self.command == "HEAD" else "GET",
                                         headers={"User-Agent": STREAM_UA, "Referer": STREAM_REFERER,
                                                  "Accept": "*/*", "Accept-Encoding": "identity"})
            if rng:
                req.add_header("Range", rng)
            try:
                up = urllib.request.urlopen(req, timeout=30)
                break
            except urllib.error.HTTPError as e:
                if e.code in (403, 404, 410, 412) and attempt == 0:
                    log("CDN 拒了", film["id"], e.code, "重新拿地址")
                    continue
                if e.code == 416:
                    return self._send(416, {"error": "Range 超出"}, extra={"Content-Range": f"bytes */{info.get('size') or '*'}"})
                return self._err(502, f"CDN 回了 {e.code}")
            except Exception as e:
                if attempt == 0:
                    log("CDN 连不上", film["id"], e, "重新拿地址")
                    continue
                return self._err(502, f"CDN 连不上：{e}")
        if up is None:
            return self._err(502, "CDN 没回")
        try:
            self.send_response(up.status)
            has_len = False
            for h in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "Last-Modified", "ETag"):
                v = up.headers.get(h)
                if v:
                    self.send_header(h, v)
                    if h == "Content-Length":
                        has_len = True
            if not up.headers.get("Accept-Ranges"):
                self.send_header("Accept-Ranges", "bytes")
            if not up.headers.get("Content-Type"):
                self.send_header("Content-Type", "video/mp4")
            self.send_header("Cache-Control", "no-store")
            if not has_len:
                self.close_connection = True
                self.send_header("Connection", "close")
            self.end_headers()
            if self.command == "HEAD":
                return
            while True:
                chunk = up.read(STREAM_CHUNK)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            self.close_connection = True          # TA 拖了进度条 / 关了页面，正常
        except Exception as e:
            log("转发中断", film["id"], e)
            self.close_connection = True
        finally:
            try:
                up.close()
            except Exception:
                pass

    def _file(self, p, ctype, cache=None):
        with open(p, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache or "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _upload_slot(self, path):
        """头像 / 皮肤底图上传：请求体是图片原始字节，空请求体＝删掉。"""
        slot, cap = UPLOAD_SLOTS[path]
        img, meta = _slot_paths(slot)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._err(400, "长度不对")
        if length <= 0:
            for p in (img, meta):
                try:
                    os.remove(p)
                except OSError:
                    pass
            return self._send(200, {"ok": True, "cleared": True, "prefs": prefs_get()})
        if length > cap:
            return self._err(413, f"图太大了，{cap // 1024}KB 以内")
        raw = self.rfile.read(length)
        mime = _img_mime(raw)
        if not mime:
            return self._err(415, "只收 jpg / png / webp / gif")
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        with open(img + ".tmp", "wb") as f:
            f.write(raw)
        os.replace(img + ".tmp", img)
        save_json(meta, {"mime": mime, "size": len(raw), "at": int(time.time() * 1000)})
        return self._send(200, {"ok": True, "size": len(raw), "prefs": prefs_get()})

    def films(self):
        out = []
        for i, n, p in list_films_raw():
            ext = os.path.splitext(n)[1].lower()
            f = {"id": i, "name": n, "path": p}
            sub = find_sub(f)
            out.append({
                "id": i,
                "title": i,
                "file": n,
                "duration": probe_duration(p),
                "hasSub": bool(sub),
                "sub": os.path.basename(sub) if sub else None,
                "poster": f"{URL_PREFIX}/api/poster?film={quote(i)}",
                "src": f"{URL_PREFIX}/media/{quote(n)}",
                "vtt": f"{URL_PREFIX}/api/vtt?film={quote(i)}" if sub else None,
                "playable": ext in NATIVE_EXT,
                "notes": len(notes_of(i)),
                "size": os.path.getsize(p) if os.path.exists(p) else 0,
                "hasDanmaku": has_side(i, ".danmaku.json"),
                "hasComments": has_side(i, ".comments.json"),
                "danmaku": f"{URL_PREFIX}/api/danmaku?film={quote(i)}" if has_side(i, ".danmaku.json") else None,
                "comments": f"{URL_PREFIX}/api/comments?film={quote(i)}" if has_side(i, ".comments.json") else None,
            })
        with _lock:
            blist = [dict(b) for b in _bili if not b.get("parked")]     # 放不了的集不进播放列表
        for b in blist:
            out.append(bili_film_entry(b))
        return out


def main():
    global _notes, _meta, _state, _bili
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(FILM_DIR, exist_ok=True)
    for d in (DM_DIR, CM_DIR, TRASH_DIR):
        os.makedirs(d, exist_ok=True)
    saved_bili = load_json(BILI_FILE, [])
    if isinstance(saved_bili, list):
        _bili = [b for b in saved_bili if isinstance(b, dict) and b.get("id") and b.get("cid")]
    _notes = load_json(NOTES_FILE, {})
    _meta = load_json(META_FILE, {})
    saved = load_json(NOW_FILE, {})
    if isinstance(saved, dict) and saved.get("film"):
        saved["playing"] = False        # 重启后一律当「没在播」，免得补一行假暂停
        _state.update(saved)
    cut = prune_catalog(recent=0)        # 开门时把底账剪到只剩用得着的
    if cut:
        log(f"底账剪掉 {cut} 条，留 {len(_bili)} 条")
    threading.Thread(target=watchdog, daemon=True).start()
    if LDM_ON:
        threading.Thread(target=ldm_manager, daemon=True).start()   # 直播公开弹幕：TA 开着直播才连
    log(f"bilitube 开门 http://{HOST}:{PORT}  片源={FILM_DIR}  直连={len(_bili)} 部  推送={'开' if PUSH_ENABLED else '关'}  小事件播报={'开' if PUSH_EVENTS else '关'}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
