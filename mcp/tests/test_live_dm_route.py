"""
stage danmaku 落在哪：推送里的 live:<房间>:<id> 照抄能发；没给 film 时 TA 在直播就回直播，
不悄悄记到上一部片子；TA 的直播页没连上时回一句没发出去。不碰网络：api 打桩。

跑法：cd mcp && python3 -m unittest tests.test_live_dm_route -v
"""
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import watch  # noqa: E402

ROOM = 1
OLD = {"film": "bili:BV1xx411c7mD", "t": 31.3, "idleMs": 3600 * 1000}


def fake_api(now, reply=None):
    sent = []

    def api(path, params=None, payload=None, **kw):
        if path == "/now":
            return now
        if path == "/dm/companion":
            sent.append(payload)
            if reply:
                return reply(payload)
            return {"ok": True, "live": payload["film"].startswith("live:"), "pooled": True, "held": None,
                    "danmaku": {"text": payload["text"], "id": "f1", "t": payload["t"]}}
        return {}
    return api, sent


class TestRoute(unittest.TestCase):
    def test_suffix_film_is_live(self):
        api, sent = fake_api(dict(OLD, live={"room": ROOM, "agoSec": 1, "watching": True}))
        with mock.patch.object(watch, "api", api):
            out = watch.act_dm({"film": f"live:{ROOM}:abc123", "text": "嗯"})
        self.assertEqual(sent[0]["film"], f"live:{ROOM}")
        self.assertIn("已经出现在 TA 的播放页上", out)

    def test_no_film_while_live_goes_live(self):
        api, sent = fake_api(dict(OLD, live={"room": ROOM, "agoSec": 14, "watching": False}))
        with mock.patch.object(watch, "api", api):
            watch.act_dm({"text": "不带 film"})
        self.assertEqual(sent[0]["film"], f"live:{ROOM}")

    def test_no_film_film_newer_than_live(self):
        api, sent = fake_api(dict(OLD, idleMs=2000, live={"room": ROOM, "agoSec": 600, "watching": False}))
        with mock.patch.object(watch, "api", api):
            watch.act_dm({"text": "回片子"})
        self.assertEqual(sent[0]["film"], OLD["film"])

    def test_no_film_ever_but_live(self):
        api, sent = fake_api({"film": None, "live": {"room": ROOM, "agoSec": 3, "watching": True}})
        with mock.patch.object(watch, "api", api):
            watch.act_dm({"text": "只有直播"})
        self.assertEqual(sent[0]["film"], f"live:{ROOM}")

    def test_live_page_gone_says_not_sent(self):
        api, sent = fake_api(dict(OLD, live={"room": ROOM, "agoSec": 90, "watching": False}),
                             reply=lambda p: {"_error": "TA 的直播页这会儿没连上（90 秒没来问了），没发出去"})
        with mock.patch.object(watch, "api", api):
            out = watch.act_dm({"film": f"live:{ROOM}:abc", "text": "在吗"})
        self.assertTrue(out.startswith("❌"))
        self.assertIn("没发出去", out)
        self.assertIn("没改投", out)
        self.assertEqual(sent[0]["film"], f"live:{ROOM}")

    def test_held_reply(self):
        api, _ = fake_api(dict(OLD, live={"room": ROOM, "agoSec": 15, "watching": False}),
                          reply=lambda p: {"ok": True, "live": True, "pooled": True, "held": 15,
                                           "danmaku": {"text": p["text"], "id": "f2"}})
        with mock.patch.object(watch, "api", api):
            out = watch.act_dm({"film": f"live:{ROOM}", "text": "抖"})
        self.assertIn("15 秒没连上", out)


if __name__ == "__main__":
    unittest.main()
