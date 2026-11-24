# Copyright (c) 2026 Hyht-Team
# SPDX-License-Identifier: MIT
"""ASR 常驻池：取消 / 心跳 / 排队语义回归（不启真实 Whisper）。"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "asr_pool", APP_DIR / "downloader" / "asr_pool.py",
)
assert _spec and _spec.loader
asr_pool = importlib.util.module_from_spec(_spec)  # noqa: N816
_spec.loader.exec_module(asr_pool)


class AsrPoolCancelTests(unittest.TestCase):
    def setUp(self) -> None:
        # 每测独立池，避免全局单例串扰
        self.pool = asr_pool.AsrWorkerPool()

    def test_cancel_queued_does_not_kill_active(self) -> None:
        """P0-2：取消排队任务只置标记，不杀持有者。"""
        self.pool._active_task_id = "A"
        self.pool._proc = mock.Mock()
        self.pool._proc.poll.return_value = None
        self.pool._ready = True

        result = self.pool.request_cancel("B")
        self.assertEqual(result, "marked")
        self.pool._proc.kill.assert_not_called()
        self.assertTrue(self.pool.cancel_flag("B").is_set())
        self.assertEqual(self.pool.active_task_id, "A")

    def test_cancel_active_soft_first(self) -> None:
        """P2：取消持有者先软取消（写哨兵），不立刻 kill，保留热模型。"""
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.stdin = mock.Mock()
        self.pool._proc = proc
        self.pool._ready = True
        self.pool._active_task_id = "A"

        result = self.pool.request_cancel("A", work_dir="")
        self.assertEqual(result, "soft")
        proc.kill.assert_not_called()
        self.assertIs(self.pool._proc, proc)
        self.assertTrue(self.pool.cancel_flag("A").is_set())

    def test_run_job_raises_if_cancelled_while_queued(self) -> None:
        """排队中取消 → JobCancelled，且不写 stdin。"""
        # 占住 job_lock，模拟 A 正在跑
        self.assertTrue(self.pool._job_lock.acquire(blocking=False))
        self.pool._ready = True
        self.pool._proc = mock.Mock()
        self.pool._proc.poll.return_value = None
        self.pool._proc.stdin = mock.Mock()

        def cancel_soon() -> None:
            time.sleep(0.2)
            self.pool.request_cancel("B")

        threading.Thread(target=cancel_soon, daemon=True).start()
        with self.assertRaises(asr_pool.JobCancelled):
            self.pool.run_job(
                {"source": "/tmp/x"}, lambda ev: None, task_id="B",
            )
        self.pool._proc.stdin.write.assert_not_called()
        self.pool._job_lock.release()

    def test_heartbeat_timeout(self) -> None:
        """超时无事件 → WorkerHeartbeatError（含 degraded）。"""
        original = asr_pool.HEARTBEAT_SECONDS
        asr_pool.HEARTBEAT_SECONDS = 0.3
        try:
            proc = mock.Mock()
            proc.poll.return_value = None
            proc.stdin = mock.Mock()
            self.pool._proc = proc
            self.pool._ready = True
            self.pool._line_q = __import__("queue").Queue()

            with self.assertRaises(asr_pool.WorkerHeartbeatError) as ctx:
                self.pool.run_job(
                    {"source": "/tmp/x", "task_id": "H"},
                    lambda ev: None,
                    task_id="H",
                )
            self.assertIn("心跳", str(ctx.exception))
            self.assertIn("心跳", ctx.exception.degraded)
        finally:
            asr_pool.HEARTBEAT_SECONDS = original

    def test_queue_snapshot_position_and_eta(self) -> None:
        self.pool._active_task_id = "A"
        self.pool._active_duration = 100.0
        self.pool._waiting = ["B", "C"]
        snap = self.pool.queue_snapshot("B")
        self.assertEqual(snap["position"], 1)
        self.assertAlmostEqual(snap["eta_seconds"], 100 * 0.44, places=1)
        snap_c = self.pool.queue_snapshot("C")
        self.assertEqual(snap_c["position"], 2)
        self.assertGreater(snap_c["eta_seconds"], snap["eta_seconds"])


class AsrPoolHandoffTests(unittest.TestCase):
    """上一单取消后的「尾巴」不能毒害下一单。

    真机验证抓出来的：取消时 ``run_job`` 读到 ``cancelled`` 就抛出，worker
    之后补发的 ``job_end{cancelled:true}`` 留在共享队列里；下一个任务一读
    就把它当成自己的结束信号 —— 表现是「新任务刚排队就被判已取消」。
    """

    def setUp(self) -> None:
        import queue

        self.pool = asr_pool.AsrWorkerPool()
        self.pool._line_q = queue.Queue()
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.stdin = mock.Mock()
        self.pool._proc = proc
        self.pool._ready = True
        # 只在真出问题时才等 3s，而不是 120s
        self._hb = asr_pool.HEARTBEAT_SECONDS
        asr_pool.HEARTBEAT_SECONDS = 3.0

    def tearDown(self) -> None:
        asr_pool.HEARTBEAT_SECONDS = self._hb

    def _feed_later(self, *lines: str) -> None:
        """等 drain 之后再喂事件 —— 否则会被当成上一单的残留清掉。"""
        import time

        def _run() -> None:
            time.sleep(0.6)
            for line in lines:
                self.pool._line_q.put(line)

        threading.Thread(target=_run, daemon=True).start()

    def test_drain_pending_drops_stale_lines(self) -> None:
        self.pool._line_q.put('{"type":"job_end","cancelled":true}\n')
        self.pool._line_q.put('{"type":"segment"}\n')
        self.assertEqual(self.pool.drain_pending(), 2)
        self.assertTrue(self.pool._line_q.empty())

    def test_drain_pending_keeps_eof_sentinel(self) -> None:
        """EOF 代表 worker 进程没了，丢了就再也发现不了。"""
        self.pool._line_q.put(None)
        self.assertEqual(self.pool.drain_pending(), 0)
        self.assertIsNone(self.pool._line_q.get_nowait())

    def test_stale_job_end_does_not_cancel_next_job(self) -> None:
        # 队列里先躺着上一单的收尾事件
        self.pool._line_q.put('{"type":"job_end","cancelled":true}\n')
        self._feed_later(
            '{"type":"done","segment_count":2}\n',
            '{"type":"job_end","ok":true}\n',
        )
        seen: list[str] = []
        # 不该抛 JobCancelled
        self.pool.run_job(
            {"source": "/tmp/x", "task_id": "next"},
            lambda ev: seen.append(str(ev.get("type"))),
            task_id="next",
        )
        self.assertIn("done", seen)
        self.assertIsNone(self.pool.active_task_id)

    def test_run_job_drains_on_exit(self) -> None:
        """正常跑完后队列里也不留下这一单的尾巴。"""
        self._feed_later(
            '{"type":"done","segment_count":1}\n',
            '{"type":"job_end","ok":true}\n',
            '{"type":"segment","segment":{"text":"尾"}}\n',
        )
        self.pool.run_job({"source": "/tmp/x"}, lambda ev: None, task_id="t")
        self.assertTrue(self.pool._line_q.empty())


class WorkerPathBootstrapTests(unittest.TestCase):
    """worker 必须能 import 主包的检查点模块。

    worker 跑在 ``.venv-asr``，脚本在 ``tools/``，主包根不在 sys.path 上；
    缺这层 bootstrap 时 ``from fusion import checkpoint`` 全都会 ImportError，
    而调用点都包在 ``except Exception: pass`` 里 —— 续跑与取消哨兵整条链路
    静默失效，UI 上却一切正常。
    """

    _MOD = None

    @classmethod
    def setUpClass(cls) -> None:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "asr_worker_pathprobe", APP_DIR / "tools" / "asr_worker.py")
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        cls._MOD = mod

    def test_app_root_on_sys_path(self) -> None:
        self.assertEqual(Path(self._MOD._APP_ROOT).resolve(), APP_DIR.resolve())
        self.assertIn(str(APP_DIR), sys.path)

    def test_ckpt_probe_ok(self) -> None:
        mod = self._MOD
        mod._CKPT_PROBED = False
        mod._CKPT_ERROR = ""
        self.assertTrue(mod._ckpt_probe())
        self.assertEqual(mod._CKPT_ERROR, "")

    def test_ckpt_probe_reports_failure(self) -> None:
        """导入失败时必须返回 False 并留下原因（不能静默）。"""
        mod = self._MOD
        mod._CKPT_PROBED = False
        mod._CKPT_ERROR = ""
        with mock.patch("importlib.import_module",
                        side_effect=ImportError("no fusion here")):
            self.assertFalse(mod._ckpt_probe())
        self.assertIn("ImportError", mod._CKPT_ERROR)


class AdaptiveChunkTests(unittest.TestCase):
    def test_adaptive_chunk_helper_present(self) -> None:
        src = (APP_DIR / "tools" / "asr_worker.py").read_text(encoding="utf-8")
        self.assertIn("def adaptive_chunk_seconds", src)
        self.assertIn("keep_engine", src)
        self.assertIn("def transcribe_cached", src)

    def test_short_media_gets_multi_chunks(self) -> None:
        """≤10 分钟素材默认 60–90s 分块，取消后检查点才有续跑收益。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "asr_worker_under_test", APP_DIR / "tools" / "asr_worker.py",
        )
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        target = mod.adaptive_chunk_seconds(300.0, 0)
        self.assertGreaterEqual(target, 60.0)
        self.assertLessEqual(target, 90.0)
        chunks = mod.plan_chunks(300.0, [], 0)
        self.assertGreaterEqual(len(chunks), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
