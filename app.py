"""本地素材工作台 · 启动入口。

拉起本地服务并自动打开浏览器。关闭终端窗口或按 Ctrl+C 即可停止。
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

from server import create_server  # noqa: E402

BANNER = r"""
   ___   _  __        __   ____                      _                 _
  / _ \ (_)/ /__  __ / /  / __ \____ __      __ ___ | | __ __ _  ____ | |
 / // // // // _ \/ // /  / / / // __ `/| | / // _ \| |/ // _` |/ __ \| |
/____//_//_/ \___/_//_/  /_/ /_/\__,_/ |_|/|_//_//_/\___/ \__,_/\____/|_|
                                                                          v1.0
"""


def main() -> int:
    os.chdir(APP_DIR)

    try:
        server = create_server()
    except RuntimeError as exc:
        print(f"\n  ✕ 启动失败：{exc}\n")
        return 1

    host, port = server.server_address[0], server.server_address[1]
    url = f"http://{host}:{port}"

    print(BANNER)
    print(f"  ▸ 服务地址   {url}")
    print(f"  ▸ 下载目录   {os.environ.get('VD_DOWNLOAD_DIR') or '(见界面「设置」)'}")
    print("  ▸ 停止服务   按 Ctrl + C\n")

    if os.environ.get("VD_NO_BROWSER") != "1":
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
