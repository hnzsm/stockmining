# -*- coding: utf-8 -*-
"""离线反事实模拟：同一批成交信号，换不同的「入场过滤 + 离场规则」看单笔期望怎么动。

严格照 `core/backtest.py` 的口径：
  * 买入价 = open[T+1] × (1+滑点)；卖出价 = 成交基准价 × (1−滑点)
  * 手续费 max(额×佣金率, 最低5) ；卖出另加印花税
  * 离场顺序 = 开盘(open 类) → 盘中(止损/止盈，跳空则以开盘成交) → 收盘(到期)
  * **买入当根不卖**（引擎的日循环里「先卖后买」）
  * 止损触发价 = 买价×(1+sl%)，成交 min(触发价, 当日开盘)；止盈 = 买价×(1+tp%)，成交 max(触发价, 当日开盘)

⚠ 这是**单笔口径**的近似：不含 max_positions / 现金约束 / 并发抢位。它只用来**排序**，
  最终结论必须回到真引擎（平台回测）复算。
"""
import sys, os, argparse, pickle, itertools, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import pandas as pd

SLIP = 0.001
COMM = 0.00025
STAMP = 0.0005
MIN_COMM = 5.0
POS_VALUE = 200000.0          # 名义单笔金额（只影响「最低 5 元佣金」那一项）


def sim_one(bars, sl=None, tp=None, hold=None, max_hold_price="close",
            sell_price="close", slip=SLIP, entry_gap_pct=None, ref_close=None):
    """返回 (pnl_pct, reason, hold_bars)。

    bars[0] = 买入那根（T+1）；bars[j] = 其后第 j 根。
    entry_gap_pct: 入场过滤（T+1 开盘相对 T0 收盘的跳空，%），不满足返 None（=不参与）。
    """
    op0 = bars[0][0]
    if entry_gap_pct is not None and ref_close:
        gap = (op0 / ref_close - 1) * 100
        lo, hi = entry_gap_pct
        if gap < lo or gap > hi:
            return None
    buy_px = op0 * (1 + slip)
    shares = POS_VALUE / buy_px
    gross_buy = buy_px * shares
    cost = gross_buy + max(gross_buy * COMM, MIN_COMM)

    exit_px, reason = None, None
    n = len(bars)
    for j in range(1, n):
        op, hi_p, lo_p, cl = bars[j]
        expire_now = bool(hold and j >= int(hold))
        # --- 第 1 段：开盘类离场 ---
        if sell_price == "open" or max_hold_price != "close":
            if expire_now and max_hold_price != "close":
                exit_px, reason = op, "max_hold"
        # --- 第 2 段：盘中（止损优先于止盈）---
        if exit_px is None and sl is not None:
            trig = buy_px * (1 + float(sl) / 100.0)
            if not np.isnan(lo_p) and lo_p <= trig:
                exit_px = min(trig, op) if not np.isnan(op) else trig
                reason = "stop_loss"
        if exit_px is None and tp is not None:
            trig = buy_px * (1 + float(tp) / 100.0)
            if not np.isnan(hi_p) and hi_p >= trig:
                exit_px = max(trig, op) if not np.isnan(op) else trig
                reason = "take_profit"
        # --- 第 3 段：收盘（到期）---
        if exit_px is None and expire_now and max_hold_price == "close":
            exit_px, reason = cl, "max_hold"
        if exit_px is not None:
            break
    if exit_px is None:
        exit_px, reason, j = bars[n - 1][3], "end", n - 1

    sell_px = exit_px * (1 - slip)
    gross = sell_px * shares
    fee = max(gross * COMM, MIN_COMM) + gross * STAMP
    pnl = (gross - fee) - cost
    return pnl / cost * 100, reason, j


def run(data, sl=None, tp=None, hold=None, max_hold_price="close",
        sell_price="close", gap=None, pool_extra=None, split=None):
    rows = []
    for d in data:
        if split and not (d["t0"] < split):
            continue
        if pool_extra and not pool_extra(d["feats"]):
            continue
        r = sim_one(d["bars"], sl=sl, tp=tp, hold=hold, max_hold_price=max_hold_price,
                    sell_price=sell_price, entry_gap_pct=gap, ref_close=d["ref_close"])
        if r is None:
            continue
        rows.append({"t0": d["t0"], "pnl": r[0], "reason": r[1], "gb": r[2], "code": d["ts_code"]})
    if not rows:
        return None
    df = pd.DataFrame(rows)
    return dict(n=len(df), win=round((df.pnl > 0).mean() * 100, 1),
                mean=round(df.pnl.mean(), 3), med=round(df.pnl.median(), 3),
                tot=round(df.pnl.sum(), 0), gb=round(df.gb.mean(), 2),
                sl=round((df.reason == "stop_loss").mean() * 100, 1),
                tp=round((df.reason == "take_profit").mean() * 100, 1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pkl", default="tools/_selfrein_fwd.pkl")
    ap.add_argument("--split", default="2025-01-01")
    ap.add_argument("--out", default="tools/_selfrein_sim.json")
    args = ap.parse_args()
    data = pickle.load(open(args.pkl, "rb"))
    print("载入 %d 笔" % len(data))

    GAPS = {"无过滤": None, "高开": (0, 999), "低开": (-999, 0),
            "高开>=3%": (3, 999), "低开<=-3%": (-999, -3), "低开<=-6%": (-999, -6),
            "平开±2%": (-2, 2)}
    RISK = [dict(sl=-2, tp=5, hold=1),                     # 原样
            dict(sl=None, tp=None, hold=1),
            dict(sl=None, tp=5, hold=1),
            dict(sl=-5, tp=None, hold=3),
            dict(sl=None, tp=None, hold=3),
            dict(sl=None, tp=None, hold=5),
            dict(sl=-8, tp=None, hold=5),
            dict(sl=None, tp=None, hold=10),
            dict(sl=-8, tp=None, hold=10),
            dict(sl=None, tp=None, hold=20),
            dict(sl=-8, tp=None, hold=20),
            dict(sl=None, tp=15, hold=10),
            dict(sl=-8, tp=15, hold=10),
            dict(sl=None, tp=30, hold=20),
            dict(sl=None, tp=None, hold=5, sell_price="open"),
            ]
    rows = []
    for gname, gap in GAPS.items():
        for rk in RISK:
            tag = "gap=%s sl=%s tp=%s hold=%s%s" % (
                gname, rk.get("sl"), rk.get("tp"), rk.get("hold"),
                " sell=open" if rk.get("sell_price") else "")
            for split, lab in ((None, "全"), (args.split, "TR"),):
                pass
            a = run(data, gap=gap, split=args.split, **rk)
            b = run(data, gap=gap, **rk)
            if a and b:
                rows.append(dict(gap=gname, sl=rk.get("sl"), tp=rk.get("tp"),
                                 hold=rk.get("hold"), sell=rk.get("sell_price", "close"),
                                 tr_n=a["n"], tr_win=a["win"], tr_mean=a["mean"],
                                 va_n=b["n"] - a["n"], va_mean=round(
                                     (b["tot"] - a["tot"]) / max(1, b["n"] - a["n"]), 3),
                                 all_n=b["n"], all_mean=b["mean"], all_win=b["win"],
                                 all_sl=b["sl"], all_tp=b["tp"], all_gb=b["gb"]))
    R = pd.DataFrame(rows)
    R.to_json(args.out, orient="records", force_ascii=False, indent=1)
    pd.set_option("display.width", 250)
    print("\n===== 按「全样本单笔均值」排（前 25）=====")
    print(R.sort_values("all_mean", ascending=False).head(25).to_string(
        index=False, float_format=lambda v: "%.3f" % v))
    print("\n===== 原样附近（对照）=====")
    print(R[(R.sl == -2) & (R.tp == 5) & (R.hold == 1)].to_string(
        index=False, float_format=lambda v: "%.3f" % v))
    print("\n→ 写出", args.out)


if __name__ == "__main__":
    main()
