#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
选股策略「看当日命中」回归（静态口径 + 接口契约 + 无头 Chrome 实测点击）

为什么需要它：命中列表要能「点一只票就看它的 K线」，这件事静态读文本读不出来 ——
链接可能拼错、可能一行串到另一行的代码、可能点进去 404，模板里都看着是对的。
所以本脚本分三段：

  【1】静态口径   —— 试跑区有没有样例数输入与「排序表达式」框、行模板里代码/名称是诊断页
                     链接而「K线」是按钮（弹窗预览，不再跳页）、弹窗只走 app.js 的
                     klinePreview 唯一入口、弹窗右上角的「下一个」按命中列表翻页、预览参数
                     用的是「试跑当时」的条件快照、表头是 testTh() 出（可点排序、缺值沉底、
                     「#」回服务端原序、只重画不重跑）、有没有把结果表
                     放进可滚动容器、旧文案「仅显示前 10 只」有没有残留、内联脚本语法
  【2】接口契约   —— /api/universe/test 的 sample 截断是否被 honored、样例字段够不够
                      前端渲染（pct_chg/total_mv/turnover_rate/score）、/stock/<代码> 是否
                      真能打开、?embed=1 预览模式是否只隐藏非图表卡片而图表容器还在
  【2b】排序契约  —— `sort_expr` 真在求值，不是只认字段名：不传 → 代码升序且评分全空；
                      传 turnover_rate → 非递增且「评分 == 该字段值」；传 ma20（不在试跑
                      固定 only 集里）→ 有值且非递增（证明 only= 跟着表达式扩，否则评分
                      恒空、顺序悄悄退回原序）；传 -total_mv → 非递增；语法错/未知字段 →
                      400 报 [排序表达式]；「全算不出值」时必须有 sort_note（不许静默）
  【3】浏览器实测 —— 把 /pools 塞进同源 iframe，真点一次「看当日命中」（用最小的
                      创业板指 100 只，不是全部A股），逐行抠 <a href> 验链接、真请求
                      验 200、**真点表头**验行序真换了（再点反向、「#」回原序、行内 K线
                      按钮跟着换行、缺值双向沉底）、**真点一次「K线」**验弹窗里是不是嵌着
                      诊断页预览模式（内层图表容器有 canvas、非图表卡片被隐藏、外层页面没被
                      跳走）、点一次「下一个」验它换到命中列表的下一只且弹窗没被重建、
                      关掉弹窗验它被移除，最后改条件验「结果已过期」提示

跑法：
    C:/Users/ming/.workbuddy/binaries/python/envs/stockmining/Scripts/python.exe tools/verify_pool_ui.py
    加 --no-e2e 只跑【1】【2】（不用起浏览器）。
依赖：服务在 127.0.0.1:8777；本机 Chrome（找不到就把【3】标 SKIP，不算失败）。
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BASE = "http://127.0.0.1:8777"
PROBE = "/static/_probe_pool.html?run=1"
POOL_HTML = os.path.join(ROOT, "templates", "pool.html")
PROBE_HTML = os.path.join(ROOT, "static", "_probe_pool.html")
NODE = r"C:\Users\ming\.workbuddy\binaries\node\versions\22.22.2-3\node.exe"
SMALL_BASE = "399006.SZ"          # 创业板指 100 只：探针唯一敢用的池子（全市场要跑 200 秒）

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


def post(path: str, data: dict, timeout: int = 300) -> dict:
    req = urllib.request.Request(BASE + path, data=json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())


def get(path: str, timeout: int = 60) -> str:
    return urllib.request.urlopen(BASE + path, timeout=timeout).read().decode("utf-8", "replace")


CHROME = None
for _c in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
           r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
           os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe")):
    if os.path.exists(_c):
        CHROME = _c
        break


def chrome_probe_out(url: str, budget_ms: int, timeout: int, profile: str) -> str:
    """渲染探针页并返回 <pre id="out"> 里的文本。"""
    out = os.path.join(tempfile.gettempdir(), "_pool_probe_dom.html")
    try:
        with open(out, "w", encoding="utf-8") as fh:
            subprocess.run(
                [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
                 "--user-data-dir=" + profile, "--dump-dom",
                 f"--virtual-time-budget={budget_ms}", "--window-size=1440,1100", url],
                stdout=fh, stderr=subprocess.DEVNULL, timeout=timeout)
        dom = open(out, encoding="utf-8", errors="ignore").read()
    finally:
        try:
            os.remove(out)
        except OSError:
            pass
    m = re.search(r'<pre id="out">([\s\S]*?)</pre>', dom)
    return m.group(1) if m else ""


def main() -> int:
    e2e = "--no-e2e" not in sys.argv

    # ---------------------------------------------------------------- 1. 静态口径
    print("【1】静态口径（试跑区与行模板）")
    html = open(POOL_HTML, encoding="utf-8").read()
    code = html

    check("试跑区有「样例」数输入（默认 50，上下限夹在 1~2000）",
          'id="ed-sample"' in code and 'value="50"' in code
          and "Math.min(2000" in code and "Math.max(1" in code)
    check("试跑请求带上样例数（不再是写死 sample: 10）",
          "sample: sample" in code and "sample: 10" not in code)
    check("样例数回写到输入框（用户输入越界时看到被夹后的值）",
          "$('#ed-sample').value = sample" in code)

    # 行模板：代码 / 名称是链接（进诊断页整页）；「K线」是按钮（弹窗预览，不跳页）
    check("有唯一的 K线链接构造入口 klineUrl()", code.count("function klineUrl") == 1)
    check("klineUrl 指向 /stock/ 且对代码做了转义",
          "return '/stock/' + encodeURIComponent(tsCode);" in code)
    check("代码列是链接（不是纯文本）",
          re.search(r'<td class="mono small"><a href="\$\{klineUrl\(x\.ts_code\)\}"', code) is not None)
    check("名称列也是链接（点名字进个股诊断整页）",
          re.search(r'<td><a href="\$\{klineUrl\(x\.ts_code\)\}" target="_blank">', code) is not None)
    check("「K线」是按钮、不再带 href（点击开弹窗预览，不是跳页）",
          re.search(r'<button class="btn sm" data-kline="\$\{esc\(x\.ts_code\)\}"', code) is not None
          and re.search(r'<a[^>]{0,90}>\s*K线\s*</a>', code) is None,
          "行里还有 <a>K线</a> 就说明还在跳页")
    check("新标签打开只剩代码/名称两条（K线 不再新开标签页）",
          code.count('target="_blank"') == 2, f"{code.count('target=\"_blank\"')} 处")
    check("结果表说明文案写清「弹窗预览不跳转 / 代码名进诊断页」",
          "弹窗预览" in code and "不跳转" in code)

    # 弹窗预览：**唯一入口**在 app.js（两个列表页共用），页面里不许自己拼 iframe
    appjs = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
    css = open(os.path.join(ROOT, "static", "css", "app.css"), encoding="utf-8").read()
    check("弹窗入口 window.klinePreview 在 app.js 里（两个列表页共用一份）",
          appjs.count("window.klinePreview = function") == 1)
    check("弹窗嵌的是诊断页预览模式（embed=1），不是第二份 K 线实现",
          "'embed=1'" in appjs and 'class="kl-frame"' in appjs and "<iframe" in appjs)
    check("同一时刻只留一个预览弹窗（不会越点越多）",
          "if (klineModal) klineModal.close();" in appjs)
    check("列表页只调入口、不自己拼 iframe",
          "window.klinePreview({" in code and "<iframe" not in code)
    # 弹窗右上角的「下一个」：核心实现在 app.js（两个列表页共用），页面只负责
    # 把「这一批票 + 行号」传进去 —— 名单必须是**当页这张表**，翻页才对得上屏幕顺序。
    check("「K线」把命中列表与行号一起交给弹窗（「下一个」= 表里的下一行）",
          'data-kline="${esc(x.ts_code)}" data-idx="${i}"' in code
          and "openKline(b.dataset.kline, s.name || '', testSamples, i)" in code)
    check("「下一个」排在右上角 ✕ 左边（在 .modal-acts 按钮组里）",
          "'下一个'" in appjs
          and "head.querySelector('.modal-acts').insertBefore(next" in appjs
          and '<div class="modal-acts">' in appjs)
    check("弹窗标题栏有按钮组样式（不然 space-between 会把标题挤到左边、按钮摊到中间）",
          ".modal-acts{" in css)
    check("翻页只换 iframe（不重建弹窗，避免遮罩与入场动画重放）",
          "frame.src = u.embed;" in appjs
          and "next.onclick = () => { if (idx + 1 < list.length) { idx += 1; paint(); } };" in appjs)
    check("没有下一只时按钮变灰（不让人空点）",
          "next.disabled = last;" in appjs)

    check("结果表放进可滚动容器 .tbl-wrap", '<div class="tbl-wrap"' in code)
    check("预览参数取自「试跑当时」的条件快照（改表单后点旧列表不会换口径）",
          re.search(r"testSpec = \{ base: f\.base, where: f\.conds", code) is not None
          and re.search(r"function previewParams\(\)[\s\S]{0,460}?testSpec", code) is not None)
    check("预览带上基准日（不带就会停在最新那天，跟列表不是同一天）",
          "if (testDate) p.trade_date = testDate;" in code)
    check("表头有「涨跌幅」（数据早就在响应里，之前没显示）",
          "testTh('涨跌幅', 'pct_chg', 'num')" in code)

    # 额外列 = **入池条件/排序表达式引用到的字段**摊到表上（与今日选股页同一套装）。
    # ⚠ 渲染三件套只允许有一份实现（app.js），本页自己再写一遍就是第二个口径。
    check("额外列走后端 extra_cols（前端不写死任何字段名）",
          "const cols = extraCols(d);" in code and "extraCell(x)" in code
          and "cols.map(c => testTh(c.label, c.key, 'num', c.key))" in code)
    check("额外列插在「换手%」与「评分」之间（与今日选股页同一位置）",
          re.search(r"\+ testTh\('换手%', 'turnover_rate', 'num'\)\s*\n\s*\+ extraHead\s*\n"
                    r"\s*\+ testTh\('评分'", code) is not None)
    check("额外列的渲染实现只有一份（app.js 的 extraCols/colText/colCls），本页不重复实现",
          "window.extraCols" in appjs and "window.colText" in appjs and "window.colCls" in appjs
          and "function colText" not in code and "function colCls" not in code)
    check("额外列按后端 fmt 渲染（num/big/mv/pct/price/int 分派在 app.js）",
          all(f"case '{f}'" in appjs for f in ("big", "mv", "pct", "price", "int"))
          and "window.fmt.num(v, c.dec)" in appjs)

    # 排序：服务端按「排序表达式」降序 + 表头可点（复用交易明细那套 CSS，不另造）
    check("试跑区有「排序表达式」输入框（与今日选股同一个口径）",
          'id="ed-sort"' in code and "排序表达式" in code)
    check("排序表达式进请求（留空不发，免得拿空串去校验）",
          "if (sortExpr) body.sort_expr = sortExpr;" in code
          and re.search(r"\$\('#ed-sort'\)\.value\.trim\(\)", code) is not None)
    check("结果卡写明「按什么排的」（别让人对着顺序猜）",
          "d.sort_expr ? '按 ' + d.sort_expr + ' 降序'" in code)
    check("式子算不出值时给提示（sort_note，不许静默按原序）",
          "d.sort_note" in code and "hintbox warn" in code)
    check("表头可排序：唯一的表头构造入口 testTh()",
          code.count("function testTh") == 1 and "data-sort=" in code)
    check("复用买卖策略页那套排序样式（.sortable / .sarrow，不另造一套）",
          "'sortable'" in code and '<span class="sarrow">' in code)
    check("换列时文本列升序、数值列降序（TEST_TEXT_COLS 决定）",
          "const TEST_TEXT_COLS = { ts_code: 1, name: 1, industry: 1 };" in code
          and "dir: TEST_TEXT_COLS[k] ? 1 : -1" in code)
    check("缺值永远沉底（点升序不会让一屏 — 冒到最前）",
          "return (na && nb) ? a.i - b.i : (na ? 1 : -1);" in code)
    check("「#」列可点 = 恢复服务端原顺序（点过别的列总得有回路）",
          "testTh('#', '__orig'" in code and "if (k === '__orig') testSort = { key: '', dir: -1 };" in code)
    check("「#」列不显 ▲/▼（那会让人以为按行号排）",
          re.search(r"if \(key === '__orig'\) \{[\s\S]{0,240}?sarrow", code) is None)
    check("点表头只重画结果表，不重新试跑（那要几分钟）",
          code.count("function paintTest") == 1
          and re.search(r"\$\$\('th\[data-sort\]', box\)\.forEach\(th => th\.onclick = \(\) => \{"
                        r"[\s\S]{0,300}?paintTest\(\);", code) is not None)
    check("新一次试跑把排序状态复位（不沿用上次点击）",
          "testSort = { key: '', dir: -1 };   // 新一次试跑" in code)
    check("「K线」的名单 = 屏幕当前顺序（弹窗「下一个」才跟列表对得上）",
          "testSamples = view;" in code)
    check("截断时提示还剩多少只未列出", "另有" in code and "只未列出" in code)
    check("旧文案「仅显示前 10 只样例」已清掉", "仅显示前 10 只样例" not in code)
    check("改动条件后把旧结果标为过期（#test-stale）",
          "test-stale" in code and "条件已改动" in code)
    check("过期提示挂在 markDirty 上（一改就出现）",
          re.search(r"function markDirty\(\)[\s\S]{0,420}?test-stale", code) is not None)
    check("重新试跑会清掉过期提示（结果渲染前先清空 #test-result）",
          re.search(r"\$\('#test-result'\)\.innerHTML = '';\s*\n\s*try \{", code) is not None)

    # ---- 周期（2026-10-10 需求「选股策略中也要有日k和周k的选择项，策略名称后面如果是
    #      周k则标记周」）。口径唯一：枚举在 `universe.FREQS`、后缀在 `universe.name_tag()`，
    #      页面只负责铺下拉与透传；判定（池子 weekly ⇒ 整条链周线）只在 `strategy.selection_cfg`。
    check("编辑器有周期下拉，选项与展示名都来自后端（页面不写死「日K / 周K」）",
          'id="ed-freq"' in code and "const FREQS = {{ freqs|tojson }};" in code
          and "fillSelect($('#ed-freq'), FREQ_ORDER.map" in code
          and '<option value="daily">日K</option>' not in code)
    check("周期下拉下方写明口径（周K = 整条链按周线跑，不是只把线换成周线）",
          'id="ed-freq-hint"' in code and "function syncFreqHint" in code
          and "整条链按周线跑" in code and "5 周均线" in code)
    check("列表项用后端给的 name_label（周K 后缀由后端出，前端不自己拼）",
          "esc(p.name_label || p.name)" in code
          and "p.timeframe === 'weekly' ? '（周K）'" not in code)
    check("试跑把周期一起送（universe.timeframe）—— 否则周K 策略会被悄悄按日线试跑",
          re.search(r"universe: \{ base: f\.base, where: f\.conds,"
                    r"\s*\n\s*filt: f\.filt, timeframe: f\.timeframe \}", code) is not None)
    check("试跑快照留下周期（弹窗预览 / 诊断页才按同一周期看）",
          re.search(r"testSpec = \{ base: f\.base, where: f\.conds,"
                    r"\s*\n\s*filt: f\.filt, timeframe: f\.timeframe \}", code) is not None
          and "const fq = (testSpec && testSpec.timeframe) || DEF_FREQ;" in code)
    check("预览参数带上周期（周K 策略的弹窗别按日K 画）",
          "freq: fq," in code
          and "config: JSON.stringify({ entry: where, timeframe: fq })" in code)
    check("「另存为」也带周期（那是显式列字段，漏了就丢）",
          "timeframe: f.timeframe, conds: f.conds" in code)
    check("页面写明周期跟着策略走（引用方的选股 / 回测 / 提醒一起变）",
          "引用它的选股 / 回测 / 提醒一并按周线算" in code)

    # 内联脚本语法：模板里改的是大段 JS，语法错整页都不会跑
    blocks = re.findall(r"<script(?![^>]*src=)[^>]*>([\s\S]*?)</script>", html)
    big = max(blocks, key=len) if blocks else ""
    js = os.path.join(tempfile.gettempdir(), "_pool_inline.js")
    with open(js, "w", encoding="utf-8") as fh:
        fh.write(big)
    if os.path.exists(NODE):
        r = subprocess.run([NODE, "--check", js], capture_output=True, text=True)
        check(f"pool.html 内联脚本语法（{len(big)} 字符）", r.returncode == 0,
              (r.stderr or "").strip()[:140])
    else:
        skip("pool.html 内联脚本语法", "找不到 node")

    check("探针页带 ?run=1 门禁（它会真发一次试跑）",
          os.path.exists(PROBE_HTML) and "params.get('run') !== '1'" in
          open(PROBE_HTML, encoding="utf-8").read())

    # ---------------------------------------------------------------- 2. 接口契约
    print("\n【2】接口契约（/api/universe/test）")
    try:
        spec = {"universe": {"base": SMALL_BASE, "where": ["close > 0"]},
                "sample": 3}
        r = post("/api/universe/test", spec)
        d = r.get("data") or {}
        hit = d.get("hit_count")
        smp = d.get("samples") or []
        check("试跑接口返回成功", bool(r.get("ok")), str(r.get("error") or ""))
        check("命中只数与样例分离（hit_count 是全部，samples 是被截断的）",
              isinstance(hit, int) and hit > 0 and len(smp) == min(3, hit),
              f"hit={hit} 样例={len(smp)}")
        need = {"ts_code", "name", "industry", "close", "pct_chg", "total_mv",
                "circ_mv", "turnover_rate", "score"}
        miss = sorted(need - set(smp[0].keys())) if smp else ["（没有样例）"]
        check("样例字段够前端渲染（含涨跌幅 / 市值 / 换手 / 评分）", not miss,
              ("缺 " + ",".join(miss)) if miss else ",".join(sorted(need)))

        # 额外列 = **入池条件引用到的字段** − 已在表上固定显示的列（口径 = `strategy.
        # referenced_fields`，与今日选股结果表同一份）。这条 `close > 0` 只碰固定列 →
        # 不该多出列（老界面的样子不变）。
        check("条件只用固定列时 extra_cols 为空（不多出列）",
              not (d.get("extra_cols") or []), str(d.get("extra_cols")))

        r2 = post("/api/universe/test", {"universe": {"base": SMALL_BASE,
                                                      "where": ["amount > 0", "ma20 > 0"]},
                                         "sample": 3})
        d2 = r2.get("data") or {}
        cols2 = d2.get("extra_cols") or []
        keys2 = [c["key"] for c in cols2]
        check("额外列 = 条件引用到的字段，顺序 = 条件里出现的先后",
              keys2 == ["amount", "ma20"], ",".join(keys2))
        check("已在表上固定显示的列不再出第二遍（否则一列占两格）",
              not ({"close", "pct_chg", "total_mv", "turnover_rate"} & set(keys2)),
              ",".join(keys2))
        check("额外列的形状就是 indicators.col_spec 的产出（表头/小数位/格式都由后端给）",
              bool(cols2) and all(set(c) == {"key", "label", "dec", "color", "fmt"}
                                  for c in cols2), str(cols2[:2]))
        smp2 = d2.get("samples") or []
        check("样例里真带了这些字段的值（不是只给列头不给数）",
              bool(smp2) and all(k in smp2[0] and smp2[0][k] is not None for k in keys2),
              str({k: (smp2[0].get(k) if smp2 else None) for k in keys2}))
        check("样例代码形状正确", all(re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", x["ts_code"] or "")
                                      for x in smp), ",".join(x["ts_code"] for x in smp))
        check("样例按代码升序（顺序稳定，不随批次抖动）",
              [x["ts_code"] for x in smp] == sorted(x["ts_code"] for x in smp))
        check("base_size 与池子规模一致", d.get("base_size") == 100, str(d.get("base_size")))

        r1 = post("/api/universe/test",
                  {"universe": {"base": SMALL_BASE, "where": ["close > 0"]},
                   "sample": 1})
        check("样例数真的被 honored（sample=1 → 只回 1 只）",
              len((r1.get("data") or {}).get("samples") or []) == 1,
              str(len((r1.get("data") or {}).get("samples") or [])))

        if smp:
            first = smp[0]["ts_code"]
            page = get("/stock/" + first)
            check(f"点进去的 K线页存在（/stock/{first} 返回 200 且含该代码）",
                  first in page and "个股诊断" in page, f"{len(page)} 字节")
            # 弹窗预览嵌的就是这一页的 ?embed=1：只隐藏非图表卡片，图表容器与脚本原样在
            emb = get("/stock/" + first + "?embed=1")
            check("预览模式 ?embed=1 隐藏非图表卡片、图表容器仍在、脚本照旧",
                  'class="embed"' in emb and "st-card-table{display:none}" in emb
                  and 'id="chart-stock"' in emb and "function buildOption()" in emb,
                  f"embed={len(emb)} 字节 / 正常={len(page)} 字节")
            check("正常打开没有 embed 类（预览模式不影响诊断页自己）",
                  'class="embed"' not in page and "st-card-table{display:none}" not in page)
            check("预览模式按 URL 参数取数（config/pool_id/trade_date/sel_only 都转给接口）",
                  "const EMBED_KEYS = ['strategy_id', 'config', 'pool_id', 'bars', 'freq', "
                  "'trade_date', 'sel_only']" in emb
                  # ⚠ 2026-10-09：这里原来断言 `"if (EMBED) { load(); return; }"` —— 那是**老版**
                  #   写法（早期 embed 直接触发一次 load）。现在实现改成 `detailUrl()` 里
                  #   `EMBED_KEYS.forEach(k => q.set(k, src.get(k)))` 逐个透传，功能一样、
                  #   写法变了 ⇒ 断言假红（产品没坏，是断言过时）。
                  and "EMBED_KEYS.forEach(k => { if (src.get(k)) q.set(k, src.get(k)); });" in emb)
            # 「只看选股条件」：试跑预览没绑定买卖策略 → 不算策略收益、不画买卖信号。
            # 不带这个参数时诊断页会拿默认买卖参数跑一遍单票回测，弹窗里冒出一张
            # 跟这批票毫无关系的「策略收益」卡（2026-09-20 用户点名）。
            cfgq = urllib.parse.quote(json.dumps(
                {"entry": ["close > 0"], "timeframe": "daily"}, ensure_ascii=False))
            base_q = f"/api/stock/{first}/detail?config={cfgq}&bars=30"
            sel = json.loads(get(base_q + "&sel_only=1")).get("data") or {}
            nor = json.loads(get(base_q)).get("data") or {}
            check("sel_only：不算策略收益（perf 为 null）", sel.get("perf") is None,
                  "perf=" + str(sel.get("perf"))[:40])
            check("sel_only：只留「选股命中」一档信号",
                  [x.get("key") for x in (sel.get("signals") or [])] == ["entry"],
                  ",".join(str(x.get("key")) for x in (sel.get("signals") or [])))
            check("sel_only：逐根明细里没有买卖点（买入/卖出全 0）",
                  bool(sel.get("rows")) and not any(
                      r.get("buy_trigger") or r.get("sell_signal") for r in sel["rows"]),
                  f"rows={len(sel.get('rows') or [])}")
            check("不带 sel_only 时照旧有策略收益、三档信号（普通诊断页没被改坏）",
                  bool(nor.get("perf")) and len(nor.get("signals") or []) == 3,
                  "signals=" + ",".join(str(x.get("key")) for x in (nor.get("signals") or [])))
    except Exception as e:
        check("试跑接口契约", False, f"{type(e).__name__}: {e}（服务起了吗？）")

    # --------------------------------------------------------------- 2c. 周期契约
    # 选股策略自带 `timeframe`（2026-10-10）：weekly 策略引用到哪，哪就整条链按周线跑。
    # ⚠ 只用 `/api/pools` 与 `selection_cfg` 验，不跑全市场选股（那要几分钟）。
    print("\n【2c】周期契约（选股策略自带日K / 周K）")
    NAME_W = "ZZ·回归·周K选股策略"
    FILT_W = {"exclude_st": True, "exclude_bj": True, "min_list_days": 60}
    try:
        from core import market as _mk          # noqa: E402
        from core import strategy as _st        # noqa: E402
        from core import universe as _uni       # noqa: E402

        def _pools():
            return json.loads(get("/api/pools")).get("data") or []

        def _del(pid):
            urllib.request.urlopen(urllib.request.Request(
                f"{BASE}/api/pools/{pid}", method="DELETE"), timeout=30).read()

        pools = _pools()
        lacked = [p for p in pools
                  if not {"timeframe", "timeframe_label", "name_label"} <= set(p)]
        check("/api/pools 的描述带周期与名字后缀（列表 / 下拉都靠它）",
              not lacked, f"{len(pools)} 条策略，缺字段 {len(lacked)} 条")
        check("日K 策略的名字**不带**后缀（只标周K）",
              all(not (p.get("name_label") or "").endswith("（周K）")
                  for p in pools if p.get("timeframe") == "daily"),
              str([p["name"] for p in pools if (p.get("name_label") or "").endswith("（周K）")][:3]))
        check("展示名与后端枚举同源（daily→日K / weekly→周K）",
              all(p.get("timeframe_label") == {"daily": "日K", "weekly": "周K"}[p["timeframe"]]
                  for p in pools))

        for p in pools:                        # 先清同名残留，免得撞 uk_name 报 500
            if p.get("name") == NAME_W:
                _del(p["id"])
        made = post("/api/pools", {"name": NAME_W, "note": "回归用，跑完即删",
                                   "base": "all", "timeframe": "weekly",
                                   "conds": ["close > 0"], "filt": FILT_W}).get("data") or {}
        nid = made.get("id")
        check("周K 策略能存进去，描述回读就是 weekly",
              bool(nid) and made.get("timeframe") == "weekly",
              f"id={nid} timeframe={made.get('timeframe')}")
        check("周K 策略的名字后带「（周K）」后缀（用户要的那个标记）",
              (made.get("name_label") or "").endswith("（周K）"), str(made.get("name_label")))

        cw = _st.selection_cfg({"_pool": {"pool_id": nid}})
        check("引用周K 策略 ⇒ 整条链抬到周线（cfg['timeframe'] 变 weekly）",
              cw["timeframe"] == "weekly", cw["timeframe"])
        check("周K 策略的条件按周线求值（`w_` 前缀会被去掉 —— 那根本身就是一个完整周）",
              bool(cw["entry"]) and all("w_" not in e for e in cw["entry"]), str(cw["entry"]))
        check("池子条件被抬成 entry（选股/回测吃的是同一份）",
              cw["entry"] == ["close > 0"], str(cw["entry"]))

        back = post("/api/pools", {"id": nid, "name": NAME_W, "base": "all",
                                   "timeframe": "daily", "conds": ["close > 0"],
                                   "filt": FILT_W}).get("data") or {}
        _uni.invalidate_pool(nid)              # 同进程的 3 秒缓存（HTTP 改库是在服务进程里失效的）
        check("改回日K ⇒ 周期跟着回日线（不是存进去就固化）",
              _st.selection_cfg({"_pool": {"pool_id": nid}})["timeframe"] == "daily")
        check("改回日K 后名字后缀也去掉（日K 不标 —— 用户原话）",
              back.get("name_label") == NAME_W, str(back.get("name_label")))

        # 试跑也走同一个周期（用「最近一个周收盘日」，免得回归正好在周中/周末跑就假红）
        wk_day = _mk.latest_trade_date()
        if not _mk.is_week_close_day(wk_day):
            wk_day = _mk.last_week_close_day(wk_day)
        r_w = post("/api/universe/test", {"universe": {"base": SMALL_BASE,
                                                       "where": ["close > 0"],
                                                       "timeframe": "weekly"},
                                          "trade_date": wk_day, "sample": 3})
        dw = r_w.get("data") or {}
        check("试跑也按周期走（周K 试跑的 describe.timeframe = weekly）",
              bool(r_w.get("ok")) and (dw.get("describe") or {}).get("timeframe") == "weekly",
              str(r_w.get("error") or (dw.get("describe") or {}).get("timeframe")))
        check("周K 试跑真出了样例（周收盘日那天按周线跑得动）",
              bool(dw.get("samples")),
              f"样例 {len(dw.get('samples') or [])} 只 @{dw.get('trade_date')}")

        for p in _pools():
            if (p.get("name") or "").startswith(NAME_W):
                _del(p["id"])
        check("收尾：临时策略删干净（不给库里留垃圾）",
              not [p for p in _pools() if NAME_W in (p.get("name") or "")])
    except Exception as e:  # noqa: BLE001
        check("周期契约", False, f"{type(e).__name__}: {e}")

    # ---------------------------------------------------------- 2d. 默认过滤项契约
    print("\n【2d】默认过滤项（新开一条选股策略时的勾选默认值）")
    try:
        import core.universe as _uni
        cfgd = json.loads(get("/api/config")).get("data") or {}
        fd = cfgd.get("filt_defaults") or {}
        check("过滤项默认值由 /api/config 下发、且与 universe.DEFAULT_FILT 逐字段相等（唯一来源）",
              fd == dict(_uni.DEFAULT_FILT), f"/api/config={fd}")
        check("剔除科创板（exclude_kcb）默认勾选",
              fd.get("exclude_kcb") is True, f"exclude_kcb={fd.get('exclude_kcb')!r}")
        m = re.search(r"FILT_DEF\s*=\s*\{[^}]*?exclude_kcb:\s*(true|false)", code)
        check("页面兜底 FILT_DEF 与后端默认一致（接口取不到时不分叉）",
              bool(m) and m.group(1) == "true", m.group(0) if m else "没找到 FILT_DEF")
        check("页面默认不再抄一份「不剔科创」（writeFilt 缺键时才用兜底）",
              "exclude_kcb: false" not in code)
        from core import pool as _pool
        check("内置样例源码里的 filt 也是 True（「补齐内置」别装回旧值）",
              _pool.BUILTIN_POOLS[0]["filt"].get("exclude_kcb") is True,
              str(_pool.BUILTIN_POOLS[0]["filt"]))
    except Exception as e:  # noqa: BLE001
        check("默认过滤项契约", False, f"{type(e).__name__}: {e}")

    # --------------------------------------------------------------- 2b. 排序契约
    print("\n【2b】排序契约（sort_expr 真在求值，不是只认字段名）")

    def _run(sort_expr=None, sample=60):
        spec = {"universe": {"base": SMALL_BASE, "where": ["close > 0"]},
                "sample": sample}
        if sort_expr:
            spec["sort_expr"] = sort_expr
        return post("/api/universe/test", spec)

    def _mono(vals):
        """非递增违例数（缺值跳过 —— 后端把缺分沉到末尾，不该算违例）。"""
        return sum(1 for a, b in zip(vals, vals[1:])
                   if a is not None and b is not None and b > a + 1e-9)

    try:
        d0 = (_run() or {}).get("data") or {}
        s0 = d0.get("samples") or []
        c0 = [x["ts_code"] for x in s0]
        check("不传排序表达式：sort_expr 为空、顺序仍是代码升序（老行为不变）",
              d0.get("sort_expr") == "" and len(c0) > 5 and c0 == sorted(c0),
              f"sort_expr={d0.get('sort_expr')!r} 前3={c0[:3]}")
        check("不传表达式时评分一律为空（不许凭空造分）",
              all(x.get("score") is None for x in s0), str([x.get("score") for x in s0[:3]]))
        check("不传表达式时不给 sort_note（没式子不算「失真」）", not d0.get("sort_note"))

        d1 = (_run("turnover_rate") or {}).get("data") or {}
        s1 = d1.get("samples") or []
        v1 = [x.get("turnover_rate") for x in s1]
        sv1 = [x.get("score") for x in s1]
        check("传 turnover_rate：回显 sort_expr（结果卡就按它写「按…降序」）",
              d1.get("sort_expr") == "turnover_rate", str(d1.get("sort_expr")))
        check("传 turnover_rate：顺序真按它降序（不是只认字段名）",
              bool(v1) and _mono(v1) == 0, f"违例 {_mono(v1)} 处 · 前6={v1[:6]}")
        check("评分列的值 == 该字段值（排序与评分就是同一个式子）",
              all((a is None) == (b is None) and (a is None or abs(a - b) < 1e-6)
                  for a, b in zip(sv1, v1)))
        check("评分真有值（整列 — 就是「填了没反应」那种最误导的失败）",
              any(x is not None for x in sv1), str(sv1[:4]))

        # ma20 不在试跑的固定 only 集里：它若没被 only 带上，评分恒空 → 顺序悄悄退回原序
        d2 = (_run("ma20") or {}).get("data") or {}
        sv2 = [x.get("score") for x in (d2.get("samples") or [])]
        check("传 ma20（不在固定 only 集里）：评分照样有值（only= 跟着表达式扩）",
              any(x is not None for x in sv2), str(sv2[:4]))
        check("传 ma20：顺序非递增", _mono(sv2) == 0, f"违例 {_mono(sv2)} 处")

        d3 = (_run("-total_mv") or {}).get("data") or {}
        sv3 = [x.get("score") for x in (d3.get("samples") or [])]
        check("传 -total_mv（取反 = 小的在前）：顺序非递增且值为负",
              _mono(sv3) == 0 and any(x is not None and x < 0 for x in sv3), str(sv3[:4]))

        r4 = post("/api/universe/test", {
            "universe": {"base": SMALL_BASE, "where": ["close > 0"]},
            "sample": 5, "sort_expr": "close >"})
        check("式子语法错 → 400 报 [排序表达式]（不能静默按空评分跑完一屏乱序）",
              r4.get("ok") is False and "[排序表达式]" in (r4.get("error") or ""),
              str(r4.get("error"))[:80])
        r5 = post("/api/universe/test", {
            "universe": {"base": SMALL_BASE, "where": ["close > 0"]},
            "sample": 5, "sort_expr": "no_such_field_xyz"})
        check("式子用了不存在的字段 → 400 报未知字段",
              r5.get("ok") is False and "未知字段" in (r5.get("error") or ""),
              str(r5.get("error"))[:80])
        # 不变量：sort_note 非空 ⟺ 命中股评分全空（「算不出值」必须留声，不许哑着）
        for tag, dd in (("turnover_rate", d1), ("ma20", d2)):
            ss = [x.get("score") for x in (dd.get("samples") or [])]
            note = bool(dd.get("sort_note"))
            check(f"sort_note 与「全算不出值」严格对应（{tag}）",
                  note == (bool(ss) and all(x is None for x in ss)), f"note={note}")
    except Exception as e:
        check("排序契约", False, f"{type(e).__name__}: {e}（服务起了吗？）")

    # ---------------------------------------------------------------- 3. 浏览器实测
    if e2e and CHROME:
        print("\n【3】浏览器实测（真点一次「看当日命中」）")
        import shutil
        profile = os.path.join(tempfile.gettempdir(), "_pool_probe_profile")
        shutil.rmtree(profile, ignore_errors=True)
        try:
            body = chrome_probe_out(BASE + PROBE, 240000, 600, profile)
            data = None
            m = re.search(r"\{[\s\S]*\}", body)
            if m:
                try:
                    data = json.loads(m.group(0))
                except Exception:
                    data = None
            if not isinstance(data, dict) or not data.get("checks"):
                check("探针返回可解析结果", False, (body or "（探针没输出）")[:200].replace("\n", " "))
            else:
                check("探针跑完了（不是半路超时）", bool(data.get("done")),
                      "done=" + str(data.get("done")))
                for c in data["checks"]:
                    check("探针 · " + c["name"], bool(c.get("ok")), c.get("extra", ""))
        except Exception as e:
            check("浏览器实测执行完成", False, f"{type(e).__name__}: {e}")
        finally:
            shutil.rmtree(profile, ignore_errors=True)
    else:
        skip("浏览器实测", "无 Chrome 或指定了 --no-e2e")

    print(f"\n{'=' * 62}\n选股策略试跑回归：{PASS} 通过 / {FAIL} 失败 / {SKIP} 跳过")
    if FAILS:
        print("失败项：")
        for x in FAILS:
            print("  -", x)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
