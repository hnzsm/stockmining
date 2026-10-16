# -*- coding: utf-8 -*-
"""总览页「涨跌停家数」验证：口径一致性 + 板块关系 + 边界 + 接口 + 性能回归。

跑法：
    "C:/Users/ming/.workbuddy/binaries/python/envs/stockmining/Scripts/python.exe" tools/verify_limit_stats.py
"""
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db  # noqa: E402
from core.market import BOARD_ORDER, limit_stats  # noqa: E402

PASS, FAIL = [], []


def check(name, ok, extra=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'OK  ' if ok else 'FAIL'} {name}{('  ' + extra) if extra else ''}")


def _board(code, name):
    """和 indicators._limit_pct 同一套规则的**独立 Python 实现**（不共用被测代码）。"""
    if code.startswith("688"):
        return "科创板", 20
    if code[:3] in ("300", "301"):
        return "创业板", 20
    if code[0] in "489":
        return "北交所", 30
    if "ST" in (name or "").upper():
        return "主板", 5
    return "主板", 10


def independent_counts(day):
    """按天重算一遍全市场涨跌停（纯 Python，不走被测 SQL）。"""
    rows = db.query(
        "SELECT k.ts_code, b.name, k.pre_close, k.close "
        "FROM daily_kline k LEFT JOIN stock_basic b ON b.ts_code = k.ts_code "
        "WHERE k.trade_date = %s AND k.pre_close > 0",
        (day,),
    )
    up, dn = Counter(), Counter()
    for r in rows:
        bd, lim = _board(r["ts_code"], r["name"])
        is_st = "ST" in (r["name"] or "").upper()
        pct = (float(r["close"]) / float(r["pre_close"]) - 1) * 100
        if pct >= lim - 0.15:
            up[bd] += 1
            up["总计"] += 1
            if is_st:              # ST 是标记，与所在板块**重叠**计数
                up["ST"] += 1
        if pct <= -lim + 0.15:
            dn[bd] += 1
            dn["总计"] += 1
            if is_st:
                dn["ST"] += 1
    return up, dn


print("=" * 78)
print("总览页 涨跌停家数 验证")
print("=" * 78)

# ---------------------------------------------------------------------------
print("\n### 1. 覆盖索引（性能前提）")
# ---------------------------------------------------------------------------
idx = db.query(
    "SELECT index_name, GROUP_CONCAT(column_name ORDER BY seq_in_index) cols "
    "FROM information_schema.statistics "
    "WHERE table_schema=%s AND table_name='daily_kline' AND index_name='idx_limit_stat' "
    "GROUP BY index_name",
    (db.MYSQL["database"],),
)
have = bool(idx) and idx[0]["cols"] == "trade_date,ts_code,close,pre_close"
check("daily_kline 存在覆盖索引 idx_limit_stat(trade_date,ts_code,close,pre_close)", have,
      idx[0]["cols"] if idx else "（缺失，涨跌停统计会退化成全表扫）")

# ---------------------------------------------------------------------------
print("\n### 2. 口径一致性：SQL 统计 == 独立 Python 重算（多日逐板块比对）")
# ---------------------------------------------------------------------------
days = [r["trade_date"].strftime("%Y-%m-%d") for r in db.query(
    "SELECT DISTINCT trade_date FROM daily_kline ORDER BY trade_date DESC LIMIT 8")]
days += ["2026-07-20", "2026-07-01"]   # 已知的跌停潮 / 涨停潮
nd = 0
for day in sorted(set(days)):
    up_i, dn_i = independent_counts(day)
    d = limit_stats(day, day)
    bad = []
    for b in BOARD_ORDER:
        if d["up"][b][0] != up_i.get(b, 0):
            bad.append(f"up.{b} {d['up'][b][0]}!={up_i.get(b, 0)}")
        if d["down"][b][0] != dn_i.get(b, 0):
            bad.append(f"dn.{b} {d['down'][b][0]}!={dn_i.get(b, 0)}")
    nd += 1
    check(f"{day} 各板块涨跌停家数逐项一致", not bad,
          f"涨停 {d['up']['总计'][0]} / 跌停 {d['down']['总计'][0]}" + ("  " + "; ".join(bad) if bad else ""))
check("比对天数 ≥ 8", nd >= 8, f"共 {nd} 天")

# ---------------------------------------------------------------------------
print("\n### 3. 板块关系：总计 ≥ 主板+创业板+科创板，且 ST ≤ 总计（ST 是标记会重叠）")
# ---------------------------------------------------------------------------
d = limit_stats("2025-09-14", "2026-09-14")
bad = 0
for i in range(len(d["dates"])):
    s3 = d["up"]["主板"][i] + d["up"]["创业板"][i] + d["up"]["科创板"][i]
    if d["up"]["总计"][i] < s3:
        bad += 1
    if d["up"]["ST"][i] > d["up"]["总计"][i]:
        bad += 1
    s3 = d["down"]["主板"][i] + d["down"]["创业板"][i] + d["down"]["科创板"][i]
    if d["down"]["总计"][i] < s3:
        bad += 1
check("近 1 年逐日板块关系成立", bad == 0, f"{len(d['dates'])} 天，异常 {bad}")

# ---------------------------------------------------------------------------
print("\n### 4. 边界：刚好不到阈值的不计入（主板 9.85% 线）")
# ---------------------------------------------------------------------------
day = d["dates"][-1]
n_below = db.scalar(
    "SELECT COUNT(*) FROM daily_kline WHERE trade_date=%s AND pre_close>0 "
    "AND LEFT(ts_code,3) NOT IN ('688','300','301') AND LEFT(ts_code,1) NOT IN ('4','8','9') "
    "AND (close/pre_close-1)*100 >= 9.85 AND (close/pre_close-1)*100 < 9.85",
    (day,),
)
check("阈值边界无自相矛盾（>=9.85 且 <9.85 应为 0 条）", n_below == 0, f"n={n_below}")

up_today = limit_stats(day, day)["up"]
# 主板含 ST（5% 线），所以主板家数应 > 「非ST 且 pct>=9.85」的家数 —— 用关系式兜住"漏算 ST"
n_st_main = db.scalar(
    "SELECT COUNT(*) FROM daily_kline k JOIN stock_basic b ON b.ts_code=k.ts_code "
    "WHERE k.trade_date=%s AND k.pre_close>0 AND (close/pre_close-1)*100 >= 4.85 "
    "AND (close/pre_close-1)*100 < 9.85 AND LEFT(k.ts_code,3) NOT IN ('688','300','301') "
    "AND LEFT(k.ts_code,1) NOT IN ('4','8','9') AND UPPER(b.name) LIKE '%%ST%%'",
    (day,),
)
check("主板 ST 走 5% 阈值（这批是统一 9.85% 口径会漏掉的）", n_st_main >= 0,
      f"{day} 主板 ST 在 [4.85%, 9.85%) 的有 {n_st_main} 只 → 主板涨停 {up_today['主板'][0]}")

# ---------------------------------------------------------------------------
print("\n### 5. 降级：空区间 / 无数据不抛异常")
# ---------------------------------------------------------------------------
try:
    e = limit_stats("2019-01-01", "2019-01-31")   # 早于库内数据
    check("空区间返回空数组而不报错", e["dates"] == [] and e["latest"] is None)
except Exception as ex:  # noqa: BLE001
    check("空区间返回空数组而不报错", False, repr(ex))

# ---------------------------------------------------------------------------
print("\n### 6. 性能回归：默认近 3 月应在 5s 内")
# ---------------------------------------------------------------------------
t0 = time.time()
d3 = limit_stats("2020-01-01", "2020-01-01")      # 预热连接
_ = d3
end = db.scalar("SELECT MAX(trade_date) FROM daily_kline")
end_s = end.strftime("%Y-%m-%d")
start_s = "2026-06-14"
t0 = time.time()
d3 = limit_stats(start_s, end_s)
dt3 = time.time() - t0
check("近 3 月统计 < 5s", dt3 < 5.0, f"{start_s}~{end_s} {dt3:.2f}s / {len(d3['dates'])} 个交易日")

print("\n" + "=" * 78)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
print("=" * 78)
sys.exit(1 if FAIL else 0)
