# -*- coding: utf-8 -*-
"""引擎口径修正（CONVENTIONS C37：离场按价格时间线重排）后，把受影响的回测记录**原地覆盖**。

背景：旧引擎把「盘中止盈/止损」排在「按 open 成交的到期/信号」**之前**，等于白送一个
「摸到止盈就吃、没摸到就当自己开盘卖了」的期权。#58 的 +191.65% 全靠它。修后同一配置
= −99.9593%（1533 笔、`take_profit` 0 笔）。

本脚本只做三件事，**不动**记录 id / 名字 / 池快照 / 参数：
    1. 覆盖前把原记录整行导出到 `data/backup_c37_runs_<ts>.json`（run + 逐笔 + 资金曲线）
    2. 用**当前引擎**按记录自己的快照重跑（benchmark 仍是沪深300，与平台同口径）
    3. 回写 metrics / log / progress，并把逐笔与资金曲线**先删后插**

⚠ 为什么不用平台的「重测」：那会**新建**一条记录，列表里同时留着假数字；
   这里要的是原地订正。走 core 直接跑也省掉 job 队列，且能复用 btcache。

    python tools/_lab_c37_refresh.py                 # 干跑：列出哪些记录会变、变多少
    python tools/_lab_c37_refresh.py --runs 58,61    # 只处理指定 id（仍先干跑）
    python tools/_lab_c37_refresh.py --apply         # 真写库（先自动备份）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import backtest as bt, db, market, strategy as st, universe as uni  # noqa: E402


# --------------------------------------------------------------------------- 工具
def benchmark_series(start: str, end: str, index_code: str = "000300.SH") -> dict:
    """与 `app._benchmark_series` 同口径（沪深300）。"""
    rows = db.query(
        "SELECT trade_date, close FROM index_daily WHERE ts_code=%s "
        "AND trade_date BETWEEN %s AND %s ORDER BY trade_date",
        (index_code, start, end))
    return {r["trade_date"].strftime("%Y-%m-%d"): float(r["close"])
            for r in rows if r["close"]}


def affected_by_c37(cfg: dict) -> bool:
    """是否属于「C37 影响面」：同时存在 open 类离场与盘中风控。

    前者：`max_hold_price=="open"`（且设了持有天数）或 `sell_price=="open"`
          （且带 `sell_signal` / `pool_exit`）。
    后者：`stop_loss_pct` / `take_profit_pct` 有值。
    只满足一个的**不受影响**（旧顺序与新顺序结果逐位相同）。
    """
    ex = cfg.get("exec") or {}
    rk = cfg.get("risk") or {}
    ss = cfg.get("sell_signal") or []
    px = bool((cfg.get("pool") or {}).get("pool_exit"))
    mhp = ex.get("max_hold_price", "close")
    spc = ex.get("sell_price", "open")
    open_exit = (mhp == "open" and rk.get("max_hold_days")) or (spc == "open" and (bool(ss) or px))
    intraday = (rk.get("stop_loss_pct") not in (None, "")) or \
               (rk.get("take_profit_pct") not in (None, ""))
    return bool(open_exit and intraday)


def dump_backup(ids: list[int]) -> str:
    ph = ",".join(["%s"] * len(ids))
    data = {
        "runs": [dict(r) for r in db.query(
            f"SELECT * FROM backtest_run WHERE id IN ({ph})", ids)],
        "trades": [dict(r) for r in db.query(
            f"SELECT * FROM backtest_trade WHERE run_id IN ({ph})", ids)],
        "equity": [dict(r) for r in db.query(
            f"SELECT * FROM backtest_equity WHERE run_id IN ({ph})", ids)],
    }
    path = os.path.join(ROOT, "data",
                        "backup_c37_runs_%s.json" % time.strftime("%Y%m%d_%H%M%S"))
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, default=str)
    return path


def write_back(run_id: int, metrics: dict, logs: list[str], trades: list, equity: list) -> None:
    db.execute(
        "UPDATE backtest_run SET metrics=%s,status='done',finished_at=NOW(),log=%s,"
        "progress=1,progress_msg=%s WHERE id=%s",
        (json.dumps(metrics, ensure_ascii=False, default=str),
         "\n".join(logs)[-6000:], "完成（引擎口径修正 C37 后重跑）", run_id))
    db.execute("DELETE FROM backtest_trade WHERE run_id=%s", [run_id])
    if trades:
        db.insert_many(
            "INSERT INTO backtest_trade (run_id,ts_code,name,buy_date,buy_price,"
            "buy_reason,sell_date,sell_price,shares,cost,revenue,pnl,pnl_pct,hold_days,"
            "sell_reason,fee,slip_cost) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [(run_id, t["ts_code"], t["name"], t["buy_date"], t["buy_price"],
              t["buy_reason"], t["sell_date"], t["sell_price"], t["shares"], t["cost"],
              t["revenue"], t["pnl"], t["pnl_pct"], t["hold_days"], t["sell_reason"],
              t.get("fee"), t.get("slip_cost")) for t in trades])
    db.execute("DELETE FROM backtest_equity WHERE run_id=%s", [run_id])
    if equity:
        db.insert_many(
            "INSERT INTO backtest_equity (run_id,trade_date,total_asset,cash,"
            "position_value,hold_num,benchmark) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            [(run_id, e["trade_date"], e["total_asset"], e["cash"],
              e["position_value"], e["hold_num"], e["benchmark"]) for e in equity])


# --------------------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default="", help="逗号分隔的记录 id；留空=自动挑受影响的")
    ap.add_argument("--apply", action="store_true", help="真写库（默认只干跑对比）")
    ap.add_argument("--only-changed", action="store_true",
                    help="写库时跳过「新旧完全一致」的记录")
    args = ap.parse_args()

    only = {int(x) for x in args.runs.split(",") if x.strip()} or None

    runs = db.query("SELECT * FROM backtest_run ORDER BY id")
    targets, skipped = [], []
    for r in runs:
        if only is not None and int(r["id"]) not in only:
            continue
        if not r.get("universe_spec"):
            skipped.append((r["id"], "无快照"))
            continue
        q = db.query("SELECT config FROM strategy WHERE id=%s", [r["strategy_id"]])
        if not q:
            skipped.append((r["id"], "策略已删"))
            continue
        cfg = json.loads(q[0]["config"]) if isinstance(q[0]["config"], str) else q[0]["config"]
        if only is None and not affected_by_c37(cfg):
            skipped.append((r["id"], "不在 C37 影响面"))
            continue
        targets.append((r, cfg))

    print("待处理 %d 条；跳过 %d 条" % (len(targets), len(skipped)))
    if skipped:
        print("  跳过明细：", ", ".join("#%s(%s)" % s for s in skipped[:12]),
              "…" if len(skipped) > 12 else "")
    # ⚠ 备份必须在**任何写库之前**做完。写在循环之后 = 备下来的是「已覆盖」的内容，等于没备。
    if args.apply and targets:
        ids_all = [int(r["id"]) for r, _ in targets]
        print("[备份] %s（%d 条 · 覆盖前原样）" % (dump_backup(ids_all), len(ids_all)))
    print()
    print("run   策略  旧收益       新收益       差        旧笔   新笔   卖出原因变化")
    print("-" * 100)

    changed, same, failed = [], [], []
    cache: dict = {}
    for r, cfg in targets:
        rid = int(r["id"])
        try:
            spec = json.loads(r["universe_spec"])
            start, end = str(r["start_date"])[:10], str(r["end_date"])[:10]
            key = (r["strategy_id"], r["universe_spec"], start, end)
            if key not in cache:
                universe = {"base": "all", "where": list(spec["where"]),
                            "pool_exit": bool(spec.get("pool_exit")), "filt": spec["filt"],
                            "pool_id": spec.get("pool_id")}
                base = st.merge_config({**cfg, "_pool": uni.parse(universe)})
                data = bt.prepare_data(
                    market.resolve_universe(universe, start=start, end=end),
                    start, end, base, batch=250)
                cache[key] = (base, data)
            base, data = cache[key]
            params = json.loads(r["params"]) if isinstance(r["params"], str) else (r["params"] or {})
            res = bt.run_backtest(data, base, start, end, params,
                                  benchmark=benchmark_series(start, end))
            new = res.get("metrics") or {}
            old = json.loads(r["metrics"]) if isinstance(r["metrics"], str) else (r["metrics"] or {})
            o_ret, n_ret = old.get("total_return"), new.get("total_return")
            d = None if (o_ret is None or n_ret is None) else float(n_ret) - float(o_ret)

            ors = db.query("SELECT sell_reason, COUNT(*) n FROM backtest_trade "
                           "WHERE run_id=%s GROUP BY sell_reason", [rid])
            od = {x["sell_reason"]: int(x["n"]) for x in ors}
            nd: dict = {}
            for t in res.get("trades") or []:
                nd[t["sell_reason"]] = nd.get(t["sell_reason"], 0) + 1
            delta = {k: (od.get(k, 0), nd.get(k, 0)) for k in set(od) | set(nd)
                     if od.get(k, 0) != nd.get(k, 0)}
            print("%-6s%-6s%-13s%-13s%-10s%-7s%-7s%s" % (
                rid, r["strategy_id"], o_ret, n_ret,
                ("—" if d is None else "%+.2f" % d),
                old.get("trade_count"), new.get("trade_count"),
                json.dumps(delta, ensure_ascii=False) if delta else "—"))
            (changed if (d is None or abs(d) > 0.01) else same).append(rid)

            if args.apply and (not args.only_changed or rid in changed):
                write_back(rid, new, list(res.get("logs") or []),
                           res.get("trades") or [], res.get("equity") or [])
        except Exception as e:  # noqa: BLE001
            failed.append((rid, "%s: %s" % (type(e).__name__, e)))
            print("%-6s%-6s  !! %s" % (rid, r["strategy_id"], e))

    print("-" * 100)
    print("会变 %d 条：%s" % (len(changed), changed))
    print("不变 %d 条：%s" % (len(same), same))
    if failed:
        print("失败 %d 条：%s" % (len(failed), failed))

    if args.apply:
        print("\n[写库] 以上记录已按新口径覆盖（`--only-changed` 时跳过逐位没变的）。"
              "备份见上面的 [备份] 行。")
    else:
        print("\n（干跑。加 --apply 才写库；写前会先整行备份。）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
