# -*- coding: utf-8 -*-
"""首板研究 · 第 6 步：分市场环境 / 情绪周期 / 季节性 / 池门槛复核。

只读。产物 tools/_sb_env.json + 终端表格。
所有切片变量都只用「T0 日收盘即可知」的信息，不做未来函数。
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

OUT = os.path.join(HERE, "_sb_env.json")
YS = ("y_gap1", "y_t1_close", "y_r1", "y_r3")


def stat(d: pd.DataFrame) -> dict:
    r = {"n": int(len(d))}
    for y in YS:
        s = d[y].dropna()
        r[y] = None if not len(s) else round(float(s.mean()) * 100, 3)
    s = d["y_t1_close"].dropna()
    r["win"] = None if not len(s) else round(float((s > 0).mean()) * 100, 1)
    s = d["y_lianban"].dropna()
    r["lianban"] = None if not len(s) else round(float(s.mean()) * 100, 1)
    return r


def _v(x, suf="%"):
    return "      —   " if x is None else f"{x:>+8.2f}{suf}"


def show(title: str, blocks: list, out: dict, key: str) -> None:
    print("\n" + "=" * 104)
    print(f"【{title}】")
    print(f"{'切片':30s} {'n':>7s} {'次日开盘':>9s} {'T1收盘':>9s} {'T1胜率':>8s} "
          f"{'持有1日':>9s} {'持有3日':>9s} {'连板率':>8s}")
    out[key] = {}
    for lab, d in blocks:
        if not len(d):
            continue
        r = stat(d)
        out[key][lab] = r
        win = "     —   " if r["win"] is None else f"{r['win']:>7.1f}%"
        lb = "     —   " if r["lianban"] is None else f"{r['lianban']:>7.1f}%"
        print(f"{lab:30s} {r['n']:>7,} {_v(r['y_gap1'])} {_v(r['y_t1_close'])} {win} "
              f"{_v(r['y_r1'])} {_v(r['y_r3'])} {lb}")


def qblocks(d: pd.DataFrame, col: str, q: int = 5, prefix: str = "") -> list:
    s = d[col]
    if s.notna().sum() < 300:
        return []
    try:
        b = pd.qcut(s, q, labels=False, duplicates="drop")
    except Exception:
        return []
    edges = s.quantile(np.linspace(0, 1, q + 1)).round(4).tolist()
    return [(f"{prefix}Q{i + 1} [{edges[i]:g},{edges[i + 1]:g}]", d[b == i])
            for i in range(q) if (b == i).sum() > 0]


def main() -> int:
    sb = load()
    pool = sb[sb["in_pool"]].copy()
    base = sb[sb["base"]].copy()
    out = {"pool_n": int(len(pool)), "base_n": int(len(base))}

    # ---- 0 池门槛复核：三道门槛各自砍掉了什么 ----
    print("=" * 104)
    print("【0】首板池的三道门槛，单独看各自的效果（起点 = base 全部首板）")
    print(f"{'样本':30s} {'n':>7s} {'次日开盘':>9s} {'T1收盘':>9s} {'T1胜率':>8s} "
          f"{'持有1日':>9s} {'持有3日':>9s} {'连板率':>8s}")
    blocks = [("base（全部首板）", base),
              ("  市值<500亿", base[base["total_mv"] < 5000000]),
              ("  市值>=500亿", base[base["total_mv"] >= 5000000]),
              ("  股价<30", base[base["close"] < 30]),
              ("  股价>=30", base[base["close"] >= 30]),
              ("  换手>=3%", base[base["turnover_rate"] >= 3]),
              ("  换手<3%", base[base["turnover_rate"] < 3])]
    # 手工打印（这段要展示"被砍掉的那一半"）
    out["gate"] = {}
    for lab, d in blocks:
        r = stat(d)
        out["gate"][lab.strip()] = r
        win = "     —   " if r["win"] is None else f"{r['win']:>7.1f}%"
        lb = "     —   " if r["lianban"] is None else f"{r['lianban']:>7.1f}%"
        print(f"{lab:30s} {r['n']:>7,} {_v(r['y_gap1'])} {_v(r['y_t1_close'])} {win} "
              f"{_v(r['y_r1'])} {_v(r['y_r3'])} {lb}")

    # ---- 1 大盘 ----
    blocks = [("大盘上涨日", pool[pool["idx_sh"] > 0]),
              ("大盘下跌日", pool[pool["idx_sh"] <= 0]),
              ("上证 > MA20", pool[pool["idx_sh_above_ma20"] == 1]),
              ("上证 < MA20", pool[pool["idx_sh_above_ma20"] == 0]),
              ("创业板 > MA20", pool[pool["idx_cyb_above_ma20"] == 1]),
              ("创业板 < MA20", pool[pool["idx_cyb_above_ma20"] == 0])]
    blocks += qblocks(pool, "idx_sh_ret20", 5, "上证20日涨幅 ")
    blocks += qblocks(pool, "mkt_med_pct", 5, "全市场中位涨幅 ")
    show("1 大盘环境", blocks, out, "market")

    # ---- 2 情绪周期 ----
    blocks = [("昨日涨停股今日涨>0", pool[pool["emo_zt_ret_1"] > 0]),
              ("昨日涨停股今日跌<=0", pool[pool["emo_zt_ret_1"] <= 0])]
    blocks += qblocks(pool, "emo_zt_ret_1", 5, "隔夜赚钱效应 ")
    blocks += qblocks(pool, "emo_zt_ret_ma5", 5, "5日赚钱效应 ")
    blocks += qblocks(pool, "zt_cnt", 5, "涨停家数 ")
    blocks += qblocks(pool, "lianban_cnt", 5, "连板家数 ")
    show("2 情绪周期", blocks, out, "emotion")

    # ---- 3 季节 / 日历 ----
    blocks = [(f"{m:>2d} 月", d) for m, d in pool.groupby(pool["trade_date"].dt.month)]
    show("3a 月份", blocks, out, "month")

    blocks = [(n, pool[pool["trade_date"].dt.dayofweek == w])
              for w, n in zip(range(5), ("周一", "周二", "周三", "周四", "周五"))]
    show("3b 星期", blocks, out, "weekday")

    dom = pool["trade_date"].dt.day
    mon = pool["trade_date"].dt.month
    blocks = [("月初(1-10日)", pool[dom <= 10]),
              ("月中(11-20日)", pool[(dom > 10) & (dom <= 20)]),
              ("月末(21日后)", pool[dom > 20]),
              ("财报月(4/8/10)", pool[mon.isin([4, 8, 10])]),
              ("非财报月", pool[~mon.isin([4, 8, 10])]),
              ("1-2月", pool[mon.isin([1, 2])]),
              ("3-4月", pool[mon.isin([3, 4])]),
              ("5-6月", pool[mon.isin([5, 6])]),
              ("7-8月", pool[mon.isin([7, 8])]),
              ("9-10月", pool[mon.isin([9, 10])]),
              ("11-12月", pool[mon.isin([11, 12])])]
    show("3c 月内位置 / 财报月", blocks, out, "calendar")

    # ---- 4 分区间复核 ----
    print("\n" + "=" * 104)
    print("【4】关键切片 · 分区间复核（TRAIN 2023-01~2024-12 / VALID 2025-01~2026-09）")
    print(f"{'切片':30s} {'TRAIN n':>8s} {'TRAIN T1':>9s} {'TRAIN 连板':>10s} "
          f"{'VALID n':>8s} {'VALID T1':>9s} {'VALID 连板':>10s}")
    checks = [("上证>MA20", pool[pool["idx_sh_above_ma20"] == 1]),
              ("上证<MA20", pool[pool["idx_sh_above_ma20"] == 0]),
              ("创业板>MA20", pool[pool["idx_cyb_above_ma20"] == 1]),
              ("创业板<MA20", pool[pool["idx_cyb_above_ma20"] == 0])]
    p80 = pool["emo_zt_ret_ma5"].quantile(0.8)
    p20 = pool["emo_zt_ret_ma5"].quantile(0.2)
    checks += [("赚钱效应 高20%", pool[pool["emo_zt_ret_ma5"] >= p80]),
               ("赚钱效应 低20%", pool[pool["emo_zt_ret_ma5"] <= p20]),
               ("涨停家数 高20%", pool[pool["zt_cnt"] >= pool["zt_cnt"].quantile(0.8)]),
               ("涨停家数 低20%", pool[pool["zt_cnt"] <= pool["zt_cnt"].quantile(0.2)]),
               ("── 换手<5%", base[base["turnover_rate"] < 5]),
               ("── 换手>=10%", base[base["turnover_rate"] >= 10]),
               ("── 缩量<1.1x前日", base[base["f_vol_vs_prev"] < 1.1]),
               ("── 放量>3x前日", base[base["f_vol_vs_prev"] > 3]),
               ("── 高开>3%涨停", base[base["f_gap"] > 0.03]),
               ("── 距120日高点<5%", base[base["f_gap_to_hh120"] > -0.05])]
    out["split_check"] = {}
    for lab, d in checks:
        tr, va = d[d["train"]], d[~d["train"]]
        a, b = stat(tr), stat(va)
        out["split_check"][lab] = {"train": a, "valid": b}
        g = lambda r: ("     —   " if r["y_t1_close"] is None else f"{r['y_t1_close']:>+8.2f}%")
        lb = lambda r: ("     —   " if r["lianban"] is None else f"{r['lianban']:>9.1f}%")
        print(f"{lab:30s} {a['n']:>8,} {g(a)} {lb(a)} {b['n']:>8,} {g(b)} {lb(b)}")

    with io.open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print(f"\n写出 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
