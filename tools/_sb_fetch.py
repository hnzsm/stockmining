# -*- coding: utf-8 -*-
"""首板研究 · 第 1 步：把全市场日线 + 估值面拉成一张本地表。

只读，不写任何库表。产物 tools/_sb_raw.pkl：
  ts_code / trade_date / ohlc / pre_close / pct_chg / vol / amount
  + turnover_rate / volume_ratio / total_mv / circ_mv
  + name / industry / market / list_date / is_st（从 stock_basic 贴）

⚠ 库里的日线只有 **2023-01-03 起**，所以任何长周期回看（ma60/ret120）
  都只从 2023-01-03 起算，前面是天然不足的 —— 分析时会显式丢掉前 120 个交易日。
"""
from __future__ import annotations

import io
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import db  # noqa: E402

START = "2023-01-03"
END = "2026-12-31"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_sb_raw.pkl")


def main() -> int:
    t0 = time.time()
    conn = db.get_conn()
    basics = pd.DataFrame(db.query(
        "SELECT ts_code, name, industry, market, exchange, list_date, list_status "
        "FROM stock_basic"))
    codes = sorted(basics.loc[basics["list_status"] == "L", "ts_code"].tolist())
    print(f"上市股票 {len(codes)} 只", flush=True)

    parts = []
    step = 400
    for i in range(0, len(codes), step):
        part = codes[i:i + step]
        ph = ",".join(["%s"] * len(part))
        sql = (
            "SELECT k.ts_code, k.trade_date, k.open, k.high, k.low, k.close, k.pre_close, "
            "       k.pct_chg, k.vol, k.amount, "
            "       b.turnover_rate, b.volume_ratio, b.total_mv, b.circ_mv "
            f"FROM daily_kline k LEFT JOIN daily_basic b "
            "  ON b.ts_code = k.ts_code AND b.trade_date = k.trade_date "
            f"WHERE k.ts_code IN ({ph}) AND k.trade_date BETWEEN %s AND %s"
        )
        rows = db.query(sql, part + [START, END])
        if rows:
            parts.append(pd.DataFrame(rows))
        print(f"  {i + len(part):5d}/{len(codes)}  累计 {sum(len(p) for p in parts):>9,} 行  "
              f"{time.time() - t0:6.1f}s", flush=True)

    df = pd.concat(parts, ignore_index=True)
    del parts
    print(f"合并 {len(df):,} 行   {time.time() - t0:.1f}s", flush=True)

    num = ["open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount",
           "turnover_rate", "volume_ratio", "total_mv", "circ_mv"]
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    for c in num:
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")

    basics = basics.drop(columns=["list_status"])
    df = df.merge(basics, on="ts_code", how="left")
    df["is_st"] = df["name"].fillna("").str.upper().str.contains("ST").astype("int8")
    df["list_date"] = pd.to_datetime(df["list_date"], errors="coerce")
    df["list_days"] = (df["trade_date"] - df["list_date"]).dt.days

    df = df.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)
    df.to_pickle(OUT)
    print(f"写出 {OUT}  {os.path.getsize(OUT) / 1048576:.1f} MB  共 {time.time() - t0:.1f}s")
    print(df.head(3).to_string())
    print("交易日", df["trade_date"].nunique(), "首日", df["trade_date"].min().date(),
          "末日", df["trade_date"].max().date())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
