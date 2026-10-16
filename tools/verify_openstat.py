#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
个股诊断页「开盘形态概率」回归（口径 + 接口契约 + 无头浏览器实测）

为什么需要它：这块四项频率**每一项都是一个口径**，读模板文本一个都验不出来：

  · 开盘涨跌幅必须按 **(今开/昨收−1)** 算 —— 拿 `pct_chg`（收盘涨跌幅）或
    「今开/今收」都能凑出一串数来，但那是**另一件事**（高开讲的是集合竞价那一跳）。
    所以【2】要把接口给的概率拿**日线原值独立重算**对拍，1e-9 内相等。
  · 「低开红柱 / 高开绿柱」是**条件概率**（分母 = 各自形态的天数），
    不是「占全部交易日」—— 这两者数值差一个量级，光看页面上一个百分数分不出来。
  · 分母是不是「区间内全部有效交易日」、区间是不是**页面上那段 K 线**，
    也只能拿接口回传的 rows 自己数一遍才能证。
  · 颜色（高开/红柱=红、低开/绿柱=绿）是 A 股约定，必须跟页面别处同源。
  · 页面渲染出来的四格是不是**逐字**等于后端给的那份（而不是前端自己算的）。

分三段：

  【1】静态口径  —— 口径唯一来源在 core/openstat.py；前端不自己算概率（模板里
                   不许出现「除以天数」这类算式）、四项标签与色 class 与后端一致、
                   一字板剔除只有一处实现（前端只认后端下发的 yz 标记）、
                   内联脚本语法过。
  【2】接口契约  —— /api/stock/<code>/detail 的 openstat：拿回传 rows 里
                    **查询日之前**那段日线原值独立重算四项概率对拍（1e-9）；
                    验分母（days=有效交易日数、条件概率分母=形态天数）、
                    色 class、边界（区间太短 / 一行都没有 → 给 — 不崩）、
                    代码大小写与 6 位码等价、bars 变化时区间跟着变。
  【3】浏览器实测 —— 真加载诊断页，四格标签/概率文本/颜色逐字对齐接口，
                    次数小字的分母口径正确，换票跟着换，图上标记里没有一字板；
                    ?embed=1（K线预览弹窗）里**照常渲染**（2026-10-10 起）。
                    （找不到 Chrome 就 SKIP，不算失败。）

跑法：
    C:/Users/ming/WorkBuddy/stockmining/.venv/Scripts/python.exe tools/verify_openstat.py
    加 --no-e2e 只跑【1】【2】。
依赖：服务在 127.0.0.1:8777。
"""

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
STOCK_HTML = os.path.join(ROOT, "templates", "stock.html")
OPENSTAT_PY = os.path.join(ROOT, "core", "openstat.py")
PROBE = "/static/_probe_openstat.html?run=1"

CODES = ["600519.SH", "300750.SZ", "688111.SH", "000001.SZ"]

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


def get_json(path: str, timeout: int = 240) -> dict:
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode("utf-8", "replace"))


def detail(code: str, bars: int = 260) -> dict:
    d = get_json("/api/stock/%s/detail?bars=%d" % (code, bars))
    return d.get("data") or {}


# ---------------------------------------------------------------------------
# 独立重算：**故意不复用 core/openstat**，按口径自己从 rows 数一遍
# ---------------------------------------------------------------------------

def recompute_from_rows(rows: list[dict], thr: float = 3.0) -> dict:
    """拿接口回传的 rows（含 open/high/low/close/pre_close）按口径独立重算四项概率。

    这一份是**验证脚本自己的实现**（不 import core/openstat）—— 否则两边同源，
    口径写错了也是「一致地错」，验不出来。

    ⚠ 一字板不算跳空（2026-10-10）：这里是**第二份实现**（按口径重新数一遍），
      不是去读接口给的 yz 标记 —— 两处都算对才叫口径真的对。
      判据：全天一个价（`high - low <= 0.005`）+ 方向（close vs pre_close）。
    """
    days = n_high = n_low = n_high_green = n_low_red = n_yz_up = n_yz_dn = 0
    for r in rows:
        try:
            o = float(r["open"]); pc = float(r["pre_close"]); c = float(r["close"])
        except (TypeError, ValueError, KeyError):
            continue
        if not (o > 0 and pc > 0):
            continue
        if o != o or pc != pc or c != c:      # NaN
            continue
        days += 1
        # 一字板（全天一个价）按方向分：收涨 ⇒ 一字涨停、收跌 ⇒ 一字跌停。
        # 缺 high/low 时（老接口/裁剪过的面板）判不出 ⇒ 不剔（与后端 applied=False 同义）。
        yz_up = yz_dn = False
        try:
            h = float(r["high"]); l = float(r["low"])
            if h == h and l == l and (h - l) <= 0.005:
                yz_up, yz_dn = c > pc, c < pc
        except (TypeError, ValueError, KeyError):
            pass
        if yz_up:
            n_yz_up += 1
        if yz_dn:
            n_yz_dn += 1
        gap = (o / pc - 1.0) * 100.0
        # 口径：达到阈值（含边界）；EPS 抵消浮点误差（与 core/openstat 同源约定）
        up, dn = gap >= thr - 1e-9, gap <= -thr + 1e-9
        if up and not yz_up:
            n_high += 1
            if c < o:                          # 收阴 = 绿柱
                n_high_green += 1
        if dn and not yz_dn:
            n_low += 1
            if c > o:                          # 收阳 = 红柱
                n_low_red += 1

    def rate(a, b):
        return (a / b * 100.0) if b else None

    return {"days": days, "n_high": n_high, "n_low": n_low,
            "n_high_green": n_high_green, "n_low_red": n_low_red,
            "n_yz_up": n_yz_up, "n_yz_dn": n_yz_dn,
            "p_high": rate(n_high, days), "p_low": rate(n_low, days),
            "p_high_green": rate(n_high_green, n_high),
            "p_low_red": rate(n_low_red, n_low)}


def close_to(a, b, tol=1e-9) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= tol


CHROME = None
for _c in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
           r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
           os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe")):
    if os.path.exists(_c):
        CHROME = _c
        break


def chrome_probe(url: str, budget_ms: int, timeout: int, profile: str) -> str:
    out = os.path.join(tempfile.gettempdir(), "_openstat_probe_dom.html")
    try:
        with open(out, "w", encoding="utf-8") as fh:
            subprocess.run(
                [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
                 "--user-data-dir=" + profile, "--window-size=1440,1500",
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
# 【1】静态口径
# ---------------------------------------------------------------------------

def seg1() -> None:
    print("【1】静态口径（口径唯一来源 / 前端不自己算）")
    src = open(OPENSTAT_PY, encoding="utf-8").read()
    html = open(STOCK_HTML, encoding="utf-8").read()

    check("口径实现在 core/openstat.py（唯一来源）",
          "def stats_from_frame" in src and "def snapshot" in src)
    check("阈值只有一处：OPEN_PCT 常量（模板里不许再写 3）",
          'OPEN_PCT = 3.0' in src)
    check("有 EPS 容差抵消浮点误差（边界口径唯一）", "EPS = 1e-9" in src)
    check("开盘涨跌幅按 (今开/昨收−1) 算",
          "/ pc[ok] - 1.0" in src or "- 1.0) * 100.0" in src)
    check("判据含边界（唯一实现：is_high_open 的 >= thr-EPS / is_low_open 的 <= -thr+EPS）",
          "return g >= float(threshold) - EPS" in src
          and "return g <= -float(threshold) + EPS" in src)
    check("昨收取 pre_close 列（复权口径），不自己 shift(1)",
          'df["pre_close"]' in src and "shift(" not in src.split("def stats_from_frame")[1]
          .split("def snapshot")[0])
    check("低开红柱 = 收阳（close>open）", "co > oo" in src)
    check("高开绿柱 = 收阴（close<open）", "co < oo" in src)
    check("条件概率：分母是各自形态天数",
          '"p_high_green": rate(n_high_green, n_high)' in src
          and '"p_low_red": rate(n_low_red, n_low)' in src)
    check("普通两项：分母是全部有效交易日",
          '"p_high": rate(n_high, days)' in src and '"p_low": rate(n_low, days)' in src)
    check("色 class 固定：高开/红柱=up，低开/绿柱=down",
          '("high_open", "高开3%", "p_high", "n_high", "up")' in src
          and '("low_open_red", "低开红柱", "p_low_red", "n_low_red", "up")' in src
          and '("low_open", "低开3%", "p_low", "n_low", "down")' in src
          and '("high_open_green", "高开绿柱", "p_high_green", "n_high_green", "down")' in src)

    # 前端不许自己拿 rows 再算一遍（那就成了第二套口径）。
    # 判据：渲染块里不出现 curRows（不重算）、不出现 toFixed / 百分比算式。
    m = re.search(r"function renderOpenStat\(\)\{[\s\S]*?\n\}", html)
    body = m.group(0) if m else ""
    check("renderOpenStat 存在", bool(body))
    check("前端不自己算概率（渲染块里不重算 / 不做除法）",
          bool(body) and "curRows" not in body and "toFixed" not in body
          and not re.search(r"[/÷]\s*(?:s\.days|days|100)", body),
          "" if body else "（没找到函数体）")

    # ---- 顶替「最新收盘」那一栏（2026-10-09 需求）----
    check("第 2 栏是 #st-head-open（开盘形态概率），不再是 #st-head-close",
          'id="st-head-open"' in html and 'id="st-head-open">' in html)
    check("原「最新收盘」的读数并进「股票」卡（#st-head-close 在 .stat.accent 里）",
          'id="st-head-close"' in html
          and re.search(r'class="stat accent"[\s\S]{0,400}id="st-head-close"', html) is not None)
    check("开盘概率卡里有四格容器 #open-list + 徽标 + 脚注",
          'id="open-list"' in html and 'id="open-badge"' in html and 'id="open-foot"' in html)
    check("没有数据时整栏卡片撤掉（card.style.display='none'）",
          "card.style.display = 'none'" in body and "card.style.display = ''" in body)
    # 2026-10-10：K线预览弹窗（?embed=1）里**也要**有这张卡 ⇒ embed 不再是撤卡的理由
    m_cells = re.search(r"function paintOpenCells\(\)\{[\s\S]*?\n\}", html)
    cells_body = m_cells.group(0) if m_cells else ""
    check("embed 下不再撤卡（renderOpenStat / paintOpenCells 里都没有 EMBED 闸门）",
          bool(body) and bool(cells_body)
          and "EMBED" not in body and "EMBED" not in cells_body,
          "" if (body and cells_body) else "（没找到函数体）")
    check("次数小字保留（n / N 次）",
          "次</div>" in html and ("count_text" in html and "denom_text" in html))
    check("curOpenstat 变量声明并在 render 里赋值",
          "let curOpenstat" in html and "curOpenstat = r.openstat" in html)
    check("render() 调用链里有 renderOpenStat()", "renderOpenStat();" in html)
    check("旧的两处注入点已清掉（不留死代码）",
          "#open-in-stock" not in html and "#open-in-close" not in html)
    css = open(os.path.join(ROOT, "static", "css", "app.css"), encoding="utf-8").read()
    check("CSS 有 .open-grid（四格排一行 = 4 列）",
          re.search(r"\.open-grid\{display:grid;grid-template-columns:repeat\(4,", css) is not None)
    check("次数小字有 .open-sub 样式且不折行",
          ".open-cell .open-sub{" in css and "white-space:nowrap" in css)

    # ---- AI 卡显隐（2026-10-09）----
    # 没分析过就不摆 AI 卡：整卡显隐在 static/js/ai.js 里控制。
    aijs = open(os.path.join(ROOT, "static", "js", "ai.js"), encoding="utf-8").read()
    check("AI 卡显隐有唯一开关 cardShow()",
          "function cardShow(on)" in aijs and "CARD.box" in aijs
          and "b.style.display = on ? '' : 'none'" in aijs)
    check("进页面先把 AI 卡收起来（没分析过不留空卡）",
          "hideCardAtStart" in aijs and "b.style.display = 'none'" in aijs)
    # cardRefresh 里：没历史（`!rows.length`）就 return ⇒ 卡片保持藏着
    m2 = re.search(r"async function cardRefresh\(o\)\s*\{[\s\S]*?\n  \}", aijs)
    cr = m2.group(0) if m2 else ""
    check("cardRefresh 只在有历史时才把卡片摆出来",
          bool(cr) and "cardShow(false)" in cr and "!rows.length" in cr
          and cr.count("cardShow(true)") >= 1)
    check("点「AI 分析」立刻摆出卡片（正在跑就得看得见）",
          re.search(r"async function cardRun\(o\)\s*\{[\s\S]*?cardShow\(true\)", aijs) is not None)
    r2 = subprocess.run([os.environ.get("NODE_BIN", "node"), "--check",
                         os.path.join(ROOT, "static", "js", "ai.js")],
                        capture_output=True, text=True)
    check("ai.js 语法过", r2.returncode == 0, (r2.stderr or "").strip()[:160])

    # ---- 幅度可调 + 即时重算（2026-10-09 需求）----
    # 口径面：阈值范围 / 解析 / 判定 / 原料重算 —— 全部在 core/openstat.py（唯一来源）
    check("阈值可调范围在 openstat.py（OPEN_PCT_MIN/MAX，模板不写死）",
          "OPEN_PCT_MIN = 0.1" in src and "OPEN_PCT_MAX = 20.0" in src)
    check("阈值解析唯一入口 clamp_threshold（非法退回默认、越界夹边界、留 1 位小数）",
          "def clamp_threshold" in src and "round(f, 1)" in src
          and "max(OPEN_PCT_MIN, min(OPEN_PCT_MAX, f))" in src)
    check("开盘涨跌幅有唯一函数 gap_pct（前端不下发第二套算法）",
          "def gap_pct" in src and "/ pc - 1.0" in src.replace(" ", " ")
          or "(o / pc - 1.0) * 100.0" in src)
    check("高开/低开判定唯一函数（is_high_open / is_low_open）",
          "def is_high_open" in src and "def is_low_open" in src)
    # stats_from_frame 内部必须**调用**这两个函数，而不是手写比较（否则口径分岔）
    body_sf = src.split("def stats_from_frame")[1].split("def snapshot")[0]
    check("stats_from_frame 走唯一判定函数（不手写 gap >= thr - EPS）",
          "is_high_open(" in body_sf and "is_low_open(" in body_sf
          and "gap >= thr - EPS" not in body_sf and "gap <= -thr + EPS" not in body_sf)
    check("下发前端重算用的逐根原料（bars: idx/gap/yang/yin）",
          '"bars": [{"idx":' in src.replace("\n", " ").replace("  ", " ")
          or ('"idx": int(i)' in src and '"yang": y' in src))
    check("后端权威重算 recalc_from_bars（回归拿它给前端对拍）",
          "def recalc_from_bars" in src)
    check("snapshot 下发 adjust（min/max/step/eps/bars）",
          '"adjust": {' in src and '"eps": EPS' in src)
    # 模板面：控件 + 状态源 + 即时重算入口
    check("K 线卡标题栏有「幅度」输入框 + 1%/3%/5% 快捷按钮",
          'id="st-open-pct"' in html and 'id="st-open-seg"' in html
          and 'data-t="3"' in html)
    check("阈值状态源唯一（let openThr / applyOpenThr 唯一入口）",
          "let openThr" in html and "function applyOpenThr(" in html)
    check("即时重算走后端下发的原料与 eps（不在前端写死 1e-9）",
          "function isHighOpen(" in html and "openAdj && openAdj.eps" in html
          and "1e-9" not in html.split("function isHighOpen")[1].split("function isLowOpen")[0])
    check("阈值改变了要重画图上标记（applyOpenThr → drawChart）",
          re.search(r"function applyOpenThr\([\s\S]*?drawChart\(\)", html) is not None)
    check("图上标记：高开=UP 红 / 低开=DOWN 绿，落在开盘价",
          "function openMarkSeries(" in html and "const y = r.open;" in html
          and "itemStyle:{ color: UP }" in html and "itemStyle:{ color: DOWN }" in html)
    check("请求带上 open_pct（首屏/换票/切周期按当前阈值取）",
          "'&open_pct=' + openThr" in html)
    check("接口层透传 open_pct（不进 _int，走 clamp_threshold）",
          "open_pct=request.args.get(\"open_pct\")" in
          open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
          or "open_pct=request.args.get('open_pct')" in
          open(os.path.join(ROOT, "app.py"), encoding="utf-8").read())
    scr = open(os.path.join(ROOT, "core", "screener.py"), encoding="utf-8").read()
    check("screener 里阈值只解析一次（clamp_threshold），并传给 stats_from_frame",
          "ows.clamp_threshold(open_pct)" in scr
          and "stats_from_frame(rows.iloc[:anchor_index + 1], open_thr)" in scr)
    check("CSS 有 .open-thr 输入框样式", ".open-thr input{" in css)

    # ---- 一字板不算跳空（2026-10-10 需求）----
    check("一字板口径在 openstat.py（ONE_WORD_TOL + one_word_mask 唯一实现）",
          "ONE_WORD_TOL = 0.005" in src and "def one_word_mask" in src)
    owb = src.split("def one_word_mask")[1].split("\ndef ")[0]
    check("一字板按「全天一个价 + 方向」判（high==low，涨/跌按 close vs pre_close）",
          "(h - l) <= ONE_WORD_TOL" in owb and "yz_up = flat & (c > pc)" in owb
          and "yz_dn = flat & (c < pc)" in owb)
    check("⚠ 一字板**不**去判「封在涨跌停价上」（避开「按当前股名判 ST」漏掉摘帽票的坑）",
          "limit_up" not in owb and "limit_down" not in owb and "limit_pct" not in owb)
    check("一字涨停从高开里剔、一字跌停从低开里剔（stats_from_frame 里）",
          "up = up & ~yz_up" in src and "dn = dn & ~yz_dn" in src)
    check("分母 days 不因一字板缩水（只剔分子，不剔分母）",
          "days = int(ok.sum())" in src)
    check("recalc_from_bars 与 stats_from_frame 同一处口径（也剔 yz）",
          'and not b.get("yz_up")' in src and 'and not b.get("yz_dn")' in src)
    check("逐根原料带 yz_up / yz_dn（前端不自己判 high==low）",
          '"yz_up": u' in src and '"yz_dn": d' in src)
    check("剔除说明由后端拼（_yizi_text / snapshot 下发 yizi）",
          "def _yizi_text" in src and '"yizi": {' in src)
    check("前端重算与图上标记都剔 yz（calcOpenStat / openMarkSeries 各一处以上）",
          html.count("!b.yz_up") >= 2 and html.count("!b.yz_dn") >= 2)
    check("脚注写着一字板剔除天数（openYiziText 读后端 yizi.text，前端不自己拼）",
          "function openYiziText(" in html and "y.applied" in html and "y.text" in html)
    _yz_sent = "一字板（全天一个价）不算跳空：一字涨停不计入高开、一字跌停不计入低开。"
    check("口径说明里写着「一字板不算跳空」（后端与模板**同句**）",
          _yz_sent in src and _yz_sent in html)

    # 模板语法（内联脚本能被 node 解析）
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "_tpl_syntax_check.py"),
                        STOCK_HTML], capture_output=True, text=True)
    check("模板内联脚本语法过", r.returncode == 0 and "OK" in r.stdout,
          (r.stdout or r.stderr).strip()[:160])


# ---------------------------------------------------------------------------
# 【2】接口契约
# ---------------------------------------------------------------------------

def seg2() -> None:
    print("【2】接口契约（独立重算对拍 / 分母 / 边界）")
    for code in CODES:
        d = detail(code)
        if d.get("error"):
            check(f"{code} 有行情", False, d["error"])
            continue
        os_ = d.get("openstat") or {}
        items = os_.get("items") or []
        check(f"{code} openstat 四项齐", len(items) == 4,
              "keys=" + ",".join(x.get("key", "") for x in items))
        if len(items) != 4:
            continue

        rows = d.get("rows") or []
        anchor_idx = (d.get("anchor") or {}).get("index")
        # 区间 = 查询日之前那段（含查询日那根）——与后端同一段
        seg_rows = rows[:int(anchor_idx) + 1] if isinstance(anchor_idx, int) else rows
        want = recompute_from_rows(seg_rows)

        by_key = {x["key"]: x for x in items}
        check(f"{code} 交易日数 == 独立重算", int(os_.get("days") or -1) == want["days"],
              f"api={os_.get('days')} calc={want['days']}")
        for key, pkey in (("high_open", "p_high"), ("low_open", "p_low"),
                          ("low_open_red", "p_low_red"), ("high_open_green", "p_high_green")):
            got = by_key.get(key, {}).get("value")
            check(f"{code} {by_key.get(key, {}).get('label', key)} 概率对拍",
                  close_to(got, want[pkey]),
                  f"api={got} calc={want[pkey]}")
        check(f"{code} 次数小字 == 独立重算",
              [int(x["count"]) for x in items] ==
              [want["n_high"], want["n_low"], want["n_low_red"], want["n_high_green"]])
        check(f"{code} 色 class 正确",
              by_key["high_open"]["cls"] == "up" and by_key["low_open"]["cls"] == "down"
              and by_key["low_open_red"]["cls"] == "up"
              and by_key["high_open_green"]["cls"] == "down")
        # 一字板（2026-10-10）：接口给的剔除数 == 独立重算数；applied 必须是 true
        yzi = os_.get("yizi") or {}
        check(f"{code} 一字板剔除数 == 独立重算（且 applied=True）",
              yzi.get("applied") is True
              and int(yzi.get("n_up") or 0) == want["n_yz_up"]
              and int(yzi.get("n_dn") or 0) == want["n_yz_dn"],
              f"api={yzi.get('n_up')}/{yzi.get('n_dn')} calc={want['n_yz_up']}/{want['n_yz_dn']}")
        # 条件概率的分母不能等于全部交易日（除非恰好相等，那种情况下也算通过）
        if want["n_high"] not in (0, want["days"]):
            check(f"{code} 高开绿柱是条件概率（分母≠全部天数）",
                  abs(float(os_["items"][3]["value"]) -
                      (want["n_high_green"] / want["days"] * 100.0)) > 1e-6
                  or want["n_high_green"] == 0)
        # 概率文本带 %
        check(f"{code} 概率文本带 %",
              all(str(x.get("text", "")).endswith("%") or x.get("text") == "—" for x in items))
        # 区间与图上窗口同段
        rng = os_.get("range") or {}
        check(f"{code} 区间非空且起止有序",
              bool(rng.get("start")) and bool(rng.get("end")) and rng["start"] <= rng["end"],
              f"{rng.get('start')}~{rng.get('end')}")

    # 6 位码与带后缀等价
    a = detail("600519.SH")
    b = get_json("/api/stock/600519/detail?bars=260").get("data") or {}
    check("6 位码与带后缀返回同一份概率",
          json.dumps(a.get("openstat"), sort_keys=True) ==
          json.dumps(b.get("openstat"), sort_keys=True))

    # bars 变化 → 区间跟着变（跟随页面 K 线根数）
    d60 = detail("600519.SH", bars=60)
    d260 = detail("600519.SH", bars=260)
    check("bars=60 / 260 的交易日数不同（区间跟随 K 线根数）",
          int((d60.get("openstat") or {}).get("days") or 0) <
          int((d260.get("openstat") or {}).get("days") or 0),
          f"60->{(d60.get('openstat') or {}).get('days')} "
          f"260->{(d260.get('openstat') or {}).get('days')}")

    # 查询日往回调：区间终点必须 <= 查询日（不含未来数据）
    hist = get_json("/api/stock/600519.SH/detail?bars=260&trade_date=2025-06-30").get("data") or {}
    hr = (hist.get("openstat") or {}).get("range") or {}
    check("指定历史查询日时，统计区间终点不超过查询日（无未来数据）",
          bool(hr.get("end")) and hr["end"] <= "2025-06-30",
          f"end={hr.get('end')}")

    # 边界：不存在的票 → error，不是 500
    bad = get_json("/api/stock/999999.SZ/detail?bars=260")
    check("不存在的票返回错误而不是崩", bad.get("ok") is False and bool(bad.get("error")),
          str(bad.get("error"))[:80])

    # ---- open_pct（幅度）参数：解析 / 越界 / 与后端权威重算对拍 ----
    d_def = get_json("/api/stock/300750.SZ/detail?bars=260").get("data") or {}
    check("不给 open_pct 时阈值 = 默认（3%）",
          float((d_def.get("openstat") or {}).get("threshold") or -1) == 3.0,
          str((d_def.get("openstat") or {}).get("threshold")))
    d5 = get_json("/api/stock/300750.SZ/detail?bars=260&open_pct=5").get("data") or {}
    check("open_pct=5 → 阈值 5%，且次数随阈值变（阈值越高越少）",
          float((d5.get("openstat") or {}).get("threshold") or -1) == 5.0
          and int((d5.get("openstat") or {}).get("items", [{}])[0].get("count") or 0)
              <= int((d_def.get("openstat") or {}).get("items", [{}])[0].get("count") or 0))
    # 越界夹边界（不是报错）
    dl = get_json("/api/stock/300750.SZ/detail?bars=260&open_pct=0").get("data") or {}
    dh = get_json("/api/stock/300750.SZ/detail?bars=260&open_pct=999").get("data") or {}
    check("open_pct=0 夹到下限 0.1", float((dl.get("openstat") or {}).get("threshold") or -1) == 0.1)
    check("open_pct=999 夹到上限 20", float((dh.get("openstat") or {}).get("threshold") or -1) == 20.0)
    # 非法退回默认（不 500）
    db_ = get_json("/api/stock/300750.SZ/detail?bars=260&open_pct=abc")
    check("open_pct=abc 退回默认 3%（不崩、不 500）",
          db_.get("ok") is True
          and float(((db_.get("data") or {}).get("openstat") or {}).get("threshold") or -1) == 3.0)
    # 后端权威重算（recalc_from_bars）必须与接口给的 items 逐字一致 —— 这一条是
    # 「前端即时重算」的地基：前端 JS 算的数要跟 recalc_from_bars 相等，才谈得上同源。
    from core import openstat as ows
    adj = (d5.get("openstat") or {}).get("adjust") or {}
    rec = ows.recalc_from_bars(adj.get("bars"), 5.0)
    it5 = (d5.get("openstat") or {}).get("items") or []
    check("后端 recalc_from_bars(5%) == 接口 items（重算地基对得上）",
          len(it5) == 4 and int(it5[0]["count"]) == rec["n_high"]
          and int(it5[1]["count"]) == rec["n_low"]
          and int(it5[2]["count"]) == rec["n_low_red"]
          and int(it5[3]["count"]) == rec["n_high_green"]
          and int((d5.get("openstat") or {}).get("days") or -1) == rec["days"],
          f"items={[x['count'] for x in it5]} recalc={[rec['n_high'], rec['n_low'], rec['n_low_red'], rec['n_high_green']]}")
    check("adjust 下发的 eps 与 openstat.EPS 一致（前端拿它做判据）",
          abs(float(adj.get("eps") or -1) - ows.EPS) < 1e-18,
          f"adj={adj.get('eps')} EPS={ows.EPS}")
    check("adjust.bars 条数 == 交易日数", len(adj.get("bars") or []) == rec["days"])

    # clamp_threshold 的纯函数行为（非法/越界/取整）
    check("clamp_threshold：非法→默认，0→下限，999→上限，2.94→2.9",
          ows.clamp_threshold(None) == 3.0 and ows.clamp_threshold("") == 3.0
          and ows.clamp_threshold(0) == 0.1 and ows.clamp_threshold(999) == 20.0
          and ows.clamp_threshold(2.94) == 2.9)
    check("gap_pct 拉平了标量与数组两种调用",
          abs((ows.gap_pct(10.3, 10.0) or 0) - 3.0) < 1e-9 or
          abs((ows.gap_pct(10.3, 10.0) or 0) - 3.0000000000000027) < 1e-9)

    # 纯函数边界：空 / 缺列
    from core import openstat as ows
    e = ows.snapshot(ows.stats_from_frame(None))
    check("空 DataFrame → 四项都是 —（不崩）",
          len(e["items"]) == 4 and all(x["text"] == "—" for x in e["items"]))
    import pandas as pd
    df_missing = pd.DataFrame({"open": [1.0], "close": [1.0]})   # 缺 pre_close
    e2 = ows.snapshot(ows.stats_from_frame(df_missing))
    check("缺 pre_close 列 → 四项都是 —（不崩）",
          all(x["text"] == "—" for x in e2["items"]))
    # 合成数据：已知答案（高开4%收阴 + 低开5%收阳 + 平开）
    import numpy as np
    df_syn = pd.DataFrame({
        "trade_date": ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"],
        "open": [10.4, 9.5, 10.0, 10.0],
        "pre_close": [10.0, 10.0, 10.0, 10.0],
        "close": [10.0, 9.8, 10.0, 10.0],   # 第1根高开4%收阴；第2根低开5%收阳
    })
    s = ows.stats_from_frame(df_syn)
    check("合成数据：高开3%=1/4、低开3%=1/4、高开绿柱=1/1、低开红柱=1/1",
          s["days"] == 4 and s["n_high"] == 1 and s["n_low"] == 1
          and s["n_high_green"] == 1 and s["n_low_red"] == 1
          and close_to(s["p_high"], 25.0) and close_to(s["p_low"], 25.0)
          and close_to(s["p_high_green"], 100.0) and close_to(s["p_low_red"], 100.0),
          json.dumps({k: s[k] for k in ("days", "n_high", "n_low", "n_high_green", "n_low_red")}))
    # 阈值边界：**达到即算**（含恰好 ±3%），带 EPS 容差抵消浮点误差。
    # ⚠ `103.0/100.0-1` 在二进制浮点下是 3.0000000000000027 —— 正是要 EPS 的理由：
    #   真实数据里「恰好跳空 3%」的有 180 余根，不能靠浮点运气决定算不算。
    df_edge = pd.DataFrame({
        "trade_date": ["a", "b", "c", "d"],
        "open": [103.0, 103.01, 97.0, 96.99],
        "pre_close": [100.0, 100.0, 100.0, 100.0],
        "close": [104.0, 104.0, 96.0, 96.0]})
    se = ows.stats_from_frame(df_edge)
    check("阈值含边界：恰好 ±3% 都算，超过当然也算",
          se["n_high"] == 2 and se["n_low"] == 2,
          f"n_high={se['n_high']} n_low={se['n_low']}")
    check("高开却收阳 → 不算绿柱；低开却收阴 → 不算红柱",
          se["n_high_green"] == 0 and se["n_low_red"] == 0)
    # 收平（close==open）既不算红柱也不算绿柱
    df_flat = pd.DataFrame({
        "trade_date": ["a"], "open": [10.5], "pre_close": [10.0], "close": [10.5]})
    sf = ows.stats_from_frame(df_flat)
    check("高开且收盘==开盘：不算绿柱", sf["n_high"] == 1 and sf["n_high_green"] == 0)

    # ---- 一字板不算跳空（2026-10-10 需求）----
    # 合成数据：一字涨停 / 一字跌停 / 普通高开 / 平盘 / 普通低开（昨收都是 10.00）
    df_yz = pd.DataFrame({
        "trade_date": ["a", "b", "c", "d", "e"],
        "pre_close": [10.0] * 5,
        "open":  [11.0, 9.0, 10.4, 10.0, 9.5],
        "high":  [11.0, 9.0, 10.6, 10.0, 9.6],
        "low":   [11.0, 9.0, 10.2, 10.0, 9.4],
        "close": [11.0, 9.0, 10.0, 10.0, 9.8],
    })
    sy = ows.stats_from_frame(df_yz, 3.0)
    check("一字板：分母 days 不缩水（仍是 5 个交易日）", sy["days"] == 5, str(sy["days"]))
    check("一字涨停不计入「高开」（只有那根 +4% 的算）", sy["n_high"] == 1, str(sy["n_high"]))
    check("一字跌停不计入「低开」（只有那根 −5% 的算）", sy["n_low"] == 1, str(sy["n_low"]))
    check("一字板天数一起回传（n_yz_up / n_yz_dn / applied）",
          sy["n_yz_up"] == 1 and sy["n_yz_dn"] == 1 and sy["yizi_applied"] is True)
    # 重要的反例：**打开过**的涨停（high != low）是能买到的，必须照旧计入
    so = ows.stats_from_frame(pd.DataFrame({
        "trade_date": ["a"], "pre_close": [10.0], "open": [10.5],
        "high": [11.0], "low": [10.4], "close": [11.0]}), 3.0)
    check("打开过的涨停（high≠low）仍算高开", so["n_high"] == 1 and so["n_yz_up"] == 0)
    # 缺 high/low 列 ⇒ 不剔除，并 applied=False（页面照实说，不假装排过）
    sn2 = ows.stats_from_frame(pd.DataFrame({
        "trade_date": ["a"], "pre_close": [10.0], "open": [11.0], "close": [11.0]}), 3.0)
    check("缺 high/low 列 ⇒ 不剔除且 applied=False",
          sn2["n_high"] == 1 and sn2["yizi_applied"] is False)
    sn_yz = ows.snapshot(sy)
    check("snapshot 下发 yizi（applied / n_up / n_dn / text 四件套）",
          set(("applied", "n_up", "n_dn", "text")).issubset(set((sn_yz.get("yizi") or {}).keys()))
          and (sn_yz["yizi"]["text"] or "") != "",
          str(sn_yz.get("yizi")))
    check("口径说明（note）里写了「一字板不算跳空」", "一字板" in (sn_yz.get("note") or ""))
    ry = ows.recalc_from_bars(sy["bars"], 3.0)
    check("后端权威重算 recalc_from_bars 同样剔一字板",
          ry["n_high"] == sy["n_high"] and ry["n_low"] == sy["n_low"],
          f"recalc={ry['n_high']}/{ry['n_low']} stat={sy['n_high']}/{sy['n_low']}")

    # ---- 真数据：拿一只**确实有一字板**的票验（别让断言退化成 0==0 的空验）----
    # 000908.SZ（现名「石药景峰」，已摘帽）：区间里有大量 +5% 的 ST 一字板，而这批
    # **恰好**就是「按当前股名判 ST 会整批漏掉」的那一类 —— 正是这条口径的靶子。
    dz = get_json("/api/stock/000908.SZ/detail?bars=1000").get("data") or {}
    doz = dz.get("openstat") or {}
    rows_z = dz.get("rows") or []
    az = (dz.get("anchor") or {}).get("index")
    seg_z = rows_z[:int(az) + 1] if isinstance(az, int) else rows_z
    wz = recompute_from_rows(seg_z)
    check("000908.SZ 区间里确实有一字板样本（不是空验）", wz["n_yz_up"] > 0,
          f"n_yz_up={wz['n_yz_up']} n_yz_dn={wz['n_yz_dn']}")
    check("000908.SZ 一字板剔除数 == 独立重算",
          int((doz.get("yizi") or {}).get("n_up") or 0) == wz["n_yz_up"]
          and int((doz.get("yizi") or {}).get("n_dn") or 0) == wz["n_yz_dn"],
          f"api={(doz.get('yizi') or {}).get('n_up')}/{(doz.get('yizi') or {}).get('n_dn')} "
          f"calc={wz['n_yz_up']}/{wz['n_yz_dn']}")
    check("000908.SZ 四格次数 == 独立重算（高开/低开已剔一字板）",
          [int(x["count"]) for x in (doz.get("items") or [])] ==
          [wz["n_high"], wz["n_low"], wz["n_low_red"], wz["n_high_green"]],
          f"api={[x['count'] for x in (doz.get('items') or [])]} "
          f"calc={[wz['n_high'], wz['n_low'], wz['n_low_red'], wz['n_high_green']]}")


# ---------------------------------------------------------------------------
# 【3】浏览器实测
# ---------------------------------------------------------------------------

def run_probe_checks(body: str) -> None:
    """探针页输出的文本逐行读成断言（key value 形式）。"""
    lines = {}
    for ln in (body or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        parts = ln.split(" ", 1)
        lines[parts[0]] = parts[1] if len(parts) > 1 else ""
    check("探针跑完（不是半路超时）", lines.get("DONE") is not None,
          "（探针没输出）" if not lines else "")
    check("开盘形态概率卡存在", lines.get("CARD_EXISTS") == "true")
    # `?embed=1` 的探针走一条**早退分支**（只验「卡在 + 四格与接口一致」）。
    # 2026-10-10 用户要求：K线预览弹窗里也要有这张卡 ⇒ 不再是「藏起来」。
    if lines.get("EMBED_VISIBLE") is not None:
        check("embed 模式下卡片照样显示（K线预览里也要有这张卡）",
              lines.get("EMBED_VISIBLE") == "true", lines.get("EMBED_VISIBLE", ""))
        check("embed 模式下四格渲染出来且条数 == items 条数",
              _exp_eq(lines.get("EMBED_CELL_N", "")), lines.get("EMBED_CELL_N", ""))
        check("embed 模式下四格数值与接口逐字一致",
              lines.get("EMBED_ITEMS_MATCH", "").startswith("true"),
              lines.get("EMBED_ITEMS_MATCH", ""))
        check("embed 模式下也能在悬停里读到完整口径（含一字板那句）",
              lines.get("EMBED_NOTE_IN_TIP") == "true")
        return
    # 顶替「最新收盘」那一栏（2026-10-09 需求）
    check("它占的是 `#st-head` 第 2 栏（下标 1，原「最新收盘」的位置）",
          lines.get("SLOT_INDEX", "").startswith("1 EXP"), lines.get("SLOT_INDEX", ""))
    check("有数据时真显示（不是 display:none 的空壳）", lines.get("VISIBLE") == "true")
    check("原「最新收盘」那张卡已经不在（内容并进「股票」卡）",
          lines.get("CLOSE_CARD_GONE") == "true")
    check("「股票」卡里带着收盘读数（价格 / 涨跌幅 / 日期）",
          lines.get("READOUT_IN_STOCK") == "true" and lines.get("READOUT_HAS_PRICE") == "true")
    check("四格渲染出来", lines.get("CELL_N") == "4", "CELL_N=" + lines.get("CELL_N", ""))
    check("四格与接口逐字一致（标签/概率/颜色）", lines.get("ITEMS_MATCH") == "true")
    # 2026-10-09 用户要求四格排一行（4 列）
    check("四格排在同一行（4 列 × 1 行，不是折成 2×2）",
          lines.get("GRID_ROWS", "").startswith("1 EXP"))
    check("四格排成一行后没被挤爆（三行小字都不溢出、不出格）",
          lines.get("CELLS_FIT", "").startswith("true"), lines.get("CELLS_FIT", ""))
    check("高开/低开红柱 = 红（up）", lines.get("COLOR_UP") == "true")
    check("低开/高开绿柱 = 绿（down）", lines.get("COLOR_DOWN") == "true")
    # 次数小字：用户明确要保留「n / N 次」
    check("四格都带「n / N 次」小字", lines.get("SUB_SHOWN") == "true")
    check("高开/低开两项的分母 = 全部交易日",
          lines.get("DENOM_ALL_HIGH") == "true" and lines.get("DENOM_ALL_LOW") == "true")
    check("低开红柱分母 = 低开3%天数", lines.get("DENOM_COND_RED") == "true")
    check("高开绿柱分母 = 高开3%天数", lines.get("DENOM_COND_GREEN") == "true")
    badge_line = lines.get("BADGE", "")
    # 探针一行输出 "BADGE <badge> EXPECT <days>"，这里一起读
    mm = re.match(r"^(.*?)\s*EXPECT\s*(.*)$", badge_line)
    badge = (mm.group(1) if mm else badge_line).strip()
    expect = (mm.group(2) if mm else "").strip()
    check("徽标 = 交易日数", bool(expect) and expect in badge, f"line={badge_line}")
    check("完整口径（区间 / 脚注）能在悬停提示里读到", lines.get("NOTE_IN_TIP") == "true")
    # ---- 一字板不算跳空（2026-10-10 需求）----
    check("逐根一字标记条数 == 后端下发的 yizi.n_up / n_dn",
          lines.get("YZ_N_MATCH", "").startswith("true"), lines.get("YZ_N_MATCH", ""))
    check("一字板剔除已生效（applied=True）", lines.get("YZ_APPLIED") == "true")
    check("脚注写着一字板剔除天数（后端给的句子）",
          lines.get("YZ_FOOT", "").startswith("true"), lines.get("YZ_FOOT", ""))
    # AI 卡：没分析过就不摆 —— 拿接口的历史条数当判据，别写死「一定显示/一定不显示」
    ai_line = lines.get("AI_HIST_N", "")
    am = re.match(r"^(\d+)\s+SHOWN\s+(\S+)\s+EXPECT_SHOWN\s+(\S+)$", ai_line.strip())
    if am:
        n, shown, want = int(am.group(1)), am.group(2), am.group(3)
        check("AI 卡显隐跟着「有没有历史」走（没分析过就不摆）",
              shown == want, f"n={n} shown={shown} want={want}")
    else:
        check("AI 卡显隐跟着「有没有历史」走（没分析过就不摆）", False,
              f"line={ai_line}")
    check("换票后这块跟着换", lines.get("SWITCH_OK", "").startswith("true"),
          lines.get("SWITCH_OK", ""))

    # ---- 幅度可调 + 即时重算（2026-10-09 需求）----
    # 控件上下限/步长来自后端下发的 `adjust`（别在模板里写死 0.1/20）
    check("K 线卡上有「幅度」输入框，且 min/max/step 来自后端 adjust",
          _exp_eq(lines.get("THR_INPUT_EXISTS", "")),
          lines.get("THR_INPUT_EXISTS", ""))
    check("输入框初值 = 后端当前阈值",
          _exp_eq(lines.get("THR_INPUT_VAL", "")), lines.get("THR_INPUT_VAL", ""))
    check("阈值范围与后端 adjust 一致", lines.get("ADJ_RANGE_OK") == "true")
    check("逐根原料条数 == 交易日数（前端重算的原料齐）",
          _exp_eq(lines.get("ADJ_BARS_N", "")), lines.get("ADJ_BARS_N", ""))
    # 前端即时重算 vs 探针独立数一遍（六个阈值）—— 判据必须逐字一致
    check("前端即时重算 == 独立重算（多阈值对拍，含不限阈值/边界）",
          lines.get("RECALC_MATCH", "").startswith("true"), lines.get("RECALC_MATCH", ""))
    check("默认那一档前端重算与后端 items 完全一致（首屏不漂）",
          lines.get("RECALC_AT_DEFAULT", "").startswith("true"),
          lines.get("RECALC_AT_DEFAULT", ""))
    check("改阈值后四格标签跟着换百分比（高开0.5%）",
          _exp_eq(lines.get("AFTER_SET_LABEL", "")), lines.get("AFTER_SET_LABEL", ""))
    check("改阈值后概率按新阈值算（与独立重算一致）",
          _exp_eq(lines.get("AFTER_SET_VAL", "")), lines.get("AFTER_SET_VAL", ""))
    check("改阈值后输入框回显新值", _exp_eq(lines.get("AFTER_SET_INPUT", "")),
          lines.get("AFTER_SET_INPUT", ""))
    check("图上高开/低开标记点数 == 按新阈值重算的次数",
          lines.get("AFTER_SET_MARKS", "").startswith("true"), lines.get("AFTER_SET_MARKS", ""))
    check("这次改的阈值真产生了标记（不是空验）",
          lines.get("AFTER_SET_MARKS_NONZERO") == "true")
    check("阈值不是 1/3/5 时快捷按钮都不亮", lines.get("SEG_NONE_ON_AT_0_5", "").startswith("true"),
          lines.get("SEG_NONE_ON_AT_0_5", ""))
    check("点「3%」快捷按钮回到默认档", lines.get("SEG_3_RESTORE", "").startswith("true"),
          lines.get("SEG_3_RESTORE", ""))
    check("手输越界（999）在 change 时夹回 max",
          _exp_eq(lines.get("THR_CLAMP_MAX", "")), lines.get("THR_CLAMP_MAX", ""))
    # ---- 图上标记（高开=红 / 低开=绿，落在开盘价上）----
    check("图上有高开/低开两类标记 series",
          lines.get("MARK_SERIES_PRESENT", "").startswith("true"),
          lines.get("MARK_SERIES_PRESENT", ""))
    check("高开标记 = 红（#e5352b，A 股习惯）", lines.get("MARK_HI_COLOR_UP", "").startswith("true"),
          lines.get("MARK_HI_COLOR_UP", ""))
    check("低开标记 = 绿（#12a06a）", lines.get("MARK_LO_COLOR_DOWN", "").startswith("true"),
          lines.get("MARK_LO_COLOR_DOWN", ""))
    check("标记落在开盘价上", lines.get("MARK_ON_OPEN_PRICE", "").startswith("true"),
          lines.get("MARK_ON_OPEN_PRICE", ""))
    check("标记的 idx 都能对回 rows（停牌跳过的根不会错位）",
          lines.get("MARK_IDX_VALID") == "true")
    check("图上标记里没有一字板那几根（与四格同一份判据）",
          lines.get("MARK_NO_ONE_WORD", "").startswith("true"),
          lines.get("MARK_NO_ONE_WORD", ""))


def _exp_eq(line: str) -> bool:
    """探针输出形如 `<可能的 true 前缀> <值> EXPECT <期望>` 时比两边是否相等。

    探针为了让这行既能被人读、也能被断言，格式统一成「值 EXPECT 期望」；
    有的行前面还带一个 true/false（表示「探针自己也判过了」），这里把它剥掉
    再比 —— 剥的规则是：去掉行首的 `true ` / `false `。
    """
    m = re.match(r"^(.*?)\s*EXPECT\s*(.*)$", (line or "").strip())
    if not m:
        return False
    got = m.group(1).strip()
    got = re.sub(r"^(?:true|false)\s+", "", got)
    return got == m.group(2).strip()


def seg3(profile: str) -> None:
    print("【3】浏览器实测（真加载 + 读 DOM）")
    if not CHROME:
        skip("浏览器实测", "找不到 Chrome")
        return
    body = chrome_probe(BASE + PROBE, budget_ms=60000, timeout=180, profile=profile)
    run_probe_checks(body)
    # embed 模式（2026-10-10）：预览弹窗里**也要有**这张卡 ⇒ 验「卡在 + 四格与接口一致」
    body2 = chrome_probe(BASE + PROBE + "&embed=1", budget_ms=45000, timeout=150,
                         profile=profile)
    lines = {}
    for ln in (body2 or "").splitlines():
        p = ln.strip().split(" ", 1)
        if p and p[0]:
            lines[p[0]] = p[1] if len(p) > 1 else ""
    if lines.get("CARD_EXISTS") is not None:
        run_probe_checks(body2)
    else:
        skip("embed 模式检查", "探针未返回")


def main() -> int:
    e2e = "--no-e2e" not in sys.argv
    profile = os.path.join(tempfile.gettempdir(), "_openstat_chrome_prof")

    seg1()
    seg2()
    if e2e:
        seg3(profile)
    else:
        skip("[3] 浏览器实测", "--no-e2e")

    print(f"\n结果：{PASS} 通过 / {FAIL} 失败 / {SKIP} 跳过")
    if FAILS:
        print("失败项：")
        for f in FAILS:
            print("  -", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
