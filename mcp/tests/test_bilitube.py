"""
bilitube 七样的离线测试：工具表形状、每个 action 路由到哪个实现、动账号那组仍受白名单和预算管、
by=companion 只进口袋、play_now 的警告、未知 action 的报错、回话带图。
B 站网络用 test_guard 的假网络；bilitube（watch.api）整个换成假的，一个真请求都不发。

跑法：cd mcp && python3 -m unittest tests.test_bilitube -v
"""
import json
import os
import re
import tempfile
import threading
import urllib.request
from unittest import mock

from tests.test_guard import Base, FakeResp, POCKET, folders, guard  # noqa: F401

import bili_mcp  # noqa: E402
import bilitube  # noqa: E402
import watch  # noqa: E402

BV = "BV1xx411c7mD"
SEVEN = ["login", "find", "video", "library", "watching", "stage", "account"]


class T(Base):
    def setUp(self):
        super().setUp()
        bili_mcp.CLIENT = self.c
        self.cine = []                                   # bilitube 收到的请求

        def fake_api(path, params=None, payload=None, raw=False, timeout=90, method=None):
            self.cine.append((path, params, payload, method))
            return {"_error": "测试里没有 bilitube"}
        p = mock.patch.object(watch, "api", side_effect=fake_api)
        p.start()
        self.addCleanup(p.stop)

    def call(self, tool, **a):
        return bilitube.call(tool, a)


class TestShape(T):
    def test_seven_tools(self):
        self.assertEqual([t["name"] for t in bili_mcp.TOOLS], SEVEN)
        for t in bili_mcp.TOOLS:
            with self.subTest(tool=t["name"]):
                self.assertRegex(t["name"], r"^[a-z_]+$")
                props = t["inputSchema"]["properties"]
                self.assertEqual(props["action"]["enum"], list(bilitube.ROUTES[t["name"]]))
                self.assertIn("action", t["inputSchema"]["required"])
                self.assertRegex(t["description"], r"[一-鿿]")
                self.assertLess(len(t["description"]), 260)
                for a in props["action"]["enum"]:
                    self.assertIn(a, t["description"], "每个 action 在说明里有一小句")
        self.assertLessEqual(len(bili_mcp.INSTRUCTIONS), 200)

    def test_unified_params(self):
        by_name = {t["name"]: t["inputSchema"]["properties"] for t in bili_mcp.TOOLS}
        for name in ("video", "stage", "account"):
            self.assertIn("ref", by_name[name])
            self.assertNotIn("id" if name != "stage" else "bvid", by_name[name])
        self.assertEqual(by_name["account"]["by"]["enum"], ["companion", "user"])
        self.assertEqual(by_name["watching"]["quality"]["enum"], ["省", "标准", "高清", "单帧"])

    def test_warnings_in_descriptions(self):
        d = {t["name"]: t["description"] for t in bili_mcp.TOOLS}
        self.assertIn("play_now：立刻切过去，会打断用户，只在用户明说现在就看时用", d["stage"])
        self.assertTrue(d["stage"].startswith("会惊动用户"))
        self.assertTrue(d["account"].startswith("会改用户的 B 站账号"))
        self.assertIn("不重试", d["account"])
        self.assertIn("stage 会惊动用户", bili_mcp.INSTRUCTIONS)
        self.assertIn("account 会改", bili_mcp.INSTRUCTIONS)

    def test_unknown_action_and_tool(self):
        with self.assertRaises(bili_mcp.ToolError) as cm:
            self.call("find", action="trending")
        self.assertIn("search / feed / hot", str(cm.exception))
        self.assertIn("trending", str(cm.exception))
        with self.assertRaises(bili_mcp.ToolError) as cm:
            self.call("video")
        self.assertIn("没给 action", str(cm.exception))
        with self.assertRaises(bili_mcp.ToolError) as cm:
            bilitube.call("bili_feed", {})
        self.assertIn("七样", str(cm.exception))
        self.assertEqual(self.net.calls, [])


# (tool, action, 额外参数, 被换掉的实现, 期望它收到的参数子集)
CORE = [
    ("login", "qr", {}, "t_login", {}),
    ("login", "status", {}, "t_status", {}),
    ("find", "feed", {"page": 2}, "t_feed", {"kind": "recommend", "fresh": 2}),
    ("find", "hot", {}, "t_feed", {"kind": "popular"}),
    ("find", "following", {"mid": 7}, "t_feed", {"kind": "dynamic", "host_mid": 7}),
    ("find", "live", {}, "t_feed", {"kind": "live"}),
    ("video", "info", {"ref": BV, "cover": True}, "t_video", {"id": BV, "cover": True}),
    ("video", "comments", {"ref": BV, "limit": 3}, "t_comments", {"id": BV, "n": 3}),
    ("video", "replies", {"ref": BV, "rpid": 9}, "t_replies", {"id": BV, "rpid": 9}),
    ("video", "subtitle", {"ref": "bili:" + BV + "_p2", "t": "1:05"}, "t_subtitle", {"id": BV + "?p=2", "p": 2, "t": "1:05"}),
    ("video", "danmaku", {"ref": BV, "t": 30}, "t_danmaku", {"id": BV, "t": 30}),
    ("library", "history", {"limit": 4}, "t_history", {"n": 4}),
    ("library", "favorites", {}, "t_fav", {"folders": True}),
    ("library", "watchlater", {}, "t_watchlater", {"action": "list"}),
    ("library", "my_replies", {}, "t_my_replies", {}),
    ("stage", "card", {"ref": BV, "text": "看这个"}, "t_share", {"id": BV, "note": "看这个"}),
    ("stage", "danmaku", {"text": "哈", "mode": "top"}, "t_danmaku_say", {"text": "哈", "mode": "top"}),
    ("account", "like", {"ref": BV}, "t_like", {"id": BV, "on": True}),
    ("account", "unlike", {"ref": BV}, "t_like", {"id": BV, "on": False}),
    ("account", "fav", {"ref": BV}, "t_fav", {"id": BV, "by": "companion"}),
    ("account", "unfav", {"ref": BV, "by": "user", "folder_id": 5}, "t_fav",
     {"id": BV, "by": "user", "remove": True, "folder_id": 5}),
    ("account", "later_add", {"ref": BV}, "t_watchlater", {"action": "add", "id": BV}),
    ("account", "later_remove", {"ref": BV}, "t_watchlater", {"action": "remove", "id": BV}),
]
HOME = [
    ("watching", "where", {}, "where_brief", {}),
    ("watching", "look", {"t": 12}, "act_look", {"quality": "标准", "t": 12}),
    ("watching", "look", {"quality": "高清"}, "act_look", {"quality": "高清"}),
    ("video", "subtitle", {"film": "some-film", "t": 5}, "act_sub", {"film": "some-film", "t": 5}),
    ("stage", "danmaku", {"text": "嗯", "t": 3}, "act_dm", {"text": "嗯", "t": 3}),
    ("stage", "danmaku", {"sub": "del", "id": "f1"}, "act_dm", {"sub": "del", "id": "f1"}),
    ("stage", "play_now", {"ref": "bili:" + BV}, "act_order", {"ref": BV, "play": "now"}),
]


class TestRouting(T):
    def test_bili_side_routes(self):
        for tool, act, extra, fn, want in CORE:
            with self.subTest(tool=tool, action=act):
                with mock.patch.object(bili_mcp, fn, return_value="ok") as m:
                    self.assertEqual(self.call(tool, action=act, **extra), "ok")
                m.assert_called_once()
                got = m.call_args[0][0]
                for k, v in want.items():
                    self.assertEqual(got.get(k), v, f"{fn} 的 {k}")

    def test_home_side_routes(self):
        for tool, act, extra, fn, want in HOME:
            with self.subTest(tool=tool, action=act, extra=extra):
                with mock.patch.object(watch, fn, return_value="ok") as m:
                    self.assertEqual(self.call(tool, action=act, **extra), "ok")
                m.assert_called_once()
                got = m.call_args[0][0]
                for k, v in want.items():
                    self.assertEqual(got.get(k), v, f"{fn} 的 {k}")
        self.assertEqual(self.net.calls, [], "bilitube 那半不该直接碰 B 站")

    def test_card_with_queue_does_both(self):
        # card 带 queue＝加进 B 站稍后再看＋发卡（没有另一份「播放列表」）
        self.net.on("GET", "/x/v2/history/toview", FakeResp(200, {"code": 0, "data": {"count": 0, "list": []}}))
        with mock.patch.object(watch, "act_order", return_value="插播") as o, \
                mock.patch.object(bili_mcp, "t_share", return_value="卡发了") as s:
            out = self.call("stage", action="card", ref=BV, queue=True)
        o.assert_not_called()
        s.assert_called_once()
        self.assertIn("/x/v2/history/toview/add", self.net.paths("POST"))
        self.assertIn("B 站稍后再看", out)
        self.assertIn("卡发了", out)


LATER_EMPTY = FakeResp(200, {"code": 0, "data": {"count": 0, "list": []}})


class TestLaterQueue(T):
    """待看的单子只有一份＝用户 B 站账号的稍后再看。stage queue 往里加；限速：每小时 N 次、两次至少隔 M 秒，没到就拒。"""

    def setUp(self):
        super().setUp()
        self.net.on("GET", "/x/v2/history/toview", LATER_EMPTY)

        def view(url, params, data):              # 哪个 BV 问就回哪个（不同片子各有各的 aid）
            from tests.test_guard import VIEW
            bv = (params or {}).get("bvid") or BV
            d = json.loads(json.dumps(VIEW))
            d["data"].update(bvid=bv, aid=170001 + sum(map(ord, bv)))
            return FakeResp(200, d)
        self.net.on("GET", "/wbi/view", view)

    def queue(self, ref=BV):
        with mock.patch.object(watch, "act_order", return_value="⏭ 排成下一个播放") as o:
            out = self.call("stage", action="queue", ref=ref)
        return out, o

    def test_queue_adds_to_bili_later_then_cues_next(self):
        out, o = self.queue()
        self.assertEqual(self.net.paths("POST"), ["/x/v2/history/toview/add"])
        self.assertIn("加进用户的 B 站稍后再看了", out)
        self.assertIn("排成下一个播放", out)
        self.assertEqual(o.call_args[0][0], {"ref": BV, "play": "next"})

    def test_already_there_no_write(self):
        self.net.on("GET", "/x/v2/history/toview", FakeResp(200, {"code": 0, "data": {"count": 1, "list": [
            {"bvid": BV, "aid": 170001, "title": "测试片", "owner": {}, "stat": {}}]}}))
        out, _ = self.queue()
        self.assertEqual(self.net.paths("POST"), [])
        self.assertIn("本来就有", out)

    def test_min_gap_refuses_without_retry(self):
        self.queue()
        with self.assertRaises(bili_mcp.ToolError) as cm:
            self.queue("BV1yy411c7mE")
        self.assertRegex(str(cm.exception), r"还要等 \d+ 秒")
        self.assertEqual(self.net.paths("POST"), ["/x/v2/history/toview/add"], "被拒那次一个写请求都不发")
        self.assertIsNone(guard.read_pause(), "限速不是风控")

    def test_hourly_cap(self):
        with mock.patch.dict(os.environ, {"BILI_LATER_MIN_GAP": "0", "BILI_LATER_HOURLY_BUDGET": "2"}):
            guard.reset_budgets()
            self.queue(); self.queue("BV1yy411c7mE")
            with self.assertRaises(bili_mcp.ToolError) as cm:
                self.queue("BV1zz411c7mF")
        self.assertIn("这一小时往 B 站稍后再看里已经加了 2 次", str(cm.exception))
        self.assertEqual(len(self.net.paths("POST")), 2)

    def test_stricter_of_two_budgets_wins(self):
        with mock.patch.dict(os.environ, {"BILI_LATER_MIN_GAP": "0", "BILI_LATER_HOURLY_BUDGET": "20",
                                          "BILI_WRITE_HOURLY_BUDGET": "1"}):
            guard.reset_budgets()
            self.queue()
            with self.assertRaises(bili_mcp.ToolError) as cm:
                self.queue("BV1yy411c7mE")
            self.assertIn("点赞＋收藏＋关注＋B 站稍后再看已经 1 次", str(cm.exception))
            with self.assertRaises(bili_mcp.ToolError):    # 稍后再看占了合计额度，点赞也停
                self.call("account", action="like", ref=BV)
        self.assertEqual(len(self.net.paths("POST")), 1)

    def test_account_later_add_shares_limit(self):
        self.call("account", action="later_add", ref=BV)
        with self.assertRaises(bili_mcp.ToolError) as cm:
            self.call("account", action="later_add", ref="BV1yy411c7mE")
        self.assertIn("还要等", str(cm.exception))

    def test_user_own_clicks_not_limited(self):
        with guard.lane("rest"):                      # 用户在 bilitube 页面上自己点的
            for bv in (BV, "BV1yy411c7mE", "BV1zz411c7mF"):
                self.assertTrue(bili_mcp.w_watchlater({"id": bv})["ok"])
        self.assertEqual(len(self.net.paths("POST")), 3)

    def test_play_now_does_not_touch_later(self):
        with mock.patch.object(watch, "act_order", return_value="▶ 插播了") as o:
            self.call("stage", action="play_now", ref=BV)
        self.assertEqual(o.call_args[0][0], {"ref": BV, "play": "now"})
        self.assertEqual(self.net.calls, [])

    def test_description_says_it_writes_account(self):
        d = {t["name"]: t["description"] for t in bili_mcp.TOOLS}
        self.assertIn("B 站稍后再看", d["stage"].split("。")[0])
        self.assertNotIn("播放列表", json.dumps(bili_mcp.TOOLS, ensure_ascii=False))


class TestRoutingTail(T):
    def test_home_errors_become_tool_errors(self):
        with self.assertRaises(bili_mcp.ToolError) as cm:
            self.call("watching", action="where")
        self.assertIn("测试里没有 bilitube", str(cm.exception))

    def test_search_has_pgc_section(self):
        def search(url, params, data):
            st = params.get("search_type")
            if st == "video":
                return FakeResp(200, {"code": 0, "data": {"numResults": 2, "result": [
                    {"title": "<em>猫</em>片", "bvid": BV, "author": "UP", "play": 12000, "duration": "1:00"}]}})
            if st == "media_bangumi":
                return FakeResp(200, {"code": 0, "data": {"result": [
                    {"season_id": 33, "title": "猫番", "season_type_name": "番剧", "ep_size": 12}]}})
            return FakeResp(200, {"code": 0, "data": {"result": []}})
        self.net.on("GET", "/wbi/search/type", search)
        out = self.call("find", action="search", q="猫")
        lines = out.splitlines()
        self.assertTrue(lines[1].startswith("1. 猫片"))
        self.assertIn("番剧 · 影视：", lines)
        self.assertTrue(any(re.match(r"^2\. 猫番（番剧.*ss33$", ln) for ln in lines), out)
        out2 = self.call("find", action="search", q="猫", page=2)
        self.assertNotIn("番剧 · 影视", out2)

    def test_bangumi_and_folder(self):
        self.net.on("GET", "/space/bangumi/follow/list", FakeResp(200, {"code": 0, "data": {"total": 1, "list": [
            {"season_id": 44, "title": "追的番", "season_type_name": "番剧", "progress": "看到第3话"}]}}))
        out = self.call("find", action="bangumi")
        self.assertIn("1. 追的番（番剧 · 看到第3话）  ss44", out)
        self.assertEqual(self.net.calls[-1]["params"]["type"], 1)
        self.call("find", action="bangumi", kind="drama")
        self.assertEqual(self.net.calls[-1]["params"]["type"], 2)
        self.net.on("GET", "/fav/resource/list", FakeResp(200, {"code": 0, "data": {
            "info": {"title": "口袋", "media_count": 1}, "medias": [{"title": "收着的", "bvid": BV, "id": 1}]}}))
        out = self.call("library", action="favorites", folder_id=POCKET)
        self.assertIn("1. 收着的", out)
        self.assertIn("口袋", out)
        self.assertEqual(self.net.paths("POST"), [])


class TestAccount(T):
    def test_writes_go_through_whitelist(self):
        self.call("account", action="like", ref=BV)
        self.call("account", action="later_add", ref=BV)
        self.assertEqual(self.net.paths("POST"), ["/x/web-interface/archive/like", "/x/v2/history/toview/add"])

    def test_write_budget_shared(self):
        self.net.on("GET", "/fav/folder/created/list-all", folders())
        with mock.patch.dict(os.environ, {"BILI_WRITE_HOURLY_BUDGET": "2"}):
            guard.reset_budgets()
            self.call("account", action="like", ref=BV)
            bili_mcp.call("bili_fav", {"id": BV})              # 同一本账：进程内别的入口也记在这上面
            with self.assertRaises(bili_mcp.ToolError) as cm:
                self.call("account", action="unlike", ref=BV)
            self.assertIn("点赞＋收藏", str(cm.exception))
        self.assertEqual(len(self.net.paths("POST")), 2)
        guard.reset_budgets()

    def test_pause_blocks_without_requests(self):
        guard.set_pause("api.bilibili.com/x/a", "code -352")
        for tool, a in (("account", {"action": "like", "ref": BV}), ("account", {"action": "fav", "ref": BV}),
                        ("find", {"action": "search", "q": "x"}), ("video", {"action": "info", "ref": BV}),
                        ("stage", {"action": "card", "ref": BV})):
            with self.subTest(tool=tool, a=a):
                with self.assertRaises(bili_mcp.ToolError) as cm:
                    bilitube.call(tool, a)
                self.assertIn("等人来解", str(cm.exception))
        self.assertIn("暂停中", self.call("login", action="status"))
        self.assertEqual(self.net.calls, [])

    def test_companion_only_into_pocket(self):
        self.net.on("GET", "/fav/folder/created/list-all", folders())
        with self.assertRaises(bili_mcp.ToolError):
            self.call("account", action="fav", ref=BV, folder_id=1)
        self.assertEqual(self.net.paths("POST"), [])
        out = self.call("account", action="fav", ref=BV)
        self.assertIn("口袋", out)
        post = [c for c in self.net.calls if c["method"] == "POST"]
        self.assertEqual(len(post), 1)
        self.assertEqual(str(post[0]["data"]["add_media_ids"]), str(POCKET))
        self.call("account", action="fav", ref=BV, by="user", folder_id=1)
        self.assertEqual(len(self.net.paths("POST")), 2)
        with self.assertRaises(bili_mcp.ToolError):
            self.call("account", action="fav", ref=BV, by="someone")

    def test_ref_required(self):
        with self.assertRaises(bili_mcp.ToolError) as cm:
            self.call("account", action="like")
        self.assertIn("ref", str(cm.exception))
        self.assertEqual(self.net.calls, [])


class TestMcpHttp(T):
    def setUp(self):
        super().setUp()
        srv = bili_mcp.ThreadingHTTPServer(("127.0.0.1", 0), bili_mcp.H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.base = f"http://127.0.0.1:{srv.server_address[1]}"

    def rpc(self, method, params=None):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode()
        req = urllib.request.Request(self.base + "/mcp", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=10).read())["result"]

    def test_initialize_and_list(self):
        r = self.rpc("initialize", {"protocolVersion": "2025-03-26"})
        self.assertEqual(r["serverInfo"]["name"], "bilitube")
        self.assertEqual(r["instructions"], bili_mcp.INSTRUCTIONS)
        self.assertEqual([t["name"] for t in self.rpc("tools/list")["tools"]], SEVEN)

    def test_look_image_inline_and_error_flag(self):
        tmp = tempfile.mkdtemp(prefix="bilitube-look-")
        path = os.path.join(tmp, "att-scene-x-3.jpg")
        with open(path, "wb") as f:
            f.write(b"\xff\xd8\xff" + b"0" * 1000)
        rx = re.compile(r"^(" + re.escape(tmp) + r"/att-scene-[A-Za-z0-9_.\-]+\.jpe?g)$", re.M)
        with mock.patch.object(watch, "SHOT_LINE_RE", rx), \
                mock.patch.object(watch, "act_look", return_value=f"《x》三帧：\n{path}"):
            r = self.rpc("tools/call", {"name": "watching", "arguments": {"action": "look"}})
        self.assertEqual([c["type"] for c in r["content"]], ["text", "image"])
        self.assertIn(path, r["content"][0]["text"], "路径那行照旧留着")
        self.assertFalse(r.get("isError"))
        r = self.rpc("tools/call", {"name": "find", "arguments": {"action": "nope"}})
        self.assertTrue(r["isError"])

    def test_cover_inline_only_when_asked(self):
        with mock.patch.object(bili_mcp, "t_video", return_value="片\n封面：/x/att-bili-cover-BV1.jpg"), \
                mock.patch.object(bili_mcp, "inline_shot", return_value={"type": "image", "data": "", "mimeType": "image/jpeg"}) as s:
            r = self.rpc("tools/call", {"name": "video", "arguments": {"action": "info", "ref": BV}})
            self.assertEqual(len(r["content"]), 1)
            s.assert_not_called()
            r = self.rpc("tools/call", {"name": "video", "arguments": {"action": "info", "ref": BV, "cover": True}})
        self.assertEqual([c["type"] for c in r["content"]], ["text", "image"])
