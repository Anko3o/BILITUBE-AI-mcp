#!/usr/bin/env python3
"""
bilitube 的 MCP 服务，同时给 bilitube 后端（server/bilitube_server.py）提供 B 站回环 REST。
Streamable HTTP，默认绑 127.0.0.1:18013（只听回环、不设 token，出不了这台机器；端口用 BILITUBE_MCP_PORT 改）。
客户端层在 server/bili.py，护栏在 server/guard.py（风控暂停、写入白名单、每小时预算），
登录凭据默认在 mcp/state/cred.json（BILI_CRED_FILE 可改，0600）。

MCP 工具表是 bilitube.py 的七样（login / find / video / library / watching / stage / account），
「一起看」那一半的逻辑在 watch.py。下面的 t_* 是七样背后的实现，登记在 HANDLERS 里，只在进程内用，不对外列出。
MCP 七样和回环 REST 在同一个进程里，共用一个 BiliClient、一道风控闸、一本预算账。

图片：二维码、封面这些会存进 BILITUBE_IMAGE_DIR，回话里单独一行给出完整路径；
要让聊天前端显示成图片，得由前端（或转发层）认这个路径。
"""
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "server"))
from bili import BiliClient, pgc_ref, pgc_pick_ep  # noqa: E402
import guard  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bilitube  # noqa: E402
bilitube.bind(sys.modules[__name__])

HOST, PORT = "127.0.0.1", int(os.environ.get("BILITUBE_MCP_PORT") or 18013)
# 推荐卡（stage card）发到哪：一个接收 JSON 的 HTTP 地址，由你自己的聊天后端提供。不配就不发卡。
CARD_URL = os.environ.get("BILITUBE_CARD_URL", "").strip()
CARD_TOKEN = os.environ.get("BILITUBE_CARD_TOKEN", "").strip()            # 有就放进 Authorization: Bearer
CARD_TOKEN_FILE = os.environ.get("BILITUBE_CARD_TOKEN_FILE", "").strip()  # 或者从文件读（只读第一行）
PROTOCOL = "2024-11-05"
INSTRUCTIONS = bilitube.INSTRUCTIONS


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class ToolError(Exception):
    pass


CLIENT = BiliClient()
LOGIN = {"auth_code": None, "png": None, "status": "idle", "result": None, "since": 0}
LOGIN_LOCK = threading.Lock()


def _bg_poll(auth_code, timeout=180):
    r = CLIENT.login_wait(auth_code, timeout=timeout)
    with LOGIN_LOCK:
        if LOGIN["auth_code"] == auth_code:
            LOGIN["status"] = r.get("status", "error")
            LOGIN["result"] = r
    log(f"bili login 后台轮询结束: {r.get('status')}")


def _check(d):
    """把客户端的 {"error":..} 抛成 ToolError。暂停 / 拒写 / 预算这几种原话就是人话，不加 code。"""
    if isinstance(d, dict) and d.get("error"):
        if d["error"] == "not_logged_in":
            raise ToolError(f"B 站没登录或登录失效了。等{names()['user']}在场时用 login(action=\"qr\") 出二维码给 TA 扫。")
        if d.get("code") in (guard.Paused.code, guard.Blocked.code, guard.OverBudget.code, "follow_denied"):
            raise ToolError(d["error"])
        raise ToolError(f"{d['error']}（code={d.get('code')}）")
    return d


# ── 回话里直接带图 ─────────────────────────────────────────
# 只在「调用就是为了看图」的地方带：video info 明确给了 cover=true 的封面（watching look 的抓帧见 watch.py）。
# 二维码、推荐卡是给用户看的，列表里的多张封面也不带，这些只给路径。
# 文字和路径原样保留（路径单独一行写进回复，聊天页才显示图）；图另作 MCP image 块追加在后面。
# 小图（≤ INLINE_RAW_MAX 且长边 ≤ INLINE_EDGE）原样带；否则用 ffmpeg 在内存里缩成 JPEG
# （长边 INLINE_EDGE，质量逐档降，到 INLINE_TARGET 以内为止），磁盘上的原图不动；缩不下来就不带。
# 和 watch.py 的 inline_shot(text) 同名同约定：给回话文字，回 image 块或 None。
INLINE_LINE_RE = re.compile(r"^封面：(/\S+/att-bili-cover-[\w-]+\.(?:jpe?g|png|webp))\s*$", re.M)
INLINE_RAW_MAX = 200 * 1024     # 原样带的文件上限（字节）
INLINE_EDGE = 1000              # 长边上限（像素），超过就缩
INLINE_TARGET = 200 * 1024      # 缩完的目标上限（字节），超过就不带
INLINE_QUALITIES = (4, 7, 11)   # ffmpeg mjpeg 的 -q:v，从好到省逐档试


def _img_mime(head):
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def _img_size(path):
    """(宽, 高)；ffprobe 认不出就 None。"""
    import subprocess
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height", "-of", "csv=p=0", path],
                           capture_output=True, text=True, timeout=10)
        w, h = (int(x) for x in r.stdout.strip().split(",")[:2])
        return (w, h) if w > 0 and h > 0 else None
    except Exception:
        return None


def _shrink_jpeg(path):
    """在内存里缩成长边 ≤ INLINE_EDGE 的 JPEG，逐档降质量直到 ≤ INLINE_TARGET；不行回 None。"""
    import subprocess
    e = INLINE_EDGE
    vf = f"scale='if(gte(iw,ih),min(iw,{e}),-2)':'if(gte(iw,ih),-2,min(ih,{e}))'"
    for q in INLINE_QUALITIES:
        try:
            r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-frames:v", "1", "-vf", vf,
                                "-pix_fmt", "yuvj420p", "-q:v", str(q), "-f", "image2pipe", "-c:v", "mjpeg", "-"],
                               capture_output=True, timeout=20)
        except Exception:
            return None
        if r.returncode != 0 or _img_mime(r.stdout[:12]) != "image/jpeg":
            return None
        if len(r.stdout) <= INLINE_TARGET:
            return r.stdout
    return None


def inline_image(path):
    """一张本机图 → MCP image 块（小的原样、大的缩过）；读不到、认不出、缩不下来都回 None。"""
    import base64
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(12)
    except OSError:
        return None
    mime, dims = _img_mime(head), _img_size(path)
    if not mime or not dims:
        return None
    if size <= INLINE_RAW_MAX and max(dims) <= INLINE_EDGE:
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return None
    else:
        data, mime = _shrink_jpeg(path), "image/jpeg"
        if not data:
            return None
    return {"type": "image", "data": base64.b64encode(data).decode("ascii"), "mimeType": mime}


def inline_shot(text):
    """回话里单独一行「封面：<路径>」的那张图 → image 块；没有就 None。"""
    m = INLINE_LINE_RE.search(text or "")
    return inline_image(m.group(1)) if m else None


def _cut(t, n):
    t = str(t or "").replace("\n", " ").strip()
    return t if len(t) <= n else t[:n] + "…"


def _slim(v):
    """瘦身行：标题（≤40 字）、播放、时长、BV。"""
    line = _cut(v.get("title"), 40)
    if v.get("play") is not None:
        line += f"  播放{_n(v['play'])}"
    if v.get("duration"):
        line += f"  {v['duration']}"
    if v.get("bvid"):
        line += f"  {v['bvid']}"
    return line


def _n(x):
    """数字带万。"""
    try:
        x = int(x)
    except Exception:
        return str(x)
    return f"{x/10000:.1f}万" if x >= 10000 else str(x)


# 两个人的称呼：直接读 bilitube 的设置文件（设置里填的），缓存 5 秒；没填就用中性默认（和 bilitube_server.py 的 DEFAULT_* 是同一对）
PREFS_FILE = os.environ.get("BILITUBE_PREFS_FILE") or os.path.join(
    os.environ.get("BILITUBE_DATA_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "server"),
    "prefs.json")
_NAMES_DEFAULT = {"user": "TA", "companion": "AI"}
_NAMES = {"t": 0.0, "d": None}


def names():
    if _NAMES["d"] is None or time.time() - _NAMES["t"] > 5:
        try:
            with open(PREFS_FILE, encoding="utf-8") as f:
                p = json.load(f) or {}
        except Exception:
            p = {}
        _NAMES["d"] = {"user": str(p.get("userName") or "").strip() or _NAMES_DEFAULT["user"],
                       "companion": str(p.get("companionName") or "").strip() or _NAMES_DEFAULT["companion"]}
        _NAMES["t"] = time.time()
    return _NAMES["d"]


# ── 推荐卡：发给你自己的聊天后端 ──────
# POST BILITUBE_CARD_URL  {"type": "video", "text": 配文, "video": {bvid,title,up,duration,view,danmaku,cover,cover_path,url}}
# 令牌只进请求头，不进日志、不进返回。接收方怎么画卡、点开去哪，由接收方决定。

def _card_token():
    if CARD_TOKEN:
        return CARD_TOKEN
    if CARD_TOKEN_FILE:
        with open(CARD_TOKEN_FILE, encoding="utf-8") as f:
            return (f.readline() or "").strip()
    return ""


def relay_card(kind, text, extra):
    if not CARD_URL:
        raise RuntimeError("没配置 BILITUBE_CARD_URL（接收推荐卡的聊天后端地址），卡没地方发")
    body = {"type": kind, "text": text or ""}
    body.update(extra)
    hdr = {"Content-Type": "application/json"}
    tok = _card_token()
    if tok:
        hdr["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request(CARD_URL, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers=hdr, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read()
    try:
        return json.loads(raw or b"{}")
    except Exception:
        return {}


def t_share(args):
    """查视频 → 推荐卡推给聊天后端（见 relay_card）。cover 是 B 站封面地址。"""
    v = _check(CLIENT.video(args["id"]))
    note = str(args.get("note") or "").strip()
    # 分 P：id 里带 ?p=N / _pN 就把 P 写进卡片 url；接收方可以据此把「点开」指到 bilitube 播放页的 ?film=bili:BV…_pN
    m = re.search(r"(?:[?&]p=|_p)(\d{1,4})\b", str(args["id"]))
    p = int(m.group(1)) if m else 1
    url = v["url"] + (f"?p={p}" if p > 1 else "")
    card = {"bvid": v["bvid"], "title": v["title"], "up": v.get("up") or "", "duration": v.get("duration") or "",
            "view": v.get("view") or 0, "danmaku": v.get("danmaku") or 0,
            # cover 给 B 站封面地址（https）；同一台机器上的接收方想直接读文件，用 cover_path（BILITUBE_IMAGE_DIR 里缓存的那张）
            "cover": str(v.get("cover_url") or "").replace("http://", "https://", 1), "cover_path": v.get("cover_path") or "",
            "url": url}
    try:
        res = relay_card("video", note, {"video": card})
    except Exception as e:
        raise ToolError(f"视频卡没发出去: {e}")
    return f"视频卡已发" + (f"（消息 #{res.get('id')}）" if isinstance(res, dict) and res.get("id") else "") + f"：{v['title']} — UP {v.get('up') or '-'} · {v.get('duration')} · {url}"


# ── 工具 ───────────────────────────────────────────────────

def t_login(args):
    with LOGIN_LOCK:
        cur = dict(LOGIN)
    if args.get("auth_code") or (args.get("wait") and cur["auth_code"]):
        code = args.get("auth_code") or cur["auth_code"]
        r = CLIENT.login_wait(code, timeout=int(args.get("timeout") or 120))
        with LOGIN_LOCK:
            if LOGIN["auth_code"] == code:
                LOGIN["status"], LOGIN["result"] = r.get("status"), r
        if r.get("status") == "ok":
            return f"登录成功：{r.get('uname')}（mid {r.get('mid')}）"
        raise ToolError(f"没等到：{r.get('status')} {r.get('error') or ''}".strip())
    # 已登录且有效就不重复出码
    st = CLIENT.status()
    if st.get("logged_in") and st.get("valid") and not args.get("force"):
        return f"已经登录着：{st.get('uname')}（mid {st.get('mid')}），token 到 {st.get('token_expires_at')}。要重新扫传 force=true。"
    q = _check(CLIENT.login_qr())
    with LOGIN_LOCK:
        LOGIN.update({"auth_code": q["auth_code"], "png": q["png"], "status": "waiting",
                      "result": None, "since": int(time.time())})
    threading.Thread(target=_bg_poll, args=(q["auth_code"],), daemon=True).start()
    return (f"二维码在 {q['png']}\n"
            "B 站 App 扫一下、点确认。后台等 3 分钟，扫完用 login(action=\"status\") 看结果（或 wait=true 原地等）。")


def t_status(args):
    with guard.lane(None):                 # 看状态不算进「自己刷」的预算
        st = CLIENT.status()
    with LOGIN_LOCK:
        lg = dict(LOGIN)
    lines = []
    if st.get("paused"):
        lines.append("暂停中：" + guard.human_pause(st["paused"]))
    if st.get("logged_in") and st.get("paused"):
        lines.append(f"登录：{st.get('uname')}（mid {st.get('mid')}），暂停着所以没去问 B 站有没有失效")
    elif st.get("logged_in"):
        lines.append(f"登录：{st.get('uname')}（mid {st.get('mid')}）"
                     + ("，有效" if st.get("valid") else f"，但 B 站说登录无效：{st.get('error')}"))
        if st.get("valid"):
            lines.append(f"等级 {st.get('level')}，硬币 {st.get('coins')}，大会员 {'是' if st.get('vip') else '否'}")
        lines.append(f"token 到期 {st.get('token_expires_at')}")
    else:
        lines.append("未登录。用 login(action=\"qr\") 出二维码。")
    if not st.get("paused"):
        lines.append("风控：没暂停")
    bs = guard.budget_status()
    b, w, lt, fo = bs["mcp"], bs["write"], bs["later"], bs["follow"]
    lines.append(f"这一小时{names()['companion']}这边用了 {b['used_last_hour']} 次请求"
                 + (f"（上限 {b['limit']}）" if b["limit"] else "（不限）")
                 + f"，点赞＋收藏＋关注＋B 站稍后再看 {w['used_last_hour']} 次" + (f"（上限 {w['limit']}）" if w["limit"] else "")
                 + f"，其中往 B 站稍后再看里加 {lt['used_last_hour']} 次" + (f"（上限 {lt['limit']}）" if lt["limit"] else "")
                 + f"、关注＋取关 {fo['used_last_hour']} 次" + (f"（上限 {fo['limit']}）" if fo["limit"] else ""))
    pk = guard.pocket_id()
    lines.append(f"口袋（{names()['companion']}专用的私密收藏夹）：{pk}" if pk
                 else f"口袋（{names()['companion']}专用的私密收藏夹）：没配，{names()['companion']}自己收不了")
    gid, gname = guard.follow_group()
    lines.append(f"关注分组：{gid}" if gid else f"关注分组：按名字认「{gname}」" if gname
                 else f"关注分组：没配（{names()['companion']}自己关注不了）")
    if lg["auth_code"] and time.time() - lg["since"] < 600:
        lines.append(f"扫码进度：{lg['status']}" + (f" {lg['result']}" if lg["result"] and lg["status"] != "ok" else ""))
    return "\n".join(lines)


def _pgc_lines(v, full):
    """番剧的一集：番名（类型，共几集），这一集，数据，简介截断。"""
    g = v["pgc"]
    head = f"{g['season_title']}（{g['type_name']}，共 {g['total']} 集{'，已完结' if g.get('is_finish') else ''}）"
    ep = " ".join(x for x in (g.get("index"), g.get("long_title")) if x) or "正片"
    tags = [x for x in (g.get("badge"), "地区受限" if g.get("area_limited") else "") if x]
    lines = [head, f"这一集：{ep} · {v['duration']} · ep{g['ep_id']}" + (f" · {'/'.join(tags)}" if tags else ""),
             f"播放 {_n(v['view'])} 弹幕 {_n(v['danmaku'])} · ss{g['season_id']} · {v['bvid']}", g["url"]]
    desc = (v.get("desc") or "").replace("\n", " ")
    if desc:
        lines.append("简介：" + (desc if full or len(desc) <= 120 else desc[:120] + "…"))
    if v.get("cover"):
        lines.append(f"封面：{v['cover']}")
    return "\n".join(lines)


def t_video(args):
    full = bool(args.get("full"))
    v = _check(CLIENT.video(args["id"], cover=bool(args.get("cover")), full_desc=full))
    if v.get("pgc"):
        return _pgc_lines(v, full)
    lines = [f"{v['title']}",
             f"UP {v['up']}（mid {v['up_mid']}） · {v['duration']} · {v['pubdate']} · 分区 {v.get('tname') or '-'}",
             f"播放 {_n(v['view'])} 赞 {_n(v['like'])} 币 {_n(v['coin'])} 藏 {_n(v['fav'])} 弹幕 {_n(v['danmaku'])} 评论 {_n(v['reply'])}",
             f"{v['bvid']} / av{v['aid']} / cid {v['cid']}" + (f" / {v['pages']}P" if v.get("pages", 1) > 1 else ""),
             v["url"]]
    if v.get("desc"):
        desc = v["desc"].replace("\n", " ")
        if full or len(desc) <= 120:
            lines.append("简介：" + desc)
        else:
            lines.append(f"简介：{desc[:120]}…（简介还有 {max(len(desc), int(v.get('desc_len') or 0)) - 120} 字，full=true 看全）")
    if v.get("cover"):
        lines.append(f"封面：{v['cover']}")
    return "\n".join(lines)


def _fmt_c(c, i):
    head = f"{i}. {c['uname']}  赞{_n(c['like'])}  {c['ctime']}  rpid={c['rpid']}"
    if c.get("top"):
        head += "  [置顶]"
    if c.get("reply_count"):
        head += f"  ({c['reply_count']} 条回复)"
    body = "   " + (c["message"] or "").replace("\n", " ")[:400]
    subs = ["     └ " + f"{s['uname']}: {(s['message'] or '').replace(chr(10), ' ')[:120]}" for s in c.get("sub") or []]
    return "\n".join([head, body] + subs)


def t_comments(args):
    r = _check(CLIENT.comments(args["id"], args.get("sort") or "hot", int(args.get("page") or 1),
                               int(args.get("ps") or 20), args.get("next_offset")))
    if not args.get("detail"):
        # 瘦身：前 n 条（默认 5），每条 80 字，带赞数，不带楼中楼和置顶
        n = max(1, int(args.get("n") or 5))
        tops = [c for c in r["list"] if c.get("top")]
        rows = [c for c in r["list"] if not c.get("top")][:n]
        sort = "最热" if r["sort"] == "hot" else "最新"
        out = [f"评论区 av{r['aid']} · {sort} · 共 {r.get('total') or '?'} 条，这里 {len(rows)} 条"
               + (f"（另有 {len(tops)} 条置顶没列）" if tops else "")]
        for c in rows:
            line = f"赞{_n(c['like'])} {c['uname']}：{_cut(c['message'], 80)}"
            if c.get("reply_count"):
                line += f"（{c['reply_count']} 回复 rpid={c['rpid']}）"
            out.append(line)
        if not rows:
            out.append("（这页没有评论）")
        if r.get("next_offset") and not r.get("is_end"):
            out.append(f"下一页：next_offset={r['next_offset']}")
        return "\n".join(out)
    head = f"评论区 av{r['aid']} · {r['sort']} · 第 {r['page']} 页 · 共 {r.get('total') or '?'} 条"
    if r.get("note"):
        head += f"（{r['note']}）"
    out = [head] + [_fmt_c(c, i + 1) for i, c in enumerate(r["list"])]
    if r.get("next_offset") and not r.get("is_end"):
        out.append(f"下一页：next_offset={r['next_offset']}")
    if not r["list"]:
        out.append("（这页没有评论）")
    return "\n".join(out)


def t_replies(args):
    r = _check(CLIENT.replies(args["id"], int(args["rpid"]), int(args.get("page") or 1)))
    out = []
    if r.get("root"):
        out.append("楼主：" + _fmt_c(r["root"], 0).split("\n", 1)[1].strip())
    out.append(f"楼中楼 共 {r.get('total')} 条 · 第 {r['page']} 页")
    out += [_fmt_c(c, i + 1) for i, c in enumerate(r["list"])]
    return "\n".join(out)


def t_like(args):
    """点赞 / 取消点赞。投币不开放：带了 coin 也不投，回话里说明。"""
    on = args.get("on", True)
    r = _check(CLIENT.like(args["id"], on=bool(on)))
    msg = f"{'点赞' if on else '取消点赞'} av{r['aid']} 成功"
    if args.get("coin"):
        msg += "；投币没开放，没投"
    return msg


def t_fav(args):
    if args.get("folders") or not args.get("id"):
        r = _check(CLIENT.fav_folders())
        pk = guard.pocket_id()
        return "收藏夹：\n" + "\n".join(f"  {f['id']}  {f['title']}（{f['count']}）"
                                        + (f"  ← {names()['companion']}的口袋" if pk and f["id"] == pk else "") for f in r["list"])
    by = "user" if str(args.get("by") or "companion").lower() == "user" else "companion"
    r = _check(CLIENT.fav(args["id"], args.get("folder_id"), remove=bool(args.get("remove")), by=by))
    where = f"口袋「{r.get('folder_title') or r['folder_id']}」" if by == "companion" else f"TA 的收藏夹 {r['folder_id']}"
    if r.get("already"):
        return f"av{r['aid']} {'本来就不在' if r['removed'] else '已经在'}{where}里了，没再发请求"
    return f"{'移出' if r['removed'] else '收进'}{where}：av{r['aid']}"


def t_follow(args):
    """自己关注 / 取关一个 UP（只进 / 只取关专属关注分组，规矩在 BiliClient.follow）。"""
    r = _check(CLIENT.follow(args["mid"], remove=bool(args.get("remove"))))
    who = (f"{args['name']}（mid {r['mid']}）" if args.get("name") else f"mid {r['mid']}")
    if r.get("already"):
        return f"{who} {'本来就没关注' if r['removed'] else '已经在「' + r['group'] + '」里了'}，没再发请求"
    if r["removed"]:
        return f"取关了 {who}（「{r['group']}」里的）"
    if r.get("warn"):
        return f"{who}：{r['warn']}"
    return f"关注了 {who}，放进了「{r['group']}」"


def t_follows(args):
    """自己关注了谁：专属关注分组里的人，一行一个（名字＋mid）。"""
    n, page = max(1, min(50, int(args.get("n") or 20))), max(1, int(args.get("page") or 1))
    r = CLIENT.follow_list(n, page)
    if isinstance(r, dict) and r.get("code") == "follow_denied":
        return f"{names()['companion']}还没有关注分组：{r['error']}"
    r = _check(r)
    g = r["group"]
    if not r["list"]:
        return f"「{g['name']}」里还没有人。" if page == 1 else f"「{g['name']}」第 {page} 页是空的。"
    out = [f"{names()['companion']}关注的 UP（「{g['name']}」，共 {r['total']} 个）："]
    out += [f"{i}. {x['uname']}  mid {x['mid']}" for i, x in enumerate(r["list"], (page - 1) * n + 1)]
    if r.get("has_more"):
        out.append(f"还有，page={page + 1}")
    return "\n".join(out)


def t_history(args):
    r = _check(CLIENT.history(int(args.get("n") or 20)))
    out = [f"TA 最近看的 {r['count']} 条："]
    for i, h in enumerate(r["list"], 1):
        tag = {"archive": "视频", "pgc": "番剧", "live": "直播", "article": "专栏"}.get(h["type"], h["type"])
        line = f"{i}. [{tag}] {h['title']}"
        if h.get("episode"):
            line += f" · {h['episode']}"
        if h.get("part"):
            line += f" · P:{h['part']}"
        line += f"  UP {h.get('up') or '-'}"
        if h.get("progress"):
            line += f"  进度 {h['progress']}"
        line += f"  {h['view_at']}"
        if h.get("bvid"):
            line += f"  {h['bvid']}"
        out.append(line)
    return "\n".join(out)


def t_danmaku(args):
    r = _check(CLIENT.danmaku(args["id"], args.get("t", 0), float(args.get("window") or 3), int(args.get("limit") or 30)))
    head = f"{r['bvid']} 在 {int(r['t'])//60}:{int(r['t'])%60:02d} 前后 {r['window']:g} 秒的弹幕：{r['total_in_window']} 条"
    if not r["lines"]:
        return head + "（这几秒没人说话）"
    return "\n".join([head] + [f"  {l['text']}" + (f"  ×{l['count']}" if l["count"] > 1 else "") for l in r["lines"]])


def t_search(args):
    r = _check(CLIENT.search(args["q"], int(args.get("n") or 8)))
    if not args.get("detail"):
        return "\n".join([f"搜「{r['keyword']}」（约 {r.get('total')} 条）："]
                         + [f"{i}. {_slim(v)}" for i, v in enumerate(r["list"], 1)])
    out = [f"搜「{r['keyword']}」（约 {r.get('total')} 条）："]
    for i, v in enumerate(r["list"], 1):
        out.append(f"{i}. {v['title']}  UP {v['up']}  播放{_n(v['play'])}  {v['duration']}  {v['pubdate']}  {v['bvid']}")
    return "\n".join(out)


def _rows(title, rows, n=None, detail=True):
    if not detail:
        return "\n".join([title] + [f"{i}. {_slim(v)}" for i, v in enumerate(rows, 1)])
    out = [title]
    for i, v in enumerate(rows, 1):
        line = f"{i}. {v.get('title')}  UP {v.get('up') or '-'}"
        if v.get("duration"):
            line += f"  {v['duration']}"
        if v.get("play") is not None:
            line += f"  播放{_n(v['play'])}"
        if v.get("progress"):
            line += f"  进度 {v['progress']}"
        if v.get("pubdate"):
            line += f"  {v['pubdate']}"
        if v.get("bvid"):
            line += f"  {v['bvid']}"
        out.append(line)
    return "\n".join(out)


def t_watchlater(args):
    act = str(args.get("action") or "list").lower()
    if act in ("add", "remove", "del"):
        if not args.get("id"):
            raise ToolError("要加/删哪个视频？给 BV 号")
        r = _check(CLIENT.watch_later_set(args["id"], remove=act != "add"))
        return f"{'移出' if r['removed'] else '加进'} B 站稍后再看：av{r['aid']}"
    r = _check(CLIENT.watch_later(int(args.get("n") or 20)))
    if not r["list"]:
        return "B 站稍后再看是空的。"
    return _rows(f"TA 的 B 站稍后再看（{r['count']}/{r.get('total')} 条）：", r["list"])


def t_feed(args):
    kind = str(args.get("kind") or "dynamic").lower()
    n = int(args.get("limit") or args.get("n") or 12)
    detail = bool(args.get("detail"))
    if kind == "live":
        r = _check(CLIENT.live_following(n))
        if not r["list"]:
            return "TA 关注的人现在没人在直播。"
        if not detail:
            return "\n".join([f"正在直播的（{r['count']} 个）："] +
                             [f"{i}. {x['uname']}：{_cut(x.get('title'), 40)}  房间 {x.get('room')}"
                              for i, x in enumerate(r["list"], 1)])
        return "\n".join([f"正在直播的（{r['count']} 个）："] +
                         [f"{i}. {x['uname']}　{x.get('title') or ''}　{x.get('area') or ''}　房间 {x.get('room')}"
                          for i, x in enumerate(r["list"], 1)])
    if kind == "recommend":
        r = _check(CLIENT.recommend(n, int(args.get("fresh") or 1)))
        return _rows(f"首页推荐（{r['count']} 条）：", r["list"], detail=detail)
    if kind == "popular":
        r = _check(CLIENT.popular(n, int(args.get("page") or 1)))
        return _rows(f"热门榜第 {r['page']} 页（{r['count']} 条）：", r["list"], detail=detail)
    r = _check(CLIENT.dynamic_videos(n, args.get("host_mid")))
    if not r["list"]:
        return "关注的 UP 最近没有新投稿。"
    return _rows(f"关注的 UP 新投稿（{r['count']} 条）：", r["list"], detail=detail)


def t_subtitle(args):
    r = _check(CLIENT.subtitle(args["id"], args.get("t"), float(args.get("window") or 6), p=args.get("p")))
    tag = r["bvid"] + (f" P{r['p']}" if (r.get("p") or 1) > 1 else "")
    if r.get("mismatch"):
        return f"{tag}：{r.get('note')}。"
    if not r.get("has"):
        return f"{tag} 这部没有 CC 字幕。"
    head = f"{tag} 的字幕（{r.get('lan')}，全片 {r.get('total')} 句"
    head += f"，这一段 {r['count']} 句）：" if args.get("t") is not None else f"，取了 {r['count']} 句）："
    if not r["list"]:
        return head + "（这几秒没有台词）"
    return "\n".join([head] + [f"  {c['at']}  {c['text']}" for c in r["list"]])


def t_my_replies(args):
    r = _check(CLIENT.my_replies(int(args.get("limit") or args.get("n") or 20)))
    if not r["list"]:
        return "TA 的投稿和评论底下没有新回复。"
    out = [f"{names()['user']}收到的回复（{r['count']} 条）："]
    for i, x in enumerate(r["list"], 1):
        out.append(f"{i}. {x['who']}　「{x['said']}」　（在《{x['on']}》下）　{x['when']}")
    return "\n".join(out)


BILITUBE = os.environ.get("BILITUBE_SERVER_URL", "http://127.0.0.1:18008").rstrip("/")


def t_danmaku_say(args):
    """AI 在 bilitube 播放页上当场发一条弹幕。不发去 B 站，只有 TA 看得见。"""
    text = (args.get("text") or "").strip()
    if not text:
        raise ToolError("要给 text（弹幕内容）")
    body = json.dumps({"text": text[:80], "author": "companion",
                       "color": args.get("color") or None,
                       "mode": "top" if args.get("mode") == "top" else "scroll"}).encode()
    req = urllib.request.Request(BILITUBE + "/api/dm/say", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read().decode("utf-8", "replace") or "{}")
    except Exception as e:
        raise ToolError(f"连不上 bilitube 后端（{BILITUBE}，在跑吗？）: {e}")
    if not d.get("ok"):
        return d.get("error") or "没发出去"
    return f"发了：{text[:80]}（只出现在 bilitube 播放页上，没发去 B 站）"


def t_post_comment(args):
    """评论不开放。留着这个实现只为被调到时说清原因；传输层白名单也会拒 reply/add。"""
    raise ToolError(CLIENT.post_comment(args.get("id"), args.get("text"))["error"])


TOOLS = bilitube.TOOLS          # 对外只有七样；下面 HANDLERS 是背后的实现，只在进程内用

HANDLERS = {"bili_login": t_login, "bili_status": t_status, "bili_video": t_video,
            "bili_comments": t_comments, "bili_replies": t_replies, "bili_like": t_like,
            "bili_fav": t_fav, "bili_history": t_history, "bili_search": t_search, "bili_danmaku": t_danmaku,
            "bili_post_comment": t_post_comment, "bili_share": t_share,
            "bili_watchlater": t_watchlater, "bili_feed": t_feed,
            "bili_subtitle": t_subtitle, "bili_my_replies": t_my_replies,
            "bili_danmaku_say": t_danmaku_say, "bili_follow": t_follow, "bili_follows": t_follows}
# 退出登录不在这里、也不在七样里：只有 bilitube 页面上人点「退出」，后端走回环 POST /logout（w_logout）。
# bili_post_comment 不对外，只为被调到时说清「不开放」（传输层也拒 reply/add）。
# 关注：只有 AI 自己那一路（account follow / unfollow → t_follow → BiliClient.follow），进专属关注分组、只取关分组里的人。
# 下面这几样不碰 B 站，暂停着也照常用。
NO_BILI = {"bili_status", "bili_danmaku_say", "bili_post_comment"}


def gated(fn, args, touches_bili=True):
    """MCP 这边每个动作都从这儿过：碰 B 站的先看暂停闸；请求记在 mcp 通道的预算上。"""
    if touches_bili:
        p = guard.read_pause()
        if p:
            raise ToolError(guard.human_pause(p))
    try:
        with guard.lane("mcp"):
            return fn(args or {})
    except guard.BiliStop as e:
        raise ToolError(e.human)
    except (ValueError, KeyError, TypeError) as e:
        raise ToolError(f"参数不对: {e}")


def call(name, args):
    """进程内按实现名调（测试和命令行用）；MCP 对外走 bilitube.call。"""
    fn = HANDLERS.get(name)
    if not fn:
        raise ToolError(f"没有这个实现: {name}")
    return gated(fn, args, touches_bili=name not in NO_BILI)


# ── 回环 REST（给同机的聊天后端 / bilitube 后端用，浏览器和外网都到不了这儿）────────
# GET /video?id=            BiliClient.video() 原样（聊天后端想画视频卡可以用）
# bilitube 后端放 B 站片子用：
# GET /cid?bvid=&p=         分 P 列表 + 第 p 页的 cid（p 不给就全列）
# GET /playurl?bvid=&cid=&qn=   渐进式 mp4 地址（html5 平台，最高 720p），bilitube_server.py 拿去代理 CDN
# GET /danmaku_all?cid=&duration=   整部片的弹幕 [{t,mode,color,size,text,weight}]
# GET /comments?bvid=&pages=2       热评前 N 页（每页 20 条），楼中楼各带前 3 条

def r_video(q):
    return CLIENT.video(q["id"])


def r_cid(q):
    r = CLIENT.pages(q["bvid"])
    if "error" in r:
        return r
    p = q.get("p")
    if p:
        p = int(p)
        pg = next((x for x in r["pages"] if x["p"] == p), None)
        if not pg:
            return {"error": f"没有第 {p} P（共 {len(r['pages'])} P）", "code": -1}
        r["p"], r["cid"], r["part"] = p, pg["cid"], pg["part"]
        r["duration"] = pg.get("duration") or r.get("duration")
    return r


def r_playurl(q):
    return CLIENT.playurl(q["bvid"], int(q["cid"]), int(q.get("qn") or 80))


def r_danmaku_all(q):
    dur = q.get("duration")
    return CLIENT.danmaku_all(int(q["cid"]), float(dur) if dur else None)


def r_comments(q):
    pages = max(1, min(5, int(q.get("pages") or 2)))
    out, nxt, total = [], None, None
    for i in range(pages):
        r = CLIENT.comments(q["bvid"], "hot", 1, 20, nxt)
        if "error" in r:
            if out:
                break
            return r
        total = r.get("total")
        out += r["list"]
        nxt = r.get("next_offset")
        if r.get("is_end") or not nxt:
            break
        time.sleep(0.3)
    return {"bvid": q["bvid"], "total": total, "count": len(out), "list": out}


# 播放页上的 B 站列表（搜索 / 收藏夹 / 最近看过）＋ 一键点赞和收藏。
# 这些只回环给 bilitube 后端（bilitube_server.py），由它挂到 /bilitube/api/bili/* 的门禁后面给播放页和逛片页用。
# GET  /search?q=&n=          搜视频（最多 20）
# GET  /fav_folders           TA 的收藏夹清单
# GET  /fav_list?media_id=&n= 某个夹子里的视频（不给 media_id 就第一个夹子）
# GET  /history?n=            TA 最近看过（最多 20）
# GET  /me                    登没登录（只回 logged_in / uname，别的不吐）
# POST /logout                退出登录（只给后端用，页面上人点「退出」才来；见 w_logout）
# GET  /follow_group          AI 自己关注的 UP（专属关注分组；见 r_follow_group）
# POST /like  {id,on}         点赞 / 取消
# POST /fav   {id,media_id,remove}  收藏 / 取消收藏

def r_search(q):
    kw = (q.get("q") or q.get("keyword") or "").strip()
    if not kw:
        return {"error": "要搜什么呢", "code": -1}
    return CLIENT.search(kw, max(1, min(20, int(q.get("n") or 20))), int(q.get("page") or 1))


def r_fav_folders(q):
    return CLIENT.fav_folders()


def r_fav_list(q):
    mid = q.get("media_id")
    return CLIENT.fav_list(int(mid) if mid else None, max(1, min(20, int(q.get("n") or 20))),
                           int(q.get("page") or 1))


def r_history(q):
    cur = {k: q[k] for k in ("max", "view_at", "business") if q.get(k)}
    return CLIENT.history(max(1, min(20, int(q.get("n") or 20))), cur or None)


def r_me(q):
    """登没登录，外加 TA 的头像地址和 mid（页面上「我的」一栏用 face+mid 经 /bilitube/api/bili/face 代理拿头像）。"""
    st = CLIENT.status()
    if "error" in st:
        return st
    return {"logged_in": bool(st.get("logged_in")), "uname": st.get("uname") or "",
            "face": st.get("face") or "", "mid": st.get("mid")}


def w_like(b):
    """用户在 bilitube 页面上自己按的点赞 / 取消点赞。"""
    vid = str(b.get("id") or b.get("bvid") or "").strip()
    if not vid:
        return {"error": "没说给哪个视频点赞", "code": -1}
    on = b.get("on")
    return CLIENT.like(vid, True if on is None else bool(on))


def w_fav(b):
    vid = str(b.get("id") or b.get("bvid") or "").strip()
    if not vid:
        return {"error": "没说收藏哪个视频", "code": -1}
    mid = b.get("media_id")
    return CLIENT.fav(vid, int(mid) if mid else None, bool(b.get("remove")))


# B 站首页那几类列表，同样给页面用：
# GET /watchlater?n=      稍后再看          POST /watchlater {id,remove}
# GET /dynamic?n=&host_mid=   关注的 UP 新投稿
# GET /live_following?n=  关注的人谁在直播   GET /recommend?n=&fresh=   GET /popular?n=&page=
# GET /subtitle?bvid=&t=&window=   CC 字幕（没有就回 has:false，不算错）
# GET /my_replies?n=      TA 收到的新回复（只读）

def r_watchlater(q):
    return CLIENT.watch_later(max(1, min(30, int(q.get("n") or 20))), int(q.get("offset") or 0))


def r_dynamic(q):
    mid = q.get("host_mid")
    return CLIENT.dynamic_videos(max(1, min(30, int(q.get("n") or 20))), int(mid) if mid else None,
                                 q.get("offset") or None, int(q.get("page") or 1))


def r_live_following(q):
    return CLIENT.live_following(max(1, min(30, int(q.get("n") or 20))))


# GET /live_danmu?room=<真房间号>  直播弹幕长连接的入场券（token＋主机；只读、匿名，后端只在 TA 开着这间直播时来问）
def r_live_danmu(q):
    return CLIENT.live_danmu_info(int(q.get("room") or 0))


def r_recommend(q):
    return CLIENT.recommend(max(1, min(30, int(q.get("n") or 12))), int(q.get("fresh") or 1))


def r_popular(q):
    return CLIENT.popular(max(1, min(20, int(q.get("n") or 20))), int(q.get("page") or 1))


def r_subtitle(q):
    vid = (q.get("bvid") or q.get("id") or "").strip()
    if not vid:
        return {"error": "没说是哪个视频", "code": -1}
    t = q.get("t")
    # 分 P：后端会带 cid（最准）或 p；都不带就按 vid 里的 ?p= / _pN，再不行就 P1
    return CLIENT.subtitle(vid, t if t not in (None, "") else None, float(q.get("window") or 6),
                           p=q.get("p") or None, cid=q.get("cid") or None)


def r_my_replies(q):
    return CLIENT.my_replies(max(1, min(30, int(q.get("n") or 20))))


# 番剧 / 影视（PGC），只读，给 bilitube 后端：
# GET /pgc/season?ref=<ep123|ss123|md123|番剧链接>[&fresh=1]   整部的信息＋挑中的那一集 pick（ep 给了就是它，否则上次看到的，再否则第一集）
# GET /pgc/playurl?ep=&cid=&qn=       这一集的 mp4 地址；放不了回 404 {"error": 人话, "code":"pgc_denied", "reason":…}
# GET /pgc/search?q=&n=               搜番剧＋影视（各一页）
# GET /pgc/follow?type=1|2&page=&n=   TA 的追番 / 追剧列表
def r_pgc_season(q):
    ref = (q.get("ref") or q.get("id") or "").strip()
    pg = pgc_ref(ref)
    if not pg:
        return {"error": "认不出番剧号（ep / ss / md 号或番剧链接）", "code": -1}
    s = CLIENT.pgc_season(pg[0], pg[1], fresh=q.get("fresh") == "1")
    if "error" in s:
        return s
    out = dict(s)
    out["pick"] = pgc_pick_ep(s, pg[1] if pg[0] == "ep" else None)
    return out


def r_pgc_playurl(q):
    return CLIENT.pgc_playurl(int(q["ep"]), int(q["cid"]), int(q.get("qn") or 80))


def r_pgc_search(q):
    kw = (q.get("q") or "").strip()
    if not kw:
        return {"error": "要搜什么呢", "code": -1}
    return CLIENT.pgc_search(kw[:80], max(1, min(10, int(q.get("n") or 6))))


def r_pgc_follow(q):
    return CLIENT.pgc_follow(int(q.get("type") or 1), int(q.get("page") or 1), int(q.get("n") or 15))


# GET /state?bvid=[&aid=]   TA 对这条视频点没点过赞、收没收藏、在不在稍后再看（只读，三个接口：
#   x/web-interface/archive/has/like、x/v2/fav/video/favoured、x/v2/history/toview）。哪样问不到就不给那样。
def r_state(q):
    bvid = (q.get("bvid") or "").strip()
    if not _BVID_RE.match(bvid):
        return {"error": "bvid 不对", "code": -1}
    if not CLIENT.logged_in():
        return {"error": "B 站没登录", "code": -101}
    vid = f"av{int(q['aid'])}" if (q.get("aid") or "").isdigit() else bvid
    out = {"bvid": bvid}
    lk = CLIENT.is_liked(vid)
    if isinstance(lk, dict) and "error" not in lk:
        out["liked"] = bool(lk.get("liked"))
        out["aid"] = lk.get("aid")
    elif isinstance(lk, dict) and lk.get("code") in (guard.Paused.code, guard.OverBudget.code):
        return lk
    fv = CLIENT.is_faved(f"av{out['aid']}" if out.get("aid") else vid)
    if isinstance(fv, dict) and "error" not in fv:
        out["favoured"] = bool(fv.get("faved"))
    if q.get("later") == "1":
        wl = CLIENT._get("/x/v2/history/toview", need_login=True)
        if isinstance(wl, dict) and "error" not in wl:
            out["later"] = any(str(x.get("bvid")) == bvid for x in (wl.get("list") or []))
    return out


# GET /follow_group?n=&page=   AI 自己关注的 UP（专属关注分组里的，只读）。没配 / 找不到分组也回 200：
#   group=null、want={id,name}、reason＝一句人话，页面照它写空状态。
def r_follow_group(q):
    gid, name = guard.follow_group()
    r = CLIENT.follow_list(max(1, min(50, int(q.get("n") or 30))), max(1, int(q.get("page") or 1)))
    if isinstance(r, dict) and r.get("code") == "follow_denied":
        return {"group": None, "want": {"id": gid, "name": name}, "reason": r["error"],
                "total": 0, "count": 0, "has_more": False, "list": []}
    if isinstance(r, dict) and "error" not in r:
        r["want"] = {"id": gid, "name": name}
    return r


# POST /logout   退出 B 站登录。只给 bilitube 后端用（页面上人点了「退出」才来），MCP 七样里没有这条路。
#   本地凭据、内存里的登录态和按账号的缓存无论如何都清；B 站那头尽力作废（remote: ok / unconfirmed / skipped）。
#   风控暂停着也照样清本地（do_POST 对这条不看暂停闸；B 站那头这时不发）。
def w_logout(b):
    r = CLIENT.logout()
    with LOGIN_LOCK:
        LOGIN.update({"auth_code": None, "png": None, "status": "idle", "result": None, "since": 0})
    with HB_LOCK:                              # 还没补发的观看进度不再替上一个号发
        for st in HB.values():
            if st.get("timer"):
                st["timer"].cancel()
        HB.clear()
    log(f"logout：本地已清，B 站那头 {r['remote']}（{r['note']}）")
    return r


def w_watchlater(b):
    vid = str(b.get("id") or b.get("bvid") or "").strip()
    if not vid:
        return {"error": "没说是哪个视频", "code": -1}
    return CLIENT.watch_later_set(vid, remove=bool(b.get("remove")))


# 观看进度上报：bilitube 播放页用自己的 <video> 放 B 站片子，
# 所以要照网页播放器的样子往 x/click-interface/web/heartbeat 报进度，B 站的「历史记录 / 上次看到」才会有这条。
# POST /heartbeat {bvid, cid, progress 秒, duration 秒, realtime 本段实看秒, play_type, type=3, aid 可选}
#   play_type 按网页播放器：0 播放中 / 1 开始 / 2 暂停 / 3 继续 / 4 结束。
#   节流：同一 cid 30 秒内最多真发一次；窗口里来的只留最新一份（实看秒数累加），窗口一到补发——频率不超、最后进度不丢。
#   日志只记 bvid / cid / 进度 / 结果码，cookie、csrf、mid 一个字都不写。
# GET  /progress?bvid=&cid=   B 站记的「上次看到」（/x/player/wbi/v2 的 last_play_time），播放页的续播提示用

HB_URL = "https://api.bilibili.com/x/click-interface/web/heartbeat"
HB_GAP = 30
HB = {}               # cid → {last, pending, timer, start_ts, real, unsent, seen}
HB_LOCK = threading.Lock()
AID_OF = {}           # bvid → aid（查一次记住）
_BVID_RE = re.compile(r"^BV[0-9A-Za-z]{10}$")


def _hb_aid(bvid):
    if bvid in AID_OF:
        return AID_OF[bvid]
    aid, err = CLIENT._aid_of(bvid)
    if aid:
        AID_OF[bvid] = int(aid)
    return AID_OF.get(bvid)


def _hb_fire(cid, form, bvid):
    """真发一次。表单里的 csrf/mid 现填，不在内存里多留；返回 (code, message)。"""
    data = dict(form)
    data["csrf"] = CLIENT._csrf()
    mid = CLIENT.cookies.get("DedeUserID")
    if mid:
        data["mid"] = mid
    try:
        # 走 BiliClient._send：heartbeat 在白名单里；暂停着不发；写操作不重试；撞风控就写暂停
        j = CLIENT._send("POST", HB_URL, data=data, timeout=15,
                         headers={"Referer": f"https://www.bilibili.com/video/{bvid}"})
        code, msg = j.get("code"), j.get("message")
    except guard.BiliStop as e:
        code, msg = e.code, "已暂停/不发"
    except Exception as e:                      # 只记异常类名：requests 的报错串里可能带请求细节
        code, msg = -1, type(e).__name__
    log(f"heartbeat {bvid} cid={cid} t={form['played_time']}/{form['video_duration']} "
        f"play_type={form['play_type']} real={form['realtime']} → code={code} {msg}")
    return code, msg


def _hb_flush(cid):
    with HB_LOCK:
        st = HB.get(cid)
        if not st:
            return
        st["timer"] = None
        if not st.get("pending"):
            return
        form, bvid = st["pending"]
        st["pending"] = None
        form["realtime"] = st["unsent"]
        st["unsent"] = 0
        st["last"] = time.time()
    _hb_fire(cid, form, bvid)


def w_heartbeat(b):
    if not CLIENT.logged_in():
        return {"error": "B 站没登录", "code": -101}
    bvid = str(b.get("bvid") or "").strip()
    if not _BVID_RE.match(bvid):
        return {"error": "bvid 不对", "code": -1}
    try:
        cid = int(b.get("cid") or 0)
        progress = max(0, int(float(b.get("progress") or 0)))
        duration = max(0, int(float(b.get("duration") or 0)))
        realtime = max(0, min(600, int(round(float(b.get("realtime") or 0)))))
        play_type = int(b.get("play_type") or 0)
        vtype = int(b.get("type") or 3)
    except (TypeError, ValueError):
        return {"error": "参数不对", "code": -1}
    if cid <= 0:
        return {"error": "cid 不对", "code": -1}
    if play_type not in (0, 1, 2, 3, 4):
        play_type = 0
    pgc = None
    try:
        if b.get("epid") and b.get("sid"):
            pgc = {"type": 4, "sub_type": max(0, min(9, int(b.get("sub_type") or 1))),
                   "epid": int(b["epid"]), "sid": int(b["sid"])}
    except (TypeError, ValueError):
        return {"error": "参数不对", "code": -1}
    if duration and progress > duration:
        progress = duration
    aid = b.get("aid") or _hb_aid(bvid)
    if not aid:
        return {"error": "查不到 aid", "code": -1}
    now = time.time()
    with HB_LOCK:
        st = HB.setdefault(cid, {"last": 0, "pending": None, "timer": None,
                                 "start_ts": int(now), "real": 0, "unsent": 0, "seen": now})
        if play_type == 1 or now - st["seen"] > 1800:          # 新开播 / 隔了半小时：另起一段观看
            st["start_ts"], st["real"] = int(now), 0
        st["seen"] = now
        st["real"] += realtime
        st["unsent"] += realtime
        form = {"aid": int(aid), "cid": cid, "bvid": bvid, "played_time": progress,
                "realtime": st["unsent"], "real_played_time": st["real"], "start_ts": st["start_ts"],
                "type": vtype, "sub_type": 0, "dt": 2, "play_type": play_type, "video_duration": duration}
        if pgc:                                   # 番剧的一集：照网页播放器带上 epid / sid（type=4）
            form.update(pgc)
        wait = HB_GAP - (now - st["last"])
        if wait > 0 or st["timer"]:
            st["pending"] = (form, bvid)
            if not st["timer"]:
                t = threading.Timer(max(0.5, wait), _hb_flush, (cid,))
                t.daemon = True
                st["timer"] = t
                t.start()
            return {"code": 0, "queued": True, "in": round(max(0.0, wait), 1)}
        st["last"] = now
        st["unsent"] = 0
    code, msg = _hb_fire(cid, form, bvid)
    out = {"code": code, "message": msg, "sent": True}
    if code != 0:
        out["error"] = f"B 站没收：{code} {msg}"
    return out


def r_progress(q):
    if not CLIENT.logged_in():
        return {"error": "B 站没登录", "code": -101}
    bvid = (q.get("bvid") or "").strip()
    if not _BVID_RE.match(bvid):
        return {"error": "bvid 不对", "code": -1}
    cid = int(q.get("cid") or 0)
    if cid <= 0:
        return {"error": "cid 不对", "code": -1}
    key = {"bvid": bvid, "cid": cid}
    aid = int(q["aid"]) if (q.get("aid") or "").isdigit() else _hb_aid(bvid)   # 番剧的一集后端会直接给 aid，不用再问 view
    if aid:
        key["aid"] = aid
    # 只用 WBI 签名版：撞风控就停，不退回未签名的 /x/player/v2（未签名的 player 接口还可能回别的视频的数据）
    d = CLIENT._get("/x/player/wbi/v2", key, wbi=True)
    if "error" in d:
        return d
    ms = int(d.get("last_play_time") or 0)
    last_cid = int(d.get("last_play_cid") or 0)
    return {"bvid": bvid, "cid": cid, "last_play_cid": last_cid,
            "t": round(ms / 1000) if ms > 0 else 0, "same_cid": (last_cid == cid) if last_cid else None}


REST = {"/video": r_video, "/cid": r_cid, "/playurl": r_playurl,
        "/danmaku_all": r_danmaku_all, "/comments": r_comments,
        "/search": r_search, "/fav_folders": r_fav_folders, "/fav_list": r_fav_list,
        "/history": r_history, "/me": r_me,
        "/watchlater": r_watchlater, "/dynamic": r_dynamic, "/live_following": r_live_following, "/live_danmu": r_live_danmu,
        "/recommend": r_recommend, "/popular": r_popular, "/subtitle": r_subtitle,
        "/my_replies": r_my_replies, "/progress": r_progress,
        "/pgc/season": r_pgc_season, "/pgc/playurl": r_pgc_playurl, "/pgc/search": r_pgc_search,
        "/pgc/follow": r_pgc_follow, "/state": r_state, "/follow_group": r_follow_group}
REST_POST = {"/like": w_like, "/fav": w_fav, "/watchlater": w_watchlater, "/heartbeat": w_heartbeat,
             "/logout": w_logout}
REST_POST_PAUSE_OK = {"/logout"}      # 暂停着也放进来的：退出只清本地，B 站那头自己会看暂停闸


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        pass

    def _send(self, code, obj):
        raw = b"" if obj is None else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if raw:
            self.wfile.write(raw)

    def _loopback(self):
        # 只认本机来的连接（HOST 本来就绑 127.0.0.1，这里再兜一层，防有人改绑 0.0.0.0）
        return (self.client_address[0] if self.client_address else "") in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def do_GET(self):
        if self.path.rstrip("/") == "/healthz":
            p = guard.read_pause()
            return self._send(200, {"ok": True, "service": "bili-mcp", "paused": bool(p),
                                    "pause": ({k: p.get(k) for k in ("since_bj", "endpoint", "signal")} if p else None)})
        # 给同机聊天后端用的小 REST：用户在聊天里贴 B 站链接，后端来这儿查一下就能画视频卡。
        # GET /video?id=<BV/av/链接/b23.tv 短链> → BiliClient().video(id) 原样 JSON（封面顺手缓存成 att-bili-cover-*.jpg）
        route = self.path.split("?", 1)[0].rstrip("/")
        if route in REST:
            if not self._loopback():
                return self._send(403, {"error": "loopback only"})
            qs = {k: v[0].strip() for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items() if v}
            p = guard.read_pause()
            if p:
                return self._send(503, {"error": guard.human_pause(p), "paused": True,
                                        "pause": {k: p.get(k) for k in ("since_bj", "endpoint", "signal")}})
            try:
                with guard.lane("rest"):
                    r = REST[route](qs)
            except guard.BiliStop as e:
                return self._send(503, {"error": e.human, "code": e.code, "paused": isinstance(e, guard.Paused)})
            except KeyError as e:
                return self._send(400, {"error": f"missing {e}"})
            except Exception as e:
                return self._send(502, {"error": f"{route} 出错: {e}"})
            if not isinstance(r, dict):
                return self._send(502, {"error": "bad response"})
            return self._send(404 if "error" in r else 200, r)
        if self.path.rstrip("/") == "/mcp":
            return self._send(405, {"error": "SSE not supported, POST only"})
        self._send(404, {"error": "not found"})

    def do_DELETE(self):
        if self.path.rstrip("/") == "/mcp":
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        route = self.path.split("?", 1)[0].rstrip("/")
        if route in REST_POST:
            if not self._loopback():
                return self._send(403, {"error": "loopback only"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            except Exception:
                return self._send(400, {"error": "bad json"})
            if not isinstance(body, dict):
                return self._send(400, {"error": "bad json"})
            p = guard.read_pause()
            if p and route not in REST_POST_PAUSE_OK:
                return self._send(503, {"error": guard.human_pause(p), "paused": True})
            try:
                with guard.lane("rest"):
                    r = REST_POST[route](body)
            except guard.BiliStop as e:
                return self._send(503, {"error": e.human, "code": e.code, "paused": isinstance(e, guard.Paused)})
            except Exception as e:
                return self._send(502, {"error": f"{route} 出错: {e}"})
            if not isinstance(r, dict):
                return self._send(502, {"error": "bad response"})
            return self._send(404 if "error" in r else 200, r)
        if self.path.rstrip("/") != "/mcp":
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            msg = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"error": "bad json"})
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:
            return self._send(202, None)
        if method == "initialize":
            client_pv = str((msg.get("params") or {}).get("protocolVersion") or "")
            pv = client_pv if re.match(r"^20\d{2}-\d{2}-\d{2}$", client_pv) else PROTOCOL
            r = {"protocolVersion": pv, "capabilities": {"tools": {}},
                 "serverInfo": {"name": "bilitube", "version": "2.0.0"},
                 "instructions": INSTRUCTIONS}
        elif method == "tools/list":
            r = {"tools": TOOLS}
        elif method == "tools/call":
            p = msg.get("params") or {}
            try:
                r = {"content": bilitube.content(p.get("name"), p.get("arguments") or {})}
            except ToolError as e:
                r = {"content": [{"type": "text", "text": str(e)}], "isError": True}
            except Exception as e:
                log(f"tools/call {p.get('name')} 出错: {type(e).__name__}: {e}")
                r = {"content": [{"type": "text", "text": f"执行出错: {e}"}], "isError": True}
        elif method == "ping":
            r = {}
        else:
            return self._send(200, {"jsonrpc": "2.0", "id": mid,
                                    "error": {"code": -32601, "message": f"method not found: {method}"}})
        self._send(200, {"jsonrpc": "2.0", "id": mid, "result": r})


if __name__ == "__main__":
    if sys.argv[1:2] == ["share"]:          # 命令行：python bili_mcp.py share <id> [配文]
        if len(sys.argv) < 3:
            sys.exit("用法: bili_mcp.py share <BV/av/链接> [配文]")
        try:
            print(t_share({"id": sys.argv[2], "note": " ".join(sys.argv[3:])}))
        except ToolError as e:
            sys.exit(str(e))
        sys.exit(0)
    log(f"bilitube MCP 服务起在 {HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
