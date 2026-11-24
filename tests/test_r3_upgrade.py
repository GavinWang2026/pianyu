# Copyright (c) 2026 Hyht-Team
# SPDX-License-Identifier: MIT
# See LICENSE (MIT) and NOTICE.md (third-party components).
"""第三批升级：分层摘要、章节质量、术语消歧、内容模板、素材库评测。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fusion import knowledge, library, longform, quality  # noqa: E402

SEGS = [
    {"start": 0.0, "end": 10.0, "text": "第一段讲倒排索引，索引结构决定检索速度与命中率。"},
    {"start": 10.0, "end": 20.0, "text": "第二段讲分层摘要，先分块再逐块提炼，最后合并。"},
    {"start": 20.0, "end": 30.0, "text": "第三段讲引用出处，每个结论都要能回到原始时间戳。"},
    {"start": 30.0, "end": 40.0, "text": "第四段讲行动项，产品负责跟进埋点，研发负责回归测试。"},
]


class LongformSummaryTests(unittest.TestCase):
    def test_chunks_cover_every_segment_within_limit(self) -> None:
        chunks = longform.chunk_segments(SEGS, chunk_chars=600, max_chunks=2)
        self.assertLessEqual(len(chunks), 2)
        got = [id(s) for c in chunks for s in c["segments"]]
        self.assertEqual(got, [id(s) for s in SEGS])
        self.assertLessEqual(chunks[0]["start"], chunks[-1]["end"])

    def test_rule_map_reduce_reports_full_coverage_and_provenance(self) -> None:
        result = longform.map_reduce_summary(SEGS, use_llm=False, chunk_chars=600)
        coverage = result["coverage"]
        self.assertEqual(coverage["covered"], coverage["chunks"])
        self.assertEqual(coverage["ratio"], 1.0)
        self.assertEqual(len(result["provenance"]), coverage["chunks"])
        self.assertEqual(result["source"], "rule_mapreduce")
        for item in result["provenance"]:
            self.assertIn(str(int(item["start"])), result["summary"])

    def test_llm_map_reduce_merges_with_keywords(self) -> None:
        calls: list[str] = []
        long_segs = [
            {"start": i * 10.0, "end": (i + 1) * 10.0, "text": text * 20}
            for i, text in enumerate([
                "第一段讲倒排索引，索引结构决定检索速度与命中率。",
                "第二段讲分层摘要，先分块再逐块提炼，最后合并。",
                "第三段讲引用出处，每个结论都要能回到原始时间戳。",
            ])
        ]

        def fake_llm(prompt: str, max_tokens: int) -> str:
            calls.append(prompt)
            if prompt.startswith("把下面的分段要点"):
                return '{"summary":"三点合一的总摘要","keywords":["索引","摘要","出处"]}'
            return "本段要点一\n本段要点二"

        result = longform.map_reduce_summary(long_segs, llm=fake_llm, chunk_chars=600)
        self.assertEqual(result["source"], "llm_mapreduce")
        self.assertEqual(result["summary"], "三点合一的总摘要")
        self.assertEqual(result["keywords"], ["索引", "摘要", "出处"])
        self.assertEqual(result["coverage"]["ratio"], 1.0)
        self.assertEqual(len(calls), result["coverage"]["chunks"] + 1)

    def test_llm_failure_falls_back_but_never_fakes_coverage(self) -> None:
        def broken(prompt: str, max_tokens: int) -> str:
            raise RuntimeError("超时")

        result = longform.map_reduce_summary(SEGS, llm=broken, chunk_chars=600)
        self.assertEqual(result["source"], "rule_mapreduce")
        self.assertEqual(result["coverage"]["covered"], result["coverage"]["chunks"])

    def test_empty_material_is_explicit(self) -> None:
        result = longform.map_reduce_summary([], use_llm=False)
        self.assertEqual(result["source"], "empty")
        self.assertEqual(result["coverage"]["chunks"], 0)


class ChapterQualityTests(unittest.TestCase):
    def test_clean_chapters_score_full(self) -> None:
        chapters = [
            {"title": "开场", "start": 0, "end": 20},
            {"title": "主体", "start": 20, "end": 40},
        ]
        result = quality.chapter_quality(chapters, SEGS)
        self.assertTrue(result["ok"])
        self.assertEqual(result["score"], 100)

    def test_bad_chapters_are_reported(self) -> None:
        chapters = [
            {"title": "", "start": 0, "end": 5},
            {"title": "重复", "start": 20, "end": 25},
            {"title": "重复", "start": 10, "end": 15},
            {"title": "越界", "start": 40, "end": 999},
        ]
        result = quality.chapter_quality(chapters, SEGS)
        self.assertFalse(result["ok"])
        codes = {i["code"] for i in result["issues"]}
        for code in ("chapter.empty_title", "chapter.too_short",
                     "chapter.duplicate_title", "chapter.unordered",
                     "chapter.out_of_range"):
            self.assertIn(code, codes)
        self.assertLess(result["score"], 100)

    def test_gap_is_reported_when_chapters_miss_most_material(self) -> None:
        result = quality.chapter_quality(
            [{"title": "只有开头", "start": 0, "end": 5}], SEGS)
        codes = {i["code"] for i in result["issues"]}
        self.assertIn("chapter.gap", codes)
        self.assertFalse(result["ok"])


class TermNormalizationTests(unittest.TestCase):
    def test_variants_converge_to_most_frequent_form(self) -> None:
        segs = [
            {"start": 0, "end": 1, "text": "AI 很重要，AI 改变一切。"},
            {"start": 1, "end": 2, "text": "Ai 也能用，但 Ai 写法不统一。"},
            {"start": 2, "end": 3, "text": "AID 是另一个词，不能被替换。"},
        ]
        out, mapping = quality.normalize_terms(segs, min_count=2)
        self.assertEqual(mapping.get("Ai"), "AI")
        self.assertIn("AI 也能用", out[1]["text"])
        self.assertNotIn("Ai 也能用", out[1]["text"])
        self.assertIn("AID", out[2]["text"])

    def test_noise_is_left_alone(self) -> None:
        segs = [{"start": 0, "end": 1, "text": "OpenAI 与 OpenAL 是不同组织。"}]
        out, mapping = quality.normalize_terms(segs)
        self.assertEqual(mapping, {})
        self.assertEqual(out[0]["text"], segs[0]["text"])


class ContentTemplateTests(unittest.TestCase):
    def _md(self, template: str) -> str:
        meta = knowledge.meta_from_info({"id": "t", "title": "模板样例"})
        meta["template"] = template
        meta["key_points"] = ["索引结构决定检索速度", "分层摘要要标出处"]
        meta["chapters"] = [{"title": "开场", "start": 0, "end": 10}]
        meta["glossary"] = [{"term": "倒排索引", "note": "用词项反查文档"}]
        segs = [
            {"start": 0, "end": 5, "text": "我们决定下周上线。", "speaker": "A"},
            {"start": 5, "end": 10, "text": "产品负责跟进埋点。", "speaker": "B"},
        ]
        return knowledge.render_markdown(
            meta, segs, summary="摘要", keywords=["索引"],
            include_frontmatter=False)

    def test_interview_course_podcast_meeting_sections(self) -> None:
        self.assertIn("## 访谈提纲", self._md("interview"))
        course = self._md("course")
        self.assertIn("## 课程大纲", course)
        self.assertIn("## 思考题", course)
        podcast = self._md("podcast")
        self.assertIn("## 本期亮点", podcast)
        self.assertIn("## 提及资源", podcast)
        meeting = self._md("meeting")
        self.assertIn("## 结论与行动项", meeting)
        self.assertIn("- [ ]", meeting)

    def test_no_template_keeps_legacy_shape(self) -> None:
        md = self._md("")
        for marker in ("## 访谈提纲", "## 课程大纲", "## 本期亮点", "## 结论与行动项"):
            self.assertNotIn(marker, md)


class LibraryEvalTests(unittest.TestCase):
    def test_eval_set_hits_recall_and_citation(self) -> None:
        fixture = json.loads(
            (ROOT / "tests" / "fixtures" / "library_eval.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            library.reset_for_tests(Path(tmp) / "eval.db")
            try:
                for m in fixture["materials"]:
                    library.upsert_material(
                        task_id=m["task_id"], title=m["title"], body=m["body"],
                        tags=m.get("tags"), collection=m.get("collection", ""),
                        meta=m.get("meta"),
                    )
                result = library.evaluate(fixture["cases"], limit=3)
            finally:
                library.reset_for_tests(None)
        self.assertEqual(result["count"], len(fixture["cases"]))
        self.assertEqual(result["recall"], 1.0)
        self.assertEqual(result["citation_accuracy"], 1.0)
        self.assertEqual(result["mrr"], 1.0)


if __name__ == "__main__":
    unittest.main()
