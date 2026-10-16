# -*- coding: utf-8 -*-
"""东财概念：个股所属概念（板块）的抓取、落库与读取。

数据源是**东方财富 F10「核心题材」**这个公开接口：

    GET https://datacenter-web.eastmoney.com/api/data/v1/get
        ?reportName=RPT_F10_CORETHEME_BOARDTYPE&columns=ALL
        &filter=(SECUCODE="600000.SH")&source=HSF10&client=PC

它不是 tushare、**不消耗额度**（与 `core/hot.py` 的热股榜同性质）。也正因为在站外，
必须当它随时不通：单只失败**只记账不影响别人**，页面读的永远是本地库。

## 为什么只认 F10，不用 push2 的「个股所属板块」

`push2.eastmoney.com/api/qt/slist/get?spt=3` 是个**混装列表** —— 实测浦发银行 24 条 =
行业(银行Ⅱ) + 地域(上海板块) + 指数成分(HS300_/上证180_/中证500) + 标签(融资融券/
沪股通/MSCI中国/机构重仓) + 概念(区块链/蚂蚁金服概念/跨境支付)，真概念只有 3~5 条，
**而且没有入选理由**。F10 这份每行带 `SELECTED_BOARD_REASON`（入选理由原文，含日期）
与 `IS_PRECISE`（1=精确概念 / 0=宽泛标签），所以**只用 F10**。

## 与 `stock_basic.industry` 的分工

`industry` 是**东财一级行业、一股一个**（~100 类）。概念是**多对多** ——
一只票可以同时是「CRO + 重组蛋白 + 减肥药 + 创新药」。要做「板块共振」这类判断
只能用概念，拿行业做会把颗粒度做粗一个量级。

## 限速（实测，别调快）

东财对 `push2` 限频很狠：连打十几发之后**连之前成功的请求也一起被断连**
（`RemoteDisconnected`），换节点 / 带 `ut` / 预热 cookie 都没用。所以这里：

· 并发 `WORKERS=5`、每线程每次请求前 `DELAY` 秒、每批之间再停 `BATCH_SLEEP` 秒
  ⇒ 稳态约 10 req/s，全市场 ~5500 只 ≈ 8~10 分钟（一次性）；
· 失败**指数退避重试** `RETRY` 次；
· `sync_concept_quotes()` 全程只打 **1 发**（一次拿回全部 500+ 板块）。
"""
from __future__ import annotations

import datetime as dt
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from core import db, jobs, log as _log, market

_logc = _log.get("concept")

TIMEOUT = 12
WORKERS = 5          # 并发线程数
DELAY = 0.45         # 每线程每次请求前的间隔（限速，见模块头）
BATCH = 400          # 每批多少只
BATCH_SLEEP = 1.0    # 批间停顿
RETRY = 3

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")
_HDR = {"User-Agent": _UA, "Accept": "application/json, text/plain, */*"}

F10_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
LIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
# push2 认 Referer，不带容易被回空
_LIST_HDR = {**_HDR, "Referer": "https://quote.eastmoney.com/"}
# push2 的公共 ut（东财页面自己用的那个，不是鉴权 token）
_UT = "bd1d9ddb04089700cf9c27f6f7426281"

_local = threading.local()

INS_SQL = """
    INSERT INTO stock_concept
      (ts_code,board_code,board_name,reason,is_concept,board_rank,synced_at)
    VALUES (%s,%s,%s,%s,%s,%s,%s)
    ON DUPLICATE KEY UPDATE board_name=VALUES(board_name),reason=VALUES(reason),
      is_concept=VALUES(is_concept),board_rank=VALUES(board_rank),
      synced_at=VALUES(synced_at)
"""


def _sess() -> requests.Session:
    """一个线程一个 Session（连接复用；requests 的 Session 不是线程安全的）。"""
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update(_HDR)
        _local.s = s
    return s


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------

# ⚠ 东财对「这票没有数据」**不是** `success:true + result:null`，而是
#   `success:false, code:9201, message:"返回数据为空"` —— 实测退市票（PT金田A/国华退）
#   与根本不存在的代码（999999.SZ）返回**完全一样**。所以这个 message 只能理解成
#   「东财没有这票的数据」，是**可靠的空结果**（可清本地旧数据），不是网络失败。
#   上一轮全市场同步就是漏了这条，把 339 只退市票记成了「抓取失败」。
_EMPTY_CODES = {9201}
_EMPTY_MARKS = ("为空", "无数据", "没有数据", "no data")


def _is_empty_result(payload: dict) -> bool:
    """`success=false` 时，这条是不是「东财说没数据」（→ `[]`）而不是真失败（→ `None`）。"""
    if payload.get("code") in _EMPTY_CODES:
        return True
    msg = str(payload.get("message") or "").lower()
    return any(m in msg for m in _EMPTY_MARKS)


def parse_f10(payload: dict) -> list[dict] | None:
    """F10 返回体 → 概念列表。

    · 返回 `None` = **这次没抓到**（调用方别动库）；
    · 返回 `[]`   = **东财可靠地告诉你这票没有题材数据**（可清本地旧数据）。
    """
    if not payload.get("success"):
        # 见 _EMPTY_CODES 的说明：9201「返回数据为空」是空结果，不是失败。
        return [] if _is_empty_result(payload) else None
    res = payload.get("result")
    if res is None:
        # 成功的返回里 result 仍可能为 null（同样表示没有题材）—— 也按空结果处理。
        return []
    data = res.get("data")
    if data is None:
        return []
    out: dict[str, dict] = {}
    for it in data:
        name = (it.get("BOARD_NAME") or "").strip()
        if not name:
            continue
        # NEW_BOARD_CODE 是带 BK 前缀的新代码（与板块行情接口同一个代码体系）；
        # 老的 BOARD_CODE 是纯数字（"937"），只在没有新代码时兜底。
        code = (it.get("NEW_BOARD_CODE") or "").strip()
        if not code:
            raw = str(it.get("BOARD_CODE") or "").strip()
            code = ("BK" + raw) if raw else ""
        if not code:
            continue
        reason = (it.get("SELECTED_BOARD_REASON") or "").strip() or None
        rank = it.get("BOARD_RANK")
        try:
            rank = int(rank) if rank is not None else None
        except (TypeError, ValueError):
            rank = None
        out[code] = {
            "board_code": code,
            "board_name": name[:80],
            "reason": reason,
            # ⚠ 「是不是概念」的判据 = **有没有入选理由**，不是东财的 `IS_PRECISE`：
            #   `IS_PRECISE` 标 1 的里面混着行业（银行/白酒Ⅱ/医药生物）、风格（大盘股/
            #   权重股/低市净率）、指数成分（上证50_/深证100R）；而**有理由的恰好全是真概念**
            #   （蚂蚁金服概念/区块链/麒麟电池/CAR-T细胞疗法），连 `锂电池`(行业) 与
            #   `锂电池概念`(概念) 都能分开。详见 db.SCHEMA_SQL 里那段说明。
            "is_concept": 1 if reason else 0,
            "board_rank": rank,
        }
    rows = list(out.values())
    rows.sort(key=lambda x: (x["board_rank"] is None, x["board_rank"] or 0, x["board_name"]))
    return rows


def fetch_concepts(ts_code: str) -> list[dict] | None:
    """抓一只票的概念。

    返回 `None` = 这次没抓到（网络/风控/解析失败）—— 调用方**不要**删它的旧数据；
    返回 `[]` = 东财说它确实没有题材数据 —— 可以清空。
    """
    params = {
        "reportName": "RPT_F10_CORETHEME_BOARDTYPE", "columns": "ALL",
        "quoteColumns": "", "filter": '(SECUCODE="%s")' % ts_code,
        "pageNumber": 1, "pageSize": 100, "sortTypes": "", "sortColumns": "",
        "source": "HSF10", "client": "PC",
    }
    hdr = {**_HDR, "Referer": "https://emweb.securities.eastmoney.com/"}
    for i in range(RETRY):
        time.sleep(DELAY)
        try:
            r = _sess().get(F10_URL, params=params, headers=hdr, timeout=TIMEOUT)
            r.raise_for_status()
            got = parse_f10(r.json())
            if got is not None:
                return got
        except Exception as e:  # noqa: BLE001 —— 站外接口，什么都可能发生
            _logc.debug("概念 %s 第 %d 次失败：%r", ts_code, i + 1, e)
        time.sleep(min(1.5 ** i, 8))
    return None


def fetch_quotes() -> list[dict] | None:
    """概念板块**行情快照**（东财只给当下，没有历史）。一次请求拿全部板块。"""
    params = {"pn": 1, "pz": 800, "po": 1, "np": 1, "ut": _UT, "fltt": 2, "invt": 2,
              "fid": "f3", "fs": "m:90 t:3", "fields": "f12,f14,f3,f104,f105,f128"}
    for i in range(RETRY):
        try:
            r = _sess().get(LIST_URL, params=params, headers=_LIST_HDR, timeout=TIMEOUT)
            r.raise_for_status()
            diff = (r.json().get("data") or {}).get("diff") or []
            if isinstance(diff, dict):
                diff = list(diff.values())
            out = []
            for x in diff:
                code = (x.get("f12") or "").strip()
                name = (x.get("f14") or "").strip()
                if not code or not name:
                    continue
                out.append({"board_code": code, "board_name": name,
                            "pct_chg": _num(x.get("f3")), "up_cnt": _int(x.get("f104")),
                            "down_cnt": _int(x.get("f105")),
                            "lead_name": (x.get("f128") or None)})
            return out
        except Exception as e:  # noqa: BLE001
            _logc.debug("概念板块行情第 %d 次失败：%r", i + 1, e)
            time.sleep(min(1.5 ** i, 8))
    return None


def _num(v):
    try:
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 落库
# ---------------------------------------------------------------------------

def all_listed_codes() -> list[str]:
    return [r["ts_code"] for r in db.query(
        "SELECT ts_code FROM stock_basic WHERE list_status='L' ORDER BY ts_code")]


def refresh_basic() -> None:
    """把 `stock_concept` 里出现过的板块汇总进 `concept_basic`，并回填本地成分股数。

    ⚠ `member_cnt` 是**本地库里该概念覆盖到多少只**（≈ 全市场，但以本地抓取结果为准），
    不是东财官方口径 —— 页面文案按这个说，别写成「成分股共 N 只」。
    """
    # 有的板块只有名单、没有行情快照（push2 那次失败时就是这状态）——
    # 补一行进去，页面上「概念名」照常显示，只是没有涨跌幅。
    db.execute(
        "INSERT IGNORE INTO concept_basic (board_code, board_name) "
        "SELECT board_code, MAX(board_name) FROM stock_concept GROUP BY board_code")
    db.execute(
        "UPDATE concept_basic cb JOIN "
        "(SELECT board_code, COUNT(*) c FROM stock_concept GROUP BY board_code) t "
        "ON t.board_code = cb.board_code SET cb.member_cnt = t.c")


def save_concepts(ts_code: str, rows: list[dict]) -> int:
    """按票**整体替换**（先删后插，天然幂等，与 `sync.sync_holder_trade` 同一做法）。

    返回**写入行数**（自己数 `len(rows)`）—— 不是 `db` 的受影响行数，
    那张数在 `ON DUPLICATE KEY UPDATE` 命中且值没变时是 0（见 db.insert_many 的说明）。
    """
    db.execute("DELETE FROM stock_concept WHERE ts_code=%s", (ts_code,))
    if not rows:
        return 0
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    vals = [(ts_code, x["board_code"], x["board_name"], x["reason"],
             x["is_concept"], x["board_rank"], now) for x in rows]
    db.insert_many(INS_SQL, vals)
    return len(vals)


def sync_concepts(codes: list[str] | None = None, jid: str | None = None,
                  workers: int = WORKERS) -> dict:
    """抓概念并落库。`codes=None` → 全部在市股票。

    返回 `{codes, saved, empty, failed, rows, elapsed, quotes}`。
    **单只失败只记名**（`failed`），不动它已有的数据 —— 「没抓到」不等于「这只票没概念」。
    """
    t0 = time.time()
    codes = list(codes) if codes else all_listed_codes()
    n = len(codes)
    saved = empty = 0
    rows_total = 0
    failed: list[str] = []

    if jid:
        jobs.update(jid, progress=0.0, message=f"概念：{n} 只待抓取")

    for bi in range(0, n, BATCH):
        batch = codes[bi: bi + BATCH]
        with ThreadPoolExecutor(max_workers=workers) as ex:
            got = list(ex.map(fetch_concepts, batch))
        for code, res in zip(batch, got):
            if res is None:
                failed.append(code)
                continue
            rows_total += save_concepts(code, res)
            saved += 1
            if not res:
                empty += 1
        done = min(bi + BATCH, n)
        if jid:
            jobs.update(jid, progress=done / max(n, 1),
                        message=f"概念：{done}/{n}（失败 {len(failed)}）")
        if done < n:
            time.sleep(BATCH_SLEEP)

    # 名单有变化 → 补上板块行、回填本地成分数（失败也不该让整件事丢结果）
    try:
        refresh_basic()
    except Exception as e:  # noqa: BLE001
        _logc.warning("概念板块汇总失败：%s: %s", type(e).__name__, e)

    quotes = {"src": "none", "boards": 0}
    try:
        quotes = sync_concept_quotes(market.latest_trade_date())
    except Exception as e:  # noqa: BLE001
        _logc.warning("概念板块行情同步失败：%s: %s", type(e).__name__, e)

    out = {"codes": n, "saved": saved, "empty": empty, "failed": len(failed),
           "failed_codes": failed[:50], "rows": rows_total,
           "quotes": quotes["boards"], "quote_src": quotes["src"],
           "elapsed": round(time.time() - t0, 1)}
    _logc.info("概念同步完成 %s", out)
    if jid:
        jobs.update(jid, progress=1.0,
                    message=f"概念：{saved} 只有数据、{empty} 只无题材、失败 {len(failed)}，"
                            f"共 {rows_total} 行")
    return out


def sync_concept_quotes(asof: str | None = None, jid: str | None = None) -> dict:
    """同步概念板块**行情快照**。优先东财；取不到就**用本库行情等权自算**兜底。

    为什么要有兜底：东财 push2 限频很狠 —— 实测连打十几发之后连之前成功过的请求
    也一起断连，换节点 / 带 `ut` / 预热 cookie 全无效。板块涨跌幅只是页面上的一个
    增强项，不该因为它拖垮整个概念同步。

    返回 `{"src": "eastmoney"|"local", "boards": n}`。两份口径**不是同一个数**
    （东财算全部成分股；本地只算本库覆盖到的），所以 `concept_basic.quote_src` 必须记下来，
    页面文案也照它说 —— 别把本地等权平均说成「东财板块涨幅」。
    """
    got = fetch_quotes()
    if got:
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        vals = [(x["board_code"], x["board_name"], x["pct_chg"], x["up_cnt"],
                 x["down_cnt"], x["lead_name"], now, "eastmoney") for x in got]
        db.insert_many(
            "INSERT INTO concept_basic "
            "(board_code,board_name,pct_chg,up_cnt,down_cnt,lead_name,quoted_at,quote_src) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE board_name=VALUES(board_name),pct_chg=VALUES(pct_chg),"
            "up_cnt=VALUES(up_cnt),down_cnt=VALUES(down_cnt),lead_name=VALUES(lead_name),"
            "quoted_at=VALUES(quoted_at),quote_src=VALUES(quote_src)", vals)
        if jid:
            jobs.update(jid, log=f"概念板块行情（东财）{len(vals)} 个")
        return {"src": "eastmoney", "boards": len(vals)}

    _logc.warning("概念板块行情取不到（东财 push2 限频），改用本库行情等权自算")
    n = local_concept_quotes(asof, jid)
    return {"src": "local", "boards": n}


def local_concept_quotes(asof: str | None = None, jid: str | None = None) -> int:
    """**用本库行情自己算**概念板块涨跌幅（东财取不到时的兜底）。返回更新的板块数。

    口径：该概念下**本库覆盖到的**成分股在 `asof` 当日的 `pct_chg` **等权平均**
    （只算真概念 `is_concept=1`）。`up_cnt`/`down_cnt` 是家数，`lead_name` 留空
    （东财才给领涨股）。日期用**交易日**（`quoted_at` = 该日 00:00），与东财那份
    「取数时刻」语义不同，靠 `quote_src` 区分。
    """
    from core import market
    asof = asof or market.latest_trade_date()
    if not asof:
        return 0
    n = db.scalar(
        "SELECT COUNT(DISTINCT sc.board_code) FROM stock_concept sc "
        "JOIN daily_kline dk ON dk.ts_code = sc.ts_code AND dk.trade_date = %s "
        "WHERE sc.is_concept = 1", (asof,)) or 0
    if not n:
        return 0
    db.execute(
        "INSERT INTO concept_basic "
        "(board_code,board_name,pct_chg,up_cnt,down_cnt,lead_name,quoted_at,quote_src) "
        "SELECT sc.board_code, MAX(sc.board_name), ROUND(AVG(dk.pct_chg), 2), "
        "       SUM(dk.pct_chg > 0), SUM(dk.pct_chg < 0), NULL, %s, 'local' "
        "  FROM stock_concept sc "
        "  JOIN daily_kline dk ON dk.ts_code = sc.ts_code AND dk.trade_date = %s "
        " WHERE sc.is_concept = 1 "
        " GROUP BY sc.board_code "
        "ON DUPLICATE KEY UPDATE pct_chg=VALUES(pct_chg), up_cnt=VALUES(up_cnt), "
        "  down_cnt=VALUES(down_cnt), quoted_at=VALUES(quoted_at), quote_src=VALUES(quote_src)",
        (f"{asof} 00:00:00", asof))
    if jid:
        jobs.update(jid, log=f"概念板块行情（本库等权自算，{asof}）{n} 个")
    return n


# ---------------------------------------------------------------------------
# 读取（个股诊断页）
# ---------------------------------------------------------------------------

def concept_snapshot(ts_code: str) -> dict:
    """一只票的**概念**，给诊断页用。

    只回 `is_concept=1` 的那批（真概念），非概念标记（行业/地域/指数成分/风格）只报个数 ——
    实测一只票 24~41 条里真概念只有 3~14 条，全铺出来会把重点淹掉。

    文本（涨跌幅 `+3.80%`）与涨跌色（`up`/`down`/`flat`）**都在这里算好** ——
    前端不判正负、不拼百分号（与 `chips_snapshot` 同一个规矩）。
    """
    rows = db.query(
        "SELECT sc.board_code, sc.board_name, sc.reason, sc.board_rank,"
        "       cb.pct_chg, cb.up_cnt, cb.down_cnt, cb.lead_name, cb.member_cnt,"
        "       cb.quoted_at, cb.quote_src"
        "  FROM stock_concept sc"
        "  LEFT JOIN concept_basic cb ON cb.board_code = sc.board_code"
        " WHERE sc.ts_code=%s AND sc.is_concept=1"
        " ORDER BY sc.board_rank IS NULL, sc.board_rank, sc.board_name",
        (ts_code,))
    marks = db.scalar(
        "SELECT COUNT(*) FROM stock_concept WHERE ts_code=%s AND is_concept=0",
        (ts_code,)) or 0
    total = len(rows) + marks
    if not total:
        return {"available": False, "items": [], "count": 0, "marks": 0,
                "note": "本地还没有这只票的概念数据 —— 跑一次「同步数据 → 概念」",
                "synced_at": None, "quoted_at": ""}
    items = []
    for r in rows:
        pct = r["pct_chg"]
        pct = None if pct is None else float(pct)
        items.append({
            "code": r["board_code"], "name": r["board_name"],
            "reason": r["reason"] or "",
            "pct_chg": pct,
            "pct_text": ("—" if pct is None else f"{pct:+.2f}%"),
            "pct_cls": ("" if pct is None else ("up" if pct > 0 else
                                                ("down" if pct < 0 else "flat"))),
            "up_cnt": r["up_cnt"], "down_cnt": r["down_cnt"],
            "lead": r["lead_name"] or "",
            "members": r["member_cnt"],
        })
    synced = db.scalar(
        "SELECT MAX(synced_at) FROM stock_concept WHERE ts_code=%s", (ts_code,))
    src = (rows[0]["quote_src"] if rows else "") or ""
    quoted = str(rows[0]["quoted_at"] or "")[:16] if rows else ""
    note = f"共 {len(items)} 个概念（东方财富 F10 题材库）"
    if marks:
        note += f"；另有 {marks} 条非概念标记（行业/地域/指数成分/风格）未列出"
    if quoted:
        # ⚠ 一定要说清是**哪一份口径**：东财那份是取数时刻的板块行情，
        #   本库兜底那份是「当天成分股等权平均」，两个数不是一回事。
        note += (" · 板块涨跌幅为本库 " + quoted[:10] + " 收盘等权自算"
                 if src == "local" else f" · 板块涨跌幅为 {quoted} 的快照")
    return {"available": bool(items), "items": items, "count": len(items),
            "marks": marks, "note": note, "quote_src": src,
            "synced_at": str(synced)[:19] if synced else None, "quoted_at": quoted}


MEMBERS_LIMIT = 100      # 一屏先给多少只
MEMBERS_MAX = 3000       # 上限（最大的真概念「央国企改革」1449 只，要留够一次拉全的余量）
# 排序白名单：**只认这几个名字**，别把用户输入拼进 ORDER BY
MEMBERS_ORDERS = {
    "pct": ("dk.pct_chg DESC, sc.ts_code", "涨跌幅降序"),
    "pct_asc": ("dk.pct_chg ASC, sc.ts_code", "涨跌幅升序"),
    "code": ("sc.ts_code ASC", "代码升序"),
}


def concept_members(board_code: str, trade_date: str | None = None,
                    limit: int = MEMBERS_LIMIT, order: str = "pct") -> dict:
    """一条概念 → **一批票**（本库口径），给诊断页「双击概念标签」看成分股用。

    与 `concept_snapshot()`（一只票 → 它的概念）方向相反，别混用。

    · 只列 `is_concept=1` 的关联行 —— 与诊断页同一套判据（`SELECTED_BOARD_REASON`
      非空才算概念），非概念标记另外报个数；全库 1030 个板块里只有 5 个存在
      「同一板块对 A 票是概念、对 B 票是标签」的混装。
    · 行情取**本库日线** `trade_date` 那天（默认最新交易日）⇒ 列表里的收盘/涨跌幅
      与页面上其他数字**同源**。那天没行情的票（退市/停牌）不列，只报个数。
    · 涨跌幅文本（`+3.80%`）与涨跌色（`up`/`down`/`flat`）在这里算好，前端不判正负。
    · `trade_date` 传了就用传的（**不回退**）：页面锚点是哪天，列表就得是哪天，
      悄悄换成另一天 = 两份口径（硬规矩 3）。
    """
    bc = (board_code or "").strip().upper()
    if not bc:
        return {"available": False, "board_code": "", "board_name": "", "stocks": [],
                "count": 0, "total": 0, "linked": 0, "marks": 0,
                "trade_date": (trade_date or None), "requested": (trade_date or None),
                "stat": None, "order": order, "limit": MEMBERS_LIMIT,
                "note": "没给板块代码"}
    if order not in MEMBERS_ORDERS:
        order = "pct"
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = MEMBERS_LIMIT
    limit = max(1, min(limit, MEMBERS_MAX))

    name = (db.scalar("SELECT board_name FROM concept_basic WHERE board_code=%s", (bc,))
            or db.scalar("SELECT MAX(board_name) FROM stock_concept WHERE board_code=%s", (bc,))
            or "")
    linked = db.scalar("SELECT COUNT(*) FROM stock_concept "
                       "WHERE board_code=%s AND is_concept=1", (bc,)) or 0
    marks = db.scalar("SELECT COUNT(*) FROM stock_concept "
                      "WHERE board_code=%s AND is_concept=0", (bc,)) or 0

    req = (trade_date or "").strip() or None
    day = req or market.latest_trade_date()
    base = {"board_code": bc, "board_name": name, "trade_date": day, "requested": req,
            "fallback_from": None, "linked": int(linked), "marks": int(marks),
            "limit": limit, "order": order, "order_label": MEMBERS_ORDERS[order][1]}
    if not day:
        return {**base, "available": False, "stocks": [], "count": 0, "total": 0,
                "stat": None, "note": "本地还没有日线数据"}
    if not linked:
        return {**base, "available": False, "stocks": [], "count": 0, "total": 0,
                "stat": None,
                "note": (f"本库没有「{name or bc}」的成分股关联 —— 跑一次「同步数据 → 概念」"
                         if not marks else f"「{name or bc}」只有 {marks} 条非概念标记")}

    # 统计口径与列表**同一段 SQL 条件**（只差聚合），否则「平均 +1.2%」与屏幕上的行对不上
    JOIN = ("FROM stock_concept sc "
            "JOIN daily_kline dk ON dk.ts_code=sc.ts_code AND dk.trade_date=%s "
            "WHERE sc.board_code=%s AND sc.is_concept=1")

    def _at(d):
        s = db.query_one(
            f"SELECT COUNT(*) n, SUM(dk.pct_chg>0) up, SUM(dk.pct_chg<0) dn, "
            f"       SUM(dk.pct_chg=0) fl, AVG(dk.pct_chg) av {JOIN}", (d, bc)) or {}
        rs = db.query(
            "SELECT sc.ts_code, sb.name, dk.close, dk.pct_chg, sc.reason "
            "  FROM stock_concept sc "
            "  JOIN daily_kline dk ON dk.ts_code=sc.ts_code AND dk.trade_date=%s "
            "  LEFT JOIN stock_basic sb ON sb.ts_code=sc.ts_code "
            " WHERE sc.board_code=%s AND sc.is_concept=1 "
            f" ORDER BY {MEMBERS_ORDERS[order][0]} LIMIT {limit}", (d, bc))
        return s, rs

    st, rows = _at(day)
    # ⚠ 诊断页切到**周K** 时，anchor 是「已收盘完整周的周五标签」，可能根本不是交易日
    #   （2026-09-25 中秋休市，本地就没有那一行）—— 直接回空列表等于没法用。所以这里
    #   显式回退到「≤ 该日的最近一个交易日」，并把回退写进 note（说清了就不算换口径）。
    if req and not int(st.get("n") or 0):
        alt = market.latest_trade_date(day)
        if alt and str(alt) != str(day):
            base["fallback_from"], day = day, alt
            base["trade_date"] = day
            st, rows = _at(day)
    total = int(st.get("n") or 0)

    def _f(v):
        return None if v is None else float(v)

    stocks = []
    for r in rows:
        pct, close = _f(r["pct_chg"]), _f(r["close"])
        stocks.append({
            "ts_code": r["ts_code"], "code": (r["ts_code"] or "").split(".")[0],
            "name": r["name"] or "",
            "close": close,
            "close_text": "—" if close is None else f"{close:.2f}",
            "pct_chg": pct,
            "pct_text": "—" if pct is None else f"{pct:+.2f}%",
            "pct_cls": ("" if pct is None else
                        ("up" if pct > 0 else ("down" if pct < 0 else "flat"))),
            # 这只票挂在**这个概念下**的入选理由（东财原文）—— 列表里做 hover 提示用
            "reason": r["reason"] or "",
        })
    av = _f(st.get("av"))
    stat = {"up": int(st.get("up") or 0), "down": int(st.get("dn") or 0),
            "flat": int(st.get("fl") or 0), "avg": av,
            "avg_text": "—" if av is None else f"{av:+.2f}%",
            "avg_cls": ("" if av is None else
                        ("up" if av > 0 else ("down" if av < 0 else "flat")))}

    if not total:
        note = (f"{day} 这天本库没有「{name or bc}」的行情 —— "
                f"该概念成分股当天全都没数据（退市/停牌）")
    else:
        note = (f"本库 {int(linked)} 只，{day} 有行情 {total} 只"
                f"（{stat['up']} 涨 / {stat['down']} 跌 / {stat['flat']} 平）")
        if total > len(stocks):
            note += f"，按{base['order_label']}显示前 {len(stocks)} 只"
    if marks:
        note += f"；另有 {marks} 条非概念标记（行业/地域/指数成分/风格）未列"
    if base["fallback_from"]:
        note = (f"查询日 {base['fallback_from']} 本库没有行情（不是交易日），"
                f"已按最近的交易日 {day} 显示 · ") + note
    return {**base, "available": bool(stocks), "stocks": stocks, "count": len(stocks),
            "total": total, "stat": stat, "note": note}


def concepts_of(codes: list[str]) -> dict[str, list[str]]:
    """批量取「代码 → 概念名列表」（**只要精确概念**）。给别的分析模块用。"""
    if not codes:
        return {}
    ph = ",".join(["%s"] * len(codes))
    out: dict[str, list[str]] = {}
    for r in db.query(
            f"SELECT ts_code, board_name FROM stock_concept "
            f"WHERE is_concept=1 AND ts_code IN ({ph}) "
            f"ORDER BY ts_code, board_rank IS NULL, board_rank", list(codes)):
        out.setdefault(r["ts_code"], []).append(r["board_name"])
    return out


def coverage() -> dict:
    """本地概念数据覆盖情况（同步页/自检用）。

    ⚠ `total_listed` 直接取 `stock_basic.list_status='L'` —— 但它**含退市/停牌票**：
    tushare 对已退市的票不翻 `list_status`。实测 339 只无概念数据里的样本 ——
    `000003.SZ PT金田A(退)`、`000004.SZ 国华退` 名字带「(退)」；
    而 `603056.SH 德邦股份` 名字干净、`industry` 也有，**但日线停在 2026-01-20**，
    早已不交易。所以「5572/5911」这个分母偏大，真正在市的大约 5572 只。

    · `untraded` = 在市名单里但**近 30 个自然日没有日线**的只数（退市/长期停牌）——
      这是**真判据**，别用 `name LIKE '%(退)'` 猜（那只数到 319，会漏掉德邦这类）。
    · `no_data`  = 库里 0 行的在市只数（东财说无题材；绝大多数就是它们）。
    """
    r = db.query_one(
        "SELECT COUNT(DISTINCT ts_code) c1, COUNT(*) c2, MAX(synced_at) t FROM stock_concept")
    b = db.query_one(
        "SELECT COUNT(*) c, MAX(quoted_at) q FROM concept_basic WHERE quoted_at IS NOT NULL")
    total = db.scalar("SELECT COUNT(*) FROM stock_basic WHERE list_status='L'") or 0
    no_data = db.scalar(
        "SELECT COUNT(*) FROM stock_basic b WHERE b.list_status='L' "
        "AND NOT EXISTS (SELECT 1 FROM stock_concept sc WHERE sc.ts_code=b.ts_code)") or 0
    latest = market.latest_trade_date()
    untraded = 0
    if latest:
        # ⚠ 必须用「LEFT JOIN 去重子查询」这个写法：`NOT EXISTS` / `NOT IN` 都会吃到坏计划
        #   （对 5911 只逐只回 PRIMARY(ts_code,trade_date) 里做区间查找），实测 **7.3s**；
        #   这个写法走 idx_trade_date 先筛 30 天、再去重，**0.08s**。
        untraded = db.scalar(
            "SELECT COUNT(*) FROM stock_basic b "
            "LEFT JOIN (SELECT DISTINCT ts_code FROM daily_kline "
            "           WHERE trade_date >= DATE_SUB(%s, INTERVAL 30 DAY)) t "
            "  ON t.ts_code = b.ts_code "
            "WHERE b.list_status='L' AND t.ts_code IS NULL", (latest,)) or 0
    return {"stocks": r["c1"] or 0, "total_listed": total, "links": r["c2"] or 0,
            "no_data": no_data, "untraded": untraded,
            "synced_at": str(r["t"])[:19] if r["t"] else None,
            "boards": db.scalar("SELECT COUNT(*) FROM concept_basic") or 0,
            "quoted_boards": b["c"] or 0,
            "quoted_at": str(b["q"])[:19] if b["q"] else None}
