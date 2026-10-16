#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""选股结果表回归（列 = 条件引用到的字段 / 表头与格式来自后端 / 涨跌色 / CSV）。

为什么需要它：结果表的列以前是前端写死的一份清单（只带 5 个筹码列），
后来改成「按策略条件/评分里真正引用到的字段自动加列」—— 列头、小数位、
数值格式、要不要上色全部由后端 `indicators.col_spec` 给。这类改动读文本读不出来：

  · 表头是不是真的叫「股东户数」而不是 `hold_num`（后端 col_spec 有没有覆盖到）；
  · 万元金额是显示 `66702.92` 还是 `6.67万`（fmt 走没走对）；
  · 涨跌色还在不在（新加的列最容易漏 `updown`，或把"水平量"也涂上色）；
  · CSV 导出的列头与屏幕上是不是同一份（以前导出另抄了一遍）。

所以分三段：

  【1】静态口径   —— 页面里不许再出现写死的列清单/字段名，列必须走 `r.extra_cols`，
                      CSV 走同一个 `csvLines()`；「K线」弹窗只走 app.js 的 klinePreview
                      唯一入口、行模板带着行号（弹窗「下一个」按表面顺序翻）、
                      历史结果逐行用自己的 trade_date；表头点排序只重画这张表
                      （`scTh`/`paintResult`，不重新选股）；内联脚本语法过不过。
  【2】接口契约   —— `/api/screen/run` 对「用筹码条件的策略」与「纯技术策略」
                      分别给出正确的 `extra_cols`（键/顺序/表头/格式），
                      且每只命中都带上了这些键的值。
  【2b】排序表达式 —— 选股页现填的 `sort_expr` 真的决定顺序（降序 / 取反成升序）、
                      它引用的字段自动进结果表、「评分」列就是它的值；
                      式子写错要当场 400，不能静默按空评分跑出一屏乱序结果。
  【3】浏览器实测 —— 同源 iframe 挂真页面 → 喂构造结果调真 `renderResult` →
                      读真 DOM（表头顺序、格式化文本、缺值破折号、涨红跌绿、
                      明细弹层用中文表头、CSV 列头）；再**真点一次「K线」**验弹窗、
                      **真点一次「下一个」**验它换到表里下一只（弹窗没被重建、
                      到末位按钮变灰）；**真点表头**验排序（数值降/升序、缺值沉底、
                      行内按钮跟着换行、「#」回服务端原序）。

依赖：服务在 127.0.0.1:8777；本机 Chrome（找不到就把【3】标 SKIP，不算失败）。
用法：python tools/verify_screen_ui.py [--no-e2e]
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BASE = "http://127.0.0.1:8777"
SCREEN_HTML = os.path.join(ROOT, "templates", "screen.html")
PROBE = "/static/_probe_screen.html"
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


def get_json(path: str, timeout: int = 300) -> dict:
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode("utf-8", "replace"))


def post_json(path: str, payload: dict, timeout: int = 600) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode("utf-8", "replace"))


def delete(path: str) -> None:
    req = urllib.request.Request(BASE + path, method="DELETE")
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except urllib.error.HTTPError:
        pass


CHROME = None
for _c in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
           r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
           os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe")):
    if os.path.exists(_c):
        CHROME = _c
        break


def chrome_probe_out(url: str, budget_ms: int, timeout: int, profile: str) -> str:
    """渲染探针页并返回 <pre id="out"> 里的文本。"""
    out = os.path.join(tempfile.gettempdir(), "_screen_probe_dom.html")
    try:
        with open(out, "w", encoding="utf-8") as fh:
            subprocess.run(
                [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
                 "--user-data-dir=" + profile, "--window-size=1500,1000",
                 "--virtual-time-budget=%d" % budget_ms, "--dump-dom", url],
                stdout=fh, stderr=subprocess.DEVNULL, timeout=timeout)
        dom = open(out, encoding="utf-8", errors="ignore").read()
    finally:
        try:
            os.remove(out)
        except OSError:
            pass
    m = re.search(r'<pre id="out">([\s\S]*?)</pre>', dom)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
def sec1_static() -> None:
    print("【1】静态口径（列不写死、CSV 与屏幕同源）")
    html = open(SCREEN_HTML, encoding="utf-8").read()
    # 去掉注释再查，免得把说明文字当成代码
    code = re.sub(r"/\*[\s\S]*?\*/", "", html)
    check("页面里没有写死的列清单（CHIP_COLS 那种）",
          "CHIP_COLS" not in code and "chipHead" not in code)
    app_js = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
    check("结果表列走 extra_cols（后端算好表头/格式）",
          "extraCols(r)" in code and "extraCell(it)" in code and "r.extra_cols" in app_js)
    check("表头用后端给的 label、字段名只进 title（可回填条件框）",
          "function scTh(" in code and "esc(label)" in code
          and "scTh(c.label, c.key, 'num', c.key)" in code)
    check("数值格式按后端 fmt 分派（num/big/mv/pct/price/int）—— 实现只有 app.js 一份",
          all(f"case '{f}'" in app_js for f in ("big", "mv", "pct", "price", "int"))
          and "window.fmt.num(v, c.dec)" in app_js
          and "function colText" not in code and "window.colText" in app_js)
    # 页面里不该再出现任何筹码/两融字段名的字面量（列名只有 chips.py 一个来源）
    lit = re.findall(r"'(hold_\w+|rz_\w+|rq_\w+|ht_\w+)'", code)
    check("页面里不出现筹码字段名字面量（列名唯一来源在 core/chips.py）",
          not lit, f"出现 {sorted(set(lit))}")
    check("CSV 导出复用同一个 csvLines()（列头与屏幕同一份）",
          code.count("csvLines") >= 2 and "cols.map(c => c.label)" in code)

    # 「K线」按钮：不跳页，弹窗里嵌诊断页的预览模式（图表的实现只有那一份）
    check("结果表的「K线」是按钮、不再跳页（<a target=_blank> 已去掉）",
          re.search(r'<button class="btn sm" data-kline="\$\{esc\(it\.ts_code\)\}"', code) is not None
          and re.search(r'<a[^>]{0,90}>\s*K线\s*</a>', code) is None)
    check("已保存结果里的「K线」同样是按钮",
          re.search(r'<button class="btn sm" data-kline="\$\{esc\(r\.ts_code\)\}"', code) is not None)
    check("弹窗预览走 app.js 的唯一入口 klinePreview（页面不自己拼 iframe）",
          "window.klinePreview({" in code and "<iframe" not in code)
    check("整页链接与弹窗参数共用 viewParams()（口径只写一份、不各拼一套）",
          "function viewParams(tradeDate)" in code
          and "new URLSearchParams(viewParams(tradeDate)).toString()" in code
          and "params: viewParams(tradeDate)" in code
          and code.count("viewParams(") >= 3)
    check("预览带上基准日与当前选股口径（不带就会停在最新那天、命中点口径也不对）",
          "if (tradeDate) p.trade_date = tradeDate;" in code)
    # 弹窗右上角的「下一个」（核心实现在 app.js）：名单必须是**当页这张表**，
    # 所以行模板要带行号，点按钮时把整张表 + 行号一起交给入口。
    check("结果表每行带着行号（「下一个」= 表里的下一行）",
          'data-kline="${esc(it.ts_code)}" data-idx="${i}"' in code
          and "openKline(b.dataset.kline, it.name || '', r.trade_date, view, i)" in code)
    check("已保存结果的行也带行号（那份列表同样能一只只往下看）",
          'data-kline="${esc(r.ts_code)}" data-idx="${j}"' in code
          and "rows, Number(b.dataset.idx)" in code)
    check("历史结果翻页逐行用自己的 trade_date（paramsOf，别串成同一份口径）",
          "it => viewParams(it.trade_date)" in code)
    check("「K线」只绑 #sc-result 里的按钮（别把历史列表那份的基准日改写成今天）",
          "$$('[data-kline]', $('#sc-result'))" in code)

    # 「排序表达式」：选股这一步就能定顺序（不必先挂买卖策略、再去策略页填评分）
    check("选股页有「排序表达式」输入框（结果表按它排）",
          'id="sc-sort"' in code and "排序表达式" in code)
    check("排序表达式随请求发出（留空 = 后端沿用买卖策略的评分表达式）",
          "const sortExpr = $('#sc-sort').value.trim();" in code
          and "if (sortExpr) body.sort_expr = sortExpr;" in code)
    check("默认跟随买卖策略的评分表达式，且用户手改过就不再被覆盖",
          "function syncSortFromStrategy()" in code and "if (sortTouched) return;" in code
          and "$('#sc-sort').addEventListener('input', () => { sortTouched = true; })" in code)
    check("排序字段名也能自动补全（与策略页的评分框共用同一份 ac.js）",
          "window.ExprAC.attach($('#sc-sort'), { weekly: false })" in code)
    check("结果卡写明「按什么排的」（别让人对着顺序猜）",
          "r.sort_expr ? ' · 按 ' + r.sort_expr + ' 降序'" in code)

    # 点表头排序：只对**已返回的这批**排（不重新跑选股），复用「交易明细」那套 CSS/交互
    check("结果表表头可点排序（复用「交易明细」那套 data-sort / .sarrow）",
          "function scTh(" in code and 'data-sort="${key}"' in code)
    check("点表头只重画这张表、不重新选股（对已返回的这批排序）",
          "function paintResult()" in code and "th.dataset.sort" in code
          and "paintResult();" in code)
    check("排序口径：文本列按文本比、数值列按数值比",
          "SC_TEXT_COLS = { ts_code: 1, name: 1, industry: 1 }" in code
          and "localeCompare(String(vb), 'zh')" in code)
    check("缺值永远沉底（不随升降序翻上来）",
          "if (na || nb) return (na && nb) ? a.i - b.i : (na ? 1 : -1);" in code)
    check("「#」列可点 = 恢复服务端排序（点过别的列后有回路）",
          "'__orig'" in code and "点击恢复服务端排序" in code)
    check("导出的 CSV 跟屏幕当前顺序一致（排过序就按排序后的导）",
          "scSortedItems().map(it=>[" in code)

    js = "\n".join(re.findall(r"<script>([\s\S]*?)</script>", html))
    js = re.sub(r"\{\{[\s\S]*?\}\}|\{%[\s\S]*?%\}", "0", js)
    jsfile = os.path.join(tempfile.gettempdir(), "_screen_inline.js")
    with open(jsfile, "w", encoding="utf-8") as fh:
        fh.write(js)
    if os.path.exists(NODE):
        r = subprocess.run([NODE, "--check", jsfile], capture_output=True, text=True)
        check("内联脚本语法检查（node --check）", r.returncode == 0, r.stderr.strip()[:120])
    else:
        skip("内联脚本语法检查", "没找到 node")


def sec2_api() -> None:
    print("【2】接口契约（extra_cols = 条件引用到的字段）")
    cfg_js = get_json("/api/config")
    today = cfg_js.get("data", {}).get("latest_trade_date")
    if not today:
        skip("接口契约", "还没有行情数据")
        return
    from core import chips as ch  # noqa: PLC0415

    # 策略：筹码 + 技术混着写，顺带覆盖 score_expr 与条件池。
    # ⚠ 这里验的是「引用了哪些字段 / 顺序」，**不是**行情择时 —— 条件必须写得几乎恒真，
    #   否则某天各子条件恰好凑不出交集，`count=0` → `extra_cols` 变空 → 假红
    #   （2026-09-18 就踩过：`hold_chg<-5` 58 只 / `close>ma20` 86 只 / `rz_net5>0` 120 只，
    #    四者交集 0）。要验「真能筛出票」请另用有明确语义的条件，别混在这一段。
    made = post_json("/api/strategies", {
        "name": "__probe_screen_cols__",
        "config": {"entry": ["hold_chg > -1e12 and close > ma20 * 0.5"],
                   "entry_any": ["rz_net5 > -1e12"],
                   "weekly_filter": ["w_close > 0", "w_ma20 > 0"],
                   "buy_trigger": ["open > 0"],
                   "sell_signal": ["close < ma5"],
                   "score_expr": "ht_net30"},
    })
    sid = (made.get("data") or {}).get("id")
    if not sid:
        check("建临时策略（含筹码条件）", False, json.dumps(made, ensure_ascii=False)[:160])
        return
    try:
        res = post_json("/api/screen/run", {
            "strategy_id": sid, "universe": "000300.SH", "trade_date": today,
            "limit": 20, "save": False}, timeout=900)
        r = res.get("data") or res
        cols = r.get("extra_cols") or []
        keys = [c["key"] for c in cols]
        # 条件里引用（先出现的排前面），去掉已有固定列的 close。
        # 顺序 = entry(hold_chg, ma20) → entry_any(rz_net5) → weekly_filter(w_close, w_ma20)
        #        → buy_trigger(open) → sell_signal(ma5) → score_expr(ht_net30)
        # 注：`w_close` 跟固定列「收盘」不是一回事（周K收盘），不该被当重复去掉。
        want = ["hold_chg", "ma20", "rz_net5", "w_close", "w_ma20", "open", "ma5", "ht_net30"]
        check("extra_cols = 条件/评分里引用到的字段（顺序 = 出现先后）",
              keys == want, f"{keys}（期望 {want}）")
        check("已有固定列的字段不重复出列（close）", "close" not in keys)
        check("每列都有中文表头且不是字段名本身",
              all(c.get("label") and c["label"] != c["key"] for c in cols),
              [(c["key"], c["label"]) for c in cols])
        check("每列都有合法数值格式（前端按它调 fmt）",
              all(c.get("fmt") in ("num", "big", "mv", "pct", "price", "int") for c in cols),
              [(c["key"], c["fmt"]) for c in cols])
        spec = ch.CHIP_COL_SPEC["rz_net5"]                      # (表头, 小数位, 颜色, 格式)
        got = next((c for c in cols if c["key"] == "rz_net5"), {})
        check("筹码列的表头/小数位/格式与 chips 字段表同源",
              (got.get("label"), got.get("dec"), got.get("color"), got.get("fmt")) == spec,
              f"{got} / 表里 {spec}")
        its = r.get("items") or []
        check("选股真有命中（这条件不该筛空）", bool(its),
              f"命中 {r.get('count')} / 扫描 {r.get('scanned')}")
        check("每条命中都带上了这些列的值（不必点开明细）",
              bool(its) and all(all(k in it for k in keys) for it in its),
              f"{len(its)} 条")
        check("命中里没有引用到的字段就不带出来（不白占带宽）",
              bool(its) and not [k for k in its[0] if k in set(ch.CHIPS_DERIVED)
                                 and k not in keys],
              sorted(k for k in (its[0] if its else {}) if k in set(ch.CHIPS_DERIVED)))

        # 纯技术策略：不该多出筹码列，但技术字段照样补列
        made2 = post_json("/api/strategies", {
            "name": "__probe_screen_tech__",
            "config": {"entry": ["rsi6 < 70"], "buy_trigger": ["open > 0"]}})
        sid2 = (made2.get("data") or {}).get("id")
        try:
            r2 = post_json("/api/screen/run", {
                "strategy_id": sid2, "universe": "000300.SH", "trade_date": today,
                "limit": 5, "save": False}, timeout=900)
            r2 = r2.get("data") or r2
            k2 = [c["key"] for c in r2.get("extra_cols") or []]
            check("纯技术策略：补技术列（rsi6 / open）且一个筹码列都没有",
                  k2 == ["rsi6", "open"] and not [k for k in k2 if k in set(ch.CHIPS_DERIVED)], k2)
            # 顺带验：引用到的字段都真带出了值（头几只有值的就行）
            i2 = (r2.get("items") or [{}])[0]
            check("纯技术策略的命中里也带上了这些列的值",
                  all(k in i2 for k in ("rsi6", "open")), sorted(i2)[:12])
        finally:
            if sid2:
                delete(f"/api/strategies/{sid2}")
    finally:
        delete(f"/api/strategies/{sid}")


def sec2b_sort() -> None:
    """【2b】排序表达式：选股页现填的一条 → 决定这一次的顺序。

    盯的是「填了到底有没有用、按哪个方向排、写错了会不会静默乱序」：
    这三件事读文本都读不出来 —— 顺序错了页面照样渲染得像模像样。
    """
    print("【2b】接口契约（排序表达式 → 顺序 / 列 / 报错）")
    cfg_js = get_json("/api/config")
    today = cfg_js.get("data", {}).get("latest_trade_date")
    if not today:
        skip("排序表达式", "还没有行情数据")
        return
    # 用一条**轻量**策略（不碰筹码）：这段要跑三次选股，别把回归拖成几分钟
    made = post_json("/api/strategies", {
        "name": "__probe_screen_sort__",
        "config": {"entry": ["rsi6 < 1e9"], "score_expr": ""}})
    sid = (made.get("data") or {}).get("id")
    if not sid:
        check("建临时策略（验排序表达式）", False,
              json.dumps(made, ensure_ascii=False)[:160])
        return
    base = {"strategy_id": sid, "universe": "000300.SH", "trade_date": today,
            "limit": 30, "save": False}
    try:
        r0 = post_json("/api/screen/run", dict(base), timeout=900)
        r0 = r0.get("data") or r0
        check("没填排序表达式：响应回空（沿用买卖策略的评分表达式）",
              (r0.get("sort_expr") or "") == "", repr(r0.get("sort_expr")))
        it0 = r0.get("items") or []
        check("买卖策略评分也空时退化成 |涨跌幅| 降序，不报错",
              len(it0) >= 2 and all(
                  abs(it0[i].get("pct_chg") or 0) >= abs(it0[i + 1].get("pct_chg") or 0)
                  for i in range(len(it0) - 1)),
              f"{len(it0)} 条")

        rm = post_json("/api/screen/run", dict(base, sort_expr="ma20"), timeout=900)
        rm = rm.get("data") or rm
        check("填了排序表达式 → 响应回显它（结果卡据此写「按什么排的」）",
              rm.get("sort_expr") == "ma20", repr(rm.get("sort_expr")))
        km = [c["key"] for c in rm.get("extra_cols") or []]
        check("排序表达式引用的字段自动进结果表（否则排了个看不见的东西）",
              "ma20" in km, km)
        itm = rm.get("items") or []
        vm = [it.get("ma20") for it in itm if it.get("ma20") is not None]
        check("结果按排序表达式**降序**（数值越大越靠前）",
              len(vm) >= 2 and all(vm[i] >= vm[i + 1] for i in range(len(vm) - 1)),
              f"前几个 ma20 = {vm[:8]}")
        check("排序值 = 「评分」列的值（同一个式子，不是两套口径）",
              bool(itm) and all(
                  (it.get("score") is None) == (it.get("ma20") is None)
                  and (it.get("score") is None or abs(it["score"] - it["ma20"]) < 1e-6)
                  for it in itm),
              f"score={itm[0].get('score')} / ma20={itm[0].get('ma20')}" if itm else "")

        rr = post_json("/api/screen/run", dict(base, sort_expr="-ma20"), timeout=900)
        rr = rr.get("data") or rr
        vr = [it.get("ma20") for it in (rr.get("items") or []) if it.get("ma20") is not None]
        check("表达式支持取反（-ma20 → 升序），不是只认字段名",
              len(vr) >= 2 and all(vr[i] <= vr[i + 1] for i in range(len(vr) - 1)),
              f"前几个 ma20 = {vr[:8]}")

        bad = post_json("/api/screen/run", dict(base, sort_expr="ma20 +"), timeout=120)
        check("排序表达式写错 → 400 且指明是「评分表达式」（不静默按空评分乱序跑完）",
              bad.get("ok") is False and "评分表达式" in (bad.get("error") or ""),
              json.dumps(bad, ensure_ascii=False)[:160])
    finally:
        delete(f"/api/strategies/{sid}")


def sec3_browser(e2e: bool) -> None:
    print("【3】浏览器实测（真页面 + 真函数 + 真 DOM）")
    if not (e2e and CHROME):
        skip("浏览器实测", "无 Chrome 或指定了 --no-e2e")
        return
    profile = os.path.join(tempfile.gettempdir(), "_chrome_screen_prof")
    # ⚠ 2026-10-09：预算原来只有 40 秒 —— 库已涨到 486 万行，全市场选股实测 **19.4s**
    #   （还不算页面加载与渲染），40s 撑不住 ⇒ 探针还没等到请求就被掐了，
    #   那两条排序断言因此报「没发请求」的**假红**（接口实测 200 且 sort_expr 生效）。
    #   提到 120s，与 watch 那几个探针同一个量级。
    raw = chrome_probe_out(BASE + PROBE, 120000, 400, profile)
    try:
        payload = json.loads(raw)
    except Exception:
        check("探针页返回结果", False, raw[:200])
        return
    if payload.get("error"):
        check("探针页跑通（无异常）", False, str(payload["error"])[:300])
        return
    for item in payload.get("res") or []:
        check(item["name"], item["ok"], item.get("extra", ""))


def main() -> int:
    e2e = "--no-e2e" not in sys.argv
    try:
        get_json("/api/config", timeout=20)
    except Exception as e:                                   # noqa: BLE001
        print(f"服务没起来（{BASE}）：{e}\n先 `python run.py` 再跑本脚本。")
        return 2
    sec1_static()
    if "--static" in sys.argv:
        pass
    else:
        sec2_api()
        sec2b_sort()
        sec3_browser(e2e)
    print(f"\n{'=' * 62}\n选股结果表回归：{PASS} 通过 / {FAIL} 失败 / {SKIP} 跳过")
    if FAILS:
        print("失败项：")
        for x in FAILS:
            print("  -", x)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
