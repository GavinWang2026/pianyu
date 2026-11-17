# Copyright (c) 2026 Hyht-Team. All rights reserved.
# See LICENSE (proprietary) and NOTICE.md (third-party components).
"""LLM 三路径 mock 验收：章节标题 / 摘要 / 要点（不联网）。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 测试隔离（HIST-01）：单测绝不写生产 outputs/avd_tasks.sqlite3，
# 否则 UI 任务历史会被 unit-* 幽灵行污染（26/26 点开全空）。
import os
import tempfile
os.environ.setdefault(
    "AVD_DB_PATH",
    str(Path(tempfile.mkdtemp(prefix="avd-test-db-")) / "tasks.sqlite3"),
)
# 素材库同样要隔离：库路径写死时单测会把「覆盖率样本」灌进真实素材库
os.environ.setdefault(
    "AVD_LIBRARY_DB",
    str(Path(tempfile.mkdtemp(prefix="avd-test-lib-")) / "library.sqlite3"),
)

from fusion import boot_check  # noqa: E402

boot_check.activate()


class _Cfg:
    def __init__(self, data: dict) -> None:
        self._data = data

    def get(self, key: str, default=None):
        return self._data.get(key, default)


class LlmPathMockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.segs = [
            {"start": 0, "end": 10, "text": "短视频增长方法论讲选题与开头"},
            {"start": 10, "end": 20, "text": "本地优先架构保障数据不出域"},
            {"start": 20, "end": 30, "text": "导出 Markdown 进入 Obsidian"},
        ]
        self.cfg = _Cfg({
            "llm_base_url": "http://127.0.0.1:11434",
            "llm_model": "mock-model",
            "llm_api_key": "sk-test",
        })

    def test_chapter_titles_llm_mock(self) -> None:
        from fusion import chapter_titles
        from fusion.knowledge import Chapter
        chs = [
            Chapter(title="a", start=0, end=10),
            Chapter(title="b", start=10, end=20),
            Chapter(title="c", start=20, end=30),
        ]
        with mock.patch("downloader.config.config", self.cfg):
            with mock.patch(
                "downloader.polish.chat_stream",
                return_value='["开场选题","本地优先","导出入库"]',
            ):
                out, src = chapter_titles.enrich_chapter_titles(chs, self.segs, use_llm=True)
        self.assertEqual(src, "llm")
        self.assertEqual(len(out), 3)
        self.assertIn("开场选题", out[0].title)

    def test_enrich_insights_llm_mock(self) -> None:
        from fusion import quality
        payload = (
            '{"key_points":["选题开头","本地优先","导出入库"],'
            '"glossary":[{"term":"ASR","note":"语音识别"}]}'
        )
        with mock.patch("downloader.config.config", self.cfg):
            with mock.patch("downloader.polish.chat_stream", return_value=payload):
                points, gloss, src = quality.enrich_insights(self.segs, use_llm=True)
        self.assertEqual(src, "llm")
        self.assertTrue(any("选题" in p or "开头" in p for p in points))
        self.assertTrue(any(g.get("term") == "ASR" for g in gloss))

    def test_enrich_summary_llm_mock(self) -> None:
        from fusion import pipeline
        payload = (
            '{"summary":"讲短视频增长与本地知识库。",'
            '"keywords":["短视频","本地优先","Obsidian"]}'
        )
        with mock.patch("downloader.config.config", self.cfg):
            with mock.patch("downloader.polish.chat_stream", return_value=payload):
                summary, kws, src = pipeline.enrich_summary(self.segs, use_llm=True)
        self.assertEqual(src, "llm")
        self.assertIn("短视频", summary)
        self.assertIn("本地优先", kws)

    def test_library_answer_llm_mock_with_sources(self) -> None:
        from fusion import library
        with tempfile.TemporaryDirectory() as tmp:
            library.reset_for_tests(Path(tmp) / "lib.sqlite3")
            library.upsert_material(
                task_id="t1",
                title="增长讲义",
                body="转化路径是核心增长杠杆，需要本地优先落地",
                tags=["增长"],
                meta={
                    "duration": 120,
                    "timestamp_index": [{"at": 45.0, "text": "转化路径是核心增长杠杆"}],
                    "segments": [{"start": 45.0, "end": 50.0, "text": "转化路径是核心增长杠杆"}],
                },
            )
            hits = library.search("转化路径")
            self.assertTrue(hits)
            self.assertEqual(hits[0].get("at"), 45.0)
            self.assertEqual(hits[0].get("at_clock"), "00:00:45")
            with mock.patch("downloader.config.config", self.cfg):
                with mock.patch(
                    "downloader.polish.chat_stream",
                    return_value="转化路径要本地优先落地。",
                ):
                    ans = library.answer("转化路径", use_llm=True)
            self.assertTrue(ans["ok"])
            self.assertEqual(ans["source"], "llm")
            self.assertIn("出处", ans["answer"])
            self.assertIn("00:00:45", ans["answer"])
            library.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
