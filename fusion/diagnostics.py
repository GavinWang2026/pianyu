# Copyright (c) 2026 Hyht-Team. All rights reserved.
# See LICENSE (proprietary) and NOTICE.md (third-party components).
"""平台解析失败的可操作诊断。

把 yt-dlp 抛出的英文错误翻译成「缺什么、去哪儿补、补完怎么验」。
实测依据：2026-11-12 抖音短链 ``v.douyin.com/nuBI2cMEa9c/`` 真跑结果
``Fresh cookies (not necessarily logged in) are needed``（s_v_web_id 缺失，
Web detail API 返回 403）。原版会把这串英文原样丢给用户，无法自助恢复。
"""

from __future__ import annotations

import re
from typing import Any

# 顺序即优先级：先匹配更具体的
_RULES: tuple[tuple[str, str, str, str, tuple[str, ...]], ...] = (
    (
        r"operation not permitted|permission denied|could not copy safari|full disk access",
        "COOKIE_PERMISSION",
        "浏览器 Cookie 读取被系统权限拒绝",
        "macOS 对 Safari 的 Cookies.binarycookies 有 TCC 保护，终端 / 启动器"
        "没有「完全磁盘访问权限」时无法读取。这是系统限制，不是产品故障。",
        (
            "系统设置 → 隐私与安全性 → 完全磁盘访问权限 → 授权终端或启动器，然后重启服务",
            "或改用 Chrome / Edge / Firefox 的 Cookie 来源（这些浏览器的 Cookie 库无此限制）",
            "最稳妥：用浏览器插件导出 Netscape 格式 cookies.txt 放到发布包 cookies/ 目录",
            "补好后调用 /api/platform/diagnose 复验",
        ),
    ),
    (
        r"fresh cookies[\s\S]{0,200}please report this issue|please report this issue[\s\S]{0,200}fresh cookies",
        "COOKIE_STALE",
        "Cookie 已加载，但被平台判定为无效 / 过期",
        "与「完全没配 Cookie」不同：yt-dlp 已成功读到 s_v_web_id（因此把它当作"
        "非预期错误并提示上报 issue），但 Web detail API 仍拒绝该凭据。"
        "通常是复制了过期 Cookie、Cookie 与当前出口 IP 不匹配，或值不完整。",
        (
            "重新用浏览器插件导出一份新鲜 cookies.txt，覆盖 cookies/cookies.txt",
            "确认导出时浏览器已正常打开过该站点（验证挑战会刷新 s_v_web_id）",
            "确认代理出口地区与浏览器访问时一致",
            "补好后调用 /api/platform/diagnose 复验",
        ),
    ),
    (
        r"fresh cookies|s_v_web_id|cookies \(not necessarily logged in\)",
        "COOKIE_REQUIRED",
        "该平台需要新鲜浏览器 Cookie（不必登录），当前没有可用凭据",
        "抖音 / TikTok 系站点要求浏览器验证挑战生成的 s_v_web_id。服务端"
        "直接请求 Web detail API 会被 403 拒绝，这不是解析引擎坏了。",
        (
            "在「设置 → Cookie 来源」选择已登录过该站点的浏览器（Chrome / Edge / Firefox / Safari）",
            "或用浏览器插件导出 Netscape 格式 cookies.txt，放到发布包 cookies/cookies.txt",
            "macOS 选 Safari 时还需给终端「完全磁盘访问权限」，否则读不到 Cookie 库",
            "也可设置环境变量 AVD_COOKIE_FILE 指向包内 cookie 文件，然后重启服务",
            "补好后调用 /api/platform/diagnose 复验，返回 ready=true 即可一键出稿",
        ),
    ),
    (
        r"po[ _-]?token|attestation|js challenge|challenge",
        "PO_TOKEN",
        "该平台要求 PO Token / JS 挑战凭据",
        "部分站点（尤其 YouTube 的某些客户端）需要额外的证明令牌。",
        (
            "按 yt-dlp 官方说明配置 PO Token 提供器或 JS runtime",
            "或改用带浏览器 Cookie 的客户端路径",
        ),
    ),
    (
        r"login required|sign in|log in|private video|members.only|authenticat",
        "LOGIN_REQUIRED",
        "该内容需要登录态",
        "私有 / 会员 / 仅粉丝可见内容，匿名请求拿不到元数据。",
        (
            "在设置中选择浏览器 Cookie，或导入登录后的 cookies.txt",
            "确认账号确实有权访问该内容",
        ),
    ),
    (
        r"http error 403|403: forbidden|forbidden",
        "BLOCKED_403",
        "站点返回 403，通常是风控 / 缺少 Cookie / 地区限制",
        "服务端 IP 或请求特征被站点拒绝。",
        (
            "先补 Cookie（见 COOKIE_REQUIRED）",
            "检查代理设置是否与站点地区匹配",
            "稍后重试，避免高频触发风控",
        ),
    ),
    (
        r"http error 429|too many requests|rate.?limit",
        "RATE_LIMIT",
        "触发平台限流",
        "短时间内请求过多。",
        ("等待数分钟后重试", "降低批量并发（当前产品为单条任务定位）"),
    ),
    (
        r"unsupported url|no suitable infoextractor|not a valid url",
        "UNSUPPORTED",
        "链接格式不被解析引擎识别",
        "短链可能已过期、被删除，或该站点不在引擎支持范围。",
        (
            "用浏览器打开链接确认可访问，再复制落地后的完整地址",
            "检查是否被合规站点白名单 / extractor 裁剪开关挡住",
        ),
    ),
    (
        r"geo.?restrict|not available in your country|region",
        "GEO_BLOCKED",
        "内容存在地区限制",
        "当前出口 IP 所在地区不可访问。",
        ("配置合规的代理出口", "或换用本地区可访问的素材"),
    ),
    (
        r"timed out|timeout|could not connect|name or service not known",
        "NETWORK",
        "网络超时或域名解析失败",
        "链路不通或站点不可达。",
        ("检查网络与代理设置", "确认站点可从本机访问"),
    ),
)


def diagnose(error: BaseException | str) -> dict[str, Any]:
    """返回结构化诊断。永不抛异常。"""
    raw = str(error).strip()
    low = raw.lower()
    for pattern, code, title, detail, actions in _RULES:
        if re.search(pattern, low):
            return {
                "code": code,
                "title": title,
                "detail": detail,
                "actions": list(actions),
                "raw": raw[:600],
                "actionable": True,
            }
    return {
        "code": "UNKNOWN",
        "title": "解析失败",
        "detail": "未能归类到已知平台限制，请查看原始错误信息。",
        "actions": ["检查链接与网络", "运行 python doctor.py --check 查看环境"],
        "raw": raw[:600],
        "actionable": False,
    }


def friendly(error: BaseException | str) -> str:
    """一行式中文提示，用于任务进度 / message。"""
    item = diagnose(error)
    return f"{item['title']}｜{item['actions'][0]}"


def platform_hint(url: str) -> dict[str, Any]:
    """静态平台需求矩阵（不联网）。用于诊断接口给出「这个站点通常要什么」。"""
    low = (url or "").lower()
    if any(k in low for k in ("douyin", "iesdouyin", "tiktok")):
        return {"platform": "抖音 / TikTok", "needs": ["fresh_cookie"],
                "note": "Web detail API 需要 s_v_web_id；短链会 302 到 www.douyin.com"}
    if "youtube" in low or "youtu.be" in low:
        return {"platform": "YouTube", "needs": ["po_token_optional", "cookie_optional"],
                "note": "部分客户端需 PO Token / JS runtime；字幕轨通常可用"}
    if "bilibili" in low or "b23.tv" in low:
        return {"platform": "哔哩哔哩", "needs": ["cookie_for_hq"],
                "note": "高清格式与大会员内容需要登录 Cookie"}
    return {"platform": "通用 / 其他", "needs": [], "note": "按链接形态由 yt-dlp 自动选择 extractor"}
