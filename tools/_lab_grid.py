# -*- coding: utf-8 -*-
"""网格搜索：现有**选股策略** × 多组**买卖规则**，找收益为正的组合。

口径与页面回测完全一致：
  · 选股 = 库里现有的 pool_strategy（**只读**，不增不删，按 id 引用）
  · 买卖 = 这里定义的变体（信号形状 × 风控参数）
  · 时序：T 收盘入池 → T+1 开盘满足 buy_trigger 才买 → 持仓期逐日判 sell_signal
    （开盘价）/ 止损止盈（盘中）/ 到期（收盘价）

**为什么分「形状 / 参数」两层**：
  `prepare_data`（加载 + 指标 + 信号）占掉 99% 的时间，而它的缓存键只含
  「池 + 区间 + 信号条件」（`core/btcache._IGNORE_KEYS`）—— `risk`（持有/止损/止盈）
  与 `exec.slippage` **不进键**。所以：
    · 形状（buy_trigger/sell_signal）= 贵，每个池算一次（全市场 1 年 ≈ 130s）
    · 参数（hold/止损/止盈）= 便宜，同形状下随便扫（每次 ≈ 6s）

跑法（多进程分片，实测 20 核上并发不减速）：
    python tools/_lab_grid.py --shapes S0,S1,S4 --shard 1/6
    ...
    python tools/_lab_grid.py --shapes S0,S1,S4 --shard 6/6
    python tools/_lab_report.py                 # 合并出榜
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from config import BACKTEST_DEFAULTS  # noqa: E402
from core import backtest as bt, db, market, strategy as st  # noqa: E402

OUT_DIR = os.path.join(ROOT, "tools")

# 仓位/成本：与 tools/auto_strategies.py 同口径，别改（改了就不是同一个口径）
PARAMS = {**BACKTEST_DEFAULTS, "init_cash": 1_000_000, "max_positions": 10,
          "position_pct": 10.0}

OPEN_UP = "open > ref(close, 1)"      # 高开
OPEN_DN = "open < ref(close, 1)"      # 低开


def _shape(key, name, buy, sell, note=""):
    return {"key": key, "name": name, "buy": list(buy), "sell": list(sell),
            "note": note}


SHAPES = [
    _shape("S0", "次日无条件买入", [], [], "纯持有到期：考验池子本身的「次日效应」"),
    _shape("S1", "高开确认买入", [OPEN_UP], [], "顺势能买才买"),
    _shape("S2", "低开买入", [OPEN_DN], [], "回踩接，赌均值回归"),
    _shape("S3", "高开买·跌破MA5走", [OPEN_UP], ["close < ma5"], "最短均线离场"),
    _shape("S4", "高开买·跌破MA10走", [OPEN_UP], ["close < ma10"], ""),
    _shape("S5", "高开买·跌破MA20走", [OPEN_UP], ["close < ma20"], ""),
    _shape("S6", "高开买·次日低开走", [OPEN_UP], [OPEN_DN], "隔日转弱就跑"),
    _shape("S7", "无条件买·低开走", [], [OPEN_DN], ""),
    _shape("S8", "高开买·收盘转阴走", [OPEN_UP], ["close < open"], ""),
]

# (持有天数, 止损%, 止盈%) —— 不进缓存键，扫描便宜
TUNES = [
    {"hold": 2, "sl": None, "tp": None},
    {"hold": 3, "sl": None, "tp": None},
    {"hold": 5, "sl": None, "tp": None},
    {"hold": 10, "sl": None, "tp": None},
    {"hold": 20, "sl": None, "tp": None},
    {"hold": 5, "sl": -5, "tp": 10},
    {"hold": 10, "sl": -8, "tp": 15},
    {"hold": 20, "sl": -8, "tp": 20},
]


def load_pools():
    rows = db.query("SELECT id, name, base, conds, pool_exit FROM pool_strategy ORDER BY id")
    out = []
    for r in rows:
        conds = json.loads(r["conds"]) if isinstance(r["conds"], str) else (r["conds"] or [])
        out.append({"id": r["id"], "name": r["name"], "base": (r["base"] or "all").upper()
                    if str(r["base"]).lower() == "all" else r["base"],
                    "conds": conds, "pool_exit": int(r["pool_exit"] or 0)})
    return out


def load_strategy_shapes():
    """把库里现成的**买卖策略**也当成「形状」来跑（key = E<id>）。

    它们的 `risk`/`exec` 是策略自带口径（如「吃一口就走」滑点千五、持 1 日），
    所以标 `fixed`：不再叠网格里的 tune，原样跑。
    """
    out = []
    for s in db.query("SELECT id, name, config FROM strategy ORDER BY id"):
        cfg = json.loads(s["config"]) if isinstance(s["config"], str) else (s["config"] or {})
        out.append({"key": f"E{s['id']}", "name": f"现有#{s['id']} {s['name']}",
                    "buy": list(cfg.get("buy_trigger") or []),
                    "sell": list(cfg.get("sell_signal") or []),
                    "fixed": True,
                    "cfg": {k: cfg.get(k) for k in
                            ("buy_trigger", "sell_signal", "risk", "exec", "score_expr")
                            if cfg.get(k) is not None}})
    return out


ALL_SHAPES = SHAPES + []  # 运行时按需追加 E*


def run_one(pool, shape, tune, start, end, slippage=0.001, data=None, holder=None):
    """跑一格。

    ⚠ `data` 可复用：`risk`/`slippage` **不进** prepare 的缓存键（见 `core/btcache`
    的 `_IGNORE_KEYS`），所以同「池×形状」的 data 对**所有**风控参数都成立。
    于是 main 里按 (池,形状,滑点) prepare **一次**、内存复用，`run_backtest` 从不写
    `data` —— 比落盘更快，也避开磁盘缓存写满后的淘汰删除（见 `--cache` 注释）。
    """
    if shape.get("fixed"):
        # 现有买卖策略：用它自带的 buy/sell + risk + exec，不再叠网格参数
        cfg = st.merge_config(shape.get("cfg") or {})
        risk = cfg.get("risk") or {}
        tune = {"hold": risk.get("max_hold_days"), "sl": risk.get("stop_loss_pct"),
                "tp": risk.get("take_profit_pct")}
        slippage = (cfg.get("exec") or {}).get("slippage") or slippage
    else:
        cfg = st.merge_config({
            "buy_trigger": shape["buy"],
            "sell_signal": shape["sell"],
            "risk": {"max_hold_days": tune["hold"], "stop_loss_pct": tune["sl"],
                     "take_profit_pct": tune["tp"]},
            "exec": {"buy_price": "open", "sell_price": "open", "max_hold_price": "close",
                     "slippage": slippage},
        })
    run_cfg = {**cfg, "_pool": {"base": pool["base"], "where": list(pool["conds"]),
                                "pool_exit": bool(pool["pool_exit"]),
                                "pool_id": pool["id"], "pool_name": pool["name"]}}
    errs = st.validate_config(run_cfg)
    if errs:
        return {"error": "；".join(errs)}

    codes = market.resolve_universe(pool["base"])
    if data is None:
        t0 = time.time()
        ci = {}
        data = bt.prepare_data(codes, start, end, run_cfg, batch=300, cache_info=ci)
        prep, cache = time.time() - t0, ci.get("cache", "?")
        if holder is not None:
            holder["data"] = data        # 同 (池,形状) 的下几档参数直接复用
    else:
        prep, cache = 0.0, "reuse"
    t1 = time.time()
    res = bt.run_backtest(data, run_cfg, start, end, PARAMS)
    m = res.get("metrics") or {}
    return {
        "pool_id": pool["id"], "pool": pool["name"], "shape": shape["key"],
        "shape_name": shape["name"], "buy": shape["buy"], "sell": shape["sell"],
        "hold": tune["hold"], "sl": tune["sl"], "tp": tune["tp"], "slip": slippage,
        "codes": len(codes), "cache": cache,
        "prep_s": round(prep, 1), "bt_s": round(time.time() - t1, 1),
        "metrics": m,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--end", default="2025-12-31")
    ap.add_argument("--pools", default="")
    ap.add_argument("--shapes", default="")
    ap.add_argument("--tunes", default="")
    ap.add_argument("--slip", default="0.001")
    ap.add_argument("--shard", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--cache", action="store_true",
                    help="允许写/读磁盘回测缓存（默认**关**：缓存目录 3000MB 已逼近上限，"
                         "再写会触发 LRU 淘汰删文件，而删文件被环境的删除保护拦下、进程直接死）")
    args = ap.parse_args()
    if not args.cache:
        from core import btcache
        btcache.ENABLED = False   # 只影响本进程；网页回测照旧用缓存

    pools = load_pools()
    if args.pools:
        keep = {int(x) for x in args.pools.split(",") if x.strip()}
        pools = [p for p in pools if p["id"] in keep]
    shapes = SHAPES
    if args.shapes:
        if args.shapes.strip().upper() == "ALL":
            shapes = SHAPES + load_strategy_shapes()
        else:                # ⚠ 按 **key 精确** 匹配：子串匹配会把 S1 也命中 S10
            keys = {k.strip().upper() for k in args.shapes.split(",") if k.strip()}
            pool_shapes = {s["key"]: s for s in SHAPES}
            if any(k.startswith("E") for k in keys):
                for s in load_strategy_shapes():
                    pool_shapes[s["key"]] = s
            shapes = [pool_shapes[k] for k in keys if k in pool_shapes]
    tunes = TUNES
    if args.tunes:
        idx = [int(x) for x in args.tunes.split(",") if x.strip()]
        tunes = [TUNES[i] for i in idx]
    if args.shard:
        i, n = (int(x) for x in args.shard.split("/"))
        pools = [p for k, p in enumerate(pools) if k % n == i - 1]
    slips = [float(x) for x in args.slip.split(",") if x.strip()]
    out = args.out or os.path.join(OUT_DIR, f"_lab_grid{args.shard.replace('/', '_')}.json")

    print(f"分片 {args.shard or '1/1'}：池 {len(pools)} × 形状 {len(shapes)} "
          f"× 参数 {len(tunes)} × 滑点 {len(slips)}   区间 {args.start}~{args.end}")

    rows = []
    t_all = time.time()
    for p in pools:
        for s in shapes:
            # 现有策略（fixed）：口径自带，只跑一次，不再叠网格参数
            _slips = [None] if s.get("fixed") else slips
            _tunes = [None] if s.get("fixed") else tunes
            for slip in _slips:
                holder: dict = {}     # 同「池×形状×滑点」只 prepare 一次，内存复用
                for t in _tunes:
                    r = run_one(p, s, t, args.start, args.end, slip,
                                data=holder.get("data"), holder=holder)
                    if r.get("error"):
                        print(f"  !! 池#{p['id']} {p['name']} / {s['key']} 失败：{r['error']}")
                        continue
                    m = r["metrics"]
                    rows.append(r)
                    print(f"  #{p['id']:<4}{p['name'][:16]:<18}{s['key']} "
                          f"hold={str(r['hold'] or '-'):<4}"
                          f" 收益 {m.get('total_return', 0):>7.2f}% "
                          f"超额 {m.get('alpha') if m.get('alpha') is not None else 0:>7.2f}% "
                          f"笔数 {m.get('trade_count', 0):>5} 胜率 {m.get('win_rate', 0):>5.1f}% "
                          f"回撤 {m.get('max_drawdown', 0):>6.2f}% "
                          f"[prep {r['prep_s']}s {r['cache']}]", flush=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"range": f"{args.start}~{args.end}", "rows": rows}, f,
                      ensure_ascii=False, indent=1, default=str)

    print(f"\n分片 {args.shard} 完成：{len(rows)} 组，耗时 {time.time()-t_all:.0f}s → {out}")


if __name__ == "__main__":
    main()
