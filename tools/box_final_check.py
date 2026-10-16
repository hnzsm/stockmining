# -*- coding: utf-8 -*-
"""箱体蓄势终选：打印最终条件、SQL 预筛裁剪、耗时，并列出最新交易日命中的票与形态读数。

    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tools/box_final_check.py
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import market, screener, universe as uni  # noqa: E402
from tools.box_pool_lab import (A60, ADXB, CORE, R3, SQLY, STEP,  # noqa: E402
                                MID, NARROW, NEAR, RANGE, FLAT, SANE, GLUE)

FILT = {"exclude_st": True, "exclude_bj": True, "min_amount": 0,
        "min_list_days": 60, "exclude_kcb": False, "exclude_cyb": False}
POOLS = {
    "f_60（推荐）": R3 + ADXB + A60,
    "f_60_sql（更快/更陡）": R3 + ADXB + A60 + SQLY,
}
SHOW = ["ret120", "hh120", "ll120", "pos120", "atr_pct", "dmi_adx", "close",
        "total_mv", "amount", "turnover_rate", "ma20", "ma60", "vol_ratio5"]

td = market.latest_trade_date()
base = len(market.resolve_universe("all"))
print(f"最新交易日 {td}，基础池 {base} 只\n")

for name, conds in POOLS.items():
    print(f"### {name}  共 {len(conds)} 条")
    for c in conds:
        print(f"  {c}")
    terms, skipped = uni._preselect_terms(conds)
    t0 = time.time()
    pre = uni.preselect_codes({"base": "all", "where": conds}, td, td)
    print(f"  SQL 预筛：可翻 {len(terms)}/{len(conds)} 条 → "
          f"{len(pre or [])} 只（{len(pre or [])/base*100:.0f}%），{time.time()-t0:.1f}s")
    spec = {"base": "all", "where": conds, "filt": FILT}
    t0 = time.time()
    res = screener.screen({"timeframe": "daily"}, uni.parse(spec), td,
                          limit=50, save=False)
    items = res.get("items") or []
    print(f"  screen() 实跑 {time.time()-t0:.1f}s：扫描 {res.get('scanned')} 只 / "
          f"命中 {res.get('count')} 只")
    for it in items[:12]:
        vals = " ".join(f"{k}={it[k]:.2f}" for k in SHOW
                        if isinstance(it.get(k), (int, float)))
        print(f"    {it['ts_code']} {it['name']:<8} {vals}")
    print()
