# -*- coding: utf-8 -*-
"""选股：按策略在指定交易日从股票池中筛出候选标的。"""
from __future__ import annotations

import datetime as dt
import time

import numpy as np
import pandas as pd

from core import db, expr, indicators as ind, log, market, strategy as st, universe as uni
from core import backtest as bt
from core import chips as ch
from core import concept as cc
from core import openstat as ows

_log = log.get(__name__)

SCREEN_LOOKBACK_CAL_DAYS = 620   # 回溯自然日，保证 250 根 K 线可算指标
BATCH_SIZE = 400                 # 分批加载只数，控制内存占用

VIEW_FREQS = ("daily", "weekly")
"""个股诊断页视图周期。周期只有这两种，别的值一律按日线处理（前端下拉同源）。"""

FIXED_COLS = {"close", "pct_chg", "amount", "turnover_rate", "total_mv", "mf_main_net"}
"""结果表里**已经有固定列**的字段 —— 自动加列时跳过，免得出两遍。
（这些是每只票都想看的基本项；其余字段"条件里提到才占列"，见 `screen()`。）"""

# 结果表**固定列**里需要「现算出来」的字段：主力净额三兄弟走 money_flow 大表
# （`(buy_lg+buy_elg)-(sell_lg+sell_elg)`，见 indicators）。它们**不能并进 `only`** ——
# 全市场都读一遍 money_flow 只为显示几列，实测单批多花 2.5s（全市场 ~30s）。
# 改成**命中之后**对少数命中票补算（见 screen() 末尾），口径与初筛同一个 load_panel。
MF_DISPLAY_FIELDS = {"mf_main_net", "mf_main_ratio", "mf_net"}

# 命中明细里想让人「顺手看到」的字段（前端 screen.html 的 DETAIL_FIELDS 是同一份意图）。
# ⚠ 这份清单**不是加载/计算清单** —— 曾经它被并进 `only`，结果 `indicators.compute`
# 的按需裁剪整个失效（`want_pre("boll")` 之类全为真 → 整套指标白算一遍，实测全市场
# 慢 50%）。现在只有**策略真正引用的字段**才进 `only`（`st.required_daily_fields`），
# 这里留作说明：这些字段的值在明细里按需出现，缺了就是策略没用到，不是数据缺失。
DETAIL_FIELDS = {
    "close", "pre_close", "open", "high", "low", "pct_chg",
    "boll_upper", "boll_mid", "boll_lower", "boll_pos", "boll_width",
    "ma5", "ma10", "ma20", "ma60", "vma5", "vma10", "vma20",
    "rsi6", "rsi12", "kdj_k", "kdj_d", "kdj_j",
    "macd_dif", "macd_dea", "macd_hist", "pos20", "pos60",
    "turnover_rate", "volume_ratio", "total_mv",
}


def _lookback_start(trade_date: str, bars: int = 300, freq: str = "daily") -> str:
    d = pd.Timestamp(trade_date)
    days = int(bars * (7 / 5 if freq == "daily" else 7)) + 40
    return (d - pd.Timedelta(days=days)).strftime("%Y-%m-%d")


def resolve_trade_date(trade_date: str | None) -> str | None:
    if trade_date:
        return trade_date
    return market.latest_trade_date()


# ---------------------------------------------------------------------------
# 字段取值参考（「可用字段参考」里点「示例」看真实值）
#
# 需求原话：「选股的时候填写条件，有些字段的值我不知道该填多少，希望在可用字段参考
# 那块加个示例链接，点击弹出示例弹窗，展示可用字段及其值」。
#
# ⚠ 值**必须与选股同源**：这里走的就是 `market.load_panel`（前复权 / 周线 asof /
#   筹码 / 资金流全在那一条路上）。自己写条 SQL 去统计会当场养出第二个口径 ——
#   价格类字段最容易露馅：选股里的 close 是**前复权**的，表里存的是未复权。
# ⚠ 只取**样本股**、不跑全市场：全市场 5900 只一遍要 80 秒，弹窗等不起。
#   样本 = 当日成交额前 N 只，回答「这字段大概是什么量级、该填多少」足够了；
#   样本口径要写在弹窗上，别让人以为那是全市场分布。
# ---------------------------------------------------------------------------
SAMPLE_STOCKS = 120      # 样本股数。实测耗时与它近似线性：80 只 ≈1s、120 只 ≈2s、
                         # 200 只 ≈4s（本机 MySQL）。120 只 × 20 天 = 2400 个值，
                         # 看分位足够了；再往上加只是让人多等。
SAMPLE_STOCKS_CHIPS = 60
"""筹码 / 两融 / 增减持字段的样本股数（比普通字段少一半）。

⚠ 这些字段的值来自另外三张表（`holder_*` / `margin_detail` / `holder_trade`），
**读表本身**才是大头，且耗时随样本只数近似线性：`ht_net30` 40 只 1.3s、80 只 5.5s、
120 只 9.4s；`rz_ye` 同量级。60 只 × 20 天 = 1200 个值，看分位够用，再往上纯粹是
让人对着转圈等。"""
SAMPLE_DAYS = 20         # 分布用每只最近多少个交易日的值
SAMPLE_BASIC_FIELDS = {"turnover_rate", "volume_ratio", "pe", "pb", "ps",
                       "total_mv", "circ_mv"}
"""值来自 `daily_basic` 的字段。只有引用到它们才去读那张表（单次 ~1.2s）。"""
_SAMPLE_TTL = 600.0      # 秒：同一天同一字段反复点不该反复算
_SAMPLE_CACHE_MAX = 60
_SAMPLE_CACHE: dict[tuple, tuple[float, dict]] = {}

# 分位点 → 中文名。0/100 收两端，中间给 4 档 —— 就是「我该填多少」的答案。
_SAMPLE_QS = ((0, "最小值"), (10, "10 分位"), (25, "25 分位"), (50, "中位数"),
              (75, "75 分位"), (90, "90 分位"), (100, "最大值"))


def _sample_codes(trade_date: str, limit: int) -> tuple[list[str], int]:
    """样本股：当天有行情的票里**等距抽** N 只 → (样本, 当天总只数)。

    ⚠ 不用「成交额前 N 只」：那要 `ORDER BY amount`，而 `daily_kline` 上只有
    `trade_date` 索引（amount 得回表排 5500 行），实测 1.2~1.4s —— 只为挑样本
    花掉弹窗三分之一的时间。等距抽样只取 `ts_code` 一列，走覆盖索引 9ms。
    附带好处：等距是按代码序铺开的，沪深主板/创业板/科创板都覆盖到，比
    「成交额头部」（清一色活跃大盘股）更接近全市场该字段的量级分布。
    """
    d = pd.Timestamp(trade_date).strftime("%Y-%m-%d")
    codes = [r["ts_code"] for r in
             db.query("SELECT ts_code FROM daily_kline WHERE trade_date=%s", [d])]
    n = len(codes)
    if n <= limit:
        return codes, n
    step = n / float(limit)
    return [codes[int(i * step)] for i in range(int(limit))], n


def field_sample(name: str, trade_date: str | None = None,
                 stocks: int | None = None, days: int = SAMPLE_DAYS) -> dict:
    """某个可用字段的「实际取值参考」：分位分布 + 样例个股。

    走 `market.load_panel`，所以复权 / 周线 asof / 筹码 / 资金流的口径与选股完全一致。
    """
    name = str(name or "").strip()
    if name not in set(st.condition_field_names()):
        raise ValueError("未知字段：%s" % (name or "(空)"))
    weekly = name.startswith("w_")
    base = name[2:] if weekly else name
    if stocks is None:                      # 筹码类要读三张额外的表，样本减半
        stocks = SAMPLE_STOCKS_CHIPS if ch.needs_chips({base}) else SAMPLE_STOCKS
    end = resolve_trade_date(trade_date)
    if not end:
        raise ValueError("本地无行情数据，请先在「数据管理」同步")

    key = (name, end, int(stocks), int(days))
    hit = _SAMPLE_CACHE.get(key)
    if hit and time.time() - hit[0] < _SAMPLE_TTL:
        return hit[1]

    codes, total = _sample_codes(end, stocks)
    if not codes:
        raise ValueError("%s 没有行情数据" % end)
    start = _lookback_start(end, 300)
    # daily_basic 按需读：单次 ~1.2s，纯技术字段（ma20/rsi6/boll_*…）根本用不上它。
    panel = market.load_panel(codes, start, end, adjust="qfq",
                              with_basic=base in SAMPLE_BASIC_FIELDS,
                              weekly=weekly,
                              weekly_fields=({name} if weekly else None),
                              only={base})

    smap = market.get_stock_map()
    rank = {c: i for i, c in enumerate(codes)}     # 样本顺序，样例行沿用它
    chunks: list[np.ndarray] = []
    picked: list[tuple] = []
    for code, df in panel.items():
        if df is None or df.empty or name not in df.columns:
            continue
        s = pd.to_numeric(df[name], errors="coerce")
        tail = s.tail(int(days)).dropna()
        if len(tail):
            chunks.append(tail.to_numpy(dtype="float64"))
        # 样例行只认**正好落在 trade_date 那根**的值：停牌股拿更早一天顶上去会误导。
        dts = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d").to_numpy()
        pos = np.where(dts == end)[0]
        if len(pos):
            v = s.iloc[int(pos[-1])]
            if not pd.isna(v):
                picked.append((rank.get(code, 1 << 30), code,
                               smap.get(code, {}).get("name", ""), float(v)))
    used = len(chunks)
    del panel

    vals = np.concatenate(chunks) if chunks else np.zeros(0)
    picked.sort(key=lambda x: x[0])
    stats = [{"k": k, "v": float(np.nanpercentile(vals, q))} for q, k in _SAMPLE_QS] \
        if len(vals) else []
    out = {
        "name": name,
        "desc": (st.condition_weekly_field_help() if weekly
                 else st.condition_field_help()).get(name, ""),
        "weekly": weekly,
        "trade_date": end,
        "sample": {
            "stocks": used,
            "asked": int(stocks),
            "basis": "全市场 %d 只里等距抽 %d 只（其中 %d 只有该字段的值）"
                     % (total, int(stocks), used),
            "days": int(days),
            "count": int(len(vals)),
        },
        "stats": stats,
        "rows": [{"ts_code": c, "name": n, "value": v} for _, c, n, v in picked[:8]],
        "empty": not len(vals),
    }
    out["note"] = ("样本内最近 %d 个交易日都没有该字段的取值 —— 可能是这类数据还没同步"
                   "（见「数据管理」），或它按公告发布、样本期内本来就没有。" % int(days)) \
        if out["empty"] else \
        ("分位里每只股票各取最近 %d 个交易日（合计 %d 个值）；样例行是抽样里代码靠前的几只。"
         % (int(days), len(vals)))

    _SAMPLE_CACHE[key] = (time.time(), out)
    if len(_SAMPLE_CACHE) > _SAMPLE_CACHE_MAX:      # 简易上限，别让内存无限涨
        for k in sorted(_SAMPLE_CACHE, key=lambda k: _SAMPLE_CACHE[k][0])[:10]:
            _SAMPLE_CACHE.pop(k, None)
    return out


def screen(cfg: dict, universe=None, trade_date: str | None = None,
           limit: int = 300, save: bool = True,
           progress=None) -> dict:
    """执行选股，返回候选列表。分批加载，避免全市场扫描时内存爆掉。"""
    cfg = st.merge_config(cfg)
    # 股票池如果是「条件池」，把描述注入 cfg —— 后面的 compute_pool / required_daily_fields
    # 都从 cfg["_pool"] 读，这样 only= 裁剪也会把池子用到的指标（如 boll_upper）算上。
    if uni.is_cond(universe):
        cfg = {**cfg, "_pool": uni.parse(universe)}
    # 选股策略的条件抬成 entry（买卖策略自己不写选股条件了）——
    # 必须在取 freq / required_*_fields 之前，否则 w_ 类池子条件不会进加载清单。
    cfg = st.selection_cfg(cfg)
    freq = cfg["timeframe"]
    trade_date = resolve_trade_date(trade_date)
    if not trade_date:
        return {"error": "本地无行情数据，请先在「数据管理」同步", "items": []}
    # 周线策略只有"已收盘的完整周"可用。周中跑的话面板最后一根是上周五，
    # 而下面要的是 trade_date 当天那根 —— 匹配不上会静默返回 0 只，这里说清楚。
    if freq == "weekly" and not market.is_week_close_day(trade_date):
        last_wk = market.last_week_close_day(trade_date)
        return {"error": (f"{trade_date} 不是本周的最后一个交易日（本周还没收盘），"
                          f"纯周线策略没有可用的周K。等本周收盘后同步再跑，或把选股日"
                          f"改成最近一个周收盘日 {last_wk or '—'}。"),
                "items": []}

    codes = market.resolve_universe(universe, start=trade_date, end=trade_date)
    if not codes:
        return {"error": "股票池为空（静态池无成分，或条件池在这一天没有股票满足）", "items": []}

    bars = 150 if freq == "weekly" else 300
    start = _lookback_start(trade_date, bars=bars, freq=freq)
    only = st.required_daily_fields(cfg)
    # ⚠ 这里**只能**放策略真正引用的字段。`indicators.compute` 靠 `only` 决定算哪些
    # 指标组，多塞一个 `boll_pos` 就会把整套布林算一遍、多塞 `obv` 就整组算一遍 ——
    # 全市场扫描实测：`only` 从 36 个字段收到 9 个，单批 28.6s → 13.9s。
    # 展示用的 `MF_DISPLAY_FIELDS` 在命中后单独补算，见函数末尾。
    wf = st.required_weekly_fields(cfg)
    # 静态过滤项（剔 ST / 板块 / 上市天数）来自**选股策略**，不再读 cfg["pool"]
    pool = st.pool_filters(cfg)
    # 结果表要自动加的那几列 = 策略条件/评分/条件池里**真正引用到**的字段，
    # 减去已经有固定列的那几个。顺序沿用「在条件里出现的先后」，列头走 col_spec。
    # 只认已经算出来的列（`w_` 字段要 weekly=True 才有），不硬造。
    show = [n for n in st.referenced_fields(cfg) if n not in FIXED_COLS]

    items: list[dict] = []
    scanned = 0
    pool_skipped = 0
    n = len(codes)
    for i in range(0, n, BATCH_SIZE):
        part = codes[i:i + BATCH_SIZE]
        if freq == "weekly":
            panel = market.compute_weekly_panel(part, start, trade_date,
                                                indicators_cfg=cfg.get("indicators"),
                                                only=only)
        else:
            panel = market.load_panel(part, start, trade_date, adjust="qfq",
                                      indicators_cfg=cfg.get("indicators"),
                                      weekly=bool(wf), weekly_fields=wf, only=only)
        panel = market.apply_pool_filters(panel, pool)
        scanned += len(panel)

        for code, df in panel.items():
            if df is None or len(df) < 5:
                continue
            last_dates = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
            pos = np.where(last_dates.to_numpy() == trade_date)[0]
            if len(pos) == 0:
                continue
            i2 = int(pos[-1])
            # 条件池：当天不在池内直接出局（与 entry 同一时机求值）
            try:
                if not bool(st.compute_pool(df, cfg)[i2]):
                    pool_skipped += 1
                    continue
            except Exception:  # noqa: BLE001  池子条件异常时保守跳过
                pool_skipped += 1
                continue
            try:
                sig = st.compute_signals(df, cfg)
            except expr.ExprError:
                continue
            if not bool(sig["entry"].iloc[i2]):
                continue
            row = df.iloc[i2]
            sc = sig["score"].iloc[i2]
            item = {
                "ts_code": code,
                "name": row.get("_name", ""),
                "industry": row.get("industry", ""),
                "market": row.get("market", ""),
                "close": _f(row.get("close")),
                "pct_chg": _f(row.get("pct_chg")),
                "open": _f(row.get("open")),
                "pre_close": _f(row.get("pre_close")),
                "amount": _f(row.get("amount")),
                "turnover_rate": _f(row.get("turnover_rate")),
                "total_mv": _f(row.get("total_mv")),
                "volume_ratio": _f(row.get("volume_ratio")),
                "mf_main_net": _f(row.get("mf_main_net")),
                "mf_main_ratio": _f(row.get("mf_main_ratio")),
                "mf_net": _f(row.get("mf_net")),
                "score": None if pd.isna(sc) else round(float(sc), 4),
                "detail": explain(cfg, df, i2),
            }
            # 条件引用到的字段逐只带出来（含筹码/两融）。前后端都不写死字段名 ——
            # 列头与格式由 `indicators.col_spec` 给，前端只负责画。
            for k in show:
                if k in df.columns:
                    item[k] = _f(row.get(k))
            items.append(item)
        del panel
        if progress:
            progress(0.1 + 0.85 * min(1, (i + BATCH_SIZE) / max(n, 1)),
                     f"扫描 {min(i + BATCH_SIZE, n)}/{n}，已命中 {len(items)}")

    items.sort(key=lambda x: (-(x["score"] if x["score"] is not None else -1e18),
                              -abs(x["pct_chg"] or 0)))
    total_hits = len(items)
    if limit:
        items = items[:limit]

    _fill_display_fields(items, only, cfg, trade_date, start, freq)

    out = {
        "trade_date": trade_date,
        "timeframe": freq,
        "universe": market.universe_label(universe),
        "pool": uni.describe(universe),
        "universe_size": len(codes),
        "scanned": scanned,
        "pool_skipped": pool_skipped,
        "count": total_hits,
        "returned": len(items),
        "strategy_name": cfg.get("_name", ""),
        # 本次**实际用来排序**的表达式（= 选股页填的「排序表达式」，没填就是买卖策略的
        # score_expr）。结果表按它降序、`评分`列显示它的值 —— 回给前端只为把
        # 「这次是按什么排的」写在结果卡片上，别让人对着顺序猜。
        "sort_expr": cfg.get("score_expr") or "",
        # 表头/小数位/格式：只给**这批命中真有值**的列（可能因缺数据整列缺席）
        "extra_cols": ([ind.col_spec(k) for k in show if k in items[0]] if items else []),
        "items": items,
    }
    if progress:
        progress(1.0, f"命中 {total_hits} 只")
    return out


def _fill_display_fields(items: list[dict], only: set[str], cfg: dict,
                         trade_date: str, start: str, freq: str) -> None:
    """给命中结果补上「结果表固定列」里需要现算、但初筛没算的字段（主力净额三兄弟）。

    为什么分两步：那三个字段要从 money_flow（全市场数百万行）算出来，而初筛时
    **绝大多数策略根本不碰资金数据** —— 为了显示几列让全市场都读一遍不值当
    （实测单批 +2.5s / 全市场 +30s）。命中通常只有几十~几百只，补算很便宜。

    取数走的是**同一个** `market.load_panel` / `compute_weekly_panel`（与初筛同源），
    所以不会冒出第二份口径；补算失败不影响选股本身，那几列按空值处理并记日志。
    """
    lack = sorted(MF_DISPLAY_FIELDS - only)
    if not items or not lack:
        return
    codes = [it["ts_code"] for it in items]
    need = set(lack) | {"close"}     # close 保证面板非空，并能定位到 trade_date 那根
    try:
        if freq == "weekly":
            panel = market.compute_weekly_panel(codes, start, trade_date,
                                                indicators_cfg=cfg.get("indicators"),
                                                only=need)
        else:
            panel = market.load_panel(codes, start, trade_date, adjust="qfq",
                                      indicators_cfg=cfg.get("indicators"),
                                      weekly=False, only=need)
    except Exception as e:  # noqa: BLE001  只是展示列，缺了不影响选股结论
        _log.warning("补算结果表展示字段失败（那几列按空处理）: %s", e)
        return
    for it in items:
        df = panel.get(it["ts_code"])
        if df is None or df.empty:
            continue
        dts = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d").to_numpy()
        pos = np.where(dts == trade_date)[0]
        if not len(pos):
            continue
        row = df.iloc[int(pos[-1])]
        for k in lack:
            if k in df.columns:
                it[k] = _f(row.get(k))


def _f(v):
    try:
        if v is None or (isinstance(v, float) and v != v) or pd.isna(v):
            return None
        return round(float(v), 4)
    except (TypeError, ValueError):
        return None


def explain(cfg: dict, df: pd.DataFrame, i: int, max_rows: int = 6) -> dict:
    """解释第 i 根 K 线为何命中：逐条给出结果与相关字段数值。"""
    cfg = st.merge_config(cfg)
    ns = st._namespace(df)
    n = len(df)
    out: dict = {"conditions": [], "values": {}}
    groups = [("entry", "选股条件"), ("entry_any", "或条件"),
              ("weekly_filter", "周线过滤")]
    # 条件是「选股策略」带进来的，就在分组名里写明来源 —— 否则用户会以为
    # 买卖策略里还藏着选股条件（2026-09-18 起那里已经不写条件了）。
    pname = st.resolve_pool(cfg).get("pool_name")
    if pname:
        groups = [(f, f"{lab}（{pname}）" if f == "entry" else lab) for f, lab in groups]
    for field, label in groups:
        for e in cfg.get(field) or []:
            rec = {"group": label, "expr": e, "ok": None, "error": None}
            try:
                arr = np.asarray(expr.evaluate(e, ns, n))
                if arr.ndim == 0:
                    rec["ok"] = bool(arr)
                else:
                    v = arr[i]
                    if v.dtype == bool or isinstance(v, (bool, np.bool_)):
                        rec["ok"] = bool(v)
                    else:
                        rec["ok"] = bool(v == v and v != 0)
            except expr.ExprError as ex:
                rec["error"] = str(ex)
            out["conditions"].append(rec)

    # 关键字段快照
    names = set()
    for e in (cfg.get("entry") or []) + (cfg.get("entry_any") or []) + \
             (cfg.get("weekly_filter") or []) + (cfg.get("buy_trigger") or []) + \
             (cfg.get("sell_signal") or []):
        names |= expr.extract_identifiers(e)
    names |= {"close", "pre_close", "open", "boll_lower", "boll_mid", "boll_upper"}
    for nm in sorted(names):
        if nm in df.columns and nm not in ("n", "True", "False"):
            out["values"][nm] = _f(df[nm].iloc[i])
    return out


def _latest_basic(ts_code: str, end: str) -> tuple[dict, str]:
    """<= end 的最近一条 daily_basic（基本面卡片在周线视图下用）。

    周K面板上**没有** daily_basic 字段（那是日频数据，跟周线不同频，硬聚合出个
    「周市盈率」是编数）。所以周线视图单独取一次最近一条，日期如实告诉前端 ——
    卡片的日期显示的是**这条数据的日期**，不是最后一根周K的日期。
    """
    try:
        row = db.query_one(
            "SELECT pe,pb,ps,total_mv,circ_mv,turnover_rate,trade_date FROM daily_basic "
            "WHERE ts_code=%s AND trade_date<=%s ORDER BY trade_date DESC LIMIT 1",
            (ts_code, end))
    except Exception as e:  # noqa: BLE001  表没建/没同步时安静降级为「—」
        _log.warning("读取 daily_basic 失败，基本面卡片按缺数据处理: %s", e)
        return {}, ""
    if not row:
        return {}, ""
    as_of = str(row.get("trade_date"))[:10]
    return {k: v for k, v in row.items() if k != "trade_date"}, as_of


def resolve_view_freq(cfg: dict, freq: str | None) -> str:
    """本次要看哪个周期。不给就**跟随策略自己的 timeframe**。

    跟随是刻意的：周线策略拿到日K上求值，`close <= boll_lower * 1.03` 就变成
    「日线布林下轨」—— 图上形状完全正常，数值却跟选股/回测不是同一件事，
    属于最难被发现的那类错。默认跟口径走，要看别的周期得显式指定。
    """
    f = str(freq or "").strip().lower()
    if f not in VIEW_FREQS:
        f = cfg.get("timeframe") or "daily"
    return "weekly" if f == "weekly" else "daily"


def _cut_after_anchor(df, apos: int):
    """把**查询日之后**那几根的信号列清掉（K 线 / 均线 / 指标一律留着）。

    多加载的那几天只用来「看形态」：`entry`（选股命中）/ `buy_trigger` / `sell_signal`
    / `score` 在查询日之后一律置空。理由不是好看不好看 —— 是**未来函数**：
    那几天的命中是「查完自选之后才发生」的，画在图上会让人以为当天就选出来了。
    选股、回测、这里的诊断页，三处口径必须一致（宁可少画，不能画出现在才知道的事）。
    """
    post = np.zeros(len(df), dtype=bool)
    post[apos + 1:] = True
    if post.any():
        for k in ("entry", "buy_trigger", "sell_signal"):
            if k in df.columns:
                df.loc[post, k] = False
        if "score" in df.columns:
            df.loc[post, "score"] = np.nan
    return df


def _code_error(raw: str, cands: list[dict]) -> str:
    """查询框里那个「输进来的东西」认不出来时的说法 —— **唯一一处**。

    两句话分开：**匹配到多只**要给出候选 + 让用户写完整代码（他自己能选，我们别猜）；
    **一只都没匹配**才说格式要求。混成一句会被当成"输入格式不对"，其实格式没错、
    只是重名 —— 那两句提示会把用户引到错的方向。
    """
    if cands:
        head = "、".join(f"{c['name']} {c['ts_code']}" for c in cands[:6])
        more = f"，等 {len(cands)} 只" if len(cands) > 6 else ""
        return f"「{raw}」匹配到 {len(cands)} 只：{head}{more}。请输入完整代码"
    return (f"没找到「{raw}」：代码写 6 位数字（600519 或 600519.SH），"
            "名称要能唯一确定一只")


def screen_detail(code: str, cfg: dict, trade_date: str | None = None,
                  bars: int = 260, freq: str | None = None,
                  sel_only: bool = False, open_pct=None) -> dict:
    """单只股票的策略命中明细（用于个股诊断）。

    `code` 是**用户在查询框里写的东西**（完整代码 / 6 位数字 / 名称都行，见
    `market.resolve_stock`）；认不出来时返回 `{"error": ...}` —— 匹配到多只
    会把候选写进文案，**不替用户挑一只**。

    `sel_only`：「只看选股条件」这一档（选股策略页的试跑预览、今日选股页的看 K 线）。
    那些入口**只选了票、没有绑定任何买卖策略**，所以：
      · 不算 `perf`（策略收益）—— 硬套默认买卖参数跑一遍单票回测，出来的收益跟
        「这批票选得对不对」毫无关系，摆出来只会让人以为试跑带了买卖策略；
      · `signals` 里只留「选股命中」一档，买卖触发/卖出信号的两条说明（「未设买入触发
        = 次日无条件买入」之类）在这里是废话，还会占满标题栏。
    用户想按真策略看收益，走上有「策略」下拉的整页诊断。

    freq: 视图周期 "daily" / "weekly"，不给则跟随策略 timeframe。
    **周线视图**：K 线与指标一律按周K重算（走 `compute_weekly_panel`，与纯周线策略
    同一个函数、只画已收盘的完整周），策略条件也跟着搬到周K上求值 —— 具体口径与
    「这是不是原策略」写在返回的 `view` 里，前端照它提示用户。
    返回里另有 `chips`：股东户数 / 融资融券 / 股东增减持三块筹码数据，
    **一律按视角日期 `end` 截止**（见 core/chips.py）。
    以及 `concepts`：东财概念（F10 核心题材，只回有入选理由的真概念）。这是**静态名单**
    （季度级变化），与查询日无关 —— 但「板块涨跌幅」是同步时点的快照，不是历史。

    **查询日（anchor）与 K 线窗口**：`trade_date` 是「选股/查看的那一天」，它决定了
    一切「当天的事实」—— 选股命中、买卖信号、基本面、筹码全算到这一天为止；
    但 K 线会**多加载到最新交易日**，好让图上能看见「选出来之后怎么走」。
    多出来的那几根只画行情与指标，信号被 `_cut_after_anchor` 清掉（未来函数）。
    查询日在 `rows` 里的下标随 `anchor.index` 返回，前端据此画竖虚线、定默认窗口。

    `open_pct`：开盘形态概率的**高开/低开阈值（%）**。不给就用 `openstat.OPEN_PCT`（3%）。
    页面上的「幅度」输入框与它同一个来源，改了就带着这个参数重取一次 —— 首屏的
    四格概率与图上标记都按它算；之后在页面上继续微调时**走前端即时重算**
    （拿回传的 `openstat.adjust.bars` 原料，见 core/openstat.recalc_from_bars），
    不再打接口。阈值只影响开盘形态那一块，不碰选股/买卖信号。
    """
    cfg = st.selection_cfg(st.merge_config(cfg))
    # 查询框里可能是完整代码、6 位数字、**或者名称**（页面上那行提示就是这么写的）。
    # 「输入 → 代码」只有一处实现（`market.resolve_stock`，含"多义不猜"），
    # 这里不再自己写一套 symbol 查询 —— 两处各写一份，改了一处就分叉。
    raw = str(code or "").strip()
    hit = market.resolve_stock(raw)
    code = hit["ts_code"]
    if not code:
        return {"error": _code_error(raw, hit["candidates"])}
    # 阈值唯一解析点（非法/越界都在这里收口，见 openstat.clamp_threshold）
    open_thr = ows.clamp_threshold(open_pct)
    end = trade_date or market.latest_trade_date()
    if not end:
        return {"error": "无行情数据"}
    # ---- 查询日（锚点）vs 加载终点 ------------------------------------------
    # `anchor` = 查询日：**选股条件、买卖信号、基本面、筹码一律算到这一天为止**，
    #   与传入的 trade_date 严格同日（用户按 9/15 选股，所有「当天的事实」就是 9/15）。
    # `load_end` = K 线多加载到今天：图上要能看见「查询日之后发生了什么」，否则每次
    #   复盘都得去翻别的页面。多出来的那几天**只画 K 线与指标**，信号一律不画
    #   （见下面 `_post_anchor` 的裁剪）—— 否则图上会冒出「查完才发生的命中」，
    #   那是未来函数，选股/回测里绝不允许，诊断页上也一样。
    anchor = end
    load_end = max(anchor, market.latest_trade_date() or anchor)
    view_freq = resolve_view_freq(cfg, freq)
    start = _lookback_start(end, bars=bars, freq=view_freq)

    weekly_view = view_freq == "weekly"
    # 周线视图下 w_ 前缀是多余的（那一根本身就是完整周），翻译一次；
    # 日线视图用原配置（weekly_filter 仍按 market._merge_weekly 的 asof 取上一根周K）。
    vcfg = st.to_weekly_view(cfg) if weekly_view else cfg
    if weekly_view:
        panel = market.compute_weekly_panel([code], start, load_end,
                                            indicators_cfg=cfg.get("indicators"),
                                            only=None)
    else:
        # 日线视图：**不管策略自称什么周期**，只要条件里引用了 w_ 字段，就得把
        # 「最近一根已收盘周K」的列并进来（`market._merge_weekly` 的 asof）。
        # ⚠ `required_weekly_fields()` 对 `timeframe != daily` 的策略直接返回 None
        #   （那是「直接画周K面板」那条路用的判断），照它取会漏列 →
        #   周线策略 + 显式 `freq=daily` 求值 `w_close` 时抛「未知字段」直接 500。
        #   所以这里先把它当成日线问一次。
        wf = st.required_weekly_fields({**cfg, "timeframe": "daily"})
        panel = market.load_panel([code], start, load_end,
                                  weekly=bool(wf), weekly_fields=wf,
                                  indicators_cfg=cfg.get("indicators"),
                                  only=None)
    if code not in panel:
        extra = "（周K 不足 5 根，或本地没有该股票）" if weekly_view else ""
        return {"error": f"本地无 {code} 的行情数据{extra}"}
    df = panel[code]
    sig = st.compute_signals(df, vcfg)
    df = df.copy()
    df["entry"] = sig["entry"]
    df["buy_trigger"] = sig["buy_trigger"]
    df["sell_signal"] = sig["sell_signal"]
    df["score"] = sig["score"]
    # 锚点在 df 里的位置 = 最后一根**不晚于查询日**的 K 线（查询日不是交易日时退到前一根）
    apos = int(pd.to_datetime(df["trade_date"]).searchsorted(
        pd.Timestamp(anchor), side="right")) - 1
    apos = max(0, min(apos, len(df) - 1))
    df = _cut_after_anchor(df, apos)
    # 下挂指标目录：选项名 / 画哪些字段全从 core.indicators 出（含实际参数），
    # 前端不再抄字段表 —— 否则策略把 cci 调成 20 之后，前端会去画不存在的 cci14。
    catalog = ind.chart_catalog(cfg.get("indicators"))
    cols = ["trade_date", "open", "high", "low", "close", "pre_close", "pct_chg",
            "vol", "amount", "boll_upper", "boll_mid", "boll_lower",
            "ma5", "ma10", "ma20", "ma60", "macd_dif", "macd_dea", "macd_hist",
            "kdj_k", "kdj_d", "kdj_j", "rsi6", "rsi12", "entry", "buy_trigger",
            "sell_signal", "score"]
    for f in ind.chart_catalog_fields(catalog):
        if f not in cols:
            cols.append(f)
    cols = [c for c in cols if c in df.columns]
    # 显示窗口 = **查询日之前 bars 根 + 查询日之后全部**。以前是 `tail(bars)`，
    # 那样「查询日之后」那几根会被切掉，图上永远看不到「选出来之后怎么走」。
    keep_from = max(0, apos - bars + 1)
    rows = df[cols].iloc[keep_from:].copy()
    rows["trade_date"] = pd.to_datetime(rows["trade_date"]).dt.strftime("%Y-%m-%d")
    anchor_index = apos - keep_from          # 查询日在 rows 里的下标（前端画竖虚线用）
    bars_after = len(rows) - anchor_index - 1
    # `sel_only`：这一档没有绑定买卖策略。⚠ 空 `buy_trigger` 在 strategy 里的语义是
    # 「次日无条件买入」（恒 True，见 `signal_marks` 的说明），所以「不给买卖条件」不能
    # 表达成「不交易」—— 逐根明细里直接按 False 落列，明细表 / 导出都不会冒出卖点。
    if sel_only:
        rows["buy_trigger"] = False
        rows["sell_signal"] = False

    # 哪些组在本地是空数据（例如没同步资金流向）——如实告诉前端，别让它静默画一条空线。
    # 判据只有一份（core.indicators.mark_catalog_empty），交易复盘图用的是同一个函数。
    ind.mark_catalog_empty(catalog, rows)

    kept = set(rows.columns)
    first_bar = str(rows["trade_date"].iloc[0]) if len(rows) else ""
    last_bar = str(rows["trade_date"].iloc[-1]) if len(rows) else ""
    anchor_date = str(rows["trade_date"].iloc[anchor_index]) if len(rows) else ""
    # 基本面卡片：日线视图用**查询日那一根**的 daily_basic 字段（不是 K 线最后一根 ——
    # 查询日之后多加载的那几根不算「选股当天的事实」）；
    # 周线视图单独取最近一条 daily_basic（周K面板上没有日频字段）。
    # 单位换算只在 indicators.fundamental_snapshot 里做一次；缺数据给「—」，不拿更早的值顶。
    if weekly_view:
        last_row, as_of = _latest_basic(code, end)
    else:
        last_row = df.iloc[apos].to_dict() if len(df) else {}
        as_of = anchor_date

    # 视图口径：这个周期是不是策略自己的口径、条件有没有被搬过 —— 必须如实说，
    # 否则「周K · 按周重算」的收益会被当成策略在周线上的真实表现。
    recomputed = view_freq != cfg["timeframe"]
    bar_label = "周K" if weekly_view else "日K"
    notes: list[str] = []
    if recomputed:
        if weekly_view:
            notes.append("该策略是日线口径，这里把条件搬到周K上重算"
                         "（MA5 = 5 周均线、boll_lower = 周线布林下轨）："
                         "信号与收益跟日线选股/回测不是同一件事，只用于看周线形态")
        else:
            notes.append("该策略是周线口径，这里把条件搬到日K上重算"
                         "（MA5 = 5 日均线）：与周线选股/回测结果不同")
    if weekly_view and anchor_date and anchor_date < anchor:
        notes.append(f"周K 只画已收盘的完整周，查询日那一根是 {anchor_date}"
                     f"（{anchor} 那一周还没收盘）")
    if bars_after > 0:
        notes.append(f"查询日 {anchor_date} 右侧还有 {bars_after} 根 K 线（到 {last_bar}）："
                     "只画行情与均线/指标，不算选股命中与买卖信号 —— 用来看"
                     "「当天选出来之后怎么走」，那不是当天就知道的事")
    view = {
        "freq": view_freq,
        "bar_label": bar_label,
        "last_label": "最新周K收盘" if weekly_view else "最新收盘",
        "tag": bar_label + (" · 条件重算" if recomputed else ""),
        "recomputed": recomputed,
        "native": not recomputed,
        "bars": len(rows),
        "last_bar": last_bar,
        # 「查询日」这四个字 + 日期由后端拼（前端别自己写文案），画竖虚线时直接用
        "anchor_label": ("查询日 " + anchor_date) if anchor_date else "",
        "notes": notes,
    }
    # 策略信号在 K 线上的画法（画哪几档、几根、为什么不画）——
    # 前端只按 key 上色，位置/形状/「为什么不画」都从这来。
    # 注意：行里的 entry/buy_trigger/sell_signal 是**裁剪后**的窗口，
    # 计数必须用同一段 rows 算，不然卡片上的数跟图上点数对不上。
    # `sel_only`：这里没有买卖策略，只画「选股命中」那一档。
    marks = st.signal_marks(
        {k: rows[k].to_numpy() for k in ("entry", "buy_trigger", "sell_signal")
         if k in rows.columns}, vcfg)
    if sel_only:
        marks = [m for m in marks if m.get("key") == "entry"]
    return {
        "ts_code": code,
        "name": df["_name"].iloc[0] if "_name" in df.columns else "",
        "industry": df["industry"].iloc[0] if "industry" in df.columns else "",
        "catalog": _jsonable(catalog),
        "view": view,
        # 查询日（锚点）在 rows 里的位置 —— 前端据此把默认窗口落在查询日附近并画竖虚线。
        # 位置由后端算，前端不拿日期去 rows 里 indexOf（那天若不是交易日就没得对）。
        "anchor": {
            "date": anchor_date,
            "index": anchor_index,
            "requested": anchor,
            "extended": bool(bars_after > 0),
        },
        # 本次运行**实际生效的选股策略**（页面传的 `pool_id`、试跑内联的 `config.entry`；
        # ⚠ **没带就是空** —— 2026-10-11 起 `app.py` 不再兜底挂预设池，见那里的注释）。
        # 回传出来有两个用处：页面能说清"这些命中点是按哪条
        # 选股策略算的"；独立对拍（回归脚本）能挂在同一份条件上，不会一边有池一边没有。
        "pool": _jsonable(uni.describe(st.resolve_pool(vcfg))),
        "fundamental": _jsonable(ind.fundamental_snapshot(last_row, as_of)),
        "signals": _jsonable(marks),
        # 策略收益（本区间）：**走组合回测同一个引擎**跑一遍单票，满仓口径。
        # 区间 = 窗口首根 ~ **查询日**（不是 K 线最后一根）：查询日右边那几根不算，
        # 否则「本区间收益」会变成「拿到今天」的收益，跟图上标出的信号点不是一件事。
        # `sel_only`（没绑定买卖策略的入口）整块不跑：省一次单票回测，返回 None，
        # 前端据此不渲染第 4 张卡片。
        "perf": (None if sel_only else _jsonable(bt.single_stock_perf(
            code, df, vcfg, sig=sig,
            start=first_bar or None, end=anchor_date or None,
            freq_label=bar_label))),
        "missing_fields": sorted({s["f"] for g in catalog for s in g["series"]} - kept),
        # 筹码与两融（股东户数 / 融资融券 / 股东增减持）：**按视角日期 `end` 截止**
        # —— 股东户数是报告期数据、增减持是公告事件，"用当年还没公告的数"就是未来函数。
        # 同样只在这里拼文本与涨跌色，前端只铺格子（见 core/chips.py）。
        # `bar_dates` = 图上 K 线的交易日：增减持要**对齐到 K 线的行号**才能画在顶边
        # （公告日落在哪一根由后端算，前端不拿日期去 indexOf —— 那天不是交易日就没得对）。
        "chips": _jsonable(ch.chips_snapshot(code, end or last_bar,
                                             bar_dates=list(rows["trade_date"]))),
        # 东财概念（F10 核心题材）：只回**真概念**（有入选理由那批），
        # 非概念标记（行业/地域/指数成分/风格）只报个数 —— 见 core/concept.py。
        # 与 `chips` 一样，文本与涨跌色在这里就算好，前端只铺格子。
        # ⚠ 这是**静态名单**（季度级变化），没有 asof 概念，不受查询日影响。
        "concepts": _jsonable(cc.concept_snapshot(code)),
        # 开盘形态概率（高开3%/低开3%/低开红柱/高开绿柱的历史频率）：
        # 区间 = **图表窗口那一段**（`rows` 首根 ~ 查询日那根）——
        # 用户口径是「跟随页面上 K 线根数」，所以直接用同一段 rows 求值，
        # 顺带天然满足「不含查询日之后的未来数据」（`_cut_after_anchor` 已把
        # 查询日之后的信号列清掉，但行情列还在；这里再按 anchor 下标切一刀）。
        "openstat": _jsonable(ows.snapshot(
            ows.stats_from_frame(rows.iloc[:anchor_index + 1], open_thr))),
        "rows": _jsonable(rows.to_dict("records")),
    }


def _jsonable(obj):
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        v = float(obj)
        return None if v != v else round(v, 4)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, float) and obj != obj:
        return None
    if isinstance(obj, (pd.Timestamp, dt.date)):
        return str(obj)[:10]
    return obj


def save_screen_result(strategy_id: int, res: dict) -> int:
    if not res.get("items"):
        return 0
    td = res["trade_date"]
    rows = []
    for it in res["items"]:
        rows.append((strategy_id, td, it["ts_code"], it["name"], it["close"],
                     it["pct_chg"], it["score"],
                     json_dumps(it.get("detail") or {})))
    sql = """
        INSERT INTO screen_result
          (strategy_id,trade_date,ts_code,name,close,pct_chg,score,detail)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE close=VALUES(close),pct_chg=VALUES(pct_chg),
          score=VALUES(score),detail=VALUES(detail),name=VALUES(name)
    """
    return db.insert_many(sql, rows)


def json_dumps(o) -> str:
    import json
    return json.dumps(_jsonable(o), ensure_ascii=False)


def list_screen_results(strategy_id: int | None = None, trade_date: str | None = None,
                        limit: int = 500) -> list[dict]:
    sql = ("SELECT r.*, s.name AS strategy_name FROM screen_result r "
           "LEFT JOIN strategy s ON s.id=r.strategy_id WHERE 1=1")
    args: list = []
    if strategy_id:
        sql += " AND r.strategy_id=%s"
        args.append(strategy_id)
    if trade_date:
        sql += " AND r.trade_date=%s"
        args.append(trade_date)
    sql += " ORDER BY r.trade_date DESC, r.score DESC LIMIT %s"
    args.append(int(limit))
    rows = db.query(sql, args)
    for r in rows:
        r["trade_date"] = r["trade_date"].strftime("%Y-%m-%d")
        if isinstance(r.get("detail"), (str, bytes)):
            import json
            try:
                r["detail"] = json.loads(r["detail"])
            except Exception:  # noqa: BLE001
                r["detail"] = {}
    return rows
