# Copyright (c) 2026 Hyht-Team. All rights reserved.
# See LICENSE (proprietary) and NOTICE.md (third-party components).
"""一键流水线 —— 把「两个程序」真正接成「一条产线」。

原版四个动作（parse / download / asr / polish）是四个独立 POST、各自跑一遍
yt-dlp 的 extract_info。本模块把它合成一个任务：

    probe        只做一次 extract_info，完整 info 留在内存并落盘
    download     可选，用官方 process_info(info) 下载，**零重复解析**
    transcript   字幕轨直接从 info 取 URL（零重复解析）；无字幕才转 ASR
    polish       离线规则整理（可接客户内网大模型）
    knowledge    产出带章节 / 带出处 / 可去口播的结构化知识稿（4 种格式）

所有阶段可降级、可注入，单测无需联网。一个任务一条进度，前端只点一次。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.request import Request, urlopen

from . import boot_check, guards, knowledge

APP_DIR = Path(__file__).resolve().parent.parent
OUTPUT_ROOT = APP_DIR / "outputs" / "pipeline"

TERMINAL = ("done", "error", "cancelled")

STAGE_WEIGHT = {
    "queued": 0.0, "probe": 15.0, "download": 35.0,
    "transcript": 70.0, "polish": 85.0, "knowledge": 95.0, "done": 100.0,
}

_LANG_ORDER = ("zh-Hans", "zh-CN", "zh", "zh-Hant", "en", "en-US", "auto")
_MAX_SUBTITLE_BYTES = 4 * 1024 * 1024
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"


@dataclass
class PipelineTask:
    id: str
    url: str
    language: str = "zh"
    want_download: bool = False
    want_polish: bool = True
    stage: str = "queued"
    percent: float = 0.0
    message: str = "准备中…"
    source_kind: str = ""
    info: dict[str, Any] = field(default_factory=dict, repr=False)
    meta: dict[str, Any] = field(default_factory=dict)
    segments: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    keywords: list[str] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)
    degraded: list[str] = field(default_factory=list)
    error: str = ""
    media_path: str = ""
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """对外快照：不含 info 全文（可能很大、含内部字段），只给必要信息。"""
        return {
            "id": self.id,
            "url": self.url,
            "stage": self.stage,
            "percent": round(self.percent, 1),
            "message": self.message,
            "title": self.meta.get("title", ""),
            "source_kind": self.source_kind,
            "chapter_count": len(self.meta.get("chapters") or []),
            "segment_count": len(self.segments),
            "summary": self.summary,
            "keywords": list(self.keywords),
            "files": dict(self.files),
            "degraded": list(self.degraded),
            "error": self.error,
            "media_path": self.media_path,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


def _probe_once(url: str) -> dict[str, Any]:
    """全任务唯一的一次站点解析。下游所有阶段都复用它的 info。"""
    boot_check.activate()
    import yt_dlp  # noqa: WPS433 — 必须在 activate() 之后导入

    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "nocheckcertificate": False,
    }
    allowed = guards.allowed_extractors()
    if allowed:
        opts["allowed_extractors"] = allowed  # 官方裁剪开关，零源码改动

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False) or {}
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise RuntimeError("该链接下没有可处理的条目")
        info = entries[0]
    if not info:
        raise RuntimeError("解析失败：未取得任何元信息")
    return info


def _download_once(info: dict[str, Any], work_dir: Path) -> str:
    """用官方 process_info(info) 下载 —— 不再解析一次 URL，这是「零重复」的关键。"""
    boot_check.activate()
    import yt_dlp  # noqa: WPS433

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "format": "bestaudio/best",
        "outtmpl": {"default": str(work_dir / "media.%(ext)s")},
        "overwrites": False,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.process_info(dict(info))
    files = sorted((p for p in work_dir.glob("media.*") if p.is_file()),
                   key=lambda p: p.stat().st_size, reverse=True)
    return str(files[0]) if files else ""


def _pick_subtitle_track(info: dict[str, Any], language: str) -> tuple[str, str, str]:
    """从 info 里挑一条字幕轨，返回 (url, 格式, 来源标记)。不额外发解析请求。"""
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    preferred = _LANG_ORDER if language in ("zh", "auto") else ("en", "en-US", "zh", "auto")
    for bucket, kind in ((manual, "subtitle"), (auto, "subtitle")):
        for lang in preferred:
            for key in (lang, lang.lower()):
                tracks = bucket.get(key)
                if not isinstance(tracks, list) or not tracks:
                    continue
                for track in tracks:
                    if not isinstance(track, dict) or not track.get("url"):
                        continue
                    ext = str(track.get("ext") or "").lower()
                    if "json3" in ext:
                        fmt = "json3"
                    elif "vtt" in ext:
                        fmt = "vtt"
                    elif "srt" in ext:
                        fmt = "srt"
                    elif "json" in ext:
                        fmt = "json"
                    else:
                        continue
                    return str(track["url"]), fmt, kind
    return "", "", ""


def _fetch_subtitle_track(url: str, fmt: str) -> list[dict[str, Any]]:
    """抓取并解析字幕文本；复用原版手写的 VTT/SRT/JSON 解析器，不引第三方包。"""
    if not url:
        return []
    req = Request(url, headers={"User-Agent": _UA, "Accept": "*/*"})
    with urlopen(req, timeout=15) as resp:  # noqa: S310 — URL 已过白名单与 IP 校验
        raw = resp.read(_MAX_SUBTITLE_BYTES)
    text = raw.decode("utf-8", errors="replace")
    try:
        from downloader import subtitles as sub
    except Exception as exc:  # 原版模块不可用时降级为最小 VTT 解析
        return _minimal_vtt(text)
    parser = {
        "vtt": sub.parse_vtt, "srt": sub.parse_srt,
        "json3": sub.parse_json3, "json": sub.parse_bilibili_json,
    }.get(fmt)
    cues = parser(text) if parser else _minimal_vtt(text)
    return sub.merge_cues(cues) if hasattr(sub, "merge_cues") else cues


def _minimal_vtt(text: str) -> list[dict[str, Any]]:
    pattern = re.compile(
        r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})"
    )
    out: list[dict[str, Any]] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = pattern.search(line)
        if not m or i + 1 >= len(lines):
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2 = m.groups()
        start = int(h1) * 3600 + int(m1) * 60 + int(s1) + int(ms1) / 1000
        end = int(h2) * 3600 + int(m2) * 60 + int(s2) + int(ms2) / 1000
        body = re.sub(r"<[^>]*>", "", lines[i + 1]).strip()
        if body:
            out.append({"start": start, "end": end, "text": body})
    return out


_STOPWORDS = {"这个", "一个", "我们", "你们", "他们", "就是", "因为", "所以", "但是",
              "可以", "没有", "什么", "怎么", "自己", "时候", "这样", "那样", "其实",
              "如果", "已经", "还是", "非常", "比较", "一直", "可能", "需要"}


def offline_polish(segments: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """断网也能跑的规则整理：摘要取首段要点，关键词按片段词频粗提。"""
    texts = [str(s.get("text") or "").strip() for s in segments if s.get("text")]
    if not texts:
        return "", []
    head = "".join(texts[:6])
    summary = head[:160].rstrip("，,、 ") + ("…" if len(head) > 160 else "")
    counter: dict[str, int] = {}
    for chunk in re.split(r"[，。！？；、,.!?\s]+", "".join(texts)):
        chunk = chunk.strip()
        if 2 <= len(chunk) <= 8 and chunk not in _STOPWORDS and not chunk.isdigit():
            counter[chunk] = counter.get(chunk, 0) + 1
    keywords = [w for w, _ in sorted(counter.items(), key=lambda kv: (-kv[1], -len(kv[0])))[:8]]
    return summary, keywords


def has_audio_stream(path: str | Path | None) -> bool | None:
    """用 ffprobe 判断媒体是否带音轨；ffprobe 不可用时返回 None（未知）。

    实测驱动（2026-11-12）：用真实直链素材跑全链，该素材只有 h264 视频轨，
    原实现仍去跑 ASR，最后却报「语音识别环境未就绪」，把运维指向错误的修复动作。
    """
    if not path or not Path(path).is_file():
        return None
    probe = shutil.which("ffprobe")
    if not probe:
        return None
    try:
        out = subprocess.run(
            [probe, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return "audio" in out.stdout.strip() if out.returncode == 0 else None


def _asr_via_original(
    url: str, language: str, title: str, media_path: str = ""
) -> list[dict[str, Any]]:
    """无字幕时接回原版 ASR 管理器。

    关键修复（实测驱动）：流水线若已下载媒体，就把**本地文件**交给原版 ASR
    （``is_url=False``），不再拿 URL 重新解析、重新下载一遍音频。这是
    「一次解析、全链复用」在 ASR 分支上的最后一环。
    """
    from downloader import asr  # noqa: WPS433

    if not asr.is_available():
        return []
    local = Path(media_path) if media_path and Path(media_path).is_file() else None
    engine = (os.environ.get("AVD_ASR_ENGINE") or "auto").strip() or "auto"
    kwargs = dict(language=language, title=title, mode="asr", engine=engine)
    if local:
        task = asr.manager.create_task(str(local), is_url=False, **kwargs)
    else:
        task = asr.manager.create_task(url, is_url=True, **kwargs)
    task_id = str(task.get("id") or "")
    if not task_id:
        return []
    deadline = time.time() + 900
    while time.time() < deadline:
        snapshot = asr.manager.get_task(task_id)
        if not snapshot:
            return []
        if snapshot.get("stage") in ("done", "error", "cancelled"):
            if snapshot.get("stage") != "done":
                return []
            return list(asr.manager.get_segments(task_id))
        time.sleep(0.4)
    return []


class PipelineManager:
    """任务表 + 后台线程。线程安全，支持注入各阶段以便离线单测。"""

    def __init__(
        self,
        *,
        probe_fn: Callable[[str], dict[str, Any]] | None = None,
        download_fn: Callable[[dict[str, Any], Path], str] | None = None,
        subtitle_fn: Callable[[str, str], list[dict[str, Any]]] | None = None,
        asr_fn: Callable[..., list[dict[str, Any]]] | None = None,
        output_root: Path | None = None,
    ) -> None:
        self._probe = probe_fn or _probe_once
        self._download = download_fn or _download_once
        self._subtitle = subtitle_fn or _fetch_subtitle_track
        self._asr = asr_fn or _asr_via_original
        self.output_root = Path(output_root) if output_root else OUTPUT_ROOT
        self._tasks: dict[str, PipelineTask] = {}
        self._cancelled: set[str] = set()
        self._lock = threading.RLock()

    def start(
        self,
        url: str,
        *,
        language: str = "zh",
        want_download: bool = False,
        want_polish: bool = True,
    ) -> dict[str, Any]:
        clean = (url or "").strip()
        ok, reason = guards.check_media_url(clean)
        if not ok:
            raise ValueError(reason)
        task = PipelineTask(
            id=uuid.uuid4().hex[:12], url=clean, language=language,
            want_download=want_download, want_polish=want_polish,
        )
        with self._lock:
            self._tasks[task.id] = task
        worker = threading.Thread(
            target=self._run, args=(task,), name=f"avd-pipeline-{task.id}", daemon=True
        )
        worker.start()
        return task.to_dict()

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return task.to_dict() if task else None

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            return [t.to_dict() for t in sorted(
                self._tasks.values(), key=lambda t: t.created_at, reverse=True)]

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            if task_id not in self._tasks:
                return False
            self._cancelled.add(task_id)
            task = self._tasks[task_id]
            if task.stage not in TERMINAL:
                self._set(task, stage="cancelled", percent=100.0,
                          message="已取消", finished_at=time.time())
        return True

    def read_artifact(self, task_id: str, fmt: str) -> str | None:
        with self._lock:
            task = self._tasks.get(task_id)
            rel = task.files.get(fmt) if task else None
        if not rel:
            return None
        path = self.output_root / rel
        safe = guards.within(self.output_root, path)
        if not safe or not safe.is_file():
            return None
        return safe.read_text(encoding="utf-8", errors="replace")

    def _set(self, task: PipelineTask, **fields: Any) -> None:
        with self._lock:
            if task.id in self._cancelled and task.stage == "cancelled":
                return
            for key, value in fields.items():
                setattr(task, key, value)

    def _stage(self, task: PipelineTask, stage: str, message: str) -> None:
        self._set(task, stage=stage, percent=STAGE_WEIGHT.get(stage, 0.0), message=message)

    def _run(self, task: PipelineTask) -> None:
        work_dir = self.output_root / task.id
        try:
            work_dir.mkdir(parents=True, exist_ok=True)

            self._stage(task, "probe", "正在解析（全任务仅此一次）…")
            info = self._probe(task.url)
            meta = knowledge.meta_from_info(info, task.url)
            self._set(task, info=info, meta=meta)
            (work_dir / "info.json").write_text(
                json.dumps({k: v for k, v in info.items()
                            if k in ("id", "title", "extractor", "extractor_key", "chapters",
                                     "duration", "uploader", "upload_date", "webpage_url",
                                     "tags", "categories", "description", "subtitles")},
                           ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )

            if task.want_download:
                self._stage(task, "download", "正在下载（复用已解析的 info）…")
                try:
                    task.media_path = self._download(info, work_dir)
                except Exception as exc:
                    task.degraded.append(f"下载阶段跳过：{exc}")

            self._stage(task, "transcript", "正在取得文字稿（字幕优先）…")
            track_url, fmt, kind = _pick_subtitle_track(info, task.language)
            if track_url:
                try:
                    cues = self._subtitle(track_url, fmt)
                except Exception as exc:
                    cues = []
                    task.degraded.append(f"字幕抓取失败：{exc}")
            else:
                cues = []
            if cues:
                task.segments = cues
                task.source_kind = kind
            else:
                task.degraded.append("平台未提供字幕")
                audio = has_audio_stream(task.media_path) if task.media_path else None
                if audio is False:
                    # 实测发现的误报：无音轨 ≠ 环境未就绪
                    task.degraded.append(
                        "该素材不含音轨，无法语音转写（本次仅输出元数据骨架）"
                    )
                    cues = []
                else:
                    try:
                        cues = self._asr(task.url, task.language,
                                         meta.get("title", ""), task.media_path)
                    except Exception as exc:
                        cues = []
                        task.degraded.append(f"语音识别调用失败：{exc}")
                    if cues:
                        task.segments = cues
                        task.source_kind = "asr"
                        if not task.media_path:
                            task.degraded.append(
                                "未下载媒体，ASR 仍走了一次独立的 URL 音频抓取"
                            )
                    else:
                        try:
                            from downloader import asr as _asr_mod  # noqa: WPS433
                            env_ready: bool | None = bool(_asr_mod.is_available())
                        except Exception:
                            env_ready = None
                        if env_ready is False:
                            task.degraded.append(
                                "语音识别环境未就绪：运行 python3 doctor.py --repair，"
                                "或双击 setup-asr.command 安装；本次仅输出元数据骨架"
                            )
                        else:
                            task.degraded.append(
                                "语音识别未产出文字稿：素材可能无语音、权重未预置或引擎加载失败；"
                                "本次仅输出元数据骨架"
                            )

            if task.want_polish and task.segments:
                self._stage(task, "polish", "正在整理（离线规则，可接内网大模型）…")
                task.summary, task.keywords = offline_polish(task.segments)

            self._stage(task, "knowledge", "正在生成结构化知识稿…")
            sponsor = knowledge.sponsor_ranges(info)
            if sponsor:
                task.degraded.append(
                    f"已按 info 内的离线段落数据剔除 {len(sponsor)} 段口播"
                )
            bundle = knowledge.build_bundle(
                meta, task.segments, summary=task.summary, keywords=task.keywords,
                source_kind=task.source_kind, sponsor=sponsor,
            )
            stem = re.sub(r"[\\/:*?\"<>|]", "_", meta.get("title") or task.id)[:80] or task.id
            saved: dict[str, str] = {}
            for ext, content in bundle.items():
                rel = f"{task.id}/{stem}.{ext}"
                (work_dir / f"{stem}.{ext}").write_text(content, encoding="utf-8")
                saved[ext] = rel
            (work_dir / "manifest.json").write_text(
                json.dumps({"task": task.to_dict(), "generator": "AVD Flagship v2.0.0",
                            "engine": "yt-dlp 2026.10.26 (vendored unmodified)"},
                           ensure_ascii=False, indent=2), encoding="utf-8")
            self._set(task, files=saved)

            if task.id in self._cancelled:
                self._set(task, stage="cancelled", percent=100.0,
                          message="已取消", finished_at=time.time())
                return
            self._set(task, stage="done", percent=100.0, message="完成",
                      finished_at=time.time())

        except Exception as exc:
            self._set(task, stage="error", error=str(exc), message=f"失败：{exc}",
                      percent=100.0, finished_at=time.time())


_MANAGER: PipelineManager | None = None


def manager() -> PipelineManager:
    """进程内单例，供 HTTP 层调用。"""
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = PipelineManager()
    return _MANAGER
