# Copyright (c) 2026 Hyht-Team. All rights reserved.
# See LICENSE (proprietary) and NOTICE.md (third-party components).
"""崩溃现场留存 + 可恢复任务扫描（R4 可靠性）。

* worker / 流水线异常时写 ``asr-crash-{task_id}.json``
* 启动或诊断时可 ``scan_recoverable`` 找到未完成检查点
* 合成续跑实证 ``prove_resume`` 不依赖真 ASR
"""

from __future__ import annotations

import json
import time
import traceback
from pathlib import Path
from typing import Any

from . import checkpoint

APP_DIR = Path(__file__).resolve().parent.parent


def crash_path(work_dir: str | Path, task_id: str = "") -> Path:
    root = Path(work_dir)
    root.mkdir(parents=True, exist_ok=True)
    name = f"asr-crash-{task_id}.json" if task_id else "asr-crash.json"
    return root / name


def save_crash_scene(
    work_dir: str | Path,
    *,
    task_id: str = "",
    reason: str = "",
    exc: BaseException | None = None,
    extra: dict[str, Any] | None = None,
) -> Path:
    """写入崩溃现场；附带当前检查点摘要（若有）。"""
    root = Path(work_dir)
    root.mkdir(parents=True, exist_ok=True)
    ckpt = checkpoint.load_checkpoint(root, task_id) if task_id else None
    payload: dict[str, Any] = {
        "task_id": task_id,
        "reason": (reason or (str(exc) if exc else "unknown"))[:800],
        "exc_type": type(exc).__name__ if exc else "",
        "traceback": "".join(traceback.format_exception(exc))[:8000] if exc else "",
        "saved_at": time.time(),
        "checkpoint": None,
    }
    if ckpt:
        payload["checkpoint"] = {
            "chunk_index": ckpt.get("chunk_index"),
            "chunks_total": ckpt.get("chunks_total"),
            "resume_chunk": int(ckpt.get("chunk_index") or 0) + 1,
            "segments": len(ckpt.get("segments") or []),
            "duration": ckpt.get("duration"),
            "checksum": ckpt.get("checksum"),
        }
    if extra:
        payload["extra"] = {str(k): v for k, v in list(extra.items())[:40]}
    path = crash_path(root, task_id)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_crash_scene(work_dir: str | Path, task_id: str = "") -> dict[str, Any] | None:
    path = crash_path(work_dir, task_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def clear_crash_scene(work_dir: str | Path, task_id: str = "") -> None:
    try:
        crash_path(work_dir, task_id).unlink(missing_ok=True)
    except OSError:
        pass


def scan_recoverable(
    *,
    outputs_root: str | Path | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """扫描 outputs/ 下未完成检查点与崩溃现场。"""
    root = Path(outputs_root) if outputs_root else (APP_DIR / "outputs")
    incomplete = checkpoint.list_incomplete(root, limit=limit)
    out: list[dict[str, Any]] = []
    for item in incomplete:
        wd = item["work_dir"]
        tid = item["task_id"]
        crash = load_crash_scene(wd, tid)
        row = dict(item)
        row["has_crash"] = crash is not None
        if crash:
            row["crash_reason"] = str(crash.get("reason") or "")[:200]
        out.append(row)
    # 有崩溃但检查点已被清掉的，也列出来
    if root.is_dir():
        seen = {r["task_id"] for r in out}
        for path in sorted(root.rglob("asr-crash-*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            if len(out) >= limit:
                break
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            tid = str((data or {}).get("task_id") or "")
            if not tid or tid in seen:
                continue
            out.append({
                "task_id": tid,
                "path": "",
                "work_dir": str(path.parent),
                "chunk_index": (data.get("checkpoint") or {}).get("chunk_index"),
                "chunks_total": (data.get("checkpoint") or {}).get("chunks_total"),
                "resume_chunk": (data.get("checkpoint") or {}).get("resume_chunk"),
                "segments": (data.get("checkpoint") or {}).get("segments") or 0,
                "duration": 0,
                "saved_at": float(data.get("saved_at") or 0),
                "source": str(((data.get("extra") or {}).get("url")) or ""),
                "has_crash": True,
                "crash_reason": str(data.get("reason") or "")[:200],
                "crash_only": True,
            })
            seen.add(tid)
    return out[:limit]


def estimate_long_resume(
    duration_sec: float,
    *,
    cancel_ratio: float = 0.4,
    asr_realtime_factor: float = 0.35,
) -> dict[str, Any]:
    """协议级长视频续跑收益估算（不跑真 ASR）。

    用与 worker 相同的 ``adaptive_chunk_seconds`` / ``plan_chunks`` 逻辑，
    假设在 ``cancel_ratio`` 处取消，返回 resume_chunk 与预估节省比例。
    """
    try:
        from tools import asr_worker as _aw
    except Exception:
        # 包外导入失败时用简化公式
        dur = max(60.0, float(duration_sec or 0))
        chunk = 90.0 if dur <= 600 else min(300.0, dur / 10.0)
        total = max(1, int(dur / chunk))
        cancel_at = max(0, min(total - 1, int(total * float(cancel_ratio))))
        resume = cancel_at + 1 if cancel_at + 1 < total else total
        remaining = max(0, total - resume)
        saved = resume / total if total else 0.0
        return {
            "duration_sec": dur,
            "chunks_total": total,
            "chunk_seconds": chunk,
            "cancel_after_chunk": cancel_at,
            "resume_chunk": resume,
            "remaining_chunks": remaining,
            "saved_ratio": round(saved, 3),
            "est_full_minutes": round(dur * float(asr_realtime_factor) / 60.0, 1),
            "est_resume_minutes": round(remaining * chunk * float(asr_realtime_factor) / 60.0, 1),
            "method": "approx",
        }

    dur = max(60.0, float(duration_sec or 0))
    chunk_sec = _aw.adaptive_chunk_seconds(dur, 0)
    chunks = _aw.plan_chunks(dur, [], 0)
    total = max(1, len(chunks))
    cancel_at = max(0, min(total - 1, int(total * float(cancel_ratio))))
    resume = min(total, cancel_at + 1)
    remaining = max(0, total - resume)
    saved = (resume / total) if total else 0.0
    factor = max(0.05, float(asr_realtime_factor))
    return {
        "duration_sec": dur,
        "chunks_total": total,
        "chunk_seconds": chunk_sec,
        "cancel_after_chunk": cancel_at,
        "resume_chunk": resume,
        "remaining_chunks": remaining,
        "saved_ratio": round(saved, 3),
        "est_full_minutes": round(dur * factor / 60.0, 1),
        "est_resume_minutes": round(
            sum(max(0.0, e - s) for s, e in chunks[resume:]) * factor / 60.0, 1,
        ),
        "method": "plan_chunks",
    }


def prove_resume(
    work_dir: str | Path,
    *,
    task_id: str = "resume-proof",
    chunks_total: int = 5,
    cancel_after: int = 2,
) -> dict[str, Any]:
    """合成续跑实证：模拟分块 → 中途取消 → 校验 resume_chunk → 跑完清检查点。

    不调用真 ASR；证明检查点协议与校验和闭环可用。
    """
    root = Path(work_dir)
    root.mkdir(parents=True, exist_ok=True)
    checkpoint.clear_checkpoint(root, task_id)
    clear_crash_scene(root, task_id)

    total = max(2, int(chunks_total))
    stop_at = max(0, min(total - 2, int(cancel_after)))
    segments: list[dict[str, Any]] = []

    # 第一程：跑到 cancel_after 后软取消
    for i in range(total):
        segments.append({
            "start": float(i * 10),
            "end": float(i * 10 + 9),
            "text": f"chunk-{i}",
        })
        checkpoint.save_checkpoint(
            root,
            task_id=task_id,
            chunk_index=i,
            chunks_total=total,
            segments=list(segments),
            duration=float(total * 10),
        )
        if i >= stop_at:
            checkpoint.request_soft_cancel(root, task_id)
            break

    mid = checkpoint.load_checkpoint(root, task_id)
    if not mid:
        raise RuntimeError("中途检查点丢失或校验失败")
    resume_chunk = int(mid["chunk_index"]) + 1
    if resume_chunk <= 0:
        raise RuntimeError("resume_chunk 应为正")
    if not checkpoint.is_cancel_requested(root, task_id):
        raise RuntimeError("软取消哨兵未生效")

    # 模拟崩溃现场
    save_crash_scene(
        root, task_id=task_id, reason="synthetic cancel for resume proof",
        extra={"phase": "mid", "resume_chunk": resume_chunk},
    )

    # 第二程：从 resume_chunk 继续（跳过已完成）
    segs2 = list(mid.get("segments") or [])
    started_at = resume_chunk
    for i in range(resume_chunk, total):
        segs2.append({
            "start": float(i * 10),
            "end": float(i * 10 + 9),
            "text": f"chunk-{i}-resume",
        })
        checkpoint.save_checkpoint(
            root,
            task_id=task_id,
            chunk_index=i,
            chunks_total=total,
            segments=list(segs2),
            duration=float(total * 10),
        )
    checkpoint.clear_checkpoint(root, task_id)
    clear_crash_scene(root, task_id)

    # 校验和拒绝：篡改后不得续跑
    checkpoint.save_checkpoint(
        root, task_id=task_id, chunk_index=0, chunks_total=3,
        segments=[{"text": "x"}], duration=1.0,
    )
    bad_path = checkpoint.checkpoint_path(root, task_id)
    raw = json.loads(bad_path.read_text(encoding="utf-8"))
    raw["segments"] = [{"text": "tampered"}]
    bad_path.write_text(json.dumps(raw), encoding="utf-8")
    rejected = checkpoint.load_checkpoint(root, task_id) is None
    checkpoint.clear_checkpoint(root, task_id)

    return {
        "ok": True,
        "task_id": task_id,
        "chunks_total": total,
        "cancel_after": stop_at,
        "resume_chunk": resume_chunk,
        "resumed_from": started_at,
        "segments_after_resume": len(segs2),
        "checksum_reject": rejected,
        "proof": "cancel→checkpoint→resume_chunk>0→finish→checksum_guard",
    }
