# Copyright (c) 2026 Hyht-Team
# SPDX-License-Identifier: MIT
# See LICENSE (MIT) and NOTICE.md (third-party components).
"""SQLite 任务快照持久化。

单文件、零运维：默认 ``outputs/avd_tasks.sqlite3``。
四套 Manager 终态统一写入；重启后可按 kind 回溯列表与产物路径。

第九轮修订（HIST-01）：支持 ``AVD_DB_PATH`` 指定库路径。此前测试与
``verify_*`` 脚本一律写生产库，导致 26/26 任务在 UI 里点开全空
（``unit-persist`` 之类测试行污染真实历史）。现在测试侧用临时库。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB = APP_DIR / "outputs" / "avd_tasks.sqlite3"

_LOCK = threading.RLock()
_CONN: sqlite3.Connection | None = None
_DB_PATH: Path | None = None
# D25：持久化不再静默失败。失败计数 + 最后一条错误由 /api/fusion/health 暴露，
# 前端与诊断包都能看到「历史列表可能不完整」，而不是假装一切正常。
_HEALTH: dict[str, Any] = {
    "ok": True, "failures": 0, "last_error": "", "last_error_at": None,
}


def health() -> dict[str, Any]:
    """当前持久化健康度（供健康检查与诊断包读取）。"""
    with _LOCK:
        return dict(_HEALTH)


def note_failure(exc: BaseException, *, op: str = "") -> None:
    """登记一次持久化失败：计数、保留最后错误、写日志。"""
    with _LOCK:
        _HEALTH["ok"] = False
        _HEALTH["failures"] = int(_HEALTH.get("failures") or 0) + 1
        prefix = f"{op}: " if op else ""
        _HEALTH["last_error"] = f"{prefix}{type(exc).__name__}: {exc}"[:300]
        _HEALTH["last_error_at"] = time.time()
    try:
        from . import avd_log
        avd_log.warning("persist degraded", op=op, error=str(exc))
    except Exception:  # noqa: BLE001 — 日志本身失败时不能再抛
        pass


def note_success() -> None:
    """一次成功写入即视为恢复；历史失败计数保留，便于诊断。"""
    with _LOCK:
        _HEALTH["ok"] = True


def configured_db_path() -> Path:
    """解析当前应使用的库路径（环境变量优先，便于测试隔离）。"""
    raw = (os.environ.get("AVD_DB_PATH") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return DEFAULT_DB


def _connect(path: Path | None = None) -> sqlite3.Connection:
    global _CONN, _DB_PATH
    with _LOCK:
        # 已有连接且未显式换库时复用（避免后台线程把单测库切回默认路径）
        if path is None and _CONN is not None and _DB_PATH is not None:
            return _CONN
        target = Path(path) if path is not None else (_DB_PATH or configured_db_path())
        if _CONN is not None and _DB_PATH == target:
            return _CONN
        if _CONN is not None:
            try:
                _CONN.close()
            except Exception:
                pass
            _CONN = None
        target.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(target), check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                kind TEXT NOT NULL,
                id TEXT NOT NULL,
                stage TEXT,
                created_at REAL,
                finished_at REAL,
                title TEXT,
                url TEXT,
                snapshot TEXT NOT NULL,
                PRIMARY KEY (kind, id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_kind_created "
            "ON tasks(kind, created_at DESC)"
        )
        conn.commit()
        _CONN = conn
        _DB_PATH = target
        return conn


def reset_for_tests(path: Path | None = None) -> None:
    """单测用：关闭并指向新库。"""
    global _CONN, _DB_PATH
    with _LOCK:
        if _CONN is not None:
            try:
                _CONN.close()
            except Exception:
                pass
        _CONN = None
        _DB_PATH = None
        if path is not None:
            _connect(path)


def save_task(kind: str, snapshot: dict[str, Any]) -> None:
    tid = str(snapshot.get("id") or "").strip()
    if not tid:
        return
    body = json.dumps(snapshot, ensure_ascii=False, default=str)
    try:
        conn = _connect()
        with _LOCK:
            conn.execute(
                """
                INSERT INTO tasks(kind, id, stage, created_at, finished_at, title, url, snapshot)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(kind, id) DO UPDATE SET
                    stage=excluded.stage,
                    finished_at=excluded.finished_at,
                    title=excluded.title,
                    url=excluded.url,
                    snapshot=excluded.snapshot
                """,
                (
                    kind,
                    tid,
                    str(snapshot.get("stage") or ""),
                    float(snapshot.get("created_at") or time.time()),
                    snapshot.get("finished_at"),
                    str(snapshot.get("title") or "")[:500],
                    str(snapshot.get("url") or "")[:800],
                    body,
                ),
            )
            conn.commit()
    except Exception as exc:
        note_failure(exc, op="save_task")
        raise
    note_success()


def load_tasks(kind: str, *, limit: int = 200) -> list[dict[str, Any]]:
    conn = _connect()
    with _LOCK:
        rows = conn.execute(
            "SELECT snapshot FROM tasks WHERE kind=? "
            "ORDER BY COALESCE(finished_at, created_at) DESC LIMIT ?",
            (kind, max(1, min(1000, int(limit)))),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            data = json.loads(row["snapshot"])
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def get_task(kind: str, task_id: str) -> dict[str, Any] | None:
    conn = _connect()
    with _LOCK:
        row = conn.execute(
            "SELECT snapshot FROM tasks WHERE kind=? AND id=?",
            (kind, task_id),
        ).fetchone()
    if not row:
        return None
    try:
        data = json.loads(row["snapshot"])
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def delete_kind(kind: str) -> int:
    conn = _connect()
    with _LOCK:
        cur = conn.execute("DELETE FROM tasks WHERE kind=?", (kind,))
        conn.commit()
        return int(cur.rowcount or 0)


def purge_older_than(seconds: float) -> int:
    cutoff = time.time() - max(0.0, float(seconds))
    conn = _connect()
    with _LOCK:
        cur = conn.execute(
            "DELETE FROM tasks WHERE COALESCE(finished_at, created_at) < ?",
            (cutoff,),
        )
        conn.commit()
        return int(cur.rowcount or 0)


def _dir_size_bytes(root: Path) -> int:
    total = 0
    if not root.is_dir():
        return 0
    for path in root.rglob("*"):
        if path.is_file():
            try:
                total += path.stat().st_size
            except OSError:
                pass
    return total


def trim_outputs(
    root: Path,
    *,
    max_bytes: int,
    media_first: bool = True,
) -> dict[str, Any]:
    """按上限裁剪产物目录：优先删媒体，保留文稿（md/srt/txt/json/docx/pdf）。"""
    if max_bytes <= 0 or not root.is_dir():
        return {"deleted": 0, "freed": 0, "bytes": _dir_size_bytes(root)}
    media_ext = {".mp4", ".m4a", ".wav", ".webm", ".mkv", ".mp3", ".jpg", ".png", ".jpeg"}
    keep_ext = {".md", ".srt", ".txt", ".json", ".docx", ".pdf", ".vtt"}
    files: list[tuple[float, int, Path, bool]] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        is_media = path.suffix.lower() in media_ext
        if path.suffix.lower() in keep_ext and not is_media:
            continue  # 文稿长期留
        files.append((st.st_mtime, st.st_size, path, is_media))
    if media_first:
        files.sort(key=lambda x: (0 if x[3] else 1, x[0]))  # 媒体优先、再按旧→新
    else:
        files.sort(key=lambda x: x[0])
    current = _dir_size_bytes(root)
    deleted = 0
    freed = 0
    for _mtime, size, path, _media in files:
        if current <= max_bytes:
            break
        try:
            path.unlink()
        except OSError:
            continue
        deleted += 1
        freed += size
        current = max(0, current - size)
    return {"deleted": deleted, "freed": freed, "bytes": current}


def retention_policy() -> dict[str, Any]:
    """读取配置中的保留策略（天数 / GB 上限）。"""
    days = 7
    max_gb = 5.0
    try:
        from downloader.config import config as _cfg
        days = int(_cfg.get("task_retain_days") or 7)
        max_gb = float(_cfg.get("outputs_max_gb") or 5)
    except Exception as exc:  # noqa: BLE001
        # 配置读取失败按默认保留策略执行，但留痕，便于排查「保留天数没生效」
        try:
            from . import avd_log
            avd_log.warning("retention config fallback", error=str(exc))
        except Exception:
            pass
    days = max(1, min(365, days))
    max_gb = max(0.0, min(500.0, max_gb))
    return {
        "task_retain_days": days,
        "outputs_max_gb": max_gb,
        "ttl_seconds": days * 24 * 3600,
        "max_bytes": int(max_gb * 1024 ** 3) if max_gb > 0 else 0,
    }


def apply_retention(*, outputs_root: Path | None = None) -> dict[str, Any]:
    """执行保留策略：清过期快照 + 按体积裁剪产物（媒体优先）。"""
    policy = retention_policy()
    purged = purge_older_than(float(policy["ttl_seconds"]))
    root = Path(outputs_root) if outputs_root else (APP_DIR / "outputs")
    trimmed = trim_outputs(root, max_bytes=int(policy["max_bytes"]))
    return {"purged_rows": purged, "trim": trimmed, "policy": policy}


def stats() -> dict[str, Any]:
    """库规模 + 健康度。库不可用时返回 degraded 结构而不是抛给健康检查。"""
    policy = retention_policy()
    try:
        conn = _connect()
        with _LOCK:
            rows = conn.execute(
                "SELECT kind, COUNT(*) AS n FROM tasks GROUP BY kind"
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]
    except Exception as exc:
        note_failure(exc, op="stats")
        return {
            "path": str(_DB_PATH or configured_db_path()),
            "total": 0,
            "by_kind": {},
            "outputs_bytes": 0,
            "policy": policy,
            "health": health(),
        }
    outputs = configured_db_path().parent
    return {
        "path": str(_DB_PATH or configured_db_path()),
        "total": int(total or 0),
        "by_kind": {str(r["kind"]): int(r["n"]) for r in rows},
        "outputs_bytes": _dir_size_bytes(outputs),
        "policy": policy,
        "health": health(),
    }
