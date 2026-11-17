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


MAIN_MIN_PYTHON = (3, 11)   # yt-dlp 2026.10.26 起最低 3.11
ASR_PYTHON = (3, 13)        # 随包 cp313 原生扩展需要独立 3.13 运行时


def _version_gate() -> str:
    """版本分层检查：主服务 3.11+，ASR 独立运行时 3.13。

    两者刻意分开：主服务是纯 Python + 标准库，不必强求 3.13；
    ASR 依赖编译过的 cp313 扩展，由 .venv-asr 单独提供。
    """
    if sys.version_info[:2] < MAIN_MIN_PYTHON:
        return (f"当前 Python {sys.version.split()[0]} 过低：主服务需要 "
                f"{MAIN_MIN_PYTHON[0]}.{MAIN_MIN_PYTHON[1]}+"
                "（上游 yt-dlp 2026.10.26 已放弃 3.10）")
    return ""


def main() -> int:
    os.chdir(APP_DIR)

    gate = _version_gate()
    if gate:
        print(f"\n  ✕ 无法启动：{gate}")
        print("    安装 Python 3.11+ 后重新运行；本地语音识别另需 3.13 运行时。\n")
        return 1

    # 装配随包交付的离线语音权重：必须在任何 ASR 任务启动之前完成。
    # 只设环境变量，不改上游 worker 一行代码。
    model = asr_offline.configure()

    try:
        report = boot_check.activate()
    except RuntimeError as exc:
        print(f"\n  ✕ 引擎装配失败：{exc}\n")
        return 1

    from server import create_server  # noqa: E402

    # NET-01：默认真本机；AVD_HOST=0.0.0.0 会与守卫「仅本机 Host」冲突导致 LAN 全 403。
    # 若确需局域网访问，必须同时设置 AVD_TOKEN，否则强制回落到 127.0.0.1。
    host = (os.environ.get("AVD_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    token = (os.environ.get("AVD_TOKEN") or "").strip()
    lan_warning = ""
    if host not in ("127.0.0.1", "localhost", "::1"):
        if not token:
            lan_warning = (
                f"AVD_HOST={host} 但未设置 AVD_TOKEN：已强制回落到 127.0.0.1"
                "（守卫只放行本机 Host；局域网模式请同时 export AVD_TOKEN=…）"
            )
            host = "127.0.0.1"
        else:
            lan_warning = (
                f"局域网模式：监听 {host}，请求须带 X-AVD-Token；"
                "请确保防火墙与网段策略已收紧"
            )
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
    if lan_warning:
        print(f"  ▸ 网络提示      {lan_warning}")
    print(f"  ▸ 运行环境      Python {sys.version.split()[0]} · {sys.platform}"
          f"  (主服务 ≥3.11 / ASR 独立运行时 3.13)")
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
        fix = ("运行 setup-windows-runtime.bat（安装 Win 版依赖）"
               if os.name == "nt" else
               "python3 doctor.py --check  然后 --repair")
        print(f"                  修复：{fix}")
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
