# -*- coding: utf-8 -*-
"""筹码与两融：股东户数、融资融券、股东增减持的读取、派生与卡片快照。

这三类数据**都不是日频 K 线字段**（股东户数是报告期、增减持是公告事件、两融虽按
交易日但属于"外部资金"，不参与 K 线面板的指标计算），所以单独一层，不塞进
`indicators` 的面板字段里 —— 面板字段会被策略条件、选股、回测一起消费，
口径一变全体跟着变。

三条硬规矩：

1. **一律按 `ann_date`（公告日）/ `trade_date` ≤ 视角日期过滤。** 个股诊断页可以把
   视角调到历史某一天；用"当年还没公告的股东户数"就是未来函数，图上看着挺准，
   实际是穿越。
2. **单位换算与派生只在这里算一次。** 两融金额库里是**元**、`daily_basic.circ_mv`
   是**万元** —— 两处单位不同，换算写在一处才不会各算各的。
3. **文本与涨跌色在这里拼好**，前端只铺格子（与 `indicators.fundamental_snapshot`、
   `backtest.perf_snapshot` 同一套做法），免得 `%`／"亿"／正负号在 JS 里再拼一遍。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core import db
from core import log as _logmod

_log = _logmod.get(__name__)

HOLDER_PERIODS = 8      # 股东户数：卡片下方趋势图画多少期
MARGIN_DAYS = 180       # 融资融券：趋势图画多少个交易日
TRADE_LIMIT = 12        # 增减持：表里最多列多少条
MARK_MAX_ITEMS = 5      # 增减持：K 线顶边那个 ± 标记的 tooltip 最多展开几条明细

HOLDER_TYPE_NAME = {"G": "高管", "P": "个人", "C": "公司"}
IN_DE_NAME = {"IN": "增持", "DE": "减持"}


# ---------------------------------------------------------------------------
# 小工具：数值 / 文本
# ---------------------------------------------------------------------------

def _is_num(v) -> bool:
    """能当数字用吗（None / NaN / 空串都算缺）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return f == f          # NaN 自己不等于自己


def _num(v):
    return float(v) if _is_num(v) else None


def _amt_text(v) -> str:
    """金额：入参单位**元** → 「亿 / 万 / 元」。

    注意与 `indicators._mv_text`（入参万元）不是一回事 —— 两融接口返回的是元。
    """
    if not _is_num(v):
        return "—"
    x = float(v)
    if abs(x) >= 1e8:
        return "%.2f亿" % (x / 1e8)
    if abs(x) >= 1e4:
        return "%.2f万" % (x / 1e4)
    return "%.0f元" % x


def _cnt_text(v) -> str:
    """数量：入参单位**股 / 户** → 「亿 / 万 / 原值」。"""
    if not _is_num(v):
        return "—"
    x = float(v)
    if abs(x) >= 1e8:
        return "%.2f亿" % (x / 1e8)
    if abs(x) >= 1e4:
        return "%.2f万" % (x / 1e4)
    return "%.0f" % x


def _signed_amt(v) -> str:
    """带符号金额：+2.27亿 / -1234万。零不带正号（「+0」会被读成"流入"）。"""
    if not _is_num(v):
        return "—"
    x = float(v)
    return ("+" if x > 0 else "") + _amt_text(x)


def _signed_pct(v) -> str:
    """带符号百分数：+21.90% / -5.02% / 0.00%。"""
    if not _is_num(v):
        return "—"
    x = float(v)
    return ("+" if x > 0 else "") + "%.2f%%" % x


def _pct_text(v) -> str:
    return "—" if not _is_num(v) else "%.2f%%" % float(v)


def _price_text(v) -> str:
    return "—" if not _is_num(v) else "%.2f元" % float(v)


def _cls(v) -> str:
    """数值 → 涨跌色 class（涨红跌绿；缺值/零不上色）。"""
    if not _is_num(v):
        return "flat"
    x = float(v)
    if x == 0:
        return "flat"
    return "up" if x > 0 else "down"


def _d(v) -> str:
    return str(v)[:10] if v is not None else ""


# ---------------------------------------------------------------------------
# 股东户数
# ---------------------------------------------------------------------------

def _holder_collapsed(rows: list[dict]) -> list[dict]:
    """把 `holder_number` 原始行归并成「报告期序列」（时间正序，含环比增长率%）。

    **这个规则只有一份**：个股诊断页的卡片（`holder_history`）与选股字段
    （`_put_holder`）都调它 —— 否则"同一报告期取哪次公告""增长率跟谁比"在
    两个地方各算一套，图上和条件里对不上。

      · 同一 `end_date` 可能在**多个公告日**出现（业绩快报 → 正式报告）：按
        `end_date` 归并、取**最新一次公告**（`ann_date` 最大）—— 卡片上
        「发布日期」显示的就是最终披露日；
      · 增长率的对比基准是**相邻的上一个报告期**（按 end_date 排序），
        上一期缺数就整条给 None，不跳过去拿更早的一期顶。
    """
    by_end: dict[str, dict] = {}
    for r in rows:
        k = _d(r["end_date"])
        ann = _d(r["ann_date"])
        cur = by_end.get(k)
        if cur is None or ann >= cur["ann_date"]:
            by_end[k] = {"end_date": k, "ann_date": ann, "holder_num": _num(r["holder_num"])}
    seq = [by_end[k] for k in sorted(by_end)]
    prev = None
    for it in seq:
        hn = it["holder_num"]
        it["growth"] = ((hn / prev - 1) * 100) if (prev and hn and prev > 0) else None
        it["prev_num"] = prev
        prev = hn
    return seq


def holder_history(code: str, end: str, periods: int = HOLDER_PERIODS) -> list[dict]:
    """按报告期归并后的股东户数序列（时间正序，含环比增长率%）。

    归并规则见 `_holder_collapsed`（与选股字段共用，别再抄一份）。
    """
    if not code or not end:
        return []
    try:
        rows = db.query(
            "SELECT ann_date, end_date, holder_num FROM holder_number "
            "WHERE ts_code=%s AND ann_date<=%s ORDER BY end_date, ann_date",
            (code, end))
    except Exception as e:  # noqa: BLE001  表没建/没同步时安静降级为空
        _log.warning("读取 holder_number 失败，股东户数按无数据处理: %s", e)
        return []
    if not rows:
        return []
    seq = _holder_collapsed(rows)
    return seq[-periods:] if periods else seq


def holder_snapshot(code: str, end: str, periods: int = HOLDER_PERIODS) -> dict:
    """股东户数卡片：最新户数 + 增长率 + 截止日期 + 发布日期（全部是后端算好的文本）。"""
    seq = holder_history(code, end, periods=periods)
    if not seq:
        return {"available": False, "badge": "本地无数据", "hero": None,
                "items": [], "series": [], "columns": [],
                "note": "尚未同步股东户数（数据管理 → 同步股东/两融数据）"}
    last = seq[-1]
    prev = seq[-2] if len(seq) > 1 else None
    g = last.get("growth")
    items = [
        {"key": "growth", "label": "环比增长率", "text": _signed_pct(g), "cls": _cls(g)},
        {"key": "end_date", "label": "截止日期", "text": last["end_date"] or "—", "cls": ""},
        {"key": "ann_date", "label": "发布时间", "text": last["ann_date"] or "—", "cls": ""},
        {"key": "prev", "label": "上期户数(%s)" % (prev["end_date"] if prev else "—"),
         "text": _cnt_text(prev["holder_num"]) if prev else "—", "cls": ""},
    ]
    return {
        "available": True,
        # 主数（户数本身）**不上色** —— 拿增长率的方向给户数上色，会把「户数变多」
        # 涂成红色，读起来像"涨了"，方向对了但对象错位。涨跌色只挂在「环比增长率」那一项。
        "hero": {"text": _cnt_text(last["holder_num"]), "label": "股东户数", "cls": ""},
        "items": items,
        "badge": "近 %d 期 · 最新 %s" % (len(seq), last["end_date"] or "—"),
        "note": ("环比增长率 = 本期股东户数与上一报告期相比；"
                 "户数减少通常意味着筹码集中，增加则意味着筹码分散。"
                 "选股条件可用：hold_num / hold_chg / hold_chg_amt / hold_age"),
        "columns": ["报告期", "股东户数", "户数增长率", "发布日期"],
        "series": [{"end_date": it["end_date"], "holder_num": it["holder_num"],
                    "growth": it["growth"], "ann_date": it["ann_date"]}
                   for it in seq],
    }


# ---------------------------------------------------------------------------
# 融资融券
# ---------------------------------------------------------------------------

def margin_history(code: str, end: str, days: int = MARGIN_DAYS) -> list[dict]:
    """按交易日正序的融资融券序列（含派生：融资净买入 = 融资买入额 - 融资偿还额）。

    每行额外带一个 `close`（**同日日线收盘价**，元）—— 两融余额是「杠杆资金的多寡」，
    单独画看不出「钱进得比价涨得快还是慢」，要和价格叠着看才有意义。价格由
    `LEFT JOIN daily_kline` 取**同一天**的收盘价对齐，不改两融自己的交易日网格：

    * ⚠ 两融数据常比行情**晚一天**（收完盘才结算），最近那一两根可能还取不到价格；
    * ⚠ 停牌等该日无 K 线时 `close` 为 `None`（前端折线断点，不插值、不用前值填）。
    """
    if not code or not end:
        return []
    try:
        rows = db.query(
            "SELECT m.trade_date,m.rzye,m.rzmre,m.rzche,m.rqye,m.rqyl,m.rqmcl,m.rqchl,m.rzrqye,"
            "       k.close AS close "
            "FROM margin_detail m "
            "LEFT JOIN daily_kline k "
            "  ON k.ts_code=m.ts_code AND k.trade_date=m.trade_date "
            "WHERE m.ts_code=%s AND m.trade_date<=%s "
            "ORDER BY m.trade_date DESC LIMIT %s", (code, end, int(days)))
    except Exception as e:  # noqa: BLE001
        _log.warning("读取 margin_detail 失败，两融按无数据处理: %s", e)
        return []
    out = []
    for r in reversed(rows):        # 上面的 SQL 取最近 N 条，这里翻回正序
        rzye, rzmre, rzche = _num(r["rzye"]), _num(r["rzmre"]), _num(r["rzche"])
        out.append({
            "trade_date": _d(r["trade_date"]),
            "rzye": rzye, "rzmre": rzmre, "rzche": rzche,
            "rz_net": (rzmre - rzche) if (rzmre is not None and rzche is not None) else None,
            "rqye": _num(r["rqye"]), "rqyl": _num(r["rqyl"]),
            "rqmcl": _num(r["rqmcl"]), "rqchl": _num(r["rqchl"]),
            "rzrqye": _num(r["rzrqye"]),
            "close": _num(r["close"]),
        })
    return out


def _circ_mv_wan(code: str, as_of: str) -> float | None:
    """取 <= as_of 的最近一条流通市值（**万元**，daily_basic 原单位）。"""
    try:
        row = db.query_one(
            "SELECT circ_mv FROM daily_basic WHERE ts_code=%s AND trade_date<=%s "
            "ORDER BY trade_date DESC LIMIT 1", (code, as_of))
    except Exception:  # noqa: BLE001
        return None
    return _num(row["circ_mv"]) if row else None


def margin_snapshot(code: str, end: str, days: int = MARGIN_DAYS) -> dict:
    """融资融券卡片：融资余额（主数）+ 融资净买入 / 融券余额 / 两融余额 / 占流通市值。"""
    seq = margin_history(code, end, days=days)
    if not seq:
        return {"available": False, "badge": "本地无数据", "hero": None,
                "items": [], "series": [], "columns": [],
                "note": "尚未同步融资融券，或该股不是两融标的"
                        "（数据管理 → 同步股东/两融数据）"}
    last = seq[-1]
    # 两融余额占流通市值：circ_mv 是**万元**，两融是**元** —— 差 1e4，只在这里换算。
    # 取与最后一根两融**同一天或更早**的流通市值（两融数据常比行情晚一天）。
    mv_wan = _circ_mv_wan(code, last["trade_date"])
    ratio = None
    if mv_wan and last["rzrqye"] is not None and mv_wan > 0:
        ratio = last["rzrqye"] / (mv_wan * 1e4) * 100
    items = [
        {"key": "rz_net", "label": "融资净买入", "text": _signed_amt(last["rz_net"]),
         "cls": _cls(last["rz_net"])},
        {"key": "rqye", "label": "融券余额", "text": _amt_text(last["rqye"]), "cls": ""},
        {"key": "rzrqye", "label": "两融余额", "text": _amt_text(last["rzrqye"]), "cls": ""},
        {"key": "ratio", "label": "占流通市值", "text": _pct_text(ratio), "cls": ""},
    ]
    return {
        "available": True,
        # 主数是「融资余额」，它本身没有涨跌方向可言 —— 颜色留给下面的「融资净买入」。
        "hero": {"text": _amt_text(last["rzye"]), "label": "融资余额", "cls": ""},
        "items": items,
        "badge": "近 %d 个交易日 · 最新 %s" % (len(seq), last["trade_date"]),
        "note": "融资净买入 = 融资买入额 − 融资偿还额；两融余额占流通市值 = "
                "融资融券余额 ÷ 同日流通市值。图上的价格折线 = 同日日线收盘价"
                "（按两融自己的交易日对齐，停牌日无价则断点）。选股条件可用："
                "rz_ye / rz_net / rz_net5 / rz_net20 / rz_chg5 / rzrq_ratio",
        "columns": ["交易日", "融资余额", "融资净买入", "融券余额", "两融余额", "收盘价"],
        "series": seq,
    }


# ---------------------------------------------------------------------------
# 股东增减持
# ---------------------------------------------------------------------------

def trade_marks(code: str, bar_dates, end: str | None = None,
                limit: int = MARK_MAX_ITEMS) -> list[dict]:
    """把股东增减持公告对齐到**图上 K 线的行号**（供 K 线顶边的 ± 标记）。

    对齐口径只有一条：**公告日 → 「当天或之后的第一根 K 线」**。公告多在盘后发布，
    市场最早在下一个交易日才知道；公告日恰为交易日时就落在当天。等价于在升序的
    K 线日期序列上做 `searchsorted(side='left')` —— **不改 K 线网格、不插值**
    （与两融那条价格折线一个道理：宁可少标，绝不把标记挪到一根不存在的 K 线上）。

    ⚠ 只看 `ann_date <= end`（= 查询日）：与筹码卡片同一口径。图上是画了查询日
      右侧的行情，但那是"之后才发生的行情"，不往上面追加事后才知道的公告。

    同一天同方向的多条（多个股东各减一笔）**归成一个标记**：`count` 是条数、
    数量/比例是合计；逐条明细放在 `items` 里（最多 `limit` 条）供 tooltip 展开。
    不对齐到任何 K 线的（公告晚于最后一根）直接丢掉。

    返回项：`{idx, date, ann_date, in_de, dir_text, count, vol_text, ratio_text, items}`
    —— `idx` 是 `bar_dates` 里的行号，前端只按它摆点。
    """
    ds = sorted({_d(x) for x in (bar_dates or []) if x})
    if not code or not ds:
        return []
    hi = _d(end) if end else ds[-1]
    hi = min(hi, ds[-1]) if hi else ds[-1]
    # 下界往前放 7 天：公告落在窗口首个交易日**之前的周末/假期**时，仍能对齐到第一根
    lo = (pd.Timestamp(ds[0]) - pd.Timedelta(days=7)).strftime("%Y-%m-%d")
    if hi < lo:
        return []
    try:
        rows = db.query(
            "SELECT ann_date,holder_name,holder_type,in_de,change_vol,change_ratio "
            "FROM holder_trade WHERE ts_code=%s AND ann_date BETWEEN %s AND %s "
            "ORDER BY ann_date, change_vol DESC", (code, lo, hi))
    except Exception as e:  # noqa: BLE001
        _log.warning("读取 holder_trade 失败，K 线增减持标记按无数据处理: %s", e)
        return []
    if not rows:
        return []
    arr = np.array(ds, dtype="datetime64[D]")
    groups: dict[tuple[int, str], dict] = {}
    for r in rows:
        in_de = (r.get("in_de") or "").strip().upper()
        if in_de not in IN_DE_NAME:
            continue            # 方向不明（既非 IN 也非 DE）的不画，免得图上出现第三种符号
        ad = _d(r.get("ann_date"))
        if not ad:
            continue
        pos = int(np.searchsorted(arr, np.datetime64(ad, "D"), side="left"))
        if pos >= len(ds):
            continue            # 公告晚于最后一根 K 线：没有可挂的 K 线
        g = groups.get((pos, in_de))
        if g is None:
            g = groups[(pos, in_de)] = {
                "idx": pos, "date": ds[pos], "ann_date": ad, "in_de": in_de,
                "count": 0, "vol": 0.0, "ratio": 0.0, "items": []}
        vol, rat = _num(r.get("change_vol")), _num(r.get("change_ratio"))
        g["count"] += 1
        g["vol"] += vol or 0.0
        g["ratio"] += rat or 0.0
        g["ann_date"] = min(g["ann_date"], ad)   # 这一组里最早的公告日 = 市场头一次知道
        if len(g["items"]) < int(limit):
            g["items"].append({
                "ann_date": ad,
                "holder_name": (r.get("holder_name") or "").strip(),
                "holder_type": HOLDER_TYPE_NAME.get(
                    (r.get("holder_type") or "").strip().upper(),
                    (r.get("holder_type") or "").strip()),
                "vol_text": _cnt_text(vol),
                "ratio_text": _pct_text(rat),
            })
    out = []
    for g in groups.values():
        g["dir_text"] = IN_DE_NAME.get(g["in_de"], g["in_de"])
        g["vol_text"] = _cnt_text(g["vol"])      # 股 → 万/亿股，只在这里换一次
        g["ratio_text"] = _pct_text(g["ratio"])
        del g["vol"], g["ratio"]
        out.append(g)
    out.sort(key=lambda x: (x["idx"], x["in_de"]))
    return out


def trade_snapshot(code: str, end: str, limit: int = TRADE_LIMIT,
                   bar_dates=None) -> dict:
    """股东增减持：最近 N 条明细 + 增减持条数与股数汇总（+ K 线顶边标记 `marks`）。

    `bar_dates` = 图上 K 线的交易日序列：给了才把公告对齐成图上的 ± 标记
    （`trade_marks`），不给就只出下面那张明细表。
    """
    if not code or not end:
        return {"available": False, "badge": "本地无数据", "items": [], "summary": None,
                "marks": [],
                "note": "尚未同步股东增减持（数据管理 → 同步股东/两融数据）"}
    try:
        rows = db.query(
            "SELECT ann_date,holder_name,holder_type,in_de,change_vol,change_ratio,"
            "after_share,after_ratio,avg_price,total_share FROM holder_trade "
            "WHERE ts_code=%s AND ann_date<=%s "
            "ORDER BY ann_date DESC, change_vol DESC LIMIT %s",
            (code, end, int(limit)))
    except Exception as e:  # noqa: BLE001
        _log.warning("读取 holder_trade 失败，增减持按无数据处理: %s", e)
        rows = []
    if not rows:
        return {"available": False, "badge": "本地无记录", "items": [], "summary": None,
                "marks": [],
                "note": "该股在本地没有增减持记录（未同步，或近三年确实没有公告）"}
    items, in_cnt, de_cnt, in_vol, de_vol = [], 0, 0, 0.0, 0.0
    for r in rows:
        in_de = (r.get("in_de") or "").strip().upper()
        vol = _num(r.get("change_vol"))
        if in_de == "IN":
            in_cnt += 1
            in_vol += vol or 0.0
        elif in_de == "DE":
            de_cnt += 1
            de_vol += vol or 0.0
        avg = _num(r.get("avg_price"))
        items.append({
            "ann_date": _d(r.get("ann_date")),
            "holder_name": (r.get("holder_name") or "").strip(),
            "holder_type": HOLDER_TYPE_NAME.get(
                (r.get("holder_type") or "").strip().upper(), r.get("holder_type") or ""),
            "in_de": in_de,
            "dir_text": IN_DE_NAME.get(in_de, in_de or "—"),
            "cls": "up" if in_de == "IN" else ("down" if in_de == "DE" else ""),
            "change_vol_text": _cnt_text(vol),
            "change_ratio_text": _pct_text(r.get("change_ratio")),
            "after_ratio_text": _pct_text(r.get("after_ratio")),
            "avg_price_text": _price_text(avg),
            # 变动金额 = 变动数量 × 交易均价；均价缺（多数公告没有）就算不出来，给「—」
            "amount_text": (_amt_text(vol * avg)
                            if (vol is not None and avg is not None) else "—"),
        })
    net = in_vol - de_vol
    summary = {
        "count": len(items),
        "badge": "近 %d 条" % len(items),
        "items": [
            {"key": "in_cnt", "label": "增持", "text": "%d 条" % in_cnt, "cls": "up"},
            {"key": "de_cnt", "label": "减持", "text": "%d 条" % de_cnt, "cls": "down"},
            {"key": "in_vol", "label": "增持股数", "text": _cnt_text(in_vol), "cls": ""},
            {"key": "de_vol", "label": "减持股数", "text": _cnt_text(de_vol), "cls": ""},
            {"key": "net", "label": "净变动", "text": _cnt_text(net), "cls": _cls(net)},
        ],
    }
    return {
        "available": True,
        "items": items,
        "summary": summary,
        # K 线顶边的 ± 标记：把公告按「当天或之后的第一根 K 线」对齐到图上（见 trade_marks）。
        # 没传 `bar_dates`（比如只想看表格）时就是空表，前端也就不摆标记。
        "marks": trade_marks(code, bar_dates, end),
        "badge": "近 %d 条" % len(items),
        "note": "取自 tushare 股东增减持公告（按公告日倒序）；"
                "「变动比例」是本次变动占流通股比例，「交易均价」多数公告不披露。"
                "图上方 K 线顶边的 + / − 就是增减持公告（+ 增持 / − 减持），"
                "按「公告日当天或之后的第一根 K 线」对齐，只画到查询日为止。"
                "选股条件可用：ht_net30 / ht_net90 / ht_ratio30 / ht_cnt30 / ht_cnt90",
    }


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def chips_snapshot(code: str, end: str, holder_periods: int = HOLDER_PERIODS,
                   margin_days: int = MARGIN_DAYS,
                   trade_limit: int = TRADE_LIMIT, bar_dates=None) -> dict:
    """个股诊断页「筹码与两融」整块数据（三份快照 + 视角日期）。

    `bar_dates` = 图上 K 线的交易日序列，只用来把增减持公告对齐到行号
    （`trade_marks`）；不传就只出卡片表格、不出图上的 ± 标记。
    """
    return {
        "as_of": end or "",
        "holder": holder_snapshot(code, end, holder_periods),
        "margin": margin_snapshot(code, end, margin_days),
        "trade": trade_snapshot(code, end, trade_limit, bar_dates),
    }


# ===========================================================================
# 选股字段：把这三类数据按「绝不使用未来数据」的口径贴到 K 线面板上
# ===========================================================================
#
# 三类数据的**对齐方式各不相同**，所以不能塞进 `indicators.compute`（那是"逐行
# 向量算"的面板算子），只能按 asof 贴进来：
#
#   · 股东户数 —— 报告期数据、**公告日才知道**：按 `ann_date <= T` 往后贴。
#     增长率用「相邻上一期」（`_holder_collapsed`，与卡片同一条规则），所以要
#     从很早读起才能保证第一期增长率算得出来。
#   · 融资融券 —— 交易日频，但常比行情晚一天：按 `trade_date <= T` 往后贴。
#     **窗口类字段（5/20 日累计净买入）在两融自己的序列上算完再贴** ——
#     在 K 线网格上 rolling 会把"两融还没出的那天"算进窗口，变成 NaN 窗口。
#   · 股东增减持 —— 公告事件：对每个 T 统计 `T-N < ann_date <= T` 的事件合计。
#     用「事件当日累加值 asof(T) − asof(T−N)」，天然就是开区间 `(T-N, T]`。
#
# 缺数据（表没同步 / 该股不在覆盖范围）**一律留 NaN**，条件因此恒不成立 ——
# 与 `mf_*` 一个规矩：宁可少选，绝不选错。**不给 0**（0 会被读成"确实没有增减持"）。
# ---------------------------------------------------------------------------

CHIP_FIELDS: dict[str, tuple[str, int, str, str, str]] = {
    # 字段名 -> (结果列表头, 小数位, 颜色角色, 数值格式, 字段说明)
    #
    #   · 表头/小数位/格式给**选股结果表**用：结果表按「条件引用到的字段」自动加列，
    #     表头从这里出，前端不写死任何字段名（同 `chart_catalog`、`monitor.html` 的 FLD 规矩）。
    #   · 颜色角色 `"updown"` = 按正负上色（涨红跌绿）；留空表示这是个"水平量"，不拿方向着色
    #     —— 拿户数增长率的方向给"股东户数"上色，会把"户数变多"涂成红色（方向对、对象错位）。
    #   · 说明**必须逐字段写**：以前写成 "hold_num / hold_chg / …" 一条，字段参考里每个字段
    #     都显示同一串"别人的含义"，看着像串了行（用户报的正是这个问题）。
    #
    # 股东户数（报告期数据，按**公告日** asof）
    "hold_num": ("股东户数", 0, "", "int",
                 "股东户数（户）。按公告日对齐：第 T 天只看得到 T 之前已公告的那一期（需同步股东户数）"),
    "hold_chg": ("户数环比%", 2, "updown", "pct",
                 "股东户数环比增长率 %（跟相邻上一个报告期比）。正=户数增加=筹码分散，负=集中；上一期缺数时缺值"),
    "hold_chg_amt": ("户数变化", 0, "updown", "int",
                     "股东户数比上一期的变化量（户）。正=增加（分散），负=减少（集中）"),
    "hold_age": ("距公告天", 0, "", "int",
                 "当前这一期户数距今过了多少天 —— 越大说明手里的户数信息越「旧」（需同步股东户数）"),
    # 融资融券（交易日频，按**交易日** asof；金额统一万元）
    "rz_ye": ("融资余额万", 2, "", "big",
              "融资余额（万元）：投资者借钱买入、尚未偿还的金额（需同步两融；只有两融标的才有）"),
    "rz_net": ("融资净买入万", 2, "updown", "big",
               "当日融资净买入（万元）= 融资买入额 − 融资偿还额（需同步两融）"),
    "rz_net5": ("5日融资净额万", 2, "updown", "big",
                "近 5 个交易日融资净买入合计（万元）。在两融自己的序列上滚动，不跟 K 线网格混"),
    "rz_net20": ("20日融资净额万", 2, "updown", "big",
                 "近 20 个交易日融资净买入合计（万元）"),
    "rz_chg5": ("融资余额5日%", 2, "updown", "pct",
                "融资余额 5 日变化率 %（相对 5 个交易日前）"),
    "rq_ye": ("融券余额万", 2, "", "big",
              "融券余额（万元）：借券卖出、尚未买回的部分（需同步两融）"),
    "rzrq_ye": ("两融余额万", 2, "", "big",
                "两融余额（万元）= 融资余额 + 融券余额"),
    "rzrq_ratio": ("两融占流通%", 2, "", "num",
                   "两融余额占流通市值 %；只有两融标的、且当日有流通市值时有值"),
    # 股东增减持（公告事件，按**公告日窗口**统计，窗口是开区间 (T-N, T]）
    "ht_net30": ("30日净增持万", 2, "updown", "big",
                 "近 30 日净增持（万股）= 增持量 − 减持量，减持记负；公告当天就算「近 30 日」（需同步增减持）"),
    "ht_net90": ("90日净增持万", 2, "updown", "big",
                 "近 90 日净增持（万股），口径同 30 日"),
    "ht_ratio30": ("30日增持%", 2, "updown", "pct",
                   "近 30 日净增持数量占流通股 %（各条公告的变动比例求和）"),
    "ht_ratio90": ("90日增持%", 2, "updown", "pct",
                   "近 90 日净增持数量占流通股 %"),
    "ht_cnt30": ("30日公告数", 0, "", "int",
                 "近 30 日增减持公告条数（只看条数、不看方向，用来判断「最近有没有人在动」）"),
    "ht_cnt90": ("90日公告数", 0, "", "int",
                 "近 90 日增减持公告条数（需同步增减持）"),
}
"""筹码与两融的**唯一字段表**：改名/加字段只改这里，下面四份视图全部由它派生。"""

CHIPS_DERIVED: list[str] = list(CHIP_FIELDS)
"""18 个筹码/两融字段名（顺序 = 上表顺序）。"""

CHIP_FIELD_HELP: list[tuple[str, str]] = [(k, v[4]) for k, v in CHIP_FIELDS.items()]
"""(字段名, 说明) —— 与 `indicators.FIELD_HELP` 同格式，由它并进去，供
`/api/fields`、`/api/universe/fields`、策略页「可用字段参考」与输入补全使用。"""

CHIP_COL_SPEC: dict[str, tuple[str, int, str, str]] = {
    k: (v[0], v[1], v[2], v[3]) for k, v in CHIP_FIELDS.items()
}
"""字段 -> 结果表列定义 `(表头, 小数位, 颜色角色, 数值格式)`（与 `indicators.COL_SPEC` 同格式，
由它并进去；前端要的 dict 形式由 `indicators.col_spec()` 统一产出）。"""


# 判断「策略引用了筹码字段吗」的前缀。加字段时**别忘了加前缀**，否则会被 only= 裁掉。
_CHIP_PREFIXES = ("hold_", "rz_", "rq_", "rzrq_", "ht_")

TRADE_WINDOWS = (30, 90)
"""增减持统计窗口（天）。开区间 (T-N, T]：公告当天就算"近 N 日"。"""


def needs_chips(names) -> bool:
    """给定要算的字段集合，判断是否需要读这三张表（加载侧与计算侧共用一条判定）。

    `names is None`（= 面板指标"全算"）**返回 False**：筹码不是面板指标，
    个股诊断页的筹码走 `chips_snapshot` 的卡片，不靠面板列 —— 别为它白读三张表。
    这与 `indicators.needs_moneyflow(None) → True` 不同，是刻意的。
    """
    if names is None:
        return False
    return any(str(n).startswith(_CHIP_PREFIXES) for n in names)


# 载入窗口：每个字段需要多久历史就回看多久（自然日）
_HOLDER_FROM = "2000-01-01"     # 户数要「相邻上一期」算增长率 → 从头读（表本身很小）
_MARGIN_LOOKBACK = 120          # 两融：5/20 日窗口预热 + 数据比行情晚一天的余量
_TRADE_LOOKBACK = 120           # 增减持：最长窗口 90 天 + 余量


def _read(table: str, cols: list[str], codes: list[str], d0: str, d1: str,
          date_col: str, chunk: int = 300) -> pd.DataFrame | None:
    """分批读一张表；表不存在 / 没同步过时返回 None（安静降级，不打断选股）。"""
    frames = []
    for i in range(0, len(codes), chunk):
        part = codes[i:i + chunk]
        sql = (f"SELECT {','.join(cols)} FROM {table} "
               f"WHERE ts_code IN ({','.join(['%s'] * len(part))}) "
               f"AND {date_col} BETWEEN %s AND %s")
        try:
            rows = db.query(sql, part + [d0, d1])
        except Exception as e:  # noqa: BLE001
            _log.warning("读取 %s 失败，筹码字段按缺数据处理: %s", table, e)
            return None
        if rows:
            frames.append(pd.DataFrame(rows))
    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True)
    df[date_col] = _dts(df[date_col])
    return df


def _dts(s) -> pd.Series:
    """统一成 `datetime64[ns]`。

    **必须显式转**：pandas 3 的默认分辨率是 **微秒**（`M8[us]`），而
    `merge_asof` 要求左右键 dtype 完全一致，否则抛
    `incompatible merge keys M8[ns] and M8[us]` —— 报错信息里两个都是 datetime，
    很容易看漏。
    """
    return pd.to_datetime(s).astype("datetime64[ns]")


def _split(df: pd.DataFrame | None) -> dict[str, pd.DataFrame]:
    """按 ts_code 切成 {code: 子表}（只切一次，后面每只股票直接取）。"""
    if df is None or not len(df):
        return {}
    return {str(c): g for c, g in df.groupby("ts_code", sort=False)}


class ChipsData:
    """一次批量载入的三类原始数据，按 code 取用。

    选股一次几百只、全市场一次 5000+ 只，**不能一只一只查** —— 这里按批
    IN 查询，和 `market._load_moneyflow` 一个做法。
    """

    __slots__ = ("holder", "margin", "trade", "circ_mv")

    def __init__(self, holder=None, margin=None, trade=None, circ_mv=None):
        self.holder = _split(holder)        # 股东户数（全历史）
        self.margin = _split(margin)        # 融资融券
        self.trade = _split(trade)          # 股东增减持
        self.circ_mv = _split(circ_mv)      # 流通市值（算两融占比用）

    def empty(self) -> bool:
        """三张表一条数据都没读到（多半是压根没同步）。"""
        return not (self.holder or self.margin or self.trade)


def load(codes: list[str], start: str, end: str) -> ChipsData:
    """批量读三类数据（**只在策略真的引用筹码字段时**才会被调用，见 needs_chips）。

    回看窗口按"每个字段要多久历史"分别给，不是统一拍一个数：
    户数要算上一期增长率 → 读全历史；两融要 rolling/shift 预热；增减持最长看 90 天。
    """
    codes = [c for c in (codes or []) if c]
    if not codes:
        return ChipsData()
    m0 = (pd.Timestamp(start) - pd.Timedelta(days=_MARGIN_LOOKBACK)).strftime("%Y-%m-%d")
    t0 = (pd.Timestamp(start) - pd.Timedelta(days=_TRADE_LOOKBACK)).strftime("%Y-%m-%d")
    return ChipsData(
        holder=_read("holder_number", ["ts_code", "ann_date", "end_date", "holder_num"],
                     codes, _HOLDER_FROM, end, "ann_date"),
        margin=_read("margin_detail",
                     ["ts_code", "trade_date", "rzye", "rzmre", "rzche", "rqye", "rzrqye"],
                     codes, m0, end, "trade_date"),
        trade=_read("holder_trade",
                    ["ts_code", "ann_date", "in_de", "change_vol", "change_ratio"],
                    codes, t0, end, "ann_date"),
        circ_mv=_read("daily_basic", ["ts_code", "trade_date", "circ_mv"],
                      codes, m0, end, "trade_date"),
    )


# ---------------------------------------------------------------------------
# 三块贴板
# ---------------------------------------------------------------------------

def _asof_pos(at: np.ndarray, keys: np.ndarray) -> np.ndarray:
    """keys 升序；返回「<= 每个 at 的最后一个 key」的下标（没有则 -1）。"""
    return np.searchsorted(keys, at, side="right") - 1


def _cum_asof(keys: np.ndarray, vals: np.ndarray, at: np.ndarray) -> np.ndarray:
    """事件累计和的 asof 值：对每个 at 取「key <= at 的事件」之和（无事件给 0）。"""
    cs = np.cumsum(vals)
    pos = _asof_pos(at, keys)
    return np.where(pos >= 0, cs[np.maximum(pos, 0)], 0.0)


def _put_holder(out: pd.DataFrame, rows, dts: np.ndarray) -> None:
    """股东户数：按**公告日** asof 贴到面板（`ann_date <= T` 才算已披露）。"""
    if rows is None or not len(rows):
        return
    seq = _holder_collapsed(rows.to_dict("records"))
    if not seq:
        return
    h = pd.DataFrame([{
        "ann_date": it["ann_date"],
        "end_date": it["end_date"],
        "hold_num": it["holder_num"],
        "hold_chg": it["growth"],
        "hold_chg_amt": (it["holder_num"] - it["prev_num"]
                         if (it["prev_num"] is not None and it["holder_num"] is not None)
                         else None),
    } for it in seq])
    h["ann_date"], h["end_date"] = _dts(h["ann_date"]), _dts(h["end_date"])
    h = h.dropna(subset=["ann_date"])
    if not len(h):
        return
    # 同一天可能既修订上一期、又披露本期（`seq` 已按报告期升序）→ 取报告期更大的那个。
    # merge_asof 对重复右键取"最后一个"，所以必须先按 (ann_date, end_date) 排好。
    h = (h.sort_values(["ann_date", "end_date"])
           .drop_duplicates(subset=["ann_date"], keep="last")
           .sort_values("ann_date"))
    m = pd.merge_asof(pd.DataFrame({"trade_date": dts}), h,
                      left_on="trade_date", right_on="ann_date", direction="backward")
    out["hold_num"] = m["hold_num"].to_numpy(dtype=float)
    out["hold_chg"] = m["hold_chg"].to_numpy(dtype=float)
    out["hold_chg_amt"] = m["hold_chg_amt"].to_numpy(dtype=float)
    out["hold_age"] = (m["trade_date"] - m["ann_date"]).dt.days.to_numpy(dtype=float)


def _margin_ratio(m: pd.DataFrame, mv, rzrq_raw: pd.Series) -> pd.Series:
    """两融余额占流通市值 %。

    `circ_mv` 单位是**万元**、两融余额是**元** —— 差 1e4，只在这里换算
    （与 `margin_snapshot` 同一个算法，别再写第二份）。市值取「该两融日或更早」
    的最近一条：两融数据常比行情晚一天。
    """
    if mv is None or not len(mv):
        return pd.Series(np.nan, index=m.index)
    v = mv.sort_values("trade_date")[["trade_date", "circ_mv"]].copy()
    v["circ_mv"] = pd.to_numeric(v["circ_mv"], errors="coerce")
    j = pd.merge_asof(m[["trade_date"]], v, on="trade_date", direction="backward")
    wan = j["circ_mv"].to_numpy(dtype=float)
    with np.errstate(invalid="ignore", divide="ignore"):
        ratio = np.where(wan > 0, rzrq_raw.to_numpy(dtype=float) / (wan * 1e4) * 100, np.nan)
    return pd.Series(ratio, index=m.index)


def _put_margin(out: pd.DataFrame, rows, mv, dts: np.ndarray) -> None:
    """融资融券：按**交易日** asof 贴到面板；窗口类字段在两融自己的序列上算。

    金额一律换算成**万元**（库里是元），与 `mf_*`、`total_mv` 同量纲，
    免得选股条件里出现 `rz_ye > 1e10` 这种读不出来的数。
    """
    if rows is None or not len(rows):
        return
    m = rows.sort_values("trade_date").copy()
    rz_ye = pd.to_numeric(m["rzye"], errors="coerce")
    rzrq = pd.to_numeric(m["rzrqye"], errors="coerce")
    net = pd.to_numeric(m["rzmre"], errors="coerce") - pd.to_numeric(m["rzche"], errors="coerce")
    m["rz_ye"] = rz_ye / 1e4
    m["rq_ye"] = pd.to_numeric(m["rqye"], errors="coerce") / 1e4
    m["rzrq_ye"] = rzrq / 1e4
    m["rz_net"] = net / 1e4
    # min_periods=1：开头不足窗口就给部分累计，别让整段变 NaN（与 mf_* 同口径）
    m["rz_net5"] = m["rz_net"].rolling(5, min_periods=1).sum()
    m["rz_net20"] = m["rz_net"].rolling(20, min_periods=1).sum()
    m["rz_chg5"] = (rz_ye / rz_ye.shift(5).replace(0, np.nan) - 1) * 100
    m["rzrq_ratio"] = _margin_ratio(m, mv, rzrq)
    cols = [c for c in CHIPS_DERIVED if c in m.columns]
    merged = pd.merge_asof(pd.DataFrame({"trade_date": dts}), m[["trade_date"] + cols],
                           on="trade_date", direction="backward")
    for c in cols:
        out[c] = merged[c].to_numpy(dtype=float)


def _put_trade(out: pd.DataFrame, rows, dts: np.ndarray) -> None:
    """股东增减持：统计每个 T 的 `T-N < ann_date <= T` 事件合计。

    用「事件累加值 asof(T) − asof(T−N)」算，差出来天然是开区间 `(T-N, T]`
    —— 公告当天就算"近 N 日"。载入窗口（`_TRADE_LOOKBACK`）比最长窗口大，
    所以对面板里的每一天，窗口起点都落在已载入的那段里，不会漏算。

    该股在回看窗口里**一条记录都没有**时整列留 NaN（缺数据 → 条件恒不成立）；
    有记录时窗口内没事件的给 0（真实含义：这 N 天确实没有公告）。
    """
    if rows is None or not len(rows):
        return
    kind = rows["in_de"].astype(str).str.strip().str.upper()
    vol = pd.to_numeric(rows["change_vol"], errors="coerce").fillna(0.0)
    rat = pd.to_numeric(rows["change_ratio"], errors="coerce").fillna(0.0)
    sign = np.where(kind == "DE", -1.0, np.where(kind == "IN", 1.0, 0.0))
    ev_src = pd.DataFrame({
        "ann_date": rows["ann_date"],
        "net": sign * vol.to_numpy(dtype=float) / 1e4,     # 股 → 万股；减持记负
        "ratio": sign * rat.to_numpy(dtype=float),
        "cnt": 1.0,
    }).groupby("ann_date", as_index=False).sum().sort_values("ann_date")
    keys = ev_src["ann_date"].to_numpy(dtype="datetime64[ns]")
    vals = {c: ev_src[c].to_numpy(dtype=float) for c in ("net", "ratio", "cnt")}
    for n in TRADE_WINDOWS:
        cut = dts - np.timedelta64(n, "D")
        out[f"ht_net{n}"] = _cum_asof(keys, vals["net"], dts) - _cum_asof(keys, vals["net"], cut)
        out[f"ht_ratio{n}"] = _cum_asof(keys, vals["ratio"], dts) - \
            _cum_asof(keys, vals["ratio"], cut)
        out[f"ht_cnt{n}"] = _cum_asof(keys, vals["cnt"], dts) - _cum_asof(keys, vals["cnt"], cut)


def attach(data: "ChipsData | None", df: pd.DataFrame, code: str) -> pd.DataFrame:
    """把 18 个筹码/两融字段按 asof 贴到 df（**已按 trade_date 升序**）上。

    无论有没有数据，**18 列一定都会出现**（没数据的填 NaN）—— 与 `mf_*` 一致：
    条件求值不会 KeyError，且"缺数据 → 恒不成立"而不是"缺数据 → 恒成立"。
    """
    if df is None or not len(df):
        return df
    out = df.copy()
    if data is not None:
        dts = pd.to_datetime(out["trade_date"]).to_numpy(dtype="datetime64[ns]")
        _put_holder(out, data.holder.get(code), dts)
        _put_margin(out, data.margin.get(code), data.circ_mv.get(code), dts)
        _put_trade(out, data.trade.get(code), dts)
    for c in CHIPS_DERIVED:
        if c not in out.columns:
            out[c] = np.nan
    return out
