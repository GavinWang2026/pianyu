# Copyright (c) 2026 Hyht-Team
# SPDX-License-Identifier: MIT
# See LICENSE (MIT) and NOTICE.md (third-party components).
"""R1 修复的验收测试（第九轮报告 3 个 P1 + 5 个 P2）。

每条用例对应报告里一条验收标准，全部可离线跑、不联网、不依赖 ASR 运行时：

P1-1 Cookie 出包        → 解析不再回写包内 cookies.txt；扫描器默认拦截
P1-2 保留策略作用域      → trim 只作用于本管理器 output_root，兄弟目录零删除
P1-3 下载误判           → 已落盘媒体直接复用，降级原因不再谎报
P2-1 低内容告警         → 片头/纯音乐素材不出摘要与要点，并显式声明
P2-2 门禁用例           → diagnose_pack 覆盖补齐（覆盖率由覆盖率测试兜底）
P2-3 关键词引擎         → 见 test_fusion 的三条关键词用例
P2-4 DOCX 样式/目录     → styles.xml + TOC 域 + Heading 样式
P2-5 安全响应头         → HEAD 与 GET 同套头，Server 不含版本号
"""

from __future__ import annotations

import io
import os
import re
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 测试隔离（HIST-01）：绝不写生产 outputs/avd_tasks.sqlite3
os.environ.setdefault(
    "AVD_DB_PATH",
    str(Path(tempfile.mkdtemp(prefix="avd-test-db-")) / "tasks.sqlite3"),
)
# 素材库同样要隔离：库路径写死时单测会把「覆盖率样本」灌进真实素材库
os.environ.setdefault(
    "AVD_LIBRARY_DB",
    str(Path(tempfile.mkdtemp(prefix="avd-test-lib-")) / "library.sqlite3"),
)

from fusion import engine_opts, knowledge, layout, pipeline  # noqa: E402

INFO = {
    "id": "BV1r1",
    "title": "R1 验收素材",
    "extractor_key": "Bilibili",
    "duration": 180,
    "webpage_url": "https://www.bilibili.com/video/BV1r1",
    "chapters": [],
    "subtitles": {},
    "automatic_captions": {},
}

PLAIN_SEGS = [
    {"start": 0.0, "end": 6.0,
     "text": "今天讲本地优先的知识管理，第一步是把素材入库，第二步才是建索引。"},
    {"start": 6.0, "end": 12.0,
     "text": "素材入库之后要建倒排索引，索引质量直接决定检索速度与命中率。"},
    {"start": 12.0, "end": 18.0,
     "text": "检索速度取决于索引结构本身，而不是机器性能，这点要特别强调。"},
]


class CookieOutOfPackageTests(unittest.TestCase):
    """P1-1：包内 cookies.txt 只读，yt-dlp 回写落在 .state。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self._orig_file = os.environ.get("AVD_COOKIE_FILE")
        self._orig_state = os.environ.get("AVD_STATE_DIR")
        pkg = self.base / "pkg"
        (pkg / "cookies").mkdir(parents=True)
        self.pkg_cookie = pkg / "cookies" / "cookies.txt"
        self.pkg_cookie.write_text(
            "# Netscape HTTP Cookie File\n"
            ".douyin.com\tTRUE\t/\tTRUE\t0\tttwid\tORIGINAL\n",
            encoding="utf-8",
        )
        self.state = self.base / "state"
        os.environ["AVD_COOKIE_FILE"] = str(self.pkg_cookie)
        os.environ["AVD_STATE_DIR"] = str(self.state)
        self._orig_within = engine_opts.guards.within
        engine_opts.guards.within = lambda root, cand: Path(cand).resolve()

    def tearDown(self) -> None:
        engine_opts.guards.within = self._orig_within
        for key, val in (("AVD_COOKIE_FILE", self._orig_file),
                         ("AVD_STATE_DIR", self._orig_state)):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        self.tmp.cleanup()

    def test_work_copy_lives_outside_package(self) -> None:
        work = engine_opts.writable_cookie_file()
        self.assertIsNotNone(work)
        assert work is not None
        self.assertEqual(work.resolve(), (self.state / "cookies.work.txt").resolve())
        self.assertNotEqual(work.resolve(), self.pkg_cookie.resolve())
        self.assertEqual(work.read_text(encoding="utf-8").count("ttwid"), 1)

    def test_engine_writeback_does_not_touch_package_copy(self) -> None:
        """模拟 yt-dlp 回写整罐 Cookie：包内文件必须字节不变。"""
        before = self.pkg_cookie.read_bytes()
        work = engine_opts.writable_cookie_file()
        assert work is not None
        with work.open("a", encoding="utf-8") as fh:
            for i in range(50):
                fh.write(f".site{i}.com\tTRUE\t/\tTRUE\t0\tsessionid\tABCDEF{i:04d}\n")
        self.assertEqual(self.pkg_cookie.read_bytes(), before)
        self.assertEqual(os.stat(work).st_mode & 0o777, 0o600)

    def test_secrets_scan_rejects_delivery_pack(self) -> None:
        sys.path.insert(0, str(ROOT / "tools"))
        import secrets_scan  # noqa: WPS433

        hits = secrets_scan.scan(self.base / "pkg")
        # 占位值 "ORIGINAL" 只有 8 字符且不是真值语义，仍应被严格判为命中
        self.assertTrue(hits, "含 ttwid 真实值的 cookies.txt 必须命中")
        clean = self.base / "clean"
        (clean / "cookies").mkdir(parents=True)
        (clean / "cookies" / "cookies.txt").write_text(
            "# Netscape HTTP Cookie File\n"
            ".douyin.com\tTRUE\t/\tTRUE\t0\tttwid\t<YOUR_VALUE>\n",
            encoding="utf-8",
        )
        self.assertFalse(secrets_scan.scan(clean), "占位模板不得误报")


class RetentionScopeTests(unittest.TestCase):
    """P1-2：保留策略只裁剪自己的 output_root，且不在热路径上重复执行。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / "outputs" / "pipeline"
        self.root.mkdir(parents=True)
        self.sibling = self.base / "outputs" / "other"
        self.sibling.mkdir(parents=True)
        self.keep = self.sibling / "audio.mp4"
        self.keep.write_bytes(b"x" * 64)
        old = time.time() - 400 * 24 * 3600
        os.utime(self.keep, (old, old))
        self.calls: list[Path] = []
        self._orig = pipeline.persist.apply_retention

        def fake_retention(*, outputs_root=None):
            self.calls.append(Path(outputs_root))
            return {"purged_rows": 0, "trim": {}, "policy": {}}

        pipeline.persist.apply_retention = fake_retention

    def tearDown(self) -> None:
        pipeline.persist.apply_retention = self._orig
        self.tmp.cleanup()

    def test_scope_is_output_root_not_parent(self) -> None:
        pipeline.PipelineManager(download_fn=lambda i, w: "",
                                 output_root=self.root)
        self.assertTrue(self.calls)
        for root in self.calls:
            self.assertEqual(root.resolve(), self.root.resolve(),
                             "trim 作用域越界到父目录会成为兄弟目录杀手")
        self.assertTrue(self.keep.is_file(), "兄弟目录媒体不得被删除")

    def test_purge_is_throttled_and_off_hot_path(self) -> None:
        mgr = pipeline.PipelineManager(
            probe_fn=lambda u: dict(INFO),
            download_fn=lambda i, w: "",
            subtitle_fn=lambda u, f: list(PLAIN_SEGS),
            asr_fn=lambda *a, **k: [],
            output_root=self.root,
        )
        self.assertEqual(len(self.calls), 1)
        mgr._purge_stale()
        mgr._purge_stale()
        self.assertEqual(len(self.calls), 1, "节流窗口内不得重复全树遍历")
        mgr._enqueue("https://www.bilibili.com/video/BV1r1")
        self.assertEqual(len(self.calls), 1, "_enqueue 不得再触发保留策略")
        mgr._purge_stale(force=True)
        self.assertEqual(len(self.calls), 2)


class DownloadReuseTests(unittest.TestCase):
    """P1-3：下载后处理异常时复用已落盘媒体，不重下、不谎报。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "pipeline"
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _wait(self, mgr, tid: str, timeout: float = 15.0) -> dict:
        end = time.time() + timeout
        snap: dict = {}
        while time.time() < end:
            snap = mgr.get(tid) or {}
            if snap.get("stage") in ("done", "error", "cancelled"):
                return snap
            time.sleep(0.02)
        self.fail(f"任务未在 {timeout}s 内进入终态：{snap}")

    def test_download_exception_reuses_media_on_disk(self) -> None:
        calls = {"n": 0}

        def flaky_download(info, work_dir):
            calls["n"] += 1
            media = Path(work_dir) / "audio.m4a"
            media.parent.mkdir(parents=True, exist_ok=True)
            media.write_bytes(b"0" * 8192)
            raise RuntimeError("ffmpeg postprocess failed after writing file")

        mgr = pipeline.PipelineManager(
            probe_fn=lambda u: dict(INFO),
            download_fn=flaky_download,
            subtitle_fn=lambda u, f: [],
            asr_fn=lambda *a, **k: list(PLAIN_SEGS),
            output_root=self.root,
        )
        snap = self._wait(mgr, mgr.start("https://www.bilibili.com/video/BV1r1")["id"])
        self.assertEqual(snap["stage"], "done", snap.get("error"))
        self.assertEqual(calls["n"], 1, "媒体已在盘上，不得再发第二次下载")
        joined = " ".join(snap.get("degraded") or [])
        self.assertNotIn("音频下载失败", joined)
        self.assertIn("落盘", joined)

    def test_existing_media_short_circuits_download(self) -> None:
        work = Path(tempfile.mkdtemp(prefix="avd-media-"))
        media = work / "audio.m4a"
        media.write_bytes(b"0" * 8192)
        got = pipeline._existing_media(work)
        self.assertEqual(Path(got).resolve(), media.resolve())
        # 太小/不存在时不得误认
        small = Path(tempfile.mkdtemp(prefix="avd-media-small-"))
        (small / "audio.m4a").write_bytes(b"0")
        self.assertEqual(pipeline._existing_media(small), "")
        self.assertEqual(pipeline._existing_media(small / "nope"), "")


class LowContentTests(unittest.TestCase):
    """P2-1：单段片头素材不得当成知识稿。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "pipeline"
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _wait(self, mgr, tid: str, timeout: float = 15.0) -> dict:
        end = time.time() + timeout
        snap: dict = {}
        while time.time() < end:
            snap = mgr.get(tid) or {}
            if snap.get("stage") in ("done", "error", "cancelled"):
                return snap
            time.sleep(0.02)
        self.fail(f"任务未在 {timeout}s 内进入终态：{snap}")

    def test_intro_only_material_is_flagged(self) -> None:
        intro = [{"start": 0.0, "end": 4.0,
                  "text": "优优独播剧场——YoYo Television Series Exclusive"}]
        mgr = pipeline.PipelineManager(
            probe_fn=lambda u: dict(INFO),
            download_fn=lambda i, w: "",
            subtitle_fn=lambda u, f: intro,
            asr_fn=lambda *a, **k: intro,
            output_root=self.root,
        )
        snap = self._wait(mgr, mgr.start("https://www.bilibili.com/video/BV1r1")["id"])
        self.assertEqual(snap["stage"], "done", snap.get("error"))
        self.assertTrue(snap["low_content"])
        self.assertEqual(snap["content_level"], "C")
        self.assertEqual(snap["insights_source"], "skipped_low_content")
        self.assertFalse(snap["summary"], "低内容素材不应产出摘要")
        self.assertFalse(snap["keywords"], "低内容素材不应产出关键词")
        md = mgr.read_artifact(snap["id"], "md") or ""
        self.assertIn("信息量不足", md)
        self.assertNotIn("## 要点", md)

    def test_normal_material_not_flagged(self) -> None:
        mgr = pipeline.PipelineManager(
            probe_fn=lambda u: dict(INFO),
            download_fn=lambda i, w: "",
            subtitle_fn=lambda u, f: [],
            asr_fn=lambda *a, **k: list(PLAIN_SEGS),
            output_root=self.root,
        )
        snap = self._wait(mgr, mgr.start("https://www.bilibili.com/video/BV1r1")["id"])
        self.assertFalse(snap["low_content"])
        self.assertTrue(snap["summary"])

    def test_sufficiency_levels(self) -> None:
        from fusion import quality
        empty = quality.content_sufficiency([], duration=120)
        self.assertFalse(empty["sufficient"])
        self.assertEqual(empty["level"], "C")
        rich = quality.content_sufficiency(PLAIN_SEGS, duration=18)
        self.assertTrue(rich["sufficient"])
        self.assertEqual(rich["level"], "A")
        self.assertTrue(quality.is_intro_noise("优优独播剧场——YoYo Television Series Exclusive"))
        self.assertFalse(quality.is_intro_noise("今天讲本地优先的知识管理"))


class DocxDeliveryTests(unittest.TestCase):
    """P2-4：DOCX 补样式表与真正的目录域。"""

    def test_styles_and_toc_field(self) -> None:
        doc = layout.build_booklet(
            "验收合集",
            [{"id": "a", "title": "甲", "body": "第一段。\n第二段。"},
             {"id": "b", "title": "乙", "body": "另一篇正文。"}],
            template="delivery",
        )
        raw = layout.render_docx(doc)
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = set(zf.namelist())
            self.assertIn("word/styles.xml", names)
            self.assertIn("word/settings.xml", names)
            styles = zf.read("word/styles.xml").decode("utf-8")
            for style_id in ('w:styleId="Heading1"', 'w:styleId="Heading2"',
                             'w:styleId="Title"', 'w:styleId="TOC1"',
                             'w:styleId="Caption"'):
                self.assertIn(style_id, styles)
            self.assertIn("eastAsia", styles)
            settings = zf.read("word/settings.xml").decode("utf-8")
            self.assertIn("updateFields", settings)
            doc_xml = zf.read("word/document.xml").decode("utf-8")
            self.assertIn("TOC \\o", doc_xml.replace("\\\\", "\\"))
            self.assertIn('w:pStyle w:val="Heading1"', doc_xml)
            self.assertIn('w:pStyle w:val="TOC1"', doc_xml)
            self.assertIn("w:dirty=\"true\"", doc_xml)
            types = zf.read("[Content_Types].xml").decode("utf-8")
            self.assertIn("/word/styles.xml", types)
            self.assertIn("/word/settings.xml", types)
            rels = zf.read("word/_rels/document.xml.rels").decode("utf-8")
            self.assertIn("styles", rels)

    def test_knowledge_docx_has_styles(self) -> None:
        meta = knowledge.meta_from_info(dict(INFO))
        raw = knowledge.render_docx(meta, PLAIN_SEGS, summary="摘要", keywords=["索引"])
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            self.assertIn("word/styles.xml", set(zf.namelist()))
            doc_xml = zf.read("word/document.xml").decode("utf-8")
            self.assertIn("Heading", doc_xml)


class RenderGoldenTests(unittest.TestCase):
    """D31：单篇与成册共用一套 LayoutDoc 渲染，并用 golden-file 锁结构。

    golden 只比对 ``word/document.xml``（与 zip 时间戳无关），时间戳统一替换为
    ``<STAMP>``。PDF 比对 Layout IR 的文本（跨平台稳定），再对字节做打开冒烟。
    """

    _STAMP = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")

    def _golden(self, name: str) -> str:
        return (ROOT / "tests" / "golden" / name).read_text(encoding="utf-8")

    def _doc_xml(self, raw: bytes) -> str:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            self.assertIsNone(zf.testzip(), "DOCX zip 完整性校验失败")
            xml = zf.read("word/document.xml").decode("utf-8")
        return self._STAMP.sub("<STAMP>", xml)

    def _footer_xml(self, raw: bytes) -> str:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            return zf.read("word/footer1.xml").decode("utf-8")

    def test_single_docx_matches_golden(self) -> None:
        meta = knowledge.meta_from_info(dict(INFO))
        raw = knowledge.render_docx(
            meta, PLAIN_SEGS, summary="摘要一句。", keywords=["索引", "知识管理"])
        self.assertEqual(self._doc_xml(raw),
                         self._golden("knowledge_docx_document.xml"))

    def test_booklet_docx_matches_golden_and_shares_style_rules(self) -> None:
        doc = layout.build_booklet(
            "验收合集",
            [{"id": "a", "title": "甲", "body": "第一段。\n第二段。"},
             {"id": "b", "title": "乙", "body": "另一篇正文。"}],
            template="delivery", stamp="2026-01-01 00:00",
        )
        xml = self._doc_xml(layout.render_docx(doc))
        self.assertEqual(xml, self._golden("booklet_docx_document.xml"))
        # 成册：章标题走 Heading1，目录域与 TOC1/TOC2 层级齐全
        for marker in ('w:pStyle w:val="Heading1"', 'w:pStyle w:val="TOC1"',
                       'w:pStyle w:val="TOC2"', "TOC \\o"):
            self.assertIn(marker, xml)
        footer = self._footer_xml(layout.render_docx(doc))
        for marker in ("PAGE", "NUMPAGES"):
            self.assertIn(marker, footer)
        # 单篇：正文分组走 Heading2，页码规则与成册同源
        single_raw = knowledge.render_docx(
            knowledge.meta_from_info(dict(INFO)), PLAIN_SEGS,
            summary="摘要一句。", keywords=["索引", "知识管理"])
        self.assertIn('w:pStyle w:val="Heading2"', self._doc_xml(single_raw))
        single_footer = self._footer_xml(single_raw)
        for marker in ("PAGE", "NUMPAGES"):
            self.assertIn(marker, single_footer)

    def test_pdf_uses_same_ir_and_opens(self) -> None:
        meta = knowledge.meta_from_info(dict(INFO))
        doc = layout.build_from_knowledge(
            meta, PLAIN_SEGS, summary="摘要一句。",
            keywords=["索引", "知识管理"], template="delivery")
        lines = self._STAMP.sub(
            "<STAMP>", "\n".join(layout.to_pdf_lines(doc)))
        self.assertEqual(lines, self._golden("knowledge_pdf_lines.txt"))
        raw = knowledge.render_pdf(
            meta, PLAIN_SEGS, summary="摘要一句。", keywords=["索引", "知识管理"])
        self.assertTrue(raw.startswith(b"%PDF-"))
        self.assertTrue(raw.rstrip().endswith(b"%%EOF"))
        self.assertIn(b"/Type /Page", raw)
        self.assertRegex(raw, rb"/Count \d+")


class SecurityHeaderTests(unittest.TestCase):
    """P2-5：HEAD 与 GET 同套安全头，Server 头不含版本号。"""

    def test_head_and_get_carry_same_headers(self) -> None:
        try:
            import http.client
            import threading
            from fusion import hardening
            import server as web
        except Exception as exc:  # pragma: no cover - 缺运行时
            self.skipTest(f"HTTP 栈不可用：{exc}")

        httpd = hardening.LimitedThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        port = httpd.server_address[1]
        th = threading.Thread(target=httpd.serve_forever, daemon=True)
        th.start()
        try:
            for method in ("GET", "HEAD"):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                conn.request(method, "/")
                resp = conn.getresponse()
                resp.read()
                headers = {k.lower(): v for k, v in resp.getheaders()}
                conn.close()
                self.assertIn("content-security-policy", headers, method)
                self.assertEqual(headers.get("x-frame-options"), "DENY", method)
                self.assertEqual(headers.get("referrer-policy"), "no-referrer", method)
                self.assertIn("permissions-policy", headers, method)
                self.assertEqual(headers.get("x-content-type-options"), "nosniff", method)
                self.assertNotIn("/", headers.get("server", ""),
                                 f"{method} 的 Server 头泄露了版本号")
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
