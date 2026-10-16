# -*- coding: utf-8 -*-
"""Stage B：选股池消融 / 变体调参。

诊断（tools/diag_eat1_edge.py）：11 条池条件在 3.7 年里只给 499 个信号，
高开子集毛期望 +0.16%，扛不住 0.5%×2 的滑点 → 出路是放宽形态、提高样本量。
本脚本对「昨日大阴今日上影线」逐条放宽/删除条件，得到若干池变体；
每个变体在自身缓存上跑「结构（买/卖变体）× 风控 × 仓位」随机搜索。

用法（每个变体一个进程，控制并行度）:
    python tools/tune_eat3.py --variant core3 --stage 2000
    python tools/tune_eat3.py --list
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from config import BACKTEST_DEFAULTS  # noqa: E402
from core import backtest as bt, market, universe as uni  # noqa: E402
from core import strategy as st  # noqa: E402

from tune_eat1 import (TRAIN, VALID, START, END, evaluate, prune, fmt)  # noqa: E402
from tune_eat2 import (SPACE2, build_arrays, variant_data, to_runq,  # noqa: E402
                       BUY_VARS, SELL_VARS)

# 现池（pool#5）11 条
BASE_CONDS = ["total_mv < 10000000", "close < 50", "is_up = 1",
              "upper_shadow > 2*lower_shadow", "ref(is_down, 1) = 1",
              "ref(amplitude, 1) >= 5", "ref(amplitude, 1) <= 15",
              "ref(body, 1) <= -5", "ref(turnover_rate, 1) < 15",
              "ref(limit_up, 2) = 1", "ref(amplitude, 2) > 6"]

VARIANTS = {
    # 删单条：定位是哪条条件在杀期望
    "no_shadow": [c for c in BASE_CONDS if c != "upper_shadow > 2*lower_shadow"],
    "no_close50": [c for c in BASE_CONDS if c != "close < 50"],
    "no_updown": [c for c in BASE_CONDS if c not in ("is_up = 1",
                                                     "upper_shadow > 2*lower_shadow")],
    "no_turn": [c for c in BASE_CONDS if c != "ref(turnover_rate, 1) < 15"],
    "no_amp2": [c for c in BASE_CONDS if c != "ref(amplitude, 2) > 6"],
    # 核心三条：涨停 → 大阴 → 次日（不看今日K线长相）
    "core3": ["ref(limit_up, 2) = 1", "ref(is_down, 1) = 1", "ref(body, 1) <= -5"],
    # 核心三条 + 流动性下限（成交额 ≥ 1 亿）
    "core3_liq": ["ref(limit_up, 2) = 1", "ref(is_down, 1) = 1",
                  "ref(body, 1) <= -5", "amount >= 100000"],
    # 大阴当天入池（跌停板次日低吸买刀口）
    "dip": ["ref(limit_up, 1) = 1", "body <= -5"],
    # 上影线放宽到 1 倍下影
    "shadow1": [c if c != "upper_shadow > 2*lower_shadow"
                else "upper_shadow > lower_shadow" for c in BASE_CONDS],
}

FILT = {"exclude_st": True, "exclude_bj": True, "min_amount": 0,
        "min_list_days": 60, "exclude_kcb": False, "exclude_cyb": False}

# 在 eat2 空间上加仓位维度（稀疏信号要靠集中度）
SPACE3 = dict(SPACE2)
SPACE3.update({"npos": [2, 3, 5, 10], "ppct": [10, 20, 33, 50],
               "slip": [0.005, 0.002, 0.001]})


def make_cfg(conds, buy_exprs, sell_exprs):
    s = st.get_strategy(31)
    spec = uni.parse({"base": "ALL", "where": list(conds), "pool_exit": False})
    cfg = {**s["config"], "_pool": spec,
           "buy_trigger": list(buy_exprs), "sell_signal": list(sell_exprs)}
    return st.merge_config(cfg)


def load_variant(name, conds):
    """该池变体 × 表达式变体 的 prepare_data（走磁盘缓存）。"""
    cache = {}
    spec = {"base": "ALL", "where": list(conds)}
    codes = market.resolve_universe(spec, start=START, end=END)
    cfg = make_cfg(conds, ["open > ref(close, 1)"], [])
    data = bt.prepare_data(codes, START, END, cfg, batch=250, cache_info=cache)
    return cfg, data, cache.get("cache")


def to_runq3(v):
    q = to_runq(v)
    q["ppct"] = v["ppct"]
    return q


def fmt3(row):
    q = row["q"]
    line = fmt({"q": to_runq3(q), "metrics": row["metrics"]})
    return f"{q.get('buy','?'):>8}/{q.get('sell','?'):<10} ppct={q.get('ppct')} " + line


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", required=True)
    ap.add_argument("--stage", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=23)
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.list:
        for k, v in VARIANTS.items():
            print(f"{k:<12} {len(v)} 条")
        return

    conds = VARIANTS[args.variant]
    sfx = "" if args.seed == 23 else f"_{args.seed}"
    out_json = os.path.join(ROOT, "tools", f"_tune_eat3_{args.variant}{sfx}.json")
    t0 = time.time()
    cfg, data, chit = load_variant(args.variant, conds)
    print(f"[{args.variant}] 数据 {len(data)} 只 cache={chit} "
          f"({time.time()-t0:.0f}s)", flush=True)
    data = prune(data, cfg)
    extra = build_arrays(data)
    print(f"[{args.variant}] 裁剪后 {len(data)}", flush=True)

    rng = random.Random(args.seed)
    rows, cur_key, cur_dv = [], None, None
    for i in range(args.stage):
        q = {k: rng.choice(v) for k, v in SPACE3.items()}
        if q["ppct"] * q["npos"] > 100:      # 总仓位上限 100%，避免无意义组合
            q["ppct"] = max(5, 100 // q["npos"])
        key = (q["buy"], q["sell"])
        if key != cur_key:
            cur_dv = variant_data(data, extra, q["buy"], q["sell"])
            cur_key = key
        try:
            m = evaluate(cur_dv, cfg, to_runq3(q), {"TRAIN": TRAIN, "VALID": VALID})
        except Exception as e:  # noqa: BLE001
            continue
        rows.append({"q": q, "metrics": m})
        if i % 100 == 0:
            print(f"[{i+1}/{args.stage}] {time.time()-t0:.0f}s " + fmt3(rows[-1]),
                  flush=True)
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({"variant": args.variant, "conds": conds, "rows": rows},
                  f, ensure_ascii=False, indent=1)
    rows.sort(key=lambda r: -(r["metrics"]["TRAIN"].get("annual_return") or -100))
    print(f"\n[{args.variant}] TOP 15：")
    for r in rows[:15]:
        print(fmt3(r))


if __name__ == "__main__":
    main()
