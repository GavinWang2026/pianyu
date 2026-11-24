# Copyright (c) 2026 Hyht-Team
# SPDX-License-Identifier: MIT
# See LICENSE (MIT) and NOTICE.md (third-party components).
"""自动更新检查脚手架 —— 只探测，不自动下载安装。

默认读环境变量 ``AVD_UPDATE_URL``（HTTPS JSON）；未配置则返回「未启用」。
JSON 形态::

    {"version": "2.1.0", "url": "https://…", "notes": "…", "min_os": "12.0"}
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from . import PRODUCT_VERSION


def _parse_ver(raw: str) -> tuple[int, ...]:
    parts: list[int] = []
    for bit in str(raw or "").strip().lstrip("vV").split("."):
        num = ""
        for ch in bit:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num or 0))
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:4])


def newer_than(candidate: str, current: str = PRODUCT_VERSION) -> bool:
    return _parse_ver(candidate) > _parse_ver(current)


def check(*, current: str | None = None, url: str | None = None,
          timeout: float = 8.0) -> dict[str, Any]:
    """返回更新探测结果；永不抛到调用方以外的网络细节。"""
    cur = (current or PRODUCT_VERSION).strip()
    feed = (url if url is not None else os.environ.get("AVD_UPDATE_URL") or "").strip()
    if not feed:
        return {
            "enabled": False,
            "current": cur,
            "update_available": False,
            "message": "未配置 AVD_UPDATE_URL，自动更新检查未启用",
        }
    parsed = urlparse(feed)
    if parsed.scheme != "https" or not parsed.hostname:
        return {
            "enabled": False,
            "current": cur,
            "update_available": False,
            "message": "更新源必须是 https URL",
        }
    req = urllib.request.Request(
        feed,
        headers={"User-Agent": f"AVD-Flagship/{cur}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read(64 * 1024).decode("utf-8", "replace")
        data = json.loads(raw)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        return {
            "enabled": True,
            "current": cur,
            "update_available": False,
            "error": f"检查失败：{exc}",
        }
    except json.JSONDecodeError:
        return {
            "enabled": True,
            "current": cur,
            "update_available": False,
            "error": "更新源返回非 JSON",
        }
    if not isinstance(data, dict):
        return {
            "enabled": True,
            "current": cur,
            "update_available": False,
            "error": "更新源格式错误",
        }
    latest = str(data.get("version") or data.get("latest") or "").strip()
    download = str(data.get("url") or data.get("download_url") or "").strip()
    notes = str(data.get("notes") or data.get("changelog") or "").strip()[:2000]
    available = bool(latest and newer_than(latest, cur))
    return {
        "enabled": True,
        "current": cur,
        "latest": latest,
        "update_available": available,
        "url": download if available else "",
        "notes": notes if available else "",
        "message": (
            f"有新版本 {latest}" if available
            else ("已是最新" if latest else "更新源未提供 version")
        ),
    }
