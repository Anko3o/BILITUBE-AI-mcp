"""
bilitube MCP 服务护栏的离线测试。网络全 mock：requests.Session.request 换成假的，
真 B 站一次都不碰；钥匙文件、暂停文件、配置、缓存全指到临时目录（假钥匙，不读真的）。

跑法：cd mcp && python3 -m unittest tests.test_guard -v
"""
import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.parse
from unittest import mock

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="bili-guard-test-")
FAKE_SESS = "FAKE-SESSDATA-not-real"
FAKE_JCT = "FAKE-JCT-not-real"
POCKET = 900001
os.environ["BILI_CONFIG"] = os.path.join(TMP, "config.json")
os.environ["BILI_PAUSE_FILE"] = os.path.join(TMP, "state", "paused.json")
for k in ("BILI_POCKET_FOLDER_ID", "BILI_HOURLY_BUDGET", "BILI_REST_HOURLY_BUDGET"):
    os.environ.pop(k, None)
with open(os.environ["BILI_CONFIG"], "w") as f:
    json.dump({"pocket_folder_id": POCKET, "hourly_budget": 0, "rest_hourly_budget": 0}, f)

sys.path.insert(0, os.path.join(ROOT, "server"))
sys.path.insert(0, ROOT)
import bili  # noqa: E402
import guard  # noqa: E402

bili.CRED_PATH = os.path.join(TMP, "cred.json")
bili.CACHE_DIR = os.path.join(TMP, "cache")
bili.UPLOADS = os.path.join(TMP, "uploads")
os.makedirs(bili.CACHE_DIR, exist_ok=True)
with open(bili.CRED_PATH, "w") as f:
    json.dump({"mid": 42, "uname": "测试", "access_token": "FAKE-AT", "refresh_token": "FAKE-RT",
               "cookies": {"SESSDATA": FAKE_SESS, "bili_jct": FAKE_JCT, "DedeUserID": "42",
                           "buvid3": "b3", "buvid4": "b4"}}, f)
with open(os.path.join(bili.CACHE_DIR, "wbi.json"), "w") as f:
    json.dump({"img": "a" * 32, "sub": "b" * 32, "day": time.strftime("%Y-%m-%d")}, f)


# ── 假网络 ──────────────────────────────────────────────────

class FakeResp:
    def __init__(self, status=200, body=None, ctype="application/json", raw=None, headers=None):
        self.status_code = status
        self.ok = 200 <= status < 300
        if raw is not None:
            self.content = raw
        elif isinstance(body, (dict, list)):
            self.content = json.dumps(body).encode()
        else:
            self.content = (body or "").encode()
        self.headers = {"Content-Type": ctype}
        self.headers.update(headers or {})

    @property
    def text(self):
        return self.content.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.content.decode())


VIEW = {"code": 0, "data": {"bvid": "BV1xx411c7mD", "aid": 170001, "cid": 5001, "title": "测试片",
                            "owner": {"name": "UP", "mid": 7, "face": "//i0.hdslb.com/face.jpg"},
                            "stat": {}, "duration": 60, "pages": [{"cid": 5001, "part": "P1"}],
                            "desc": "简" * 300}}


class Net:
    """记下每个真要发出去的请求；routes: (method, path 子串) → FakeResp 或 callable。"""

    def __init__(self):
        self.calls = []
        self.routes = []

    def on(self, method, frag, resp):
        self.routes.insert(0, (method.upper(), frag, resp))

    def __call__(self, session, method, url, params=None, data=None, headers=None, **kw):
        self.calls.append({"method": method.upper(), "url": url, "params": params, "data": data,
                           "headers": headers or {}})
        for m, frag, resp in self.routes:
            if m == method.upper() and frag in url:
                return resp(url, params, data) if callable(resp) else resp
        return FakeResp(200, {"code": 0, "data": {}})

    def paths(self, method=None):
        return [urllib.parse.urlsplit(c["url"]).path for c in self.calls
                if method is None or c["method"] == method]


class Base(unittest.TestCase):
    def setUp(self):
        guard.clear_pause("test")
        guard.reset_budgets()
        self.net = Net()
        self.net.on("GET", "/wbi/view", FakeResp(200, VIEW))
        p = mock.patch.object(requests.Session, "request", autospec=True, side_effect=self.net)
        p.start()
        self.addCleanup(p.stop)
        # 兜底：任何绕过 request 的真发送都炸
        p2 = mock.patch.object(requests.Session, "send", side_effect=AssertionError("真网络！"))
        p2.start()
        self.addCleanup(p2.stop)
        self.c = bili.BiliClient()

    def tearDown(self):
        guard.clear_pause("test")
        # 假钥匙也不该出现在任何落盘的东西里
        for root, _, files in os.walk(os.path.dirname(os.environ["BILI_PAUSE_FILE"])):
            for fn in files:
                with open(os.path.join(root, fn), encoding="utf-8", errors="replace") as fh:
                    txt = fh.read()
                self.assertNotIn(FAKE_SESS, txt)
                self.assertNotIn(FAKE_JCT, txt)


# ── 2. 写入白名单 ─────────────────────────────────────────────

FORBIDDEN = [
    ("https://api.bilibili.com/x/v2/reply/add", "公开评论"),
    ("https://api.bilibili.com/x/v2/reply/action", "公开评论"),
    ("https://api.bilibili.com/x/v2/dm/post", "公开弹幕"),
    ("https://api.bilibili.com/x/v2/dm/web/post", "公开弹幕"),
    ("https://api.bilibili.com/x/web-interface/archive/like/triple", "一键三连"),
    ("https://api.bilibili.com/x/web-interface/coin/add", "投币"),
    ("https://api.bilibili.com/x/relation/batch/modify", "批量关注"),
    ("https://api.bilibili.com/x/v2/reply/action", "公开评论"),
    ("https://api.bilibili.com/x/v3/fav/folder/edit", "收藏夹"),
    ("https://api.bilibili.com/x/v3/fav/folder/del", "收藏夹"),
    ("https://api.bilibili.com/x/v3/fav/folder/add", "收藏夹"),
    ("https://api.bilibili.com/x/v3/fav/resource/deal?x=1", "这条写入"),        # 带 query 也不行
    ("http://api.bilibili.com/x/v3/fav/resource/deal", "这条写入"),             # 非 https
    ("https://evil.example.com/x/v3/fav/resource/deal", "这条写入"),            # 别的主机
    ("https://api.live.bilibili.com/msg/send", "这条写入"),                     # 直播弹幕
]


class TestWriteWhitelist(Base):
    def test_transport_refuses_forbidden_posts(self):
        for url, kind in FORBIDDEN:
            with self.subTest(url=url):
                with self.assertRaises(guard.Blocked) as cm:
                    self.c._send("POST", url, data={"x": 1})
                self.assertIn(kind if kind != "收藏夹" else "收藏夹", cm.exception.human)
        for m in ("PUT", "DELETE", "PATCH"):
            with self.assertRaises(guard.Blocked):
                self.c._send(m, "https://api.bilibili.com/x/v3/fav/resource/deal")
        self.assertEqual(self.net.calls, [], "被拒的写入一个请求都不该发出去")

    def test_private_post_helper_refuses(self):
        for path in ("/x/v2/reply/add", "/x/web-interface/coin/add", "/x/web-interface/archive/like/triple",
                     "/x/v2/dm/post", "/x/v3/fav/folder/add"):
            d = self.c._post(path, {"oid": 1})
            self.assertEqual(d.get("code"), "blocked", path)
        self.assertEqual(self.net.paths("POST"), [])

    def test_client_functions_refuse_without_any_request(self):
        for d in (self.c.post_comment("BV1xx411c7mD", "hi"), self.c.coin("BV1xx411c7mD", 2, also_like=True)):
            self.assertEqual(d["code"], "blocked")
        self.assertEqual(self.net.calls, [])

    def test_allowed_writes_pass(self):
        self.assertEqual(self.c.fav("BV1xx411c7mD", 123, by="user")["ok"], True)
        self.assertEqual(self.c.watch_later_set("BV1xx411c7mD")["ok"], True)
        self.assertEqual(self.c.watch_later_set("BV1xx411c7mD", remove=True)["ok"], True)
        self.assertTrue(self.c.like("BV1xx411c7mD")["ok"])
        self.assertTrue(self.c.like("BV1xx411c7mD", on=False)["ok"])
        self.c._send("POST", "https://api.bilibili.com/x/relation/modify", data={"fid": 7, "act": 1})   # 关注（AI 的 account follow 用）
        posts = self.net.paths("POST")
        self.assertEqual(posts, ["/x/v3/fav/resource/deal", "/x/v2/history/toview/add", "/x/v2/history/toview/del",
                                 "/x/web-interface/archive/like", "/x/web-interface/archive/like",
                                 "/x/relation/modify"])
        likes = [c["data"]["like"] for c in self.net.calls if c["url"].endswith("/archive/like")]
        self.assertEqual(likes, [1, 2])

    def test_like_412_pauses_and_no_retry(self):
        self.net.on("POST", "/archive/like", FakeResp(412, "no", ctype="text/plain"))
        self.assertEqual(self.c.like("BV1xx411c7mD")["code"], "paused")
        self.assertEqual(self.net.paths("POST"), ["/x/web-interface/archive/like"])
        self.assertIn("HTTP 412", guard.read_pause()["signal"])

    def test_writes_never_retry(self):
        self.net.on("POST", "/fav/resource/deal", lambda *a: (_ for _ in ()).throw(requests.ConnectionError("x")))
        d = self.c.fav("BV1xx411c7mD", 123, by="user")
        self.assertIn("error", d)
        self.assertEqual(self.net.paths("POST"), ["/x/v3/fav/resource/deal"])


class TestMcpSurface(Base):
    def setUp(self):
        super().setUp()
        import bili_mcp
        self.m = bili_mcp
        self.m.CLIENT = self.c

    def test_tools_list(self):
        # 对外是 bilitube 七样；动作清单里不该有评论、投币、退出登录、解除暂停；关注只有 account 的 follow / unfollow
        names = {t["name"] for t in self.m.TOOLS}
        self.assertEqual(names, {"login", "find", "video", "library", "watching", "stage", "account"})
        acts = {a for t in self.m.TOOLS for a in t["inputSchema"]["properties"]["action"]["enum"]}
        self.assertIn("like", acts)
        self.assertNotIn("coin", json.dumps([t["inputSchema"] for t in self.m.TOOLS]))
        self.assertFalse(any("comment" in a and a != "comments" for a in acts), "不能有发评论的动作")
        self.assertFalse(any("relation" in a or "logout" in a for a in acts), "没有直接动关注关系的动作、没有退出")
        follow_acts = {(t["name"], a) for t in self.m.TOOLS for a in t["inputSchema"]["properties"]["action"]["enum"]
                       if "follow" in a}
        self.assertEqual(follow_acts, {("find", "following"), ("library", "follows"),
                                       ("account", "follow"), ("account", "unfollow")})
        self.assertFalse(any("resume" in a or "unpause" in a for a in acts | names), "不能有解除暂停的 MCP 工具")

    def test_comment_refused_like_allowed_coin_never(self):
        with self.assertRaises(self.m.ToolError):
            self.m.call("bili_post_comment", {"id": "BV1xx411c7mD", "text": "hi"})
        self.assertEqual(self.net.calls, [])
        out = self.m.call("bili_like", {"id": "BV1xx411c7mD", "coin": 2})
        self.assertIn("点赞", out)
        self.assertIn("投币没开", out)
        self.assertEqual(self.net.paths("POST"), ["/x/web-interface/archive/like"])

    def test_rest_like_works(self):
        d = self.m.w_like({"id": "BV1xx411c7mD", "on": False})
        self.assertTrue(d["ok"])
        self.assertEqual(self.net.paths("POST"), ["/x/web-interface/archive/like"])

    def test_danmaku_say_goes_home_only(self):
        sent = []

        class R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            sent.append(req.full_url)
            return R(b'{"ok": true}')
        with mock.patch.object(self.m.urllib.request, "urlopen", side_effect=fake_urlopen):
            out = self.m.call("bili_danmaku_say", {"text": "好看"})
        self.assertEqual(sent, [self.m.BILITUBE + "/api/dm/say"])
        self.assertTrue(sent[0].startswith("http://127.0.0.1:"))
        self.assertIn("没发去 B 站", out)
        self.assertEqual(self.net.calls, [], "发弹幕一个 B 站请求都不该有")

    def test_no_route_resumes(self):
        self.assertFalse(any("resume" in r or "pause" in r for r in list(self.m.REST) + list(self.m.REST_POST)))


# ── 1. 风控暂停 ──────────────────────────────────────────────

class TestPause(Base):
    def assertPausedThenSilent(self, signal_part):
        p = guard.read_pause()
        self.assertTrue(p, "该写暂停标记")
        self.assertIn(signal_part, p["signal"])
        self.assertTrue(p["since"] and p["endpoint"])
        n = len(self.net.calls)
        d = self.c.search("猫")                       # 之后什么都不发
        self.assertEqual(d["code"], "paused")
        self.assertIn("B 站在拦，已暂停，等人来解", d["error"])
        self.assertEqual(len(self.net.calls), n)

    def test_http_412_pauses_and_no_fallback(self):
        self.net.on("GET", "/wbi/view", FakeResp(412, "blocked", ctype="text/plain"))
        d = self.c.video("BV1xx411c7mD", cover=False)
        self.assertEqual(d["code"], "paused")
        self.assertEqual(self.net.paths(), ["/x/web-interface/wbi/view"], "412 后不许退回普通版 /view")
        self.assertPausedThenSilent("HTTP 412")

    def test_http_codes(self):
        for st in (401, 403, 429):
            with self.subTest(st=st):
                guard.clear_pause("t")
                self.net.on("GET", "/wbi/search", FakeResp(st, "no", ctype="text/plain"))
                self.assertEqual(self.c.search("x")["code"], "paused")
                self.assertIn(f"HTTP {st}", guard.read_pause()["signal"])

    def test_api_codes(self):
        for code in (-352, -412, -509, -403):
            with self.subTest(code=code):
                guard.clear_pause("t")
                self.net.on("GET", "/wbi/search", FakeResp(200, {"code": code, "message": "x"}))
                self.assertEqual(self.c.search("x")["code"], "paused")
                self.assertIn(f"code {code}", guard.read_pause()["signal"])

    def test_v_voucher_and_captcha(self):
        self.net.on("GET", "/popular", FakeResp(200, {"code": 0, "data": {"v_voucher": "voucher_x"}}))
        self.assertEqual(self.c.popular()["code"], "paused")
        self.assertPausedThenSilent("v_voucher")
        guard.clear_pause("t")
        self.net.on("GET", "/popular", FakeResp(200, "<html>请完成验证码</html>", ctype="text/html"))
        self.assertEqual(self.c.popular()["code"], "paused")
        self.assertPausedThenSilent("验证页")

    def test_minus_101_is_not_pause(self):
        self.net.on("GET", "/history/cursor", FakeResp(200, {"code": -101, "message": "账号未登录"}))
        d = self.c.history()
        self.assertEqual(d["error"], "not_logged_in")
        self.assertIsNone(guard.read_pause(), "-101 该提示扫码，不该锁死")
        # 登录流程照样能走（画二维码要 segno，没装就跳过这半）
        try:
            import segno  # noqa: F401
        except ImportError:
            self.skipTest("没装 segno，扫码那半跳过")
        self.net.on("POST", "qrcode/auth_code", FakeResp(200, {"code": 0, "data": {"url": "https://x", "auth_code": "a"}}))
        q = self.c.login_qr()
        self.assertEqual(q.get("auth_code"), "a")

    def test_pause_blocks_everything_including_writes_and_status(self):
        guard.set_pause("api.bilibili.com/x/test", "HTTP 412")
        self.assertEqual(self.c.fav("BV1xx411c7mD", 1, by="user")["code"], "paused")
        self.assertEqual(self.c.danmaku("BV1xx411c7mD", 10)["code"], "paused")
        self.assertEqual(self.c.subtitle("BV1xx411c7mD", 5)["code"], "paused")
        self.assertEqual(self.c.danmaku_all(5001)["code"], "paused")
        st = self.c.status()
        self.assertTrue(st["paused"])
        self.assertEqual(self.net.calls, [])

    def test_pause_survives_new_client_and_resume_clears(self):
        guard.set_pause("api.bilibili.com/x/a", "HTTP 412")
        guard.set_pause("api.bilibili.com/x/b", "HTTP 429")      # 不覆盖第一次的原因
        self.assertEqual(bili.BiliClient().search("x")["code"], "paused")
        self.assertEqual(guard.read_pause()["endpoint"], "api.bilibili.com/x/a")
        self.assertTrue(guard.clear_pause("t"))
        self.assertIsNone(guard.read_pause())
        self.assertNotEqual(self.c.search("x").get("code"), "paused")

    def test_resume_cli_refuses_without_tty(self):
        guard.set_pause("api.bilibili.com/x/a", "HTTP 412")
        with mock.patch.object(sys, "stdin", io.StringIO("yes\n")), mock.patch("sys.stdout", new=io.StringIO()):
            rc = bili._resume([])
        self.assertEqual(rc, 2)
        self.assertTrue(guard.read_pause(), "不是终端就不解")

    def test_mcp_and_rest_gate(self):
        import bili_mcp
        bili_mcp.CLIENT = self.c
        guard.set_pause("api.bilibili.com/x/a", "code -352")
        with self.assertRaises(bili_mcp.ToolError) as cm:
            bili_mcp.call("bili_search", {"q": "x"})
        self.assertIn("等人来解", str(cm.exception))
        out = bili_mcp.call("bili_status", {})
        self.assertIn("暂停中", out)
        self.assertEqual(self.net.calls, [])

    def test_rest_http_gate(self):
        import threading
        import urllib.error
        import urllib.request
        import bili_mcp
        bili_mcp.CLIENT = self.c
        srv = bili_mcp.ThreadingHTTPServer(("127.0.0.1", 0), bili_mcp.H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        guard.set_pause("api.bilibili.com/x/a", "HTTP 412")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(base + "/search?q=x", timeout=5)
        self.assertEqual(cm.exception.code, 503)
        self.assertIn("等人来解", json.loads(cm.exception.read())["error"])
        req = urllib.request.Request(base + "/fav", data=b'{"id":"BV1xx411c7mD"}', method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 503)
        h = json.loads(urllib.request.urlopen(base + "/healthz", timeout=5).read())
        self.assertTrue(h["paused"])
        self.assertEqual(self.net.calls, [])

    def test_transport_retry_get_only_once(self):
        calls = {"n": 0}

        def flaky(url, params, data):
            calls["n"] += 1
            if calls["n"] == 1:
                raise requests.ConnectionError("reset")
            return FakeResp(200, {"code": 0, "data": {"list": []}})
        self.net.on("GET", "/popular", flaky)
        self.assertEqual(self.c.popular()["count"], 0)
        self.assertEqual(calls["n"], 2)
        calls["n"] = -10
        self.net.on("GET", "/popular", lambda *a: (_ for _ in ()).throw(requests.Timeout("t")))
        self.assertIn("error", self.c.popular())
        self.assertIsNone(guard.read_pause(), "传输错误不算风控")


# ── 3. 收藏护栏 ──────────────────────────────────────────────

def folders(attr=23, fav_state=0, fid=POCKET, mid=42):
    return FakeResp(200, {"code": 0, "data": {"list": [
        {"id": 1, "title": "默认收藏夹", "attr": 0, "mid": mid, "fav_state": 0},
        {"id": fid, "title": "AI 的口袋", "attr": attr, "mid": mid, "fav_state": fav_state}]}})


class TestFav(Base):
    def test_companion_into_private_pocket(self):
        self.net.on("GET", "/fav/folder/created/list-all", folders())
        d = self.c.fav("BV1xx411c7mD", by="companion")
        self.assertTrue(d["ok"])
        post = [c for c in self.net.calls if c["method"] == "POST"]
        self.assertEqual(len(post), 1)
        self.assertEqual(post[0]["data"]["add_media_ids"], POCKET)

    def test_companion_other_folder_refused(self):
        d = self.c.fav("BV1xx411c7mD", folder_id=1, by="companion")
        self.assertEqual(d["code"], "folder_denied")
        self.assertEqual(self.net.paths("POST"), [])

    def test_companion_public_pocket_refused(self):
        self.net.on("GET", "/fav/folder/created/list-all", folders(attr=22))
        d = self.c.fav("BV1xx411c7mD", by="companion")
        self.assertEqual(d["code"], "folder_denied")
        self.assertIn("不是私密", d["error"])
        self.assertEqual(self.net.paths("POST"), [])

    def test_companion_already_in_no_post(self):
        self.net.on("GET", "/fav/folder/created/list-all", folders(fav_state=1))
        d = self.c.fav("BV1xx411c7mD", by="companion")
        self.assertTrue(d["already"])
        self.assertEqual(self.net.paths("POST"), [])

    def test_companion_no_pocket_configured(self):
        with mock.patch.object(guard, "pocket_id", return_value=None):
            d = self.c.fav("BV1xx411c7mD", by="companion")
        self.assertEqual(d["code"], "folder_denied")
        self.assertEqual(self.net.calls, [])

    def test_mcp_default_is_companion(self):
        import bili_mcp
        bili_mcp.CLIENT = self.c
        with self.assertRaises(bili_mcp.ToolError):
            bili_mcp.call("bili_fav", {"id": "BV1xx411c7mD", "folder_id": 1})
        self.assertEqual(self.net.paths("POST"), [])
        out = bili_mcp.call("bili_fav", {"id": "BV1xx411c7mD", "folder_id": 1, "by": "user"})
        self.assertIn("TA 的收藏夹 1", out)

    def test_pocket_id_from_env(self):
        with mock.patch.dict(os.environ, {"BILI_POCKET_FOLDER_ID": "777"}):
            self.assertEqual(guard.pocket_id(), 777)


# ── 4. 字幕身份 ──────────────────────────────────────────────

class TestSubtitle(Base):
    def test_mismatch_means_no_subtitle(self):
        self.net.on("GET", "/x/player/wbi/v2", FakeResp(200, {"code": 0, "data": {
            "bvid": "BV1xx411c7mD", "aid": 170001, "cid": 9999,
            "subtitle": {"subtitles": [{"lan": "zh-CN", "subtitle_url": "//aisubtitle.hdslb.com/x.json"}]}}}))
        d = self.c.subtitle("BV1xx411c7mD")
        self.assertFalse(d["has"])
        self.assertEqual(d["mismatch"]["field"], "cid")
        self.assertFalse(any("aisubtitle" in c["url"] for c in self.net.calls), "对不上就别去拉那条字幕")
        self.assertFalse(any(urllib.parse.urlsplit(c["url"]).path == "/x/player/v2" for c in self.net.calls))

    def test_signed_and_matching(self):
        self.net.on("GET", "/x/player/wbi/v2", FakeResp(200, {"code": 0, "data": {
            "bvid": "BV1xx411c7mD", "aid": 170001, "cid": 5001,
            "subtitle": {"subtitles": [{"lan": "zh-CN", "subtitle_url": "//aisubtitle.hdslb.com/x.json"}]}}}))
        self.net.on("GET", "aisubtitle.hdslb.com", FakeResp(200, {"body": [{"from": 1, "to": 2, "content": "你好"}]}))
        d = self.c.subtitle("BV1xx411c7mD")
        self.assertTrue(d["has"])
        player = next(c for c in self.net.calls if "/x/player/wbi/v2" in c["url"])
        self.assertIn("w_rid", player["params"])
        sub = next(c for c in self.net.calls if "aisubtitle" in c["url"])
        self.assertNotIn("Cookie", sub["headers"], "字幕 CDN 不带 cookie")

    def test_view_identity(self):
        bad = json.loads(json.dumps(VIEW))
        bad["data"]["bvid"] = "BV1yy411c7mE"
        self.net.on("GET", "/wbi/view", FakeResp(200, bad))
        self.assertEqual(self.c.subtitle("BV1xx411c7mD")["code"], "mismatch")


# ── 5. 预算 ─────────────────────────────────────────────────

class TestBudget(Base):
    def test_mcp_lane_limited_rest_not(self):
        with mock.patch.dict(os.environ, {"BILI_HOURLY_BUDGET": "3"}):
            guard.reset_budgets()
            with guard.lane("mcp"):
                for _ in range(3):
                    self.assertNotIn("error", self.c.popular())
                d = self.c.popular()
            self.assertEqual(d["code"], "over_budget")
            self.assertIn("这一小时刷够了", d["error"])
            self.assertIsNone(guard.read_pause(), "预算到了不算风控")
            with guard.lane("rest"):
                for _ in range(5):
                    self.assertNotIn("error", self.c.popular())
        self.assertEqual(len(self.net.calls), 8)


class TestWriteBudget(Base):
    def test_mcp_writes_capped_rest_not(self):
        import bili_mcp
        bili_mcp.CLIENT = self.c
        self.net.on("GET", "/fav/folder/created/list-all", folders())
        with mock.patch.dict(os.environ, {"BILI_WRITE_HOURLY_BUDGET": "2"}):
            guard.reset_budgets()
            bili_mcp.call("bili_like", {"id": "BV1xx411c7mD"})
            bili_mcp.call("bili_fav", {"id": "BV1xx411c7mD"})
            with self.assertRaises(bili_mcp.ToolError) as cm:
                bili_mcp.call("bili_like", {"id": "BV1xx411c7mD"})
            self.assertIn("点赞＋收藏", str(cm.exception))
            self.assertEqual(len(self.net.paths("POST")), 2)
            self.assertIsNone(guard.read_pause())
            with guard.lane("rest"):                 # TA 在 bilitube 自己按的不卡
                for _ in range(3):
                    self.assertTrue(bili_mcp.w_like({"id": "BV1xx411c7mD"})["ok"])
            self.assertEqual(len(self.net.paths("POST")), 5)

    def test_default_budgets(self):
        with mock.patch.dict(os.environ, {"BILI_CONFIG": os.path.join(TMP, "none.json")}):
            guard.reset_budgets()
            self.assertEqual(guard.budget("mcp").limit, 30)
            self.assertEqual(guard.budget("write").limit, 20)
            self.assertEqual(guard.budget("rest").limit, 0)
        guard.reset_budgets()


# ── 6. 头像透传 ──────────────────────────────────────────────

class TestFace(Base):
    def test_lists_carry_face(self):
        item = {"bvid": "BV1xx411c7mD", "title": "t", "owner": {"name": "u", "mid": 7, "face": "//i0.hdslb.com/f.jpg"},
                "stat": {"view": 1}, "duration": 61, "pic": "//i0.hdslb.com/c.jpg"}
        self.net.on("GET", "/top/feed/rcmd", FakeResp(200, {"code": 0, "data": {"item": [item]}}))
        self.net.on("GET", "/popular", FakeResp(200, {"code": 0, "data": {"list": [item]}}))
        self.net.on("GET", "/search/type", FakeResp(200, {"code": 0, "data": {"result": [
            {"bvid": "BV1xx411c7mD", "title": "<em>t</em>", "author": "u", "mid": 7, "upic": "https://i1.hdslb.com/u.jpg"}]}}))
        self.assertEqual(self.c.recommend()["list"][0]["face"], "https://i0.hdslb.com/f.jpg")
        self.assertEqual(self.c.popular()["list"][0]["face"], "https://i0.hdslb.com/f.jpg")
        self.assertEqual(self.c.search("t")["list"][0]["face"], "https://i1.hdslb.com/u.jpg")
        self.net.on("GET", "/web-interface/nav", FakeResp(200, {"code": 0, "data": {"uname": "测试", "face": "http://i0.hdslb.com/me.jpg"}}))
        import bili_mcp
        bili_mcp.CLIENT = self.c
        me = bili_mcp.r_me({})
        self.assertEqual((me["face"], me["uname"], me["mid"]), ("https://i0.hdslb.com/me.jpg", "测试", 42))
        self.assertFalse(any(c["url"].endswith(".jpg") for c in self.net.calls), "头像只透传地址，不下载")


if __name__ == "__main__":
    unittest.main()


class TestLiveDanmu(Base):
    """直播弹幕入场券：只读 GET、匿名（不带登录 cookie，只带 buvid）、撞风控照样停。"""
    INFO = {"code": 0, "data": {"token": "FAKE-DM-TOKEN", "host_list": [
        {"host": "broadcastlv.chat.bilibili.com", "port": 2243, "wss_port": 443, "ws_port": 2244},
        {"host": "evil.example.com", "wss_port": 443}]}}

    def test_anonymous_get_only(self):
        self.net.on("GET", "/getDanmuInfo", FakeResp(200, self.INFO))
        d = self.c.live_danmu_info(2)
        self.assertEqual(d["token"], "FAKE-DM-TOKEN")
        self.assertEqual([h["host"] for h in d["hosts"]], ["broadcastlv.chat.bilibili.com"], "只认 *.bilibili.com")
        self.assertEqual(d["buvid"], "b3")
        call = [c for c in self.net.calls if "getDanmuInfo" in c["url"]][0]
        self.assertEqual(call["method"], "GET")
        self.assertNotIn(FAKE_SESS, call["headers"].get("Cookie", ""))
        self.assertIn("buvid3=b3", call["headers"].get("Cookie", ""))
        self.assertIn("w_rid", call["params"])
        self.assertEqual(self.net.paths("POST"), [])

    def test_risk_pauses(self):
        self.net.on("GET", "/getDanmuInfo", FakeResp(200, {"code": -352, "message": "风控校验失败"}))
        d = self.c.live_danmu_info(1)
        self.assertEqual(d["code"], "paused")
        self.assertTrue(guard.read_pause())

    def test_rest_route(self):
        import bili_mcp
        self.assertIn("/live_danmu", bili_mcp.REST)
        self.assertNotIn("/live_danmu", bili_mcp.REST_POST)
