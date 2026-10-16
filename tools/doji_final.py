# -*- coding: utf-8 -*-
"""十字星候选配置的终验：分年年化 + 滑点敏感 + 排序表达式（一次加载、批量评估）。

网格台（doji_backtest.py）便宜参数全扫太慢，这里只盯住少数「已经赢过」的配置，
把每个配置在 千一/千二 两档滑点下、按 2023/2024/2025/2026 + FULL/TRAIN/VALID
七个窗口打全，顺带扫评分表达式（槽位少于候选数时排序决定结果）。

    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tools/doji_final.py
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tools/doji_final.py --pool u_star
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from doji_backtest import (END, START, WINS, YEAR_WINS, BUYS, build_cfg,  # noqa: E402
                           load_data, run_one)
from doji_pools import POOLS  # noqa: E402

# (池, 持有期, 止盈%, 槽位, 单仓%)
CFGS = [
    ("u_star", 1, 5.0, 10, 10.0),
    ("u_star", 1, 5.0, 5, 20.0),
    ("u_star", 1, 5.0, 20, 5.0),
    ("u_star", 1, 3.0, 10, 10.0),
    ("u_star", 2, 5.0, 10, 10.0),
    ("dn_star", 1, 5.0, 10, 10.0),
    ("legdom_lo", 1, 5.0, 5, 20.0),
    ("legdom_lo", 1, 5.0, 10, 10.0),
    ("gap_calm", 1, 5.0, 10, 10.0),
    ("gap_calm", 1, 5.0, 5, 20.0),
    ("leg_lo", 3, 5.0, 5, 20.0),
    ("leg_lo", 5, None, 20, 5.0),
]
SLIPS = [0.001, 0.002]
SCORES = ["", "ref(vol_ratio5, 1)", "-ref(vol_ratio5, 1)",
          "ref(lower_shadow, 1)", "-ref(amplitude, 1)",
          "-ref(pos60, 1)", "ref(lower_shadow, 1) - ref(upper_shadow, 1)"]
ALL_WINS = {**YEAR_WINS, **WINS}
YS = ("2023", "2024", "2025", "2026")


def evaluate(data, cfg, cfgs, label):
    print(f"\n----- {label} -----", flush=True)
    print(f"{'hold/tp/slot':<20}{'slip':>6}{'FULL':>8}{'TRAIN':>8}{'VALID':>8}"
          f"{'回撤':>8}{'笔':>6}{'均笔':>7}  " + " ".join(f"{y:>7}" for y in YS))
    for _p, hold, tp, npos, ppct in cfgs:
        for slip in SLIPS:
            m = run_one(data, cfg, npos, ppct, slip, hold, tp, None, ALL_WINS)
            f = m["FULL"]
            print(f"hold{hold}/tp{str(tp):>4}/{npos}x{ppct:<5}"
                  f"{slip:>7.3f}{f.get('annual_return') or 0:>8.2f}"
                  f"{m['TRAIN'].get('annual_return') or 0:>8.2f}"
                  f"{m['VALID'].get('annual_return') or 0:>8.2f}"
                  f"{f.get('max_drawdown') or 0:>8.2f}{f.get('trade_count') or 0:>6}"
                  f"{f.get('avg_pnl_pct') or 0:>+7.3f}  " + " ".join(
                      f"{(m[y].get('annual_return') or 0):>7.1f}" for y in YS),
                  flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", default="u_star")
    # 默认只跑「空评分」；--all-scores 才扫评分轴（每个评分都要重加载一次）
    ap.add_argument("--scores", default=None)
    ap.add_argument("--all-scores", action="store_true")
    ap.add_argument("--buy", default="none", choices=list(BUYS))
    args = ap.parse_args()
    pool_keys = [k for k in args.pool.split(",") if k]
    # 分隔符用 | —— 评分表达式里本来就有逗号
    scores = (SCORES if args.all_scores else [""]) if args.scores is None \
        else args.scores.split("|")
    buy = BUYS[args.buy]
    for key in pool_keys:
        conds, note = POOLS[key]
        cfgs = [c for c in CFGS if c[0] == key]
        if not cfgs:
            print(f"池 {key} 没在 CFGS 里配配置")
            continue
        for sc in scores:
            cfg = build_cfg(conds, [], buy, sc)
            data = load_data(cfg, conds,
                             note=f"池 {key}｜{note}｜评分 {sc or '（空）'}｜"
                                  f"确认 {args.buy}：")
            evaluate(data, cfg, cfgs,
                     f"{key} × score={sc or '（空）'} × buy={args.buy}")


if __name__ == "__main__":
    main()
