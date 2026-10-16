# -*- coding: utf-8 -*-
"""把网格搜索里**站得住**的组合落成**买卖策略**（普通策略，`builtin=0`）。

入选条件（三条都要过，缺一不写库）：
  1. 样本内（2025）收益 > 0 且笔数 ≥ --min-trades；
  2. 样本外（2024 / 2026）**至少一个**区间收益 > 0 —— 只要 β 不要 α 的组合一律不写；
  3. 样本外不能「全崩」（允许一个区间小亏，但不能两个都 < --max-oos-loss）。

写入的是**买卖规则**（buy_trigger / sell_signal / risk / exec），
配套的选股策略写进 note —— 选股策略一条都不动（不增不删）。

    python tools/_lab_install.py                  # 先看会写什么
    python tools/_lab_install.py --apply          # 真写库
"""
from __future__ import annotations

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


def _fmt(v, n=2, dash="-"):
    return dash if v is None else f"{v:.{n}f}"


def build_note(pool, shape, tune, is_m, oos_rows, span=None):
    lines = [
        f"配套选股策略：#{pool['id']} {pool['name']}"
        f"（{pool['base']}，{len(pool['conds'])} 条条件；**这条别删**，买卖规则离开它就另是一套）",
        "",
        f"【样本内 2025 全年】收益 {_fmt(is_m.get('total_return'))}%"
        f"（池内等权躺平 {_fmt(is_m.get('benchmark_return'))}%，"
        f"超额 {_fmt(is_m.get('alpha'))}%）"
        f"· 最大回撤 {_fmt(is_m.get('max_drawdown'))}%"
        f"· {is_m.get('trade_count')} 笔，胜率 {_fmt(is_m.get('win_rate'), 1)}%"
        f"· 盈亏比 {_fmt(is_m.get('profit_factor'))}· 平均持股 {_fmt(is_m.get('avg_hold_days'), 1)} 天",
    ]
    for o in oos_rows:
        lines.append(
            f"【样本外 {o['range']}】收益 {_fmt(o['ret'])}%"
            f"（池内等权躺平 {_fmt(o.get('bench'))}%，超额 {_fmt(o.get('alpha'))}%）"
            f"· 回撤 {_fmt(o.get('dd'))}% · {o.get('trades')} 笔"
            f"· 胜率 {_fmt(o.get('win'), 1)}% · 盈亏比 {_fmt(o.get('pf'))}")
    if span:
        bi = span.get("bench_idx") or {}
        lines += [
            "",
            f"【连续跑 {span.get('range')}】收益 {_fmt(span.get('ret'))}%"
            f"（池内等权 {_fmt(span.get('bench_eq'))}%，"
            f"沪深300 {_fmt(bi.get('000300.SH'))}%，中证500 {_fmt(bi.get('000905.SH'))}%）"
            f"· 最大回撤 {_fmt(span.get('dd'))}% · {span.get('trades')} 笔"
            f"· 胜率 {_fmt(span.get('win'), 1)}% · 盈亏比 {_fmt(span.get('pf'))}",
        ]
    lines += ["", f"买卖规则：{shape['name']}；"
                  f"持有 {tune['hold']} 根、止损 {tune['sl']}、止盈 {tune['tp']}。",
              "⚠ 由 tools/_lab_grid.py 网格搜索产出，过拟合是这类搜索的常态，"
              "落地前请自己在别的年份再跑一遍。"]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--oos", default=os.path.join(ROOT, "tools", "_lab_oos*.json"))
    ap.add_argument("--min-trades", type=int, default=30)
    ap.add_argument("--min-ret", type=float, default=0.0)
    ap.add_argument("--max-oos-loss", type=float, default=-15.0)
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--combos", default="",
                    help="手工点名，形如 58:S4:20,2:S4:10（池id:形状:持有）")
    ap.add_argument("--max-span-dd", type=float, default=-25.0,
                    help="全区间连续跑的最大回撤下限，默认 -25")
    ap.add_argument("--require-span", action="store_true",
                    help="没有全区间成绩的一律不写")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    rows = []
    for f in sorted(glob.glob(args.oos)):
        rows.extend(json.load(open(f, encoding="utf-8")))
    if not rows:
        print(f"没有 OOS 结果（{args.oos}）"); return

    pools = {p["id"]: p for p in grid.load_pools()}
    shapes = {s["key"]: s for s in grid.SHAPES}

    # 样本内完整指标从 merged 里按 (池,形状,参数) 取回 —— oos.json 只存了收益/笔数/回撤，
    # 直接拿它拼 note 会把基准/超额/胜率/盈亏比全显示成「-」。
    is_full = {}
    _mg = os.path.join(ROOT, "tools", "_lab_merged.json")
    if os.path.exists(_mg):
        for r in (json.load(open(_mg, encoding="utf-8")).get("rows") or []):
            is_full[(r.get("pool_id"), r.get("shape"), r.get("hold"),
                     r.get("sl"), r.get("tp"))] = r.get("metrics") or {}

    # 全区间（2024-01-01~今天）连续跑的结果，key=(池,形状,持有)。
    # 有它的组合才写库：单年切片再漂亮，也可能是「某一年蒙对」，
    # 连续跑的回撤才是真要挨的打（例如 60日新高 三年 +66% 但回撤 −64%）。
    span = {}
    for f in sorted(glob.glob(os.path.join(ROOT, "tools", "_lab_span*.json"))):
        for s in json.load(open(f, encoding="utf-8")):
            span[(s.get("pool_id"), s.get("shape"), s.get("hold"))] = s

    pinned = None
    if args.combos.strip():
        pinned = []
        for c in args.combos.split(","):
            pid, sk, hold = c.strip().split(":")
            pinned.append((int(pid), sk.upper(), int(hold)))

    picked = []
    for r in rows:
        if r["pool_id"] not in pools or r["shape"] not in shapes:
            continue                                  # E*（现有策略）不在本脚本范围内
        if pinned is not None and (r["pool_id"], r["shape"], r["hold"]) not in pinned:
            continue                                  # 手工点名：只写这几条
        if (r.get("is") or 0) <= args.min_ret or (r.get("is_trades") or 0) < args.min_trades:
            continue
        oos = r.get("oos") or []
        if not oos:
            continue
        if not any((o.get("ret") or 0) > 0 for o in oos):
            continue                                  # 样本外一个正的都没有 → 只是 β
        if all((o.get("ret") or 0) < args.max_oos_loss for o in oos):
            continue                                  # 两个区间都崩 → 不要
        # ⚠ span 的键只有 (池,形状,持有)，不含止损止盈 —— 带止损止盈的变体
        # 并没有跑过全区间，拿同一份成绩贴上去是**假证据**，宁可不贴。
        sp = None if (r.get("sl") or r.get("tp")) else \
            span.get((r["pool_id"], r["shape"], r["hold"]))
        if sp is None:
            if args.require_span:
                continue                              # 没有全区间成绩 → 不写
        else:
            if (sp.get("dd") or 0) < args.max_span_dd:
                continue                              # 连续跑回撤太深 → 不要
            r["_span"] = sp
        picked.append(r)

    picked.sort(key=lambda r: -(r.get("is") or 0))
    if picked and pinned is not None:
        order = {k: i for i, k in enumerate(pinned)}
        picked.sort(key=lambda r: order.get((r["pool_id"], r["shape"], r["hold"]), 99))
    picked = picked[:args.top]
    print(f"入选 {len(picked)} 条（总候选 {len(rows)}）\n")

    made = []
    for r in picked:
        pool = pools[r["pool_id"]]
        shape = shapes[r["shape"]]
        tune = {"hold": r["hold"], "sl": r["sl"], "tp": r["tp"]}
        cfg = {
            "buy_trigger": shape["buy"],
            "sell_signal": shape["sell"],
            "risk": {"max_hold_days": tune["hold"], "stop_loss_pct": tune["sl"],
                     "take_profit_pct": tune["tp"]},
            "exec": {"buy_price": "open", "sell_price": "open",
                     "max_hold_price": "close", "slippage": 0.001},
            "timeframe": "daily",
        }
        # ⚠ 名字必须唯一：同一个「池+形状+持有」可能配了不同止损止盈，
        # 只按持有命名会让后写的那条把先写的整条覆盖掉（save_strategy 同名即更新）。
        name = f"{PREFIX}{pool['name']}·{shape['name']}·持{tune['hold']}"
        if tune["sl"] or tune["tp"]:
            name += f"·止损{tune['sl']}止盈{tune['tp']}"
        is_m = is_full.get((r["pool_id"], r["shape"], r["hold"], r["sl"], r["tp"])) or {
            "total_return": r["is"], "trade_count": r["is_trades"],
            "max_drawdown": r["is_dd"]}
        note = build_note(pool, shape, tune, is_m, r["oos"], r.get("_span"))
        sp = r.get("_span")
        print(f"  {name}")
        print(f"      2025 样本内 {_fmt(r['is'])}% / "
              + " ; ".join(f"{o['range'][:4]}~{o['range'][15:]} {_fmt(o.get('ret'))}%"
                           for o in r["oos"])
              + (f" / 连续跑 {_fmt(sp.get('ret'))}% 回撤 {_fmt(sp.get('dd'))}%" if sp
                 else " / 无全区间成绩"))
        if args.apply:
            # 同名即更新（不新开一条）—— 改完 note 口径重跑本脚本不该多出重复记录
            old = db.query_one("SELECT id FROM strategy WHERE name=%s", (name,))
            sid = st.save_strategy(name, note, cfg, int(old["id"]) if old else None)
            made.append((sid, name))
    if args.apply:
        print(f"\n已写入 {len(made)} 条买卖策略：" + "、".join(f"#{i} {n}" for i, n in made))
    else:
        print("\n（dry-run：加 --apply 才会写库）")


if __name__ == "__main__":
    main()
