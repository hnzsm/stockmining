# -*- coding: utf-8 -*-
"""热股榜：东方财富 / 同花顺 / 雪球，各取前 N 名（总览页的 3 张卡片）。

⚠ 这三个都是**第三方公开接口**，与 tushare 无关、**不消耗额度**、不进 `sync_log`
（`sync_log` 只记本地库的写入任务）。也正因为在站外，必须容忍外网不通：
每个源单独取、单独失败，失败只写进自己的 `error` 字段（页面那张卡显示原因），
**不影响另外两张卡，也不影响页面其余部分**。

取数口径（2026-09-22 实测）：

| key | 源 | 接口 | 自带字段 |
|---|---|---|---|
| `eastmoney` | 东方财富 人气榜 | `POST emappdata.eastmoney.com/stockrank/getAllCurrentList` | 只有代码 + 名次 |
| `ths` | 同花顺 A股热榜 | `GET dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock` | 名称/涨跌幅/热度值/连板标签/上榜理由 |
| `xueqiu` | 雪球 沪深热股榜 | `GET stock.xueqiu.com/v5/stock/hot_stock/list.json?_type=12` | 名称/涨跌幅/热度值（需先取 `xq_a_token`） |

⚠ **新浪财经没有可用的公开热股接口**（首页 `topstock_display` 已废弃恒返回空、
`touzi/lhpromote/lhstocks.js` 是 2020 年的死数据、`StockHotService.*` 全部
`Service not valid`；它的成交额榜是「活跃度」不是「热股」），所以这里**没有**新浪这一源。
"""
from __future__ import annotations

import datetime as dt
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from core import db, log as _log
from core import market

_log_hot = _log.get("hot")

# 单源超时（秒）。三个源是并发取的，所以整页最坏也就是这个数量级。
TIMEOUT = 8
# 榜单缓存（秒）：榜单是分钟级变化的，同一分钟内反复刷新不必每次都打站外接口。
CACHE_TTL = 60
# 全失败时的短缓存（秒）——避免外网不通时每刷一次页面都等满超时。
FAIL_TTL = 15
# 单源最多取多少条（页面只用前 10，留点余量给以后做「展开」）。
MAX_N = 20

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")
_HDR = {"User-Agent": _UA, "Accept": "application/json, text/plain, */*"}

# 卡片顺序 / 标题的唯一来源（前端只按 key 找容器，标题从接口取，别在页面写死）
SOURCES: list[dict] = [
    {"key": "eastmoney", "title": "东方财富 热股", "subtitle": "人气榜"},
    {"key": "ths", "title": "同花顺 A股热股", "subtitle": "热度榜"},
    {"key": "xueqiu", "title": "雪球 A股热股", "subtitle": "热股榜"},
]
SOURCE_KEYS = [s["key"] for s in SOURCES]

_cache: dict = {"t": 0.0, "n": 0, "ttl": 0.0, "data": None}
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def ts_code_of(raw) -> str:
    """第三方给的证券代码 → 库里的 ts_code；认不出来返回空串。

    ⚠ **只认数字段，不信来源给的市场前缀** —— 实测东财把北交所的 `920229`
    标成 `SZ920229`（库里是 `920229.BJ`）。规则与页面 `normCode` 同一套：
    6→沪、0/3→深、4/8/9→北。
    """
    s = re.sub(r"[^0-9]", "", str(raw or ""))
    if len(s) != 6:
        return ""
    c = s[0]
    mk = "SH" if c == "6" else ("SZ" if c in "03" else ("BJ" if c in "489" else ""))
    return f"{s}.{mk}" if mk else ""


def _num(v):
    """转 float；`--`/空/None 一律给 None（页面显示「—」）。"""
    if v is None or v == "" or v == "--":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _err_text(e: Exception) -> str:
    """异常 → 给人看的一句话（不堆栈）。"""
    if isinstance(e, requests.exceptions.Timeout):
        return f"请求超时（{TIMEOUT}s）"
    if isinstance(e, requests.exceptions.ConnectionError):
        return "连不上站点（检查网络 / 代理 / 防火墙）"
    if isinstance(e, requests.exceptions.HTTPError):
        return f"接口返回 HTTP {getattr(e.response, 'status_code', '?')}"
    return str(e)[:100] or type(e).__name__


# ---------------------------------------------------------------------------
# 三个源
# ---------------------------------------------------------------------------

def _eastmoney(n: int) -> list[dict]:
    """东方财富「人气榜」当前榜（股吧人气，只给代码 + 名次 + 名次变化）。"""
    url = "https://emappdata.eastmoney.com/stockrank/getAllCurrentList"
    payload = {"appId": "appId01", "globalId": "786e4c21-70dc-435a-93bb-38",
               "marketType": "", "pageNo": 1, "pageSize": n}
    r = requests.post(url, json=payload,
                      headers={**_HDR, "Content-Type": "application/json"}, timeout=TIMEOUT)
    r.raise_for_status()
    rows = r.json().get("data") or []
    out = []
    for i in rows:
        code = ts_code_of(i.get("sc"))
        if not code:
            continue
        out.append({"rank": int(i.get("rk") or len(out) + 1), "ts_code": code,
                    "name": None, "pct_chg": None, "hot": None,
                    "tag": None, "note": None})
    return out[:n]


def _ths(n: int) -> list[dict]:
    """同花顺「A股热榜」（小时榜，一次给 100 条，去重后按 order 排序）。"""
    url = "https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock"
    r = requests.get(url, params={"stock_type": "a", "type": "hour", "list_type": "normal"},
                     headers={**_HDR, "Referer": "https://eq.10jqka.com.cn/"}, timeout=TIMEOUT)
    r.raise_for_status()
    rows = ((r.json().get("data") or {}).get("stock_list")) or []
    out = []
    for i in rows:
        code = ts_code_of(i.get("code"))
        if not code:
            continue
        tag = i.get("tag") or {}
        out.append({"rank": int(i.get("order") or len(out) + 1), "ts_code": code,
                    "name": i.get("name"), "pct_chg": _num(i.get("rise_and_fall")),
                    "hot": _num(i.get("rate")), "tag": tag.get("popularity_tag"),
                    "note": i.get("analyse_title")})
    out.sort(key=lambda x: x["rank"])
    return out[:n]


def _xueqiu(n: int) -> list[dict]:
    """雪球「沪深热股榜」。

    ⚠ 两个坑（都实测过）：
    1. **预热要打 `https://xueqiu.com/hq`**，打首页 `/` 只会拿到 `acw_tc`（阿里 WAF 的），
       没有 `xq_a_token`；没 token 直接请求接口会回 `400016 请刷新页面或者重新登录帐号后再试`。
    2. `_type=10`（默认）是**全球**热股（前几名全是美股），沪深要用 **`_type=12`**；
       13=港股、21=美股。这里只认 A股，另外再按 `exchange` 兜一道。
    """
    s = requests.Session()
    s.headers.update(_HDR)
    s.get("https://xueqiu.com/hq", timeout=TIMEOUT)
    r = s.get("https://stock.xueqiu.com/v5/stock/hot_stock/list.json",
              params={"size": n, "_type": 12, "type": 12}, timeout=TIMEOUT)
    r.raise_for_status()
    d = r.json()
    items = (d.get("data") or {}).get("items") or []
    if not items:
        # 雪球把失败原因放在 error_description 里（例如「请刷新页面或者重新登录帐号后再试」）
        raise RuntimeError(d.get("error_description")
                           or f"雪球返回空列表（error_code={d.get('error_code')}）")
    out = []
    for it in items:
        if it.get("exchange") not in ("SH", "SZ", "BJ"):
            continue
        code = ts_code_of(it.get("code"))
        if not code:
            continue
        out.append({"rank": len(out) + 1, "ts_code": code, "name": it.get("name"),
                    "pct_chg": _num(it.get("percent")), "hot": _num(it.get("value")),
                    "tag": None, "note": None})
        if len(out) >= n:
            break
    return out


_FETCHERS = {"eastmoney": _eastmoney, "ths": _ths, "xueqiu": _xueqiu}


# ---------------------------------------------------------------------------
# 本地库补齐
# ---------------------------------------------------------------------------

def _fill_local(items: list[dict]) -> None:
    """东财（以及雪球缺字段时）**只给代码**——名称与涨跌幅用本地库补。

    ⚠ 涨跌幅取的是**最近交易日收盘**那一根（库里没有当日盘中价），
    与同花顺自带的那份实时涨幅在盘中会不一致，页面上有说明。
    """
    need = [i["ts_code"] for i in items
            if not i.get("name") or i.get("pct_chg") is None]
    if not need:
        return
    ph = ",".join(["%s"] * len(need))
    names = {r["ts_code"]: r["name"] for r in db.query(
        f"SELECT ts_code, name FROM stock_basic WHERE ts_code IN ({ph})", need)}
    latest = market.latest_trade_date()
    chg: dict = {}
    if latest:
        chg = {r["ts_code"]: r["pct_chg"] for r in db.query(
            f"SELECT ts_code, pct_chg FROM daily_kline "
            f"WHERE trade_date=%s AND ts_code IN ({ph})", [latest] + need)}
    for i in items:
        if not i.get("name"):
            i["name"] = names.get(i["ts_code"])
        if i.get("pct_chg") is None:
            i["pct_chg"] = _num(chg.get(i["ts_code"]))


# ---------------------------------------------------------------------------
# 对外
# ---------------------------------------------------------------------------

def fetch_one(key: str, n: int) -> dict:
    """取单个源。**不抛异常**：失败返回 `{key, items: [], error: "人话"}`。"""
    try:
        items = _FETCHERS[key](n)
        _fill_local(items)
        return {"key": key, "items": items, "error": None}
    except Exception as e:                      # noqa: BLE001 —— 站外接口，什么都可能发生
        msg = _err_text(e)
        _log_hot.warning("热股榜 %s 取数失败：%s", key, msg)
        return {"key": key, "items": [], "error": msg}


def get_all(n: int = 10, force: bool = False) -> dict:
    """三个源**并发**取，返回

        {as_of, n, latest_trade_date, cached, sources: [{key,title,subtitle,items,error}]}

    ⚠ 缓存按 `n` 分开存：`n` 是榜单**长度**本身而不是过滤条件，n 变了必须重取。
    """
    n = max(1, min(int(n), MAX_N))
    now = time.time()
    with _lock:
        c = _cache
        if (not force and c["data"] is not None and c["n"] == n
                and now - c["t"] < c["ttl"]):
            out = dict(c["data"])
            out["cached"] = True
            return out

    with ThreadPoolExecutor(max_workers=len(SOURCE_KEYS)) as ex:
        got = list(ex.map(lambda k: fetch_one(k, n), SOURCE_KEYS))

    meta = {s["key"]: s for s in SOURCES}
    # 顺序按 SOURCES；字段 = 标题 + 取数结果
    sources = [dict(meta[g["key"]], **g) for g in got]
    all_ok = all(s["error"] is None for s in sources)
    any_ok = any(s["items"] for s in sources)

    data = {
        "as_of": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "n": n,
        "latest_trade_date": market.latest_trade_date(),
        "cached": False,
        "sources": sources,
    }
    # 全成功 → 正常 TTL；有失败 → 短 TTL（别把一次抖动缓存一分钟）；
    # 一个都没取到 → 更短，允许很快重试。
    ttl = CACHE_TTL if all_ok else (FAIL_TTL if any_ok else 5)
    with _lock:
        _cache.update({"t": now, "n": n, "ttl": ttl, "data": data})
    return data
