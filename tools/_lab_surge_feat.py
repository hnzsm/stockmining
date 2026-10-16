# -*- coding: utf-8 -*-
"""对「10 日涨 50%」样本做特征分析：单规则提升度 + 训练/验证切分 + 贪心组合。

用法：
    python tools/_lab_surge_feat.py                     # 默认 2025 训练 / 2026 验证
    python tools/_lab_surge_feat.py --all               # 全区间不切分
输出：tools/_lab_surge_feat.md  以及控制台表格
"""
from __future__ import annotations

import argparse
import os

import numpy as np
import pandas as pd

NUM = [
    "close", "pct_chg", "amount", "vol", "turnover_rate", "volume_ratio",
    "total_mv", "circ_mv", "amplitude", "body", "close_pos", "gap_open",
    "ma5", "ma10", "ma20", "ma60", "ma120", "vol_ratio5",
    "pos20", "pos60", "pos120", "ret5", "ret20", "ret60", "ret120",
    "atr_pct", "boll_pos", "boll_width", "rsi6", "rsi12", "kdj_j",
    "macd_hist", "c_ma20", "c_ma60", "c_hh60", "c_hh120", "lim20", "lim60",
    "rng5", "ma_bull", "limit_up", "is_up", "is_down",
] + [f"ref{l}_{c}" for l in (1, 2) for c in
     ("pct_chg", "body", "amplitude", "close_pos", "turnover_rate",
      "vol_ratio5", "limit_up", "is_up", "gap_open", "volume_ratio")]
FLAG = ["is_up", "is_down", "limit_up", "ma_bull"]

# 每日候选股票数（用于把「命中率」换算成「每天几只」）—— 2025 年 A 股约 5400，
# 过滤掉北交所 / ST / 次新后约 4700
DAILY_UNIVERSE = 4700


def load(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["trade_date"] = df["trade_date"].astype(str)
    for c in NUM + FLAG:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def rule_stats(pos: pd.DataFrame, base: pd.DataFrame, col: str, op: str,
               thr: float):
    m = pos[col] <= thr if op == "<=" else pos[col] >= thr
    b = base[col] <= thr if op == "<=" else base[col] >= thr
    r = m.mean() if len(pos) else np.nan
    n = b.mean() if len(base) else np.nan
    if not n or np.isnan(n) or n < 0.01:      # 太窄的规则没意义
        return None
    return {"col": col, "op": op, "thr": thr, "recall": r, "noise": n,
            "lift": r / n, "hits_day": n * DAILY_UNIVERSE}


def grid(pos: pd.DataFrame, base: pd.DataFrame, cols):
    out = []
    for c in cols:
        if c not in pos.columns:
            continue
        qs = [5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 95]
        thrs = [np.nanpercentile(base[c].dropna(), q) for q in qs]
        for t in sorted(set([round(float(x), 4) for x in thrs if np.isfinite(x)])):
            for op in ("<=", ">="):
                s = rule_stats(pos, base, c, op, t)
                if s:
                    out.append(s)
    return pd.DataFrame(out)


def show(top: pd.DataFrame, n: int, title: str) -> list[str]:
    lines = [f"\n### {title}\n",
             "| 条件 | 命中样本(召回) | 全市场命中率 | 提升 | 每天几只 |",
             "|---|---|---|---|---|"]
    for _, r in top.head(n).iterrows():
        thr = round(r["thr"], 3)
        lines.append(f"`{r['col']} {r['op']} {thr}` | {r['recall']*100:.1f}% | "
                     f"{r['noise']*100:.2f}% | **×{r['lift']:.2f}** | "
                     f"{r['hits_day']:.0f} |")
    return lines


def mask(df: pd.DataFrame, col: str, op: str, thr: float) -> pd.Series:
    return (df[col] <= thr) if op == "<=" else (df[col] >= thr)


def fwd_eval(df: pd.DataFrame, m: pd.Series) -> str:
    """命中的这批票-日，未来 10 日最高涨幅的中位数 / ≥50% 的概率 / 中位数回撤。"""
    sub = df[m]
    if len(sub) < 20 or "fwd_gain10" not in sub.columns:
        return ""
    g = sub["fwd_gain10"].dropna()
    if len(g) < 20:
        return ""
    return (f" 未来10日最高涨幅中位数 {g.median()*100:.1f}%"
            f" / ≥50% 概率 {(g >= 0.5).mean()*100:.1f}%"
            f" / 中位回撤 {sub['fwd_dd10'].median()*100:.1f}%")


def greedy(pos: pd.DataFrame, base: pd.DataFrame, cand: pd.DataFrame,
           k: int = 6, min_noise: float = 0.0002):
    """贪心挑规则：每次加一条让「新组合的 lift」最高的，输出每一步的账。"""
    chosen, lines = [], []
    cur_pos = pd.Series(True, index=pos.index)
    cur_base = pd.Series(True, index=base.index)
    for _ in range(k):
        best = None
        for _, r in cand.iterrows():
            if r["col"] in [c["col"] for c in chosen]:
                continue
            np_, nb = (cur_pos & mask(pos, r["col"], r["op"], r["thr"])), \
                      (cur_base & mask(base, r["col"], r["op"], r["thr"]))
            rr, nn = np_.mean(), nb.mean()
            if nn < min_noise or rr < 0.02:
                continue
            sc = rr / nn
            if best is None or sc > best[0]:
                best = (sc, r, rr, nn)
        if best is None:
            break
        sc, r, rr, nn = best
        chosen.append({"col": r["col"], "op": r["op"], "thr": round(float(r["thr"]), 3)})
        cur_pos = cur_pos & mask(pos, r["col"], r["op"], r["thr"])
        cur_base = cur_base & mask(base, r["col"], r["op"], r["thr"])
        lines.append(f"{len(chosen)}. `{r['col']} {r['op']} {round(float(r['thr']),3)}`"
                     f" → 累计召回 {rr*100:.1f}%  命中率 {nn*100:.3f}%"
                     f"  提升 ×{sc:.2f}  每天 {nn*DAILY_UNIVERSE:.1f} 只")
    return chosen, lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", default="tools/_lab_surge_samples.csv")
    ap.add_argument("--split", default="2026-01-01",
                    help="此日期之前训练、之后验证；空=不切")
    ap.add_argument("--top", type=int, default=18)
    ap.add_argument("--max-noise", type=float, default=0.02)
    ap.add_argument("--k", type=int, default=6)
    ap.add_argument("--out", default="tools/_lab_surge_feat.md")
    args = ap.parse_args()

    df = load(args.samples)
    pos = df[df.is_surge == 1]
    base = df[df.is_surge == 0]
    print(f"观测 {len(df)}  事件样本 {len(pos)}  基线 {len(base)}")

    tr_pos, tr_base = pos, base
    va_pos, va_base = None, None
    if args.split:
        tr_pos = pos[pos.trade_date < args.split]
        tr_base = base[base.trade_date < args.split]
        va_pos = pos[pos.trade_date >= args.split]
        va_base = base[base.trade_date >= args.split]
        print(f"训练：事件 {len(tr_pos)} 基线 {len(tr_base)}   "
              f"验证：事件 {len(va_pos)} 基线 {len(va_base)}")

    cand = grid(tr_pos, tr_base, NUM)
    cand = cand.sort_values("lift", ascending=False)
    lines = ["# 10 日涨 50% · 起涨前一日特征分析\n",
             f"- 事件样本 {len(pos)}  基线样本 {len(base)}（随机票-日）",
             f"- 训练区间 < {args.split}  验证区间 >= {args.split}",
             f"- 「提升」= 召回 / 全市场命中率；每天几只 = 命中率 × {DAILY_UNIVERSE}\n"]

    lines += show(cand, args.top, "单条件提升度 TOP（训练集）")

    if va_pos is not None and len(va_pos):
        rows = []
        for _, r in cand.head(60).iterrows():
            s = rule_stats(va_pos, va_base, r["col"], r["op"], r["thr"])
            if s:
                s["train_lift"] = r["lift"]
                s["train_recall"] = r["recall"]
                rows.append(s)
        v = pd.DataFrame(rows).sort_values("lift", ascending=False)
        lines += ["\n### 同一批条件在验证区间的表现（有没有过拟合）\n",
                  "| 条件 | 训练提升 | 验证提升 | 训练召回 | 验证召回 | 每天几只 |",
                  "|---|---|---|---|---|---|"]
        for _, r in v.head(args.top).iterrows():
            lines.append(f"`{r['col']} {r['op']} {round(r['thr'],3)}` | "
                         f"×{r['train_lift']:.2f} | **×{r['lift']:.2f}** | "
                         f"{r['train_recall']*100:.0f}% | {r['recall']*100:.0f}% | "
                         f"{r['hits_day']:.0f} |")

    chosen, glines = greedy(tr_pos, tr_base, cand.head(120), args.k)
    lines += ["\n### 贪心组合（训练集上挑，逐步收紧）\n"] + glines
    lines.append("\n组合条件（可直接粘进选股池）：\n")
    for c in chosen:
        lines.append(f"    {c['col']} {c['op']} {c['thr']}")

    _mv = pd.Series(True, index=tr_pos.index)
    _mb = pd.Series(True, index=tr_base.index)
    for c in chosen:
        _mv &= mask(tr_pos, c["col"], c["op"], c["thr"])
        _mb &= mask(tr_base, c["col"], c["op"], c["thr"])
    lines.append(f"\n训练集复核：召回 {_mv.mean()*100:.1f}%  "
                 f"命中率 {_mb.mean()*100:.3f}%  每天 "
                 f"{_mb.mean()*DAILY_UNIVERSE:.1f} 只")
    lines.append("训练集命中票的未来收益：" + fwd_eval(tr_pos, _mv))
    lines.append("训练集全市场基准：" + fwd_eval(tr_base,
                                            pd.Series(True, index=tr_base.index)))

    if va_pos is not None and len(va_pos) and chosen:
        mp = pd.Series(True, index=va_pos.index)
        mb = pd.Series(True, index=va_base.index)
        for c in chosen:
            mp &= mask(va_pos, c["col"], c["op"], c["thr"])
            mb &= mask(va_base, c["col"], c["op"], c["thr"])
        rr, nn = mp.mean(), mb.mean()
        lines.append(f"\n**验证区间复核**：召回 {rr*100:.1f}%  命中率 {nn*100:.3f}%"
                     f"  提升 ×{(rr/nn if nn else 0):.2f}  "
                     f"每天 {nn*DAILY_UNIVERSE:.1f} 只")
        lines.append("验证集命中票的未来收益：" + fwd_eval(va_pos, mp))
        lines.append("验证集全市场基准：" + fwd_eval(va_base,
                                              pd.Series(True, index=va_base.index)))

    txt = "\n".join(lines) + "\n"
    open(args.out, "w", encoding="utf-8").write(txt)
    print(txt)


if __name__ == "__main__":
    main()
