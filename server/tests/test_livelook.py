"""AI 主动 look 直播：TA 开着这间的页面时向页面要 TA 屏幕上此刻那张，要不到才改用服务端现抓，响应头写明图是哪来的。不连网。
跑法：cd server && python3 -m unittest discover -s tests -v"""
import base64
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

TMP = tempfile.mkdtemp(prefix="bilitube-livelook-test-")
os.environ.update(BILITUBE_DATA_DIR=TMP, BILITUBE_FILM_DIR=TMP, BILITUBE_PUSH="0", BILITUBE_LIVE_DM="0")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bilitube_server as C  # noqa: E402

JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 4000 + b"\xff\xd9"
GRAB = JPG[:-2] + b"GRAB\xff\xd9"
ROOM = 4242


class TestLiveLook(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = C.ThreadingHTTPServer(("127.0.0.1", 0), C.Handler)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        g = C.live_frame_bytes
        C.live_frame_bytes = lambda room, frames=3, width=C.TRI_W: (GRAB, None)
        urllib.request.urlopen(cls.base + "/api/frame?film=live:1&frames=1", timeout=30).read()   # 第一次请求有懒加载，先预热一下
        C.live_frame_bytes = g

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self._grab, self._wait = C.live_frame_bytes, C.LIVE_LOOK_WAIT
        C.live_frame_bytes = lambda room, frames=3, width=C.TRI_W: (GRAB, None)
        C.LIVE_LOOK_WAIT = 1.0
        with C._lock:
            C._live_seen.clear()
            C._live_dm_last.clear()
        C._live_look_req.clear()
        C._live_look_got.clear()

    def tearDown(self):
        C.live_frame_bytes, C.LIVE_LOOK_WAIT = self._grab, self._wait

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return r.read(), {k.lower(): v for k, v in r.headers.items()}

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def page(self, answer, ls="1", stop=None):
        """假页面：每 0.2 秒问一次 /dm/say，看到 shotReq 就照 answer 回。"""
        def run():
            done = set()
            while not stop.is_set():
                d = json.loads(self.get(f"/api/dm/say?since=0&live={ROOM}&ls={ls}")[0])
                rid = d.get("shotReq")
                if rid and rid not in done and answer is not None:
                    done.add(rid)
                    self.post("/api/live/lookshot", dict(room=ROOM, id=rid, **answer))
                time.sleep(0.2)
        threading.Thread(target=run, daemon=True).start()
        time.sleep(0.3)

    def look(self):
        return self.get(f"/api/frame?film=live:{ROOM}&frames=3")

    def test_page_answers_with_user_screen(self):
        stop = threading.Event()
        self.page({"shot": base64.b64encode(JPG).decode()}, stop=stop)
        try:
            body, h = self.look()
        finally:
            stop.set()
        self.assertEqual(body, JPG)
        self.assertEqual(h.get("x-live-shot"), "page")
        self.assertFalse(C._live_look_req)              # 请求号已收回，页面下一次来不会再拼

    def test_page_cannot_capture_falls_back_fast(self):
        stop = threading.Event()
        self.page({"shot": None, "err": "hidden"}, stop=stop)
        try:
            t0 = time.time()
            body, h = self.look()
        finally:
            stop.set()
        self.assertEqual(body, GRAB)
        self.assertEqual((h.get("x-live-shot"), h.get("x-live-why")), ("server", "hidden"))
        self.assertLess(time.time() - t0, 0.9)

    def test_page_silent_times_out(self):
        stop = threading.Event()
        self.page(None, stop=stop)
        try:
            body, h = self.look()
        finally:
            stop.set()
        self.assertEqual((h.get("x-live-shot"), h.get("x-live-why")), ("server", "timeout"))

    def test_old_page_not_asked(self):
        stop = threading.Event()
        self.page(None, ls="", stop=stop)
        try:
            t0 = time.time()
            body, h = self.look()
        finally:
            stop.set()
        self.assertEqual((h.get("x-live-shot"), h.get("x-live-why")), ("server", "old"))
        self.assertLess(time.time() - t0, 0.5)

    def test_not_open(self):
        body, h = self.look()
        self.assertEqual(body, GRAB)
        self.assertEqual((h.get("x-live-shot"), h.get("x-live-why")), ("server", "closed"))

    def test_recent_dm_shot_when_page_absent(self):
        C.live_dm_shot_store(ROOM, "abc", JPG)
        body, h = self.look()
        self.assertEqual(body, JPG)
        self.assertEqual((h.get("x-live-shot"), h.get("x-live-why")), ("dm", "closed"))

    def test_bad_image_and_wrong_id(self):
        C._live_look_req[ROOM] = {"id": "x1", "at": time.time()}
        self.assertFalse(C.live_look_put(ROOM, "nope", base64.b64encode(JPG).decode()))
        self.assertTrue(C.live_look_put(ROOM, "x1", base64.b64encode(b"not a jpeg" * 200).decode()))
        self.assertEqual(C._live_look_got["x1"][1:], (None, "bad"))

    def test_single_frame_goes_to_server(self):
        body, h = self.get(f"/api/frame?film=live:{ROOM}&frames=1")
        self.assertEqual((h.get("x-live-shot"), h.get("x-live-why")), ("server", "single"))


if __name__ == "__main__":
    unittest.main()
