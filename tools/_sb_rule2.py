# -*- coding: utf-8 -*-
"""首板研究 · 第 9 步：卖出时点对照 + 可买入率 + 环境交叉。

⭐ 打板（T0 收盘在涨停板上买入）到底该怎么卖？这些条件真的买得到吗？
"""
from __future__ import annotations

import io
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _sb_analyze import load  # noqa: E402

OUT = os.path.join(HERE, "_sb_rule2.json")
BUY_COST = 0.03      # 买入佣金万2.5 ≈ 0.025%
SELL_COST = 0.15     # 卖出滑点千一 + 印花税千一 + 佣金 ≈ 0.175%
COST = BUY_COST + SELL_COST


def main() -> int:
    sb = load()
    base = sb[sb["base"]].copy()
    cnt = base.groupby(["trade_date", "industry"]).size().rename("ind_sb_cnt")
    base = base.merge(cnt, on=["trade_date", "industry"], how="left")
    base["low_vol"] = base["f_vol_vs_prev"] < 1.1
    base["low_turn"] = base["turnover_rate"] < 5
    base["hi_gap"] = base["f_gap"] > 0.03
    base["resonance"] = base["ind_sb_cnt"] >= 7
    base["cyb_up"] = base["idx_cyb_above_ma20"] == 1
    # T0 收盘 → T+n 收盘（含首日跳空）
    for n in (2, 3):
        base[f"t0_to_t{n}"] = (1 + base["y_gap1"]) * (1 + base[f"y_r{n}"]) - 1
    out = {"cost_pct": round(COST, 3)}

    SETS = [
        ("基准：全部首板", pd.Series(True, index=base.index)),
        ("高开>3% + 缩量", base["hi_gap"] & base["low_vol"]),
        ("高开>3% + 低换手<5%", base["hi_gap"] & base["low_turn"]),
        ("缩量 + 低换手", base["low_vol"] & base["low_turn"]),
        ("缩量+低换手+板块共振>=7", base["low_vol"] & base["low_turn"] & base["resonance"]),
        ("缩量+低换手+创业板MA20上", base["low_vol"] & base["low_turn"] & base["cyb_up"]),
        ("缩量+低换手+高开+共振", base["low_vol"] & base["low_turn"] & base["hi_gap"]
         & base["resonance"]),
    ]

    print("=" * 124)
    print("【A】卖出时点对照（T0 收盘在涨停板买入，收益已扣 %.2f%% 成本）" % COST)
    print(f"{'条件':28s} {'n':>6s} {'T+1开盘卖':>10s} {'T+1收盘卖':>10s} "
          f"{'T+2收盘卖':>10s} {'T+3收盘卖':>10s} {'可买子集n':>9s} {'可买子集T+1收':>12s} "
          f"{'T+1一字板%':>10s} {'T+1连板%':>9s}")
    out["sell_timing"] = []
    for lab, mask in SETS:
        d = base[mask]
        if len(d) < 100:
            continue
        row = {"label": lab, "n": int(len(d))}
        vals = []
        for y in ("y_gap1", "y_t1_close", "t0_to_t2", "t0_to_t3"):
            s = d[y].dropna()
            vals.append(round(float(s.mean()) * 100 - COST, 2))
        db_ = d[d["f_open_is_limit"] == 0]
        bv = (round(float(db_["y_t1_close"].mean()) * 100 - COST, 2)
              if len(db_) >= 100 else None)
        yz = round(float((d["y_t1_yizi"] == 1).mean()) * 100, 1)
        lb = round(float(d["y_lianban"].mean()) * 100, 1)
        row.update({"t1_open": vals[0], "t1_close": vals[1], "t2_close": vals[2],
                    "t3_close": vals[3], "t1_yizi_pct": yz, "lianban_pct": lb,
                    "buyable_n": int(len(db_)), "buyable_t1_close": bv})
        out["sell_timing"].append(row)
        bs = "     —    " if bv is None else f"{bv:>+9.2f}%"
        print(f"{lab:28s} {len(d):>6,} {vals[0]:>+9.2f}% {vals[1]:>+9.2f}% "
              f"{vals[2]:>+9.2f}% {vals[3]:>+9.2f}% {len(db_):>9,} {bs:>12s} "
              f"{yz:>9.1f}% {lb:>8.1f}%")

    print("\n" + "=" * 116)
    print("【B】这些条件买得到吗（T0 当天开盘未涨停 ⇒ 能买）")
    print(f"{'条件':28s} {'n':>6s} {'T0可买%':>9s} {'一字板%':>9s} {'开盘板%':>9s} "
          f"{'平均换手':>9s} {'平均量/前日':>11s}")
    out["buyability"] = []
    for lab, mask in SETS:
        d = base[mask]
        if len(d) < 100:
            continue
        can = round(float((d["f_open_is_limit"] == 0).mean()) * 100, 1)
        yz = round(float((d["f_is_yizi"] == 1).mean()) * 100, 1)
        op = round(float(((d["f_open_is_limit"] == 1) & (d["f_is_yizi"] == 0)).mean()) * 100, 1)
        row = {"label": lab, "n": int(len(d)), "can_buy_pct": can, "yizi_pct": yz,
               "open_limit_pct": op,
               "turnover": round(float(d["turnover_rate"].mean()), 2),
               "vol_vs": round(float(d["f_vol_vs_prev"].median()), 3)}
        out["buyability"].append(row)
        print(f"{lab:28s} {len(d):>6,} {can:>8.1f}% {yz:>8.1f}% {op:>8.1f}% "
              f"{row['turnover']:>9.2f} {row['vol_vs']:>11.3f}")

    # ---- C 环境交叉 ----
    print("\n" + "=" * 116)
    print("【C】环境交叉（打板口径：T0 收盘买 → T+1 收盘卖，扣 %.2f%%）" % COST)
    best = base["hi_gap"] & base["low_vol"]
    print(f"{'环境':32s} {'全样本 n':>9s} {'全样本':>9s} "
          f"{'高开+缩量 n':>11s} {'高开+缩量':>10s} {'胜率':>7s}")
    out["env_cross"] = []
    envs = [("大盘上涨日", base["idx_sh"] > 0), ("大盘下跌日", base["idx_sh"] <= 0),
            ("上证 > MA20", base["idx_sh_above_ma20"] == 1),
            ("上证 < MA20", base["idx_sh_above_ma20"] == 0),
            ("创业板 > MA20", base["idx_cyb_above_ma20"] == 1),
            ("创业板 < MA20", base["idx_cyb_above_ma20"] == 0),
            ("全市场中位涨幅 >1%", base["mkt_med_pct"] > 0.01),
            ("全市场中位涨幅 <-1%", base["mkt_med_pct"] < -0.01),
            ("赚钱效应(5日) 高20%", base["emo_zt_ret_ma5"] >= base["emo_zt_ret_ma5"].quantile(0.8)),
            ("赚钱效应(5日) 低20%", base["emo_zt_ret_ma5"] <= base["emo_zt_ret_ma5"].quantile(0.2))]
    for lab, em in envs:
        a = base[em]
        b = base[em & best]
        if len(a) < 100:
            continue
        av = round(float(a["y_t1_close"].mean()) * 100 - COST, 2)
        bv = None if len(b) < 50 else round(float(b["y_t1_close"].mean()) * 100 - COST, 2)
        bw = None if len(b) < 50 else round(float((b["y_t1_close"] > 0).mean()) * 100, 1)
        out["env_cross"].append({"label": lab, "n_all": int(len(a)), "all": av,
                                 "n_best": int(len(b)), "best": bv, "best_win": bw})
        f = lambda v: "     —  " if v is None else f"{v:>+8.2f}%"
        w = "    —  " if bw is None else f"{bw:>6.1f}%"
        print(f"{lab:32s} {len(a):>9,} {f(av):>9s} {len(b):>11,} {f(bv):>10s} {w}")

    # ---- D 最优组合分年 ----
    print("\n" + "=" * 124)
    print(f"【D】「高开大于3% + 缩量」分年分月（T+1 收盘卖，扣 {COST:.2f}%）")
    d = base[best].copy()
    print(f"  n={len(d):,}  合计 {d['y_t1_close'].mean() * 100 - COST:+.2f}%  "
          f"胜率 {(d['y_t1_close'] > 0).mean() * 100:.1f}%")
    yrs = {}
    for y, g in d.groupby("year"):
        v = round(float(g["y_t1_close"].mean()) * 100 - COST, 2)
        yrs[int(y)] = {"n": int(len(g)), "avg": v,
                       "win": round(float((g["y_t1_close"] > 0).mean()) * 100, 1)}
        print(f"    {y}  n={len(g):>5,}  {v:>+7.2f}%  胜率 {yrs[int(y)]['win']:>5.1f}%")
    out["best_by_year"] = yrs
    print("  分月：")
    mons = []
    for mo, g in d.groupby(d["trade_date"].dt.month):
        v = round(float(g["y_t1_close"].mean()) * 100 - COST, 2)
        mons.append({"month": int(mo), "n": int(len(g)), "avg": v})
        print(f"    {mo:>2d}月  n={len(g):>5,}  {v:>+7.2f}%")
    out["best_by_month"] = mons

    with io.open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print(f"\n写出 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
