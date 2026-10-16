# -*- coding: utf-8 -*-
"""回测数据缓存：`prepare_data` 产物的磁盘复用。

**为什么只缓存 prepare_data**：它只取决于「股票池 + 区间 + 指标口径 + 选股/买卖信号条件」，
而回测页最常调的是 `risk`（止损/止盈/持有期）与 `exec.slippage`（滑点）—— 这两个
**完全不参与** prepare_data（它们是 `run_backtest` 的输入，见 `backtest.resolve_params`）。
所以「同池同信号、只改参数」时整段加载与指标计算可以整块省掉。

⚠ 产物是**只读**的：`run_backtest` 从不写 `data`（只查 `idx/close/entry/...`），
所以复用同一份内存对象不会串味；写盘也发生在回测开始之前。

失效有三道闸：
  1. `digest` 里带 `config.BACKTEST_DEFAULTS` 之外的全部 cfg 键 → 改条件自动换 key；
  2. 带「实现指纹」（indicators/strategy/market 的 mtime）→ 改算法自动不认老文件；
  3. 带 `market.latest_trade_date()` + `sync_data.py` 同步后主动 `clear()` + TTL 兜底。

开关见 `config.BACKTEST_CACHE / _TTL_DAYS / _MAX_MB`。
"""
from __future__ import annotations

import hashlib
import json
import os
import pickle
import time

from config import (BACKTEST_CACHE, BACKTEST_CACHE_MAX_MB,
                    BACKTEST_CACHE_TTL_DAYS)
from core import log

LOG = log.get("btcache")

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(_HERE, "data", "cache", "bt")

# 产物结构变了就 +1：老文件会被当作不兼容直接丢弃（不做迁移）
SCHEMA = 1

ENABLED = bool(BACKTEST_CACHE)
TTL_SECONDS = max(0, int(BACKTEST_CACHE_TTL_DAYS)) * 86400
MAX_BYTES = max(0, int(BACKTEST_CACHE_MAX_MB)) * 1024 * 1024

# 明确「只服务 run_backtest」的键：改了它们不该换 key。
#
# ⚠ 这里**故意逐个列出、不用 `set(BACKTEST_DEFAULTS)`**：那份默认值里有 5 个键
#   （`exclude_st / exclude_kcb / exclude_bj / min_list_days / min_amount`）是**池过滤语义**，
#   `min_amount` 直接参与 `strategy.compute_pool()`（→ `in_pool` 掩码），其余几个
#   经 `pool_filters(cfg)` 影响 `market.apply_pool_filters()` —— **都改变产物**，
#   一律不能忽略。逐个列表的代价是「以后新增的运行时参数会多进 key」，
#   那只会少命中、不会错命中。
_IGNORE_KEYS = {
    # run_backtest 的仓位 / 成本参数
    "init_cash", "max_positions", "position_pct",
    "commission", "stamp_tax", "min_commission", "slippage",
    # 风控：止损/止盈/持有期限，只在日循环里用
    "max_hold_days", "min_hold_days",
    # 买卖策略自带（同样只服务 run_backtest）
    "risk", "exec",
    # 纯元数据，不参与任何计算
    "id", "name", "note", "run_name",
    "strategy_id", "pool_id", "created_at", "updated_at",
    "_name", "_ts",
}


def _ignore_keys() -> set[str]:
    """**不参与** prepare_data 的 cfg 键（见 `_IGNORE_KEYS` 的理由）。"""
    return set(_IGNORE_KEYS)


def _norm(v):
    """把 cfg 值转成可稳定序列化的形式（dict 按键排序、set 转有序 list）。"""
    if isinstance(v, bool) or v is None or isinstance(v, (int, str)):
        return v
    if isinstance(v, float):
        return v
    if isinstance(v, dict):
        return {str(k): _norm(x) for k, x in sorted(v.items(), key=lambda kv: str(kv[0]))}
    if isinstance(v, (list, tuple)):
        return [_norm(x) for x in v]
    if isinstance(v, (set, frozenset)):
        return sorted(str(x) for x in v)
    return str(v)


def impl_stamp() -> str:
    """实现指纹：算产物那几个模块的 mtime。改了算法，老缓存自动失效。

    ⚠ 必须含 `backtest`（`prepare_data` / `pack_one` 本体）与 `expr`（条件表达式引擎）：
    产物里装的就是**信号数组**（`entry/buy_trigger/sell_signal/score`），信号的算法在这两个
    文件里。少了它们，改一次「信号怎么算」只会静默命中旧产物 —— 回测数字变了、却查不出为什么
    （2026-10-04 补：原指纹只有 indicators/market/strategy 三个）。
    """
    from core import backtest, expr, indicators, market, strategy
    parts = []
    for m in (indicators, market, strategy, backtest, expr):
        f = getattr(m, "__file__", "")
        try:
            parts.append("%s@%d" % (os.path.basename(f), int(os.path.getmtime(f))))
        except OSError:
            parts.append(os.path.basename(f or "?") + "@0")
    return "|".join(parts)


def digest(codes, start, end, cfg) -> str:
    """缓存键。股票池按集合取（顺序无关），cfg 只取参与 prepare_data 的部分。"""
    from core import market
    try:
        latest = market.latest_trade_date() or ""
    except Exception:  # noqa: BLE001  取不到就不带，宁可少命中不可错命中
        latest = ""
    ignore = _ignore_keys()
    payload = {
        "v": SCHEMA,
        "impl": impl_stamp(),
        "codes": sorted({str(c) for c in codes}),
        "start": str(start),
        "end": str(end),
        "latest": latest,
        "cfg": {k: _norm(v) for k, v in cfg.items() if k not in ignore},
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def path_of(key: str) -> str:
    return os.path.join(CACHE_DIR, key + ".pkl")


def _silent_remove(p: str) -> bool:
    try:
        os.remove(p)
        return True
    except OSError:
        return False


def _list_files() -> list[str]:
    try:
        return [os.path.join(CACHE_DIR, f) for f in os.listdir(CACHE_DIR)
                if f.endswith(".pkl") or ".tmp" in f]
    except OSError:
        return []


def load(key: str):
    """命中则返回产物（`dict[code] -> numpy 结构`），否则 None。"""
    if not ENABLED or not key:
        return None
    p = path_of(key)
    if not os.path.isfile(p):
        return None
    try:
        size = os.path.getsize(p)
        if TTL_SECONDS and time.time() - os.path.getmtime(p) > TTL_SECONDS:
            _silent_remove(p)
            LOG.info("回测缓存过期已删 key=%s（TTL %d 天）", key, TTL_SECONDS // 86400)
            return None
        with open(p, "rb") as f:
            blob = pickle.load(f)
    except Exception as e:  # noqa: BLE001
        LOG.warning("回测缓存读取失败，已丢弃 key=%s: %s", key, e)
        _silent_remove(p)
        return None
    if not isinstance(blob, dict) or blob.get("v") != SCHEMA:
        _silent_remove(p)
        return None
    data = blob.get("data")
    if not isinstance(data, dict) or not data:
        _silent_remove(p)
        return None
    LOG.info("回测缓存命中 key=%s 标的 %d 只 / %.1f MB（跳过加载与指标计算）",
             key, len(data), size / 1048576.0)
    return data


def save(key: str, data) -> bool:
    """落盘（原子替换）。失败只记日志，绝不影响回测本身。"""
    if not ENABLED or not key or not data:
        return False
    os.makedirs(CACHE_DIR, exist_ok=True)
    p = path_of(key)
    tmp = "%s.tmp%d" % (p, os.getpid())
    try:
        with open(tmp, "wb") as f:
            pickle.dump({"v": SCHEMA, "ts": time.time(), "n": len(data), "data": data},
                        f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, p)          # 原子：读者要么看到旧文件、要么看到新的
        size = os.path.getsize(p)
    except Exception as e:  # noqa: BLE001
        LOG.warning("回测缓存写入失败（不影响本次回测）key=%s: %s", key, e)
        _silent_remove(tmp)
        return False
    LOG.info("回测缓存已写 key=%s 标的 %d 只 / %.1f MB",
             key, len(data), size / 1048576.0)
    prune()
    return True


def prune() -> int:
    """容量上限：超了按 mtime 从旧到新删到 80% 以下。返回删除个数。"""
    items = []
    for f in _list_files():
        try:
            items.append((f, os.path.getsize(f), os.path.getmtime(f)))
        except OSError:
            pass
    total = sum(s for _, s, _ in items)
    if not MAX_BYTES or total <= MAX_BYTES:
        return 0
    items.sort(key=lambda x: x[2])
    n = 0
    for f, s, _ in items:
        if total <= MAX_BYTES * 0.8:
            break
        if _silent_remove(f):
            total -= s
            n += 1
    if n:
        LOG.info("回测缓存超限，清理 %d 个旧文件（现 %.0f MB）", n, total / 1048576.0)
    return n


def clear(reason: str = "") -> int:
    """清空全部缓存。数据同步后调用 —— 行情/资金流一变，老产物就不再可信。"""
    n = 0
    for f in _list_files():
        if _silent_remove(f):
            n += 1
    if n:
        LOG.info("回测缓存已清空 %d 个文件%s", n, ("（" + reason + "）") if reason else "")
    return n


def info() -> dict:
    """状态快照：文件数 / 总大小 / 上限 / TTL（供排查与 `/api/config`）。"""
    files = _list_files()
    total, newest = 0, 0.0
    for f in files:
        try:
            total += os.path.getsize(f)
            newest = max(newest, os.path.getmtime(f))
        except OSError:
            pass
    return {
        "enabled": ENABLED,
        "dir": CACHE_DIR,
        "files": len(files),
        "mb": round(total / 1048576.0, 1),
        "max_mb": round(MAX_BYTES / 1048576.0, 1),
        "ttl_days": TTL_SECONDS / 86400.0,
        "newest": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(newest)) if newest else "",
        "schema": SCHEMA,
    }
