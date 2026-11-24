# Copyright (c) 2026 Hyht-Team
# SPDX-License-Identifier: MIT
# See LICENSE (MIT) and NOTICE.md (third-party components).
"""ASR 常驻进程池 —— 复用已加载的 Whisper 权重，避免每任务冷启动 5–8 s。

取消语义（v6 P0）：
  * 只有 ``active_task_id`` 持有者被取消时才 kill 进程；
  * 排队任务只置取消标记，拿到槽位后立即退出，不误杀别人；
  * ``run_job`` 带事件心跳：120s 无 progress/stage 即判卡死。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

APP_DIR = Path(__file__).resolve().parent.parent
ASR_WORKER = APP_DIR / "tools" / "asr_worker.py"

OnEvent = Callable[[dict], Optional[str]]
HEARTBEAT_SECONDS = 120.0
# 历史实测实时系数（转写耗时 / 音频时长）；用于排队 ETA
REALTIME_FACTOR = 0.44


class JobCancelled(Exception):
    """任务在排队或运行中被取消。"""


class WorkerHeartbeatError(RuntimeError):
    """120s 无事件：已杀进程并标记需重启；上层应写入 degraded。"""

    def __init__(self, message: str, *, degraded: str = "") -> None:
        super().__init__(message)
        self.degraded = degraded or message


class AsrWorkerPool:
    """单槽常驻 worker（模型常驻内存；任务串行）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._job_lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._ready = False
        self._stderr_tail: list[str] = []
        self._active_task_id: Optional[str] = None
        self._active_duration: float = 0.0  # 当前持有者音频时长（秒），供 ETA
        self._active_work_dir: str = ""
        self._active_ckpt_dir: str = ""
        self._active_ckpt_task_id: str = ""
        self._waiting: list[str] = []  # 排队 task_id 顺序
        self._cancel_flags: dict[str, threading.Event] = {}
        self._line_q: queue.Queue = queue.Queue()
        self._reader_stop = threading.Event()
        self._reader_thread: Optional[threading.Thread] = None
        self._python: Optional[Path] = None  # 供心跳后自动 ensure 重启

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def active_task_id(self) -> Optional[str]:
        with self._lock:
            return self._active_task_id

    def queue_snapshot(self, task_id: str = "") -> dict[str, Any]:
        """排队位次与预计等待（秒）。position=0 表示正在跑或即将开跑。"""
        with self._lock:
            waiting = list(self._waiting)
            active = self._active_task_id
            active_dur = float(self._active_duration or 0.0)
        # position：0=正在跑/未排队；1=下一位；2+=更后
        position = 0
        if task_id and active and task_id == active:
            position = 0
        elif task_id and task_id in waiting:
            position = waiting.index(task_id) + 1
        # ETA：当前持有者按音频时长×0.44；再往后每位按 60s 音频估
        if position <= 0:
            eta = 0.0
        else:
            eta = (active_dur if active else 0.0) * REALTIME_FACTOR
            if position > 1:
                eta += (position - 1) * 60.0 * REALTIME_FACTOR
        return {
            "position": position,
            "waiting": len(waiting),
            "active_task_id": active,
            "eta_seconds": round(eta, 1),
            "realtime_factor": REALTIME_FACTOR,
        }

    def cancel_flag(self, task_id: str) -> threading.Event:
        with self._lock:
            ev = self._cancel_flags.get(task_id)
            if ev is None:
                ev = threading.Event()
                self._cancel_flags[task_id] = ev
            return ev

    def drain_pending(self) -> int:
        """丢弃队列里上一单残留的事件，返回丢弃条数。

        一个任务被取消时，``run_job`` 读到 ``cancelled`` / ``job_end`` 就抛出，
        worker 紧接着补发的 ``job_end``、以及还没读完的 ``segment`` 会留在
        共享队列里。下一个任务若不清空就直接读，会**立刻**收到上一单的
        ``job_end{cancelled:true}`` —— 表现是「新任务一启动就说已取消」，
        更糟的是可能把上一个任务的分段记到自己头上。

        EOF 哨兵（``None``）要保留，它代表 worker 进程已经没了。
        """
        dropped = 0
        while True:
            try:
                item = self._line_q.get_nowait()
            except queue.Empty:
                break
            if item is None:
                self._line_q.put(None)
                break
            dropped += 1
        return dropped

    def request_cancel(self, task_id: str, *, work_dir: str = "") -> str:
        """取消任务。优先协议级软取消（写哨兵，等 chunk 边界）；超时再 kill。

        返回 ``soft`` / ``marked`` / ``killed`` / ``noop``。
        """
        with self._lock:
            flag = self._cancel_flags.setdefault(task_id, threading.Event())
            flag.set()
            if self._active_task_id == task_id:
                # 软取消：写哨兵到持久 ckpt_dir（保留热模型 + 检查点）
                ckpt = (
                    work_dir
                    or getattr(self, "_active_ckpt_dir", "")
                    or getattr(self, "_active_work_dir", "")
                    or ""
                ).strip()
                ckpt_tid = (
                    getattr(self, "_active_ckpt_task_id", "") or task_id
                ).strip() or task_id
                if ckpt:
                    try:
                        from fusion import checkpoint as _ckpt
                        _ckpt.request_soft_cancel(ckpt, ckpt_tid)
                    except Exception:
                        pass
                # 也尝试向 stdin 发 cancel（若 worker 在读下一行；运行中可能阻塞于 stdout）
                proc = self._proc
                if proc is not None and proc.poll() is None and proc.stdin:
                    try:
                        proc.stdin.write(json.dumps({
                            "cmd": "cancel", "task_id": ckpt_tid,
                            "ckpt_dir": ckpt, "work_dir": ckpt,
                        }, ensure_ascii=False) + "\n")
                        proc.stdin.flush()
                    except OSError:
                        pass
                return "soft"
            return "marked"

    def clear_cancel(self, task_id: str) -> None:
        with self._lock:
            self._cancel_flags.pop(task_id, None)

    def ensure(self, python: Path) -> bool:
        with self._lock:
            self._python = Path(python)
            if self.alive and self._ready:
                return True
            self._shutdown_unlocked()
            return self._spawn_unlocked(python)

    def _spawn_unlocked(self, python: Path) -> bool:
        if not ASR_WORKER.is_file() or not python.exists():
            return False
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        env.pop("PYTHONPATH", None)
        try:
            proc = subprocess.Popen(
                [str(python), str(ASR_WORKER), "--serve"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
                env=env,
                cwd=str(APP_DIR),
            )
        except OSError:
            return False
        self._proc = proc
        self._ready = False
        self._stderr_tail = []
        self._reader_stop.clear()
        self._line_q = queue.Queue()

        def drain_err() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                self._stderr_tail.append(line.rstrip())
                # D16：环形缓冲最近约 200 行
                if len(self._stderr_tail) > 200:
                    del self._stderr_tail[:100]

        def drain_out() -> None:
            assert proc.stdout is not None
            try:
                for line in proc.stdout:
                    if self._reader_stop.is_set():
                        break
                    self._line_q.put(line)
            finally:
                self._line_q.put(None)  # EOF sentinel

        threading.Thread(target=drain_err, daemon=True, name="asr-pool-stderr").start()
        self._reader_thread = threading.Thread(
            target=drain_out, daemon=True, name="asr-pool-stdout")
        self._reader_thread.start()

        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                return False
            try:
                line = self._line_q.get(timeout=0.5)
            except queue.Empty:
                continue
            if line is None:
                return False
            text = line.strip()
            if not text.startswith("{"):
                continue
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "ready":
                self._ready = True
                return True
        self._shutdown_unlocked()
        return False

    def _read_event(self, *, timeout: float) -> Optional[dict[str, Any]]:
        """读一条 JSON 事件；超时返回 None；EOF 抛 RuntimeError。"""
        try:
            line = self._line_q.get(timeout=timeout)
        except queue.Empty:
            return None
        if line is None:
            raise RuntimeError("ASR worker 输出中断")
        text = line.strip()
        if not text.startswith("{"):
            return {"type": "_skip"}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"type": "_skip"}

    def run_job(
        self,
        payload: dict[str, Any],
        on_event: OnEvent,
        *,
        task_id: str = "",
        on_queued: Callable[[dict[str, Any]], None] | None = None,
        audio_duration: float = 0.0,
    ) -> None:
        """同步跑一单。排队期间可被取消；运行中取消会 kill 本任务占用的进程。"""
        tid = task_id or str(payload.get("task_id") or "")
        flag = self.cancel_flag(tid) if tid else threading.Event()
        dur = float(audio_duration or payload.get("audio_duration") or 0.0)

        if tid:
            with self._lock:
                if tid not in self._waiting:
                    self._waiting.append(tid)

        # 排队等槽：可见 + 可取消
        while not self._job_lock.acquire(blocking=False):
            if flag.is_set():
                with self._lock:
                    if tid in self._waiting:
                        self._waiting.remove(tid)
                raise JobCancelled("排队中取消")
            if on_queued:
                try:
                    on_queued(self.queue_snapshot(tid))
                except Exception:
                    pass
            time.sleep(0.4)

        try:
            if tid:
                with self._lock:
                    if tid in self._waiting:
                        self._waiting.remove(tid)
            if flag.is_set():
                raise JobCancelled("拿到槽位前已取消")
            work_dir = str(payload.get("work_dir") or "")
            ckpt_dir = str(payload.get("ckpt_dir") or work_dir or "")
            # 检查点键：优先 payload.task_id（流水线 id），否则 ASR 临时 id
            ckpt_tid = str(payload.get("task_id") or tid or "").strip()
            with self._lock:
                self._active_task_id = tid or None
                self._active_duration = dur
                self._active_work_dir = work_dir
                self._active_ckpt_dir = ckpt_dir
                self._active_ckpt_task_id = ckpt_tid
            proc = self._proc
            if proc is None or proc.poll() is not None or not self._ready:
                # 心跳杀进程后自动拉起
                if self._python and self.ensure(self._python):
                    proc = self._proc
                if proc is None or proc.poll() is not None or not self._ready:
                    raise RuntimeError("ASR 常驻进程未就绪")
            assert proc.stdin is not None
            # 先把上一单的尾巴清掉，再发新任务：否则新任务会把残留的
            # job_end{cancelled:true} 当成自己的，一启动就「已取消」。
            self.drain_pending()
            job = dict(payload)
            job["cmd"] = "job"
            if ckpt_tid:
                job["task_id"] = ckpt_tid
            elif tid:
                job["task_id"] = tid
            try:
                proc.stdin.write(json.dumps(job, ensure_ascii=False) + "\n")
                proc.stdin.flush()
            except OSError as exc:
                self._ready = False
                raise RuntimeError(f"无法向 ASR worker 写任务：{exc}") from exc

            failure: Optional[str] = None
            saw_terminal = False
            last_beat = time.time()
            soft_since: Optional[float] = None
            while True:
                if flag.is_set():
                    if soft_since is None:
                        soft_since = time.time()
                        if ckpt_tid:
                            try:
                                from fusion import checkpoint as _ckpt
                                _ckpt.request_soft_cancel(ckpt_dir or work_dir, ckpt_tid)
                            except Exception:
                                pass
                    # 给当前 chunk 最多 90s 写完；超时再 hard kill
                    elif time.time() - soft_since > 90:
                        with self._lock:
                            self._kill_proc_unlocked()
                        raise JobCancelled("运行中取消（强制）")
                if proc.poll() is not None:
                    if flag.is_set():
                        raise JobCancelled("运行中取消")
                    detail = "\n".join(self._stderr_tail[-6:]).strip()
                    raise RuntimeError(detail or "ASR 常驻进程意外退出")
                event = self._read_event(timeout=1.0)
                if event is None:
                    if time.time() - last_beat > HEARTBEAT_SECONDS:
                        reason = (
                            f"ASR worker 心跳超时（>{int(HEARTBEAT_SECONDS)}s 无事件），"
                            "已终止并准备重启"
                        )
                        with self._lock:
                            self._kill_proc_unlocked()
                        # 尝试立刻拉起，供下一单或 oneshot 前的 ensure
                        if self._python:
                            try:
                                self.ensure(self._python)
                            except Exception:
                                pass
                        raise WorkerHeartbeatError(reason, degraded=reason)
                    continue
                if event.get("type") == "_skip":
                    continue
                last_beat = time.time()
                kind = event.get("type")
                if kind == "job_end":
                    if flag.is_set() or event.get("cancelled"):
                        raise JobCancelled("运行中取消（检查点已保存）")
                    break
                if kind == "cancelled":
                    raise JobCancelled(str(event.get("message") or "运行中取消（检查点已保存）"))
                if kind in ("done", "error"):
                    saw_terminal = True
                if kind == "info":
                    try:
                        self._active_duration = float(
                            event.get("duration") or self._active_duration or 0
                        )
                    except (TypeError, ValueError):
                        pass
                msg = on_event(event)
                if msg:
                    failure = msg
                    continue
            if failure:
                raise RuntimeError(failure)
            if not saw_terminal:
                raise RuntimeError("ASR worker 未返回 done/error")
        finally:
            with self._lock:
                if self._active_task_id == tid:
                    self._active_task_id = None
                    self._active_duration = 0.0
                if tid in self._waiting:
                    self._waiting.remove(tid)
            # 取消/异常退出时这一单的尾巴还没读完，留在这里会毒害下一单
            self.drain_pending()
            self._job_lock.release()

    def kill(self) -> None:
        with self._lock:
            self._shutdown_unlocked()

    def _kill_proc_unlocked(self) -> None:
        proc = self._proc
        self._proc = None
        self._ready = False
        self._active_task_id = None
        self._reader_stop.set()
        if proc is None:
            return
        try:
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        except Exception:
            pass

    def _shutdown_unlocked(self) -> None:
        proc = self._proc
        self._proc = None
        self._ready = False
        self._active_task_id = None
        self._reader_stop.set()
        if proc is None:
            return
        try:
            if proc.poll() is None and proc.stdin:
                try:
                    proc.stdin.write(json.dumps({"cmd": "shutdown"}) + "\n")
                    proc.stdin.flush()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        except Exception:
            pass


_POOL: Optional[AsrWorkerPool] = None
_POOL_LOCK = threading.Lock()


def get_pool() -> AsrWorkerPool:
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = AsrWorkerPool()
        return _POOL


def pool_enabled() -> bool:
    raw = (os.environ.get("AVD_ASR_POOL") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")
