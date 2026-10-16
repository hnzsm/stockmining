# -*- coding: utf-8 -*-
"""条件股票池（Rule-based Universe）。

把「股票池」从一份写死的代码列表，扩展成

    基础池（静态范围：全部A股 / 指数成分 / 行业）
        ×
    每日条件（动态：逐日在 K 线上求值，全局成立才算池内）

的组合。

## 为什么需要它

`entry`（选股条件）其实也能表达「换手率 > 5」，但它有两个做不到的地方：

1. **加载前裁剪**：条件池可以先用一条 SQL 把「区间内从没可能入池」的股票剔掉，
   再进入指标计算。全市场 5000 只算 BOLL/MACD 很贵，先裁到 2000 只是实打实的提速。
2. **口径分离**：池子是「范围」，信号是「时点」。分开之后，同一个池子可以套不同策略，
   选股结果里也能明确区分「不符合池子」和「不符合信号」。

⚠ 2026-10-10：原本这里还有第 3 条「作用到持仓」= 出池即卖（`pool_exit`），
**已彻底移除** —— 选股策略只管选股，卖出归买卖策略，池子不再承担卖出语义。

## 数据模型

    {"base": "all", "where": ["total_mv < 500000", "turnover_rate > 5"], "filt": {...}}

* `base`  基础池，取值同 `market.resolve_universe`：all / 000300.SH / industry:银行
* `where` 条件列表，逐日全部成立才算池内；空列表 = 不筛（退化为纯静态池）
* `filt`  静态过滤项（剔ST/板块/上市天数/最小成交额），默认见 `DEFAULT_FILT`

## 引用已保存的选股策略

除了就地写条件，也可以引用「选股策略管理」里保存好的一条：

    {"pool_id": 3}      # 或字符串 "pool:3"

展开时按 id 读出 base / conds / filt / timeframe。回测、选股入口会先 `expand()` 一次，
后续逐日求值不再查库；直接把 `pool_id` 传给 parse 也能工作（带 3 秒短缓存）。

## 求值时机

`where` 与 `entry` 完全同一时机：在信号日 T 的 K 线上求值（`ref()` 向前看历史）。
所以「昨日大阴」写成 `ref(pct_chg, 1) <= -5` —— 站在 T 日回看 T-1 日。
"""
from __future__ import annotations

import datetime as dt
import re

from core import db, expr

UNIVERSE_ALL = "all"

# 周期枚举 —— **全站唯一来源**（选股策略 `pool_strategy.timeframe`、买卖策略 `cfg["timeframe"]`、
# 提醒的日K/周K 都从这里取，别在别处再写一套）。放在 universe 是因为它是模型层最底：
# `pool` 和 `strategy` 都要用，而 strategy 顶部就 import 了 universe（放 strategy 会循环导入）。
#   · `daily`  —— 在日线面板上求值（老行为）；
#   · `weekly` —— 在**周线**面板上求值：`close` 就是周收盘、`ma5` 就是 5 周均线，
#     每一根都是一个**已收盘的完整周**（`market.compute_weekly_panel`）。
# ⚠ 选股策略标成 weekly 时**整条链**跟着走 —— 判据只有一处：`strategy.selection_cfg()`
#   （池子是 weekly ⇒ 整个 cfg 走 `to_weekly_view`）。也就是「要么全周线、要么全日线」，
#   不会出现一半日线一半周线。
FREQS: dict[str, str] = {"daily": "日K", "weekly": "周K"}
FREQ_ORDER = ["daily", "weekly"]
DEFAULT_FREQ = "daily"


def norm_freq(v) -> str:
    """把任意输入归一化成 `daily` / `weekly`。不认的一律 daily（与老行为一致）。"""
    return "weekly" if str(v or "").strip().lower() == "weekly" else DEFAULT_FREQ


# 名字后缀标记：**周K 才标、日K 不标**（用户要求「策略名称后面如果是周k则标记周」）。
# 唯一实现 —— 选股策略列表、股票池下拉、回测记录的池子名都从这里取。
FREQ_NAME_TAG: dict[str, str] = {"daily": "", "weekly": "（周K）"}


def name_tag(freq) -> str:
    return FREQ_NAME_TAG[norm_freq(freq)]

# 股票池的「静态属性过滤」默认值 —— 剔 ST / 北交所、上市满 60 天。
# ⚠ 这里是这一组默认值的**唯一来源**：strategy.DEFAULT_POOL 直接引用它，
# 别再在别处抄一份（改一处生效，改两处早晚跑偏）。
# 这些过滤项属于「选股逻辑」，所以 2026-09-18 起挂在**选股策略**上（pool_strategy.filt），
# 不再由买卖策略携带 —— 买卖策略只管什么时候买、什么时候卖。
DEFAULT_FILT: dict = {
    "exclude_st": True,        # 剔除 ST / *ST
    "exclude_kcb": True,       # 剔除科创板 688（2026-10-11 起默认勾选；已落库的策略不受影响）
    "exclude_cyb": False,      # 剔除创业板 300/301
    "exclude_bj": True,        # 剔除北交所
    "min_amount": 0,           # 最小日成交额（千元），逐日生效
    "min_list_days": 60,       # 最小上市天数
}

# 前端表单要用的字段清单（改这里就够，页面按它铺，别写死）
FILT_KEYS = tuple(DEFAULT_FILT)


def norm_filt(filt) -> dict:
    """把外来的 filt 归一化成完整字典（缺的补默认、多的丢掉、类型纠正）。"""
    out = dict(DEFAULT_FILT)
    if not isinstance(filt, dict):
        return out
    for k in DEFAULT_FILT:
        if k not in filt or filt[k] is None:
            continue
        if k.startswith("exclude_"):
            out[k] = bool(filt[k])
        else:
            try:
                out[k] = max(0, int(float(filt[k])))
            except (TypeError, ValueError):
                pass
    return out


# ---------------------------------------------------------------------------
# 预设池（两个用途）
#   1. 前端「一键套用预设」下拉（`GET /api/universe/presets`）—— 主要用途；
#   2. `pool.install_builtins()` 会把它逐条落库，但落成**普通**策略（builtin=0 ⇒ 可改名、
#      可删除），**不再**自动带内置标记。
#      ⚠ 2026-09-29 起「内置」与「预设」解耦：不可删的内置样例只在 `pool.BUILTIN_POOLS`
#      里明写（当前 1 条「首板」），本清单只管内容、不管「能不能删」。
# ---------------------------------------------------------------------------

PRESETS: list[dict] = [
    {
        "id": "small_cap",
        "name": "小市值",
        "note": "总市值 < 50 亿（小盘/微盘股）。total_mv 单位为万元，500000 万元 = 50 亿。"
                "想更极致可改 300000（30 亿），或换成 circ_mv 用流通市值。",
        "base": "all",
        "where": ["total_mv < 500000"],
    },
    {
        "id": "high_turnover",
        "name": "高换手",
        "note": "换手率 > 5%（成交活跃）。需要已同步 daily_basic，否则该字段为空、条件恒不成立。",
        "base": "all",
        "where": ["turnover_rate > 5"],
    },
    {
        "id": "boll_upper",
        "name": "BOLL 上轨",
        "note": "收盘价站上布林带上轨（强势突破）。"
                "想表达「贴近上轨」用 boll_pos > 0.9；「位于中轨之上」用 close > boll_mid。",
        "base": "all",
        "where": ["close >= boll_upper"],
    },
    {
        "id": "big_bear",
        "name": "昨日大阴",
        "note": "昨日跌幅 >= 5%（站在 T 日回看 T-1 日）。"
                "想更严格可加一条 ref(is_down, 1) = 1（昨收低于昨开），或用 ref(body, 1) <= -5 看实体幅度。",
        "base": "all",
        "where": ["ref(pct_chg, 1) <= -5"],
    },
    {
        "id": "small_cap_active",
        "name": "小市值 + 活跃",
        "note": "总市值 < 100 亿 且 换手率 > 3%：小盘里有资金关注的。池子条件可以多条叠加，全部成立才算入池。",
        "base": "all",
        "where": ["total_mv < 1000000", "turnover_rate > 3"],
    },
    {
        "id": "main_inflow",
        "name": "主力净流入",
        "note": "当日主力净额 > 0（大单+特大单净买入）。需要已同步资金流向（moneyflow），"
                "没同步时该字段为空、条件恒不成立；北交所部分标的没有资金流向数据，也进不了池子。",
        "base": "all",
        "where": ["mf_main_net > 0"],
    },
    {
        "id": "main_inflow_strong",
        "name": "主力强流入 + 放量",
        "note": "主力净额占成交额 > 5% 且 成交额 > 10 亿（amount 单位千元，1000000 = 10 亿元），"
                "并叠加量能比 vol_ratio5 > 1.2：资金推动 + 量能配合的强势股。",
        "base": "all",
        "where": ["mf_main_ratio > 5", "amount > 1000000", "vol_ratio5 > 1.2"],
    },
    # ---- 下面两条是「选股逻辑」的样例：以前它们写在买卖策略的「选股条件」里，
    #      2026-09-18 起选股逻辑只归选股策略，所以搬过来做开箱样例。
    #      配套的买卖策略样例是「开盘确认」（见 strategy.BUILTIN_STRATEGIES；2026-09-29 起
    #      内置只保留这一条，「跌破MA10离场」已取消内置标记、记录仍在库里可直接引用）。
    {
        "id": "boll_lower_dip",
        "name": "BOLL下轨低吸",
        "note": "日K收盘价贴近布林带下轨（收盘 <= 下轨 × 1.02）：超跌到统计意义上的下沿。"
                "回测时配「开盘确认」这套买卖规则，就是经典的「下轨低吸 + 次日高开确认」。",
        "base": "all",
        "where": ["close <= boll_lower * 1.02"],
    },
    {
        "id": "ma_cross_volume",
        "name": "均线金叉 + 放量",
        "note": "MA5 上穿 MA20（金叉）且成交量大于 5 日均量的 1.5 倍：趋势刚转 + 有量配合。"
                "配「跌破MA10离场」这套买卖规则，就是「金叉进、破线出」。",
        "base": "all",
        "where": ["cross_above(ma5, ma20)", "vol > vma5 * 1.5"],
    },
]

_PRESET_BY_ID = {p["id"]: p for p in PRESETS}


def get_preset(pid: str) -> dict | None:
    p = _PRESET_BY_ID.get(pid)
    return dict(p) if p else None


# ---------------------------------------------------------------------------
# 解析 / 校验
# ---------------------------------------------------------------------------

_POOL_PREFIX = "pool:"
_POOL_CACHE: dict[int, tuple[float, dict | None]] = {}
_POOL_TTL = 3.0          # 秒。只用于「没走 expand 的零散调用」，避免逐股查库


def invalidate_pool(pid: int | None = None) -> None:
    """让选股策略缓存失效。保存/删除后必须调用，否则会读到改之前的条件。"""
    if pid is None:
        _POOL_CACHE.clear()
    else:
        _POOL_CACHE.pop(int(pid), None)


def _load_pool(pid: int) -> dict | None:
    """按 id 读选股策略（带短缓存）。延迟导入以避免 core.pool <-> core.universe 循环。"""
    import time
    from core import pool as pool_mod
    hit = _POOL_CACHE.get(pid)
    now = time.time()
    if hit and now - hit[0] < _POOL_TTL:
        return hit[1]
    row = pool_mod.get_pool(pid)
    _POOL_CACHE[pid] = (now, row)
    return row


def pool_ref(spec):
    """从 spec 里取出引用的选股策略 id，取不到返回 None。

    支持 {"pool_id": 3} / {"pool": 3} / "pool:3"。
    """
    if isinstance(spec, dict):
        for k in ("pool_id", "pool"):
            v = spec.get(k)
            if v not in (None, ""):
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return None
        return None
    if isinstance(spec, str) and spec.strip().lower().startswith(_POOL_PREFIX):
        tail = spec.strip()[len(_POOL_PREFIX):].strip()
        try:
            return int(tail)
        except ValueError:
            return None
    return None


def is_cond(spec) -> bool:
    """是否「条件池」描述（而非静态代码列表）。"""
    if pool_ref(spec) is not None:
        return True
    return isinstance(spec, dict) and ("where" in spec or "base" in spec)


def expand(spec):
    """把「按 id 引用选股策略」展开成完整的条件池描述。

    在回测/选股入口调用一次，之后逐日求值不再查库。
    引用不到（记录被删）时退化成该策略原本的基础池，并在 label 里标注。
    """
    pid = pool_ref(spec)
    if pid is None:
        return spec
    row = _load_pool(pid)
    if not row:
        return {"base": UNIVERSE_ALL, "where": [],
                "filt": dict(DEFAULT_FILT), "pool_id": pid, "pool_name": None,
                "timeframe": DEFAULT_FREQ}
    inline = {}
    if isinstance(spec, dict):
        inline = {k: v for k, v in spec.items() if k not in ("pool_id", "pool")}
    out = {
        "base": row.get("base") or UNIVERSE_ALL,
        "where": list(row.get("conds") or []),
        # 静态过滤项跟着选股策略走：引用 `pool:3` 就等于连它的剔ST/板块/上市天数一起带上
        "filt": norm_filt(row.get("filt")),
        "pool_id": pid,
        "pool_name": row.get("name") or "",
        # ⚠ 周期也**跟着选股策略走**：引用一条周K策略 ⇒ 整条链按周线跑
        #   （由 `strategy.selection_cfg()` 一处决定，见那里注释）。
        "timeframe": norm_freq(row.get("timeframe")),
    }
    # 调用方显式覆盖的字段优先（一般不用，留个口子）
    for k in ("base", "where"):
        if inline.get(k) not in (None, "", []):
            out[k] = inline[k]
    if isinstance(inline.get("filt"), dict):
        out["filt"] = norm_filt(inline["filt"])
    return out


def parse(spec) -> dict:
    """归一化成 {"kind","base","where","filt","pool_id","pool_name","timeframe"}。

    `timeframe` = 这条条件池跑在哪个周期上（引用选股策略时 = 它自己的 `timeframe`；
    就地下写的临时条件池 = daily）。`strategy.selection_cfg()` 靠它决定整条链的周期。
    """
    if not is_cond(spec):
        return {"kind": "static", "base": spec, "where": [],
                "filt": dict(DEFAULT_FILT), "pool_id": None, "pool_name": None,
                "timeframe": DEFAULT_FREQ}

    pid = pool_ref(spec)
    if pid is not None:
        spec = expand(spec)
    base = spec.get("base")
    if base in (None, ""):
        base = UNIVERSE_ALL
    # `#` 起注释（行内或整行）—— 注释不是条件，在这一层统一剥掉、纯注释行丢弃。
    # ⚠ 存库与回显仍保留原文（`pool_strategy.conds` 原样存），剥只在「要执行」这里，
    #   所以下面所有拿 where 求值/翻译预筛/取字段的地方都已经是纯表达式。
    where = expr.split_conds(spec.get("where") or [])
    return {
        "kind": "cond",
        "base": base,
        "where": where,
        # 就地写的条件池没带 filt 时用默认值（页面上的临时条件池走这条）
        "filt": norm_filt(spec.get("filt")),
        "pool_id": pid,
        "pool_name": spec.get("pool_name") if isinstance(spec, dict) else None,
        "timeframe": norm_freq(spec.get("timeframe")),
    }


def validate(spec, known_names: set[str] | None = None) -> list[str]:
    """返回错误列表，空表示通过。"""
    # 引用了不存在的选股策略：明确报错，不要静默退化成全市场 ——
    # 那会让「以为在跑小市值池」的回测悄悄跑在全市场上，结果完全不可信。
    pid = pool_ref(spec)
    if pid is not None and not _load_pool(pid):
        return [f"引用的选股策略 #{pid} 已不存在（可能已被删除），请重新选择股票池"]
    errs: list[str] = []
    s = parse(spec)
    if s["kind"] == "static":
        return errs
    if not s["where"]:
        errs.append("条件池至少要写一条条件，否则请直接用静态股票池")
    for i, e in enumerate(s["where"]):
        ok, msg = expr.check_expr(e)
        if not ok:
            errs.append(f"[池子条件 第{i+1}条] {msg}")
            continue
        if known_names:
            names = expr.extract_identifiers(e)
            unknown = {n for n in names if n not in known_names}
            if unknown:
                errs.append(f"[池子条件 第{i+1}条] 未知字段: {', '.join(sorted(unknown))}")
    return errs


def base_label(base) -> str:
    from core import market
    return market.universe_label(base)


def match_preset(spec) -> dict | None:
    s = parse(spec)
    if s["kind"] != "cond":
        return None
    for p in PRESETS:
        if p["base"] == s["base"] and p["where"] == s["where"]:
            return p
    return None


def label(spec) -> str:
    """人类可读的池子名称（存回测记录、页面标题用）。"""
    s = parse(spec)
    if s["kind"] == "static":
        return base_label(spec)
    bl = base_label(s["base"])
    # 引用的选股策略：直接用它的名字，别显示成裸表达式
    # ⚠ 周K 策略在名字后带「（周K）」—— 否则同一套条件的日线版/周线版在记录里长得一样，
    #   回头翻回测记录根本分不清那次跑的是哪个（后缀唯一来源 `name_tag`）。
    if s["pool_id"]:
        nm = s["pool_name"] or f"选股策略#{s['pool_id']}（已删除）"
        return f"{bl} · {nm}{name_tag(s.get('timeframe'))}"
    if not s["where"]:
        return bl
    p = match_preset(s)
    core = p["name"] if p else f"条件池({len(s['where'])}条)"
    # 非预设时，用第一条条件做提示，避免只看到「条件池(2条)」不知道是啥
    if not p:
        core += f" · {s['where'][0]}"
    return f"{bl} · {core}"


def describe(spec) -> dict:
    """给前端的结构化描述。"""
    s = parse(spec)
    out = {
        "kind": s["kind"],
        "base": s["base"],
        "where": s["where"],
        "filt": s["filt"],
        "pool_id": s["pool_id"],
        "pool_name": s["pool_name"],
        # 周期：前端要据此在名字后标「周K」（日K 不标）—— 判据就这一处
        "timeframe": s["timeframe"],
        "label": label(spec),
    }
    p = match_preset(spec)
    out["preset_id"] = p["id"] if p else None
    return out


# ---------------------------------------------------------------------------
# 加载前裁剪（SQL 精确求并集）
# ---------------------------------------------------------------------------

# 允许走预筛的字段：必须是「不随复权变化」且**直接存在**于 daily_kline / daily_basic。
# 价格类字段（open/high/low/close/pre_close）故意排除 —— 面板里是前复权价，直接和 SQL 里的
# 原始价比较会得到错误结果（qfq 以**最新一根**为基准、每天变），**宁可跳过这条条件**。
PRESELECT_KLINE = {"amount", "vol", "pct_chg"}
PRESELECT_BASIC = {"total_mv", "circ_mv", "turnover_rate", "volume_ratio", "pe", "pb", "ps"}

# 「形态派生字段」在 SQL 里的表达（**未复权**列），`{a}` 是别名（k / k1 / k2 …）。
#
# 为什么可以拿未复权表算：这些都是**同一根 K 线内**的比例或比较，而 `market.apply_adjust`
# 给 open/high/low/close/pre_close 乘的是**同一个** ratio → 比值不变、比较方向不变，
# 所以未复权表上算与面板（前复权）上算**逐值一致**。
#
# ⚠ `ret1` 故意不做：面板里它是 `close / close.shift(1) - 1`（按**该股上一根 K 线**），
#    不是 `pre_close` —— 停牌缺口下两者不同，硬翻会算错。
# ⚠ 除零与 pandas 对齐：面板里 `pc = pre_close.replace(0, NaN)`、`close_pos` 用
#    `(high-low).replace(0, NaN)` → 得到 NaN → 比较为 False（该票出局）；SQL 里除 0 得
#    NULL → 比较得 NULL → 同样出局。两边行为一致，不需要额外 `pre_close > 0` 前缀。
_SHAPE_SQL = {
    "amplitude":    "({a}.high - {a}.low) / {a}.pre_close * 100",
    "body":         "({a}.close - {a}.open) / {a}.pre_close * 100",
    "upper_shadow": "({a}.high - GREATEST({a}.close, {a}.open)) / {a}.pre_close * 100",
    "lower_shadow": "(LEAST({a}.close, {a}.open) - {a}.low) / {a}.pre_close * 100",
    "gap_open":     "({a}.open - {a}.pre_close) / {a}.pre_close * 100",
    "close_pos":    "({a}.close - {a}.low) / NULLIF({a}.high - {a}.low, 0)",
    "is_up":        "({a}.close > {a}.open)",
    "is_down":      "({a}.close < {a}.open)",
}
PRESELECT_SHAPE = set(_SHAPE_SQL)

# `ref(字段, N)` 最多往前挪几层。再深就别翻了（LEFT JOIN 链会拖慢 SQL，收益也小）。
PRESELECT_MAX_REF = 5

_OP_SQL = {"<=": "<=", ">=": ">=", "!=": "<>", "<>": "<>", "==": "=", "=": "=",
           "<": "<", ">": ">"}
# 写成「数字 op ref(...)」时要把算子翻过来（`1 = ref(x,2)` → `ref(x,2) = 1`）
_OP_FLIP = {"<=": ">=", ">=": "<=", "<": ">", ">": "<", "=": "=", "==": "=",
            "!=": "<>", "<>": "<>"}

_NUM = r"-?\d+(?:\.\d+)?"
_FIELD = r"[A-Za-z_][A-Za-z0-9_]*"
_REF = rf"ref\s*\(\s*(?P<rf>{_FIELD})\s*,\s*(?P<rn>\d+)\s*\)"
_LHS = rf"(?:{_FIELD}|{_REF})"
_TERM_LR = re.compile(rf"^\s*(?P<lhs>{_LHS})\s*(?P<op><=|>=|!=|<>|==|=|<|>)\s*(?P<num>{_NUM})\s*$")
_TERM_RL = re.compile(rf"^\s*(?P<num>{_NUM})\s*(?P<op><=|>=|!=|<>|==|=|<|>)\s*(?P<lhs>{_LHS})\s*$")


def _cond_term(e: str):
    """把**一条**条件解析成 (别名层, 字段名, sql 算子, 数字)。

    别名层 0 = 当天（k）· 1 = 该股上一根 K 线（k1）· 2 = 上两根（k2）…

    任何一种「看不准」的情况都返回 None —— 由调用方**跳过这条条件**（不是否决整份预筛）。
    """
    m = _TERM_LR.match(e)
    flip = False
    if m is None:
        m = _TERM_RL.match(e)
        if m is None:
            return None
        flip = True
    lhs, op, num = m.group("lhs"), m.group("op"), m.group("num")

    mr = re.fullmatch(_REF, lhs)
    if mr:
        level = int(mr.group("rn"))
        # `ref(x,0)` = 当天本身；再深的链就没意义了
        if level > PRESELECT_MAX_REF:
            return None
        field = mr.group("rf")
    else:
        level, field = 0, lhs

    if field not in PRESELECT_KLINE and field not in PRESELECT_BASIC \
            and field not in PRESELECT_SHAPE:
        return None           # 指标类 / 价格类 / 筹码类 —— 翻不了，跳过
    return level, field, (_OP_FLIP[op] if flip else _OP_SQL[op]), num


def _term_sql(level: int, field: str, op: str, num: str, guard: bool = False) -> str:
    """把 (层, 字段, 算子, 数字) 渲染成 SQL 片段。字段合法性由 `_cond_term` 保证。

    guard: 历史层（level ≥ 1）必须带上 —— 那只票那一层 K 线**缺行**时（停牌、或落在
    窗口外），条件求值得到 NULL，SQL 会把它当假 → 把票**剔掉**。而面板里的 `ref(x,N)`
    是 shift()，跨过缺口取更早那根，未必为假。为了**绝不漏命中**，缺行时一律**保留**。
    """
    a = "k" if level == 0 else f"k{level}"
    b = "b" if level == 0 else f"b{level}"
    if field in _SHAPE_SQL:
        expr_ = _SHAPE_SQL[field].format(a=a)
    elif field in PRESELECT_BASIC:
        expr_ = f"{b}.{field}"
    else:
        expr_ = f"{a}.{field}"
    sql = f"{expr_} {op} {num}"
    return f"({sql} OR {a}.ts_code IS NULL)" if guard else sql


def _preselect_terms(where: list[str]):
    """把池子条件**逐条**翻译成可用的 SQL 片段。

    **与旧实现的根本区别**：翻不了的条件**直接跳过**，而不是一条翻不了就整份放弃。

    安全性：预筛只用「条件的一个子集」—— 少一条 AND 只会让结果**变大**（超集），
    绝不会漏掉真正的命中 → 与逐票求值的结果**精确等价**（代价只是可能少筛掉一些票）。
    这条性质是本函数允许"跳过"的全部依据，改这里之前先想清楚。

    返回 (terms, skipped)，两者都是 list；terms 为空表示一条都翻不了（调用方退回全量）。
    """
    terms, skipped = [], []
    for e in where:
        t = _cond_term(e)
        if t is None:
            skipped.append(e)
        else:
            terms.append(t)
    return terms, skipped


def preselectable(spec) -> bool:
    """这条池子**是否真的会走 SQL 预筛**（有一条可翻译的条件就算）。

    ⚠ 判据是 `_preselect_terms()` 有没有产出，**不是** `preselect_note() == ""` ——
    2026-09-19 起预筛改成「逐条翻译、翻不了就跳过」，说明也就改成**逐条**报的：
    「3 条能翻 + 1 条翻不了」照样会给出文案，但它**确实**走预筛（只是少筛一点点）。
    拿说明文案当判据，会让这类池子在界面上被误标成「含指标，全量加载」。
    """
    s = parse(spec)
    if s["kind"] != "cond" or not s["where"]:
        return False
    terms, _skipped = _preselect_terms(s["where"])
    return bool(terms)


def preselect_codes(spec, start: str, end: str) -> list[str] | None:
    """用 SQL 精确求出「区间内任意一天满足全部池子条件」的股票集合。

    **结果精确相等，不是近似**。

    ⚠ 这里的"精确"来自「只收紧、不放松」：预筛用的是**条件的一个子集**
    （翻不了的条件被 `_preselect_terms` 跳过），少一条 AND 只会让候选集变成**超集**，
    真正的命中一个都不会掉。所以**跳过条件是安全的，整份放弃才是浪费**。

    `ref(字段, N)` 通过「该股自己的上一根 K 线」的自连接实现 —— 一次
    `ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_date DESC)` 把窗口内每只票
    的倒数第 N 根排好再等值 join，**不是**按自然日减 N（停牌缺口下按自然日会错），
    也不是逐行相关子查询（在 MySQL 8 上实测 ~2ms/行、全市场 11s，白搭）。

    返回 None 表示一条都翻不了（调用方退回全量加载）；返回空列表表示**真的**一只都没有。
    """
    s = parse(spec)
    if s["kind"] != "cond" or not s["where"]:
        return None
    terms, _skipped = _preselect_terms(s["where"])
    # 区间语义下「往前的第 N 根 K 线」是**逐行变化**的。要么用相关子查询（实测 2ms/行，
    # 全市场 11s，白搭），要么按自然日减 N（**错**：停牌缺口下不是同一根）。所以只在
    # 「单日」场景翻 ref（试跑 / 今日选股都是单日），区间场景把 ref 条件丢掉 ——
    # 丢掉只会让候选集变大（超集），不会漏命中。
    if str(start)[:10] != str(end)[:10]:
        terms = [t for t in terms if t[0] == 0]
    if not terms:
        return None

    # 区间内一条 K 线都没有时（如传了非交易日），返回 None 而不是空列表 ——
    # 别把「这天没数据」误判成「这天池子里一只都没有」。
    if not db.scalar("SELECT COUNT(*) FROM daily_kline WHERE trade_date BETWEEN %s AND %s LIMIT 1",
                     (start, end)):
        return None

    max_level = max(lv for lv, _, _, _ in terms)
    joins: list[str] = []
    ctes: list[str] = []
    args: list = []

    # 第 0 层（当天）的 daily_basic 用 INNER JOIN —— 与面板口径一致：那几列缺数据时
    # 面板里是 NaN、条件恒 False（该票出局），INNER JOIN 的剔除方向相同。
    basic_levels = {lv for lv, f, _, _ in terms if f in PRESELECT_BASIC}
    if 0 in basic_levels:
        joins.append("JOIN daily_basic b ON b.ts_code=k.ts_code "
                     "AND b.trade_date=k.trade_date")

    # 历史层：一次 ROW_NUMBER 把「窗口内每只票的倒数第 N 根」排好，再等值 join。
    # 比逐行相关子查询快几十倍（后者在 MySQL 8 上实测 ~2ms/行）。
    # ⚠ 用「该股自己的上一根 K 线」而不是「自然日的前一天」：停牌股在自然日上取不到行，
    #    但面板里的 `ref(x,N)` 是 shift(N)，跨过缺口取的是更早那根 —— 两者必须一致。
    if max_level > 0:
        w0 = _prev_trade_date(start, max_level + 2)
        ctes.append(
            "pr AS (SELECT ts_code, trade_date, "
            "ROW_NUMBER() OVER (PARTITION BY ts_code ORDER BY trade_date DESC) AS back "
            "FROM daily_kline WHERE trade_date < %s AND trade_date >= %s)")
        args += [str(start)[:10], w0]
        for lv in range(1, max_level + 1):
            joins.append(f"LEFT JOIN pr pr{lv} ON pr{lv}.ts_code = k.ts_code "
                         f"AND pr{lv}.back = {lv}")
            joins.append(f"LEFT JOIN daily_kline k{lv} ON k{lv}.ts_code = pr{lv}.ts_code "
                         f"AND k{lv}.trade_date = pr{lv}.trade_date")
            if lv in basic_levels:
                joins.append(f"LEFT JOIN daily_basic b{lv} ON b{lv}.ts_code = k{lv}.ts_code "
                             f"AND b{lv}.trade_date = k{lv}.trade_date")

    sql_where = [_term_sql(lv, f, op, num, guard=lv > 0) for lv, f, op, num in terms]
    sql = ("WITH " + ", ".join(ctes) + " " if ctes else "") + \
          "SELECT k.ts_code FROM daily_kline k " + " ".join(joins) + \
          " WHERE k.trade_date BETWEEN %s AND %s AND " + " AND ".join(sql_where) + \
          " GROUP BY k.ts_code"
    args += [start, end]
    try:
        rows = db.query(sql, args)
    except Exception:  # noqa: BLE001  预筛只是优化，失败就退回全量
        return None
    return [r["ts_code"] for r in rows]


def _prev_trade_date(d, back: int) -> str:
    """往前数 `back` 个**交易日**的日期，只用来定窗口起点（取不到就退回自然日估算）。

    只是窗口边界，宁大勿小 —— 窗口偏小不会算错（落不进窗口的股票会被保守保留），
    但会让预筛少筛掉一些票。
    """
    try:
        from core import market  # noqa: PLC0415  延迟导入：market 反向依赖本模块
        ds = market.get_trade_dates(None, str(d)[:10])
        if len(ds) >= back:
            return ds[-back]
    except Exception:  # noqa: BLE001  日历取不到就退回自然日估算
        pass
    cal = dt.date.fromisoformat(str(d)[:10]) - dt.timedelta(days=int(back * 7 / 5) + 10)
    return cal.isoformat()


def preselect_note(spec) -> str:
    """预筛能力说明（写进回测/试跑日志，让用户知道为什么快 / 为什么没变快）。"""
    s = parse(spec)
    if s["kind"] != "cond" or not s["where"]:
        return ""
    terms, skipped = _preselect_terms(s["where"])
    if not skipped:
        return ""
    if not terms:
        # 一条都翻不了 —— 说清到底是哪一类字段挡住的，别用一句话搪塞。
        from core import chips as _ch  # noqa: PLC0415  延迟导入：chips 只用得到常量

        names: set[str] = set()
        for e in s["where"]:
            try:
                names |= expr.extract_identifiers(e)
            except Exception:  # noqa: BLE001  说明文案而已，解析不了就退回通用说法
                pass
        if names & set(_ch.CHIPS_DERIVED):
            return ("池子条件含筹码/两融字段（股东户数 / 融资融券 / 增减持），这些不在 daily_kline 里，"
                    "无法用 SQL 预筛，本次按全量加载（结果不受影响，只是慢些）")
        return ("池子条件含指标函数或复权价字段，无法用 SQL 预筛，"
                "本次按全量加载（结果不受影响，只是慢些）")
    return (f"池子 {len(s['where'])} 条条件里 {len(terms)} 条已用于 SQL 预筛、"
            f"{len(skipped)} 条（指标/复权价/筹码字段）跳过 —— 跳过只会少筛一些票，"
            f"不会漏掉命中")
