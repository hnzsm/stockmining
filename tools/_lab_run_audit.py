"""单条回测记录的「可信度体检」（口径与结论见 .workbuddy/memory/CONVENTIONS.md C36）。

四件事：
  【A】成交价核对 —— 比值法 k = 记录价 / (原始开盘 × (1±滑点))。
       记录里的价是**前复权**价，直接跟 daily_kline.open 比会差一个缩放（假警报）；
       同一次回测里同一只票的 k 必然恒定，所以只比 k_buy 与 k_sell。
  【B】有没有未来函数 —— 看信号是否只在 T−1/T 收盘算、T+1 开盘执行（打印信号配置）。
  【C】「T 日买的当根不卖」的代价 —— 注入变体：允许买入当日就止盈/止损。
  【D】收益结构与敏感度 —— 关掉止盈（裸持有）/ 止盈值扫描 / 滑点扫描 / 集中度 / 年度。

跑法（PY = C:/Users/ming/.workbuddy/binaries/python/envs/stockmining/Scripts/python.exe）：
    PY tools/_lab_run_audit.py --run 58 --check price          # 只做 A（快，不跑引擎）
    PY tools/_lab_run_audit.py --run 58 --check all            # A+B+C+D
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# --------------------------------------------------------------------------- A
def check_price(run_id: int) -> None:
    from core import db
    trades = db.query("SELECT ts_code,name,buy_date,sell_date,buy_price,sell_price,pnl_pct,"
                      "sell_reason FROM backtest_trade WHERE run_id=%s", [run_id])
    if not trades:
        print(f"#{run_id} 没有交易明细"); return
    codes = sorted({t["ts_code"] for t in trades})
    rows: dict = {}
    seq: dict = {}
    d1, d2 = str(min(t["buy_date"] for t in trades)), str(max(t["sell_date"] for t in trades))
    for i in range(0, len(codes), 200):
        ch = codes[i:i + 200]
        ph = ",".join(["%s"] * len(ch))
        for r in db.query(f"SELECT ts_code,trade_date,open,high,low,close,adj_factor FROM daily_kline "
                          f"WHERE ts_code IN ({ph}) AND trade_date BETWEEN %s AND %s", ch + [d1, d2]):
            rows[(r["ts_code"], r["trade_date"])] = r
    for (code, d) in rows:
        seq.setdefault(code, []).append(d)
    for c in seq:
        seq[c].sort()

    print(f"【A】成交价核对（比值法）· #{run_id} 共 {len(trades)} 笔")
    bad, tp_bad, gap = [], [], 0
    mh = tp = 0
    for t in trades:
        b = rows.get((t["ts_code"], t["buy_date"]))
        s = rows.get((t["ts_code"], t["sell_date"]))
        if not b or not s or not b["open"] or not s["open"]:
            continue
        days = seq.get(t["ts_code"]) or []
        try:
            if days.index(t["sell_date"]) - days.index(t["buy_date"]) > 1:
                gap += 1
        except ValueError:
            pass
        kb = float(t["buy_price"]) / (float(b["open"]) * 1.001)
        ks = float(t["sell_price"]) / (float(s["open"]) * 0.999)
        if t["sell_reason"] in ("max_hold", "signal", "pool_out", "end"):
            mh += 1
            if abs(kb / ks - 1) > 0.005:
                bad.append((t["ts_code"], str(t["buy_date"]), str(t["sell_date"]),
                            round(kb, 4), round(ks, 4)))
        elif t["sell_reason"] == "take_profit":
            tp += 1
            got = float(t["sell_price"]) / 0.999
            alt = float(s["open"]) * kb
            if abs(got / max(float(t["buy_price"]) * 1.05, alt) - 1) > 0.005:
                tp_bad.append((t["ts_code"], str(t["sell_date"]), round(got, 3)))
    print(f"  · 非止盈单（按卖日开盘成交）{mh} 笔：k_buy≠k_sell 的 {len(bad)} 笔")
    print("     （不一致的正常来源只有两种：买卖之间**夹了除权日** ⇒ k 随 adj_factor 跳；"
          "或**区间末尾强平** buy_date=sell_date）")
    for x in bad[:6]:
        print("     ", x)
    print(f"  · 止盈单 {tp} 笔：卖出价 ≠ max(买价×1.05, 卖日开盘×k) 的 {len(tp_bad)} 笔")
    for x in tp_bad[:6]:
        print("     ", x)
    print(f"  · 买卖跨 >1 个交易日的（停牌）: {gap} 笔")


# --------------------------------------------------------------------------- B
def show_signal(run_id: int) -> None:
    from core import db
    run = db.query("SELECT strategy_id,universe_spec,params FROM backtest_run WHERE id=%s", [run_id])[0]
    cfg = json.loads(db.query("SELECT config FROM strategy WHERE id=%s",
                              [run["strategy_id"]])[0]["config"])
    spec = json.loads(run["universe_spec"]) if run["universe_spec"] else {}
    print(f"【B】信号口径 · 策略 #{run['strategy_id']}")
    print("  池条件:", json.dumps(spec.get("where"), ensure_ascii=False))
    print("  entry:", json.dumps(cfg.get("entry"), ensure_ascii=False),
          "| buy_trigger:", json.dumps(cfg.get("buy_trigger"), ensure_ascii=False),
          "| score_expr:", repr(cfg.get("score_expr")))
    print("  sell_signal:", json.dumps(cfg.get("sell_signal"), ensure_ascii=False),
          "| pool_exit:", (cfg.get("pool") or {}).get("pool_exit"),
          "| risk:", json.dumps(cfg.get("risk"), ensure_ascii=False))
    print("  exec:", json.dumps(cfg.get("exec"), ensure_ascii=False))
    print("  ⇒ 卖出若只靠 risk（持有N日/止损止盈），则 C34 的位移两行不参与，不受未来函数影响。")


# ------------------------------------------------------------------------ C+D
def _load(run_id: int):
    from core import backtest as bt, db, market, strategy as st, universe as uni
    run = db.query("SELECT * FROM backtest_run WHERE id=%s", [run_id])[0]
    spec = json.loads(run["universe_spec"])
    params = json.loads(run["params"])
    cfg = json.loads(db.query("SELECT config FROM strategy WHERE id=%s",
                              [run["strategy_id"]])[0]["config"])
    start, end = str(run["start_date"])[:10], str(run["end_date"])[:10]
    universe = {"base": "all", "where": list(spec["where"]),
                "pool_exit": bool(spec.get("pool_exit")), "filt": spec["filt"],
                "pool_id": spec.get("pool_id")}
    base = st.merge_config({**cfg, "_pool": uni.parse(universe)})
    data = bt.prepare_data(market.resolve_universe(universe, start=start, end=end),
                           start, end, base, batch=250)
    return bt, base, data, params, start, end


SAME_DAY = '''
        # ---- 变体：允许买入当日就止盈/止损（真实限价单当天就会成交）----
        for code in list(positions):
            pos = positions[code]
            if pos.buy_date != today:
                continue
            v = data[code]
            i = v["idx"].get(today)
            if i is None:
                continue
            op, hi, lo, cl = v["open"][i], v["high"][i], v["low"][i], v["close"][i]
            if np.isnan(cl):
                continue
            exit_px, reason = None, None
            if stop_loss is not None:
                trig = pos.buy_price * (1 + float(stop_loss) / 100.0)
                if not np.isnan(lo) and lo <= trig:
                    exit_px = min(trig, op) if not np.isnan(op) else trig
                    reason = "stop_loss_same"
            if exit_px is None and take_profit is not None:
                trig = pos.buy_price * (1 + float(take_profit) / 100.0)
                if not np.isnan(hi) and hi >= trig:
                    exit_px = max(trig, op) if not np.isnan(op) else trig
                    reason = "take_profit_same"
            if exit_px is not None and exit_px > 0:
                sell_px = exit_px * (1 - slippage)
                gross = sell_px * pos.shares
                fee = _fee_sell(gross, commission, stamp_tax, min_commission)
                revenue = gross - fee
                cash += revenue
                pnl = revenue - pos.cost
                slip = pos.buy_slip + (exit_px - sell_px) * pos.shares
                trades.append(Trade(
                    ts_code=code, name=pos.name, buy_date=pos.buy_date,
                    buy_price=round(pos.buy_price, 4), sell_date=today,
                    sell_price=round(sell_px, 4), shares=pos.shares,
                    cost=round(pos.cost, 2), revenue=round(revenue, 2),
                    pnl=round(pnl, 2),
                    pnl_pct=round(pnl / pos.cost * 100, 4) if pos.cost else 0.0,
                    hold_days=pos.hold_bars, sell_reason=reason,
                    buy_reason=pos.buy_reason,
                    fee=round(fee + pos.buy_fee, 2),
                    slip_cost=round(slip, 2),
                ))
                del positions[code]

'''


def _sameday_module():
    src = open(os.path.join(ROOT, "core", "backtest.py"), encoding="utf-8").read()
    anchor = "        # ---------- 3. 收盘估值 ----------"
    if src.count(anchor) != 1:
        raise SystemExit("注入锚点找不到/不唯一，core/backtest.py 结构变了吗？")
    mod = types.ModuleType("bt_sameday")
    mod.__file__ = "<bt_sameday>"
    sys.modules["bt_sameday"] = mod
    exec(compile(src.replace(anchor, SAME_DAY + anchor), "<bt_sameday>", "exec"), mod.__dict__)
    return mod


def check_variants(run_id: int) -> None:
    bt, base, data, params, start, end = _load(run_id)
    mod = _sameday_module()
    print(f"\n【C】「T 日买的当根不卖」的代价 · #{run_id} {start}~{end}")
    for tag, m in (("现引擎（次日才判止盈）", bt), ("变体（当日也能止盈）", mod)):
        r = m.run_backtest(data, base, start, end, params, benchmark=None)
        mt = r["metrics"]
        rs = collections.Counter(t["sell_reason"] for t in r["trades"])
        print(f"  {tag}: 总收益 {mt['total_return']}%  年化 {mt['annual_return']}%  "
              f"单笔均值 {mt['avg_pnl_pct']}%  胜率 {mt['win_rate']}%  笔 {mt['trade_count']}")
        print("     ", dict(rs.most_common(5)))


def check_sweep(run_id: int) -> None:
    from core import db
    bt, base, data, params, start, end = _load(run_id)
    tp = (base.get("risk") or {}).get("take_profit_pct")
    ori_tp = params.get("take_profit_pct", tp)

    def run(cfg=None, prm=None):
        return bt.run_backtest(data, cfg or base, start, end, prm or params,
                               benchmark=None)["metrics"]

    print(f"\n【D】收益结构与敏感度 · #{run_id}（记录值：止盈 {tp}、滑点 {base['exec'].get('slippage')}）")
    m = run()
    print(f"  原样: 总收益 {m['total_return']}%  单笔均值 {m['avg_pnl_pct']}%  胜率 {m['win_rate']}%  "
          f"笔 {m['trade_count']}  回撤 {m['max_drawdown']}%")
    if tp is not None:
        c = json.loads(json.dumps(base)); c["risk"]["take_profit_pct"] = None
        m2 = run(cfg=c)
        print(f"  ⚠ 关掉止盈（持 {base['risk'].get('max_hold_days')} 日按开盘卖）: "
              f"总收益 {m2['total_return']}%  单笔均值 {m2['avg_pnl_pct']}%  笔 {m2['trade_count']}")
    print("  止盈扫描:", end=" ")
    for v in (None, 3, 4, 5, 6, 8):
        c = json.loads(json.dumps(base)); c["risk"]["take_profit_pct"] = v
        print(f"{v}→{run(cfg=c)['total_return']}%", end="  ")
    print()
    print("  滑点扫描（改策略自带 exec.slippage）:", end=" ")
    for v in (0.0005, 0.001, 0.0015, 0.002, 0.003):
        c = json.loads(json.dumps(base)); c["exec"]["slippage"] = v
        print(f"{v * 100:.2f}%→{run(cfg=c)['total_return']}%", end="  ")
    print()

    trades = db.query("SELECT pnl,pnl_pct,fee,slip_cost,buy_date FROM backtest_trade "
                      "WHERE run_id=%s ORDER BY pnl DESC", [run_id])
    if trades:
        pnls = [float(t["pnl"]) for t in trades]
        tot = sum(pnls)
        print(f"  集中度: 前20笔 {sum(pnls[:20]) / tot * 100:.1f}% / 前50笔 "
              f"{sum(pnls[:50]) / tot * 100:.1f}% / 前100笔 {sum(pnls[:100]) / tot * 100:.1f}%"
              f"（净利 {tot:,.0f}，手续费 {sum(float(t['fee']) for t in trades):,.0f}，"
              f"滑点成本 {sum(float(t['slip_cost']) for t in trades):,.0f}）")
        yr = collections.defaultdict(lambda: [0, 0.0, 0.0])
        for t in trades:
            y = str(t["buy_date"])[:4]
            yr[y][0] += 1
            yr[y][1] += float(t["pnl"])
            yr[y][2] += float(t["pnl_pct"])
        print("  年度（笔数 / 累计盈亏 / 单笔均值%）:", end=" ")
        for y in sorted(yr):
            n, s, p = yr[y]
            print(f"{y}: {n}/{s:,.0f}/{p / n:.3f}", end="  ")
        print()
    _ = ori_tp


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=int, required=True, help="backtest_run.id")
    ap.add_argument("--check", default="price",
                    choices=["price", "signal", "variants", "sweep", "all"])
    a = ap.parse_args()
    todo = ["price", "signal", "variants", "sweep"] if a.check == "all" else [a.check]
    for c in todo:
        if c == "price":
            check_price(a.run)
        elif c == "signal":
            show_signal(a.run)
        elif c == "variants":
            check_variants(a.run)
        else:
            check_sweep(a.run)


if __name__ == "__main__":
    main()
