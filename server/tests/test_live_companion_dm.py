"""AI 回直播弹幕落在哪：推送里的 live:<房间>:<id> 也认；TA 的直播页网络抖十几秒仍算在这间；
断得久了明说没发出去，不记、不改投上一部片子。不连网。
跑法：cd server && python3 -m unittest discover -s tests -v"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

TMP = tempfile.mkdtemp(prefix="bilitube-livecompanion-test-")
os.environ.update(BILITUBE_DATA_DIR=TMP, BILITUBE_FILM_DIR=TMP, BILITUBE_PUSH="0", BILITUBE_LIVE_DM="0")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bilitube_server as C  # noqa: E402

ROOM = 1
OLD_FILM = "bili:BV1xx411c7mD"


class TestLiveCompanionDm(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = C.ThreadingHTTPServer(("127.0.0.1", 0), C.Handler)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        with C._lock:
            C._live_seen.clear()
            C._live_room.clear()
            C._state.clear()
            C._state.update(film=OLD_FILM, t=31.3, at=C.now_ms() - 3600 * 1000)   # 上一部片子，一小时前
            del C._dmsay[:]

    def seen(self, ago):
        with C._lock:
            C._live_seen.update(room=ROOM, poll=time.time() - ago, at=time.time() - ago)

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def journal(self):
        return [r for r in C.journal_read(None, ("danmaku",)) if r.get("author") == "companion"]

    def test_live_fid(self):
        self.assertEqual(C.live_fid(f"live:{ROOM}:abc123"), f"live:{ROOM}")
        self.assertEqual(C.live_fid(f"LIVE:{ROOM}"), f"live:{ROOM}")
        self.assertIsNone(C.live_fid(OLD_FILM))
        self.assertIsNone(C.live_fid(f"live:{ROOM}:不是编号"))

    def test_suffix_film_pooled_now(self):
        self.seen(1)
        n0 = len(self.journal())
        code, d = self.post("/api/dm/companion", {"film": f"live:{ROOM}:abc123", "text": "接住"})
        self.assertEqual(code, 200, d)
        self.assertTrue(d["live"] and d["pooled"])
        self.assertIsNone(d["held"])
        self.assertEqual(d["danmaku"]["film"], f"live:{ROOM}")
        self.assertEqual(self.journal()[-1]["film"], f"live:{ROOM}")
        self.assertEqual(len(self.journal()), n0 + 1)
        self.assertEqual(C._dmsay[-1]["film"], f"live:{ROOM}")

    def test_blip_still_live(self):
        self.seen(15)                         # 网络抖了 15 秒
        code, d = self.post("/api/dm/companion", {"film": f"live:{ROOM}", "text": "还在吗"})
        self.assertEqual(code, 200, d)
        self.assertTrue(d["pooled"])
        self.assertGreaterEqual(d["held"], 14)

    def test_gone_refuses_and_does_not_record(self):
        self.seen(C.LIVE_HOLD_SEC + 30)
        n0 = len(self.journal())
        code, d = self.post("/api/dm/companion", {"film": f"live:{ROOM}:abc", "text": "没人"})
        self.assertEqual(code, 409, d)
        self.assertIn("没连上", d["error"])
        self.assertIn("没发出去", d["error"])
        self.assertEqual(len(self.journal()), n0)

    def test_no_film_during_blip_goes_to_live_not_old_film(self):
        self.seen(12)
        code, d = self.post("/api/dm/companion", {"text": "不带 film"})
        self.assertEqual(code, 200, d)
        self.assertEqual(d["danmaku"]["film"], f"live:{ROOM}")
        self.assertNotEqual(self.journal()[-1]["film"], OLD_FILM)

    def test_dm_say_blip(self):
        self.seen(12)
        rec, err = C.dm_say("当场一条")
        self.assertIsNone(err)
        self.assertEqual(rec["film"], f"live:{ROOM}")
        self.seen(C.LIVE_HOLD_SEC + 5)
        rec, err = C.dm_say("又一条")
        self.assertEqual(err, "TA 没在看")


if __name__ == "__main__":
    unittest.main()
