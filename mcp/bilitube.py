# -*- coding: utf-8 -*-
"""
bilitube 的 MCP 工具表：七个工具，每个用 action 选动作。和 bili_mcp.py 跑在同一个进程里（bili_mcp.py 导入时 bind），
所以只有一个 BiliClient、一道风控闸（server/guard.py）、一本每小时预算账。

  login     qr / status
  find      search / feed / hot / following / live / bangumi              （只读）
  video     info / comments / replies / subtitle / danmaku                （只读）
  library   history / favorites / watchlater / my_replies / follows       （只读）
  watching  where / look                                                  （问 bilitube 后端，不碰 B 站账号）
  stage     danmaku / queue / play_now / card                             （会惊动用户；queue 写 B 站稍后再看）
  account   like / unlike / fav / unfav / later_add / later_remove / follow / unfollow   （会改用户的 B 站账号）
  （退出登录不在这里：只有用户在页面设置里点「退出」，后端走回环 REST POST /logout。）

碰 B 站的动作都经 core.gated（暂停闸＋mcp 通道预算）；「一起看」那一半在 watch.py。
by 取 companion（AI 自己）或 user（用户），和前后端、数据里 author 字段用的是同一对值。
"""
import re

import watch

C = None          # bili_mcp 模块本身（由它 bind 进来，不 import，免得 __main__ 被导入第二份）


def bind(core):
    global C
    C = core


INSTRUCTIONS = (
    "bilitube：逛 B 站，和用户一起看片。图片路径单独一行原样写进回复，用户才看得到。"
    "自己逛：find → video info → comments → subtitle，喜欢再 account。"
    "一起看：先 watching where，要画面再 look，接话用 stage danmaku。"
    "stage 会惊动用户；account 会改用户的 B 站账号。撞风控会整套暂停，别换法子重试。"
)


def _s(desc, **extra):
    return dict({"type": "string", "description": desc}, **extra) if desc else dict({"type": "string"}, **extra)


REF = _s("BV/av 号、ep/ss 号或链接")
T = _s("秒或 mm:ss")
LIMIT = {"type": "integer", "description": "条数"}
FULL = {"type": "boolean", "description": "简介不截断，评论带楼中楼和置顶"}


def _tool(name, desc, actions, props, required=()):
    p = {"action": {"type": "string", "enum": list(actions)}}
    p.update(props)
    return {"name": name, "description": desc,
            "inputSchema": {"type": "object", "properties": p, "required": ["action", *required]}}


TOOLS = [
    _tool("login",
          "B 站登录。qr：出二维码，把路径那行原样写进回复给用户扫，后台等 3 分钟；"
          "status：登没登录、有没有因风控暂停、这一小时用了多少请求。退出登录只能由用户在页面上做，这里没有。",
          ("qr", "status"),
          {"wait": {"type": "boolean", "description": "qr：原地等到扫完再返回"},
           "force": {"type": "boolean", "description": "qr：已经登录也重新出码"}}),
    _tool("find",
          "找片，只读，一行一条带编号。search：搜视频，第一页另列番剧 · 影视；feed：首页推荐；hot：热门；"
          "following：关注的 UP 新投稿；live：关注的人里谁在直播；bangumi：追番 / 追剧列表。",
          ("search", "feed", "hot", "following", "live", "bangumi"),
          {"q": _s("search：关键词"), "limit": LIMIT,
           "page": {"type": "integer", "description": "第几页（feed 是换一批）"},
           "full": {"type": "boolean", "description": "每条多给 UP 和日期"},
           "mid": {"type": "integer", "description": "following：只看这个 UP（mid）"},
           "kind": _s("bangumi：drama＝追剧，默认追番", enum=["anime", "drama"])}),
    _tool("video",
          "看一条视频，只读，按开销从小到大。info：标题、UP、数据、简介（截断）；comments：最热几条评论；"
          "replies：一条评论下的楼中楼；subtitle：t 前后一小段字幕；danmaku：t 前后的公开弹幕，去重计数。",
          ("info", "comments", "replies", "subtitle", "danmaku"),
          {"ref": REF, "t": T, "limit": LIMIT, "full": FULL,
           "cover": {"type": "boolean", "description": "info：带封面图"},
           "rpid": {"type": "integer", "description": "replies：评论的 rpid"},
           "sort": _s("comments：默认 hot", enum=["hot", "time"]),
           "window": {"type": "number", "description": "t 前后各几秒"},
           "page": {"type": "integer"}, "next_offset": _s("comments：上一页给的游标"),
           "film": _s("subtitle：bilitube 里本地片的 id，给了就不用 ref")}),
    _tool("library",
          "用户 B 站账号里的东西，只读。history：最近看过；favorites：收藏夹列表或某个夹子里的视频；"
          "watchlater：用户 B 站账号的稍后再看（bilitube 里唯一的待看单子就是它）；my_replies：用户收到的回复（用户问起再看，不替用户回）；"
          "follows：AI 自己关注的 UP（专属关注分组里的）。",
          ("history", "favorites", "watchlater", "my_replies", "follows"),
          {"limit": LIMIT,
           "folder_id": {"type": "integer", "description": "favorites：夹子 id，不给＝列夹子"},
           "page": {"type": "integer"}}),
    _tool("watching",
          "一起看：问用户的 bilitube 播放页现在什么样；会让用户的播放页亮 20 秒「在看这一幕」角标。"
          "where：在放哪部、第几秒、播着没，带这几秒的字幕和弹幕，只回文字；"
          "look：t 前后三帧竖拼成一张图，路径那行原样写进回复用户才看得到，回话写明图从哪来、是哪一刻的。"
          "直播用 film=live:<房间号>。",
          ("where", "look"),
          {"film": _s("不给＝正在看的那部"),
           "t": _s("look：秒或 mm:ss，不给＝播放器最近一次上报的进度"),
           "quality": _s("look：默认标准；单帧＝那一秒一张大图", enum=["省", "标准", "高清", "单帧"])}),
    _tool("stage",
          "会惊动用户：往用户的 bilitube 播放页、聊天和 B 站稍后再看里动手。danmaku：在 t 发一条私人弹幕，只出现在 bilitube 播放页上，"
          "不发去 B 站；也能列出两人的、删自己的。queue：加进用户 B 站账号的稍后再看（会写账号；已在不重复；"
          "每小时 20 次、间隔 30 秒，被拒别重试），用户开着播放页就排成下一个播放。"
          "play_now：立刻切过去，会打断用户，只在用户明说现在就看时用；不进稍后再看。"
          "card：往聊天里发一张推荐卡，点开进 bilitube。",
          ("danmaku", "queue", "play_now", "card"),
          {"ref": REF, "film": _s("danmaku：不给＝正在看的那部"), "t": _s("danmaku：发在第几秒，不给＝播放器此刻"),
           "text": _s("danmaku：弹幕内容；card：配文", maxLength=1000),
           "sub": _s("danmaku：默认 add", enum=["add", "list", "del"]),
           "id": _s("sub=del：弹幕 id（sub=list 里有）"),
           "queue": {"type": "boolean", "description": "card：true 时同时照 queue 加进 B 站稍后再看"},
           "mode": _s("danmaku：当场发的样式", enum=["scroll", "top"]),
           "color": _s("danmaku：十六进制颜色")}),
    _tool("account",
          "会改用户的 B 站账号。like/unlike：点赞、取消；fav/unfav：收藏、移出，by=companion（默认）只进「口袋」"
          "（AI 专用的私密收藏夹），by=user 进用户的夹子、只在用户开口时用；later_add/later_remove：B 站稍后再看加 / 移（同 stage queue）；"
          "follow/unfollow：AI 自己关注 UP，只进专属关注分组，只取关分组里的人（每小时 5 次、间隔 60 秒）。"
          "撞风控整套停、不重试；评论、公开弹幕、投币不开放。",
          ("like", "unlike", "fav", "unfav", "later_add", "later_remove", "follow", "unfollow"),
          {"ref": REF, "by": _s("默认 companion", enum=["companion", "user"]),
           "folder_id": {"type": "integer", "description": "by=user：收进哪个夹子，不给＝默认夹子"},
           "mid": {"type": "integer", "description": "follow/unfollow：UP 的 mid，不给＝ref 那条的 UP"}}),
]
ACTIONS = {t["name"]: t["inputSchema"]["properties"]["action"]["enum"] for t in TOOLS}


# ── 小工具 ─────────────────────────────────────────────

def _ref(a, need=True):
    """ref（兼容 film 写法 bili:BV…_p2 → BV…?p=2）。"""
    r = str(a.get("ref") or "").strip()
    if not r and need:
        raise C.ToolError("要给 ref（BV/av 号、ep/ss 号或链接）")
    m = re.match(r"^(?:bili:)?(BV[0-9A-Za-z]{10})_p(\d{1,4})$", r)
    if m:
        return f"{m.group(1)}?p={m.group(2)}"
    return r[5:] if r.startswith("bili:") else r


def _int(v, d):
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


def _by(a):
    b = str(a.get("by") or "companion").strip().lower()
    if b in ("companion", "user"):
        return b
    raise C.ToolError("by 只能是 companion（AI 自己收，只进口袋）或 user（TA 开口让收）")


def _bili(fn, a):
    """碰 B 站的：过暂停闸、记 mcp 通道预算（和 bili_mcp.call 同一道）。"""
    return C.gated(fn, a, touches_bili=True)


def _home(fn, a):
    """只问 bilitube 后端：暂停着也能用（后端自己问 B 站时走同一道闸）。"""
    return C.gated(fn, a, touches_bili=False)


def _w(text):
    """watch.py 的失败回话以 ❌ 开头 → 报错。"""
    if str(text).startswith("❌"):
        raise C.ToolError(str(text)[1:].strip())
    return text


# ── find ──

def _pgc_row(i, g, follow=False):
    bits = [x for x in (g.get("type_name"), g.get("progress") if follow else "",
                        g.get("index_show"), g.get("badge")) if x]
    return f"{i}. {C._cut(g.get('title'), 40)}" + (f"（{' · '.join(bits)}）" if bits else "") + f"  ss{g.get('season_id')}"


def f_search(a):
    q = str(a.get("q") or "").strip()
    if not q:
        raise C.ToolError("search 要给 q（搜什么）")
    n, page = max(1, min(20, _int(a.get("limit"), 8))), max(1, _int(a.get("page"), 1))
    r = C._check(C.CLIENT.search(q, n, page))
    out = [f"搜「{r['keyword']}」（约 {r.get('total')} 条" + (f"，第 {page} 页" if page > 1 else "") + "）："]
    for i, v in enumerate(r["list"], 1):
        out.append(f"{i}. " + (f"{v['title']}  UP {v['up']}  播放{C._n(v['play'])}  {v['duration']}  {v['pubdate']}  {v['bvid']}"
                               if a.get("full") else C._slim(v)))
    if not r["list"]:
        out.append("（没搜到视频）")
    if page == 1:                                   # 番剧 / 影视只在第一页另列一段
        g = C.CLIENT.pgc_search(q[:80], 4)
        if isinstance(g, dict) and g.get("code") in (C.guard.Paused.code, C.guard.OverBudget.code):
            out.append(f"番剧 · 影视：没问（{g.get('error')}）")
        elif isinstance(g, dict) and "error" not in g:
            rows = (g.get("bangumi") or []) + (g.get("ft") or [])
            if rows:
                out.append("番剧 · 影视：")
                out += [_pgc_row(i, x) for i, x in enumerate(rows, len(r["list"]) + 1)]
    return "\n".join(out)


def f_bangumi(a):
    kind = 2 if str(a.get("kind") or "").lower() == "drama" else 1
    r = C._check(C.CLIENT.pgc_follow(kind, max(1, _int(a.get("page"), 1)), max(1, min(30, _int(a.get("limit"), 10)))))
    what = "追剧" if kind == 2 else "追番"
    if not r["list"]:
        return f"{what}列表是空的。"
    out = [f"{what}（{r['count']}/{r['total']} 部）："] + [_pgc_row(i, x, follow=True) for i, x in enumerate(r["list"], 1)]
    if r.get("has_more"):
        out.append(f"还有，page={r['page'] + 1}")
    return "\n".join(out)


def _feed(kind):
    def go(a):
        args = {"kind": kind, "limit": a.get("limit"), "detail": a.get("full"), "host_mid": a.get("mid")}
        if kind == "recommend":
            args["fresh"] = a.get("page")
        elif kind == "popular":
            args["page"] = a.get("page")
        return C.t_feed(args)
    return go


# ── video ──

def v_subtitle(a):
    if a.get("film") and not a.get("ref"):        # bilitube 里的片子：本地字幕文件优先，B 站片子退到 CC 字幕
        return _home(lambda x: _w(watch.act_sub(x)), {"film": a["film"], "t": a.get("t"), "window": a.get("window")})
    ref = _ref(a)
    m = re.search(r"[?&]p=(\d{1,4})", ref)
    return _bili(C.t_subtitle, {"id": ref, "t": a.get("t"), "window": a.get("window"),
                                "p": int(m.group(1)) if m else None})


def v_danmaku(a):
    if a.get("t") in (None, ""):
        raise C.ToolError("danmaku 要给 t（第几秒）")
    return _bili(C.t_danmaku, {"id": _ref(a), "t": a["t"], "window": a.get("window"), "limit": a.get("limit")})


# ── library ──

def l_favorites(a):
    if not a.get("folder_id"):
        return C.t_fav({"folders": True})
    r = C._check(C.CLIENT.fav_list(int(a["folder_id"]), max(1, min(40, _int(a.get("limit"), 20))),
                                   max(1, _int(a.get("page"), 1))))
    if not r["list"]:
        return f"收藏夹「{r.get('title') or r['media_id']}」这页是空的。"
    pk = C.guard.pocket_id()
    title = f"收藏夹「{r.get('title') or r['media_id']}」（共 {r.get('total')} 个" + \
            ("，" + C.names()["companion"] + "的口袋" if pk and r["media_id"] == pk else "") + "）："
    out = C._rows(title, r["list"], detail=False)
    return out + (f"\n还有，page={r['page'] + 1}" if r.get("has_more") else "")


# ── watching / stage ──

def w_look(a):
    a = dict(a)
    if not str(a.get("quality") or "").strip():
        a["quality"] = "标准"
    return _w(watch.act_look(a))


def s_danmaku(a):
    if (a.get("mode") or a.get("color")) and not a.get("film") and a.get("t") in (None, "") \
            and str(a.get("sub") or "add") == "add":
        return C.t_danmaku_say({"text": a.get("text"), "mode": a.get("mode"), "color": a.get("color")})
    return _w(watch.act_dm({k: a.get(k) for k in ("film", "t", "text", "sub", "id")}))


_BV_IN = re.compile(r"(BV[0-9A-Za-z]{10})")
_PGC_REF = re.compile(r"^(?:bili:)?(ep|ss|md)\d{1,12}$", re.I)


def _later_put(ref):
    """往用户 B 站稍后再看里加一条（bilitube 只有这一份待看单子）。已在就不发写请求。
    限速在 guard.spend_write：每小时 later_hourly_budget 次、两次至少隔 later_min_gap_s 秒，没到直接拒，不排队。
    番剧那一集按 aid 加（B 站收不收看它的回话）。"""
    vid, bv = ref, None
    if _PGC_REF.match(ref):
        d = watch.api("/bili/add", payload={"ref": ref[5:] if ref.lower().startswith("bili:") else ref}, timeout=40)
        f = d.get("film") or {} if "_error" not in d else {}
        if not f.get("aid"):
            raise C.ToolError(f"这一集没问到 aid，没加进 B 站稍后再看：{d.get('_error') or '番剧信息里没有'}")
        vid = f"av{int(f['aid'])}"
    else:
        m = _BV_IN.search(ref)
        bv = m.group(1) if m else None
        vid = bv or ref
    have = C.CLIENT.watch_later(100)
    if "error" in have:
        raise C.ToolError(f"B 站稍后再看没读到，没加：{have['error']}")
    rows = have.get("list") or []
    if any((bv and r.get("bvid") == bv) or (vid.startswith("av") and str(r.get("aid")) == vid[2:]) for r in rows):
        return "B 站稍后再看里本来就有，没再加。"
    r = C._check(C.CLIENT.watch_later_set(vid))
    return f"加进用户的 B 站稍后再看了（av{r['aid']}）。"


def _follow(remove):
    """follow / unfollow：对象是 mid，或 ref 那条视频的 UP（先问一次视频信息，不下封面）。"""
    def go(a):
        name, m = None, a.get("mid")
        if m not in (None, "", 0, "0"):
            mid = _int(m, 0)
            if mid <= 0:
                raise C.ToolError("mid 要是 UP 的数字 id")
        elif a.get("ref"):
            v = C._check(C.CLIENT.video(_ref(a), cover=False))
            if v.get("pgc") or not v.get("up_mid"):
                raise C.ToolError("这条没有能关注的 UP（番剧影视没有），给 UP 的 mid")
            mid, name = int(v["up_mid"]), v.get("up")
        else:
            raise C.ToolError("要给 mid（UP 的 mid）或 ref（关注这条视频的 UP）")
        return C.t_follow({"mid": mid, "name": name, "remove": remove})
    return go


def s_queue(a):
    ref = _ref(a)
    head = _bili(lambda x: _later_put(x["ref"]), {"ref": ref})
    tail = _home(lambda x: _w(watch.act_order(x)), {"ref": ref, "play": "next"})
    return head + "\n" + tail


def s_card(a):
    ref = _ref(a)
    head = ""
    if a.get("queue"):
        head = _bili(lambda x: _later_put(x["ref"]), {"ref": ref}) + "\n"
    return head + _bili(C.t_share, {"id": ref, "note": a.get("text") or ""})


# ── 路由表：tool → action → (handler, 碰不碰 B 站) ───────────────

ROUTES = {
    "login": {
        "qr": (lambda a: C.t_login({"wait": a.get("wait"), "force": a.get("force")}), True),
        "status": (lambda a: C.t_status({}), False),
    },
    "find": {
        "search": (f_search, True),
        "feed": (_feed("recommend"), True),
        "hot": (_feed("popular"), True),
        "following": (_feed("dynamic"), True),
        "live": (_feed("live"), True),
        "bangumi": (f_bangumi, True),
    },
    "video": {
        "info": (lambda a: C.t_video({"id": _ref(a), "full": a.get("full"), "cover": a.get("cover")}), True),
        "comments": (lambda a: C.t_comments({"id": _ref(a), "sort": a.get("sort"), "n": a.get("limit"),
                                             "detail": a.get("full"), "page": a.get("page"),
                                             "next_offset": a.get("next_offset")}), True),
        "replies": (lambda a: C.t_replies({"id": _ref(a), "rpid": a.get("rpid"), "page": a.get("page")}), True),
        "subtitle": (v_subtitle, None),             # None：handler 自己决定碰不碰 B 站
        "danmaku": (v_danmaku, None),
    },
    "library": {
        "history": (lambda a: C.t_history({"n": a.get("limit")}), True),
        "favorites": (l_favorites, True),
        "watchlater": (lambda a: C.t_watchlater({"action": "list", "n": a.get("limit")}), True),
        "my_replies": (lambda a: C.t_my_replies({"limit": a.get("limit")}), True),
        "follows": (lambda a: C.t_follows({"n": a.get("limit"), "page": a.get("page")}), True),
    },
    "watching": {
        "where": (lambda a: _w(watch.where_brief(a)), False),
        "look": (w_look, False),
    },
    "stage": {
        "danmaku": (s_danmaku, False),
        "queue": (s_queue, None),
        "play_now": (lambda a: _w(watch.act_order({"ref": _ref(a), "play": "now"})), False),
        "card": (s_card, None),
    },
    "account": {
        "like": (lambda a: C.t_like({"id": _ref(a), "on": True}), True),
        "unlike": (lambda a: C.t_like({"id": _ref(a), "on": False}), True),
        "fav": (lambda a: C.t_fav({"id": _ref(a), "folder_id": a.get("folder_id"), "by": _by(a)}), True),
        "unfav": (lambda a: C.t_fav({"id": _ref(a), "folder_id": a.get("folder_id"), "by": _by(a), "remove": True}), True),
        "later_add": (lambda a: C.t_watchlater({"action": "add", "id": _ref(a)}), True),
        "later_remove": (lambda a: C.t_watchlater({"action": "remove", "id": _ref(a)}), True),
        "follow": (_follow(False), True),
        "unfollow": (_follow(True), True),
    },
}


def call(name, args):
    """tools/call → 文字。错了抛 core.ToolError。"""
    args = dict(args or {})
    acts = ROUTES.get(name)
    if acts is None:
        raise C.ToolError(f"没有 {name} 这个工具。bilitube 有七样：{' / '.join(ROUTES)}，每个用 action 选动作。")
    act = str(args.get("action") or "").strip()
    if act not in acts:
        raise C.ToolError(f"{name} 的 action 只能是：{' / '.join(acts)}" + (f"（收到的是 {act!r}）" if act else "（没给 action）"))
    fn, bili = acts[act]
    if bili is None:
        return fn(args)
    return C.gated(fn, args, touches_bili=bili)


def content(name, args):
    """tools/call 的 content 列表：文字一块；看封面 / look 时图另作 image 块跟在后面。"""
    text = call(name, args)
    blocks = [{"type": "text", "text": text}]
    img = None
    if name == "video" and args.get("action") == "info" and args.get("cover"):
        img = C.inline_shot(text)
    elif name == "watching" and args.get("action") == "look":
        img = watch.inline_shot(text, shrink=C._shrink_jpeg)
    if img:
        blocks.append(img)
    return blocks
