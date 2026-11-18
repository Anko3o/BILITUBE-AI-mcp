"""
watching look 直播：回话照 bilitube 的 X-Live-Shot 说清这张是 TA 屏幕上的、TA 发弹幕那一刻的，还是服务器自己拉的。
不碰网络：api 打桩，图存临时目录。

跑法：cd mcp && python3 -m unittest tests.test_livelook -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import watch  # noqa: E402

JPG = b"\xff\xd8\xff" + b"\x00" * 3000


def fake_api(shot, why=None, last_dm=False):
    def api(path, params=None, payload=None, raw=False, timeout=90, method=None, hdrs=None):
        if path == "/now":
            lv = {"room": 77, "agoSec": 1}
            if last_dm:
                lv["lastDm"] = {"id": "abc", "agoSec": 30}
            return {"live": lv}
        if path == "/live/room":
            return {"room": 77, "title": "测试间", "uname": "主播"}
        if path == "/live/pubsum":
            return {}
        if path == "/frame":
            if hdrs is not None and shot:
                hdrs["x-live-shot"] = shot
                if why:
                    hdrs["x-live-why"] = why
            return JPG
        raise AssertionError(path)
    return api


class TestLiveLookText(unittest.TestCase):
    def run_look(self, *a, film="live:77", **kw):
        tmp = tempfile.mkdtemp(prefix="bili-livelook-")
        with mock.patch.object(watch, "api", fake_api(*a, **kw)), mock.patch.object(watch, "UPLOADS", tmp):
            return watch.act_look_live({}, film)

    def test_user_screen(self):
        out = self.run_look("page")
        self.assertIn("TA 屏幕上此刻的最近约三秒", out)
        self.assertIn("可以照着图说", out)
        self.assertNotIn("服务器", out)

    def test_server_with_reason(self):
        out = self.run_look("server", "timeout")
        self.assertIn("服务器自己从直播流拉的", out)
        self.assertIn("TA 的播放页 4 秒内没回", out)
        self.assertIn("差几秒到十几秒", out)

    def test_dm_shot_when_page_closed(self):
        out = self.run_look("dm", "closed", last_dm=True)
        self.assertIn("TA 发那条弹幕那一刻 TA 屏幕上存下的", out)
        self.assertIn("没在播放页开着这间直播", out)

    def test_old_bilitube_without_header(self):
        out = self.run_look(None)
        self.assertIn("服务器自己从直播流拉的", out)
        out = self.run_look(None, last_dm=True)
        self.assertIn("TA 发那条弹幕那一刻", out)

    def test_no_fei(self):
        for args in (("page",), ("server", "old"), ("dm", "hidden")):
            self.assertNotIn("飘", self.run_look(*args, last_dm=True))


if __name__ == "__main__":
    unittest.main()
