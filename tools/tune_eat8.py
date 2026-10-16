# -*- coding: utf-8 -*-
"""在已入库的 dip 池（选股策略 #156）之上做「缩小范围」条件研究。

为什么能秒级评估：引擎只认数组 —— 池条件在 T 日收盘求值成 `in_pool`，
T+1 开盘买入（见 backtest.run_backtest 的「下一日候选」段）。所以任何
「只用 T 日及以前数据」的收窄条件都等价于 `in_pool &= mask(T)`，
不必为了换条件重算 6~12 分钟的行情/指标。

分三段：
    python tools/tune_eat8.py --build            # 一次性算掩码（load_panel 一次）
    python tools/tune_eat8.py --stage            # 单条条件逐个评估
    python tools/tune_eat8.py --combo top        # 头部条件的两两/三三组合

所有候选条件都是**页面能直接写进选股策略的表达式**，赢了就抄进池子，
不依赖本脚本的数组技巧。未来函数防线：候选只允许引用 T 日与 ref(x, k>=0)。
"""
from __future__ import annotations

import argparse
import itertools
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

from core import backtest as bt, expr, market  # noqa: E402
from core import strategy as st  # noqa: E402

from tune_eat1 import (START, END, TRAIN, VALID, bench, make_run_cfg,  # noqa: E402
                       prune)
import tune_eat3 as e3  # noqa: E402

MASK_PKL = os.path.join(ROOT, "tools", "_tune_eat8_masks.pkl")
OUT_JSON = os.path.join(ROOT, "tools", "_tune_eat8_result.json")

# 基准执行配置：与已入库的买卖策略 #58 完全一致
Q_BASE = {"stop": None, "tp": 5, "hold": 1, "buy_px": "open", "sell_px": "open",
          "hold_px": "open", "slip": 0.001, "npos": 20, "ppct": 5,
          "pool_exit": False}
POS = [(20, 5.0), (10, 10.0), (30, 3.0)]

WINS = {"2023": ("2023-01-01", "2023-12-31"),
        "2024": ("2024-01-01", "2024-12-31"),
        "2025": ("2025-01-01", "2025-12-31"),
        "2026": ("2026-01-01", "2026-09-18"),
        "TRAIN": TRAIN, "VALID": VALID,
        "FULL": (START, END)}

# ---------------------------------------------------------------------------
# 候选条件（key, 表达式, 中文说明）—— 全部是 T 日可知的信息
# ---------------------------------------------------------------------------
CANDS: list[tuple[str, str, str]] = [
    # 涨停日（T-1）的封板质量
    ("yz1",     "ref(amplitude, 1) < 0.5",                     "昨日一字板"),
    ("yz0",     "ref(amplitude, 1) >= 0.5",                    "昨日非一字"),
    ("seal_t",  "ref(low, 1) >= ref(close, 1) * 0.995",        "昨日几乎未开板"),
    ("seal_o",  "ref(low, 1) < ref(close, 1) * 0.995",         "昨日盘中开过板"),
    ("first",   "ref(limit_up, 2) = 0",                        "首板（前日非涨停）"),
    ("cont",    "ref(limit_up, 2) = 1",                        "连板"),
    ("hot3",    "count(limit_up, 4) >= 2",                     "近4日≥2个涨停"),
    ("vlo1",    "ref(vol, 1) < ref(vma5, 1)",                  "涨停日缩量"),
    ("vhi1",    "ref(vol, 1) >= ref(vma5, 1)",                 "涨停日放量"),
    ("t1lo",    "ref(turnover_rate, 1) < 3",                   "涨停日换手<3%"),
    ("t1mid",   "between(ref(turnover_rate, 1), 3, 10)",       "涨停日换手3~10%"),
    ("t1hi",    "ref(turnover_rate, 1) >= 10",                 "涨停日换手≥10%"),
    # 大阴日（T）的量能
    ("t2lo",    "turnover_rate < 5",                           "今日换手<5%"),
    ("t2mid",   "between(turnover_rate, 5, 15)",               "今日换手5~15%"),
    ("t2hi",    "turnover_rate >= 15",                         "今日换手≥15%"),
    ("vrlo",    "volume_ratio < 1",                            "今日量比<1"),
    ("vrhi",    "volume_ratio >= 2",                           "今日量比≥2"),
    ("liq1",    "amount >= 100000",                            "今日成交额≥1亿"),
    ("liq3",    "amount >= 300000",                            "今日成交额≥3亿"),
    ("amtsh",   "amount < ref(amount, 1) * 0.5",               "今日较涨停日缩量过半"),
    # 大阴日的形态深度
    ("d_mild",  "body > -7 and body <= -5",                    "跌 5~7%"),
    ("d_mid",   "body > -9 and body <= -7",                    "跌 7~9%"),
    ("d_deep",  "body <= -9",                                  "跌 ≥9%"),
    ("g_dn",    "gap_open <= -2",                              "今日低开≥2%"),
    ("g_up",    "gap_open >= 0",                               "今日平/高开"),
    ("cp_lo",   "close_pos < 0.2",                             "收盘在日内低位"),
    ("cp_hi",   "close_pos >= 0.2",                            "收盘未被按到最低"),
    ("ldn0",    "limit_down = 0",                              "今日未跌停"),
    ("ldn1",    "limit_down = 1",                              "今日收跌停"),
    ("us_lo",   "upper_shadow <= lower_shadow",                "下影不短于上影"),
    # 市值 / 价格 / 趋势位置
    ("mv_s",    "total_mv < 400000",                           "总市值<40亿"),
    ("mv_m",    "between(total_mv, 400000, 1500000)",          "40~150亿"),
    ("mv_l",    "total_mv > 1500000",                          ">150亿"),
    ("p_lo",    "close < 20",                                   "低价股"),
    ("p_hi",    "close >= 20",                                  "中高价股"),
    ("tr_ma20", "close > ma(close, 20)",                       "仍站上20日线"),
    ("tr_dn20", "close < ma(close, 20)",                       "跌破20日线"),
    ("pos20lo", "pos20 < 0.3",                                 "20日区间低位"),
    ("pos20hi", "pos20 > 0.7",                                 "20日区间高位"),
    ("pre_hot", "ref(roc(close, 5), 1) > 15",                  "涨停前5日已涨>15%"),
    ("pre_cld", "ref(roc(close, 5), 1) <= 5",                  "涨停前5日涨幅≤5%"),
]
CAND_IDX = {k: i for i, (k, _e, _d) in enumerate(CANDS)}

# 排序杠杆（买入日用的分数，NaN 安全）
SCORES: list[tuple[str, str, str]] = [
    ("none",  "",                          "不排序"),
    ("deep",  "-body",                     "跌得越狠越优先"),
    ("mild",  "body",                      "跌得越浅越优先"),
    ("cp",    "close_pos",                 "收盘位置高优先"),
    ("turn",  "turnover_rate",             "换手高优先"),
    ("liq",   "amount",                    "成交额大优先"),
]

ONLY = set(st.BASE_FIELDS) | {
    "limit_up", "limit_down", "amplitude", "body", "upper_shadow",
    "lower_shadow", "gap_open", "is_up", "is_down", "close_pos",
    "turnover_rate", "volume_ratio", "vma5", "total_mv", "pos20"}


def base_data():
    """dip 池的紧凑数组（命中磁盘缓存）+ 裁剪。"""
    cfg, data, chit = e3.load_variant("dip", e3.VARIANTS["dip"])
    return cfg, prune(data, cfg), chit


def build():
    cfg, data, chit = base_data()
    codes = sorted(data)
    print(f"dip 池 cache={chit} 裁剪后 {len(codes)} 只，开始补算候选字段…",
          flush=True)
    t0 = time.time()
    per: dict[str, dict] = {}
    for i in range(0, len(codes), 200):
        part = codes[i:i + 200]
        panel = market.load_panel(part, START, END, adjust="qfq",
                                  with_basic=True, weekly=False, only=ONLY)
        for code, df in panel.items():
            if code not in data or len(df) < 2:
                continue
            ns = st._namespace(df)
            n = len(df)
            M = np.zeros((len(CANDS), n), dtype=bool)
            for k, (_key, e, _d) in enumerate(CANDS):
                try:
                    M[k] = expr.evaluate_bool(e, ns, n)
                except expr.ExprError:
                    pass          # 求值失败 → 该条恒 False（宁可少选）
            raw = {}
            for key, e, _d in SCORES:
                if not e:
                    continue
                try:
                    v = expr.evaluate(e, ns, n)
                    a = np.asarray(v, dtype=float)
                    raw[key] = (np.broadcast_to(a, (n,)) if a.ndim == 0
                                else a).astype(np.float32)
                except expr.ExprError:
                    raw[key] = np.full(n, np.nan, dtype=np.float32)
            per[code] = {
                "dates": pd.to_datetime(df["trade_date"]).dt.strftime(
                    "%Y-%m-%d").to_numpy(),
                "masks": np.packbits(M, axis=1),
                "raw": raw,
            }
        print(f"  {min(i + 200, len(codes))}/{len(codes)} "
              f"{time.time() - t0:.0f}s", flush=True)
    tag = f"{len(codes)}|{START}|{END}|{len(CANDS)}"
    with open(MASK_PKL, "wb") as f:
        pickle.dump({"tag": tag, "cands": [(k, e, d) for k, e, d in CANDS],
                     "per": per}, f)
    tot = {k: 0 for k, _e, _d in CANDS}
    for code, p in per.items():
        M = unpack(p, len(p["dates"]))
        for k in tot:
            tot[k] += int(M[CAND_IDX[k]].sum())
    print(f"掩码落盘 {MASK_PKL}（{len(per)} 只），候选命中日数：")
    for k, _e, d in CANDS:
        print(f"  {k:<8} {tot[k]:>7}  {d}")


def unpack(p: dict, n: int) -> np.ndarray:
    nc = len(CANDS)
    M = np.unpackbits(p["masks"], axis=1).reshape(nc, -8)[:, :n]
    return M.astype(bool)


def narrowed(data: dict, per: dict, sel: list[str], score: str = "") -> dict:
    """把 sel 里的条件 AND 进 in_pool/entry，并换成「次日无条件开盘买」。"""
    out = {}
    for code, v in data.items():
        p = per.get(code)
        if p is None:
            continue
        dates, pd_ = v["dates"], p["dates"]
        n = len(dates)
        if list(pd_) == list(dates):
            M, align = unpack(p, n), np.arange(n)
        else:   # 少见：两边日期不完全一致，按日期对齐
            pos = {d: i for i, d in enumerate(pd_)}
            align = np.array([pos.get(d, -1) for d in dates])
            M = np.zeros((len(CANDS), n), dtype=bool)
            ok = align >= 0
            M[:, ok] = unpack(p, len(pd_))[:, align[ok]]
        ip = v["in_pool"].copy()
        for k in sel:
            ip &= M[CAND_IDX[k]]
        if not ip.any():
            continue
        d = {**v, "entry": ip, "in_pool": ip,
             "buy_trigger": np.ones(n, dtype=bool),
             "sell_signal": np.zeros(n, dtype=bool)}
        if score and score in p["raw"]:
            sc = np.full(n, np.nan, dtype=float)
            r = p["raw"][score]
            if list(pd_) == list(dates):
                sc = r.astype(float)
            else:
                sc[align >= 0] = np.asarray(r, dtype=float)[align[align >= 0]]
            d["score"] = sc
        out[code] = d
    return out


def run(dv: dict, cfg: dict, q: dict) -> dict:
    c, p = make_run_cfg(cfg, q)
    out = {}
    for name, (s, e) in WINS.items():
        m = (bt.run_backtest(dv, c, s, e, p, benchmark=bench(s, e))
             .get("metrics") or {})
        out[name] = m
    return out


def line(tag: str, m: dict, sig: int) -> str:
    f, tr, va = m["FULL"], m["TRAIN"], m["VALID"]
    yr = " ".join(f"{(m[y].get('annual_return') or 0):+6.1f}"
                  for y in ("2023", "2024", "2025", "2026"))
    return (f"{tag:<26} 信号{sig:>6}  FULL {f.get('annual_return') or 0:>7.2f}%  "
            f"TRAIN {tr.get('annual_return') or 0:>7.2f}%  "
            f"VALID {va.get('annual_return') or 0:>7.2f}%  "
            f"回撤 {f.get('max_drawdown') or 0:>7.2f}%  "
            f"{f.get('trade_count') or 0:>5}笔  "
            f"胜率 {f.get('win_rate') or 0:>5.1f}%  均笔 {f.get('avg_pnl_pct') or 0:>+.3f}%  "
            f"| {yr}")


def evaluate_all(dv, cfg, q, sig):
    rows = []
    for (npos, ppct), name in zip(POS, ["20x5", "10x10", "30x3"]):
        qq = {**q, "npos": npos, "ppct": ppct}
        m = run(dv, cfg, qq)
        rows.append({"pos": name, "metrics": m, "sig": sig})
        print(line(name, m, sig), flush=True)
    return rows


def stage():
    cfg, data, _ = base_data()
    per = load_masks()
    sig0 = int(sum(int(v["in_pool"].sum()) for v in data.values()))
    # 基线也必须走 narrowed —— 缓存数组的 buy_trigger 是「高开才买」，
    # 不替换成全 True 就测的是另一个策略（2026-09-19 第二次踩，别再踩）。
    d0 = narrowed(data, per, [])
    print(f"基线（dip 池原样，无条件收窄）信号日 {sig0}", flush=True)
    evaluate_all(d0, cfg, Q_BASE, sig0)
    rows = []
    t0 = time.time()
    for key, e, desc in CANDS:
        dv = narrowed(data, per, [key])
        sig = int(sum(int(v["in_pool"].sum()) for v in dv.values()))
        rr = evaluate_all(dv, cfg, Q_BASE, sig)
        best = max(rr, key=lambda r: min(r["metrics"]["TRAIN"]["annual_return"] or -100,
                                         r["metrics"]["VALID"]["annual_return"] or -100))
        rows.append({"sel": [key], "expr": e, "desc": desc, "sig": sig,
                     "pos": best["pos"], "metrics": best["metrics"]})
        print(f"  [{key}] {desc} {time.time()-t0:.0f}s", flush=True)
    rows.sort(key=lambda r: -(min(r["metrics"]["TRAIN"]["annual_return"] or -100,
                                  r["metrics"]["VALID"]["annual_return"] or -100)))
    print("\n=== 单条收窄：按 min(TRAIN, VALID) 年化排序 ===")
    for r in rows:
        m = r["metrics"]
        print(f"{r['sel'][0]:<8} {r['desc']:<18} {r['sig']:>6}信号 {r['pos']:>5} "
              f"FULL {m['FULL']['annual_return']:>7.2f}%  "
              f"TRAIN {m['TRAIN']['annual_return']:>7.2f}%  "
              f"VALID {m['VALID']['annual_return']:>7.2f}%  "
              f"回撤 {m['FULL']['max_drawdown']:>7.2f}%  "
              f"{m['FULL']['trade_count']:>5}笔  "
              f"{r['expr']}")
    json.dump(rows, open(OUT_JSON, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, default=str)


def load_masks():
    if not os.path.isfile(MASK_PKL):
        sys.exit("先跑 --build")
    j = pickle.load(open(MASK_PKL, "rb"))
    if [(k, e, d) for k, e, d in j["cands"]] != CANDS:
        sys.exit("候选条件清单与掩码文件不一致，请重新 --build")
    return j["per"]


def combo(top_n: int = 12, max_k: int = 2):
    cfg, data, _ = base_data()
    per = load_masks()
    rows = json.load(open(OUT_JSON, encoding="utf-8"))
    keys = [r["sel"][0] for r in rows[:top_n]]
    base_sig = int(sum(int(v["in_pool"].sum()) for v in data.values()))
    out = []
    t0 = time.time()
    for sel in [c for k in range(2, max_k + 1)
                for c in itertools.combinations(keys, k)]:
        dv = narrowed(data, per, list(sel))
        sig = int(sum(int(v["in_pool"].sum()) for v in dv.values()))
        if sig < base_sig * 0.02:
            print(f"  {sel} 信号仅 {sig}，跳过", flush=True)
            continue
        rr = evaluate_all(dv, cfg, Q_BASE, sig)
        best = max(rr, key=lambda r: min(r["metrics"]["TRAIN"]["annual_return"] or -100,
                                         r["metrics"]["VALID"]["annual_return"] or -100))
        out.append({"sel": list(sel),
                    "expr": [dict((k, e) for k, e, _d in CANDS)[k] for k in sel],
                    "desc": "+".join(dict((k, d) for k, _e, d in CANDS)[k]
                                     for k in sel),
                    "sig": sig, "pos": best["pos"], "metrics": best["metrics"]})
        print(f"  {sel} {time.time()-t0:.0f}s", flush=True)
    out.sort(key=lambda r: -(min(r["metrics"]["TRAIN"]["annual_return"] or -100,
                                 r["metrics"]["VALID"]["annual_return"] or -100)))
    print(f"\n=== 组合：按 min(TRAIN, VALID) 年化 ===")
    for r in out:
        m = r["metrics"]
        print(f"{'+'.join(r['sel']):<20} {r['desc']:<28} {r['sig']:>6}信号 {r['pos']:>5} "
              f"FULL {m['FULL']['annual_return']:>7.2f}%  "
              f"TRAIN {m['TRAIN']['annual_return']:>7.2f}%  "
              f"VALID {m['VALID']['annual_return']:>7.2f}%  "
              f"回撤 {m['FULL']['max_drawdown']:>7.2f}%  "
              f"{m['FULL']['trade_count']:>5}笔  均笔 {m['FULL']['avg_pnl_pct']:+.3f}%")
    json.dump(out, open(OUT_JSON.replace("result", "combo"), "w",
                        encoding="utf-8"), ensure_ascii=False, indent=1,
              default=str)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--stage", action="store_true")
    ap.add_argument("--combo", type=int, default=0, metavar="TOP_N")
    ap.add_argument("--max-k", type=int, default=2)
    args = ap.parse_args()
    if args.build:
        build()
    elif args.stage:
        stage()
    elif args.combo:
        combo(args.combo, args.max_k)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
