"""本地 HTTP 服务：路由、JSON API、SSE 进度推流。

刻意只依赖标准库（``http.server`` + ``threading``），第三方依赖收敛到 yt-dlp 一个包，
以规避目标机器上可能出现的 pip / 版本兼容问题。

安全模型：
  * 仅监听 ``127.0.0.1``，不暴露到局域网
  * 写操作校验 ``Origin``，阻断浏览器发起的跨站请求（CSRF）
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from downloader import asr, config, friendly_error, manager, polish, probe
from fusion import api as fusion_api
from fusion import guards
from fusion import hardening

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
UPLOAD_DIR = APP_DIR / ".uploads"

# 拖入的本地媒体文件上限（流式落盘，不占内存）
MAX_UPLOAD_BYTES = 4 * 1024 * 1024 * 1024

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}

# 图床防盗链：不同站点需要携带各自的 Referer 才能取到缩略图
_REFERER_RULES: tuple[tuple[str, str], ...] = (
    ("hdslb.com", "https://www.bilibili.com/"),
    ("bilivideo.com", "https://www.bilibili.com/"),
    ("ytimg.com", "https://www.youtube.com/"),
    ("ggpht.com", "https://www.youtube.com/"),
    ("douyinpic.com", "https://www.douyin.com/"),
    ("douyincdn.com", "https://www.douyin.com/"),
    ("xhscdn.com", "https://www.xiaohongshu.com/"),
    ("sinaimg.cn", "https://weibo.com/"),
    ("qpic.cn", "https://mp.weixin.qq.com/"),
    ("kwaicdn.com", "https://www.kuaishou.com/"),
)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_thumb_cache: dict[str, tuple[float, str, bytes]] = {}
_thumb_lock = threading.Lock()
_THUMB_TTL = 3600


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _referer_for(url: str) -> str:
    host = urllib.parse.urlparse(url).hostname or ""
    for needle, referer in _REFERER_RULES:
        if needle in host:
            return referer
    return ""


def _reveal_in_file_manager(target: str) -> bool:
    """在系统文件管理器中定位文件 / 打开目录。"""
    path = Path(target).expanduser()
    try:
        if sys.platform == "darwin":
            if path.is_dir():
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["open", "-R", str(path)])
        elif os.name == "nt":
            if path.is_dir():
                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path if path.is_dir() else path.parent)])
        return True
    except Exception:
        return False


def _open_with_default_app(target: str) -> bool:
    path = Path(target).expanduser()
    if not path.exists():
        return False
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        elif os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return True
    except Exception:
        return False


def _fetch_thumbnail(url: str) -> tuple[str, bytes]:
    """服务端代取缩略图，绕过浏览器侧的防盗链与 CORS 限制。"""
    now = time.time()
    with _thumb_lock:
        cached = _thumb_cache.get(url)
        if cached and now - cached[0] < _THUMB_TTL:
            return cached[1], cached[2]

    cfg = config.as_dict()
    proxy = cfg.get("proxy_url") if cfg.get("proxy_mode") == "always" else None
    if cfg.get("proxy_mode") == "auto":
        from downloader import resolve_proxy

        proxy = resolve_proxy(url, cfg)

    headers = {"User-Agent": _UA, "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}
    referer = _referer_for(url)
    if referer:
        headers["Referer"] = referer

    request = urllib.request.Request(url, headers=headers)
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    else:
        opener = urllib.request.build_opener()

    with opener.open(request, timeout=15) as response:
        content_type = response.headers.get("Content-Type", "image/jpeg")
        body = response.read(6 * 1024 * 1024)

    with _thumb_lock:
        _thumb_cache[url] = (now, content_type, body)
        hardening.trim_thumb_cache(_thumb_cache, now)
    return content_type, body


_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_DURATION_RE = re.compile(r"^(\d+):")

# 繁体字幕的语言标记。只有拿到繁体字幕时才需要额外做一次繁转简。
_TRADITIONAL_MARKERS = ("Hant", "TW", "HK", "MO")


def _needs_simplified(task: Optional[dict]) -> bool:
    """判断这次文案是否需要额外做繁转简。

    语音识别侧 worker 已经统一输出简体；只有「平台字幕」这条路
    可能拿到繁体（我们优先挑简体轨道，但对方只提供繁体时也收下），
    这时才值得多跑一次转换。
    """
    if not task:
        return False
    if task.get("language") not in ("zh", "auto"):
        return False
    if task.get("source_kind") != "subtitle":
        return False
    lang = str(task.get("subtitle_lang") or "")
    return any(marker in lang for marker in _TRADITIONAL_MARKERS)


# ---------------------------------------------------------------------------
# 请求处理器
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "VideoDawnload/1.0"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------ 基础

    def setup(self) -> None:
        super().setup()
        try:
            self.connection.setsockopt(6, 1, 1)  # IPPROTO_TCP, TCP_NODELAY
        except OSError:
            pass

    def log_message(self, fmt: str, *args: Any) -> None:  # 收敛默认的逐请求日志
        if os.environ.get("VD_VERBOSE"):
            super().log_message(fmt, *args)

    # ------------------------------------------------------------------ 响应助手

    def _send_bytes(self, body: bytes, content_type: str, status: int = 200, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(body, "application/json; charset=utf-8", status)

    def _ok(self, **kwargs: Any) -> None:
        self._send_json({"ok": True, **kwargs})

    def _fail(self, message: str, status: int = 400) -> None:
        self._send_json({"ok": False, "error": message}, status)

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        if length > hardening.MAX_JSON_BYTES:
            raise hardening.PayloadTooLarge(
                f"请求体过大，上限 {hardening.MAX_JSON_BYTES // 1024} KB")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _origin_allowed(self) -> bool:
        """阻断跨站写操作。同源请求（或本地工具、无 Origin 的请求）放行。"""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host") or ""
        return origin in (f"http://{host}", f"https://{host}")

    # ------------------------------------------------------------------ 路由

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        ok, reason = guards.check_request(self)
        if not ok:
            self._fail(reason, HTTPStatus.FORBIDDEN)
            return

        if route.startswith("/api/"):
            self._handle_api_get(route, query)
            return
        self._serve_static(route)

    def do_HEAD(self) -> None:  # noqa: N802
        """HEAD 只回响应头，绝不写响应体（P2-1）。

        原版直接复用 do_GET：/api/progress 这类 SSE 路由会把事件流写进
        HEAD 响应，既违反 HTTP 语义，也会把连接挂住。
        """
        route = urllib.parse.urlparse(self.path).path
        if route.startswith("/api/"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        relative = "index.html" if route in ("/", "") else route.lstrip("/")
        if relative.startswith("static/"):
            relative = relative[len("static/"):]
        candidate = (STATIC_DIR / relative).resolve()
        safe = guards.within(STATIC_DIR, candidate)
        if safe is None or not safe.is_file():
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header(
            "Content-Type",
            mimetypes.guess_type(str(safe))[0] or "application/octet-stream",
        )
        self.send_header("Content-Length", str(safe.stat().st_size))
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path

        if not route.startswith("/api/"):
            self._fail("未知接口", HTTPStatus.NOT_FOUND)
            return
        ok, reason = guards.check_request(self)
        if not ok:
            self._fail(reason, HTTPStatus.FORBIDDEN)
            return

        # 二进制上传不是 JSON，必须在读取 body 之前分流
        if route == "/api/asr/upload":
            self._handle_asr_upload(parsed.query)
            return

        try:
            self._handle_api_post(route)
        except hardening.PayloadTooLarge as exc:
            self._fail(str(exc), HTTPStatus.REQUEST_ENTITY_TOO_LARGE)

    # ------------------------------------------------------------------ 静态资源

    def _serve_static(self, route: str) -> None:
        if route in ("/", ""):
            relative = "index.html"
        else:
            relative = route.lstrip("/")
            if relative.startswith("static/"):
                relative = relative[len("static/"):]
        candidate = (STATIC_DIR / relative).resolve()
        try:
            candidate.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self._fail("非法路径", HTTPStatus.FORBIDDEN)
            return
        if not candidate.is_file():
            self._fail("资源不存在", HTTPStatus.NOT_FOUND)
            return
        mime = MIME_TYPES.get(candidate.suffix.lower(), "application/octet-stream")
        cache = "no-store" if candidate.suffix == ".html" else "public, max-age=300"
        self._send_bytes(candidate.read_bytes(), mime, cache=cache)

    # ------------------------------------------------------------------ GET API

    def _handle_api_get(self, route: str, query: dict) -> None:
        if fusion_api.handle_get(self, route, query):
            return

        if route == "/api/health":
            self._ok(version="2.0.0", product="AVD Flagship",
                     engine="yt-dlp 2026.10.26 (vendored unmodified)")

        elif route == "/api/config":
            self._ok(config=guards.redact_config(config.as_dict()), env_proxy=config.env_proxy())

        elif route == "/api/tasks":
            self._ok(tasks=manager.list_tasks())

        elif route == "/api/thumb":
            url = (query.get("url") or [""])[0]
            if not url.startswith(("http://", "https://")):
                self._fail("缺少合法的图片地址")
                return
            ok, why = guards.check_image_url(url)
            if not ok:
                self._fail(why, HTTPStatus.FORBIDDEN)
                return
            try:
                content_type, body = _fetch_thumbnail(url)
            except Exception:
                # 缩略图取不到不是致命问题，返回 1x1 透明占位
                transparent = (
                    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
                    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
                    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
                )
                self._send_bytes(transparent, "image/png", cache="no-store")
                return
            self._send_bytes(body, content_type, cache="public, max-age=3600")

        elif route == "/api/progress":
            task_id = (query.get("id") or [""])[0]
            self._stream_progress(task_id)

        elif route == "/api/asr/status":
            engines = asr.detect_engines()
            self._ok(
                available=asr.is_available(),
                model=asr.DEFAULT_MODEL,
                languages=list(asr.LANGUAGES),
                modes=[
                    {"id": "auto", "label": "自动（有字幕用字幕）"},
                    {"id": "subtitle", "label": "仅用平台字幕"},
                    {"id": "asr", "label": "仅用语音识别"},
                ],
                engine_choices=[
                    {"id": "auto", "label": "自动选择"},
                    {"id": "mlx", "label": "MLX（Apple 芯片）"},
                    {"id": "faster", "label": "Faster-Whisper（通用）"},
                ],
                engines=engines,
                prefer_subtitle=bool(config.get("prefer_subtitle", True)),
                default_engine=str(config.get("asr_engine") or "auto"),
                max_upload=MAX_UPLOAD_BYTES,
            )

        elif route == "/api/asr/tasks":
            self._ok(tasks=asr.manager.list_tasks())

        elif route == "/api/asr/segments":
            task_id = (query.get("id") or [""])[0]
            task = asr.manager.get_task(task_id)
            if task is None:
                self._fail("任务不存在", HTTPStatus.NOT_FOUND)
                return
            self._ok(task=task, segments=asr.manager.get_segments(task_id))

        elif route == "/api/asr/progress":
            task_id = (query.get("id") or [""])[0]
            self._stream_progress(task_id, asr.manager)

        elif route == "/api/polish/status":
            self._ok(capability=polish.probe(), suggested_base_url=polish.env_hint())

        elif route == "/api/polish/progress":
            task_id = (query.get("id") or [""])[0]
            self._stream_progress(task_id, polish.manager)

        else:
            self._fail("未知接口", HTTPStatus.NOT_FOUND)

    # ------------------------------------------------------------------ POST API

    def _handle_api_post(self, route: str) -> None:
        payload = self._read_json()

        if fusion_api.handle_post(self, route, payload):
            return

        # 原版路由的安全底线：禁内网 / 本机 / 非常规端口 / DNS rebinding。
        # 合规站点白名单只在旗舰版一键流水线里启用，不破坏原版既有用途。
        if route in ("/api/parse", "/api/download", "/api/asr/start"):
            raw_url = str(payload.get("url") or "").strip()
            if raw_url:
                ok, why = guards.check_media_url(raw_url, enforce_allowlist=False)
                if not ok:
                    self._fail(why, HTTPStatus.FORBIDDEN)
                    return

        if route == "/api/parse":
            url = str(payload.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                self._fail("请输入以 http:// 或 https:// 开头的视频链接")
                return
            try:
                info = probe(url, config.as_dict())
                self._ok(info=info)
            except Exception as exc:  # noqa: BLE001
                self._fail(friendly_error(exc))
            return

        if route == "/api/download":
            url = str(payload.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                self._fail("请输入以 http:// 或 https:// 开头的视频链接")
                return
            audio_only = bool(payload.get("audio_only"))
            raw_height = payload.get("height")
            height: Optional[int] = None
            if not audio_only and raw_height not in (None, "", "auto"):
                try:
                    height = int(raw_height)
                except (TypeError, ValueError):
                    height = None

            format_id = str(payload.get("format_id") or "")[:64]
            audio_format_id = str(payload.get("audio_format_id") or "")[:64]
            # 分离式 video+audio 需要两条流，提前告知前端，避免进度条来回跳
            stream_count = 2 if (not audio_only and format_id and audio_format_id) else 1

            task = manager.create_task(
                url,
                height=height,
                audio_only=audio_only,
                # format_id 由引擎侧再做白名单校验，这里只做类型收敛
                format_id=format_id,
                audio_format_id=audio_format_id,
                resolution=str(payload.get("resolution") or "")[:32],
                tier=int(payload.get("tier") or 0),
                stream_count=stream_count,
                title=str(payload.get("title") or "")[:300],
                thumbnail=str(payload.get("thumbnail") or "")[:800],
                extractor=str(payload.get("extractor") or "")[:64],
                label=str(payload.get("label") or "")[:40],
            )
            self._ok(task=task)
            return

        if route == "/api/cancel":
            task_id = str(payload.get("id") or "")
            self._ok(cancelled=manager.cancel_task(task_id))
            return

        if route == "/api/clear":
            self._ok(cleared=manager.clear_finished())
            return

        if route == "/api/config":
            # 前端拿到的是脱敏后的 "***"；保存时忽略哨兵值，避免把真 key 覆盖掉
            clean = {k: v for k, v in payload.items()
                     if not (isinstance(v, str) and v == "***")}
            ok, why = hardening.validate_config_patch(clean)
            if not ok:
                self._fail(why, HTTPStatus.FORBIDDEN)
                return
            updated = config.update(clean)
            self._ok(config=guards.redact_config(updated))
            return

        if route == "/api/reveal":
            target = str(payload.get("path") or "").strip()
            base = Path(str(config.get("download_dir") or APP_DIR / "downloads"))
            if not target:
                target = str(base)
            safe = guards.within(base, target)
            if safe is None:
                self._fail("路径超出下载目录", HTTPStatus.FORBIDDEN)
                return
            self._ok(revealed=_reveal_in_file_manager(str(safe)))
            return

        if route == "/api/open-file":
            target = str(payload.get("path") or "").strip()
            if not target:
                self._fail("缺少文件路径")
                return
            base = Path(str(config.get("download_dir") or APP_DIR / "downloads"))
            safe = guards.within(base, target)
            if safe is None:
                self._fail("路径超出下载目录", HTTPStatus.FORBIDDEN)
                return
            self._ok(opened=_open_with_default_app(str(safe)))
            return

        if route == "/api/asr/start":
            self._start_asr_task(
                source=str(payload.get("url") or "").strip(),
                is_url=True,
                language=str(payload.get("language") or "zh"),
                title=str(payload.get("title") or ""),
                thumbnail=str(payload.get("thumbnail") or ""),
                extractor=str(payload.get("extractor") or ""),
                mode=str(payload.get("mode") or "auto"),
                engine=str(payload.get("engine") or ""),
            )
            return

        if route == "/api/asr/cancel":
            task_id = str(payload.get("id") or "")
            self._ok(cancelled=asr.manager.cancel_task(task_id))
            return

        if route == "/api/asr/clear":
            self._ok(cleared=asr.manager.clear_finished())
            return

        if route == "/api/polish/start":
            self._start_polish_task(payload)
            return

        if route == "/api/polish/cancel":
            task_id = str(payload.get("id") or "")
            self._ok(cancelled=polish.manager.cancel_task(task_id))
            return

        if route == "/api/polish/clear":
            self._ok(cleared=polish.manager.clear_finished())
            return

        if route == "/api/llm/models":
            self._list_llm_models(payload)
            return

        self._fail("未知接口", HTTPStatus.NOT_FOUND)

    # ------------------------------------------------------------------ 文案整理

    def _start_polish_task(self, payload: dict) -> None:
        """创建文案整理任务。

        优先按 ``source_id`` 从文案任务里取原文，省掉把整篇文稿再传一遍；
        也接受直接传 ``segments``（例如用户只想要本地整理，或前端持有自定义文本）。
        """
        source_id = str(payload.get("source_id") or "")
        segments = payload.get("segments")

        if not segments and source_id:
            task = asr.manager.get_task(source_id)
            if task is None:
                self._fail("文案任务不存在或已过期")
                return
            segments = asr.manager.get_segments(source_id)
            if not segments:
                self._fail("该文案任务还没有可整理的内容")
                return

        if not isinstance(segments, list) or not segments:
            self._fail("没有可整理的文案内容")
            return

        cleaned_segments = []
        for item in segments[:20000]:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            cleaned_segments.append({
                "text": text[:2000],
                "start": float(item.get("start") or 0.0),
                "end": float(item.get("end") or 0.0),
            })
        if not cleaned_segments:
            self._fail("没有可整理的文案内容")
            return

        source_task = asr.manager.get_task(source_id) if source_id else None
        language = str((source_task or {}).get("language") or payload.get("language") or "zh")

        operations = payload.get("operations")
        if not isinstance(operations, list):
            operations = None

        try:
            task = polish.manager.create_task(
                cleaned_segments,
                source_id=source_id,
                title=str((source_task or {}).get("title") or payload.get("title") or "")[:300],
                language=language,
                operations=operations,
                convert_simplified=_needs_simplified(source_task),
            )
        except Exception as exc:  # noqa: BLE001
            self._fail(friendly_error(exc))
            return
        self._ok(task=task)

    def _list_llm_models(self, payload: dict) -> None:
        """拉取 OpenAI 兼容服务的模型清单，供设置页下拉选择。"""
        base_url = str(payload.get("base_url") or "").strip()
        api_key = str(payload.get("api_key") or "").strip()
        if not base_url:
            # 没填就用当前配置，避免用户还得再抄一遍
            base_url = str(config.get("llm_base_url") or "")
            api_key = api_key or str(config.get("llm_api_key") or "")
        try:
            models = polish.list_models(base_url, api_key)
        except Exception as exc:  # noqa: BLE001
            self._fail(friendly_error(exc))
            return
        self._ok(models=models)

    # ------------------------------------------------------------------ 文案提取

    def _start_asr_task(
        self,
        *,
        source: str,
        is_url: bool,
        is_upload: bool = False,
        language: str = "zh",
        title: str = "",
        thumbnail: str = "",
        extractor: str = "",
        mode: str = "auto",
        engine: str = "",
    ) -> None:
        """创建一个文案提取任务（URL 与本地文件共用）。"""
        if not source:
            self._fail("缺少视频链接或文件")
            return
        if is_url and not source.startswith(("http://", "https://")):
            self._fail("请输入以 http:// 或 https:// 开头的视频链接")
            return

        try:
            task = asr.manager.create_task(
                source,
                is_url=is_url,
                is_upload=is_upload,
                language=language,
                title=title[:300],
                thumbnail=thumbnail[:800],
                extractor=extractor[:64],
                mode=mode if mode in asr.MODES else "auto",
                engine=(engine or str(config.get("asr_engine") or "auto")),
            )
        except Exception as exc:  # noqa: BLE001
            self._fail(friendly_error(exc))
            return
        self._ok(task=task)

    def _handle_asr_upload(self, query: str) -> None:
        """接收浏览器拖入的本地媒体文件（原始二进制 body，文件名走请求头）。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self._fail("上传内容为空")
            return
        if length > MAX_UPLOAD_BYTES:
            self._fail(f"文件过大，上限 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
            return

        raw_name = self.headers.get("X-File-Name") or "media"
        try:
            decoded = urllib.parse.unquote(raw_name)
        except Exception:  # noqa: BLE001
            decoded = "media"
        # 只取文件名，剔除任何路径成分，防止目录穿越
        safe_name = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", Path(decoded).name)[:80] or "media"

        try:
            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._fail(f"无法创建上传目录：{exc}")
            return

        dest = UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}_{safe_name}"
        written = 0
        try:
            with dest.open("wb") as handle:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(262144, remaining))
                    if not chunk:
                        break
                    handle.write(chunk)
                    written += len(chunk)
                    remaining -= len(chunk)
        except OSError as exc:
            self._fail(f"写入上传文件失败：{exc}")
            return

        if written == 0:
            self._fail("上传内容为空")
            return
        if written != length:
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
            self._fail(f"上传不完整（已收 {written}/{length} 字节），请重试",
                       HTTPStatus.BAD_REQUEST)
            return

        params = urllib.parse.parse_qs(query or "")
        language = (params.get("language") or ["zh"])[0]
        mode = (params.get("mode") or ["asr"])[0]     # 本地文件没有平台字幕，直接走语音
        engine = (params.get("engine") or [""])[0]

        self._start_asr_task(
            source=str(dest),
            is_url=False,
            is_upload=True,
            language=language,
            title=Path(safe_name).stem,
            mode=mode,
            engine=engine,
        )

    # ------------------------------------------------------------------ SSE

    def _write_chunk(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):X}\r\n".encode("ascii"))
        self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _stream_progress(self, task_id: str, mgr=None) -> None:
        """把任务事件流以 SSE 推送出去。下载与文案提取共用同一套实现。"""
        store = mgr if mgr is not None else manager
        if not task_id or store.get_task(task_id) is None:
            self._fail("任务不存在", HTTPStatus.NOT_FOUND)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        subscription = store.subscribe(task_id)
        terminal = {"done", "error", "cancelled"}
        try:
            while True:
                try:
                    event = subscription.get(timeout=15)
                except Exception:  # queue.Empty
                    self._write_chunk(b": ping\n\n")
                    continue

                payload = json.dumps(event, ensure_ascii=False).encode("utf-8")
                self._write_chunk(b"data: " + payload + b"\n\n")

                task = event.get("task") if event.get("type") == "task" else None
                if task and task.get("stage") in terminal:
                    self._write_chunk(b"event: end\ndata: {}\n\n")
                    break
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            store.unsubscribe(task_id, subscription)
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except OSError:
                pass
            self.close_connection = True


# ---------------------------------------------------------------------------
# 服务入口
# ---------------------------------------------------------------------------

def create_server(host: str = "127.0.0.1", port: int = 8760) -> ThreadingHTTPServer:
    """创建服务实例，端口被占用时自动向后顺延。"""
    last_error: Optional[Exception] = None
    for candidate in range(port, port + 40):
        try:
            server = hardening.LimitedThreadingHTTPServer((host, candidate), Handler)
            server.daemon_threads = True
            return server
        except OSError as exc:
            last_error = exc
            continue
    raise RuntimeError(f"无法找到可用端口：{last_error}")
