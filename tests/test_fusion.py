# Copyright (c) 2026 Hyht-Team. All rights reserved.
# See LICENSE (proprietary) and NOTICE.md (third-party components).
"""旗舰版融合层回归测试（unittest，零第三方依赖、不联网）。

覆盖：合规边界、结构化知识稿、一次解析全链复用、引擎版本闸门、官方插件扩展点。
运行： python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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


class GuardsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig = guards._resolved_ips
        guards._resolved_ips = lambda host: []  # 单测不碰 DNS
        self.tmp = tempfile.TemporaryDirectory()
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
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        guards._resolved_ips = self._orig
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
            return list(SAMPLE_SEGMENTS)

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
        self.assertEqual(set(snap["files"]), {"md", "srt", "txt", "json"})
        md = mgr.read_artifact(task["id"], "md")
        self.assertIsNotNone(md)
        self.assertIn("## 开场与背景", md)
        self.assertTrue((self.root / task["id"] / "manifest.json").is_file())

    def test_graceful_degradation_without_subtitle_or_asr(self) -> None:
        calls = {"probe": 0}

        def probe(url):
            calls["probe"] += 1
            return dict(SAMPLE_INFO)

        mgr = pipeline.PipelineManager(
            probe_fn=probe, subtitle_fn=lambda u, f: [], asr_fn=lambda *a: [],
            output_root=self.root)
        task = mgr.start("https://www.bilibili.com/video/BV1test")
        snap = self._wait(mgr, task["id"])
        self.assertEqual(snap["stage"], "done")
        self.assertEqual(calls["probe"], 1)
        self.assertEqual(snap["source_kind"], "")
        self.assertTrue(snap["degraded"], "降级原因必须如实上报，不能粉饰")
        self.assertTrue(any("语音识别" in d for d in snap["degraded"]))
        md = mgr.read_artifact(task["id"], "md")
        self.assertIn("来源与可信度", md)
        self.assertIn("仅生成元数据骨架", md)

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

    def test_offline_model_is_bundled(self) -> None:
        models = asr_offline.available_models()
        self.assertTrue(models, "发布包必须预置至少一份离线权重")
        self.assertTrue(all((ROOT / "models" / m["name"] / "model.bin").is_file()
                            for m in models))
        st = asr_offline.configure(env={})
        self.assertEqual(st["mode"], "bundled-offline")
        self.assertTrue(st["offline_capable"])
        self.assertEqual(st["engine"], "faster")


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
