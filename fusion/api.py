# Copyright (c) 2026 Hyht-Team. All rights reserved.
# See LICENSE (proprietary) and NOTICE.md (third-party components).
"""旗舰版 HTTP 扩展路由。

以最少侵入的方式挂到原版 server.py 上：GET / POST 分派开头各插一行调用，
原版 700 多行路由一行不改。新增接口：

    GET  /api/fusion/health          引擎 / 插件 / ASR / 版本自检
    GET  /api/pipeline/tasks         任务列表
    GET  /api/pipeline/status?id=    单任务进度（前端轮询，一条进度条）
    GET  /api/pipeline/result?id=&fmt=md|srt|txt|json   取回结构化稿
    POST /api/pipeline/start         一键启动整条产线
    POST /api/pipeline/cancel        取消
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from . import boot_check, engine_opts, pipeline
from . import PRODUCT_VERSION

_VALID_FORMATS = ("md", "srt", "txt", "json")


def handle_get(handler: Any, route: str, query: dict[str, list[str]]) -> bool:
    """返回 True 表示已处理，server.py 应立即 return。"""
    if route == "/api/fusion/health":
        health = boot_check.health()
        health["auth"] = engine_opts.cookie_status()
        handler._ok(
            product="AVD Flagship",
            version=PRODUCT_VERSION,
            health=health,
        )
        return True

    if route == "/api/pipeline/tasks":
        handler._ok(tasks=pipeline.manager().list_tasks())
        return True

    if route == "/api/pipeline/status":
        task_id = (query.get("id") or [""])[0]
        task = pipeline.manager().get(task_id)
        if task is None:
            handler._fail("任务不存在", HTTPStatus.NOT_FOUND)
        else:
            handler._ok(task=task)
        return True

    if route == "/api/pipeline/result":
        task_id = (query.get("id") or [""])[0]
        fmt = (query.get("fmt") or ["md"])[0]
        if fmt not in _VALID_FORMATS:
            handler._fail("不支持的导出格式", HTTPStatus.BAD_REQUEST)
            return True
        content = pipeline.manager().read_artifact(task_id, fmt)
        if content is None:
            handler._fail("文件不存在或任务尚未完成", HTTPStatus.NOT_FOUND)
        else:
            handler._ok(fmt=fmt, content=content, size=len(content))
        return True

    return False


def handle_post(handler: Any, route: str, payload: dict[str, Any]) -> bool:
    if route == "/api/platform/diagnose":
        url = str(payload.get("url") or "").strip()
        if not url:
            handler._fail("缺少 url", HTTPStatus.BAD_REQUEST)
        else:
            handler._ok(**pipeline.diagnose_url(url))
        return True

    if route == "/api/pipeline/start":
        try:
            task = pipeline.manager().start(
                str(payload.get("url") or ""),
                language=str(payload.get("language") or "zh"),
                want_download=bool(payload.get("want_download")),
                want_polish=payload.get("want_polish", True) is not False,
            )
        except ValueError as exc:
            handler._fail(str(exc), HTTPStatus.FORBIDDEN)
        except Exception as exc:
            handler._fail(f"无法启动：{exc}", HTTPStatus.INTERNAL_SERVER_ERROR)
        else:
            handler._ok(task=task)
        return True

    if route == "/api/pipeline/cancel":
        task_id = str(payload.get("id") or "")
        handler._ok(cancelled=pipeline.manager().cancel(task_id))
        return True

    return False
