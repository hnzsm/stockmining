# -*- coding: utf-8 -*-
"""「宽幅箱体 + 底部抬高 + 均线粘合 + 缩量」形态选股条件实验台。

参照物：用户 2026-09-21 给的日线截图 —— 高点 11.16 / 11.52 / 12.06 逐步抬高、
低点 9.20 / 9.30 / 9.46 同步抬高、均线簇粘合缓慢上行、量能间歇脉冲后归于缩量、
MACD 在零轴附近。本质是**平台蓄势**，不是单边趋势（与 trend_pool_lab 的「新高族」互斥）。

复用 trend_pool_lab 的评估口径：T 日收盘命中 → T+1 开盘买入 → 持 k 日收盘，
excess = 减去**同一交易日**全市场均值（按命中数加权），并给分年绝对均值。

    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tools/box_pool_lab.py
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tools/box_pool_lab.py --only box_core
"""
from __future__ import annotations

import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools import trend_pool_lab as lab  # noqa: E402

# 本实验要用、但 trend_pool_lab 没列进加载清单的字段
lab.FIELDS |= {"ll60", "ll120", "ret120", "ma30", "ma120", "boll_pos", "boll_mid",
               "macd_dif", "macd_dea", "macd_hist", "rsi12", "vma20"}

SANE = ["limit_up = 0", "between(close, 5, 100)", "total_mv > 300000",
        "amount > 50000"]
# 半年整体没涨没崩（箱体，不是单边）
FLAT = ["between(ret120, -0.15, 0.35)"]
# 120 日振幅 20%~60%：太窄是僵尸票，太宽是暴涨暴跌
RANGE = ["between(hh120 / ll120 - 1, 0.2, 0.6)"]
# 低点、高点都在抬高（截图里 9.20→9.30→9.46、11.16→11.52→12.06）
STEP = ["ll60 > ref(ll120, 60) * 1.02", "hh60 > ref(hh120, 60) * 1.02"]
# 均线粘合：四条均线的首尾差 / ma20 < 6%
GLUE = ["(max(max(ma5, ma10), max(ma20, ma60))"
        " - min(min(ma5, ma10), min(ma20, ma60))) / ma20 < 0.06"]
GLUE10 = ["(max(max(ma5, ma10), max(ma20, ma60))"
          " - min(min(ma5, ma10), min(ma20, ma60))) / ma20 < 0.10"]
# 均线走平微升（20 日线 20 日内涨幅 0~10%）
MAUP = ["ma20 > ref(ma20, 20)", "ma20 < ref(ma20, 20) * 1.10"]
# 波动收窄
NARROW = ["atr_pct < 3.5"]
NARROW_BOLL = ["boll_width < 15"]
# 缩量（5 日均量比 < 1）
SHRINK = ["vol_ratio5 < 1.0"]
# 价格贴在 20 日线附近（截图里现价就压在均线簇上）
NEAR = ["between(close, ma20 * 0.97, ma20 * 1.10)"]
# 不在半年高位也不在半年低位
MID = ["between(pos120, 0.3, 0.85)"]

CORE = SANE + FLAT + RANGE + GLUE + NARROW + NEAR
# 近 20 日窄幅（收敛到末端）
TIGHT = ["between(hh20 / ll20 - 1, 0, 0.15)"]
# MACD 贴着零轴（截图里 DIF 0.23 / DEA 0.33 / 柱 -0.19）—— dif 绝对值不可跨票比，
# 除以价格归一
MACD0 = ["abs(macd_dif) / close < 0.01"]
# 无趋势强度（ADX 低 = 确实还在箱体里）
NOADX = ["dmi_adx < 20"]

CASES = [
    ("base_all", [], "全市场基线（超额对照）"),
    ("x_glue_only", SANE + GLUE, "反面对照：只要均线粘合"),
    ("x_box_only", SANE + FLAT + RANGE, "反面对照：只要箱体（半年没涨没崩）"),
    ("box_core", CORE, "箱体 + 粘合 + 波动收窄 + 贴均线"),
    ("box_step", CORE + STEP, "再加低高点逐步抬高"),
    ("box_shrink", CORE + SHRINK, "再加缩量"),
    ("box_step_shrink", CORE + STEP + SHRINK, "抬高 + 缩量"),
    ("box_glue10", SANE + FLAT + RANGE + GLUE10 + NARROW + NEAR,
     "粘合放宽到 10%"),
    ("box_maup", CORE + MAUP, "再加 20 日线缓慢上行"),
    ("box_mid", CORE + MID, "再加不在半年高低位"),
    ("box_boll", SANE + FLAT + RANGE + GLUE + NARROW_BOLL + NEAR,
     "波动收窄改用 boll_width<15"),
    ("box_hi_break", CORE + ["close >= hh60 * 0.98"], "对照：箱体里已摸到 60 日高"),
    ("box_lo", CORE + ["pos60 < 0.4"], "对照：箱体下半部"),
    ("box_all", CORE + STEP + SHRINK + MAUP + MID, "全家桶"),
    ("box_all_nostep", CORE + SHRINK + MAUP + MID, "全家桶去掉抬高"),
    # ---- 第二轮：把截图里剩下的特征补进来 ----
    ("b_tight", CORE + STEP + TIGHT, "末端 20 日窄幅收敛"),
    ("b_macd0", CORE + STEP + MACD0, "MACD 贴零轴"),
    ("b_noadx", CORE + STEP + NOADX, "ADX<20（确认还没走成趋势）"),
    ("b_step_break", CORE + STEP + ["close >= hh60 * 0.98"],
     "抬高后摸到 60 日高（突破当天）"),
    ("b_step_tight_vol", CORE + STEP + TIGHT + ["vol_ratio5 > 1.2"],
     "窄幅收敛 + 当日放量（启动脚）"),
    ("b_step_mid", CORE + STEP + MID, "抬高 + 不在半年高低位"),
    ("b_step_maup", CORE + STEP + MAUP, "抬高 + 均线缓慢上行"),
]

# ---- 第三轮：第二轮里唯一持20/持40 都不塌的是「抬高 + ADX 还低」，围绕它组合 ----
ADXB = ["dmi_adx < 20"]
R3 = CORE + STEP + MID
PULSE = ["count(vol_ratio5 > 1.5, 20) >= 1"]   # 箱体内有过放量脉冲（截图里的红柱）
SQLY = ["turnover_rate > 1", "amount > 100000"]  # 能被 SQL 预筛翻译的两条
CASES3 = [
    ("base_all", [], "全市场基线"),
    ("box_all", CORE + STEP + SHRINK + MAUP + MID, "上轮冠军（15 条）"),
    ("b_noadx", CORE + STEP + NOADX, "上轮次优：抬高 + ADX<20"),
    ("r3_core", R3, "箱体抬高 + 不在半年高低位"),
    ("r3_adx", R3 + ADXB, "上条 + ADX<20"),
    ("r3_adx_shrink", R3 + ADXB + SHRINK, "+ 缩量"),
    ("r3_adx_maup", R3 + ADXB + MAUP, "+ 均线缓慢上行"),
    ("r3_adx_noglue", SANE + FLAT + RANGE + NARROW + NEAR + STEP + MID + ADXB,
     "去掉粘合比（看它到底有没有用）"),
    ("r3_adx_pulse", R3 + ADXB + PULSE, "+ 20 日内有过放量脉冲"),
    ("r3_adx_above60", R3 + ADXB + ["close > ma60"], "+ 站在半年线上"),
    ("r3_adx_sql", R3 + ADXB + SQLY, "+ 两条可 SQL 预筛的（提速版）"),
    ("r3_adx_wide", SANE + FLAT + ["between(hh120 / ll120 - 1, 0.15, 0.5)"]
     + GLUE + NARROW + NEAR + STEP + MID + ADXB, "箱体宽度改成 15%~50%"),
]

# ---- 第四轮：第三轮里唯一「分年持20 超额四年全正」的是 above60，在它上面加/减 ----
A60 = ["close > ma60"]
CASES4 = [
    ("base_all", [], "全市场基线"),
    ("f_60", R3 + ADXB + A60, "锚点：箱体抬高 + ADX<20 + 站上半年线"),
    ("f_60_sql", R3 + ADXB + A60 + SQLY, "+ 两条可 SQL 预筛的流动性下限"),
    ("f_60_sql_maup", R3 + ADXB + A60 + SQLY + MAUP, "+ 均线缓慢上行"),
    ("f_60_sql_pulse", R3 + ADXB + A60 + SQLY + PULSE, "+ 20 日内放量脉冲"),
    ("f_60_sql_narrow", R3 + ADXB + A60 + SQLY + SHRINK, "+ 当日缩量"),
    ("f_60_sql_glue10", SANE + FLAT + RANGE + GLUE10 + NARROW + NEAR + STEP + MID
     + ADXB + A60 + SQLY, "粘合放宽到 10%"),
]


def main():
    dst = os.path.join(ROOT, "tools", "_trend_pool_lab.json")
    bak = dst + ".bak"
    if os.path.exists(dst):
        shutil.copy(dst, bak)
    try:
        for flag, cases in (("--r3", CASES3), ("--r4", CASES4)):
            if flag in sys.argv:
                sys.argv.remove(flag)      # lab.main() 的 argparse 不认这些开关
                lab.CASES = cases
                break
        lab.main()
    finally:
        if os.path.exists(bak):
            shutil.copy(bak, dst)
            print(f"（已还原 {dst}）")


if __name__ == "__main__":
    main()
