# -*- coding: utf-8 -*-
"""评分杠杆实验：不改缓存，直接在紧凑数组上注入自定义 score。

引擎按 v["score"][买入日 i] 降序挑标的；仓位坑位 < 当日命中时，排序规则
决定买谁。score 只用「买入时刻已知」的信息（昨日及更早），无未来函数：
  none  原样（全 NaN → 插入序）
  cp    形态日收盘位置 close_pos =（收-低)/(高-低)
  body  形态日实体涨幅 收/开-1
  gap   买入日高开幅度 开/昨收-1（追最强开盘）
  nogap 买入日开盘越弱越好（低吸）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import numpy as np  # noqa: E402

from tune_eat1 import TRAIN, VALID, evaluate, prune  # noqa: E402
import tune_eat3 as e3  # noqa: E402


def inject_scores(data, mode):
    out = {}
    for code, v in data.items():
        d = dict(v)
        op, hi, lo, cl = v["open"], v["high"], v["low"], v["close"]
        n = len(cl)
        with np.errstate(invalid="ignore", divide="ignore"):
            cp = (cl - lo) / np.maximum(hi - lo, 1e-9)
            body = cl / op - 1.0
            prev = np.concatenate(([np.nan], cl[:-1]))
            gap = op / prev - 1.0
        nan = np.full(n, np.nan)
        if mode == "none":
            sc = nan
        elif mode == "cp":
            sc = np.concatenate(([np.nan], cp[:-1]))       # 昨日收盘位置
        elif mode == "body":
            sc = np.concatenate(([np.nan], body[:-1]))
        elif mode == "gap":
            sc = gap                                        # 买入日开盘已知
        elif mode == "nogap":
            sc = -gap
        d["score"] = sc
        out[code] = d
    return out


GRID = []
for buy in ["always", "orig"]:
    for sell in ["none", "loser_open"]:
        for hold in [1, 2, 3, 5, 8]:
            for tp in [None, 2, 5, 8]:
                for npos, ppct in [(3, 33), (5, 20), (10, 10), (2, 50)]:
                    GRID.append({"buy": buy, "sell": sell, "pool_exit": False,
                                 "stop": None, "tp": tp, "hold": hold,
                                 "sell_px": "open", "hold_px": "open",
                                 "slip": 0.001, "npos": npos, "ppct": ppct})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="no_close50")
    ap.add_argument("--stage", type=int, default=120)
    args = ap.parse_args()
    conds = e3.VARIANTS[args.variant]
    cfg, data, chit = e3.load_variant(args.variant, conds)
    data = prune(data, cfg)
    from tune_eat2 import build_arrays, variant_data
    extra = build_arrays(data)
    rng = np.random.default_rng(5)
    picks = [GRID[i] for i in rng.choice(len(GRID), size=min(args.stage, len(GRID)),
                                         replace=False)]
    rows = []
    t0 = time.time()
    for mode in ["none", "cp", "body", "gap", "nogap"]:
        dv0 = inject_scores(data, mode)
        cur = None
        for q in picks:
            key = (q["buy"], q["sell"])
            if key != cur:
                dv = variant_data(dv0, extra, q["buy"], q["sell"])
                cur = key
            m = evaluate(dv, cfg, e3.to_runq3(q), {"TRAIN": TRAIN, "VALID": VALID})
            rows.append({"mode": mode, "q": q, "metrics": m})
        print(f"[{mode}] 完成 {len(picks)} 个，{time.time()-t0:.0f}s", flush=True)
    out = os.path.join(ROOT, "tools", f"_tune_scores_{args.variant}.json")
    json.dump(rows, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    rows.sort(key=lambda r: -min(r["metrics"]["TRAIN"]["annual_return"],
                                 r["metrics"]["VALID"]["annual_return"]))
    print("\nTOP 15（min(T,V) 年化）：")
    for r in rows[:15]:
        m, v = r["metrics"]["TRAIN"], r["metrics"]["VALID"]
        print(f"{r['mode']:>5} T {m['annual_return']:>7.2f} V {v['annual_return']:>7.2f} "
              f"笔{m['trade_count']:>4} 胜{m['win_rate']:>5.1f} dd{m['max_drawdown']:>7.2f} | "
              + json.dumps(r["q"], ensure_ascii=False))


if __name__ == "__main__":
    main()
