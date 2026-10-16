# -*- coding: utf-8 -*-
"""后台任务管理（数据同步、回测等长任务），供前端轮询进度。"""
from __future__ import annotations

import threading
import time
import traceback
import uuid
from typing import Any, Callable

from core import log as _log

_log_job = _log.get("jobs")

_LOCK = threading.Lock()
_JOBS: dict[str, dict] = {}


def new_job(kind: str, title: str = "") -> str:
    jid = uuid.uuid4().hex[:12]
    with _LOCK:
        _JOBS[jid] = {
            "id": jid, "kind": kind, "title": title or kind,
            "status": "pending", "progress": 0.0, "message": "等待执行",
            "logs": [], "result": None, "error": None,
            "started_at": time.time(), "ended_at": None,
        }
    _log_job.info("任务创建 jid=%s kind=%s title=%s", jid, kind, title or kind)
    return jid


def update(jid: str, progress: float | None = None, message: str | None = None,
           log: str | None = None) -> None:
    with _LOCK:
        j = _JOBS.get(jid)
        if not j:
            return
        if progress is not None:
            j["progress"] = max(0.0, min(1.0, float(progress)))
        if message is not None:
            j["message"] = message
        if log:
            j["logs"].append(f"[{time.strftime('%H:%M:%S')}] {log}")
            if len(j["logs"]) > 400:
                j["logs"] = j["logs"][-300:]
            _log_job.debug("[%s] %s", jid[:6], log)


def finish(jid: str, result: Any = None, message: str = "完成") -> None:
    with _LOCK:
        j = _JOBS.get(jid)
        if not j:
            return
        j["status"] = "done"
        j["progress"] = 1.0
        j["message"] = message
        j["result"] = result
        j["ended_at"] = time.time()
        cost = j["ended_at"] - j["started_at"]
    _log_job.info("任务完成 jid=%s 耗时 %.1fs", jid, cost)


def fail(jid: str, err: str) -> None:
    with _LOCK:
        j = _JOBS.get(jid)
        if not j:
            return
        j["status"] = "error"
        j["message"] = err
        j["error"] = err
        j["ended_at"] = time.time()
        cost = j["ended_at"] - j["started_at"]
    _log_job.error("任务失败 jid=%s 耗时 %.1fs | %s", jid, cost, err)


def get(jid: str) -> dict | None:
    with _LOCK:
        j = _JOBS.get(jid)
        return dict(j) if j else None


def cancel(jid: str) -> None:
    with _LOCK:
        j = _JOBS.get(jid)
        if j and j["status"] in ("pending", "running"):
            j["cancel"] = True


def is_cancelled(jid: str) -> bool:
    with _LOCK:
        j = _JOBS.get(jid)
        return bool(j and j.get("cancel"))


def list_jobs(limit: int = 30) -> list[dict]:
    with _LOCK:
        items = sorted(_JOBS.values(), key=lambda x: -x["started_at"])[:limit]
        return [{k: v for k, v in j.items() if k != "logs"} for j in items]


def run_async(jid: str, fn: Callable[[], Any], done_message: str = "完成") -> None:
    """在后台线程执行 fn(jid)，自动维护状态。"""
    def _wrapper():
        with _LOCK:
            j = _JOBS.get(jid) or {}
            if jid in _JOBS:
                _JOBS[jid]["status"] = "running"
        _log_job.info("任务开始 jid=%s kind=%s", jid, j.get("kind") or "-")
        t0 = time.time()
        try:
            res = fn()
            finish(jid, res, done_message)
        except Exception as e:  # noqa: BLE001
            fail(jid, f"{type(e).__name__}: {e}")
            update(jid, log=traceback.format_exc()[-1500:])
            _log_job.error("任务异常 jid=%s %.1fs\n%s", jid, time.time() - t0,
                           traceback.format_exc()[-2000:])

    t = threading.Thread(target=_wrapper, daemon=True, name=f"job-{jid[:6]}")
    t.start()
