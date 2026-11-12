# Copyright (c) 2026 Hyht-Team. All rights reserved.
# See LICENSE (proprietary) and NOTICE.md (third-party components).
"""官方扩展点示例：给每条 info 挂上 AVD 溯源字段。

这是「上游零改动定制」的实证 —— 通过 yt_dlp_plugins 命名空间包注入，
不修改 vendor/yt_dlp 里任何一行源码，nightly 升级自动继承。

默认**无磁盘副作用**（只改内存里的 info）。设 AVD_WRITE_SIDECAR=1 时，
才在输出目录旁写一份 ``.avd-provenance.json``，便于交付留痕与合规举证。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from yt_dlp.postprocessor.common import PostProcessor

GENERATOR = "AVD Flagship v2.0.0"


class AVDProvenancePP(PostProcessor):
    """类名以 PP 结尾，yt-dlp 的插件发现机制会自动加载。"""

    def run(self, info: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
        info = dict(info or {})
        provenance = {
            "generator": GENERATOR,
            "engine": "yt-dlp 2026.10.26 (vendored unmodified)",
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "source": info.get("webpage_url") or info.get("url") or "",
            "extractor": info.get("extractor_key") or info.get("extractor") or "",
        }
        info["avd_provenance"] = provenance

        if os.environ.get("AVD_WRITE_SIDECAR") == "1":
            try:
                filepath = info.get("filepath") or info.get("_filename")
                if filepath:
                    sidecar = Path(str(filepath)).with_suffix(".avd-provenance.json")
                    sidecar.write_text(
                        json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
            except OSError:
                pass  # 留痕失败不影响下载
        return [], info
