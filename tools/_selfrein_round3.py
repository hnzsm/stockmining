# -*- coding: utf-8 -*-
"""第三轮：把 round2 挑出来的两组，按 **TRAIN / VALID 分开** 在真引擎上复核。

⚠ 为什么非做不可：全样本一个数字会被单段行情绑架（C26「只看单年会骗人」）。
   本脚本把区间切开，只有**两段都为正**才算真站得住。

    C1 = 原池 + [量能≥3.2]                       + buy_trigger 低开≤-2%
    C2 = 原池 + [量能≥3.2] + [ref(ret20,1)<=0.147] + buy_trigger 低开≤-2%

区间：TRAIN 2023-01-01~2024-12-31 / VALID 2025-01-01~2026-09-18（split 与离线搜索一致）
每段一次 prepare（区间进键），段内所有风险/仓位变体共享。
"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
from core import backtest as bt, market, strategy as st, pool as pool_mod, btcache
from config import BACKTEST_DEFAULTS

btcache.MAX_BYTES = 8 * 1024 * 1024 * 1024        # 见 _selfrein_engine.py 说明：只抬上限、不删文件

VOLC = "vol / ref(vol, 1) >= 3.2"
LOWBASE = "ref(ret20, 1) <= 0.147"
GAP_LO = ["open <= ref(close, 1) * 0.98"]

COMBOS = [
    ("C1 放量×低吸", [VOLC]),
    ("C2 放量×低位×低吸", [VOLC, LOWBASE]),
]
SPANS = [("TRAIN", "2023-01-01", "2024-12-31"), ("VALID", "2025-01-01", "2026-09-18")]
VARIANTS = [("持1无止盈损", 1, None, None), ("持1止损2止盈5", 1, -2, 5)]
POSS = [(5, 20.0), (10, 10.0)]


def cfg_of(spec, hold, sl, tp):
    return st.merge_config({
        "exec": {"buy_price": "open", "sell_price": "close", "max_hold_price": "close",
                 "slippage": 0.001},
        "risk": {"max_hold_days": hold, "stop_loss_pct": sl, "take_profit_pct": tp},
        "buy_trigger": list(GAP_LO), "entry": [], "sell_signal": [], "score_expr": "",
        "timeframe": "daily", "indicators": {}, "_pool": spec})


p = pool_mod.get_pool(156)
codes = market.resolve_universe(p["base"])
rows = []
for cname, extra in COMBOS:
    spec = {"base": p["base"], "where": list(p["conds"]) + extra, "pool_exit": False,
            "pool_id": 156, "pool_name": p["name"]}
    for span, s0, s1 in SPANS:
        rc0 = cfg_of(spec, 1, None, None)
        errs = st.validate_config(rc0)
        if errs:
            print("跳过", cname, span, errs); continue
        t0 = time.time()
        ci = {}
        data = bt.prepare_data(codes, s0, s1, rc0, batch=300, cache_info=ci)
        bn = len({c for c in data})
        print("\n### %s / %s  准备 %.1fs  缓存=%s  标的=%d"
              % (cname, span, time.time() - t0, ci.get("cache"), bn), flush=True)
        for (vname, hold, sl, tp) in VARIANTS:
            cfg = cfg_of(spec, hold, sl, tp)
            for (mp, pct) in POSS:
                prm = {**BACKTEST_DEFAULTS, "init_cash": 1_000_000.0,
                       "max_positions": mp, "position_pct": pct}
                m = bt.run_backtest(data, cfg, s0, s1, prm).get("metrics") or {}
                r = dict(combo=cname, span=span, cond=" ; ".join(extra), var=vname,
                         mp=mp, pct=pct, ret=m.get("total_return"),
                         dd=m.get("max_drawdown"), n=m.get("trade_count"),
                         win=m.get("win_rate"), pnl=m.get("avg_pnl_pct"),
                         annual=m.get("annual_return"))
                rows.append(r)
                print("   %-14s %2d×%-5.2f%% → 收益 %8.2f%%  年化 %8.2f%%  均笔 %7.3f%%  "
                      "胜率 %5.1f  %4s笔  回撤 %7.2f%%"
                      % (vname, mp, pct, r["ret"] or 0, r["annual"] or 0, r["pnl"] or 0,
                         r["win"] or 0, r["n"], r["dd"] or 0), flush=True)
        del data

json.dump(rows, open("tools/_selfrein_round3.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
R = pd.DataFrame(rows)
print("\n===== 分区间汇总（同组合同变体，TRAIN vs VALID）=====")
for cname, extra in COMBOS:
    for vname, _, _, _ in VARIANTS:
        for (mp, pct) in POSS:
            s = R[(R.combo == cname) & (R["var"] == vname) & (R.mp == mp)]
            if len(s) < 2:
                continue
            tr = s[s.span == "TRAIN"].iloc[0]
            va = s[s.span == "VALID"].iloc[0]
            flag = "  ✅两段都正" if (tr.ret or -9) > 0 and (va.ret or -9) > 0 else ""
            print("  %-16s %-14s %2d×%-5.1f%%  TRAIN %8.2f%% (n=%s) | VALID %8.2f%% (n=%s)%s"
                  % (cname, vname, mp, pct, tr.ret or 0, tr.n, va.ret or 0, va.n, flag))
print("→ tools/_selfrein_round3.json")
