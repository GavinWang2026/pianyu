# Copyright (c) 2026 Hyht-Team. All rights reserved.
# See LICENSE (proprietary) and NOTICE.md (third-party components).
"""素材库：SQLite FTS5 全文检索 + 标签/合集 + 简易跨任务问答。"""

from __future__ import annotations

import atexit
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB = APP_DIR / "outputs" / "avd_library.sqlite3"

_LOCK = threading.RLock()
_CONN: sqlite3.Connection | None = None
_DB_PATH: Path | None = None
_TOKENIZER: str = "unicode61"


def configured_db_path() -> Path:
    """解析当前应使用的素材库路径。

    R2：此前库路径写死 ``outputs/avd_library.sqlite3``，于是单测跑一遍就把
    「覆盖率样本」「R1 验收素材」这类假素材灌进真实素材库（v8 只隔离了任务库
    ``avd_tasks.sqlite3``，素材库漏了）。现在与 ``persist`` 同款：环境变量优先。
    """
    raw = (os.environ.get("AVD_LIBRARY_DB") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return DEFAULT_DB


def reset_for_tests(path: Path | None = None) -> None:
    """单测用：关闭当前连接并指向新库（None = 回到默认路径）。"""
    global _CONN, _DB_PATH
    with _LOCK:
        if _CONN is not None:
            try:
                _CONN.close()
            except Exception:
                pass
        _CONN = None
        _DB_PATH = None
        if path is not None:
            _connect(path)

# 问答答案缓存：(query, hit-ids) → (expire_at, payload)；默认 24h
_ASK_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_ASK_CACHE_TTL = 24 * 3600
_ASK_LLM_TIMEOUT = 30.0
_ASK_CACHE_MAX = 128

_FTS_SPECIAL = re.compile(r'[^\w\u4e00-\u9fff]+', re.UNICODE)


def _escape_like(q: str) -> str:
    """转义 LIKE 通配符 % _ \\。"""
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fts_safe_query(q: str) -> str:
    """去掉 FTS5 特殊语法，只保留可搜索字词。"""
    cleaned = _FTS_SPECIAL.sub(" ", q).strip()
    return cleaned


def _snippet_around(body: str, query: str, *, radius: int = 60) -> str:
    """命中位置 ±radius 切片，并用「」高亮关键词。"""
    text = body or ""
    q = (query or "").strip()
    if not text:
        return ""
    if not q:
        return text[:120]
    idx = text.find(q)
    if idx < 0:
        idx = text.lower().find(q.lower())
    if idx < 0:
        # 两字词：尝试连续子串
        for i in range(len(q), 1, -1):
            sub = q[:i]
            idx = text.find(sub)
            if idx >= 0:
                q = sub
                break
    if idx < 0:
        return text[:120]
    start = max(0, idx - radius)
    end = min(len(text), idx + len(q) + radius)
    frag = text[start:end]
    # 高亮（大小写敏感优先原词）
    highlighted = frag.replace(q, f"「{q}」", 1)
    if f"「{q}」" not in highlighted:
        # 不区分大小写替换一次
        low = frag.lower()
        pos = low.find(q.lower())
        if pos >= 0:
            highlighted = (
                frag[:pos] + f"「{frag[pos:pos + len(q)]}」" + frag[pos + len(q):]
            )
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return prefix + highlighted + suffix


def _probe_trigram(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _avd_fts_probe USING fts5(x, tokenize='trigram')")
        conn.execute("DROP TABLE IF EXISTS _avd_fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


def _ensure_schema(conn: sqlite3.Connection) -> str:
    global _TOKENIZER
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS materials (
            id TEXT PRIMARY KEY,
            title TEXT,
            body TEXT,
            tags TEXT,
            collection TEXT,
            created_at REAL,
            meta TEXT
        )
        """
    )
    want = "trigram" if _probe_trigram(conn) else "unicode61"
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='materials_fts'"
    ).fetchone()
    sql = (row[0] if row else "") or ""
    need_rebuild = (not sql) or (want == "trigram" and "trigram" not in sql)
    if need_rebuild:
        conn.execute("DROP TABLE IF EXISTS materials_fts")
        conn.execute(
            f"""
            CREATE VIRTUAL TABLE materials_fts USING fts5(
                id UNINDEXED, title, body, tags,
                tokenize='{want}'
            )
            """
        )
        # 从 materials 回填
        for r in conn.execute("SELECT id, title, body, tags FROM materials"):
            conn.execute(
                "INSERT INTO materials_fts(id, title, body, tags) VALUES(?,?,?,?)",
                (r["id"], r["title"], r["body"], r["tags"]),
            )
        conn.commit()
    _TOKENIZER = want
    return want


def _connect(path: Path | None = None) -> sqlite3.Connection:
    global _CONN, _DB_PATH
    with _LOCK:
        if path is None and _CONN is not None and _DB_PATH is not None:
            return _CONN
        target = Path(path) if path is not None else (_DB_PATH or configured_db_path())
        if _CONN is not None and _DB_PATH == target:
            return _CONN
        if _CONN is not None:
            try:
                _CONN.close()
            except Exception:
                pass
            _CONN = None
        target.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(target), check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        _ensure_schema(conn)
        conn.commit()
        _CONN = conn
        _DB_PATH = target
        return conn


def close() -> None:
    """关闭单例连接（测试 teardown / atexit）。"""
    global _CONN, _DB_PATH
    with _LOCK:
        if _CONN is not None:
            try:
                _CONN.close()
            except Exception:
                pass
        _CONN = None
        _DB_PATH = None


atexit.register(close)


def reset_for_tests(path: Path | None = None) -> None:
    close()
    if path is not None:
        _connect(path)


def upsert_material(
    *,
    task_id: str,
    title: str,
    body: str,
    tags: list[str] | None = None,
    collection: str = "",
    meta: dict[str, Any] | None = None,
) -> None:
    tid = (task_id or "").strip()
    if not tid:
        return
    tag_s = ",".join(t.strip() for t in (tags or []) if str(t).strip())
    conn = _connect()
    with _LOCK:
        conn.execute("DELETE FROM materials_fts WHERE id=?", (tid,))
        conn.execute("DELETE FROM materials WHERE id=?", (tid,))
        conn.execute(
            "INSERT INTO materials(id, title, body, tags, collection, created_at, meta) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                tid,
                title or tid,
                body or "",
                tag_s,
                collection or "",
                time.time(),
                json.dumps(meta or {}, ensure_ascii=False),
            ),
        )
        conn.execute(
            "INSERT INTO materials_fts(id, title, body, tags) VALUES(?,?,?,?)",
            (tid, title or tid, body or "", tag_s),
        )
        conn.commit()


def _like_search(
    conn: sqlite3.Connection, q: str, *, limit: int, collection: str,
) -> list[tuple[sqlite3.Row, str]]:
    esc = _escape_like(q)
    like = f"%{esc}%"
    if collection:
        rows = conn.execute(
            "SELECT id, title, tags, collection, created_at, body "
            "FROM materials "
            "WHERE (title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\' "
            "OR tags LIKE ? ESCAPE '\\') AND collection = ? "
            "LIMIT ?",
            (like, like, like, collection, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, title, tags, collection, created_at, body "
            "FROM materials "
            "WHERE title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\' "
            "OR tags LIKE ? ESCAPE '\\' LIMIT ?",
            (like, like, like, limit),
        ).fetchall()
    return [(r, _snippet_around(str(r["body"] or ""), q)) for r in rows]


def _fmt_clock(sec: float) -> str:
    s = max(0, int(sec))
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def _parse_meta(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        obj = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _locate_timestamp(query: str, meta: dict[str, Any], body: str) -> float | None:
    """从 meta.timestamp_index / segments 定位关键词大致时间（秒）。"""
    q = (query or "").strip()
    if not q:
        return None
    for item in meta.get("timestamp_index") or []:
        if isinstance(item, dict) and q in str(item.get("text") or ""):
            try:
                return float(item.get("at") or 0)
            except (TypeError, ValueError):
                pass
    for seg in meta.get("segments") or []:
        if isinstance(seg, dict) and q in str(seg.get("text") or ""):
            try:
                return float(seg.get("start") or 0)
            except (TypeError, ValueError):
                pass
    # 按正文命中位置粗估
    idx = (body or "").find(q)
    if idx < 0 or not body:
        return None
    try:
        dur = float(meta.get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0.0
    if dur <= 0:
        return None
    return round(dur * (idx / max(1, len(body))), 1)


def search(query: str, *, limit: int = 20, collection: str = "") -> list[dict[str, Any]]:
    q = (query or "").strip()
    if not q:
        return []
    lim = max(1, min(100, int(limit)))
    conn = _connect()
    with _LOCK:
        safe = _fts_safe_query(q)
        rows_out: list[tuple[Any, str]] = []
        if safe:
            try:
                # trigram：短语子串；unicode61：加引号短语
                if _TOKENIZER == "trigram":
                    match_q = f'"{safe}"'
                else:
                    match_q = f'"{safe}" OR {safe}*'
                if collection:
                    rows = conn.execute(
                        "SELECT f.id, m.title, m.tags, m.collection, m.created_at, m.body, m.meta "
                        "FROM materials_fts f "
                        "JOIN materials m ON m.id = f.id "
                        "WHERE materials_fts MATCH ? AND m.collection = ? "
                        "ORDER BY rank LIMIT ?",
                        (match_q, collection, lim),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT f.id, m.title, m.tags, m.collection, m.created_at, m.body, m.meta "
                        "FROM materials_fts f "
                        "JOIN materials m ON m.id = f.id "
                        "WHERE materials_fts MATCH ? "
                        "ORDER BY rank LIMIT ?",
                        (match_q, lim),
                    ).fetchall()
                rows_out = [(r, _snippet_around(str(r["body"] or ""), q)) for r in rows]
            except sqlite3.OperationalError:
                rows_out = []
        if not rows_out:
            # LIKE 路径也带 meta
            esc = _escape_like(q)
            like = f"%{esc}%"
            if collection:
                rows = conn.execute(
                    "SELECT id, title, tags, collection, created_at, body, meta "
                    "FROM materials WHERE (title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\' "
                    "OR tags LIKE ? ESCAPE '\\') AND collection = ? LIMIT ?",
                    (like, like, like, collection, lim),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, title, tags, collection, created_at, body, meta "
                    "FROM materials WHERE title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\' "
                    "OR tags LIKE ? ESCAPE '\\' LIMIT ?",
                    (like, like, like, lim),
                ).fetchall()
            rows_out = [(r, _snippet_around(str(r["body"] or ""), q)) for r in rows]
    out: list[dict[str, Any]] = []
    for r, snip in rows_out:
        meta = _parse_meta(r["meta"] if "meta" in r.keys() else "{}")
        at = _locate_timestamp(q, meta, str(r["body"] or ""))
        item = {
            "id": r["id"],
            "title": r["title"],
            "tags": [t for t in str(r["tags"] or "").split(",") if t],
            "collection": r["collection"],
            "created_at": r["created_at"],
            "snippet": snip,
            "tokenizer": _TOKENIZER,
            "webpage_url": meta.get("webpage_url") or "",
        }
        if at is not None:
            item["at"] = at
            item["at_clock"] = _fmt_clock(at)
        out.append(item)
    return out


def answer(query: str, *, limit: int = 5, use_llm: bool = True) -> dict[str, Any]:
    """跨任务问答：Top-K 检索；有 LLM 时一次归纳，答案必须带出处与时间戳。

    LLM 路径有 30s 上限与 24h 缓存（按 query+hit-ids）；超时回退规则答案。
    """
    hits = search(query, limit=limit)
    if not hits:
        return {"ok": False, "answer": "素材库中未找到相关内容。", "sources": [], "source": "none"}

    def _cite(h: dict[str, Any]) -> str:
        clock = h.get("at_clock") or ""
        loc = f" @{clock}" if clock else ""
        return f"- 《{h['title']}》{loc}：{h['snippet']}"

    cites = [_cite(h) for h in hits]
    rule_answer = "根据已转写素材：\n" + "\n".join(cites)
    cache_key = f"{(query or '').strip()}|{','.join(str(h.get('id') or '') for h in hits)}"

    with _LOCK:
        cached = _ASK_CACHE.get(cache_key)
        if cached and cached[0] > time.time():
            payload = dict(cached[1])
            payload["cached"] = True
            return payload

    result: dict[str, Any]
    if use_llm:
        llm = _llm_answer(query, hits)
        if llm:
            result = {
                "ok": True,
                "answer": llm.rstrip() + "\n\n出处：\n" + "\n".join(cites),
                "sources": hits,
                "source": "llm",
                "cached": False,
            }
        else:
            result = {
                "ok": True, "answer": rule_answer, "sources": hits,
                "source": "rule", "cached": False,
            }
    else:
        result = {
            "ok": True, "answer": rule_answer, "sources": hits,
            "source": "rule", "cached": False,
        }

    with _LOCK:
        if len(_ASK_CACHE) >= _ASK_CACHE_MAX:
            # 简单淘汰：删最早过期的若干条
            expired = sorted(_ASK_CACHE.items(), key=lambda kv: kv[1][0])[:32]
            for k, _ in expired:
                _ASK_CACHE.pop(k, None)
        _ASK_CACHE[cache_key] = (time.time() + _ASK_CACHE_TTL, {
            "ok": result["ok"],
            "answer": result["answer"],
            "sources": result["sources"],
            "source": result["source"],
        })
    return result


def _llm_answer(query: str, hits: list[dict[str, Any]]) -> str | None:
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
    ctx = "\n".join(
        f"[{i + 1}]《{h['title']}》{h.get('at_clock') or ''} {h['snippet']}"
        for i, h in enumerate(hits)
    )
    prompt = (
        f"问题：{query}\n"
        "根据下列素材片段用中文作答，不超过 180 字；不要编造未出现的事实。\n"
        f"{ctx}"
    )
    box: dict[str, Any] = {}

    def _run() -> None:
        try:
            box["raw"] = polish.chat_stream(
                base, key, model,
                [
                    {"role": "system", "content": "你是本地知识库助手。只依据给定片段回答。"},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=280,
                temperature=0.2,
            )
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc

    t = threading.Thread(target=_run, name="avd-library-ask", daemon=True)
    t.start()
    t.join(timeout=_ASK_LLM_TIMEOUT)
    if t.is_alive():
        return None
    if box.get("error"):
        return None
    text = str(box.get("raw") or "").strip()
    return text[:400] if text else None


def batch_set_collection(task_ids: list[str], collection: str) -> int:
    """批量改合集。"""
    ids = [str(t).strip() for t in task_ids if str(t).strip()]
    if not ids:
        return 0
    conn = _connect()
    with _LOCK:
        n = 0
        for tid in ids:
            cur = conn.execute(
                "UPDATE materials SET collection=? WHERE id=?",
                (collection or "", tid),
            )
            n += cur.rowcount
        conn.commit()
    return n


def _reindex_fts(conn: sqlite3.Connection, tid: str) -> None:
    row = conn.execute(
        "SELECT id, title, body, tags FROM materials WHERE id=?", (tid,),
    ).fetchone()
    if not row:
        return
    conn.execute("DELETE FROM materials_fts WHERE id=?", (tid,))
    conn.execute(
        "INSERT INTO materials_fts(id, title, body, tags) VALUES(?,?,?,?)",
        (row["id"], row["title"], row["body"], row["tags"]),
    )


def batch_add_tags(task_ids: list[str], tags: list[str]) -> int:
    """批量追加标签。"""
    ids = [str(t).strip() for t in task_ids if str(t).strip()]
    add = [str(t).strip() for t in tags if str(t).strip()]
    if not ids or not add:
        return 0
    conn = _connect()
    with _LOCK:
        n = 0
        for tid in ids:
            row = conn.execute("SELECT tags FROM materials WHERE id=?", (tid,)).fetchone()
            if not row:
                continue
            old = [t for t in str(row["tags"] or "").split(",") if t]
            merged = list(dict.fromkeys(old + add))
            conn.execute("UPDATE materials SET tags=? WHERE id=?", (",".join(merged), tid))
            _reindex_fts(conn, tid)
            n += 1
        conn.commit()
    return n


def auto_cluster_by_chapters(*, limit: int = 200) -> dict[str, Any]:
    """按章节标题关键词自动归类标签（不改合集名，只补 tags）。"""
    conn = _connect()
    updated = 0
    with _LOCK:
        rows = conn.execute(
            "SELECT id, tags, meta FROM materials ORDER BY created_at DESC LIMIT ?",
            (max(1, min(2000, int(limit))),),
        ).fetchall()
        for row in rows:
            meta = _parse_meta(row["meta"])
            titles = []
            for ch in meta.get("chapters") or []:
                if isinstance(ch, dict):
                    t = str(ch.get("title") or "").replace("（自动分段）", "").strip()
                    if t and t not in ("正文",):
                        titles.append(t[:16])
            if not titles:
                continue
            old = [t for t in str(row["tags"] or "").split(",") if t]
            merged = list(dict.fromkeys(old + titles[:6]))
            if merged == old:
                continue
            conn.execute("UPDATE materials SET tags=? WHERE id=?", (",".join(merged), row["id"]))
            _reindex_fts(conn, row["id"])
            updated += 1
        conn.commit()
    return {"updated": updated, "scanned": len(rows)}


def list_collections() -> list[dict[str, Any]]:
    conn = _connect()
    with _LOCK:
        rows = conn.execute(
            "SELECT collection, COUNT(*) AS n FROM materials "
            "WHERE collection != '' GROUP BY collection ORDER BY n DESC"
        ).fetchall()
    return [{"collection": r["collection"], "count": r["n"]} for r in rows]


def list_materials(
    *,
    collection: str = "",
    limit: int = 100,
) -> list[dict[str, Any]]:
    """列出素材（可按合集过滤），含正文摘要。"""
    lim = max(1, min(500, int(limit)))
    conn = _connect()
    with _LOCK:
        if collection:
            rows = conn.execute(
                "SELECT id, title, tags, collection, created_at, body, meta "
                "FROM materials WHERE collection = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (collection, lim),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, title, tags, collection, created_at, body, meta "
                "FROM materials ORDER BY created_at DESC LIMIT ?",
                (lim,),
            ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        body = str(r["body"] or "")
        out.append({
            "id": r["id"],
            "title": r["title"],
            "tags": [t for t in str(r["tags"] or "").split(",") if t],
            "collection": r["collection"],
            "created_at": r["created_at"],
            "body": body,
            "preview": body[:240],
            "meta": _parse_meta(r["meta"]),
        })
    return out


def export_booklet(
    collection: str,
    *,
    dest_dir: str | Path | None = None,
    fmt: str = "md",
    template: str = "delivery",
) -> dict[str, Any]:
    """一个合集 → 一本带封面/目录/章节分页的册子（MD / DOCX / PDF，同源 LayoutSpec）。"""
    coll = (collection or "").strip()
    if not coll:
        raise ValueError("缺少 collection")
    items = list_materials(collection=coll, limit=200)
    if not items:
        raise ValueError(f"合集为空或不存在：{coll}")

    from . import layout as layout_mod

    doc = layout_mod.build_booklet(coll, items, template=template)
    want = (fmt or "md").strip().lower()
    want_md = want in ("md", "both", "all", "")
    want_docx = want in ("docx", "both", "all")
    want_pdf = want in ("pdf", "all")
    # both = md+docx（兼容旧调用）；all = 三件套
    if want == "both":
        want_md = want_docx = True
        want_pdf = False

    rendered = layout_mod.render_all(
        doc, want_md=want_md, want_docx=want_docx, want_pdf=want_pdf,
    )

    root = Path(dest_dir) if dest_dir else (APP_DIR / "outputs" / "booklets")
    root.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", coll)[:48] or "booklet"
    result: dict[str, Any] = {
        "collection": coll,
        "count": len(items),
        "template": doc.spec.name,
        "fmt": want or "md",
    }

    if want_md and rendered.get("md"):
        md_path = root / f"{safe}.md"
        md_path.write_text(str(rendered["md"]), encoding="utf-8")
        result["md"] = str(md_path)

    if want_docx:
        try:
            raw = rendered.get("docx")
            if not isinstance(raw, (bytes, bytearray)):
                raise RuntimeError("DOCX 渲染失败")
            docx_path = root / f"{safe}.docx"
            docx_path.write_bytes(bytes(raw))
            result["docx"] = str(docx_path)
        except Exception as exc:  # noqa: BLE001
            result["docx_error"] = str(exc)[:200]

    if want_pdf:
        try:
            raw = rendered.get("pdf")
            if raw is None:
                raise RuntimeError(rendered.get("pdf_error") or "PDF 不可用")
            pdf_path = root / f"{safe}.pdf"
            pdf_path.write_bytes(bytes(raw))
            result["pdf"] = str(pdf_path)
        except Exception as exc:  # noqa: BLE001
            result["pdf_error"] = str(exc)[:200]

    # 回填实际产出的 fmt 标签
    produced = []
    if result.get("md"):
        produced.append("md")
    if result.get("docx"):
        produced.append("docx")
    if result.get("pdf"):
        produced.append("pdf")
    if produced:
        result["fmt"] = "+".join(produced) if len(produced) > 1 else produced[0]
    return result


def tokenizer_name() -> str:
    _connect()
    return _TOKENIZER


def evaluate(cases: list[dict[str, Any]], *, limit: int = 5) -> dict[str, Any]:
    """素材库问答评测：Recall@K、引用命中率、MRR。

    ``cases`` 每项形如
    ``{"query": "索引结构", "task_id": "abc123", "contains": "倒排"}``。
    只看检索与引用，不调用 LLM，结果可复现；用于回归「改了索引后答不准」。
    """
    lim = max(1, min(20, int(limit)))
    rows: list[dict[str, Any]] = []
    for case in cases:
        query = str(case.get("query") or "").strip()
        expect = str(case.get("task_id") or "").strip()
        needle = str(case.get("contains") or "").strip()
        hits = search(query, limit=lim) if query else []
        ids = [str(h.get("id") or "") for h in hits]
        rank = ids.index(expect) + 1 if expect and expect in ids else 0
        cited = False
        if expect:
            for h in hits:
                if str(h.get("id") or "") != expect:
                    continue
                blob = f"{h.get('title') or ''}{h.get('snippet') or ''}"
                if not needle or needle in blob:
                    cited = True
                    break
        rows.append({
            "query": query,
            "task_id": expect,
            "hit": bool(rank),
            "rank": rank,
            "citation": cited,
        })

    n = len(rows)
    return {
        "count": n,
        "recall": round(sum(1 for r in rows if r["hit"]) / n, 3) if n else 0.0,
        "citation_accuracy": round(sum(1 for r in rows if r["citation"]) / n, 3) if n else 0.0,
        "mrr": round(
            sum(1.0 / r["rank"] for r in rows if r["rank"]) / n, 3
        ) if n else 0.0,
        "k": lim,
        "rows": rows,
    }

