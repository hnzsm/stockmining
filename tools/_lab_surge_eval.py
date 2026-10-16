# -*- coding: utf-8 -*-
"""评估若干「暴涨前夜」候选条件集：命中数 / 真涨概率 / 期望收益，训练与验证分开看。

关键指标（都在**随机票-日基线**上算，避免循环论证）：
- 每天几只 = 基线命中率 × 每日候选股票数
- 真涨概率 = 命中的基线票-日里，未来 10 日最高收盘涨幅 >= 50% 的比例（precision）
- 提升 = precision / 全市场自然发生率
- 期望 = 命中的基线票-日，未来 10 日**收盘**涨幅的中位数（等于「买了拿 10 天」）

用法：python tools/_lab_surge_eval.py [--split 2026-01-01]
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

UNIVERSE = 4700
GAIN = 0.50

# 候选条件集：每条是 (列名, "<="|">=", 阈值)。列名用 samples.csv 里的列，
# 落地成平台表达式时按 COL2EXPR 翻译。
CANDIDATES = {
    "A·强势股急跌洗盘": [
        ("ret60", ">=", 0.30), ("c_ma60", ">=", 0.10),
        ("pct_chg", "<=", -3.0), ("amplitude", ">=", 7.0),
    ],
    "A2·强势股急跌(收紧)": [
        ("ret60", ">=", 0.30), ("c_ma60", ">=", 0.10),
        ("pct_chg", "<=", -3.8), ("amplitude", ">=", 8.5),
        ("turnover_rate", ">=", 5.0),
    ],
    "B·高波动+近期涨停过": [
        ("atr_pct", ">=", 6.0), ("rng5", ">=", 0.15),
        ("lim20", ">=", 1), ("turnover_rate", ">=", 5.0),
    ],
    "C·中期强+高换手+大跌": [
        ("ret60", ">=", 0.33), ("pct_chg", "<=", -2.6),
        ("turnover_rate", ">=", 8.0), ("atr_pct", ">=", 6.0),
    ],
    "D·趋势中回踩(温)": [
        ("ret120", ">=", 0.40), ("c_ma60", ">=", 0.15),
        ("pct_chg", "<=", -2.0), ("vol_ratio5", ">=", 0.8),
    ],
    "E·贪心3条": [
        ("pct_chg", "<=", -3.8), ("c_ma60", ">=", 0.24),
        ("amplitude", ">=", 8.5),
    ],
    "F·贪心4条": [
        ("pct_chg", "<=", -3.8), ("c_ma60", ">=", 0.24),
        ("amplitude", ">=", 8.5), ("atr_pct", ">=", 6.85),
    ],
    "G·强势急跌+涨停基因": [
        ("ret60", ">=", 0.30), ("pct_chg", "<=", -3.0),
        ("lim20", ">=", 1), ("amplitude", ">=", 7.0),
        ("turnover_rate", ">=", 5.0),
    ],
    "H·强趋势缩量回踩不破MA20": [
        ("ret60", ">=", 0.30), ("pct_chg", "<=", -2.0),
        ("vol_ratio5", "<=", 0.9), ("c_ma20", ">=", -0.02),
    ],
    "I·强趋势回踩+高波动": [
        ("ret60", ">=", 0.30), ("c_ma20", ">=", -0.02),
        ("amplitude", ">=", 6.0), ("atr_pct", ">=", 5.0),
    ],
    "J·D+缩量回调": [
        ("ret120", ">=", 0.40), ("c_ma60", ">=", 0.15),
        ("pct_chg", "<=", -2.0), ("vol_ratio5", "<=", 1.0),
    ],
    "K·强趋势+高波动(不限当日涨跌)": [
        ("ret60", ">=", 0.33), ("atr_pct", ">=", 6.0),
        ("pos60", ">=", 0.5), ("turnover_rate", ">=", 5.0),
    ],
    "L·H+涨停基因": [
        ("ret60", ">=", 0.30), ("pct_chg", "<=", -2.0),
        ("vol_ratio5", "<=", 0.9), ("c_ma20", ">=", -0.02),
        ("lim20", ">=", 1),
    ],
    "D2·D+换手>=5": [
        ("ret120", ">=", 0.40), ("c_ma60", ">=", 0.15),
        ("pct_chg", "<=", -2.0), ("vol_ratio5", "<=", 1.0),
        ("turnover_rate", ">=", 5.0),
    ],
    "D3·D+跌幅>=3.5": [
        ("ret120", ">=", 0.40), ("c_ma60", ">=", 0.15),
        ("pct_chg", "<=", -3.5), ("vol_ratio5", "<=", 1.0),
    ],
    "D4·D+近20日涨停过": [
        ("ret120", ">=", 0.40), ("c_ma60", ">=", 0.15),
        ("pct_chg", "<=", -2.0), ("vol_ratio5", "<=", 1.0),
        ("lim20", ">=", 1),
    ],
    "D5·D+流动性过滤": [
        ("ret120", ">=", 0.40), ("c_ma60", ">=", 0.15),
        ("pct_chg", "<=", -2.0), ("vol_ratio5", "<=", 1.0),
        ("amount", ">=", 100000), ("circ_mv", ">=", 200000),
    ],
}


def hit(df: pd.DataFrame, conds) -> pd.Series:
    m = pd.Series(True, index=df.index)
    for c, op, t in conds:
        v = pd.to_numeric(df[c], errors="coerce")
        m &= (v <= t) if op == "<=" else (v >= t)
    return m.fillna(False)


def evaluate(df: pd.DataFrame, conds, name: str, tag: str) -> dict:
    pos = df[df.is_surge == 1]
    base = df[df.is_surge == 0]
    rate = (base["fwd_gain10"] >= GAIN).mean()          # 自然发生率
    mb = hit(base, conds)
    mp = hit(pos, conds)
    nb = int(mb.sum())
    if nb < 8:
        return {"方案": name, "区间": tag, "每天几只": np.nan, "真涨概率": np.nan,
                "提升": np.nan, "召回": mp.mean() * 100, "命中样本数": nb,
                "期望10日": np.nan, "中位回撤": np.nan}
    g = base.loc[mb, "fwd_gain10"]
    return {"方案": name, "区间": tag,
            "每天几只": round(mb.mean() * UNIVERSE, 1),
            "真涨概率": round((g >= GAIN).mean() * 100, 2),
            "提升": round((g >= GAIN).mean() / rate, 1),
            "召回": round(mp.mean() * 100, 1),
            "命中样本数": nb,
            "期望10日": round(base.loc[mb, "fwd_ret10"].median() * 100, 2),
            "期望5日": round(base.loc[mb, "fwd_ret5"].median() * 100, 2),
            "中位回撤": round(base.loc[mb, "fwd_dd10"].median() * 100, 1)}


def baseline_row(df: pd.DataFrame, tag: str) -> dict:
    base = df[df.is_surge == 0]
    return {"方案": "（全市场对照）", "区间": tag, "每天几只": UNIVERSE,
            "真涨概率": round((base["fwd_gain10"] >= GAIN).mean() * 100, 2),
            "提升": 1.0, "召回": 100.0, "命中样本数": len(base),
            "期望10日": round(base["fwd_ret10"].median() * 100, 2),
            "期望5日": round(base["fwd_ret5"].median() * 100, 2),
            "中位回撤": round(base["fwd_dd10"].median() * 100, 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", default="tools/_lab_surge_samples.csv")
    ap.add_argument("--split", default="2026-01-01")
    ap.add_argument("--out", default="tools/_lab_surge_eval.md")
    args = ap.parse_args()

    df = pd.read_csv(args.samples)
    df["trade_date"] = df["trade_date"].astype(str)
    base = df[df.is_surge == 0]
    print(f"观测 {len(df)}  事件 {(df.is_surge==1).sum()}  基线 {len(base)}")
    print(f"全市场自然发生率（10 日内涨 50%）= "
          f"{(base['fwd_gain10'] >= GAIN).mean()*100:.3f}%")

    rows = []
    splits = [("训练 <" + args.split, df[df.trade_date < args.split]),
              ("验证 >=" + args.split, df[df.trade_date >= args.split]),
              ("全区间", df)]
    for tag, sub in splits:
        rows.append(baseline_row(sub, tag))
        for name, conds in CANDIDATES.items():
            rows.append(evaluate(sub, conds, name, tag))
    res = pd.DataFrame(rows)
    pd.set_option("display.width", 200)
    for tag, _sub in splits:
        print(f"\n===== {tag} =====")
        print(res[res.区间 == tag].drop(columns=["区间"]).to_string(index=False))
    res.to_csv("tools/_lab_surge_eval.csv", index=False, encoding="utf-8-sig")
    lines = ["# 候选条件集评估\n",
             f"- 样本：事件 2275（2025-01 起 10 日内涨 ≥50%，已剔除首板一字板）、"
             f"随机票-日基线 {len(base)}",
             f"- 全市场自然发生率：{(base['fwd_gain10'] >= GAIN).mean()*100:.3f}%",
             "- 「真涨概率」= 命中的票里真在 10 日内涨 50% 的比例；"
             "「期望10日」= 买了拿满 10 天的收盘涨幅**中位数**\n"]
    for tag, _sub in splits:
        blk = res[res.区间 == tag].drop(columns=["区间"])
        lines.append(f"\n## {tag}\n")
        lines.append("| " + " | ".join(blk.columns) + " |")
        lines.append("|" + "---|" * len(blk.columns))
        for _, r in blk.iterrows():
            lines.append("| " + " | ".join("" if pd.isna(v) else str(v)
                                           for v in r.values) + " |")
    open(args.out, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("\n已写", args.out)


if __name__ == "__main__":
    main()
