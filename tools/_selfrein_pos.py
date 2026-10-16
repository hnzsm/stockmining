# -*- coding: utf-8 -*-
"""仓位槽位 × 风控 的二维扫描（两套池都命中缓存，几乎零成本）。

目的：把「池收窄 → 槽位重新分配」这个组合效应剥掉，看过滤器本身值多少。
    max_positions / position_pct / init_cash / risk / exec 全部**不进** btcache 键。
"""
import sys, os, itertools, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
from core import backtest as bt, market, strategy as st, pool as pool_mod
from config import BACKTEST_DEFAULTS

COND = "vol / ref(vol, 1) >= 3.2"
START, END = "2023-01-01", "2026-09-18"


def make(spec, hold, sl, tp, buy=None):
    return st.merge_config({
        "exec": {"buy_price": "open", "sell_price": "close", "max_hold_price": "close",
                 "slippage": 0.001},
        "risk": {"max_hold_days": hold, "stop_loss_pct": sl, "take_profit_pct": tp},
        "buy_trigger": list(buy or []), "entry": [], "sell_signal": [], "score_expr": "",
        "timeframe": "daily", "indicators": {}, "_pool": spec})


p = pool_mod.get_pool(156)
POOLS = {
    "原池": list(p["conds"]),
    "+量能3.2": list(p["conds"]) + [COND],
}
codes = market.resolve_universe(p["base"])
rows = []
DATA = {}
for pname, where in POOLS.items():
    spec = {"base": p["base"], "where": where, "pool_exit": False,
            "pool_id": 156, "pool_name": p["name"]}
    rc = make(spec, 1, None, None)
    ci = {}
    DATA[pname] = (bt.prepare_data(codes, START, END, rc, batch=300, cache_info=ci),
                   spec, rc)
    print("池 %-8s 准备完成 缓存=%s 标的=%d" % (pname, ci.get("cache"), len(DATA[pname][0])), flush=True)

for pname in POOLS:
    data, spec, rc = DATA[pname]
    for mp in (5, 10, 20, 30):
        for (hold, sl, tp) in ((1, None, None), (1, -2, 5), (2, None, None),
                               (5, -6, 12), (3, None, None)):
            cfg = make(spec, hold, sl, tp)
            prm = {**BACKTEST_DEFAULTS, "init_cash": 1_000_000.0,
                   "max_positions": mp, "position_pct": round(100.0 / mp, 4)}
            m = bt.run_backtest(data, cfg, START, END, prm).get("metrics") or {}
            rows.append(dict(pool=pname, mp=mp, pct=prm["position_pct"], hold=hold, sl=sl, tp=tp,
                             ret=m.get("total_return"), dd=m.get("max_drawdown"),
                             n=m.get("trade_count"), win=m.get("win_rate"),
                             pnl=m.get("avg_pnl_pct"), final=m.get("final_asset")))
            print("  %-8s mp=%-3d 持%-2s sl=%-5s tp=%-5s → 收益 %8.2f%%  均笔 %7.3f%%  胜率 %5.1f  %4s笔  回撤 %7.2f%%"
                  % (pname, mp, hold, sl, tp, m.get("total_return") or 0,
                     m.get("avg_pnl_pct") or 0, m.get("win_rate") or 0,
                     m.get("trade_count"), m.get("max_drawdown") or 0), flush=True)
    del DATA[pname]
json.dump(rows, open("tools/_selfrein_pos.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
R = pd.DataFrame(rows)
print("\n===== 只看收益为正的 =====")
print(R[R.ret > 0].sort_values("ret", ascending=False).to_string(index=False,
      float_format=lambda v: "%.3f" % v))
print("\n→ tools/_selfrein_pos.json")
