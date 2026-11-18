"""
截图和封面自动清理。

MCP 进程会往 BILITUBE_IMAGE_DIR 里写两类图：
  att-scene-*.jpg       watching look 截的画面（watch.py）
  att-bili-cover-*.jpg  video info 顺手缓存的封面（server/bili.py 的 _download_cover）
不清的话它们只增不减。这里在 MCP 服务启动时清一次、之后每 24 小时清一次，
只删这两类里修改时间早于 scene_keep_days 天的文件；目录里别的文件（二维码、你自己放进去的图）一概不碰，
也不进子目录、不跟符号链接。

配置：mcp/config.json 的 scene_keep_days（环境变量 BILI_SCENE_KEEP_DAYS 优先），默认 7；0 = 不清。
每次清之前现读配置，改了不用重启，下一轮就按新的天数算。想长期留的图，自己另存到别的目录。
"""
import os
import re
import sys
import threading
import time

import guard

PATTERNS = (
    re.compile(r"^att-scene-.+\.jpe?g$"),
    re.compile(r"^att-bili-cover-[\w-]+\.jpe?g$"),
)
INTERVAL_S = 24 * 3600


def keep_days():
    """config 里的 scene_keep_days；写错（不是数字）按默认 7 天，负数当 0（不清）。"""
    v = guard.config().get("scene_keep_days")
    try:
        d = float(v)
    except (TypeError, ValueError):
        return float(guard.DEFAULTS["scene_keep_days"])
    return max(0.0, d)


def ours(name):
    return any(p.match(name) for p in PATTERNS)


def sweep(dirpath, days, now=None):
    """删 dirpath 里过期的截图 / 封面，返回删了几张。days <= 0 什么都不做。"""
    if not days or days <= 0:
        return 0
    cutoff = (time.time() if now is None else now) - days * 86400
    n = 0
    try:
        entries = list(os.scandir(dirpath))
    except FileNotFoundError:
        return 0
    for e in entries:
        try:
            if not ours(e.name) or not e.is_file(follow_symlinks=False):
                continue
            if e.stat(follow_symlinks=False).st_mtime < cutoff:
                os.remove(e.path)
                n += 1
        except OSError:
            continue
    return n


def start(dirpath, interval=INTERVAL_S):
    """后台线程：现在清一次，之后每 interval 秒清一次。返回线程。"""
    def loop():
        while True:
            try:
                d = keep_days()
                n = sweep(dirpath, d)
                if n:
                    print(f"[sweep] 清掉 {n} 张超过 {d:g} 天的截图 / 封面", file=sys.stderr, flush=True)
            except Exception as e:  # 清理出错不影响服务
                print(f"[sweep] 清理出错：{type(e).__name__}", file=sys.stderr, flush=True)
            time.sleep(interval)
    t = threading.Thread(target=loop, name="image-sweep", daemon=True)
    t.start()
    return t
