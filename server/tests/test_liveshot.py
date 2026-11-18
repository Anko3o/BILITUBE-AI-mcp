"""直播里发弹幕时的三帧：页面带来 TA 点进输入框那一刻的图就存那张，没带或认不出才由服务端现抓。不连网。
跑法：cd server && python3 -m unittest discover -s tests -v"""
import base64
import json
import os
import sys
import tempfile
import unittest

TMP = tempfile.mkdtemp(prefix="bilitube-liveshot-test-")
os.environ.update(BILITUBE_DATA_DIR=TMP, BILITUBE_FILM_DIR=TMP, BILITUBE_PUSH="0", BILITUBE_LIVE_DM="0")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bilitube_server as C  # noqa: E402

JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 4000 + b"\xff\xd9"


class _Now:                     # 让发送线程当场跑完
    def __init__(self, target=None, daemon=None, **kw):
        self.t = target

    def start(self):
        self.t()


class TestLiveShot(unittest.TestCase):
    def setUp(self):
        self._thr, self._grab = C.threading.Thread, C.live_dm_shot_save
        C.threading.Thread = _Now
        self.grabbed = []

        def fake_grab(room, lid):
            self.grabbed.append((room, lid))
            return C.live_dm_shot_store(room, lid, JPG[:-2] + b"GRAB\xff\xd9")
        C.live_dm_shot_save = fake_grab

    def tearDown(self):
        C.threading.Thread, C.live_dm_shot_save = self._thr, self._grab

    def _last_event(self):
        with open(C.EVENTS_LOG, encoding="utf-8") as f:
            return json.loads(f.read().strip().splitlines()[-1])

    def test_page_shot_saved(self):
        C.push_user_danmaku("同事！", None, {"film": "live:2", "focusAgo": 14.2},
                           base64.b64encode(JPG).decode())
        ev = self._last_event()
        self.assertEqual(ev.get("shot"), "page")
        self.assertEqual(ev.get("focusAgo"), 14.2)
        self.assertEqual(self.grabbed, [])
        self.assertEqual(C.live_dm_shot_bytes(2, ev["id"]), JPG)
        self.assertEqual(C.live_dm_shot_bytes(2), JPG)            # 不给 id：两分钟内最近那条

    def test_fallback_when_missing_or_bad(self):
        for bad in (None, "", "not-base64!!", base64.b64encode(b"GIF89a" + b"\x00" * 3000).decode(),
                    base64.b64encode(b"\xff\xd8\xff" + b"\x00" * (C.SAY_FRAME_MAX + 1)).decode()):
            with self.subTest(bad=str(bad)[:12]):
                n = len(self.grabbed)
                C.push_user_danmaku("没带图", None, {"film": "live:2", "focusAgo": 3}, bad)
                ev = self._last_event()
                self.assertNotIn("shot", ev)
                self.assertNotIn("focusAgo", ev)
                self.assertEqual(len(self.grabbed), n + 1)
                self.assertTrue(C.live_dm_shot_bytes(2, ev["id"]).endswith(b"GRAB\xff\xd9"))

    def test_vod_untouched(self):
        C.push_user_danmaku("录播", None, {"film": "nope", "t": 3}, base64.b64encode(JPG).decode())
        self.assertEqual(self.grabbed, [])


if __name__ == "__main__":
    unittest.main()
