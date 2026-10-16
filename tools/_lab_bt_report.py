# -*- coding: utf-8 -*-
"""把「基线 / 收紧」两批平台回测合并成一张对比表（tools/_lab_bt_result.md）。

    python tools/_lab_bt_report.py

为什么单独一份：收紧实验那张 `_lab_tight_result.md` 里的收益是**旧引擎**（卖出侧未来
函数）跑的，虚高一大截；这一份的两侧数字都是**修好的引擎**在平台上真跑出来的
（落在 `backtest_run` 里，能点开看逐笔），所以「收紧到底是好是坏」只能看这一张。

判定只看**同池同期**的两条记录之差（收紧 − 基线）：
  Δ > +2pt  → ✅ 收紧有效（选得少、还更赚）
  |Δ| ≤ 2pt → ・差别不大（那就要不要收紧纯看你想不想少选点）
  Δ < −2pt  → ⚠ 收紧反而更差
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_J = os.path.join(ROOT, "tools", "_lab_bt_base.json")
TIGHT_J = os.path.join(ROOT, "tools", "_lab_bt_tight.json")
OUT = os.path.join(ROOT, "tools", "_lab_bt_result.md")


def load(path: str) -> dict[int, dict]:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        rows = json.load(fh)
    return {int(r["pool_id"]): r for r in rows}


def num(v, nd=1):
    return "—" if v is None else f"{float(v):.{nd}f}"


def main() -> int:
    base = load(BASE_J)
    tight = load(TIGHT_J)
    if not base and not tight:
        print("没有结果文件，先跑 _lab_bt_platform.py")
        return 1

    lines = [
        "# 收紧方案 · 平台回测复验（修好的引擎）",
        "",
        "两侧都是 `POST /api/backtest/start` 真跑出来的记录（历史回测记录页可点开看逐笔）：",
        "**基线** = 原池原条件；**收紧** = 原池 + 收紧条件（副本池）。买卖策略都是 #101 跌破MA10离场",
        "（高开买 + 收盘跌破MA10走，策略自带持 15 天 / 止损 8%），区间 2025-01-01~2025-12-31。",
        "",
        "⚠ 表里「旧引擎」那两列是 `_lab_tight_result.md` 的数字（卖出侧未来函数，虚高），",
        "**只用来说明差距有多大，别拿它当结论**。",
        "",
        "| 池 | 变体 | 日均（降幅） | 旧引擎持10/持20 | 基线收益 | 收紧收益 | Δ | 回撤 基线→收紧 | 笔数 基线→收紧 | 判定 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    win = lose = flat = 0
    for pid in sorted(set(base) | set(tight)):
        b = base.get(pid)
        t = tight.get(pid)
        name = (b or t)["pool"]
        var = (t or {}).get("var") or "—"
        br = (b or {}).get("ret")
        tr = (t or {}).get("ret")
        if br is None or tr is None:
            delta = None
            verdict = "缺一侧"
        else:
            delta = float(tr) - float(br)
            if delta > 2:
                verdict, win = "✅ 收紧有效", win + 1
            elif delta < -2:
                verdict, lose = "⚠ 更差", lose + 1
            else:
                verdict, flat = "・差不多", flat + 1
        old_r = (t or {}).get("old_ret") or (b or {}).get("old_base_ret") or {}
        old_txt = f"{num(old_r.get('10'))} / {num(old_r.get('20'))}"
        avg_b = (b or {}).get("base_avg")
        avg_t = (t or {}).get("avg")
        cut = (t or {}).get("cut")
        avg_txt = "—"
        if avg_b is not None and avg_t is not None:
            avg_txt = f"{avg_b:.0f}→{avg_t:.0f}（-{cut:.0f}%）" if cut else \
                      f"{avg_b:.0f}→{avg_t:.0f}"
        dd = f"{num((b or {}).get('dd'))}→{num((t or {}).get('dd'))}"
        tc = f"{(b or {}).get('trades', '—')}→{(t or {}).get('trades', '—')}"
        lines.append(
            f"| {name}（#{pid}） | {var} | {avg_txt} | {old_txt} | {num(br)}% | {num(tr)}% "
            f"| {num(delta)}pt | {dd}% | {tc} | {verdict} |")

    lines += [
        "",
        f"**合计**：收紧有效 {win} 个 · 差不多 {flat} 个 · 更差 {lose} 个。",
        "",
        "## 怎么看",
        "",
        "1. 「Δ」是**同一池**收紧后减去基线的收益差 —— 这才是收紧的净效果；",
        "   跟「旧引擎」那列比大小没有意义（那列是 bug 时期的数字）。",
        "2. 选股变少本身是目的之一（日均那列），所以「差不多」也值得收紧 —— 前提是",
        "   收益没掉、笔数没少到没法分散。",
        "3. 所有记录都在 /backtest 里，`run_id` 在 `_lab_bt_base.json` / `_lab_bt_tight.json`",
        "   里，点开能看逐笔买卖、资金曲线和当时那一份选股条件快照。",
    ]
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"已写 {OUT}（{len(set(base) | set(tight))} 个池：{win} 有效 / {flat} 差不多 / {lose} 更差）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
