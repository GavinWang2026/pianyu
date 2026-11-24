# Copyright (c) 2026 Hyht-Team
# SPDX-License-Identifier: MIT
# See LICENSE (MIT) and NOTICE.md (third-party components).
"""引擎装配与版本闸门。

旗舰版把 yt-dlp 2026.10.26 的源码包**原样**内嵌在 ``vendor/yt_dlp/``
（Unlicense，未 fork、未裁剪、未改动一行）。本模块负责：

1. 把 ``vendor/`` 顶到 ``sys.path`` 最前，确保用的是包内那一份而不是系统里装的版本；
2. 校验引擎版本与文件来源，从根上消掉「启动脚本每次重建 venv 装 latest」的依赖漂移；
3. 把产品根目录放进 ``sys.path``，让 ``yt_dlp_plugins`` 命名空间包能被官方插件机制发现；
4. 对外暴露一份可自检的健康报告（引擎、插件、ffmpeg、ASR）。

这一层就是「两个程序合成一个」的物理接缝：对外是一个发布包、一个进程、一个版本。
"""

from __future__ import annotations

import importlib
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from . import ENGINE_VERSION, PRODUCT_NAME, PRODUCT_VERSION

APP_DIR = Path(__file__).resolve().parent.parent
VENDOR_DIR = APP_DIR / "vendor"
PLUGIN_ROOT = APP_DIR
PINNED_VERSION = ENGINE_VERSION
PINNED_RELEASE_GIT_HEAD = "c91f8de0bfb3577894abad716eaaa01659e6bd14"

_activated = False


def _drop_foreign_yt_dlp(vendor_prefix: str) -> None:
    """若系统 site-packages 的 yt_dlp 已被导入，卸掉，强制重新从 vendor 加载。"""
    for name in [n for n in sys.modules if n == "yt_dlp" or n.startswith("yt_dlp.")]:
        mod = sys.modules.get(name)
        origin = getattr(mod, "__file__", "") or ""
        if origin and not str(Path(origin).resolve()).startswith(vendor_prefix):
            del sys.modules[name]


def activate(force: bool = False) -> dict[str, Any]:
    """装配内嵌引擎并校验；失败直接抛 RuntimeError，绝不静默退回系统版。"""
    global _activated
    if _activated and not force:
        return health()

    engine_pkg = VENDOR_DIR / "yt_dlp" / "__init__.py"
    if not engine_pkg.is_file():
        raise RuntimeError(f"内嵌引擎缺失：{engine_pkg}")

    vendor = str(VENDOR_DIR.resolve())
    root = str(PLUGIN_ROOT.resolve())
    for path in (vendor, root):
        if path in sys.path:
            sys.path.remove(path)
    # vendor 必须比产品根目录更靠前，避免任何同名包抢先命中
    sys.path.insert(0, root)
    sys.path.insert(0, vendor)

    _drop_foreign_yt_dlp(vendor)
    yt_dlp = importlib.import_module("yt_dlp")

    origin = Path(yt_dlp.__file__).resolve()
    if not str(origin).startswith(vendor):
        raise RuntimeError(f"引擎来源不符（疑似用了系统安装版）：{origin}")

    version = yt_dlp.version.__version__
    if version != PINNED_VERSION:
        raise RuntimeError(
            f"引擎版本漂移：发布包锁定 {PINNED_VERSION}，实得 {version}。"
            "请勿替换 vendor/yt_dlp；升级须走显式发布流程。"
        )

    _activated = True
    return health()


def plugin_report() -> dict[str, Any]:
    """官方插件扩展点的装配情况（不改动上游源码即可定制）。"""
    root = PLUGIN_ROOT / "yt_dlp_plugins"
    dirs = {
        "extractor": root / "extractor",
        "postprocessor": root / "postprocessor",
    }
    files: dict[str, list[str]] = {}
    for kind, path in dirs.items():
        files[kind] = sorted(
            p.name for p in path.glob("*.py") if p.is_file() and p.name != "__init__.py"
        ) if path.is_dir() else []
    # 只验证命名空间包能被 Python 按 yt-dlp 官方约定发现并导入；
    # 不主动调用上游内部 API，避免上游改版导致我们这边误判。
    try:
        importlib.import_module("yt_dlp_plugins.postprocessor.avd_provenance")
    except Exception as exc:  # 插件加载失败不阻断主服务，只如实上报
        return {"root": str(root), "files": files, "loaded": False, "reason": str(exc)}
    return {
        "root": str(root), "files": files, "loaded": True,
        "names": ["yt_dlp_plugins.postprocessor.avd_provenance"],
        "note": "按官方命名空间包约定发现，上游 vendor/ 源码零改动",
    }


def health() -> dict[str, Any]:
    """给 /api/fusion/health 用的自检报告。任何一项失败都如实写明，不粉饰。"""
    report: dict[str, Any] = {
        "product": PRODUCT_NAME,
        "product_version": PRODUCT_VERSION,
        "engine": "yt-dlp",
        "engine_version": PINNED_VERSION,
        "engine_pinned": True,
        "engine_source": "vendor/yt_dlp (unmodified, Unlicense)",
        "engine_activated": _activated,
        "python": sys.version.split()[0],
        "ffmpeg": shutil.which("ffmpeg") or "",
        "asr": {"available": False, "reason": "未探测"},
    }

    report["plugins"] = plugin_report() if _activated else {"loaded": False, "reason": "引擎未装配"}

    try:
        from downloader import asr  # noqa: WPS433 — 延迟导入，避免装配前触发副作用
        info = asr.detect_engines()
        report["asr"] = {
            "available": bool(asr.is_available()),
            "engines": info.get("engines", {}),
            "python": info.get("python", ""),
            "reason": "" if info.get("available") else str(info.get("reason") or "解释器或依赖缺失"),
        }
    except Exception as exc:
        report["asr"] = {"available": False, "reason": f"探测失败：{exc}"}

    try:
        from . import asr_offline  # noqa: WPS433
        report["asr"]["model"] = asr_offline.status()
    except Exception as exc:  # 模型状态上报失败不影响主服务
        report["asr"]["model"] = {"offline_capable": False, "reason": str(exc)}

    report["bind"] = f"{os.environ.get('AVD_HOST', '127.0.0.1')}:{os.environ.get('AVD_PORT', '8760')}"
    return report
