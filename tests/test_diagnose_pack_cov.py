# Copyright (c) 2026 Hyht-Team. All rights reserved.
# See LICENSE (proprietary) and NOTICE.md (third-party components).
"""诊断包模块的覆盖补齐（P2-2 · CI 门禁转绿）。

第九轮报告指出 CI 覆盖率门禁自身为红：``diagnose_pack`` 67.4% < 70%。
本文件按模块的真实分支补测：路径白名单、瘦身/全量两档、日志收集、
工单列表、支持端点校验、multipart 上传与失败回执。
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
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

from fusion import diagnose_pack  # noqa: E402


class _UploadHandler(BaseHTTPRequestHandler):
    status = 200
    body = b'{"ok": true, "receipt": "RCPT-12345"}'

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args) -> None:  # 静音
        return


class SafeIdTests(unittest.TestCase):
    def test_rejects_traversal_and_accepts_normal(self) -> None:
        for bad in ("", "..", "../cookies", "a/b", "a\\b", ".hidden", "x" * 80):
            self.assertEqual(diagnose_pack._safe_task_id(bad), "", bad)
        for good in ("abc123", "t-1_2.3", "A" * 40):
            self.assertEqual(diagnose_pack._safe_task_id(good), good)

    def test_task_file_whitelist(self) -> None:
        base = Path(tempfile.mkdtemp(prefix="avd-dp-"))
        allowed = [base / "a.md", base / "b.srt", base / "c.json",
                   base / "asr-ckpt-1.json", base / "asr-crash-9.json"]
        denied = [base / "config.json", base / "cookies.txt", base / "api_key.txt",
                  base / "token.json", base / "movie.mp4", base / "photo.png"]
        for path in allowed:
            path.write_text("x", encoding="utf-8")
            self.assertTrue(diagnose_pack._allowed_task_file(path), path.name)
        for path in denied:
            path.write_text("x", encoding="utf-8")
            self.assertFalse(diagnose_pack._allowed_task_file(path), path.name)


class BuildPackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="avd-dp-"))
        self.outputs = self.base / "outputs"
        self.task = self.outputs / "task-abc"
        (self.task / "sub").mkdir(parents=True)
        (self.task / "meta.json").write_text(
            json.dumps({"title": "诊断样本"}), encoding="utf-8")
        (self.task / "note.md").write_text("正文" * 1200, encoding="utf-8")
        (self.task / "asr-ckpt-1.json").write_text("{}", encoding="utf-8")
        (self.task / "asr-crash-a.json").write_text("{}", encoding="utf-8")
        (self.task / "audio.m4a").write_bytes(b"0" * 2048)
        (self.task / "cookies.txt").write_text("sessionid=abcdefghij",
                                              encoding="utf-8")
        (self.task / "sub" / "chapter.md").write_text("子目录", encoding="utf-8")
        self.logs = self.base / "logs"
        self.logs.mkdir()
        (self.logs / "asr-fail-1.log").write_text("boom", encoding="utf-8")
        self.dest = self.base / "pack.zip"

    def _build(self, **kwargs):
        params = dict(
            task_id="task-abc",
            dest=self.dest,
            outputs_root=self.outputs,
            log_dir=self.logs,
        )
        params.update(kwargs)
        return diagnose_pack.build_diagnose_pack(**params)

    def test_slim_pack_contents(self) -> None:
        info = self._build()
        self.assertTrue(info["path"].endswith("pack.zip"))
        self.assertGreater(info["bytes"], 0)
        self.assertEqual(info["task_id_accepted"], "task-abc")
        self.assertTrue(info["ticket_id"].startswith("AVD-"))
        with zipfile.ZipFile(self.dest) as zf:
            names = set(zf.namelist())
        self.assertIn("env.json", names)
        self.assertIn("RECEIPT.txt", names)
        self.assertIn("logs/asr-fail-1.log", names)
        self.assertIn("task/meta.json", names)
        self.assertIn("task/asr-ckpt-1.json", names)
        self.assertIn("task/asr-crash-a.json", names)
        self.assertTrue(any(n.startswith("task/note.md.preview") for n in names))
        # 瘦身档：不带子目录正文、不带媒体、不带 cookie
        self.assertNotIn("task/sub/chapter.md", names)
        self.assertFalse(any("cookie" in n.lower() for n in names))
        self.assertFalse(any(n.endswith(".m4a") for n in names))

    def test_full_artifacts_adds_files_and_skips_secrets(self) -> None:
        info = self._build(include_full_artifacts=True)
        with zipfile.ZipFile(self.dest) as zf:
            names = set(zf.namelist())
        self.assertIn("task/sub/chapter.md", names)
        self.assertFalse(any("cookie" in n.lower() for n in names))
        self.assertTrue(info["skipped"])

    def test_illegal_task_id_is_skipped_not_crashed(self) -> None:
        info = self._build(task_id="../cookies")
        self.assertEqual(info["task_id_accepted"], "")
        self.assertTrue(any("拒绝非法 task_id" in s for s in info["skipped"]))
        with zipfile.ZipFile(self.dest) as zf:
            self.assertNotIn("task/meta.json", set(zf.namelist()))

    def test_missing_task_dir_is_reported(self) -> None:
        info = self._build(task_id="nosuchtask")
        self.assertTrue(any("任务目录不存在" in s for s in info["skipped"]))

    def test_receipt_file_written(self) -> None:
        info = self._build()
        receipt = diagnose_pack.list_tickets(limit=5)
        self.assertTrue(any(r["ticket_id"] == info["ticket_id"] for r in receipt))
        row = next(r for r in receipt if r["ticket_id"] == info["ticket_id"])
        self.assertTrue(row["exists"])
        self.assertEqual(row["submit_status"], "local")

    def test_list_tickets_limit_and_empty(self) -> None:
        self.assertEqual(diagnose_pack.list_tickets(limit=0)[:0], [])
        self._build()
        self.assertTrue(diagnose_pack.list_tickets(limit=1))


class SupportEndpointTests(unittest.TestCase):
    def test_support_url_validation(self) -> None:
        self.assertEqual(diagnose_pack._support_url(""), "")
        with self.assertRaises(ValueError):
            diagnose_pack._support_url("http://example.com/hook")
        with self.assertRaises(ValueError):
            diagnose_pack._support_url("ftp://example.com/hook")

    def test_submit_rejects_bad_ticket_and_missing_receipt(self) -> None:
        with self.assertRaises(ValueError):
            diagnose_pack.submit_ticket("nope")
        with self.assertRaises(ValueError):
            diagnose_pack.submit_ticket("AVD-00000000-0000001")


class SubmitTicketTests(unittest.TestCase):
    """multipart 上传闭环：成功与 HTTP 失败两条路径。"""

    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="avd-dp-"))
        self.outputs = self.base / "outputs"
        (self.outputs / "task-xyz").mkdir(parents=True)
        self.dest = self.base / "pack.zip"
        self.info = diagnose_pack.build_diagnose_pack(
            task_id="task-xyz", dest=self.dest,
            outputs_root=self.outputs, log_dir=self.base / "nologs",
        )
        self._orig = diagnose_pack._support_url
        _UploadHandler.status = 200
        _UploadHandler.body = b'{"ok": true, "receipt": "RCPT-12345"}'
        self.httpd = HTTPServer(("127.0.0.1", 0), _UploadHandler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        diagnose_pack._support_url = lambda explicit=None: (
            explicit or f"http://127.0.0.1:{self.port}/upload")

    def tearDown(self) -> None:
        diagnose_pack._support_url = self._orig
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_no_endpoint_returns_local_only(self) -> None:
        diagnose_pack._support_url = lambda explicit=None: ""
        out = diagnose_pack.submit_ticket(self.info["ticket_id"])
        self.assertEqual(out["status"], "local_only")
        self.assertEqual(out["alternative"], "export_zip")

    def test_submit_success_updates_receipt(self) -> None:
        out = diagnose_pack.submit_ticket(self.info["ticket_id"])
        self.assertEqual(out["status"], "submitted")
        self.assertEqual(out["remote_receipt"], "RCPT-12345")
        row = next(r for r in diagnose_pack.list_tickets(limit=50)
                   if r["ticket_id"] == self.info["ticket_id"])
        self.assertEqual(row["submit_status"], "submitted")
        self.assertTrue(row["submitted_at"])

    def test_submit_http_error_raises(self) -> None:
        _UploadHandler.status = 500
        _UploadHandler.body = b'{"error": "boom"}'
        with self.assertRaises(RuntimeError):
            diagnose_pack.submit_ticket(self.info["ticket_id"])

    def test_submit_missing_zip_raises(self) -> None:
        self.dest.unlink()
        with self.assertRaises(ValueError):
            diagnose_pack.submit_ticket(self.info["ticket_id"])

    def test_submit_corrupted_receipt_raises(self) -> None:
        receipt = (diagnose_pack.APP_DIR / "outputs" / "diagnose"
                   / f"{self.info['ticket_id']}.receipt.json")
        receipt.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            diagnose_pack.submit_ticket(self.info["ticket_id"])


class EnvSectionTests(unittest.TestCase):
    def test_pack_records_stage_machine_and_health(self) -> None:
        base = Path(tempfile.mkdtemp(prefix="avd-dp-"))
        dest = base / "p.zip"
        info = diagnose_pack.build_diagnose_pack(
            dest=dest, outputs_root=base, log_dir=base / "nologs")
        with zipfile.ZipFile(dest) as zf:
            env = json.loads(zf.read("env.json").decode("utf-8"))
        self.assertIn("python", env)
        self.assertIn("ticket_id", env)
        self.assertEqual(env["task_id_accepted"], "")
        self.assertIn("stage_machine", env)
        self.assertTrue(Path(info["path"]).is_file())


if __name__ == "__main__":
    unittest.main()
