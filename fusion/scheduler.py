# Copyright (c) 2026 Hyht-Team. All rights reserved.
# See LICENSE (proprietary) and NOTICE.md (third-party components).
"""批量延迟 / 定时调度：把「何时开跑」从 Manager 里拆出来。

不引入 cron 守护进程；每个任务在入队时带 ``not_before``（unix 秒），
worker 拿到槽位后若未到点则 sleep 到点再跑。上限与批量一致。
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any


MAX_DELAY_SECONDS = 7 * 24 * 3600  # 最长延迟 7 天


def local_tzinfo():
    """本机本地时区（无时区 ISO 按此解释，而非 UTC）。"""
    return datetime.now().astimezone().tzinfo or timezone.utc


def parse_not_before(
    *,
    delay_seconds: Any = None,
    run_at: Any = None,
    now: float | None = None,
) -> float | None:
    """解析 delay_seconds 或 run_at（ISO8601 / unix），返回 unix 秒；None=立即。

    无时区的 ISO 字符串按**本地时区**解释（不是 UTC），避免东八区晚 8 小时。
    """
    base = time.time() if now is None else float(now)
    if delay_seconds is not None and str(delay_seconds).strip() != "":
        try:
            delay = float(delay_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("delay_seconds 必须是数字") from exc
        if delay < 0:
            raise ValueError("delay_seconds 不能为负")
        if delay > MAX_DELAY_SECONDS:
            raise ValueError(f"delay_seconds 上限 {MAX_DELAY_SECONDS} 秒")
        if delay < 0.5:
            return None
        return base + delay

    if run_at is None or str(run_at).strip() == "":
        return None
    raw = str(run_at).strip()
    # unix 秒（纯数字）
    try:
        ts = float(raw)
    except (TypeError, ValueError):
        ts = None
    if ts is not None and raw.replace(".", "", 1).isdigit():
        if ts > 1e11:  # 毫秒
            ts /= 1000.0
        if ts < base - 60:
            raise ValueError("run_at 已过期")
        if ts - base > MAX_DELAY_SECONDS:
            raise ValueError(f"run_at 距现在超过 {MAX_DELAY_SECONDS} 秒")
        return ts if ts > base + 0.5 else None
    # ISO8601
    text = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("run_at 需为 unix 秒或 ISO8601 时间") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local_tzinfo())
    ts = dt.timestamp()
    if ts < base - 60:
        raise ValueError("run_at 已过期")
    if ts - base > MAX_DELAY_SECONDS:
        raise ValueError(f"run_at 距现在超过 {MAX_DELAY_SECONDS} 秒")
    return ts if ts > base + 0.5 else None


def resolve_run_at(run_at: Any, *, now: float | None = None) -> dict[str, Any]:
    """解析 run_at 并回显本地时间，便于 UI 确认。"""
    ts = parse_not_before(run_at=run_at, now=now)
    if ts is None:
        return {"not_before": None, "resolved_local": None}
    local = datetime.fromtimestamp(ts).astimezone()
    return {
        "not_before": ts,
        "resolved_local": local.strftime("%Y-%m-%d %H:%M:%S %z"),
    }


def wait_until(not_before: float | None, *, aborted: Any = None, tick: float = 0.5) -> None:
    """阻塞到 not_before；aborted() 为真则提前返回。"""
    if not_before is None:
        return
    while True:
        if aborted is not None and aborted():
            return
        remain = float(not_before) - time.time()
        if remain <= 0:
            return
        time.sleep(min(tick, remain))
