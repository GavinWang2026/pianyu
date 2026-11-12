"""平台自带字幕的抓取与解析。

「有字幕就别跑语音识别」—— 平台字幕是官方或人群校对过的文本，
秒出、零误差、还能拿到精确时间轴；语音识别只在没有字幕时才需要出场。

三条设计约束：

1. **纯标准库解析。** VTT / SRT / JSON 全部手写解析，不引第三方包，
   维持「主服务只依赖 yt-dlp」这条底线。
2. **交给 yt-dlp 去取。** 各站字幕格式千奇百怪（B 站是自定义 JSON、
   YouTube 是 json3 / vtt、HLS 是分片 VTT），yt-dlp 已经把这一切摆平了，
   我们只需把落盘文件读回来解析。
3. **人工字幕优先于自动字幕。** 自动字幕没有标点、专有名词错得离谱，
   但聊胜于无；结果里会把来源标出来，让用户自己判断可信度。
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# 语言偏好表：越靠前越优先。
# 中文场景刻意把「简体」排在「繁体」前面 —— 用户要的是能直接用的稿子。
_LANGUAGE_ORDER: dict[str, tuple[str, ...]] = {
    "zh": ("zh-Hans", "zh-CN", "zh-Hans-CN", "zh", "zh-Hant", "zh-TW", "zh-HK", "en", "en-US"),
    "en": ("en", "en-US", "en-GB", "en-orig", "zh-Hans", "zh"),
    "auto": ("zh-Hans", "zh-CN", "zh", "en", "en-US"),
}

# 传给 yt-dlp 的抓取模式（yt-dlp 按前缀正则匹配语言键）。
# 只抓候选语言，避免像 YouTube 那样一次拖回几十种字幕。
_FETCH_PATTERNS: dict[str, tuple[str, ...]] = {
    "zh": ("zh-Hans", "zh-CN", "zh", "en"),
    "en": ("en", "zh-Hans", "zh"),
    "auto": ("zh-Hans", "zh", "en"),
}

_TAG_RE = re.compile(r"<[^>]*>")
# VTT 时间轴：00:00:01.000 --> 00:00:04.000 align:start position:0%
_TIMING_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})"
)
# 无小时位的时间轴（少数导出工具会这么写）
_TIMING_SHORT_RE = re.compile(
    r"(\d{1,2}):(\d{2})[.,](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2})[.,](\d{1,3})"
)

_SENTENCE_END = "。！？!?…；;"
# 强句末标点：出现在结尾即视为一句话说完了，可以断开。
# 逗号、顿号一类**不**算 —— 那是句内停顿，在那儿断开只会把句子切碎。
_STRONG_END = "。！？!?…"
# 太短的「句子」不值得独立成条（多半是语气词或标点误判）
_MIN_SENTENCE_CHARS = 6
# 合并上限：太长的时间轴条目在「时间戳」视图里反而不好用
_MAX_SEGMENT_CHARS = 72
# 两条字幕之间的静默超过这个秒数就断开，作为自然句界
_GAP_BREAK = 1.0


@dataclass
class SubtitleResult:
    """一次字幕抓取的结果。"""

    segments: list[dict]
    language: str = ""
    kind: str = "manual"      # manual（人工） | auto（平台自动生成）
    fmt: str = ""             # vtt | srt | json

    @property
    def text(self) -> str:
        return "".join(item["text"] for item in self.segments).strip()


# ---------------------------------------------------------------------------
# 时间轴解析
# ---------------------------------------------------------------------------

def _to_seconds(hours: str, minutes: str, seconds: str, millis: str) -> float:
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(millis.ljust(3, "0")[:3]) / 1000.0
    )


def _match_timing(line: str) -> Optional[tuple[float, float]]:
    match = _TIMING_RE.search(line)
    if match:
        g = match.groups()
        return _to_seconds(*g[:4]), _to_seconds(*g[4:])
    match = _TIMING_SHORT_RE.search(line)
    if match:
        g = match.groups()
        start = int(g[0]) * 60 + int(g[1]) + int(g[2].ljust(3, "0")[:3]) / 1000.0
        end = int(g[3]) * 60 + int(g[4]) + int(g[5].ljust(3, "0")[:3]) / 1000.0
        return start, end
    return None


def _clean_line(raw: str) -> str:
    """去掉字幕里的标记：内联时间戳、``<c>``、``<v Speaker>``、HTML 实体。"""
    text = _TAG_RE.sub("", raw)
    text = html.unescape(text)
    # 自动字幕常用全角空格与不换行空格对齐
    text = text.replace("\u200b", "").replace("\ufeff", "").replace("\xa0", " ")
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# 各格式解析
# ---------------------------------------------------------------------------

def parse_vtt(content: str) -> list[dict]:
    """解析 WebVTT（含 YouTube 自动字幕那种滚动式逐词刷新）。"""
    cues: list[dict] = []
    # 去掉 BOM / 头部 / NOTE / STYLE 块
    text = content.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    blocks = re.split(r"\n{2,}", text)

    for block in blocks:
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        if lines[0].startswith(("WEBVTT", "NOTE", "STYLE", "REGION", "Kind:", "Language:")):
            lines = lines[1:]
            if not lines:
                continue

        timing_index = next(
            (i for i, line in enumerate(lines) if "-->" in line), None
        )
        if timing_index is None:
            continue
        span = _match_timing(lines[timing_index])
        if span is None:
            continue

        payload = [_clean_line(line) for line in lines[timing_index + 1:]]
        payload = [line for line in payload if line]
        if not payload:
            continue

        # 滚动字幕会把上一句重复带进下一帧，只保留最后一行为新增内容
        body = payload[-1] if len(payload) > 1 else payload[0]
        if not body:
            continue
        cues.append({"start": span[0], "end": span[1], "text": body})

    return _dedupe(cues)


def parse_srt(content: str) -> list[dict]:
    """解析 SRT（结构最规整，几乎没有脏数据）。"""
    cues: list[dict] = []
    text = content.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    for block in re.split(r"\n{2,}", text):
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        span = _match_timing(lines[timing_index])
        if span is None:
            continue
        body = " ".join(_clean_line(line) for line in lines[timing_index + 1:]).strip()
        if body:
            cues.append({"start": span[0], "end": span[1], "text": body})
    return _dedupe(cues)


def parse_json3(content: str) -> list[dict]:
    """解析 YouTube 的 json3 字幕。"""
    data = json.loads(content)
    cues: list[dict] = []
    for event in data.get("events") or []:
        pieces = [seg.get("utf8") or "" for seg in (event.get("segs") or [])]
        body = _clean_line("".join(pieces))
        if not body:
            continue
        start = float(event.get("tStartMs") or 0) / 1000.0
        end = start + float(event.get("dDurationMs") or 0) / 1000.0
        cues.append({"start": start, "end": end, "text": body})
    return _dedupe(cues)


def parse_bilibili_json(content: str) -> list[dict]:
    """解析 B 站字幕接口返回的 ``{"body": [{"from", "to", "content"}]}``。"""
    data = json.loads(content)
    cues: list[dict] = []
    for row in data.get("body") or []:
        body = _clean_line(str(row.get("content") or ""))
        if not body:
            continue
        cues.append({
            "start": float(row.get("from") or 0),
            "end": float(row.get("to") or 0),
            "text": body,
        })
    return _dedupe(cues)


def _dedupe(cues: list[dict]) -> list[dict]:
    """丢掉与前一条完全重复的条目（滚动字幕的常见冗余）。"""
    result: list[dict] = []
    for cue in cues:
        if not cue["text"]:
            continue
        if result and result[-1]["text"] == cue["text"]:
            result[-1]["end"] = max(result[-1]["end"], cue["end"])
            continue
        result.append(dict(cue))
    return result


def parse_subtitle_file(path: Path) -> list[dict]:
    """按内容嗅探格式并解析，不依赖扩展名（各站导出的扩展名很不可靠）。"""
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "utf-16", "gb18030", "latin-1"):
        try:
            content = raw.decode(encoding)
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
    else:  # pragma: no cover - 上面的 latin-1 兜底几乎不会失败
        return []

    stripped = content.lstrip("\ufeff \t\r\n")
    if stripped.startswith("WEBVTT"):
        return parse_vtt(content)
    if stripped.startswith("{"):
        try:
            return parse_json3(content)
        except Exception:  # noqa: BLE001 - 只取 "body" 结构，失败就换下一路
            pass
        try:
            return parse_bilibili_json(content)
        except Exception:  # noqa: BLE001
            return []
    if "-->" in content:
        # SRT 与 VTT 的差别只在毫秒分隔符与时间轴行规整度，两类解析器都能吃；取条数多的
        srt = parse_srt(content)
        vtt = parse_vtt(content)
        return srt if len(srt) >= len(vtt) else vtt
    return []


# ---------------------------------------------------------------------------
# 语义合并：把碎片化的时间轴条目并成句子
# ---------------------------------------------------------------------------

def merge_cues(cues: list[dict]) -> list[dict]:
    """把逐词 / 短句级字幕合并成可读句子。

    平台字幕（尤其是自动生成的那种）往往两三秒一条、一条只有半句话，
    直接拿去当稿件是没法看的。这里按「句子未完就继续接」的规则合句，
    同时用停顿长度和长度上限兜底，避免合出一大坨。
    """
    merged: list[dict] = []
    buffer: Optional[dict] = None

    for cue in cues:
        text = cue["text"]
        if buffer is None:
            buffer = {"start": cue["start"], "end": cue["end"], "text": text}
            continue

        gap = cue["start"] - buffer["end"]
        pending = buffer["text"]
        ends_sentence = pending[-1] in _STRONG_END and len(pending) >= _MIN_SENTENCE_CHARS
        should_break = (
            gap > _GAP_BREAK
            or ends_sentence
            or len(pending) >= _MAX_SEGMENT_CHARS
        )

        if should_break:
            merged.append(buffer)
            buffer = {"start": cue["start"], "end": cue["end"], "text": text}
        else:
            buffer["text"] = _join_text(pending, text)
            buffer["end"] = cue["end"]

    if buffer is not None:
        merged.append(buffer)

    for item in merged:
        item["start"] = round(item["start"], 2)
        item["end"] = round(item["end"], 2)
    return [item for item in merged if item["text"].strip()]


def _join_text(left: str, right: str) -> str:
    """拼接两段字幕文本：中文之间不加空格，西文之间补一个空格。"""
    if not left:
        return right
    if not right:
        return left
    # 上一段以标点结尾，或两侧都是中日韩字符 → 直接相连
    if left[-1] in _SENTENCE_END + "，,、" or _is_cjk(left[-1]) or _is_cjk(right[0]):
        return left + right
    return f"{left} {right}"


def _is_cjk(char: str) -> bool:
    return "\u3000" <= char <= "\u9fff" or "\uff00" <= char <= "\uffef"


# ---------------------------------------------------------------------------
# 轨道选择
# ---------------------------------------------------------------------------

def _lang_rank(lang: str, want: str) -> int:
    order = _LANGUAGE_ORDER.get(want) or _LANGUAGE_ORDER["auto"]
    if lang in order:
        return order.index(lang)
    lowered = lang.lower()
    for index, preferred in enumerate(order):
        if lowered.startswith(preferred.lower().split("-")[0]):
            return index + len(order)
    return len(order) * 2 + 1


def describe(info: dict) -> dict:
    """从 yt-dlp 的 info 里摘出字幕可用性（零额外网络请求）。"""
    manual = list((info.get("subtitles") or {}).keys())
    auto = list((info.get("automatic_captions") or {}).keys())
    return {"manual": manual, "auto": auto, "has_any": bool(manual or auto)}


def pick_track(
    files: dict[str, Path], info: dict, language: str
) -> Optional[tuple[str, Path, str]]:
    """在已落盘的字幕里挑最合适的一条，返回 ``(语言键, 文件路径, 类型)``。

    人工字幕整体优先于自动字幕 —— 哪怕自动字幕的语言键更贴合，
    人工字幕的质量也值得这一次取舍。
    """
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}

    for kind, pool in (("manual", manual), ("auto", auto)):
        langs = [lang for lang in files if lang in pool]
        if langs:
            best = min(langs, key=lambda lang: _lang_rank(lang, language))
            return best, files[best], kind

    # 少数抽取器不回填 subtitles / automatic_captions，退化为按文件名判断
    if files:
        best = min(files, key=lambda lang: _lang_rank(lang, language))
        return best, files[best], "manual" if manual else "auto"
    return None


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------

def collect_files(work_dir: Path, prefix: str = "sub.") -> dict[str, Path]:
    """收集 yt-dlp 写下的字幕文件，返回 ``{语言键: 路径}``。"""
    found: dict[str, Path] = {}
    for path in sorted(work_dir.glob(f"{prefix}*")):
        if not path.is_file():
            continue
        stem = path.name[len(prefix):]
        lang = stem.rsplit(".", 1)[0] if "." in stem else stem
        # 同一语言有多种格式时，优先 vtt / srt（解析器最稳）
        current = found.get(lang)
        if current is None or _fmt_rank(path) < _fmt_rank(current):
            found[lang] = path
    return found


def _fmt_rank(path: Path) -> int:
    return {".vtt": 0, ".srt": 1, ".json": 2, ".json3": 2, ".ass": 3, ".ssa": 3, ".lrc": 4}.get(
        path.suffix.lower(), 5
    )


def build_options(cfg: dict, url: str, language: str, work_dir: Path) -> dict:
    """组装只抓字幕、不碰视频流的 yt-dlp 选项。"""
    from .engine import resolve_proxy  # 局部导入，避免与 engine 形成循环依赖

    patterns = list(_FETCH_PATTERNS.get(language) or _FETCH_PATTERNS["auto"])
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "skip_download": True,                  # 不下载视频，只取字幕
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": patterns,
        "subtitlesformat": "vtt/srt/best",
        "outtmpl": str(work_dir / "sub.%(ext)s"),
        "retries": 3,
        "socket_timeout": 30,
        "no_color": True,
    }
    try:
        proxy = resolve_proxy(url, cfg)
    except Exception:  # noqa: BLE001
        proxy = None
    if proxy:
        opts["proxy"] = proxy
    browser = (cfg.get("cookies_from_browser") or "").strip()
    if browser:
        opts["cookiesfrombrowser"] = (browser,)
    return opts


def fetch(url: str, cfg: dict, language: str, work_dir: Path) -> Optional[SubtitleResult]:
    """抓取平台字幕并解析成句子。

    返回 ``None`` 表示该视频没有可用字幕 —— 这是正常情况，调用方应回落到语音识别。
    """
    import yt_dlp

    work_dir.mkdir(parents=True, exist_ok=True)
    opts = build_options(cfg, url, language, work_dir)

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True) or {}

    if info.get("_type") == "playlist":
        entries = [entry for entry in (info.get("entries") or []) if entry]
        if entries:
            info = entries[0]

    files = collect_files(work_dir)
    chosen = pick_track(files, info, language)
    if chosen is None:
        return None

    lang, path, kind = chosen
    cues = parse_subtitle_file(path)
    if not cues:
        return None

    segments = merge_cues(cues)
    if not segments:
        return None

    return SubtitleResult(
        segments=segments,
        language=lang,
        kind=kind,
        fmt=path.suffix.lstrip(".").lower(),
    )
