# -*- coding: utf-8 -*-
"""十字星池子的「页面口径」条件清单（回测与入库共用这一份）。

条件来自 tools/doji_pool_lab.py 的三轮信号层扫描（2023-01~2026-09，901 个交易日）。
名字：u/d/m = 放量/缩量/量平，leg = 长下影，gap = 高开平走，lo = 60 日低位。
每条都能直接粘进「选股策略」的条件列表（一条一行，**不要写注释** ——
`#` 会让这条翻不成 SQL 预筛，整池退化成全市场扫描）。
"""
from __future__ import annotations

SANE = ["limit_up = 0", "between(close, 5, 100)", "total_mv > 300000",
        "amount > 50000"]
# 标准十字星：实体不超过 0.3%，且真有 2% 以上振幅（否则是一字线/僵尸线）
STAR = ["abs(body) <= 0.3", "amplitude >= 2"]
LEG = ["lower_shadow >= 3", "upper_shadow <= 1"]        # 下影 >=3%、几乎没上影
LEGDOM = ["abs(body) <= 0.3", "lower_shadow >= amplitude * 0.6",
          "amplitude >= 3"]                              # 下影占振幅 6 成以上
LO = ["pos60 < 0.3"]                                     # 还在 60 日区间下三成
UP = ["vol_ratio5 >= 1.5"]
DN = ["vol_ratio5 <= 0.7"]
MID = ["between(vol_ratio5, 0.7, 1.5)"]
GAP = ["gap_open >= 2"]                                  # 高开 2% 以上却平走

POOLS: dict[str, tuple[list[str], str]] = {
    # ---- 信号层最佳：低位长下影星（持20 超额 +2.10，四年绝对均值全正）----
    "leg_lo": (SANE + STAR + LEG + LO, "低位长下影十字星（锚点，1.6 只/日）"),
    "leg_lo_m": (SANE + STAR + LEG + LO + MID, "上条 + 量能平（0.7~1.5 倍）"),
    "leg_lo_u": (SANE + STAR + LEG + LO + UP, "上条 + 放量（0.2 只/日，太稀）"),
    "leg_lo_d": (SANE + STAR + LEG + LO + DN, "上条 + 缩量（0.1 只/日，太稀）"),
    "legdom_lo": (SANE + LEGDOM + LO, "低位下影主导星（放宽版，6.3 只/日）"),
    "legdom_lo_u": (SANE + LEGDOM + LO + UP, "放宽版 + 放量"),
    "legdom_lo_d": (SANE + LEGDOM + LO + DN, "放宽版 + 缩量"),
    # ---- 第二族：高开平走星（2023 塌，2024~2026 好）----
    "gap_calm": (SANE + STAR + GAP + ["ret20 < 0.08"], "高开星 + 近 20 日没大涨"),
    "gap_u": (SANE + STAR + GAP + UP, "高开星 + 放量"),
    # ---- 用户原问题的直白版：不分位置的放量 / 缩量十字星 ----
    "u_star": (SANE + STAR + UP, "放量十字星（朴素定义）"),
    "dn_star": (SANE + STAR + DN, "缩量十字星（朴素定义）"),
}
