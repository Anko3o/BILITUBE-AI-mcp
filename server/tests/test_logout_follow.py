"""后端这头的退出登录和「AI 的关注」：/api/bili/logout 叫 MCP 的回环 POST /logout，成了就把按账号缓存的东西丢掉；
MCP 那头连不上就回一句中文原因。/api/bili/follow_group 原样转 MCP 的只读列表。
MCP 用本机一个假的小服务顶替，不连外网、不碰真数据。跑法：cd server && python3 -m unittest discover -s tests -v"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

TMP = tempfile.mkdtemp(prefix="bilitube-logout-test-")
os.environ.setdefault("BILITUBE_DATA_DIR", TMP)
os.environ.setdefault("BILITUBE_FILM_DIR", TMP)
os.environ.update(BILITUBE_PUSH="0", BILITUBE_LIVE_DM="0")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bilitube_server as C  # noqa: E402


class FakeMcp(BaseHTTPRequestHandler):
    seen = []
    logout_status = 200
    group = {"group": {"tagid": 7, "name": "AI 的关注", "count": 1}, "want": {"id": None, "name": "AI 的关注"},
             "list": [{"mid": 9, "uname": "UP 九", "face": "https://i0.hdslb.com/9.jpg"}]}

    def _send(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        FakeMcp.seen.append(("GET", self.path))
        if self.path == "/me":
            return self._send(200, {"logged_in": True, "uname": "测试", "mid": 42, "face": ""})
        if self.path.startswith("/follow_group"):
            return self._send(200, FakeMcp.group)
        self._send(404, {"error": "not found"})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        FakeMcp.seen.append(("POST", self.path))
        if self.path == "/logout":
            if FakeMcp.logout_status != 200:
                return self._send(FakeMcp.logout_status, {"error": "logout 出错: 测试里故意的"})
            return self._send(200, {"ok": True, "local_cleared": True, "remote": "unconfirmed", "note": "没连上 B 站（Timeout）"})
        self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass


class TestLogoutFollow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mcp = ThreadingHTTPServer(("127.0.0.1", 0), FakeMcp)
        threading.Thread(target=cls.mcp.serve_forever, daemon=True).start()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), C.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.patch = mock.patch.object(C, "BILI_MCP", f"http://127.0.0.1:{cls.mcp.server_address[1]}")
        cls.patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.patch.stop()
        cls.srv.shutdown()
        cls.mcp.shutdown()
        cls.srv.server_close()
        cls.mcp.server_close()

    def setUp(self):
        FakeMcp.seen.clear()
        FakeMcp.logout_status = 200

    def call(self, path, body=None):
        req = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                                     method="GET" if body is None else "POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_logout_calls_mcp_and_drops_account_caches(self):
        C._state_cache["BV1xx411c7mD"] = (9e9, {"liked": True})
        C._season_cache["ss1"] = (9e9, {"progress": {"ep_id": 1, "t": 30}})
        C._pgc_accept["bili:ep1"] = [{"qn": 116, "vip_only": True}]
        C._login_cache.update(t=9e9, d={"ok": True, "logged_in": True, "uname": "测试"})
        C._playurl["x"] = {"url": "https://example.invalid/v.mp4", "expires_at": 9e9}
        os.makedirs(C.POSTER_DIR, exist_ok=True)
        face = os.path.join(C.POSTER_DIR, "bili_up_42.jpg")
        with open(face, "wb") as f:
            f.write(b"\xff\xd8\xff")
        code, d = self.call("/api/bili/logout", {})
        self.assertEqual(code, 200)
        self.assertEqual((d["ok"], d["remote"]), (True, "unconfirmed"))
        self.assertIn("没连上", d["note"])
        self.assertIn(("POST", "/logout"), FakeMcp.seen)
        self.assertEqual((C._state_cache, C._season_cache, C._pgc_accept), ({}, {}, {}))
        self.assertIsNone(C._login_cache["d"])
        self.assertFalse(os.path.exists(face), "上一个号的头像图不留")
        self.assertIn("x", C._playurl, "视频地址缓存不动，正在播的不打断")
        C._playurl.pop("x", None)

    def test_logout_failure_says_why_in_chinese(self):
        FakeMcp.logout_status = 502
        C._state_cache["BV1xx411c7mD"] = (9e9, {"liked": True})
        code, d = self.call("/api/bili/logout", {})
        self.assertEqual(code, 200)
        self.assertFalse(d["ok"])
        self.assertTrue(d["error"].startswith("没退成"))
        self.assertIn("BV1xx411c7mD", C._state_cache, "MCP 没退成，这边也不假装退了")
        C._state_cache.clear()

    def test_logout_is_post_only(self):
        code, _ = self.call("/api/bili/logout")
        self.assertNotEqual(code, 200)
        self.assertNotIn(("POST", "/logout"), FakeMcp.seen)

    def test_follow_group_passthrough(self):
        code, d = self.call("/api/bili/follow_group?page=2&n=999")
        self.assertEqual(code, 200)
        self.assertEqual(d["group"]["name"], "AI 的关注")
        self.assertEqual(d["list"][0]["mid"], 9)
        self.assertIn(("GET", "/follow_group?n=50&page=2"), FakeMcp.seen)
