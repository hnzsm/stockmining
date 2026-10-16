# -*- coding: utf-8 -*-
"""汇总收紧实验（`_lab_tighten.py` 产出的分片），回答两个问题：

  1. 各池**现在**每天选出多少只（基线 T0）——「选出的股相对少一些」的量化起点；
  2. 追加哪条过滤能**显著变少、而收益不塌**。

筛选口径（可调）：
  * `--min-cut`  命中数至少要降这么多（默认 30%）；
  * `--max-loss` 收益相对基线最多掉多少个百分点（默认 10pt，两档持有期都要满足）；
  * `--min-avg`  收紧后日均仍要有这么多只（默认 3，防止把池子打死）。

    python tools/_lab_tight_report.py
    python tools/_lab_tight_report.py --min-cut 50 --max-loss 5
"""
from __future__ import annotations

import argparse
import glob
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load():
    rows = []
    for f in sorted(glob.glob(os.path.join(ROOT, "tools", "_lab_tight_*.json"))):
        with open(f, encoding="utf-8") as fh:
            data = json.load(fh)
        for r in (data if isinstance(data, list) else data.get("rows", [])):
            if r.get("var") == "T0" and r.get("pool") == "60日新高" \
                    and "probe" in f:      # 探针文件不算（区间/参数可能不同）
                continue
            rows.append(r)
    return rows


def f(v, d=1):
    return "—" if v is None else f"{float(v):.{d}f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-cut", type=float, default=30.0)
    ap.add_argument("--max-loss", type=float, default=10.0)
    ap.add_argument("--min-avg", type=float, default=3.0)
    ap.add_argument("--holds", default="10,20")
    args = ap.parse_args()
    holds = [int(x) for x in args.holds.split(",") if x.strip()]

    rows = load()
    if not rows:
        print("没有结果文件（tools/_lab_tight_*.json）"); return

    by: dict[tuple, dict] = {}
    for r in rows:
        by[(r["pool_id"], r["var"])] = r
    pools = {}
    for (pid, _v), r in by.items():
        pools.setdefault(pid, r["pool"])
    print(f"共 {len(rows)} 格，{len(pools)} 个池；区间 {rows[0].get('range')}，"
          f"形状 {rows[0].get('shape')}\n")

    print("=== 一、基线（现在每天选多少只）===")
    print(f"{'池':<22}{'日均':>7}{'中位':>6}{'p90':>6}{'最多':>6}{'空仓':>6}"
          + "".join(f"{'持'+str(h)+'收益':>11}" for h in holds)
          + "".join(f"{'持'+str(h)+'笔':>8}" for h in holds))
    base = {}
    for pid in sorted(pools):
        r = by.get((pid, "T0"))
        if not r:
            print(f"{pools[pid][:20]:<22}(缺 T0 基线)")
            continue
        base[pid] = r
        line = f"{pools[pid][:20]:<22}{f(r['avg']):>7}{r['med']:>6}{r['p90']:>6}" \
               f"{r['max']:>6}{r['empty']:>6}"
        for h in holds:
            hh = r.get(f"h{h}") or {}
            line += f"{f(hh.get('ret'), 2):>11}"
        for h in holds:
            hh = r.get(f"h{h}") or {}
            line += f"{hh.get('trades') or 0:>8}"
        print(line)

    print("\n=== 二、收紧效果（相对该池基线）===")
    print(f"{'池':<20}{'变体':<6}{'追加条件':<26}{'日均':>7}{'降幅':>8}"
          + "".join(f"{'持'+str(h):>9}" for h in holds)
          + "".join(f"{'持'+str(h)+'Δ':>9}" for h in holds) + "  判定")
    picks = []
    for pid in sorted(pools):
        b = base.get(pid)
        if not b:
            continue
        for (qid, v), r in sorted(by.items()):
            if qid != pid or v == "T0":
                continue
            cut = (b["avg"] - r["avg"]) / b["avg"] * 100 if b["avg"] else 0.0
            deltas, rets = [], []
            for h in holds:
                rb = (b.get(f"h{h}") or {}).get("ret")
                rv = (r.get(f"h{h}") or {}).get("ret")
                rb = 0.0 if rb is None else float(rb)
                rv = 0.0 if rv is None else float(rv)
                deltas.append(rv - rb)
                rets.append(rv)
            worst = min(deltas)
            ok = (cut >= args.min_cut and worst >= -args.max_loss
                  and r["avg"] >= args.min_avg)
            line = f"{pools[pid][:18]:<20}{v:<6}{r.get('var_name','')[:24]:<26}" \
                   f"{f(r['avg']):>7}{cut:>7.0f}%"
            for x in rets:
                line += f"{x:>9.1f}"
            for x in deltas:
                line += f"{x:>+9.1f}"
            line += ("  ✅推荐" if ok else "")
            print(line)
            if ok:
                picks.append((pid, pools[pid], v, r.get("extra", []), r["avg"],
                              cut, rets, deltas))

    print(f"\n=== 三、满足「降幅>={args.min_cut}% 且 收益最多掉 {args.max_loss}pt "
          f"且 日均>={args.min_avg} 只」的组合 ===")
    if not picks:
        print("（无 —— 说明在这个口径下「少选股」必然要以收益为代价，"
              "见下方结论）")
    for pid, pname, v, extra, avg, cut, rets, deltas in sorted(
            picks, key=lambda x: -x[5]):
        print(f"  池#{pid} {pname} → {v} {' + '.join(extra)}")
        print(f"      日均 {avg}（降 {cut:.0f}%）；收益 " +
              " / ".join(f"持{h} {r:.1f}%({d:+.1f}pt)"
                         for h, r, d in zip(holds, rets, deltas)))


if __name__ == "__main__":
    main()
