"""后端的几条约定：返回的地址带 URL_PREFIX；弹幕推送的开头标记和最后一行；推送打到配置的地址。
不连外网、不碰真数据。跑法：cd server && python3 -m unittest discover -s tests -v"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TMP = tempfile.mkdtemp(prefix="bilitube-release-test-")
os.environ.setdefault("BILITUBE_DATA_DIR", TMP)
os.environ.setdefault("BILITUBE_FILM_DIR", TMP)
os.environ.update(BILITUBE_PUSH="0", BILITUBE_LIVE_DM="0")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bilitube_server as C  # noqa: E402


class TestRelease(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        open(os.path.join(C.FILM_DIR, "rel-test.mp4"), "wb").close()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), C.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def call(self, path, body=None, method=None):
        req = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                                     method=method or ("GET" if body is None else "POST"),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_film_urls_use_prefix(self):
        code, d = self.call("/api/films")
        self.assertEqual(code, 200)
        f = [x for x in d["films"] if x["id"] == "rel-test"][0]
        self.assertTrue(f["src"].startswith(C.URL_PREFIX + "/media/"), f["src"])
        self.assertTrue(f["poster"].startswith(C.URL_PREFIX + "/api/"), f["poster"])

    def test_dm_text_head_and_tail(self):
        txt = C.user_danmaku_text("rel-test", 12.5, "这段好看")
        lines = txt.split("\n")
        self.assertTrue(lines[0].startswith(C.DM_PUSH_TAG))
        self.assertIn("发了一条弹幕", lines[0])
        self.assertIn("「这段好看」", txt)
        self.assertEqual(lines[-1], "film=rel-test t=12.5")

    def test_push_post_goes_to_configured_url(self):
        got = []

        class Hook(BaseHTTPRequestHandler):
            def do_POST(self):
                got.append((self.headers.get("Authorization"), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass

        hook = ThreadingHTTPServer(("127.0.0.1", 0), Hook)
        threading.Thread(target=hook.serve_forever, daemon=True).start()
        try:
            old = (C.PUSH_URL, C._push_token, C.PUSH_EXTRA)
            C.PUSH_URL, C._push_token, C.PUSH_EXTRA = f"http://127.0.0.1:{hook.server_address[1]}/in", "tok", {"room": "r"}
            C.push_post({"source": "bilitube", "text": "hi"}, "hi")
            for _ in range(50):
                if got:
                    break
                time.sleep(0.05)
            self.assertEqual(got[0][0], "Bearer tok")
            self.assertEqual(got[0][1], {"room": "r", "source": "bilitube", "text": "hi"})
        finally:
            C.PUSH_URL, C._push_token, C.PUSH_EXTRA = old
            hook.shutdown()


if __name__ == "__main__":
    unittest.main()
