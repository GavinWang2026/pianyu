# Copyright (c) 2026 Hyht-Team. All rights reserved.
# See LICENSE (proprietary) and NOTICE.md (third-party components).
"""安全与稳定性加固层（自研）。

    · 配置写入校验 / 路径越界防护           → validate_config_patch()
    · 缩略图缓存上限与淘汰                 → trim_thumb_cache()
    · 连接数 / 任务并发上限                → LimitedThreadingHTTPServer
    · JSON 请求体大小限制                  → MAX_JSON_BYTES / PayloadTooLarge
    · LLM base_url SSRF 防护               → check_llm_base_url()

只依赖标准库；所有函数可单测，不改上游 yt-dlp 一行。
"""

from __future__ import annotations

import os
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import guards

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

MAX_JSON_BYTES = 1024 * 1024          # JSON 请求体 1 MiB（P1-4）
THUMB_CACHE_MAX_ENTRIES = 64          # 缓存条目上限（P1-1）
THUMB_CACHE_MAX_BYTES = 32 * 1024 * 1024
THUMB_TTL = 3600
MAX_WORKERS = 48                      # 同时服务的连接上限（P1-2）

# 本地 LLM 常见端口：Ollama 11434 / LM Studio 1234 / vLLM 8000 / 通用 8080
_LLM_LOCAL_PORTS = (80, 443, 11434, 1234, 8000, 8080)
_LLM_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")

# 不允许把下载目录指向系统 / 凭据目录（P0-5）
_FORBIDDEN_DIR_PREFIXES = (
    "/etc", "/System", "/Library", "/usr", "/bin", "/sbin", "/var",
    "/private", "/Applications", "/dev", "/proc", "/sys",
)


class PayloadTooLarge(ValueError):
    """JSON 请求体超过上限。"""


# ---------------------------------------------------------------------------
# P0-5 / P1-5：配置写入校验
# ---------------------------------------------------------------------------

def _llm_ports() -> tuple[int, ...]:
    raw = (os.environ.get("AVD_LLM_ALLOW_PORTS") or "").strip()
    if not raw:
        return _LLM_LOCAL_PORTS
    ports: list[int] = []
    for part in raw.split(","):
        try:
            value = int(part.strip())
        except ValueError:
            continue
        if 0 < value < 65536:
            ports.append(value)
    return tuple(sorted(set(_LLM_LOCAL_PORTS + tuple(ports))))


def check_llm_base_url(raw: str) -> tuple[bool, str]:
    """LLM 出口校验：堵住「把转写全文 POST 到内网地址」这条数据外带链。

    默认放行：公网域名（标准端口 / 显式端口白名单）、本机 LLM（Ollama 等）。
    拒绝：私网 / 回环以外的内网 IP、云元数据、DNS rebinding、userinfo 注入。

    需要访问内网自建模型时，用 ``AVD_LLM_ALLOW_HOSTS`` 显式放行域名，
    或用 ``AVD_LLM_ALLOW_PORTS`` 放开端口；不默许。
    """
    text = (raw or "").strip()
    if not text:
        return True, ""
    parsed = urlparse(text if "://" in text else "https://" + text)
    if parsed.scheme not in ("http", "https"):
        return False, "模型地址仅支持 http/https"
    if parsed.username or parsed.password:
        return False, "模型地址不允许携带账号密码"
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return False, "模型地址缺少主机名"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in _llm_ports():
        return False, f"模型地址端口 {port} 不在允许列表（AVD_LLM_ALLOW_PORTS 可放开）"

    explicit = (os.environ.get("AVD_LLM_ALLOW_HOSTS") or "").strip()
    allowed = tuple(h.strip().lower().lstrip(".") for h in explicit.split(",") if h.strip())
    if allowed and guards._host_matches(host, allowed):
        return True, ""

    if host in _LLM_LOCAL_HOSTS:
        return True, ""
    if not guards._ip_is_public(host):
        return False, "禁止把模型地址指向内网 / 云元数据地址"
    if host in ("169.254.169.254", "metadata.google.internal"):
        return False, "禁止访问云元数据地址"
    ips = guards._resolved_ips(host)
    if ips and any(not guards._ip_is_public(ip) for ip in ips):
        return False, "模型域名解析到内网地址，拒绝（DNS rebinding 防护）"
    if (parsed.path or "").strip("/"):
        # 允许 /v1 这类路径，但不允许把整段 Query/Fragment 塞进来
        if any(c in parsed.path for c in ("\r", "\n", "\\")):
            return False, "模型地址包含非法字符"
    return True, ""


def validate_config_patch(patch: dict[str, Any]) -> tuple[bool, str]:
    """写配置前的合法性校验。返回 (是否放行, 原因)。

    * ``download_dir``：必须绝对、不能是系统/凭据目录、不能是文件；
    * ``filename_template``：不得含路径分隔符、``..`` 或控制字符（防模板注入写任意目录）；
    * ``llm_base_url``：走 :func:`check_llm_base_url`。
    """
    if not isinstance(patch, dict):
        return False, "配置格式错误"

    if "download_dir" in patch:
        raw = str(patch["download_dir"] or "").strip()
        if not raw:
            return False, "下载目录不能为空"
        if ".." in Path(raw).parts:
            return False, "下载目录不允许包含 .. 上级路径"
        try:
            target = Path(raw).expanduser().resolve()
        except (OSError, ValueError):
            return False, "下载目录无法解析"
        home = Path.home().resolve()
        if target == Path(target.anchor):
            return False, "下载目录不能是磁盘根目录"
        lowered = str(target).lower()
        if any(lowered == p.lower() or lowered.startswith(p.lower() + "/")
               for p in _FORBIDDEN_DIR_PREFIXES):
            return False, "下载目录不能指向系统目录"
        if target == home / ".workbuddy" or (home / ".workbuddy") in target.parents:
            return False, "下载目录不能指向 WorkBuddy 配置目录"
        if target == home / ".ssh" or (home / ".ssh") in target.parents:
            return False, "下载目录不能指向凭据目录"
        if target.is_file():
            return False, "下载目录指向了一个文件"

    if "filename_template" in patch:
        tpl = str(patch["filename_template"] or "").strip()
        if not tpl:
            return False, "文件名模板不能为空"
        if "/" in tpl or "\\" in tpl or ".." in tpl:
            return False, "文件名模板不允许包含路径分隔符或 .."
        if any(ord(c) < 32 for c in tpl):
            return False, "文件名模板包含控制字符"

    if "llm_base_url" in patch:
        ok, why = check_llm_base_url(str(patch["llm_base_url"] or ""))
        if not ok:
            return False, why

    if "obsidian_dir" in patch:
        raw = str(patch["obsidian_dir"] or "").strip()
        if raw:
            if ".." in Path(raw).parts:
                return False, "Obsidian 目录不允许包含 .. 上级路径"
            try:
                target = Path(raw).expanduser().resolve()
            except (OSError, ValueError):
                return False, "Obsidian 目录无法解析"
            home = Path.home().resolve()
            if target == Path(target.anchor):
                return False, "Obsidian 目录不能是磁盘根目录"
            lowered = str(target).lower()
            if any(lowered == p.lower() or lowered.startswith(p.lower() + "/")
                   for p in _FORBIDDEN_DIR_PREFIXES):
                return False, "Obsidian 目录不能指向系统目录"
            # 必须落在用户主目录下，避免任意写
            try:
                target.relative_to(home)
            except ValueError:
                return False, "Obsidian 目录必须位于用户主目录下"
            if target.is_file():
                return False, "Obsidian 目录指向了一个文件"

    if "playlist_max_entries" in patch:
        try:
            n = int(patch["playlist_max_entries"])
        except (TypeError, ValueError):
            return False, "playlist_max_entries 必须是整数"
        if n < 1 or n > 50:
            return False, "playlist_max_entries 允许范围 1–50"

    if "asr_hotwords" in patch:
        raw = str(patch["asr_hotwords"] or "")
        if len(raw) > 8000:
            return False, "热词表过长（上限约 8000 字符）"

    if "live_record_seconds" in patch:
        try:
            n = int(patch["live_record_seconds"])
        except (TypeError, ValueError):
            return False, "live_record_seconds 必须是整数"
        if n not in (0, -1) and (n < 60 or n > 7200):
            return False, "live_record_seconds 允许 0（拒绝）、-1（录到取消）或 60–7200"

    if "download_rate_kbps" in patch:
        try:
            n = int(patch["download_rate_kbps"])
        except (TypeError, ValueError):
            return False, "download_rate_kbps 必须是整数"
        if n < 0 or n > 100_000:
            return False, "download_rate_kbps 允许 0–100000"

    if "feishu_webhook" in patch:
        raw = str(patch["feishu_webhook"] or "").strip()
        if raw:
            from urllib.parse import urlparse as _urlparse
            p = _urlparse(raw)
            if p.scheme != "https" or not p.hostname:
                return False, "飞书 webhook 必须是 https URL"
            host = (p.hostname or "").lower()
            if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local"):
                return False, "飞书 webhook 不能指向本机"

    if "notion_token" in patch:
        tok = str(patch["notion_token"] or "").strip()
        if tok and len(tok) > 512:
            return False, "notion_token 过长"

    if "notion_database_id" in patch:
        db = str(patch["notion_database_id"] or "").strip().replace("-", "")
        if db and (len(db) != 32 or any(c not in "0123456789abcdefABCDEF" for c in db)):
            return False, "notion_database_id 应为 32 位十六进制"

    return True, ""


# ---------------------------------------------------------------------------
# P1-1：缩略图缓存上限 + 淘汰（调用方须持有 _thumb_lock）
# ---------------------------------------------------------------------------

def trim_thumb_cache(cache: dict[str, tuple[float, str, bytes]],
                     now: float | None = None) -> dict[str, int]:
    """按 TTL + 条目数 + 总字节数三重限制裁剪缓存；返回裁剪统计。

    ``cache`` 结构：``url -> (timestamp, content_type, body)``。
    """
    now = time.time() if now is None else now
    expired = 0
    for key, value in list(cache.items()):
        if now - value[0] >= THUMB_TTL:
            cache.pop(key, None)
            expired += 1
    total = sum(len(value[2]) for value in cache.values())
    evicted = 0
    while cache and (len(cache) > THUMB_CACHE_MAX_ENTRIES or total > THUMB_CACHE_MAX_BYTES):
        oldest = min(cache, key=lambda k: cache[k][0])
        total -= len(cache.pop(oldest)[2])
        evicted += 1
    return {"expired": expired, "evicted": evicted,
            "entries": len(cache), "bytes": total}


# ---------------------------------------------------------------------------
# P1-2：并发上限（SSE 长连接会长期占线程，不能无限开）
# ---------------------------------------------------------------------------

class LimitedThreadingHTTPServer(ThreadingHTTPServer):
    """带硬上限的 ThreadingHTTPServer，超限直接回 503 并关闭连接。

    原版 ThreadingHTTPServer 每连接一线程且无上限，SSE 每 15s ping 会长期占线程，
    配合无限缓存可造成线程耗尽 DoS。这里用 BoundedSemaphore 收口。
    """

    max_workers = MAX_WORKERS
    request_queue_size = 32
    daemon_threads = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._slots = threading.BoundedSemaphore(self.max_workers)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
            except OSError:
                pass
            try:
                request.close()
            except OSError:
                pass
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()
