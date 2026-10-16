# -*- coding: utf-8 -*-
"""「放量十字星 / 缩量十字星」选股条件实验台（信号层）。

十字星在这套字段里的定义：`body` 是带符号实体%（阳正阴负），所以星线要写
`abs(body) <= 0.3`；`amplitude` 是振幅%，没有振幅的十字星是「一字/T字」僵尸线，
所以必须配 `amplitude >= 2`。放量/缩量用 `vol_ratio5`（vol / vma5）。

口径沿用 trend_pool_lab：T 日收盘命中 → T+1 开盘买入 → 持 k 日收盘，
excess = 同交易日全市场均值之差（按命中数加权），并给分年绝对均值（实盘不能做空）。

    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tools/doji_pool_lab.py
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tools/doji_pool_lab.py --r2
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tools/doji_pool_lab.py --only d_up,d_dn
"""
from __future__ import annotations

import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools import trend_pool_lab as lab  # noqa: E402

# 只用日线：十字星不需要周线上下文，跳过周线重采样能省一半加载时间
lab.WEEKLY_FIELDS = set()
lab.FIELDS |= {"upper_shadow", "lower_shadow", "gap_open", "vol", "vma5",
               "ret120", "pos120", "ll60", "boll_pos", "rsi12", "pct_chg",
               "volume_ratio", "limit_down"}

SANE = ["limit_up = 0", "between(close, 5, 100)", "total_mv > 300000",
        "amount > 50000"]
# 标准十字星：实体不超过 0.3%，且真有 2% 以上的振幅（否则是一字/僵尸线）
STAR = ["abs(body) <= 0.3", "amplitude >= 2"]
# 宽松版：实体只占振幅一成以内
STAR_REL = ["abs(body) / amplitude <= 0.1", "amplitude >= 2"]
# 严格版：实体 0.1% 以内、振幅 4% 以上（长腿大十字）
STAR_TIGHT = ["abs(body) <= 0.1", "amplitude >= 4"]
UP = ["vol_ratio5 >= 1.5"]
UP2 = ["vol_ratio5 >= 2.0"]
MID = ["between(vol_ratio5, 0.7, 1.5)"]
DN = ["vol_ratio5 <= 0.7"]
DN2 = ["vol_ratio5 <= 0.5"]
# 位置 / 趋势 / 前一日
HI = ["pos60 > 0.7"]
LO = ["pos60 < 0.3"]
AB20 = ["close > ma20"]
BE20 = ["close < ma20"]
BULL = ["ma_bull = 1"]
CPR = ["close_pos > 0.6"]      # 收在日内上半部
CPL = ["close_pos < 0.4"]
PREV_YANG = ["ref(body, 1) >= 5"]     # 前一天放量大阳（星线=滞涨位置）
PREV_DOWN = ["ref(is_down, 1) = 1"]
PREV_LIMIT = ["ref(limit_up, 1) = 1"]  # 昨天涨停、今天开收持平
RET20_HOT = ["ret20 > 0.15"]
RET20_CALM = ["ret20 < 0.08"]
TURNOVER_HI = ["turnover_rate > 5"]

CASES = [
    ("base_all", [], "全市场基线"),
    ("d_star", SANE + STAR, "所有十字星（不分量能）"),
    ("d_star_rel", SANE + STAR_REL, "十字星改用「实体÷振幅<=0.1」"),
    ("d_up", SANE + STAR + UP, "放量十字星（>=1.5 倍 5 日均量）"),
    ("d_up2", SANE + STAR + UP2, "放量十字星（>=2 倍）"),
    ("d_mid", SANE + STAR + MID, "对照：量能平的十字星"),
    ("d_dn", SANE + STAR + DN, "缩量十字星（<=0.7 倍）"),
    ("d_dn2", SANE + STAR + DN2, "缩量十字星（<=0.5 倍）"),
    ("d_up_hi", SANE + STAR + UP + HI, "放量星 + 60 日高位"),
    ("d_up_lo", SANE + STAR + UP + LO, "放量星 + 60 日低位"),
    ("d_dn_lo", SANE + STAR + DN + LO, "缩量星 + 60 日低位"),
    ("d_dn_hi", SANE + STAR + DN + HI, "缩量星 + 60 日高位"),
    ("d_up_ab20", SANE + STAR + UP + AB20, "放量星 + 站上 20 日线"),
    ("d_up_be20", SANE + STAR + UP + BE20, "放量星 + 20 日线下方"),
    ("d_dn_ab20", SANE + STAR + DN + AB20, "缩量星 + 站上 20 日线"),
    ("d_dn_be20", SANE + STAR + DN + BE20, "缩量星 + 20 日线下方"),
    ("d_up_bull", SANE + STAR + UP + BULL, "放量星 + 均线多头"),
    ("d_dn_bull", SANE + STAR + DN + BULL, "缩量星 + 均线多头（上涨中缩量休整）"),
    ("d_up_prevyang", SANE + STAR + UP + PREV_YANG, "放量星 + 昨日大阳（滞涨位）"),
    ("d_up_prevlimit", SANE + STAR + UP + PREV_LIMIT, "放量星 + 昨日涨停"),
    ("d_dn_prevdown", SANE + STAR + DN + PREV_DOWN, "缩量星 + 昨日阴线（跌势企稳）"),
    ("d_up_cplo", SANE + STAR + UP + CPL, "放量星 + 收在日内低位"),
    ("d_up_cphi", SANE + STAR + UP + CPR, "放量星 + 收在日内高位"),
    ("d_up_retcalm", SANE + STAR + UP + RET20_CALM, "放量星 + 近 20 日没涨"),
    ("d_up_rethot", SANE + STAR + UP + RET20_HOT, "放量星 + 近 20 日已涨 15%"),
    ("d_up_turn", SANE + STAR + UP + TURNOVER_HI, "放量星 + 换手>5%"),
]

# ---- 第二轮：第一轮里**每一个**十字星变体的按日对齐超额都是负的（持20 −0.35~−1.9）。
#      所以这一轮做两件事：① 找能翻正的上下文（止跌、地量、长腿承接、跌停次日…）；
#      ② 加「同上下文但非星线」的配对对照（`abs(body) >= 1`），这样才能分清
#         「是十字星有用」还是「只是上下文有用」。
NONSTAR = ["abs(body) >= 1"]          # 同上下文里的「不是星线」对照组
STARCL = ["count(abs(body) <= 0.5, 5) >= 3"]   # 连续星线群
DILAND = ["vol <= llv(vol, 60) * 1.02"]        # 60 日地量（今日量=近 60 日最低）
DOWN5 = ["count(is_down, 5) >= 4"]             # 连跌 4~5 天
LEG_DOWN = ["lower_shadow >= 3", "upper_shadow <= 1"]   # 下影长腿星
TOMB = ["upper_shadow >= 3", "lower_shadow <= 1"]        # 墓碑线
GAP_DN = ["gap_open <= -2"]                    # 低开、收回（开盘即最低附近）
GAP_UP = ["gap_open >= 2"]
NEAR60 = ["between(close, ma60 * 0.98, ma60 * 1.05)"]
BOT_BOX = ["between(pos120, 0.05, 0.35)", "abs(ret120) <= 0.1"]
REST = ["close > ma20", "ret20 > 0.05", "ret20 < 0.15"]   # 上涨初段的休整
PREV_LD = ["ref(limit_down, 1) = 1"]                       # 昨日跌停

R2 = [
    ("base_all", [], "全市场基线"),    ("a_d_up_lo", SANE + STAR + UP + LO, "锚点：低位放量星（第一轮最佳）"),
    ("a_c_up_lo", SANE + NONSTAR + UP + LO, "对照：低位放量但**不是**星线"),
    ("a_d_dn_lo", SANE + STAR + DN + LO, "锚点：低位缩量星"),
    ("a_c_dn_lo", SANE + NONSTAR + DN + LO, "对照：低位缩量但**不是**星线"),
    ("a_c_rest", SANE + NONSTAR + DN + REST, "对照：上涨休整缩量但非星"),
    ("s_leg_dn", SANE + STAR + LEG_DOWN, "下影长腿星（承接）"),
    ("s_tomb", SANE + STAR + TOMB, "墓碑线（上影长腿）"),
    ("s_leg_dn_lo", SANE + STAR + LEG_DOWN + LO, "下影长腿星 + 低位"),
    ("s_gap_dn", SANE + STAR + GAP_DN, "低开 2% 以上再收回的星"),
    ("s_gap_up", SANE + STAR + GAP_UP, "高开 2% 以上平走的星"),
    ("s_diland", SANE + STAR + DILAND, "60 日地量十字星"),
    ("s_diland_lo", SANE + STAR + DILAND + LO, "地量星 + 60 日低位"),
    ("s_dn5", SANE + STAR + DOWN5, "连跌 4/5 日后的星"),
    ("s_dn5_dn", SANE + STAR + DOWN5 + DN, "连跌后 + 缩量星"),
    ("s_dn5_up", SANE + STAR + DOWN5 + UP, "连跌后 + 放量星"),
    ("s_prevlimit_dn", SANE + STAR + PREV_LD, "跌停次日的星"),
    ("s_bot_box", SANE + STAR + BOT_BOX, "底部箱体里的星（半年没涨没跌）"),
    ("s_bot_box_dn", SANE + STAR + BOT_BOX + DN, "底部箱体 + 缩量星"),
    ("s_rest_dn", SANE + STAR + DN + REST, "上涨初段休整：缩量星"),
    ("s_near60_up", SANE + STAR + UP + NEAR60, "放量星踩在 60 日线上"),
    ("s_tight_lo", SANE + ["abs(body) <= 0.1", "amplitude >= 3"] + LO + DN,
     "更严的星（实体<=0.1%、振幅>=3%）+ 低位缩量"),
    ("s_cluster", SANE + STARCL + DN, "连续 3 根以上小实体 + 缩量"),
    ("s_amp_mid", SANE + ["abs(body) <= 0.3", "between(amplitude, 2, 4)"],
     "振幅 2~4% 的小星"),
    ("s_amp_big", SANE + ["abs(body) <= 0.5", "amplitude >= 6"], "振幅>=6% 的大星"),
]


R3 = [
    ("base_all", [], "全市场基线"),
    ("L", SANE + STAR + LEG_DOWN + LO, "锚点：低位下影长腿星（上轮持20 超额 +2.10）"),
    ("L_up", SANE + STAR + LEG_DOWN + LO + UP, "锚点 + 放量>=1.5"),
    ("L_up2", SANE + STAR + LEG_DOWN + LO + UP2, "锚点 + 放量>=2.0"),
    ("L_mid", SANE + STAR + LEG_DOWN + LO + MID, "锚点 + 量能平"),
    ("L_dn", SANE + STAR + LEG_DOWN + LO + DN, "锚点 + 缩量<=0.7"),
    ("L_dn2", SANE + STAR + LEG_DOWN + LO + DN2, "锚点 + 缩量<=0.5"),
    ("L_leg2", SANE + ["abs(body) <= 0.3", "lower_shadow >= 2",
                       "upper_shadow <= 1"] + LO, "下影放宽到 >=2%"),
    ("L_leg5", SANE + ["abs(body) <= 0.3", "lower_shadow >= 5",
                       "upper_shadow <= 1"] + LO, "下影收紧到 >=5%"),
    ("L_legdom", SANE + ["abs(body) <= 0.3", "lower_shadow >= amplitude * 0.6",
                         "amplitude >= 3"] + LO, "下影占振幅 6 成以上"),
    ("L_lo40", SANE + STAR + LEG_DOWN + ["pos60 < 0.4"], "低位放宽到 pos60<0.4"),
    ("L_lo120", SANE + STAR + LEG_DOWN + ["pos120 < 0.35"], "改用 120 日低位"),
    ("L_oversold", SANE + STAR + LEG_DOWN + LO + ["ret20 < -0.05"], "锚点 + 近 20 日跌过 5%"),
    ("L_ampcap", SANE + STAR + LEG_DOWN + LO + ["amplitude <= 7"], "锚点 + 振幅<=7%"),
    ("L_diland", SANE + STAR + LEG_DOWN + LO + DILAND, "锚点 + 60 日地量"),
    ("L_sql", SANE + STAR + LEG_DOWN + LO + ["turnover_rate > 1", "amount > 100000"],
     "锚点 + 两条可 SQL 预筛的流动性"),
    ("L_bull20", SANE + STAR + LEG_DOWN + LO + ["close > ma20"], "锚点 + 仍在 20 日线上"),
    ("G", SANE + STAR + GAP_UP, "锚点：高开 2% 平走的星（上轮 +0.45）"),
    ("G_up", SANE + STAR + GAP_UP + UP, "高开星 + 放量"),
    ("G_dn", SANE + STAR + GAP_UP + DN, "高开星 + 缩量"),
    ("G_bull", SANE + STAR + GAP_UP + BULL, "高开星 + 均线多头"),
    ("G_hi", SANE + STAR + GAP_UP + HI, "高开星 + 60 日高位"),
    ("G_calm", SANE + STAR + GAP_UP + ["ret20 < 0.08"], "高开星 + 近 20 日没大涨"),
    ("D_lo", SANE + STAR + DN + LO + ["ret20 < -0.05"], "低位缩量星 + 刚跌过"),
    ("U_lo", SANE + STAR + UP + LO + ["ret20 < -0.05"], "低位放量星 + 刚跌过（对照）"),
    ("X_legup_lo", SANE + STAR + TOMB + LO, "对照：低位墓碑线"),
]


def main():
    dst = os.path.join(ROOT, "tools", "_trend_pool_lab.json")
    bak = dst + ".bak"
    if os.path.exists(dst):
        shutil.copy(dst, bak)
    try:
        cases = CASES
        for flag, alt in (("--r2", R2), ("--r3", R3)):
            if flag in sys.argv:
                sys.argv.remove(flag)
                cases = alt
                break
        lab.CASES = cases
        lab.main()
    finally:
        if os.path.exists(bak):
            shutil.copy(bak, dst)
            print(f"（已还原 {dst}）")


if __name__ == "__main__":
    main()
