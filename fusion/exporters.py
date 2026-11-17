# Copyright (c) 2026 Hyht-Team. All rights reserved.
# See LICENSE (proprietary) and NOTICE.md (third-party components).
"""分发出口：Notion / 飞书 webhook 直推（标准库 urllib）。

只推本地已生成的 Markdown；不替用户登录第三方、不存第三方 Cookie。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from . import guards


def _post_json(url: str, payload: dict[str, Any], *, headers: dict[str, str] | None = None,
               timeout: float = 30.0) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    hdrs = {"Content-Type": "application/json; charset=utf-8", "User-Agent": "AVD-Flagship/2.0"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8", "replace")
            try:
                return {"ok": True, "status": resp.status, "body": json.loads(raw) if raw else {}}
            except json.JSONDecodeError:
                return {"ok": True, "status": resp.status, "body": raw[:500]}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"网络错误：{exc.reason}") from exc


def _safe_https(url: str) -> str:
    """与 guards 统一：禁本机/私网/DNS rebinding；仅 https。"""
    u = (url or "").strip()
    parsed = urlparse(u)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("仅允许 https 推送地址")
    ok, reason = guards.check_media_url(u, enforce_allowlist=False)
    if not ok:
        raise ValueError(f"推送地址不安全：{reason}")
    return u


def _chunk_text(text: str, size: int) -> list[str]:
    raw = text or ""
    if not raw:
        return []
    return [raw[i:i + size] for i in range(0, len(raw), size)]


def _paragraph_blocks(parts: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for part in parts:
        out.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": part[:2000]}}],
            },
        })
    return out


def push_notion(*, token: str, database_id: str, title: str, markdown: str) -> dict[str, Any]:
    """写入 Notion 数据库一页；超 90 块时用 children append 续写，避免静默截断。"""
    tok = (token or "").strip()
    db = (database_id or "").strip().replace("-", "")
    if not tok or not db:
        raise ValueError("缺少 notion_token 或 notion_database_id")
    # Notion paragraph rich_text 单块上限 2000；创建页 children 上限约 100
    chunks = _chunk_text(markdown or "", 2000)
    if not chunks:
        chunks = ["（空文稿）"]
    total = len(chunks)
    if total > 1:
        chunks = [f"（全文已分 {total} 块推送）\n" + chunks[0]] + chunks[1:]
        total = len(chunks)

    headers = {
        "Authorization": f"Bearer {tok}",
        "Notion-Version": "2022-06-28",
    }
    first_batch = chunks[:90]
    payload = {
        "parent": {"database_id": db if len(db) == 32 else database_id.strip()},
        "properties": {
            "Name": {"title": [{"text": {"content": (title or "AVD 知识稿")[:200]}}]},
        },
        "children": _paragraph_blocks(first_batch),
    }
    create = _post_json(
        "https://api.notion.com/v1/pages",
        payload,
        headers=headers,
    )
    page_id = ""
    body = create.get("body") if isinstance(create, dict) else {}
    if isinstance(body, dict):
        page_id = str(body.get("id") or "")

    pushed = len(first_batch)
    append_errors: list[str] = []
    # 续写剩余块（每批最多 90）
    rest = chunks[90:]
    while rest and page_id:
        batch = rest[:90]
        rest = rest[90:]
        try:
            _post_json(
                f"https://api.notion.com/v1/blocks/{page_id}/children",
                {"children": _paragraph_blocks(batch)},
                headers=headers,
            )
            pushed += len(batch)
        except Exception as exc:  # noqa: BLE001
            append_errors.append(str(exc)[:160])
            break

    truncated = pushed < total
    receipt = {
        "ok": True,
        "target": "notion",
        "page_id": page_id,
        "chunks_total": total,
        "chunks_pushed": pushed,
        "truncated": truncated,
        "chars_total": len(markdown or ""),
        "append_errors": append_errors,
        "message": (
            f"已推送 {pushed}/{total} 块"
            + ("（续写中断，请打开页面或本地全文）" if truncated else "")
        ),
        "raw": create,
    }
    return receipt


def push_feishu(
    *,
    webhook: str,
    title: str,
    markdown: str,
    fulltext_link: str = "",
) -> dict[str, Any]:
    """飞书自定义机器人 webhook：分块 Markdown 卡片；可附全文链接。"""
    url = _safe_https(webhook)
    chunks = _chunk_text(markdown or "", 3200)
    if not chunks:
        chunks = ["（空文稿）"]
    header = (title or "AVD 知识稿")[:80]
    elements = [
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**{header}**"}},
    ]
    link = (fulltext_link or "").strip()
    if link:
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"[打开全文 Markdown]({link})",
            },
        })
    total = len(chunks)
    card_limit = 8
    pushed = min(total, card_limit)
    for i, part in enumerate(chunks[:card_limit], 1):
        prefix = f"（{i}/{total}）\n" if total > 1 else ""
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": prefix + part},
        })
    truncated = total > card_limit
    if truncated:
        tip = f"…另有 {total - card_limit} 块未推送"
        if link:
            tip += "，请点上方全文链接查看"
        else:
            tip += "，请在本地打开全文"
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": tip + "。"},
        })
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": header}},
            "elements": elements,
        },
    }
    raw = _post_json(url, payload)
    return {
        "ok": True,
        "target": "feishu",
        "chunks_total": total,
        "chunks_pushed": pushed,
        "truncated": truncated,
        "chars_total": len(markdown or ""),
        "fulltext_link": link,
        "message": (
            f"已推送 {pushed}/{total} 块"
            + ("（卡片截断，请用全文链接）" if truncated else "")
        ),
        "raw": raw,
    }


def push_from_config(
    target: str,
    *,
    title: str,
    markdown: str,
    cfg: dict[str, Any],
    fulltext_link: str = "",
) -> dict[str, Any]:
    t = (target or "").strip().lower()
    if t == "notion":
        return push_notion(
            token=str(cfg.get("notion_token") or ""),
            database_id=str(cfg.get("notion_database_id") or ""),
            title=title,
            markdown=markdown,
        )
    if t in ("feishu", "lark"):
        return push_feishu(
            webhook=str(cfg.get("feishu_webhook") or ""),
            title=title,
            markdown=markdown,
            fulltext_link=fulltext_link or str(cfg.get("feishu_fulltext_link") or ""),
        )
    raise ValueError("target 仅支持 notion / feishu")
