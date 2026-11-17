"""包内运行时资源定位 —— 「资源在哪」的唯一事实来源。

为什么要这个模块
----------------
绿色便携包把 Python 解释器、ffmpeg、第三方依赖全部收进 ``runtime/`` 子目录，
跟着程序主体一起分发，于是**用户的机器上不需要预装任何东西**：
没有 python、没有 ffmpeg、没有命令行操作，解压双击即可用。

但「包内优先、系统兜底」这个判断如果散落在各处，迟早会出现
「主服务用了系统 ffmpeg、worker 却在找包里 ffmpeg」这种撕裂。因此集中到这里，
其它模块只准问本模块，不准自己拼 ``runtime/`` 路径。

当前实际布局（以本仓库交付为准，D17）::

    runtime/
      site-packages/     ASR / 推理依赖（随包实体文件）
      MANIFEST.json      组件清单
      python/            （可选）便携 CPython —— find_portable_python()
      ffmpeg/bin/        （可选）静态 ffmpeg —— find_ffmpeg()

历史规划中的 ``pkgs/`` / ``pkgs-asr/`` 目录**尚未落地**；相关辅助函数已删除，
避免文档地图与实物漂移。需要分层依赖时再恢复并同步本模块文档。

查找顺序（改写时务必保持这个次序）::

    1. 环境变量覆盖   VD_FFMPEG / VD_RUNTIME —— 开发态调试、多引擎对照测试用
    2. 包内 runtime/  便携包的正常路径
    3. 系统 PATH      开发者机器上的 brew / 系统安装

开发态（在本仓库直接跑）没有 ``runtime/python`` 时落到 PATH；
打包态则第 1、2 步先命中，**完全不碰用户系统**。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Optional

APP_DIR = Path(__file__).resolve().parent.parent

RUNTIME_DIR_NAME = "runtime"

_ENV_RUNTIME = "VD_RUNTIME"
_ENV_FFMPEG = "VD_FFMPEG"

# ---------------------------------------------------------------------------
# 目录
# ---------------------------------------------------------------------------

def app_dir() -> Path:
    """程序主体所在目录。"""
    return APP_DIR


def runtime_dir() -> Path:
    """运行时根目录。允许 ``VD_RUNTIME`` 覆盖（便于把运行时放到别处做对照测试）。"""
    override = (os.environ.get(_ENV_RUNTIME) or "").strip()
    if override:
        return Path(override).expanduser()
    return APP_DIR / RUNTIME_DIR_NAME


def has_runtime() -> bool:
    """便携运行时是否已装配：site-packages 实体，或可选 python/ffmpeg 目录。"""
    root = runtime_dir()
    return (
        (root / "site-packages").is_dir()
        or (root / "python").is_dir()
        or (root / "ffmpeg").is_dir()
    )


# ---------------------------------------------------------------------------
# ffmpeg
# ---------------------------------------------------------------------------

def _executable(path: Path) -> Optional[str]:
    """是个可执行文件就返回字符串路径，否则 None。"""
    try:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    except OSError:
        pass
    return None


def find_ffmpeg() -> Optional[str]:
    """定位 ffmpeg 可执行文件；找不到返回 ``None``（由调用方决定怎么报错）。"""
    # 1. 显式覆盖
    explicit = (os.environ.get(_ENV_FFMPEG) or "").strip()
    if explicit:
        hit = _executable(Path(explicit).expanduser())
        if hit:
            return hit

    # 2. 包内静态二进制
    root = runtime_dir()
    for candidate in (
        root / "ffmpeg" / "bin" / "ffmpeg",
        root / "ffmpeg" / "bin" / "ffmpeg.exe",
        root / "ffmpeg" / "ffmpeg",
        root / "ffmpeg" / "ffmpeg.exe",
    ):
        hit = _executable(candidate)
        if hit:
            return hit

    # 3. 系统 PATH
    return shutil.which("ffmpeg")


def find_ffprobe() -> Optional[str]:
    """定位 ffprobe。查找次序与 :func:`find_ffmpeg` 完全一致。"""
    explicit = (os.environ.get("VD_FFPROBE") or "").strip()
    if explicit:
        hit = _executable(Path(explicit).expanduser())
        if hit:
            return hit

    root = runtime_dir()
    for candidate in (
        root / "ffmpeg" / "bin" / "ffprobe",
        root / "ffmpeg" / "bin" / "ffprobe.exe",
        root / "ffmpeg" / "ffprobe",
        root / "ffmpeg" / "ffprobe.exe",
    ):
        hit = _executable(candidate)
        if hit:
            return hit

    return shutil.which("ffprobe")


def ffmpeg_location() -> Optional[str]:
    """给 yt-dlp 用的 ``ffmpeg_location``。

    yt-dlp 接受「目录」或「可执行文件路径」两种形式。优先给目录 ——
    这样它能在同一目录里顺带找到 ffprobe（合并/探测都要用）。
    目录里只有 ffmpeg 没有 ffprobe 时退回给文件路径，省得它误判。
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None
    folder = Path(ffmpeg).parent
    if find_ffprobe() and Path(find_ffprobe() or "").parent == folder:
        return str(folder)
    return ffmpeg


# ---------------------------------------------------------------------------
# 便携解释器
# ---------------------------------------------------------------------------

def find_portable_python() -> Optional[Path]:
    """包内便携解释器路径；没装配运行时则返回 ``None``。"""
    root = runtime_dir() / "python"
    candidates = (
        root / "bin" / "python3",          # macOS / Linux
        root / "bin" / "python",
        root / "python.exe",               # Windows
        root / "Scripts" / "python.exe",
    )
    if sys.platform == "win32":
        candidates = tuple(reversed(candidates))
    for candidate in candidates:
        if _executable(candidate):
            return candidate
    return None
