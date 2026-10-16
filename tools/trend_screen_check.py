# -*- coding: utf-8 -*-
"""「等价但快」版本校验：逐条命中记录 amount / 换手 / 流通市值，回答两个问题。

1. **砍掉两条周线条件**会不会丢命中（全样本 2023-01-01~2026-09-18，逐日对比）。
2. 加一条**能翻成 SQL 的粗筛条件**（`amount >` / `turnover_rate >` / `circ_mv <`）
   会不会把真正的命中筛掉，以及被筛掉那部分票的前向收益长什么样。

口径与 trend_pool_lab 一致：T 日收盘命中 → T+1 开盘买入 → 持 k 日收盘。

    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tools/trend_screen_check.py
"""
from __future__ import annotations

import json
import os
import sys
import time

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import db, expr, market, universe as uni  # noqa: E402
from core import strategy as st  # noqa: E402
from tools.trend_pool_lab import (FILT, HORIZONS, WEEKLY_FIELDS, FIELDS,  # noqa: E402
                                  START, END, fr_arrs)

# 「可 SQL 预筛」的字段清单，直接引自 core/universe，避免两边写跑偏
from core import universe as _u  # noqa: E402
SQLABLE = sorted(_u.PRESELECT_KLINE | _u.PRESELECT_BASIC | _u.PRESELECT_SHAPE)

CORE = ["ma_bull = 1", "count(ma_bull, 10) = 10", "ref(ma_bull, 20) = 0",
        "close >= ref(hh60, 1) * 0.97", "pos120 > 0.6", "close_pos > 0.7",
        "limit_up = 0", "pct_chg < 7", "between(close, 5, 100)",
        "total_mv > 300000"]
WEEKLY = ["w_close > w_ma20", "w_ma20 > ref(w_ma20, 4)"]
FULL = CORE + WEEKLY

# 候选粗筛条件（都必须能被 _cond_term 翻译成 SQL）
ROUGH = ["amount > 50000", "amount > 100000", "amount > 200000", "amount > 300000",
         "turnover_rate > 1", "turnover_rate > 2", "turnover_rate > 3",
         "circ_mv > 200000", "circ_mv > 500000", "circ_mv < 8000000",
         "volume_ratio > 0.8", "volume_ratio > 1.2", "body > 0"]


def main():
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 0
    # 先自检：每条粗筛条件真的能被 SQL 预筛翻译吗（翻不了的就白加）
    print("== 粗筛条件可翻译性（core/universe._cond_term）==")
    for e in FULL + WEEKLY + ROUGH:
        t, skipped = uni._preselect_terms([e])
        print(f"  {'SQL可翻' if t else '翻不了  '}  {e}")

    codes = market.resolve_universe(uni.parse({"base": "ALL", "filt": FILT}),
                                    start=START, end=END)
    print(f"\nuniverse {len(codes)} 只", flush=True)
    cal = [str(r["trade_date"])[:10] for r in db.query(
        "SELECT DISTINCT trade_date FROM daily_kline WHERE trade_date BETWEEN %s "
        "AND %s ORDER BY trade_date", (START, END))]
    dpos = {d: i for i, d in enumerate(cal)}

    recs: list[dict] = []   # 每条 = 一次 FULL 命中
    lost: list[dict] = []   # FULL 不命中、但 CORE 命中（即被周线条件砍掉的）
    t0 = time.time()
    for i in range(0, len(codes), 250):
        part = codes[i:i + 250]
        panel = market.load_panel(part, START, END, adjust="qfq", with_basic=True,
                                  weekly=True, weekly_fields=WEEKLY_FIELDS,
                                  only=FIELDS)
        for code, df in panel.items():
            n = len(df)
            if n < 130:
                continue
            dates = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d").to_numpy()
            gi = np.fromiter((dpos.get(d, -1) for d in dates), dtype=np.int64, count=n)
            ok_g = gi >= 0
            ns = st._namespace(df)
            fr = fr_arrs(df["open"].to_numpy(float), df["close"].to_numpy(float))

            def mask(conds):
                M = np.ones(n, dtype=bool)
                for e in conds:
                    try:
                        M &= expr.evaluate_bool(e, ns, n)
                    except expr.ExprError:
                        return np.zeros(n, dtype=bool)
                return M & ok_g

            mc, mf = mask(CORE), mask(FULL)
            for tag, M in (("full", mc & mf), ("weeklost", mc & ~mf)):
                if not M.any():
                    continue
                idx = np.where(M)[0]
                for j in idx:
                    row = {
                        "tag": tag,
                        "date": dates[j], "code": code,
                        "amount": float(df["amount"].iloc[j]),
                        "turnover_rate": _v(df, "turnover_rate", j),
                        "circ_mv": _v(df, "circ_mv", j),
                        "total_mv": _v(df, "total_mv", j),
                        "volume_ratio": _v(df, "volume_ratio", j),
                        "body": _v(df, "body", j),
                        "fr": {str(h): (_v(df, "__fr", j, fr[h]))
                               for h in HORIZONS},
                    }
                    (recs if tag == "full" else lost).append(row)
        del panel
        print(f"  {min(i + 250, len(codes))}/{len(codes)} {time.time()-t0:.0f}s "
              f"命中 {len(recs)} / 周线砍掉 {len(lost)}", flush=True)

    out = {"hits_full": len(recs), "hits_weeklost": len(lost),
           "days_with_full": len({r["date"] for r in recs}),
           "days_with_core": len(({r["date"] for r in recs})
                                 | {r["date"] for r in lost}),
           "n_trade_days": len(cal)}
    print("\n== 1) 砍掉两条周线条件的影响 ==")
    print(f"  FULL 命中 {len(recs)} 笔 / CORE 额外命中 {len(lost)} 笔"
          f"（即周线条件筛掉 {len(lost)} / {len(recs)+len(lost)}"
          f" = {len(lost)/max(1,len(recs)+len(lost))*100:.0f}%）")
    print(f"  有票的交易日：FULL {out['days_with_full']} 天 / CORE {out['days_with_core']} 天"
          f"（共 {len(cal)} 天）")
    _cmp(fr_of(recs), fr_of(lost), "留下", "周线砍掉")
    _dump(recs, lost, out)

    print("\n== 2) 加一条 SQL 可翻的粗筛条件会丢多少命中 ==")
    hf, hc = recs + lost, recs   # 用 CORE 命中（最坏情况）当被筛对象
    print(f"  基准：CORE 命中 {len(hc)} 笔（FULL 是它的子集）")
    rows = []
    for e in ROUGH:
        keep = [r for r in hc if _sat(e, r)]
        drop = [r for r in hc if not _sat(e, r)]
        if not drop:
            rows.append((e, 0, 0.0, None, None))
            continue
        rows.append((e, len(drop), len(drop) / len(hc) * 100,
                     _mean(drop, 20), _mean(keep, 20)))
    for e, nd, pc, md, mk in rows:
        print(f"  {e:<22} 丢 {nd:>5} 笔 ({pc:>4.1f}%)  "
              f"被丢的持20均 {('—' if md is None else f'{md:+.2f}%'):>9}  "
              f"留下的持20均 {('—' if mk is None else f'{mk:+.2f}%'):>9}")
    _dump(recs, lost, out)


def _v(df, col, j, arr=None):
    if arr is not None:
        v = arr[j]
        return None if not np.isfinite(v) else round(float(v), 4)
    if col not in df.columns:
        return None
    v = df[col].iloc[j]
    return None if pd.isna(v) else round(float(v), 4)


def fr_of(rows):
    return {str(h): np.array([r["fr"][str(h)] for r in rows], dtype=float)
            for h in HORIZONS} if rows else {}


def _cmp(a, b, na, nb):
    for h in (20, 40):
        va, vb = a.get(str(h), np.array([])), b.get(str(h), np.array([]))
        va, vb = va[np.isfinite(va)], vb[np.isfinite(vb)]
        print(f"  持{h}日：{na} {np.nanmean(va) if len(va) else float('nan'):+.2f}%"
              f"({len(va)}笔)  {nb} {np.nanmean(vb) if len(vb) else float('nan'):+.2f}%"
              f"({len(vb)}笔)")


def _sat(e, r):
    col, op, num = e.split()
    v = r.get(col)
    if v is None:
        return True   # 缺值当作「不筛」，与面板 NaN→False 相反，保守不丢命中
    return v > float(num) if op == ">" else v < float(num)


def _mean(rows, h):
    v = np.array([r["fr"][str(h)] for r in rows], dtype=float)
    v = v[np.isfinite(v)]
    return float(v.mean()) if len(v) else None


def _dump(recs, lost, out):
    with open(os.path.join(ROOT, "tools", "_trend_screen_check.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"summary": out, "hits_full": recs, "dropped_by_weekly": lost},
                  fh, ensure_ascii=False, default=str)


if __name__ == "__main__":
    main()
