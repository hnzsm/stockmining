# -*- coding: utf-8 -*-
"""回测记录「卖出侧未来函数」影响面审计 + 指定记录复跑核对。

背景：2026-09-30 修了卖出侧未来函数（`sell_signal[i]` / `in_pool[i]` → `[i-1]`）。
网上有两种记录：
  · 卖出**靠条件**（`sell_signal` 非空 / `pool_exit=true`）→ 老数字被污染，必须重跑；
  · 卖出**只靠风控**（持有 N 日 / 止损 / 止盈）→ 引擎里那两行根本没参与，数字与修复无关。

【A】列出全部回测记录，标出「卖出侧来源」与「是否受该修复影响」。
【B】把指定记录（默认 #58）用**当前引擎**重跑，与原记录逐项对比。
【C】滑点敏感性：该策略单笔净期望只有零点几个百分点，多千分之一滑点就能吃掉大半。
【D】开盘跌停卖不掉的笔数：回测按开盘价成交，实盘可能挂不出。

跑法：
    PY tools/_lab_engine_audit.py                # A + B(#58) + C + D
    PY tools/_lab_engine_audit.py --run 65       # 换记录
    PY tools/_lab_engine_audit.py --only-a       # 只跑 A（秒级，不加载行情）
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import backtest as bt  # noqa: E402
from core import db, indicators, market  # noqa: E402
from core import strategy as st  # noqa: E402
from core import universe as uni  # noqa: E402

SEP = "=" * 74


def _j(v, default):
    if isinstance(v, (str, bytes)):
        try:
            return json.loads(v)
        except Exception:  # noqa: BLE001
            return default
    return v if v is not None else default


def sell_side_of(cfg: dict, spec: dict) -> tuple[str, bool]:
    """返回（卖出侧来源描述, 是否受卖出侧位移修复影响）。"""
    parts, hit = [], False
    n_sell = len(cfg.get("sell_signal") or [])
    if n_sell:
        parts.append(f"卖条件 {n_sell} 条")
        hit = True
    pool_exit = bool((cfg.get("pool") or {}).get("pool_exit")) or bool(spec.get("pool_exit"))
    if pool_exit:
        parts.append("掉出池卖")
        hit = True
    risk = cfg.get("risk") or {}
    rk = []
    if risk.get("stop_loss_pct") is not None:
        rk.append(f"止损{risk['stop_loss_pct']}%")
    if risk.get("take_profit_pct") is not None:
        rk.append(f"止盈{risk['take_profit_pct']}%")
    if risk.get("max_hold_days"):
        rk.append(f"持有{risk['max_hold_days']}日")
    if rk:
        parts.append("风控 " + "/".join(rk))
    return ("、".join(parts) or "无（只靠到期）"), hit


def part_a() -> None:
    print(SEP)
    print("【A】回测记录 · 卖出侧来源审计")
    print(SEP)
    rows = db.query(
        "SELECT id,name,strategy_id,status,metrics,created_at,universe_spec "
        "FROM backtest_run WHERE status='done' ORDER BY id")
    strat: dict[int, dict] = {}
    n_hit = 0
    for r in rows:
        sid = int(r["strategy_id"] or 0)
        if sid not in strat:
            s = st.get_strategy(sid)
            strat[sid] = (s or {}).get("config") or {}
        cfg = strat[sid]
        spec = _j(r["universe_spec"], {}) or {}
        desc, hit = sell_side_of(cfg, spec)
        m = _j(r["metrics"], {}) or {}
        tr = m.get("total_return")
        sname = (st.get_strategy(sid) or {}).get("name") or f"#{sid}"
        if hit:
            n_hit += 1
        flag = "⚠受影响" if hit else "○不受影响"
        print(f"  #{r['id']:<4} {flag}  收益 {tr if tr is None else round(float(tr), 2):>8}  "
              f"策略[{sid}] {sname[:16]:<18} 卖出侧: {desc[:44]}")
        print(f"        建于 {str(r['created_at'])[:19]}  池 {str(r['name'])[:52]}")
    print(f"\n  合计 {len(rows)} 条；受卖出侧位移修复影响 {n_hit} 条，"
          f"不受影响 {len(rows) - n_hit} 条")
    print("  说明：「不受影响」= 该次回测的卖出完全不看 `sell_signal`/`in_pool`，"
          "老数字与新引擎同源，\n        重跑只为核对数据更新后的细微差异。")


def _load_run(rid: int):
    r = db.query("SELECT * FROM backtest_run WHERE id=%s", [rid])[0]
    s = st.get_strategy(int(r["strategy_id"]))
    if not s:
        raise SystemExit(f"记录 #{rid} 引用的策略已不在库里，无法复跑")
    spec = _j(r["universe_spec"], None)
    cfg = {**s["config"]}
    if spec and uni.is_cond(spec):
        cfg = {**cfg, "_pool": uni.parse(spec)}
    cfg = st.merge_config(cfg)
    params = bt.resolve_params(cfg, _j(r["params"], {}))
    start = str(r["start_date"])[:10]
    end = str(r["end_date"])[:10]
    return r, spec, cfg, params, start, end


def _bench(start: str, end: str) -> dict:
    rows = db.query("SELECT trade_date, close FROM index_daily WHERE ts_code=%s "
                    "AND trade_date BETWEEN %s AND %s ORDER BY trade_date",
                    ("000300.SH", start, end))
    return {r["trade_date"].strftime("%Y-%m-%d"): float(r["close"])
            for r in rows if r["close"]}


def part_b(rid: int, sweep: bool, limitdown: bool) -> None:
    r, spec, cfg, params, start, end = _load_run(rid)
    print("\n" + SEP)
    print(f"【B】用当前引擎复跑 #{rid}：{str(r['name'])[:56]}")
    print(SEP)
    desc, hit = sell_side_of(cfg, spec or {})
    print(f"  卖出侧: {desc}  →  {'受未来函数修复影响' if hit else '不受影响（修复只动 sell_signal/in_pool，这两样没参与）'}")
    codes = market.resolve_universe(spec if spec else None, start=start, end=end)
    print(f"  股票池 {len(codes)} 只 / 区间 {start} ~ {end}")
    cinfo: dict = {}
    data = bt.prepare_data(codes, start, end, cfg, batch=250, cache_info=cinfo)
    print(f"  有效标的 {len(data)} 只 / 缓存 {cinfo.get('cache')} key {str(cinfo.get('key'))[:8]}")
    res = bt.run_backtest(data, cfg, start, end, params, benchmark=_bench(start, end))
    m = res.get("metrics") or {}
    old = _j(r["metrics"], {}) or {}
    print("\n  项目            原记录        复跑")
    for k in ("total_return", "annual_return", "trade_count", "win_rate", "avg_pnl_pct",
              "max_drawdown", "sharpe", "final_asset"):
        a, b = old.get(k), m.get(k)
        print(f"    {k:<15} {str(a):<13} {b}")

    if sweep:
        # ⚠ 只改 params["slippage"] 是没用的：`resolve_params` 里**策略自带
        #   `exec.slippage` 会覆盖调用方参数**（它是策略的成交假设）⇒ 必须改 cfg。
        print("\n  【C】滑点敏感性（改策略自带 exec.slippage；其余参数不变）")
        print(f"    原 cfg.exec.slippage = {(cfg.get('exec') or {}).get('slippage')}"
              f"（params 里写什么都会被它盖掉）")
        print("    滑点(单边)   总收益      年化      单笔净期望   笔数")
        for sl in (0.0005, 0.001, 0.0015, 0.002, 0.003):
            cfg2 = {**cfg, "exec": {**(cfg.get("exec") or {}), "slippage": sl}}
            m2 = (bt.run_backtest(data, cfg2, start, end, params,
                                  benchmark=_bench(start, end)).get("metrics") or {})
            print(f"    {sl:<12} {m2.get('total_return'):<11} "
                  f"{m2.get('annual_return'):<9} {m2.get('avg_pnl_pct'):<12} {m2.get('trade_count')}")

    if limitdown:
        print("\n  【D】卖出日「开盘即跌停」的笔数（回测按开盘价成交，实盘可能挂不出）")
        trades = res.get("trades") or []
        # `data` 里没有 pre_close（紧凑结构只留引擎用到的数组）→ 回原表按 (票, 日) 取。
        # 表里是不复权价 + 复权因子，开/昨收之比与复权无关，直接用。
        pairs = [(t["ts_code"], str(t["sell_date"])) for t in trades]
        raw: dict[tuple, dict] = {}
        for i in range(0, len(pairs), 500):
            ch = pairs[i:i + 500]
            ph = ",".join(["(%s,%s)"] * len(ch))
            args = [x for pr in ch for x in pr]
            for r in db.query(
                    f"SELECT ts_code,trade_date,open,pre_close FROM daily_kline "
                    f"WHERE (ts_code,trade_date) IN ({ph})", args):
                raw[(r["ts_code"], r["trade_date"].strftime("%Y-%m-%d"))] = r
        bad = []
        for t in trades:
            r = raw.get((t["ts_code"], str(t["sell_date"])))
            if not r or not r["pre_close"] or not r["open"]:
                continue
            op = float(r["open"])
            pc = float(r["pre_close"])
            lim = indicators._limit_pct(t["ts_code"], None, t.get("name") or "")
            if op / pc - 1 <= -(lim - 0.003):
                bad.append((t, op / pc - 1))
        tot = sum(float(x[0]["pnl"]) for x in bad)
        total_pnl = sum(float(t["pnl"]) for t in trades) or 1e-9
        n_all = len(trades)
        print(f"    合计 {len(bad)} 笔 / {n_all} 笔（{len(bad) / max(n_all, 1) * 100:.2f}%），"
              f"这些单子累计盈亏 {tot:,.0f} 元（占总盈亏 {tot / total_pnl * 100:+.1f}%）")
        for t, g in sorted(bad, key=lambda x: float(x[0]["pnl"]))[:5]:
            print(f"      {t['ts_code']} {t['sell_date']} 开盘{g * 100:+.2f}%  "
                  f"pnl {float(t['pnl']):>10,.0f}  {t.get('name')}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=int, default=58)
    ap.add_argument("--only-a", action="store_true")
    ap.add_argument("--no-sweep", action="store_true")
    ap.add_argument("--no-limitdown", action="store_true")
    args = ap.parse_args()
    part_a()
    if args.only_a:
        return
    part_b(args.run, not args.no_sweep, not args.no_limitdown)


if __name__ == "__main__":
    main()
