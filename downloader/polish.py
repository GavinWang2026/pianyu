"""文案后处理：本地规则整理 + 大模型整理。

刚提取出来的文案是「生」的：语音识别没有标点、平台字幕一句话切成八段、
偶尔还夹着重复片段。这里负责把它变成能直接用的稿子。

两条路线，按可用性自动降级：

1. **本地规则**（零依赖、秒出）—— 去重、标点全角化、按停顿分段、繁转简。
   它**不会凭空造标点**，只做有把握的事。
2. **大模型整理**（需要配置模型）—— 补标点、按语义分段、压缩口头禅，
   另外产出摘要与关键词。它有能力改写，所以提示词里把「不许增删内容」写死。

大模型接入刻意走 **OpenAI 兼容协议**（``POST {base_url}/chat/completions``），
一份配置就能对接本机 Ollama、DeepSeek、通义、OpenAI 等——用户想用哪个都行，
不必为每家写适配。请求全用标准库 ``urllib``，不引第三方 HTTP 客户端。
"""

from __future__ import annotations

import json
import queue
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

from .asr import resolve_python
from .config import config

TERMINAL_STAGES = ("done", "error", "cancelled")

# 可选的整理动作
OPERATIONS = ("clean", "summary", "keywords")
OPERATION_LABELS = {
    "clean": "标点修复 + 自动分段",
    "summary": "内容摘要",
    "keywords": "关键词",
}

# 单次喂给模型的字符上限。超出就分片处理，避免把上下文撑爆。
CHUNK_CHARS = 6000
# 摘要阶段输入上限（超长文稿先压缩再摘要）
SUMMARY_INPUT_CHARS = 12000
# 摘要 / 关键词的输出预算
INSIGHT_MAX_TOKENS = 1600

# 已知的「思考型」模型家族：它们默认先输出一长段思维链，
# 若不关掉，用户会盯着空白等半天，而且正文容易被 max_tokens 挤掉。
_THINKING_FAMILIES = ("qwen3", "qwq", "deepseek-r1", "reasoning", "magistral")
_NO_THINK_SWITCH = "/no_think"

_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?…；;\n])")


def _flatten_block(text: str) -> str:
    """把一段文本内部的零散换行处理掉。

    模型经常在一段中间插一个软换行（尤其是换行后的新句子），
    直接留着会在界面里表现为莫名其妙的位置断行。段内的换行统一消掉，
    段与段的分隔（空行）由调用方用 ``\\n\\n`` 重新拼装。
    """
    lines = [line.strip() for line in (text or "").split("\n")]
    lines = [line for line in lines if line]
    if not lines:
        return ""
    result = lines[0]
    for line in lines[1:]:
        joiner = "" if (_is_cjk(result[-1:]) or _is_cjk(line[:1])) else " "
        result = result + joiner + line
    return result.strip()


# ---------------------------------------------------------------------------
# 本地规则整理
# ---------------------------------------------------------------------------

# 半角 → 全角（仅在中文语境里替换，英文原文里的逗号句点保持原样）
_HALF_TO_FULL = {
    ",": "，", ";": "；", ":": "：", "?": "？", "!": "！",
    "(": "（", ")": "）",
}
_RE_CJK = re.compile(r"[\u4e00-\u9fff\u3000-\u303f]")
_RE_REPEAT_PUNCT = re.compile(r"([，。！？；：、,.!?;:])\1{1,}")


def _is_cjk(char: str) -> bool:
    return bool(_RE_CJK.match(char))


def normalize_punctuation(text: str, language: str = "zh") -> str:
    """把中文语境里的半角标点换成全角，并折叠重复标点。

    语音识别（尤其 faster-whisper）经常输出 ``今天天气不错,我们...``
    这种半角逗号。中文稿子里它是明显的瑕疵，改掉几乎零风险。
    """
    if language not in ("zh", "auto"):
        return text

    chars = list(text)
    for index, char in enumerate(chars):
        full = _HALF_TO_FULL.get(char)
        if not full:
            continue
        # 前后任一侧出现中日韩字符，才认定这是中文语境的标点
        left = chars[index - 1] if index > 0 else ""
        right = chars[index + 1] if index + 1 < len(chars) else ""
        if _is_cjk(left) or _is_cjk(right):
            chars[index] = full
    return _RE_REPEAT_PUNCT.sub(r"\1", "".join(chars))


def dedupe_segments(segments: list[dict]) -> list[dict]:
    """丢掉识别引擎抽风导致的连续重复片段。

    Whisper 在噪声或音乐段上偶尔会卡住，把同一句话重复吐十几遍；
    平台自动字幕也会出现整条重复。

    判定刻意**只认完全重复**（忽略空白与标点）。曾经用过「包含即相似」的
    宽松规则，结果把「欢迎来到本期节目吧」并进了「欢迎来到本期节目」——
    丢内容比留一条重复严重得多，宁可漏杀不可误杀。
    """
    result: list[dict] = []
    for segment in segments:
        text = (segment.get("text") or "").strip()
        if not text:
            continue
        if result and _normalize_for_compare(result[-1]["text"]) == _normalize_for_compare(text):
            result[-1]["end"] = max(result[-1]["end"], segment.get("end") or result[-1]["end"])
            continue
        result.append(dict(segment))
    return result


_STRIP_FOR_COMPARE = re.compile(r"[\s，。！？；：、,.!?;:…~～\-—]+")


def _normalize_for_compare(text: str) -> str:
    return _STRIP_FOR_COMPARE.sub("", text or "").lower()


def align_timings(paragraph_texts: list[str], segments: list[dict]) -> list[dict]:
    """把「整理后的段落」对齐回原始时间轴。

    大模型会重新划分段落，段数与原始分段对不上，因此**绝不能按序号对齐** ——
    那样段数不匹配时后面的段落会全部挤到同一段时间上（实测踩过：
    五段文案全被标成同一个时间区间，是彻头彻尾的假精确）。

    这里改用**文本比例对齐**：标点在比较时被剥离，于是「整理后文本」与
    「原始文本」在可比较流里长度接近；按字符偏移的占比映射回原始时间轴，
    结果单调、不重叠，误差在一个分句以内 —— 用来做「跳到视频对应位置」足够准。
    """
    def comparable(text: str) -> str:
        return _STRIP_FOR_COMPARE.sub("", text or "")

    src_units: list[tuple[int, float, float]] = []   # (长度, 起点, 终点)
    for segment in segments:
        length = len(comparable(segment.get("text") or ""))
        if length <= 0:
            continue
        start = float(segment.get("start") or 0.0)
        end = float(segment.get("end") or start)
        src_units.append((length, start, end))

    if not src_units:
        return [{"text": text, "start": 0.0, "end": 0.0} for text in paragraph_texts]

    src_total = sum(unit[0] for unit in src_units)
    positions: list[int] = []                        # 每个原始分段在流中的起点
    cursor = 0
    for length, _start, _end in src_units:
        positions.append(cursor)
        cursor += length

    def time_at(offset: int) -> float:
        """给定可比较流上的偏移，返回对应的秒数（取包含该偏移的分段边界）。"""
        offset = max(0, min(offset, src_total - 1))
        for index, position in enumerate(positions):
            length, start, end = src_units[index]
            if offset < position + length:
                # 落在分段内部：按段内比例插值，避免整段抖到同一个时间点
                ratio = (offset - position) / max(1, length)
                return start + (end - start) * ratio
        return src_units[-1][2]

    clean_lengths = [len(comparable(text)) for text in paragraph_texts]
    clean_total = sum(clean_lengths) or 1

    result: list[dict] = []
    clean_cursor = 0
    for text, length in zip(paragraph_texts, clean_lengths):
        head = clean_cursor / clean_total
        tail = (clean_cursor + max(1, length)) / clean_total
        result.append({
            "text": text,
            "start": round(time_at(int(head * src_total)), 2),
            "end": round(time_at(int(tail * src_total) - 1), 2),
        })
        clean_cursor += length
    return result


def segment_paragraphs(
    segments: list[dict],
    *,
    gap_break: float = 1.4,
    intonation_gap: float = 0.4,
    max_chars: int = 220,
) -> list[dict]:
    """把逐句文案归拢成自然段，保留时间范围。

    这是「自动分段」的零依赖版本：不改字，只决定在哪里分段。

    断段依据两条：
    * **长停顿**（超过 ``gap_break``）—— 说话人自己划出的边界，最可靠；
    * **句末标点 + 短停顿**（超过 ``intonation_gap``）—— 句子说完并稍作停顿。

    刻意不做「见句号就分段」：那样一段只有一句话，等于没分段。
    """
    paragraphs: list[dict] = []
    buffer: Optional[dict] = None

    for segment in segments:
        text = (segment.get("text") or "").strip()
        if not text:
            continue
        start = float(segment.get("start") or 0.0)
        end = float(segment.get("end") or start)

        if buffer is None:
            buffer = {"text": text, "start": start, "end": end, "count": 1}
            continue

        gap = start - buffer["end"]
        sentence_done = buffer["text"][-1:] in "。！？!?…"
        should_break = (
            gap > gap_break
            or (sentence_done and gap >= intonation_gap)
            or len(buffer["text"]) >= max_chars
        )
        if should_break:
            paragraphs.append(buffer)
            buffer = {"text": text, "start": start, "end": end, "count": 1}
        else:
            joiner = "" if (_is_cjk(buffer["text"][-1:]) or _is_cjk(text[:1])) else " "
            buffer["text"] = buffer["text"] + joiner + text
            buffer["end"] = end
            buffer["count"] += 1

    if buffer is not None:
        paragraphs.append(buffer)

    for item in paragraphs:
        item["start"] = round(item["start"], 2)
        item["end"] = round(item["end"], 2)
        item["text"] = normalize_punctuation(item["text"])
    return paragraphs


def to_simplified(text: str) -> str:
    """繁转简。

    转换表放在 ``.venv-asr`` 里（zhconv，一个小纯 Python 包），
    这里以子进程方式调用 —— 主环境「只依赖 yt-dlp」这条底线因此得以保持。
    环境缺失时原样返回，不报错（老实用繁体，也比乱转一通强）。
    """
    python = resolve_python()
    if python is None or not text:
        return text
    script = "import sys,zhconv;sys.stdout.write(zhconv.convert(sys.stdin.read(),'zh-cn'))"
    try:
        proc = subprocess.run(
            [str(python), "-c", script],
            input=text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return text
    converted = (proc.stdout or "").strip()
    return converted if proc.returncode == 0 and converted else text


def local_polish(
    segments: list[dict],
    *,
    language: str = "zh",
    convert_simplified: bool = False,
) -> dict:
    """零依赖整理：去重 → 分段 → 标点归一。返回与任务结果同构的字典。"""
    cleaned = dedupe_segments(segments)
    paragraphs = segment_paragraphs(cleaned, gap_break=1.4)

    if convert_simplified and language in ("zh", "auto"):
        for item in paragraphs:
            item["text"] = to_simplified(item["text"])
        joined = "\n\n".join(item["text"] for item in paragraphs)
    else:
        joined = "\n\n".join(item["text"] for item in paragraphs)

    return {
        "text": joined,
        "paragraphs": paragraphs,
        "summary": "",
        "keywords": [],
        "engine": "本地规则",
    }


# ---------------------------------------------------------------------------
# 大模型客户端（OpenAI 兼容）
# ---------------------------------------------------------------------------

class LLMError(RuntimeError):
    """大模型调用失败，消息已本地化，可直接展示给用户。"""


def _friendly_http_error(status: int, body: str) -> str:
    detail = ""
    try:
        parsed = json.loads(body)
        detail = str(
            (parsed.get("error") or {}).get("message")
            or parsed.get("message")
            or parsed.get("detail")
            or ""
        )[:160]
    except Exception:  # noqa: BLE001
        detail = (body or "").strip()[:160]

    table = {
        400: "请求被模型服务拒绝（可能是模型名不正确）",
        401: "API 密钥无效或未提供",
        403: "密钥没有访问该模型的权限",
        404: "接口地址不正确，或该模型不存在",
        429: "请求过于频繁或额度不足，请稍后重试",
        500: "模型服务内部错误",
        502: "模型服务网关错误",
        503: "模型服务暂时不可用",
    }
    message = table.get(status, f"模型服务返回错误（HTTP {status}）")
    return f"{message}：{detail}" if detail else message


def _normalize_base_url(raw: str) -> str:
    """把用户填的地址归一成 ``.../v1`` 形态。

    用户很可能只填 ``http://127.0.0.1:11434``（Ollama 的根地址），
    或者直接贴了完整的 ``.../v1/chat/completions``；这里统一收敛。
    """
    url = (raw or "").strip().rstrip("/")
    if not url:
        return ""
    for suffix in ("/chat/completions", "/completions"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    if not url.endswith("/v1") and "/v1/" not in url:
        url = f"{url}/v1"
    return url


def list_models(base_url: str, api_key: str = "", timeout: float = 12.0) -> list[str]:
    """拉取模型清单（``GET {base_url}/models``）。用于设置页一键选模型。"""
    root = _normalize_base_url(base_url)
    if not root:
        raise LLMError("请先填写模型服务地址")

    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(f"{root}/models", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise LLMError(_friendly_http_error(exc.code, exc.read().decode("utf-8", "replace"))) from exc
    except urllib.error.URLError as exc:
        raise LLMError(f"连接模型服务失败：{exc.reason}. 请确认服务已启动、地址填写正确") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise LLMError("模型服务返回的内容无法解析，请确认地址指向 OpenAI 兼容接口") from exc

    rows = payload.get("data") if isinstance(payload, dict) else payload
    models: list[str] = []
    for row in rows or []:
        if isinstance(row, dict):
            name = row.get("id") or row.get("name") or row.get("model")
        else:
            name = str(row)
        if name:
            models.append(str(name))
    return sorted(set(models))


def _is_thinking_model(model: str) -> bool:
    lowered = (model or "").lower()
    return any(family in lowered for family in _THINKING_FAMILIES)


def chat_stream(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    *,
    on_delta: Optional[Callable[[str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    max_tokens: int = 4096,
    temperature: float = 0.3,
) -> str:
    """流式对话，逐段回调增量文本，返回完整回复。

    走 SSE（``stream: true``）。若服务端无视 ``stream`` 直接回整包 JSON，
    这里也能识别并降级处理 —— 有些自建网关就是这么干的。
    """
    root = _normalize_base_url(base_url)
    if not root:
        raise LLMError("请先填写模型服务地址")
    if not (model or "").strip():
        raise LLMError("请先选择要使用的模型")

    body: dict = {
        "model": model,
        "messages": messages,
        "stream": True,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    request = urllib.request.Request(f"{root}/chat/completions", data=data, headers=headers)
    pieces: list[str] = []
    reasoning_chars = 0

    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            content_type = (response.headers.get("Content-Type") or "").lower()
            if "text/event-stream" not in content_type:
                # 服务端没按流式返回，整体读回来解析
                text = _extract_full(response.read().decode("utf-8", "replace"))
                if on_delta and text:
                    on_delta(text)
                return text

            for raw in response:
                if cancel_event is not None and cancel_event.is_set():
                    raise LLMError("已取消")
                line = raw.decode("utf-8", "replace").strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload in ("[DONE]", ""):
                    break
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    continue

                choices = event.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                # 思考型模型会把思维链塞进 reasoning / thinking 字段，
                # 那部分不该混进正文，只统计长度用于诊断
                thinking = delta.get("reasoning") or delta.get("thinking") or ""
                if thinking:
                    reasoning_chars += len(thinking)
                piece = delta.get("content") or ""
                if not piece:
                    continue
                pieces.append(piece)
                if on_delta:
                    on_delta(piece)
    except urllib.error.HTTPError as exc:
        raise LLMError(_friendly_http_error(exc.code, exc.read().decode("utf-8", "replace"))) from exc
    except urllib.error.URLError as exc:
        raise LLMError(f"连接模型服务失败：{exc.reason}. 请确认服务已启动、地址填写正确") from exc

    text = "".join(pieces).strip()
    if not text:
        if reasoning_chars > 0:
            raise LLMError(
                "模型只输出了思考过程，没有产出正文。请在设置里改用非思考型模型，"
                "或把「关闭思考模式」保持开启。"
            )
        raise LLMError("模型没有返回任何内容，请检查模型名是否正确")
    return text


def _extract_full(raw: str) -> str:
    """从非流式响应里取正文。"""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMError("模型服务返回的内容无法解析") from exc
    choices = payload.get("choices") or []
    if not choices:
        raise LLMError("模型服务返回结构异常（缺少 choices）")
    message = choices[0].get("message") or {}
    return str(message.get("content") or "").strip()


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------

_CLEAN_SYSTEM = (
    "你是一名严谨的中文文字编辑。用户给你一段由语音识别或平台字幕得到的原始文本，"
    "它可能没有标点、断句混乱、夹杂口头禅与重复片段。\n"
    "请你在**绝不增删内容、绝不改写事实**的前提下完成整理：\n"
    "1. 补全标点，中文一律使用全角标点；\n"
    "2. 按语义划分自然段，段与段之间空一行；\n"
    "3. 删去识别错误造成的重复片段与明显口头禅（如「然后然后」「就是就是」）；\n"
    "4. 修正明显的同音字错误。\n"
    "只输出整理后的正文，不要标题、不要解释、不要使用代码块包裹。"
)

_INSIGHT_SYSTEM = (
    "你是内容编辑。请阅读用户提供的文稿，输出一个 JSON 对象，字段如下：\n"
    '{"summary": "不超过 150 字的中文摘要，抓住核心信息",'
    ' "keywords": ["关键词", "…"]}\n'
    "keywords 给出 5 到 8 个最能代表主题的词或短语。\n"
    "只输出 JSON 本身，不要代码块、不要任何解释文字。"
)

_CLEAN_CHUNK_NOTE = (
    "\n注意：这段文本是一份长文稿的第 {index}/{total} 段，"
    "请只整理这一段，不要在结尾补充总结，也不要承接上一段的语气。"
)


def _messages(system: str, user: str) -> list[dict]:
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _apply_system(content: str, model: str, disable_thinking: bool) -> str:
    """给思考型模型加一个「别想了」开关。

    Qwen3 / QwQ 这类模型默认先吐一长串思维链，既拖时间又容易挤掉正文。
    它们支持 ``/no_think`` 软开关，加在系统提示词末尾即可；
    非思考型模型不加，免得平白混入噪声。
    """
    if disable_thinking and _is_thinking_model(model) and _NO_THINK_SWITCH not in content:
        return f"{content}\n{_NO_THINK_SWITCH}"
    return content


def split_text(text: str, limit: int = CHUNK_CHARS) -> list[str]:
    """按句子边界切分长文本，尽量不切在句子中间。"""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []

    chunks: list[str] = []
    buffer = ""
    for piece in _SENTENCE_SPLIT.split(text):
        if not piece:
            continue
        if len(buffer) + len(piece) > limit and buffer:
            chunks.append(buffer)
            buffer = piece
        else:
            buffer += piece
    if buffer.strip():
        chunks.append(buffer)
    return chunks


def parse_insight(raw: str) -> dict:
    """从模型回复里抠出 JSON（容忍代码块包裹与前后废话）。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise LLMError("模型没有按要求返回 JSON，可尝试更换模型")

    try:
        payload = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError("模型返回的 JSON 无法解析，可尝试更换模型") from exc
    if not isinstance(payload, dict):
        raise LLMError("模型返回的 JSON 结构不符合预期")

    summary = str(payload.get("summary") or "").strip()
    raw_keywords = payload.get("keywords") or payload.get("tags") or []
    if isinstance(raw_keywords, str):
        raw_keywords = re.split(r"[,，、;；\s]+", raw_keywords)
    keywords = [str(item).strip() for item in raw_keywords if str(item).strip()][:12]
    return {"summary": summary, "keywords": keywords}


# ---------------------------------------------------------------------------
# 后处理任务
# ---------------------------------------------------------------------------

@dataclass
class PolishTask:
    """一次文案整理任务。

    ``source_id`` 指向文案提取任务，前端据此把整理结果贴回同一个文案面板。
    """

    id: str
    source_id: str = ""
    title: str = ""
    language: str = "zh"
    operations: list[str] = field(default_factory=lambda: ["clean", "summary", "keywords"])
    input_chars: int = 0
    convert_simplified: bool = False

    stage: str = "queued"     # queued|local|cleaning|insight|done|error|cancelled
    percent: float = 0.0
    message: str = "准备中…"

    text: str = ""
    paragraphs: list[dict] = field(default_factory=list)
    summary: str = ""
    keywords: list[str] = field(default_factory=list)
    engine: str = ""
    cleaned: bool = False

    error: str = ""
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


class PolishManager:
    """线程安全的整理任务注册表 + 事件总线（与 AsrManager 同构）。"""

    def __init__(self) -> None:
        self._tasks: dict[str, PolishTask] = {}
        self._subscribers: dict[str, list[queue.Queue]] = {}
        self._cancels: dict[str, threading.Event] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 查询

    def list_tasks(self) -> list[dict]:
        with self._lock:
            tasks = sorted(self._tasks.values(), key=lambda t: t.created_at, reverse=True)
            return [task.to_dict() for task in tasks]

    def get_task(self, task_id: str) -> Optional[dict]:
        with self._lock:
            task = self._tasks.get(task_id)
            return task.to_dict() if task else None

    def latest_for(self, source_id: str) -> Optional[dict]:
        """取某个文案任务最近一次整理结果（前端刷新后恢复状态用）。"""
        with self._lock:
            matches = [t for t in self._tasks.values() if t.source_id == source_id]
            if not matches:
                return None
            return max(matches, key=lambda t: t.created_at).to_dict()

    # ------------------------------------------------------------------ 订阅

    def subscribe(self, task_id: str) -> queue.Queue:
        channel: queue.Queue = queue.Queue(maxsize=2048)
        with self._lock:
            self._subscribers.setdefault(task_id, []).append(channel)
            task = self._tasks.get(task_id)
            snapshot = task.to_dict() if task else None
        if snapshot is not None:
            # 任务若已结束，先补发完整结果，避免「秒完成 → 结果收不到」
            if snapshot.get("stage") == "done":
                self._enqueue(channel, {
                    "type": "result",
                    "text": snapshot.get("text", ""),
                    "paragraphs": snapshot.get("paragraphs", []),
                    "summary": snapshot.get("summary", ""),
                    "keywords": snapshot.get("keywords", []),
                })
            self._enqueue(channel, {"type": "task", "task": snapshot})
        return channel

    @staticmethod
    def _enqueue(channel: queue.Queue, event: dict) -> None:
        try:
            channel.put_nowait(event)
        except queue.Full:
            pass

    def unsubscribe(self, task_id: str, channel: queue.Queue) -> None:
        with self._lock:
            subs = self._subscribers.get(task_id)
            if subs and channel in subs:
                subs.remove(channel)
            if subs is not None and not subs:
                self._subscribers.pop(task_id, None)

    def _broadcast(self, task_id: str, event: dict) -> None:
        with self._lock:
            subs = list(self._subscribers.get(task_id, ()))
        for channel in subs:
            try:
                channel.put_nowait(event)
            except queue.Full:
                pass

    def _patch(self, task: PolishTask, **fields) -> None:
        with self._lock:
            for key, value in fields.items():
                if hasattr(task, key):
                    setattr(task, key, value)
            snapshot = task.to_dict()
        self._broadcast(task.id, {"type": "task", "task": snapshot})

    # ------------------------------------------------------------------ 控制

    def create_task(
        self,
        segments: list[dict],
        *,
        source_id: str = "",
        title: str = "",
        language: str = "zh",
        operations: Optional[list[str]] = None,
        convert_simplified: bool = False,
    ) -> dict:
        ops = [op for op in (operations or []) if op in OPERATIONS] or list(OPERATIONS)
        task = PolishTask(
            id=uuid.uuid4().hex[:12],
            source_id=source_id,
            title=title,
            language=language if language in ("zh", "en", "auto") else "zh",
            operations=ops,
            input_chars=sum(len(s.get("text") or "") for s in segments),
            convert_simplified=bool(convert_simplified),
        )
        with self._lock:
            self._tasks[task.id] = task
            self._cancels[task.id] = threading.Event()

        worker = threading.Thread(
            target=self._run, args=(task, list(segments)), name=f"polish-{task.id}", daemon=True
        )
        worker.start()
        return task.to_dict()

    def cancel_task(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            event = self._cancels.get(task_id)
        if not task or task.stage in TERMINAL_STAGES:
            return False
        if event is not None:
            event.set()
        self._patch(task, stage="cancelled", message="已取消", finished_at=time.time())
        return True

    def clear_finished(self) -> int:
        with self._lock:
            done = [tid for tid, task in self._tasks.items() if task.stage in TERMINAL_STAGES]
            for tid in done:
                self._tasks.pop(tid, None)
                self._cancels.pop(tid, None)
        return len(done)

    # ------------------------------------------------------------------ 执行

    def _run(self, task: PolishTask, segments: list[dict]) -> None:
        cancel = self._cancels.get(task.id) or threading.Event()
        try:
            cfg = config.as_dict()
            base_url = _normalize_base_url(cfg.get("llm_base_url") or "")
            model = str(cfg.get("llm_model") or "").strip()
            api_key = str(cfg.get("llm_api_key") or "").strip()
            disable_thinking = bool(cfg.get("llm_disable_thinking", True))
            use_llm = bool(base_url and model)

            # ---- 1. 本地规则整理（始终先跑一遍，作为底座与兜底） ----
            self._patch(
                task, stage="local", percent=4.0,
                message=f"本地整理…（{'接下来交给 ' + model if use_llm else '未配置模型，仅用本地规则'}）",
            )
            lowered = [dict(s) for s in segments]
            base = local_polish(
                lowered,
                language=task.language,
                convert_simplified=task.convert_simplified,
            )
            self._set_result(task, base["text"], base["paragraphs"], "", [])
            self._patch(task, engine="本地规则", cleaned=False)

            if not use_llm:
                if "clean" in task.operations or "summary" in task.operations or "keywords" in task.operations:
                    self._broadcast(task.id, {
                        "type": "log",
                        "message": "未配置大模型，标点修复 / 摘要 / 关键词已跳过（可在设置中配置）",
                    })
                self._finish(task)
                return

            source_text = self._flatten(segments, base["text"])

            # ---- 2. 标点修复 + 分段 ----
            if "clean" in task.operations:
                cleaned = self._clean(task, source_text, base_url, api_key, model, disable_thinking, cancel)
                if cancel.is_set():
                    return
                if cleaned:
                    blocks = [_flatten_block(block) for block in cleaned.split("\n\n")]
                    blocks = [block for block in blocks if block]
                    # 时间轴按文本比例对齐回原始分段，而不是按段落序号
                    paragraphs = align_timings(blocks, segments) if blocks else base["paragraphs"]
                    for block, para in zip(blocks, paragraphs):
                        para["text"] = normalize_punctuation(block, task.language)
                    # 段落之间统一用空行分隔，避免混入模型给的零散换行
                    self._set_result(
                        task, "\n\n".join(blocks), paragraphs, task.summary, task.keywords
                    )
                    self._patch(task, cleaned=True)

            # ---- 3. 摘要 + 关键词 ----
            if cancel.is_set():
                return
            if "summary" in task.operations or "keywords" in task.operations:
                insight = self._insight(task, task.text or source_text, base_url, api_key, model,
                                        disable_thinking, cancel)
                if cancel.is_set():
                    return
                if insight:
                    self._set_result(
                        task, task.text,
                        task.paragraphs or base["paragraphs"],
                        insight.get("summary", "") if "summary" in task.operations else "",
                        insight.get("keywords", []) if "keywords" in task.operations else [],
                    )

            self._patch(task, engine=model)
            self._finish(task)

        except Exception as exc:  # noqa: BLE001 - 统一收敛为任务级错误
            if task.stage not in TERMINAL_STAGES:
                message = str(exc) if isinstance(exc, LLMError) else f"整理失败：{str(exc)[:200]}"
                self._patch(task, stage="error", message=message, error=str(exc)[:600],
                            finished_at=time.time())
        finally:
            with self._lock:
                self._cancels.pop(task.id, None)

    # ------------------------------------------------------------------ 各阶段

    @staticmethod
    def _flatten(segments: list[dict], fallback: str) -> str:
        text = "".join((s.get("text") or "") for s in segments).strip()
        return text or fallback

    def _clean(
        self, task: PolishTask, text: str, base_url: str, api_key: str,
        model: str, disable_thinking: bool, cancel: threading.Event,
    ) -> str:
        chunks = split_text(text, CHUNK_CHARS) or [text]
        system = _apply_system(_CLEAN_SYSTEM, model, disable_thinking)
        outputs: list[str] = []

        for index, chunk in enumerate(chunks):
            if cancel.is_set():
                return ""
            if len(chunks) > 1:
                system_chunk = system + _CLEAN_CHUNK_NOTE.format(index=index + 1, total=len(chunks))
            else:
                system_chunk = system

            self._patch(
                task, stage="cleaning",
                percent=6.0 + 64.0 * (index / len(chunks)),
                message=f"标点修复与分段 {index + 1}/{len(chunks)}",
            )
            self._broadcast(task.id, {"type": "delta", "op": "clean", "index": index, "reset": True})

            def on_delta(piece: str, _index: int = index) -> None:
                if cancel.is_set():
                    return
                self._broadcast(task.id, {
                    "type": "delta", "op": "clean", "index": _index, "text": piece,
                })

            result = chat_stream(
                base_url, api_key, model,
                _messages(system_chunk, chunk),
                on_delta=on_delta, cancel_event=cancel,
            )
            outputs.append(self._strip_wrapping(result))

        return "\n\n".join(outputs).strip()

    def _insight(
        self, task: PolishTask, text: str, base_url: str, api_key: str,
        model: str, disable_thinking: bool, cancel: threading.Event,
    ) -> dict:
        self._patch(task, stage="insight", percent=74.0, message="生成摘要与关键词…")
        self._broadcast(task.id, {"type": "delta", "op": "insight", "reset": True})

        payload = text
        if len(payload) > SUMMARY_INPUT_CHARS:
            payload = payload[:SUMMARY_INPUT_CHARS] + "\n…（文本过长，已截取前段用于摘要）"

        raw = chat_stream(
            base_url, api_key, model,
            _messages(_apply_system(_INSIGHT_SYSTEM, model, disable_thinking), payload),
            on_delta=lambda piece: self._broadcast(
                task.id, {"type": "delta", "op": "insight", "text": piece}
            ),
            cancel_event=cancel,
            max_tokens=INSIGHT_MAX_TOKENS,
            temperature=0.2,
        )
        return parse_insight(raw)

    # ------------------------------------------------------------------ 小工具

    @staticmethod
    def _strip_wrapping(text: str) -> str:
        """模型有时会不听话地用代码块把正文裹起来，这里剥掉。"""
        stripped = text.strip()
        if stripped.startswith("```"):
            stripped = re.sub(r"^```[a-zA-Z]*\s*", "", stripped)
            stripped = re.sub(r"\s*```$", "", stripped)
        return stripped.strip()

    def _set_result(
        self, task: PolishTask, text: str, paragraphs: list[dict],
        summary: str, keywords: list[str],
    ) -> None:
        self._patch(
            task,
            text=text or "",
            paragraphs=paragraphs or [],
            summary=summary or "",
            keywords=keywords or [],
        )
        self._broadcast(task.id, {
            "type": "result",
            "text": text or "",
            "paragraphs": paragraphs or [],
            "summary": summary or "",
            "keywords": keywords or [],
        })

    def _finish(self, task: PolishTask) -> None:
        if task.stage in TERMINAL_STAGES:
            return
        self._patch(
            task,
            stage="done",
            percent=100.0,
            message=f"整理完成（{task.engine or '本地规则'}）",
            finished_at=time.time(),
        )


manager = PolishManager()


# ---------------------------------------------------------------------------
# 能力探测
# ---------------------------------------------------------------------------

def probe() -> dict:
    """报告整理能力的可用情况，供前端决定按钮状态。"""
    cfg = config.as_dict()
    base_url = _normalize_base_url(cfg.get("llm_base_url") or "")
    model = str(cfg.get("llm_model") or "").strip()
    return {
        "llm_ready": bool(base_url and model),
        "base_url": base_url,
        "model": model,
        "operations": [
            {"id": op, "label": OPERATION_LABELS[op], "needs_llm": op != "clean"}
            for op in OPERATIONS
        ],
        "local_operations": ["去重", "按停顿分段", "标点全角化"],
    }


def env_hint() -> str:
    """返回本机可能已有的模型服务地址（用于设置页「一键填入」）。"""
    candidates = (
        "http://127.0.0.1:11434/v1",   # Ollama
        "http://127.0.0.1:1234/v1",    # LM Studio
        "http://127.0.0.1:8000/v1",    # vLLM / 本地网关
    )
    for url in candidates:
        parsed = urllib.parse.urlparse(url)
        host, port = parsed.hostname or "", parsed.port
        if not (host and port):
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.35)
            if sock.connect_ex((host, port)) == 0:
                return url
    return ""
