# -*- coding: utf-8 -*-
"""回测数据缓存回归（`core.btcache` + `backtest.prepare_data`，2026-09-19 加）。

背景：回测 99% 的时间花在 `prepare_data`（读行情 + 算指标 + 算信号），而它**只取决于**
「股票池 + 区间 + 指标口径 + 信号条件」—— 回测页最常调的 `risk`（止损/止盈/持有期）与
`exec.slippage` 压根不参与它。于是把产物落盘复用，就是"改参数不用重跑全市场"。

这个脚本盯三件事，**不看耗时下结论**：

  1. **键的语义**：只该随池/区间/指标/信号变。
     · 改 `risk` / `exec.slippage` / `init_cash` / `max_hold_days` → **不许**换 key（这是前提）；
     · 改 `min_amount`（池过滤）→ **必须**换 key（它进 `in_pool` 掩码），
       这条守着"别把池过滤误当运行时参数忽略掉"。
  2. **命中产物与现算逐值一致**：缓存最容易出的错是"悄悄给了旧结果"，
     所以逐字段对拍（含 NaN 位置），而不是只比长度。
  3. **坏文件不炸**：版本不符 / 字节损坏 / 过期 → 丢弃并重算，绝不能把回测带崩。

不跑真回测、不写业务表，全段约 1 分钟。
用法：$PY -W ignore tools/verify_btcache.py
"""
from __future__ import annotations

import os
import pickle
import sys
import time

sys.path.insert(0, ".")

import numpy as np

from core import backtest as bt
from core import btcache, db, market, strategy as st

SAMPLE = 120     # 抽样只数：够跑全链路，又不至于慢
BARS = 90        # 区间长度（交易日）

PASS = 0
FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  OK   %s%s" % (name, (" — " + detail) if detail else ""))
    else:
        FAIL += 1
        print("  FAIL %s — %s" % (name, detail))


def base_cfg() -> dict:
    cfg = st.merge_config({})
    cfg["entry"] = ["close >= boll_upper"]
    cfg["_name"] = "verify_btcache 临时"
    return cfg


def arr_eq(x, y) -> bool:
    """数值数组相等（NaN 位置必须一致 —— np.array_equal 默认把 NaN 当不相等）。"""
    a = np.asarray(x)
    b = np.asarray(y)
    if a.shape != b.shape:
        return False
    if a.dtype.kind == "f" or b.dtype.kind == "f":
        a = a.astype(float)
        b = b.astype(float)
        na, nb = np.isnan(a), np.isnan(b)
        return bool(np.array_equal(na, nb) and
                    np.array_equal(a[~na], b[~nb]))
    return bool(np.array_equal(a, b))


FLOAT_KEYS = ("open", "high", "low", "close", "score")
BOOL_KEYS = ("entry", "in_pool", "buy_trigger", "sell_signal")


def diff_data(a, b) -> str:
    """逐值对拍两份产物，返回不一致说明（空串 = 一致）。"""
    if set(a) != set(b):
        return "标的集合不同：%d vs %d" % (len(a), len(b))
    for code in a:
        x, y = a[code], b[code]
        if x.get("name") != y.get("name"):
            return "%s 的 name 不同" % code
        dx, dy = x.get("dates"), y.get("dates")
        if (dx is None) != (dy is None):
            return "%s 的 dates 有一侧缺失" % code
        if dx is not None and list(dx) != list(dy):
            return "%s 的 dates 不同" % code
        if dict(x.get("idx") or {}) != dict(y.get("idx") or {}):
            return "%s 的 idx 不同" % code
        for k in FLOAT_KEYS + BOOL_KEYS:
            if k not in x or k not in y:
                return "%s 缺字段 %s" % (code, k)
            if not arr_eq(x[k], y[k]):
                return "%s 的 %s 逐值不同" % (code, k)
    return ""


def main():
    global PASS, FAIL
    if not btcache.ENABLED:
        print("缓存未启用（config.BACKTEST_CACHE=False）—— 只跑键语义段")

    latest = market.latest_trade_date()
    rows = db.query("SELECT DISTINCT trade_date FROM daily_kline WHERE trade_date <= %s "
                    "ORDER BY trade_date DESC LIMIT %s", [latest, BARS])
    days = sorted(str(r["trade_date"]) for r in rows)
    if len(days) < 10:
        print("行情不足（%d 天），跳过" % len(days))
        sys.exit(0)
    start, end = days[0], days[-1]

    allc = market.resolve_universe("all", start=end, end=end)
    step = max(1, len(allc) // SAMPLE)
    codes = sorted(allc[::step][:SAMPLE])
    cfg = base_cfg()
    print("回测缓存回归 | 区间 %s ~ %s | 抽样 %d 只 | 缓存目录 %s\n"
          % (start, end, len(codes), btcache.CACHE_DIR))

    # ---- 1. 键的语义 -----------------------------------------------------
    print("---- 1. 缓存键只随「池 / 区间 / 指标口径 / 信号条件」变 ----")
    k0 = btcache.digest(codes, start, end, cfg)
    check("同一输入两次 digest 相同", btcache.digest(codes, start, end, cfg) == k0)

    ignorable = {
        "risk": {"stop_loss": -5.0, "take_profit": 10.0, "max_hold_days": 3},
        "exec": {"slippage": 0.002},
        "init_cash": 500000.0,
        "max_positions": 3,
        "commission": 0.001,
        "slippage": 0.005,
        "max_hold_days": 20,
    }
    for field, val in ignorable.items():
        alt = dict(cfg)
        alt[field] = val
        check("改 %s 不换 key（它只在 run_backtest 生效）" % field,
              btcache.digest(codes, start, end, alt) == k0)

    check("股票池顺序无关",
          btcache.digest(list(reversed(codes)), start, end, cfg) == k0)

    alt = dict(cfg)
    alt["entry"] = ["close >= boll_lower"]
    check("改 entry（选股条件）换 key",
          btcache.digest(codes, start, end, alt) != k0)

    alt = dict(cfg)
    alt["indicators"] = {"ma": [5, 20]}
    check("改 indicators 换 key",
          btcache.digest(codes, start, end, alt) != k0)

    check("改股票池换 key",
          btcache.digest(codes[:-1], start, end, cfg) != k0)
    check("改区间换 key",
          btcache.digest(codes, days[1], end, cfg) != k0)

    alt = dict(cfg)
    alt["_pool"] = {"kind": "cond", "base": "all", "where": [],
                    "filt": {"min_amount": 20000.0}}
    check("改池过滤 min_amount 换 key（它进 in_pool，不能被当运行时参数忽略）",
          btcache.digest(codes, start, end, alt) != k0)

    # ---- 2. miss -> hit，且逐值一致 ---------------------------------------
    print("\n---- 2. 首次现算落盘，二次命中复用 ----")
    btcache.clear("verify_btcache 开跑前")
    info1: dict = {}
    t0 = time.perf_counter()
    d1 = bt.prepare_data(codes, start, end, cfg, cache_info=info1)
    t1 = time.perf_counter() - t0
    check("首次 cache=miss 且产物非空",
          info1.get("cache") == "miss" and len(d1) > 0,
          "cache=%s 标的 %d 只 / 现算 %.2fs" % (info1.get("cache"), len(d1), t1))

    info2: dict = {}
    t0 = time.perf_counter()
    d2 = bt.prepare_data(codes, start, end, cfg, cache_info=info2)
    t2 = time.perf_counter() - t0
    check("二次 cache=hit", info2.get("cache") == "hit", "cache=%s" % info2.get("cache"))
    check("命中确实更快", t2 < t1, "现算 %.2fs → 命中 %.2fs（%.0f%%）"
          % (t1, t2, (1 - t2 / t1) * 100 if t1 else 0))

    d = diff_data(d1, d2)
    check("命中产物与现算逐值一致（含 NaN 位置）", not d, d)

    # ---- 3. 坏文件不炸 ----------------------------------------------------
    print("\n---- 3. 版本不符 / 损坏 / 过期 —— 一律丢弃重算，不许抛 ----")
    key = info1.get("key") or ""
    path = btcache.path_of(key)
    check("产物已落盘", bool(key) and os.path.isfile(path), path)

    # 3.1 版本不符
    with open(path, "rb") as f:
        blob = pickle.load(f)
    blob["v"] = 999
    with open(path, "wb") as f:
        pickle.dump(blob, f)
    check("SCHEMA 不符 → 丢弃", btcache.load(key) is None)
    check("SCHEMA 不符 → 文件已删", not os.path.isfile(path))

    # 3.2 字节损坏
    fake = "verifybt000000000000"
    fps = btcache.path_of(fake)
    os.makedirs(btcache.CACHE_DIR, exist_ok=True)
    with open(fps, "wb") as f:
        f.write(b"not-a-pickle-at-all")
    got = None
    try:
        got = btcache.load(fake)
    except Exception as e:  # noqa: BLE001
        got = "RAISED:%s" % e
    check("损坏文件 → 返回 None 不抛", got is None, repr(got))
    check("损坏文件 → 已删", not os.path.isfile(fps))

    # 3.3 过期（造一份真产物再改 mtime）
    btcache.save(key, d1)
    old = time.time() - btcache.TTL_SECONDS - 60
    os.utime(path, (old, old))
    check("超过 TTL → 丢弃", btcache.load(key) is None)
    check("超过 TTL → 文件已删", not os.path.isfile(path))

    # ---- 4. clear --------------------------------------------------------
    print("\n---- 4. clear（数据同步后调用） ----")
    btcache.save(key, d1)
    before = btcache.info()["files"]
    n = btcache.clear("回归收尾")
    after = btcache.info()["files"]
    check("clear 清空目录", before > 0 and after == 0 and n == before,
          "清掉 %d 个（%d → %d）" % (n, before, after))

    print("\n回测缓存回归：%d 通过 / %d 失败" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
