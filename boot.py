# Copyright (c) 2026 Hyht-Team. All rights reserved.
# See LICENSE (proprietary) and NOTICE.md (third-party components).
"""AVD Flagship v2.0.0 —— 统一启动入口。

一个发布包、一个进程、一个版本：
  * 先把 vendor/yt_dlp（2026.10.26，原样内嵌）装进 sys.path 并校验版本；
  * 再导入 AVD 自研服务，启动本地 Web UI；
  * 全程不联网、不 pip install、不重建 venv。

关闭终端或 Ctrl+C 停止。
"""

from __future__ import annotations

import os
import sys
import threading
import webbrowser
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

# 必须在导入 server / downloader 之前完成 —— engine.py 顶层就会 import yt_dlp
from fusion import asr_offline, boot_check  # noqa: E402

BANNER = r"""
     _    ____  ____      _____ _      ___   ____ _  _ ___ _  _ ____
    / \  |  _ \|  _ \    |  ___| |    / _ \ / ___| \| / __| \| |  _ \
   / _ \ | | | | | | |   | |_  | |   | | | | |  _| .` | _|| .` | | | |
  / ___ \| |_| | |_| |   |  _| | |___| |_| | |_| | |\ | ||| |\ | |_| |
 /_/   \_\____/|____/    |_|   |_____\___/ \____|_| \_|_||_| \_|____/
                                        v2.0.0  ·  内嵌 yt-dlp 2026.10.26
"""


def main() -> int:
    os.chdir(APP_DIR)

    # 装配随包交付的离线语音权重：必须在任何 ASR 任务启动之前完成。
    # 只设环境变量，不改上游 worker 一行代码。
    model = asr_offline.configure()

    try:
        report = boot_check.activate()
    except RuntimeError as exc:
        print(f"\n  ✕ 引擎装配失败：{exc}\n")
        return 1

    from server import create_server  # noqa: E402

    host = os.environ.get("AVD_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("AVD_PORT", "8760"))
    except ValueError:
        port = 8760

    try:
        server = create_server(host=host, port=port)
    except RuntimeError as exc:
        print(f"\n  ✕ 启动失败：{exc}\n")
        return 1

    url = f"http://{server.server_address[0]}:{server.server_address[1]}"

    print(BANNER)
    print(f"  ▸ 服务地址      {url}")
    print(f"  ▸ 下载引擎      yt-dlp {report['engine_version']}  (vendor/ 内嵌，已锁定)")
    print(f"  ▸ 插件扩展点    {'已装配' if report['plugins'].get('loaded') else '未装配'}"
          f"  {report['plugins'].get('root', '')}")
    print(f"  ▸ ffmpeg        {report['ffmpeg'] or '未找到（高清合并将受限）'}")
    print(f"  ▸ 语音识别      {'可用' if report['asr'].get('available') else '未就绪'}"
          f"  {report['asr'].get('reason', '')}")
    print(f"  ▸ 离线权重      {model['selected']}"
          f"  ({'断网可用' if model['offline_capable'] else '需联网下载'})"
          f"  engine={model['engine']}")
    if not report["asr"].get("available"):
        print("                  修复：python3 doctor.py --check  然后 --repair")
    print("  ▸ 停止服务      按 Ctrl + C\n")

    if os.environ.get("AVD_NO_BROWSER") != "1":
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止。\n")
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
