# -*- coding: utf-8 -*-
"""用**真引擎**扫同一池（#156）的买卖侧变体。

⚠ 性能与安全口径（照 C16/C26）：
  * `risk` / `exec` **不进** btcache 键 ⇒ 只改这两样的变体全部命中同一份 prepare（几乎零成本）；
  * 会改 **信号条件**（`buy_trigger` / 池条件）的变体才需要重算 prepare —— 那种每组单独跑、
    且**只跑少数几个**（缓存目录已用 2.3G / 上限 3G，多写会触发淘汰删除）。
  * 不写库（落库另外走平台接口）。

用法：
    python tools/_selfrein_engine.py --pool 156 --start 2023-01-01 --end 2026-09-18 --stage risk
    python tools/_selfrein_engine.py --pool 156 --stage entry
"""
import sys, os, argparse, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from core import backtest as bt, market, strategy as st
from core import pool as pool_mod
from core import btcache
from config import BACKTEST_DEFAULTS

# ⚠ 缓存目录已用到 2.8G / 上限 3G，再加一份新的信号键就会触发 prune 淘汰删文件
#   （C26 记过那种「淘汰被拦下 → 进程死、结果没落盘」）。本脚本**临时把上限抬高**，
#   不删任何已有文件；跑完解绑立即失效，落盘的新键交给服务端按它自己的策略淘汰。
btcache.MAX_BYTES = 8 * 1024 * 1024 * 1024

BASE_EXEC = {"buy_price": "open", "sell_price": "close", "max_hold_price": "close",
             "slippage": 0.001}

# ---- 只改 risk / exec 的变体（命中缓存）----
RISK_VAR = [
    ("R0", "原样：持1 止损-2 止盈+5",           dict(hold=1, sl=-2, tp=5)),
    ("R1", "持1 无止损无止盈",                  dict(hold=1, sl=None, tp=None)),
    ("R2", "持1 无止损 止盈+5",                 dict(hold=1, sl=None, tp=5)),
    ("R3", "持1 止损-2 无止盈",                 dict(hold=1, sl=-2, tp=None)),
    ("R4", "持3 无止损无止盈",                  dict(hold=3, sl=None, tp=None)),
    ("R5", "持5 无止损无止盈",                  dict(hold=5, sl=None, tp=None)),
    ("R6", "持10 无止损无止盈",                 dict(hold=10, sl=None, tp=None)),
    ("R7", "持20 无止损无止盈",                 dict(hold=20, sl=None, tp=None)),
    ("R8", "持5 止损-8 无止盈",                 dict(hold=5, sl=-8, tp=None)),
    ("R9", "持10 止损-8 无止盈",                dict(hold=10, sl=-8, tp=None)),
    ("RA", "持1 无止损无止盈 买入价改收盘",       dict(hold=1, sl=None, tp=None, buy_price="close")),
    ("RB", "持5 无止损无止盈 买入价改收盘",       dict(hold=5, sl=None, tp=None, buy_price="close")),
    ("RC", "持1 无止损无止盈 卖出价改开盘",       dict(hold=1, sl=None, tp=None, sell_price="open",
                                                  max_hold_price="open")),
    ("RD", "持5 止损-6 止盈+12",                dict(hold=5, sl=-6, tp=12)),
    ("RE", "持2 无止损无止盈",                  dict(hold=2, sl=None, tp=None)),
]

# ---- 需要重算 prepare 的（信号条件变了）----
ENTRY_VAR = [
    ("B0", "高开>=3%才买",   dict(buy=["open >= ref(close, 1) * 1.03"])),
    ("B1", "低开<=-3%才买",  dict(buy=["open <= ref(close, 1) * 0.97"])),
]


def cfg_of(spec, hold=1, sl=None, tp=None, buy=None, buy_price="open",
           sell_price="close", max_hold_price="close"):
    return st.merge_config({
        "exec": {"buy_price": buy_price, "sell_price": sell_price,
                 "max_hold_price": max_hold_price, "slippage": 0.001},
        "risk": {"max_hold_days": hold,
                 "stop_loss_pct": sl, "take_profit_pct": tp},
        "buy_trigger": list(buy or []),
        "entry": [], "sell_signal": [], "score_expr": "",
        "timeframe": "daily", "indicators": {},
        "_pool": spec,
    })


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", type=int, default=156)
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--end", default="2026-09-18")
    ap.add_argument("--stage", default="risk", choices=["risk", "entry"])
    ap.add_argument("--extra-cond", action="append", default=[],
                    help="追加到池条件（可多次；会换 btcache 键 ⇒ 重算 prepare）")
    ap.add_argument("--buy", action="append", default=[],
                    help="buy_trigger 条件（可多次；同样换键）")
    ap.add_argument("--only", default="", help="只跑这些变体名，逗号分隔")
    ap.add_argument("--out", default="tools/_selfrein_engine.json")
    args = ap.parse_args()

    p = pool_mod.get_pool(args.pool)
    where = list(p["conds"] or []) + list(args.extra_cond)
    spec = {"base": p["base"], "where": where, "pool_exit": bool(p["pool_exit"]),
            "pool_id": p["id"], "pool_name": p["name"]}
    print("池 #%s %s  base=%s  条件 %d 条%s" % (
        p["id"], p["name"], p["base"], len(where),
        "（追加：" + " ; ".join(args.extra_cond) + "）" if args.extra_cond else ""))
    codes = market.resolve_universe(p["base"])
    print("标的 %d   区间 %s~%s" % (len(codes), args.start, args.end))

    prm = {**BACKTEST_DEFAULTS, "init_cash": 1_000_000.0,
           "max_positions": 5, "position_pct": 20.0}

    # 先按「信号形状」分组，同组共用一份 prepare
    VAR = RISK_VAR if args.stage == "risk" else ENTRY_VAR
    base_buy = list(args.buy)
    if args.only:
        keep = {x.strip() for x in args.only.split(",") if x.strip()}
        VAR = [v for v in VAR if v[0] in keep]
    groups = {}
    for key, name, ov in VAR:
        bkey = tuple(ov.get("buy") or base_buy)
        groups.setdefault(bkey, []).append((key, name, ov))

    rows = []
    for gi, (buykey, members) in enumerate(groups.items(), 1):
        rc0 = cfg_of(spec, hold=1, sl=None, tp=None, buy=list(buykey))
        errs = st.validate_config(rc0)
        if errs:
            print("  跳过该组", errs)
            continue
        t0 = time.time()
        ci = {}
        data = bt.prepare_data(codes, args.start, args.end, rc0, batch=300, cache_info=ci)
        print("组%d  buy_trigger=%s  准备 %.1fs  有效标的 %d  缓存 %s" % (
            gi, list(buykey) or "无", time.time() - t0, len(data),
            {k: ci.get(k) for k in ("hit", "key") if k in ci}), flush=True)

        for key, name, ov in members:
            rc = cfg_of(spec, hold=ov.get("hold", 1), sl=ov.get("sl"), tp=ov.get("tp"),
                        buy=list(buykey), buy_price=ov.get("buy_price", "open"),
                        sell_price=ov.get("sell_price", "close"),
                        max_hold_price=ov.get("max_hold_price", "close"))
            errs = st.validate_config(rc)
            if errs:
                print("  跳过", key, errs)
                continue
            t1 = time.time()
            m = bt.run_backtest(data, rc, args.start, args.end, prm).get("metrics") or {}
            sr = m.get("sell_reason_stat") or {}
            r = dict(var=key, desc=name, buy=list(buykey), extra_cond=list(args.extra_cond),
                     ret=m.get("total_return"), win=m.get("win_rate"), n=m.get("trade_count"),
                     pnl=m.get("avg_pnl_pct"), aw=m.get("avg_win_pct"), al=m.get("avg_lose_pct"),
                     dd=m.get("max_drawdown"), final=m.get("final_asset"),
                     sl=sr.get("stop_loss"), tp=sr.get("take_profit"), mh=sr.get("max_hold"),
                     sec=round(time.time() - t1, 1))
            rows.append(r)
            print("   %-3s %-26s 收益 %8s%%  均笔 %7s%%  胜率 %5s  %5s笔  回撤 %8s  "
                  "止损%5s 止盈%5s 到期%5s  (%.1fs)" % (
                      key, name,
                      round(r["ret"], 2) if r["ret"] is not None else "-",
                      round(r["pnl"], 3) if r["pnl"] is not None else "-",
                      round(r["win"], 1) if r["win"] is not None else "-",
                      r["n"], round(r["dd"], 1) if r["dd"] is not None else "-",
                      r["sl"], r["tp"], r["mh"], r["sec"]), flush=True)
        del data
    json.dump(rows, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("→ 写出", args.out)


if __name__ == "__main__":
    main()
