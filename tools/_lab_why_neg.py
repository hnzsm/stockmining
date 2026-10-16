"""为什么回测大多为负：单变量拆解。

在策略 #101「跌破MA10离场」的形状上，每次**只改一个变量**跑一遍，看收益怎么动，
从而把「亏在哪」拆成几块可归因的量：买入时点 / 卖出时点 / 止损 / 成本 / 持有期。

⚠ 只调引擎、不写库、不开盘缓存（core/btcache 写满会触发淘汰删文件 → 进程死）。
⚠ **信号集变了必须重新 prepare**：`entry/buy_trigger/sell_signal/score` 是在
   `prepare_data` 里算好塞进 data 的，复用旧 data 时改 cfg 里的条件**不生效**
   （第一版就栽在这：V3/V5/V10 跟 V0 数字一模一样，看着像"结论"其实是没跑）。
   ⇒ 按 (buy, sell) 分组，一组 prepare 一次，组内 `risk`/`slippage`/`exec` 随便改。
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "core"))

from core import backtest as bt, market, strategy as st          # noqa: E402
from config import BACKTEST_DEFAULTS                             # noqa: E402

PARAMS = {**BACKTEST_DEFAULTS, "init_cash": 1_000_000,
          "max_positions": 10, "position_pct": 10.0}

# 基准形状 = 策略 #101
B0 = dict(buy=["open > ref(close, 1)"],                 # 高开才买
          sell=["close < ma10", "open < ref(close, 1)"],  # 破MA10 或 低开 就卖
          hold=15, sl=-8, tp=None, slip=0.001,
          buy_price="open", sell_price="open")

# 每次只动一处
VAR = [
    ("V0",  "原样（高开买 / 破MA10或低开卖 / 止损8% / 持15）", {}),
    ("V1",  "去掉滑点",                    dict(slip=0.0)),
    ("V2",  "去掉止损",                    dict(sl=None)),
    ("V3",  "不限高开（次日开盘无条件买）",  dict(buy=[])),
    ("V4",  "买入价改收盘",                dict(buy_price="close")),
    ("V5",  "去掉「低开就卖」",            dict(sell=["close < ma10"])),
    ("V6",  "卖出价改收盘",                dict(sell_price="close")),
    ("V10", "改成低开买（其余同原样）",     dict(buy=["open < ref(close, 1)"])),
    ("V7",  "裸持有15天（无触发/无信号/无止损）",
     dict(buy=[], sell=[], sl=None, hold=15)),
    ("V8",  "裸持有5天",
     dict(buy=[], sell=[], sl=None, hold=5)),
    ("V13", "裸持有30天",
     dict(buy=[], sell=[], sl=None, hold=30)),
    ("V9",  "裸持有15天 + 无滑点",
     dict(buy=[], sell=[], sl=None, hold=15, slip=0.0)),
    ("V11", "裸持有15天（无滑点无手续费）",
     dict(buy=[], sell=[], sl=None, hold=15, slip=0.0, zero_fee=True)),
]


def cfg_of(base: dict) -> dict:
    """基准形状 + 单变量改动 → 引擎 cfg。"""
    exec_ = {"buy_price": base["buy_price"], "sell_price": base["sell_price"],
             "max_hold_price": "close", "slippage": base["slip"]}
    return st.merge_config({
        "buy_trigger": list(base["buy"]),
        "sell_signal": list(base["sell"]),
        "risk": {"max_hold_days": base["hold"],
                 "stop_loss_pct": base["sl"],
                 "take_profit_pct": base["tp"]},
        "exec": exec_,
    })


def load_pools(only):
    """only 里写 0 ⇒ 加一条「全市场·不选股」做黄金对照（看选股是加分还是减分）。"""
    out = []
    if 0 in only:
        # ⚠ 条件池 where 为空会被 validate 拒（「至少要写一条条件」）⇒ 用一条恒真条件
        #   表达「全市场」，语义等价且能过校验。
        out.append({"id": 0, "name": "全市场·不选股", "base": "all",
                    "conds": ["close > 0"], "pool_exit": False})
    from core import pool as pool_mod
    ps = []
    ids = {x for x in only if x}
    # ⚠ ids 为空 ⇒ 一个真实池都不加（只有 --pools 0 这种纯对照跑法）。
    #   写成「空集就不筛选」会把全部 46 个池拉进来白跑。
    if ids:
        ps = [p for p in pool_mod.list_pools() if p["id"] in ids]
    out += [{"id": p["id"], "name": p["name"], "base": p["base"],
             "conds": list(p["conds"] or []),
             "pool_exit": bool(p["pool_exit"])} for p in ps]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--end", default="2025-12-31")
    ap.add_argument("--pools", default="0,1,58,230")
    ap.add_argument("--out", default="tools/_lab_why_neg.json")
    args = ap.parse_args()

    from core import btcache
    btcache.ENABLED = False

    only = {int(x) for x in args.pools.split(",") if x.strip()}
    pools = load_pools(only)
    print(f"池 {len(pools)} × 变体 {len(VAR)}   区间 {args.start}~{args.end}",
          flush=True)

    rows = []
    for pi, p in enumerate(pools, 1):
        # ⚠ 全市场对照没有对应选股池 ⇒ pool_id 必须给 None，给 0 会被
        #   validate_config 当成「引用了已删除的选股策略 #0」全部跳过（白跑 5 次 prepare）
        pool_spec = {"base": p["base"], "where": list(p["conds"]),
                     "pool_exit": p["pool_exit"],
                     "pool_id": (p["id"] or None),
                     "pool_name": p["name"]}
        codes = market.resolve_universe(p["base"])
        print(f"[{pi}/{len(pools)}] {p['name']}  标的 {len(codes)}", flush=True)

        # 按 (buy, sell) 分组：同组共用一份 data
        groups: dict[tuple, list] = {}
        for key, name, ov in VAR:
            b = {**B0, **ov}
            groups.setdefault((tuple(b["buy"]), tuple(b["sell"])), []).append(
                (key, name, ov))

        for gi, (sig, members) in enumerate(groups.items(), 1):
            buy, sell = list(sig[0]), list(sig[1])
            base_cfg = cfg_of({**B0, "buy": buy, "sell": sell})
            run_cfg = {**base_cfg, "_pool": pool_spec}
            t0 = time.time()
            data = bt.prepare_data(codes, args.start, args.end, run_cfg,
                                   batch=300, cache_info={})
            print(f"   组{gi} 买{len(buy)}/卖{len(sell)}  准备 {time.time()-t0:.0f}s"
                  f"  有效 {len(data)}", flush=True)

            for key, name, ov in members:
                b = {**B0, "buy": buy, "sell": sell, **ov}
                cfg = cfg_of(b)
                rc = {**cfg, "_pool": pool_spec}
                errs = st.validate_config(rc)
                if errs:
                    print("  跳过", key, errs, flush=True)
                    continue
                prm = dict(PARAMS)
                if ov.get("zero_fee"):
                    prm.update({"commission": 0.0, "stamp_tax": 0.0,
                                "min_commission": 0.0})
                t1 = time.time()
                m = (bt.run_backtest(data, rc, args.start, args.end, prm)
                     .get("metrics") or {})
                sr = m.get("sell_reason_stat") or {}
                r = dict(pool_id=p["id"], pool=p["name"], var=key, desc=name,
                         ret=m.get("total_return"), win=m.get("win_rate"),
                         n=m.get("trade_count"), hold=m.get("avg_hold_days"),
                         aw=m.get("avg_win_pct"), al=m.get("avg_lose_pct"),
                         pnl=m.get("avg_pnl_pct"), dd=m.get("max_drawdown"),
                         slip=m.get("slippage_pct_of_init"),
                         fee=m.get("fee_pct_of_init"),
                         sl=sr.get("stop_loss"), sg=sr.get("signal"),
                         mh=sr.get("max_hold"),
                         s=round(time.time() - t1, 1))
                rows.append(r)
                print(f"     {key:<4} {name[:26]:<28} "
                      f"收益 {str(round(r['ret'], 1) if r['ret'] is not None else '-'):>7}% "
                      f"胜率 {str(round(r['win'],1) if r['win'] is not None else '-'):>5} "
                      f"{str(r['n']):>5}笔  均盈 "
                      f"{str(round(r['aw'],1) if r['aw'] is not None else '-'):>5}/"
                      f"{str(round(r['al'],1) if r['al'] is not None else '-'):>6} "
                      f"止损{str(r['sl'] or 0):>5} 信号{str(r['sg'] or 0):>5}",
                      flush=True)
            del data
        json.dump(rows, open(args.out, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
    print("已写", args.out)


if __name__ == "__main__":
    main()
