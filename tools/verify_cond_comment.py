#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""条件注释回归：`#` 之后（含 #）算注释，注释**不是**条件（2026-09-21 加）。

需求原话：「选股条件，#开头的不作为条件，只是条件注释，例如 total_mv > 300000  # 市值大于30亿，
这种#后面的都算作注释，不是条件表达式。如果行开头是#，则整行都为注释」。

为什么值得单独一条回归：注释**看起来**只是"多点一下删除键"的事，但它同时踩三个坑，每个都可能
静默出错（一声不响那种）：

  · **静默全灭**：注释行进了求值循环 → 空表达式 raise。`compute_pool` 遇 ExprError 按 False，
    表现是「全市场 0 命中」，看日志才有「条件池求值失败」；
  · **池子形同虚设**：`selection_cfg` / `has_entry_conds` 若只看 `cfg["entry"]` 非空，
    「只写了注释」会被当成"策略自带选股条件" → 选股策略的条件不抬进来，一声不响；
  · **注释里提一句字段就多一列**：选股结果表的列 = 条件引用到的字段；注释里写 `# 也可以看 w_close`
    不该把 w_close 拉进表里、更不该拉进 `only=` 裁剪（多算一份周线数据）。

口径只有一处：`core/expr.py` 的 `strip_comment()`（前端是 `window.condText`，同一条正则）。
**存库与回显保留注释原文**（那是用户写给自己的批注），只在「要执行」这一层剥。

分五段：

  【1】纯函数   —— strip_comment / split_conds / clean_conds / is_active / normalize；
                   纯注释表达式直接求值必须**明确报错**（不能退化成恒真/恒假）。
  【2】选股策略 —— `universe.parse` 剥注释（预筛翻译/label/取字段都吃干净的 where）；
                   只写注释 ⇒ 校验照实报「至少要写一条条件」。
  【3】买卖策略 —— 运行时口径剥、保存口径留（`save_strategy` → 库里/回显带注释）；
                   只有注释的 entry 不算"自带选股条件"，不会挡住条件池。
  【4】真函数喂合成数据 —— compute_signals / compute_pool / referenced_fields /
                   validate_config / screener.explain：结果与"根本没写那行"逐位相同。
  【5】浏览器实测 —— 同源 iframe 挂 /pools：condText 口径、只写注释时点试跑只弹提示不发请求、
                   提交时注释原文照旧带上。

依赖：服务在 127.0.0.1:8777（【2】的接口段与【5】）；本机 Chrome（找不到就把【5】标 SKIP）。
【2】会在库里建一个临时选股策略并在结束时删掉。
用法：python tools/verify_cond_comment.py [--no-e2e]
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BASE = "http://127.0.0.1:8777"
PROBE = "/static/_probe_cond_comment.html"
EXPR_PY = os.path.join(ROOT, "core", "expr.py")
UNIVERSE_PY = os.path.join(ROOT, "core", "universe.py")
STRATEGY_PY = os.path.join(ROOT, "core", "strategy.py")
APP_PY = os.path.join(ROOT, "app.py")
APPJS = os.path.join(ROOT, "static", "js", "app.js")
ACJS = os.path.join(ROOT, "static", "js", "ac.js")

NODE = r"C:\Users\ming\.workbuddy\binaries\node\versions\22.22.2-3\node.exe"

PASS = FAIL = SKIP = 0
FAILS: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  OK   {name}" + (f"  {extra}" if extra else ""))
    else:
        FAIL += 1
        FAILS.append(name)
        print(f"  FAIL {name}" + (f"  {extra}" if extra else ""))


def skip(name: str, why: str) -> None:
    global SKIP
    SKIP += 1
    print(f"  SKIP {name}  {why}")


def req(path: str, payload=None, method: str = "GET", timeout: int = 60):
    """返回 (status, json)。"""
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "replace") or "{}")


CHROME = None
for _c in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
           r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
           os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe")):
    if os.path.exists(_c):
        CHROME = _c
        break


def chrome_probe_out(url: str, budget_ms: int, timeout: int, profile: str) -> str:
    """渲染探针页并返回 <pre id="out"> 里的文本。"""
    dom_file = os.path.join(tempfile.gettempdir(), "_cc_probe_dom.html")
    try:
        with open(dom_file, "w", encoding="utf-8") as fh:
            subprocess.run(
                [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
                 "--user-data-dir=" + profile, "--window-size=1440,1200",
                 "--virtual-time-budget=%d" % budget_ms, "--dump-dom", url],
                stdout=fh, stderr=subprocess.DEVNULL, timeout=timeout)
        dom = open(dom_file, encoding="utf-8", errors="ignore").read()
    finally:
        try:
            os.remove(dom_file)
        except OSError:
            pass
    m = re.search(r'<pre id="out">([\s\S]*?)</pre>', dom)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
def sec1_pure() -> None:
    print("【1】纯函数（剥注释的唯一口径）")
    from core import expr

    check("行内注释被剥掉、两边空白收干净",
          expr.strip_comment("total_mv > 300000  # 市值大于30亿") == "total_mv > 300000",
          expr.strip_comment("total_mv > 300000  # 市值大于30亿"))
    check("行首 # ⇒ 整行都是注释（返回空串）",
          expr.strip_comment("   # 下面是小市值筛选") == "")
    check("# 在中间也照剥（注释不是「行尾才生效」）",
          expr.strip_comment("vol > vma5 * 1.5 # 放量  # 再补一句") == "vol > vma5 * 1.5",
          expr.strip_comment("vol > vma5 * 1.5 # 放量  # 再补一句"))
    check("没有 # 时原样返回（不动表达式一个字）",
          expr.strip_comment("close <= boll_lower * 1.02") == "close <= boll_lower * 1.02")
    check("None / '' 不炸", expr.strip_comment(None) == "" and expr.strip_comment("") == "")
    check("全角 ＃ 也认（中文输入法全角标点打出来的是它）",
          expr.strip_comment("total_mv > 300000　＃ 市值大于30亿") == "total_mv > 300000"
          and expr.is_active("＃ 只有注释") is False,
          expr.strip_comment("total_mv > 300000　＃ 市值大于30亿"))

    txt = "total_mv > 300000  # 市值大于30亿\n# 纯注释行\n\nturnover_rate > 5；；vol > 0;"
    got = expr.split_conds(txt)
    check("split_conds：换行 / 中英文分号都分隔、空行与纯注释行丢弃、行内注释剥掉",
          got == ["total_mv > 300000", "turnover_rate > 5", "vol > 0"], str(got))
    check("split_conds 传列表 == clean_conds",
          expr.split_conds(["a > 1 # c", "# only", "  "]) == ["a > 1"])
    check("clean_conds 传字符串时走 split_conds（不会把字符串拆成一个个字符）",
          expr.clean_conds("# 注释\nb > 2") == ["b > 2"],
          str(expr.clean_conds("# 注释\nb > 2")))
    check("is_active：空 / 纯注释 = False，带注释的真条件 = True",
          expr.is_active("# 注释") is False and expr.is_active("   ") is False
          and expr.is_active("close > 0  # 注释") is True)

    check("normalize 也剥注释（单条表达式字段如 score_expr 走这条）",
          expr.normalize("close > open  # 阳线") == "close > open",
          expr.normalize("close > open  # 阳线"))
    check("normalize 的 = 归一化仍在（没被剥注释挤掉）",
          expr.normalize("is_down = 1  # 阴线") == "is_down == 1",
          expr.normalize("is_down = 1  # 阴线"))

    # 纯注释表达式：必须**明确报错**，绝不能静默当成恒真/恒假
    try:
        expr.evaluate("# 只是注释", {"n": 1}, 1)
        check("纯注释表达式求值要报错（不许静默成功）", False, "居然没报错")
    except expr.ExprError as e:
        check("纯注释表达式求值要报错（不许静默成功）", True, str(e)[:60])
    ok, msg = expr.check_expr("# 只是注释")
    check("check_expr 对纯注释返回「不合法」（所以调用方必须先剥/跳过）",
          ok is False, msg)
    check("check_expr 对「真条件 + 行内注释」是合法的（剥完就干净）",
          expr.check_expr("close > 0  # 阳线")[0] is True)


def sec2_universe() -> None:
    print("【2】选股策略（池子）路径")
    from core import universe as uni

    s = uni.parse({"base": "all", "where": [
        "total_mv > 300000  # 市值大于30亿",
        "# 下面这条是换手",
        "turnover_rate > 5",
        "",
    ]})
    check("parse 把 where 洗成纯表达式（注释行不在里面）",
          s["where"] == ["total_mv > 300000", "turnover_rate > 5"], str(s["where"]))
    check("parse 传字符串形态也洗（前端直接给多行文本的场景）",
          uni.parse({"base": "all", "where": "close > 0 # 阳线\n# 注释"})["where"] == ["close > 0"])
    check("label 用的是洗干净的条件（不会把注释当第一条条件念出来）",
          "#" not in uni.label({"base": "all", "where": ["# 注释", "close > 0"]}),
          uni.label({"base": "all", "where": ["# 注释", "close > 0"]}))

    errs = uni.validate({"base": "all", "where": ["# 只有注释", "   "]}, set())
    check("只写注释 ⇒ 校验照实报「至少要写一条条件」（不是静默通过）",
          any("至少要写一条条件" in e for e in errs), str(errs))
    errs2 = uni.validate({"base": "all", "where": ["total_mv > 300000  # 市值大于30亿"]},
                         {"total_mv"})
    check("注释里的字不会被当成未知字段", errs2 == [], str(errs2))

    # 只写注释的池子：掩码必须全 True（= 没有池子条件），绝不能全灭
    from core import strategy as st
    df = _synth_df()
    m = st.compute_pool(df, {"_pool": {"base": "all", "where": ["# 只有注释"]}})
    check("条件全是注释 ⇒ compute_pool 全 True（退化成纯静态池，不是 0 命中）",
          bool(m.all()) and len(m) == len(df), f"True {int(m.sum())}/{len(m)}")

    # 存 / 读往返：库里与回显**保留注释原文**
    pid = None
    try:
        conds = ["total_mv > 300000  # 市值大于30亿", "# 换手确认", "turnover_rate > 5"]
        code, d = req("/api/pools", {"name": "自检临时-条件注释", "base": "all",
                                     "conds": conds,
                                     "filt": {"exclude_st": True, "exclude_kcb": False,
                                              "exclude_cyb": False, "exclude_bj": True,
                                              "min_amount": 0, "min_list_days": 60}},
                      method="POST")
        pid = (d.get("data") or {}).get("id") if code == 200 else None
        if not pid:
            skip("选股策略存读往返（注释原文保留）", f"接口返回 {code} {str(d)[:120]}")
        else:
            code2, d2 = req(f"/api/pools/{pid}")
            got = ((d2.get("data") or {}).get("conds")) or []
            check("存进去的注释原样回显（用户写的批注不能存一次就没了）",
                  got == conds, str(got))
            check("回显里注释行还在、位置也没乱（第 2 条就是纯注释行）",
                  len(got) == 3 and got[1].startswith("#"), str(got))
            check("列表/编辑器上的「N 条条件」按**真条件**算（3 条里 1 条是注释 ⇒ 2 条）",
                  (d2.get("data") or {}).get("cond_count") == 2,
                  f"cond_count={(d2.get('data') or {}).get('cond_count')} / conds={len(got)}")
            check("label（同一批条件、不引用 id 时）条数也按真条件算 —— 与 cond_count 同口径",
                  "条件池(2条)" in uni.label({"base": "all", "where": conds}),
                  uni.label({"base": "all", "where": conds}))
            code3, d3 = req("/api/pools/validate",
                            {"name": "x", "base": "all", "conds": ["# 只有注释"]},
                            method="POST")
            errs3 = ((d3.get("data") or {}).get("errors")) or []
            check("接口校验：只写注释 = 不通过，且文案说清是「没条件」",
                  code3 == 200 and any("至少要写一条条件" in e for e in errs3), str(errs3))
            code4, d4 = req("/api/pools/validate",
                            {"name": "x", "base": "all",
                             "conds": ["total_mv > 300000  # 市值大于30亿"]}, method="POST")
            check("接口校验：带行内注释的真条件是合法的",
                  ((d4.get("data") or {}).get("valid")) is True, str(d4.get("data")))
    finally:
        if pid:
            req(f"/api/pools/{pid}", method="DELETE")


def sec3_strategy() -> None:
    print("【3】买卖策略路径（运行时剥 / 保存留）")
    from core import strategy as st

    raw = {"buy_trigger": ["open > ref(close, 1)  # 次日高开"],
           "sell_signal": ["# 只写了注释"], "entry": ["# 只有注释"]}
    run = st.merge_config(raw)
    check("merge_config 默认（运行时口径）剥掉行内注释",
          run["buy_trigger"] == ["open > ref(close, 1)"], str(run["buy_trigger"]))
    check("merge_config 默认丢掉纯注释行",
          run["sell_signal"] == [] and run["entry"] == [],
          f"sell={run['sell_signal']} entry={run['entry']}")
    kept = st.merge_config(raw, keep_comments=True)
    check("keep_comments=True（保存口径）原样留着注释",
          kept["buy_trigger"] == ["open > ref(close, 1)  # 次日高开"]
          and kept["sell_signal"] == ["# 只写了注释"],
          str(kept["buy_trigger"]))
    check("保存口径下为空/空白行照样丢",
          st.merge_config({"entry": ["", "   ", "close > 0"]}, keep_comments=True)["entry"]
          == ["close > 0"])
    back = st.merge_config(json.loads(json.dumps(kept)))
    check("存了再读 → 运行时口径又变干净（闭环）",
          back["buy_trigger"] == ["open > ref(close, 1)"], str(back["buy_trigger"]))

    check("has_entry_conds：只写注释 = 没写选股条件", st.has_entry_conds(raw) is False)
    check("has_entry_conds：真条件（哪怕带注释）= 写了",
          st.has_entry_conds({"entry": ["close > 0  # 阳线"]}) is True)
    check("has_entry_conds：只看注释时的 _has_selection 也不认（不会漏铺命中点）",
          st._has_selection({"_pool": {"base": "all", "where": ["# 注释"]}}) is False)

    # 只写注释的 entry 不能挡住「选股策略条件抬进 entry」
    cfg = st.selection_cfg({"entry": ["# 只有注释"],
                            "_pool": {"base": "all", "where": ["total_mv > 300000  # 小市值"]}})
    check("只写注释的 entry 不挡条件池：选股策略的条件照样抬进 entry",
          cfg["entry"] == ["total_mv > 300000"], str(cfg["entry"]))

    errs = st.validate_config(raw)
    check("validate_config 不把注释行报成语法错误", errs == [], str(errs))
    errs2 = st.validate_config({"weekly_filter": ["# 只是说明", "w_close > w_boll_mid"]})
    check("周线过滤的 w_ 前缀检查跳过注释行（注释里没 w_ 也不报错）",
          not any("w_ 前缀" in e for e in errs2), str(errs2))
    errs3 = st.validate_config({"score_expr": "# 只是注释"})
    check("评分表达式是纯注释 ⇒ 不报错（等于没填）", errs3 == [], str(errs3))

    # 真落库一次再删（save_strategy 必须用 keep_comments=True 才对得起用户写的批注）
    sid = None
    try:
        sid = st.save_strategy("自检临时-条件注释", "自检用",
                               {"buy_trigger": ["open > ref(close, 1)  # 次日高开"]})
        row = st.get_strategy(sid)
        bt = ((row or {}).get("config") or {}).get("buy_trigger") or []
        check("save_strategy → 库里存的 buy_trigger 带注释原文",
              bt == ["open > ref(close, 1)  # 次日高开"], str(bt))
        check("get_strategy 读回来仍是运行时干净口径（compute_signals 用的那份）",
              st.merge_config((row or {}).get("config"))["buy_trigger"]
              == ["open > ref(close, 1)"])
    finally:
        if sid:
            st.delete_strategy(sid)
            check("自检临时策略已删除（不留垃圾）", st.get_strategy(sid) is None)


def _synth_df() -> pd.DataFrame:
    """合成 12 根 K 线（真函数 + 合成数据，别依赖"库里某天恰好有票"）。"""
    n = 12
    close = np.array([10, 10.2, 10.5, 10.1, 10.4, 11.0, 11.2, 10.9, 11.5, 11.8, 11.6, 12.0])
    return pd.DataFrame({
        "trade_date": [f"2026-06-{i+1:02d}" for i in range(n)],
        "open": close * 0.99, "high": close * 1.02, "low": close * 0.97,
        "close": close, "pre_close": np.r_[close[0], close[:-1]],
        "vol": np.linspace(1e5, 3e5, n), "amount": np.linspace(1e6, 3e6, n),
        "pct_chg": np.r_[0.0, np.diff(close) / close[:-1] * 100],
    })


def sec4_real_funcs() -> None:
    print("【4】真函数喂合成数据（结果必须与「根本没写那行」逐位相同）")
    from core import screener, strategy as st

    df = _synth_df()

    a = st.compute_signals(df, {"buy_trigger": ["close > open"]})
    b = st.compute_signals(df, {"buy_trigger": ["close > open      # 阳线才买"]})
    check("compute_signals：条件带行内注释，结果与不带逐根相同",
          np.array_equal(a["buy_trigger"].to_numpy(), b["buy_trigger"].to_numpy()),
          f"True {int(a['buy_trigger'].sum())} vs {int(b['buy_trigger'].sum())}")

    c = st.compute_signals(df, {"buy_trigger": ["close > open", "# 注释行", "vol > 0"]})
    d = st.compute_signals(df, {"buy_trigger": ["close > open", "vol > 0"]})
    check("compute_signals：夹在中间的纯注释行不影响结果",
          np.array_equal(c["buy_trigger"].to_numpy(), d["buy_trigger"].to_numpy()))

    only_cmt = st.compute_signals(df, {"buy_trigger": ["# 只有注释"], "sell_signal": ["# 只有注释"]})
    empty = st.compute_signals(df, {})
    check("只有注释的 buy_trigger == 留空（全 True：次日无条件买入）",
          bool(only_cmt["buy_trigger"].all()) and bool(empty["buy_trigger"].all()))
    check("只有注释的 sell_signal == 留空（全 False：不靠信号离场）",
          not bool(only_cmt["sell_signal"].any()) and not bool(empty["sell_signal"].any()))

    ent = st.compute_signals(df, {"entry": ["close > open"]})
    ent2 = st.compute_signals(df, {"entry": ["# 注释", "close > open"]})
    check("只有注释时不改 entry 语义（空 entry = 全 True，不额外筛）",
          bool(st.compute_signals(df, {"entry": ["# 注释"]})["entry"].all())
          and np.array_equal(ent["entry"].to_numpy(), ent2["entry"].to_numpy()))

    # 选股结果表的列：注释里提到的字段不许进表、不许进 only= 裁剪
    refs = st.referenced_fields({"entry": ["close > 0  # 也可以看 w_close 和 boll_upper"]})
    check("referenced_fields 不把注释里提到的字段算进来（否则表会莫名多列）",
          refs == ["close"], str(refs))
    df_daily = st.required_daily_fields({"entry": ["close > 0  # 看 w_close"]})
    check("required_daily_fields 同理（注释里写 w_ 字段不会去多算一份周线）",
          "w_close" not in df_daily, str(sorted(df_daily)))
    check("required_weekly_fields 拿到的也干净",
          "w_close" not in (st.required_weekly_fields({"entry": ["close > 0  # 看 w_close"]}) or set()))

    # 命中明细：注释行不该变成一条「报错的条件」
    ex = screener.explain({"entry": ["# 只是注释", "close > open"]}, df, len(df) - 1)
    with_err = [c for c in ex["conditions"] if c.get("error")]
    check("命中明细里没有注释行留下的报错条目", with_err == [],
          str([c.get("expr") for c in with_err]))
    check("命中明细里只剩 1 条真条件",
          len(ex["conditions"]) == 1 and ex["conditions"][0]["expr"] == "close > open",
          str([c.get("expr") for c in ex["conditions"]]))

    # 周线视图（诊断页切周K）：前缀替换不许把注释里的字也改掉后当成条件
    wk = st.to_weekly_view({"entry": ["w_close > w_boll_mid  # 周线确认"]})
    check("切周线视图后 entry 仍是干净的表达式（注释已剥）",
          wk["entry"] == ["close > boll_mid"], str(wk["entry"]))


def sec5_static_and_probe(no_e2e: bool) -> None:
    print("【5】前端口径 + 浏览器实测")
    appjs = open(APPJS, encoding="utf-8").read()
    check("app.js 有 condText 口径，且与后端同一个正则 (#[^\\n]*)",
          "window.condText" in appjs and r"replace(/#[^\n]*/g" in appjs)
    check("condText 也认全角 ＃（与 expr.strip_comment 同口径）",
          r"replace(/＃/g, '#')" in appjs)
    check("condText 声明了 strip / active / count 三件",
          all(k in appjs for k in ("strip:", "active:", "count:")))
    check("condText 的命名避开了页面里的 const COND（同名会直接 SyntaxError）",
          re.search(r"window\.COND\s*=", appjs) is None
          and re.search(r"^\s*(?:const|let|var)\s+COND\b", appjs, re.M) is None)

    pool_html = open(os.path.join(ROOT, "templates", "pool.html"), encoding="utf-8").read()
    check("试跑前的空条件提示走 activeConds()（只写注释也拦得住）",
          "if (!activeConds().length)" in pool_html and "function activeConds()" in pool_html)
    check("页面明确写了「只写 # 注释不算」的文案", "只写 # 注释不算" in pool_html)
    check("入池条件输入框的提示里写了 # 注释（用户得知道有这个功能）",
          "起注释" in pool_html and "#" in pool_html)
    st_html = open(os.path.join(ROOT, "templates", "strategy.html"), encoding="utf-8").read()
    check("买卖策略页也提示了 # 是注释", "之后是注释" in st_html)

    acjs = open(ACJS, encoding="utf-8").read()
    check("ac.js：光标在注释里时不弹字段提示", "inComment" in acjs
          and "if (inComment(el.value, el.selectionStart))" in acjs)
    if os.path.exists(NODE):
        r = subprocess.run(
            [NODE, "-e",
             "var A=require(process.argv[1]);"
             "var o=[];"
             "o.push(A.inComment('a>1  # 注', 9));"          # 光标在注释里 → true
             "o.push(A.inComment('a>1  # 注', 3));"          # 光标在 # 之前 → false
             "o.push(A.inComment('a>1\\n# 注', 8));"         # 第二行的注释 → true（逐行判）
             "o.push(A.inComment('a>1\\nclose>0', 11));"     # 第二行没注释 → false
             "o.push(A.inComment('a>1  ＃ 注', 9));"         # 全角 ＃ 也认 → true
             "console.log(JSON.stringify(o));", ACJS],
            capture_output=True, text=True, timeout=30)
        try:
            got = json.loads((r.stdout or "").strip().splitlines()[-1])
        except Exception:  # noqa: BLE001
            got = None
        check("ac.js 的 inComment 逐行判（前一行有 # 不影响下一行）+ 认全角 ＃",
              got == [True, False, True, False, True], f"{got} {r.stderr[:80]}")
    else:
        skip("ac.js inComment 单测", "找不到 node")

    if no_e2e:
        skip("浏览器实测（探针）", "--no-e2e")
        return
    if not CHROME:
        skip("浏览器实测（探针）", "本机找不到 Chrome")
        return
    profile = os.path.join(tempfile.gettempdir(), "_cc_probe_profile")
    txt = chrome_probe_out(BASE + PROBE + "?run=1", 12000, 120, profile)
    try:
        data = json.loads(txt)
    except Exception:  # noqa: BLE001
        check("探针跑完并给出 JSON", False, txt[:200] or "(空)")
        return
    check("探针跑完并给出 JSON", data.get("done") is True)
    for c in data.get("checks") or []:
        check("浏览器：" + c["name"], bool(c.get("ok")), c.get("extra", ""))


# ---------------------------------------------------------------------------
def main() -> int:
    no_e2e = "--no-e2e" in sys.argv
    print("=" * 78)
    print("条件注释回归：`#` 之后算注释，注释不是条件")
    print("=" * 78)
    sec1_pure()
    sec2_universe()
    sec3_strategy()
    sec4_real_funcs()
    sec5_static_and_probe(no_e2e)
    print("-" * 78)
    print(f"{PASS} 通过 / {FAIL} 失败 / {SKIP} 跳过")
    if FAILS:
        print("失败项：")
        for n in FAILS:
            print("  · " + n)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
