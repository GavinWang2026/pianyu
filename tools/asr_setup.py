#!/usr/bin/env python3
"""文案提取环境的后半程：下载识别模型 + 自检。

由 ``setup-asr.command`` / ``setup-asr.bat`` 在**建好 .venv-asr 并装完依赖之后**调用，
必须用 ``.venv-asr`` 里的解释器运行（需要 huggingface_hub 与对应引擎包）。

单独跑也可以，便于排查问题::

    .venv-asr/bin/python tools/asr_setup.py --engine mlx
    .venv-asr/bin/python tools/asr_setup.py --engine faster --check-only
"""

from __future__ import annotations

import argparse
import os
import sys

# 引擎 → 默认模型仓库。mlx 用 MLX 量化版，faster 用 CTranslate2 版。
MLX_MODEL = "mlx-community/whisper-large-v3-turbo"
FASTER_MODEL = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"

DEFAULT_MIRROR = "https://hf-mirror.com"


def _green(text: str) -> str:
    return f"\033[32m{text}\033[0m" if sys.stdout.isatty() else text


def _red(text: str) -> str:
    return f"\033[31m{text}\033[0m" if sys.stdout.isatty() else text


def download_model(repo: str) -> bool:
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        print(_red(f"   ✗ 缺少 huggingface_hub：{exc}"))
        return False

    # DEP-01：显式检查 httpx，避免抛出晦涩的 ModuleNotFoundError
    try:
        import httpx  # noqa: F401
    except ImportError:
        print(_red("   ✗ 未预置模型依赖 httpx（huggingface_hub 需要它）"))
        print("     请重跑安装脚本，或：.venv-asr/bin/pip install httpx")
        print("     若本地已有 models/ 权重，可跳过联网下载，直接启动 boot.py")
        return False

    endpoint = os.environ.get("HF_ENDPOINT") or DEFAULT_MIRROR
    os.environ["HF_ENDPOINT"] = endpoint
    try:
        path = snapshot_download(repo)
    except Exception as exc:  # noqa: BLE001 - 网络原因千奇百怪，统一给一句人话
        msg = str(exc)
        if "httpx" in msg.lower() or "No module named" in msg:
            print(_red("   ✗ 未预置模型（依赖缺失），不是网络故障"))
            print(f"     详情：{msg[:160]}")
        else:
            print(_red(f"   ✗ 模型下载失败：{msg[:200]}"))
            print("     可换镜像重试： HF_ENDPOINT=https://huggingface.co 重新运行")
        return False
    print(f"   模型已就绪: {path}")
    print(f"   （镜像 {endpoint}）")
    return True


def self_check(engine: str) -> bool:
    if engine == "mlx":
        try:
            import mlx_whisper  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            print(_red(f"   ✗ mlx-whisper 导入失败：{str(exc)[:160]}"))
            return False
        print("   ✓ mlx-whisper 可导入")
    else:
        try:
            import ctranslate2
            import faster_whisper  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            print(_red(f"   ✗ faster-whisper 导入失败：{str(exc)[:160]}"))
            return False
        print(f"   ✓ faster-whisper 可导入（CTranslate2 {ctranslate2.__version__}）")
        try:
            if ctranslate2.get_cuda_device_count() == 0:
                print("   提示：未检测到 CUDA 设备，将以 CPU 模式运行（int8 量化）")
        except Exception:  # noqa: BLE001 - 探测失败不影响可用性
            pass

    print(f"   ✓ Python {sys.version.split()[0]}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="文案提取环境：模型下载与自检")
    parser.add_argument("--engine", choices=("mlx", "faster"), required=True)
    parser.add_argument("--model", default="", help="留空则用该引擎的默认模型")
    parser.add_argument("--check-only", action="store_true", help="跳过下载，只做导入自检")
    args = parser.parse_args()

    repo = args.model or (MLX_MODEL if args.engine == "mlx" else FASTER_MODEL)

    if not args.check_only:
        print("③ 下载识别模型（已缓存则秒过）")
        if not download_model(repo):
            return 1
        print()

    print("④ 自检")
    return 0 if self_check(args.engine) else 1


if __name__ == "__main__":
    raise SystemExit(main())
