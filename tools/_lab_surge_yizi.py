# -*- coding: utf-8 -*-
"""只数数：10 日涨 50% 的事件里，有多少是「首板一字板」（被剔除的）。

不落特征，只加载 open/high/low/close/limit_up，跑得快。
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db, market  # noqa: E402

SINCE, UNTIL, WARM = "2025-01-01", "2026-09-28", "2024-06-01"
WIN, GAIN = 10, 0.50
FIELDS = {"open", "high", "low", "close", "limit_up"}


def main():
    rows = db.query("SELECT DISTINCT ts_code FROM daily_kline WHERE trade_date >= %s",
                    [SINCE])
    codes = sorted(r["ts_code"] for r in rows)
    smap = market.get_stock_map() or {}
    tot = yizi = 0
    t0 = time.time()
    B = 400
    for bi in range(0, len(codes), B):
        batch = codes[bi:bi + B]
        try:
            panel = market.load_panel(batch, WARM, UNTIL, freq="daily",
                                      adjust="qfq", with_basic=False,
                                      weekly=False, only=FIELDS)
        except Exception as e:
            print("批次失败", bi, e)
            continue
        for code, df in panel.items():
            if df is None or len(df) < 30:
                continue
            meta = smap.get(code) or {}
            if code[:6].startswith(("8", "4", "9")) or "ST" in (meta.get("name") or "").upper():
                continue
            d = df.reset_index(drop=True)
            dt = pd.Series(d["trade_date"]).dt.strftime("%Y-%m-%d") \
                if hasattr(d["trade_date"].iloc[0], "strftime") \
                else d["trade_date"].astype(str)
            close = d["close"].to_numpy(float)
            high = d["high"].to_numpy(float)
            low = d["low"].to_numpy(float)
            lu = d["limit_up"].to_numpy(float)
            n = len(d)
            fwd = np.full(n, np.nan)
            for k in range(1, WIN + 1):
                sh = np.full(n, np.nan)
                if n - k > 0:
                    sh[:n - k] = close[k:]
                fwd = np.where(np.isnan(fwd), sh,
                               np.where(np.isnan(sh), fwd, np.maximum(fwd, sh)))
            gain = fwd / close - 1
            last = -1
            for i in range(n):
                ds = dt.iloc[i]
                if ds < SINCE or ds > UNTIL or not np.isfinite(gain[i]) \
                        or gain[i] < GAIN or i <= last:
                    continue
                seg = slice(i + 1, min(i + WIN, n - 1) + 1)
                j = i + 1 + int(np.nanargmax(np.where(np.isnan(close[seg]), -np.inf,
                                                      close[seg])))
                last = j
                tot += 1
                lims = [k for k in range(i + 1, j + 1) if lu[k] == 1]
                if lims and high[lims[0]] == low[lims[0]]:
                    yizi += 1
        print(f"[{bi + len(batch)}/{len(codes)}] 总事件 {tot} 其中首板一字 {yizi}"
              f"  {time.time() - t0:.0f}s", flush=True)
    print(f"结果：总事件 {tot}，首板一字板 {yizi}（占 {yizi/max(tot,1)*100:.1f}%），"
          f"采纳 {tot - yizi}")


if __name__ == "__main__":
    import pandas as pd  # noqa: E402
    main()
