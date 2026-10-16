# -*- coding: utf-8 -*-
"""首板研究 · 第 2 步：指数（大盘环境）本地化。产物 tools/_sb_index.pkl。只读。"""
from __future__ import annotations

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import db  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_sb_index.pkl")


def main() -> int:
    df = pd.DataFrame(db.query(
        "SELECT ts_code, trade_date, open, high, low, close, pre_close, pct_chg, vol, amount "
        "FROM index_daily WHERE trade_date >= '2022-09-01'"))
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    for c in ("open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)

    g = df.groupby("ts_code", sort=False)
    df["ma20"] = g["close"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    df["ma60"] = g["close"].transform(lambda s: s.rolling(60, min_periods=60).mean())
    df["ret5"] = g["close"].transform(lambda s: s / s.shift(5) - 1)
    df["ret20"] = g["close"].transform(lambda s: s / s.shift(20) - 1)
    df["ret60"] = g["close"].transform(lambda s: s / s.shift(60) - 1)

    df.to_pickle(OUT)
    print("写出", OUT, len(df), "行")
    for code, sub in df.groupby("ts_code"):
        print(f"  {code}  {sub['trade_date'].min().date()} ~ {sub['trade_date'].max().date()}  "
              f"{len(sub)} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
