# -*- coding: utf-8 -*-
"""
bilitube 的「一起看」这一半：watching（where / look）、stage（danmaku / queue / play_now），
以及 video subtitle 查 bilitube 里的片子（本地片），都走这里。

真正干活的是 bilitube 后端 server/bilitube_server.py（默认 127.0.0.1:18008），这里只负责问它、把结果写成人话。
只用标准库。

look 把画面存成 BILITUBE_IMAGE_DIR（默认 <仓库>/data/images）下的 att-scene-*.jpg，回话里单独一行写出完整路径；
聊天前端认这个目录的话，就能把它显示成图片。where / look 会在后端记一笔，用户的播放页亮 20 秒「在看这一幕」角标。
"""
import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

API = (os.environ.get("BILITUBE_API") or os.environ.get("BILITUBE_SERVER_URL", "http://127.0.0.1:18008").rstrip("/") + "/api").rstrip("/")
# 回话里「在播放页打开」的链接用；改成你部署的 bilitube 播放页地址
PAGE_URL = os.environ.get("BILITUBE_PAGE_URL", "/bilitube/")
UPLOADS = os.environ.get("BILITUBE_IMAGE_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "images")
BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
LOOK = 'watching(action="look")'          # 回话里提示下一步时用的写法


def api(path, params=None, payload=None, raw=False, timeout=90, method=None, hdrs=None):
    url = API + path
    if params:
        clean = {k: v for k, v in params.items() if v not in (None, "")}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    try:
        if method:
            req = urllib.request.Request(url, method=method)
        elif payload is None:
            req = urllib.request.Request(url)
        else:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            if hdrs is not None:                 # 要响应头的调用方（直播 look 要读 X-Live-Shot）传个 dict 进来
                hdrs.update((k.lower(), v) for k, v in r.headers.items())
            return body if raw else json.loads(body or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return {"_error": (json.loads(e.read() or b"{}") or {}).get("error") or f"HTTP {e.code}"}
        except Exception:
            return {"_error": f"HTTP {e.code}"}
    except urllib.error.URLError as e:
        return {"_error": f"连不上 bilitube 后端（在跑吗？看 BILITUBE_API）: {e}"}
    except Exception as e:
        return {"_error": str(e)}


def fmt_t(sec):
    sec = max(0, int(float(sec or 0)))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_cn(sec):
    sec = max(0, int(float(sec or 0)))
    if sec < 60:
        return f"{sec}秒"
    if sec < 3600:
        return f"{sec // 60}分{sec % 60}秒"
    return f"{sec // 3600}小时{(sec % 3600) // 60}分"


# 两个人的称呼：问 bilitube 后端 /api/prefs 的 names（设置里填的），缓存 5 秒；连不上就用中性默认
_NAMES_FALLBACK = {"user": "TA", "companion": "AI"}
_names = {"t": 0.0, "d": None}


def names():
    if _names["d"] is None or time.time() - _names["t"] > 5:
        d = api("/prefs", timeout=3)
        n = d.get("names") if isinstance(d, dict) else None
        _names["d"] = {k: str((n or {}).get(k) or "").strip() or v for k, v in _NAMES_FALLBACK.items()}
        _names["t"] = time.time()
    return _names["d"]


def who(a):
    return {"companion": names()["companion"], "user": names()["user"]}.get(a, a)


def current():
    d = api("/now")
    if "_error" in d:
        return None, d["_error"]
    if not d.get("film"):
        return None, "TA 还没在 bilitube 播放页开过片子。"
    return d, None


def pick_t(args, now):
    t = args.get("t")
    if t in (None, "", "当前", "now"):
        return float((now or {}).get("t") or 0)
    try:
        return max(0.0, float(t))
    except Exception:
        m = re.fullmatch(r"\s*(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)\s*", str(t))
        if m:
            return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        return float((now or {}).get("t") or 0)


def pick_film(args, now):
    f = str(args.get("film") or "").strip()
    return f or ((now or {}).get("film") or "")


def safe_name(s):
    s = re.sub(r"[^\w一-鿿.-]+", "_", str(s or "film"))
    return s.strip("._") or "film"


# ── watching where ──

def where_brief(args):
    """在放哪部、第几秒、播着没；这几秒的字幕和弹幕（和 look 用同一套按三秒对齐的行）；TA 最近发的一条弹幕；直播间。只回文字。"""
    now, err = current()
    if err:
        ll = live_line()
        return f"❌ {err}" + (f"\n{ll}" if ll else "")
    film = now.get("film")
    t = float(now.get("t") or 0)
    head = f"《{now.get('title')}》　{fmt_t(t)}"
    if now.get("duration"):
        head += f" / {fmt_t(now['duration'])}"
    head += "　" + ("正在播" if now.get("playing") else "停着")
    idle = now.get("idleMs") or 0
    if idle > 20000:
        head += f"（播放器已经 {fmt_cn(idle / 1000)}没上报进度，TA 多半离开了播放页）"
    lines = [head]
    ar = api("/around", {"film": film, "t": f"{t:.2f}"}, timeout=30)
    if isinstance(ar, dict) and "_error" not in ar:
        lines.extend(ar.get("lines") or ["（这几秒没字幕，也没人发弹幕）"])
    dm = now.get("lastDanmaku")
    if dm and dm.get("text"):
        lines.append(f"TA 最近发的　{fmt_t(dm.get('t'))}：{dm.get('text')}")
    ll = live_line(now)
    if ll:
        lines.append(ll)
    lines.append(f"film={film}")
    return "\n".join(lines)


def live_line(now=None):
    """TA 最近开的直播间。后端记的是播放页最近一次来要直播地址的时间，不等于 TA 此刻还在看。"""
    if now is None:
        now = api("/now")
    lv = now.get("live") if isinstance(now, dict) else None
    if not lv or not lv.get("room"):
        return ""
    name = f"《{lv.get('title') or lv['room']}》" + (f"（{lv['uname']}）" if lv.get("uname") else "")
    return (f"直播：播放页 {fmt_cn(lv.get('agoSec') or 0)}前打开过直播间 {lv['room']}{name}"
            f"（不等于 TA 此刻还在看）→ film=live:{lv['room']}")


# ── watching look ──

def act_look_live(args, fa):
    """直播没有时间轴：默认取最近约三秒、每秒一帧竖着摞（最下面那帧最新），quality=单帧 是一张大图。
    图的来源按顺序试：先向 TA 开着的播放页要 TA 屏幕上此刻的画面；要不到，再用 TA 两分钟内发弹幕那一刻存下的；
    最后才由服务器自己从直播流抓。回话照后端的 X-Live-Shot 响应头说清这张是哪来的；抓不到就照实说。"""
    q = str(args.get("quality") or "").strip()
    single = str(args.get("frames") or "").strip() == "1" or q == "单帧"
    m = re.fullmatch(r"(?i)live:(\d{1,12})(?::([0-9a-z]{1,20}))?", fa)
    lv = (api("/now") or {}).get("live") or {}
    if m:
        room, lid = m.group(1), m.group(2)
    else:
        if not lv:
            return "❌ TA 最近三小时没在播放页开过直播间；要看哪间就给 film=live:<房间号>"
        room, lid = str(lv["room"]), None
    saved = not single and (lid or (str(lv.get("room")) == room and lv.get("lastDm")))
    params = {"film": f"live:{room}:{lid}" if lid else "live:" + room, "frames": 1 if single else 3}
    if args.get("width"):
        params["w"] = int(args["width"])
    elif q in ("省", "高清"):
        params["w"] = 320 if q == "省" else 640
    info = api("/live/room", {"room": room})
    if "_error" in info:
        info = {}
    hd = {}
    data = api("/frame", params, raw=True, timeout=60, hdrs=hd)
    if isinstance(data, dict):
        return f"❌ 直播间 {room} 的画面没抓到：{data.get('_error', '不知道为什么')}"
    if not data or len(data) < 512:
        return "❌ 抓回来的直播画面是空的"
    os.makedirs(UPLOADS, exist_ok=True)
    path = os.path.join(UPLOADS, f"att-scene-live-{room}-{int(time.time())}{'' if single else '-3'}.jpg")
    try:
        with open(path, "wb") as f:
            f.write(data)
        os.chmod(path, 0o644)
    except Exception as e:
        return f"❌ 存不下来: {e}"
    name = f"直播间 {info.get('room') or room}《{info.get('title') or '（没拿到标题）'}》，主播 {info.get('uname') or '（没拿到名字）'}"
    what = "这一帧" if single else "最近约三秒（从上到下 −2s / −1s / 0s，粉框那帧最新）"
    src = hd.get("x-live-shot") or ""
    if not src:                                  # 旧版后端不带这个响应头：按有没有弹幕那一刻的截图来猜
        src = "dm" if saved else "server"
    saved = src == "dm"
    why = LIVE_WHY.get(hd.get("x-live-why") or "", "")
    if src == "page":
        when = "TA 屏幕上此刻的"
    elif src == "dm":
        when = "TA 发那条弹幕那一刻 TA 屏幕上存下的"
    else:
        when = "服务器自己从直播流拉的"
    out = [f"{name}　{when}{what}，{len(data) // 1024} KB：", path]
    if src == "page":
        out.append("（这张是 TA 播放器里的画面，就是 TA 眼前这一刻，可以照着图说。）")
    elif src == "dm":
        out.append(f"（这张是 TA 发那条弹幕那一刻存下的，不是此刻，TA 眼前的画面可能已经变了{'：' + why if why else ''}。）")
    else:
        out.append(f"（这张不是 TA 屏幕上的{'：' + why if why else ''}。服务器拉的流和 TA 眼前可能差几秒到十几秒，"
                   "照着图说画面要留余地。）")
    sl = lid or ((lv.get("lastDm") or {}).get("id") if saved else None)
    ps = api("/live/pubsum", {"room": room, "lid": sl}, timeout=5)
    if isinstance(ps, dict) and ps.get("line"):
        out.append(ps["line"])
    return "\n".join(out)


QUALITY_W = {"省": 320, "标准": None, "高清": 640}
# 直播 look 没拿到「TA 屏幕上此刻」那张的原因（后端响应头 X-Live-Why 的取值）
LIVE_WHY = {
    "closed": "TA 这会儿没在播放页开着这间直播",
    "old": "TA 的播放页是旧版本，不会回截图（刷新一下就好）",
    "timeout": "TA 的播放页 4 秒内没回",
    "hidden": "TA 的播放页在后台（切走了或锁屏了）",
    "tainted": "TA 这条线路的画面截不了",
    "nodecode": "TA 的播放器这会儿还没出画面",
    "bad": "TA 的播放页回来的图不对",
    "single": "单帧大图只能由服务器拉",
}


def film_name(film, now=None):
    """回话标题用片名，不写 bili:BV… 编号：TA 正在放的那部用心跳里的片名，别的问后端（/dm/log?film= 带 title），都没有才写编号。"""
    if now and now.get("film") == film and now.get("title") and now["title"] != film:
        return now["title"]
    d = api("/dm/log", {"film": film}, timeout=10)
    t = d.get("title") if isinstance(d, dict) and "_error" not in d else None
    return t if t and t != film else film


def act_look(args):
    """t 前后三帧竖拼一张（quality：省／标准／高清），或单帧大图（quality=单帧）。只给图；这几秒的字幕弹幕用 where 拿。
    图是后端用 ffmpeg 从片源按第 t 秒截的，不是 TA 的屏幕截图；回话里写明。"""
    fa = str(args.get("film") or "").strip()
    if re.fullmatch(r"(?i)live(?::\d{1,12}(?::[0-9a-z]{1,20})?)?", fa) or fa == "直播":
        return act_look_live(args, fa)
    now, err = current()
    if err and not args.get("film"):
        return f"❌ {err}"
    film = pick_film(args, now)
    t = pick_t(args, now)
    name = film_name(film, now)
    frames = str(args.get("frames") or "").strip()
    q = str(args.get("quality") or "").strip()
    if q == "单帧":
        params = {"film": film, "t": f"{t:.2f}", "w": int(args.get("width") or 960), "peek": "companion", "frames": 1}
    elif q in QUALITY_W:
        params = {"film": film, "t": f"{t:.2f}", "peek": "companion", "frames": 3}
        if QUALITY_W[q]:
            params["w"] = QUALITY_W[q]
    elif frames == "1" or (frames != "3" and args.get("width")):
        params = {"film": film, "t": f"{t:.2f}", "w": int(args.get("width") or 640), "peek": "companion", "frames": 1}
    else:
        params = {"film": film, "t": f"{t:.2f}", "peek": "companion", "frames": 3}
    single = params.get("frames") == 1
    data = api("/frame", params, raw=True)
    if isinstance(data, dict):
        return f"❌ {data.get('_error', '抓帧失败')}"
    if not data or len(data) < 512:
        return "❌ 抓回来的画面是空的（ffmpeg 没输出）"
    os.makedirs(UPLOADS, exist_ok=True)
    path = os.path.join(UPLOADS, f"att-scene-{safe_name(film)}-{int(round(t))}{'' if single else '-3'}.jpg")
    try:
        with open(path, "wb") as f:
            f.write(data)
        os.chmod(path, 0o644)
    except Exception as e:
        return f"❌ 存不下来: {e}"
    src = ("（这张是服务器从片源截的 " + fmt_t(t) + " 那一秒，不是 TA 的屏幕截图"
           + ("。）" if args.get("t") not in (None, "", "当前", "now")
              else "；这一秒取的是播放器最近一次上报的进度，和 TA 眼前可能差几秒。）"))

    if not single:
        lines = [f"《{name}》{fmt_t(max(0, t - 1))} / {fmt_t(t)} / {fmt_t(t + 1)} 三帧（中间粉框那帧是 {fmt_t(t)}，"
                 f"{len(data) // 1024} KB）：", path, src]
        # 字幕和弹幕不在这里重给：发弹幕的播报里已有，单独想看用 where
        return "\n".join(lines)

    lines = [f"《{name}》{fmt_t(t)} 这一帧（{len(data) // 1024} KB）：", path, src]
    cue = api("/sub", {"film": film, "t": f"{t:.2f}", "window": 2})
    cues = cue.get("cues") if "_error" not in cue else None
    if not cues:
        cues = bili_cues(film, t, 2) or []
    if cues:
        lines.append("")
        lines.append("这会儿的字幕：" + " / ".join(c["text"].replace("\n", " ") for c in cues))
    return "\n".join(lines)


_bili_why = {"note": ""}     # bili_cues 拿不到字幕时，后端给的原因


def bili_cues(film, t, window):
    """B 站片子（film=bili:BV… / bili:BV…_p2 / bili:ep…）没有本地字幕文件：去后端 /api/bili/subtitle 拿 CC 字幕，整理成 cues。"""
    if not str(film).startswith("bili:"):
        return None
    m = re.match(r"^bili:(BV[0-9A-Za-z]{10})(?:_p(\d{1,4}))?$", str(film))
    mg = re.match(r"^bili:ep\d{1,12}$", str(film))
    if not m and not mg:
        return None
    bv, p = (m.group(1), int(m.group(2) or 1)) if m else (str(film), 1)
    d = api("/bili/subtitle", {"bvid": bv, "p": p if p > 1 else None, "t": f"{t:.2f}", "window": f"{window:g}"})
    _bili_why["note"] = (d.get("note") or d.get("_error") or "") if isinstance(d, dict) else ""
    if not isinstance(d, dict) or "_error" in d or not d.get("has"):
        return None
    return [{"start": float(c.get("t", 0)), "end": float(c.get("end", c.get("t", 0))), "text": str(c.get("text", ""))}
            for c in (d.get("list") or [])]


def act_sub(args):
    """某部片 t 前后 window 秒的台词（本地字幕文件优先，B 站片子退到 CC 字幕）。"""
    now, err = current()
    if err and not args.get("film"):
        return f"❌ {err}"
    film = pick_film(args, now)
    t = pick_t(args, now)
    w = float(args.get("window") or 20)
    d = api("/sub", {"film": film, "t": f"{t:.2f}", "window": w})
    if "_error" in d and str(film).startswith("bili:"):
        d = {"hasSub": False, "cues": []}
    if "_error" in d:
        return f"❌ {d['_error']}"
    cues = d.get("cues") or []
    if not d.get("hasSub"):
        cues = bili_cues(film, t, w)
        if cues is None and str(film).startswith("bili:"):
            return f"《{film}》B 站没给 CC 字幕" + (f"（{_bili_why['note']}）" if _bili_why["note"] else "（这一 P 没有 CC）") + "。"
        if cues is None:
            return f"《{film}》旁边没有字幕文件（同名 .srt / .vtt 放进片源目录 BILITUBE_FILM_DIR 就认）。"
    if not cues:
        return f"《{film}》{fmt_t(t)} 前后 {int(w)} 秒没人说话。"
    out = [f"《{film}》{fmt_t(t)} 前后 {int(w)} 秒："]
    for c in cues:
        mark = "▸" if c["start"] <= t <= c["end"] + 0.4 else " "
        out.append(f"  {mark} [{fmt_t(c['start'])}] {c['text']}")
    return "\n".join(out)


# ── stage danmaku ──

LIVE_FILM_RE = re.compile(r"(?i)live:(\d{1,12})(?::[0-9a-z]{1,20})?")


def dm_film(args, d):
    """弹幕落在哪：(film, 错误)。推送里的 live:<房间>:<id> 收成 live:<房间>；没给 film 时，
    TA 这会儿（或网络抖之前）开着直播、而且比片子更近，就是那间直播——不悄悄落到上一部片子上。"""
    f = str(args.get("film") or "").strip()
    lv = (d or {}).get("live") or None
    m = LIVE_FILM_RE.fullmatch(f)
    if m:
        return "live:" + m.group(1), None
    if f.lower() == "live" or f == "直播":
        return ("live:%s" % lv["room"], None) if lv else (None, "TA 最近没开过直播间；要回哪间就给 film=live:<房间号>")
    if f:
        return f, None
    film_idle = (d or {}).get("idleMs") if (d or {}).get("film") else None
    if lv and (lv.get("watching") or film_idle is None or lv.get("agoSec", 1e9) * 1000 < film_idle):
        return "live:%s" % lv["room"], None
    if not (d or {}).get("film"):
        return None, "TA 还没在 bilitube 播放页开过片子。"
    return d["film"], None


def act_dm(args):
    """私人弹幕：sub=add 在 t 发一条（只出现在 bilitube 播放页上，不发去 B 站），list 列两人的，del 删自己的一条。"""
    now = api("/now")
    if "_error" in now:
        return f"❌ {now['_error']}"
    film, err = dm_film(args, now)
    if err:
        return f"❌ {err}"
    sub = str(args.get("sub") or ("add" if args.get("text") else "list")).strip()
    if sub == "add":
        text = str(args.get("text") or "").strip()
        if not text:
            return "❌ 弹幕不能是空的"
        is_live = film.startswith("live:")
        t = 0 if is_live else pick_t(args, now)
        d = api("/dm/companion", payload={"film": film, "t": t, "text": text})
        if "_error" in d:
            return f"❌ {d['_error']}" + ("（直播的弹幕没改投别的片子）" if is_live else "")
        r = d.get("danmaku") or {}
        if d.get("live"):
            if d.get("held") is not None:
                return (f"发进直播 {film} 了：{r.get('text')}（id {r.get('id')}）——但 TA 的直播页 {d['held']} 秒没连上，"
                        "多半网络在抖：两分钟内连回来就会飘过，连不回来就只留在这间直播的记录里。")
            return f"发进直播 {film} 了：{r.get('text')}（id {r.get('id')}）——TA 正开着这间，已经出现在 TA 的播放页上。"
        out = f"发在 {fmt_t(r.get('t'))} 了：{r.get('text')}（id {r.get('id')}）"
        if r.get("cut"):
            out += "\n（太长，截到 140 字了）"
        out += ("\nTA 正开着这部：离 TA 当前进度近的马上飘过画面，远的等播到那一秒再飘；进度条上多一个紫色小方块。"
                if d.get("pooled") else "\nTA 之后播到那一秒时会飘过画面；进度条上多一个紫色小方块。")
        return out + "\n不会暂停 TA 的播放器，也不往聊天里推。"
    if sub == "list":
        d = api("/dm/log", {"film": film})
        if "_error" in d:
            return f"❌ {d['_error']}"
        items = d.get("items") or []
        if not items:
            return f"《{d.get('title') or film}》里两个人都还没发过私人弹幕。"
        out = [f"《{d.get('title') or film}》两个人的弹幕 {len(items)} 条："]
        for r in items:
            mark = who("companion" if r.get("author") == "companion" else "user")
            out.append(f"  [{fmt_t(r.get('t'))}] {mark}：{r.get('text')}"
                       + (f"（id {r['id']}）" if r.get("author") == "companion" and r.get("id") else ""))
        return "\n".join(out)
    if sub == "del":
        lid = str(args.get("id") or "").strip()
        if not lid:
            return "❌ 要删哪条？给 id（sub=list 里自己发的那几条后面带着）"
        d = api(f"/dm/log?id={urllib.parse.quote(lid)}&film={urllib.parse.quote(film)}", method="DELETE")
        if "_error" in d:
            return f"❌ {d['_error']}"
        return f"删掉了 {d.get('removed', 0)} 条。"
    return "❌ danmaku 的 sub 只能是 add / list / del"


# ── stage queue / play_now ──

def act_order(args):
    """插播：play=next 排成 TA 正在看的这部后面的「下一个播放」，play=now 立刻切过去。
    只动 TA 开着的播放页，不进任何单子（往 B 站稍后再看里加是 bilitube.py 的 stage queue 自己做的）。不发推荐卡（那是 stage card）。"""
    ref = str(args.get("ref") or args.get("film") or "").strip()
    if not ref:
        return "❌ 要给 BV 号、番剧 ep/ss 号或 B 站链接（分 P 带 ?p=N）"
    m = re.match(r"^(?:bili:)?(BV[0-9A-Za-z]{10})_p(\d{1,4})$", ref)
    if m:
        ref = f"{m.group(1)}?p={m.group(2)}"
    elif ref.startswith("bili:"):
        ref = ref[5:]
    play = str(args.get("play") or "").strip().lower()
    payload = {"ref": ref}
    if play in ("now", "next"):
        payload["play"] = play
    d = api("/bili/add", payload=payload, timeout=40)
    if "_error" in d:
        return f"❌ 没点上：{d['_error']}"
    f = d.get("film") or {}
    fid, p, pages = f.get("id") or "", int(f.get("p") or 1), int(f.get("pages") or 0)
    head = f"《{f.get('title') or fid}》"
    bits = []
    if f.get("duration"):
        bits.append(fmt_cn(f["duration"]))
    if f.get("pgc"):
        g = f["pgc"]
        bits.append(f"{g.get('type_name') or '番剧'}，共 {pages} 集" + (f"，这是{g['index']}" if g.get("index") else ""))
        if f.get("epTitle"):
            bits.append(f["epTitle"])
    elif pages > 1:
        bits.append(f"共 {pages} P，这是 P{p}")
    elif pages == 1:
        bits.append("单 P")
    if f.get("up") and not f.get("pgc"):
        bits.append(f"UP {f['up']}")
    lines = [head + ("（" + " · ".join(bits) + "）" if bits else "")]
    lines.append(f"film={fid}　播放页：{PAGE_URL}?film={urllib.parse.quote(fid, safe=':')}")
    cued = d.get("cued")
    if cued == "now":
        lines.append("▶ 插播了：TA 开着 bilitube 播放页，几秒内就会切到这部开始放（打断了 TA 正在看的）。")
    elif cued == "next":
        un = d.get("upNext") or {}
        lines.append(f"⏭ 排成下一个播放：TA 这部《{un.get('afterTitle') or '当前这部'}》放完自动接上。")
    elif cued == "same":
        lines.append("TA 正在看的就是这部，没动 TA 的播放器。")
    elif cued is None and play in ("now", "next"):
        lines.append("TA 这会儿没开着 bilitube 播放页，" + ("没切过去。" if play == "now" else "没排成下一个。"))
    return "\n".join(lines)


# ── 回话里直接带图（look 那张）──

SHOT_LINE_RE = re.compile(r"^(" + re.escape(UPLOADS) + r"/att-scene-[A-Za-z0-9_.\-]+\.jpe?g)$", re.M)
INLINE_SHOT_MAX = 400 * 1024      # 不超过这么大就原样带；再大就缩（shrink 由调用方给，缩不下就只留路径）


def inline_shot(text, shrink=None):
    """回话里单独一行的那张抓帧 → MCP image 块。太大时用 shrink(path) 缩成 JPEG；读不到、缩不下都回 None（路径还在文字里）。"""
    m = SHOT_LINE_RE.search(text or "")
    if not m:
        return None
    path = m.group(1)
    try:
        if os.path.getsize(path) <= INLINE_SHOT_MAX:
            with open(path, "rb") as f:
                data = f.read()
        else:
            data = shrink(path) if shrink else None
            if not data:
                return None
    except OSError:
        return None
    return {"type": "image", "data": base64.b64encode(data).decode("ascii"), "mimeType": "image/jpeg"}
