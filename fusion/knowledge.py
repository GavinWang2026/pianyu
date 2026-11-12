# Copyright (c) 2026 Hyht-Team. All rights reserved.
# See LICENSE (proprietary) and NOTICE.md (third-party components).
"""结构化知识稿 —— 旗舰版里那个「>2」的产物。

yt-dlp 单独只输出 MP4；AVD 原版单独只输出平铺 txt / SRT。
把上游 ``info`` 里的章节、标签、完整描述、出处喂给 AVD 的分段数据，
才长得出「带章节 + 带出处 + 可剔除口播 + 带 front matter」的知识资产。

本模块是纯函数（标准库），不联网、不碰磁盘、可完整单测。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any, Iterable

GENERATOR = "AVD Flagship v2.0.0"


@dataclass(frozen=True)
class Chapter:
    title: str
    start: float
    end: float | None = None


def chapters_from_info(info: dict[str, Any]) -> list[Chapter]:
    """从 yt-dlp 的 info dict 取章节（不联网，站点没给就返回空列表）。"""
    raw = info.get("chapters") or []
    out: list[Chapter] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item.get("start_time") or 0)
        except (TypeError, ValueError):
            start = 0.0
        end = item.get("end_time")
        try:
            end = float(end) if end is not None else None
        except (TypeError, ValueError):
            end = None
        title = str(item.get("title") or "").strip() or f"章节 {len(out) + 1}"
        out.append(Chapter(title=title, start=max(0.0, start), end=end))
    return sorted(out, key=lambda c: c.start)


def sponsor_ranges(info: dict[str, Any]) -> list[tuple[float, float]]:
    """只消费 info 里已存在的 sponsorblock 段，**绝不联网请求 SponsorBlock API**。

    段数据由上游后处理器在联网环境下写入；隔离网/断网场景该字段为空，
    本函数自然返回空列表，链路照常可用 —— 这是「断网可验收」的保证。
    """
    raw = info.get("sponsorblock_chapters") or []
    ranges: list[tuple[float, float]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            start, end = float(item["start_time"]), float(item["end_time"])
        except (KeyError, TypeError, ValueError):
            continue
        if end > start:
            ranges.append((start, end))
    return ranges


def strip_ranges(
    segments: list[dict[str, Any]], ranges: list[tuple[float, float]]
) -> list[dict[str, Any]]:
    """剔除落在口播/广告区间内的句子。"""
    if not ranges:
        return list(segments)

    def hit(seg: dict[str, Any]) -> bool:
        try:
            s, e = float(seg.get("start", 0)), float(seg.get("end", 0))
        except (TypeError, ValueError):
            return False
        return any(s < r_end and e > r_start for r_start, r_end in ranges)

    return [s for s in segments if not hit(s)]


def _chapter_at(moment: float, chapters: list[Chapter]) -> Chapter | None:
    current = None
    for chapter in chapters:
        if chapter.start <= moment:
            current = chapter
        else:
            break
    return current


def assign_chapters(
    segments: list[dict[str, Any]], chapters: list[Chapter]
) -> list[dict[str, Any]]:
    """给每个句子打上章节标签（不改动原对象）。"""
    tagged: list[dict[str, Any]] = []
    for seg in segments:
        item = dict(seg)
        try:
            moment = float(seg.get("start") or 0)
        except (TypeError, ValueError):
            moment = 0.0
        chapter = _chapter_at(moment, chapters)
        item["chapter"] = chapter.title if chapter else ""
        item["chapter_start"] = chapter.start if chapter else None
        tagged.append(item)
    return tagged


def group_by_chapter(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按章节归组；无章节时退化为单组「正文」（AVD 原版行为，保证不断链）。"""
    groups: list[dict[str, Any]] = []
    index: dict[str, int] = {}
    for seg in segments:
        title = str(seg.get("chapter") or "").strip()
        if title and title in index:
            groups[index[title]]["segments"].append(seg)
            continue
        if title:
            index[title] = len(groups)
            groups.append({"title": title, "start": seg.get("chapter_start") or 0.0, "segments": [seg]})
        else:
            if not groups or groups[-1]["title"] != "正文":
                groups.append({"title": "正文", "start": 0.0, "segments": []})
            groups[-1]["segments"].append(seg)
    return groups


def meta_from_info(info: dict[str, Any], url: str = "") -> dict[str, Any]:
    """把上游 info 里原本被丢掉的结构化字段完整留下。

    原版 probe() 只回传 11 个字段、description 截到 400 字；这里保留全文。
    """
    return {
        "title": str(info.get("title") or "未命名素材").strip(),
        "id": str(info.get("id") or ""),
        "extractor": str(info.get("extractor_key") or info.get("extractor") or ""),
        "uploader": str(info.get("uploader") or info.get("channel") or info.get("uploader_id") or ""),
        "upload_date": str(info.get("upload_date") or ""),
        "duration": info.get("duration"),
        "view_count": info.get("view_count"),
        "webpage_url": str(info.get("webpage_url") or url),
        "thumbnail": str(info.get("thumbnail") or ""),
        "description": str(info.get("description") or ""),
        "tags": list(info.get("tags") or []),
        "categories": list(info.get("categories") or []),
        "chapters": [asdict(c) for c in chapters_from_info(info)],
        "subtitle_tracks": sorted((info.get("subtitles") or {}).keys()),
        "automatic_caption_tracks": sorted((info.get("automatic_captions") or {}).keys()),
    }


def fmt_clock(seconds: float) -> str:
    total = int(max(0, seconds))
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def fmt_srt_time(seconds: float) -> str:
    ms = int(max(0, seconds) * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _yaml_scalar(value: Any) -> str:
    text = str(value if value is not None else "").replace("\n", " ").replace('"', "'").strip()
    return f'"{text}"' if text else '""'


def frontmatter(meta: dict[str, Any], *, source_kind: str = "") -> str:
    """YAML front matter —— 让稿子能被 Obsidian / 知识库 / 检索系统直接吃进去。"""
    rows = [
        f"title: {_yaml_scalar(meta.get('title'))}",
        f"source: {_yaml_scalar(meta.get('webpage_url'))}",
        f"platform: {_yaml_scalar(meta.get('extractor'))}",
        f"author: {_yaml_scalar(meta.get('uploader'))}",
        f"upload_date: {_yaml_scalar(meta.get('upload_date'))}",
        f"duration: {_yaml_scalar(meta.get('duration'))}",
        f"source_kind: {_yaml_scalar(source_kind)}",
        f"chapters: {len(meta.get('chapters') or [])}",
        f"tags: [{', '.join(_yaml_scalar(t) for t in (meta.get('tags') or [])[:12])}]",
        f"generator: {_yaml_scalar(GENERATOR)}",
        f"generated_at: {_yaml_scalar(datetime.now().isoformat(timespec='seconds'))}",
    ]
    return "---\n" + "\n".join(rows) + "\n---"


def render_markdown(
    meta: dict[str, Any],
    segments: list[dict[str, Any]],
    *,
    summary: str = "",
    keywords: Iterable[str] = (),
    source_kind: str = "",
    include_frontmatter: bool = True,
) -> str:
    lines: list[str] = []
    if include_frontmatter:
        lines += [frontmatter(meta, source_kind=source_kind), ""]

    lines += [f"# {meta.get('title') or '文案'}", ""]
    facts = [
        ("平台", meta.get("extractor")),
        ("作者", meta.get("uploader")),
        ("发布日期", meta.get("upload_date")),
        ("时长", fmt_clock(float(meta.get("duration") or 0)) if meta.get("duration") else ""),
        ("原始链接", meta.get("webpage_url")),
        ("文案来源", source_kind),
    ]
    for label, value in facts:
        if value:
            lines.append(f"- {label}：{value}")
    if meta.get("tags"):
        lines.append(f"- 标签：{'、'.join(str(t) for t in meta['tags'][:12])}")
    lines += ["", "---", ""]

    if summary:
        lines += ["## 摘要", "", str(summary).strip(), ""]
    words = [str(k) for k in keywords if k]
    if words:
        lines += [f"**关键词**：{'、'.join(words)}", "", "---", ""]

    groups = group_by_chapter(assign_chapters(segments, [Chapter(**c) for c in (meta.get("chapters") or [])]))
    for group in groups:
        title = group["title"]
        heading = f"## {title}" if title == "正文" else f"## {title} · {fmt_clock(float(group['start']))}"
        lines += [heading, ""]
        paragraph: list[str] = []
        for seg in group["segments"]:
            text = str(seg.get("text") or "").strip()
            if text:
                paragraph.append(text)
        if paragraph:
            lines += ["".join(paragraph) if _is_cjk_run(paragraph) else " ".join(paragraph), ""]

    lines += ["---", "", "## 来源与可信度", ""]
    lines.append(f"- 本稿由 {GENERATOR} 在本地生成，解析引擎为内嵌 yt-dlp 2026.10.26。")
    reason = {
        "subtitle": "文本来自平台字幕，未经语音识别，误差最低。",
        "asr": "文本来自本地语音识别，专有名词与方言可能偏差。",
        "": "本次未取得正文文本，仅生成元数据骨架。",
    }.get(source_kind, "")
    if reason:
        lines.append(f"- {reason}")
    if meta.get("description"):
        lines += ["", "<details><summary>原始简介</summary>", "", meta["description"][:4000], "", "</details>"]
    return "\n".join(lines).rstrip() + "\n"


def _is_cjk_run(parts: list[str]) -> bool:
    sample = "".join(parts[:5])
    if not sample:
        return False
    cjk = sum(1 for ch in sample if "\u4e00" <= ch <= "\u9fff")
    return cjk / len(sample) > 0.3


def render_srt(segments: list[dict[str, Any]]) -> str:
    blocks = []
    for index, seg in enumerate(segments, 1):
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        try:
            start = float(seg.get("start") or 0)
            end = float(seg.get("end") or start)
        except (TypeError, ValueError):
            start, end = 0.0, 0.0
        blocks.append(f"{index}\n{fmt_srt_time(start)} --> {fmt_srt_time(end)}\n{text}")
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def render_txt(segments: list[dict[str, Any]]) -> str:
    return "\n".join(str(s.get("text") or "").strip() for s in segments if s.get("text")).strip() + "\n"


def build_bundle(
    meta: dict[str, Any],
    segments: list[dict[str, Any]],
    *,
    summary: str = "",
    keywords: Iterable[str] = (),
    source_kind: str = "",
    sponsor: list[tuple[float, float]] | None = None,
) -> dict[str, str]:
    """一次性产出四种交付物；调用方负责落盘。"""
    cleaned = strip_ranges(segments, sponsor or [])
    return {
        "md": render_markdown(meta, cleaned, summary=summary, keywords=keywords, source_kind=source_kind),
        "srt": render_srt(cleaned),
        "txt": render_txt(cleaned),
        "json": json.dumps(
            {"meta": meta, "segments": cleaned, "source_kind": source_kind,
             "sponsor_removed": len(segments) - len(cleaned), "generator": GENERATOR},
            ensure_ascii=False, indent=2,
        ),
    }
