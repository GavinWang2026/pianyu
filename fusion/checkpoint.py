# Copyright (c) 2026 Hyht-Team
# SPDX-License-Identifier: MIT
# See LICENSE (MIT) and NOTICE.md (third-party components).
"""ASR 分块检查点：崩溃 / 取消后可从已完成 chunk 续跑。"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any


def checkpoint_path(work_dir: str | Path, task_id: str = "") -> Path:
    root = Path(work_dir)
    root.mkdir(parents=True, exist_ok=True)
    name = f"asr-ckpt-{task_id}.json" if task_id else "asr-ckpt.json"
    return root / name


def cancel_flag_path(work_dir: str | Path, task_id: str = "") -> Path:
    root = Path(work_dir)
    root.mkdir(parents=True, exist_ok=True)
    name = f".asr-cancel-{task_id}" if task_id else ".asr-cancel"
    return root / name


def _checksum(segments: list[dict[str, Any]], chunk_index: int, chunks_total: int) -> str:
    blob = json.dumps(
        {"i": chunk_index, "n": chunks_total, "s": segments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.md5(blob.encode("utf-8")).hexdigest()


def save_checkpoint(
    work_dir: str | Path,
    *,
    task_id: str,
    chunk_index: int,
    chunks_total: int,
    segments: list[dict[str, Any]],
    duration: float = 0.0,
    source: str = "",
) -> Path:
    path = checkpoint_path(work_dir, task_id)
    payload = {
        "task_id": task_id,
        "chunk_index": int(chunk_index),
        "chunks_total": int(chunks_total),
        "segments": segments,
        "duration": float(duration or 0),
        "saved_at": time.time(),
        "checksum": _checksum(segments, int(chunk_index), int(chunks_total)),
    }
    # 来源 URL：崩溃 / 重启后内存里没有这条任务，只能靠它重建并续跑，
    # 否则「可续跑列表」给得出 task_id 却永远续不起来。
    if source:
        payload["source"] = str(source)[:2048]
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def load_checkpoint(work_dir: str | Path, task_id: str = "") -> dict[str, Any] | None:
    path = checkpoint_path(work_dir, task_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    segs = list(data.get("segments") or [])
    idx = int(data.get("chunk_index") or 0)
    total = int(data.get("chunks_total") or 0)
    expect = str(data.get("checksum") or "")
    if expect:
        got = _checksum(segs, idx, total)
        if got != expect:
            return None  # 校验失败，不续跑
    return data


def clear_checkpoint(work_dir: str | Path, task_id: str = "") -> None:
    path = checkpoint_path(work_dir, task_id)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        cancel_flag_path(work_dir, task_id).unlink(missing_ok=True)
    except OSError:
        pass


def request_soft_cancel(work_dir: str | Path, task_id: str = "") -> Path:
    """写取消哨兵；worker 在 chunk 边界读到后优雅退出并保留检查点。"""
    path = cancel_flag_path(work_dir, task_id)
    path.write_text("1", encoding="utf-8")
    return path


def is_cancel_requested(work_dir: str | Path, task_id: str = "") -> bool:
    return cancel_flag_path(work_dir, task_id).is_file()


def list_incomplete(
    root: str | Path,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """扫描目录下可续跑的检查点（校验和通过且未跑完）。"""
    base = Path(root)
    if not base.is_dir():
        return []
    lim = max(1, min(200, int(limit)))
    hits: list[dict[str, Any]] = []
    for path in sorted(base.rglob("asr-ckpt*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        if len(hits) >= lim:
            break
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        tid = str(data.get("task_id") or "")
        # 用父目录重载以走校验和
        loaded = load_checkpoint(path.parent, tid)
        if not loaded:
            continue
        idx = int(loaded.get("chunk_index") or 0)
        total = int(loaded.get("chunks_total") or 0)
        if total <= 0 or idx + 1 >= total:
            continue  # 已跑完或无效
        hits.append({
            "task_id": tid,
            "path": str(path),
            "work_dir": str(path.parent),
            "chunk_index": idx,
            "chunks_total": total,
            "resume_chunk": idx + 1,
            "segments": len(loaded.get("segments") or []),
            "duration": float(loaded.get("duration") or 0),
            "saved_at": float(loaded.get("saved_at") or 0),
            "source": str(loaded.get("source") or ""),
        })
    return hits


def resolve_ckpt_dir(cfg: dict[str, Any]) -> Path:
    """优先持久 ckpt_dir（流水线产物目录），否则回退临时 work_dir。"""
    raw = str(cfg.get("ckpt_dir") or cfg.get("work_dir") or "").strip()
    if not raw:
        raise ValueError("缺少 ckpt_dir / work_dir")
    path = Path(raw)
    path.mkdir(parents=True, exist_ok=True)
    return path
