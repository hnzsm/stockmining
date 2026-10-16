# -*- coding: utf-8 -*-
"""对 _selfrein_mine.py 产出的特征表做「赢家 vs 输家」判别 + 阈值搜索。

⚠ 纪律（照 C31/C32 的教训）：
  * 只在 **TRAIN**（默认 t0 < 2025-01-01）上挑特征与阈值；
  * 报告 **VALID** 上的同口径表现，样本外不成立的一律标注；
  * 阈值搜索**必须带最小样本约束**（`--min-n`），否则会收敛到「召回 1%、胜率 100%」的假象；
  * 基线是**随机票-日**不是事件样本：这里的 win/avg 都是**同一批成交**内的对比，
    所以衡量的是「加了这条条件后，剩下的成交是否更好」，不是「这条条件能不能选中暴涨票」。

用法：
    python tools/_selfrein_rank.py [--csv tools/_selfrein_feats.csv] [--split 2025-01-01] [--top 25]
"""
import sys, os, argparse, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
import numpy as np

pd.set_option("display.width", 200)


def auc(x, y):
    """Mann-Whitney AUC：y=1 的值比 y=0 大的概率。0.5 = 无判别力。"""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y).astype(bool)
    m = ~np.isnan(x)
    x, y = x[m], y[m]
    if y.sum() == 0 or (~y).sum() == 0:
        return np.nan, 0
    r = pd.Series(x).rank().values
    n1, n0 = y.sum(), (~y).sum()
    return (r[y].sum() - n1 * (n1 + 1) / 2) / (n1 * n0), len(x)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="tools/_selfrein_feats.csv")
    ap.add_argument("--split", default="2025-01-01")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--min-n", type=int, default=80, help="阈值搜索时子集的最小笔数")
    args = ap.parse_args()

    df = pd.read_csv(args.csv)
    df["win"] = (df.pnl_pct > 0).astype(int)
    df["t0"] = df["t0"].astype(str)
    tr = df[df.t0 < args.split].copy()
    va = df[df.t0 >= args.split].copy()
    print("全样本 %d 笔（胜率 %.1f%%，均 %.3f%%，中位 %.3f%%）" %
          (len(df), df.win.mean() * 100, df.pnl_pct.mean(), df.pnl_pct.median()))
    print("TRAIN(< %s) %d 笔  胜率 %.1f%%  均 %.3f%%" % (args.split, len(tr), tr.win.mean() * 100, tr.pnl_pct.mean()))
    print("VALID(>=%s) %d 笔  胜率 %.1f%%  均 %.3f%%" % (args.split, len(va), va.win.mean() * 100, va.pnl_pct.mean()))

    feats = [c for c in df.columns if c.startswith(("f_", "p1_", "p2_", "x_"))]
    rows = []
    for f in feats:
        a, n = auc(tr[f], tr.win)
        s = tr[[f, "pnl_pct"]].dropna()
        # ⚠ 没有 scipy：spearman = 先取秩再做 pearson
        corr = (s[f].rank().corr(s.pnl_pct.rank()) if len(s) > 30 else np.nan)
        a2, _ = auc(va[f], va.win)
        rows.append({"feat": f, "auc_tr": a, "auc_va": a2, "sp_r": corr,
                     "med_win": tr.loc[tr.win == 1, f].median(),
                     "med_lose": tr.loc[tr.win == 0, f].median(),
                     "nan%": round(df[f].isna().mean() * 100, 1)})
    R = pd.DataFrame(rows)
    R["dist"] = (R.auc_tr - 0.5).abs()
    R = R.sort_values("dist", ascending=False)
    print("\n================ 判别力 Top（按 |AUC-0.5| 排，TRAIN）================")
    print(R.head(args.top).to_string(index=False, float_format=lambda v: "%.4f" % v))

    # ---- 阈值搜索：方向由 AUC 决定 ----
    print("\n================ 单条件阈值搜索（TRAIN 上选、VALID 上验）================")
    base_tr = (len(tr), tr.win.mean() * 100, tr.pnl_pct.mean(), len(va), va.win.mean() * 100, va.pnl_pct.mean())
    print("基线: TRAIN n=%d 胜率%.1f%% 均%.3f%%  |  VALID n=%d 胜率%.1f%% 均%.3f%%" % base_tr)
    out = []
    for f in R.head(45).feat:
        s_tr, s_va = tr[[f, "win", "pnl_pct"]].dropna(), va[[f, "win", "pnl_pct"]].dropna()
        if len(s_tr) < args.min_n * 2:
            continue
        high_good = auc(tr[f], tr.win)[0] >= 0.5
        best = None
        for q in np.arange(0.05, 0.96, 0.05):
            th = s_tr[f].quantile(q)
            sel = s_tr[s_tr[f] >= th] if high_good else s_tr[s_tr[f] <= th]
            if len(sel) < args.min_n:
                continue
            score = sel.pnl_pct.sum()          # 用「总损益」当目标（笔数×均值）
            if best is None or score > best[0]:
                best = (score, th, len(sel), sel.win.mean() * 100, sel.pnl_pct.mean(), q)
        if best is None:
            continue
        _, th, n, wr, ap, q = best
        sel_va = (s_va[s_va[f] >= th] if high_good else s_va[s_va[f] <= th])
        op = ">=" if high_good else "<="
        out.append({"cond": "%s %s %.6g" % (f, op, th), "dir": op,
                    "tr_n": n, "tr_wr": wr, "tr_avg": ap,
                    "va_n": len(sel_va), "va_wr": sel_va.win.mean() * 100 if len(sel_va) else np.nan,
                    "va_avg": sel_va.pnl_pct.mean() if len(sel_va) else np.nan,
                    "q": q})
    O = pd.DataFrame(out).sort_values("tr_avg", ascending=False)
    print(O.to_string(index=False, float_format=lambda v: "%.3f" % v))
    with open("tools/_selfrein_rank.json", "w", encoding="utf-8") as fp:
        json.dump({"baseline": {"tr_n": len(tr), "tr_wr": tr.win.mean() * 100, "tr_avg": tr.pnl_pct.mean(),
                                "va_n": len(va), "va_wr": va.win.mean() * 100, "va_avg": va.pnl_pct.mean()},
                   "top_auc": R.head(45).to_dict("records"),
                   "thresholds": O.to_dict("records")}, fp, ensure_ascii=False, indent=1)
    print("\n→ 写出 tools/_selfrein_rank.json")


if __name__ == "__main__":
    main()
