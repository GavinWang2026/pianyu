"""yt-dlp 引擎封装。

对外只暴露两个动作：

    probe()     解析链接 -> 元信息 + 用户可读的画质档位
    download()  执行下载 -> 通过回调上报进度 / 日志

设计要点：
1. **画质聚合**：yt-dlp 返回的 formats 列表动辄上百条（含各种 codec / 码率 / 分片），
   直接丢给用户是灾难。这里按「分辨率」聚合，每档挑最优流，并给出体积预估。
2. **代理智能路由**：国内站点走境外代理往往更慢甚至被拒，因此 auto 模式下按域名分流。
3. **多流进度合成**：DASH（B 站 / YouTube 高清）是音视频分离的，需下载两个流再合并。
   进度条必须把两条流折算成一个整体百分比，否则会出现「下到 100% 又跳回 0%」的观感灾难。
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import yt_dlp

from .config import DEFAULT_DOWNLOAD_DIR, DEFAULTS

# Safari 的 Cookie 库受 macOS TCC 保护；未授权时打开即 EPERM。
SAFARI_COOKIE_FILE = (
    Path.home() / "Library" / "Containers" / "com.apple.Safari" / "Data"
    / "Library" / "Cookies" / "Cookies.binarycookies"
)


def browser_cookie_issue(
    browser: str, cookie_path: Path | None = None,
) -> dict[str, str]:
    """检查浏览器 Cookie 库能否读取；读不到返回原因。

    实测（2026-11-18）：设置里选 Safari 后，yt-dlp 读
    ``Cookies.binarycookies`` 会被 TCC 拒绝（EPERM），原版 ``/api/parse``
    与融合层流水线都会因此 100% 失败。调用方应在构造 opts 时摘掉该来源，
    让公开素材继续解析；需要登录态的站点再走 Cookie 过期诊断。
    """
    src = (browser or "").strip().lower()
    if src != "safari" or sys.platform != "darwin":
        return {}
    path = cookie_path or SAFARI_COOKIE_FILE
    if not path.is_file():
        return {"source": src, "reason": f"找不到 Safari Cookie 库：{path}"}
    try:
        with open(path, "rb") as fh:
            fh.read(8)
    except OSError as exc:
        return {"source": src, "reason": str(exc)}
    return {}


class DownloadCancelled(Exception):
    """用户在下载过程中主动取消。"""


def live_record_seconds(cfg: dict | None = None) -> int:
    """直播录制秒数：0=拒绝；-1=录到取消；60–7200=限时片段。"""
    if cfg is None:
        from .config import config as _cfg
        cfg = _cfg.as_dict()
    try:
        n = int((cfg or {}).get("live_record_seconds") or 0)
    except (TypeError, ValueError):
        n = 0
    if n == -1:
        return -1
    if n <= 0:
        return 0
    return max(60, min(7200, n))


def apply_live_clip_opts(opts: dict, seconds: int) -> dict:
    """给直播下载加上 ffmpeg 时长上限；seconds=-1 表示录到用户取消（不加 -t）。"""
    opts["external_downloader"] = {
        "default": "ffmpeg",
        "m3u8": "ffmpeg",
        "http_dash_segments": "ffmpeg",
    }
    opts["hls_use_mpegts"] = True
    if int(seconds) == -1:
        # 不设 -t：依赖任务取消终止 ffmpeg / yt-dlp
        return opts
    secs = max(60, min(7200, int(seconds)))
    extra = ["-t", str(secs)]
    raw = opts.get("external_downloader_args")
    if isinstance(raw, dict):
        merged = dict(raw)
        prev = list(merged.get("ffmpeg_i") or [])
        merged["ffmpeg_i"] = prev + extra
        opts["external_downloader_args"] = merged
    else:
        opts["external_downloader_args"] = {"ffmpeg_i": extra}
    return opts


# ---------------------------------------------------------------------------
# 代理策略
# ---------------------------------------------------------------------------

# 国内站点：直连即可。走境外代理反而更慢，甚至被目标站拒绝。
DOMESTIC_HOSTS: tuple[str, ...] = (
    "bilibili.com", "b23.tv", "acgvideo.com", "bilivideo.com", "hdslb.com",
    "acfun.cn", "douyin.com", "iesdouyin.com", "amemv.com", "douyinpic.com",
    "kuaishou.com", "gifshow.com", "kwai.com", "kwaicdn.com",
    "xiaohongshu.com", "xhslink.com", "sns-video",
    "weibo.com", "weibo.cn", "sina.com.cn", "sinaimg.cn",
    "ixigua.com", "toutiao.com", "byteimg.com", "bytedance.com",
    "qq.com", "weixin.qq.com", "mp.weixin.qq.com", "qpic.cn",
    "youku.com", "iqiyi.com", "mgtv.com", "le.com", "pptv.com",
    "douyu.com", "huya.com", "yy.com", "zhanqi.tv",
    "163.com", "music.163.com", "zhihu.com", "sohu.com", "ifeng.com",
    "baidu.com", "bcebos.com", "tudou.com", "56.com", "miaopai.com",
    "6.cn", "ku6.com", "le.com", "acg.tv",
)


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def is_domestic(url: str) -> bool:
    host = _host_of(url)
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in DOMESTIC_HOSTS)


def resolve_proxy(url: str, cfg: Optional[dict] = None) -> Optional[str]:
    """按配置判定该 URL 应否走代理。返回 ``None`` 表示直连。"""
    cfg = cfg or {}
    mode = cfg.get("proxy_mode") or "never"
    proxy = (cfg.get("proxy_url") or "").strip()

    if not proxy or mode == "never":
        return None
    if mode == "always":
        return proxy
    # auto：只给境外站点挂代理
    return None if is_domestic(url) else proxy


# ---------------------------------------------------------------------------
# 画质档位聚合
# ---------------------------------------------------------------------------

# 越小越优先：优先 h264 / aac，因为兼容性最好（QuickTime / 剪辑软件 / 手机都能直接播）
_CODEC_RANK = {"avc": 0, "h264": 0, "hev": 1, "h265": 1, "vp9": 2, "vp09": 2, "av01": 3, "mp4a": 0, "aac": 0, "opus": 1}
_EXT_RANK = {"mp4": 0, "m4a": 0, "mov": 1, "webm": 2, "mkv": 2, "flv": 3, "ts": 4}

_TIER_LABEL = {4320: "8K", 2160: "4K", 1440: "2K", 1080: "高清", 720: "标清", 480: "流畅", 360: "省流"}


def _codec_rank(codec: Optional[str]) -> int:
    if not codec or codec == "none":
        return 9
    codec = codec.lower()
    for key, rank in _CODEC_RANK.items():
        if codec.startswith(key):
            return rank
    return 5


def _ext_rank(ext: Optional[str]) -> int:
    return _EXT_RANK.get((ext or "").lower(), 5)


def _size_of(fmt: dict) -> int:
    return int(fmt.get("filesize") or fmt.get("filesize_approx") or 0)


def _has_video(fmt: dict) -> bool:
    return fmt.get("vcodec") not in (None, "none")


def _has_audio(fmt: dict) -> bool:
    return fmt.get("acodec") not in (None, "none")


def _video_key(fmt: dict) -> tuple:
    """取「最佳视频流」的排序键（越大越好）。"""
    return (
        -_ext_rank(fmt.get("ext")),
        -_codec_rank(fmt.get("vcodec")),
        bool(fmt.get("acodec") not in (None, "none")),  # 音视频一体的渐进流优先，免合并
        float(fmt.get("fps") or 0),
        float(fmt.get("vbr") or fmt.get("tbr") or 0),
    )


def _audio_key(fmt: dict) -> tuple:
    return (
        -_ext_rank(fmt.get("ext")),
        -_codec_rank(fmt.get("acodec")),
        float(fmt.get("abr") or fmt.get("tbr") or 0),
    )


def _pick_best_audio(formats: list[dict]) -> Optional[dict]:
    pure = [f for f in formats if _has_audio(f) and not _has_video(f)]
    pool = pure or [f for f in formats if _has_audio(f)]
    if not pool:
        return None
    return max(pool, key=_audio_key)


def _tier_of(fmt: dict) -> tuple[int, str, str]:
    """推导画质档位。

    返回 ``(档位数值, 展示标签, 分辨率文本)``。

    这里刻意用**短边**而非 ``height`` 判定档位：竖屏视频（如手机拍摄的 1080×1920）
    的 ``height`` 是长边，直接拿来当档位会得出「1920P」这种没人看得懂的标签。
    取短边后，横屏竖屏都能落到同一套 1080P / 720P / 480P 直觉上。
    """
    width = fmt.get("width")
    height = fmt.get("height")
    try:
        height = int(height) if height else 0
        width = int(width) if width else 0
    except (TypeError, ValueError):
        width = height = 0

    short_side = min(width, height) if (width and height) else height
    if short_side in _TIER_LABEL:
        tier = short_side
    elif height in _TIER_LABEL:
        tier = height
    else:
        tier = short_side or height

    tier_name = _TIER_LABEL.get(tier)
    label = f"{tier}P {tier_name}" if tier_name else f"{tier}P"

    resolution = f"{width}×{height}" if (width and height) else (f"{height}P" if height else "")
    if width and height and height > width:
        resolution += " 竖屏"
    return tier, label, resolution


_HDR_HINTS = ("hdr", "hlg", "pq", "dolby vision", "dvhe", "dvh1")
_LANG_ZH = {
    "zh": "国语", "zh-cn": "国语", "zh-hans": "国语", "cmn": "国语",
    "zh-tw": "粤语/繁", "zh-hant": "粤语/繁", "yue": "粤语",
    "en": "英语", "ja": "日语", "ko": "韩语",
}


def _dynamic_range_of(fmt: dict) -> str:
    note = " ".join(
        str(fmt.get(k) or "") for k in ("dynamic_range", "format_note", "format", "vcodec")
    ).lower()
    if any(h in note for h in _HDR_HINTS):
        if "dolby" in note or "dvhe" in note or "dvh1" in note:
            return "Dolby Vision"
        return "HDR"
    return "SDR"


def _bitrate_mbps(fmt: dict, audio: dict | None = None, *, muxed: bool = True) -> float:
    tbr = float(fmt.get("tbr") or fmt.get("vbr") or 0)
    if not muxed and audio:
        tbr += float(audio.get("tbr") or audio.get("abr") or 0)
    if tbr <= 0 and audio and not _has_video(fmt):
        tbr = float(audio.get("tbr") or audio.get("abr") or 0)
    return round(tbr / 1000.0, 2) if tbr >= 100 else (round(tbr, 1) if tbr else 0.0)


def _audio_lang_label(fmt: dict | None) -> str:
    if not fmt:
        return ""
    raw = str(fmt.get("language") or fmt.get("language_preference") or "").strip()
    if not raw:
        return ""
    key = raw.lower().replace("_", "-")
    if key in _LANG_ZH:
        return _LANG_ZH[key]
    short = key.split("-")[0]
    return _LANG_ZH.get(short, raw)


def _enrich_label(base: str, *, tbr_mbps: float, dynamic_range: str, audio_lang: str) -> str:
    parts = [base]
    if tbr_mbps > 0:
        parts.append(f"{tbr_mbps:g} Mbps")
    if dynamic_range and dynamic_range != "SDR":
        parts.append(dynamic_range)
    if audio_lang:
        parts.append(audio_lang)
    return " · ".join(parts)


def summarize_formats(info: dict) -> list[dict]:
    """把 yt-dlp 的原始 formats 聚合成用户可选的清晰度档位。"""
    raw_formats = [f for f in (info.get("formats") or []) if f.get("format_id")]
    all_video = [f for f in raw_formats if _has_video(f)]
    video_formats = [f for f in all_video if f.get("height")]
    best_audio = _pick_best_audio(raw_formats)
    audio_size = _size_of(best_audio) if best_audio else 0
    audio_lang = _audio_lang_label(best_audio)

    # 按「短边档位」分桶，每桶取最优流；桶内再保留真实分辨率用于展示
    buckets: dict[int, dict] = {}
    for fmt in video_formats:
        tier, _label, _res = _tier_of(fmt)
        if not tier:
            continue
        current = buckets.get(tier)
        if current is None or _video_key(fmt) > _video_key(current):
            buckets[tier] = fmt

    options: list[dict] = []
    for tier in sorted(buckets, reverse=True):
        fmt = buckets[tier]
        muxed = _has_audio(fmt)
        size = _size_of(fmt) + (0 if muxed else audio_size)
        _tier, base_label, resolution = _tier_of(fmt)
        tbr = _bitrate_mbps(fmt, best_audio, muxed=muxed)
        dr = _dynamic_range_of(fmt)
        lang = _audio_lang_label(fmt) if muxed else audio_lang
        options.append({
            "id": f"h{tier}",
            "label": _enrich_label(base_label, tbr_mbps=tbr, dynamic_range=dr, audio_lang=lang),
            "tier": tier,
            "resolution": resolution,
            "height": fmt.get("height"),
            "format_id": fmt.get("format_id"),
            "audio_format_id": None if muxed else (best_audio or {}).get("format_id"),
            "fps": fmt.get("fps"),
            "ext": (fmt.get("ext") if muxed else "mp4") or "mp4",
            "vcodec": fmt.get("vcodec"),
            "acodec": fmt.get("acodec") if muxed else (best_audio or {}).get("acodec"),
            "filesize": size or None,
            "needs_merge": not muxed,
            "audio_only": False,
            "tbr_mbps": tbr or None,
            "dynamic_range": dr,
            "audio_language": lang or None,
        })

    # 兜底：确认有视频流、但取不到分辨率（直链文件、部分自建站常见）。
    # 这类源的 format 常连 vcodec / height 都是 null，若直接跳过，
    # 用户就只剩「仅音频」可选，等于功能缺失。
    if not options:
        pool = all_video or raw_formats
        if pool:
            best = max(pool, key=_video_key)
            acodec = best.get("acodec")

            # 一体流判定：明确带音轨 → 一体；明确无音轨且存在可合并音轨 → 需合并；
            # 其余（编码未知）一律按一体处理 —— 最坏是拿到无声视频，
            # 也好过自作聪明去合并、结果整体失败。
            if acodec and acodec != "none":
                muxed = True
            elif acodec == "none" and best_audio is not None:
                muxed = False
            else:
                muxed = True

            tbr = _bitrate_mbps(best, best_audio, muxed=muxed)
            dr = _dynamic_range_of(best)
            lang = _audio_lang_label(best) if muxed else audio_lang
            options.append({
                "id": "best",
                "label": _enrich_label("最佳画质", tbr_mbps=tbr, dynamic_range=dr, audio_lang=lang),
                "tier": 0,
                "resolution": "原始画质",
                "height": best.get("height"),
                "format_id": best.get("format_id"),
                "audio_format_id": None if muxed else (best_audio or {}).get("format_id"),
                "fps": best.get("fps"),
                "ext": (best.get("ext") if muxed else "mp4") or "mp4",
                "vcodec": best.get("vcodec"),
                "acodec": acodec if muxed else (best_audio or {}).get("acodec"),
                "filesize": (_size_of(best) + (0 if muxed else audio_size)) or None,
                "needs_merge": not muxed,
                "audio_only": False,
                "tbr_mbps": tbr or None,
                "dynamic_range": dr,
                "audio_language": lang or None,
            })

    # 推荐档：不超过 1080P 的最高一档（画质与体积的平衡点）
    recommended = next((o for o in options if o["tier"] <= 1080), options[0] if options else None)
    for opt in options:
        opt["recommended"] = recommended is not None and opt["id"] == recommended["id"]

    # 仅音频档（保留原始音轨，M4A 容器，兼容性最好）
    audio_total = (_size_of(best_audio) if best_audio else 0) or None
    audio_tbr = _bitrate_mbps(best_audio or {}, muxed=True) if best_audio else 0.0
    options.append({
        "id": "audio",
        "label": _enrich_label("仅音频", tbr_mbps=audio_tbr, dynamic_range="SDR", audio_lang=audio_lang),
        "tier": 0,
        "resolution": "原始音轨",
        "height": 0,
        "format_id": None,
        "audio_format_id": (best_audio or {}).get("format_id"),
        "fps": None,
        "ext": "m4a",
        "vcodec": None,
        "acodec": (best_audio or {}).get("acodec"),
        "filesize": audio_total,
        "needs_merge": False,
        "audio_only": True,
        "recommended": False,
        "tbr_mbps": audio_tbr or None,
        "dynamic_range": None,
        "audio_language": audio_lang or None,
    })
    return options


_FORMAT_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


def _tagged_template(template: str, option: dict) -> str:
    """把画质标签注入文件名模板。

    没有这一步，同一视频的 1080P / 720P / 480P 会生成**完全同名**的文件，
    后一次下载会被 yt-dlp 当作「已存在」而静默跳过 —— 用户选了 720P 却拿到 480P 的文件。
    """
    if option.get("audio_only"):
        tag = "音频"
    else:
        tier = option.get("tier") or option.get("height") or ""
        tag = f"{int(tier)}P" if str(tier).isdigit() else ""
    if not tag:
        return template

    marker = ".%(ext)s"
    if marker in template:
        return template.replace(marker, f" [{tag}]" + marker, 1)
    return f"{template} [{tag}]"


def build_selector(option: dict) -> str:
    """生成 yt-dlp 的 format 选择表达式。

    优先精确指定 ``video+audio`` 的 format_id —— 用户点哪一档就下哪一档，
    避免 ``height<=N`` 在竖屏 / 多编码场景下选错流。附带一条兜底链，
    万一该流临时不可用仍能退化到同档次的最佳流。
    """
    if option.get("audio_only"):
        return "bestaudio[ext=m4a]/bestaudio/best"

    height = option.get("height")
    fallback = "bv*+ba/b"
    if height:
        h = int(height)
        fallback = (
            f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/"
            f"bv*[height<={h}]+ba/"
            f"b[height<={h}]/"
            f"bv*+ba/b"
        )

    video_id = str(option.get("format_id") or "")
    if not _FORMAT_ID_RE.match(video_id):
        return fallback

    audio_id = str(option.get("audio_format_id") or "")
    if _FORMAT_ID_RE.match(audio_id):
        return f"{video_id}+{audio_id}/{fallback}"
    return f"{video_id}/{fallback}"



# ---------------------------------------------------------------------------
# 错误信息本地化
# ---------------------------------------------------------------------------

_FRIENDLY_ERRORS: tuple[tuple[str, str], ...] = (
    ("unsupported url", "暂不支持该站点的链接"),
    ("is not a valid url", "链接格式不正确"),
    ("video unavailable", "视频不可用（可能已删除或设为私密）"),
    ("private video", "该视频为私密视频，需要登录后才能下载"),
    ("sign in to confirm", "需要登录验证（请在「设置」中配置浏览器 Cookie）"),
    ("only available to", "该视频仅对会员 / 特定用户可见，需要 Cookie"),
    ("this video is available to this channel's members", "会员专属内容，需要配置已订阅账号的 Cookie"),
    ("http error 403", "服务器拒绝访问（可能需要 Cookie，或需要配置代理）"),
    ("http error 404", "链接不存在或已失效"),
    ("http error 429", "请求过于频繁，请稍后再试"),
    ("unable to download", "网络层面下载失败，请检查网络或代理"),
    ("timed out", "网络超时，请检查网络或代理设置"),
    ("connection refused", "连接被拒绝（代理地址可能不正确）"),
    ("connection reset", "连接被重置（代理或网络不稳定）"),
    ("failed to resolve", "域名解析失败，请检查网络"),
    ("no video formats found", "未取到视频流：平台可能触发了频率限制，请稍等片刻重试；若长期如此，请在「设置」中配置浏览器 Cookie"),
    ("requested format is not available", "所选清晰度不可用，请换一档"),
    ("ffmpeg", "缺少 ffmpeg，无法合并音视频（请先安装 ffmpeg）"),
    ("certificate", "证书校验失败"),
)


def friendly_error(raw: Any) -> str:
    """把 yt-dlp 的英文报错翻译成用户能看懂的中文提示。"""
    text = str(raw)
    lowered = text.lower()
    for needle, message in _FRIENDLY_ERRORS:
        if needle in lowered:
            return message
    cleaned = re.sub(r"^ERROR:\s*", "", text.strip())
    return cleaned[:300] or "未知错误"


# ---------------------------------------------------------------------------
# yt-dlp 选项
# ---------------------------------------------------------------------------

class _CollectorLogger:
    """把 yt-dlp 的内部日志收敛后转发给上层（用于 UI 的实时日志区）。"""

    def __init__(self, sink: Optional[Callable[[str], None]] = None) -> None:
        self._sink = sink

    def _emit(self, message: str) -> None:
        if not self._sink:
            return
        text = str(message).strip()
        if text:
            self._sink(text)

    def debug(self, message: str) -> None:
        if not str(message).startswith("[debug] "):
            self._emit(message)

    def info(self, message: str) -> None:
        self._emit(message)

    def warning(self, message: str) -> None:
        self._emit(f"⚠ {message}")

    def error(self, message: str) -> None:
        self._emit(f"✕ {message}")


def _base_opts(cfg: dict, url: str, logger=None) -> dict:
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,        # 单条下载定位：多 P / 播放列表只取当前条目
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 4,
        "no_color": True,
        "nocheckcertificate": False,
        "logger": logger or _CollectorLogger(None),
    }
    proxy = resolve_proxy(url, cfg)
    if proxy:
        opts["proxy"] = proxy
    browser = (cfg.get("cookies_from_browser") or "").strip()
    if browser and not browser_cookie_issue(browser):
        opts["cookiesfrombrowser"] = (browser,)
    try:
        rate_kb = int(cfg.get("download_rate_kbps") or 0)
    except (TypeError, ValueError):
        rate_kb = 0
    if rate_kb > 0:
        opts["ratelimit"] = rate_kb * 1024
    return opts


def _pick_thumbnail(info: dict) -> Optional[str]:
    thumbs = info.get("thumbnails") or []
    if thumbs:
        best = max(
            thumbs,
            key=lambda t: (t.get("preference") or 0, (t.get("width") or 0) * (t.get("height") or 0)),
        )
        if best.get("url"):
            return best["url"]
    return info.get("thumbnail")


# ---------------------------------------------------------------------------
# 对外动作
# ---------------------------------------------------------------------------

def probe(url: str, cfg: Optional[dict] = None) -> dict:
    """解析链接，返回元信息 + 画质档位。不下载任何数据。"""
    cfg = cfg or {}
    opts = _base_opts(cfg, url)
    opts.update({"skip_download": True, "extract_flat": False})

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        raise RuntimeError("无法解析该链接")

    # 兜底：万一仍是播放列表结构，取第一个条目
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise RuntimeError("该链接下没有可下载的视频")
        info = entries[0]

    is_live = bool(info.get("is_live"))
    live_secs = live_record_seconds(cfg) if is_live else 0
    if is_live and live_secs <= 0:
        raise RuntimeError(
            "这是直播流。可在设置里把「直播录制时长」设为 60–3600 秒以限时录制一段；"
            "当前为 0（拒绝）"
        )

    qualities = summarize_formats(info)
    if not qualities:
        raise RuntimeError("没有找到可用的清晰度")

    # 字幕可用性顺手带出去：这份 info 本来就要拿，不额外发请求。
    # 前端据此提前告诉用户「这个视频有字幕，提取会秒出」。
    from .subtitles import describe as describe_subtitles

    return {
        "id": info.get("id"),
        "title": info.get("title") or "未命名视频",
        "thumbnail": _pick_thumbnail(info),
        "duration": info.get("duration") or (live_secs if is_live else None),
        "uploader": info.get("uploader") or info.get("channel") or info.get("uploader_id") or "",
        "webpage_url": info.get("webpage_url") or url,
        "extractor": info.get("extractor_key") or info.get("extractor") or "",
        "view_count": info.get("view_count"),
        "upload_date": info.get("upload_date"),
        "description": (info.get("description") or "")[:400],
        "qualities": qualities,
        "subtitles": describe_subtitles(info),
        "is_live": is_live,
        "live_record_seconds": live_secs if is_live else 0,
    }


def download(
    url: str,
    option: dict,
    cfg: dict,
    on_progress: Callable[[dict], None],
    on_log: Optional[Callable[[str], None]] = None,
    cancel_event=None,
) -> dict:
    """执行下载。

    ``option`` 即 ``probe()`` 返回的某个画质档位，形如::

        {"height": 1080, "audio_only": False,
         "format_id": "80", "audio_format_id": "30280"}

    on_progress 收到的事件形如::

        {"stage": "downloading", "percent": 42.5, "speed": 1234567.0,
         "eta": 12, "downloaded_bytes": ..., "total_bytes": ...}

    返回 ``{"filepath": ..., "filename": ..., "title": ...}``。
    """
    option = option or {}
    audio_only = bool(option.get("audio_only"))
    started_at = time.time()
    out_dir = Path(cfg.get("download_dir") or DEFAULT_DOWNLOAD_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    template = _tagged_template(cfg.get("filename_template") or DEFAULTS["filename_template"], option)

    # 预估流数量：分离式 video+audio 必然是两条流。
    # 这个值必须在下载开始前就知道，否则进度条会出现「下到 100% 又退回 50%」的跳变。
    if audio_only:
        expected_streams = 1
    elif option.get("format_id") and option.get("audio_format_id"):
        expected_streams = 2
    else:
        expected_streams = 1

    # 多流进度合成所需的运行时状态
    state: dict = {"streams": [], "expected": expected_streams}

    def _emit(payload: dict) -> None:
        try:
            on_progress(payload)
        except Exception:  # 进度回调绝不能反过来打断下载
            pass

    def _hook(d: dict) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled()

        info = d.get("info_dict") or {}
        filename = d.get("filename") or info.get("_filename")

        # 记录出现过的流（DASH 下是视频流 + 音频流，两条）
        if filename and filename not in state["streams"]:
            state["streams"].append(filename)

        # yt-dlp 在合并下载时会把待下载流列表放在 requested_formats
        requested = info.get("requested_formats")
        if requested:
            state["expected"] = max(1, len(requested))

        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            stream_pct = (done / total * 100.0) if total else 0.0

            stream_count = max(1, len(state["streams"]), state["expected"])
            index = state["streams"].index(filename) + 1 if filename in state["streams"] else 1
            overall = ((index - 1) + min(stream_pct, 100.0) / 100.0) / stream_count * 100.0

            _emit({
                "stage": "downloading",
                "percent": round(overall, 2),
                "stream_percent": round(stream_pct, 2),
                "stream_index": index,
                "stream_count": stream_count,
                "downloaded_bytes": done,
                "total_bytes": total,
                "speed": d.get("speed"),
                "eta": d.get("eta"),
                "filename": Path(filename).name if filename else "",
            })
        elif status == "finished":
            _emit({
                "stage": "postprocess",
                "percent": None,
                "message": "已完成下载，正在处理…",
                "filename": Path(filename).name if filename else "",
            })
        elif status == "error":
            _emit({"stage": "error", "message": "下载过程中出现错误"})

    def _pp_hook(d: dict) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled()
        if d.get("status") != "started":
            return
        name = d.get("postprocessor") or ""
        labels = {
            "Merger": "正在合并音视频…",
            "FFmpegMerger": "正在合并音视频…",
            "ExtractAudio": "正在提取音频…",
            "FFmpegExtractAudio": "正在提取音频…",
            "VideoConvertor": "正在转换容器格式…",
            "FFmpegVideoConvertor": "正在转换容器格式…",
            "VideoRemuxer": "正在重封装…",
            "FFmpegVideoRemuxer": "正在重封装…",
        }
        message = labels.get(name)
        if message:
            _emit({"stage": "postprocess", "percent": None, "message": message})

    opts = _base_opts(cfg, url, logger=_CollectorLogger(on_log))
    opts.update({
        "format": build_selector(option),
        "outtmpl": {"default": str(out_dir / template)},
        "progress_hooks": [_hook],
        "postprocessor_hooks": [_pp_hook],
        "overwrites": False,
        "continuedl": True,
        # 在候选流之间优先 mp4/m4a + h264/aac，最大化兼容性
        "format_sort": ["ext:mp4:m4a", "vcodec:h264", "acodec:aac"],
    })

    # 直播：限时片段，避免无限拉流
    if option.get("is_live"):
        secs = int(option.get("live_record_seconds") or 0) or live_record_seconds(cfg)
        if secs <= 0:
            raise RuntimeError("这是直播流，当前未开启限时录制（live_record_seconds=0）")
        apply_live_clip_opts(opts, secs)
        if on_log:
            on_log(f"直播限时录制 {secs} 秒…")

    if audio_only:
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "m4a",
            "preferredquality": "0",
        }]
    else:
        opts["merge_output_format"] = "mp4/mkv"

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        final_path = _resolve_final_path(info or {}, out_dir, ydl)

    # 这里刻意**不**广播终态：宣布「完成」的资格属于任务层 ——
    # 只有任务层拿到最终文件路径，才能给出完整快照。若在此处抢先广播 done，
    # 前端会立即关闭进度订阅，随后那条带文件名的完整快照就永远收不到了。
    reused = False
    if final_path:
        try:
            reused = Path(final_path).stat().st_mtime < started_at - 1
        except OSError:
            reused = False

    return {
        "filepath": final_path or "",
        "filename": Path(final_path).name if final_path else "",
        "title": (info or {}).get("title") or "",
        "reused": reused,
    }


def _resolve_final_path(info: dict, out_dir: Path, ydl) -> Optional[str]:
    """定位合并 / 转码后的最终文件。"""
    # 1) 首选 yt-dlp 自己记录的最终路径
    for record in (info.get("requested_downloads") or []):
        candidate = record.get("filepath") or record.get("_filename")
        if candidate and Path(candidate).exists():
            return str(Path(candidate))

    # 2) prepare_filename
    try:
        candidate = ydl.prepare_filename(info)
        if candidate and Path(candidate).exists():
            return str(Path(candidate))
    except Exception:
        pass

    # 3) 兜底：按视频 id 扫目录，排除分片临时文件（形如 xxx.f137.mp4）
    video_id = info.get("id")
    if video_id:
        try:
            found = [p for p in out_dir.iterdir() if p.is_file() and video_id in p.name]
        except OSError:
            found = []
        if found:
            temp_pattern = re.compile(r"\.f\d+\.")
            real = [p for p in found if not temp_pattern.search(p.name)]
            pool = real or found
            return str(max(pool, key=lambda p: p.stat().st_mtime))
    return None
