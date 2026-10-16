# -*- coding: utf-8 -*-
"""首板研究 · 第 5 步：整体画像 + 单变量判别力。

产物 tools/_sb_overview.json + 终端表格。
结果变量（都是「T0 首板」之后的）：
  · y_gap1      次日开盘跳空（打板客隔夜溢价）
  · y_t1_close  T0 收盘（涨停价）→ T+1 收盘   ← 主口径「这个板打得好不好」
  · y_r1        T+1 开盘买 → T+1 收盘（可执行、持有 1 日）
  · y_r3        T+1 开盘买 → T+3 收盘
  · y_lianban   次日是否再涨停（晋级）
"""
from __future__ import annotations

import io
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

SAMPLES = os.path.join(HERE, "_sb_samples.pkl")
MARKET = os.path.join(HERE, "_sb_market.pkl")
OUT = os.path.join(HERE, "_sb_overview.json")

FEATS = [
    "f_gap", "f_body", "f_amp", "f_up_shadow", "f_low_shadow", "f_close_pos",
    "f_is_yizi", "f_open_is_limit", "f_closed_at_limit",
    "f_vol_vs_prev", "f_vol_ratio5", "f_vol_ratio10", "f_vol_ratio20",
    "f_ma5_dev", "f_ma20_dev", "f_ma60_dev", "f_ma_bull",
    "f_pos20", "f_pos60", "f_gap_to_hh60", "f_gap_to_hh120",
    "f_pre_ret5", "f_pre_ret10", "f_pre_ret20", "f_pre_ret60",
    "f_pre_lu10", "f_pre_lu20", "f_pre_lu60",
    "f_prev_pct", "f_prev_body", "f_prev_gap", "f_prev_vol_vs",
    "f_atr_pct", "f_std20", "f_kdj_k", "f_kdj_d", "f_kdj_j", "f_rsi6",
    "f_boll_pos", "f_boll_width",
    "total_mv", "circ_mv", "close", "turnover_rate", "volume_ratio", "amount",
]
YS = ["y_gap1", "y_t1_close", "y_r1", "y_r3", "y_lianban"]


def auc(x: pd.Series, y: pd.Series) -> float:
    """Mann-Whitney AUC：y 为 0/1。"""
    ok = x.notna() & y.notna()
    x, y = x[ok], y[ok]
    n1 = int((y == 1).sum())
    n0 = int((y == 0).sum())
    if n1 == 0 or n0 == 0:
        return np.nan
    r = x.rank()
    return float((r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def sp(x: pd.Series, y: pd.Series) -> float:
    ok = x.notna() & y.notna()
    if ok.sum() < 30:
        return np.nan
    return float(x[ok].rank().corr(y[ok].rank()))


def load() -> pd.DataFrame:
    sb = pd.read_pickle(SAMPLES)
    mk = pd.read_pickle(MARKET)
    mk["trade_date"] = pd.to_datetime(mk["trade_date"])
    sb = sb.merge(mk, on="trade_date", how="left")
    sb["is_bj"] = sb["ts_code"].str.endswith(".BJ")
    sb["base"] = ((sb["is_st"] == 0) & (~sb["is_bj"]) & (sb["list_days"] >= 60))
    sb["in_pool"] = (sb["base"] & (sb["total_mv"] < 5000000)
                     & (sb["close"] < 30) & (sb["turnover_rate"] >= 3))
    sb["year"] = sb["trade_date"].dt.year
    sb["train"] = sb["trade_date"] < "2025-01-01"
    for c in FEATS:
        sb[c] = pd.to_numeric(sb[c], errors="coerce")
    return sb


def describe(df: pd.DataFrame, label: str) -> dict:
    r = {"label": label, "n": int(len(df))}
    for y in YS:
        s = df[y].dropna()
        r[y] = {"mean": round(float(s.mean()) * 100, 3),
                "median": round(float(s.median()) * 100, 3),
                "win": round(float((s > 0).mean()) * 100, 1),
                "n": int(len(s))}
    return r


def main() -> int:
    sb = load()
    out = {"universe": {}, "by_year": [], "auc": {}, "quantile": {}}

    print("=" * 100)
    print("【0】总体")
    print(f"原始首板 {len(sb):,} 笔   base(剔ST/BJ/次新) {int(sb['base'].sum()):,}   "
          f"池内(+市值/价/换手) {int(sb['in_pool'].sum()):,}")
    for lab, d in (("全部首板", sb), ("base", sb[sb["base"]]), ("池内", sb[sb["in_pool"]])):
        r = describe(d, lab)
        out["universe"][lab] = r
        print(f"  {lab:8s} n={r['n']:>6,}  " + "  ".join(
            f"{y}: 均{r[y]['mean']:+.2f}% 胜率{r[y]['win']:.1f}%"
            for y in ("y_gap1", "y_t1_close", "y_r1")))

    pool = sb[sb["in_pool"]].copy()
    print("\n" + "=" * 100)
    print("【1】分年（池内）")
    print(f"{'年份':6s} {'n':>6s} {'次日开盘':>9s} {'T1收盘':>9s} {'T1胜率':>8s} "
          f"{'持有1日':>9s} {'持有3日':>9s} {'连板率':>8s}")
    for y, d in pool.groupby("year"):
        row = {"year": int(y), "n": int(len(d))}
        for c in YS:
            row[c] = round(float(d[c].mean()) * 100, 3)
        row["y_r1_win"] = round(float((d["y_r1"] > 0).mean()) * 100, 1)
        out["by_year"].append(row)
        print(f"{y:<6d} {len(d):>6,} {row['y_gap1']:>8.2f}% {row['y_t1_close']:>8.2f}% "
              f"{row['y_r1_win']:>7.1f}% {row['y_r1']:>8.2f}% {row['y_r3']:>8.2f}% "
              f"{row['y_lianban'] * 100:>7.1f}%")

    print("\n" + "=" * 100)
    print("【2】单变量判别力（池内，|AUC-0.5| 排序）")
    print("  列 = 对 y_t1_close>0 / y_lianban 的 AUC，以及对 y_t1_close 的 Spearman")
    rows = []
    good = pool[pool["y_ok"] == 1]
    ybin = (good["y_t1_close"] > 0).astype(int)
    for f in FEATS:
        a1 = auc(good[f], ybin)
        a2 = auc(good[f], good["y_lianban"])
        s1 = sp(good[f], good["y_t1_close"])
        rows.append({"f": f, "auc_up": a1, "auc_lb": a2, "spearman": s1,
                     "score": max(abs((a1 or 0.5) - 0.5), abs((a2 or 0.5) - 0.5))})
    rows.sort(key=lambda r: -r["score"])
    print(f"  {'特征':18s} {'AUC(涨)':>8s} {'AUC(连板)':>10s} {'Spearman':>9s}")
    for r in rows[:20]:
        print(f"  {r['f']:18s} {r['auc_up']:>8.4f} {r['auc_lb']:>10.4f} "
              f"{r['spearman']:>9.4f}")
    out["auc"] = rows

    print("\n" + "=" * 100)
    print("【3】关键特征五分位（池内，k=5）—— 看单调性")
    for f in ("f_gap", "f_vol_vs_prev", "f_close_pos", "f_pre_ret20", "f_pos60",
              "f_gap_to_hh120", "f_pre_lu60", "total_mv", "turnover_rate"):
        if f not in pool.columns:
            continue
        d = pool[pool[f].notna() & pool["y_t1_close"].notna()].copy()
        try:
            d["q"] = pd.qcut(d[f], 5, labels=False, duplicates="drop")
        except Exception:
            continue
        g = d.groupby("q").agg(n=("y_t1_close", "size"),
                               v=("y_t1_close", "mean"),
                               w=("y_t1_close", lambda s: (s > 0).mean()),
                               lb=("y_lianban", "mean"),
                               lo=(f, "min"), hi=(f, "max"))
        out["quantile"][f] = [
            {"n": int(r.n), "avg": round(r.v * 100, 3), "win": round(r.w * 100, 1),
             "lianban": round(r.lb * 100, 1), "lo": round(r.lo, 4), "hi": round(r.hi, 4)}
            for r in g.itertuples()]
        print(f"\n  ── {f}")
        for i, r in enumerate(g.itertuples()):
            print(f"     Q{i + 1} [{r.lo:>10.3f},{r.hi:>10.3f}]  n={int(r.n):>5,}  "
                  f"T1均 {r.v * 100:+7.2f}%  胜率 {r.w * 100:5.1f}%  连板 {r.lb * 100:5.1f}%")

    with io.open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print(f"\n写出 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
