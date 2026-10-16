# -*- coding: utf-8 -*-
import sys, time
sys.path.insert(0, ".")
from core import market, universe as uni
CONDS = ["limit_up = 0", "between(close, 5, 100)", "total_mv > 300000",
         "amount > 50000", "between(ret120, -0.15, 0.35)",
         "between(hh120 / ll120 - 1, 0.2, 0.6)",
         "(max(max(ma5, ma10), max(ma20, ma60)) - min(min(ma5, ma10), min(ma20, ma60))) / ma20 < 0.06",
         "atr_pct < 3.5", "between(close, ma20 * 0.97, ma20 * 1.10)",
         "ll60 > ref(ll120, 60) * 1.02", "hh60 > ref(hh120, 60) * 1.02",
         "turnover_rate > 1", "circ_mv > 200000", "volume_ratio < 2"]
td = market.latest_trade_date()
base = len(market.resolve_universe("all"))
for e in CONDS:
    t, _ = uni._preselect_terms([e])
    if t:
        n = len(uni.preselect_codes({"base": "all", "where": [e]}, td, td) or [])
        print(f"  可翻 {e[:52]:<54} 剩 {n:>5} ({n/base*100:.0f}%)")
    else:
        print(f"  翻不了 {e[:50]}")
