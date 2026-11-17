"""本地素材工作台 · 启动入口（兼容薄壳）。

正式入口是 ``boot.py``（装配 vendor yt-dlp、离线权重、版本闸门）。
本文件保留为薄壳，避免旧脚本 / 文档里的 ``python3 app.py`` 踩到未装配的系统版 yt-dlp（ENTRY-01）。
"""

from __future__ import annotations

from boot import main

if __name__ == "__main__":
    raise SystemExit(main())
