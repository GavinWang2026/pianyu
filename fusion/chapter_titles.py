# Copyright (c) 2026 Hyht-Team. All rights reserved.
# See LICENSE (proprietary) and NOTICE.md (third-party components).
"""章节标题：有 LLM 一次调用生成 3–8 个；无 LLM 用 TF-IDF 前 3 词回退。"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from .knowledge import Chapter, _CHAPTER_STOP, _title_from_text

_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9_\-]{1,}")


def _tokenize(text: str) -> list[str]:
    out: list[str] = []
    for m in _TOKEN_RE.finditer(text or ""):
        t = m.group(0)
        if t in _CHAPTER_STOP or len(t) < 2:
            continue
        out.append(t)
    # 汉字 bigram 补充
    cjk = re.sub(r"[^\u4e00-\u9fff]", "", text or "")
    for i in range(len(cjk) - 1):
        gram = cjk[i:i + 2]
        if gram not in _CHAPTER_STOP:
            out.append(gram)
    return out


def tfidf_title(block_texts: list[str], *, all_blocks: list[list[str]], fallback: str) -> str:
    """块内 TF-IDF 前 2–3 词拼标题。"""
    docs = [" ".join(_tokenize("".join(b))) for b in all_blocks]
    if not docs:
        return fallback
    # 简化：当前块 vs 全局文档频率
    tokens = _tokenize("".join(block_texts))
    if not tokens:
        return fallback
    df: dict[str, int] = {}
    for doc in docs:
        seen = set(doc.split())
        for t in seen:
            df[t] = df.get(t, 0) + 1
    n_docs = max(1, len(docs))
    tf: dict[str, int] = {}
    for t in tokens:
        tf[t] = tf.get(t, 0) + 1
    scored: list[tuple[float, str]] = []
    for t, c in tf.items():
        idf = math.log((n_docs + 1) / (1 + df.get(t, 0))) + 1.0
        scored.append((c * idf, t))
    scored.sort(key=lambda x: (-x[0], x[1]))
    top = [t for _, t in scored[:3] if t]
    if not top:
        return fallback
    return "·".join(top[:2])


def enrich_chapter_titles(
    chapters: list[Chapter],
    segments: list[dict[str, Any]],
    *,
    use_llm: bool = True,
) -> tuple[list[Chapter], str]:
    """为已切好的章节补标题。返回 (chapters, source) source=llm|tfidf。"""
    if not chapters:
        return chapters, "none"
    # 按章节时间窗切段
    blocks: list[list[str]] = []
    for ch in chapters:
        texts: list[str] = []
        for seg in segments:
            try:
                mid = (float(seg.get("start") or 0) + float(seg.get("end") or 0)) / 2
            except (TypeError, ValueError):
                mid = 0.0
            end = ch.end if ch.end is not None else 1e18
            if ch.start <= mid <= end:
                t = str(seg.get("text") or "").strip()
                if t:
                    texts.append(t)
        blocks.append(texts)

    if use_llm:
        llm_titles = _llm_titles(chapters, blocks)
        if llm_titles and len(llm_titles) == len(chapters) and _titles_usable(llm_titles):
            out = [
                Chapter(title=f"{llm_titles[i]}（自动分段）", start=ch.start, end=ch.end)
                for i, ch in enumerate(chapters)
            ]
            return out, "llm"

    # TF-IDF 回退；过弱（纯短 bigram 且无信息）时改用段首句
    out: list[Chapter] = []
    for i, ch in enumerate(chapters):
        fb = f"话题 {i + 1}"
        title = tfidf_title(blocks[i], all_blocks=blocks, fallback=fb)
        if title in _CHAPTER_STOP or len(title) < 2 or _title_too_weak(title, blocks[i]):
            head = blocks[i][0] if blocks[i] else ""
            title = _title_from_text(head, fb)
        if title in _CHAPTER_STOP or len(title) < 2:
            title = fb
        out.append(Chapter(title=f"{title}（自动分段）", start=ch.start, end=ch.end))
    return out, "tfidf"


def _titles_usable(titles: list[str]) -> bool:
    """拒绝全重复 / 过短 / 全是「话题N」式空洞标题。"""
    cleaned = [re.sub(r"\s+", "", str(t or "")) for t in titles]
    if not cleaned or any(len(t) < 2 for t in cleaned):
        return False
    if len(set(cleaned)) == 1 and len(cleaned) > 1:
        return False
    hollow = sum(1 for t in cleaned if t.startswith("话题") or t in _CHAPTER_STOP)
    return hollow < len(cleaned)


def _title_too_weak(title: str, block_texts: list[str]) -> bool:
    """TF-IDF 标题若几乎全是 2 字 bigram 且与首句无关，视为过弱。"""
    t = (title or "").replace("·", "")
    if len(t) <= 2:
        return True
    if not block_texts:
        return False
    head = block_texts[0][:24]
    # 标题与首句无任何 2 字重叠 → 优先改用段首
    grams = {t[i:i + 2] for i in range(len(t) - 1)} if len(t) >= 2 else set()
    if not grams:
        return True
    return not any(g in head for g in grams)


def _llm_titles(chapters: list[Chapter], blocks: list[list[str]]) -> list[str] | None:
    """一次调用生成标题；失败返回 None。限制约 300 token。"""
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

    sketches = []
    for i, texts in enumerate(blocks):
        snippet = "".join(texts)[:180]
        sketches.append(f"{i + 1}. {snippet}")
    prompt = (
        f"下面有 {len(chapters)} 段口播摘要，请为每段生成一个不超过 12 字的中文章节标题。"
        "只输出 JSON 数组，长度必须等于段数，不要解释。\n"
        + "\n".join(sketches)
    )
    try:
        raw = polish.chat_stream(
            base, key, model,
            [
                {"role": "system", "content": "你是中文视频章节标题助手。只输出 JSON 数组。"},
                {"role": "user", "content": prompt},
            ],
            max_tokens=300,
            temperature=0.2,
        )
    except Exception:
        return None
    text = (raw or "").strip()
    # 抽取 JSON 数组
    m = re.search(r"\[[\s\S]*\]", text)
    if not m:
        return None
    try:
        arr = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(arr, list) or len(arr) != len(chapters):
        return None
    titles: list[str] = []
    for item in arr:
        t = re.sub(r"\s+", "", str(item or "").strip())[:16]
        titles.append(t or "话题")
    return titles
