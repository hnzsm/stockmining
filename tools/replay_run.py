# -*- coding: utf-8 -*-
"""按 app 的组 cfg 顺序复现某条 backtest_run 记录，用来对账「页面数字从哪来」。

    python tools/replay_run.py 58 59 60
    python tools/replay_run.py 60 --score "ref(upper_shadow,1) - ref(lower_shadow,1)"

--score 只覆盖内存里的 score_expr（评分表达式），不写库。
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
from core import strategy as st  # noqa: E402

from tune_eat1 import TRAIN, VALID, bench  # noqa: E402

WINS = {"2023": ("2023-01-01", "2023-12-31"),
        "2024": ("2024-01-01", "2024-12-31"),
        "2025": ("2025-01-01", "2025-12-31"),
        "2026": ("2026-01-01", "2026-09-18"),
        "TRAIN": TRAIN, "VALID": VALID}


def replay(rid: int, score: str | None, where_override: list | None = None):
    run = db.query_one("SELECT * FROM backtest_run WHERE id=%s", (rid,))
    if not run:
        sys.exit(f"run {rid} 不存在")
    spec = json.loads(run["universe_spec"])
    params = json.loads(run["params"]) if isinstance(run["params"], str) \
        else dict(run["params"])
    stored = json.loads(run["metrics"]) if isinstance(run["metrics"], str) \
        else dict(run["metrics"])
    start, end = str(run["start_date"]), str(run["end_date"])

    if where_override is not None:
        spec = {**spec, "where": where_override, "pool_id": None}
    cfg = st.get_strategy(int(run["strategy_id"]))["config"]
    if score is not None:
        cfg["score_expr"] = score
    parsed = uni.parse(spec)
    cfg = st.merge_config({**cfg, "_pool": parsed})
    errs = st.validate_config(cfg)
    print(f"\n===== run #{rid}（{run['created_at']}）复现 =====")
    print(f"  where={json.dumps(spec.get('where'), ensure_ascii=False)}")
    print(f"  score_expr={cfg.get('score_expr') or '（空）'}  校验：{'；'.join(errs) or '通过'}")
    if errs:
        return
    p = bt.resolve_params(cfg, {**BACKTEST_DEFAULTS, **params})
    print(f"  持仓 {p['max_positions']}×{p['position_pct']}% 滑点 {p['slippage']} "
          f"止盈 {p.get('take_profit_pct')} max_hold {p.get('max_hold_days')}")
    codes = market.resolve_universe(spec, start=start, end=end)
    data = bt.prepare_data(codes, start, end, cfg, batch=250)
    print(f"  候选 {len(codes)} 只 / 有效 {len(data)} 只", flush=True)
    m = bt.run_backtest(data, cfg, start, end, p,
                        benchmark=bench(start, end)).get("metrics") or {}
    print(f"  {'':<6} {'年化':>8} {'累计':>10} {'回撤':>9} {'笔数':>6} "
          f"{'胜率':>7} {'均笔':>8}")
    for tag, mm in (("库里", stored), ("复现", m)):
        print(f"  {tag:<6} {mm.get('annual_return'):>8.2f} "
              f"{mm.get('total_return'):>10.2f} {mm.get('max_drawdown'):>9.2f} "
              f"{mm.get('trade_count'):>6} {mm.get('win_rate'):>7.2f} "
              f"{mm.get('avg_pnl_pct') or 0:>+8.3f}")
    for name, (s, e) in WINS.items():
        y = bt.run_backtest(data, cfg, s, e, p, benchmark=bench(s, e)
                            ).get("metrics") or {}
        print(f"  {name:<6} 年化 {y.get('annual_return'):>8.2f}%  "
              f"累计 {y.get('total_return'):>9.2f}%  回撤 {y.get('max_drawdown'):>8.2f}%  "
              f"{y.get('trade_count'):>5}笔", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", type=int, nargs="+")
    ap.add_argument("--score", default=None)
    ap.add_argument("--where", default=None,
                    help="JSON 数组，覆盖该 run 的条件列表（测「表达式写进评分框」的正确口径）")
    args = ap.parse_args()
    wo = json.loads(args.where) if args.where else None
    for rid in args.ids:
        replay(rid, args.score, wo)


if __name__ == "__main__":
    main()
