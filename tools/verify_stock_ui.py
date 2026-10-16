#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
个股诊断「K线 + 下挂指标」回归（静态口径 + 接口契约 + 无头 Chrome 实测拖动/切换）

为什么需要它：这一版把 K线、MACD、KDJ 从「三张各画各的图」改成「一个实例、多个 grid
共享一条时间轴」，并让下挂指标可切换。三件事都读文本读不出来：

  · 「拖 K 线时间轴、下挂格跟着走」是不是真的 —— dataZoom 里写了 xAxisIndex:[0,1,2]
    不等于渲染出来是对齐的，必须真拖一下、再逐个 grid 比对同一日期映射到的像素；
  · 下挂指标切换是不是真的换了线（还是切了没反应 / 切完图例错位）；
  · 指标选项与字段是不是只有后端一个来源 —— 前端要是自己抄一份字段表，策略把
    cci 调成 20 之后就会去画不存在的 cci14，画出来是一条空线，静态看模板完全正常。

所以本脚本分三段：

  【1】静态口径   —— 页面只剩一个图表容器、两个下拉、缩放/光标覆盖全部轴、参考线不挂
                      在数据 series 上（否则会被图例点掉）、图表代码里不许出现指标字段
                      字面量、调色板 key 与后端目录一致、内联脚本语法过不过；
                      K 线上方 4 张卡片第 3 张是基本面（前端不自己算市值单位）、
                      第 4 张是策略收益（主数/小项/正负号/涨跌色都走后端，不在 JS 里拼
                      `%`/`笔`）；策略信号按后端 signals 取字段/侧别/形状（不写死 entry/buy/sell）；
                      周期下拉只有 daily/weekly、换周期**重新取数**、换策略只拨周期不自动查询、
                      回车只绑在「查询条件」卡上**一处**（逐框绑会漏下拉、绑 document 会劫持
                      K 线卡上的回车）
  【2】接口契约   —— /api/stock/<code>/detail 返回 catalog；目录里每个字段在 rows 里
                      都真的存在（对三只不同板块的票）；把策略参数改掉（cci=20 /
                      rsi=[6,24] / atr=7 / vma=[5]）后目录跟着变、字段也跟着变；本地
                      没有资金流向的票，mf 组要被标成 empty 让前端隐藏而不是画空线；
                      返回的 fundamental 拿 daily_basic 原值独立重算对拍，
                      并单测「万元→亿/万亿」「缺数据给 —」这两条口径；
                      signals 里每档的根数 == 窗口内真值数，且「买入触发」未设条件时
                      必须标成不画并说明原因（否则每根都是 True，会铺满箭头）；
                      perf 则**绕开 single_stock_perf、自己加载行情直接调 run_backtest**
                      对拍（收益/回撤/胜率/笔数 1e-6 内相等）、「买入持有」拿窗口首尾
                      收盘价独立复算、0 交易与「有命中但没触发买入」两种原因必须区分、
                      改风控参数后必须重算；
                      **freq=weekly** 另外验：周K 的 close/open == 该周最后/第一个交易日的
                      日线复权价（拿日线视图的行数据独立复算，证明是同源不是另算）、
                      最后一根是已收盘的完整周（周中不画半周线）、w_ 前缀在周K上被去掉
                      （不翻译会恒 False）、周线策略不给 freq 时默认看周K、收益按周K重算且
                      与引擎对拍、基本面另取 daily_basic、0 交易的原因按「一根周K」说；
                      **查询框**另外验：完整代码 / 6 位数字 / **名称**三种写法认到同一只、
                      重名（输「银行」）不瞎猜而是报错带候选、乱输入给「没找到」不是 500
  【3】浏览器实测 —— 真 MouseEvent 拖时间轴（滑块手柄→滑块窗口→主图平移，三选一），
                      拖完比对三个 grid 的坐标；切换下拉验 series 真的换了；关到只剩
                      K线一格仍能缩放；量柱按当日阴阳着色；指标格 hover 出 tooltip；
                      4 张卡片跟后端 fundamental / perf 逐字一致（收益区间 == 图上窗口、
                      换策略点查询后主数与 4 个小项都重算）；信号标记锚点/偏移/图例核对，
                      并**真切一次策略 + 点「查询」**看信号与收益一起跟着换；
                      **切周线**：选周线策略时周期自动跟着拨（用户不必记口径）、点查询后
                      横轴/蜡烛/信号标记换成周线那份、每根周K 独立复算对拍日线、
                      下挂格仍与主轴同步、标签与悬停里写明「条件重算」、再手动切回日线；
                      查询框输重名的前半段（「平安」）列出候选、点候选即填代码并查询、
                      完整名称/完整代码都不弹（弹一条或一串都是挡路）

跑法：
    C:/Users/ming/.workbuddy/binaries/python/envs/stockmining/Scripts/python.exe tools/verify_stock_ui.py
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
PROBE = "/static/_probe_stock.html?run=1"
PROBE_FREQ = "/static/_probe_freq.html?run=1"      # 第二支：K线周期分段切换（真加载 + 真点）
STOCK_HTML = os.path.join(ROOT, "templates", "stock.html")
PROBE_HTML = os.path.join(ROOT, "static", "_probe_stock.html")
NODE = r"C:\Users\ming\.workbuddy\binaries\node\versions\22.22.2-3\node.exe"

# 三只票覆盖三个板块（主板 / 创业板 / 科创板），顺带覆盖涨跌停比例不同的分支
CODES = ["600519.SH", "300750.SZ", "688111.SH"]
DETAIL = "/api/stock/%s/detail"

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


def _elem_html(html: str, marker: str) -> str:
    """从 `marker`（例如 `id="st-freq-seg"`）所在的那个元素的 **开标签起，
    切到它的闭合标签** —— 用栈配对数标签，而不是「切到下一个已知 id」。

    ⚠ 为什么需要它：标题栏里并排排着好几个控件，用「切到下一个 id 为止」的写法，
    新插一个控件就会把它的 `<button>` 一起数进来（2026-10-09 加「幅度」分段按钮
    时，`#st-freq-seg` 的「正好 2 个按钮」断言就被数成 5 个）。
    """
    i = html.find(marker)
    if i < 0:
        return ""
    start = html.rfind("<", 0, i)          # 这个元素的开标签起点
    if start < 0:
        return ""
    m = re.match(r"<(span|div)\b", html[start:start + 8])
    if not m:
        return html[start:html.find(">", i) + 1]
    tag = m.group(1)
    # 用正则逐个数 <tag> 与 </tag>，栈深度归零即到元素末尾
    pat = re.compile(r"<(/?)" + tag + r"\b", re.I)
    depth = 0
    for mm in pat.finditer(html, start):
        if mm.group(1):
            depth -= 1
            if depth == 0:
                end = html.find(">", mm.end())
                return html[start:end + 1]
        else:
            depth += 1
    return html[start:]


def get_json(path: str, timeout: int = 180) -> dict:
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode("utf-8", "replace"))


def get(path: str, timeout: int = 60) -> str:
    return urllib.request.urlopen(BASE + path, timeout=timeout).read().decode("utf-8", "replace")


def post_json(path: str, payload: dict, timeout: int = 180) -> dict:
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode("utf-8", "replace"))


def delete_json(path: str, timeout: int = 60) -> dict:
    req = urllib.request.Request(BASE + path, method="DELETE")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# ---------------------------------------------------------------------------
# 买卖策略夹具
# ---------------------------------------------------------------------------
# ⚠ 别写死 `strategy_id=1` / `=2`：2026-09-18 两策略分离后，迁移脚本把旧的
#   「选股 + 买卖合体」策略整批删了（只剩 2 个纯买卖样例），行号跟本不存在；
#   而且内置行在界面上改得动 —— 按行号取就是假红的来源（见 MEMORY 硬规矩 16）。
#   这里**自己建两条临时策略**，跑完删掉；周线那条顺便给探针【3】用
#   （探针要找一个 `timeframe=weekly` 的策略来验「周期跟着策略口径走」）。
SID_DAILY = None     # 日线口径（原 strategy_id=1 的角色）
SID_WEEKLY = None    # 周线口径 + w_ 过滤（原 strategy_id=2 的角色）


def install_probe_strategies() -> None:
    global SID_DAILY, SID_WEEKLY

    def mk(name: str, cfg: dict):
        r = post_json("/api/strategies", {"name": name, "config": cfg})
        return (r.get("data") or {}).get("id")

    SID_DAILY = mk("__probe_stock_daily__", {
        "buy_trigger": ["open > ref(close, 1)"], "sell_signal": ["open < ref(close, 1)"]})
    SID_WEEKLY = mk("__probe_stock_weekly__", {
        "timeframe": "weekly",
        "weekly_filter": ["w_close > w_ma20"],
        "buy_trigger": ["open > ref(close, 1)"], "sell_signal": ["open < ref(close, 1)"]})


def drop_probe_strategies() -> None:
    for sid in (SID_DAILY, SID_WEEKLY):
        if sid:
            try:
                delete_json("/api/strategies/%d" % int(sid))
            except Exception:  # noqa: BLE001  清理失败不影响结论
                pass


def pool_spec_of(detail: dict) -> dict | None:
    """把接口回传的「本次真正生效的选股策略」还原成 cfg["_pool"]。

    独立对拍必须挂在**同一份**条件上，否则一边有池一边没有，比的不是一件事
    （2026-09-18 实测：引擎 -15.82%/56 笔 vs 卡片 -9.18%/26 笔，看着像引擎坏了）。
    ⚠ 2026-10-11 起后端**不再兜底**（原来是「不带选股条件就自动挂 BOLL下轨低吸」）：
    接口没回 `where` 就是真没有，这里返回 None = 两边都不带池，仍然对得上。
    只用 base/where/filt 重建，**不带 pool_id** —— 免得再回查库里那份。
    """
    p = (detail or {}).get("pool") or {}
    if p.get("kind") != "cond" or not p.get("where"):
        return None
    return {"base": p.get("base"), "where": list(p.get("where") or []),
            "filt": p.get("filt")}


CHROME = None
for _c in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
           r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
           os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe")):
    if os.path.exists(_c):
        CHROME = _c
        break


def chrome_probe_out(url: str, budget_ms: int, timeout: int, profile: str) -> str:
    """渲染探针页并返回 <pre id="out"> 里的文本。"""
    out = os.path.join(tempfile.gettempdir(), "_stock_probe_dom.html")
    try:
        with open(out, "w", encoding="utf-8") as fh:
            subprocess.run(
                [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
                 "--user-data-dir=" + profile, "--window-size=1440,1400",
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


def run_probe_checks(body: str, tag: str) -> None:
    """把探针页 <pre id="out"> 里的 JSON 逐条变成断言（两支探针共用同一套读法）。"""
    data = None
    m = re.search(r"\{[\s\S]*\}", body or "")
    if m:
        try:
            data = json.loads(m.group(0))
        except Exception:  # noqa: BLE001
            data = None
    if not isinstance(data, dict) or not data.get("checks"):
        check(f"{tag}探针返回可解析结果", False, (body or "（探针没输出）")[:200].replace("\n", " "))
        return
    check(f"{tag}探针跑完了（不是半路超时）", bool(data.get("done")),
          "done=" + str(data.get("done")))
    for c in data["checks"]:
        check(f"{tag} · " + c["name"], bool(c.get("ok")), c.get("extra", ""))


def main() -> int:
    e2e = "--no-e2e" not in sys.argv

    # 临时买卖策略夹具（替代旧的 strategy_id=1 / =2；周线那条也给探针【3】用）
    install_probe_strategies()
    if not (SID_DAILY and SID_WEEKLY):
        print("！！ 建临时买卖策略失败（服务没起？），后面按行号取策略的段会全红")

    # ---------------------------------------------------------------- 1. 静态口径
    print("【1】静态口径（单实例多栅格 / 指标目录唯一来源）")
    html = open(STOCK_HTML, encoding="utf-8").read()
    # 图表侧的「口径」现在有两处载体：页面模板 + 共用的 app.js
    #（坐标轴合并 / 图例样式 / 下挂指标线色与数值格式都在 app.js —— 交易复盘图也用同一份）
    appjs = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()

    check("只剩一个图表容器 #chart-stock（旧的 chart-macd / chart-kdj 已合并）",
          html.count('id="chart-stock"') == 1
          and "chart-macd" not in html and "chart-kdj" not in html)
    check("容器用 .chart.stk（高度由 JS 按格数算，CSS 里只是首帧兜底）",
          'class="chart stk" id="chart-stock"' in html and ".chart.stk{" in
          open(os.path.join(ROOT, "static", "css", "app.css"), encoding="utf-8").read())
    check("两个下挂指标下拉放在卡片标题栏里",
          'class="ind-picks"' in html and 'id="st-ind-a"' in html and 'id="st-ind-b"' in html
          and html.index('class="card-head"') < html.index('id="st-ind-a"'))
    check("筹码区是 g2 双列（股东户数 / 融资融券），增减持单独一张卡",
          html.count('class="grid g2"') == 1
          and 'id="chart-holder"' in html and 'id="chart-margin"' in html
          and 'id="ch-trade-table"' in html
          and html.index('id="st-result"') < html.index('id="chart-holder"') < html.index('id="st-table"'))
    # 两融图上叠价格折线（2026-10-10 需求「融资融券图表上加上 k 线价格折线图，按收盘价算」）。
    # ⚠ 必须挂**第 3 根轴**：融资余额是百亿级、融券/净买入是亿级、价格是元级，
    #   挤同一根轴的话后两者会被压成贴着 0 的直线，等于没画。
    # ⚠ 右侧**只能有一根带 name 的轴**（两行 name 都在轴顶部，水平方向必然叠字），
    #   所以原来的「融券/净买入(亿)」整根隐藏、把名字让给「收盘价(元)」。
    check("两融图叠了价格折线（收盘价），且挂在第 3 根轴上（三样东西量级差太远，同轴等于没画）",
          "name:'收盘价', type:'line', yAxisIndex:2, data:s.map(x => x.close)" in html
          and "name:'收盘价(元)', position:'right'" in html
          # ⚠ 只查「轴名写法」，别查纯文本 —— 函数头注释里正解释着为什么要把这根轴隐藏
          and "name:'融券/净买入(亿)'" not in html,
          "价格线挂 yAxisIndex=2 · 右轴 name='收盘价(元)' · 右侧只有一根带名轴")

    # K 线图**顶边**的股东增减持 ± 标记（2026-10-10 需求「股东增减持也画在 k 线上，
    # 用 + 和 - 代表增持和减持，在 k 线图的顶边展示」）。
    # ⚠ 实现要点：**不新增 grid**，而是在 K 线那一格里**另加一根 0~1 的隐形 y 轴**
    #   （`gridIndex:0`，且按约定 push 在最后）—— 这样 K 线 / 各下挂格的 gridIndex、
    #   xAxisIndex 一个都不用动，「拖主轴、下挂格跟着走」的联动不会被碰坏；
    #   标记的 y 就是"离顶边多远"，与价格无关，涨跌都不跑位。
    check("增减持 ± 标记画在 K 线那格的顶边：同格隐形轴（0~1，gridIndex 仍是 0），不新增栅格",
          "const TRADE_UP = '增持', TRADE_DN = '减持';" in html
          and "yAxes.push(withBase({ type:'value', gridIndex:0, min:0, max:1," in html
          and "const tyIdx = yAxes.length;" in html
          and "type:'scatter', xAxisIndex:0, yAxisIndex:tyIdx" in html,
          "隐形轴挂在 grid 0 · 标记 series 走 xAxisIndex:0 / yAxisIndex:tyIdx")
    check("± 标记的加减号与涨跌色配套（增持红 + / 减持绿 −），没数据就不摆这两条 series",
          "formatter:sym" in html and "mk(TRADE_UP, tms.up, UP, '+')" in html
          and "mk(TRADE_DN, tms.dn, DOWN, '-')" in html
          and "if (tms.up.length || tms.dn.length)" in html)
    check("标记的行号由后端给（前端不拿日期去 rows 里 indexOf —— 那天不是交易日就没得对）",
          "tradeMarksAll().filter(m => m.idx === i)" in html
          and "curChips || {}).trade) || {}).marks" in html)
    check("tooltip 里用文字复述当根的增减持（标记只有十几像素，容易看漏）",
          "股东${esc(m.dir_text)} ${m.count} 条 · ${esc(m.vol_text)}" in html)
    try:
        from core import chips as _chk
        check("增减持标记的口径只有一处实现（core.chips.trade_marks），卡片与图上共用",
              hasattr(_chk, "trade_marks")
              and "trade_marks(code, bar_dates, end)" in open(
                  os.path.join(ROOT, "core", "chips.py"), encoding="utf-8").read(),
              "core/chips.py: trade_marks()")
    except Exception as e:  # noqa: BLE001
        check("增减持标记的口径只有一处实现（core.chips.trade_marks）", False,
              f"{type(e).__name__}: {e}")

    # K 线上方的卡片：固定 3 张（股票 / **开盘形态概率** / 基本面）+ 条件渲染的第 4 张「策略收益」。
    # ⚠ 2026-09-20 起第 4 张是**有 perf 才摆**的：试跑 / 今日选股那些没绑定买卖策略的入口
    #   后端返回 perf=null，就只摆 3 张、栅格从 g4 降成 g3（不留空槽）。
    # ⚠ 2026-10-09 起第 2 张**不再是「最新收盘」**，而是「开盘形态概率」——
    #   原来那个收盘读数并进了「股票」卡的 `.sub`（`#st-head-close`）。
    head = html[html.index("const fund = r.fundamental"):html.index("fillIndicatorPicks();")]
    fixed = head[head.index("$('#st-head').innerHTML"):]
    order = [x.strip() for x in re.findall(r'<div class="label">([^<]*)', fixed)]
    check("K线上方固定 3 张卡片，第 3 张是「基本面」（顺序：股票 → 开盘形态概率 → 基本面）",
          fixed.count('<div class="stat') == 3
          and order[0] == "股票" and order[1].startswith("开盘形态概率")
          and order[2].startswith("基本面"), " → ".join(order))
    check("第 2 张是 #st-head-open（开盘形态概率），它是 stat 卡而不是 row2 的普通卡",
          'id="st-head-open"' in fixed and fixed.count('id="st-head-open"') == 1)
    perf_blk = head[:head.index("$('#st-head').innerHTML")]
    check("第 4 张「策略收益」卡是条件渲染（perf 有才摆，没绑定买卖策略就不摆）",
          perf_blk.count('<div class="stat') == 1 and "策略收益" in perf_blk
          and "if (perf) {" in perf_blk and "perfCard" in perf_blk)
    check("没有收益卡时栅格跟着降（g4 → g3，第 4 列不留空槽）",
          "'grid ' + (perf ? 'g4' : 'g3')" in head)
    check("原「最新收盘」读数并进「股票」卡（#st-head-close 在 stat accent 里）",
          'id="st-head-close"' in fixed
          and re.search(r'class="stat accent"[\s\S]{0,400}id="st-head-close"', fixed) is not None)
    check("页面上不留旧的「最新收盘」独立卡（不再有 view.last_label 当卡片标题）",
          "<div class=\"label\">${esc(view.last_label)}" not in html
          and "最新周K收盘" not in html)
    check("基本面卡片按后端 items 铺格子（标签+数值成对，走 .fund 网格）",
          'class="fund"' in head and "fund.items" in head and "${esc(i.label)}" in head
          and "${esc(i.text)}" in head)
    check("第 3 张卡片不再写 BOLL（位置已让给基本面）", "BOLL" not in head)
    check("前端不自己算市值单位（「万元→亿」只在 core.indicators 里做一次）",
          "1e4" not in head and "万元" not in head and "toFixed(2) + '亿'" not in head)
    check("基本面数据挂了探针钩子（供浏览器段逐字比对）", "fund: () => curFund" in html)
    check("基本面卡片的网格样式在 CSS 里（两列三行，跟旁边 stat 卡齐平）",
          ".stat .fund{display:grid;" in open(os.path.join(ROOT, "static", "css", "app.css"),
                                              encoding="utf-8").read())

    # 策略收益卡片：主数(hero)/小项(items)/正负号/涨跌色全部由 core.backtest.perf_snapshot 给，
    # 前端只铺格子 —— 否则「±」「%」「N 笔」这些口径会在 JS 里再写一遍
    check("策略收益卡片走后端 perf（hero 主数 + items 小项 + badge 口径）",
          "r.perf" in head and "perf.hero" in head and "perf.items" in head
          and "${esc(hero.text)}" in head and "${esc(hero.cls" in head
          and "${esc(i.text)}" in head and "perf.badge" in head)
    check("收益卡片不自己拼百分号/笔数（格式只在 core.backtest 里拼一次）",
          not re.search(r"toFixed\(2\)\s*\+\s*'%'", head) and "' 笔'" not in head
          and " 笔'" not in head and "toFixed(1) + '%'" not in head)
    check("三档信号数并入收益卡片下方小字（不再单占一张卡）",
          "命中 ${hits} · 买入 ${buys} · 卖出 ${sells}" in head)
    check("收益卡片带区间/口径提示（鼠标悬停能看到区间与说明）",
          "perf.range.start" in head and "perfTip" in head and 'title="${esc(perfTip)}"' in head)
    check("收益数据挂了探针钩子（供浏览器段逐字比对）", "perf: () => curPerf" in html)

    # 周期（日线 / 周线）：前端只管「把选中的周期发出去 + 把后端给的说明摆出来」。
    # 周K 是另一份面板（另一次请求），所以它与「切下挂指标只重画」必须分开写；
    # 而「周K 的收盘该叫什么」「这是不是原策略」一律来自后端 view，不许在 JS 里另写一套。
    frq = html[html.index('id="st-freq"'):html.index('id="st-bars"')]
    check("查询条件里有「周期」下拉（日线 / 周线两个取值）",
          'id="st-freq"' in html and frq.count("<option") == 2
          and 'value="daily"' in frq and 'value="weekly"' in frq,
          " ".join("%s=%s" % (v, t) for v, t in re.findall(r'<option value="(\w+)">([^<]+)', frq)))
    check("查询时把周期一起发给后端（换周期要重新取数，不是前端把日K画稀）",
          "'&freq=' + $('#st-freq').value" in html)
    check("改周期立刻重新查询（与「切下挂指标只重画」是两条路径）",
          "$('#st-freq').addEventListener('change', () => { freqTouched = true; load(); })" in html)
    _stchg = html[html.index("$('#st-strategy').addEventListener"):]
    _stchg = _stchg[:_stchg.index("\n")]
    check("换策略只把周期拨到该策略的口径上，**不**自动查询（查不查由「查询」按钮定）",
          "syncFreqFromStrategy" in _stchg and "load()" not in _stchg, _stchg.strip())
    check("周期标签的文字全部来自后端 view（不写死「周K」/「条件重算」）",
          'id="st-view-tag"' in html and "vt.textContent = view.tag" in html
          and "view.bar_label" in html and "'周K'" not in head and "'条件重算'" not in head)
    check("「为什么周K上的信号不是原策略」由后端 notes 给，前端只挂到 title 上",
          "vt.title = (view.notes || []).join('；')" in html and "view.notes" in html)
    check("周期与视图说明挂了探针钩子（供浏览器段对拍）",
          "view: () => curView" in html and "strategies: () => strategies" in html)

    # 回车 = 点「查询」（2026-10-10 需求）：绑在**整张「查询条件」卡**上，不是逐个输入框。
    # 卡里还排着「策略」「周期」两个下拉，逐框绑迟早漏一个；而绑到 document 又会把焦点在
    # K 线卡（下挂指标 / 高开幅度）上的回车也一起劫持走 —— 那两处回车各有各的用途。
    # 行为（各控件各发一发、连按只发一发、别卡不劫持）由探针页负责，这里只钉住结构。
    _kq0 = html.index("$('#st-card-query').addEventListener('keydown'")
    _kq = html[_kq0:]
    _kq = _kq[:_kq.index("});") + 3]
    check("回车绑在「查询条件」卡上（卡里任意控件都能查询，不是只绑代码框）",
          _kq0 > 0)
    check("回车走**按钮那条路**（`#btn-load.click()`）—— 「查询中」与点按钮同样被挡住",
          "$('#btn-load').click()" in _kq)
    check("回车只有**一处**实现（没有逐框再绑一遍 keydown）",
          html.count("'keydown'") == 1 and "keypress" not in html,
          "keydown 绑了 %d 处" % html.count("'keydown'"))
    check("焦点在按钮上时不再补点一下（原生回车自己就会点它，别点两次）",
          "closest('button')" in _kq)
    check("不绑在 document 上（免得 K 线卡控件上的回车被劫持去查询）",
          "document.addEventListener('keydown'" not in html)

    # 周期还有**第二个入口**：K 线卡片上的分段按钮。它必须放在 K 线卡片上 ——
    # 预览模式（?embed=1）会把整张「查询条件」卡隐藏，没有这个入口，从列表点开的
    # K 线就永远只能看日K。它与那个下拉是**同一个状态源**（`#st-freq`），不另开一份状态。
    # ⚠ 切片**必须收在 `#st-freq-seg` 这个元素自己身上**：它后面还排着「幅度」输入框
    #   和它的 1%/3%/5% 快捷分段按钮（`#st-open-seg`，也是 `<button>`）——
    #   按「到下一个控件为止」切会把那 3 个按钮一起数进来（2026-10-09 踩过）。
    seg = _elem_html(html, 'id="st-freq-seg"')
    check("K线卡片上有「日K / 周K」分段按钮（预览弹窗切周期就靠它）",
          'id="st-freq-seg"' in seg and 'data-f="daily"' in seg and 'data-f="weekly"' in seg
          and seg.count("<button") == 2, "%d 个按钮" % seg.count("<button"))
    check("分段按钮与下拉共用一个状态源（选中态从 `#st-freq` 现画，不另存一份）",
          "function paintFreqSeg()" in html and "const v = $('#st-freq').value;" in html
          and "b.dataset.f === v" in html and "function setFreq(f)" in html)
    check("点分段按钮 = 写回下拉 + 重新取数（周K 是另一份面板，不是把日K画稀）",
          "$$('#st-freq-seg button').forEach" in html and "if (changed) load();" in html)
    check("预览模式取数时周期以控件为准（URL 上那份只是进来时的初值）",
          "q.set('freq', $('#st-freq').value);" in html)
    check("预览模式下切周期会告诉外面（「在诊断页打开」的链接跟着换）",
          "postMessage({ type: 'st-freq', freq: f }" in html)
    check("分段按钮**不锁** guardClick（与旁边那个周期下拉一致，来回切是正常操作）",
          "$$('#st-freq-seg button').forEach(b => b.onclick = () => setFreq(b.dataset.f));" in html
          and "guardClick($('#st-freq-seg')" not in html)

    # 预览模式（?embed=1）：选股策略试跑 / 今日选股列表里的「K线」弹窗嵌的就是这一页。
    # 只许隐藏非图表卡片，图表的取数与渲染必须一个字节都不变（重画一份就是第二个口径）。
    css = open(os.path.join(ROOT, "static", "css", "app.css"), encoding="utf-8").read()
    # ⚠ 隐藏名单是**锁死的一整串**：模板里多出一张卡片（例如 2026-09-23 加的 #st-card-ai）
    #   就必须同时改这里和 stock.html 的 <style>，否则预览弹窗里会漏出一块。
    # ⚠ 隐藏名单**不写死整串**（写死的话每加一张卡片都要改两处，2026-10-09 加卡片时
    #   正是这样假红一次）。改由下面那条「隐藏名单与页面卡片一一对上」的行为断言把关 ——
    #   它读的是块里的选择器与页面上的 id 集合，新卡片漏进名单会直接红。
    #   ⚠ 2026-10-09 起「开盘形态概率」不再是独立 `.card`（它顶替了 `#st-head` 第 2 栏），
    #     所以名单里**不该**再有它的 id —— 别再把它写进来。
    check("预览模式只隐藏非图表卡片（查询区/AI/概念/筹码/明细表），图表容器照旧",
          "{% if embed %}" in html
          and "#st-card-query" in html
          and "#st-card-chips" in html
          and html.count('id="st-card-query"') == 1)
    _hide = html[html.index("{% if embed %}"):]          # 只在这个块里抠，别把页面别处的选择器算进来
    _hide = _hide[:_hide.index("{% endif %}")]
    _hide_ids = set(re.findall(r"#(st-card-[a-z0-9-]+)\s*[,{]", _hide))
    _page_ids = set(re.findall(r'id="(st-card-[a-z0-9-]+)"', html))
    check("预览模式隐藏名单与页面卡片一一对上（隐藏的恰好是页面上的全部非图表卡片）",
          _hide_ids == _page_ids and len(_page_ids) >= 5,
          "藏=%s 页=%s" % (sorted(_hide_ids), sorted(_page_ids)))
    check("预览模式里侧栏/顶栏由 CSS 让位（.embed 只改看不看得见）",
          ".embed .sidebar, .embed .topbar{display:none}" in css)
    check("预览模式按 URL 参数取数、不看表单（预览里没有查询区）",
          "const EMBED_KEYS = ['strategy_id', 'config', 'pool_id', 'bars', 'freq', "
          "'trade_date', 'sel_only']" in html
          and "if (EMBED) {" in html and "load();" in html
          # 周期是唯一一个「以控件为准」的参数（URL 上那份只是初值），见下一条
          and "q.set('freq', $('#st-freq').value);" in html)
    check("弹窗用的预览 URL / 尺寸口径由 app.js 统一给（页面只负责被嵌）",
          "window.klinePreview = function" in open(
              os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
          and ".kl-frame{" in css)

    # 一个实例、多个 grid：grids / xAxes / yAxes 一一对应，dataZoom 与 axisPointer 覆盖全部
    check("按 K线 + 每组下挂各建一个 grid（高度按格类型取：成交量格矮一档）",
          "grids.push({ left: 58, right: 58, top: paneTop, height: pane })" in html)
    # ⚠ 每格必须把**自己的高度**也累加进游标：只 `cursor += GAP` 时 top 照样递增、
    #   断言照样过，但 96/118 高的格子会互相叠住（成交量盖住 MACD 就是这么来的，
    #   2026-09-25 修）。几何真值由探针读 option 再验一次「互不重叠」。
    check("逐格向下时把本格高度也累加（漏了就几格重叠）",
          "const paneTop = cursor + GAP;" in html and "cursor = paneTop + pane;" in html
          and "top:cursor - 26" not in html)
    # 2026-09-25 需求：「在股票K线预览的下面，macd指标图的上面，加上成交量指标」。
    # 成交量**不是新造的一格** —— 它复用后端目录里的 `vol` 组（量柱按当日阴阳着色 +
    # VMA5/VMA10、单位「手/万手」），只是从「下拉里可选」改成「固定前置」。
    # 静态只锁这几处（「确实排在 K 线之后、下挂之前」由探针读真实 option 锁）。
    # ⚠ 高度是**审美参数**（用户随时会调），所以只锁**关系**不锁字面量：
    #   成交量格必须比下挂格矮一档。别写 "const VH = 96;" —— 调一次就假红一次。
    _vh = int(re.search(r"const VH = (\d+);", html).group(1))
    _ih = int(re.search(r"const IH\s*=\s*(\d+);", html).group(1))
    check("成交量是固定格：常量 + 专属高度（比下挂格矮一档）+ 前置进 groups（顺序由探针实测锁）",
          "const VOL_KEY = 'vol';" in html and 0 < _vh < _ih
          and "const paneH = (g) => (g.key === VOL_KEY ? VH : IH)" in html
          and "(volGroup && !volGroup.empty ? [volGroup] : []).concat(pickedGroups().groups)" in html,
          "VH=%d / IH=%d" % (_vh, _ih))
    check("成交量不进下挂下拉（否则同一格能画两遍）",
          "const pickable = curCatalog.filter(g => g.key !== VOL_KEY);" in html
          and re.search(r"prevA !== VOL_KEY && catByKey\[prevA\]", html) is not None)
    check("成交量组整列没数据时整格不画、并在标题栏说明（与下挂格同一句话）",
          "if (volG && volG.empty) skipped.push(volG.name);" in html)
    check("每个 grid 各配一条 x 轴（同 data、同 boundaryGap，才能像素对齐）",
          html.count("type:'category', data:d, gridIndex:") == 2
          and "boundaryGap:true" in html)
    check("缩放作用于全部 x 轴（同步的实现本体）",
          re.search(r"const allX = xAxes\.map\(\(_, i\) => i\);", html) is not None
          and html.count("xAxisIndex: allX") == 2)
    check("十字光标联动覆盖全部 x 轴", "link: [{ xAxisIndex: 'all' }]" in html)
    check("只有最下面一格显示日期标签（其余留白给指标）",
          "{ show:isLast }" in html and "{ show:false }" in html)
    # 这条是实测抓出来的坑：baseAxis 里也带 axisLabel/splitLine，直接
    # Object.assign({...我的设置}, baseAxis) 会把 axisLabel.show 整个顶掉，
    # 结果三格全冒出日期标签 —— 必须走一层显式合并。
    # 合并函数**只有一份实现**（app.js 的 window.withBaseAxis），交易复盘图也用；
    # 页面里只留一个短名，免得哪天两边各改一套、其中一个又冒出日期标签。
    check("坐标轴合并只有一份实现（app.js），页面引用它",
          "window.withBaseAxis = function (extra, axisLabel)" in appjs
          and "out.axisLabel = Object.assign({}, window.baseAxis.axisLabel, axisLabel || {})" in appjs
          and "const withBase = window.withBaseAxis;" in html
          and re.search(r"Object\.assign\(\{[^}]*type:'(?:category|value)'[^}]*\}, baseAxis\)", html) is None)
    check("有界的指标（RSI/WR）固定 y 轴范围，不至于被噪声压扁",
          "if (g.band) { y.min = g.band[0]; y.max = g.band[1]; }" in html)

    # 参考线：挂在空数据的专用 series 上，否则会被图例连带点掉
    check("参考线用空数据 series 承载（不挂在数据线上）",
          "GUIDE_PREFIX + g.key" in html and "data: [], silent:true, tooltip:{ show:false }" in html)
    check("tooltip 过滤掉参考线 series（不会出现一行「—」）",
          "indexOf(GUIDE_PREFIX) !== 0" in html)
    check("tooltip 正文剔掉信号标记点（改用表头徽标复述，不然会出现「命中: 1254.10」）",
          "!(meta && meta.sig)" in html)
    # 实测抓到的：蜡烛图 value 首位是 dataIndex，直接当价格打会多一个假数字
    check("tooltip 里蜡烛图要切掉 value 首位的 dataIndex",
          "x.seriesType === 'candlestick' && v.length === 5 && v[0] === i" in html)

    # 策略信号：画在哪一侧、什么形状由后端 signals 定，前端只负责上色与像素偏移
    sigblk = html[html.index("/* --- 策略信号"):html.index("// --- 下挂格")]
    check("信号标记按后端 signals 取字段/侧别/形状（前端不写死 entry/buy/sell）",
          "x[sg.f]" in sigblk and "sg.side === 'above' ? x.high : x.low" in sigblk
          and "SIG_SHAPE[sg.shape]" in sigblk and "SIG_OFF[sg.side]" in sigblk
          and all(("'" + k + "'") not in sigblk for k in ("entry", "buy_trigger", "sell_signal")))
    check("后端说「不画」的档就不铺（买入触发未设条件时不会满屏红三角）",
          "if (!sg.draw) return;" in sigblk)
    check("信号标记落在 K 线那一格（grid 0），并进 K 线图例可单独开关",
          "type:'scatter', xAxisIndex:0, yAxisIndex:0" in sigblk
          and "legends[0].data = BASE_ITEMS.concat(sigNames)" in sigblk)
    check("tooltip 表头复述当根上的信号名（标记点小，容易看漏）",
          "curSignals.filter(sg => r[sg.f])" in html)
    check("信号数据挂了探针钩子（供浏览器段对拍）", "signals: () => curSignals" in html)
    try:
        from core import strategy as _st
        blk2 = re.search(r"const SIG_COLOR = \{([^}]*)\}", html)
        keys2 = re.findall(r"(\w+)\s*:", blk2.group(1)) if blk2 else []
        want2 = [s["key"] for s in _st.SIGNAL_PLOT]
        check(f"信号配色表的 key 与后端 SIGNAL_PLOT 一致（{len(want2)} 档）", keys2 == want2,
              "前端 " + ",".join(keys2) + " vs 后端 " + ",".join(want2))
        check("涨跌语义锁死：买入红 / 卖出绿（A 股习惯，换主题也不许动）",
              "buy_trigger: UP" in html and "sell_signal: DOWN" in html
              and "UP = '#e5352b'" in html and "DOWN = '#12a06a'" in html)
        # 同在下侧的两档必须错开：600519 + 内置策略1 实测 99 天命中里有 40 天同时触发买入，
        # 不错开的话红三角会把蓝点整个盖住（这条不锁死，以后改偏移量又会重叠回去）
        mo = re.search(r"const SIG_OFF = \{\s*below:\s*\{([^}]*)\},\s*above:\s*\{([^}]*)\}", html)
        if not mo:
            check("信号标记的像素偏移表可解析", False, "没找到 SIG_OFF")
        else:
            off = lambda t: {k: int(v) for k, v in re.findall(r"(\w+):\s*(-?\d+)", t)}
            bel, abv = off(mo.group(1)), off(mo.group(2))
            check("两档同在下侧时偏移错开 ≥8px、且上下镜像（同一天命中+买入触发不互相盖住）",
                  set(bel) == set(keys2) and set(abv) == set(bel)
                  and all(bel[k] > 0 for k in bel) and all(abv[k] < 0 for k in abv)
                  and all(abv[k] == -bel[k] for k in bel)
                  and abs(bel["entry"] - bel["buy_trigger"]) >= 8,
                  "below " + str(bel) + " / above " + str(abv))
    except Exception as e:  # noqa: BLE001
        check("信号配色表与后端一致", False, f"{type(e).__name__}: {e}")

    # 图表代码不许出现指标字段字面量 —— 选项/字段必须来自后端目录
    seg = html[html.index("function buildOption()"):html.index("function tipFormatter")]
    data_fields = ("macd_dif", "macd_dea", "macd_hist", "kdj_k", "kdj_d", "kdj_j",
                   "rsi6", "rsi12", "rsi24", "cci14", "wr14", "atr14",
                   "dmi_pdi", "dmi_mdi", "dmi_adx", "obv", "v_r", "mf_main_net")
    hits = sorted({f for f in data_fields if f in seg})
    check("图表代码里不写死指标字段名（一律走目录的 s.f）", not hits, ",".join(hits))
    check("下挂指标一律用目录给的字段取数", "x[s.f]" in seg and "seriesMeta[s.label]" in seg)
    check("数值格式按目录给的 token 走（单位口径只在一处定）",
          "FMT_TOKEN[meta && meta.fmt] || FMT_TOKEN.plain" in html)
    check("切换下拉只重画不重新请求（drawChart 挂在下拉 change 上）",
          "$('#st-ind-a').addEventListener('change', drawChart)" in seg.replace("function buildOption", "")
          or "$('#st-ind-a').addEventListener('change', drawChart);" in html)
    check("切指标时记住当前缩放窗口（setOption 会把 dataZoom 顶回默认）",
          "curZoom = [z.start, z.end]" in html and "curZoom ? curZoom[0] :" in html)

    # 调色板 key 必须与后端目录一一对应。表在 app.js（复盘图共用），页面只是引用它 ——
    # 所以这里断言两件事：app.js 的表与目录一致 + 页面确实引用的是那一份。
    try:
        from core import indicators as ind
        blk = re.search(r"window\.CHART_PAL = \{([\s\S]*?)\n  \};", appjs)
        pal = set(re.findall(r"(\w+):\s*\[", blk.group(1))) if blk else set()
        want = set(ind.CHART_INDICATOR_KEYS)
        check(f"调色板 key 与后端目录完全一致（{len(want)} 组，表在 app.js）", pal == want,
              "多 " + ",".join(sorted(pal - want)) + " 少 " + ",".join(sorted(want - pal)))
        check("页面引用共用的调色板 / 图例样式（不再各存一份）",
              "const PAL = window.CHART_PAL;" in html
              and "const LEGEND_STYLE = window.CHART_LEGEND;" in html)
        tok = set(re.findall(r"^\s*(\w+):\s*v =>", appjs, re.M))
        need = {"plain", "price", "vol", "count", "money_wan"}
        check("数值格式 token 前端都实现了（表在 app.js）", need <= tok,
              "缺 " + ",".join(sorted(need - tok)))
        check("页面用的是共用 token 表", "const FMT_TOKEN = window.CHART_FMT;" in html)
    except Exception as e:  # noqa: BLE001
        check("调色板 key 与后端目录一致", False, f"{type(e).__name__}: {e}")

    # 内联脚本语法：模板里改的是大段 JS，语法错整页都不会跑。
    # ⚠ 查的是**原始模板**，得先把 Jinja 表达式中和掉（如 `const EMBED_CODE = {{ code | tojson }};`），
    #   否则 `node --check` 一定报语法错 —— 那不是 JS 写错了，是模板语法（verify_screen_ui 同样处理）。
    blocks = re.findall(r"<script(?![^>]*src=)[^>]*>([\s\S]*?)</script>", html)
    big = max(blocks, key=len) if blocks else ""
    big = re.sub(r"\{\{[\s\S]*?\}\}|\{%[\s\S]*?%\}", "0", big)
    js = os.path.join(tempfile.gettempdir(), "_stock_inline.js")
    with open(js, "w", encoding="utf-8") as fh:
        fh.write(big)
    if os.path.exists(NODE):
        r = subprocess.run([NODE, "--check", js], capture_output=True, text=True)
        check(f"stock.html 内联脚本语法（{len(big)} 字符）", r.returncode == 0,
              (r.stderr or "").strip()[:140])
    else:
        skip("stock.html 内联脚本语法", "找不到 node")

    check("探针页带 ?run=1 门禁",
          os.path.exists(PROBE_HTML)
          and "params.get('run') !== '1'" in open(PROBE_HTML, encoding="utf-8").read())

    # 查询框：代码 / 6 位数字 / **名称** 都认（2026-10-10 需求「查询条件说可以输入名称和代码，
    # 实际只能输入代码，希望也可以输入股票名称」）。
    # 静态这边只钉**结构**：解析只有一处、诊断页真的走它、多义不猜、候选下拉绑得窄。
    # 「名称到底能不能查出票」由【2e】打真接口验，这里读文本验不出来。
    mk_src = open(os.path.join(ROOT, "core", "market.py"), encoding="utf-8").read()
    sc_src = open(os.path.join(ROOT, "core", "screener.py"), encoding="utf-8").read()
    check("「输入 → ts_code」只有一处实现（market.resolve_stock）",
          "def resolve_stock(" in mk_src)
    check("诊断页的 code 走那一处解析（screener 不再自己写 symbol 查询）",
          "market.resolve_stock(" in sc_src and "WHERE symbol=%s" not in sc_src)
    check("查询框的提示写着三种写法都能输（提示与实际能力一致）",
          "000001.SZ 或 000001 或 平安银行" in html and "请输入股票代码或名称" in html)
    check("名称模糊匹配只在**唯一命中**时才认，多义一律交回候选（不挑第一条）",
          "if len(rows) == 1:" in mk_src and '"candidates"' in mk_src)
    check("认不出来时的两句话分开：多义给候选 / 都没有才说格式（_code_error）",
          "def _code_error(" in sc_src and "匹配到" in sc_src and "没找到" in sc_src)
    check("用户输的 % 与 _ 当字面字符转义（不当通配符把整张表捞回来）",
          'replace("%", "\\\\%")' in mk_src)
    check("空壳 #st-suggest 用起来了：候选下拉走已有的 /api/stock/search",
          "sugDraw" in html and "'/api/stock/search?q='" in html)
    check("候选下拉只绑输入框自己（**不绑 document 的 keydown**，免劫持 K 线卡上的回车）",
          "document.addEventListener('click'" in html
          and "document.addEventListener('keydown'" not in html)
    check("点候选 = 填代码 + 走「查询」那条入口（防连点共用同一道锁）",
          "sugHide();" in html and "$('#btn-load').click();" in html)
    check("已经是完整代码就不再弹候选（那时用户是「输完了」）",
          r"/^\d{6}(\.(SH|SZ|BJ))?$/i" in html)

    # ---------------------------------------------------------------- 1b. 取数反馈
    # 「点了没反应」= 请求在飞时页面上什么都不动、失败后除了 3 秒就飘走的 toast 之外
    # 什么都不留（2026-10-10 需求）。三件事都收在 app.js **一处**：
    #   · #busy-bar   —— 任何页面打 /api 时顶部走一条细线（base.html 只负责摆位）
    #   · boxHTML/boxState —— 列表卡「取数中 / 取失败 / 没数据」同一套块
    #   · fail()      —— 失败收尾「toast + 页面上留痕」，两样都要
    print("\n【1b】取数反馈（加载中 / 报错留痕）—— 全站统一在 app.js")
    base = open(os.path.join(ROOT, "templates", "base.html"), encoding="utf-8").read()
    check("全局请求进度条 #busy-bar 摆在 base.html（任何页面打 /api 都点亮）",
          'id="busy-bar"' in base and "<i></i>" in base)
    check("进度条由 api() 统一驱动、且成对（收在 finally 里，报错也不会永远亮着）",
          "function busyInc()" in appjs and "function busyDec()" in appjs
          and "if (!opts.quiet) busyInc();" in appjs
          and "if (!opts.quiet) busyDec();" in appjs and "} finally {" in appjs)
    check("轮询类调用传 quiet（任务进度 900ms 一拍、侧栏状态别把进度条顶成常驻）",
          appjs.count("{ quiet: 1 }") >= 3, "%d 处" % appjs.count("{ quiet: 1 }"))
    check("容器状态块只有一处实现（boxHTML / boxState，全站列表共用同一套样式与文案）",
          "window.boxHTML = function" in appjs and "window.boxState = function" in appjs)
    check("失败收尾「toast + 页面上留痕」两样都要（只 toast 三秒后就像没反应）",
          "window.fail = function" in appjs
          and "toast(msg, 'err', 7000)" in appjs and "window.boxState(el, 'error'" in appjs)
    check("出错 / 警告的 toast 比普通提示留得久（要人读完再动手，别 3 秒飘走）",
          "type === 'err' ? 6000 : (type === 'warn' ? 4200 : 3200)" in appjs)
    check("进度条 / 转圈 / 状态块的样式都在 CSS 里（#busy-bar / .box-st / .spin）",
          "#busy-bar{position:fixed" in css and ".box-st{" in css and ".spin{" in css
          and "@keyframes busy-run" in css)
    # 每个「取失败会一直停在加载中…」的列表页都必须接上留痕：静态上钉住「该页用了
    # boxState」—— 少一个就是那一页漏了（行为真值由浏览器冒烟段兜）。
    for _f, _need in (("backtest.html", 2), ("data.html", 2), ("index.html", 2),
                      ("pool.html", 1), ("screen.html", 1), ("strategy.html", 1),
                      ("watch.html", 1)):
        _t = open(os.path.join(ROOT, "templates", _f), encoding="utf-8").read()
        check(f"{_f} 取数失败会往页面上留痕（调 boxState，不是只弹 toast）",
              _t.count("boxState(") >= _need, "%d 处" % _t.count("boxState("))
    check("futures.js 取数失败也留痕（列表不再永远停在「正在取期货行情…」）",
          "boxState(" in open(os.path.join(ROOT, "static", "js", "futures.js"),
                              encoding="utf-8").read())
    check("诊断页有查询状态区 #st-status（查询中 / 失败 / 无数据都落在页面上）",
          'id="st-status"' in html and "function stStatus(" in html)
    check("查询先摆「正在查询…」、失败把后端原话摊在页面上并弹 toast",
          "stStatus('loading', '正在查询 '" in html
          and "stStatus('error', '查询失败', e.message)" in html)
    check("状态区只有一处写入口（stStatus 调 app.js 的 boxHTML，别处不往 #st-status 塞 HTML）",
          "el.innerHTML = boxHTML(" in html and html.count("el.innerHTML = boxHTML(") == 1)

    # ---------------------------------------------------------------- 2. 接口契约
    print("\n【2】接口契约（/api/stock/<code>/detail 的 catalog）")
    try:
        first = (get_json(DETAIL % CODES[0] + "?bars=260").get("data") or {})
        cat = first.get("catalog") or []
        check("详情接口返回下挂指标目录", bool(cat), f"{len(cat)} 组")
        keys = [g["key"] for g in cat]
        check("目录含 MACD 与 KDJ（原来那两张图的内容没丢）",
              "macd" in keys and "kdj" in keys, ",".join(keys))
        check("目录不止 MACD/KDJ（可切换的其它指标）", len(keys) >= 8, ",".join(keys))
        param_keys = {"macd", "kdj", "rsi", "dmi", "cci", "wr", "atr"}
        bare = [g["name"] for g in cat if g["key"] in param_keys and not re.search(r"\(\d", g["name"])]
        check("参数化指标的名字带真实参数（不是光秃秃的 MACD）", not bare,
              ",".join(bare) or " | ".join(g["name"] for g in cat))
        check("目录里没有「占位名」——成交量/主力资金这类本身无参的除外",
              all(g["name"].strip() for g in cat))
        check("每组都标注了线型（line/bar）与数值格式",
              all(s.get("type") in ("line", "bar") and s.get("fmt")
                  for g in cat for s in g["series"]))

        # 目录给的字段在每只票的行数据里都必须真的存在 —— 否则前端画出来是空线
        for code in CODES:
            d = (get_json(DETAIL % code + "?bars=260").get("data") or {})
            rows = d.get("rows") or []
            if not rows:
                check(f"{code} 有数据可验", False, "rows 为空（本地没同步这只票？）")
                continue
            have = set(rows[0].keys())
            miss = sorted({s["f"] for g in d.get("catalog", []) for s in g["series"]} - have)
            check(f"{code} 目录引用的字段在行数据里都存在（{len(rows)} 根）", not miss,
                  ("缺 " + ",".join(miss)) if miss else "missing_fields=" + str(d.get("missing_fields")))
            empty = [g["key"] for g in d.get("catalog", []) if g.get("empty")]
            check(f"{code} 空数据组被如实标记（前端据此隐藏而不是画空线）",
                  all((rows[-1].get(s["f"]) is None) for g in d["catalog"] if g.get("empty")
                      for s in g["series"]),
                  "empty=" + (",".join(empty) or "无"))
    except Exception as e:  # noqa: BLE001
        check("详情接口契约", False, f"{type(e).__name__}: {e}（服务起了吗？）")

    # 基本面卡片：字段清单 / 单位换算 / 缺数据表现
    try:
        from core import db as _db, indicators as _ind
        first = (get_json(DETAIL % CODES[0] + "?bars=260").get("data") or {})
        fn = first.get("fundamental") or {}
        items = fn.get("items") or []
        want_keys = [f for f, _, _ in _ind.FUNDAMENTALS]
        check("详情接口返回基本面快照（字段与后端 FUNDAMENTALS 逐项一致）",
              [i["key"] for i in items] == want_keys
              and all(i.get("label") and i.get("text") for i in items),
              " | ".join(i["label"] + "=" + i["text"] for i in items))
        check("快照带上数据日期（= 最新那根 K 线那天）",
              re.match(r"^\d{4}-\d{2}-\d{2}$", fn.get("as_of") or "") is not None
              and (first.get("rows") or [{}])[-1].get("trade_date") == fn.get("as_of"),
              str(fn.get("as_of")))

        # 独立重算：直接从 daily_basic 取那一天的原值走一遍口径，跟接口给的对拍
        raw = _db.query_one("SELECT pe, pb, ps, total_mv, circ_mv, turnover_rate "
                            "FROM daily_basic WHERE ts_code=%s AND trade_date=%s",
                            (CODES[0], fn.get("as_of")))
        exp = {i["key"]: i["text"] for i in
               _ind.fundamental_snapshot(raw or {}, fn.get("as_of") or "")["items"]}
        got = {i["key"]: i["text"] for i in items}
        check("基本面数值 == 拿 daily_basic 原值独立重算（接口没取错行/错日期）",
              bool(raw) and exp == got, json.dumps(got, ensure_ascii=False))

        # 口径本体：万元→亿/万亿、格式、缺数据
        snap = {i["key"]: i["text"] for i in _ind.fundamental_snapshot(
            {"pe": 12.3456, "pb": None, "ps": 3.2, "total_mv": 157260000.0,
             "circ_mv": 500000.0, "turnover_rate": 3.214}, "2026-01-05")["items"]}
        check("市值口径：万元→「亿/万亿」（500000 万元 = 50 亿；上万亿不写成 15726.00亿）",
              snap["total_mv"] == "1.57万亿" and snap["circ_mv"] == "50.00亿",
              snap["total_mv"] + " / " + snap["circ_mv"])
        check("倍数与百分数各按各的格式（PE 12.35 / PS 3.20 / 换手率 3.21%）",
              snap["pe"] == "12.35" and snap["ps"] == "3.20" and snap["turnover_rate"] == "3.21%",
              f"{snap['pe']} / {snap['ps']} / {snap['turnover_rate']}")
        check("缺数据给「—」（None / NaN 都算缺，不拿更早一天的值顶也不填 0）",
              snap["pb"] == "—"
              and [i["text"] for i in _ind.fundamental_snapshot(
                  {"pe": float("nan"), "total_mv": None}, "2026-01-05")["items"]][0] == "—",
              f"pb={snap['pb']}")
    except Exception as e:  # noqa: BLE001
        check("基本面快照契约", False, f"{type(e).__name__}: {e}")

    # 策略一改指标参数，目录与字段必须跟着变 —— 这条是「后端唯一来源」的实证
    try:
        cfg = {"entry": ["close > 0"], "buy_trigger": ["open > ref(close, 1)"],
               "sell_signal": ["open < ref(close, 1)"],
               "indicators": {"cci": 20, "rsi": [6, 24], "atr": 7, "vma": [5],
                              "macd": {"fast": 5, "slow": 26, "signal": 9}}}
        q = urllib.parse.urlencode({"config": json.dumps(cfg), "bars": 120})
        d = (get_json(DETAIL % CODES[0] + "?" + q).get("data") or {})
        cm = {g["key"]: g for g in d.get("catalog") or []}
        rows = d.get("rows") or []
        have = set(rows[0].keys()) if rows else set()
        check("cci 改成 20 → 目录与字段都跟着变成 cci20（不是依然画 cci14）",
              cm.get("cci", {}).get("name") == "CCI(20)"
              and cm.get("cci", {}).get("series", [{}])[0].get("f") == "cci20"
              and "cci20" in have and "cci14" not in have,
              cm.get("cci", {}).get("name") + " / " + str(cm.get("cci", {}).get("series")))
        check("rsi 改成 [6,24] → 只剩两条 RSI，且行数据里有 rsi24",
              [s["f"] for s in cm.get("rsi", {}).get("series", [])] == ["rsi6", "rsi24"]
              and "rsi24" in have, str(cm.get("rsi", {}).get("name")))
        check("atr 改成 7 → ATR(7) / atr7",
              cm.get("atr", {}).get("name") == "ATR(7)" and "atr7" in have,
              str(cm.get("atr", {}).get("name")))
        check("vma 改成 [5] → 量均线只剩 VMA5",
              [s["label"] for s in cm.get("vol", {}).get("series", [])] == ["成交量", "VMA5"],
              str([s["label"] for s in cm.get("vol", {}).get("series", [])]))
        check("macd fast 改成 5 → 目录名字同步（参数不是写死的）",
              cm.get("macd", {}).get("name") == "MACD(5,26,9)", str(cm.get("macd", {}).get("name")))
    except Exception as e:  # noqa: BLE001
        check("指标参数跟随策略", False, f"{type(e).__name__}: {e}")

    # 策略信号：清单口径（画哪几档 / 几根 / 为什么不画）必须与行数据、与策略条件对得上
    try:
        from core import strategy as _st, screener as _scr
        first = (get_json(DETAIL % CODES[0] + "?bars=260").get("data") or {})
        sigs = first.get("signals") or []
        rows = first.get("rows") or []
        check("详情接口返回策略信号清单（三档：命中 / 买入 / 卖出）",
              [s["key"] for s in sigs] == [s["key"] for s in _st.SIGNAL_PLOT]
              and all(s.get("name") and s.get("side") in ("below", "above")
                      and s.get("shape") in ("dot", "arrow") for s in sigs),
              " | ".join(s["name"] + "(" + s["side"] + "/" + s["shape"] + ")" for s in sigs))
        bad = [(s["key"], s["count"],
                sum(1 for r in rows if r.get(s["f"])))
               for s in sigs if s["count"] != sum(1 for r in rows if r.get(s["f"]))]
        check("每档根数 == 返回窗口内该字段的真值数（卡片上的数、图上的点数同源）",
              not bad and all(s["count"] > 0 for s in sigs),
              ("不一致 " + str(bad)) if bad else
              " ".join(s["key"] + "=" + str(s["count"]) for s in sigs))

        # 空条件的两歧：entry 空 = 恒不成立；buy_trigger 空 = 次日无条件买入（必须标成不画）
        e = _scr.screen_detail(CODES[0], {"entry": ["close > ma20"], "buy_trigger": [],
                                          "sell_signal": []}, bars=120)["signals"]
        em = {s["key"]: s for s in e}
        check("「买入触发」未设条件时标成不画并说明原因（否则每根都是 True，会铺满箭头）",
              em["buy_trigger"]["count"] == 120 and em["buy_trigger"]["draw"] is False
              and bool(em["buy_trigger"]["note"]),
              f"count={em['buy_trigger']['count']} draw={em['buy_trigger']['draw']} "
              f"note={em['buy_trigger']['note']}")
        check("「卖出信号」未设条件时给出去哪离场的说明（本来就没有逐日信号）",
              em["sell_signal"]["count"] == 0 and em["sell_signal"]["draw"] is False
              and bool(em["sell_signal"]["note"]), em["sell_signal"]["note"])
        check("有条件的档照常给 draw=True（不是一律不画）",
              em["entry"]["count"] > 0 and em["entry"]["draw"] is True
              and em["entry"]["note"] == "", f"entry count={em['entry']['count']}")
        # 区间的裁剪要跟着 bars 走，否则图上点数与卡片数会对不上
        a = _scr.screen_detail(CODES[0], {"entry": ["close > ma20"]}, bars=60)["signals"]
        b = _scr.screen_detail(CODES[0], {"entry": ["close > ma20"]}, bars=250)["signals"]
        am = {s["key"]: s["count"] for s in a}
        bm = {s["key"]: s["count"] for s in b}
        check("信号根数按请求的 K 线根数裁剪（60 根窗口里的命中数 < 250 根的）",
              am["entry"] <= bm["entry"] and bm["entry"] > 0, f"60根={am['entry']} 250根={bm['entry']}")
    except Exception as e:  # noqa: BLE001
        check("策略信号清单契约", False, f"{type(e).__name__}: {e}")

    # mf 组的 empty 标记 —— 分两种情形（窗口自「查询日之后不截断」改版后必须分开验，
    # 否则会拿「查询日之后那几根里有数」去误判成「本地没这份数据」）
    try:
        from core import db
        # (a) 本地**压根没有** money_flow 行的票：整组该标 empty
        #     （否则前端画一条空线，看着像「主力没动」，是句假话）
        x0 = db.query_one(
            "SELECT k.ts_code FROM (SELECT ts_code FROM daily_kline GROUP BY ts_code) k "
            "WHERE NOT EXISTS (SELECT 1 FROM money_flow m WHERE m.ts_code = k.ts_code) LIMIT 1")
        if not x0:
            # 库里每只票都有资金流向 ⇒ 改用**合成数据喂真函数**（别靠「某只票恰好缺数据」碰运气）
            import pandas as pd
            from core import indicators as _ind0
            cat = [{"key": "mf", "series": [{"f": "mf_main_net"}]},
                   {"key": "ma", "series": [{"f": "close"}]}]
            fr = pd.DataFrame({"mf_main_net": [None, None], "close": [10.0, 11.0]})
            _ind0.mark_catalog_empty(cat, fr)
            check("本地无该组字段/整列全 NaN → 标 empty（合成数据喂真函数）",
                  cat[0]["empty"] is True and cat[1]["empty"] is False,
                  f"mf={cat[0]['empty']} ma={cat[1]['empty']}")
        else:
            code0 = x0["ts_code"]
            d0 = (get_json(DETAIL % code0 + "?bars=60").get("data") or {})
            mg0 = [g for g in d0.get("catalog", []) if g["key"] == "mf"]
            check(f"本地无任何资金流向的票（{code0}）把 mf 组标成 empty",
                  bool(mg0) and mg0[0].get("empty") is True,
                  f"empty={mg0[0].get('empty') if mg0 else '无该组'}")
            others = [g["key"] for g in d0.get("catalog", []) if g.get("empty") and g["key"] != "mf"]
            check("同一批数据里其它有数的组没有被误标 empty", not others, ",".join(others))

        # (b) 「资金流向起步很晚」的票：把视角停在起步之前。
        #     查询日那根仍是 NaN（当天本地确实没这份数据），但**整组不是 empty**——
        #     窗口按新口径延伸到了查询日之后，那几根里有数（旧口径 tail(bars) 会把它们切掉）
        cand = None
        for x in db.query("SELECT ts_code, MIN(trade_date) d FROM money_flow "
                          "GROUP BY ts_code HAVING d >= '2026-08-01' ORDER BY d DESC LIMIT 60"):
            k = db.query_one("SELECT MIN(trade_date) d FROM daily_kline WHERE ts_code=%s",
                             (x["ts_code"],))
            if k and str(k["d"])[:10] <= "2026-06-01":
                cand = (x["ts_code"], str(x["d"])[:10])
                break
        if not cand:
            skip("查询日那根的资金流向仍为空", "库里找不到「日线早于资金流向」的票")
        else:
            code, mf_start = cand
            d = (get_json(DETAIL % code + "?bars=30&trade_date=2026-08-01").get("data") or {})
            mg = [g for g in d.get("catalog", []) if g["key"] == "mf"]
            rows = d.get("rows") or []
            anc = d.get("anchor") or {}
            ai = anc.get("index")
            ai = (len(rows) - 1) if ai is None else int(ai)
            row = rows[ai] if rows else {}
            null_at_anchor = bool(rows) and all(row.get(s["f"]) is None
                                                for s in (mg[0]["series"] if mg else []))
            check(f"查询日那根的资金流向仍为空（{code}，其资金流向 {mf_start} 才开始）",
                  bool(mg) and null_at_anchor,
                  f"anchor={anc.get('date')} 行内值全空={null_at_anchor}")
            check("窗口延伸过查询日 ⇒ 整组不是 empty（那几根里有数，不是「本地没数据」）",
                  bool(mg) and anc.get("extended") is True and mg[0].get("empty") is False,
                  f"extended={anc.get('extended')} empty={mg[0].get('empty') if mg else '无该组'}")
    except Exception as e:  # noqa: BLE001
        skip("本地无资金流向的票把 mf 组标成 empty", f"{type(e).__name__}: {e}")

    # 策略收益卡片：必须真的跑过一遍回测引擎，且口径 = **满仓单票**
    # （默认等分 10 份会把单票收益缩成 1/10 —— 卡片就成了误导）
    try:
        from core import backtest as _bt, db as _db, market as _mk
        from core import screener as _sc, strategy as _stg

        d = (get_json(DETAIL % "600519.SH"
                      + "?bars=260&strategy_id=%d" % SID_DAILY).get("data") or {})
        p = d.get("perf") or {}
        rr0 = d.get("rows") or []
        check("接口返回 perf（hero 主数 + 4 个小项 + 区间）",
              bool(p.get("hero")) and len(p.get("items") or []) == 4 and bool(p.get("range")),
              json.dumps(p.get("hero"), ensure_ascii=False))
        check("收益统计区间与图上窗口严格同段（卡片数字才对得上图上的信号点）",
              bool(rr0) and p["range"]["start"] == rr0[0]["trade_date"]
              and p["range"]["end"] == rr0[-1]["trade_date"]
              and p["range"]["bars"] == len(rr0), json.dumps(p["range"]))
        check("单票仓位口径 = 满仓（默认等分 10 份会把收益缩成 1/10）",
              _bt.SINGLE_POSITION == {"max_positions": 1, "position_pct": 100.0},
              json.dumps(_bt.SINGLE_POSITION))
        check("收益文本格式：零不带正号（「+0.00%」会被读成小涨）",
              _bt._signed(0) == "0.00%" and _bt._signed(-3.214) == "-3.21%"
              and _bt._signed(12.3446) == "+12.34%" and _bt._signed(None) == "—",
              " / ".join([_bt._signed(0), _bt._signed(-3.214), _bt._signed(None)]))

        # ---- 独立对拍：**不经过** single_stock_perf，自己加载行情 + 直接调 run_backtest
        row = _db.query_one("SELECT * FROM strategy WHERE id=%s", (SID_DAILY,))
        cfg1 = _stg.merge_config(json.loads(row["config"])
                                 if isinstance(row["config"], str) else row["config"])
        # 选股条件在**选股策略**里 → 抬成 entry，才能跟接口算的是同一件事
        _ps = pool_spec_of(d)
        cfg1 = _stg.selection_cfg({**cfg1, "_pool": _ps} if _ps else cfg1)
        end = str(rr0[-1]["trade_date"])
        start = _sc._lookback_start(end, bars=260)
        panel = _mk.load_panel(["600519.SH"], start, end,
                               weekly=bool(_stg.required_weekly_fields(cfg1)),
                               indicators_cfg=cfg1.get("indicators"),
                               weekly_fields=_stg.required_weekly_fields(cfg1), only=None)
        df = panel["600519.SH"]
        sig = _stg.compute_signals(df, cfg1)
        data = {"600519.SH": _bt.pack_one("600519.SH", df, cfg1, sig=sig)}
        m = _bt.run_backtest(data, cfg1, str(rr0[0]["trade_date"]), end,
                             params=dict(_bt.SINGLE_POSITION))["metrics"]
        g = p.get("metrics") or {}
        keys = ("total_return", "benchmark_return", "max_drawdown", "win_rate", "alpha")
        same = all(abs(float(m[k]) - float(g[k])) < 1e-6 for k in keys)
        check("卡片上的收益 = 直接用回测引擎（满仓单票）跑出来的同一组数",
              same and int(m["trade_count"]) == int(g["trade_count"]),
              "引擎 %.4f%%/%d笔 vs 卡片 %s%%/%s笔"
              % (m["total_return"], m["trade_count"], g.get("total_return"), g.get("trade_count")))
        check("卡片主数就是引擎的 total_return，且文本按同一格式函数生成",
              abs(float(p["hero"]["value"]) - float(m["total_return"])) < 1e-9
              and p["hero"]["text"] == _bt._signed(m["total_return"]),
              f'{p["hero"]["text"]} vs {_bt._signed(m["total_return"])}')
        # 对照值「买入持有」= 窗口首尾收盘价，独立复算（引擎不传基准时按池内等权，单票即自身）
        bh = (float(rr0[-1]["close"]) / float(rr0[0]["close"]) - 1) * 100
        check("卡片里的「买入持有」= 窗口首尾收盘价算出的涨幅（独立复算）",
              g.get("benchmark_return") is not None
              and abs(float(g["benchmark_return"]) - bh) < 0.02,
              "引擎 %.4f%% vs 手算 %.4f%%" % (float(g["benchmark_return"]), bh))

        # 一笔都没成交：必须写明原因，别让「0.00%」被读成「不赚不亏」
        q0 = urllib.parse.quote(json.dumps({"entry": ["close > 100000"],
                                            "buy_trigger": ["open > 0"],
                                            "sell_signal": ["close < 0"]}))
        pz = ((get_json(DETAIL % "600519.SH" + "?bars=260&config=" + q0).get("data") or {})
              .get("perf") or {})
        check("一笔都没成交时主数是 0.00%（不带正号）、标明无交易并给出原因",
              (pz.get("hero") or {}).get("text") == "0.00%"
              and (pz.get("hero") or {}).get("cls") == "flat"
              and "无交易" in (pz.get("badge") or "")
              and "没有一根日K满足选股条件" in (pz.get("note") or ""),
              "%s / %s / %s" % ((pz.get("hero") or {}).get("text"), pz.get("badge"), pz.get("note")))
        check("0 交易时「交易」小项是 0 笔（不是空值/—）",
              (pz.get("items") or [{}])[0].get("text") == "0 笔",
              str((pz.get("items") or [{}])[0].get("text")))

        # 命中但买入触发永不成立：与「无命中」是两种不同原因，说明文字必须区分
        q1 = urllib.parse.quote(json.dumps({"entry": ["close > 0"],
                                            "buy_trigger": ["open < 0"],
                                            "sell_signal": ["close < 0"]}))
        pb2 = ((get_json(DETAIL % "600519.SH" + "?bars=260&config=" + q1).get("data") or {})
               .get("perf") or {})
        check("「有命中但买入触发没成立」与「没有命中」给的原因不同（都是 0 笔，但没说错话）",
              "有选股命中" in (pb2.get("note") or "")
              and (pb2.get("note") or "") != (pz.get("note") or ""),
              pb2.get("note"))

        # 改风控参数 → 收益必须重算（说明真在跑回测，不是缓存/写死）
        q2 = urllib.parse.quote(json.dumps(
            {"entry": ["close > 0"], "buy_trigger": [], "sell_signal": [],
             "risk": {"stop_loss_pct": -3, "take_profit_pct": 5}}, ensure_ascii=False))
        pa = ((get_json(DETAIL % "600519.SH" + "?bars=260&config=" + q2).get("data") or {})
              .get("perf") or {})
        q3 = urllib.parse.quote(json.dumps(
            {"entry": ["close > 0"], "buy_trigger": [], "sell_signal": []}, ensure_ascii=False))
        pc = ((get_json(DETAIL % "600519.SH" + "?bars=260&config=" + q3).get("data") or {})
              .get("perf") or {})
        check("改风控（止损/止盈）后收益跟着重算",
              bool(pa.get("hero")) and bool(pc.get("hero"))
              and pa["hero"]["text"] != pc["hero"]["text"],
              "%s（止损-3/止盈+5） vs %s（不带）"
              % (pa["hero"]["text"], pc["hero"]["text"]))
        # 换策略也要重算（与信号清单同步）
        d2 = (get_json(DETAIL % "600519.SH"
                       + "?bars=260&strategy_id=%d" % SID_WEEKLY).get("data") or {})
        check("换策略 ID 后收益与信号清单一起换",
              (d2.get("perf") or {}).get("hero", {}).get("text", "")
              != p["hero"]["text"],
              "%s → %s" % (p["hero"]["text"], (d2.get("perf") or {}).get("hero", {}).get("text")))
    except Exception as e:  # noqa: BLE001
        check("策略收益统计（回测引擎对拍 / 满仓口径 / 0 交易说明）",
              False, f"{type(e).__name__}: {e}")

    # K 线可切周线：周K **不是**「把日K画稀一点」——它是另一份面板（只画已收盘的完整周、
    # 指标按周K重算、策略条件也被搬到周K上求值）。这里要验三件事：
    #   · 数值同源：周K 的 close/open == 该周最后/第一个交易日的日线复权价；
    #   · 条件翻译：w_ 前缀在周K上就是它自己（否则周线策略的 w_close 会求值失败）；
    #   · 口径如实：view 里说清「这是不是原策略」，0 交易的原因也按周说（一根周K，不是一天）。
    try:
        import pandas as _pd
        from core import backtest as _bt2, db as _db3, market as _mk3
        from core import screener as _sc3, strategy as _stg3

        dq = (get_json(DETAIL % "600519.SH"
                       + "?bars=260&freq=weekly&strategy_id=%d" % SID_DAILY).get("data") or {})
        dd = (get_json(DETAIL % "600519.SH"
                       + "?bars=260&freq=daily&strategy_id=%d" % SID_DAILY).get("data") or {})
        wr, dr = dq.get("rows") or [], dd.get("rows") or []
        vw, vd = dq.get("view") or {}, dd.get("view") or {}
        # ⚠ 周K 的「查询日那一根」**未必是末根**：`latest_trade_date()` 落在**周中**时
        #   （例：国庆前最后一个交易日 2026-09-30 是周三），周线 anchor 会回退到最近
        #   **已收盘的完整周**（2026-09-25），但面板**照画**查询日之后那几根 —— 硬规矩 27 / C9：
        #   只走行情与指标、`_cut_after_anchor` 把它们的选择/买卖信号清零、也不算进收益。
        #   ⇒ 所有「截止查询日」的量都必须按 **anchor 那根** 算，不能按 `wr[-1]`。
        #   按末根断言会在「最新交易日落在周中」时变假红（2026-10-07 假期实测）。
        _anc = dq.get("anchor") or {}
        _adi = _anc.get("index")
        _ancd = (wr[_adi]["trade_date"]
                 if isinstance(_adi, int) and 0 <= _adi < len(wr) else None)
        _nb = (_adi + 1) if _ancd else len(wr)     # 截止 anchor 的根数（= 收益卡上的 N）
        _post = wr[_adi + 1:] if _ancd else []     # anchor 之后「照画但不计」的那几根
        check("freq=weekly 返回的是周K（根数远少于日线，且每根都是周五标签）",
              bool(wr) and bool(dr) and len(wr) < len(dr)
              and all(_pd.Timestamp(x["trade_date"]).weekday() == 4 for x in wr),
              f"{len(wr)} 根周K（{wr[0]['trade_date']}~{wr[-1]['trade_date']}）"
              f" vs {len(dr)} 根日K（{dr[0]['trade_date']}~{dr[-1]['trade_date']}）")
        check("view 说明这个周期是什么、条件有没有被搬过（三个字段都要有）",
              vw.get("freq") == "weekly" and vw.get("bar_label") == "周K"
              and vw.get("last_label") == "最新周K收盘" and vw.get("recomputed") is True
              and str(vw.get("tag", "")).startswith("周K") and vw.get("bars") == len(wr)
              and vd.get("freq") == "daily" and vd.get("recomputed") is False
              and vd.get("last_label") == "最新收盘" and vd.get("bars") == len(dr),
              json.dumps(vw, ensure_ascii=False) + " | 日线 " + json.dumps(vd, ensure_ascii=False))

        # ---- 数值同源：拿日线视图的行数据独立复算每一根周K ----
        wb_first, wb_last = _mk3.week_bounds()
        dmap = {x["trade_date"]: x for x in dr}
        bad_c = bad_o = cmpk = 0
        for w in wr:
            k = _pd.Timestamp(w["trade_date"])
            if k not in wb_last.index:
                continue
            lb, fb = str(wb_last[k])[:10], str(wb_first[k])[:10]
            if lb not in dmap:
                continue
            cmpk += 1
            if abs(dmap[lb]["close"] - w["close"]) > 1e-6:
                bad_c += 1
            if fb in dmap and abs(dmap[fb]["open"] - w["open"]) > 1e-6:
                bad_o += 1
        check("周K 的收盘 == 该周最后一个交易日的日线复权价（同一份数，不是另算一套）",
              cmpk >= 20 and bad_c == 0, f"{cmpk} 周可比，不一致 {bad_c} 条")
        check("周K 的开盘 == 该周第一个交易日的日线复权价",
              cmpk >= 20 and bad_o == 0, f"{cmpk} 周可比，不一致 {bad_o} 条")

        # ---- 只画已收盘的完整周 ----
        # 「不画半周线」这条约束管的是**查询日那一根**（anchor）：它必须是已收盘的完整周。
        # 查询日之后那几根是**故意照画**的（硬规矩 27 / C9），但必须①在 notes 里说出口、
        # ②不带任何选股/买卖信号、③不算进收益 —— 见下面三条一起断言。
        # ⚠ 别对 `wr[-1]` 断言「完整周」：最新交易日落在周中时末根就是延伸根，必假红
        #   （2026-10-07 假期实测）。也不要写死「notes 必须有『完整周』」—— anchor 正好
        #   是本周最后一天时本来就没有可解释的（2026-09-18 实测）。
        _anc_wk = _pd.Timestamp(_ancd) if _ancd else None
        _anc_complete = bool(_anc_wk is not None and _mk3.complete_week_mask(
            _pd.Series([_anc_wk]), _mk3.latest_trade_date()).iloc[0])
        check("周K 的「查询日那一根」是已收盘的完整周，之后多画的根只走行情、不带任何信号",
              _anc_complete and _ancd is not None
              and not any(x.get("entry") or x.get("buy_trigger") or x.get("sell_signal")
                          for x in _post)
              and (_post == [] or any("完整周" in s for s in (vw.get("notes") or []))),
              f"anchor {_ancd}(idx {_adi}) / 末根 {wr[-1]['trade_date']} / 之后 "
              f"{len(_post)} 根 / 最新交易日 {_mk3.latest_trade_date()} / "
              f"notes={len(vw.get('notes') or [])}")

        # ---- 条件翻译：w_ 前缀在周K上就是它自己（不翻译会整档求值失败/恒 False）----
        from core import expr as _ex
        cfg2 = _stg3.merge_config((_stg3.get_strategy(SID_WEEKLY) or {}).get("config") or {})
        tw = _stg3.to_weekly_view(cfg2)
        check("带周线过滤的策略搬到周K上时，w_ 前缀被去掉（w_close → close，同源取值）",
              bool(cfg2.get("weekly_filter"))
              and tw.get("weekly_filter") == [e.replace("w_", "") for e in cfg2["weekly_filter"]]
              and tw["timeframe"] == "weekly" and tw["entry"] == cfg2["entry"],
              f'{cfg2.get("weekly_filter")} → {tw.get("weekly_filter")}')
        check("前缀替换不误伤普通字段（low_close / 含 w 的字段名不能被改）",
              _stg3._drop_w_prefix(["low_close > ma20", "w_close > w_boll_mid", "close > 0"])
              == ["low_close > ma20", "close > boll_mid", "close > 0"],
              str(_stg3._drop_w_prefix(["low_close > ma20", "w_close > w_boll_mid", "close > 0"])))
        # 真正周线口径的策略（内置「周线布林下轨低吸」是 daily+weekly_filter，不是它）
        wcfg0 = _stg3.merge_config({"timeframe": "weekly",
                                    "entry": ["close <= boll_lower * 1.03"],
                                    "buy_trigger": ["open > ref(close, 1)"],
                                    "sell_signal": ["open < ref(close, 1)"],
                                    "risk": {"max_hold_days": 8}})
        _wv0 = _sc3.screen_detail("600519.SH", wcfg0, bars=120).get("view") or {}
        _wv1 = _sc3.screen_detail("600519.SH", wcfg0, bars=120, freq="daily").get("view") or {}
        check("周线策略不带 freq 时默认就看周K（否则条件会落在日K上求值 —— 形状对、口径错）",
              _wv0.get("freq") == "weekly" and _wv0.get("recomputed") is False
              and _wv0.get("tag") == "周K" and _wv0.get("native") is True
              and _wv1.get("freq") == "daily" and _wv1.get("recomputed") is True,
              f'默认 {_wv0.get("tag")}(freq={_wv0.get("freq")}) / '
              f'显式日线 {_wv1.get("tag")}(freq={_wv1.get("freq")})')

        # ---- 卡片：收益按周K重算（max_hold_days 变成周数），区间与图上窗口同段 ----
        # ⚠ 根数/区间终点按 **anchor**（截止查询日）算，不是 `wr[-1]`（延伸根不算收益）。
        pw = dq.get("perf") or {}
        _pr = pw.get("range") or {}
        check("收益卡片标明是周K口径（「满仓单票 · 周K N 根」，不会被按日读）",
              "周K" in (pw.get("badge") or "") and str(_nb) in (pw.get("badge") or "")
              and _pr.get("start") == wr[0]["trade_date"]
              and _pr.get("end") == _ancd and _pr.get("bars") == _nb,
              str(pw.get("badge")) + " / " + str(_pr))
        # 独立对拍：不经过 screen_detail，自己合成周线面板 + 直接调回测引擎
        row = _db3.query_one("SELECT * FROM strategy WHERE id=%s", (SID_DAILY,))
        cfg1 = _stg3.merge_config(json.loads(row["config"])
                                  if isinstance(row["config"], str) else row["config"])
        # 同上游：先把选股策略抬成 entry，再翻译到周K（顺序必须与 screen_detail 一致）
        _ps = pool_spec_of(dq)
        cfg1 = _stg3.selection_cfg({**cfg1, "_pool": _ps} if _ps else cfg1)
        cfgw = _stg3.to_weekly_view(cfg1)
        # ⚠ 对拍必须跑在**同一段**上：卡片的区间到 anchor，延伸到 2026-10-02 那根不计收益。
        #   拿 `wr[-1]` 当终点会让引擎多跑一根延伸根 ⇒ 两组数不同（假期时必红）。
        endw = _ancd or wr[-1]["trade_date"]
        startw = _sc3._lookback_start(endw, bars=260, freq="weekly")
        panw = _mk3.compute_weekly_panel(["600519.SH"], startw, endw,
                                         indicators_cfg=cfgw.get("indicators"), only=None)
        dfw = panw["600519.SH"]
        sigw = _stg3.compute_signals(dfw, cfgw)
        mw = _bt2.run_backtest({"600519.SH": _bt2.pack_one("600519.SH", dfw, cfgw, sig=sigw)},
                               cfgw, wr[0]["trade_date"], endw,
                               params=dict(_bt2.SINGLE_POSITION))["metrics"]
        gw = pw.get("metrics") or {}
        samew = all(abs(float(mw[k]) - float(gw[k])) < 1e-6
                    for k in ("total_return", "benchmark_return", "max_drawdown", "win_rate"))
        check("周K 的收益 = 用回测引擎在同一份周线面板上跑出来的同一组数",
              samew and int(mw["trade_count"]) == int(gw.get("trade_count") or 0),
              "引擎 %.4f%%/%d笔 vs 卡片 %s%%/%s笔"
              % (mw["total_return"], mw["trade_count"], gw.get("total_return"),
                 gw.get("trade_count")))
        check("周K 视图的收益与日线视图是两组不同的数（印证口径确实分了岔）",
              (pw.get("hero") or {}).get("text") != (dd.get("perf") or {}).get("hero", {}).get("text"),
              "%s（周K） vs %s（日K）"
              % ((pw.get("hero") or {}).get("text"),
                 (dd.get("perf") or {}).get("hero", {}).get("text")))

        # ---- 基本面：周K面板上没有日频字段，必须另取最近一条 daily_basic ----
        fw = dq.get("fundamental") or {}
        raw = _db3.query_one("SELECT pe,pb,ps,total_mv,circ_mv,turnover_rate FROM daily_basic "
                             "WHERE ts_code=%s AND trade_date=%s", ("600519.SH", fw.get("as_of")))
        exp = {i["key"]: i["text"] for i in
               _ind.fundamental_snapshot(raw or {}, fw.get("as_of") or "")["items"]}
        check("周K 视图的基本面照样有数（另取最近一条 daily_basic，不硬聚合出「周市盈率」）",
              bool(raw) and exp == {i["key"]: i["text"] for i in (fw.get("items") or [])},
              f"as_of={fw.get('as_of')} vs 末根周K {wr[-1]['trade_date']}，"
              + str({i["key"]: i["text"] for i in (fw.get("items") or [])}))

        # ---- 0 交易的原因要按周期说：是「一根周K」没命中，不是「一天」----
        qw = urllib.parse.quote(json.dumps({"entry": ["close > 100000"],
                                            "buy_trigger": ["open > 0"],
                                            "sell_signal": ["close < 0"]}))
        pz2 = ((get_json(DETAIL % "600519.SH" + "?bars=260&freq=weekly&config=" + qw)
                .get("data") or {}).get("perf") or {})
        check("0 交易时原因按周说（「没有一根周K满足选股条件」，不写成「没有一天」）",
              "没有一根周K满足选股条件" in (pz2.get("note") or "")
              and (pz2.get("badge") or "") == "本区间无交易",
              str(pz2.get("badge")) + " / " + str(pz2.get("note")))
    except Exception as e:  # noqa: BLE001
        check("K线切周线（数值同源 / 条件翻译 / 口径标注）", False, f"{type(e).__name__}: {e}")

    # ------------------------------------------------------------------ 筹码与两融
    # 股东户数 / 融资融券 / 股东增减持：**不走 K 线面板**，是另外三张表。
    # 这里独立复算，不调用 core.chips 的派生函数 —— 否则等于自己给自己判卷。
    print("\n【2d】筹码与两融（股东户数 / 融资融券 / 增减持）")
    try:
        from core import db as _db4
        d4 = (get_json(DETAIL % "600519.SH"
                       + "?strategy_id=%d&bars=120" % SID_DAILY).get("data") or {})
        c4 = d4.get("chips") or {}
        h4, m4, t4 = c4.get("holder") or {}, c4.get("margin") or {}, c4.get("trade") or {}
        check("返回里有 chips 三块（holder / margin / trade）且带视角日期 as_of",
              all(k in c4 for k in ("holder", "margin", "trade")) and bool(c4.get("as_of")),
              f"as_of={c4.get('as_of')} holder={h4.get('available')} "
              f"margin={m4.get('available')} trade={t4.get('available')}")

        # ---- 股东户数：独立按 end_date 归并、取最新公告日重算一遍 ----
        rows4 = _db4.query(
            "SELECT ann_date,end_date,holder_num FROM holder_number "
            "WHERE ts_code=%s AND ann_date<=%s ORDER BY end_date,ann_date",
            ("600519.SH", c4.get("as_of")))
        merged = {}
        for r in rows4:
            k = str(r["end_date"])[:10]
            a = str(r["ann_date"])[:10]
            if k not in merged or a >= merged[k][0]:
                merged[k] = (a, r["holder_num"])
        order = sorted(merged)
        exp_last = merged[order[-1]] if order else None
        exp_prev = merged[order[-2]] if len(order) > 1 else None
        exp_g = ((exp_last[1] / exp_prev[1] - 1) * 100
                 if (exp_last and exp_prev and exp_prev[1]) else None)
        got_items = {i["key"]: i["text"] for i in (h4.get("items") or [])}

        def _cnt(v):
            if v is None:
                return "—"
            fv = float(v)
            return ("%.2f亿" % (fv / 1e8)) if abs(fv) >= 1e8 else (
                ("%.2f万" % (fv / 1e4)) if abs(fv) >= 1e4 else "%.0f" % fv)

        def _sp(v):
            return "—" if v is None else (("+" if v > 0 else "") + "%.2f%%" % v)

        check("股东户数：主数 / 截止日期 / 发布时间 / 上期户数 由库里原值独立重算对拍",
              exp_last is not None
              and h4.get("hero", {}).get("text") == _cnt(exp_last[1])
              and got_items.get("end_date") == order[-1]
              and got_items.get("ann_date") == exp_last[0]
              and got_items.get("prev") == (exp_prev[1] and _cnt(exp_prev[1]) or "—"),
              f"主数 {h4.get('hero', {}).get('text')} / 期望 {_cnt(exp_last[1]) if exp_last else '—'}"
              f" · 截止 {got_items.get('end_date')} · 发布 {got_items.get('ann_date')}")
        check("股东户数增长率 = 与上一报告期相比（不是与上一期非空值、也不是同比）",
              got_items.get("growth") == _sp(exp_g),
              f"卡片 {got_items.get('growth')} / 独立复算 {_sp(exp_g)}"
              f"（{order[-2] if len(order) > 1 else '—'} → {order[-1] if order else '—'}）")
        check("趋势图序列逐期等于库里原值（柱数 == 期数，户数逐点相等）",
              len(h4.get("series") or []) == len(order[-8:])
              and all(s["holder_num"] == merged[s["end_date"]][1]
                      for s in (h4.get("series") or [])),
              f"{len(h4.get('series') or [])} 期 / 库里 {len(order)} 期")

        # ---- 融资融券：占流通市值独立复算（两融是元、circ_mv 是万元，差 1e4）----
        mi = m4.get("items") or []
        gm = {i["key"]: i["text"] for i in mi}
        last_m = (m4.get("series") or [{}])[-1]
        mv = _db4.query_one(
            "SELECT circ_mv FROM daily_basic WHERE ts_code=%s AND trade_date<=%s "
            "ORDER BY trade_date DESC LIMIT 1", ("600519.SH", last_m.get("trade_date")))
        exp_ratio = (last_m.get("rzrqye") / (mv["circ_mv"] * 1e4) * 100
                     if (mv and mv.get("circ_mv") and last_m.get("rzrqye")) else None)
        check("两融占流通市值 = 两融余额 ÷ 同日流通市值（元 vs 万元差 1e4，只有一处换算）",
              exp_ratio is not None and gm.get("ratio") == "%.2f%%" % exp_ratio,
              f"卡片 {gm.get('ratio')} / 独立复算 {exp_ratio and ('%.2f%%' % exp_ratio)}")
        check("融资净买入 = 融资买入额 − 融资偿还额（逐日全序列对拍）",
              all(abs(s["rz_net"] - (s["rzmre"] - s["rzche"])) < 1e-6
                  for s in (m4.get("series") or []) if s.get("rzmre") is not None),
              f"{len(m4.get('series') or [])} 个交易日")
        check("两融金额文本按「元 → 亿/万」换算（不是拿万元口径套）",
              gm.get("rzrqye", "").endswith("亿") and
              abs(float(gm.get("rzrqye", "0亿")[:-1]) - last_m.get("rzrqye", 0) / 1e8) < 0.01,
              f"{gm.get('rzrqye')} vs {last_m.get('rzrqye', 0) / 1e8:.2f}亿")

        # ---- 价格折线：独立从 daily_kline 取**同日**收盘价对拍（不调 core.chips）----
        # 这条是「按收盘价算」这个需求的真正判据：既不能拿 pct_chg 当价格，
        # 也不能用「≤ 该日的最近一条」把停牌日填成前值 —— 停牌日就该是 None（断点）。
        px_bad, px_none = [], 0
        for s in (m4.get("series") or []):
            r5 = _db4.query_one(
                "SELECT close FROM daily_kline WHERE ts_code=%s AND trade_date=%s",
                ("600519.SH", s.get("trade_date")))
            want5 = float(r5["close"]) if (r5 and r5.get("close") is not None) else None
            if want5 is None:
                px_none += 1
                if s.get("close") is not None:
                    px_bad.append(s.get("trade_date"))
            elif s.get("close") is None or abs(want5 - s["close"]) > 1e-9:
                px_bad.append(s.get("trade_date"))
        check("价格折线 = 同日日线**收盘价**（逐点独立复算；停牌/无价日必须是空值，不填前值）",
              bool(m4.get("series")) and not px_bad,
              f"{len(m4.get('series') or [])} 个交易日（其中 {px_none} 天库里无 K 线）"
              + (f"，不符 {len(px_bad)} 天 → {px_bad[:3]}" if px_bad else ""))

        # ---- 增减持：条数 / 汇总 / 方向 ----
        ti = t4.get("items") or []
        sm = {i["key"]: i["text"] for i in ((t4.get("summary") or {}).get("items") or [])}
        n_in = len([x for x in ti if x["in_de"] == "IN"])
        n_de = len([x for x in ti if x["in_de"] == "DE"])
        check("增减持：明细条数 == 汇总里的增持+减持条数，方向文案与徽标色配套",
              (not ti and not t4.get("available"))
              or (sm.get("in_cnt") == f"{n_in} 条" and sm.get("de_cnt") == f"{n_de} 条"
                  and all(x["cls"] == ("up" if x["in_de"] == "IN" else "down") for x in ti)),
              f"{len(ti)} 条（增{n_in}/减{n_de}）{sm.get('in_cnt')}/{sm.get('de_cnt')}")

        # ---- K 线顶边的「股东增减持」± 标记（2026-10-10 需求）------------------
        # ⚠ **独立复算**，不调 core.chips.trade_marks（否则等于自己给自己判卷）。
        #    口径只有一条：公告日 → 「当天或之后的第一根 K 线」。上面那次 d4 只取 120 根，
        #    窗口里多半没有公告；这里按 300 根再取一次，让窗口含进 600519 在 2025-09 /
        #    2025-12 的两条增持 —— 才能验到"真的对齐上了"，而不是空对空。
        import bisect as _bisec
        import datetime as _dt
        d4b = (get_json(DETAIL % "600519.SH"
                        + "?strategy_id=%d&bars=300" % SID_DAILY).get("data") or {})
        mk4 = ((d4b.get("chips") or {}).get("trade") or {}).get("marks")
        rd4 = [str(x["trade_date"])[:10] for x in (d4b.get("rows") or [])]
        check("增减持返回里带图上的 marks（按 K 线行号对齐，前端只按 idx 摆点）",
              isinstance(mk4, list) and bool(rd4),
              f"{len(mk4 or [])} 个标记 / K线 {len(rd4)} 根")
        raw4 = _db4.query(
            "SELECT ann_date, in_de FROM holder_trade WHERE ts_code=%s AND ann_date<=%s "
            "ORDER BY ann_date, change_vol DESC",
            ("600519.SH", (d4b.get("chips") or {}).get("as_of")))
        # ⚠ 与后端同一条下界：**窗口首根之前 7 天以外**的公告不参与 —— 否则 2023 年的
        #   老公告会被对齐到第 0 根（2025-07 那根）K 线上，那是错的口径；首根前一周内
        #   的公告仍要对齐到第 0 根（它们就是"开头那几天"刚发生的）。
        _lo4 = (_dt.date.fromisoformat(rd4[0]) - _dt.timedelta(days=7)).isoformat()
        exp4 = {}
        for r in raw4:
            _inde = (r["in_de"] or "").strip().upper()
            if _inde not in ("IN", "DE"):
                continue
            _ad = str(r["ann_date"])[:10]
            if _ad < _lo4:
                continue
            _i = _bisec.bisect_left(rd4, _ad)         # 当天或之后的第一根
            if _i >= len(rd4):
                continue
            exp4[(_i, _inde)] = exp4.get((_i, _inde), 0) + 1
        got4 = {(m["idx"], m["in_de"]): m["count"] for m in (mk4 or [])}
        check("每条公告都对齐到「当天或之后的第一根 K 线」，同日同方向归成一个标记",
              got4 == exp4,
              f"{len(got4)} 个标记 vs 独立复算 {len(exp4)} 个"
              + (f"；多 {sorted(set(got4) - set(exp4))[:3]} 少 {sorted(set(exp4) - set(got4))[:3]}"
                 if got4 != exp4 else ""))
        check("marks 的 idx / 日期与返回的 rows 对得上（没拿日期去 indexOf 那种对不上）",
              all(0 <= m["idx"] < len(rd4) and rd4[m["idx"]] == m["date"] for m in (mk4 or [])),
              f"{[(m['idx'], m['date']) for m in (mk4 or [])][:4]}")
        check("mark 里带方向和数量文本（前端不自己判正负、不自己拼单位）",
              all(m.get("dir_text") in ("增持", "减持") and m.get("in_de") in ("IN", "DE")
                  and m.get("vol_text") and m.get("count", 0) >= 1 for m in (mk4 or [])),
              f"{[(m['dir_text'], m['count'], m['vol_text']) for m in (mk4 or [])][:3]}")

        # ---- 未来函数：把视角调到过去某天，chips 里不能出现更晚的记录 ----
        past = _db4.scalar(
            "SELECT MAX(trade_date) FROM daily_kline WHERE trade_date<='2025-06-30'")
        past = str(past)[:10] if past else "2025-06-30"
        d4p = (get_json(DETAIL % "600519.SH"
                        + f"?strategy_id={SID_DAILY}&bars=60&trade_date={past}")
               .get("data") or {})
        c4p = d4p.get("chips") or {}
        hp, mp, tp = (c4p.get("holder") or {}), (c4p.get("margin") or {}), (c4p.get("trade") or {})
        late = []
        for s in (hp.get("series") or []):
            if s.get("ann_date", "") > past:
                late.append("holder:" + s["ann_date"])
        for s in (mp.get("series") or []):
            if s.get("trade_date", "") > past:
                late.append("margin:" + s["trade_date"])
        for x in (tp.get("items") or []):
            if x.get("ann_date", "") > past:
                late.append("trade:" + x["ann_date"])
        # 图上的 ± 标记同属筹码这一层：查询日之后才发生的公告不许挂到 K 线上
        for m in (tp.get("marks") or []):
            if m.get("ann_date", "") > past or m.get("date", "") > past:
                late.append("mark:" + m.get("date", ""))
        check("「视角日期」是真截止：调回到过去那天后，chips 里没有更晚的公告/交易日（无未来函数）",
              not late, f"视角 {past}；越界 {len(late)} 条" + (f" → {late[:3]}" if late else ""))
        check("过去视角下股东户数也跟着回到当时能看到的那一期",
              past not in ("", None) and
              (not (hp.get("available")) or (hp.get("items") and
               [i for i in hp["items"] if i["key"] == "end_date"][0]["text"] <= past)),
              f"截止 {[i for i in (hp.get('items') or []) if i['key']=='end_date'][:1]}")

        # ---- 缺数据要如实说，不能假装有 ----
        d4n = (get_json(DETAIL % "000001.SZ"
                        + "?strategy_id=%d&bars=60" % SID_DAILY).get("data") or {})
        t4n = (d4n.get("chips") or {}).get("trade") or {}
        check("没有增减持记录时给的是空态 + 原因（不是悄悄渲染成 0 条让人以为查过）",
              (t4n.get("available") is False and bool(t4n.get("note")))
              or (t4n.get("available") is True and bool(t4n.get("items"))),
              f"available={t4n.get('available')} note={(t4n.get('note') or '')[:46]}")

        # ---- 所属概念（东财 F10 核心题材）：只回真概念、文本与色都算好、口径写清 ----
        cpt4 = d4.get("concepts") or {}
        cptI = cpt4.get("items") or []
        cptRows = _db4.query(
            "SELECT board_code, board_name, reason FROM stock_concept "
            "WHERE ts_code=%s AND is_concept=1", ("600519.SH",))
        check("概念只回 is_concept=1 的真概念（行业/地域/指数成分/风格只报个数不列出）",
              (not cptRows and cpt4.get("available") is False) or
              (len(cptI) == len(cptRows) and
               {x["code"] for x in cptI} == {r["board_code"] for r in cptRows} and
               all(bool(x.get("reason")) for x in cptI)),
              f"{len(cptI)} 个 vs 库里真概念 {len(cptRows)} 个，另有 {cpt4.get('marks')} 条标记")
        check("概念涨跌幅的文本与颜色由后端算好（前端不判正负、不拼百分号）",
              all(x["pct_text"] == ("—" if x["pct_chg"] is None else "%+.2f%%" % x["pct_chg"])
                  and x["pct_cls"] == ("" if x["pct_chg"] is None else
                                       ("up" if x["pct_chg"] > 0 else
                                        ("down" if x["pct_chg"] < 0 else "flat")))
                  for x in cptI),
              f"{len(cptI)} 个"
              + ("" if not cptI else "，例：" + cptI[0]["name"] + " " + cptI[0]["pct_text"]))
        check("概念 note 交代了口径：板块涨跌幅是哪一份（东财快照 / 本库等权自算）",
              bool(cpt4.get("note")) and
              (("收盘等权自算" in cpt4["note"]) if cpt4.get("quote_src") == "local"
               else ("快照" in cpt4["note"]) if cpt4.get("quoted_at")
               else True),
              f"src={cpt4.get('quote_src')} · {(cpt4.get('note') or '')[-46:]}")

        # ---- 筹码与 K 线周期无关：同一天背靠背取 daily / weekly 各一次，整段逐字相等。
        # 用一个**过去的** trade_date 取，避开"后台同步正在往里填数"造成的序列漂移；
        # 万一正好撞上，重试一次（外部数据变化不该记成「周期影响口径」）。----
        # ⚠ 例外只有一个：`trade.marks` 是**图上的**东西（对齐到 K 线行号），日K 与周K 的
        #   网格本就不同 ⇒ 行号必然不同。卡片那三块（items / summary / series）才该与
        #   周期无关，所以比之前先把 marks 摘掉；marks 自己另有一条断言（见下）。
        def _card_only(c):
            c = dict(c or {})
            t = dict(c.get("trade") or {})
            t.pop("marks", None)
            c["trade"] = t
            return c
        same = None
        a = b = None
        for _ in range(2):
            q = f"?strategy_id={SID_DAILY}&bars=120&trade_date={past}"
            a = ((get_json(DETAIL % "600519.SH" + q + "&freq=daily").get("data") or {})
                 .get("chips"))
            b = ((get_json(DETAIL % "600519.SH" + q + "&freq=weekly").get("data") or {})
                 .get("chips"))
            same = (_card_only(a) == _card_only(b))
            if same:
                break
        check("筹码三块与 K 线周期无关（同一天 daily / weekly 拿到的 chips 逐字相等）",
              same is True, "相等" if same else "两次不一致")

        # ---- 图上的增减持 marks：跟着自己的 K 线网格走（同一天、同一批公告，行号不同）----
        # 用一个**确定有公告**的查询日（600519 在 2025-09-02 / 2025-12-30 各有一次增持），
        # 否则这段就是"空对空"，验不出东西。
        q2 = f"?strategy_id={SID_DAILY}&bars=200&trade_date=2026-03-31"
        ad = ((get_json(DETAIL % "600519.SH" + q2 + "&freq=daily").get("data") or {})
              .get("chips") or {}).get("trade") or {}
        bw = ((get_json(DETAIL % "600519.SH" + q2 + "&freq=weekly").get("data") or {})
              .get("chips") or {}).get("trade") or {}
        am, bm = ad.get("marks") or [], bw.get("marks") or []
        # ⚠ 两个周期的**窗口长度不一样**（日K 200 根 ≈ 10 个月，周K 200 根 ≈ 4 年），
        #   所以不能比"条数相同"；要看的是**对齐口径**：每条都落在"公告日当天或之后"
        #   的那根上，且行号按各自的网格算（两个网格的 idx 必然不同）。
        check("增减持 marks 跟着自己的 K 线网格走：每条都落在「公告日当天或之后的第一根」上",
              bool(am) and bool(bm) and
              all(m["idx"] >= 0 and m["date"] >= m["ann_date"] for m in am + bm) and
              [m["idx"] for m in am] != [m["idx"] for m in bm],
              f"日K {len(am)} 个 {[(m['idx'], m['date']) for m in am]} / "
              f"周K {len(bm)} 个 {[(m['idx'], m['date']) for m in bm]}")
    except Exception as e:  # noqa: BLE001
        check("筹码与两融（股东户数 / 融资融券 / 增减持）", False, f"{type(e).__name__}: {e}")

    # 坏代码：要明确报错，不是 500 / 空对象
    try:
        r = get_json(DETAIL % "999999.SZ" + "?bars=60")
        check("不存在的代码返回明确错误（不是 500、不是空白图）",
              r.get("ok") is False and "error" in r, str(r.get("error"))[:80])
    except Exception as e:  # noqa: BLE001
        check("不存在的代码返回明确错误", False, f"{type(e).__name__}: {e}")

    # 查询框：代码 / 6 位数字 / **名称** 都认（2026-10-10 需求「查询条件说可以输入名称和代码，
    # 实际只能输入代码，希望也可以输入股票名称」）。
    # ⚠ 一律用**接口给回来的名字**反查，不写死「贵州茅台」这类字面量 —— 写死了，哪天那只票
    #   改了名/退了市，断言就跟着红，而代码根本没坏（假红主因，见 MEMORY 硬规矩 15）。
    print("\n【2e】查询框：代码 / 6 位数字 / 名称都认")
    try:
        c0 = CODES[0]
        d0 = (get_json(DETAIL % c0 + "?bars=60").get("data") or {})
        nm, sym = d0.get("name") or "", c0.split(".")[0]
        check(f"先取到 {c0} 的名字与 6 位数字（下面拿它俩反查）", bool(nm), f"{nm} / {sym}")

        for label, q in (("完整代码", c0), ("6 位数字", sym), ("名称", nm)):
            r = get_json(DETAIL % urllib.parse.quote(q) + "?bars=60")
            got = ((r.get("data") or {}).get("ts_code") or "") if r.get("ok") else ""
            check(f"查询框输「{label}」（{q}）查到的还是同一只 {c0}",
                  got == c0, f"→ {got or r.get('error')}")

        # 多义：**绝不挑第一条**，把候选交回来。用「银行」这种必然有好几只的词，
        # 不用「平安」—— 哪天只剩一只平安，红的该是数据而不是代码。
        r = get_json(DETAIL % urllib.parse.quote("银行") + "?bars=60")
        err = str(r.get("error") or "")
        check("重名（输「银行」）不瞎猜：报错并把候选写在文案里",
              r.get("ok") is False and "匹配到" in err
              and (".SZ" in err or ".SH" in err), err[:120])

        r = get_json(DETAIL % urllib.parse.quote("zzz不存在的票zzz") + "?bars=60")
        check("什么都不是的输入给「没找到」（不是 500、不是空白图）",
              r.get("ok") is False and "没找到" in str(r.get("error") or ""),
              str(r.get("error"))[:100])
    except Exception as e:  # noqa: BLE001
        check("查询框认名称", False, f"{type(e).__name__}: {e}")

    try:
        page = get("/stock/600519.SH")
        check("个股诊断页能打开且引用了新的图表容器",
              'id="chart-stock"' in page and "个股诊断" in page, f"{len(page)} 字节")
    except Exception as e:  # noqa: BLE001
        check("个股诊断页能打开", False, f"{type(e).__name__}: {e}")

    # ---------------------------------------------------------------- 3. 浏览器实测
    if e2e and CHROME:
        print("\n【3】浏览器实测（真拖时间轴 + 真切指标）")
        import shutil
        profile = os.path.join(tempfile.gettempdir(), "_stock_probe_profile")
        shutil.rmtree(profile, ignore_errors=True)
        try:
            body = chrome_probe_out(BASE + PROBE, 90000, 420, profile)
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
        except Exception as e:  # noqa: BLE001
            check("浏览器实测执行完成", False, f"{type(e).__name__}: {e}")
        finally:
            shutil.rmtree(profile, ignore_errors=True)

        # 第二支探针：K 线卡片上的「日K / 周K」分段按钮。单看模板只能知道「按钮在 HTML 里」，
        # 验不出它在预览模式里是不是被一起隐藏、点了链路通不通、图有没有真换一份。
        print("\n【3b】浏览器实测（K线周期分段切换，预览模式）")
        profile2 = os.path.join(tempfile.gettempdir(), "_stock_freq_profile")
        shutil.rmtree(profile2, ignore_errors=True)
        try:
            run_probe_checks(chrome_probe_out(BASE + PROBE_FREQ, 120000, 480, profile2), "周期切换")
        except Exception as e:  # noqa: BLE001
            check("周期切换探针执行完成", False, f"{type(e).__name__}: {e}")
        finally:
            shutil.rmtree(profile2, ignore_errors=True)
    else:
        skip("浏览器实测", "无 Chrome 或指定了 --no-e2e")

    drop_probe_strategies()
    print(f"\n{'=' * 62}\n个股诊断图表回归：{PASS} 通过 / {FAIL} 失败 / {SKIP} 跳过")
    if FAILS:
        print("失败项：")
        for x in FAILS:
            print("  -", x)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
