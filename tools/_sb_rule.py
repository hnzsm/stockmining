# -*- coding: utf-8 -*-
"""首板研究 · 第 8 步：候选条件 / 组合的样本内外 + 分年复核 + 成本敏感性。

⭐ 定稿这一步 —— 只有「TRAIN 和 VALID 都为正、且分年不靠单一年份」的条件才留下。
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

OUT = os.path.join(HERE, "_sb_rule.json")
COST = 0.20          # 单边来回成本近似（滑点千一 + 佣金/印花），单位 %
YEARS = [2023, 2024, 2025, 2026]


def m(d: pd.DataFrame, y: str = "y_t1_close"):
    s = d[y].dropna()
    return (None, None) if not len(s) else (
        round(float(s.mean()) * 100 - COST, 2), round(float((s > 0).mean()) * 100, 1))


def main() -> int:
    sb = load()
    base = sb[sb["base"]].copy()
    cnt = base.groupby(["trade_date", "industry"]).size().rename("ind_sb_cnt")
    base = base.merge(cnt, on=["trade_date", "industry"], how="left")
    base["low_vol"] = base["f_vol_vs_prev"] < 1.1
    base["low_turn"] = base["turnover_rate"] < 5
    base["very_low_turn"] = base["turnover_rate"] < 3
    base["hi_gap"] = base["f_gap"] > 0.03
    base["resonance"] = base["ind_sb_cnt"] >= 7
    base["deep_low"] = base["f_gap_to_hh120"] < -0.25
    base["cyb_up"] = base["idx_cyb_above_ma20"] == 1
    base["tr"] = base["train"]

    RULES = [
        ("① 缩量（量/前日 < 1.1）", base["low_vol"]),
        ("① 极缩量（< 0.8）", base["f_vol_vs_prev"] < 0.8),
        ("② 低换手（< 5%）", base["low_turn"]),
        ("② 超低换手（< 3%）", base["very_low_turn"]),
        ("③ 高开涨停（开盘 > +3%）", base["hi_gap"]),
        ("③ 高开 5~9%", base["f_gap"].between(0.05, 0.09)),
        ("③ 高开 > 9%", base["f_gap"] > 0.09),
        ("④ 板块共振（同行业首板>=7）", base["resonance"]),
        ("④ 板块共振>=4", base["ind_sb_cnt"] >= 4),
        ("⑤ 创业板在 MA20 上", base["cyb_up"]),
        ("⑥ 深跌位（距120日高 < -25%）", base["deep_low"]),
        ("⑦ 高开涨停 + 缩量", base["hi_gap"] & base["low_vol"]),
        ("⑦ 缩量 + 低换手", base["low_vol"] & base["low_turn"]),
        ("⑦ 缩量 + 板块共振", base["low_vol"] & base["resonance"]),
        ("⑦ 高开 + 低换手", base["hi_gap"] & base["low_turn"]),
        ("⑦ 缩量+低换手+板块共振", base["low_vol"] & base["low_turn"] & base["resonance"]),
        ("⑦ 缩量+低换手+创业板MA20上", base["low_vol"] & base["low_turn"] & base["cyb_up"]),
        ("⑦ 全部（缩量+低换手+高开共振）",
         base["low_vol"] & base["low_turn"] & base["hi_gap"] & base["resonance"]),
        ("— 基准：全部首板", pd.Series(True, index=base.index)),
    ]

    print("=" * 118)
    print("【打板口径】T0 收盘（涨停价）买入 → T+1 收盘卖出   收益已扣 %.2f%% 成本" % COST)
    print(f"{'条件':30s} {'n':>7s} {'扣费后':>8s} {'胜率':>7s} {'连板率':>7s} "
          f"{'TRAIN n':>8s} {'TRAIN':>8s} {'VALID n':>8s} {'VALID':>8s} "
          f"{'23':>7s} {'24':>7s} {'25':>7s} {'26':>7s}")
    out = {"cost_pct": COST, "rules": []}
    for lab, mask in RULES:
        d = base[mask]
        if len(d) < 100:
            continue
        tr, va = d[d["tr"]], d[~d["tr"]]
        avg, win = m(d)
        lt = round(float(d["y_lianban"].mean()) * 100, 1)
        tra, _ = m(tr)
        vaa, _ = m(va)
        yrs = {}
        for y in YEARS:
            dd = d[d["year"] == y]
            yrs[y] = (None if len(dd) < 30 else
                      round(float(dd["y_t1_close"].mean()) * 100 - COST, 2))
        row = {"label": lab, "n": int(len(d)), "avg": avg, "win": win, "lianban": lt,
               "train_n": int(len(tr)), "train": tra, "valid_n": int(len(va)), "valid": vaa,
               "years": yrs}
        out["rules"].append(row)
        f = lambda v: ("    —  " if v is None else f"{v:>+7.2f}")
        print(f"{lab:30s} {len(d):>7,} {f(avg):>8s} {win:>6.1f}% {lt:>6.1f}% "
              f"{len(tr):>8,} {f(tra):>8s} {len(va):>8,} {f(vaa):>8s} "
              f"{f(yrs[2023]):>7s} {f(yrs[2024]):>7s} {f(yrs[2025]):>7s} {f(yrs[2026]):>7s}")

    # ---- 次日开盘买入口径（对照）----
    ex = base[base["y_t1_open_is_limit"] == 0].copy()
    print("\n" + "=" * 118)
    print("【次日开盘买口径】T+1 开盘买入 → T+1 收盘卖出（T+1 开盘涨停的买不到，已剔除）")
    print(f"{'条件':30s} {'n':>7s} {'扣费后':>8s} {'胜率':>7s} "
          f"{'TRAIN n':>8s} {'TRAIN':>8s} {'VALID n':>8s} {'VALID':>8s}")
    out["open_buy"] = []
    RULES2 = [
        ("深跌位（距120日高<-25%）", ex["deep_low"]),
        ("前20日跌幅>5%", ex["f_pre_ret20"] < -0.05),
        ("低开（开盘<0）", ex["f_gap"] < 0),
        ("低开 + 深跌位", (ex["f_gap"] < 0) & ex["deep_low"]),
        ("低开 + 换手<5%", (ex["f_gap"] < 0) & ex["low_turn"]),
        ("低开 + 缩量", (ex["f_gap"] < 0) & ex["low_vol"]),
        ("低开+深跌+缩量", (ex["f_gap"] < 0) & ex["deep_low"] & ex["low_vol"]),
        ("小市值20亿内", ex["total_mv"] < 200000),
        ("— 基准：可买入全样本", pd.Series(True, index=ex.index)),
    ]
    for lab, mask in RULES2:
        d = ex[mask]
        if len(d) < 100:
            continue
        tr, va = d[d["tr"]], d[~d["tr"]]
        avg, win = m(d, "y_r1")
        tra, _ = m(tr, "y_r1")
        vaa, _ = m(va, "y_r1")
        yrs = {y: (None if len(d[d["year"] == y]) < 30 else
                   round(float(d[d["year"] == y]["y_r1"].mean()) * 100 - COST, 2))
               for y in YEARS}
        out["open_buy"].append({"label": lab, "n": int(len(d)), "avg": avg, "win": win,
                                "train": tra, "valid": vaa, "train_n": int(len(tr)),
                                "valid_n": int(len(va)), "years": yrs})
        f = lambda v: ("    —  " if v is None else f"{v:>+7.2f}")
        print(f"{lab:30s} {len(d):>7,} {f(avg):>8s} {win:>6.1f}% "
              f"{len(tr):>8,} {f(tra):>8s} {len(va):>8,} {f(vaa):>8s}")

    with io.open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print(f"\n写出 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
