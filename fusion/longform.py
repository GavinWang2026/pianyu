# Copyright (c) 2026 Hyht-Team
# SPDX-License-Identifier: MIT
# See LICENSE (MIT) and NOTICE.md (third-party components).
"""长素材分层摘要（map-reduce）+ 覆盖度与出处。

两小时以上的访谈/课程一次性塞进 2400 字预算，必然丢掉后半程。这里按
「段 → 块」装箱，逐块摘要（map），再合并成总摘要（reduce），并输出：

* ``coverage``：总块数 / 成功块数 / 覆盖率 / 总字数，回答「摘要覆盖了多少素材」
* ``provenance``：每块的时间范围与要点，回答「这句话出自哪一段」

LLM 不可用或超时逐块回退规则要点，覆盖率仍按成功块统计，不虚报。
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

DEFAULT_CHUNK_CHARS = 1800
DEFAULT_MAX_CHUNKS = 24
_MIN_CHUNK_CHARS = 600

LlmCall = Callable[[str, int], "str | None"]


def _seg_text(seg: dict[str, Any]) -> str:
    return str(seg.get("text") or "").strip()


def chunk_segments(
    segments: list[dict[str, Any]],
    *,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    max_chunks: int = DEFAULT_MAX_CHUNKS,
) -> list[dict[str, Any]]:
    """按字符预算把连续段装箱；块数超限时放大预算而不是丢素材。"""
    items = [s for s in segments if _seg_text(s)]
    if not items:
        return []
    total = sum(len(_seg_text(s)) for s in items)
    budget = max(_MIN_CHUNK_CHARS, int(chunk_chars))
    if max_chunks > 0 and total > budget * max_chunks:
        budget = max(budget, total // max_chunks + 1)

    chunks: list[dict[str, Any]] = []
    buf: list[dict[str, Any]] = []
    size = 0
    for seg in items:
        n = len(_seg_text(seg))
        if buf and size + n > budget:
            chunks.append(_make_chunk(buf, len(chunks)))
            buf, size = [], 0
        buf.append(seg)
        size += n
    if buf:
        chunks.append(_make_chunk(buf, len(chunks)))
    return chunks


def _make_chunk(buf: list[dict[str, Any]], index: int) -> dict[str, Any]:
    starts: list[float] = []
    ends: list[float] = []
    for s in buf:
        try:
            starts.append(float(s.get("start") or 0))
            ends.append(float(s.get("end") or s.get("start") or 0))
        except (TypeError, ValueError):
            continue
    return {
        "index": index,
        "start": min(starts) if starts else 0.0,
        "end": max(ends) if ends else 0.0,
        "chars": sum(len(_seg_text(s)) for s in buf),
        "text": "\n".join(_seg_text(s) for s in buf),
        "segments": buf,
    }


def _rule_points(chunk: dict[str, Any], limit: int = 3) -> list[str]:
    from . import quality  # 局部导入：quality 依赖重，保持本模块可独立测试

    try:
        points = quality.rule_key_points(chunk["segments"], max_points=limit)
    except Exception:
        points = []
    if points:
        return [str(p) for p in points]
    text = str(chunk.get("text") or "")
    return [text[:80]] if text else []


def _map_one(chunk: dict[str, Any], llm: LlmCall | None) -> dict[str, Any]:
    """单块摘要：LLM 优先，失败立即回退规则；返回 points 与来源。"""
    fallback = _rule_points(chunk)
    if llm is None:
        return {"points": fallback, "source": "rule"}
    prompt = (
        "用不超过 80 字概括下面这段口播，并给出 2-3 个要点（每行一个，保留数字、结论、"
        "专有名词）。只输出正文，不要客套。\n\n"
        f"{chunk['text'][:2400]}"
    )
    try:
        raw = llm(prompt, 220)
    except Exception:
        raw = None
    lines = [
        re.sub(r"^[\-\*\d.、\s]+", "", ln).strip()
        for ln in str(raw or "").splitlines()
        if ln.strip()
    ]
    lines = [ln for ln in lines if ln][:3]
    if not lines:
        return {"points": fallback, "source": "rule"}
    return {"points": lines, "source": "llm"}


def _reduce(points: list[str], chunks: list[dict[str, Any]],
            llm: LlmCall | None) -> tuple[str, list[str], str]:
    """合并各块要点。LLM 合并失败时用带时间锚的规则摘要。"""
    if llm is not None and len(points) > 1:
        numbered = "\n".join(
            f"[{int(c['start'])}-{int(c['end'])}] {p}"
            for c, p in zip(chunks, points)
        )
        prompt = (
            "把下面的分段要点合并成一段不超过 180 字的中文总摘要，并给 5-8 个关键词。"
            "只输出 JSON：{\"summary\":\"...\",\"keywords\":[\"…\"]}。\n\n"
            f"{numbered}"
        )
        try:
            raw = llm(prompt, 320)
        except Exception:
            raw = None
        m = re.search(r"\{[\s\S]*\}", str(raw or ""))
        if m:
            try:
                obj = json.loads(m.group(0))
            except json.JSONDecodeError:
                obj = None
            if isinstance(obj, dict) and str(obj.get("summary") or "").strip():
                kws = obj.get("keywords") or []
                if isinstance(kws, str):
                    kws = re.split(r"[,，、;；\s]+", kws)
                words = [str(k).strip()[:24] for k in kws if str(k).strip()][:8]
                return str(obj["summary"]).strip()[:220], words, "llm_mapreduce"

    # 规则合并：每个块一条，带起止时间
    lines: list[str] = []
    for chunk, point in zip(chunks, points):
        lines.append(
            f"- [{int(chunk['start'])}-{int(chunk['end'])}s] {point}"
        )
        if len(lines) >= 12:
            break
    summary = "【分层摘要】\n" + "\n".join(lines)
    return summary, [], "rule_mapreduce"


def _default_llm(prompt: str, max_tokens: int) -> str | None:
    try:
        from downloader import polish
        from downloader.config import config as _cfg
    except Exception:
        return None
    base = str(_cfg.get("llm_base_url") or "").strip()
    model = str(_cfg.get("llm_model") or "").strip()
    key = str(_cfg.get("llm_api_key") or "").strip()
    if not base or not model:
        return None
    try:
        return polish.chat_stream(
            base, key, model,
            [
                {"role": "system", "content": "你是中文知识稿助手，只输出要求的内容。"},
                {"role": "user", "content": prompt},
            ],
            max_tokens=max_tokens,
            temperature=0.2,
        )
    except Exception:
        return None


def map_reduce_summary(
    segments: list[dict[str, Any]],
    *,
    llm: LlmCall | None = None,
    use_llm: bool = True,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    max_chunks: int = DEFAULT_MAX_CHUNKS,
) -> dict[str, Any]:
    """返回 ``summary / keywords / source / coverage / provenance``。"""
    chunks = chunk_segments(
        segments, chunk_chars=chunk_chars, max_chunks=max_chunks)
    if not chunks:
        return {
            "summary": "", "keywords": [], "source": "empty",
            "coverage": {"chunks": 0, "covered": 0, "ratio": 0.0, "chars": 0},
            "provenance": [],
        }

    caller = (llm if llm is not None else _default_llm) if use_llm else None
    mapped = [_map_one(c, caller) for c in chunks]
    covered = sum(1 for m in mapped if m["points"])
    points = [m["points"][0] for m in mapped if m["points"]]
    summary, keywords, source = _reduce(points, chunks, caller)
    return {
        "summary": summary,
        "keywords": keywords,
        "source": source,
        "coverage": {
            "chunks": len(chunks),
            "covered": covered,
            "ratio": round(covered / len(chunks), 3),
            "chars": sum(int(c["chars"]) for c in chunks),
        },
        "provenance": [
            {
                "chunk": c["index"],
                "start": round(float(c["start"]), 1),
                "end": round(float(c["end"]), 1),
                "chars": c["chars"],
                "points": m["points"],
                "source": m["source"],
            }
            for c, m in zip(chunks, mapped)
        ],
    }
