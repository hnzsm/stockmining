# -*- coding: utf-8 -*-
"""给「暴涨前夜」票池配一条买卖策略，并在平台上跑回测。

策略形状（针对高赔率低胜率的票池，裸持有中位数是负的，必须右侧止盈）：
  次日开盘买入 → 盘中 +30% 止盈 / -8% 止损 / 最长持 10 根收盘走。
不写 buy_trigger / sell_signal（选股条件归池子，买卖只管进出场与风控）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import strategy as st  # noqa: E402

BASE = "http://127.0.0.1:8777"
NAME = "暴涨前夜·止盈30止损8持10"


def api(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=120).read().decode())


def ensure_strategy(tp: float, sl: float, hold: int) -> int:
    t = f"止盈{int(tp)}" if tp else "不止盈"
    s = f"止损{int(-sl)}" if sl else "不止损"
    name = f"暴涨前夜·{t}{s}持{hold}"
    for s_ in st.list_strategies():
        if s_["name"] == name:
            return s_["id"]
    cfg = st.merge_config({
        "timeframe": "daily",
        "entry": [], "entry_any": [], "weekly_filter": [],
        "buy_trigger": [],
        "sell_signal": [],
        "exec": {"buy_price": "open", "sell_price": "open",
                 "max_hold_price": "close", "slippage": None},
        "risk": {"max_hold_days": hold,
                 "stop_loss_pct": (float(sl) if sl else None),
                 "take_profit_pct": (float(tp) if tp else None)},
        "score_expr": "",
        "indicators": {},
    })
    errs = st.validate_config(cfg)
    if errs:
        raise SystemExit("策略校验失败: " + "；".join(errs))
    sid = st.save_strategy(name, f"配套「暴涨前夜」票池：次日开盘买，{t}、{s}，"
                                 f"最长持 {hold} 根收盘走。", cfg)
    print("新建买卖策略", sid, name)
    return sid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", type=int, default=274)
    ap.add_argument("--tp", type=float, default=30.0)
    ap.add_argument("--sl", type=float, default=-8.0)
    ap.add_argument("--hold", type=int, default=10)
    ap.add_argument("--segs", default="2025-01-01:2025-12-31,"
                                      "2026-01-01:2026-09-28,"
                                      "2025-01-01:2026-09-28")
    ap.add_argument("--submit-only", action="store_true")
    args = ap.parse_args()

    sid = ensure_strategy(args.tp, args.sl, args.hold)
    pools = {p["id"]: p["name"] for p in _pools()}
    pname = pools.get(args.pool, str(args.pool))
    for seg in args.segs.split(","):
        d0, d1 = seg.split(":")
        t = f"止盈{int(args.tp)}" if args.tp else "不止盈"
        s = f"止损{int(-args.sl)}" if args.sl else "不止损"
        nm = f"{pname} · {t}{s}持{args.hold} · {d0[:4]}-{d1[:4]}"
        r = api("POST", "/api/backtest/start",
                {"strategy_id": sid, "universe": {"pool_id": args.pool},
                 "start": d0, "end": d1, "name": nm})
        rid = (r.get("data") or {}).get("run_id") or r.get("run_id")
        print(f"提交 {nm}  run={rid}")
        if args.submit_only:
            continue
        t0 = time.time()
        while True:
            d = api("GET", f"/api/backtest/runs/{rid}")["data"]
            if d["status"] in ("done", "error", "canceled"):
                break
            time.sleep(5)
        m = d.get("metrics") or {}
        sr = m.get("sell_reason_stat") or {}
        print(f"   {d['status']}  收益 {m.get('total_return')}%  "
              f"基准 {m.get('benchmark_return')}%  回撤 {m.get('max_drawdown')}%  "
              f"{m.get('trade_count')} 笔  胜率 {m.get('win_rate')}%  "
              f"止盈{sr.get('take_profit')} 止损{sr.get('stop_loss')} "
              f"到期{sr.get('max_hold')}  {time.time()-t0:.0f}s")


def _pools():
    return api("GET", "/api/pools")["data"]


if __name__ == "__main__":
    main()
