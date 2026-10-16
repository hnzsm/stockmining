# -*- coding: utf-8 -*-
"""首板研究 · 第 4 步：市场环境表（情绪温度 + 大盘位置）。

只读。产物 tools/_sb_market.pkl —— 每个交易日一行。

情绪指标全部只用「T0 日收盘即可知」的信息：
  · zt_cnt / lianban_cnt / max_streak / sb_cnt  —— T0 当日收盘统计
  · emo_zt_ret / emo_zt_rate                    —— **昨日**涨停股在 T0 的平均涨幅 / 再涨停率
  · emo_sb_ret / emo_sb_rate                    —— **昨日**首板股在 T0 的平均涨幅 / 晋级率
  · mkt_med_pct / up_cnt / dn_cnt               —— 全市场宽度（中位涨幅最实在）
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

RAW = os.path.join(HERE, "_sb_raw.pkl")
STOCKS = os.path.join(HERE, "_sb_stocks.pkl")
INDEX = os.path.join(HERE, "_sb_index.pkl")
OUT = os.path.join(HERE, "_sb_market.pkl")


def main() -> int:
    t0 = time.time()

    # ---- 全市场宽度 ----
    raw = pd.read_pickle(RAW)
    raw = raw[["trade_date", "pct_chg", "amount"]]
    g = raw.groupby("trade_date")["pct_chg"]
    mkt = pd.DataFrame({
        "mkt_med_pct": g.median(),
        "mkt_mean_pct": g.mean(),
        "up_cnt": raw[raw["pct_chg"] > 0].groupby("trade_date").size(),
        "dn_cnt": raw[raw["pct_chg"] < 0].groupby("trade_date").size(),
        "amt_total": raw.groupby("trade_date")["amount"].sum(),
    }).fillna(0.0)
    del raw
    print(f"全市场宽度 {len(mkt)} 天 {time.time() - t0:.1f}s", flush=True)

    # ---- 涨停 / 连板 统计 ----
    z = pd.read_pickle(STOCKS)
    zz = z.groupby("trade_date")
    lu = pd.DataFrame({
        "zt_cnt": zz.size(),
        "lianban_cnt": z[z["lu_streak"] >= 2].groupby("trade_date").size(),
        "zhaban_cnt": z[z["lu_streak"] >= 3].groupby("trade_date").size(),
        "max_streak": zz["lu_streak"].max(),
        "sb_cnt": z[z["is_sb"] == 1].groupby("trade_date").size(),
    }).fillna(0.0)

    # 昨日涨停股 / 昨日首板股 —— 在「今天」的表现（今天收盘后可知）
    nxt = z.groupby("trade_date").agg(
        zt_ret=("y_t1_close", "mean"),
        zt_rate=("y_lianban", "mean"),
        zt_gap=("y_gap1", "mean"),
    )
    nxt_sb = z[z["is_sb"] == 1].groupby("trade_date").agg(
        sb_ret=("y_t1_close", "mean"),
        sb_rate=("y_lianban", "mean"),
        sb_gap=("y_gap1", "mean"),
    )
    emo = nxt.join(nxt_sb, how="outer")
    emo.columns = ["emo_zt_ret", "emo_zt_rate", "emo_zt_gap",
                   "emo_sb_ret", "emo_sb_rate", "emo_sb_gap"]
    for c in ("emo_zt_ret", "emo_zt_rate", "emo_zt_gap",
              "emo_sb_ret", "emo_sb_rate", "emo_sb_gap"):
        emo[c + "_1"] = emo[c].shift(1)     # 前一日的「涨停股次日表现」= 今天可观测
        emo[c + "_ma5"] = emo[c].rolling(5, min_periods=3).mean().shift(1)
        emo[c + "_ma10"] = emo[c].rolling(10, min_periods=5).mean().shift(1)
    emo = emo[[c for c in emo.columns if c.endswith(("_1", "_ma5", "_ma10"))]]

    # ---- 大盘 ----
    idx = pd.read_pickle(INDEX)
    idx = idx[idx["ts_code"].isin(["000001.SH", "399006.SZ", "000852.SH"])]
    piv = {}
    for code, short in (("000001.SH", "sh"), ("399006.SZ", "cyb"), ("000852.SH", "zz1000")):
        d = idx[idx["ts_code"] == code].set_index("trade_date")
        piv[f"idx_{short}"] = d["pct_chg"] / 100.0
        piv[f"idx_{short}_ret5"] = d["ret5"]
        piv[f"idx_{short}_ret20"] = d["ret20"]
        piv[f"idx_{short}_above_ma20"] = (d["close"] > d["ma20"]).astype(float)
        piv[f"idx_{short}_above_ma60"] = (d["close"] > d["ma60"]).astype(float)
    ip = pd.DataFrame(piv)
    ip.loc[~ip["idx_sh_above_ma20"].isin([0.0, 1.0]), "idx_sh_above_ma20"] = np.nan
    # 20 日波动
    ip["idx_sh_vol20"] = idx[idx["ts_code"] == "000001.SH"].set_index(
        "trade_date")["pct_chg"].rolling(20, min_periods=10).std() / 100.0
    # 大盘 20 日累计涨幅分位（滚动 250 日）
    r20 = ip["idx_sh_ret20"]
    ip["idx_sh_ret20_rank"] = r20.rolling(250, min_periods=60).apply(
        lambda a: float((a[-1] > a).mean()), raw=True)

    out = lu.join(mkt, how="outer").join(emo, how="left").join(ip, how="left")
    out.index.name = "trade_date"
    out = out.reset_index().sort_values("trade_date").reset_index(drop=True)
    out.to_pickle(OUT)
    print(f"市场环境表 {len(out)} 行 → {OUT}  {time.time() - t0:.1f}s")
    print(out.tail(4).to_string())
    print("\n列：", list(out.columns))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
