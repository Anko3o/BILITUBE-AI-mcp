#!/usr/bin/env python3
"""
bilitube 的 B 站客户端层。

读：视频信息、评论区、弹幕、字幕、搜索、番剧，以及登录账号的历史、收藏夹、稍后再看、追番、收到的回复。
写：只有点赞、收藏、稍后再看、关注（AI 只进自己的专属分组）、观看进度上报、扫码登录和退出登录；发评论、投币在这一层就拒，传输层白名单也不放行。
playurl / pgc_playurl 只给 bilitube 后端用：bilitube_server.py 经本机 bili_mcp.py 的回环 REST 拿地址，自己代理 CDN。

登录走 TV 端扫码（passport-tv-login），凭据落在 BILI_CRED_FILE（默认 mcp/state/cred.json，0600），
里面有 access_token / refresh_token / cookies（SESSDATA、bili_jct、DedeUserID、buvid3…）。
任何函数都不打印凭据，status() 只说「有没有、是谁、几时到期」。

未登录也能跑的：video / comments / replies / search / danmaku（靠 buvid3+buvid4 指纹和 WBI 签名过风控）。
要登录的：history / like / fav / is_liked / is_faved / watch_later / 追番 / 字幕等。

命令行：
  bili.py login            出二维码 png（把路径写进聊天，TA 就能看见），然后原地等 TA 扫（120s）
  bili.py status
  bili.py video <BV/av/链接/b23>
  bili.py comments <id> [hot|time] [page]
  bili.py replies <id> <rpid> [pn]
  bili.py history [n]
  bili.py search <关键词> [n]
  bili.py danmaku <id> <秒或mm:ss> [window] [limit]   某个时间点前后的弹幕（去重计数）
  bili.py like <id> [off]     fav <id> [folder_id]     folders
  bili.py coin <id> / post <id> <文字>     （不开放：调了直接拒，传输层也拒）
  bili.py pause-status         看是不是因为撞风控暂停着
  bili.py resume               解除暂停（只能在真终端里由人来敲）

护栏（server/guard.py）：所有对 B 站的请求都走 BiliClient._send——
撞风控（HTTP 401/403/412/429、code -352/-412/-509/-403、v_voucher、验证码字样）就写暂停标记，
之后什么都不发；POST 只放行写死的几条（收藏、点赞、关注、稍后再看、观看进度上报、扫码登录、退出登录）。
"""
import datetime
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import zlib

import requests

import guard  # 护栏：风控暂停 / 写入白名单 / 每小时预算，都在 _send 这一层守

_MCP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))      # mcp/
_REPO_ROOT = os.path.dirname(_MCP_ROOT)
# 登录凭据（扫码后写入，0600）。别放进仓库目录以外的公共位置，也别提交进 git。
CRED_PATH = os.environ.get("BILI_CRED_FILE") or os.path.join(_MCP_ROOT, "state", "cred.json")
CACHE_DIR = os.environ.get("BILI_CACHE_DIR") or os.path.join(_MCP_ROOT, "state", "cache")
# 二维码、封面等图片落在这里。想让聊天前端直接显示图片，就把它指到前端认得的上传目录。
UPLOADS = os.environ.get("BILITUBE_IMAGE_DIR") or os.path.join(_REPO_ROOT, "data", "images")

API = "https://api.bilibili.com"
PASSPORT = "https://passport.bilibili.com"
LIVE = "https://api.live.bilibili.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

# 云视听小电视（TV 端）的 appkey / appsec：TV 扫码登录必须用这一组签名，
# 拿到的 access_token 之后也要配同一组用。
TV_APPKEY = "4409e2ce8ffd12b8"
TV_APPSEC = "59b43e04ad6965f34319062b478f83dd"

# WBI 混淆表：img_key+sub_key 按这张表重排取前 32 位就是 mixin key
_WBI_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52]

NOT_LOGGED_IN = {"error": "not_logged_in", "hint": "扫码登录：MCP 用 login(action=\"qr\")，命令行用 bili.py login"}


def _md5(s):
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _dur_norm(d):
    """搜索接口给的时长是 "3:0" / "60:1" 这种不补零的怪串，换算成秒再用 _mmss 写成人话。"""
    try:
        parts = [int(x) for x in str(d or "").split(":")]
    except Exception:
        return str(d or "")
    if not parts or len(parts) > 3:
        return str(d or "")
    sec = 0
    for x in parts:
        sec = sec * 60 + x
    return _mmss(sec)


def _mmss(sec):
    try:
        sec = int(sec)
    except Exception:
        return str(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


_BJ = datetime.timezone(datetime.timedelta(hours=8))


def _when(ts):
    """时间戳 → 北京时间（B 站的时区；服务器本身可能是 UTC）。"""
    try:
        return datetime.datetime.fromtimestamp(int(ts), _BJ).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def _parse_t(t):
    """秒数或 mm:ss / h:mm:ss → 秒。"""
    if isinstance(t, (int, float)):
        return float(t)
    s = str(t).strip()
    if ":" in s:
        parts = [float(x) for x in s.split(":")]
        sec = 0.0
        for x in parts:
            sec = sec * 60 + x
        return sec
    return float(s)


def _https(u):
    """头像/封面地址补协议；没有就空串。只透传地址，不下载、不缓存。"""
    u = str(u or "")
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("http://"):
        return "https://" + u[7:]
    return u


def _strip_em(s):
    return re.sub(r"</?em[^>]*>", "", s or "")


def _load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def _save_private(path, obj):
    """0600 落盘（先建空文件再写，避免任何一瞬间权限是 644）。"""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.chmod(path, 0o600)


# ── 视频 id 解析 ────────────────────────────────────────────

_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_AV_RE = re.compile(r"(?:^|/|\b)av(\d+)", re.I)


# ── 番剧 / 影视（PGC）：ep / ss / md 号、bangumi 链接 ─────────────────
# 只做「登录账号本来就能看的」：取流照实问 B 站，大会员 / 付费 / 地区 / DRM 不给放就照实说，不绕。

_PGC_REF_RE = re.compile(r"(?<![0-9A-Za-z])(ep|ss|md)(\d{1,12})(?![0-9])", re.I)
PGC_TYPES = {1: "番剧", 2: "电影", 3: "纪录片", 4: "国创", 5: "电视剧", 7: "综艺"}


def pgc_ref(s):
    """'ep123' / 'ss123' / 'md123' / 'bili:ep123' / bilibili.com/bangumi/play/ep123 / bangumi/media/md123
    → ("ep"|"ss"|"md", 数字)；不是番剧号就 None。BV 号里的字母挨着数字，lookbehind 挡住，不会误认。"""
    m = _PGC_REF_RE.search(str(s or ""))
    if not m:
        return None
    return m.group(1).lower(), int(m.group(2))


def pgc_index(title):
    """一集的序号：'3' → '第3话'；'正片' / '全片'（电影）→ ''；'番外5' / 'PV' 这类原样。"""
    t = str(title or "").strip()
    if t.isdigit():
        return f"第{t}话"
    if t in ("正片", "全片"):
        return ""
    return t


def pgc_film_title(season_title, ep):
    """片单里这一集的名字：「番名 第N话」（电影就是番名）。小标题另放 long_title。"""
    return " ".join(x for x in (str(season_title or "").strip(), (ep or {}).get("index") or "") if x)


def norm_pgc_ep(e):
    e = e or {}
    rights = e.get("rights") or {}
    ms = e.get("duration") or 0
    return {"ep_id": e.get("id") or e.get("ep_id"), "aid": e.get("aid"), "bvid": e.get("bvid"), "cid": e.get("cid"),
            "index": pgc_index(e.get("title")), "title_raw": str(e.get("title") or ""),
            "long_title": str(e.get("long_title") or "").strip(),
            "duration": int(round(ms / 1000)) if ms else 0,
            "badge": str(e.get("badge") or "").strip(), "status": e.get("status"),
            "area_limited": bool(rights.get("area_limit")),
            "cover": _https(e.get("cover"))}


def norm_pgc_season(res):
    """/pgc/view/web/season 的 result → 这里用的样子（只留用得上的几样）。"""
    res = res or {}
    st = res.get("stat") or {}
    us = res.get("user_status") or {}
    prog = us.get("progress") or {}
    pub = res.get("publish") or {}
    eps = [norm_pgc_ep(e) for e in (res.get("episodes") or []) if (e.get("id") or e.get("ep_id"))]
    t = res.get("type")
    return {"season_id": res.get("season_id"), "media_id": res.get("media_id"),
            "title": str(res.get("season_title") or res.get("title") or "").strip(),
            "type": t, "type_name": PGC_TYPES.get(t, "番剧"),
            "cover": _https(res.get("cover")), "evaluate": str(res.get("evaluate") or "").strip(),
            "total": len(eps), "is_finish": bool(pub.get("is_finish")),
            "new_ep_desc": str((res.get("new_ep") or {}).get("desc") or ""),
            "views": st.get("views"), "danmakus": st.get("danmakus"),
            "pay_tip": str((res.get("payment") or {}).get("tip") or ""),
            "vip": ((us.get("vip_info") or {}).get("status") == 1),
            "progress": {"ep_id": prog.get("last_ep_id"), "t": prog.get("last_time") or 0,
                         "index": prog.get("last_ep_index")},
            "episodes": eps,
            "url": f"https://www.bilibili.com/bangumi/play/ss{res.get('season_id')}"}


def pgc_pick_ep(season, ep_id=None):
    """要的那集；没指定就「上次看到的那一集」，再没有就第一集。找不到回 None。"""
    eps = (season or {}).get("episodes") or []
    if ep_id:
        return next((e for e in eps if str(e.get("ep_id")) == str(ep_id)), None)
    last = ((season or {}).get("progress") or {}).get("ep_id")
    if last:
        hit = next((e for e in eps if str(e.get("ep_id")) == str(last)), None)
        if hit:
            return hit
    return eps[0] if eps else None


PGC_DENY = {
    "area": "这一集 B 站说这个地区看不了（取播放地址的是服务器，B 站按服务器所在的地区判断）",
    "vip": "这一集要大会员",
    "pay": "这一集要单独付费",
    "drm": "这一集有版权加密（DRM），网页播放器放不了",
    "format": "B 站只给了这个播放器放不了的格式",
    "other": "B 站不给放这一集",
}


def norm_pgc_play(r):
    """/pgc/player/web/playurl 的整份返回 → 能放：{url, backup_urls, expires_at, qn, format, accept, ...}；
    放不了：{"error": 人话, "code": "pgc_denied", "reason": area|vip|pay|drm|format|other}。
    accept 只列 B 站这次回的档（accept_quality），大会员档在非大会员账号上标 vip_only、不给选。"""
    def deny(reason, msg=""):
        return {"error": PGC_DENY[reason] + (f"（B 站原话：{msg}）" if msg and reason == "other" else ""),
                "code": "pgc_denied", "reason": reason, "bili_message": str(msg or "")[:60]}
    r = r if isinstance(r, dict) else {}
    if r.get("code") not in (0, None):
        msg = str(r.get("message") or "")
        if "地区" in msg or "区域" in msg:
            return deny("area", msg)
        if "大会员" in msg or "会员" in msg:
            return deny("vip", msg)
        if any(k in msg for k in ("付费", "购买", "承包", "用券")):
            return deny("pay", msg)
        return deny("other", msg or f"code {r.get('code')}")
    res = r.get("result") or r.get("data") or {}
    if not isinstance(res, dict):
        return deny("other", "没有 result")
    if res.get("is_drm"):
        return deny("drm")
    if res.get("is_preview") in (1, True):
        # 只给了试看：大会员专享的说要大会员，别的说要付费；试看片段不拿来放
        return deny("vip" if int(res.get("status") or 0) == 13 or res.get("vip_status") != 1 else "pay")
    durl = res.get("durl") or []
    fmt = str(res.get("format") or "")
    if not durl or not (durl[0] or {}).get("url") or len(durl) > 1 or (fmt and not fmt.startswith("mp4")):
        return deny("format")
    u = durl[0]["url"]
    try:
        exp = int(urllib.parse.parse_qs(urllib.parse.urlsplit(u).query).get("deadline", ["0"])[0])
    except Exception:
        exp = 0
    is_vip = res.get("vip_status") == 1
    need_vip = {int(x.get("quality")): bool(x.get("need_vip")) for x in (res.get("support_formats") or [])
                if isinstance(x, dict) and x.get("quality") is not None}
    descs = list(res.get("accept_description") or [])
    accept = []
    for i, q in enumerate(res.get("accept_quality") or []):
        try:
            q = int(q)
        except Exception:
            continue
        vip_only = need_vip.get(q, False) and not is_vip
        accept.append({"qn": q, "desc": descs[i] if i < len(descs) else str(q), "vip_only": vip_only})
    return {"qn": res.get("quality"), "format": fmt, "url": u,
            "backup_urls": list(durl[0].get("backup_url") or []),
            "expires_at": exp or int(time.time()) + 3600, "size": durl[0].get("size"),
            "timelength": res.get("timelength"), "accept": accept, "vip": is_vip}


def norm_pgc_card(r, from_follow=False):
    """搜索结果（media_bangumi / media_ft）或追番列表的一条 → 列表项。点了进 bili:ss<id>（服务器挑那一集）。"""
    r = r or {}
    sid = r.get("season_id")
    badge = r.get("badge") or ""
    if not badge and isinstance(r.get("badges"), list) and r["badges"]:
        badge = (r["badges"][0] or {}).get("text") or ""
    new_ep = r.get("new_ep") or {}
    out = {"season_id": sid, "media_id": r.get("media_id"), "title": _strip_em(r.get("title")),
           "type_name": str(r.get("season_type_name") or PGC_TYPES.get(r.get("season_type"), "")),
           "cover_url": _https(r.get("cover")), "badge": str(badge),
           "index_show": str(new_ep.get("index_show") or r.get("index_show") or ""),
           "eps": r.get("ep_size") or r.get("total_count") or len(r.get("eps") or []) or None,
           "film": f"bili:ss{sid}" if sid else "", "pgc": True}
    if from_follow:
        out["progress"] = str(r.get("progress") or "")
    else:
        out["areas"] = str(r.get("areas") or "")
        out["desc"] = _cut_plain(r.get("desc"), 80)
    return out


def _cut_plain(s, n):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[:n] + "…"


class BiliClient:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Referer": "https://www.bilibili.com/",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Origin": "https://www.bilibili.com",
        })
        self.cred = _load_json(CRED_PATH, {})
        self.cred.setdefault("cookies", {})
        self._wbi = None

    # ── 凭据 ───────────────────────────────────────────────

    def _save(self):
        _save_private(CRED_PATH, self.cred)

    @property
    def cookies(self):
        return self.cred.get("cookies") or {}

    def logged_in(self):
        return bool(self.cookies.get("SESSDATA"))

    def _csrf(self):
        return self.cookies.get("bili_jct", "")

    def _ensure_buvid(self):
        """buvid3/buvid4 是 B 站的浏览器指纹 cookie，没有它 web 接口容易 412。
        匿名向 finger/spi 领一次，存进凭据文件里的 cookies（不算秘密，但也只存在这里）。"""
        if self.cookies.get("buvid3") and self.cookies.get("buvid4"):
            return
        try:
            r = self._send("GET", f"{API}/x/frontend/finger/spi", timeout=10, cookie=False)
            d = r.get("data") or {}
            if d.get("b_3"):
                self.cred["cookies"]["buvid3"] = d["b_3"]
            if d.get("b_4"):
                self.cred["cookies"]["buvid4"] = d["b_4"]
            self._save()
        except Exception:
            pass

    def _cookie_header(self):
        self._ensure_buvid()
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items() if v)

    # ── 签名 ───────────────────────────────────────────────

    def _wbi_keys(self):
        """从 nav 拿 img/sub key（不登录也给），按天缓存。"""
        if self._wbi and self._wbi.get("day") == time.strftime("%Y-%m-%d"):
            return self._wbi
        cache_path = os.path.join(CACHE_DIR, "wbi.json")
        c = _load_json(cache_path, {})
        if c.get("day") == time.strftime("%Y-%m-%d") and c.get("img"):
            self._wbi = c
            return c
        r = self._raw_get(f"{API}/x/web-interface/nav")
        w = ((r.get("data") or {}).get("wbi_img") or {})
        img = os.path.basename(urllib.parse.urlparse(w.get("img_url", "")).path).split(".")[0]
        sub = os.path.basename(urllib.parse.urlparse(w.get("sub_url", "")).path).split(".")[0]
        if not (img and sub):
            raise RuntimeError("nav 没给 wbi key")
        c = {"img": img, "sub": sub, "day": time.strftime("%Y-%m-%d")}
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(c, f)
        self._wbi = c
        return c

    def _wbi_sign(self, params):
        k = self._wbi_keys()
        raw = k["img"] + k["sub"]
        mixin = "".join(raw[i] for i in _WBI_TAB if i < len(raw))[:32]
        p = {key: re.sub(r"[!'()*]", "", str(v)) for key, v in params.items()}
        p["wts"] = str(int(time.time()))
        query = urllib.parse.urlencode(sorted(p.items()), quote_via=urllib.parse.quote)
        p["w_rid"] = _md5(query + mixin)
        return p

    @staticmethod
    def _app_sign(params, appsec=TV_APPSEC):
        p = {k: str(v) for k, v in params.items()}
        query = "&".join(f"{k}={p[k]}" for k in sorted(p))
        p["sign"] = _md5(query + appsec)
        return p

    # ── 下层跑腿 ───────────────────────────────────────────

    def _send(self, method, url, params=None, data=None, headers=None, timeout=15,
              expect="json", cookie=True, allow_redirects=True):
        """唯一真正发请求的地方（护栏）。顺序：
        写入白名单（不在单子上的 POST 直接拒，什么都不发）→ 暂停标记（暂停着就不发）→ 本通道每小时预算
        → 发；只有连接断开/超时这种传输错误、而且只有 GET/HEAD 才重试一次，写操作永不重试
        → 看回来的是不是风控（HTTP 401/403/412/429、code -352/-412/-509/-403、v_voucher、验证码字样），是就写暂停。
        expect="json" 回解析好的 dict（非 JSON 回 {"code": HTTP 码, "message": ..}）；"raw" 回 Response。
        cookie 只发给 *.bilibili.com，字幕 / 封面 CDN 不带。做法参考 dsh-bili-humanism lib/network.mjs（MIT）。"""
        method = method.upper()
        guard.check_write(method, url)
        p = guard.read_pause()
        if p:
            raise guard.Paused(guard.human_pause(p), p)
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        endpoint = f"{host}{parts.path}"
        h = dict(headers or {})
        if cookie and (host == "bilibili.com" or host.endswith(".bilibili.com")):
            h["Cookie"] = self._cookie_header()
        attempts = 2 if method in guard.SAFE_METHODS else 1
        if method == "POST":
            guard.spend_write(parts.path)      # AI 手上的写入小额度（点赞＋收藏＋关注）
        r = None
        for i in range(attempts):
            guard.spend()
            try:
                r = self.s.request(method, url, params=params, data=data, headers=h, timeout=timeout,
                                   allow_redirects=allow_redirects)
                break
            except (requests.ConnectionError, requests.Timeout):
                if i + 1 >= attempts:
                    raise
                time.sleep(0.5)
        if r.status_code in guard.RISK_HTTP:
            p = guard.set_pause(endpoint, f"HTTP {r.status_code}")
            raise guard.Paused(guard.human_pause(p), p)
        ctype = (r.headers.get("Content-Type") or "").lower()
        if expect == "raw":
            if "text/html" in ctype:
                sig = guard.risk_of_text(r.text)
                if sig:
                    p = guard.set_pause(endpoint, sig)
                    raise guard.Paused(guard.human_pause(p), p)
            return r
        try:
            j = r.json()
        except Exception:
            sig = guard.risk_of_text(r.text)
            if sig:
                p = guard.set_pause(endpoint, sig)
                raise guard.Paused(guard.human_pause(p), p)
            return {"code": r.status_code, "message": f"非 JSON 响应 HTTP {r.status_code}"}
        sig = guard.risk_of_json(j)
        if sig:
            p = guard.set_pause(endpoint, sig)
            raise guard.Paused(guard.human_pause(p), p)
        return j if isinstance(j, dict) else {"code": -1, "message": "返回不是对象"}

    def _raw_get(self, url, params=None, timeout=15, cookie=True):
        return self._send("GET", url, params=params, timeout=timeout, cookie=cookie)

    def _get(self, path, params=None, wbi=False, need_login=False, base=API):
        """返回 data；失败返回 {"error":..,"code":..}。base 换成 LIVE 就问直播的接口。"""
        if need_login and not self.logged_in():
            return dict(NOT_LOGGED_IN)
        params = dict(params or {})
        try:
            if wbi:
                params = self._wbi_sign(params)
            r = self._raw_get(f"{base}{path}", params)
        except guard.BiliStop as e:
            return {"error": e.human, "code": e.code}
        except requests.RequestException as e:
            return {"error": f"网络错误: {type(e).__name__}", "code": -1}
        except Exception as e:
            return {"error": str(e), "code": -1}
        if r.get("code") != 0:
            if r.get("code") == -101:
                return dict(NOT_LOGGED_IN)
            return {"error": r.get("message") or "未知错误", "code": r.get("code")}
        return r.get("data") if r.get("data") is not None else {}

    def _post(self, path, data, base=API, need_login=True):
        if need_login and not self.logged_in():
            return dict(NOT_LOGGED_IN)
        data = dict(data)
        if need_login:
            data["csrf"] = self._csrf()
        try:
            j = self._send("POST", f"{base}{path}", data=data, timeout=15)
        except guard.BiliStop as e:
            return {"error": e.human, "code": e.code}
        except requests.RequestException as e:
            return {"error": f"网络错误（写操作不重试）: {type(e).__name__}", "code": -1}
        except Exception as e:
            return {"error": f"出错: {type(e).__name__}", "code": -1}
        if j.get("code") != 0:
            if j.get("code") == -101:
                return dict(NOT_LOGGED_IN)
            return {"error": j.get("message") or "未知错误", "code": j.get("code")}
        return j.get("data") if j.get("data") is not None else {"ok": True}

    # ── 登录（TV 扫码）───────────────────────────────────────

    def login_qr(self):
        """向 TV 端申请 auth_code，把二维码画成 png 放进 BILITUBE_IMAGE_DIR。
        返回 {"png": 路径, "auth_code": ..., "url": ...}。"""
        params = self._app_sign({"appkey": TV_APPKEY, "local_id": "0", "ts": str(int(time.time()))})
        try:
            j = self._send("POST", f"{PASSPORT}/x/passport-tv-login/qrcode/auth_code",
                           data=params, timeout=15, cookie=False)
        except guard.BiliStop as e:
            return {"error": e.human, "code": e.code}
        except Exception as e:
            return {"error": f"申请二维码失败: {e}", "code": -1}
        if j.get("code") != 0:
            return {"error": j.get("message"), "code": j.get("code")}
        d = j["data"]
        try:
            import segno
        except ImportError:
            return {"error": "缺 segno（pip install segno）", "code": -1}
        os.makedirs(UPLOADS, exist_ok=True)
        png = os.path.join(UPLOADS, f"att-bili-qr-{int(time.time())}.png")
        segno.make(d["url"], error="m").save(png, scale=8, border=2)
        return {"png": png, "auth_code": d["auth_code"], "url": d["url"],
                "hint": "用 B 站 App 扫这张图；扫完用 login(action=\"status\") 确认"}

    def login_poll(self, auth_code):
        """轮询一次。返回 {"status": "ok"|"waiting"|"scanned"|"expired", ...}。"""
        params = self._app_sign({"appkey": TV_APPKEY, "auth_code": auth_code,
                                 "local_id": "0", "ts": str(int(time.time()))})
        try:
            j = self._send("POST", f"{PASSPORT}/x/passport-tv-login/qrcode/poll",
                           data=params, timeout=15, cookie=False)
        except guard.BiliStop as e:
            return {"status": "error", "error": e.human}
        except Exception as e:
            return {"status": "error", "error": type(e).__name__}
        code = j.get("code")
        if code == 86039:
            return {"status": "waiting"}
        if code == 86090:
            return {"status": "scanned", "hint": "已扫，等 TA 在手机上点确认"}
        if code == 86038:
            return {"status": "expired", "error": "二维码过期了，重新出码"}
        if code != 0:
            return {"status": "error", "error": j.get("message"), "code": code}
        return self._accept_token(j["data"])

    def _accept_token(self, d):
        cookies = dict(self.cookies)
        for c in ((d.get("cookie_info") or {}).get("cookies") or []):
            if c.get("name") and c.get("value") is not None:
                cookies[c["name"]] = c["value"]
        self.cred.update({
            "access_token": d.get("access_token", ""),
            "refresh_token": d.get("refresh_token", ""),
            "mid": d.get("mid") or int(cookies.get("DedeUserID") or 0),
            "expires_at": int(time.time()) + int(d.get("expires_in") or 0),
            "cookies": cookies,
            "logged_at": int(time.time()),
        })
        self._save()
        nav = self._get("/x/web-interface/nav")
        if "error" not in nav:
            self.cred["uname"] = nav.get("uname", "")
            self.cred["mid"] = nav.get("mid") or self.cred["mid"]
            self._save()
        return {"status": "ok", "uname": self.cred.get("uname", ""), "mid": self.cred.get("mid")}

    def login_wait(self, auth_code, timeout=120, interval=3):
        """每 3 秒轮询一次直到登录 / 过期 / 超时。"""
        deadline = time.time() + timeout
        last = {"status": "waiting"}
        while time.time() < deadline:
            last = self.login_poll(auth_code)
            if last["status"] in ("ok", "expired", "error"):
                return last
            time.sleep(interval)
        return {"status": "timeout", "error": f"{timeout}s 内没等到 TA 扫码/确认", "last": last.get("status")}

    def status(self):
        """暂停着就不去问 nav（一个请求都不发），只说本地知道的。"""
        p = guard.read_pause()
        if not self.logged_in():
            out = {"logged_in": False, "hint": NOT_LOGGED_IN["hint"]}
            if p:
                out["paused"] = p
            return out
        out = {"logged_in": True, "uname": self.cred.get("uname", ""), "mid": self.cred.get("mid"),
               "token_expires_at": _when(self.cred.get("expires_at", 0)),
               "has_access_token": bool(self.cred.get("access_token"))}
        if p:
            out.update({"paused": p, "valid": None})
            return out
        nav = self._get("/x/web-interface/nav")
        if "error" in nav:
            out.update({"valid": False, "error": nav["error"], "error_code": nav.get("code")})
        else:
            out.update({"valid": True, "uname": nav.get("uname", out["uname"]),
                        "face": _https(nav.get("face")),
                        "level": (nav.get("level_info") or {}).get("current_level"),
                        "coins": nav.get("money"), "vip": bool(nav.get("vipStatus"))})
            if nav.get("uname") and nav.get("uname") != self.cred.get("uname"):
                self.cred["uname"] = nav["uname"]
                self._save()
        return out

    def refresh(self):
        """用 refresh_token 续期（TV 端 h5/refresh；不认就退回 passport-login/oauth2/refresh_token）。"""
        if not self.cred.get("refresh_token"):
            return dict(NOT_LOGGED_IN)
        base = {"access_key": self.cred.get("access_token", ""), "access_token": self.cred.get("access_token", ""),
                "refresh_token": self.cred["refresh_token"], "appkey": TV_APPKEY, "ts": str(int(time.time()))}
        tried = []
        for path in ("/x/passport-tv-login/h5/refresh", "/x/passport-login/oauth2/refresh_token"):
            try:
                j = self._send("POST", f"{PASSPORT}{path}", data=self._app_sign(base), timeout=15, cookie=False)
            except guard.BiliStop as e:
                return {"error": e.human, "code": e.code}
            except Exception as e:
                tried.append(f"{path}: {type(e).__name__}")
                continue
            if j.get("code") == 0 and j.get("data"):
                d = j["data"]
                if "token_info" in d:
                    d = dict(d["token_info"], cookie_info=d.get("cookie_info"))
                r = self._accept_token(d)
                r["via"] = path
                return r
            tried.append(f"{path}: code={j.get('code')} {j.get('message')}")
        return {"error": "续期没成", "code": -1, "tried": tried,
                "hint": "过期了就重新扫码登录"}

    # ── 退出登录（只由页面上人点「退出」走到这里；MCP 工具里没有这条路）───────────

    def logout(self, timeout=8):
        """先尽力让 B 站那头作废这串登录（网页端 passport login/exit/v2，带 cookie 和 bili_jct），
        再**无论那头成没成**都清掉本地凭据。那头调不通、超时、撞风控、暂停着，都不挡本地清除。
        TV 端扫码拿到的 access_token 没有公开的作废接口，只在本地删掉，到期自然失效。
        回 {"ok": True, "local_cleared": True, "remote": "ok"|"unconfirmed"|"skipped", "note": 一句人话}。"""
        remote, note = "skipped", ""
        if not self.logged_in():
            note = "本地本来就没登录"
        elif guard.read_pause():
            note = "风控暂停着，没去 B 站那头"
        else:
            try:
                j = self._send("POST", f"{PASSPORT}/login/exit/v2", data={"biliCSRF": self._csrf()}, timeout=timeout)
                if j.get("code") == 0:
                    remote, note = "ok", "B 站那头也退出了"
                else:
                    remote, note = "unconfirmed", f"B 站回了 {j.get('code')}"
            except guard.Paused:
                remote, note = "unconfirmed", "撞到风控，已整套暂停"
            except guard.BiliStop as e:
                remote, note = "unconfirmed", e.human
            except requests.RequestException as e:
                remote, note = "unconfirmed", f"没连上 B 站（{type(e).__name__}）"
            except Exception as e:                  # 只留异常类名：报错串里可能带请求细节
                remote, note = "unconfirmed", f"出错（{type(e).__name__}）"
        self.clear_login()
        return {"ok": True, "local_cleared": True, "remote": remote, "note": note}

    def clear_login(self):
        """凭据文件整个清空（cookie、token、mid、uname 都不留，浏览器指纹 buvid 下次匿名重领），
        内存里跟账号有关的缓存一起丢：之后 status()/logged_in() 如实是没登录。"""
        self.cred = {"cookies": {}}
        self._save()
        for k in ("_pgc_cache", "_pgc_ep_sid"):     # 番剧信息里带着这个号的「上次看到」
            if hasattr(self, k):
                getattr(self, k).clear()

    # ── 视频 ───────────────────────────────────────────────

    def resolve(self, vid):
        """BV / av / 纯数字 / 各种链接 / b23.tv 短链 → {"bvid":..} 或 {"aid":..}。"""
        s = str(vid).strip()
        if "b23.tv" in s or "bili2233.cn" in s:
            url = s if s.startswith("http") else "https://" + s
            try:
                r = self._send("HEAD", url, allow_redirects=False, timeout=10, expect="raw", cookie=False)
                loc = r.headers.get("Location", "")
                if loc:
                    s = loc
            except guard.BiliStop as e:
                return {"error": e.human, "code": e.code}
            except Exception as e:
                return {"error": f"短链跳转失败: {type(e).__name__}"}
        m = _BV_RE.search(s)
        if m:
            return {"bvid": m.group(1)}
        pg = pgc_ref(s)
        if pg:
            return {"pgc": pg}
        m = _AV_RE.search(s)
        if m:
            return {"aid": int(m.group(1))}
        if s.isdigit():
            return {"aid": int(s)}
        return {"error": f"认不出这个视频 id: {vid}"}

    # ── 番剧 / 影视（PGC）────────────────────────────────────
    _PGC_TTL = 300

    def _get_result(self, path, params):
        """pgc 那几个接口把东西放在 result 里（不是 data）；失败同 _get 的形状。"""
        try:
            r = self._raw_get(f"{API}{path}", params)
        except guard.BiliStop as e:
            return {"error": e.human, "code": e.code}
        except requests.RequestException as e:
            return {"error": f"网络错误: {type(e).__name__}", "code": -1}
        except Exception as e:
            return {"error": str(e), "code": -1}
        if r.get("code") != 0:
            if r.get("code") == -101:
                return dict(NOT_LOGGED_IN)
            if r.get("code") == -404:
                return {"error": "B 站说没有这部（或者这个地区看不到）", "code": -404}
            return {"error": r.get("message") or "未知错误", "code": r.get("code")}
        res = r.get("result")
        if res is None:
            res = r.get("data")
        return res if isinstance(res, dict) else {}

    def pgc_season(self, kind, n, fresh=False):
        """ep / ss / md 号 → 整部的信息（norm_pgc_season 的样子）。五分钟内同一部不重复问；fresh 强制现问（要「上次看到」时）。"""
        if not hasattr(self, "_pgc_cache"):
            self._pgc_cache, self._pgc_ep_sid = {}, {}
        kind, n = str(kind).lower(), int(n)
        if kind == "md":
            hit = self._pgc_cache.get(("md", n))
            if hit and not fresh and time.time() - hit[0] < 86400:
                kind, n = "ss", hit[1]
            else:
                d = self._get("/pgc/review/user", {"media_id": n})
                if "error" in d:
                    return d
                sid = ((d.get("media") or {}).get("season_id")) if isinstance(d, dict) else None
                if not sid:
                    d2 = self._get_result("/pgc/review/user", {"media_id": n})
                    sid = ((d2.get("media") or {}).get("season_id")) if isinstance(d2, dict) else None
                if not sid:
                    return {"error": f"md{n} 没找到对应的番", "code": -404}
                self._pgc_cache[("md", n)] = (time.time(), int(sid))
                kind, n = "ss", int(sid)
        sid = n if kind == "ss" else self._pgc_ep_sid.get(n)
        if sid and not fresh:
            hit = self._pgc_cache.get(sid)
            if hit and time.time() - hit[0] < self._PGC_TTL:
                return hit[1]
        res = self._get_result("/pgc/view/web/season", {"season_id": n} if kind == "ss" else {"ep_id": n})
        if "error" in res:
            return res
        s = norm_pgc_season(res)
        if not s.get("season_id"):
            return {"error": "B 站没给这部的信息", "code": -1}
        if kind == "ss" and str(s["season_id"]) != str(n):
            return {"error": f"B 站回来的番对不上：要 ss{n}，回的是 ss{s['season_id']}，当没拿到", "code": "mismatch"}
        if kind == "ep" and not any(str(e["ep_id"]) == str(n) for e in s["episodes"]):
            # 预告/花絮这类不在正片列表里的 ep：不硬凑
            return {"error": f"ep{n} 不在《{s['title']}》的正片列表里（可能是预告或花絮）", "code": -404}
        self._pgc_cache[s["season_id"]] = (time.time(), s)
        for e in s["episodes"]:
            self._pgc_ep_sid[int(e["ep_id"])] = s["season_id"]
        return s

    def _pgc_view(self, kind, n):
        """番剧的一集装成 wbi/view 的样子（bvid/aid/cid/pages/...），这样评论、弹幕、字幕的代码可以照用，
        也不用拿番剧的 BV 号再去问一遍 view。另带 "pgc" 一块（番名、第几话、整部几集）。"""
        s = self.pgc_season(kind, n)
        if "error" in s:
            return s
        ep = pgc_pick_ep(s, n if kind == "ep" else None)
        if not ep:
            return {"error": f"《{s['title']}》还没有能看的正片", "code": -404}
        return {"bvid": ep["bvid"], "aid": ep["aid"], "cid": ep["cid"],
                "title": pgc_film_title(s["title"], ep) + (f" {ep['long_title']}" if ep["long_title"] else ""),
                "duration": ep["duration"], "pic": ep["cover"] or s["cover"],
                "owner": {"name": s["type_name"], "mid": None}, "pubdate": None,
                "stat": {"view": s.get("views"), "danmaku": s.get("danmakus")},
                "tname": s["type_name"], "desc": s.get("evaluate") or "",
                "pages": [{"cid": ep["cid"], "part": ep["long_title"] or ep["index"], "duration": ep["duration"]}],
                "pgc": {"season_id": s["season_id"], "media_id": s.get("media_id"), "season_title": s["title"],
                        "type_name": s["type_name"], "total": s["total"], "is_finish": s["is_finish"],
                        "ep_id": ep["ep_id"], "index": ep["index"], "long_title": ep["long_title"],
                        "badge": ep["badge"], "area_limited": ep["area_limited"], "status": ep["status"],
                        "url": f"https://www.bilibili.com/bangumi/play/ep{ep['ep_id']}"}}

    def pgc_playurl(self, ep_id, cid, qn=80):
        """番剧一集的渐进式 mp4（fnval=1，单条 mp4，<video> 直接放）。清晰度：只按 B 站这次给的档，
        qn 是想要的上限，B 站给不了更高的就回它给得了的；不拼地址、不另请求账号没有的档。"""
        try:
            r = self._send("GET", f"{API}/pgc/player/web/playurl",
                           params={"ep_id": int(ep_id), "cid": int(cid), "qn": int(qn or 80),
                                   "fnval": 1, "fnver": 0, "fourk": 0})
        except guard.BiliStop as e:
            return {"error": e.human, "code": e.code}
        except requests.RequestException as e:
            return {"error": f"网络错误: {type(e).__name__}", "code": -1}
        if r.get("code") == -101:
            return dict(NOT_LOGGED_IN)
        out = norm_pgc_play(r)
        out.update({"ep_id": int(ep_id), "cid": int(cid)})
        return out

    def pgc_search(self, keyword, n=6):
        """搜番剧和影视（各一页，各最多 n 条）。和普通视频的搜索分开问，结果在列表里另起一段。"""
        out = {"keyword": keyword, "bangumi": [], "ft": []}
        for st, key in (("media_bangumi", "bangumi"), ("media_ft", "ft")):
            d = self._get("/x/web-interface/wbi/search/type",
                          {"search_type": st, "keyword": keyword, "page": 1}, wbi=True)
            if "error" in d:
                if d.get("code") in (guard.Paused.code, guard.OverBudget.code):
                    return d
                out[key + "_error"] = d.get("error")
                continue
            out[key] = [norm_pgc_card(r) for r in (d.get("result") or [])[:max(1, int(n))] if r.get("season_id")]
        return out

    def pgc_follow(self, kind=1, page=1, n=15):
        """TA 的追番（kind=1）/ 追剧（kind=2）列表，只读。追番/取消追番是写入，这里不做。"""
        if not self.logged_in():
            return dict(NOT_LOGGED_IN)
        kind = 2 if int(kind or 1) == 2 else 1
        page, n = max(1, int(page or 1)), max(1, min(30, int(n or 15)))
        d = self._get("/x/space/bangumi/follow/list",
                      {"type": kind, "follow_status": 0, "pn": page, "ps": n, "vmid": self.cred.get("mid")},
                      need_login=True)
        if "error" in d:
            return d
        rows = [norm_pgc_card(r, from_follow=True) for r in (d.get("list") or []) if r.get("season_id")]
        total = int(d.get("total") or 0)
        return {"kind": kind, "page": page, "total": total, "count": len(rows),
                "has_more": page * n < total, "list": rows}

    def _view(self, key):
        if "pgc" in key:
            return self._pgc_view(*key["pgc"])
        # 只走 WBI 签名版：撞风控就停，不退回普通版 /view（退回去等于换个法子再撞一次）。
        d = self._get("/x/web-interface/wbi/view", key, wbi=True)
        if "error" in d:
            return d
        # 身份核对：回来的不是要的那部就当没拿到（参考 dsh-bili-humanism reader.mjs 的 SUBTITLE_SOURCE_MISMATCH）
        for k in ("bvid", "aid"):
            if key.get(k) is not None and d.get(k) is not None and str(d.get(k)) != str(key[k]):
                return {"error": f"B 站回来的视频信息对不上：要 {k}={key[k]}，回的是 {d.get(k)}，当没拿到",
                        "code": "mismatch"}
        return d

    def video(self, vid, cover=True, full_desc=False):
        key = self.resolve(vid)
        if "error" in key:
            return key
        d = self._view(key)
        if "error" in d:
            return d
        st = d.get("stat") or {}
        own = d.get("owner") or {}
        out = {
            "title": d.get("title"), "bvid": d.get("bvid"), "aid": d.get("aid"), "cid": d.get("cid"),
            "up": own.get("name"), "up_mid": own.get("mid"),
            "duration": _mmss(d.get("duration")), "duration_sec": d.get("duration"),
            "pubdate": _when(d.get("pubdate")), "pubdate_ts": d.get("pubdate"),
            "view": st.get("view"), "like": st.get("like"), "coin": st.get("coin"), "fav": st.get("favorite"),
            "danmaku": st.get("danmaku"), "reply": st.get("reply"), "share": st.get("share"),
            "tname": d.get("tname"), "pages": len(d.get("pages") or []),
            "desc": (d.get("desc") or "").strip() if full_desc else (d.get("desc") or "").strip()[:300],
            "desc_len": len((d.get("desc") or "").strip()),
            "cover_url": d.get("pic"),
            "url": f"https://www.bilibili.com/video/{d.get('bvid')}",
        }
        if d.get("pgc"):                      # 番剧的一集：番名、第几话、整部几集；链接给番剧页
            out["pgc"] = d["pgc"]
            out["url"] = d["pgc"]["url"]
        elif "/bangumi/play/" in str(d.get("redirect_url") or ""):
            out["redirect_url"] = d["redirect_url"]     # 这个 BV 其实是番剧的一集
        if cover and d.get("pic") and d.get("bvid"):
            out["cover"] = out["cover_path"] = self._download_cover(d["pic"], d["bvid"])
        return out

    def _download_cover(self, url, bvid):
        """封面存成 att-bili-cover-<bvid>.jpg（有就复用）。CDN 偶尔失败：缩图版不行就退回原图，各重试一次。"""
        path = os.path.join(UPLOADS, f"att-bili-cover-{bvid}.jpg")
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return path
        u = url.replace("http://", "https://", 1)
        cands = [u] if "@" in u else [u + "@640w_400h_1c.jpg", u]
        for cand in cands:
            for _ in range(2):
                try:
                    r = self._send("GET", cand, timeout=15, expect="raw", cookie=False)
                    if r.ok and r.content and r.headers.get("Content-Type", "").startswith("image"):
                        os.makedirs(UPLOADS, exist_ok=True)
                        tmp = path + ".part"
                        with open(tmp, "wb") as f:
                            f.write(r.content)
                        os.replace(tmp, path)
                        return path
                except guard.BiliStop:
                    return None              # 暂停 / 预算到了：封面不要了，别再试
                except Exception:
                    pass
                time.sleep(0.3)
        return None

    def _aid_of(self, vid):
        key = self.resolve(vid)
        if "error" in key:
            return None, key
        if "aid" in key:
            return key["aid"], None
        d = self._view(key)
        if "error" in d:
            return None, d
        return d.get("aid"), None

    # ── 评论区 ──────────────────────────────────────────────

    @staticmethod
    def _fmt_reply(c, subs=3):
        m = c.get("member") or {}
        out = {"rpid": c.get("rpid"), "uname": m.get("uname"), "mid": c.get("mid"),
               "like": c.get("like"), "ctime": _when(c.get("ctime")),
               "message": (c.get("content") or {}).get("message", ""),
               "reply_count": c.get("rcount") or c.get("count") or 0}
        if subs and c.get("replies"):
            out["sub"] = [{"uname": (r.get("member") or {}).get("uname"),
                           "message": (r.get("content") or {}).get("message", ""),
                           "like": r.get("like")} for r in c["replies"][:subs]]
        return out

    def comments(self, vid, sort="hot", page=1, ps=20, next_offset=None):
        """评论区。首页走 WBI 版 /x/v2/reply/wbi/main（海外服务器匿名实测：普通 /main 回空、
        wbi/main 有货）；翻页两条路：带回来的 next_offset 游标继续 wbi/main，
        或 page>1 走老接口 /x/v2/reply?pn=（按页码）。匿名只给前几条，登录后齐全。"""
        aid, err = self._aid_of(vid)
        if err:
            return err
        mode = 2 if sort == "time" else 3
        page = int(page or 1)
        if page > 1 and not next_offset:
            d = self._get("/x/v2/reply", {"oid": aid, "type": 1, "sort": 0 if sort == "time" else 2,
                                          "pn": page, "ps": ps})
            if "error" in d:
                return d
            pg = d.get("page") or {}
            out = {"aid": aid, "sort": sort, "page": page, "total": pg.get("count"),
                   "is_end": page * pg.get("size", ps) >= (pg.get("count") or 0), "list": []}
            out["list"] = [self._fmt_reply(c) for c in (d.get("replies") or [])]
            return out
        pag = {"offset": next_offset} if next_offset else {"offset": ""}
        d = self._get("/x/v2/reply/wbi/main", {"oid": aid, "type": 1, "mode": mode,
                                               "pagination_str": json.dumps(pag),
                                               "plat": 1, "web_location": 1315875}, wbi=True)
        if "error" in d:
            return d
        cur = d.get("cursor") or {}
        out = {"aid": aid, "sort": sort, "page": page,
               "total": cur.get("all_count"), "is_end": cur.get("is_end"),
               "next_offset": (cur.get("pagination_reply") or {}).get("next_offset"), "list": []}
        for t in (d.get("top_replies") or []):
            r = self._fmt_reply(t)
            r["top"] = True
            out["list"].append(r)
        out["list"] += [self._fmt_reply(c) for c in (d.get("replies") or [])]
        if not self.logged_in() and out["total"] and len(out["list"]) < out["total"]:
            out["note"] = "未登录只能看到前几条，登录后齐全"
        return out

    def replies(self, vid, rpid, pn=1, ps=20):
        """楼中楼。"""
        aid, err = self._aid_of(vid)
        if err:
            return err
        d = self._get("/x/v2/reply/reply", {"oid": aid, "type": 1, "root": rpid, "pn": pn, "ps": ps})
        if "error" in d:
            return d
        root = d.get("root") or {}
        return {"aid": aid, "root": self._fmt_reply(root, subs=0) if root else None,
                "page": pn, "total": (d.get("page") or {}).get("count"),
                "list": [self._fmt_reply(c, subs=0) for c in (d.get("replies") or [])]}

    def post_comment(self, vid, text):
        """关掉。函数留着，调用即拒，不解析视频、不发请求；
        就算有人绕过这里直接 _post("/x/v2/reply/add")，传输层的白名单也会拒。"""
        return guard.refuse("发评论")

    # ── 互动 ───────────────────────────────────────────────

    def like(self, vid, on=True):
        """点赞 / 取消。
        走白名单里的 archive/like；写操作不重试、撞风控照样整套暂停、MCP 通道计入写入小额度。"""
        aid, err = self._aid_of(vid)
        if err:
            return err
        d = self._post("/x/web-interface/archive/like", {"aid": aid, "like": 1 if on else 2})
        if "error" in d:
            return d
        return {"ok": True, "aid": aid, "liked": bool(on)}

    def coin(self, vid, n=1, also_like=False):
        """关掉（不投币）。调用即拒；coin/add 也不在传输层白名单里。"""
        return guard.refuse("投币")

    def is_liked(self, vid):
        aid, err = self._aid_of(vid)
        if err:
            return err
        d = self._get("/x/web-interface/archive/has/like", {"aid": aid}, need_login=True)
        if isinstance(d, dict) and "error" in d:
            return d
        return {"aid": aid, "liked": d == 1}

    def is_faved(self, vid):
        aid, err = self._aid_of(vid)
        if err:
            return err
        d = self._get("/x/v2/fav/video/favoured", {"aid": aid}, need_login=True)
        if "error" in d:
            return d
        return {"aid": aid, "faved": bool(d.get("favoured")), "count": d.get("count")}

    def fav_folders(self):
        if not self.logged_in():
            return dict(NOT_LOGGED_IN)
        d = self._get("/x/v3/fav/folder/created/list-all", {"up_mid": self.cred.get("mid")}, need_login=True)
        if "error" in d:
            return d
        return {"list": [{"id": f.get("id"), "title": f.get("title"), "count": f.get("media_count"),
                          "default": bool(f.get("attr", 0) & 1 == 0 and i == 0)}
                         for i, f in enumerate(d.get("list") or [])]}

    def _folder_state(self, folder_id, aid):
        """写之前现查这个夹子：是不是 TA 的、是不是私密（attr&1）、这条视频在不在里面（fav_state）。
        做法参考 dsh-bili-humanism lib/writer.mjs 的 favorites 分支（MIT）。"""
        d = self._get("/x/v3/fav/folder/created/list-all",
                      {"up_mid": self.cred.get("mid"), "type": 2, "rid": aid}, need_login=True)
        if "error" in d:
            return d
        cur = next((f for f in (d.get("list") or []) if str(f.get("id")) == str(folder_id)), None)
        if not cur:
            return {"error": f"TA 的收藏夹里没有 {folder_id}", "code": "folder_denied"}
        if self.cred.get("mid") and cur.get("mid") is not None and str(cur.get("mid")) != str(self.cred.get("mid")):
            return {"error": f"夹子 {folder_id} 不是 TA 的", "code": "folder_denied"}
        attr = cur.get("attr")
        if not isinstance(attr, int):
            return {"error": f"夹子 {folder_id} 看不出私密没私密，不动", "code": "folder_denied"}
        return {"id": cur.get("id"), "title": cur.get("title"), "private": bool(attr & 1),
                "attr": attr, "faved": cur.get("fav_state") == 1}

    def fav(self, vid, folder_id=None, remove=False, by="user"):
        """by="companion"：AI 自己收，只能进配置里的「口袋」（AI 专用的私密收藏夹）；写之前核对它还是私密的，
        已经在里面就不再发请求。by="user"：用户明确让收（bilitube 页面上的按钮，或 TA 开口），
        folder_id 不给就进第一个（默认）夹子。"""
        if by == "companion":
            pocket = guard.pocket_id()
            if not pocket:
                return {"error": "没配口袋（AI 专用的私密收藏夹：config.json 的 pocket_folder_id 或 BILI_POCKET_FOLDER_ID），by=companion 收不了",
                        "code": "folder_denied"}
            if folder_id not in (None, "", 0) and int(folder_id) != pocket:
                return {"error": f"by=companion 只能进口袋（{pocket}），{folder_id} 不行；要进 TA 的夹子得 TA 开口，用 by=user",
                        "code": "folder_denied"}
            if not self.logged_in():
                return dict(NOT_LOGGED_IN)
            aid, err = self._aid_of(vid)
            if err:
                return err
            st = self._folder_state(pocket, aid)
            if "error" in st:
                return st
            if not st["private"]:
                return {"error": f"口袋「{st.get('title')}」现在不是私密的了（attr={st['attr']}），不往里放",
                        "code": "folder_denied"}
            if st["faved"] == (not remove):
                return {"ok": True, "aid": aid, "folder_id": pocket, "removed": bool(remove), "already": True,
                        "folder_title": st.get("title")}
            key = "del_media_ids" if remove else "add_media_ids"
            d = self._post("/x/v3/fav/resource/deal", {"rid": aid, "type": 2, key: pocket})
            if "error" in d:
                return d
            return {"ok": True, "aid": aid, "folder_id": pocket, "removed": bool(remove),
                    "folder_title": st.get("title")}
        aid, err = self._aid_of(vid)
        if err:
            return err
        if folder_id is None:
            fl = self.fav_folders()
            if "error" in fl:
                return fl
            if not fl["list"]:
                return {"error": "TA 一个收藏夹都没有", "code": -1}
            folder_id = fl["list"][0]["id"]
        key = "del_media_ids" if remove else "add_media_ids"
        d = self._post("/x/v3/fav/resource/deal", {"rid": aid, "type": 2, key: folder_id})
        if "error" in d:
            return d
        return {"ok": True, "aid": aid, "folder_id": folder_id, "removed": bool(remove)}

    def fav_list(self, media_id=None, n=20, page=1):
        """某个收藏夹里的视频（media_id 不给就取第一个夹子）。播放页的 B 站列表和 library favorites 用。"""
        if not self.logged_in():
            return dict(NOT_LOGGED_IN)
        title = None
        if media_id is None:
            fl = self.fav_folders()
            if "error" in fl:
                return fl
            if not fl["list"]:
                return {"error": "TA 一个收藏夹都没有", "code": -1}
            media_id, title = fl["list"][0]["id"], fl["list"][0]["title"]
        n = max(1, min(int(n), 40))
        d = self._get("/x/v3/fav/resource/list",
                      {"media_id": int(media_id), "pn": max(1, int(page)), "ps": n,
                       "keyword": "", "order": "mtime", "type": 0, "tid": 0, "platform": "web"},
                      need_login=True)
        if "error" in d:
            return d
        info = d.get("info") or {}
        out = []
        for m in (d.get("medias") or [])[:n]:
            up = m.get("upper") or {}
            cov = m.get("cover") or ""
            if cov.startswith("//"):
                cov = "https:" + cov
            out.append({"title": m.get("title"), "bvid": m.get("bvid"), "aid": m.get("id"),
                        "up": up.get("name"), "up_mid": up.get("mid"),
                        "duration": _mmss(m.get("duration")), "duration_sec": m.get("duration"),
                        "play": (m.get("cnt_info") or {}).get("play"),
                        "danmaku": (m.get("cnt_info") or {}).get("danmaku"),
                        "pubdate": _when(m.get("pubtime")), "cover_url": cov,
                        "face": _https(up.get("face")), "faved": True})
        return {"media_id": int(media_id), "title": info.get("title") or title,
                "total": info.get("media_count"), "has_more": bool(d.get("has_more")),
                "page": max(1, int(page)), "count": len(out), "list": out}

    # ── 关注（只给 AI 自己用：进专属分组、只取关分组里的人）──────────────
    # 关注写在登录账号上，会和用户自己的关注混在一起，所以 AI 关注的人一律放进配置里的专属分组
    # （guard.follow_group：follow_group_id，或按 follow_group_name 认），取关只认这个分组里的人。
    # 写之前现查关系（x/relation），已经是那样就不发写请求。只有 relation/modify（＋紧跟的 tags/addUsers）两条写，
    # 都在 guard 白名单里；MCP 通道另有每小时上限和最小间隔（follow_hourly_budget / follow_min_gap_s）。

    def follow_group(self):
        """专属分组此刻的样子 {"tagid", "name", "count"}；没配、B 站上找不到都回 error（code follow_denied）。"""
        gid, name = guard.follow_group()
        if not gid and not name:
            return {"error": "没配 AI 的关注分组（config.json 的 follow_group_id 或 follow_group_name），不关注",
                    "code": "follow_denied"}
        if not self.logged_in():
            return dict(NOT_LOGGED_IN)
        d = self._get("/x/relation/tags", need_login=True)
        if isinstance(d, dict) and "error" in d:
            return d
        tags = [t for t in (d if isinstance(d, list) else []) if isinstance(t, dict)]
        if gid:
            cur = next((t for t in tags if str(t.get("tagid")) == str(gid)), None)
        else:
            cur = next((t for t in tags if str(t.get("name") or "").strip() == name), None)
        if not cur or int(cur.get("tagid") or 0) <= 0:     # 默认分组 0、特别关注 -10 不算
            want = f" id {gid}" if gid else f"「{name}」"
            return {"error": f"B 站的关注分组里找不到{want}：先在 B 站建一个关注分组（建议就叫这个名字），或把它的 id 填进 follow_group_id",
                    "code": "follow_denied", "want": {"id": gid, "name": name}}
        return {"tagid": int(cur["tagid"]), "name": cur.get("name") or "", "count": int(cur.get("count") or 0)}

    def follow_list(self, n=20, page=1):
        """专属分组里的人（只读）：{"group", "total", "page", "has_more", "count", "list":[{mid, uname, face, sign}]}。"""
        g = self.follow_group()
        if "error" in g:
            return g
        n, page = max(1, min(int(n), 50)), max(1, int(page))
        d = self._get("/x/relation/tag", {"mid": self.cred.get("mid"), "tagid": g["tagid"], "pn": page, "ps": n},
                      need_login=True)
        if isinstance(d, dict) and "error" in d:
            return d
        rows = [{"mid": x.get("mid"), "uname": x.get("uname") or "", "face": _https(x.get("face")),
                 "sign": _cut_plain(x.get("sign"), 60)} for x in (d if isinstance(d, list) else []) if isinstance(x, dict)]
        return {"group": g, "total": g["count"], "page": page, "has_more": page * n < g["count"],
                "count": len(rows), "list": rows}

    def followings(self, n=30, page=1):
        """登录账号自己关注的人（只读，逛片页「关注」页的「关注列表」用）。按 B 站的「最常访问」排。
        {"total", "page", "has_more", "count", "list":[{mid, uname, face, sign}]}。不关注、不取关、不改分组。"""
        if not self.logged_in():
            return dict(NOT_LOGGED_IN)
        mid = self.cred.get("mid") or self.cookies.get("DedeUserID")
        if not mid:
            return dict(NOT_LOGGED_IN)
        n, page = max(1, min(int(n), 50)), max(1, min(int(page), 100))
        d = self._get("/x/relation/followings",
                      {"vmid": int(mid), "pn": page, "ps": n, "order": "desc", "order_type": "attention"},
                      need_login=True)
        if isinstance(d, dict) and "error" in d:
            return d
        rows = [{"mid": x.get("mid"), "uname": x.get("uname") or "", "face": _https(x.get("face")),
                 "sign": _cut_plain(x.get("sign"), 60)} for x in ((d or {}).get("list") or []) if isinstance(x, dict)]
        total = int((d or {}).get("total") or 0)
        return {"total": total, "page": page, "has_more": page * n < total and len(rows) > 0,
                "count": len(rows), "list": rows}

    def relation(self, mid):
        """和这个人的关系：{"following": 关注着没, "blocked": 拉黑没, "tags": [所在分组 id]}（只读）。"""
        d = self._get("/x/relation", {"fid": int(mid)}, need_login=True)
        if isinstance(d, dict) and "error" in d:
            return d
        attr = int((d or {}).get("attribute") or 0)
        return {"following": attr in (1, 2, 6), "blocked": attr == 128,
                "tags": [int(t) for t in ((d or {}).get("tag") or []) if str(t).lstrip("-").isdigit()]}

    def follow(self, mid, remove=False):
        """AI 关注（进专属分组）/ 取关（只取关专属分组里的人）。用户自己关注的人不动：
        关注时发现本来就关注着、但不在分组里，算用户的，不发请求；取关时不在分组里，同样拒。
        回 {"ok", "mid", "removed", "group", "already"?, "warn"?}；拒绝回 error（code follow_denied）。"""
        try:
            mid = int(str(mid).strip())
        except (TypeError, ValueError):
            return {"error": "mid 不对", "code": -1}
        if mid <= 0:
            return {"error": "mid 不对", "code": -1}
        if not self.logged_in():
            return dict(NOT_LOGGED_IN)
        if str(mid) == str(self.cred.get("mid") or self.cookies.get("DedeUserID") or ""):
            return {"error": "这是登录账号自己", "code": "follow_denied"}
        g = self.follow_group()
        if "error" in g:
            return g
        rel = self.relation(mid)
        if "error" in rel:
            return rel
        mine = g["tagid"] in rel["tags"]
        base = {"ok": True, "mid": mid, "removed": bool(remove), "group": g["name"], "tagid": g["tagid"]}
        if remove:
            if not rel["following"]:
                return dict(base, already=True)
            if not mine:
                return {"error": f"这个人不在「{g['name']}」里，是用户自己关注的，AI 取关不了", "code": "follow_denied"}
            d = self._post("/x/relation/modify", {"fid": mid, "act": 2, "re_src": 11})
            return d if "error" in d else base
        if rel["blocked"]:
            return {"error": "这个人在黑名单里，不关注", "code": "follow_denied"}
        if rel["following"]:
            if mine:
                return dict(base, already=True)
            return {"error": f"用户本来就关注着（不在「{g['name']}」里），算用户自己的，没动", "code": "follow_denied"}
        d = self._post("/x/relation/modify", {"fid": mid, "act": 1, "re_src": 11})
        if "error" in d:
            return d
        t = self._post("/x/relation/tags/addUsers", {"fids": str(mid), "tagids": str(g["tagid"])})
        if "error" in t:
            return dict(base, grouped=False,
                        warn=f"关注上了，但没放进「{g['name']}」（{t['error']}）：这个人现在在默认分组里，AI 取关不了，"
                             f"要用户自己在 B 站把 TA 挪进分组或取关")
        return dict(base, grouped=True)

    # ── 观看历史 ───────────────────────────────────────────

    def history(self, n=20, cursor=None):
        """cursor = 上一页返回的 {max, view_at, business}，不给就从头。"""
        if not self.logged_in():
            return dict(NOT_LOGGED_IN)
        q = {"ps": min(int(n), 30), "type": "all"}
        c = cursor if isinstance(cursor, dict) else {}
        for k in ("max", "view_at", "business"):
            if c.get(k) not in (None, "", 0):
                q[k] = c[k]
        d = self._get("/x/web-interface/history/cursor", q, need_login=True)
        if "error" in d:
            return d
        cur = d.get("cursor") or {}
        out = []
        for it in (d.get("list") or [])[:int(n)]:
            h = it.get("history") or {}
            biz = h.get("business")
            cov = it.get("cover") or ""
            if cov.startswith("//"):
                cov = "https:" + cov
            row = {"title": it.get("title"), "type": biz, "cover_url": cov,
                   "up": it.get("author_name"), "up_mid": it.get("author_mid"),
                   "face": _https(it.get("author_face")),
                   "view_at": _when(it.get("view_at")), "view_at_ts": it.get("view_at"),
                   "device": it.get("kid") and None}
            row.pop("device")
            if biz == "archive":
                row["bvid"] = h.get("bvid")
                row["aid"] = h.get("oid")
                if it.get("videos", 1) > 1 and h.get("part"):
                    row["part"] = h.get("part")
            elif biz == "pgc":
                row["bvid"] = h.get("bvid")
                row["epid"] = h.get("epid")
                row["episode"] = it.get("show_title") or it.get("long_title")
                if h.get("epid"):
                    row["film"] = f"bili:ep{h.get('epid')}"      # 点了进番剧播放，不当普通投稿
                    row["pgc"] = True
            elif biz == "live":
                row["room"] = h.get("oid")
            elif biz in ("article", "article-list"):
                row["cvid"] = h.get("cid") or h.get("oid")
            prog, dur = it.get("progress"), it.get("duration")
            if dur:
                row["progress"] = "看完" if prog == -1 else f"{_mmss(prog or 0)}/{_mmss(dur)}"
            out.append(row)
        nxt = {"max": cur.get("max"), "view_at": cur.get("view_at"), "business": cur.get("business")}
        return {"count": len(out), "cursor": nxt,
                "has_more": bool(out) and bool(nxt.get("max")), "list": out}

    # ── 弹幕 ───────────────────────────────────────────────

    @staticmethod
    def _pb_iter(buf):
        """最小 protobuf 解码：yield (field, wire_type, value)。只认 varint / 64bit / 长度前缀 / 32bit。"""
        i, n = 0, len(buf)

        def varint():
            nonlocal i
            r, sh = 0, 0
            while True:
                b = buf[i]; i += 1
                r |= (b & 0x7f) << sh; sh += 7
                if not b & 0x80:
                    return r
        while i < n:
            tag = varint()
            f, wt = tag >> 3, tag & 7
            if wt == 0:
                yield f, wt, varint()
            elif wt == 1:
                yield f, wt, buf[i:i + 8]; i += 8
            elif wt == 2:
                ln = varint(); yield f, wt, buf[i:i + ln]; i += ln
            elif wt == 5:
                yield f, wt, buf[i:i + 4]; i += 4
            else:
                return

    def _dm_seg(self, cid, seg_index):
        """seg.so 一段（6 分钟）的弹幕 → [(秒, 文本)]。DmSegMobileReply.elems=1；DanmakuElem.progress=2(ms) content=7。"""
        r = self._send("GET", f"{API}/x/v2/dm/web/seg.so",
                       params={"type": 1, "oid": cid, "segment_index": seg_index}, timeout=20, expect="raw")
        if r.status_code != 200 or not r.content.startswith(b"\n"):
            return None
        out = []
        for f, wt, elem in self._pb_iter(r.content):
            if f != 1 or wt != 2:
                continue
            t, text = None, None
            for ff, wtt, v in self._pb_iter(elem):
                if ff == 2 and wtt == 0:
                    t = v / 1000
                elif ff == 7 and wtt == 2:
                    text = v.decode("utf-8", "replace")
            if t is not None and text:
                out.append((t, text))
        return out

    def _dm_list(self, cid):
        """list.so 的 XML（deflate）→ [(秒, 文本)]。全量但有 maxlimit 上限，作 seg.so 的备胎。"""
        r = self._send("GET", f"{API}/x/v1/dm/list.so", params={"oid": cid}, timeout=20, expect="raw")
        raw = r.content
        if not raw.lstrip().startswith(b"<"):
            try:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            except Exception:
                raw = zlib.decompress(raw)
        xml = raw.decode("utf-8", "replace")
        import html
        return [(float(m.group(1).split(",")[0]), html.unescape(m.group(2)))
                for m in re.finditer(r'<d p="([^"]+)">(.*?)</d>', xml, re.S)]

    @staticmethod
    def _dm_norm(text):
        t = re.sub(r"[\s\W_]+", "", text.lower())
        t = re.sub(r"(.)\1{2,}", r"\1\1", t)   # 哈哈哈哈哈 → 哈哈
        return t or text.strip().lower()

    def danmaku(self, vid, t, window=3, limit=30):
        """t 前后 window 秒内的弹幕，去重计数。t 可以是秒数或 mm:ss。"""
        key = self.resolve(vid)
        if "error" in key:
            return key
        d = self._view(key)
        if "error" in d:
            return d
        cid, bvid = d.get("cid"), d.get("bvid")
        try:
            t = _parse_t(t)
        except Exception:
            return {"error": f"时间点认不出: {t}", "code": -1}
        window = max(0.0, float(window))
        lo, hi = t - window, t + window
        segs = sorted({max(1, int(lo // 360) + 1), max(1, int(hi // 360) + 1)})
        items, source = [], "seg.so"
        try:
            for si in segs:
                part = self._dm_seg(cid, si)
                if part is None:
                    raise RuntimeError("seg.so 不认")
                items += part
        except guard.BiliStop as e:
            return {"error": e.human, "code": e.code}     # 撞风控 / 预算：不退到 list.so
        except Exception:
            # seg.so 回了看不懂的东西（不是风控）才换 list.so
            try:
                items, source = self._dm_list(cid), "list.so"
            except guard.BiliStop as e:
                return {"error": e.human, "code": e.code}
            except Exception as e:
                return {"error": f"弹幕拉不下来: {type(e).__name__}", "code": -1}
        hit = [(ts, tx) for ts, tx in items if lo <= ts <= hi]
        groups = {}
        for ts, tx in sorted(hit):
            k = self._dm_norm(tx)
            g = groups.get(k)
            if g is None:
                groups[k] = {"text": tx.strip(), "count": 1, "t_rel": round(ts - t, 2)}
            else:
                g["count"] += 1
                if len(tx.strip()) < len(g["text"]):
                    g["text"] = tx.strip()
        lines = sorted(groups.values(), key=lambda g: (-g["count"], g["t_rel"]))[:int(limit)]
        return {"bvid": bvid, "cid": cid, "t": t, "window": window, "total_in_window": len(hit),
                "source": source, "lines": lines}

    # ── 给 bilitube 后端：取流 / 分 P / 全量弹幕 ──────────────
    # 只给本机 bili_mcp.py 的回环 REST 用；bilitube_server.py 拿到 url 后自己代理 CDN，凭据不出这个文件。

    def pages(self, vid):
        """分 P 列表：{"bvid","aid","title","duration","cover","up","pages":[{p,cid,part,duration}]}。"""
        key = self.resolve(vid)
        if "error" in key:
            return key
        d = self._view(key)
        if "error" in d:
            return d
        out = {"bvid": d.get("bvid"), "aid": d.get("aid"), "title": d.get("title"),
               "duration": d.get("duration"), "cover": d.get("pic"), "up": (d.get("owner") or {}).get("name"),
               "cid": d.get("cid"),
               "pages": [{"p": i + 1, "cid": pg.get("cid"), "part": pg.get("part"), "duration": pg.get("duration")}
                         for i, pg in enumerate(d.get("pages") or [])]}
        m = re.search(r"/bangumi/play/ep(\d+)", str(d.get("redirect_url") or ""))
        if m:
            out["pgc_ep"] = int(m.group(1))       # 番剧那一集的 BV：bilitube 据此改走番剧那条路
        if d.get("pgc"):
            out["pgc"] = d["pgc"]
        return out

    def playurl(self, bvid, cid, qn=80):
        """渐进式 mp4（platform=html5 → durl，一整条 mp4，浏览器 <video> 直接放）。
        依次试 qn / 64 / 32，第一个给出 durl 的就用。html5 平台实测最高只给 64（720p）。
        返回 {bvid,cid,qn,url,backup_urls,expires_at,size,timelength}；url 里的 deadline 就是 expires_at。"""
        tried = []
        order = []
        for q in (int(qn or 80), 80, 64, 32, 16):
            if q not in order:
                order.append(q)
        for q in order:
            d = self._get("/x/player/wbi/playurl", {"bvid": bvid, "cid": cid, "qn": q,
                                                     "platform": "html5", "high_quality": 1, "fnval": 0}, wbi=True)
            if "error" in d:
                tried.append(f"qn={q}: {d.get('error')}")
                if d.get("code") in (guard.Paused.code, guard.OverBudget.code, "mismatch"):
                    return {"error": d.get("error"), "code": d.get("code")}   # 不换清晰度再撞
                continue
            durl = d.get("durl") or []
            if not durl or not durl[0].get("url"):
                tried.append(f"qn={q}: 没有 durl")
                continue
            u = durl[0]["url"]
            exp = 0
            try:
                exp = int(urllib.parse.parse_qs(urllib.parse.urlsplit(u).query).get("deadline", ["0"])[0])
            except Exception:
                exp = 0
            if not exp:
                exp = int(time.time()) + 3600
            return {"bvid": bvid, "cid": cid, "qn": d.get("quality") or q, "format": d.get("format"),
                    "url": u, "backup_urls": list(durl[0].get("backup_url") or []),
                    "expires_at": exp, "size": durl[0].get("size"), "timelength": d.get("timelength")}
        return {"error": "拿不到播放地址：" + "; ".join(tried), "code": -1}

    def danmaku_all(self, cid, duration=None):
        """整部片的弹幕（seg.so 每 6 分钟一段，逐段拉）→
        [{t, mode(1 滚动/4 底部/5 顶部/6 逆向/7 高级), color, size, text, weight}]，按 t 排。
        给了 duration 就按段数拉；没给就一直拉到连续两段空为止（最多 120 段 = 12 小时）。"""
        segs = int(duration // 360) + 1 if duration else 120
        out, empty_run = [], 0
        for si in range(1, segs + 1):
            try:
                r = self._send("GET", f"{API}/x/v2/dm/web/seg.so",
                               params={"type": 1, "oid": cid, "segment_index": si}, timeout=20, expect="raw")
            except guard.BiliStop as e:
                return {"error": e.human, "code": e.code}
            except Exception as e:
                return {"error": f"弹幕拉不下来: {type(e).__name__}", "code": -1}
            if r.status_code != 200 or not r.content:
                empty_run += 1
                if duration or empty_run >= 2:
                    break
                continue
            empty_run = 0
            got = 0
            for f, wt, elem in self._pb_iter(r.content):
                if f != 1 or wt != 2:
                    continue
                d = {"mode": 1, "size": 25, "color": 16777215, "weight": 0}
                t, text = None, None
                for ff, wtt, v in self._pb_iter(elem):
                    if wtt == 0:
                        if ff == 2:
                            t = v / 1000.0
                        elif ff == 3:
                            d["mode"] = v
                        elif ff == 4:
                            d["size"] = v
                        elif ff == 5:
                            d["color"] = v
                        elif ff == 9:
                            d["weight"] = v
                    elif wtt == 2 and ff == 7:
                        text = v.decode("utf-8", "replace")
                if t is None or not text:
                    continue
                d["t"] = round(t, 2)
                d["text"] = text
                out.append(d)
                got += 1
            if not got and not duration:
                empty_run += 1
                if empty_run >= 2:
                    break
        out.sort(key=lambda x: x["t"])
        return {"cid": cid, "count": len(out), "list": out}

    # ── 列表：稍后再看、关注动态、直播、推荐、热门、字幕、收到的回复、搜索 ──────────────

    @staticmethod
    def _row(title, bvid, up, up_mid, dur, cover, play=None, dm=None, pub=None, face=None, **extra):
        """列表项统一的样子：封面/标题/UP/时长，页面上的几张列表共用一套渲染。
        原始返回里带 UP 主头像的，透传成 face（只是地址；没有就空串，不另查）。"""
        cover = str(cover or "")
        if cover.startswith("//"):
            cover = "https:" + cover
        out = {"title": title, "bvid": bvid, "up": up, "up_mid": up_mid,
               "duration": _mmss(dur) if isinstance(dur, (int, float)) else _dur_norm(dur),
               "cover_url": cover, "face": _https(face)}
        if play is not None:
            out["play"] = play
        if dm is not None:
            out["danmaku"] = dm
        if pub:
            out["pubdate"] = pub
        for k, v in extra.items():
            if v is not None:
                out[k] = v
        return out

    def watch_later(self, n=20, offset=0):
        """稍后再看（要登录）。B 站这个接口一次返回全部，所以分页在这里按 offset 切。"""
        d = self._get("/x/v2/history/toview", need_login=True)
        if "error" in d:
            return d
        rows = d.get("list") or []
        offset = max(0, int(offset or 0))
        out = []
        for it in rows[offset:offset + int(n)]:
            own = it.get("owner") or {}
            prog, dur = it.get("progress"), it.get("duration")
            row = self._row(it.get("title"), it.get("bvid"), own.get("name"), own.get("mid"),
                            dur, it.get("pic"), (it.get("stat") or {}).get("view"),
                            (it.get("stat") or {}).get("danmaku"), _when(it.get("pubdate")),
                            face=own.get("face"), aid=it.get("aid"), later=True)
            if dur and prog:
                row["progress"] = "看完" if prog == -1 else f"{_mmss(prog)}/{_mmss(dur)}"
            epm = re.search(r"/bangumi/play/ep(\d+)", str(it.get("redirect_url") or ""))
            ep_id = (it.get("bangumi") or {}).get("ep_id") if isinstance(it.get("bangumi"), dict) else None
            if ep_id or epm:
                row["film"] = f"bili:ep{ep_id or epm.group(1)}"      # 稍后再看里的番剧那一集
                row["pgc"] = True
            out.append(row)
        return {"count": len(out), "total": d.get("count") or len(rows),
                "offset": offset, "next_offset": offset + len(out),
                "has_more": offset + len(out) < len(rows), "list": out}

    def watch_later_set(self, vid, remove=False):
        aid, err = self._aid_of(vid)
        if err:
            return err
        d = self._post("/x/v2/history/toview/" + ("del" if remove else "add"), {"aid": aid})
        if "error" in d:
            return d
        return {"ok": True, "aid": aid, "removed": bool(remove)}

    def dynamic_videos(self, n=20, host_mid=None, offset=None, page=1):
        """关注的 UP 最新投稿（动态流里只留视频那种）。host_mid 给了就只看这一个人的。
        offset＝上一页返回的游标，翻页靠它。"""
        if not self.logged_in():
            return dict(NOT_LOGGED_IN)
        off = str(offset or "")
        if host_mid:
            d = self._get("/x/polymer/web-dynamic/v1/feed/space",
                          {"host_mid": int(host_mid), "offset": off, "timezone_offset": -480,
                           "platform": "web", "features": "itemOpusStyle"}, need_login=True)
        else:
            d = self._get("/x/polymer/web-dynamic/v1/feed/all",
                          {"type": "video", "page": max(1, int(page or 1)), "offset": off,
                           "timezone_offset": -480,
                           "platform": "web", "features": "itemOpusStyle"}, need_login=True)
        if "error" in d:
            return d
        out = []
        for it in (d.get("items") or []):
            if it.get("type") != "DYNAMIC_TYPE_AV":
                continue
            mods = it.get("modules") or {}
            author = mods.get("module_author") or {}
            arc = ((mods.get("module_dynamic") or {}).get("major") or {}).get("archive") or {}
            if not arc.get("bvid"):
                continue
            st = arc.get("stat") or {}
            out.append(self._row(arc.get("title"), arc.get("bvid"), author.get("name"),
                                 author.get("mid"), arc.get("duration_text"), arc.get("cover"),
                                 st.get("play"), st.get("danmaku"), author.get("pub_time"),
                                 face=author.get("face")))
            if len(out) >= int(n):
                break
        return {"count": len(out), "offset": d.get("offset") or "",
                "page": max(1, int(page or 1)),
                "has_more": bool(d.get("has_more")) and bool(d.get("offset")),
                "list": out}

    def live_following(self, n=20):
        """关注的人里正在直播的（直播接口在另一个域名）。"""
        if not self.logged_in():
            return dict(NOT_LOGGED_IN)
        d = self._get("/xlive/web-ucenter/user/following",
                      {"page": 1, "page_size": max(10, min(int(n) * 2, 30)), "ignoreRecord": 1},
                      need_login=True, base=LIVE)
        if "error" in d:
            return d
        out = []
        for it in (d.get("list") or []):
            if int(it.get("live_status") or 0) != 1:
                continue
            face = str(it.get("face") or "")
            if face.startswith("//"):
                face = "https:" + face
            out.append({"uname": it.get("uname"), "mid": it.get("uid") or it.get("mid"),
                        "face": face, "room": it.get("roomid"), "title": it.get("title"),
                        "area": it.get("area_name_v2") or it.get("area_v2_name")})
            if len(out) >= int(n):
                break
        return {"count": len(out), "total": d.get("live_count"), "list": out}

    def live_danmu_info(self, room):
        """直播间弹幕长连接的入场券（getDanmuInfo，只读）：token ＋ 弹幕服务器主机。
        以未登录身份问——只带 buvid 指纹、不带登录 cookie，于是连上去也是匿名（用户名打码），
        不会以账号身份「进入直播间」。返回 {room, token, hosts:[{host, wss_port}], buvid}。"""
        try:
            room = int(room)
        except (TypeError, ValueError):
            return {"error": "房间号不对", "code": -1}
        if room <= 0:
            return {"error": "房间号不对", "code": -1}
        self._ensure_buvid()
        ck = {k: self.cookies.get(k) for k in ("buvid3", "buvid4") if self.cookies.get(k)}
        hdr = {"Cookie": "; ".join(f"{k}={v}" for k, v in ck.items()),
               "Referer": f"https://live.bilibili.com/{room}", "Origin": "https://live.bilibili.com"}
        try:
            params = self._wbi_sign({"id": room, "type": 0})
            r = self._send("GET", f"{LIVE}/xlive/web-room/v1/index/getDanmuInfo",
                           params=params, headers=hdr, timeout=10, cookie=False)
        except guard.BiliStop as e:
            return {"error": e.human, "code": e.code}
        except requests.RequestException as e:
            return {"error": f"网络错误: {type(e).__name__}", "code": -1}
        except Exception as e:
            return {"error": str(e), "code": -1}
        if r.get("code") != 0:
            return {"error": r.get("message") or "未知错误", "code": r.get("code")}
        d = r.get("data") or {}
        hosts = []
        for h in d.get("host_list") or []:
            host = str(h.get("host") or "").lower()
            if re.fullmatch(r"[0-9a-z.\-]+\.bilibili\.com", host) and h.get("wss_port"):
                hosts.append({"host": host, "wss_port": int(h["wss_port"])})
        if not d.get("token") or not hosts:
            return {"error": "没给弹幕服务器", "code": -1}
        return {"room": room, "token": str(d["token"]), "hosts": hosts[:4], "buvid": ck.get("buvid3", "")}

    def recommend(self, n=12, fresh=1):
        """首页推荐流一页（登录了就是 TA 的口味）。fresh 每换一批加一。"""
        d = self._get("/x/web-interface/wbi/index/top/feed/rcmd",
                      {"ps": max(1, min(int(n), 30)), "fresh_type": 4, "feed_version": "V8",
                       "fresh_idx": int(fresh), "fresh_idx_1h": int(fresh), "brush": int(fresh),
                       "y_num": 4, "last_y_num": 4, "homepage_ver": 1, "outside_trigger": "",
                       "screen": 1080, "seo_info": "", "last_showlist": "", "uniq_id": ""},
                      wbi=True)
        if "error" in d:
            return d
        out = []
        for it in (d.get("item") or []):
            if not it.get("bvid"):
                continue
            own = it.get("owner") or {}
            st = it.get("stat") or {}
            out.append(self._row(it.get("title"), it.get("bvid"), own.get("name"), own.get("mid"),
                                 it.get("duration"), it.get("pic"), st.get("view"), st.get("danmaku"),
                                 _when(it.get("pubdate")), face=own.get("face")))
            if len(out) >= int(n):
                break
        return {"count": len(out), "fresh": int(fresh), "list": out}

    def popular(self, n=20, page=1):
        """热门榜一页。"""
        d = self._get("/x/web-interface/popular", {"ps": max(1, min(int(n), 20)), "pn": max(1, int(page))})
        if "error" in d:
            return d
        out = []
        for it in (d.get("list") or [])[:int(n)]:
            own = it.get("owner") or {}
            st = it.get("stat") or {}
            out.append(self._row(it.get("title"), it.get("bvid"), own.get("name"), own.get("mid"),
                                 it.get("duration"), it.get("pic"), st.get("view"), st.get("danmaku"),
                                 _when(it.get("pubdate")), face=own.get("face"),
                                 reason=((it.get("rcmd_reason") or {}).get("content") or None)))
        return {"count": len(out), "page": int(page),
                "has_more": not bool(d.get("no_more")) and len(out) > 0, "list": out}

    def subtitle(self, vid, t=None, window=6, p=None, cid=None):
        """CC 字幕：t 给了就只取前后 window 秒，没字幕回空列表（不算错）。
        分 P 按 cid（bilitube 后端手里有）> p（数字，或 vid 里带的 ?p=N / _pN）> P1 的顺序挑；
        不能直接用 view 顶层的 cid，那个永远是 P1。"""
        key = self.resolve(vid)
        if "error" in key:
            return key
        info = self._view(key)
        if "error" in info:
            return info
        aid, bvid = info.get("aid"), info.get("bvid")
        pages = info.get("pages") or []
        if p in (None, ""):
            m = re.search(r"(?:[?&]p=|_p)(\d{1,4})\b", str(vid))
            p = int(m.group(1)) if m else None
        try:
            p = int(p) if p not in (None, "") else None
        except Exception:
            p = None
        try:
            cid = int(cid) if cid not in (None, "", 0, "0") else None
        except Exception:
            cid = None
        if cid:
            hit = next((i + 1 for i, pg in enumerate(pages) if pg.get("cid") == cid), None)
            if not hit:
                return {"error": f"{bvid} 里没有 cid {cid}", "code": -1}
            p = hit
        elif p and p > 1:
            if p > len(pages):
                return {"error": f"没有第 {p} P（共 {len(pages)} P）", "code": -1}
            cid = pages[p - 1].get("cid")
        else:
            p, cid = 1, info.get("cid")
        # 只走 WBI 签名的 /x/player/wbi/v2（dsh-bili-humanism README A1：未签名的 player 接口
        # 会回别的视频的字幕轨），并核对回来的 bvid/cid/aid；对不上就当没有字幕、明说，不去拉那条字幕。
        d = self._get("/x/player/wbi/v2", {"aid": aid, "cid": cid, "bvid": bvid}, wbi=True, need_login=True)
        if "error" in d:
            return d
        for k, want in (("bvid", bvid), ("cid", cid), ("aid", aid)):
            got = d.get(k)
            if got is not None and want is not None and str(got) != str(want):
                return {"bvid": bvid, "cid": cid, "p": p, "has": False, "count": 0, "list": [],
                        "mismatch": {"field": k, "want": want, "got": got},
                        "note": f"B 站回来的字幕身份对不上（要 {k}={want}，回的是 {got}），当这部没有字幕"}
        subs = ((d.get("subtitle") or {}).get("subtitles")) or []
        if not subs:
            return {"bvid": bvid, "cid": cid, "p": p, "has": False, "count": 0, "list": []}
        pick = next((x for x in subs if str(x.get("lan") or "").startswith("zh")), subs[0])
        url = str(pick.get("subtitle_url") or "")
        if url.startswith("//"):
            url = "https:" + url
        if not url:
            return {"bvid": bvid, "cid": cid, "p": p, "has": False, "count": 0, "list": []}
        try:
            body = (self._raw_get(url, cookie=False) or {}).get("body") or []
        except guard.BiliStop as e:
            return {"error": e.human, "code": e.code}
        except Exception as e:
            return {"error": f"字幕拉不下来: {type(e).__name__}", "code": -1}
        lo = hi = None
        if t is not None:
            try:
                tt = _parse_t(t)
            except Exception:
                return {"error": f"时间点认不出: {t}", "code": -1}
            w = max(0.0, float(window))
            lo, hi = tt - w, tt + w
        out = []
        for c in body:
            a, b = float(c.get("from") or 0), float(c.get("to") or 0)
            if lo is not None and (b < lo or a > hi):
                continue
            out.append({"t": round(a, 1), "end": round(b, 1), "at": _mmss(int(a)),
                        "text": str(c.get("content") or "").strip()})
        return {"bvid": bvid, "cid": cid, "p": p, "has": True, "lan": pick.get("lan_doc") or pick.get("lan"),
                "count": len(out), "total": len(body), "list": out}

    def my_replies(self, n=20):
        """TA 自己的投稿和评论底下的新回复。"""
        if not self.logged_in():
            return dict(NOT_LOGGED_IN)
        d = self._get("/x/msgfeed/reply", {"platform": "web", "build": 0, "mobi_app": "web"}, need_login=True)
        if "error" in d:
            return d
        out = []
        for it in (d.get("items") or [])[:int(n)]:
            u = it.get("user") or {}
            item = it.get("item") or {}
            out.append({"who": u.get("nickname"), "mid": u.get("mid"),
                        "said": str(item.get("source_content") or "").strip()[:200],
                        "on": str(item.get("title") or "").strip()[:80],
                        "kind": item.get("business") or item.get("type"),
                        "url": item.get("uri") or "",
                        "when": _when(it.get("reply_time"))})
        return {"count": len(out), "list": out}

    def search(self, keyword, n=10, page=1):
        page = max(1, int(page or 1))
        d = self._get("/x/web-interface/wbi/search/type",
                      {"search_type": "video", "keyword": keyword, "page": page, "page_size": min(int(n), 30)},
                      wbi=True)
        if "error" in d:
            return d
        out = []
        for r in (d.get("result") or [])[:int(n)]:
            pic = r.get("pic") or ""
            if pic.startswith("//"):
                pic = "https:" + pic
            out.append({"title": _strip_em(r.get("title")), "bvid": r.get("bvid"),
                        "up": r.get("author"), "up_mid": r.get("mid"),
                        "play": r.get("play"), "danmaku": r.get("video_review"),
                        "duration": _dur_norm(r.get("duration")), "pubdate": _when(r.get("pubdate")),
                        "cover_url": pic, "face": _https(r.get("upic")),
                        "desc": (r.get("description") or "")[:120]})
        pages = d.get("numPages") or d.get("numResults") and None
        return {"keyword": keyword, "total": d.get("numResults"), "page": page,
                "pages": pages, "has_more": bool(out) and (pages is None or page < pages),
                "list": out}


# ── 命令行 ─────────────────────────────────────────────────

def _resume(argv):
    """解除风控暂停。只在真终端里、由人敲 yes 才解（MCP 和回环 REST 都没有这条路）。"""
    p = guard.read_pause()
    if not p:
        print("现在没暂停，B 站工具照常。")
        return 0
    print(guard.human_pause(p))
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("要在真终端里由人来解（这里不是终端，不解）。", file=sys.stderr)
        return 2
    print("先去 B 站 App / 网页看一眼账号有没有要过验证、是不是正常；确认没事了再解。")
    try:
        ans = input("确认解除暂停？输入 yes：").strip().lower()
    except EOFError:
        ans = ""
    if ans != "yes":
        print("没解。")
        return 1
    guard.clear_pause(who=os.environ.get("SUDO_USER") or os.environ.get("USER") or "local")
    print("解了。之后的请求照常发；再撞风控会自己再停。")
    return 0


def _cli(argv):
    cmd = argv[0] if argv else "status"
    a = argv[1:]
    if cmd == "resume":
        return _resume(a)
    if cmd == "pause-status":
        p = guard.read_pause()
        return guard.human_pause(p) if p else "没暂停。"
    c = BiliClient()
    if cmd == "login":
        q = c.login_qr()
        print(json.dumps({k: v for k, v in q.items() if k != "url"}, ensure_ascii=False, indent=2))
        if "error" in q:
            return 1
        print("等 TA 扫码（120s）…", file=sys.stderr)
        return json.dumps(c.login_wait(q["auth_code"]), ensure_ascii=False, indent=2)
    if cmd == "status":
        return c.status()
    if cmd == "refresh":
        return c.refresh()
    if cmd == "video":
        return c.video(a[0])
    if cmd == "comments":
        return c.comments(a[0], a[1] if len(a) > 1 else "hot", int(a[2]) if len(a) > 2 else 1)
    if cmd == "replies":
        return c.replies(a[0], int(a[1]), int(a[2]) if len(a) > 2 else 1)
    if cmd == "history":
        return c.history(int(a[0]) if a else 20)
    if cmd == "danmaku":
        return c.danmaku(a[0], a[1], float(a[2]) if len(a) > 2 else 3, int(a[3]) if len(a) > 3 else 30)
    if cmd == "search":
        return c.search(a[0], int(a[1]) if len(a) > 1 else 10)
    if cmd == "like":
        return c.like(a[0], on=not (len(a) > 1 and a[1] == "off"))
    if cmd == "coin":
        return c.coin(a[0], int(a[1]) if len(a) > 1 else 1)
    if cmd == "fav":
        return c.fav(a[0], int(a[1]) if len(a) > 1 else None, by="user")
    if cmd == "folders":
        return c.fav_folders()
    if cmd == "isliked":
        return c.is_liked(a[0])
    if cmd == "isfaved":
        return c.is_faved(a[0])
    if cmd == "post":
        return c.post_comment(a[0], " ".join(a[1:]))
    print(__doc__)
    return 1


if __name__ == "__main__":
    try:
        r = _cli(sys.argv[1:])
    except IndexError:
        print(__doc__)
        sys.exit(1)
    if isinstance(r, int):
        sys.exit(r)
    print(r if isinstance(r, str) else json.dumps(r, ensure_ascii=False, indent=2))
