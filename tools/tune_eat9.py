# -*- coding: utf-8 -*-
"""候选排序指标研究：同一天多只入池、槽位不够时先买谁。

背景：回测记录 #39（10 只×10%）年化 +24.29%，#47（5 只×20%）只有 +10.15%、
回撤 −63.54%。只数从 10 收到 5 就腰斩，因为引擎在「入池多于槽位」时按
`score` 降序取前 N（core/backtest.py:527 cands.sort），不填评分表达式时
score 全 0 → 实际按 data 字典的插入顺序（≈代码序）买，等于用一个随机顺序
决定 41.6% 的信号能不能成交（dip 池日均 12.4 只候选 / 中位 8 / P90 33）。

口径要点：引擎在**买入日**读 score[i]（i = T+1），所以这里每条表达式都写成
买入日视角 —— 大阴日特征 = ref(x, 1)、涨停日特征 = ref(x, 2)。这样
胜出的字符串可以原样贴进策略的 `score_expr`，页面与调优台是同一个式子。
未来函数防线：表达式在买入日求值时只能引用 ≤ 买入日开盘前已知的数据，
即全部字段都带 ref(..., k>=1)。

    python tools/tune_eat9.py --build                 # 一次性算指标序列（~15 分钟）
    python tools/tune_eat9.py --rank                  # dip 池 × 各指标 × 各仓位档
    python tools/tune_eat9.py --rank --pool yz0,cp_lo # 收窄池上再验一遍
    python tools/tune_eat9.py --top                   # 读结果表
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
import time

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from core import expr, market  # noqa: E402
from core import strategy as st  # noqa: E402

import tune_eat8 as e8  # noqa: E402

RANK_PKL = os.path.join(ROOT, "tools", "_tune_eat9_ranks.pkl")
OUT_JSON = os.path.join(ROOT, "tools", "_tune_eat9_rank.json")

# key, 表达式（买入日视角）, 说明
METRICS: list[tuple[str, str, str]] = [
    ("t_dip",    "ref(turnover_rate, 1)",                 "大阴日换手"),
    ("t_lu",     "ref(turnover_rate, 2)",                 "涨停日换手"),
    ("vr_dip",   "ref(volume_ratio, 1)",                  "大阴日量比"),
    ("amt_dip",  "ref(amount, 1)",                        "大阴日成交额"),
    ("amt_lu",   "ref(amount, 2)",                        "涨停日成交额"),
    ("amt_r",    "ref(amount, 2) / ref(amount, 1)",       "涨停日÷大阴日成交额"),
    ("amp_dip",  "ref(amplitude, 1)",                     "大阴日振幅"),
    ("amp_lu",   "ref(amplitude, 2)",                     "涨停日振幅（一字则小）"),
    ("body_dip", "ref(body, 1)",                          "大阴日实体（越负越狠）"),
    ("cp_dip",   "ref(close_pos, 1)",                     "大阴日收盘位置"),
    ("gap_dip",  "ref(gap_open, 1)",                      "大阴日开盘缺口"),
    ("us_dip",   "ref(upper_shadow, 1)",                 "大阴日上影"),
    ("ls_dip",   "ref(lower_shadow, 1)",                 "大阴日下影"),
    ("ldn_dip",  "ref(limit_down, 1)",                    "大阴日是否跌停收盘"),
    ("mv",       "ref(total_mv, 1)",                      "大阴日总市值"),
    ("price",    "ref(close, 1)",                         "大阴日收盘价"),
    ("pos20",    "ref(pos20, 1)",                         "大阴日 20 日区间位置"),
    ("roc5_lu",  "ref(roc(close, 5), 2)",                 "涨停前 5 日涨幅"),
    ("cont",     "ref(limit_up, 2)",                      "是否连板（涨停日前一天也涨停）"),
    # 第一轮（--rank 5x20）跑出来的方向性结论：上影/影线族最好，补几条复合式
    ("us_rate",  "ref(upper_shadow, 1) / (ref(amplitude, 1) + 0.001)",
     "上影占大阴日振幅的比例"),
    ("sh_sum",   "ref(upper_shadow, 1) + ref(lower_shadow, 1)",
     "大阴日影线合计（振幅−实体）"),
    ("us_minus_ls", "ref(upper_shadow, 1) - ref(lower_shadow, 1)",
     "上影减下影"),
    ("vol_shr",  "ref(vol, 1) / ref(vol, 2)",             "大阴日÷涨停日成交量（缩量度）"),
    ("give_back", "ref(close, 1) / ref(close, 2)",        "大阴日收盘÷涨停日收盘（回吐越多越小）"),
    ("ma20_dist", "ref(close, 1) / ref(ma(close, 20), 1)", "大阴日收盘相对 20 日线"),
    ("lu_gap",   "ref(gap_open, 2)",                      "涨停日开盘缺口（秒板则高）"),
    ("t_lu_x_us", "ref(turnover_rate, 2) * ref(upper_shadow, 1)",
     "涨停日换手 × 大阴日上影"),
]
M_IDX = {k: i for i, (k, _e, _d) in enumerate(METRICS)}
ONLY = e8.ONLY

TIERS = [(5, 20.0), (10, 10.0), (15, 6.67), (20, 5.0)]
TIER_NAME = ["5x20", "10x10", "15x6.67", "20x5"]


def load_ranks() -> dict:
    if not os.path.isfile(RANK_PKL):
        sys.exit(f"先跑 --build 生成 {RANK_PKL}")
    j = pickle.load(open(RANK_PKL, "rb"))
    if [(k, e) for k, e, _d in j["metrics"]] != \
            [(k, e) for k, e, _d in METRICS]:
        sys.exit("指标清单与 pkl 不一致，重新 --build")
    return j["per"]


def build():
    cfg, data, chit = e8.base_data()
    codes = sorted(data)
    print(f"rank build：dip 池 cache={chit} 裁剪后 {len(codes)} 只 × "
          f"{len(METRICS)} 指标", flush=True)
    t0 = time.time()
    per: dict[str, dict] = {}
    for i in range(0, len(codes), 200):
        part = codes[i:i + 200]
        panel = market.load_panel(part, START_END[0], START_END[1], adjust="qfq",
                                  with_basic=True, weekly=False, only=ONLY)
        for code, df in panel.items():
            if code not in data or len(df) < 2:
                continue
            ns = st._namespace(df)
            n = len(df)
            V = np.full((len(METRICS), n), np.nan, dtype=np.float32)
            for k, (_key, e, _d) in enumerate(METRICS):
                try:
                    with np.errstate(invalid="ignore", divide="ignore"):
                        a = np.asarray(expr.evaluate(e, ns, n), dtype=float)
                    V[k] = (np.broadcast_to(a, (n,)) if a.ndim == 0 else a)
                except expr.ExprError:
                    pass
            per[code] = {
                "dates": pd.to_datetime(df["trade_date"]).dt.strftime(
                    "%Y-%m-%d").to_numpy(),
                "vals": V}
        print(f"  {min(i + 200, len(codes))}/{len(codes)} {time.time()-t0:.0f}s",
              flush=True)
    with open(RANK_PKL, "wb") as f:
        pickle.dump({"metrics": list(METRICS), "per": per}, f)
    ok = np.zeros(len(METRICS), int)
    for p in per.values():
        ok += np.isfinite(p["vals"]).sum(axis=1).astype(bool).astype(int)
    print(f"→ {RANK_PKL}（{len(per)} 只，{time.time()-t0:.0f}s）非空票数：")
    for k, e, d in METRICS:
        print(f"  {k:<8} {ok[M_IDX[k]]:>5}  {e}   {d}")


START_END = (e8.START, e8.END)


def scored(dv: dict, ranks: dict, key: str, sign: float) -> dict:
    """把某指标的序列按日期对齐写进 score（NaN → 引擎按 0 处理）。"""
    if key == "none":
        return {c: {**v, "score": np.zeros(len(v["dates"]))} for c, v in dv.items()}
    k = M_IDX[key]
    out = {}
    for code, v in dv.items():
        p = ranks.get(code)
        n = len(v["dates"])
        if p is None:
            out[code] = {**v, "score": np.zeros(n)}
            continue
        if list(p["dates"]) == list(v["dates"]):
            s = np.asarray(p["vals"][k], dtype=float)
        else:
            pos = {dt: i for i, dt in enumerate(p["dates"])}
            idx = np.array([pos.get(dt, -1) for dt in v["dates"]])
            s = np.full(n, np.nan)
            ok = idx >= 0
            s[ok] = np.asarray(p["vals"][k], dtype=float)[idx[ok]]
        out[code] = {**v, "score": np.where(np.isfinite(s), sign * s, 0.0)}
    return out


def rank_stage(sel: list[str], slip: float, tiers: list[str],
               only: list[str] | None = None):
    cfg, data, _ = e8.base_data()
    ranks = load_ranks()
    dv0 = e8.narrowed(data, e8.load_masks(), sel)
    tag = "+".join(sel) or "base"
    print(f"池={tag} 滑点={slip} 参与票 {len(dv0)} 只 档位={tiers}", flush=True)
    rows = []
    t0 = time.time()
    if only:
        cases = [("none", 1.0)] if "none" in only else []
        for spec in only:
            if spec == "none":
                continue
            cases.append((spec[:-1], -1.0) if spec.endswith("-") else (spec, 1.0))
    else:
        cases = ([("none", 1.0)]
                 + [(k, sg) for k, _e, _d in METRICS for sg in (1.0, -1.0)])
    for key, sign in cases:
        dv = scored(dv0, ranks, key, sign)
        label = key if sign > 0 else f"{key}-"
        rec = {"pool": tag, "rank": key, "sign": sign, "label": label,
               "expr": dict((k, e) for k, e, _d in METRICS).get(key, ""),
               "desc": dict((k, d) for k, _e, d in METRICS).get(
                   key, "不排序（代码序）") + ("" if sign > 0 else "（反向）"),
               "pos": {}}
        line = [f"{label:<9}"]
        for (npos, ppct), name in zip(TIERS, TIER_NAME):
            if name not in tiers:
                continue
            m = e8.run(dv, cfg, {**e8.Q_BASE, "npos": npos, "ppct": ppct,
                                "slip": slip})
            rec["pos"][name] = m
            f = m["FULL"]
            line.append(f"{name} F{f.get('annual_return'):>7.2f} "
                        f"T{(m['TRAIN'].get('annual_return') or 0):>7.2f} "
                        f"V{(m['VALID'].get('annual_return') or 0):>7.2f} "
                        f"dd{f.get('max_drawdown'):>7.2f} {f.get('trade_count'):>5}笔")
        rows.append(rec)
        print("  " + " | ".join(line), flush=True)
        rec["label"] = f"{key}{'-' if sign < 0 else ''}"
    with open(OUT_JSON.replace(".json", f"_{tag}.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1, default=str)
    print(f"\n=== {tag}：按 min(TRAIN,VALID) 排序（档位 {tiers}），"
          f"用时 {time.time()-t0:.0f}s ===")
    for name in tiers:
        rr = [r for r in rows if name in r["pos"]]
        rr.sort(key=lambda r: -(min(r["pos"][name]["TRAIN"].get("annual_return") or -100,
                                    r["pos"][name]["VALID"].get("annual_return") or -100)))
        print(f"\n--- 档位 {name} ---")
        for r in rr[:10]:
            m = r["pos"][name]
            yr = " ".join(f"{m[y].get('annual_return'):>6.1f}"
                          for y in ("2023", "2024", "2025", "2026"))
            print(f"{r['label']:<10} {r['desc']:<24} "
                  f"FULL {m['FULL'].get('annual_return'):>7.2f}%  "
                  f"TRAIN {m['TRAIN'].get('annual_return'):>7.2f}%  "
                  f"VALID {m['VALID'].get('annual_return'):>7.2f}%  "
                  f"回撤 {m['FULL'].get('max_drawdown'):>7.2f}%  "
                  f"{m['FULL'].get('trade_count'):>5}笔  | {yr}")
    print("\n表达式对照：")
    for r in rows:
        print(f"  {r['label']:<10} {r['expr']}")


def top():
    rows = json.load(open(OUT_JSON, encoding="utf-8"))
    for name in TIER_NAME:
        rr = [r for r in rows if name in r["pos"]]
        if not rr:
            continue
        base = next((r for r in rr if r["rank"] == "none"), None)
        rr.sort(key=lambda r: -(min(r["pos"][name]["TRAIN"].get("annual_return") or -100,
                                    r["pos"][name]["VALID"].get("annual_return") or -100)))
        print(f"\n--- {name} ---")
        for r in rr[:12]:
            m = r["pos"][name]
            d = (m["FULL"].get("annual_return") or 0) - \
                (base["pos"][name]["FULL"].get("annual_return") or 0) if base else 0
            print(f"{r['label']:<10} {r['desc']:<26} "
                  f"FULL {m['FULL'].get('annual_return'):>7.2f}%（比不排序 {d:+.2f}）  "
                  f"TRAIN {m['TRAIN'].get('annual_return'):>7.2f}%  "
                  f"VALID {m['VALID'].get('annual_return'):>7.2f}%  "
                  f"回撤 {m['FULL'].get('max_drawdown'):>7.2f}%  "
                  f"{m['FULL'].get('trade_count'):>5}笔")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--rank", action="store_true")
    ap.add_argument("--top", action="store_true")
    ap.add_argument("--pool", default="", help="逗号分隔的 eat8 收窄条件 key")
    ap.add_argument("--slip", type=float, default=0.001)
    ap.add_argument("--tiers", default="5x20,10x10,20x5")
    ap.add_argument("--only", default="",
                    help="逗号分隔的指标 key，后缀 - 表示反向；留空 = 全指标双向")
    args = ap.parse_args()
    if args.build:
        build()
    elif args.top:
        top()
    elif args.rank:
        rank_stage([s for s in args.pool.split(",") if s], args.slip,
                   [t for t in args.tiers.split(",") if t],
                   [s for s in args.only.split(",") if s])
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
