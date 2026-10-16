# -*- coding: utf-8 -*-
"""统一日志模块。

用法::

    from core import log
    log.setup_logging()                 # 进程启动时调一次（幂等）
    logger = log.get(__name__)          # 各模块自己的 logger

    logger.debug("解析池子 %s", spec)
    with log.timer("加载行情"):
        ...
    @log.timed("选股")
    def run(...): ...

开关（config.py / 环境变量）：
    STOCKMINING_LOG_LEVEL    DEBUG/INFO/WARN/ERROR（默认随 FLASK_DEBUG）
    STOCKMINING_LOG_SQL=0    关闭 SQL 明细（慢查询仍会 WARN）
    STOCKMINING_LOG_SLOW_MS  慢查询阈值（毫秒）
    STOCKMINING_LOG_FILE=0   只打控制台
"""
from __future__ import annotations

import contextvars
import functools
import logging
import os
import re
import time
from collections import deque
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config import (LOG_BACKUPS, LOG_DIR, LOG_FILE, LOG_LEVEL, LOG_MAX_BYTES,
                    LOG_SLOW_MS, LOG_SQL, LOG_SQL_MAX, LOG_TO_CONSOLE,
                    LOG_TO_FILE)

ROOT_NAME = "stockmining"
FMT = "%(asctime)s.%(msecs)03d %(levelname)-5s [%(threadName)s/%(rid)s] %(name)s: %(message)s"
DATEFMT = "%m-%d %H:%M:%S"
LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO,
          "WARN": logging.WARNING, "WARNING": logging.WARNING,
          "ERROR": logging.ERROR}
# 结构化行首：09-12 18:42:01.835 ERROR [...] —— 用整字段匹配级别，
# 别用子串（"RuntimeError".upper() 里也有 "ERROR"，会把 traceback 续行算进来）
_LINE_LEVEL = re.compile(r"^\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}\s+([A-Z]+)\s")

_req_id: contextvars.ContextVar[str] = contextvars.ContextVar("rid", default="-")
logger = logging.getLogger(ROOT_NAME)


# ---------------------------------------------------------------------------
# 请求 id（Flask before_request 里设置，日志 Filter 自动带上）
# ---------------------------------------------------------------------------

def set_rid(rid: str) -> None:
    _req_id.set(rid or "-")


def get_rid() -> str:
    return _req_id.get()


class RidFilter(logging.Filter):
    """把当前请求 id 填进 %(rid)s；没有请求时为 '-'。"""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D102
        record.rid = _req_id.get() if _req_id.get() != "-" else "-"
        return True


# ---------------------------------------------------------------------------
# 初始化
# ---------------------------------------------------------------------------

def _resolve_level(level=None) -> int:
    name = (level or LOG_LEVEL or "INFO").upper()
    return LEVELS.get(name, logging.INFO)


def setup_logging(level: str | int | None = None, force: bool = False) -> logging.Logger:
    """配置 root logger：控制台 + 滚动文件。可重复调用（幂等）。"""
    root = logging.getLogger()
    lvl = _resolve_level(level)
    root.setLevel(lvl)

    if getattr(root, "_sm_ready", False) and not force:
        for h in root.handlers:
            h.setLevel(lvl)
        return logger

    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:  # noqa: BLE001
            pass

    fmt = logging.Formatter(FMT, datefmt=DATEFMT)
    rid_filter = RidFilter()

    if LOG_TO_CONSOLE:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        sh.addFilter(rid_filter)
        root.addHandler(sh)

    path = None
    if LOG_TO_FILE:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            fh = RotatingFileHandler(str(LOG_FILE), maxBytes=LOG_MAX_BYTES,
                                     backupCount=LOG_BACKUPS, encoding="utf-8")
            fh.setFormatter(fmt)
            fh.addFilter(rid_filter)
            root.addHandler(fh)
            path = LOG_FILE
        except Exception as e:  # noqa: BLE001
            root.addHandler(logging.NullHandler())
            print(f"[log] 无法写日志文件 {LOG_FILE}: {e}")

    # 第三方噪声：werkzeug 访问日志与自己的「→ / ←」重复，一律压到 WARNING
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("pymysql").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    root._sm_ready = True  # noqa: SLF001
    logger.info("日志已启动 level=%s file=%s", logging.getLevelName(lvl), path or "-")
    return logger


def get(name: str = ROOT_NAME) -> logging.Logger:
    """取一个 stockmining.* 下的 logger。"""
    if name.startswith(ROOT_NAME):
        return logging.getLogger(name)
    return logging.getLogger(f"{ROOT_NAME}.{name.split('.')[-1]}")


# ---------------------------------------------------------------------------
# SQL 日志
# ---------------------------------------------------------------------------

def sqlish(sql: str) -> str:
    """压缩空白并截断，用于日志里的 SQL 文本。"""
    s = re.sub(r"\s+", " ", str(sql)).strip()
    return s if len(s) <= LOG_SQL_MAX else s[:LOG_SQL_MAX] + f" …(+{len(s) - LOG_SQL_MAX})"


def _args_repr(args) -> str:
    if args is None:
        return ""
    s = repr(args)
    if len(s) > 200:
        s = s[:200] + f"…(+{len(s) - 200})"
    return " " + s


def log_sql(op: str, sql: str, args=None, dur_ms: float = 0.0,
            rows: int | None = None) -> None:
    """记录一条 SQL。明细走 DEBUG（受 LOG_SQL 控制），超时走 WARNING。"""
    slow = dur_ms >= LOG_SLOW_MS
    if LOG_SQL and logger.isEnabledFor(logging.DEBUG):
        tail = "" if rows is None else f" rows={rows}"
        logger.debug("SQL %s %.1fms%s | %s%s", op, dur_ms, tail,
                     sqlish(sql), _args_repr(args))
    if slow:
        logger.warning("慢查询 %.0fms %s | %s", dur_ms, op, sqlish(sql))


# ---------------------------------------------------------------------------
# 耗时打点
# ---------------------------------------------------------------------------

class Timer:
    """`with log.timer("加载行情"):` 或手工 `t = Timer("x"); t.done()`。"""

    def __init__(self, name: str, level: int = logging.DEBUG,
                slow_ms: float | None = None, log: logging.Logger | None = None):
        self.name = name
        self.level = level
        self.slow_ms = slow_ms
        self.log = log or logger
        self.t0 = time.perf_counter()
        self.ms = 0.0

    def _stop(self) -> float:
        if not self.ms:
            self.ms = (time.perf_counter() - self.t0) * 1000.0
        return self.ms

    def done(self, note: str = "") -> float:
        ms = self._stop()
        lvl = self.level
        if self.slow_ms is not None and ms >= self.slow_ms:
            lvl = logging.WARNING
        if self.log.isEnabledFor(lvl):
            self.log.log(lvl, "%s 耗时 %.1fms%s", self.name, ms, f" ({note})" if note else "")
        return ms

    def __enter__(self) -> Timer:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        note = f"{type(exc).__name__}: {exc}" if exc else ""
        self.done(note)
        return False


def timer(name: str, level: int = logging.DEBUG, slow_ms: float | None = None) -> Timer:
    return Timer(name, level=level, slow_ms=slow_ms)


@contextmanager
def step(name: str, level: int = logging.DEBUG, slow_ms: float | None = None):
    """同 timer，contextmanager 形式。"""
    t = Timer(name, level=level, slow_ms=slow_ms)
    try:
        yield t
    finally:
        t.done()


def timed(name: str | None = None, level: int = logging.DEBUG):
    """函数耗时装饰器。"""
    def deco(fn):
        label = name or fn.__qualname__

        @functools.wraps(fn)
        def wrapper(*a, **kw):
            with Timer(label, level=level):
                return fn(*a, **kw)
        return wrapper
    return deco


# ---------------------------------------------------------------------------
# 日志查看（供 /api/logs 使用）
# ---------------------------------------------------------------------------

def log_path() -> Path | None:
    return LOG_FILE if LOG_TO_FILE else None


def tail(n: int = 200, level: str = "", grep: str = "") -> list[str]:
    """读日志文件末尾 n 行（可按级别/关键字过滤）。"""
    p = log_path()
    if not p or not p.exists():
        return []
    n = max(1, min(int(n or 200), 5000))
    token = level.strip().upper()
    if token == "WARN":
        token = "WARNING"
    key = grep.strip()
    out: deque[str] = deque(maxlen=n)
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            size = f.seek(0, os.SEEK_END)
            chunk = 65536
            buf = ""
            pos = size
            while pos > 0 and len(out) < n:
                pos = max(0, pos - chunk)
                f.seek(pos)
                buf = f.read(min(chunk, size - pos)) + buf
                lines = buf.splitlines()
                buf = lines[0] if pos > 0 else ""
                for line in reversed(lines[1:] if pos > 0 else lines):
                    if token:
                        m = _LINE_LEVEL.match(line)
                        if not m or m.group(1) != token:
                            continue
                    if key and key not in line:
                        continue
                    out.appendleft(line)
                    if len(out) >= n:
                        break
    except Exception as e:  # noqa: BLE001
        return [f"[log] 读取失败: {e}"]
    return list(out)


def clear() -> bool:
    """清空日志文件（保留文件本身）。"""
    p = log_path()
    if not p:
        return False
    try:
        with open(p, "w", encoding="utf-8"):
            pass
        return True
    except Exception:  # noqa: BLE001
        return False
