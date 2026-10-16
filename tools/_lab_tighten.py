# -*- coding: utf-8 -*-
"""选股条件**收紧**实验：让池子每天选出的股更少，同时看收益有没有变坏。

背景：用户希望「选股策略选出的股相对少一些」。收紧有两条路 —— 改原条件阈值、
或在后面追加过滤条件。本脚本只走**第二条（纯加法）**：原条件一字不动，
追加的过滤条件独立可命名、可撤销，也不会把某条原有语义搅浑。

口径：
  * 命中数 = 逐日统计 `entry` 为真的股票只数（就是选股页结果表的行数），
    除以区间交易天数得**日均**；另给中位/p90/最大/空仓天数，防「日均」被极端日带偏。
  * 收益 = 同一买卖形状（默认 S4 高开买·跌破MA10走）× 持有 10 / 20 两档，
    滑点千一、满仓 10 档，与阶段一榜上同一口径，可直接比。
  * ⚠ 变体改变了 `where` ⇒ 缓存键变 ⇒ **每格都要重算** prepare（~135s）。
    T0（原样）与阶段一 S4 同键，应命中缓存，且收益必须与榜上一致（天然校验）。

    python tools/_lab_tighten.py --pools 3,57,58 --variants T0,T1,T6 --shard 1/1
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import backtest as bt, market, strategy as st  # noqa: E402
from tools import _lab_grid as grid  # noqa: E402

# 追加型过滤条件（只做加法）。⚠ 单位：amount 千元、circ_mv/total_mv 万元、
#                                turnover_rate %、pct_chg %、close 元。
VARIANTS = [
    ("T0", "原样（基线）", []),
    ("T1", "成交额>2亿", ["amount > 200000"]),
    ("T2", "流通市值>30亿", ["circ_mv > 300000"]),
    ("T3", "换手>=1.5%", ["turnover_rate >= 1.5"]),
    ("T4", "涨幅<5%（不追高）", ["pct_chg < 5"]),
    ("T5", "股价>5元", ["close > 5"]),
    ("T6", "成交额>2亿 + 流通>30亿", ["amount > 200000", "circ_mv > 300000"]),
    ("T7", "成交额>2亿 + 换手>=1.5%", ["amount > 200000", "turnover_rate >= 1.5"]),
    ("T8", "成交额>5亿 + 流通>50亿 + 换手>=2%",
     ["amount > 500000", "circ_mv > 500000", "turnover_rate >= 2"]),
]
VKEYS = [v[0] for v in VARIANTS]


def trade_days(start, end):
    try:
        return len(market.trade_dates(start, end))
    except Exception:  # noqa: BLE001
        return 0


def hit_stats(data, ndays):
    """逐日统计 entry 命中只数。data: {code: {"entry": bool[], "dates": [...]}}"""
    cnt: dict[str, int] = {}
    for _code, d in data.items():
        e = np.asarray(d.get("entry"), dtype=bool)
        if e is None or not e.any():
            continue
        for dt in np.asarray(d["dates"])[e]:
            dt = str(dt)
            cnt[dt] = cnt.get(dt, 0) + 1
    vals = sorted(cnt.values())
    n = max(ndays, len(cnt)) or 1
    if not vals:
        return {"avg": 0.0, "med": 0, "p90": 0, "max": 0,
                "days": 0, "empty": n, "total": 0}
    return {
        "avg": round(sum(vals) / n, 2),
        "med": int(vals[len(vals) // 2]),
        "p90": int(vals[int(len(vals) * 0.9)]),
        "max": int(vals[-1]),
        "days": len(vals),          # 有命中的交易日
        "empty": n - len(vals),     # 一只都没选出的交易日
        "total": sum(vals),
    }


def run_cell(pool, conds, start, end, holds, shape_key, slip=0.001):
    shapes = {s["key"]: s for s in grid.SHAPES}
    shape = shapes[shape_key]
    cfg = st.merge_config({
        "buy_trigger": shape["buy"],
        "sell_signal": shape["sell"],
        "risk": {"max_hold_days": holds[0], "stop_loss_pct": None,
                 "take_profit_pct": None},
        "exec": {"buy_price": "open", "sell_price": "open",
                 "max_hold_price": "close", "slippage": slip},
    })
    run_cfg = {**cfg, "_pool": {"base": pool["base"], "where": list(conds),
                                "pool_exit": bool(pool["pool_exit"]),
                                "pool_id": pool["id"], "pool_name": pool["name"]}}
    errs = st.validate_config(run_cfg)
    if errs:
        return {"error": "；".join(errs)}

    t0 = time.time()
    codes = market.resolve_universe(pool["base"])
    ci: dict = {}
    data = bt.prepare_data(codes, start, end, run_cfg, batch=300, cache_info=ci)
    prep = time.time() - t0
    out = {"prep_s": round(prep, 1), "cache": ci.get("cache"),
           **hit_stats(data, trade_days(start, end))}
    for hold in holds:
        rc = dict(run_cfg)
        rc["risk"] = {**(run_cfg.get("risk") or {}), "max_hold_days": hold}
        res = bt.run_backtest(data, rc, start, end, grid.PARAMS)
        m = res.get("metrics") or {}
        out[f"h{hold}"] = {"ret": m.get("total_return"), "trades": m.get("trade_count"),
                           "win": m.get("win_rate"), "dd": m.get("max_drawdown"),
                           "bench": m.get("benchmark_return")}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pools", default="")            # 空 = 全部
    ap.add_argument("--variants", default=",".join(VKEYS))
    ap.add_argument("--shape", default="S4")
    ap.add_argument("--holds", default="10,20")
    ap.add_argument("--range", default="2025-01-01~2025-12-31")
    ap.add_argument("--shard", default="1/1")
    ap.add_argument("--out", default="")
    ap.add_argument("--cache", action="store_true",
                    help="允许写/读磁盘回测缓存（默认**关**：目录 3000MB 已逼近上限，"
                         "再写会触发淘汰删文件，而删文件被环境删除保护拦下、进程直接死）")
    args = ap.parse_args()
    if not args.cache:
        from core import btcache
        btcache.ENABLED = False   # 只影响本进程；网页回测照旧用缓存
    start, end = args.range.split("~")
    holds = [int(x) for x in args.holds.split(",") if x.strip()]

    pools = grid.load_pools()
    if args.pools:
        want = {int(x) for x in args.pools.split(",") if x.strip()}
        pools = [p for p in pools if p["id"] in want]
    vs = [v for v in VARIANTS if v[0] in
          {k.strip().upper() for k in args.variants.split(",")}]

    idx, total = (int(x) for x in args.shard.split("/"))
    cells = [(p, v) for p in pools for v in vs]
    mine = [c for i, c in enumerate(cells) if i % total == idx - 1]

    rows = []
    for p, (vkey, vname, extra) in mine:
        conds = list(p["conds"]) + list(extra)
        r = run_cell(p, conds, start, end, holds, args.shape)
        if r.get("error"):
            print(f"  !! 池#{p['id']} {p['name']} / {vkey} 失败：{r['error']}", flush=True)
            continue
        row = {"pool_id": p["id"], "pool": p["name"], "var": vkey, "var_name": vname,
               "extra": extra, "range": args.range, "shape": args.shape, **r}
        rows.append(row)
        h10 = r.get("h10") or {}
        h20 = r.get("h20") or {}
        print(f"  #{p['id']:<4}{p['name'][:14]:<16}{vkey} {vname[:16]:<18}"
              f"日均{r['avg']:>7.1f} 中位{r['med']:>5} p90{r['p90']:>5} "
              f"空仓{r['empty']:>4}天 | 持10 {h10.get('ret') or 0:>7.2f}%/"
              f"{h10.get('trades') or 0:>5}笔  持20 {h20.get('ret') or 0:>7.2f}%/"
              f"{h20.get('trades') or 0:>5}笔 [{r['prep_s']}s {r['cache']}]", flush=True)

    out = args.out or os.path.join(ROOT, "tools", f"_lab_tight_{idx}of{total}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    print(f"写出 {len(rows)} 行 -> {out}")


if __name__ == "__main__":
    main()
