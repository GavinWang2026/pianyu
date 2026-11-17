#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 Safari 的二进制 Cookie 库导出「仅抖音/TikTok 系」Netscape 格式 Cookie。

设计原则（不绕系统权限）：
  - 只读 Safari 的 Cookies.binarycookies（macOS 受 TCC 保护，需用户在
    系统设置 → 隐私与安全性 → 完全磁盘访问权限 中为本应用/终端授权）；
  - 只导出与抖音/TikTok 相关的域名（.douyin.com / .tiktok.com / .iesdouyin.com /
    .bytedance.com 等），不导出无关站点，最小化隐私外泄面；
  - 写入发布包内 cookies/cookies.txt（与 guards.within 同一约束，不出包）；
  - 绝不打印 Cookie 值；只打印域名/名称/过期状态摘要。

用法：
    python3 cookies/export_safari_cookies.py            # 默认写 cookies/cookies.txt
    python3 cookies/export_safari_cookies.py --dry      # 只统计、不写盘
依赖：仅标准库。
"""
from __future__ import annotations

import argparse
import struct
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
DEFAULT_OUT = APP / "cookies" / "cookies.txt"
SAFARI = Path.home() / "Library" / "Containers" / "com.apple.Safari" / "Data" \
    / "Library" / "Cookies" / "Cookies.binarycookies"

KEEP_DOMAINS = ("douyin", "tiktok", "iesdouyin", "bytedance", "byted", "pstatp")
COCOA_EPOCH = 978307200  # 2001-01-01 UTC 距 Unix 纪元的秒数


def read_cstr(data: bytes, off: int) -> str:
    if off < 0 or off >= len(data):
        return ""
    end = data.find(b"\x00", off)
    if end == -1:
        end = len(data)
    return data[off:end].decode("utf-8", "replace")


def parse_binary_cookies(data: bytes) -> list[dict]:
    if len(data) < 12 or data[:4] != b"cook":
        return []
    (num_pages,) = struct.unpack(">I", data[4:8])
    off = 8
    page_sizes = []
    for _ in range(num_pages):
        (sz,) = struct.unpack(">I", data[off:off + 4])
        page_sizes.append(sz)
        off += 4

    out: list[dict] = []
    for i in range(num_pages):
        page = data[off:off + page_sizes[i]]
        off += page_sizes[i]
        if len(page) < 8 or page[:4] != b"cook":
            continue
        (n,) = struct.unpack(">I", page[4:8])
        offsets = [struct.unpack(">I", page[8 + 4 * j:12 + 4 * j])[0]
                   for j in range(n)]
        for base in offsets:
            rec = page[base:]
            if len(rec) < 52:
                continue
            (csize,) = struct.unpack("<I", rec[0:4])
            if csize <= 0 or csize > len(rec):
                continue
            cookie = rec[:csize]
            flags = struct.unpack("<I", cookie[8:12])[0]
            urloff, nameoff, pathoff, valoff = struct.unpack("<IIII", cookie[16:32])
            # 标准 Safari binarycookies cookie 记录布局（小端）:
            # [0]size [4]version [8]flags [12]unknown
            # [16]url_off [20]name_off [24]path_off [28]value_off
            # [32]comment_url_off [36]unknown
            # [40:48]expiry(double, Cocoa纪元) [48:56]create(double)
            # [56:60]domain_off  ← 原脚本误放在 [48]，导致 domain 全错位
            exp_cocoa = struct.unpack("<d", cookie[40:48])[0]
            domoff = struct.unpack("<I", cookie[56:60])[0]

            domain = read_cstr(cookie, domoff)
            name = read_cstr(cookie, nameoff)
            path = read_cstr(cookie, pathoff)
            value = read_cstr(cookie, valoff)
            _url = read_cstr(cookie, urloff)

            exp_unix = 0
            if exp_cocoa > 0:
                exp_unix = int(exp_cocoa + COCOA_EPOCH)
            secure = "TRUE" if flags & 0x1 else "FALSE"
            out.append({
                "domain": domain,
                "include_sub": "TRUE" if domain.startswith(".") else "FALSE",
                "path": path or "/",
                "secure": secure,
                "expires": exp_unix,
                "name": name,
                "value": value,
            })
    return out


def to_netscape(cookies: list[dict]) -> str:
    lines = ["# Netscape HTTP Cookie File",
             "# Exported from Safari by AVD Flagship cookie exporter (douyin/tiktok only).",
             ""]
    for c in cookies:
        # Netscape 约定：HttpOnly 用域名前缀表示
        dom = c["domain"]
        lines.append("\t".join([
            dom, c["include_sub"], c["path"], c["secure"],
            str(c["expires"]), c["name"], c["value"],
        ]))
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Safari → Netscape Cookie 导出（仅抖音系）")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--dry", action="store_true", help="只统计，不写盘")
    args = ap.parse_args()

    if not SAFARI.exists():
        print(f"❌ 找不到 Safari Cookie 库：{SAFARI}", file=sys.stderr)
        return 1
    try:
        data = SAFARI.read_bytes()
    except PermissionError as e:
        print(f"❌ 无法读取 Safari Cookie 库（macOS TCC 完全磁盘访问权限未授予）：{e}",
              file=sys.stderr)
        print("   解决：系统设置 → 隐私与安全性 → 完全磁盘访问权限 → 开启本应用/终端，"
              "然后重试。", file=sys.stderr)
        return 2

    all_cookies = parse_binary_cookies(data)
    kept = [c for c in all_cookies
            if any(k in c["domain"].lower() for k in KEEP_DOMAINS)]
    print(f"从 Safari 共解析 {len(all_cookies)} 条 Cookie，"
          f"其中抖音/TikTok 系 {len(kept)} 条：")
    for c in kept[:40]:
        exp = c["expires"]
        state = "会话" if exp == 0 else ("已过期" if 0 < exp <= int(time.time()) else "有效")
        print(f"   {c['domain']:<30} {c['name']:<22} {state}")

    if not kept:
        print("❌ 未找到任何抖音/TikTok 系 Cookie，请确认已在 Safari 登录抖音。",
              file=sys.stderr)
        return 1

    if args.dry:
        print("DRY：未写盘。")
        return 0

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(to_netscape(kept), encoding="utf-8")
    print(f"✅ 已写入：{out}  ({len(kept)} 条)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
