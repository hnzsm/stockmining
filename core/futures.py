# -*- coding: utf-8 -*-
r"""今日期货：**本地库**（tushare `fut_basic` / `fut_daily` / `opt_basic`
→ `futures_basic` / `futures_daily` / `futures_option`）。

⚠ 2026-09-27 换源：这一层**曾经**走东财站外接口（`push2.eastmoney.com`），
2026-09-25 起该域名**服务端持续断连**（curl 复现 `(56) server closed abruptly`，
而同家的 `push2his` 正常），六个市场全部取不到 —— 页面整页空。
换成本地库后与项目其它数据同一条链路（tushare → MySQL → 页面），**打开页面不出网**。

代价：tushare 期货日线**收盘结算后**才更新（⚠ 不是「当天拿不到」—— 2026-09-28 收盘后就拿到了
当天 1075 行；当初误判是因为 09-25 那天**中秋休市**、取回 0 行，`trade_cal` 里 `is_open=0`）。
所以盘中打开时页面显示的是「**截至上一交易日**」的收盘行情，收盘后同步一次就能拿到当天 ——
`as_of` / `trade_date` 里写明到底画到哪一天。

口径（唯一来源都在本文件，别在页面/同步层各写一套）：

| 概念 | 判据 |
|---|---|
| 主连 | `futures_basic.is_main = 1`（写入时由 `core/sync.fut_is_main` 算：交易所内代码 == 品种码；中金所 30 年国债是特例 `TL0`） |
| 同品种 | 同一 `(exchange, fut_code)`、**未退市**、且 `symbol` 带月份数字（"连续"序列不可交易，不算合约）；组内排序 = **到期日近→远**（`delist_date` 升序，缺到期日的垫底，同一天到期再看持仓量降序）—— 唯一来源 `_sort_key_delist` |
| 涨跌幅 | `change1 / pre_settle * 100`（**收盘价 vs 昨结算**，期货的规矩）；缺昨结算才退回 `(close-pre_close)/pre_close` |
| 品种名 | 主连 name 去掉结尾的「主力 / 期货」 |
| **到期日** | `futures_basic.delist_date` —— **该期货合约自己的最后交易日**（tushare `fut_basic`）。主连是连续序列，恒为空 |
| **期权到期日** | `futures_option` 里按**标的期货 ts_code** 对上（`opt_code` 去掉 `OP` 前缀就是它，唯一来源 `sync.opt_underlying`）。同一合约的期权到期日通常唯一，连续月份期权会有 2 个 ⇒ `opt_maturity` 取**最早**、全量留在 `opt_maturities`。股指期权（IO/MO/HO）标的是**指数**，对不上 ⇒ 显示「—」（正确，现金交割） |
| **最近期权到期**（主连行） | 期权是**逐合约**挂的、主连自己（`RU.SHF`）不是可交易合约 ⇒ 没有对应期权。所以主连行上的这个字段是**品种级聚合**：取该品种**全部可交易合约**（就是展开行里那些）里、对应期权到期日**最早且未过期**（`>= today`）的那个 ⇒ `opt_near_date` / `opt_near_days`（距**今天**的**自然日**数）/ `opt_near_code`（带出这个到期日的是哪个合约）。没有就全是 `None`（页面显示「—」、排序垫底） |
| **最近合约交割**（主连行） | 同理必须**品种级聚合**（主连是连续序列、自己没有到期日）：取该品种全部可交易合约里**最后交易日最早**的那个 ⇒ `fut_near_date` / `fut_near_days`（距**今天**的自然日，**可为负** = 刚交割完）/ `fut_near_code`。`mates` 已按到期日近→远排好 ⇒ 它就是**展开行第一行**，主连格与展开行永远指同一个合约（探针据此交叉验）。⚠ 与 `opt_near_*` 是两个概念：一个指**期货合约交割日**、一个指**期权到期日**，别混 |

对外：`get_all()`（列表）、`kline()`（K线）、`option_map()`（期权到期日映射）、`clear_cache()`。
`get_all()` 形状与旧版一致 + 每个合约多 `delist_date` / `opt_maturity` / `opt_maturities` 三个字段，
每个**分组**多 `opt_near_*` 与 `fut_near_*` 两组字段（主连行的「最近期权到期」「最近合约交割」两列）。
⚠ 分组级字段与合约级**同名不同义**，别混用：合约级 `opt_maturity` 是
「这个合约自己的期权到期日」，分组级 `opt_near_date` 是「这个品种里最快到期的那个」。
**默认排序 = `opt_near_days` 升序**（最快到期的排最前，没有期权的垫底，并列按主连涨跌幅降序）；
「最近合约交割」列由前端点表头排（`fut_near_days` 升序，同样是快交割的在前）。

K 线：日线直接读库；周/月线**即时聚合**日线（不落库）——
⚠ 最后一根可能是不完整的周/月（落库那层才只写完整周，见 `core/sync.build_weekly`）。
"""
from __future__ import annotations

import datetime as dt
import re
import threading
import time
from collections import defaultdict

from core import db
from core import log as _log

_log_fut = _log.get("futures")

# 交易所（唯一清单）：顺序就是页面筛选下拉里的顺序。
EXCHANGES: list[dict] = [
    {"code": "SHFE",  "name": "上期所"},
    {"code": "DCE",   "name": "大商所"},
    {"code": "CZCE",  "name": "郑商所"},
    {"code": "CFFEX", "name": "中金所"},
    {"code": "INE",   "name": "上期能源"},
    {"code": "GFEX",  "name": "广期所"},
]
EXCHANGE_NAMES = {e["code"]: e["name"] for e in EXCHANGES}

# K 线周期：对外沿用旧页面的 klt 编号（101 日 / 102 周 / 103 月），内部转成词。
KLT = {"day": 101, "week": 102, "month": 103}
_KLT_NAME = {101: "day", 102: "week", 103: "month"}

# 允许的根数上限（页面给 120/250/500/1000 四档）
MAX_BARS = 1000

# 缓存：读库很快，但列表页一刷就查两遍也没必要；留 30s。
CACHE_TTL = 30

# 具体合约代码：品种码 + 4 位年月（RU2511）。带数字的才是**可交易合约**，
# 「连续」序列（RUL / IML1）不算 —— 展开里不摆它们。
_MONTH_RE = re.compile(r"^\D+\d{3,4}$")

_cache: dict = {"t": 0.0, "data": None}
_lock = threading.Lock()

# 当日一行要查的列（basic 的形态 + daily 的行情）
_COLS = """
    b.ts_code, b.symbol, b.exchange, b.name, b.fut_code, b.is_main, b.delist_date,
    d.open, d.high, d.low, d.close, d.settle, d.pre_close, d.pre_settle,
    d.change1, d.change2, d.vol, d.amount, d.oi, d.oi_chg
"""


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _f(v):
    """DECIMAL / None → float（页面要的是数字，不要 Decimal 的字符串样）。"""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def pct_of(row: dict):
    """涨跌幅%（**唯一来源**）：收盘价相对**昨结算**。

    期货的涨跌是相对**结算价**算的（tushare 的 `change1 = close − pre_settle`），
    不是股票的「收盘 vs 昨收」。缺昨结算（上市首日等）才退回昨收口径。
    """
    c, ps, pc = _f(row.get("close")), _f(row.get("pre_settle")), _f(row.get("pre_close"))
    if c is None:
        return None
    base = ps if ps else pc
    if not base:
        return None
    return (c - base) / base * 100.0


def product_name(main_name: str) -> str:
    """主连名 → 品种名：「橡胶主力」→「橡胶」、「沪深300期货」→「沪深300」。"""
    n = (main_name or "").strip()
    for suf in ("主力", "期货"):
        if n.endswith(suf) and len(n) > len(suf):
            return n[: -len(suf)]
    return n


def option_map(as_of: str | None = None) -> dict:
    """`{标的期货 ts_code: [期权到期日, ...]}`（**只算在市期权**）。

    一个期货合约（`PG2709.DCE`）对应的是一大批期权（各档行权价 × 购/沽），但到期日
    通常只有一个；少数品种（如豆一）会同时挂「连续月份」期权 ⇒ 到期日有 2 个，所以
    这里返回**列表**（升序，调用方取第一个当主显示）。

    ⚠ 表里会留历史残留（同步只丢「当次已退市」的），所以这里必须自己再按
    `delist_date >= 今天` 过滤一遍 —— 不能默认表里全是在市。
    ⚠ 中金所股指期权（IO/MO/HO）的标的是**指数**（`000300.SH`），对不到任何期货合约，
    自然不在这里出现 ⇒ 页面上那几行显示「—」，这是对的（它们现金交割、没有对应期货）。
    """
    day = as_of or dt.date.today().strftime("%Y-%m-%d")
    rows = db.query(
        "SELECT DISTINCT underlying, maturity_date FROM futures_option "
        "WHERE delist_date >= %s AND underlying <> '' AND maturity_date IS NOT NULL "
        "ORDER BY underlying, maturity_date", (day,)) or []
    out: dict[str, list] = defaultdict(list)
    for r in rows:
        out[str(r["underlying"])].append(str(r["maturity_date"]))
    return dict(out)


def _row(r: dict, opts: dict | None = None) -> dict:
    """库里一行 → 页面要的一行（字段与旧版一致，前端不用改）。"""
    code = r.get("ts_code") or ""
    dl = r.get("delist_date")
    mats = list((opts or {}).get(code) or [])
    return {
        "code": code,
        "secid": code,                      # K 线按钮带的就是这个（现在就是 ts_code）
        "name": r.get("name") or "",
        "symbol": r.get("symbol") or "",
        "market": EXCHANGE_NAMES.get(r.get("exchange") or "", r.get("exchange") or ""),
        "market_code": r.get("exchange") or "",
        "fut_code": r.get("fut_code") or "",
        "price": _f(r.get("close")),
        "pct_chg": pct_of(r),
        "chg": _f(r.get("change1")),
        "vol": _f(r.get("vol")),
        "amount": _f(r.get("amount")),
        "oi": _f(r.get("oi")),
        "high": _f(r.get("high")),
        "low": _f(r.get("low")),
        "open": _f(r.get("open")),
        "pre": _f(r.get("pre_settle")) or _f(r.get("pre_close")),
        "settle": _f(r.get("settle")),
        # ---- 两个「到期日」 ----
        # `dl` 来自 `futures_basic.delist_date`（tushare `fut_basic`）：**期货合约自己的**
        # 最后交易日。主连是连续序列，这一列恒为空（正确，它没有到期日）。
        "delist_date": str(dl) if dl else None,
        # 对应期权的到期日（`futures_option`，按标的期货合约 ts_code 对上）。
        # 多个时 `opt_maturity` 取**最早**那个，全部在 `opt_maturities` 里。
        "opt_maturity": mats[0] if mats else None,
        "opt_maturities": mats,
    }



def _sort_key_oi(x: dict):
    """次级排序：持仓量降序（缺的垫底，再看成交量）。

    以前它自己是**主** key（展开行按活跃度排）；2026-09-28 改成「按到期日近→远」后
    退成**并列时的次级** —— 同一天到期的合约怎么看，用持仓量决定更合理。
    """
    oi, vol = x.get("oi"), x.get("vol")
    return (-(oi if oi is not None else -1.0),
            -(vol if vol is not None else -1.0))


def _sort_key_delist(x: dict):
    """展开行（同品种合约）排序：按**合约自己的到期日**升序 —— 近月在前、远月在后。

    ⚠ 到期日取 `futures_basic.delist_date`（该合约自己的最后交易日），**不是**从合约名里
    的月份数字去猜：郑商所等的命名与最后交易日并不一一对应（`delist_date` 才是权威口径）。
    缺到期日的垫底（「不知道哪天到期」排最前会看着像最近月）；同一天到期（罕见）再按
    持仓量降序。
    """
    d = x.get("delist_date")
    return (1 if d is None else 0, str(d or ""), _sort_key_oi(x))


def _sort_key_pct(g: dict):
    """分组排序：按**主连**的涨跌幅降序。

    ⚠ 入参是**分组**（涨跌幅在 `g["main"]` 里），直接 `g.get("pct_chg")` 会恒为 None
    ⇒ 所有 key 相同、`sort` 退化成原序。这是踩过的坑，别改回去。
    """
    v = g["main"].get("pct_chg")
    return -(v if v is not None else -1e9)


def _sort_key_opt(g: dict):
    """分组排序（**默认排序**）：按「最近期权到期还有几天」**升序**（最快的排最前）。

    没有对应期权的品种（`opt_near_days is None`，如中金所股指期货）**垫底** ——
    它们是「不知道」，不是「0 天后到期」，排最前会误导。
    并列（天数相同）时退回按主连涨跌幅降序，省得顺序随库里行序飘。
    """
    d = g.get("opt_near_days")
    if d is None:
        return (1, 0.0, _sort_key_pct(g))
    return (0, float(d), _sort_key_pct(g))


# ---------------------------------------------------------------------------
# 列表
# ---------------------------------------------------------------------------

def latest_trade_date():
    """库里期货日线的最新交易日（`None` 表示还没同步过）。"""
    d = db.scalar("SELECT MAX(trade_date) FROM futures_daily")
    return None if d is None else str(d)


def build_groups(rows: list[dict], as_of: str, opts: dict | None = None,
                 today: str | None = None) -> list[dict]:
    """当日有行情的全部合约 → 按品种分组（主连一行 + 同品种合约）。

    `rows` 是 `_row()` 之前的原始字典（要带 `is_main` / `delist_date` / `ts_code`）。
    `opts` 是 `option_map()` 的结果（`{标的期货 ts_code: [期权到期日…]}`），只用来给
    每行补「对应期权到期日」；**不给就只是没有这一列**（回归里喂合成数据方便）。
    `today` 是「最近期权到期还有几天」的基准日（`YYYY-MM-DD`），不给就取**本机今天**；
    回归里显式传，免得天数断言随运行日期飘。
    ⚠ 分组 key 是 `(exchange, fut_code)` —— 不同交易所可以有同名品种码。
    """
    day = today or dt.date.today().strftime("%Y-%m-%d")
    by_key: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        by_key[(r.get("exchange") or "", r.get("fut_code") or "")].append(r)

    groups: list[dict] = []
    for (ex, fut), items in by_key.items():
        main_src = next((x for x in items if int(x.get("is_main") or 0) == 1), None)
        if main_src is None:
            continue                                  # 没有主连的品种不占顶层一行
        mates = []
        for x in items:
            if x is main_src:
                continue
            sym = str(x.get("symbol") or "")
            if not _MONTH_RE.match(sym):
                continue                              # 「连续」序列不可交易，不摆
            dl = x.get("delist_date")
            if dl is not None and str(dl) < as_of:
                continue                              # 已退市
            mates.append(_row(x, opts))
        mates.sort(key=_sort_key_delist)
        main = _row(main_src, opts)
        # 主连行的「最近期权到期」：从**这个品种的可交易合约**里挑最快到期的那个期权。
        # ⚠ 不能只看 main —— 主连（`RU.SHF`）不是可交易合约，`opts` 里根本没有它的 key，
        #   直接取会恒为 None（页面永远显示「—」，且排序整体退化）。
        # ⚠ 也不要用 `opt_maturity`（那是「这个合约自己的」）；这里要的是品种内最小值。
        # ⚠ 已过期的期权（`< today`）不算「最快到期」—— 那是历史，排最前纯属误导。
        near_date, near_code, near_name = None, "", ""
        for c in mates:
            for m in c.get("opt_maturities") or []:
                if str(m) >= day and (near_date is None or str(m) < near_date):
                    near_date, near_code, near_name = str(m), c["code"], c["name"]
        near_days = None
        if near_date:
            near_days = (dt.date.fromisoformat(near_date) - dt.date.fromisoformat(day)).days
        # 主连行的「最近合约交割」：品种内**最后交易日最早**的那个可交易期货合约。
        # ⚠ 不能看主连自己的 delist_date —— 主连是连续序列，恒为空（与 opt_near_ 同理，
        #   必须在品种内聚合）。
        # ⚠ `mates` 已经按到期日近→远排好、且剔了已退市的（`delist_date < as_of`）⇒ 第一个
        #   带到期日的**就是展开行第一行**。两处必须指同一个合约（探针据此交叉验），
        #   所以这里**不跳过「已交割但还在最后一个行情日里」的合约**（那种常见于夜盘前 /
        #   周末打开：行情日是上周五、合约上周五交割），days 会是负数，页面显示「已交割」。
        near_f = next((c for c in mates if c.get("delist_date")), None)
        fut_near_date = near_f["delist_date"] if near_f else None
        fut_near_code = near_f["code"] if near_f else None
        fut_near_name = near_f["name"] if near_f else None
        fut_near_days = None
        if fut_near_date:
            fut_near_days = (dt.date.fromisoformat(fut_near_date)
                             - dt.date.fromisoformat(day)).days
        groups.append({
            "product": product_name(main_src.get("name") or ""),
            "market": EXCHANGE_NAMES.get(ex, ex),
            "market_code": ex,
            "fut_code": fut,
            "main": main,
            "contracts": mates,
            "n_contracts": len(mates) + 1,            # 含主连自己
            # ---- 分组级（主连行的「最近期权到期」列；与合约级同名字段不同义）----
            "opt_near_date": near_date,               # 最快到期的期权到期日
            "opt_near_days": near_days,               # 距 today 的自然日（None = 没有）
            "opt_near_code": near_code or None,       # 这个到期日是从哪个期货合约带出来的
            "opt_near_name": near_name or None,
            # ---- 分组级（主连行的「最近合约交割」列；与 opt_near_* 是两个概念）----
            "fut_near_date": fut_near_date,           # 最快交割的期货合约最后交易日
            "fut_near_days": fut_near_days,           # 距 today 的自然日（负数 = 已交割）
            "fut_near_code": fut_near_code or None,   # 就是展开行第一行那个合约
            "fut_near_name": fut_near_name or None,
        })
    return groups


def get_all(force: bool = False) -> dict:
    """读本地库：主连 + 同品种合约，**默认按「最近期权到期」升序**（最快到期的排最前）。

        {as_of, cached, source, trade_date, markets: [{code, name, n, n_mains, error}],
         groups: [{product, market, market_code, main, contracts, n_contracts,
                   opt_near_date, opt_near_days, opt_near_code, opt_near_name,
                   fut_near_date, fut_near_days, fut_near_code, fut_near_name}],
         stats: {n_markets, n_contracts, n_mains, up, down, flat}}

    ⚠ 库里没数据时**不报错** —— 返回空列表 + `error` 说明「还没同步期货日线」，
    页面照常渲染（提示去同步），而不是整页崩掉。
    """
    now = time.time()
    with _lock:
        c = _cache
        if not force and c["data"] is not None and now - c["t"] < CACHE_TTL:
            out = dict(c["data"])
            out["cached"] = True
            return out

    day = latest_trade_date()
    today = dt.date.today().strftime("%Y-%m-%d")
    rows: list[dict] = []
    if day:
        rows = db.query(
            "SELECT %s FROM futures_basic b "
            "JOIN futures_daily d ON d.ts_code = b.ts_code AND d.trade_date = %%s"
            % _COLS, (day,)) or []

    groups = build_groups(rows, day or "", option_map() if day else {}, today)
    groups.sort(key=_sort_key_opt)          # 默认：最近期权到期升序（没有期权的垫底）

    market_rows = []
    for e in EXCHANGES:
        gs = [g for g in groups if g["market_code"] == e["code"]]
        market_rows.append({
            "code": e["code"], "name": e["name"],
            "n": sum(g["n_contracts"] for g in gs),
            "n_mains": len(gs),
            "error": None if day else "本地库还没有期货日线（先跑一次期货同步）",
        })

    pcts = [g["main"].get("pct_chg") for g in groups if g["main"].get("pct_chg") is not None]
    data = {
        "as_of": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        # ⚠ 行情日与「现在」不是一回事：期货日线收盘结算后才更新，盘中最新一天还是上一交易日，
        #   页面要把「截至哪一天」写清楚，别让人以为是实时价。
        "trade_date": day,
        "cached": False,
        "source": "tushare · 本地库",
        "markets": market_rows,
        "groups": groups,
        "stats": {
            "n_markets": len(EXCHANGES),
            "n_contracts": sum(m["n"] for m in market_rows),
            "n_mains": len(groups),
            "up": sum(1 for v in pcts if v > 0),
            "down": sum(1 for v in pcts if v < 0),
            "flat": sum(1 for v in pcts if v == 0),
        },
    }
    with _lock:
        _cache.update({"t": now, "data": data})
    return data


def clear_cache() -> None:
    """丢掉缓存（手动刷新 / 测试用）。"""
    with _lock:
        _cache.update({"t": 0.0, "data": None})


# ---------------------------------------------------------------------------
# K 线
# ---------------------------------------------------------------------------

def _week_key(d: dt.date) -> tuple:
    """周 key：与项目周线口径一致 —— 用**周一**当一周的开头。"""
    return (d.isocalendar()[0], d.isocalendar()[1])


def _agg(bars: list[dict], freq: str) -> list[dict]:
    """日线 → 周/月线（**即时聚合，不落库**）。

    开=区间首根的开盘，高/低=区间极值，收=区间末根收盘，量额=求和。
    日期标签取区间**最后一根**的日期（与 `weekly_kline.trade_date` 同一口径）。
    """
    # ⚠ 别写成 `key_of = lambda s: A if freq == 'week' else (lambda s: s[:7])` ——
    #   那样 else 分支返回的是一个 **lambda 对象**，而每次调用都新建一个 ⇒ key 两两不等
    #   ⇒ 永远不聚合（月线退化成日线，实测就是这么静默错的，周线却是对的，极难发现）。
    if freq == "week":
        def key_of(s):
            return _week_key(dt.date.fromisoformat(s))
    else:
        def key_of(s):
            return s[:7]
    out: list[dict] = []
    cur_key, cur = None, None
    for b in bars:
        k = key_of(b["date"])
        if k != cur_key:
            if cur:
                out.append(cur)
            cur_key = k
            cur = {"date": b["date"], "open": b["open"], "high": b["high"],
                   "low": b["low"], "close": b["close"], "vol": b["vol"] or 0.0,
                   "amount": b["amount"] or 0.0, "pct_chg": None}
        else:
            cur["date"] = b["date"]
            cur["high"] = max([v for v in (cur["high"], b["high"]) if v is not None], default=None)
            cur["low"] = min([v for v in (cur["low"], b["low"]) if v is not None], default=None)
            cur["close"] = b["close"]
            cur["vol"] = (cur["vol"] or 0.0) + (b["vol"] or 0.0)
            cur["amount"] = (cur["amount"] or 0.0) + (b["amount"] or 0.0)
    if cur:
        out.append(cur)
    # 聚合完再算涨跌幅（相对上一根的收盘），这样周/月也有涨跌
    prev = None
    for b in out:
        if prev is not None and b["close"] is not None and prev:
            b["pct_chg"] = (b["close"] - prev) / prev * 100.0
        prev = b["close"]
    return out


def kline(ts_code: str, klt: int = 101, limit: int = 250) -> dict:
    """取一根合约的 K 线（**读本地库**）。

    `ts_code` 形如 `RU.SHF` / `RU2511.SHF`（页面「查看K线」原样带过来的就是它）。
    日线直接读 `futures_daily`；周/月线由日线即时聚合（不落库）。
    """
    if klt not in _KLT_NAME:
        raise ValueError("klt 只支持 101(日) / 102(周) / 103(月)")
    limit = max(30, min(int(limit or 250), MAX_BARS))
    rows = db.query(
        "SELECT trade_date, open, high, low, close, vol, amount, pre_settle, pre_close "
        "FROM futures_daily WHERE ts_code = %s ORDER BY trade_date DESC LIMIT %s",
        (ts_code, int(limit * (8 if _KLT_NAME[klt] != "day" else 1))))
    if not rows:
        raise RuntimeError("本地库没有这个合约的日线（代码不对，或还没同步）")
    bars = [{
        "date": str(r["trade_date"]),
        "open": _f(r.get("open")), "high": _f(r.get("high")),
        "low": _f(r.get("low")), "close": _f(r.get("close")),
        "vol": _f(r.get("vol")), "amount": _f(r.get("amount")),
        "pct_chg": pct_of(r),
    } for r in rows][::-1]
    freq = _KLT_NAME[klt]
    if freq != "day":
        bars = _agg(bars, freq)
    bars = bars[-limit:]
    name = db.scalar("SELECT name FROM futures_basic WHERE ts_code = %s", (ts_code,))
    return {"secid": ts_code, "name": name, "klt": klt, "bars": bars}
