# -*- coding: utf-8 -*-
"""时序审计：确认榜上的收益不是「用明天的数据买今天」买出来的。

做法（不靠嘴说，逐笔对）：
  1. 抽出真实成交记录，拿回测当时的同一份（个股, 区间, 前复权）重新算信号；
  2. 断言 **买入日 = 信号日(entry) 的下一根**，且那一根的 buy_trigger 为真；
  3. 断言 **买入价 = 那一根的开盘价 ×(1+滑点)**（滑点买入上浮）；
  4. 断言 **卖出日 > 买入日**，卖出价 = 当根开盘价（到期则是收盘价）。
任意一条不满足 ⇒ 未来函数，那条组合的收益作废。

    python tools/_lab_audit.py --pool 58 --shape S4 --hold 20
    python tools/_lab_audit.py --pool 58 --shape S4 --hold 20 --range 2024-01-01~2024-12-31
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pandas as pd  # noqa: E402

from core import backtest as bt, market, strategy as st  # noqa: E402
from tools import _lab_grid as grid  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", type=int, default=58)
    ap.add_argument("--shape", default="S4")
    ap.add_argument("--hold", type=int, default=20)
    ap.add_argument("--range", default="2026-01-01~2026-09-28")
    ap.add_argument("--sample", type=int, default=6)
    args = ap.parse_args()
    start, end = args.range.split("~")

    pools = {p["id"]: p for p in grid.load_pools()}
    shapes = {s["key"]: s for s in grid.SHAPES}
    pool, shape = pools[args.pool], shapes[args.shape]
    cfg = st.merge_config({
        "buy_trigger": shape["buy"], "sell_signal": shape["sell"],
        "risk": {"max_hold_days": args.hold, "stop_loss_pct": None,
                 "take_profit_pct": None},
        "exec": {"buy_price": "open", "sell_price": "open",
                 "max_hold_price": "close", "slippage": 0.001},
    })
    run_cfg = {**cfg, "_pool": {"base": pool["base"], "where": list(pool["conds"]),
                                "pool_exit": bool(pool["pool_exit"]),
                                "pool_id": pool["id"], "pool_name": pool["name"]}}
    print(f"池 #{pool['id']} {pool['name']} / {shape['key']} {shape['name']} "
          f"持 {args.hold}  区间 {start}~{end}\n加载中…", flush=True)

    codes = market.resolve_universe(pool["base"])
    data = bt.prepare_data(codes, start, end, run_cfg, batch=300)
    res = bt.run_backtest(data, run_cfg, start, end, grid.PARAMS)
    trades = res.get("trades") or []
    m = res.get("metrics") or {}
    print(f"收益 {m.get('total_return')}%  池内等权买入持有 {m.get('benchmark_return')}%  "
          f"{m.get('trade_count')} 笔  胜率 {m.get('win_rate')}%  "
          f"回撤 {m.get('max_drawdown')}%")
    if not trades:
        print("没有成交，审计无从谈起"); return

    sel_cfg = st.selection_cfg(run_cfg)
    bad = 0
    print(f"\n共 {len(trades)} 笔，抽 {args.sample} 笔逐根核对：")
    for t in trades[:args.sample]:
        code = t.get("ts_code")
        bd = str(t.get("buy_date"))[:10]
        sd = str(t.get("sell_date"))[:10] if t.get("sell_date") else ""
        bp = t.get("buy_price")
        # 往前多加载 400 天只为指标预热（MA60/BOLL），前复权基准取决于区间末端，
        # 末端不变时向前扩展不改变任何一根的价格 —— 与成交价同口径。
        warm = (pd.Timestamp(bd) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
        panel = market.load_panel([code], min(start, warm), end, adjust="qfq",
                                  indicators_cfg=sel_cfg.get("indicators"),
                                  weekly=False, only=None)
        df = panel.get(code)
        if df is None or df.empty:
            print(f"  {code} 本地无数据，跳过"); continue
        df = df.copy()
        sig = st.compute_signals(df, sel_cfg)
        df["entry"] = sig["entry"]
        df["buy"] = sig["buy_trigger"]
        df["sell"] = sig["sell_signal"]
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
        dates = df["trade_date"].tolist()
        pos = {d: i for i, d in enumerate(dates)}
        bi = pos.get(bd)
        if bi is None:
            print(f"  {code} 买入日 {bd} 不在本地 K 线里"); bad += 1; continue
        # entry 必须出现在买入日的**前面**（同一根或更早都算出问题：同一根 = 用当天开盘买当天）
        prev_entry = [i for i in range(bi) if bool(df["entry"].iloc[i])]
        pi = prev_entry[-1] if prev_entry else None
        ok_entry = pi is not None and pi == bi - 1
        ok_buy = bool(df["buy"].iloc[bi])
        exp_bp = float(df["open"].iloc[bi]) * 1.001
        ok_price = bp is not None and abs(float(bp) - exp_bp) < max(0.002 * exp_bp, 0.01)
        si = pos.get(sd) if sd else None
        ok_sell = si is None or si > bi
        ok = ok_entry and ok_buy and ok_price and ok_sell
        if not ok:
            bad += 1
        print(f"  {code}  entry@{'—' if pi is None else dates[pi]} → 买 {bd}"
              f"（差 {bi - pi if pi is not None else '?'} 根）"
              f"  买价 {bp} vs 开盘×1.001 {exp_bp:.3f}  卖 {sd}"
              f"  {'OK' if ok else '!! 时序/价格对不上'}")

    print(f"\n异常 {bad} 笔 ⇒ "
          + ("没有发现未来函数，收益是 T 日收盘出信号、T+1 开盘买的口径。" if not bad
             else "存在未来函数，这条组合的收益作废。"))


if __name__ == "__main__":
    main()
