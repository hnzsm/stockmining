# -*- coding: utf-8 -*-
"""把 `_lab_report.py` 榜上的组合拿去**别的年份**验证（防过拟合）。

2025 是沪深300 +40% 的大年，样本内「收益为正」很可能是白捡的 β。
所以入选组合必须在**另外的区间**也能站住：这里默认跑 2024 全年 + 2026 年初至今。

    python tools/_lab_oos.py --top 12 --shard 1/6
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools import _lab_grid as grid  # noqa: E402

WINDOWS_DEFAULT = "2024-01-01~2024-12-31,2026-01-01~2026-09-28"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--merged", default=os.path.join(ROOT, "tools", "_lab_merged.json"))
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--min-trades", type=int, default=30)
    ap.add_argument("--windows", default=WINDOWS_DEFAULT)
    ap.add_argument("--shard", default="")
    ap.add_argument("--out", default=os.path.join(ROOT, "tools", "_lab_oos.json"))
    args = ap.parse_args()

    d = json.load(open(args.merged, encoding="utf-8"))
    rows = [r for r in d["rows"]
            if (r["metrics"].get("total_return") or 0) > 0
            and r["metrics"].get("trade_count", 0) >= args.min_trades]
    rows.sort(key=lambda r: -(r["metrics"].get("total_return") or 0))
    rows = rows[:args.top]
    pools = {p["id"]: p for p in grid.load_pools()}
    shapes = {s["key"]: s for s in grid.SHAPES}
    rows = [r for r in rows if r["pool_id"] in pools and r["shape"] in shapes]
    if args.shard:
        i, n = (int(x) for x in args.shard.split("/"))
        rows = [r for k, r in enumerate(rows) if k % n == i - 1]
        args.out = args.out.replace(".json", f"{i}_{n}.json")
    wins = [tuple(w.split("~")) for w in args.windows.split(",") if "~" in w]

    print(f"待验证 {len(rows)} 组 × 区间 {len(wins)} 个（样本内区间 {d.get('ranges')}）")
    out = []
    t0 = time.time()
    for r in rows:
        p = pools[r["pool_id"]]
        s = shapes[r["shape"]]
        tune = {"hold": r["hold"], "sl": r["sl"], "tp": r["tp"]}
        line = {"pool_id": r["pool_id"], "pool": r["pool"], "shape": r["shape"],
                "shape_name": r["shape_name"], "hold": r["hold"], "sl": r["sl"],
                "tp": r["tp"], "is": r["metrics"].get("total_return"),
                "is_trades": r["metrics"].get("trade_count"),
                "is_dd": r["metrics"].get("max_drawdown"), "oos": []}
        for (st, en) in wins:
            res = grid.run_one(p, s, tune, st, en, r.get("slip") or 0.001)
            if res.get("error"):
                print(f"  !! {r['pool']}/{r['shape']} {st}~{en} 失败：{res['error']}")
                continue
            m = res["metrics"]
            line["oos"].append({"range": f"{st}~{en}", "ret": m.get("total_return"),
                                "alpha": m.get("alpha"), "trades": m.get("trade_count"),
                                "dd": m.get("max_drawdown"), "win": m.get("win_rate"),
                                "pf": m.get("profit_factor"), "bench": m.get("benchmark_return")})
            print(f"  {r['pool'][:14]:<16}{r['shape']} hold={r['hold']:<3} "
                  f"{st}~{en} 收益 {m.get('total_return', 0):>7.2f}% "
                  f"超额 {m.get('alpha') or 0:>7.2f}% 笔数 {m.get('trade_count', 0):>5} "
                  f"胜率 {m.get('win_rate', 0):>5.1f}%", flush=True)
        out.append(line)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1, default=str)

    print(f"\n完成 {len(out)} 组，耗时 {time.time()-t0:.0f}s → {args.out}")


if __name__ == "__main__":
    main()
