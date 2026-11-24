# Copyright (c) 2026 Hyht-Team
# SPDX-License-Identifier: MIT
# See LICENSE (MIT) and NOTICE.md (third-party components).
"""统一任务内核 —— 订阅总线 + 注册表 + 终态判定。

把原先 tasks / asr / polish / pipeline 四套同构的
``subscribe / unsubscribe / _broadcast / _patch`` 收敛到一处，
业务 Manager 只保留领域逻辑（下载、转写、整理、流水线）。
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable, Generic, Iterable, TypeVar

T = TypeVar("T")

TERMINAL_STAGES = frozenset({"done", "error", "cancelled"})


def is_terminal(stage: str | None, terminal: Iterable[str] = TERMINAL_STAGES) -> bool:
    return str(stage or "") in terminal


class EventBus:
    """按 task_id 分发事件；队列满时丢事件，不反压生产者。"""

    def __init__(self, *, queue_size: int = 512) -> None:
        self._queue_size = max(32, int(queue_size))
        self._subscribers: dict[str, list[queue.Queue]] = {}
        self._lock = threading.RLock()

    def subscribe(
        self,
        task_id: str,
        *,
        snapshot: dict[str, Any] | None = None,
        prelude: list[dict[str, Any]] | None = None,
    ) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=self._queue_size)
        with self._lock:
            self._subscribers.setdefault(task_id, []).append(q)
        for event in prelude or ():
            self._enqueue(q, event)
        if snapshot is not None:
            self._enqueue(q, {"type": "task", "task": snapshot})
        return q

    def unsubscribe(self, task_id: str, q: queue.Queue) -> None:
        with self._lock:
            subs = self._subscribers.get(task_id)
            if subs and q in subs:
                subs.remove(q)
            if subs is not None and not subs:
                self._subscribers.pop(task_id, None)

    def broadcast(self, task_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subscribers.get(task_id, ()))
        for q in subs:
            self._enqueue(q, event)

    @staticmethod
    def _enqueue(q: queue.Queue, event: dict[str, Any]) -> None:
        try:
            q.put_nowait(event)
        except queue.Full:
            pass


class TaskRegistry(Generic[T]):
    """线程安全任务表：patch 时广播快照，终态可回调持久化。"""

    def __init__(
        self,
        *,
        kind: str,
        to_dict: Callable[[T], dict[str, Any]],
        terminal: Iterable[str] = TERMINAL_STAGES,
        queue_size: int = 512,
        on_terminal: Callable[[str, dict[str, Any]], None] | None = None,
        id_of: Callable[[T], str] | None = None,
    ) -> None:
        self.kind = kind
        self._to_dict = to_dict
        self._terminal = frozenset(terminal)
        self._on_terminal = on_terminal
        self._id_of = id_of or (lambda t: str(getattr(t, "id")))
        self.bus = EventBus(queue_size=queue_size)
        self._tasks: dict[str, T] = {}
        self._lock = threading.RLock()

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def put(self, task: T) -> None:
        tid = self._id_of(task)
        with self._lock:
            self._tasks[tid] = task

    def pop(self, task_id: str) -> T | None:
        with self._lock:
            return self._tasks.pop(task_id, None)

    def get(self, task_id: str) -> T | None:
        with self._lock:
            return self._tasks.get(task_id)

    def get_dict(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return self._to_dict(task) if task else None

    def list_dicts(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = [self._to_dict(t) for t in self._tasks.values()]
        rows.sort(key=lambda r: float(r.get("created_at") or 0), reverse=True)
        return rows

    def items(self) -> list[tuple[str, T]]:
        with self._lock:
            return list(self._tasks.items())

    def values(self) -> list[T]:
        with self._lock:
            return list(self._tasks.values())

    def __contains__(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._tasks

    def __len__(self) -> int:
        with self._lock:
            return len(self._tasks)

    def is_terminal(self, stage: str | None) -> bool:
        return str(stage or "") in self._terminal

    def patch(self, task: T, **fields: Any) -> dict[str, Any]:
        """更新字段 → 广播 task 快照；若进入终态则触发持久化回调。

        若本 kind 在 ``StageMachine`` 中有定义，且 patch 含 stage，
        则校验转移合法性（非法仅打日志，不阻断业务）。
        终态不可逆：已 cancelled/done/error 时丢弃非终态 stage。
        """
        with self._lock:
            if "stage" in fields:
                fields = dict(fields)
                nxt = str(fields.get("stage") or "")
                cur = str(getattr(task, "stage", None) or "")
                if cur in self._terminal and nxt not in self._terminal:
                    fields.pop("stage", None)
                    if not fields:
                        return self._to_dict(task)
                elif "stage" in fields:
                    try:
                        from .machine import StageMachine, STAGES
                        if self.kind in STAGES:
                            sm = StageMachine(self.kind)
                            fields["stage"] = sm.advance(cur, nxt)
                    except Exception:
                        # StageError 或未安装：宽松放行
                        pass
            for key, value in fields.items():
                if hasattr(task, key):
                    setattr(task, key, value)
            snapshot = self._to_dict(task)
            stage = str(snapshot.get("stage") or "")
            tid = self._id_of(task)
        self.bus.broadcast(tid, {"type": "task", "task": snapshot})
        if stage in self._terminal and self._on_terminal is not None:
            try:
                self._on_terminal(self.kind, snapshot)
            except Exception:
                pass
        return snapshot

    def subscribe(self, task_id: str, *, prelude: list[dict[str, Any]] | None = None) -> queue.Queue:
        snap = self.get_dict(task_id)
        return self.bus.subscribe(task_id, snapshot=snap, prelude=prelude)

    def unsubscribe(self, task_id: str, q: queue.Queue) -> None:
        self.bus.unsubscribe(task_id, q)

    def broadcast(self, task_id: str, event: dict[str, Any]) -> None:
        self.bus.broadcast(task_id, event)

    def clear_finished(self) -> int:
        with self._lock:
            done = [
                tid for tid, task in self._tasks.items()
                if self.is_terminal(getattr(task, "stage", None))
            ]
            for tid in done:
                self._tasks.pop(tid, None)
        return len(done)

    def prune(self, *, max_tasks: int = 200, ttl_seconds: float = 0) -> int:
        """按数量 / TTL 裁剪终态任务。"""
        now = time.time()
        removed = 0
        with self._lock:
            if ttl_seconds > 0:
                stale = []
                for tid, task in self._tasks.items():
                    if not self.is_terminal(getattr(task, "stage", None)):
                        continue
                    finished = getattr(task, "finished_at", None) or getattr(task, "created_at", None)
                    try:
                        age = now - float(finished or 0)
                    except (TypeError, ValueError):
                        age = 0
                    if age > ttl_seconds:
                        stale.append(tid)
                for tid in stale:
                    self._tasks.pop(tid, None)
                    removed += 1
            if len(self._tasks) > max_tasks:
                ordered = sorted(
                    self._tasks.items(),
                    key=lambda kv: float(getattr(kv[1], "created_at", 0) or 0),
                )
                for tid, _ in ordered[: max(0, len(ordered) - max_tasks)]:
                    self._tasks.pop(tid, None)
                    removed += 1
        return removed
