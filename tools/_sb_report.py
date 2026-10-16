# -*- coding: utf-8 -*-
"""首板研究 · 第 10 步：最终画像验证 + 生成 HTML 报告。

产物 tools/_sb_report.html（自包含，无外部依赖）。
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

OUT_HTML = os.path.join(HERE, "_sb_report.html")
OUT_JSON = os.path.join(HERE, "_sb_final.json")
COST = 0.18
YEARS = [2023, 2024, 2025, 2026]


def prep():
    sb = load()
    base = sb[sb["base"]].copy()
    cnt = base.groupby(["trade_date", "industry"]).size().rename("ind_sb_cnt")
    base = base.merge(cnt, on=["trade_date", "industry"], how="left")
    base["low_vol"] = base["f_vol_vs_prev"] < 1.1
    base["low_turn"] = base["turnover_rate"] < 5
    base["hi_gap"] = base["f_gap"] > 0.03
    base["resonance"] = base["ind_sb_cnt"] >= 7
    base["can_t0"] = base["f_open_is_limit"] == 0
    base["cyb_up"] = base["idx_cyb_above_ma20"] == 1
    return base


def row(d: pd.DataFrame, label: str, y: str = "y_t1_close") -> dict:
    s = d[y].dropna()
    n = len(s)
    if n == 0:
        return {"label": label, "n": 0}
    tr = d[d["train"]]
    va = d[~d["train"]]
    yrs = {}
    for yy in YEARS:
        dd = d[d["year"] == yy]
        yrs[yy] = None if len(dd) < 30 else round(float(dd[y].mean()) * 100 - COST, 2)
    can = d[d["can_t0"]]
    return {
        "label": label, "n": n,
        "avg": round(float(s.mean()) * 100 - COST, 2),
        "win": round(float((s > 0).mean()) * 100, 1),
        "lianban": round(float(d["y_lianban"].mean()) * 100, 1),
        "train": (None if len(tr) < 30 else round(float(tr[y].mean()) * 100 - COST, 2)),
        "valid": (None if len(va) < 30 else round(float(va[y].mean()) * 100 - COST, 2)),
        "train_n": len(tr), "valid_n": len(va),
        "years": yrs,
        "buy_pct": round(float(d["f_open_is_limit"].mean() * 0 + (d["can_t0"].mean()) * 100), 1),
        "can_avg": (None if len(can) < 100 else round(float(can[y].mean()) * 100 - COST, 2)),
        "can_n": len(can),
    }


def bar(v, lo, hi, color) -> str:
    if v is None:
        return '<span class="mut">—</span>'
    w = max(2.0, min(100.0, (v - lo) / (hi - lo) * 100))
    return (f'<span class="barwrap"><span class="bar" style="width:{w:.1f}%;'
            f'background:{color}"></span></span>')


def sig(v, good=0.0):
    if v is None:
        return '<td class="mut">—</td>'
    cls = "up" if v > good else ("dn" if v < 0 else "mut")
    return f'<td class="{cls}">{v:+.2f}%</td>'


def main() -> int:
    base = prep()
    out = {}

    # ---- 0 整体 ----
    allbase = {
        "n": len(base),
        "t0_to_t1open": round(float(base["y_gap1"].mean()) * 100 - COST, 2),
        "t0_to_t1close": round(float(base["y_t1_close"].mean()) * 100 - COST, 2),
        "t1open_buy_r1": round(float(base.loc[base["y_t1_open_is_limit"] == 0, "y_r1"].mean()) * 100 - COST, 2),
        "lianban": round(float(base["y_lianban"].mean()) * 100, 1),
        "win_t1close": round(float((base["y_t1_close"] > 0).mean()) * 100, 1),
        "yizi_pct": round(float((base["f_is_yizi"] == 1).mean()) * 100, 1),
        "openlimit_pct": round(float(((base["f_open_is_limit"] == 1) & (base["f_is_yizi"] == 0)).mean()) * 100, 1),
    }
    out["overall"] = allbase
    print("【整体】", json.dumps(allbase, ensure_ascii=False))

    # ---- 1 特征 ----
    SETS = [
        ("极缩量（量/前日 < 0.8）", base["f_vol_vs_prev"] < 0.8),
        ("缩量（量/前日 < 1.1）", base["low_vol"]),
        ("超低换手（< 3%）", base["turnover_rate"] < 3),
        ("低换手（< 5%）", base["low_turn"]),
        ("高开涨停（开盘 > +3%）", base["hi_gap"]),
        ("板块共振（同行业首板 ≥ 7 只）", base["resonance"]),
        ("创业板在 MA20 上方", base["cyb_up"]),
        ("深跌位（距 120 日高 < −25%）", base["f_gap_to_hh120"] < -0.25),
        ("缩量 + 低换手", base["low_vol"] & base["low_turn"]),
        ("高开 + 低换手", base["hi_gap"] & base["low_turn"]),
        ("缩量 + 板块共振", base["low_vol"] & base["resonance"]),
        ("缩量 + 低换手 + 创业板MA20上", base["low_vol"] & base["low_turn"] & base["cyb_up"]),
        ("缩量 + 低换手 + 板块共振", base["low_vol"] & base["low_turn"] & base["resonance"]),
        ("四合一（缩量+低换手+高开+共振）", base["low_vol"] & base["low_turn"]
         & base["hi_gap"] & base["resonance"]),
        ("全部首板（基准）", pd.Series(True, index=base.index)),
    ]
    feats = [row(base[m], lab) for lab, m in SETS]
    out["features"] = feats
    print("\n【特征】")
    for r in feats:
        print(f"  {r['label']:30s} n={r['n']:>6,} {r['avg']:>+6.2f}% win {r['win']:>5.1f}% "
              f"lb {r['lianban']:>5.1f}% T {r['train']} V {r['valid']} 可买{r['can_avg']}")

    # ---- 2 环境 ----
    envs = [("大盘上涨日", base["idx_sh"] > 0), ("大盘下跌日", base["idx_sh"] <= 0),
            ("上证在 MA20 上方", base["idx_sh_above_ma20"] == 1),
            ("上证在 MA20 下方", base["idx_sh_above_ma20"] == 0),
            ("创业板在 MA20 上方", base["idx_cyb_above_ma20"] == 1),
            ("创业板在 MA20 下方", base["idx_cyb_above_ma20"] == 0),
            ("全市场中位涨幅 > 1%", base["mkt_med_pct"] > 0.01),
            ("全市场中位涨幅 < −1%", base["mkt_med_pct"] < -0.01),
            ("昨涨停股今日上涨（热）", base["emo_zt_ret_1"] > 0),
            ("昨涨停股今日下跌（冷）", base["emo_zt_ret_1"] <= 0),
            ("涨停家数 > 119（最热 20%）", base["zt_cnt"] >= base["zt_cnt"].quantile(0.8)),
            ("涨停家数 < 49（最冷 20%）", base["zt_cnt"] <= base["zt_cnt"].quantile(0.2))]
    out["env"] = [row(base[m], lab) for lab, m in envs]

    # 环境 × 最优条件
    best = base["low_vol"] & base["low_turn"] & base["resonance"]
    out["env_cross"] = []
    for lab, em in envs:
        a = base[em]
        b = base[em & best]
        out["env_cross"].append({
            "label": lab,
            "all": (None if len(a) < 100 else round(float(a["y_t1_close"].mean()) * 100 - COST, 2)),
            "all_n": len(a),
            "best": (None if len(b) < 50 else round(float(b["y_t1_close"].mean()) * 100 - COST, 2)),
            "best_n": len(b),
            "best_win": (None if len(b) < 50 else round(float((b["y_t1_close"] > 0).mean()) * 100, 1)),
        })

    # ---- 3 季节 ----
    mon = [row(base[base["trade_date"].dt.month == i], f"{i} 月")
           for i in range(1, 13)]
    wd = [row(base[base["trade_date"].dt.dayofweek == i], n)
          for i, n in enumerate(("周一", "周二", "周三", "周四", "周五"))]
    dom = base["trade_date"].dt.day
    seg = [row(base[dom <= 10], "月初（1-10日）"),
           row(base[(dom > 10) & (dom <= 20)], "月中（11-20日）"),
           row(base[dom > 20], "月末（21日后）"),
           row(base[base["trade_date"].dt.month.isin([4, 8, 10])], "财报月（4/8/10月）"),
           row(base[~base["trade_date"].dt.month.isin([4, 8, 10])], "非财报月")]
    out["month"] = mon
    out["weekday"] = wd
    out["dom"] = seg

    # ---- 4 行业 ----
    ind = base.groupby("industry").agg(n=("y_t1_close", "size"),
                                       t1=("y_t1_close", "mean"),
                                       lb=("y_lianban", "mean"))
    ind = ind[ind["n"] >= 300].sort_values("t1", ascending=False)
    out["industry"] = [{"industry": i, "n": int(r.n),
                        "t1": round(float(r.t1) * 100 - COST, 2),
                        "lianban": round(float(r.lb) * 100, 1)}
                       for i, r in ind.iterrows()]

    with io.open(OUT_JSON, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)

    # ================= HTML =================
    def tbl(rows, cols, fmt):
        h = "".join(f"<th>{c[1]}</th>" for c in cols)
        body = []
        for r in rows:
            tds = []
            for key, _lab, kind in cols:
                tds.append(fmt(r, key, kind))
            body.append("<tr>" + "".join(tds) + "</tr>")
        return f"<table><thead><tr>{h}</tr></thead><tbody>{''.join(body)}</tbody></table>"

    def feat_row(r, key, kind):
        v = r.get(key)
        if kind == "label":
            return f'<td class="lb">{r["label"]}</td>'
        if kind == "n":
            return f'<td class="num">{v:,}</td>'
        if kind == "pct":
            if v is None:
                return '<td class="mut">—</td>'
            cls = "up" if v > 0.5 else ("dn" if v < 0 else "")
            return f'<td class="num {cls}">{v:+.2f}%</td>'
        if kind == "pl":
            return f'<td class="num">{v:.1f}%</td>'
        if kind == "yrs":
            cells = []
            for yy in YEARS:
                x = (r["years"] or {}).get(yy)
                cells.append('<span class="mut">—</span>' if x is None
                             else f'<span class="{"up" if x > 0.5 else ("dn" if x < 0 else "")}">{x:+.1f}</span>')
            return '<td class="yrs">' + " · ".join(cells) + "</td>"
        return "<td></td>"

    FCOLS = [("label", "条件", "label"), ("n", "样本", "n"),
             ("avg", "扣费后收益", "pct"), ("win", "胜率", "pl"),
             ("lianban", "连板率", "pl"), ("can_avg", "只算买得到", "pct"),
             ("train", "训练段", "pct"), ("valid", "验证段", "pct"),
             ("years", "分年（23·24·25·26）", "yrs")]
    frows = feats
    fhtml = tbl(frows, FCOLS, feat_row)

    def minrow(r, key, kind):
        if kind == "label":
            return f'<td class="lb">{r["label"]}</td>'
        if kind == "n":
            return f'<td class="num">{r[key]:,}</td>'
        if kind == "pct":
            v = r.get(key)
            if v is None:
                return '<td class="mut">—</td>'
            cls = "up" if v > 0.5 else ("dn" if v < 0 else "")
            return f'<td class="num {cls}">{v:+.2f}%</td>'
        if kind == "pl":
            v2 = r.get(key)
            if v2 is None:
                return '<td class="mut">—</td>'
            return f'<td class="num">{v2:.1f}%</td>'
        if kind == "bar":
            v = r.get(key)
            return "<td>" + bar(v, 0.5, 3.2, "#e5352b") + "</td>"
        return "<td></td>"

    MCOLS = [("label", "环境", "label"), ("n", "样本", "n"),
             ("avg", "收益", "pct"), ("win", "胜率", "pl"), ("lianban", "连板率", "pl"),
             ("avg", "", "bar")]
    mhtml = tbl(out["env"], MCOLS, minrow)

    CCOLS = [("label", "环境", "label"), ("all_n", "全部首板 n", "n"),
             ("all", "全部首板", "pct"),
             ("best_n", "精筛后 n", "n"), ("best", "精筛后收益", "pct"),
             ("best_win", "精筛后胜率", "pl")]
    chtml = tbl(out["env_cross"], CCOLS, minrow)

    def monthrow(r, key, kind):
        if kind == "label":
            return f'<td class="lb">{r["label"]}</td>'
        if kind == "n":
            return f'<td class="num">{r[key]:,}</td>'
        if kind == "pct":
            v = r.get(key)
            if v is None:
                return '<td class="mut">—</td>'
            cls = "up" if v > 0.5 else ("dn" if v < 0 else "")
            return f'<td class="num {cls}">{v:+.2f}%</td>'
        if kind == "pl":
            v2 = r.get(key)
            if v2 is None:
                return '<td class="mut">—</td>'
            return f'<td class="num">{v2:.1f}%</td>'
        return "<td></td>"

    MONCOLS = [("label", "月份", "label"), ("n", "样本", "n"), ("avg", "收益", "pct"),
               ("win", "胜率", "pl"), ("lianban", "连板率", "pl")]
    monhtml = tbl(out["month"], MONCOLS, monthrow)
    wdhtml = tbl(out["weekday"], MONCOLS, monthrow)
    domhtml = tbl(out["dom"], MONCOLS, monthrow)
    # 年度
    yr_rows = []
    for yy in YEARS:
        d = base[base["year"] == yy]
        if len(d) < 100:
            continue
        bym = [round(float(d[d["trade_date"].dt.month == i]["y_t1_close"].mean()) * 100 - COST, 2)
               for i in range(1, 13)]
        yr_rows.append({"label": f"{yy} 年", "n": len(d),
                        "avg": round(float(d["y_t1_close"].mean()) * 100 - COST, 2),
                        "win": round(float((d["y_t1_close"] > 0).mean()) * 100, 1),
                        "lianban": round(float(d["y_lianban"].mean()) * 100, 1),
                        "best": round(float(d["y_gap1"].mean()) * 100 - COST, 2)})
    yhtml = tbl(yr_rows, [("label", "年份", "label"), ("n", "样本", "n"),
                          ("avg", "T+1收盘卖", "pct"), ("best", "T+1开盘卖", "pct"),
                          ("win", "胜率", "pl"), ("lianban", "连板率", "pl")], monthrow)

    ind_rows = [{"label": x["industry"], "n": x["n"], "avg": x["t1"], "win": None,
                 "lianban": x["lianban"]} for x in out["industry"][:10]]
    ind_rows += [{"label": "……", "n": 0, "avg": None, "win": None, "lianban": None}]
    ind_rows += [{"label": x["industry"], "n": x["n"], "avg": x["t1"], "win": None,
                  "lianban": x["lianban"]} for x in out["industry"][-5:]]
    ihtml = tbl(ind_rows, [("label", "行业", "label"), ("n", "样本", "n"),
                           ("avg", "收益", "pct"), ("lianban", "连板率", "pl")], monthrow)

    buy_rows = []
    for lab, m in (("T0 当天买得到（开盘未涨停）", base["can_t0"]),
                   ("T0 买不到（一字板 / 开盘即涨停）", ~base["can_t0"]),
                   ("T+1 可开盘买入", base["y_t1_open_is_limit"] == 0),
                   ("T+1 买不到（一字板 / 开盘即涨停）", base["y_t1_open_is_limit"] == 1)):
        d = base[m]
        buy_rows.append({
            "label": lab, "n": len(d),
            "avg": round(float(d["y_t1_close"].mean()) * 100 - COST, 2),
            "win": round(float((d["y_t1_close"] > 0).mean()) * 100, 1),
            "lianban": round(float(d["y_lianban"].mean()) * 100, 1)})
    bhtml = tbl(buy_rows, [("label", "分类", "label"), ("n", "数量", "n"),
                           ("avg", "次日收益", "pct"), ("win", "胜率", "pl"),
                           ("lianban", "连板率", "pl")], monthrow)

    o = allbase
    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>首板研究 · 什么样的首板值得关注</title>
<style>
:root{{--bg:#0d1117;--card:#161b22;--bd:#30363d;--tx:#e6edf3;--mu:#8b949e;
--up:#e5352b;--dn:#12a06a;--ac:#58a6ff;--hl:#d29922}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--tx);
font:14px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;padding:32px 28px 80px}}
.wrap{{max-width:1180px;margin:0 auto}}
h1{{font-size:26px;margin:0 0 6px;letter-spacing:.5px}}
h2{{font-size:19px;margin:42px 0 6px;padding-left:11px;border-left:4px solid var(--ac)}}
h3{{font-size:15px;margin:26px 0 8px;color:var(--mu);font-weight:600}}
.sub{{color:var(--mu);font-size:13px;margin-bottom:26px}}
.lead{{background:linear-gradient(135deg,#1a2332,#161b22);border:1px solid var(--bd);
border-left:4px solid var(--hl);border-radius:8px;padding:18px 20px;margin:22px 0}}
.lead b{{color:var(--hl)}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:12px;margin:20px 0 6px}}
.kpi{{background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:14px 16px}}
.kpi .k{{color:var(--mu);font-size:12px;margin-bottom:5px}}
.kpi .v{{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}}
.kpi .d{{color:var(--mu);font-size:11.5px;margin-top:3px}}
table{{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--bd);
border-radius:8px;overflow:hidden;margin:12px 0;font-size:13px}}
th{{background:#1c2431;color:var(--mu);font-weight:600;text-align:right;padding:9px 11px;
font-size:12px;white-space:nowrap}}
th:first-child{{text-align:left}}
td{{padding:8px 11px;border-top:1px solid #21262d;text-align:right;
font-variant-numeric:tabular-nums;white-space:nowrap}}
td.lb{{text-align:left;color:var(--tx)}}
td.num{{}}
td.yrs{{font-size:12px;color:var(--mu)}}
.up{{color:var(--up);font-weight:600}}
.dn{{color:var(--dn);font-weight:600}}
.mut{{color:#6e7681}}
.barwrap{{display:inline-block;width:90px;height:9px;background:#21262d;border-radius:5px;
vertical-align:middle;overflow:hidden}}
.bar{{display:block;height:100%;border-radius:5px}}
.note{{background:#161b22;border:1px dashed var(--bd);border-radius:8px;padding:14px 18px;
margin:14px 0;color:#c9d1d9;font-size:13.2px}}
.note b{{color:var(--hl)}}
.warn{{border-left:4px solid var(--up);background:#241a1a}}
.ok{{border-left:4px solid var(--dn);background:#13221c}}
ul{{margin:8px 0 8px 4px;padding-left:20px}}li{{margin:5px 0}}
code{{background:#21262d;padding:1px 6px;border-radius:4px;font-size:12.5px;color:#79c0ff}}
.foot{{color:var(--mu);font-size:12px;margin-top:44px;border-top:1px solid var(--bd);padding-top:16px}}
</style></head><body><div class="wrap">

<h1>首板研究 · 什么样的首板值得关注</h1>
<div class="sub">样本 {o['n']:,} 笔首板（已剔 ST / 北交所 / 上市不足 60 天）·
2023-01-03 ~ 2026-09-28 · 5293 只个股 · 906 个交易日 ·
口径：T 日收盘确认首板 → T+1 成交；收益均已扣 {COST:.2f}% 往返成本（买入佣金 + 卖出滑点/印花税）</div>

<div class="lead">
<b>一句话结论：</b>首板确实有稳定的钱，但钱<b>只在隔夜</b>——首板当天买入、次日开盘卖出平均
<b>{o['t0_to_t1open']:+.2f}%</b>，次日收盘卖 <b>{o['t0_to_t1close']:+.2f}%</b>；
而如果改成<b>次日开盘才追进去</b>，同样持有一天就变成 <b>{o['t1open_buy_r1']:+.2f}%（亏）</b>。
溢价在开盘那一秒就被吃光了。<br>
<b>值得关注的首板有清晰共性：缩量、低换手、板块共振</b>——三个都满足时（n=784）
次日收盘卖 <b>+4.01%</b>、胜率 <b>76.3%</b>，2024/2025/2026 三年逐年 +4.90/+3.19/+2.72 <b>全部为正</b>，
且这类板 <b>87.9% 当天买得到</b>（不是一字板）。
</div>

<div class="kpis">
<div class="kpi"><div class="k">首板当天买 → 次日开盘卖</div>
  <div class="v up">{o['t0_to_t1open']:+.2f}%</div><div class="d">扣费后 · 基准全部首板</div></div>
<div class="kpi"><div class="k">首板当天买 → 次日收盘卖</div>
  <div class="v up">{o['t0_to_t1close']:+.2f}%</div><div class="d">扣费后 · 胜率 {o['win_t1close']:.1f}%</div></div>
<div class="kpi"><div class="k">次日开盘追进去（持 1 日）</div>
  <div class="v dn">{o['t1open_buy_r1']:+.2f}%</div><div class="d">扣费后 · 亏钱</div></div>
<div class="kpi"><div class="k">次日连板率</div>
  <div class="v">{o['lianban']:.1f}%</div><div class="d">首板次日再涨停的比例</div></div>
<div class="kpi"><div class="k">当天买不到的比例</div>
  <div class="v">{o['yizi_pct'] + o['openlimit_pct']:.1f}%</div>
  <div class="d">一字板 {o['yizi_pct']:.1f}% + 开盘板 {o['openlimit_pct']:.1f}%</div></div>
</div>

<h2>一、钱到底在哪一步</h2>
<p>把同一批首板按不同买卖时点切开，差距一目了然。<b>所有收益都是「首板当天在涨停价成交」或
「次日开盘价成交」，已扣 0.18% 成本。</b></p>
<table><thead><tr><th>年份</th><th>样本</th><th>T+1 收盘卖</th><th>T+1 开盘卖</th>
<th>胜率</th><th>连板率</th></tr></thead><tbody>{yhtml}</tbody></table>
<div class="note warn"><b>⚠ 这是全部结论里最重要的一条：</b>
首板次日开盘平均高开约 2.1%，但你若在开盘价买进，这 2.1% 就是别人的利润。
<b>首板不是买点，是卖点</b>——除非你能在前一天收盘（或盘中）就拿到筹码。</div>

<h2>二、什么样的首板值得关注 —— 六条共性</h2>
<p>下表每条都是「首板当天收盘在涨停价买入 → 次日收盘卖出」，扣 0.18% 成本。
<b>训练段 = 2023-01~2024-12，验证段 = 2025-01 起</b>，两段都为正才算数。</p>
{fhtml}
<div class="note ok"><b>✓ 读法：</b>看三样东西 ——
①「扣费后收益」比基准高多少；②「训练段 / 验证段」是否<b>同号</b>（防过拟合）；
③「只算买得到」这一列（剔除一字板后还剩多少）。<br>
<b>最稳的四条（两段都正、且逐年不靠单一年份）：</b>
<ul>
<li><b>缩量</b>：首板当天量能 &lt; 前一日 1.1 倍（甚至 &lt;0.8 更好）——次日 <b>+2.36%</b>（极缩量 +3.11%）</li>
<li><b>低换手</b>：当天换手率 &lt; 5%（&lt; 3% 更好）——次日 <b>+2.16%</b>（超低换手 +2.55%）</li>
<li><b>高开涨停</b>：开盘就涨 3% 以上再封板——次日 <b>+2.55%</b>。⚠ 这类里有 33% 是一字板，只算买得到的仍有 <b>+2.57%</b></li>
<li><b>板块共振</b>：当天同行业首板 ≥ 7 只——次日 <b>+2.84%</b>，胜率 66%</li>
</ul>
<b>三个合起来（缩量 + 低换手 + 板块共振）</b>：n=784，次日 <b>+4.01%</b>、胜率 <b>76.3%</b>、
连板率 24.9%，训练段 +4.85 / 验证段 +2.84，且 <b>87.9% 当天买得到</b>——这是全表里"可执行 + 稳"最好的一条。
</div>
<div class="note"><b>反向结论（同样重要）：</b>
<b>放量首板是差板</b>。当天量能 &gt; 3 倍前日时，次日只有 +1.37%（验证段），连板率也只有 13.9%，
明显低于缩量组。<b>换手率越高越差</b>（&gt;12.6% 那档持有 1 天只有 +0.12%）。
你现有的「首板」池要求 <code>换手率 ≥ 3%</code> —— 这条门槛方向是反的，
它把表现最好的低换手那批（换手 &lt;3%：次日 +2.75%、胜率 65.0%）先筛掉了。</div>

<h2>三、买得到吗 —— 别拿买不到的收益当本事</h2>
<p>首板有 {o['yizi_pct']:.1f}% 是一字板、另有 {o['openlimit_pct']:.1f}% 开盘即涨停，这两类当天<b>买不进去</b>。
它们恰恰是涨得最好的（次日 +3.36%、连板率 34.8%）——所以任何回测都必须把它们扣掉再看。</p>
{bhtml}
<div class="note warn"><b>⚠ 次日追高更没得玩：</b>
次日一字板 / 开盘涨停的占 7.8%，这批「买不到的」次日收益 +8.93%、连板率 71.2%；
而<b>买得到的那 92.2%，次日收益只有 +1.24%</b>。你永远买在别人愿意卖给你的价位上。</div>

<h2>四、阶段性特征（一）：大盘环境</h2>
<p>同一批首板，按当天的大盘状态切片。打板口径，扣费后。</p>
{mhtml}
<div class="note ok"><b>✓ 结论：</b>
① <b>创业板（成长股情绪）比上证更有指示性</b>——创业板在 MA20 上方时首板次日 +1.90%，
下方只有 +1.24%；② 大盘当日涨跌、以及「涨停家数」「赚钱效应」这些情绪指标，
对收益的影响<b>远小于个股自身形态</b>；③ 最强的一条：市场普涨（中位涨幅 &gt; 1%）时首板
+1.94%、胜率 66.7%，普跌时只有 +1.23%。</div>

<h3>环境 × 最优条件（缩量+低换手+板块共振）</h3>
<p>好消息是：<b>这套条件不挑环境</b>，牛熊里都能用，只是强弱有别。</p>
{chtml}

<h2>五、阶段性特征（二）：季节与日历</h2>
<h3>按月份</h3>
{monhtml}
<div class="note ok"><b>✓ 旺季：9 月（+3.05% / 胜率 63.0% / 连板 22.8%）、2 月（+2.26%）、11 月（+2.14%）。</b>
最弱：10 月（+0.91%，节后首月）、1 月（+1.26%）。
最强与最弱相差 2 个百分点，<b>方向稳定但不该单独当信号用</b>——月份只是微调，不是开关。</div>

<h3>按星期</h3>
{wdhtml}
<div class="note"><b>周一最强（+2.13%）、周二最弱（+1.12%）</b>；周一的隔夜溢价高达 +2.57%。
而「周一收盘买 → 周二卖」之所以最强，一部分是周末消息与资金回补的效应。
反过来，周四、周五买入的持有 1 日收益是正的（+0.09%~+0.37%），说明周末前有资金抢筹。</div>

<h3>按月中位置 / 财报窗口</h3>
{domhtml}
<div class="note"><b>月末（21 日后）明显好于月初</b>（+2.03% vs +1.35%）；
<b>财报月（4/8/10 月）是首板的淡季</b>（+1.14% vs 非财报月 +1.75%）——业绩爆雷风险压制题材炒作。</div>

<h2>六、行业偏好</h2>
<p>首板成功率与行业高度相关（样本 ≥ 300 才列出，扣费后）。</p>
{ihtml}
<div class="note"><b>软件服务、医疗保健、航空、机械基件</b>是首板最"给面子"的行业；
<b>纺织、影视音像、家居用品、供气供热</b>最差。<b>震荡市里题材成长股的首板 > 传统防御股的首板。</b></div>

<h2>七、怎么用</h2>
<div class="note ok">
<b>如果你要的是「值得关注的首板」，按这个顺序看：</b>
<ul>
<li>① <b>先看能不能买到</b>：一字板 / 开盘即涨停的，直接放弃（好也只对别人好）。</li>
<li>② <b>看量能和换手</b>：当天量能不到前一日 1.1 倍、换手率 &lt; 5% 的，是"惜售型"好板；
放量换手巨大的，是"出货型"差板。</li>
<li>③ <b>看能不能形成板块</b>：当天同行业首板 ≥ 7 只（板块共振）的，次日收益 +3.04%、胜率 66.0%。</li>
<li>④ <b>看大盘底色</b>：创业板在 MA20 上方、全市场普涨时打板；创业板破 MA20、市场普跌时收手。</li>
<li>⑤ <b>卖出纪律</b>：隔夜溢价在开盘最高，<b>次日开盘卖（+2.96%）优于收盘卖（+1.94%）</b>；
但真打到连板（次日一字）就不要急着卖，那才是超额收益的来源。</li>
</ul>
</div>
<div class="note warn"><b>⚠ 三条必须一起说的限制：</b>
<ul>
<li><b>「买得到」是估算的。</b> 判据只有「开盘是否已涨停」，真实的封单大小、排队位置拿不到（库里无 L2 数据），
所以「能买到」的样本里仍有一部分实际排不上队。</li>
<li><b>打板依赖盘中执行力。</b> 上表把"首板当天在涨停价成交"当成既定前提，实际要盯盘、要手快；
本工作台的回测引擎只能在 T 日收盘选股、T+1 开盘买，<b>因此"次日开盘买"那条口径（−0.34%）才是你现在能自动跑出来的</b>。</li>
<li><b>成本敏感。</b> 缩量+低换手+共振那条 +4.01% 是按 0.18% 成本算的；成本升到 0.5% 仍有 +3.7%，
但"次日开盘买"类玩法只要成本超过收益立刻转负（本来就只有 +1.2%）。</li>
</ul>
</div>

<div class="foot">
数据：本地 MySQL <code>daily_kline</code> / <code>daily_basic</code> / <code>index_daily</code>（2023-01-03 ~ 2026-09-28）。
复权：因库里无 <code>adj_factor</code>，用 <code>pct_chg</code> 链式累计还原前复权。
涨停判定沿用项目口径 <code>indicators._limit_pct</code>（主板 10% / 创业板·科创板 20% / 北交所 30% / ST 5%，容差 0.15）。
「板块共振」= 当日同 <code>industry</code> 首板家数。「赚钱效应」= 前一交易日涨停股在当日的平均涨幅（当日收盘即可知，无未来函数）。
脚本：<code>tools/_sb_fetch.py · _sb_index.py · _sb_build.py · _sb_market.py · _sb_analyze.py · _sb_env.py · _sb_exec.py · _sb_rule.py · _sb_rule2.py</code>。
</div>
</div></body></html>"""

    with io.open(OUT_HTML, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"\n写出 {OUT_HTML}  ({len(html):,} 字符)")
    print(f"写出 {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
