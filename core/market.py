# -*- coding: utf-8 -*-
"""行情数据访问层：从 MySQL 读取日/周线，做复权处理，组装股票池。"""
from __future__ import annotations

import datetime as dt
import re
from functools import lru_cache

import numpy as np
import pandas as pd

from core import db, indicators as ind
from core import chips as ch
from core import log as _log

_log_mk = _log.get("market")

# ---------------------------------------------------------------------------
# 股票池
# ---------------------------------------------------------------------------

UNIVERSE_ALL = "all"


def get_stock_map() -> dict[str, dict]:
    """{ts_code: {...}} 全部股票基础信息。"""
    rows = db.query(
        "SELECT ts_code, symbol, name, area, industry, market, exchange, "
        "list_date, list_status FROM stock_basic"
    )
    return {r["ts_code"]: r for r in rows}


# ---------------------------------------------------------------------------
# 「输入框里写的东西」→ 库里的 ts_code
# ---------------------------------------------------------------------------

_TS_CODE_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")


def resolve_stock(q: str, limit: int = 20) -> dict:
    """把查询框里输的东西解析成库里的 ts_code —— **全站唯一的一处「输入 → 代码」**。

    认这几种写法（个股诊断页那行提示就是这么写的：「000001.SZ 或 000001 或 平安银行」）：
      · `600519.SH` / `000001.SZ` —— 完整代码，**形式对就收、不查库**：
        库里到底有没有这只票，交给下游那句「本地无 … 的行情数据」去说 ——
        同一个输入别在两处得到不同判断。
      · `600519` —— 6 位数字，按 `symbol` 查在市的票；**查不到原样返回**（同上）。
      · `贵州茅台` —— 名称**精确**匹配。
      · `茅台` —— 名称**模糊**匹配，**只有唯一命中才算数**。

    返回 `{"ts_code", "name", "candidates"}`，三种状态：
      · 认出来了 → `ts_code` 有值、`candidates` 空；
      · 名称匹配到多只 → `ts_code` 空、`candidates` 是候选。**绝不挑第一条**：
        输「平安」时「平安银行」和「中国平安」谁都不是"默认那个"，挑错了就是拿
        另一只票的行情冒充（而且图看着完全正常，看不出错）；
      · 什么都不匹配 → `ts_code` 空、`candidates` 空。
    调用方按这三态分别给文案（见 `screener.screen_detail` 的 `_code_error`）。

    ⚠ 「分时监控」页的腾讯写法（`sh600519`）→ ts_code 是**另一套输入空间**
      （认不出要返回空串、且不查库），在 `app._page_to_ts_code` 里，别和这里混。
    """
    q = str(q or "").strip()
    if not q:
        return {"ts_code": "", "name": "", "candidates": []}
    up = q.upper()

    if _TS_CODE_RE.match(up):                     # 完整代码：形式对就收
        row = db.query_one("SELECT name FROM stock_basic WHERE ts_code=%s", (up,)) or {}
        return {"ts_code": up, "name": row.get("name") or "", "candidates": []}

    if up.isdigit() and len(up) == 6:             # 6 位数字：按 symbol 找
        row = db.query_one(
            "SELECT ts_code, name FROM stock_basic WHERE symbol=%s AND list_status='L' "
            "LIMIT 1", (up,))
        if row:
            return {"ts_code": row["ts_code"], "name": row["name"] or "", "candidates": []}
        return {"ts_code": up, "name": "", "candidates": []}

    # 名称：先精确、再模糊；**模糊必须唯一**，多义一律把候选交回去
    # ⚠ 用户输的 `%` `_` 是**字面字符**，不是通配符（`601318` 没事，但「ST%」这种
    #   输入会把整张表捞回来）—— 转义掉，用 MySQL 默认的 `\` 转义符。
    like = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    cols = "ts_code, symbol, name, industry"
    rows = db.query(
        f"SELECT {cols} FROM stock_basic WHERE list_status='L' AND name=%s "
        f"ORDER BY ts_code LIMIT {int(limit)}", (q,))
    if not rows:
        rows = db.query(
            f"SELECT {cols} FROM stock_basic WHERE list_status='L' AND name LIKE %s "
            f"ORDER BY ts_code LIMIT {int(limit)}", (f"%{like}%",))
    if len(rows) == 1:
        r = rows[0]
        return {"ts_code": r["ts_code"], "name": r["name"] or "", "candidates": []}
    return {"ts_code": "", "name": "", "candidates": [
        {"ts_code": r["ts_code"], "symbol": r["symbol"], "name": r["name"],
         "industry": r["industry"]} for r in rows]}


def list_universes() -> list[dict]:
    """可选股票池列表。"""
    out = [{"code": "all", "name": "全部A股（不含退市/暂停）", "count": None}]
    rows = db.query(
        "SELECT index_code, COUNT(*) c FROM index_member GROUP BY index_code ORDER BY c DESC"
    )
    idx_names = {
        "000300.SH": "沪深300", "000905.SH": "中证500", "000852.SH": "中证1000",
        "000016.SH": "上证50", "399006.SZ": "创业板指", "000001.SH": "上证指数",
    }
    for r in rows:
        out.append({
            "code": r["index_code"],
            "name": idx_names.get(r["index_code"], r["index_code"]),
            "count": r["c"],
        })
    return out


def resolve_universe(universe: str | dict | list | None,
                     start: str | None = None, end: str | None = None) -> list[str]:
    """把股票池描述解析成 ts_code 列表。

    支持：
      "all"                       全部A股
      "000300.SH"                 指数成分
      "industry:银行"              行业
      {"codes": [...]}            自定义
      ["000001.SZ", ...]          自定义
      {"base": ..., "where": [...]}  条件池（逐日条件，见 core/universe.py）

    start/end 给定时，条件池会先用一条 SQL 把「区间内从没可能入池」的股票剔掉，
    再进入指标计算 —— 这一步结果与原集合**精确相等**，纯粹是提速。
    """
    from core import universe as uni

    if uni.is_cond(universe):
        spec = uni.parse(universe)
        codes = resolve_universe(spec["base"])
        if start and end:
            presel = uni.preselect_codes(spec, start, end)
            if presel is not None:
                keep = set(presel)
                codes = [c for c in codes if c in keep]
        return codes

    if universe is None or universe == "" or universe == UNIVERSE_ALL:
        rows = db.query(
            "SELECT ts_code FROM stock_basic WHERE list_status='L' ORDER BY ts_code"
        )
    elif isinstance(universe, dict) and universe.get("codes"):
        return [c.strip().upper() for c in universe["codes"] if c.strip()]
    elif isinstance(universe, list):
        return [str(c).strip().upper() for c in universe if str(c).strip()]
    elif isinstance(universe, str) and universe.startswith("industry:"):
        kw = universe.split(":", 1)[1]
        rows = db.query(
            "SELECT ts_code FROM stock_basic WHERE list_status='L' AND industry LIKE %s",
            (f"%{kw}%",),
        )
    elif isinstance(universe, str) and universe.startswith("codes:"):
        return [c.strip().upper() for c in universe.split(":", 1)[1].split(",") if c.strip()]
    else:
        rows = db.query(
            "SELECT DISTINCT ts_code FROM index_member WHERE index_code=%s", (universe,)
        )
        if not rows:
            rows = db.query(
                "SELECT ts_code FROM stock_basic WHERE list_status='L' ORDER BY ts_code"
            )
    return [r["ts_code"] for r in rows]


def universe_label(universe) -> str:
    from core import universe as uni
    if uni.is_cond(universe):
        return uni.label(universe)
    if universe in (None, "", "all"):
        return "全部A股"
    if isinstance(universe, list):
        return f"自定义({len(universe)}只)"
    if isinstance(universe, dict) and universe.get("codes"):
        return f"自定义({len(universe['codes'])}只)"
    if isinstance(universe, str) and universe.startswith("industry:"):
        return f"行业:{universe.split(':', 1)[1]}"
    if isinstance(universe, str) and universe.startswith("codes:"):
        return f"自定义({len(universe.split(':', 1)[1].split(','))}只)"
    for u in list_universes():
        if u["code"] == universe:
            return u["name"]
    return str(universe)


# ---------------------------------------------------------------------------
# 交易日
# ---------------------------------------------------------------------------

def get_trade_dates(start: str | None = None, end: str | None = None,
                    exchange: str = "SSE") -> list[str]:
    sql = "SELECT cal_date FROM trade_cal WHERE exchange=%s AND is_open=1"
    args: list = [exchange]
    if start:
        sql += " AND cal_date>=%s"
        args.append(_d(start))
    if end:
        sql += " AND cal_date<=%s"
        args.append(_d(end))
    sql += " ORDER BY cal_date"
    rows = db.query(sql, args)
    return [r["cal_date"].strftime("%Y-%m-%d") for r in rows]


def latest_trade_date(before: str | None = None) -> str | None:
    sql = ("SELECT MAX(trade_date) d FROM daily_kline"
           + (" WHERE trade_date<=%s" if before else ""))
    v = db.scalar(sql, (_d(before),) if before else None)
    return v.strftime("%Y-%m-%d") if v else None


def _d(v) -> str:
    if isinstance(v, (dt.date, dt.datetime)):
        return v.strftime("%Y-%m-%d")
    return str(v).replace("/", "-")[:10]


# ---------------------------------------------------------------------------
# 周线「已收盘」判据（唯一来源）
# ---------------------------------------------------------------------------
#
# 周线用 `resample('W-FRI')` 合成，标签是**该周的周五**（不一定是交易日）。
# 窗口截在周中时标签会落在未来 —— 那根周线其实只有周一~周二的数据，
# 而 `dropna(subset=['close'])` 挡不住它（它有 close）。
# 所以"这周走完没有"只能看该周**最后一个交易日**，不能拿标签跟日期比。
#
# 下面四个函数是全项目唯一的判据来源，`sync.build_weekly`（写库）、
# `compute_weekly_panel`（纯周线面板）、`load_panel`（日线的 w_ 过滤）都调它。

_WEEK_CACHE: dict = {"t": 0.0, "first": None, "last": None}
_WEEK_TTL = 300.0


def week_bounds(force: bool = False) -> tuple[pd.Series, pd.Series]:
    """每个自然周（W-FRI 标签）的第一个 / 最后一个交易日。

    返回 `(first, last)`，index 是与 `resample('W-FRI')` 一致的周标签（该周周五）。
    trade_cal 没数据时返回两个空 Series。
    """
    import time
    now = time.time()
    if (not force and _WEEK_CACHE["last"] is not None
            and now - _WEEK_CACHE["t"] < _WEEK_TTL):
        return _WEEK_CACHE["first"], _WEEK_CACHE["last"]
    dts = pd.to_datetime(pd.Series(get_trade_dates() or [], dtype="object"))
    if dts.empty:
        first = last = pd.Series(dtype="datetime64[ns]")
    else:
        ser = pd.Series(dts.values, index=dts.values)
        first = ser.resample("W-FRI").min()
        last = ser.resample("W-FRI").max()
    _WEEK_CACHE.update(first=first, last=last, t=now)
    return first, last


def complete_week_mask(labels: pd.Series, cutoff, since=None) -> pd.Series:
    """哪些周标签是「已收盘的完整周」。

    cutoff: 视角里的市场最新交易日 —— 回测传回测 end，同步传本地日线最新日期。
    since:  给定时还要求该周第一个交易日 >= since —— 否则取数窗口的起点会
            把某一周切成半周（open 不是周一的开盘价）。
    trade_cal 缺失时全部放行：宁可多写一根，也别把整张周线表清空。
    """
    first, last = week_bounds()
    if last.empty or not len(labels):
        return pd.Series(True, index=labels.index)
    ts = pd.to_datetime(labels)
    c = pd.Timestamp(cutoff)
    m = pd.to_datetime(labels.map(last.to_dict()), errors="coerce")
    ok = m.notna() & (m <= c)
    # trade_cal 覆盖不到的老周（m 是 NaT）：退回"标签 <= cutoff 就算已收盘"，
    # 别因为日历少拉了几年就把整段历史周线判没了。
    ok |= m.isna() & (ts <= c)
    if since is not None and not first.empty:
        f = pd.to_datetime(labels.map(first.to_dict()), errors="coerce")
        ok &= f.isna() | (f >= pd.Timestamp(since))
    return ok


def is_week_close_day(date) -> bool:
    """date 是否为其所在自然周的最后一个交易日（即该周已收盘）。"""
    d = pd.Timestamp(date)
    mon = (d - pd.Timedelta(days=d.weekday())).normalize()
    ds = get_trade_dates(mon.strftime("%Y-%m-%d"),
                         (mon + pd.Timedelta(days=4)).strftime("%Y-%m-%d"))
    return bool(ds) and ds[-1] == d.strftime("%Y-%m-%d")


def last_week_close_day(before=None) -> str | None:
    """<= before 的最近一个「周最后一个交易日」。"""
    _, last = week_bounds()
    s = last.dropna() if not last.empty else last
    if before is not None and len(s):
        s = s[s <= pd.Timestamp(before)]
    return s.max().strftime("%Y-%m-%d") if len(s) else None


# ---------------------------------------------------------------------------
# K 线读取 + 复权
# ---------------------------------------------------------------------------

_BASIC_COLS = ("turnover_rate, volume_ratio, pe, pb, ps, total_mv, circ_mv")

# K 线表里的数值列。**在 SQL 层 CAST 成 DOUBLE 再取**：
# `daily_kline` 的价格列是 DECIMAL，pymysql 会返回 Decimal 对象 → pandas 只能
# 存成 object 列 → 必须逐列 `to_numeric`（全市场实测 1.3s/批 ≈ 17s，白白花掉）。
# 直接让 MySQL 给 DOUBLE 就是 float64，这一步整个省掉；4 位小数的价格用 double
# 表示与 Decimal 逐值等价（回测本来就在 float64 上跑）。
_KLINE_NUM_COLS = ("open", "high", "low", "close", "pre_close", "change_amt",
                   "pct_chg", "vol", "amount", "adj_factor")
_KLINE_SEL = ", ".join(f"CAST({c} AS DOUBLE) AS {c}" for c in _KLINE_NUM_COLS)


def _load_moneyflow(ts_codes: list[str], start: str, end: str) -> pd.DataFrame | None:
    """读资金流向原始列（万元/手）。表不存在或没同步过时返回 None，安静降级。

    只在条件/评分真的引用 mf_* 字段时才会被调用 —— 多读 18 列 × 480 万行
    对没用到资金数据的策略是纯浪费。
    """
    frames = []
    for i in range(0, len(ts_codes), 400):
        part = ts_codes[i:i + 400]
        ph = ",".join(["%s"] * len(part))
        sql = (f"SELECT ts_code, trade_date, {','.join(ind.MF_RAW_COLS)} FROM money_flow "
               f"WHERE ts_code IN ({ph}) AND trade_date BETWEEN %s AND %s")
        try:
            rows = db.query(sql, part + [_d(start), _d(end)])
        except Exception as e:  # noqa: BLE001
            _log_mk.warning("读取 money_flow 失败，按无资金数据继续: %s", e)
            return None
        if rows:
            frames.append(pd.DataFrame(rows))
    if not frames:
        return None
    mdf = pd.concat(frames, ignore_index=True)
    mdf["trade_date"] = pd.to_datetime(mdf["trade_date"])
    return mdf


def load_kline_long(ts_codes: list[str], start: str, end: str,
                    freq: str = "daily", with_basic: bool = True,
                    with_moneyflow: bool = False) -> pd.DataFrame:
    """批量读取长表 K 线（未复权）。"""
    if not ts_codes:
        return pd.DataFrame()
    table = "daily_kline" if freq == "daily" else "weekly_kline"
    frames = []
    # 分批 IN 查询，避免 SQL 过长
    for i in range(0, len(ts_codes), 400):
        part = ts_codes[i:i + 400]
        ph = ",".join(["%s"] * len(part))
        sql = (f"SELECT ts_code, trade_date, {_KLINE_SEL} FROM {table} "
               f"WHERE ts_code IN ({ph}) AND trade_date BETWEEN %s AND %s")
        rows = db.query(sql, part + [_d(start), _d(end)])
        if rows:
            frames.append(pd.DataFrame(rows))
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    # SQL 已 CAST 成 DOUBLE，正常拿到的就是 float64 —— 这一步只兜底
    # （换驱动/换取数路径时仍可能拿到 Decimal 的 object 列）。
    for c in _KLINE_NUM_COLS:
        if c in df.columns and df[c].dtype == object:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    if with_basic:
        basics = []
        for i in range(0, len(ts_codes), 400):
            part = ts_codes[i:i + 400]
            ph = ",".join(["%s"] * len(part))
            sql = (f"SELECT ts_code, trade_date, {_BASIC_COLS} FROM daily_basic "
                   f"WHERE ts_code IN ({ph}) AND trade_date BETWEEN %s AND %s")
            rows = db.query(sql, part + [_d(start), _d(end)])
            if rows:
                basics.append(pd.DataFrame(rows))
        if basics:
            bdf = pd.concat(basics, ignore_index=True)
            bdf["trade_date"] = pd.to_datetime(bdf["trade_date"])
            df = df.merge(bdf, on=["ts_code", "trade_date"], how="left")

    if with_moneyflow:
        mdf = _load_moneyflow(ts_codes, start, end)
        if mdf is not None:
            df = df.merge(mdf, on=["ts_code", "trade_date"], how="left")
    return df



def apply_adjust(g: pd.DataFrame, mode: str = "qfq") -> pd.DataFrame:
    """对单只股票做复权。g 需含 adj_factor，按日期升序。"""
    if mode in (None, "none", "") or "adj_factor" not in g.columns:
        return g
    g = g.copy()
    af = pd.to_numeric(g["adj_factor"], errors="coerce").fillna(1.0)
    if (af == 1.0).all():
        return g
    if mode == "hfq":
        ratio = af
    else:  # qfq：以最后一根为基准
        ratio = af / float(af.iloc[-1])
    for c in ("open", "high", "low", "close", "pre_close"):
        if c in g.columns:
            g[c] = g[c] * ratio
    return g


# 附加到日线面板上的周线字段前缀
WEEKLY_ALWAYS = {"w_close", "w_pct_chg", "w_trade_date"}


def load_panel(ts_codes: list[str], start: str, end: str, freq: str = "daily",
               adjust: str = "qfq", indicators_cfg: dict | None = None,
               with_basic: bool = True, weekly: bool = True,
               weekly_fields: set[str] | None = None,
               only: set[str] | None = None) -> dict[str, pd.DataFrame]:
    """返回 {ts_code: 含指标列的 DataFrame}，已按日期升序。

    weekly=True 且 freq='daily' 时，为日线面板附加 `w_` 前缀的周线字段
    （映射到"最近一根已收盘的周K"），用于"周线过滤 + 日线触发"的多周期策略。
    weekly_fields 给定时只保留这些 w_ 字段（省内存）。
    only 给定时只计算策略实际引用的指标（大幅提速）；引用到 mf_*（或 w_mf_*）时
    才会额外读 money_flow 表。
    """
    need_mf = ind.needs_moneyflow(only) or ind.needs_moneyflow(
        {n[2:] for n in (weekly_fields or ())})
    # 筹码/两融：引用 hold_*/rz_*/rq_*/rzrq_*/ht_*（或它们的 w_ 版本）才读那三张表。
    # 与 money_flow 一样，"只算技术指标"的策略完全不该付这份 IO。
    need_chips = ch.needs_chips(only) or ch.needs_chips(
        {n[2:] for n in (weekly_fields or ())})
    long = load_kline_long(ts_codes, start, end, freq=freq, with_basic=with_basic,
                           with_moneyflow=need_mf)
    if long.empty:
        return {}
    cdata = ch.load(ts_codes, start, end) if need_chips else None

    stock_map = get_stock_map()
    out: dict[str, pd.DataFrame] = {}
    need_weekly = bool(weekly and freq == "daily" and weekly_fields)
    weekly_only = ({n[2:] for n in weekly_fields} | {"close", "open", "high", "low",
                                                     "vol", "amount", "pre_close"}
                   ) if need_weekly else None

    for code, g in long.groupby("ts_code", sort=False):
        g = g.sort_values("trade_date").reset_index(drop=True)
        # 筹码要在复权与重采样**之前**贴进来：周线那根靠 resample_weekly 的 "last"
        # 取"当周最后一个交易日的值"，先贴才能带上。
        if cdata is not None:
            g = ch.attach(cdata, g, code)
        g = apply_adjust(g, adjust)
        info = stock_map.get(code, {})
        name = info.get("name", "")
        d = ind.compute(g, indicators_cfg, ts_code=code, name=name, only=only)
        d["industry"] = info.get("industry", "")
        d["market"] = info.get("market", "")
        d["list_date"] = info.get("list_date")
        d["is_st"] = 1 if "ST" in name.upper() else 0
        if need_weekly:
            wk = resample_weekly(g)
            if len(wk) >= 2:
                # 丢掉最后一根未收盘的周线：它的标签是本周周五（可能还没到），
                # 数据只到窗口末端 —— merge_asof 本来就取不到它，这里显式去掉，
                # 免得面板里躺一根"半周线"让人误读。
                wk = wk[complete_week_mask(wk["trade_date"], end).to_numpy()]
                wdf = ind.compute(wk, indicators_cfg, ts_code=code, name=name,
                                  only=weekly_only)
                d = _merge_weekly(d, wdf, weekly_fields)
        out[code] = d
    return out


def _merge_weekly(daily: pd.DataFrame, weekly: pd.DataFrame,
                  keep: set[str] | None) -> pd.DataFrame:
    """把周线指标按 asof 方式贴到日线上（严格不使用未来数据）。

    周线的 trade_date 是 **W-FRI 分箱标签**（该周的周五那天，不一定是交易日；
    窗口截断在周中时标签会落在未来，例如窗口只到周二，那根周线标签是该周周五）。
    对日线 t 只有「标签 <= t」的周线能被 asof 取到，于是：
      * t 落在该周最后交易日（通常是周五）：拿到**当周**（已完整）的周线；
      * t 在周中：只能拿到**上一周**的周线。
    两个方向都不看未来；代价是周中拿不到当周数据 —— 这是刻意的。
    """
    w = weekly.copy()
    w = w[[c for c in w.columns if not c.startswith("_")]]
    w = w.rename(columns={c: f"w_{c}" for c in w.columns if c != "trade_date"})
    if keep is not None:
        cols = ["trade_date"] + [c for c in w.columns
                                 if c in (keep | WEEKLY_ALWAYS)]
        w = w[cols]
    w = w.sort_values("trade_date")
    d = daily.sort_values("trade_date")
    merged = pd.merge_asof(d, w, on="trade_date", direction="backward")
    merged = merged.rename(columns={"w_trade_date": "w_last_date"})
    return merged


def resample_weekly(df: pd.DataFrame) -> pd.DataFrame:
    """日线 -> 周线（按自然周，周五为周末）。

    资金流向原始列按周**求和**（净额类相加才有意义），这样周线面板上重算
    `w_mf_*` 派生字段（净额、占比、5 周累计）口径与日线一致。

    筹码/两融列按周取 **last**（当周最后一个交易日的值）—— 它们是"截至某日的
    状态量"（最新一期户数、融资余额、近 30 日净增持），**不能求和**：
    把 5 天的融资余额加起来是个没有含义的数。
    """
    if df.empty:
        return df
    d = df.copy()
    d["trade_date"] = pd.to_datetime(d["trade_date"])
    d = d.sort_values("trade_date").set_index("trade_date")
    agg = {
        "open": "first", "high": "max", "low": "min", "close": "last",
        "vol": "sum", "amount": "sum", "adj_factor": "last",
    }
    for c in ind.MF_RAW_COLS:                    # 资金流向：周内累加
        if c in d.columns:
            agg[c] = "sum"
    for c in ch.CHIPS_DERIVED:                   # 筹码/两融：取当周最后一个交易日的值
        if c in d.columns:
            agg[c] = "last"
    agg = {k: v for k, v in agg.items() if k in d.columns}
    out = d.resample("W-FRI").agg(agg).dropna(subset=["close"]).reset_index()
    out["pre_close"] = out["close"].shift(1)
    out["change_amt"] = out["close"] - out["pre_close"]
    out["pct_chg"] = (out["close"] / out["pre_close"].replace(0, np.nan) - 1) * 100
    return out


def compute_weekly_panel(ts_codes: list[str], start: str, end: str,
                         adjust: str = "qfq",
                         indicators_cfg: dict | None = None,
                         only: set[str] | None = None) -> dict[str, pd.DataFrame]:
    """合成周线面板（含指标），用于纯周线策略。"""
    long = load_kline_long(ts_codes, start, end, freq="daily", with_basic=False,
                           with_moneyflow=ind.needs_moneyflow(only))
    if long.empty:
        return {}
    cdata = ch.load(ts_codes, start, end) if ch.needs_chips(only) else None
    stock_map = get_stock_map()
    out = {}
    for code, g in long.groupby("ts_code", sort=False):
        g = g.sort_values("trade_date").reset_index(drop=True)
        if cdata is not None:
            g = ch.attach(cdata, g, code)
        g = apply_adjust(g, adjust)
        wk = resample_weekly(g)
        if not wk.empty:
            # 只保留已收盘的完整周：回测/选股的 end 落在本周中间时，最后一根
            # 是"周一~今天"的半周线，标签还挂在未来的周五上，不能拿它出信号。
            wk = wk[complete_week_mask(wk["trade_date"], end).to_numpy()]
        if wk.empty or len(wk) < 5:
            continue
        info = stock_map.get(code, {})
        wd = wk.copy()
        wd["trade_date"] = pd.to_datetime(wd["trade_date"])
        d = ind.compute(wd, indicators_cfg, ts_code=code, name=info.get("name", ""),
                        only=only)
        d["industry"] = info.get("industry", "")
        d["market"] = info.get("market", "")
        d["is_st"] = 1 if "ST" in info.get("name", "").upper() else 0
        out[code] = d
    return out


# ---------------------------------------------------------------------------
# 股票池过滤（ST / 上市天数 / 成交额 / 板块）
# ---------------------------------------------------------------------------

def apply_pool_filters(panel: dict[str, pd.DataFrame], filt: dict) -> dict[str, pd.DataFrame]:
    """股票的「静态属性」过滤（ST / 板块 / 上市天数）。

    注意：最小日成交额 min_amount 是**逐日**条件，不在这里处理 ——
    它由 strategy.compute_pool() 逐日求值。
    （旧版本在这里写了个 `_amt_ok` 标记，但没有任何地方消费它，导致该过滤静默失效。）
    """
    if not filt:
        return panel
    stock_map = get_stock_map()
    keep = {}
    ex_st = filt.get("exclude_st", True)
    ex_kcb = filt.get("exclude_kcb", False)
    ex_cyb = filt.get("exclude_cyb", False)
    ex_bj = filt.get("exclude_bj", True)
    min_list_days = int(filt.get("min_list_days") or 0)

    today = pd.Timestamp.today()
    for code, d in panel.items():
        if not len(d):
            continue
        if ex_bj and code.endswith(".BJ"):
            continue
        num = code[:6]
        if ex_kcb and num.startswith("688"):
            continue
        if ex_cyb and num.startswith(("300", "301")):
            continue
        info = stock_map.get(code, {})
        name = info.get("name", "")
        if ex_st and "ST" in name.upper():
            continue
        if min_list_days:
            ld = info.get("list_date")
            if ld and (today - pd.Timestamp(ld)).days < min_list_days:
                continue
        keep[code] = d
    return keep


_STATS_CACHE: dict = {"t": 0.0, "v": None}
STATS_TTL = 60.0


def data_stats(force: bool = False) -> dict:
    """数据库概览（带 60 秒缓存，避免大表 COUNT 拖慢页面）。"""
    import time
    now = time.time()
    if not force and _STATS_CACHE["v"] is not None and now - _STATS_CACHE["t"] < STATS_TTL:
        return dict(_STATS_CACHE["v"])

    out = {}
    out["stock_basic"] = db.scalar("SELECT COUNT(*) FROM stock_basic") or 0
    out["daily_rows"], exact1 = db.safe_count("daily_kline")
    out["weekly_rows"], exact2 = db.safe_count("weekly_kline", 800)
    out["basic_rows"], exact3 = db.safe_count("daily_basic")
    try:
        out["mf_rows"], exact4 = db.safe_count("money_flow")
    except Exception:  # noqa: BLE001  表还没建（老库）时不该让数据页挂掉
        out["mf_rows"], exact4 = 0, True
    # 筹码与两融：股东户数 / 融资融券 / 股东增减持（同样是"可选增强数据"，缺表不算错）
    exacts = [True]
    for key, table in (("holder_rows", "holder_number"),
                       ("margin_rows", "margin_detail"),
                       ("htrade_rows", "holder_trade")):
        try:
            out[key], ex = db.safe_count(table)
        except Exception:  # noqa: BLE001
            out[key], ex = 0, True
        exacts.append(ex)
    out["rows_exact"] = bool(exact1 and exact2 and exact3 and exact4 and all(exacts))
    out["daily_codes"] = db.scalar(
        "SELECT COUNT(*) FROM stock_basic WHERE list_status='L'") or 0
    r = db.query_one("SELECT MIN(trade_date) a, MAX(trade_date) b FROM daily_kline")
    out["daily_start"] = _d(r["a"]) if r and r["a"] else None
    out["daily_end"] = _d(r["b"]) if r and r["b"] else None
    out["trade_cal_rows"] = db.scalar("SELECT COUNT(*) FROM trade_cal") or 0
    cal_end = db.scalar("SELECT MAX(cal_date) FROM trade_cal")
    out["trade_cal_end"] = _d(cal_end) if cal_end else None
    out["strategy_count"] = db.scalar("SELECT COUNT(*) FROM strategy") or 0
    out["run_count"] = db.scalar("SELECT COUNT(*) FROM backtest_run") or 0
    _STATS_CACHE["t"] = now
    _STATS_CACHE["v"] = dict(out)
    return out


# ---------------------------------------------------------------------------
# 涨跌停家数统计（总览页用）
# ---------------------------------------------------------------------------
# 板块判定与涨跌幅必须和 `indicators._limit_pct` 用**同一套规则**，否则
# 「总览的涨跌停家数」和「策略里的 limit_up / limit_down 字段」会各算一套、互相打架：
#   688(科创) / 300,301(创业) → 20% · 北交所(4/8/9 开头) → 30% · ST(名称含 ST) → 5% · 其余主板 → 10%
# 用 LEFT() 而不是 LIKE，避免 SQL 里出现 % 被 pymysql 当占位符转义。
# 板块用整数编码 bid（1=主板 2=创业板 3=科创板 4=北交所），便于 Python 侧透视。
#
# 性能（实测 3 个月区间，均为真机数据）：
#   ① **必须靠覆盖索引 `idx_limit_stat`**（见 core/db.py INDEX_MIGRATIONS）。
#      没有它 → 退化成全表顺序扫 340 万行 ≈ 10s；
#      有它 → `Using index` 走 index-only 扫描 ≈ 0.25s。
#      注意**别用 FORCE INDEX (idx_trade_date)** —— 那个索引不含 close/pre_close，
#      要回表做 36 万次随机读，实测反而 26s，比全表扫还慢。
#   ② **别把各板块写成 10 个 SUM 塞进一条 SQL**：派生表会被合并进外层，
#      `pct`/`lim` 表达式在每个 SUM 里重复求值 → 10.3s；
#      改成按 (日期, 板块, 是否ST) 分组只做 2 个 SUM、再在 Python 透视 → 2.2s。
#
# 注意：**ST 是「标记」不是「板块」**，与主板/创业板等互相重叠 ——
# 所以「总计」≠ 各板块相加；一只 ST 股既进「ST」也进它所在的板块。
BOARD_ORDER = ("主板", "创业板", "科创板", "ST", "总计")
BID_NAME = {1: "主板", 2: "创业板", 3: "科创板", 4: "北交所"}

_LIMIT_INNER_SQL = """
  SELECT k.trade_date,
         CASE WHEN LEFT(k.ts_code, 3) = '688' THEN 3
              WHEN LEFT(k.ts_code, 3) IN ('300', '301') THEN 2
              WHEN LEFT(k.ts_code, 1) IN ('4', '8', '9') THEN 4
              ELSE 1 END                                            AS bid,
         IF(LOCATE('ST', UPPER(COALESCE(b.name, ''))) > 0, 1, 0)     AS is_st,
         CASE WHEN LEFT(k.ts_code, 3) = '688' THEN 20
              WHEN LEFT(k.ts_code, 3) IN ('300', '301') THEN 20
              WHEN LEFT(k.ts_code, 1) IN ('4', '8', '9') THEN 30
              WHEN LOCATE('ST', UPPER(COALESCE(b.name, ''))) > 0 THEN 5
              ELSE 10 END                                            AS lim,
         (k.close / k.pre_close - 1) * 100                           AS pct
  FROM daily_kline k
  LEFT JOIN stock_basic b ON b.ts_code = k.ts_code
  WHERE k.trade_date BETWEEN %s AND %s AND k.pre_close > 0
"""

_LIMIT_AGG_SQL = """
SELECT t.trade_date, t.bid, t.is_st,
       SUM(t.pct >= t.lim - 0.15) AS up,
       SUM(t.pct <= -t.lim + 0.15) AS dn
FROM (""" + _LIMIT_INNER_SQL + """) t
GROUP BY t.trade_date, t.bid, t.is_st
ORDER BY t.trade_date
"""


def limit_stats(start: str, end: str) -> dict:
    """按交易日统计涨停 / 跌停家数，分主板 / 创业板 / 科创板 / ST / 总计。

    返回 {dates: [...], boards: [...], up: {板块: [...]}, down: {板块: [...]}, latest: {...}}
    —— 这种「并列数组」结构前端拿来直接喂 ECharts，不用再 reshape。

    只按 (日期, 板块, 是否ST) 分组做 2 个 SUM（约 500 行），再在 Python 里透视。
    别把 10 个板块 SUM 写进一条 SQL：那样 `pct` / `lim` 表达式会在每个 SUM 里被重复求值，
    实测反而慢 5 倍（10.3s → 2.2s）。
    """
    rows = db.query(_LIMIT_AGG_SQL, (start, end)) or []
    dates: list[str] = []
    acc: dict[str, dict] = {}
    for r in rows:
        d = _d(r["trade_date"])
        rec = acc.get(d)
        if rec is None:
            rec = acc[d] = {}
            dates.append(d)
        up, dn = int(r["up"] or 0), int(r["dn"] or 0)
        # 每个分组的行都计入「总计」；北交所只进总计，不单列
        rec["up_总计"] = rec.get("up_总计", 0) + up
        rec["dn_总计"] = rec.get("dn_总计", 0) + dn
        name = BID_NAME.get(r["bid"])
        if name in BOARD_ORDER:            # 主板 / 创业板 / 科创板
            rec["up_" + name] = rec.get("up_" + name, 0) + up
            rec["dn_" + name] = rec.get("dn_" + name, 0) + dn
        if r["is_st"]:                     # ST 是标记，与上面的板块**重叠**计数
            rec["up_ST"] = rec.get("up_ST", 0) + up
            rec["dn_ST"] = rec.get("dn_ST", 0) + dn

    up = {b: [] for b in BOARD_ORDER}
    down = {b: [] for b in BOARD_ORDER}
    for d in dates:
        rec = acc[d]
        for b in BOARD_ORDER:
            up[b].append(int(rec.get("up_" + b, 0)))
            down[b].append(int(rec.get("dn_" + b, 0)))
    latest = None
    if dates:
        latest = {"trade_date": dates[-1],
                  "up": {b: up[b][-1] for b in BOARD_ORDER},
                  "down": {b: down[b][-1] for b in BOARD_ORDER}}
    return {"start": start, "end": end, "dates": dates, "boards": list(BOARD_ORDER),
            "up": up, "down": down, "latest": latest}
