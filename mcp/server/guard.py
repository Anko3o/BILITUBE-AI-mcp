"""
bilitube MCP 服务的护栏。三件事，都在「真正发请求」那一层守：

1. 风控暂停：任何对 B 站的请求撞到 HTTP 401/403/412/429、code -352/-412/-509/-403、
   返回里带 v_voucher、或者消息/页面里带验证码字样，就把「暂停」写进磁盘（时间、接口、信号），
   之后所有请求不发、直接回一句人话。不重试、不换接口。解除只能由人在本机终端跑 python3 mcp/server/bili.py resume。
   code -101（未登录 / 登录失效）不算暂停：那是该去扫码，不是被拦。
2. 写入白名单：POST 只放行下面 ALLOWED_POST 里写死的几条（收藏、点赞、关注、稍后再看、观看进度上报、扫码登录、退出登录）。
   评论、公开弹幕、投币、一键三连（含投币）、新建/改/删收藏夹……一律在发出之前拒掉。
   写入另有一份小额度：MCP 通道每小时点赞＋收藏＋关注合计 write_hourly_budget 次，默认 20。
3. 每小时预算：进程内的滑动窗口计数，按「通道」分开记——MCP 工具一份，
   bilitube 回环 REST 一份（默认不限：用户在页面上一起看片时别被卡住）。超了回「这一小时刷够了」，不算暂停。

做法参考 dsh-bili-humanism (MIT, Copyright (c) 2026 InariNGT-Ofiicial) 的 lib/network.mjs
（风控状态码/错误码表、POST 白名单、只对传输错误重试、写操作不重试）和 lib/budget.mjs（请求预算）。
Python 重写，没有搬代码；见仓库根目录的 NOTICE。

配置：mcp/config.json（或 BILI_CONFIG 指的文件；照 config.example.json 抄一份），环境变量优先：
  pocket_folder_id     BILI_POCKET_FOLDER_ID      「口袋」：AI 自己收藏时唯一能进的夹子（必须是私密收藏夹）
  hourly_budget        BILI_HOURLY_BUDGET         MCP 通道每小时请求上限，默认 30；0 = 不限
  rest_hourly_budget   BILI_REST_HOURLY_BUDGET    回环 REST 通道每小时上限，默认 0 = 不限
  write_hourly_budget  BILI_WRITE_HOURLY_BUDGET   MCP 通道每小时点赞＋收藏＋关注＋B 站稍后再看加/删合计上限，默认 20；0 = 不限
  later_hourly_budget  BILI_LATER_HOURLY_BUDGET   MCP 通道每小时往 B 站稍后再看里「加」的上限，默认 20；0 = 不限
  later_min_gap_s      BILI_LATER_MIN_GAP         MCP 通道两次「加」之间至少隔几秒，默认 30；0 = 不限
                       （「加」同时计进上面两份小时额度，哪份先满就先停＝取更严的那个；没到间隔直接拒，不排队、不自动重试。
                         用户在 bilitube 页面上自己点的走回环 REST 通道，这三样都不卡。）
  follow_group_id      BILI_FOLLOW_GROUP_ID       AI 自己关注的人放进的那个关注分组（B 站关注分组的 tagid），默认不配
  follow_group_name    BILI_FOLLOW_GROUP_NAME     没配 id 时按名字认分组，默认「AI 的关注」；id 和名字都认不出就不关注
  follow_hourly_budget BILI_FOLLOW_HOURLY_BUDGET  MCP 通道每小时关注＋取关合计上限，默认 5；0 = 不限
  follow_min_gap_s     BILI_FOLLOW_MIN_GAP        MCP 通道两次关注／取关之间至少隔几秒，默认 60；0 = 不限
                       （同时计进 write_hourly_budget，哪份先满就先停；没到间隔直接拒，不排队、不自动重试。）
  pause_file           BILI_PAUSE_FILE            暂停标记放哪，默认 mcp/state/paused.json
"""
import collections
import contextlib
import datetime
import json
import os
import re
import sys
import threading
import time
import urllib.parse

BILI_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BJ = datetime.timezone(datetime.timedelta(hours=8))

DEFAULTS = {
    "pocket_folder_id": None,
    "hourly_budget": 30,
    "rest_hourly_budget": 0,
    "write_hourly_budget": 20,
    "later_hourly_budget": 20,
    "later_min_gap_s": 30,
    "follow_group_id": None,
    "follow_group_name": "AI 的关注",
    "follow_hourly_budget": 5,
    "follow_min_gap_s": 60,
    "pause_file": os.path.join(BILI_ROOT, "state", "paused.json"),
}
_ENV = {
    "pocket_folder_id": "BILI_POCKET_FOLDER_ID",
    "hourly_budget": "BILI_HOURLY_BUDGET",
    "rest_hourly_budget": "BILI_REST_HOURLY_BUDGET",
    "write_hourly_budget": "BILI_WRITE_HOURLY_BUDGET",
    "later_hourly_budget": "BILI_LATER_HOURLY_BUDGET",
    "later_min_gap_s": "BILI_LATER_MIN_GAP",
    "follow_group_id": "BILI_FOLLOW_GROUP_ID",
    "follow_group_name": "BILI_FOLLOW_GROUP_NAME",
    "follow_hourly_budget": "BILI_FOLLOW_HOURLY_BUDGET",
    "follow_min_gap_s": "BILI_FOLLOW_MIN_GAP",
    "pause_file": "BILI_PAUSE_FILE",
}


def config():
    """每次现读（文件很小）：改了 config.json 不用重启也生效（预算上限除外，见 reset_budgets）。"""
    cfg = dict(DEFAULTS)
    path = os.environ.get("BILI_CONFIG") or os.path.join(BILI_ROOT, "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            j = json.load(f)
        if isinstance(j, dict):
            cfg.update({k: v for k, v in j.items() if k in DEFAULTS})
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[guard] config 读不了，用默认值: {type(e).__name__}", file=sys.stderr)
    for k, env in _ENV.items():
        v = os.environ.get(env)
        if v not in (None, ""):
            cfg[k] = v
    return cfg


def pocket_id():
    v = config().get("pocket_folder_id")
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def follow_group():
    """AI 关注用的分组：(tagid 或 None, 名字或 None)。id 优先；只给名字时由 BiliClient 现查分组列表按名字认。
    默认分组（0）和特别关注（-10）不能当 AI 的分组。"""
    cfg = config()
    try:
        gid = int(cfg.get("follow_group_id"))
    except (TypeError, ValueError):
        gid = None
    if gid is not None and gid <= 0:
        gid = None
    name = str(cfg.get("follow_group_name") or "").strip() or None
    return gid, name


def _when(ts):
    try:
        return datetime.datetime.fromtimestamp(int(ts), _BJ).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


# ── 停下来的几种原因（都不是「出错」，是「不发」）──────────────────

class BiliStop(Exception):
    code = "stopped"

    def __init__(self, human, info=None):
        super().__init__(human)
        self.human = human
        self.info = info or {}


class Paused(BiliStop):
    code = "paused"


class Blocked(BiliStop):
    code = "blocked"


class OverBudget(BiliStop):
    code = "over_budget"


# ── 1. 风控暂停 ──────────────────────────────────────────────

RISK_HTTP = {401, 403, 412, 429}
RISK_CODES = {-352, -412, -509, -403}     # -101 故意不在里面：登录失效该去扫码，不锁死
_CHALLENGE = re.compile(r"captcha|geetest|验证码|验证页面|风控|账号异常|安全验证", re.I)
_PAUSE_LOCK = threading.Lock()


def _pause_file():
    return str(config().get("pause_file") or DEFAULTS["pause_file"])


def read_pause():
    try:
        with open(_pause_file(), encoding="utf-8") as f:
            p = json.load(f)
        return p if isinstance(p, dict) and p.get("paused") else None
    except FileNotFoundError:
        return None
    except Exception:
        # 文件在但读不懂：宁可当作暂停着
        return {"paused": True, "since": 0, "since_bj": "", "endpoint": "?", "signal": "暂停文件读不懂"}


def human_pause(p):
    when = p.get("since_bj") or _when(p.get("since")) or "不知道几时"
    return (f"B 站在拦，已暂停，等人来解。原因：{p.get('signal') or '?'}（接口 {p.get('endpoint') or '?'}，"
            f"{when} 北京时间起）。这期间什么请求都不发；解除要人在本机终端跑 python3 mcp/server/bili.py resume。")


def set_pause(endpoint, signal):
    """写暂停标记。已经暂停着就不覆盖第一次的原因（只在日志里记一笔）。返回当前暂停记录。"""
    with _PAUSE_LOCK:
        cur = read_pause()
        if cur:
            _log(f"[guard] 又撞一次（已暂停着）：{endpoint} {signal}")
            return cur
        now = int(time.time())
        p = {"paused": True, "since": now, "since_bj": _when(now), "endpoint": endpoint, "signal": signal,
             "by": f"pid {os.getpid()}"}
        path = _pause_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(p, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        _history(dict(p, event="pause"))
        _log(f"[guard] 暂停：{endpoint} {signal}")
        return p


def clear_pause(who="local"):
    """只给本机命令 bili.py resume 用。MCP 和 REST 都没有通到这里的路。"""
    with _PAUSE_LOCK:
        cur = read_pause()
        try:
            os.remove(_pause_file())
        except FileNotFoundError:
            pass
        if cur:
            _history({"event": "resume", "at": int(time.time()), "at_bj": _when(time.time()),
                      "who": who, "was": {k: cur.get(k) for k in ("since_bj", "endpoint", "signal")}})
        return cur


def _history(rec):
    try:
        with open(os.path.join(os.path.dirname(_pause_file()), "pause-history.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def risk_of_json(j):
    """JSON 返回里的风控信号 → 一句描述；没有就 None。"""
    if not isinstance(j, dict):
        return None
    code = j.get("code")
    if isinstance(code, int) and code in RISK_CODES:
        return f"code {code} {str(j.get('message') or '')[:40]}".strip()
    data = j.get("data")
    if isinstance(data, dict) and data.get("v_voucher"):
        return "返回里带 v_voucher（要验证）"
    if _CHALLENGE.search(str(j.get("message") or "")):
        return f"消息里带验证/风控字样：{str(j.get('message'))[:40]}"
    return None


def risk_of_text(text):
    if _CHALLENGE.search((text or "")[:8192]):
        return "返回的是验证页（非 JSON）"
    return None


# ── 2. 写入白名单 ─────────────────────────────────────────────
# 只认 https、默认端口、没有 userinfo、没有 query 的这几条 (host, path)。
ALLOWED_POST = {
    ("api.bilibili.com", "/x/v3/fav/resource/deal"),               # 收藏 / 移出收藏
    ("api.bilibili.com", "/x/web-interface/archive/like"),         # 点赞 / 取消
    ("api.bilibili.com", "/x/relation/modify"),                    # 关注 / 取关（只有 AI 的 account follow / unfollow 调它，见 BiliClient.follow）
    ("api.bilibili.com", "/x/relation/tags/addUsers"),             # 把 AI 刚关注的人放进它的专属分组（只紧跟在一次关注后面用）
    ("api.bilibili.com", "/x/v2/history/toview/add"),              # 稍后再看
    ("api.bilibili.com", "/x/v2/history/toview/del"),
    ("api.bilibili.com", "/x/click-interface/web/heartbeat"),      # bilitube 观看进度上报（B 站历史记录）
    ("passport.bilibili.com", "/x/passport-tv-login/qrcode/auth_code"),   # 扫码登录
    ("passport.bilibili.com", "/x/passport-tv-login/qrcode/poll"),
    ("passport.bilibili.com", "/x/passport-tv-login/h5/refresh"),         # 续期
    ("passport.bilibili.com", "/x/passport-login/oauth2/refresh_token"),
    ("passport.bilibili.com", "/login/exit/v2"),                   # 退出登录（只由页面上人点「退出」触发，MCP 工具里没有这条路）
}
# 只为了拒的时候说清楚是哪一类；不在白名单里的不管叫什么都拒。
_NAMED = [
    (re.compile(r"/reply/(add|action|hate|del|top)"), "公开评论"),
    (re.compile(r"/dm/(post|thumbup|report|recall)|/dm/web/post"), "公开弹幕"),
    (re.compile(r"/like/triple"), "一键三连（含投币）"),
    (re.compile(r"/coin/add"), "投币"),
    (re.compile(r"/relation/batch"), "批量关注"),
    (re.compile(r"/thumbup"), "评论/弹幕点赞"),
    (re.compile(r"/fav/folder/(add|edit|del)"), "新建/改/删收藏夹"),
    (re.compile(r"/relation/tag/(create|update|del)"), "新建/改/删关注分组"),
]
SAFE_METHODS = {"GET", "HEAD"}


def check_write(method, url):
    """不在白名单的写入在这里就拒（请求还没发）。GET/HEAD 不管。"""
    m = str(method or "").upper()
    if m in SAFE_METHODS:
        return
    u = urllib.parse.urlsplit(str(url))
    host, path = (u.hostname or "").lower(), u.path
    ok = (m == "POST" and u.scheme == "https" and not u.username and not u.password
          and u.port in (None, 443) and not u.query and (host, path) in ALLOWED_POST)
    if ok:
        return
    kind = next((name for rx, name in _NAMED if rx.search(path)), "这条写入")
    raise Blocked(f"{kind}不在允许的写入里，没发（{m} {host}{path}）。"
                  f"bilitube 不评论、不发公开弹幕、不投币。",
                  {"method": m, "host": host, "path": path})


def refuse(kind):
    """给不开放的那几个函数（发评论、投币）用：不解析视频、不发任何请求，直接拒。"""
    return {"error": f"{kind}不开放：bilitube 不评论、不发公开弹幕、不投币。没发任何请求。",
            "code": Blocked.code}


# ── 3. 每小时预算（按通道）──────────────────────────────────────

class HourlyBudget:
    def __init__(self, limit, window=3600):
        self.limit = int(limit or 0)
        self.window = window
        self.hits = collections.deque()
        self.lock = threading.Lock()

    def _prune(self, now):
        while self.hits and now - self.hits[0] >= self.window:
            self.hits.popleft()

    def spend(self):
        if self.limit <= 0:
            return
        now = time.time()
        with self.lock:
            self._prune(now)
            if len(self.hits) >= self.limit:
                wait = int(self.window - (now - self.hits[0])) + 1
                raise OverBudget(f"这一小时刷够了（每小时 {self.limit} 次请求），"
                                 f"大约 {max(1, wait // 60)} 分钟后再来。这不是风控，只是歇一会儿。",
                                 {"limit": self.limit, "retry_in": wait})
            self.hits.append(now)

    def used(self):
        with self.lock:
            self._prune(time.time())
            return len(self.hits)

    def wait_s(self):
        """现在再花一次要等几秒（0＝能花）。只看不花。"""
        if self.limit <= 0:
            return 0
        now = time.time()
        with self.lock:
            self._prune(now)
            if len(self.hits) < self.limit:
                return 0
            return int(self.window - (now - self.hits[0])) + 1


_LANE = threading.local()
_BUDGETS = {}
_BUDGETS_LOCK = threading.Lock()
_LANE_KEYS = {"mcp": "hourly_budget", "rest": "rest_hourly_budget", "write": "write_hourly_budget",
              "later": "later_hourly_budget", "follow": "follow_hourly_budget"}


def budget(lane):
    if lane not in _LANE_KEYS:
        return None
    with _BUDGETS_LOCK:
        b = _BUDGETS.get(lane)
        if b is None:
            try:
                limit = int(config().get(_LANE_KEYS[lane]) or 0)
            except (TypeError, ValueError):
                limit = DEFAULTS[_LANE_KEYS[lane]] or 0
            b = _BUDGETS[lane] = HourlyBudget(limit)
        return b


def reset_budgets():
    with _BUDGETS_LOCK:
        _BUDGETS.clear()
        _LAST.clear()


@contextlib.contextmanager
def lane(name):
    """这个线程接下来发的请求记在哪个通道上。None＝不计（命令行、后台扫码轮询）。"""
    prev = getattr(_LANE, "name", None)
    _LANE.name = name
    try:
        yield
    finally:
        _LANE.name = prev


def current_lane():
    return getattr(_LANE, "name", None)


def spend():
    b = budget(current_lane())
    if b:
        b.spend()


# 计入写入小额度的几条（只在 MCP 通道记：用户在页面上自己按的不卡）
LATER_ADD = "/x/v2/history/toview/add"
FOLLOW = "/x/relation/modify"
COUNTED_WRITES = {"/x/v3/fav/resource/deal", "/x/web-interface/archive/like", FOLLOW,
                  LATER_ADD, "/x/v2/history/toview/del"}
# 另有一道「间隔＋小时上限」的几条：path → (额度通道, 间隔的配置键, 太密时的说法, 满了时的说法)。关注和取关走同一条 relation/modify，合计。
SPACED = {LATER_ADD: ("later", "later_min_gap_s", "往 B 站稍后再看里加", "往 B 站稍后再看里已经加了 {n} 次"),
          FOLLOW: ("follow", "follow_min_gap_s", "关注／取关", "关注＋取关已经 {n} 次了")}
_LAST = {}                 # path → MCP 通道上一次真发的时刻（进程内）
_LAST_LOCK = threading.Lock()


def _gap_s(key="later_min_gap_s"):
    try:
        return max(0.0, float(config().get(key) or 0))
    except (TypeError, ValueError):
        return float(DEFAULTS[key])


def spend_write(path):
    """MCP 通道的写入：先全看一遍够不够（间隔、这一类的小时额度、写入合计额度），都够才一起记账，免得一份记了另一份拒。"""
    if current_lane() != "mcp" or path not in COUNTED_WRITES:
        return
    w = budget("write")
    sp = SPACED.get(path)
    if not sp:
        if w:
            try:
                w.spend()
            except OverBudget as e:
                raise OverBudget(f"这一小时点赞＋收藏＋关注＋B 站稍后再看已经 {w.limit} 次了，先停手，下个钟头再说。这不是风控。",
                                 e.info)
        return
    lane_name, gap_key, what, used = sp
    lt = budget(lane_name)
    with _LAST_LOCK:
        gap, now, last = _gap_s(gap_key), time.time(), _LAST.get(path, 0.0)
        left = int(last + gap - now + 0.999) if last else 0
        if gap and left > 0:
            raise OverBudget(f"{what}得太密了，还要等 {left} 秒。没发请求，也不会自己排队重试。",
                             {"retry_in": left, "min_gap": gap})
        if lt and lt.wait_s():
            raise OverBudget(f"这一小时{used.format(n=lt.limit)}，先停手，"
                             f"大约 {max(1, lt.wait_s() // 60)} 分钟后再说。这不是风控。", {"limit": lt.limit, "retry_in": lt.wait_s()})
        if w and w.wait_s():
            raise OverBudget(f"这一小时点赞＋收藏＋关注＋B 站稍后再看已经 {w.limit} 次了，先停手，下个钟头再说。这不是风控。",
                             {"limit": w.limit, "retry_in": w.wait_s()})
        if lt:
            lt.spend()
        if w:
            w.spend()
        _LAST[path] = now


def budget_status():
    out = {}
    for ln in _LANE_KEYS:
        b = budget(ln)
        out[ln] = {"limit": b.limit, "used_last_hour": b.used()}
    return out
