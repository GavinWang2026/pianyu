# Copyright (c) 2026 Hyht-Team. All rights reserved.
# See LICENSE (proprietary) and NOTICE.md (third-party components).
"""P0–P2 修复项的回归测试（对应验收报告 §05 / §09）。"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

# 测试隔离（HIST-01）：单测绝不写生产 outputs/avd_tasks.sqlite3，
# 否则 UI 任务历史会被 unit-* 幽灵行污染（26/26 点开全空）。
import os
import tempfile
os.environ.setdefault(
    "AVD_DB_PATH",
    str(Path(tempfile.mkdtemp(prefix="avd-test-db-")) / "tasks.sqlite3"),
)
# 素材库同样要隔离：库路径写死时单测会把「覆盖率样本」灌进真实素材库
os.environ.setdefault(
    "AVD_LIBRARY_DB",
    str(Path(tempfile.mkdtemp(prefix="avd-test-lib-")) / "library.sqlite3"),
)

import importlib.util

from fusion import avd_log, diagnostics, engine_opts, guards, hardening  # noqa: E402

# 直接加载 config 模块，避免 downloader/__init__ 拉起 yt_dlp
_cfg_spec = importlib.util.spec_from_file_location(
    "_avd_config_ut", APP_DIR / "downloader" / "config.py")
_cfg_mod = importlib.util.module_from_spec(_cfg_spec)
assert _cfg_spec and _cfg_spec.loader
_cfg_spec.loader.exec_module(_cfg_mod)
ConfigStore = _cfg_mod.ConfigStore
MAX_HOTWORDS = _cfg_mod.MAX_HOTWORDS


class ConfigPatchTests(unittest.TestCase):
    """P0-5：配置可写导致任意目录写入 / 输出模板注入。"""

    def test_rejects_template_path_traversal(self) -> None:
        for tpl in ("../escape.%(ext)s", "/tmp/x.%(ext)s", "a/b.%(ext)s",
                    "..%(ext)s", "x\x00y.%(ext)s"):
            with self.subTest(tpl=tpl):
                ok, why = hardening.validate_config_patch({"filename_template": tpl})
                self.assertFalse(ok, tpl)
                self.assertTrue(why)

    def test_rejects_system_download_dirs(self) -> None:
        for target in ("/", "/etc", "/System/Library", "/Users/<user>/.ssh",
                       "/Users/<user>/.workbuddy/secrets"):
            with self.subTest(target=target):
                ok, _ = hardening.validate_config_patch({"download_dir": target})
                self.assertFalse(ok, target)

    def test_accepts_legitimate_patch(self) -> None:
        # 不写盘；仅校验合法绝对路径，避免 /tmp 被「系统目录」规则拒绝
        download_dir = str(Path.home() / "Downloads")
        ok, why = hardening.validate_config_patch({
            "download_dir": download_dir,
            "filename_template": "%(title).120s [%(id)s].%(ext)s",
            "llm_base_url": "http://127.0.0.1:11434",
        })
        self.assertTrue(ok, why)

    def test_obsidian_must_be_under_home(self) -> None:
        ok, _ = hardening.validate_config_patch({"obsidian_dir": "/etc/obsidian"})
        self.assertFalse(ok)
        ok, why = hardening.validate_config_patch({
            "obsidian_dir": str(Path.home() / "Documents" / "ObsidianVault"),
            "playlist_max_entries": 10,
            "asr_hotwords": "术语A，术语B",
        })
        self.assertTrue(ok, why)
        ok, _ = hardening.validate_config_patch({"playlist_max_entries": 99})
        self.assertFalse(ok)


class HotwordAndLogTests(unittest.TestCase):
    def test_hotword_list_caps_and_dedupes(self) -> None:
        words = ConfigStore.hotword_list("甲，乙,甲\n丙")
        self.assertEqual(words, ["甲", "乙", "丙"])
        big = "，".join(f"词{i}" for i in range(MAX_HOTWORDS + 50))
        self.assertEqual(len(ConfigStore.hotword_list(big)), MAX_HOTWORDS)

    def test_avd_log_redacts_secrets(self) -> None:
        redacted = avd_log._redact("api_key=sk-secret123 cookie=abc token=xyz")
        self.assertNotIn("sk-secret123", redacted)
        self.assertIn("***", redacted)
        avd_log.info("unit-test ping", task="t1")
        self.assertTrue(avd_log.LOG_FILE.parent.is_dir())

    def test_live_clip_opts_and_validate(self) -> None:
        ok, _ = hardening.validate_config_patch({"live_record_seconds": 300})
        self.assertTrue(ok)
        ok, _ = hardening.validate_config_patch({"live_record_seconds": 0})
        self.assertTrue(ok)
        ok, _ = hardening.validate_config_patch({"live_record_seconds": -1})
        self.assertTrue(ok)
        ok, _ = hardening.validate_config_patch({"live_record_seconds": 10})
        self.assertFalse(ok)


class LlmEgressTests(unittest.TestCase):
    """P1-5：LLM base_url 形成第二处 SSRF + 数据外带。"""

    def test_allows_local_ollama(self) -> None:
        self.assertTrue(hardening.check_llm_base_url("http://127.0.0.1:11434")[0])
        self.assertTrue(hardening.check_llm_base_url("http://localhost:1234")[0])

    def test_blocks_private_and_metadata(self) -> None:
        for url in ("http://169.254.169.254/latest/meta-data/",
                    "http://192.168.1.10:11434", "http://10.0.0.5:8000",
                    "http://172.16.0.1:443"):
            with self.subTest(url=url):
                self.assertFalse(hardening.check_llm_base_url(url)[0], url)

    def test_blocks_bad_port_and_userinfo(self) -> None:
        self.assertFalse(hardening.check_llm_base_url("http://example.com:9999")[0])
        self.assertFalse(hardening.check_llm_base_url("http://u:p@example.com")[0])

    def test_allows_public_endpoint(self) -> None:
        original = guards._resolved_ips
        guards._resolved_ips = lambda host: ["93.184.216.34"]  # 稳定公网站点，无 DNS 依赖
        try:
            self.assertTrue(hardening.check_llm_base_url("https://api.example.com/v1")[0])
        finally:
            guards._resolved_ips = original


class ThumbCacheTests(unittest.TestCase):
    """P1-1：缓存无上限、无淘汰。"""

    def test_evicts_by_entry_count(self) -> None:
        now = time.time()
        cache = {f"u{i}": (now - i, "image/jpeg", b"x" * 100)
                 for i in range(hardening.THUMB_CACHE_MAX_ENTRIES + 20)}
        stat = hardening.trim_thumb_cache(cache, now)
        self.assertLessEqual(stat["entries"], hardening.THUMB_CACHE_MAX_ENTRIES)
        self.assertGreater(stat["evicted"], 0)

    def test_evicts_by_bytes(self) -> None:
        now = time.time()
        big = hardening.THUMB_CACHE_MAX_BYTES // 3
        cache = {f"u{i}": (now, "image/jpeg", b"x" * big) for i in range(5)}
        stat = hardening.trim_thumb_cache(cache, now)
        self.assertLessEqual(stat["bytes"], hardening.THUMB_CACHE_MAX_BYTES)

    def test_drops_expired(self) -> None:
        now = time.time()
        cache = {"old": (now - hardening.THUMB_TTL - 1, "image/jpeg", b"x"),
                 "fresh": (now, "image/jpeg", b"y")}
        stat = hardening.trim_thumb_cache(cache, now)
        self.assertEqual(stat["expired"], 1)
        self.assertNotIn("old", cache)


class DiagnosticsTests(unittest.TestCase):
    """P0 平台站点链路：错误必须可操作，不能甩英文原文。"""

    def test_douyin_cookie_error_is_actionable(self) -> None:
        item = diagnostics.diagnose(
            "ERROR: [Douyin] Fresh cookies (not necessarily logged in) are needed")
        self.assertEqual(item["code"], "COOKIE_REQUIRED")
        self.assertTrue(item["actionable"])
        self.assertTrue(any("cookies.txt" in a for a in item["actions"]))
        self.assertIn("设置", diagnostics.friendly(
            "Fresh cookies (not necessarily logged in) are needed"))

    def test_other_platform_errors(self) -> None:
        cases = {
            "PO token is required": "PO_TOKEN",
            "HTTP Error 403: Forbidden": "BLOCKED_403",
            "Unsupported URL": "UNSUPPORTED",
            "Login required": "LOGIN_REQUIRED",
            "Operation not permitted: Cookies.binarycookies": "COOKIE_PERMISSION",
            "Fresh cookies are needed; please report this issue on GitHub": "COOKIE_STALE",
            "something entirely new": "UNKNOWN",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(diagnostics.diagnose(raw)["code"], expected)

    def test_platform_hint_for_douyin(self) -> None:
        hint = diagnostics.platform_hint("https://v.douyin.com/abc/")
        self.assertEqual(hint["platform"], "抖音 / TikTok")
        self.assertIn("fresh_cookie", hint["needs"])

    def test_diagnose_url_blocks_private_target(self) -> None:
        from fusion import pipeline  # noqa: WPS433
        result = pipeline.diagnose_url("http://169.254.169.254/latest/meta-data/")
        self.assertFalse(result["ready"])
        self.assertEqual(result["diagnosis"]["code"], "BLOCKED_BY_POLICY")


class EngineOptsTests(unittest.TestCase):
    """P0 平台链路：cookie / 代理 / 裁剪参数必须贯通到融合层。"""

    def test_cookie_status_shape(self) -> None:
        status = engine_opts.cookie_status()
        for key in ("cookie_file", "cookie_file_present",
                    "cookies_from_browser", "authentication_ready"):
            self.assertIn(key, status)

    def test_cookie_file_is_confined_to_bundle(self) -> None:
        import os  # noqa: WPS433
        original = os.environ.get("AVD_COOKIE_FILE")
        os.environ["AVD_COOKIE_FILE"] = "/etc/passwd"
        try:
            self.assertIsNone(engine_opts.cookie_file(),
                              "包外文件绝不能被当作 Cookie 发给远端")
        finally:
            if original is None:
                os.environ.pop("AVD_COOKIE_FILE", None)
            else:
                os.environ["AVD_COOKIE_FILE"] = original

    def test_base_opts_inherits_config(self) -> None:
        opts = engine_opts.base_opts("https://www.douyin.com/video/123")
        for key in ("quiet", "noplaylist", "retries", "socket_timeout",
                    "skip_download", "nocheckcertificate"):
            self.assertIn(key, opts)
        self.assertFalse(opts["nocheckcertificate"])
        self.assertTrue(opts["skip_download"])


class ServerHardeningTests(unittest.TestCase):
    """P1-2 / P1-4 的静态契约。"""

    def test_worker_cap_is_bounded(self) -> None:
        self.assertLessEqual(hardening.LimitedThreadingHTTPServer.max_workers, 128)
        self.assertGreater(hardening.LimitedThreadingHTTPServer.max_workers, 0)

    def test_json_limit(self) -> None:
        self.assertEqual(hardening.MAX_JSON_BYTES, 1024 * 1024)
        self.assertTrue(issubclass(hardening.PayloadTooLarge, ValueError))

    def test_request_token_and_origin_guard(self) -> None:
        """D35：Token 走常量时间比较；错 Token / 无 Token / 非本机 Host 均拒绝。"""

        class _Handler:
            def __init__(self, headers):
                self.headers = headers

        old = os.environ.get("AVD_TOKEN")
        os.environ["AVD_TOKEN"] = "s3cret-token"
        try:
            ok, _ = guards.check_request(
                _Handler({"Host": "127.0.0.1:8760", "X-AVD-Token": "s3cret-token"}))
            self.assertTrue(ok)
            for headers in (
                {"Host": "127.0.0.1:8760", "X-AVD-Token": "wrong"},
                {"Host": "127.0.0.1:8760"},
                {"Host": "evil.example.com", "X-AVD-Token": "s3cret-token"},
                {"Host": "127.0.0.1:8760", "X-AVD-Token": "s3cret-token",
                 "Origin": "https://evil.example.com"},
            ):
                ok, _ = guards.check_request(_Handler(headers))
                self.assertFalse(ok, headers)
        finally:
            if old is None:
                os.environ.pop("AVD_TOKEN", None)
            else:
                os.environ["AVD_TOKEN"] = old


class PackagingContractTests(unittest.TestCase):
    """D23 / D24：交付脚本与文档不得再承诺 Python 3.10。"""

    def test_macos_launchers_require_311(self) -> None:
        for name in ("start.command", "启动旗舰版.command", "一键启动.command"):
            body = (APP_DIR / name).read_text(encoding="utf-8")
            self.assertIn("(3, 11)", body, name)
            self.assertNotIn("(3, 10)", body, name)
            self.assertNotIn("python3.10", body, name)
            self.assertIn("Python 3.11+", body, name)

    def test_app_builder_searches_python_and_states_asr_gap(self) -> None:
        body = (APP_DIR / "packaging/macos/build_app.sh").read_text(encoding="utf-8")
        self.assertIn("3,11", body)
        self.assertNotIn('exec python3 -c', body)
        self.assertIn(".venv-asr", body)
        self.assertIn("setup-asr.command", body)

    def test_readme_declares_python_311(self) -> None:
        body = (APP_DIR / "README-FLAGSHIP.md").read_text(encoding="utf-8")
        self.assertIn("Python 3.11+", body)


if __name__ == "__main__":
    unittest.main()
