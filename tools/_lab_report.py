# -*- coding: utf-8 -*-
"""合并 `tools/_lab_grid*.json` 的分片结果，出榜。

    python tools/_lab_report.py                  # 默认：只看笔数≥30 的
    python tools/_lab_report.py --min-trades 50 --top 30
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def load(glob_pat):
    rows, ranges = [], set()
    for f in sorted(glob.glob(glob_pat)):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"  跳过 {os.path.basename(f)}：{e}")
            continue
        ranges.add(d.get("range"))
        rows.extend(d.get("rows") or [])
    return rows, sorted(ranges)


def dedup(rows):
    seen, out = set(), []
    for r in rows:
        k = (r.get("pool_id"), r.get("shape"), r.get("hold"), r.get("sl"),
             r.get("tp"), r.get("slip"))
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--glob", default=os.path.join(ROOT, "tools", "_lab_grid*.json"))
    ap.add_argument("--min-trades", type=int, default=30)
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--out", default=os.path.join(ROOT, "tools", "_lab_merged.json"))
    args = ap.parse_args()

    rows, ranges = load(args.glob)
    rows = dedup(rows)
    print(f"合并 {len(rows)} 组   区间 {ranges}")
    if not rows:
        return
    # ⚠ 引擎不传 benchmark 时，`benchmark_return` 是**池内等权买入持有**，
    # 不是沪深300（见 core/backtest.py 的 PERF_ITEMS 标签「买入持有」）。
    # 所以这里的「超额」= 相对同池等权躺平的差额；要跟指数比得自己算（_lab_span.py 会给）。
    bench = rows[0]["metrics"].get("benchmark_return")
    print(f"池内等权买入持有（不是沪深300）同期：{bench}%")

    def tr(r):
        return r["metrics"].get("total_return") or 0

    def alpha(r):
        return r["metrics"].get("alpha") or 0

    ok = [r for r in rows if r["metrics"].get("trade_count", 0) >= args.min_trades]
    pos = [r for r in ok if tr(r) > 0]
    print(f"笔数≥{args.min_trades}：{len(ok)} 组；其中收益为正：{len(pos)} 组")

    hdr = (f"{'池':<18}{'形状':<20}{'持有':>4}{'止损':>6}{'止盈':>6}"
           f"{'收益%':>9}{'超额%':>8}{'回撤%':>8}{'笔数':>6}{'胜率%':>7}"
           f"{'盈亏比':>7}{'持股天':>7}")
    print("\n【按收益排序】")
    print(hdr)
    for r in sorted(ok, key=tr, reverse=True)[:args.top]:
        m = r["metrics"]
        print(f"{r['pool'][:17]:<18}{r['shape_name'][:19]:<20}{r['hold']:>4}"
              f"{str(r['sl'] or '-'):>6}{str(r['tp'] or '-'):>6}"
              f"{tr(r):>9.2f}{alpha(r):>8.2f}{m.get('max_drawdown', 0):>8.2f}"
              f"{m.get('trade_count', 0):>6}{m.get('win_rate', 0):>7.1f}"
              f"{(m.get('profit_factor') or 0):>7.2f}{m.get('avg_hold_days', 0):>7.1f}")

    beat = [r for r in ok if alpha(r) > 0]
    print(f"\n【跑赢基准的（超额>0）：{len(beat)} 组】")
    print(hdr)
    for r in sorted(beat, key=alpha, reverse=True)[:args.top]:
        m = r["metrics"]
        print(f"{r['pool'][:17]:<18}{r['shape_name'][:19]:<20}{r['hold']:>4}"
              f"{str(r['sl'] or '-'):>6}{str(r['tp'] or '-'):>6}"
              f"{tr(r):>9.2f}{alpha(r):>8.2f}{m.get('max_drawdown', 0):>8.2f}"
              f"{m.get('trade_count', 0):>6}{m.get('win_rate', 0):>7.1f}"
              f"{(m.get('profit_factor') or 0):>7.2f}{m.get('avg_hold_days', 0):>7.1f}")

    # 每个池的最好一组（按收益）
    print("\n【每个池的最优组合】")
    print(hdr)
    by_pool = {}
    for r in ok:
        cur = by_pool.get(r["pool_id"])
        if cur is None or tr(r) > tr(cur):
            by_pool[r["pool_id"]] = r
    for r in sorted(by_pool.values(), key=tr, reverse=True):
        m = r["metrics"]
        print(f"{r['pool'][:17]:<18}{r['shape_name'][:19]:<20}{r['hold']:>4}"
              f"{str(r['sl'] or '-'):>6}{str(r['tp'] or '-'):>6}"
              f"{tr(r):>9.2f}{alpha(r):>8.2f}{m.get('max_drawdown', 0):>8.2f}"
              f"{m.get('trade_count', 0):>6}{m.get('win_rate', 0):>7.1f}"
              f"{(m.get('profit_factor') or 0):>7.2f}{m.get('avg_hold_days', 0):>7.1f}")

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"ranges": ranges, "benchmark": bench, "rows": rows}, f,
                  ensure_ascii=False, indent=1, default=str)
    print(f"\n合并结果：{args.out}")


if __name__ == "__main__":
    main()
