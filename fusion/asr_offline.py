# Copyright (c) 2026 Hyht-Team
# SPDX-License-Identifier: MIT
# See LICENSE (MIT) and NOTICE.md (third-party components).
"""离线语音模型装配 —— 让「断网本地转写」从话术变成事实。

实测背景（2026-11-12，本机真实跑过）：

* 原版 ``tools/asr_worker.py`` 默认模型是 ``large-v3-turbo``（约 1.5 GB），
  首次使用需联网从 HuggingFace 拉取；隔离环境里 ``huggingface_hub`` 走
  Xet/CAS 链路直接抛 ``HfHubHTTPError``，转写整块失败。
* 本机 ``~/.cache/huggingface`` 只有 1.2 MB，没有任何 Whisper 权重。
* worker 本身已内建 ``VD_ASR_FASTER_MODEL`` 与「缓存优先」逻辑
  （``resolve_faster_model``），只要把本地 CT2 模型目录喂进去即可完全离线，
  **上游/自研代码一行都不用改**。

本模块负责：扫描发布包 ``models/`` 下预置的 CT2 模型目录，按档位挑最大的，
装配成 ASR worker 的默认模型，并返回一个可自检的报告。

优先级：调用方显式设置的环境变量 > 包内最大档模型 > worker 上游默认（联网）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent.parent
MODEL_ROOT = APP_DIR / "models"

# 与 tools/asr_worker.py::resolve_faster_model 的 rank 保持同一口径
_RANK = (
    ("large-v3-turbo", 90), ("large-v3", 80), ("large", 70),
    ("medium", 50), ("small", 30), ("base", 20), ("tiny", 10),
)

_REQUIRED = ("model.bin", "config.json", "tokenizer.json")


def _score(name: str) -> int:
    low = name.lower()
    for token, weight in _RANK:
        if token in low:
            return weight
    return 0


def available_models() -> list[dict[str, Any]]:
    """列出包内可直接离线加载的 CT2 模型目录。"""
    if not MODEL_ROOT.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for entry in sorted(MODEL_ROOT.iterdir()):
        if not entry.is_dir() or entry.is_symlink():
            continue
        if not all((entry / f).is_file() for f in _REQUIRED):
            continue
        size = sum((entry / f).stat().st_size for f in _REQUIRED)
        found.append({"name": entry.name, "path": str(entry),
                      "bytes": size, "rank": _score(entry.name)})
    return sorted(found, key=lambda x: (-x["rank"], x["name"]))


def configure(*, env: dict[str, str] | None = None) -> dict[str, Any]:
    """把包内离线模型装配为默认模型；返回状态报告，不抛异常。

    不覆盖调用方显式设置的值。设了包内模型时同时把识别引擎固定为
    ``faster``，避免 macOS 上先去试需要联网下载的 MLX 大模型、白等十几秒。
    """
    target = env if env is not None else os.environ
    models = available_models()
    explicit = (target.get("VD_ASR_FASTER_MODEL") or "").strip()
    report: dict[str, Any] = {
        "model_root": str(MODEL_ROOT),
        "bundled_models": [m["name"] for m in models],
        "explicit_override": bool(explicit),
        "mode": "",
        "selected": "",
        "offline_capable": False,
        "engine": (target.get("AVD_ASR_ENGINE") or "auto"),
    }
    if not models:
        report["mode"] = "upstream-default"
        report["selected"] = explicit or "large-v3-turbo (需联网)"
        report["note"] = "发布包未预置权重；断网转写不可用"
        return report

    best = models[0]
    if explicit:
        report["mode"] = "explicit"
        report["selected"] = explicit
        report["offline_capable"] = True
    else:
        target["VD_ASR_FASTER_MODEL"] = best["path"]
        report["mode"] = "bundled-offline"
        report["selected"] = best["name"]
        report["offline_capable"] = True
        if "AVD_ASR_ENGINE" not in target:
            target["AVD_ASR_ENGINE"] = "faster"
    report["engine"] = target.get("AVD_ASR_ENGINE", "auto")
    report["selected_path"] = best["path"] if not explicit else explicit
    report["note"] = "权重随包交付，转写不依赖外网"
    return report


def status() -> dict[str, Any]:
    """只读状态，供 doctor / health 上报（不修改环境）。

    刻意排除启动时装配的那两个变量，报告的是「包内模型本身」的可用性，
    而不是「当前进程恰好已经设了什么」。
    """
    fake: dict[str, str] = {
        k: v for k, v in os.environ.items()
        if k not in ("VD_ASR_FASTER_MODEL", "VD_ASR_MLX_MODEL", "AVD_ASR_ENGINE")
    }
    return configure(env=fake)
