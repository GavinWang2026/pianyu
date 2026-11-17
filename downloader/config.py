"""全局配置存储。

配置持久化到项目根目录的 ``config.json``，纯文本、用户可读可改。
任何一次写入都会做归一化（补默认值 / 纠正非法值 / 确保下载目录存在）。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from threading import RLock

APP_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = APP_DIR / "config.json"
DEFAULT_DOWNLOAD_DIR = APP_DIR / "downloads"

# 代理模式：
#   auto   仅「境外站点」走代理，国内站点直连（避免代理拖慢甚至阻断国内源）
#   always 所有站点统一走代理
#   never  全部直连（默认，最稳）
PROXY_MODES = ("auto", "always", "never")

# 可选：从浏览器读取 Cookie，用于访问你已获授权的自有内容
COOKIE_BROWSERS = ("", "chrome", "edge", "safari", "firefox", "chromium", "brave", "opera", "vivaldi", "whale")

# 语音识别引擎：auto 按平台自动挑（Apple Silicon → MLX，其余 → Faster-Whisper）
ASR_ENGINES = ("auto", "mlx", "faster")


def _as_bool(value, default: bool = False) -> bool:
    """把 JSON / 表单里五花八门的「真值」写法收敛成布尔。

    前端表单提交的是字符串（``"false"`` / ``"0"`` / ``"off"``），
    直接丢进 ``if`` 判断会把 ``"false"`` 当成真 —— 这类坑栽过一次就够了。
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("", "none", "null"):
        return default
    return text not in ("0", "false", "no", "off", "disabled")


DEFAULTS: dict = {
    "download_dir": str(DEFAULT_DOWNLOAD_DIR),
    "proxy_mode": "never",
    "proxy_url": "",
    "cookies_from_browser": "",
    "filename_template": "%(title).120s [%(id)s].%(ext)s",
    # 有平台字幕就直接用字幕，没有才跑语音识别（秒出且零误差）
    "prefer_subtitle": True,
    "asr_engine": "auto",
    # 热词 / 术语表（逗号或换行分隔，最多 200 词）→ ASR initial_prompt
    "asr_hotwords": "",
    # 播放列表展开条数：1=只取当前/首条（默认）；2–50=展开前 N 条进批量队列
    "playlist_max_entries": 1,
    # Obsidian 库目录（空=不直写）；必须落在用户主目录下
    "obsidian_dir": "",
    # 直播录制：0=拒绝；-1=录到取消；60–7200=限时秒数（默认 300）
    "live_record_seconds": 300,
    # 下载限速（KB/s）：0=不限；用于弱网或共享带宽
    "download_rate_kbps": 0,
    # 说话人启发式标注（停顿交替 A/B；非神经网络）
    "asr_diarize": False,
    # Notion / 飞书分发（仅本地存；推送走 HTTPS）
    "notion_token": "",
    "notion_database_id": "",
    "feishu_webhook": "",
    # 文案后处理用的模型服务（OpenAI 兼容协议）
    "llm_base_url": "",
    "llm_api_key": "",
    "llm_model": "",
    "llm_disable_thinking": True,
    # 磁盘保留：任务快照天数；产物目录总上限 GB（0=不限）
    "task_retain_days": 7,
    "outputs_max_gb": 5.0,
}

MAX_HOTWORDS = 200


class ConfigStore:
    """线程安全的 JSON 配置存储。"""

    def __init__(self, path: Path = CONFIG_PATH) -> None:
        self._path = path
        self._lock = RLock()
        self._data: dict = dict(DEFAULTS)
        self.load()

    # ------------------------------------------------------------------ 持久化

    def load(self) -> dict:
        with self._lock:
            if self._path.exists():
                try:
                    raw = json.loads(self._path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        for key, value in raw.items():
                            if key in DEFAULTS:
                                self._data[key] = value
                except (json.JSONDecodeError, OSError):
                    # 配置损坏时静默回退到默认值，不阻塞启动
                    pass
            normalized = self._normalize()
            self._ensure_download_dir()
            return normalized

    def save(self) -> None:
        with self._lock:
            try:
                self._path.write_text(
                    json.dumps(self._data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except OSError:
                pass

    # ------------------------------------------------------------------ 读写

    def get(self, key: str, default=None):
        with self._lock:
            return self._data.get(key, default)

    def as_dict(self) -> dict:
        with self._lock:
            return dict(self._data)

    def update(self, patch: dict) -> dict:
        with self._lock:
            for key, value in (patch or {}).items():
                if key in DEFAULTS:
                    self._data[key] = value
            normalized = self._normalize()
            self._ensure_download_dir()
        self.save()
        return normalized

    # ------------------------------------------------------------------ 内部

    def _normalize(self) -> dict:
        if self._data.get("proxy_mode") not in PROXY_MODES:
            self._data["proxy_mode"] = "never"
        if self._data.get("cookies_from_browser") not in COOKIE_BROWSERS:
            self._data["cookies_from_browser"] = ""
        if self._data.get("asr_engine") not in ASR_ENGINES:
            self._data["asr_engine"] = "auto"

        self._data["proxy_mode"] = str(self._data.get("proxy_mode"))
        self._data["proxy_url"] = str(self._data.get("proxy_url") or "").strip()
        self._data["cookies_from_browser"] = str(self._data.get("cookies_from_browser") or "").strip()
        self._data["asr_engine"] = str(self._data.get("asr_engine") or "auto").strip()
        self._data["prefer_subtitle"] = _as_bool(self._data.get("prefer_subtitle"), True)
        self._data["asr_diarize"] = _as_bool(self._data.get("asr_diarize"), False)
        self._data["llm_disable_thinking"] = _as_bool(self._data.get("llm_disable_thinking"), True)
        self._data["llm_base_url"] = str(self._data.get("llm_base_url") or "").strip()
        self._data["llm_api_key"] = str(self._data.get("llm_api_key") or "").strip()
        self._data["llm_model"] = str(self._data.get("llm_model") or "").strip()
        self._data["notion_token"] = str(self._data.get("notion_token") or "").strip()
        self._data["notion_database_id"] = str(self._data.get("notion_database_id") or "").strip()
        self._data["feishu_webhook"] = str(self._data.get("feishu_webhook") or "").strip()

        # 热词：统一成逗号分隔，截断到 MAX_HOTWORDS
        raw_hw = str(self._data.get("asr_hotwords") or "")
        words = []
        for part in re.split(r"[,，;\n\r\t]+", raw_hw):
            w = part.strip()
            if w and w not in words:
                words.append(w)
            if len(words) >= MAX_HOTWORDS:
                break
        self._data["asr_hotwords"] = "，".join(words)

        try:
            n = int(self._data.get("playlist_max_entries") or 1)
        except (TypeError, ValueError):
            n = 1
        self._data["playlist_max_entries"] = max(1, min(50, n))

        obs = str(self._data.get("obsidian_dir") or "").strip()
        self._data["obsidian_dir"] = str(Path(obs).expanduser()) if obs else ""

        try:
            live_n = int(self._data.get("live_record_seconds") or 0)
        except (TypeError, ValueError):
            live_n = 300
        if live_n == -1:
            self._data["live_record_seconds"] = -1
        elif live_n <= 0:
            self._data["live_record_seconds"] = 0
        else:
            self._data["live_record_seconds"] = max(60, min(7200, live_n))

        try:
            rate = int(self._data.get("download_rate_kbps") or 0)
        except (TypeError, ValueError):
            rate = 0
        self._data["download_rate_kbps"] = max(0, min(100_000, rate))

        raw_dir = str(self._data.get("download_dir") or "").strip()
        self._data["download_dir"] = str(Path(raw_dir).expanduser()) if raw_dir else str(DEFAULT_DOWNLOAD_DIR)

        try:
            days = int(self._data.get("task_retain_days") or 7)
        except (TypeError, ValueError):
            days = 7
        self._data["task_retain_days"] = max(1, min(365, days))

        try:
            gb = float(self._data.get("outputs_max_gb") or 5)
        except (TypeError, ValueError):
            gb = 5.0
        self._data["outputs_max_gb"] = max(0.0, min(500.0, gb))

        tmpl = str(self._data.get("filename_template") or "").strip()
        self._data["filename_template"] = tmpl or DEFAULTS["filename_template"]
        return dict(self._data)

    @staticmethod
    def hotword_list(raw: str | None = None) -> list[str]:
        text = raw if raw is not None else ""
        if raw is None:
            text = str(config.get("asr_hotwords") or "")
        out: list[str] = []
        for part in re.split(r"[,，;\n\r\t]+", text):
            w = part.strip()
            if w and w not in out:
                out.append(w)
            if len(out) >= MAX_HOTWORDS:
                break
        return out

    def _ensure_download_dir(self) -> None:
        try:
            Path(self._data["download_dir"]).mkdir(parents=True, exist_ok=True)
        except OSError:
            self._data["download_dir"] = str(DEFAULT_DOWNLOAD_DIR)
            try:
                DEFAULT_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass

    # ------------------------------------------------------------------ 工具

    @staticmethod
    def env_proxy() -> str:
        """探测系统环境变量里的代理，供 UI 作为「一键填入」的候选值。

        SEC-05：剥离 userinfo，只回显 ``host:port``，避免把代理账号密码打到前端。
        """
        from urllib.parse import urlparse

        for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            value = (os.environ.get(key) or "").strip()
            if not value:
                continue
            try:
                parsed = urlparse(value if "://" in value else f"http://{value}")
                host = parsed.hostname or ""
                if not host:
                    return value.split("@")[-1]
                if parsed.port:
                    return f"{host}:{parsed.port}"
                return host
            except Exception:
                return value.split("@")[-1] if "@" in value else value
        return ""


config = ConfigStore()
