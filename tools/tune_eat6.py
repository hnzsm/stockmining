# -*- coding: utf-8 -*-
"""定向复核：给定池变体 × 评分模式 × 结构网格，输出分年窗口成绩。

    python tools/tune_eat6.py --variant dip --score-mode none,nogap,gap,cp,body
    python tools/tune_eat6.py --variant no_close50 --score-mode cp,gap

⚠ 网格是 buy=always / sell=none，必须用 `variant_data` 真正替换 `buy_trigger` 数组：
  data 里带的是买卖策略 #31 原始的「高开才买」，不替换就等于整张网格只在测 gapup，
  笔数会少一个数量级、年化也全不是那回事（2026-09-19 踩过一次）。
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

from tune_eat1 import evaluate, prune  # noqa: E402
import tune_eat3 as e3  # noqa: E402
from tune_eat5 import inject_scores  # noqa: E402
from tune_eat2 import build_arrays, variant_data  # noqa: E402

WINS = {
    "2023": ("2023-01-01", "2023-12-31"),
    "2024": ("2024-01-01", "2024-12-31"),
    "2025": ("2025-01-01", "2025-12-31"),
    "2026": ("2026-01-01", "2026-09-18"),
    "TRAIN": ("2023-01-01", "2025-12-31"),
    "VALID": ("2026-01-01", "2026-09-18"),
    "FULL": ("2023-01-01", "2026-09-18"),
}

GRID = []
for hold in [1, 2, 3]:
    for hold_px in ["close", "open"]:
        for tp in [None, 2, 5]:
            for npos, ppct in [(2, 50), (3, 33), (5, 20), (10, 10)]:
                GRID.append({"buy": "always", "sell": "none", "pool_exit": False,
                             "stop": None, "tp": tp, "hold": hold,
                             "sell_px": "open", "hold_px": hold_px,
                             "slip": 0.001, "npos": npos, "ppct": ppct})


def _ann(m, w):
    return ((m.get(w) or {}).get("annual_return")) or float("nan")


def _cnt(m, w):
    return (m.get(w) or {}).get("trade_count") or 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", required=True)
    ap.add_argument("--score-mode", default="none,cp,gap")
    args = ap.parse_args()
    modes = [m for m in args.score_mode.split(",") if m]
    conds = e3.VARIANTS[args.variant]
    cfg, data, chit = e3.load_variant(args.variant, conds)
    data = prune(data, cfg)
    print(f"[{args.variant}] {len(data)} 只 cache={chit}", flush=True)

    extra = build_arrays(data)
    rows = []
    t0 = time.time()
    for mode in modes:
        dv = variant_data(inject_scores(data, mode), extra, "always", "none")
        for q in GRID:
            m = evaluate(dv, cfg, e3.to_runq3(q), WINS)
            rows.append({"mode": mode, "q": q, "metrics": m})
        print(f"[{mode}] {time.time()-t0:.0f}s", flush=True)

    out = os.path.join(ROOT, "tools", f"_tune_eat6_{args.variant}.json")
    json.dump(rows, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n[{args.variant}] {len(rows)} 个配置，按 FULL 年化降序（前 40）：")
    print("mode  hold tp  npos×ppct | " + " | ".join(f"{w:>16}" for w in WINS))
    for r in sorted(rows, key=lambda r: -_ann(r["metrics"], "FULL"))[:40]:
        m, q = r["metrics"], r["q"]
        cells = " | ".join(f"{_ann(m, w):+7.1f}% {_cnt(m, w):>4}笔" for w in WINS)
        print(f"{r['mode']:>5} h{q['hold']} tp={str(q['tp']):>4} "
              f"{q['npos']}x{q['ppct']:>2} | {cells}")


if __name__ == "__main__":
    main()
