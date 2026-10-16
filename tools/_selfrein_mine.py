# -*- coding: utf-8 -*-
"""从 run 的回测明细里挖「赢家 vs 输家」的入场形态差异。

用法：
    python tools/_selfrein_mine.py --run 151 [--out tools/_selfrein_feats.csv]

口径：
  * 每一笔的 buy_date = 信号日 T0 的**下一根**（T0 收盘出信号、T0+1 开盘买）。
  * 所有特征只在 **T0 及之前** 取值（不用 T0+1 的任何数据，防未来函数）。
  * 特征取自 `market.load_panel`（与选股同源：复权/指标/资金流同一套口径）。
"""
import sys, os, argparse, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
import numpy as np
import core.db as db
import core.market as market

# T0 那根（信号日）上直接能拿到的特征
F_T0 = [
    "pct_chg", "body", "amplitude", "upper_shadow", "lower_shadow", "gap_open",
    "close_pos", "is_up", "is_down", "limit_up", "limit_down",
    "vol", "amount", "turnover_rate", "volume_ratio",
    "total_mv", "circ_mv", "avg_price",
    "close", "pre_close",
    "vol_ratio5", "vol_ratio10", "vr",
    "ret1", "ret5", "ret20", "ret60", "ret120",
    "pos20", "pos60", "pos120", "boll_pos", "boll_width", "boll_dev",
    "atr_pct", "ma_bull",
    "rsi6", "rsi12", "rsi24", "cci14", "wr14", "mtmpct12",
    "kdj_k", "kdj_d", "kdj_j", "macd_hist",
    "dmi_adx", "dmi_pdi", "dmi_mdi",
    "mf_main_ratio", "mf_net_ratio", "mf_big_ratio", "mf_main_ratio5", "mf_in5",
    "ma5", "ma10", "ma20", "ma60", "hh20", "ll20", "hh60", "ll60", "hh120", "ll120",
]

DERIVED = {
    "c_ma5":      lambda d: d.close / d.ma5 - 1,
    "c_ma10":     lambda d: d.close / d.ma10 - 1,
    "c_ma20":     lambda d: d.close / d.ma20 - 1,
    "c_ma60":     lambda d: d.close / d.ma60 - 1,
    "d_hh20":     lambda d: d.close / d.hh20 - 1,
    "d_hh60":     lambda d: d.close / d.hh60 - 1,
    "d_hh120":    lambda d: d.close / d.hh120 - 1,
    "d_ll120":    lambda d: d.close / d.ll120 - 1,
    "atr_ratio":  lambda d: d.atr_pct / (d.close * 0 + 1) if False else d.atr_pct,
    "ma5_ma20":   lambda d: d.ma5 / d.ma20 - 1,
    "ma20_ma60":  lambda d: d.ma20 / d.ma60 - 1,
}

# 前一日（涨停日）与「涨停日之前」的特征：用 ref 语义逐个取
F_PREV = [
    "pct_chg", "body", "amplitude", "upper_shadow", "lower_shadow", "gap_open",
    "close_pos", "vol", "amount", "turnover_rate", "vol_ratio5", "limit_up",
    "ret20", "ret60", "pos20", "pos60", "pos120", "ma_bull", "atr_pct",
    "hh20", "hh60", "boll_pos", "close", "high", "low",
]


def load_trades(run_id):
    return db.query(
        "SELECT ts_code, name, buy_date, buy_price, sell_date, sell_price, "
        "pnl_pct, hold_days, sell_reason FROM backtest_trade WHERE run_id=%s ORDER BY buy_date, ts_code",
        (run_id,),
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=int, required=True)
    ap.add_argument("--out", default="tools/_selfrein_feats.csv")
    ap.add_argument("--pad", type=int, default=140, help="T0 之前多取多少个自然日（算 ref 用）")
    args = ap.parse_args()

    trades = load_trades(args.run)
    print("run=%s 明细 %d 笔，票数 %d" % (args.run, len(trades), len(set(t["ts_code"] for t in trades))))

    dates = market.get_trade_dates("2022-01-01", "2026-12-31")
    didx = {d: i for i, d in enumerate(dates)}

    # 按票分组，一只票只 load 一次
    by_code = {}
    for t in trades:
        by_code.setdefault(t["ts_code"], []).append(t)

    rows = []
    miss = 0
    for n, (code, ts) in enumerate(sorted(by_code.items()), 1):
        buys = sorted(str(t["buy_date"]) for t in ts)
        lo_i = min(didx.get(b, 0) for b in buys)
        hi_i = max(didx.get(b, 0) for b in buys)
        start = dates[max(0, lo_i - args.pad)]
        end = dates[min(len(dates) - 1, hi_i + 3)]
        try:
            panel = market.load_panel([code], start, end, freq="daily", adjust="qfq")
        except Exception as e:
            print("   !! load_panel 失败", code, e)
            miss += len(ts)
            continue
        df = panel.get(code)
        if df is None or df.empty:
            miss += len(ts)
            continue
        df = df.reset_index(drop=True)
        # ⚠ 面板里 trade_date 是 Timestamp，str() 会带 " 00:00:00" —— 必须统一成日期串再匹配
        dstr = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
        pos = {v: i for i, v in enumerate(dstr)}
        for t in ts:
            b = str(t["buy_date"])
            i = pos.get(b)
            if i is None or i < 1:
                miss += 1
                continue
            t0 = i - 1                      # 信号日 = 买入日的前一根
            r = {"ts_code": code, "name": t["name"], "t0": dstr[t0],
                 "buy_date": b, "pnl_pct": float(t["pnl_pct"] or 0),
                 "hold_days": t["hold_days"], "sell_reason": t["sell_reason"]}
            for f in F_T0:
                r["f_" + f] = df.at[t0, f] if f in df.columns else np.nan
            for f in F_PREV:
                r["p1_" + f] = df.at[t0 - 1, f] if (f in df.columns and t0 >= 1) else np.nan
            for f in ("pct_chg", "body", "amplitude", "upper_shadow", "lower_shadow"):
                r["p2_" + f] = df.at[t0 - 2, f] if (f in df.columns and t0 >= 2) else np.nan
            # 派生
            r["x_vol_vs_prev"] = (r["f_vol"] / r["p1_vol"]) if r["p1_vol"] else np.nan
            r["x_body_1d"] = r["f_body"] - r["p1_body"]
            r["x_is_yizi_prev"] = 1 if (r["p1_high"] == r["p1_low"]) else 0
            r["x_ret5_before_prev"] = df.at[t0 - 2, "ret5"] if t0 >= 2 else np.nan
            r["x_ret20_at_prev"] = r["p1_ret20"]
            rows.append(r)
        if n % 200 == 0:
            print("   ... 已处理 %d 只票" % n)

    out = pd.DataFrame(rows)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    out.to_csv(args.out, index=False, encoding="utf-8-sig")
    print("写出 %s   行=%d 列=%d  (缺 %d 笔)" % (args.out, len(out), out.shape[1], miss))
    print("胜率 %.1f%%  均值 %.3f%%  中位 %.3f%%" % (
        (out.pnl_pct > 0).mean() * 100, out.pnl_pct.mean(), out.pnl_pct.median()))


if __name__ == "__main__":
    main()
