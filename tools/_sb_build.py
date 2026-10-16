# -*- coding: utf-8 -*-
"""首板研究 · 第 3 步：构造「首板」样本 + T0 特征 + 前向收益 + 涨停股明细。

口径（全部对齐项目）：
  · 涨停 = (close/pre_close - 1)*100 >= limit_pct*100 - 0.15   （indicators._limit_pct）
  · 首板 = 今日涨停 且 前两日都不涨停
  · 复权：库里没有 adj_factor，用 `pct_chg` 链式累计还原前复权曲线
         k_t = cum_t / close_t（cum_t = cum_{t-1}*(1+pct_chg_t/100)）
  · 前向收益：**T+1 开盘买入**（引擎口径），持有 N 日后收盘卖出

产物：
  tools/_sb_stocks.pkl   —— 全部涨停股一行（含次日表现）→ 算市场情绪/晋级率
  tools/_sb_samples.pkl  —— 首板样本（含全部 T0 特征）
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from core.indicators import _limit_pct  # noqa: E402

RAW = os.path.join(HERE, "_sb_raw.pkl")
OUT_Z = os.path.join(HERE, "_sb_stocks.pkl")
OUT_S = os.path.join(HERE, "_sb_samples.pkl")

BATCH = 800


def prep_one(sub: pd.DataFrame) -> pd.DataFrame:
    sub = sub.sort_values("trade_date").reset_index(drop=True)
    sub = sub[sub["close"].notna() & (sub["close"] > 0)].copy()
    if len(sub) < 5:
        return sub.iloc[0:0]

    code = sub["ts_code"].iloc[0]
    name = sub["name"].iloc[0]
    lim = float(_limit_pct(code, name=name))

    # ---- 前复权缩放因子 ----
    pr = (1.0 + sub["pct_chg"].fillna(0.0) / 100.0).cumprod()
    sub["k"] = pr / sub["close"]
    for col in ("open", "high", "low", "close", "pre_close"):
        sub["a_" + col] = sub[col] * sub["k"]

    # ---- 涨跌停 / 连板 ----
    up = (sub["close"] / sub["pre_close"] - 1.0) * 100.0
    sub["limit_up"] = (up >= lim * 100 - 0.15).astype("int8")
    sub["limit_down"] = (up <= -lim * 100 + 0.15).astype("int8")
    s = sub["limit_up"]
    grp = (s != s.shift()).cumsum()
    sub["lu_streak"] = (s.groupby(grp).cumsum() * s).astype("int16")

    sub["prev_lu1"] = s.shift(1).fillna(0).astype("int8")
    sub["prev_lu2"] = s.shift(2).fillna(0).astype("int8")
    sub["is_sb"] = ((s == 1) & (sub["prev_lu1"] == 0) & (sub["prev_lu2"] == 0)).astype("int8")

    # ---- T0 特征（只用当日及之前）----
    c, o, h, l, pc = sub["a_close"], sub["a_open"], sub["a_high"], sub["a_low"], sub["a_pre_close"]
    sub["f_gap"] = o / pc - 1.0
    sub["f_body"] = (c - o) / pc
    sub["f_amp"] = (h - l) / pc
    sub["f_up_shadow"] = (h - c) / pc
    sub["f_low_shadow"] = (o - l) / pc
    sub["f_close_pos"] = (c - l) / (h - l).replace(0, np.nan)
    sub["f_is_yizi"] = ((h - l) / pc <= 0.001).astype("int8")
    sub["f_open_is_limit"] = (o >= pc * (1 + lim) - pc * 0.002).astype("int8")
    sub["f_closed_at_limit"] = (h <= c * 1.0005).astype("int8")

    v = sub["vol"]
    sub["f_vol_vs_prev"] = v / v.shift(1).replace(0, np.nan)
    for n in (5, 10, 20):
        vma = v.rolling(n, min_periods=max(2, n // 2)).mean()
        sub[f"f_vol_ratio{n}"] = v / vma.replace(0, np.nan)

    ma5 = c.rolling(5, min_periods=3).mean()
    ma10 = c.rolling(10, min_periods=5).mean()
    ma20 = c.rolling(20, min_periods=10).mean()
    ma60 = c.rolling(60, min_periods=30).mean()
    sub["f_ma5_dev"] = c / ma5 - 1.0
    sub["f_ma20_dev"] = c / ma20 - 1.0
    sub["f_ma60_dev"] = c / ma60 - 1.0
    sub["f_ma_bull"] = ((ma5 > ma10) & (ma10 > ma20)).astype("int8")

    hh20 = h.rolling(20, min_periods=10).max()
    ll20 = l.rolling(20, min_periods=10).min()
    hh60 = h.rolling(60, min_periods=30).max()
    ll60 = l.rolling(60, min_periods=30).min()
    hh120 = h.rolling(120, min_periods=60).max()
    sub["f_pos20"] = (c - ll20) / (hh20 - ll20).replace(0, np.nan)
    sub["f_pos60"] = (c - ll60) / (hh60 - ll60).replace(0, np.nan)
    sub["f_gap_to_hh60"] = c / hh60 - 1.0
    sub["f_gap_to_hh120"] = c / hh120 - 1.0

    for n in (5, 10, 20, 60):
        sub[f"f_pre_ret{n}"] = c.shift(1) / c.shift(1 + n) - 1.0
    for n in (10, 20, 60):
        sub[f"f_pre_lu{n}"] = sub["limit_up"].shift(1).rolling(
            n, min_periods=n).sum()

    sub["f_prev_pct"] = sub["pct_chg"].shift(1)
    sub["f_prev_body"] = sub["f_body"].shift(1)
    sub["f_prev_gap"] = sub["f_gap"].shift(1)
    sub["f_prev_vol_vs"] = sub["f_vol_vs_prev"].shift(1)

    pc_s = c.shift(1)
    tr = pd.concat([h - l, (h - pc_s).abs(), (l - pc_s).abs()], axis=1).max(axis=1)
    sub["f_atr_pct"] = tr.rolling(14, min_periods=7).mean() / c
    sub["f_std20"] = c.pct_change().rolling(20, min_periods=10).std()

    low_n = l.rolling(9, min_periods=5).min()
    high_n = h.rolling(9, min_periods=5).max()
    rsv = ((c - low_n) / (high_n - low_n).replace(0, np.nan) * 100).fillna(50)
    kk = rsv.ewm(alpha=1 / 3, adjust=False).mean()
    dd = kk.ewm(alpha=1 / 3, adjust=False).mean()
    sub["f_kdj_k"] = kk
    sub["f_kdj_d"] = dd
    sub["f_kdj_j"] = 3 * kk - 2 * dd

    d1 = c.diff()
    au = d1.clip(lower=0).ewm(alpha=1 / 6, adjust=False).mean()
    ad = (-d1).clip(lower=0).ewm(alpha=1 / 6, adjust=False).mean()
    sub["f_rsi6"] = (100 - 100 / (1 + au / ad.replace(0, np.nan))).fillna(50)

    mid = c.rolling(20, min_periods=20).mean()
    sd = c.rolling(20, min_periods=20).std()
    sub["f_boll_pos"] = (c - (mid - 2 * sd)) / ((4 * sd).replace(0, np.nan))
    sub["f_boll_width"] = (4 * sd) / mid.replace(0, np.nan)

    # ---- 前向（结果）----
    nc = c.shift(-1)
    no = o.shift(-1)
    nh, nl = h.shift(-1), l.shift(-1)
    sub["y_gap1"] = no / c - 1.0
    sub["y_t1_close"] = nc / c - 1.0
    sub["y_t1_high"] = nh / c - 1.0
    sub["y_t1_low"] = nl / c - 1.0
    sub["y_lianban"] = sub["limit_up"].shift(-1).fillna(0).astype("int8")
    sub["y_t1_yizi"] = (((nh - nl) / nc.replace(0, np.nan)) <= 0.001).astype("int8")
    sub["y_t1_open_is_limit"] = (no >= c * (1 + lim) - c * 0.002).astype("int8")

    sub["y_r1"] = nc / no - 1.0
    for n in (2, 3, 5, 10):
        sub[f"y_r{n}"] = c.shift(-n) / no - 1.0
    fh = pd.concat([h.shift(-k) for k in range(1, 6)], axis=1)
    fl = pd.concat([l.shift(-k) for k in range(1, 6)], axis=1)
    sub["y_mfe5"] = fh.max(axis=1) / no - 1.0
    sub["y_mae5"] = fl.min(axis=1) / no - 1.0
    sub["y_ok"] = (no.notna() & c.shift(-10).notna()).astype("int8")
    return sub


def main() -> int:
    t0 = time.time()
    raw = pd.read_pickle(RAW)
    print(f"读入 {len(raw):,} 行 {time.time() - t0:.1f}s", flush=True)

    keep_lu, keep_sb = [], []
    codes = sorted(raw["ts_code"].unique())
    n_lu = n_sb = 0
    for i in range(0, len(codes), BATCH):
        part = set(codes[i:i + BATCH])
        sub_all = raw[raw["ts_code"].isin(part)]
        outs = []
        for _, g in sub_all.groupby("ts_code", sort=False):
            d = prep_one(g)
            if len(d):
                outs.append(d)
        if not outs:
            continue
        d = pd.concat(outs, ignore_index=True)
        z = d[d["limit_up"] == 1]
        keep_lu.append(z)
        keep_sb.append(z[z["is_sb"] == 1])
        n_lu += len(z)
        n_sb += int((z["is_sb"] == 1).sum())
        del d, outs, sub_all
        print(f"  {i + BATCH:5d}/{len(codes)}  涨停 {n_lu:>7,}  首板 {n_sb:>6,}  "
              f"{time.time() - t0:6.1f}s", flush=True)

    lu = pd.concat(keep_lu, ignore_index=True)
    sb = pd.concat(keep_sb, ignore_index=True)
    lu.to_pickle(OUT_Z)
    sb.to_pickle(OUT_S)
    print(f"\n涨停股行 {len(lu):,} → {OUT_Z}")
    print(f"首板样本 {len(sb):,} → {OUT_S}")
    print(f"日期范围 {sb['trade_date'].min().date()} ~ {sb['trade_date'].max().date()}")
    print(f"涉及股票 {sb['ts_code'].nunique():,}  耗时 {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
