# -*- coding: utf-8 -*-
"""东财「新概念速览」—— 新增概念 + 概念板块成分变更，本地缓存。

来源页面：https://emdata.eastmoney.com/appdc/gnsl/index.html?appfenxiang=1#/
（移动端 H5，纯前端渲染，`#app` 是空的 —— 直接抓 HTML 拿不到东西，必须打它背后的报表接口）

**页面背后的真实接口只有两条**（`GET datacenter.eastmoney.com/securities/api/data/v1/get`，
公开、无鉴权、**不耗 tushare 额度**、不进 `sync_log`，与 `core/hot.py`、`core/concept.py` 同性质）：

| 报表名 | 内容 | 实测规模 |
|---|---|---|
| `RPT_CUSTOM_BOARD_NEWCONCEPT` | **新概念清单**：概念名 / 创建时间 / 简介原文 / 3 只代表股 | 35 条（`pageSize=500` 也是 35，说明就这么些） |
| `RPT_CUSTOM_BOARD_EMBOARD_DYNAMIC` | **概念板块成分变更流水**：按 `CHANGE_DATE` 筛，含「新增/剔除」方向与入选理由 | 交易日 9~75 条/天；非交易日返回 `code=9201 返回数据为空` |

⚠ **三个「概念」口径不要混**（页面文案里也分开写）：
1. `core/concept.py` 的 `stock_concept` —— **个股视角**（F10 核心题材，多对多，票 → 概念）；
2. 本模块的 `em_newconcept` —— **板块视角**（这个概念是新的：什么时候建的、讲什么、代表股）；
3. 本模块的 `em_concept_event` —— **变更流水**（哪天把哪只票加进了哪个概念、理由是什么）。

⚠ 报告①给的 `CHANGE_RATE_FIRST/SECOND/THIRD` 是**代表股在概念创建当日**的涨跌幅
（光纤概念那条是 2026-04-03 的 +20%，早就是历史了），拿它当「现在的涨幅」会闹笑话 ——
所以展示时的涨跌幅一律**另取本库最近交易日**（`live_pct`），东财原值只留档。

⚠ **「新概念」清单的展示窗口 = 创建未满 365 天**（2026-10-11 加）：满一年的不再叫「新概念」，
`overview()` 里整条不摆（判据唯一见常量 `MAX_AGE_DAYS`）。清单条数、徽标、note 全按
**过滤后**的走，被剔掉的条数在 `note` / `new_hidden` 里说明；`coverage()` 报的仍是库里全部条数。

缓存策略（2026-10-08 改）：**不缓存了** —— 总览卡片**每次刷新都真去东财抓一遍**
（`overview()` 里先 `sync()`）。`app_setting['em_newconcept']` 那一行降级成「上次抓取
时间 + 失败时的退路」，不再拿它做「今日已抓过就跳过」的短路。
两条硬化措施：
- ⚠ 抓失败时**不写状态**，而且 `overview()` 会**退回上一次的快照**（`stale=True` +
  `error` 一起带给前端）—— 卡片不该因为东财抖一下就直接变空；
- ⚠ 两个请求同时打进来时用一把内存锁串起来（**这不是缓存、不省请求**），
  只是别让两个标签页刷新时把同一个接口同时打两遍。
"""
from __future__ import annotations

import datetime as dt
import re
import threading

import requests

from core import db, log as _log
from core import market

_log_nc = _log.get("newconcept")

# 抓取串行锁：同一时刻只让一个请求去打东财。后来者**等**它（拿到的是同一批网络结果，
# 但各自的 `sync()` 会照常跑一遍落库，幂等）。
# ⚠ 这不是缓存 —— 它不减少请求数，只是避免两个标签页同时刷新时把接口打两遍。
_LOCK = threading.Lock()

API = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
RPT_NEW = "RPT_CUSTOM_BOARD_NEWCONCEPT"
RPT_EVENT = "RPT_CUSTOM_BOARD_EMBOARD_DYNAMIC"

TIMEOUT = 15
SETTING_KEY = "em_newconcept"

# 「最近动态」往回找几个交易日（今天是假期时得往前翻，找到第一份有数据的日子）
EVENT_LOOKBACK = 8

# ⚠ 「新概念」只摆**创建未满一年**的（2026-10-11 用户要求「超过 365 天的概念不展示」）。
#   判据唯一在这一处：`overview()` 里 `age_days > MAX_AGE_DAYS` 就整条不摆；**日期缺失
#   （`age_days is None`）的保留** —— 算不出年龄 ≠ 已经超期，别把不确定的一起砍掉。
#   窗口**只作用于「新概念」清单**；变更流水（`em_concept_event`）是逐日流水、天然近期，
#   不受它影响。`coverage()` 报的仍是库里**全部**条数（那是数据面，不是展示面）。
MAX_AGE_DAYS = 365

_HDR = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Referer": "https://emdata.eastmoney.com/",
    "Accept": "application/json, text/plain, */*",
}

_EVENT_COLS = ("BOARD_NAME,BOARD_CODE,CHANGE_DATE,CHANGE_TIME,CHANGE_RATE,CHANGE_NUM,"
               "SECURITY_CODE,SECURITY_NAME,SECURITY_CHANGE_RATE,CHANGE_DIRECTION,"
               "ORG_PROFILE,SECUCODE,MARKET_CODE")

# 变更方向的展示名（东财原值就是中文「新增」/「剔除」，这里只做兜底归一）
DIRECTION = {"新增": "新增", "剔除": "剔除", "调入": "新增", "调出": "剔除"}


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _num(v):
    if v is None or v == "" or v == "--":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _txt(v) -> str:
    return (str(v).strip() if v is not None else "")


def _ts_code(secucode, code) -> str:
    """东财 SECUCODE（`600000.SH`）优先；没有就按代码首位猜市场（与 `hot.ts_code_of` 同规则）。"""
    s = _txt(secucode).upper()
    if re.match(r"^\d{6}\.(SH|SZ|BJ)$", s):
        return s
    c = re.sub(r"\D", "", _txt(code))
    if len(c) != 6:
        return ""
    mk = "SH" if c[0] == "6" else ("SZ" if c[0] in "03" else ("BJ" if c[0] in "489" else ""))
    return f"{c}.{mk}" if mk else ""


def _err_text(e: Exception) -> str:
    if isinstance(e, requests.exceptions.Timeout):
        return f"请求超时（{TIMEOUT}s）"
    if isinstance(e, requests.exceptions.ConnectionError):
        return "连不上东财（检查网络 / 代理 / 防火墙）"
    if isinstance(e, requests.exceptions.HTTPError):
        return f"接口返回 HTTP {getattr(e.response, 'status_code', '?')}"
    return str(e)[:120] or type(e).__name__


def _get(report: str, **params) -> dict:
    """打一条报表。返回原始 JSON；**网络/HTTP 异常往外抛**，业务空结果不抛。"""
    p = {"reportName": report, "source": "SECURITIES", "client": "APP", "v": "1"}
    p.update({k: v for k, v in params.items() if v is not None})
    r = requests.get(API, params=p, headers=_HDR, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _rows(payload: dict) -> list[dict]:
    """报表 → 行列表。`success=false`（含 `9201 返回数据为空`）算**可靠空结果**，返回 `[]`。"""
    if not payload.get("success"):
        _log_nc.info("报表返回非成功：code=%s msg=%s",
                     payload.get("code"), payload.get("message"))
        return []
    res = payload.get("result") or {}
    data = res.get("data")
    if isinstance(data, dict):          # 东财有时把 diff 包成 dict
        data = list(data.values())
    return data or []


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------

def fetch_new_concepts() -> list[dict] | None:
    """新概念清单。`None` = **这次没抓到**（调用方别动库）；`[]` = 东财说就是空的。"""
    try:
        payload = _get(RPT_NEW, pageNumber=1, pageSize=200,
                       sortColumns="START_DATE", sortTypes="-1")
    except Exception as e:                      # noqa: BLE001
        _log_nc.warning("新概念清单取数失败：%s", _err_text(e))
        return None
    if not payload.get("success") and payload.get("result") is None:
        return None
    out = []
    for it in _rows(payload):
        bc = _txt(it.get("BOARD_CODE")).upper()
        if not bc:
            continue
        start = _txt(it.get("START_DATE"))          # '2026-04-03 15:29'
        out.append({
            "board_code": bc[:16],
            "board_name": _txt(it.get("BOARD_NAME"))[:80],
            "org_board_code": _txt(it.get("ORG_BOARD_CODE"))[:16],
            "start_date": start[:19] or None,
            "start_text": _txt(it.get("START_TIME"))[:24],
            "new_date": _txt(it.get("NEW_DATE"))[:24],
            "profile": _txt(it.get("BOARD_PROFILE")),
            "c1_code": _ts_code(it.get("SECUCODE_FIRST"), it.get("SECURITY_CODE_FIRST")),
            "c1_name": _txt(it.get("SECURITY_NAME_FIRST"))[:40],
            "c1_pct": _num(it.get("CHANGE_RATE_FIRST")),
            "c2_code": _ts_code(it.get("SECUCODE_SECOND"), it.get("SECURITY_CODE_SECOND")),
            "c2_name": _txt(it.get("SECURITY_NAME_SECOND"))[:40],
            "c2_pct": _num(it.get("CHANGE_RATE_SECOND")),
            "c3_code": _ts_code(it.get("SECUCODE_THIRD"), it.get("SECURITY_CODE_THIRD")),
            "c3_name": _txt(it.get("SECURITY_NAME_THIRD"))[:40],
            "c3_pct": _num(it.get("CHANGE_RATE_THIRD")),
        })
    return out


def fetch_events(day: str) -> list[dict] | None:
    """某一天的概念板块成分变更。`None` = 没抓到（别动库）；`[]` = 那天确实没有。"""
    try:
        payload = _get(RPT_EVENT, columns=_EVENT_COLS,
                       filter=f"(CHANGE_DATE='{day}')",
                       sortColumns="CHANGE_TIME", sortTypes="-1")
    except Exception as e:                      # noqa: BLE001
        _log_nc.warning("概念板块动态取数失败（%s）：%s", day, _err_text(e))
        return None
    out = []
    for it in _rows(payload):
        bc = _txt(it.get("BOARD_CODE")).upper()
        if not bc:
            continue
        d = _txt(it.get("CHANGE_DATE"))[:10]
        direction = DIRECTION.get(_txt(it.get("CHANGE_DIRECTION")), _txt(it.get("CHANGE_DIRECTION"))[:8])
        out.append({
            "board_code": bc[:16],
            "board_name": _txt(it.get("BOARD_NAME"))[:80],
            "change_date": d,
            "change_time": _txt(it.get("CHANGE_TIME"))[:12],
            "direction": direction or "变更",
            "ts_code": _ts_code(it.get("SECUCODE"), it.get("SECURITY_CODE")),
            "code": re.sub(r"\D", "", _txt(it.get("SECURITY_CODE")))[:8],
            "name": _txt(it.get("SECURITY_NAME"))[:40],
            "pct_chg": _num(it.get("SECURITY_CHANGE_RATE")),
            "board_pct": _num(it.get("CHANGE_RATE")),
            "change_num": int(it.get("CHANGE_NUM") or 0) or None,
            "reason": _txt(it.get("ORG_PROFILE")),
        })
    return out


# ---------------------------------------------------------------------------
# 落库
# ---------------------------------------------------------------------------

def _save_new_concepts(rows: list[dict], today: str) -> int:
    """整份快照：先 upsert 再删掉这次没出现过的（清单本身会缩）。`rows` 为空时**不动库**。"""
    if not rows:
        return 0
    vals = [(r["board_code"], r["board_name"], r["org_board_code"], r["start_date"],
             r["start_text"], r["new_date"], r["profile"],
             r["c1_code"], r["c1_name"], r["c1_pct"],
             r["c2_code"], r["c2_name"], r["c2_pct"],
             r["c3_code"], r["c3_name"], r["c3_pct"], today) for r in rows]
    db.insert_many(
        "INSERT INTO em_newconcept "
        "(board_code,board_name,org_board_code,start_date,start_text,new_date,profile,"
        " c1_code,c1_name,c1_pct, c2_code,c2_name,c2_pct, c3_code,c3_name,c3_pct, fetched_at) "
        "VALUES (" + ",".join(["%s"] * 17) + ") "
        "ON DUPLICATE KEY UPDATE board_name=VALUES(board_name), "
        " org_board_code=VALUES(org_board_code), start_date=VALUES(start_date), "
        " start_text=VALUES(start_text), new_date=VALUES(new_date), profile=VALUES(profile), "
        " c1_code=VALUES(c1_code), c1_name=VALUES(c1_name), c1_pct=VALUES(c1_pct), "
        " c2_code=VALUES(c2_code), c2_name=VALUES(c2_name), c2_pct=VALUES(c2_pct), "
        " c3_code=VALUES(c3_code), c3_name=VALUES(c3_name), c3_pct=VALUES(c3_pct), "
        " fetched_at=VALUES(fetched_at)", vals)
    ph = ",".join(["%s"] * len(rows))
    db.execute(f"DELETE FROM em_newconcept WHERE board_code NOT IN ({ph})",
               [r["board_code"] for r in rows])
    return len(vals)


def _save_events(rows: list[dict], today: str) -> int:
    """变更流水：只增不改不删（历史流水多跑几次也不会重复 —— 唯一键兜着）。"""
    if not rows:
        return 0
    vals = [(r["board_code"], r["board_name"], r["change_date"], r["change_time"],
             r["direction"], r["ts_code"], r["code"], r["name"], r["pct_chg"],
             r["board_pct"], r["change_num"], r["reason"], today) for r in rows]
    db.insert_many(
        "INSERT INTO em_concept_event "
        "(board_code,board_name,change_date,change_time,direction,ts_code,code,name,"
        " pct_chg,board_pct,change_num,reason,fetched_at) "
        "VALUES (" + ",".join(["%s"] * 13) + ") "
        "ON DUPLICATE KEY UPDATE board_name=VALUES(board_name), name=VALUES(name), "
        " pct_chg=VALUES(pct_chg), board_pct=VALUES(board_pct), "
        " change_num=VALUES(change_num), reason=VALUES(reason), "
        " fetched_at=VALUES(fetched_at)", vals)
    return len(vals)


def _recent_events(start: str | None = None) -> tuple[str, list[dict]]:
    """从最近交易日往回找第一个**东财有变更流水**的日子，**连同数据一起**返回。

    ⚠ 顺带修掉一处重复请求：老写法先 `_recent_event_day()` 探到日子、外部再
    `fetch_events(day)` 抓一次 —— 同一天打了两次接口。探到就直接把数据带回去。

    返回 `(day, rows)`；`rows == []` 表示那天确实没有（或全都没抓到，调用方按空处理）。
    """
    days = [r["trade_date"] for r in db.query(
        "SELECT DISTINCT trade_date FROM daily_kline ORDER BY trade_date DESC LIMIT %s",
        (EVENT_LOOKBACK,))]
    days = [str(d)[:10] for d in days]
    if not days:
        days = [market.latest_trade_date() or dt.date.today().isoformat()]
    for d in days:
        rows = fetch_events(d)
        if rows:
            return d, rows
    return days[0], []


# ---------------------------------------------------------------------------
# 状态 & 同步
# ---------------------------------------------------------------------------

def state() -> dict:
    row = db.query_one("SELECT v, updated_at FROM app_setting WHERE k=%s", (SETTING_KEY,))
    if not row or not row["v"]:
        return {}
    import json
    try:
        st = json.loads(row["v"])
    except Exception:                           # noqa: BLE001
        return {}
    st["updated_at"] = str(row["updated_at"])[:19] if row["updated_at"] else None
    return st or {}


def _set_state(st: dict) -> None:
    import json
    db.execute(
        "INSERT INTO app_setting (k, v) VALUES (%s, %s) "
        "ON DUPLICATE KEY UPDATE v=VALUES(v)", (SETTING_KEY, json.dumps(st, ensure_ascii=False)))


def sync(force: bool = False, event_day: str | None = None) -> dict:
    """抓一次并落库。

    ⚠ **不再有「今日已抓过就跳过」的短路**（2026-10-08 取消本地缓存）：每次调用都真
    去打东财。`force` 参数只为兼容旧调用方留着 —— 现在传不传都一样。

    抓失败时**不写状态**（下次还会重试），错误往外抛给调用方看原因。
    同一时刻只允许一个抓取在跑（`_LOCK`），后来者等前面的跑完再进去。
    """
    with _LOCK:
        return _sync_once(event_day)


def _sync_once(event_day: str | None = None) -> dict:
    today = dt.date.today().isoformat()

    rows = fetch_new_concepts()
    if rows is None:
        raise RuntimeError("新概念清单没抓到（东财接口不可达或返回异常）")
    n_new = _save_new_concepts(rows, today)

    if event_day:
        day, ev = event_day, (fetch_events(event_day) or [])
    else:
        day, ev = _recent_events()
    n_ev = _save_events(ev, today)

    st = {"date": today, "fetched_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
          "new": n_new, "events": n_ev, "event_date": day}
    _set_state(st)
    _log_nc.info("新概念同步完成：新概念 %d 条，%s 变更 %d 条", n_new, day, n_ev)
    return {"skipped": False, "date": today, **st}


# ---------------------------------------------------------------------------
# 读取（页面）
# ---------------------------------------------------------------------------

def _live_pcts(codes: list[str]) -> dict:
    """一批票 → 本库最近交易日的涨跌幅（东财自带的那个是**概念创建当日**的老数字）。"""
    codes = [c for c in codes if c]
    if not codes:
        return {}
    latest = market.latest_trade_date()
    if not latest:
        return {}
    ph = ",".join(["%s"] * len(codes))
    return {r["ts_code"]: _num(r["pct_chg"]) for r in db.query(
        f"SELECT ts_code, pct_chg FROM daily_kline WHERE trade_date=%s AND ts_code IN ({ph})",
        [latest] + codes)}


def _pct_pair(v):
    """数值 → `(text, cls)`。涨红跌绿由前端 CSS 决定，这里只给 `up/down/flat`。"""
    if v is None:
        return "—", ""
    return f"{v:+.2f}%", ("up" if v > 0 else ("down" if v < 0 else "flat"))


def _stock(code, name, pct, live: dict) -> dict | None:
    code = code or ""
    if not code and not name:
        return None
    lp = live.get(code) if code else None
    txt, cls = _pct_pair(lp)
    return {"ts_code": code, "name": name or "", "live_pct": lp,
            "pct_text": txt, "pct_cls": cls, "has_live": lp is not None}


def overview(new_n: int = 15, event_n: int = 30, days: int = 1,
             refresh: bool = True) -> dict:
    """总览卡片用：新概念清单（按创建时间倒序）+ 最近的板块成分变更。

    ⚠ **每次调用都实时去东财抓一遍**（`refresh=True`，2026-10-08 取消本地缓存）。
    抓失败时**不返空**：库里还有上次的快照就照常渲染，把 `stale=True` 与 `error`
    一起带出去，页面提示「实时抓取失败，显示上次数据」—— 卡片不该因为东财抖一下
    就直接变空。`refresh=False` 只读本地快照（自检 / 离线场景用）。

    ⚠ **满 `MAX_AGE_DAYS`（365）天的概念整条不摆**（2026-10-11 用户要求「超过 365 天的
    概念不展示」）—— 剔在**取前 `new_n` 条之前**，所以摆出来的是「全部合龄的概念里最新
    的 N 条」。`new_total` 报**过滤后**的条数（与徽标同一口径），被剔掉的条数在
    `new_hidden` 与 `note` 里说明。

    文本与涨跌色**在这里算好**（前端不写死字段名）。
    """
    err = None
    if refresh:
        try:
            sync()
        except Exception as e:                      # noqa: BLE001
            err = _err_text(e)
            _log_nc.warning("实时抓取失败，退回本地快照：%s", err)

    st = state()

    rows = db.query(
        "SELECT * FROM em_newconcept ORDER BY start_date IS NULL, start_date DESC, board_name")
    codes = []
    for r in rows:
        codes += [r["c1_code"], r["c2_code"], r["c3_code"]]
    live = _live_pcts(codes)

    # ⚠ 先把**整份清单**都算成 candidate（不是只算前 `new_n` 条）：要先按年龄剔一部分、
    #   再取前 `new_n` 条。只算前 15 条的话，「前 15 条里剔掉 13 条」就只剩 2 条，
    #   而不是「全部里合龄的前 15 条」—— 那是两个口径。
    cand = []
    for r in rows:
        start = str(r["start_date"])[:19] if r["start_date"] else ""
        age = None
        if start:
            try:
                age = (dt.date.today() - dt.date.fromisoformat(start[:10])).days
            except ValueError:
                age = None
        stocks = [s for s in (_stock(r["c1_code"], r["c1_name"], r["c1_pct"], live),
                              _stock(r["c2_code"], r["c2_name"], r["c2_pct"], live),
                              _stock(r["c3_code"], r["c3_name"], r["c3_pct"], live)) if s]
        cand.append({
            "board_code": r["board_code"], "board_name": r["board_name"],
            "start_date": start, "start_text": r["start_text"] or start[5:16],
            "age_days": age,
            "profile": r["profile"] or "", "stocks": stocks,
        })

    # ⚠ 满 MAX_AGE_DAYS 天的概念**不再算「新概念」** → 整条不摆（判据唯一的定义见
    #   模块常量 MAX_AGE_DAYS；日期缺失的 `age_days is None` 保留）。
    fresh = [it for it in cand if it["age_days"] is None or it["age_days"] <= MAX_AGE_DAYS]
    hidden = len(cand) - len(fresh)
    items = fresh[:new_n]

    ev_days = [str(x["change_date"])[:10] for x in db.query(
        "SELECT DISTINCT change_date FROM em_concept_event ORDER BY change_date DESC LIMIT %s",
        (max(1, days),))]
    ev_items = []
    if ev_days:
        ph = ",".join(["%s"] * len(ev_days))
        for r in db.query(
                f"SELECT * FROM em_concept_event WHERE change_date IN ({ph}) "
                "ORDER BY change_date DESC, change_time DESC, board_name", ev_days):
            txt, cls = _pct_pair(_num(r["pct_chg"]))
            btxt, bcls = _pct_pair(_num(r["board_pct"]))
            ev_items.append({
                "board_code": r["board_code"], "board_name": r["board_name"],
                "change_date": str(r["change_date"])[:10], "change_time": r["change_time"] or "",
                "direction": r["direction"] or "", "ts_code": r["ts_code"] or "",
                "name": r["name"] or "", "pct_text": txt, "pct_cls": cls,
                "board_pct_text": btxt, "board_pct_cls": bcls,
                "reason": r["reason"] or "",
            })

    n_ev_day = len([e for e in ev_items if ev_days and e["change_date"] == ev_days[0]])
    note = f"新概念 {len(fresh)} 个"
    if hidden:
        note += f"（另有 {hidden} 个已满 {MAX_AGE_DAYS} 天，不再展示）"
    if ev_days:
        note += f" · 最近变更 {ev_days[0]}（{n_ev_day} 条）"
    if err:
        note += " · 实时抓取失败（" + err + "）"
        if st.get("fetched_at"):
            note += f"，显示 {st['fetched_at'][5:16]} 的快照"
    elif st.get("fetched_at"):
        note += f" · 实时抓取于 {st['fetched_at'][11:16]}"
    return {
        "available": bool(items or ev_items),
        "stale": bool(err),                 # 现在只表示「这次没抓到、看的是旧快照」
        "error": err,
        # `None` = 这次根本没去抓（`refresh=False` 的只读口子），别让人误以为「刚抓过是新数」
        "live": (err is None) if refresh else None,
        "fetched_at": st.get("fetched_at"),
        # ⚠ 2026-10-11：「新概念」只摆未满 MAX_AGE_DAYS 天的 → 这里的「总数」= **过滤后**
        #   还能摆的条数（与清单、与徽标同一口径）；`new_hidden` 是被剔掉的条数，供 note 说明。
        "new_total": len(fresh),
        "new_hidden": hidden,
        "new": items,
        "events": ev_items,
        "event_days": ev_days,
        "note": note,
        "source": "东方财富「新概念速览」· 站外公开接口",
    }


def events(days: int = 5, limit: int = 200) -> dict:
    """最近若干天的变更流水（关注页 / 展开看历史用）。"""
    ds = [str(x["change_date"])[:10] for x in db.query(
        "SELECT DISTINCT change_date FROM em_concept_event ORDER BY change_date DESC LIMIT %s",
        (max(1, days),))]
    if not ds:
        return {"days": [], "items": [], "count": 0}
    ph = ",".join(["%s"] * len(ds))
    rows = db.query(
        f"SELECT * FROM em_concept_event WHERE change_date IN ({ph}) "
        f"ORDER BY change_date DESC, change_time DESC, board_name LIMIT {int(limit)}", ds)
    out = []
    for r in rows:
        txt, cls = _pct_pair(_num(r["pct_chg"]))
        out.append({
            "board_code": r["board_code"], "board_name": r["board_name"],
            "change_date": str(r["change_date"])[:10], "change_time": r["change_time"] or "",
            "direction": r["direction"] or "", "ts_code": r["ts_code"] or "",
            "name": r["name"] or "", "pct_text": txt, "pct_cls": cls,
            "reason": r["reason"] or "",
        })
    return {"days": ds, "items": out, "count": len(out)}


def coverage() -> dict:
    """给数据页/自检用的一行摘要。"""
    a = db.query_one("SELECT COUNT(*) n, MAX(start_date) s FROM em_newconcept") or {}
    b = db.query_one("SELECT COUNT(*) n, MAX(change_date) d FROM em_concept_event") or {}
    st = state()
    return {"new": int(a.get("n") or 0),
            "new_latest": str(a.get("s"))[:19] if a.get("s") else None,
            "events": int(b.get("n") or 0),
            "event_latest": str(b.get("d"))[:10] if b.get("d") else None,
            "fetched_at": st.get("fetched_at"),
            "cached_date": st.get("date")}
