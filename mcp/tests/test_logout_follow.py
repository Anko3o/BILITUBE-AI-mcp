"""
退出登录和 AI 自己关注 UP 的离线测试。B 站网络用 test_guard 的假网络（requests.Session.request 换掉，
真发送一律炸）；凭据文件是 test_guard 指到临时目录里的假凭据，每个用例前后都重新写回去。

退出：本地一定清（凭据文件、内存登录态、扫码进度、没补发的进度上报、番剧缓存）；B 站那头尽力而为，
调不通 / 撞风控 / 暂停着都不挡本地清除；只能走回环 REST POST /logout，MCP 七样里没有。
关注：只进专属关注分组、只取关分组里的人，写之前现查关系；每小时上限＋最小间隔；撞风控照样整套停。

跑法：cd mcp && python3 -m unittest tests.test_logout_follow -v
"""
import json
import os
import threading
import urllib.error
import urllib.request
from unittest import mock

import requests

from tests.test_guard import FAKE_JCT, FAKE_SESS, TMP, Base, FakeResp, bili, guard  # noqa: F401

import bili_mcp  # noqa: E402
import bilitube  # noqa: E402
import watch  # noqa: E402

BV = "BV1xx411c7mD"
GROUP = 777
FAKE_CRED = {"mid": 42, "uname": "测试", "access_token": "FAKE-AT", "refresh_token": "FAKE-RT",
             "cookies": {"SESSDATA": FAKE_SESS, "bili_jct": FAKE_JCT, "DedeUserID": "42",
                         "buvid3": "b3", "buvid4": "b4"}}


def write_fake_cred():
    assert bili.CRED_PATH.startswith(TMP), "测试只许碰临时目录里的假凭据"
    with open(bili.CRED_PATH, "w") as f:
        json.dump(FAKE_CRED, f)


class T(Base):
    def setUp(self):
        write_fake_cred()
        self.addCleanup(write_fake_cred)          # 别的测试文件还要用登录着的假账号
        super().setUp()
        bili_mcp.CLIENT = self.c
        p = mock.patch.object(watch, "api", side_effect=lambda *a, **k: {"_error": "测试里没有 bilitube"})
        p.start()
        self.addCleanup(p.stop)

    def call(self, tool, **a):
        return bilitube.call(tool, a)

    def cred_on_disk(self):
        with open(bili.CRED_PATH) as f:
            return json.load(f)


# ── 退出登录 ─────────────────────────────────────────────────

class TestLogoutClient(T):
    def test_ok_clears_everything_and_tells_bili(self):
        self.net.on("POST", "/login/exit/v2", FakeResp(200, {"code": 0, "status": True, "data": {}}))
        self.c._pgc_cache, self.c._pgc_ep_sid = {1: (0, {"progress": "x"})}, {2: 1}
        r = self.c.logout()
        self.assertEqual((r["ok"], r["local_cleared"], r["remote"]), (True, True, "ok"))
        post = [c for c in self.net.calls if c["method"] == "POST"]
        self.assertEqual(len(post), 1)
        self.assertEqual(post[0]["url"], "https://passport.bilibili.com/login/exit/v2")
        self.assertEqual(post[0]["data"], {"biliCSRF": FAKE_JCT})
        self.assertIn("SESSDATA=" + FAKE_SESS, post[0]["headers"].get("Cookie", ""), "退出要带着这串登录去")
        self.assertFalse(self.c.logged_in())
        self.assertEqual(self.cred_on_disk(), {"cookies": {}})
        self.assertEqual((self.c._pgc_cache, self.c._pgc_ep_sid), ({}, {}))
        self.assertFalse(bili.BiliClient().logged_in(), "新开的客户端读盘也是没登录")
        self.assertFalse(self.c.status()["logged_in"])

    def test_bili_says_no_still_clears(self):
        self.net.on("POST", "/login/exit/v2", FakeResp(200, {"code": 2202, "status": False, "message": "csrf 请求非法"}))
        r = self.c.logout()
        self.assertEqual(r["remote"], "unconfirmed")
        self.assertIn("2202", r["note"])
        self.assertFalse(self.c.logged_in())
        self.assertEqual(self.cred_on_disk(), {"cookies": {}})

    def test_network_down_no_retry_still_clears(self):
        self.net.on("POST", "/login/exit/v2", lambda *a: (_ for _ in ()).throw(requests.Timeout("x")))
        r = self.c.logout()
        self.assertEqual(r["remote"], "unconfirmed")
        self.assertIn("没连上", r["note"])
        self.assertEqual(self.net.paths("POST"), ["/login/exit/v2"], "写操作不重试")
        self.assertFalse(self.c.logged_in())

    def test_risk_pauses_and_still_clears(self):
        self.net.on("POST", "/login/exit/v2", FakeResp(412, "no", ctype="text/plain"))
        r = self.c.logout()
        self.assertEqual(r["remote"], "unconfirmed")
        self.assertIn("风控", r["note"])
        self.assertTrue(guard.read_pause(), "撞风控照现有规矩整套暂停")
        self.assertFalse(self.c.logged_in())
        self.assertEqual(self.cred_on_disk(), {"cookies": {}})

    def test_paused_sends_nothing_still_clears(self):
        guard.set_pause("api.bilibili.com/x/a", "code -352")
        r = self.c.logout()
        self.assertEqual(r["remote"], "skipped")
        self.assertEqual(self.net.calls, [])
        self.assertFalse(self.c.logged_in())

    def test_not_logged_in_sends_nothing(self):
        self.c.clear_login()
        r = self.c.logout()
        self.assertEqual((r["ok"], r["remote"]), (True, "skipped"))
        self.assertEqual(self.net.calls, [])

    def test_exit_is_whitelisted_but_only_that_path(self):
        guard.check_write("POST", "https://passport.bilibili.com/login/exit/v2")
        for url in ("https://passport.bilibili.com/login/exit/v2?gourl=x", "http://passport.bilibili.com/login/exit/v2",
                    "https://passport.bilibili.com/login/exit/v3"):
            with self.assertRaises(guard.Blocked):
                guard.check_write("POST", url)


class TestLogoutRest(T):
    def setUp(self):
        super().setUp()
        self.net.on("POST", "/login/exit/v2", FakeResp(200, {"code": 0, "status": True, "data": {}}))
        srv = bili_mcp.ThreadingHTTPServer(("127.0.0.1", 0), bili_mcp.H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.base = f"http://127.0.0.1:{srv.server_address[1]}"

    def post(self, path, body=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(body or {}).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return json.loads(r.read())

    def test_logout_route_clears_login_state_and_pending_heartbeats(self):
        timer = threading.Timer(3600, lambda: None)
        bili_mcp.HB[5001] = {"timer": timer, "pending": ({}, BV)}
        bili_mcp.LOGIN.update({"auth_code": "x", "status": "ok", "since": 1})
        code, d = self.post("/logout")
        self.assertEqual(code, 200)
        self.assertEqual((d["ok"], d["remote"]), (True, "ok"))
        self.assertTrue(timer.finished.is_set(), "还没补发的进度上报要取消")
        self.assertEqual(bili_mcp.HB, {})
        self.assertEqual((bili_mcp.LOGIN["auth_code"], bili_mcp.LOGIN["status"]), (None, "idle"))
        me = self.get("/me")
        self.assertFalse(me["logged_in"])
        self.assertIn("未登录", self.call("login", action="status"))

    def test_logout_works_while_paused(self):
        guard.set_pause("api.bilibili.com/x/a", "code -352")
        code, d = self.post("/logout")
        self.assertEqual(code, 200)
        self.assertEqual(d["remote"], "skipped")
        self.assertEqual(self.net.calls, [], "暂停着 B 站那头一个请求都不发")
        self.assertFalse(self.c.logged_in())
        code, d = self.post("/like", {"id": BV})
        self.assertEqual(code, 503, "别的写入照旧被暂停闸挡住")

    def test_ai_has_no_way_to_logout(self):
        acts = {a for t in bili_mcp.TOOLS for a in t["inputSchema"]["properties"]["action"]["enum"]}
        self.assertFalse(any("logout" in a or "exit" in a for a in acts))
        self.assertFalse(any("logout" in n for n in bili_mcp.HANDLERS))
        self.assertIn("退出登录只能由用户在页面上做", next(t["description"] for t in bili_mcp.TOOLS if t["name"] == "login"))
        for tool in ("login", "account"):
            with self.assertRaises(bili_mcp.ToolError):
                self.call(tool, action="logout")
        self.assertTrue(self.c.logged_in())
        self.assertEqual(self.net.calls, [])


# ── AI 自己关注 ──────────────────────────────────────────────

def tags(*extra):
    return FakeResp(200, {"code": 0, "data": [
        {"tagid": -10, "name": "特别关注", "count": 1}, {"tagid": 0, "name": "默认分组", "count": 40},
        {"tagid": GROUP, "name": "AI 的关注", "count": 2}, *extra]})


class F(T):
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"BILI_FOLLOW_GROUP_ID": str(GROUP), "BILI_FOLLOW_MIN_GAP": "0"})
        env.start()
        self.addCleanup(env.stop)
        guard.reset_budgets()
        self.addCleanup(guard.reset_budgets)
        self.rel = {}                              # mid → (attribute, [tagid])
        self.net.on("GET", "/x/relation", lambda url, params, data: FakeResp(200, {"code": 0, "data": {
            "mid": int(params["fid"]), "attribute": self.rel.get(int(params["fid"]), (0, None))[0],
            "tag": self.rel.get(int(params["fid"]), (0, None))[1]}}))
        self.net.on("GET", "/x/relation/tag", FakeResp(200, {"code": 0, "data": [
            {"mid": 7, "uname": "UP 七", "face": "//i0.hdslb.com/7.jpg", "sign": "签名"},
            {"mid": 8, "uname": "UP 八", "face": "", "sign": ""}]}))
        self.net.on("GET", "/x/relation/tags", tags())

    def posts(self):
        return [(c["url"].split("bilibili.com", 1)[1], c["data"]) for c in self.net.calls if c["method"] == "POST"]


class TestFollow(F):
    def test_follow_goes_into_group(self):
        out = self.call("account", action="follow", mid=7)
        self.assertIn("AI 的关注", out)
        p = self.posts()
        self.assertEqual([x[0] for x in p], ["/x/relation/modify", "/x/relation/tags/addUsers"])
        self.assertEqual((p[0][1]["fid"], p[0][1]["act"], p[0][1]["csrf"]), (7, 1, FAKE_JCT))
        self.assertEqual((p[1][1]["fids"], p[1][1]["tagids"]), ("7", str(GROUP)))

    def test_follow_by_ref_uses_the_uploader(self):
        out = self.call("account", action="follow", ref=BV)
        self.assertIn("UP", out)
        self.assertEqual(self.posts()[0][1]["fid"], 7, "VIEW 假数据里 UP 的 mid 是 7")

    def test_already_in_group_no_write(self):
        self.rel[7] = (2, [GROUP])
        self.assertIn("已经在", self.call("account", action="follow", mid=7))
        self.assertEqual(self.posts(), [])

    def test_users_own_follow_left_alone(self):
        self.rel[7] = (2, [5])
        with self.assertRaises(bili_mcp.ToolError) as cm:
            self.call("account", action="follow", mid=7)
        self.assertIn("用户本来就关注着", str(cm.exception))
        self.rel[8] = (6, None)
        with self.assertRaises(bili_mcp.ToolError) as cm:
            self.call("account", action="unfollow", mid=8)
        self.assertIn("AI 取关不了", str(cm.exception))
        self.assertEqual(self.posts(), [])

    def test_unfollow_only_group_members(self):
        self.rel[7] = (2, [GROUP])
        self.assertIn("取关了", self.call("account", action="unfollow", mid=7))
        self.assertEqual([(x[0], x[1]["act"]) for x in self.posts()], [("/x/relation/modify", 2)])
        self.rel[9] = (0, None)
        self.assertIn("本来就没关注", self.call("account", action="unfollow", mid=9))
        self.assertEqual(len(self.posts()), 1)

    def test_blocked_and_self_refused(self):
        self.rel[7] = (128, None)
        for mid in (7, 42):
            with self.subTest(mid=mid):
                with self.assertRaises(bili_mcp.ToolError):
                    self.call("account", action="follow", mid=mid)
        self.assertEqual(self.posts(), [])

    def test_no_group_means_no_follow(self):
        self.net.on("GET", "/x/relation/tags", FakeResp(200, {"code": 0, "data": [{"tagid": 0, "name": "默认分组", "count": 3}]}))
        with self.assertRaises(bili_mcp.ToolError) as cm:
            self.call("account", action="follow", mid=7)
        self.assertIn("先在 B 站建一个关注分组", str(cm.exception))
        with mock.patch.object(guard, "follow_group", return_value=(None, None)):
            with self.assertRaises(bili_mcp.ToolError) as cm:
                self.call("account", action="follow", mid=7)
            self.assertIn("follow_group_id", str(cm.exception))
            self.assertIn("还没有关注分组", self.call("library", action="follows"))
        self.assertEqual(self.posts(), [])

    def test_default_and_special_group_never_count(self):
        for gid in ("0", "-10"):
            with self.subTest(gid=gid), mock.patch.dict(os.environ, {"BILI_FOLLOW_GROUP_ID": gid,
                                                                      "BILI_FOLLOW_GROUP_NAME": "特别关注"}):
                with self.assertRaises(bili_mcp.ToolError):
                    self.call("account", action="follow", mid=7)
        self.assertEqual(self.posts(), [])

    def test_group_by_name(self):
        with mock.patch.object(guard, "follow_group", return_value=(None, "AI 的关注")):
            self.call("account", action="follow", mid=7)
        self.assertEqual(self.posts()[1][1]["tagids"], str(GROUP))

    def test_group_step_fails_is_reported(self):
        self.net.on("POST", "/tags/addUsers", FakeResp(200, {"code": 22104, "message": "分组不存在"}))
        out = self.call("account", action="follow", mid=7)
        self.assertIn("没放进", out)
        self.assertIn("AI 取关不了", out)

    def test_needs_mid_or_ref(self):
        with self.assertRaises(bili_mcp.ToolError) as cm:
            self.call("account", action="follow")
        self.assertIn("mid", str(cm.exception))
        self.assertEqual(self.net.calls, [])


class TestFollowLimits(F):
    def test_min_gap_refuses_without_retry(self):
        with mock.patch.dict(os.environ, {"BILI_FOLLOW_MIN_GAP": "60"}):
            self.call("account", action="follow", mid=7)
            self.rel[7] = (2, [GROUP])
            with self.assertRaises(bili_mcp.ToolError) as cm:
                self.call("account", action="unfollow", mid=7)
        self.assertIn("太密", str(cm.exception))
        self.assertEqual([x[0] for x in self.posts()].count("/x/relation/modify"), 1)

    def test_hourly_cap_follow_and_unfollow_together(self):
        with mock.patch.dict(os.environ, {"BILI_FOLLOW_HOURLY_BUDGET": "2"}):
            guard.reset_budgets()
            self.call("account", action="follow", mid=7)
            self.rel[7] = (2, [GROUP])
            self.call("account", action="unfollow", mid=7)
            with self.assertRaises(bili_mcp.ToolError) as cm:
                self.call("account", action="follow", mid=8)
        self.assertIn("关注＋取关已经 2 次", str(cm.exception))
        self.assertEqual([x[0] for x in self.posts()].count("/x/relation/modify"), 2)

    def test_defaults(self):
        cfg = guard.config()
        self.assertEqual((guard.DEFAULTS["follow_hourly_budget"], guard.DEFAULTS["follow_min_gap_s"]), (5, 60))
        self.assertIsNone(guard.DEFAULTS["follow_group_id"])
        self.assertIn("follow_group_name", cfg)

    def test_pause_blocks_follow(self):
        guard.set_pause("api.bilibili.com/x/a", "code -352")
        with self.assertRaises(bili_mcp.ToolError) as cm:
            self.call("account", action="follow", mid=7)
        self.assertIn("等人来解", str(cm.exception))
        self.assertEqual(self.net.calls, [])

    def test_risk_on_follow_pauses(self):
        self.net.on("POST", "/x/relation/modify", FakeResp(200, {"code": -352, "message": "风控校验失败"}))
        with self.assertRaises(bili_mcp.ToolError):
            self.call("account", action="follow", mid=7)
        self.assertTrue(guard.read_pause())
        self.assertEqual(self.net.paths("POST"), ["/x/relation/modify"], "撞了就停，不再放进分组、不重试")

    def test_group_edits_still_blocked(self):
        for path in ("/x/relation/tag/create", "/x/relation/tag/del", "/x/relation/tag/update"):
            with self.assertRaises(guard.Blocked) as cm:
                self.c._send("POST", "https://api.bilibili.com" + path, data={})
            self.assertIn("关注分组", cm.exception.human)
        guard.check_write("POST", "https://api.bilibili.com/x/relation/tags/addUsers")
        self.assertEqual(self.net.calls, [])


class TestFollowList(F):
    def test_library_follows(self):
        out = self.call("library", action="follows")
        self.assertIn("「AI 的关注」，共 2 个", out)
        self.assertIn("1. UP 七  mid 7", out)
        self.assertEqual(self.posts(), [])
        req = next(c for c in self.net.calls if c["url"].endswith("/x/relation/tag"))
        self.assertEqual((req["params"]["tagid"], req["params"]["mid"]), (GROUP, 42))

    def test_rest_for_the_page(self):
        r = bili_mcp.r_follow_group({"n": "30"})
        self.assertEqual(r["group"]["name"], "AI 的关注")
        self.assertEqual([x["mid"] for x in r["list"]], [7, 8])
        self.assertTrue(r["list"][0]["face"].startswith("https://"))
        self.net.on("GET", "/x/relation/tags", FakeResp(200, {"code": 0, "data": []}))
        r = bili_mcp.r_follow_group({})
        self.assertIsNone(r["group"])
        self.assertNotIn("error", r, "没分组也是 200：页面照 want / reason 写空状态")
        self.assertEqual(r["want"]["id"], GROUP)

    def test_tool_table(self):
        by = {t["name"]: t for t in bili_mcp.TOOLS}
        acts = by["account"]["inputSchema"]["properties"]["action"]["enum"]
        self.assertEqual(acts[-2:], ["follow", "unfollow"])
        self.assertIn("follows", by["library"]["inputSchema"]["properties"]["action"]["enum"])
        self.assertTrue(by["account"]["description"].startswith("会改用户的 B 站账号"))
        self.assertIn("mid", by["account"]["inputSchema"]["properties"])
        self.assertEqual(by["account"]["inputSchema"]["required"], ["action"], "follow 用 mid，不再强制 ref")
