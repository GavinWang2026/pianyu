# Copyright (c) 2026 Hyht-Team. All rights reserved.
# See LICENSE (proprietary) and NOTICE.md (third-party components).
"""旗舰版 HTTP 扩展路由。

以最少侵入的方式挂到原版 server.py 上：GET / POST 分派开头各插一行调用，
原版 700 多行路由一行不改。新增接口：

    GET  /api/fusion/health          引擎 / 插件 / ASR / 版本自检
    POST /api/fusion/reconcile       以产物目录为真相，对账内存/索引/SQLite
    GET  /api/pipeline/tasks         任务列表
    GET  /api/pipeline/status?id=    单任务进度（兼容轮询）
    GET  /api/pipeline/progress?id=  SSE 进度（SSE-01，旗舰版首选）
    GET  /api/pipeline/result?id=&fmt=md|srt|txt|json   取回结构化稿
    GET  /api/platform/diagnose?id=  异步诊断结果（MISC-03）
    POST /api/pipeline/start         一键启动整条产线（可展开播放列表）
    POST /api/pipeline/batch         多 URL 排队
    POST /api/pipeline/export-obsidian  把 Markdown 写入 Obsidian 库
    POST /api/pipeline/export-push   Notion / 飞书推送
    POST /api/pipeline/export-clip   按时间轴切片导出
    POST /api/pipeline/export-chapters  按章节批量切片
    POST /api/pipeline/cancel        取消
    GET  /api/library/search?q=      素材库 FTS5 检索
    GET  /api/library/collections    合集列表
    GET  /api/library/materials      素材列表（可按合集）
    POST /api/library/ask            跨任务问答（基于已转写库）
    POST /api/library/batch          合集/标签批量与自动归类
    POST /api/library/export-booklet 合集成册（MD / DOCX）
    POST /api/diagnose/pack          一键诊断包
    GET  /api/diagnose/tickets       本地工单回执列表
    POST /api/diagnose/submit        工单上传（需 AVD_SUPPORT_URL；未配置则 local_only）
    GET  /api/pipeline/recoverable   可续跑检查点 / 崩溃现场
    GET  /api/updates/check          自动更新探测（需 AVD_UPDATE_URL）
    POST /api/platform/diagnose      启动异步诊断（立即返回 job id）
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from http import HTTPStatus
from typing import Any

from . import PRODUCT_VERSION, boot_check, engine_opts, pipeline

_VALID_FORMATS = ("md", "srt", "txt", "json", "docx", "pdf")

# MISC-03：诊断作业表（内存，短生命周期）
_DIAG_JOBS: dict[str, dict[str, Any]] = {}
_DIAG_LOCK = threading.RLock()
_DIAG_TTL = 600.0


def _purge_diag() -> None:
    now = time.time()
    with _DIAG_LOCK:
        dead = [k for k, v in _DIAG_JOBS.items()
                if now - float(v.get("created_at") or 0) > _DIAG_TTL]
        for k in dead:
            _DIAG_JOBS.pop(k, None)


def _run_diagnose(job_id: str, url: str) -> None:
    try:
        result = pipeline.diagnose_url(url)
        with _DIAG_LOCK:
            job = _DIAG_JOBS.get(job_id)
            if job is not None:
                job.update({"stage": "done", "result": result, "finished_at": time.time()})
    except Exception as exc:  # noqa: BLE001
        with _DIAG_LOCK:
            job = _DIAG_JOBS.get(job_id)
            if job is not None:
                job.update({
                    "stage": "error", "error": str(exc),
                    "finished_at": time.time(),
                })


def handle_get(handler: Any, route: str, query: dict[str, list[str]]) -> bool:
    """返回 True 表示已处理，server.py 应立即 return。"""
    if route == "/api/fusion/health":
        health = boot_check.health()
        health["auth"] = engine_opts.cookie_status()
        try:
            from . import persist
            health["persist"] = persist.stats()
        except Exception:
            health["persist"] = {"error": "unavailable"}
        try:
            from downloader import asr_pool
            pool = asr_pool.get_pool()
            health["asr_pool"] = {
                "enabled": asr_pool.pool_enabled(),
                "alive": pool.alive,
                "concurrency": 1,
                "queue": pool.queue_snapshot(),
                "note": "转写并发=1（常驻池串行）；流水线可并行下载，转写阶段会排队",
            }
        except Exception:
            health["asr_pool"] = {"enabled": False, "concurrency": 1}
        try:
            from pathlib import Path
            cap = Path(__file__).resolve().parent.parent / "CAPABILITIES.md"
            health["capabilities"] = cap.is_file()
        except Exception:
            health["capabilities"] = False
        handler._ok(
            product="AVD Flagship",
            version=PRODUCT_VERSION,
            health=health,
        )
        return True

    if route == "/api/library/search":
        from . import library
        q = (query.get("q") or query.get("query") or [""])[0]
        collection = (query.get("collection") or [""])[0]
        try:
            limit = int((query.get("limit") or ["20"])[0])
        except (TypeError, ValueError):
            limit = 20
        handler._ok(results=library.search(q, limit=limit, collection=collection))
        return True

    if route == "/api/library/collections":
        from . import library
        handler._ok(collections=library.list_collections())
        return True

    if route == "/api/library/materials":
        from . import library
        collection = (query.get("collection") or [""])[0]
        try:
            limit = int((query.get("limit") or ["100"])[0])
        except (TypeError, ValueError):
            limit = 100
        handler._ok(materials=library.list_materials(collection=collection, limit=limit))
        return True

    if route == "/api/diagnose/tickets":
        from . import diagnose_pack
        try:
            limit = int((query.get("limit") or ["50"])[0])
        except (TypeError, ValueError):
            limit = 50
        handler._ok(tickets=diagnose_pack.list_tickets(limit=limit))
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

    if route == "/api/pipeline/progress":
        # SSE-01：旗舰版改用 SSE，带 id: 与 Last-Event-ID
        task_id = (query.get("id") or [""])[0]
        _stream_pipeline(handler, task_id)
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
            handler._ok(
                fmt=fmt, content=content, size=len(content),
                encoding="base64" if fmt in ("docx", "pdf") else "utf-8",
            )
        return True

    if route == "/api/platform/diagnose":
        job_id = (query.get("id") or [""])[0]
        _purge_diag()
        with _DIAG_LOCK:
            job = _DIAG_JOBS.get(job_id)
        if not job:
            handler._fail("诊断任务不存在", HTTPStatus.NOT_FOUND)
        else:
            handler._ok(job=job)
        return True

    if route == "/api/updates/check":
        from . import updates
        handler._ok(**updates.check())
        return True

    if route == "/api/storage/stats":
        from . import persist
        handler._ok(**persist.stats())
        return True

    if route == "/api/pipeline/recoverable":
        from . import recovery
        try:
            limit = int((query.get("limit") or ["50"])[0])
        except (TypeError, ValueError):
            limit = 50
        handler._ok(items=recovery.scan_recoverable(limit=limit))
        return True

    return False


def handle_post(handler: Any, route: str, payload: dict[str, Any]) -> bool:
    """D30：薄分派器；具体逻辑按域拆到下面的小 handler。"""
    for group in (
        _post_platform_diagnose,
        _post_pipeline,
        _post_library,
        _post_diagnose_admin,
        _post_pipeline_tail,
        _post_storage,
    ):
        if group(handler, route, payload):
            return True
    return False


def _post_platform_diagnose(handler: Any, route: str,
                            payload: dict[str, Any]) -> bool:
    if route == "/api/platform/diagnose":
        url = str(payload.get("url") or "").strip()
        if not url:
            handler._fail("缺少 url", HTTPStatus.BAD_REQUEST)
            return True
        # MISC-03：默认仍同步（兼容 curl / cookie_doctor / e2e）；
        # 传 async=true 时立即返回 job id，用 GET ?id= 取结果，避免占满连接。
        want_async = payload.get("async") in (True, "true", "1", 1, "yes")
        if want_async:
            _purge_diag()
            job_id = uuid.uuid4().hex[:12]
            with _DIAG_LOCK:
                _DIAG_JOBS[job_id] = {
                    "id": job_id, "url": url, "stage": "running",
                    "created_at": time.time(), "result": None, "error": "",
                }
            threading.Thread(
                target=_run_diagnose, args=(job_id, url),
                name=f"avd-diagnose-{job_id}", daemon=True,
            ).start()
            handler._ok(job={"id": job_id, "stage": "running", "url": url}, async_mode=True)
            return True
        # 同步路径：在独立线程跑，主请求最多等 90s，超时回 504
        box: dict[str, Any] = {}

        def _sync() -> None:
            try:
                box["result"] = pipeline.diagnose_url(url)
            except Exception as exc:  # noqa: BLE001
                box["error"] = str(exc)

        t = threading.Thread(target=_sync, daemon=True, name="avd-diagnose-sync")
        t.start()
        t.join(timeout=90)
        if t.is_alive():
            handler._fail("诊断超时（>90s），请改用 async=true 后轮询", HTTPStatus.GATEWAY_TIMEOUT)
            return True
        if box.get("error"):
            handler._fail(f"诊断失败：{box['error']}", HTTPStatus.INTERNAL_SERVER_ERROR)
            return True
        handler._ok(**(box.get("result") or {}))
        return True
    return False


def _post_pipeline(handler: Any, route: str,
                   payload: dict[str, Any]) -> bool:
    if route == "/api/pipeline/start":
        # ARCH-01：默认 want_download=True，兑现「一次解析、全链复用」
        raw_dl = payload.get("want_download", True)
        want_download = False if raw_dl in (False, "false", "0", 0, "no") else True
        expand = payload.get("expand_playlist")
        expand_flag = None
        if expand in (True, "true", "1", 1, "yes"):
            expand_flag = True
        elif expand in (False, "false", "0", 0, "no"):
            expand_flag = False
        try:
            task = pipeline.manager().start(
                str(payload.get("url") or ""),
                language=str(payload.get("language") or "zh"),
                want_download=want_download,
                want_polish=payload.get("want_polish", True) is not False,
                expand_playlist=expand_flag,
            )
        except ValueError as exc:
            handler._fail(str(exc), HTTPStatus.FORBIDDEN)
        except Exception as exc:
            handler._fail(f"无法启动：{exc}", HTTPStatus.INTERNAL_SERVER_ERROR)
        else:
            handler._ok(task=task)
        return True

    if route == "/api/pipeline/batch":
        # 多 URL 排队：body.urls 为数组，或 body.text 按行拆分
        raw_urls = payload.get("urls")
        urls: list[str] = []
        if isinstance(raw_urls, list):
            urls = [str(u) for u in raw_urls]
        elif isinstance(raw_urls, str):
            urls = re.split(r"[\n\r]+", raw_urls)
        text = str(payload.get("text") or "")
        if text.strip():
            urls.extend(re.split(r"[\n\r]+", text))
        raw_dl = payload.get("want_download", True)
        want_download = False if raw_dl in (False, "false", "0", 0, "no") else True
        try:
            batch = pipeline.manager().start_batch(
                urls,
                language=str(payload.get("language") or "zh"),
                want_download=want_download,
                want_polish=payload.get("want_polish", True) is not False,
                delay_seconds=payload.get("delay_seconds"),
                run_at=payload.get("run_at"),
            )
        except ValueError as exc:
            handler._fail(str(exc), HTTPStatus.FORBIDDEN)
        except Exception as exc:
            handler._fail(f"无法启动批量：{exc}", HTTPStatus.INTERNAL_SERVER_ERROR)
        else:
            # 回显本地解析时间，便于确认定时不会偏 8 小时
            if payload.get("run_at"):
                try:
                    from . import scheduler as sched_mod
                    batch["schedule"] = sched_mod.resolve_run_at(payload.get("run_at"))
                except Exception as exc:  # noqa: BLE001
                    batch["schedule_error"] = str(exc)
            handler._ok(**batch)
        return True

    if route == "/api/pipeline/export-obsidian":
        task_id = str(payload.get("id") or "")
        vault = payload.get("path")
        vault_s = str(vault).strip() if vault else None
        try:
            result = pipeline.write_obsidian(task_id, vault=vault_s)
        except ValueError as exc:
            handler._fail(str(exc), HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            handler._fail(f"写入失败：{exc}", HTTPStatus.INTERNAL_SERVER_ERROR)
        else:
            handler._ok(**result)
        return True

    if route == "/api/pipeline/export-push":
        task_id = str(payload.get("id") or "")
        target = str(payload.get("target") or "").strip().lower()
        if target not in ("notion", "feishu", "lark"):
            handler._fail("target 仅支持 notion / feishu", HTTPStatus.BAD_REQUEST)
            return True
        try:
            result = pipeline.push_export(task_id, target)
        except ValueError as exc:
            handler._fail(str(exc), HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            handler._fail(f"推送失败：{exc}", HTTPStatus.INTERNAL_SERVER_ERROR)
        else:
            handler._ok(target=target, **(result if isinstance(result, dict) else {"result": result}))
        return True

    if route == "/api/pipeline/export-clip":
        task_id = str(payload.get("id") or "")
        try:
            start = float(payload.get("start") or 0)
            end = float(payload.get("end") or 0)
        except (TypeError, ValueError):
            handler._fail("start/end 必须是数字", HTTPStatus.BAD_REQUEST)
            return True
        reencode = payload.get("reencode") in (True, "true", "1", 1, "yes")
        try:
            result = pipeline.export_clip(
                task_id, start=start, end=end, reencode=reencode,
            )
        except ValueError as exc:
            handler._fail(str(exc), HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            handler._fail(f"切片失败：{exc}", HTTPStatus.INTERNAL_SERVER_ERROR)
        else:
            handler._ok(**result)
        return True

    if route == "/api/pipeline/export-chapters":
        task_id = str(payload.get("id") or "")
        reencode = payload.get("reencode") in (True, "true", "1", 1, "yes")
        try:
            result = pipeline.export_clips_by_chapters(task_id, reencode=reencode)
        except ValueError as exc:
            handler._fail(str(exc), HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            handler._fail(f"章节切片失败：{exc}", HTTPStatus.INTERNAL_SERVER_ERROR)
        else:
            handler._ok(**result)
        return True
    return False


def _post_library(handler: Any, route: str,
                  payload: dict[str, Any]) -> bool:
    if route == "/api/library/ask":
        from . import library
        q = str(payload.get("q") or payload.get("query") or "").strip()
        if not q:
            handler._fail("缺少 q", HTTPStatus.BAD_REQUEST)
            return True
        try:
            limit = int(payload.get("limit") or 5)
        except (TypeError, ValueError):
            limit = 5
        use_llm = payload.get("use_llm") not in (False, "false", "0", 0, "no")
        handler._ok(**library.answer(q, limit=limit, use_llm=use_llm))
        return True

    if route == "/api/library/batch":
        from . import library
        action = str(payload.get("action") or "").strip().lower()
        ids = payload.get("ids") or payload.get("task_ids") or []
        if not isinstance(ids, list):
            handler._fail("ids 须为数组", HTTPStatus.BAD_REQUEST)
            return True
        try:
            if action == "set_collection":
                n = library.batch_set_collection(ids, str(payload.get("collection") or ""))
                handler._ok(updated=n, action=action)
            elif action == "add_tags":
                tags = payload.get("tags") or []
                if not isinstance(tags, list):
                    handler._fail("tags 须为数组", HTTPStatus.BAD_REQUEST)
                    return True
                n = library.batch_add_tags(ids, [str(t) for t in tags])
                handler._ok(updated=n, action=action)
            elif action == "auto_cluster":
                handler._ok(**library.auto_cluster_by_chapters(
                    limit=int(payload.get("limit") or 200),
                ), action=action)
            else:
                handler._fail("action 仅支持 set_collection / add_tags / auto_cluster", HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            handler._fail(f"批量操作失败：{exc}", HTTPStatus.INTERNAL_SERVER_ERROR)
        return True

    if route == "/api/library/export-booklet":
        from . import library
        collection = str(payload.get("collection") or "").strip()
        if not collection:
            handler._fail("缺少 collection", HTTPStatus.BAD_REQUEST)
            return True
        fmt = str(payload.get("fmt") or "all").strip().lower() or "all"
        template = str(payload.get("template") or "delivery").strip().lower() or "delivery"
        try:
            result = library.export_booklet(collection, fmt=fmt, template=template)
        except ValueError as exc:
            handler._fail(str(exc), HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            handler._fail(f"合集成册失败：{exc}", HTTPStatus.INTERNAL_SERVER_ERROR)
        else:
            handler._ok(**result)
        return True
    return False


def _post_diagnose_admin(handler: Any, route: str,
                         payload: dict[str, Any]) -> bool:
    if route == "/api/diagnose/pack":
        from . import diagnose_pack
        task_id = str(payload.get("id") or payload.get("task_id") or "")
        full = payload.get("include_full_artifacts") in (True, "true", "1", 1, "yes")
        try:
            result = diagnose_pack.build_diagnose_pack(
                task_id=task_id, include_full_artifacts=full,
            )
        except Exception as exc:
            handler._fail(f"诊断包失败：{exc}", HTTPStatus.INTERNAL_SERVER_ERROR)
        else:
            handler._ok(**result)
        return True

    if route == "/api/diagnose/submit":
        from . import diagnose_pack
        ticket_id = str(payload.get("ticket_id") or payload.get("id") or "").strip()
        if not ticket_id:
            handler._fail("缺少 ticket_id", HTTPStatus.BAD_REQUEST)
            return True
        try:
            result = diagnose_pack.submit_ticket(ticket_id)
        except ValueError as exc:
            handler._fail(str(exc), HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            handler._fail(f"工单提交失败：{exc}", HTTPStatus.INTERNAL_SERVER_ERROR)
        else:
            handler._ok(**result)
        return True
    return False


def _post_pipeline_tail(handler: Any, route: str,
                        payload: dict[str, Any]) -> bool:
    if route == "/api/pipeline/proofread":
        task_id = str(payload.get("id") or "")
        segments = payload.get("segments")
        if not isinstance(segments, list):
            handler._fail("缺少 segments 数组", HTTPStatus.BAD_REQUEST)
            return True
        strict = payload.get("strict") in (True, "true", "1", 1, "yes")
        try:
            task = pipeline.manager().proofread(task_id, segments, strict=strict)
        except ValueError as exc:
            handler._fail(str(exc), HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            handler._fail(f"校对回写失败：{exc}", HTTPStatus.INTERNAL_SERVER_ERROR)
        else:
            handler._ok(task=task)
        return True

    if route == "/api/pipeline/cancel":
        task_id = str(payload.get("id") or "")
        handler._ok(cancelled=pipeline.manager().cancel(task_id))
        return True

    if route == "/api/pipeline/retry":
        task_id = str(payload.get("id") or "")
        if not task_id:
            handler._fail("缺少 id", HTTPStatus.BAD_REQUEST)
            return True
        try:
            task = pipeline.manager().retry(task_id)
        except ValueError as exc:
            handler._fail(str(exc), HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            handler._fail(f"重试失败：{exc}", HTTPStatus.INTERNAL_SERVER_ERROR)
        else:
            handler._ok(task=task)
        return True

    if route == "/api/fusion/reconcile":
        # D27：以产物目录为真相，恢复丢失的内存记录并标记孤儿记录。
        from . import pipeline as _pipe
        try:
            result = _pipe.manager().reconcile()
        except Exception as exc:  # noqa: BLE001
            handler._fail(f"对账失败：{exc}", HTTPStatus.INTERNAL_SERVER_ERROR)
            return True
        handler._ok(**result)
        return True

    return False


def _post_storage(handler: Any, route: str,
                  payload: dict[str, Any]) -> bool:
    if route == "/api/storage/purge":
        from . import persist
        try:
            result = persist.apply_retention()
        except Exception as exc:
            handler._fail(f"清理失败：{exc}", HTTPStatus.INTERNAL_SERVER_ERROR)
        else:
            handler._ok(**result)
        return True
    return False


def _stream_pipeline(handler: Any, task_id: str) -> None:
    """流水线 SSE：快照轮询推送，带事件 id（兼容无订阅总线的 PipelineManager）。"""
    if not task_id or pipeline.manager().get(task_id) is None:
        handler._fail("任务不存在", HTTPStatus.NOT_FOUND)
        return

    last_id = 0
    raw_last = (handler.headers.get("Last-Event-ID") or "").strip()
    if raw_last.isdigit():
        last_id = int(raw_last)

    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache, no-transform")
    handler.send_header("Connection", "keep-alive")
    handler.send_header("X-Accel-Buffering", "no")
    handler.send_header("Transfer-Encoding", "chunked")
    if hasattr(handler, "_security_headers"):
        handler._security_headers()
    handler.end_headers()

    terminal = {"done", "error", "cancelled"}
    last_stage = ""
    last_percent = -1.0
    try:
        while True:
            task = pipeline.manager().get(task_id)
            if task is None:
                break
            stage = str(task.get("stage") or "")
            percent = float(task.get("percent") or 0)
            if stage != last_stage or abs(percent - last_percent) >= 0.5 or stage in terminal:
                last_stage = stage
                last_percent = percent
                last_id += 1
                payload = json.dumps({"type": "task", "task": task}, ensure_ascii=False).encode("utf-8")
                chunk = f"id: {last_id}\n".encode("utf-8") + b"data: " + payload + b"\n\n"
                handler._write_chunk(chunk)
                if stage in terminal:
                    handler._write_chunk(b"event: end\ndata: {}\n\n")
                    break
            else:
                handler._write_chunk(b": ping\n\n")
            time.sleep(0.45)
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        try:
            handler.wfile.write(b"0\r\n\r\n")
            handler.wfile.flush()
        except OSError:
            pass
