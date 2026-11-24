# Copyright (c) 2026 Hyht-Team
# SPDX-License-Identifier: MIT
# See LICENSE (MIT) and NOTICE.md (third-party components).
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
from urllib.error import URLError, HTTPError
from urllib.request import ProxyHandler, Request, build_opener, urlopen

from . import (
    avd_log, boot_check, checkpoint, degradation, diagnostics, engine_opts,
    guards, knowledge, persist, recovery,
)
from .machine import StageError, StageMachine
from . import scheduler as sched_mod

APP_DIR = Path(__file__).resolve().parent.parent
OUTPUT_ROOT = APP_DIR / "outputs" / "pipeline"
MAX_BATCH = 50

TERMINAL = ("done", "error", "cancelled")
MAX_TASKS = 200
TASK_TTL_SECONDS = 7 * 24 * 3600
# 保留策略最小执行间隔：清理要遍历产物树，不能挂在每单提交的热路径上
_PURGE_MIN_INTERVAL = 300.0
# 流水线槽位：下载/探测可并行；ASR 常驻池并发固定为 1，多任务会在转写阶段排队
MAX_CONCURRENT = 4
ASR_CONCURRENCY = 1
# 阶段看门狗（秒）：抓取 120 / 下载 300 / 转写按下方默认，可被音频时长放大
STAGE_TIMEOUT = {
    "probe": 120.0, "download": 300.0, "transcript": 900.0,
    "polish": 120.0, "knowledge": 60.0,
}
_WIN_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
# 控制字符：POSIX 只禁 NUL，Windows 对 0x00–0x1F 全禁（含标题里常见的换行/制表）。
# 这些字符若原样进文件名，Linux/macOS 能创建但会让目录列表、SRT 拼接、日志全部错行。
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")

STAGE_WEIGHT = {
    "queued": 0.0, "probe": 15.0, "download": 35.0,
    "transcript": 70.0, "polish": 85.0, "knowledge": 95.0, "done": 100.0,
}


class Cancelled(Exception):
    """流水线协作式取消。"""


class StageTimeout(Exception):
    """阶段看门狗超时。"""

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
    # 结构化降级项（同序：degradations[i] 对应 degraded[i]）——见 fusion/degradation.py
    degradations: list[dict[str, Any]] = field(default_factory=list)
    diagnosis: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    media_path: str = ""
    local_media: str = ""  # D11：本地上传路径（跳过 probe/download）
    asr_task_id: str = ""
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    not_before: float | None = None
    _stage_deadline: float | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """对外快照：不含 info 全文（可能很大、含内部字段），只给必要信息。"""
        items = degradation.to_dicts(self.degradations)
        if not items and self.degraded:
            # 旧快照（升级前落库）只有字符串列表：包成 note 级，保证前端结构一致
            items = [
                {"code": "legacy", "level": "note", "level_label": "提示",
                 "cause": str(line), "retriable": False, "action": ""}
                for line in self.degraded
            ]
        return {
            "id": self.id,
            "url": self.url,
            "stage": self.stage,
            "percent": round(self.percent, 1),
            "message": self.message,
            "title": self.meta.get("title", ""),
            "source_kind": self.source_kind,
            "chapter_count": len(self.meta.get("chapters") or []),
            "chapter_quality": dict(self.meta.get("chapter_quality") or {}),
            # R2：完整章节列表随快照输出。第十轮发现 export-clips-by-chapters
            # 从 snap["meta"] 取章节，而 to_dict 从来没给过 meta —— 该接口
            # 100% 回「任务没有章节」。章节通常 <50 条，随快照带的代价可接受。
            "chapters": [
                {"title": str(c.get("title") or ""),
                 "start": c.get("start"), "end": c.get("end")}
                for c in (self.meta.get("chapters") or []) if isinstance(c, dict)
            ],
            "segment_count": len(self.segments),
            "summary": self.summary,
            "keywords": list(self.keywords),
            "files": dict(self.files),
            "degraded": list(self.degraded),
            "degraded_items": items,
            "degraded_summary": degradation.summary(items),
            "diagnosis": dict(self.diagnosis),
            "error": self.error,
            "media_path": self.media_path,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "not_before": self.not_before,
            "chapters_auto": bool(self.meta.get("chapters_auto")),
            "summary_kind": self.meta.get("summary_kind") or "",
            "summary_coverage": dict(self.meta.get("summary_coverage") or {}),
            "speakers": bool(self.meta.get("speakers")),
            "speaker_source": self.meta.get("speaker_source") or "",
            "speaker_confidence": self.meta.get("speaker_confidence"),
            "speaker_stats": dict(self.meta.get("speaker_stats") or {}) or None,
            "low_content": bool(self.meta.get("low_content")),
            "content_level": str(self.meta.get("content_level") or ""),
            "content_sufficiency": dict(self.meta.get("content_sufficiency") or {}),
            "insights_source": str(self.meta.get("insights_source") or ""),
        }


def _playlist_limit() -> int:
    try:
        from downloader.config import config as _cfg
        return max(1, min(50, int(_cfg.get("playlist_max_entries") or 1)))
    except Exception:
        return 1


def list_playlist_entries(url: str, *, limit: int | None = None) -> list[dict[str, Any]]:
    """展开播放列表 / 多 P：返回前 N 条的可处理 URL（含标题）。

    ``limit=1`` 时与历史行为一致（只取首条）。失败时返回空列表，由调用方回退单条。
    """
    clean = (url or "").strip()
    if not clean:
        return []
    n = limit if limit is not None else _playlist_limit()
    n = max(1, min(50, int(n)))
    boot_check.activate()
    import yt_dlp  # noqa: WPS433

    opts = engine_opts.base_opts(clean)
    opts["noplaylist"] = False
    opts["extract_flat"] = "in_playlist"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(clean, download=False) or {}
    except Exception as exc:
        avd_log.warning("playlist expand failed", url=clean, error=str(exc))
        return []
    if info.get("_type") != "playlist":
        landing = str(info.get("webpage_url") or clean)
        title = str(info.get("title") or "")
        return [{"url": landing, "title": title, "id": info.get("id")}]
    out: list[dict[str, Any]] = []
    for entry in info.get("entries") or []:
        if not entry:
            continue
        eu = str(entry.get("url") or entry.get("webpage_url") or "").strip()
        if not eu:
            continue
        # flat 条目有时是相对 id；优先 webpage_url
        if not eu.startswith(("http://", "https://")):
            eu = str(entry.get("webpage_url") or "").strip()
        if not eu.startswith(("http://", "https://")):
            continue
        ok, _ = guards.check_media_url(eu)
        if not ok:
            continue
        out.append({
            "url": eu,
            "title": str(entry.get("title") or ""),
            "id": entry.get("id"),
        })
        if len(out) >= n:
            break
    return out


def _probe_once(url: str) -> dict[str, Any]:
    """全任务唯一的一次站点解析。下游所有阶段都复用它的 info。"""
    boot_check.activate()
    import yt_dlp  # noqa: WPS433 — 必须在 activate() 之后导入

    # 统一参数契约：cookie / 代理 / 重试 / extractor 裁剪全部继承配置。
    # 实测教训：原先这里只拼 6 个固定参数，抖音短链因缺 Cookie 直接 403。
    opts = engine_opts.base_opts(url)

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False) or {}
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise RuntimeError("该链接下没有可处理的条目")
        info = entries[0]
    if not info:
        raise RuntimeError("解析失败：未取得任何元信息")
    # 短链 / 开放重定向二次校验：入口白名单只管用户给的 URL，
    # 这里再确认引擎最终落地地址没有跳进内网或元数据服务。
    landing = str(info.get("webpage_url") or "")
    if landing.startswith(("http://", "https://")):
        ok, why = guards.check_media_url(landing, enforce_allowlist=False)
        if not ok:
            raise RuntimeError(f"解析结果落地地址不安全，已拒绝：{why}")
    return info


def _diarize_enabled() -> bool:
    try:
        from downloader.config import config as _cfg
        return bool(_cfg.get("asr_diarize"))
    except Exception:
        return False


def push_export(task_id: str, target: str) -> dict[str, Any]:
    """把已完成任务的 Markdown 推到 Notion / 飞书（读本地配置）。"""
    from . import exporters
    from downloader.config import config as _cfg

    mgr = manager()
    content = mgr.read_artifact(task_id, "md")
    if content is None:
        raise ValueError("任务不存在或尚未产出 Markdown")
    snap = mgr.get(task_id) or {}
    title = str(snap.get("title") or task_id)
    # 飞书卡片附本地全文路径（file://），便于点开对照
    md_path = mgr.output_root / task_id
    fulltext = ""
    for p in sorted(md_path.glob("*.md")):
        fulltext = p.resolve().as_uri()
        break
    return exporters.push_from_config(
        target, title=title, markdown=content, cfg=_cfg.as_dict(),
        fulltext_link=fulltext,
    )


def _resolve_task_media(task_id: str) -> tuple[str, dict[str, Any]]:
    mgr = manager()
    snap = mgr.get(task_id) or {}
    media = str(snap.get("media_path") or "").strip()
    if not media or not Path(media).is_file():
        work = mgr.output_root / task_id
        candidates = sorted(
            list(work.glob("audio.*")) + list(work.glob("media.*")) + list(work.glob("*.mp4")),
            key=lambda p: p.stat().st_size if p.is_file() else 0,
            reverse=True,
        )
        media = str(candidates[0]) if candidates else ""
    if not media or not Path(media).is_file():
        raise ValueError("任务没有可切片的媒体（请勾选「同时下载媒体」后重跑）")
    return media, snap


def export_clip(
    task_id: str,
    *,
    start: float,
    end: float,
    reencode: bool = False,
) -> dict[str, Any]:
    """从任务媒体文件切一段；无媒体则报错。"""
    from . import clips

    mgr = manager()
    media, snap = _resolve_task_media(task_id)
    stem = safe_stem(str(snap.get("title") or task_id), fallback=task_id)
    out = mgr.output_root / task_id / f"{stem}_{int(start)}-{int(end)}.mp4"
    result = clips.export_clip(media, start=start, end=end, dest=out, reencode=reencode)
    avd_log.info("clip export", task=task_id, **{k: result[k] for k in ("start", "end", "mode")})
    return result


def export_clips_by_chapters(
    task_id: str,
    *,
    reencode: bool = False,
) -> dict[str, Any]:
    """按章节批量切片，服务二次剪辑。"""
    from . import clips

    mgr = manager()
    media, snap = _resolve_task_media(task_id)
    chapters = list(snap.get("chapters") or (snap.get("meta") or {}).get("chapters") or [])
    if not chapters:
        raise ValueError("任务没有章节，无法批量切片")
    stem = safe_stem(str(snap.get("title") or task_id), fallback=task_id)
    out_dir = mgr.output_root / task_id / "chapter_clips"
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for i, ch in enumerate(chapters):
        try:
            start = float(ch.get("start") or 0)
            end = float(ch.get("end") if ch.get("end") is not None else start + 30)
        except (TypeError, ValueError):
            continue
        if end <= start + 0.2:
            continue
        title = safe_stem(str(ch.get("title") or f"ch{i + 1}"), fallback=f"ch{i + 1}")
        dest = out_dir / f"{i + 1:02d}_{title}.mp4"
        try:
            results.append(
                clips.export_clip(media, start=start, end=end, dest=dest, reencode=reencode)
            )
        except Exception as exc:  # noqa: BLE001
            results.append({"error": str(exc), "start": start, "end": end, "title": title})
    return {"count": len(results), "clips": results, "dir": str(out_dir)}


def resolve_obsidian_dir(raw: str | None = None) -> Path | None:
    """校验并返回可写的 Obsidian 库目录；未配置或非法则 None。"""
    from downloader.config import config as _cfg
    text = (raw if raw is not None else str(_cfg.get("obsidian_dir") or "")).strip()
    if not text:
        return None
    try:
        target = Path(text).expanduser().resolve()
    except (OSError, ValueError):
        return None
    home = Path.home().resolve()
    try:
        target.relative_to(home)
    except ValueError:
        return None
    if target.is_file():
        return None
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return target


def write_obsidian(task_id: str, *, vault: str | None = None) -> dict[str, Any]:
    """把已完成任务的 Markdown 写入 Obsidian 库目录。"""
    mgr = manager()
    content = mgr.read_artifact(task_id, "md")
    if content is None:
        raise ValueError("任务不存在或尚未产出 Markdown")
    snap = mgr.get(task_id) or {}
    title = str(snap.get("title") or task_id)
    dest_dir = resolve_obsidian_dir(vault)
    if dest_dir is None:
        raise ValueError("未配置 Obsidian 目录，或目录不在用户主目录下")
    stem = safe_stem(title, fallback=task_id)
    path = dest_dir / f"{stem}.md"
    # 重名时追加短 id，避免覆盖用户手稿
    if path.exists():
        path = dest_dir / f"{stem}-{task_id[:8]}.md"
    path.write_text(content, encoding="utf-8")
    avd_log.info("obsidian write", task=task_id, path=str(path))
    return {"path": str(path), "bytes": len(content.encode("utf-8"))}


def diagnose_url(url: str) -> dict[str, Any]:
    """同步诊断一条链接：只解析一次、不下载，返回可操作结论。

    供 ``POST /api/platform/diagnose`` 使用，让运维在正式跑流水线前
    就能知道「这个站点要 Cookie / 要 PO Token / 还是被白名单挡了」。
    """
    clean = (url or "").strip()
    ok, reason = guards.check_media_url(clean)
    if not ok:
        return {
            "ok": False, "ready": False, "url": clean,
            "platform": diagnostics.platform_hint(clean),
            "auth": engine_opts.cookie_status(),
            "diagnosis": {
                "code": "BLOCKED_BY_POLICY", "title": "链接未通过合规 / 安全校验",
                "detail": reason,
                "actions": ["确认站点在 AVD_ALLOWED_HOSTS 白名单内",
                            "确认目标不是内网 / 本机 / 云元数据地址"],
                "raw": reason, "actionable": True,
            },
        }
    hint = diagnostics.platform_hint(clean)
    auth = engine_opts.cookie_status()
    try:
        info = _probe_once(clean)
    except Exception as exc:  # 平台错误一律转成结构化诊断，不把英文原文甩给用户
        return {
            "ok": True, "ready": False, "url": clean,
            "platform": hint, "auth": auth,
            "diagnosis": diagnostics.diagnose(exc),
        }
    return {
        "ok": True, "ready": True, "url": clean,
        "platform": hint, "auth": auth,
        "diagnosis": {
            "code": "OK", "title": "解析成功，可以一键出稿",
            "detail": "已取得完整 info；把这条链接交给 /api/pipeline/start 即可。",
            "actions": [], "raw": "", "actionable": False,
        },
        "info": {
            "id": info.get("id"), "title": info.get("title"),
            "extractor": info.get("extractor") or info.get("extractor_key"),
            "duration": info.get("duration"),
            "chapters": len(info.get("chapters") or []),
            "subtitle_tracks": len(info.get("subtitles") or {}),
            "auto_captions": len(info.get("automatic_captions") or {}),
            "webpage_url": info.get("webpage_url"),
        },
    }


_MEDIA_PATTERNS = ("audio.*", "media.*")


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return -1


def _existing_media(work_dir: Path, *, min_bytes: int = 4096) -> str:
    """返回 ``work_dir`` 里已落盘且非空的媒体路径，没有则空串。

    P1（第九轮）：实测真实 B 站任务 download 阶段 2.5s，随后 degraded 写
    「音频下载失败」，但 ``audio.mp4``（21,498,132B）其实已完整落盘、且最终被
    ASR 使用——异常发生在下载之后的后处理/收尾，而代码把 ``media_path`` 留空，
    转写阶段又用 URL 重新解析一次（26.5s 空档）。这里先认盘再决定要不要重下。
    """
    items: list[Path] = []
    for pattern in _MEDIA_PATTERNS:
        try:
            items.extend(p for p in work_dir.glob(pattern) if p.is_file())
        except OSError:
            continue
    usable = [p for p in items if _safe_size(p) >= min_bytes]
    if not usable:
        return ""
    return str(max(usable, key=_safe_size))


def _download_once(info: dict[str, Any], work_dir: Path) -> str:
    """用官方 process_info(info) 下载 —— 不再解析一次 URL，这是「零重复」的关键。

    流水线媒体语义固定为「纯音频优先」：bestaudio → 抽成 m4a，供 ASR/字幕链路复用；
    仅当站点无独立音轨时才回落 best（可能含视频）。落盘文件名 ``audio.*`` 以示区别。

    幂等：``work_dir`` 已有可用媒体时直接复用（重跑/续跑不再发第二次下载请求）。
    """
    cached = _existing_media(work_dir)
    if cached:
        avd_log.info("media cache hit", path=cached)
        return cached
    boot_check.activate()
    import yt_dlp  # noqa: WPS433

    # 与解析同一套参数契约：平台视频流同样需要 Cookie / 代理 / extractor 配置。
    opts = engine_opts.base_opts(str(info.get("webpage_url") or ""))
    opts.pop("skip_download", None)  # 下载阶段必须允许真正取流
    opts.update({
        "format": "bestaudio/best",
        "outtmpl": {"default": str(work_dir / "audio.%(ext)s")},
        "overwrites": False,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "m4a",
            "preferredquality": "0",
        }],
        "keepvideo": False,
    })
    if info.get("is_live"):
        secs = engine_opts.live_record_seconds()
        if secs == 0:
            raise RuntimeError(
                "这是直播流。请在设置中把「直播录制时长」设为 60–7200 秒，或 -1（录到取消）"
            )
        if secs < 0 and secs != -1:
            raise RuntimeError("直播录制时长无效")
        engine_opts.apply_live_clip_opts(opts, secs)
        # 直播产物保留容器（跳过抽音频），取消可终止「录到取消」模式
        opts.pop("postprocessors", None)
        opts["outtmpl"] = {"default": str(work_dir / "media.%(ext)s")}
        avd_log.info("live clip download", seconds=secs, title=info.get("title"))
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.process_info(dict(info))
    return _existing_media(work_dir) or ""


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
    """抓取并解析字幕文本；复用原版手写的 VTT/SRT/JSON 解析器，不引第三方包。

    SEC-03：统一走 proxy / cookie / UA；对 track URL 做安全底线校验
    （``enforce_allowlist=False``，CDN 主机常不在媒体白名单内）。
    """
    if not url:
        return []
    ok, why = guards.check_media_url(url, enforce_allowlist=False)
    if not ok:
        raise RuntimeError(f"字幕轨地址不安全，已拒绝：{why}")

    headers = {"User-Agent": _UA, "Accept": "*/*"}
    opts = engine_opts.base_opts(url)
    # yt-dlp cookiefile → Cookie 头（若有）
    cookiefile = opts.get("cookiefile")
    if cookiefile and Path(cookiefile).is_file():
        try:
            from http.cookiejar import MozillaCookieJar
            jar = MozillaCookieJar(str(cookiefile))
            jar.load(ignore_discard=True, ignore_expires=True)
            cookie_header = "; ".join(f"{c.name}={c.value}" for c in jar)
            if cookie_header:
                headers["Cookie"] = cookie_header
        except Exception as exc:  # noqa: BLE001
            # Cookie 文件损坏不应阻断字幕抓取，但必须留痕，否则「为什么 403」无从查起
            avd_log.warning("cookie header skipped", url=url, error=str(exc))

    req = Request(url, headers=headers)
    proxy = opts.get("proxy")
    if proxy:
        opener = build_opener(ProxyHandler({"http": proxy, "https": proxy}))
    else:
        opener = build_opener()
    try:
        with opener.open(req, timeout=15) as resp:  # noqa: S310 — 已过 IP/端口校验
            raw = resp.read(_MAX_SUBTITLE_BYTES)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"字幕轨抓取失败：{exc}") from exc

    text = raw.decode("utf-8", errors="replace")
    try:
        from downloader import subtitles as sub
    except Exception:
        return _minimal_vtt(text)
    parser = {
        "vtt": sub.parse_vtt, "srt": sub.parse_srt,
        "json3": sub.parse_json3, "json": sub.parse_bilibili_json,
    }.get(fmt)
    cues = parser(text) if parser else _minimal_vtt(text)
    return sub.merge_cues(cues) if hasattr(sub, "merge_cues") else cues


def safe_stem(title: str, fallback: str = "untitled") -> str:
    """文件名消毒：控制字符 + 非法字符 + Windows 保留名 + 首尾点/空格（WIN-01）。

    R2（三系统 E2E）：补掉两个跨平台坑——
    * 控制字符（``\\n`` / ``\\t`` / ``\\x00``）此前原样留着：Windows 直接
      ``ValueError``（NUL）或生成诡异文件名，POSIX 虽能建但会破坏目录列表；
    * 保留名表补 ``CLOCK$`` / ``CONIN$`` / ``CONOUT$``。
    空白压缩成单个空格，避免 "标题\\n\\n\\n副标题" 变成一长串空格。
    """
    text = _CTRL_RE.sub(" ", (title or ""))
    stem = re.sub(r'[\\/:*?"<>|]', "_", text.strip())
    stem = re.sub(r"\s+", " ", stem).strip(" .")[:80].strip(" .")
    stem = stem or fallback
    if stem.upper() in _WIN_RESERVED or stem.upper().split(".")[0] in _WIN_RESERVED:
        stem = f"_{stem}"
    return stem


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
_LATIN_STOP = frozenset({
    "the", "and", "for", "you", "are", "this", "that", "with", "from", "have",
    "not", "but", "all", "can", "get", "our", "your", "was", "has", "how",
    "why", "what", "who", "its", "his", "her", "they", "will", "would", "there",
    "their", "about", "into", "than", "then", "out", "one", "two", "off",
})
# 虚词/代词：含这些字的 bigram 基本是跨词边界碎片（「的知」「路与」），先剪掉
_CJK_FUNC = frozenset("的了是在与和就把被这那个们也都很有没不之其此该各每些让使向往从而或及"
                      "我你他她它又再才还要会能对为以着过吗呢吧啊嘛哦嗯")
_LATIN_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'’\-\.\+#_]{1,}")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")


def _rule_keyword_stats(texts: list[str]) -> tuple[dict[str, int], dict[str, float]]:
    """词频统计：**拉丁按整词**、**中文按 bigram**，并对中文做虚词边界剪裁。

    P2（第九轮）：原实现对所有字符（含英文）开 2–3 字符滑窗，于是
    "Exclusive / Series / Television / YoYo" 被切成 ``Exc`` / ``Ser`` /
    ``Tel`` / ``YoY`` 当关键词，中文也常出跨词碎片。现在：

    * 拉丁：正则整词 + 小写 + 停用词表，长度 ≥2；
    * 中文：只取连续汉字段内的 bigram，且首尾不得是虚词/代词；
    * 频次门槛：段数 ≥3 时要求 ``tf≥2 且 df≥2``（跨段复现才算关键词）；
    * 排序辅助：左右邻接多样性（新词发现里的邻接熵代理量），
      用来把「转写」这种真词排在「出机」这种跨词碎片前面。
    """
    counts: dict[str, int] = {}
    df: dict[str, int] = {}
    left: dict[str, set[str]] = {}
    right: dict[str, set[str]] = {}
    for text in texts:
        seen: set[str] = set()
        for raw in _LATIN_WORD_RE.findall(text or ""):
            word = raw.lower().strip(".-'’")
            if len(word) < 2 or word.isdigit():
                continue
            if word in _LATIN_STOP or word in _STOPWORDS:
                continue
            counts[word] = counts.get(word, 0) + 1
            seen.add(word)
            left.setdefault(word, set()).add("w")
            right.setdefault(word, set()).add("w")
        for run in _CJK_RUN_RE.findall(text or ""):
            for i in range(len(run) - 1):
                word = run[i:i + 2]
                if word in _STOPWORDS:
                    continue
                if word[0] in _CJK_FUNC or word[1] in _CJK_FUNC:
                    continue
                counts[word] = counts.get(word, 0) + 1
                seen.add(word)
                left.setdefault(word, set()).add(run[i - 1] if i > 0 else "^")
                right.setdefault(word, set()).add(
                    run[i + 2] if i + 2 < len(run) else "$")
        for word in seen:
            df[word] = df.get(word, 0) + 1
    if len(texts) >= 3:
        kept = {w: c for w, c in counts.items() if c >= 2 and df.get(w, 0) >= 2}
    else:
        kept = {w: c for w, c in counts.items() if c >= 1}
    if not kept:
        # 极短/单段素材：保留重复项与拉丁整词，宁可少给也不给碎片
        kept = {
            w: c for w, c in counts.items()
            if c >= 2 or not _CJK_RUN_RE.fullmatch(w)
        }
    diversity = {w: float(len(left.get(w, ())) + len(right.get(w, ()))) for w in kept}
    return kept, diversity


def _rule_keyword_counter(texts: list[str]) -> dict[str, int]:
    """兼容入口：只要词频（供句子抽取打分复用同一套切分）。"""
    return _rule_keyword_stats(texts)[0]


def _score_sentence(text: str, counter: dict[str, int], *, index: int, total: int) -> float:
    """抽取式打分：关键词命中 + 适中长度 + 开头/结尾位置加权。"""
    t = (text or "").strip()
    if len(t) < 6:
        return -1.0
    hits = 0.0
    for run in _CJK_RUN_RE.findall(t):
        for i in range(len(run) - 1):
            hits += float(counter.get(run[i:i + 2], 0))
    for raw in _LATIN_WORD_RE.findall(t):
        hits += float(counter.get(raw.lower().strip(".-'’"), 0))
    # 过短/过长惩罚
    length_bonus = 1.0 if 12 <= len(t) <= 80 else (0.6 if len(t) < 12 else 0.75)
    pos = index / max(1, total - 1)
    # 开头与结尾略加权（导语 / 收束）
    pos_bonus = 1.25 if pos <= 0.12 or pos >= 0.88 else 1.0
    return hits * length_bonus * pos_bonus


def offline_polish(segments: list[dict[str, Any]]) -> tuple[str, list[str]]:
    """断网规则整理：抽取式要点（非首句拼接）→ 导语 + 要点列表 → 词频关键词。

    返回的摘要正文会带「规则摘要」标识，避免被当成 LLM 产出。
    """
    texts = [str(s.get("text") or "").strip() for s in segments if s.get("text")]
    if not texts:
        return "", []

    counter, diversity = _rule_keyword_stats(texts)
    ranked_idx = sorted(
        range(len(texts)),
        key=lambda i: (-_score_sentence(texts[i], counter, index=i, total=len(texts)), i),
    )
    # 取 Top-K，再按时间序输出，避免摘要乱序
    k = min(8, max(3, len(texts) // 8 or 3))
    picked_idx = sorted(ranked_idx[:k])
    # 去近邻重复（相邻且前 16 字相同）
    clean_idx: list[int] = []
    for i in picked_idx:
        if clean_idx and texts[i][:16] == texts[clean_idx[-1]][:16]:
            continue
        clean_idx.append(i)

    bullets: list[str] = []
    for i in clean_idx[:8]:
        head = texts[i][:56].rstrip("，,、 ")
        if len(texts[i]) > 56:
            head += "…"
        bullets.append(f"- {head}")

    lead = texts[clean_idx[0]][:72].rstrip("，,、 ") if clean_idx else texts[0][:72]
    if clean_idx and len(texts[clean_idx[0]]) > 72:
        lead += "…"
    body = f"导语：{lead}\n" + "\n".join(bullets)
    if len(body) > 480:
        body = f"导语：{lead}\n" + "\n".join(bullets[:6]) + "\n- …"
    summary = "【规则摘要】\n" + body

    ranked = sorted(
        counter.items(),
        key=lambda kv: (-kv[1], -diversity.get(kv[0], 0.0), -len(kv[0]), kv[0]),
    )
    keywords = [w for w, _ in ranked[:8]]
    return summary, keywords


def enrich_summary(
    segments: list[dict[str, Any]],
    *,
    use_llm: bool = True,
) -> tuple[str, list[str], str]:
    """摘要 + 关键词：规则兜底；有 LLM 配置时一次调用增强。

    返回 (summary, keywords, source)，source 为 ``llm`` 或 ``rule``。
    """
    rule_summary, rule_kw = offline_polish(segments)
    if not use_llm:
        return rule_summary, rule_kw, "rule"
    try:
        from downloader.config import config as _cfg
        from downloader import polish
    except Exception:
        return rule_summary, rule_kw, "rule"
    base = str(_cfg.get("llm_base_url") or "").strip()
    model = str(_cfg.get("llm_model") or "").strip()
    key = str(_cfg.get("llm_api_key") or "").strip()
    if not base or not model:
        return rule_summary, rule_kw, "rule"

    texts = [str(s.get("text") or "").strip() for s in segments if s.get("text")]
    if not texts:
        return rule_summary, rule_kw, "rule"
    try:
        from .quality import sample_for_llm
        sketch = sample_for_llm(texts, budget=2400)
    except Exception:
        sketch = " ".join(texts)[:2400]
    prompt = (
        "根据口播摘要输出 JSON：{\"summary\":\"不超过150字中文摘要\",\"keywords\":[\"词1\",…]}。"
        "keywords 5–8 个。不要解释。\n"
        f"摘要：\n{sketch}"
    )
    try:
        raw = polish.chat_stream(
            base, key, model,
            [
                {"role": "system", "content": "你是中文知识稿助手。只输出合法 JSON。"},
                {"role": "user", "content": prompt},
            ],
            max_tokens=280,
            temperature=0.2,
        )
    except Exception:
        return rule_summary, rule_kw, "rule"
    text = (raw or "").strip()
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return rule_summary, rule_kw, "rule"
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return rule_summary, rule_kw, "rule"
    if not isinstance(obj, dict):
        return rule_summary, rule_kw, "rule"
    summary = str(obj.get("summary") or "").strip()[:200]
    raw_kw = obj.get("keywords") or []
    if isinstance(raw_kw, str):
        raw_kw = re.split(r"[,，、;；\s]+", raw_kw)
    keywords = [str(x).strip()[:24] for x in raw_kw if str(x).strip()][:8]
    if not summary:
        return rule_summary, rule_kw, "rule"
    return summary, keywords or rule_kw, "llm"


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


def _wait_checkpoint_flush(
    ckpt_dir: str,
    resume_task_id: str,
    asr_task_id: str = "",
    *,
    budget: float | None = None,
) -> bool:
    """取消后等 worker 把已转写内容落盘，返回是否拿到可续跑检查点。

    ``cancel_task`` 会**立刻**把 ASR 任务置成 cancelled —— 那是给 UI 看的，
    不代表 worker 已经把内容写完。worker 只在 chunk 边界（以及块内每 15s）
    落盘，两者之间有时间差。这个函数把这段窗口补上：

    * 已经存在检查点 → 直接返回 True（块内 15s 落盘的功劳，绝大多数情况走这条）；
    * 否则轮询到常驻池空闲或超出预算为止。

    预算默认 30s：块内 15s 一次的落盘意味着「刚取消就查不到检查点」只可能
    发生在开跑 15s 内，30s 足够覆盖；再久就是在转一个大块，没必要让用户干等。
    """
    if not ckpt_dir or not resume_task_id:
        return False
    try:
        from . import checkpoint as _ckpt  # noqa: WPS433
    except Exception:
        return False
    if _ckpt.load_checkpoint(ckpt_dir, resume_task_id):
        return True
    try:
        limit = float(os.environ.get("AVD_ASR_CANCEL_FLUSH_SECS", "") or 0) or 30.0
    except (TypeError, ValueError):
        limit = 30.0
    if budget is not None:
        limit = budget
    deadline = time.time() + max(0.0, limit)
    seen_active = False
    while time.time() < deadline:
        time.sleep(0.2)
        if _ckpt.load_checkpoint(ckpt_dir, resume_task_id):
            return True
        if asr_task_id:
            try:
                from downloader import asr_pool  # noqa: WPS433
                active = asr_pool.get_pool().active_task_id
            except Exception:
                active = None
            if active == asr_task_id:
                # 还在这单手里：worker 正在往边界跑，继续等
                seen_active = True
            elif seen_active:
                # 先见过它占用、现在空了 —— worker 已经走到边界并落盘
                return bool(_ckpt.load_checkpoint(ckpt_dir, resume_task_id))
    return bool(_ckpt.load_checkpoint(ckpt_dir, resume_task_id))


def _asr_via_original(
    url: str,
    language: str,
    title: str,
    media_path: str = "",
    *,
    cancel_check: Callable[[], bool] | None = None,
    on_task_id: Callable[[str], None] | None = None,
    on_progress: Callable[[float, str], None] | None = None,
    deadline: float | None = None,
    ckpt_dir: str = "",
    resume_task_id: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """无字幕时接回原版 ASR 管理器。

    返回 ``(segments, meta)``；``meta`` 含 ``no_speech`` / ``stage`` 等诊断字段
    （DIAG-01：区分「无人声」与「环境故障」）。
    支持协作式取消：``cancel_check`` 为真时调用 ``asr.manager.cancel_task``。
    ``on_progress(mapped_percent, message)`` 上报块级细进度。
    ``ckpt_dir`` / ``resume_task_id``：R4 持久检查点（流水线产物目录 + 可续跑键）。
    """
    from downloader import asr  # noqa: WPS433

    # R2：把「包内预置权重」装配进环境。boot.py 里已经做过一次，但
    # verify 脚本 / HTTP 服务被直接 import 时会绕过启动器 —— 那样 worker
    # 会去 HuggingFace 找 large-v3-turbo（内网直接失败，公网白等几十秒）。
    # configure() 幂等且不覆盖显式设置的值。
    try:
        from . import asr_offline  # noqa: WPS433
        asr_offline.configure()
    except Exception as exc:  # noqa: BLE001
        # 装配失败不阻断：转写仍可走用户已装环境，但必须留下可诊断线索
        avd_log.warning("asr_offline configure failed", error=str(exc))
    empty_meta: dict[str, Any] = {"no_speech": False, "stage": "", "env_ready": asr.is_available()}
    if not asr.is_available():
        empty_meta["stage"] = "env_missing"
        return [], empty_meta
    if cancel_check and cancel_check():
        return [], {**empty_meta, "stage": "cancelled"}
    local = Path(media_path) if media_path and Path(media_path).is_file() else None
    engine = (os.environ.get("AVD_ASR_ENGINE") or "auto").strip() or "auto"
    kwargs = dict(
        language=language, title=title, mode="asr", engine=engine,
        ckpt_dir=ckpt_dir, resume_task_id=resume_task_id,
    )
    if local:
        task = asr.manager.create_task(str(local), is_url=False, **kwargs)
    else:
        task = asr.manager.create_task(url, is_url=True, **kwargs)
    task_id = str(task.get("id") or "")
    if not task_id:
        return [], empty_meta
    if on_task_id:
        on_task_id(task_id)
    hard_deadline = deadline if deadline is not None else (time.time() + 900)
    last_pct = -1.0
    last_msg = ""
    while time.time() < hard_deadline:
        if cancel_check and cancel_check():
            try:
                asr.manager.cancel_task(task_id)
            except Exception as exc:  # noqa: BLE001
                avd_log.warning("asr cancel failed", task=task_id, error=str(exc))
            # 关键：cancel_task 是**乐观置终态**（立刻把 stage 写成 cancelled），
            # worker 其实还在写检查点。若这里直接返回，调用方马上查「可续跑」
            # 会查到空 —— 用户点重试就从 0 重来，等于取消白取消。
            # 这里等一小段时间，让 worker 走到 chunk 边界把已转写内容落盘。
            flushed = _wait_checkpoint_flush(ckpt_dir, resume_task_id, task_id)
            meta = {**empty_meta, "stage": "cancelled", "flushed": flushed}
            if flushed:
                try:
                    from . import checkpoint as _ckpt  # noqa: WPS433
                    snap = _ckpt.load_checkpoint(ckpt_dir, resume_task_id) or {}
                    meta["resume_at"] = round(float(
                        max([0.0] + [float(s.get("end") or 0)
                                     for s in (snap.get("segments") or [])])), 2)
                    meta["resume_chunk"] = int(snap.get("chunk_index") or 0) + 1
                except Exception as exc:  # noqa: BLE001
                    avd_log.warning("resume checkpoint unreadable",
                                    task=task_id, error=str(exc))
            return [], meta
        snapshot = asr.manager.get_task(task_id) or {}
        if not snapshot:
            return [], empty_meta
        # 块级细进度：ASR 0–100 → 流水线 transcript 区间 70→84
        try:
            asr_pct = float(snapshot.get("percent") or 0)
        except (TypeError, ValueError):
            asr_pct = 0.0
        # 一条消息只要变了就得往上送：排队位次、从检查点续跑、模型加载中……
        # 这些事件百分比不动，早期只看百分比会让 UI 长时间停在同一条文案上。
        msg = str(snapshot.get("message") or "")
        if on_progress and (abs(asr_pct - last_pct) >= 0.5 or (msg and msg != last_msg)):
            last_pct = asr_pct
            last_msg = msg
            mapped = 70.0 + (84.0 - 70.0) * max(0.0, min(100.0, asr_pct)) / 100.0
            on_progress(round(mapped, 1), msg)
        if snapshot.get("stage") in ("done", "error", "cancelled"):
            if snapshot.get("stage") != "done":
                return [], {**empty_meta, "stage": snapshot.get("stage"), "error": snapshot.get("error")}
            segments = list(asr.manager.get_segments(task_id))
            no_speech = bool(snapshot.get("no_speech")) or (
                snapshot.get("stage") == "done" and len(segments) == 0
            )
            return segments, {
                "no_speech": no_speech,
                "stage": "done",
                "env_ready": True,
                "segment_count": len(segments),
            }
        time.sleep(0.35)
    try:
        asr.manager.cancel_task(task_id)
    except Exception as exc:  # noqa: BLE001
        avd_log.warning("asr cancel failed on timeout", task=task_id, error=str(exc))
    return [], {**empty_meta, "stage": "timeout"}


class PipelineManager:
    """任务表 + 后台线程。线程安全，支持注入各阶段以便离线单测。"""

    _sm = StageMachine("pipeline")

    def __init__(
        self,
        *,
        probe_fn: Callable[[str], dict[str, Any]] | None = None,
        download_fn: Callable[[dict[str, Any], Path], str] | None = None,
        subtitle_fn: Callable[[str, str], list[dict[str, Any]]] | None = None,
        asr_fn: Callable[..., Any] | None = None,
        output_root: Path | None = None,
        max_concurrent: int = MAX_CONCURRENT,
    ) -> None:
        self._probe = probe_fn or _probe_once
        self._download = download_fn or _download_once
        self._subtitle = subtitle_fn or _fetch_subtitle_track
        self._asr = asr_fn or _asr_via_original
        self.output_root = Path(output_root) if output_root else OUTPUT_ROOT
        self._tasks: dict[str, PipelineTask] = {}
        self._cancelled: set[str] = set()
        self._lock = threading.RLock()
        self._sem = threading.Semaphore(max(1, max_concurrent))
        self._last_purge = 0.0
        self.output_root.mkdir(parents=True, exist_ok=True)
        self._purge_stale(force=True)
        self._load_index()

    def _index_path(self) -> Path:
        return self.output_root / "index.json"

    def _load_index(self) -> None:
        """MISC-04：从 SQLite（优先）与磁盘索引恢复已完成任务快照。"""
        rows: list[dict[str, Any]] = []
        try:
            rows = persist.load_tasks("pipeline", limit=MAX_TASKS)
        except Exception:
            rows = []
        if not rows:
            path = self._index_path()
            if path.is_file():
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(raw, list):
                        rows = [r for r in raw if isinstance(r, dict)]
                except (json.JSONDecodeError, OSError):
                    rows = []
        with self._lock:
            for row in rows[-MAX_TASKS:]:
                if not isinstance(row, dict) or not row.get("id"):
                    continue
                tid = str(row["id"])
                if tid in self._tasks:
                    continue
                task = PipelineTask(
                    id=tid,
                    url=str(row.get("url") or ""),
                    stage=str(row.get("stage") or "done"),
                    percent=float(row.get("percent") or 100.0),
                    message=str(row.get("message") or ""),
                    source_kind=str(row.get("source_kind") or ""),
                    summary=str(row.get("summary") or ""),
                    keywords=list(row.get("keywords") or []),
                    files=dict(row.get("files") or {}),
                    degraded=list(row.get("degraded") or []),
                    degradations=[
                        dict(d) for d in (row.get("degraded_items") or [])
                        if isinstance(d, dict)
                    ],
                    error=str(row.get("error") or ""),
                    created_at=float(row.get("created_at") or time.time()),
                    finished_at=row.get("finished_at"),
                )
                task.meta = dict(row.get("_meta") or row.get("meta") or {})
                if not task.meta.get("title"):
                    task.meta["title"] = str(row.get("title") or "")
                if row.get("chapters") and not task.meta.get("chapters"):
                    task.meta["chapters"] = [dict(c) for c in row["chapters"]
                                             if isinstance(c, dict)]
                self._tasks[tid] = task

    def _persist_index(self) -> None:
        with self._lock:
            rows = []
            for t in self._tasks.values():
                if t.stage not in TERMINAL:
                    continue
                row = t.to_dict()
                # 快照面向 API（不给 meta 全文），落库要能重建任务：
                # 章节 / 要点 / 时间戳索引 / 说话人统计都在 meta 里，另存一列。
                row["_meta"] = dict(t.meta or {})
                rows.append(row)
            rows.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
            rows = rows[:MAX_TASKS]
        persist_failed = False
        for row in rows:
            try:
                persist.save_task("pipeline", row)
            except Exception as exc:  # noqa: BLE001
                # D25：落库失败必须在任务上可见，而不是只吞进日志。
                persist_failed = True
                avd_log.warning(
                    "pipeline persist failed", task=row.get("id"), error=str(exc))
                task = self._tasks.get(str(row.get("id") or ""))
                if task is not None and not any(
                    d.get("code") == "persist.failed" for d in task.degradations
                ):
                    self._degrade(
                        task, "persist.failed", f"任务历史落库失败：{exc}")
        try:
            self._index_path().write_text(
                json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
            if not persist_failed:
                persist.note_success()
        except OSError as exc:
            persist.note_failure(exc, op="index.json")
            avd_log.warning("pipeline index write failed", error=str(exc))

    def reconcile(self) -> dict[str, Any]:
        """D27：以产物目录为真相，对账内存 / index.json / SQLite。

        - 磁盘有 ``manifest.json``、内存没有 → 重建为已完成任务（历史可恢复）
        - 内存有条目、产物目录已不存在 → 标 ``state.missing`` 降级，记录不删
        返回统计供 ``POST /api/fusion/reconcile`` 与诊断包使用。
        """
        restored = 0
        missing = 0
        scanned = 0
        root = self.output_root
        try:
            dirs = [p for p in root.iterdir() if p.is_dir()]
        except OSError as exc:
            avd_log.warning("reconcile scan failed", error=str(exc))
            dirs = []
        with self._lock:
            known = set(self._tasks)
        for work_dir in dirs:
            manifest = work_dir / "manifest.json"
            if work_dir.name in known or not manifest.is_file():
                continue
            scanned += 1
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                avd_log.warning("reconcile manifest unreadable",
                                path=str(manifest), error=str(exc))
                continue
            snap = data.get("task") if isinstance(data, dict) else None
            if not isinstance(snap, dict) or not snap.get("id"):
                continue
            tid = str(snap["id"])
            # manifest 在 knowledge 阶段写入，随后才置 done；只要产物齐全，
            # 恢复时统一视为已完成（error / cancelled 保持原状）。
            stage = str(snap.get("stage") or "done")
            if stage not in TERMINAL:
                stage = "done"
            task = PipelineTask(
                id=tid,
                url=str(snap.get("url") or ""),
                stage=stage,
                percent=float(snap.get("percent") or 100.0),
                message=str(snap.get("message") or "已从产物目录恢复"),
                source_kind=str(snap.get("source_kind") or ""),
                summary=str(snap.get("summary") or ""),
                keywords=list(snap.get("keywords") or []),
                files=dict(snap.get("files") or {}),
                degraded=list(snap.get("degraded") or []),
                degradations=[dict(d) for d in (snap.get("degraded_items") or [])
                              if isinstance(d, dict)],
                error=str(snap.get("error") or ""),
                created_at=float(snap.get("created_at") or time.time()),
                finished_at=snap.get("finished_at"),
            )
            task.meta["title"] = str(snap.get("title") or "")
            with self._lock:
                if tid in self._tasks:
                    continue
                self._tasks[tid] = task
            restored += 1

        with self._lock:
            tasks = list(self._tasks.values())
        for task in tasks:
            if task.stage not in TERMINAL:
                continue
            if (root / task.id).is_dir():
                continue
            missing += 1
            if not any(d.get("code") == "state.missing" for d in task.degradations):
                self._degrade(
                    task, "state.missing", "产物目录已不存在（可能被清理或移动）")
        self._persist_index()
        return {"restored": restored, "missing": missing,
                "scanned": scanned, "total": len(tasks)}

    def _purge_stale(self, *, force: bool = False) -> None:
        """OPS-01：按 TTL 清理过期任务目录；裁剪内存任务表；顺带清持久化过期行。

        P1（第九轮）：此前 ``apply_retention(outputs_root=self.output_root.parent)``
        把裁剪范围放大到 outputs 的**父目录**——单测临时根的父目录是
        ``/var/folders/T``（10 万条目），single-run 10.5s、402k 次 glob、并会
        unlink 兄弟目录媒体。现在裁剪范围严格等于本管理器的 ``output_root``。
        同时该清理不再挂在 ``_enqueue`` 热路径上（每提交一个任务都全树 stat），
        改为按最小间隔节流，只在构造与到期时执行。
        """
        now = time.time()
        if not force and (now - float(self._last_purge or 0.0)) < _PURGE_MIN_INTERVAL:
            return
        self._last_purge = now
        ttl = float(TASK_TTL_SECONDS)
        try:
            policy = persist.retention_policy()
            ttl = float(policy.get("ttl_seconds") or ttl)
            persist.apply_retention(outputs_root=self.output_root)
        except Exception as exc:  # noqa: BLE001
            avd_log.warning("retention policy failed", error=str(exc))
            try:
                persist.purge_older_than(ttl)
            except Exception as exc2:  # noqa: BLE001
                avd_log.warning("retention fallback purge failed", error=str(exc2))
        if self.output_root.is_dir():
            for child in list(self.output_root.iterdir()):
                if not child.is_dir():
                    continue
                try:
                    age = now - child.stat().st_mtime
                except OSError:
                    continue
                if age > ttl:
                    shutil.rmtree(child, ignore_errors=True)
        with self._lock:
            stale = [
                tid for tid, t in self._tasks.items()
                if t.finished_at and (now - float(t.finished_at)) > ttl
            ]
            for tid in stale:
                self._tasks.pop(tid, None)
            if len(self._tasks) > MAX_TASKS:
                ordered = sorted(self._tasks.values(), key=lambda t: t.created_at)
                for task in ordered[: max(0, len(ordered) - MAX_TASKS)]:
                    self._tasks.pop(task.id, None)

    def start_local(
        self,
        media_path: str,
        *,
        title: str = "",
        language: str = "zh",
        want_polish: bool = True,
    ) -> dict[str, Any]:
        """D11：本地文件走完整知识稿流水线（跳过 URL 解析与下载）。"""
        path = Path(media_path).expanduser().resolve()
        if not path.is_file():
            raise ValueError("本地文件不存在")
        # 必须落在上传沙箱或本机可读路径；拒绝穿越到系统敏感目录之外由调用方保证
        stem = (title or path.stem or "本地素材").strip()[:120]
        self._purge_stale()
        task = PipelineTask(
            id=uuid.uuid4().hex[:12],
            url=f"local://{path.name}",
            language=language,
            want_download=False,
            want_polish=want_polish,
            stage="queued",
            message="排队中（本地文件）…",
            media_path=str(path),
            local_media=str(path),
            meta={"title": stem, "extractor": "local", "webpage_url": "", "local_file": True},
        )
        with self._lock:
            if len(self._tasks) >= MAX_TASKS:
                raise RuntimeError(f"任务数已达上限（{MAX_TASKS}），请清理后再试")
            self._tasks[task.id] = task
        worker = threading.Thread(
            target=self._run_gated, args=(task,), name=f"avd-pipeline-{task.id}", daemon=True
        )
        worker.start()
        avd_log.info("pipeline local queued", task=task.id, path=str(path))
        return task.to_dict()

    def start(
        self,
        url: str,
        *,
        language: str = "zh",
        want_download: bool = True,
        want_polish: bool = True,
        expand_playlist: bool | None = None,
        template: str = "",
    ) -> dict[str, Any]:
        clean = (url or "").strip()
        ok, reason = guards.check_media_url(clean)
        if not ok:
            raise ValueError(reason)
        # 播放列表展开：配置 >1 或显式请求时，拆成批量队列（本方法返回首条快照）
        limit = _playlist_limit()
        do_expand = expand_playlist if expand_playlist is not None else (limit > 1)
        if do_expand and limit > 1:
            entries = list_playlist_entries(clean, limit=limit)
            if len(entries) > 1:
                batch = self.start_batch(
                    [e["url"] for e in entries],
                    language=language,
                    want_download=want_download,
                    want_polish=want_polish,
                    template=template,
                )
                first = (batch.get("tasks") or [None])[0]
                if first:
                    first = dict(first)
                    first["batch"] = {
                        "count": batch.get("count"),
                        "skipped": batch.get("skipped"),
                        "ids": [t["id"] for t in batch.get("tasks") or []],
                    }
                    return first
        return self._enqueue(
            clean, language=language,
            want_download=want_download, want_polish=want_polish,
            template=template,
        )

    def start_batch(
        self,
        urls: list[str],
        *,
        language: str = "zh",
        want_download: bool = True,
        want_polish: bool = True,
        delay_seconds: Any = None,
        run_at: Any = None,
        template: str = "",
    ) -> dict[str, Any]:
        """多 URL 排队：单条失败不影响其余；并发由信号量控制。

        delay_seconds / run_at：整批共用同一「不早于」时刻（定时调度）。
        """
        not_before = sched_mod.parse_not_before(
            delay_seconds=delay_seconds, run_at=run_at,
        )
        cleaned: list[str] = []
        skipped: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw in urls:
            u = str(raw or "").strip()
            if not u:
                continue
            if u in seen:
                skipped.append({"url": u, "reason": "重复链接"})
                continue
            seen.add(u)
            ok, reason = guards.check_media_url(u)
            if not ok:
                skipped.append({"url": u, "reason": reason})
                continue
            cleaned.append(u)
            if len(cleaned) >= MAX_BATCH:
                break
        if not cleaned:
            raise ValueError("没有可启动的有效链接（请检查白名单与 URL 格式）")
        tasks = [
            self._enqueue(
                u, language=language,
                want_download=want_download, want_polish=want_polish,
                not_before=not_before, template=template,
            )
            for u in cleaned
        ]
        avd_log.info(
            "batch start", count=len(tasks), skipped=len(skipped),
            not_before=not_before,
        )
        return {
            "count": len(tasks), "skipped": skipped, "tasks": tasks,
            "not_before": not_before,
        }

    def _enqueue(
        self,
        url: str,
        *,
        language: str = "zh",
        want_download: bool = True,
        want_polish: bool = True,
        not_before: float | None = None,
        template: str = "",
    ) -> dict[str, Any]:
        self._purge_stale()  # 已节流：≥300s 才真跑，不再是每单一次全树 stat
        msg = "排队中…"
        if not_before is not None and not_before > time.time():
            wait = int(not_before - time.time())
            msg = f"定时等待中（约 {wait}s 后启动）…"
        task = PipelineTask(
            id=uuid.uuid4().hex[:12], url=url, language=language,
            want_download=want_download, want_polish=want_polish,
            stage="queued", message=msg, not_before=not_before,
        )
        if template:
            task.meta["template"] = str(template)
        with self._lock:
            if len(self._tasks) >= MAX_TASKS:
                raise RuntimeError(f"任务数已达上限（{MAX_TASKS}），请清理后再试")
            self._tasks[task.id] = task
        worker = threading.Thread(
            target=self._run_gated, args=(task,), name=f"avd-pipeline-{task.id}", daemon=True
        )
        worker.start()
        avd_log.info("pipeline queued", task=task.id, url=url, not_before=not_before)
        return task.to_dict()

    def _run_gated(self, task: PipelineTask) -> None:
        """CONC-01：信号量限流；拿不到槽位时保持 queued；支持定时等待。"""
        # 定时：先等到点再抢槽位，避免占满并发
        if task.not_before is not None:
            self._set(task, message="定时等待中…")
            sched_mod.wait_until(
                task.not_before,
                aborted=lambda: self._aborted(task),
            )
            if self._aborted(task):
                self._mark_cancelled(task)
                self._persist_index()
                return
            self._set(task, message="排队中…")
        acquired = self._sem.acquire(timeout=600)
        if not acquired:
            self._set(task, stage="error", error="排队超时",
                      message="并发已满，排队超时", percent=100.0, finished_at=time.time())
            self._persist_index()
            return
        try:
            if task.id in self._cancelled:
                return
            self._run(task)
        finally:
            self._sem.release()
            self._persist_index()

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return task.to_dict() if task else None

    def _artifact_exists(self, task: PipelineTask) -> bool:
        """产物是否仍在盘上（至少有一份 files 指向的真实文件）。"""
        if not task.files:
            return task.stage not in TERMINAL
        for rel in task.files.values():
            if not rel:
                continue
            path = self.output_root / rel
            safe = guards.within(self.output_root, path)
            if safe is not None and safe.is_file():
                return True
        return False

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = [
                t.to_dict()
                for t in sorted(self._tasks.values(), key=lambda t: t.created_at, reverse=True)
            ]
            tasks_by_id = {t.id: t for t in self._tasks.values()}
        for row in rows:
            task = tasks_by_id.get(str(row.get("id") or ""))
            if task is None:
                row["exists"] = False
                row["stale"] = True
                continue
            exists = self._artifact_exists(task)
            row["exists"] = exists
            # 终态且产物缺失 → stale（不删除，用户可能只是换了盘）
            row["stale"] = bool(task.stage in TERMINAL and not exists)
            if row["stale"] and not row.get("error"):
                row["message"] = "产物已删除"
        return rows

    def cancel(self, task_id: str) -> bool:
        """真取消：标记 + 终止关联 ASR 子进程；后台线程在阶段边界退出。"""
        asr_id = ""
        with self._lock:
            if task_id not in self._tasks:
                return False
            self._cancelled.add(task_id)
            task = self._tasks[task_id]
            asr_id = task.asr_task_id
            if task.stage not in TERMINAL:
                task.stage = "cancelled"
                task.percent = 100.0
                task.message = "已取消"
                task.finished_at = time.time()
        if asr_id:
            try:
                from downloader import asr as _asr_mod  # noqa: WPS433
                _asr_mod.manager.cancel_task(asr_id)
            except Exception as exc:  # noqa: BLE001
                avd_log.warning("asr cancel failed", task=asr_id, error=str(exc))
        return True

    def _rehydrate(self, task_id: str) -> "PipelineTask | None":
        """内存里没有这条任务时（崩溃 / 重启后），用检查点把它重建出来。

        实测缺陷：「可续跑」列表能列出 task_id，但 ``retry`` 直接抛「任务不存在」——
        因为 ``_persist_index`` 只落终态任务，崩在转写中途的任务从未进过库。
        列表是活的，入口是死的。这里从检查点（含来源 URL）重建最小任务，
        让「一键续跑」在它真正该起作用的场景里成立。
        """
        work_dir = Path(self.output_root) / task_id
        if not work_dir.is_dir():
            return None
        ck = checkpoint.load_checkpoint(work_dir, task_id)
        if not ck:
            return None
        source = str(ck.get("source") or "")
        if not source:
            scene = recovery.load_crash_scene(work_dir, task_id) or {}
            source = str(((scene.get("extra") or {}).get("url")) or "")
        if not source:
            # 老检查点没有来源字段（r5 之前写的），只能如实说清原因
            return None
        media = _existing_media(work_dir)
        task = PipelineTask(
            id=task_id,
            url=source,
            stage="cancelled",
            percent=0.0,
            message="从检查点恢复（进程重启后续跑）",
            created_at=float(ck.get("saved_at") or time.time()),
        )
        if media:
            task.media_path = media
        with self._lock:
            self._tasks.setdefault(task_id, task)
            return self._tasks.get(task_id)

    def retry(self, task_id: str) -> dict[str, Any]:
        """复用同一 task_id 重跑（检查点可续）；仅终态可重试。"""
        task = self._tasks.get(task_id)
        if task is None:
            task = self._rehydrate(task_id)
            if task is None:
                raise ValueError(
                    "该任务既不在内存、也没有带来源的检查点，无法续跑"
                    "（r5 之前的检查点未记录来源 URL）"
                )
        with self._lock:
            if task.stage not in TERMINAL:
                raise ValueError("任务仍在运行，请先取消或等待完成")
            self._cancelled.discard(task_id)
            task.stage = "queued"
            task.percent = 0.0
            task.message = "重试排队中…"
            task.error = ""
            task.degraded = []
            task.degradations = []
            task.diagnosis = {}
            task.finished_at = None
            task.asr_task_id = ""
            task.not_before = None
            snap = task.to_dict()
        threading.Thread(
            target=self._run_gated, args=(task,),
            name=f"avd-pipeline-retry-{task_id[:8]}", daemon=True,
        ).start()
        return snap

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
        if fmt in ("docx", "pdf"):
            import base64
            return base64.b64encode(safe.read_bytes()).decode("ascii")
        return safe.read_text(encoding="utf-8", errors="replace")

    def proofread(
        self,
        task_id: str,
        segments: list[dict[str, Any]],
        *,
        strict: bool = False,
    ) -> dict[str, Any]:
        """人工校对：用改过的分段重写 md/srt/txt/json，时间轴随文本长度微调。

        ``strict=True`` 时实体/字数超阈值硬回滚；默认仅写入 meta 告警，不阻断人工校对。
        """
        if not isinstance(segments, list) or not segments:
            raise ValueError("缺少校对分段")
        cleaned: list[dict[str, Any]] = []
        for raw in segments[:5000]:
            if not isinstance(raw, dict):
                continue
            text = str(raw.get("text") or "").strip()
            if not text:
                continue
            try:
                start = float(raw.get("start") or 0)
                end = float(raw.get("end") or (start + 1.0))
            except (TypeError, ValueError):
                continue
            if end <= start:
                end = start + 0.5
            cleaned.append({
                "start": round(start, 2),
                "end": round(end, 2),
                "text": text[:2000],
            })
        if not cleaned:
            raise ValueError("校对后没有有效句子")

        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise ValueError("任务不存在")
            if task.stage not in ("done",):
                raise ValueError("仅已完成任务可校对回写")
            meta = dict(task.meta or {})
            summary = task.summary
            keywords = list(task.keywords)
            source_kind = task.source_kind or "proofread"
            work_dir = self.output_root / task.id
            previous = list(task.segments or [])
            degraded = list(task.degraded or [])
            degradations = [dict(d) for d in (task.degradations or [])]

        # 保留 raw_text；一致性校验：strict 硬回滚，否则告警
        for i, seg in enumerate(cleaned):
            if i < len(previous):
                prev = previous[i]
                seg["raw_text"] = str(
                    prev.get("raw_text") or prev.get("text") or seg["text"]
                )
            else:
                seg.setdefault("raw_text", seg["text"])
        try:
            from . import quality
            check = quality.consistency_check(
                previous or cleaned, cleaned,
                char_ratio_min=0.45 if strict else 0.25,
                entity_keep_min=0.35 if strict else 0.10,
            )
            meta["proofread_check"] = check
            if not check.get("ok"):
                msg = (
                    "校对改动偏大："
                    f"字数比 {check.get('char_ratio')}，"
                    f"实体保留 {check.get('entity_keep')}"
                )
                if strict:
                    raise ValueError(msg + "（strict 模式已回滚）")
                item = degradation.make(
                    "insights.error", msg + "（已保存，请人工复核）",
                    level="warn", retriable=True,
                    action="回到校对页缩小改动幅度后重新保存",
                )
                if item.as_line() not in degraded:
                    degraded.append(item.as_line())
                    degradations.append(item.as_dict())
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001
            meta["proofread_check"] = {"ok": True, "skipped": str(exc)}

        # 按相邻边界钳制，避免时间轴交叉
        for i, seg in enumerate(cleaned):
            if i + 1 < len(cleaned) and cleaned[i + 1]["start"] <= seg["start"]:
                cleaned[i + 1]["start"] = seg["end"]
            if i + 1 < len(cleaned) and seg["end"] > cleaned[i + 1]["start"]:
                seg["end"] = max(seg["start"] + 0.4, cleaned[i + 1]["start"] - 0.05)

        meta["proofread"] = True
        bundle = knowledge.build_bundle(
            meta, cleaned, summary=summary, keywords=keywords,
            source_kind=source_kind or "proofread",
        )
        try:
            from . import quality
            if quality.dual_worth_emitting(cleaned):
                bundle["dual.md"] = quality.render_dual_column_md(meta, cleaned)
            else:
                # 校对后仍无实质差异：删掉可能残留的 dual 产物
                stem_rm = safe_stem(meta.get("title") or "", fallback=task_id)
                leftover = work_dir / f"{stem_rm}.dual.md"
                if leftover.is_file():
                    leftover.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001
            avd_log.warning("dual column render skipped", task=task_id, error=str(exc))
        stem = safe_stem(meta.get("title") or "", fallback=task_id)
        saved: dict[str, str] = {}
        work_dir.mkdir(parents=True, exist_ok=True)
        import base64
        for ext, content in bundle.items():
            safe_ext = str(ext).replace("/", ".")
            rel = f"{task_id}/{stem}.{safe_ext}"
            path = work_dir / f"{stem}.{safe_ext}"
            if safe_ext in ("docx", "pdf"):
                path.write_bytes(base64.b64decode(content.encode("ascii")))
            else:
                path.write_text(content, encoding="utf-8")
            saved[safe_ext] = rel
        with self._lock:
            task.segments = cleaned
            task.files = {**dict(task.files or {}), **saved}
            task.meta = meta
            task.degraded = degraded
            task.degradations = degradations
            task.message = "已校对回写"
            snap = task.to_dict()
        self._persist_index()
        avd_log.info("proofread saved", task=task_id, segments=len(cleaned))
        return snap

    def _aborted(self, task: PipelineTask) -> bool:
        with self._lock:
            return task.id in self._cancelled

    def _set(self, task: PipelineTask, **fields: Any) -> None:
        with self._lock:
            if task.id in self._cancelled and task.stage == "cancelled":
                return
            for key, value in fields.items():
                setattr(task, key, value)

    def _degrade(
        self,
        task: PipelineTask,
        code: str,
        cause: str,
        *,
        level: str | None = None,
        retriable: bool | None = None,
        action: str | None = None,
        prepend: bool = False,
    ) -> degradation.Degradation:
        """登记一条降级项（结构化 + 兼容旧字符串列表）。

        ``code`` 决定默认 level / retriable / action（见 ``degradation._SPECS``），
        ``cause`` 补本任务的具体原因。``prepend=True`` 用于把「信息量不足」这类
        总纲性结论排到最前面——它解释了后面所有降级项的由来。
        """
        item = degradation.make(
            code, cause, level=level, retriable=retriable, action=action)
        row = item.as_dict()
        with self._lock:
            if prepend:
                task.degraded.insert(0, item.as_line())
                task.degradations.insert(0, row)
            else:
                task.degraded.append(item.as_line())
                task.degradations.append(row)
        return item

    def _stage(self, task: PipelineTask, stage: str, message: str,
               *, timeout: float | None = None) -> None:
        if self._aborted(task):
            raise Cancelled()
        try:
            stage = self._sm.advance(task.stage, stage)
        except StageError as exc:
            avd_log.warning("stage rejected", task=task.id, error=str(exc),
                            from_stage=task.stage, to_stage=stage)
            # 宽松降级：仍推进业务（避免旧路径卡死），但记一条
            self._degrade(task, "stage.transition", f"阶段转移告警：{exc}")
        limit = timeout if timeout is not None else STAGE_TIMEOUT.get(stage, 300.0)
        self._set(task, stage=stage, percent=STAGE_WEIGHT.get(stage, 0.0),
                  message=message, _stage_deadline=time.time() + limit)

    def _tick(self, task: PipelineTask) -> None:
        """阶段边界检查：取消或看门狗超时。"""
        if self._aborted(task):
            raise Cancelled()
        deadline = task._stage_deadline
        if deadline is not None and time.time() > deadline:
            raise StageTimeout(
                f"阶段「{task.stage}」超时（>{STAGE_TIMEOUT.get(task.stage, 0):.0f}s），"
                "可换源或重试"
            )

    def _mark_cancelled(self, task: PipelineTask) -> None:
        with self._lock:
            self._cancelled.add(task.id)
            task.stage = "cancelled"
            task.percent = 100.0
            task.message = "已取消"
            task.finished_at = time.time()

    def _run(self, task: PipelineTask) -> None:
        work_dir = self.output_root / task.id
        try:
            work_dir.mkdir(parents=True, exist_ok=True)
            self._tick(task)

            info, meta, is_local = self._prepare_source(task, work_dir)

            # ARCH-01：默认下载（本地文件跳过）
            if not is_local and task.want_download:
                msg = "正在下载（复用已解析的 info）…"
                if info.get("is_live"):
                    live_secs = engine_opts.live_record_seconds()
                    if live_secs == -1:
                        msg = "正在录制直播（点取消结束）…"
                    else:
                        msg = f"正在限时录制直播片段（约 {live_secs} 秒）…"
                self._stage(task, "download", msg)
                try:
                    task.media_path = self._download(info, work_dir)
                except Exception as exc:
                    reuse = _existing_media(work_dir)
                    if reuse:
                        task.media_path = reuse
                        self._degrade(
                            task, "download.postprocess",
                            f"下载后处理告警：{exc}（媒体已落盘，直接复用，不重下）",
                        )
                    else:
                        self._degrade(task, "download.skip",
                                      f"下载阶段跳过：{exc}")
                self._tick(task)

            self._obtain_transcript(task, info, meta, is_local, work_dir)

            low_content = self._polish_and_annotate(task, meta)

            bundle, saved = self._render_knowledge(
                task, info, meta, low_content, work_dir)

            # 写入素材库 FTS（供跨任务检索 / 问答）
            try:
                from . import library
                from . import quality as _q_index
                library.upsert_material(
                    task_id=task.id,
                    title=str(meta.get("title") or task.id),
                    body=bundle.get("md") or bundle.get("txt") or "",
                    tags=list(meta.get("tags") or [])[:12],
                    collection=str(meta.get("extractor") or ""),
                    meta={
                        "source_kind": task.source_kind,
                        "webpage_url": meta.get("webpage_url"),
                        "duration": meta.get("duration"),
                        "chapters": meta.get("chapters") or [],
                        "timestamp_index": meta.get("timestamp_index") or [],
                        "segments": _q_index.sample_segments_for_index(
                            task.segments or [], char_budget=24_000,
                        ),
                    },
                )
            except Exception as exc:  # noqa: BLE001
                self._degrade(task, "library.index", f"素材库索引跳过：{exc}")

            self._finish_task(task, meta, bundle, saved)

        except Cancelled:
            self._mark_cancelled(task)
            avd_log.info("pipeline cancelled", task=task.id)
        except StageTimeout as exc:
            self._set(task, stage="error", error=str(exc), message=str(exc),
                      percent=100.0, finished_at=time.time())
            avd_log.warning("pipeline timeout", task=task.id, error=str(exc))
        except Exception as exc:
            if self._aborted(task):
                self._mark_cancelled(task)
                return
            item = diagnostics.diagnose(exc)
            self._set(task, stage="error", error=str(exc),
                      message=diagnostics.friendly(exc), diagnosis=item,
                      percent=100.0, finished_at=time.time())
            avd_log.error("pipeline error", task=task.id, error=str(exc))

    def _polish_and_annotate(self, task: PipelineTask,
                             meta: dict[str, Any]) -> bool:
        """D30 整理阶段：信息量分级、正文分段/摘要、可选说话人标注。"""
        # 素材信息量分级：先判断「这份素材能出什么」，再决定要不要出知识稿
        try:
            from . import quality as _quality
            suff = _quality.content_sufficiency(
                task.segments, duration=meta.get("duration"))
        except Exception:
            suff = {}
        low_content = bool(suff) and not suff.get("sufficient", True)
        if suff:
            meta["content_level"] = suff.get("level") or ("C" if low_content else "A")
            meta["content_sufficiency"] = suff
        if low_content:
            meta["low_content"] = True
            self._degrade(
                task, "content.low",
                suff.get("message") or "信息量不足", prepend=True,
            )
            self._set(task, meta=meta)
            avd_log.warning(
                "low content material", task=task.id,
                chars=suff.get("chars"), level=suff.get("level"),
            )

        # D10：正文分段走 polish.local_polish（去重/停顿分段/填充词），再出规则/LLM 摘要
        if task.segments and not low_content:
            self._stage(task, "polish", "正在整理正文分段与摘要…")
            try:
                from downloader import polish as _polish
                polished = _polish.local_polish(
                    task.segments, language=task.language or "zh",
                )
                paras = list(polished.get("paragraphs") or [])
                if paras:
                    meta["paragraphs"] = [
                        {
                            "start": p.get("start"),
                            "end": p.get("end"),
                            "text": p.get("text"),
                            "count": p.get("count"),
                        }
                        for p in paras
                    ]
                    # 回写去重/去填充后的句子，供后续说话人与索引使用
                    if polished.get("text"):
                        cleaned_segs = _polish.dedupe_segments(task.segments)
                        for seg in cleaned_segs:
                            seg["text"] = _polish.strip_leading_fillers(
                                str(seg.get("text") or "")
                            )
                            seg["text"] = _polish.normalize_punctuation(
                                str(seg.get("text") or ""),
                                language=task.language or "zh",
                            )
                        task.segments = [
                            s for s in cleaned_segs if str(s.get("text") or "").strip()
                        ]
            except Exception as exc:  # noqa: BLE001
                self._degrade(task, "polish.body", f"正文分段跳过：{exc}")

            # 实体/术语消歧：把 A.I. / Ai 之类的变体收敛到最高频写法
            try:
                from . import quality as _quality_terms
                task.segments, term_map = _quality_terms.normalize_terms(task.segments)
                if term_map:
                    meta["term_normalized"] = term_map
                    self._degrade(
                        task, "term.normalized",
                        f"术语消歧 {len(term_map)} 处："
                        + "、".join(f"{k}→{v}" for k, v in list(term_map.items())[:5]),
                    )
            except Exception as exc:  # noqa: BLE001
                self._degrade(task, "term.normalize", f"术语消歧跳过：{exc}")

            if task.want_polish:
                chars = sum(len(str(s.get("text") or "")) for s in task.segments)
                if chars >= 8000:
                    # D26：长素材一次性塞进 2400 字预算会丢后半程，
                    # 改走 map-reduce，并把覆盖度/出处写进快照。
                    from . import longform
                    result = longform.map_reduce_summary(
                        task.segments, use_llm=True)
                    task.summary = str(result.get("summary") or "")
                    task.keywords = list(result.get("keywords") or [])
                    meta["summary_kind"] = str(result.get("source") or "rule_mapreduce")
                    meta["summary_coverage"] = dict(result.get("coverage") or {})
                    meta["summary_provenance"] = list(result.get("provenance") or [])
                else:
                    summary, keywords, src = enrich_summary(task.segments, use_llm=True)
                    task.summary, task.keywords = summary, keywords
                    meta["summary_kind"] = src
            self._set(task, segments=task.segments, meta=meta)
            self._tick(task)

        # 可选说话人标注（配置 asr_diarize=true；有 pyannote 则神经网络）
        if task.segments and _diarize_enabled():
            task.segments = knowledge.assign_speakers(
                task.segments,
                audio_path=task.media_path or None,
            )
            labeled = any(str(s.get("speaker") or "").strip() for s in task.segments)
            neural = any(
                str(s.get("speaker_source") or "") == "pyannote" for s in task.segments
            )
            stats = knowledge.speaker_stats(task.segments)
            meta["speaker_stats"] = stats
            if labeled:
                meta["speakers"] = True
                meta["speaker_source"] = "pyannote" if neural else "heuristic"
                meta["speaker_confidence"] = stats.get("confidence") or 0
                self._set(task, segments=task.segments, meta=meta)
                if neural:
                    self._degrade(task, "speaker.neural",
                                  "已用 pyannote 神经网络标注说话人")
                else:
                    conf = stats.get("confidence") or 0
                    self._degrade(
                        task, "speaker.heuristic",
                        f"已按停顿启发式标注说话人 A/B（未校准 · 置信 {conf:.2f}；"
                        "不均衡/停顿不足会自动撤回）",
                    )
            else:
                self._degrade(
                    task, "speaker.inconclusive",
                    "说话人启发式：证据不足或 A/B 不均衡，未写入标签（避免独白误判）",
                )
                self._set(task, segments=task.segments, meta=meta)
        return low_content

    def _render_knowledge(self, task: PipelineTask, info: dict[str, Any],
                          meta: dict[str, Any], low_content: bool,
                          work_dir: Path) -> tuple[dict[str, Any], dict[str, str]]:
        """D30 渲染阶段：章节/要点 → 产物矩阵 → 落盘与 manifest。"""
        self._stage(task, "knowledge", "正在生成结构化知识稿…")
        # 伪章节：平台无 chapters 且有正文时自动分段（含 TF-IDF / LLM 标题）
        if not (meta.get("chapters") or []) and task.segments and not low_content:
            auto, title_src = knowledge.synthesize_chapters(task.segments)
            if auto:
                meta["chapters"] = [
                    {"title": c.title, "start": c.start, "end": c.end} for c in auto
                ]
                meta["chapters_auto"] = True
                meta["chapter_title_source"] = title_src
                self._degrade(
                    task, "chapter.auto",
                    f"平台未提供章节，已按停顿自动分成 {len(auto)} 段（标注「自动分段」）",
                )
                self._set(task, meta=meta)

        # 章节质量评估：空章/过短/倒序/越界/重名/覆盖空洞，出稿前给可观测分数
        if meta.get("chapters"):
            try:
                from . import quality as _quality_ch
                cq = _quality_ch.chapter_quality(meta["chapters"], task.segments)
                meta["chapter_quality"] = cq
                if not cq.get("ok"):
                    first = (cq.get("issues") or [{}])[0].get("detail") or ""
                    self._degrade(
                        task, "chapter.quality",
                        f"章节质量 {cq.get('score')} 分：{first}",
                    )
            except Exception as exc:  # noqa: BLE001
                self._degrade(task, "chapter.quality", f"章节质量评估跳过：{exc}")

        # 要点 / 术语 / 时间戳索引（规则兜底；有 LLM 时一次调用增强）
        try:
            from . import quality
            if low_content:
                # 信息量不足：不出摘要/要点/术语表，只留元数据与转写原文
                meta["key_points"] = []
                meta["glossary"] = []
                meta["insights_source"] = "skipped_low_content"
                meta["timestamp_index"] = quality.timestamp_index(task.segments)
            else:
                kp, gloss, src = quality.enrich_insights(task.segments, use_llm=True)
                meta["key_points"] = kp
                meta["glossary"] = gloss
                meta["insights_source"] = src
                meta["timestamp_index"] = quality.timestamp_index(task.segments)
            # 保留 raw_text：校对后仍可双栏
            for seg in task.segments:
                if "raw_text" not in seg:
                    seg["raw_text"] = str(seg.get("text") or "")
        except Exception as exc:  # noqa: BLE001
            self._degrade(task, "insights.error", f"质量增强跳过：{exc}")

        sponsor = knowledge.merge_ranges(
            knowledge.sponsor_ranges(info),
            knowledge.heuristic_promo_ranges(task.segments),
        )
        if sponsor:
            self._degrade(
                task, "sponsor.removed",
                f"已剔除 {len(sponsor)} 段口播/引流（SponsorBlock 字段或启发式话术）",
            )
        bundle = knowledge.build_bundle(
            meta, task.segments, summary=task.summary, keywords=task.keywords,
            source_kind=task.source_kind, sponsor=sponsor,
        )
        # D18：按 content_level 裁剪产物键；D12：未校对无差异时不出 dual
        level = str(meta.get("content_level") or ("C" if low_content else "A")).upper()
        if level == "C":
            keep = {"md", "srt", "json"}
            bundle = {k: v for k, v in bundle.items() if k in keep}
        elif level == "B":
            bundle.pop("dual.md", None)
        else:
            try:
                from . import quality
                if (
                    meta.get("proofread")
                    and quality.dual_worth_emitting(task.segments)
                ):
                    bundle["dual.md"] = quality.render_dual_column_md(
                        meta, task.segments,
                    )
            except Exception as exc:  # noqa: BLE001
                self._degrade(task, "proofread.dual", f"双栏校对稿跳过：{exc}")
        stem = safe_stem(meta.get("title") or "", fallback=task.id)
        saved: dict[str, str] = {}
        import base64
        for ext, content in bundle.items():
            safe_ext = ext.replace("/", ".")
            rel = f"{task.id}/{stem}.{safe_ext}"
            path = work_dir / f"{stem}.{safe_ext}"
            if ext in ("docx", "pdf") or safe_ext in ("docx", "pdf"):
                path.write_bytes(base64.b64decode(content.encode("ascii")))
            else:
                path.write_text(content, encoding="utf-8")
            saved[safe_ext] = rel
        # 先回填 files 再写 manifest：manifest 是 D27 对账的唯一真相，
        # 若先写快照再改 files，恢复出的任务会「已完成但没有产物清单」。
        self._set(task, files=saved)
        (work_dir / "manifest.json").write_text(
            json.dumps({"task": task.to_dict(), "generator": "AVD Flagship v2.0.0",
                        "engine": "yt-dlp 2026.10.26 (vendored unmodified)"},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        self._tick(task)
        return bundle, saved

    def _obtain_transcript(self, task: PipelineTask, info: dict[str, Any],
                           meta: dict[str, Any], is_local: bool,
                           work_dir: Path) -> None:
        """D30 转写阶段：字幕优先，缺失时下载音频并走本地 ASR。"""
        # 转写超时：至少 STAGE_TIMEOUT，或按时长 ×3；直播按录制上限放大
        duration = float(meta.get("duration") or 0) or 0.0
        if info.get("is_live"):
            live_secs = engine_opts.live_record_seconds()
            if live_secs > 0:
                duration = max(duration, float(live_secs))
            elif live_secs == -1:
                duration = max(duration, 3600.0)  # 看门狗至少留 1h 量级
        transcript_timeout = max(STAGE_TIMEOUT["transcript"], duration * 3.0 + 60.0)
        self._stage(
            task, "transcript",
            "正在本地转写…" if is_local else "正在取得文字稿（字幕优先）…",
            timeout=transcript_timeout,
        )
        if is_local:
            cues = []
        else:
            track_url, fmt, kind = _pick_subtitle_track(info, task.language)
            if track_url:
                try:
                    cues = self._subtitle(track_url, fmt)
                except Exception as exc:
                    cues = []
                    self._degrade(task, "subtitle.fetch", f"字幕抓取失败：{exc}")
            else:
                cues = []
        self._tick(task)
        if cues:
            task.segments = cues
            task.source_kind = kind
        else:
            if not is_local:
                self._degrade(task, "subtitle.absent", "平台未提供字幕")
            if not task.media_path:
                # 先认盘：下载阶段可能已落盘却因后处理异常没回传路径
                task.media_path = _existing_media(work_dir)
            if not task.media_path and not is_local:
                self._stage(task, "download",
                            "字幕不可用，下载音频供转写（复用已解析 info）…")
                try:
                    task.media_path = self._download(info, work_dir)
                except Exception as exc:
                    reuse = _existing_media(work_dir)
                    if reuse:
                        task.media_path = reuse
                        self._degrade(
                            task, "download.postprocess",
                            f"音频下载告警：{exc}（媒体已在盘上，转写继续）",
                        )
                    else:
                        self._degrade(task, "media.download",
                                      f"音频下载失败：{exc}")
                self._tick(task)
                self._stage(task, "transcript", "正在语音转写…",
                            timeout=transcript_timeout)
            audio = has_audio_stream(task.media_path) if task.media_path else None
            if audio is False:
                self._degrade(
                    task, "media.no_audio",
                    "该素材不含音轨，无法语音转写（本次仅输出元数据骨架）",
                )
                cues = []
            else:
                asr_meta: dict[str, Any] = {}
                try:
                    def _on_asr_id(tid: str) -> None:
                        task.asr_task_id = tid

                    def _on_asr_prog(pct: float, msg: str) -> None:
                        self._set(
                            task, percent=pct,
                            message=msg or task.message,
                        )

                    result = self._asr(
                        task.url, task.language, meta.get("title", ""),
                        task.media_path,
                        cancel_check=lambda: self._aborted(task),
                        on_task_id=_on_asr_id,
                        on_progress=_on_asr_prog,
                        deadline=task._stage_deadline,
                        ckpt_dir=str(work_dir),
                        resume_task_id=task.id,
                    )
                    if isinstance(result, tuple):
                        cues, asr_meta = result[0], dict(result[1] or {})
                    else:
                        cues = list(result or [])
                except TypeError:
                    # 注入的 asr_fn 可能不接受新关键字参数
                    result = self._asr(task.url, task.language,
                                       meta.get("title", ""), task.media_path)
                    if isinstance(result, tuple):
                        cues, asr_meta = result[0], dict(result[1] or {})
                    else:
                        cues = list(result or [])
                except Exception as exc:
                    cues = []
                    asr_meta = {"stage": "engine_error", "error": str(exc)}
                    self._degrade(task, "asr.failed",
                                  f"语音识别调用失败：{exc}")
                    try:
                        from . import recovery as _rec
                        _rec.save_crash_scene(
                            work_dir, task_id=task.id,
                            reason="pipeline asr exception", exc=exc,
                        )
                    except Exception as scene_exc:  # noqa: BLE001
                        avd_log.warning("crash scene save failed", task=task.id,
                                        error=str(scene_exc))
                self._tick(task)
                if asr_meta.get("stage") == "cancelled" or self._aborted(task):
                    raise Cancelled()
                if cues:
                    task.segments = cues
                    task.source_kind = "asr"
                else:
                    if asr_meta.get("no_speech") or asr_meta.get("stage") == "done":
                        self._degrade(
                            task, "asr.no_speech",
                            "这段素材没有人声（或已被 VAD 滤掉），已输出元数据骨架；"
                            "无需重装模型",
                        )
                    elif asr_meta.get("stage") == "env_missing" or asr_meta.get("env_ready") is False:
                        self._degrade(
                            task, "asr.env_missing",
                            "语音识别环境未就绪：运行 python3 doctor.py --repair，"
                            "或检查 .venv-asr 运行时；本次仅输出元数据骨架",
                        )
                    elif asr_meta.get("stage") == "timeout":
                        self._degrade(
                            task, "asr.timeout",
                            "语音识别超时；可换源或重试。本次仅输出元数据骨架",
                        )
                    else:
                        self._degrade(
                            task, "asr.failed",
                            "语音识别引擎错误或超时；本次仅输出元数据骨架。"
                            "若反复失败再检查权重是否预置",
                        )

    def _prepare_source(self, task: PipelineTask, work_dir: Path
                        ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        """D30 采集阶段：本地文件直接建元信息，远端只做一次解析。"""
        is_local = bool(task.local_media and Path(task.local_media).is_file())
        if is_local:
            # D11：本地上传 —— 跳过 probe/download/字幕
            path = Path(task.local_media)
            self._stage(task, "probe", "本地文件，跳过解析…")
            info = {
                "id": task.id,
                "title": (task.meta.get("title") or path.stem or "本地素材"),
                "extractor": "local",
                "extractor_key": "local",
                "webpage_url": "",
                "duration": None,
                "subtitles": {},
            }
            meta = knowledge.meta_from_info(info, "")
            meta["local_file"] = True
            if task.meta.get("template"):
                meta["template"] = task.meta["template"]
            task.media_path = str(path)
            self._set(task, info=info, meta=meta)
            try:
                dest = work_dir / path.name
                if path.resolve() != dest.resolve():
                    import shutil as _shutil
                    _shutil.copy2(path, dest)
                    task.media_path = str(dest)
            except OSError:
                pass
            self._degrade(task, "subtitle.absent", "本地文件无平台字幕，走语音识别")
        else:
            issue = engine_opts.cookie_browser_issue(
                engine_opts.cookie_status().get("cookies_from_browser", ""))
            if issue:
                self._degrade(
                    task, "cookie.browser_unreadable",
                    "Safari Cookie 读取被 macOS 拒绝，本次未携带浏览器 Cookie；"
                    "公开素材不受影响，需要登录态时请给启动器开「完全磁盘访问权限」"
                    "或改用 cookies.txt",
                )
            self._stage(task, "probe", "正在解析（全任务仅此一次）…")
            info = self._probe(task.url)
            self._tick(task)
            meta = knowledge.meta_from_info(info, task.url)
            if task.meta.get("template"):
                meta["template"] = task.meta["template"]
            self._set(task, info=info, meta=meta)

        (work_dir / "info.json").write_text(
            json.dumps({k: v for k, v in info.items()
                        if k in ("id", "title", "extractor", "extractor_key", "chapters",
                                 "duration", "uploader", "upload_date", "webpage_url",
                                 "tags", "categories", "description", "subtitles")},
                       ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

        if not is_local and info.get("is_live"):
            live_secs = engine_opts.live_record_seconds()
            meta["is_live"] = True
            meta["live_record_seconds"] = live_secs
            if live_secs == -1:
                self._degrade(
                    task, "live.continuous",
                    "检测到直播流：将持续录制直到点「取消」（非平台完整回放页）",
                )
            elif live_secs > 0:
                self._degrade(
                    task, "live.limited",
                    f"检测到直播流：将限时录制约 {live_secs} 秒片段",
                )
                if not meta.get("duration"):
                    meta["duration"] = float(live_secs)
            self._set(task, meta=meta)
        elif not is_local and str(info.get("live_status") or "") in ("was_live", "post_live"):
            self._degrade(
                task, "live.replay",
                "检测到直播回放（已结束），将按完整时长下载",
            )
            meta["was_live"] = True
            self._set(task, meta=meta)
        return info, meta, is_local

    def _finish_task(self, task: PipelineTask, meta: dict[str, Any],
                     bundle: dict[str, Any], saved: dict[str, str]) -> None:
        """D30 收尾阶段：可选直写 Obsidian、清理媒体、落 done。"""
        try:
            vault = resolve_obsidian_dir()
            if vault is not None and "md" in bundle:
                stem = safe_stem(meta.get("title") or "", fallback=task.id)
                obs_path = vault / f"{stem}.md"
                if obs_path.exists():
                    obs_path = vault / f"{stem}-{task.id[:8]}.md"
                obs_path.write_text(bundle["md"], encoding="utf-8")
                saved["obsidian"] = str(obs_path)
                self._set(task, files=saved)
                avd_log.info("obsidian auto-write", task=task.id, path=str(obs_path))
        except Exception as exc:  # noqa: BLE001
            self._degrade(task, "obsidian.write", f"Obsidian 直写跳过：{exc}")

        keep_media = task.want_download or os.environ.get("AVD_KEEP_MEDIA") == "1"
        if task.media_path and not keep_media:
            try:
                Path(task.media_path).unlink(missing_ok=True)
                task.media_path = ""
            except OSError:
                pass

        self._set(task, stage="done", percent=100.0, message="完成",
                  finished_at=time.time())
        avd_log.info("pipeline done", task=task.id, title=meta.get("title", ""),
                     source=task.source_kind)


_MANAGER: PipelineManager | None = None


def manager() -> PipelineManager:
    """进程内单例，供 HTTP 层调用。"""
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = PipelineManager()
    return _MANAGER
