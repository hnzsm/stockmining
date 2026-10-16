# -*- coding: utf-8 -*-
"""我的关注 —— 自选股分组 + 价格形态提醒 + 选股命中提醒。

## 规则有**两种**，由 `kind` 分（别把两套口径混着读）

| `kind` | 判什么 | 用到的字段 |
|---|---|---|
| `price`（默认） | 指标/价位：收盘价与某条线/某个价位的关系 | `freq` / `line` / `value` / `op` |
| `pool` | **选股命中**：按一条**选股策略**（`pool_strategy`）求值，票在池里就提醒 | `pool_id` / `freq` |

⚠ `kind='pool'` 的行里 `line/value/op` 全是空的（别把这三个当判断条件）—— 拿 price 那一套去读它，
`line=''` 会被当成「绝对价位」、`op=''` 查不到动作名，页面就冒出一个「空动作」的假胶囊。
`freq` 两档都用：price 档 = 判哪张图；pool 档 = 判**哪一天**的在池状态。

## 规则模型（price 档：只有四个字段，别再加一层）
`watch_alert` 一行 = 一条提醒，判什么由 `freq` / `line` / `value` / `op` 四个字段决定：

| 字段 | 含义 |
|---|---|
| `freq` | 判**哪张图**：`daily` 日K · `weekly` 周K（周K 只判**已收盘的完整周**） |
| `line` | 判**哪条线**：`ma5/ma10/ma20/ma60`、`boll_mid`（布林中轨）/`boll_upper`/`boll_lower`。为空表示改判**绝对价位** |
| `value` | `line` 为空时用的绝对价位（如 `10.50`） |
| `op` | `cross_up` 上穿 · `cross_down` 下穿 · `touch` 触及 |

## 判定口径（price 档：**必须与页面文案一致，改这里就要改文案**）
- 逐根 K 线判，**只用第 i-1 根与第 i 根** ⇒ 不含未来函数。
- `cross_up`：`close[i-1] <= 线[i-1]` 且 `close[i] > 线[i]` —— **收盘价口径**，
  不是「盘中突破一下就算」。
- `cross_down`：`close[i-1] >= 线[i-1]` 且 `close[i] < 线[i]`。
- `touch`（触及）：`low[i] <= 线[i] <= high[i]` —— 这条是**盘中**口径（当根最低价与最高价之间碰到线）。
- `line` 为空时把「线」换成固定的 `value`：上穿 = `close[i-1] <= v < close[i]`；触及 = `low[i] <= v <= high[i]`。

⚠ 上面的 `close / low / high` **跟着 `freq` 走**：日K 那根就是当天，周K 那根是**整周**的
（周开/周高/周低/周收）。所以周K 的「上穿」= 这一周**收在**线上方、上一周收在线下方，
**一周最多出一次**，而且**要等这周走完**（`weekly_kline` 里只有已收盘的完整周）。

## 判定口径（pool 档：**与选股页 / 回测同一套**）
`scan_pool()` 是唯一实现，走的全是选股那条路的函数：
  · 条件 → `strategy.selection_cfg()` + `strategy.resolve_pool()` / `strategy.compute_pool()`
    （**不是**自己再写一遍表达式求值）；
  · 静态过滤（剔 ST / 板块 / 上市天数）→ `market.apply_pool_filters()`；
  · 最小日成交额 → 由 `compute_pool()` 内部按 `pool_filters()["min_amount"]` 处理；
  · 基础池（`base`，如沪深300 / 行业）→ `market.resolve_universe()` 取当天成分再求交集；
  · 数据来源 → 日K 走 `market.load_panel(..., adjust="qfq")`、周K 走 `market.compute_weekly_panel()`，
    与选股 / 回测 / 个股诊断页**同一个**。
⚠ 判定是「**这一天在不在池里**」，与 price 档一样逐根判、默认只看最新那根（`lookback=1`）。
⚠ **周期（2026-10-10）**：面板周期 = **选股策略自己的 `timeframe`**（`selection_cfg()` 一步抬到位）——
  周K 策略的 `close`/`ma5` 都在周线上算，与选股页 / 回测完全一致（用户要的「整条链按周K跑」）；
  提醒自己另选的 `freq` 只管**判定节奏**（日K 判 `latest`、周K 判最近一个已收盘的完整周）。

## 数据口径
价格与均线走的是 `market.load_panel(..., adjust="qfq")` —— **前复权**，与选股 / 回测 /
个股诊断页**同一个来源**。⚠ 库里 `daily_kline` / `weekly_kline` 存的是**未复权**价，自己写条 SQL 去算
会当场养出第二个口径（除权那天均线会跟页面上的数对不上）。

日K 取 `daily_kline`、周K 取 `weekly_kline`（只是 `freq=` 换个表），复权口径完全一样。
⚠ 别拿「日线面板上的 `w_ma10`」来做周K 提醒：那是**按 asof 贴在日线上**的另一套口径，
周中只拿得到上一周的周线，等于「日线收盘价去穿上周的均线」—— 与「判周线那一根」是两回事。

## 触发记录
命中写 `watch_alert_log`，唯一键 `(alert_id, trade_date)` ⇒ 同一条规则同一天**只记一条**，
反复检测不会刷屏。检测只判**最近若干根**（默认 1 根 = 最新交易日），不是全历史回放。
"""
from __future__ import annotations

import datetime as dt
import re

import pandas as pd

from core import db, jobs, log as _log
from core import market, universe

_log_w = _log.get("watch")

# 线名 → (展示名, 需要的指标字段, 分组标签)。展示名与分组的**唯一来源**，页面下拉从这里取。
LINES: dict[str, tuple[str, str, str]] = {
    "ma5": ("MA5", "ma5", "均线"),
    "ma10": ("MA10", "ma10", "均线"),
    "ma20": ("MA20", "ma20", "均线"),
    "ma60": ("MA60", "ma60", "均线"),
    "boll_mid": ("布林中轨", "boll_mid", "布林"),
    "boll_upper": ("布林上轨", "boll_upper", "布林"),
    "boll_lower": ("布林下轨", "boll_lower", "布林"),
}
LINE_GROUPS = ["均线", "布林"]      # 下拉里各分组的先后；「绝对价位」由页面单独摆在最后
# ⚠ 保序的 key 列表：页面**不能**用 `Object.keys(LINES)` 来排 —— Flask 的 `tojson` 会把 dict
#   的键**按字典序**输出（实测 MA5 会被排到 MA10/MA20 后面），得靠这份清单定顺序。
LINE_ORDER = list(LINES)
OPS: dict[str, str] = {"cross_up": "上穿", "cross_down": "下穿", "touch": "触及"}
OP_ORDER = list(OPS)

# 周期：判哪张图。展示名的**唯一来源是 `universe.FREQS`**（全站共用，选股策略的周期也用它），
# 这里只是转出来 —— 页面下拉取 `watch.FREQS`，改枚举只改 `universe` 一处。
# ⚠ 2026-10-08 需求原话「提醒也可以添加周线指标提醒」。
#   ⚠ 周K 判的是 `weekly_kline` 里**那根完整周线**，不是「把 `w_ma10` 贴到日线上」：
#     后者在周中只拿得到上周的周线，等于「日线收盘价去穿上周均线」，一周里可能反复触发；
#     判周线那根则**每周最多出一次**，且只在该周收盘之后 —— 两种口径的答案不一样，别混。
FREQS = universe.FREQS
FREQ_ORDER = universe.FREQ_ORDER
DEFAULT_FREQ = universe.DEFAULT_FREQ

# 规则类型（`watch_alert.kind`）。展示名的**唯一来源**，页面上的类型切换按钮从这里取。
# ⚠ 2026-10-10 需求：「我的关注里面的提醒，希望可以通过绑定选股策略规则，如果选股命中则提醒」。
#   `price` = 老的那一套（指标 / 绝对价位）；`pool` = 绑定一条选股策略，入池即提醒。
#   两者的字段口径完全不同（pool 不用 line/value/op），别在别处再各写一套判断。
# ⚠ `freq`（日K / 周K）**两档都适用**（2026-10-10 需求「绑定选股策略也应该要有日k周期和
#   周k周期的选择」）：price 档看的是「判哪张图的线」，pool 档看的是「判**哪一天**的在池状态」——
#   `daily` = 判 `latest` 那根；`weekly` = 只判「最近一个已收盘的完整周」的最后一个交易日。
#   ⚠ pool 档另有**第二个周期**：面板周期 = **选股策略自己的 `timeframe`**（周K 策略整条链按周线跑，
#     判定只有一处 `strategy.selection_cfg()`）。两者各管一段：面板周期管「条件在哪条 K 线上算」，
#     freq 管「判哪一天」。策略是周K 时面板只有走完的整周 ⇒ freq 给什么都收敛到同一根，实际节奏仍是 weekly。
KINDS: dict[str, str] = {"price": "指标 / 价位", "pool": "选股命中"}
KIND_ORDER = ["price", "pool"]
DEFAULT_KIND = "price"

# 表单默认：**判 MA10、动作上穿**。
# ⚠ 2026-10-08 用户反馈：「我要的是上穿 MA10 那种提醒，不是填价格的提醒」——
#   原来下拉第一项是「绝对价位」，一打开弹窗就是个价格输入框，等于把最常用的那条路藏起来了。
DEFAULT_LINE = "ma10"
DEFAULT_OP = "cross_up"

# 「常用规则」一键按钮：`(周期, 线, 动作, 按钮文案)`。点一下直接加，不必先去三个下拉里挑。
# ⚠ 周K 那几条**文案必须带「周K」** —— 页面把两行按钮上下摆在一起，不带前缀分不清
#   点的是日线还是周线，而这两条路建出来的规则行为完全不同。
QUICK_RULES: list[tuple[str, str, str, str]] = [
    ("daily", "ma5", "cross_up", "上穿 MA5"),
    ("daily", "ma10", "cross_up", "上穿 MA10"),
    ("daily", "ma20", "cross_up", "上穿 MA20"),
    ("daily", "ma10", "cross_down", "下穿 MA10"),
    ("daily", "boll_mid", "touch", "触及布林中轨"),
    ("daily", "boll_lower", "touch", "触及布林下轨"),
    ("weekly", "ma5", "cross_up", "周K 上穿 MA5"),
    ("weekly", "ma10", "cross_up", "周K 上穿 MA10"),
    ("weekly", "ma10", "cross_down", "周K 下穿 MA10"),
    ("weekly", "boll_lower", "touch", "周K 触及布林下轨"),
]

NEED_FIELDS = {"close", "high", "low"} | {v[1] for v in LINES.values()}


# ---------------------------------------------------------------------------
# 分组 / 自选股
# ---------------------------------------------------------------------------

def ensure_group(name: str = "我的自选", note: str = "") -> int:
    """分组不存在就建一个（默认分组「我的自选」）。"""
    row = db.query_one("SELECT id FROM watch_group WHERE name=%s", (name,))
    if row:
        return int(row["id"])
    return int(db.insert("INSERT INTO watch_group (name, note, sort) VALUES (%s, %s, "
                         "(SELECT COALESCE(MAX(sort),0)+1 FROM watch_group g))", (name, note)))


def list_groups() -> list[dict]:
    rows = db.query(
        "SELECT g.id, g.name, g.note, g.sort, "
        "       (SELECT COUNT(*) FROM watch_stock s WHERE s.group_id=g.id) n, "
        "       (SELECT COUNT(*) FROM watch_alert a JOIN watch_stock s2 ON s2.ts_code=a.ts_code "
        "         WHERE s2.group_id=g.id) alerts "
        "  FROM watch_group g ORDER BY g.sort, g.id")
    return [{"id": int(r["id"]), "name": r["name"], "note": r["note"] or "",
             "stocks": int(r["n"] or 0), "alerts": int(r["alerts"] or 0)} for r in rows]


def save_group(name: str, note: str = "", gid: int | None = None) -> int:
    name = (name or "").strip()
    if not name:
        raise ValueError("分组名不能为空")
    if gid:
        db.execute("UPDATE watch_group SET name=%s, note=%s WHERE id=%s", (name[:40], note[:200], gid))
        return int(gid)
    dup = db.query_one("SELECT id FROM watch_group WHERE name=%s", (name[:40],))
    if dup:
        raise ValueError(f"已经有叫「{name}」的分组了")
    return int(db.insert("INSERT INTO watch_group (name, note, sort) VALUES (%s, %s, "
                         "(SELECT COALESCE(MAX(sort),0)+1 FROM watch_group g))",
                         (name[:40], note[:200])))


def delete_group(gid: int) -> dict:
    """删分组连带删组内自选股；**任何组都不再持有的票，提醒规则一起清掉**（免得留孤儿）。"""
    codes = [r["ts_code"] for r in db.query("SELECT ts_code FROM watch_stock WHERE group_id=%s", (gid,))]
    db.execute("DELETE FROM watch_stock WHERE group_id=%s", (gid,))
    db.execute("DELETE FROM watch_group WHERE id=%s", (gid,))
    dropped = []
    for c in codes:
        if not db.scalar("SELECT COUNT(*) FROM watch_stock WHERE ts_code=%s", (c,)):
            dropped += [int(r["id"]) for r in db.query("SELECT id FROM watch_alert WHERE ts_code=%s", (c,))]
            db.execute("DELETE FROM watch_alert WHERE ts_code=%s", (c,))
            db.execute("DELETE FROM watch_alert_log WHERE ts_code=%s", (c,))
    return {"group_id": int(gid), "stocks": len(codes), "alerts_dropped": len(dropped)}


def norm_code(raw) -> str:
    """用户输入 → 库里的 `ts_code`。`600000` / `600000.sh` / `sh600000` 都收。

    规则与 `hot.ts_code_of` / 诊断页 `normCode` 同一套：**只认数字段**，6→沪、0/3→深、4/8/9→北。
    已经是 `XXXXXX.XX` 的原样返回（但后缀统一大写）。
    """
    s = str(raw or "").strip().upper()
    if re.match(r"^\d{6}\.(SH|SZ|BJ)$", s):
        return s
    c = re.sub(r"\D", "", s)
    if len(c) != 6:
        return ""
    mk = "SH" if c[0] == "6" else ("SZ" if c[0] in "03" else ("BJ" if c[0] in "489" else ""))
    return f"{c}.{mk}" if mk else ""


def add_stocks(gid: int, codes: list[str]) -> dict:
    """加自选。代码宽容一点（`600000` 也认）；认不出的、库里没有的，分别报出来。"""
    names = {r["ts_code"]: r["name"] for r in db.query("SELECT ts_code, name FROM stock_basic")} \
        if codes else {}
    added, dup, unknown = [], [], []
    for raw in codes:
        c = norm_code(raw)
        if not c or c not in names:
            unknown.append(str(raw)[:20])
            continue
        if db.scalar("SELECT COUNT(*) FROM watch_stock WHERE group_id=%s AND ts_code=%s", (gid, c)):
            dup.append(c)
            continue
        db.insert("INSERT INTO watch_stock (group_id, ts_code, name, sort) VALUES "
                  "(%s, %s, %s, (SELECT COALESCE(MAX(sort),0)+1 FROM watch_stock s WHERE s.group_id=%s))",
                  (gid, c, names[c], gid))
        added.append(c)
    return {"added": added, "dup": dup, "unknown": unknown}


def remove_stocks(gid: int | None, codes: list[str]) -> dict:
    n = 0
    pruned = []
    for raw in codes:
        c = norm_code(raw)
        if not c:
            continue
        if gid:
            db.execute("DELETE FROM watch_stock WHERE group_id=%s AND ts_code=%s", (gid, c))
        else:
            db.execute("DELETE FROM watch_stock WHERE ts_code=%s", (c,))
        n += 1
        if not db.scalar("SELECT COUNT(*) FROM watch_stock WHERE ts_code=%s", (c,)):
            db.execute("DELETE FROM watch_alert WHERE ts_code=%s", (c,))
            db.execute("DELETE FROM watch_alert_log WHERE ts_code=%s", (c,))
            pruned.append(c)
    return {"removed": n, "alerts_pruned": pruned}


def transfer_stocks(from_gid: int, to_gid: int, codes: list[str], mode: str = "move") -> dict:
    """把自选股在分组之间**复制**（`copy`）或**移动**（`move`）。

    ⚠ 写库顺序只能是「**先加到目标组、再删源组**」，不能反：
      `remove_stocks` 有个防孤儿的收尾 —— 某只票**哪个组都不剩**时，它的提醒规则会被一并清掉。
      先删的话，这只票在中途短暂地不属于任何组，规则当场被误清，紧接着的 add 也救不回来
      （用户看到的现象是「拖一下，提醒没了」，而且不报错）。
    ⚠ 同一组之间的拖动直接短路：`move` = 删了又加回来、净效果为零，`copy` = 目标组本来就有。
      不短路的话两个动作都会白跑一堆写库，还会在「删」那一步踩上一条的孤儿清理。
    ⚠ 备注（`note`）**跟着票走**：复制 / 移动都把源组的备注带过去；但**只填目标组里空着的那行** ——
      目标组已经写过备注时保留人家的，别拿源组的把用户写的覆盖了。
    """
    mode = (mode or "move").strip().lower()
    if mode not in ("copy", "move"):
        raise ValueError(f"不认识的操作：{mode}")
    from_gid, to_gid = int(from_gid), int(to_gid)
    if from_gid == to_gid:
        return {"from": from_gid, "to": to_gid, "mode": mode, "same": True,
                "added": [], "dup": [], "unknown": [], "removed": [], "alerts_pruned": [],
                "copied": 0, "moved": 0}
    src_note = {r["ts_code"]: (r["note"] or "") for r in db.query(
        "SELECT ts_code, note FROM watch_stock WHERE group_id=%s", (from_gid,))}
    res = add_stocks(to_gid, codes)               # ① 先加（含已存在的，记在 dup 里）
    touched = res["added"] + res["dup"]
    for c in touched:                             # ② 备注带过去（只填空的）
        n = src_note.get(c) or ""
        if n:
            db.execute("UPDATE watch_stock SET note=%s WHERE group_id=%s AND ts_code=%s "
                       "AND (note IS NULL OR note='')", (n[:512], to_gid, c))
    out = {"from": from_gid, "to": to_gid, "mode": mode, "same": False,
           "added": res["added"], "dup": res["dup"], "unknown": res["unknown"],
           "removed": [], "alerts_pruned": [], "copied": len(touched), "moved": 0}
    if mode == "move" and touched:                # ③ 最后才删源组（此时目标组已持有 ⇒ 规则不会被当孤儿清）
        r2 = remove_stocks(from_gid, touched)
        out["removed"] = list(touched)
        out["alerts_pruned"] = r2["alerts_pruned"]
        out["moved"] = len(touched)
    return out


def list_stocks(gid: int | None = None) -> list[dict]:
    """组内自选股 + 本库最近交易日行情 + 该票的提醒条数。行情**只报那天有数据的**。"""
    if gid:
        rows = db.query("SELECT * FROM watch_stock WHERE group_id=%s ORDER BY sort, id", (gid,))
    else:
        rows = db.query("SELECT * FROM watch_stock ORDER BY group_id, sort, id")
    if not rows:
        return []
    latest = market.latest_trade_date()
    codes = [r["ts_code"] for r in rows]
    quote: dict = {}
    if latest and codes:
        ph = ",".join(["%s"] * len(codes))
        quote = {r["ts_code"]: r for r in db.query(
            f"SELECT ts_code, close, pct_chg FROM daily_kline "
            f"WHERE trade_date=%s AND ts_code IN ({ph})", [latest] + codes)}
    alert_n = {r["ts_code"]: int(r["n"]) for r in db.query(
        "SELECT ts_code, COUNT(*) n FROM watch_alert GROUP BY ts_code")}
    out = []
    for r in rows:
        q = quote.get(r["ts_code"]) or {}
        pct = q.get("pct_chg")
        pct = None if pct is None else float(pct)
        close = q.get("close")
        out.append({
            "ts_code": r["ts_code"], "name": r["name"] or "", "note": r["note"] or "",
            "group_id": int(r["group_id"]),
            "close": None if close is None else float(close),
            "pct_chg": pct,
            "pct_text": "—" if pct is None else f"{pct:+.2f}%",
            "pct_cls": "" if pct is None else ("up" if pct > 0 else ("down" if pct < 0 else "flat")),
            "has_quote": bool(q),
            "alerts": alert_n.get(r["ts_code"], 0),
        })
    return out


def set_note(gid: int, ts_code: str, note: str) -> None:
    # ⚠ 512 与 `watch_stock.note` 列宽一致（原 200 会把批量导入的题材标记截尾，见 db.WIDEN_MIGRATIONS）。
    db.execute("UPDATE watch_stock SET note=%s WHERE group_id=%s AND ts_code=%s",
               ((note or "")[:512], gid, ts_code))


# ---------------------------------------------------------------------------
# 提醒规则 CRUD
# ---------------------------------------------------------------------------

def _norm_alert(line: str, op: str, value, freq: str = DEFAULT_FREQ):
    freq = (freq or DEFAULT_FREQ).strip() or DEFAULT_FREQ
    if freq not in FREQS:
        raise ValueError(f"不认识的周期：{freq}")
    line = (line or "").strip()
    if line and line not in LINES:
        raise ValueError(f"不认识的线：{line}")
    if op not in OPS:
        raise ValueError(f"不认识的动作：{op}")
    if not line:
        if value is None:
            raise ValueError("没选线就必须给一个价位")
        value = float(value)
    else:
        value = None
    # ⚠ 「触及」用在绝对价位上是合理的（盘中摸到某价）；三种动作对两种目标都允许。
    return freq, line, op, value


def save_alert(ts_code: str, line: str, op: str, value=None, note: str = "",
               enabled: int = 1, aid: int | None = None,
               freq: str = DEFAULT_FREQ, kind: str = DEFAULT_KIND,
               pool_id=None) -> int:
    # ⚠ `freq` 放在**最后**且带默认值：老调用（不传周期）落到日K，与加这个字段之前的行为一致。
    # ⚠ `kind` / `pool_id`（2026-10-10）同样追加在末尾带默认值 —— 老调用方（含回归里的
    #   `wt._norm_alert`）一个字都不用改，等价于「老规则全是 price 档」。
    ts_code = norm_code(ts_code)
    if not ts_code:
        raise ValueError("股票代码认不出来")
    kind = (kind or DEFAULT_KIND).strip() or DEFAULT_KIND
    if kind not in KINDS:
        raise ValueError(f"不认识的规则类型：{kind}")
    if kind == "pool":
        # pool 档只认 `pool_id` + **周期**。⚠ `line` / `op` / `value` 三个**一律清空**
        # （页面切过类型再存时，表单里可能还留着上一次填的线/价位）—— 留着的话列表里会按
        # price 口径回显，而检测走的是 pool 口径，两边说的不是一回事。
        # ⚠⚠ `freq` 从 2026-10-10 起**不再清空**（需求「绑定选股策略也应该要有日k周期和
        #   周k周期的选择」）：`daily` = 每个交易日判；`weekly` = 只判「最近一个**已收盘的
        #   完整周**」那根 —— 判定仍用**日线**数据（选股策略的条件本来就是日线口径），
        #   取该周最后一个交易日的收盘算，一周最多一条（唯一键 `(alert_id, trade_date)`）。
        #   见 `scan_pool` / `check()` 里 pool 段那两处注释。
        pool_id = int(pool_id) if str(pool_id or "").strip() not in ("", "None") else None
        if not pool_id:
            raise ValueError("选股命中提醒必须挑一条选股策略")
        from core import pool as pool_mod      # 延迟导入：pool → universe → strategy 链路
        if not pool_mod.get_pool(pool_id):
            raise ValueError(f"选股策略 #{pool_id} 不存在（可能已被删除）")
        freq = (freq or DEFAULT_FREQ).strip() or DEFAULT_FREQ
        if freq not in FREQS:
            raise ValueError(f"不认识的周期：{freq}")
        line, op, value = "", "", None
    else:
        pool_id = None
        freq, line, op, value = _norm_alert(line, op, value, freq)
    if aid:
        db.execute("UPDATE watch_alert SET kind=%s, pool_id=%s, freq=%s, line=%s, value=%s, "
                   "op=%s, note=%s, enabled=%s WHERE id=%s",
                   (kind, pool_id, freq, line, value, op, (note or "")[:200],
                    1 if enabled else 0, aid))
        return int(aid)
    return int(db.insert(
        "INSERT INTO watch_alert (ts_code, kind, pool_id, freq, line, value, op, note, enabled) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (ts_code, kind, pool_id, freq, line, value, op, (note or "")[:200], 1 if enabled else 0)))


def _alert_text(line: str, op: str, value, freq: str = DEFAULT_FREQ,
                kind: str = DEFAULT_KIND, pool_name: str | None = None) -> str:
    """一条规则的「人话」（列表里那颗胶囊上的字）。

    ⚠ **唯一来源**：`list_alerts` 的 `text` 与 `delete_alert` 回的 `text` 都走这里 ——
      两处各拼一份，迟早「列表里写的是这个、删除提示里写的是那个」。
    ⚠ 日K **不加前缀**（老文案一个字不变，回归里那些「上穿 MA10」的断言不用改）；
      周K 把周期写进文本里，一眼能分开两种周期（「周K·上穿MA10」）。
    ⚠ pool 档（2026-10-10）单独一支：写成「选股命中·策略名」。
      `pool_name` 取不到（策略被删）时退成「选股策略已删除」—— 别渲染成一个空胶囊
      （`line=''` 会走到「绝对价位」那一支，显示出来就是「上穿—」这种东西）。
    ⚠ pool 档的**周K**也带周期前缀（2026-10-10）：「周K·选股命中·策略名」。
      日K 仍然**不加前缀**（`text` 还是以「选股命中」开头，老断言与老文案都不动）。
    """
    if (kind or DEFAULT_KIND) == "pool":
        wtag = "" if (freq or DEFAULT_FREQ) == "daily" else FREQS.get(freq, freq)
        return (f"{wtag}·" if wtag else "") + "选股命中·" + (pool_name or "选股策略已删除")
    line = line or ""
    freq = (freq or DEFAULT_FREQ) or DEFAULT_FREQ
    target = LINES[line][0] if line in LINES else (
        "—" if value is None else f"{float(value):g}")
    tag = "" if freq == "daily" else FREQS.get(freq, freq)
    return (f"{tag}·" if tag else "") + f"{OPS.get(op, op)}{target}"


def list_alerts(ts_code: str | None = None) -> list[dict]:
    # `pool_strategy` 一起 LEFT JOIN 出来：胶囊上要写**策略名**（写 `#3` 等于没说），
    # 而名字只存在那张表里。池子被删时名字为 NULL → `_alert_text` 走「已删除」那一支。
    base = ("SELECT a.*, p.name AS pool_name FROM watch_alert a "
            "LEFT JOIN pool_strategy p ON p.id=a.pool_id ")
    if ts_code:
        rows = db.query(base + "WHERE a.ts_code=%s ORDER BY a.id", (ts_code,))
    else:
        rows = db.query(base + "ORDER BY a.ts_code, a.id")
    out = []
    for r in rows:
        line = r["line"] or ""
        freq = (r.get("freq") or DEFAULT_FREQ) or DEFAULT_FREQ
        kind = (r.get("kind") or DEFAULT_KIND) or DEFAULT_KIND
        pname = r.get("pool_name")
        is_pool = kind == "pool"
        out.append({
            "id": int(r["id"]), "ts_code": r["ts_code"], "line": line,
            "kind": kind, "kind_label": KINDS.get(kind, kind),
            "pool_id": None if r.get("pool_id") is None else int(r["pool_id"]),
            "pool_name": pname or "",
            "freq": freq, "freq_label": FREQS.get(freq, freq),
            # pool 档的 line 是空的，硬按「绝对价位」回显会写出「上穿—」这种东西；
            # 这里统一改成策略名，前端铺格子时按 kind 分岔即可。
            "line_label": (pname or "选股策略") if is_pool
                          else (LINES[line][0] if line in LINES else "绝对价位"),
            "value": None if r["value"] is None else float(r["value"]),
            "op": r["op"], "op_label": "选股命中" if is_pool else OPS.get(r["op"], r["op"]),
            "text": _alert_text(line, r["op"], r["value"], freq, kind, pname),
            "note": r["note"] or "", "enabled": int(r["enabled"] or 0),
        })
    return out


def delete_alert(aid: int) -> dict:
    """删**一条提醒规则**，连带它的全部触发记录一起删（`watch_alert_log.alert_id`）。

    这是「删规则」的**唯一实现** —— 自选股弹窗里那个「删」和关注页**触发提醒列表**里
    那个「删」（2026-10-08 需求：「触发提醒列表添加删除提醒字段，只删除触发提醒的
    提醒」）都走它。两处各写一套，迟早出现「一处只删记录、一处删整条规则」的分裂。
    ⚠ 「只删提醒」= 只动 `watch_alert` + 它的 log，**不碰自选股**（`watch_stock`）、
      不碰同票的其他规则 —— 一只票可能在好几条规则里各命中过，删这条不该带走那几条。
    ⚠ 要报「连带删了几条记录」只能**自己先数**：`db.execute` 返的是 affected rows，
      不能当行数用（硬规矩 4）—— 要展示「删了 N 条」就必须自己 `COUNT(*)`。
    ⚠ pool 档也一样：删的是**提醒规则**，**不动**它引用的选股策略（那条策略还在选股页里）。

    回 `{"deleted":bool,"id","ts_code","logs",...}`：规则已经不在时 `deleted=False`
    （`logs=0`）而不是静默成功 —— 前端要能区分「真删了」和「这条早被人删过」。
    """
    aid = int(aid)
    row = db.query_one(
        "SELECT a.ts_code, a.kind, a.pool_id, a.freq, a.line, a.op, a.value, p.name AS pool_name "
        "FROM watch_alert a LEFT JOIN pool_strategy p ON p.id=a.pool_id WHERE a.id=%s", (aid,))
    if not row:
        return {"deleted": False, "id": aid, "ts_code": None, "logs": 0, "text": ""}
    logs = int(db.scalar("SELECT COUNT(*) FROM watch_alert_log WHERE alert_id=%s", (aid,)) or 0)
    db.execute("DELETE FROM watch_alert WHERE id=%s", (aid,))
    db.execute("DELETE FROM watch_alert_log WHERE alert_id=%s", (aid,))
    freq = (row.get("freq") or DEFAULT_FREQ) or DEFAULT_FREQ
    kind = (row.get("kind") or DEFAULT_KIND) or DEFAULT_KIND
    pname = row.get("pool_name")
    is_pool = kind == "pool"
    return {"deleted": True, "id": aid, "ts_code": row["ts_code"], "logs": logs,
            "kind": kind, "kind_label": KINDS.get(kind, kind),
            "pool_id": None if row.get("pool_id") is None else int(row["pool_id"]),
            "pool_name": pname or "",
            "freq": freq, "line": row["line"] or "", "op": row["op"],
            "text": _alert_text(row["line"], row["op"], row["value"], freq, kind, pname),
            "line_label": (pname or "选股策略") if is_pool
                          else (LINES[row["line"]][0] if (row["line"] or "") in LINES else "绝对价位"),
            "op_label": "选股命中" if is_pool else OPS.get(row["op"], row["op"])}


def toggle_alert(aid: int, enabled: bool) -> None:
    db.execute("UPDATE watch_alert SET enabled=%s WHERE id=%s", (1 if enabled else 0, aid))


# ---------------------------------------------------------------------------
# 批量加规则：「日K 在布林下轨」的票一键加「触及布林下轨」提醒
# ---------------------------------------------------------------------------
# 2026-10-08 需求原话：「对日k在布林下轨的股票全部添加日k触及boll下轨提醒」。
#
# ⚠ 「在布林下轨」的判定口径**唯一来源就是下面这个阈值** —— 别在模板/回归里再写一个 0.1。
#   `boll_pos` 的定义只有 `indicators.compute` 一处（=(收盘-下轨)/(上轨-下轨)，
#   跌破下轨时为**负**、突破上轨时 >1、`min_periods` 不够或通道宽度为 0 时是 NaN）。
#   两处各写一套迟早在边界上对不上 —— 现象是「扫出来有它、可规则加不上」这种哑巴亏。
BOLL_LOWER_POS = 0.1
# 批量加出来的规则统一带这个备注（后面再拼上判定日），方便日后在提醒弹窗里
# 一眼认出哪些是这一批塞的、要撤一起撤。
BOLL_LOWER_NOTE = "布林下轨批量"


def boll_lower_hit(df: pd.DataFrame, i: int | None = None) -> bool:
    """第 `i` 根（默认**最新**那根）算不算「日K 在布林下轨」。

    **纯函数**，只看 `boll_pos` 列、不碰库 —— 回归里拿合成数据直接喂它验边界，
    不必依赖「今天恰好有票落在下轨」（那种断言换个交易日就假红）。
    """
    if df is None or not len(df):
        return False
    idx = len(df) - 1 if i is None else int(i)
    if idx < 0 or idx >= len(df):
        return False
    row = df.iloc[idx]
    if "boll_pos" not in df.columns:
        return False
    pos = row.get("boll_pos")
    if pos is None:
        return False
    try:
        pos = float(pos)
    except (TypeError, ValueError):
        return False
    if pos != pos:              # NaN：不足 20 根 / 通道宽度为 0 ⇒ 不判命中（不是「算 0」）
        return False
    return pos <= BOLL_LOWER_POS


def scan_boll_lower(codes: list[str] | None = None, end: str | None = None) -> dict:
    """扫一批票，挑出「日K 在布林下轨」的。**只读，不写库。**

    `codes` 不给 = **全部自选股**（跨组去重 —— 同一只票在几个组里都只算一次）。
    `end` 不给 = 本库最新交易日（与提醒检测 / 自选股表上那两列**同一个锚**）。
    返回 `{date, scanned, matched, codes, pos}`，`pos` 是每只的布林位置（给页面看一眼）。
    """
    latest = end or market.latest_trade_date()
    if codes is None:
        codes = [r["ts_code"] for r in db.query(
            "SELECT DISTINCT ts_code FROM watch_stock ORDER BY ts_code")]
    codes = list(dict.fromkeys([c for c in codes if c]))        # 去重、保序
    out = {"date": latest, "scanned": len(codes), "matched": 0, "codes": [], "pos": {}}
    if not codes:
        return out
    start = _lookback_start(latest, unit="D")
    # `only=NEED_FIELDS` 里含 `boll_lower` ⇒ indicators 的 `want_pre("boll")` 为真，
    #   **整块 boll 一起算**（`boll_pos` 随之出来，不会被裁掉）—— 别为此再单独传字段。
    panel = market.load_panel(codes, start, latest, freq="daily", adjust="qfq",
                              with_basic=False, weekly=False, only=NEED_FIELDS)
    for c in codes:
        df = panel.get(c)
        if df is None or len(df) < 2 or "boll_pos" not in df.columns:
            continue
        pos = df["boll_pos"].iloc[-1]
        if boll_lower_hit(df):
            out["codes"].append(c)
            out["pos"][c] = round(float(pos), 4)
    out["matched"] = len(out["codes"])
    return out


def add_boll_lower_alerts(codes: list[str] | None = None, note: str | None = None,
                          dry_run: bool = False) -> dict:
    """给「日K 在布林下轨」的票批量加「日K 触及布林下轨」规则。

    ⚠ **幂等**：某只票已经有 `(日K, boll_lower, touch)` 这条就跳过（记在 `skipped`），
      连点两下按钮不会堆出一串一样的规则；被用户删掉过的会**重新**加上。
    ⚠ 「已有」**不看启用状态**：暂停着的那条也算已有 —— 用户自己按的暂停说明他见过这条，
      再悄悄塞一条启用版等于替他撤销决定（C51 那套「别绕过用户已有的选择」同一个道理）。
    ⚠ `dry_run=True` **只算不写**（页面先拿它去弹「将新增 N 条」的确认框），
      `pending` 才是这次真会写进去的条数，`added` 在 dry_run 下恒 0。
    """
    scan = scan_boll_lower(codes)
    want = scan["codes"]
    have = {r["ts_code"] for r in db.query(
        "SELECT ts_code FROM watch_alert WHERE freq=%s AND line=%s AND op=%s",
        (DEFAULT_FREQ, "boll_lower", "touch"))}
    todo = [c for c in want if c not in have]
    nt = (BOLL_LOWER_NOTE if note is None else note) or ""
    nt = (f"{nt} {scan['date']}".strip())[:200]
    added = 0
    if todo and not dry_run:
        rows = [(c, DEFAULT_FREQ, "boll_lower", None, "touch", nt, 1) for c in todo]
        # ⚠ `insert_many` 返的是 affected rows（≠ 行数）⇒ 要报数只能自己数 `len(rows)`
        #   （硬规矩 4）。这里压根不用它的返回值。
        db.insert_many("INSERT INTO watch_alert (ts_code, freq, line, value, op, note, enabled) "
                       "VALUES (%s,%s,%s,%s,%s,%s,%s)", rows)
        added = len(rows)
    _log_w.info("布林下轨批量：扫 %d 只 / 命中 %d 只 / %s %d 条 / 跳过 %d 条",
                scan["scanned"], scan["matched"], "预演" if dry_run else "新增",
                len(todo) if dry_run else added, scan["matched"] - len(todo))
    return {"date": scan["date"], "scanned": scan["scanned"], "matched": scan["matched"],
            "pending": len(todo), "added": added, "skipped": scan["matched"] - len(todo),
            "dry_run": bool(dry_run), "codes": want, "pos": scan["pos"], "note": nt}


# ---------------------------------------------------------------------------
# 选股命中（kind='pool'）：绑定一条选股策略，入池即提醒
# ---------------------------------------------------------------------------
# 2026-10-10 需求原话：「我的关注里面的提醒，希望可以通过绑定选股策略规则，如果选股命中则提醒」。
#
# ⚠ 这一段**只做「取一批自选股 + 一条选股策略 → 今天谁在池里」这一件事**，
#   背后全是选股那条路已经有的函数（`strategy.resolve_pool/compute_pool/pool_filters`、
#   `market.apply_pool_filters/resolve_universe/load_panel`）。别在这里另写一套表达式求值 ——
#   那样「提醒说命中了、选股页说没这只」只是时间问题（口径分家，两边都查不出错）。
# ⚠ 与选股页唯一的口径差异是**范围**：选股是「全市场（或 base）里挑出命中的」，
#   这里是「我关注的这几只里，哪些命中」。条件、过滤、复权、数据源三样完全一样。
POOL_LOOKBACK_BARS = 300
"""选股命中的取数回溯根数。⚠ 必须与选股页同量级（`screener` 日线用 300）——
按 160 根取的话 MA60 / MACD 那些条件的预热期不够，**条件和选股页一样却算不出同样的结果**。"""


def _pool_msg(pool_name: str, close: float, freq: str = DEFAULT_FREQ) -> str:
    """选股命中的那条人话。写成「收盘 12.34 命中选股策略「首板」」——
    「命中」两个字必须出现：列表里 price 档那几条都是「上穿 / 触及」开头，
    不说「命中」的话让人以为这也是一条线价规则。
    ⚠ 周K 加「周K 」前缀（2026-10-10）—— 与 price 档周K 的 `_msg` 同一个写法，
      触发记录里一眼能看出这条是「一周一次」的那种。日K 不加，保持老文案。
    """
    pre = "" if (freq or DEFAULT_FREQ) == "daily" else "周K "
    return f"{pre}收盘 {close:.2f} 命中选股策略「{pool_name or '已删除'}」"


def scan_pool(pid: int, codes: list[str] | None = None, end: str | None = None,
              lookback: int = 1, freq: str | None = None) -> dict:
    """按一条**选股策略**扫一批票，挑出「在 `end` 那根 K 线上入池」的。**只读，不写库。**

    `codes` 不给 = **全部自选股**（跨组去重）；`end` 不给 = 本库最新交易日；
    `freq` = 提醒自己选的判定节奏（`daily` / `weekly`，不给按日K）。

    返回 `{pool_id,pool_name,pool_label,date,freq,scanned,matched,codes,hits,error}`，
    `hits[code] = {"date": 落在哪一天, "close": 那根收盘价}`（写触发记录用）。

    ⚠ 条件求值走 `strategy.compute_pool()`（**不是**自己再解释一遍表达式）；
      静态过滤走 `market.apply_pool_filters()`；最小成交额在 `compute_pool()` 里按
      `pool_filters()["min_amount"]` 处理 —— 三样都与选股页同一处实现。
    ⚠ `base` 也要认：`base='000300.SH'` 的池子，自选股里不在沪深300 的那几只**不该命中**
      （选股页是先解析 base 再筛条件的，少这一步等于把它悄悄当成「全市场」池子）。
    ⚠ 池子已删 / `pool_id` 为空 ⇒ 回 `error`，**绝不**退化成「空条件 = 全命中」——
      那会让所有挂着这条的自选股当天一起响，且看不出是错的。

    ## 周期（2026-10-10：pool 档也有周期，**两个来源别混**）
    · **面板周期**（条件在哪种 K 线上求值）= **选股策略自己的 `timeframe`**：
      `cfg = selection_cfg({"_pool": {...}})` 一步到位 —— `weekly` 策略会被抬成
      「条件去 `w_` 前缀 + `timeframe=weekly`」。`weekly` 走 `market.compute_weekly_panel`
      （`close` 是周收盘、`ma5` 是 5 周均线），`daily` 走 `market.load_panel(... freq="daily")`。
      **这是「整条链按周K跑」在提醒这头的落点** —— 少了它，绑一条周K 策略的提醒会拿
      日线口径去跑它的条件（`ma5` 算成 5 日均线）= 养出第三个口径。
    · **判定节奏**（判哪一根）= `freq`：`daily` 判 `end` 那天；`weekly` 判「最近一个
      **已收盘的完整周**」的最后一个交易日（`_pool_week_end`）。同一只票同一条策略，
      两个 `end` 的答案可能不同（周二在池、周五出池 ⇒ 周K 不算命中）。
      ⚠ 面板周期是 `weekly` 时周线本来就只有走完的整周 ⇒ 无论 `freq` 给什么都落到同一个
      `end`，实际节奏仍是 weekly。
    · 返回值 `freq` = **实际用的节奏**（`weekly` ⟺「策略是周K 或 提醒选了周K」）——
      触发文案的「周K 」前缀就认它，别在别处拿策略周期和提醒周期各判一次。
    """
    from core import pool as pool_mod          # 延迟导入：pool → universe → strategy 链路
    from core import strategy as st

    out = {"pool_id": int(pid), "pool_name": "", "pool_label": "", "date": "",
           "freq": universe.norm_freq(freq), "scanned": 0, "matched": 0,
           "codes": [], "hits": {}, "error": ""}
    p = pool_mod.get_pool(int(pid))
    if not p:
        out["error"] = f"选股策略 #{pid} 已不存在（可能已被删除）"
        return out
    out["pool_name"] = p.get("name") or ""
    out["pool_label"] = pool_mod.describe(p)["label"]

    # ⚠ 先解析策略配置 —— 它带出**策略自己的周期**（`selection_cfg` 把 weekly 策略抬成周线口径），
    #   面板走哪种 K 线就认它。解析放前面：base 也从这个 cfg 取。
    cfg = {"_pool": {"pool_id": int(pid)}}
    try:
        cfg = st.selection_cfg(cfg)
    except Exception as e:                       # noqa: BLE001  条件坏了就别硬跑
        out["error"] = f"选股策略 #{pid} 解析失败：{e}"
        return out
    ptf = cfg["timeframe"]                        # 面板周期（**策略自己的**）
    eff = "weekly" if (ptf == "weekly" or out["freq"] == "weekly") else "daily"
    out["freq"] = eff                             # 实际节奏（文案前缀用它）

    end = end or market.latest_trade_date()
    if not end:
        out["error"] = "本地无行情数据（先在「数据管理」同步）"
        return out
    if eff == "weekly":
        end = _pool_week_end(end)                 # 周线只有走完的整周可判
    out["date"] = end

    if codes is None:
        codes = [r["ts_code"] for r in db.query(
            "SELECT DISTINCT ts_code FROM watch_stock ORDER BY ts_code")]
    codes = list(dict.fromkeys([c for c in codes if c]))        # 去重、保序
    out["scanned"] = len(codes)
    if not codes:
        return out

    # 基础池在这一天的成员（`all` 不用查：全部在册股票都是它的成员）
    base_codes = None
    base = st.resolve_pool(cfg).get("base")
    if base and base != market.UNIVERSE_ALL:
        try:
            base_codes = set(market.resolve_universe(base))
        except Exception as e:                   # noqa: BLE001  指数没同步 → 退回不按 base 收窄
            _log_w.warning("选股命中：base=%s 成分取不到（%s），本次不按基础池收窄", base, e)

    only = st.required_daily_fields(cfg)
    if ptf == "weekly":
        start = _lookback_start(end, bars=POOL_LOOKBACK_BARS, unit="W")
        panel = market.compute_weekly_panel(codes, start, end,
                                            indicators_cfg=cfg.get("indicators"), only=only)
    else:
        wf = st.required_weekly_fields(cfg)      # 池子条件里写了 w_ 字段才会有（按 asof 贴日线）
        start = _lookback_start(end, bars=POOL_LOOKBACK_BARS, unit="D")
        panel = market.load_panel(codes, start, end, freq="daily", adjust="qfq",
                                  weekly=bool(wf), weekly_fields=wf, only=only)
    panel = market.apply_pool_filters(panel, st.pool_filters(cfg))

    # 周线面板的 `trade_date` 是 W-FRI 标签（可能是休市日）⇒ 落库前映射成**该周最后一个交易日**。
    # 不映射的话 feed() / summary() 按真实交易日取，这条命中会从页面上凭空消失（见 `_week_last_day`）。
    wmap = _week_last_day() if ptf == "weekly" else {}

    lookback = max(1, min(int(lookback or 1), 60))
    for code, df in panel.items():
        if df is None or len(df) < 2:
            continue
        if base_codes is not None and code not in base_codes:
            continue
        try:
            mask = st.compute_pool(df, cfg)
        except Exception as e:                   # noqa: BLE001  某只算炸了不该拖垮整轮
            _log_w.warning("选股命中：%s 按策略 #%s 求值失败：%s", code, pid, e)
            continue
        for i in range(max(1, len(df) - lookback), len(df)):
            if not bool(mask[i]):
                continue
            lab = str(df["trade_date"].iloc[i])[:10]
            out["codes"].append(code)
            out["hits"][code] = {"date": wmap.get(lab, lab),
                                 "close": float(df["close"].iloc[i])}
            break                                 # 同一只票一次扫只报最近那根
    out["matched"] = len(out["codes"])
    return out


# ---------------------------------------------------------------------------
# 触发检测
# ---------------------------------------------------------------------------

def _lookback_start(end: str, bars: int = 160, unit: str = "D") -> str:
    """往前留够算 MA60 / BOLL20 的余量。

    `bars` 的单位跟着 `unit` 走：日K 是**交易日**（乘 7/5 换成自然日），
    周K 是**周**（乘 7 换成自然日）。
    ⚠ 两者不能混：周K 只回看 160 个自然日就只有 22 根周线，MA60 全是 NaN
    ⇒ 规则**永远不响**（还查不出错，页面上看就是「设了没反应」）。
    """
    days = int(bars * (7 / 5 if unit == "D" else 7)) + 40
    return (pd.Timestamp(end) - pd.Timedelta(days=days)).strftime("%Y-%m-%d")


def _week_last_day() -> dict[str, str]:
    """周线标签（W-FRI，可能是**非交易日**的周五）→ **该周最后一个交易日**。

    ⚠ 为什么非做这一步：`weekly_kline.trade_date` 存的是 W-FRI 标签，碰上「周内最后
    一个交易日不是周五」的周（国庆 / 春节那种），标签会落在**休市日**上 —— 实测
    2026-09-28 那一周的标签是 10-02，而 10-02 是假期。可 `feed()` 的日期集合、
    `summary()` 的「今天命中几条」都是按 `daily_kline` 里**真实交易日**取的，
    直接把标签写进日志，那条命中就从这两处**凭空消失**（用户看到的就是「规则不响」）。
    拿不到映射（trade_cal 缺那段）时退回标签本身，宁可日期难看也别丢记录。
    """
    try:
        _first, last = market.week_bounds()
    except Exception:                                        # noqa: BLE001
        return {}
    if last is None or not len(last):
        return {}
    return {pd.Timestamp(k).strftime("%Y-%m-%d"): pd.Timestamp(v).strftime("%Y-%m-%d")
            for k, v in last.to_dict().items() if pd.notna(v)}


def _pool_week_end(latest: str, wmap: dict[str, str] | None = None) -> str:
    """判**哪一天**：「最近一个已收盘的完整周」的最后一个交易日。

    ⚠ 与 price 档周K **同一口径**（`market.complete_week_mask` = 「该周最后一个交易日
      ≤ 视角日(trade_cal 口径)」）—— 一周只认走完之后那天的收盘状态。
      ⚠ 这与「整周任一天入池就算命中」是两回事（那种写法会把「周二入池、周五又出池」也算响）。
    ⚠ 2026-10-10 后它有**两个触发点**，含义都是「判周K 那根」：
      · 提醒自己选了周K（`freq='weekly'`）；
      · **选股策略本身是周K**（`timeframe='weekly'`）—— 周线本来就只有走完的整周可判。
      两种情形下面板都取周线（`market.compute_weekly_panel`），`end` 都收敛到这一天。
    ⚠ 周中（周二）跑一次，拿到的是**上一个完整周**的最后交易日 —— 记录键仍是那一天，
      所以重复跑不会刷出第二条（唯一键 `(alert_id, trade_date)`），跨周才出新记录。
    ⚠ 取不到 `wmap`（trade_cal 缺）时退回 `latest` —— 宁可多判一次，也别让规则静默不响。
    """
    wm = wmap if wmap is not None else _week_last_day()
    ends = [d for d in set(wm.values()) if d and d <= latest]
    return max(ends) if ends else latest


def _hit(df: pd.DataFrame, i: int, line: str, value, op: str) -> tuple[bool, float, float]:
    """判第 i 根是否命中。返回 `(命中, 触发价, 线的值)`。**只用 i-1 与 i 两根。**"""
    close = df["close"]
    if line:
        col = LINES[line][1]
        if col not in df.columns:
            return False, 0.0, 0.0
        lv = df[col]
        cur, prev = lv.iloc[i], lv.iloc[i - 1]
    else:
        lv = None
        cur = prev = float(value)
    if pd.isna(cur) or pd.isna(prev):
        return False, 0.0, 0.0
    c, cp = float(close.iloc[i]), float(close.iloc[i - 1])
    tgt = float(cur)
    if op == "cross_up":
        return (cp <= float(prev) and c > tgt), c, tgt
    if op == "cross_down":
        return (cp >= float(prev) and c < tgt), c, tgt
    # touch：盘中最低价与最高价之间碰到线（线是数值时同理）
    lo, hi = float(df["low"].iloc[i]), float(df["high"].iloc[i])
    return (lo <= tgt <= hi), c, tgt


def _msg(name: str, line: str, op: str, c: float, tgt: float, bar: dict,
         freq: str = DEFAULT_FREQ) -> str:
    label = LINES[line][0] if line in LINES else "价位"
    if op == "touch":
        if freq == "weekly":
            # 周K 的 low/high 是**整周**的极值，再写「盘中」就跟「当周」自相矛盾了。
            return (f"周K 当周触及{label} {tgt:.2f}（{bar['low']:.2f}~{bar['high']:.2f}，"
                    f"收 {c:.2f}）")
        return (f"盘中触及{label} {tgt:.2f}（当日 {bar['low']:.2f}~{bar['high']:.2f}，"
                f"收 {c:.2f}）")
    pre = "" if freq == "daily" else "周K "          # 记录里一眼看出是哪种周期的命中
    return f"{pre}收盘 {c:.2f} {OPS[op]}{label} {tgt:.2f}"


def check(lookback: int = 1, jid: str | None = None) -> dict:
    """跑一次检测。命中的写 `watch_alert_log`（同一规则同一天只记一条）。

    ⚠ 日K 与周K 是**两份面板、两次取数**（周K 走 `weekly_kline`）：日K 判「最近这几根
    日线」，周K 判「最近这几个**已收盘的完整周**」。`lookback` 对两者都生效，单位跟着
    各自的图走（日K = 交易日 / 周K = 周）。没有周K 规则时**不会多花这次取数**。

    ⚠ **两档规则分两段跑**（2026-10-10 加「选股命中」）：`price` 档走下面按 freq 分组的
    那一段；`pool` 档走 `scan_pool()`（按**选股策略**判，与选股页同一套函数）。
      两段的取值字段、判据、数据源全不一样，混在一个循环里迟早互相污染
      （最典型：pool 行的 `line=''` 被当成「绝对价位」去跟 `value=None` 比 → 抛异常或恒不命中）。
      `lookback` 两段都生效：pool 档是「最近 lookback 根 K 线里任意一根在池内」。
    ⚠ pool 档的**面板周期**（日K / 周K）由**选股策略自己的 `timeframe`** 决定，在这一层
      不判、不猜（连同 `end` 一起交给 `scan_pool`）；这里只把**提醒自己选的 `freq`** 传下去。
    """
    alerts = db.query("SELECT * FROM watch_alert WHERE enabled=1 ORDER BY ts_code, id")
    if not alerts:
        return {"codes": 0, "alerts": 0, "fired": 0, "daily_fired": 0, "weekly_fired": 0,
                "weekly_alerts": 0, "pool_alerts": 0, "pool_fired": 0,
                "skipped": "没有启用的提醒规则",
                "trade_date": market.latest_trade_date(), "items": []}
    latest = market.latest_trade_date()
    lookback = max(1, min(int(lookback or 1), 60))
    names = {r["ts_code"]: r["name"] for r in db.query(
        "SELECT ts_code, name FROM stock_basic")}
    wmap = _week_last_day()
    # ⚠ 按 `kind` 分档（老行没这列时当 `price`）—— 分档**只能在这一处**，
    #   下面的循环与 scan_pool 都别再各判一次。
    price_alerts = [a for a in alerts if (a.get("kind") or DEFAULT_KIND) != "pool"]
    pool_alerts = [a for a in alerts if (a.get("kind") or DEFAULT_KIND) == "pool"]
    total_codes = len({a["ts_code"] for a in alerts})
    fired, no_data, stat, done = 0, [], {}, 0

    for freq in FREQ_ORDER:
        group = [a for a in price_alerts if (a.get("freq") or DEFAULT_FREQ) == freq]
        if not group:
            continue
        by_code: dict[str, list[dict]] = {}
        for a in group:
            by_code.setdefault(a["ts_code"], []).append(a)
        codes = list(by_code)
        start = _lookback_start(latest, unit="W" if freq == "weekly" else "D")
        panel = market.load_panel(codes, start, latest, freq=freq, adjust="qfq",
                                  with_basic=False, weekly=False, only=NEED_FIELDS)
        f_fired = 0
        for k, code in enumerate(codes, 1):
            df = panel.get(code)
            if df is not None and len(df) and freq == "weekly":
                # 只认「已收盘的完整周」—— 与 sync 写库 / 回测**同一处判据**（market.complete_week_mask）。
                # 表里本来只存完整周，这一步是兜底：判据永远只有一处。
                keep = market.complete_week_mask(df["trade_date"], latest)
                df = df[keep.to_numpy()]
            if df is None or len(df) < 2:
                no_data.append(code)
                continue
            idxs = range(max(1, len(df) - lookback), len(df))
            for a in by_code[code]:
                for i in idxs:
                    ok, c, tgt = _hit(df, i, a["line"], a["value"], a["op"])
                    if not ok:
                        continue
                    bar = {"low": float(df["low"].iloc[i]), "high": float(df["high"].iloc[i])}
                    lab = str(df["trade_date"].iloc[i])[:10]
                    # ⚠ 周K 按**该周最后一个交易日**落库（标签本身可能是个休市日，
                    #   直接写标签会从 feed() 与 summary() 里消失，见 _week_last_day）。
                    td = wmap.get(lab, lab) if freq == "weekly" else lab
                    msg = _msg(names.get(code, ""), a["line"], a["op"], c, tgt, bar, freq)
                    db.execute(
                        "INSERT INTO watch_alert_log (alert_id, ts_code, trade_date, message, "
                        "ts_value, line_value) VALUES (%s,%s,%s,%s,%s,%s) "
                        "ON DUPLICATE KEY UPDATE message=VALUES(message), ts_value=VALUES(ts_value), "
                        " line_value=VALUES(line_value)",
                        (a["id"], code, td, msg[:255], round(c, 4), round(tgt, 4)))
                    fired += 1
                    f_fired += 1
                    break                   # 一条规则一次检测只报一次（取最近那根）
            done += 1
            if jid and done % 20 == 0:
                jobs.update(jid, progress=done / max(total_codes, 1),
                            log=f"检测 {done}/{total_codes} 只")
        stat[freq] = {"codes": len(codes), "alerts": len(group), "fired": f_fired}

    # ---- 选股命中（kind='pool'）：按**（策略, 周期）**分组，一组扫一次 --------
    # ⚠ 一条策略可能被好几只自选股各挂一条 ⇒ 合并成一次扫描（`codes` 去重），
    #   否则 100 只票各扫一遍 = 100 次取数。同一个 (pid, freq) 的命中再按 ts_code 展开回各自规则。
    # ⚠ **提醒的周期也必须进分组键**（2026-10-10 加 pool 档周K）：日K 判 `latest` 那根、
    #   周K 判「最近一个已收盘的完整周」的最后一个交易日 —— 两种 `end` 不是同一天，
    #   混在一组里会拿日K 的结论去填周K 的规则（日期与收盘价都是错的）。
    # ⚠ 分组键里的 `freq` 只是**提醒自己选的节奏**；**面板周期由选股策略自己的 `timeframe`
    #   决定**（在 `scan_pool` 里一步到位，别在这儿再判一次 —— 判两次就是第三个口径）。
    by_key: dict[tuple[int, str], list[dict]] = {}
    orphan = 0
    for a in pool_alerts:
        if a.get("pool_id") is None:            # 池子被删后清掉了引用（见 save_alert）
            orphan += 1
            continue
        fq = (a.get("freq") or DEFAULT_FREQ) or DEFAULT_FREQ
        by_key.setdefault((int(a["pool_id"]), fq), []).append(a)
    p_fired, p_codes, p_bad = 0, set(), []
    for group in by_key.values():
        p_codes |= {a["ts_code"] for a in group}
    p_weekly = 0
    done2 = 0
    for (pid, fq), group in by_key.items():
        codes = sorted({a["ts_code"] for a in group})
        # `end=latest` + `freq` 一起交给 `scan_pool`：它按**策略周期**选面板、按**节奏**定 `end`
        # （周K 会自己收敛到「最近一个已收盘的完整周」的最后一个交易日）。
        sc = scan_pool(pid, codes, end=latest, lookback=lookback, freq=fq)
        if sc.get("error"):
            p_bad.append(sc["error"])
            done2 += len(codes)
            continue
        eff = sc.get("freq") or DEFAULT_FREQ       # 实际节奏（策略周K 或 提醒周K ⇒ 都是 weekly）
        if eff == "weekly":
            p_weekly += len(group)                  # 「周K 那几条」按**实际节奏**统计
        by_code: dict[str, list[dict]] = {}
        for a in group:
            by_code.setdefault(a["ts_code"], []).append(a)
        for code, hit in sc["hits"].items():
            msg = _pool_msg(sc.get("pool_name") or "", hit["close"], eff)
            for a in by_code.get(code, []):
                db.execute(
                    "INSERT INTO watch_alert_log (alert_id, ts_code, trade_date, message, "
                    "ts_value, line_value) VALUES (%s,%s,%s,%s,%s,%s) "
                    "ON DUPLICATE KEY UPDATE message=VALUES(message), ts_value=VALUES(ts_value), "
                    " line_value=VALUES(line_value)",
                    (a["id"], code, hit["date"], msg[:255],
                     round(hit["close"], 4), None))
                fired += 1
                p_fired += 1
        done2 += len(codes)
        if jid:
            jobs.update(jid, log=f"选股命中检测 {done2}/{len(p_codes)} 只")
    stat["pool"] = {"codes": len(p_codes), "alerts": len(pool_alerts), "fired": p_fired,
                    "weekly_alerts": p_weekly}

    if not price_alerts and pool_alerts:
        # 只有选股命中规则时把进度推到 100%（价格那段一次都没跑，`done` 恒 0）
        if jid:
            jobs.update(jid, progress=1.0)
    res = {"codes": total_codes, "alerts": len(alerts), "fired": fired,
           "daily_fired": (stat.get("daily") or {}).get("fired", 0),
           "weekly_alerts": (stat.get("weekly") or {}).get("alerts", 0),
           "weekly_fired": (stat.get("weekly") or {}).get("fired", 0),
           # 选股命中那一档的计数单独报（前端文案要分开写「日K x 条 / 周K y 条 / 选股命中 z 条」）
           # ⚠ `pool_weekly_alerts` = 其中选了**周K** 的那几条（2026-10-10 起 pool 档也有周期）
           "pool_alerts": len(pool_alerts), "pool_fired": p_fired,
           "pool_weekly_alerts": p_weekly,
           # 池子失效 / 解析失败时如实报出来 —— 静默跳过会让人以为「条件没命中」
           "pool_skipped": p_bad[:10] + ([f"{orphan} 条规则没绑定选股策略"] if orphan else []),
           "trade_date": latest, "no_data": no_data[:20], "lookback": lookback,
           "items": feed(days=3)["items"]}
    if jid:
        jobs.update(jid, log=f"命中 {fired} 条（检测 {total_codes} 只）")
    _log_w.info("提醒检测：%d 只 / %d 条规则（日K %d · 周K %d · 选股命中 %d[周K %d]）/ 命中 %d",
                total_codes, len(alerts), (stat.get("daily") or {}).get("alerts", 0),
                (stat.get("weekly") or {}).get("alerts", 0), len(pool_alerts), p_weekly, fired)
    return res


# ---------------------------------------------------------------------------
# 读取（总览/关注页）
# ---------------------------------------------------------------------------

def feed(days: int = 1, limit: int = 500, group_id=None) -> dict:
    """最近的触发记录（新的在前）。`days` 按**最近 N 个交易日**算，不是自然日。

    ⚠ 2026-10-08 起默认 **1 个交易日**（原来是 7）：关注页那个「触发提醒」列表的默认区间
      改成了「最近 1 日」（需求原话），接口默认值必须跟着走 —— 两边各写一个默认值，
      迟早在「页面显示最近 1 天的、接口偷偷给 7 天的」这种事情上对不上。

    ⚠ 2026-10-09 起 `limit` 默认 **500**（原 100）：加了 limit 参数那天只把「可配」做完了，
      默认还留着 100 —— 实测当天命中 **108** 条，不传参就**静默少给 8 条**，页面看着
      「就这么几条」，看不出后头的被吃了（与 2026-10-08 那个 `LIMIT 100` 坑同源）。
      默认给到 500（页面也传 500），上限仍是 2000 兜底。

    ⚠ `group_id`（2026-10-08 加）= 只看**这个分组里的票**的触发记录；不传 = 不过滤（全部）。
      过滤**必须放在 SQL 里**，不能让调用方拿回去自己筛：`limit` 是在这一层截的，
      别的组把窗口占满之后，前端再筛就会把本组那几条**静默筛没**
      （明明有命中，页面显示「暂无触发」）—— 跟 `LIMIT 100` 那次是同一个坑。
      判据是「**这只票在这个组里**」（`watch_stock`）：一只票同时属于 A、B 两组时，
      它在 A 组和 B 组的列表里都会出现 —— 这是对的，它就是这一组的股票。

    ⚠ `stock_note`（2026-10-11 加）= 这只票在自选股里写的**文本标记**（`watch_stock.note`，
      题材/成本那类长文本），给总览卡多铺一列用。⚠⚠ 它和 `note` **不是一回事**：
      `note` = **这条提醒规则**的备注（`watch_alert.note`）；`stock_note` = **这只票**的标记。
      取数**不能 `LEFT JOIN watch_stock`** —— 一只票在 N 个组里会把一条触发记录**裂成 N 行**
      （列表是触发记录，一票多组不该多出记录），所以单独查一次收成映射再回填。
      一票多组的 note 各存一份（按组），于是：**带了 `group_id` 就只认那个组那份**；
      不带（总览卡就是这样）按 `group_id` 升序取第一条非空 —— 同一只票在不同组写了不同标记时
      总览只显示其中一条，是**有损**的，要看全得进「我的关注」页。
    """
    gid = int(group_id) if group_id else None
    ds = [str(x["trade_date"])[:10] for x in db.query(
        "SELECT DISTINCT trade_date FROM daily_kline ORDER BY trade_date DESC LIMIT %s",
        (max(1, days),))]
    if not ds:
        return {"days": [], "items": [], "count": 0, "today": market.latest_trade_date(),
                "group_id": gid}
    ph = ",".join(["%s"] * len(ds))
    # 只看某个分组：`EXISTS` 而不是 JOIN —— `watch_stock` 有 `uk_ws(group_id, ts_code)`，
    #   本来也只会连出一行，但 JOIN 让人以为「这票在几个组里就出几行」，而这里要的是
    #   「命中行是不是属于这一组」这个布尔判断，`EXISTS` 写的正是这个意思。
    args: list = list(ds)
    cond = ""
    if gid:
        cond = ("AND EXISTS (SELECT 1 FROM watch_stock ws "
                "WHERE ws.ts_code=l.ts_code AND ws.group_id=%s) ")
        args.append(gid)
    rows = db.query(
        f"SELECT l.*, a.line, a.value, a.op, a.freq, a.note, a.kind, a.pool_id, "
        f"       p.name AS pool_name, sb.name AS sname "
        f"  FROM watch_alert_log l "
        f"  LEFT JOIN watch_alert a ON a.id=l.alert_id "
        f"  LEFT JOIN pool_strategy p ON p.id=a.pool_id "
        f"  LEFT JOIN stock_basic sb ON sb.ts_code=l.ts_code "
        f" WHERE l.trade_date IN ({ph}) {cond}"
        f" ORDER BY l.trade_date DESC, l.id DESC LIMIT {int(limit)}", args)
    # 涨跌幅：取**该行那一天**（trade_date）的日涨跌幅，与自选股表「涨跌幅」列**同源**
    #   （`daily_kline.pct_chg`）。
    #   ⚠ 不能拿「最新交易日」那一列去填所有行：列表里的行跨若干天，把今天的涨跌幅贴到
    #     3 天前那条命中上，是两个日期的数拼在一起（「口径唯一」那条规矩）。
    #   ⚠ 周K 命中的 trade_date 已经是**该周最后一个交易日**（见 check 里的 `_week_last_day`），
    #     所以这天也有日线，取到的是那天的日涨跌幅 —— 不是整周的涨跌幅（页面文案写明了）。
    codes = sorted({r["ts_code"] for r in rows})
    quotes: dict = {}
    snote: dict = {}
    if codes:
        phd = ",".join(["%s"] * len(ds))
        phc = ",".join(["%s"] * len(codes))
        for q in db.query(
                f"SELECT trade_date, ts_code, pct_chg FROM daily_kline "
                f"WHERE trade_date IN ({phd}) AND ts_code IN ({phc})", ds + codes):
            quotes[(str(q["trade_date"])[:10], q["ts_code"])] = q["pct_chg"]
        # 这只票的**文本标记**（`watch_stock.note`）。⚠⚠ 别改成 JOIN —— 见上面 docstring：
        #   一只票在 N 个组里会把这些触发记录裂成 N 行。这里收成 `ts_code → note` 回填。
        sql_sn = ("SELECT ts_code, note FROM watch_stock "
                  f"WHERE note IS NOT NULL AND note<>'' AND ts_code IN ({phc})")
        args_sn: list = list(codes)
        if gid:
            sql_sn += " AND group_id=%s"
            args_sn.append(gid)
        for s in db.query(sql_sn + " ORDER BY group_id", args_sn):
            snote.setdefault(s["ts_code"], s["note"])
    out = []
    for r in rows:
        line = r["line"] or ""
        # ⚠ `kind='pool'` 的行里 `line` 是空的，硬按「绝对价位」回显会写出「上穿—」这种东西
        #   （前端那颗胶囊拼的是 `op_label + line_label`）。这一档统一改成「选股命中·策略名」，
        #   并把 `text` 也一并给出去 —— 前端优先用它，省得两处各拼一份。
        kind = (r.get("kind") or DEFAULT_KIND) or DEFAULT_KIND
        pname = r.get("pool_name")
        is_pool = kind == "pool"
        label = ((pname or "选股策略") if is_pool
                 else (LINES[line][0] if line in LINES else "绝对价位"))
        freq = (r.get("freq") or DEFAULT_FREQ) or DEFAULT_FREQ
        td = str(r["trade_date"])[:10]
        pct = quotes.get((td, r["ts_code"]))
        try:
            pct = None if pct is None else float(pct)
        except (TypeError, ValueError):
            pct = None
        if pct is not None and pct != pct:      # 库里 NaN（首日/停牌）当「没有」，别渲染成 "nan%"
            pct = None
        out.append({
            "id": int(r["id"]), "alert_id": int(r["alert_id"]), "ts_code": r["ts_code"],
            "name": r["sname"] or "", "trade_date": td,
            "message": r["message"] or "",
            "kind": kind, "kind_label": KINDS.get(kind, kind),
            "pool_id": None if r.get("pool_id") is None else int(r["pool_id"]),
            "pool_name": pname or "",
            "op": r["op"] or "", "op_label": ("选股命中" if is_pool
                                              else OPS.get(r["op"] or "", r["op"] or "")),
            # 周K 的命中点「K线」按钮要走周线图（前端拿它当 freq 参数），所以这里必须带出来。
            "freq": freq, "freq_label": FREQS.get(freq, freq),
            "line": line, "line_label": label,
            # 胶囊上的字：唯一来源仍是 `_alert_text`（与自选股弹窗里那颗同一份口径）。
            "text": _alert_text(line, r["op"], r["value"], freq, kind, pname),
            "ts_value": None if r["ts_value"] is None else float(r["ts_value"]),
            "line_value": None if r["line_value"] is None else float(r["line_value"]),
            "note": r["note"] or "",
            # 这只票在自选股里写的**文本标记**（与上面 `note` 不是一回事，见 docstring）。
            # 前端只铺截断后的十几字，全文挂 title —— 这列动辄几百字。
            "stock_note": snote.get(r["ts_code"], ""),
            # 涨跌幅三件套与自选股表**同一个形状**（`pct_text` / `pct_cls`）——
            #   前端照同一套渲染逻辑铺，页面自己不判正负（涨红跌绿由 `pct_cls` 定）。
            "pct_chg": pct,
            "pct_text": "—" if pct is None else f"{pct:+.2f}%",
            "pct_cls": "" if pct is None else ("up" if pct > 0 else ("down" if pct < 0 else "flat")),
        })
    return {"days": ds, "items": out, "count": len(out), "today": ds[0],
            # 回带实际生效的分组（`None` = 全部）：前端与回归都拿它核对
            #   「这次请求到底按哪个组筛的」，免得两边各记一份、悄悄不一致。
            "group_id": gid}


def summary() -> dict:
    """侧栏角标 / 总览小字用：最新交易日命中几条。"""
    today = market.latest_trade_date()
    fired = db.scalar("SELECT COUNT(*) FROM watch_alert_log WHERE trade_date=%s", (today,)) or 0
    groups = db.scalar("SELECT COUNT(*) FROM watch_group") or 0
    stocks = db.scalar("SELECT COUNT(DISTINCT ts_code) FROM watch_stock") or 0
    alerts = db.scalar("SELECT COUNT(*) FROM watch_alert WHERE enabled=1") or 0
    last = db.query_one("SELECT MAX(created_at) t FROM watch_alert_log") or {}
    return {"groups": int(groups), "stocks": int(stocks), "alerts": int(alerts),
            "fired_today": int(fired), "trade_date": today,
            "last_check": str(last.get("t"))[:19] if last.get("t") else None}
