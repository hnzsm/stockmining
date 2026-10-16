# -*- coding: utf-8 -*-
"""条件池 SQL 预筛回归（2026-09-19 加）。

背景：`universe.preselect_codes()` 原本是「一票否决」—— 池子条件里只要**有一条**
含 `ref()`/形态字段，就整份放弃、退回全量加载。实测 `pool_strategy.id=5` 的 9 条
条件全是可翻译的，却因为一条 `ref(limit_up,2)=1` 让整条 SQL 白废，全市场
5904 只硬跑 83 秒，而它的当日入池本来就是 **0 只**。

改法：**逐条翻译，翻不了的跳过**。安全性来自一个纯粹的逻辑性质 ——

    预筛只用「条件的子集」 ⇒ 少一条 AND ⇒ 候选集是**超集** ⇒ 绝不会漏掉真命中

所以本脚本最核心的断言就是这条性质（`真命中 ⊆ 预筛结果`），而不是"数量对不对"。

⚠ 另一个必须守住的点：历史层（`ref(x,N)`）必须按「**该股自己的上一根 K 线**」取，
不能用自然日减 N —— 停牌缺口下两者不是同一根。缺行时**保守保留**（`OR kL.ts_code IS NULL`），
宁愿少筛也不能漏。

用法：$PY -W ignore tools/verify_preselect.py
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

import numpy as np
import pandas as pd

from core import db, market, strategy as st, universe as uni

PASS = FAIL = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [OK]   %s%s" % (name, ("  | " + extra) if extra else ""))
    else:
        FAIL += 1
        print("  [FAIL] %s%s" % (name, ("  | " + extra) if extra else ""))


SHAPES = ["amplitude", "body", "upper_shadow", "lower_shadow", "gap_open",
          "close_pos", "is_up", "is_down"]


def main() -> None:
    td = market.latest_trade_date()
    if not td:
        print("本地无行情数据，跳过")
        return
    print("选股日 %s" % td)

    # ------------------------------------------------------------------
    print("\n### 1. 形态字段：SQL 表达式 vs 面板列（未复权 vs 前复权，必须逐值一致）")
    # ------------------------------------------------------------------
    # 依据：这些量都是**同一根 K 线内**的比例/比较，而 apply_adjust 给
    # open/high/low/close/pre_close 乘的是同一个 ratio → 比值与比较方向都不变。
    codes = market.resolve_universe("000300.SH")[:150]
    panel = market.load_panel(codes, market.get_trade_dates(None, td)[-12], td, only=None)
    sel = ", ".join("k.ts_code AS ts_code, %s AS %s"
                    % (uni._SHAPE_SQL[f].format(a="k"), f) for f in SHAPES)
    ph = ",".join(["%s"] * len(codes))
    rows = db.query("SELECT %s FROM daily_kline k WHERE k.trade_date=%%s AND k.ts_code IN (%s)"
                    % (sel, ph), [td] + codes)
    sq = {r["ts_code"]: r for r in rows}
    worst, worst_where = 0.0, ""
    n_cmp = 0
    for code, df in panel.items():
        if df is None or df.empty or code not in sq:
            continue
        if str(df["trade_date"].iloc[-1])[:10] != td:
            continue
        last, srow = df.iloc[-1], sq[code]
        for f in SHAPES:
            if f not in df.columns:
                continue
            a = float(last[f]) if pd.notna(last[f]) else None
            b = float(srow[f]) if srow.get(f) is not None else None
            if a is None or b is None:
                continue
            n_cmp += 1
            d = abs(a - b)
            if d > worst:
                worst, worst_where = d, "%s.%s" % (code, f)
    check("8 个形态列逐值一致（比对 %d 个值）" % n_cmp,
          n_cmp > 500 and worst < 1e-6, "最大差 %.2e @ %s" % (worst, worst_where or "-"))

    # ------------------------------------------------------------------
    print("\n### 2. ref 层：SQL 的 k1 vs 面板的上一根（必须逐值一致）")
    # ------------------------------------------------------------------
    w0 = uni._prev_trade_date(td, 3)
    sel1 = ", ".join("%s AS %s" % (uni._SHAPE_SQL[f].format(a="k1"), f)
                     for f in ("amplitude", "body", "is_down", "is_up"))
    rows = db.query(
        "WITH pr AS (SELECT ts_code, trade_date,"
        " ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_date DESC) AS back"
        " FROM daily_kline WHERE trade_date < %%s AND trade_date >= %%s) "
        "SELECT k.ts_code AS ts_code, %s FROM daily_kline k "
        "LEFT JOIN pr pr1 ON pr1.ts_code=k.ts_code AND pr1.back=1 "
        "LEFT JOIN daily_kline k1 ON k1.ts_code=pr1.ts_code AND k1.trade_date=pr1.trade_date "
        "WHERE k.trade_date=%%s AND k.ts_code IN (%s)" % (sel1, ph),
        [td, w0, td] + codes)
    sq1 = {r["ts_code"]: r for r in rows}
    worst, worst_where, n_cmp = 0.0, "", 0
    for code, df in panel.items():
        if df is None or len(df) < 2 or code not in sq1:
            continue
        if str(df["trade_date"].iloc[-1])[:10] != td:
            continue
        prev, srow = df.iloc[-2], sq1[code]
        if srow.get("amplitude") is None:
            continue          # 该票上一根不在窗口里 → SQL 侧 NULL（保守保留，比对不了）
        for f in ("amplitude", "body", "is_down", "is_up"):
            a = float(prev[f]) if pd.notna(prev[f]) else None
            b = float(srow[f]) if srow.get(f) is not None else None
            if a is None or b is None:
                continue
            n_cmp += 1
            d = abs(a - b)
            if d > worst:
                worst, worst_where = d, "%s.%s" % (code, f)
    check("ref 层逐值一致（比对 %d 个值）" % n_cmp,
          n_cmp > 300 and worst < 1e-6, "最大差 %.2e @ %s" % (worst, worst_where or "-"))

    # ------------------------------------------------------------------
    print("\n### 3. ★ 超集性质：预筛结果必须包含**全部**真命中")
    # ------------------------------------------------------------------
    # 用沪深300 当基础池 —— 预筛照样跑全市场 SQL，但"真命中"只需在 300 只里算，
    # 所以能便宜地把这条最关键的断言做实。
    #
    # ⚠ 这条断言有个致命陷阱：**真命中为空时它恒成立**（空集是任何集合的子集）。
    #   所以用例必须挑「本来就会命中若干只」的松散条件，并在最后断言「至少一个
    #   用例的命中数 > 0」—— 否则整段测试是自我安慰。
    D = pd.Timestamp(td)
    start = (D - pd.Timedelta(days=460)).strftime("%Y-%m-%d")
    base = market.resolve_universe("000300.SH")

    SPECS = [
        ("昨日阴 + 今日阳（两条 ref/形态）",
         ["is_up = 1", "ref(is_down, 1) = 1"]),
        ("振幅放大（同日 + ref 形态）",
         ["amplitude >= 2", "ref(amplitude, 1) >= 2"]),
        ("换手活跃（daily_basic + ref）",
         ["turnover_rate > 1", "ref(turnover_rate, 1) > 1", "is_up = 1"]),
        ("ref 叠两层（ref 到 2 日前）",
         ["ref(is_down, 1) = 1", "ref(is_up, 2) = 1"]),
        ("★ 混入一条翻不了的指标条件（验证「跳过」不破坏超集）",
         ["is_up = 1", "close > ma5"]),
        ("★ 混入一条翻不了的复权价条件",
         ["is_up = 1", "close < 50"]),
    ]

    total_hits = 0
    for label, where in SPECS:
        spec = {"base": "000300.SH", "where": list(where)}
        t0 = time.perf_counter()
        presel = uni.preselect_codes(spec, td, td)
        t_presel = time.perf_counter() - t0
        if presel is None:
            cand = list(base)
            tag = "未预筛（全量）"
        else:
            keep = set(presel)
            cand = [c for c in base if c in keep]
            tag = "预筛 %d 只" % len(presel)

        cfg = st.merge_config({"_pool": spec})
        only = st.required_daily_fields(cfg)
        pan = market.load_panel(cand, start, td, only=only) if cand else {}
        hit = set()
        n_err = 0
        for code, df in pan.items():
            if df is None or df.empty:
                continue
            if str(df["trade_date"].iloc[-1])[:10] != td:
                continue
            # ⚠ `compute_pool` 返回的是 **np.ndarray(bool)**，不是 DataFrame —— 别写 `.iloc[-1]`。
            #   这里用真实求值当「真命中」，异常必须显式计数：一旦被静默吞掉，hit 会全空，
            #   下面那条超集断言就退化成「空集 ⊆ 任何集合」的自我安慰。
            try:
                mask = st.compute_pool(df, cfg)
                if bool(np.asarray(mask)[-1]):
                    hit.add(code)
            except Exception as e:  # noqa: BLE001
                n_err += 1
                if n_err <= 3:
                    print("      compute_pool 异常 %s: %s" % (code, e))
        if n_err:
            print("      ⚠ 逐票求值异常 %d 只（命中统计不可信）" % n_err)
        missed = hit - set(cand)
        total_hits += len(hit)
        check("%s" % label, not missed and n_err == 0,
              "%s | 真命中 %d 只 | %.2fs%s%s" % (tag, len(hit), t_presel,
                                                ("  漏掉 %s" % sorted(missed)[:5]) if missed else "",
                                                ("  求值异常 %d 只" % n_err) if n_err else ""))
        del pan

    # 非平凡性：真命中全为 0 时上面那几条等于没测
    check("超集断言非平凡（至少一个用例真有命中）", total_hits > 0,
          "本次数据下总命中 %d 只%s" % (total_hits,
                                        "" if total_hits else "  ← 用例全是空集，断言形同虚设"))

    # 收窄效果：预筛必须真的把候选集压小了（否则"预筛"没意义）
    p = uni.preselect_codes({"base": "000300.SH",
                             "where": ["is_up = 1", "ref(is_down, 1) = 1"]}, td, td)
    check("预筛确有收窄（不是原样返回全市场）",
          p is not None and 0 < len(p) < len(market.resolve_universe("all")),
          "预筛 %s 只 / 全市场 %d 只" % (None if p is None else len(p),
                                         len(market.resolve_universe("all"))))

    # ------------------------------------------------------------------
    print("\n### 4. 翻不了的条件：跳过而不是否决整份预筛")
    # ------------------------------------------------------------------
    all_n = len(market.resolve_universe("all"))
    part = {"base": "all", "where": ["total_mv < 1000000", "macd_hist > 0"]}
    r = uni.preselect_codes(part, td, td)
    check("含指标条件时仍能预筛（不再退回全量）",
          r is not None and 0 < len(r) < all_n,
          "预筛 %s 只 / 全市场 %d 只" % ("None" if r is None else len(r), all_n))
    check("预筛说明如实写了「几条用于预筛、几条跳过」",
          "跳过" in uni.preselect_note(part), uni.preselect_note(part)[:60])

    only_ind = {"base": "all", "where": ["macd_hist > 0"]}
    check("一条都翻不了时才退回全量（返回 None）",
          uni.preselect_codes(only_ind, td, td) is None)

    chips = {"base": "all", "where": ["hold_chg < 0"]}
    check("筹码条件仍退回全量，且说明指名是筹码字段",
          uni.preselect_codes(chips, td, td) is None
          and "筹码" in uni.preselect_note(chips)
          and "ref()" not in uni.preselect_note(chips),
          uni.preselect_note(chips)[:52])

    # ★ 界面判据：`/api/pools` 的 `preselectable` 必须跟**真能不能预筛**走。
    #   陷阱：若拿 `preselect_note() == ""` 当判据，「部分条件可翻」的池子会因为
    #   说明非空而被误标成「含指标，全量加载」—— 实际上它明明走了预筛。
    check("preselectable()：部分可翻 = True，一条都翻不了 = False",
          uni.preselectable(part) is True
          and uni.preselectable(only_ind) is False
          and uni.preselectable(chips) is False,
          "部分可翻 %s / 纯指标 %s / 纯筹码 %s" % (uni.preselectable(part),
                                                  uni.preselectable(only_ind),
                                                  uni.preselectable(chips)))

    # ------------------------------------------------------------------
    print("\n### 5. 区间场景：ref 条件被跳过（区间里「第 N 根」逐行不同），但仍能预筛")
    # ------------------------------------------------------------------
    RANGE_SPEC = {"base": "all",
                  "where": ["is_up = 1", "ref(is_down, 1) = 1", "ref(amplitude, 1) >= 3"]}
    rng = uni.preselect_codes(dict(RANGE_SPEC), "2024-01-02", "2024-12-31")
    check("区间调用不会因 ref 而整份放弃",
          rng is None or (0 < len(rng) <= all_n),
          "区间预筛 %s 只" % ("None" if rng is None else len(rng)))
    day = uni.preselect_codes(dict(RANGE_SPEC), td, td)
    check("区间语义下 ref 被跳过（只剩同日条件），候选明显更宽",
          day is not None and rng is not None and len(day) <= len(rng),
          "单日 %s / 区间 %s" % (None if day is None else len(day),
                                None if rng is None else len(rng)))

    # ------------------------------------------------------------------
    print("\n### 6. body 的符号语义：阳线体为正、阴线体为负（带符号，不是幅度）")
    # ------------------------------------------------------------------
    # ⚠ 这里原本断言「id=5 当日入池 0 只 → 预筛必须为空」。那是**假红温床**：
    #   内置选股策略在界面上改得动（`save_pool` 不拦 builtin），2026-09-19 把它的
    #   `ref(body, 1) >=5` 修正成 `<= -5`（大阴）后真命中变成 4 只，断言当场变红。
    #   **命中数不是「已知答案」**；能锁的只有超集关系（第 3 段，已含非平凡断言）。
    #
    #   真正值得锁的是这次踩坑的根因：`body` **带符号**。谁把它改成 `abs()`
    #   （当成"幅度"），所有「大阴写 `body <= -5`」的条件会**静默反转语义**。
    pan_b = market.load_panel(["000868.SZ", "600519.SH"], start, td,
                              only={"open", "high", "low", "close", "pre_close"})
    bad = []
    for code, d in pan_b.items():
        lo, hi = d[d["close"] < d["open"]], d[d["close"] > d["open"]]
        if (lo["body"] >= 0).any() or (hi["body"] <= 0).any():
            bad.append(code)
    check("body 带符号：阳线体为正、阴线体为负（改成 abs 会静默反转所有用到 body 的条件）",
          bool(pan_b) and not bad,
          "检查 %d 只 | 反例 %s" % (len(pan_b), bad or "无"))

    print("\n条件池预筛回归：%d 通过 / %d 失败" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
