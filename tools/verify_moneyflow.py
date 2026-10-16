# -*- coding: utf-8 -*-
"""个股资金流向（moneyflow）的端到端校验。

覆盖六组：
  1. 表与数据：结构、覆盖度、单位（万元/手）
  2. 派生口径：mf_* 与原始列手算逐日相等（含单位换算）
  3. 加载与裁剪：引用 mf_* 才读 money_flow，不引用就完全不读
  4. 周线：按周求和、asof 对齐无未来函数
  5. 字段清单：/api/fields 齐全 + 每个名字都能过后端校验
  6. 端到端：选股/回测真的能用 mf_* 条件跑；数据缺失时安静降级为"恒不成立"

跑法：
    python tools/verify_moneyflow.py
    python tools/verify_moneyflow.py --fast      # 跳过全市场扫描（只留小样本）
"""
from __future__ import annotations

import os
import random
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db, indicators as ind, market, screener, strategy as st   # noqa: E402
from core import universe as uni                                          # noqa: E402

FAST = "--fast" in sys.argv
PASS = 0
FAIL = 0
TOL = 1e-6


def check(name, ok, extra=""):
    global PASS, FAIL
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print(f"  {'OK  ' if ok else 'FAIL'} {name}{'  ' + str(extra) if extra else ''}"
          .replace("\n", " "))


def section(t):
    print(f"\n=== {t} ===")


def close(a, b, rel=TOL, abs_=1e-9):
    try:
        if a is None or b is None:
            return False
        if isinstance(a, float) and a != a:
            return False
        return abs(float(a) - float(b)) <= max(abs_, rel * max(abs(float(a)), 1.0))
    except (TypeError, ValueError):
        return False


print("=" * 78)
print("资金流向（moneyflow）校验")
print("=" * 78)

# ---------------------------------------------------------------------------
section("1. 表结构与数据覆盖")
# ---------------------------------------------------------------------------
cols = [c["Field"] for c in db.query("SHOW COLUMNS FROM money_flow")]
check("money_flow 表存在且列数 = 20", len(cols) == 20, f"{len(cols)} 列")
check("包含 18 个原始字段（小/中/大/特大 × 买/卖 × 量/额 + 净额）",
      all(c in cols for c in ind.MF_RAW_COLS), f"{len(ind.MF_RAW_COLS)} 个")

rows_mf, exact = db.safe_count("money_flow")
check("已同步行数 > 0", rows_mf > 0, f"{rows_mf:,} 行" + ("" if exact else " (估算)"))
r = db.query_one("SELECT MIN(trade_date) a, MAX(trade_date) b, "
                 "COUNT(DISTINCT trade_date) d FROM money_flow")
mf_start, mf_end, mf_days = str(r["a"]), str(r["b"]), int(r["d"])
k_days = db.scalar("SELECT COUNT(DISTINCT trade_date) FROM daily_kline "
                   "WHERE trade_date BETWEEN %s AND %s", (mf_start, mf_end))
print(f"    区间 {mf_start} ~ {mf_end}，{mf_days} 个交易日（同期日线 {k_days} 天）")
check("交易日覆盖率 ≥ 99%（与日线对齐；回补中可忽略此项）",
      k_days and mf_days >= k_days * 0.99, f"{mf_days}/{k_days}")

# 股票集合：money_flow 是 daily_kline 的**子集**。缺失的那部分来自 tushare 的覆盖限制：
#   * 北交所（.BJ）只有一部分标的/一段历史有资金流向数据（实测 5700+ 覆盖标的中 .BJ 343 只）
#   * 个别标的晚开始覆盖（如 302132.SZ 从 2025-07、689009.SH 从 2024-04）
# 真正要守的是两条：① 资金流向不该有日线里没有的股票；② 缺失必须来自这两类原因，
# 而不是「抓漏了某些交易日」。
all_days = [x["trade_date"] for x in db.query(
    "SELECT DISTINCT trade_date FROM money_flow ORDER BY trade_date")]
covered = {x["ts_code"] for x in db.query("SELECT DISTINCT ts_code FROM money_flow")}
universe_k = {x["ts_code"] for x in db.query("SELECT DISTINCT ts_code FROM daily_kline")}
never = universe_k - covered                        # 从来没有资金数据的标的
random.seed(20260912)
picked = random.sample(all_days, min(5, len(all_days)))
extra_mf = 0
miss = set()
for d in picked:
    a = {x["ts_code"] for x in db.query(
        "SELECT ts_code FROM money_flow WHERE trade_date=%s", (d,))}
    b = {x["ts_code"] for x in db.query(
        "SELECT ts_code FROM daily_kline WHERE trade_date=%s", (d,))}
    extra_mf += len(a - b)                       # 资金流向里有、日线里没有的（应为 0）
    miss |= (b - a)
miss_bj = len([c for c in miss if c.endswith(".BJ")])
print(f"    日线 {len(universe_k)} 只 / 有资金数据 {len(covered)} 只 / 从不覆盖 {len(never)} 只")
print(f"    抽样 5 日共 {len(miss)} 只当日无资金数据，其中北交所 {miss_bj} 只，"
      f"非北交所 {sorted(c for c in miss if not c.endswith('.BJ'))[:5]}")
check("资金流向没有「日线里不存在」的股票（是日线的子集）", extra_mf == 0, f"{extra_mf} 只")
check("当日无资金数据的基本都是北交所（tushare 覆盖限制）",
      bool(miss) and miss_bj / len(miss) >= 0.95, f"{miss_bj}/{len(miss)}")
check("资金数据覆盖日线全量的 97% 以上",
      len(covered) / max(len(universe_k), 1) >= 0.97,
      f"{len(covered)}/{len(universe_k)} = {len(covered)/max(len(universe_k),1):.2%}")

# 单位：moneyflow 的量=手（与 kline vol 同口径），额=万元（kline amount 是千元，差 10 倍）
sev = db.query(
    "SELECT m.ts_code, m.trade_date, "
    "(m.buy_sm_vol+m.buy_md_vol+m.buy_lg_vol+m.buy_elg_vol) mv, "
    "(m.buy_sm_amount+m.buy_md_amount+m.buy_lg_amount+m.buy_elg_amount) ma, "
    "k.vol, k.amount FROM money_flow m JOIN daily_kline k "
    "ON k.ts_code=m.ts_code AND k.trade_date=m.trade_date "
    "WHERE m.trade_date=%s AND k.vol>0 AND m.buy_sm_vol IS NOT NULL LIMIT 300",
    (mf_end,))
vrat = np.array([float(x["mv"]) / float(x["vol"]) for x in sev if float(x["vol"])])
arat = np.array([float(x["ma"]) / float(x["amount"]) for x in sev if float(x["amount"])])
check("量口径 = 手（mf 量合计 / 日线 vol ≈ 1）",
      len(vrat) and abs(np.median(vrat) - 1) < 0.02, f"中位数 {np.median(vrat):.4f}")
check("额口径 = 万元（mf 额合计 / 日线 amount ≈ 0.1）",
      len(arat) and abs(np.median(arat) - 0.1) < 0.005, f"中位数 {np.median(arat):.4f}")

# ---------------------------------------------------------------------------
section("2. 派生口径（与原始列手算逐日比对）")
# ---------------------------------------------------------------------------
# 比对窗口用「最早的 120 个已同步交易日」：回补是按日期递增写入的，开头这段必然无空洞，
# 不会被「还没同步到」误判成口径 bug（表中间某天缺数据时下面也会自动跳过）。
win_days = all_days[:max(1, min(len(all_days), 120))]
WIN_S, WIN_E = str(win_days[0]), str(win_days[-1])
print(f"    比对窗口 {WIN_S} ~ {WIN_E}（{len(win_days)} 个交易日）")
codes = [x["ts_code"] for x in db.query(
    "SELECT ts_code FROM money_flow WHERE trade_date=%s "
    "AND buy_lg_amount IS NOT NULL AND buy_elg_amount IS NOT NULL "
    "ORDER BY ts_code LIMIT 12", (WIN_E,))]
start = WIN_S
panel = market.load_panel(codes, start, WIN_E, adjust="none", weekly=False,
                          only=set(ind.MF_DERIVED) | {"close", "amount"})
check("load_panel 能拿到资金流向列（引用 mf_* 时）",
      bool(panel) and "mf_main_net" in next(iter(panel.values())).columns,
      f"{len(panel)} 只")

raw = {}
for x in db.query(
        "SELECT * FROM money_flow WHERE trade_date BETWEEN %s AND %s AND ts_code IN "
        f"({','.join(['%s'] * len(codes))})", (start, WIN_E, *codes)):
    raw[(x["ts_code"], x["trade_date"])] = x

n_pairs = 0
bad = {"main": 0, "net": 0, "ratio": 0, "big": 0, "in5": 0, "sum5": 0, "ratio5": 0}
diff_from_net_mf = 0
checksum_ok = True
for code, df in panel.items():
    for _, row in df.iterrows():
        d = row["trade_date"].date() if hasattr(row["trade_date"], "date") else row["trade_date"]
        m = raw.get((code, d))
        if m is None or m["buy_lg_amount"] is None:
            continue
        n_pairs += 1
        lg = float(m["buy_lg_amount"]) - float(m["sell_lg_amount"])
        elg = float(m["buy_elg_amount"]) - float(m["sell_elg_amount"])
        main = lg + elg
        amt_wan = float(row["amount"]) / 10.0
        if not close(row["mf_main_net"], main, 1e-9, 1e-4):
            bad["main"] += 1
        if not close(row["mf_net"], m["net_mf_amount"], 1e-9, 1e-4):
            bad["net"] += 1
        if amt_wan and not close(row["mf_main_ratio"], main / amt_wan * 100, 1e-6, 1e-6):
            bad["ratio"] += 1
        big = (float(m["buy_lg_amount"]) + float(m["buy_elg_amount"])
               + float(m["sell_lg_amount"]) + float(m["sell_elg_amount"]))
        if amt_wan and not close(row["mf_big_ratio"], big / amt_wan * 100, 1e-6, 1e-6):
            bad["big"] += 1
        if abs(main - float(m["net_mf_amount"])) > 1e-6:
            diff_from_net_mf += 1
        # 5 日窗口：用前 4 行 + 当前行重算，和向量化的值比对
        i = df.index.get_loc(row.name)
        seg = df.iloc[max(0, i - 4): i + 1]
        exp5 = float(seg["mf_main_net"].sum())
        if not close(row["mf_main_net5"], exp5, 1e-9, 1e-4):
            bad["sum5"] += 1
        exp_in = float((seg["mf_main_net"] > 0).sum())
        if not close(row["mf_in5"], exp_in, 1e-9, 1e-9):
            bad["in5"] += 1
        a5 = float(seg["amount"].sum()) / 10.0
        if a5 and not close(row["mf_main_ratio5"], exp5 / a5 * 100, 1e-6, 1e-6):
            bad["ratio5"] += 1

print(f"    比对样本 {n_pairs} 个 (股票,交易日)")
check("mf_main_net = (大单+特大单)净额", n_pairs and bad["main"] == 0, f"不符 {bad['main']}")
check("mf_net = net_mf_amount", n_pairs and bad["net"] == 0, f"不符 {bad['net']}")
check("mf_main_ratio = 主力净额 / 成交额(万元)", n_pairs and bad["ratio"] == 0,
      f"不符 {bad['ratio']}")
check("mf_big_ratio = 大+特大单成交额 / 成交额", n_pairs and bad["big"] == 0,
      f"不符 {bad['big']}")
check("mf_main_net5 = 近 5 日累计主力净额", n_pairs and bad["sum5"] == 0, f"不符 {bad['sum5']}")
check("mf_in5 = 近 5 日主力净流入天数", n_pairs and bad["in5"] == 0, f"不符 {bad['in5']}")
check("mf_main_ratio5 = 5 日主力净额 / 5 日成交额", n_pairs and bad["ratio5"] == 0,
      f"不符 {bad['ratio5']}")
check("主力净额 ≠ net_mf_amount（防张冠李戴，口径本就不同）",
      diff_from_net_mf >= n_pairs * 0.8, f"{diff_from_net_mf}/{n_pairs} 个不相等")

# ---------------------------------------------------------------------------
section("3. 加载与 only 裁剪（不该白读 480 万行 × 18 列）")
# ---------------------------------------------------------------------------
calls = {"n": 0}
_real = market._load_moneyflow


def _spy(*a, **kw):
    calls["n"] += 1
    return _real(*a, **kw)


market._load_moneyflow = _spy
try:
    market.load_panel(codes[:3], start, WIN_E, adjust="qfq", weekly=False,
                      only={"close", "ma5"})
    n_plain = calls["n"]
    calls["n"] = 0
    market.load_panel(codes[:3], start, WIN_E, adjust="qfq", weekly=False,
                      only={"close", "mf_main_net"})
    n_mf = calls["n"]
finally:
    market._load_moneyflow = _real

check("只用技术指标时不读 money_flow", n_plain == 0, f"读了 {n_plain} 次")
check("引用 mf_* 时才读 money_flow", n_mf > 0, f"读了 {n_mf} 次")

p1 = market.load_panel(codes[:3], start, WIN_E, adjust="qfq", weekly=False,
                       only={"close", "ma5"})
p2 = market.load_panel(codes[:3], start, WIN_E, adjust="qfq", weekly=False,
                       only={"close", "mf_main_net"})
d1 = next(iter(p1.values()))
d2 = next(iter(p2.values()))
check("裁剪掉时面板里没有 mf_* 列",
      not any(c.startswith("mf_") for c in d1.columns),
      [c for c in d1.columns if c.startswith("mf_")][:5])
check("引用时面板里有 mf_* 且不是全 NaN",
      "mf_main_net" in d2.columns and d2["mf_main_net"].notna().any(),
      f"非空 {int(d2['mf_main_net'].notna().sum())} 行")

# ---------------------------------------------------------------------------
section("4. 周线（按周求和 + 无未来函数）")
# ---------------------------------------------------------------------------
# 复现 _merge_weekly 的 asof 语义：周线的 trade_date 是 **W-FRI 分箱标签**（该周周五那天，
# 不一定是交易日，比如窗口截断在周二时标签是那周的周五、属于未来日期），
# 日线 t 只能看到「标签 <= t」的最后一根周线 —— 既验按周求和，也验不看未来。
wf = {"w_mf_main_net", "w_close", "w_pct_chg"}
pw = market.load_panel(codes[:6], start, WIN_E, adjust="qfq", weekly=True,
                       weekly_fields=wf, only={"close", "amount", "mf_main_net"})
wbad_sum = wbad_future = wn = 0
detail = ""
for code, df in pw.items():
    if "w_mf_main_net" not in df.columns or df["w_mf_main_net"].isna().all():
        continue
    t = df[["trade_date", "mf_main_net", "w_mf_main_net"]].copy()
    t["trade_date"] = pd.to_datetime(t["trade_date"])
    t = t.sort_values("trade_date")
    t["wk"] = t["trade_date"].dt.to_period("W-FRI")
    t["label"] = t["trade_date"] + pd.to_timedelta(4 - t["trade_date"].dt.weekday, "D")
    labels = t["label"].drop_duplicates().sort_values().to_numpy()
    for row in t.dropna(subset=["w_mf_main_net"]).itertuples():
        cand = labels[labels <= np.datetime64(row.trade_date)]
        if not len(cand):
            continue
        lab = pd.Timestamp(cand[-1])
        if lab > row.trade_date:                     # 用了未来数据
            wbad_future += 1
        exp = t.loc[t["label"] == lab, "mf_main_net"].sum()
        wn += 1
        if not close(row.w_mf_main_net, exp, 1e-6, 1e-3):
            wbad_sum += 1
            detail = f"{code} {row.trade_date.date()} 贴 {row.w_mf_main_net} vs W-FRI 周合计 {exp}"
check("w_mf_main_net = 已收盘 W-FRI 周的主力净额之和", wn > 0 and wbad_sum == 0,
      f"比对 {wn} 行，不符 {wbad_sum} {detail}")
check("周线对齐不使用未来数据（贴的周标签 <= 当日）", wbad_future == 0, f"越界 {wbad_future}")

# 纯周线策略路径
wp = market.compute_weekly_panel(codes[:6], start, WIN_E, adjust="qfq",
                                 only={"close", "mf_main_net", "ma5"})
ok_w = bool(wp) and any("mf_main_net" in d.columns and d["mf_main_net"].notna().any()
                        for d in wp.values())
check("compute_weekly_panel（纯周线策略）也拿得到主力净额", ok_w)

# ---------------------------------------------------------------------------
section("5. 字段清单与校验")
# ---------------------------------------------------------------------------
help_map = st.condition_field_help()
whelp = st.condition_weekly_field_help()
# ⚠ 这里断言的是 MF_AVAILABLE（**对外可选**的 9 个），不是 MF_DERIVED（算出来的 13 个）：
# 分单明细四兄弟（mf_lg_net / mf_elg_net / mf_md_net / mf_sm_net）2026-09-19 起
# 刻意不进「可用字段参考」（太细碎），但**照常计算**（主力净额要用）。
# 混用这两份清单，下面 4 条会一起假红 —— 见紧接着的两条断言，它们专门锁这个区分。
missing = [n for n in ind.MF_AVAILABLE if n not in help_map]
check("日线字段清单含全部 %d 个可用的 mf_* 字段" % len(ind.MF_AVAILABLE),
      not missing, f"缺 {missing}")
check("周线字段清单含 w_mf_* 版本",
      all(f"w_{n}" in whelp for n in ind.MF_AVAILABLE),
      [n for n in ind.MF_AVAILABLE if f"w_{n}" not in whelp][:5])
check("说明里标了「资金流向」来源",
      all("资金流向" in help_map[n] for n in ind.MF_AVAILABLE),
      help_map.get("mf_main_net", "")[:60])
# 反向：分单明细**必须不在**可用字段里（有意的精简，不是漏加）。将来谁想加回来，
# 会先在这里红 —— 顺带提醒同步 MF_HIDDEN / COL_SPEC / README 字段表。
leaked = [n for n in ind.MF_HIDDEN
          if n in help_map or f"w_{n}" in whelp
          or ind.col_spec(n)["label"] != n or ind.col_spec(f"w_{n}")["label"] != f"w_{n}"]
check("分单明细（%d 个）不在可用字段里、也没有表头定义" % len(ind.MF_HIDDEN),
      not leaked, leaked)
check("分单明细仍照常计算（主力净额 = 大单 + 特大单，删了会一起没）",
      set(ind.MF_HIDDEN) <= set(ind.MF_DERIVED) and "mf_main_net" in ind.MF_AVAILABLE,
      f"MF_DERIVED {len(ind.MF_DERIVED)} 个 / MF_AVAILABLE {len(ind.MF_AVAILABLE)} 个")
badname = [n for n in help_map if not st.FIELD_RE.match(n)]
check("字段清单里没有非法标识符（'ma5…ma250' 这类展示名不该混进来）",
      not badname, badname[:5])
# 只断言"不少于"，别写死数字：字段表由各域自己提供（资金流向 13 个 mf_*、
# 筹码与两融 18 个另算），新增一个域就会让写死的数变红，而那不是缺陷。
check("日线字段总数 ≥ 90（81 基础/技术 + 9 资金；筹码/两融另加）",
      len(help_map) >= 90, f"{len(help_map)}")
known = set(st.condition_field_names())
bad_names = [n for n in list(ind.MF_AVAILABLE) + [f"w_{n}" for n in ind.MF_AVAILABLE]
             if n not in known]
check("全部可用的 mf_* 都在「可用字段」集合里", not bad_names, f"缺 {bad_names}")
verr = []
for n in ind.MF_AVAILABLE:
    e = uni.validate({"kind": "cond", "where": [f"{n} > 0"]}, known)
    if e:
        verr.append((n, e))
check("每个 mf_* 字段都能通过股票池条件校验", not verr, verr[:2])

# 接口层面（服务在跑才有意义）
try:
    import json
    import urllib.request
    d = json.load(urllib.request.urlopen("http://127.0.0.1:8777/api/fields", timeout=10))["data"]
    fs = set(d.get("fields") or [])
    if fs:
        check("GET /api/fields 返回了 mf_* 与 w_mf_*",
              all(n in fs for n in ind.MF_AVAILABLE) and "w_mf_main_net" in fs,
              f"{len(fs)} 个字段")
        # 补全候选也不该带分单明细（用户看到的就是它）
        check("GET /api/fields 不返回分单明细（前端补全里看不到）",
              not (set(ind.MF_HIDDEN) & fs),
              sorted(set(ind.MF_HIDDEN) & fs))
        check("接口的 field_help 里有资金流向说明",
              "资金流向" in (d.get("field_help") or {}).get("mf_main_net", ""))
    else:
        print("    （接口未返回字段清单，跳过）")
except Exception as e:  # noqa: BLE001
    print(f"    （服务不可用，跳过接口断言：{e}）")

# ---------------------------------------------------------------------------
section("6. 端到端（选股 / 回测 / 缺数据降级）")
# ---------------------------------------------------------------------------
cfg = st.merge_config({
    "name": "资金流向验证",
    "entry": ["mf_main_net > 0", "close > ma5"],
    "buy_trigger": ["open > 0"],
    "sell_signal": ["close < ma5"],
    "score_expr": "mf_main_ratio",
})
check("required_daily_fields 会带上 mf_*（保证加载器读 money_flow）",
      "mf_main_net" in st.required_daily_fields(cfg),
      str(sorted(st.required_daily_fields(cfg) & set(ind.MF_DERIVED))))

if not FAST:
    res = screener.screen(cfg, universe="000300.SH", trade_date=WIN_E,
                          limit=50, save=False)
    hit = res.get("count", 0)
    check("沪深300 上跑 mf_main_net>0 能出结果", hit > 0,
          f"命中 {hit} / 扫描 {res.get('scanned')}（{res.get('trade_date')}）")
    ok_mf = [it for it in res.get("items", []) if it.get("mf_main_net") is not None]
    check("命中明细带主力净额，且都 > 0",
          len(ok_mf) == len(res.get("items", [])) and all(it["mf_main_net"] > 0 for it in ok_mf),
          f"{len(ok_mf)}/{len(res.get('items', []))}")

    cfg2 = st.merge_config({"name": "极端资金条件", "entry": ["mf_main_net > 1e12"],
                            "buy_trigger": ["open > 0"], "sell_signal": ["close < ma5"]})
    res2 = screener.screen(cfg2, universe="000300.SH", trade_date=WIN_E,
                           limit=10, save=False)
    check("不可能满足的资金条件 → 0 命中（不误报）", res2.get("count", -1) == 0,
          f"命中 {res2.get('count')}")

    # 回测路径
    from core import backtest as bt
    data = bt.prepare_data(codes[:20], start, WIN_E, cfg)
    check("回测的 prepare_data 能处理含 mf_* 的策略", len(data) > 0, f"{len(data)} 只")
else:
    print("    （--fast：跳过全市场扫描与回测加载）")

# 数据缺失时的降级：money_flow 读不到 → 面板仍有 mf 列（全 NaN），条件恒不成立，不崩
_saved = market._load_moneyflow
try:
    market._load_moneyflow = lambda *a, **kw: None
    p = market.load_panel(codes[:3], start, WIN_E, adjust="qfq", weekly=False,
                          weekly_fields={"w_mf_main_net"},
                          only={"close", "ma5", "mf_main_net"})
    d = next(iter(p.values()))
    has_col = "mf_main_net" in d.columns
    sig = st.compute_signals(d, cfg)
    entry_false = not bool(sig["entry"].any())
    check("money_flow 读不到时仍产出 mf 列（不报 KeyError）", has_col)
    check("缺资金数据时条件恒不成立（宁可少选，不放错）", entry_false)
    check("缺数据时面板不是空的（行情照常可用）", len(d) > 0, f"{len(d)} 行")
finally:
    market._load_moneyflow = _saved

print("\n" + "=" * 78)
print(f"通过 {PASS} 项，失败 {FAIL} 项")
print("=" * 78)
sys.exit(1 if FAIL else 0)
