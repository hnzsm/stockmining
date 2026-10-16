# -*- coding: utf-8 -*-
"""终选复核：把少量候选配置在「分年窗口 × 滑点敏感性」上重放，看成绩是否站得住。

    python tools/tune_eat7.py --variant no_close50 --file _cand_eat7.json
    python tools/tune_eat7.py --variant dip --file _cand_eat7.json --slip 0.001,0.002,0.005

`--file` 是候选清单：[{"name": ..., "variant": ..., "mode": ..., "q": {...}}]
输出每一行 = 一个候选 × 一个滑点，列出各年份的年化/回撤/笔数/胜率。
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--slip", default="0.001,0.002,0.005")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    cands = json.load(open(os.path.join(ROOT, "tools", args.file), encoding="utf-8"))
    slips = [float(x) for x in args.slip.split(",") if x]
    loaded = {}
    rows = []
    for c in cands:
        var = c["variant"]
        if var not in loaded:
            cfg, data, chit = e3.load_variant(var, e3.VARIANTS[var])
            data = prune(data, cfg)
            loaded[var] = (cfg, data, build_arrays(data))
            print(f"[{var}] {len(data)} 只 cache={chit}", flush=True)
        cfg, data, extra = loaded[var]
        # ⚠ 必须按 q["buy"]/q["sell"] 替换数组：data 里带的是策略 #31 的「高开才买」，
        #   而候选几乎都是 buy=always —— 不替换就测成了 gapup 变体，笔数会少一个量级。
        dv = variant_data(inject_scores(data, c.get("mode") or "none"),
                          extra, c["q"]["buy"], c["q"]["sell"])
        for slip in slips:
            q = {**c["q"], "slip": slip}
            t0 = time.time()
            m = evaluate(dv, cfg, e3.to_runq3(q), WINS)
            rows.append({"name": c["name"], "variant": var, "mode": c.get("mode"),
                         "q": q, "metrics": m})
            f = m["FULL"]
            print(f"  {c['name']:<26} slip={slip:<6} {time.time()-t0:>5.1f}s  "
                  f"FULL {f['annual_return']:>+7.2f}%/{f['max_drawdown']:>7.2f}%  "
                  f"{f['trade_count']:>4}笔  均笔 {f['avg_pnl_pct'] or 0:+.3f}%  "
                  + "  ".join(f"{w} {m[w]['annual_return']:>+6.1f}%" for w in WINS
                              if w not in ("TRAIN", "VALID", "FULL")), flush=True)

    out = args.out or os.path.join(ROOT, "tools",
                                   "_tune_eat7_" +
                                   os.path.splitext(os.path.basename(args.file))[0] + ".json")
    json.dump(rows, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n已保存 {out}")

    print("\n" + "=" * 118)
    print(f"{'候选':<26}{'滑点':<8}" + "".join(f"{w:>12}" for w in WINS))
    print("-" * 118)
    for r in rows:
        m = r["metrics"]
        print(f"{r['name']:<26}{r['q']['slip']:<8}"
              + "".join(f"{(m[w]['annual_return'] or 0):>8.1f}%/"
                        f"{abs(m[w]['max_drawdown'] or 0):>3.0f}" for w in WINS))


if __name__ == "__main__":
    main()
