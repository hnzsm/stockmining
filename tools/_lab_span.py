# -*- coding: utf-8 -*-
"""把入选组合放到**一整段连续区间**里跑一遍（2024-01-01 ~ 今天），看「一直跑」是什么样。

前面的 2025 网格是**挑过**的（按 2025 收益排序取前排），所以只有 2024/2026 那两格是干净的样本外；
但真正要落地，关心的是「不挑年份、从头跑到尾」的曲线。这里就跑这一件事，并同时给出
沪深300 / 中证500 / 上证 / 池内等权 四个对照。

    python tools/_lab_span.py --combos 58:S4:20,2:S4:20,57:S4:10 --shard 1/3
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import db  # noqa: E402
from tools import _lab_grid as grid  # noqa: E402

DEFAULT_COMBOS = "58:S4:20,58:S4:10,2:S4:20,2:S4:10,57:S4:10,57:S4:5,3:S4:10,117:S4:20"


def index_ret(code, start, end):
    rows = db.query("SELECT trade_date, close FROM index_daily WHERE ts_code=%s "
                    "AND trade_date BETWEEN %s AND %s ORDER BY trade_date",
                    (code, start, end))
    if len(rows) < 2:
        return None
    return round((float(rows[-1]["close"]) / float(rows[0]["close"]) - 1) * 100, 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--combos", default=DEFAULT_COMBOS)
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default="2026-09-28")
    ap.add_argument("--shard", default="")
    ap.add_argument("--out", default=os.path.join(ROOT, "tools", "_lab_span.json"))
    args = ap.parse_args()

    combos = []
    for c in args.combos.split(","):
        pid, sk, hold = c.strip().split(":")
        combos.append((int(pid), sk.upper(), int(hold)))
    if args.shard:
        i, n = (int(x) for x in args.shard.split("/"))
        combos = [c for k, c in enumerate(combos) if k % n == i - 1]
        args.out = args.out.replace(".json", f"{i}_{n}.json")

    pools = {p["id"]: p for p in grid.load_pools()}
    shapes = {s["key"]: s for s in grid.SHAPES}
    bench = {c: index_ret(c, args.start, args.end)
             for c in ("000300.SH", "000905.SH", "000001.SH")}
    print(f"区间 {args.start}~{args.end}   指数对照 {bench}", flush=True)

    out = []
    for pid, sk, hold in combos:
        if pid not in pools or sk not in shapes:
            print(f"  跳过 {pid}/{sk}（不在库里）"); continue
        t0 = time.time()
        r = grid.run_one(pools[pid], shapes[sk], {"hold": hold, "sl": None, "tp": None},
                         args.start, args.end, 0.001)
        if r.get("error"):
            print(f"  !! {pid}/{sk} {r['error']}"); continue
        m = r["metrics"]
        line = {"pool_id": pid, "pool": pools[pid]["name"], "shape": sk, "hold": hold,
                "range": f"{args.start}~{args.end}",
                "ret": m.get("total_return"), "bench_eq": m.get("benchmark_return"),
                "bench_idx": bench, "trades": m.get("trade_count"),
                "win": m.get("win_rate"), "pf": m.get("profit_factor"),
                "dd": m.get("max_drawdown"), "avg_hold": m.get("avg_hold_days"),
                "secs": round(time.time() - t0)}
        out.append(line)
        print(f"  {pools[pid]['name'][:16]:<18}{sk} 持{hold:<3}"
              f" 收益 {m.get('total_return', 0):>7.2f}%  "
              f"vs 池内等权 {m.get('benchmark_return')}%  vs 沪深300 {bench['000300.SH']}%  "
              f"回撤 {m.get('max_drawdown')}%  笔数 {m.get('trade_count')}  "
              f"胜率 {m.get('win_rate')}%  盈亏比 {m.get('profit_factor')}  "
              f"[{line['secs']}s]", flush=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1, default=str)
    print(f"→ {args.out}")


if __name__ == "__main__":
    main()
