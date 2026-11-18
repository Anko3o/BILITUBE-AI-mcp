"""
回话带图（inline_shot / inline_image）的离线测试。不碰网络、不碰真钥匙：
图用 ffmpeg 现场生成在临时目录，bili_mcp 导入前把钥匙路径指到临时文件。

跑法：cd mcp && python3 -m unittest tests.test_inline -v
"""
import base64
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="bili-inline-test-")
import bili  # noqa: E402

if not str(bili.CRED_PATH).startswith(tempfile.gettempdir()):
    bili.CRED_PATH = os.path.join(TMP, "cred.json")
import bili_mcp  # noqa: E402


def make_img(name, w, h, noise=False):
    """ffmpeg 画一张 w×h 的 JPEG；noise=True 是满屏噪点（压不小的那种）。"""
    path = os.path.join(TMP, name)
    src = f"nullsrc=s={w}x{h},geq=random(1)*255:128:128" if noise else f"testsrc=s={w}x{h}"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", src, "-frames:v", "1",
                    "-q:v", "1" if noise else "4", path], check=True)
    return path


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "没有 ffmpeg")
class InlineTest(unittest.TestCase):
    def test_small_cover_goes_raw(self):
        p = make_img("att-bili-cover-BVsmall.jpg", 640, 400)
        img = bili_mcp.inline_shot(f"某个视频\n封面：{p}")
        self.assertEqual(img["type"], "image")
        self.assertEqual(img["mimeType"], "image/jpeg")
        with open(p, "rb") as f:
            self.assertEqual(base64.b64decode(img["data"]), f.read())      # 原样，一字节不改

    def test_big_cover_is_shrunk_and_original_untouched(self):
        p = make_img("att-bili-cover-BVbig.jpg", 3000, 1800)
        before = os.path.getsize(p)
        img = bili_mcp.inline_image(p)
        data = base64.b64decode(img["data"])
        self.assertLessEqual(len(data), bili_mcp.INLINE_TARGET)
        out = os.path.join(TMP, "shrunk.jpg")
        with open(out, "wb") as f:
            f.write(data)
        w, h = bili_mcp._img_size(out)
        self.assertEqual(max(w, h), bili_mcp.INLINE_EDGE)
        self.assertEqual(os.path.getsize(p), before)

    def test_no_inline_when_cannot_shrink_or_missing(self):
        old = bili_mcp.INLINE_TARGET
        try:
            bili_mcp.INLINE_TARGET = 1000                     # 噪点图怎么压都过不了 1KB → 不带
            p = make_img("att-bili-cover-BVnoise.jpg", 1600, 1000, noise=True)
            self.assertIsNone(bili_mcp.inline_image(p))
        finally:
            bili_mcp.INLINE_TARGET = old
        self.assertIsNone(bili_mcp.inline_shot("封面：/nonexistent/att-bili-cover-BVx.jpg"))
        self.assertIsNone(bili_mcp.inline_shot("二维码在 /tmp/x/att-bili-qr-1.png"))


if __name__ == "__main__":
    unittest.main()
