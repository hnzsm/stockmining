# -*- coding: utf-8 -*-
"""数据同步：从 tushare 拉取行情并写入 MySQL。

设计要点：
  * 日线按「交易日」循环拉取（一次调用返回全市场约 5500 行），比按股票循环省几十倍请求；
  * 同一天同时拉 adj_factor（复权因子）与 daily_basic（换手率/市值等）；
  * 个股资金流向（moneyflow）同样按交易日循环，落库到 money_flow（原始单位：万元/手）；
  * 周线由日线本地合成，不额外消耗接口额度；且**只重算「日线真的动过」的那几周**
    （最近一个完整周 + 本次变动到的周，见 `weekly_since`），不是每次都把区间内所有周重写一遍；
  * 支持断点续传：已同步的交易日会被跳过（除非开启 force）。
"""
from __future__ import annotations

import datetime as dt
import functools
import re
import threading
import time

import numpy as np
import pandas as pd

from config import TUSHARE_MAX_RETRY, TUSHARE_QPS, TUSHARE_TOKEN
from core import db, jobs
from core.log import get as _logget

_log = _logget("sync")

_pro = None
_pro_lock = threading.Lock()
_rate_lock = threading.Lock()
_last_call = [0.0]

BENCHMARK_INDEXES = ["000300.SH", "000905.SH", "000852.SH", "000001.SH", "399006.SZ"]
INDEX_NAMES = {
    "000300.SH": "沪深300", "000905.SH": "中证500", "000852.SH": "中证1000",
    "000016.SH": "上证50", "399006.SZ": "创业板指", "000001.SH": "上证指数",
}


class SyncError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 同步日志（sync_log）：开一条 / 收一条 / 启动清理
# ---------------------------------------------------------------------------
# 一条日志的生命周期：`_open_log()` 插一行 running 并拿到**自增 id** → 干完按这个 id
# 调 `_close_log()` 收尾。
#
# ⚠⚠ 绝不能写成 `log_id = db.execute("INSERT INTO sync_log …")`：
# `db.execute()` 返回的是**受影响行数**（INSERT 成功恒为 1），拿它当 id 会让**每一次**
# 收尾都 UPDATE 到 id=1 —— 现象就是「历史里除第一条外全是 running、行数全是 0、
# 结束时间全是空，而第一条又被后来的任务反复覆盖成一条四不像的记录」。
# 取自增 id 只有 `db.insert()` 一条路（见 core/db.py 里 db.insert 的注释：
# 也别写成 `db.execute(...) or db.lastrowid`，那个 or 永远不走）。
SYNC_LOG_ENABLED = True          # 离线回归脚本置 False：别往真实历史里塞假记录

# 状态词表唯一来源：写库用 key，前端只显示后端给的中文标签与配色（不自己映射英文）。
STATUS_LABEL: dict[str, tuple[str, str]] = {
    "running":     ("运行中", "y"),
    "done":        ("已完成", "g"),
    "failed":      ("失败",   "r"),
    "cancelled":   ("已取消", "r"),
    "interrupted": ("已中断", "r"),
}

# 本线程当前打开的那条日志的 id（见 _open_log / logged_sync）。
_LOG_OPEN = threading.local()


def status_label(status: str | None) -> tuple[str, str]:
    """状态 →（中文标签, 标签色）。未知状态原样显示、按红处理。"""
    return STATUS_LABEL.get(status or "", (status or "—", "r"))


def _open_log(task: str, start: str, end: str) -> int | None:
    """开一条同步日志，返回**自增 id**（`SYNC_LOG_ENABLED=False` 时返回 None）。"""
    if not SYNC_LOG_ENABLED:
        return None
    lid = db.insert(
        "INSERT INTO sync_log (task,date_from,date_to,status,message) "
        "VALUES (%s,%s,%s,'running','')", (task, start, end))
    # 记在本线程，供 `logged_sync` 在异常逃逸时兜底收尾（正常路径由 _close_log 清掉）。
    _LOG_OPEN.id = lid
    return lid


def _close_log(log_id: int | None, rows_cnt: int, message: str,
               status: str = "done") -> None:
    """按 id 收尾。`log_id` 为 None（日志被关掉）时什么都不做。"""
    _LOG_OPEN.id = None
    if not log_id:
        return
    db.execute(
        "UPDATE sync_log SET rows_cnt=%s,status=%s,ended_at=NOW(),message=%s WHERE id=%s",
        (int(rows_cnt or 0), status, message, log_id))


def logged_sync(fn):
    """给同步函数兜底：异常逃出去时把这条日志标成 failed。

    循环内部已经逐日 try/except（单日失败只记一条 warning、不中断），所以异常逃逸只可能
    来自写库/日历这种致命错误 —— 那种情况下函数末尾的收尾代码根本执行不到，日志就会永远
    挂着 running（行数 0、结束时间空）。这里是最后一道；进程被杀/断电则由启动时的
    `close_stale_logs()` 兜底。
    """
    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        _LOG_OPEN.id = None
        try:
            return fn(*args, **kwargs)
        except BaseException as e:  # noqa: BLE001  （含 KeyboardInterrupt，如实记录）
            lid = getattr(_LOG_OPEN, "id", None)
            if lid:
                _close_log(lid, 0, f"中断：{type(e).__name__}: {e}"[:500], status="failed")
            raise
    return _wrapper


def close_stale_logs() -> int:
    """把库里仍挂 'running' 的同步日志如实标成「已中断」，返回条数。

    同步任务只活在内存（`core/jobs._JOBS`），服务一重启就全没了；但日志里的 status
    还写着 running —— 列表里于是出现一排「看着还在跑、其实早结束了」的记录。
    启动这一刻内存里必然是空的，所以此刻的 running 一定是孤儿：标 interrupted、
    补上结束时间并说明原因，**记录不删**（行数是丢的，历史留个坑比删掉更有信息量）。

    与回测记录的处理同一套规矩（见 app.py 的 `_abort_orphan_runs`）。
    """
    n = db.execute(
        "UPDATE sync_log SET status='interrupted', ended_at=NOW(), "
        "message=CONCAT(IFNULL(message,''), IF(IFNULL(message,'')='','',' · '), "
        "'未收尾（服务重启／收尾异常），实际结果未知') "
        "WHERE status='running'")
    if n:
        _log.warning("启动清理：%s 条 running 同步日志标为已中断（任务只在内存，重启即失效）", n)
    return n


def get_pro():
    global _pro
    if _pro is None:
        with _pro_lock:
            if _pro is None:
                import tushare as ts
                if not TUSHARE_TOKEN:
                    raise SyncError("未配置 tushare token")
                _pro = ts.pro_api(TUSHARE_TOKEN)
    return _pro


def _throttle() -> None:
    min_interval = 1.0 / max(TUSHARE_QPS, 0.1)
    with _rate_lock:
        now = time.time()
        wait = min_interval - (now - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()


def call(api: str, **kw) -> pd.DataFrame:
    """带限流与重试的接口调用。"""
    pro = get_pro()
    fn = getattr(pro, api)
    last_err = None
    for attempt in range(TUSHARE_MAX_RETRY):
        _throttle()
        try:
            df = fn(**kw)
            if df is None:
                return pd.DataFrame()
            return df
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            last_err = e
            low = msg.lower()
            if any(k in msg for k in ("每分钟", "每天", "频率", "权限", "积分")) or \
               "limit" in low or "per minute" in low:
                time.sleep(min(30, 4 * (attempt + 1)))
            elif "抱歉" in msg or "timeout" in low or "timed out" in low:
                time.sleep(2 + attempt * 2)
            else:
                if attempt >= 1:
                    raise
                time.sleep(1.5)
    raise SyncError(f"{api} 调用失败: {last_err}")


def _ymd(v) -> str:
    return str(v).replace("-", "")[:8]


def _dash(v) -> str:
    s = str(v).replace("-", "")[:8]
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def _nan_to_none(v):
    try:
        if v is None:
            return None
        if isinstance(v, float) and (v != v):
            return None
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


# ---------------------------------------------------------------------------
# 基础数据
# ---------------------------------------------------------------------------

def sync_stock_basic(jid: str | None = None) -> int:
    df = call("stock_basic", exchange="", list_status="L",
              fields="ts_code,symbol,name,area,industry,market,exchange,list_date,is_hs")
    df2 = call("stock_basic", exchange="", list_status="D",
               fields="ts_code,symbol,name,area,industry,market,exchange,list_date,is_hs")
    df3 = call("stock_basic", exchange="", list_status="P",
               fields="ts_code,symbol,name,area,industry,market,exchange,list_date,is_hs")
    df = pd.concat([df, df2, df3], ignore_index=True).drop_duplicates("ts_code")
    rows = []
    for r in df.to_dict("records"):
        rows.append((
            r.get("ts_code"), r.get("symbol"), r.get("name"), r.get("area"),
            r.get("industry"), r.get("market"), r.get("exchange"),
            _dash(r["list_date"]) if r.get("list_date") else None,
            r.get("list_status", "L"), r.get("is_hs"),
        ))
    sql = """
        INSERT INTO stock_basic
          (ts_code,symbol,name,area,industry,market,exchange,list_date,list_status,is_hs)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
          symbol=VALUES(symbol), name=VALUES(name), area=VALUES(area),
          industry=VALUES(industry), market=VALUES(market), exchange=VALUES(exchange),
          list_date=VALUES(list_date), list_status=VALUES(list_status),
          is_hs=VALUES(is_hs), updated_at=NOW()
    """
    db.insert_many(sql, rows)
    if jid:
        jobs.update(jid, log=f"股票基础信息 {len(rows)} 条")
    return len(rows)


def sync_trade_cal(start: str = "2010-01-01", end: str | None = None,
                   jid: str | None = None) -> int:
    end = end or dt.date.today().strftime("%Y-%m-%d")
    df = call("trade_cal", exchange="SSE", start_date=_ymd(start), end_date=_ymd(end))
    if df.empty:
        return 0
    rows = []
    for r in df.to_dict("records"):
        rows.append((
            r.get("exchange", "SSE"), _dash(r["cal_date"]), int(r.get("is_open") or 0),
            _dash(r["pretrade_date"]) if r.get("pretrade_date") else None,
        ))
    sql = """
        INSERT INTO trade_cal (exchange,cal_date,is_open,pretrade_date)
        VALUES (%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE is_open=VALUES(is_open),
          pretrade_date=VALUES(pretrade_date)
    """
    db.insert_many(sql, rows)
    if jid:
        jobs.update(jid, log=f"交易日历 {len(rows)} 天")
    return len(rows)


def sync_index_member(start: str = "2020-01-01", jid: str | None = None) -> int:
    end = dt.date.today().strftime("%Y%m%d")
    total = 0
    for code in BENCHMARK_INDEXES:
        try:
            df = call("index_weight", index_code=code,
                      start_date=_ymd(start), end_date=end)
        except Exception as e:  # noqa: BLE001
            if jid:
                jobs.update(jid, log=f"指数成分 {code} 跳过: {e}")
            continue
        if df.empty:
            continue
        df = df.sort_values("trade_date")
        latest = df["trade_date"].max()
        snap = df[df["trade_date"] == latest]
        rows = [(code, r["con_code"], _dash(latest), None,
                 _nan_to_none(r.get("weight"))) for r in snap.to_dict("records")]
        sql = """
            INSERT INTO index_member (index_code,ts_code,in_date,out_date,weight)
            VALUES (%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE in_date=VALUES(in_date), weight=VALUES(weight),
              updated_at=NOW()
        """
        if rows:
            db.execute("DELETE FROM index_member WHERE index_code=%s", (code,))
            db.insert_many(sql, rows)
            total += len(rows)
        if jid:
            jobs.update(jid, log=f"指数成分 {INDEX_NAMES.get(code, code)} {len(rows)} 只")
    db.execute(
        "INSERT IGNORE INTO index_member (index_code,ts_code,in_date) "
        "SELECT 'ALL', ts_code, list_date FROM stock_basic WHERE list_status='L'"
    )
    return total


def sync_index_daily(start: str = "2015-01-01", jid: str | None = None) -> int:
    end = dt.date.today().strftime("%Y%m%d")
    total = 0
    for code in BENCHMARK_INDEXES:
        try:
            df = call("index_daily", ts_code=code,
                      start_date=_ymd(start), end_date=end)
        except Exception as e:  # noqa: BLE001
            if jid:
                jobs.update(jid, log=f"指数日线 {code} 跳过: {e}")
            continue
        if df.empty:
            continue
        rows = []
        for r in df.to_dict("records"):
            rows.append((r["ts_code"], _dash(r["trade_date"]), _nan_to_none(r.get("open")),
                         _nan_to_none(r.get("high")), _nan_to_none(r.get("low")),
                         _nan_to_none(r.get("close")), _nan_to_none(r.get("pre_close")),
                         _nan_to_none(r.get("pct_chg")), _nan_to_none(r.get("vol")),
                         _nan_to_none(r.get("amount"))))
        sql = """
            INSERT INTO index_daily
              (ts_code,trade_date,open,high,low,close,pre_close,pct_chg,vol,amount)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE open=VALUES(open),high=VALUES(high),low=VALUES(low),
              close=VALUES(close),pre_close=VALUES(pre_close),pct_chg=VALUES(pct_chg),
              vol=VALUES(vol),amount=VALUES(amount)
        """
        db.insert_many(sql, rows)
        total += len(rows)          # 报处理行数，不是受影响行数（值没变时它是 0）
        if jid:
            jobs.update(jid, log=f"指数日线 {INDEX_NAMES.get(code, code)} {len(rows)} 条")
    return total


# ---------------------------------------------------------------------------
# 日线行情
# ---------------------------------------------------------------------------

def synced_dates(start: str, end: str) -> set[str]:
    rows = db.query(
        "SELECT DISTINCT trade_date FROM daily_kline WHERE trade_date BETWEEN %s AND %s",
        (start, end),
    )
    return {r["trade_date"].strftime("%Y-%m-%d") for r in rows}


@logged_sync
def sync_daily_kline(start: str, end: str, jid: str | None = None,
                     with_basic: bool = True, force: bool = False) -> dict:
    """按交易日同步全市场日线 + 复权因子（+每日指标）。"""
    cal = db.query(
        "SELECT cal_date FROM trade_cal WHERE exchange='SSE' AND is_open=1 "
        "AND cal_date BETWEEN %s AND %s ORDER BY cal_date", (start, end)
    )
    days = [r["cal_date"].strftime("%Y-%m-%d") for r in cal]
    if not days:
        raise SyncError("交易日历为空，请先同步交易日历")

    done = set() if force else synced_dates(start, end)
    todo = [d for d in days if d not in done]
    if jid:
        jobs.update(jid, log=f"待同步交易日 {len(todo)} / {len(days)}（已跳过 {len(done)}）")

    log_id = _open_log("daily_kline", start, end)
    rows_daily = rows_adj = rows_basic = 0
    n = len(todo)
    t0 = time.time()

    for k, day in enumerate(todo):
        if jid and jobs.is_cancelled(jid):
            if jid:
                jobs.update(jid, log="用户取消")
            break
        ymd = _ymd(day)
        try:
            dl = call("daily", trade_date=ymd)
        except Exception as e:  # noqa: BLE001
            if jid:
                jobs.update(jid, log=f"{day} 日线失败: {e}")
            continue
        if dl.empty:
            continue
        try:
            af = call("adj_factor", trade_date=ymd)[["ts_code", "adj_factor"]]
        except Exception:  # noqa: BLE001
            af = pd.DataFrame(columns=["ts_code", "adj_factor"])
        dl = dl.merge(af, on="ts_code", how="left")
        dl["adj_factor"] = dl["adj_factor"].fillna(1.0)

        bb = pd.DataFrame()
        if with_basic:
            try:
                bb = call("daily_basic", trade_date=ymd,
                          fields="ts_code,trade_date,turnover_rate,volume_ratio,"
                                 "pe,pb,ps,total_mv,circ_mv")
            except Exception:  # noqa: BLE001
                bb = pd.DataFrame()

        rows = [(
            r["ts_code"], day, _nan_to_none(r.get("open")), _nan_to_none(r.get("high")),
            _nan_to_none(r.get("low")), _nan_to_none(r.get("close")),
            _nan_to_none(r.get("pre_close")), _nan_to_none(r.get("change")),
            _nan_to_none(r.get("pct_chg")), _nan_to_none(r.get("vol")),
            _nan_to_none(r.get("amount")), _nan_to_none(r.get("adj_factor")),
        ) for r in dl.to_dict("records")]
        sql = """
            INSERT INTO daily_kline
              (ts_code,trade_date,open,high,low,close,pre_close,change_amt,pct_chg,
               vol,amount,adj_factor)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE open=VALUES(open),high=VALUES(high),low=VALUES(low),
              close=VALUES(close),pre_close=VALUES(pre_close),
              change_amt=VALUES(change_amt),pct_chg=VALUES(pct_chg),vol=VALUES(vol),
              amount=VALUES(amount),adj_factor=VALUES(adj_factor)
        """
        db.insert_many(sql, rows)    # 它回报的是**受影响行数**（值没变就是 0），不能用
        rows_daily += len(rows)      # 对外报的行数一律是「本次处理行数」
        rows_adj += len(dl)

        if not bb.empty:
            brows = [(
                r["ts_code"], day, _nan_to_none(r.get("turnover_rate")),
                _nan_to_none(r.get("volume_ratio")), _nan_to_none(r.get("pe")),
                _nan_to_none(r.get("pb")), _nan_to_none(r.get("ps")),
                _nan_to_none(r.get("total_mv")), _nan_to_none(r.get("circ_mv")),
            ) for r in bb.to_dict("records")]
            bsql = """
                INSERT INTO daily_basic
                  (ts_code,trade_date,turnover_rate,volume_ratio,pe,pb,ps,total_mv,circ_mv)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE turnover_rate=VALUES(turnover_rate),
                  volume_ratio=VALUES(volume_ratio),pe=VALUES(pe),pb=VALUES(pb),
                  ps=VALUES(ps),total_mv=VALUES(total_mv),circ_mv=VALUES(circ_mv)
            """
            db.insert_many(bsql, brows)
            rows_basic += len(brows)     # 同上：报处理行数，不报受影响行数

        if jid and (k % 5 == 0 or k == n - 1):
            elapsed = time.time() - t0
            speed = (k + 1) / max(elapsed, 1e-6)
            eta = (n - k - 1) / max(speed, 1e-6)
            jobs.update(
                jid,
                progress=0.05 + 0.75 * (k + 1) / max(n, 1),
                message=(f"{day}  {k+1}/{n}  已写入 {rows_daily} 行  "
                         f"剩余约 {eta/60:.1f} 分钟"),
                log=f"{day} {len(dl)} 只" if k % 25 == 0 else None,
            )

    _close_log(log_id, rows_daily, f"daily={rows_daily} adj={rows_adj} basic={rows_basic}",
               status="done" if not (jid and jobs.is_cancelled(jid)) else "cancelled")
    # `first_day` 给周线合成用：本次**真正写过**的最早一天 —— 只有它之后的周线才需要重算
    # （见 `build_weekly` / `weekly_since`）。日线本来就会跳过已同步日期，所以这个值通常
    # 就是「今天」，于是周线也只需重算最新一两周，而不是把 190 周全写一遍。
    return {"days": len(todo), "daily_rows": rows_daily, "basic_rows": rows_basic,
            "first_day": todo[0] if todo else None}


# ---------------------------------------------------------------------------
# 个股资金流向
# ---------------------------------------------------------------------------

def synced_moneyflow_dates(start: str, end: str) -> set[str]:
    """已同步过资金流向的交易日（断点续传用）。"""
    rows = db.query(
        "SELECT DISTINCT trade_date FROM money_flow WHERE trade_date BETWEEN %s AND %s",
        (start, end),
    )
    return {r["trade_date"].strftime("%Y-%m-%d") for r in rows}


@logged_sync
def sync_moneyflow(start: str, end: str, jid: str | None = None,
                   force: bool = False, p0: float = 0.0, p1: float = 1.0,
                   batch_days: int = 1) -> dict:
    """按交易日同步全市场个股资金流向（tushare moneyflow）。

    单位与 daily_kline 不同，落库前不做换算，保持 tushare 原样：
    金额是**万元**（daily_kline.amount 是千元），量与 daily_kline.vol 一样是**手**。
    换算与派生口径统一放在 core/indicators.py，避免两处口径打架。
    """
    from core.indicators import MF_RAW_COLS

    cal = db.query(
        "SELECT cal_date FROM trade_cal WHERE exchange='SSE' AND is_open=1 "
        "AND cal_date BETWEEN %s AND %s ORDER BY cal_date", (start, end)
    )
    days = [r["cal_date"].strftime("%Y-%m-%d") for r in cal]
    if not days:
        raise SyncError("交易日历为空，请先同步交易日历")

    done = set() if force else synced_moneyflow_dates(start, end)
    todo = [d for d in days if d not in done]
    if jid:
        jobs.update(jid, log=f"资金流向待同步 {len(todo)} / {len(days)} 个交易日"
                              f"（已跳过 {len(done)}）")

    log_id = _open_log("money_flow", start, end)
    cols = ["ts_code", "trade_date"] + MF_RAW_COLS
    sql = (f"INSERT INTO money_flow ({','.join(cols)}) "
           f"VALUES ({','.join(['%s'] * len(cols))}) "
           f"ON DUPLICATE KEY UPDATE " +
           ",".join(f"{c}=VALUES({c})" for c in MF_RAW_COLS))

    rows_cnt = 0
    n = len(todo)
    t0 = time.time()
    for k, day in enumerate(todo):
        if jid and jobs.is_cancelled(jid):
            if jid:
                jobs.update(jid, log="用户取消")
            break
        try:
            mf = call("moneyflow", trade_date=_ymd(day))
        except Exception as e:  # noqa: BLE001
            if jid:
                jobs.update(jid, log=f"{day} 资金流向失败: {e}")
            continue
        if mf is None or mf.empty:
            continue
        rows = [(r["ts_code"], day) + tuple(_nan_to_none(r.get(c)) for c in MF_RAW_COLS)
                for r in mf.to_dict("records")]
        db.insert_many(sql, rows)
        rows_cnt += len(rows)        # 报处理行数，不是受影响行数（见 _insert_or_log）
        if jid and (k % 5 == 0 or k == n - 1):
            el = time.time() - t0
            eta = (n - k - 1) / max((k + 1) / max(el, 1e-6), 1e-6)
            jobs.update(jid,
                        progress=p0 + (p1 - p0) * (k + 1) / max(n, 1),
                        message=(f"{day}  {k+1}/{n}  已写入 {rows_cnt} 行  "
                                 f"剩余约 {eta/60:.1f} 分钟"),
                        log=f"{day} 资金流向 {len(mf)} 只" if k % 25 == 0 else None)
    _close_log(log_id, rows_cnt, f"money_flow={rows_cnt}",
               status="done" if not (jid and jobs.is_cancelled(jid)) else "cancelled")
    return {"days": len(todo), "rows": rows_cnt}


# ---------------------------------------------------------------------------
# 股东户数 / 融资融券 / 股东增减持
# ---------------------------------------------------------------------------
# 三个接口的"时间轴"不一样，**别一把梭**：
#   * `stk_holdernumber` / `stk_holdertrade` 的 ann_date 是**公告日**，含周末与节假日
#     —— 实测 2026-07-04（周六）有 119 条增减持公告。所以按**自然日**逐日扫，
#     拿 trade_cal 过滤会漏掉周末披露的公告。
#   * `margin_detail` 是按**交易日**公布的两融数据，只按交易日扫。
# 为什么逐日拉而不是按区间一次拉：`stk_holdertrade(start_date,end_date)` 单次上限
# 3000 行，实测 2026-08-01~09-17 就被截断（返回正好 3000），区间一长会静默丢数据；
# 按单日拉每份只有几十~几百行，稳。`margin_detail` 单日全市场约 4400 行，也远没到上限。
#
# 断点续传靠"库里已有的公告日/交易日集合"判定（`synced_ann_dates` / 交易日）。
# 某天真的没有公告时不会被记进去，增量重跑会重扫那天 —— 一天一个请求，成本可忽略。
#
# ⚠ **必须剔除 tushare 偶尔混进来的"提示行"**：`margin_detail` 在 2026-01-05 那天的返回里
# 多了一行 `ts_code='日期：2026-01-05.BJ'`（16 字符、除 trade_date 外全 NaN，4284 行 vs
# 平常 4283 行）。插进 VARCHAR(12) 的 ts_code 直接抛 `DataError 1406`，而它是当天批次里的
# 最后一行 —— 于是**整个区间同步在第一天就整体中断**（167 个交易日全没写进去）。
# 判据见 `_drop_bad_codes()`：ts_code 不匹配 `^\d{6}\.(SH|SZ|BJ)$` 的一律丢掉并记日志。
# 另外单日写入失败也只跳过那天（`_insert_or_log`），不让一条脏行带走整段同步。
# ---------------------------------------------------------------------------


def _natural_days(start: str, end: str) -> list[str]:
    """自然日序列（含周末与节假日）。"""
    return [d.strftime("%Y-%m-%d") for d in pd.date_range(start, end, freq="D")]


def _trade_days(start: str, end: str) -> list[str]:
    cal = db.query(
        "SELECT cal_date FROM trade_cal WHERE exchange='SSE' AND is_open=1 "
        "AND cal_date BETWEEN %s AND %s ORDER BY cal_date", (start, end))
    return [r["cal_date"].strftime("%Y-%m-%d") for r in cal]


def synced_ann_dates(table: str, start: str, end: str) -> set[str]:
    """库里已有数据的**公告日**集合（公告类接口的断点续传凭据）。"""
    rows = db.query(
        f"SELECT DISTINCT ann_date FROM `{table}` WHERE ann_date BETWEEN %s AND %s",
        (start, end))
    return {r["ann_date"].strftime("%Y-%m-%d") for r in rows}


def synced_trade_dates_of(table: str, start: str, end: str) -> set[str]:
    """库里已有数据的**交易日**集合。"""
    rows = db.query(
        f"SELECT DISTINCT trade_date FROM `{table}` WHERE trade_date BETWEEN %s AND %s",
        (start, end))
    return {r["trade_date"].strftime("%Y-%m-%d") for r in rows}


def _report_progress(jid, k: int, n: int, t0: float, what: str, rows: int,
                     p0: float, p1: float, every: int = 5) -> None:
    if not jid or (k % every and k != n - 1):
        return
    el = time.time() - t0
    eta = (n - k - 1) / max((k + 1) / max(el, 1e-6), 1e-6)
    jobs.update(jid, progress=p0 + (p1 - p0) * (k + 1) / max(n, 1),
                message=(f"{what} {k+1}/{n}  已写入 {rows} 行  "
                         f"剩余约 {eta/60:.1f} 分钟"))


# 标准 A 股代码：6 位数字 + 交易所后缀（SH/SZ/BJ），恒为 9 字符。
TS_CODE_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")


def _drop_bad_codes(df, what: str = ""):
    """剔除 tushare 混进返回里的"提示行"（ts_code 是中文提示语而非代码）。

    实测 `margin_detail(trade_date='20260105')` 末尾多一行
    `ts_code='日期：2026-01-05.BJ'`、其余字段全 NaN。它是**提示语不是行情**，
    但长度 16 > `VARCHAR(12)`，直接插入会让整批 executemany 报 1406 并中断同步。
    这里只按代码格式做唯一判据（正则 `TS_CODE_RE`），不猜内容。
    """
    if df is None or len(df) == 0 or "ts_code" not in getattr(df, "columns", ()):
        return df
    ok = df["ts_code"].astype(str).str.match(TS_CODE_RE)
    if bool(ok.all()):
        return df
    bad = df.loc[~ok, "ts_code"].astype(str).tolist()
    _log.warning("剔除 %s 行非标准 ts_code%s：%s", len(bad), f"（{what}）" if what else "",
                 bad[:5])
    return df.loc[ok]


def _insert_or_log(sql: str, rows: list, day: str, what: str,
                   jid: str | None, bad: list) -> int:
    """单日写入失败**只跳过那天**，不中断整个区间；失败的日期收集起来最后汇报。

    （2026-01-05 的 ts_code 提示行就是"一条脏行带走整段同步"的实例。）

    返回的是**本次处理的行数 `len(rows)`**，不是 `db.insert_many` 回报的受影响行数 ——
    后者在 `ON DUPLICATE KEY UPDATE` 命中旧值没变时是 **0**，拿它当行数会让「重跑一遍」
    显示成 0 行（周线那个「写入 0 行」就是同一个坑，见 `db.insert_many` 的注释）。
    affected 只用来判「这批是不是纯粹覆盖写、值一点没变」。
    """
    if not rows:
        return 0
    try:
        affected = db.insert_many(sql, rows)
    except Exception as e:  # noqa: BLE001
        bad.append(day)
        _log.warning("%s %s 写入失败，跳过该日：%s: %s", what, day, type(e).__name__, e)
        if jid:
            jobs.update(jid, log=f"{day} {what}写入失败（已跳过）：{type(e).__name__}: {e}")
        return 0
    if not affected and jid:
        jobs.update(jid, log=f"{day} {what} {len(rows)} 行均为覆盖写入、值无变化")
    return len(rows)


@logged_sync
def sync_holder_number(start: str, end: str, jid: str | None = None,
                       force: bool = False, p0: float = 0.0,
                       p1: float = 1.0) -> dict:
    """按公告日同步全市场股东户数（tushare `stk_holdernumber`）。

    落库**不做任何归并**：同一报告期可能在多个公告日出现（快报/正式报告），
    主键 (ts_code, ann_date, end_date) 全部留下；"取哪一条"由读取层
    （`core/chips.py`）按 end_date 归并、取最新公告决定。
    """
    days = _natural_days(start, end)
    if not days:
        raise SyncError("日期区间为空")
    done = set() if force else synced_ann_dates("holder_number", start, end)
    todo = [d for d in days if d not in done]
    if jid:
        jobs.update(jid, log=f"股东户数待同步 {len(todo)} / {len(days)} 个公告日"
                              f"（已跳过 {len(done)}）")
    log_id = _open_log("holder_number", start, end)
    sql = """
        INSERT INTO holder_number (ts_code,ann_date,end_date,holder_num)
        VALUES (%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE holder_num=VALUES(holder_num)
    """
    n, rows_cnt, t0 = len(todo), 0, time.time()
    bad: list[str] = []
    for k, day in enumerate(todo):
        if jid and jobs.is_cancelled(jid):
            jobs.update(jid, log="用户取消")
            break
        try:
            df = call("stk_holdernumber", ann_date=_ymd(day))
        except Exception as e:  # noqa: BLE001
            if jid:
                jobs.update(jid, log=f"{day} 股东户数失败: {e}")
            continue
        df = _drop_bad_codes(df, "股东户数")
        if df is not None and not df.empty:
            rows = [(r["ts_code"], _dash(r["ann_date"]), _dash(r["end_date"]),
                     _nan_to_none(r.get("holder_num")))
                    for r in df.dropna(subset=["end_date"]).to_dict("records")]
            rows_cnt += _insert_or_log(sql, rows, day, "股东户数", jid, bad)
        _report_progress(jid, k, n, t0, "股东户数", rows_cnt, p0, p1)
    if bad:
        _log.warning("股东户数 %s 天写入失败已跳过：%s", len(bad), bad[:10])
    msg = f"holder_number={rows_cnt}" + (f" 跳过失败日 {len(bad)}" if bad else "")
    _close_log(log_id, rows_cnt, msg,
               status="done" if not (jid and jobs.is_cancelled(jid)) else "cancelled")
    return {"days": n, "rows": rows_cnt, "failed_days": bad}


@logged_sync
def sync_margin_detail(start: str, end: str, jid: str | None = None,
                       force: bool = False, p0: float = 0.0,
                       p1: float = 1.0) -> dict:
    """按交易日同步全市场融资融券明细（tushare `margin_detail`）。

    单位保持 tushare 原样：金额是**元**（注意与 money_flow 的万元不同），量是**股**。
    """
    days = _trade_days(start, end)
    if not days:
        raise SyncError("交易日历为空，请先同步交易日历")
    done = set() if force else synced_trade_dates_of("margin_detail", start, end)
    todo = [d for d in days if d not in done]
    if jid:
        jobs.update(jid, log=f"融资融券待同步 {len(todo)} / {len(days)} 个交易日"
                              f"（已跳过 {len(done)}）")
    log_id = _open_log("margin_detail", start, end)
    cols = ["ts_code", "trade_date", "rzye", "rzmre", "rzche",
            "rqye", "rqyl", "rqmcl", "rqchl", "rzrqye"]
    sql = (f"INSERT INTO margin_detail ({','.join(cols)}) "
           f"VALUES ({','.join(['%s'] * len(cols))}) "
           f"ON DUPLICATE KEY UPDATE " +
           ",".join(f"{c}=VALUES({c})" for c in cols[2:]))
    n, rows_cnt, t0 = len(todo), 0, time.time()
    bad: list[str] = []
    for k, day in enumerate(todo):
        if jid and jobs.is_cancelled(jid):
            jobs.update(jid, log="用户取消")
            break
        try:
            md = call("margin_detail", trade_date=_ymd(day))
        except Exception as e:  # noqa: BLE001
            if jid:
                jobs.update(jid, log=f"{day} 融资融券失败: {e}")
            continue
        md = _drop_bad_codes(md, "融资融券")
        if md is not None and not md.empty:
            rows = [(r["ts_code"], day) + tuple(_nan_to_none(r.get(c)) for c in cols[2:])
                    for r in md.to_dict("records")]
            rows_cnt += _insert_or_log(sql, rows, day, "融资融券", jid, bad)
        _report_progress(jid, k, n, t0, "融资融券", rows_cnt, p0, p1)
    if bad:
        _log.warning("融资融券 %s 天写入失败已跳过：%s", len(bad), bad[:10])
    msg = f"margin_detail={rows_cnt}" + (f" 跳过失败日 {len(bad)}" if bad else "")
    _close_log(log_id, rows_cnt, msg,
               status="done" if not (jid and jobs.is_cancelled(jid)) else "cancelled")
    return {"days": n, "rows": rows_cnt, "failed_days": bad}


@logged_sync
def sync_holder_trade(start: str, end: str, jid: str | None = None,
                      force: bool = False, p0: float = 0.0,
                      p1: float = 1.0) -> dict:
    """按公告日同步全市场股东增减持（tushare `stk_holdertrade`）。

    **按公告日整体替换**：tushare 这个接口没有唯一键、且实测存在完全重复的行
    （600519.SH 2025-12-30 同一股东同样的数字出现 3 次），所以先删掉该公告日的
    全部旧行、再去重插入 —— 重复跑幂等，也不会因为造指纹列而把修订前后的数据搅在一起。
    """
    days = _natural_days(start, end)
    if not days:
        raise SyncError("日期区间为空")
    done = set() if force else synced_ann_dates("holder_trade", start, end)
    todo = [d for d in days if d not in done]
    if jid:
        jobs.update(jid, log=f"股东增减持待同步 {len(todo)} / {len(days)} 个公告日"
                              f"（已跳过 {len(done)}）")
    log_id = _open_log("holder_trade", start, end)
    cols = ["ts_code", "ann_date", "holder_name", "holder_type", "in_de",
            "change_vol", "change_ratio", "after_share", "after_ratio",
            "avg_price", "total_share"]
    sql = (f"INSERT INTO holder_trade ({','.join(cols)}) "
           f"VALUES ({','.join(['%s'] * len(cols))})")
    n, rows_cnt, t0 = len(todo), 0, time.time()
    bad: list[str] = []
    for k, day in enumerate(todo):
        if jid and jobs.is_cancelled(jid):
            jobs.update(jid, log="用户取消")
            break
        try:
            df = call("stk_holdertrade", ann_date=_ymd(day))
        except Exception as e:  # noqa: BLE001
            if jid:
                jobs.update(jid, log=f"{day} 股东增减持失败: {e}")
            continue
        df = _drop_bad_codes(df, "股东增减持")
        # 先删后插：即使这次请求失败（df 为空）也已把旧行清掉，
        # 下次重跑该日会补回来（`force=False` 时空日期不在 done 集合里，会重扫）。
        db.execute("DELETE FROM holder_trade WHERE ann_date=%s", (day,))
        if df is not None and not df.empty:
            df = df.drop_duplicates()
            rows = [tuple(_nan_to_none(r.get(c)) if c != "ann_date" else _dash(r["ann_date"])
                          for c in cols)
                    for r in df.dropna(subset=["ann_date"]).to_dict("records")]
            rows_cnt += _insert_or_log(sql, rows, day, "股东增减持", jid, bad)
        _report_progress(jid, k, n, t0, "股东增减持", rows_cnt, p0, p1)
    if bad:
        _log.warning("股东增减持 %s 天写入失败已跳过：%s", len(bad), bad[:10])
    msg = f"holder_trade={rows_cnt}" + (f" 跳过失败日 {len(bad)}" if bad else "")
    _close_log(log_id, rows_cnt, msg,
               status="done" if not (jid and jobs.is_cancelled(jid)) else "cancelled")
    return {"days": n, "rows": rows_cnt, "failed_days": bad}


# ---------------------------------------------------------------------------
# 周线合成
# ---------------------------------------------------------------------------

def _week_start_of(day) -> pd.Timestamp:
    """`day` 所在自然周的**首个交易日**。

    必须对齐到周首交易日：`complete_week_mask(since=…)` 是拿「该周首个交易日」比大小的
    （防取数窗口把某一周切成半周，周线 open 就不是周一那天的价）。直接拿变动日
    （比如周二）当 since，会把它**自己那一周**也筛掉 —— 想重算的那一周反而没重算。
    """
    from core import market
    ts = pd.Timestamp(day)
    lbl = ts + pd.Timedelta(days=4 - ts.weekday())
    first, _ = market.week_bounds()
    ws = first.get(lbl) if len(first) else None
    if ws is None or pd.isna(ws):
        return (ts - pd.Timedelta(days=ts.weekday())).normalize()
    return pd.Timestamp(ws)


def _last_complete_week_start(cutoff) -> pd.Timestamp | None:
    """`cutoff` 之前最近一个**完整周**的首个交易日（拿不到就 None）。"""
    from core import market
    first, last = market.week_bounds()
    if last.empty:
        return None
    done = last.dropna()
    done = done[done <= pd.Timestamp(cutoff)]
    if not len(done):
        return None
    ws = first.get(done.index.max())
    return None if ws is None or pd.isna(ws) else pd.Timestamp(ws)


def weekly_since(start: str, end: str, cutoff, changed: str | None = None) -> str:
    """本次周线合成**真正要重算**的最早日期（已对齐到「所在周的首个交易日」）。

    **周线是日线的派生数据 —— 只有日线变了的那几周才需要重算。** 老写法固定用
    `since=start`，等于每次同步都把区间内所有周线原样 upsert 一遍：2023 起是
    190 周 × 约 5360 只 = **101.8 万行 / 6 分 43 秒**，而本次日线其实只写了
    1 个交易日（5554 行）—— 白干 180 倍，且「同步历史」里那一列大得吓人。

    规则 = min(下面两件) 再夹进 [start, end]：

    | 候选 | 什么时候算 | 为什么 |
    |---|---|---|
    | `changed` | 本次**真正写过**的日线里最早那天（`sync_daily_kline` 的 `todo[0]`） | 补历史缺口时，那几周必须重算 |
    | 最近一个完整周的首交易日 | 每次都要 | 它上次可能是在盘中合成的半成品，或上次同步时它还没收盘（一根都没写）—— 保证不漏周 |

    从「最早变动的那一周」一直算到最后，不是只算那一周：周线的 `pre_close` 依赖
    上一周的收盘价，改了一周就得顺带刷它后面那一周。
    传 `changed=start`（force 全量重拉）就退化成整段重算，与老行为一致。

    对外口径没变（日志里的区间仍是 `start ~ end`），变的只是内部的取数与重算窗口。
    """
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    cand: list[pd.Timestamp] = []
    if changed:
        cand.append(_week_start_of(changed))
    floor = _last_complete_week_start(cutoff)
    if floor is not None:
        cand.append(floor)
    since = min(cand) if cand else s
    # 不越过用户给的区间（夹回 s）——否则会写出区间起点之前那根「半周线」。
    return min(max(since, s), e).strftime("%Y-%m-%d")


def _weekly_codes(start: str, end: str) -> list[str]:
    """周线合成的输入域：该区间内有日线的股票（按代码升序）。

    单独抽出来只为一件事：**输入域只有这一个定义处**。顺带给了回归脚本一个缝 ——
    把输入压到 3 只票，就能在 1 秒内跑完整条链路（合成 → 写库 → 日志收尾），
    否则「周线」这条路径只能拿全市场 5000+ 只跑，一次 90 秒，没法进回归。
    """
    return [r["ts_code"] for r in db.query(
        "SELECT DISTINCT ts_code FROM daily_kline WHERE trade_date BETWEEN %s AND %s "
        "ORDER BY ts_code", (start, end))]


@logged_sync
def build_weekly(start: str, end: str, jid: str | None = None,
                 batch: int = 300, p0: float = 0.8, p1: float = 1.0,
                 changed: str | None = None) -> int:
    """由本地日线合成周线并写入 weekly_kline（不消耗接口额度）。

    `changed` = 本次**真正写过**的日线里最早那天（没有就传 None）。周线是日线的派生
    数据，只有日线变了的那几周才需要重算；不传它等于「区间内所有周线原样重写一遍」
    （2023 起 190 周 ≈ 101.8 万行 / 6 分 43 秒，而实际新增常常只有 1 周 5557 行）。
    重算窗口的完整规则见 `weekly_since`。

    **只写已收盘的完整周**：`resample('W-FRI')` 的标签是"该周的周五"，
    窗口截在周中时标签会落在未来，那根周线其实只有周一~周二的数据，
    而 `dropna(subset=['close'])` 挡不住它（它有 close）。
    判据统一在 `market.complete_week_mask()`，全项目只有那一个口径。

    **返回值 = 本次合成的周线行数**（实际提交写入的行数），**不是** MySQL 的受影响行数：
    `INSERT … ON DUPLICATE KEY UPDATE` 在「命中已有行、值没变」时受影响行数是 **0**，
    拿它当行数 ⇒ 重新合成周线永远显示「周线写入 0 行」（看着像没写，其实整段都覆盖
    写了一遍）。两件事分开记：对外的行数报 `rows`（幂等、重跑同一个数），
    受影响行数只进应用日志与说明文字。
    """
    from core import market
    # 视角里的"市场最新日期"：取 end 与本地日线末端的**较小者**。
    #   * 用 end —— 补历史段（end 落在周中）时不把 end 那一周当成完整周；
    #   * 用本地日线末端 —— 停牌/退市股自身数据早停，不该因此丢掉它的最后一根周线。
    mx = db.scalar("SELECT MAX(trade_date) FROM daily_kline")
    cutoff = min(pd.Timestamp(end), pd.Timestamp(mx)) if mx else pd.Timestamp(end)

    # 实际重算起点：**输入域也跟着它收窄** —— 没动过的那几百周，连取数都不必取。
    since = weekly_since(start, end, cutoff, changed)
    codes = _weekly_codes(since, end)
    log_id = _open_log("weekly_kline", start, end)
    if jid:
        jobs.update(jid, log=f"重算窗口 {since} ~ {end}（{len(codes)} 只）")

    # 自愈：历史上写进去的「未收盘周」（标签落在未来 / 本周还没走完）直接清掉。
    # ⚠ 边界只认本地市场（`mx` = 本地日线最新交易日），**绝不能认 `cutoff`**：
    #   `cutoff = min(end, 本地日线末端)`，而补历史段与回归脚本都会传一个**历史 end**，
    #   那时 cutoff 退到历史 ⇒ `trade_date > 历史那周` 会把 end 之后**已经合法写入**
    #   的周线一并删掉（实测：`verify_sync_log` 传 end=2026-09-18 ⇒ 删光 09-25 与
    #   10-02 两根，`weekly_kline` 从 10-02 静默退化到 09-18）。
    #   自愈删除是**全表**动作，视角必须取全局的本地市场，而不是本次调用的区间末端。
    ref = pd.Timestamp(mx) if mx else None
    _, wl = market.week_bounds()
    if ref is not None and not wl.empty:
        done = wl[wl <= ref]
        if len(done):
            last_lbl = done.index.max().strftime("%Y-%m-%d")
            n_del = db.execute("DELETE FROM weekly_kline WHERE trade_date > %s",
                               (last_lbl,))
            if n_del:
                _log.info("周线自愈：删除未收盘的周线 %s 行（trade_date > %s）",
                          n_del, last_lbl)
                if jid:
                    jobs.update(jid, log=f"清理未收盘周线 {n_del} 行（>{last_lbl}）")

    # 取数窗口往前多留 4 周：pre_close 是窗口内 shift(1) 算的，不往前多取的话
    # 窗口第一周的 pre_close 为 NULL，会靠 ON DUPLICATE KEY 覆盖掉库里正确的值。
    data_start = (pd.Timestamp(since) - pd.Timedelta(days=28)).strftime("%Y-%m-%d")

    # 「哪些周标签可写」事先算一次，之后按标签查表。
    # 判据仍是 `market.complete_week_mask`（唯一口径）—— 但它只跟**周标签**有关，跟是哪只票
    # 无关，逐只调用等于把同一个答案算 5561 遍（每次还要 to_dict 上千个周标签，实测 12.8s）。
    cutoff_ts = pd.Timestamp(cutoff)
    ok_label: dict = {}
    _, wl_all = market.week_bounds()
    if not wl_all.empty:
        base = pd.Series(wl_all.index, index=wl_all.index)
        ok_label = {k: bool(v) for k, v in market.complete_week_mask(
            base, cutoff_ts, since=since).items()}
    n = len(codes)
    total_rows = 0        # 本次合成的行数（对外报这个，重跑幂等）
    affected = 0          # MySQL 受影响行数（值没变就是 0，只用于日志与说明）
    sql = """
        INSERT INTO weekly_kline
          (ts_code,trade_date,open,high,low,close,pre_close,change_amt,pct_chg,
           vol,amount,adj_factor)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE open=VALUES(open),high=VALUES(high),low=VALUES(low),
          close=VALUES(close),pre_close=VALUES(pre_close),
          change_amt=VALUES(change_amt),pct_chg=VALUES(pct_chg),vol=VALUES(vol),
          amount=VALUES(amount),adj_factor=VALUES(adj_factor)
    """
    for i in range(0, n, batch):
        if jid and jobs.is_cancelled(jid):
            jobs.update(jid, log="用户取消")
            break
        part = codes[i:i + batch]
        ph = ",".join(["%s"] * len(part))
        rows = db.query(
            f"SELECT ts_code,trade_date,open,high,low,close,vol,amount,adj_factor "
            f"FROM daily_kline WHERE ts_code IN ({ph}) "
            f"AND trade_date BETWEEN %s AND %s ORDER BY ts_code,trade_date",
            part + [data_start, end],
        )
        if not rows:
            continue
        df = pd.DataFrame(rows)
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        # 周标签：`trade_date` 所在自然周的周五（与 `resample('W-FRI')` 的标签完全一致，
        # `verify_weekly.py` 第 1 组有断言钉住）。
        df["wk"] = df["trade_date"] + pd.to_timedelta(
            4 - df["trade_date"].dt.weekday, unit="D")
        # ⚠ 整批一次聚合，**不逐只票循环**：老写法每只票一次 `resample('W-FRI')`（内部
        #   本就是按标签分组再 agg），全市场实测 16.9s；这里一次 groupby 只要 1.4s。
        #   `sort=True` 保证同一只票的周按时间升序 —— pre_close 的 shift 靠它。
        w = df.groupby(["ts_code", "wk"], sort=True).agg(
            open=("open", "first"), high=("high", "max"), low=("low", "min"),
            close=("close", "last"), vol=("vol", "sum"), amount=("amount", "sum"),
            adj_factor=("adj_factor", "last"),
        ).reset_index().rename(columns={"wk": "trade_date"}).dropna(subset=["close"])
        if not w.empty:
            # pre_close 必须在**裁剪之前**算：它依赖上一周的收盘价，
            # 而窗口起点那一周的上一周，只有往前多取 4 周才有。
            w["pre_close"] = w.groupby("ts_code", sort=False)["close"].shift(1)
            w["change_amt"] = w["close"] - w["pre_close"]
            w["pct_chg"] = (w["close"] / w["pre_close"].replace(0, float("nan")) - 1) * 100
            # 可写的周标签查表（判据仍是 `market.complete_week_mask`，只是全市场算一次）。
            # 查不到 = trade_cal 覆盖不到的老周 → 退回「标签 <= cutoff」，与那个函数里
            # `m.isna()` 那条分支同一判据。
            keep = np.array([ok_label.get(x, x <= cutoff_ts) for x in w["trade_date"]],
                            dtype=bool)
            w = w[keep]
        out = [(
            r["ts_code"], r["trade_date"].strftime("%Y-%m-%d"),
            _nan_to_none(r.get("open")), _nan_to_none(r.get("high")),
            _nan_to_none(r.get("low")), _nan_to_none(r.get("close")),
            _nan_to_none(r.get("pre_close")), _nan_to_none(r.get("change_amt")),
            _nan_to_none(r.get("pct_chg")), _nan_to_none(r.get("vol")),
            _nan_to_none(r.get("amount")), _nan_to_none(r.get("adj_factor")),
        ) for r in w.to_dict("records")]
        affected += db.insert_many(sql, out)
        total_rows += len(out)     # ← 报给用户的行数：本次实际提交写入的行数
        if jid:
            jobs.update(jid, progress=p0 + (p1 - p0) * min(1, (i + batch) / max(n, 1)),
                        message=f"合成周线 {min(i+batch, n)}/{n} 只",
                        log=f"周线 {min(i+batch, n)}/{n} 只 · 累计写入 {total_rows} 行")
    msg = f"weekly_kline={total_rows} 行"
    # 重算窗口一定要写进说明：不写的话，页面上「行数」那一列从 101.8 万掉到 5557
    # 会让人以为少算了（其实是本来就不该算）。
    msg += f"（只重算 {since} 起的周线）" if since != start else "（整段重算）"
    if not codes:
        # 没有日线的区间（比如未来的日期）什么都不该算 —— 但日志照样开、照样收，
        # 说明里写清为什么是 0 行（不然历史里就是一条"0 行且没写原因"的记录）。
        # 注意这里**不是**提前 return：自愈清理（删未收盘周）与收尾共用同一个出口。
        msg += "；区间内没有日线数据，未合成"
    elif total_rows and not affected:
        # 受影响 0 = 每行都命中已有行、且值一模一样。这不是"没写"，是"重跑一遍、
        # 库里本来就是这份数据"—— 说明里点破，免得又被当成故障（原先就是被当成故障的）。
        msg += "；覆盖写入，值无变化"
    _log.info("周线合成完成：%s 行（受影响 %s，输入 %s 只，重算自 %s，区间 %s ~ %s）",
              total_rows, affected, n, since, start, end)
    _close_log(log_id, total_rows, msg,
               status="cancelled" if (jid and jobs.is_cancelled(jid)) else "done")
    return total_rows


# ---------------------------------------------------------------------------
# 期货（tushare fut_basic / fut_daily → 本地库）
# ---------------------------------------------------------------------------

FUT_EXCHANGES = ["SHFE", "DCE", "CZCE", "CFFEX", "INE", "GFEX"]
# 具体合约（带月份的那些）保留多久。用户口径：主连全历史、具体合约近 1 年。
FUT_CONTRACT_DAYS = 365
# 主连历史起点（首次同步时用它；之后按库里已有最后一天增量续）
FUT_MAIN_SINCE = "20000101"


def fut_is_main(symbol, fut_code, exchange, name: str = "") -> bool:
    """「主连（主力连续合约）」的唯一判据 —— 写库时算一次，读取层直接用 `is_main` 列。

    tushare 同一个品种会给出好几条「连续」序列，各所叫法还不统一，所以**不看名字**：

    | ts_code | symbol | fut_code | name | |
    |---|---|---|---|---|
    | `RU.SHF` | RU | RU | 橡胶主力 | ✔ 主连 |
    | `RUL.SHF` | RUL | RU | 橡胶连续 | ✘ 连续 |
    | `RU2511.SHF` | RU2511 | RU | 橡胶2511 | ✘ 具体合约 |

    判据 = **交易所内代码 == 品种码**（全市场 97 条，与「名字以『主力』结尾」的 90 条
    完全对应，多出的 7 条是中金所 —— 它的主力叫「沪深300期货」而不是「…主力」）。

    ⚠ 两个特例，都是实测出来的：
    - 中金所 30 年期国债期货的主力是 `TL0.CFX`（symbol=TL0、fut_code=TL，多了一个 0）；
    - **「月均价主力」要排除**：大商所的 `L_F` / `PP_F` / `V_F`（塑料 / 聚丙烯 / PVC 月均价）
      也满足「symbol == fut_code」，但它们是**结算均价序列**、不可交易，而且 fut_code 带了
      `_F` ⇒ 同一个品种会冒出两行主连（"塑料主力" 与 "塑料月均价主力"）。名字里都有「均价」。
    """
    s = str(symbol or "").strip()
    f = str(fut_code or "").strip()
    if not s or not f:
        return False
    if "均价" in str(name or ""):
        return False
    return s == f or (str(exchange or "").upper() == "CFFEX" and s == f + "0")


@logged_sync
def sync_fut_basic(jid: str | None = None) -> int:
    """同步期货合约基本信息（六家交易所全量，upsert）。"""
    log_id = _open_log("futures_basic", None, None)
    sql = """
        INSERT INTO futures_basic
          (ts_code,symbol,exchange,name,fut_code,is_main,list_date,delist_date)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
          symbol=VALUES(symbol), name=VALUES(name), fut_code=VALUES(fut_code),
          is_main=VALUES(is_main), list_date=VALUES(list_date),
          delist_date=VALUES(delist_date)
    """
    rows: list = []
    for ex in FUT_EXCHANGES:
        try:
            df = call("fut_basic", exchange=ex)
        except Exception as e:                      # noqa: BLE001
            if jid:
                jobs.update(jid, log=f"期货合约 {ex} 失败：{e}")
            continue
        if df is None or df.empty:
            continue
        for r in df.to_dict("records"):
            sym = str(r.get("symbol") or "").strip()
            fc = str(r.get("fut_code") or "").strip()
            rows.append((
                str(r.get("ts_code") or "").strip(), sym, ex,
                str(r.get("name") or "").strip(), fc,
                1 if fut_is_main(sym, fc, ex, r.get("name")) else 0,
                # ⚠ 上市/退市日可能是 NaN：`if r.get(...)` 对 NaN 是 True，
                #   直接 `_dash` 会拼出 'nan--' 让整批插入挂掉（1292）。
                _dash(r["list_date"]) if _nan_to_none(r.get("list_date")) else None,
                _dash(r["delist_date"]) if _nan_to_none(r.get("delist_date")) else None,
            ))
        if jid:
            jobs.update(jid, log=f"期货合约 {ex} {len(df)} 条")
    db.insert_many(sql, rows)
    _close_log(log_id, len(rows), f"futures_basic={len(rows)}（主连 "
                                  f"{sum(1 for r in rows if r[5])} 个）")
    return len(rows)


_FUT_DAILY_SQL = """
    INSERT INTO futures_daily
      (ts_code,trade_date,open,high,low,close,settle,pre_close,pre_settle,
       change1,change2,vol,amount,oi,oi_chg)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    ON DUPLICATE KEY UPDATE
      open=VALUES(open), high=VALUES(high), low=VALUES(low), close=VALUES(close),
      settle=VALUES(settle), pre_close=VALUES(pre_close), pre_settle=VALUES(pre_settle),
      change1=VALUES(change1), change2=VALUES(change2), vol=VALUES(vol),
      amount=VALUES(amount), oi=VALUES(oi), oi_chg=VALUES(oi_chg)
"""


def _fut_daily_rows(df) -> list:
    # ⚠ 别写 `df or pd.DataFrame()`：DataFrame 的 `__bool__` 是刻意抛错的
    #   （"truth value of a DataFrame is ambiguous"），空不空只能问 `.empty`。
    if df is None or getattr(df, "empty", True):
        return []
    out = []
    for r in df.to_dict("records"):
        d = _dash(r["trade_date"]) if r.get("trade_date") else None
        if not d:
            continue
        out.append((
            str(r.get("ts_code") or "").strip(), d,
            _nan_to_none(r.get("open")), _nan_to_none(r.get("high")),
            _nan_to_none(r.get("low")), _nan_to_none(r.get("close")),
            _nan_to_none(r.get("settle")), _nan_to_none(r.get("pre_close")),
            _nan_to_none(r.get("pre_settle")), _nan_to_none(r.get("change1")),
            _nan_to_none(r.get("change2")), _nan_to_none(r.get("vol")),
            _nan_to_none(r.get("amount")), _nan_to_none(r.get("oi")),
            _nan_to_none(r.get("oi_chg")),
        ))
    return out


@logged_sync
def sync_fut_daily(contract_days: int = FUT_CONTRACT_DAYS,
                   jid: str | None = None, force: bool = False,
                   p0: float = 0.0, p1: float = 1.0) -> dict:
    """同步期货日线：**主连全历史** + **具体合约近 `contract_days` 天**。

    两种粒度走两种拉法，因为 tushare 两边都不便宜：
    - 主连：按 `ts_code` 拉（约 98 个，每个一次请求拿全历史，之后按库里最后一天增量续）；
    - 具体合约：按 `trade_date` 拉（一天一次拿全市场 ~1075 行），只补没同步过的日子。

    返回 `{"mains": 主连个数, "main_rows": 行数, "days": 天数, "day_rows": 行数}`。
    ⚠ 行数都是**自己数的 `len(rows)`**，不是 `insert_many` 的受影响行数（后者重跑时是 0）。

    `p0`/`p1` 是**进度区间**：这段活儿占整个任务条的多大一块（挂进 `full_sync` 时用），
    单独跑 `--only-futures` 就是默认的 0~1。两段各占一半。
    """
    today = dt.date.today()
    end = today.strftime("%Y-%m-%d")
    log_id = _open_log("futures_daily", None, end)
    res = {"mains": 0, "main_rows": 0, "days": 0, "day_rows": 0}
    p_mid = p0 + (p1 - p0) * 0.5
    bad: list[str] = []

    # --- 1) 主连：按 ts_code 增量拉全历史 ---
    have = db.query(
        "SELECT b.ts_code AS code, MAX(d.trade_date) AS last "
        "FROM futures_basic b LEFT JOIN futures_daily d ON d.ts_code = b.ts_code "
        "WHERE b.is_main = 1 GROUP BY b.ts_code")
    t0 = time.time()
    n = len(have)
    for k, h in enumerate(have):
        if jid and jobs.is_cancelled(jid):
            break
        last = h.get("last")
        if last is not None:
            sd = (pd.Timestamp(last).date() + dt.timedelta(days=1))
            if sd > today:
                continue
            sd = sd.strftime("%Y-%m-%d")
        else:
            sd = FUT_MAIN_SINCE
        try:
            df = call("fut_daily", ts_code=h["code"],
                      start_date=_ymd(sd), end_date=_ymd(end))
        except Exception as e:                      # noqa: BLE001
            bad.append(f"{h['code']}: {e}"[:120])
            continue
        rows = _fut_daily_rows(df)
        if rows:
            db.insert_many(_FUT_DAILY_SQL, rows)
            res["main_rows"] += len(rows)
            res["mains"] += 1
        _report_progress(jid, k, n, t0, "期货主连", res["main_rows"], p0, p_mid)

    # --- 2) 具体合约：按交易日补近 N 天 ---
    start = (today - dt.timedelta(days=int(contract_days or FUT_CONTRACT_DAYS)))
    start_s = start.strftime("%Y-%m-%d")
    todo_all = _trade_days(start_s, end)
    done = set()
    if not force:
        # ⚠ 「这天有没有数据」**不能只看日期**：主连与具体合约共用同一张表，
        #   而主连是全历史拉的 —— 近 1 年的日期早就被主连写过了，光看日期会得出
        #   「全都同步过」的结论，具体合约于是一行都拉不到（首次同步实测就是 0 天）。
        #   判据必须是「这天有没有**具体合约**（is_main=0）的数据」。
        done = {str(r["trade_date"]) for r in db.query(
            "SELECT DISTINCT d.trade_date FROM futures_daily d "
            "JOIN futures_basic b ON b.ts_code = d.ts_code "
            "WHERE d.trade_date >= %s AND b.is_main = 0", (start_s,)) or []}
    todo = [d for d in todo_all if str(d) not in done]
    t0 = time.time()
    n = len(todo)
    for k, day in enumerate(todo):
        if jid and jobs.is_cancelled(jid):
            break
        try:
            df = call("fut_daily", trade_date=_ymd(day))
        except Exception as e:                      # noqa: BLE001
            bad.append(f"{day}: {e}"[:120])
            continue
        rows = _fut_daily_rows(df)
        if rows:
            db.insert_many(_FUT_DAILY_SQL, rows)
            res["day_rows"] += len(rows)
            res["days"] += 1
        _report_progress(jid, k, n, t0, "期货合约", res["day_rows"], p_mid, p1)

    if bad:
        _log.warning("期货日线 %d 项失败已跳过：%s", len(bad), bad[:8])
    msg = (f"主连 {res['mains']} 个 / {res['main_rows']} 行；"
           f"合约 {res['days']} 天 / {res['day_rows']} 行"
           + (f"；跳过失败 {len(bad)}" if bad else ""))
    _close_log(log_id, res["main_rows"] + res["day_rows"], msg,
               status="cancelled" if (jid and jobs.is_cancelled(jid)) else "done")
    return res


# ---- 期权（只为给期货页补一列「对应期权到期日」）-----------------------------
# ⚠ `opt_basic` 单页**硬顶 12000 行**：给 limit=20000 / 40000 都只回 12000（实测），
#   所以必须拿 offset 翻页，否则数据静默缺一半（各所实际 5~7 万行含历史）。
OPT_LIMIT = 12000
OPT_MAX_PAGES = 12          # 每所最多翻 12 页（14.4 万行），跑满说明出事了

_OPT_SQL = """
    INSERT INTO futures_option
      (ts_code,symbol,exchange,name,opt_type,call_put,exercise_price,per_unit,
       s_month,underlying,opt_code,maturity_date,list_date,delist_date)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    ON DUPLICATE KEY UPDATE
      symbol=VALUES(symbol), exchange=VALUES(exchange), name=VALUES(name),
      opt_type=VALUES(opt_type), call_put=VALUES(call_put),
      exercise_price=VALUES(exercise_price), per_unit=VALUES(per_unit),
      s_month=VALUES(s_month), underlying=VALUES(underlying),
      opt_code=VALUES(opt_code), maturity_date=VALUES(maturity_date),
      list_date=VALUES(list_date), delist_date=VALUES(delist_date)
"""


def opt_underlying(opt_code, ts_code="") -> str:
    """期权 → **标的期货合约的 ts_code**（唯一来源）。

    tushare 的 `opt_code` 就是 `OP` + 标的代码：
    `OPPG2709.DCE` → `PG2709.DCE`、`OPAU2708.SHF` → `AU2708.SHF`、
    `OPSA2707.ZCE` → `SA2707.ZCE`。正好是 `futures_basic.ts_code`，可以直接对。
    ⚠ 中金所股指期权的标的是**指数**（`OP000300.SH` → `000300.SH`），对不到任何期货
    合约 —— 这是正常的（IO 期权现金交割），读取端自然显示「—」，不要特殊照顾它。
    `opt_code` 缺失时退回从 `ts_code` 前段猜（`AU2708P728.SHF` → `AU2708.SHF`）。
    """
    oc = str(opt_code or "").strip()
    if oc.startswith("OP") and "." in oc:
        return oc[2:]
    s = str(ts_code or "").strip()
    m = re.match(r"^([A-Za-z]+\d{3,4})", s)
    if not m:
        return ""
    ex = s.split(".")[-1] if "." in s else ""
    return f"{m.group(1)}.{ex}" if ex else m.group(1)


def _opt_rows(df, today_y: str) -> list:
    """期权 DataFrame → 入库行（**只留未退市**的，页面只关心在市的）。"""
    if df is None or getattr(df, "empty", True):
        return []
    out = []
    for r in df.to_dict("records"):
        code = str(r.get("ts_code") or "").strip()
        if not code:
            continue
        dl = _nan_to_none(r.get("delist_date"))
        dl_s = _dash(dl) if dl else None
        if dl_s and dl_s < today_y:
            continue                                  # 已退市，页面用不上
        mat = _nan_to_none(r.get("maturity_date"))
        out.append((
            code, str(r.get("symbol") or "").strip(),
            str(r.get("exchange") or "").strip(), str(r.get("name") or "").strip(),
            str(r.get("opt_type") or "").strip(), str(r.get("call_put") or "").strip(),
            _nan_to_none(r.get("exercise_price")), _nan_to_none(r.get("per_unit")),
            str(r.get("s_month") or "").strip(),
            opt_underlying(r.get("opt_code"), code),
            str(r.get("opt_code") or "").strip(),
            _dash(mat) if mat else dl_s,              # 到期日缺了就退回退市日
            _dash(r["list_date"]) if _nan_to_none(r.get("list_date")) else None,
            dl_s,
        ))
    return out


@logged_sync
def sync_fut_option(jid: str | None = None,
                    p0: float = 0.0, p1: float = 1.0) -> int:
    """同步**在市**期货期权（六所，翻页拉全）。

    只为给期货页「展开的同品种合约」补一列「对应期权到期日」。同一标的期货合约的
    期权有一大批（各档行权价 × 购/沽），但**到期日相同**，所以读取端按 `underlying`
    取集合就够了。

    ⚠ 增量：全量重拉（在市 2.4 万行 / 各所 5~7 页），不做日期增量 —— 期权上市是
    零散的，逐日补要维护另一套判据，不划算。upsert 幂等，重复跑安全。
    ⚠ 只在市：`delist_date < 今天` 的直接丢。所以表里**只会在市 + 历史残留**，
    读取端还要再按 `delist_date >= 今天` 过一遍（别默认表里全是在市）。

    `p0`/`p1` 是进度区间（挂进 `full_sync` 时用）；单独跑就是默认 0~1。
    """
    today_y = dt.date.today().strftime("%Y-%m-%d")
    log_id = _open_log("futures_option", None, today_y)
    rows: list = []
    bad: list[str] = []
    t0 = time.time()
    for k, ex in enumerate(FUT_EXCHANGES):
        got = 0
        for p in range(OPT_MAX_PAGES):
            try:
                df = call("opt_basic", exchange=ex, limit=OPT_LIMIT,
                          offset=p * OPT_LIMIT)
            except Exception as e:                  # noqa: BLE001
                bad.append(f"{ex}@{p}: {e}"[:140])
                break
            n = 0 if df is None or getattr(df, "empty", True) else len(df)
            if not n:
                break
            got += n
            rows.extend(_opt_rows(df, today_y))
            if n < OPT_LIMIT:
                break
        if jid:
            jobs.update(jid, log=f"期权 {ex} 取回 {got} 条 / 在市累计 {len(rows)}")
        _report_progress(jid, k, len(FUT_EXCHANGES), t0, "期货期权", len(rows), p0, p1)

    if rows:
        for i in range(0, len(rows), 2000):         # 分批，别一次塞 2 万行
            db.insert_many(_OPT_SQL, rows[i:i + 2000])
    if bad:
        _log.warning("期货期权 %d 项失败已跳过：%s", len(bad), bad[:6])
    msg = (f"在市期权 {len(rows)} 条"
           + (f"；跳过失败 {len(bad)}" if bad else "")
           + (f"；每所都翻满 {OPT_MAX_PAGES} 页，可能还有没拉完的" if len(rows) >= OPT_LIMIT * OPT_MAX_PAGES else ""))
    _close_log(log_id, len(rows), msg,
               status="cancelled" if (jid and jobs.is_cancelled(jid)) else "done")
    return len(rows)


def sync_futures(contract_days: int = FUT_CONTRACT_DAYS,
                 jid: str | None = None, force: bool = False,
                 p0: float = 0.0, p1: float = 1.0) -> dict:
    """期货一站式同步：合约基本信息 → 日线 → **期权**（只拉在市）。

    `p0`/`p1` 是进度区间：挂在一站式同步的最后一段时传 0.98~1.0，
    单独跑（`--only-futures` / `/api/sync/futures`）用默认 0~1。
    日线吃这个区间的 95%，期权吃剩下 5%（期权约 14s、日线约 25s，按耗时分的）。
    """
    p_mid = p0 + (p1 - p0) * 0.95
    if jid:
        jobs.update(jid, progress=p0, message="同步期货合约基本信息…")
    n_basic = sync_fut_basic(jid)
    if jid and jobs.is_cancelled(jid):
        return {"basic": n_basic}
    if jid:
        jobs.update(jid, progress=p0, message="同步期货日线…")
    daily = sync_fut_daily(contract_days=contract_days, jid=jid, force=force,
                           p0=p0, p1=p_mid)
    if jid and jobs.is_cancelled(jid):
        return {"basic": n_basic, **daily}
    if jid:
        jobs.update(jid, progress=p_mid, message="同步期货期权…")
    n_opt = sync_fut_option(jid, p0=p_mid, p1=p1)
    return {"basic": n_basic, **daily, "options": n_opt}


# ---------------------------------------------------------------------------
# 一站式同步
# ---------------------------------------------------------------------------

def full_sync(start: str, end: str | None = None, jid: str | None = None,
              with_basic: bool = True, force: bool = False,
              include_base: bool = True, with_moneyflow: bool = True,
              with_holders: bool = True, with_futures: bool = True) -> dict:
    end = end or dt.date.today().strftime("%Y-%m-%d")
    res: dict = {}
    if include_base or not db.scalar("SELECT COUNT(*) FROM trade_cal"):
        if jid:
            jobs.update(jid, progress=0.01, message="同步交易日历…")
        sync_trade_cal(start, end, jid)
        sync_stock_basic(jid)
        res["base"] = True
    if jid:
        jobs.update(jid, progress=0.03, message="同步指数成分与指数日线…")
    try:
        sync_index_member(jid=jid)
    except Exception as e:  # noqa: BLE001
        if jid:
            jobs.update(jid, log=f"指数成分失败（可忽略）: {e}")
    try:
        sync_index_daily(jid=jid)
    except Exception as e:  # noqa: BLE001
        if jid:
            jobs.update(jid, log=f"指数日线失败（可忽略）: {e}")

    if jid:
        jobs.update(jid, progress=0.05, message="同步日线行情…")
    res["daily"] = sync_daily_kline(start, end, jid, with_basic=with_basic, force=force)

    if with_moneyflow:
        if jid:
            jobs.update(jid, progress=0.80, message="同步个股资金流向…")
        try:
            res["moneyflow"] = sync_moneyflow(start, end, jid, force=force,
                                              p0=0.80, p1=0.90)
        except Exception as e:  # noqa: BLE001
            # 资金流向是可选增强数据：没权限/额度不足时不该拖垮整条同步
            res["moneyflow"] = {"error": str(e)}
            if jid:
                jobs.update(jid, log=f"资金流向同步失败（行情不受影响）: {e}")

    if with_holders:
        if jid:
            jobs.update(jid, progress=0.90, message="同步股东户数 / 融资融券 / 增减持…")
        for key, fn, band in (("holder_number", sync_holder_number, (0.90, 0.925)),
                              ("margin_detail", sync_margin_detail, (0.925, 0.95)),
                              ("holder_trade", sync_holder_trade, (0.95, 0.975))):
            try:
                res[key] = fn(start, end, jid, force=force, p0=band[0], p1=band[1])
            except Exception as e:  # noqa: BLE001
                # 与资金流向同样处理：增强数据没权限/额度不足时，不该拖垮整条同步
                res[key] = {"error": str(e)}
                if jid:
                    jobs.update(jid, log=f"{key} 同步失败（行情不受影响）: {e}")

    if jid:
        jobs.update(jid, progress=0.975, message="合成周线…")
    # 把「日线这次真正写过的最早一天」交给周线合成：只重算那几周 + 最近一个完整周，
    # 而不是把 start~end 之间 190 周（101.8 万行）原样重写一遍（见 `weekly_since`）。
    # force 全量重拉时 daily 的 todo 就是整段 → first_day=start → 自然退化成整段重算。
    # 勾了期货就给它留 0.98~1.0 的尾巴（期货是最后一段）。
    res["weekly_rows"] = build_weekly(start, end, jid, p0=0.975,
                                      p1=0.98 if with_futures else 1.0,
                                      changed=(res.get("daily") or {}).get("first_day"))

    if with_futures:
        # ⚠ 期货和股票行情**不同源、不同粒度**：主连拉全历史 + 具体合约拉近 1 年 +
        #   在市期权，跟 start/end 这个区间**无关** —— 它永远只补「最新到哪一天」。
        #   所以这里不传日期，增量判据由 `sync_fut_daily` 自己按「库里最后一天」算。
        # ⚠ 放最后：期货只喂「今日期货」页，不进 prepare_data、不参与选股/回测，
        #   夜里挂掉也不该影响前面已经拉好的行情。
        if jid:
            jobs.update(jid, progress=0.98, message="同步期货（主连/合约/期权）…")
        try:
            res["futures"] = sync_futures(jid=jid, force=force, p0=0.98, p1=1.0)
        except Exception as e:  # noqa: BLE001
            # 与资金流向 / 股东数据同待遇：期货是增强展示数据，拿不到不该拖垮整条同步
            res["futures"] = {"error": str(e)}
            if jid:
                jobs.update(jid, log=f"期货同步失败（行情不受影响）: {e}")
    return res
