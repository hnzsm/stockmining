# -*- coding: utf-8 -*-
"""次日开盘形态概率统计（个股诊断页「开盘概率」卡片）。

统计四项**历史频率**（不是预测，只是「这只票过去这么走过多大比例」）：

  1. **高开 3%** —— 开盘相对昨收**涨 ≥ +3%** 的日子占多少（**一字涨停不算**，见下）；
  2. **低开 3%** —— 开盘相对昨收**跌 ≥ 3%**（≤ −3%）的日子占多少（**一字跌停不算**）；
  3. **低开红柱** —— 低开 3% 的那些天里，**当日收阳**（收盘 > 开盘，日K画成红柱）；
  4. **高开绿柱** —— 高开 3% 的那些天里，**当日收阴**（收盘 < 开盘，日K画成绿柱）。

口径（**只在这里定义一次**，前端不自己算）：

  · **开盘涨跌幅 = 今开 / 昨收 − 1**，判据「≥ 阈值」（含边界，带 EPS 容差，见上）。
    用 `pct_chg`（收盘涨跌幅）或「今开/今收」都是错的 —— 高开低开讲的是
    **集合竞价那一跳**，跟当天怎么收无关。
  · 昨收取 `pre_close` 那一列（**复权口径**，除权日不会被算成假高开）。
    ⚠ 面板的 `pre_close` 是裁剪前算好的（见 market._weekly / load_panel），
    所以这里直接用，不自己 `shift(1)` —— 自己 shift 会把停牌后复牌那天算错。
  · 分母 = 区间内**全部有效交易日**（`open`/`pre_close` 都>0 的那几根）。
    四项各自的机会数也会回传（`n_*`），前端可以摆在备注里；四项概率**不要求加和为 1**
    ——那四个事件不是互斥的划分（一只票当天可能既不高开也不低开）。
  · 「低开红柱」「高开绿柱」这两项**是条件概率**：分母用「低开3%的天数」/「高开3%的天数」，
    不是全部交易日 —— 否则「红柱概率」会被「有多少天低开」稀释掉，读者会以为那是
    「随便哪天收阳的概率」。两个分母都随 `n_*` 一起回传，免得被误读。
  · **一字板不算跳空形态**（2026-10-10）：全天只有一个价（`high == low`）就按方向分 ——
    收涨 = 一字涨停 ⇒ **不计入「高开」**；收跌 = 一字跌停 ⇒ **不计入「低开」**。
    这一整天**仍算在分母 `days` 里**（它确实是有效交易日），只是不作为跳空事件计数。
    理由：一字板一整天都挂不上单（买不到 / 卖不掉），当成「集合竞价那一跳」会把
    概率虚高；跌停那条还会顺带稀释「低开红柱」（一字跌停不可能收阳）。

缺数据（本地没同步 / 区间太短）一律给 `—`，不拿更早的值顶（与基本面、筹码同一规矩）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# 高开 / 低开的阈值（%）。**唯一来源** —— 前端文案里的「3%」也从这里带到页面上，
# 别在模板里再写一个 3（改阈值时两边分岔）。
OPEN_PCT = 3.0

# 前端可调的阈值范围（%）。**唯一来源** —— 页面上的输入框 min/max 也从这里带过去，
# 免得模板里再写一个 0.1 / 20（改范围时两边分岔）。
#   · 下限 0.1：0 会让「任何一天都算高开或低开」，四格全 100%，没意义；
#   · 上限 20：A 股主板单日跳空极少超 20%，再大的阈值在多数票上恒为 0 次。
OPEN_PCT_MIN = 0.1
OPEN_PCT_MAX = 20.0


def clamp_threshold(v, default: float = OPEN_PCT) -> float:
    """把用户传的阈值收进 [MIN, MAX]，并**四舍五入到 0.1**。

    这是阈值入参的**唯一解析点**（接口、前端回显都走它）：
      · 非法（None / 空串 / NaN / 非数字）→ 返回 default（不报错，退回默认口径）；
      · 越界 → 夹到边界；
      · 统一保留 1 位小数 —— 浮点算出来的 2.9999999996 显示成「3%」而不是「3.0%」以外
        的怪串，也免得「输入 3 但内部是 2.9999」导致边界那几根判据漂移。
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return float(default)
    if f != f:                       # NaN
        return float(default)
    f = max(OPEN_PCT_MIN, min(OPEN_PCT_MAX, f))
    return round(f, 1)


def gap_pct(open_, pre_close_):
    """开盘涨跌幅 %（今开 / 昨收 − 1）× 100。**唯一定义**。

    单根或多根都行（标量 → float，数组 → ndarray）。前端 JS 侧那份
    （见 `front_spec()` 下发的 `gap_expr`）与这里**同一条式子** ——
    两处都别自己另写一个算法（「今开/今收」「pct_chg」都是错的，见文件头）。
    """
    o = np.asarray(open_, dtype="float64")
    pc = np.asarray(pre_close_, dtype="float64")
    with np.errstate(divide="ignore", invalid="ignore"):
        g = (o / pc - 1.0) * 100.0
    if g.ndim == 0:
        v = float(g)
        return v if np.isfinite(v) else None
    return g


def is_high_open(gap, threshold: float = OPEN_PCT) -> bool:
    """高开判定：gap ≥ 阈值（含边界，带 EPS 容差，见文件头）。"""
    try:
        g = float(gap)
    except (TypeError, ValueError):
        return False
    if g != g:
        return False
    return g >= float(threshold) - EPS


def is_low_open(gap, threshold: float = OPEN_PCT) -> bool:
    """低开判定：gap ≤ −阈值（含边界，带 EPS 容差，见文件头）。"""
    try:
        g = float(gap)
    except (TypeError, ValueError):
        return False
    if g != g:
        return False
    return g <= -float(threshold) + EPS

# 边界容差（百分点）。**为什么需要它**：判据是「开盘涨跌幅 ≥ 3%」，
# 但 `(103.0/100.0 - 1)*100 = 3.0000000000000027` —— 二进制浮点下
# 「数学上恰好 3.0%」会算出略大于 3 的值，于是同一根 K 线在不同机器/不同
# 运算次序下可能落进或落出。真实数据里「恰好跳空 3%」的有 180 余根
# （整数百分比跳空在 A 股很常见），不是可以忽略的噪声。
# 加一个远小于任何有效价格精度（0.01 元）的容差，让它稳定归入「达到阈值」：
#   gap >= thr - EPS   ⇔   数学上的 gap >= 3.0（含边界）
EPS = 1e-9

# ---- 一字板：不算跳空形态（2026-10-10）-------------------------------------
# 用户原话：「如果高开一字涨停不算做高开」；用户确认对称处理 ⇒ 一字跌停也不算低开。
#
# 判据 = **全天只有一个价**（`high == low` ⇒ 一整天都挂不上单），再按**相对昨收的方向**分：
#         收涨 ⇒ 一字涨停（不计入「高开」）；收跌 ⇒ 一字跌停（不计入「低开」）。
#
# ⚠⚠ **故意不判「是不是封在涨跌停价上」**，原因是有个真实的坑：
#    项目里 10%/20%/30%/5% 那套涨跌停口径（`indicators._limit_pct`）是按**当前股票名**
#    判 ST 的（面板的 `is_st` 列同理）—— 一只已经摘帽的票，它当年 5% 的一字板会被
#    按 10% 去判，**整批漏掉**。实测 `000908.SZ`（现名「石药景峰」）：区间内有
#    **49 天** `high == low` 且收涨（全是 +5% 的 ST 一字板），若用 `limit_up` 列判，
#    **一天都认不出来**（高开 107 天一个没剔）。按方向分则 49 天全部命中。
#    实务上「全天只有一个价」本身就是封板，方向已经说明了是涨板还是跌板。
#
# ⚠ 必需列 = `high` / `low` / `close` / `pre_close`（`openstat` 两个调用方都是
#   `load_panel(only=None)`，都在）。真缺了就**不剔除**并把 `applied=False` 回传，
#   页面照实说 —— 不静默假装排过（`limit_up`/`limit_down` 只在有需要时才读，非必需）。
#
# 「一个价」的容差：半个最小报价单位（0.005 元）。真实数据里一字板 high 与 low
# 是同一个数；给半分钱只是防浮点/数据源末位噪声，远不足以把「打开过的涨停」误判成一字。
ONE_WORD_TOL = 0.005


def one_word_mask(df: pd.DataFrame, ok=None):
    """返回 `(yz_up, yz_dn, applied)`：一字涨停 / 一字跌停的布尔掩码（长度为 df 行数）。

    `ok` 是「有效交易日」掩码时，返回值与之等长对齐（不在 ok 里的位置恒 False）。
    `applied=False` 表示**必需列不全，没做排除**（调用方据此把这件事说清楚，而不是装作排过）。
    """

    n = len(df)
    zero = np.zeros(n, dtype=bool)
    if df is None or not n:
        return zero, zero, False
    cols = {"high", "low", "close", "pre_close"}
    if not cols.issubset(set(df.columns)):
        return zero, zero, False

    h = pd.to_numeric(df["high"], errors="coerce").to_numpy(dtype="float64")
    l = pd.to_numeric(df["low"], errors="coerce").to_numpy(dtype="float64")
    c = pd.to_numeric(df["close"], errors="coerce").to_numpy(dtype="float64")
    pc = pd.to_numeric(df["pre_close"], errors="coerce").to_numpy(dtype="float64")

    # 全天一个价（含半分钱容差）；方向按**相对昨收**判 —— 见上面「故意不判涨跌停价」的说明。
    flat = (np.isfinite(h) & np.isfinite(l) & np.isfinite(c) & np.isfinite(pc)
            & ((h - l) <= ONE_WORD_TOL))
    yz_up = flat & (c > pc)
    yz_dn = flat & (c < pc)
    if ok is not None:
        keep = np.asarray(ok, dtype=bool)
        yz_up = yz_up & keep
        yz_dn = yz_dn & keep
    return yz_up, yz_dn, True

# 四项的展示顺序与 key（前端按 key 上色，不认中文标签）
#   (前端 key, 卡片标签, 概率字段, 次数字段, 固定色)
ITEMS = [
    ("high_open", "高开3%", "p_high", "n_high", "up"),
    ("low_open", "低开3%", "p_low", "n_low", "down"),
    ("low_open_red", "低开红柱", "p_low_red", "n_low_red", "up"),
    ("high_open_green", "高开绿柱", "p_high_green", "n_high_green", "down"),
]


def _is_num(v) -> bool:
    """能当数字用吗（None / NaN / 空串都算缺）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return f == f              # NaN 自己不等于自己


def _num(v):
    return float(v) if _is_num(v) else None


def _pct_text(v) -> str:
    return "—" if not _is_num(v) else "%.1f%%" % float(v)


def _cnt_text(v) -> str:
    return "—" if not _is_num(v) else "%d" % int(v)


def stats_from_frame(df: pd.DataFrame, threshold: float = OPEN_PCT) -> dict:
    """从**已含 open / pre_close / close 的 DataFrame** 算四项频次。

    这是唯一的口径实现 —— 接口取数（`open_stat`）与回归对拍都走这里，
    免得「页面上的数」与「验证脚本算的数」是两套算法。
    """
    empty = {"days": 0, "n_high": 0, "n_low": 0, "n_high_green": 0,
             "n_low_red": 0, "p_high": None, "p_low": None,
             "p_high_green": None, "p_low_red": None, "threshold": threshold,
             "n_yz_up": 0, "n_yz_dn": 0, "yizi_applied": False,
             "range": {"start": "", "end": ""}, "bars": []}
    if df is None or not len(df):
        return empty
    need = [c for c in ("open", "pre_close", "close") if c in df.columns]
    if len(need) < 3:
        return empty

    o = pd.to_numeric(df["open"], errors="coerce").to_numpy(dtype="float64")
    pc = pd.to_numeric(df["pre_close"], errors="coerce").to_numpy(dtype="float64")
    c = pd.to_numeric(df["close"], errors="coerce").to_numpy(dtype="float64")

    # 有效交易日：昨收与开盘都得是正数（停牌/退市的 0 或 NaN 不算样本）
    ok = np.isfinite(o) & np.isfinite(pc) & np.isfinite(c) & (o > 0) & (pc > 0)
    if not ok.any():
        return empty

    # 开盘涨跌幅 %（今开相对昨收）—— 正=高开，负=低开。**走唯一口径函数**，
    # 不在本函数里另写一遍 `(o/pc-1)*100`（那样前端要复用时就找不到源头了）。
    gap = gap_pct(o[ok], pc[ok])
    co = c[ok]
    oo = o[ok]

    thr = float(threshold)
    # 「达到」阈值：含边界（≥3%），并带 EPS 容差抵消浮点误差（见文件头 EPS 说明）。
    # ⚠ 判据同样走唯一口径函数（`is_high_open` / `is_low_open`），别在这里手写比较 ——
    #   前端拿阈值即时重算时用的是同一条式子，两处必须同源。
    up = np.array([is_high_open(g, thr) for g in np.atleast_1d(gap)])
    dn = np.array([is_low_open(g, thr) for g in np.atleast_1d(gap)])
    # 一字板不算跳空形态（2026-10-10，见 one_word_mask）：一字涨停不计入高开、
    # 一字跌停不计入低开。**只从分子里剔掉，不剔分母**（那一天仍是有效交易日）。
    # ⚠ 掩码要在**取 ok 之后**再切一刀，否则与 gap/co/oo 那几段的下标对不上。
    _yz_up, _yz_dn, yz_applied = one_word_mask(df, ok)
    yz_up, yz_dn = _yz_up[ok], _yz_dn[ok]
    up = up & ~yz_up
    dn = dn & ~yz_dn
    # 当日阴阳：收盘 > 开盘 = 阳线（红柱）；收盘 < 开盘 = 阴线（绿柱）
    yang = co > oo
    yin = co < oo

    days = int(ok.sum())
    n_high = int(up.sum())
    n_low = int(dn.sum())
    n_high_green = int((up & yin).sum())     # 高开 3% 且收阴
    n_low_red = int((dn & yang).sum())       # 低开 3% 且收阳
    # 被剔掉的一字板天数（只在「不看阈值」这一层有意义：一字板与阈值无关）
    n_yz_up = int(yz_up.sum())
    n_yz_dn = int(yz_dn.sum())

    def rate(num, den):
        return (num / den * 100.0) if den else None

    dts = df.loc[ok, "trade_date"] if "trade_date" in df.columns else None
    rng = {"start": "", "end": ""}
    if dts is not None and len(dts):
        rng = {"start": str(dts.iloc[0])[:10], "end": str(dts.iloc[-1])[:10]}

    # 逐根的「开盘涨跌幅 + 是否收阳」—— 供页面**改阈值时即时重算**用。
    # ⚠ 只下发**原料**（gap / is_yang），不下发判定结果：阈值一变，高开/低开的
    #   判定就得跟着变，但「gap 怎么算」「什么叫收阳」是**口径**、不随阈值变。
    #   前端拿到这里的两列后只做一次 `>= thr - EPS` 比较（EPS 同时下发），
    #   于是「页面上的数」与「后端算的数」永远是同一段原料、同一条判据。
    #   `idx` = 这一根在**原 df 里的行号**，页面据此把标记对回 K 线那一根
    #   （有效交易日会跳过停牌/缺数据那几根，不能用连续序号）。
    idx = np.nonzero(ok)[0]
    gaps = [None if not np.isfinite(float(g)) else round(float(g), 6)
            for g in np.atleast_1d(gap)]
    yang_l = [bool(v) for v in np.atleast_1d(yang)]
    yin_l = [bool(v) for v in np.atleast_1d(yin)]
    # 一字涨停 / 一字跌停的**逐根标记**：前端即时重算时不重判 `high==low`，
    # 只按这两个标记把这一根从高开/低开里剔掉（判据仍在后端，一份口径）。
    yzup_l = [bool(v) for v in np.atleast_1d(yz_up)]
    yzdn_l = [bool(v) for v in np.atleast_1d(yz_dn)]

    return {
        "days": days,
        "n_high": n_high, "n_low": n_low,
        "n_high_green": n_high_green, "n_low_red": n_low_red,
        "n_yz_up": n_yz_up, "n_yz_dn": n_yz_dn, "yizi_applied": bool(yz_applied),
        "p_high": rate(n_high, days),
        "p_low": rate(n_low, days),
        # 两项条件概率：分母是各自的开盘形态天数（不是全部交易日）
        "p_high_green": rate(n_high_green, n_high),
        "p_low_red": rate(n_low_red, n_low),
        "threshold": thr,
        "range": rng,
        # 即时重算用的原料（前端只做阈值比较 + 按 yz 标记剔除）
        "bars": [{"idx": int(i), "gap": g, "yang": y, "yin": n, "yz_up": u, "yz_dn": d}
                 for i, g, y, n, u, d in zip(idx, gaps, yang_l, yin_l, yzup_l, yzdn_l)],
    }


def snapshot(stat: dict) -> dict:
    """把频次整理成卡片可直接渲染的 `items`（文本、色 class、次数全算好）。

    与基本面 / 筹码 / 策略收益卡片同一套做法：前端只铺格子。

    额外下发 `adjust`（阈值可调范围 + 判据常量 + 逐根原料）—— 页面改阈值时
    用它**即时重算**四格概率与图上标记，不必再打一次接口。见 `recalc_from_bars`。
    """
    items = []
    for key, label, pf, nf, cls in ITEMS:
        items.append({
            "key": key,
            "label": label,
            "text": _pct_text(stat[pf]),
            "value": stat[pf],
            "cls": cls,                        # 固定色（高开/红柱=红，低开/绿柱=绿）
            "count": stat[nf],
            "count_text": _cnt_text(stat[nf]),
        })
    thr = float(stat.get("threshold") or OPEN_PCT)
    n_yz_up = int(stat.get("n_yz_up") or 0)
    n_yz_dn = int(stat.get("n_yz_dn") or 0)
    yz_applied = bool(stat.get("yizi_applied"))
    return {
        "threshold": thr,
        "threshold_text": "%g%%" % thr,
        "days": stat["days"],
        "days_text": _cnt_text(stat["days"]),
        "range": stat.get("range") or {"start": "", "end": ""},
        "items": items,
        # 给页面尾部小字用的现成话（前端不自己拼口径说明）
        "note": ("按 (今开/昨收−1) 算开盘涨跌（达到阈值即计，含恰好 "
                 "±%g%%）；红柱/绿柱按当日收阳/收阴。"
                 "高开绿柱、低开红柱为条件概率（分母是各自形态的天数）。"
                 "一字板（全天一个价）不算跳空：一字涨停不计入高开、一字跌停不计入低开。"
                 % thr),
        # ---- 一字板剔除的**事实**（前端据此在卡片上写「已剔除 N 天」）----
        # ⚠ `applied=False` = 面板里没有 `limit_up`/`limit_down` 列 ⇒ **没做剔除**。
        #   这种情况页面要照实说，不能装作排过（见 one_word_mask 的说明）。
        #   个数与阈值**无关**（一字板是 K 线事实），所以改了阈值也不会变 ——
        #   前端即时重算不需要重算这两个数。
        "yizi": {
            "applied": yz_applied,
            "n_up": n_yz_up,
            "n_dn": n_yz_dn,
            "text": _yizi_text(yz_applied, n_yz_up, n_yz_dn),
        },
        # ---- 阈值可调 + 即时重算所需的**全部口径常量与原料** ----
        # 页面只按这一个描述做「改阈值 → 重算」，不在前端另写一套算法：
        #   · `eps`   —— 边界容差（与后端 EPS 同一个数，含边界判据的唯一常量）；
        #   · `bars`  —— 逐根 `{idx, gap, yang, yin, yz_up, yz_dn}`（gap=开盘涨跌幅%，
        #                idx=该根在 rows 里的行号，用于把标记对回 K 线；
        #                yz_* = 一字涨停/一字跌停，**后端判好的**，前端只按它剔）；
        # 前端重算就是：`gap >= thr - eps` / `gap <= -thr + eps` 各数一遍，
        # 再把 yz_up / yz_dn 那几根从对应的分子里去掉。
        "adjust": {
            "min": OPEN_PCT_MIN,
            "max": OPEN_PCT_MAX,
            "step": 0.1,
            "eps": EPS,
            "bars": stat.get("bars") or [],
        },
    }


def _yizi_text(applied: bool, n_up: int, n_dn: int) -> str:
    """卡片上那句「一字板剔除了几天」。**唯一拼法**（前端也用它，别各拼一套）。"""
    if not applied:
        return ""
    parts = []
    if n_up:
        parts.append("一字涨停 %d 天" % n_up)
    if n_dn:
        parts.append("一字跌停 %d 天" % n_dn)
    return ("已剔除 " + " / ".join(parts)) if parts else ""


def recalc_from_bars(bars: list, threshold: float) -> dict:
    """用**已下发的逐根原料**按新阈值重算四格频次。

    与 `stats_from_frame` 的判据**逐字一致**（同一个 `is_high_open` /
    `is_low_open`）。这是给回归对拍用的「后端权威重算」—— 页面上的 JS 版
    （`window.openstatCalc`，见 templates/stock.html）算出来的结果必须与它相等；
    两边若分岔，说明前端偷偷自己改了算法。
    """
    thr = float(threshold)
    days = 0
    n_high = n_low = n_high_green = n_low_red = 0
    for b in bars or []:
        days += 1
        # ⚠ 一字板不算跳空形态（2026-10-10）：与 stats_from_frame 同一处口径 ——
        #   一字涨停从「高开」里剔、一字跌停从「低开」里剔（只看后端判好的标记，
        #   不在这儿重判 high==low）。分母 days 照旧。
        hi = is_high_open(b.get("gap"), thr) and not b.get("yz_up")
        lo = is_low_open(b.get("gap"), thr) and not b.get("yz_dn")
        if hi:
            n_high += 1
            if b.get("yin"):
                n_high_green += 1
        if lo:
            n_low += 1
            if b.get("yang"):
                n_low_red += 1

    def rate(num, den):
        return (num / den * 100.0) if den else None

    return {"days": days, "n_high": n_high, "n_low": n_low,
            "n_high_green": n_high_green, "n_low_red": n_low_red,
            "p_high": rate(n_high, days), "p_low": rate(n_low, days),
            "p_high_green": rate(n_high_green, n_high),
            "p_low_red": rate(n_low_red, n_low), "threshold": thr}


def open_stat(code: str, start: str, end: str, threshold: float = OPEN_PCT) -> dict:
    """个股诊断页「开盘概率」整块数据（按 `start~end` 区间统计）。

    区间就是页面上「K线根数」对应的那段（调用方给 start/end），
    与图上画的 K 线同一段 —— 否则卡片上的「N 个交易日」跟图对不上。
    """
    from core import market      # 局部 import：避免 market ↔ openstat 顶部循环

    panel = market.load_panel([code], start, end, weekly=False, only=None)
    if code not in panel:
        out = snapshot(stats_from_frame(None, threshold))
        out["ok"] = False
        return out
    out = snapshot(stats_from_frame(panel[code], threshold))
    out["ok"] = True
    return out
