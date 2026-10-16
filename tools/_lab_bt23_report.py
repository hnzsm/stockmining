# -*- coding: utf-8 -*-
"""「2025 年收益为正」的那批组合，放到 2023-01-01~2026-09-28（近 4 年）再跑一遍，做对照。

    python tools/_lab_bt23_report.py

两侧都读平台回测结果 json（run_id 都在 /backtest 里能点开看逐笔）：
  _lab_bt23_probe.json / _lab_bt23_base.json  = 原池基线
  _lab_bt23_tight.json                        = 收紧副本池
  _lab_bt_metrics.json                        = 2025 全年那批（40 条），用来对照

判定看「2023 起」这一段（同期沪深300 +11.65%）：
  收益 > +11.65%  → ✅ 真稳（跑赢指数）
  0 ~ +11.65%     → ・正收益但没跑赢指数
  < 0             → ⚠ 2025 那次是牛市运气
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T = os.path.join(ROOT, "tools")

BENCH = 11.65          # 沪深300 2023-01-03~2026-09-28
DAYS = 3.74            # 区间年数
SPAN = "2023-01-01~2026-09-28"


def load(name: str) -> list[dict]:
    p = os.path.join(T, name)
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def num(v, nd=1):
    return "—" if v is None else f"{float(v):.{nd}f}"


def ann(r):
    """把区间总收益折成年化。"""
    if r is None:
        return None
    try:
        return ((1 + float(r) / 100) ** (1 / DAYS) - 1) * 100
    except Exception:
        return None


def main() -> int:
    rows = load("_lab_bt23_probe.json") + load("_lab_bt23_base.json") \
        + load("_lab_bt23_tight.json")
    if not rows:
        print("没有 2023 起的结果文件")
        return 1
    # 2025 那批：名字 → 收益
    old = {r["name"]: r for r in load("_lab_bt_metrics.json")}

    out = []
    for r in rows:
        nm = r["name"]
        tight = "收紧" in nm
        pool = r["pool"]
        # 2025 那条的名字是「池·原池基线/收紧Tx · 策略 · 2025」，按「池+是否收紧」模糊配对
        key = None
        for k in old:
            if k.startswith(pool) and ("收紧" in k) == tight and k.endswith("2025"):
                key = k
                break
        o = old.get(key) or {}
        a = ann(r.get("ret"))
        rt = r.get("ret")
        if rt is None:
            verdict = "未完成"
        elif rt > BENCH:
            verdict = "✅ 真稳"
        elif rt > 0:
            verdict = "・正但跑输指数"
        else:
            verdict = "⚠ 牛市运气"
        out.append(dict(pool=pool, tight=tight, r25=o.get("ret"), r23=rt, ann=a,
                        dd=r.get("dd"), n=r.get("trades"), win=r.get("win"),
                        run=r.get("run_id"), verdict=verdict))

    out.sort(key=lambda x: -(x["r23"] if x["r23"] is not None else -999))

    lines = [
        "# 正收益组合 · 拉长到 2023 年至今复跑",
        "",
        f"买卖策略统一是 **#101 跌破MA10离场**（高开买 + 收盘跌破MA10走，自带持 15 天 / 止损 8%）。",
        f"左列是 2025 全年那次的收益（挑出为正的 15 条），右列是同一组合在 **{SPAN}** 的表现。",
        f"同期 **沪深300 +{BENCH}%**（2023-01-03 3887.90 → 2026-09-28 4340.76）；"
        "分年 2023 −11.38% / 2024 +14.68% / 2025 +21.19% / 2026 至今 −6.25%。",
        "| 池 | 口径 | 2025 收益 | 2023起 收益 | 年化 | 回撤 | 笔数 | 胜率 | run | 判定 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    good = mid = bad = 0
    for x in out:
        v = x["verdict"]
        good += v.startswith("✅")
        mid += v.startswith("・")
        bad += v.startswith("⚠")
        lines.append(
            f"| {x['pool']} | {'收紧' if x['tight'] else '原池基线'} | {num(x['r25'])}% "
            f"| **{num(x['r23'])}%** | {num(x['ann'])}% | {num(x['dd'])}% "
            f"| {x['n']} | {num(x['win'])}% | {x['run']} | {v} |")

    lines += [
        "",
        f"**合计**：{good} 个跑赢指数 · {mid} 个正但跑输 · {bad} 个转负。",
        "",
        "## 怎么看",
        "",
        "1. 2025 是单边牛市（沪深300 +21.19%），那年为正**说明不了什么**；"
        "真正的问题是 2023（−11.38%）和 2026（−6.25%）这两段能不能扛住。",
        "2. 「年化」= 区间总收益按 3.74 年折年，方便跟其他资产比；"
        "**回撤**比收益更重要 —— 一年赚 30% 但回撤 50% 的策略拿不住。",
        "3. 每条记录 run_id 都能在 /backtest 点开看逐笔买卖、资金曲线和当时的选股条件快照。",
    ]
    dst = os.path.join(T, "_lab_bt23_result.md")
    with open(dst, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"已写 {dst}（{len(out)} 条：{good} 真稳 / {mid} 正但跑输 / {bad} 转负）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
