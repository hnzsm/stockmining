# -*- coding: utf-8 -*-
"""用已 dump 的命中明细评估「快版」池：丢多少票、前向收益怎么变、预筛砍到多少、耗时。"""
import sys, json, os, time
sys.path.insert(0, ".")
import numpy as np
from core import market, universe as uni, screener, strategy as st

D = json.load(open("tools/_trend_screen_check.json", encoding="utf-8"))
F, L = D["hits_full"], D["dropped_by_weekly"]
CORE = L and (F + L) or F          # CORE 命中 = FULL + 被周线砍掉的
cal_days = D["summary"]["n_trade_days"]

def rep(name, rows):
    d = {r["date"] for r in rows}
    out = [f"{name:<26} {len(rows):>6}笔  有票 {len(d):>4}/{cal_days} 天  日均 {len(rows)/max(len(d),1):>5.2f} 只"]
    for h in ("20", "40"):
        v = np.array([r["fr"][h] for r in rows], dtype=float)
        v = v[np.isfinite(v)]
        out.append(f"持{h} {np.nanmean(v):+.2f}%")
    print("  ".join(out))

rep("A 原版 12 条(含周线)", F)
rep("B 砍周线 10 条", CORE)
for th in [(200000, 0), (200000, 50000), (0, 50000), (200000, 100000)]:
    cm, am = th
    rows = [r for r in CORE
            if (r["circ_mv"] is None or r["circ_mv"] > cm)
            and (r["amount"] is None or r["amount"] > am)]
    rep(f"C 砍周线+circ_mv>{cm}+amount>{am}", rows)

CONDS = {
 "A12": ["ma_bull = 1", "count(ma_bull, 10) = 10", "ref(ma_bull, 20) = 0",
         "close >= ref(hh60, 1) * 0.97", "pos120 > 0.6", "close_pos > 0.7",
         "limit_up = 0", "pct_chg < 7", "w_close > w_ma20", "w_ma20 > ref(w_ma20, 4)",
         "between(close, 5, 100)", "total_mv > 300000"],
 "C_noW": ["ma_bull = 1", "count(ma_bull, 10) = 10", "ref(ma_bull, 20) = 0",
           "close >= ref(hh60, 1) * 0.97", "pos120 > 0.6", "close_pos > 0.7",
           "limit_up = 0", "pct_chg < 7", "between(close, 5, 100)",
           "total_mv > 300000", "circ_mv > 200000", "amount > 50000"],
 "带注释": ["ma_bull = 1        # 均线多头", "close_pos > 0.7   # 收在日内高位",
            "total_mv > 300000  # 30亿以上"],
}
FILT = {"exclude_st": True, "exclude_bj": True, "min_amount": 0,
        "min_list_days": 60, "exclude_kcb": False, "exclude_cyb": False}
td = market.latest_trade_date()
print(f"\n== SQL 预筛效果 @{td}（基础 5904 只）==")
for k, v in CONDS.items():
    terms, skipped = uni._preselect_terms(v)
    n = len(uni.preselect_codes({"base": "all", "where": v}, td, td) or [])
    print(f"  {k:<8} 可翻 {len(terms)}/{len(v)} 条 → 预筛剩 {n:>5} 只 "
          f"({n/5904*100:4.1f}%)  skipped={len(skipped)}")

print("\n== screen() 端到端耗时 ==")
for k in ("A12", "C_noW"):
    spec = {"base": "all", "where": CONDS[k], "filt": FILT, "pool_exit": False}
    t = time.time()
    r = screener.screen({"timeframe": "daily"}, uni.parse(spec), td, limit=300, save=False)
    print(f"  {k:<8} {time.time()-t:6.1f}s  扫描 {r.get('scanned')}  命中 {r.get('count')}  "
          f"{[x['ts_code'] for x in (r.get('items') or [])][:8]}")
