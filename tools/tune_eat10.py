# -*- coding: utf-8 -*-
"""按市值分档看收益差：同一个池、同一套执行，只改「大阴日总市值」区间。

口径（与已入库的 #156 推荐配置一致）：
  池 = ref(limit_up,1)=1 + ref(amplitude,1)>=0.5（非一字）+ body<=-5
  买 = 次日开盘无条件买；卖 = 止盈 5% / max_hold_days=1 到期开盘卖；滑点千一
  评分表达式 = ref(close,1)/ref(close,2)（买入日视角，回吐少优先）

市值取**大阴日（T 日）**的 total_mv（单位万元）。数据直接复用
tune_eat9 的 ranks pkl —— 里面存的是 ref(total_mv,1)（买入日 i 上等于 T-1），
所以本脚本把它整体左移一格还原成 T 日的值，不重新拉行情。

档位含 100 只 × 1%：候选数 < 槽位 → 等于「所有信号等权来一遍」，
那一列才是各市值区间的**固有质量**；5×20/10×10 还混了「谁能挤进槽位」。

    python tools/tune_eat10.py                # 全部分档
    python tools/tune_eat10.py --slip 0.002   # 换个滑点
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import tune_eat8 as e8  # noqa: E402
import tune_eat9 as e9  # noqa: E402

SEL = ["yz0"]                      # eat8 的「昨日非一字」掩码 key
MV_KEY = "mv"                      # eat9 ranks 里的 ref(total_mv, 1)
SCORE_KEY = "give_back"            # ref(close,1)/ref(close,2)

# (标签, 下界, 上界) —— total_mv 单位是万元，10000 万 = 1 亿
BANDS = [
    ("<20亿",        0,        200_000),
    ("20~40亿",      200_000,  400_000),
    ("40~70亿",      400_000,  700_000),
    ("70~120亿",     700_000,  1_200_000),
    ("120~200亿",    1_200_000, 2_000_000),
    ("200~400亿",    2_000_000, 4_000_000),
    (">400亿",       4_000_000, np.inf),
    ("全部",         0,         np.inf),
]
TIERS = [("100x1", 100, 1.0), ("20x5", 20, 5.0),
         ("10x10", 10, 10.0), ("5x20", 5, 20.0)]
OUT_JSON = os.path.join(ROOT, "tools", "_tune_eat10_mv.json")

# 把分档结果当成「能写进页面的条件」再跑一遍，并叠上千二滑点看是否还站得住
COMBOS = [
    ("剔 <20亿",     200_000,  np.inf),
    ("只做 20~40亿", 200_000,  400_000),
    ("只做 20~70亿", 200_000,  700_000),
    ("只做 20~120亿", 200_000, 1_200_000),
    ("剔 <40亿",     400_000,  np.inf),
]


def aligned(p: dict, dates: np.ndarray, key: str) -> np.ndarray:
    """把某指标序列按 data 的日期对齐（对齐不上 → NaN）。"""
    n = len(dates)
    v = np.asarray(p["vals"][e9.M_IDX[key]], dtype=float)
    if list(p["dates"]) == list(dates):
        return v
    pos = {dt: i for i, dt in enumerate(p["dates"])}
    idx = np.array([pos.get(dt, -1) for dt in dates])
    out = np.full(n, np.nan)
    ok = idx >= 0
    out[ok] = v[idx[ok]]
    return out


def with_band(dv: dict, ranks: dict, lo: float, hi: float) -> dict:
    """按 T 日（大阴日）市值筛池，并挂上评分。"""
    out = {}
    for code, v in dv.items():
        p = ranks.get(code)
        n = len(v["dates"])
        if p is None:
            continue
        mv_t = aligned(p, v["dates"], MV_KEY)
        mv_t = np.concatenate([mv_t[1:], [np.nan]])    # 左移：i 上就是 T 日市值
        ip = v["in_pool"] & (mv_t >= lo) & (mv_t < hi)
        if not ip.any():
            continue
        out[code] = {**v, "entry": ip, "in_pool": ip,
                     "buy_trigger": np.ones(n, dtype=bool),
                     "sell_signal": np.zeros(n, dtype=bool)}
    return e9.scored(out, ranks, SCORE_KEY, 1.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slip", type=float, default=0.001)
    ap.add_argument("--tiers", default=",".join(t[0] for t in TIERS))
    ap.add_argument("--combos", action="store_true",
                    help="把市值带当成条件跑，并叠上千一/千二/千三滑点")
    ap.add_argument("--slips", default="", help="逗号分隔，如 0.001,0.002")
    ap.add_argument("--bands", default="", help="逗号分隔的带标签（在 BANDS+COMBOS 里找）")
    args = ap.parse_args()
    tiers = [t for t in TIERS if t[0] in args.tiers.split(",")]

    cfg, data, chit = e8.base_data()
    ranks = e9.load_ranks()
    dv0 = e8.narrowed(data, e8.load_masks(), SEL)
    base_sig = int(sum(int(v["in_pool"].sum()) for v in dv0.values()))
    print(f"池=dip+非一字 cache={chit} 参与票 {len(dv0)} 只 信号日 {base_sig} "
          f"评分={dict((k, e) for k, e, _d in e9.METRICS)[SCORE_KEY]}",
          flush=True)

    if args.bands:
        want = args.bands.split(",")
        bands = [b for b in BANDS + COMBOS if b[0] in want]
    else:
        bands = COMBOS if args.combos else BANDS
    slips = ([float(s) for s in args.slips.split(",") if s]
             or ([0.001, 0.002, 0.003] if args.combos else [args.slip]))
    out = OUT_JSON
    if args.combos and not args.bands:
        out = OUT_JSON.replace(".json", "_combo.json")
    elif args.bands:
        out = OUT_JSON.replace(".json", "_ctrl.json")
    rows = []
    for slip in slips:
        for label, lo, hi in bands:
            dv = with_band(dv0, ranks, lo, hi)
            sig = int(sum(int(v["in_pool"].sum()) for v in dv.values()))
            rec = {"slip": slip, "band": label, "lo": lo,
                   "hi": (None if hi == np.inf else hi),
                   "sig": sig, "share": round(sig / base_sig * 100, 1),
                   "stocks": len(dv), "pos": {}}
            print(f"\n--- {label}（滑点{slip}）  信号日 {sig}（{rec['share']}%）"
                  f"{len(dv)} 只 ---", flush=True)
            for name, npos, ppct in tiers:
                m = e8.run(dv, cfg, {**e8.Q_BASE, "npos": npos, "ppct": ppct,
                                     "slip": slip})
                rec["pos"][name] = m
                f = m["FULL"]
                yr = " ".join(f"{m[y].get('annual_return') or 0:>6.1f}"
                              for y in ("2023", "2024", "2025", "2026"))
                print(f"  {name:<6} FULL {f.get('annual_return'):>7.2f}%  "
                      f"累计 {f.get('total_return') or 0:>8.2f}%  "
                      f"TRAIN {m['TRAIN'].get('annual_return'):>7.2f}%  "
                      f"VALID {m['VALID'].get('annual_return'):>7.2f}%  "
                      f"回撤 {f.get('max_drawdown'):>7.2f}%  "
                      f"{f.get('trade_count'):>5}笔  胜率 {f.get('win_rate'):>5.1f}%  "
                      f"均笔 {f.get('avg_pnl_pct') or 0:>+.3f}%  | {yr}", flush=True)
            rows.append(rec)
    json.dump(rows, open(out, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, default=str)
    print(f"\n完成 → {out}")


if __name__ == "__main__":
    main()
