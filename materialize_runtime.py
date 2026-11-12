# Copyright (c) 2026 Hyht-Team. All rights reserved.
# See LICENSE (proprietary) and NOTICE.md (third-party components).
"""把 ASR 运行时从「外卷软链」实体化为「包内自包含」。

背景（2026-11-12 实测）：原发布包的 ``.venv-asr/lib/python3.13/site-packages``
是软链到 ``/Volumes/Adu5T/.../site-packages``（1.2 GB），拔卷/换机器即失效。

本脚本按**实测导入探针**的结果精确裁剪：离线权重固定走 faster 引擎后，
worker 真正加载的只有 8 个第三方包（约 170 MB），torch 568M / mlx 211M /
llvmlite 125M / scipy 87M 等全是死重，不进发布包。

    python3 materialize_runtime.py [--src /path/to/existing/.venv-asr]

产出 ``runtime/site-packages/``，并把 ``.venv-asr`` 里那条软链改成
**相对路径**指向包内目录 —— 整个发布包可以随意移动、解压、拷到 U 盘。
外卷只在首次迁移时被读取，之后完全不需要。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent
RUNTIME = APP_DIR / "runtime" / "site-packages"
LINK = APP_DIR / ".venv-asr" / "lib" / "python3.13" / "site-packages"
PY_TAG = "python3.13"
SITE_REL = Path("lib") / PY_TAG / "site-packages"

# 来自真实运行探针：tools/asr_worker.py 转写自有中文语音时加载的顶层包
WANT = (
    "av", "ctranslate2", "faster_whisper", "huggingface_hub",
    "numpy", "onnxruntime", "tokenizers", "tqdm",
)

LEGACY_SOURCES = (
    Path("/Volumes/Adu5T/01 Software/AduVedioDawnload(AVD)/.venv-asr") / SITE_REL,
)

_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".DS_Store")


def find_source(explicit: str | None = None) -> Path | None:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.extend(LEGACY_SOURCES)
    for path in candidates:
        site = path / SITE_REL if (path / SITE_REL).is_dir() else path
        if site.is_dir() and all((site / name).exists() for name in WANT):
            return site
    return None


def _copy_entry(src: Path, dst: Path) -> int:
    if src.is_dir():
        shutil.copytree(src, dst, symlinks=True, ignore=_IGNORE, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dst, follow_symlinks=False)
    if dst.is_dir():
        return sum(f.stat().st_size for f in dst.rglob("*") if f.is_file())
    return dst.stat().st_size


def materialize(src: Path) -> dict[str, Any]:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    copied: list[dict[str, Any]] = []
    for name in WANT:
        for entry in sorted(src.glob(f"{name}*")):
            if not (entry.is_dir() or entry.is_file()):
                continue
            # 只取包目录本身与对应的 .dist-info，不误伤同前缀的其他包
            stem = entry.name
            if stem != name and not (stem.startswith(name + "-") and stem.endswith(".dist-info")):
                continue
            size = _copy_entry(entry, RUNTIME / stem)
            copied.append({"name": stem, "bytes": size})

    total = sum(x["bytes"] for x in copied)
    manifest = {
        "generator": "AVD Flagship v2 materialize_runtime.py",
        "python_abi": "cp313",
        "platform": sys.platform,
        "source": str(src),
        "selection_basis": "运行期导入探针（tools/asr_worker.py 实测加载集合）",
        "packages": copied,
        "total_bytes": total,
        "note": "包内实体文件；发布包不依赖任何外卷或本机 site-packages",
    }
    (APP_DIR / "runtime" / "MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # 相对软链：从 .venv-asr/lib/python3.13/site-packages 回到包根需 ../../..
    rel = Path("..") / ".." / ".." / "runtime" / "site-packages"
    if LINK.is_symlink() or LINK.is_dir():
        if LINK.is_symlink():
            LINK.unlink()
        else:
            shutil.rmtree(LINK)
    LINK.parent.mkdir(parents=True, exist_ok=True)
    LINK.symlink_to(rel, target_is_directory=True)

    return {
        "source": str(src), "runtime": str(RUNTIME),
        "entries": len(copied), "total_mb": round(total / 1024 / 1024, 1),
        "link": str(LINK), "link_target": str(rel),
        "link_resolves": (LINK / "faster_whisper").is_dir(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="实体化 ASR 运行时，解除外卷依赖")
    parser.add_argument("--src", default=None, help="已有 .venv-asr 目录（首次迁移用）")
    args = parser.parse_args()

    if (RUNTIME / "faster_whisper").is_dir() and not args.src:
        print("包内运行时已存在，无需重复迁移。如需重建先删除 runtime/site-packages。")
        print(json.dumps({"runtime": str(RUNTIME), "already": True}, ensure_ascii=False))
        return 0

    src = find_source(args.src)
    if not src:
        print("✕ 找不到可迁移的 ASR 依赖源；用 --src 指向已有的 .venv-asr")
        return 2
    print(f"▸ 源：{src}")
    result = materialize(src)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["link_resolves"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
