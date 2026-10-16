# -*- coding: utf-8 -*-
"""UI 等价性验证：只用「页面能表达的配置」跑一遍调优结论。

调优台里为了复用缓存，把「次日无条件买入」做成了替换 buy_trigger 数组、
把评分杠杆做成了注入 score 数组。页面上没有这两件事，只有：
  * 买入触发留空        → compute_signals 按「全 True」算，等价于替换数组
  * 评分表达式 ref(close_pos, 1) → 买入日的「形态日收盘位置」，等价于注入 cp
本脚本走**页面口径**（表达式 + risk/exec + params），确认结论不是调优台造出来的。

    python tools/verify_ui_cfg.py
"""
from __future__ import annotations

import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from config import BACKTEST_DEFAULTS  # noqa: E402
from core import backtest as bt, market, universe as uni  # noqa: E402
from core import strategy as st  # noqa: E402

from tune_eat1 import TRAIN, VALID, bench  # noqa: E402
from tune_eat3 import FILT  # noqa: E402

START, END = "2023-01-01", "2026-09-18"

# 池：现池 #5 去掉「close < 50」（其余 10 条原样）
CONDS = ["total_mv < 10000000", "is_up = 1", "upper_shadow > 2*lower_shadow",
         "ref(is_down, 1) = 1", "ref(amplitude, 1) >= 5", "ref(amplitude, 1) <= 15",
         "ref(body, 1) <= -5", "ref(turnover_rate, 1) < 15", "ref(limit_up, 2) = 1",
         "ref(amplitude, 2) > 6"]

DIP = ["ref(limit_up, 1) = 1", "body <= -5"]

POOLS = {
    # 消融后的现池家族
    "no_close50": CONDS,
    # 换池：昨日涨停 + 今日大阴（两条条件，样本量大一个数量级）
    "dip": DIP,
    # 池 #156 之上再收窄（tune_eat8 的胜出条件，全部是 T 日可知的信息）
    "dip_yz": DIP + ["ref(amplitude, 1) >= 0.5"],
    "dip_yz_cp": DIP + ["ref(amplitude, 1) >= 0.5", "close_pos < 0.2"],
    "dip_yz_cp_ldn": DIP + ["ref(amplitude, 1) >= 0.5", "close_pos < 0.2",
                            "limit_down = 0"],
}

WINS = {"2023": ("2023-01-01", "2023-12-31"),
        "2024": ("2024-01-01", "2024-12-31"),
        "2025": ("2025-01-01", "2025-12-31"),
        "2026": ("2026-01-01", "2026-09-18"),
        "TRAIN": TRAIN, "VALID": VALID,
        "FULL": (START, END)}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", default="no_close50", choices=list(POOLS))
    ap.add_argument("--slip", type=float, default=0.001)
    ap.add_argument("--npos", type=int, default=2)
    ap.add_argument("--ppct", type=float, default=50.0)
    ap.add_argument("--tp", type=float, default=5.0)
    ap.add_argument("--hold", type=int, default=1)
    ap.add_argument("--score", default="ref(close_pos, 1)",
                    help="评分表达式；留空 = 不排序（引擎按池内插入序）")
    args = ap.parse_args()
    conds = POOLS[args.pool]
    print(f"池={args.pool}｜滑点 {args.slip}｜持仓 {args.npos}×{args.ppct}%｜"
          f"止盈 {args.tp}｜最长持有 {args.hold}｜评分 {args.score or '（无）'}")

    cfg = {
        "timeframe": "daily",
        "entry": [],
        "buy_trigger": [],            # 空 = 次日无条件开盘买（调优台的 buy=always）
        "sell_signal": [],            # 空 = 只按到期 / 止盈离场
        "score_expr": args.score,     # 买入日用「形态日」的收盘位置排序
        "risk": {"stop_loss_pct": None, "take_profit_pct": args.tp,
                 "max_hold_days": args.hold},
        "exec": {"buy_price": "open", "sell_price": "open",
                 "max_hold_price": "open", "slippage": args.slip},
        "_pool": uni.parse({"base": "ALL", "where": conds, "filt": FILT}),
    }
    cfg = st.merge_config(cfg)
    errs = st.validate_config(cfg)
    print("校验：", errs or "通过")
    if errs:
        return

    t0 = time.time()
    cache = {}
    codes = market.resolve_universe({"base": "ALL", "where": conds},
                                    start=START, end=END)
    base = market.resolve_universe("ALL")
    data = bt.prepare_data(codes, START, END, cfg, batch=250, cache_info=cache)
    print(f"股票池：预筛 {len(codes)} 只 / 基础池 {len(base)} 只 / 有效 {len(data)} 只"
          f"，加载 {time.time()-t0:.0f}s，缓存={cache.get('cache')}", flush=True)

    params = {**BACKTEST_DEFAULTS, "init_cash": 1_000_000, "slippage": args.slip,
              "max_positions": args.npos, "position_pct": args.ppct}
    out = {}
    for name, (s, e) in WINS.items():
        res = bt.run_backtest(data, cfg, s, e, params, benchmark=bench(s, e))
        m = res.get("metrics") or {}
        out[name] = m
        print(f"  {name:<6} 年化 {m.get('annual_return'):>7.2f}%  "
              f"累计 {m.get('total_return'):>8.2f}%  回撤 {m.get('max_drawdown'):>7.2f}%  "
              f"{m.get('trade_count'):>4}笔  胜率 {m.get('win_rate'):>5.1f}%  "
              f"均笔 {m.get('avg_pnl_pct') or 0:>+.3f}%  "
              f"基准 {m.get('benchmark_return') or 0:>6.2f}%  "
              f"手续费 {m.get('total_fee'):>9.0f}  滑点 {m.get('slippage_cost'):>9.0f}",
              flush=True)
    print("\n本脚本与调优台的差别只在「表达式 vs 数组」，指标/回测函数完全是同一份代码。")
    with open(os.path.join(ROOT, "tools", f"_verify_ui_cfg_{args.pool}_"
                           f"{args.slip}_{args.npos}x{args.ppct}.json"),
              "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)


if __name__ == "__main__":
    main()
