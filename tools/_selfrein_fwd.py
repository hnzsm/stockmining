# -*- coding: utf-8 -*-
"""为 run 的成交明细取「入场那根及其后 30 根」的 K 线，供离线反事实模拟用。

产出 `tools/_selfrein_fwd.pkl`：list[dict]，每笔：
    ts_code, name, t0, buy_date, pnl_pct(实际), sell_reason,
    ref_close   = T0 收盘（T+1 开盘做买卖决策时已知）
    bars        = [(open, high, low, close), ...]  从 T+1 起连续（T 下标 0 = 买入那根）
    feats       = {特征名: 值}（同 _selfrein_mine.py，供叠加池条件）

⚠ 全部来自 `market.load_panel`（与选股/回测同一套复权口径），
  同一序列内部的比值才是可比的 —— 不要拿它跟 daily_kline 原表直接比。
"""
import sys, os, argparse, pickle
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
import numpy as np
import core.db as db
import core.market as market

F_KEEP = ["pct_chg", "body", "amplitude", "upper_shadow", "lower_shadow", "gap_open",
          "close_pos", "vol", "amount", "turnover_rate", "total_mv", "circ_mv",
          "vol_ratio5", "vr", "ret5", "ret20", "ret60", "ret120",
          "pos20", "pos60", "pos120", "boll_pos", "boll_width", "atr_pct", "ma_bull",
          "rsi6", "rsi12", "cci14", "wr14", "kdj_d", "macd_hist", "dmi_adx",
          "mf_main_ratio", "mf_main_ratio5", "close", "ma5", "ma20", "ma60",
          "hh20", "hh60", "hh120", "ll20", "ll60", "ll120", "limit_up", "limit_down"]
F_PREV = ["pct_chg", "body", "amplitude", "upper_shadow", "lower_shadow", "gap_open",
          "vol", "close", "high", "low", "ret20", "pos120", "limit_up"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=int, required=True)
    ap.add_argument("--out", default="tools/_selfrein_fwd.pkl")
    ap.add_argument("--fwd", type=int, default=30)
    ap.add_argument("--pad", type=int, default=140)
    args = ap.parse_args()

    trades = db.query(
        "SELECT ts_code,name,buy_date,buy_price,sell_date,sell_price,pnl_pct,hold_days,sell_reason "
        "FROM backtest_trade WHERE run_id=%s ORDER BY buy_date, ts_code", (args.run,))
    dates = market.get_trade_dates("2022-01-01", "2026-12-31")
    didx = {d: i for i, d in enumerate(dates)}

    by_code = {}
    for t in trades:
        by_code.setdefault(t["ts_code"], []).append(t)

    out = []
    for n, (code, ts) in enumerate(sorted(by_code.items()), 1):
        buys = sorted(str(t["buy_date"]) for t in ts)
        lo_i = min(didx.get(b, 0) for b in buys)
        hi_i = max(didx.get(b, 0) for b in buys)
        start = dates[max(0, lo_i - args.pad)]
        end = dates[min(len(dates) - 1, hi_i + args.fwd + 5)]
        try:
            panel = market.load_panel([code], start, end, freq="daily", adjust="qfq")
        except Exception as e:
            print("   !! ", code, e)
            continue
        df = panel.get(code)
        if df is None or df.empty:
            continue
        df = df.reset_index(drop=True)
        dstr = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
        pos = {v: i for i, v in enumerate(dstr)}
        for t in ts:
            b = str(t["buy_date"])
            i = pos.get(b)
            if i is None or i < 1:
                continue
            t0 = i - 1
            bars = []
            for j in range(i, min(len(df), i + args.fwd + 1)):
                bars.append((float(df.at[j, "open"]), float(df.at[j, "high"]),
                             float(df.at[j, "low"]), float(df.at[j, "close"])))
            feats = {}
            for f in F_KEEP:
                feats["f_" + f] = float(df.at[t0, f]) if f in df.columns else np.nan
            for f in F_PREV:
                feats["p1_" + f] = float(df.at[t0 - 1, f]) if f in df.columns else np.nan
            feats["x_vol_vs_prev"] = (feats["f_vol"] / feats["p1_vol"]) if feats.get("p1_vol") else np.nan
            out.append({"ts_code": code, "name": t["name"], "t0": dstr[t0], "buy_date": b,
                        "ref_close": float(df.at[t0, "close"]),
                        "pnl_pct": float(t["pnl_pct"] or 0), "sell_reason": t["sell_reason"],
                        "bars": bars, "feats": feats})
        if n % 200 == 0:
            print("   ... %d 只" % n)

    pickle.dump(out, open(args.out, "wb"))
    print("写出 %s  笔数=%d  (平均 %d 根前向 K 线)" % (
        args.out, len(out), int(np.mean([len(x["bars"]) for x in out]) if out else 0)))


if __name__ == "__main__":
    main()
