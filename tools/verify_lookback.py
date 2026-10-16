# -*- coding: utf-8 -*-
"""回溯窗口回归：哪些指标字段能省 K 线、哪些一省就换口径（2026-09-19 加）。

背景：一次全 A 股选股 93% 的时间花在 `load_panel`（读 K 线 + 算指标）上，
所以"把 `screener._lookback_start(trade_date, bars=300)` 缩小"是最诱人的优化。
这个脚本用**逐值对拍**说明它能不能做：

  · **有限窗口**字段（boll / ma5~30 / pos20 / limit / mf_* …）：bars=30 与 bars=300 的
    **末根值逐值一致** → 只引用这类字段的策略，缩短窗口可证明无损；
  · **无限记忆**字段（macd / atr / dmi / rsi12·24 / obv / ma250）：EMA / Wilder 平滑的
    值依赖序列起点、`obv` 是累加量直接跟着起点平移 —— 300 根内**都不收敛** → 缩窗口
    等于换口径（指标值本身变了），必须逐策略对拍命中集合才敢动。

断言的是**第一类**（必须逐值一致）；第二类只打印"不一致"，因为那份不一致正是
"不能缩窗口"的证据。想让第二类也可断言，得先证明它们在某个窗口收敛。

用法：$PY -W ignore tools/verify_lookback.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

import numpy as np
import pandas as pd

from core import market, screener

SAMPLE = 300          # 抽样只数（够覆盖各板块，又不至于跑太久）
BASE_BARS = 300       # 基准 = 现行默认

META = {"trade_date", "ts_code", "_name", "industry", "market", "list_date",
        "is_st", "w_last_date"}

# bars=30（≈71 个交易日）就该逐值一致的字段
TIER_30 = [
    "close", "open", "high", "low", "pre_close", "vol", "amount", "pct_chg",
    "amplitude", "avg_price", "body", "upper_shadow", "lower_shadow", "gap_open",
    "is_up", "is_down", "close_pos", "ret1",
    "boll_mid", "boll_upper", "boll_lower", "boll_width", "boll_pos", "boll_dev",
    "ma5", "ma10", "ma20", "ma30", "ma_bull", "vma5", "vma10", "vma20",
    "vol_ratio5", "vol_ratio10", "pos20", "hh20", "ll20",
    "cci14", "wr14", "mtm12", "mtmpct12", "vr",
    "limit_up", "limit_down", "turnover_rate", "volume_ratio", "pe", "pb", "ps",
    "total_mv", "circ_mv", "mf_main_net", "mf_main_ratio", "mf_net", "mf_in5",
]
# 需要更长的窗口：60 日级别 + 5 日外的累计
TIER_60 = ["ma60", "pos60", "hh60", "ll60", "ret5", "ret20", "ret60"]
# 120 日级别
TIER_150 = ["ma120", "pos120", "hh120", "ll120", "ret120", "rsi6"]
# ⚠ 无限记忆 —— 只报告，不断言
#    kdj_k/kdj_d/kdj_j 也在这里：`indicators._kdj` 用的是 `ewm`（K/D 递归平滑），
#    值依赖序列起点、60 根根本不收敛 —— 曾经被错放进 TIER_60，导致 1 条假红
#    （实测：kdj_j 在 bars=60 vs 300 时有 1/286 只不一致）。
INFINITE = ["macd_dif", "macd_dea", "macd_hist", "atr14", "atr_pct",
            "dmi_pdi", "dmi_mdi", "dmi_adx", "rsi12", "rsi24", "obv", "obv_ma10",
            "ma250", "kdj_k", "kdj_d", "kdj_j"]

PASS = 0
FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  OK   %s%s" % (name, (" — " + detail) if detail else ""))
    else:
        FAIL += 1
        print("  FAIL %s — %s" % (name, detail))


def snap(panel, td):
    """{code: {col: 末根值}}，只取 trade_date == td 的那根。"""
    out = {}
    for code, df in panel.items():
        if df is None or df.empty:
            continue
        d = df[df["trade_date"].astype(str).str[:10] == td]
        if d.empty:
            continue
        r = d.iloc[-1]
        out[code] = {c: r[c] for c in df.columns if c not in META}
    return out


def same(a, b) -> bool:
    fa, fb = pd.isna(a), pd.isna(b)
    if fa and fb:
        return True
    if fa or fb:
        return False
    try:
        return bool(abs(float(a) - float(b)) < 1e-9)
    except (TypeError, ValueError):
        return bool(a == b)


def diff_fields(cur, base, fields):
    """返回 [(字段, 不一致只数, 总只数)]。"""
    out = []
    for f in fields:
        bad = tot = 0
        for code, row in cur.items():
            if code not in base or f not in base[code] or f not in row:
                continue
            tot += 1
            if not same(row[f], base[code][f]):
                bad += 1
        out.append((f, bad, tot))
    return out


def main():
    td = market.latest_trade_date()
    codes = market.resolve_universe("all", start=td, end=td)
    step = max(1, len(codes) // SAMPLE)
    codes = codes[::step][:SAMPLE]
    print("回溯窗口回归 | trade_date=%s | 抽样 %d 只 | 基准 bars=%d"
          % (td, len(codes), BASE_BARS))

    base_start = screener._lookback_start(td, bars=BASE_BARS)
    base = snap(market.load_panel(codes, base_start, td, only=None), td)
    print("  基准面板 %d 只（起点 %s）\n" % (len(base), base_start))

    for tier_name, k, fields in (("30", 30, TIER_30), ("60", 60, TIER_60),
                                 ("150", 150, TIER_150)):
        start = screener._lookback_start(td, bars=k)
        cur = snap(market.load_panel(codes, start, td, only=None), td)
        diffs = diff_fields(cur, base, fields)
        missing = [f for f, bad, tot in diffs if tot == 0]
        bad = [(f, b, t) for f, b, t in diffs if b]
        check("bars=%s 与 %s 末根值逐值一致（%d 个字段）" % (k, BASE_BARS, len(fields)),
              not bad and not missing,
              "" if (not bad and not missing) else
              ("不一致 %s；未找到 %s" % (bad[:5], missing[:5])))
        print("       起点 %s（%d 只）" % (start, len(cur)))

    # 无限记忆组：报告 bars=150 的不一致情况（证明"不能缩"）
    print("\n---- 无限记忆字段（EMA/Wilder/累加）：只报告，不断言 ----")
    start150 = screener._lookback_start(td, bars=150)
    cur150 = snap(market.load_panel(codes, start150, td, only=None), td)
    for f, b, t in diff_fields(cur150, base, INFINITE):
        if t == 0:
            print("  %-10s 本次数据里没有该列（跳过）" % f)
        else:
            print("  %-10s bars=150 时有 %d/%d 只与 300 根不同%s"
                  % (f, b, t, "  ← 缩窗口会改口径" if b else ""))

    print("\n回溯窗口回归：%d 通过 / %d 失败" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
