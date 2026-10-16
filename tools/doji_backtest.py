# -*- coding: utf-8 -*-
"""十字星池子的组合回测网格（页面口径）：找「最佳回测配置」。

一次 prepare_data 服务整套便宜参数（max_hold / 止盈 / 止损 / 槽位×仓位 / 滑点都不进
缓存键，见 core/btcache.py 的 _IGNORE_KEYS），所以贵的是「换池 / 换 buy_trigger /
换 sell_signal / 换 score_expr」这四件事，便宜参数随便扫。

卖出只允许两种不踩未来函数的形态：
  * hold —— sell_signal 留空，只按 max_hold 到期（开盘价成交）+ 可选止盈
  * 表达式必须自带 ref(..., >=1)（昨天才知道），否则第 i 天用第 i 天收盘信息按开盘卖

    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tools/doji_backtest.py --pools u_star,d_star
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tools/doji_backtest.py --pools u_star --fine
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

from doji_pools import POOLS  # noqa: E402
from tune_eat1 import TRAIN, VALID, bench  # noqa: E402
from tune_eat3 import FILT  # noqa: E402

START, END = "2023-01-01", "2026-09-18"
YEARS = ("2023", "2024", "2025", "2026")
WINS = {"FULL": (START, END), "TRAIN": TRAIN, "VALID": VALID}
YEAR_WINS = {"2023": ("2023-01-01", "2023-12-31"),
             "2024": ("2024-01-01", "2024-12-31"),
             "2025": ("2025-01-01", "2025-12-31"),
             "2026": ("2026-01-01", END)}

HOLDS = [1, 2, 3, 5, 10]
TPS = [None, 5.0]
SLS = [None]
TIERS = [(10, 10.0), (20, 5.0), (5, 20.0)]
SLIPS = [0.001]
# --fine 再补的便宜轴
HOLDS_FINE = [1, 2, 3, 5, 8, 10, 15, 20]
TPS_FINE = [None, 3.0, 5.0, 8.0, 12.0]
SLS_FINE = [None, -3.0, -5.0]
TIERS_FINE = [(10, 10.0), (20, 5.0), (5, 20.0), (2, 50.0), (30, 3.0)]
SLIPS_FINE = [0.001, 0.002, 0.003]


def run_one(data, cfg, npos, ppct, slip, hold, tp, sl, wins):
    # ⚠ 滑点必须写进 cfg.exec：bt.resolve_params 的优先级是「默认 ← 表单 params ←
    #   策略自带」，exec.slippage 一旦有值就覆盖 params（core/backtest.py:374）。
    #   只改 params 的话整条滑点轴是空转（2026-09-25 踩过：三档滑点结果逐位相同）。
    c2 = {**cfg, "risk": {**cfg["risk"], "max_hold_days": hold,
                          "take_profit_pct": tp, "stop_loss_pct": sl},
          "exec": {**cfg["exec"], "slippage": slip}}
    p = {**BACKTEST_DEFAULTS, "init_cash": 1_000_000, "slippage": slip,
         "max_positions": npos, "position_pct": ppct}
    out = {}
    for name, (s, e) in wins.items():
        out[name] = (bt.run_backtest(data, c2, s, e, p,
                                     benchmark=bench(s, e)).get("metrics") or {})
    return out


def build_cfg(conds, sells=(), buy=([], "open"), score=""):
    b_conds, b_px = buy
    return st.merge_config({
        "timeframe": "daily", "entry": [], "buy_trigger": list(b_conds),
        "sell_signal": list(sells), "score_expr": score,
        "risk": {"stop_loss_pct": None, "take_profit_pct": None,
                 "max_hold_days": 5},
        "exec": {"buy_price": b_px, "sell_price": "open",
                 "max_hold_price": "open", "slippage": 0.001},
        "_pool": uni.parse({"base": "ALL", "where": conds, "filt": FILT}),
    })


def load_data(cfg, conds, note=""):
    """命中磁盘缓存时约 25s；换池/换买卖表达式约 150s。"""
    t0 = time.time()
    info: dict = {}
    codes = market.resolve_universe({"base": "ALL", "where": list(conds)},
                                    start=START, end=END)
    data = bt.prepare_data(codes, START, END, cfg, batch=250, cache_info=info)
    print(f"{note}预筛 {len(codes)} 只 / 有效 {len(data)} 只 / 加载 "
          f"{time.time()-t0:.0f}s / 缓存={info.get('cache')}", flush=True)
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pools", default="")
    ap.add_argument("--score", default="")
    ap.add_argument("--sell", default="hold")
    ap.add_argument("--buy", default="none", choices=list(BUYS))
    ap.add_argument("--fine", action="store_true")
    ap.add_argument("--holds", default="", help="逗号分隔，覆盖默认持有期")
    ap.add_argument("--tps", default="", help="逗号分隔，None 用 n 表示；覆盖默认止盈")
    ap.add_argument("--sls", default="", help="逗号分隔，None 用 n 表示；覆盖默认止损")
    ap.add_argument("--tiers", default="", help="形如 10x10.0,5x20.0；覆盖默认槽位")
    ap.add_argument("--slips", default="", help="逗号分隔；覆盖默认滑点")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--tag", default="c")
    args = ap.parse_args()

    def _num(s):
        return [None if x.strip().lower() == "n" else float(x)
                for x in s.split(",") if x.strip()]

    keys = [k for k in (args.pools.split(",") if args.pools else list(POOLS)) if k]
    sells = SELLS[args.sell] if args.sell in SELLS else []
    b_conds, b_px = BUYS[args.buy]
    wins = dict(WINS) if not args.fine else {**YEAR_WINS, **WINS}
    holds = [int(x) for x in args.holds.split(",") if x] if args.holds else (
        HOLDS_FINE if args.fine else HOLDS)
    tps = _num(args.tps) if args.tps else (TPS_FINE if args.fine else TPS)
    sls = _num(args.sls) if args.sls else (SLS_FINE if args.fine else SLS)
    slips = [float(x) for x in args.slips.split(",") if x] if args.slips else (
        SLIPS_FINE if args.fine else SLIPS)
    if args.tiers:
        tiers = [(int(t.split("x")[0]), float(t.split("x")[1]))
                 for t in args.tiers.split(",") if t]
    else:
        tiers = TIERS_FINE if args.fine else TIERS

    for key in keys:
        conds, note = POOLS[key]
        print(f"\n########## 池 {key}｜{note}｜{len(conds)} 条 ##########", flush=True)
        cfg = build_cfg(conds, sells, (b_conds, b_px), args.score)
        errs = st.validate_config(cfg)
        if errs:
            print("校验失败：", errs)
            continue
        data = load_data(cfg, conds)

        rows = []
        for hold in holds:
            for tp in tps:
                for sl in sls:
                    for npos, ppct in tiers:
                        for slip in slips:
                            m = run_one(data, cfg, npos, ppct, slip, hold, tp, sl, wins)
                            f = m["FULL"]
                            rec = {"hold": hold, "tp": tp, "sl": sl, "npos": npos,
                                   "ppct": ppct, "slip": slip, "w": m}
                            rows.append(rec)
                            print(f"  hold{hold:<3} tp{str(tp):<5} sl{str(sl):<5} "
                                  f"{npos}x{ppct:<5} slip{slip:<6} "
                                  f"F{f.get('annual_return') or 0:>7.2f} "
                                  f"累计{f.get('total_return') or 0:>8.2f} "
                                  f"回撤{f.get('max_drawdown') or 0:>7.2f} "
                                  f"{f.get('trade_count') or 0:>5}笔 "
                                  f"胜{f.get('win_rate') or 0:>5.1f} "
                                  f"| T{m['TRAIN'].get('annual_return') or 0:>7.2f} "
                                  f"| V{m['VALID'].get('annual_return') or 0:>7.2f}",
                                  flush=True)

        # 稳健性排序：TRAIN 与 VALID 都要为正，再看 FULL
        good = [r for r in rows
                if (r["w"]["TRAIN"].get("annual_return") or -1) > 0
                and (r["w"]["VALID"].get("annual_return") or -1) > 0]
        good.sort(key=lambda r: -(r["w"]["FULL"].get("annual_return") or -99))
        print(f"\n  === {key}：样本内外都为正的配置里按 FULL 年化取前 {args.top} ===")
        for r in good[:args.top]:
            f = r["w"]["FULL"]
            yrs = " ".join(f"{(r['w'].get(y) or {}).get('annual_return') or 0:>6.1f}"
                           for y in YEARS) if args.fine else ""
            print(f"   hold{r['hold']:<3} tp{str(r['tp']):<5} sl{str(r['sl']):<5} "
                  f"{r['npos']}x{r['ppct']:<5} slip{r['slip']:<6} "
                  f"F{f.get('annual_return'):>7.2f} 回撤{f.get('max_drawdown'):>7.2f} "
                  f"{f.get('trade_count'):>5}笔 {yrs}")
        with open(os.path.join(ROOT, "tools",
                               f"_doji_bt|{key}|{args.sell}|{args.buy}|"
                               f"{args.score or 'nos'}|{'fine' if args.fine else 'coarse'}"
                               f"|{tag}.json"),
                  "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=1, default=str)


SELLS = {"hold": [], "ma20": ["ref(close, 1) < ref(ma20, 1)"]}
# 次日确认（买入触发）。引擎在第 i 天用第 i 天的 buy_trigger 决定买入、按 exec.buy_price
# 成交（core/backtest.py:「t 日 entry 成立 → t+1 日买入候选」），所以：
#   open_*  只能用开盘前就知道的信息（开盘价），配 buy_price=open 才不是未来函数；
#   close_* 要求收盘站稳，必须配 buy_price=close（收盘成交）。
BUYS = {
    "none": ([], "open"),
    "gapopen": (["open >= ref(close, 1)"], "open"),     # 星线次日不低开才买
    "break": (["open > ref(high, 1)"], "open"),          # 次日直接高开过星线最高
    "close_conf": (["close > ref(high, 1)"], "close"),   # 次日收盘突破星线高点（尾盘买）
}

if __name__ == "__main__":
    main()
