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
from typing import Optional

from . import subtitles
from .config import config
from .engine import friendly_error, resolve_proxy

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

TERMINAL_STAGES = ("done", "error", "cancelled")

# 抓取音频阶段占总进度的比例，其余留给转写
FETCH_SHARE = 15.0

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

    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


class AsrManager:
    """线程安全的转写任务注册表 + 事件总线（与 TaskManager 同构）。"""

    def __init__(self) -> None:
        self._tasks: dict[str, AsrTask] = {}
        self._segments: dict[str, list[dict]] = {}
        self._subscribers: dict[str, list[queue.Queue]] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 查询

    def list_tasks(self) -> list[dict]:
        with self._lock:
            tasks = sorted(self._tasks.values(), key=lambda t: t.created_at, reverse=True)
            return [t.to_dict() for t in tasks]

    def get_task(self, task_id: str) -> Optional[dict]:
        with self._lock:
            task = self._tasks.get(task_id)
            return task.to_dict() if task else None

    def get_segments(self, task_id: str) -> list[dict]:
        with self._lock:
            return list(self._segments.get(task_id, ()))

    # ------------------------------------------------------------------ 订阅

    def subscribe(self, task_id: str) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1024)
        with self._lock:
            self._subscribers.setdefault(task_id, []).append(q)
            task = self._tasks.get(task_id)
            snapshot = task.to_dict() if task else None
            segments = list(self._segments.get(task_id, ())) if task else []

        if snapshot is not None:
            # 任务若已结束，先补发完整结果再发快照：
            # 这样前端收到 done 时数据一定是齐的（避免任务过快完成导致结果丢失）
            if snapshot.get("stage") == "done" and segments:
                self._enqueue(q, {
                    "type": "result",
                    "text": snapshot.get("text", ""),
                    "segments": segments,
                })
            self._enqueue(q, {"type": "task", "task": snapshot})
        return q

    @staticmethod
    def _enqueue(q: queue.Queue, event: dict) -> None:
        try:
            q.put_nowait(event)
        except queue.Full:
            pass

    def unsubscribe(self, task_id: str, q: queue.Queue) -> None:
        with self._lock:
            subs = self._subscribers.get(task_id)
            if subs and q in subs:
                subs.remove(q)
            if subs is not None and not subs:
                self._subscribers.pop(task_id, None)

    def _broadcast(self, task_id: str, event: dict) -> None:
        with self._lock:
            subs = list(self._subscribers.get(task_id, ()))
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    def _patch(self, task: AsrTask, **fields) -> None:
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
    ) -> dict:
        # 平台字幕不需要本地识别环境，所以「仅用平台字幕」不设这道闸。
        # 其余模式要跑语音识别，环境缺失时提前拦下来，比跑到一半才报错友好。
        if mode != "subtitle" and not is_available():
            raise RuntimeError("语音识别环境未就绪，请先运行一次安装脚本；若视频自带字幕，可把来源改为「仅用平台字幕」")

        # 本地文件没有「平台字幕」这回事，强制走语音识别
        if not is_url:
            mode = "asr"

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
        )
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
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        self._patch(task, stage="cancelled", message="已取消", finished_at=time.time())
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
                if task.is_url:
                    media = self._fetch_audio(task, work_dir)
                else:
                    media = Path(task.source).expanduser()
                    if not media.exists():
                        raise RuntimeError("待处理的文件不存在或已被移动")
                    self._patch(task, stage="decoding", percent=FETCH_SHARE, message="准备音频…")

                self._run_worker(task, media, work_dir)

            if task.stage not in TERMINAL_STAGES:
                # 终态判定权收归任务层：确保前端收到 done 时数据已经完整
                with self._lock:
                    segments = list(self._segments.get(task.id, ()))
                    text = "".join(s["text"] for s in segments).strip()
                self._broadcast(task.id, {"type": "result", "text": text, "segments": segments})
                self._patch(
                    task,
                    stage="done",
                    percent=100.0,
                    message="字幕提取完成" if from_subtitle else "提取完成",
                    text=text,
                    segment_count=len(segments),
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

        payload = {
            "source": str(media),
            "language": task.language,
            "engine": task.engine,
            "model": "",
            "work_dir": str(work_dir / "asr"),
            "chunk_seconds": 300,
            "initial_percent": FETCH_SHARE,
        }

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

        # stderr 单独抽干，避免缓冲区写满后子进程卡死
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
            proc.wait(timeout=10)
            reader.join(timeout=2)

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
            fields: dict = {"duration": float(event.get("duration") or 0.0)}
            if engine:
                fields["engine_used"] = engine
            self._patch(task, **fields)
            if label:
                self._broadcast(task.id, {"type": "log", "message": f"识别引擎：{label}"})
            return None

        if kind == "progress":
            self._patch(
                task,
                percent=float(event.get("percent") or task.percent),
                message=event.get("message") or task.message,
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
            self._patch(task, segment_count=int(event.get("segment_count") or task.segment_count))
            return None

        if kind == "error":
            return str(event.get("message") or "转写失败")
        return None


manager = AsrManager()
