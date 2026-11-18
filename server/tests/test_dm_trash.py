"""「聊过」页横滑删一部片：POST /api/dm/log/trash {film} 把这部片下面两个人的私人弹幕行原样挪进 trash/，
别的片、别的流水、批注都不动；直播那几条存的截帧顺手删掉，别的不碰；dm_log_untrash 能原样接回来。不连网。
跑法：cd server && python3 -m unittest discover -s tests -v"""

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

TMP = tempfile.mkdtemp(prefix="bilitube-dmtrash-test-")
os.environ.update(BILITUBE_DATA_DIR=TMP, BILITUBE_FILM_DIR=TMP, BILITUBE_PUSH="0", BILITUBE_LIVE_DM="0")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bilitube_server as C  # noqa: E402

A = "bili:BV1xx411c7mD"
B = "bili:BV1xx411c7mD_p2"
LIVE = "live:1"


def rows():
    return [
        {"at": "2026-01-02T01:00:00", "kind": "open", "film": A},
        {"at": "2026-01-02T01:00:05", "kind": "danmaku", "film": A, "t": 3.0, "text": "第一句", "author": "user"},
        {"at": "2026-01-02T01:00:09", "kind": "danmaku", "film": A, "t": 7.5, "text": "回一句", "author": "companion", "id": "f1"},
        {"at": "2026-01-02T01:01:00", "kind": "danmaku", "film": B, "t": 1.0, "text": "别的片", "author": "user"},
        {"at": "2026-01-02T02:00:00", "kind": "danmaku", "film": LIVE, "text": "直播里", "author": "user", "id": "a1",
         "via": "live", "roomTitle": "测试房间", "uname": "测试主播"},
        {"at": "2026-01-02T02:00:30", "kind": "danmaku", "film": LIVE, "text": "回直播", "author": "companion", "id": "f2"},
    ]


class TestDmTrash(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = C.ThreadingHTTPServer(("127.0.0.1", 0), C.Handler)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        with open(C.EVENTS_LOG, "w", encoding="utf-8") as f:
            f.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows()))
            f.write("这一行坏了\n")
        if os.path.isdir(C.TRASH_DIR):
            for n in os.listdir(C.TRASH_DIR):
                os.remove(os.path.join(C.TRASH_DIR, n))
        os.makedirs(C.FRAME_DIR, exist_ok=True)
        for lid in ("a1", "f2", "zz"):
            with open(C.live_dm_shot_path(1, lid), "wb") as f:
                f.write(b"\xff\xd8\xff" + b"0" * 2000)
        with C._lock:
            del C._dmsay[:]
            C._dmsay.append({"id": 1, "film": A, "text": "回一句", "log": True})
            C._dmsay.append({"id": 2, "film": B, "text": "别的片", "log": True})
            C._last_user_dm.clear()
            C._last_user_dm[A] = {"t": 3.0, "text": "第一句", "at": "x"}

    def post(self, body):
        req = urllib.request.Request(self.base + "/api/dm/log/trash", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return json.loads(r.read())

    def lines(self):
        with open(C.EVENTS_LOG, encoding="utf-8") as f:
            return f.readlines()

    def test_trash_one_film(self):
        before = self.lines()
        st, d = self.post({"film": A})
        self.assertEqual(st, 200, d)
        self.assertEqual(d["removed"], 2)
        # 流水：A 的两条弹幕没了；A 的开映、B、直播、坏行都在，顺序不变
        after = self.lines()
        self.assertEqual(len(after), len(before) - 2)
        self.assertEqual([l for l in before if not (f'"film": "{A}"' in l and '"danmaku"' in l)], after)
        # 回收文件：原样两行＋删除时间
        with open(os.path.join(C.TRASH_DIR, d["trash"]), encoding="utf-8") as f:
            rec = json.load(f)
        self.assertEqual(rec["film"], A)
        self.assertEqual(rec["count"], 2)
        self.assertTrue(rec["deleted_at"])
        self.assertEqual(rec["lines"], [l for l in before if f'"film": "{A}"' in l and '"danmaku"' in l])
        # 页面、播放页、工具读的都是这两个口子：都不再有 A
        films = [x["film"] for x in self.get("/api/dm/log")["films"]]
        self.assertNotIn(A, films)
        self.assertIn(B, films)
        self.assertEqual(self.get("/api/dm/log?film=" + A)["items"], [])
        self.assertIsNone(C.last_user_dm(A))
        self.assertEqual([p["film"] for p in C._dmsay], [B])
        # 再删一次：没有了
        st, d = self.post({"film": A})
        self.assertEqual(st, 404)

    def test_live_and_shots(self):
        st, d = self.post({"film": LIVE})
        self.assertEqual(st, 200, d)
        self.assertEqual(d["removed"], 2)
        self.assertEqual(d["shots"], 2)
        self.assertTrue(d["title"].startswith("直播"))   # 名字是删之前认的
        self.assertFalse(os.path.exists(C.live_dm_shot_path(1, "a1")))
        self.assertFalse(os.path.exists(C.live_dm_shot_path(1, "f2")))
        self.assertTrue(os.path.exists(C.live_dm_shot_path(1, "zz")))   # 不是这几条的不碰
        self.assertEqual(self.get("/api/dm/log?film=" + LIVE)["items"], [])

    def test_bad_input(self):
        self.assertEqual(self.post({})[0], 400)
        self.assertEqual(self.post({"film": "bili:BV1nothere00"})[0], 404)
        self.assertEqual(len(self.lines()), len(rows()) + 1)   # 什么都没动

    def test_untrash(self):
        st, d = self.post({"film": A})
        self.assertEqual(st, 200)
        n = C.dm_log_untrash(os.path.join(C.TRASH_DIR, d["trash"]))
        self.assertEqual(n, 2)
        items = self.get("/api/dm/log?film=" + A)["items"]
        self.assertEqual([i["text"] for i in items], ["第一句", "回一句"])

    def test_journal_while_trashing(self):
        # 删的同时还在记：新记的那行不能被整份重写盖掉
        stop = threading.Event()

        def writer():
            k = 0
            while not stop.is_set() and k < 300:
                C.journal("danmaku", B, t=k, text=f"并发{k}", author="user")
                k += 1
            return k
        t = threading.Thread(target=writer)
        t.start()
        st, _ = self.post({"film": A})
        t.join()
        stop.set()
        self.assertEqual(st, 200)
        texts = [json.loads(l).get("text") for l in self.lines() if l.startswith("{")]
        self.assertEqual(sum(1 for x in texts if str(x).startswith("并发")), 300)

    def test_not_in_tools(self):
        # 删除只给页面：MCP 那头（mcp/）的源码里不该出现这个口子
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "mcp")
        for dp, _, fs in os.walk(root):
            if "__pycache__" in dp or os.sep + "tests" in dp:
                continue
            for n in fs:
                if n.endswith(".py"):
                    with open(os.path.join(dp, n), encoding="utf-8", errors="ignore") as f:
                        self.assertNotIn("dm/log/trash", f.read(), n)

if __name__ == "__main__":
    unittest.main()
