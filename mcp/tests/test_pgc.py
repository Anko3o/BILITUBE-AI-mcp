"""
番剧 / 影视（PGC）那几样的离线测试：认号、整理番剧信息、整理取流结果（放不了要说清原因）、
以及走的仍是 _send 那一处（只读、不发 POST）。网络全 mock，照 test_guard 的假网络。

跑法：cd mcp && python3 -m unittest tests.test_pgc -v
"""
import unittest

from tests.test_guard import Base, FakeResp, bili, guard  # noqa: F401  （同一套假钥匙、假网络、临时目录）


SEASON = {"code": 0, "message": "success", "result": {
    "season_id": 777, "media_id": 888, "season_title": "测试番", "type": 1, "cover": "http://i0.hdslb.com/a.png",
    "evaluate": "简介", "publish": {"is_finish": 1}, "stat": {"views": 12345, "danmakus": 67},
    "payment": {"tip": "大会员专享观看特权哦~"},
    "user_status": {"progress": {"last_ep_id": 1002, "last_ep_index": "2", "last_time": 80},
                    "vip_info": {"status": 0}},
    "episodes": [
        {"id": 1001, "aid": 11, "bvid": "BV1aa411c7m1", "cid": 101, "title": "1", "long_title": "开头",
         "duration": 1440000, "badge": "", "status": 2, "rights": {"area_limit": 0}},
        {"id": 1002, "aid": 12, "bvid": "BV1aa411c7m2", "cid": 102, "title": "2", "long_title": "第二集",
         "duration": 1440500, "badge": "会员", "status": 13, "rights": {"area_limit": 0}},
        {"id": 1003, "aid": 13, "bvid": "BV1aa411c7m3", "cid": 103, "title": "番外", "long_title": "",
         "duration": 600000, "badge": "", "status": 2, "rights": {"area_limit": 1}},
    ]}}

PLAY_OK = {"code": 0, "message": "success", "result": {
    "is_drm": False, "is_preview": 0, "status": 2, "vip_status": 0, "format": "mp4720", "quality": 64,
    "accept_quality": [80, 64, 16], "accept_description": ["高清 1080P", "高清 720P", "流畅 360P"],
    "support_formats": [{"quality": 80, "need_vip": True}, {"quality": 64}, {"quality": 16}],
    "timelength": 1440000,
    "durl": [{"url": "https://upos-sz-mirrorcos.bilivideo.com/upgcxcode/x.mp4?deadline=1900000000&e=1",
              "backup_url": ["https://b.bilivideo.com/upgcxcode/x.mp4?deadline=1900000000"], "size": 123}]}}


class TestRef(unittest.TestCase):
    def test_pgc_ref(self):
        cases = {
            "ep123456": ("ep", 123456), "ss12345": ("ss", 12345), "md28223": ("md", 28223),
            "bili:ep99": ("ep", 99), "EP42": ("ep", 42),
            "https://www.bilibili.com/bangumi/play/ep1503469?spm_id_from=333.1": ("ep", 1503469),
            "https://m.bilibili.com/bangumi/play/ss84438": ("ss", 84438),
            "https://www.bilibili.com/bangumi/media/md24846395/": ("md", 24846395),
            "看看这个 ss84438 好看": ("ss", 84438),
        }
        for s, want in cases.items():
            with self.subTest(s=s):
                self.assertEqual(bili.pgc_ref(s), want)
        for s in ("BV1ss411c7mD", "BV1ep4y1C7md", "deep123", "https://www.bilibili.com/video/BV1xx411c7mD", "", None):
            with self.subTest(s=s):
                self.assertIsNone(bili.pgc_ref(s))

    def test_resolve_keeps_bv_first(self):
        c = bili.BiliClient()
        self.assertEqual(c.resolve("BV1xx411c7mD"), {"bvid": "BV1xx411c7mD"})
        self.assertEqual(c.resolve("https://www.bilibili.com/bangumi/play/ep77"), {"pgc": ("ep", 77)})
        self.assertEqual(c.resolve("av170001"), {"aid": 170001})

    def test_index_and_title(self):
        self.assertEqual(bili.pgc_index("3"), "第3话")
        self.assertEqual(bili.pgc_index("正片"), "")
        self.assertEqual(bili.pgc_index("番外5"), "番外5")
        self.assertEqual(bili.pgc_film_title("流浪地球", {"index": ""}), "流浪地球")
        self.assertEqual(bili.pgc_film_title("测试番", {"index": "第2话"}), "测试番 第2话")


class TestSeason(unittest.TestCase):
    def test_norm_and_pick(self):
        s = bili.norm_pgc_season(SEASON["result"])
        self.assertEqual((s["season_id"], s["title"], s["type_name"], s["total"]), (777, "测试番", "番剧", 3))
        self.assertEqual(s["episodes"][0]["duration"], 1440)
        self.assertTrue(s["episodes"][2]["area_limited"])
        self.assertEqual(s["cover"], "https://i0.hdslb.com/a.png")
        self.assertEqual(bili.pgc_pick_ep(s)["ep_id"], 1002)               # 上次看到的那一集
        self.assertEqual(bili.pgc_pick_ep(s, 1001)["ep_id"], 1001)
        self.assertIsNone(bili.pgc_pick_ep(s, 5555))
        s["progress"]["ep_id"] = None
        self.assertEqual(bili.pgc_pick_ep(s)["ep_id"], 1001)               # 没看过就第一集


class TestPlay(unittest.TestCase):
    def test_ok_and_quality_honest(self):
        d = bili.norm_pgc_play(PLAY_OK)
        self.assertNotIn("error", d)
        self.assertEqual((d["qn"], d["format"], d["expires_at"]), (64, "mp4720", 1900000000))
        self.assertEqual([a["qn"] for a in d["accept"]], [80, 64, 16])      # 只列 B 站给的档
        self.assertTrue(d["accept"][0]["vip_only"])                          # 非大会员：1080 标大会员、不给选
        self.assertFalse(d["accept"][1]["vip_only"])
        vip = dict(PLAY_OK, result=dict(PLAY_OK["result"], vip_status=1))
        self.assertFalse(bili.norm_pgc_play(vip)["accept"][0]["vip_only"])

    def test_denied_reasons(self):
        R = PLAY_OK["result"]
        cases = [
            ({"code": -10403, "message": "抱歉您所在地区不可观看！"}, "area"),
            ({"code": -10403, "message": "大会员专享限制"}, "vip"),
            ({"code": -10403, "message": "需要付费购买"}, "pay"),
            ({"code": -404, "message": "啥都木有"}, "other"),
            ({"code": 0, "result": dict(R, is_drm=True)}, "drm"),
            ({"code": 0, "result": dict(R, is_preview=1, status=13)}, "vip"),
            ({"code": 0, "result": dict(R, is_preview=1, status=6, vip_status=1)}, "pay"),
            ({"code": 0, "result": dict(R, format="flv720")}, "format"),
            ({"code": 0, "result": dict(R, durl=[], dash={"video": []})}, "format"),
            ({"code": 0, "result": dict(R, durl=R["durl"] * 2)}, "format"),
        ]
        for r, want in cases:
            with self.subTest(want=want, r=str(r)[:60]):
                d = bili.norm_pgc_play(r)
                self.assertEqual((d.get("code"), d.get("reason")), ("pgc_denied", want))
                self.assertTrue(d["error"])
                self.assertNotIn("url", d)
        self.assertIn("地区", bili.norm_pgc_play(cases[0][0])["error"])
        self.assertIn("大会员", bili.norm_pgc_play(cases[1][0])["error"])

    def test_card(self):
        it = bili.norm_pgc_card({"season_id": 5, "media_id": 6, "title": '<em class="keyword">测试</em>番',
                                 "season_type_name": "番剧", "cover": "//i0.hdslb.com/b.jpg", "ep_size": 12,
                                 "badges": [{"text": "独家"}], "desc": "很长" * 80})
        self.assertEqual((it["title"], it["film"], it["badge"], it["eps"]), ("测试番", "bili:ss5", "独家", 12))
        self.assertEqual(it["cover_url"], "https://i0.hdslb.com/b.jpg")
        self.assertLessEqual(len(it["desc"]), 81)
        f = bili.norm_pgc_card({"season_id": 9, "title": "追的", "progress": "看到第6话 13:21",
                                "new_ep": {"index_show": "全12话"}}, from_follow=True)
        self.assertEqual((f["progress"], f["index_show"]), ("看到第6话 13:21", "全12话"))


class TestClient(Base):
    """走的是 _send 那一处：只发 GET，受暂停闸管；番剧那一集装成 view 的样子，评论 / 字幕的代码照用。"""

    def setUp(self):
        super().setUp()
        self.net.on("GET", "/pgc/view/web/season", FakeResp(200, SEASON))
        self.net.on("GET", "/pgc/player/web/playurl", FakeResp(200, PLAY_OK))

    def test_view_and_play_are_reads(self):
        v = self.c.video("ss777", cover=False)
        self.assertEqual((v["bvid"], v["cid"], v["pgc"]["ep_id"]), ("BV1aa411c7m2", 102, 1002))
        self.assertTrue(v["title"].startswith("测试番 第2话"))
        self.assertIn("/bangumi/play/ep1002", v["url"])
        self.assertEqual(self.c._aid_of("ep1001"), (11, None))           # 评论区拿 aid：不再问 wbi/view
        pu = self.c.pgc_playurl(1002, 102)
        self.assertEqual(pu["qn"], 64)
        self.assertEqual(self.net.paths("POST"), [])
        self.assertNotIn("/x/web-interface/wbi/view", self.net.paths())
        params = [c["params"] for c in self.net.calls if "/pgc/player/web/playurl" in c["url"]][0]
        self.assertEqual((params["ep_id"], params["cid"], params["fnval"]), (1002, 102, 1))

    def test_season_cached_and_mismatch(self):
        self.c.pgc_season("ss", 777)
        self.c.pgc_season("ep", 1001)                                     # 同一部，缓存里有
        self.assertEqual(self.net.paths().count("/pgc/view/web/season"), 1)
        c2 = bili.BiliClient()
        self.assertEqual(c2.pgc_season("ss", 778).get("code"), "mismatch")
        self.assertEqual(c2.pgc_season("ep", 4040).get("code"), -404)     # 不在正片列表里的 ep 不硬凑

    def test_area_denied_does_not_pause(self):
        self.net.on("GET", "/pgc/player/web/playurl", FakeResp(200, {"code": -10403, "message": "抱歉您所在地区不可观看！"}))
        d = self.c.pgc_playurl(1003, 103)
        self.assertEqual(d["reason"], "area")
        self.assertIsNone(guard.read_pause())                             # 地区不给看不是风控

    def test_412_on_pgc_pauses(self):
        self.net.on("GET", "/pgc/player/web/playurl", FakeResp(412, "no", ctype="text/plain"))
        self.assertEqual(self.c.pgc_playurl(1002, 102)["code"], "paused")
        self.assertIsNotNone(guard.read_pause())
        n = len(self.net.calls)
        self.assertEqual(self.c.pgc_season("ss", 999, fresh=True)["code"], "paused")
        self.assertEqual(len(self.net.calls), n, "暂停着一个请求都不发")

    def test_history_pgc_row(self):
        self.net.on("GET", "/history/cursor", FakeResp(200, {"code": 0, "data": {"cursor": {}, "list": [
            {"title": "测试番", "history": {"business": "pgc", "bvid": "BV1aa411c7m2", "epid": 1002},
             "show_title": "第2话 第二集"}]}}))
        row = self.c.history(5)["list"][0]
        self.assertEqual((row["film"], row["episode"]), ("bili:ep1002", "第2话 第二集"))


class TestHeartbeatPgc(Base):
    def setUp(self):
        super().setUp()
        import bili_mcp
        self.m = bili_mcp
        self.m.CLIENT = self.c
        self.m.HB.clear()

    def test_pgc_fields(self):
        r = self.m.w_heartbeat({"bvid": "BV1aa411c7m2", "cid": 102, "aid": 12, "progress": 30, "duration": 1440,
                                "play_type": 1, "epid": 1002, "sid": 777, "sub_type": 1})
        self.assertTrue(r.get("sent"))
        form = [c["data"] for c in self.net.calls if c["url"].endswith("/web/heartbeat")][0]
        self.assertEqual((form["type"], form["sub_type"], form["epid"], form["sid"]), (4, 1, 1002, 777))
        self.assertNotIn("/x/web-interface/wbi/view", self.net.paths())   # 给了 aid 就不再问 view


if __name__ == "__main__":
    unittest.main()
