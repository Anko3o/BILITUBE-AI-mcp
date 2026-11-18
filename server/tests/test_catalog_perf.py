"""提速＋没有「播放列表」：底账只留用得着的、/bili/add 顺带直连地址、JSON 自己压、封面图让浏览器记一周。
不连网（bili_mcp 那头整个换成假的）、不碰真数据。
跑法：cd server && python3 -m unittest discover -s tests -v"""
import gzip
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from unittest import mock

TMP = tempfile.mkdtemp(prefix="bilitube-catalog-test-")
os.environ.update(BILITUBE_DATA_DIR=TMP, BILITUBE_FILM_DIR=TMP, BILITUBE_PUSH="0", BILITUBE_LIVE_DM="0")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bilitube_server as C  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402


def rec(i, when):
    return {"id": f"bili:BV1xx411c7m{i}", "bvid": f"BV1xx411c7m{i}", "cid": 100 + ord(i), "title": f"片{i}",
            "added_at": when}


def fake_bili_get(path, timeout=30):
    if path.startswith("/cid?"):
        bv = path.split("bvid=")[1].split("&")[0]
        return {"bvid": bv, "aid": 1, "cid": 77, "title": "新片", "duration": 60, "cover": "", "up": "UP",
                "pages": [{"p": 1, "cid": 77}]}
    if path.startswith("/playurl?"):
        return {"url": "https://upos-sz-mirrorcos.bilivideo.com/upgcxcode/x.mp4?deadline=9999999999",
                "backup_urls": [], "expires_at": 9999999999, "qn": 64}
    raise AssertionError("不该问这个：" + path)


class TestCatalog(unittest.TestCase):
    def setUp(self):
        self.saved = list(C._bili)
        C._state.clear()
        C.upnext_clear()
        open(C.EVENTS_LOG, "w").close()

    def tearDown(self):
        C._bili[:] = self.saved
        C._state.clear()
        C.upnext_clear()

    def test_prune_keeps_only_what_is_used(self):
        C._bili[:] = [rec(c, f"2026-11-1{n}T00:00:00") for n, c in enumerate("ABCDEFG")]
        C._bili.append(dict(rec("H", "2026-11-01T00:00:00"), parked="area"))       # 还没转正的番剧集照留
        C._state.update(film="bili:BV1xx411c7mA")                                    # 此刻在看
        C.upnext_set(None, "bili:BV1xx411c7mB")                                     # AI 排的下一个
        C.journal("danmaku", "bili:BV1xx411c7mC", author="user", text="有弹幕记录", t=3)   # 「一起聊过」要片名封面
        cut = C.prune_catalog(recent=0)
        ids = sorted(b["id"][-1] for b in C._bili)
        self.assertEqual(ids, ["A", "B", "C", "H"])
        self.assertEqual(cut, 4)
        self.assertEqual(json.load(open(C.BILI_FILE)), C._bili, "剪完落盘")

    def test_recent_cache_is_bounded(self):
        C._bili[:] = [rec(c, f"2026-11-1{n}T00:00:00") for n, c in enumerate("ABCDEFG")]
        C.prune_catalog(recent=2)
        self.assertEqual(sorted(b["id"][-1] for b in C._bili), ["F", "G"], "只留最近开过的两条")


class TestHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), C.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.saved = list(C._bili)
        C._direct_pu.clear()

    def tearDown(self):
        C._bili[:] = self.saved

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def test_add_with_direct_saves_a_round_trip(self):
        with mock.patch.object(C, "bili_get", side_effect=fake_bili_get):
            d = self.post("/api/bili/add", {"ref": "BV1xx411c7mZ", "direct": 1})
        self.assertTrue(d["ok"])
        self.assertTrue(d["direct"]["ok"])
        self.assertTrue(d["direct"]["urls"][0].startswith("https://"))
        self.assertEqual(d["direct"]["id"], "bili:BV1xx411c7mZ")
        with mock.patch.object(C, "bili_get", side_effect=fake_bili_get):
            d2 = self.post("/api/bili/add", {"ref": "BV1xx411c7mZ"})
        self.assertNotIn("direct", d2, "不带 direct=1 就不附")

    def test_json_gzip_when_asked(self):
        C._bili[:] = [rec(c, "2026-11-18T00:00:00") for c in "ABCDEFGHIJKLMNOP"]
        req = urllib.request.Request(self.base + "/api/films", headers={"Accept-Encoding": "gzip"})
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(r.headers.get("Content-Encoding"), "gzip")
            d = json.loads(gzip.decompress(r.read()))
        self.assertEqual(len([f for f in d["films"] if f.get("kind") == "bili"]), 16)
        with urllib.request.urlopen(self.base + "/api/films", timeout=10) as r:    # 不说要压就不压
            self.assertIsNone(r.headers.get("Content-Encoding"))
            json.loads(r.read())

    def test_cover_proxy_cacheable(self):
        os.makedirs(C.POSTER_DIR, exist_ok=True)
        with open(os.path.join(C.POSTER_DIR, "bili_BV1xx411c7mQ.jpg"), "wb") as f:
            f.write(b"\xff\xd8\xff" + b"0" * 64)
        with urllib.request.urlopen(self.base + "/api/bili/listcover?bvid=BV1xx411c7mQ&u=https%3A%2F%2Fi0.hdslb.com%2Fx.jpg",
                                    timeout=10) as r:
            self.assertIn("max-age=604800", r.headers.get("Cache-Control"))
        with urllib.request.urlopen(self.base + "/api/now", timeout=10) as r:
            self.assertEqual(r.headers.get("Cache-Control"), "no-store", "接口还是不缓存")


if __name__ == "__main__":
    unittest.main()
