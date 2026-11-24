# Copyright (c) 2026 Hyht-Team
# SPDX-License-Identifier: MIT
# See LICENSE (MIT) and NOTICE.md (third-party components).
"""切片导出与三方分发的覆盖补齐（P2-2 · 补测空白区）。

报告点名的空白区：clips 32%、exporters 42%。两者都是「只在本机某条命令 /
某个 webhook 上才会跑到」的代码，这里用假 ffmpeg 与假 HTTP 客户端覆盖，
不联网、不依赖真实凭据。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
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

from fusion import clips, exporters  # noqa: E402


class ClipExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="avd-clip-"))
        self.src = self.base / "in.mp4"
        self.src.write_bytes(b"0" * 4096)
        self.dest = self.base / "out" / "clip.mp4"
        self._orig_ffmpeg = clips._ffmpeg
        self._orig_run = clips.subprocess.run
        clips._ffmpeg = lambda: "ffmpeg"

    def tearDown(self) -> None:
        clips._ffmpeg = self._orig_ffmpeg
        clips.subprocess.run = self._orig_run

    def _fake_run(self, *, rc: int = 0, mode: str = "always") -> list[list[str]]:
        calls: list[list[str]] = []

        def run(cmd, **kwargs):
            calls.append(list(cmd))
            copy_mode = "-c" in cmd and "copy" in cmd
            ok = rc == 0 and (mode == "always" or (mode == "copy_only" and copy_mode))
            if ok:
                out = Path(cmd[-1])
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"1" * 128)
            return subprocess.CompletedProcess(cmd, 0 if ok else 1, "", "err-line")

        clips.subprocess.run = run
        return calls

    def test_copy_mode_success(self) -> None:
        calls = self._fake_run(mode="copy_only")
        out = clips.export_clip(self.src, start=1, end=3, dest=self.dest)
        self.assertEqual(out["mode"], "copy")
        self.assertEqual(out["start"], 1.0)
        self.assertEqual(out["end"], 3.0)
        self.assertTrue(Path(out["path"]).is_file())
        self.assertEqual(len(calls), 1)
        self.assertIn("copy", calls[0])

    def test_fallback_to_reencode(self) -> None:
        calls = self._fake_run(mode="never", rc=1)
        with self.assertRaises(RuntimeError):
            clips.export_clip(self.src, start=0, end=2, dest=self.dest)
        self.assertEqual(len(calls), 2, "流复制失败后必须尝试重编码")
        self.assertIn("libx264", " ".join(calls[1]))

    def test_forced_reencode(self) -> None:
        calls = self._fake_run(mode="always")
        out = clips.export_clip(self.src, start=0, end=2, dest=self.dest, reencode=True)
        self.assertEqual(out["mode"], "reencode")
        self.assertEqual(len(calls), 1)

    def test_validation_errors(self) -> None:
        with self.assertRaises(ValueError):
            clips.export_clip(self.base / "nope.mp4", start=0, end=1, dest=self.dest)
        with self.assertRaises(ValueError):
            clips.export_clip(self.src, start="x", end=1, dest=self.dest)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            clips.export_clip(self.src, start=1, end=1.1, dest=self.dest)
        with self.assertRaises(ValueError):
            clips.export_clip(self.src, start=0, end=700, dest=self.dest)

    def test_negative_start_is_clamped(self) -> None:
        self._fake_run(mode="copy_only")
        out = clips.export_clip(self.src, start=-5, end=2, dest=self.dest)
        self.assertEqual(out["start"], 0.0)

    def test_missing_ffmpeg_raises(self) -> None:
        clips._ffmpeg = lambda: (_ for _ in ()).throw(RuntimeError("未找到 ffmpeg"))
        with self.assertRaises(RuntimeError):
            clips.export_clip(self.src, start=0, end=2, dest=self.dest)


class ExporterTests(unittest.TestCase):
    """三方分发：用假 _post_json 覆盖分块/续写/截断三条路径。"""

    def setUp(self) -> None:
        self._orig_post = exporters._post_json
        self._orig_safe = exporters._safe_https
        self.calls: list[tuple[str, dict]] = []

    def tearDown(self) -> None:
        exporters._post_json = self._orig_post
        exporters._safe_https = self._orig_safe

    def _patch(self, *, page_id: str = "page-1", fail_append: bool = False,
               raiser: Exception | None = None):
        def fake_post(url, payload, *, headers=None, timeout=30.0):
            self.calls.append((url, payload))
            if raiser is not None:
                raise raiser
            if url.endswith("/children") and fail_append:
                raise RuntimeError("append failed")
            if url.endswith("/pages"):
                return {"ok": True, "status": 200, "body": {"id": page_id}}
            return {"ok": True, "status": 200, "body": {}}

        exporters._post_json = fake_post

    def test_notion_single_batch(self) -> None:
        self._patch()
        out = exporters.push_notion(
            token="tok", database_id="a" * 32, title="标题", markdown="正文")
        self.assertEqual(out["target"], "notion")
        self.assertEqual(out["page_id"], "page-1")
        self.assertEqual(out["chunks_total"], 1)
        self.assertFalse(out["truncated"])
        self.assertEqual(len(self.calls), 1)
        payload = self.calls[0][1]
        self.assertEqual(payload["parent"]["database_id"], "a" * 32)
        self.assertIn("title", payload["properties"]["Name"])

    def test_notion_multi_batch_appends(self) -> None:
        self._patch()
        markdown = "a" * 2000 * 120  # 120 块
        out = exporters.push_notion(
            token="tok", database_id="db-uuid-with-dashes",
            title="长文", markdown=markdown)
        self.assertEqual(out["chunks_total"], 120)
        self.assertEqual(out["chunks_pushed"], 120)
        self.assertFalse(out["truncated"])
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(self.calls[1][0].endswith("/children"))
        self.assertEqual(out["raw"]["status"], 200)

    def test_notion_append_failure_is_recorded(self) -> None:
        self._patch(fail_append=True)
        out = exporters.push_notion(
            token="tok", database_id="b" * 32, title="长文",
            markdown="a" * 2000 * 100)
        self.assertTrue(out["truncated"])
        self.assertTrue(out["append_errors"])
        self.assertIn("续写中断", out["message"])

    def test_notion_requires_token_and_db(self) -> None:
        with self.assertRaises(ValueError):
            exporters.push_notion(token="", database_id="x", title="t", markdown="m")
        with self.assertRaises(ValueError):
            exporters.push_notion(token="t", database_id="", title="t", markdown="m")

    def test_notion_empty_markdown_uses_placeholder(self) -> None:
        self._patch()
        out = exporters.push_notion(token="t", database_id="c" * 32, title="t",
                                    markdown="")
        self.assertEqual(out["chunks_total"], 1)
        body = self.calls[0][1]
        self.assertIn("（空文稿）", json.dumps(body, ensure_ascii=False))

    def test_feishu_card_with_link(self) -> None:
        self._patch()
        exporters._safe_https = lambda url: url
        out = exporters.push_feishu(
            webhook="https://open.feishu.cn/hook",
            title="标题",
            markdown="正文" * 10,
            fulltext_link="https://example.com/a.md",
        )
        self.assertEqual(out["target"], "feishu")
        self.assertEqual(out["chunks_total"], 1)
        self.assertFalse(out["truncated"])
        elements = self.calls[0][1]["card"]["elements"]
        self.assertTrue(any("打开全文" in json.dumps(e, ensure_ascii=False)
                            for e in elements))

    def test_feishu_truncates_beyond_card_limit(self) -> None:
        self._patch()
        exporters._safe_https = lambda url: url
        out = exporters.push_feishu(
            webhook="https://open.feishu.cn/hook",
            title="长文",
            markdown="b" * 3200 * 12,
        )
        self.assertEqual(out["chunks_total"], 12)
        self.assertEqual(out["chunks_pushed"], 8)
        self.assertTrue(out["truncated"])
        self.assertIn("卡片截断", out["message"])
        elements = self.calls[0][1]["card"]["elements"]
        self.assertTrue(any("另有 4 块未推送" in json.dumps(e, ensure_ascii=False)
                            for e in elements))

    def test_feishu_rejects_non_https(self) -> None:
        with self.assertRaises(ValueError):
            exporters.push_feishu(webhook="http://127.0.0.1/hook",
                                  title="t", markdown="m")

    def test_push_from_config_dispatch(self) -> None:
        self._patch()
        exporters._safe_https = lambda url: url
        out = exporters.push_from_config(
            "lark", title="t", markdown="m", cfg={"feishu_webhook": "https://a/b"})
        self.assertEqual(out["target"], "feishu")
        out = exporters.push_from_config(
            "notion", title="t", markdown="m",
            cfg={"notion_token": "tok", "notion_database_id": "d" * 32})
        self.assertEqual(out["target"], "notion")
        with self.assertRaises(ValueError):
            exporters.push_from_config("slack", title="t", markdown="m", cfg={})

    def test_post_json_error_paths(self) -> None:
        import urllib.error
        from unittest import mock

        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("boom")):
            with self.assertRaises(RuntimeError):
                exporters._post_json("https://example.com", {"a": 1})

        class _Resp:
            status = 200

            def read(self) -> bytes:
                return b"plain text"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch("urllib.request.urlopen", return_value=_Resp()):
            out = exporters._post_json("https://example.com", {"a": 1})
        self.assertTrue(out["ok"])
        self.assertEqual(out["body"], "plain text")


if __name__ == "__main__":
    unittest.main()
