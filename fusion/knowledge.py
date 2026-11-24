# Copyright (c) 2026 Hyht-Team
# SPDX-License-Identifier: MIT
# See LICENSE (MIT) and NOTICE.md (third-party components).
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
from pathlib import Path
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


_CHAPTER_STOP = frozenset({
    "这个", "一个", "我们", "你们", "他们", "就是", "因为", "所以", "但是",
    "可以", "没有", "什么", "怎么", "自己", "时候", "这样", "那么", "其实",
    "如果", "已经", "还是", "非常", "比较", "一直", "可能", "需要", "今天",
    "大家", "然后", "以及", "或者", "不是", "开始", "结束",
})


def _title_from_text(text: str, fallback: str) -> str:
    clean = re.sub(r"\s+", "", (text or "").strip())
    if not clean:
        return fallback
    # 取首句前若干汉字/词作为标题
    piece = re.split(r"[，。！？；、,.!?\s]", clean)[0][:16]
    if len(piece) < 2:
        piece = clean[:12]
    return piece or fallback


def _keywords_from_block(block: list[dict[str, Any]], *, top_n: int = 3) -> list[str]:
    """无 LLM 时的章节标题词：按字 bigram 频次取前 N，过滤停用词。"""
    blob = "".join(str(s.get("text") or "") for s in block)
    blob = re.sub(r"\s+", "", blob)
    if not blob:
        return []
    freq: dict[str, int] = {}
    for i in range(len(blob) - 1):
        gram = blob[i:i + 2]
        if not all("\u4e00" <= ch <= "\u9fff" for ch in gram):
            continue
        if gram in _CHAPTER_STOP or gram[0] in _CHAPTER_STOP or gram[1] in _CHAPTER_STOP:
            continue
        freq[gram] = freq.get(gram, 0) + 1
    ranked = sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))
    return [w for w, _ in ranked[:top_n] if w]


def synthesize_chapters(
    segments: list[dict[str, Any]],
    *,
    gap_break: float = 2.8,
    min_chars: int = 80,
    target_min: int = 3,
    target_max: int = 8,
) -> tuple[list[Chapter], str]:
    """无平台 chapters 时，按「长停顿 + 段落长度」切 3–8 个伪章节。

    返回 (chapters, title_source)；title_source 为 llm|tfidf|keyword|none。
    调用方应在 meta 上标注 ``chapters_auto=True``。
    """
    if not segments:
        return [], "none"
    # 先按停顿切成粗块
    blocks: list[list[dict[str, Any]]] = [[]]
    prev_end = 0.0
    for seg in segments:
        try:
            start = float(seg.get("start") or 0)
            end = float(seg.get("end") or start)
        except (TypeError, ValueError):
            start, end = prev_end, prev_end
        if blocks[-1] and (start - prev_end) >= gap_break:
            blocks.append([])
        blocks[-1].append(seg)
        prev_end = end

    def block_chars(block: list[dict[str, Any]]) -> int:
        return sum(len(str(s.get("text") or "")) for s in block)

    # 停顿切开的块优先保留；仅当超过 target_max 时合并最短相邻对
    merged: list[list[dict[str, Any]]] = [list(b) for b in blocks if b]
    while len(merged) > target_max:
        best_i, best_n = 0, 10**9
        for i in range(len(merged) - 1):
            n = block_chars(merged[i]) + block_chars(merged[i + 1])
            if n < best_n:
                best_i, best_n = i, n
        merged[best_i] = merged[best_i] + merged[best_i + 1]
        del merged[best_i + 1]

    # 停顿不够、块数太少：按字数硬切到 target_min
    total = sum(block_chars(b) for b in merged)
    if len(merged) < target_min and total >= max(min_chars, 30) * 2:
        flat = [s for b in merged for s in b]
        want = min(target_min, max(2, len(flat) // 3))
        chunk = max(1, len(flat) // want)
        merged = [flat[i:i + chunk] for i in range(0, len(flat), chunk)]
        if len(merged) > target_max:
            tail: list[dict[str, Any]] = []
            for b in merged[target_max - 1:]:
                tail.extend(b)
            merged = merged[: target_max - 1] + [tail]

    chapters: list[Chapter] = []
    for i, block in enumerate(merged):
        if not block:
            continue
        try:
            start = float(block[0].get("start") or 0)
        except (TypeError, ValueError):
            start = 0.0
        try:
            end = float(block[-1].get("end") or start)
        except (TypeError, ValueError):
            end = start
        # 占位标题；enrich_chapter_titles 再升级为 TF-IDF / LLM
        kws = _keywords_from_block(block, top_n=3)
        if kws:
            title = "·".join(kws[:2])
        else:
            title = _title_from_text(str(block[0].get("text") or ""), f"话题 {i + 1}")
        if title in _CHAPTER_STOP or len(title) < 2:
            title = f"话题 {i + 1}"
        chapters.append(Chapter(title=f"{title}（自动分段）", start=start, end=end))
    title_src = "keyword"
    try:
        from . import chapter_titles
        chapters, title_src = chapter_titles.enrich_chapter_titles(
            chapters, segments, use_llm=True,
        )
    except Exception:
        pass
    return chapters, title_src


_PROMO_RE = re.compile(
    r"(点个关注|一键三连|点赞订阅|感谢观看|下期再见|广告时间|赞助商|"
    r"优惠券|领券|加微信|加我微信|扫码|公号|公众号|带货|直播间下单|"
    r"sponsor|subscribe|like and subscribe|follow me)",
    re.IGNORECASE,
)


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


def heuristic_promo_ranges(segments: list[dict[str, Any]]) -> list[tuple[float, float]]:
    """离线启发式口播/引流句剔除（不联网）：命中营销话术的句子成段。"""
    ranges: list[tuple[float, float]] = []
    for seg in segments:
        text = str(seg.get("text") or "")
        if not text or not _PROMO_RE.search(text):
            continue
        try:
            start, end = float(seg.get("start") or 0), float(seg.get("end") or 0)
        except (TypeError, ValueError):
            continue
        if end > start:
            ranges.append((start, end))
    return ranges


def merge_ranges(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged = sorted([*a, *b], key=lambda x: x[0])
    if not merged:
        return []
    out = [merged[0]]
    for start, end in merged[1:]:
        ps, pe = out[-1]
        if start <= pe:
            out[-1] = (ps, max(pe, end))
        else:
            out.append((start, end))
    return out


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
    """YAML 双引号标量：转义反斜杠与双引号（DOC-01）。"""
    text = str(value if value is not None else "").replace("\n", " ").strip()
    text = text.replace("\\", "\\\\").replace('"', '\\"')
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
    if meta.get("speakers"):
        rows.append(
            f"speakers: {_yaml_scalar(meta.get('speaker_source') or 'heuristic')}"
        )
    return "---\n" + "\n".join(rows) + "\n---"


_TEMPLATES = ("interview", "course", "podcast", "meeting")
_ACTION_HINTS = ("决定", "结论", "需要", "负责", "跟进", "下一步", "待办", "行动项", "安排")


def _template_block(meta: dict[str, Any],
                    segments: list[dict[str, Any]]) -> list[str]:
    """按内容模板追加结构化小节：访谈 / 课程 / 播客 / 会议。

    只做「同一批素材的不同组织方式」，不改变正文与事实；未指定模板
    （或缺省）时返回空列表，历史产物逐字节不变。
    """
    tpl = str(meta.get("template") or "").strip().lower()
    if tpl not in _TEMPLATES:
        return []
    lines: list[str] = []
    points = [str(p) for p in (meta.get("key_points") or []) if str(p).strip()]
    chapters = [c for c in (meta.get("chapters") or []) if isinstance(c, dict)]
    glossary = list(meta.get("glossary") or [])

    if tpl == "interview":
        lines += ["## 访谈提纲", ""]
        turns = [s for s in segments if str(s.get("speaker") or "").strip()][:40]
        if turns:
            for s in turns:
                at = fmt_clock(float(s.get("start") or 0))
                lines.append(
                    f"- `{at}` **{s.get('speaker')}**：{str(s.get('text') or '').strip()}")
        else:
            lines.append("- 未启用说话人标注；可在设置中开启 `asr_diarize` 后重跑")
        lines += ["", "---", ""]

    elif tpl == "course":
        lines += ["## 课程大纲", ""]
        for c in chapters:
            lines.append(
                f"- `{fmt_clock(float(c.get('start') or 0))}` "
                f"{c.get('title') or '未命名'}")
        if not chapters:
            lines.append("- 平台未提供章节，可先在校对页分段")
        if points:
            lines += ["", "## 知识点", ""] + [f"- {p}" for p in points[:12]]
        questions = [f"- 如何用自己的话解释「{p.rstrip('。；;')}」？" for p in points[:5]]
        if questions:
            lines += ["", "## 思考题", ""] + questions
        lines += ["", "---", ""]

    elif tpl == "podcast":
        lines += ["## 本期亮点", ""]
        lines += [f"- {p}" for p in points[:8]] or ["- 暂无要点"]
        if glossary:
            lines += ["", "## 提及资源", ""]
            for g in glossary[:12]:
                if isinstance(g, dict):
                    lines.append(f"- **{g.get('term')}**：{g.get('note') or ''}")
                else:
                    lines.append(f"- {g}")
        if chapters:
            lines += ["", "## 章节时间轴", ""]
            for c in chapters:
                lines.append(
                    f"- `{fmt_clock(float(c.get('start') or 0))}` {c.get('title') or ''}")
        lines += ["", "---", ""]

    elif tpl == "meeting":
        actions = [
            s for s in segments
            if any(h in str(s.get("text") or "") for h in _ACTION_HINTS)
        ][:15]
        lines += ["## 结论与行动项", ""]
        if actions:
            for s in actions:
                who = str(s.get("speaker") or "").strip()
                at = fmt_clock(float(s.get("start") or 0))
                prefix = f"**{who}** " if who else ""
                lines.append(f"- [ ] `{at}` {prefix}{str(s.get('text') or '').strip()}")
        else:
            lines.append("- 未识别到明确结论/行动项，请在校对页补充")
        stats = meta.get("speaker_stats") or {}
        if stats:
            lines += ["", "## 发言分布", ""]
            for name, info in stats.items():
                if isinstance(info, dict):
                    lines.append(
                        f"- **{name}**：{info.get('count', 0)} 段 / "
                        f"{info.get('seconds', 0)} 秒")
        lines += ["", "---", ""]

    return lines


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

    if meta.get("low_content"):
        suff = meta.get("content_sufficiency") or {}
        lines += [
            "> ⚠️ **信息量不足（{} 级素材）**".format(suff.get("level") or "C"),
            "> ",
            "> {}".format(
                suff.get("message")
                or "本素材几乎没有可转写的人声，已只输出元数据与转写原文。"
            ),
            "> ",
            "> 这类素材（纯音乐 / MV / 纯片头）不存在可提炼的知识点，"
            "因此本稿不含摘要、要点与术语表。",
            "",
            "---",
            "",
        ]

    if summary:
        label = "摘要（规则摘要）" if meta.get("summary_kind") == "rule" else "摘要"
        lines += [f"## {label}", "", str(summary).strip(), ""]
    words = [str(k) for k in keywords if k]
    if words:
        lines += [f"**关键词**：{'、'.join(words)}", "", "---", ""]
    points = list(meta.get("key_points") or [])
    if points:
        lines += ["## 要点", ""]
        for p in points:
            lines.append(f"- {p}")
        lines += ["", "---", ""]
    glossary = list(meta.get("glossary") or [])
    if glossary:
        lines += ["## 术语表", ""]
        for g in glossary:
            if isinstance(g, dict):
                lines.append(f"- **{g.get('term')}**：{g.get('note') or ''}")
            else:
                lines.append(f"- {g}")
        lines += ["", "---", ""]
    ts_index = list(meta.get("timestamp_index") or [])
    if ts_index:
        lines += ["## 时间戳索引", ""]
        for item in ts_index:
            if isinstance(item, dict):
                lines.append(f"- `{fmt_clock(float(item.get('at') or 0))}` {item.get('text') or ''}")
        lines += ["", "---", ""]

    lines += _template_block(meta, segments)

    webpage = str(meta.get("webpage_url") or meta.get("url") or "").strip()
    paras = list(meta.get("paragraphs") or [])

    chapter_objs = [Chapter(**c) for c in (meta.get("chapters") or [])]
    groups = group_by_chapter(assign_chapters(segments, chapter_objs))
    for gi, group in enumerate(groups):
        title = group["title"]
        heading = f"## {title}" if title == "正文" else f"## {title} · {fmt_clock(float(group['start']))}"
        lines += [heading, ""]
        paragraph: list[str] = []
        has_speaker = any(str(s.get("speaker") or "").strip() for s in group["segments"])
        if has_speaker:
            for seg in group["segments"]:
                text = str(seg.get("text") or "").strip()
                if not text:
                    continue
                sp = str(seg.get("speaker") or "").strip()
                if sp and str(seg.get("speaker_source") or "") == "heuristic":
                    lines.append(f"**[{sp}·未校准]** {text}")
                elif sp:
                    lines.append(f"**[{sp}]** {text}")
                else:
                    lines.append(text)
            lines.append("")
        elif paras:
            # D10：优先用 polish 分段（带时间锚），按章节时间窗过滤
            g_start = float(group.get("start") or 0)
            if gi + 1 < len(groups):
                g_end = float(groups[gi + 1].get("start") or 1e12)
            else:
                ends = []
                for s in group.get("segments") or []:
                    try:
                        ends.append(float(s.get("end") or s.get("start") or 0))
                    except (TypeError, ValueError):
                        pass
                g_end = max(ends) if ends else 1e12
            for p in paras:
                try:
                    ps = float(p.get("start") or 0)
                    pe = float(p.get("end") or ps)
                except (TypeError, ValueError):
                    continue
                # 段中点落入本章，或与本章时间窗相交
                mid = (ps + pe) / 2.0
                if mid < g_start or mid >= g_end:
                    continue
                text = str(p.get("text") or "").strip()
                if not text:
                    continue
                clock = fmt_clock(ps)
                if webpage:
                    lines.append(
                        f'<a href="{webpage}#t={int(ps)}" title="{clock}">`{clock}`</a> {text}'
                    )
                else:
                    lines.append(f"`{clock}` {text}")
                lines.append("")
        else:
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
    if meta.get("chapters_auto"):
        lines.append("- 章节为自动分段（按停顿与段落长度），非平台原生章节。")
    if meta.get("summary_kind") == "rule":
        lines.append("- 摘要为离线规则生成（段要点 + 词频关键词），未调用大模型。")
    elif meta.get("summary_kind") == "llm":
        lines.append("- 摘要与关键词由本机配置的大模型一次调用生成（失败时回退规则）。")
    insights_src = str(meta.get("insights_source") or "")
    if insights_src == "llm":
        lines.append("- 要点与术语表由本机配置的大模型一次调用生成（失败时回退规则）。")
    elif insights_src == "rule" and (meta.get("key_points") or meta.get("glossary")):
        lines.append("- 要点与术语表为离线规则生成（抽样首句 + 实体词频）。")
    ch_src = str(meta.get("chapter_title_source") or "")
    if ch_src == "llm":
        lines.append("- 自动章节标题由大模型一次调用生成。")
    elif ch_src == "tfidf":
        lines.append("- 自动章节标题为 TF-IDF / 段首回退。")
    if meta.get("speakers"):
        kind = str(meta.get("speaker_source") or "heuristic")
        if kind == "pyannote":
            lines.append("- 说话人标签来自 pyannote 神经网络分离。")
        else:
            lines.append(
                "- 说话人标签为停顿启发式（A/B 交替，未校准），仅供参考；"
                "frontmatter 标注 speakers: heuristic。"
            )
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
        sp = str(seg.get("speaker") or "").strip()
        if sp:
            text = f"[{sp}] {text}"
        try:
            start = float(seg.get("start") or 0)
            end = float(seg.get("end") or start)
        except (TypeError, ValueError):
            start, end = 0.0, 0.0
        blocks.append(f"{index}\n{fmt_srt_time(start)} --> {fmt_srt_time(end)}\n{text}")
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def render_txt(segments: list[dict[str, Any]]) -> str:
    rows: list[str] = []
    for s in segments:
        text = str(s.get("text") or "").strip()
        if not text:
            continue
        sp = str(s.get("speaker") or "").strip()
        rows.append(f"[{sp}] {text}" if sp else text)
    return "\n".join(rows).strip() + "\n"


def _chapter_groups(
    meta: dict[str, Any],
    segments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    chapters = [c for c in (meta.get("chapters") or []) if isinstance(c, dict)]
    chapter_objs: list[Chapter] = []
    for ch in chapters:
        try:
            chapter_objs.append(Chapter(
                title=str(ch.get("title") or "章节"),
                start=float(ch.get("start") or 0),
                end=(float(ch["end"]) if ch.get("end") is not None else None),
            ))
        except (TypeError, ValueError):
            continue
    try:
        if chapter_objs:
            return group_by_chapter(assign_chapters(segments, chapter_objs))
    except Exception:
        pass
    return [{"title": "正文", "start": 0, "segments": segments}]


def render_docx(
    meta: dict[str, Any],
    segments: list[dict[str, Any]],
    *,
    summary: str = "",
    keywords: Iterable[str] = (),
) -> bytes:
    """经统一 LayoutSpec 写出 OOXML：封面 / 目录 / 页眉页脚 / 章节分页。"""
    from . import layout as layout_mod
    groups = _chapter_groups(meta, segments)
    doc = layout_mod.build_from_knowledge(
        meta, segments, summary=summary, keywords=keywords,
        template="delivery", groups=groups,
    )
    return layout_mod.render_docx(doc)


def render_pdf(
    meta: dict[str, Any],
    segments: list[dict[str, Any]],
    *,
    summary: str = "",
    keywords: Iterable[str] = (),
) -> bytes:
    """CJK 可读 PDF：同源 LayoutSpec；无字体时回退附件方案。"""
    from . import layout as layout_mod
    title = str(meta.get("title") or "知识稿")
    groups = _chapter_groups(meta, segments)
    doc = layout_mod.build_from_knowledge(
        meta, segments, summary=summary, keywords=keywords,
        template="delivery", groups=groups,
    )
    try:
        cjk = layout_mod.render_pdf(doc)
        if cjk:
            return cjk
    except Exception:
        pass
    lines = layout_mod.to_pdf_lines(doc)
    preview_lines = [title, "(Full UTF-8 text attached as transcript.txt)", ""] + [
        ln for ln in lines if ln != layout_mod.PAGEBREAK_SENTINEL
    ]
    utf8_body = "\n".join([title] + lines).encode("utf-8")
    return _pdf_with_embedded_utf8(title, preview_lines, utf8_body)


def _pdf_with_embedded_utf8(title: str, lines: list[str], utf8_body: bytes) -> bytes:
    """PDF：一页 ASCII 预览 + EmbeddedFile 完整 UTF-8 文稿。"""
    preview = lines[:48] or ["(empty)"]
    parts = ["BT", "/F1 11 Tf", "50 800 Td"]
    for i, line in enumerate(preview):
        safe = "".join(ch if 32 <= ord(ch) < 128 else "?" for ch in line)[:90]
        esc = safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        if i:
            parts.append("0 -14 Td")
        parts.append(f"({esc}) Tj")
    parts.append("ET")
    stream = "\n".join(parts).encode("latin-1", "replace")
    content = f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"\nendstream"
    font = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    emb = (
        f"<< /Type /EmbeddedFile /Subtype /text#2Fplain /Params << /Size {len(utf8_body)} >> "
        f"/Length {len(utf8_body)} >>\nstream\n".encode("ascii")
        + utf8_body
        + b"\nendstream"
    )
    filespec = (
        b"<< /Type /Filespec /F (transcript.txt) /EF << /F 5 0 R >> "
        b"/Desc (UTF-8 full transcript) >>"
    )
    page = (
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Contents 4 0 R /Resources << /Font << /F1 3 0 R >> >> >>"
    )
    pages = b"<< /Type /Pages /Kids [6 0 R] /Count 1 >>"
    catalog = (
        b"<< /Type /Catalog /Pages 2 0 R "
        b"/Names << /EmbeddedFiles << /Names [(transcript.txt) 7 0 R] >> >> >>"
    )
    objs = [catalog, pages, font, content, emb, page, filespec]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(out))
        out.extend(f"{i} 0 obj\n".encode("ascii"))
        out.extend(obj)
        out.extend(b"\nendobj\n")
    xref = len(out)
    out.extend(f"xref\n0 {len(objs) + 1}\n".encode("ascii"))
    out.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    out.extend(
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii")
    )
    return bytes(out)


def assign_speakers(
    segments: list[dict[str, Any]],
    *,
    max_speakers: int = 2,
    gap_sec: float = 1.2,
    audio_path: str | None = None,
) -> list[dict[str, Any]]:
    """说话人标注：有 pyannote + 音频时走神经网络；否则停顿启发式 A/B。

    已有 speaker 字段的段落保留不动。max_speakers 启发式仅支持 1–2。
    """
    if audio_path:
        neural = _assign_speakers_pyannote(segments, audio_path, max_speakers=max_speakers)
        if neural is not None:
            return neural
    return _assign_speakers_heuristic(segments, max_speakers=max_speakers, gap_sec=gap_sec)


def speaker_stats(segments: list[dict[str, Any]]) -> dict[str, Any]:
    """说话人标注可读统计（供面板 / 诊断）。"""
    labels: dict[str, int] = {}
    sources: dict[str, int] = {}
    for seg in segments:
        sp = str(seg.get("speaker") or "").strip()
        if not sp:
            continue
        labels[sp] = labels.get(sp, 0) + 1
        src = str(seg.get("speaker_source") or "unknown")
        sources[src] = sources.get(src, 0) + 1
    total = sum(labels.values())
    balance = 0.0
    if total and len(labels) >= 2:
        vals = sorted(labels.values())
        balance = vals[0] / max(1, vals[-1])
    conf = 0.0
    if sources.get("neural"):
        conf = 0.85
    elif sources.get("heuristic") and total:
        # 切换次数与均衡度共同决定置信
        conf = round(0.35 + 0.35 * min(1.0, balance) + 0.15 * min(1.0, (len(labels) - 1)), 2)
    return {
        "labeled": total,
        "labels": labels,
        "sources": sources,
        "balance": round(balance, 3),
        "confidence": conf,
        "calibrated": bool(sources.get("neural")),
    }


def _assign_speakers_heuristic(
    segments: list[dict[str, Any]],
    *,
    max_speakers: int = 2,
    gap_sec: float = 1.2,
    min_switches: int = 2,
) -> list[dict[str, Any]]:
    """停顿启发式 A/B。

    独白里的自然停顿容易误判换人：先数有效换人次数，不足 ``min_switches``
    则**不写** speaker 字段（调用方视为未标注），避免假对话。
    另要求 A/B 段数相对均衡（弱侧 ≥ 强侧 18%），否则视为独白误判并撤回。
    """
    n = max(1, min(2, int(max_speakers or 2)))
    if n < 2:
        return [dict(s) for s in segments]

    # 预扫描：统计会触发换人的停顿次数
    switches = 0
    prev_end: float | None = None
    for seg in segments:
        if str(seg.get("speaker") or "").strip():
            try:
                prev_end = float(seg.get("end") or 0)
            except (TypeError, ValueError):
                pass
            continue
        try:
            start = float(seg.get("start") or 0)
            end = float(seg.get("end") or start)
        except (TypeError, ValueError):
            start, end = 0.0, 0.0
        if prev_end is not None and (start - prev_end) >= gap_sec:
            switches += 1
        prev_end = end

    if switches < max(1, int(min_switches)):
        # 证据不足：原样返回，不贴 A/B
        return [dict(s) for s in segments]

    out: list[dict[str, Any]] = []
    cur = 0  # 0 -> A, 1 -> B
    prev_end = None
    counts = {"A": 0, "B": 0}
    for seg in segments:
        item = dict(seg)
        existing = str(item.get("speaker") or "").strip()
        if existing:
            out.append(item)
            try:
                prev_end = float(item.get("end") or 0)
            except (TypeError, ValueError):
                pass
            continue
        try:
            start = float(item.get("start") or 0)
            end = float(item.get("end") or start)
        except (TypeError, ValueError):
            start, end = 0.0, 0.0
        if prev_end is not None and (start - prev_end) >= gap_sec:
            cur = 1 - cur
        label = "A" if cur == 0 else "B"
        item["speaker"] = label
        item["speaker_source"] = "heuristic"
        counts[label] = counts.get(label, 0) + 1
        prev_end = end
        out.append(item)

    # 均衡门槛：一侧过少 → 撤回启发式标签（保留已有非启发式）
    a, b = counts.get("A", 0), counts.get("B", 0)
    if a and b:
        weak, strong = (a, b) if a < b else (b, a)
        if weak / max(1, strong) < 0.18:
            cleaned: list[dict[str, Any]] = []
            for item in out:
                row = dict(item)
                if str(row.get("speaker_source") or "") == "heuristic":
                    row.pop("speaker", None)
                    row.pop("speaker_source", None)
                cleaned.append(row)
            return cleaned
    return out


def _assign_speakers_pyannote(
    segments: list[dict[str, Any]],
    audio_path: str,
    *,
    max_speakers: int = 2,
) -> list[dict[str, Any]] | None:
    """可选：pyannote.audio 管道。未安装或失败返回 None（调用方回退启发式）。"""
    try:
        from pyannote.audio import Pipeline  # type: ignore
    except Exception:
        return None
    path = Path(audio_path)
    if not path.is_file():
        return None
    try:
        import os
        token = (os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN") or "").strip()
        kwargs: dict[str, Any] = {}
        if token:
            kwargs["use_auth_token"] = token
        pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1", **kwargs,
        )
        diarization = pipeline(str(path), num_speakers=max(1, min(8, int(max_speakers or 2))))
    except Exception:
        return None

    # 收集 (start, end, speaker_label)
    turns: list[tuple[float, float, str]] = []
    try:
        for turn, _, speaker in diarization.itertracks(yield_label=True):
            turns.append((float(turn.start), float(turn.end), str(speaker)))
    except Exception:
        return None
    if not turns:
        return None

    # 稳定映射 SPEAKER_00 → A, SPEAKER_01 → B …
    order: list[str] = []
    for _, _, sp in sorted(turns, key=lambda t: t[0]):
        if sp not in order:
            order.append(sp)
    label_map = {sp: chr(ord("A") + i) if i < 26 else sp for i, sp in enumerate(order)}

    def _speaker_at(mid: float) -> str:
        for a, b, sp in turns:
            if a <= mid <= b:
                return label_map.get(sp, "A")
        # 最近邻
        best = min(turns, key=lambda t: min(abs(mid - t[0]), abs(mid - t[1])))
        return label_map.get(best[2], "A")

    out: list[dict[str, Any]] = []
    for seg in segments:
        item = dict(seg)
        if str(item.get("speaker") or "").strip():
            out.append(item)
            continue
        try:
            start = float(item.get("start") or 0)
            end = float(item.get("end") or start)
        except (TypeError, ValueError):
            start, end = 0.0, 0.0
        item["speaker"] = _speaker_at((start + end) / 2.0)
        item["speaker_source"] = "pyannote"
        out.append(item)
    return out


def build_bundle(
    meta: dict[str, Any],
    segments: list[dict[str, Any]],
    *,
    summary: str = "",
    keywords: Iterable[str] = (),
    source_kind: str = "",
    sponsor: list[tuple[float, float]] | None = None,
) -> dict[str, str]:
    """一次性产出交付物；docx/pdf 以 base64 文本存入便于统一落盘接口。"""
    import base64

    cleaned = strip_ranges(segments, sponsor or [])
    docx_bytes = render_docx(meta, cleaned, summary=summary, keywords=keywords)
    pdf_bytes = render_pdf(meta, cleaned, summary=summary, keywords=keywords)
    return {
        "md": render_markdown(meta, cleaned, summary=summary, keywords=keywords, source_kind=source_kind),
        "srt": render_srt(cleaned),
        "txt": render_txt(cleaned),
        "json": json.dumps(
            {"meta": meta, "segments": cleaned, "source_kind": source_kind,
             "sponsor_removed": len(segments) - len(cleaned), "generator": GENERATOR},
            ensure_ascii=False, indent=2,
        ),
        "docx": base64.b64encode(docx_bytes).decode("ascii"),
        "pdf": base64.b64encode(pdf_bytes).decode("ascii"),
    }
