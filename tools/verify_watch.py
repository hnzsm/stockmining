#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""「我的关注（自选分组 + 提醒）」与「东财新概念」回归。

为什么需要它：这两块的实现里，能静默出错的地方都不在「页面长得对不对」上：
  · **提醒判定**的三个动作（上穿 / 下穿 / 触及）只差一个比较符号 ——
    写反了照样出结果，只是结果恒为「不命中」或「天天命中」，光看页面看不出来；
  · **线 / 价位二选一**：`line` 为空才用 `value`，写反了会拿一个数去跟均线比，
    算出来是 NaN → 条件恒假 → 「设了提醒但永远不响」；
  · **同一条规则同一天只记一条**：靠唯一键兜，改表结构时最容易漏；
  · **新概念是「每次刷新都真抓」**（2026-10-08 取消本地缓存）—— 断言要跟着走：
    还按老口径去测「今天已抓过就跳过」，回归必红；
  · **提醒弹窗的默认值**：用户要的是「上穿 MA10」那种，默认却停在「绝对价位」时，
    静态断言照样全绿（代码里确实有 MA10 这个 option）—— 只有真开弹窗才知道迎面是个价格框；
  · **周K 提醒**（2026-10-08 需求「提醒也可以添加周线指标提醒」）：周K 判的是**周线那一根**
    （数据源 `weekly_kline`，只含已收盘的完整周），不是「把周线均线贴到日线上」。
    两处最容易静默出错：①回溯窗口按**周**算 —— 按天算就只有 20 来根周线，MA60 全 NaN，
    规则**永远不响**；②日志日期写**该周最后一个交易日** —— W-FRI 标签可能落在休市日
    （国庆那种周），直接写标签，命中会从 `feed()` / `summary()` 里凭空消失；
  · 一张卡片高度可控、标签云不撑破，这些只有真渲染才知道。

  · **拖动换成组**（2026-10-08 需求「鼠标选中自选股可拖动至某分组，拖动可选复制到或移动到」）：
    两条路（落在分组行**左半 = 移动到 / 右半 = 复制到**）在代码里只差一个字符串比较 ——
    写反了不报错，用户以为复制了、其实整只票从原组消失了。左右半判定还必须用
    `clientX`（`offsetX` 是相对**事件目标**算的，划过行里的 <span> 时会跳）；
    另外「移动」在后端是**先加到目标组、再删源组**，顺序反了会把提醒规则当孤儿清掉。

七段：
  【1】静态口径 —— 模板里必须有的东西（口径文案、guardClick、导航、两张卡、探针钩子、拖拽接线）
  【2】纯函数语义 —— **喂合成 K 线给真的判定函数**（不碰库、不依赖某天恰好有行情）
  【3】接口契约 —— 临时分组走一遍 增 / 查 / 改 / 删；提醒规则 CRUD；检出 / 触发记录
                   （默认区间=1 天、每条带**该触发那天**的涨跌幅，逐条回 daily_kline 核；
                    **按分组取数**：合成两个临时组 + 合成命中记录，验「只出本组的」、
                    「一只票在两组成员里两边都看得到」、「不存在的组给空列表」）；
                   拖到别的组（复制 / 移动 / 同组 / 备注 / 规则不丢 / 乱传 mode）；
                   **触发提醒列表的「删」**：临时规则走一遍删 —— 连带清掉它**全部**触发记录、
                   同票其他规则与自选股一行不动、删一条不存在的如实回 `deleted=False`；
                   新概念的「每次都真抓（skipped 恒为 false）」
  【4】浏览器实测 —— 同源 iframe 加载真页面，比对 DOM 与页面钩子（静态探针页）；
                   触发记录：默认 1 天、涨跌幅列有值、**真点表头**换行序、点「#」回原序、
                   **真点另一个分组**（列表跟着换 + 每行都属于该组 + 与接口 group_id=X 对齐）、
                   **真点一次「删」但只点「取消」**（列在不在 / 按钮带的是不是真 alert_id /
                   confirm 说了什么 / 取消后那条规则还在 —— 这页**绝不真删**，真删那条路在【3】）
  【5】K线入口实测 —— 真点五个 K线按钮，读弹窗 iframe 的 src，确认都带 sel_only=1
                   （这五处都没绑买卖策略；漏了页面照常开，只多一张会误导的「收益」卡。
                    最新动态表里的票名还要**锚在变更那天**，不是全锚到今天）
  【6】提醒弹窗实测 —— 真开一次弹窗：默认选中哪条线 / 下拉顺序 / 一键按钮 / 周期下拉；
                   再真点「上穿 MA10」与「周K 上穿 MA10」，确认两条路都通、且落到不同周期
  【7】拖拽实测 —— **合成拖放事件**真拖两次：落左半 ⇒ 移动、落右半 ⇒ 复制；
                   再验勾选后拖一行 = 拖整批（这几条静态断言证明不了）

跑法：
    C:/Users/ming/.workbuddy/binaries/python/envs/stockmining/Scripts/python.exe tools/verify_watch.py
    加 --no-e2e 只跑【1】【2】【3】（不用起浏览器）。
依赖：服务在 127.0.0.1:8777；本机 Chrome（找不到就把【4】【5】【6】【7】标 SKIP，不算失败）。
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

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BASE = "http://127.0.0.1:8777"
PROBE = "/static/_probe_watch.html?run=1"
WATCH_HTML = os.path.join(ROOT, "templates", "watch.html")
INDEX_HTML = os.path.join(ROOT, "templates", "index.html")
BASE_HTML = os.path.join(ROOT, "templates", "base.html")
APP_CSS = os.path.join(ROOT, "static", "css", "app.css")
APP_PY = os.path.join(ROOT, "app.py")
PROBE_HTML = os.path.join(ROOT, "static", "_probe_watch.html")
PROBE_KLINE = "/static/_probe_kline_src.html?run=1"
PROBE_ALERT = "/static/_probe_alert_form.html?run=1"
PROBE_DRAG = "/static/_probe_drag.html?run=1"
# 端到端「真删」那条路：`?gid=&code=` 由 part8 现填（数据也是 part8 现造的）
PROBE_FLOW = "/static/_probe_flow.html?run=1"
TMP_GROUP = "ZZ·回归临时组"
TMP_GROUP_K = "ZZ·K线探针组"
TMP_GROUP_A = "ZZ·提醒探针组"
TMP_GROUP_T1 = "ZZ·拖拽源"
TMP_GROUP_T2 = "ZZ·拖拽目标"
TMP_GROUP_T3 = "ZZ·拖拽副本"
TMP_GROUP_BL = "ZZ·布林下轨组"
# 「触发提醒只显示当前分组」用：两个临时组用来验「按组取数」与「一只票在两个组里两边都看得到」
TMP_GROUP_G1 = "ZZ·分组过滤A"
TMP_GROUP_G2 = "ZZ·分组过滤B"
TMP_GROUP_FL = "ZZ·删链路组"

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


def req(method: str, path: str, data: dict | None = None, timeout: int = 90) -> dict:
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(BASE + path, data=body, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(r, timeout=timeout).read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:                                    # noqa: BLE001
            return {"ok": False, "error": f"HTTP {e.code}"}


def read(p: str) -> str:
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def kline_calls(src: str) -> list[str]:
    """抠出每一处 `klinePreview({...})` 的**实参原文**（按括号配对，不是正则偷懒）。

    ⚠ 别用 `\\((.*?)\\)`：参数里本来就有 `(items.find(...) || {})` 这种带 `})` 的写法，
    正则会提前收尾，于是「带没带 sel_only」变得看不出来（假绿）。
    """
    out = []
    for m in re.finditer(r"klinePreview\(", src):
        i = m.end() - 1                       # 停在 '('
        depth, j = 0, i
        while j < len(src):
            if src[j] in "([{":
                depth += 1
            elif src[j] in ")]}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        out.append(src[i + 1:j])
    return out


# ---------------------------------------------------------------------------
# 【1】静态口径
# ---------------------------------------------------------------------------

def part1() -> None:
    print("\n【1】静态口径（模板里必须有的东西）")
    w = read(WATCH_HTML)
    ix = read(INDEX_HTML)
    bs = read(BASE_HTML)

    check("「我的关注」进了侧栏导航，并带一个角标位",
          '/watch' in bs and 'nav-watch-badge' in bs and "page=='watch'" in bs)

    check("提醒口径**写在页面上**（上穿按收盘价 / 触及是盘中 / 前复权）",
          "收盘价" in w and "盘中" in w and "前复权" in w and "只记一条" in w)

    check("线名与动作**不写死在前端**，由后端模板变量注入",
          "{{ lines|tojson }}" in w and "{{ ops|tojson }}" in w and "const LINES" in w)

    # 提交类按钮必须走 guardClick（C38）；本地瞬时控件不该锁
    guards = re.findall(r"\$\('#(btn-new-group|btn-add-stock|btn-check|btn-boll-lower|nc-refresh|wf-check)'\)\.onclick\s*=\s*guardClick", w + ix)
    check("提交类按钮全部走 guardClick（防连点三道保护）",
          len(guards) == 6, f"命中 {sorted(guards)}")
    check("换区间下拉**不**走 guardClick（本地瞬时控件不锁，C38）",
          "$('#feed-days').onchange = loadFeed" in w)

    check("总览页两张卡都在（东财新概念 + 触发提醒），且各有自己的锚点",
          'id="nc-list"' in ix and 'id="wf-list"' in ix and 'id="nc-tabs"' in ix)

    # ---- 总览「触发提醒」卡：点票名开预览 + 「股票标记」列（2026-10-11）----
    # 需求原话：「总览中的我的关注触发提醒那块，k线预览不用按钮，点击股票名称实现就行，
    # 把股票的文本标记那列也拉出来」。两处都会「看着对、其实没生效」：
    #   ① 按钮撤了、名字却没带 data-k/data-d ⇒ 整张卡谁也点不开（不报错，只是点不动）；
    #   ② 「股票标记」列铺的是 `x.note`（**这条提醒**的备注）—— 那张卡本来就有个「备注」列，
    #      两个字段长得像，铺错了页面上一样是行文本，肉眼看不出来。
    check("总览触发提醒卡：K线按钮整列撤掉，改成点票名（`.wf-k` 带 data-k/data-d 锚点）",
          'class="wf-k" data-k="${esc(x.ts_code)}" data-d="${esc(x.trade_date)}"' in ix
          and '<button class="btn sm" data-k=' not in ix)
    check("总览触发提醒卡多了「股票标记」列，取自 `stock_note`（不是提醒的 `note`）",
          ">股票标记</th>" in ix and "x.stock_note" in ix)
    _wfpy = read(os.path.join(ROOT, "core", "watch.py"))
    check("后端 feed 带出 `stock_note`：单独查 `watch_stock` 收成映射回填（不是 JOIN 进来放大行数）",
          "snote: dict = {}" in _wfpy
          and 'snote.setdefault(s["ts_code"], s["note"])' in _wfpy
          and '"stock_note": snote.get(r["ts_code"], "")' in _wfpy)

    check("总览新概念卡的两个 tab 是**两个口径**，文案里分开写了",
          "板块视角" in ix and "成分变更流水" in ix and "不是同一个口径" in ix)

    # ---- 新概念卡的两条展示口径（2026-10-11）----
    # ①「新概念」清单只摆**创建未满 365 天**的（用户要求「超过 365 天的概念不展示」）；
    # ②「最新动态」表里的**股票那列**也要能点开 K线预览（原来只有新概念那边能点）。
    nc_py = read(os.path.join(ROOT, "core", "newconcept.py"))
    check("「新概念」展示窗口 = 365 天，判据只有一处（常量 MAX_AGE_DAYS）",
          "MAX_AGE_DAYS = 365" in nc_py and nc_py.count("MAX_AGE_DAYS") >= 4
          and 'it["age_days"] is None or it["age_days"] <= MAX_AGE_DAYS' in nc_py)
    check("365 天过滤发生在「取前 new_n 条」**之前**（摆的是合龄的概念里最新的 N 条）",
          nc_py.index("fresh = [it for it in cand") < nc_py.index("items = fresh[:new_n]"))
    check("徽标 / note 按**过滤后**的条数报（不是库里全部），被剔的条数如实说出",
          '"new_total": len(fresh)' in nc_py and '"new_hidden": hidden' in nc_py
          and 'note = f"新概念 {len(fresh)} 个"' in nc_py)
    check("最新动态 tab 的「股票」列可点（.nc-k + data-d = 变更日 → 锚在那天）",
          'class="nc-k" data-c="${esc(e.ts_code)}" data-d="${esc(e.change_date)}"' in ix)
    check("两个 tab 共用同一个绑定入口 bindNCKline()（不是各写一套）",
          "function bindNCKline(){" in ix and ix.count("bindNCKline();") == 2
          and ix.count("$$('#nc-list .nc-k').forEach") == 1)
    check("代表股那栏**没有** data-d（仍锚本库最新交易日，别乱锚）",
          ix.count('data-d="${esc(e.change_date)}"') == 1)

    # ⚠ 2026-10-08 取消本地缓存：卡片每次刷新都真抓。别再说「今天抓过就用缓存」（老口径）。
    check("总览写清了「每次刷新都实时抓」，且旧口径文案已清干净",
          "实时抓取于" in ix and "每次刷新" in ix
          and "每天最多抓一次" not in ix and "本地缓存于" not in ix)

    check("关注页有探针钩子（回归要比对 DOM 与页面状态）",
          "window.__watchDebug" in w and "groups:" in w and "stocks:" in w)

    # ---- K线预览入口的口径（C8）：这一页与总览两张卡都**没有绑定买卖策略** ----
    # 不带 `sel_only=1` 的话诊断页会拿默认买卖参数跑一遍单票回测、摆出第 4 张「收益」卡，
    # 而那个收益跟「我关注的这几只走得怎么样」毫无关系 —— 2026-10-07 加这两个入口时就漏过。
    # 用括号配对取实参，别用 `\((.*?)\)`：参数里本来就有 `|| {}` 这种带 `})` 的写法，
    # 正则会在那儿提前收尾，于是「带没带 sel_only」看不出来（假绿）。
    calls = kline_calls(w + ix)
    check("K线预览入口至少 4 处（关注页 2 + 总览 2）", len(calls) >= 4, f"抓到 {len(calls)} 处")
    check("每一处 K线预览都带 sel_only=1（不套默认买卖策略、不算策略收益）",
          bool(calls) and all("sel_only" in c for c in calls),
          "漏了 " + "; ".join(c.strip()[:70] for c in calls if "sel_only" not in c))

    # 「看当日命中」那两处（关注页的触发列表 / 总览的触发列表）要锚在**该条触发那天**
    check("触发列表的 K线按钮带「那一天」（看当日命中，不是都锚到今天）",
          w.count('data-d="${esc(x.trade_date)}"') == 1
          and ix.count('data-d="${esc(x.trade_date)}"') == 1
          and "trade_date: b.dataset.d" in w and "trade_date: b.dataset.d" in ix)
    check("关注页自选股的 K线锚在表格那两列的同一天（trade_date 由接口给）",
          "trade_date: STK_DATE" in w and "STK_DATE = d.trade_date" in w)

    # ---- 自选股表的「代码」列 → 个股诊断页（2026-10-09）----
    # 口径与 /pools、/screen 一致：/stock/<ts_code> 新标签打开。
    # （总览热股榜不在此列：2026-10-11 起它整行点开的是 K线预览弹窗。）
    # 只用 <a href> 不用 JS 跳转 —— 表重画（排序）后绑定会全丢（C52），
    # 而 href 是 DOM 属性，重画自动带上，天然免疫。
    check("「代码」列是链接，href 走唯一入口 stkUrl()",
          'class="stk-code" href="${esc(stkUrl(s.ts_code))}"' in w
          and "function stkUrl(code){" in w
          and "'/stock/' + encodeURIComponent(String(code || ''))" in w)
    check("代码链接新标签打开（不顶掉「我的关注」这页）",
          re.search(r'class="stk-code"[^>]*target="_blank"', w) is not None)
    check("代码链接 draggable=false（行本身可拖，别让 <a> 抢走拖拽）",
          re.search(r'class="stk-code"[^>]*draggable="false"', w) is not None)
    check("拖拽的排除名单里有 a（从链接按下不算拖股票）",
          "closest('button,input,a,select')" in w)

    # 侧栏角标脚本会在**每个页面**打一次 /api/watch/summary —— 分时监控页必须排除掉，
    # 那一页是纯前端，本站 /api 只允许「交易日历 + 日K线」两条（verify_monitor 锁着）。
    check("角标脚本不在分时监控页打接口（否则破坏监控页的「只有两个本站接口」）",
          "page != 'monitor'" in bs)

    # ---- 提醒弹窗的默认口径 ----
    # 2026-10-08 用户反馈：「我之前说的是**上穿 MA10** 那种提醒，不是触发价格的提醒」——
    # 当时下拉第一项是「绝对价位」，一开弹窗迎面就是个价格输入框，把最常用那条路藏进了下拉尾巴。
    from core import watch as _wt
    check("提醒表单默认：判 MA10 + 动作上穿（不是「绝对价位」）",
          _wt.DEFAULT_LINE == "ma10" and _wt.DEFAULT_OP == "cross_up",
          f"{_wt.DEFAULT_LINE} / {_wt.DEFAULT_OP}")
    check("「线」带分组标签，分组顺序里均线在布林前面",
          _wt.LINE_GROUPS == ["均线", "布林"]
          and all(len(v) == 3 for v in _wt.LINES.values())
          and _wt.LINES["ma10"][2] == "均线" and _wt.LINES["boll_mid"][2] == "布林")
    # ⚠ 保序清单：Flask 的 tojson 会把 dict 的键按**字典序**输出（MA5 会跑到 MA10/MA20 后面）
    check("均线顺序 MA5→MA10→MA20→MA60（靠保序清单，不靠 dict 键序）",
          _wt.LINE_ORDER[:4] == ["ma5", "ma10", "ma20", "ma60"],
          str(_wt.LINE_ORDER))
    check("常用规则里有一键「上穿 MA10」（日K 那条；周K 的那条在下面单独断言）",
          any(q[0] == "daily" and q[1] == "ma10" and q[2] == "cross_up"
              for q in _wt.QUICK_RULES))
    check("模板用后端给的保序清单排下拉（没回退到会乱序的 Object.keys）",
          "LINE_ORDER.filter" in w and "OP_ORDER.map" in w
          and "Object.keys(LINES).filter" not in w and "Object.keys(OPS).map" not in w)
    check("弹窗有「常用规则」一键区，且接线走 guardClick（C38：它也是提交类）",
          'id="al-quick"' in w
          and "$$('#al-quick [data-q]').forEach(b => b.onclick = guardClick" in w)
    check("价格提醒仍在（「绝对价位」只是挪到下拉最后，没被砍掉）",
          "绝对价位（不判线）" in w,
          "「绝对价位」应作为唯一一项留在下拉末尾的「其他」分组里")

    # ---- 周期（2026-10-08 需求「提醒也可以添加周线指标提醒」）----
    check("周期清单与默认值来自后端（日K / 周K，默认日K）",
          _wt.FREQS == {"daily": "日K", "weekly": "周K"}
          and _wt.FREQ_ORDER == ["daily", "weekly"] and _wt.DEFAULT_FREQ == "daily",
          str(_wt.FREQS))
    check("一键规则是四元组（周期, 线, 动作, 文案），周K 的都带「周K」前缀、日K 的不带",
          all(len(q) == 4 for q in _wt.QUICK_RULES)
          and any(q[0] == "weekly" and q[1] == "ma10" and q[2] == "cross_up"
                  for q in _wt.QUICK_RULES)
          and all(q[3].startswith("周K") for q in _wt.QUICK_RULES if q[0] == "weekly")
          and all(not q[3].startswith("周K") for q in _wt.QUICK_RULES if q[0] == "daily"),
          "共 %d 条（周K %d 条）" % (len(_wt.QUICK_RULES),
                                  len([q for q in _wt.QUICK_RULES if q[0] == "weekly"])))
    ap = read(APP_PY)
    check("页面与 /api/watch/meta 都透传了「周期三件套」（少传一个页面就静默降级成日K only）",
          "freqs=watch.FREQS" in ap and '"freqs": [{"key": k, "label": v}' in ap
          and "freq_order=watch.FREQ_ORDER" in ap and "default_freq=watch.DEFAULT_FREQ" in ap
          and '"default_freq": watch.DEFAULT_FREQ' in ap and '"freq_order": watch.FREQ_ORDER' in ap)
    check("服务端存规则时收 freq，且改老规则不带 freq 时**保留原周期**（不静默改回日K）",
          "freq=freq or watch.DEFAULT_FREQ" in ap and "SELECT freq FROM watch_alert WHERE id=%s" in ap)
    check("周期不写死在前端：下拉 id=al-freq、常量来自后端模板变量",
          'id="al-freq"' in w and "const FREQS = {{ freqs|tojson }}" in w
          and "const FREQ_ORDER = {{ freq_order|tojson }}" in w
          and "const DEF_FREQ = {{ default_freq|tojson }}" in w)
    check("提交时把周期一起带上（一键按钮与「添加」两条路都带）",
          "freq: q[0]" in w and "freq: $('#al-freq').value" in w)
    check("页面上写明「周K 要等这周走完才判、一周最多一条」",
          "要等这周走完" in w and "一周最多出一次结果" in w)
    check("触发记录里的 K线按钮带得动周期（周K 命中要按周线开图，否则图上看不到那根周线）",
          "data-f=" in w and "x.freq || 'daily'" in w and "freq: b.dataset.f" in w)

    # ---- 拖动自选股到别的分组（2026-10-08 需求「拖动可选复制到或者移动到」）----
    css = read(APP_CSS)
    check("自选股每一行都可拖（draggable），首列有勾选框、表头有全选（一次拖一整批）",
          'draggable="true" data-code=' in w and 'data-pick=' in w and 'id="stk-all"' in w)
    check("分组行里预置了「左半 move / 右半 copy」两个投放区 —— 提示「复制到还是移动到」不能靠修饰键",
          'class="dzb dz-move" data-drop="move"' in w
          and 'class="dzb dz-copy" data-drop="copy"' in w
          and ".grp.hot .dz{display:flex}" in css and ".grp .dz .dzb.on{" in css)
    check("左右半判定用 clientX 与行左沿算（**不用 offsetX** —— 它相对事件目标，划过行内 <span> 会跳）",
          "const half = (e.clientX - r.left) < r.width / 2" in w and "e.offsetX" not in w)
    check("拖动状态存在 JS 变量上、不读 dataTransfer 的内容（dragover 阶段本来就读不到）",
          "let DRAG = null;" in w and "DRAG = { codes: [...PICK], fromGid: GID }" in w)
    check("投放区只在**鼠标划过的那一行**展开（31 个组全展开就糊成一片、看不出拖到哪了）",
          "el.classList.add('hot')" in w and "$$('#grp-list .grp').forEach(clearHot)" in w)
    check("拖放只打一个 transfer 接口（不自己拼「删一只 + 加一只」两次请求）",
          "/api/watch/stocks/transfer" in w and "from_group_id" in w and "to_group_id" in w)
    check("拖放是提交类动作 ⇒ 走 guardClick（C38）",
          "el.addEventListener('drop', guardClick(el," in w)
    check("后端有 /api/watch/stocks/transfer 路由，把 mode 收下来并交给 watch.transfer_stocks",
          '"/api/watch/stocks/transfer"' in ap and "watch.transfer_stocks" in ap
          and 'b.get("mode") or "move"' in ap)
    check("「先加到目标组、再删源组」写在后端唯一一处（先删会把提醒规则当孤儿清掉）",
          "先加到目标组、再删源组" in read(os.path.join(ROOT, "core", "watch.py"))
          and "if mode == \"move\" and touched:" in read(os.path.join(ROOT, "core", "watch.py")))

    # ---- 自选股表：点表头排序（2026-10-08 需求「可点击表头进行顺序排列」）----
    wp = read(os.path.join(ROOT, "core", "watch.py"))
    check("自选股表头可点排序：6 个值列 + 「#」回原序（复用选股结果页那套 .sortable/.sarrow）",
          "const STK_TEXT_COLS = { ts_code: 1, name: 1, note: 1 }" in w
          and "stkTh('#', '__orig', '', '点击恢复原始顺序（按加入先后）')" in w
          and '<span class="sarrow"></span>' in w
          and "table.data th.sortable .sarrow::after" in css)
    check("排序口径与选股结果页同源：缺值沉底 / 同值保原序 / 文本列升序·数值列降序",
          "const stkMissing" in w and "永远沉底" in w
          and "if (!STK_SORT.key) return its.slice();" in w
          and "localeCompare(String(vb), 'zh')" in w
          and "Number(va) < Number(vb) ? -1" in w)
    check("点表头是**本地重画**（只排已拿到的那一批，不重新请求接口）",
          "$$('#stk-list th[data-sort]').forEach(th => th.onclick = () => {" in w
          and "paintStocks();" in w)
    check("表头排序**不套 guardClick**（C38 点名：本地瞬时动作锁了连续点会很黏手）",
          "th[data-sort]').forEach(th => th.onclick = () => {" in w
          and "guardClick" not in w.split("th[data-sort]').forEach")[1].split("});")[0])
    msrc = re.search(r"function paintStocks\(\)\{([\s\S]*?)\n\}", w)
    check("重画时把绑定**全部**重挂（漏一个就「排一次序，按钮就点不动了」）",
          bool(msrc) and all(k in msrc.group(1) for k in
                             ("[data-alert]", "[data-rm]", "[data-k]", "[data-pick]",
                              "th[data-sort]", "attachDrag()", "paintPick()")))
    check("「#」回的是后端原序（`ORDER BY sort, id` = 加入先后），不是「按序号排」",
          "ORDER BY sort, id" in wp and "点击恢复原始顺序（按加入先后）" in w)
    check("K线弹窗的名单交给**屏幕上这个顺序**（排完序后「下一个」才跟列表对得上）",
          "const i = +b.dataset.k, s = view[i];" in w
          and "list: view, index: i" in w)

    # ---- 布林下轨批量加提醒（2026-10-08 需求「对日k在布林下轨的股票全部添加…提醒」）----
    # 这一段能静默出错的地方：口径在页面/后端各写一个（0.1 两处走样）、
    # dry_run 其实偷偷写了库、批量入口跳过去重（连点两下堆出一串一样的规则）。
    check("「布林下轨加提醒」按钮在页面上，且走 guardClick（写上百条规则，防连点）",
          'id="btn-boll-lower"' in w and "guardClick($('#btn-boll-lower')" in w)
    check("按钮是**两步走**：先 dry_run 预演、再由确认框里那个按钮真写",
          re.search(r"boll_lower',\s*\{\s*dry_run:\s*1\s*\}", w) is not None
          and re.search(r"boll_lower',\s*\{\s*\}", w) is not None)
    check("页面只打这一个批量接口（不在前端自己拼「逐只加规则」两次请求）",
          w.count("/api/watch/alerts/boll_lower") == 2,
          f"出现 {w.count('/api/watch/alerts/boll_lower')} 次")
    check("确认框里那句「布林位置 ≤ …」读**后端来的**阈值，没写死 0.1",
          "const BOLL_POS = {{ boll_pos|tojson }}" in w
          and "布林位置 ≤ ${BOLL_POS}" in w and "布林位置 ≤ 0.1" not in w)
    check("后端口径常量 + 三个入口都在（阈值 / 纯判定 / 扫描 / 批量写）",
          all(k in wp for k in ("BOLL_LOWER_POS = 0.1", "def boll_lower_hit",
                                "def scan_boll_lower", "def add_boll_lower_alerts")))
    check("「在布林下轨」的阈值**只有一处**定义（别处再各写一个 0.1 就会跟判定分家）",
          wp.count("BOLL_LOWER_POS = 0.1") == 1 and "pos <= BOLL_LOWER_POS" in wp)
    check("批量写规则**跳过已有的**（幂等：连点两下不会堆出一串一样的规则）",
          "AND line=%s AND op=%s" in wp and "todo = [c for c in want if c not in have]" in wp)
    check("「已有」判据只按 (周期, 线, 动作) 查，**不看启用状态**（不绕开用户按下的暂停）",
          'SELECT ts_code FROM watch_alert WHERE freq=%s AND line=%s AND op=%s' in wp)
    check("`dry_run` 下**不写库**（预演必须是只读的）", "if todo and not dry_run:" in wp)
    app_src = read(APP_PY)
    check("批量路由在，且把阈值/备注透传给模板（页面上那句「≤ …」跟着后端走）",
          '"/api/watch/alerts/boll_lower"' in app_src
          and "boll_pos=watch.BOLL_LOWER_POS" in app_src)

    # ---- 触发记录：默认区间 / 涨跌幅列 / 可点表头排序 ----
    # （2026-10-08 需求原话：「提醒列表默认展示最近1日的，列表也增加涨跌幅字段，可点击字段大小排序」）
    # 能静默出错的三处：①页面默认改成 1 天、接口默认还留着 7 天（默认值两处各写一个）；
    #   ②涨跌幅拿「最新交易日」那一列去填所有行（跨天的列表会把两个日期拼在一起）；
    #   ③「有排序函数」但点下去顺序不变（本地重画时忘了重挂绑定 / 表头没接上事件）。
    #   ⚠ 「接口默认也是 1」**不在这里按字符串验**（2026-10-09 教训：当初写死
    #     `int(request.args.get("days") or 1)`，后来把入参解析统一换成 `_int(...)`，
    #     实现一变这条就假红，可页面和接口其实都没错）。默认值按**行为**在【3】段验。
    check("触发记录默认区间改成「最近 1 个交易日」（页面选中项 + `feed()` 签名默认 1）",
          '<option value="1" selected>最近 1 个交易日</option>' in w
          and "def feed(days: int = 1" in wp,
          "页面选中 1 / feed() 默认 1（接口默认走【3】段行为断言）")
    check("触发记录加了「涨跌幅」列，且是可点排序的表头（代码 / 名称 / 涨跌幅 + 「#」回原序）",
          "feedTh('涨跌幅', 'pct_chg', 'num'" in w
          and "feedTh('#', '__orig'" in w
          and "const FEED_TEXT_COLS = { ts_code: 1, name: 1 }" in w)
    check("触发记录的排序是**本地重画**（只排已拿到的那一批，不重新请求接口）",
          "$$('#feed-list th[data-fsort]').forEach(th => th.onclick = () => {" in w
          and "paintFeed();" in w)
    check("触发记录的表头排序**不套 guardClick**（C38 点名：本地瞬时动作锁了连续点很黏手）",
          "guardClick" not in w.split("th[data-fsort]').forEach")[1].split("});")[0])
    check("排序只在**每个交易日内部**做（跨天混排会把「哪天命中」一起排没了）",
          "只在每个交易日内部" in w and "feedSorted(byDay[day])" in w)
    check("重画时把绑定**重挂**（K线按钮不重挂，就会「排一次序，K线点不动了」）",
          "$$('#feed-list [data-k]').forEach(b => b.onclick = ()" in w)
    check("页面写清了涨跌幅的口径（取该行触发那天，与自选股表同一个来源）",
          "该行触发那天的日涨跌幅" in w)
    check("触发记录仍按交易日分组（日期做成表内分隔行，多天不重复铺一遍表头）",
          'class="feed-sep"' in w and 'class="feed-day"' in w)

    # ---- 触发提醒列表的「删」= 只删这条提醒本身 ----
    # （2026-10-08 需求原话：「触发提醒列表添加删除提醒字段，只删除触发提醒的提醒」）
    # 容易做错的四处：①列头在、按钮却没带真 `alert_id`（点了删错人的规则）；
    #   ②不 `confirm` 就删（这个动作不可逆，还连带清掉历史触发记录）；
    #   ③删完只刷这张表、忘了自选股表的提醒胶囊也跟着变了；
    #   ④另起一个「只删 `watch_alert_log`」的新接口 —— 那规则还在，下次一检测又冒出来，
    #     于是跟自选股弹窗里那个「删」分裂成两套语义。所以这里卡「两处走同一个接口」。
    check("触发记录最右一列是「删除提醒」，按钮带真的 `alert_id`（`data-daid`）",
          "删除提醒</th>" in w and 'data-daid="${x.alert_id}"' in w)
    check("「删」走既有那个删规则接口（与自选股弹窗里的「删」同一套，不另起一套语义）",
          "$$('#feed-list [data-daid]').forEach(b => b.onclick = guardClick" in w
          and "post('/api/watch/alert/delete', { id: aid })" in w)
    check("「删」先 `confirm`，文案写明「删的是提醒本身 / 清历史触发记录 / 不动自选股」",
          "if (!confirm(`删除提醒：" in w and "以后不再提醒；它的历史触发记录一起清掉" in w
          and "不会动自选股" in w)
    _del_js = (w.split("$$('#feed-list [data-daid]')")[1].split("}));")[0]
               if "$$('#feed-list [data-daid]')" in w else "")
    check("删完**两张表都刷新**（触发列表 + 自选股表的提醒胶囊）",
          "await loadFeed()" in _del_js and "await loadStocks()" in _del_js)
    check("删一条已经不存在的提醒时**不谎报成功**（按接口回的 deleted 分岔）",
          "res.deleted" in w and "这条提醒已经不在了" in w)

    # ---- 触发提醒**只显示当前分组**的记录 ----
    # （2026-10-08 需求原话：「我的关注里，触发提醒只展示当前分组的提醒」）
    # 两种「看着对、其实会静默出错」的写法，都得卡住：
    #   ①把全量拉回来在前端 `filter` —— 看着也对，但 `limit` 窗口先被别的组占满，
    #     本组那几条就被挤掉了（页面写「暂无触发」，其实明明有命中）；跟 `LIMIT 100` 同一个坑。
    #   ②只有「点分组」换得动 —— 加票 / 移出 / 拖到别的组这些动作改了组内成分，却不刷新，
    #     列表就停在上一次那张上（用户看到的是「刚加进来的票怎么没记录」）。
    check("触发提醒按当前分组取数：`loadFeed()` 把 GID 带进接口（不是取回全量在前端筛）",
          "'&limit=500&group_id=' + gid" in w and "const gid = GID;" in w)
    # ⚠ 签名里的默认数字**不写死**（2026-10-09 教训：写死 `limit: int = 100`，后来把默认
    #   提到 500（默认 100 会静默截断当天 108 条），这条就假红 —— 而 SQL 过滤其实一点没动）。
    #   这里只认「签名形状 + SQL 里真有 EXISTS」；默认值本身在【3】段按行为验。
    check("后端把分组过滤写进 SQL（`EXISTS` 子查询按 watch_stock.group_id）",
          re.search(r"def feed\(days: int = \d+, limit: int = \d+, group_id=None\)", wp) is not None
          and "EXISTS (SELECT 1 FROM watch_stock ws" in wp,
          "签名带 group_id + SQL 用 EXISTS")
    check("路由 `/api/watch/feed` 认 `group_id`，乱传给 400（不静默回退成「全部」）",
          'request.args.get("group_id")' in ap
          and "group_id 必须是整数" in ap and "group_id 必须是正整数" in ap)
    check("改「组内成分」的动作都会刷新触发提醒（点分组 / 建组 / 加票 / 移出 / 拖到别的组）",
          "GID = +el.dataset.id; loadGroups(); loadStocks(); loadFeed();" in w
          and w.count("await loadGroups(); await loadStocks(); await loadFeed();") >= 3,
          f"三连刷新 {w.count('await loadGroups(); await loadStocks(); await loadFeed();')} 处")
    check("切组很快时丢掉过期响应（序号对不上就丢，免得上一组的记录盖住新组的）",
          "const seq = ++FEED_SEQ;" in w and "seq !== FEED_SEQ" in w)
    check("说明文字报出的是**哪个分组**（不是笼统的「最近 N 个交易日」）",
          "只显示<b>左边选中分组</b>" in w and "grpName(gid)" in w)
    check("触发提醒的钩子带 `feed_group`（探针靠它判断「换组真的换过来了」）",
          "feed_group: FEED_GID" in w)
    check("一个分组都没有时给「先在左边选一个分组」的提示，不是空列表干杵着",
          "先在左边选一个分组" in w)

    # ---- 提醒的第二种规则：绑定选股策略「入池即提醒」（2026-10-10） ----
    # 需求原话：「我的关注里面的提醒，希望可以通过绑定选股策略规则，如果选股命中则提醒」。
    # 这一段能静默出错的地方（静态能卡住的三处）：
    #   ① 类型切换只改了显示、提交口径没跟着改（切到「选股命中」点添加，存进去的是一条
    #      空的指标规则 —— 页面看着成功，实际永远不响）；
    #   ② 策略下拉在前端写死一份 / 或写死 `#3` 这种 id；
    #   ③ pool 判定自己另写一套表达式求值（不调 `strategy.compute_pool`）——
    #      那样「提醒说命中、选股页说没这只」只是时间问题。
    dbp = read(os.path.join(ROOT, "core", "db.py"))
    check("规则类型（kind）来自后端：模板注入 + 常量只有一处",
          "const KINDS = {{ kinds|tojson }}" in w and "const KIND_ORDER = {{ kind_order|tojson }}" in w
          and 'KINDS: dict[str, str] = {"price": "指标 / 价位", "pool": "选股命中"}' in wp)
    check("数据层：watch_alert 有 kind / pool_id，且老库有幂等迁移（老行默认 price ⇒ 行为不变）",
          "kind    VARCHAR(8) NOT NULL DEFAULT 'price'" in dbp
          and ("watch_alert\", \"kind\"" in dbp and "watch_alert\", \"pool_id\"" in dbp))
    check("提示类型是**二选一**的两块面板（不是在同一条表单上再加几个框）",
          'id="al-kind"' in w and 'id="al-price-box"' in w and 'id="al-pool-box"' in w
          and "AKIND === 'pool' ? 'none' : ''" in w)
    check("提交时按类型分岔：pool 档送 kind+pool_id+**周期**（不再送线/动作/价位）",
          re.search(r"ts_code: code, kind: 'pool', pool_id: pid, freq: fq", w) is not None
          and re.search(r"ts_code: code, kind: 'price', freq:", w) is not None)
    check("选股策略下拉真读接口（`/api/watch/pools`），不在前端写死",
          "await get('/api/watch/pools')" in w and 'id="al-pool"' in w)
    check("有「看这只票现在命中吗」预览，且走 guardClick（它虽只读，但会真跑一遍取数）",
          'id="al-pool-check"' in w and "guardClick($('#al-pool-check')" in w
          and "/api/watch/pool/hit" in w)
    # 2026-10-10 需求「绑定选股策略也应该要有日k周期和周k周期的选择」：
    #   pool 档的周期**不是摆设**（看日期、看 end），所以三处都得有——页面下拉、接口入参、检测分组键。
    check("选股命中也有「周期」下拉：id=al-pool-freq，选项来自后端 FREQ_ORDER（不写死）",
          'id="al-pool-freq"' in w and "${freqOpts}" in w
          and "$('#al-pool-freq').onchange = syncPoolFreq" in w)
    check("选股命中的两个周期写清楚：下拉只管「多久判一次」，条件口径由**策略自己的 timeframe** 决定",
          ("只判「这周走完后那一天」的收盘状态" in w or "这周走完后那一天" in w)
          and "按周线算，与选股页一致" in w)
    check("预览把周期一起送（否则「预览说命中、检测不响」）",
          re.search(r"pool_id: pid, codes: \[code\], freq: fq", w) is not None)
    check("pool 档的周期**不再被清空**，且照常校验合法性（选了周K 不会被默默降成日K）",
          'freq = (freq or DEFAULT_FREQ).strip() or DEFAULT_FREQ' in wp
          and "line, op, value = \"\", \"\", None" in wp
          and "不认识的周期：{freq}" in wp)
    check("pool 档的周K 判定点只有一处实现 `_pool_week_end`（与 price 档周K 同一口径：完整周）",
          "def _pool_week_end(" in wp and "market.complete_week_mask" in wp
          and "已收盘的完整周" in wp)
    check("检测按**（策略, 提醒周期）**分组跑：日K / 周K 的 `end` 不同，不能混在一组；"
          "且把 `freq` 一路传进 scan_pool（面板周期在这儿不判）",
          'by_key.setdefault((int(a["pool_id"]), fq), [])' in wp
          and "scan_pool(pid, codes, end=latest, lookback=lookback, freq=fq)" in wp
          and "eff = sc.get(\"freq\") or DEFAULT_FREQ" in wp)
    check("⚠ **面板周期 = 选股策略自己的 timeframe**（`selection_cfg` 一步抬到位）："
          "周K 策略走 compute_weekly_panel，daily 走 load_panel —— 这是「整条链按周K跑」的落点",
          "cfg = st.selection_cfg(cfg)" in wp
          and 'ptf = cfg["timeframe"]' in wp
          and 'if ptf == "weekly":' in wp
          and "market.compute_weekly_panel(codes, start, end," in wp
          and "market.load_panel(codes, start, end, freq=\"daily\", adjust=\"qfq\"," in wp)
    check("⚠ 实际节奏 `eff` = 「策略是周K 或 提醒选了周K」—— 周线只有走完的整周，"
          "周K 策略的 `end` 收敛到 `_pool_week_end`，文案前缀与周K 计数都用它",
          'eff = "weekly" if (ptf == "weekly" or out["freq"] == "weekly") else "daily"' in wp
          and 'end = _pool_week_end(end)' in wp
          and 'msg = _pool_msg(sc.get("pool_name") or "", hit["close"], eff)' in wp
          and "p_weekly += len(group)" in wp)
    check("⚠ 周线面板的 trade_date 是 W-FRI 标签（可能是休市日）⇒ 落库前映射成该周最后交易日",
          "wmap = _week_last_day() if ptf == \"weekly\" else {}" in wp
          and 'out["hits"][code] = {"date": wmap.get(lab, lab),' in wp)
    check("pool 判定**复用选股那条路**（compute_pool + 静态过滤 + 基础池），没自己另写一套求值",
          "st.compute_pool(df, cfg)" in wp and "market.apply_pool_filters(panel" in wp
          and "market.resolve_universe(base)" in wp
          and "def scan_pool(pid" in wp)
    check("池子已删 / 没绑策略 ⇒ 如实报错，**不退化成「空条件 = 全命中」**",
          "已不存在（可能已被删除）" in wp and "选股命中提醒必须挑一条选股策略" in wp
          and "out[\"error\"]" in wp)
    check("检测把两档分开跑（`price` 走 freq 那一段、`pool` 走 scan_pool），看板分开报数",
          "pool_alerts = [a for a in alerts if (a.get(\"kind\") or DEFAULT_KIND) == \"pool\"]" in wp
          and '"pool_alerts": len(pool_alerts), "pool_fired": p_fired' in wp
          and '"pool_weekly_alerts": p_weekly' in wp)
    check("触发记录里 pool 档单独上色/上字（胶囊写「选股命中·策略名」，周K 带「周K·」前缀）",
          "x.kind === 'pool'" in w and "'选股命中·' + (x.pool_name" in w
          and "(x.freq === 'weekly' ? '周K·' : '')" in w
          and "选股命中·" in wp)
    check("后端多了两个接口：策略下拉 `/api/watch/pools` + 预览 `/api/watch/pool/hit`",
          '@app.route("/api/watch/pools")' in ap and '@app.route("/api/watch/pool/hit"' in ap)
    check("页面写清了「选股命中」的口径（与选股页同一套、入池即提醒、池子改了跟着变）",
          "入池即提醒" in w or "入了那个池子就提醒" in w)
    check("删一条选股命中提醒**不动**它引用的选股策略（两件事别搅在一起）",
          "**不动**它引用的选股策略" in wp)


# ---------------------------------------------------------------------------
# 【2】纯函数语义（喂合成数据给真函数）
# ---------------------------------------------------------------------------

def part2() -> None:
    print("\n【2】纯函数语义（合成 K 线，不碰库）")
    from core import newconcept as nc
    from core import watch as wt

    # 一段人造日线：close 依次 10, 11, 9, 12；ma10 我们用一条常数列伪造
    df = pd.DataFrame({
        "trade_date": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-05", "2026-01-06"]),
        "open": [10.0, 10.0, 11.0, 9.0],
        "high": [10.4, 11.2, 11.2, 12.2],
        "low": [9.6, 9.9, 8.8, 11.8],
        "close": [10.0, 11.0, 9.0, 12.0],
        "ma10": [10.5, 10.5, 10.5, 11.0],
    })

    # i=1：close 10.0 -> 11.0，ma10 前后都是 10.5 ⇒ 上穿
    hit, c, t = wt._hit(df, 1, "ma10", None, "cross_up")
    check("cross_up：前一根在均线下方、这一根跑到上方 ⇒ 命中，且报回触发价与线的值",
          hit and abs(c - 11.0) < 1e-9 and abs(t - 10.5) < 1e-9, f"{hit} {c} {t}")

    # i=2：11.0 -> 9.0，穿到 10.5 下方 ⇒ 下穿
    hit, c, t = wt._hit(df, 2, "ma10", None, "cross_down")
    check("cross_down：反方向才算命中", hit and abs(c - 9.0) < 1e-9, f"{hit} {c}")

    # i=1 用 cross_down 必须**不**命中（符号写反就会在这里翻车）
    check("cross_down 不会把「上穿」也算命中（比较符号不能写反）",
          not wt._hit(df, 1, "ma10", None, "cross_down")[0])

    # i=3：low 11.8 <= 11.0? 不 —— 用 ma10=11.0，当日区间 11.8~12.2 碰不到 ⇒ 不命中
    check("touch：当根最低~最高之间**碰不到**线 ⇒ 不命中",
          not wt._hit(df, 3, "ma10", None, "touch")[0])
    # 把线改成 12.0（落在 11.8~12.2 之间）⇒ 命中
    df2 = df.copy()
    df2["ma10"] = [10.5, 10.5, 10.5, 12.0]
    check("touch：碰到就算命中（盘中口径，不看收盘）",
          wt._hit(df2, 3, "ma10", None, "touch")[0])

    # 绝对价位：10.5 —— i=1 收盘 11.0 从 10.0 上穿
    check("绝对价位：上穿 = 前收 <= v < 当收",
          wt._hit(df, 1, "", 10.5, "cross_up")[0])
    check("绝对价位：前收与当收**都在 v 上方**时不算上穿（不是「大于就报」）",
          not wt._hit(df, 3, "", 5.0, "cross_up")[0])
    check("绝对价位：触及 = 当日区间含 v",
          wt._hit(df, 3, "", 12.0, "touch")[0] and not wt._hit(df, 3, "", 15.0, "touch")[0])

    # 均线列缺失 ⇒ 不能崩、也不能误报
    check("线列不存在时静默不命中（不抛异常、不误报）",
          not wt._hit(df, 1, "ma60", None, "cross_up")[0])

    # 规则归一化
    bad = []
    for args in (("", "cross_up", None), ("maX", "cross_up", 1.0), ("ma10", "crossX", None)):
        try:
            wt._norm_alert(args[0], args[1], args[2])
            bad.append(str(args))
        except ValueError:
            pass
    check("规则归一化：没给线还没给价位 → 报错；不认识的线 / 动作 → 报错",
          not bad, "；".join(bad))

    check("line 给了就**丢掉** value（线 / 价二选一，不会两个一起生效）",
          wt._norm_alert("ma10", "cross_up", 9.9) == ("daily", "ma10", "cross_up", None),
          str(wt._norm_alert("ma10", "cross_up", 9.9)))

    # ---- 周K（2026-10-08）：归一化带周期 + 两件最容易静默出错的事 ----
    bad_freq = False
    try:
        wt._norm_alert("ma10", "cross_up", None, "monthly")
    except ValueError:
        bad_freq = True
    check("归一化带上周期：不传 = 日K、传 weekly 原样留下、乱传的周期报错（不静默当日子K）",
          wt._norm_alert("ma10", "cross_up", None, "weekly")[0] == "weekly"
          and wt._norm_alert("ma10", "cross_up", None)[0] == "daily" and bad_freq)

    wstart = wt._lookback_start("2026-10-08", unit="W")
    dstart = wt._lookback_start("2026-10-08", unit="D")
    check("周K 的回溯窗口按**周**算（按天算只有 20 来根周线 → MA60 全 NaN → 规则永远不响）",
          wstart < "2024-01-01" and wstart < dstart, f"周 {wstart} / 日 {dstart}")

    wdf = pd.DataFrame({          # 合成周线：连续三周收 20.10 → 19.82 → 19.66，MA10 三周都是 19.5~19.7
        "open": [20.0, 19.9, 19.8], "high": [20.2, 20.0, 19.9],
        "low": [19.5, 19.4, 19.3], "close": [20.1, 19.82, 19.66],
        "ma10": [19.5, 19.6, 19.7],
    })
    check("周K 判在**周线那一根**上（整周的收/高/低，不是某一天）",
          wt._hit(wdf, 2, "ma10", None, "cross_down")[0]
          and not wt._hit(wdf, 2, "ma10", None, "cross_up")[0],
          str(wt._hit(wdf, 2, "ma10", None, "cross_down")))
    check("周K 的触发文案带「周K」，日K 的不变（记录里两种周期分得开）",
          wt._msg("", "ma10", "cross_up", 19.66, 19.7, {"low": 19.3, "high": 19.9},
                  "weekly").startswith("周K ")
          and wt._msg("", "ma10", "cross_up", 19.66, 19.7, {"low": 19.3, "high": 19.9},
                      "daily").startswith("收盘")
          and "当周" in wt._msg("", "ma10", "touch", 19.66, 19.5, {"low": 19.3, "high": 19.9},
                                "weekly"))

    # ---- 选股命中（kind='pool'）的人话与常量（2026-10-10） ----
    # ⚠ pool 行的 `line` 是空的：`_alert_text` 里要是没这一支，它会掉进「绝对价位」那条分支，
    #   拼出「上穿—」这种东西（`op` 也是空的）—— 页面上一颗看不懂的胶囊，而且不报错。
    check("KINDS 恰好两档（指标/价位 · 选股命中），默认 price",
          set(wt.KINDS) == {"price", "pool"} and wt.DEFAULT_KIND == "price"
          and wt.KIND_ORDER == ["price", "pool"], str(list(wt.KINDS)))
    check("`_alert_text`：pool 档写「选股命中·策略名」（没策略名时说「已删除」，不退成「上穿—」）",
          wt._alert_text("", "", None, "daily", "pool", "首板") == "选股命中·首板"
          and wt._alert_text("", "", None, "daily", "pool", None) == "选股命中·选股策略已删除",
          wt._alert_text("", "", None, "daily", "pool", "首板"))
    check("`_alert_text`：price 档一个字没变（日K 无前缀 / 周K 带「周K·」）",
          wt._alert_text("ma10", "cross_up", None, "daily") == "上穿MA10"
          and wt._alert_text("ma10", "cross_up", None, "weekly") == "周K·上穿MA10"
          and wt._alert_text("", "touch", 10.5, "daily") == "触及10.5")
    check("选股命中的取数回溯与选股页同量级（不够根数的话 MA60/MACD 预热不足 ⇒ 与选股页算不一样）",
          wt.POOL_LOOKBACK_BARS >= 300)
    # 2026-10-10 需求「绑定选股策略也应该要有日k周期和周k周期的选择」。
    check("`_alert_text`：pool 档的**周K**带「周K·」前缀（日K 那条一个字没变）",
          wt._alert_text("", "", None, "weekly", "pool", "首板") == "周K·选股命中·首板"
          and wt._alert_text("", "", None, "weekly", "pool", None) == "周K·选股命中·选股策略已删除",
          wt._alert_text("", "", None, "weekly", "pool", "首板"))
    check("选股命中的人话：周K 加「周K 」前缀、日K 不加（与 price 档周K 同一个写法）",
          wt._pool_msg("首板", 12.34) == "收盘 12.34 命中选股策略「首板」"
          and wt._pool_msg("首板", 12.34, "weekly") == "周K 收盘 12.34 命中选股策略「首板」",
          wt._pool_msg("首板", 12.34, "weekly"))
    # pool 档的周K = 判「最近一个**已收盘的完整周**」那根 ⇒ `end` 取该周最后一个交易日。
    # ⚠ 合成一份 wmap 来验（不依赖库里的日历）：周中跑、当天恰好是周末、跨周三种情形。
    _wm = {"2026-10-02": "2026-09-30", "2026-10-09": "2026-10-09", "2026-10-16": "2026-10-16"}
    check("pool 周K 的判定点：取**最近一个已收盘完整周**的最后交易日（周中跑 = 上一个完整周）",
          wt._pool_week_end("2026-10-14", _wm) == "2026-10-09"
          and wt._pool_week_end("2026-10-09", _wm) == "2026-10-09"
          and wt._pool_week_end("2026-10-02", _wm) == "2026-09-30",
          f"10-14→{wt._pool_week_end('2026-10-14', _wm)} / "
          f"10-09→{wt._pool_week_end('2026-10-09', _wm)}")
    check("pool 周K 的判定点：日历取不到时退回当天（宁可多判一次，也别让规则静默不响）",
          wt._pool_week_end("2026-10-14", {}) == "2026-10-14")
    wmap = wt._week_last_day()
    check("周标签映射到**该周最后一个交易日**（W-FRI 标签可能落在休市日的周五）",
          wmap.get("2026-10-02") == "2026-09-30" and wmap.get("2026-09-18") == "2026-09-18",
          f"2026-10-02 → {wmap.get('2026-10-02')} / 2026-09-18 → {wmap.get('2026-09-18')}")

    # 代码归一化
    got = [wt.norm_code("600000"), wt.norm_code("600000.sh"), wt.norm_code("000001"),
           wt.norm_code("300750.SZ"), wt.norm_code("920229"), wt.norm_code("sh600000")]
    check("代码归一化：只认数字段，6→沪 / 0·3→深 / 4·8·9→北（与热股榜同一套规则）",
          got == ["600000.SH", "600000.SH", "000001.SZ", "300750.SZ", "920229.BJ", "600000.SH"],
          str(got))
    check("代码归一化：认不出来给空串（不会瞎编一个市场）",
          wt.norm_code("abc") == "" and wt.norm_code("") == "")

    # 线名 / 动作的清单是最小集，且都能在指标层查到列
    from core import indicators as ind
    miss = [k for k, v in wt.LINES.items() if ind.col_spec(v[1])["label"] == v[1]]
    check("LINES 里每条线在 indicators.COL_SPEC 里都有定义（不会出现裸字段名当表头）",
          not miss, "；".join(miss))
    check("OPS 恰好三个动作（上穿 / 下穿 / 触及），没有多也没有少",
          set(wt.OPS) == {"cross_up", "cross_down", "touch"}, str(list(wt.OPS)))

    # 东财报表解析（合成 payload，不联网）
    payload = {"success": True, "result": {"count": 1, "data": [
        {"BOARD_NAME": "测试概念", "BOARD_CODE": "BK9999", "ORG_BOARD_CODE": "9999",
         "START_DATE": "2026-01-02 09:30", "START_TIME": "01-02 09:30",
         "BOARD_PROFILE": "简介原文", "SECUCODE_FIRST": "600000.SH",
         "SECURITY_NAME_FIRST": "浦发银行", "CHANGE_RATE_FIRST": 3.5}]}}
    rows = nc._rows(payload)
    check("报表解析：success=true 取 result.data", len(rows) == 1)
    check("空结果两种形状都认：success=false / result 为 null ⇒ 可靠空结果 `[]`",
          nc._rows({"success": False, "code": 9201}) == [] and nc._rows({"success": True, "result": None}) == [])
    check("报表解析：data 是 dict 时也能取（东财偶发把 diff 包成 dict）",
          nc._rows({"success": True, "result": {"data": {"0": {"a": 1}}}}) == [{"a": 1}])
    check("SECUCODE 优先，缺了才按代码首位猜市场",
          nc._ts_code("600000.SH", "000001") == "600000.SH" and nc._ts_code("", "300750") == "300750.SZ")
    check("涨跌幅 `--` / 空 → None（页面显示「—」，不是 0）",
          nc._num("--") is None and nc._num("") is None and nc._num("1.5") == 1.5)
    check("涨跌色只有 up/down/flat 三种，且文本带符号",
          nc._pct_pair(1.2) == ("+1.20%", "up") and nc._pct_pair(-1.2) == ("-1.20%", "down")
          and nc._pct_pair(0) == ("+0.00%", "flat") and nc._pct_pair(None) == ("—", ""))

    # ---- 「日K 在布林下轨」的判定（`boll_lower_hit`）----
    # 需求原话「对日k在布林下轨的股票全部添加日k触及boll下轨提醒」。
    # ⚠ 拿**合成** boll_pos 喂真函数，绝不去赌「今天恰好有票贴在下轨」——
    #   那种断言换一个交易日就假红（硬规矩 15）。要注意的三处边界：
    #   阈值取「≤」（恰好 = 0.1 算命中）、负值（已跌破下轨）也算、NaN **不**当成 0。
    def _posdf(v):
        return pd.DataFrame({"boll_pos": [v]})

    check("布林下轨：位置 0.05（贴着下轨）⇒ 命中", wt.boll_lower_hit(_posdf(0.05)))
    check("布林下轨：位置恰好 = 阈值 0.1 ⇒ 命中（边界取「≤」，不是「<」）",
          wt.boll_lower_hit(_posdf(0.1)))
    check("布林下轨：位置 0.1001（刚超出阈值）⇒ 不命中", not wt.boll_lower_hit(_posdf(0.1001)))
    check("布林下轨：位置为**负**（已经跌破下轨）⇒ 照样命中",
          wt.boll_lower_hit(_posdf(-0.8)))
    check("布林下轨：boll_pos 是 NaN（不足 20 根 / 通道宽度为 0）⇒ 不命中，别当成 0",
          not wt.boll_lower_hit(_posdf(float("nan"))))
    check("布林下轨：连 boll_pos 列都没有 ⇒ 不命中（不是抛异常把整轮扫挂掉）",
          not wt.boll_lower_hit(pd.DataFrame({"close": [10.0]})))
    check("布林下轨：空 df / i 越界 ⇒ 不命中（不抛）",
          not wt.boll_lower_hit(pd.DataFrame())
          and not wt.boll_lower_hit(_posdf(0.0), 5)
          and not wt.boll_lower_hit(_posdf(0.0), -1))
    check("布林下轨：给了 i 就判第 i 根（不是永远判最后一根）",
          not wt.boll_lower_hit(pd.DataFrame({"boll_pos": [0.9, 0.05]}), 0)
          and wt.boll_lower_hit(pd.DataFrame({"boll_pos": [0.9, 0.05]}), 1))
    check("布林下轨：阈值本身只有一处（`BOLL_LOWER_POS`，别处不许再写一个 0.1）",
          abs(wt.BOLL_LOWER_POS - 0.1) < 1e-12)


# ---------------------------------------------------------------------------
# 【3】接口契约
# ---------------------------------------------------------------------------

def part3() -> None:
    print("\n【3】接口契约（临时分组，收尾清干净）")

    meta = req("GET", "/api/watch/meta")
    d = meta.get("data") or {}
    check("/api/watch/meta 给了线与动作的清单 + 口径说明",
          meta.get("ok") and len(d.get("lines") or []) == 7 and len(d.get("ops") or []) == 3
          and "收盘价" in (d.get("note") or ""),
          f"{len(d.get('lines') or [])} 条线 / {len(d.get('ops') or [])} 个动作")

    # 先清掉可能残留的同名临时组
    for g in (req("GET", "/api/watch/groups").get("data") or {}).get("groups") or []:
        if g["name"] == TMP_GROUP:
            req("POST", "/api/watch/group/delete", {"id": g["id"]})

    # 600000.SH 在别的组里有没有？有的话「删组是否连规则一起清」这条判不了（不是本脚本能决定的）
    held_before = set()
    for g in (req("GET", "/api/watch/groups").get("data") or {}).get("groups") or []:
        for s in (req("GET", f"/api/watch/stocks?group_id={g['id']}").get("data") or {}).get("stocks") or []:
            held_before.add(s["ts_code"])

    g = req("POST", "/api/watch/group/save", {"name": TMP_GROUP, "note": "回归用"})
    gid = (g.get("data") or {}).get("id")
    check("建分组：POST /api/watch/group/save 返回新 id",
          g.get("ok") and gid, f"id={gid}")

    dup = req("POST", "/api/watch/group/save", {"name": TMP_GROUP})
    check("重名分组被挡下（400，不是静默建第二条）",
          dup.get("ok") is False, str(dup.get("error"))[:40])

    # 加自选：宽容代码 + 报出认不出的
    a = req("POST", "/api/watch/stocks/add",
            {"group_id": gid, "codes": ["600000", "300750.SZ", "999999.XX", "阿里巴巴"]})
    res = (a.get("data") or {}).get("result") or {}
    check("加自选：不带后缀的 600000 也认，两次加入同一只不重复",
          set(res.get("added") or []) == {"600000.SH", "300750.SZ"} and not res.get("dup"),
          str(res.get("added")))
    check("加自选：认不出的代码如实报回来（不静默丢）",
          len(res.get("unknown") or []) == 2, str(res.get("unknown")))
    again = (req("POST", "/api/watch/stocks/add", {"group_id": gid, "codes": ["600000.SH"]})
             .get("data") or {}).get("result") or {}
    check("重复加同一只 → 落到 dup，不会出现两条", again.get("dup") == ["600000.SH"])

    # 提醒规则
    s1 = req("POST", "/api/watch/alert/save",
             {"ts_code": "600000.SH", "line": "ma10", "op": "cross_up", "note": "回归·均线"})
    aid1 = (s1.get("data") or {}).get("id")
    bad = req("POST", "/api/watch/alert/save", {"ts_code": "600000.SH", "line": "", "op": "touch"})
    check("规则：选了线能存，且 id 是**真自增**（不是接口自己编的）", bool(aid1), f"id={aid1}")
    check("规则：没给线又没给价位 → 400（不是存一条永远不响的规则）",
          bad.get("ok") is False, str(bad.get("error"))[:40])
    s2 = req("POST", "/api/watch/alert/save",
             {"ts_code": "600000.SH", "line": "", "op": "cross_up", "value": 1.0, "note": "回归·价位"})
    aid2 = (s2.get("data") or {}).get("id")
    al = (req("GET", "/api/watch/alerts?ts_code=600000.SH").get("data") or {}).get("alerts") or []
    check("规则列表：两只规则都在，且文本是「动作 + 目标」（前端不用自己拼）",
          len(al) == 2 and all(("上穿" in x["text"]) or ("下穿" in x["text"]) for x in al),
          "；".join(x["text"] for x in al))

    tog = req("POST", "/api/watch/alert/toggle", {"id": aid1, "enabled": 0})
    al2 = (tog.get("data") or {}).get("alerts") or []
    one = [x for x in al2 if x["id"] == aid1]
    # ⚠ 只数**这只票**的，别断言「全表正好 2 条」：toggle 返回的是不带 ts_code 的**全表**，
    #   用户自己给自选股加的规则也在里面 —— 那正是「会变的东西」（硬规矩 15）。
    #   2026-10-08 实测被用户那条 601678.SH 的规则打红过一次。
    check("暂停一条规则：enabled 归 0，规则**还在**（不是删掉）",
          bool(one) and one[0]["enabled"] == 0
          and len([x for x in al2 if x["ts_code"] == "600000.SH"]) == 2,
          "id=%s enabled=%s / 本票 %d 条"
          % (aid1, one[0]["enabled"] if one else None,
             len([x for x in al2 if x["ts_code"] == "600000.SH"])))
    req("POST", "/api/watch/alert/toggle", {"id": aid1, "enabled": 1})

    # ---- 周K：真建一条、真跑一遍检测，并核对日志落在**真实交易日**上 ----
    # 规则取「触及该周区间中点」：周线 low <= 中点 <= high 必然成立，不依赖哪一天恰好有行情
    # （硬规矩 15：别断言「会变的东西」）。中点用**前复权日线**算 —— 周线那根的低/高就是
    # 该周各日低/高的极值，中点必然落在里面，比直接读 weekly_kline 的未复权价稳。
    from core import db as _db
    from core import market as _mk
    from core import watch as wt
    _latest = _mk.latest_trade_date()
    _lw = _mk.last_week_close_day(_latest)          # 最近一个「周最后一个交易日」
    _first_b, _last_b = _mk.week_bounds()
    _p = _mk.load_panel(["600000.SH"], wt._lookback_start(_latest, unit="W"), _latest,
                        freq="daily", adjust="qfq", with_basic=False, weekly=False,
                        only={"low", "high"})
    _df = _p.get("600000.SH")
    _mid = None
    if _df is not None and len(_df) and _lw:
        _lab = [k for k, v in _last_b.to_dict().items() if str(v)[:10] == str(_lw)[:10]]
        if _lab:
            _f0 = pd.Timestamp(str(_first_b.loc[_lab[0]])[:10])
            _seg = _df[(_df["trade_date"] >= _f0) & (_df["trade_date"] <= pd.Timestamp(_lw))]
            if len(_seg):
                _mid = (float(_seg["low"].min()) + float(_seg["high"].max())) / 2
    if _mid is None:
        skip("周K 规则真跑一遍", "算不出最近完整周的区间中点")
    else:
        s3 = req("POST", "/api/watch/alert/save",
                 {"ts_code": "600000.SH", "freq": "weekly", "line": "", "value": _mid,
                  "op": "touch", "note": "回归·周K"})
        aid3 = (s3.get("data") or {}).get("id")
        al3 = [x for x in ((req("GET", "/api/watch/alerts?ts_code=600000.SH").get("data")
                            or {}).get("alerts") or []) if x["id"] == aid3]
        check("周K 规则能存，列表里带出周期标记与「周K·」文案",
              bool(aid3) and bool(al3) and al3[0]["freq"] == "weekly"
              and al3[0]["freq_label"] == "周K" and al3[0]["text"].startswith("周K·"),
              str(al3[0]) if al3 else "没查到")
        badf = req("POST", "/api/watch/alert/save",
                   {"ts_code": "600000.SH", "freq": "monthly", "line": "ma10",
                    "op": "cross_up"})
        check("乱传周期 → 400（不是静默存成日K）",
              badf.get("ok") is False, str(badf.get("error"))[:40])
        ck2 = req("POST", "/api/watch/check", {"lookback": 1, "days": 3})
        c2 = (ck2.get("data") or {}).get("result") or {}
        check("检测结果把日K / 周K 分开报（daily_fired / weekly_alerts / weekly_fired）",
              ck2.get("ok") and all(k in c2 for k in ("daily_fired", "weekly_alerts",
                                                      "weekly_fired"))
              and c2.get("weekly_alerts", 0) >= 1,
              f"周K 规则 {c2.get('weekly_alerts')} 条 / 命中 {c2.get('weekly_fired')} 条")
        row3 = _db.query_one("SELECT * FROM watch_alert_log WHERE alert_id=%s", (aid3,))
        real = _db.scalar("SELECT COUNT(*) FROM daily_kline WHERE trade_date=%s",
                          (row3["trade_date"],)) if row3 else 0
        check("周K 的命中落在**真实交易日**上（不是 W-FRI 标签那种休市日，否则记录会从列表里消失）",
              bool(row3) and str(row3["message"]).startswith("周K ") and real > 0,
              (f"{row3['trade_date']}（该日有 {real} 条日线）/ {row3['message']}"
               if row3 else "没有命中"))
        it3 = [x for x in ((req("GET", "/api/watch/feed?days=7&limit=2000").get("data") or {})
                           .get("items") or []) if x["alert_id"] == aid3]
        check("这条周K 命中在「触发记录」里看得到，且带 freq=weekly（前端靠它按周线开图）",
              bool(it3) and it3[0]["freq"] == "weekly" and it3[0]["freq_label"] == "周K",
              str(it3[0])[:110] if it3 else "feed 里没有")

    # 检测：只判命中/不命中，不断言条数（命中与否取决于那天行情）
    ck = req("POST", "/api/watch/check", {"lookback": 1, "days": 3})
    c = ck.get("data") or {}
    check("检测接口返回结构完整（只数规则、命中数、判到哪天）",
          ck.get("ok") and c["result"]["alerts"] >= 2 and "trade_date" in c["result"],
          f"{c['result']['codes']} 只 / {c['result']['alerts']} 条 / 命中 {c['result']['fired']}")

    feed1 = (req("GET", "/api/watch/feed?days=3").get("data") or {})
    feed2 = (req("GET", "/api/watch/feed?days=3").get("data") or {})
    check("触发记录：同日重复检测不刷屏（幂等，条数稳定）",
          feed1.get("count") == feed2.get("count"),
          f"{feed1.get('count')} 条")
    # ⚠ `limit` 是 2026-10-08 跟着「布林下轨批量加提醒」一起加的：一次给上百只票挂规则之后，
    #   点一下「立即检测」当天就可能一两百条命中，而 `feed()` 默认 `LIMIT 100` 会**静默截断**
    #   （页面看着「就这么几条」，看不出后头的被吃了）。这条盯着「可配 + 乱传要报错」。
    f_lim = req("GET", "/api/watch/feed?days=3&limit=1000")
    f_bad = req("GET", "/api/watch/feed?days=3&limit=abc")
    check("触发记录的 limit 可配（默认 100 会把批量命中的后头静默吃掉）",
          f_lim.get("ok") is True and "items" in (f_lim.get("data") or {})
          and (f_lim.get("data") or {}).get("count", 0) >= (feed1.get("count") or 0)
          and f_bad.get("ok") is False,
          f"limit=1000 → {len((f_lim.get('data') or {}).get('items') or [])} 条 / "
          f"乱传 → {str(f_bad.get('error'))[:24]}")
    check("触发记录：每条都带「动作 + 线名」与人话说明",
          all(("上穿" in x["message"] or "下穿" in x["message"] or "触及" in x["message"])
              for x in (feed1.get("items") or [])) or not feed1.get("items"),
          f"{len(feed1.get('items') or [])} 条")

    # ---- 触发记录带出「股票标记」（2026-10-11 需求原话：「把股票的文本标记那列也拉出来」）----
    # ⚠ 判据是 `watch_stock.note`（**这只票**的文本标记），不是上面那个 `note`（**这条提醒**的备注）。
    #   两个字段都叫得出「备注」，串了页面上一样是行文本 ⇒ 逐条回库核对，别看接口自洽。
    fd_sn = (req("GET", "/api/watch/feed?days=3&limit=2000").get("data") or {})
    sn_items = fd_sn.get("items") or []
    if sn_items:
        want_sn: dict = {}
        for r in _db.query("SELECT ts_code, note FROM watch_stock "
                           "WHERE note IS NOT NULL AND note<>'' ORDER BY group_id"):
            want_sn.setdefault(r["ts_code"], r["note"])
        bad_sn = [x for x in sn_items
                  if (x.get("stock_note") or "") != (want_sn.get(x["ts_code"]) or "")]
        check("每条触发记录都带「股票标记」= 自选股里那只票的 note（逐条回库核对）",
              not bad_sn,
              f"{len(sn_items)} 条 / 对不上 {len(bad_sn)} 条"
              + (f"（如 {bad_sn[0]['ts_code']}）" if bad_sn else ""))
        # ⚠ 这条盯的是「取标记时别 JOIN 进来」：真 JOIN 了，一只票在 N 个组里就会把这行
        #   **裂成 N 条**（列表凭空多出记录，看着还挺像「今天命中变多了」）。独立数一遍 SQL 对拍。
        #   （limit 给到 2000，远大于当前量级，避免又踩「静默截断」那个坑。）
        _ds = fd_sn.get("days") or []
        _ph = ",".join(["%s"] * len(_ds))
        real_n = _db.scalar(f"SELECT COUNT(*) FROM watch_alert_log WHERE trade_date IN ({_ph})",
                            _ds) if _ds else 0
        check("触发记录条数 == 库里条数（取标记没把行数放大 / 没静默截断）",
              (fd_sn.get("count") or 0) == real_n,
              f"接口 {fd_sn.get('count')} vs 库里 {real_n}")
        # 同一只票的多条记录必须拿同一份标记（防「按行号错位回填」那种写法）
        _seen: dict = {}
        bad2 = [x for x in sn_items
                if _seen.setdefault(x["ts_code"], x.get("stock_note")) != x.get("stock_note")]
        check("同一只票的多条记录，标记是同一份（不是按行号错位回填的）",
              not bad2, f"{len(bad2)} 条不一致")
    else:
        check("触发记录带「股票标记」（当前没有触发记录，跳过）", True, "SKIP-无记录")

    # ---- 触发记录的涨跌幅（2026-10-08 需求「列表也增加涨跌幅字段」）----
    # ⚠ 最容易写错的是**取哪一天**：列表里的行跨若干天，用「最新交易日」那一列去填所有行，
    #   等于把今天的涨跌幅贴到 3 天前那条命中上（两个日期的数拼在一起）。这里逐条回 `daily_kline`
    #   核对「该行 trade_date 那天」的原值。
    dflt = (req("GET", "/api/watch/feed").get("data") or {})
    check("接口不传 days 时默认给「最近 1 个交易日」（默认值只有一处口径）",
          len(dflt.get("days") or []) <= 1,
          f"days={dflt.get('days')}")

    # ---- 入参乱传 ⇒ 400，不许 500（2026-10-09 接口测试抓出来的）----
    # 当时各路由直接 `int(request.args.get(...))`：传个 `abc` 就抛 ValueError ⇒ Flask 转成
    # **500 + 一整屏 traceback**。「你传错了」和「我写错了」被混成一类，日志里真 bug 会被
    # 参数错误淹没。现在统一走 `_int()` → `BadParam` → 400。
    # ⚠ 按**行为**验（`req()` 拿不到状态码，但 500 那份响应体里带 `trace` —— 那是崩溃的指纹），
    #   不按实现字符串验 —— 写死实现那条刚在【1】段吃过亏。
    _badp = [
        ("POST", "/api/watch/alert/delete", {"id": "abc"}),
        ("POST", "/api/watch/alert/delete", {"id": [1, 2]}),
        ("POST", "/api/watch/alert/delete", {"id": {"a": 1}}),
        ("POST", "/api/watch/alert/toggle", {"id": "abc"}),
        ("POST", "/api/watch/group/delete", {"id": "abc"}),
        ("POST", "/api/watch/stocks/add", {"group_id": "abc", "codes": ["000001.SZ"]}),
        ("POST", "/api/watch/stocks/remove", {"group_id": "abc", "codes": ["000001.SZ"]}),
        ("POST", "/api/watch/stock/note", {"group_id": "abc", "ts_code": "000001.SZ", "note": "x"}),
        ("POST", "/api/watch/check", {"days": "abc"}),
        ("GET", "/api/watch/feed?days=abc", None),
        ("GET", "/api/watch/stocks?group_id=abc", None),
    ]
    _crash, _not400 = [], []
    for _m, _p, _pl in _badp:
        _r = req(_m, _p, _pl)
        if _r.get("trace"):                       # 500 的指纹：响应体里带整屏 traceback
            _crash.append(f"{_m} {_p} → {str(_r.get('error'))[:40]}")
        elif _r.get("ok") is not False:
            _not400.append(f"{_m} {_p} → ok={_r.get('ok')}")
    check("接口入参乱传（非数字 / 数组 / 对象）一律 **400**，不许泄漏成 500 + traceback",
          not _crash and not _not400,
          "; ".join((_crash + _not400)[:3]) or f"{len(_badp)} 组全数挡下")

    # ⚠ 这条盯着**两个入口同一个口径**：以前 `/api/watch/feed` 乱传 group_id 给 400，
    #   而 `/api/watch/check` 是 `try/except → 静默退回全部` —— 用户以为在看本组，
    #   看到的其实是全市场，而且不报错。两边现在都走 `_parse_group_id()`。
    _ck = req("POST", "/api/watch/check", {"group_id": "abc", "lookback": 1})
    check("`/api/watch/check` 的 group_id 乱传也报 400（不许静默退回「全部」）",
          _ck.get("ok") is False and not _ck.get("trace"),
          str(_ck.get("error"))[:40])
    # ⚠ 默认值**不传参**时必须给全 —— 2026-10-08 只做了「limit 可配」，默认还留着 100，
    #   实测当天命中 108 条 ⇒ 不传参就静默少给 8 条（坑只修了一半）。这里按条数比。
    _def = (req("GET", "/api/watch/feed?days=1").get("data") or {})
    _all = (req("GET", "/api/watch/feed?days=1&limit=2000").get("data") or {})
    check("不传 `limit` 时也不会被默认值截断（默认 100 会静默吃掉后头的命中）",
          (_def.get("count") or 0) == (_all.get("count") or 0),
          f"默认 {_def.get('count')} 条 vs limit=2000 {_all.get('count')} 条")
    pit = feed1.get("items") or []
    check("触发记录每条都带涨跌幅三件套（pct_chg / pct_text / pct_cls）",
          bool(pit) and all(("pct_chg" in x and "pct_text" in x and "pct_cls" in x) for x in pit),
          f"{len(pit)} 条")
    from core import db as _db3
    _mism = []
    for x in pit[:40]:
        _row = _db3.query_one("SELECT pct_chg FROM daily_kline WHERE trade_date=%s AND ts_code=%s",
                              (x["trade_date"], x["ts_code"]))
        _want = None if _row is None or _row["pct_chg"] is None else round(float(_row["pct_chg"]), 4)
        _got = None if x.get("pct_chg") is None else round(float(x["pct_chg"]), 4)
        if _want != _got:
            _mism.append(f"{x['ts_code']}@{x['trade_date']} {_got}≠{_want}")
    check("涨跌幅取的是**该行触发那天**的日涨跌幅（与 daily_kline 同源，不是都填最新那天）",
          bool(pit) and not _mism,
          "; ".join(_mism[:3]) or f"抽验 {min(40, len(pit))} 条全一致")
    check("涨跌幅的文本与涨跌色由**后端**算好（前端不判正负）",
          bool(pit) and all(
              (x["pct_chg"] is None and x["pct_text"] == "—" and x["pct_cls"] == "")
              or (x["pct_chg"] is not None
                  and x["pct_text"] == f"{x['pct_chg']:+.2f}%"
                  and x["pct_cls"] == ("up" if x["pct_chg"] > 0
                                       else ("down" if x["pct_chg"] < 0 else "flat")))
              for x in pit),
          f"{len(pit)} 条")

    # ---- 触发提醒只展示**当前分组**的记录（2026-10-08 需求原话）----
    # 这条用**合成数据**验，不赌「库里恰好有记录」（硬规矩 15）：造两个临时组各放票，
    # 直接往 `watch_alert_log` 插「最新交易日」的记录，再比对各组看到的东西。
    # ⚠ 断的是**函数层返回的 items 范围**（过滤写在 SQL 里），不是「前端筛完剩下的」——
    #   前端筛的话这条会假绿（拿全量筛，行看着也对，只是 limit 窗口会把本组那几条挤掉）。
    from core import db as _db4
    from core import market as _mk4
    from core import watch as _wt4
    _latest = _mk4.latest_trade_date()
    for _gn in (TMP_GROUP_G1, TMP_GROUP_G2):      # 上次跑崩留下的同名组先清掉
        _ex = _db4.query_one("SELECT id FROM watch_group WHERE name=%s", (_gn,))
        if _ex:
            _wt4.delete_group(int(_ex["id"]))
    _A, _B = _wt4.save_group(TMP_GROUP_G1), _wt4.save_group(TMP_GROUP_G2)
    _aids: list = []
    _logids: list = []
    try:
        _wt4.add_stocks(_A, ["600000.SH", "000001.SZ"])
        _wt4.add_stocks(_B, ["600000.SH"])                 # 600000 同时在 A、B 两组
        _aids = [_wt4.save_alert("000001.SZ", "ma10", "cross_up"),
                 _wt4.save_alert("600000.SH", "ma10", "cross_up"),
                 _wt4.save_alert("600519.SH", "ma10", "cross_up")]   # 这只**哪个组都不在**
        for _aid, _code in zip(_aids, ["000001.SZ", "600000.SH", "600519.SH"]):
            _logids.append(_db4.insert(
                "INSERT INTO watch_alert_log (alert_id, ts_code, trade_date, message) "
                "VALUES (%s,%s,%s,%s)",
                (_aid, _code, _latest, f"合成·{_code} 触及 MA10")))
        _fa = _wt4.feed(days=1, limit=2000, group_id=_A)
        _fb = _wt4.feed(days=1, limit=2000, group_id=_B)
        _fall = _wt4.feed(days=1, limit=2000)
        _ac = sorted({x["ts_code"] for x in _fa["items"]})
        _bc = sorted({x["ts_code"] for x in _fb["items"]})
        _allc = {x["ts_code"] for x in _fall["items"]}
        check("按分组取触发记录：只剩本组那几只的（别组的记录一条都不许混进来）",
              _ac == ["000001.SZ", "600000.SH"] and _bc == ["600000.SH"],
              f"A={_ac} / B={_bc}")
        check("不传分组 = 全部（这个语义没被改坏，总览那张卡还靠它）",
              {"000001.SZ", "600000.SH", "600519.SH"} <= _allc,
              f"全局 {len(_allc)} 只")
        check("一只票同时在两个组 ⇒ 两边都看得到它（按「票在不在组里」判，不是唯一归属）",
              "600000.SH" in _ac and "600000.SH" in _bc,
              f"A 有 600000={'600000.SH' in _ac} / B 有 600000={'600000.SH' in _bc}")
        check("哪个组都不在的票，任何分组里都不出现（它只出现在全局里）",
              "600519.SH" not in _ac and "600519.SH" not in _bc and "600519.SH" in _allc)
        _fz = _wt4.feed(days=1, limit=2000, group_id=987654)
        check("分组 id 不存在 ⇒ 空列表（不是退回「全部」）",
              _fz["count"] == 0 and _fz["group_id"] == 987654, f"count={_fz['count']}")
        _f0 = _wt4.feed(days=1, limit=2000, group_id=0)
        check("`group_id=0` 当成「全部」",
              _f0["count"] == _fall["count"], f"{_f0['count']} vs {_fall['count']}")
    finally:
        for _lg in _logids:
            _db4.execute("DELETE FROM watch_alert_log WHERE id=%s", (_lg,))
        for _aid in _aids:
            _wt4.delete_alert(_aid)     # ⚠ 按 id 删；按 ts_code 删会把真实规则一起带走
        _wt4.delete_group(_A)
        _wt4.delete_group(_B)

    # ---- 触发提醒列表里的「删」= 只删这条提醒本身 ----
    # 2026-10-08 需求原话：「触发提醒列表添加删除提醒字段，只删除触发提醒的提醒」。
    # ⚠ 一律用**临时规则**（自己建、自己删），全程不碰分组、不碰自选股 —— 这条路径删的是
    #   真规则，在用户那 149 条布林下轨规则上试一次就削掉人家一条提醒。
    # ⚠ 「列表里的删」与「自选股弹窗里的删」走的是**同一个函数**（`watch.delete_alert`）：
    #   两处各写一套，迟早出现「一处只删记录、一处删整条规则」的分裂。这里同时盯三件事 ——
    #   删干净（规则 + 它**全部**触发记录）/ **不越界**（同票其他规则不动、自选股不动）/
    #   删不存在的要如实回 `deleted=False`（不假装成功）。
    from core import db as _db5
    from core import watch as _wt5
    _n_stock0 = int(_db5.scalar("SELECT COUNT(*) FROM watch_stock") or 0)
    _days5 = [str(r["trade_date"])[:10] for r in _db5.query(
        "SELECT DISTINCT trade_date FROM daily_kline ORDER BY trade_date DESC LIMIT 2")]
    _aid5 = _aid5b = None
    try:
        _aid5 = _wt5.save_alert("600000.SH", "boll_lower", "touch", None,
                                note="ZZ·临时删除测试")
        # 旁证：同一只票的**另一条**规则 —— 删上面那条不许把它带走
        _aid5b = _wt5.save_alert("600000.SH", "ma20", "touch", None,
                                 note="ZZ·临时删除测试·旁证")
        _lg5 = [_db5.insert(
            "INSERT INTO watch_alert_log (alert_id, ts_code, trade_date, message) "
            "VALUES (%s,%s,%s,%s)",
            (_aid5, "600000.SH", _d, f"合成·删提醒 {_d}")) for _d in _days5]
        _r5 = ((req("POST", "/api/watch/alert/delete", {"id": _aid5}).get("data")
                or {}).get("result") or {})
        check("触发提醒列表的「删」= 删这条提醒本身，并如实报出连带删了几条记录",
              _r5.get("deleted") is True and _r5.get("logs") == len(_lg5)
              and _r5.get("ts_code") == "600000.SH" and _r5.get("text") == "触及布林下轨",
              f"{_r5.get('ts_code')} {_r5.get('text')} / logs={_r5.get('logs')}"
              f"（插了 {len(_lg5)} 条）")
        check("删得干净：规则没了，它的触发记录也一起没了（不留孤儿记录）",
              int(_db5.scalar("SELECT COUNT(*) FROM watch_alert WHERE id=%s", (_aid5,)) or 0) == 0
              and int(_db5.scalar("SELECT COUNT(*) FROM watch_alert_log WHERE alert_id=%s",
                                  (_aid5,)) or 0) == 0)
        check("删提醒**不越界**：同票的其他提醒还在、自选股一行没动",
              bool(_db5.query_one("SELECT id FROM watch_alert WHERE id=%s", (_aid5b,)))
              and int(_db5.scalar("SELECT COUNT(*) FROM watch_stock") or 0) == _n_stock0,
              f"旁证规则还在；自选 {_n_stock0} 只不变")
        _r5b = ((req("POST", "/api/watch/alert/delete", {"id": _aid5}).get("data")
                 or {}).get("result") or {})
        _bad5 = req("POST", "/api/watch/alert/delete", {})
        check("删一条已经不存在的提醒 → deleted=False（不假装删成功）；缺 id → 400",
              _r5b.get("deleted") is False and _bad5.get("ok") is False,
              f"重复删 deleted={_r5b.get('deleted')} / 缺 id ok={_bad5.get('ok')}")
    finally:
        for _x5 in (_aid5, _aid5b):
            if _x5:
                _wt5.delete_alert(_x5)
    check("清场：临时规则删净（不给下一次留脏数据）",
          int(_db5.scalar("SELECT COUNT(*) FROM watch_alert WHERE note LIKE %s",
                          ("ZZ·临时删除测试%",)) or 0) == 0)

    sm = (req("GET", "/api/watch/summary").get("data") or {})
    check("summary 报出组数/自选数/启用规则数/最新交易日命中数",
          all(k in sm for k in ("groups", "stocks", "alerts", "fired_today", "trade_date")),
          str(sm))

    # 前端拿它当 K线预览的锚点：预览窗口必须与表里「收盘/涨跌幅」那两列同一天，
    # 否则同一个页面里两处「这只票走得怎么样」会是两天的口径。
    stk = (req("GET", "/api/watch/stocks").get("data") or {})
    check("自选股接口报出「这批行情是哪天」（= summary 的 trade_date）",
          bool(stk.get("trade_date")) and stk.get("trade_date") == sm.get("trade_date"),
          f"接口 {stk.get('trade_date')} / summary {sm.get('trade_date')}")

    # 东财新概念
    ov1 = (req("GET", "/api/newconcept").get("data") or {})
    check("/api/newconcept 返回新概念 + 变更流水 + 抓取时间 + 实时标记",
          ov1.get("new_total", 0) > 0 and "events" in ov1 and "stale" in ov1 and ov1.get("fetched_at"),
          f"{ov1.get('new_total')} 个概念 / {len(ov1.get('events') or [])} 条动态 / live={ov1.get('live')}")
    # ⚠ 2026-10-08 起卡片**不再读本地缓存**：每次 GET 都真去东财抓一遍。
    check("卡片每次调用都实时抓（`live=true`，没有「今日已抓过就用缓存」的短路）",
          ov1.get("live") is True and ov1.get("stale") is False,
          f"live={ov1.get('live')} error={ov1.get('error')}")
    # `?refresh=0` 是留给自检 / 断网场景的只读口子（不打东财，直接读库里的快照）
    ov0 = (req("GET", "/api/newconcept?refresh=0").get("data") or {})
    check("`?refresh=0` 只读本地快照（离线自检口子），字段结构与实时那份一致",
          "new_total" in ov0 and "events" in ov0 and "note" in ov0 and "live" in ov0,
          f"{ov0.get('new_total')} 个 / live={ov0.get('live')}")
    check("新概念的文本与涨跌色由后端算好（前端不判正负）",
          all(("pct_text" in s and "pct_cls" in s) for it in ov1["new"][:3] for s in it["stocks"]),
          "")
    check("代表股涨跌幅是**本库最近交易日**的（东财原值是概念创建当日的老数字）",
          all(s.get("has_live") is not None for it in ov1["new"][:1] for s in it["stocks"]), "")

    # ⚠ 2026-10-08 改口径：**取消本地缓存** —— 不再是「今天已抓过就跳过」，而是每次真抓。
    #   所以这里断言「不传 force 也照抓」；别再断言 skipped=true（那是老口径，必红）。
    sy2 = (req("POST", "/api/newconcept/sync", {"force": True}) or {}).get("data") or {}
    check("force=true 能抓一次（手动刷新的口子）",
          (sy2.get("sync") or {}).get("skipped") is False
          and (sy2.get("coverage") or {}).get("new", 0) > 0,
          str((sy2.get("sync") or {}).get("new")) + " 个")
    sy1 = (req("POST", "/api/newconcept/sync", {}) or {}).get("data") or {}
    check("新概念同步：已取消缓存 ⇒ 不传 force 也照抓（skipped=false）",
          (sy1.get("sync") or {}).get("skipped") is False,
          str(sy1.get("sync"))[:80])
    ev = (req("GET", "/api/newconcept/events?days=5").get("data") or {})
    check("/api/newconcept/events 能按天数取变更流水",
          "items" in ev and "days" in ev, f"{len(ev.get('items') or [])} 条")

    # ---- 新概念清单：满 365 天的整条不摆（2026-10-11）----
    # 独立重算：自己按库里的 start_date 数一遍「未满 365 天」有几条，与接口对拍 ——
    # 别只信接口自己报的数（回头把过滤挪到别处 / 挪错位置，接口照样自洽）。
    import datetime as _dt
    from core import db as _db
    _rows = _db.query("SELECT board_code, start_date FROM em_newconcept")
    _today = _dt.date.today()
    _want = 0
    for _r in _rows:
        _s = str(_r["start_date"])[:10] if _r["start_date"] else ""
        _age = None
        if _s:
            try:
                _age = (_today - _dt.date.fromisoformat(_s)).days
            except ValueError:
                _age = None
        if _age is None or _age <= 365:
            _want += 1
    check("新概念清单只摆未满 365 天的（独立按库里的 start_date 重算一遍对拍）",
          ov1.get("new_total") == _want
          and all(it.get("age_days") is None or it["age_days"] <= 365 for it in ov1["new"]),
          f"接口 {ov1.get('new_total')} / 重算 {_want} / hidden={ov1.get('new_hidden')}")
    check("被剔掉的条数如实报出来（new_hidden + note 里说明），不是静默变少",
          ov1.get("new_hidden") == (len(_rows) - _want)
          and (not ov1.get("new_hidden") or "已满 365 天" in (ov1.get("note") or "")),
          f"hidden={ov1.get('new_hidden')} / note={ov1.get('note')}")
    # 变更流水那条路**不受**这个窗口影响（它是逐日流水，天然近期）
    check("变更流水不受 365 天窗口影响（口径只管「新概念」清单）",
          all("change_date" in e and "ts_code" in e for e in (ov1.get("events") or [])),
          f"{len(ov1.get('events') or [])} 条动态")

    # ---- 拖到别的分组：复制 / 移动（2026-10-08 需求「拖动可选复制到或者移动到」）----
    def _stocks(gid):
        return ((req("GET", f"/api/watch/stocks?group_id={gid}").get("data") or {})
                .get("stocks") or [])

    def _fresh_group(name: str):
        for x in ((req("GET", "/api/watch/groups").get("data") or {}).get("groups") or []):
            if x["name"] == name:
                req("POST", "/api/watch/group/delete", {"id": x["id"]})
        return (req("POST", "/api/watch/group/save", {"name": name}).get("data") or {}).get("id")

    # 用的两只票**从库里现挑「任何组都不持有的」**（硬规矩 15：别断言会变的东西，
    # 更别拿用户自选里可能已有的票当样本）—— 只有「这只票原本只在源组里」，
    # 才验得出「先加到目标组、再删源组」这个顺序：反过来先删，`remove_stocks` 的防孤儿
    # 收尾会当场把它的提醒规则清掉，还不报错。
    # ⚠ 取样必须**只挑规规矩矩的 6 位码**：`stock_basic` 里混着 `T600018.SH` 这种带前缀的
    #   特殊状态票，`norm_code` 会把前缀当非数字剥掉（⇒ 它其实就是 `600018.SH`），
    #   可原样写进 `set_note` 的 WHERE 就**匹配不上、静默不写**。2026-10-08 第一版没滤，
    #   挑到 `T600018.SH`，「备注跟过去」与「规则还在」两条全假红。
    free = [r["ts_code"] for r in _db.query(
        "SELECT ts_code FROM stock_basic WHERE ts_code NOT IN "
        "(SELECT ts_code FROM watch_stock) ORDER BY ts_code DESC LIMIT 80")
        if re.match(r"^\d{6}\.(SH|SZ|BJ)$", r["ts_code"])][:2]
    if len(free) < 2:
        skip("拖动·移动 / 复制（接口契约）", "库里挑不出「不属于任何组」的票当样本")
    else:
        FC1, FC2 = free
        t1 = _fresh_group(TMP_GROUP_T1)
        t2 = _fresh_group(TMP_GROUP_T2)
        t3 = _fresh_group(TMP_GROUP_T3)
        check("拖拽用的三个临时分组建起来了", all([t1, t2, t3]), f"{t1}/{t2}/{t3}")
        req("POST", "/api/watch/stocks/add", {"group_id": t1, "codes": [FC1, FC2]})
        req("POST", "/api/watch/stock/note", {"group_id": t1, "ts_code": FC1, "note": "拖拽·备注"})
        t_aid = ((req("POST", "/api/watch/alert/save",
                      {"ts_code": FC1, "line": "ma10", "op": "cross_up",
                       "note": "拖拽·规则"})).get("data") or {}).get("id")
        s1 = [x["ts_code"] for x in _stocks(t1)]
        check("拖拽前置：源组两只，一只带备注、一只带提醒规则",
              bool(t_aid) and len(s1) == 2 and FC1 in s1, f"源组 {s1} / 规则 {t_aid}")

        mv = req("POST", "/api/watch/stocks/transfer",
                 {"from_group_id": t1, "to_group_id": t2, "codes": [FC1], "mode": "move"})
        rs = (mv.get("data") or {}).get("result") or {}
        sA = [x["ts_code"] for x in _stocks(t1)]
        sB = _stocks(t2)
        check("拖动·移动：源组不再有它、目标组有了（一次调用搞定，不是前端拼两次）",
              mv.get("ok") and FC1 not in sA and any(x["ts_code"] == FC1 for x in sB)
              and rs.get("moved") == 1 and rs.get("same") is False,
              f"源 {sA} / 目标 {[x['ts_code'] for x in sB]} / {rs}")
        check("拖动·移动：备注跟着票一起过去",
              any(x["ts_code"] == FC1 and x["note"] == "拖拽·备注" for x in sB),
              str([(x["ts_code"], x["note"]) for x in sB]))
        check("拖动·移动：这只票的提醒规则**还在**（先加后删；顺序反了会被当孤儿清掉且不报错）",
              len(((req("GET", f"/api/watch/alerts?ts_code={FC1}").get("data") or {})
                   .get("alerts") or [])) == 1)

        cp1 = req("POST", "/api/watch/stocks/transfer",
                  {"from_group_id": t1, "to_group_id": t3, "codes": [FC2], "mode": "copy"})
        cp2 = req("POST", "/api/watch/stocks/transfer",
                  {"from_group_id": t1, "to_group_id": t3, "codes": [FC2], "mode": "copy"})
        r2 = (cp2.get("data") or {}).get("result") or {}
        sA2 = [x["ts_code"] for x in _stocks(t1)]
        sC2 = [x["ts_code"] for x in _stocks(t3)]
        check("拖动·复制：源组留着、目标组也有一份（这才是「复制」，不是移动）",
              cp1.get("ok") and FC2 in sA2 and sC2 == [FC2],
              f"源 {sA2} / 副本 {sC2}")
        check("拖动·复制：同一只再复制一次落到 dup（目标组不会出现两条）",
              r2.get("dup") == [FC2] and not r2.get("added") and sC2 == [FC2], str(r2))

        sm = req("POST", "/api/watch/stocks/transfer",
                 {"from_group_id": t1, "to_group_id": t1, "codes": [FC2], "mode": "move"})
        rsm = (sm.get("data") or {}).get("result") or {}
        check("拖动·同组：直接短路 same=true、一只都不动（不是先删再加白折腾一遍）",
              sm.get("ok") and rsm.get("same") is True and rsm.get("moved") == 0
              and [x["ts_code"] for x in _stocks(t1)] == sA2, str(rsm))

        badm = req("POST", "/api/watch/stocks/transfer",
                   {"from_group_id": t1, "to_group_id": t2, "codes": [FC2], "mode": "sync"})
        check("拖动·乱传 mode → 400（不是静默当成移动或复制）",
              badm.get("ok") is False, str(badm.get("error"))[:40])
        nom = req("POST", "/api/watch/stocks/transfer",
                  {"from_group_id": t1, "codes": [FC2]})
        check("拖动·缺目标组 → 400（不会把票从一个组里「移出去」移丢）",
              nom.get("ok") is False, str(nom.get("error"))[:40])
        noc = req("POST", "/api/watch/stocks/transfer",
                  {"from_group_id": t1, "to_group_id": t2, "codes": []})
        check("拖动·没给代码 → 400", noc.get("ok") is False, str(noc.get("error"))[:40])

        req("POST", "/api/watch/alert/delete", {"id": t_aid})   # 别把探针规则留给后面的断言
        for _g in (t1, t2, t3):
            req("POST", "/api/watch/group/delete", {"id": _g})
        check("拖动用的临时分组清干净了",
              all(x["name"] not in (TMP_GROUP_T1, TMP_GROUP_T2, TMP_GROUP_T3)
                  for x in ((req("GET", "/api/watch/groups").get("data") or {})
                            .get("groups") or [])))

        # ---- 布林下轨批量加提醒（需求「对日k在布林下轨的股票全部添加…提醒」）----
        # ⚠ 真写只走**显式 codes**（一批「任何组都不持有」的自由票）：不传 codes 就是
        #   全部自选股（515 只 / 上百条规则），回归绝不能那样改用户的库状态。
        # ⚠ 断言只比**关系**：命中几只每天在变，一个数字都不写死；期望值拿
        #   `scan_boll_lower` 现算一遍（口径本来就该只有一处，这里比的是
        #   「范围过滤 / 跨组去重 / 已有跳过」这些逻辑有没有漏）。
        # ⚠ 为什么取 12 只而不是 2 只：自由票里大约三成贴在下轨，2 只**经常全空** ——
        #   真写那条路就白验了。12 只几乎必然至少命中一只（不依赖具体哪只）。
        from core import watch as _wt
        t_bl = _fresh_group(TMP_GROUP_BL)
        bl_codes = [r["ts_code"] for r in _db.query(
            "SELECT ts_code FROM stock_basic WHERE ts_code NOT IN "
            "(SELECT ts_code FROM watch_stock) ORDER BY ts_code LIMIT 40")
            if re.match(r"^\d{6}\.(SH|SZ|BJ)$", r["ts_code"])][:12]
        req("POST", "/api/watch/stocks/add", {"group_id": t_bl, "codes": bl_codes})

        def _bl_rows():
            """本批自由票上挂着的 `(日K, boll_lower, touch)` 规则。"""
            all_al = (req("GET", "/api/watch/alerts").get("data") or {}).get("alerts") or []
            return [a for a in all_al if a["ts_code"] in bl_codes
                    and a["line"] == "boll_lower" and a["op"] == "touch"]

        for a in _bl_rows():                              # 万一有残留，先清（幂等那几条才准）
            req("POST", "/api/watch/alert/delete", {"id": a["id"]})
        exp_bl = _wt.scan_boll_lower(bl_codes)

        pre = req("POST", "/api/watch/alerts/boll_lower", {"codes": bl_codes, "dry_run": 1})
        pr = (pre.get("data") or {}).get("result") or {}
        check("布林下轨·预演：命中集合与独立复算一致（「在布林下轨」的口径只有一处）",
              pre.get("ok") and pr.get("scanned") == len(bl_codes)
              and pr.get("codes") == exp_bl["codes"],
              f"接口 {pr.get('matched')} 只 / 独立复算 {exp_bl['matched']} 只")
        check("布林下轨·预演：dry_run 下 added 恒 0，`pending` 才是真要写的条数",
              pr.get("added") == 0 and pr.get("pending") == len(exp_bl["codes"])
              and pr.get("dry_run") is True,
              str({k: pr.get(k) for k in ("added", "pending", "dry_run")}))
        check("布林下轨·预演：一条库都没写（预演必须是只读的，别「顺手先存了」）",
              not _bl_rows())

        go = req("POST", "/api/watch/alerts/boll_lower", {"codes": bl_codes})
        gr = (go.get("data") or {}).get("result") or {}
        rows = _bl_rows()
        check("布林下轨·真写：新增条数 = 预演的 pending，且都是 (日K, boll_lower, touch, 启用)",
              go.get("ok") and gr.get("added") == len(exp_bl["codes"]) == len(rows)
              and all(a["freq"] == "daily" and a["enabled"] == 1 for a in rows),
              f"added={gr.get('added')} / 命中 {len(exp_bl['codes'])} 只 / 库里 {len(rows)} 条")
        if rows:
            check("布林下轨·真写：批量规则带上备注（日后在弹窗里认得出、能成批撤）",
                  bool(gr.get("note")) and all(a["note"] == gr["note"] for a in rows),
                  f"备注「{rows[0]['note']}」")
        else:
            skip("布林下轨·真写：批量规则带上备注", "这批自由票今天一只都不在下轨，没写进东西")

        go2 = req("POST", "/api/watch/alerts/boll_lower", {"codes": bl_codes})
        gr2 = (go2.get("data") or {}).get("result") or {}
        check("布林下轨·幂等：再点一次不再加（added=0、全落 skipped），库里也不会多出第二条",
              go2.get("ok") and gr2.get("added") == 0
              and gr2.get("skipped") == len(exp_bl["codes"]) and len(_bl_rows()) == len(rows),
              str({k: gr2.get(k) for k in ("added", "pending", "skipped")}))

        if rows:
            req("POST", "/api/watch/alert/toggle", {"id": rows[0]["id"], "enabled": 0})
            go3 = req("POST", "/api/watch/alerts/boll_lower", {"codes": bl_codes})
            gr3 = (go3.get("data") or {}).get("result") or {}
            paused = [a for a in _bl_rows() if a["id"] == rows[0]["id"]]
            check("布林下轨·幂等：**暂停着**的那条也算「已有」—— 不绕开用户的暂停另塞一条启用的",
                  gr3.get("added") == 0 and len(_bl_rows()) == len(rows)
                  and paused and paused[0]["enabled"] == 0,
                  f"added={gr3.get('added')} / 库里 {len(_bl_rows())} 条")
        else:
            skip("布林下轨·幂等：暂停着也算「已有」", "这批自由票今天一只都不在下轨")

        all_pre = req("POST", "/api/watch/alerts/boll_lower", {"dry_run": 1})
        ar = (all_pre.get("data") or {}).get("result") or {}
        exp_all = _wt.scan_boll_lower()
        check("布林下轨·不传 codes ⇒ 扫**全部自选股**（跨组去重），且仍然不写库",
              all_pre.get("ok") and ar.get("scanned") == exp_all["scanned"]
              and ar.get("codes") == exp_all["codes"] and ar.get("added") == 0,
              f"扫 {ar.get('scanned')} 只 / 命中 {ar.get('matched')} 只")

        for a in _bl_rows():
            req("POST", "/api/watch/alert/delete", {"id": a["id"]})
        req("POST", "/api/watch/group/delete", {"id": t_bl})
        check("布林下轨批量用的临时分组与规则清干净了",
              not _bl_rows()
              and all(x["name"] != TMP_GROUP_BL for x in
                      ((req("GET", "/api/watch/groups").get("data") or {}).get("groups") or [])))

    # ---- 选股命中提醒（2026-10-10 需求「绑定选股策略规则，选股命中则提醒」）----
    # 这一段能静默出错的地方：①「池子不存在 / 没绑策略」被当成「空条件」⇒ 全部自选股
    #   当天一起响（而且看不出是错的）；②预览接口算的与检测算的不是同一套（预览命中、
    #   检测不响）；③同一策略被多只票挂上 ⇒ 每只各扫一遍（慢，且可能口径不一致）。
    # ⚠ 自己造临时池（必然命中 / 必然不中 / 必然命中），跑完删掉 —— 绝不碰用户那几十条策略。
    from core import watch as _wp
    P_OK, P_NO = "ZZ·回归·必然命中", "ZZ·回归·必然不中"
    P_WK = "ZZ·回归·周K策略"
    for p in (req("GET", "/api/pools").get("data") or []):
        if p["name"] in (P_OK, P_NO):
            req("DELETE", f"/api/pools/{p['id']}")
    pid_ok = (req("POST", "/api/pools",
                  {"name": P_OK, "base": "all", "conds": ["close > 0"], "note": "回归临时"})
              .get("data") or {}).get("id")
    pid_no = (req("POST", "/api/pools",
                  {"name": P_NO, "base": "all", "conds": ["close < 0"], "note": "回归临时"})
              .get("data") or {}).get("id")
    check("选股命中：临时选股策略建得起来（一条必然命中、一条必然不中）",
          bool(pid_ok and pid_no), f"#{pid_ok} / #{pid_no}")

    # 预览接口：单只 / 整组；且与直接调 scan_pool 逐字一致（同一处实现）
    hv = req("POST", "/api/watch/pool/hit", {"pool_id": pid_ok, "codes": ["600000.SH"]})
    vd = hv.get("data") or {}
    direct = _wp.scan_pool(pid_ok, ["600000.SH"])
    check("选股命中·预览：接口与 `scan_pool` 同一份结果（不是接口里另写一套判定）",
          hv.get("ok") and vd.get("codes") == direct["codes"]
          and vd.get("date") == direct["date"] and vd.get("pool_name") == P_OK,
          f"{vd.get('codes')} / {vd.get('date')}")
    check("选股命中·预览：「必然命中」的池子里那只票确实命中（close > 0）",
          vd.get("matched") == 1 and vd.get("rows") and vd["rows"][0]["ts_code"] == "600000.SH",
          str(vd.get("rows"))[:80])
    hg = (req("POST", "/api/watch/pool/hit", {"pool_id": pid_ok, "group_id": gid}).get("data") or {})
    check("选股命中·预览：不传 codes 而传 group_id ⇒ 按**本组**扫（两只都在池里）",
          hg.get("scanned") == 2 and hg.get("matched") == 2, str(hg.get("codes")))
    hn = (req("POST", "/api/watch/pool/hit", {"pool_id": pid_no, "codes": ["600000.SH"]})
          .get("data") or {})
    check("选股命中·预览：「必然不中」的池子 ⇒ 一只都不命中（不是恒真）",
          hn.get("scanned") == 1 and hn.get("matched") == 0 and hn.get("codes") == [],
          str(hn.get("codes")))
    b1 = req("POST", "/api/watch/pool/hit", {"pool_id": 999999, "codes": ["600000.SH"]})
    b2 = req("POST", "/api/watch/pool/hit", {"pool_id": pid_ok})
    b3 = req("POST", "/api/watch/pool/hit", {"pool_id": "abc", "codes": ["600000.SH"]})
    check("选股命中·预览：乱传一律 400（池子不存在 / 没给股票 / pool_id 不是整数）",
          b1.get("ok") is False and "已不存在" in (b1.get("error") or "")
          and b2.get("ok") is False and b3.get("ok") is False,
          f"{b1.get('error')} | {b2.get('error')} | {b3.get('error')}")

    # 存一条选股命中提醒
    sa = req("POST", "/api/watch/alert/save",
             {"ts_code": "600000.SH", "kind": "pool", "pool_id": pid_ok, "note": "回归·选股命中"})
    aid_p = (sa.get("data") or {}).get("id")
    al_p = [a for a in ((sa.get("data") or {}).get("alerts") or []) if a["id"] == aid_p]
    check("选股命中：能存，列表带出 kind / 策略名 / 「选股命中·」文案",
          bool(aid_p) and bool(al_p) and al_p[0]["kind"] == "pool"
          and al_p[0]["pool_id"] == pid_ok and al_p[0]["pool_name"] == P_OK
          and al_p[0]["text"] == "选股命中·" + P_OK,
          str(al_p[0]) if al_p else "没查到")
    check("选股命中：这条规则的四个 price 字段全是空的（口径不混）",
          bool(al_p) and al_p[0]["op"] == "" and al_p[0]["value"] is None
          and al_p[0]["line"] == "" and al_p[0]["freq"] == "daily")
    e1 = req("POST", "/api/watch/alert/save", {"ts_code": "600000.SH", "kind": "pool"})
    e2 = req("POST", "/api/watch/alert/save",
             {"ts_code": "600000.SH", "kind": "pool", "pool_id": 999999})
    e3 = req("POST", "/api/watch/alert/save",
             {"ts_code": "600000.SH", "kind": "xxx", "line": "ma10", "op": "cross_up"})
    check("选股命中：没挑策略 / 策略不存在 / 乱传类型 ⇒ 一律 400（不存一条永远不响的规则）",
          e1.get("ok") is False and "必须挑一条选股策略" in (e1.get("error") or "")
          and e2.get("ok") is False and "不存在" in (e2.get("error") or "")
          and e3.get("ok") is False and "不认识的规则类型" in (e3.get("error") or ""),
          f"{e1.get('error')} | {e2.get('error')} | {e3.get('error')}")

    # 再挂一只**必然不中**的票：检测时它不该响（同一次检测里两档结果必须分得开）
    sa2 = req("POST", "/api/watch/alert/save",
              {"ts_code": "300750.SZ", "kind": "pool", "pool_id": pid_no, "note": "回归·不中"})
    aid_p2 = (sa2.get("data") or {}).get("id")

    ck3 = req("POST", "/api/watch/check", {"lookback": 1, "days": 3})
    c3 = (ck3.get("data") or {}).get("result") or {}
    check("选股命中：检测单独报数（pool_alerts / pool_fired / pool_skipped 都在）",
          ck3.get("ok") and c3.get("pool_alerts", 0) >= 2
          and "pool_fired" in c3 and isinstance(c3.get("pool_skipped"), list),
          f"规则 {c3.get('pool_alerts')} 条 / 命中 {c3.get('pool_fired')} 条 / "
          f"跳过 {c3.get('pool_skipped')}")
    check("选股命中：只报**真在池里**的那只（不中的那条一次都不响）",
          c3.get("pool_fired") == 1,
          f"命中 {c3.get('pool_fired')}（应为 1：600000 命中 / 300750 不在「必然不中」池里）")

    from core import db as _db
    rowp = _db.query_one("SELECT * FROM watch_alert_log WHERE alert_id=%s", (aid_p,))
    rown = _db.query_one("SELECT * FROM watch_alert_log WHERE alert_id=%s", (aid_p2,))
    check("选股命中：写进触发记录的那条日期 = 最新交易日、文案写「命中选股策略「…」」",
          bool(rowp) and str(rowp["trade_date"])[:10] == c3.get("trade_date")
          and "命中选股策略" in (rowp["message"] or ""),
          (f"{rowp['trade_date']} / {rowp['message']}" if rowp else "没有记录"))
    check("选股命中：不中的那条**一条记录都没有**（不是漏判成命中、也不是报错）",
          rown is None)

    fd_p = ((req("GET", f"/api/watch/feed?days=3&limit=2000&group_id={gid}").get("data") or {})
            .get("items") or [])
    pool_rows = [x for x in fd_p if x.get("kind") == "pool"]
    check("选股命中：触发列表里能看到它，且带 kind=pool / 策略名 / 「选股命中·」胶囊文案",
          len(pool_rows) >= 1 and pool_rows[0]["pool_name"] == P_OK
          and pool_rows[0]["text"] == "选股命中·" + P_OK
          and pool_rows[0]["ts_code"] == "600000.SH",
          str(pool_rows[:1])[:120])
    req("POST", "/api/watch/check", {"lookback": 1, "days": 3})     # 再跑一遍
    fd2 = ((req("GET", f"/api/watch/feed?days=3&limit=2000&group_id={gid}").get("data") or {})
           .get("items") or [])
    n2 = len([x for x in fd2 if x.get("kind") == "pool"])
    check("选股命中：同日重复检测不刷屏（唯一键 (alert_id, trade_date) 仍在起作用）",
          len(pool_rows) == n2 and n2 >= 1, f"{len(pool_rows)} → {n2}")

    # ---- 选股命中的**周期**（2026-10-10 需求「绑定选股策略也应该要有日k周期和周k周期的选择」）----
    # 周K = 只判「最近一个已收盘的完整周」那根 ⇒ end 取该周最后一个交易日。
    # ⚠ 预览与检测必须**同一个 end**，否则就是那句「预览说命中、检测不响」。
    latest = c3.get("trade_date")
    wk_end = _wp._pool_week_end(latest)
    pv = req("POST", "/api/watch/pool/hit",
             {"pool_id": pid_ok, "codes": ["600000.SH"], "freq": "weekly"})
    pvd = pv.get("data") or {}
    check("选股命中·预览：`freq=weekly` 判的是「最近一个已收盘完整周」那根（不是拿今天冒充）",
          pv.get("ok") and pvd.get("freq") == "weekly" and pvd.get("date") == wk_end,
          f"date={pvd.get('date')} / 完整周末={wk_end} / freq={pvd.get('freq')}")
    check("选股命中·预览：不传 freq = 日K（老行为不变）；乱传 400（不静默降级成日K）",
          (req("POST", "/api/watch/pool/hit", {"pool_id": pid_ok, "codes": ["600000.SH"]})
           .get("data") or {}).get("freq") == "daily"
          and req("POST", "/api/watch/pool/hit",
                  {"pool_id": pid_ok, "codes": ["600000.SH"], "freq": "xxx"}).get("ok") is False)
    saw = req("POST", "/api/watch/alert/save",
              {"ts_code": "600000.SH", "kind": "pool", "pool_id": pid_ok, "freq": "weekly",
               "note": "回归·选股命中周K"})
    aid_w = (saw.get("data") or {}).get("id")
    al_w = [a for a in ((saw.get("data") or {}).get("alerts") or []) if a["id"] == aid_w]
    check("选股命中：`freq=weekly` 存得下来（不被默默降成日K），胶囊带「周K·」前缀",
          bool(aid_w) and bool(al_w) and al_w[0]["freq"] == "weekly"
          and al_w[0]["freq_label"] == "周K"
          and al_w[0]["text"] == "周K·选股命中·" + P_OK and al_w[0]["line"] == "",
          str(al_w[0]) if al_w else "没存上")
    e4 = req("POST", "/api/watch/alert/save",
             {"ts_code": "600000.SH", "kind": "pool", "pool_id": pid_ok, "freq": "monthly"})
    check("选股命中：周期乱传 ⇒ 400（不存一条自己都说不清的规则）",
          e4.get("ok") is False and "不认识的周期" in (e4.get("error") or ""), e4.get("error"))

    ck4 = req("POST", "/api/watch/check", {"lookback": 1, "days": 3})
    c4 = (ck4.get("data") or {}).get("result") or {}
    check("选股命中：检测把周K 那几条也算了（`pool_weekly_alerts` 报得出来）",
          ck4.get("ok") and c4.get("pool_weekly_alerts", 0) >= 1,
          f"pool_alerts={c4.get('pool_alerts')} / 其中周K={c4.get('pool_weekly_alerts')}")
    roww = _db.query_one("SELECT * FROM watch_alert_log WHERE alert_id=%s", (aid_w,))
    check("选股命中·周K：触发记录落在**该周最后一个交易日**（不是今天），文案带「周K 」",
          bool(roww) and str(roww["trade_date"])[:10] == wk_end
          and (roww["message"] or "").startswith("周K 收盘"),
          (f"{roww['trade_date']} / {roww['message']}" if roww else "没有记录")
          + f"（完整周末 {wk_end}）")
    fd_w = ((req("GET", f"/api/watch/feed?days=20&limit=2000&group_id={gid}").get("data") or {})
            .get("items") or [])
    pool_w = [x for x in fd_w if x.get("kind") == "pool" and x.get("freq") == "weekly"]
    check("选股命中·周K：触发列表里胶囊带「周K·」前缀（与日K 那条分得开）",
          len(pool_w) >= 1 and pool_w[0]["text"] == "周K·选股命中·" + P_OK,
          str(pool_w[:1])[:120])
    req("POST", "/api/watch/alert/delete", {"id": aid_w})      # 别留到删组那一步（会打乱条数）

    # ---- ⚠ **策略自己是周K** 时：整条链按周线跑 --------------------------------
    # 2026-10-10 需求「选股策略中也要有日k和周k的选择项」。面板周期由**策略自己的 timeframe**
    # 决定；提醒选的 freq 只管「多久判一次」。策略是周K 时面板只有走完的整周 ⇒ 实际节奏恒 weekly。
    ckb = req("POST", "/api/watch/check", {"lookback": 1, "days": 3})
    cb = (ckb.get("data") or {}).get("result") or {}       # 基线（此时 aid_w 已删）
    pwk = (req("POST", "/api/pools", {"name": P_WK, "base": "all", "conds": ["close > 0"],
                                      "timeframe": "weekly", "note": "回归临时"})
           .get("data") or {}).get("id")
    ls_pw = [p for p in (req("GET", "/api/pools").get("data") or []) if p["id"] == pwk]
    check("选股策略周期：能存下 timeframe=weekly，列表里带 timeframe / name_label（「（周K）」后缀）",
          bool(pwk) and bool(ls_pw) and ls_pw[0].get("timeframe") == "weekly"
          and ls_pw[0].get("name_label") == P_WK + "（周K）",
          str(ls_pw[0] if ls_pw else "没查到"))
    wk_direct = _wp.scan_pool(pwk, ["600000.SH"])       # 提醒不给 freq ⇒ 日K 节奏
    check("⚠ 周K 策略 + 日K 节奏：面板走**周线**（freq 报 weekly、end 收到完整周末）",
          wk_direct.get("freq") == "weekly" and wk_direct.get("date") == wk_end
          and wk_direct.get("matched") == 1,
          f"freq={wk_direct.get('freq')} / date={wk_direct.get('date')} / wk_end={wk_end}")
    hw2 = req("POST", "/api/watch/pool/hit",
              {"pool_id": pwk, "codes": ["600000.SH"]}).get("data") or {}
    check("⚠ 预览接口同一份判定（接口不另写一套）",
          hw2.get("freq") == "weekly" and hw2.get("date") == wk_end,
          f"freq={hw2.get('freq')} / date={hw2.get('date')}")
    saw2 = req("POST", "/api/watch/alert/save",
               {"ts_code": "600000.SH", "kind": "pool", "pool_id": pwk, "freq": "daily",
                "note": "回归·策略周K"})
    aid_w2 = (saw2.get("data") or {}).get("id")
    ck5 = req("POST", "/api/watch/check", {"lookback": 1, "days": 3})
    c5 = (ck5.get("data") or {}).get("result") or {}
    check("⚠ 策略周K + 提醒日K：仍算作**周K 那条**（+1，按实际节奏统计，不看提醒下拉选了啥）",
          ck5.get("ok") and (c5.get("pool_weekly_alerts") or 0) == (cb.get("pool_weekly_alerts") or 0) + 1,
          f"{cb.get('pool_weekly_alerts')} → {c5.get('pool_weekly_alerts')}")
    rowv = _db.query_one("SELECT * FROM watch_alert_log WHERE alert_id=%s", (aid_w2,))
    check("⚠ 策略周K ⇒ 文案带「周K 」前缀、触发日 = 该周最后交易日（哪怕提醒选的是日K）",
          bool(rowv) and str(rowv["trade_date"])[:10] == wk_end
          and (rowv["message"] or "").startswith("周K 收盘"),
          (f"{rowv['trade_date']} / {rowv['message']}" if rowv else "没有记录"))
    req("POST", "/api/watch/alert/delete", {"id": aid_w2})
    req("DELETE", f"/api/pools/{pwk}")

    # 删提醒 ≠ 删策略（两件事）
    dl = (req("POST", "/api/watch/alert/delete", {"id": aid_p}).get("data") or {}).get("result") or {}
    still = [p for p in (req("GET", "/api/pools").get("data") or []) if p["id"] == pid_ok]
    check("选股命中：删这条提醒**不动**它引用的选股策略（策略还在选股页里）",
          dl.get("deleted") is True and dl.get("kind") == "pool" and dl.get("pool_name") == P_OK
          and len(still) == 1,
          f"deleted={dl.get('deleted')} / 策略还在 {len(still)} 条")
    check("选股命中：删掉的那条连它的触发记录一起清（不留孤儿）",
          _db.query_one("SELECT id FROM watch_alert_log WHERE alert_id=%s", (aid_p,)) is None)
    req("POST", "/api/watch/alert/delete", {"id": aid_p2})
    for x in (pid_ok, pid_no):
        req("DELETE", f"/api/pools/{x}")
    check("选股命中：临时策略清干净了（用户自己的策略一条没动）",
          all(p["name"] not in (P_OK, P_NO) for p in (req("GET", "/api/pools").get("data") or [])))

    # 清理：删组 —— 组内自选股与孤儿规则一起清
    before = (req("GET", "/api/watch/summary").get("data") or {}).get("alerts")
    dd = (req("POST", "/api/watch/group/delete", {"id": gid}) or {}).get("data") or {}
    after_al = (req("GET", "/api/watch/alerts?ts_code=600000.SH").get("data") or {}).get("alerts") or []
    check("删分组：组里那几只自选股一起清掉",
          dd.get("result", {}).get("stocks") == 2, str(dd.get("result")))
    if "600000.SH" in held_before:
        skip("删分组：不再被任何组持有的票，规则一并清（不留孤儿）",
             "600000.SH 本来就在别的组里，这条判不了")
    else:
        check("删分组：不再被任何组持有的票，规则一并清（不留孤儿）",
              len(after_al) == 0 and dd.get("result", {}).get("alerts_dropped") >= 2,
              f"清掉 {dd.get('result', {}).get('alerts_dropped')} 条规则；该票剩余 {len(after_al)} 条")
    gl = (req("GET", "/api/watch/groups").get("data") or {}).get("groups") or []
    check("临时分组没有残留", all(x["name"] != TMP_GROUP for x in gl),
          "；".join(x["name"] for x in gl) or "（无分组）")
    print(f"    清理前启用规则 {before} 条")


# ---------------------------------------------------------------------------
# 【4】浏览器实测
# ---------------------------------------------------------------------------

def chrome_path() -> str | None:
    for c in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe")):
        if os.path.exists(c):
            return c
    return None


def part4() -> None:
    print("\n【4】浏览器实测（同源 iframe，只读 DOM）")
    chrome = chrome_path()
    if not chrome:
        skip("浏览器实测", "找不到 Chrome")
        return
    out_file = os.path.join(tempfile.gettempdir(), "_probe_watch_dom.html")
    prof = os.path.join(tempfile.gettempdir(), "_probe_watch_profile")
    with open(out_file, "w", encoding="utf-8") as fh:
        subprocess.run([chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
                        "--user-data-dir=" + prof, "--window-size=1500,1600",
                        "--virtual-time-budget=120000", "--dump-dom", BASE + PROBE],
                       stdout=fh, stderr=subprocess.DEVNULL, timeout=300)
    dom = open(out_file, encoding="utf-8", errors="replace").read()
    m = re.search(r'<pre id="out">([\s\S]*?)</pre>', dom)
    if not m:
        check("探针页产出结果", False, "没抓到 <pre id=out>")
        return
    try:
        data = json.loads(m.group(1))
    except Exception as e:                                   # noqa: BLE001
        check("探针页产出可解析的 JSON", False, f"{e}：{m.group(1)[:120]}")
        return
    for c in data.get("checks") or []:
        if str(c.get("extra", "")).startswith("SKIP-"):
            skip(c["name"], c["extra"])
        else:
            check(c["name"], c.get("ok"), c.get("extra", ""))


def part5() -> None:
    """K线预览入口的 URL 口径 —— **真点一次按钮**，读弹窗里那个同源 iframe 的 src。

    为什么要单列一段：这四个入口（关注页自选股 / 关注页触发列表 / 总览触发提醒 /
    总览新概念代表股）加上 2026-10-11 新增的「总览·新概念·最新动态表里的票名」，
    都**没有绑定买卖策略**，必须带 `sel_only=1`。漏了不会报错、
    页面也照常打开，只是里面多出一张拿默认买卖参数跑出来的「收益」卡 ——
    静态断言只能证明「代码里写了 sel_only」，证明不了「点下去真的带上了」。
    """
    print("\n【5】K线预览入口的 URL 口径（真点一次按钮）")
    chrome = chrome_path()
    if not chrome:
        skip("K线入口实测", "找不到 Chrome")
        return
    gid = None
    try:
        g = (req("POST", "/api/watch/group/save", {"name": TMP_GROUP_K}).get("data") or {})
        gid = g.get("id")
        if not gid:
            check("临时分组建起来了", False, str(g))
            return
        req("POST", "/api/watch/stocks/add",
            {"group_id": gid, "codes": ["600000", "300750.SZ"]})
        st = (req("GET", f"/api/watch/stocks?group_id={gid}").get("data") or {}).get("stocks") or []
        row = [x for x in st if x["ts_code"] == "600000.SH" and x.get("close") is not None]
        if not row:
            skip("K线入口实测", "库里没有 600000.SH 的最近交易日行情")
            return
        # 「触及最新收盘价」必然命中（low <= close <= high）—— 这样触发列表一定有行可点，
        # 不然「总览触发提醒」那条断言会因为今天恰好没命中而假绿（跳过）。
        req("POST", "/api/watch/alert/save",
            {"ts_code": "600000.SH", "line": "", "value": row[0]["close"], "op": "touch"})
        fired = ((req("POST", "/api/watch/check", {"lookback": 1, "days": 7})
                  .get("data") or {}).get("result") or {}).get("fired")
        check("临时规则确实触发了（不然后面那条断言会因为没行可点而假绿）",
              (fired or 0) >= 1, f"命中 {fired} 条")

        out_file = os.path.join(tempfile.gettempdir(), "_probe_kline_dom.html")
        prof = os.path.join(tempfile.gettempdir(), "_probe_kline_profile")
        with open(out_file, "w", encoding="utf-8") as fh:
            subprocess.run([chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
                            "--user-data-dir=" + prof, "--window-size=1500,1000",
                            "--virtual-time-budget=120000", "--dump-dom", BASE + PROBE_KLINE],
                           stdout=fh, stderr=subprocess.DEVNULL, timeout=300)
        dom = open(out_file, encoding="utf-8", errors="replace").read()
        m = re.search(r'<pre id="out">([\s\S]*?)</pre>', dom)
        if not m:
            check("K线探针页产出结果", False, "没抓到 <pre id=out>")
            return
        try:
            data = json.loads(m.group(1))
        except Exception as e:                               # noqa: BLE001
            check("K线探针页产出可解析的 JSON", False, f"{e}：{m.group(1)[:120]}")
            return
        for c in data.get("checks") or []:
            if str(c.get("extra", "")).startswith("SKIP-"):
                skip(c["name"], c["extra"])
            else:
                check(c["name"], c.get("ok"), c.get("extra", ""))
    finally:
        if gid:
            req("POST", "/api/watch/group/delete", {"id": gid})
            left = [x["name"] for x in
                    ((req("GET", "/api/watch/groups").get("data") or {}).get("groups") or [])]
            check("K线探针的临时分组清干净了", TMP_GROUP_K not in left, "剩：" + "、".join(left))


def part6() -> None:
    """提醒弹窗的默认口径 —— **真开一次弹窗**，看默认落在哪条线上。

    为什么单列一段：用户 2026-10-08 反馈「我之前说的是**上穿 MA10** 那种提醒，不是触发价格
    的提醒」。下拉默认停在「绝对价位」时，静态断言照样全绿 —— 代码里确实有 MA10 这个 option，
    只有真打开弹窗才知道第一眼看到的是个价格输入框。顺带把「一键上穿 MA10」这条路真跑一遍。
    """
    print("\n【6】提醒弹窗的默认口径与周K（真开弹窗 + 真点「上穿 MA10」/「周K 上穿 MA10」）")
    chrome = chrome_path()
    if not chrome:
        skip("提醒弹窗实测", "找不到 Chrome")
        return
    out_file = os.path.join(tempfile.gettempdir(), "_probe_alert_dom.html")
    prof = os.path.join(tempfile.gettempdir(), "_probe_alert_profile")
    with open(out_file, "w", encoding="utf-8") as fh:
        subprocess.run([chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
                        "--user-data-dir=" + prof, "--window-size=1500,1200",
                        "--virtual-time-budget=120000", "--dump-dom", BASE + PROBE_ALERT],
                       stdout=fh, stderr=subprocess.DEVNULL, timeout=300)
    dom = open(out_file, encoding="utf-8", errors="replace").read()
    m = re.search(r'<pre id="out">([\s\S]*?)</pre>', dom)
    if not m:
        check("提醒弹窗探针页产出结果", False, "没抓到 <pre id=out>")
        return
    try:
        data = json.loads(m.group(1))
    except Exception as e:                                   # noqa: BLE001
        check("提醒弹窗探针页产出可解析的 JSON", False, f"{e}：{m.group(1)[:120]}")
        return
    for c in data.get("checks") or []:
        if str(c.get("extra", "")).startswith("SKIP-"):
            skip(c["name"], c["extra"])
        else:
            check(c["name"], c.get("ok"), c.get("extra", ""))
    # 探针自己会删临时组，这里再核一遍没留在库里
    left = [x["name"] for x in
            ((req("GET", "/api/watch/groups").get("data") or {}).get("groups") or [])]
    check("提醒弹窗探针的临时分组清干净了", TMP_GROUP_A not in left, "剩：" + "、".join(left))


def part7() -> None:
    """拖拽 + 表头排序实测 —— **合成拖放事件 / 真点表头**各跑一遍。

    为什么不并进【4】：那一页只读 DOM；这一段要往页面上**派事件**、再回查库里的变化。
    为什么非做不可：「落在左半 = 移动到 / 右半 = 复制到」两条路在代码里只差一个字符串，
    静态断言能证明「模板里写了 `data-drop="copy"`」，证明不了「鼠标落在右半时真走 copy」。
    而且错了的现象很安静 —— 用户拖到右边以为复制了，其实整只票从原组消失了。
    顺带把「勾选后拖一行 = 拖整批」「拖到当前组自己上什么也不做」也验了。

    排序那几段同理：`paintStocks()` 一换整张表的节点，**绑定漏挂**（排一次序按钮就失灵）
    和**缺值不沉底**这两种错看页面都看不出来，只能真点一下、再把屏幕顺序跟接口数字对上。
    """
    print("\n【7】拖拽 + 表头排序实测（合成 dragstart/dragover/drop · 真点表头）")
    chrome = chrome_path()
    if not chrome:
        skip("拖拽实测", "找不到 Chrome")
        return
    out_file = os.path.join(tempfile.gettempdir(), "_probe_drag_dom.html")
    prof = os.path.join(tempfile.gettempdir(), "_probe_drag_profile")
    with open(out_file, "w", encoding="utf-8") as fh:
        subprocess.run([chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
                        "--user-data-dir=" + prof, "--window-size=1500,1200",
                        "--virtual-time-budget=180000", "--dump-dom", BASE + PROBE_DRAG],
                       stdout=fh, stderr=subprocess.DEVNULL, timeout=420)
    dom = open(out_file, encoding="utf-8", errors="replace").read()
    m = re.search(r'<pre id="out">([\s\S]*?)</pre>', dom)
    if not m:
        check("拖拽探针页产出结果", False, "没抓到 <pre id=out>")
        return
    try:
        data = json.loads(m.group(1))
    except Exception as e:                                   # noqa: BLE001
        check("拖拽探针页产出可解析的 JSON", False, f"{e}：{m.group(1)[:120]}")
        return
    for c in data.get("checks") or []:
        if str(c.get("extra", "")).startswith("SKIP-"):
            skip(c["name"], c["extra"])
        else:
            check(c["name"], c.get("ok"), c.get("extra", ""))
    # 探针自己会删临时组，这里再核一遍没留在库里
    left = [x["name"] for x in
            ((req("GET", "/api/watch/groups").get("data") or {}).get("groups") or [])]
    check("拖拽探针的临时分组清干净了",
          all(n not in left for n in ("ZZ·拖拽探针源", "ZZ·拖拽探针目标", "ZZ·拖拽探针副本")),
          "剩 " + str(len(left)) + " 组")


def part8() -> None:
    """【8】触发提醒「删」的**端到端**链路：真点一次「确定」。

    回归里那条「点删」只敢点**取消**（真删一次就少一条真规则，跑几轮用户那 149 条布林下轨
    规则就没了），所以「点了确定 ⇒ 规则真没了、列表真刷新」这一档一直没人验 —— 而它恰恰
    最容易坏：删完忘了刷新、列表还留着那一行、或者接口换口径只删了记录。

    数据全在这里现造：**不在任何组里的自由票 + 临时组 + 临时规则 + 一条合成触发记录**，
    探针只负责 UI 操作，跑完无论成败都把临时组删掉。
    """
    chrome = chrome_path()
    if not chrome:
        skip("端到端真删实测", "找不到 Chrome")
        return
    from core import db as _db8
    from core import market as _mk8
    from core import watch as _wt8

    latest = _mk8.latest_trade_date()
    free = _db8.query(
        "SELECT d.ts_code FROM daily_kline d WHERE d.trade_date=%s "
        "AND d.ts_code NOT IN (SELECT ts_code FROM watch_stock) "
        "AND d.ts_code NOT IN (SELECT ts_code FROM watch_alert) "
        "ORDER BY d.ts_code LIMIT 1", (latest,))
    if not free:
        skip("端到端真删实测", "找不到「任何组都不持有」的自由票")
        return
    code = free[0]["ts_code"]
    gid = _wt8.save_group(TMP_GROUP_FL, "端到端删链路用的临时组", None)
    try:
        _wt8.add_stocks(gid, [code])
        aid = _wt8.save_alert(code, "ma10", "cross_up", None, "ZZ·端到端删链路", 1,
                              None, freq="daily")
        _db8.execute("INSERT INTO watch_alert_log (alert_id,ts_code,trade_date,message) "
                     "VALUES (%s,%s,%s,%s)", (aid, code, latest, "回归合成的触发记录"))
        url = f"{BASE}{PROBE_FLOW}&gid={gid}&code={code}"
        out_file = os.path.join(tempfile.gettempdir(), "_probe_flow_dom.html")
        prof = os.path.join(tempfile.gettempdir(), "_probe_flow_profile")
        with open(out_file, "w", encoding="utf-8") as fh:
            subprocess.run([chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
                            "--user-data-dir=" + prof, "--window-size=1500,1200",
                            "--virtual-time-budget=120000", "--dump-dom", url],
                           stdout=fh, stderr=subprocess.DEVNULL, timeout=300)
        dom = open(out_file, encoding="utf-8", errors="replace").read()
        m = re.search(r'<pre id="out">([\s\S]*?)</pre>', dom)
        if not m:
            check("端到端真删·探针页产出结果", False, "没抓到 <pre id=out>")
            return
        try:
            data = json.loads(m.group(1))
        except Exception as e:                               # noqa: BLE001
            check("端到端真删·探针页产出可解析的 JSON", False, f"{e}：{m.group(1)[:120]}")
            return
        for c in data.get("checks") or []:
            if str(c.get("extra", "")).startswith("SKIP-"):
                skip(c["name"], c["extra"])
            else:
                check(c["name"], c.get("ok"), c.get("extra", ""))
        # 页面看着没了不算数，库里也得真没（「只把 DOM 那行抹了」是这类 bug 的经典形态）
        n_alert = _db8.scalar("SELECT COUNT(*) FROM watch_alert WHERE id=%s", (aid,))
        n_log = _db8.scalar("SELECT COUNT(*) FROM watch_alert_log WHERE alert_id=%s", (aid,))
        check("端到端真删·库里规则与它连带的触发记录都归零（不只是把那行 DOM 抹掉）",
              n_alert == 0 and n_log == 0, f"规则 {n_alert} 条 / 记录 {n_log} 条")
    finally:
        _wt8.delete_group(gid)
    left = [x["name"] for x in
            ((req("GET", "/api/watch/groups").get("data") or {}).get("groups") or [])]
    check("端到端真删的临时分组清干净了", TMP_GROUP_FL not in left,
          "剩 " + str(len(left)) + " 组")


def main() -> int:
    no_e2e = "--no-e2e" in sys.argv
    part1()
    part2()
    try:
        part3()
    except Exception as e:                                   # noqa: BLE001
        check("【3】接口契约整体跑通", False, f"{type(e).__name__}: {e}")
    if no_e2e:
        skip("【4】浏览器实测", "--no-e2e")
        skip("【5】K线入口实测", "--no-e2e")
        skip("【6】提醒弹窗实测", "--no-e2e")
        skip("【7】拖拽 + 表头排序实测", "--no-e2e")
        skip("【8】端到端真删实测", "--no-e2e")
    else:
        try:
            part4()
        except Exception as e:                               # noqa: BLE001
            check("【4】浏览器实测整体跑通", False, f"{type(e).__name__}: {e}")
        try:
            part5()
        except Exception as e:                               # noqa: BLE001
            check("【5】K线入口实测整体跑通", False, f"{type(e).__name__}: {e}")
        try:
            part6()
        except Exception as e:                               # noqa: BLE001
            check("【6】提醒弹窗实测整体跑通", False, f"{type(e).__name__}: {e}")
        try:
            part7()
        except Exception as e:                               # noqa: BLE001
            check("【7】拖拽 + 表头排序实测整体跑通", False, f"{type(e).__name__}: {e}")
        try:
            part8()
        except Exception as e:                               # noqa: BLE001
            check("【8】端到端真删实测整体跑通", False, f"{type(e).__name__}: {e}")

    print(f"\n{'=' * 60}\n通过 {PASS} / 失败 {FAIL} / 跳过 {SKIP}")
    if FAILS:
        print("失败项：")
        for x in FAILS:
            print("  -", x)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
