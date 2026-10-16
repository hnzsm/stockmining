# -*- coding: utf-8 -*-
"""「吃一口就走」（池#5 × 买卖策略#31）参数调优台。

思路：
  * prepare_data 的缓存键不含 risk / exec / 仓位参数（见 core/btcache），
    所以「同信号、只改参数」可以整块复用 run38 的 268MB 缓存产物，单次回测秒级。
  * 评分窗口切分：TRAIN=2023-01-01~2025-12-31（调参），VALID=2026-01-01~今（复核），
    FULL=全程。信号数组在同一份全区间历史上只算一次，切窗口只是切片，无未来函数。
  * 结构变体（改池条件 / 买卖表达式）会换缓存键 → 需要重算（全市场约 5 分钟/个），
    放在 stage B，用 --variant 指定。

用法:
    python tools/tune_eat1.py --check            # 复现 run38，校验口径
    python tools/tune_eat1.py --stage-a 1200     # 随机搜参（train 评分）
    python tools/tune_eat1.py --report           # 重放 stage A 结果表
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import random
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from config import BACKTEST_DEFAULTS  # noqa: E402
from core import backtest as bt, db, market, universe as uni  # noqa: E402
from core import strategy as st  # noqa: E402

OUT_JSON = os.path.join(ROOT, "tools", "_tune_eat1_result.json")
CODES_JSON = os.path.join(ROOT, "tools", "_tune_eat1_codes.json")

POOL_ID = 5
STRATEGY_ID = 31
START, END = "2023-01-01", "2026-09-18"
TRAIN = ("2023-01-01", "2025-12-31")
VALID = ("2026-01-01", "2026-09-18")


def base_cfg() -> dict:
    """还原 run38 提交时的 cfg（与 app.api_backtest_start 同一顺序）。"""
    s = st.get_strategy(STRATEGY_ID)
    cfg = s["config"]
    spec = uni.parse({"pool_id": POOL_ID})
    cfg = {**cfg, "_pool": spec}
    return st.merge_config(cfg)


def load_data(cfg, cache_info=None):
    spec = {"pool_id": POOL_ID}
    tag = f"{POOL_ID}|{START}|{END}|{market.latest_trade_date()}"
    if os.path.isfile(CODES_JSON):
        j = json.load(open(CODES_JSON, encoding="utf-8"))
        if j.get("tag") == tag:
            codes = j["codes"]
        else:
            codes = None
    else:
        codes = None
    if codes is None:
        codes = market.resolve_universe(spec, start=START, end=END)
        with open(CODES_JSON, "w", encoding="utf-8") as f:
            json.dump({"tag": tag, "codes": codes}, f)
    data = bt.prepare_data(codes, START, END, cfg, batch=250,
                           cache_info=cache_info)
    return data


def prune(data: dict, cfg) -> dict:
    """只保留「entry 与 buy_trigger 都可能成立」的票 —— 日循环是 O(天数×标的数)，
    4981 只里绝大多数永远进不了候选。裁剪结果与全量逐笔一致
    （基准由外部传入，不走 data 的等权兜底）。"""
    keep = {}
    for code, v in data.items():
        if (v["entry"] & v["in_pool"] & v["buy_trigger"]).any() or \
           (v["entry"] & v["in_pool"]).any():
            keep[code] = v
    return keep


def bench(start: str, end: str) -> dict:
    rows = db.query(
        "SELECT trade_date, close FROM index_daily WHERE ts_code='000300.SH' "
        "AND trade_date BETWEEN %s AND %s ORDER BY trade_date", (start, end))
    return {str(r["trade_date"])[:10]: float(r["close"]) for r in rows if r["close"]}


def make_run_cfg(cfg: dict, q: dict) -> tuple[dict, dict]:
    """把调优向量拆成 (策略cfg覆盖, 回测params覆盖)。"""
    c = json.loads(json.dumps(cfg, default=str))
    c["risk"] = {
        "stop_loss_pct": q.get("stop"),
        "take_profit_pct": q.get("tp"),
        "max_hold_days": q.get("hold"),
    }
    c["exec"] = {
        "buy_price": q.get("buy_px", "open"),
        "sell_price": q.get("sell_px", "open"),
        "max_hold_price": q.get("hold_px", "close"),
        "slippage": None,   # 滑点走 params，便于对比
    }
    c["_pool"]["pool_exit"] = bool(q.get("pool_exit"))
    c["pool"]["pool_exit"] = bool(q.get("pool_exit"))
    p = {**BACKTEST_DEFAULTS,
         "init_cash": 1_000_000,
         "slippage": q.get("slip", 0.005),
         "max_positions": q.get("npos", 10),
         "position_pct": q.get("ppct", 10)}
    return c, p


def evaluate(data, cfg, q, windows) -> dict:
    c, p = make_run_cfg(cfg, q)
    out = {}
    for name, (s, e) in windows.items():
        res = bt.run_backtest(data, c, s, e, p, benchmark=bench(s, e))
        m = res.get("metrics") or {}
        out[name] = {k: m.get(k) for k in
                     ("total_return", "annual_return", "max_drawdown", "trade_count",
                      "win_rate", "sharpe", "profit_factor", "avg_pnl_pct",
                      "benchmark_return", "slippage_cost", "total_fee")}
    return out


SPACE = {
    "pool_exit": [True, False],
    "stop":      [None, -1, -2, -3, -4, -6],
    "tp":        [None, 1, 1.5, 2, 3, 5, 8],
    "hold":      [1, 2, 3, 5, 8],
    "buy_px":    ["open", "close"],
    "sell_px":   ["open", "close"],
    "hold_px":   ["close", "open"],
    "slip":      [0.005, 0.002],
    "npos":      [5, 10],
    "ppct":      [10],
}


def sample(rng: random.Random) -> dict:
    return {k: rng.choice(v) for k, v in SPACE.items()}


def fmt(row: dict) -> str:
    q, m = row["q"], row["metrics"]["TRAIN"]
    v = row["metrics"].get("VALID") or {}
    return (f"年化 {m.get('annual_return'):>7.2f}%  回撤 {m.get('max_drawdown'):>7.2f}%  "
            f"{m.get('trade_count'):>4}笔  胜率 {m.get('win_rate') or 0:>5.1f}%  | "
            f"VALID {str(v.get('annual_return')):>8}%  | "
            f"exit={int(bool(q['pool_exit']))} sl={q['stop']} tp={q['tp']} h={q['hold']} "
            f"b={q['buy_px'][0]} s={q['sell_px'][0]} e={q['hold_px'][0]} "
            f"slip={q['slip']} n={q['npos']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--stage-a", type=int, default=0, help="随机搜参次数")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    if args.report:
        rows = json.load(open(OUT_JSON, encoding="utf-8"))["rows"]
        rows.sort(key=lambda r: -(r["metrics"]["TRAIN"].get("annual_return") or -100))
        for r in rows[:40]:
            print(fmt(r))
        return

    t0 = time.time()
    cache_info: dict = {}
    cfg = base_cfg()
    data = load_data(cfg, cache_info)
    print(f"数据就绪 {len(data)} 只 cache={cache_info.get('cache')} "
          f"({time.time()-t0:.1f}s)", flush=True)
    n_full = len(data)
    data = prune(data, cfg)
    print(f"裁剪后 {len(data)}/{n_full}", flush=True)

    if args.check:
        q = {"pool_exit": True, "stop": None, "tp": None, "hold": 1,
             "buy_px": "open", "sell_px": "open", "hold_px": "close",
             "slip": 0.005, "npos": 10, "ppct": 10}
        m = evaluate(data, cfg, q, {"FULL": (START, END)})["FULL"]
        print("复现 run38：", json.dumps(m, ensure_ascii=False), flush=True)
        print("期望： total_return=-14.8424 annual=-4.2387 dd=-15.7403 笔数=126 胜率=36.51")
        return

    rows = []
    rng = random.Random(args.seed)
    t0 = time.time()
    for i in range(args.stage_a):
        q = sample(rng)
        try:
            m = evaluate(data, cfg, q, {"TRAIN": TRAIN, "VALID": VALID})
        except Exception as e:  # noqa: BLE001
            print(f"  !! {q} → {type(e).__name__}: {e}")
            continue
        rows.append({"q": q, "metrics": m})
        if i % 50 == 0:
            print(f"[{i+1}/{args.stage_a}] {time.time()-t0:.0f}s  {fmt(rows[-1])}",
                  flush=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump({"space": {k: [str(x) for x in v] for k, v in SPACE.items()},
                   "rows": rows}, f, ensure_ascii=False, indent=1)
    print(f"\n共 {len(rows)} 次，用时 {time.time()-t0:.0f}s → {OUT_JSON}")
    rows.sort(key=lambda r: -(r["metrics"]["TRAIN"].get("annual_return") or -100))
    print("\nTOP 20（按 TRAIN 年化）：")
    for r in rows[:20]:
        print(fmt(r))


if __name__ == "__main__":
    main()
