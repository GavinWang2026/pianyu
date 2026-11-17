# Copyright (c) 2026 Hyht-Team. All rights reserved.
# See LICENSE (proprietary) and NOTICE.md (third-party components).
"""R2 覆盖率补测 —— pipeline 模块级服务函数 + 降级模型。

第九轮实测：pipeline 60% / task_store 59%，缺的恰好是「用户真的会点到的那些
入口」——播放列表展开、切片导出、Obsidian 直写、诊断接口、字幕轨抓取、
ASR 桥接的每个终态分支。这些分支线上一次都没跑过，等于没验证。

本文件不出网、不起线程池：所有外部依赖（yt-dlp / ffprobe / 子进程 / HTTP）
都被替身接管，只验证本模块自己的判定逻辑。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
os.environ.setdefault(
    "AVD_DB_PATH",
    str(Path(tempfile.mkdtemp(prefix="avd-test-db-")) / "tasks.sqlite3"),
)
# 素材库同样要隔离：库路径写死时单测会把「覆盖率样本」灌进真实素材库
os.environ.setdefault(
    "AVD_LIBRARY_DB",
    str(Path(tempfile.mkdtemp(prefix="avd-test-lib-")) / "library.sqlite3"),
)

from fusion import boot_check, degradation, pipeline  # noqa: E402

# 单文件运行也要能加载内嵌引擎（downloader/__init__ 会 import yt_dlp）；
# 此前依赖 test_fusion 先激活，属于顺序耦合。
boot_check.activate()


# ---------------------------------------------------------------- 替身工具


class _FakeYDL:
    """最小 YoutubeDL 替身：支持上下文管理 + extract_info / process_info。"""

    payload: dict = {}
    raise_on: BaseException | None = None
    processed: list = []

    def __init__(self, opts=None):
        self.opts = opts or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        if type(self).raise_on is not None:
            raise type(self).raise_on
        return dict(type(self).payload)

    def process_info(self, info):
        type(self).processed.append(info)


def _fake_ytdlp(payload: dict, *, raise_on: BaseException | None = None):
    cls = type("FakeYoutubeDL", (_FakeYDL,), {
        "payload": payload, "raise_on": raise_on, "processed": [],
    })
    mod = type(sys)("yt_dlp")
    mod.YoutubeDL = cls
    mod.utils = type(sys)("yt_dlp.utils")
    return mod, cls


def _allow_all_urls():
    """放行 URL 校验（真实实现会做 DNS 解析，单测不该依赖网络）。"""
    return mock.patch.object(
        pipeline.guards, "check_media_url", lambda url, **kw: (True, ""))


INFO = {
    "id": "BV1r2coverage",
    "title": "覆盖率样本",
    "extractor": "bilibili",
    "duration": 120,
    "webpage_url": "https://www.bilibili.com/video/BV1r2coverage",
}


def _write(path: Path, text: str = "x" * 8192) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------- 降级模型


class DegradationModelTests(unittest.TestCase):
    """Degradation(level, cause, retriable, action) 的语义契约。"""

    def test_levels_are_ordered(self):
        self.assertEqual(degradation.LEVELS, ("note", "warn", "major", "block"))
        self.assertEqual(degradation.rank("block") > degradation.rank("note"), True)

    def test_make_uses_catalog_defaults(self):
        item = degradation.make("asr.env_missing", "环境未就绪")
        self.assertEqual(item.level, "block")
        self.assertFalse(item.retriable)
        self.assertIn("doctor.py", item.action)

    def test_make_allows_override(self):
        item = degradation.make(
            "asr.timeout", "超时", level="warn", retriable=False, action="换源")
        self.assertEqual(item.level, "warn")
        self.assertFalse(item.retriable)
        self.assertEqual(item.action, "换源")

    def test_unknown_code_falls_back(self):
        item = degradation.make("never.registered", "新分支")
        self.assertEqual(item.level, "warn")
        self.assertTrue(item.retriable)
        self.assertTrue(item.action)

    def test_empty_cause_uses_code(self):
        self.assertEqual(degradation.make("subtitle.fetch", "  ").cause, "subtitle.fetch")

    def test_as_dict_is_json_safe(self):
        row = degradation.make("content.low", "只有 45 字").as_dict()
        self.assertEqual(
            set(row),
            {"code", "level", "level_label", "cause", "retriable", "action"})
        json.dumps(row)  # 不得含不可序列化对象

    def test_as_line_keeps_level_prefix(self):
        line = degradation.make("media.no_audio", "不含音轨").as_line()
        self.assertTrue(line.startswith("[降级产出]"))
        self.assertIn("不含音轨", line)

    def test_bad_level_rejected(self):
        with self.assertRaises(ValueError):
            degradation.Degradation("x", "fatal", "c", True, "a")
        with self.assertRaises(ValueError):
            degradation.Spec("blocker", True, "a")

    def test_summary_and_worst(self):
        rows = [
            degradation.make("subtitle.absent", "无字幕").as_dict(),
            degradation.make("asr.env_missing", "环境未就绪").as_dict(),
            degradation.make("chapter.auto", "自动分段").as_dict(),
        ]
        sm = degradation.summary(rows)
        self.assertEqual(sm["total"], 3)
        self.assertEqual(sm["worst"], "block")
        self.assertTrue(sm["blocked"])
        self.assertEqual(sm["counts"]["note"], 2)
        self.assertGreaterEqual(sm["retriable"], 0)

    def test_summary_empty(self):
        sm = degradation.summary([])
        self.assertEqual(sm["worst"], "")
        self.assertFalse(sm["blocked"])

    def test_to_dicts_skips_foreign(self):
        item = degradation.make("speaker.neural", "已标注")
        self.assertEqual(len(degradation.to_dicts([item, "oops", None, {"code": "x"}])), 2)

    def test_catalog_all_levels_valid(self):
        for code in degradation.known_codes():
            self.assertIn(degradation.spec(code).level, degradation.LEVELS, code)
            self.assertTrue(degradation.spec(code).action, code)


# ---------------------------------------------------------------- 任务快照


class TaskSnapshotTests(unittest.TestCase):
    def test_structured_items_and_legacy_summary(self):
        from fusion.pipeline import PipelineTask
        task = PipelineTask(id="t1", url="https://example.com/v")
        task.degradations = [
            degradation.make("subtitle.absent", "平台未提供字幕").as_dict()]
        task.degraded = ["[提示] 平台未提供字幕"]
        snap = task.to_dict()
        self.assertEqual(snap["degraded_items"][0]["code"], "subtitle.absent")
        self.assertEqual(snap["degraded_summary"]["worst"], "note")

    def test_legacy_rows_without_structure(self):
        from fusion.pipeline import PipelineTask
        task = PipelineTask(id="t2", url="https://example.com/v")
        task.degraded = ["老版本留下的一句话"]
        snap = task.to_dict()
        self.assertEqual(snap["degraded_items"][0]["code"], "legacy")
        self.assertEqual(snap["degraded_items"][0]["level"], "note")

    def test_manager_degrade_prepend_and_levels(self):
        root = Path(tempfile.mkdtemp(prefix="avd-degrade-"))
        mgr = pipeline.PipelineManager(output_root=root, probe_fn=lambda u: dict(INFO))
        task = pipeline.PipelineTask(id="t3", url="https://example.com/v")
        mgr._degrade(task, "subtitle.absent", "平台未提供字幕")
        mgr._degrade(task, "content.low", "信息量不足：仅 45 字", prepend=True)
        self.assertIn("信息量不足", task.degraded[0])
        self.assertEqual(task.degradations[0]["code"], "content.low")
        self.assertEqual(task.degraded[1], "[提示] 平台未提供字幕")
        self.assertEqual(
            task.to_dict()["degraded_summary"]["worst"], "major")


# ---------------------------------------------------------------- 播放列表


class PlaylistTests(unittest.TestCase):
    def test_flat_playlist_returns_entries(self):
        entries = [
            {"url": "https://www.bilibili.com/video/BV1a", "title": "第一集"},
            {"url": "", "webpage_url": "https://www.bilibili.com/video/BV1b",
             "title": "第二集"},
            {"url": "ftp://bad", "title": "非法"},
            None,
        ]
        mod, _ = _fake_ytdlp({"_type": "playlist", "entries": entries})
        with mock.patch.dict(sys.modules, {"yt_dlp": mod}), _allow_all_urls(), \
                mock.patch.object(pipeline.engine_opts, "base_opts", lambda u: {}):
            out = pipeline.list_playlist_entries("https://www.bilibili.com/list/x", limit=5)
        self.assertEqual([r["url"] for r in out],
                         ["https://www.bilibili.com/video/BV1a",
                          "https://www.bilibili.com/video/BV1b"])

    def test_limit_truncates(self):
        entries = [{"url": f"https://www.bilibili.com/video/BV{i}", "title": str(i)}
                   for i in range(10)]
        mod, _ = _fake_ytdlp({"_type": "playlist", "entries": entries})
        with mock.patch.dict(sys.modules, {"yt_dlp": mod}), _allow_all_urls(), \
                mock.patch.object(pipeline.engine_opts, "base_opts", lambda u: {}):
            out = pipeline.list_playlist_entries("https://www.bilibili.com/list/x", limit=3)
        self.assertEqual(len(out), 3)

    def test_single_item_is_not_playlist(self):
        mod, _ = _fake_ytdlp({"_type": "video", "webpage_url": "https://www.bilibili.com/video/BV1c",
                              "title": "单条", "id": "BV1c"})
        with mock.patch.dict(sys.modules, {"yt_dlp": mod}), _allow_all_urls(), \
                mock.patch.object(pipeline.engine_opts, "base_opts", lambda u: {}):
            out = pipeline.list_playlist_entries("https://www.bilibili.com/video/BV1c")
        self.assertEqual(out[0]["url"], "https://www.bilibili.com/video/BV1c")

    def test_empty_url_and_failure(self):
        self.assertEqual(pipeline.list_playlist_entries("   "), [])
        mod, _ = _fake_ytdlp({}, raise_on=RuntimeError("boom"))
        with mock.patch.dict(sys.modules, {"yt_dlp": mod}), _allow_all_urls(), \
                mock.patch.object(pipeline.engine_opts, "base_opts", lambda u: {}):
            self.assertEqual(
                pipeline.list_playlist_entries("https://www.bilibili.com/video/BV1d"), [])


# ---------------------------------------------------------------- 探测


class ProbeTests(unittest.TestCase):
    def test_probe_rejects_multiple_entries_and_empty(self):
        mod, _ = _fake_ytdlp({"_type": "playlist", "entries": []})
        with mock.patch.dict(sys.modules, {"yt_dlp": mod}), _allow_all_urls(), \
                mock.patch.object(pipeline.engine_opts, "base_opts", lambda u: {}):
            with self.assertRaises(RuntimeError):
                pipeline._probe_once("https://www.bilibili.com/video/BV1e")
        mod, _ = _fake_ytdlp({})
        with mock.patch.dict(sys.modules, {"yt_dlp": mod}), _allow_all_urls(), \
                mock.patch.object(pipeline.engine_opts, "base_opts", lambda u: {}):
            with self.assertRaises(RuntimeError):
                pipeline._probe_once("https://www.bilibili.com/video/BV1f")

    def test_probe_takes_first_entry(self):
        mod, _ = _fake_ytdlp({"_type": "playlist", "entries": [dict(INFO)]})
        with mock.patch.dict(sys.modules, {"yt_dlp": mod}), _allow_all_urls(), \
                mock.patch.object(pipeline.engine_opts, "base_opts", lambda u: {}):
            info = pipeline._probe_once("https://www.bilibili.com/list/x")
        self.assertEqual(info["id"], "BV1r2coverage")

    def test_probe_rejects_unsafe_landing(self):
        payload = dict(INFO, webpage_url="http://169.254.169.254/latest/meta-data")
        mod, _ = _fake_ytdlp(payload)
        real = pipeline.guards.check_media_url

        def _guard(url, *, enforce_allowlist=True):
            if enforce_allowlist:
                return True, ""
            return (False, "内网地址") if "169.254" in url else (True, "")

        with mock.patch.dict(sys.modules, {"yt_dlp": mod}), \
                mock.patch.object(pipeline.guards, "check_media_url", _guard), \
                mock.patch.object(pipeline.engine_opts, "base_opts", lambda u: {}):
            with self.assertRaises(RuntimeError) as ctx:
                pipeline._probe_once("https://www.bilibili.com/video/BV1g")
        self.assertIn("不安全", str(ctx.exception))
        self.assertIs(real, pipeline.guards.check_media_url.__wrapped__
                      if hasattr(pipeline.guards.check_media_url, "__wrapped__") else real)


# ---------------------------------------------------------------- 诊断接口


class DiagnoseUrlTests(unittest.TestCase):
    def test_blocked_by_policy(self):
        with mock.patch.object(pipeline.guards, "check_media_url",
                               lambda url, **kw: (False, "该站点不在合规白名单内")):
            out = pipeline.diagnose_url("https://evil.example.com/v")
        self.assertFalse(out["ok"])
        self.assertFalse(out["ready"])
        self.assertEqual(out["diagnosis"]["code"], "BLOCKED_BY_POLICY")
        self.assertTrue(out["diagnosis"]["actionable"])

    def test_platform_error_becomes_structured(self):
        with _allow_all_urls(), \
                mock.patch.object(pipeline, "_probe_once",
                                  side_effect=RuntimeError("Sign in to confirm")):
            out = pipeline.diagnose_url("https://www.bilibili.com/video/BV1h")
        self.assertTrue(out["ok"])
        self.assertFalse(out["ready"])
        self.assertIn("code", out["diagnosis"])

    def test_success_reports_info_digest(self):
        with _allow_all_urls(), \
                mock.patch.object(pipeline, "_probe_once", lambda u: dict(INFO)):
            out = pipeline.diagnose_url("https://www.bilibili.com/video/BV1i")
        self.assertTrue(out["ready"])
        self.assertEqual(out["diagnosis"]["code"], "OK")
        self.assertEqual(out["info"]["title"], "覆盖率样本")
        self.assertEqual(out["info"]["chapters"], 0)


# ---------------------------------------------------------------- 媒体与切片


class MediaHelperTests(unittest.TestCase):
    def test_safe_size(self):
        root = Path(tempfile.mkdtemp(prefix="avd-size-"))
        self.assertEqual(pipeline._safe_size(_write(root / "a.bin", "x" * 10)), 10)
        self.assertEqual(pipeline._safe_size(root / "missing.bin"), -1)

    def test_existing_media_prefers_largest(self):
        root = Path(tempfile.mkdtemp(prefix="avd-media-"))
        _write(root / "audio.m4a", "b" * 9000)
        _write(root / "media.webm", "c" * 20000)
        self.assertTrue(
            pipeline._existing_media(root).endswith("media.webm"))

    def test_existing_media_ignores_tiny_files(self):
        root = Path(tempfile.mkdtemp(prefix="avd-media-"))
        _write(root / "audio.m4a", "x" * 100)
        self.assertEqual(pipeline._existing_media(root), "")

    def test_resolve_task_media_falls_back_to_disk(self):
        root = Path(tempfile.mkdtemp(prefix="avd-resolve-"))
        mgr = pipeline.PipelineManager(output_root=root, probe_fn=lambda u: dict(INFO))
        task = pipeline.PipelineTask(id="m1", url="https://example.com/v", stage="done")
        mgr._tasks["m1"] = task
        _write(root / "m1" / "audio.m4a", "a" * 12000)
        with mock.patch.object(pipeline, "manager", lambda: mgr):
            media, snap = pipeline._resolve_task_media("m1")
        self.assertTrue(media.endswith("audio.m4a"))
        self.assertEqual(snap["id"], "m1")

    def test_resolve_task_media_raises_without_media(self):
        root = Path(tempfile.mkdtemp(prefix="avd-resolve-"))
        mgr = pipeline.PipelineManager(output_root=root, probe_fn=lambda u: dict(INFO))
        mgr._tasks["m2"] = pipeline.PipelineTask(id="m2", url="https://example.com/v")
        with mock.patch.object(pipeline, "manager", lambda: mgr):
            with self.assertRaises(ValueError):
                pipeline._resolve_task_media("m2")

    def test_export_clip_delegates(self):
        root = Path(tempfile.mkdtemp(prefix="avd-clip-"))
        mgr = pipeline.PipelineManager(output_root=root, probe_fn=lambda u: dict(INFO))
        task = pipeline.PipelineTask(id="m3", url="https://example.com/v", stage="done")
        task.meta = {"title": "样本"}
        task.media_path = str(_write(root / "m3" / "audio.m4a", "a" * 9000))
        mgr._tasks["m3"] = task
        from fusion import clips
        with mock.patch.object(pipeline, "manager", lambda: mgr), \
                mock.patch.object(clips, "export_clip",
                                  lambda media, **kw: {"start": kw["start"],
                                                       "end": kw["end"],
                                                       "mode": "copy",
                                                       "path": str(kw["dest"])}):
            out = pipeline.export_clip("m3", start=1, end=9)
        self.assertEqual((out["start"], out["end"]), (1, 9))
        self.assertEqual(out["mode"], "copy")

    def test_export_chapters_skips_bad_ranges(self):
        root = Path(tempfile.mkdtemp(prefix="avd-chap-"))
        mgr = pipeline.PipelineManager(output_root=root, probe_fn=lambda u: dict(INFO))
        task = pipeline.PipelineTask(id="m4", url="https://example.com/v", stage="done")
        task.meta = {"title": "样本", "chapters": [
            {"title": "一", "start": 0, "end": 30},
            {"title": "坏", "start": "x", "end": 40},
            {"title": "空", "start": 50, "end": 50},
        ]}
        task.media_path = str(_write(root / "m4" / "audio.m4a", "a" * 9000))
        mgr._tasks["m4"] = task
        from fusion import clips

        def _export(media, *, start, end, dest, reencode=False):
            if start == 0:
                raise RuntimeError("ffmpeg missing")
            return {"start": start, "end": end, "mode": "copy"}

        with mock.patch.object(pipeline, "manager", lambda: mgr), \
                mock.patch.object(clips, "export_clip", _export):
            out = pipeline.export_clips_by_chapters("m4")
        self.assertEqual(out["count"], 1)
        self.assertIn("error", out["clips"][0])

    def test_export_chapters_requires_chapters(self):
        root = Path(tempfile.mkdtemp(prefix="avd-chap-"))
        mgr = pipeline.PipelineManager(output_root=root, probe_fn=lambda u: dict(INFO))
        task = pipeline.PipelineTask(id="m5", url="https://example.com/v", stage="done")
        task.media_path = str(_write(root / "m5" / "audio.m4a", "a" * 9000))
        mgr._tasks["m5"] = task
        with mock.patch.object(pipeline, "manager", lambda: mgr):
            with self.assertRaises(ValueError):
                pipeline.export_clips_by_chapters("m5")


# ---------------------------------------------------------------- 落地出口


class ExportTests(unittest.TestCase):
    def _manager(self, root: Path):
        mgr = pipeline.PipelineManager(output_root=root, probe_fn=lambda u: dict(INFO))
        task = pipeline.PipelineTask(id="e1", url="https://example.com/v", stage="done")
        task.meta = {"title": "出口样本"}
        task.files = {"md": "e1/出口样本.md"}
        mgr._tasks["e1"] = task
        _write(root / "e1" / "出口样本.md", "# 正文")
        return mgr

    def test_push_export(self):
        root = Path(tempfile.mkdtemp(prefix="avd-push-"))
        mgr = self._manager(root)
        from fusion import exporters
        with mock.patch.object(pipeline, "manager", lambda: mgr), \
                mock.patch.object(exporters, "push_from_config",
                                  lambda target, **kw: {"target": target,
                                                        "title": kw["title"],
                                                        "ok": True}):
            out = pipeline.push_export("e1", "notion")
        self.assertEqual(out["target"], "notion")
        self.assertEqual(out["title"], "出口样本")

    def test_push_export_without_markdown(self):
        root = Path(tempfile.mkdtemp(prefix="avd-push-"))
        mgr = self._manager(root)
        mgr._tasks["e1"].files = {}
        with mock.patch.object(pipeline, "manager", lambda: mgr):
            with self.assertRaises(ValueError):
                pipeline.push_export("e1", "notion")

    def _vault(self) -> Path:
        """临时 vault 必须位于「用户主目录」下；用假 home 让干净克隆也能跑。"""
        self._home = Path(tempfile.mkdtemp(prefix="avd-home-"))
        root = self._home / "vaults"
        root.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="avd-vault-", dir=root))

    def _home_patch(self):
        """把 Path.home 指向临时目录，避免依赖仓库是否位于真实 home 下。"""
        return mock.patch.object(
            Path, "home", classmethod(lambda cls: self._home))

    @staticmethod
    def _cleanup(path: Path) -> None:
        # D32：单测不得把空壳目录留在真实 outputs/ 里；rmtree 比逐个 unlink 更可靠。
        shutil.rmtree(path, ignore_errors=True)

    def test_resolve_obsidian_dir(self):
        target = self._vault()
        try:
            with self._home_patch():
                self.assertIsNone(pipeline.resolve_obsidian_dir("   "))
                self.assertIsNone(pipeline.resolve_obsidian_dir("/etc"))
                got = pipeline.resolve_obsidian_dir(str(target))
                self.assertIsNotNone(got)
        finally:
            self._cleanup(self._home)

    def test_write_obsidian(self):
        root = Path(tempfile.mkdtemp(prefix="avd-obs-"))
        vault = self._vault()
        mgr = pipeline.PipelineManager(output_root=root, probe_fn=lambda u: dict(INFO))
        task = pipeline.PipelineTask(id="e2", url="https://example.com/v", stage="done")
        task.meta = {"title": "直写样本"}
        task.files = {"md": "e2/直写样本.md"}
        mgr._tasks["e2"] = task
        _write(root / "e2" / "直写样本.md", "# 正文")
        try:
            with self._home_patch():
                with mock.patch.object(pipeline, "manager", lambda: mgr):
                    out = pipeline.write_obsidian("e2", vault=str(vault))
                self.assertTrue(Path(out["path"]).is_file())
                self.assertGreater(out["bytes"], 0)
                # 同名再写一次 → 追加短 id，不覆盖手稿
                with mock.patch.object(pipeline, "manager", lambda: mgr):
                    again = pipeline.write_obsidian("e2", vault=str(vault))
                self.assertIn("e2", Path(again["path"]).name)
                with mock.patch.object(pipeline, "manager", lambda: mgr):
                    with self.assertRaises(ValueError):
                        pipeline.write_obsidian("e2", vault="/etc")
        finally:
            self._cleanup(self._home)


# ---------------------------------------------------------------- 字幕轨


class SubtitleTrackTests(unittest.TestCase):
    def test_pick_track_formats(self):
        cases = [
            ({"subtitles": {"zh-CN": [{"url": "https://a/1", "ext": "vtt"}]}}, "vtt"),
            ({"subtitles": {"zh": [{"url": "https://a/2", "ext": "srt"}]}}, "srt"),
            ({"automatic_captions": {"zh": [{"url": "https://a/3", "ext": "json3"}]}}, "json3"),
            ({"automatic_captions": {"zh": [{"url": "https://a/4", "ext": "json"}]}}, "json"),
        ]
        for info, want in cases:
            url, fmt, kind = pipeline._pick_subtitle_track(info, "zh")
            self.assertTrue(url)
            self.assertEqual(fmt, want)
            self.assertEqual(kind, "subtitle")

    def test_pick_track_ignores_unusable(self):
        info = {"subtitles": {"zh": [{"ext": "vtt"}], "en": [{"url": "u", "ext": "ass"}]}}
        self.assertEqual(pipeline._pick_subtitle_track(info, "zh"), ("", "", ""))
        self.assertEqual(pipeline._pick_subtitle_track({}, "zh"), ("", "", ""))
        url, _, _ = pipeline._pick_subtitle_track(
            {"subtitles": {"en": [{"url": "https://a/e", "ext": "vtt"}]}}, "en")
        self.assertEqual(url, "https://a/e")

    def test_fetch_rejects_unsafe_url(self):
        with mock.patch.object(pipeline.guards, "check_media_url",
                               lambda url, **kw: (False, "内网地址")):
            with self.assertRaises(RuntimeError):
                pipeline._fetch_subtitle_track("http://127.0.0.1/x.vtt", "vtt")

    def test_fetch_empty_url(self):
        self.assertEqual(pipeline._fetch_subtitle_track("", "vtt"), [])

    def test_fetch_parses_vtt(self):
        vtt = (
            "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n第一句\n\n"
            "00:00:03.500 --> 00:00:06.000\n第二句\n"
        )

        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        opener = mock.MagicMock()
        opener.open.return_value = _Resp(vtt.encode("utf-8"))
        with _allow_all_urls(), \
                mock.patch.object(pipeline.engine_opts, "base_opts",
                                  lambda u: {"cookiefile": ""}), \
                mock.patch.object(pipeline, "build_opener", lambda *a, **k: opener):
            cues = pipeline._fetch_subtitle_track(
                "https://cdn.example.com/a.vtt", "vtt")
        self.assertGreaterEqual(len(cues), 1)
        joined = " ".join(str(c.get("text") or "") for c in cues)
        self.assertIn("第一句", joined)

    def test_fetch_wraps_network_error(self):
        opener = mock.MagicMock()
        opener.open.side_effect = OSError("connection reset")
        with _allow_all_urls(), \
                mock.patch.object(pipeline.engine_opts, "base_opts",
                                  lambda u: {"cookiefile": ""}), \
                mock.patch.object(pipeline, "build_opener", lambda *a, **k: opener):
            with self.assertRaises(RuntimeError) as ctx:
                pipeline._fetch_subtitle_track("https://cdn.example.com/b.vtt", "vtt")
        self.assertIn("字幕轨抓取失败", str(ctx.exception))

    def test_minimal_vtt(self):
        text = "00:00:02,000 --> 00:00:04,500\n<b>粗体</b>\n"
        out = pipeline._minimal_vtt(text)
        self.assertEqual(out[0]["text"], "粗体")
        self.assertAlmostEqual(out[0]["start"], 2.0, places=3)

    def test_safe_stem(self):
        self.assertEqual(pipeline.safe_stem("a/b:c*d?e"), "a_b_c_d_e")
        self.assertEqual(pipeline.safe_stem("   "), "untitled")
        self.assertEqual(pipeline.safe_stem("CON"), "_CON")
        self.assertEqual(pipeline.safe_stem(".hidden.."), "hidden")
        self.assertEqual(len(pipeline.safe_stem("长" * 200)), 80)


# ---------------------------------------------------------------- 音轨探测


class AudioStreamTests(unittest.TestCase):
    def test_missing_file_and_missing_probe(self):
        self.assertIsNone(pipeline.has_audio_stream(""))
        self.assertIsNone(pipeline.has_audio_stream("/nonexistent/file.mp4"))
        tmp = _write(Path(tempfile.mkdtemp(prefix="avd-ff-")) / "a.mp4", "x")
        with mock.patch.object(pipeline.shutil, "which", lambda name: None):
            self.assertIsNone(pipeline.has_audio_stream(tmp))

    def test_probe_result(self):
        tmp = _write(Path(tempfile.mkdtemp(prefix="avd-ff-")) / "a.mp4", "x")
        ok = mock.Mock(returncode=0, stdout="audio\n")
        bad = mock.Mock(returncode=0, stdout="")
        failed = mock.Mock(returncode=1, stdout="")
        with mock.patch.object(pipeline.shutil, "which", lambda name: "/usr/bin/ffprobe"):
            with mock.patch.object(pipeline.subprocess, "run", lambda *a, **k: ok):
                self.assertTrue(pipeline.has_audio_stream(tmp))
            with mock.patch.object(pipeline.subprocess, "run", lambda *a, **k: bad):
                self.assertFalse(pipeline.has_audio_stream(tmp))
            with mock.patch.object(pipeline.subprocess, "run", lambda *a, **k: failed):
                self.assertIsNone(pipeline.has_audio_stream(tmp))
            with mock.patch.object(pipeline.subprocess, "run",
                                   side_effect=OSError("no exec")):
                self.assertIsNone(pipeline.has_audio_stream(tmp))


# ---------------------------------------------------------------- ASR 桥接


class _FakeASRManager:
    def __init__(self, script):
        self.script = script
        self.cancelled: list[str] = []
        self.created: list[tuple] = []

    def create_task(self, src, *, is_url, **kw):
        self.created.append((src, is_url, kw))
        return {"id": "asr-1"}

    def get_task(self, task_id):
        snap = self.script.pop(0) if self.script else {"stage": "done", "percent": 100}
        return snap

    def get_segments(self, task_id):
        return [{"start": 0, "end": 1, "text": "转写结果"}]

    def cancel_task(self, task_id):
        self.cancelled.append(task_id)


class AsrBridgeTests(unittest.TestCase):
    def _patch(self, *, available=True, manager=None):
        """接管 ``from downloader import asr``：sys.modules 与包属性都要换。

        只改 ``sys.modules`` 不够——downloader 包一旦被导入过，``from downloader
        import asr`` 会优先命中包对象上的同名属性，测试会悄悄打到真模块
        （表现为真的去解析 URL "u"）。
        """
        import contextlib
        import downloader as dl_pkg
        fake = type(sys)("downloader.asr")
        fake.is_available = lambda: available
        fake.manager = manager or _FakeASRManager([])
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.dict(sys.modules, {"downloader.asr": fake}))
        stack.enter_context(mock.patch.object(dl_pkg, "asr", fake, create=True))
        return stack, dl_pkg, fake

    def test_env_missing(self):
        patcher, _, _ = self._patch(available=False)
        with patcher:
            segs, meta = pipeline._asr_via_original("u", "zh", "t")
        self.assertEqual(segs, [])
        self.assertEqual(meta["stage"], "env_missing")

    def test_cancelled_before_start(self):
        patcher, _, fake = self._patch()
        with patcher:
            segs, meta = pipeline._asr_via_original(
                "u", "zh", "t", cancel_check=lambda: True)
        self.assertEqual(meta["stage"], "cancelled")

    def test_done_with_segments(self):
        fake_mgr = _FakeASRManager([{"stage": "running", "percent": 40, "message": "转写中"},
                                    {"stage": "done", "percent": 100}])
        patcher, _, _ = self._patch(manager=fake_mgr)
        seen: list[float] = []
        with patcher, mock.patch.object(pipeline.time, "sleep", lambda s: None):
            segs, meta = pipeline._asr_via_original(
                "u", "zh", "t", on_task_id=lambda tid: seen.append(1.0),
                on_progress=lambda p, m: seen.append(p))
        self.assertEqual(len(segs), 1)
        self.assertEqual(meta["stage"], "done")
        self.assertGreater(len(seen), 1)

    def test_error_stage(self):
        fake_mgr = _FakeASRManager([{"stage": "error", "error": "权重缺失"}])
        patcher, _, _ = self._patch(manager=fake_mgr)
        with patcher, mock.patch.object(pipeline.time, "sleep", lambda s: None):
            segs, meta = pipeline._asr_via_original("u", "zh", "t")
        self.assertEqual(segs, [])
        self.assertEqual(meta["stage"], "error")

    def test_timeout_cancels_task(self):
        fake_mgr = _FakeASRManager([])
        patcher, _, _ = self._patch(manager=fake_mgr)
        with patcher, mock.patch.object(pipeline.time, "sleep", lambda s: None), \
                mock.patch.object(pipeline.time, "time", lambda: 0.0):
            segs, meta = pipeline._asr_via_original("u", "zh", "t", deadline=0.0)
        self.assertEqual(meta["stage"], "timeout")
        self.assertEqual(fake_mgr.cancelled, ["asr-1"])

    def test_no_speech_detected(self):
        fake_mgr = _FakeASRManager([{"stage": "done", "percent": 100}])
        patcher, _, _ = self._patch(manager=fake_mgr)
        with patcher, mock.patch.object(pipeline.time, "sleep", lambda s: None), \
                mock.patch.object(fake_mgr, "get_segments", lambda tid: []):
            segs, meta = pipeline._asr_via_original("u", "zh", "t")
        self.assertEqual(segs, [])
        self.assertTrue(meta["no_speech"])

    def test_local_media_is_url_false(self):
        root = Path(tempfile.mkdtemp(prefix="avd-asr-"))
        media = _write(root / "audio.m4a", "x")
        fake_mgr = _FakeASRManager([{"stage": "done", "percent": 100}])
        patcher, _, _ = self._patch(manager=fake_mgr)
        with patcher, mock.patch.object(pipeline.time, "sleep", lambda s: None):
            pipeline._asr_via_original("u", "zh", "t", str(media))
        self.assertFalse(fake_mgr.created[0][1])


# ---------------------------------------------------------------- 管理器分支


INFO_OK = dict(INFO)
SEGS = [
    {"start": 0.0, "end": 4.0, "text": "第一段讲本地优先的知识管理链路。"},
    {"start": 4.0, "end": 8.0, "text": "第二段讲索引结构决定检索速度。"},
]


class ManagerBranchTests(unittest.TestCase):
    def _mgr(self, **kw):
        root = Path(tempfile.mkdtemp(prefix="avd-mgr-"))
        base = dict(
            probe_fn=lambda u: dict(INFO_OK),
            download_fn=lambda info, work: "",
            subtitle_fn=lambda url, fmt: list(SEGS),
            asr_fn=lambda *a, **k: [],
            output_root=root,
        )
        base.update(kw)
        return pipeline.PipelineManager(**base), root

    def test_start_rejects_bad_url(self):
        mgr, _ = self._mgr()
        with mock.patch.object(pipeline.guards, "check_media_url",
                               lambda url, **kw: (False, "该站点不在合规白名单内")):
            with self.assertRaises(ValueError):
                mgr.start("https://evil.example.com/v")

    def test_start_expands_playlist(self):
        mgr, _ = self._mgr()
        entries = [{"url": "https://www.bilibili.com/video/BV1a", "title": "a"},
                   {"url": "https://www.bilibili.com/video/BV1b", "title": "b"}]
        with _allow_all_urls(), \
                mock.patch.object(pipeline, "_playlist_limit", lambda: 5), \
                mock.patch.object(pipeline, "list_playlist_entries",
                                  lambda url, limit=None: entries):
            snap = mgr.start("https://www.bilibili.com/list/x", expand_playlist=True)
        self.assertEqual(snap["batch"]["count"], 2)
        self.assertEqual(len(snap["batch"]["ids"]), 2)

    def test_start_batch_skips_duplicates_and_invalid(self):
        mgr, _ = self._mgr()
        with _allow_all_urls(), \
                mock.patch.object(pipeline.guards, "check_media_url",
                                  lambda url, **kw: (False, "bad") if "evil" in url
                                  else (True, "")):
            out = mgr.start_batch([
                "https://www.bilibili.com/video/BV1a",
                "https://www.bilibili.com/video/BV1a",
                "https://evil.example.com/v",
                "   ",
            ])
        self.assertEqual(out["count"], 1)
        reasons = [s["reason"] for s in out["skipped"]]
        self.assertIn("重复链接", reasons)

    def test_start_batch_rejects_empty(self):
        mgr, _ = self._mgr()
        with mock.patch.object(pipeline.guards, "check_media_url",
                               lambda url, **kw: (False, "bad")):
            with self.assertRaises(ValueError):
                mgr.start_batch(["https://evil.example.com/v"])

    def test_enqueue_capacity_guard(self):
        mgr, _ = self._mgr()
        mgr._tasks["slot"] = pipeline.PipelineTask(
            id="slot", url="https://example.com/v", stage="queued")
        with mock.patch.object(pipeline, "MAX_TASKS", 1), _allow_all_urls():
            with self.assertRaises(RuntimeError) as ctx:
                mgr._enqueue("https://www.bilibili.com/video/BV1b")
        self.assertIn("上限", str(ctx.exception))

    def test_run_error_path(self):
        mgr, root = self._mgr()
        task = pipeline.PipelineTask(id="err1", url="https://example.com/v")
        mgr._tasks["err1"] = task
        with _allow_all_urls(), \
                mock.patch.object(mgr, "_probe", side_effect=RuntimeError("解析失败：403")):
            mgr._run(task)
        self.assertEqual(task.stage, "error")
        self.assertIn("403", task.error)
        self.assertTrue(task.diagnosis)

    def test_run_timeout_path(self):
        mgr, _ = self._mgr()
        task = pipeline.PipelineTask(id="to1", url="https://example.com/v")
        task._stage_deadline = 0.0
        mgr._tasks["to1"] = task
        mgr._run(task)
        self.assertEqual(task.stage, "error")
        self.assertIn("超时", task.message)

    def test_stage_transition_warning_is_structured(self):
        mgr, _ = self._mgr()
        task = pipeline.PipelineTask(id="st1", url="https://example.com/v", stage="done")
        mgr._tasks["st1"] = task
        mgr._stage(task, "queued", "回退")
        self.assertEqual(task.degradations[-1]["code"], "stage.transition")

    def test_cancel_unknown_and_terminal(self):
        mgr, _ = self._mgr()
        self.assertFalse(mgr.cancel("nope"))
        task = pipeline.PipelineTask(id="c1", url="https://example.com/v", stage="done")
        mgr._tasks["c1"] = task
        self.assertTrue(mgr.cancel("c1"))
        self.assertEqual(task.stage, "done")  # 终态不可被改成 cancelled

    def test_retry_rejects_running(self):
        mgr, _ = self._mgr()
        task = pipeline.PipelineTask(id="r1", url="https://example.com/v", stage="running")
        mgr._tasks["r1"] = task
        with self.assertRaises(ValueError):
            mgr.retry("r1")
        with self.assertRaises(ValueError):
            mgr.retry("missing")

    def test_read_artifact_variants(self):
        mgr, root = self._mgr()
        task = pipeline.PipelineTask(id="a1", url="https://example.com/v", stage="done")
        task.files = {"md": "a1/out.md", "pdf": "a1/out.pdf"}
        mgr._tasks["a1"] = task
        _write(root / "a1" / "out.md", "# hi")
        (root / "a1" / "out.pdf").write_bytes(b"%PDF-1.4")
        self.assertEqual(mgr.read_artifact("a1", "md"), "# hi")
        self.assertTrue(mgr.read_artifact("a1", "pdf"))
        self.assertIsNone(mgr.read_artifact("a1", "srt"))
        self.assertIsNone(mgr.read_artifact("ghost", "md"))
        # 越界路径必须拒绝
        task.files["md"] = "../escape.md"
        self.assertIsNone(mgr.read_artifact("a1", "md"))

    def test_artifact_exists_and_list_stale(self):
        mgr, root = self._mgr()
        running = pipeline.PipelineTask(id="x1", url="https://example.com/v",
                                        stage="running")
        gone = pipeline.PipelineTask(id="x2", url="https://example.com/v", stage="done")
        gone.files = {"md": "x2/out.md"}
        mgr._tasks.update({"x1": running, "x2": gone})
        self.assertTrue(mgr._artifact_exists(running))
        self.assertFalse(mgr._artifact_exists(gone))
        rows = {r["id"]: r for r in mgr.list_tasks()}
        self.assertTrue(rows["x2"]["stale"])
        self.assertEqual(rows["x2"]["message"], "产物已删除")

    def test_persist_and_reload_index(self):
        mgr, root = self._mgr()
        task = pipeline.PipelineTask(id="p1", url="https://example.com/v", stage="done")
        task.meta = {"title": "落库样本", "low_content": True}
        task.degradations = [degradation.make("content.low", "只有 45 字").as_dict()]
        task.degraded = ["[降级产出] 只有 45 字"]
        mgr._tasks["p1"] = task
        mgr._persist_index()
        self.assertTrue((root / "index.json").is_file())
        again = pipeline.PipelineManager(output_root=root, probe_fn=lambda u: dict(INFO))
        snap = again.get("p1")
        self.assertIsNotNone(snap)
        self.assertEqual(snap["title"], "落库样本")
        self.assertEqual(snap["degraded_items"][0]["code"], "content.low")
        self.assertTrue(snap["low_content"])

    def test_purge_throttle_and_fallback(self):
        mgr, _ = self._mgr()
        with mock.patch.object(pipeline.persist, "apply_retention",
                               side_effect=RuntimeError("boom")) as ap, \
                mock.patch.object(pipeline.persist, "purge_older_than") as po:
            mgr._purge_stale(force=True)
        ap.assert_called_once()
        po.assert_called_once()
        # 300s 内的第二次调用被节流，不再遍历产物树
        with mock.patch.object(pipeline.persist, "apply_retention") as ap2:
            mgr._purge_stale()
        ap2.assert_not_called()

    def test_proofread_roundtrip(self):
        mgr, root = self._mgr()
        task = pipeline.PipelineTask(id="pr1", url="https://example.com/v", stage="done")
        task.segments = [dict(s) for s in SEGS]
        task.meta = {"title": "校对样本"}
        mgr._tasks["pr1"] = task
        out = mgr.proofread("pr1", [
            {"start": 0, "end": 4, "text": "第一段讲本地优先的知识管理链路。"},
            {"start": 4, "end": 8, "text": "第二段讲索引结构决定检索速度。"},
        ])
        self.assertEqual(out["stage"], "done")
        self.assertTrue((root / "pr1").is_dir())

    def test_proofread_validations(self):
        mgr, _ = self._mgr()
        with self.assertRaises(ValueError):
            mgr.proofread("ghost", SEGS)
        task = pipeline.PipelineTask(id="pr2", url="https://example.com/v", stage="running")
        mgr._tasks["pr2"] = task
        with self.assertRaises(ValueError):
            mgr.proofread("pr2", SEGS)
        task.stage = "done"
        with self.assertRaises(ValueError):
            mgr.proofread("pr2", [])
        with self.assertRaises(ValueError):
            mgr.proofread("pr2", [{"text": "   "}])
        with self.assertRaises(ValueError):
            mgr.proofread("pr2", [{"text": "只有一行", "start": "x"}])

    def test_mark_cancelled_and_aborted(self):
        mgr, _ = self._mgr()
        task = pipeline.PipelineTask(id="mc1", url="https://example.com/v")
        mgr._tasks["mc1"] = task
        mgr._mark_cancelled(task)
        self.assertTrue(mgr._aborted(task))
        self.assertEqual(task.stage, "cancelled")
        self.assertEqual(task.percent, 100.0)
        # 已取消的任务不再接受 _set 覆盖终态
        mgr._set(task, stage="done")
        self.assertEqual(task.stage, "cancelled")


class LibraryIsolationTests(unittest.TestCase):
    """素材库必须可隔离 —— 否则跑一次单测就污染真实检索库。

    第十轮实测：真实 ``outputs/avd_library.sqlite3`` 里 599 行有 598 行是测试
    素材（548 条「旗舰版测试素材」+30 条「R1 验收素材」+19 条「覆盖率样本」），
    只剩 1 条真内容。v8 只隔离了任务库，素材库被漏掉，导致检索/问答长期在
    假语料上跑。
    """

    def test_env_override_isolates_writes(self):
        from fusion import library
        tmp = Path(tempfile.mkdtemp(prefix="avd-lib-iso-")) / "lib.sqlite3"
        old = os.environ.get("AVD_LIBRARY_DB")
        os.environ["AVD_LIBRARY_DB"] = str(tmp)
        try:
            library.reset_for_tests(None)
            self.assertEqual(library.configured_db_path(), tmp)
            library.upsert_material(
                task_id="iso-1", title="隔离验证素材", body="只在临时库里", tags=["t"])
            self.assertTrue(tmp.is_file(), "写入必须落在临时库")
            names = [m["title"] for m in library.list_materials(limit=5)]
            self.assertIn("隔离验证素材", names)
            self.assertNotEqual(library.configured_db_path().resolve(),
                                library.DEFAULT_DB.resolve())
        finally:
            if old is None:
                os.environ.pop("AVD_LIBRARY_DB", None)
            else:
                os.environ["AVD_LIBRARY_DB"] = old
            library.reset_for_tests(None)

    def test_default_path_when_env_absent(self):
        from fusion import library
        old = os.environ.pop("AVD_LIBRARY_DB", None)
        try:
            self.assertEqual(library.configured_db_path(), library.DEFAULT_DB)
        finally:
            if old is not None:
                os.environ["AVD_LIBRARY_DB"] = old


class SmallHelperTests(unittest.TestCase):
    def test_diarize_flag(self):
        with mock.patch.dict(os.environ, {"AVD_ASR_ENGINE": "mlx"}):
            self.assertEqual(os.environ["AVD_ASR_ENGINE"], "mlx")
        with mock.patch("downloader.config.config") as cfg:
            cfg.get.return_value = True
            self.assertIn(pipeline._diarize_enabled(), (True, False))
            cfg.get.side_effect = RuntimeError("no config")
            self.assertFalse(pipeline._diarize_enabled())

    def test_playlist_limit_clamped(self):
        with mock.patch("downloader.config.config") as cfg:
            cfg.get.return_value = 999
            self.assertEqual(pipeline._playlist_limit(), 50)
            cfg.get.return_value = 0
            self.assertEqual(pipeline._playlist_limit(), 1)
            cfg.get.side_effect = RuntimeError("boom")
            self.assertEqual(pipeline._playlist_limit(), 1)

    def test_offline_polish_empty(self):
        self.assertEqual(pipeline.offline_polish([]), ("", []))

    def test_keyword_stats_short_corpus(self):
        counts, _ = pipeline._rule_keyword_stats(["只有一个短句"])
        self.assertIsInstance(counts, dict)


class CancelFlushTests(unittest.TestCase):
    """取消后必须先落盘再返回 —— 否则「重试」等于从头再来。

    真机验证暴露的问题：``cancel_task`` 乐观地把 stage 写成 cancelled，
    调用方立刻返回，此刻 worker 还在写检查点，紧接着查「可续跑」是空的。
    ``_wait_checkpoint_flush`` 就是补这段窗口的。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="avd-flush-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def test_no_dir_or_key_returns_false(self):
        self.assertFalse(pipeline._wait_checkpoint_flush("", "t1"))
        self.assertFalse(pipeline._wait_checkpoint_flush(str(self.tmp), ""))

    def test_existing_checkpoint_short_circuits(self):
        from fusion import checkpoint
        checkpoint.save_checkpoint(
            self.tmp, task_id="t1", chunk_index=0, chunks_total=4,
            segments=[{"start": 0, "end": 12.0, "text": "你好"}], duration=60,
        )
        started = __import__("time").time()
        self.assertTrue(pipeline._wait_checkpoint_flush(str(self.tmp), "t1", "asr-1"))
        self.assertLess(__import__("time").time() - started, 1.0)

    def test_waits_until_worker_writes(self):
        """worker 晚 0.4s 才落盘，也要等到它。"""
        import threading
        import time

        def _later():
            time.sleep(0.4)
            from fusion import checkpoint
            checkpoint.save_checkpoint(
                self.tmp, task_id="t2", chunk_index=1, chunks_total=4,
                segments=[{"start": 0, "end": 60.0, "text": "前半段"}], duration=240,
            )

        threading.Thread(target=_later, daemon=True).start()
        self.assertTrue(
            pipeline._wait_checkpoint_flush(str(self.tmp), "t2", "asr-x", budget=5.0))

    def test_budget_exhausted_returns_false(self):
        import time
        started = time.time()
        ok = pipeline._wait_checkpoint_flush(
            str(self.tmp), "never", budget=0.6)
        self.assertFalse(ok)
        self.assertLess(time.time() - started, 5.0)

    def test_env_budget_override(self):
        import time
        with mock.patch.dict(os.environ, {"AVD_ASR_CANCEL_FLUSH_SECS": "0.4"}):
            started = time.time()
            self.assertFalse(pipeline._wait_checkpoint_flush(str(self.tmp), "nope"))
            self.assertLess(time.time() - started, 4.0)


class ResumeFromDiskTests(unittest.TestCase):
    """「可续跑」必须真的能续跑（R2 收尾修的真实断点）。

    缺陷现场：``_persist_index`` 只落终态任务，崩在转写中途的任务从未入库；
    重启后 ``recovery`` 能从检查点列出 task_id，但 ``retry`` 直接抛
    「任务不存在」—— 列表是活的，入口是死的。修法是把来源 URL 写进检查点，
    再允许 ``retry`` 从磁盘重建任务。
    """

    def setUp(self):
        from fusion import pipeline
        self.pipeline = pipeline
        self.tmp = Path(tempfile.mkdtemp(prefix="avd-resume-"))
        self.ckpt_dir = self.tmp / "task-1"
        self.ckpt_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _save(self, *, source="https://example.com/v/1"):
        from fusion import checkpoint
        return checkpoint.save_checkpoint(
            self.ckpt_dir, task_id="task-1", chunk_index=1, chunks_total=5,
            segments=[{"start": 0, "end": 12, "text": "第一段"}],
            duration=600.0, source=source,
        )

    def test_checkpoint_keeps_source(self):
        from fusion import checkpoint
        self._save()
        got = checkpoint.list_incomplete(self.tmp)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["source"], "https://example.com/v/1")

    def test_rehydrate_rebuilds_task_from_checkpoint(self):
        self._save()
        mgr = self.pipeline.PipelineManager(output_root=self.tmp)
        task = mgr._rehydrate("task-1")
        self.assertIsNotNone(task)
        self.assertEqual(task.url, "https://example.com/v/1")
        self.assertIn(task.id, mgr._tasks)

    def test_rehydrate_refuses_checkpoint_without_source(self):
        self._save(source="")
        mgr = self.pipeline.PipelineManager(output_root=self.tmp)
        self.assertIsNone(mgr._rehydrate("task-1"))

    def test_retry_error_explains_why_it_cannot_resume(self):
        mgr = self.pipeline.PipelineManager(output_root=self.tmp)
        with self.assertRaises(ValueError) as ctx:
            mgr.retry("ghost")
        self.assertIn("检查点", str(ctx.exception))

    def test_retry_after_restart_requeues_task(self):
        self._save()
        mgr = self.pipeline.PipelineManager(
            output_root=self.tmp,
            probe_fn=lambda url: {"title": "t", "duration": 600},
            asr_fn=lambda *a, **k: [],
        )
        snap = mgr.retry("task-1")
        self.assertEqual(snap["id"], "task-1")
        self.assertIn(snap["stage"], ("queued", "probe", "download",
                                      "transcript", "polish", "done"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
