# Copyright (c) 2026 Hyht-Team. All rights reserved.
# See LICENSE (proprietary) and NOTICE.md (third-party components).
"""旗舰版环境自检与离线修复（v2：发布包自包含）。

v1 解决的是解释器断链：``.venv-asr/bin/python`` 指向已删除的 uv Python 3.13.8。
v2 进一步解决了**可移植性**：原先 site-packages 软链到外卷 1.2 GB 依赖，
拔卷/换机器即失效。现在发布包自带裁剪后的运行时：

    runtime/site-packages/            161 MB 实体文件（实测只用了 8 个包）
    .venv-asr/lib/.../site-packages → ../../../runtime/site-packages（相对软链）
    .venv-asr/bin/python           → 本机 Python 3.13（换机器由 --repair 自动重接）

迁移命令（只在首次做，之后不再读外卷）：

    python3 materialize_runtime.py [--src /path/to/old/.venv-asr]

体检 / 换机器重接：

    python3 doctor.py --check
    python3 doctor.py --repair [--force]

依赖含 cp313-darwin 原生扩展，Windows 需另行安装 Windows 版 ASR 运行时。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent
ENV = APP_DIR / ".venv-asr"
PY_TAG = "python3.13"
IS_WINDOWS = os.name == "nt"
# Windows venv 布局是 Scripts/python.exe + Lib/site-packages；
# 且 macOS 的 cp313-darwin 原生扩展不可跨平台复用，必须另装 Win 版运行时。
VENV_PY = Path("Scripts") / "python.exe" if IS_WINDOWS else Path("bin") / "python"
SITE_REL = (Path("Lib") / "site-packages" if IS_WINDOWS
            else Path("lib") / PY_TAG / "site-packages")
RUNTIME_SITE = APP_DIR / "runtime" / "site-packages"
REL_LINK = Path("..") / ".." / ".." / "runtime" / "site-packages"

# 仅用于从旧安装做一次性迁移；发布包正常运行**不依赖**这些路径。
LEGACY_SITE_SOURCES = (
    Path("/Volumes/Adu5T/01 Software/AduVedioDawnload(AVD)/.venv-asr") / SITE_REL,
)


def _python_version(path: Path) -> str:
    try:
        result = subprocess.run(
            [str(path), "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
            capture_output=True, text=True, timeout=6, check=False,
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def find_python313() -> list[tuple[Path, str]]:
    seen: set[str] = set()
    out: list[tuple[Path, str]] = []
    candidates = [Path(sys.executable)]
    for name in ("python3.13", "python3", "python"):
        hit = shutil.which(name)
        if hit:
            candidates.append(Path(hit))
    if IS_WINDOWS:
        try:  # Windows 官方 py 启动器可直接定位 3.13
            launcher = subprocess.run(
                ["py", "-3.13", "-c", "import sys; print(sys.executable)"],
                capture_output=True, text=True, timeout=8, check=False,
            )
            if launcher.returncode == 0 and launcher.stdout.strip():
                candidates.append(Path(launcher.stdout.strip()))
        except (OSError, subprocess.SubprocessError):
            pass
    for path in candidates:
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        version = _python_version(path)
        if version.startswith("3.13."):
            out.append((path, version))
    return out


def find_site_source(explicit: str | None = None) -> Path | None:
    """包内运行时优先；外卷只作为一次性迁移兜底。"""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    env_hint = (os.environ.get("AVD_ASR_SITE") or "").strip()
    if env_hint:
        candidates.append(Path(env_hint).expanduser())
    candidates.append(RUNTIME_SITE)
    candidates.extend(LEGACY_SITE_SOURCES)
    for path in candidates:
        site = path / SITE_REL if (path / SITE_REL).is_dir() else path
        if site.is_dir() and ((site / "faster_whisper").exists() or (site / "mlx_whisper").exists()):
            return site
    return None


def _probe_env() -> dict[str, Any]:
    py = ENV / VENV_PY
    exists = py.exists() and os.access(str(py), os.X_OK)
    result: dict[str, Any] = {
        "env_dir": str(ENV),
        "env_is_symlink": ENV.is_symlink(),
        "python": str(py),
        "python_usable": exists,
        "packages": {},
    }
    if exists:
        try:
            probe = subprocess.run(
                [str(py), "-c",
                 "import json, importlib.util as u; "
                 "print(json.dumps({k: bool(u.find_spec(k)) for k in "
                 "('faster_whisper','mlx_whisper','ctranslate2','av')}))"],
                capture_output=True, text=True, timeout=20, check=False,
            )
            if probe.returncode == 0:
                result["packages"] = json.loads(probe.stdout.strip())
            else:
                result["error"] = (probe.stderr or "").strip()[-400:]
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            result["error"] = str(exc)
    return result


def check() -> dict[str, Any]:
    from fusion import asr_offline, boot_check  # noqa: WPS433
    link = ENV / SITE_REL
    manifest = APP_DIR / "runtime" / "MANIFEST.json"
    runtime_bytes = 0
    if manifest.is_file():
        try:
            runtime_bytes = int(json.loads(manifest.read_text(encoding="utf-8")).get("total_bytes", 0))
        except (OSError, ValueError):
            pass
    report: dict[str, Any] = {
        "product": "AVD Flagship",
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "engine": {},
        "ffmpeg": shutil.which("ffmpeg") or "",
        "python313_candidates": [{"path": str(p), "version": v} for p, v in find_python313()],
        "site_source": str(find_site_source() or ""),
        "models": asr_offline.status(),
        "asr": _probe_env(),
        "portable": {
            "runtime_bundled": bool((RUNTIME_SITE / "faster_whisper").is_dir()),
            "runtime_mb": round(runtime_bytes / 1024 / 1024, 1),
            "relative_link": link.is_symlink() and not os.path.isabs(os.readlink(link)),
            "link_target": os.readlink(link) if link.is_symlink() else "(实体目录)",
            "interpreter_usable": bool((ENV / VENV_PY).exists()),
            "native_abi": "cp313-win_amd64（需另装）" if IS_WINDOWS else "cp313-darwin / macOS arm64",
            "note": ("Windows 需运行 setup-windows-runtime.bat 安装 Win 版依赖；macOS 原生扩展不可复用"
                     if IS_WINDOWS else
                     "换机器需本机有 Python 3.13；--repair 自动重接解释器"),
        },
    }
    try:
        boot_check.activate()
        report["engine"] = boot_check.health()
    except Exception as exc:
        report["engine"] = {"ok": False, "reason": str(exc)}
    report["asr"]["ready"] = bool(
        report["asr"].get("python_usable")
        and any(report["asr"].get("packages", {}).values())
    )
    return report


def repair(explicit_src: str | None = None, *, force: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"changed": False, "steps": [], "error": ""}
    if IS_WINDOWS:
        result["error"] = (
            "Windows 请用 setup-windows-runtime.bat 创建 Win 版 ASR 运行时："
            "包内 runtime/ 是 cp313-darwin 原生扩展，不能跨平台软链复用"
        )
        result["asr"] = _probe_env()
        return result
    if ENV.is_symlink():
        result["steps"].append("包内已有 .venv-asr 符号链接，保持不动")
        result["asr"] = _probe_env()
        return result
    interpreter_link = ENV / VENV_PY
    interpreter_ok = interpreter_link.exists() and os.access(str(interpreter_link), os.X_OK)
    if ENV.exists() and not force and interpreter_ok:
        result["steps"].append("包内 .venv-asr 已就绪（解释器可用），未改动")
        result["asr"] = _probe_env()
        return result
    if ENV.exists() and not interpreter_ok:
        result["steps"].append("检测到解释器链接失效（常见于换机器），自动重接")

    pythons = find_python313()
    if not pythons:
        result["error"] = "本机没有 Python 3.13；请运行 setup-asr.command 安装独立识别环境"
        return result
    site = find_site_source(explicit_src)
    if not site:
        result["error"] = (
            "找不到已装好 faster-whisper / mlx-whisper 的 site-packages；"
            "用 --asr-src 指向已有 .venv-asr，或运行 setup-asr.command 安装"
        )
        return result

    interpreter, version = pythons[0]
    bin_dir = ENV / "bin"
    site_dir = ENV / SITE_REL
    bin_dir.mkdir(parents=True, exist_ok=True)
    site_dir.parent.mkdir(parents=True, exist_ok=True)

    (bin_dir / "python").unlink(missing_ok=True)
    (bin_dir / "python").symlink_to(interpreter)
    (bin_dir / PY_TAG).unlink(missing_ok=True)
    (bin_dir / PY_TAG).symlink_to("python")
    (bin_dir / "python3").unlink(missing_ok=True)
    (bin_dir / "python3").symlink_to("python")

    if site_dir.is_symlink() or site_dir.is_dir():
        if site_dir.is_symlink():
            site_dir.unlink()
        elif force:
            shutil.rmtree(site_dir)
    if site.resolve() == RUNTIME_SITE.resolve():
        site_dir.symlink_to(REL_LINK, target_is_directory=True)
        link_desc = f"包内运行时（相对软链 → {REL_LINK}，整包可移动）"
    else:
        site_dir.symlink_to(site, target_is_directory=True)
        link_desc = f"{site}（外部路径；建议先跑 materialize_runtime.py 实体化）"

    home = str(interpreter.parent)
    (ENV / "pyvenv.cfg").write_text(
        f"home = {home}\nimplementation = CPython\nversion_info = {version}\n"
        "include-system-site-packages = false\n",
        encoding="utf-8",
    )
    result["steps"] += [
        f"解释器链接 → {interpreter} ({version})",
        f"依赖目录 → {link_desc}",
        f"写入 pyvenv.cfg（home={home}；换机器由 --repair 自动重写）",
    ]
    result["changed"] = True
    result["asr"] = _probe_env()
    result["asr"]["ready"] = bool(result["asr"].get("python_usable")
                                  and any(result["asr"].get("packages", {}).values()))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="AVD Flagship 环境自检与离线修复")
    parser.add_argument("--check", action="store_true", help="只体检，不改动")
    parser.add_argument("--repair", action="store_true", help="离线修复 ASR 解释器断链")
    parser.add_argument("--asr-src", default=None, help="已有 .venv-asr 目录（提供依赖来源）")
    parser.add_argument("--force", action="store_true", help="允许重建包内 .venv-asr")
    args = parser.parse_args()

    if args.repair:
        payload = repair(args.asr_src, force=args.force)
    else:
        payload = check()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not payload.get("error") else 1


if __name__ == "__main__":
    raise SystemExit(main())
