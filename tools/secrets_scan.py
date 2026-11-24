#!/usr/bin/env python3
# Copyright (c) 2026 Hyht-Team
# SPDX-License-Identifier: MIT
"""交付前 secrets 扫描：打包产物不得含真实 Cookie / 会话字段（SEC-01）。

用法::

    python3 tools/secrets_scan.py              # 默认 strict：命中即退出码 1
    python3 tools/secrets_scan.py --lenient    # 只报告，不改变退出码
    python3 tools/secrets_scan.py --root dist/macos/*.app/Contents/Resources/app

第九轮修订（2026-11-14）：原先「非 strict 时 rc=0」+ 未进 CI，等于形同虚设；
现在默认即 strict，并接入 CI 与 ``packaging/macos/build_app.sh`` 出包自检。
同时不再只认文件名：任何以 Netscape Cookie 头开头的文件（被改名的副本也算）
都会进入扫描范围。

只读扫描，不改文件。凭据占位模板（含 ``<YOUR_VALUE>`` / 空值）不算命中。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 真实凭据字段名（Netscape / JSON / RTF 导出常见）
_FIELD_RE = re.compile(
    r"(?i)\b(ttwid|passport_csrf_token|odin_tt|sessionid|s_v_web_id|sid_tt|uid_tt)\b"
)
# 跳过说明文档与本扫描脚本自身 —— 只扫「可能含真值」的文件
_SKIP_DIR = {".git", "vendor", "runtime", "models", ".venv-asr", ".venv",
             "__pycache__", "node_modules", "outputs", "downloads", ".uploads",
             "evidence", ".state", "dist"}
_SKIP_SUFFIX = {".pyc", ".pyo", ".so", ".dylib", ".bin", ".pt", ".onnx",
                ".safetensors", ".mp4", ".mkv", ".wav", ".jpg", ".png", ".webp"}
# 文档/报告里提字段名不算泄露；只扫凭据容器与配置
_CREDENTIAL_NAMES = {
    "cookies.txt", "cookies.rtf", "cookies.json", "cookies.har",
    "config.json", ".env",
}
_CREDENTIAL_SUFFIX = {".txt", ".json", ".rtf", ".har", ".env"}
# Netscape Cookie 文件标准头；被改名的副本靠它识别
_NETSCAPE_HEAD = "# Netscape HTTP Cookie File"
_MAX_SCAN_BYTES = 4 * 1024 * 1024


def _looks_like_placeholder(line: str) -> bool:
    low = line.lower()
    return (
        "<your_value>" in low
        or "<实际值>" in line
        or "占位" in line
        or "example" in low
        or "请替换" in line
        or "YOUR_" in line
    )


def _has_real_value(line: str, field: str) -> bool:
    """判断字段是否带有「像真凭据」的值（排除文档提及）。"""
    # Netscape：至少 7 列，末列为 value
    parts = line.split("\t")
    if len(parts) >= 7 and parts[-2].strip().lower() == field.lower():
        value = parts[-1].strip()
        return bool(value) and not value.startswith("<") and len(value) >= 8
    # name=value / "name": "value"
    m = re.search(
        rf'(?i)(?:^|[^\w]){re.escape(field)}\s*[=:]\s*["\']?([^\s"\';,]+)',
        line,
    )
    if not m:
        return False
    value = m.group(1).strip().strip("'\"")
    if _looks_like_placeholder(value) or value in ("0", "null", "None", "true", "false"):
        return False
    return len(value) >= 8


def _is_credential_container(path: Path) -> bool:
    """按文件名判定常规凭据容器。"""
    if path.name in _CREDENTIAL_NAMES:
        return True
    return "cookies" in path.parts and path.suffix.lower() in _CREDENTIAL_SUFFIX


def scan(root: Path) -> list[tuple[str, int, str]]:
    hits: list[tuple[str, int, str]] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIR for part in path.parts):
            continue
        if path.suffix.lower() in _SKIP_SUFFIX:
            continue
        if path.resolve() == Path(__file__).resolve():
            continue
        try:
            if path.stat().st_size > _MAX_SCAN_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # 改名副本兜底：Netscape 头的文件一律按凭据容器处理
        if not (_is_credential_container(path) or text.lstrip().startswith(_NETSCAPE_HEAD)):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if _looks_like_placeholder(line):
                continue
            for m in _FIELD_RE.finditer(line):
                field = m.group(1)
                if not _has_real_value(line, field):
                    continue
                rel = str(path.relative_to(root))
                hits.append((rel, i, line.strip()[:120]))
                break
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(description="AVD 发布包 secrets 扫描")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--lenient", action="store_true",
        help="只报告不改退出码（默认 strict）",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="兼容旧调用；默认已是 strict",
    )
    args = parser.parse_args()
    hits = scan(args.root.resolve())
    if not hits:
        print("secrets_scan: OK（未发现真实凭据字段值）")
        return 0
    print(f"secrets_scan: 发现 {len(hits)} 处可疑凭据痕迹：")
    for rel, lineno, snippet in hits[:40]:
        print(f"  {rel}:{lineno}: {snippet}")
    if len(hits) > 40:
        print(f"  … 另有 {len(hits) - 40} 处")
    return 0 if args.lenient else 1


if __name__ == "__main__":
    raise SystemExit(main())
