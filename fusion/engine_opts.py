# Copyright (c) 2026 Hyht-Team. All rights reserved.
# See LICENSE (proprietary) and NOTICE.md (third-party components).
"""融合层与 yt-dlp 之间的统一参数契约。

实测背景（2026-11-12，抖音真实短链）：融合层原先自己拼 6 个固定参数，
完全没有继承原版 ``downloader/engine.py::_base_opts`` 里的
``cookies_from_browser`` / ``proxy`` / ``retries`` 等配置，导致：

    [Douyin] Failed to download web detail JSON: HTTP Error 403
    ERROR: Fresh cookies (not necessarily logged in) are needed

即「平台站点链路」在旗舰版里连配置入口都没接上。本模块让 probe 与
download 共用同一套 opts，且额外支持 Netscape 格式 ``cookies.txt``。

安全约定：cookie 文件必须位于发布包内（防止把本机任意文件当 Cookie
发给远端 = 数据外带），由 ``guards.within()`` 强制限定。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import guards

APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_COOKIE_FILE = APP_DIR / "cookies" / "cookies.txt"


def cookie_file() -> Path | None:
    """返回可用的包内 cookie 文件，否则 None。"""
    raw = (os.environ.get("AVD_COOKIE_FILE") or "").strip()
    candidate = Path(raw).expanduser() if raw else DEFAULT_COOKIE_FILE
    safe = guards.within(APP_DIR, candidate)
    if safe is None:
        return None
    return safe if safe.is_file() else None


def _ensure_engine() -> None:
    """保证内嵌 yt-dlp 已挂到 sys.path（幂等）。

    单测 / 独立调用本模块时，``downloader`` 包会 import yt_dlp；
    服务进程里 boot.py 已激活，这里重复调用无副作用。
    """
    from . import boot_check  # noqa: WPS433
    boot_check.activate()


def base_opts(url: str) -> dict[str, Any]:
    """构造一次解析 / 下载所需的完整 yt-dlp 参数。

    复用原版 ``_base_opts``（cookie 浏览器、代理、重试、日志），再叠加：
    包内 cookiefile、官方 extractor 裁剪开关、不校验证书保持 False。
    """
    _ensure_engine()
    from downloader import config, engine  # noqa: WPS433 — 延迟导入避免循环

    opts: dict[str, Any] = engine._base_opts(config.as_dict(), url)
    opts["skip_download"] = True
    opts["nocheckcertificate"] = False
    path = cookie_file()
    if path is not None:
        opts["cookiefile"] = str(path)
    allowed = guards.allowed_extractors()
    if allowed:
        opts["allowed_extractors"] = allowed
    return opts


def cookie_status() -> dict[str, Any]:
    """只读状态，供 health / doctor 上报（不回显任何 cookie 内容）。"""
    path = cookie_file()
    _ensure_engine()
    from downloader import config  # noqa: WPS433
    browser = str(config.as_dict().get("cookies_from_browser") or "").strip()
    return {
        "cookie_file": str(path) if path else "",
        "cookie_file_present": path is not None,
        "cookies_from_browser": browser,
        "authentication_ready": bool(path or browser),
    }
