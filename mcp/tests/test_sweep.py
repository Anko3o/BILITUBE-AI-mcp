"""
截图 / 封面自动清理（server/sweep.py）的离线测试：临时目录、改文件时间，不碰网络。

跑法：cd mcp && python3 -m unittest tests.test_sweep -v
"""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
import guard  # noqa: E402
import sweep  # noqa: E402

DAY = 86400


class TestSweep(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bilitube-sweep-test-")
        self.now = time.time()

    def put(self, name, age_days, data=b"x"):
        p = os.path.join(self.dir, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        t = self.now - age_days * DAY
        os.utime(p, (t, t))
        return p

    def left(self):
        out = []
        for dp, _, fs in os.walk(self.dir):
            out += [os.path.relpath(os.path.join(dp, n), self.dir) for n in fs]
        return sorted(out)

    def test_only_old_scene_and_cover(self):
        self.put("att-scene-bili_BV1xx411c7mD-31.jpg", 8)
        self.put("att-scene-live-1-1700000000-3.jpg", 30)
        self.put("att-scene-测试片-5.jpeg", 9)
        self.put("att-bili-cover-BV1xx411c7mD.jpg", 10)
        self.put("att-scene-bili_BV1xx411c7mD-40.jpg", 2)        # 还新：留着
        self.put("att-bili-cover-BV1xx411c7mE.jpg", 6.5)          # 还新：留着
        self.put("att-bili-qr-123.png", 40)                        # 二维码：不是这两类
        self.put("my-photo.jpg", 40)                               # 用户自己的图
        self.put("att-scene-notes.txt", 40)                        # 前缀对但不是图
        self.put("att-bili-cover-BV1xx411c7mD.jpg.part", 40)       # 半截下载：不是 .jpg
        self.put("small/att-scene-bili_BV1xx411c7mD-31.jpg", 40)   # 子目录不进
        n = sweep.sweep(self.dir, 7, now=self.now)
        self.assertEqual(n, 4)
        self.assertEqual(self.left(), sorted([
            "att-scene-bili_BV1xx411c7mD-40.jpg", "att-bili-cover-BV1xx411c7mE.jpg", "att-bili-qr-123.png",
            "my-photo.jpg", "att-scene-notes.txt", "att-bili-cover-BV1xx411c7mD.jpg.part",
            os.path.join("small", "att-scene-bili_BV1xx411c7mD-31.jpg")]))

    def test_zero_means_keep(self):
        self.put("att-scene-bili_BV1xx411c7mD-31.jpg", 400)
        self.assertEqual(sweep.sweep(self.dir, 0, now=self.now), 0)
        self.assertEqual(sweep.sweep(self.dir, -3, now=self.now), 0)
        self.assertEqual(len(self.left()), 1)

    def test_symlink_not_followed(self):
        outside = tempfile.mkdtemp(prefix="bilitube-sweep-outside-")
        target = os.path.join(outside, "keep.jpg")
        with open(target, "wb") as f:
            f.write(b"x")
        t = self.now - 40 * DAY
        os.utime(target, (t, t))
        os.symlink(target, os.path.join(self.dir, "att-scene-link-1.jpg"))
        sweep.sweep(self.dir, 7, now=self.now)
        self.assertTrue(os.path.exists(target))

    def test_missing_dir(self):
        self.assertEqual(sweep.sweep(os.path.join(self.dir, "nope"), 7), 0)

    def test_keep_days_config(self):
        self.assertEqual(guard.DEFAULTS["scene_keep_days"], 7)
        with mock.patch.dict(os.environ, {"BILI_SCENE_KEEP_DAYS": "3"}):
            self.assertEqual(sweep.keep_days(), 3)
        with mock.patch.dict(os.environ, {"BILI_SCENE_KEEP_DAYS": "0"}):
            self.assertEqual(sweep.keep_days(), 0)
        with mock.patch.dict(os.environ, {"BILI_SCENE_KEEP_DAYS": "abc"}):
            self.assertEqual(sweep.keep_days(), 7)
        with mock.patch.dict(os.environ, {"BILI_SCENE_KEEP_DAYS": "-1"}):
            self.assertEqual(sweep.keep_days(), 0)

    def test_start_sweeps_once_now(self):
        self.put("att-scene-bili_BV1xx411c7mD-31.jpg", 30)
        self.put("att-scene-bili_BV1xx411c7mD-40.jpg", 1)
        with mock.patch.dict(os.environ, {"BILI_SCENE_KEEP_DAYS": "7"}):
            sweep.start(self.dir, interval=3600)
            for _ in range(50):
                if len(self.left()) == 1:
                    break
                time.sleep(0.05)
        self.assertEqual(self.left(), ["att-scene-bili_BV1xx411c7mD-40.jpg"])


if __name__ == "__main__":
    unittest.main()
