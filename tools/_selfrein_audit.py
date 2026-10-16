# -*- coding: utf-8 -*-
"""时序审计（自我强化版）：把**平台记录里真实落库的逐笔**，对着同一份行情重算信号，
逐笔确认没有「用明天的数据买今天」。

与 `_lab_audit.py` 的区别：这里**不重跑回测**，只读 `backtest_trade`（用户能在记录页
点开看到的那份），所以审的就是用户实际看到的东西。

断言（任意一条不满足 ⇒ 未来函数，收益作废）：
  1. `buy_date` 必须是某个 `entry`(=池条件命中) 信号日的**后一根**（差 1 根，不能同根、不能更晚）；
  2. `buy_trigger`（低开≤-2%）在 `buy_date` 那根上为真；
  3. `buy_price ≈ open(buy_date) × (1+滑点)`（比值法，见 C36①：拿记录价除原始开盘，
     同票应恒定；直接比 daily_kline.open 会因前复权缩放报假警报）；
  4. `sell_date` 严格晚于 `buy_date`（T 日买的当根不卖）。

    python tools/_selfrein_audit.py --run 160 --sample 8
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import pandas as pd  # noqa: E402

from core import backtest as bt, db, market, strategy as st  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=int, required=True)
    ap.add_argument("--slip", type=float, default=0.001)
    ap.add_argument("--sample", type=int, default=8)
    ap.add_argument("--all", action="store_true", help="审全部逐笔（默认抽前 N 笔）")
    args = ap.parse_args()

    run = db.query_one("SELECT * FROM backtest_run WHERE id=%s", (args.run,))
    if not run:
        print("找不到 run", args.run)
        return 1
    sid = run.get("strategy_id")
    s = st.get_strategy(int(sid))
    cfg = st.merge_config(s["config"])
    spec = run.get("universe_spec") or {}
    if isinstance(spec, (str, bytes)):      # JSON 列：驱动可能给 str、也可能已解好
        import json
        try:
            spec = json.loads(spec)
        except (ValueError, TypeError):
            spec = {}
    start = str(run.get("start_date"))[:10]
    end = str(run.get("end_date"))[:10]
    cfgrun = {**cfg, "_pool": {"base": spec.get("base") or "all",
                              "where": list(spec.get("where") or []),
                              "pool_exit": bool(spec.get("pool_exit"))}}
    print(f"run #{args.run} {run.get('name')}")
    print(f"  策略 #{sid} {s['name']}  exec={cfg['exec']}  risk={cfg['risk']}")
    print(f"  池 {spec.get('base')} 条件 {spec.get('where')}")
    print(f"  区间 {start}~{end}  滑点 {args.slip}\n")

    total = db.query_one("SELECT COUNT(*) c FROM backtest_trade WHERE run_id=%s", (args.run,))
    n_all = int(total["c"] if total else 0)
    lim = "" if args.all else f" LIMIT {args.sample}"
    trades = db.query(
        "SELECT ts_code, name, buy_date, buy_price, sell_date, sell_price, sell_reason, hold_days, pnl_pct "
        f"FROM backtest_trade WHERE run_id=%s ORDER BY buy_date, ts_code{lim}", (args.run,))
    print(f"逐笔共 {n_all} 条，本次审 {len(trades)} 条\n")

    bad = 0
    for t in trades:
        code = t["ts_code"]
        bd = str(t["buy_date"])[:10]
        sd = str(t["sell_date"])[:10] if t["sell_date"] else ""
        bp = float(t["buy_price"])
        warm = (pd.Timestamp(bd) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
        panel = market.load_panel([code], min(start, warm), end, adjust="qfq",
                                  indicators_cfg=cfg.get("indicators"), weekly=False, only=None)
        df = panel.get(code)
        if df is None or df.empty:
            print(f"  {code} 本地无数据，跳过"); continue
        df = df.copy()
        sig = st.compute_signals(df, cfgrun)
        df["entry"] = sig["entry"]
        df["buy"] = sig["buy_trigger"]
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
        dates = df["trade_date"].tolist()
        pos = {d: i for i, d in enumerate(dates)}
        bi = pos.get(bd)
        if bi is None:
            print(f"  {code} 买入日 {bd} 不在本地 K 线里"); bad += 1; continue
        prev_entry = [i for i in range(bi) if bool(df["entry"].iloc[i])]
        pi = prev_entry[-1] if prev_entry else None
        ok_entry = pi is not None and pi == bi - 1
        ok_buy = bool(df["buy"].iloc[bi])
        raw_open = float(df["open"].iloc[bi])
        exp_bp = raw_open * (1 + args.slip)
        ok_price = abs(bp - exp_bp) < max(0.002 * exp_bp, 0.01)
        si = pos.get(sd) if sd else None
        ok_sell = si is None or si > bi
        ok = ok_entry and ok_buy and ok_price and ok_sell
        if not ok:
            bad += 1
        print(f"  {code} {t['name']}  entry@{'—' if pi is None else dates[pi]} → 买 {bd}"
              f"（差 {bi - pi if pi is not None else '?'} 根）买价 {bp:.4f} vs 开盘×{(1+args.slip):.3f}"
              f" {exp_bp:.4f}  trigger={'T' if ok_buy else 'F'}  卖 {sd} ({t['sell_reason']})"
              f"  {'OK' if ok else '!! 对不上'}")

    print(f"\n异常 {bad} 笔 ⇒ " + ("没有发现未来函数：T 日收盘出信号、T+1 开盘按 buy_trigger 买、"
                                   "下一根才卖。" if not bad else "存在未来函数，收益作废。"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
