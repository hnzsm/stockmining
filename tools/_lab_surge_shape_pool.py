# -*- coding: utf-8 -*-
"""把「暴涨前几天的形态规律」落成选股池，并在平台上试跑核对命中数。

条件来自 tools/_lab_surge_shape.py 的挖掘结果（2025 训练 / 2026 验证）：
- 精选 = 单条最优方案（AND），窄但每只更值钱
- 广选 = 5 条方案取并集（写成一条 or 表达式），召回更高但每天更多只

⚠ 池一律 builtin=0（可删）；同名已存在则原地更新。
⚠ 并集必须写成**一行** `or` 表达式：平台条件按行分割、行间是 AND，
   写成多行会被当成「同时满足」，正好把并集变成交集。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import market, pool as pool_mod, screener, strategy as st, universe as uni  # noqa: E402

P1 = ("amplitude >= 11.5094 and std(close, 5) / mean(close, 5) * 100 >= 1.4585 "
      "and mean(amplitude, 10) >= 3.306")
P2 = ("ret20 >= 0.3852 and turnover_rate >= 2.2225 "
      "and (highest(high, 10) - lowest(low, 10)) / close * 100 >= 10.765")
P3 = ("mean(amplitude, 5) >= 9.2665 and turnover_rate >= 2.2225 "
      "and highest(pct_chg, 5) >= 2.2056")
P4 = ("sum(pct_chg, 5) >= 17.2032 and turnover_rate >= 3.6772 "
      "and (highest(high, 10) - lowest(low, 10)) / close * 100 >= 18.3136")
P5 = ("close / ma60 - 1 >= 0.3731 "
      "and std(close, 5) / mean(close, 5) * 100 >= 1.4585 "
      "and std(close, 10) / mean(close, 10) * 100 >= 2.2607")

POOLS = [
    {
        "name": "暴涨前夜·形态精选",
        "expect": 137.0,
        "conds": [
            # 单条方案拆成 3 行（行间 AND，等价）
            "amplitude >= 11.5094                              # 当日振幅 ≥11.5%",
            "std(close, 5) / mean(close, 5) * 100 >= 1.4585    # 近 5 日收盘波动系数 ≥1.46%",
            "mean(amplitude, 10) >= 3.306                      # 近 10 日均振幅 ≥3.3%",
        ],
        "note": (
            "来源：2025-01~2026-09 全市场「10 个交易日内最高收盘涨 ≥50%」的 2275 波"
            "（已剔除首板是一字板的），分析**起涨前 0~3 天**的 K 线形态/波动/位置得出。\n"
            "2026 年（样本外）实测：\n"
            "· 每天命中约 137 只\n"
            "· 每波召回 26.7%：约每 4 个真暴涨股能提前 0~3 天抓到 1 个\n"
            "· 命中后真在 10 日内涨 50% 的概率 2.5%，是全市场自然发生率 0.54% 的 4.6 倍\n"
            "⚠ 这是**候选池不是买入信号**：命中后裸持有 10 天的收盘涨幅中位数 −4.2%。\n"
            "规律（暴涨前几天的共性）：波动已经放大（5 日均振幅 4.6% vs 全市场 3.2%）、"
            "换手更高（3.8% vs 2.2%）、中期已上涨且站在 60 日线上（120 日 +28% vs +13%）、"
            "近期有过涨停、前 5 日常有长上下影。**不是**缩量横盘票。"
        ),
    },
    {
        "name": "暴涨前夜·形态广选",
        "expect": 387.0,
        "conds": [
            # 5 条方案取并集：必须写成一行，行间是 AND
            "(" + P1 + ") or (" + P2 + ") or (" + P3 + ") or ("
            + P4 + ") or (" + P5 + ")   # 5 条形态方案取并集，满足任一即入选",
        ],
        "note": (
            "同一批样本的「召回优先」版本：把 5 条形态方案取并集（满足任一即入选）。\n"
            "2026 年（样本外）实测：\n"
            "· 每天命中约 387 只（精选版 137 只）\n"
            "· 每波召回 40.9%，明显高于精选版 26.7%\n"
            "· 命中后真在 10 日内涨 50% 的概率 2.49%（×4.6），与精选版基本持平\n"
            "5 条方案分别是：① 当日大振幅 + 近 5 日波动系数 + 近 10 日均振幅；"
            "② 近 20 日涨幅 + 换手 + 近 10 日跨度；③ 近 5 日均振幅 + 换手 + 近 5 日最大单日涨幅；"
            "④ 近 5 日累计涨幅 + 换手 + 近 10 日跨度；⑤ 高于 60 日均线 + 近 5/10 日波动系数。\n"
            "⚠ 同样：裸持有 10 天的收盘涨幅中位数 −2.9%，只能当盯盘候选，别直接买。"
        ),
    },
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的写库（默认只校验+打印）")
    ap.add_argument("--days", type=int, default=4, help="试跑最近几个交易日")
    ap.add_argument("--from", dest="dfrom", default="2026-09-01")
    ap.add_argument("--to", dest="dto", default="2026-09-28")
    args = ap.parse_args()

    for p in POOLS:
        errs = pool_mod.validate("__dry__", "all", p["conds"])
        print(f"## {p['name']}  校验: {'OK' if not errs else errs}")
        if errs:
            continue
        if not args.apply:
            for c in p["conds"]:
                print("   ", c)
            continue
        old = pool_mod.get_pool_by_name(p["name"])
        pid = pool_mod.save_pool(p["name"], p["note"], "all", p["conds"],
                                 pid=old["id"] if old else None)
        print(f"   池 id = {pid}（{'更新' if old else '新建'}）")
        cfg = st.merge_config({})
        cfg["_pool"] = uni.parse({"pool_id": pid})
        cfg["_name"] = p["name"]
        dates = [d for d in market.get_trade_dates(args.dfrom, args.dto)]
        for td in dates[-args.days:]:
            try:
                res = screener.screen(cfg, {"pool_id": pid}, str(td)[:10],
                                      limit=2000, save=False)
                print(f"   {str(td)[:10]}  扫描 {res.get('scanned')} → "
                      f"命中 {res.get('count')}   （挖掘预估 {p['expect']:.0f} 只）")
            except Exception as e:
                print("   试跑失败", td, type(e).__name__, e)


if __name__ == "__main__":
    main()
