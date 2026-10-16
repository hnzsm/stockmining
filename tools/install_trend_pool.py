# -*- coding: utf-8 -*-
"""新增一条「趋势票（启动初段）」选股策略（pool_strategy），走 app 自己的写入入口。

只 **INSERT 新记录**，不碰 #156 / #58。同名不重复插入（uk_name）。

    python tools/install_trend_pool.py             # 打印将要写入的内容 + 入库前校验
    python tools/install_trend_pool.py --commit    # 真正写入
    python tools/install_trend_pool.py --verify    # 从库里读回来：最新交易日命中几只、
                                                   # 并按页面口径跑一遍区间回测
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from config import BACKTEST_DEFAULTS  # noqa: E402
from core import backtest as bt, db, market, universe as uni  # noqa: E402
from core import pool, strategy as st  # noqa: E402

from tune_eat1 import TRAIN, VALID, bench  # noqa: E402
from tune_eat3 import FILT  # noqa: E402

NAME = "趋势票启动初段"
NOTE = ("趋势票筛选（2026-09-21 由 tools/trend_pool_lab.py 四轮前向收益研究选出）。"
        "口径：均线多头已成立 10 日且 20 日前还未多头 + 收盘价逼近 60 日新高 + "
        "当日收在日内高位（close_pos>0.7）+ 不涨停、当日涨幅<7% + 周线 20 周均线向上。"
        "含义是「趋势刚走出第一段、但当日没被炒到板上」的启动初段，不是趋势中后段"
        "（ret60>60% 的对照组持 20 日超额 −2.2%，明确有害）。"
        "⚠ 这只给**相对强弱**（同一天里比别的票强）：2023 与 2026 上半年全市场等权"
        "持 20 日的绝对收益本身就是负的（−0.14% / −0.68%），任何满仓做多在这些窗口都亏。"
        "卖出规则写在买卖策略的 sell_signal 里 —— 引擎卖出已位移一根（T 日收盘确认、"
        "T+1 执行），不会用未来函数。选股策略只管选股。")
CONDS = [
    "ma_bull = 1",
    "count(ma_bull, 10) = 10",
    "ref(ma_bull, 20) = 0",
    "close >= ref(hh60, 1) * 0.97",
    "pos120 > 0.6",
    "close_pos > 0.7",
    "limit_up = 0",
    "pct_chg < 7",
    "w_close > w_ma20",
    "w_ma20 > ref(w_ma20, 4)",
    "between(close, 5, 100)",
    "total_mv > 300000",
]
START, END = "2023-01-01", "2026-09-18"
WINS = {"2023": ("2023-01-01", "2023-12-31"),
        "2024": ("2024-01-01", "2024-12-31"),
        "2025": ("2025-01-01", "2025-12-31"),
        "2026": ("2026-01-01", END),
        "TRAIN": TRAIN, "VALID": VALID, "FULL": (START, END)}


def existing():
    return db.query("SELECT id FROM pool_strategy WHERE name=%s", (NAME,))


def verify(npos: int = 10, ppct: float = 10.0, hold: int = 20,
           slip: float = 0.001):
    rows = existing()
    if not rows:
        sys.exit("尚未入库：先跑 --commit")
    pid = int(rows[0]["id"])
    spec = uni.parse({"pool_id": pid})
    print(f"pool_strategy #{pid}「{spec['pool_name']}」条件 {len(spec['where'])} 条")
    cfg = st.merge_config({
        "timeframe": "daily", "entry": [], "buy_trigger": [], "sell_signal": [],
        "score_expr": "",
        "risk": {"stop_loss_pct": None, "take_profit_pct": None,
                 "max_hold_days": hold},
        "exec": {"buy_price": "open", "sell_price": "open",
                 "max_hold_price": "open", "slippage": slip},
        "_pool": spec})
    errs = st.validate_config(cfg)
    print("校验：", errs or "通过")
    if errs:
        return
    params = bt.resolve_params(cfg, {**BACKTEST_DEFAULTS, "init_cash": 1_000_000,
                                     "max_positions": npos, "position_pct": ppct,
                                     "slippage": slip})
    codes = market.resolve_universe(spec, start=START, end=END)
    info: dict = {}
    data = bt.prepare_data(codes, START, END, cfg, batch=250, cache_info=info)
    print(f"预筛 {len(codes)} 只 / 有效 {len(data)} 只 / 缓存={info.get('cache')}"
          f"｜参数：{npos}×{ppct}% 持有 {hold} 日 滑点 {params['slippage']}", flush=True)
    for name, (s, e) in WINS.items():
        m = bt.run_backtest(data, cfg, s, e, params,
                            benchmark=bench(s, e)).get("metrics") or {}
        print(f"  {name:<6} 年化 {m.get('annual_return'):>7.2f}%  "
              f"累计 {m.get('total_return'):>8.2f}%  回撤 {m.get('max_drawdown'):>7.2f}%  "
              f"{m.get('trade_count'):>5}笔  胜率 {m.get('win_rate') or 0:>5.1f}%  "
              f"基准 {m.get('benchmark_return') or 0:>6.2f}%", flush=True)
    # 最新交易日实际命中几只：走 app 的选股入口（save=False，不落 screen_result）
    from core import screener
    td = market.latest_trade_date()
    res = screener.screen(cfg, universe=spec, trade_date=td, limit=500,
                          save=False)
    items = res.get("items") or []
    print(f"最新交易日 {td} 命中 {len(items)} 只："
          + "、".join(f"{i.get('name') or ''}({i.get('ts_code')})" for i in items[:12]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--npos", type=int, default=10)
    ap.add_argument("--ppct", type=float, default=10.0)
    ap.add_argument("--hold", type=int, default=20)
    ap.add_argument("--slip", type=float, default=0.001)
    args = ap.parse_args()
    if args.verify:
        verify(args.npos, args.ppct, args.hold, args.slip)
        return
    errs = pool.validate(NAME, "ALL", CONDS)
    cfg = st.merge_config({"_pool": uni.parse({"base": "ALL", "where": CONDS,
                                               "filt": FILT})})
    errs += st.validate_config(cfg)
    print("入库前校验：", "；".join(errs) or "通过")
    if errs:
        return
    dup = existing()
    print(f"选股策略 {NAME!r}：{'已存在 id=' + str(dup[0]['id']) if dup else '待新建'}")
    print("\npool_strategy.conds =", json.dumps(CONDS, ensure_ascii=False))
    print("pool_strategy.filt =", json.dumps(FILT, ensure_ascii=False))
    if dup:
        print("\n同名记录已存在（uk_name 唯一），不重复插入。跑 --verify 复核。")
        return
    if not args.commit:
        print("\n以上是将要写入的内容，确认无误后加 --commit")
        return
    pid = pool.save_pool(NAME, NOTE, "ALL", CONDS, filt=FILT)
    print(f"\n已入库：pool_strategy id={pid}「{NAME}」")


if __name__ == "__main__":
    main()
