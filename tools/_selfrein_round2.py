# -*- coding: utf-8 -*-
"""第二轮：把「离线搜索挑出来的条件组合」放到真引擎上验。

两组（各一次 prepare，约 2 分钟）：
    G1 = 原池 + [量能≥3.2] + [ref(ret20,1) <= 0.147]         （放天量大阴 × 低位）
    G2 = 原池 + [量能≥3.2]，buy_trigger = 低开≤-2%（T+1 开盘）  （放天量大阴 × 承接弱）

每组再扫仓位（max_positions / position_pct 不进缓存键，零成本）。
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
from core import backtest as bt, market, strategy as st, pool as pool_mod, btcache
from config import BACKTEST_DEFAULTS

btcache.MAX_BYTES = 8 * 1024 * 1024 * 1024        # 见 _selfrein_engine.py 的说明

START, END = "2023-01-01", "2026-09-18"
VOLC = "vol / ref(vol, 1) >= 3.2"
LOWBASE = "ref(ret20, 1) <= 0.147"
GAP_LO = ["open <= ref(close, 1) * 0.98"]

GROUPS = [
    ("G1 放量×低位", [VOLC, LOWBASE], []),
    ("G2 放量×低吸", [VOLC], GAP_LO),
    ("G3 放量×低位×低吸", [VOLC, LOWBASE], GAP_LO),
]
RISKS = [(1, None, None), (1, -2, 5), (2, None, None), (5, -6, 12)]
POSS = [(5, 20.0), (10, 10.0), (20, 5.0)]


def make(spec, hold, sl, tp, buy):
    return st.merge_config({
        "exec": {"buy_price": "open", "sell_price": "close", "max_hold_price": "close",
                 "slippage": 0.001},
        "risk": {"max_hold_days": hold, "stop_loss_pct": sl, "take_profit_pct": tp},
        "buy_trigger": list(buy), "entry": [], "sell_signal": [], "score_expr": "",
        "timeframe": "daily", "indicators": {}, "_pool": spec})


p = pool_mod.get_pool(156)
codes = market.resolve_universe(p["base"])
rows = []
for gname, extra, buy in GROUPS:
    spec = {"base": p["base"], "where": list(p["conds"]) + extra, "pool_exit": False,
            "pool_id": 156, "pool_name": p["name"]}
    rc = make(spec, 1, None, None, buy)
    errs = st.validate_config(rc)
    if errs:
        print("跳过", gname, errs)
        continue
    ci = {}
    data = bt.prepare_data(codes, START, END, rc, batch=300, cache_info=ci)
    print("\n### %s  条件+%s  buy=%s  缓存=%s 标的=%d" % (gname, extra, buy, ci.get("cache"), len(data)),
          flush=True)
    for (hold, sl, tp) in RISKS:
        for (mp, pct) in POSS:
            cfg = make(spec, hold, sl, tp, buy)
            prm = {**BACKTEST_DEFAULTS, "init_cash": 1_000_000.0,
                   "max_positions": mp, "position_pct": pct}
            m = bt.run_backtest(data, cfg, START, END, prm).get("metrics") or {}
            row = dict(group=gname, cond=" ; ".join(extra), buy=" ; ".join(buy),
                       hold=hold, sl=sl, tp=tp, mp=mp, pct=pct,
                       ret=m.get("total_return"), dd=m.get("max_drawdown"),
                       n=m.get("trade_count"), win=m.get("win_rate"), pnl=m.get("avg_pnl_pct"))
            rows.append(row)
            print("   持%-2s sl=%-5s tp=%-5s  %2d×%-5.2f%% → 收益 %8.2f%%  均笔 %7.3f%%  胜率 %5.1f  %4s笔  回撤 %7.2f%%"
                  % (hold, sl, tp, mp, pct, row["ret"] or 0, row["pnl"] or 0,
                     row["win"] or 0, row["n"], row["dd"] or 0), flush=True)
    del data
json.dump(rows, open("tools/_selfrein_round2.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
R = pd.DataFrame(rows)
print("\n===== 正收益 =====")
print(R[R.ret > 0].sort_values("ret", ascending=False).to_string(index=False,
      float_format=lambda v: "%.3f" % v))
print("→ tools/_selfrein_round2.json")
