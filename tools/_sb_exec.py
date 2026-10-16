# -*- coding: utf-8 -*-
"""首板研究 · 第 7 步：可买入性分层 + 行业 + 可执行条件搜索。

⭐ 这是「首板的钱到底能不能拿到」的关键一节。
   · T0 一字板 / 开盘即涨停 ⇒ 当天**买不到**（打板收益是纸面的）
   · T+1 一字板 / 开盘即涨停 ⇒ 次日**买不到**（持有收益是纸面的）
"""
from __future__ import annotations

import io
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _sb_analyze import load  # noqa: E402

OUT = os.path.join(HERE, "_sb_exec.json")


def stat(d: pd.DataFrame) -> dict:
    r = {"n": int(len(d))}
    for y in ("y_gap1", "y_t1_close", "y_r1", "y_r3", "y_r5"):
        s = d[y].dropna()
        r[y] = None if not len(s) else round(float(s.mean()) * 100, 3)
    s = d["y_t1_close"].dropna()
    r["win"] = None if not len(s) else round(float((s > 0).mean()) * 100, 1)
    s = d["y_lianban"].dropna()
    r["lianban"] = None if not len(s) else round(float(s.mean()) * 100, 1)
    return r


def _v(x):
    return "      —   " if x is None else f"{x:>+8.2f}%"


def show(title: str, blocks: list, out: dict, key: str) -> None:
    print("\n" + "=" * 104)
    print(f"【{title}】")
    print(f"{'切片':32s} {'n':>7s} {'次日开盘':>9s} {'T1收盘':>9s} {'T1胜率':>8s} "
          f"{'持有1日':>9s} {'持有3日':>9s} {'连板率':>8s}")
    out[key] = {}
    for lab, d in blocks:
        if not len(d):
            continue
        r = stat(d)
        out[key][lab] = r
        win = "     —   " if r["win"] is None else f"{r['win']:>7.1f}%"
        lb = "     —   " if r["lianban"] is None else f"{r['lianban']:>7.1f}%"
        print(f"{lab:32s} {r['n']:>7,} {_v(r['y_gap1'])} {_v(r['y_t1_close'])} {win} "
              f"{_v(r['y_r1'])} {_v(r['y_r3'])} {lb}")


def main() -> int:
    sb = load()
    base = sb[sb["base"]].copy()
    base["dip"] = base["f_body"] + base["f_low_shadow"]     # 盘中最低离涨停价的距离
    base["can_t0"] = (base["f_open_is_limit"] == 0)          # T0 当天买得到吗
    base["can_t1"] = (base["y_t1_open_is_limit"] == 0)       # T+1 开盘买得到吗
    out = {}

    print("=" * 104)
    print("【A】T0 当天能不能买到（打板的前提）")
    n = len(base)
    print(f"  base 全部首板 {n:,}")
    print(f"    一字板          {(base['f_is_yizi'] == 1).sum():>7,}  "
          f"{(base['f_is_yizi'] == 1).mean() * 100:5.1f}%")
    print(f"    开盘即涨停(非一字) {(base['f_open_is_limit'] == 1).sum() - (base['f_is_yizi'] == 1).sum():>7,}  "
          f"{((base['f_open_is_limit'] == 1).sum() - (base['f_is_yizi'] == 1).sum()) / n * 100:5.1f}%")
    print(f"    开盘未涨停 ⇒ 能买到 {int(base['can_t0'].sum()):>7,}  "
          f"{base['can_t0'].mean() * 100:5.1f}%")
    show("A2 按「T0 可否买入」分层", [
        ("T0 买得到（开盘未涨停）", base[base["can_t0"]]),
        ("  └ 低开/平开(<1%)", base[base["can_t0"] & (base["f_gap"] < 0.01)]),
        ("  └ 高开1~5%", base[base["can_t0"] & base["f_gap"].between(0.01, 0.05)]),
        ("  └ 高开5~9%", base[base["can_t0"] & base["f_gap"].between(0.05, 0.09)]),
        ("  └ 高开>9%", base[base["can_t0"] & (base["f_gap"] >= 0.09)]),
        ("T0 买不到（一字/开盘板）", base[~base["can_t0"]]),
    ], out, "buy_t0")

    print("\n" + "=" * 104)
    print("【B】T+1 开盘买得到吗（持有收益的前提）")
    print(f"  T+1 一字板 {(base['y_t1_yizi'] == 1).sum():,}  "
          f"T+1 开盘即涨停 {(base['y_t1_open_is_limit'] == 1).sum():,}  "
          f"⇒ 买不到合计 {(~base['can_t1']).sum():,} ({(~base['can_t1']).mean() * 100:.1f}%)")
    show("B2 按「T+1 可否开盘买入」分层", [
        ("T+1 可开盘买入", base[base["can_t1"]]),
        ("T+1 买不到（一字/开盘板）", base[~base["can_t1"]]),
    ], out, "buy_t1")

    # ---- 真实可执行：T+1 可买入 -----
    ex = base[base["can_t1"]].copy()
    print("\n" + "=" * 104)
    print("【C】在「T+1 开盘能买到」的样本上，找能把持有收益做正的条件")
    print(f"  基准（可执行，n={len(ex):,}）：T+1开盘买→T+1收盘 {ex['y_r1'].mean() * 100:+.3f}% / "
          f"→T+3收盘 {ex['y_r3'].mean() * 100:+.3f}% / →T+5收盘 {ex['y_r5'].mean() * 100:+.3f}%")

    def qblocks(d, col, q=4, prefix=""):
        s = d[col]
        if s.notna().sum() < 400:
            return []
        try:
            b = pd.qcut(s, q, labels=False, duplicates="drop")
        except Exception:
            return []
        e = s.quantile(np.linspace(0, 1, q + 1)).round(3).tolist()
        return [(f"{prefix}Q{i + 1}[{e[i]:g},{e[i + 1]:g}]", d[b == i])
                for i in range(q) if (b == i).sum() > 0]

    blk = []
    blk += qblocks(ex, "turnover_rate", 4, "换手 ")
    blk += qblocks(ex, "f_vol_vs_prev", 4, "量/前日 ")
    blk += qblocks(ex, "f_gap", 4, "当日开盘涨幅 ")
    blk += qblocks(ex, "f_gap_to_hh120", 4, "距120日高 ")
    blk += qblocks(ex, "total_mv", 4, "总市值 ")
    blk += qblocks(ex, "f_pre_ret20", 4, "前20日涨幅 ")
    blk += qblocks(ex, "dip", 4, "盘中回落 ")
    blk += [("缩量<1.1x + 换手<5%", ex[(ex["f_vol_vs_prev"] < 1.1) & (ex["turnover_rate"] < 5)]),
            ("高开>3% + 距高<5%", ex[(ex["f_gap"] > 0.03) & (ex["f_gap_to_hh120"] > -0.05)]),
            ("低位(距120日高<-25%)", ex[ex["f_gap_to_hh120"] < -0.25]),
            ("高位(距120日高>-5%)", ex[ex["f_gap_to_hh120"] > -0.05])]
    show("C2 可执行口径 · 单变量分层", blk, out, "executable")

    # ---- 行业 ----
    print("\n" + "=" * 104)
    print("【D】行业（base，n>=300 才列）")
    ind = base.groupby("industry").agg(n=("y_t1_close", "size"),
                                       t1=("y_t1_close", "mean"),
                                       lb=("y_lianban", "mean"),
                                       r1=("y_r1", "mean"))
    ind = ind[ind["n"] >= 300].sort_values("t1", ascending=False)
    out["industry"] = [{"industry": i, "n": int(r.n), "t1": round(r.t1 * 100, 2),
                        "lianban": round(r.lb * 100, 1), "r1": round(r.r1 * 100, 2)}
                       for i, r in ind.iterrows()]
    print(f"  {'行业':14s} {'n':>6s} {'T1收盘':>9s} {'连板率':>8s} {'持有1日':>9s}")
    for i, r in ind.head(12).iterrows():
        print(f"  {i:14s} {int(r.n):>6,} {r.t1 * 100:>+8.2f}% {r.lb * 100:>7.1f}% {r.r1 * 100:>+8.2f}%")
    print("  ...")
    for i, r in ind.tail(6).iterrows():
        print(f"  {i:14s} {int(r.n):>6,} {r.t1 * 100:>+8.2f}% {r.lb * 100:>7.1f}% {r.r1 * 100:>+8.2f}%")

    # ---- 板块共振 ----
    print("\n" + "=" * 104)
    print("【E】同日同行业「板块共振」（当天该行业首板数）")
    d = base.copy()
    cnt = d.groupby(["trade_date", "industry"]).size().rename("ind_sb_cnt")
    d = d.merge(cnt, on=["trade_date", "industry"], how="left")
    show("E2 按同行业首板数分层", [
        ("独苗（本行业仅它1只）", d[d["ind_sb_cnt"] == 1]),
        ("2-3 只", d[d["ind_sb_cnt"].between(2, 3)]),
        ("4-6 只", d[d["ind_sb_cnt"].between(4, 6)]),
        ("7 只以上（成板块）", d[d["ind_sb_cnt"] >= 7]),
    ], out, "resonance")

    with io.open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print(f"\n写出 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
