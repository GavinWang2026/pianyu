# Copyright (c) 2026 Hyht-Team. All rights reserved.
# See LICENSE (proprietary) and NOTICE.md (third-party components).
"""领域状态机骨架 —— 把四套 Manager 的阶段推进收成同一套规则。

Manager 仍保留各自业务（下载/转写/整理/流水线），但：
  * 合法阶段集合与转移表集中在此；
  * ``StageMachine.advance`` 统一校验「能否进入下一阶段」；
  * 与 ``TaskRegistry.patch`` 配合，避免各写一套终态判定。
"""

from __future__ import annotations

from typing import Any, Iterable

from .task_store import TERMINAL_STAGES, is_terminal

# 各领域允许的阶段（含终态）
STAGES: dict[str, frozenset[str]] = {
    "download": frozenset({
        "queued", "downloading", "postprocess", "done", "error", "cancelled",
    }),
    "asr": frozenset({
        "queued", "subtitle", "fetching", "decoding", "analyzing",
        "transcribing", "done", "error", "cancelled",
    }),
    "polish": frozenset({
        "queued", "local", "cleaning", "insight", "running",
        "done", "error", "cancelled",
    }),
    "pipeline": frozenset({
        "queued", "probe", "download", "transcript", "polish",
        "knowledge", "done", "error", "cancelled",
    }),
}

# 允许的转移：from -> to（终态可从任意非终态进入）
_TRANSITIONS: dict[str, dict[str, frozenset[str]]] = {
    "download": {
        "queued": frozenset({"downloading", "error", "cancelled"}),
        "downloading": frozenset({"postprocess", "done", "error", "cancelled"}),
        "postprocess": frozenset({"done", "error", "cancelled"}),
    },
    "asr": {
        "queued": frozenset({"subtitle", "fetching", "decoding", "transcribing", "done", "error", "cancelled"}),
        "subtitle": frozenset({"fetching", "decoding", "queued", "done", "error", "cancelled"}),
        "fetching": frozenset({"decoding", "analyzing", "transcribing", "queued", "done", "error", "cancelled"}),
        "decoding": frozenset({"analyzing", "transcribing", "queued", "done", "error", "cancelled"}),
        "analyzing": frozenset({"transcribing", "queued", "done", "error", "cancelled"}),
        # queued：串行池等待时可回排队态，避免 UI 误显示「识别中」
        "transcribing": frozenset({"queued", "done", "error", "cancelled"}),
    },
    "polish": {
        "queued": frozenset({"local", "cleaning", "running", "done", "error", "cancelled"}),
        "local": frozenset({"cleaning", "insight", "done", "error", "cancelled"}),
        "cleaning": frozenset({"insight", "done", "error", "cancelled"}),
        "insight": frozenset({"done", "error", "cancelled"}),
        "running": frozenset({"local", "cleaning", "insight", "done", "error", "cancelled"}),
    },
    "pipeline": {
        "queued": frozenset({"probe", "error", "cancelled"}),
        "probe": frozenset({"download", "transcript", "polish", "knowledge", "done", "error", "cancelled"}),
        "download": frozenset({"transcript", "download", "error", "cancelled"}),
        "transcript": frozenset({"polish", "knowledge", "download", "transcript", "done", "error", "cancelled"}),
        "polish": frozenset({"knowledge", "done", "error", "cancelled"}),
        "knowledge": frozenset({"done", "error", "cancelled"}),
    },
}


class StageError(ValueError):
    """非法阶段或非法转移。"""


class StageMachine:
    """某一领域的阶段校验器。"""

    def __init__(self, kind: str) -> None:
        if kind not in STAGES:
            raise StageError(f"未知领域：{kind}")
        self.kind = kind
        self.stages = STAGES[kind]
        self.transitions = _TRANSITIONS.get(kind, {})

    def assert_stage(self, stage: str) -> str:
        s = str(stage or "")
        if s not in self.stages:
            raise StageError(f"{self.kind} 非法阶段：{s}")
        return s

    def can_enter(self, current: str | None, nxt: str) -> bool:
        cur = str(current or "queued")
        nxt = self.assert_stage(nxt)
        if cur == nxt:
            return True
        if is_terminal(cur):
            return False
        if is_terminal(nxt):
            return True
        allowed = self.transitions.get(cur)
        if allowed is None:
            return nxt in self.stages
        return nxt in allowed

    def advance(self, current: str | None, nxt: str) -> str:
        """校验并返回下一阶段；非法则抛 StageError。"""
        nxt = self.assert_stage(nxt)
        if not self.can_enter(current, nxt):
            raise StageError(f"{self.kind} 不能从 {current!r} 进入 {nxt!r}")
        return nxt


def machines() -> dict[str, StageMachine]:
    return {k: StageMachine(k) for k in STAGES}


def describe() -> dict[str, Any]:
    return {
        "kinds": sorted(STAGES),
        "terminal": sorted(TERMINAL_STAGES),
        "stages": {k: sorted(v) for k, v in STAGES.items()},
    }
