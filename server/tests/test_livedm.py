"""直播公开弹幕的离线测试：归一化、同类合并计数、拆包、只要文字弹幕、限流。不连网。
跑法：cd server && python3 -m unittest discover -s tests -v"""
import json
import os
import struct
import sys
import tempfile
import time
import unittest
import zlib

TMP = tempfile.mkdtemp(prefix="bilitube-livedm-test-")
os.environ.update(BILITUBE_DATA_DIR=TMP, BILITUBE_FILM_DIR=TMP, BILITUBE_PUSH="0")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bilitube_server as C  # noqa: E402


class TestNorm(unittest.TestCase):
    def test_same_class(self):
        same = [("哈哈哈", "哈哈哈哈哈"), ("？？？", "???"), ("气死了", "气死了！！"), ("6666", "666"),
                ("笑死笑死", "笑死"), ("ＡＢＣ", "abc"), ("主播 加油", "主播加油")]
        for a, b in same:
            with self.subTest(a=a, b=b):
                self.assertEqual(C.ldm_norm(a), C.ldm_norm(b))

    def test_different_class(self):
        for a, b in [("哈哈哈", "嘿嘿嘿"), ("3号好人", "4号好人"), ("？？？", "！！！"), ("气死了", "笑死了")]:
            with self.subTest(a=a, b=b):
                self.assertNotEqual(C.ldm_norm(a), C.ldm_norm(b))


class TestSummary(unittest.TestCase):
    def test_shape_and_order(self):
        texts = ["气死了"] * 87 + ["毁灭吧"] * 40 + ["毁灭吧！"] * 2 + ["3号好人"] * 22 + ["散句一", "散句二"]
        line = C.ldm_summary(texts)
        self.assertEqual(line, "这会儿弹幕：气死了×87 · 毁灭吧×42 · 3号好人×22")

    def test_singletons_dropped_and_empty(self):
        self.assertEqual(C.ldm_summary(["一", "二", "三"]), "")
        self.assertEqual(C.ldm_summary([]), "")

    def test_most_common_form_shown(self):
        line = C.ldm_summary(["哈哈哈哈"] * 3 + ["哈哈哈"] * 5 + ["哈哈哈哈哈哈哈"])
        self.assertEqual(line, "这会儿弹幕：哈哈哈×9")

    def test_limits(self):
        texts = []
        for i in range(10):
            texts += [f"这是一句很长很长很长很长的弹幕第{i}号"] * (20 - i)
        line = C.ldm_summary(texts)
        self.assertLessEqual(len(line), C.LDM_SUM_CHARS)
        parts = line.split("：", 1)[1].split(" · ")
        self.assertLessEqual(len(parts), C.LDM_SUM_TOP)
        for p in parts:
            self.assertLessEqual(len(p.split("×")[0]), C.LDM_SUM_ITEM)
            self.assertIn("…", p)


def pkt(op, body, ver=0):
    if isinstance(body, str):
        body = body.encode()
    return struct.pack(">IHHII", 16 + len(body), 16, ver, op, 1) + body


def danmu(text, dm_type=0, mode=1, color=0xffffff, storm=0):
    meta = [0, mode, 25, color, 0, 0, 0, "", 0, storm, 0, "", dm_type]
    return {"cmd": "DANMU_MSG", "info": [meta, text, [1, "用***"]]}


class TestPackets(unittest.TestCase):
    def test_zlib_bundle(self):
        inner = pkt(5, json.dumps(danmu("你好"))) + pkt(5, json.dumps({"cmd": "INTERACT_WORD", "data": {}}))
        outer = pkt(5, zlib.compress(inner), ver=2) + pkt(3, struct.pack(">I", 99), ver=1)
        got = C.ldm_packets(outer)
        self.assertEqual([op for op, _, _ in got], [5, 5, 3])
        self.assertEqual(json.loads(got[0][2])["info"][1], "你好")

    def test_truncated_tail_ignored(self):
        p = pkt(5, json.dumps(danmu("你好")))
        self.assertEqual(C.ldm_packets(p + p[:10]), C.ldm_packets(p))

    def test_text_only(self):
        self.assertEqual(C.ldm_parse_danmu(danmu("好耶")), ("好耶", 1, 0xffffff))
        self.assertIsNone(C.ldm_parse_danmu(danmu("[dog]", dm_type=1)), "表情包弹幕不要")
        self.assertIsNone(C.ldm_parse_danmu(danmu("参与天选", storm=2)), "抽奖自动弹幕不要")
        for cmd in ("SEND_GIFT", "SUPER_CHAT_MESSAGE", "INTERACT_WORD", "GUARD_BUY"):
            self.assertIsNone(C.ldm_parse_danmu({"cmd": cmd, "info": [[0], "x"], "data": {}}))
        self.assertEqual(C.ldm_parse_danmu({"cmd": "DANMU_MSG:4:0:2:2:2:0", "info": [[0, 5, 25, 255], "顶上"]}),
                         ("顶上", 5, 255))


class TestRing(unittest.TestCase):
    def setUp(self):
        with C._ldm_lock:
            C._ldm_ring.clear()
            C._ldm_state.update(room=123, status="open")

    def test_poll_cap_and_room(self):
        for i in range(100):
            C._ldm_put(f"第{i}条", 1, 0xffffff)
        d = C.ldm_since(123, 0)
        self.assertEqual(len(d["list"]), C.LDM_POLL_MAX)
        self.assertEqual(d["list"][-1]["text"], "第99条", "多了留最新的")
        self.assertEqual(C.ldm_since(456, 0)["list"], [], "别的房间不给")
        self.assertEqual(C.ldm_since(123, d["last"])["list"], [])

    def test_summary_window(self):
        now = time.time()
        with C._ldm_lock:
            C._ldm_ring[:] = [(1, now - 60, "老的", 1, 0), (2, now - 60, "老的", 1, 0),
                              (3, now - 5, "新的", 1, 0), (4, now - 3, "新的！", 1, 0)]
        self.assertEqual(C.ldm_summary_line(123), "这会儿弹幕：新的×2")
        self.assertEqual(C.ldm_summary_line(999), "")


if __name__ == "__main__":
    unittest.main()
