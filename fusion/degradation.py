# Copyright (c) 2026 Hyht-Team
# SPDX-License-Identifier: MIT
# See LICENSE (MIT) and NOTICE.md (third-party components).
"""结构化降级模型 —— 「这份产出缺了什么、还能不能救、下一步做什么」。

历史包袱：``task.degraded`` 曾是 ``list[str]``，前端只能把整句原文当徽标显示
（``degraded_items`` 里的 ``action`` 恒为空串，形同虚设）。这让三类信息在
链路上丢失：

1. **严重度**：把「已剔除 3 段口播」和「语音识别环境未就绪」混在同一个列表里，
   用户无法判断哪条必须处理；
2. **可重试性**：重试按钮不知道哪些失败值得再跑一次；
3. **动作**：用户看到「语音识别环境未就绪」但不知道要跑哪条命令。

本模块把降级项统一成 ``Degradation(level, code, cause, retriable, action)``：

* ``level``：``note`` / ``warn`` / ``major`` / ``block``，四档语义见 ``LEVELS``；
* ``code``：稳定机器码（``asr.env_missing``），前端可做 i18n / 图标映射，
  不随文案改动而变；
* ``cause``：本任务的具体原因（含动态值），给人读；
* ``retriable``：同参数重跑是否有意义（决定「重试」按钮是否亮）；
* ``action``：**可执行**的下一步建议（命令 / 配置项 / 换源），不是废话。

向后兼容：``PipelineTask.degraded`` 仍是字符串列表（旧 UI / 旧导出 / 既有
测试全部照常工作），结构化视图另放 ``degradations``，由 ``to_dict()`` 同时
输出 ``degraded`` 与 ``degraded_items``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

# 严重度由低到高。排序/取最大值时按本元组顺序。
LEVELS: tuple[str, ...] = ("note", "warn", "major", "block")

LEVEL_LABEL = {
    "note": "提示",
    "warn": "告警",
    "major": "降级产出",
    "block": "能力缺失",
}

LEVEL_HINT = {
    "note": "无需处理，知情即可",
    "warn": "建议处理，不处理也能用",
    "major": "产出已被削减，建议按建议动作处理",
    "block": "关键能力不可用，产出可能只是骨架",
}


@dataclass(frozen=True)
class Spec:
    """一类降级项的默认语义；调用处只需补具体 cause。"""

    level: str
    retriable: bool
    action: str

    def __post_init__(self) -> None:
        if self.level not in LEVELS:
            raise ValueError(f"unknown degradation level: {self.level}")


@dataclass(frozen=True)
class Degradation:
    code: str
    level: str
    cause: str
    retriable: bool
    action: str

    def __post_init__(self) -> None:
        if self.level not in LEVELS:
            raise ValueError(f"unknown degradation level: {self.level}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "level": self.level,
            "level_label": LEVEL_LABEL.get(self.level, self.level),
            "cause": self.cause,
            "retriable": bool(self.retriable),
            "action": self.action,
        }

    def as_line(self) -> str:
        """旧版 ``degraded: list[str]`` 的一行文本。"""
        return f"[{LEVEL_LABEL.get(self.level, self.level)}] {self.cause}"


# 目录表：code -> 默认语义。新增降级点时先在这里登记，避免文案/动作散落各处。
_SPECS: dict[str, Spec] = {
    # —— 阶段机 ——
    "stage.transition": Spec("note", True, "查看运行日志；反复出现请提交诊断包"),
    # —— 直播 ——
    "live.continuous": Spec(
        "warn", False, "点「取消」结束录制；需要完整内容请改用回放页链接"),
    "live.limited": Spec(
        "warn", False, "调大 asr_live_record_seconds 或改用回放页"),
    "live.replay": Spec("note", False, "无需处理：按普通视频全量处理"),
    # —— 下载 ——
    "download.postprocess": Spec(
        "note", True, "无需重下：媒体已落盘并直接复用"),
    "download.skip": Spec(
        "warn", True, "检查网络 / Cookie 后重试，或换源"),
    "cookie.browser_unreadable": Spec(
        "warn", False,
        "本次已跳过浏览器 Cookie 继续解析；需要登录态时给小助手/终端开完全磁盘访问，"
        "或改用 cookies.txt / Chrome 等来源"),
    # —— 字幕 ——
    "subtitle.fetch": Spec(
        "warn", True, "可重试；或把模式切成「仅语音识别」"),
    "subtitle.absent": Spec(
        "note", False, "无需处理：已自动改用语音识别"),
    # —— 媒体 / 转写 ——
    "media.download": Spec(
        "block", True, "重试或换源；持续失败请提交诊断包"),
    "media.no_audio": Spec(
        "major", False, "换含音轨的源；当前仅输出元数据骨架"),
    "asr.failed": Spec(
        "major", True, "重试；反复失败检查权重目录与磁盘空间"),
    "asr.no_speech": Spec(
        "note", False, "无需重装模型：该素材本身无人声"),
    "asr.env_missing": Spec(
        "block", False, "运行 python3 doctor.py --repair，或检查 .venv-asr 运行时"),
    "asr.timeout": Spec(
        "major", True, "换源或重试；超长素材建议先切片"),
    # —— 内容 ——
    "content.low": Spec(
        "major", False, "换信息量更足的素材；当前只输出元数据与原文"),
    # —— 说话人 ——
    "speaker.neural": Spec("note", False, "无需处理"),
    "speaker.heuristic": Spec(
        "note", False, "需要精确说话人时安装 pyannote 后重跑"),
    "speaker.inconclusive": Spec(
        "note", False, "无需处理（避免独白被误判成多说话人）"),
    # —— 知识稿 ——
    "chapter.auto": Spec("note", False, "可在校对页手工修改章节标题"),
    "chapter.quality": Spec("warn", False, "校对页调整章节边界/标题；不影响已有正文与导出"),
    "term.normalized": Spec("note", False, "如需保留原写法，可在校对页改回"),
    "term.normalize": Spec("note", False, "无需处理"),
    "insights.skip": Spec("warn", True, "重试；持续失败请查看日志"),
    "insights.error": Spec("warn", True, "重试"),
    "sponsor.removed": Spec(
        "note", False, "如需保留口播，配置里关闭 sponsor 过滤"),
    # —— 落地 ——
    "persist.failed": Spec(
        "major", True, "检查磁盘空间与 outputs 目录权限；本次产物仍在，但历史列表可能缺这条"),
    "state.missing": Spec(
        "warn", False, "任务记录仍在，但产物目录已被清理或移动；请从诊断包确认是否需要重跑"),
    "library.index": Spec(
        "warn", True, "重试；或稍后在素材库页重建索引"),
    "obsidian.write": Spec(
        "warn", False, "检查 obsidian_dir 位于用户主目录下且可写"),
}

# 未登记的 code 用它兜底，避免因为漏登记直接抛错
_FALLBACK = Spec("warn", True, "查看日志；反复出现请提交诊断包")


def spec(code: str) -> Spec:
    return _SPECS.get(code, _FALLBACK)


def known_codes() -> tuple[str, ...]:
    return tuple(sorted(_SPECS))


def make(
    code: str,
    cause: str,
    *,
    level: str | None = None,
    retriable: bool | None = None,
    action: str | None = None,
) -> Degradation:
    """按目录表构造降级项；显式参数可覆盖默认语义。"""
    base = spec(code)
    return Degradation(
        code=code,
        level=level or base.level,
        cause=str(cause or "").strip() or code,
        retriable=base.retriable if retriable is None else bool(retriable),
        action=action or base.action,
    )


def to_dicts(items: Iterable[Degradation | dict[str, Any]]) -> list[dict[str, Any]]:
    """统一成可 JSON 序列化的结构化列表。"""
    out: list[dict[str, Any]] = []
    for item in items or ():
        if isinstance(item, Degradation):
            out.append(item.as_dict())
        elif isinstance(item, dict):
            out.append(dict(item))
    return out


def rank(level: str) -> int:
    try:
        return LEVELS.index(str(level))
    except ValueError:
        return 0


def worst(items: Iterable[dict[str, Any]]) -> str:
    """最高严重度；空列表返回空串。"""
    best = ""
    for item in items or ():
        level = str(item.get("level") or "")
        if not best or rank(level) > rank(best):
            best = level
    return best


def summary(items: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """给 UI 的汇总：最高级别 + 各级计数 + 可重试条数。"""
    rows = list(items or ())
    counts = {level: 0 for level in LEVELS}
    retriable = 0
    for row in rows:
        level = str(row.get("level") or "note")
        counts[level] = counts.get(level, 0) + 1
        if row.get("retriable"):
            retriable += 1
    top = worst(rows)
    return {
        "total": len(rows),
        "worst": top,
        "worst_label": LEVEL_LABEL.get(top, "") if top else "",
        "counts": counts,
        "retriable": retriable,
        "blocked": counts.get("block", 0) > 0,
    }
