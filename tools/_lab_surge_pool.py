# -*- coding: utf-8 -*-
"""把「10 日涨 50%」挖掘出的条件落成选股池，并在平台上试跑核对命中数。

- 池一律 builtin=0（可删）；同名已存在则原地更新，不重复建。
- 建完立刻试跑最近几个交易日：把「平台算出来的命中数」跟挖掘阶段预估的
  「每天 N 只」对齐 —— 对不上就说明自定义列和平台表达式口径不一致。
"""
from __future__ import annotations

import argparse
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import market, pool as pool_mod, screener, strategy as st, universe as uni  # noqa: E402

POOLS = [
    {
        "name": "暴涨前夜·趋势回踩",
        "expect": 56.0,
        "conds": [
            "ret120 >= 0.4              # 近 120 日涨幅 ≥40%：已在中期上升趋势里",
            "close / ma60 - 1 >= 0.15   # 现价高于 60 日均线 15% 以上",
            "pct_chg <= -2              # 当日回踩跌 ≥2%（洗盘坑）",
            "vol_ratio5 <= 1            # 回踩缩量：量能不超过 5 日均量",
        ],
        "note": (
            "来源：2025-01~2026-09 全市场「10 个交易日内最高收盘涨 ≥50%」的 2275 个样本"
            "（已剔除首板是一字板的）反向挖掘。\n"
            "实测（训练 2025 / 验证 2026 分开看）：\n"
            "· 每天命中约 56 只（训练 55.6 / 验证 56.5）\n"
            "· 命中后真在 10 日内涨 50% 的概率 3.36%，是全市场自然发生率 0.54% 的 6.2 倍"
            "（训练 ×5.8 / 验证 ×6.7，两段一致）\n"
            "· 召回 8.9%：约每 11 个真暴涨股能提前一天抓到 1 个\n"
            "⚠ 期望值要认清楚：命中后「买入拿满 10 天」的收盘涨幅**中位数是 −2.4%**"
            "（训练 −1.7% / 验证 −4.7%）。这是高赔率低胜率的票池 —— 3.4% 的票贡献"
            " +50% 以上，其余多数回落。必须配右侧止盈/严格止损的买卖策略，"
            "裸持有是亏的。"
        ),
    },
    {
        "name": "暴涨前夜·急跌高换手",
        "expect": 37.0,
        "conds": [
            "ret60 >= 0.33       # 近 60 日涨幅 ≥33%：中期强势",
            "pct_chg <= -2.6     # 当日急跌 ≥2.6%",
            "turnover_rate >= 8  # 换手 ≥8%：筹码剧烈交换",
            "atr_pct >= 6        # 日均波幅 ≥6%：高波动票",
        ],
        "note": (
            "同一批样本的备选方案（提升略低于「趋势回踩」且训练/验证差距较大："
            "训练 ×7.6 / 验证 ×2.5，稳定性不如前者）。\n"
            "· 每天命中约 37 只，真涨概率 2.83%（×5.2），召回 6.7%\n"
            "· 命中后拿满 10 天的收盘涨幅中位数 −4.2%\n"
            "⚠ 同样是高赔率低胜率票池，必须配止盈止损。"
        ),
    },
]


def dry_run(conds):
    errs = pool_mod.validate("__dry__", "all", conds)
    return errs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的写库（默认只校验+打印）")
    ap.add_argument("--days", type=int, default=5, help="试跑最近几个交易日")
    args = ap.parse_args()

    for p in POOLS:
        errs = dry_run(p["conds"])
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
        # 试跑核对命中数
        cfg = st.merge_config({})
        cfg["_pool"] = uni.parse({"pool_id": pid})
        cfg["_name"] = p["name"]
        dates = [d for d in market.get_trade_dates("2026-09-01", "2026-09-28")]
        for td in dates[-args.days:]:
            try:
                res = screener.screen(cfg, {"pool_id": pid}, str(td)[:10],
                                      limit=500, save=False)
                print(f"   {str(td)[:10]}  扫描 {res.get('scanned')} → "
                      f"命中 {res.get('count')}   （挖掘预估 {p['expect']:.0f} 只）")
            except Exception as e:
                print("   试跑失败", td, type(e).__name__, e)


if __name__ == "__main__":
    main()
