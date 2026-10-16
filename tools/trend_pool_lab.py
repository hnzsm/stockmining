# -*- coding: utf-8 -*-
"""趋势票（要趋势**前期**）选股条件实验台。

「前期」只能靠买入之后的走势验证，所以口径是前向收益：
T 日收盘命中条件 → T+1 开盘买入 → 持 k 日收盘的收益率。

两个关键设计：
1. **同期全市场基线按日对齐**。2023–2026 这段整体是上行的（全市场持有 20 日
   平均 +0.7%），只看绝对收益会把「beta」当成「alpha」。所以每格同时给
   原始均值和**超额**（命中日的市场均值之差）。
2. 保留两个反面对照：`bull_new_hi`（多头刚成立就追 60 日新高）和
   `late`（多头 + 已涨 60%，趋势中后段）—— 用来判断条件抓到的是启动点还是终点。

    python tools/trend_pool_lab.py                    # 全部 case
    python tools/trend_pool_lab.py --limit 800        # 抽样调试
    python tools/trend_pool_lab.py --only dip_trend,fresh_flat
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import db, expr, market, universe as uni  # noqa: E402
from core import strategy as st  # noqa: E402

START, END = "2023-01-01", "2026-09-18"
FILT = {"exclude_st": True, "exclude_bj": True, "min_amount": 0,
        "min_list_days": 60, "exclude_kcb": False, "exclude_cyb": False}
HORIZONS = (5, 10, 20, 40, 60)

# 趋势已确立（中长期均线多头 + 60 日线上行）
TREND = ["ma_bull = 1", "close > ma60", "ma60 > ref(ma60, 10)"]
# 均线多头刚成立（近 5 日一直多头、20 日前还不多头）
FRESH = ["ma_bull = 1", "count(ma_bull, 5) = 5", "ref(ma_bull, 20) = 0"]
SANITY = ["limit_up = 0", "between(close, 5, 100)", "total_mv > 300000"]
# 注意单位：ret{n} = close/close.shift(n) - 1 是**小数**（0.15 = 15%），
# 而 body / amplitude / pct_chg 是百分数、pos{n} 与 close_pos 是 0~1。
# 上一版把 ret60 > 60 当「涨 60%」用，结果整个对照组 0 命中、`ret60 < 15` 是空条件。
# 第二轮的分年结果：唯一在持 10/20/40 日都为正的一族 = 「均线多头刚成立 + 逼近 60 日
# 新高 + 当日不涨停」，第三轮就在它上面逐条加过滤（BASE_HI）。
BASE_HI = FRESH + ["close >= ref(hh60, 1) * 0.97", "pos120 > 0.6",
                   "limit_up = 0", "pct_chg < 7", "between(close, 5, 100)",
                   "total_mv > 300000"]
# 第三轮发现「多头成立 10 日」优于「成立 5 日」→ 第四轮以它为基准
BASE10 = [e.replace("count(ma_bull, 5) = 5", "count(ma_bull, 10) = 10")
          for e in BASE_HI]
CASES: list[tuple[str, list[str], str]] = [
    ("base_all", [], "全市场基线"),
    ("late", TREND + ["ret60 > 0.6"], "反面对照：趋势中后段（60日已涨60%）"),
    ("bull_new_hi", FRESH + ["close >= ref(hh60, 1) * 0.97", "pos120 > 0.6"],
     "反面对照：刚多头就追 60 日新高"),
    ("dip_trend", TREND + ["close < ma5", "close > ma20"],
     "上升趋势中回踩 5 日线下方"),
    ("dip_shrink", TREND + ["close < ma5", "close > ma20", "vol_ratio5 < 0.9"],
     "回踩 + 缩量"),
    ("dip_ma20", TREND + ["close <= ma20 * 1.02", "close > ll20 * 1.05"],
     "回踩到 20 日线附近"),
    ("dip_ma20_shrink", TREND + ["close <= ma20 * 1.02", "close > ll20 * 1.05",
                                 "vol_ratio5 < 0.9"],
     "回踩 20 日线 + 缩量"),
    ("dip_rebound", TREND + ["close < ma5", "close > ma20", "is_up = 1",
                             "body > 1"],
     "回踩后当日反包收阳"),
    ("early_trend", TREND + ["ret20 < 0.08", "pos120 < 0.6"],
     "多头已成立但近 20 日没大涨、不在高位（趋势前期）"),
    ("early_fresh", FRESH + ["ret20 < 0.08", "pos120 < 0.6"],
     "多头刚成立 + 涨幅未展开"),
    ("ma60_stand", ["count(close > ma60, 3) = 3",
                    "ref(count(close > ma60, 20), 3) <= 4",
                    "ma60 >= ref(ma60, 5)", "ret60 < 0.2"] + SANITY,
     "刚站上 60 日线并站稳 3 日"),
    ("first_cross", ["cross_above(ma5, ma20)", "ma10 > ref(ma10, 1)",
                     "ret60 < 0.2", "vol_ratio5 > 1.2"] + SANITY,
     "5 日上穿 20 日当天"),
    ("ma20_turn", ["count(close > ma20, 3) = 3", "ref(count(close > ma20, 15), 3) <= 4",
                   "ret60 < 0.2", "between(vol_ratio5, 1, 3)"] + SANITY,
     "长期在 20 日线下、刚站上去"),
    ("calm_climb", ["close > ma20", "ma20 > ref(ma20, 5)", "ret20 < 0.08",
                    "between(vol_ratio5, 0.8, 2)", "between(turnover_rate, 1, 10)",
                    "between(pos60, 0.5, 0.95)"] + SANITY,
     "温和爬升（20日线刚抬头）"),
    ("adx_low", ["dmi_adx > 20", "dmi_adx > ref(dmi_adx, 5)", "dmi_pdi > dmi_mdi",
                 "pos120 < 0.5", "close > ma20", "ret20 < 0.1"] + SANITY,
     "低位 ADX 抬头（趋势刚启动）"),
    ("flat_break", ["cross_above(close, ref(hh20, 1))", "vol_ratio5 > 1.5",
                    "ret60 < 0.25", "pos120 < 0.7", "pct_chg < 7"] + SANITY,
     "横盘后放量突破 20 日新高（首日）"),
    ("squeeze", ["between(boll_width, 0, 8)", "close > ma20", "ma20 > ref(ma20, 10)",
                 "ret60 < 0.2", "is_up = 1"] + SANITY,
     "布林收口 + 20 日线走平转多（蓄势）"),
    ("break_nochase", FRESH + ["close >= ref(hh60, 1) * 0.97", "pct_chg < 5",
                               "vol_ratio5 < 2.5"] + SANITY,
     "创新高但当日涨幅/量能克制"),
    # ---- 第二轮：把第一轮有正超额的元素组合起来 ----
    ("c_hi_nolimit", FRESH + ["close >= ref(hh60, 1) * 0.97", "pos120 > 0.6",
                              "limit_up = 0", "pct_chg < 7", "between(close, 5, 100)",
                              "total_mv > 300000"],
     "新高族 + 当日不涨停不追大阳"),
    ("c_hi_volmod", FRESH + ["close >= ref(hh60, 1) * 0.97", "pos120 > 0.6",
                             "between(vol_ratio5, 1, 2.5)"] + SANITY,
     "新高族 + 量能温和（1~2.5 倍 5 日均量）"),
    ("c_hi_small", FRESH + ["close >= ref(hh60, 1) * 0.97", "pos120 > 0.6",
                            "total_mv < 1000000"] + SANITY,
     "新高族 + 市值 < 100 亿"),
    ("c_hi_calm20", FRESH + ["close >= ref(hh60, 1) * 0.97", "ret20 < 0.15"] + SANITY,
     "新高族 + 近 20 日涨幅受控（更贴「前期」）"),
    ("c_squeeze_break", ["between(boll_width, 0, 8)", "close > ma20",
                         "ma20 > ref(ma20, 10)", "ret60 < 0.2",
                         "close >= ref(hh20, 1)", "vol_ratio5 > 1.2"] + SANITY,
     "布林收口后放量突破 20 日高"),
    ("c_dip_calm", TREND + ["close < ma5", "close > ma20", "ret20 < 0.15"] + SANITY,
     "趋势中回踩 + 近 20 日没涨（洗盘型前期）"),
    ("c_adx_strong", ["dmi_adx > 25", "dmi_adx > ref(dmi_adx, 5)",
                      "dmi_pdi > dmi_mdi", "pos120 < 0.5", "close > ma20",
                      "ret20 < 0.1"] + SANITY,
     "低位 ADX>25 抬头"),
    ("c_bull3", ["count(ma_bull, 3) = 3", "ref(ma_bull, 15) = 0", "ret20 < 0.12",
                 "close > ma20", "is_up = 1", "vol_ratio5 > 1.2"] + SANITY,
     "多头成立 3 日 + 当日放量收阳"),
    ("c_hi_squeeze", FRESH + ["close >= ref(hh60, 1) * 0.97",
                             "between(vol_ratio5, 1, 2.5)", "ret20 < 0.15",
                             "limit_up = 0"] + SANITY,
     "新高 + 量能温和 + 近 20 日未展开"),
    # ---- 第三轮：第二轮里唯一在 10/20/40 日都为正的是「新高族 + 不涨停」，
    #      在它上面逐条加过滤，既看能不能把日均 58 只压到可用规模，也看超额掉不掉
    ("r_hi_vol", BASE_HI + ["vol_ratio5 > 1.2"], "基准 + 放量>1.2倍"),
    ("r_hi_volmod", BASE_HI + ["between(vol_ratio5, 1, 2.5)"], "基准 + 量能 1~2.5 倍"),
    ("r_hi_calm20", BASE_HI + ["ret20 < 0.15"], "基准 + 近20日涨幅<15%"),
    ("r_hi_ret60", BASE_HI + ["ret60 < 0.3"], "基准 + 近60日涨幅<30%"),
    ("r_hi_pos80", BASE_HI + ["pos120 > 0.8"], "基准 + 120日位置>0.8"),
    ("r_hi_pos70", BASE_HI + ["pos120 > 0.7"], "基准 + 120日位置>0.7"),
    ("r_hi_mv", BASE_HI + ["total_mv < 1000000"], "基准 + 市值<100亿"),
    ("r_hi_turn", BASE_HI + ["turnover_rate > 3"], "基准 + 换手>3%"),
    ("r_hi_turnlo", BASE_HI + ["turnover_rate < 3"], "基准 + 换手<3%"),
    ("r_hi_ma60up", BASE_HI + ["ma60 > ref(ma60, 10)"], "基准 + 60日线上行"),
    ("r_hi_amt", BASE_HI + ["amount > 200000"], "基准 + 成交额>2亿"),
    ("r_hi_amp", BASE_HI + ["amplitude > 4"], "基准 + 当日振幅>4%"),
    ("r_hi_wick", BASE_HI + ["close_pos > 0.7"], "基准 + 收在日内高位"),
    ("r_hi_body", BASE_HI + ["body > 2"], "基准 + 当日实体>2%"),
    ("r_hi_nofresh", [e.replace("count(ma_bull, 5) = 5", "count(ma_bull, 10) = 10")
                      for e in BASE_HI],
     "基准但多头已成立 10 日（对照「新鲜度」）"),
    # ---- 第四轮：第三轮里分年（按日对齐）不只看 2026 的三条元素是
    #      ① 多头已成立 10 日 > 5 日（r_hi_nofresh）、② 换手 < 3%（r_hi_turnlo）、
    #      ③ 收在日内高位（r_hi_wick）、④ 近 20 日涨幅 < 15%（r_hi_calm20）。
    #      这里做它们的交叉组合，看能不能拼出「四年都不难看」的池子。
    ("q_base10", BASE10, "多头 10 日版基准"),
    ("q_turnlo", BASE10 + ["turnover_rate < 3"], "①+ 换手<3%"),
    ("q_wick", BASE10 + ["close_pos > 0.7"], "①+ 收日内高位"),
    ("q_calm", BASE10 + ["ret20 < 0.15"], "①+ 近20日<15%"),
    ("q_tw_gc", BASE10 + ["turnover_rate < 3", "ret20 < 0.15"], "①+换手低+涨幅受控"),
    ("q_full", BASE10 + ["turnover_rate < 3", "close_pos > 0.7", "ret20 < 0.15"],
     "①+②+③+④"),
    ("q_ma60up", BASE10 + ["ma60 > ref(ma60, 10)", "turnover_rate < 3"],
     "①+60日线上行+低换手"),
    ("q_pos80", BASE10 + ["pos120 > 0.8", "turnover_rate < 3"], "①+位置>0.8+低换手"),
    ("q_dip_turn", TREND + ["close < ma5", "close > ma20", "vol_ratio5 < 0.9",
                            "turnover_rate < 3"] + SANITY,
     "回踩族 + 缩量 + 低换手"),
    ("q_dip_early", TREND + ["close < ma5", "close > ma20", "ret20 < 0.15",
                             "turnover_rate < 5", "pos120 < 0.8"] + SANITY,
     "回踩族 + 涨幅未展开 + 不在极高位"),
    # ---- 第五轮：q_wick 的前向超额四年都在 0 以上，但**组合回测 2023 年化 −30%**
    #      —— 因为「超额」扣掉了当天市场均值，而实盘不能做空：绝对收益为负的年份就是亏。
    #      所以这一轮加「周线向上」这类市场环境过滤，看能不能把 2023/2024 的
    #      **绝对**前向收益抬正（同时看超额别掉）。
    ("w_none", BASE10 + ["close_pos > 0.7"], "对照：q_wick 本身"),
    ("w_up", BASE10 + ["close_pos > 0.7", "w_close > w_ma20"],
     "q_wick + 周线收盘在 20 周线上"),
    ("w_up2", BASE10 + ["close_pos > 0.7", "w_close > w_ma20",
                        "w_ma20 > ref(w_ma20, 4)"],
     "q_wick + 周线向上且 20 周线抬头"),
    ("w_up3", BASE10 + ["close_pos > 0.7", "w_close > w_ma60"],
     "q_wick + 周线收盘在 60 周线上（更松）"),
    ("w_turnlo", BASE10 + ["turnover_rate < 3", "w_close > w_ma20"],
     "q_turnlo + 周线向上"),
    ("w_only", ["w_close > w_ma20", "w_ma20 > ref(w_ma20, 4)", "is_up = 1",
                "ret20 < 0.15"] + SANITY,
     "只看周线向上 + 当日小阳（不要求新高）"),
]

FAILS: set[tuple[str, str]] = set()
FIELDS = set(st.BASE_FIELDS) | {    "ma5", "ma10", "ma20", "ma60", "ma120", "vma5", "ma_bull",
    "hh20", "ll20", "hh60", "hh120", "pos20", "pos60", "pos120",
    "ret5", "ret20", "ret60", "vol_ratio5", "turnover_rate", "volume_ratio",
    "total_mv", "circ_mv", "amplitude", "close_pos", "limit_up", "limit_down",
    "body", "atr_pct", "dmi_adx", "dmi_pdi", "dmi_mdi", "is_up", "boll_width",
    "w_close", "w_ma20", "w_ma60"}
WEEKLY_FIELDS = {"w_close", "w_ma20", "w_ma60"}


def fr_arrs(o: np.ndarray, c: np.ndarray) -> dict[int, np.ndarray]:
    """T 日命中 → T+1 开盘买入 → 持 k 日收盘的前向收益（%）。"""
    n = len(c)
    ent = np.full(n, np.nan)
    ent[:-1] = o[1:]
    out = {}
    for k in HORIZONS:
        r = np.full(n, np.nan)
        j = np.arange(n) + 1 + k
        ok = j < n
        r[ok] = (c[j[ok]] / ent[ok] - 1) * 100
        out[k] = r
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    cases = [c for c in CASES if not args.only or c[0] in args.only.split(",")]

    codes = market.resolve_universe(uni.parse({"base": "ALL", "filt": FILT}),
                                    start=START, end=END)
    if args.limit:
        codes = codes[:args.limit]
    print(f"universe {len(codes)} 只（含 filt）case {len(cases)} 个", flush=True)

    cal = [str(r["trade_date"])[:10] for r in db.query(
        "SELECT DISTINCT trade_date FROM daily_kline WHERE trade_date BETWEEN %s "
        "AND %s ORDER BY trade_date", (START, END))]
    dpos = {d: i for i, d in enumerate(cal)}
    n_days = len(cal) or 900
    # 按日累加：命中数 / 收益和 / 盈利笔数（避免把每笔都留在内存里）
    acc = {k: {"cnt": np.zeros(n_days), "sum": {h: np.zeros(n_days) for h in HORIZONS},
               "win": {h: np.zeros(n_days) for h in HORIZONS},
               "hits": 0, "yr": {y: np.zeros(n_days) for y in ("2023", "2024",
                                                               "2025", "2026")}}
           for k, _c, _d in cases}
    yidx = {y: np.array([1 if d.startswith(y) else 0 for d in cal])
            for y in ("2023", "2024", "2025", "2026")}
    ydays = {y: int(v.sum()) for y, v in yidx.items()}
    # --only 可能没带基线；此时「超额」退化为原始均值（bmean=0）
    base_all_acc = acc.get("base_all") or {
        "cnt": np.ones(n_days), "sum": {h: np.zeros(n_days) for h in HORIZONS}}
    t0 = time.time()
    for i in range(0, len(codes), 250):
        part = codes[i:i + 250]
        panel = market.load_panel(part, START, END, adjust="qfq",
                                  with_basic=True, weekly=True,
                                  weekly_fields=WEEKLY_FIELDS, only=FIELDS)
        for code, df in panel.items():
            n = len(df)
            if n < 60:
                continue
            o = df["open"].to_numpy(dtype=float)
            c = df["close"].to_numpy(dtype=float)
            dates = pd.to_datetime(df["trade_date"]).dt.strftime(
                "%Y-%m-%d").to_numpy()
            gi = np.fromiter((dpos.get(d, -1) for d in dates), dtype=np.int64,
                             count=n)
            ok_g = gi >= 0
            ns = st._namespace(df)
            fr = fr_arrs(o, c)
            for key, conds, _d in cases:
                M = np.ones(n, dtype=bool)
                for e in conds:
                    try:
                        M &= expr.evaluate_bool(e, ns, n)
                    except expr.ExprError:
                        FAILS.add((key, e))
                        M[:] = False
                M &= ok_g
                if not M.any():
                    continue
                a = acc[key]
                idx = gi[M]
                np.add.at(a["cnt"], idx, 1.0)
                a["hits"] += int(M.sum())
                for y in a["yr"]:
                    np.add.at(a["yr"][y], idx, yidx[y][idx])
                for h in HORIZONS:
                    v = fr[h][M]
                    good = np.isfinite(v)
                    np.add.at(a["sum"][h], idx[good], v[good])
                    np.add.at(a["win"][h], idx[good], (v[good] > 0).astype(float))
        print(f"  {min(i + 250, len(codes))}/{len(codes)} {time.time()-t0:.0f}s",
              flush=True)

    def stats(a):
        """返回 (原始均值, 按日对齐的超额均值, 胜率) —— 超额用该 case 命中数加权。"""
        out = {}
        cnt = a["cnt"]
        base = base_all_acc
        for h in HORIZONS:
            s, w = a["sum"][h], a["win"][h]
            tot = s.sum()
            n_tot = (cnt > 0).astype(float)
            denom = cnt.sum()
            bcnt, bsum = base["cnt"], base["sum"][h]
            bmean = np.divide(bsum, bcnt, out=np.zeros_like(bsum),
                              where=bcnt > 0)
            exc = np.divide(s, cnt, out=np.zeros_like(s), where=cnt > 0) - bmean
            exc_w = (exc * cnt).sum() / denom if denom else float("nan")
            out[h] = {"mean": tot / denom if denom else float("nan"),
                      "excess": exc_w,
                      "win": w.sum() / denom if denom else float("nan")}
        return out

    out = []
    print(f"\n=== 前向收益（T+1 开盘买入，持 k 日收盘；单位 %）交易日 {n_days}"
          f"｜excess = 减去当日全市场均值 ===")
    hdr = f"{'case':<14}{'命中':>8}{'日均':>7}{'有票日':>7}"
    for h in HORIZONS:
        hdr += f"{('持' + str(h) + '均'):>9}{'超额':>8}{'胜率':>7}"
    print(hdr)
    for key, conds, desc in cases:
        a = acc[key]
        st_ = stats(a)
        row = {"key": key, "conds": conds, "desc": desc, "hits": a["hits"],
               "per_day": round(a["hits"] / n_days, 2),
               "days_hit": round(float((a["cnt"] > 0).sum()) / n_days * 100, 1),
               "per_day_year": {y: round(float(a["yr"][y].sum() / max(ydays[y], 1)), 1)
                                for y in yidx},
               "fr": {str(h): {k: round(val, 3) for k, val in st_[h].items()}
                      for h in HORIZONS}}
        line = f"{key:<14}{a['hits']:>8}{row['per_day']:>7.1f}{row['days_hit']:>7.0f}"
        for h in HORIZONS:
            line += (f"{st_[h]['mean']:>9.2f}{st_[h]['excess']:>8.2f}"
                     f"{st_[h]['win'] * 100:>7.1f}")
        print(line, flush=True)
        out.append(row)
    path = os.path.join(ROOT, "tools", "_trend_pool_lab.json")
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    def year_excess(a, h, y):
        """该年内、**按日对齐**的超额：Σ_d cnt_d·(case 日均 − 当天全市场日均) / Σcnt。
        与 stats() 的 overall 同一口径（都控制「信号出现在哪一天」），
        这样分年相加才等于总体，不会被「信号集中在弱市日」的择时效应骗到。"""
        m = yidx[y] > 0
        cnt, s = a["cnt"][m], a["sum"][h][m]
        bc, bs = base_all_acc["cnt"][m], base_all_acc["sum"][h][m]
        if not cnt.sum() or not bc.sum():
            return float("nan")
        bmean = np.divide(bs, bc, out=np.zeros_like(bs), where=bc > 0)
        return float((s - cnt * bmean).sum() / cnt.sum())

    def year_mean(a, h, y):
        m = yidx[y] > 0
        cnt, s = a["cnt"][m], a["sum"][h][m]
        return float(s.sum() / cnt.sum()) if cnt.sum() else float("nan")

    # 实盘不能做空 → **绝对**前向收益为负的年份就是亏钱，哪怕超额是正的。
    # 两张表并排打：mean = 该年绝对均值，exc = 按日对齐超额。
    for h in (20, 40):
        print(f"\n=== 分年（持 {h} 日）：mean=绝对均值 % ｜exc=按日对齐超额 % ===")
        print(f"{'case':<16}" + "".join(f"{y + ' mean':>12}{'exc':>8}"
                                        for y in yidx))
        for key, conds, desc in cases:
            a = acc[key]
            print(f"{key:<16}" + "".join(
                f"{year_mean(a, h, y):>12.2f}{year_excess(a, h, y):>8.2f}"
                for y in yidx), flush=True)
    print("\n条件：")
    for key, conds, _d in cases:
        print(f"  {key:<14} {json.dumps(conds, ensure_ascii=False)}")
    print(f"\n日均命中（分年）：")
    for r in out:
        print(f"  {r['key']:<14} {r['per_day_year']}")
    print(f"→ {path}")
    if FAILS:
        print("\n求值报错（这些 case 被置空，先修表达式/字段）：")
        for k, e in sorted(FAILS):
            print(f"  {k:<14} {e}")


if __name__ == "__main__":
    main()
