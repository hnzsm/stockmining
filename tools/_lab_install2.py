# -*- coding: utf-8 -*-
"""把「三段都为正」的组合落成**买卖策略**（普通策略、可删）。

与旧 `_lab_install.py` 的区别：数据源换成修正引擎后的三段结果
（2025 干净形状 / 2024 全年 / 2026 年初至今），且**只落三段都为正的**。

    python tools/_lab_install2.py                    # dry-run
    python tools/_lab_install2.py --apply            # 真写库
"""
import argparse
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import db, strategy as st  # noqa: E402
from tools import _lab_grid as grid  # noqa: E402

PREFIX = "LAB·"
CLEAN = {"S0", "S1", "S2", "S6", "S7"}
DIRTY_POOLS = {5, 227, 229}


def load(pats, clean_only=False):
    out = {}
    for pat in pats:
        for f in sorted(glob.glob(os.path.join(ROOT, pat))):
            try:
                d = json.load(open(f, encoding="utf-8"))
            except Exception:
                continue
            rs = d.get("rows", []) if isinstance(d, dict) else d
            for r in rs:
                if clean_only and (r.get("shape") not in CLEAN
                                   or r.get("pool_id") in DIRTY_POOLS):
                    continue
                out[(r["pool_id"], r["shape"], r["hold"], r["sl"], r["tp"])] = r
    return out


IS = load(("tools/_lab_grid*_6.json", "tools/_lab_wide*.json"), True)
O24 = load(("tools/_lab_o24_*.json",))
O26 = load(("tools/_lab_o26_*.json",))
SHAPES = {s["key"]: s for s in grid.SHAPES}
POOLS = {p["id"]: p for p in grid.load_pools()}


def fmt(v, nd=1):
    return "—" if v is None else f"{v:.{nd}f}"


def pick(min_ret=0.0):
    """三段都为正的组合。"""
    out = []
    for k, r in IS.items():
        if k[3] or k[4]:
            continue                       # 只收无止损止盈的
        a, b = O24.get(k), O26.get(k)
        if not a or not b:
            continue
        m = lambda x: (x or {}).get("metrics") or {}
        v25 = m(r).get("total_return")
        v24 = m(a).get("total_return")
        v26 = m(b).get("total_return")
        if None in (v25, v24, v26):
            continue
        if min(v25, v24, v26) <= min_ret:
            continue
        out.append({
            "pool_id": k[0], "shape": k[1], "hold": k[2],
            "is": v25, "r24": v24, "r26": v26,
            "is_dd": m(r).get("max_drawdown"), "dd24": m(a).get("max_drawdown"),
            "dd26": m(b).get("max_drawdown"),
            "is_n": m(r).get("trade_count"), "is_wr": m(r).get("win_rate"),
            "is_pf": m(r).get("profit_factor"),
        })
    out.sort(key=lambda x: -min(x["is"], x["r24"], x["r26"]))
    return out


def build(p):
    pool = POOLS[p["pool_id"]]
    shape = SHAPES[p["shape"]]
    name = f"{PREFIX}{pool['name']}·{shape['name']}·持{p['hold']}"
    note = "\n".join([
        f"配套选股策略：**{pool['name']}**（池 #{pool['id']}，base={pool['base']}）。",
        f"买卖规则：{shape['name']}"
        + (f"（买入触发 {' + '.join(shape['buy']) or '次日无条件买入'}）"
           if shape["buy"] else "（次日无条件买入）")
        + f"；卖出信号：{'、'.join(shape['sell']) if shape['sell'] else '无，只按到期离场'}。",
        f"风控：最长持有 {p['hold']} 根 K 线，无止损止盈；滑点 0.1%，买卖均按开盘价成交。",
        "",
        "【实测：三段都为正】",
        f"· 2025 全年（样本内）  收益 {fmt(p['is'])}%   回撤 {fmt(p['is_dd'])}%"
        f"   {p['is_n']} 笔 / 胜率 {fmt(p['is_wr'])}% / 盈亏比 {fmt(p['is_pf'], 2)}",
        f"· 2024 全年（样本外）  收益 {fmt(p['r24'])}%   回撤 {fmt(p['dd24'])}%",
        f"· 2026 年初至今（样本外）收益 {fmt(p['r26'])}%   回撤 {fmt(p['dd26'])}%",
        "",
        "口径：2026-09-30 起引擎卖出侧已位移一根（T 日收盘确认 → T+1 开盘执行），"
        "本条的数字是修正后跑出来的，不含未来函数。",
    ])
    cfg = st.merge_config({
        "entry": [], "buy_trigger": list(shape["buy"]),
        "sell_signal": list(shape["sell"]),
        "risk": {"max_hold_days": p["hold"], "stop_loss_pct": None,
                 "take_profit_pct": None},
        "exec": {"buy_price": "open", "sell_price": "open",
                 "max_hold_price": "close", "slippage": 0.001},
    })
    return name, note, cfg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-ret", type=float, default=0.0)
    ap.add_argument("--top", type=int, default=6)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    rows = pick(args.min_ret)[:args.top]
    print(f"三段都为正的组合：{len(rows)} 组（min_ret={args.min_ret}）\n")
    made = []
    for p in rows:
        name, note, cfg = build(p)
        print(f"  {name}")
        print(f"      2025 {fmt(p['is'])}% / 2024 {fmt(p['r24'])}% / 2026 {fmt(p['r26'])}%"
              f"   最差回撤 {fmt(min(p['is_dd'], p['dd24'], p['dd26']))}%")
        if args.apply:
            exist = db.query_one("SELECT id FROM strategy WHERE name=%s", (name,))
            if exist:
                db.execute("UPDATE strategy SET note=%s, config=%s WHERE id=%s",
                           (note, st.config_json(cfg), exist["id"]))
                made.append((exist["id"], name, "更新"))
            else:
                sid = db.insert(
                    "INSERT INTO strategy (name, note, timeframe, config, builtin) "
                    "VALUES (%s,%s,%s,%s,0)",
                    (name, note, "daily", st.config_json(cfg)))
                made.append((sid, name, "新增"))
    if args.apply:
        print(f"\n已写库 {len(made)} 条：")
        for sid, name, how in made:
            print(f"  #{sid} {name}（{how}）")
        print("全部 builtin=0 = 普通策略，界面上可改名可删。")
    else:
        print("\n（dry-run，加 --apply 才写库）")


if __name__ == "__main__":
    main()
