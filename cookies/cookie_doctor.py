#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AVD Flagship v2 · Cookie 文件体检（**绝不回显 Cookie 值**）。

检查发布包内的 Netscape 格式 Cookie 文件是否可用，并可顺带调用本地服务
的 /api/platform/diagnose 做真实链路复验。

安全约定：
  - 只显示域名 / 名称 / 是否过期 / 值长度，不打印值、不打印值哈希；
  - 只接受发布包目录内的文件（与 fusion/guards.within 同一约束）；
  - 只读，不修改、不移动、不上传任何文件。

退出码：0 可用 / 1 存在问题 / 2 服务不可用（仅 --probe 时）。
用法：
    python3 cookies/cookie_doctor.py
    python3 cookies/cookie_doctor.py --probe --url https://v.douyin.com/xxx/
    python3 cookies/cookie_doctor.py --selftest   # 用内存合成数据验证解析逻辑
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
DEFAULT_COOKIE = APP / "cookies" / "cookies.txt"
MAX_BYTES = 1024 * 1024
REQUIRED_HINT = "s_v_web_id"


def within(base: Path, candidate: str | Path) -> Path | None:
    """与 fusion/guards.within 等价的路径越界校验。"""
    path = Path(candidate).expanduser()
    if not path.is_absolute():
        path = base / path
    try:
        resolved = path.resolve()
        resolved.relative_to(base.resolve())
    except (ValueError, OSError):
        return None
    return resolved


def locate() -> tuple[Path | None, str]:
    import os
    raw = (os.environ.get("AVD_COOKIE_FILE") or "").strip()
    if raw:
        candidate = within(APP, raw)
        if candidate is None:
            return None, ("AVD_COOKIE_FILE 指向发布包外，已按安全约束拒绝；"
                          "请改用包内路径")
        return candidate, "AVD_COOKIE_FILE"
    return DEFAULT_COOKIE, "默认 cookies/cookies.txt"


def parse_netscape(text: str) -> tuple[list[dict], list[str]]:
    """解析 Netscape / curl jar 格式；只保留不含值明文的结构化摘要。"""
    entries: list[dict] = []
    problems: list[str] = []
    for lineno, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.rstrip("\r\n")
        if not line.strip():
            continue
        if line.startswith("#") and not line.startswith("#HttpOnly_"):
            continue
        domain_line = line[len("#HttpOnly_"):] if line.startswith(
            "#HttpOnly_") else line
        fields = domain_line.split("\t")
        if len(fields) < 7:
            fields = domain_line.split()
        if len(fields) < 7:
            problems.append(f"第 {lineno} 行字段不足 7 个，已跳过")
            continue
        domain, include_sub, path, secure, expires, name, value = fields[:7]
        try:
            expiry = int(expires)
        except ValueError:
            expiry = 0
        entries.append({
            "line": lineno,
            "domain": domain,
            "name": name,
            "value_len": len(value),
            "expires": expiry,
            "expired": 0 < expiry <= int(time.time()),
            "secure": secure.upper() == "TRUE",
            "path": path,
        })
    return entries, problems


def selftest() -> int:
    """用内存合成数据验证解析逻辑，不写任何文件、不代表真实凭据。"""
    sample = (
        "# Netscape HTTP Cookie File\n"
        ".douyin.com\tTRUE\t/\tTRUE\t1799999999\ts_v_web_id\tSYNTHETIC_VALUE_NOT_REAL\n"
        ".douyin.com\tTRUE\t/\tFALSE\t1799999999\tttwid\tALSO_SYNTHETIC\n"
        "#HttpOnly_.example.com\tTRUE\t/\tTRUE\t1799999999\tsession\tFAKE\n"
        "bad-line-without-enough-fields\n"
    )
    entries, problems = parse_netscape(sample)
    names = {e["name"] for e in entries}
    ok = ("s_v_web_id" in names and "session" in names
          and len(entries) == 3 and len(problems) == 1
          and all(e["value_len"] > 0 for e in entries))
    print("合成数据自检（非真实 Cookie，不写盘）：")
    print(f"  解析有效条目 {len(entries)} / 预期 3")
    print(f"  识别名称 {sorted(names)}")
    print(f"  格式问题 {len(problems)} / 预期 1")
    print(f"  含 {REQUIRED_HINT}: {'s_v_web_id' in names}")
    print("  自检结果:", "通过 ✅" if ok else "失败 ❌")
    return 0 if ok else 1


def probe_service(url: str, port: int) -> int:
    endpoint = f"http://127.0.0.1:{port}/api/platform/diagnose"
    body = json.dumps({"url": url}).encode("utf-8")
    req = urllib.request.Request(
        endpoint, data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"服务返回 HTTP {exc.code}：{exc.read()[:200].decode('utf-8', 'replace')}")
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"服务不可用（{type(exc).__name__}: {exc}）；"
              f"请先运行 python3 boot.py")
        return 2

    ready = bool(data.get("ready"))
    diag = data.get("diagnosis") or {}
    print(f"  真实链路复验: ready={'true' if ready else 'false'}"
          f" / code={diag.get('code', '—')}")
    print(f"  结论: {diag.get('title', '—')}")
    if not ready:
        for i, action in enumerate(diag.get("actions") or [], 1):
            print(f"    {i}. {action}")
    return 0 if ready else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Cookie 文件体检（不回显值）")
    parser.add_argument("--selftest", action="store_true",
                        help="用内存合成数据验证解析逻辑")
    parser.add_argument("--probe", action="store_true",
                        help="调用本地服务的 /api/platform/diagnose 复验")
    parser.add_argument("--url", default="https://v.douyin.com/nuBI2cMEa9c/",
                        help="--probe 使用的链接")
    parser.add_argument("--port", type=int, default=8760)
    args = parser.parse_args()

    if args.selftest:
        return selftest()

    path, origin = locate()
    print("AVD Flagship v2 · Cookie 文件体检（不显示任何 Cookie 值）")
    print(f"  来源: {origin}")
    if path is None:
        print(f"  ❌ {origin}")
        return 1
    print(f"  路径: {path}")
    if not path.is_file():
        print("  ❌ 文件不存在。请按 cookies/导出指引.md 放置 cookies.txt")
        print("     本机实测：Safari Cookie 库受 macOS TCC 保护，"
              "Chrome/Edge/Firefox profile 均不存在。")
        return 1
    size = path.stat().st_size
    print(f"  大小: {size} bytes")
    if size > MAX_BYTES:
        print(f"  ❌ 超过 {MAX_BYTES // 1024} KB 上限，疑似放错文件")
        return 1

    text = path.read_text(encoding="utf-8", errors="replace")
    entries, problems = parse_netscape(text)
    print(f"  有效条目: {len(entries)}")
    for problem in problems[:5]:
        print(f"  ⚠️  {problem}")

    domains = sorted({e["domain"] for e in entries})
    print(f"  涉及域名: {', '.join(domains) if domains else '（无）'}")
    relevant = [e for e in entries
                if "douyin" in e["domain"].lower()
                or "tiktok" in e["domain"].lower()]
    print("  --- 相关条目（只列名称与状态，不列值）---")
    for e in relevant[:20]:
        state = "已过期" if e["expired"] else "有效"
        print(f"    {e['domain']:<28} {e['name']:<22} {state:<6} "
              f"值长度 {e['value_len']}")

    checks = {
        "文件在发布包内": within(APP, path) is not None,
        "存在有效条目": len(entries) > 0,
        "包含抖音/TikTok 域": len(relevant) > 0,
        f"包含 {REQUIRED_HINT}": any(e["name"] == REQUIRED_HINT
                                     for e in relevant),
        "关键条目未过期": any(e["name"] == REQUIRED_HINT
                          and not e["expired"] for e in relevant),
    }
    gitignore = APP / ".gitignore"
    excluded = (gitignore.is_file()
                and "cookies/*.txt" in gitignore.read_text(
                    encoding="utf-8", errors="replace"))
    checks["已加入 .gitignore"] = excluded

    print("  --- 检查项 ---")
    for label, passed in checks.items():
        print(f"    {'✅' if passed else '❌'} {label}")

    ok = all(checks.values())
    print("  总体:", "可用 ✅" if ok else "不可用 ❌（请按导出指引补齐）")
    if not ok:
        print("  提示: 通过文件校验 ≠ 平台一定放行；"
              "签名 Cookie 过期或无效时仍会返回 COOKIE_STALE。")

    if args.probe:
        rc = probe_service(args.url, args.port)
        return rc if rc != 0 else (0 if ok else 1)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
