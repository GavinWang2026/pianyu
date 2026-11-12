"""视频下载引擎包。

对外暴露：
    config          全局配置单例
    probe()         解析链接元信息 + 画质档位
    download()      执行下载（带进度回调）
    manager         下载任务管理器单例
    asr             文案提取（平台字幕 + 语音转文字）
    polish          文案后处理（本地规则 + 大模型）
"""

from . import asr, polish, subtitles
from .config import DEFAULT_DOWNLOAD_DIR, ConfigStore, config
from .engine import DownloadCancelled, download, friendly_error, probe, resolve_proxy
from .tasks import Task, TaskManager, manager

__all__ = [
    "config",
    "ConfigStore",
    "DEFAULT_DOWNLOAD_DIR",
    "probe",
    "download",
    "friendly_error",
    "resolve_proxy",
    "DownloadCancelled",
    "manager",
    "TaskManager",
    "Task",
    "asr",
    "polish",
    "subtitles",
]
