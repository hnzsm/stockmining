# -*- coding: utf-8 -*-
"""短线选股工作台 —— Flask 应用入口。"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
import traceback
import uuid

import numpy as np
import pandas as pd
from flask import Flask, Response, g, jsonify, render_template, request, send_from_directory
from flask_cors import CORS
from werkzeug.exceptions import HTTPException

from config import (ADJUST_MODE, BACKTEST_CACHE, BACKTEST_CACHE_MAX_MB,
                    BACKTEST_CACHE_TTL_DAYS, BACKTEST_DEFAULTS, DEFAULT_SYNC_START,
                    FLASK_DEBUG, FLASK_HOST, FLASK_PORT, LOG_LEVEL,
                    LOG_SLOW_REQ_MS, TUSHARE_TOKEN)
from core import backtest as bt
from core import db, expr, hot, jobs, market, pool as pool_mod, screener, strategy as st, sync
from core import futures as fut
from core import concept
from core import newconcept
from core import watch
from core import ai as aia
from core import llm
from core import log as logx
from core import universe as uni

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True
CORS(app)

# 日志：控制台 + logs/app.log（滚动），级别由 STOCKMINING_LOG_LEVEL 控制
logx.setup_logging()
log = logx.get("web")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def ok(data=None, **kw):
    return jsonify({"ok": True, "data": data, **kw})


def err(msg: str, code: int = 400):
    return jsonify({"ok": False, "error": str(msg)}), code


def body() -> dict:
    return request.get_json(silent=True) or {}


class BadParam(ValueError):
    """请求参数不是期望的类型 —— 由 `_int()` 抛，全局兜底成 **400**（见下面 errorhandler）。

    ⚠ 2026-10-09 接口测试抓出来的：以前各路由直接 `int(request.args.get(...))`，
      用户（或回归）传个 `abc` 就抛 `ValueError` ⇒ Flask 转成 **500 + 一整屏 traceback**。
      接口契约上「乱传必须 400」：500 会把「你传错了」和「我写错了」混成一类，
      而后者才是要紧的 —— 日志里被一片参数错误淹没的真 bug 最难查。
    """


def _int(v, default=None, name: str = "参数"):
    """把**请求参数**转成 int：没给/空 → `default`；乱传 → 抛 `BadParam`（→400）。

    ⚠ 与老写法 `int(x or 默认)` 有个区别：**`0` 现在真的是 0**（老的 `or` 把 0 当「没给」
      顶成默认值）。这是顺手修掉的第二个坑 —— 想传 0 却被当成没传，属于静默换口径。
    """
    if v is None or (isinstance(v, str) and not v.strip()):
        return default
    if isinstance(v, bool):
        raise BadParam(f"{name} 必须是整数")
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        try:
            return int(v.strip())
        except ValueError:
            raise BadParam(f"{name} 必须是整数（收到 {v.strip()[:20]!r}）")
    raise BadParam(f"{name} 必须是整数")


@app.errorhandler(BadParam)
def _handle_bad_param(e):
    """「参数不对」一律 400 —— 不许冒泡成 500。"""
    return err(str(e))


def _parse_group_id(raw):
    """`group_id` 的**唯一**解析口径（`GET /api/watch/feed` 与 `POST /api/watch/check` 共用）。

    `None` / 空串 / `0` = 全部；负数 → 400；不像数的 → 400。
    ⚠ 两边各写一套的下场：一边报错、一边静默退回「全部」 ⇒ 用户以为在看本组，
      看到的其实是全市场 —— 这条最阴，而且**不报错**，看不出来。
    """
    if raw is None or raw == 0 or raw == "0" or (isinstance(raw, str) and not raw.strip()):
        return None
    gid = _int(raw, None, "group_id")
    if gid is None or gid <= 0:
        raise BadParam("group_id 必须是正整数")
    return gid


def _date(v, default=None):
    if not v:
        return default
    return str(v).replace("/", "-")[:10]


def _months_before(iso: str, months: int) -> str:
    """ISO 日期往前推 N 个自然月（「近 3 月」这类默认区间用）。

    月末夹紧：3/31 往前推 1 个月是 2/28（不是 2/31），逐日回退到合法日期。
    """
    d = dt.date.fromisoformat(str(iso)[:10])
    y, m = d.year, d.month - int(months)
    while m <= 0:
        m += 12
        y -= 1
    day = d.day
    while day > 1:
        try:
            return dt.date(y, m, day).isoformat()
        except ValueError:  # 该月没有这一天，回退一天
            day -= 1
    return dt.date(y, m, 1).isoformat()


def _qs(d: dict) -> str:
    """把请求参数压成一行，便于排查（长文本截断）。"""
    out = {}
    for k, v in d.items():
        s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
        out[k] = s if len(s) <= 300 else s[:300] + f"…(+{len(s) - 300})"
    return json.dumps(out, ensure_ascii=False)


@app.before_request
def _log_req_in():
    g.t0 = time.perf_counter()
    g.rid = uuid.uuid4().hex[:8]
    logx.set_rid(g.rid)
    if request.path.startswith(("/static/", "/favicon.ico")):
        return None
    qs = request.query_string.decode("utf-8", "ignore")
    extra = ""
    if request.is_json:
        try:
            extra = " body=" + _qs(request.get_json(silent=True) or {})
        except Exception:  # noqa: BLE001
            extra = ""
    elif request.form:
        extra = " form=" + _qs(dict(request.form))
    log.debug("→ %s %s%s%s", request.method, request.path,
              ("?" + qs) if qs else "", extra)
    return None


@app.after_request
def _no_cache(resp):
    if resp.mimetype == "application/json":
        resp.headers["Cache-Control"] = "no-store"
    if hasattr(g, "rid"):
        resp.headers["X-Request-Id"] = g.rid
    if hasattr(g, "t0") and not request.path.startswith(("/static/", "/favicon.ico")):
        ms = (time.perf_counter() - g.t0) * 1000
        size = resp.headers.get("Content-Length") or "-"
        msg = f"← {request.method} {request.path} {resp.status_code} {ms:.1f}ms size={size}"
        lvl = log.warning if ms >= LOG_SLOW_REQ_MS else log.debug
        if resp.status_code >= 400:
            lvl = log.warning
        lvl(msg)
    logx.set_rid("-")
    return resp


@app.errorhandler(HTTPException)
def _on_http_error(e):  # noqa: ANN001
    """404/405/400 是正常的 HTTP 语义，不是程序出错。

    注意：HTTPException 是 Exception 的子类，如果只注册 Exception 处理器，
    浏览器惯例请求的 /favicon.ico 会走 NotFound 并被当成未捕获异常，
    回 500 + 整个 traceback 记 ERROR（日志被刷屏，还掩盖真正的故障）。
    所以这里必须先按 HTTP 语义处理，让 Exception 处理器只留给真异常。
    """
    code = e.code or 500
    if request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json":
        return jsonify({"ok": False, "error": e.name, "code": code}), code
    return e.get_response()


@app.errorhandler(Exception)
def _on_error(e):  # noqa: ANN001
    tb = traceback.format_exc()
    log.error("未捕获异常 %s %s\n%s", request.method, request.path, tb)
    return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}",
                    "trace": tb[-1200:]}), 500


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------

@app.route("/favicon.ico")
def page_favicon():
    """浏览器默认会来要站点图标；显式给一个，别让它变成 404 噪音。"""
    return send_from_directory(app.static_folder, "favicon.ico", max_age=86400)

@app.route("/")
def page_home():
    return render_template("index.html", page="home",
                           stats=market.data_stats(), strategies=st.list_strategies())


@app.route("/monitor")
def page_monitor():
    """分时监控：以纯前端为主，行情直接向腾讯接口取，不经过本站后端。

    这里只负责渲染壳子（宫格由前端 JS 生成），不传任何行情数据。
    本站与它有关的只有两个接口：`/api/market/trade_days`（判断今天该不该取数）
    和 `/api/monitor/kline`（切日K时取本地库里的日线历史）。
    """
    return render_template("monitor.html", page="monitor")


@app.route("/futures")
def page_futures():
    """今日期货：六个交易所的全部合约，按品种分组（一组一个主连 + 同品种合约）。

    页面壳子交给前端 JS 渲染（`static/js/futures.js`），后端只出 `/api/futures`。
    数据来自东方财富的公开接口（见 `core/futures.py`）—— **不进本地库、不耗 tushare 额度**。
    """
    return render_template("futures.html", page="futures")


@app.route("/futures/kline")
def page_futures_kline():
    """期货 K 线页：给列表里的「查看K线」当**同源 iframe** 嵌用。

    ⚠ 与个股那条不同：期货没有基本面/筹码/信号，套不进 `/stock/<代码>`（那一页的
    卡片与字段全是股票口径）⇒ 单独一页。但「弹窗里嵌同源 iframe」这个做法保持一致，
    这样 K 线只是**一份实现**，弹窗与单独打开看到的东西不会分叉。
    """
    return render_template("futures_kline.html", page="futures",
                           secid=(request.args.get("secid") or "").strip(),
                           name=(request.args.get("name") or "").strip(),
                           embed=bool(request.args.get("embed")))


@app.route("/data")
def page_data():
    return render_template("data.html", page="data",
                           stats=market.data_stats(),
                           universes=market.list_universes())


@app.route("/strategies")
def page_strategies():
    return render_template("strategy.html", page="strategy",
                           strategies=st.list_strategies())


@app.route("/pools")
def page_pools():
    """选股策略管理：保存各种「条件股票池」，供回测/选股的股票池下拉框直接引用。"""
    return render_template("pool.html", page="pool",
                           pools=[pool_mod.describe(p) for p in pool_mod.list_pools()],
                           universes=market.list_universes(),
                           # 周期选项与标签后端的唯一来源（`universe.FREQS`）——
                           # 页面不写死「日K / 周K」，改一处全站生效
                           freqs=st.FREQS, freq_order=st.FREQ_ORDER,
                           default_freq=st.DEFAULT_FREQ)


@app.route("/backtest")
def page_backtest():
    return render_template("backtest.html", page="backtest",
                           strategies=st.list_strategies(),
                           universes=market.list_universes())


@app.route("/screen")
def page_screen():
    td = market.latest_trade_date() or dt.date.today().strftime("%Y-%m-%d")
    return render_template("screen.html", page="screen",
                           strategies=st.list_strategies(),
                           universes=market.list_universes(),
                           trade_date=td)


@app.route("/stock/<code>")
def page_stock(code):
    # embed=1：「K线预览弹窗」用（选股策略试跑列表 / 今日选股结果里的「K线」按钮）。
    # 弹窗是同源 iframe 直接嵌这一页，只靠 CSS 隐藏侧栏、顶栏与三块非图表卡片，
    # **取数与图表代码一个字节都不变** —— 预览与诊断页看到的永远是同一份实现，
    # 弹窗里不会养出第二个 K 线口径（见 README「K线弹窗预览」）。
    return render_template("stock.html", page="stock", code=code,
                           strategies=st.list_strategies(),
                           embed=bool(request.args.get("embed")))


@app.route("/settings")
def page_settings():
    """模型设置：目前就两块 —— AI 接入（本地 Ollama / 远程 API）与 AI 语句模板。

    页面本身不传数据（配置里有 api_key，**只在接口里给脱敏版**，别塞进 HTML）。
    """
    return render_template("settings.html", page="settings")


@app.route("/watch")
def page_watch():
    """我的关注：自选股分组 + 提醒规则 + 触发记录。

    提醒判定口径（收盘价上穿/下穿、盘中触及）写在 `core/watch.py` 的模块头，
    页面上也照抄一份 —— 别让人以为「上穿」是盘中一碰就算。
    ⚠ 2026-10-08 加周K：`freqs / freq_order / default_freq` 与 `quick`（现在是
    `(周期, 线, 动作, 文案)` 四元组）必须一起透传 —— 页面上的周期下拉与两行一键按钮
    都按它们渲染，少传一个页面上就直接是「日K only」了（静默降级，不报错）。
    """
    watch.ensure_group()          # 首次进来自动建一个默认分组，免得对着一片空白
    return render_template("watch.html", page="watch",
                           lines=watch.LINES, ops=watch.OPS,
                           line_order=watch.LINE_ORDER, op_order=watch.OP_ORDER,
                           line_groups=watch.LINE_GROUPS, quick=watch.QUICK_RULES,
                           freqs=watch.FREQS, freq_order=watch.FREQ_ORDER,
                           default_line=watch.DEFAULT_LINE, default_op=watch.DEFAULT_OP,
                           default_freq=watch.DEFAULT_FREQ,
                           # ⚠ 规则类型（指标/价位 · 选股命中）与它的顺序、默认值 —— 同源 `watch.KINDS`。
                           #   页面上的类型切换按钮按它铺；写死在前端的话，以后加第三种规则
                           #   （比如「公告/事件」）就得改两个地方。
                           kinds=watch.KINDS, kind_order=watch.KIND_ORDER,
                           default_kind=watch.DEFAULT_KIND,
                           # ⚠ 「在布林下轨」的阈值与批量备注**从后端来**（口径唯一）：
                           #   页面确认框里那句「布林位置 ≤ 0.1」必须跟着 `watch.BOLL_LOWER_POS`
                           #   一起变，写死在模板里迟早跟后端判定对不上。
                           boll_pos=watch.BOLL_LOWER_POS, boll_note=watch.BOLL_LOWER_NOTE)


# ---------------------------------------------------------------------------
# 基础 API
# ---------------------------------------------------------------------------

@app.route("/api/stats")
def api_stats():
    return ok(market.data_stats(force=bool(request.args.get("force"))))


@app.route("/api/market/limit_stats")
def api_limit_stats():
    """涨停 / 跌停家数（按交易日，分主板 / 创业板 / 科创板 / ST / 总计）。

    板块与涨跌幅判定复用 `indicators._limit_pct` 的同一套规则。
    不传区间时默认「最近 3 个月」，与总览页图表默认区间一致。
    """
    end = (_date(request.args.get("end")) or market.latest_trade_date()
           or dt.date.today().strftime("%Y-%m-%d"))
    start = _date(request.args.get("start")) or _months_before(end, 3)
    if start > end:
        return err("起始日期不能晚于结束日期")
    return ok(market.limit_stats(start, end))


@app.route("/api/hot")
def api_hot():
    """热股榜：东方财富人气榜 / 同花顺热度榜 / 雪球沪深热股榜，每源前 N（默认 10）。

    ⚠ 这三个都是**站外第三方公开接口**（见 `core/hot.py`），与 tushare 无关、**不消耗额度**、
    不进 `sync_log`。站外就可能不通，所以**按源隔离失败**：某个源挂了只有它自己那张卡
    报错（`sources[].error`），另外两张卡和页面其余部分照常。`force=1` 跳过 60s 缓存。
    """
    try:
        n = _int(request.args.get("n"), 10, "n")
    except (TypeError, ValueError):
        return err("n 必须是整数")
    if not 1 <= n <= hot.MAX_N:
        return err(f"n 取值范围 1 ~ {hot.MAX_N}")
    return ok(hot.get_all(n, force=bool(request.args.get("force"))))


@app.route("/api/futures")
def api_futures():
    """今日期货：六市场全量合约 → 按品种分组（主连 + 同品种合约）。

    ⚠ 与 `/api/hot` 同类：都是**站外第三方公开接口**（见 `core/futures.py`），
    不消耗 tushare 额度、不进 `sync_log`。站外就可能不通，所以**按市场隔离失败**：
    某个交易所挂了只有它自己那行报错（`markets[].error`），其余市场照常。
    `force=1` 跳过 60s 缓存。
    """
    return ok(fut.get_all(force=bool(request.args.get("force"))))


# `code` 会被拼进 SQL（`futures_daily` 按 ts_code 查），先卡形状再放行。
# 期货 ts_code 形如 `RU.SHF` / `RU2511.SHF` / `TL0.CFX`：品种码（字母数字）+ 点 + 交易所后缀。
_TSCODE_RE = re.compile(r"^[A-Za-z0-9]{1,12}\.[A-Za-z]{3,5}$")


@app.route("/api/futures/kline")
def api_futures_kline():
    """期货 K 线（**读本地库** `futures_daily`，不出网）。

    参数：`secid`（现在就是 tushare 的 ts_code，形如 `RU.SHF` / `RU2511.SHF`）、
    `klt`（101 日 / 102 周 / 103 月，默认日）、`limit`（根数，默认 250，上限 1000）。
    周/月线是日线**即时聚合**的（不落库），最后一根可能是不完整的周/月。
    """
    secid = (request.args.get("secid") or "").strip()
    if not _TSCODE_RE.match(secid):
        return err("secid 形状不对，应形如 RU.SHF")
    try:
        klt = _int(request.args.get("klt"), 101, "klt")
        limit = _int(request.args.get("limit"), 250, "limit")
    except (TypeError, ValueError):
        return err("klt / limit 必须是整数")
    if klt not in fut.KLT.values():
        return err("klt 只支持 101(日) / 102(周) / 103(月)")
    try:
        return ok(fut.kline(secid, klt, limit))
    except Exception as e:                          # noqa: BLE001 —— 站外接口
        return err(f"取 K 线失败：{e}", 502)


@app.route("/api/market/trade_days")
def api_trade_days():
    """交易日历（只返回开市日），给「分时监控」页判断今天该不该取数用。

    分时监控的**行情**仍然直连腾讯接口、不经过本站；但「今天是不是交易日」这件事得问日历 ——
    只按周一~周五近似，国庆/春节这种长假会白轮询一整天。日历覆盖不到的日期前端会退回周末近似。
    """
    today = dt.date.today()
    start = _date(request.args.get("start")) or today.isoformat()
    end = _date(request.args.get("end")) or (today + dt.timedelta(days=30)).isoformat()
    if start > end:
        return err("起始日期不能晚于结束日期")
    if (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days > 400:
        return err("区间过大（最多 400 天）")
    return ok({"start": start, "end": end, "days": market.get_trade_dates(start, end)})


def _page_to_ts_code(code: str) -> str:
    """「分时监控」页的代码 → 库里的 ts_code。

    页面用腾讯那套 `sh600519` 前缀写法，库里是 tushare 的 `600519.SH`。
    只写 6 位数字时按首位补市场（6→沪、0/3→深、4/8/9→北），与页面 `normCode` 同一套规则。
    认不出来就返回空串（调用方跳过这一只，不报错 —— 页面本来就是用户随手输代码的地方）。
    """
    s = re.sub(r"[^A-Za-z0-9.]", "", str(code or "")).upper()
    m = re.fullmatch(r"(SH|SZ|BJ)(\d{6})", s)
    if m:
        return f"{m.group(2)}.{m.group(1)}"
    if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", s):
        return s
    m = re.fullmatch(r"(\d{6})", s)
    if m:
        c = s[0]
        mk = "SH" if c == "6" else ("SZ" if c in "03" else ("BJ" if c in "489" else ""))
        return f"{s}.{mk}" if mk else ""
    return ""


@app.route("/api/monitor/kline")
def api_monitor_kline():
    """「分时监控」页切到日K视图时用的日线：读本地 daily_kline，**默认不含当天**。

    这一页的**分时**行情是前端直连腾讯接口的（那个接口只给当日快照、没有历史日线），
    日线历史只在本地库里 —— 所以切日K时由这里供给。一次可问多只（逗号分隔），
    与页面「一个请求覆盖全部宫格」的口径一致。
    **当天那根日K不由这里返回**：日线是收盘后才同步的，库里根本没有当天那一行；
    它由前端拿浏览器本地缓存现算（每整分钟的全字段存档里最高/最低/量/额都是「当日累计」口径，
    再叠上最新快照的今开与最新价）。所以 `end` 默认就是昨天，传今天也会被夹回昨天。
    价格按 `config.ADJUST_MODE`（默认前复权）算 —— 窗口内最后一根的复权因子恒为 1，
    与页面上看到的实时价可比。返回里 amount 统一换成**万元**（库里存的是千元），
    与腾讯行情字段 f[37] 同口径，前端两个来源才能直接拼一根 K 线。
    """
    raw = (request.args.get("codes") or request.args.get("code") or "").strip()
    if not raw:
        return err("缺少 codes")
    page_codes = [c for c in re.split(r"[,\s]+", raw) if c]
    if len(page_codes) > 12:
        return err("一次最多 12 只")
    try:
        bars = _int(request.args.get("bars"), 60, "bars")
    except ValueError:
        return err("bars 必须是数字")
    bars = max(5, min(bars, 250))

    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    end = _date(request.args.get("end")) or yesterday
    if end >= dt.date.today().isoformat():          # 当天不从这里取，由前端本地现算
        end = yesterday

    out: dict[str, list] = {}
    for pc in page_codes:
        ts = _page_to_ts_code(pc)
        if not ts:
            continue
        rows = db.query(
            "SELECT trade_date,open,high,low,close,pre_close,pct_chg,vol,amount,adj_factor "
            "FROM daily_kline WHERE ts_code=%s AND trade_date<=%s "
            "ORDER BY trade_date DESC LIMIT %s", (ts, end, bars))
        if not rows:
            continue
        # 查询是倒序取的（只为走主键索引），复权要按日期升序算，所以先翻回来
        df = pd.DataFrame(rows).iloc[::-1].reset_index(drop=True)
        for c in ("open", "high", "low", "close", "pre_close", "pct_chg",
                  "vol", "amount", "adj_factor"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = market.apply_adjust(df, ADJUST_MODE)

        def _r(v, nd=None):
            """NaN → None；给了 nd 才四舍五入。

            **价格不取整**（nd=None）：前端要拿这些收盘价现算 MA/BOLL/跳空，
            取整到 3 位会让 60 日均线与策略面板差到 1e-6 量级 —— 两套数看着一样、
            其实不是一个口径，正是这个项目最忌讳的那种错。价格原样出（前端只显示 2 位）。
            量/额只用于显示，照旧取整（位数大，取整对显示无损、还能压一点体积）。"""
            try:
                if v is None or pd.isna(v):
                    return None
                f = float(v)
                return f if nd is None else round(f, nd)
            except (TypeError, ValueError):
                return None

        seq = []
        for r in df.to_dict("records"):
            if not (r.get("close") or 0) > 0:
                continue
            seq.append({
                "date": str(r["trade_date"])[:10],
                "open": _r(r.get("open")), "high": _r(r.get("high")),
                "low": _r(r.get("low")), "close": _r(r.get("close")),
                "pre_close": _r(r.get("pre_close")),
                "pct": _r(r.get("pct_chg"), 4),
                "vol": _r(r.get("vol"), 2),                 # 手
                "amount": _r((r.get("amount") or 0) / 10.0, 2),   # 千元 → 万元
            })
        if seq:
            out[pc] = seq

    miss = [c for c in page_codes if c not in out]
    if miss:
        log.info("日K线 %s 只无本地日线：%s", len(miss), miss)
    return ok({"bars": out, "missing": miss, "bars_per_code": bars, "end": end,
               "adjust": ADJUST_MODE, "units": {"price": "元", "vol": "手", "amount": "万元"}})


@app.route("/api/config")
def api_config():
    return ok({
        "token_ok": bool(TUSHARE_TOKEN),
        "token_masked": (TUSHARE_TOKEN[:6] + "…" + TUSHARE_TOKEN[-4:]) if TUSHARE_TOKEN else "",
        "defaults": BACKTEST_DEFAULTS,
        "today": dt.date.today().strftime("%Y-%m-%d"),
        "latest_trade_date": market.latest_trade_date(),
        "db": {k: v for k, v in db.MYSQL.items() if k != "password"},
        # 选股策略的「静态过滤项」默认值 —— 唯一来源是 universe.DEFAULT_FILT，
        # 前端「新建」时按它铺初值，别在页面里再抄一份默认（改一处生效）。
        "filt_defaults": uni.DEFAULT_FILT,
        # 买卖策略里的「滑点」可填范围（小数）—— 唯一来源 strategy.SLIPPAGE_MIN/MAX。
        # 界面按**百分数**摆（×100），别在页面里写死 0~3。
        "slippage_range": {"min": st.SLIPPAGE_MIN, "max": st.SLIPPAGE_MAX},
        # 回测数据缓存的开关与护栏 —— 唯一来源 config.BACKTEST_CACHE*。
        # 只做「可查」，命中与否在回测记录的首行日志里如实标注，不另开运维页面。
        "cache": {
            "enabled": BACKTEST_CACHE,
            "ttl_days": BACKTEST_CACHE_TTL_DAYS,
            "max_mb": BACKTEST_CACHE_MAX_MB,
        },
    })


@app.route("/api/universes")
def api_universes():
    """静态股票池列表。条件池是「规则」不是固定名单，走 /api/universe/presets + 前端编辑器。

    返回结构保持扁平数组（数据管理页直接 map 渲染），新增字段是纯附加信息。
    """
    static = market.list_universes()
    for u in static:
        u["kind"] = "static"
        u["spec"] = u["code"]
    return ok(static)


@app.route("/api/universe/presets")
def api_universe_presets():
    return ok(uni.PRESETS)


# ---------------------------------------------------------------------------
# 选股策略（条件股票池）CRUD —— 股票池下拉框的数据源
# ---------------------------------------------------------------------------


@app.route("/api/pools")
def api_pools():
    return ok([pool_mod.describe(p) for p in pool_mod.list_pools()])


@app.route("/api/pools/<int:pid>")
def api_pool_get(pid):
    p = pool_mod.get_pool(pid)
    if not p:
        return err("选股策略不存在", 404)
    return ok(pool_mod.describe(p))


def _norm_conds(conds) -> list[str]:
    """条件列表归一化：支持数组，也支持多行文本/分号分隔。

    ⚠ 这里**只删空行、不剥 `#` 注释** —— 存库与回显保留用户写的批注原文。
    注释是在「要执行」那一层剥的（`universe.parse` → `expr.split_conds`），
    「入池条件 ≥1 条」这类**校验**也走 parse，所以只写注释会被照实判成没条件。
    """
    if isinstance(conds, str):
        conds = [x.strip() for x in conds.replace("；", "\n").replace(";", "\n").split("\n")]
    return [str(x).strip() for x in (conds or []) if str(x).strip()]


@app.route("/api/pools", methods=["POST"])
def api_pool_save():
    b = body()
    name = (b.get("name") or "").strip()
    base = (b.get("base") or "").strip() or uni.UNIVERSE_ALL
    conds = _norm_conds(b.get("conds"))
    filt = uni.norm_filt(b.get("filt"))
    errs = pool_mod.validate(name, base, conds)
    if errs and not b.get("force"):
        log.warning("选股策略校验不通过 name=%s errs=%s", name, errs)
        return jsonify({"ok": False, "error": "；".join(errs), "errors": errs}), 400
    pid = pool_mod.save_pool(name, b.get("note") or "", base, conds,
                             b.get("id"), filt=filt,
                             timeframe=b.get("timeframe"))
    log.info("选股策略已保存 id=%s name=%s base=%s timeframe=%s conds=%s filt=%s",
             pid, name, base, st.norm_freq(b.get("timeframe")), conds, filt)
    return ok(pool_mod.describe(pool_mod.get_pool(pid)))


@app.route("/api/pools/<int:pid>", methods=["DELETE"])
def api_pool_delete(pid):
    if not pool_mod.delete_pool(pid):
        log.warning("删除被拒（内置策略）id=%s", pid)
        return err("内置策略不可删除（可复制后另存修改）", 400)
    log.info("选股策略已删除 id=%s", pid)
    return ok()


@app.route("/api/pools/validate", methods=["POST"])
def api_pool_validate():
    """只校验不落库，编辑页「校验」按钮用。"""
    b = body()
    errs = pool_mod.validate((b.get("name") or "").strip(),
                             b.get("base") or uni.UNIVERSE_ALL,
                             _norm_conds(b.get("conds")))
    return ok({"valid": not errs, "errors": errs})


@app.route("/api/pools/builtins", methods=["POST"])
def api_pool_builtins():
    n = pool_mod.install_builtins(force=bool(body().get("force")))
    return ok({"installed": n})


@app.route("/api/universe/fields")
def api_universe_fields():
    """条件池/选股策略表达式可用字段（含 `w_` 周线系列）。

    ⚠ 字段清单**必须与 `/api/fields` 同一来源**（`strategy.condition_field_*`，
    它的根又是 `indicators.FIELD_HELP`）。以前这里是拿 FIELD_HELP 逐行拆名字拼的，
    于是「拆不出真字段名的那一行」整行丢失：`ma5…ma250` 的省略号过不了
    `FIELD_RE` ⇒ ma5/ma20/ma60、boll_dev、ret60、pos120、ma_bull… 十几个字段
    在「可用字段参考」里**根本查不到**，反倒把 `ma5…ma250` 这个不是字段名的东西
    列了出来（2026-09-20 修）。`group` 仍从 FIELD_HELP 取（同一行一起说明），
    只影响展示分组，不影响字段集合。
    """
    from core import indicators as ind, strategy as st
    daily = st.condition_field_help()
    group: dict[str, str] = {}
    order: list[str] = []
    for names, _desc in ind.FIELD_HELP:
        for part in names.split(" / "):
            part = part.strip()
            if part in daily:
                order.append(part)
                group.setdefault(part, names)
    seen: set[str] = set()
    out = []
    for n in order + sorted(set(daily) - set(order)):
        if n in seen:
            continue
        seen.add(n)
        out.append({"name": n, "desc": daily[n], "group": group.get(n, "")})
    for n, desc in st.condition_weekly_field_help().items():
        if n not in seen:
            seen.add(n)
            out.append({"name": n, "desc": desc, "group": "周线字段（w_ 前缀）"})
    return ok(out)


@app.route("/api/fields/sample")
def api_field_sample():
    """某个可用字段的**实际取值参考**（「可用字段参考」里点「示例」）。

    值走 `screener.field_sample` → `market.load_panel`：复权、周线 asof、筹码、
    资金流全在那一条路上，与选股**同源**。自己写条 SQL 统计会当场养出第二个口径
    —— 价格类字段最明显（选股用前复权，表里存的是未复权）。

    样本股 = 当日成交额前 N 只（全市场一遍 80 秒，弹窗等不起），弹窗上写明样本口径。
    """
    name = (request.args.get("name") or "").strip()
    date = _date(request.args.get("trade_date"))
    try:
        data = screener.field_sample(name, date)
    except ValueError as e:
        return err(str(e))
    return ok(data)


# 试跑结果表**固定显示**的那几列（页面写死：代码/名称/行业 + 收盘/涨跌幅/总市值/换手%）。
# 入池条件引用到的字段要摊到表上当「额外列」时，已经在固定列里的就不再出第二遍。
# ⚠ 与「今日选股」的 `screener.FIXED_COLS` 是同一**意图**、但不是同一份清单（那边固定列更多，
#   还有成交额/主力净额），所以各写一份；两边改动时都想想另一处。
POOL_TEST_FIXED_COLS = {"close", "pct_chg", "total_mv", "turnover_rate"}


@app.route("/api/universe/test", methods=["POST"])
def api_universe_test():
    """试跑条件池：返回某日基础池大小、预筛后大小、当日实际入池只数与样例个股。

    只做「池子」判定，不跑策略信号，所以很快，适合边调条件边看效果。

    `sort_expr`（选填）= 「排序表达式」，与今日选股页那一条**同一个口径**：
    数值越大越靠前。**排序与评分是同一个东西** —— 同一个式子既定顺序、又是结果表
    `评分` 列的值（否则就成了「按一个看不见的东西排序」）。填了**只覆盖本次试跑**，
    不落库、不动任何策略；没填则按 ts_code 升序（保持老行为，不替用户猜）。
    式子写错**当场 400**，不能安静地按「空评分」跑完一屏乱序的结果。
    """
    b = body()
    spec = b.get("universe") or {"base": "all", "where": []}
    if not uni.is_cond(spec):
        spec = {"base": spec if isinstance(spec, str) else "all", "where": []}
    else:
        # 展开 {"pool_id": N} → {base, where, filt, timeframe}，否则 base 会取不到策略的基础池
        spec = uni.parse(spec)
    date = _date(b.get("trade_date"), market.latest_trade_date() or "")
    if not date:
        return err("本地无行情数据")
    sample_limit = _int(b.get("sample"), 12, "sample")

    known = set(st.condition_field_names())
    errs = uni.validate(spec, known)
    if errs:
        return jsonify({"ok": False, "error": "；".join(errs), "errors": errs}), 400
    sort_expr = str(b.get("sort_expr") or "").strip()
    if sort_expr:
        okk, msg = expr.check_expr(sort_expr, known)
        if not okk:
            one = f"[排序表达式] {msg}"
            return jsonify({"ok": False, "error": one, "errors": [one]}), 400

    t0 = time.time()
    base_codes = market.resolve_universe(spec.get("base"))
    codes = market.resolve_universe(spec, start=date, end=date)
    # 池子条件可能用到 boll_upper 等指标，按池子实际引用的字段裁剪计算
    cfg = {"pool": dict(st.DEFAULT_POOL), "_pool": uni.parse(spec)}
    # 排序表达式要用的字段必须进 only= —— 面板没算那一列，评分就恒空、顺序悄悄退回
    # 代码升序（表现是「填了式子没反应」，最难查的那种）。
    if sort_expr:
        cfg["score_expr"] = sort_expr
    # ⚠ 周期跟着选股策略走，**唯一判定处**是 `st.selection_cfg`：池子是周K ⇒ 整条 cfg
    #   抬到周线（条件里的 `w_` 前缀也会被去掉，与选股/回测同一条路）。
    #   若在这里自己手工分支，就成了「试跑说命中、选股说没有」的第二个口径。
    cfg = st.selection_cfg(cfg)
    fq = cfg["timeframe"]
    if fq == "weekly" and not market.is_week_close_day(date):
        last_wk = market.last_week_close_day(date)
        return err(f"{date} 不是本周的最后一个交易日（本周还没收盘），周K 策略没有可用的周K。"
                   f"等本周收盘后同步再跑，或把试跑日改成最近一个周收盘日 {last_wk or '—'}。")
    # 结果表的额外列 = 入池条件/排序表达式**真正引用到的字段**（唯一来源
    # `strategy.referenced_fields`，与「今日选股」结果表同一口径），减去页面已固定显示的几列。
    # 这样「我这条条件盯的是什么」在表上直接看得见，不用逐只点开诊断页核对。
    from core import indicators as ind      # 列定义（表头/小数位/格式）的唯一来源
    show = [n for n in st.referenced_fields(cfg) if n not in POOL_TEST_FIXED_COLS]
    # 静态过滤项（剔ST/板块/上市天数）现在挂在选股策略上 —— 试跑也要照它筛，
    # 否则页面上勾了「剔除 ST」却看到 ST 股在样例里，等于骗人。
    filt = st.pool_filters(cfg)
    only = st.required_daily_fields(cfg) | {"total_mv", "turnover_rate", "circ_mv",
                                           "volume_ratio", "amount", "close"}
    # 周K 策略下它是 None（面板本身每根就是完整周，不需要再贴 w_ 列）
    wf = st.required_weekly_fields(cfg)
    hit: list[dict] = []
    for i in range(0, len(codes), 400):
        part = codes[i:i + 400]
        if fq == "weekly":
            # 周K 策略：整条链在**周线**上求值（`close` = 周收盘、`ma5` = 5 周均线）
            panel = market.compute_weekly_panel(
                part, screener._lookback_start(date, 150, "weekly"), date,
                indicators_cfg=cfg.get("indicators"), only=only)
        else:
            panel = market.load_panel(part, _lookback(date, 320), date, adjust="qfq",
                                      weekly=bool(wf), weekly_fields=wf, only=only)
        panel = market.apply_pool_filters(panel, filt)
        for code, df in panel.items():
            if df is None or not len(df):
                continue
            if str(df["trade_date"].iloc[-1])[:10] != date:
                continue
            if bool(st.compute_pool(df, cfg)[-1]):
                d = df.iloc[-1]
                sc = None
                if sort_expr:
                    # 评分只对**命中股**算：命中通常几十只，比全市场便宜得多。
                    # 走 compute_signals 的 score 通路（与选股结果表的「评分」同源），
                    # 别在这里另写一遍求值 —— 那就是第二个口径。
                    try:
                        sv = st.compute_signals(df, cfg)["score"].iloc[-1]
                        sc = None if pd.isna(sv) else round(float(sv), 4)
                    except expr.ExprError:
                        sc = None
                item = {
                    "ts_code": code,
                    "name": d.get("_name", ""),
                    "industry": d.get("industry", ""),
                    "close": _num(d.get("close")),
                    "pct_chg": _num(d.get("pct_chg")),
                    "total_mv": _num(d.get("total_mv")),
                    "circ_mv": _num(d.get("circ_mv")),
                    "turnover_rate": _num(d.get("turnover_rate")),
                    "boll_upper": _num(d.get("boll_upper")),
                    "pre_pct_chg": _num(df["pct_chg"].iloc[-2]) if len(df) > 1 else None,
                    "score": sc,
                }
                # 条件引用到的字段逐只带出来（含筹码/两融、周线 w_ 字段）。与「今日选股」
                # 结果表同一做法：前后端都不写死字段名 —— 列头与格式由 `indicators.col_spec` 给。
                for k in show:
                    if k in df.columns:
                        item[k] = _num(d.get(k))
                hit.append(item)
        del panel
    # 顺序：填了表达式就按它（评分降序、缺分沉底、同分按代码升序）；没填保持代码升序。
    if sort_expr:
        hit.sort(key=lambda x: (-(x["score"] if x["score"] is not None else -1e18),
                                x["ts_code"]))
    else:
        hit.sort(key=lambda x: x["ts_code"])
    # 「式子合法但一只都算不出分」（字段在清单里、当日却没数据）→ 顺序会看着像没生效。
    # 这种情况必须留声，否则用户只会觉得「填了没用」。
    sort_note = ""
    if sort_expr and hit and all(x["score"] is None for x in hit):
        sort_note = (f"排序表达式「{sort_expr}」在全部 {len(hit)} 只命中股上都算不出值"
                     f"（该字段当日可能没有数据），顺序已退回代码升序。")
        log.warning("条件池试跑排序失真 %s @%s：%s", uni.label(spec), date, sort_note)
    log.info("条件池试跑 %s @%s：基础 %s 只 / 预筛 %s 只 / 当日入池 %s 只，排序=%s，耗时 %.1fs",
             uni.label(spec), date, len(base_codes), len(codes), len(hit),
             sort_expr or "代码升序", time.time() - t0)
    return ok({
        "trade_date": date,
        "label": uni.label(spec),
        "describe": uni.describe(spec),
        "base_size": len(base_codes),
        "loaded_size": len(codes),
        "preselected": len(codes) < len(base_codes),
        "preselect_note": uni.preselect_note(spec),
        "hit_count": len(hit),
        # 本次**实际用来排序**的表达式 —— 前端写在结果卡上，别让人对着顺序猜
        "sort_expr": sort_expr,
        "sort_note": sort_note,
        # 额外列定义（表头/小数位/格式）—— 只给**这批命中真有值**的列，与「今日选股」同源
        # （`indicators.col_spec`）。页面照单渲染，不写死任何字段名。
        "extra_cols": ([ind.col_spec(k) for k in show if k in hit[0]] if hit else []),
        "samples": hit[:sample_limit],
        "elapsed": round(time.time() - t0, 2),
    })


def _num(v):
    try:
        if v is None or pd.isna(v):
            return None
        return round(float(v), 4)
    except (TypeError, ValueError):
        return None


def _lookback(date: str, bars: int = 320) -> str:
    d = pd.Timestamp(date)
    return (d - pd.Timedelta(days=int(bars * 7 / 5) + 60)).strftime("%Y-%m-%d")


@app.route("/api/fields")
def api_fields():
    """条件表达式可用的字段与函数。前端提示、帮助面板都取这里，不再各自维护一份。"""
    daily = st.condition_field_help()
    return ok({
        "fields": st.condition_field_names(),
        "field_help": {**daily, **st.condition_weekly_field_help()},
        "functions": expr.describe_functions(),
        "periods": sorted(daily),
    })


@app.route("/api/trade_dates")
def api_trade_dates():
    start = _date(request.args.get("start"))
    end = _date(request.args.get("end"))
    return ok(market.get_trade_dates(start, end)[-400:])


# ---------------------------------------------------------------------------
# 数据同步
# ---------------------------------------------------------------------------

@app.route("/api/sync/start", methods=["POST"])
def api_sync_start():
    b = body()
    start = _date(b.get("start"), DEFAULT_SYNC_START)
    end = _date(b.get("end"), dt.date.today().strftime("%Y-%m-%d"))
    if start > end:
        return err("起始日期不能晚于结束日期")
    jid = jobs.new_job("sync", f"数据同步 {start} ~ {end}")
    log.info("数据同步提交 %s ~ %s（含基础信息=%s，含资金流向=%s，含股东/两融=%s，含期货=%s）",
             start, end, bool(b.get("with_basic", True)),
             bool(b.get("with_moneyflow", True)), bool(b.get("with_holders", True)),
             bool(b.get("with_futures", True)))

    def task():
        return sync.full_sync(start, end, jid=jid,
                              with_basic=bool(b.get("with_basic", True)),
                              force=bool(b.get("force", False)),
                              include_base=bool(b.get("include_base", True)),
                              with_moneyflow=bool(b.get("with_moneyflow", True)),
                              with_holders=bool(b.get("with_holders", True)),
                              with_futures=bool(b.get("with_futures", True)))
    jobs.run_async(jid, task, "数据同步完成")
    return ok({"job_id": jid})


@app.route("/api/sync/base", methods=["POST"])
def api_sync_base():
    jid = jobs.new_job("sync_base", "同步基础信息")
    log.info("同步基础信息提交")

    def task():
        n1 = sync.sync_stock_basic(jid)
        n2 = sync.sync_trade_cal("2010-01-01", jid=jid)
        try:
            n3 = sync.sync_index_member(jid=jid)
        except Exception:  # noqa: BLE001
            n3 = 0
        try:
            n4 = sync.sync_index_daily(jid=jid)
        except Exception:  # noqa: BLE001
            n4 = 0
        return {"stock_basic": n1, "trade_cal": n2, "index_member": n3,
                "index_daily": n4}
    jobs.run_async(jid, task, "基础数据同步完成")
    return ok({"job_id": jid})


@app.route("/api/sync/weekly", methods=["POST"])
def api_sync_weekly():
    """单独合成周线。默认**只重算需要重算的那几周**（最近一个完整周 + 本次日线真正
    变动到的周）；勾了「强制重新拉取」就整段重算（见 `sync.weekly_since`）。"""
    b = body()
    start = _date(b.get("start"), DEFAULT_SYNC_START)
    end = _date(b.get("end"), dt.date.today().strftime("%Y-%m-%d"))
    force = bool(b.get("force"))
    jid = jobs.new_job("weekly", "合成周线")
    log.info("周线合成提交 %s ~ %s（%s）", start, end,
             "整段重算" if force else "只补需要重算的周")

    def task():
        return {"weekly_rows": sync.build_weekly(start, end, jid,
                                                 changed=start if force else None)}
    jobs.run_async(jid, task, "周线合成完成")
    return ok({"job_id": jid})


@app.route("/api/sync/moneyflow", methods=["POST"])
def api_sync_moneyflow():
    """单独补个股资金流向（可与日线分片并行跑，不同日期区间互不冲突）。"""
    b = body()
    start = _date(b.get("start"), DEFAULT_SYNC_START)
    end = _date(b.get("end"), dt.date.today().strftime("%Y-%m-%d"))
    if start > end:
        return err("起始日期不能晚于结束日期")
    jid = jobs.new_job("money_flow", f"资金流向 {start} ~ {end}")
    log.info("资金流向同步提交 %s ~ %s（force=%s）", start, end, bool(b.get("force")))

    def task():
        return sync.sync_moneyflow(start, end, jid, force=bool(b.get("force")))
    jobs.run_async(jid, task, "资金流向同步完成")
    return ok({"job_id": jid})


@app.route("/api/sync/holders", methods=["POST"])
def api_sync_holders():
    """单独补「股东户数 / 融资融券 / 股东增减持」。

    三个接口的时间轴不同（公告类按**自然日**、两融按**交易日**），但都按日整块拉全市场，
    彼此独立、互不重叠，可以和日线分片并行跑。
    """
    b = body()
    start = _date(b.get("start"), DEFAULT_SYNC_START)
    end = _date(b.get("end"), dt.date.today().strftime("%Y-%m-%d"))
    if start > end:
        return err("起始日期不能晚于结束日期")
    jid = jobs.new_job("holders", f"股东/两融数据 {start} ~ {end}")
    kind = str(b.get("kind") or "all")     # all / holder_number / margin_detail / holder_trade
    log.info("股东/两融同步提交 %s ~ %s（kind=%s，force=%s）", start, end, kind,
             bool(b.get("force")))

    def task():
        force = bool(b.get("force", False))
        out: dict = {}
        for key, fn, band in (("holder_number", sync.sync_holder_number, (0.0, 0.34)),
                              ("margin_detail", sync.sync_margin_detail, (0.34, 0.67)),
                              ("holder_trade", sync.sync_holder_trade, (0.67, 1.0))):
            if kind not in ("all", key):
                continue
            out[key] = fn(start, end, jid, force=force, p0=band[0], p1=band[1])
        return out
    jobs.run_async(jid, task, "股东/两融数据同步完成")
    return ok({"job_id": jid})


@app.route("/api/sync/futures", methods=["POST"])
def api_sync_futures():
    """单独同步**期货**（合约基本信息 + 日线 + 在市期权），与股票那几块互不相干。

    主连拉**全历史**、具体合约拉**近 1 年**（`contract_days` 可改，默认 365）、
    期权只拉**在市**的（给期货页展开行补「对应期权到期日」）。
    增量：主连按「库里最后一天 +1」续；具体合约按「这天有没有合约数据」跳过，
    所以重复点是安全的、断点也能续；期权是全量重拉（约 12s），upsert 幂等。
    """
    b = body()
    try:
        days = _int(b.get("contract_days"), 365, "contract_days")
    except (TypeError, ValueError):
        return err("contract_days 必须是整数")
    days = max(30, min(days, 3650))
    force = bool(b.get("force"))
    jid = jobs.new_job("futures", f"期货同步（合约近 {days} 天）")
    log.info("期货同步提交：contract_days=%s force=%s", days, force)

    def task():
        return sync.sync_futures(contract_days=days, jid=jid, force=force)
    jobs.run_async(jid, task, "期货同步完成")
    return ok({"job_id": jid})


@app.route("/api/sync/concept", methods=["POST"])
def api_sync_concept():
    """单独同步**东财概念**（F10 核心题材）。

    ⚠ 与股票那几块**不同源**：走的是东方财富公开接口，**不消耗 tushare 额度、
    也不进 `sync_log`**（那张表只记本地库的写入任务，与热股榜同一个规矩）。
    ⚠ 全市场一遍要 **40 分钟上下**（东财限频，实测 ~2.3 只/秒）—— 跑的时候别改 .py：
    `debug=True` 的 reloader 会重起子进程，内存里的任务表跟着清空（进度条就断了）。

    body 可选 `codes`：只补指定的几只（`["000001.SZ", ...]` 或空格/逗号分隔的字符串）。
    """
    b = body()
    codes = b.get("codes") or None
    if isinstance(codes, str):
        codes = [c.strip().upper() for c in codes.replace(",", " ").split() if c.strip()]
    jid = jobs.new_job("concept", "东财概念同步")
    log.info("概念同步提交：%s", f"{len(codes)} 只" if codes else "全部在市股票")

    def task():
        return concept.sync_concepts(codes, jid=jid)
    jobs.run_async(jid, task, "概念同步完成")
    return ok({"job_id": jid, "coverage": concept.coverage()})


@app.route("/api/concept/coverage")
def api_concept_coverage():
    """概念数据的覆盖情况（数据页上那行小字）。"""
    return ok(concept.coverage())


@app.route("/api/concept/<board_code>/stocks")
def api_concept_stocks(board_code):
    """**一条概念 → 一批票**（诊断页双击概念标签看成分股）。

    与 `/api/stock/<code>/detail` 里的 `concepts` 方向相反：那边是一只票 → 它的概念。

    参数：`trade_date`（默认最新交易日；页面锚点是哪天就传哪天，**不回退**）、
    `limit`（默认 100 / 上限 3000）、`order`（`pct` 涨跌幅降序[默认] / `pct_asc` / `code`）。
    行情取**本库日线**，所以列里的收盘与涨跌幅跟页面上其他数字同源；那天没有行情的
    票（退市/停牌）不列进去，只在 note 里报个数。
    """
    args = request.args
    try:
        limit = int(args.get("limit") or concept.MEMBERS_LIMIT)
    except (TypeError, ValueError):
        return err("limit 必须是整数")
    return ok(concept.concept_members(
        board_code, trade_date=(args.get("trade_date") or None),
        limit=limit, order=(args.get("order") or "pct")))


@app.route("/api/newconcept")
def api_newconcept():
    """东财「新概念速览」：新概念清单 + 概念板块成分变更（总览卡片）。

    与 `/api/concept/coverage` 那条 F10 题材库**不是一回事**（见 `core/newconcept.py` 头部的
    口径说明）。

    ⚠ **每次调用都实时去东财抓一遍**（2026-10-08 取消本地缓存：需求改了，不再「今日已抓过
    就跳过」）。抓失败不会返空 —— 退回上一次的快照，并带 `stale=true` + `error`，
    页面据此提示「实时抓取失败，显示上次数据」。
    `?refresh=0` 可关掉实时抓取，只读本地快照（自检 / 断网场景）。
    """
    try:
        days = _int(request.args.get("days"), 1, "days")
    except (TypeError, ValueError):
        return err("days 必须是整数")
    refresh = str(request.args.get("refresh", "1")).lower() not in ("0", "false", "no")
    return ok(newconcept.overview(days=max(1, min(days, 30)), refresh=refresh))


@app.route("/api/newconcept/sync", methods=["POST"])
def api_newconcept_sync():
    """抓一次东财新概念并落库（0.5 秒的事，同步做）。

    ⚠ 取消缓存后这里**每次都是真抓**（`force` 参数已无实际作用，只为兼容旧前端留着）。
    返回里那份 `overview` 传 `refresh=False` —— 刚抓完就别再抓一遍了。
    """
    try:
        r = newconcept.sync(force=bool(body().get("force")))
    except Exception as e:                      # noqa: BLE001 —— 站外接口
        log.warning("新概念同步失败：%s", e)
        return err(f"抓取失败：{e}", 502)
    return ok({"sync": r, "coverage": newconcept.coverage(),
               "overview": newconcept.overview(refresh=False)})


@app.route("/api/newconcept/events")
def api_newconcept_events():
    """最近几天的概念板块成分变更流水（关注页 / 展开看历史）。"""
    try:
        days = _int(request.args.get("days"), 5, "days")
    except (TypeError, ValueError):
        return err("days 必须是整数")
    return ok(newconcept.events(days=max(1, min(days, 60))))


# ---------------------------------------------------------------------------
# 我的关注（自选股 + 提醒）
# ---------------------------------------------------------------------------

@app.route("/api/watch/meta")
def api_watch_meta():
    """提醒表单要用的选项（周期 / 线名 / 动作 / 口径说明）—— 唯一来源 `core/watch.py`。"""
    return ok({
        "lines": [{"key": k, "label": v[0], "group": v[2]} for k, v in watch.LINES.items()],
        "line_groups": watch.LINE_GROUPS,
        "ops": [{"key": k, "label": v} for k, v in watch.OPS.items()],
        # 周期：`daily` 判日线那根、`weekly` 判**周线**那根（只认已收盘的完整周）。
        "freqs": [{"key": k, "label": v} for k, v in watch.FREQS.items()],
        "freq_order": watch.FREQ_ORDER, "default_freq": watch.DEFAULT_FREQ,
        # 规则类型：`price` 指标/价位 · `pool` 选股命中（2026-10-10 加）。
        "kinds": [{"key": k, "label": v} for k, v in watch.KINDS.items()],
        "kind_order": watch.KIND_ORDER, "default_kind": watch.DEFAULT_KIND,
        "default_line": watch.DEFAULT_LINE, "default_op": watch.DEFAULT_OP,
        "quick": [{"freq": a, "line": b, "op": c, "label": d}
                  for a, b, c, d in watch.QUICK_RULES],
        "note": "上穿/下穿按**收盘价**判（前一根在下方、这根在上方）；"
                "「触及」是**盘中**口径（当日最低~最高之间碰到线）。均线/布林为**前复权**，"
                "与选股、回测、个股诊断页同一个来源。"
                "周期选周K 时判的是**周线那一根**（数据源只含已收盘的完整周，"
                "所以周K 要等这周走完才出结果，一周最多一条）。",
        "summary": watch.summary(),
    })


@app.route("/api/watch/groups")
def api_watch_groups():
    return ok({"groups": watch.list_groups(), "summary": watch.summary()})


@app.route("/api/watch/group/save", methods=["POST"])
def api_watch_group_save():
    b = body()
    try:
        gid = watch.save_group(b.get("name"), b.get("note") or "",
                               _int(b.get("id"), None, "id"))
    except ValueError as e:
        return err(str(e))
    return ok({"id": gid, "groups": watch.list_groups()})


@app.route("/api/watch/group/delete", methods=["POST"])
def api_watch_group_delete():
    gid = body().get("id")
    if not gid:
        return err("缺 id")
    return ok({"result": watch.delete_group(_int(gid, None, "id")), "groups": watch.list_groups()})


@app.route("/api/watch/stocks")
def api_watch_stocks():
    try:
        gid = _int(request.args.get("group_id"), 0, "group_id") or None
    except (TypeError, ValueError):
        return err("group_id 必须是整数")
    # `trade_date` = 这批 self.close/pct_chg 取的是哪一天（本库最近交易日）。
    # 前端拿它当 K线预览的锚点，预览窗口才与表里那两列同一天（C8：预览必须带 trade_date）。
    return ok({"stocks": watch.list_stocks(gid), "alerts": watch.list_alerts(),
               "trade_date": market.latest_trade_date()})


@app.route("/api/watch/stocks/add", methods=["POST"])
def api_watch_stocks_add():
    b = body()
    gid = b.get("group_id")
    codes = b.get("codes")
    if isinstance(codes, str):
        codes = [c.strip().upper() for c in codes.replace(",", " ").split() if c.strip()]
    if not gid:
        return err("缺 group_id")
    if not codes:
        return err("没给代码")
    return ok({"result": watch.add_stocks(_int(gid, None, "group_id"), codes),
               "stocks": watch.list_stocks(_int(gid, None, "group_id"))})


@app.route("/api/watch/stocks/remove", methods=["POST"])
def api_watch_stocks_remove():
    b = body()
    codes = b.get("codes")
    if isinstance(codes, str):
        codes = [c.strip().upper() for c in codes.replace(",", " ").split() if c.strip()]
    if not codes:
        return err("没给代码")
    gid = _int(b.get("group_id"), None, "group_id")
    return ok({"result": watch.remove_stocks(gid, codes),
               "stocks": watch.list_stocks(gid), "groups": watch.list_groups()})


@app.route("/api/watch/stocks/transfer", methods=["POST"])
def api_watch_stocks_transfer():
    """把自选股拖到别的分组：`mode=copy` 复制 / `mode=move` 移动。

    ⚠ **别把它在前端拆成「先 remove 再 add」两次调用** —— 写库顺序在
      `watch.transfer_stocks` 里定死了（先加到目标组、再删源组）：先删的话那只票会短暂
      不属于任何组，`remove_stocks` 的防孤儿收尾会把它的提醒规则一起清掉，且不报错。
    """
    b = body()
    codes = b.get("codes")
    if isinstance(codes, str):
        codes = [c.strip().upper() for c in codes.replace(",", " ").split() if c.strip()]
    if not codes:
        return err("没给代码")
    if not (b.get("from_group_id") and b.get("to_group_id")):
        return err("缺 from_group_id / to_group_id")
    try:
        res = watch.transfer_stocks(_int(b.get("from_group_id"), None, "from_group_id"),
                                    _int(b.get("to_group_id"), None, "to_group_id"),
                                    codes, b.get("mode") or "move")
    except ValueError as e:
        return err(str(e))
    return ok({"result": res, "stocks": watch.list_stocks(_int(b.get("from_group_id"), None, "from_group_id")),
               "groups": watch.list_groups()})


@app.route("/api/watch/stock/note", methods=["POST"])
def api_watch_stock_note():
    b = body()
    if not (b.get("group_id") and b.get("ts_code")):
        return err("缺 group_id / ts_code")
    watch.set_note(_int(b.get("group_id"), None, "group_id"), b["ts_code"], b.get("note") or "")
    return ok({"stocks": watch.list_stocks(_int(b.get("group_id"), None, "group_id"))})


@app.route("/api/watch/alerts")
def api_watch_alerts():
    return ok({"alerts": watch.list_alerts(request.args.get("ts_code") or None)})


@app.route("/api/watch/alert/save", methods=["POST"])
def api_watch_alert_save():
    """存一条提醒规则。两类：`kind='price'`（指标/价位，老的那套）· `kind='pool'`（选股命中）。

    ⚠ `kind` 不传 = `price`（老调用方与老页面一个字不用改）。
    ⚠ 改一条**老规则**而请求里没带周期 ⇒ 保留它原来那个（见下），别悄悄改回日K。
    ⚠ `kind='pool'` 时只认 `pool_id`；选股策略不存在 → 400（**不存**一条永远不响的规则）。
    """
    b = body()
    freq = b.get("freq")
    if not freq and b.get("id"):
        # ⚠ 改一条**老规则**而请求里没带周期 ⇒ 保留它原来那个，别悄悄改回日K ——
        #   周K 规则被无声降级成日K 是最难查的一类错：页面上看着还在，行为完全不同。
        cur = db.query_one("SELECT freq FROM watch_alert WHERE id=%s", (_int(b.get("id"), None, "id"),))
        freq = (cur or {}).get("freq")
    try:
        aid = watch.save_alert(b.get("ts_code"), b.get("line") or "", b.get("op") or "",
                               b.get("value"), b.get("note") or "",
                               1 if b.get("enabled", 1) else 0,
                               _int(b.get("id"), None, "id"),
                               freq=freq or watch.DEFAULT_FREQ,
                               kind=(b.get("kind") or watch.DEFAULT_KIND),
                               pool_id=b.get("pool_id"))
    except ValueError as e:
        return err(str(e))
    return ok({"id": aid, "alerts": watch.list_alerts(b.get("ts_code") or None)})


@app.route("/api/watch/pools")
def api_watch_pools():
    """「我的关注」弹窗里那个选股策略下拉的数据源。

    ⚠ 只回下拉要用的字段（id / name / name_label / label / timeframe / cond_count / builtin），
      **不是** `/api/pools` 的整份描述：那条接口给的是选股页要的完整对象
      （conds/filt/preselectable…），这里全用不上，回多了只是把页面和选股页绑在一起
      （选股页改结构时这里跟着抖）。
    ⚠ `timeframe`（2026-10-10）：选股策略自带周期；弹窗绑定策略时把「日K/周K」下拉
      **默认切到策略的周期**（仍可改），`name_label` 是带「（周K）」后缀的显示名。
    """
    out = []
    for p in pool_mod.list_pools():
        d = pool_mod.describe(p)
        out.append({"id": d["id"], "name": d["name"], "name_label": d["name_label"],
                    "label": d["label"], "timeframe": d["timeframe"],
                    "cond_count": d["cond_count"], "builtin": d["builtin"]})
    return ok(out)


@app.route("/api/watch/pool/hit", methods=["POST"])
def api_watch_pool_hit():
    """预览「这条选股策略现在名中了哪些」—— 只读，**不写库、不建提醒**。

    弹窗里选完策略立刻看一眼用（`codes` 只给这一只票，几十毫秒）；也可以不传 `codes`、
    改传 `group_id` 看**整组**命中多少只（慢一些：要按 300 根 K 线把整组面板算出来）。
    ⚠ 与「选中策略就自动查整组」相比，这里刻意做成**显式调用**：一只 500 只的组要十几秒，
      自动查会让人以为页面卡住了。
    ⚠ `freq`（2026-10-10）：`daily` 判**最新交易日**那根；`weekly` 判「最近一个
      **已收盘的完整周**」的最后一个交易日（`watch._pool_week_end`）—— 预览与真检测
      **同一个 `end`**，否则「预览说命中、检测不响」。**不传 = 跟随这条选股策略自己的
      周期**（`pool_strategy.timeframe`，弹窗没动下拉时走这条，免得「策略是周K、预览却按日K」）；
      **乱传 400**（不静默降级：选了周K 却按日K 给结论，比报错更难查）。
    ⚠ 这里只管**判定节奏**；**面板周期**（条件在哪条 K 线上算）由策略自己的 `timeframe`
      决定，在 `watch.scan_pool()` 里一步到位 —— 本路由**不再自己算 `end`**。
    """
    b = body() or {}
    pid = _int(b.get("pool_id"), None, "pool_id")
    if not pid:
        return err("缺 pool_id")
    freq = (b.get("freq") or "").strip().lower()
    if freq and freq not in watch.FREQS:
        return err(f"不认识的周期：{freq}")
    if not freq:
        p_row = pool_mod.get_pool(pid)
        if not p_row:
            # ⚠ 文案与 `watch.scan_pool()` 里那句**逐字一致**（同一件事两处各写一遍，
            #   页面上/回归里就会看到「不存在」和「已不存在」两种说法）。
            return err(f"选股策略 #{pid} 已不存在（可能已被删除）")
        freq = uni.norm_freq(p_row.get("timeframe"))
    codes = b.get("codes")
    if isinstance(codes, str):
        codes = [c.strip().upper() for c in codes.replace(",", " ").split() if c.strip()]
    if not codes:
        gid = _parse_group_id(b.get("group_id"))
        if gid:
            codes = [r["ts_code"] for r in db.query(
                "SELECT ts_code FROM watch_stock WHERE group_id=%s", (gid,))]
    if not codes:
        return err("没给股票：传 codes，或传一个存在的 group_id")
    # ⚠ `end` 不再在这儿算：面板周期（策略自己的 `timeframe`）与判定节奏（`freq`）都由
    #   `scan_pool` 一处决定 —— 外面再算一次 `end` 就是第二个判定点（改一处漏一处）。
    res = watch.scan_pool(pid, [str(c).strip().upper() for c in codes if str(c).strip()],
                          freq=freq)
    res["freq"] = res.get("freq") or freq          # 实际节奏（策略周K 或 本次选了周K）
    if res.get("error"):
        return err(res["error"])
    # 命中票的名字一并给出来（页面上写「浦发银行」比写「600000.SH」有用）
    names = {r["ts_code"]: r["name"] for r in db.query(
        "SELECT ts_code, name FROM stock_basic")}
    res["rows"] = [{"ts_code": c, "name": names.get(c, ""),
                    "close": res["hits"][c]["close"], "date": res["hits"][c]["date"]}
                   for c in res["codes"]]
    res.pop("hits", None)
    return ok(res)


@app.route("/api/watch/alert/delete", methods=["POST"])
def api_watch_alert_delete():
    """删一条提醒规则（**连带**它的触发记录，见 `watch.delete_alert`）。

    两个入口共用：自选股弹窗里的「删」、**触发提醒列表**里新加的那个「删」。
    回 `result`（删的是哪只票的哪条、连带几条记录、是不是真删掉了）——
    页面拿它写清 toast，不然用户只能看到「已删除」，不知道删掉了什么。
    """
    aid = body().get("id")
    if not aid:
        return err("缺 id")
    res = watch.delete_alert(_int(aid, None, "id"))
    return ok({"result": res, "alerts": watch.list_alerts(), "summary": watch.summary()})


@app.route("/api/watch/alert/toggle", methods=["POST"])
def api_watch_alert_toggle():
    b = body()
    if not b.get("id"):
        return err("缺 id")
    watch.toggle_alert(_int(b.get("id"), None, "id"), bool(b.get("enabled")))
    return ok({"alerts": watch.list_alerts()})


@app.route("/api/watch/alerts/boll_lower", methods=["POST"])
def api_watch_alerts_boll_lower():
    """给「日K 在布林下轨」的自选股**批量**加「日K 触及布林下轨」提醒。

    ⚠ `dry_run=1` **只算不写** —— 页面先拿它把「将新增 N 条」摆进确认框，用户点了确认
      再真写。扫一遍全自选要几秒，不能让用户在不知道会改多少条的情况下闭着眼点。
    ⚠ 不给 `codes` = **全部自选股**（跨组去重），这是「全部添加」那个需求的落点；
      传了 `codes` 就只扫这些（回归里限定在临时组，不碰用户那 515 只真身）。
    """
    b = body() or {}
    codes = b.get("codes")
    if isinstance(codes, str):
        codes = [c.strip().upper() for c in codes.replace(",", " ").split() if c.strip()]
    res = watch.add_boll_lower_alerts(codes or None, note=b.get("note"),
                                      dry_run=bool(b.get("dry_run")))
    return ok({"result": res, "alerts": watch.list_alerts()})


@app.route("/api/watch/check", methods=["POST"])
def api_watch_check():
    """立刻跑一次提醒检测（只判最新一两根，不是全历史回放）。"""
    b = body() or {}
    try:
        lookback = _int(b.get("lookback"), 1, "lookback")
    except (TypeError, ValueError):
        return err("lookback 必须是整数")
    res = watch.check(lookback=lookback)
    # `group_id` 一起透传：这份 feed 只是「检测完顺手给的一份快照」（关注页还会自己
    #   `loadFeed()` 一次），但口径别在这里分家 —— 页面传了组，这里也按那个组回。
    #   ⚠ 2026-10-09：以前这里 `try: int(gid) except: gid=None` —— 传个 `abc` 会**静默
    #     退回「全部」**，跟 feed 那条路「乱传 → 400」不是一个口径（以为在看本组、看到全市场）。
    #     现在两边都走 `_parse_group_id()`；「没传 / 0」才是全部，乱传就是 400。
    gid = _parse_group_id(b.get("group_id"))
    return ok({"result": res, "feed": watch.feed(days=_int(b.get("days"), 1, "days"),
                                                    group_id=gid, limit=500),
               "summary": watch.summary()})


@app.route("/api/watch/feed")
def api_watch_feed():
    """触发记录（新的在前）。

    ⚠ `limit` 是 2026-10-08 加的：那天上了「布林下轨批量加提醒」，一次能给上百只票挂规则 ——
      用户点一下「立即检测」，当天命中就是一两百条，而 `feed()` 默认 `LIMIT 100` 会**静默截断**
      （页面只会显得「就这么几条」，看不出后头的被吃了）。上限放到 2000 兜底，别让页面无限拉。
      ⚠ 2026-10-09：默认从 100 提到 **500** —— 那天只做了「可配」，默认还是 100，实测当天
      命中 108 条 ⇒ 不传参就静默少给 8 条。同一个坑修一半等于没修。
    ⚠ `days` 默认 **1**（原来 7）：关注页那个列表的默认区间是「最近 1 个交易日」，
      接口默认值跟着走 —— 免得页面没传参时这里偷偷给 7 天。
    ⚠ `group_id`（2026-10-08 加）：只看**这个分组**里的票的触发记录；不传 / `0` = 全部。
      关注页那张列表是**跟着左边选中的分组走**的（需求原话：「触发提醒只展示当前分组的提醒」），
      页面必须传。过滤只能在 SQL 里做，别让前端拿回去自己筛 —— 见 `watch.feed()` 的说明。
    """
    try:
        days = _int(request.args.get("days"), 1, "days")
    except (TypeError, ValueError):
        return err("days 必须是整数")
    try:
        limit = _int(request.args.get("limit"), 500, "limit")
    except (TypeError, ValueError):
        return err("limit 必须是整数")
    # 「不传 / 0」= 全部；给了一个不像数的东西直接报错，不静默当「全部」
    #   （静默回退成全部 = 用户以为在看本组，看到的其实是全市场）。
    #   ⚠ 口径只在 `_parse_group_id()` 一处 —— 与 `/api/watch/check` 共用。
    gid = _parse_group_id(request.args.get("group_id"))
    feed = watch.feed(days=max(1, min(days, 60)), limit=max(20, min(limit, 2000)),
                      group_id=gid)
    return ok({**feed, "summary": watch.summary()})


@app.route("/api/watch/summary")
def api_watch_summary():
    return ok(watch.summary())


@app.route("/api/sync/logs")
def api_sync_logs():
    rows = db.query("SELECT * FROM sync_log ORDER BY id DESC LIMIT 30")
    for r in rows:
        for k in ("date_from", "date_to", "started_at", "ended_at"):
            if r.get(k) is not None:
                r[k] = str(r[k])[:19]
        # 状态的中文标签与配色由后端给（词表唯一来源 sync.STATUS_LABEL）——
        # 首页与数据管理页两处都只是把它铺出来，不各自映射一遍英文状态。
        label, cls = sync.status_label(r.get("status"))
        r["status_label"] = label
        r["status_cls"] = cls
    return ok(rows)


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------

@app.route("/api/jobs")
def api_jobs():
    return ok(jobs.list_jobs())


@app.route("/api/jobs/<jid>")
def api_job(jid):
    j = jobs.get(jid)
    if not j:
        return err("任务不存在", 404)
    j.pop("cancel", None)
    return ok(j)


@app.route("/api/jobs/<jid>/cancel", methods=["POST"])
def api_job_cancel(jid):
    jobs.cancel(jid)
    return ok()


# ---------------------------------------------------------------------------
# 策略
# ---------------------------------------------------------------------------

@app.route("/api/strategies")
def api_strategies():
    return ok(st.list_strategies())


@app.route("/api/strategies/<int:sid>")
def api_strategy_get(sid):
    s = st.get_strategy(sid)
    if not s:
        return err("策略不存在", 404)
    return ok(s)


@app.route("/api/strategies", methods=["POST"])
def api_strategy_save():
    b = body()
    name = (b.get("name") or "").strip()
    if not name:
        return err("策略名称不能为空")
    cfg = b.get("config") or {}
    errors = st.validate_config(cfg)
    if errors and not b.get("force"):
        return jsonify({"ok": False, "error": "；".join(errors), "errors": errors}), 400
    sid = st.save_strategy(name, b.get("note", ""), cfg, b.get("id"))
    log.info("策略已保存 id=%s name=%s", sid, name)
    return ok(st.get_strategy(sid))


@app.route("/api/strategies/<int:sid>", methods=["DELETE"])
def api_strategy_delete(sid):
    st.delete_strategy(sid)
    log.info("策略已删除 id=%s", sid)
    return ok()


@app.route("/api/strategies/validate", methods=["POST"])
def api_strategy_validate():
    b = body()
    errors = st.validate_config(b.get("config") or {})
    cfg = st.merge_config(b.get("config") or {})
    return ok({"valid": not errors, "errors": errors, "config": cfg})


@app.route("/api/strategies/builtins", methods=["POST"])
def api_install_builtins():
    n = st.install_builtins(force=bool(body().get("force")))
    return ok({"installed": n})


@app.route("/api/strategies/reorder", methods=["POST"])
def api_strategy_reorder():
    """买卖策略卡片拖拽排序：按 `ids` 的先后把 `sort` 重写一遍。

    ⚠ `ids` 必须是**列表** —— 乱传（非列表 / 元素不是整数 / 空）一律 400，不许 500（C57）。
    ⚠ 元素逐个走 `_int()`（全站唯一的整数入参解析），所以 `"12"` / `12.0` 都收，
      而 `true` / `abc` / `{...}` 会抛 `BadParam` ⇒ 400。**别在这里另写一套 int 转换**。
    ⚠ 单个元素乱传时**整批 400**（不静默丢掉它）—— 静默丢会让用户「拖了没生效却没有任何
      提示」，比直接报错难查得多。
    """
    b = body()
    ids = b.get("ids")
    if not isinstance(ids, list):
        raise BadParam("ids 必须是 id 列表")
    seq = [_int(x, name="ids 里的 id") for x in ids]
    changed = st.reorder_strategies(seq)
    # 回全套最新清单：前端拿到后直接按它重画，省一次 GET（也避免「拖完再拉一次」的空窗）
    return ok({"changed": changed, "strategies": st.list_strategies()})


# ---------------------------------------------------------------------------
# 回测
# ---------------------------------------------------------------------------

def _benchmark_series(start: str, end: str, index_code: str = "000300.SH") -> dict:
    rows = db.query(
        "SELECT trade_date, close FROM index_daily WHERE ts_code=%s "
        "AND trade_date BETWEEN %s AND %s ORDER BY trade_date",
        (index_code, start, end))
    return {r["trade_date"].strftime("%Y-%m-%d"): float(r["close"]) for r in rows
            if r["close"]}


@app.route("/api/backtest/start", methods=["POST"])
def api_backtest_start():
    b = body()
    sid = b.get("strategy_id")
    if sid:
        s = st.get_strategy(_int(sid, None, "strategy_id"))
        if not s:
            return err("策略不存在")
        cfg = s["config"]
        sname = s["name"]
    else:
        cfg = b.get("config") or {}
        sname = b.get("name") or "临时策略"
    universe = b.get("universe") or "000300.SH"
    # 条件池：把池子描述注入本次运行的 cfg，并单独校验表达式
    if uni.is_cond(universe):
        pool_errs = uni.validate(universe, set(st.condition_field_names()))
        if pool_errs:
            return jsonify({"ok": False, "error": "；".join(pool_errs),
                            "errors": pool_errs}), 400
        cfg = {**cfg, "_pool": uni.parse(universe)}

    cfg = st.merge_config(cfg)
    errors = st.validate_config(cfg)
    if errors:
        return jsonify({"ok": False, "error": "；".join(errors), "errors": errors}), 400

    start = _date(b.get("start"), DEFAULT_SYNC_START)
    end = _date(b.get("end"), market.latest_trade_date() or
                dt.date.today().strftime("%Y-%m-%d"))
    # 运行参数走引擎的**唯一合成点**：默认值 ← 页面表单 ← 策略自带（风控 / 滑点）。
    # 落库的就是合成后的值 —— 否则「记录里的滑点」和「实际用的滑点」不一致。
    params = bt.resolve_params(cfg, b.get("params"))
    run_name = b.get("name") or f"{sname} · {market.universe_label(universe)}"
    # 选股条件快照：`universe` 列存的是人话标签，池子改一条，标签背后的条件和这次跑的东西
    # 就没关系了。列表里悬浮要看的是**当时筛了什么** → 启动那一刻把池子描述整体存下来
    # （引用 `pool:156` 的会被 describe 展开成那一刻的 where，之后池子怎么改都不影响这条记录）。
    uspec = uni.describe(universe)
    jid = jobs.new_job("backtest", run_name)
    log.info("回测提交 run=%s 策略=%s 池=%s 区间 %s~%s 持仓上限=%s",
             jid, sname, market.universe_label(universe), start, end,
             params.get("max_positions"))

    # job_id 一起落库：页面刷新后要靠它把「活着的任务」找回来（进度、阶段说明、日志都在内存里）。
    run_id = db.execute(
        "INSERT INTO backtest_run (strategy_id,name,universe,universe_spec,start_date,end_date,"
        "init_cash,params,status,progress,progress_msg,job_id) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'running',0,%s,%s)",
        (_int(sid, 0, "strategy_id"), run_name, market.universe_label(universe),
         json.dumps(uspec, ensure_ascii=False), start, end, float(params["init_cash"]),
         json.dumps(params, ensure_ascii=False), "排队中…", jid),
    )
    run_id = int(db.scalar("SELECT LAST_INSERT_ID()") or run_id)

    # 进度**唯一写字处**：内存任务（页面上那行字 + 日志）与回测记录（刷新后接得回来）一起更新。
    # 内存那侧是实时的、也是「页面上正跑着」时看的那个；库里那侧按时间节流 ——
    # run_backtest 是**逐日**回调（400 个交易日 = 400 次），逐个写库纯属浪费。
    last_write = [0.0]

    def _prog(pr: float, msg: str) -> None:
        jobs.update(jid, pr, msg)
        now = time.time()
        if now - last_write[0] < 0.5 and pr < 1.0:
            return
        last_write[0] = now
        try:
            db.execute(
                "UPDATE backtest_run SET progress=%s,progress_msg=%s WHERE id=%s",
                (float(pr), str(msg)[:200], run_id))
        except Exception as e:  # noqa: BLE001
            # 进度落库失败**不能**把回测带崩 —— 它只是展示用的
            log.warning("run=%s 进度落库失败：%s: %s", run_id, type(e).__name__, e)

    def task():
        t0 = time.time()
        # 先按「基础池 × 预筛」缩小加载范围（条件池能走 SQL 时集合精确相等，纯提速）
        codes = market.resolve_universe(universe, start=start, end=end)
        # ⚠ 这里必须走 `uni.parse()` 而不是直接 `universe.get("base")`：`universe` 有三种
        #   合法写法（静态 code / `{"pool_id":3}` / `"pool:3"`），而 `is_cond()` 对**字符串**
        #   的 `"pool:3"` 同样返回 True —— 直接 `.get()` 会 `AttributeError: 'str' object
        #   has no attribute 'get'`，回测在「排队中」就静默死掉（2026-09-30 实测：从脚本按
        #   官方前缀传池引用必崩，界面因为传的是 `{pool_id}` 才一直没暴露）。
        pspec = uni.parse(universe) if uni.is_cond(universe) else None
        base_spec = pspec["base"] if pspec else universe
        base_n = len(market.resolve_universe(base_spec))
        note = uni.preselect_note(universe)
        job_msg = f"股票池 {len(codes)} 只"
        if base_n > len(codes):
            job_msg += f"（条件池预筛，基础池 {base_n} 只）"
        _prog(0.02, job_msg + "，分批加载行情…")
        cinfo: dict = {}
        data = bt.prepare_data(
            codes, start, end, cfg, batch=250,
            progress=lambda msg, pr: _prog(pr, msg),
            cache_info=cinfo)
        cache_note = {"hit": "命中数据缓存", "miss": "已写数据缓存",
                      "off": "缓存关闭"}.get(cinfo.get("cache") or "", "")
        extra = f"股票池 {len(codes)} 只 / 基础池 {base_n} 只 / 有效标的 {len(data)} 只"
        extra += f" / 加载+信号 {time.time()-t0:.1f}s"
        if cache_note:
            extra += f" / {cache_note}"
        if note:
            extra += f" / {note}"
        jobs.update(jid, log=extra)
        log.info("run=%s 数据就绪：%s", run_id, extra)
        _prog(0.62, f"有效标的 {len(data)} 只，开始回测…")
        res = bt.run_backtest(
            data, cfg, start, end, params,
            benchmark=_benchmark_series(start, end),
            progress=lambda msg, pr: _prog(0.62 + 0.36 * pr, msg))
        del data
        # 引擎算完（它的 1.0 映射过来是 0.98）到这里、以及下面那段插交易/资金曲线，
        # 都是**真实占用时间**的活；进度必须推满，否则「库已 done、进度条卡 97%」
        # 这段窗口会一直挂着（落库几千笔时能看见好几秒）。
        _prog(1.0, "回测完成，正在写库…")
        m = res.get("metrics") or {}
        log.info("run=%s 回测完成：交易 %s 笔 / 收益 %s%% / 回撤 %s%% / 耗时 %.1fs",
                 run_id, m.get("trade_count"), m.get("total_return"),
                 m.get("max_drawdown"), time.time() - t0)
        run_logs = list(res.get("logs") or [])
        if cache_note:
            run_logs.insert(0, "[数据] %s（key %s）"
                            % (cache_note, str(cinfo.get("key") or "")[:8]))
        # ⚠ 这条记录可能在跑到一半时被删掉了（界面的「删」对 running 也点得动）。
        #   那时下面这几笔写库就全成了**指向已删记录的孤儿行**，而且没有任何入口能再删掉
        #   它们（列表不显示、删接口也无从下手）。写之前先确认记录还在；不在就只记日志。
        if not db.scalar("SELECT id FROM backtest_run WHERE id=%s", (run_id,)):
            log.warning("run=%s 记录已被删除，跳过明细与终态写入（避免孤儿行）", run_id)
            jobs.fail(jid, "记录已被删除")
            return
        db.execute(
            "UPDATE backtest_run SET metrics=%s,status='done',finished_at=NOW(),log=%s,"
            "progress=1,progress_msg='完成' WHERE id=%s",
            (json.dumps(m, ensure_ascii=False, default=str),
             "\n".join(run_logs)[-6000:], run_id))
        tr = res.get("trades") or []
        if tr:
            sql = """
                INSERT INTO backtest_trade
                  (run_id,ts_code,name,buy_date,buy_price,buy_reason,sell_date,
                   sell_price,shares,cost,revenue,pnl,pnl_pct,hold_days,sell_reason,fee,
                   slip_cost)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """
            rows = [(run_id, t["ts_code"], t["name"], t["buy_date"], t["buy_price"],
                     t["buy_reason"], t["sell_date"], t["sell_price"], t["shares"],
                     t["cost"], t["revenue"], t["pnl"], t["pnl_pct"], t["hold_days"],
                     t["sell_reason"], t.get("fee"), t.get("slip_cost")) for t in tr]
            db.insert_many(sql, rows)
        eq = res.get("equity") or []
        if eq:
            sql = """
                INSERT INTO backtest_equity
                  (run_id,trade_date,total_asset,cash,position_value,hold_num,benchmark)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE total_asset=VALUES(total_asset),
                  cash=VALUES(cash),position_value=VALUES(position_value),
                  hold_num=VALUES(hold_num),benchmark=VALUES(benchmark)
            """
            rows = [(run_id, e["trade_date"], e["total_asset"], e["cash"],
                     e["position_value"], e["hold_num"], e["benchmark"]) for e in eq]
            db.insert_many(sql, rows)
        return {"run_id": run_id, "metrics": m, "trade_count": len(tr)}

    def _on_err(e):
        log.error("run=%s 回测失败：%s", run_id, e)
        # progress 停在失败那一刻、进度上限不再假装跑到 100%；msg 换成失败原因，
        # 列表里鼠标放到状态上就能看到为啥挂（不用点进去翻日志）。
        db.execute("UPDATE backtest_run SET status='error',log=%s,progress_msg=%s "
                   "WHERE id=%s",
                   (str(e)[:4000], ("失败：" + str(e))[:200], run_id))

    jobs.run_async(jid, task, "回测完成")
    return ok({"job_id": jid, "run_id": run_id})


def _run_json(v, default):
    """回测记录里的 JSON 列（params / metrics）统一在这里解析 —— 各处别再各写一遍。"""
    if isinstance(v, (str, bytes)):
        try:
            return json.loads(v)
        except Exception:  # noqa: BLE001
            return default
    return default if v is None else v


def _live_job(r):
    """这条记录对应的**内存任务还活着**吗（活着的才有实时进度 / 阶段说明 / 逐条日志）。"""
    j = jobs.get(r.get("job_id")) if r.get("job_id") else None
    return j if (j and j.get("status") in ("pending", "running")) else None


def _run_progress(r):
    """某条回测现在的进度：跑着的时候内存里的**活任务**最实时，终态一律以**库**为准。

    为什么要两层：任务只活在内存（core/jobs._JOBS），刷新页面不影响它、但服务重启就没了；
    而库里的 progress/progress_msg 是任务自己一路写下来的，服务重启后至少还剩最后一帧。
    ⚠ 终态（done/error）**不看内存**：库里 status 已经落定，而内存任务在「写完 done 之后
    还要把几千笔交易/资金曲线写进库」这段时间里仍是 running、进度停在最后一帧 —— 采信它就会
    渲染出「状态 done、进度条 97%」这种自相矛盾（2026-09-20 报的）。done 直接给 1.0，
    让「done ⇒ 100%」成为**出口上的不变量**，不去指望每条记录自身的 progress 都恰好对。
    """
    prog = float(r.get("progress") or 0.0)
    msg = r.get("progress_msg")
    st = r.get("status") or ""
    if st == "done":
        return 1.0, (msg or "完成")
    if st != "running":
        return prog, msg
    j = _live_job(r)
    if j:
        return float(j.get("progress") or 0.0), j.get("message")
    return prog, msg


def _run_writing(r):
    """库里已落定终态、但内存任务还活着 ⇒ 后端正**往库里写交易明细 / 资金曲线**。

    `status='done'` 是在写库**之前**写的（实测 1600 多笔要写好几秒），所以那几秒里
    「已完成」是提前话：点进明细会是空的。前端拿这个字段决定是现在收尾、还是再等一拍。
    """
    return (r.get("status") or "") != "running" and bool(_live_job(r))


def _spec_by_label(label: str, cache: dict) -> dict | None:
    """按「股票池」那句标签，反查**当前**库里是哪条池子（老记录没有条件快照时用）。

    标签的格式由 `universe.label()` 定死：静态池就是它的中文名（全部A股 / 沪深300），
    条件池是 `基础池 · 池子名`。所以这里只有两条路：先比静态池名，再逐个算池子的 label
    比字符串 —— **不解析标签文本**（拿 `·` 去切再去猜，改天 label 格式一动就全错）。

    命中返回 `uni.describe({'pool_id': pid})`，找不到返回 None。`cache` 由调用方持有，
    一次列表请求里 60 条记录共用一份池子清单，不会每条都去查库。
    """
    from core import pool as pool_mod  # noqa: PLC0415  延迟导入：只在展示老记录时才需要
    if not label:
        return None
    if "static" not in cache:
        cache["static"] = {market.universe_label(u["code"]): u["code"]
                           for u in market.list_universes()}
        try:
            cache["pools"] = [(p, uni.label({"pool_id": p["id"]}))
                              for p in pool_mod.list_pools()]
        except Exception:  # noqa: BLE001  只是展示用的反查，查不到就不显示条件
            cache["pools"] = []
    code = cache["static"].get(label)
    if code:
        return {**uni.describe(code), "derived": True}
    for p, lab in cache["pools"]:
        if lab == label:
            return {**uni.describe({"pool_id": p["id"]}), "derived": True}
    return None


def _run_universe_spec(r, cache: dict) -> dict | None:
    """列表里「股票池」悬浮要看的东西：**当时**这次回测的选股条件。

    两条来源，优先级分明：
    * `universe_spec`（新记录都存）→ 回测启动那一刻的条件快照，池子后来怎么改都不影响它；
    * 老记录（该列为 NULL）→ 按标签反查**当前**池子定义，并带上 `derived: True`，
      让界面老实标注「条件已按当前池定义反查」，别让人以为是当时的原样。
    """
    spec = _run_json(r.get("universe_spec"), None)
    if isinstance(spec, dict) and spec:
        spec["derived"] = False
        return spec
    return _spec_by_label(r.get("universe"), cache)


@app.route("/api/backtest/runs")
def api_backtest_runs():
    limit = _int(request.args.get("limit"), 50, "limit")
    rows = db.query(
        "SELECT r.id,r.strategy_id,r.name,r.universe,r.universe_spec,"
        "r.start_date,r.end_date,"
        "r.init_cash,r.params,r.metrics,r.status,r.progress,r.progress_msg,r.job_id,"
        "r.created_at,s.name AS strategy_name "
        "FROM backtest_run r LEFT JOIN strategy s ON s.id=r.strategy_id "
        "ORDER BY r.id DESC LIMIT %s", (limit,))
    out = []
    uspec_cache: dict = {}      # 老记录的「按标签反查当前池子」一次请求里只算一遍
    for r in rows:
        m = _run_json(r.get("metrics"), {})
        prog, pmsg = _run_progress(r)
        out.append({
            "id": r["id"], "strategy_id": r["strategy_id"],
            "strategy_name": r["strategy_name"] or "—", "name": r["name"],
            "universe": r["universe"],
            # 「股票池」列悬浮要看的东西：当时这次回测的选股条件（老记录按标签反查、带 derived 标记）
            "universe_spec": _run_universe_spec(r, uspec_cache),
            "start_date": str(r["start_date"]), "end_date": str(r["end_date"]),
            "init_cash": float(r["init_cash"]), "status": r["status"],
            "created_at": str(r["created_at"])[:19],
            # 列表里的「回测配置」列：原样给出这次跑的合成参数（含初始资金、最大持仓只数…）
            "params": _run_json(r.get("params"), {}),
            "progress": round(prog, 4),
            "progress_msg": pmsg,
            # 库已 done 但后端还在写交易明细 —— 前端据此延后「完成」的收尾动作
            "writing": _run_writing(r),
            "total_return": (m or {}).get("total_return"),
            "annual_return": (m or {}).get("annual_return"),
            "max_drawdown": (m or {}).get("max_drawdown"),
            "win_rate": (m or {}).get("win_rate"),
            "trade_count": (m or {}).get("trade_count"),
            "sharpe": (m or {}).get("sharpe"),
            "benchmark_return": (m or {}).get("benchmark_return"),
            "alpha": (m or {}).get("alpha"),
        })
    return ok(out)


@app.route("/api/backtest/runs/<int:rid>")
def api_backtest_run(rid):
    r = db.query_one(
        "SELECT r.*, s.name AS strategy_name, s.config AS strategy_config "
        "FROM backtest_run r LEFT JOIN strategy s ON s.id=r.strategy_id WHERE r.id=%s",
        (rid,))
    if not r:
        return err("回测不存在", 404)
    for k in ("metrics", "params", "strategy_config", "universe_spec"):
        if isinstance(r.get(k), (str, bytes)):
            try:
                r[k] = json.loads(r[k])
            except Exception:  # noqa: BLE001
                pass
    # 与列表口径一致：老记录没有条件快照时按池名反查**当前**池子（头部「回测信息」也用它）
    r["universe_spec"] = _run_universe_spec(r, {})
    for k in ("start_date", "end_date", "created_at", "finished_at"):
        if r.get(k) is not None:
            r[k] = str(r[k])[:19]
    r["init_cash"] = float(r["init_cash"])
    # 进度条「刷新后还在」就靠这里：活任务（内存）给出实时的进度、阶段说明、逐条日志；
    # 任务没了（服务重启）则退回库里的最后一帧 + 落库的运行日志。前端只认这个接口，
    # 「刚点开始」和「刷新页面」走的是同一条路 —— 不需要两份逻辑。
    prog, pmsg = _run_progress(r)
    r["progress"] = round(prog, 4)
    r["progress_msg"] = pmsg
    j = _live_job(r)
    r["writing"] = bool(j) and (r.get("status") or "") != "running"
    if j:
        r["logs"] = j.get("logs") or []
    else:
        r["logs"] = [ln for ln in str(r.get("log") or "").splitlines() if ln.strip()]
    return ok(r)


@app.route("/api/backtest/runs/<int:rid>/trades")
def api_backtest_trades(rid):
    sort = request.args.get("sort") or "buy_date"
    # 默认**从近到远**（buy_date DESC）—— 页面「交易明细」的默认排序也是它，两端口径一致；
    # 想看最早那几笔就显式传 order=asc（tools/chart_preview.js 就是这么固定住自己那笔的）。
    order = "ASC" if (request.args.get("order") or "desc").lower() == "asc" else "DESC"
    allow = {"buy_date", "sell_date", "pnl_pct", "pnl", "hold_days", "ts_code"}
    if sort not in allow:
        sort = "buy_date"
    filt = request.args.get("result")  # win / lose
    sql = "SELECT * FROM backtest_trade WHERE run_id=%s"
    args: list = [rid]
    if filt == "win":
        sql += " AND pnl>0"
    elif filt == "lose":
        sql += " AND pnl<=0"
    # id 兜底：同一个买入日会买多只，只按日期排的话这批在 MySQL 里次序不定 ——
    # 分页/取第一笔时会「同一笔忽前忽后」。id 单调递增 ⇒ 次序确定，与插入序一致。
    # 页是**前端**分的（一次拉全、切片渲染），这里的 LIMIT 只是防「极端策略每根 K 线都交易」
    # 把内存打爆的兜底 —— 6000 笔 ≈ 2MB JSON，实测可接受，所以给到 2 万。
    sql += f" ORDER BY {sort} {order}, id ASC LIMIT 20000"
    rows = db.query(sql, args)
    for r in rows:
        for k in ("buy_date", "sell_date"):
            r[k] = str(r[k])[:10] if r[k] else None
        for k in ("buy_price", "sell_price", "cost", "revenue", "pnl", "pnl_pct",
                  "fee", "slip_cost"):
            r[k] = float(r[k]) if r[k] is not None else None
    return ok(rows)


@app.route("/api/backtest/runs/<int:rid>/trades/<int:tid>/kline")
def api_trade_kline(rid, tid):
    """单笔交易的个股 K 线 + 买卖点标注。"""
    try:
        before = _int(request.args.get("before"), 40, "before")
        after = _int(request.args.get("after"), 25, "after")
    except ValueError:
        before, after = 40, 25
    before = max(5, min(before, 250))
    after = max(5, min(after, 250))
    res = bt.trade_kline(rid, tid, before=before, after=after)
    if res.get("error"):
        return err(res["error"], 404)
    return ok(screener._jsonable(res))


@app.route("/api/backtest/runs/<int:rid>/equity")
def api_backtest_equity(rid):
    rows = db.query(
        "SELECT trade_date,total_asset,cash,position_value,hold_num,benchmark "
        "FROM backtest_equity WHERE run_id=%s ORDER BY trade_date", (rid,))
    out = []
    base = None
    for r in rows:
        if base is None:
            base = float(r["total_asset"])
        b = float(r["benchmark"]) if r["benchmark"] is not None else None
        out.append({
            "date": str(r["trade_date"])[:10],
            "nav": round(float(r["total_asset"]) / base, 6) if base else 1.0,
            "asset": float(r["total_asset"]),
            "cash": float(r["cash"]),
            "position": float(r["position_value"]),
            "hold": int(r["hold_num"]),
            "benchmark": round(b, 6) if b is not None else None,
        })
    return ok(out)


@app.route("/api/backtest/runs/<int:rid>/delete", methods=["POST"])
def api_backtest_delete(rid):
    # ⚠ 界面上的「删」对**还在跑**的记录也点得动。只删三张表的话，跑着那个任务**跑完还会
    #   把明细插回来**（它只看 run_id，不查父行在不在）⇒ 一堆指向已删记录的孤儿行：
    #   列表里看不见、接口也删不掉（2026-10-06 实测 #147 留下 1533 笔 + 901 行）。
    #   所以删除时先**叫停**那个任务；真跑完的收尾还有一道存在性检查兜底（见 task 内注释）。
    try:
        row = db.query_one("SELECT status,job_id FROM backtest_run WHERE id=%s", (rid,))
    except Exception:  # noqa: BLE001
        row = None
    if row and row.get("status") == "running" and row.get("job_id"):
        jobs.cancel(str(row["job_id"]))
        log.info("run=%s 删除时仍在跑，已请求取消该任务（防明细回写）", rid)
    db.execute("DELETE FROM backtest_trade WHERE run_id=%s", (rid,))
    db.execute("DELETE FROM backtest_equity WHERE run_id=%s", (rid,))
    db.execute("DELETE FROM backtest_run WHERE id=%s", (rid,))
    return ok()


@app.route("/api/backtest/stats")
def api_backtest_stats():
    """按卖出原因/月度收益聚合，用于图表。"""
    rid = request.args.get("run_id")
    if not rid:
        return err("缺少 run_id")
    rid = _int(rid, None, "run_id")
    reasons = db.query(
        "SELECT sell_reason,COUNT(*) c,AVG(pnl_pct) avg_pct,SUM(pnl) sum_pnl "
        "FROM backtest_trade WHERE run_id=%s GROUP BY sell_reason", (rid,))
    eq = db.query(
        "SELECT trade_date,total_asset FROM backtest_equity WHERE run_id=%s "
        "ORDER BY trade_date", (rid,))
    monthly = []
    if eq:
        df = pd.DataFrame(eq)
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        df["total_asset"] = df["total_asset"].astype(float)
        s = df.set_index("trade_date")["total_asset"]
        m = s.resample("ME").last()
        first = s.resample("ME").first()
        mm = (m / first - 1) * 100
        monthly = [{"month": str(i)[:7], "ret": round(float(v), 3)}
                   for i, v in mm.items() if pd.notna(v)]
    for r in reasons:
        r["avg_pct"] = round(float(r["avg_pct"] or 0), 3)
        r["sum_pnl"] = round(float(r["sum_pnl"] or 0), 2)
    return ok({"reasons": reasons, "monthly": monthly})


# ---------------------------------------------------------------------------
# 选股
# ---------------------------------------------------------------------------

@app.route("/api/screen/run", methods=["POST"])
def api_screen_run():
    """跑一次选股。

    两种用法：
      · 只给 `universe`（选股策略）→ 命中 = **当日入池的股票**（选股逻辑全在池子里）；
      · 再给 `strategy_id`（买卖策略）→ 在池内基础上再按它的买入触发/卖出信号标注，
        选股条件仍来自池子（买卖策略不再写选股条件）。

    `sort_expr`（选填）= 选股页的「排序表达式」：数值越大越靠前，填了**只覆盖这一次选股**
    的排序（直接换掉 cfg 的 score_expr —— 对一次选股来说「排序」和「评分」就是同一个东西：
    同一个式子既决定顺序、又是结果表 `评分` 列的值）。没填则沿用买卖策略的评分表达式，
    都没有就退化成按 |涨跌幅| 降序。买卖策略里存的那条**不会**被改。
    """
    b = body()
    sid = b.get("strategy_id")
    if sid:
        s = st.get_strategy(int(sid))
        if not s:
            return err("策略不存在")
        cfg = dict(s["config"])
        cfg["_name"] = s["name"]
    else:
        cfg = st.merge_config(b.get("config") or {})
        cfg["_name"] = b.get("name") or ""
    universe = b.get("universe") or "all"
    if uni.is_cond(universe):
        pool_errs = uni.validate(universe, set(st.condition_field_names()))
        if pool_errs:
            return jsonify({"ok": False, "error": "；".join(pool_errs),
                            "errors": pool_errs}), 400
        cfg = {**cfg, "_pool": uni.parse(universe)}
    # 「排序表达式」（选股页现填）→ 覆盖本次的 score_expr。放在 validate 之前，
    # 式子写错就当场 400 报出来，而不是安静地按「空评分」跑完一屏乱序的结果。
    sort_expr = str(b.get("sort_expr") or "").strip()
    if sort_expr:
        cfg = {**cfg, "score_expr": sort_expr}
    if not cfg.get("_name"):
        cfg["_name"] = uni.label(universe) if uni.is_cond(universe) else "全部A股"
    errors = st.validate_config(cfg)
    if errors:
        return jsonify({"ok": False, "error": "；".join(errors), "errors": errors}), 400

    td = _date(b.get("trade_date")) or market.latest_trade_date()
    limit = _int(b.get("limit"), 300, "limit")
    _t0 = time.perf_counter()
    res = screener.screen(cfg, universe, td, limit=limit)
    if res.get("error"):
        log.warning("选股失败 %s @%s：%s", cfg.get("_name"), td, res["error"])
        return err(res["error"])
    log.info("选股 %s @%s 池=%s：扫描 %s 只 / 命中 %s 只 / 耗时 %.1fs",
             cfg.get("_name"), td, market.universe_label(universe),
             res.get("scanned"), res.get("count"), time.perf_counter() - _t0)
    if b.get("save") and sid:
        screener.save_screen_result(_int(sid, None, "strategy_id"), res)
    return ok(res)


@app.route("/api/screen/history")
def api_screen_history():
    sid = request.args.get("strategy_id")
    td = _date(request.args.get("trade_date"))
    rows = screener.list_screen_results(_int(sid, None, "strategy_id"), td,
                                        _int(request.args.get("limit"), 500, "limit"))
    return ok(rows)


@app.route("/api/screen/dates")
def api_screen_dates():
    rows = db.query(
        "SELECT DISTINCT trade_date FROM screen_result ORDER BY trade_date DESC LIMIT 60")
    return ok([str(r["trade_date"])[:10] for r in rows])


@app.route("/api/stock/search")
def api_stock_search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return ok([])
    rows = db.query(
        "SELECT ts_code,symbol,name,industry,market FROM stock_basic "
        "WHERE list_status='L' AND (ts_code LIKE %s OR symbol LIKE %s OR name LIKE %s) "
        "ORDER BY ts_code LIMIT 30", (f"{q}%", f"{q}%", f"%{q}%"))
    return ok(rows)


@app.route("/api/stock/<code>/detail")
def api_stock_detail(code):
    """个股诊断：K 线 + 买卖信号 + 「命中（在池内）」+ 单票收益。

    选股逻辑在选股策略里，所以页面可以带 `pool_id`（或 `config` / `strategy_id`）：
      · 带了 → 图上的「命中」点 = **当日是否在池内**（按那份选股条件逐日求值）；
      · 没带 → **就没有命中点**（`entry` 留空 = 不额外筛；后端会在图表卡标题栏
        写明原因，见 `strategy.signal_marks`）。
        ⚠ 2026-10-11 用户要求「别兜底，没有就没有」—— 这里原来有一段兜底：没带选股条件
        就自动挂预设池「BOLL下轨低吸」（按**名字**查），结果是「只看形态」的入口
        （自选股 / 触发提醒列表）平白冒出一批跟本次查询毫无关系的蓝点。已删。

    `sel_only=1`：**只看选股条件**（选股策略页的试跑预览、今日选股页的看 K 线都走这个）。
    那两个入口一次只选票、没有绑定任何买卖策略，所以这一档不算「策略收益」、
    也不给买卖信号 —— 拿默认买卖参数跑出来的收益跟「这批票选得对不对」无关，
    摆出来只会让人以为试跑还带了买卖策略。命中点照旧按选股条件逐日画。
    具体怎么落列在 `screener.screen_detail` 里（`sel_only`）。
    """
    sel_only = request.args.get("sel_only") == "1"
    sid = request.args.get("strategy_id")
    cfg = {}
    if sid:
        s = st.get_strategy(int(sid))
        if s:
            cfg = s["config"]
    elif request.args.get("config"):
        try:
            cfg = json.loads(request.args["config"])
        except Exception:  # noqa: BLE001
            cfg = {}
    if not cfg:
        cfg = st.merge_config({}).copy()
        cfg["buy_trigger"] = ["open > ref(close, 1)"]
        cfg["sell_signal"] = ["open < ref(close, 1)"]
    pool_id = request.args.get("pool_id")
    if pool_id:
        try:
            sp = uni.parse({"pool_id": int(pool_id)})
            if sp.get("where"):
                cfg = {**cfg, "_pool": sp}
        except (TypeError, ValueError):
            pass
    # ⚠ 这里**曾经**有一段兜底：cfg 里没有选股条件时（判据 `st.has_entry_conds`）就
    #   自动挂预设池「BOLL下轨低吸」。2026-10-11 按用户要求整段删掉（「别兜底，
    #   没有就没有」）—— 现在没带就是没有：`entry` 留空、图上不铺命中点，
    #   `signal_marks` 会在图表卡标题栏写「没选『选股策略』（或它没写条件）= 不额外筛，
    #   所以不铺命中点」，不至于看着像「选了但一只没命中」。
    bars = _int(request.args.get("bars"), 260, "bars")
    # freq=daily/weekly：不给就跟随策略自己的 timeframe（周线策略默认看周K，
    # 否则条件会落在日K上求值 —— 形状对、口径错）
    # open_pct：开盘形态概率的高开/低开阈值（%）。**不进 `_int`**（它是小数），
    # 由 openstat.clamp_threshold 收口：非法退回默认、越界夹边界、统一留 1 位小数。
    res = screener.screen_detail(code, cfg, _date(request.args.get("trade_date")), bars,
                                 freq=request.args.get("freq"),
                                 sel_only=sel_only,
                                 open_pct=request.args.get("open_pct"))
    if res.get("error"):
        return err(res["error"], 404)
    return ok(res)


# ---------------------------------------------------------------------------
# 行情图表
# ---------------------------------------------------------------------------

@app.route("/api/kline")
def api_kline():
    code = (request.args.get("code") or "").strip().upper()
    if not code:
        return err("缺少 code")
    if "." not in code and code.isdigit():
        row = db.query_one(
            "SELECT ts_code FROM stock_basic WHERE symbol=%s LIMIT 1", (code,))
        code = row["ts_code"] if row else code
    end = _date(request.args.get("end")) or market.latest_trade_date()
    bars = _int(request.args.get("bars"), 250, "bars")
    freq = request.args.get("freq") or "daily"
    start = screener._lookback_start(end, bars=bars)
    panel = market.load_panel([code], start, end, weekly=False,
                              indicators_cfg=None)
    if code not in panel:
        return err("本地无该股票行情，请先同步数据", 404)
    df = panel[code]
    cols = ["trade_date", "open", "high", "low", "close", "vol", "amount",
            "pct_chg", "ma5", "ma10", "ma20", "ma60", "boll_upper", "boll_mid",
            "boll_lower", "macd_dif", "macd_dea", "macd_hist", "kdj_k", "kdj_d",
            "kdj_j", "rsi6", "rsi12"]
    cols = [c for c in cols if c in df.columns]
    rows = df[cols].tail(bars).copy()
    rows["trade_date"] = pd.to_datetime(rows["trade_date"]).dt.strftime("%Y-%m-%d")
    return ok({"ts_code": code, "bars": screener._jsonable(rows.to_dict("records"))})


@app.route("/api/health")
def api_health():
    try:
        db.scalar("SELECT 1")
        db_ok = True
    except Exception:  # noqa: BLE001
        db_ok = False
    return ok({"db": db_ok, "server_time": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")})


# ---------------------------------------------------------------------------
# 模型设置 · 大模型接入与语句模板
# ---------------------------------------------------------------------------
# 口径全在 core/llm.py（预设/配置/传输）与 core/ai.py（模板/上下文/落库）里，
# 这一层只做三件事：收参数、翻成人话的错误、把结果包成 {ok,data}。

def _ai_payload() -> dict:
    """模型模型设置页要的全套：当前配置（脱敏）+ **全部配置** + 预设 + 变量表 + 模板列表。

    `cfg` 是「当前生效那条」（老前端/弹窗只认它），`profiles` 是全部可切换的配置。
    """
    cfg = llm.load_cfg()
    return {"cfg": llm.public_cfg(cfg), "profiles": llm.list_profiles(),
            "active_id": llm.active_id(), "presets": llm.PRESETS,
            "vars": llm.var_help(), "templates": aia.list_templates(),
            "ready": llm.is_ready(cfg)}


@app.route("/api/ai/config")
def api_ai_config():
    return ok(_ai_payload())


@app.route("/api/ai/profiles", methods=["POST"])
def api_ai_profile_save():
    """新建 / 更新一条模型配置。body：`{id?, profile:{name,preset,kind,base_url,model,api_key?,...}}`

    `api_key` 三态（见 `llm.save_profile`）：没传/空 = **保留这条原来那把**；传了就用新的；
    `clear_api_key=true` 才清空 —— 表单里从不回显 Key，不能让空框把它冲掉。
    """
    b = body()
    form = dict(b.get("profile") or b.get("cfg") or b)
    try:
        pid = llm.save_profile(form, b.get("id") or form.get("id"))
    except llm.LLMError as e:
        return err(str(e))
    except Exception as e:  # noqa: BLE001
        log.warning("保存 AI 配置失败：%s", e)
        return err(f"保存失败：{type(e).__name__}: {e}")
    # saved_id：新建时前端得知道「刚建的是哪条」才好把表单停在那条上
    return ok({**_ai_payload(), "saved_id": pid})


@app.route("/api/ai/profiles/<int:pid>/activate", methods=["POST"])
def api_ai_profile_activate(pid):
    """切换「现在用哪个模型」—— 全局生效，之后所有分析都用它。"""
    if not llm.set_active(pid):
        return err("这条配置不存在", 404)
    return ok(_ai_payload())


@app.route("/api/ai/profiles/<int:pid>/delete", methods=["POST"])
def api_ai_profile_delete(pid):
    try:
        llm.delete_profile(pid)
    except llm.LLMError as e:
        return err(str(e))
    return ok(_ai_payload())


@app.route("/api/ai/profiles/test", methods=["POST"])
def api_ai_profile_test():
    """测一次连接（测的是**表单里当前填的**，不必先保存）。

    ⚠ Key 没重填时要用**这条自己的**那把，不是「当前生效那条」的 —— 配了多条之后，
    这两者经常不是同一条（测试一条还没启用的配置就是为此存在的）。
    """
    b = body()
    form = dict(b.get("profile") or b.get("cfg") or b)
    pid = b.get("id") or form.get("id")
    try:
        base = (llm.get_profile(int(pid)) if pid else None) or llm.active_profile() or llm.load_cfg()
    except (TypeError, ValueError):
        base = llm.load_cfg()
    merged = {**base, **{k: v for k, v in form.items() if k in llm.DEFAULT_CFG}}
    if not str(form.get("api_key") or "").strip():
        merged["api_key"] = base.get("api_key") or ""
    try:
        res = llm.test(merged)
    except llm.LLMError as e:
        return err(str(e))
    return ok({"text": res["text"], "model": res["model"], "kind": res["kind"],
               "profile": base.get("name") or "", "url": res["url"],
               "elapsed_ms": res["elapsed_ms"]})


@app.route("/api/ai/templates")
def api_ai_templates():
    return ok(aia.list_templates())


@app.route("/api/ai/templates", methods=["POST"])
def api_ai_template_save():
    b = body()
    try:
        tid = aia.save_template(b.get("name"), b.get("template"), b.get("id"),
                                b.get("is_default"))
    except aia.AIError as e:
        return err(str(e))
    return ok({"id": tid, "templates": aia.list_templates()})


@app.route("/api/ai/templates/<int:tid>", methods=["POST"])
def api_ai_template_update(tid):
    b = body()
    try:
        aia.save_template(b.get("name"), b.get("template"), tid, b.get("is_default"))
    except aia.AIError as e:
        return err(str(e))
    return ok(aia.list_templates())


@app.route("/api/ai/templates/<int:tid>/default", methods=["POST"])
def api_ai_template_default(tid):
    if not aia.set_default(tid):
        return err("模板不存在")
    return ok(aia.list_templates())


@app.route("/api/ai/templates/<int:tid>/delete", methods=["POST"])
def api_ai_template_delete(tid):
    try:
        aia.delete_template(tid)
    except aia.AIError as e:
        return err(str(e))
    return ok(aia.list_templates())


@app.route("/api/ai/analyze", methods=["POST"])
def api_ai_analyze():
    """发起一次 AI 分析。立刻回记录行（status=running），前端拿 id 轮询。

    body：`{ts_code, name?, industry?, trade_date?, template_id?, profile_id?, source?,
             strategy?, close?, pct_chg?}`
    `close/pct_chg` 由列表页传**它自己那一行**的值 —— 提示词里就该是用户在屏幕上看到的数。
    `profile_id` 是这次用哪条模型配置（不传 = 当前生效那条）。
    """
    b = body()
    code = str(b.get("ts_code") or b.get("code") or "").strip()
    if not code:
        return err("缺少 ts_code")
    try:
        rec = aia.start_analysis(
            code, name=str(b.get("name") or ""), industry=str(b.get("industry") or ""),
            trade_date=_date(b.get("trade_date")), template_id=b.get("template_id"),
            source=str(b.get("source") or "stock"), strategy=str(b.get("strategy") or ""),
            close=b.get("close"), pct_chg=b.get("pct_chg"), profile_id=b.get("profile_id"))
    except aia.AIError as e:
        return err(str(e))
    return ok(rec)


@app.route("/api/ai/analyze/stream", methods=["POST"])
def api_ai_analyze_stream():
    """**流式**发起一次 AI 分析：回 `text/event-stream`，前端边收边画。

    协议：一行一节，`data: {json}` + 空行。事件类型见 `aia._run_stream` 的 docstring
    （start / think / delta / done / error）。body 与 `/api/ai/analyze` 完全一致。

    ⚠ 前置校验（代码 / 模型配置 / 模板）在 `start_analysis_stream` 里**同步**做完 ——
      不过就还是老老实实回 400 `{ok:false,error}`，前端不必「先把卡片画出来再拆掉」。
      （一旦开始推流就不能再改 HTTP 状态码了，所以校验必须在开流之前。）
    """
    b = body()
    code = str(b.get("ts_code") or b.get("code") or "").strip()
    if not code:
        return err("缺少 ts_code")
    try:
        _rec, gen = aia.start_analysis_stream(
            code, name=str(b.get("name") or ""), industry=str(b.get("industry") or ""),
            trade_date=_date(b.get("trade_date")), template_id=b.get("template_id"),
            source=str(b.get("source") or "stock"), strategy=str(b.get("strategy") or ""),
            close=b.get("close"), pct_chg=b.get("pct_chg"), profile_id=b.get("profile_id"))
    except aia.AIError as e:
        return err(str(e))

    def _sse_events():
        try:
            for ev in gen:
                yield "data: " + json.dumps(ev, ensure_ascii=False, default=str) + "\n\n"
        except Exception as e:  # noqa: BLE001  生成器自己已经落库了，这里只保证流能收干净
            log.warning("AI 流式响应中断：%s", e)
            yield "data: " + json.dumps({"type": "error", "error": f"{type(e).__name__}: {e}"},
                                        ensure_ascii=False) + "\n\n"

    # `X-Accel-Buffering: no` 是给将来可能加上的反向代理看的：不加的话 nginx 会
    # 把流攒成一整块再发，前端就变成「等半天、然后一次全出来」，流式白做。
    #
    # ⚠ `Connection: close` 不是随手写的（2026-09-23 实测踩过）：Flask 自带服务器
    #   （Werkzeug）在 HTTP/1.1 下会把这个无 Content-Length 的流按 chunked 发完、
    #   然后让连接**留在池子里等复用**。可实测下来，复用那条连接的下一个请求
    #   会一直卡住（浏览器发了、服务端连请求行都没读到；探针里第 1 轮 53ms、
    #   第 2 轮直接卡到超时）—— 也就是「分析跑完，紧接着去读历史/详情」会莫名转圈。
    #   一次分析就是一个请求，没有复用的必要：显式 close，让浏览器下次开新连接。
    return Response(_sse_events(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                             "Connection": "close"})


@app.route("/api/ai/analysis")
def api_ai_analysis_list():
    """历史记录：`?ts_code=` 可选，`?limit=` 默认 20（**不含正文**，只给 160 字预览）。"""
    return ok(aia.list_analysis(request.args.get("ts_code"),
                                _int(request.args.get("limit"), 20, "limit")))


@app.route("/api/ai/analysis/<int:rid>")
def api_ai_analysis_get(rid):
    r = aia.get_analysis(rid)
    if not r:
        return err("记录不存在", 404)
    return ok(r)


@app.route("/api/ai/analysis/<int:rid>/delete", methods=["POST"])
def api_ai_analysis_delete(rid):
    if not aia.delete_analysis(rid):
        return err("记录不存在", 404)
    return ok({"deleted": rid})


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def _abort_orphan_runs() -> None:
    """把库里仍挂 'running' 的回测如实标成「中断」。

    任务线程只活在内存（core/jobs._JOBS），服务一重启就全没了；但记录里的 status 还写着
    running，进度百分比也冻在最后一帧 —— 列表里于是出现一条「看着还在跑、其实早死了」的记录
    （点进去也永远没有结果）。启动这一刻内存里必然是空的，所以此刻的 running 一定是孤儿：
    标成 error 并留下一句说明，**记录不删**。
    """
    n = db.execute(
        "UPDATE backtest_run SET status='error', finished_at=NOW(), "
        "progress_msg='服务重启，回测已中断', "
        "log=CONCAT(IFNULL(log,''), IF(IFNULL(log,'')='', '', '\\n'), "
        "'[中断] 服务重启，该回测未跑完') WHERE status='running'")
    if n:
        log.warning("启动清理：%s 条 running 回测标为中断（任务只在内存，重启即失效）", n)
    # 本功能上线前跑完的老记录 progress 还是默认 0 —— 界面不显示它，但「已完成却写着 0%」
    # 是句假话，顺手补齐（只补 0，不覆盖真进度）。
    m = db.execute("UPDATE backtest_run SET progress=1 "
                   "WHERE status='done' AND IFNULL(progress,0)=0")
    if m:
        log.info("启动清理：%s 条已完成回测的进度补成 100%%", m)


def bootstrap():
    with logx.timer("数据库初始化"):
        db.init_schema()
        st.ensure_builtins()
        _abort_orphan_runs()
        # 同步日志同理：同步任务也只活在内存，启动这一刻还挂 running 的必然是孤儿。
        # 不收尾的话历史里会积一排「看着还在跑、其实早结束」的记录（行数与结束时间还都空着）。
        sync.close_stale_logs()
        # AI：内置语句模板幂等补齐 + 至少一条模型配置且恰好一条是当前 +
        # 把上辈子留下的 running 分析标成中断（跑分析的是内存线程，服务一重启线程就没了）。
        aia.ensure_builtins()
        llm.ensure_profiles()
        aia.abort_orphan_runs()
    log.info("启动完成，日志目录：%s（级别 %s）", logx.log_path(), LOG_LEVEL)


def main():
    bootstrap()
    print(f"\n  短线选股工作台  ->  http://{FLASK_HOST}:{FLASK_PORT}")
    print(f"  运行日志        ->  {logx.log_path()}  （级别 {LOG_LEVEL}）\n")
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=FLASK_DEBUG, threaded=True)


if __name__ == "__main__":
    main()
