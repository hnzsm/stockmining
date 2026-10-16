# -*- coding: utf-8 -*-
"""AI 分析业务层：语句模板、本地行情上下文、异步分析、记录落库。

分工（别把两层的活混着写）：

    core/llm.py   怎么跟模型说话（预设 / 配置 / 传输 / 错误人话化）
    core/ai.py    说什么、拿回来的东西放哪儿（本文件）

## 一次「AI 分析」的完整链路

两条收法，**上下文/提示词/落库口径完全共用**，只在「正文是一次性取还是边生成边送」上不同：

```
POST /api/ai/analyze            （轮询式：接口立刻回 running，前端 1.2s 一次 /analysis/<id>）
  ├─ 建一条 ai_analysis（status='running'）→ 立刻回 id
  └─ 后台线程 _run：
       ① 取默认（或指定）语句模板
       ② 需要 {本地数据} 时走 screener.screen_detail 拉**同一份**本地快照
       ③ llm.render 渲染 → 有认不出的占位符就记下来（不静默）
       ④ llm.chat 调模型（一次性拿全文）
       ⑤ 回写 status/content/model/耗时/token

POST /api/ai/analyze/stream     （流式：text/event-stream，前端边收边画）
  ├─ start_analysis_stream：**同步**做完前置校验并落 running（校验不过直接 400）
  └─ _run_stream 生成器：
       ①~③ 同上（同一个 _prompt_of）
       ④ llm.chat_stream 逐块往外送（delta / think 事件）
       ⑤ 结束时把**完整正文**落库，并把落库那一份随 done 事件回给前端
```

**为什么先落 running 再后台跑**：模型调用动辄十几秒到两分钟，同步请求会把浏览器
和 nginx 一起挂在超时上；而且页面一刷新，结果就没了。落库 + 轮询这套跟回测
（`backtest_run`）是同一个想法，历史也能回看。**流式那条同理**：正文一直落库，
所以流断了、页面关了，已经生成的部分与错误原因都还在。

⚠ 两条别互相抄：轮询那条**会复用**同一只票进行中的记录（防连点）；流式那条**不能复用**
（复用就意味着这次不推流，前端会一直空着）—— 它改成把旧的 running 标成「已被新的分析取代」。

## 本地数据上下文的口径

`{本地数据}` 里的每一个数都来自 `screener.screen_detail(..., sel_only=True)`
—— **和个股诊断页看的是同一份**（前复权、日线/周线 asof、筹码口径全在内），
这里只做「挑哪几个数、摆成几行」，不重算、不换算单位（文本本来就是后端算好的）。
"""
from __future__ import annotations

import threading
import traceback

from core import db, llm, log as _log
from core import screener

_log_ai = _log.get("ai")

# ---------------------------------------------------------------------------
# 内置语句模板
# ---------------------------------------------------------------------------
# 与 pool_strategy 的内置策略同一套做法：按名字幂等补齐（同名跳过，不 force 不覆盖），
# 所以每次启动跑一遍是安全的 —— 老库也能看到后来新加的内置模板。
BUILTIN_TEMPLATES: list[dict] = [
    {
        "name": "亮点与消息面",
        "template": "分析{股票名称}有何亮点，近期有无利好利空消息。",
        "is_default": 1,
    },
    {
        "name": "技术面与资金面速评",
        "template": (
            "以下是{股票名称}（{股票代码}，{行业}）在 {日期} 的本地行情快照：\n"
            "{本地数据}\n\n"
            "请用不超过 300 字点评：① 现在的技术形态（趋势/位置/量能）；"
            "② 资金与筹码动向；③ 主要风险点。\n"
            "数据里没提到的信息（例如最新公告、消息面）直接写「未知」，不要编造。"
        ),
        "is_default": 0,
    },
    {
        "name": "短线买卖点参考",
        "template": (
            "基于下面这份 {日期} 的本地数据，判断{股票名称}接下来的短线（3~10 个交易日）"
            "买卖点参考：支撑位 / 压力位 / 止损参考 / 现在追高是否划算。\n"
            "{本地数据}\n\n"
            "结论先行：先给一句判断，再列 3 条以内的依据。"
        ),
        "is_default": 0,
    },
]
"""开箱即用的模板。第一条就是需求里那句原话（不带数据，本地小模型也能答）。"""

MAX_PROMPT_CHARS = 200_000      # 兜底：别把离谱大的东西塞给模型
RUNNING_REUSE_SEC = 600         # 同一只票 10 分钟内已有 running → 复用它（防连点/防刷）


class AIError(RuntimeError):
    """AI 分析里的业务错误（数据缺失、配置缺失等），message 已经可直接展示。"""


# ---------------------------------------------------------------------------
# 模板 CRUD
# ---------------------------------------------------------------------------

def _tpl_norm(row: dict | None) -> dict | None:
    if not row:
        return None
    row["is_default"] = bool(row.get("is_default"))
    row["builtin"] = bool(row.get("builtin"))
    row["created_at"] = str(row.get("created_at") or "")
    row["updated_at"] = str(row.get("updated_at") or "")
    # ⚠ 给界面看的是**原文**写法（`{股票名称}`），不是内部字段名（`name`）—— 见 `llm.raw_tokens`。
    row["vars"] = llm.raw_tokens(row.get("template") or "")
    return row


def list_templates() -> list[dict]:
    rows = db.query("SELECT * FROM ai_prompt ORDER BY is_default DESC, builtin DESC, id ASC")
    return [_tpl_norm(r) for r in rows]


def get_template(tid: int) -> dict | None:
    return _tpl_norm(db.query_one("SELECT * FROM ai_prompt WHERE id=%s", (tid,)))


def default_template() -> dict | None:
    """默认模板：`is_default=1` 那条；一条都没标就退到 id 最小的那条。"""
    r = db.query_one("SELECT * FROM ai_prompt WHERE is_default=1 ORDER BY id LIMIT 1")
    if not r:
        r = db.query_one("SELECT * FROM ai_prompt ORDER BY id LIMIT 1")
    return _tpl_norm(r)


def set_default(tid: int) -> bool:
    """把某条设成默认（**全局只留一条**，先清后置）。"""
    if not db.query_one("SELECT id FROM ai_prompt WHERE id=%s", (tid,)):
        return False
    db.execute("UPDATE ai_prompt SET is_default=0 WHERE is_default=1")
    db.execute("UPDATE ai_prompt SET is_default=1 WHERE id=%s", (tid,))
    return True


def save_template(name: str, template: str, tid: int | None = None,
                  is_default: bool | None = None) -> int:
    name = (name or "").strip()
    template = (template or "").strip()
    if not name:
        raise AIError("模板名不能为空")
    if not template:
        raise AIError("模板内容不能为空")
    if len(name) > 60:
        raise AIError("模板名太长（≤60 字）")
    dup = db.query_one("SELECT id FROM ai_prompt WHERE name=%s", (name,))
    if dup and (not tid or int(dup["id"]) != int(tid)):
        raise AIError(f"模板名「{name}」已存在")
    if tid:
        if not db.query_one("SELECT id FROM ai_prompt WHERE id=%s", (tid,)):
            raise AIError("模板不存在")
        db.execute("UPDATE ai_prompt SET name=%s, template=%s WHERE id=%s",
                   (name, template, tid))
        new_id = int(tid)
    else:
        new_id = int(db.insert(
            "INSERT INTO ai_prompt (name, template) VALUES (%s,%s)", (name, template)))
    if is_default:
        set_default(new_id)
    elif db.query_one("SELECT id FROM ai_prompt WHERE is_default=1") is None:
        set_default(new_id)         # 一条默认都没有 → 新的这条兜底
    return new_id


def delete_template(tid: int) -> bool:
    """删模板；**内置的不给删**（可改文案），默认那条也不给删（先换默认再删）。"""
    r = db.query_one("SELECT id, name, builtin, is_default FROM ai_prompt WHERE id=%s", (tid,))
    if not r:
        return False
    if r.get("builtin"):
        raise AIError("内置模板不能删除（可以改文案）")
    if r.get("is_default"):
        raise AIError("这是默认模板，先把别的设成默认再删")
    n = db.execute("DELETE FROM ai_prompt WHERE id=%s", (tid,))
    # 记录里的 template_id 置空（留 template_name 快照，历史照样看得懂）
    db.execute("UPDATE ai_analysis SET template_id=NULL WHERE template_id=%s", (tid,))
    return bool(n)


def ensure_builtins() -> None:
    """启动时补齐内置模板（同名跳过、不覆盖用户改过的文案）。"""
    try:
        for t in BUILTIN_TEMPLATES:
            if db.query_one("SELECT id FROM ai_prompt WHERE name=%s", (t["name"],)):
                continue
            db.execute(
                "INSERT INTO ai_prompt (name, template, is_default, builtin) "
                "VALUES (%s,%s,%s,1)",
                (t["name"], t["template"], int(t.get("is_default") or 0)))
        if db.query_one("SELECT id FROM ai_prompt WHERE is_default=1") is None:
            r = db.query_one("SELECT id FROM ai_prompt ORDER BY id LIMIT 1")
            if r:
                set_default(int(r["id"]))
    except Exception:  # noqa: BLE001  内置模板只是开箱内容，失败不该拖垮启动
        _log_ai.warning("补齐内置 AI 模板失败\n%s", traceback.format_exc()[-800:])


# ---------------------------------------------------------------------------
# 本地行情上下文
# ---------------------------------------------------------------------------

def _num(v) -> float | None:
    try:
        if v is None:
            return None
        f = float(v)
        return None if f != f else f      # NaN
    except (TypeError, ValueError):
        return None


def _s(v, d: int = 2, unit: str = "", sign: bool = False) -> str | None:
    f = _num(v)
    if f is None:
        return None
    t = f"{f:+,.{d}f}" if sign else f"{f:,.{d}f}"
    return t + unit


def _wan(v) -> str | None:      # 万元 → 亿/万
    f = _num(v)
    if f is None:
        return None
    if abs(f) >= 1e4:
        return f"{f / 1e4:,.2f} 亿"
    return f"{f:,.2f} 万"


def _qian_yi(v) -> str | None:  # 千元（tushare amount）→ 亿
    f = _num(v)
    return None if f is None else f"{f / 1e5:,.2f} 亿"


def _resolve(code: str) -> tuple[str, str, str, str]:
    """代码 → (ts_code, 名称, 行业, 板块)。与选股/诊断同源（stock_basic）。"""
    c = (code or "").strip().upper()
    if "." not in c and c.isdigit():
        row = db.query_one(
            "SELECT ts_code FROM stock_basic WHERE symbol=%s AND list_status='L' LIMIT 1", (c,))
        if row:
            c = row["ts_code"]
    info = db.query_one(
        "SELECT name, industry, market FROM stock_basic WHERE ts_code=%s", (c,)) or {}
    return c, str(info.get("name") or ""), str(info.get("industry") or ""), str(info.get("market") or "")


def _block(res: dict) -> str:
    """把 `screen_detail` 的返回摆成几行**给人读、也给模型读**的快照。"""
    rows = [r for r in (res.get("rows") or [])]
    ap = int(((res.get("anchor") or {}).get("index")) or 0)
    win = rows[:ap + 1]                      # ⚠ 只到查询日：查询日之后那几根是「未来」，不进提示词
    if not win:
        return "（本地没有该股票在查询日的行情数据）"
    last = win[-1]
    out: list[str] = []

    out.append(f"【标的】{res.get('name') or ''} {res.get('ts_code') or ''}"
               + (f" · {res.get('industry')}" if res.get("industry") else ""))
    out.append(f"【查询日】{((res.get('anchor') or {}).get('date')) or ''}"
               f"（本地最新交易日 {((res.get('view') or {}).get('last_bar')) or ''}）")

    close, pct = _num(last.get("close")), _num(last.get("pct_chg"))
    amt = _qian_yi(last.get("amount"))
    out.append(f"【行情】收盘 {_s(close)} 元，涨跌幅 {_s(pct, sign=True)}%"
               + (f"，成交额 {amt}元" if amt else ""))

    def cum(n: int) -> str | None:
        if len(win) <= n:
            return None
        a, b = _num(win[-1 - n].get("close")), _num(win[-1].get("close"))
        if not a or b is None:
            return None
        return f"{n} 日 {(b / a - 1) * 100:+.2f}%"

    cums = [x for x in (cum(5), cum(20), cum(60)) if x]
    if cums:
        out.append("【区间涨跌】" + " / ".join(cums))

    ma = {k: _num(last.get(k)) for k in ("ma5", "ma10", "ma20", "ma60")}
    if any(v is not None for v in ma.values()):
        txt = " / ".join(f"{k.upper()} {_s(v)}" for k, v in ma.items() if v is not None)
        rel = []
        if close is not None and ma.get("ma20") is not None:
            rel.append("收盘在 MA20 " + ("上方" if close >= ma["ma20"] else "下方"))
        if close is not None and ma.get("ma60") is not None:
            rel.append("MA60 " + ("上方" if close >= ma["ma60"] else "下方"))
        out.append(f"【均线】{txt}" + (f"（{'、'.join(rel)}）" if rel else ""))

    bu, bm, bl = (_num(last.get(k)) for k in ("boll_upper", "boll_mid", "boll_lower"))
    if any(v is not None for v in (bu, bm, bl)):
        out.append(f"【BOLL】上轨 {_s(bu)} / 中轨 {_s(bm)} / 下轨 {_s(bl)}")

    dif, dea, hist = (_num(last.get(k)) for k in ("macd_dif", "macd_dea", "macd_hist"))
    if any(v is not None for v in (dif, dea, hist)):
        state = "金叉（DIF>DEA）" if (dif is not None and dea is not None and dif > dea) else \
                "死叉" if (dif is not None and dea is not None) else ""
        out.append(f"【MACD】DIF {_s(dif, 3)} / DEA {_s(dea, 3)} / HIST {_s(hist, 3)}"
                   + (f"（{state}）" if state else ""))
    kk, dd, jj = (_num(last.get(k)) for k in ("kdj_k", "kdj_d", "kdj_j"))
    if any(v is not None for v in (kk, dd, jj)):
        out.append(f"【KDJ】K {_s(kk, 1)} / D {_s(dd, 1)} / J {_s(jj, 1)}")
    if _num(last.get("rsi6")) is not None or _num(last.get("rsi12")) is not None:
        out.append(f"【RSI】RSI6 {_s(last.get('rsi6'), 1)} / RSI12 {_s(last.get('rsi12'), 1)}")

    his = [(_num(r.get("high")), r.get("trade_date")) for r in win]
    los = [(_num(r.get("low")), r.get("trade_date")) for r in win]
    his = [x for x in his if x[0] is not None]
    los = [x for x in los if x[0] is not None]
    if his and los and close is not None:
        hi, hi_d = max(his, key=lambda x: x[0])
        lo, lo_d = min(los, key=lambda x: x[0])
        out.append(f"【区间位置】近 {len(win)} 根：最高 {_s(hi)}（{hi_d}）/ 最低 {_s(lo)}（{lo_d}）；"
                   f"收盘距最高 {(close / hi - 1) * 100:+.2f}%、距最低 {(close / lo - 1) * 100:+.2f}%")

    vols = [_num(r.get("vol")) for r in win[-6:-1]]
    vols = [v for v in vols if v is not None]
    v_now = _num(last.get("vol"))
    if v_now is not None and vols:
        avg = sum(vols) / len(vols)
        if avg:
            out.append(f"【量能】最新成交 {v_now / 1e4:,.2f} 万手，前 5 日均量 {avg / 1e4:,.2f} 万手"
                       f"（量能比 {v_now / avg:.2f}）")

    mf = [_num(r.get("mf_main_net")) for r in win]
    mf = [v for v in mf if v is not None]
    if mf:
        s5 = sum(mf[-5:])
        out.append(f"【主力资金】最新 {_wan(mf[-1])}元；近 5 个交易日合计 {_wan(s5)}元"
                   f"（口径：大单+特大单买入 − 卖出）")

    fund = (res.get("fundamental") or {}).get("items") or []
    if fund:
        out.append("【基本面】" + " · ".join(
            f"{i.get('label')} {i.get('text')}" for i in fund if i.get("text") not in (None, "—")))

    ch = res.get("chips") or {}
    holder = ch.get("holder") or {}
    if holder.get("available"):
        it = {i.get("key"): i.get("text") for i in (holder.get("items") or [])}
        out.append(f"【股东户数】{holder.get('hero', {}).get('text')}"
                   + (f"（截止 {it.get('end_date')}，环比 {it.get('growth')}）" if it.get("growth") else ""))
    margin = ch.get("margin") or {}
    if margin.get("available"):
        it = {i.get("key"): i.get("text") for i in (margin.get("items") or [])}
        out.append(f"【融资融券】融资余额 {margin.get('hero', {}).get('text')}"
                   f" · 融资净买入 {it.get('rz_net')} · 两融余额 {it.get('rzrqye')}"
                   f" · 占流通市值 {it.get('ratio')}")
    trade = ch.get("trade") or {}
    if trade.get("available"):
        sm = (trade.get("summary") or {}).get("items") or []
        cnt = " / ".join(f"{i.get('label')} {i.get('text')}" for i in sm) or (trade.get("badge") or "")
        out.append(f"【股东增减持】近段公告：{cnt}")
        for d in (trade.get("items") or [])[:3]:
            out.append("  · " + " ".join(
                str(d.get(k)) for k in ("ann_date", "holder_name", "dir_text",
                                        "change_vol_text", "change_ratio_text")
                if d.get(k) not in (None, ""))[:160])

    pool = res.get("pool") or {}
    if pool.get("where"):
        # ⚠ 只有**真有条件**时才报命中次数：`where` 为空 = 恒真 = 窗口内 100% 命中，
        #   写成「命中 60 次」会让模型以为这是个很强的信号（其实是没设条件）。
        hits = [r.get("trade_date") for r in win if r.get("entry")]
        out.append(f"【选股策略】按「{pool.get('label')}」逐日求值："
                   f"窗口内命中 {len(hits)} 次"
                   + (f"（最近 {hits[-1]}）" if hits else "（窗口内没有命中）"))
    elif pool.get("label"):
        out.append(f"【选股策略】{pool.get('label')}（本次没有选股条件，不判断池内命中）")
    return "\n".join(x for x in out if x)


def build_context(ts_code: str, trade_date: str | None = None, cfg: dict | None = None,
                  with_data: bool = False, name: str = "", industry: str = "",
                  close=None, pct_chg=None, strategy: str = "") -> dict:
    """拼出模板可用的上下文字典（`llm.TEMPLATE_VARS` 的 key 就是它的键）。

    `with_data=False` 时**不拉快照** —— 只有在模板里写了 `{本地数据}` 时才值得拉
    （要走一次选股口径，不是白拿的）。
    """
    code, nm, ind, _board = _resolve(ts_code)
    if not code:
        raise AIError("没给股票代码")
    ctx = {
        "code": code, "name": name or nm, "industry": industry or ind,
        "close": "", "pct_chg": "", "date": trade_date or "", "strategy": strategy or "",
        "conds": "", "data": "",
    }
    res = screener.screen_detail(code, cfg or {}, trade_date, bars=60, sel_only=True)
    if res.get("error"):
        if with_data:
            raise AIError(str(res["error"]))
        res = {}
    anchor = (res.get("anchor") or {}).get("date") or ""
    ctx["date"] = ctx["date"] or anchor
    ctx["code"] = res.get("ts_code") or code
    ctx["name"] = ctx["name"] or res.get("name") or ""
    ctx["industry"] = ctx["industry"] or res.get("industry") or ""
    ctx["strategy"] = ctx["strategy"] or ""
    if ctx["close"] in ("", None) or ctx["pct_chg"] in ("", None):
        rows = res.get("rows") or []
        ap = int(((res.get("anchor") or {}).get("index")) or 0)
        if rows:
            last = rows[min(ap, len(rows) - 1)]
            if ctx["close"] in ("", None):
                ctx["close"] = _s(last.get("close"))
            if ctx["pct_chg"] in ("", None):
                ctx["pct_chg"] = _s(last.get("pct_chg"), sign=True)
    pool = res.get("pool") or {}
    label = pool.get("label") or ""
    # 只有**真有条件**的池子才算「按它求值」；空条件池是恒真，写成条件会误导模型
    ctx["conds"] = (f"按选股策略「{label}」求值" if pool.get("where")
                    else (f"{label}（本次没有设选股条件）" if label else ""))
    if with_data and res:
        ctx["data"] = _block(res)
    return ctx


# ---------------------------------------------------------------------------
# 分析记录：发起 / 查询 / 历史
# ---------------------------------------------------------------------------

_REC_COLS = ("id, ts_code, name, trade_date, source, template_id, template_name, provider, "
             "model, status, elapsed_ms, tokens, error, created_at, ended_at")


def _rec_norm(row: dict | None) -> dict | None:
    if not row:
        return None
    row["trade_date"] = str(row.get("trade_date") or "")
    row["created_at"] = str(row.get("created_at") or "")
    row["ended_at"] = str(row.get("ended_at") or "")
    return row


def get_analysis(rec_id: int, with_content: bool = True) -> dict | None:
    cols = ("*" if with_content else _REC_COLS)
    r = _rec_norm(db.query_one(f"SELECT {cols} FROM ai_analysis WHERE id=%s", (rec_id,)))
    if r and with_content:
        r["content"] = r.get("content") or ""
        r["prompt"] = r.get("prompt") or ""
    return r


def list_analysis(ts_code: str | None = None, limit: int = 20) -> list[dict]:
    limit = max(1, min(int(limit or 20), 200))
    if ts_code:
        rows = db.query(
            f"SELECT {_REC_COLS}, LEFT(COALESCE(content,''), 160) AS preview "
            "FROM ai_analysis WHERE ts_code=%s ORDER BY id DESC LIMIT %s",
            (ts_code.strip().upper(), limit))
    else:
        rows = db.query(
            f"SELECT {_REC_COLS}, LEFT(COALESCE(content,''), 160) AS preview "
            "FROM ai_analysis ORDER BY id DESC LIMIT %s", (limit,))
    return [_rec_norm(r) for r in rows]


def delete_analysis(rec_id: int) -> bool:
    return bool(db.execute("DELETE FROM ai_analysis WHERE id=%s", (rec_id,)))


def _running_rec(ts_code: str) -> dict | None:
    return db.query_one(
        "SELECT id FROM ai_analysis WHERE ts_code=%s AND status='running' "
        "AND created_at > (NOW() - INTERVAL %s SECOND) ORDER BY id DESC LIMIT 1",
        (ts_code, RUNNING_REUSE_SEC))


def start_analysis(ts_code: str, name: str = "", industry: str = "", trade_date: str | None = None,
                   template_id: int | None = None, source: str = "stock",
                   cfg: dict | None = None, strategy: str = "",
                   close=None, pct_chg=None, profile_id: int | None = None) -> dict:
    """建记录 + 起后台线程，立刻返回记录行（前端拿 id 去轮询）。

    `profile_id`：这次用哪条模型接入（模型设置页配的多条之一）。**不传就是当前生效那条** ——
    弹窗里换个模型只影响这一次，不会把别人的默认改掉（改默认在模型设置页点「设为当前」）。
    """
    code, nm, ind, _b = _resolve(ts_code)
    if not code:
        raise AIError("没给股票代码")
    prof = llm.get_profile(int(profile_id)) if profile_id else llm.active_profile()
    if profile_id and not prof:
        raise AIError("选的那条模型配置不存在（可能刚被删，重开一下弹窗）")
    conf = llm.normalize_cfg(cfg or prof or llm.load_cfg())
    if not llm.is_ready(conf):
        who = (prof or {}).get("name") or "当前配置"
        raise AIError(f"「{who}」还没配好（模型设置：地址 + 模型名"
                      + ("，这个接入还要 API Key" if conf.get("key_required") else "") + "）")
    tpl = get_template(int(template_id)) if template_id else default_template()
    if not tpl:
        raise AIError("没有可用的 AI 语句模板（模型设置 → 语句模板）")

    busy = _running_rec(code)
    if busy:                      # 同一只票正在跑 → 复用，别叠请求（防连点刷爆本地模型）
        _log_ai.info("复用进行中的 AI 分析：ts_code=%s rec=%s", code, busy["id"])
        return get_analysis(int(busy["id"]), with_content=False) or {}

    rec_id = int(db.insert(
        "INSERT INTO ai_analysis (ts_code, name, trade_date, source, template_id, "
        "template_name, provider, model, status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'running')",
        (code, name or nm, trade_date or None, source or "stock", int(tpl["id"]),
         tpl["name"], conf["kind"], conf["model"])))
    args = dict(rec_id=rec_id, code=code, name=name, industry=industry,
                trade_date=trade_date, tpl=tpl, conf=conf, strategy=strategy,
                close=close, pct_chg=pct_chg)
    threading.Thread(target=_run, kwargs=args, daemon=True,
                     name=f"ai-{rec_id}").start()
    _log_ai.info("AI 分析已排队 rec=%s %s %s tpl=%s model=%s",
                 rec_id, code, trade_date or "-", tpl["name"], conf["model"])
    return get_analysis(rec_id, with_content=False) or {}


def _finish(rec_id: int, **kw) -> None:
    if not kw:
        return
    cols = ", ".join(f"{k}=%s" for k in kw)
    db.execute(f"UPDATE ai_analysis SET {cols} WHERE id=%s",
               tuple(kw.values()) + (rec_id,))


# ---------------------------------------------------------------------------
# 流式：发起 → 逐块推 → 收尾落库
# ---------------------------------------------------------------------------
# 与上面的「先落 running 再后台跑 + 前端轮询」是**同一件事的两种收法**：
#   落库位置、状态词、耗时/token 口径、模板与模型配置的来源全都共用；
#   区别只在「正文是等齐了一次性取，还是边生成边往外送」。
# ⚠ 流式那条**不能复用 running 记录** —— 复用就意味着这次不推流了（前端会一直空着）。

def _prompt_of(code: str, name: str, industry: str, trade_date: str | None,
               tpl: dict, strategy: str, close, pct_chg) -> str:
    """渲染出这一轮真正要发出去的提示词（流式与非流式共用，免得口径分家）。"""
    with_data = llm.wants_data(tpl["template"])
    ctx = build_context(code, trade_date, with_data=with_data, name=name,
                        industry=industry, close=close, pct_chg=pct_chg,
                        strategy=strategy)
    prompt, unknown = llm.render(tpl["template"], ctx)
    if unknown:
        _log_ai.warning("模板 %s 里有认不出的占位符：%s", tpl["name"], unknown)
    if len(prompt) > MAX_PROMPT_CHARS:
        raise AIError(f"提示词太长（{len(prompt)} 字），请精简模板或去掉 {{本地数据}}")
    return prompt


def _messages(conf: dict, prompt: str) -> list[dict]:
    msgs = []
    if conf.get("system_prompt"):
        msgs.append({"role": "system", "content": conf["system_prompt"]})
    msgs.append({"role": "user", "content": prompt})
    return msgs


def start_analysis_stream(ts_code: str, name: str = "", industry: str = "",
                          trade_date: str | None = None, template_id: int | None = None,
                          source: str = "stock", cfg: dict | None = None, strategy: str = "",
                          close=None, pct_chg=None, profile_id: int | None = None) -> tuple[dict, object]:
    """流式发起一次分析：**同步**做完所有前置校验，返回 `(记录行, 事件生成器)`。

    前置校验（代码 / 模型配置 / 模板）在这里做完并抛 `AIError` —— 这样接口还能老老实实
    回 400 而不是「先回 200 再在流里报错」（后者前端得先把卡片画出来再拆掉，很难看）。

    ⚠ 生成器**必须被消费**：客户端断开时 `GeneratorExit` 会把记录落成 error，
      不留半截 `running`（否则历史里会一直挂一条「进行中」）。
    """
    code, nm, ind, _b = _resolve(ts_code)
    if not code:
        raise AIError("没给股票代码")
    prof = llm.get_profile(int(profile_id)) if profile_id else llm.active_profile()
    if profile_id and not prof:
        raise AIError("选的那条模型配置不存在（可能刚被删，重开一下弹窗）")
    conf = llm.normalize_cfg(cfg or prof or llm.load_cfg())
    if not llm.is_ready(conf):
        who = (prof or {}).get("name") or "当前配置"
        raise AIError(f"「{who}」还没配好（模型设置：地址 + 模型名"
                      + ("，这个接入还要 API Key" if conf.get("key_required") else "") + "）")
    tpl = get_template(int(template_id)) if template_id else default_template()
    if not tpl:
        raise AIError("没有可用的 AI 语句模板（模型设置 → 语句模板）")

    # 同一只票还挂着 running 的旧记录 → 记成「被新的分析取代」。流式下不复用 running，
    # 所以不留着它（不然历史里会出现两条「进行中」，其中一条永远不会结束）。
    stale = db.execute(
        "UPDATE ai_analysis SET status='error', error='已被新的分析取代', ended_at=NOW() "
        "WHERE ts_code=%s AND status='running'", (code,))
    if stale:
        _log_ai.info("把 %d 条旧的 running 记录标为「被取代」：ts_code=%s", stale, code)

    rec_id = int(db.insert(
        "INSERT INTO ai_analysis (ts_code, name, trade_date, source, template_id, "
        "template_name, provider, model, status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'running')",
        (code, name or nm, trade_date or None, source or "stock", int(tpl["id"]),
         tpl["name"], conf["kind"], conf["model"])))
    _log_ai.info("AI 分析（流式）已开始 rec=%s %s %s tpl=%s model=%s",
                 rec_id, code, trade_date or "-", tpl["name"], conf["model"])
    rec = get_analysis(rec_id, with_content=False) or {}
    gen = _run_stream(rec_id=rec_id, code=code, name=name, industry=industry,
                      trade_date=trade_date, tpl=tpl, conf=conf, strategy=strategy,
                      close=close, pct_chg=pct_chg)
    return rec, gen


def _run_stream(rec_id: int, code: str, name: str, industry: str, trade_date: str | None,
                tpl: dict, conf: dict, strategy: str, close, pct_chg):
    """生成器：渲染 → 调模型（流式）→ 逐块 yield → 收尾落库。

    yield 的事件（前端只认这几种，别在页面里再解析一遍协议）：

        {"type": "start",  "rec": {...}}          记录已建（含 id）
        {"type": "think",  "text": "…"}           思考增量（推理型模型才有，只用来显示进度）
        {"type": "delta",  "text": "…"}           正文增量
        {"type": "done",   "rec": {...}, "from_think": false}   完成（正文已落库）
        {"type": "error",  "error": "人话"}       失败（错误原因已落库）

    ⚠ 正文**以落库的那一份为准**（`done` 事件里带回来）—— 前端自己拼的字符可能与
      我们落库的略有差别（例如首尾空白），显示口径必须只有一个。
    """
    import time
    t0 = time.perf_counter()
    yield {"type": "start", "rec": get_analysis(rec_id, with_content=False) or {}}
    try:
        prompt = _prompt_of(code, name, industry, trade_date, tpl, strategy, close, pct_chg)
        _finish(rec_id, prompt=prompt)
        done_ev = None
        for ev in llm.chat_stream(conf, _messages(conf, prompt)):
            if "delta" in ev:
                yield {"type": "delta", "text": ev["delta"]}
            elif "think" in ev:
                yield {"type": "think", "text": ev["think"]}
            elif ev.get("done"):
                done_ev = ev
        if not done_ev:
            raise AIError("模型没给出结束标志（流被截断了）")
        _finish(rec_id, status="done", content=done_ev["text"], model=done_ev["model"],
                provider=conf["kind"], tokens=done_ev.get("tokens"),
                elapsed_ms=done_ev["elapsed_ms"], ended_at=db_now())
        _log_ai.info("AI 分析（流式）完成 rec=%s %s %dms len=%d",
                     rec_id, done_ev["model"], done_ev["elapsed_ms"], len(done_ev["text"]))
        # `text` = **落库的那一份**（不是前端自己拼的）—— 前端拿到用它覆盖本地累积，
        # 显示口径就只有一处。`rec` 走 `_REC_COLS`（不含正文，列表口径不塞大字段）。
        yield {"type": "done", "from_think": bool(done_ev.get("from_think")),
               "text": done_ev["text"], "rec": get_analysis(rec_id, with_content=False) or {}}
    except GeneratorExit:
        # 前端关了页面 / 掐了连接：Flask 会 close() 这个生成器 —— 记录必须收口，
        # 否则历史里永远挂着一条「进行中」。⚠ 这里**不能再 yield**（已在关闭流程里）。
        _finish(rec_id, status="error", error="前端已断开，分析被中断",
                elapsed_ms=int((time.perf_counter() - t0) * 1000), ended_at=db_now())
        _log_ai.info("AI 分析（流式）被前端断开 rec=%s", rec_id)
        raise
    except Exception as e:  # noqa: BLE001  跑在流里，绝不能把异常吞成「一直转圈」
        msg = str(e) if isinstance(e, (AIError, llm.LLMError)) else f"{type(e).__name__}: {e}"
        _finish(rec_id, status="error", error=msg[:500],
                elapsed_ms=int((time.perf_counter() - t0) * 1000), ended_at=db_now())
        _log_ai.error("AI 分析（流式）失败 rec=%s %s\n%s", rec_id, msg,
                      traceback.format_exc()[-1200:])
        yield {"type": "error", "error": msg}


def _run(rec_id: int, code: str, name: str, industry: str, trade_date: str | None,
         tpl: dict, conf: dict, strategy: str, close, pct_chg) -> None:
    """后台线程：拉快照 → 渲染 → 调模型 → 回写。异常一律落进 `error` 列。"""
    import time
    t0 = time.perf_counter()
    try:
        prompt = _prompt_of(code, name, industry, trade_date, tpl, strategy, close, pct_chg)
        _finish(rec_id, prompt=prompt)
        res = llm.chat(conf, _messages(conf, prompt))
        _finish(rec_id, status="done", content=res["text"], model=res["model"],
                provider=conf["kind"], tokens=res.get("tokens"),
                elapsed_ms=res["elapsed_ms"], ended_at=db_now())
        _log_ai.info("AI 分析完成 rec=%s %s %dms", rec_id, res["model"], res["elapsed_ms"])
    except Exception as e:  # noqa: BLE001  跑在线程里，绝不能把异常吞成「一直转圈」
        msg = str(e) if isinstance(e, (AIError, llm.LLMError)) else f"{type(e).__name__}: {e}"
        _finish(rec_id, status="error", error=msg[:500],
                elapsed_ms=int((time.perf_counter() - t0) * 1000), ended_at=db_now())
        _log_ai.error("AI 分析失败 rec=%s %s\n%s", rec_id, msg,
                      traceback.format_exc()[-1200:])


def db_now() -> str:
    """用库的时间（不是本机 Python 的）—— 记录里的时间戳只认一个来源。"""
    return str(db.query_one("SELECT NOW() AS n")["n"])


def abort_orphan_runs() -> int:
    """启动时把「上个进程留下的 running」标成中断。

    跑分析的是内存里的线程，服务一重启（改 .py 触发 reloader 也算）线程就没了，
    记录却还挂着 running —— 页面会一直转圈等一个永远不来的结果。
    """
    n = db.execute(
        "UPDATE ai_analysis SET status='error', error='服务重启，分析被中断（重新点一次即可）', "
        "ended_at=NOW() WHERE status='running'")
    if n:
        _log_ai.info("清理了 %d 条被中断的 AI 分析记录", n)
    return int(n or 0)
