# -*- coding: utf-8 -*-
"""分析「暴涨前几天的 K 线形态」，并搜出**能提前选中这些票**的选股条件。

输入 tools/_lab_surge_samples.csv（挖掘脚本产出）：
  每行一个「票-日」观测，含 T0 及之前 N 根的形态序列特征。
  is_surge=1 是事件样本（lead=0 起涨前一日，lead=1..5 提前 N 天），0 是随机基线。

关键口径（跟「逐日命中率」不是一回事）：
- **每波召回**：2275 个暴涨事件里，有多少个在 `lead <= LEADMAX` 任一天被条件命中。
  这才是「能不能选中那些票」——提前几天埋伏到，都算选到了。
- **每天几只**：条件在随机基线上的命中率 × UNIVERSE，衡量筛得有多窄。
- **提升** = 召回 / 基线命中率；**真涨概率** = 命中的票-日里真在 10 日内涨 50% 的比例。

⚠ 训练/验证按 T0 所在年份切：2025 年挑条件，2026 年复核。
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

GAIN = 0.50
UNIVERSE = 5200          # 每天参与筛选的标的数（实测 2025-06-10 5186 / 2026-03-10 5275）
LEADMAX = 3              # 「提前几天内选中」算数（训练用）


def md_table(df: pd.DataFrame) -> str:
    """自己拼 markdown 表（环境里没装 tabulate）。"""
    cols = [str(c) for c in df.columns]
    out = ["| " + " | ".join(cols) + " |",
           "|" + "|".join(["---"] * len(cols)) + "|"]
    for _, r in df.iterrows():
        out.append("| " + " | ".join("" if v is None else str(v)
                                     for v in r.tolist()) + " |")
    return "\n".join(out)


# 形态列 → 平台条件表达式（列 == 1 时的写法）
EXPR = {
    "e_bigup5": "count(pct_chg >= 5, 5) >= 1",
    "e_bigup10": "count(pct_chg >= 5, 10) >= 1",
    "e_bigup20": "count(pct_chg >= 5, 20) >= 1",
    "e_lim5": "count(limit_up == 1, 5) >= 1",
    "e_lim10": "count(limit_up == 1, 10) >= 1",
    "e_lim20": "count(limit_up == 1, 20) >= 1",
    "e_lim60": "count(limit_up == 1, 60) >= 1",
    "e_vspike5": "count(vol >= ma(vol, 5) * 2, 5) >= 1",
    "e_vspike10": "count(vol >= ma(vol, 5) * 2, 10) >= 1",
    "e_vspike20": "count(vol >= ma(vol, 5) * 2, 20) >= 1",
    "e_star5": "count(abs(body) <= 1.5 and amplitude >= 3, 5) >= 1",
    "e_star10": "count(abs(body) <= 1.5 and amplitude >= 3, 10) >= 1",
    "e_upsh5": "count(upper_shadow >= 3, 5) >= 1",
    "e_upsh10": "count(upper_shadow >= 3, 10) >= 1",
    "e_dnsh5": "count(lower_shadow >= 3, 5) >= 1",
    "e_dnsh10": "count(lower_shadow >= 3, 10) >= 1",
    "e_bigdn5": "count(pct_chg <= -5, 5) >= 1",
    "e_bigdn10": "count(pct_chg <= -5, 10) >= 1",
    "e_amp8_5": "count(amplitude >= 8, 5) >= 1",
    "e_amp8_10": "count(amplitude >= 8, 10) >= 1",
    "e_ma20t5": "count(low <= ma20 and close > ma20, 5) >= 1",
    "e_ma20t10": "count(low <= ma20 and close > ma20, 10) >= 1",
    "e_ma60t5": "count(low <= ma60 and close > ma60, 5) >= 1",
    "e_ma60t10": "count(low <= ma60 and close > ma60, 10) >= 1",
    "e_gold5": "count(cross_above(ma5, ma10), 5) >= 1",
    "e_gold10": "count(cross_above(ma5, ma10), 10) >= 1",
    "e_shrink5": "mean(vol, 5) < mean(vol, 20) * 0.8",
    "e_ydown3": "count(is_down == 1, 3) >= 1",
    "e_flat5": "std(close, 5) / mean(close, 5) * 100 <= 3",
    "rng5": "(highest(high, 5) - lowest(low, 5)) / close * 100",
    "rng10": "(highest(high, 10) - lowest(low, 10)) / close * 100",
    "rng20": "(highest(high, 20) - lowest(low, 20)) / close * 100",
    "amp5": "mean(amplitude, 5)",
    "amp10": "mean(amplitude, 10)",
    "amp20": "mean(amplitude, 20)",
    "narrow5": "std(close, 5) / mean(close, 5) * 100",
    "narrow10": "std(close, 10) / mean(close, 10) * 100",
    "narrow20": "std(close, 20) / mean(close, 20) * 100",
    "volshr5": "mean(vol, 5) / mean(vol, 20)",
    "volshr10": "mean(vol, 10) / mean(vol, 20)",
    "volshr20": "mean(vol, 20) / mean(vol, 20)",
    "maxup5": "highest(pct_chg, 5)",
    "maxup10": "highest(pct_chg, 10)",
    "maxdn5": "lowest(pct_chg, 5)",
    "maxdn10": "lowest(pct_chg, 10)",
    "maxvolx5": "highest(vol / ma(vol, 5), 5)",
    "maxvolx10": "highest(vol / ma(vol, 10), 10)",
    "barslast_lim": "barslast(limit_up == 1)",
    "ret120": "ret120",
    "ret60": "ret60",
    "ret20": "ret20",
    "c_ma60": "close / ma60 - 1",
    "c_ma20": "close / ma20 - 1",
    "atr_pct": "atr_pct",
    "turnover_rate": "turnover_rate",
    "volume_ratio": "volume_ratio",
    "amplitude": "amplitude",
    "pct_chg": "pct_chg",
    "lim20": "count(limit_up == 1, 20)",
    "lim60": "count(limit_up == 1, 60)",
    "pos60": "pos60",
    "ma_bull": "ma_bull",
    "circ_mv": "circ_mv",
    "cnt_up5": "count(is_up == 1, 5)",
    "sum_ret5": "sum(pct_chg, 5)",
    "dn5": "count(is_down == 1, 5)",
}


def neg_expr(e: str) -> str:
    """列 == 0 的写法。`count(...) >= 1` 取反写成 `count(...) == 0`。"""
    if e.startswith("count(") and e.endswith(">= 1"):
        return e[:-5] + "== 0"
    if "<=" in e:
        i = e.index("<=")
        return e[:i] + ">" + e[i + 2:]
    if ">=" in e:
        i = e.index(">=")
        return e[:i] + "<" + e[i + 2:]
    if "<" in e:
        i = e.index("<")
        return e[:i] + ">=" + e[i + 1:]
    if ">" in e:
        i = e.index(">")
        return e[:i] + "<=" + e[i + 1:]
    return "not (" + e + ")"


SCAN_NUM = ["rng5", "rng10", "amp5", "amp10", "narrow5", "narrow10",
            "volshr5", "maxup5", "maxup10", "maxdn5", "maxvolx5",
            "barslast_lim", "ret120", "ret60", "ret20", "c_ma60", "c_ma20",
            "atr_pct", "turnover_rate", "volume_ratio", "amplitude", "pct_chg",
            "lim20", "lim60", "pos60", "ma_bull", "circ_mv", "cnt_up5",
            "sum_ret5", "dn5"]
SCAN_EVT = ["e_bigup5", "e_bigup10", "e_lim5", "e_lim10", "e_lim20",
            "e_vspike5", "e_vspike10", "e_star5", "e_upsh5", "e_upsh10",
            "e_dnsh5", "e_dnsh10", "e_bigdn5", "e_bigdn10", "e_amp8_5",
            "e_amp8_10", "e_ma20t5", "e_ma60t5", "e_gold5", "e_shrink5",
            "e_ydown3", "e_flat5"]


def mask_of(df: pd.DataFrame, col: str, op: str, thr: float) -> pd.Series:
    x = pd.to_numeric(df[col], errors="coerce")
    return x >= thr if op == ">=" else x <= thr


def build_cands(pos: pd.DataFrame, base: pd.DataFrame, pos_key: pd.Series):
    """扫描全部单条件，返回候选表（含每波召回）。"""
    out = []
    for c in SCAN_EVT:
        if c not in pos.columns:
            continue
        for op, thr in ((">=", 1.0), ("<=", 0.0)):
            e = EXPR.get(c)
            if not e:
                continue
            mp, mb = mask_of(pos, c, op, thr), mask_of(base, c, op, thr)
            rec = wave_recall(mp, pos_key)
            nn = mb.mean()
            if nn <= 0 or nn > 0.75:
                continue
            out.append({"col": c, "op": op, "thr": thr, "rec": rec,
                        "base": nn, "lift": rec / nn,
                        "expr": e if op == ">=" else neg_expr(e)})
    for c in SCAN_NUM:
        if c not in pos.columns:
            continue
        x = pd.to_numeric(base[c], errors="coerce").dropna()
        if len(x) < 100:
            continue
        qs = np.unique(np.round(x.quantile([0.5, 0.7, 0.8, 0.9, 0.95,
                                            0.98]).to_numpy(), 4))
        for q in qs:
            for op in (">=", "<="):
                mp, mb = mask_of(pos, c, op, q), mask_of(base, c, op, q)
                rec = wave_recall(mp, pos_key)
                nn = mb.mean()
                if nn < 0.01 or nn > 0.75:
                    continue
                out.append({"col": c, "op": op, "thr": float(q), "rec": rec,
                            "base": nn, "lift": rec / nn,
                            "expr": f"{EXPR.get(c, c)} {op} {round(float(q), 4)}"})
    return pd.DataFrame(out).sort_values("lift", ascending=False)


def wave_recall(m: pd.Series, key: pd.Series) -> float:
    """每波召回：按 (票, 起涨日) 分组，任一天命中即该波命中。"""
    if not len(m):
        return 0.0
    return float(pd.Series(m.values, index=key.values).groupby(level=0).max().mean())


def greedy(pos, base, cand, pos_key, budget, k=6):
    """预算内最大化「每波召回」：每步挑满足 base<=budget 且召回最高的。"""
    chosen = []
    cur_p = pd.Series(True, index=pos.index)
    cur_b = pd.Series(True, index=base.index)
    for _ in range(k):
        best = None
        for _, r in cand.iterrows():
            if r["col"] in [c["col"] for c in chosen]:
                continue
            np_ = cur_p & mask_of(pos, r["col"], r["op"], r["thr"])
            nb = cur_b & mask_of(base, r["col"], r["op"], r["thr"])
            nn = nb.mean()
            if nn < 0.0002 or nn > budget:
                continue
            rr = wave_recall(np_, pos_key)
            # 在预算内尽量多召回；同等召回时选更窄的
            sc = (rr, -nn)
            if best is None or sc > best[0]:
                best = (sc, r, rr, nn)
        if best is None:
            break
        _sc, r, rr, nn = best
        chosen.append({"col": r["col"], "op": r["op"], "thr": r["thr"],
                       "expr": r["expr"]})
        cur_p = cur_p & mask_of(pos, r["col"], r["op"], r["thr"])
        cur_b = cur_b & mask_of(base, r["col"], r["op"], r["thr"])
    return chosen


def apply_combo(df: pd.DataFrame, chosen) -> pd.Series:
    m = pd.Series(True, index=df.index)
    for c in chosen:
        m &= mask_of(df, c["col"], c["op"], c["thr"])
    return m


def eval_combo(pos, base, pos_key, chosen, tag):
    mp, mb = apply_combo(pos, chosen), apply_combo(base, chosen)
    rr, nn = wave_recall(mp, pos_key), mb.mean()
    hit = base[mb]
    prec = (pd.to_numeric(hit["fwd_gain10"], errors="coerce") >= GAIN).mean()
    med = pd.to_numeric(hit["fwd_ret10"], errors="coerce").median()
    return {"区间": tag, "每波召回": round(rr * 100, 1),
            "每天几只": round(nn * UNIVERSE, 1),
            "提升": round(rr / nn, 2) if nn else 0,
            "真涨概率": round(prec * 100, 2) if pd.notna(prec) else None,
            "期望10日": round(med * 100, 2) if pd.notna(med) else None}


def main():
    global UNIVERSE
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", default="tools/_lab_surge_samples.csv")
    ap.add_argument("--out", default="tools/_lab_surge_shape.md")
    ap.add_argument("--universe", type=int, default=UNIVERSE)
    ap.add_argument("--leadmax", type=int, default=LEADMAX)
    ap.add_argument("--min-lift", type=float, default=1.25)
    args = ap.parse_args()
    UNIVERSE = args.universe

    df = pd.read_csv(args.samples)
    df["yr"] = df["ev_t0"].astype(str).str[:4]
    base = df[df.is_surge == 0].copy()
    pos = df[(df.is_surge == 1) & (df.lead <= args.leadmax)].copy()
    pos["yr"] = pos["ev_t0"].astype(str).str[:4]      # 按起涨日年份切
    key = pos["ts_code"] + "|" + pos["ev_t0"].astype(str)
    tr_pos, va_pos = pos[pos.yr == "2025"], pos[pos.yr == "2026"]
    tr_base, va_base = base[base.trade_date.astype(str).str[:4] == "2025"], \
        base[base.trade_date.astype(str).str[:4] == "2026"]
    tr_key = tr_pos["ts_code"] + "|" + tr_pos["ev_t0"].astype(str)
    va_key = va_pos["ts_code"] + "|" + va_pos["ev_t0"].astype(str)
    nkey = key.nunique()
    rate = (pd.to_numeric(base["fwd_gain10"], errors="coerce") >= GAIN).mean()
    L = []
    L.append("# 暴涨前几天的 K 线形态分析\n")
    L.append(f"- 暴涨事件 **{nkey}** 波（2025 年 {tr_key.nunique()} / "
             f"2026 年 {va_key.nunique()}），每波取起涨前 0~{args.leadmax} 天\n"
             f"- 正样本 {len(pos)} 条；随机基线 {len(base)} 条\n"
             f"- 全市场任一票-日「10 日内涨 50%」的自然概率 = **{rate*100:.3f}%**\n"
             f"- 每天参与筛选标的按 {args.universe} 只换算\n"
             f"- 「每波召回」= 一波暴涨里，起涨前 0~{args.leadmax} 天**任一天**被条件选中就算选到了\n"
             f"- 条件在 **2025 年**挑，**2026 年**复核\n")

    cand = build_cands(tr_pos, tr_base, tr_key)
    cand = cand[cand.lift >= args.min_lift]
    L.append("\n## 一、单特征提升（只看提升 ≥ %.2f 的）\n" % args.min_lift)
    L.append("「样本」= 暴涨前几天出现该形态的比例，「基线」= 全市场随机票-日。\n")
    top = cand.drop_duplicates("col").sort_values("lift", ascending=False).head(26)
    L.append("| 形态 / 特征 | 条件写法 | 样本 | 基线 | 提升 |")
    L.append("|---|---|---|---|---|")
    for _, r in top.iterrows():
        L.append(f"| {r['col']} | `{r['expr']}` | {r['rec']*100:.1f}% | "
                 f"{r['base']*100:.1f}% | **×{r['lift']:.2f}** |")

    L.append("\n### 波动 / 位置画像（中位数，起涨前一日）\n")
    L.append("| 特征 | 含义 | 暴涨前夜 | 全市场 |")
    L.append("|---|---|---|---|")
    MEAN = {"rng5": "近 5 日最高-最低跨度 %", "rng10": "近 10 日跨度 %",
            "amp5": "近 5 日均振幅 %", "amp10": "近 10 日均振幅 %",
            "narrow5": "近 5 日收盘波动系数 %", "volshr5": "近 5 日量 / 近 20 日量",
            "maxup5": "近 5 日最大单日涨幅 %", "maxdn5": "近 5 日最大单日跌幅 %",
            "maxvolx5": "近 5 日最大量 / 5 日均量", "ret120": "近 120 日涨幅",
            "ret60": "近 60 日涨幅", "ret20": "近 20 日涨幅",
            "c_ma60": "收盘 / MA60 - 1", "atr_pct": "ATR 占价 %",
            "turnover_rate": "换手率 %", "barslast_lim": "距上次涨停天数",
            "circ_mv": "流通市值（万元）", "lim20": "近 20 日涨停天数"}
    p0 = df[(df.is_surge == 1) & (df.lead == 0)]
    for c, m in MEAN.items():
        if c not in df.columns:
            continue
        a = pd.to_numeric(p0[c], errors="coerce").median()
        b = pd.to_numeric(base[c], errors="coerce").median()
        L.append(f"| {c} | {m} | **{a:.2f}** | {b:.2f} |")

    # ---------- 三档组合 ----------
    L.append("\n## 二、三档选股条件（预算内最大化召回，2025 训练 / 2026 复核）\n")
    plans = {}
    for tag, budget in (("宽·召回优先", 0.06), ("中·均衡", 0.02), ("严·精度优先", 0.006)):
        chosen = greedy(tr_pos, tr_base, cand.head(80), tr_key, budget, k=6)
        if not chosen:
            continue
        plans[tag] = chosen
        L.append(f"\n### {tag}（目标每天 ≤ {round(budget*UNIVERSE)} 只）\n")
        L.append("```")
        for c in chosen:
            L.append(f"{c['expr']}")
        L.append("```\n")
        rows = [eval_combo(tr_pos, tr_base, tr_key, chosen, "2025 训练"),
                eval_combo(va_pos, va_base, va_key, chosen, "2026 验证")]
        L.append(md_table(pd.DataFrame(rows)))

    # ---------- 提前天数曲线 ----------
    tag = "中·均衡" if "中·均衡" in plans else (list(plans)[0] if plans else None)
    if tag:
        chosen = plans[tag]
        L.append(f"\n## 三、提前几天能选中（方案「{tag}」）\n")
        L.append("| 提前天数 | 该天被选中的事件占比 | 命中后 10 日涨幅中位数 |")
        L.append("|---|---|---|")
        for k in range(0, 6):
            sub = df[(df.is_surge == 1) & (df.lead == k)]
            if not len(sub):
                continue
            m = apply_combo(sub, chosen)
            r10 = pd.to_numeric(sub.loc[m, "fwd_ret10"], errors="coerce").median()
            L.append(f"| lead={k}（提前 {k} 天） | {m.mean()*100:.1f}% | "
                     f"{r10*100 if pd.notna(r10) else float('nan'):.2f}% |")

        # ---------- 预算-召回曲线 ----------
        L.append("\n## 五、要选到多少，得每天看几只（2026 验证）\n")
        L.append("横轴是条件放宽的程度，纵轴是能选中的暴涨波数。\n")
        rows = []
        for budget in (0.004, 0.008, 0.015, 0.03, 0.06, 0.12, 0.25):
            ch = greedy(tr_pos, tr_base, cand.head(80), tr_key, budget, k=6)
            if not ch:
                continue
            r = eval_combo(va_pos, va_base, va_key, ch, "")
            rows.append({"每天几只": r["每天几只"], "每波召回": r["每波召回"],
                         "提升": r["提升"], "真涨概率": r["真涨概率"],
                         "期望10日": r["期望10日"]})
        L.append(md_table(pd.DataFrame(rows)))

        # ---------- 分型：暴涨票不止一种形态 ----------
        L.append("\n## 六、分型挖掘：按「起涨前位置」分三类各自找特征\n")
        L.append("三类条件**取并集**（满足任一即入选），所以召回能叠加。\n")
        TYPES = [("A·高位强势", tr_pos["ret60"] >= 0.30),
                 ("B·中位启动", (tr_pos["ret60"] >= 0.0) & (tr_pos["ret60"] < 0.30)),
                 ("C·低位反转", tr_pos["ret60"] < 0.0)]
        type_ch = {}
        for tname, sel in TYPES:
            sub = tr_pos[sel]
            if len(sub) < 50:
                continue
            skey = sub["ts_code"] + "|" + sub["ev_t0"].astype(str)
            c2 = build_cands(sub, tr_base, skey)
            c2 = c2[c2.lift >= 1.3]
            ch = greedy(sub, tr_base, c2.head(80), skey, 0.03, k=5)
            if not ch:
                continue
            type_ch[tname] = ch
            L.append(f"\n### {tname}（2025 年该类 {skey.nunique()} 波）\n")
            L.append("```")
            for c in ch:
                L.append(f"{c['expr']}")
            L.append("```\n")
            rr, nn = wave_recall(apply_combo(sub, ch), skey), \
                apply_combo(tr_base, ch).mean()
            L.append(f"单类：该类召回 **{rr*100:.1f}%**，每天 {nn*UNIVERSE:.0f} 只"
                     f"，提升 ×{rr/nn if nn else 0:.1f}\n")
        if type_ch:
            # 并集：类内 AND，类间 OR
            def any_hit(frame):
                m = pd.Series(False, index=frame.index)
                for ch in type_ch.values():
                    mm = pd.Series(True, index=frame.index)
                    for c in ch:
                        mm &= mask_of(frame, c["col"], c["op"], c["thr"])
                    m |= mm
                return m
            mp, mb = any_hit(va_pos), any_hit(va_base)
            rr = wave_recall(mp, va_key)
            nn = mb.mean()
            hit = va_base[mb]
            prec = (pd.to_numeric(hit["fwd_gain10"], errors="coerce") >= GAIN).mean()
            med = pd.to_numeric(hit["fwd_ret10"], errors="coerce").median()
            nb_tr = any_hit(tr_base).mean()
            rr_tr = wave_recall(any_hit(tr_pos), tr_key)
            L.append("\n### 三类合并（并集）\n")
            L.append(md_table(pd.DataFrame([
                {"区间": "2025 训练", "每波召回": round(rr_tr * 100, 1),
                 "每天几只": round(nb_tr * UNIVERSE, 1),
                 "提升": round(rr_tr / nb_tr, 2) if nb_tr else 0,
                 "真涨概率": round((pd.to_numeric(
                     tr_base.loc[any_hit(tr_base), "fwd_gain10"],
                     errors="coerce") >= GAIN).mean() * 100, 2),
                 "期望10日": round(pd.to_numeric(
                     tr_base.loc[any_hit(tr_base), "fwd_ret10"],
                     errors="coerce").median() * 100, 2)},
                {"区间": "2026 验证", "每波召回": round(rr * 100, 1),
                 "每天几只": round(nn * UNIVERSE, 1),
                 "提升": round(rr / nn, 2) if nn else 0,
                 "真涨概率": round(prec * 100, 2),
                 "期望10日": round(med * 100, 2)}])))

            # ---------- 七、多方案 OR：把召回堆上去 ----------
            L.append("\n## 七、多方案取并集（把召回堆上去）\n")
            L.append("以 lift 最高的若干特征各起一条方案，每条再收紧 1~2 条；"
                     "方案之间**取并集**（满足任一即入选）。\n")
            plans_or, seen = [], set()
            for _, sd in cand.drop_duplicates("col").head(10).iterrows():
                ch = [{"col": sd["col"], "op": sd["op"], "thr": sd["thr"],
                       "expr": sd["expr"]}]
                cur_p = mask_of(tr_pos, sd["col"], sd["op"], sd["thr"])
                cur_b = mask_of(tr_base, sd["col"], sd["op"], sd["thr"])
                for _ in range(2):
                    best = None
                    for _, r in cand.iterrows():
                        if r["col"] in [c["col"] for c in ch]:
                            continue
                        np_ = cur_p & mask_of(tr_pos, r["col"], r["op"], r["thr"])
                        nb = cur_b & mask_of(tr_base, r["col"], r["op"], r["thr"])
                        nn = nb.mean()
                        if nn < 0.0005 or nn > 0.02:
                            continue
                        sc = (wave_recall(np_, tr_key), -nn)
                        if best is None or sc > best[0]:
                            best = (sc, r, sc[0], nn)
                    if best is None:
                        break
                    _s, r, rr, nn = best
                    ch.append({"col": r["col"], "op": r["op"], "thr": r["thr"],
                               "expr": r["expr"]})
                    cur_p = cur_p & mask_of(tr_pos, r["col"], r["op"], r["thr"])
                    cur_b = cur_b & mask_of(tr_base, r["col"], r["op"], r["thr"])
                sig = tuple(c["expr"] for c in ch)
                if sig not in seen:
                    seen.add(sig)
                    plans_or.append(ch)

            def or_hit(frame, plans):
                m = pd.Series(False, index=frame.index)
                for ch in plans:
                    mm = pd.Series(True, index=frame.index)
                    for c in ch:
                        mm &= mask_of(frame, c["col"], c["op"], c["thr"])
                    m |= mm
                return m

            for i, ch in enumerate(plans_or, 1):
                L.append(f"\n**方案 {i}**：`{'` 且 `'.join(c['expr'] for c in ch)}`\n")
            rows = []
            for n in range(1, len(plans_or) + 1):
                sub = plans_or[:n]
                mp, mb = or_hit(va_pos, sub), or_hit(va_base, sub)
                rr, nn = wave_recall(mp, va_key), mb.mean()
                hit = va_base[mb]
                rows.append({"并集条数": n,
                             "每天几只": round(nn * UNIVERSE, 1),
                             "每波召回(2026)": round(rr * 100, 1),
                             "提升": round(rr / nn, 2) if nn else 0,
                             "真涨概率": round((pd.to_numeric(
                                 hit["fwd_gain10"], errors="coerce")
                                 >= GAIN).mean() * 100, 2),
                             "期望10日": round(pd.to_numeric(
                                 hit["fwd_ret10"], errors="coerce").median() * 100, 2)})
            L.append("\n" + md_table(pd.DataFrame(rows)) + "\n")

        # 被选中的例子（2026，每波一条）
        m = apply_combo(va_pos, chosen)
        hit = va_pos[m].copy()
        hit["gain"] = pd.to_numeric(hit["ev_gain"], errors="coerce") * 100
        hit = hit.sort_values("gain", ascending=False)
        hit = hit.drop_duplicates("ev_t0").head(15)
        L.append(f"\n## 四、2026 年真被选中的暴涨票（方案「{tag}」，按涨幅前 15）\n")
        L.append("| 代码 | 名称 | 信号日 | 提前几天 | 起涨日 T0 | 10 日内涨幅 |")
        L.append("|---|---|---|---|---|---|")
        for _, r in hit.iterrows():
            L.append(f"| {r['ts_code']} | {r['name']} | {r['trade_date']} | "
                     f"{int(r['lead'])} | {r['ev_t0']} | +{r['gain']:.1f}% |")

    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("已写", args.out)


if __name__ == "__main__":
    main()
