# Copyright (c) 2026 Hyht-Team
# SPDX-License-Identifier: MIT
# See LICENSE (MIT) and NOTICE.md (third-party components).
"""结构化滚动日志 —— 供流水线 / 诊断 / 运维回看。

默认写到 ``logs/avd.log``（相对项目根），按大小滚动，保留若干份。
不向 stdout 刷敏感字段；对 URL query / Cookie 做粗粒度脱敏。
"""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = APP_DIR / "logs"
LOG_FILE = LOG_DIR / "avd.log"

_MAX_BYTES = 2 * 1024 * 1024
_BACKUP_COUNT = 5
_SENSITIVE = re.compile(
    r"(api[_-]?key|token|password|cookie|authorization)\s*[:=]\s*\S+",
    re.IGNORECASE,
)
_QUERY_SECRET = re.compile(
    r"([?&](?:token|key|sign|signature|access_token|api_key)=)[^&]+",
    re.IGNORECASE,
)

_logger: logging.Logger | None = None


def _redact(text: str) -> str:
    text = _SENSITIVE.sub(r"\1=***", text)
    text = _QUERY_SECRET.sub(r"\1***", text)
    return text


def get_logger(name: str = "avd") -> logging.Logger:
    """进程内单例：首次调用时挂上 RotatingFileHandler。"""
    global _logger
    if _logger is not None and name == "avd":
        return _logger
    log = logging.getLogger(name)
    if log.handlers:
        if name == "avd":
            _logger = log
        return log
    log.setLevel(logging.INFO)
    log.propagate = False
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            LOG_FILE, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
    except OSError:
        handler = logging.NullHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    log.addHandler(handler)
    if name == "avd":
        _logger = log
    return log


def info(msg: str, **fields: Any) -> None:
    get_logger().info(_format(msg, fields))


def warning(msg: str, **fields: Any) -> None:
    get_logger().warning(_format(msg, fields))


def error(msg: str, **fields: Any) -> None:
    get_logger().error(_format(msg, fields))


def _format(msg: str, fields: dict[str, Any]) -> str:
    parts = [_redact(str(msg))]
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(f"{key}={_redact(str(value))}")
    return " ".join(parts)
