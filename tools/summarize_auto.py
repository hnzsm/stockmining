# -*- coding: utf-8 -*-
"""汇总 auto_strategies.py 的多份结果，按收益率排序，并把正收益的写入策略库。

用法:
    python tools/summarize_auto.py tools/_out_all_1.json tools/_out_all_2.json ...
    python tools/summarize_auto.py tools/_out_all_*.json tools/_out_oos.json --install
    python tools/summarize_auto.py tools/_out_all_*.json --html tools/_auto_report.html

多个结果文件按策略名合并：每个区间一列收益，便于一眼看出是不是只有某一段好。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import db, strategy as st  # noqa: E402


def label_of(path: str, d: dict) -> str:
    """结果集的标签（池子 + 过滤条件）。同一区间不同口径靠它区分。"""
    tag = d.get("tag")
    if tag:
        return tag
    b = os.path.basename(path).lower()
    pool = {"all": "全市场", "000300.sh": "沪深300"}.get(str(d.get("pool", "")).lower(),
                                                    str(d.get("pool", "?")))
    if "liq" in b:
        return pool + "+流动性过滤"
    return pool


def load(paths: list[str]) -> tuple[dict[str, dict], list[dict]]:
    """合并结果：{策略名: {区间: metrics, ...}}"""
    merged: dict[str, dict] = {}
    metas: list[dict] = []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        rng = d.get("range", "?")
        label = label_of(p, d)
        metas.append({"file": os.path.basename(p), "range": rng, "pool": label})
        for r in d.get("results", []):
            if r.get("error"):
                continue
            m = merged.setdefault(r["name"], {"note": r.get("note", ""),
                                              "config": r.get("config"),
                                              "spans": {}})
            if r.get("config") and not m.get("config"):
                m["config"] = r["config"]
            m["spans"][f"{label} {rng}"] = r["metrics"]
    return merged, metas


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", help="结果 JSON（支持通配符）")
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--min-trades", type=int, default=30)
    ap.add_argument("--main", default="", help="主判据区间名包含的关键字（默认取第一个）")
    ap.add_argument("--require-positive-all", action="store_true",
                    help="要求每个区间都为正才入库（默认按第一个区间判断）")
    ap.add_argument("--html", default="")
    args = ap.parse_args()

    paths: list[str] = []
    for f in args.files:
        paths.extend(sorted(glob.glob(f)) or [f])
    merged, metas = load(paths)

    print("=" * 110)
    print("有名短线策略 · 批量回测汇总")
    for m in metas:
        print(f"  {m['file']:<28} 池={m['pool']:<12} 区间={m['range']}")
    print("=" * 110)

    spans = []
    for m in merged.values():
        for k in m["spans"]:
            if k not in spans:
                spans.append(k)
    spans.sort()

    def _row(name, m):
        cells = []
        for s in spans:
            mm = m["spans"].get(s)
            cells.append(f"{mm['total_return']:>8.2f}" if mm else f"{'—':>8}")
        return cells

    head = f"{'策略':<26}" + "".join(f"{s[:22]:>24}" for s in spans)
    print(head)
    rows = sorted(merged.items(),
                  key=lambda kv: -(kv[1]["spans"].get(spans[0], {}).get("total_return", -999)))
    for name, m in rows:
        print(f"{name:<26}" + "".join(f"{c:>24}" for c in _row(name, m)))

    main_span = next((s for s in spans if args.main in s), spans[0]) if args.main else spans[0]
    print("\n" + "=" * 110)
    print(f"主判据区间：{main_span}")
    print("=" * 110)
    print(f"{'策略':<26}{'收益%':>9}{'年化%':>9}{'基准%':>9}{'超额%':>9}{'回撤%':>9}{'夏普':>7}"
          f"{'笔数':>7}{'胜率%':>8}{'盈亏比':>8}{'持股天':>8}  其它区间")
    picked = []
    for name, m in rows:
        mm = m["spans"].get(main_span)
        if not mm:
            continue
        others = " ".join(f"{s.split(' ')[-1][:12]}={m['spans'][s]['total_return']:.1f}%"
                          for s in spans[1:] if s in m["spans"])
        flag = "+" if mm.get("total_return", 0) > 0 else "-"
        print(f"{name:<26}{mm.get('total_return', 0):>9.2f}{mm.get('annual_return', 0):>9.2f}"
              f"{mm.get('benchmark_return') or 0:>9.2f}{mm.get('alpha') or 0:>9.2f}"
              f"{mm.get('max_drawdown', 0):>9.2f}{(mm.get('sharpe') or 0):>7.2f}"
              f"{mm.get('trade_count', 0):>7}{mm.get('win_rate', 0):>8.1f}"
              f"{(mm.get('profit_factor') or 0):>8.2f}{mm.get('avg_hold_days', 0):>8.1f}"
              f"  {others}")
        vals = [m["spans"][s]["total_return"] for s in spans if s in m["spans"]]
        ok = (all(v > 0 for v in vals) if args.require_positive_all else vals[0] > 0)
        if ok and mm.get("trade_count", 0) >= args.min_trades:
            picked.append((name, m, mm))

    print(f"\n可入库（主区间收益>0 且笔数≥{args.min_trades}）：{len(picked)} 条")
    for name, _m, _mm in picked:
        print(f"  + {name}")

    if args.html:
        _write_html(args.html, rows, spans, main_span, metas,
                    [n for n, _m, _mm in picked],
                    [n for n, m in rows if m["spans"].get(main_span, {}).get("total_return", 0) <= 0])
        print(f"\nHTML 报告：{args.html}")

    if args.install:
        print()
        for name, m, mm in picked:
            note = (m["note"] + "\n\n【实测】"
                    + "；".join(f"{s}：收益 {m['spans'][s]['total_return']}%，"
                               f"年化 {m['spans'][s].get('annual_return')}%，"
                               f"最大回撤 {m['spans'][s].get('max_drawdown')}%，"
                               f"{m['spans'][s].get('trade_count')} 笔，"
                               f"胜率 {m['spans'][s].get('win_rate')}%"
                               for s in spans if s in m["spans"]))
            exist = db.query_one("SELECT id FROM strategy WHERE name=%s", (name,))
            if exist:
                # 同名只改内容、**不动 builtin**（撞上内置样例「开盘确认」也不能抹它的标记）
                db.execute("UPDATE strategy SET note=%s, config=%s WHERE id=%s",
                           (note, st.config_json(m["config"]), exist["id"]))
                print(f"  更新 {name}（id={exist['id']}）")
            else:
                # 落成**普通**策略（builtin=0 ⇒ 界面上可改名、可删除），2026-09-29
                sid = db.insert(
                    "INSERT INTO strategy (name, note, timeframe, config, builtin) "
                    "VALUES (%s,%s,%s,%s,0)",
                    (name, note, "daily", st.config_json(m["config"])))
                print(f"  新增 {name}（id={sid}）")


def _write_html(path: str, rows, spans, main_span, metas, picked=(), discarded=()):
    esc = lambda s: (str(s).replace("&", "&amp;").replace("<", "&lt;")
                     .replace(">", "&gt;"))
    trs = []
    for name, m in rows:
        mm = m["spans"].get(main_span, {})
        ret = mm.get("total_return", 0)
        cls = "up" if ret > 0 else "down"
        tds = "".join(
            f"<td class='{'up' if s in m['spans'] and m['spans'][s]['total_return'] > 0 else 'down'}'>"
            f"{m['spans'][s]['total_return']:.2f}%</td>" if s in m["spans"] else "<td>—</td>"
            for s in spans)
        trs.append(f"""<tr>
<td class='nm'>{esc(name)}</td>{tds}
<td>{mm.get('annual_return', 0):.2f}%</td><td>{mm.get('benchmark_return') or 0:.2f}%</td>
<td>{mm.get('max_drawdown', 0):.2f}%</td><td>{mm.get('sharpe') or 0:.2f}</td>
<td>{mm.get('trade_count', 0)}</td><td>{mm.get('win_rate', 0):.1f}%</td>
<td>{(mm.get('profit_factor') or 0):.2f}</td><td>{mm.get('avg_hold_days', 0):.1f}</td>
<td class='note'>{esc(m['note'])[:120]}</td></tr>""")
    meta_html = "<br>".join(esc(f"{m['file']} · 池={m['pool']} · {m['range']}") for m in metas)
    keep = "".join(f"<li class='up'>{esc(n)}</li>" for n in picked)
    drop = "、".join(esc(n) for n in discarded)
    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>有名短线策略批量回测</title><style>
body{{background:#0f1319;color:#c9d3e0;font:13px/1.6 -apple-system,"Microsoft YaHei",sans-serif;
margin:0;padding:24px}}
h1{{font-size:18px;margin:0 0 6px}} .sub{{color:#7d8ba1;font-size:12px;margin-bottom:16px}}
table{{border-collapse:collapse;width:100%;font-size:12px}}
th,td{{border:1px solid #232b38;padding:6px 8px;text-align:right;white-space:nowrap}}
th{{background:#161d27;color:#9fb0c6;font-weight:600}}
td.nm{{text-align:left;color:#e6edf6}} td.note{{text-align:left;color:#7d8ba1;font-size:11px;
white-space:normal;max-width:320px}}
.up{{color:#e5352b}} .down{{color:#12a06a}}
.box{{background:#141a23;border:1px solid #232b38;border-radius:8px;padding:12px 16px;
margin:14px 0;font-size:12px}}
.box ul{{margin:6px 0 10px;padding-left:20px}} .box li{{font-size:13px;font-weight:600}}
.drop{{color:#6b7889;line-height:1.8;margin-bottom:6px}}
.why{{color:#8b97a8;border-top:1px dashed #2a3342;padding-top:8px}}
tr:hover td{{background:#151c26}}
</style></head><body>
<h1>有名短线策略 · 批量回测</h1>
<div class="sub">{meta_html}<br>口径：T日收盘选股 → 次日高开买入（滑点千1）→ 收盘判定卖出 → 止损/止盈/到期；
100万本金、最多10只、每只10%仓位、佣金万2.5+印花税千1<br>
主判据：<b>{esc(main_span)}</b>（最宽的口径，避免只在小池子上好看）</div>
<div class="box"><b>保留（主口径收益为正）</b><ul>{keep}</ul>
<b>淘汰（{len(discarded)} 个）</b><div class="drop">{drop}</div>
<div class="why">被淘汰的多是「每天能选出几百只、持股 1~3 天」的高频形态打法：
全市场下几乎每天满仓换手，滑点+佣金+印花税累积吃掉几个百分点/月，
而盈亏比普遍 &lt; 1，说明策略本身也是负期望 —— 不只是成本问题。</div></div>
<table><tr><th>策略</th>{"".join(f"<th>{s[:26]}</th>" for s in spans)}
<th>年化</th><th>基准</th><th>回撤</th><th>夏普</th><th>笔数</th><th>胜率</th><th>盈亏比</th><th>持股天</th><th>说明</th></tr>
{"".join(trs)}
</table></body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


if __name__ == "__main__":
    main()
