# -*- coding: utf-8 -*-
"""趋势票池子的组合回测（页面口径）：把 trend_pool_lab 的前向收益结论
放到真实资金曲线（槽位、滑点、复利）上再验一遍。

池子来自 tools/trend_pool_lab.py 第四轮：`q_wick` / `q_turnlo` / `q_base10`，
外加 `q_dip`（回踩族）做反面参照 —— 前向收益里它只有 ~0.1% 的微弱超额。

一次 prepare_data 服务多个卖出配置（risk / 仓位都是在 run_backtest
阶段生效的），所以改卖出参数只付一次数据加载的钱。

    python tools/trend_backtest.py                 # 默认网格
    python tools/trend_backtest.py --pools q_wick  # 只跑一个池
    python tools/trend_backtest.py --slips 0.001,0.002,0.003
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from config import BACKTEST_DEFAULTS  # noqa: E402
from core import backtest as bt, market, universe as uni  # noqa: E402
from core import strategy as st  # noqa: E402

from tune_eat1 import TRAIN, VALID, bench  # noqa: E402
from tune_eat3 import FILT  # noqa: E402

START, END = "2023-01-01", "2026-09-18"

# 「均线多头已成立 + 逼近 60 日新高 + 不涨停」这一族（trend_pool_lab 第三/四轮）
CORE = ["ma_bull = 1", "count(ma_bull, 10) = 10", "ref(ma_bull, 20) = 0",
        "close >= ref(hh60, 1) * 0.97", "pos120 > 0.6", "limit_up = 0",
        "pct_chg < 7", "between(close, 5, 100)", "total_mv > 300000"]
POOLS = {
    "q_base10": CORE,
    "q_wick": CORE + ["close_pos > 0.7"],
    "q_turnlo": CORE + ["turnover_rate < 3"],
    "q_wick_turnlo": CORE + ["close_pos > 0.7", "turnover_rate < 3"],
    # 第五轮：q_wick + 周线向上（lab 里分年按日对齐超额最不平坦的一条）
    "w_up2": CORE + ["close_pos > 0.7", "w_close > w_ma20",
                     "w_ma20 > ref(w_ma20, 4)"],
    "q_dip": ["ma_bull = 1", "close > ma60", "ma60 > ref(ma60, 10)",
              "close < ma5", "close > ma20", "limit_up = 0",
              "between(close, 5, 100)", "total_mv > 300000"],
}
TIERS = [(10, 10.0), (20, 5.0), (5, 20.0)]

# 卖出规则。**引擎的卖出已位移一根**（2026-09-30）：第 i 天用 `sell_signal[i-1]` /
# `in_pool[i-1]` 判定、按第 i 天开盘或收盘成交（core/backtest.py 卖出段）。
# 所以 sell_signal 里可以放心写「今天收盘才知道的条件」（`close < ma10` 等）——
# 它在 T 日收盘确认、T+1 开盘执行，不再是未来函数。
# ⚠ 代价：这里的 `ref(close,1)` 写法等于「T-1 收盘确认、T+1 卖」，比 `close` 晚一根。
# 两种写法都合法，别混用；新写的建议直接用当天写法，语义更直观。
SELLS = {
    "hold": ([], None),                       # 只按 max_hold 到期
    "ma20": (["ref(close, 1) < ref(ma20, 1)"], 60),   # 昨收跌破 20 日线 → 今开盘卖
    "ma10": (["ref(close, 1) < ref(ma10, 1)"], 60),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pools", default="q_wick,q_turnlo,q_dip")
    ap.add_argument("--holds", default="10,20,40")
    ap.add_argument("--sells", default="hold,ma20")
    ap.add_argument("--slips", default="0.001")
    ap.add_argument("--score", default="")
    args = ap.parse_args()
    slips = [float(x) for x in args.slips.split(",") if x]

    for pool_key in [p for p in args.pools.split(",") if p]:
        conds = POOLS[pool_key]
        print(f"\n########## 池 {pool_key}（{len(conds)} 条）##########", flush=True)
        codes = market.resolve_universe({"base": "ALL", "where": conds},
                                        start=START, end=END)
        print(f"预筛 {len(codes)} 只", flush=True)
        for skey in [s for s in args.sells.split(",") if s]:
            sig, backstop = SELLS[skey]
            print(f"\n--- 卖出={skey} ---", flush=True)
            for slip in slips:
                cfg = st.merge_config({
                    "timeframe": "daily", "entry": [], "buy_trigger": [],
                    "sell_signal": list(sig), "score_expr": args.score,
                    "risk": {"stop_loss_pct": None, "take_profit_pct": None,
                             "max_hold_days": 20},
                    "exec": {"buy_price": "open", "sell_price": "open",
                             "max_hold_price": "open", "slippage": slip},
                    "_pool": uni.parse({"base": "ALL", "where": conds,
                                         "filt": FILT}),
                })
                errs = st.validate_config(cfg)
                if errs:
                    print("校验失败：", errs)
                    return
                # 缓存键含信号条件（core/btcache.py），所以换 sell 规则要重载一次；
                # 换 risk（max_hold / 止盈）与仓位不进键 → 后面全是秒级。
                t0 = time.time()
                info: dict = {}
                data = bt.prepare_data(codes, START, END, cfg, batch=250,
                                       cache_info=info)
                print(f"滑点 {slip}：有效 {len(data)} 只 / 加载 "
                      f"{time.time()-t0:.0f}s / 缓存={info.get('cache')}", flush=True)
                rows = []
                holds = ([int(x) for x in args.holds.split(",") if x]
                         if skey == "hold" else [backstop])
                for hold in holds:
                    c2 = {**cfg, "risk": {**cfg["risk"], "max_hold_days": hold,
                                          "take_profit_pct": None}}
                    for npos, ppct in TIERS:
                        p = {**BACKTEST_DEFAULTS, "init_cash": 1_000_000,
                             "slippage": slip, "max_positions": npos,
                             "position_pct": ppct}
                        rec = {"sell": skey, "hold": hold, "npos": npos,
                               "ppct": ppct, "slip": slip, "w": {}}
                        for name, (s, e) in (
                                ("FULL", (START, END)), ("TRAIN", TRAIN),
                                ("VALID", VALID), ("2023", ("2023-01-01", "2023-12-31")),
                                ("2024", ("2024-01-01", "2024-12-31")),
                                ("2025", ("2025-01-01", "2025-12-31")),
                                ("2026", ("2026-01-01", END))):
                            m = bt.run_backtest(data, c2, s, e, p,
                                                benchmark=bench(s, e)).get("metrics") or {}
                            rec["w"][name] = m
                        f = rec["w"]["FULL"]
                        print(f"  {skey:<5} hold{hold:<3} {npos}x{ppct:<5} "
                              f"F{f.get('annual_return'):>7.2f} "
                              f"累计{f.get('total_return'):>9.2f} "
                              f"回撤{f.get('max_drawdown'):>7.2f} "
                              f"{f.get('trade_count'):>5}笔 "
                              f"胜{f.get('win_rate') or 0:>5.1f} "
                              f"| T{rec['w']['TRAIN'].get('annual_return'):>7.2f} "
                              f"V{rec['w']['VALID'].get('annual_return'):>7.2f} "
                              f"| " + " ".join(
                                  f"{rec['w'][y].get('annual_return') or 0:>6.1f}"
                                  for y in ("2023", "2024", "2025", "2026")),
                              flush=True)
                        rows.append(rec)
                with open(os.path.join(ROOT, "tools",
                                       f"_trend_bt_{pool_key}_{skey}_{slip}.json"),
                          "w", encoding="utf-8") as fh:
                    json.dump(rows, fh, ensure_ascii=False, indent=1, default=str)


if __name__ == "__main__":
    main()
