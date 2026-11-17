# Copyright (c) 2026 Hyht-Team. All rights reserved.
# See LICENSE (proprietary) and NOTICE.md (third-party components).
"""切片导出：按时间轴用 ffmpeg 精确切一段媒体。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any


def _ffmpeg() -> str:
    bundled = Path(__file__).resolve().parent.parent / "runtime" / "ffmpeg" / "bin" / "ffmpeg"
    if bundled.is_file():
        return str(bundled)
    found = shutil.which("ffmpeg")
    if not found:
        raise RuntimeError("未找到 ffmpeg，无法切片")
    return found


def export_clip(
    source: str | Path,
    *,
    start: float,
    end: float,
    dest: str | Path,
    reencode: bool = False,
) -> dict[str, Any]:
    """从 source 切 [start, end) 秒到 dest。

    默认流复制（快）；失败或 reencode=True 时重编码为 H.264/AAC。
    """
    src = Path(source).expanduser()
    if not src.is_file():
        raise ValueError("源媒体不存在")
    try:
        t0 = float(start)
        t1 = float(end)
    except (TypeError, ValueError) as exc:
        raise ValueError("start/end 必须是数字（秒）") from exc
    if t0 < 0:
        t0 = 0.0
    if t1 <= t0 + 0.2:
        raise ValueError("切片时长至少 0.2 秒")
    if t1 - t0 > 600:
        raise ValueError("单次切片上限 600 秒")

    out = Path(dest).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    duration = t1 - t0
    ff = _ffmpeg()

    def _run(copy: bool) -> subprocess.CompletedProcess:
        cmd = [
            ff, "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{t0:.3f}", "-t", f"{duration:.3f}",
            "-i", str(src),
        ]
        if copy:
            cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero", str(out)]
        else:
            cmd += [
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                str(out),
            ]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=300)

    if not reencode:
        proc = _run(True)
        if proc.returncode == 0 and out.is_file() and out.stat().st_size > 0:
            return {
                "path": str(out),
                "bytes": out.stat().st_size,
                "start": t0,
                "end": t1,
                "mode": "copy",
            }
    proc = _run(False)
    if proc.returncode != 0 or not out.is_file():
        detail = (proc.stderr or "").strip().splitlines()
        raise RuntimeError(detail[-1] if detail else "ffmpeg 切片失败")
    return {
        "path": str(out),
        "bytes": out.stat().st_size,
        "start": t0,
        "end": t1,
        "mode": "reencode",
    }
