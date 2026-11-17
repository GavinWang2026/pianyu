# Copyright (c) 2026 Hyht-Team. All rights reserved.
# See LICENSE (proprietary) and NOTICE.md (third-party components).
"""缩略图缓存 —— 从 server.py 旁路收进融合层，与 hardening 限额共用。

同一 URL 并发请求只发一次网络（in-flight 去重），其余等待首个结果。
"""

from __future__ import annotations

import threading
import time
import urllib.request
from typing import Any
from urllib.parse import urlparse

from . import guards, hardening

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
_MAX_BODY = 6 * 1024 * 1024

_cache: dict[str, tuple[float, str, bytes]] = {}
_lock = threading.RLock()
# url -> (Event, result_box)；result_box 在完成后写入 (ok, value_or_exc)
_inflight: dict[str, tuple[threading.Event, dict[str, Any]]] = {}


def stats() -> dict[str, Any]:
    with _lock:
        total = sum(len(v[2]) for v in _cache.values())
        return {
            "entries": len(_cache),
            "bytes": total,
            "inflight": len(_inflight),
            "max_entries": hardening.THUMB_CACHE_MAX_ENTRIES,
            "max_bytes": hardening.THUMB_CACHE_MAX_BYTES,
            "ttl": hardening.THUMB_TTL,
        }


def clear() -> int:
    with _lock:
        n = len(_cache)
        _cache.clear()
        return n


def _referer_for(url: str) -> str:
    try:
        p = urlparse(url)
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}/"
    except Exception:
        pass
    return ""


def _fetch_network(url: str, *, proxy: str | None = None) -> tuple[str, bytes]:
    headers = {
        "User-Agent": _UA,
        "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
    }
    referer = _referer_for(url)
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(url, headers=headers)
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    else:
        opener = urllib.request.build_opener()

    with opener.open(request, timeout=15) as response:  # noqa: S310
        content_type = response.headers.get("Content-Type", "image/jpeg")
        body = response.read(_MAX_BODY)
    return content_type, body


def get_or_fetch(url: str, *, proxy: str | None = None) -> tuple[str, bytes]:
    """校验 → 缓存命中或抓取；返回 (content_type, body)。"""
    clean = (url or "").strip()
    ok, why = guards.check_image_url(clean)
    if not ok:
        raise ValueError(why)

    now = time.time()
    with _lock:
        cached = _cache.get(clean)
        if cached and now - cached[0] < hardening.THUMB_TTL:
            return cached[1], cached[2]
        pending = _inflight.get(clean)
        if pending is None:
            event = threading.Event()
            box: dict[str, Any] = {}
            _inflight[clean] = (event, box)
            leader = True
        else:
            event, box = pending
            leader = False

    if not leader:
        event.wait(timeout=30)
        if box.get("ok"):
            return box["value"]
        raise box.get("error") or TimeoutError("缩略图并发等待超时")

    try:
        content_type, body = _fetch_network(clean, proxy=proxy)
        with _lock:
            _cache[clean] = (time.time(), content_type, body)
            hardening.trim_thumb_cache(_cache)
            box["ok"] = True
            box["value"] = (content_type, body)
        return content_type, body
    except Exception as exc:
        with _lock:
            box["ok"] = False
            box["error"] = exc
        raise
    finally:
        with _lock:
            _inflight.pop(clean, None)
        event.set()
