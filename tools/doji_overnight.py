# -*- coding: utf-8 -*-
"""解释「hold=1 + 止盈 5%」为什么能吃：星线次日的开盘→开盘漂移与触 +5% 的概率。

回测里 best 配置是 T+1 开盘买、盘中触及 +5% 就限价卖、否则 T+2 开盘卖。
信号层的前向收益台子最短只算到持 5 日收盘，看不出这种「只吃一口」的结构，
所以这里直接量两个数：
  · oo1 = open[i+2] / open[i+1] - 1（买次日开盘、再下一日开盘卖，不含止盈）
  · tp5 = 买入后 1 日内（当天盘中 high）触及 entry*1.05 的比例
  · sl  = 同日触及 entry*0.95 的比例（止盈止损同时可能触发 → 顺序未知，只给上界）

    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tools/doji_overnight.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from core import db, expr, market, universe as uni  # noqa: E402
from core import strategy as st  # noqa: E402

from doji_pools import POOLS  # noqa: E402
from tune_eat3 import FILT  # noqa: E402

START, END = "2023-01-01", "2026-09-18"
KEYS = ["u_star", "dn_star", "leg_lo", "legdom_lo", "gap_calm"]
FIELDS = set(st.BASE_FIELDS) | {"body", "amplitude", "upper_shadow",
                               "lower_shadow", "gap_open", "vol_ratio5",
                               "pos60", "ret20", "turnover_rate", "total_mv",
                               "limit_up"}


def main():
    codes = market.resolve_universe(uni.parse({"base": "ALL", "filt": FILT}),
                                    start=START, end=END)
    cal = [str(r["trade_date"])[:10] for r in db.query(
        "SELECT DISTINCT trade_date FROM daily_kline WHERE trade_date BETWEEN %s "
        "AND %s ORDER BY trade_date", (START, END))]
    dpos = {d: i for i, d in enumerate(cal)}
    n_days = len(cal)
    acc = {k: {"oo1": np.zeros(n_days), "oo1c": np.zeros(n_days),
               "tp5": np.zeros(n_days), "sl5": np.zeros(n_days),
               "hits": np.zeros(n_days)} for k in KEYS + ["base"]}
    import time
    t0 = time.time()
    for i in range(0, len(codes), 250):
        part = codes[i:i + 250]
        panel = market.load_panel(part, START, END, adjust="qfq",
                                 with_basic=True, weekly=False, only=FIELDS)
        for code, df in panel.items():
            n = len(df)
            if n < 60:
                continue
            o = df["open"].to_numpy(float)
            h = df["high"].to_numpy(float)
            dates = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d").to_numpy()
            gi = np.fromiter((dpos.get(d, -1) for d in dates), np.int64, count=n)
            ns = st._namespace(df)
            # 次日开盘买 → 再下一日开盘卖；以及买入日盘中是否触及 +5% / -5%
            oo1 = np.full(n, np.nan)
            oo1[:-2] = (o[2:] / o[1:-1] - 1) * 100
            touch_hi = np.full(n, np.nan)   # 买入当天 high / entry
            touch_lo = np.full(n, np.nan)
            touch_hi[:-1] = h[1:-1] / o[1:-1]
            touch_lo[:-1] = df["low"].to_numpy(float)[1:-1] / o[1:-1]
            ok = gi >= 0
            for k in KEYS + ["base"]:
                conds = [] if k == "base" else POOLS[k][0]
                M = np.ones(n, dtype=bool)
                for e in conds:
                    M &= expr.evaluate_bool(e, ns, n)
                M &= ok
                if not M.any():
                    continue
                idx = gi[M]
                a = acc[k]
                a["hits"][idx] += 1
                v = oo1[M]
                g = np.isfinite(v)
                a["oo1"][idx[g]] += v[g]
                a["oo1c"][idx[g]] += 1
                th, tl = touch_hi[M], touch_lo[M]
                g2 = np.isfinite(th)
                a["tp5"][idx[g2]] += (th[g2] >= 1.05)
                a["sl5"][idx[g2]] += (tl[g2] <= 0.95)
        print(f"  {min(i + 250, len(codes))}/{len(codes)} {time.time()-t0:.0f}s",
              flush=True)

    def per_day(a):
        d = (a["hits"] > 0).sum()
        return a["hits"].sum() / n_days, d / n_days * 100

    print(f"\n=== 买入次日开盘 → 第三日开盘（不含止盈）；交易日 {n_days} ===")
    print(f"{'pool':<12}{'日均':>7}{'有票日%':>8}{'oo1均':>9}{'触+5%':>8}{'触-5%':>8}")
    for k in ["base"] + KEYS:
        a = acc[k]
        pd_, dh = per_day(a)
        oo = a["oo1"].sum() / max(a["oo1c"].sum(), 1)
        tp = a["tp5"].sum() / max(a["hits"].sum(), 1) * 100
        sl = a["sl5"].sum() / max(a["hits"].sum(), 1) * 100
        print(f"{k:<12}{pd_:>7.1f}{dh:>8.0f}{oo:>9.3f}{tp:>8.1f}{sl:>8.1f}")


if __name__ == "__main__":
    main()
