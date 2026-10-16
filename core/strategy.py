# -*- coding: utf-8 -*-
"""策略模型：配置结构、校验、信号生成。"""
from __future__ import annotations

import json
import re
from typing import Any

import numpy as np
import pandas as pd

from core import db, expr, indicators as ind, log, universe
# 周期枚举的唯一实现在 `universe`（模型层最底，`pool` / `strategy` / `watch` 都据此归一化）。
# 这里转出来是为了让老调用点 `st.FREQS` / `st.norm_freq` 继续可用。
from core.universe import FREQS, FREQ_ORDER, DEFAULT_FREQ, norm_freq  # noqa: F401

_log = log.get(__name__)
# 条件池求值失败的表达式（去重）。全市场逐票判一次，不去重会把日志刷爆，
# 但**完全不留声**更糟：结果是「0 命中且毫无线索」，用户只能来问"为什么查不出来"。
_POOL_WARNED: set[str] = set()

# ---------------------------------------------------------------------------
# 配置结构
# ---------------------------------------------------------------------------

DEFAULT_RISK = {
    "stop_loss_pct": None,      # 止损，如 -7 表示亏损 7% 止损
    "take_profit_pct": None,    # 止盈，如 15 表示盈利 15% 止盈
    "max_hold_days": None,      # 最长持有 K 线数
}

DEFAULT_EXEC = {
    "buy_price": "open",        # 买入价：open / close
    "sell_price": "open",       # 信号卖出价：open / close
    "max_hold_price": "close",  # 到期卖出价
    # 滑点：**买卖策略自带**的成交假设。None = 未设 → 跟随回测时的参数 / 默认值。
    # 单位是**小数**（0.001 = 千分之 1 = 0.1%），与 config.BACKTEST_DEFAULTS["slippage"]
    # 完全同口径；**界面按百分数填**（填 0.1 表示 0.1%）→ 换算只在 static/js/app.js 的
    # window.SLIP 里做一次，别在页面里再各写一份。
    # 回测里它对买入是**上浮**（buy_px = px*(1+slip)）、对卖出是**下调**
    # （sell_px = px*(1-slip)），即永远是把亏损算进去的方向，不存在「回测白占便宜」。
    "slippage": None,
}

# 滑点可填范围（小数）。唯一来源 —— 校验与 /api/config 都取这里，前端别再写死。
# 上限 3%：A 股涨跌停 10%，成交价偏离开盘价到 3% 已属极端（相当于开盘买在接近跌停/涨停）。
SLIPPAGE_MIN = 0.0
SLIPPAGE_MAX = 0.03

DEFAULT_POOL = {
    # 静态过滤项（剔ST/板块/上市天数/最小成交额）—— 唯一来源在 `universe.DEFAULT_FILT`，
    # 选股策略那边用的是同一份。这里只是**向后兼容**：2026-09-18 之前这些过滤项写在
    # 买卖策略上，老配置里还留着，没选池子时按它兜底。
    **universe.DEFAULT_FILT,
    "where": [],                # 旧字段：条件池（选股逻辑已归选股策略，只为老配置兜底）
}

DEFAULT_CONFIG: dict[str, Any] = {
    "timeframe": "daily",       # daily / weekly
    "indicators": {},           # 覆盖指标参数
    # ⚠ 选的股票从哪来：由**选股策略**在运行时注入（见 `selection_cfg`）。
    #   这三个键保留是给「老配置」和「程序化临时策略」用的 —— 买卖策略页面不再编辑它们。
    "entry": [],                # 选股条件（T日收盘，全部满足）—— 空 = 不额外筛（选股交给选股策略）
    "entry_any": [],            # 选股或条件（任一满足，可留空）
    "weekly_filter": [],        # 周线过滤（timeframe=daily 时生效；新写法是放进选股策略条件里）
    "buy_trigger": [],          # 次日买入触发（全部满足；留空=次日无条件买入）
    "sell_signal": [],          # 持仓期卖出信号（全部满足即卖出）
    "score_expr": "",           # 评分表达式（越大越优先）
    "risk": dict(DEFAULT_RISK),
    "exec": dict(DEFAULT_EXEC),
    "pool": dict(DEFAULT_POOL),
}

COND_FIELDS = ["entry", "entry_any", "weekly_filter", "buy_trigger", "sell_signal"]


def merge_config(cfg: dict | None, keep_comments: bool = False) -> dict:
    """把用户配置合并到默认结构。

    `keep_comments`（`#` 注释怎么处理）：
      · `False`（默认 = **运行时口径**）：剥掉行内注释、丢弃纯注释行。注释不是条件，
        绝不能进求值循环 —— 空表达式会 raise（`compute_pool` 里则按 False），
        表现出来是「全市场 0 命中」，看不出是条件坏了还是真没票。
      · `True`：原样保留（只删空行）—— **只有保存策略时用**。用户写给自己看的批注
        要能原样回显，存一次就没了等于没这个功能。
    """
    out = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg = cfg or {}
    for k, v in cfg.items():
        if k in ("risk", "exec", "pool", "indicators") and isinstance(v, dict):
            out[k].update(v)
        else:
            out[k] = v
    for f in COND_FIELDS:
        v = out.get(f)
        if isinstance(v, str):
            lines = v.split("\n")
        elif v is None:
            lines = []
        else:
            lines = [str(x) for x in v]
        if keep_comments:
            out[f] = [str(x).strip() for x in lines if str(x).strip()]
        else:
            out[f] = expr.clean_conds(lines)
    out["timeframe"] = norm_freq(out.get("timeframe"))
    return out


def config_json(cfg: dict) -> str:
    return json.dumps(cfg, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def validate_config(cfg: dict, known_names: set[str] | None = None) -> list[str]:
    """返回错误列表，空表示通过。"""
    errs: list[str] = []
    cfg = merge_config(cfg)
    names = set(known_names) if known_names else None
    if names:
        names |= set(expr.FUNCTIONS)

    # 「选股条件为空」不再是错误：2026-09-18 起选股逻辑归**选股策略**，
    # 买卖策略可以一条选股条件都没有（回测时由选股策略注入 entry，见 selection_cfg）。
    # 一条池子也不选 = 静态池内不额外筛，这是合法用法。

    # 周线过滤必须使用 w_ 前缀字段
    if cfg["timeframe"] == "daily":
        for i, e in enumerate(cfg["weekly_filter"]):
            if "w_" not in e:
                errs.append(
                    f"[周线过滤 第{i+1}条] 请使用 w_ 前缀字段"
                    f"（如 w_close、w_boll_lower、w_ma20），否则会在日线上求值"
                )

    # 逐条语法/字段校验
    for f in COND_FIELDS:
        for i, e in enumerate(cfg[f]):
            ok, msg = expr.check_expr(e, None)
            if not ok:
                errs.append(f"[{LABELS[f]} 第{i+1}条] {msg}")
    if expr.is_active(cfg.get("score_expr")):
        ok, msg = expr.check_expr(cfg["score_expr"], None)
        if not ok:
            errs.append(f"[评分表达式] {msg}")

    # 条件池（页面传入的 _pool，或策略自带的 pool.where）
    pool_spec = cfg.get("_pool")
    if pool_spec:
        for e in universe.validate(pool_spec, names):
            errs.append(e)
    elif (cfg.get("pool") or {}).get("where"):
        for e in universe.validate({"where": cfg["pool"]["where"]}, names):
            errs.append(e)

    r = cfg["risk"]
    for k in ("stop_loss_pct", "take_profit_pct", "max_hold_days"):
        v = r.get(k)
        if v in ("", None):
            continue
        try:
            r[k] = float(v)
        except (TypeError, ValueError):
            errs.append(f"[风控] {k} 必须是数字")
    for k in ("max_positions", "position_pct"):
        if cfg.get(k) not in (None, ""):
            try:
                cfg[k] = float(cfg[k])
            except (TypeError, ValueError):
                errs.append(f"{k} 必须是数字")

    # 滑点（策略自带，见 DEFAULT_EXEC）：留空合法 —— 那表示「跟随回测参数」。
    # 报错文案按**界面单位（百分数）**说，否则用户填 0.5 收到「必须在 0~0.03 之间」
    # 会完全不知道该怎么改。
    ex = cfg["exec"]
    v = ex.get("slippage")
    if v not in ("", None):
        try:
            fv = float(v)
        except (TypeError, ValueError):
            errs.append("[滑点] 必须是数字（百分数，如 0.1 表示 0.1%）")
        else:
            if not (SLIPPAGE_MIN <= fv <= SLIPPAGE_MAX):
                errs.append(
                    f"[滑点] 请填 {_pct(SLIPPAGE_MIN)} ~ {_pct(SLIPPAGE_MAX)}"
                    f"（当前 {_pct(fv)}）"
                )
            else:
                ex["slippage"] = fv
    return errs


def _pct(v: float) -> str:
    """小数 → 百分数文案（0.001 → "0.1%"）。滑点的报错文案专用。"""
    return f"{float(v) * 100:g}%"


LABELS = {
    "entry": "选股条件",
    "entry_any": "选股或条件",
    "weekly_filter": "周线过滤",
    "buy_trigger": "买入触发",
    "sell_signal": "卖出信号",
}


# ---------------------------------------------------------------------------
# 信号生成
# ---------------------------------------------------------------------------

SIGNAL_COLS = ["entry", "buy_trigger", "sell_signal", "score"]


def _namespace(df: pd.DataFrame) -> dict[str, Any]:
    """构造表达式可用的变量空间。"""
    ns: dict[str, Any] = {}
    for c in df.columns:
        if c.startswith("_"):
            continue
        s = df[c]
        if pd.api.types.is_numeric_dtype(s) or pd.api.types.is_bool_dtype(s):
            ns[c] = s.to_numpy(dtype=float, na_value=np.nan)
        elif c in ("ts_code", "name"):
            ns[c] = s
    ns["n"] = len(df)
    ns["True"] = True
    ns["False"] = False
    return ns


def compute_signals(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """为单只股票的（含指标）DataFrame 计算 entry/buy_trigger/sell_signal/score。"""
    cfg = merge_config(cfg)
    n = len(df)
    ns = _namespace(df)
    res = pd.DataFrame(index=df.index)

    def _and(exprs: list[str], default_false: bool = True) -> np.ndarray:
        if not exprs:
            return np.zeros(n, dtype=bool) if default_false else np.ones(n, dtype=bool)
        out = np.ones(n, dtype=bool)
        for e in exprs:
            out &= expr.evaluate_bool(e, ns, n)
        return out

    def _or(exprs: list[str]) -> np.ndarray:
        if not exprs:
            return np.zeros(n, dtype=bool)
        out = np.zeros(n, dtype=bool)
        for e in exprs:
            out |= expr.evaluate_bool(e, ns, n)
        return out

    # ⚠ 空 entry 的语义是**全 True（不额外筛）**，不是「恒不画/恒不成立」。
    #   2026-09-18 起选股逻辑归「选股策略」：买卖策略可以一条选股条件都不写，
    #   此时「谁能买」完全由选股策略（池子条件，见 selection_cfg）决定。
    #   旧版本这里是 `_and([])` → 全 False，因为校验强制要求 entry 非空；
    #   现在校验放开了，再按恒 False 算会让「静态池 + 纯买卖规则」一只都选不出来。
    entry = _and(cfg["entry"]) if cfg["entry"] else np.ones(n, dtype=bool)
    if cfg["entry_any"]:
        entry &= _or(cfg["entry_any"])
    if cfg["weekly_filter"]:
        entry &= _and(cfg["weekly_filter"])

    res["entry"] = entry
    # 买入触发为空 -> 次日无条件买入
    res["buy_trigger"] = _and(cfg["buy_trigger"], default_false=False) if cfg["buy_trigger"] \
        else np.ones(n, dtype=bool)
    res["sell_signal"] = _and(cfg["sell_signal"]) if cfg["sell_signal"] \
        else np.zeros(n, dtype=bool)

    if cfg.get("score_expr"):
        try:
            sc = expr.evaluate(cfg["score_expr"], ns, n)
            res["score"] = np.asarray(sc, dtype=float) if np.ndim(sc) else float(sc)
        except expr.ExprError:
            res["score"] = np.nan
    else:
        res["score"] = np.nan
    return res


# 个股诊断图上「策略信号」画在哪、什么形状 —— 在这里定义一次。前端只按 key 上色，
# 位置（上/下）与形状照这里的 side/shape 来，不许自己编 —— 否则换个策略就画歪。
SIGNAL_PLOT: tuple[dict, ...] = (
    {"key": "entry",       "name": "选股命中", "f": "entry",       "side": "below", "shape": "dot"},
    {"key": "buy_trigger", "name": "买入触发", "f": "buy_trigger", "side": "below", "shape": "arrow"},
    {"key": "sell_signal", "name": "卖出信号", "f": "sell_signal", "side": "above", "shape": "arrow"},
)


def _has_selection(cfg: dict) -> bool:
    """本次运行到底有没有「选股条件」。

    四者任一都算：`entry` / `entry_any` / `weekly_filter` / 条件池的 where。
    ⚠ 只看 `cfg["entry"]` 会漏：`selection_cfg` 在 entry 已经写了东西时**原样优先**，
    所以「只有 weekly_filter 的日线策略」entry 还是空的，但 `compute_signals` 把
    weekly_filter AND 进 entry 之后确实筛出了票（cnt>0）。照旧判据会把它标成
    「不铺命中点」—— 图上少一档点、数却对得上，是最难发现的那种不一致。
    """
    if has_entry_conds(cfg):
        return True
    return bool(resolve_pool(cfg)["where"])


def has_entry_conds(cfg: dict) -> bool:
    """买卖策略**自己**写了「选股条件」没有（entry / entry_any / weekly_filter）。

    ⚠ 这里判据是「剥掉注释后还有没有真条件」，不能直接 `if cfg.get("entry")`：
    只写注释也非空，会让 `selection_cfg` 以为「策略已自带 entry」而不把选股策略的
    条件抬进来 —— 池子形同虚设，而且一声不响。
    """
    for f in ("entry", "entry_any", "weekly_filter"):
        if expr.clean_conds(cfg.get(f) or []):
            return True
    return False


def signal_marks(flags: dict, cfg: dict) -> list[dict]:
    """把逐日信号整理成「K 线上要画哪些标记」的清单（含当前有几根、为什么不画）。

    三个「空条件」的分歧必须如实写出来，否则图上会骗人：
      · entry 空 = 「不额外筛」（compute_signals 里按全 True 算）→ **一根都不画**，
        否则会铺满整屏点。命中点要看「选股策略」的条件，所以页面请带上选股策略；
      · buy_trigger 空 = 规则是「次日无条件买入」→ **每一根都是 True**，
        真照它铺箭头会满屏红三角，所以标成不画并说明原因；
      · sell_signal 空 = 靠到期 / 止损止盈离场，本来就没有逐日信号。
    """
    cfg = merge_config(cfg)
    out: list[dict] = []
    for spec in SIGNAL_PLOT:
        col = flags.get(spec["f"])
        if col is None:
            continue
        cnt = int(np.count_nonzero(np.asarray(col, dtype=bool)))
        item = dict(spec, count=cnt, draw=cnt > 0, note="")
        if not (cfg.get(spec["f"]) or []):
            if spec["key"] == "buy_trigger":
                item["draw"] = False
                item["note"] = "未设「买入触发」= 次日无条件买入（不铺满箭头）"
            elif spec["key"] == "sell_signal":
                item["note"] = "未设「卖出信号」，按到期 / 止损止盈离场"
            elif not _has_selection(cfg):
                item["draw"] = False
                item["note"] = ("没选「选股策略」（或它没写条件）= 不额外筛，"
                                "所以不铺命中点")
            elif not cnt:
                item["note"] = "所选区间内无命中"
        elif not cnt:
            item["note"] = "所选区间内无命中"
        out.append(item)
    return out


# `w_close` / `w_boll_lower` 这类周线字段的识别式：`\b` 保证 low_close 里的 "w_" 不会被误伤
WEEKLY_FIELD_RE = re.compile(r"\bw_([A-Za-z_][A-Za-z0-9_]*)")


def _drop_w_prefix(exprs: list[str] | None) -> list[str]:
    return [WEEKLY_FIELD_RE.sub(r"\1", str(e)) for e in (exprs or [])]


def to_weekly_view(cfg: dict) -> dict:
    """把策略配置翻译到「周K面板」上求值（个股诊断页切周线时用）。

    周线面板上每一根就是**一个已收盘的完整周**，所以 `w_` 前缀在这里是多余的：
      · 日线口径下 `w_close` 取的是「最近一根已收盘周K」（market._merge_weekly 的
        asof），落在该周最后一个交易日时**正好等于当周那根周K的 close** ——
        去掉前缀后取值同源，不是两套数；
      · `weekly_filter` 原本是「日线条件 AND 周线条件」，在周K上退化成同一条 K 线上的
        AND，所以字段名去前缀、仍留在 weekly_filter 里（compute_signals 一样会 AND 进 entry）；
      · `timeframe` 跟着改成 weekly —— 面板加载与回测都按它分支。

    条件与评分表达式**只做前缀替换**，不做别的改写：`close` 在周K上就是周收盘，
    「MA5」自然变成 5 周均线。这也是为什么必须把「这不是日线那个策略」写进 view.notes。
    """
    cfg = merge_config(cfg)
    out = dict(cfg)
    for f in COND_FIELDS:
        out[f] = _drop_w_prefix(cfg.get(f))
    if cfg.get("score_expr"):
        out["score_expr"] = WEEKLY_FIELD_RE.sub(r"\1", str(cfg["score_expr"]))
    pool = dict(cfg.get("pool") or {})
    if pool.get("where"):
        pool["where"] = _drop_w_prefix(pool["where"])
    out["pool"] = pool
    spec = cfg.get("_pool")
    if isinstance(spec, dict) and spec.get("where"):
        spec = dict(spec)
        spec["where"] = _drop_w_prefix(spec["where"])
        out["_pool"] = spec
    out["timeframe"] = "weekly"
    return out


def resolve_pool(cfg: dict) -> dict:
    """取出本次运行的「条件池」描述（归一化）。

    优先级：运行期注入的 cfg["_pool"]（页面选的条件池）> 策略自带 cfg["pool"].where。

    ⚠ 返回值里带 `timeframe` —— 那是**选股策略自己的周期**（`pool_strategy.timeframe`），
      由 `universe.parse/expand` 从库里带出来；`selection_cfg()` 靠它决定整条链跑日线还是周线。
    ⚠ **周K 池子的条件在这里统一去 `w_` 前缀**（2026-10-10）：周线面板上 `close` 就是周收盘，
      `w_` 是多余的、而且那根 K 上**没有 `w_` 列** ⇒ 留着会让条件求值按 False（**静默 0 命中**）。
      ⚠ 为什么必须放这里、而不是只放 `to_weekly_view()`：`to_weekly_view` 只翻译**就地写在
        cfg 里的**条件，而按 `pool_id` 引用的策略条件**在库里** —— 它根本碰不到。
        这里是**唯一**的「取本次运行条件池」入口，放这儿下游（`selection_cfg` 抬 entry、
        `compute_pool` 求掩码、`required_*_fields` 定加载字段、`referenced_fields` 出列）
        自动全部一致。
    """
    cfg = cfg or {}
    spec = cfg.get("_pool")
    if spec:
        out = universe.parse(spec)
    else:
        p = cfg.get("pool") or {}
        if p.get("where"):
            out = universe.parse({"base": None, "where": p.get("where") or []})
        else:
            out = {"kind": "static", "base": None, "where": [],
                   "timeframe": DEFAULT_FREQ}
    if out.get("timeframe") == "weekly" and out.get("where"):
        out = dict(out)
        out["where"] = _drop_w_prefix(out["where"])
    return out


def pool_filters(cfg: dict) -> dict:
    """本次运行的「静态池过滤项」：剔 ST / 板块 / 上市天数 / 最小成交额。

    来源唯一 = **选股策略**（`cfg["_pool"]` 归一化后的 `filt`）。
    没选条件池时退回 `cfg["pool"]` 里的同名字段（2026-09-18 之前老配置写在那里），
    再缺就用 `universe.DEFAULT_FILT` 兜底 —— 别让老配置静默变成「什么都不剔」。
    """
    s = resolve_pool(cfg)
    legacy = cfg.get("pool") or {}
    out = dict(universe.DEFAULT_FILT)
    if s.get("kind") == "cond":
        out.update(s.get("filt") or {})
    elif legacy:
        for k in universe.DEFAULT_FILT:
            if k in legacy:
                out[k] = legacy[k]
    return universe.norm_filt(out)


def selection_cfg(cfg: dict) -> dict:
    """把「选股策略」的条件填进 `cfg["entry"]`，得到「选股 + 买卖」合体配置。

    这是 2026-09-18 那次概念归位的关键一步：**选股逻辑只在选股策略里**，
    买卖策略不再提供 entry。但下游（`compute_signals` / 回测 / 选股）认的是
    `cfg["entry"]`，所以运行时在这里把池子条件「抬」成 entry ——
    于是三处求值逻辑一行不用改，只是「条件从哪来」变了。

    三条规矩：
      · 买卖策略自己写了 entry（老配置 / 程序化临时策略）→ **原样优先**，不动它；
      · 否则用选股策略的条件（引用选股策略时，这条就是它）；
      · 都没有 → entry 留空，`compute_signals` 按「全 True」处理（池内不额外筛）。

    ⚠ **周期也跟着选股策略走**（2026-10-10）：池子是 `weekly` ⇒ 整个 cfg 走一遍
      `to_weekly_view()`（条件与评分去掉 `w_` 前缀、`timeframe` 改 weekly），于是
      选股 / 回测 / 提醒全在周线上跑 —— 这就是用户要的「整条链按周K跑」。
      `daily` 池子则一切照旧（买卖策略自己的 timeframe 说了算）。
      判定只有这一处，别在下游各自再判一次。
      ⚠ **别加 `and cfg["timeframe"] != "weekly"` 这种守卫**：买卖策略本身也是周K 时
        `cfg["timeframe"]` 已经是 weekly，守卫会把这一步整个跳过 ⇒ 池子条件里的 `w_`
        前缀**留着没去掉**，而周线面板上根本没有 `w_` 列 ⇒ 求值按 False ⇒ **静默 0 命中**。
        `to_weekly_view()` 是幂等的（再剥一次前缀还是原样），该调就调。
    """
    cfg = merge_config(cfg)
    if resolve_pool(cfg).get("timeframe") == "weekly":
        cfg = to_weekly_view(cfg)
    if has_entry_conds(cfg):
        return cfg
    where = resolve_pool(cfg)["where"]
    if where:
        cfg = {**cfg, "entry": list(where)}
    return cfg


def compute_pool(df: pd.DataFrame, cfg: dict) -> np.ndarray:
    """逐日计算「是否在池内」的布尔掩码（长度 = len(df)）。

    与 entry 同时机：在 T 日这根 K 线上求值，`ref()` 可向前看历史。
    空池子条件时返回全 True，退化为纯静态池。
    """
    cfg = merge_config(cfg)
    n = len(df)
    mask = np.ones(n, dtype=bool)
    conds = resolve_pool(cfg)["where"]
    if conds:
        ns = _namespace(df)
        for e in conds:
            try:
                mask &= expr.evaluate_bool(e, ns, n)
            except expr.ExprError as exc:
                # 条件写错 / 字段没加载 → 当天不入池（宁可少选也不放错）。但必须留声：
                # 静默 False 的表现是「全市场 0 命中」，看不出是条件坏了还是真没票。
                if e not in _POOL_WARNED:
                    _POOL_WARNED.add(e)
                    _log.warning("条件池求值失败，该条按 False 处理（全市场将无人入池）：%s → %s",
                                 e, exc)
                mask &= False
    # 最小日成交额来自选股策略的 filt（老配置从 cfg["pool"] 兜底）
    min_amount = float(pool_filters(cfg).get("min_amount") or 0)
    if min_amount and "amount" in df.columns:
        amt = pd.to_numeric(df["amount"], errors="coerce").to_numpy(dtype=float)
        with np.errstate(invalid="ignore"):
            mask &= np.where(np.isnan(amt), False, amt >= min_amount)
    return mask


def referenced_fields(cfg: dict) -> list[str]:
    """按「在条件/评分里出现的先后」返回引用到的字段名（含 `w_` 版本与条件池条件）。

    选股结果表就用这个加列 —— 把用户自己写在条件里的字段摊到表上，
    省得每只票都点一次「命中明细」去核对。顺序有意义（先出现的排前面），
    所以用 `identifiers_in_order` 而不是 set 版。
    """
    cfg = merge_config(cfg)
    exprs = [e for f in COND_FIELDS for e in cfg[f]]
    if cfg.get("score_expr"):
        exprs.append(cfg["score_expr"])
    exprs += list(resolve_pool(cfg)["where"])
    out: list[str] = []
    seen: set[str] = set()
    for e in exprs:
        for n in expr.identifiers_in_order(e):
            if n not in seen:
                seen.add(n)
                out.append(n)
    return out


def required_fields(cfg: dict) -> set[str]:
    """策略所有条件与评分表达式里引用到的变量名（含 w_ 前缀与条件池条件）。"""
    cfg = merge_config(cfg)
    names: set[str] = set()
    for f in COND_FIELDS:
        for e in cfg[f]:
            names |= expr.extract_identifiers(e)
    if cfg.get("score_expr"):
        names |= expr.extract_identifiers(cfg["score_expr"])
    # 条件池里引用的字段也要算进来，否则 only= 裁剪会把 boll_upper 之类的指标漏掉
    for e in resolve_pool(cfg)["where"]:
        names |= expr.extract_identifiers(e)
    return names


BASE_FIELDS = {"open", "high", "low", "close", "pre_close", "vol", "amount", "pct_chg"}


def required_daily_fields(cfg: dict) -> set[str]:
    """日线面板需要计算的字段集合（用于裁剪指标计算量）。"""
    names = {x for x in required_fields(cfg) if not x.startswith("w_")}
    names |= BASE_FIELDS
    return names


def required_weekly_fields(cfg: dict) -> set[str] | None:
    """策略用到的 w_ 字段集合。None 表示不需要周线数据。"""
    cfg = merge_config(cfg)
    if cfg["timeframe"] != "daily":
        return None
    names = {x for x in required_fields(cfg) if x.startswith("w_")}
    if not names:
        return None
    return names | {"w_close", "w_pct_chg"}


FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def condition_field_help() -> dict[str, str]:
    """日线字段名 → 中文说明。前端「可用字段参考」与输入提示共用，不在前端写死。"""
    names: dict[str, str] = {}
    for f, d in ind.FIELD_HELP:
        for part in f.split(" / "):
            part = part.strip()
            if FIELD_RE.match(part):
                names[part] = d
    base = ["open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount",
            "change_amt", "turnover_rate", "volume_ratio", "pe", "pb", "ps",
            "total_mv", "circ_mv"]
    for b in base:
        names.setdefault(b, "行情字段")
    for c in ("ma5", "ma10", "ma20", "ma30", "ma60", "ma120", "ma250",
              "vma5", "vma10", "vma20",
              "boll_upper", "boll_mid", "boll_lower", "boll_width", "boll_pos", "boll_dev",
              "macd_dif", "macd_dea", "macd_hist", "kdj_k", "kdj_d", "kdj_j",
              "rsi6", "rsi12", "rsi24", "atr14", "atr_pct", "cci14", "wr14",
              "mtm12", "mtmpct12", "obv", "obv_ma10", "vr",
              "dmi_pdi", "dmi_mdi", "dmi_adx",
              "hh20", "ll20", "pos20", "hh60", "ll60", "pos60", "hh120", "ll120", "pos120",
              "ret1", "ret5", "ret20", "ret60", "ret120",
              "vol_ratio5", "vol_ratio10", "ma_bull",
              "amplitude", "avg_price", "body", "upper_shadow", "lower_shadow",
              "gap_open", "is_up", "is_down", "close_pos", "limit_up", "limit_down"):
        names.setdefault(c, "技术指标")
    return names


def condition_weekly_field_help() -> dict[str, str]:
    """周线过滤可用字段：日线字段整体加 w_ 前缀。

    周线面板是由日线重采样后**算同一套指标**得到（见 market.resample_weekly /
    _merge_weekly），所以 w_ 后面跟的可以是任意日线字段名，而不只是均线类。
    """
    return {f"w_{k}": f"周线·{v}" for k, v in condition_field_help().items()}


def condition_field_names() -> list[str]:
    """所有可用字段名（日线 + 周线 w_ 前缀），用于条件校验与前端提示。

    必须包含 w_ 系列：条件池的 where 里也能写周线字段（策略据此贴周线数据），
    漏了会让合法条件被报「未知字段」。
    """
    return sorted(set(condition_field_help()) | set(condition_weekly_field_help()))


# ---------------------------------------------------------------------------
# 策略 CRUD
# ---------------------------------------------------------------------------

def list_strategies() -> list[dict]:
    # ⚠ 排序三段：先按用户拖出来的 `sort`，再内置优先，最后 id 兜底。
    # · 第 1 段 sort 是主序。⚠ 为什么 `builtin DESC` **不能**打头（2026-10-10 改）：
    #   内置样例也能拖了（用户要求「MACD与KDJ共振 和 开盘确认也可以拖动排序」），
    #   若还让 builtin 打头，内置就被钉死在前两条 —— 拖内置 = 白拖。
    # · 第 2 段 `builtin DESC` 只在第 1 段并列时才起作用，也就是**从没拖过的开箱默认态**
    #   （所有行 sort 都是默认 0）：那时靠它把两条内置样例排在自定义之前；
    #   一旦拖过，sort 从 SORT_BASE 起算、各不相同，这一段自然失效。
    # · 第 3 段 id 负责默认态内部的定序（新策略 id 最大 ⇒ 落在自定义末尾）。
    rows = db.query(
        "SELECT id, name, note, timeframe, config, builtin, sort, created_at, updated_at "
        "FROM strategy ORDER BY sort ASC, builtin DESC, id ASC"
    )
    for r in rows:
        if isinstance(r.get("config"), (str, bytes)):
            r["config"] = json.loads(r["config"])
    return rows


# 拖拽排序写入的 sort 起点。
# ⚠⚠ **必须是「没拖过的行永远达不到」的区间**：`sort` 列的默认值是 0，
#   若重排也从 0 开始写（0,1,2,…），就会和「没拖过的那些行的 0」撞在一起 ——
#   实测踩过：把 3 条拖到最前、写成 sort=0,1,2，结果一条**没拖过**的策略
#   （sort 仍是 0）就横插到了第 1 位。折中办法是重排一律从本常量起算，
#   没拖过的恒为 0 ⇒ 全部排在「拖过的」之前，两者永远不相等、不会互相插队。
#   （副作用：首次拖拽后，其余没拖过的会聚到前面。这符合直觉 ——
#    没动过的保持原样在上，你拖的那几条在下面按你排的走。）
SORT_BASE = 100000


def reorder_strategies(ids: list[int]) -> list[int]:
    """按给定顺序把 `sort` 重写成 `SORT_BASE, SORT_BASE+1, …`（拖拽排序的落库入口）。

    - **只认传进来的这些 id**，其余策略的 sort 不动 —— 前端只拖可见列表，
      不该因为一次拖拽把没在列表里的记录也搅一遍。
    - ⚠ **内置策略（builtin=1）照样参与排序**（2026-10-10 用户要求内置样例也能拖）。
      这里**只写 `sort`、绝不碰 `builtin` 标记**：内置仍然不可删、仍由
      `ensure_builtins()` 维护 —— `install_builtins(force)` 只更新 note/timeframe/config，
      **不动 sort**，所以拖出来的位置重启后还在。
    - ⚠ **不存在的 id 直接忽略、不报错** —— 拖拽是批量操作，为一个脏 id 整批失败不划算。
    - 返回真正被改动的 id（按新顺序），供接口回报、便于回归断言。
    """
    seq: list[int] = []
    seen: set[int] = set()
    for x in ids:
        v = int(x)
        if v in seen:
            continue          # 重复 id 只算一次，否则 sort 会跳号
        seen.add(v)
        seq.append(v)
    if not seq:
        return []
    # 一次查清哪些 id 真实存在（脏 id 要忽略；内置不跳过，一样写 sort）
    ph = ",".join(["%s"] * len(seq))
    rows = db.query(f"SELECT id FROM strategy WHERE id IN ({ph})", tuple(seq))
    alive = {int(r["id"]) for r in rows}
    out: list[int] = []
    pos = SORT_BASE
    for sid in seq:
        if sid not in alive:
            continue
        db.execute("UPDATE strategy SET sort=%s WHERE id=%s", (pos, sid))
        out.append(sid)
        pos += 1
    return out


def get_strategy(sid: int) -> dict | None:
    r = db.query_one("SELECT * FROM strategy WHERE id=%s", (sid,))
    if r and isinstance(r.get("config"), (str, bytes)):
        r["config"] = json.loads(r["config"])
    return r


def save_strategy(name: str, note: str, config: dict, sid: int | None = None) -> int:
    # keep_comments=True：条件里的 `# 批注` 要原样落库、下次打开还在（运行时另走
    # merge_config 的默认口径剥掉它们，注释永远不进求值）。
    cfg = merge_config(config, keep_comments=True)
    payload = (name.strip(), note or "", cfg.get("timeframe", "daily"),
               config_json(cfg))
    if sid:
        db.execute(
            "UPDATE strategy SET name=%s, note=%s, timeframe=%s, config=%s WHERE id=%s",
            payload + (sid,),
        )
        return sid
    return db.insert(
        "INSERT INTO strategy (name, note, timeframe, config) VALUES (%s,%s,%s,%s)",
        payload,
    )


def delete_strategy(sid: int) -> None:
    db.execute("DELETE FROM strategy WHERE id=%s AND builtin=0", (sid,))


# ---------------------------------------------------------------------------
# 内置策略（买卖策略样例）
# ---------------------------------------------------------------------------

# ⚠ 这里是**买卖规则**的样例，一律不含选股条件 —— 2026-09-18 起选股逻辑只归
# 「选股策略」（见 universe.PRESETS 里的「BOLL下轨低吸」「均线金叉 + 放量」）。
# 回测时「选股策略 × 买卖策略」两个下拉一配就是一条完整策略。
# ⚠ 2026-09-29 起**只保留 1 条**内置样例（用户要求「保留第一条，其余取消内置」）。
#   原来还有一条「跌破MA10离场」：已从本清单移出，库里那条记录**照旧留着**，只是不再带
#   内置标记（builtin=0 ⇒ 界面上可改名、可删除）。
#   ⚠「从清单移出」≠「删记录」：记录是用户可能正在用的策略，只去掉标记；而清单必须同步收窄，
#   否则用户删掉它之后，下次启动 `ensure_builtins()` 会把它**当内置重新插回来**。
BUILTIN_STRATEGIES = [
    {
        "name": "开盘确认",
        "note": (
            "【买卖规则样例 · 高开进低开出】次日开盘价高于前一日收盘就买入（开盘价成交）；"
            "持仓期间任意一日开盘价低于前一日收盘就卖出（开盘价成交）；最长持有 10 根 K 线。\n\n"
            "它**只描述买卖时点**，不含选股条件 —— 买什么由你在「选股策略」里选。"
            "例如配「BOLL下轨低吸」就是经典的「下轨低吸 + 次日高开确认」。"
        ),
        "config": {
            "timeframe": "daily",
            "buy_trigger": ["open > ref(close, 1)"],
            "sell_signal": ["open < ref(close, 1)"],
            "risk": {"max_hold_days": 10},
            "exec": {"buy_price": "open", "sell_price": "open", "max_hold_price": "close"},
        },
    },
    {
        "name": "MACD与KDJ共振",
        "note": (
            "【买卖规则样例 · 双指标同时转折】MACD 与 KDJ **同一天**都出现「昨降今升」的向上"
            "转折就买入（次日开盘价成交）；两者**同一天**都出现「昨升今降」的向下转折就卖出"
            "（开盘价成交）；最长持有 20 根 K 线，并带 -8% 止损 / +18% 止盈。\n\n"
            "**「转折」的准确含义**（判据见 `expr.turn_up` / `expr.turn_down`，全站唯一）：\n"
            "· 向上转折 = 今天 > 昨天，且 昨天 <= 前天（前面还在下行或走平，今天翻上来）\n"
            "· 向下转折 = 今天 < 昨天，且 昨天 >= 前天\n"
            "⚠ 不是「金叉」：金叉看的是两条线谁在上谁在下，这里看的是**同一条线自己拐头**。\n"
            "⚠ 也不是「低位阈值」：不限定 DIF 在零轴下、K 在 20 以下 —— 要「只在低位转折才买」"
            "请另配一个选股策略（例如「低位（MACD/KDJ共振）」）。\n\n"
            "**共振**指两个指标在同一天同时转折，缺一不可 —— 单指标转折太频繁、噪音大。\n"
            "买什么由你在「选股策略」里选；这条只描述买卖时点。"
        ),
        "config": {
            "timeframe": "daily",
            # 共振买入：MACD 与 KDJ 同一天都向上转折。⚠ 用 turn_up() 而不是手写
            # `x > ref(x,1) and ref(x,1) <= ref(x,2)` —— 判据只留一处，改口径不用改策略串。
            "buy_trigger": [
                "turn_up(macd_dif) and turn_up(macd_dea)",
                "turn_up(kdj_k) and turn_up(kdj_d)",
            ],
            # 共振卖出：两个指标同一天都向下转折（与买入完全对称）
            "sell_signal": [
                "turn_down(macd_dif) and turn_down(macd_dea)",
                "turn_down(kdj_k) and turn_down(kdj_d)",
            ],
            "risk": {"max_hold_days": 20, "stop_loss_pct": -8, "take_profit_pct": 18},
            "exec": {"buy_price": "open", "sell_price": "open", "max_hold_price": "close"},
        },
    },
]


def install_builtins(force: bool = False) -> int:
    """写入内置买卖策略（已存在同名则跳过，force=True 时覆盖）。"""
    cnt = 0
    for b in BUILTIN_STRATEGIES:
        cfg = merge_config(b["config"])
        r = db.query_one("SELECT id FROM strategy WHERE name=%s", (b["name"],))
        if r:
            if force:
                db.execute(
                    "UPDATE strategy SET note=%s, timeframe=%s, config=%s WHERE id=%s",
                    (b["note"], cfg["timeframe"], config_json(cfg), r["id"]),
                )
                cnt += 1
            continue
        db.execute(
            "INSERT INTO strategy (name, note, timeframe, config, builtin) VALUES (%s,%s,%s,%s,1)",
            (b["name"], b["note"], cfg["timeframe"], config_json(cfg)),
        )
        cnt += 1
    return cnt


def ensure_builtins() -> None:
    try:
        install_builtins()
    except Exception:  # noqa: BLE001  内置样例只是开箱内容，失败不该拖垮启动
        pass
