# -*- coding: utf-8 -*-
"""组合回测引擎。

执行时序（每根 K 线 t）：
  1. 卖出：持仓股在 t 日检查 止损/止盈(盘中触发价) → 卖出信号(开盘价) → 到期(收盘价)
  2. 买入：t-1 日选出的候选股，若 t 日满足买入触发条件，按开盘价买入
  3. 收盘市值：用 t 日收盘价对持仓估值，记录资金曲线
  4. 选股：记录 t 日 entry 成立的股票，作为 t+1 日的买入候选
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from config import BACKTEST_DEFAULTS
from core import strategy as st

BUY_SHARES_LOT = 100  # A股一手
BENCH_NAME = "沪深300"

# 交易明细复盘图的**基础**列（K 线 + 均线 / 布林）。
# 下挂指标格要画哪些字段**不在这里写死** —— 由 `core.indicators.chart_catalog` 出，
# 与个股诊断页共用同一份目录（含指标的真实参数）：哪天策略把 cci 调成 20，
# 两边的下拉与字段一起跟着变，不会一边画 cci14、另一边画 cci20。
KLINE_BASE_COLS = [
    "trade_date", "open", "high", "low", "close", "pre_close", "pct_chg",
    "vol", "amount", "turnover_rate", "volume_ratio",
    "ma5", "ma10", "ma20", "ma60",
    "boll_upper", "boll_mid", "boll_lower",
]


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else round(f, 4)   # NaN -> None


def _trade_out(tr: dict) -> dict:
    """交易记录出参。金额字段转 float —— pymysql 返回 Decimal，
    Flask 会把它序列化成字符串，前端做算术/比较时容易踩坑。"""
    money_keys = ("buy_price", "sell_price", "cost", "revenue", "fee", "slip_cost",
                  "pnl", "pnl_pct")
    out = {}
    for k in ("id", "ts_code", "name", "buy_date", "sell_date", "shares",
              "hold_days", "sell_reason", "buy_reason"):
        out[k] = tr.get(k)
    for k in money_keys:
        out[k] = _num(tr.get(k))
    return out


def trade_kline(run_id: int, trade_id: int, before: int = 40, after: int = 25) -> dict:
    """取一笔回测交易对应个股的 K 线，并标出买入 / 卖出的实际成交点。

    **关键**：用回测当时的（个股, 起止区间）以相同复权基准加载，
    因此图上的价格与交易明细里的买入价/卖出价严格同口径（前复权），
    不会出现"明细 12.30、图上 13.05"这种对不上的情况。
    标记点用的是**实际成交价**（含滑点），与当日开盘价的差即是滑点。
    """
    from core import db, indicators as ind, market

    run = db.query_one("SELECT * FROM backtest_run WHERE id=%s", (run_id,))
    if not run:
        return {"error": "回测记录不存在"}
    tr = db.query_one("SELECT * FROM backtest_trade WHERE id=%s AND run_id=%s",
                      (trade_id, run_id))
    if not tr:
        return {"error": "该回测下找不到这笔交易"}

    code = tr["ts_code"]
    start = str(run["start_date"])[:10]
    end = str(run["end_date"])[:10]
    buy_date = str(tr["buy_date"])[:10]

    sid = run.get("strategy_id")
    cfg = {}
    if sid:
        cfg = (st.get_strategy(int(sid)) or {}).get("config") or {}
    cfg = st.selection_cfg(cfg)

    # 往前多加载一段只为"指标预热"（MA60/BOLL 等）。
    # 前复权基准 = adj_factor.iloc[-1]，只取决于区间**末端**，所以末端保持不变时
    # 向前扩展不会改变任何一根 K 线的价格 —— 与交易明细的成交价仍然严格同口径。
    warm_from = (pd.Timestamp(buy_date) - pd.Timedelta(days=380)).strftime("%Y-%m-%d")
    load_start = min(start, warm_from)

    panel = market.load_panel([code], load_start, end, adjust="qfq",
                              indicators_cfg=cfg.get("indicators"),
                              weekly=False, only=None)
    df = panel.get(code)
    if df is None or len(df) == 0:
        return {"error": f"本地无 {code} 的行情数据，请先同步该区间"}

    df = df.copy()
    try:
        sg = st.compute_signals(df, cfg)
        df["sig_entry"] = sg["entry"]
        df["sig_buy"] = sg["buy_trigger"]
        df["sig_sell"] = sg["sell_signal"]
    except Exception:  # noqa: BLE001
        for c in ("sig_entry", "sig_buy", "sig_sell"):
            df[c] = False

    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
    dates = df["trade_date"].tolist()
    pos = {d: i for i, d in enumerate(dates)}

    sell_date = str(tr["sell_date"])[:10] if tr.get("sell_date") else ""
    bi, si = pos.get(buy_date), pos.get(sell_date)

    head = bi if bi is not None else 0
    tail = si if si is not None else head
    lo = max(0, head - max(int(before), 5))
    hi = min(len(df) - 1, tail + max(int(after), 5))
    win = df.iloc[lo:hi + 1].reset_index(drop=True)

    # 下挂指标目录：选项名 / 画哪些字段全从 core.indicators 出（含指标的真实参数），
    # 与个股诊断页同一个函数。前端不抄字段表 —— 抄一份就会在该指标被改参数后静默画空线。
    catalog = ind.mark_catalog_empty(
        ind.chart_catalog((cfg or {}).get("indicators")), win)

    keep = [c for c in KLINE_BASE_COLS if c in win.columns]
    for c in ind.chart_catalog_fields(catalog):
        if c not in keep and c in win.columns:
            keep.append(c)
    keep += [c for c in ("sig_entry", "sig_buy", "sig_sell") if c in win.columns]
    bars = win[keep].to_dict("records")

    def _mark(i, price, reason):
        if i is None:
            return None
        return {
            "date": dates[i], "i": i - lo, "price": _num(price),
            "open": _num(df["open"].iloc[i]), "low": _num(df["low"].iloc[i]),
            "high": _num(df["high"].iloc[i]), "reason": reason or "",
        }

    # 窗口内其它信号点，便于看清策略"为什么在这里触发"
    signals = []
    for k in range(len(win)):
        tags = [t for c, t in (("sig_entry", "entry"), ("sig_buy", "buy"),
                               ("sig_sell", "sell"))
                if c in win.columns and bool(win[c].iloc[k])]
        if tags:
            signals.append({"i": k, "date": dates[lo + k], "tags": tags})

    return {
        "ts_code": code,
        "name": tr.get("name") or (df["_name"].iloc[0] if "_name" in df.columns else ""),
        "industry": df["industry"].iloc[0] if "industry" in df.columns else "",
        "strategy_id": sid,
        "strategy_name": run.get("name") or "",
        "adjust": "qfq",
        "slippage": _num((json_params(run) or {}).get("slippage")),
        "range": {
            "run_start": start, "run_end": end, "load_from": load_start,
            "shown_from": dates[lo], "shown_to": dates[hi],
            "has_more_before": lo > 0, "has_more_after": hi < len(df) - 1,
        },
        "trade": _trade_out(tr),
        "marks": {
            "buy": _mark(bi, tr.get("buy_price"), tr.get("buy_reason")),
            "sell": _mark(si, tr.get("sell_price"), tr.get("sell_reason")),
        },
        "signals": signals,
        "indicators": catalog,
        "bars": bars,
    }


def json_params(run: dict) -> dict:
    """backtest_run.params 可能是 str / dict / None。"""
    p = (run or {}).get("params")
    if isinstance(p, dict):
        return p
    if isinstance(p, str) and p.strip():
        try:
            import json as _json
            v = _json.loads(p)
            return v if isinstance(v, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
    return {}


@dataclass
class Position:
    code: str
    name: str
    shares: int
    buy_date: str
    buy_price: float
    cost: float
    buy_i: int
    hold_bars: int = 0
    buy_reason: str = ""
    buy_fee: float = 0.0
    buy_slip: float = 0.0     # 买入端滑点成本（元）= (买入成交价 - 当时基准价) × 股数


@dataclass
class Trade:
    ts_code: str
    name: str
    buy_date: str
    buy_price: float
    sell_date: str
    sell_price: float
    shares: int
    cost: float
    revenue: float
    pnl: float
    pnl_pct: float
    hold_days: int
    sell_reason: str
    buy_reason: str
    fee: float = 0.0
    # 滑点成本（元）：买入上浮 + 卖出下调 合计。**与 fee 分开记** ——
    # fee 是佣金+印花税（交给券商的），滑点是「成交价吃亏」的隐性成本，
    # 以前它只体现在买卖价里、从不出现在任何统计里，等于白亏了看不见。
    slip_cost: float = 0.0


def _fee_buy(amount: float, commission: float, min_commission: float) -> float:
    return max(amount * commission, min_commission if amount > 0 else 0.0)


def _fee_sell(amount: float, commission: float, stamp_tax: float,
              min_commission: float) -> float:
    return max(amount * commission, min_commission if amount > 0 else 0.0) + amount * stamp_tax


def pack_one(code: str, df: pd.DataFrame, cfg: dict,
             sig: dict | None = None) -> dict | None:
    """把**单只**股票的 df 打包成回测用的紧凑结构（只留 numpy 数组）。

    组合回测（compact_panel）与单票诊断（single_stock_perf）共用这一份 ——
    否则「entry/buy_trigger 怎么落进数组」会有两处实现，早晚跑偏。

    `sig` 已经算过就传进来：个股诊断页的信号已经算过一次，复用同一份对象，
    顺手保证「图上画的信号」与「收益统计用的信号」严格是同一份。
    """
    if df is None or len(df) < 2:
        return None
    if sig is None:
        sig = st.compute_signals(df, cfg)
    try:
        in_pool = st.compute_pool(df, cfg)
    except Exception:
        in_pool = np.ones(len(df), dtype=bool)
    d = {
        "name": df["_name"].iloc[-1] if "_name" in df.columns else "",
        "dates": pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d").to_numpy(),
        "open": pd.to_numeric(df["open"], errors="coerce").to_numpy(dtype=float),
        "high": pd.to_numeric(df["high"], errors="coerce").to_numpy(dtype=float),
        "low": pd.to_numeric(df["low"], errors="coerce").to_numpy(dtype=float),
        "close": pd.to_numeric(df["close"], errors="coerce").to_numpy(dtype=float),
        "entry": sig["entry"].to_numpy(dtype=bool),
        "in_pool": np.asarray(in_pool, dtype=bool),
        "buy_trigger": sig["buy_trigger"].to_numpy(dtype=bool),
        "sell_signal": sig["sell_signal"].to_numpy(dtype=bool),
        "score": pd.to_numeric(sig["score"], errors="coerce").to_numpy(dtype=float),
    }
    d["idx"] = {dt: i for i, dt in enumerate(d["dates"])}
    return d


def compact_panel(panel: dict[str, pd.DataFrame], cfg: dict,
                  progress: Callable[[int, int, str], None] | None = None
                  ) -> dict[str, dict]:
    """把带指标的 panel 转成回测用的紧凑结构（只保留 numpy 数组），并计算信号。"""
    out: dict[str, dict] = {}
    items = list(panel.items())
    total = len(items)
    for k, (code, df) in enumerate(items):
        try:
            d = pack_one(code, df, cfg)
        except Exception:
            continue
        if d is None:
            continue
        out[code] = d
        if progress and (k % 200 == 0 or k == total - 1):
            progress(k + 1, total, code)
    return out


def prepare_data(codes: list[str], start: str, end: str, cfg: dict, batch: int = 250,
                 progress: Callable[[str, float], None] | None = None,
                 cache_info: dict | None = None) -> dict[str, dict]:
    """分批加载行情 -> 计算指标与信号 -> 压缩为回测结构。

    分批处理可以避免全市场（5000+ 只）一次性载入导致内存爆掉。

    产物会按 `core.btcache` 的键落盘复用。键只含「股票池 + 区间 + 指标口径 + 信号条件」，
    而 `risk`（止损/止盈/持有期）与 `exec.slippage` **只在 `run_backtest` 里生效、不进键**
    —— 于是「同池同信号、只改参数」时这一整段（全市场 100+ 秒）可以整块跳过。

    传 `cache_info`（一个 dict）可拿到 `{"cache": "hit"/"miss"/"off", "key", "n"}`，
    方便上层在进度与记录里如实标注「这次是复用的」。
    """
    from core import btcache, log, market
    # 选股条件由「选股策略」提供（买卖策略不再写 entry）—— 这里先抬成 entry，
    # 下游 compute_signals / 门控逻辑一行不用改。
    cfg = st.selection_cfg(cfg)

    key = None
    if btcache.ENABLED:
        try:
            key = btcache.digest(codes, start, end, cfg)
        except Exception as e:  # noqa: BLE001  键都算不出来就老实现算
            log.get("backtest").warning("回测缓存键计算失败，本次不走缓存: %s", e)
            key = None
        if key:
            hit = btcache.load(key)
            if hit is not None:
                if cache_info is not None:
                    cache_info.update(cache="hit", key=key, n=len(hit))
                if progress:
                    progress("命中数据缓存，跳过加载与指标计算", 0.60)
                return hit

    only = st.required_daily_fields(cfg)
    wf = st.required_weekly_fields(cfg)
    pool = st.pool_filters(cfg)
    n = len(codes)
    out: dict[str, dict] = {}
    for i in range(0, n, batch):
        part = codes[i:i + batch]
        if cfg["timeframe"] == "weekly":
            panel = market.compute_weekly_panel(part, start, end,
                                                indicators_cfg=cfg.get("indicators"),
                                                only=only)
        else:
            panel = market.load_panel(part, start, end, adjust="qfq",
                                      indicators_cfg=cfg.get("indicators"),
                                      weekly=bool(wf), weekly_fields=wf, only=only)
        panel = market.apply_pool_filters(panel, pool)
        out.update(compact_panel(panel, cfg))
        del panel
        if progress:
            progress(f"加载行情并计算信号 {min(i + batch, n)}/{n}",
                     0.05 + 0.55 * min(1, (i + batch) / max(n, 1)))
    if key:
        btcache.save(key, out)
    if cache_info is not None:
        cache_info.update(cache="miss" if key else "off", key=key or "", n=len(out))
    return out


def resolve_params(cfg: dict, params: dict | None = None) -> dict:
    """运行时参数的**唯一合成点**：默认值 ← 调用方参数 ← 策略自带。

    优先级（后写的赢）：
      1. `BACKTEST_DEFAULTS`
      2. 调用方 params（回测页表单 / 单票诊断的 `SINGLE_POSITION`）
      3. 策略自带：`risk`（止损/止盈/持有期限）与 `exec.slippage`（滑点）——
         **只覆盖「设了值」的键**（None / "" 视为没设）。

    为什么单拎出来：滑点是**策略自带的成交假设**（见 `strategy.DEFAULT_EXEC`），
    但回测页也会传一个 slippage。两处都能定，就必须只有一条合成路径 ——
    否则「落库的 params 里写的滑点」和「引擎实际用的滑点」会不一致，
    回测记录看起来就是骗人的。落库与执行都用本函数。
    """
    p = {**BACKTEST_DEFAULTS, **(params or {})}
    p.update({k: v for k, v in ((cfg.get("risk") or {})).items() if v not in (None, "")})
    slip = (cfg.get("exec") or {}).get("slippage")
    if slip not in (None, ""):
        p["slippage"] = float(slip)
    return p


def run_backtest(data: dict[str, dict], cfg: dict, start: str, end: str,
                 params: dict | None = None,
                 benchmark: dict[str, float] | None = None,
                 progress: Callable[[str, float], None] | None = None) -> dict:
    """执行回测，返回 {"metrics","trades","equity","logs"}。

    data: 由 prepare_data / compact_panel 产出的紧凑结构。
    benchmark: {日期: 指数收盘价}，给定时用作基准（净值化为 1.0）。
    """
    cfg = st.selection_cfg(cfg)
    p = resolve_params(cfg, params)

    init_cash = float(p.get("init_cash") or 1_000_000)
    max_positions = int(p.get("max_positions") or 10)
    position_pct = float(p.get("position_pct") or 0)
    commission = float(p.get("commission") or 0)
    stamp_tax = float(p.get("stamp_tax") or 0)
    slippage = float(p.get("slippage") or 0)
    min_commission = float(p.get("min_commission") or 0)
    max_candidates = int(p.get("max_candidates") or 100)

    stop_loss = cfg["risk"].get("stop_loss_pct")
    take_profit = cfg["risk"].get("take_profit_pct")
    max_hold = cfg["risk"].get("max_hold_days")
    buy_price_col = cfg["exec"].get("buy_price", "open")
    sell_price_col = cfg["exec"].get("sell_price", "open")
    max_hold_price = cfg["exec"].get("max_hold_price", "close")

    logs: list[str] = []
    logs.append(f"标的 {len(data)} 只，区间 {start} ~ {end}")

    if not data:
        return {"metrics": {"error": "股票池内无可用数据"}, "trades": [], "equity": [],
                "logs": logs}
    logs.append(f"有效股票 {len(data)} 只")

    # ---- 时间轴 ----
    all_dates = sorted({d for v in data.values() for d in v["dates"]
                        if start <= d <= end})
    if len(all_dates) < 3:
        return {"metrics": {"error": "区间内 K 线不足"}, "trades": [], "equity": [],
                "logs": logs}
    logs.append(f"K线 {len(all_dates)} 根（{all_dates[0]} ~ {all_dates[-1]}）")

    cash = init_cash
    positions: dict[str, Position] = {}
    trades: list[Trade] = []
    equity_rows: list[dict] = []
    pending: list[str] = []

    # 基准：沪深300（若提供）否则股票池等权买入持有
    bench_norm: dict[str, float] = {}
    if benchmark:
        first = None
        for d in all_dates:
            if d in benchmark and benchmark[d]:
                first = benchmark[d]
                break
        if first:
            bench_norm = {d: benchmark[d] / first for d in all_dates
                          if d in benchmark and benchmark[d]}
            logs.append(f"基准：{p.get('benchmark_name', BENCH_NAME)}"
                        f"（{len(bench_norm)} 个交易日）")

    first_close = {}
    for code, v in data.items():
        m = (v["dates"] >= all_dates[0]) & (v["dates"] <= all_dates[-1])
        cl = v["close"][m]
        cl = cl[~np.isnan(cl)]
        if len(cl):
            first_close[code] = cl[0]
    bench_base = dict(first_close)

    n_dates = len(all_dates)
    for ti, today in enumerate(all_dates):
        if progress and ti % 20 == 0:
            progress(f"回测 {ti}/{n_dates}  {today}", 0.5 + 0.45 * ti / n_dates)

        # ---------- 1. 卖出 ----------
        for code in list(positions):
            v = data[code]
            i = v["idx"].get(today)
            if i is None:
                continue  # 停牌，无法交易
            pos = positions[code]
            pos.hold_bars += 1
            op, hi, lo, cl = v["open"][i], v["high"][i], v["low"][i], v["close"][i]
            if np.isnan(cl):
                continue
            exit_px, reason = None, None

            # ---------- 按**价格时间线**判定离场：open → 盘中(high/low) → close ----------
            # ⚠ 同一根 K 线上，「按 open 成交」的离场与「盘中止损/止盈」**不可能都发生**：
            # 开盘卖了，盘中那一下就不属于你。旧写法把盘中风控排在最前、再用 open 兜底，
            # 等于白送一个期权 ——「摸到止盈就吃 5%，没摸到就当自己开盘那一刻卖了」，
            # 而「今天没摸到」要到收盘才知道。`#58` 的 +191.65% 全靠它（去掉任一半 → −99.96%）。
            # 详见 CONVENTIONS C37。
            signal_now = i > 0 and bool(v["sell_signal"][i - 1])
            expire_now = bool(max_hold and pos.hold_bars >= int(max_hold))
            at_open = sell_price_col == "open"      # 信号离场的成交价列
            open_exit = at_open or max_hold_price != "close"   # 是否存在「开盘即走」的离场

            # --- 第 1 段：开盘。优先级沿用旧口径：信号 > 到期 ---
            if open_exit and not np.isnan(op):
                if signal_now and at_open:
                    exit_px, reason = op, "signal"
                elif expire_now and max_hold_price != "close":
                    exit_px, reason = op, "max_hold"

            # --- 第 2 段：盘中（止损 / 止盈，跳空则以开盘价成交）---
            if exit_px is None and stop_loss is not None:
                trig = pos.buy_price * (1 + float(stop_loss) / 100.0)
                if not np.isnan(lo) and lo <= trig:
                    exit_px = min(trig, op) if not np.isnan(op) else trig
                    reason = "stop_loss"
            if exit_px is None and take_profit is not None:
                trig = pos.buy_price * (1 + float(take_profit) / 100.0)
                if not np.isnan(hi) and hi >= trig:
                    exit_px = max(trig, op) if not np.isnan(op) else trig
                    reason = "take_profit"

            # --- 第 3 段：收盘。优先级同第 1 段 ---
            # ⚠ 信号必须位移一根：`sell_signal` 在**完整 K 线**上求值（`close` / `ma*` 都是
            # 当天值），按 `open` 成交时直接取 [i] 就是「拿当天收盘才知道的信息在开盘卖」
            # = 未来函数。口径与买入侧对称：T 日收盘确认信号 → T+1 开盘执行。
            # 实测反差：`close<open` +490.7% vs 合法写法 `ref(close,1)<ref(open,1)` −37.4%。
            # `sell_price=close` 的收盘卖不存在该问题（收盘信号当天可得）。
            if exit_px is None and not np.isnan(cl):
                if signal_now and not at_open:
                    exit_px, reason = cl, "signal"
                elif expire_now and max_hold_price == "close":
                    exit_px, reason = cl, "max_hold"

            if exit_px is not None and exit_px > 0:
                sell_px = exit_px * (1 - slippage)
                gross = sell_px * pos.shares
                fee = _fee_sell(gross, commission, stamp_tax, min_commission)
                revenue = gross - fee
                cash += revenue
                pnl = revenue - pos.cost
                # 滑点成本：卖出端按「基准价 - 成交价」，与买入端相加
                slip = pos.buy_slip + (exit_px - sell_px) * pos.shares
                trades.append(Trade(
                    ts_code=code, name=pos.name, buy_date=pos.buy_date,
                    buy_price=round(pos.buy_price, 4), sell_date=today,
                    sell_price=round(sell_px, 4), shares=pos.shares,
                    cost=round(pos.cost, 2), revenue=round(revenue, 2),
                    pnl=round(pnl, 2),
                    pnl_pct=round(pnl / pos.cost * 100, 4) if pos.cost else 0.0,
                    hold_days=pos.hold_bars, sell_reason=reason,
                    buy_reason=pos.buy_reason,
                    fee=round(fee + pos.buy_fee, 2),
                    slip_cost=round(slip, 2),
                ))
                del positions[code]

        # ---------- 2. 买入 ----------
        if pending and len(positions) < max_positions:
            # 有评分的按评分降序
            cands = []
            for code in pending:
                v = data.get(code)
                if v is None:
                    continue
                i = v["idx"].get(today)
                if i is None:
                    continue
                if not v["buy_trigger"][i]:
                    continue
                cands.append((code, v["score"][i] if not np.isnan(v["score"][i]) else 0.0, i))
            cands.sort(key=lambda x: -x[1])
            cands = cands[:max_candidates]
            for code, _sc, i in cands:
                if len(positions) >= max_positions:
                    break
                if code in positions:
                    continue
                v = data[code]
                px = v["open"][i] if buy_price_col == "open" else v["close"][i]
                if np.isnan(px) or px <= 0:
                    continue
                buy_px = px * (1 + slippage)
                budget = init_cash * position_pct / 100.0 if position_pct > 0 \
                    else init_cash / max_positions
                budget = min(budget, cash)
                shares = int(budget / buy_px / BUY_SHARES_LOT) * BUY_SHARES_LOT
                if shares <= 0:
                    continue
                gross = shares * buy_px
                fee = _fee_buy(gross, commission, min_commission)
                if gross + fee > cash:
                    shares -= BUY_SHARES_LOT
                    if shares <= 0:
                        continue
                    gross = shares * buy_px
                    fee = _fee_buy(gross, commission, min_commission)
                cost = gross + fee
                if cost > cash:
                    continue
                cash -= cost
                positions[code] = Position(
                    code=code, name=v["name"], shares=shares, buy_date=today,
                    buy_price=buy_px, cost=cost, buy_i=i, buy_reason="entry",
                    buy_fee=fee,
                    buy_slip=(buy_px - px) * shares,
                )

        # ---------- 3. 收盘估值 ----------
        pos_val = 0.0
        for code, pos in positions.items():
            v = data[code]
            i = v["idx"].get(today)
            px = v["close"][i] if i is not None and not np.isnan(v["close"][i]) \
                else pos.buy_price
            pos_val += px * pos.shares
        total = cash + pos_val

        # 基准净值
        if bench_norm:
            bench_nav = bench_norm.get(today, list(bench_norm.values())[-1])
        else:
            bench_vals = []
            for code, base in bench_base.items():
                v = data[code]
                i = v["idx"].get(today)
                if i is not None and not np.isnan(v["close"][i]) and base:
                    bench_vals.append(v["close"][i] / base)
            bench_nav = float(np.mean(bench_vals)) if bench_vals else 1.0

        equity_rows.append({
            "trade_date": today, "total_asset": round(total, 2),
            "cash": round(cash, 2), "position_value": round(pos_val, 2),
            "hold_num": len(positions), "benchmark": round(bench_nav, 4),
        })

        # ---------- 4. 下一日候选 ----------
        picks = []
        for code, v in data.items():
            if code in positions:
                continue
            i = v["idx"].get(today)
            if i is None:
                continue
            if not v["entry"][i]:
                continue
            # 条件池：当天不在池内的不作为次日候选（与 entry 同一时机求值）
            if "in_pool" in v and not v["in_pool"][i]:
                continue
            picks.append((code, v["score"][i] if not np.isnan(v["score"][i]) else 0.0))
        picks.sort(key=lambda x: -x[1])
        pending = [c for c, _ in picks[:max_candidates * 5]]

    # ---------- 期末清仓 ----------
    last = all_dates[-1]
    for code in list(positions):
        v = data[code]
        i = v["idx"].get(last)
        pos = positions[code]
        px = v["close"][i] if i is not None and not np.isnan(v["close"][i]) else pos.buy_price
        sell_px = px * (1 - slippage)
        gross = sell_px * pos.shares
        fee = _fee_sell(gross, commission, stamp_tax, min_commission)
        revenue = gross - fee
        cash += revenue
        pnl = revenue - pos.cost
        slip = pos.buy_slip + (px - sell_px) * pos.shares
        trades.append(Trade(
            ts_code=code, name=pos.name, buy_date=pos.buy_date,
            buy_price=round(pos.buy_price, 4), sell_date=last,
            sell_price=round(sell_px, 4), shares=pos.shares, cost=round(pos.cost, 2),
            revenue=round(revenue, 2), pnl=round(pnl, 2),
            pnl_pct=round(pnl / pos.cost * 100, 4) if pos.cost else 0.0,
            hold_days=pos.hold_bars, sell_reason="end", buy_reason=pos.buy_reason,
            fee=round(fee + pos.buy_fee, 2),
            slip_cost=round(slip, 2),
        ))
        del positions[code]
    if equity_rows:
        equity_rows[-1]["total_asset"] = round(cash, 2)
        equity_rows[-1]["cash"] = round(cash, 2)
        equity_rows[-1]["position_value"] = 0.0
        equity_rows[-1]["hold_num"] = 0

    if progress:
        progress("统计绩效…", 0.98)
    metrics = compute_metrics(equity_rows, trades, init_cash, all_dates, logs)
    # 进度空间必须**回到 1.0**：调用方把这根 0~1 线性映射到自己的区间
    # （app 是 0.62 + 0.36*pr），本函数不发最后那一帧，映射出来就永远差一截 ——
    # 实测表现为「进度条卡在 97%，而库里记录已经 done」。
    if progress:
        progress("回测完成", 1.0)
    return {"metrics": metrics, "trades": [t.__dict__ for t in trades],
            "equity": equity_rows, "logs": logs}


# ---------------------------------------------------------------------------
# 绩效指标
# ---------------------------------------------------------------------------

def compute_metrics(equity: list[dict], trades: list[Trade], init_cash: float,
                    dates: list[str], logs: list[str]) -> dict:
    m: dict = {}
    if not equity:
        return {"error": "无资金曲线"}
    eq = pd.DataFrame(equity)
    eq["trade_date"] = pd.to_datetime(eq["trade_date"])
    nav = eq["total_asset"] / init_cash
    final = float(eq["total_asset"].iloc[-1])
    m["init_cash"] = round(init_cash, 2)
    m["final_asset"] = round(final, 2)
    m["total_return"] = round((final / init_cash - 1) * 100, 4)
    m["benchmark_return"] = round((float(eq["benchmark"].iloc[-1]) - 1) * 100, 4) \
        if "benchmark" in eq else None
    m["alpha"] = round(m["total_return"] - (m["benchmark_return"] or 0), 4)

    days = (pd.Timestamp(dates[-1]) - pd.Timestamp(dates[0])).days
    years = max(days / 365.0, 1e-6)
    m["days"] = days
    m["annual_return"] = round(((final / init_cash) ** (1 / years) - 1) * 100, 4) \
        if final > 0 else -100.0

    # 回撤
    cummax = nav.cummax()
    dd = nav / cummax - 1
    m["max_drawdown"] = round(float(dd.min()) * 100, 4)
    if len(dd):
        i_end = int(dd.idxmin())
        i_start = int(nav[:i_end + 1].idxmax()) if i_end > 0 else 0
        m["max_dd_start"] = eq["trade_date"].iloc[i_start].strftime("%Y-%m-%d")
        m["max_dd_end"] = eq["trade_date"].iloc[i_end].strftime("%Y-%m-%d")
        m["max_dd_days"] = (eq["trade_date"].iloc[i_end] -
                            eq["trade_date"].iloc[i_start]).days

    # 夏普 / 波动
    rets = nav.pct_change().dropna()
    if len(rets) > 1 and rets.std() > 0:
        m["sharpe"] = round(float(rets.mean() / rets.std() * math.sqrt(252)), 3)
        m["annual_volatility"] = round(float(rets.std() * math.sqrt(252)) * 100, 4)
    else:
        m["sharpe"] = None
        m["annual_volatility"] = None
    # 卡玛
    if m["max_drawdown"] and m["annual_return"] is not None:
        m["calmar"] = round(m["annual_return"] / abs(m["max_drawdown"]), 3) \
            if m["max_drawdown"] else None

    # 交易统计
    n = len(trades)
    m["trade_count"] = n
    if n:
        df = pd.DataFrame([t.__dict__ for t in trades])
        win = df[df["pnl"] > 0]
        lose = df[df["pnl"] <= 0]
        m["win_count"] = int(len(win))
        m["lose_count"] = int(len(lose))
        m["win_rate"] = round(len(win) / n * 100, 2)
        gross_win = float(win["pnl"].sum())
        gross_lose = float(-lose["pnl"].sum())
        m["gross_profit"] = round(gross_win, 2)
        m["gross_loss"] = round(gross_lose, 2)
        m["profit_factor"] = round(gross_win / gross_lose, 3) if gross_lose > 0 else None
        m["avg_pnl"] = round(float(df["pnl"].mean()), 2)
        m["avg_pnl_pct"] = round(float(df["pnl_pct"].mean()), 4)
        m["avg_win_pct"] = round(float(win["pnl_pct"].mean()), 4) if len(win) else 0
        m["avg_lose_pct"] = round(float(lose["pnl_pct"].mean()), 4) if len(lose) else 0
        m["max_win_pct"] = round(float(df["pnl_pct"].max()), 4)
        m["max_lose_pct"] = round(float(df["pnl_pct"].min()), 4)
        m["avg_hold_days"] = round(float(df["hold_days"].mean()), 2)
        m["total_fee"] = round(float(df["fee"].sum()), 2)
        m["avg_fee"] = round(float(df["fee"].mean()), 2)
        m["fee_pct_of_init"] = round(float(df["fee"].sum()) / init_cash * 100, 4) if init_cash else 0.0
        # 滑点亏损单独统计：它在买卖价里**已经**扣过（所以收益是真亏过的），
        # 这里只是把「亏了多少」拿出来给人看 —— 高换手策略里这块常常比手续费还大。
        m["slippage_cost"] = round(float(df["slip_cost"].sum()), 2)
        m["avg_slippage_cost"] = round(float(df["slip_cost"].mean()), 2)
        m["slippage_pct_of_init"] = round(
            float(df["slip_cost"].sum()) / init_cash * 100, 4) if init_cash else 0.0
        # 交易总成本 = 佣金 + 印花税 + 滑点（README 一直是这么说的，之前只算了前两项）
        m["total_cost"] = round(m["total_fee"] + m["slippage_cost"], 2)
        m["avg_cost"] = round(float(df["fee"].mean()) + float(df["slip_cost"].mean()), 2)
        m["cost_pct_of_init"] = round(
            m["total_cost"] / init_cash * 100, 4) if init_cash else 0.0
        months = max(years * 12, 1e-6)
        m["trades_per_month"] = round(n / months, 2)
        reasons = df["sell_reason"].value_counts().to_dict()
        m["sell_reason_stat"] = {str(k): int(v) for k, v in reasons.items()}
        m["avg_positions"] = round(float(eq["hold_num"].mean()), 2)
        m["max_positions_held"] = int(eq["hold_num"].max())
    else:
        for k in ("win_count", "lose_count", "win_rate", "gross_profit", "gross_loss",
                  "profit_factor", "avg_pnl", "avg_pnl_pct", "avg_win_pct",
                  "avg_lose_pct", "max_win_pct", "max_lose_pct", "avg_hold_days",
                  "trades_per_month", "avg_positions", "max_positions_held",
                  "total_fee", "avg_fee", "fee_pct_of_init",
                  "slippage_cost", "avg_slippage_cost", "slippage_pct_of_init",
                  "total_cost", "avg_cost", "cost_pct_of_init"):
            m[k] = 0
        m["sell_reason_stat"] = {}
    return m


# ---------------------------------------------------------------------------
# 单票收益统计（个股诊断页的「策略收益」卡片）
# ---------------------------------------------------------------------------

SINGLE_POSITION = {"max_positions": 1, "position_pct": 100.0}
"""单票诊断的仓位口径：**满仓**。

组合回测默认「等分 10 份」（`position_pct=10`）—— 那是给多标的分散用的。
拿到单票上会把收益缩成真实值的 1/10（1 万元买入 100 万本金），卡片就成了误导。
其余口径（止损/止盈/持有期限/滑点/手续费）一律跟随策略配置，不另设一套。
"""

# 卡片下面网格里的小项：(metrics 键, 中文标签, 显示格式)
PERF_ITEMS = [
    ("trade_count", "交易", "count"),
    ("win_rate", "胜率", "rate"),
    ("max_drawdown", "最大回撤", "signed"),
    ("benchmark_return", "买入持有", "signed"),
]


def _signed(v) -> str:
    """带符号百分数：+12.34% / -3.21% / 0.00%。

    零**不带**正号 —— 没交易时显示「+0.00%」会被读成「小幅上涨」，那是骗人。
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    if f != f:
        return "—"
    return "0.00%" if f == 0 else f"{f:+.2f}%"


def _cls(v) -> str:
    """数值 → 涨跌色 class（涨红跌绿，flat 不上色）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "flat"
    if f != f:
        return "flat"
    if f > 0:
        return "up"
    if f < 0:
        return "down"
    return "flat"


def _perf_text(key: str, kind: str, m: dict) -> str:
    v = m.get(key)
    if kind == "count":
        return f"{int(v or 0)} 笔"
    if v is None:
        return "—"
    if kind == "rate":
        return f"{float(v):.1f}%"
    return _signed(v)


def perf_snapshot(m: dict, rng: dict, flags: dict | None = None) -> dict:
    """把回测 metrics 整理成卡片要显示的收益统计（标签/文本/涨跌色全算好）。

    与基本面卡片（`indicators.fundamental_snapshot`）同一套做法：前端只铺格子，
    不自己决定「哪个数是主数」「百分号怎么带符号」「几笔怎么拼」。

    flags 给的是窗口内 entry / buy_trigger / sell_signal 的真值根数 ——
    一笔交易都没做出来时，要能说清是**没命中**还是**命中后没触发买入**，
    否则「收益 0.00%」会被读成「策略在这只票上不赚不亏」。

    rng 里的 `freq_label` 是周期口径（"日K" / "周K"）：周线视图下 `max_hold_days`
    是**周数**、`bars` 是**周数**，卡片上必须写清是周K的 N 根，否则会被按日读。
    """
    rng = rng or {}
    if m.get("error"):
        return {"error": m["error"], "hero": None, "items": [],
                "badge": "收益不可用", "note": m["error"], "range": rng}

    fl = str(rng.get("freq_label") or "").strip()
    badge = f"满仓单票 · {fl + ' ' if fl else ''}{int(rng.get('bars') or 0)} 根"
    # 生效滑点写在口径里：卡片收益是**含滑点**的，不说出来就会被当成「理论收益」。
    # （滑点来源：买卖策略自带 → 没设才用默认；见 resolve_params）
    if rng.get("slippage") is not None:
        badge += f" · 滑点 {float(rng['slippage']) * 100:g}%"
    unit = fl or "K线"          # 「一根周K」不是「一天」—— 0 笔的原因也要跟周期同口径
    note = ""
    if not int(m.get("trade_count") or 0):
        badge = "本区间无交易"
        if flags is not None and not flags.get("entry"):
            note = f"所选区间内没有一根{unit}满足选股条件，无从买入"
        elif flags is not None:
            note = f"所选区间内有选股命中，但没有一根{unit}满足买入触发，一笔都没成交"
        else:
            note = "所选区间内未产生交易"

    tr = m.get("total_return")
    items = [{"key": k, "label": lab, "value": m.get(k),
              "text": _perf_text(k, kind, m),
              "cls": _cls(m.get(k)) if kind == "signed" else ""}
             for k, lab, kind in PERF_ITEMS]
    return {
        "hero": {"label": "策略收益", "key": "total_return", "value": tr,
                 "text": _signed(tr), "cls": _cls(tr)},
        "items": items,
        "badge": badge,
        "note": note,
        "range": rng,
        "flags": flags or {},
        "metrics": {k: m.get(k) for k in (
            "total_return", "benchmark_return", "alpha", "trade_count", "win_rate",
            "win_count", "lose_count", "max_drawdown", "avg_hold_days", "final_asset",
            "sell_reason_stat", "days")},
    }


def single_stock_perf(code: str, df: pd.DataFrame, cfg: dict,
                      sig: dict | None = None,
                      start: str | None = None, end: str | None = None,
                      params: dict | None = None,
                      freq_label: str = "") -> dict:
    """单只股票照策略跑一遍，给出收益统计（个股诊断页「策略收益」卡片）。

    **用的是组合回测同一个引擎**（`run_backtest`），只是把仓位口径换成满仓
    （见 `SINGLE_POSITION`）。对照值取「同期买入持有」：不传 benchmark 时引擎
    按池内等权净值化，单票时恰是该票自身的买入持有收益，`alpha` 即超额。

    df 传周K也行（个股诊断页切周线时就是这么用的）：引擎本来就按「一根 K 线」
    推进，`max_hold_days` 于是变成周数。`freq_label` 只是把这件事实写进卡片文案。
    """
    cfg = st.selection_cfg(cfg)
    d = pack_one(code, df, cfg, sig=sig)
    if d is None:
        return {"error": "本地该股票 K 线不足，无法统计收益", "hero": None,
                "items": [], "badge": "收益不可用", "note": "本地该股票 K 线不足",
                "range": {}}
    s = start or str(d["dates"][0])
    e = end or str(d["dates"][-1])
    win = (d["dates"] >= s) & (d["dates"] <= e)
    rng = {"start": s, "end": e, "bars": int(np.count_nonzero(win)),
           "freq_label": freq_label or ""}
    flags = {k: int(np.count_nonzero(d[k][win]))
             for k in ("entry", "buy_trigger", "sell_signal")}
    p = dict(SINGLE_POSITION)
    p.update(params or {})
    # 卡片的收益口径：滑点跟随买卖策略（引擎侧同一套合成规则），这里只为「说出来」取一次
    rng["slippage"] = resolve_params(cfg, p).get("slippage")
    res = run_backtest({code: d}, cfg, s, e, params=p)
    m = res.get("metrics") or {}
    if m.get("error"):
        return {"error": m["error"], "hero": None, "items": [],
                "badge": "收益不可用", "note": m["error"], "range": rng}
    return perf_snapshot(m, rng, flags)
