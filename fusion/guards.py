# Copyright (c) 2026 Hyht-Team. All rights reserved.
# See LICENSE (proprietary) and NOTICE.md (third-party components).
"""合规边界与出口安全 —— 「可控的全网解析」的落地点。

原版 AVD 的服务端会代用户抓取任意 URL（``/api/thumb`` 可回显 SSRF），
且 GET 完全不校验来源、``/api/config`` 明文回显 LLM API Key。
本模块把这些收口成一层可配置、可单测的守卫，由 server.py 调用：

    check_request()      Host / Origin / 可选令牌（防 DNS rebinding 与跨站）
    check_media_url()    解析入口白名单（合规 + 防内网探测）
    check_image_url()    图片代理白名单（防 SSRF）
    redact_config()      配置对外脱敏
    within()             文件路径越界校验
    allowed_extractors() 交给 yt-dlp 的官方 extractor 裁剪开关

只依赖标准库。
"""

from __future__ import annotations

import hmac
import ipaddress
import os
import socket
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

# 合规站点白名单：与「使用边界」话术一致 —— 只处理用户有权处理的素材。
# 可通过环境变量 AVD_ALLOWED_HOSTS 覆盖（逗号分隔，支持后缀匹配）。
MEDIA_HOST_SUFFIXES: tuple[str, ...] = (
    "youtube.com", "youtu.be", "youtube-nocookie.com",
    "bilibili.com", "b23.tv", "bilivideo.com", "hdslb.com",
    "douyin.com", "iesdouyin.com", "douyinvod.com",
    "kuaishou.com", "kwai.com", "xiaohongshu.com", "xhscdn.com",
    "weibo.com", "sinaimg.cn", "ixigua.com", "toutiao.com",
    "v.qq.com", "youku.com", "iqiyi.com", "mgtv.com", "sohu.com",
    "acfun.cn", "zhihu.com", "vimeo.com", "dailymotion.com",
    "twitch.tv", "soundcloud.com", "bandcamp.com",
    "tiktok.com", "instagram.com", "facebook.com", "twitter.com", "x.com",
)

IMAGE_CDN_SUFFIXES: tuple[str, ...] = (
    "hdslb.com", "bilivideo.com", "ytimg.com", "ggpht.com",
    "douyinpic.com", "douyincdn.com", "douyinvod.com",
    "xhscdn.com", "sinaimg.cn", "qpic.cn", "kwaicdn.com",
    "vimage.com", "yximgs.com", "smtcdns.com", "akamaized.net",
)

# 精确键名白名单（禁止子串匹配）：避免 cookies_from_browser 被打成 "***"
# 导致前端 select 失配、保存时静默清空配置（SEC-02）。
SENSITIVE_KEYS = frozenset({
    "llm_api_key", "api_key", "token", "password", "secret",
    "cookie", "cookies", "cookiefile", "cookie_file",
    "notion_token", "feishu_webhook",
})
# 明确不脱敏的配置键（浏览器名 / 开关，不是凭据本体）
REDACT_EXCLUDE = frozenset({"cookies_from_browser"})

_LOCAL_HINTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0", "169.254.169.254",
                ".local", ".internal", ".lan", ".home.arpa")
# 允许的媒体/图片端口：80/443 为主；常见 CDN / 自建备用端口（MISC-02）
_ALLOWED_PORTS = frozenset({80, 443, 8080, 8443, 8000, 8888, 9000})


def allowed_hosts() -> tuple[str, ...]:
    override = (os.environ.get("AVD_ALLOWED_HOSTS") or "").strip()
    if not override:
        return MEDIA_HOST_SUFFIXES
    return tuple(h.strip().lower().lstrip(".") for h in override.split(",") if h.strip())


def allowed_extractors() -> list[str] | None:
    """交给 yt-dlp 的官方裁剪开关（--use-extractors / params allowed_extractors）。

    默认返回 None 表示不额外裁剪；设了 AVD_ALLOWED_EXTRACTORS 后按正则白名单收紧，
    并去掉 Generic 兜底，进一步缩小服务端可触达的解析范围。
    """
    raw = (os.environ.get("AVD_ALLOWED_EXTRACTORS") or "").strip()
    if not raw:
        return None
    return [p.strip() for p in raw.split(",") if p.strip()]


def _host_matches(host: str, suffixes: Iterable[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == s or host.endswith("." + s) for s in suffixes)


def _ip_is_public(literal: str) -> bool:
    try:
        addr = ipaddress.ip_address(literal)
    except ValueError:
        return True  # 不是 IP 字面量，交给域名规则
    return not (
        addr.is_private or addr.is_loopback or addr.is_link_local
        or addr.is_reserved or addr.is_multicast or addr.is_unspecified
    )


def _resolved_ips(host: str) -> list[str]:
    try:
        return [item[4][0] for item in socket.getaddrinfo(host, None)]
    except (socket.gaierror, OSError):
        return []


def check_media_url(url: str, *, enforce_allowlist: bool = True) -> tuple[bool, str]:
    """解析入口校验。返回 (是否放行, 原因)。

    ``enforce_allowlist=False`` 只保留安全底线（禁内网/本机/非常规端口/
    DNS rebinding），不套合规站点白名单 —— 供原版路由平滑升级使用；
    旗舰版一键流水线用默认 True，走完整合规边界。
    """
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https"):
        return False, "仅支持 http/https 链接"
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return False, "链接缺少主机名"
    # 后缀必须带点边界，避免 endswith("local") 误伤 foo.notlocal.com（MISC-05）
    for hint in _LOCAL_HINTS:
        if hint.startswith("."):
            if host.endswith(hint) or host == hint.lstrip("."):
                return False, "禁止解析内网地址"
        elif host == hint:
            return False, "禁止解析本机 / 内网地址"
    if not _ip_is_public(host):
        return False, "禁止解析本机 / 内网地址"
    if parsed.port and parsed.port not in _ALLOWED_PORTS:
        return False, "禁止非常规端口"
    if enforce_allowlist and not _host_matches(host, allowed_hosts()):
        return False, f"该站点不在合规白名单内：{host}"
    ips = _resolved_ips(host)
    if ips:
        bad = [ip for ip in ips if not _ip_is_public(ip)]
        if bad:
            return False, f"域名解析到内网地址，拒绝（DNS rebinding 防护）：{bad[0]}"
    return True, ""


def check_image_url(url: str) -> tuple[bool, str]:
    """图片代理校验：只放行已知 CDN，且解析结果不得指向内网。"""
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https"):
        return False, "仅支持 http/https"
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or not _ip_is_public(host):
        return False, "禁止回显本机 / 内网地址"
    if parsed.port and parsed.port not in _ALLOWED_PORTS:
        return False, "禁止非常规端口"
    if not _host_matches(host, IMAGE_CDN_SUFFIXES):
        return False, f"图片域名不在 CDN 白名单内：{host}"
    ips = _resolved_ips(host)
    if ips and any(not _ip_is_public(ip) for ip in ips):
        return False, "图片域名解析到内网地址，拒绝"
    return True, ""


def redact_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """对外回显配置时抹掉凭据，只保留「有没有配」。

    用精确键名匹配（SEC-02）：``cookies_from_browser`` 是浏览器名而非凭据，
    必须原样回显，否则前端 ``<select>`` 失配 → 保存时静默清空。
    """
    out: dict[str, Any] = {}
    for key, value in (cfg or {}).items():
        low = key.lower()
        if low in REDACT_EXCLUDE:
            out[key] = value
            continue
        if low in SENSITIVE_KEYS:
            out[key] = "***" if value else ""
            out[f"has_{key}"] = bool(value)
        else:
            out[key] = value
    return out


def within(base: Path, candidate: str | Path) -> Path | None:
    """路径越界校验：必须落在 base 内，否则返回 None。"""
    try:
        resolved = Path(candidate).expanduser().resolve()
        resolved.relative_to(Path(base).expanduser().resolve())
    except (ValueError, OSError):
        return None
    return resolved


def check_request(handler: Any) -> tuple[bool, str]:
    """请求级守卫：Host 白名单 + Origin 同源 + 可选访问令牌。

    原版只对 POST 校验 Origin、且无 Origin 就放行；这里把 GET 一并纳入，
    并拒绝非本机 Host，堵住 DNS rebinding。设 AVD_TOKEN 后所有 API 都要带
    X-AVD-Token（前端由 flagship.js 从 sessionStorage 读取并附带）。

    威胁模型（D35）：这是**单机本地防护**，不是多用户认证。
    - 仅回环地址可访问；默认无 Token，同机进程本来就能访问 127.0.0.1。
    - 无 Origin 的请求放行，因为命令行/脚本不会带 Origin；浏览器跨站请求必带
      Origin，因此能被同源校验拦住。
    - Token 用 ``hmac.compare_digest`` 比较，避免时间侧信道。
    - 若未来开放局域网访问，必须同时启用 Token、Origin 校验与速率限制。
    """
    raw_host = (handler.headers.get("Host") or "").strip()
    host = raw_host.split(":")[0].strip("[]").lower()
    if host not in ("127.0.0.1", "localhost", "::1"):
        return False, "仅允许本机访问"

    origin = handler.headers.get("Origin")
    if origin:
        if origin not in (f"http://{raw_host}", f"https://{raw_host}"):
            return False, "请求被拒绝（跨站来源）"

    token = (os.environ.get("AVD_TOKEN") or "").strip()
    if token:
        presented = handler.headers.get("X-AVD-Token") or ""
        if not hmac.compare_digest(presented.encode("utf-8"), token.encode("utf-8")):
            return False, "需要访问令牌（X-AVD-Token）"
    return True, ""
