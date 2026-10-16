# -*- coding: utf-8 -*-
"""查清「真引擎成交数」与「离线子集成交数」为什么对不上（容量效应）。"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
from core import backtest as bt, market, strategy as st, pool as pool_mod
from config import BACKTEST_DEFAULTS

COND = "vol / ref(vol, 1) >= 3.2"
p = pool_mod.get_pool(156)
spec = {"base": p["base"], "where": list(p["conds"]) + [COND], "pool_exit": False,
        "pool_id": 156, "pool_name": p["name"]}
rc = st.merge_config({"exec": {"buy_price": "open", "sell_price": "close",
                              "max_hold_price": "close", "slippage": 0.001},
                      "risk": {"max_hold_days": 1, "stop_loss_pct": None,
                               "take_profit_pct": None},
                      "buy_trigger": [], "entry": [], "sell_signal": [], "score_expr": "",
                      "timeframe": "daily", "indicators": {}, "_pool": spec})
codes = market.resolve_universe(p["base"])
ci = {}
data = bt.prepare_data(codes, "2023-01-01", "2026-09-18", rc, batch=300, cache_info=ci)
print("缓存", ci.get("key"), "命中情况", {k: v for k, v in ci.items() if k != "key"})
prm = {**BACKTEST_DEFAULTS, "init_cash": 1_000_000.0, "max_positions": 5, "position_pct": 20.0}
res = bt.run_backtest(data, rc, "2023-01-01", "2026-09-18", prm)
tr = pd.DataFrame(res["trades"])
print("真引擎成交 %d 笔；收益 %.3f%%" % (len(tr), res["metrics"]["total_return"]))

feat = pd.read_csv("tools/_selfrein_feats.csv")
sub = feat[feat.x_vol_vs_prev >= 3.2]
print("run151 里满足该条件的成交 %d 笔" % len(sub))

ka = set(zip(sub.ts_code, sub.buy_date.astype(str)))
kb = set(zip(tr.ts_code, tr.buy_date.astype(str)))
print("交集 %d ；只在引擎里 %d ；只在 run151 子集里 %d" % (
    len(ka & kb), len(kb - ka), len(ka - kb)))

# 引擎多出来的那批，收益如何
extra = tr[~tr.apply(lambda r: (r.ts_code, str(r.buy_date)) in ka, axis=1)]
print("引擎多出 %d 笔，均笔 %.3f%%" % (len(extra), extra.pnl_pct.mean()))
both = tr[tr.apply(lambda r: (r.ts_code, str(r.buy_date)) in ka, axis=1)]
print("两边都有的 %d 笔，均笔 %.3f%%" % (len(both), both.pnl_pct.mean()))

# 引擎成交按日分布，看容量
d = tr.groupby("buy_date").size()
print("引擎成交日数 %d，每日笔数 max=%d，>5 的天数 %d" % (len(d), d.max(), (d > 5).sum()))
print("按年：")
tr["y"] = pd.to_datetime(tr.buy_date).dt.year
print(tr.groupby("y").agg(n=("pnl_pct", "size"), 均笔=("pnl_pct", "mean")).to_string())
