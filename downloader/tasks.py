"""下载任务管理：状态机 + 事件广播（供 SSE 推送）。

每个任务跑在独立线程里，进度事件通过 ``queue.Queue`` 广播给所有订阅者
（浏览器端就是一条 EventSource 连接）。
"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from .config import config
from .engine import DownloadCancelled, download, friendly_error

try:
    from fusion.task_store import TERMINAL_STAGES as _TS, TaskRegistry
    from fusion import persist as _persist
except ImportError:  # 极端：仅跑 downloader 子包时
    TERMINAL_STAGES = ("done", "error", "cancelled")
    TaskRegistry = None  # type: ignore
    _persist = None
else:
    TERMINAL_STAGES = tuple(_TS)


def _persist_terminal(kind: str, snapshot: dict) -> None:
    if _persist is not None:
        _persist.save_task(kind, snapshot)


@dataclass
class Task:
    """单个下载任务的状态快照。"""

    id: str
    url: str
    title: str = ""
    thumbnail: str = ""
    extractor: str = ""
    height: Optional[int] = None
    audio_only: bool = False
    format_id: str = ""
    audio_format_id: str = ""
    resolution: str = ""
    tier: int = 0
    label: str = ""
    is_live: bool = False
    live_record_seconds: int = 0

    stage: str = "queued"          # queued | downloading | postprocess | done | error | cancelled
    percent: float = 0.0
    stream_index: int = 1
    stream_count: int = 1
    downloaded_bytes: int = 0
    total_bytes: int = 0
    speed: Optional[float] = None
    eta: Optional[int] = None
    # 平滑用：不对前端暴露
    _speed_ema: Optional[float] = field(default=None, repr=False)

    filename: str = ""
    filepath: str = ""
    message: str = ""
    error: str = ""

    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("_speed_ema", None)
        return data


class TaskManager:
    """下载任务管理：业务逻辑 + 统一 TaskRegistry 总线/持久化。"""

    def __init__(self) -> None:
        self._cancels: dict[str, threading.Event] = {}
        if TaskRegistry is not None:
            self._reg: Any = TaskRegistry(
                kind="download",
                to_dict=lambda t: t.to_dict(),
                on_terminal=_persist_terminal,
            )
            self._tasks = self._reg._tasks
            self._lock = self._reg.lock
            self._restore()
        else:
            self._reg = None
            self._tasks = {}
            self._lock = threading.RLock()
            self._subscribers: dict[str, list[queue.Queue]] = {}

    def _restore(self) -> None:
        if _persist is None:
            return
        for row in _persist.load_tasks("download", limit=100):
            tid = str(row.get("id") or "")
            if not tid or tid in self._tasks:
                continue
            task = Task(
                id=tid,
                url=str(row.get("url") or ""),
                title=str(row.get("title") or ""),
                stage=str(row.get("stage") or "done"),
                percent=float(row.get("percent") or 100.0),
                message=str(row.get("message") or ""),
                filepath=str(row.get("filepath") or ""),
                filename=str(row.get("filename") or ""),
                error=str(row.get("error") or ""),
                created_at=float(row.get("created_at") or time.time()),
                finished_at=row.get("finished_at"),
            )
            self._tasks[tid] = task

    # ------------------------------------------------------------------ 查询

    def list_tasks(self) -> list[dict]:
        if self._reg is not None:
            return self._reg.list_dicts()
        with self._lock:
            tasks = sorted(self._tasks.values(), key=lambda t: t.created_at, reverse=True)
            return [t.to_dict() for t in tasks]

    def get_task(self, task_id: str) -> Optional[dict]:
        if self._reg is not None:
            return self._reg.get_dict(task_id)
        with self._lock:
            task = self._tasks.get(task_id)
            return task.to_dict() if task else None

    # ------------------------------------------------------------------ 订阅

    def subscribe(self, task_id: str) -> queue.Queue:
        """订阅某任务的事件流，立即收到一份当前快照。"""
        if self._reg is not None:
            return self._reg.subscribe(task_id)
        q: queue.Queue = queue.Queue(maxsize=512)
        with self._lock:
            self._subscribers.setdefault(task_id, []).append(q)
            task = self._tasks.get(task_id)
        if task is not None:
            try:
                q.put_nowait({"type": "task", "task": task.to_dict()})
            except queue.Full:
                pass
        return q

    def unsubscribe(self, task_id: str, q: queue.Queue) -> None:
        if self._reg is not None:
            self._reg.unsubscribe(task_id, q)
            return
        with self._lock:
            subs = self._subscribers.get(task_id)
            if subs and q in subs:
                subs.remove(q)
            if subs is not None and not subs:
                self._subscribers.pop(task_id, None)

    def _broadcast(self, task_id: str, event: dict) -> None:
        if self._reg is not None:
            self._reg.broadcast(task_id, event)
            return
        with self._lock:
            subs = list(self._subscribers.get(task_id, ()))
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    # ------------------------------------------------------------------ 控制

    def create_task(
        self,
        url: str,
        *,
        height: Optional[int] = None,
        audio_only: bool = False,
        format_id: str = "",
        audio_format_id: str = "",
        resolution: str = "",
        tier: int = 0,
        stream_count: int = 1,
        title: str = "",
        thumbnail: str = "",
        extractor: str = "",
        label: str = "",
        is_live: bool = False,
        live_record_seconds: int = 0,
    ) -> dict:
        task = Task(
            id=uuid.uuid4().hex[:12],
            url=url,
            height=height,
            audio_only=audio_only,
            format_id=format_id,
            audio_format_id=audio_format_id,
            resolution=resolution,
            tier=int(tier or 0),
            stream_count=max(1, int(stream_count or 1)),
            title=title,
            thumbnail=thumbnail,
            extractor=extractor,
            label=label,
            is_live=bool(is_live),
            live_record_seconds=int(live_record_seconds or 0),
        )
        cancel_event = threading.Event()
        with self._lock:
            self._tasks[task.id] = task
            self._cancels[task.id] = cancel_event

        worker = threading.Thread(
            target=self._run,
            args=(task, cancel_event, config.as_dict()),
            name=f"download-{task.id}",
            daemon=True,
        )
        worker.start()
        return task.to_dict()

    def cancel_task(self, task_id: str) -> bool:
        with self._lock:
            event = self._cancels.get(task_id)
            task = self._tasks.get(task_id)
        if not event or not task or task.stage in TERMINAL_STAGES:
            return False
        event.set()
        return True

    def clear_finished(self) -> int:
        if self._reg is not None:
            n = self._reg.clear_finished()
            with self._lock:
                for tid in list(self._cancels):
                    if tid not in self._tasks:
                        self._cancels.pop(tid, None)
            return n
        with self._lock:
            done = [tid for tid, t in self._tasks.items() if t.stage in TERMINAL_STAGES]
            for tid in done:
                self._tasks.pop(tid, None)
                self._cancels.pop(tid, None)
        return len(done)

    # ------------------------------------------------------------------ 内部

    def _patch(self, task: Task, **fields) -> None:
        if self._reg is not None:
            self._reg.patch(task, **fields)
            return
        with self._lock:
            for key, value in fields.items():
                if hasattr(task, key):
                    setattr(task, key, value)
            snapshot = task.to_dict()
        self._broadcast(task.id, {"type": "task", "task": snapshot})

    def _on_progress(self, task: Task, event: dict) -> None:
        stage = event.get("stage")
        with self._lock:
            fallback = (task.percent, task.stream_index,
                        task.stream_count, task.filename)
        fields: dict = {}

        if stage == "downloading":
            raw_speed = event.get("speed")
            raw_eta = event.get("eta")
            downloaded = int(event.get("downloaded_bytes") or 0)
            total = int(event.get("total_bytes") or 0)
            # EMA 平滑速度，再反推 ETA，减轻 yt-dlp 原值抖动
            smooth_speed = raw_speed
            smooth_eta = raw_eta
            try:
                spd = float(raw_speed) if raw_speed is not None else 0.0
            except (TypeError, ValueError):
                spd = 0.0
            if spd > 0:
                prev = task._speed_ema
                ema = spd if prev is None else (0.25 * spd + 0.75 * float(prev))
                fields_ema = {"_speed_ema": ema}
                smooth_speed = ema
                remain = max(0, total - downloaded) if total > downloaded else 0
                if ema > 1 and remain > 0:
                    smooth_eta = int(remain / ema)
            else:
                fields_ema = {}
            fields.update({
                "stage": "downloading",
                "percent": event.get("percent", fallback[0]),
                "stream_index": event.get("stream_index", fallback[1]),
                "stream_count": event.get("stream_count", fallback[2]),
                "downloaded_bytes": downloaded,
                "total_bytes": total,
                "speed": smooth_speed,
                "eta": smooth_eta,
                "filename": event.get("filename") or fallback[3],
                "message": "",
                **fields_ema,
            })
        elif stage == "postprocess":
            fields.update({"stage": "postprocess", "message": event.get("message") or "正在处理…"})
            if event.get("filename"):
                fields["filename"] = event["filename"]
        elif stage == "error":
            fields.update({"stage": "error", "message": event.get("message") or "下载失败"})
        else:
            # 注意：引擎不再广播 done —— 终态统一由 _run() 在拿到最终文件路径后判定
            return

        self._patch(task, **fields)

    def _run(self, task: Task, cancel_event: threading.Event, cfg: dict) -> None:
        try:
            self._patch(task, stage="downloading", message="正在获取视频流…", percent=0.0)

            result = download(
                url=task.url,
                option={
                    "height": task.height,
                    "audio_only": task.audio_only,
                    "format_id": task.format_id,
                    "audio_format_id": task.audio_format_id,
                    "tier": task.tier,
                    "is_live": task.is_live,
                    "live_record_seconds": task.live_record_seconds,
                },
                cfg=cfg,
                on_progress=lambda event: self._on_progress(task, event),
                on_log=lambda message: self._broadcast(task.id, {"type": "log", "message": message}),
                cancel_event=cancel_event,
            )

            filepath = result.get("filepath") or ""
            filename = result.get("filename") or ""

            # 合并后的真实体积只有落盘才知道；用它覆盖「最后一条流」的字节数
            final_size = 0
            if filepath:
                try:
                    final_size = Path(filepath).stat().st_size
                except OSError:
                    final_size = 0
            size_fields = {}
            if final_size > 0:
                size_fields = {"downloaded_bytes": final_size, "total_bytes": final_size}

            self._patch(
                task,
                stage="done",
                percent=100.0,
                message="文件已存在，未重复下载" if result.get("reused") else "下载完成",
                filepath=filepath,
                filename=filename,
                speed=None,
                eta=None,
                finished_at=time.time(),
                **size_fields,
            )
        except DownloadCancelled:
            self._patch(task, stage="cancelled", message="已取消", finished_at=time.time())
        except Exception as exc:  # noqa: BLE001 - 统一收敛为任务级错误
            self._patch(
                task,
                stage="error",
                message=friendly_error(exc),
                error=str(exc)[:600],
                finished_at=time.time(),
            )
        finally:
            with self._lock:
                self._cancels.pop(task.id, None)


manager = TaskManager()
