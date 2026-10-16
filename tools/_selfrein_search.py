# -*- coding: utf-8 -*-
"""联合搜索：入场跳空 × 池条件（T0 特征） × 风控，找**训练与验证同时为正**的子集。

⚠ 只有同时满足下面全部条件的组合才会被列为「候选」，否则一律视为噪声：
    * TRAIN 笔数 >= --min-tr，VALID 笔数 >= --min-va
    * TRAIN 均笔 > 0 **且** VALID 均笔 > 0
    * TRAIN 胜率 >= 35%（不是只靠一两笔大赢家）
"""
import sys, os, argparse, pickle, itertools, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.getcwd(), "tools"))
from _selfrein_sim import sim_one, SLIP                       # noqa: E402

# 池条件候选：(名字, 判定函数, 用的是 T0/T−1 的哪一格信息)
CONDS = [
    ("无", None),
    ("量能>=3.2倍", lambda f: (f.get("x_vol_vs_prev") or 0) >= 3.2),
    ("量能>=2倍", lambda f: (f.get("x_vol_vs_prev") or 0) >= 2.0),
    ("缩量(<0.8倍)", lambda f: (f.get("x_vol_vs_prev") or 9) < 0.8),
    ("T0跌幅浅(>=-0.45%)", lambda f: f.get("f_pct_chg", -99) >= -0.45),
    ("T0跌幅深(<=-9%)", lambda f: f.get("f_pct_chg", 0) <= -9.0),
    ("T0非跌停", lambda f: (f.get("f_limit_down") or 0) == 0),
    ("T0是跌停", lambda f: (f.get("f_limit_down") or 0) == 1),
    ("T0收在低位(close_pos<=0.2)", lambda f: f.get("f_close_pos", 1) <= 0.2),
    ("T0ret20<=8%", lambda f: f.get("f_ret20", 9) <= 0.08),
    ("T0ret20>=25%", lambda f: f.get("f_ret20", -9) >= 0.25),
    ("昨日20日涨幅<=14.7%", lambda f: f.get("p1_ret20", 9) <= 0.147),
    ("昨日20日涨幅>=25%", lambda f: f.get("p1_ret20", -9) >= 0.25),
    ("昨日一字板", lambda f: (f.get("p1_high") or 0) == (f.get("p1_low") or -1)),
    ("昨日非一字板", lambda f: (f.get("p1_high") or 0) != (f.get("p1_low") or -1)),
    ("小市值(<=224151万)", lambda f: f.get("f_total_mv", 9e9) <= 224151),
    ("大市值(>50亿)", lambda f: f.get("f_total_mv", 0) > 500000),
    ("主力净流入(ratio>0)", lambda f: f.get("f_mf_main_ratio", -9) > 0),
    ("T0站上MA20", lambda f: f.get("f_close", 0) > f.get("f_ma20", 9e9)),
    ("T0在MA20下方", lambda f: f.get("f_close", 9e9) < f.get("f_ma20", 0)),
    ("ATR>=4%(高波动)", lambda f: f.get("f_atr_pct", 0) >= 4.0),
    ("换手>=8%", lambda f: f.get("f_turnover_rate", 0) >= 8.0),
]

GAPS = [("无", None),
        ("低开", (-999, 0)), ("高开", (0, 999)),
        ("低开<=-2%", (-999, -2)), ("低开<=-4%", (-999, -4)), ("低开<=-6%", (-999, -6)),
        ("高开>=2%", (2, 999)), ("高开>=5%", (5, 999)),
        ("跳空<-8%", (-999, -8))]

RISK = [dict(sl=None, tp=None, hold=1),
        dict(sl=None, tp=None, hold=2),
        dict(sl=None, tp=None, hold=3),
        dict(sl=-6, tp=None, hold=2),
        dict(sl=-6, tp=12, hold=5)]


def measure(data, cond, gap, risk, lo, hi):
    """lo/hi 是 t0 的字符串区间（左闭右开/闭）。"""
    rows = []
    for d in data:
        if not (lo <= d["t0"] < hi if hi else lo <= d["t0"]):
            continue
        if cond and not cond(d["feats"]):
            continue
        r = sim_one(d["bars"], sl=risk.get("sl"), tp=risk.get("tp"), hold=risk.get("hold"),
                    entry_gap_pct=gap, ref_close=d["ref_close"])
        if r is None:
            continue
        rows.append(r[0])
    if not rows:
        return None
    a = np.array(rows)
    return dict(n=len(a), mean=round(a.mean(), 3), win=round((a > 0).mean() * 100, 1),
                med=round(np.median(a), 3))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pkl", default="tools/_selfrein_fwd.pkl")
    ap.add_argument("--split", default="2025-01-01")
    ap.add_argument("--min-tr", type=int, default=60)
    ap.add_argument("--min-va", type=int, default=30)
    ap.add_argument("--out", default="tools/_selfrein_search.json")
    args = ap.parse_args()
    data = pickle.load(open(args.pkl, "rb"))
    print("载入 %d 笔" % len(data))

    hits, allrows = [], []
    for cname, cf in CONDS:
        for gname, gap in GAPS:
            for risk in RISK:
                tr = measure(data, cf, gap, risk, "0000", args.split)
                va = measure(data, cf, gap, risk, args.split, None)
                if not tr or not va:
                    continue
                row = dict(cond=cname, gap=gname, sl=risk.get("sl"), tp=risk.get("tp"),
                           hold=risk.get("hold"), tr_n=tr["n"], tr_mean=tr["mean"],
                           tr_win=tr["win"], va_n=va["n"], va_mean=va["mean"], va_win=va["win"])
                allrows.append(row)
                if (tr["n"] >= args.min_tr and va["n"] >= args.min_va
                        and tr["mean"] > 0 and va["mean"] > 0 and tr["win"] >= 35):
                    hits.append(row)
    A = pd.DataFrame(allrows)
    A.to_json(args.out, orient="records", force_ascii=False, indent=1)
    print("组合总数 %d，其中「训练与验证同时为正」%d 个" % (len(A), len(hits)))
    pd.set_option("display.width", 250)
    if hits:
        H = pd.DataFrame(hits).sort_values("va_mean", ascending=False)
        print(H.to_string(index=False, float_format=lambda v: "%.3f" % v))
    top = A[(A.tr_n >= args.min_tr) & (A.va_n >= args.min_va)].sort_values("va_mean", ascending=False)
    print("\n---- 按 VALID 均笔排前 15（不筛正负，看上限在哪）----")
    print(top.head(15).to_string(index=False, float_format=lambda v: "%.3f" % v))
    print("\n→ 写出", args.out)


if __name__ == "__main__":
    main()
