# Copyright (c) 2026 Hyht-Team. All rights reserved.
# See LICENSE (proprietary) and NOTICE.md (third-party components).
"""D34：前端关键路径冒烟（静态契约 + 真实 HTTP 取页）。

不依赖浏览器：校验前端调用的每个 /api 路由后端都注册了、每个 data-i18n
键在 zh/en 词典里都存在，并起一个回环服务把首屏与健康检查真正取一遍。
"""

from __future__ import annotations

import http.client
import json
import re
import sys
import threading
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

_STATIC = APP_DIR / "static"
_FRONTEND_FILES = ("index.html", "app.js", "flagship.js", "i18n.js")
_API_RE = re.compile(r"""['"](/api/[a-zA-Z0-9/_-]+)""")
_I18N_RE = re.compile(r'data-i18n(?:-aria)?="([^"]+)"')


def _read(name: str) -> str:
    return (_STATIC / name).read_text(encoding="utf-8")


def _backend_routes() -> set[str]:
    """从服务端源码收集已注册路由（含 startswith 前缀）。"""
    routes: set[str] = set()
    for rel in ("server.py", "fusion/api.py"):
        text = (APP_DIR / rel).read_text(encoding="utf-8")
        routes.update(re.findall(r'"(/api/[a-zA-Z0-9/_-]+)"', text))
    return routes


class FrontendStaticContractTests(unittest.TestCase):
    def test_every_frontend_api_route_exists_in_backend(self) -> None:
        called: set[str] = set()
        for name in _FRONTEND_FILES:
            called.update(_API_RE.findall(_read(name)))
        self.assertTrue(called, "前端没有解析到任何 /api 调用，检查正则是否失效")
        missing = sorted(r for r in called if r not in _backend_routes())
        self.assertEqual(missing, [], f"前端调用了后端不存在的路由：{missing}")

    def test_every_i18n_key_has_both_languages(self) -> None:
        used: set[str] = set()
        for name in ("index.html", "flagship.js", "app.js"):
            used.update(_I18N_RE.findall(_read(name)))
        self.assertTrue(used, "没有解析到 data-i18n 键")

        body = _read("i18n.js")
        zh = body.split("zh: {", 1)[1].split("\n    },", 1)[0]
        en = body.split("en: {", 1)[1].split("\n    },", 1)[0]
        zh_keys = set(re.findall(r"'([a-z][\w.-]*)':", zh))
        en_keys = set(re.findall(r"'([a-z][\w.-]*)':", en))
        self.assertEqual(sorted(used - zh_keys), [], "中文词典缺键")
        self.assertEqual(sorted(used - en_keys), [], "英文词典缺键")
        self.assertEqual(sorted(zh_keys ^ en_keys), [], "zh/en 词典键不一致")

    def test_critical_flows_are_wired(self) -> None:
        flagship = _read("flagship.js")
        app = _read("app.js")
        for marker in ("/api/pipeline/start", "/api/pipeline/progress",
                       "/api/pipeline/cancel", "/api/pipeline/result",
                       "/api/library/ask", "/api/fusion/health"):
            self.assertIn(marker, flagship, f"旗舰版前端缺少关键路径：{marker}")
        self.assertIn("EventSource", flagship, "进度应走 SSE")
        for marker in ("/api/parse", "/api/download", "/api/asr/status",
                       "/api/polish/status"):
            self.assertIn(marker, app, f"原版前端缺少关键路径：{marker}")


class FrontendLiveSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            from fusion import boot_check, hardening
            boot_check.activate()
            import server as web
        except Exception as exc:  # pragma: no cover - 缺运行时
            raise unittest.SkipTest(f"HTTP 栈不可用：{exc}")
        cls.httpd = hardening.LimitedThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _get(self, path: str) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read()
            headers = {k.lower(): v for k, v in resp.getheaders()}
            return resp.status, headers, body
        finally:
            conn.close()

    def test_index_and_assets_load(self) -> None:
        for path, needle in (("/", b"<!doctype html"),
                             ("/static/flagship.js", b"pipeline/start"),
                             ("/static/i18n.js", b"AVD_I18N")):
            status, headers, body = self._get(path)
            self.assertEqual(status, 200, path)
            self.assertTrue(headers.get("content-type", ""), path)
            self.assertIn(needle, body.lower() if path == "/" else body, path)

    def test_health_reports_persist(self) -> None:
        status, _, body = self._get("/api/fusion/health")
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertIn("health", data)
        self.assertIn("persist", data["health"])


if __name__ == "__main__":
    unittest.main()
