# -*- coding: utf-8 -*-
"""条件股票池验证：预筛精确性 + 四类预设 + 出池即卖已移除 + 成交额门槛修复。

跑法：
    "C:/Users/ming/.workbuddy/binaries/python/envs/stockmining/Scripts/python.exe" verify_pool.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd

from core import backtest as bt, db, market, strategy as st, universe  # noqa: E402

START, END = "2023-01-03", "2023-06-30"
PASS, FAIL = [], []


def check(name, ok, extra=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'OK  ' if ok else 'FAIL'} {name}{('  ' + extra) if extra else ''}")


print("=" * 78)
print(f"条件股票池验证  区间 {START} ~ {END}")
print("=" * 78)

# ---------------------------------------------------------------------------
print("\n### 1. 四类预设：SQL 预筛集合 vs 逐日门控集合（必须精确相等）")
# ---------------------------------------------------------------------------

SIMPLE = [
    ("小市值", {"base": "all", "where": ["total_mv < 500000"]}),
    ("高换手", {"base": "all", "where": ["turnover_rate > 5"]}),
    ("小市值+活跃", {"base": "all", "where": ["total_mv < 1000000", "turnover_rate > 3"]}),
]

all_codes = market.resolve_universe("all")
sample = all_codes[:500]           # 对照样本（全量逐日计算，成本可控）
print(f"  基础池 {len(all_codes)} 只，对照样本取前 {len(sample)} 只")

for name, spec in SIMPLE:
    t0 = time.time()
    presel = universe.preselect_codes(spec, START, END)
    if presel is None:
        check(f"{name} 预筛可用", False, "（应可预筛却返回 None）")
        continue
    in_sample = {c for c in sample if c in set(presel)}
    panel = market.load_panel(sample, START, END, adjust="qfq",
                              only=st.required_daily_fields({"_pool": spec}))
    gate = set()
    for code, df in panel.items():
        if st.compute_pool(df, {"_pool": spec}).any():
            gate.add(code)
    ok = gate == in_sample
    check(f"{name}：预筛 == 逐日门控（{len(presel)} 只 / 样本内 {len(in_sample)} 只）", ok,
          f"{time.time()-t0:.1f}s" + ("" if ok else f" 差集={gate ^ in_sample}"))
    del panel

# ---------------------------------------------------------------------------
print("\n### 2. 四类预设：单日实际命中（真实数据，看数量级是否合理）")
# ---------------------------------------------------------------------------

DATE = "2023-06-30"
# 口径：只看 DATE 这一天是否入池（不是"区间内曾入池"）。
# 合理区间按 A 股经验给出，用来兜住"数量级明显不对"的回归：
#   小市值 <50亿 ~40%、换手>5% ~13%、收盘>=布林上轨 2%~15%、昨日跌>=5% 0.1%~8%
for name, spec, lo, hi in [
    ("小市值 <50亿", {"base": "all", "where": ["total_mv < 500000"]}, 0.20, 0.60),
    ("换手率 >5%", {"base": "all", "where": ["turnover_rate > 5"]}, 0.05, 0.30),
    ("收盘 >= BOLL上轨", {"base": "all", "where": ["close >= boll_upper"]}, 0.02, 0.20),
    ("昨日跌幅 >=5%", {"base": "all", "where": ["ref(pct_chg, 1) <= -5"]}, 0.001, 0.10),
    ("昨日收阴且跌>=5%",
     {"base": "all", "where": ["ref(pct_chg, 1) <= -5", "ref(is_down, 1) = 1"]}, 0.001, 0.10),
]:
    # 分母必须用「基础池」：可预筛的池子 resolve_universe 返回的 codes 已经是筛后结果
    base_size = len(market.resolve_universe(spec.get("base", "all")))
    codes = market.resolve_universe(spec, start=DATE, end=DATE)
    panel = market.load_panel(codes, "2023-01-01", DATE, adjust="qfq",
                              only=st.required_daily_fields({"_pool": spec}))
    t0 = time.time()
    # 逐股取 DATE 当天的掩码值，而不是整段 any()
    hit = 0
    for _, df in panel.items():
        m = st.compute_pool(df, {"_pool": spec})
        pos = np.where(pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d").to_numpy() == DATE)[0]
        if len(pos) and bool(np.asarray(m)[int(pos[-1])]):
            hit += 1
    ratio = hit / max(1, base_size)
    check(f"{name} @ {DATE} 当日入池占比合理", lo <= ratio <= hi,
          f"基础 {base_size} 只 / 加载 {len(codes)} 只 / 当日入池 {hit} 只 ({ratio:.1%})"
          f" / {time.time()-t0:.1f}s")
    del panel

# ---------------------------------------------------------------------------
print("\n### 3. 回测门控：加池子后交易必须落在池内，且交易数应减少")
# ---------------------------------------------------------------------------

# 买卖策略就地构造：本段只验「池子门控交易」，买卖规则越简单越好 ——
# 而且两策略分离后旧的 #1 已被迁移脚本合理删除、内置行在界面上也改得动，
# 依赖库里的某一行就是假红的来源（见 MEMORY 硬规矩 16）。
cfg0 = st.merge_config({"buy_trigger": ["open > ref(close, 1)"],
                        "sell_signal": ["open < ref(close, 1)"]})
codes = market.resolve_universe("000300.SH")
bench = {r["trade_date"].strftime("%Y-%m-%d"): float(r["close"])
         for r in __import__("core.db", fromlist=["db"]).query(
             "SELECT trade_date,close FROM index_daily WHERE ts_code='000300.SH'"
             " AND trade_date BETWEEN %s AND %s", (START, END)) if r["close"]}

params = {"init_cash": 1_000_000, "max_positions": 10, "position_pct": 10}

data0 = bt.prepare_data(codes, START, END, cfg0, batch=250)
res0 = bt.run_backtest(data0, cfg0, START, END, params, benchmark=bench)
n0 = res0["metrics"].get("trade_count", 0)

POOL = {"base": "000300.SH", "where": ["total_mv < 3000000"]}
cfg1 = {**cfg0, "_pool": POOL}
data1 = bt.prepare_data(codes, START, END, cfg1, batch=250)
res1 = bt.run_backtest(data1, cfg1, START, END, params, benchmark=bench)
n1 = res1["metrics"].get("trade_count", 0)
check("加条件池后交易数减少", n1 < n0, f"无池 {n0} 笔 → 小市值池(<300亿) {n1} 笔")
check("池子内仍有交易（不是空池）", n1 > 0)

# 池子门控与 entry 同一时机：在**信号日 T**（买入日的前一个交易日）求值。
# 所以不变量是「买入必然来自 信号日 的 entry & in_pool」，而不是「买入日当天在池内」——
# 后者会把「昨日大阴」这类条件的含义改成「今日大跌」，反而错。
all_dates = sorted({d for v in data1.values() for d in v["dates"] if START <= d <= END})
dpos = {d: i for i, d in enumerate(all_dates)}
bad = 0
for t in res1["trades"]:
    j = dpos.get(t["buy_date"])
    if j is None or j == 0:
        bad += 1
        continue
    sig_date = all_dates[j - 1]
    v = data1.get(t["ts_code"])
    i = v["idx"].get(sig_date) if v else None
    if i is None or not v["entry"][i] or not v["in_pool"][i]:
        bad += 1
check("每笔买入的信号日都同时满足 entry 与非空条件池", bad == 0, f"越界 {bad} 笔")

# ---------------------------------------------------------------------------
print("\n### 4. 「掉出池子即卖」已彻底移除：池子不再承担卖出语义")
# ---------------------------------------------------------------------------
# 2026-10-10 用户要求：「选股策略中的掉出池子即卖出这个不需要了吧，这个属于买卖策略，
# 选股策略只管选股」→ 彻底移除（DB 列、模型字段、引擎分支一起清）。
# 这里钉三件事，任何一件反弹都要红：
#   ① `universe.parse()` 不再产出 `pool_exit` 键（外面多传也不认）；
#   ② 池子可读标签里不再出现「出池即卖」；
#   ③ 回测不再产生 `pool_out` 离场原因。
_x = universe.parse({"base": "all", "where": ["close > 0"], "pool_exit": True})
check("universe.parse 不再产出 pool_exit 键（多传也忽略）", "pool_exit" not in _x,
      str(sorted(_x)))
_lab = universe.label({"base": "all", "where": ["close >= boll_upper"], "pool_exit": True})
check("池子标签不再带「出池即卖」", "出池即卖" not in _lab, _lab)
check("选股策略表已无 pool_exit 列",
      not db.scalar("SELECT COUNT(*) FROM information_schema.columns "
                    "WHERE table_schema=%s AND table_name='pool_strategy' "
                    "AND column_name='pool_exit'", (db.MYSQL["database"],)),
      "老库由 core/db.DROP_MIGRATIONS 幂等删除")
POOLX = {"base": "000300.SH", "where": ["close >= boll_upper"], "pool_exit": True}
cfg2 = {**cfg0, "_pool": POOLX}
data2 = bt.prepare_data(codes, START, END, cfg2, batch=250)
res2 = bt.run_backtest(data2, cfg2, START, END, params, benchmark=bench)
reasons = res2["metrics"].get("sell_reason_stat", {})
check("回测不再出现 pool_out 离场原因", not reasons.get("pool_out"), f"离场分布 {reasons}")

# ---------------------------------------------------------------------------
print("\n### 5. min_amount 修复：逐日成交额门槛真正生效")
# ---------------------------------------------------------------------------
c = codes[:20]
no_thr = {**cfg0, "pool": {**st.DEFAULT_POOL, "min_amount": 0, "min_list_days": 0}}
mk0 = sum(int(st.compute_pool(df, no_thr).sum()) for _, df in
          market.load_panel(c, START, END, adjust="qfq").items())
hi_thr = {**cfg0, "pool": {**st.DEFAULT_POOL, "min_amount": 500000, "min_list_days": 0}}
mk1 = sum(int(st.compute_pool(df, hi_thr).sum()) for _, df in
          market.load_panel(c, START, END, adjust="qfq").items())
check("提高 min_amount 后入池天数减少", mk1 < mk0, f"门槛0 → {mk0} 天；门槛50亿 → {mk1} 天")
check("空条件池退化为全 True", bool(st.compute_pool(
    list(market.load_panel(codes[:1], START, END, adjust="qfq").values())[0], cfg0).all()))

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
print("\n### 6. 表达式兼容：单个 = 当相等比较（通达信写法）")
# ---------------------------------------------------------------------------
from core import expr  # noqa: E402

check("ns_is_down_double", expr.check_expr("ref(is_down, 1) == 1")[0])
check("ns_is_down_single", expr.check_expr("ref(is_down, 1) = 1")[0])
ns = {"a": __import__("numpy").array([1.0, 0.0, 1.0])}
r_eq = expr.evaluate("a = 1", ns, 3)
check("单个 = 求值等价于 ==", list(map(bool, r_eq)) == [True, False, True])
check("单个 = 与 == 结果一致",
      list(map(bool, r_eq)) == list(map(bool, expr.evaluate("a == 1", ns, 3))))
check("<= 未被误改", list(map(bool, expr.evaluate("a <= 1", ns, 3))) == [True, True, True])
check("!= 未被误改", list(map(bool, expr.evaluate("a != 1", ns, 3))) == [False, True, False])

print("\n" + "=" * 78)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
for f in FAIL:
    print("  未通过:", f)
print("=" * 78)
sys.exit(1 if FAIL else 0)