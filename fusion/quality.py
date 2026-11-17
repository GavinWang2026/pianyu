# Copyright (c) 2026 Hyht-Team. All rights reserved.
# See LICENSE (proprietary) and NOTICE.md (third-party components).
"""知识稿质量：要点 / 术语表 / 实体一致性 / 双栏对照。"""

from __future__ import annotations

import json
import re
from typing import Any

_ENTITY_RE = re.compile(
    r"[\u4e00-\u9fff]{2,8}|[A-Z][A-Za-z0-9_\-]{1,24}|[A-Za-z]{3,}(?:\s+[A-Za-z]{2,}){0,2}"
)
_STOP = frozenset({
    "这个", "一个", "我们", "你们", "他们", "就是", "因为", "所以", "但是",
    "可以", "没有", "什么", "怎么", "自己", "时候", "这样", "那么", "其实",
    "如果", "已经", "还是", "非常", "比较", "一直", "可能", "需要", "今天",
    "大家", "然后", "以及", "或者", "不是", "开始", "结束", "the", "and", "for",
})


def extract_entities(text: str) -> set[str]:
    out: set[str] = set()
    for m in _ENTITY_RE.finditer(text or ""):
        t = m.group(0).strip()
        if len(t) < 2 or t.lower() in _STOP or t in _STOP:
            continue
        out.add(t)
    return out


def sample_for_llm(texts: list[str], *, budget: int = 2400) -> str:
    """等距抽样填满字符预算（D14）：避免「30 段就到头」浪费上限。"""
    clean = [str(t or "").strip() for t in texts if str(t or "").strip()]
    if not clean:
        return ""
    total = sum(len(t) + 1 for t in clean)
    if total <= budget:
        return "\n".join(clean)[:budget]
    avg = max(1.0, total / len(clean))
    want = max(8, min(len(clean), int(budget / avg) + 1))
    if want <= 1:
        return clean[0][:budget]
    step = (len(clean) - 1) / (want - 1)
    idxs = sorted({min(len(clean) - 1, int(round(i * step))) for i in range(want)})
    out: list[str] = []
    used = 0
    for i in idxs:
        t = clean[i]
        if used + len(t) + 1 > budget:
            break
        out.append(t)
        used += len(t) + 1
    return "\n".join(out)[:budget]


def sample_segments_for_index(
    segments: list[dict[str, Any]],
    *,
    char_budget: int = 24_000,
) -> list[dict[str, Any]]:
    """素材库时间锚：按字符配额等距抽稀，禁止硬截断头部（D13）。"""
    rows = list(segments or [])
    if not rows:
        return []
    texts = [str(s.get("text") or "")[:120] for s in rows]
    total = sum(len(t) for t in texts)
    if total <= char_budget or len(rows) <= 1:
        return [
            {
                "start": s.get("start"),
                "end": s.get("end"),
                "text": str(s.get("text") or "")[:120],
            }
            for s in rows
        ]
    # 目标段数 ≈ 配额 / 平均段长
    avg = max(1.0, total / len(rows))
    want = max(50, min(len(rows), int(char_budget / avg)))
    step = (len(rows) - 1) / max(1, want - 1)
    idxs = sorted({min(len(rows) - 1, int(round(i * step))) for i in range(want)})
    out: list[dict[str, Any]] = []
    used = 0
    for i in idxs:
        text = str(rows[i].get("text") or "")[:120]
        if used + len(text) > char_budget:
            break
        out.append({
            "start": rows[i].get("start"),
            "end": rows[i].get("end"),
            "text": text,
        })
        used += len(text)
    return out


def dual_worth_emitting(segments: list[dict[str, Any]], *, min_ratio: float = 0.10) -> bool:
    """仅当 raw_text 与 text 有实质差异时才值得出 dual.md（D12）。"""
    rows = list(segments or [])
    if not rows:
        return False
    diffs = 0
    for seg in rows:
        raw = str(seg.get("raw_text") or "").strip()
        cur = str(seg.get("text") or "").strip()
        if raw and cur and raw != cur:
            diffs += 1
    return diffs > max(1, int(len(rows) * float(min_ratio)))


# 片头/水印/台标话术：纯音乐、MV、剧场片头素材转写出来就是这类句子，
# 不该被当成知识稿正文、摘要或要点。
_INTRO_NOISE_RE = re.compile(
    r"(独播|剧场|电视剧|独家|片头|片尾|字幕由|字幕组|本视频|版权|版权所有|"
    r"欢迎订阅|订阅频道|请关注|点赞|转发|打赏|商务合作|"
    r"television\s+series|exclusive|subscribe|all\s+rights\s+reserved)",
    re.IGNORECASE,
)


def is_intro_noise(text: str) -> bool:
    """判断一句是否为片头/水印/引流类噪声（用于低内容素材过滤）。"""
    t = (text or "").strip()
    if not t:
        return True
    if _INTRO_NOISE_RE.search(t):
        return True
    # 「A——B」式纯台标串（无动词、无标点分句、短）
    return len(t) <= 40 and t.count("—") + t.count("——") >= 1


def content_sufficiency(
    segments: list[dict[str, Any]],
    *,
    duration: float | None = None,
    min_chars: int = 80,
) -> dict[str, Any]:
    """素材信息量分级（A 充足 / B 偏薄 / C 不足）。

    P1（第九轮实测）：一支 MV 只转出 1 段「优优独播剧场——YoYo Television
    Series Exclusive」，系统仍产出 7 份产物 + 摘要 + 要点 + 术语表 + 章节，
    用户看到的是「像样但全是废话」的知识稿。判据取两条：
    正文字数 < 80，或 段数 ≤ 1 且时长 > 60s（说明几乎没有可转写人声）。
    """
    texts = [str(s.get("text") or "").strip() for s in segments or []]
    texts = [t for t in texts if t]
    body = "".join(texts)
    chars = len(body)
    spoken = sum(
        max(0.0, float(s.get("end") or 0) - float(s.get("start") or 0))
        for s in (segments or [])
        if str(s.get("text") or "").strip()
    )
    noise_ratio = (
        sum(len(t) for t in texts if is_intro_noise(t)) / chars if chars else 0.0
    )
    dur = float(duration or 0) or spoken
    sufficient = chars >= min_chars and not (len(texts) <= 1 and dur > 60)
    if noise_ratio >= 0.9:
        # 字数够但几乎全是片头水印：同样按不足处理
        sufficient = False
    if sufficient:
        level = "A"
    elif chars >= min_chars or (chars >= min_chars // 2 and len(texts) > 1
                               and noise_ratio < 0.6):
        level = "B"
    else:
        level = "C"
    msg = ""
    if not sufficient:
        msg = (
            f"信息量不足：仅 {chars} 字 / {len(texts)} 段"
            f"（时长约 {int(dur)}s，片头水印占比 {noise_ratio:.0%}）。"
            "已输出元数据与转写原文，不生成摘要/要点/术语表——"
            "这类素材（纯音乐、MV、片头）本身没有可提炼的知识点。"
        )
    return {
        "sufficient": sufficient,
        "level": level,
        "chars": chars,
        "segments": len(texts),
        "duration": round(dur, 1),
        "noise_ratio": round(noise_ratio, 3),
        "message": msg,
    }


def consistency_check(
    original_segments: list[dict[str, Any]],
    rewritten_segments: list[dict[str, Any]],
    *,
    char_ratio_min: float = 0.55,
    char_ratio_max: float = 1.85,
    entity_keep_min: float = 0.45,
) -> dict[str, Any]:
    """改写后字数比 + 实体保留率；超阈值建议回滚。"""
    orig = "".join(str(s.get("raw_text") or s.get("text") or "") for s in original_segments)
    new = "".join(str(s.get("text") or "") for s in rewritten_segments)
    o_n, n_n = max(1, len(orig)), max(1, len(new))
    ratio = n_n / o_n
    o_ent, n_ent = extract_entities(orig), extract_entities(new)
    keep = (len(o_ent & n_ent) / len(o_ent)) if o_ent else 1.0
    ok = (char_ratio_min <= ratio <= char_ratio_max) and keep >= entity_keep_min
    return {
        "ok": ok,
        "char_ratio": round(ratio, 3),
        "entity_keep": round(keep, 3),
        "original_chars": len(orig),
        "rewritten_chars": len(new),
        "original_entities": len(o_ent),
        "shared_entities": len(o_ent & n_ent),
        "action": "" if ok else "rollback",
    }


def rule_key_points(segments: list[dict[str, Any]], *, max_points: int = 8) -> list[str]:
    """离线要点：抽取式打分（关键词密度），按时间序输出。"""
    texts = [str(s.get("text") or "").strip() for s in segments if s.get("text")]
    if not texts:
        return []
    try:
        from . import pipeline as _pipe
        counter = _pipe._rule_keyword_counter(texts)
        scored = sorted(
            range(len(texts)),
            key=lambda i: (
                -_pipe._score_sentence(texts[i], counter, index=i, total=len(texts)),
                i,
            ),
        )
        k = min(max_points, max(3, len(texts) // 8 or 3))
        idxs = sorted(scored[:k])
    except Exception:
        step = max(1, len(texts) // max_points)
        idxs = list(range(0, len(texts), step))[:max_points]
    points: list[str] = []
    for i in idxs:
        head = texts[i][:56].rstrip("，,、 ")
        if len(texts[i]) > 56:
            head += "…"
        if points and points[-1][:16] == head[:16]:
            continue
        points.append(head)
        if len(points) >= max_points:
            break
    return points


def rule_glossary(segments: list[dict[str, Any]], *, top_n: int = 12) -> list[dict[str, str]]:
    """术语表雏形：高频专名（实体）列表。"""
    blob = "".join(str(s.get("text") or "") for s in segments)
    freq: dict[str, int] = {}
    for ent in extract_entities(blob):
        if len(ent) < 2:
            continue
        freq[ent] = freq.get(ent, 0) + blob.count(ent)
    ranked = sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"term": t, "note": f"出现 {c} 次"} for t, c in ranked[:top_n]]


def enrich_insights(
    segments: list[dict[str, Any]],
    *,
    use_llm: bool = True,
    max_points: int = 8,
    top_n: int = 12,
) -> tuple[list[str], list[dict[str, str]], str]:
    """要点 + 术语表：默认离线规则；有 LLM 配置时一次调用增强。

    返回 (key_points, glossary, source)，source 为 ``llm`` 或 ``rule``。
    """
    points = rule_key_points(segments, max_points=max_points)
    glossary = rule_glossary(segments, top_n=top_n)
    if use_llm:
        llm_out = _llm_insights(segments, max_points=max_points, top_n=top_n)
        if llm_out is not None:
            lp, lg = llm_out
            if lp:
                points = lp
            if lg:
                glossary = lg
            return points, glossary, "llm"
    return points, glossary, "rule"


def _llm_insights(
    segments: list[dict[str, Any]],
    *,
    max_points: int,
    top_n: int,
) -> tuple[list[str], list[dict[str, str]]] | None:
    """一次调用产出要点数组 + 术语表；失败返回 None。限制约 400 token。"""
    try:
        from downloader.config import config as _cfg
        from downloader import polish
    except Exception:
        return None
    base = str(_cfg.get("llm_base_url") or "").strip()
    model = str(_cfg.get("llm_model") or "").strip()
    key = str(_cfg.get("llm_api_key") or "").strip()
    if not base or not model:
        return None

    texts = [str(s.get("text") or "").strip() for s in segments if s.get("text")]
    if not texts:
        return None
    sketch = sample_for_llm(texts, budget=2400)
    prompt = (
        "根据下面口播摘要，输出 JSON 对象，键为 key_points 与 glossary，不要解释。\n"
        f"key_points：最多 {max_points} 条中文要点，每条不超过 28 字。\n"
        f"glossary：最多 {top_n} 项，每项 {{term, note}}，note 为不超过 20 字的释义。\n"
        f"摘要：\n{sketch}"
    )
    try:
        raw = polish.chat_stream(
            base, key, model,
            [
                {"role": "system", "content": "你是中文知识稿助手。只输出合法 JSON 对象。"},
                {"role": "user", "content": prompt},
            ],
            max_tokens=400,
            temperature=0.2,
        )
    except Exception:
        return None
    text = (raw or "").strip()
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None

    points: list[str] = []
    for item in (obj.get("key_points") or [])[:max_points]:
        t = re.sub(r"\s+", "", str(item or "").strip())[:36]
        if t:
            points.append(t)

    glossary: list[dict[str, str]] = []
    for item in (obj.get("glossary") or [])[:top_n]:
        if isinstance(item, dict):
            term = str(item.get("term") or "").strip()[:32]
            note = str(item.get("note") or "").strip()[:40]
        else:
            term = str(item or "").strip()[:32]
            note = ""
        if term:
            glossary.append({"term": term, "note": note or "文中专名"})
    if not points and not glossary:
        return None
    return points, glossary


def timestamp_index(segments: list[dict[str, Any]], *, step_sec: float = 60.0) -> list[dict[str, Any]]:
    """时间戳索引：每 step_sec 取一句。"""
    out: list[dict[str, Any]] = []
    next_at = 0.0
    for seg in segments:
        try:
            start = float(seg.get("start") or 0)
        except (TypeError, ValueError):
            continue
        text = str(seg.get("text") or "").strip()
        if not text or start < next_at:
            continue
        out.append({"at": round(start, 1), "text": text[:80]})
        next_at = start + step_sec
    return out


def render_dual_column_md(
    meta: dict[str, Any],
    segments: list[dict[str, Any]],
) -> str:
    """双栏对照：左 raw_text / 右 text。"""
    title = str(meta.get("title") or "双栏对照")
    lines = [f"# {title} · 双栏对照", "", "| 原始 ASR | 整理后 |", "| --- | --- |"]
    for seg in segments:
        raw = str(seg.get("raw_text") or seg.get("text") or "").replace("|", "\\|").strip()
        cur = str(seg.get("text") or "").replace("|", "\\|").strip()
        if not raw and not cur:
            continue
        lines.append(f"| {raw} | {cur} |")
    return "\n".join(lines) + "\n"


def chapter_quality(
    chapters: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    *,
    min_seconds: float = 20.0,
) -> dict[str, Any]:
    """章节质量评估：空章、过短、越界、重名与正文覆盖空洞。

    ``score`` 100 为满分；每个问题扣分并附可执行的修复建议。
    这是「自动分段是否可信」的可观测指标，不阻塞出稿。
    """
    issues: list[dict[str, str]] = []
    total_end = 0.0
    for s in segments:
        try:
            total_end = max(total_end, float(s.get("end") or s.get("start") or 0))
        except (TypeError, ValueError):
            continue

    seen: dict[str, int] = {}
    prev_start = -1.0
    for i, ch in enumerate(chapters):
        title = str(ch.get("title") or "").strip()
        try:
            start = float(ch.get("start") or 0)
            end = float(ch.get("end") or start)
        except (TypeError, ValueError):
            issues.append({"code": "chapter.bad_time", "detail": f"第 {i + 1} 章时间不可解析"})
            continue
        if not title:
            issues.append({"code": "chapter.empty_title", "detail": f"第 {i + 1} 章没有标题"})
        if end - start < min_seconds:
            issues.append({
                "code": "chapter.too_short",
                "detail": f"「{title or i + 1}」仅 {end - start:.0f}s，建议并入相邻章",
            })
        if start < prev_start:
            issues.append({"code": "chapter.unordered", "detail": f"「{title}」时间倒序"})
        prev_start = start
        if total_end and end > total_end + 1:
            issues.append({"code": "chapter.out_of_range", "detail": f"「{title}」超出素材时长"})
        if title:
            seen[title] = seen.get(title, 0) + 1
            if seen[title] == 2:
                issues.append({"code": "chapter.duplicate_title", "detail": f"标题重复：{title}"})

    if chapters:
        covered = sum(
            max(0.0, float(c.get("end") or 0) - float(c.get("start") or 0))
            for c in chapters
        )
        if total_end and covered / total_end < 0.85:
            issues.append({
                "code": "chapter.gap",
                "detail": f"章节只覆盖 {covered / total_end:.0%} 的时长，存在空洞",
            })

    score = max(0, 100 - 15 * len(issues))
    return {"ok": not issues, "score": score, "issues": issues}


def _term_key(term: str) -> str:
    """同一术语的归一键：ASCII 忽略空格/点/连字符/大小写，中文保持原样。"""
    t = term.strip()
    if all(ord(c) < 128 for c in t):
        return re.sub(r"[\s.\-_]+", "", t).lower()
    return t


def normalize_terms(
    segments: list[dict[str, Any]],
    *,
    min_count: int = 2,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """实体/术语消歧：把同一术语的变体收敛到最高频写法。

    例：``A.I.`` / ``AI`` / ``Ai`` → ``AI``；只处理出现 ≥ ``min_count`` 次的
    变体，避免把偶然出现的拼写错误当成规范名。返回 (新段列表, 映射)。
    """
    blob = "".join(str(s.get("text") or "") for s in segments)
    counts: dict[str, int] = {}
    for ent in extract_entities(blob):
        n = blob.count(ent)
        if n >= min_count and len(_term_key(ent)) >= 2:
            counts[ent] = n

    groups: dict[str, list[tuple[str, int]]] = {}
    for term, n in counts.items():
        groups.setdefault(_term_key(term), []).append((term, n))

    mapping: dict[str, str] = {}
    for items in groups.values():
        if len(items) < 2:
            continue
        items.sort(key=lambda kv: (-kv[1], kv[0]))
        canonical = items[0][0]
        for term, _ in items[1:]:
            if term != canonical:
                mapping[term] = canonical

    if not mapping:
        return segments, {}

    out: list[dict[str, Any]] = []
    for seg in segments:
        text = str(seg.get("text") or "")
        for variant, canonical in mapping.items():
            if variant in text:
                # ASCII 变体要求边界，避免把 "AI" 替换进 "AID"
                if all(ord(c) < 128 for c in variant):
                    text = re.sub(
                        rf"(?<![A-Za-z0-9]){re.escape(variant)}(?![A-Za-z0-9])",
                        canonical, text,
                    )
                else:
                    text = text.replace(variant, canonical)
        out.append({**seg, "text": text})
    return out, mapping

