"""文案提取：平台字幕抓取 + 音频抓取 + 语音转写任务管理。

取文案有两条路，能走第一条就不走第二条：

1. **平台字幕**（首选）—— 官方或人群校对过的文本，秒出、零误差，
   时间轴还精确。由 ``downloader/subtitles.py`` 负责。
2. **语音识别**（兜底）—— 没有字幕才需要。转写交给 ``tools/asr_worker.py``，
   跑在独立的 ``.venv-asr`` 环境里，通过 stdin / stdout 的 JSON 行协议通信。

这样主服务依旧只依赖 yt-dlp 一个第三方包，语音模型那一堆重依赖被完全隔离。
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import subtitles
from .config import config
from .engine import friendly_error, resolve_proxy

try:
    from fusion.task_store import TERMINAL_STAGES as _TS, TaskRegistry
    from fusion import persist as _persist
    TERMINAL_STAGES = tuple(_TS)
except ImportError:
    TaskRegistry = None  # type: ignore
    _persist = None
    TERMINAL_STAGES = ("done", "error", "cancelled")

APP_DIR = Path(__file__).resolve().parent.parent
ASR_WORKER = APP_DIR / "tools" / "asr_worker.py"
UPLOAD_DIR = APP_DIR / ".uploads"

# ASR 环境的解释器。允许用环境变量覆盖 —— 便于把重依赖装到别处（或做多引擎对照测试）。
_ENV_PYTHON = "VD_ASR_PYTHON"


def _python_in(venv: Path) -> Optional[Path]:
    """返回该虚拟环境的解释器路径（自动适配 Windows 的 Scripts 布局）。"""
    candidates = (
        venv / "bin" / "python",
        venv / "bin" / "python3",
        venv / "Scripts" / "python.exe",
    )
    return next((path for path in candidates if path.exists()), None)


def resolve_python() -> Optional[Path]:
    """定位承载识别引擎的解释器。"""
    override = (os.environ.get(_ENV_PYTHON) or "").strip()
    if override:
        path = Path(override).expanduser()
        return path if path.exists() else None
    return _python_in(APP_DIR / ".venv-asr")


ASR_PYTHON = resolve_python()

DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"
LANGUAGES = ("zh", "en", "auto")

# 文案来源：自动（字幕优先，无字幕则转写）| 仅用平台字幕 | 仅用语音识别
MODES = ("auto", "subtitle", "asr")
# 识别引擎：auto 按平台自动挑（Apple Silicon → MLX，其余 → Faster-Whisper）
ENGINES = ("auto", "mlx", "faster")
ENGINE_LABELS = {"mlx": "MLX 引擎", "faster": "Faster-Whisper 引擎"}

# 抓取音频阶段占总进度的比例，其余留给转写
FETCH_SHARE = 15.0


def _persist_terminal(kind: str, snapshot: dict) -> None:
    if _persist is not None:
        _persist.save_task(kind, snapshot)

# 各引擎对应的包目录名，用于「不启动子进程就能判断引擎是否装好」
_ENGINE_PACKAGES = {"mlx": "mlx_whisper", "faster": "faster_whisper"}


def is_available() -> bool:
    """语音识别环境是否就绪（缺失时前端会隐藏入口并给出安装提示）。"""
    python = resolve_python()
    return bool(python and python.exists() and ASR_WORKER.exists())


def detect_engines() -> dict:
    """探测识别环境里实际装好了哪些引擎。

    直接看 site-packages 里有没有对应的包，避免为了问一句
    「你支持哪个引擎」而付一次进程启动 + 模型导入的代价。
    """
    python = resolve_python()
    found = {name: False for name in _ENGINE_PACKAGES}
    if not python:
        return {"available": False, "engines": found, "python": "", "recommended": "auto"}

    # venv 根目录 → site-packages 候选位置
    roots = [python.parent.parent]
    search: list[Path] = []
    for root in roots:
        for pattern in ("lib/python*/site-packages", "Lib/site-packages"):
            search.extend(root.glob(pattern))

    for name, package in _ENGINE_PACKAGES.items():
        found[name] = any((path / package).exists() for path in search)

    if found["mlx"]:
        recommended = "mlx"
    elif found["faster"]:
        recommended = "faster"
    else:
        recommended = "auto"
    return {
        "available": True,
        "engines": found,
        "python": str(python),
        "recommended": recommended,
    }


@dataclass
class AsrTask:
    """单次文案提取任务的状态快照。"""

    id: str
    source: str
    is_url: bool = True
    is_upload: bool = False
    title: str = ""
    thumbnail: str = ""
    extractor: str = ""
    language: str = "zh"
    mode: str = "auto"            # auto（字幕优先）| subtitle（仅字幕）| asr（仅语音）
    engine: str = "auto"          # auto | mlx | faster（仅语音识别时有效）

    stage: str = "queued"    # queued|subtitle|fetching|decoding|analyzing|transcribing|done|error|cancelled
    percent: float = 0.0
    message: str = "准备中…"
    downloaded_bytes: int = 0
    total_bytes: int = 0
    speed: Optional[float] = None

    duration: float = 0.0
    segment_count: int = 0
    text: str = ""
    error: str = ""

    # 文案来源：让用户知道这份稿子有多可信
    source_kind: str = ""         # subtitle | asr
    subtitle_kind: str = ""       # manual（人工） | auto（平台自动生成）
    subtitle_lang: str = ""
    engine_used: str = ""         # 实际跑起来的识别引擎
    no_speech: bool = False       # DIAG-01：成功但 0 段（VAD 滤掉 / 无人声）
    degraded: list = field(default_factory=list)  # 降级/告警文案（心跳超时等）
    queue_position: int = 0
    queue_eta_seconds: float = 0.0

    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


class AsrManager:
    """转写任务管理：业务逻辑 + 统一 TaskRegistry 总线/持久化。"""

    def __init__(self) -> None:
        self._segments: dict[str, list[dict]] = {}
        self._procs: dict[str, Any] = {}
        if TaskRegistry is not None:
            self._reg: Any = TaskRegistry(
                kind="asr",
                to_dict=lambda t: t.to_dict(),
                queue_size=1024,
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
        for row in _persist.load_tasks("asr", limit=80):
            tid = str(row.get("id") or "")
            if not tid or tid in self._tasks:
                continue
            task = AsrTask(
                id=tid,
                source=str(row.get("source") or ""),
                title=str(row.get("title") or ""),
                stage=str(row.get("stage") or "done"),
                percent=float(row.get("percent") or 100.0),
                message=str(row.get("message") or ""),
                text=str(row.get("text") or ""),
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

    def get_segments(self, task_id: str) -> list[dict]:
        with self._lock:
            return list(self._segments.get(task_id, ()))

    # ------------------------------------------------------------------ 订阅

    def subscribe(self, task_id: str) -> queue.Queue:
        with self._lock:
            task = self._tasks.get(task_id)
            snapshot = task.to_dict() if task else None
            segments = list(self._segments.get(task_id, ())) if task else []
        prelude: list[dict] = []
        if snapshot is not None and snapshot.get("stage") == "done" and segments:
            prelude.append({
                "type": "result",
                "text": snapshot.get("text", ""),
                "segments": segments,
            })
        if self._reg is not None:
            return self._reg.bus.subscribe(
                task_id,
                snapshot=snapshot,
                prelude=prelude,
            )
        q: queue.Queue = queue.Queue(maxsize=1024)
        for event in prelude:
            self._enqueue(q, event)
        if snapshot is not None:
            self._enqueue(q, {"type": "task", "task": snapshot})
        with self._lock:
            self._subscribers.setdefault(task_id, []).append(q)
        return q

    @staticmethod
    def _enqueue(q: queue.Queue, event: dict) -> None:
        try:
            q.put_nowait(event)
        except queue.Full:
            pass

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

    def _patch(self, task: AsrTask, **fields) -> None:
        # P1：终态不可逆 —— 已 cancelled/done/error 后丢弃非终态 stage 事件
        if "stage" in fields:
            nxt = str(fields["stage"] or "")
            cur = str(task.stage or "")
            if cur in TERMINAL_STAGES and nxt not in TERMINAL_STAGES:
                fields = {k: v for k, v in fields.items() if k != "stage"}
                if not fields:
                    return
        if self._reg is not None:
            self._reg.patch(task, **fields)
            return
        with self._lock:
            for key, value in fields.items():
                if hasattr(task, key):
                    setattr(task, key, value)
            snapshot = task.to_dict()
        self._broadcast(task.id, {"type": "task", "task": snapshot})

    # ------------------------------------------------------------------ 控制

    def create_task(
        self,
        source: str,
        *,
        is_url: bool = True,
        is_upload: bool = False,
        title: str = "",
        thumbnail: str = "",
        extractor: str = "",
        language: str = "zh",
        mode: str = "auto",
        engine: str = "auto",
        ckpt_dir: str = "",
        resume_task_id: str = "",
    ) -> dict:
        # UX-01：auto 模式不在入口硬拦 —— 先尝试平台字幕；真正要转写前再校验环境。
        # 仅 asr 模式（确定要跑语音）才提前拦，避免白跑。
        # 本地文件没有「平台字幕」这回事，强制走语音识别
        if not is_url:
            mode = "asr"
        if mode == "asr" and not is_available():
            raise RuntimeError(
                "语音识别环境未就绪，请先运行一次安装脚本；"
                "若视频自带字幕，可把来源改为「仅用平台字幕」"
            )

        task = AsrTask(
            id=uuid.uuid4().hex[:12],
            source=source,
            is_url=is_url,
            is_upload=is_upload,
            title=title,
            thumbnail=thumbnail,
            extractor=extractor,
            language=language if language in LANGUAGES else "zh",
            mode=mode if mode in MODES else "auto",
            engine=engine if engine in ENGINES else "auto",
            # MISC-01：本地/上传任务一开始就标 source_kind，不必等 _fetch_audio
            source_kind="asr" if not is_url else "",
        )
        # R4：持久检查点目录 + 流水线 task_id（续跑键），挂在实例上供 worker 读取
        setattr(task, "ckpt_dir", str(ckpt_dir or "").strip())
        setattr(task, "resume_task_id", str(resume_task_id or "").strip())
        with self._lock:
            self._tasks[task.id] = task
            self._segments[task.id] = []

        worker = threading.Thread(target=self._run, args=(task,), name=f"asr-{task.id}", daemon=True)
        worker.start()
        return task.to_dict()

    def cancel_task(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            proc = self._procs.get(task_id)
        if not task or task.stage in TERMINAL_STAGES:
            return False
        # 先置终态，阻止后续 stage 事件把状态「复活」
        self._patch(task, stage="cancelled", message="已取消", finished_at=time.time())
        # 常驻池：只取消本任务；仅持有者才会被 kill
        try:
            from . import asr_pool
            asr_pool.get_pool().request_cancel(task_id)
        except Exception:
            pass
        if isinstance(proc, subprocess.Popen) and proc.poll() is None:
            def _reap(child: subprocess.Popen) -> None:
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        child.kill()
                        child.wait(timeout=3)
                    except (OSError, subprocess.TimeoutExpired):
                        pass

            try:
                proc.terminate()
            except OSError:
                pass
            threading.Thread(target=_reap, args=(proc,), daemon=True,
                             name=f"avd-asr-reap-{task_id}").start()
        return True

    def clear_finished(self) -> int:
        with self._lock:
            done = [tid for tid, t in self._tasks.items() if t.stage in TERMINAL_STAGES]
            for tid in done:
                self._tasks.pop(tid, None)
                self._segments.pop(tid, None)
        return len(done)

    # ------------------------------------------------------------------ 执行

    def _run(self, task: AsrTask) -> None:
        work_dir = Path(tempfile.mkdtemp(prefix="vd-asr-"))
        try:
            from_subtitle = False
            if task.is_url and task.mode != "asr":
                from_subtitle = self._try_subtitles(task, work_dir)
                if not from_subtitle and task.mode == "subtitle":
                    raise RuntimeError("这个视频没有可用的平台字幕，请改用「自动」或「仅语音识别」")

            if not from_subtitle:
                # UX-01：字幕失败后、真正要转写前再校验环境
                if not is_available():
                    raise RuntimeError(
                        "平台字幕不可用，且语音识别环境未就绪。"
                        "请运行安装脚本，或将来源改为「仅用平台字幕」后重试有字幕的视频"
                    )
                if task.is_url:
                    media = self._fetch_audio(task, work_dir)
                else:
                    media = Path(task.source).expanduser()
                    if not media.exists():
                        raise RuntimeError("待处理的文件不存在或已被移动")
                    self._patch(
                        task, stage="decoding", percent=FETCH_SHARE,
                        message="准备音频…", source_kind="asr",
                    )

                self._run_worker(task, media, work_dir)

            if task.stage not in TERMINAL_STAGES:
                # 终态判定权收归任务层：确保前端收到 done 时数据已经完整
                with self._lock:
                    segments = list(self._segments.get(task.id, ()))
                    text = "".join(s["text"] for s in segments).strip()
                no_speech = (not from_subtitle) and len(segments) == 0
                msg = "字幕提取完成" if from_subtitle else (
                    "素材无人声或语音已被滤掉" if no_speech else "提取完成"
                )
                self._broadcast(task.id, {"type": "result", "text": text, "segments": segments,
                                          "no_speech": no_speech})
                self._patch(
                    task,
                    stage="done",
                    percent=100.0,
                    message=msg,
                    text=text,
                    segment_count=len(segments),
                    no_speech=no_speech,
                    speed=None,
                    finished_at=time.time(),
                )
        except Exception as exc:  # noqa: BLE001 - 统一收敛为任务级错误
            if task.stage not in TERMINAL_STAGES:
                self._patch(
                    task,
                    stage="error",
                    message=friendly_error(exc),
                    error=str(exc)[:600],
                    finished_at=time.time(),
                )
        finally:
            with self._lock:
                self._procs.pop(task.id, None)
            shutil.rmtree(work_dir, ignore_errors=True)
            if task.is_upload:
                try:
                    Path(task.source).unlink(missing_ok=True)
                except OSError:
                    pass

    # ------------------------------------------------------------------ 字幕优先

    def _try_subtitles(self, task: AsrTask, work_dir: Path) -> bool:
        """尝试用平台自带字幕完成任务。返回是否成功。

        字幕这条路失败**不报错** —— 没字幕是极常见的情况，
        该做的是安静地回落到语音识别，而不是拿一个红叉打扰用户。
        """
        if not config.get("prefer_subtitle", True):
            return False

        self._patch(task, stage="subtitle", percent=0.0, message="正在获取平台字幕…")
        cfg = config.as_dict()
        try:
            result = subtitles.fetch(task.source, cfg, task.language, work_dir / "subs")
        except Exception as exc:  # noqa: BLE001
            self._broadcast(task.id, {"type": "log", "message": f"字幕获取失败：{friendly_error(exc)}"})
            return False

        if result is None or not result.segments:
            message = (
                "该视频没有平台字幕，改用语音识别"
                if task.mode == "auto"
                else "该视频没有平台字幕"
            )
            self._broadcast(task.id, {"type": "log", "message": message})
            return False

        if task.stage in TERMINAL_STAGES:
            return False

        self._broadcast(task.id, {
            "type": "subtitle",
            "kind": result.kind,
            "language": result.language,
            "format": result.fmt,
            "count": len(result.segments),
            "message": "已获取平台字幕" if result.kind == "manual" else "已获取平台自动字幕",
        })

        with self._lock:
            bucket = self._segments.setdefault(task.id, [])
            for index, segment in enumerate(result.segments):
                bucket.append(segment)
                self._broadcast(task.id, {
                    "type": "segment",
                    "segment": segment,
                    "count": index + 1,
                })

        self._patch(
            task,
            stage="subtitle",
            percent=100.0,
            message=f"已取到 {len(result.segments)} 句字幕",
            segment_count=len(result.segments),
            source_kind="subtitle",
            subtitle_kind=result.kind,
            subtitle_lang=result.language,
        )
        return True

    # ------------------------------------------------------------------ 音频抓取

    def _fetch_audio(self, task: AsrTask, work_dir: Path) -> Path:
        import yt_dlp

        cfg = config.as_dict()
        self._patch(
            task,
            stage="fetching",
            percent=0.0,
            message="正在抓取音频…",
            source_kind="asr",
        )

        options: dict = {
            "format": "bestaudio/best",
            "outtmpl": str(work_dir / "media.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "noprogress": True,
            "socket_timeout": 30,
            "progress_hooks": [lambda d: self._on_fetch_progress(task, d)],
        }
        proxy = None
        try:
            proxy = resolve_proxy(task.source, cfg)
        except Exception:
            proxy = None
        if proxy:
            options["proxy"] = proxy
        cookies = (cfg.get("cookies_from_browser") or "").strip()
        if cookies:
            options["cookiesfrombrowser"] = (cookies,)

        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.extract_info(task.source, download=True)

        candidates = [p for p in work_dir.glob("media.*") if p.is_file()]
        if not candidates:
            raise RuntimeError("音频抓取失败，未生成本地文件")
        return max(candidates, key=lambda p: p.stat().st_size)

    def _on_fetch_progress(self, task: AsrTask, payload: dict) -> None:
        if task.stage in TERMINAL_STAGES:
            return
        status = payload.get("status")
        if status == "downloading":
            total = payload.get("total_bytes") or payload.get("total_bytes_estimate") or 0
            done = payload.get("downloaded_bytes") or 0
            ratio = (done / total) if total else 0.0
            self._patch(
                task,
                stage="fetching",
                percent=round(FETCH_SHARE * min(1.0, ratio), 2),
                downloaded_bytes=done,
                total_bytes=total,
                speed=payload.get("speed"),
                message="正在抓取音频…",
            )
        elif status == "finished":
            self._patch(
                task,
                percent=FETCH_SHARE,
                speed=None,
                message="音频抓取完成",
            )

    # ------------------------------------------------------------------ 转写子进程

    def _run_worker(self, task: AsrTask, media: Path, work_dir: Path) -> None:
        python = resolve_python()
        if python is None:
            raise RuntimeError("语音识别环境未就绪，请先运行一次安装脚本")

        try:
            from .config import config as _cfg
            hotwords = _cfg.hotword_list()
        except Exception:
            hotwords = []
        ckpt = str(getattr(task, "ckpt_dir", "") or "").strip()
        resume_tid = str(getattr(task, "resume_task_id", "") or "").strip() or task.id
        payload = {
            "source": str(media),
            "language": task.language,
            "engine": task.engine,
            "model": "",
            "work_dir": str(work_dir / "asr"),
            # R4：检查点写到持久目录，取消/崩溃后可续
            "ckpt_dir": ckpt or str(work_dir / "asr"),
            "task_id": resume_tid,
            # 约按音频 10% 一切，短素材也能看到进度（下限 15s，上限 300s）
            "chunk_seconds": 0,
            "initial_percent": FETCH_SHARE,
            "hotwords": hotwords,
        }

        # 优先常驻池（模型热复用）；失败则回退一次性进程
        from . import asr_pool
        if asr_pool.pool_enabled():
            pool = asr_pool.get_pool()
            # 清掉上次残留取消标记
            pool.clear_cancel(task.id)
            try:
                if not pool.ensure(python):
                    raise RuntimeError("ASR 常驻进程启动失败")

                def _on(ev: dict) -> Optional[str]:
                    if task.stage in TERMINAL_STAGES:
                        return "已取消"
                    return self._handle_event(task, ev)

                def _queued(snap: dict) -> None:
                    if task.stage in TERMINAL_STAGES:
                        return
                    pos = int(snap.get("position") or 0)
                    eta = float(snap.get("eta_seconds") or 0)
                    if pos <= 0:
                        msg = "转写排队中（串行池，请稍候）…"
                    else:
                        eta_txt = f"约 {int(eta)} 秒" if eta > 0 else "计算中"
                        msg = f"转写排队中 · 第 {pos} 位 · 预计等待 {eta_txt}"
                    self._patch(
                        task, stage="queued", percent=0.0, message=msg,
                        queue_position=pos, queue_eta_seconds=eta,
                    )

                pool.run_job(
                    payload, _on, task_id=task.id, on_queued=_queued,
                    audio_duration=float(task.duration or 0),
                )
            except asr_pool.JobCancelled:
                if task.stage not in TERMINAL_STAGES:
                    self._patch(
                        task, stage="cancelled", message="已取消",
                        finished_at=time.time(),
                    )
                return
            except asr_pool.WorkerHeartbeatError as hb_exc:
                if task.stage in TERMINAL_STAGES:
                    return
                note = getattr(hb_exc, "degraded", None) or str(hb_exc)
                deg = list(task.degraded or [])
                if note not in deg:
                    deg.append(note)
                try:
                    self._dump_asr_fail(task.id, pool)
                except Exception:
                    pass
                self._patch(task, degraded=deg, message=note)
                self._broadcast(task.id, {
                    "type": "log",
                    "message": f"{note}；将回退冷启动重试",
                })
                # 回退 oneshot（池已重启）；不直接失败
            except Exception as pool_exc:  # noqa: BLE001
                if task.stage in TERMINAL_STAGES:
                    return
                try:
                    self._dump_asr_fail(task.id, pool)
                except Exception:
                    pass
                try:
                    pool.kill()
                except Exception:
                    pass
                self._broadcast(task.id, {
                    "type": "log",
                    "message": f"常驻 ASR 池不可用，回退冷启动：{pool_exc}",
                })
            else:
                if task.stage in TERMINAL_STAGES:
                    return
                return
            finally:
                pool.clear_cancel(task.id)

        if task.stage in TERMINAL_STAGES:
            return
        self._run_worker_oneshot(task, python, payload)

    def _dump_asr_fail(self, task_id: str, pool: Any) -> None:
        """P2：失败时落完整 stderr 尾，便于复盘。"""
        import time as _time
        log_dir = APP_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"asr-fail-{task_id}.log"
        lines = list(getattr(pool, "_stderr_tail", []) or [])
        header = [
            f"task_id={task_id}",
            f"time={_time.strftime('%Y-%m-%dT%H:%M:%S')}",
            f"pool_ready={getattr(pool, '_ready', None)}",
            f"pool_alive={bool(getattr(pool, '_proc', None) and getattr(pool._proc, 'poll', lambda: 0)() is None)}",
        ]
        last_ev = getattr(pool, "_last_event", None)
        if last_ev is not None:
            header.append(f"last_event={last_ev!r}"[:500])
        if not lines:
            header.append("note=worker 未产生 stderr（环形缓冲为空）")
        body = "\n".join(header + ["--- stderr ---"] + lines) + "\n"
        path.write_text(body, encoding="utf-8")

    def _run_worker_oneshot(self, task: AsrTask, python: Path, payload: dict) -> None:
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        env.pop("PYTHONPATH", None)      # 防止主环境的包串进隔离环境

        proc = subprocess.Popen(
            [str(python), str(ASR_WORKER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=env,
            cwd=str(APP_DIR),
        )
        with self._lock:
            self._procs[task.id] = proc

        stderr_tail: list[str] = []

        def drain() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                stderr_tail.append(line.rstrip())
                if len(stderr_tail) > 80:
                    del stderr_tail[:40]

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()

        try:
            assert proc.stdin is not None
            proc.stdin.write(json.dumps(payload, ensure_ascii=False))
            proc.stdin.close()
        except OSError:
            pass

        failure: Optional[str] = None
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                message = self._handle_event(task, event)
                if message:
                    failure = message
                    break
        finally:
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            reader.join(timeout=2)
            with self._lock:
                self._procs.pop(task.id, None)

        if task.stage in TERMINAL_STAGES:
            return
        if failure:
            raise RuntimeError(failure)

        code = proc.returncode
        if code != 0:
            detail = "\n".join(stderr_tail[-6:]).strip()
            raise RuntimeError(detail or f"转写进程异常退出（退出码 {code}）")

    def _handle_event(self, task: AsrTask, event: dict) -> Optional[str]:
        """处理 worker 事件；返回非空字符串表示需要终止并报错。"""
        kind = event.get("type")

        if kind == "stage":
            stage = event.get("stage") or "transcribing"
            if stage == "log":
                # 引擎切换一类的提示，只当作日志播出去，不要改动任务阶段
                if event.get("message"):
                    self._broadcast(task.id, {"type": "log", "message": event["message"]})
                return None
            self._patch(
                task,
                stage=stage if stage in ("decode", "segment", "transcribe") else "transcribing",
                message=event.get("message") or "处理中…",
            )
            if event.get("message"):
                self._broadcast(task.id, {"type": "log", "message": event["message"]})
            return None

        if kind == "info":
            engine = str(event.get("engine") or "")
            label = str(event.get("engine_label") or ENGINE_LABELS.get(engine, ""))
            fields: dict = {}
            # 只在事件真的带了 duration 时才覆盖：续跑通知这类 info 没有时长，
            # 旧写法会把已经算好的 duration 冲成 0.0，ETA 与百分比从此失真。
            try:
                dur = float(event.get("duration") or 0.0)
            except (TypeError, ValueError):
                dur = 0.0
            if dur > 0:
                fields["duration"] = dur
            if engine:
                fields["engine_used"] = engine
            if fields:
                self._patch(task, **fields)
            if label:
                self._broadcast(task.id, {"type": "log", "message": f"识别引擎：{label}"})
            # 续跑通知要能让上层看见（UI 显示「已从 58s 续跑」）
            if event.get("resume_at") is not None and event.get("message"):
                self._patch(task, message=str(event["message"]))
                self._broadcast(task.id, {
                    "type": "log", "message": str(event["message"]),
                    "resume_at": event.get("resume_at"),
                    "resume_chunk": event.get("resume_chunk"),
                })
            return None

        if kind == "progress":
            msg = event.get("message") or task.message
            chunk = event.get("chunk")
            chunks = event.get("chunks")
            if chunk and chunks:
                msg = f"转写中 {chunk}/{chunks}"
            self._patch(
                task,
                percent=float(event.get("percent") or task.percent),
                message=msg,
            )
            return None

        if kind == "segment":
            segment = event.get("segment") or {}
            if not segment.get("text"):
                return None
            with self._lock:
                bucket = self._segments.setdefault(task.id, [])
                bucket.append(segment)
                count = len(bucket)
            self._broadcast(task.id, {"type": "segment", "segment": segment, "count": count})
            if count % 5 == 0 or count == 1:
                self._patch(task, segment_count=count, message=f"已识别 {count} 句…")
            return None

        if kind == "done":
            # 不在这里置终态 —— 交由 _run() 统一判定，保证 done 时数据完整
            fields: dict = {
                "segment_count": int(event.get("segment_count") or task.segment_count),
            }
            if event.get("no_speech"):
                fields["no_speech"] = True
            self._patch(task, **fields)
            return None

        if kind == "error":
            return str(event.get("message") or "转写失败")
        return None


manager = AsrManager()
