# Copyright (c) 2026 Hyht-Team
# SPDX-License-Identifier: MIT
# See LICENSE (MIT) and NOTICE.md (third-party components).
"""结构化诊断包：一键打包环境 / 日志 / 失败样本（不含 Cookie/密钥）。"""

from __future__ import annotations

import json
import os
import platform
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import guards

APP_DIR = Path(__file__).resolve().parent.parent

# 仅允许安全的任务 id：拒绝 ".."/绝对路径/斜杠
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")

# zip 内允许的任务产物后缀（二次白名单）
_ALLOWED_SUFFIX = frozenset({
    ".md", ".txt", ".srt", ".json", ".vtt", ".log", ".yml", ".yaml",
})
# 崩溃现场 / 检查点文件名允许进包
_CRASH_CKPT_PREFIX = ("asr-crash-", "asr-ckpt-")
_DENIED_NAMES = frozenset({
    "config.json", "cookies.txt", "cookies.sqlite", ".env", "credentials.json",
    "secret", "secrets",
})
_DENIED_SUBSTR = (
    "cookie", "api_key", "apikey", "password", "token", "secret", ".git",
)


def _safe_task_id(task_id: str) -> str:
    tid = (task_id or "").strip()
    if not tid or not _TASK_ID_RE.match(tid):
        return ""
    if ".." in tid or "/" in tid or "\\" in tid:
        return ""
    return tid


def _allowed_task_file(path: Path) -> bool:
    name = path.name.lower()
    if name in _DENIED_NAMES:
        return False
    if any(s in name for s in _DENIED_SUBSTR):
        return False
    if any(name.startswith(p) for p in _CRASH_CKPT_PREFIX) and path.suffix.lower() == ".json":
        return True
    if path.suffix.lower() not in _ALLOWED_SUFFIX:
        return False
    return True


def build_diagnose_pack(
    *,
    task_id: str = "",
    dest: Path | None = None,
    include_full_artifacts: bool = False,
    outputs_root: Path | None = None,
    log_dir: Path | None = None,
) -> dict[str, Any]:
    """打包诊断信息。

    * ``task_id`` 必须通过格式校验，且目录必须落在 ``outputs/`` 内。
    * 默认只附任务 ``meta.json`` + Markdown 前 2000 字；完整产物需显式勾选。
    * 永不打包 config / cookies / 媒体二进制。
    * ``outputs_root`` / ``log_dir`` 仅供单测注入，默认仍指向产品目录。
    """
    out_dir = APP_DIR / "outputs" / "diagnose"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe_tid = _safe_task_id(task_id)
    # 工单回执号：时间戳 + 短随机，便于售后对账（不上传、仅本地）
    ticket_id = f"AVD-{stamp}-{abs(hash((safe_tid, stamp))) % 10_000_000:07d}"
    name = f"avd-diagnose-{stamp}" + (f"-{safe_tid[:8]}" if safe_tid else "")
    zip_path = Path(dest) if dest else (out_dir / f"{name}.zip")

    env: dict[str, Any] = {
        "python": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "app_dir": str(APP_DIR),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "ticket_id": ticket_id,
        "task_id_requested": task_id or "",
        "task_id_accepted": safe_tid,
    }
    try:
        from . import boot_check
        env["health"] = boot_check.health()
    except Exception as exc:  # noqa: BLE001
        env["health_error"] = str(exc)
    try:
        from . import machine as stage_machine
        env["stage_machine"] = stage_machine.describe()
    except Exception as exc:  # noqa: BLE001
        env["stage_machine_error"] = str(exc)

    files: list[tuple[str, bytes]] = [
        ("env.json", json.dumps(env, ensure_ascii=False, indent=2).encode("utf-8")),
    ]
    skipped: list[str] = []

    # 最近 ASR 失败日志（仅 logs/ 内）
    logs_root = Path(log_dir) if log_dir is not None else (APP_DIR / "logs")
    if logs_root.is_dir():
        logs = sorted(logs_root.glob("asr-fail-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in logs[:5]:
            resolved = guards.within(logs_root, path)
            if resolved is None or not resolved.is_file():
                continue
            try:
                files.append((f"logs/{resolved.name}", resolved.read_bytes()[:200_000]))
            except OSError:
                pass

    # 指定任务产物（路径校验 + 白名单）
    if task_id and not safe_tid:
        skipped.append(f"拒绝非法 task_id：{task_id!r}")
    elif safe_tid:
        outputs_root = (Path(outputs_root) if outputs_root is not None else (APP_DIR / "outputs")).resolve()
        work = guards.within(outputs_root, outputs_root / safe_tid)
        if work is None or not work.is_dir():
            skipped.append(f"任务目录不存在或不在 outputs 内：{safe_tid}")
        else:
            # 默认瘦身：meta + md 前 2000 字 + 崩溃现场 / 检查点
            meta = work / "meta.json"
            if meta.is_file() and _allowed_task_file(meta):
                try:
                    files.append(("task/meta.json", meta.read_bytes()[:200_000]))
                except OSError:
                    pass
            md_candidates = sorted(work.glob("*.md"))
            if md_candidates:
                md = md_candidates[0]
                if _allowed_task_file(md):
                    try:
                        text = md.read_text(encoding="utf-8", errors="replace")
                        preview = text[:2000]
                        if len(text) > 2000:
                            preview += "\n…(truncated)…\n"
                        files.append((f"task/{md.name}.preview.txt", preview.encode("utf-8")))
                    except OSError:
                        pass
            for pattern in ("asr-crash-*.json", "asr-ckpt-*.json", "asr-ckpt.json"):
                for path in sorted(work.glob(pattern))[:8]:
                    if not _allowed_task_file(path):
                        continue
                    try:
                        files.append((f"task/{path.name}", path.read_bytes()[:200_000]))
                    except OSError:
                        pass
            if include_full_artifacts:
                already = {name for name, _ in files}
                for path in work.rglob("*"):
                    if not path.is_file():
                        continue
                    if path.suffix.lower() in {".mp4", ".m4a", ".wav", ".webm", ".jpg", ".png"}:
                        continue
                    if not _allowed_task_file(path):
                        skipped.append(path.name)
                        continue
                    resolved = guards.within(work, path)
                    if resolved is None:
                        skipped.append(str(path))
                        continue
                    rel = f"task/{resolved.relative_to(work).as_posix()}"
                    if rel in already:
                        continue
                    try:
                        data = resolved.read_bytes()
                    except OSError:
                        continue
                    if len(data) > 500_000:
                        data = data[:500_000] + b"\n...truncated...\n"
                    files.append((rel, data))
                    already.add(rel)

    # 能力边界说明（公开文档）
    cap = APP_DIR / "CAPABILITIES.md"
    if cap.is_file():
        files.append(("CAPABILITIES.md", cap.read_bytes()))

    # 可恢复任务摘要（不附媒体）
    try:
        from . import recovery as _rec
        recoverable = _rec.scan_recoverable(outputs_root=outputs_root or (APP_DIR / "outputs"), limit=20)
        if recoverable:
            files.append((
                "recoverable.json",
                json.dumps(recoverable, ensure_ascii=False, indent=2).encode("utf-8"),
            ))
            env["recoverable_count"] = len(recoverable)
    except Exception as exc:  # noqa: BLE001
        env["recoverable_error"] = str(exc)[:200]

    receipt = (
        f"ticket_id: {ticket_id}\n"
        f"generated_at: {env['generated_at']}\n"
        f"task_id: {safe_tid or '(none)'}\n"
        f"note: 本地工单回执；可经 POST /api/diagnose/submit 上传（需 AVD_SUPPORT_URL）。\n"
    )
    files.append(("RECEIPT.txt", receipt.encode("utf-8")))

    if skipped:
        files.append((
            "skipped.txt",
            ("\n".join(skipped) + "\n").encode("utf-8"),
        ))

    # zip 写入前再滤一次名字
    safe_files: list[tuple[str, bytes]] = []
    for name_in_zip, data in files:
        low = name_in_zip.lower()
        if any(s in low for s in ("cookie", "config.json", ".git/", "api_key", "secret")):
            skipped.append(name_in_zip)
            continue
        safe_files.append((name_in_zip, data))

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name_in_zip, data in safe_files:
            zf.writestr(name_in_zip, data)

    # 本地回执索引（便于工单对账）
    try:
        (out_dir / f"{ticket_id}.receipt.json").write_text(
            json.dumps({
                "ticket_id": ticket_id,
                "path": str(zip_path),
                "task_id": safe_tid,
                "generated_at": env["generated_at"],
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass

    return {
        "path": str(zip_path),
        "bytes": zip_path.stat().st_size,
        "files": len(safe_files),
        "task_id_accepted": safe_tid,
        "ticket_id": ticket_id,
        "skipped": skipped[:40],
    }


def list_tickets(*, limit: int = 50) -> list[dict[str, Any]]:
    """列出本地诊断工单回执（按生成时间倒序）。"""
    lim = max(1, min(200, int(limit)))
    out_dir = APP_DIR / "outputs" / "diagnose"
    if not out_dir.is_dir():
        return []
    receipts = sorted(
        out_dir.glob("AVD-*.receipt.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    items: list[dict[str, Any]] = []
    for path in receipts[:lim]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        ticket = str(data.get("ticket_id") or path.name.replace(".receipt.json", ""))
        zip_path = str(data.get("path") or "")
        items.append({
            "ticket_id": ticket,
            "task_id": str(data.get("task_id") or ""),
            "generated_at": str(data.get("generated_at") or ""),
            "path": zip_path,
            "exists": bool(zip_path) and Path(zip_path).is_file(),
            "receipt": str(path),
            "submit_status": str(data.get("submit_status") or "local"),
            "submitted_at": str(data.get("submitted_at") or ""),
            "remote_receipt": str(data.get("remote_receipt") or ""),
        })
    return items


def _support_url(explicit: str | None = None) -> str:
    feed = (explicit if explicit is not None else os.environ.get("AVD_SUPPORT_URL") or "").strip()
    if not feed:
        return ""
    parsed = urlparse(feed)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("AVD_SUPPORT_URL 必须是 https URL")
    ok, reason = guards.check_media_url(feed, enforce_allowlist=False)
    if not ok:
        raise ValueError(f"支持端点不安全：{reason}")
    return feed


def submit_ticket(
    ticket_id: str,
    *,
    url: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """工单上传闭环：把本地诊断 zip POST 到 AVD_SUPPORT_URL，回写回执。

    未配置支持端点时返回 ``status=local_only``（诚实替代：导出 zip 人工发送）。
    """
    tid = (ticket_id or "").strip()
    if not tid.startswith("AVD-"):
        raise ValueError("无效 ticket_id")
    out_dir = APP_DIR / "outputs" / "diagnose"
    receipt_path = out_dir / f"{tid}.receipt.json"
    if not receipt_path.is_file():
        raise ValueError("工单回执不存在")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"回执损坏：{exc}") from exc
    zip_path = Path(str(receipt.get("path") or ""))
    if not zip_path.is_file():
        raise ValueError("诊断包 zip 已删除，请重新生成")

    try:
        endpoint = _support_url(url)
    except ValueError:
        raise
    if not endpoint:
        return {
            "ok": True,
            "status": "local_only",
            "ticket_id": tid,
            "path": str(zip_path),
            "message": "未配置 AVD_SUPPORT_URL：请把 zip 与 ticket_id 发给售后（本地闭环）",
            "alternative": "export_zip",
        }

    boundary = f"----AVD{int(time.time())}"
    raw_zip = zip_path.read_bytes()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="ticket_id"\r\n\r\n{tid}\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="pack"; filename="{zip_path.name}"\r\n'
        f"Content-Type: application/zip\r\n\r\n"
    ).encode("utf-8") + raw_zip + f"\r\n--{boundary}--\r\n".encode("ascii")
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "AVD-Flagship/2.0",
            "Accept": "application/json",
            "X-AVD-Ticket": tid,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read(64 * 1024).decode("utf-8", "replace")
            status_code = int(resp.status)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"上传失败 HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"上传失败：{exc}") from exc

    remote = ""
    try:
        parsed = json.loads(raw) if raw else {}
        if isinstance(parsed, dict):
            remote = str(parsed.get("receipt") or parsed.get("id") or parsed.get("ticket_id") or "")[:200]
    except json.JSONDecodeError:
        parsed = {"raw": raw[:300]}
        remote = ""

    submitted_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    receipt.update({
        "submit_status": "submitted",
        "submitted_at": submitted_at,
        "remote_receipt": remote,
        "remote_status": status_code,
        "remote_body": parsed if isinstance(parsed, dict) else {"raw": str(parsed)[:300]},
    })
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "ok": True,
        "status": "submitted",
        "ticket_id": tid,
        "path": str(zip_path),
        "remote_receipt": remote,
        "submitted_at": submitted_at,
    }
