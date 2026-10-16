# -*- coding: utf-8 -*-
"""技术指标计算。输入为按 trade_date 升序排好的 DataFrame（已复权）。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core import chips as _chips

# 默认指标参数（策略里可通过 config["indicators"] 覆盖）
DEFAULT_INDICATORS = {
    "ma": [5, 10, 20, 30, 60, 120, 250],
    "vma": [5, 10, 20],
    "boll": {"n": 20, "k": 2},
    "macd": {"fast": 12, "slow": 26, "signal": 9},
    "kdj": {"n": 9, "m1": 3, "m2": 3},
    "rsi": [6, 12, 24],
    "atr": 14,
    "cci": 14,
    "wr": 14,
    "mtm": 12,
    "dmi": 14,
}

# 供前端展示的字段说明
#
# ⚠ 这一份是「对外可用字段」的**唯一来源**（`/api/fields`、`/api/universe/fields`、
#   两个页面的「可用字段参考」、输入补全都从它派生）。所以每条里的名字必须写成
#   **能被 `" / "` 拆成真字段名**的形式 —— 以前写过 `("ma5…ma250", …)` 这种省略号，
#   拆出来的 `ma5…ma250` 过不了 `strategy.FIELD_RE`，于是 ma5/ma20/ma60、boll_dev、
#   ret60、pos120、ma_bull… **19 个真能用的字段在选股策略页的字段参考里查不到**
#   （2026-09-20 修：把省略号补成实名枚举）。
FIELD_HELP = [
    ("open / high / low / close", "开/高/低/收（复权后）"),
    ("pre_close", "昨收价（复权后）"),
    ("pct_chg", "当日涨跌幅 %"),
    ("change_amt", "涨跌额（元）"),
    ("vol", "成交量（手）"),
    ("amount", "成交额（千元）"),
    ("turnover_rate", "换手率 %（需同步 daily_basic）"),
    ("volume_ratio", "量比"),
    ("total_mv / circ_mv", "总市值 / 流通市值（万元）"),
    ("pe / pb / ps", "市盈率 / 市净率 / 市销率"),
    ("amplitude", "振幅 %  = (high-low)/pre_close*100"),
    ("avg_price", "均价 = amount/vol"),
    # ⚠ `body` 是**带符号**的：阳线为正、阴线为负 —— 别当「幅度」用。
    #   要"大阴"得写 `body <= -5`（配 `is_down = 1`），写 `body >= 5` 是"大阳"，
    #   与「昨日大阴」意图正好相反，会静默筛出 0 只（2026-09-19 踩过）。
    #   同族的 `amplitude` / `upper_shadow` / `lower_shadow` 才是恒非负的幅度。
    ("body", "实体 %（带符号，阳正阴负）= (close-open)/pre_close*100"),
    ("upper_shadow / lower_shadow", "上/下影线幅度 %"),
    ("gap_open", "跳空幅度 %  = (open-pre_close)/pre_close*100"),
    ("is_up / is_down", "收阳 / 收阴"),
    ("close_pos", "收盘价在当日振幅里的位置(0=最低,1=最高)"),
    ("limit_up / limit_down", "涨停 / 跌停（按板块 10%/20%/30% 判定）"),
    ("ma5 / ma10 / ma20 / ma30 / ma60 / ma120 / ma250", "N 日简单均价"),
    ("ma_bull", "均线多头排列（ma5>ma10>ma20，1/0）"),
    ("vma5 / vma10 / vma20", "N 日成交量均线"),
    ("boll_upper / boll_mid / boll_lower", "布林带上/中/下轨"),
    ("boll_width / boll_pos / boll_dev",
     "布林带宽 / 收盘价在带内位置(0=下轨,1=上轨) / 偏离中轨几个标准差"),
    ("macd_dif / macd_dea / macd_hist", "MACD DIF/DEA/柱"),
    ("kdj_k / kdj_d / kdj_j", "KDJ 三线"),
    ("rsi6 / rsi12 / rsi24", "RSI"),
    ("atr14", "真实波幅均值"),
    ("atr_pct", "ATR 占收盘价 %"),
    ("cci14", "顺势指标 CCI"),
    ("wr14", "威廉指标 WR（0~100，越大越弱）"),
    ("mtm12 / mtmpct12", "动量指标 / 动量 %"),
    ("obv / obv_ma10", "能量潮 / 能量潮的 10 日均线"),
    ("vr", "量比指标 VR"),
    ("dmi_pdi / dmi_mdi / dmi_adx", "DMI 趋向指标"),
    ("hh20 / ll20 / pos20", "20 日最高/最低/收盘位置(0~1)"),
    ("hh60 / ll60 / pos60", "60 日最高/最低/收盘位置"),
    ("hh120 / ll120 / pos120", "120 日最高/最低/收盘位置"),
    ("ret1 / ret5 / ret20 / ret60 / ret120", "1/5/20/60/120 日涨跌幅 %"),
    ("vol_ratio5 / vol_ratio10", "量能比 = vol / vma5（vma10）"),
    ("mf_main_net / mf_net",
     "资金流向(万元)：主力净额(大单+特大单)、净流入额(主动买-主动卖)"
     "（需同步资金流向 money_flow）"),
    # 大/特大/中/小单的**分单净额**（mf_lg_net mf_elg_net mf_md_net mf_sm_net）
    # 刻意不列 —— 见 MF_HIDDEN。它们照常算（主力净额就是前两个之和），
    # 只是不进「可用字段参考」与输入补全：拆到中单/小单这一层实际用不到。
    # ⚠ 想加回来就往这里补一行；回归 tools/verify_moneyflow.py 会盯着这份清单。
    ("mf_main_ratio / mf_net_ratio / mf_big_ratio",
     "资金流向占比 %：主力净额、净流入额、大+特大单成交额（都除以当日成交额）"),
    ("mf_main_net5 / mf_net5", "资金流向(万元)：5 日累计主力净额 / 5 日累计净流入额"),
    ("mf_main_ratio5 / mf_in5", "资金流向：5 日主力净额÷5 日成交额 % / 5 日内主力净流入天数(0~5)"),
]

# 筹码与两融（股东户数 / 融资融券 / 股东增减持）的字段表在 `core/chips.py` ——
# 那三类数据的读、派生、卡片、选股字段全在那一个模块里，这里是**唯一来源**。
# 并进 FIELD_HELP 是为了让 `/api/fields`、`/api/universe/fields`、策略页
# 「可用字段参考」与输入补全**自动带上它们**，不用在三个地方各维护一份
# （以前漏掉一次：新增字段只加了加载侧，字段清单没跟上 → 合法条件被判"未知字段"）。
FIELD_HELP = list(FIELD_HELP) + list(_chips.CHIP_FIELD_HELP)

# ---------------------------------------------------------------------------
# 选股结果表的列定义（**唯一来源**）
#
# 需求原话：「希望选股查出来的结果列表也要包含我查询条件相关的字段」。
# 做法：结果表按「策略条件/评分里真正引用到的字段」自动加列。表头、小数位、
# 数值格式、是否按涨跌上色**全部从这里出** —— 前端只按 fmt 调 `window.fmt`，
# 不写死任何字段名（同 `chart_catalog`、`monitor.html` 的 FLD 规矩）。
#
#   颜色角色：`"updown"` = 按正负上色（涨红跌绿）。留空 = 水平量，不拿方向着色
#             （拿增长率的方向给"户数"上色，会把"户数变多"涂成红色 —— 对象错位）。
#   数值格式：见 `static/js/app.js` 的 window.fmt
#             num(小数位) / big(元·千元自适应) / mv(万元→亿·万亿) / pct(带正号+%) / price(两位) / int(千分位)
#             ⚠ 市值（total_mv / circ_mv）只能用 `mv`：库里是**万元**，走 `big` 会永远落在
#               「万」档（22704900 万元 = 2270.49 亿，`big` 会写成「2270.49万」）。
#
# ⚠ 新增日线字段必须同时在这里补一行：`verify_chips` 有一段断言
#   「每个可用字段都有中文表头（不许退回英文字段名当列头）」，忘了补会红。
#   周线字段（w_ 前缀）不用另写：`col_spec()` 自动复用同名日线字段的定义、表头加"周"。
# ---------------------------------------------------------------------------
COL_SPEC: dict[str, tuple[str, int, str, str]] = {
    # 行情 / 价格
    "open": ("开盘", 2, "", "price"),
    "high": ("最高", 2, "", "price"),
    "low": ("最低", 2, "", "price"),
    "close": ("收盘", 2, "", "price"),
    "pre_close": ("昨收", 2, "", "price"),
    "change_amt": ("涨跌额", 2, "updown", "num"),
    "pct_chg": ("涨跌幅%", 2, "updown", "pct"),
    "amplitude": ("振幅%", 2, "updown", "pct"),
    "avg_price": ("均价", 2, "", "price"),
    "body": ("实体%", 2, "updown", "pct"),
    "upper_shadow": ("上影%", 2, "", "pct"),
    "lower_shadow": ("下影%", 2, "", "pct"),
    "gap_open": ("跳空%", 2, "updown", "pct"),
    "close_pos": ("收盘位置", 3, "", "num"),
    "is_up": ("收阳", 0, "", "int"),
    "is_down": ("收阴", 0, "", "int"),
    "limit_up": ("涨停", 0, "", "int"),
    "limit_down": ("跌停", 0, "", "int"),
    # 量能
    "vol": ("成交量手", 0, "", "big"),
    "amount": ("成交额千元", 2, "", "big"),
    "turnover_rate": ("换手%", 2, "", "pct"),
    "volume_ratio": ("量比", 2, "", "num"),
    "vol_ratio5": ("5日量能比", 2, "", "num"),
    "vol_ratio10": ("10日量能比", 2, "", "num"),
    "vma5": ("VMA5", 0, "", "big"),
    "vma10": ("VMA10", 0, "", "big"),
    "vma20": ("VMA20", 0, "", "big"),
    "vr": ("VR", 2, "", "num"),
    "obv": ("OBV", 0, "", "big"),
    "obv_ma10": ("OBV均线", 0, "", "big"),
    "ma_bull": ("均线多头", 0, "", "int"),
    # 估值（daily_basic）
    "pe": ("市盈率", 2, "", "num"),
    "pb": ("市净率", 2, "", "num"),
    "ps": ("市销率", 2, "", "num"),
    "total_mv": ("总市值亿", 2, "", "mv"),
    "circ_mv": ("流通市值亿", 2, "", "mv"),
    # 均线
    "ma5": ("MA5", 2, "", "num"),
    "ma10": ("MA10", 2, "", "num"),
    "ma20": ("MA20", 2, "", "num"),
    "ma30": ("MA30", 2, "", "num"),
    "ma60": ("MA60", 2, "", "num"),
    "ma120": ("MA120", 2, "", "num"),
    "ma250": ("MA250", 2, "", "num"),
    # 布林
    "boll_upper": ("布林上轨", 2, "", "num"),
    "boll_mid": ("布林中轨", 2, "", "num"),
    "boll_lower": ("布林下轨", 2, "", "num"),
    "boll_width": ("布林带宽%", 2, "", "pct"),
    "boll_pos": ("布林位置", 3, "", "num"),
    "boll_dev": ("布林偏离%", 2, "updown", "pct"),
    # 摆动指标
    "macd_dif": ("DIF", 3, "", "num"),
    "macd_dea": ("DEA", 3, "", "num"),
    "macd_hist": ("MACD柱", 3, "updown", "num"),
    "kdj_k": ("K", 2, "", "num"),
    "kdj_d": ("D", 2, "", "num"),
    "kdj_j": ("J", 2, "", "num"),
    "rsi6": ("RSI6", 2, "", "num"),
    "rsi12": ("RSI12", 2, "", "num"),
    "rsi24": ("RSI24", 2, "", "num"),
    "atr14": ("ATR14", 3, "", "num"),
    "atr_pct": ("ATR%", 2, "", "pct"),
    "cci14": ("CCI14", 2, "", "num"),
    "wr14": ("WR14", 2, "", "num"),
    "mtm12": ("MTM12", 2, "", "num"),
    "mtmpct12": ("MTM12%", 2, "updown", "pct"),
    "dmi_pdi": ("PDI", 2, "", "num"),
    "dmi_mdi": ("MDI", 2, "", "num"),
    "dmi_adx": ("ADX", 2, "", "num"),
    # 区间高低 / 位置 / 涨幅
    "hh20": ("20日最高", 2, "", "num"),
    "ll20": ("20日最低", 2, "", "num"),
    "pos20": ("20日位置", 3, "", "num"),
    "hh60": ("60日最高", 2, "", "num"),
    "ll60": ("60日最低", 2, "", "num"),
    "pos60": ("60日位置", 3, "", "num"),
    "hh120": ("120日最高", 2, "", "num"),
    "ll120": ("120日最低", 2, "", "num"),
    "pos120": ("120日位置", 3, "", "num"),
    "ret1": ("1日涨幅%", 2, "updown", "pct"),
    "ret5": ("5日涨幅%", 2, "updown", "pct"),
    "ret20": ("20日涨幅%", 2, "updown", "pct"),
    "ret60": ("60日涨幅%", 2, "updown", "pct"),
    "ret120": ("120日涨幅%", 2, "updown", "pct"),
    # 资金流向（万元 / %）—— 口径见本模块「个股资金流向」一节
    "mf_main_net": ("主力净额万", 2, "updown", "big"),
    "mf_net": ("净流入额万", 2, "updown", "big"),
    # 分单明细（大/特大/中/小单净额）不是可用条件字段（见 MF_HIDDEN），结果表永远不会
    # 出现这四列，表头定义一并去掉、不留死定义 —— 历史记录里无人引用（2026-09-19 全库扫过）。
    "mf_main_ratio": ("主力净额占比%", 2, "updown", "pct"),
    "mf_net_ratio": ("净流入占比%", 2, "updown", "pct"),
    "mf_big_ratio": ("大单成交占比%", 2, "", "pct"),
    "mf_main_net5": ("5日主力净额万", 2, "updown", "big"),
    "mf_net5": ("5日净流入万", 2, "updown", "big"),
    "mf_main_ratio5": ("5日主力占比%", 2, "updown", "pct"),
    "mf_in5": ("5日净流入天数", 0, "", "int"),
}
COL_SPEC.update(_chips.CHIP_COL_SPEC)


def col_spec(name: str) -> dict:
    """字段名 → 结果表列定义 `{key,label,dec,color,fmt}`。

    未知字段**不猜**：直接退回字段名当表头（`verify_chips` 有断言盯着，
    正常不会走到这里 —— 走到说明新加了字段忘了补 `COL_SPEC`）。
    周线字段自动复用同名日线定义、表头加「周」，不用在表里再写一遍。
    """
    src = COL_SPEC.get(name)
    label_pre = ""
    if src is None and name.startswith("w_"):
        src = COL_SPEC.get(name[2:])
        label_pre = "周"
    if src is None:
        return {"key": name, "label": name, "dec": 2, "color": "", "fmt": "num"}
    return {"key": name, "label": label_pre + src[0], "dec": src[1],
            "color": src[2], "fmt": src[3]}

# ---------------------------------------------------------------------------
# 个股资金流向（tushare moneyflow）
#
# 单位**与 K 线不同**，派生时统一在这里换算，别在别处再算一遍：
#   * 金额：moneyflow 用「万元」，daily_kline.amount 是「千元」（差 10 倍）
#   * 量：两边都是「手」（实测 600519 当天 mf 合计量 / kline vol = 1.0004）
# 口径实测（2026-09-11 全市场）：
#   * buy_*/sell_* 四档是**双边撮合口径**：全市场 sum(buy_*) == sum(sell_*)，
#     四档净额市场级互相抵消≈0 —— 所以「主力净流入」要用
#     (buy_lg+buy_elg) - (sell_lg+sell_elg) **自己算**。
#   * net_mf_amount 是另一套口径（主动买卖单边，市场级不归零），不能当主力净额用。
# ---------------------------------------------------------------------------

MF_RAW_COLS = [
    "buy_sm_vol", "buy_sm_amount", "sell_sm_vol", "sell_sm_amount",
    "buy_md_vol", "buy_md_amount", "sell_md_vol", "sell_md_amount",
    "buy_lg_vol", "buy_lg_amount", "sell_lg_vol", "sell_lg_amount",
    "buy_elg_vol", "buy_elg_amount", "sell_elg_vol", "sell_elg_amount",
    "net_mf_vol", "net_mf_amount",
]

# 由原始列派生的字段（**全部会算出来**，但下面 MF_HIDDEN 那几个不对外当条件字段）
MF_DERIVED = [
    "mf_main_net", "mf_net", "mf_lg_net", "mf_elg_net", "mf_md_net", "mf_sm_net",
    "mf_main_ratio", "mf_net_ratio", "mf_big_ratio",
    "mf_main_net5", "mf_net5", "mf_main_ratio5", "mf_in5",
]

# 「分单明细」四兄弟**不列入可用条件字段**（2026-09-19 用户要求：太细碎、用不到）：
# 想看资金方向，聚合口径的 `mf_main_net`（大+特大）/ `mf_net` / `mf_*_ratio` 已经够；
# 拆到中单、小单这一层只会让「可用字段参考」和输入补全变长。
# ⚠ 它们**仍然照常计算**：`mf_main_net = mf_lg_net + mf_elg_net`，删了会连带主力净额一起没。
#   所以这里只控制「对不对外」，不控制「算不算」—— 判定留给 FIELD_HELP（字段参考的唯一来源）。
MF_HIDDEN = ("mf_lg_net", "mf_elg_net", "mf_md_net", "mf_sm_net")

# 对外可用的资金流向字段（= MF_DERIVED 去掉分单明细），回归脚本按它断言清单
MF_AVAILABLE = [n for n in MF_DERIVED if n not in MF_HIDDEN]

MF_WINDOW = 5          # 累计/天数类字段的窗口


def needs_moneyflow(names) -> bool:
    """给定要算的字段集合（None = 全算），判断是否需要读 money_flow 原始列。

    加载侧（core/market.py）与计算侧共用这一条判定，避免两边口径不一致 ——
    漏读会得到全 NaN，多读只是慢。
    """
    if names is None:
        return True
    return any(str(n).startswith("mf_") for n in names)



def _limit_pct(ts_code: str, market: str | None = None, name: str = "") -> float:
    """按板块返回涨跌停比例。"""
    code = (ts_code or "")[:6]
    if code.startswith("688"):
        return 0.20
    if code.startswith("300") or code.startswith("301"):
        return 0.20
    if code.startswith(("8", "4", "9")):
        return 0.30
    if "ST" in (name or "").upper():
        return 0.05
    return 0.10


def _macd(close: pd.Series, fast=12, slow=26, signal=9):
    ef = close.ewm(span=fast, adjust=False).mean()
    es = close.ewm(span=slow, adjust=False).mean()
    dif = ef - es
    dea = dif.ewm(span=signal, adjust=False).mean()
    return dif, dea, (dif - dea) * 2


def _kdj(df: pd.DataFrame, n=9, m1=3, m2=3):
    low_n = df["low"].rolling(n, min_periods=1).min()
    high_n = df["high"].rolling(n, min_periods=1).max()
    rsv = (df["close"] - low_n) / (high_n - low_n).replace(0, np.nan) * 100
    rsv = rsv.fillna(50)
    k = rsv.ewm(alpha=1 / m1, adjust=False).mean()
    d = k.ewm(alpha=1 / m2, adjust=False).mean()
    return k, d, 3 * k - 2 * d


def _rsi(close: pd.Series, n: int):
    delta = close.diff()
    up = delta.clip(lower=0)
    down = (-delta).clip(lower=0)
    au = up.ewm(alpha=1 / n, adjust=False).mean()
    ad = down.ewm(alpha=1 / n, adjust=False).mean()
    rs = au / ad.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def _atr(df: pd.DataFrame, n=14):
    pc = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - pc).abs(),
        (df["low"] - pc).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def _cci(df: pd.DataFrame, n=14):
    tp = (df["high"] + df["low"] + df["close"]) / 3
    ma = tp.rolling(n, min_periods=1).mean()
    md = (tp - ma).abs().rolling(n, min_periods=1).mean()
    return (tp - ma) / (0.015 * md.replace(0, np.nan))


def _wr(df: pd.DataFrame, n=14):
    hh = df["high"].rolling(n, min_periods=1).max()
    ll = df["low"].rolling(n, min_periods=1).min()
    return (hh - df["close"]) / (hh - ll).replace(0, np.nan) * 100


def _dmi(df: pd.DataFrame, n=14):
    up = df["high"].diff()
    dn = -df["low"].diff()
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    pc = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - pc).abs(),
        (df["low"] - pc).abs(),
    ], axis=1).max(axis=1)
    atr = pd.Series(tr).ewm(alpha=1 / n, adjust=False).mean()
    pdi = pd.Series(pdm).ewm(alpha=1 / n, adjust=False).mean() / atr.replace(0, np.nan) * 100
    mdi = pd.Series(mdm).ewm(alpha=1 / n, adjust=False).mean() / atr.replace(0, np.nan) * 100
    dx = (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan) * 100
    return pdi, mdi, dx.ewm(alpha=1 / n, adjust=False).mean()


def compute(df: pd.DataFrame, indicators: dict | None = None,
            ts_code: str = "", name: str = "", only: set[str] | None = None) -> pd.DataFrame:
    """在 df 上追加全部指标列，返回新 DataFrame。df 必须含
    open/high/low/close/vol/amount 且按日期升序。

    only: 只需要计算的字段集合（None = 全算）。用于选股/回测时按策略实际引用
    的字段裁剪计算量，全市场扫描可提速数倍。
    """
    if df is None or df.empty:
        return df
    cfg = {**DEFAULT_INDICATORS, **(indicators or {})}
    df = df.copy().reset_index(drop=True)

    def want(*names) -> bool:
        if only is None:
            return True
        return any(n in only for n in names)

    def want_pre(prefix: str) -> bool:
        if only is None:
            return True
        return any(n.startswith(prefix) for n in only)

    for c in ("open", "high", "low", "close", "pre_close", "vol", "amount"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    if "pre_close" not in df.columns or df["pre_close"].isna().all():
        df["pre_close"] = df["close"].shift(1)

    close, high, low, op = df["close"], df["high"], df["low"], df["open"]
    pc = df["pre_close"].replace(0, np.nan)

    # ---- 形态派生字段（逐元素运算，成本极低，始终计算）----
    df["amplitude"] = (high - low) / pc * 100
    df["avg_price"] = (df["amount"] * 1000 / (df["vol"] * 100).replace(0, np.nan))
    df["body"] = (close - op) / pc * 100
    df["upper_shadow"] = (high - np.maximum(close, op)) / pc * 100
    df["lower_shadow"] = (np.minimum(close, op) - low) / pc * 100
    df["gap_open"] = (op - pc) / pc * 100
    df["is_up"] = (close > op).astype(int)
    df["is_down"] = (close < op).astype(int)
    df["close_pos"] = (close - low) / (high - low).replace(0, np.nan)
    df["ret1"] = close / close.shift(1) - 1

    for c in ("turnover_rate", "volume_ratio", "pe", "pb", "ps", "total_mv", "circ_mv"):
        if c not in df.columns:
            df[c] = np.nan
        else:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    # ---- 涨跌停（依赖板块，成本低）----
    if want("limit_up", "limit_down"):
        lim = _limit_pct(ts_code, name=name) * 100
        df["limit_up"] = ((close / pc - 1) * 100 >= lim - 0.15).astype(int)
        df["limit_down"] = ((close / pc - 1) * 100 <= -lim + 0.15).astype(int)

    # ---- 均线 ----
    need_ma_bull = want("ma_bull")
    for n in cfg.get("ma", []):
        if want(f"ma{n}") or (need_ma_bull and n in (5, 10, 20)):
            df[f"ma{n}"] = close.rolling(int(n), min_periods=1).mean()
    for n in cfg.get("vma", []):
        if want(f"vma{n}", f"vol_ratio{n}"):
            df[f"vma{n}"] = df["vol"].rolling(int(n), min_periods=1).mean()

    # ---- 布林带 ----
    if want_pre("boll"):
        b = cfg.get("boll") or {}
        bn, bk = int(b.get("n", 20)), float(b.get("k", 2))
        mid = close.rolling(bn, min_periods=bn).mean()
        sd = close.rolling(bn, min_periods=bn).std()
        df["boll_mid"] = mid
        df["boll_upper"] = mid + bk * sd
        df["boll_lower"] = mid - bk * sd
        width = (df["boll_upper"] - df["boll_lower"]).replace(0, np.nan)
        df["boll_width"] = width / mid.replace(0, np.nan) * 100
        df["boll_pos"] = (close - df["boll_lower"]) / width
        df["boll_dev"] = (close - mid) / (bk * sd.replace(0, np.nan))

    # ---- MACD ----
    if want_pre("macd"):
        m = cfg.get("macd") or {}
        dif, dea, hist = _macd(close, int(m.get("fast", 12)), int(m.get("slow", 26)),
                               int(m.get("signal", 9)))
        df["macd_dif"], df["macd_dea"], df["macd_hist"] = dif, dea, hist

    # ---- KDJ ----
    if want_pre("kdj"):
        kc = cfg.get("kdj") or {}
        k, d, j = _kdj(df, int(kc.get("n", 9)), int(kc.get("m1", 3)), int(kc.get("m2", 3)))
        df["kdj_k"], df["kdj_d"], df["kdj_j"] = k, d, j

    # ---- RSI ----
    for n in cfg.get("rsi", []):
        if want(f"rsi{n}"):
            df[f"rsi{n}"] = _rsi(close, int(n))

    # ---- ATR ----
    an = int(cfg.get("atr", 14))
    if want(f"atr{an}", "atr_pct"):
        df[f"atr{an}"] = _atr(df, an)
        df["atr_pct"] = df[f"atr{an}"] / close.replace(0, np.nan) * 100

    # ---- CCI / WR / MTM ----
    cn = int(cfg.get("cci", 14))
    if want(f"cci{cn}"):
        df[f"cci{cn}"] = _cci(df, cn)
    wn = int(cfg.get("wr", 14))
    if want(f"wr{wn}"):
        df[f"wr{wn}"] = _wr(df, wn)
    mn = int(cfg.get("mtm", 12))
    if want(f"mtm{mn}", f"mtmpct{mn}"):
        df[f"mtm{mn}"] = close.diff(mn)
        df[f"mtmpct{mn}"] = close / close.shift(mn) - 1

    # ---- OBV / VR ----
    if want("obv", "obv_ma10", "vr"):
        sign = np.sign(close.diff()).fillna(0)
        if want("obv", "obv_ma10"):
            df["obv"] = (sign * df["vol"]).cumsum()
            df["obv_ma10"] = df["obv"].rolling(10, min_periods=1).mean()
        if want("vr"):
            up_v = df["vol"].where(close > pc, 0.0).rolling(26, min_periods=5).sum()
            dn_v = df["vol"].where(close < pc, 0.0).rolling(26, min_periods=5).sum()
            df["vr"] = up_v / dn_v.replace(0, np.nan) * 100

    # ---- DMI ----
    if want_pre("dmi"):
        dn_ = int(cfg.get("dmi", 14))
        pdi, mdi, adx = _dmi(df, dn_)
        df["dmi_pdi"], df["dmi_mdi"], df["dmi_adx"] = pdi, mdi, adx

    # ---- 区间位置与区间涨幅 ----
    for n in (20, 60, 120):
        if want(f"hh{n}", f"ll{n}", f"pos{n}", f"ret{n}"):
            hh = high.rolling(n, min_periods=1).max()
            ll = low.rolling(n, min_periods=1).min()
            if want(f"hh{n}", f"pos{n}"):
                df[f"hh{n}"] = hh
            if want(f"ll{n}", f"pos{n}"):
                df[f"ll{n}"] = ll
            if want(f"pos{n}"):
                df[f"pos{n}"] = (close - ll) / (hh - ll).replace(0, np.nan)
            if want(f"ret{n}"):
                df[f"ret{n}"] = close / close.shift(n) - 1

    for n in (5, 20):
        if want(f"ret{n}") and f"ret{n}" not in df.columns:
            df[f"ret{n}"] = close / close.shift(n) - 1
    for n in (5, 10):
        col = f"vol_ratio{n}"
        if want(col) and f"vma{n}" in df.columns:
            df[col] = df["vol"] / df[f"vma{n}"].replace(0, np.nan)
    if want("ma_bull") and all(f"ma{n}" in df.columns for n in (5, 10, 20)):
        df["ma_bull"] = ((df["ma5"] >= df["ma10"]) & (df["ma10"] >= df["ma20"])).astype(int)

    # ---- 个股资金流向派生（只有策略/评分真的引用 mf_* 时才算）----
    if needs_moneyflow(only):
        for c in MF_RAW_COLS:
            df[c] = (pd.to_numeric(df[c], errors="coerce") if c in df.columns
                     else np.nan)
        amt_wan = (pd.to_numeric(df["amount"], errors="coerce") / 10.0
                   if "amount" in df.columns else pd.Series(np.nan, index=df.index))
        buy_lg, buy_elg = df["buy_lg_amount"], df["buy_elg_amount"]
        sell_lg, sell_elg = df["sell_lg_amount"], df["sell_elg_amount"]
        df["mf_sm_net"] = df["buy_sm_amount"] - df["sell_sm_amount"]
        df["mf_md_net"] = df["buy_md_amount"] - df["sell_md_amount"]
        df["mf_lg_net"] = buy_lg - sell_lg
        df["mf_elg_net"] = buy_elg - sell_elg
        df["mf_main_net"] = df["mf_lg_net"] + df["mf_elg_net"]
        df["mf_net"] = df["net_mf_amount"]
        with np.errstate(invalid="ignore", divide="ignore"):
            base = amt_wan.replace(0, np.nan)
            df["mf_main_ratio"] = df["mf_main_net"] / base * 100
            df["mf_net_ratio"] = df["mf_net"] / base * 100
            df["mf_big_ratio"] = (buy_lg + buy_elg + sell_lg + sell_elg) / base * 100
            win = MF_WINDOW
            # min_periods=1：开头不足窗口时给部分累计，别让整段变 NaN
            # （与项目其它滚动指标口径一致，否则条件恒 False）
            r5 = df["mf_main_net"].rolling(win, min_periods=1).sum()
            df["mf_main_net5"] = r5
            df["mf_net5"] = df["mf_net"].rolling(win, min_periods=1).sum()
            amt5 = amt_wan.rolling(win, min_periods=1).sum().replace(0, np.nan)
            df["mf_main_ratio5"] = r5 / amt5 * 100
        inflow = (df["mf_main_net"] > 0).where(df["mf_main_net"].notna())
        df["mf_in5"] = inflow.astype(float).rolling(win, min_periods=1).sum()

    # 日期与代码字段（非数值）
    df["_ts_code"] = ts_code or df.get("ts_code", pd.Series([""] * len(df)))
    df["_name"] = name
    return df


def variable_names(df: pd.DataFrame) -> dict[str, str]:
    """返回可用于表达式的变量名 -> 中文说明。"""
    names: dict[str, str] = {}
    for f, d in FIELD_HELP:
        for part in f.split(" / "):
            names[part.strip()] = d
    for c in df.columns:
        if c.startswith("_"):
            continue
        names.setdefault(c, "数据列")
    return names


# ---------------------------------------------------------------------------
# 「个股诊断」K线下方可切换的下挂指标目录
#
# 这是**唯一来源**：选项名（含实际参数）、画哪些字段、每根线是 line 还是 bar、
# 柱子按什么着色、数值按什么单位格式化 —— 全部从这里出，前端只负责上色。
# 前端要是再抄一份字段表，策略把 cci 调成 20 之后就会去画一个不存在的 cci14
# 而「静默画空线」，所以这里跟着 compute() 的同一份 merge 逻辑走参数。
#
# 每项字段：
#   key     —— 稳定标识，前端用它选配色
#   name    —— 下拉框里显示的名字，已带上真实参数（如 MACD(12,26,9)）
#   series  —— [{f: 字段名, label: 图例名, type: line|bar,
#                role: '' | 'updown' | 'updown_open', fmt: 数值格式 token}]
#   band    —— [min, max] 固定 y 轴范围（只有真有界的指标才给，别硬夹 KDJ 的 J）
#   guides  —— y 轴上要画的参考水平线（0 轴 / 20·80 超买超卖 / 100 等）
#   needs   —— 依赖的额外数据源（'money_flow'），缺了整组是空的，前端要提示
#
# fmt token（前端 fmt 映射，验证脚本会断言两边集合一致）：
#   plain 普通小数 · price 价格 · vol 手 · count 大整数(千分位) · money_wan 万元
# ---------------------------------------------------------------------------

CHART_INDICATOR_KEYS = ("macd", "kdj", "rsi", "dmi", "cci", "wr", "obv", "vr",
                        "atr", "vol", "mf")


def chart_catalog(indicators: dict | None = None) -> list[dict]:
    """下挂指标目录（顺序即下拉框顺序）。参数取自策略的 indicators 配置。"""
    c = {**DEFAULT_INDICATORS, **(indicators or {})}

    def num(key, default):
        try:
            return int(c.get(key, default))
        except (TypeError, ValueError):
            return int(default)

    def dct(key, defaults):
        v = c.get(key)
        return {**defaults, **(v if isinstance(v, dict) else {})}

    def line(f, label, fmt="plain"):
        return {"f": f, "label": label, "type": "line", "role": "", "fmt": fmt}

    def bar(f, label, role="updown", fmt="plain"):
        return {"f": f, "label": label, "type": "bar", "role": role, "fmt": fmt}

    out: list[dict] = []

    m = dct("macd", {"fast": 12, "slow": 26, "signal": 9})
    out.append({
        "key": "macd",
        "name": "MACD(%d,%d,%d)" % (int(m["fast"]), int(m["slow"]), int(m["signal"])),
        "series": [line("macd_dif", "DIF"), line("macd_dea", "DEA"),
                   bar("macd_hist", "MACD")],
        "guides": [0],
    })

    k = dct("kdj", {"n": 9, "m1": 3, "m2": 3})
    out.append({
        "key": "kdj",
        "name": "KDJ(%d,%d,%d)" % (int(k["n"]), int(k["m1"]), int(k["m2"])),
        "series": [line("kdj_k", "K"), line("kdj_d", "D"), line("kdj_j", "J")],
        # J 会冲出 0~100，别夹
        "guides": [20, 80],
    })

    rs = [x for x in (c.get("rsi") or [])]
    if rs:
        rs = [int(x) for x in rs]
        out.append({"key": "rsi", "name": "RSI(" + ",".join(str(x) for x in rs) + ")",
                    "series": [line(f"rsi{x}", f"RSI{x}") for x in rs],
                    "band": [0, 100], "guides": [30, 70]})

    dn = num("dmi", 14)
    out.append({"key": "dmi", "name": f"DMI({dn})",
                "series": [line("dmi_pdi", "PDI"), line("dmi_mdi", "MDI"),
                           line("dmi_adx", "ADX")]})

    cn = num("cci", 14)
    out.append({"key": "cci", "name": f"CCI({cn})",
                "series": [line(f"cci{cn}", f"CCI{cn}")], "guides": [100, -100]})

    wn = num("wr", 14)
    out.append({"key": "wr", "name": f"WR({wn})",
                "series": [line(f"wr{wn}", f"WR{wn}")],
                "band": [0, 100], "guides": [20, 80]})

    out.append({"key": "obv", "name": "OBV(10)",
                "series": [line("obv", "OBV", "count"),
                           line("obv_ma10", "OBV_MA10", "count")],
                "guides": [0]})

    out.append({"key": "vr", "name": "VR(26)",
                "series": [line("vr", "VR")], "guides": [100]})

    an = num("atr", 14)
    out.append({"key": "atr", "name": f"ATR({an})",
                "series": [line(f"atr{an}", f"ATR{an}", "price")]})

    # 成交量：量柱按当日阴阳着色（收阳红 / 收阴绿），不是按数值正负
    vmas = [int(x) for x in (c.get("vma") or []) if int(x) in (5, 10)]
    out.append({"key": "vol", "name": "成交量 + 量均线",
                "series": [bar("vol", "成交量", "updown_open", "vol")]
                          + [line(f"vma{x}", f"VMA{x}", "vol") for x in vmas]})

    out.append({"key": "mf", "name": "主力资金(万元)", "needs": "money_flow",
                "series": [bar("mf_main_net", "主力净额", "updown", "money_wan"),
                           bar("mf_net", "净流入额", "updown", "money_wan")],
                "guides": [0]})

    return out


def chart_catalog_fields(catalog: list[dict]) -> list[str]:
    """目录里出现的字段名（去重，保持顺序）——用于裁剪返回的列。"""
    seen: list[str] = []
    for g in catalog:
        for s in g.get("series") or []:
            if s["f"] not in seen:
                seen.append(s["f"])
    return seen


def mark_catalog_empty(catalog: list[dict], df) -> list[dict]:
    """给目录里「本地压根没数据」的组打上 `empty`。

    前端据此把它标成「（无数据）」并跳过，而不是**静默画一条空线** ——
    空线看着像「该指标恒为 0」，是句假话（典型：没同步过资金流向时的「主力资金」）。

    判据两条：要画的字段一个都不在 df 里，或字段都在但**整列全 NaN**。

    **个股诊断与交易复盘共用这一份**（两处都在画下挂指标格）——
    各写一份的话，早晚会出现「诊断页说没数据、复盘页却画了条空线」。
    """
    for g in catalog:
        present = [s["f"] for s in (g.get("series") or []) if s["f"] in df.columns]
        g["empty"] = (not present) or bool(df[present].isna().all().all())
    return catalog


# ---------------------------------------------------------------------------
# 个股诊断页的「基本面」卡片
#
# 展示口径（**只在这里算一次**，前端不自己除一万、也不自己挑字段）：
#   * `total_mv` / `circ_mv` 库里单位是**万元**（500000 万元 = 50 亿），展示换成「亿 / 万亿」；
#     上万亿还用亿会写成「15726.00亿」，所以超过 1 万亿换单位。
#   * `pe` / `pb` / `ps` 是倍数，`turnover_rate` 是百分数（3.21 即 3.21%）。
#   * 缺数据一律给 `—`，**不要**拿更早一天的值来顶（那会让人以为是最新的）。
# ---------------------------------------------------------------------------

FUNDAMENTALS: list[tuple[str, str, str]] = [
    # (字段, 卡片上的标签, 格式 token)
    ("pe", "PE", "num"),
    ("pb", "PB", "num"),
    ("ps", "PS", "num"),
    ("total_mv", "总市值", "mv"),
    ("circ_mv", "流通市值", "mv"),
    ("turnover_rate", "换手率", "pct"),
]


def _is_num(v) -> bool:
    """能当数字用吗（None / NaN / 空串都算缺）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return f == f          # NaN 自己不等于自己


def _mv_text(wan) -> str:
    """市值：入参单位**万元** → 「亿 / 万亿」。"""
    yi = float(wan) / 1e4              # 万元 → 亿元
    if abs(yi) >= 1e4:
        return "%.2f万亿" % (yi / 1e4)
    return "%.2f亿" % yi


def fundamental_snapshot(row: dict, as_of: str = "") -> dict:
    """把一根 K 线上的基本面字段整理成卡片可直接渲染的 `items`（顺序即展示顺序）。"""
    items = []
    for f, label, fmt in FUNDAMENTALS:
        v = row.get(f)
        if not _is_num(v):
            text = "—"
        elif fmt == "mv":
            text = _mv_text(v)
        elif fmt == "pct":
            text = "%.2f%%" % float(v)
        else:
            text = "%.2f" % float(v)
        items.append({"key": f, "label": label, "text": text})
    return {"as_of": as_of or "", "items": items}
