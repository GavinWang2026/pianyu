# Copyright (c) 2026 Hyht-Team
# SPDX-License-Identifier: MIT
# See LICENSE (MIT) and NOTICE.md (third-party components).
"""yt-dlp 官方插件命名空间：extractor。

目录留空即为「没有自定义站点实现」—— 这是刻意的设计：
站点覆盖全部交给内嵌的上游 971 个 extractor，AVD 不 fork、不复制、
不承担站点反爬维护。将来若需要私有内网站点，只在本目录加一个
InfoExtractor 子类即可，上游源码仍是一行不改。
"""
