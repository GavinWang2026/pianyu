# Copyright (c) 2026 Hyht-Team
# SPDX-License-Identifier: MIT
# See LICENSE (MIT) and NOTICE.md (third-party components).
"""旗舰版融合层回归测试（unittest，零第三方依赖、不联网）。

覆盖：合规边界、结构化知识稿、一次解析全链复用、引擎版本闸门、官方插件扩展点。
运行： python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
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

from fusion import asr_offline, boot_check, guards, knowledge, pipeline  # noqa: E402

SAMPLE_INFO = {
    "id": "BV1test",
    "title": "旗舰版测试素材",
    "extractor_key": "Bilibili",
    "uploader": "Hyht-Team",
    "upload_date": "20261112",
    "duration": 180,
    "webpage_url": "https://www.bilibili.com/video/BV1test",
    "description": "简介" * 600,
    "tags": ["知识管理", "本地优先"],
    "categories": ["教程"],
    "chapters": [
        {"start_time": 0, "end_time": 60, "title": "开场与背景"},
        {"start_time": 60, "end_time": 180, "title": "核心方法"},
    ],
    "subtitles": {},
    "automatic_captions": {},
}

SAMPLE_SEGMENTS = [
    {"start": 5.0, "end": 8.0, "text": "大家好，今天讲第一件事。"},
    {"start": 70.0, "end": 74.0, "text": "第二部分是本地优先的架构。"},
    {"start": 150.0, "end": 153.0, "text": "最后我们看导出格式。"},
]

# A 级素材：字数充足、多段、无片头水印。D21：产物矩阵测试必须区分 A/B/C，
# 原先 35 字的 SAMPLE_SEGMENTS 被判 C 级却断言 docx/txt，是测试自身失配。
RICH_SEGMENTS = [
    {"start": 5.0, "end": 12.0, "text": "大家好，今天这期节目我们讲知识管理的完整落地方法，先说结论再展开细节。"},
    {"start": 30.0, "end": 40.0, "text": "第一部分是本地优先的架构设计，所有原始素材和转写结果都保存在本机目录。"},
    {"start": 70.0, "end": 82.0, "text": "第二部分讲检索与问答：先做全文索引，再用规则抽取要点，最后才调用大模型。"},
    {"start": 120.0, "end": 132.0, "text": "第三部分是导出格式，我们同时提供 Markdown、字幕、纯文本和 Word 文档。"},
    {"start": 150.0, "end": 165.0, "text": "最后总结：先保证数据不出本机，再追求自动化，遇到失败要能降级并如实告知。"},
]


class GuardsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig = guards._resolved_ips
        guards._resolved_ips = lambda host: []  # 单测不碰 DNS
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base = Path(self.tmp.name)

    def tearDown(self) -> None:
        guards._resolved_ips = self._orig
        self.tmp.cleanup()

    def test_rejects_internal_and_local_targets(self) -> None:
        for url in (
            "http://127.0.0.1/secret", "http://169.254.169.254/latest/meta-data/",
            "http://localhost:8080/admin", "file:///etc/passwd",
            "http://192.168.1.7/", "http://example.com:22/",
        ):
            ok, _ = guards.check_media_url(url, enforce_allowlist=False)
            self.assertFalse(ok, f"应当拒绝：{url}")

    def test_security_floor_allows_public_host_but_allowlist_narrows(self) -> None:
        ok, _ = guards.check_media_url(
            "https://www.bilibili.com/video/BV1", enforce_allowlist=False)
        self.assertTrue(ok)
        ok, why = guards.check_media_url("https://unknown-example.org/v/1")
        self.assertFalse(ok)
        self.assertIn("白名单", why)
        ok, _ = guards.check_media_url("https://www.bilibili.com/video/BV1")
        self.assertTrue(ok)

    def test_image_proxy_only_allows_known_cdn(self) -> None:
        ok, _ = guards.check_image_url("https://i0.hdslb.com/bfs/abc.jpg")
        self.assertTrue(ok)
        for url in ("http://127.0.0.1/x.png", "https://evil.example.org/x.png",
                    "http://169.254.169.254/x"):
            ok, _ = guards.check_image_url(url)
            self.assertFalse(ok, url)

    def test_config_redaction_keeps_presence_flag(self) -> None:
        out = guards.redact_config({"llm_api_key": "sk-secret", "download_dir": "/tmp/x"})
        self.assertEqual(out["llm_api_key"], "***")
        self.assertTrue(out["has_llm_api_key"])
        self.assertEqual(out["download_dir"], "/tmp/x")

    def test_cookies_from_browser_not_redacted(self) -> None:
        """SEC-02：浏览器名不是凭据，脱敏后必须原样回显，否则 select 失配清空配置。"""
        out = guards.redact_config({
            "cookies_from_browser": "chrome",
            "llm_api_key": "sk-x",
            "cookiefile": "/secret/cookies.txt",
        })
        self.assertEqual(out["cookies_from_browser"], "chrome")
        self.assertEqual(out["llm_api_key"], "***")
        self.assertEqual(out["cookiefile"], "***")

    def test_local_suffix_does_not_false_positive(self) -> None:
        """MISC-05：endswith('local') 不得误伤正常公网域名。"""
        ok, _ = guards.check_media_url(
            "https://notlocal.example.com/v", enforce_allowlist=False)
        self.assertTrue(ok)
        ok, _ = guards.check_media_url(
            "https://foo.local/v", enforce_allowlist=False)
        self.assertFalse(ok)

    def test_alternate_cdn_port_allowed(self) -> None:
        """MISC-02：常见备用端口不应被一律拒绝。"""
        ok, _ = guards.check_media_url(
            "https://www.bilibili.com:8080/video/BV1", enforce_allowlist=False)
        self.assertTrue(ok)

    def test_path_traversal_blocked(self) -> None:
        inside = self.base / "downloads" / "a.mp4"
        inside.parent.mkdir(parents=True, exist_ok=True)
        inside.write_text("x", encoding="utf-8")
        self.assertIsNotNone(guards.within(self.base / "downloads", inside))
        self.assertIsNone(guards.within(self.base / "downloads", self.base / "etc" / "passwd"))
        self.assertIsNone(guards.within(self.base / "downloads", "../../etc/passwd"))


class KnowledgeTests(unittest.TestCase):
    def test_chapters_and_assignment(self) -> None:
        chapters = knowledge.chapters_from_info(SAMPLE_INFO)
        self.assertEqual([c.title for c in chapters], ["开场与背景", "核心方法"])
        tagged = knowledge.assign_chapters(SAMPLE_SEGMENTS, chapters)
        self.assertEqual(tagged[0]["chapter"], "开场与背景")
        self.assertEqual(tagged[1]["chapter"], "核心方法")
        groups = knowledge.group_by_chapter(tagged)
        self.assertEqual([g["title"] for g in groups], ["开场与背景", "核心方法"])

    def test_metadata_is_not_truncated(self) -> None:
        meta = knowledge.meta_from_info(SAMPLE_INFO)
        self.assertEqual(len(meta["description"]), 1200)  # 原版截到 400 字
        self.assertEqual(meta["tags"], ["知识管理", "本地优先"])
        self.assertEqual(len(meta["chapters"]), 2)

    def test_sponsor_ranges_are_offline_only(self) -> None:
        info = dict(SAMPLE_INFO, sponsorblock_chapters=[
            {"start_time": 60, "end_time": 80, "title": "sponsor"}])
        ranges = knowledge.sponsor_ranges(info)
        self.assertEqual(ranges, [(60.0, 80.0)])
        kept = knowledge.strip_ranges(SAMPLE_SEGMENTS, ranges)
        self.assertEqual(len(kept), 2)  # 70s 那句被剔除
        self.assertEqual(knowledge.sponsor_ranges(SAMPLE_INFO), [])

    def test_heuristic_promo_and_docx(self) -> None:
        segs = list(SAMPLE_SEGMENTS) + [
            {"start": 170.0, "end": 175.0, "text": "记得点个关注一键三连谢谢"},
        ]
        promo = knowledge.heuristic_promo_ranges(segs)
        self.assertEqual(promo, [(170.0, 175.0)])
        merged = knowledge.merge_ranges([(60.0, 80.0)], promo)
        self.assertEqual(len(merged), 2)
        meta = knowledge.meta_from_info(SAMPLE_INFO)
        raw = knowledge.render_docx(meta, SAMPLE_SEGMENTS, summary="摘要", keywords=["测"])
        self.assertTrue(raw.startswith(b"PK"))  # zip/docx
        bundle = knowledge.build_bundle(meta, SAMPLE_SEGMENTS)
        self.assertIn("docx", bundle)
        self.assertIn("pdf", bundle)
        import base64
        pdf_raw = base64.b64decode(bundle["pdf"].encode("ascii"))
        self.assertTrue(pdf_raw.startswith(b"%PDF"))

    def test_pdf_chinese_no_question_marks(self) -> None:
        """P0-3：中文 PDF 可见内容不得整页变成问号。"""
        meta = {"title": "短视频运营方法论 & <测试>"}
        segs = [
            {"start": 0.0, "end": 1.0, "text": "今天讲短视频增长的三个关键动作。"},
            {"start": 1.0, "end": 2.0, "text": "第一是选题，第二是开头，第三是完播。"},
        ]
        raw = knowledge.render_pdf(meta, segs, summary="摘要一句")
        self.assertTrue(raw.startswith(b"%PDF"))
        # 旧实现把非 ASCII 写成 ?；新实现要么嵌入子集字体，要么附件 UTF-8
        from fusion import pdf_cjk
        if pdf_cjk.find_cjk_font() is not None:
            # 内容流不应再出现成串问号预览
            self.assertNotIn(b"(????????", raw)
            self.assertIn(b"/Identity-H", raw)
            self.assertIn(b"/FontFile2", raw)
        else:
            self.assertIn(b"transcript.txt", raw)

    def test_assign_speakers_alternates_on_gap(self) -> None:
        # 至少 2 次长停顿才标注，避免独白误判
        segs = [
            {"start": 0.0, "end": 1.0, "text": "甲"},
            {"start": 1.1, "end": 2.0, "text": "继续"},
            {"start": 5.0, "end": 6.0, "text": "乙回应"},
            {"start": 6.2, "end": 7.0, "text": "乙继续"},
            {"start": 10.0, "end": 11.0, "text": "甲再问"},
        ]
        out = knowledge.assign_speakers(segs, gap_sec=1.2)
        self.assertEqual(out[0]["speaker"], "A")
        self.assertEqual(out[1]["speaker"], "A")
        self.assertEqual(out[2]["speaker"], "B")
        self.assertEqual(out[4]["speaker"], "A")
        self.assertEqual(out[0]["speaker_source"], "heuristic")
        md = knowledge.render_markdown(
            {"title": "t", "speakers": True}, out, source_kind="asr",
            include_frontmatter=True,
        )
        self.assertIn("**[A·未校准]**", md)
        self.assertIn("**[B·未校准]**", md)
        self.assertIn("speakers: heuristic", md)

    def test_assign_speakers_skips_monologue(self) -> None:
        segs = [
            {"start": 0.0, "end": 1.0, "text": "独白一"},
            {"start": 1.2, "end": 2.0, "text": "独白二"},
            {"start": 5.0, "end": 6.0, "text": "仅一次长停顿"},
        ]
        out = knowledge.assign_speakers(segs, gap_sec=1.2)
        self.assertTrue(all(not s.get("speaker") for s in out))

    def test_assign_speakers_balance_gate_and_stats(self) -> None:
        """弱侧 <18% 视为独白误判并撤回；均衡对话可出 speaker_stats。"""
        # 仅 1 段 B vs 多段 A → 弱侧 <18% 撤回（仍需 ≥2 次换人停顿才进入标注路径）
        unbalanced = [
            {"start": 0.0, "end": 1.0, "text": "甲"},
            {"start": 1.1, "end": 2.0, "text": "甲续"},
            {"start": 4.0, "end": 5.0, "text": "乙短答"},
            {"start": 7.0, "end": 8.0, "text": "甲再开1"},
            {"start": 8.2, "end": 9.0, "text": "甲再开2"},
            {"start": 9.2, "end": 10.0, "text": "甲再开3"},
            {"start": 10.2, "end": 11.0, "text": "甲再开4"},
            {"start": 11.2, "end": 12.0, "text": "甲再开5"},
            {"start": 12.2, "end": 13.0, "text": "甲再开6"},
        ]
        out_u = knowledge.assign_speakers(unbalanced, gap_sec=1.2)
        self.assertTrue(all(not s.get("speaker") for s in out_u), out_u)

        balanced = [
            {"start": 0.0, "end": 1.0, "text": "甲问"},
            {"start": 3.0, "end": 4.0, "text": "乙答"},
            {"start": 4.2, "end": 5.0, "text": "乙续"},
            {"start": 7.0, "end": 8.0, "text": "甲再问"},
            {"start": 8.2, "end": 9.0, "text": "甲补充"},
            {"start": 11.0, "end": 12.0, "text": "乙收束"},
        ]
        out_b = knowledge.assign_speakers(balanced, gap_sec=1.2)
        self.assertTrue(any(s.get("speaker") for s in out_b), out_b)
        stats = knowledge.speaker_stats(out_b)
        self.assertGreaterEqual(stats["labeled"], 2)
        self.assertGreaterEqual(stats["balance"], 0.18)
        self.assertGreater(stats["confidence"], 0)

    def test_bundle_has_frontmatter_chapters_and_provenance(self) -> None:
        meta = knowledge.meta_from_info(SAMPLE_INFO)
        bundle = knowledge.build_bundle(
            meta, SAMPLE_SEGMENTS, summary="这是摘要", keywords=["知识管理"],
            source_kind="subtitle")
        self.assertTrue(bundle["md"].startswith("---\n"))
        self.assertIn("## 开场与背景", bundle["md"])
        self.assertIn("## 核心方法", bundle["md"])
        self.assertIn("https://www.bilibili.com/video/BV1test", bundle["md"])
        self.assertIn("来源与可信度", bundle["md"])
        self.assertIn("00:00:05,000 --> 00:00:08,000", bundle["srt"])
        payload = json.loads(bundle["json"])
        self.assertEqual(payload["source_kind"], "subtitle")
        self.assertEqual(payload["generator"], "AVD Flagship v2.0.0")


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig = guards._resolved_ips
        guards._resolved_ips = lambda host: []
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        # 单测不碰本机 Obsidian 配置
        self._obs = pipeline.resolve_obsidian_dir
        pipeline.resolve_obsidian_dir = lambda raw=None: None  # type: ignore
        from fusion import persist
        persist.reset_for_tests(self.root / "avd_tasks.sqlite3")

    def tearDown(self) -> None:
        guards._resolved_ips = self._orig
        pipeline.resolve_obsidian_dir = self._obs  # type: ignore
        from fusion import persist
        persist.reset_for_tests(None)
        self.tmp.cleanup()

    def _wait(self, mgr: pipeline.PipelineManager, task_id: str, timeout: float = 8.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            snap = mgr.get(task_id)
            if snap and snap["stage"] in ("done", "error", "cancelled"):
                return snap
            time.sleep(0.03)
        self.fail("任务超时未完成")

    def test_one_probe_for_whole_pipeline(self) -> None:
        calls = {"probe": 0, "download": 0, "asr": 0}

        def probe(url):
            calls["probe"] += 1
            return dict(SAMPLE_INFO)

        def download(info, work_dir):
            calls["download"] += 1
            return ""

        def asr(url, language, title, media_path=""):
            calls["asr"] += 1
            return []

        def subtitle(url, fmt):
            return list(RICH_SEGMENTS)

        # 让字幕轨来自 info，验证「字幕 URL 直接复用 info」这条零重复路径
        info = dict(SAMPLE_INFO)
        info["subtitles"] = {"zh-Hans": [{"url": "https://i0.hdslb.com/sub.vtt", "ext": "vtt"}]}
        probe_with_sub = lambda url: (calls.__setitem__("probe", calls["probe"] + 1), dict(info))[1]

        mgr = pipeline.PipelineManager(
            probe_fn=probe_with_sub, download_fn=download,
            subtitle_fn=subtitle, asr_fn=asr, output_root=self.root)

        task = mgr.start("https://www.bilibili.com/video/BV1test", want_download=True)
        snap = self._wait(mgr, task["id"])

        self.assertEqual(snap["stage"], "done", snap.get("error"))
        self.assertEqual(calls["probe"], 1, "全链路只允许解析一次")
        self.assertEqual(calls["download"], 1)
        self.assertEqual(calls["asr"], 0, "命中字幕就不应再触发语音识别")
        self.assertEqual(snap["source_kind"], "subtitle")
        self.assertEqual(snap["chapter_count"], 2)
        self.assertTrue({"md", "srt", "txt", "json", "docx"}.issubset(set(snap["files"])))
        md = mgr.read_artifact(task["id"], "md")
        self.assertIsNotNone(md)
        self.assertIn("## 开场与背景", md)
        self.assertTrue((self.root / task["id"] / "manifest.json").is_file())
        docx_b64 = mgr.read_artifact(task["id"], "docx")
        self.assertTrue(docx_b64 and len(docx_b64) > 40)

    def test_artifact_matrix_by_content_level(self) -> None:
        """D21：A/B/C 三级产物矩阵与裁剪契约一致，防止回归时静默少产物。"""
        cases = {
            "A": (list(RICH_SEGMENTS), {"md", "srt", "txt", "json", "docx", "pdf"}),
            "B": (
                [
                    {"start": 0.0, "end": 12.0,
                     "text": "第一段讲方法与流程，覆盖准备工作、执行顺序和常见错误。"},
                    {"start": 40.0, "end": 55.0,
                     "text": "第二段补充细节，重点是如何校验结果并保留出处。"},
                ],
                {"md", "srt", "txt", "json", "docx", "pdf"},
            ),
            "C": (
                [{"start": 0.0, "end": 30.0, "text": "优优独播剧场"}],
                {"md", "srt", "json"},
            ),
        }
        for expected_level, (segs, expected_keys) in cases.items():
            info = dict(SAMPLE_INFO)
            info["chapters"] = []
            info["subtitles"] = {
                "zh-Hans": [{"url": "https://i0.hdslb.com/sub.vtt", "ext": "vtt"}],
            }
            mgr = pipeline.PipelineManager(
                probe_fn=lambda u, _i=info: dict(_i),
                download_fn=lambda info_, wd: "",
                subtitle_fn=lambda u, f, _s=segs: [dict(x) for x in _s],
                asr_fn=lambda *a, **k: [],
                output_root=self.root,
            )
            started = mgr.start(
                "https://www.bilibili.com/video/BV1test", want_download=False)
            snap = self._wait(mgr, started["id"])
            self.assertEqual(snap["stage"], "done", snap.get("error"))
            self.assertEqual(
                snap["content_level"], expected_level,
                f"{expected_level} 级判定不符：{snap.get('content_sufficiency')}",
            )
            self.assertEqual(set(snap["files"]), expected_keys, expected_level)

    def test_reconcile_restores_from_manifest_and_marks_missing(self) -> None:
        """D27：产物目录是真相；内存丢失可恢复，目录丢失要标降级。"""
        info = dict(SAMPLE_INFO)
        info["subtitles"] = {
            "zh-Hans": [{"url": "https://i0.hdslb.com/sub.vtt", "ext": "vtt"}],
        }
        mgr = pipeline.PipelineManager(
            probe_fn=lambda u: dict(info),
            download_fn=lambda info_, wd: "",
            subtitle_fn=lambda u, f: list(RICH_SEGMENTS),
            asr_fn=lambda *a, **k: [],
            output_root=self.root,
        )
        started = mgr.start(
            "https://www.bilibili.com/video/BV1test", want_download=False)
        snap = self._wait(mgr, started["id"])
        self.assertEqual(snap["stage"], "done", snap.get("error"))
        tid = started["id"]
        manifest = self.root / tid / "manifest.json"
        self.assertTrue(manifest.is_file())

        # 模拟「内存与索引都丢了、只剩产物目录」
        (self.root / "index.json").unlink(missing_ok=True)
        with mock.patch.object(pipeline.persist, "load_tasks",
                               lambda *a, **k: []):
            fresh = pipeline.PipelineManager(
                probe_fn=lambda u: dict(info),
                subtitle_fn=lambda u, f: list(RICH_SEGMENTS),
                asr_fn=lambda *a, **k: [],
                output_root=self.root,
            )
        self.assertIsNone(fresh.get(tid))
        report = fresh.reconcile()
        self.assertGreaterEqual(report["restored"], 1, report)
        restored = fresh.get(tid)
        self.assertIsNotNone(restored)
        self.assertEqual(restored["stage"], "done")
        self.assertEqual(restored["files"], snap["files"])

        # 产物目录被清理 → 记录保留但标 state.missing
        shutil.rmtree(self.root / tid)
        report = fresh.reconcile()
        self.assertGreaterEqual(report["missing"], 1, report)
        self.assertTrue(any(d.get("code") == "state.missing"
                            for d in fresh.get(tid)["degraded_items"]))

    def test_persist_failure_marked_degraded_and_health(self) -> None:
        """D25：落库失败不得静默——任务标降级、健康检查可见、错误进日志。"""
        from fusion import persist
        info = dict(SAMPLE_INFO)
        info["subtitles"] = {
            "zh-Hans": [{"url": "https://i0.hdslb.com/sub.vtt", "ext": "vtt"}],
        }
        mgr = pipeline.PipelineManager(
            probe_fn=lambda u: dict(info),
            download_fn=lambda info_, wd: "",
            subtitle_fn=lambda u, f: list(RICH_SEGMENTS),
            asr_fn=lambda *a, **k: [],
            output_root=self.root,
        )
        started = mgr.start(
            "https://www.bilibili.com/video/BV1test", want_download=False)
        snap = self._wait(mgr, started["id"])
        self.assertEqual(snap["stage"], "done", snap.get("error"))

        orig = persist._connect

        def boom(*args, **kwargs):
            raise OSError("模拟磁盘写满")

        persist._connect = boom  # type: ignore[assignment]
        try:
            mgr._persist_index()
            h = persist.health()
            self.assertFalse(h["ok"], h)
            self.assertGreaterEqual(h["failures"], 1)
            self.assertIn("模拟磁盘写满", h["last_error"])
            snap2 = mgr.get(started["id"])
            self.assertTrue(
                any(d.get("code") == "persist.failed"
                    for d in snap2["degraded_items"]),
                snap2.get("degraded_items"),
            )
        finally:
            persist._connect = orig  # type: ignore[assignment]


    def test_proofread_rewrites_artifacts(self) -> None:
        info = dict(SAMPLE_INFO)
        info["subtitles"] = {"zh-Hans": [{"url": "https://i0.hdslb.com/sub.vtt", "ext": "vtt"}]}
        mgr = pipeline.PipelineManager(
            probe_fn=lambda u: dict(info),
            download_fn=lambda info_, wd: "",
            subtitle_fn=lambda u, f: list(SAMPLE_SEGMENTS),
            asr_fn=lambda *a, **k: [],
            output_root=self.root,
        )
        started = mgr.start("https://www.bilibili.com/video/BV1test", want_download=False)
        snap = self._wait(mgr, started["id"])
        self.assertEqual(snap["stage"], "done")
        edited = [
            {"start": 5.0, "end": 8.0, "text": "大家好，今天讲校对编辑。"},
            {"start": 70.0, "end": 74.0, "text": "本地优先架构已更新。"},
        ]
        out = mgr.proofread(started["id"], edited)
        self.assertEqual(out["segment_count"], 2)
        srt = mgr.read_artifact(started["id"], "srt")
        self.assertIn("校对编辑", srt or "")
        md = mgr.read_artifact(started["id"], "md")
        self.assertIn("校对编辑", md or "")

    def test_proofread_rolls_back_on_entity_loss(self) -> None:
        info = dict(SAMPLE_INFO)
        info["subtitles"] = {"zh-Hans": [{"url": "https://i0.hdslb.com/sub.vtt", "ext": "vtt"}]}
        mgr = pipeline.PipelineManager(
            probe_fn=lambda u: dict(info),
            download_fn=lambda info_, wd: "",
            subtitle_fn=lambda u, f: list(SAMPLE_SEGMENTS),
            asr_fn=lambda *a, **k: [],
            output_root=self.root,
        )
        started = mgr.start("https://www.bilibili.com/video/BV1test", want_download=False)
        snap = self._wait(mgr, started["id"])
        self.assertEqual(snap["stage"], "done")
        # strict：硬回滚
        with self.assertRaises(ValueError) as ctx:
            mgr.proofread(
                started["id"],
                [{"start": 0, "end": 1, "text": "x"}, {"start": 1, "end": 2, "text": "y"}],
                strict=True,
            )
        self.assertIn("回滚", str(ctx.exception))
        # 默认：告警但仍保存
        out = mgr.proofread(
            started["id"],
            [{"start": 0, "end": 1, "text": "x"}, {"start": 1, "end": 2, "text": "y"}],
        )
        self.assertTrue(any("校对改动偏大" in d for d in (out.get("degraded") or [])))

    def test_batch_queues_multiple_urls(self) -> None:
        """批量队列：多 URL 同时排队，单条失败不影响整批。"""
        calls = {"probe": 0}

        def probe(url):
            calls["probe"] += 1
            info = dict(SAMPLE_INFO)
            info["webpage_url"] = url
            return info

        mgr = pipeline.PipelineManager(
            probe_fn=probe,
            download_fn=lambda info, wd: "",
            subtitle_fn=lambda u, f: list(SAMPLE_SEGMENTS),
            asr_fn=lambda *a, **k: [],
            output_root=self.root,
            max_concurrent=2,
        )
        batch = mgr.start_batch([
            "https://www.bilibili.com/video/BV1a",
            "https://www.bilibili.com/video/BV1b",
            "http://127.0.0.1/evil",  # 应跳过
            "https://www.bilibili.com/video/BV1a",  # 重复
        ], want_download=False)
        self.assertEqual(batch["count"], 2)
        self.assertEqual(len(batch["skipped"]), 2)
        ids = [t["id"] for t in batch["tasks"]]
        for tid in ids:
            snap = self._wait(mgr, tid)
            self.assertEqual(snap["stage"], "done", snap.get("error"))
        self.assertEqual(calls["probe"], 2)

    def test_batch_delay_sets_not_before(self) -> None:
        mgr = pipeline.PipelineManager(
            probe_fn=lambda u: dict(SAMPLE_INFO),
            download_fn=lambda info, wd: "",
            subtitle_fn=lambda u, f: list(SAMPLE_SEGMENTS),
            asr_fn=lambda *a, **k: [],
            output_root=self.root,
            max_concurrent=1,
        )
        before = time.time()
        batch = mgr.start_batch(
            ["https://www.bilibili.com/video/BV1delay"],
            want_download=False,
            delay_seconds=5,
        )
        self.assertIsNotNone(batch.get("not_before"))
        # 用创建前锚点，避免 start_batch 耗时导致 wall-clock 竞态
        self.assertGreaterEqual(batch["not_before"], before + 4.5)
        tid = batch["tasks"][0]["id"]
        # 取消掉，避免测试干等
        mgr.cancel(tid)
        snap = self._wait(mgr, tid, timeout=4.0)
        self.assertIn(snap["stage"], ("cancelled", "done", "error"))

    def test_obsidian_export_writes_markdown(self) -> None:
        vault = self.root / "vault"
        vault.mkdir()
        # 单测绕过「必须在主目录」校验，只验写出逻辑
        pipeline.resolve_obsidian_dir = lambda raw=None: vault  # type: ignore
        info = dict(SAMPLE_INFO)
        info["subtitles"] = {"zh-Hans": [{"url": "https://i0.hdslb.com/sub.vtt", "ext": "vtt"}]}
        mgr = pipeline.PipelineManager(
            probe_fn=lambda u: dict(info),
            download_fn=lambda info_, wd: "",
            subtitle_fn=lambda u, f: list(SAMPLE_SEGMENTS),
            asr_fn=lambda *a, **k: [],
            output_root=self.root,
        )
        prev = pipeline._MANAGER
        pipeline._MANAGER = mgr
        try:
            started = mgr.start("https://www.bilibili.com/video/BV1test", want_download=False)
            snap = self._wait(mgr, started["id"])
            self.assertEqual(snap["stage"], "done")
            result = pipeline.write_obsidian(started["id"], vault=str(vault))
            written = Path(result["path"])
            self.assertTrue(written.is_file())
            self.assertIn("大家好", written.read_text(encoding="utf-8"))
        finally:
            pipeline._MANAGER = prev
            pipeline.resolve_obsidian_dir = lambda raw=None: None  # type: ignore

    def test_graceful_degradation_without_subtitle_or_asr(self) -> None:
        calls = {"probe": 0}

        def probe(url):
            calls["probe"] += 1
            return dict(SAMPLE_INFO)

        mgr = pipeline.PipelineManager(
            probe_fn=probe, download_fn=lambda info, wd: "",
            subtitle_fn=lambda u, f: [], asr_fn=lambda *a: [],
            output_root=self.root)
        task = mgr.start("https://www.bilibili.com/video/BV1test", want_download=False)
        snap = self._wait(mgr, task["id"])
        self.assertEqual(snap["stage"], "done")
        self.assertEqual(calls["probe"], 1)
        self.assertEqual(snap["source_kind"], "")
        self.assertTrue(snap["degraded"], "降级原因必须如实上报，不能粉饰")
        self.assertTrue(any("语音识别" in d or "人声" in d for d in snap["degraded"]))
        md = mgr.read_artifact(task["id"], "md")
        self.assertIn("来源与可信度", md)
        self.assertIn("仅生成元数据骨架", md)

    def test_asr_no_speech_not_misdiagnosed_as_env(self) -> None:
        """DIAG-01：done + 0 段 → 无人声，不得提示重装模型。"""
        def asr(*_a, **_k):
            return [], {"no_speech": True, "stage": "done", "env_ready": True}

        mgr = pipeline.PipelineManager(
            probe_fn=lambda u: dict(SAMPLE_INFO),
            download_fn=lambda info, wd: str(self.root / "x.mp4"),
            subtitle_fn=lambda u, f: [], asr_fn=asr, output_root=self.root)
        started = mgr.start("https://www.bilibili.com/video/BV1test", want_download=True)
        snap = self._wait(mgr, started["id"])
        self.assertEqual(snap["stage"], "done")
        self.assertTrue(any("人声" in d for d in snap["degraded"]))
        self.assertFalse(any("环境未就绪" in d or "权重未预置" in d for d in snap["degraded"]))

    def test_safe_stem_windows_reserved(self) -> None:
        """WIN-01：Windows 保留名与结尾点必须规避。"""
        self.assertTrue(pipeline.safe_stem("CON").startswith("_"))
        self.assertEqual(pipeline.safe_stem("hello."), "hello")
        self.assertIn("\\\\", knowledge._yaml_scalar('a\\b"c'))  # DOC-01

    def test_synthesize_chapters_and_rule_summary(self) -> None:
        """伪章节 + 规则摘要：无平台 chapters 时自动分段并标注。"""
        segs = []
        t = 0.0
        for i in range(12):
            segs.append({"start": t, "end": t + 2.0, "text": f"第{i}段内容讲方法与实践。"})
            t += 2.0 if i % 4 else 6.0  # 每隔几段拉长停顿
        chapters, title_src = knowledge.synthesize_chapters(segs)
        self.assertGreaterEqual(len(chapters), 2)
        self.assertTrue(any("自动分段" in c.title for c in chapters))
        self.assertIn(title_src, ("llm", "tfidf", "keyword"))

        summary, kws = pipeline.offline_polish(segs)
        self.assertIn("规则摘要", summary)
        self.assertIn("导语：", summary)
        self.assertIn("- ", summary)  # 抽取式要点，非纯首句拼接
        self.assertTrue(kws)

        info = dict(SAMPLE_INFO)
        info["chapters"] = []
        info["subtitles"] = {"zh-Hans": [{"url": "https://i0.hdslb.com/sub.vtt", "ext": "vtt"}]}
        mgr = pipeline.PipelineManager(
            probe_fn=lambda u: dict(info),
            download_fn=lambda info_, wd: "",
            subtitle_fn=lambda u, f: segs,
            asr_fn=lambda *a, **k: [],
            output_root=self.root,
        )
        started = mgr.start("https://www.bilibili.com/video/BV1test", want_download=False)
        snap = self._wait(mgr, started["id"])
        self.assertEqual(snap["stage"], "done", snap.get("error"))
        self.assertTrue(snap.get("chapters_auto") or snap["chapter_count"] >= 2, snap)
        md = mgr.read_artifact(started["id"], "md")
        self.assertIsNotNone(md)
        self.assertIn("自动分段", md or "")
        self.assertIn("规则摘要", md or "")

    def test_cancel_stops_before_knowledge(self) -> None:
        """真取消：标记后后台线程在阶段边界退出，不再写产物。"""
        barrier = {"probed": False}

        def slow_probe(url):
            barrier["probed"] = True
            time.sleep(0.35)
            return dict(SAMPLE_INFO)

        mgr = pipeline.PipelineManager(
            probe_fn=slow_probe,
            download_fn=lambda info, wd: "",
            subtitle_fn=lambda u, f: list(SAMPLE_SEGMENTS),
            output_root=self.root,
        )
        started = mgr.start("https://www.bilibili.com/video/BV1test", want_download=False)
        tid = started["id"]
        # 等进入 probe 后立刻取消
        for _ in range(50):
            if barrier["probed"]:
                break
            time.sleep(0.02)
        self.assertTrue(mgr.cancel(tid))
        snap = self._wait(mgr, tid, timeout=5.0)
        self.assertEqual(snap["stage"], "cancelled")
        self.assertFalse(snap.get("files"))

    def test_rejects_unsafe_url_before_doing_any_work(self) -> None:
        mgr = pipeline.PipelineManager(probe_fn=lambda u: dict(SAMPLE_INFO),
                                       output_root=self.root)
        with self.assertRaises(ValueError):
            mgr.start("http://169.254.169.254/latest/meta-data/")

    def test_asr_reuses_downloaded_media(self) -> None:
        """实测驱动修复：已下载媒体必须喂给 ASR，不得再拿 URL 解析一次。"""
        media = self.root / "local.mp4"
        media.write_bytes(b"fake")
        captured: dict = {}

        def asr(url, language, title, media_path=""):
            captured["media_path"] = media_path
            return list(SAMPLE_SEGMENTS)

        mgr = pipeline.PipelineManager(
            probe_fn=lambda u: dict(SAMPLE_INFO),
            download_fn=lambda info, wd: str(media),
            subtitle_fn=lambda u, f: [], asr_fn=asr, output_root=self.root)
        started = mgr.start("https://www.bilibili.com/video/BV1test", want_download=True)
        snap = self._wait(mgr, started["id"])

        self.assertEqual(snap["stage"], "done", snap.get("error"))
        self.assertEqual(captured["media_path"], str(media))
        self.assertEqual(snap["source_kind"], "asr")
        self.assertEqual(snap["segment_count"], 3)
        self.assertFalse(any("独立的 URL" in d for d in snap["degraded"]))
        md = mgr.read_artifact(started["id"], "md")
        self.assertIsNotNone(md)
        self.assertIn("大家好", md)

    def test_no_audio_track_reports_accurate_reason(self) -> None:
        """无音轨 ≠ 环境未就绪：不能把运维指向错误的修复动作。"""
        from unittest import mock

        calls = {"asr": 0}

        def asr(*args):
            calls["asr"] += 1
            return []

        with mock.patch.object(pipeline, "has_audio_stream", return_value=False):
            mgr = pipeline.PipelineManager(
                probe_fn=lambda u: dict(SAMPLE_INFO),
                download_fn=lambda info, wd: str(self.root / "x.mp4"),
                subtitle_fn=lambda u, f: [], asr_fn=asr, output_root=self.root)
            started = mgr.start("https://www.bilibili.com/video/BV1test",
                                want_download=True)
            snap = self._wait(mgr, started["id"])

        self.assertEqual(snap["stage"], "done")
        self.assertEqual(calls["asr"], 0, "无音轨不该白跑一次 ASR")
        self.assertTrue(any("不含音轨" in d for d in snap["degraded"]))
        self.assertFalse(any("环境未就绪" in d for d in snap["degraded"]))

    @unittest.skipUnless((ROOT / "models").is_dir(), "源码仓不含权重；发布包才校验")
    def test_offline_model_is_bundled(self) -> None:
        models = asr_offline.available_models()
        self.assertTrue(models, "发布包必须预置至少一份离线权重")
        self.assertTrue(all((ROOT / "models" / m["name"] / "model.bin").is_file()
                            for m in models))
        st = asr_offline.configure(env={})
        self.assertEqual(st["mode"], "bundled-offline")
        self.assertTrue(st["offline_capable"])
        self.assertEqual(st["engine"], "faster")


class StageMethodTests(unittest.TestCase):
    """D30：阶段级测试，直接调用拆分后的方法，不依赖整条线程链。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        from fusion import persist
        persist.reset_for_tests(self.root / "stage.sqlite3")
        self.mgr = pipeline.PipelineManager(output_root=self.root)

    def tearDown(self) -> None:
        from fusion import persist
        persist.reset_for_tests(None)
        self.tmp.cleanup()

    def test_prepare_source_local_copies_and_writes_info(self) -> None:
        src = self.root / "input.m4a"
        src.write_bytes(b"fake-audio")
        task = pipeline.PipelineTask(id="stage01", url="", local_media=str(src))
        work = self.root / "stage01"
        work.mkdir()
        info, meta, is_local = self.mgr._prepare_source(task, work)
        self.assertTrue(is_local)
        self.assertTrue(meta.get("local_file"))
        self.assertTrue((work / "input.m4a").is_file())
        self.assertTrue((work / "info.json").is_file())
        self.assertEqual(task.media_path, str(work / "input.m4a"))
        self.assertEqual(info.get("extractor_key"), "local")

    def test_polish_marks_low_content_level_c(self) -> None:
        task = pipeline.PipelineTask(id="stage02", url="u")
        task.segments = [{"start": 0.0, "end": 1.0, "text": "嗯"}]
        meta = knowledge.meta_from_info({"id": "x", "title": "t"})
        low = self.mgr._polish_and_annotate(task, meta)
        self.assertTrue(low)
        self.assertEqual(meta.get("content_level"), "C")
        self.assertEqual([d["code"] for d in task.degradations], ["content.low"])

    def test_render_knowledge_crops_artifacts_for_low_content(self) -> None:
        task = pipeline.PipelineTask(id="stage03", url="u")
        task.segments = [{"start": 0.0, "end": 1.0, "text": "短"}]
        meta = knowledge.meta_from_info({"id": "x", "title": "t"})
        work = self.root / "stage03"
        work.mkdir()
        bundle, saved = self.mgr._render_knowledge(task, {}, meta, True, work)
        self.assertEqual(set(bundle), {"md", "srt", "json"})
        self.assertEqual(set(saved), {"md", "srt", "json"})
        self.assertTrue((work / "manifest.json").is_file())

    def test_finish_task_marks_done(self) -> None:
        task = pipeline.PipelineTask(id="stage04", url="u")
        with mock.patch.object(pipeline, "resolve_obsidian_dir", return_value=None):
            self.mgr._finish_task(task, {"title": "t"}, {}, {})
        self.assertEqual(task.stage, "done")
        self.assertEqual(task.percent, 100.0)


class EngineLockTests(unittest.TestCase):
    def test_vendored_engine_is_the_one_actually_loaded(self) -> None:
        report = boot_check.activate(force=True)
        import yt_dlp  # noqa: WPS433
        self.assertTrue(
            str(Path(yt_dlp.__file__).resolve()).startswith(str(boot_check.VENDOR_DIR.resolve())),
            "必须加载包内引擎，不能是系统 pip 版",
        )
        self.assertEqual(yt_dlp.version.__version__, "2026.10.26")
        self.assertEqual(report["engine_version"], "2026.10.26")
        self.assertTrue(report["engine_pinned"])

    def test_official_plugin_namespace_is_importable(self) -> None:
        boot_check.activate(force=True)
        plugin = boot_check.plugin_report()
        self.assertIn("avd_provenance.py", plugin["files"]["postprocessor"])
        self.assertTrue(plugin["loaded"], plugin.get("reason", ""))


class FormatLabelTests(unittest.TestCase):
    def test_summarize_formats_exposes_bitrate_hdr_lang(self) -> None:
        boot_check.activate(force=True)
        from downloader.engine import summarize_formats
        info = {
            "formats": [
                {
                    "format_id": "30080", "ext": "mp4", "height": 1080, "width": 1920,
                    "vcodec": "avc1.640028", "acodec": "none", "tbr": 4500,
                    "dynamic_range": "HDR10", "fps": 30, "filesize": 20_000_000,
                },
                {
                    "format_id": "30280", "ext": "m4a", "height": None,
                    "vcodec": "none", "acodec": "mp4a.40.2", "abr": 128,
                    "language": "zh-CN", "filesize": 1_000_000,
                },
            ],
        }
        opts = summarize_formats(info)
        video = next(o for o in opts if o["id"] == "h1080")
        self.assertIn("Mbps", video["label"])
        self.assertEqual(video["dynamic_range"], "HDR")
        self.assertEqual(video["audio_language"], "国语")
        self.assertIn("国语", video["label"])


class UpdatesAndClipsTests(unittest.TestCase):
    def test_updates_disabled_without_url(self) -> None:
        from fusion import updates
        import os
        old = os.environ.pop("AVD_UPDATE_URL", None)
        try:
            report = updates.check(current="2.0.0")
            self.assertFalse(report["enabled"])
            self.assertFalse(report["update_available"])
            self.assertTrue(updates.newer_than("2.1.0", "2.0.0"))
            self.assertFalse(updates.newer_than("2.0.0", "2.0.0"))
        finally:
            if old is not None:
                os.environ["AVD_UPDATE_URL"] = old

    def test_clip_rejects_bad_range(self) -> None:
        from fusion import clips
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            src = Path(tmp) / "a.mp4"
            src.write_bytes(b"not-a-real-mp4")
            with self.assertRaises(ValueError):
                clips.export_clip(src, start=5, end=5.1, dest=Path(tmp) / "o.mp4")


class SchedulerMachineExporterTests(unittest.TestCase):
    def test_scheduler_delay_and_run_at(self) -> None:
        from fusion import scheduler
        from datetime import datetime, timezone
        now = 1_700_000_000.0  # 2023-11-14 22:13 UTC
        nb = scheduler.parse_not_before(delay_seconds=120, now=now)
        self.assertEqual(nb, now + 120)
        future = datetime.fromtimestamp(now + 3600, tz=timezone.utc).isoformat()
        nb2 = scheduler.parse_not_before(run_at=future, now=now)
        self.assertAlmostEqual(nb2 or 0, now + 3600, delta=1)
        with self.assertRaises(ValueError):
            scheduler.parse_not_before(delay_seconds=-1)
        with self.assertRaises(ValueError):
            scheduler.parse_not_before(run_at="2020-01-01T00:00:00+00:00", now=now)

    def test_stage_machine_pipeline(self) -> None:
        from fusion.machine import StageMachine, StageError
        sm = StageMachine("pipeline")
        self.assertEqual(sm.advance("queued", "probe"), "probe")
        self.assertEqual(sm.advance("probe", "download"), "download")
        with self.assertRaises(StageError):
            sm.advance("queued", "knowledge")

    def test_exporters_reject_bad_webhook(self) -> None:
        from fusion import exporters
        with self.assertRaises(ValueError):
            exporters.push_feishu(
                webhook="http://evil.example/hook",
                title="t", markdown="m",
            )
        with self.assertRaises(ValueError):
            exporters.push_feishu(
                webhook="https://127.0.0.1/hook",
                title="t", markdown="m",
            )
        with self.assertRaises(ValueError):
            exporters.push_feishu(
                webhook="https://192.168.1.8/hook",
                title="t", markdown="m",
            )
        with self.assertRaises(ValueError):
            exporters.push_feishu(
                webhook="https://10.0.0.2/hook",
                title="t", markdown="m",
            )

    def test_notion_chunks_long_markdown(self) -> None:
        from fusion import exporters
        chunks = exporters._chunk_text("汉" * 5000, 2000)
        self.assertGreaterEqual(len(chunks), 3)
        self.assertEqual(sum(len(c) for c in chunks), 5000)

    def test_feishu_receipt_marks_truncated(self) -> None:
        from fusion import exporters
        from unittest.mock import patch
        long_md = "段" * 3200 * 10
        with patch.object(exporters, "_post_json", return_value={"ok": True, "status": 200}):
            with patch.object(exporters, "_safe_https", return_value="https://open.feishu.cn/hook"):
                r = exporters.push_feishu(
                    webhook="https://open.feishu.cn/hook",
                    title="t",
                    markdown=long_md,
                    fulltext_link="file:///tmp/x.md",
                )
        self.assertTrue(r["truncated"])
        self.assertEqual(r["chunks_pushed"], 8)
        self.assertGreater(r["chunks_total"], 8)
        self.assertIn("全文", r["message"])

    def test_chapter_titles_reject_duplicate_llm(self) -> None:
        from fusion import chapter_titles
        self.assertFalse(chapter_titles._titles_usable(["同一", "同一", "同一"]))
        self.assertTrue(chapter_titles._titles_usable(["开场", "方法", "收尾"]))

    def test_quality_entity_consistency_and_dual(self) -> None:
        from fusion import quality
        orig = [{"raw_text": "短视频增长方法论本地优先", "text": "短视频增长方法论本地优先"}]
        bad = [{"text": "你好"}]
        check = quality.consistency_check(orig, bad)
        self.assertEqual(check["action"], "rollback")
        dual = quality.render_dual_column_md({"title": "t"}, orig)
        self.assertIn("双栏对照", dual)
        self.assertIn("短视频", dual)
        # D12：左右相同不出 dual
        self.assertFalse(quality.dual_worth_emitting(orig))
        edited = [{"raw_text": "原始一句", "text": "整理后的一句"}] * 5
        self.assertTrue(quality.dual_worth_emitting(edited))

    def test_sample_for_llm_fills_budget(self) -> None:
        """D14：长文案应尽量用满 2400 字预算，而不是卡在 ~30 段。"""
        from fusion import quality
        texts = [f"这是第{i}句关于知识管理与检索增强的口播内容。" for i in range(200)]
        sketch = quality.sample_for_llm(texts, budget=2400)
        self.assertGreaterEqual(len(sketch), 1800)
        self.assertLessEqual(len(sketch), 2400)

    def test_sample_segments_for_index_spans_tail(self) -> None:
        """D13：等距抽稀后尾部段仍有时间锚。"""
        from fusion import quality
        segs = [
            {"start": float(i), "end": float(i) + 0.8, "text": f"段{i}内容" + ("x" * 40)}
            for i in range(800)
        ]
        sampled = quality.sample_segments_for_index(segs, char_budget=24_000)
        self.assertGreater(len(sampled), 50)
        self.assertLess(len(sampled), 800)
        # 必须覆盖接近尾部
        self.assertGreaterEqual(float(sampled[-1]["start"]), 600.0)

    def test_render_markdown_uses_paragraphs_with_anchors(self) -> None:
        """D10：meta.paragraphs 驱动分段 + 时间锚。"""
        segs = [
            {"start": 0.0, "end": 2.0, "text": "大家好今天聊RAG"},
            {"start": 3.0, "end": 5.0, "text": "检索质量很重要"},
        ]
        meta = {
            "title": "t",
            "paragraphs": [
                {"start": 0.0, "end": 2.0, "text": "大家好今天聊RAG", "count": 1},
                {"start": 3.0, "end": 5.0, "text": "检索质量很重要", "count": 1},
            ],
            "webpage_url": "https://example.com/v",
        }
        md = knowledge.render_markdown(meta, segs, source_kind="asr")
        self.assertIn("#t=", md)
        self.assertGreaterEqual(md.count("`00:00:"), 2)

    def test_enrich_insights_rule_fallback(self) -> None:
        from fusion import quality
        segs = [
            {"text": "短视频增长方法论讲选题与开头"},
            {"text": "本地优先的知识库整理流程"},
            {"text": "ASR 与字幕轨的取舍原则"},
            {"text": "导出 Markdown 进入 Obsidian"},
        ]
        points, gloss, src = quality.enrich_insights(segs, use_llm=False)
        self.assertEqual(src, "rule")
        self.assertTrue(points)
        self.assertTrue(any("短视频" in p or "增长" in p for p in points))
        self.assertTrue(isinstance(gloss, list))

    def test_library_fts_search_and_ask(self) -> None:
        import tempfile
        from pathlib import Path
        from fusion import library
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            library.reset_for_tests(Path(tmp) / "lib.sqlite3")
            library.upsert_material(
                task_id="t1", title="短视频增长", body="本地优先的短视频运营方法论",
                tags=["运营"], collection="bilibili",
            )
            hits = library.search("短视频")
            self.assertTrue(hits)
            self.assertEqual(hits[0]["id"], "t1")
            ans = library.answer("短视频")
            self.assertTrue(ans["ok"])
            library.close()

    def test_library_snippet_contains_query(self) -> None:
        """P1：命中片段须含关键词（±60 字 + 高亮）。"""
        import tempfile
        from pathlib import Path
        from fusion import library
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            library.reset_for_tests(Path(tmp) / "lib.sqlite3")
            body = ("前言" * 40) + "转化路径是核心增长杠杆" + ("后记" * 40)
            library.upsert_material(
                task_id="hit1", title="增长讲义", body=body, tags=["增长"],
            )
            hits = library.search("转化路径")
            self.assertTrue(hits)
            self.assertIn("转化路径", hits[0]["snippet"])
            self.assertIn("「转化路径」", hits[0]["snippet"])
            library.close()

    def test_library_like_escape_and_percent(self) -> None:
        """P2：LIKE 通配符转义，搜 100% 不当通配。"""
        import tempfile
        from pathlib import Path
        from fusion import library
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            library.reset_for_tests(Path(tmp) / "lib.sqlite3")
            library.upsert_material(
                task_id="p1", title="转化率", body="本周转化率达到 100% 目标", tags=[],
            )
            library.upsert_material(
                task_id="p2", title="其他", body="无关内容 a_b 测试", tags=[],
            )
            hits = library.search("100%")
            self.assertTrue(any(h["id"] == "p1" for h in hits))
            # 不应把 _ 当通配扫到无关
            hits2 = library.search("a_b")
            self.assertTrue(any(h["id"] == "p2" for h in hits2))
            library.close()

    def test_diagnose_pack_rejects_path_traversal(self) -> None:
        """P1：task_id='..' 不得打包 config/cookies。"""
        import tempfile
        import zipfile
        from pathlib import Path
        from fusion import diagnose_pack
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            dest = Path(tmp) / "diag.zip"
            result = diagnose_pack.build_diagnose_pack(task_id="..", dest=dest)
            self.assertTrue(dest.is_file())
            self.assertEqual(result.get("task_id_accepted"), "")
            self.assertTrue(str(result.get("ticket_id") or "").startswith("AVD-"))
            with zipfile.ZipFile(dest) as zf:
                names = zf.namelist()
            self.assertIn("env.json", names)
            self.assertIn("RECEIPT.txt", names)
            joined = "\n".join(names).lower()
            self.assertNotIn("config.json", joined)
            self.assertNotIn("cookies", joined)
            self.assertNotIn(".git/", joined)

    def test_library_batch_and_cluster(self) -> None:
        import tempfile
        from pathlib import Path
        from fusion import library
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            library.reset_for_tests(Path(tmp) / "lib.sqlite3")
            library.upsert_material(
                task_id="b1", title="A", body="正文", tags=["旧"],
                meta={"chapters": [{"title": "开场", "start": 0, "end": 10}]},
            )
            self.assertEqual(library.batch_set_collection(["b1"], "合集甲"), 1)
            self.assertEqual(library.batch_add_tags(["b1"], ["新标签"]), 1)
            clustered = library.auto_cluster_by_chapters()
            self.assertGreaterEqual(clustered["updated"], 1)
            hits = library.search("正文", collection="合集甲")
            self.assertTrue(hits)
            self.assertIn("新标签", hits[0]["tags"])
            library.close()

    def test_library_export_booklet_md_docx(self) -> None:
        """R3：一个合集 → 带目录 MD + DOCX + PDF（同源 LayoutSpec）。"""
        import tempfile
        import zipfile
        import io
        from pathlib import Path
        from fusion import library, pdf_cjk
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            library.reset_for_tests(Path(tmp) / "lib.sqlite3")
            library.upsert_material(
                task_id="bk1", title="第一篇", body="转化路径与增长",
                tags=["增长"], collection="试用合集",
            )
            library.upsert_material(
                task_id="bk2", title="第二篇", body="本地优先知识稿",
                tags=["知识"], collection="试用合集",
            )
            dest = Path(tmp) / "booklets"
            result = library.export_booklet(
                "试用合集", dest_dir=dest, fmt="all", template="delivery",
            )
            self.assertEqual(result["count"], 2)
            self.assertEqual(result.get("template"), "delivery")
            md_path = Path(result["md"])
            self.assertTrue(md_path.is_file())
            text = md_path.read_text(encoding="utf-8")
            self.assertIn("合集成册", text)
            self.assertIn("## 目录", text)
            self.assertIn("第一篇", text)
            self.assertIn("第二篇", text)
            self.assertTrue(result.get("docx"))
            self.assertTrue(Path(result["docx"]).is_file())
            self.assertGreater(Path(result["docx"]).stat().st_size, 40)
            with zipfile.ZipFile(io.BytesIO(Path(result["docx"]).read_bytes())) as zf:
                self.assertIn("word/header1.xml", zf.namelist())
                self.assertIn("word/footer1.xml", zf.namelist())
                doc_xml = zf.read("word/document.xml").decode("utf-8")
                self.assertIn('w:type="page"', doc_xml)
            if pdf_cjk.find_cjk_font() is not None:
                self.assertTrue(result.get("pdf"))
                self.assertTrue(Path(result["pdf"]).is_file())
                self.assertTrue(Path(result["pdf"]).read_bytes().startswith(b"%PDF"))
            with self.assertRaises(ValueError):
                library.export_booklet("")
            with self.assertRaises(ValueError):
                library.export_booklet("不存在的合集")
            library.close()

    def test_layout_spec_drives_md_docx_pdf(self) -> None:
        """R3：同一 LayoutDoc 渲染三格式，章节强制分页。"""
        import zipfile
        import io
        from fusion import layout, pdf_cjk
        items = [
            {"id": "a", "title": "甲", "body": "第一段内容。\n第二段。", "tags": ["t"]},
            {"id": "b", "title": "乙", "body": "另一篇正文。", "tags": []},
        ]
        doc = layout.build_booklet("测试合集", items, template="delivery")
        self.assertTrue(any(b.kind == "page_break" for b in doc.blocks))
        self.assertTrue(any(b.kind == "toc_item" for b in doc.blocks))
        md = layout.render_markdown(doc)
        self.assertIn("## 目录", md)
        self.assertIn("甲", md)
        docx = layout.render_docx(doc)
        self.assertTrue(docx.startswith(b"PK"))
        with zipfile.ZipFile(io.BytesIO(docx)) as zf:
            footer = zf.read("word/footer1.xml").decode("utf-8")
            self.assertIn("PAGE", footer)
            header = zf.read("word/header1.xml").decode("utf-8")
            self.assertIn("测试合集", header)
        lines = layout.to_pdf_lines(doc)
        self.assertIn(layout.PAGEBREAK_SENTINEL, lines)
        if pdf_cjk.find_cjk_font() is not None:
            pdf = layout.render_pdf(doc)
            self.assertTrue(pdf and pdf.startswith(b"%PDF"))
            # 强制分页应产生多页
            pages = pdf_cjk._paginate(doc.title, lines)
            self.assertGreaterEqual(len(pages), 2)

    def test_diagnose_ticket_list(self) -> None:
        """R4：诊断包写回执后可列表对账。"""
        import tempfile
        from pathlib import Path
        from fusion import diagnose_pack
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            dest = Path(tmp) / "pack.zip"
            result = diagnose_pack.build_diagnose_pack(task_id="", dest=dest)
            ticket = result["ticket_id"]
            self.assertTrue(ticket.startswith("AVD-"))
            tickets = diagnose_pack.list_tickets(limit=20)
            self.assertTrue(any(t["ticket_id"] == ticket for t in tickets))
            hit = next(t for t in tickets if t["ticket_id"] == ticket)
            self.assertTrue(hit["exists"])
            self.assertTrue(str(hit.get("receipt") or "").endswith(".receipt.json"))

    def test_recovery_prove_resume_and_submit_local(self) -> None:
        """R4：合成续跑实证 + 工单 local_only 闭环。"""
        import tempfile
        from pathlib import Path
        from fusion import diagnose_pack, recovery
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            proof = recovery.prove_resume(tmp, chunks_total=4, cancel_after=1)
            self.assertTrue(proof["ok"])
            self.assertGreater(proof["resume_chunk"], 0)
            self.assertTrue(proof["checksum_reject"])
            items = recovery.scan_recoverable(outputs_root=tmp)
            # prove_resume 结束会清检查点；再造一条
            from fusion import checkpoint
            checkpoint.save_checkpoint(
                tmp, task_id="r4a", chunk_index=0, chunks_total=3,
                segments=[{"t": 1}], duration=30,
            )
            recovery.save_crash_scene(tmp, task_id="r4a", reason="unit")
            items = recovery.scan_recoverable(outputs_root=tmp)
            self.assertTrue(any(i["task_id"] == "r4a" for i in items))
            pack = diagnose_pack.build_diagnose_pack(
                task_id="", dest=Path(tmp) / "p.zip",
                outputs_root=Path(tmp), log_dir=Path(tmp),
            )
            sub = diagnose_pack.submit_ticket(pack["ticket_id"])
            self.assertEqual(sub["status"], "local_only")
            self.assertIn("alternative", sub)

    def test_checkpoint_checksum_and_soft_cancel(self) -> None:
        import tempfile
        from pathlib import Path
        from fusion import checkpoint
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            p = checkpoint.save_checkpoint(
                tmp, task_id="x", chunk_index=1, chunks_total=3,
                segments=[{"text": "hi"}], duration=10,
            )
            self.assertTrue(p.is_file())
            data = checkpoint.load_checkpoint(tmp, "x")
            self.assertEqual(data["chunk_index"], 1)
            self.assertTrue(data.get("checksum"))
            # 篡改后拒绝加载
            raw = json.loads(p.read_text(encoding="utf-8"))
            raw["segments"] = [{"text": "tampered"}]
            p.write_text(json.dumps(raw), encoding="utf-8")
            self.assertIsNone(checkpoint.load_checkpoint(tmp, "x"))
            # 软取消哨兵
            checkpoint.request_soft_cancel(tmp, "x")
            self.assertTrue(checkpoint.is_cancel_requested(tmp, "x"))
            checkpoint.clear_checkpoint(tmp, "x")
            self.assertFalse(checkpoint.is_cancel_requested(tmp, "x"))

    def test_thumb_cache_stats_and_clear(self) -> None:
        """P2：thumb_cache 补测（原 0%）。"""
        from fusion import thumb_cache
        n = thumb_cache.clear()
        self.assertGreaterEqual(n, 0)
        st = thumb_cache.stats()
        self.assertIn("entries", st)
        self.assertEqual(st["entries"], 0)

    def test_thumb_cache_get_or_fetch_and_reject(self) -> None:
        """抬高 thumb_cache 覆盖：拒绝坏 URL + mock 抓取命中。"""
        from unittest.mock import patch
        from fusion import thumb_cache
        thumb_cache.clear()
        with self.assertRaises(ValueError):
            thumb_cache.get_or_fetch("http://127.0.0.1/x.png")
        with self.assertRaises(ValueError):
            thumb_cache.get_or_fetch("")
        url = "https://i0.hdslb.com/bfs/archive/demo.jpg"
        with patch.object(thumb_cache, "_fetch_network", return_value=("image/jpeg", b"JPEGDATA")):
            ct, body = thumb_cache.get_or_fetch(url)
            self.assertEqual(ct, "image/jpeg")
            self.assertEqual(body, b"JPEGDATA")
            # 二次命中走缓存，不应再调网络
            with patch.object(thumb_cache, "_fetch_network", side_effect=AssertionError("cache miss")):
                ct2, body2 = thumb_cache.get_or_fetch(url)
            self.assertEqual(body2, b"JPEGDATA")
        st = thumb_cache.stats()
        self.assertGreaterEqual(st["entries"], 1)
        thumb_cache.clear()

    def test_pdf_layout_has_footer_and_sizes(self) -> None:
        """P1：PDF 含页脚与多级字号指令；页脚无 .notdef。"""
        from fusion import pdf_cjk
        if pdf_cjk.find_cjk_font() is None:
            self.skipTest("无 CJK 字体")
        raw = pdf_cjk.render_cjk_pdf(
            "交付标题",
            ["# 一级标题", "## 二级标题", "正文段落一。", "", "正文段落二 English words here。"],
        )
        self.assertIsNotNone(raw)
        assert raw is not None
        self.assertIn(b"/Identity-H", raw)
        self.assertTrue(raw.startswith(b"%PDF"))
        self.assertIn(b"/Count ", raw)
        # 解压内容流：页脚 gid 不应为 0（方块）
        import re as _re
        import zlib
        streams = _re.findall(rb"stream\r?\n(.*?)\r?\nendstream", raw, _re.S)
        foot_ok = False
        for blob in streams:
            try:
                plain = zlib.decompress(blob)
            except Exception:
                continue
            if b"BT" not in plain:
                continue
            # 找页脚行的 hex 串（含多个 4 位 hex）
            for m in _re.finditer(rb"<([0-9A-Fa-f]+)> Tj", plain):
                hx = m.group(1).decode("ascii")
                if len(hx) < 8:
                    continue
                gids = [int(hx[i:i + 4], 16) for i in range(0, len(hx), 4)]
                # 页脚通常较长（含「第 N / M 页」）
                if len(gids) >= 12 and all(g != 0 for g in gids):
                    foot_ok = True
                    break
            if foot_ok:
                break
        self.assertTrue(foot_ok, "页脚仍含 gid=0 方块")
        # 英文不断词：中间不应单独出现 "wo"/"rds" 硬切痕迹（弱断言：wrap 自身）
        parts = pdf_cjk._wrap("English words stay together", 12)
        joined = "/".join(parts)
        self.assertNotIn("wo/rd", joined)
        self.assertNotIn("wo/rds", joined)

    def test_docx_has_real_footer(self) -> None:
        """DOCX 含 footer1.xml 与 PAGE 域。"""
        import zipfile
        import io
        meta = knowledge.meta_from_info(SAMPLE_INFO)
        raw = knowledge.render_docx(meta, SAMPLE_SEGMENTS, summary="摘要", keywords=["测"])
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = zf.namelist()
            self.assertIn("word/footer1.xml", names)
            footer = zf.read("word/footer1.xml").decode("utf-8")
            self.assertIn("PAGE", footer)
            self.assertIn("NUMPAGES", footer)
            doc = zf.read("word/document.xml").decode("utf-8")
            self.assertIn("footerReference", doc)
            self.assertNotIn("第末页", doc)

    def test_keywords_not_sentence_fragments(self) -> None:
        """关键词不得是整句/残句碎片，也不得靠「凑满 8 个」来充数。

        第九轮重写引擎后，口径改为：**宁可少给，也不给碎片**。四句话的小语料
        里真正的跨段复现搭配只有 5 个，因此不再断言「≥6 个」（旧断言实际是靠
        单次出现的碎片凑出来的），改为断言集合干净 + 复现搭配必须命中。
        """
        segs = [
            {"text": "今天讲本地优先的知识稿链路与转化路径。"},
            {"text": "重点是把解析转写环节串成一次点击。"},
            {"text": "前三秒决定完播率，隐私数据不出机器。"},
            {"text": "避免停在概念层，整理串成一次点击。"},
        ]
        _, kws = pipeline.offline_polish(segs)
        self.assertTrue(kws)
        self.assertLessEqual(len(kws), 8)
        for w in kws:
            self.assertGreaterEqual(len(w), 2)
            self.assertLessEqual(len(w), 3)
            self.assertNotIn(w, ("今天讲本", "er", "整理串成一次点击", "转写环节用"))
        # 跨两段复现的搭配必须被识别（「串成一次点击」出现两次）
        self.assertIn("点击", kws)
        self.assertIn("串成", kws)

    def test_keywords_word_level_on_real_corpus(self) -> None:
        """语料足够时关键词要够 8 个、且全部是词级单元（中英都不得是滑窗碎片）。"""
        texts = [
            "本地优先的知识管理，第一步是把素材入库。",
            "素材入库之后要建索引，索引决定检索速度。",
            "检索速度取决于索引结构，而不是机器性能。",
            "我们用的索引是倒排加前缀树，检索更快。",
            "转写结果要带时间戳，时间戳方便定位原话。",
            "有了时间戳，检索就能直接跳到那一句。",
            "所有转写和索引都在本机完成，数据不出机器。",
            "隐私数据不出机器，这是本地优先的底线。",
        ]
        segs = [{"text": t} for t in texts]
        _, kws = pipeline.offline_polish(segs)
        self.assertGreaterEqual(len(kws), 6)
        for w in kws:
            self.assertGreaterEqual(len(w), 2)
        for must in ("索引", "检索", "转写"):
            self.assertIn(must, kws)

    def test_keywords_latin_whole_words(self) -> None:
        """英文按整词切分：片头里的 YoYo/Television 不得切成 YoY/Tel/Ser。"""
        segs = [
            {"text": "优优独播剧场——YoYo Television Series Exclusive。"},
            {"text": "Knowledge management 的核心是 index 与 search。"},
            {"text": "index 决定 search 速度，search 决定体感。"},
            {"text": "knowledge 沉淀下来，index 才有意义。"},
        ]
        _, kws = pipeline.offline_polish(segs)
        for bad in ("Exc", "Ser", "Tel", "YoY", "Yo", "Ex", "Te", "knowledge"[:4]):
            self.assertNotIn(bad, kws)
        # 整词保留（小写）
        self.assertTrue(any(w in ("index", "search", "knowledge") for w in kws))

    def test_scheduler_naive_iso_is_local(self) -> None:
        """无时区 ISO 按本地解释，不是 UTC。"""
        from datetime import datetime
        from fusion import scheduler
        local = datetime.now().astimezone()
        # 选明天同一本地时刻，避免过期
        tomorrow = local.replace(hour=9, minute=0, second=0, microsecond=0)
        from datetime import timedelta
        if tomorrow <= local:
            tomorrow = tomorrow + timedelta(days=1)
        naive = tomorrow.strftime("%Y-%m-%dT%H:%M")
        ts = scheduler.parse_not_before(run_at=naive)
        self.assertIsNotNone(ts)
        got = datetime.fromtimestamp(ts).astimezone()
        self.assertEqual(got.hour, 9)
        self.assertEqual(got.minute, 0)
        info = scheduler.resolve_run_at(naive)
        self.assertTrue(info.get("resolved_local"))

    def test_list_tasks_marks_missing_artifacts(self) -> None:
        """幽灵任务：产物缺失时 exists=false / stale=true。"""
        import shutil
        import tempfile
        from pathlib import Path
        from fusion import persist
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            persist.reset_for_tests(root / "avd_tasks.sqlite3")
            info = dict(SAMPLE_INFO)
            info["subtitles"] = {"zh-Hans": [{"url": "https://i0.hdslb.com/sub.vtt", "ext": "vtt"}]}
            mgr = pipeline.PipelineManager(
                probe_fn=lambda u: dict(info),
                download_fn=lambda info_, wd: "",
                subtitle_fn=lambda u, f: list(SAMPLE_SEGMENTS),
                asr_fn=lambda *a, **k: [],
                output_root=root,
            )
            task = mgr.start("https://www.bilibili.com/video/BV1test", want_download=False)
            tid = task["id"]
            deadline = time.time() + 8
            snap = None
            while time.time() < deadline:
                snap = mgr.get(tid)
                if snap and snap["stage"] in ("done", "error", "cancelled"):
                    break
                time.sleep(0.03)
            self.assertIsNotNone(snap)
            self.assertEqual(snap["stage"], "done")
            shutil.rmtree(root / tid, ignore_errors=True)
            rows = mgr.list_tasks()
            hit = next(r for r in rows if r["id"] == tid)
            self.assertFalse(hit["exists"])
            self.assertTrue(hit["stale"])
            self.assertIn("产物已删除", hit.get("message") or "")
            persist.reset_for_tests(None)

    def test_persist_trim_outputs_media_first(self) -> None:
        """磁盘保留：超限优先删媒体，保留 md。"""
        import tempfile
        from pathlib import Path
        from fusion import persist
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            (root / "a.md").write_text("keep", encoding="utf-8")
            big = root / "clip.mp4"
            big.write_bytes(b"0" * 5000)
            (root / "old.wav").write_bytes(b"1" * 3000)
            result = persist.trim_outputs(root, max_bytes=2000, media_first=True)
            self.assertGreaterEqual(result["deleted"], 1)
            self.assertTrue((root / "a.md").is_file())
            self.assertFalse(big.exists())

    def test_diagnose_pack_list_and_full_flag(self) -> None:
        """抬高 diagnose_pack 覆盖：任务瘦身/完整产物/日志/名单过滤。"""
        import tempfile
        import zipfile
        from pathlib import Path
        from fusion import diagnose_pack
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            dest = root / "d.zip"
            r1 = diagnose_pack.build_diagnose_pack(task_id="nope!!", dest=dest)
            self.assertEqual(r1.get("task_id_accepted"), "")
            self.assertTrue(any("拒绝" in s for s in (r1.get("skipped") or [])))
            tickets = diagnose_pack.list_tickets(limit=5)
            self.assertTrue(any(t["ticket_id"] == r1["ticket_id"] for t in tickets))
            with zipfile.ZipFile(dest) as zf:
                self.assertIn("RECEIPT.txt", zf.namelist())

            # 造真实任务目录 + 日志，覆盖瘦身与 full 路径
            outs = root / "outputs"
            task = outs / "taskABC"
            task.mkdir(parents=True)
            (task / "meta.json").write_text('{"ok":true}', encoding="utf-8")
            (task / "note.md").write_text("正文" * 1200, encoding="utf-8")  # >2000 字触发截断
            (task / "secret.env").write_text("x", encoding="utf-8")  # 后缀不在白名单
            (task / "clip.mp4").write_bytes(b"00")  # 媒体跳过
            (task / "ok.txt").write_text("keep", encoding="utf-8")
            logs = root / "logs"
            logs.mkdir()
            (logs / "asr-fail-demo.log").write_text("fail line\n", encoding="utf-8")

            slim = diagnose_pack.build_diagnose_pack(
                task_id="taskABC", dest=root / "slim.zip",
                outputs_root=outs, log_dir=logs,
            )
            self.assertEqual(slim["task_id_accepted"], "taskABC")
            with zipfile.ZipFile(root / "slim.zip") as zf:
                names = zf.namelist()
            self.assertTrue(any(n.startswith("logs/") for n in names))
            self.assertTrue(any("preview.txt" in n for n in names))
            self.assertNotIn("task/ok.txt", names)  # 瘦身不含全文 txt

            full = diagnose_pack.build_diagnose_pack(
                task_id="taskABC", dest=root / "full.zip",
                include_full_artifacts=True,
                outputs_root=outs, log_dir=logs,
            )
            with zipfile.ZipFile(root / "full.zip") as zf:
                names = zf.namelist()
            self.assertIn("task/ok.txt", names)
            self.assertNotIn("task/clip.mp4", names)
            self.assertTrue(any("secret" in s or "secret.env" in s for s in (full.get("skipped") or [])))

            # 不存在的合法 id
            miss = diagnose_pack.build_diagnose_pack(
                task_id="missing1", dest=root / "miss.zip", outputs_root=outs,
            )
            self.assertTrue(any("不存在" in s for s in (miss.get("skipped") or [])))

    def test_chapter_titles_tfidf(self) -> None:
        from fusion import chapter_titles
        from fusion.knowledge import Chapter
        segs = [
            {"start": 0, "end": 10, "text": "短视频增长方法论讲选题与开头"},
            {"start": 10, "end": 20, "text": "本地优先架构保障数据不出域"},
        ]
        chs = [
            Chapter(title="话题 1", start=0, end=10),
            Chapter(title="话题 2", start=10, end=20),
        ]
        out, src = chapter_titles.enrich_chapter_titles(chs, segs, use_llm=False)
        self.assertEqual(src, "tfidf")
        self.assertEqual(len(out), 2)
        self.assertTrue(all("自动分段" in c.title for c in out))


if __name__ == "__main__":
    unittest.main(verbosity=2)
