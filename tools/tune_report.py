# -*- coding: utf-8 -*-
"""调参结果汇总 + 深度复盘。

  --rank          只读 json，把 stage A / A2 / B(各池变体) 的样本排在一起看
  --deep VARI,N   重放某变体 TRAIN 排名前 N 的配置，跑分年窗口（2023/24/25/2026）
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from tune_eat1 import TRAIN, VALID, START, END, evaluate  # noqa: E402

WINS = {
    "2023": ("2023-01-01", "2023-12-31"),
    "2024": ("2024-01-01", "2024-12-31"),
    "2025": ("2025-01-01", "2025-12-31"),
    "2026": ("2026-01-01", "2026-09-18"),
    "TRAIN": TRAIN, "VALID": VALID, "FULL": (START, END),
}


def _ann(r):
    return (r["metrics"].get("TRAIN") or {}).get("annual_return") or -999


def rank():
    rows = []
    tj = os.path.join(ROOT, "tools", "_tune_eat1_result.json")
    t2 = os.path.join(ROOT, "tools", "_tune_eat2_result.json")
    for f, tag in ([tj, "A:risk"] if os.path.isfile(tj) else []) + \
                  ([t2, "A2:struct"] if os.path.isfile(t2) else []):
        j = json.load(open(f, encoding="utf-8"))
        for r in j["rows"]:
            r["src"] = tag
            rows.append(r)
    for f in glob.glob(os.path.join(ROOT, "tools", "_tune_eat3_*.json")):
        j = json.load(open(f, encoding="utf-8"))
        for r in j["rows"]:
            r["src"] = "B:" + j["variant"]
            rows.append(r)
    good = [r for r in rows
            if (r["metrics"].get("TRAIN") or {}).get("trade_count", 0) >= 30]
    good.sort(key=lambda r: -min(_ann(r),
                                 (r["metrics"].get("VALID") or {}).get("annual_return") or -999))
    print(f"总样本 {len(rows)}，TRAIN 笔数≥30 的 {len(good)}；按 min(TRAIN,VALID) 年化排：")
    hdr = f"{'src':<14}{'T年化':>8}{'V年化':>8}{'T笔':>5}{'胜率':>6}{'回撤':>8}  配置"
    print(hdr)
    for r in good[:25]:
        m, v, q = r["metrics"]["TRAIN"], r["metrics"]["VALID"], r["q"]
        print(f"{r['src']:<14}{m['annual_return']:>8.2f}{v['annual_return']:>8.2f}"
              f"{m['trade_count']:>5}{m.get('win_rate') or 0:>6.1f}"
              f"{m['max_drawdown']:>8.2f}  {json.dumps(q, ensure_ascii=False)}")


def deep(variant: str, n: int):
    """重放某池变体的 top-n：eat2 结构向量在缓存数组上换 buy/sell。"""
    import numpy as np  # noqa: F401
    if variant == "A":       # 现池（pool#5）
        from tune_eat1 import base_cfg as mk_cfg, load_data as ld
        cfg = mk_cfg()
        data = ld(cfg)
        conds_json = None
    else:
        import tune_eat3 as e3
        conds_json = e3.VARIANTS[variant]
        cfg, data, _ = e3.load_variant(variant, conds_json)
    from tune_eat1 import prune
    from tune_eat2 import build_arrays, variant_data, to_runq
    data = prune(data, cfg)
    extra = build_arrays(data)

    f = os.path.join(ROOT, "tools",
                     "_tune_eat1_result.json" if variant == "A" else
                     f"_tune_eat3_{variant}.json")
    if variant == "A2":
        f = os.path.join(ROOT, "tools", "_tune_eat2_result.json")
    j = json.load(open(f, encoding="utf-8"))
    rows = [r for r in j["rows"]
            if (r["metrics"].get("TRAIN") or {}).get("trade_count", 0) >= 30]
    rows.sort(key=lambda r: -_ann(r))
    rows = rows[:n]
    print(f"[deep] {variant}: {len(rows)} 个候选，分年窗口复盘")
    for r in rows:
        q = r["q"]
        if variant == "A":
            dv, runq = data, q
        else:
            dv = variant_data(data, extra, q.get("buy", "orig"), q.get("sell", "none"))
            runq = (to_runq(q) if "buy" in q else q)
            if "ppct" in q:
                runq["ppct"] = q["ppct"]
        ms = evaluate(dv, cfg, runq, WINS)
        tag = json.dumps(q, ensure_ascii=False)
        print("\n  " + tag[:180])
        line = "   "
        for w in ("2023", "2024", "2025", "2026", "FULL"):
            m = ms[w]
            line += (f" {w}: {m['annual_return']:+6.1f}%/{m['trade_count'] or 0}笔"
                     f"(dd {m['max_drawdown'] or 0:+.1f}) |")
        print(line)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--rank", action="store_true")
    ap.add_argument("--deep", nargs=2, metavar=("VARIANT", "N"))
    args = ap.parse_args()
    if args.rank:
        rank()
    elif args.deep:
        deep(args.deep[0], int(args.deep[1]))
    else:
        print("用 --rank 或 --deep VARI,N")
