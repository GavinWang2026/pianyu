# Copyright (c) 2026 Hyht-Team. All rights reserved.
# See LICENSE (proprietary) and NOTICE.md (third-party components).
"""AVD Flagship 融合层。

把上游 yt-dlp（内嵌引擎）与 AVD 自研工作台接成一条产线，
不改上游一行源码。分层职责：

    boot_check   引擎装配与版本闸门
    guards       合规边界与出口安全
    knowledge    结构化知识稿构建
    pipeline     一次解析、全链复用的流水线
    api          HTTP 扩展路由

本包只依赖 Python 标准库。
"""

from __future__ import annotations

PRODUCT_NAME = "AVD Flagship"
PRODUCT_VERSION = "2.0.0"
ENGINE_VERSION = "2026.10.26"

__all__ = ["PRODUCT_NAME", "PRODUCT_VERSION", "ENGINE_VERSION"]
