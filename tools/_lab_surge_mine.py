# -*- coding: utf-8 -*-
"""挖掘「10 个交易日内大涨 50%」的样本，dump 出**起涨前几天**的 K 线形态 / 波动 / 位置特征。

口径（唯一定义，后面所有统计都按它来）：
- 事件：交易日 T0 收盘 C0，之后 10 个交易日内（T0+1 .. T0+10）**最高收盘** H，
        H / C0 - 1 >= 50%  ⇒ 记一次事件。T0 = 「起涨前一日」，最早 T0+1 才能买。
- 事件区间：T0 落在 [SINCE, UNTIL]（UNTIL 之后不足 10 根的不算，避免窗口截断）。
- 排除「首板是一字板」：窗口内第一个涨停日若是 high == low（全天一个价 ⇒ 买不进去）则整条丢弃。
  窗口内没有涨停日的（慢涨型）保留。
- 池过滤与平台一致：排除北交所 / ST / 上市不足 60 天。

本版重点：**特征不再是 T0 单日快照，而是「T0 及之前 N 根」的形态序列**，
即回答「上涨前几天发生了什么」。窗口与平台的 `count(x, n)` / `exist(cond, n)` 对齐：
**含当日**，前 5 日 = 最近 5 根（T0, T-1, T-2, T-3, T-4）。

另外每个事件除了 T0 本身，再往前取 LEAD=1..5 天各生成一条观测（列 `lead`），
用来回答「提前几天能选中」：`lead=0` 是起涨前一日，`lead=3` 是提前 3 天。

输出：
  tools/_lab_surge_samples.csv   每行一个「票-日」观测；is_surge=1 样本 / 0 随机基线；lead=提前天数
  tools/_lab_surge_events.csv    事件明细（代码 / T0 / 起涨首日 / 终点 / 涨幅 / 首板是否一字）
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db, market  # noqa: E402

SINCE = "2025-01-01"     # 事件起（用户要「2025 年到现在」）
WARM = "2024-06-01"      # 数据起：给 ma120 / pos120 / ret120 留够回溯
GAIN = 0.50              # 10 日内涨幅门槛
WIN = 10                 # 窗口交易日数
LEAD = 5                 # 每个事件额外往前取几天的观测

FIELDS = {
    "open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount",
    "turnover_rate", "volume_ratio", "total_mv", "circ_mv",
    "ma5", "ma10", "ma20", "ma60", "ma120", "vma5", "vol_ratio5",
    "amplitude", "body", "close_pos", "gap_open", "is_up", "is_down",
    "limit_up", "pos20", "pos60", "pos120", "ret5", "ret20", "ret60", "ret120",
    "hh60", "ll60", "hh120", "ll120", "atr_pct", "boll_pos", "boll_width",
    "ma_bull", "rsi6", "rsi12", "kdj_j", "macd_hist",
    "upper_shadow", "lower_shadow", "avg_price",
}

# T0 单日快照（保留，便于对照）
KEEP = [
    "open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount",
    "turnover_rate", "volume_ratio", "total_mv", "circ_mv",
    "ma5", "ma10", "ma20", "ma60", "ma120", "vma5", "vol_ratio5",
    "amplitude", "body", "close_pos", "gap_open", "is_up", "is_down",
    "limit_up", "pos20", "pos60", "pos120", "ret5", "ret20", "ret60", "ret120",
    "hh60", "ll60", "hh120", "ll120", "atr_pct", "boll_pos", "boll_width",
    "ma_bull", "rsi6", "rsi12", "kdj_j", "macd_hist",
    "upper_shadow", "lower_shadow",
]

# 「前 N 根」形态序列里的 0/1 事件标记；值 = 窗口内是否出现过
EVT_SHAPE = [
    "e_bigup5", "e_bigup10", "e_bigup20",      # 单日涨 >= 5%
    "e_lim5", "e_lim10", "e_lim20", "e_lim60",  # 涨停
    "e_vspike5", "e_vspike10", "e_vspike20",    # 量 >= 2 倍 5 日均量
    "e_star5", "e_star10",                      # 十字星：|实体|<=1.5 且 振幅>=3
    "e_upsh5", "e_upsh10",                      # 长上影 >= 3%
    "e_dnsh5", "e_dnsh10",                      # 长下影 >= 3%
    "e_bigdn5", "e_bigdn10",                    # 单日跌 >= 5%
    "e_amp8_5", "e_amp8_10",                    # 单日振幅 >= 8%
    "e_ma20t5", "e_ma20t10",                    # 盘中破 MA20 但收盘站回（回踩不破）
    "e_ma60t5", "e_ma60t10",
    "e_gold5", "e_gold10",                      # MA5 上穿 MA10
    "e_shrink5",                                # 近 5 日均量 < 近 20 日均量 * 0.8
    "e_ydown3",                                 # 近 3 日有阴线
    "e_flat5",                                  # 近 5 日收盘变异系数 <= 3%（平台整理）
]


def dstr(x) -> str:
    return x.strftime("%Y-%m-%d") if hasattr(x, "strftime") else str(x)[:10]


def load_codes() -> list[str]:
    rows = db.query(
        "SELECT DISTINCT ts_code FROM daily_kline WHERE trade_date >= %s", [SINCE])
    return sorted(r["ts_code"] for r in rows)


def in_universe(code: str, name: str, list_date, date_str: str) -> bool:
    """与平台 DEFAULT_FILT 对齐：排除北交所 / ST / 上市不足 60 天。"""
    c6 = code[:6]
    if c6.startswith(("8", "4", "9")):          # 北交所
        return False
    if "ST" in (name or "").upper():
        return False
    if list_date:
        ld = dstr(list_date)
        if (pd.Timestamp(date_str) - pd.Timestamp(ld)).days < 60:
            return False
    return True


def fwd_stats(close: np.ndarray, i: int, n: int, win: int = WIN):
    """未来收益：最高收盘涨幅 / 第 win 日收盘涨幅 / 期间最深回撤。"""
    if i + win >= n:
        return np.nan, np.nan, np.nan
    c0 = close[i]
    seg = close[i + 1:i + 1 + win]
    if not np.isfinite(c0) or c0 <= 0 or np.isnan(seg).any():
        return np.nan, np.nan, np.nan
    return (float(seg.max() / c0 - 1), float(close[i + win] / c0 - 1),
            float(seg.min() / c0 - 1))


def shape_of(A: dict, i: int) -> dict:
    """「T0 及之前 N 根」的形态 / 波动 / 位置聚合。窗口含当日，与 count(x,n) 一致。"""
    r = {}
    close = A["close"]

    def w(k):
        return max(0, i - k + 1)

    def s(name, k):
        return A[name][w(k):i + 1]

    # ---- 计数 / 累计 ----------------------------------------------------
    r["cnt_up5"] = float(np.nansum(s("is_up", 5)))
    r["cnt_up10"] = float(np.nansum(s("is_up", 10)))
    r["dn5"] = float(np.nansum(s("is_down", 5)))
    r["dn10"] = float(np.nansum(s("is_down", 10)))
    r["sum_ret5"] = float(np.nansum(s("pct_chg", 5)))
    r["sum_ret10"] = float(np.nansum(s("pct_chg", 10)))
    r["lim20"] = float(np.nansum(s("limit_up", 20)))
    r["lim60"] = float(np.nansum(s("limit_up", 60)))

    # ---- 波动 ------------------------------------------------------------
    for k in (5, 10, 20):
        r[f"amp{k}"] = float(np.nanmean(s("amplitude", k)))
        r[f"rng{k}"] = float((np.nanmax(s("high", k)) - np.nanmin(s("low", k)))
                             / close[i] * 100) if close[i] else np.nan
        cv = s("close", k)
        m = np.nanmean(cv)
        r[f"narrow{k}"] = float(np.nanstd(cv) / m * 100) if m else np.nan
    v20 = np.nanmean(s("vol", 20))
    for k in (5, 10):
        r[f"volshr{k}"] = float(np.nanmean(s("vol", k)) / v20) if v20 else np.nan
        v5s = s("vma5", k)
        r[f"maxvolx{k}"] = float(np.nanmax(s("vol", k) / np.where(v5s > 0, v5s, np.nan)))
    r["maxup5"] = float(np.nanmax(s("pct_chg", 5)))
    r["maxup10"] = float(np.nanmax(s("pct_chg", 10)))
    r["maxdn5"] = float(np.nanmin(s("pct_chg", 5)))
    r["maxdn10"] = float(np.nanmin(s("pct_chg", 10)))

    # ---- 位置 ------------------------------------------------------------
    cl = float(close[i])
    for col, div in (("c_ma20", "ma20"), ("c_ma60", "ma60"),
                     ("c_hh60", "hh60"), ("c_hh120", "hh120")):
        v = float(A[div][i]) if np.isfinite(A[div][i]) else np.nan
        r[col] = cl / v - 1 if (v and np.isfinite(v)) else np.nan
    # 距上次涨停多少根（没涨停过 = 99）
    lu = A["limit_up"]
    bl = 99
    for j in range(i, -1, -1):
        if lu[j] == 1:
            bl = i - j
            break
    r["barslast_lim"] = float(bl)

    # ---- 形态事件（0/1） --------------------------------------------------
    def ex(mask, k):
        return 1.0 if np.any(mask) else 0.0

    for k in (5, 10, 20):
        r[f"e_bigup{k}"] = ex(s("pct_chg", k) >= 5.0, k)
    for k in (5, 10, 20, 60):
        r[f"e_lim{k}"] = ex(s("limit_up", k) == 1, k)
    for k in (5, 10, 20):
        v5 = s("vma5", k)
        r[f"e_vspike{k}"] = ex(s("vol", k) >= np.where(v5 > 0, v5 * 2, np.inf), k)
    for k in (5, 10):
        bd, am = s("body", k), s("amplitude", k)
        r[f"e_star{k}"] = ex((np.abs(bd) <= 1.5) & (am >= 3.0), k)
        r[f"e_upsh{k}"] = ex(s("upper_shadow", k) >= 3.0, k)
        r[f"e_dnsh{k}"] = ex(s("lower_shadow", k) >= 3.0, k)
        r[f"e_bigdn{k}"] = ex(s("pct_chg", k) <= -5.0, k)
        r[f"e_amp8_{k}"] = ex(s("amplitude", k) >= 8.0, k)
        lw, m20, m60 = s("low", k), s("ma20", k), s("ma60", k)
        cs = s("close", k)
        r[f"e_ma20t{k}"] = ex((lw <= m20) & (cs > m20), k)
        r[f"e_ma60t{k}"] = ex((lw <= m60) & (cs > m60), k)
        # MA5 上穿 MA10（窗口内任意一根）
        g = 0.0
        for j in range(w(k), i + 1):
            if j > 0 and A["ma5"][j] > A["ma10"][j] \
                    and A["ma5"][j - 1] <= A["ma10"][j - 1]:
                g = 1.0
                break
        r[f"e_gold{k}"] = g
    r["e_shrink5"] = 1.0 if (r["volshr5"] is not None
                             and np.isfinite(r["volshr5"])
                             and r["volshr5"] < 0.8) else 0.0
    r["e_ydown3"] = ex(s("is_down", 3) == 1, 3)
    r["e_flat5"] = 1.0 if (np.isfinite(r["narrow5"]) and r["narrow5"] <= 3.0) else 0.0
    return r


def row_of(A: dict, i: int, code: str, name: str, ds: str, surge: int,
           lead: int = 0, ev_t0: str = "", ev_gain: float = np.nan) -> dict:
    """把第 i 行整理成一条观测（含形态序列特征）。"""
    r = {"ts_code": code, "name": name, "trade_date": ds, "is_surge": surge,
         "lead": lead, "ev_t0": ev_t0, "ev_gain": ev_gain}
    for c in KEEP:
        r[c] = A[c][i] if c in A else np.nan
    r.update(shape_of(A, i))
    close = A["close"]
    n = len(close)
    g10, r10, dd10 = fwd_stats(close, i, n, WIN)
    g5, r5, _ = fwd_stats(close, i, n, 5)
    r["fwd_gain10"], r["fwd_ret10"], r["fwd_dd10"] = g10, r10, dd10
    r["fwd_gain5"], r["fwd_ret5"] = g5, r5
    return r


def detect(df: pd.DataFrame, until: str):
    """在单只票上找事件。df 已按日期升序、含指标列。"""
    d = df.reset_index(drop=True)
    dt = pd.to_datetime(d["trade_date"]).dt.strftime("%Y-%m-%d")
    close = d["close"].to_numpy(dtype=float)
    high = d["high"].to_numpy(dtype=float)
    low = d["low"].to_numpy(dtype=float)
    lu = d["limit_up"].to_numpy(dtype=float) if "limit_up" in d.columns \
        else np.zeros(len(d))
    n = len(d)
    # 未来 10 日最高收盘（严格不含当日）
    fwd = np.full(n, np.nan)
    for k in range(1, WIN + 1):
        shifted = np.full(n, np.nan)
        if n - k > 0:
            shifted[:n - k] = close[k:]          # 未来第 k 根（严格不含当日）
        fwd = np.where(np.isnan(fwd), shifted,
                       np.where(np.isnan(shifted), fwd, np.maximum(fwd, shifted)))
    gain = fwd / close - 1.0

    out = []
    last_end = -1
    for i in range(n):
        ds = dt.iloc[i]
        if ds < SINCE or ds > until:
            continue
        if not np.isfinite(gain[i]) or gain[i] < GAIN:
            continue
        if i <= last_end:                     # 与上一次事件重叠 ⇒ 同一波，跳过
            continue
        # 终点 = 窗口内最高收盘那天
        seg = slice(i + 1, min(i + WIN, n - 1) + 1)
        j = i + 1 + int(np.nanargmax(np.where(np.isnan(close[seg]), -np.inf,
                                              close[seg])))
        # 窗口内第一个涨停日
        lim_idx = [k for k in range(i + 1, j + 1) if lu[k] == 1]
        first_lim = lim_idx[0] if lim_idx else None
        yizi = 0
        if first_lim is not None:
            yizi = 1 if (high[first_lim] == low[first_lim]) else 0
        if yizi:                              # 首板一字板 ⇒ 买不进去，丢弃
            last_end = j
            continue
        out.append({"i": i, "j": j, "gain": gain[i], "first_lim": first_lim})
        last_end = j
    return out, dt, d


def arrays_of(d: pd.DataFrame) -> dict:
    cols = set(KEEP) | {"vma5", "is_up", "is_down", "limit_up", "upper_shadow",
                        "lower_shadow", "ma5", "ma10", "ma20", "ma60",
                        "hh60", "hh120"}
    A = {}
    for c in cols:
        A[c] = d[c].to_numpy(dtype=float) if c in d.columns \
            else np.zeros(len(d)) * np.nan
    return A


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--until", default="2026-09-28")
    ap.add_argument("--base-per-code", type=int, default=2,
                    help="每只票随机抽几个「票-日」当基线")
    ap.add_argument("--batch", type=int, default=400)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 个代码（试跑）")
    ap.add_argument("--out", default="tools/_lab_surge_samples.csv")
    ap.add_argument("--events", default="tools/_lab_surge_events.csv")
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    codes = load_codes()
    if args.limit:
        codes = codes[:args.limit]
    smap = market.get_stock_map() or {}
    print(f"标的 {len(codes)}  区间 {SINCE} ~ {args.until}  预热起点 {WARM}"
          f"  提前观测 LEAD={LEAD}", flush=True)

    last_ok = args.until
    rows, evrows = [], []
    t0 = time.time()
    for bi in range(0, len(codes), args.batch):
        batch = codes[bi:bi + args.batch]
        try:
            panel = market.load_panel(batch, WARM, args.until, freq="daily",
                                      adjust="qfq", with_basic=True,
                                      weekly=False, only=FIELDS)
        except Exception as e:
            print("  批次失败", bi, type(e).__name__, e, flush=True)
            continue
        for code, df in panel.items():
            if df is None or len(df) < 70:
                continue
            meta = smap.get(code) or {}
            name = meta.get("name") or ""
            ldate = meta.get("list_date")
            evs, dt, d = detect(df, last_ok)
            A = arrays_of(d)
            for e in evs:
                i = e["i"]
                t0ds = dt.iloc[i]
                if not in_universe(code, name, ldate, t0ds):
                    continue
                evrows.append({"ts_code": code, "name": name, "t0": t0ds,
                               "t1": dt.iloc[i + 1], "peak": dt.iloc[e["j"]],
                               "gain_pct": round(e["gain"] * 100, 1),
                               "days": e["j"] - i,
                               "first_limit_day": dt.iloc[e["first_lim"]]
                               if e["first_lim"] is not None else "",
                               "yizi": 0})
                # T0 本身 + 提前 1..LEAD 天
                for lead in range(0, LEAD + 1):
                    k = i - lead
                    if k < 0:
                        continue
                    ds = dt.iloc[k]
                    if not in_universe(code, name, ldate, ds):
                        continue
                    rows.append(row_of(A, k, code, name, ds, 1, lead,
                                       t0ds, e["gain"]))
            # 基线：本票 2025 起随机抽 N 个交易日
            idx = np.where((dt >= SINCE).to_numpy() & (dt <= last_ok).to_numpy())[0]
            idx = idx[idx <= len(d) - WIN - 1]        # 后面至少留 10 根算未来收益
            if len(idx) > 30:
                pick = rng.choice(idx, size=min(args.base_per_code, len(idx)),
                                  replace=False)
                for i in pick:
                    ds = dt.iloc[i]
                    if not in_universe(code, name, ldate, ds):
                        continue
                    rows.append(row_of(A, int(i), code, name, ds, 0, 0))
        print(f"[{bi + len(batch)}/{len(codes)}] 事件 {len(evrows)}  "
              f"观测 {len(rows)}  {time.time() - t0:.0f}s", flush=True)

    df = pd.DataFrame(rows)
    df.to_csv(args.out, index=False, encoding="utf-8-sig")
    ev = pd.DataFrame(evrows)
    ev.to_csv(args.events, index=False, encoding="utf-8-sig")
    print(f"事件 {len(ev)}  观测 {len(df)}  → {args.out} / {args.events}")


if __name__ == "__main__":
    main()
