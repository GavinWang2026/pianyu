# Copyright (c) 2026 Hyht-Team
# SPDX-License-Identifier: MIT
"""统一任务内核 + SQLite 持久化回归。"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

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

from fusion import persist, task_store  # noqa: E402


@dataclass
class _Toy:
    id: str
    stage: str = "queued"
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    title: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "stage": self.stage, "created_at": self.created_at,
            "finished_at": self.finished_at, "title": self.title,
        }


class TaskStoreTests(unittest.TestCase):
    def test_patch_broadcasts_and_terminal_callback(self) -> None:
        seen: list[tuple[str, dict]] = []
        reg = task_store.TaskRegistry(
            kind="toy",
            to_dict=lambda t: t.to_dict(),
            on_terminal=lambda kind, snap: seen.append((kind, snap)),
        )
        toy = _Toy(id="abc", title="hello")
        reg.put(toy)
        q = reg.subscribe("abc")
        first = q.get(timeout=1)
        self.assertEqual(first["type"], "task")
        reg.patch(toy, stage="done", finished_at=time.time(), title="done-title")
        ev = q.get(timeout=1)
        self.assertEqual(ev["task"]["stage"], "done")
        self.assertEqual(seen[-1][0], "toy")
        self.assertEqual(seen[-1][1]["title"], "done-title")
        self.assertTrue(task_store.is_terminal("cancelled"))

    def test_terminal_stage_is_irreversible(self) -> None:
        reg = task_store.TaskRegistry(
            kind="toy",
            to_dict=lambda t: t.to_dict(),
        )
        toy = _Toy(id="c1", stage="cancelled")
        reg.put(toy)
        snap = reg.patch(toy, stage="transcribing", title="nope")
        self.assertEqual(snap["stage"], "cancelled")
        self.assertEqual(toy.stage, "cancelled")
        # 终态之间允许覆盖（如 error → cancelled 不在此强制）；非终态必须丢掉
        snap2 = reg.patch(toy, title="keep-title")
        self.assertEqual(snap2["title"], "keep-title")


class EventBusTests(unittest.TestCase):
    def test_subscribe_prelude_and_snapshot_order(self) -> None:
        bus = task_store.EventBus(queue_size=32)
        q = bus.subscribe(
            "t1",
            snapshot={"id": "t1"},
            prelude=[{"type": "log", "message": "先来一条"}],
        )
        self.assertEqual(q.get(timeout=1)["message"], "先来一条")
        self.assertEqual(q.get(timeout=1)["task"]["id"], "t1")

    def test_unsubscribe_is_idempotent(self) -> None:
        bus = task_store.EventBus()
        q = bus.subscribe("t1")
        bus.unsubscribe("t1", q)
        bus.unsubscribe("t1", q)          # 重复退订不得抛错
        bus.unsubscribe("ghost", q)       # 未知任务同样安静
        bus.broadcast("t1", {"type": "x"})
        self.assertTrue(q.empty())

    def test_broadcast_drops_when_queue_full(self) -> None:
        """队列满时必须丢事件而不是反压生产者（否则无订阅者的慢客户端会拖死任务）。"""
        bus = task_store.EventBus(queue_size=32)
        q = bus.subscribe("t1")
        for i in range(200):
            bus.broadcast("t1", {"type": "task", "n": i})
        self.assertEqual(q.qsize(), 32)   # 上限封顶，未抛 queue.Full

    def test_broadcast_without_subscribers(self) -> None:
        task_store.EventBus().broadcast("nobody", {"type": "x"})  # 不得抛错


class RegistryApiTests(unittest.TestCase):
    def _reg(self, **kw):
        return task_store.TaskRegistry(
            kind=kw.pop("kind", "toy"),
            to_dict=lambda t: t.to_dict(),
            **kw,
        )

    def test_container_protocol_and_getters(self) -> None:
        reg = self._reg()
        a = _Toy(id="a", created_at=100.0)
        b = _Toy(id="b", created_at=200.0)
        reg.put(a)
        reg.put(b)
        self.assertEqual(len(reg), 2)
        self.assertIn("a", reg)
        self.assertNotIn("zz", reg)
        self.assertIs(reg.get("a"), a)
        self.assertIsNone(reg.get("zz"))
        self.assertEqual(reg.get_dict("a")["id"], "a")
        self.assertIsNone(reg.get_dict("zz"))
        # list_dicts 按 created_at 倒序
        self.assertEqual([r["id"] for r in reg.list_dicts()], ["b", "a"])
        self.assertEqual(sorted(k for k, _ in reg.items()), ["a", "b"])
        self.assertEqual(len(reg.values()), 2)
        self.assertIsInstance(reg.lock, type(reg.lock))
        self.assertIs(reg.pop("a"), a)
        self.assertIsNone(reg.pop("a"))

    def test_custom_id_of(self) -> None:
        reg = task_store.TaskRegistry(
            kind="toy", to_dict=lambda t: t.to_dict(),
            id_of=lambda t: f"custom-{t.title}",
        )
        reg.put(_Toy(id="ignored", title="x"))
        self.assertIn("custom-x", reg)
        self.assertNotIn("ignored", reg)

    def test_patch_ignores_unknown_fields(self) -> None:
        reg = self._reg()
        toy = _Toy(id="p1")
        reg.put(toy)
        snap = reg.patch(toy, not_a_field="x", title="ok")
        self.assertEqual(snap["title"], "ok")
        self.assertFalse(hasattr(toy, "not_a_field"))

    def test_patch_terminal_to_terminal_allowed(self) -> None:
        reg = self._reg()
        toy = _Toy(id="p2", stage="error")
        reg.put(toy)
        self.assertEqual(reg.patch(toy, stage="cancelled")["stage"], "cancelled")

    def test_patch_terminal_only_field_returns_snapshot(self) -> None:
        """终态任务只收到非终态 stage 时：丢弃 stage，且不广播空事件。"""
        reg = self._reg()
        toy = _Toy(id="p3", stage="done")
        reg.put(toy)
        q = reg.subscribe("p3")
        q.get(timeout=1)                       # 订阅快照
        snap = reg.patch(toy, stage="queued")
        self.assertEqual(snap["stage"], "done")
        self.assertTrue(q.empty())

    def test_patch_stage_machine_rejection_is_lenient(self) -> None:
        """StageMachine 拒绝非法转移时不得阻断业务（只记日志）。"""
        reg = self._reg(kind="definitely-not-a-machine")
        toy = _Toy(id="p4", stage="queued")
        reg.put(toy)
        self.assertEqual(reg.patch(toy, stage="weird")["stage"], "weird")

    def test_on_terminal_error_is_swallowed(self) -> None:
        def _boom(kind, snap):
            raise RuntimeError("持久化挂了")

        reg = self._reg(on_terminal=_boom)
        toy = _Toy(id="p5")
        reg.put(toy)
        # 持久化失败不能把任务状态回滚，也不能把异常抛给调用方
        self.assertEqual(reg.patch(toy, stage="done")["stage"], "done")

    def test_subscribe_missing_task_gets_no_snapshot(self) -> None:
        reg = self._reg()
        q = reg.subscribe("ghost", prelude=[{"type": "notice"}])
        self.assertEqual(q.get(timeout=1)["type"], "notice")
        self.assertTrue(q.empty())

    def test_registry_broadcast_and_unsubscribe(self) -> None:
        reg = self._reg()
        reg.put(_Toy(id="b1"))
        q = reg.subscribe("b1")
        q.get(timeout=1)
        reg.broadcast("b1", {"type": "progress", "percent": 40})
        self.assertEqual(q.get(timeout=1)["percent"], 40)
        reg.unsubscribe("b1", q)
        reg.broadcast("b1", {"type": "progress", "percent": 90})
        self.assertTrue(q.empty())

    def test_clear_finished_and_prune_by_ttl(self) -> None:
        reg = self._reg()
        now = time.time()
        done = _Toy(id="d1", stage="done", created_at=now - 100, finished_at=now - 100)
        old = _Toy(id="d2", stage="error", created_at=now - 10_000,
                   finished_at=now - 10_000)
        running = _Toy(id="r1", stage="transcribing", created_at=now - 20_000)
        for t in (done, old, running):
            reg.put(t)
        removed = reg.prune(ttl_seconds=3600, max_tasks=200)
        self.assertEqual(removed, 1)
        self.assertIn("d1", reg)
        self.assertNotIn("d2", reg)
        self.assertIn("r1", reg)               # 运行中任务不受 TTL 影响
        self.assertEqual(reg.clear_finished(), 1)
        self.assertIn("r1", reg)

    def test_prune_by_count(self) -> None:
        reg = self._reg()
        for i in range(6):
            reg.put(_Toy(id=f"n{i}", stage="done", created_at=float(i)))
        removed = reg.prune(max_tasks=4)
        self.assertEqual(removed, 2)
        self.assertEqual(sorted(t.id for t in reg.values()), ["n2", "n3", "n4", "n5"])

    def test_prune_survives_bad_timestamps(self) -> None:
        reg = self._reg()
        bad = _Toy(id="bad", stage="done")
        bad.finished_at = "not-a-number"
        bad.created_at = "also-bad"
        reg.put(bad)
        # 时间戳脏数据按 age=0 处理，不能被 TTL 误删
        self.assertEqual(reg.prune(ttl_seconds=1), 0)
        self.assertIn("bad", reg)


class PersistTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Path(self.tmp.name) / "t.sqlite3"
        persist.reset_for_tests(self.db)

    def tearDown(self) -> None:
        persist.reset_for_tests(None)
        self.tmp.cleanup()

    def test_roundtrip(self) -> None:
        kind = "unit-persist"
        persist.delete_kind(kind)
        persist.save_task(kind, {
            "id": "t1", "stage": "done", "title": "稿", "url": "https://x",
            "created_at": time.time(), "finished_at": time.time(),
        })
        rows = persist.load_tasks(kind)
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["id"], "t1")
        self.assertEqual(persist.get_task(kind, "t1")["title"], "稿")
        st = persist.stats()
        self.assertEqual(st["by_kind"].get(kind), 1)
        self.assertTrue(str(st["path"]).endswith("t.sqlite3"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
