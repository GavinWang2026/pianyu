# Copyright (c) 2026 Hyht-Team
# SPDX-License-Identifier: MIT
# See LICENSE (MIT) and NOTICE.md (third-party components).
"""fusion.api 路由层 happy-path + 错误路径（不启真实 HTTP 端口）。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from typing import Any
from unittest.mock import patch

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

from fusion import api, library, persist  # noqa: E402


class _WFile:
    def __init__(self) -> None:
        self.buf = bytearray()

    def write(self, data: bytes) -> None:
        self.buf.extend(data)

    def flush(self) -> None:
        pass


class FakeHandler:
    def __init__(self) -> None:
        self.status: int | None = None
        self.payload: dict[str, Any] = {}
        self.headers: dict[str, str] = {}
        self._sent: list[tuple[int, dict[str, Any]]] = []
        self.wfile = _WFile()
        self._chunks: list[bytes] = []

    def _ok(self, **kwargs: Any) -> None:
        self.status = 200
        self.payload = {"ok": True, **kwargs}
        self._sent.append((200, self.payload))

    def _fail(self, message: str, status: int = 400) -> None:
        self.status = int(status)
        self.payload = {"ok": False, "error": message}
        self._sent.append((self.status, self.payload))

    def send_response(self, code: int) -> None:
        self.status = code

    def send_header(self, key: str, value: str) -> None:
        self.headers[key] = value

    def end_headers(self) -> None:
        pass

    def _write_chunk(self, data: bytes) -> None:
        self._chunks.append(data)
        self.wfile.write(data)

    def wfile_write(self, data: bytes) -> None:
        self.wfile.write(data)


class ApiRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        persist.reset_for_tests(self.root / "tasks.sqlite3")
        library.reset_for_tests(self.root / "lib.sqlite3")
        library.upsert_material(
            task_id="a1", title="转化路径讲义", body="本地优先转化路径与增长",
            tags=["增长"], collection="试用",
        )

    def tearDown(self) -> None:
        library.close()
        persist.reset_for_tests(None)
        self.tmp.cleanup()

    def test_health_and_library_get(self) -> None:
        h = FakeHandler()
        self.assertTrue(api.handle_get(h, "/api/fusion/health", {}))
        self.assertTrue(h.payload.get("ok"))
        self.assertIn("health", h.payload)

        h = FakeHandler()
        self.assertTrue(api.handle_get(h, "/api/library/search", {"q": ["转化路径"]}))
        self.assertTrue(h.payload.get("results"))

        h = FakeHandler()
        self.assertTrue(api.handle_get(h, "/api/library/collections", {}))
        self.assertTrue(any(c.get("collection") == "试用" for c in h.payload.get("collections") or []))

        h = FakeHandler()
        self.assertTrue(api.handle_get(h, "/api/library/materials", {"collection": ["试用"]}))
        self.assertEqual(len(h.payload.get("materials") or []), 1)

        h = FakeHandler()
        self.assertTrue(api.handle_get(h, "/api/diagnose/tickets", {}))
        self.assertIn("tickets", h.payload)

        h = FakeHandler()
        self.assertTrue(api.handle_get(h, "/api/storage/stats", {}))
        self.assertIn("policy", h.payload)

        h = FakeHandler()
        self.assertTrue(api.handle_get(h, "/api/updates/check", {}))
        self.assertIn("ok", h.payload)

    def test_library_ask_and_booklet(self) -> None:
        h = FakeHandler()
        self.assertTrue(api.handle_post(h, "/api/library/ask", {}))
        self.assertEqual(h.status, HTTPStatus.BAD_REQUEST)

        h = FakeHandler()
        self.assertTrue(api.handle_post(h, "/api/library/ask", {"q": "转化路径", "use_llm": False}))
        self.assertTrue(h.payload.get("ok"))
        ans = h.payload.get("answer") or ""
        self.assertTrue("转化路径" in ans or "讲义" in ans)
        self.assertTrue(h.payload.get("sources"))

        h = FakeHandler()
        self.assertTrue(api.handle_post(h, "/api/library/batch", {"action": "nope", "ids": ["a1"]}))
        self.assertEqual(h.status, HTTPStatus.BAD_REQUEST)

        h = FakeHandler()
        self.assertTrue(api.handle_post(
            h, "/api/library/batch",
            {"action": "add_tags", "ids": ["a1"], "tags": ["新"]},
        ))
        self.assertEqual(h.payload.get("updated"), 1)

        h = FakeHandler()
        self.assertTrue(api.handle_post(h, "/api/library/export-booklet", {}))
        self.assertEqual(h.status, HTTPStatus.BAD_REQUEST)

        h = FakeHandler()
        with patch("fusion.library.export_booklet", return_value={
            "collection": "试用", "count": 1, "md": str(self.root / "x.md"),
            "docx": str(self.root / "x.docx"), "pdf": str(self.root / "x.pdf"),
            "fmt": "md+docx+pdf", "template": "delivery",
        }) as mocked:
            self.assertTrue(api.handle_post(
                h, "/api/library/export-booklet",
                {"collection": "试用", "fmt": "all", "template": "delivery"},
            ))
            mocked.assert_called()
            kwargs = mocked.call_args.kwargs
            self.assertEqual(kwargs.get("fmt"), "all")
            self.assertEqual(kwargs.get("template"), "delivery")
        self.assertEqual(h.payload.get("count"), 1)
        self.assertEqual(h.payload.get("template"), "delivery")

    def test_diagnose_pack_and_storage_purge(self) -> None:
        h = FakeHandler()
        dest = self.root / "d.zip"
        with patch("fusion.diagnose_pack.build_diagnose_pack", return_value={
            "ticket_id": "AVD-TEST", "path": str(dest), "files": 2, "skipped": [],
        }):
            self.assertTrue(api.handle_post(h, "/api/diagnose/pack", {"id": ""}))
        self.assertEqual(h.payload.get("ticket_id"), "AVD-TEST")

        h = FakeHandler()
        self.assertTrue(api.handle_post(h, "/api/diagnose/submit", {}))
        self.assertEqual(h.status, HTTPStatus.BAD_REQUEST)

        h = FakeHandler()
        with patch("fusion.diagnose_pack.submit_ticket", return_value={
            "ok": True, "status": "local_only", "ticket_id": "AVD-TEST",
            "message": "未配置", "alternative": "export_zip",
        }):
            self.assertTrue(api.handle_post(
                h, "/api/diagnose/submit", {"ticket_id": "AVD-TEST"},
            ))
        self.assertEqual(h.payload.get("status"), "local_only")

        h = FakeHandler()
        with patch("fusion.recovery.scan_recoverable", return_value=[
            {"task_id": "t1", "resume_chunk": 2, "chunks_total": 5},
        ]):
            self.assertTrue(api.handle_get(h, "/api/pipeline/recoverable", {}))
        self.assertEqual(len(h.payload.get("items") or []), 1)

        h = FakeHandler()
        with patch("fusion.persist.apply_retention", return_value={
            "purged_rows": 0, "trim": {"deleted": 0, "freed": 0}, "policy": {},
        }):
            self.assertTrue(api.handle_post(h, "/api/storage/purge", {}))
        self.assertIn("trim", h.payload)

    def test_pipeline_errors_and_retry(self) -> None:
        h = FakeHandler()
        self.assertTrue(api.handle_get(h, "/api/pipeline/status", {"id": ["missing"]}))
        self.assertEqual(h.status, HTTPStatus.NOT_FOUND)

        h = FakeHandler()
        self.assertTrue(api.handle_get(h, "/api/pipeline/result", {"id": ["x"], "fmt": ["exe"]}))
        self.assertEqual(h.status, HTTPStatus.BAD_REQUEST)

        h = FakeHandler()
        self.assertTrue(api.handle_post(h, "/api/pipeline/cancel", {"id": "nope"}))
        self.assertIn("cancelled", h.payload)

        h = FakeHandler()
        self.assertTrue(api.handle_post(h, "/api/pipeline/retry", {}))
        self.assertEqual(h.status, HTTPStatus.BAD_REQUEST)

        h = FakeHandler()
        with patch.object(api.pipeline, "manager") as mgr:
            mgr.return_value.retry.side_effect = ValueError("任务不存在")
            self.assertTrue(api.handle_post(h, "/api/pipeline/retry", {"id": "abc"}))
        self.assertEqual(h.status, HTTPStatus.BAD_REQUEST)

        h = FakeHandler()
        with patch.object(api.pipeline, "manager") as mgr:
            mgr.return_value.retry.return_value = {"id": "abc", "stage": "queued"}
            self.assertTrue(api.handle_post(h, "/api/pipeline/retry", {"id": "abc"}))
        self.assertEqual(h.payload.get("task", {}).get("id"), "abc")

    def test_platform_diagnose_async_and_missing(self) -> None:
        h = FakeHandler()
        self.assertTrue(api.handle_post(h, "/api/platform/diagnose", {}))
        self.assertEqual(h.status, HTTPStatus.BAD_REQUEST)

        h = FakeHandler()
        with patch.object(api.pipeline, "diagnose_url", return_value={"ready": True}):
            self.assertTrue(api.handle_post(
                h, "/api/platform/diagnose",
                {"url": "https://www.bilibili.com/video/BV1", "async": True},
            ))
        self.assertTrue(h.payload.get("async_mode"))
        job_id = (h.payload.get("job") or {}).get("id")
        self.assertTrue(job_id)

        h = FakeHandler()
        self.assertTrue(api.handle_get(h, "/api/platform/diagnose", {"id": ["missing-job"]}))
        self.assertEqual(h.status, HTTPStatus.NOT_FOUND)

    def test_unknown_route_passthrough(self) -> None:
        h = FakeHandler()
        self.assertFalse(api.handle_get(h, "/api/not-a-route", {}))
        self.assertFalse(api.handle_post(h, "/api/not-a-route", {}))

    def test_pipeline_start_batch_and_exports(self) -> None:
        h = FakeHandler()
        with patch.object(api.pipeline, "manager") as mgr:
            mgr.return_value.start.return_value = {"id": "t1", "stage": "queued"}
            self.assertTrue(api.handle_post(
                h, "/api/pipeline/start",
                {"url": "https://www.bilibili.com/video/BV1", "want_download": "false",
                 "expand_playlist": True},
            ))
        self.assertEqual(h.payload.get("task", {}).get("id"), "t1")

        h = FakeHandler()
        with patch.object(api.pipeline, "manager") as mgr:
            mgr.return_value.start.side_effect = ValueError("域名不允许")
            self.assertTrue(api.handle_post(h, "/api/pipeline/start", {"url": "x"}))
        self.assertEqual(h.status, HTTPStatus.FORBIDDEN)

        h = FakeHandler()
        with patch.object(api.pipeline, "manager") as mgr:
            mgr.return_value.start.side_effect = RuntimeError("boom")
            self.assertTrue(api.handle_post(h, "/api/pipeline/start", {"url": "x"}))
        self.assertEqual(h.status, HTTPStatus.INTERNAL_SERVER_ERROR)

        h = FakeHandler()
        with patch.object(api.pipeline, "manager") as mgr:
            mgr.return_value.start_batch.return_value = {
                "batch_id": "b1", "tasks": [{"id": "t2"}], "not_before": None,
            }
            self.assertTrue(api.handle_post(
                h, "/api/pipeline/batch",
                {"urls": ["https://a", "https://b"], "want_download": 0, "delay_seconds": 1},
            ))
        self.assertEqual(h.payload.get("batch_id"), "b1")

        h = FakeHandler()
        with patch.object(api.pipeline, "manager") as mgr:
            mgr.return_value.start_batch.return_value = {
                "batch_id": "b2", "tasks": [], "not_before": 1.0,
            }
            with patch("fusion.scheduler.resolve_run_at", return_value={"iso": "2026-01-01T00:00:00+08:00"}):
                self.assertTrue(api.handle_post(
                    h, "/api/pipeline/batch",
                    {"text": "https://a\nhttps://b", "run_at": "明天 09:00"},
                ))
        self.assertIn("schedule", h.payload)

        h = FakeHandler()
        with patch.object(api.pipeline, "manager") as mgr:
            mgr.return_value.start_batch.side_effect = ValueError("超限")
            self.assertTrue(api.handle_post(h, "/api/pipeline/batch", {"urls": []}))
        self.assertEqual(h.status, HTTPStatus.FORBIDDEN)

        h = FakeHandler()
        with patch.object(api.pipeline, "write_obsidian", return_value={"path": "/vault/a.md"}):
            self.assertTrue(api.handle_post(
                h, "/api/pipeline/export-obsidian", {"id": "t1", "path": "/vault"},
            ))
        self.assertEqual(h.payload.get("path"), "/vault/a.md")

        h = FakeHandler()
        with patch.object(api.pipeline, "write_obsidian", side_effect=ValueError("无产物")):
            self.assertTrue(api.handle_post(h, "/api/pipeline/export-obsidian", {"id": "t1"}))
        self.assertEqual(h.status, HTTPStatus.BAD_REQUEST)

        h = FakeHandler()
        self.assertTrue(api.handle_post(h, "/api/pipeline/export-push", {"id": "t1", "target": "x"}))
        self.assertEqual(h.status, HTTPStatus.BAD_REQUEST)

        h = FakeHandler()
        with patch.object(api.pipeline, "push_export", return_value={"ok": True, "url": "u"}):
            self.assertTrue(api.handle_post(
                h, "/api/pipeline/export-push", {"id": "t1", "target": "notion"},
            ))
        self.assertEqual(h.payload.get("target"), "notion")

        h = FakeHandler()
        with patch.object(api.pipeline, "push_export", side_effect=RuntimeError("net")):
            self.assertTrue(api.handle_post(
                h, "/api/pipeline/export-push", {"id": "t1", "target": "feishu"},
            ))
        self.assertEqual(h.status, HTTPStatus.INTERNAL_SERVER_ERROR)

        h = FakeHandler()
        self.assertTrue(api.handle_post(
            h, "/api/pipeline/export-clip", {"id": "t1", "start": "bad", "end": 1},
        ))
        self.assertEqual(h.status, HTTPStatus.BAD_REQUEST)

        h = FakeHandler()
        with patch.object(api.pipeline, "export_clip", return_value={"path": "c.mp4"}):
            self.assertTrue(api.handle_post(
                h, "/api/pipeline/export-clip",
                {"id": "t1", "start": 1, "end": 5, "reencode": True},
            ))
        self.assertEqual(h.payload.get("path"), "c.mp4")

        h = FakeHandler()
        with patch.object(api.pipeline, "export_clip", side_effect=ValueError("无媒体")):
            self.assertTrue(api.handle_post(
                h, "/api/pipeline/export-clip", {"id": "t1", "start": 0, "end": 1},
            ))
        self.assertEqual(h.status, HTTPStatus.BAD_REQUEST)

        h = FakeHandler()
        with patch.object(api.pipeline, "export_clips_by_chapters", return_value={"clips": 2}):
            self.assertTrue(api.handle_post(
                h, "/api/pipeline/export-chapters", {"id": "t1", "reencode": "yes"},
            ))
        self.assertEqual(h.payload.get("clips"), 2)

        h = FakeHandler()
        with patch.object(api.pipeline, "export_clips_by_chapters", side_effect=ValueError("无章节")):
            self.assertTrue(api.handle_post(h, "/api/pipeline/export-chapters", {"id": "t1"}))
        self.assertEqual(h.status, HTTPStatus.BAD_REQUEST)

        h = FakeHandler()
        self.assertTrue(api.handle_post(h, "/api/pipeline/proofread", {"id": "t1"}))
        self.assertEqual(h.status, HTTPStatus.BAD_REQUEST)

        h = FakeHandler()
        with patch.object(api.pipeline, "manager") as mgr:
            mgr.return_value.proofread.return_value = {"id": "t1", "stage": "done"}
            self.assertTrue(api.handle_post(
                h, "/api/pipeline/proofread",
                {"id": "t1", "segments": [{"text": "hi"}], "strict": True},
            ))
        self.assertEqual(h.payload.get("task", {}).get("id"), "t1")

        h = FakeHandler()
        with patch.object(api.pipeline, "manager") as mgr:
            mgr.return_value.proofread.side_effect = ValueError("空")
            self.assertTrue(api.handle_post(
                h, "/api/pipeline/proofread", {"id": "t1", "segments": []},
            ))
        self.assertEqual(h.status, HTTPStatus.BAD_REQUEST)

    def test_get_tasks_status_result_and_limits(self) -> None:
        h = FakeHandler()
        with patch.object(api.pipeline, "manager") as mgr:
            mgr.return_value.list_tasks.return_value = [{"id": "t1"}]
            self.assertTrue(api.handle_get(h, "/api/pipeline/tasks", {}))
        self.assertEqual(len(h.payload.get("tasks") or []), 1)

        h = FakeHandler()
        with patch.object(api.pipeline, "manager") as mgr:
            mgr.return_value.get.return_value = {"id": "t1", "stage": "done"}
            self.assertTrue(api.handle_get(h, "/api/pipeline/status", {"id": ["t1"]}))
        self.assertEqual(h.payload.get("task", {}).get("stage"), "done")

        h = FakeHandler()
        with patch.object(api.pipeline, "manager") as mgr:
            mgr.return_value.read_artifact.return_value = "# hi"
            self.assertTrue(api.handle_get(
                h, "/api/pipeline/result", {"id": ["t1"], "fmt": ["md"]},
            ))
        self.assertEqual(h.payload.get("content"), "# hi")

        h = FakeHandler()
        with patch.object(api.pipeline, "manager") as mgr:
            mgr.return_value.read_artifact.return_value = None
            self.assertTrue(api.handle_get(
                h, "/api/pipeline/result", {"id": ["t1"], "fmt": ["md"]},
            ))
        self.assertEqual(h.status, HTTPStatus.NOT_FOUND)

        h = FakeHandler()
        self.assertTrue(api.handle_get(h, "/api/library/search", {"q": ["x"], "limit": ["bad"]}))
        self.assertTrue(h.payload.get("ok"))

        h = FakeHandler()
        self.assertTrue(api.handle_get(
            h, "/api/library/materials", {"collection": ["试用"], "limit": ["xx"]},
        ))
        self.assertTrue(h.payload.get("ok"))

        h = FakeHandler()
        self.assertTrue(api.handle_get(h, "/api/diagnose/tickets", {"limit": ["nope"]}))
        self.assertIn("tickets", h.payload)

    def test_library_batch_variants_and_errors(self) -> None:
        h = FakeHandler()
        self.assertTrue(api.handle_post(
            h, "/api/library/batch", {"action": "set_collection", "ids": "a1"},
        ))
        self.assertEqual(h.status, HTTPStatus.BAD_REQUEST)

        h = FakeHandler()
        self.assertTrue(api.handle_post(
            h, "/api/library/batch",
            {"action": "set_collection", "ids": ["a1"], "collection": "新合集"},
        ))
        self.assertEqual(h.payload.get("updated"), 1)

        h = FakeHandler()
        self.assertTrue(api.handle_post(
            h, "/api/library/batch",
            {"action": "add_tags", "ids": ["a1"], "tags": "bad"},
        ))
        self.assertEqual(h.status, HTTPStatus.BAD_REQUEST)

        h = FakeHandler()
        with patch("fusion.library.auto_cluster_by_chapters", return_value={"updated": 0, "clusters": []}):
            self.assertTrue(api.handle_post(
                h, "/api/library/batch", {"action": "auto_cluster", "ids": []},
            ))
        self.assertEqual(h.payload.get("action"), "auto_cluster")

        h = FakeHandler()
        with patch("fusion.library.export_booklet", side_effect=ValueError("空合集")):
            self.assertTrue(api.handle_post(
                h, "/api/library/export-booklet", {"collection": "试用"},
            ))
        self.assertEqual(h.status, HTTPStatus.BAD_REQUEST)

        h = FakeHandler()
        with patch("fusion.diagnose_pack.build_diagnose_pack", side_effect=RuntimeError("io")):
            self.assertTrue(api.handle_post(h, "/api/diagnose/pack", {}))
        self.assertEqual(h.status, HTTPStatus.INTERNAL_SERVER_ERROR)

        h = FakeHandler()
        with patch("fusion.persist.apply_retention", side_effect=RuntimeError("disk")):
            self.assertTrue(api.handle_post(h, "/api/storage/purge", {}))
        self.assertEqual(h.status, HTTPStatus.INTERNAL_SERVER_ERROR)

        h = FakeHandler()
        with patch.object(api.pipeline, "manager") as mgr:
            mgr.return_value.retry.side_effect = RuntimeError("fail")
            self.assertTrue(api.handle_post(h, "/api/pipeline/retry", {"id": "abc"}))
        self.assertEqual(h.status, HTTPStatus.INTERNAL_SERVER_ERROR)

    def test_diagnose_sync_and_sse_progress(self) -> None:
        h = FakeHandler()
        with patch.object(api.pipeline, "diagnose_url", return_value={"ready": True, "platform": "bilibili"}):
            self.assertTrue(api.handle_post(
                h, "/api/platform/diagnose",
                {"url": "https://www.bilibili.com/video/BV1"},
            ))
        self.assertTrue(h.payload.get("ready"))

        h = FakeHandler()
        with patch.object(api.pipeline, "diagnose_url", side_effect=RuntimeError("down")):
            self.assertTrue(api.handle_post(
                h, "/api/platform/diagnose",
                {"url": "https://www.bilibili.com/video/BV1"},
            ))
        self.assertEqual(h.status, HTTPStatus.INTERNAL_SERVER_ERROR)

        h = FakeHandler()
        with patch.object(api.pipeline, "manager") as mgr:
            mgr.return_value.get.side_effect = [
                {"id": "t1", "stage": "asr", "percent": 10},
                {"id": "t1", "stage": "done", "percent": 100},
            ]
            with patch("fusion.api.time.sleep", return_value=None):
                self.assertTrue(api.handle_get(h, "/api/pipeline/progress", {"id": ["t1"]}))
        self.assertEqual(h.status, 200)
        self.assertTrue(any(b"event: end" in c for c in h._chunks))

        h = FakeHandler()
        with patch.object(api.pipeline, "manager") as mgr:
            mgr.return_value.get.return_value = None
            self.assertTrue(api.handle_get(h, "/api/pipeline/progress", {"id": ["gone"]}))
        self.assertEqual(h.status, HTTPStatus.NOT_FOUND)


if __name__ == "__main__":
    unittest.main(verbosity=2)
