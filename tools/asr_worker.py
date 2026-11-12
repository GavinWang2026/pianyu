#!/usr/bin/env python3
"""文案提取 worker —— 独立进程，运行在 ``.venv-asr`` 环境里。

与主服务之间只通过「stdin 进 / stdout 出」的 JSON 行协议通信，
因此主服务无需引入 torch / mlx 这类重依赖。

双引擎
------
worker 支持两套识别后端，按运行环境自动选择：

* ``mlx``    Apple Silicon（M 系列芯片）专用，走 MLX 框架，速度最快；
* ``faster`` 跨平台（Intel Mac / Windows / Linux），基于 CTranslate2，CPU 上也能跑。

两者共用同一套「解码 → 分块 → 过滤 → 上报」流程，差异被收敛在 ``Engine`` 子类里。

音频输入
--------
刻意**不把媒体文件路径交给识别库**，而是先用 ffmpeg 归一化成 16kHz 单声道，
再用标准库 ``wave`` + numpy 读成数组喂给引擎。原因：faster-whisper 1.2 的解码器
依赖 PyAV 的 ``metadata_errors`` 参数，而 PyAV 19 已移除该参数 —— 直接把路径交给它
会直接崩溃。绕开它自己的解码器，既躲开这颗版本地雷，也省掉一次重复解码。

协议
----
stdin  一个 JSON 对象：
    {
      "source":   "/abs/path/to/media.m4a",   # 已由主服务抓取好的本地媒体文件
      "language": "zh",                        # zh | en | auto
      "engine":   "auto",                      # auto | mlx | faster
      "model":    "",                          # 留空则按引擎取默认模型
      "work_dir": "/tmp/vd-asr-xxxx",
      "chunk_seconds": 300,
      "initial_percent": 15.0                  # 转写阶段在总进度里的起点
    }

stdout 逐行 JSON 事件：
    {"type": "stage",    "stage": "decode", "message": "正在提取音轨…"}
    {"type": "info",     "duration": 128.4, "chunks": 2, "engine": "mlx"}
    {"type": "progress", "percent": 42.5, "message": "转写中 3/12"}
    {"type": "segment",  "segment": {"start": 0.0, "end": 3.2, "text": "…"}}
    {"type": "done",     "text": "全文", "duration": 128.4, "segment_count": 87}
    {"type": "error",    "message": "可读的错误信息"}
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
from typing import Optional

# 各引擎的默认模型。mlx 用 MLX 量化版，faster 用 CTranslate2 版。
DEFAULT_MLX_MODEL = "mlx-community/whisper-large-v3-turbo"
DEFAULT_FASTER_MODEL = "large-v3-turbo"     # faster-whisper 内部映射到对应 ct2 仓库

HF_MIRROR = "https://hf-mirror.com"

# 分块边界搜索半径（秒）：在目标切点附近找静音，避免把一句话切两半
CUT_SEARCH_RADIUS = 45.0
MIN_CHUNK_SECONDS = 45.0

# 中文转写时喂给模型的提示词。
# Whisper 在没有提示时对中文常常输出**繁体**；喂一句简体中文能把它稳稳掰回简体，
# 顺带改善标点与断句。实测对 base 档位效果立竿见影。
ZH_PROMPT = "以下是普通话的句子，请使用简体中文转写，并加上标点符号。"

# whisper 在静音/纯音乐段落上的典型幻觉（中文 + 英文），命中且置信度低则丢弃
_HALLUCINATION_PATTERNS = (
    "请不吝点赞", "訂閱", "订阅", "轉發", "打賞", "明鏡與點點",
    "字幕由", "字幕志愿者", "amara.org", "字幕組", "字幕组",
    "thanks for watching", "thank you for watching", "subscribe",
    "please subscribe", "like and subscribe", "www.", ".com",
    "关注我", "點贊訂閱",
)

_RE_SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")


# ---------------------------------------------------------------------------
# 事件输出
# ---------------------------------------------------------------------------

def emit(event: dict) -> None:
    """向主服务推送一条事件。每行一个 JSON，必须 flush。"""
    try:
        sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except (BrokenPipeError, OSError):
        # 主服务已断开（用户取消），直接退出
        raise SystemExit(0)


def log(message: str) -> None:
    emit({"type": "stage", "stage": "log", "message": message})


# ---------------------------------------------------------------------------
# ffmpeg 封装
# ---------------------------------------------------------------------------

# 绿色便携包里的 ffmpeg 位置（相对本文件：tools/ → 上一级 → runtime/ffmpeg/bin/）。
# 这里刻意**不复用** downloader.runtime_paths：worker 跑在隔离环境里，
# 为了找一行路径而把主服务的包引进来，会破坏「两边只靠 JSON 行协议通信」的隔离。
# 主服务会把权威路径通过 VD_FFMPEG 传进来，下面这段只是终端手工调试时的兜底。
_BUNDLED_FFMPEG = (
    Path(__file__).resolve().parent.parent / "runtime" / "ffmpeg" / "bin" / "ffmpeg"
)


def _ffmpeg() -> str:
    """定位 ffmpeg。

    查找次序（与 downloader/runtime_paths.py 保持一致）::

        1. VD_FFMPEG 环境变量    —— 主服务启动 worker 时注入的权威路径
        2. 包内 runtime/ffmpeg   —— 兜底，双开终端手工调试时走这条
        3. 系统 PATH             —— 开发机上的 brew / 系统安装
    """
    explicit = (os.environ.get("VD_FFMPEG") or "").strip()
    if explicit and Path(explicit).is_file() and os.access(explicit, os.X_OK):
        return explicit

    if _BUNDLED_FFMPEG.is_file() and os.access(_BUNDLED_FFMPEG, os.X_OK):
        return str(_BUNDLED_FFMPEG)

    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError(
            "未找到 ffmpeg。若这是便携包，说明 runtime/ffmpeg 缺失或被杀毒软件隔离，"
            "请重新解压完整压缩包。"
        )
    return exe


def normalize_audio(src: Path, dst: Path) -> None:
    """把任意音视频归一化为 16kHz 单声道 PCM wav —— whisper 的标准输入。"""
    cmd = [
        _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        str(dst),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not dst.exists():
        detail = (proc.stderr or "").strip().splitlines()
        raise RuntimeError(f"音轨提取失败：{detail[-1] if detail else '未知原因'}")


def read_pcm16(path: Path):
    """把 16kHz 单声道 wav 读成 int16 数组。

    返回 ``(samples, sample_rate)``。只依赖标准库 ``wave`` + numpy，
    刻意不引入任何解码库 —— 见模块头部关于 PyAV 的说明。
    """
    import numpy as np

    with wave.open(str(path), "rb") as handle:
        params = handle.getparams()
        if params.nchannels != 1 or params.framerate != 16000:
            raise RuntimeError("内部错误：归一化音频格式不符合预期")
        raw = handle.readframes(params.nframes)
    return np.frombuffer(raw, dtype=np.int16), params.framerate


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        frames = handle.getnframes()
        rate = handle.getframerate() or 1
    return frames / float(rate)


def detect_silences(path: Path) -> list[float]:
    """用 ffmpeg silencedetect 找出静音起点，作为候选分块边界。"""
    cmd = [
        _ffmpeg(), "-hide_banner", "-nostats",
        "-i", str(path),
        "-af", "silencedetect=n=-35dB:d=0.35",
        "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    points: list[float] = []
    for line in (proc.stderr or "").splitlines():
        match = _RE_SILENCE_START.search(line)
        if match:
            try:
                value = float(match.group(1))
            except ValueError:
                continue
            if value > 0:
                points.append(value)
    return points


def plan_chunks(duration: float, silences: list[float], target: float) -> list[tuple[float, float]]:
    """把音频切成分块区间，切点尽量落在静音处，避免截断句子。"""
    if duration <= target * 1.2:
        return [(0.0, duration)]

    cuts = [0.0]
    while True:
        want = cuts[-1] + target
        if want >= duration - MIN_CHUNK_SECONDS:
            break
        window = [
            s for s in silences
            if want - CUT_SEARCH_RADIUS <= s <= want + CUT_SEARCH_RADIUS
        ]
        cut = min(window, key=lambda s: abs(s - want)) if window else want
        if cut <= cuts[-1] + MIN_CHUNK_SECONDS:
            cut = want  # 退化：附近没有可用的静音点
        cuts.append(cut)

    cuts.append(duration)
    return [(cuts[i], cuts[i + 1]) for i in range(len(cuts) - 1)]


# ---------------------------------------------------------------------------
# 识别引擎
# ---------------------------------------------------------------------------

class EngineError(RuntimeError):
    """引擎不可用或转写失败。"""


class BaseEngine:
    """识别引擎的公共接口。子类只需实现 ``load`` 与 ``_run``。"""

    name = "base"
    label = "未知引擎"

    def __init__(self, model: str, language: str) -> None:
        self.model = model
        self.language = language
        self._handle = None

    # -- 子类实现 ---------------------------------------------------------

    def _import(self):  # pragma: no cover - 由子类实现
        raise NotImplementedError

    def _load(self):  # pragma: no cover - 由子类实现
        raise NotImplementedError

    def _run(self, audio, prompt: Optional[str]) -> list[dict]:  # pragma: no cover
        raise NotImplementedError

    def release(self) -> None:
        """释放模型占用（长任务结束前调用，内存能早点还给系统）。"""
        self._handle = None

    # -- 公共流程 ---------------------------------------------------------

    def prepare(self) -> None:
        try:
            self._import()
        except ImportError as exc:
            raise EngineError(
                f"{self.label}不可用（{exc}）。请重新运行安装脚本以补齐依赖。"
            ) from exc
        self._handle = self._load()

    def transcribe(self, audio, prompt: Optional[str]) -> list[dict]:
        raw = self._run(audio, prompt)
        normalised: list[dict] = []
        for item in raw:
            text = (item.get("text") or "").strip()
            if _is_hallucination(text, item):
                continue
            normalised.append({
                "start": float(item.get("start") or 0.0),
                "end": float(item.get("end") or 0.0),
                "text": text,
            })
        return normalised


class MlxEngine(BaseEngine):
    """Apple Silicon 专用：MLX 框架，GPU 加速。"""

    name = "mlx"
    label = "MLX 引擎"

    def _import(self):
        import mlx_whisper  # noqa: F401

    def _load(self):
        import mlx_whisper
        return mlx_whisper

    def _run(self, audio, prompt):
        result = self._handle.transcribe(
            audio,                                   # 直接喂 numpy 数组，避免落盘再解码
            path_or_hf_repo=self.model,
            language=None if self.language == "auto" else self.language,
            initial_prompt=prompt,
            condition_on_previous_text=False,
            verbose=None,
        )
        return list(result.get("segments") or [])


class FasterEngine(BaseEngine):
    """跨平台：CTranslate2，CPU 上 int8 量化后可跑，Windows / Intel 走这条。"""

    name = "faster"
    label = "Faster-Whisper 引擎"

    def _import(self):
        import faster_whisper  # noqa: F401

    def _load(self):
        import ctranslate2
        from faster_whisper import WhisperModel

        device, compute_type = "cpu", "int8"
        try:
            if ctranslate2.get_cuda_device_count() > 0:
                device, compute_type = "cuda", "float16"
        except Exception:  # noqa: BLE001 - 探测失败就老实走 CPU
            pass

        return WhisperModel(self.model, device=device, compute_type=compute_type)

    def _run(self, audio, prompt):
        segments, _info = self._handle.transcribe(
            audio,
            language=None if self.language == "auto" else self.language,
            initial_prompt=prompt,
            beam_size=5,
            vad_filter=True,                    # 丢掉静音段，减少幻觉
            vad_parameters={"min_silence_duration_ms": 500},
            condition_on_previous_text=False,
        )
        return [
            {
                "start": seg.start,
                "end": seg.end,
                "text": seg.text,
                "no_speech_prob": getattr(seg, "no_speech_prob", 0.0),
                "avg_logprob": getattr(seg, "avg_logprob", 0.0),
            }
            for seg in segments
        ]


ENGINES: dict[str, type[BaseEngine]] = {"mlx": MlxEngine, "faster": FasterEngine}

def _is_apple_silicon() -> bool:
    return sys.platform == "darwin" and platform.machine() in ("arm64", "aarch64")


def _auto_order() -> tuple[str, ...]:
    """auto 模式下的尝试顺序。

    Apple 芯片上 MLX 明显更快，排第一；其它平台（Intel Mac / Windows / Linux）
    装不上 mlx，让它先试一次只是白白等一次导入失败，因此直接以 faster 起手。
    """
    return ("mlx", "faster") if _is_apple_silicon() else ("faster", "mlx")


def _cached_models(prefix: str) -> list[str]:
    """列出 HuggingFace 缓存里已就位的模型仓库名（形如 ``Systran/faster-whisper-x``）。"""
    cache_root = Path(os.environ.get("HF_HOME") or (Path.home() / ".cache" / "huggingface"))
    hub = cache_root / "hub"
    if not hub.is_dir():
        return []
    found: list[str] = []
    for entry in hub.iterdir():
        name = entry.name
        if not name.startswith("models--"):
            continue
        repo = name[len("models--"):].replace("--", "/")
        if repo.startswith(prefix) and (entry / "snapshots").is_dir():
            found.append(repo)
    return found


def resolve_faster_model(requested: str) -> str:
    """挑一个 faster-whisper 可用的模型名。

    优先用调用方指定的；如果本地缓存里已经有更合适的档位，就用缓存的 ——
    用户可能处在无网络环境，能跑起来比跑得完美更重要。
    """
    if requested:
        return requested
    cached = _cached_models("Systran/faster-whisper") + _cached_models("mobiuslabsgmbh/faster-whisper")
    if cached:
        # 档位越大越准，按体积排序取最大的
        def rank(repo: str) -> int:
            for size, weight in (("large-v3-turbo", 90), ("large-v3", 80), ("large", 70),
                                 ("medium", 50), ("small", 30), ("base", 20), ("tiny", 10)):
                if size in repo:
                    return weight
            return 0
        return max(cached, key=rank)
    return DEFAULT_FASTER_MODEL


def select_engine(preferred: str, language: str) -> tuple[BaseEngine, list[str]]:
    """按偏好选出可用引擎，返回 ``(引擎实例, 尝试过程中的提示)``。"""
    order = list(_auto_order()) if preferred in ("", "auto", None) else [str(preferred)]
    notes: list[str] = []
    last_error: Optional[Exception] = None

    for name in order:
        factory = ENGINES.get(name)
        if factory is None:
            continue
        model = ""
        if name == "mlx":
            model = os.environ.get("VD_ASR_MLX_MODEL") or DEFAULT_MLX_MODEL
        else:
            model = resolve_faster_model(os.environ.get("VD_ASR_FASTER_MODEL") or "")
        engine = factory(model, language)
        try:
            engine.prepare()
            if name != order[0]:
                notes.append(f"{ENGINES[order[0]].label}不可用，已切换到{engine.label}")
            return engine, notes
        except Exception as exc:  # noqa: BLE001 - 换下一个引擎继续试
            last_error = exc
            notes.append(f"{engine.label}加载失败：{str(exc)[:120]}")

    available = "、".join(factory.label for factory in ENGINES.values())
    raise EngineError(
        f"没有可用的识别引擎（已尝试：{available}）。{str(last_error)[:160] if last_error else ''}"
    )


# ---------------------------------------------------------------------------
# 幻觉过滤
# ---------------------------------------------------------------------------

def _is_hallucination(text: str, segment: dict) -> bool:
    stripped = text.strip()
    if not stripped:
        return True

    # 置信度低的静音段：whisper 会凭空造句，直接丢弃
    if segment.get("no_speech_prob", 0.0) > 0.65 and segment.get("avg_logprob", 0.0) < -0.75:
        return True

    lowered = stripped.lower()
    for pattern in _HALLUCINATION_PATTERNS:
        if pattern in lowered:
            # 仅在短句且置信度不高时才判为幻觉，避免误杀正常内容
            if len(stripped) <= 30 and segment.get("avg_logprob", 0.0) < -0.35:
                return True
    return False


# ---------------------------------------------------------------------------
# 繁简兜底
# ---------------------------------------------------------------------------

def _simplifier():
    """返回一个繁转简函数；zhconv 缺失时退化为原样返回（不影响主流程）。"""
    try:
        import zhconv
    except ImportError:
        return lambda text: text

    cache: dict[str, str] = {}

    def convert(text: str) -> str:
        hit = cache.get(text)
        if hit is None:
            hit = zhconv.convert(text, "zh-cn")
            if len(cache) < 5000:
                cache[text] = hit
        return hit

    return convert


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def configure_hf_cache(model: str) -> None:
    """模型已在本地缓存时强制离线，省掉每次启动的网络校验。"""
    os.environ.setdefault("HF_ENDPOINT", HF_MIRROR)
    slug = "models--" + model.replace("/", "--")
    cache_root = Path(os.environ.get("HF_HOME") or (Path.home() / ".cache" / "huggingface"))
    if (cache_root / "hub" / slug).is_dir():
        os.environ["HF_HUB_OFFLINE"] = "1"


def transcribe(cfg: dict) -> None:
    source = Path(cfg["source"]).expanduser()
    if not source.exists():
        raise RuntimeError("待转写的媒体文件不存在")

    work_dir = Path(cfg.get("work_dir") or tempfile.mkdtemp(prefix="vd-asr-"))
    work_dir.mkdir(parents=True, exist_ok=True)

    language = str(cfg.get("language") or "zh").strip()
    engine_pref = str(cfg.get("engine") or "auto").strip().lower()
    chunk_seconds = float(cfg.get("chunk_seconds") or 300)
    base_percent = float(cfg.get("initial_percent") or 15.0)
    explicit_model = str(cfg.get("model") or "").strip()

    # ---- 1. 归一化音频 ----
    emit({"type": "stage", "stage": "decode", "message": "正在提取音轨…"})
    wav_path = work_dir / "audio16k.wav"
    normalize_audio(source, wav_path)

    duration = wav_duration(wav_path)
    if duration <= 0.2:
        raise RuntimeError("音频为空或时长过短，无法识别")

    # ---- 2. 规划分块 ----
    # 静音探测必须在归一化后的 wav 上做：它已是 16k 单声道 PCM，
    # 扫一遍代价极低；若拿原始视频去扫，等于把整段视频再解一遍。
    emit({"type": "stage", "stage": "segment", "message": "正在分析语音段落…"})
    silences = detect_silences(wav_path)
    chunks = plan_chunks(duration, silences, chunk_seconds)

    samples, sample_rate = read_pcm16(wav_path)
    wav_path.unlink(missing_ok=True)          # 已进内存，临时文件即刻回收

    # ---- 3. 选择引擎 ----
    if explicit_model:
        os.environ["VD_ASR_MLX_MODEL"] = explicit_model
        os.environ["VD_ASR_FASTER_MODEL"] = explicit_model

    engine, notes = select_engine(engine_pref, language)
    for note in notes:
        log(note)
    emit({
        "type": "info",
        "duration": duration,
        "chunks": len(chunks),
        "engine": engine.name,
        "engine_label": engine.label,
        "model": engine.model,
    })

    if language == "auto":
        emit({"type": "stage", "stage": "transcribe", "message": "正在识别语音（自动判定语言）…"})
    else:
        emit({"type": "stage", "stage": "transcribe", "message": "正在识别语音…"})

    # 中文才需要简体偏置；其它语言喂中文提示词反而会干扰
    prompt = ZH_PROMPT if language == "zh" else None
    to_simplified = _simplifier() if language in ("zh", "auto") else None

    segments: list[dict] = []
    span = max(0.0, 100.0 - base_percent)

    try:
        for index, (start, end) in enumerate(chunks):
            lo = max(0, int(start * sample_rate))
            hi = min(len(samples), int(end * sample_rate))
            # 只把当前分块转成 float32：整段一起转会白白多吃一倍内存
            piece = samples[lo:hi].astype("float32") / 32768.0
            if piece.size == 0:
                continue

            for item in engine.transcribe(piece, prompt):
                text = item["text"]
                if to_simplified is not None:
                    text = to_simplified(text)
                record = {
                    "start": round(item["start"] + start, 2),
                    "end": round(item["end"] + start, 2),
                    "text": text,
                }
                segments.append(record)
                emit({"type": "segment", "segment": record})

            done_ratio = (index + 1) / len(chunks)
            emit({
                "type": "progress",
                "percent": round(base_percent + span * done_ratio, 2),
                "message": f"转写中 {index + 1}/{len(chunks)}",
            })
    finally:
        engine.release()

    text = "".join(item["text"] for item in segments).strip()
    emit({
        "type": "done",
        "text": text,
        "duration": round(duration, 2),
        "segment_count": len(segments),
        "engine": engine.name,
    })


def main() -> int:
    try:
        raw = sys.stdin.read()
        cfg = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        emit({"type": "error", "message": "worker 参数解析失败"})
        return 2

    try:
        transcribe(cfg)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一收敛为 error 事件
        message = str(exc) or exc.__class__.__name__
        emit({"type": "error", "message": message[:400]})
        return 1
    finally:
        work_dir = cfg.get("work_dir")
        if work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
