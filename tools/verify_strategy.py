#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""「买卖策略」页回归：MACD与KDJ共振策略 + 卡片拖拽排序。

为什么需要它：这两块都是「页面看着对、实际不生效」的重灾区 ——
  · **转折判据**写错一个比较号，结果照样有、只是含义全变了：
    「昨降今升」误写成「今天比昨天大」（漏掉「昨天还在降」），
    在一段持续上涨里**每根都算转折**，信号从个位数膨胀到几百个，
    光看页面（有信号、有买卖点）根本发现不了。所以【2】要用合成序列
    把「昨降今升 / 昨升今降 / 单边上涨 / 横盘后转」四种形态逐个钉死。
  · **共振**必须真的是「两个指标同一天都转折」：`and` 写成了两个独立条件、
    或者两个条件分别落在 `buy_trigger` 的两条里（那是「都满足」没错，
    但**漏了同一天**这层约束就会退化成「各自转折过就算」）—— 回归要数出
    「买点数 == 两指标转折日的交集」，而不是「至少有一个转折」。
  · **排序落库**最容易假绿：只验「拖完 DOM 顺序变了」的话，
    纯前端排序（不写库）也能全绿；必须**重拉后端清单**再看顺序还在不在。
    还有三个静默坑：①**内置样例也能拖**（2026-10-10 起）—— 若 SQL 还用
    `builtin DESC` 打头，内置会被钉死在前两条，拖内置 = 白拖，界面上却毫无提示；
    ②`sort` 从 0 起算时和「没拖过的 0」撞车 —— 实测踩过，没拖过的策略横插到第 1 位
    （所以有 `SORT_BASE`）；③拖内置时后端**只许写 `sort`**，绝不能顺手把
    `builtin` 标记也改了（改了内置就变成可删的普通策略了）。
  · **接口乱传必须 400 不许 500**（C57）：`ids` 传字符串 / 元素传 `abc`，
    以前那种直写 `int()` 的写法会抛 500 + 一整屏 traceback。

四段：
  【1】静态口径 —— 判据只有一个实现（`expr.turn_up/turn_down`）、策略串不手写 ref 拼、
                   内置策略清单、模板里的拖拽接线 / 内置也可拖（但仍不可删）/ 重画必重挂、
                   接口走 `_int` + `BadParam`、CSS 有插入线
  【2】纯函数语义 —— **喂合成序列**给真的 `turn_up/turn_down`（不碰库）：
                   昨降今升 / 昨升今降 / 单边上涨不出信号 / 昨平今升算 / 两三根的边界不炸；
                   再用合成面板验「共振 = 交集」
  【3】接口契约 —— 策略清单含新内置且 `builtin=1`；共振信号在真数据上出得来；
                   reorder 正常 / 空列表 / 脏 id / **内置照样参与排序** / 重复 id / 字符串数字；
                   乱传一律 400；落库后重查顺序一致
  【4】浏览器实测 —— 同源 iframe 真拖：中间态（半透明 / 左右插入线 / 同一时刻只有一条）、
                   **内置样例能被拖走且不再被 builtin DESC 钉回**、落位插入点、
                   **落库后重拉清单顺序还在**（落库的分水岭）、
                   拖到空白处不掉卡、内置可拖但仍不可删、删按钮的 guardClick 还在

⚠ 探针（static/_probe_stratdrag.html）踩过的两个 headless 死穴，改它之前先读：
   1. **给非卡片元素直接派发 `drop`** ⇒ 浏览器执行 drop 的默认行为（拿 dataTransfer
      去导航），headless 直接挂到超时 ⇒ 第 5 段改成只发 dragstart + dragend。
   2. **让原生 `confirm()` 真弹**（点「删」按钮）⇒ 阻塞 JS 线程等一个永远不来的
      响应，同样挂到超时 ⇒ 第 6 段临时把 iframe 的 `confirm` 换成返回 false 的桩。
   3. 给同一个 iframe **第二次**赋 src 时 onload 可能不触发 ⇒ 第 4 段不重载页面，
      改成直接 fetch 后端清单比对（判据一样硬，还少一层导航）。

跑法：
    python tools/verify_strategy.py
    python tools/verify_strategy.py --no-e2e     # 只跑【1】【2】【3】，不起浏览器
依赖：服务在 127.0.0.1:8777；本机 Chrome（找不到就把【4】标 SKIP，不算失败）。
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
PROBE_DRAG = "/static/_probe_stratdrag.html?run=1"
STRATEGY_HTML = os.path.join(ROOT, "templates", "strategy.html")
EXPR_PY = os.path.join(ROOT, "core", "expr.py")
STRATEGY_PY = os.path.join(ROOT, "core", "strategy.py")
DB_PY = os.path.join(ROOT, "core", "db.py")
APP_PY = os.path.join(ROOT, "app.py")
APP_CSS = os.path.join(ROOT, "static", "css", "app.css")
PROBE_HTML = os.path.join(ROOT, "static", "_probe_stratdrag.html")

PASS = FAIL = SKIP = 0
FAILS: list[str] = []

NEW_NAME = "MACD与KDJ共振"


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


def req(method: str, path: str, data: dict | None = None, timeout: int = 120):
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(BASE + path, data=body, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(r, timeout=timeout).read().decode()), 200
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode()), e.code
        except Exception:                                     # noqa: BLE001
            return {}, e.code


def read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# 【1】静态口径
# ---------------------------------------------------------------------------

def part1() -> None:
    print("\n【1】静态口径")
    ex = read(EXPR_PY)
    sp = read(STRATEGY_PY)
    html = read(STRATEGY_HTML)
    app = read(APP_PY)
    db = read(DB_PY)
    css = read(APP_CSS)

    # --- 转折判据只有一个实现 ---
    check("expr.py 有 turn_up 实现（昨降今升）",
          re.search(r"def f_turn_up\(x\)", ex) is not None)
    check("expr.py 有 turn_down 实现（昨升今降）",
          re.search(r"def f_turn_down\(x\)", ex) is not None)
    check("turn_up 的判据同时要求「今天>昨天」与「昨天<=前天」",
          re.search(r"r = \(a > prev\) & \(prev <= prev2\)", ex) is not None,
          "只看「今天>昨天」= 上涨，不是转折")
    check("turn_down 的判据同时要求「今天<昨天」与「昨天>=前天」",
          re.search(r"r = \(a < prev\) & \(prev >= prev2\)", ex) is not None)
    check("转折判据把前两根显式判 False（np.roll 会绕回首尾，别串成环）",
          ex.count("r[:2] = False") >= 2, f"出现 {ex.count('r[:2] = False')} 次")
    check("turn_up / turn_down 都注册进了 FUNCTIONS",
          '"turn_up": f_turn_up' in ex and '"turn_down": f_turn_down' in ex)
    check("FUNC_HELP 里有这两条（编辑界面能看到）",
          '("turn_up(x)"' in ex and '("turn_down(x)"' in ex)

    # --- 策略串不许手写 ref 拼判据（口径只留一处）---
    m = re.search(r'\{\s*"name": "MACD与KDJ共振".*?^\s*\},', sp, re.S | re.M)
    check("core/strategy.py 里有「MACD与KDJ共振」这条内置", m is not None)
    body = m.group(0) if m else ""
    check("共振策略用的是 turn_up()/turn_down()，没手写 ref 拼判据",
          "turn_up(" in body and "turn_down(" in body and "ref(macd_dif" not in body,
          "手写 `x > ref(x,1) and ref(x,1) <= ref(x,2)` 会把口径复制成两份")
    check("买入条件里 MACD 与 KDJ 是**两条**（同一根 K 线上都要满足 = 共振）",
          len(re.findall(r'"turn_up\(', body)) == 2,
          f"{len(re.findall(chr(34) + 'turn_up' + chr(92) + chr(40), body))} 条")
    check("卖出条件用 turn_down（与买入对称）",
          len(re.findall(r'"turn_down\(', body)) == 2)
    check("买入触发用 turn_up(macd_dif) 与 turn_up(kdj_k)",
          "turn_up(macd_dif)" in body and "turn_up(kdj_k)" in body)
    check("策略带风控（最长持有 / 止损 / 止盈）",
          '"max_hold_days"' in body and '"stop_loss_pct"' in body
          and '"take_profit_pct"' in body)
    check("策略里**没有**选股条件 entry（选股逻辑归选股策略）",
          '"entry"' not in body,
          "2026-09-18 起选股只归「选股策略」")

    # --- 排序：DB 列 + 迁移 + 起点偏移 ---
    check("db.py 的 strategy 建表里有 sort 列",
          re.search(r"sort\s+INT\s+NOT NULL DEFAULT 0", db) is not None)
    check("db.py 有 sort 列的迁移（老库补列，不然直接 Unknown column）",
          re.search(r'\("strategy", "sort",', db) is not None)
    check("reorder 的 sort 起点是 SORT_BASE（不能从 0 起，否则撞没拖过的行）",
          "SORT_BASE = 100000" in sp,
          "实测踩过：从 0 起算时没拖过的行（sort=0）横插到第 1 位")
    check("reorder_strategies 用 SORT_BASE 起算",
          re.search(r"pos = SORT_BASE", sp) is not None)
    check("list_strategies 按 sort, builtin DESC, id 排（sort 打头，内置才拖得动）",
          re.search(r"ORDER BY sort ASC, builtin DESC, id ASC", sp) is not None,
          "⚠ builtin DESC 打头会把内置钉死在前两条 ⇒ 拖内置 = 白拖（2026-10-10 踩过）")
    check("reorder **不再跳过内置**（内置样例也能排序）",
          "sid in skip" not in sp and 'if int(r["builtin"])' not in sp,
          "还在按 builtin 过滤 ⇒ 内置又被钉成不可拖、拖了白拖")
    check("reorder 只写 sort、绝不碰 builtin 标记（碰了内置就变成可删的普通策略）",
          re.search(r"UPDATE strategy SET sort=%s WHERE id=%s", sp) is not None
          and "SET builtin" not in sp and "builtin=%s" not in sp)
    check("reorder 去重重复 id（不去重 sort 会跳号）",
          "if v in seen:" in sp)
    check("reorder 忽略不存在的 id（批量操作别为一个脏 id 整批失败）",
          "if sid not in alive:" in sp)

    # --- 接口：乱传 400、走 _int ---
    m2 = re.search(r'@app\.route\("/api/strategies/reorder", methods=\["POST"\]\)([\s\S]*?)\n\n\n',
                   app)
    check("app.py 有 /api/strategies/reorder 路由", m2 is not None)
    route = m2.group(0) if m2 else ""
    check("reorder 路由先验 ids 是 list（非列表直接 400，不许 500）",
          "isinstance(ids, list)" in route and "BadParam" in route)
    check("reorder 路由的元素解析走 _int()（全站唯一整数入参口径）",
          "_int(x, name=" in route,
          "别在这里另写一套 int 转换")

    # --- 模板接线 ---
    check("每张卡都带 data-drag（含内置 —— 内置样例也能拖）",
          'data-id="${s.id}" data-drag="1"' in html)
    check("attachDrag() 对内置也 draggable=true（不再有 builtin 短路）",
          "if (!s) { el.draggable = false; return; }" in html
          and "el.draggable = true;" in html)
    check("内置仍然没有「删」按钮（可拖 ≠ 可删）",
          "${s.builtin?'':'<button class=\"btn sm danger\" data-del=" in html)
    check("dragstart 里从按钮/链接按下不算拖卡片",
          "closest('button,a,input,select')" in html,
          "否则点「回测」会变成拖拽")
    check("dragover 用 clientX 判左右半（不用 offsetX）",
          "const after = (e.clientX - r.left) > r.width / 2;" in html and "e.offsetX" not in html,
          "合成事件里 offsetX 恒为 0（C17/C52）")
    check("dragover 里 preventDefault（不挡默认行为根本不允许 drop）",
          re.search(r"dragover[\s\S]{0,400}?e\.preventDefault\(\)", html) is not None)
    check("drop 里**先读**落点再收尾（clearDropMark 会清掉 drop-r）",
          re.search(r"const after = el\.classList\.contains\('drop-r'\);[\s\S]{0,120}clearDropMark\(\)",
                    html) is not None)
    # ⚠ 别断言「attachDrag() 在函数最后一行」这种排版细节（注释一夹就假红）——
    #   要判的是「它在 renderList 的函数体内被调用」，这才是行为。
    body_rl = re.search(r"function renderList\(\)\{([\s\S]*?)\n\}", html)
    check("⚠ 重画后拖拽绑定重挂（attachDrag 在 renderList 体内被调用）",
          body_rl is not None and "attachDrag()" in body_rl.group(1),
          "⚠ 不重挂 = 拖一次之后再没反应（C52 同类坑）")
    check("commitOrder 写库后按后端返回重画（不信任本地 DOM）",
          "strategies = r.strategies;" in html and "renderList();" in html)
    check("commitOrder 用 try/catch 接住 apiPost 的异常（失败要提示 + 回真实顺序）",
          "catch (e)" in html and "顺序没能保存" in html,
          "apiPost 失败是抛异常不是返回 null —— 不接住就是静默不生效")
    check("commitOrder 提交**全量** id（含内置 —— 只提交自定义的话拖内置不落库）",
          "const ids = listedIds();" in html and "listedIds().filter" not in html)
    check("拖拽是本地瞬时动作，没套 guardClick（C38）",
          "guardClick" not in re.search(r"function attachDrag\(\)[\s\S]*?\n\}", html).group(0))
    check("保留了「删」按钮的 guardClick（拖拽改造没碰掉它）",
          re.search(r"\[data-del\]'\)\.forEach\(b => b\.onclick = guardClick", html) is not None)
    check("模板暴露了只读探针钩子 __stratDebug",
          "window.__stratDebug" in html and "list: () =>" in html)
    check("CSS 有拖拽插入线（drop-l / drop-r）",
          ".strat-item.drop-l" in css and ".strat-item.drop-r" in css)
    check("CSS 给 .strat-item 加了 position:relative（否则插入线飘出卡片）",
          re.search(r"#strat-list \.strat-item\{position:relative\}", css) is not None)


# ---------------------------------------------------------------------------
# 【2】纯函数语义（喂合成序列给真函数）
# ---------------------------------------------------------------------------

def part2() -> None:
    print("\n【2】纯函数语义（合成序列，不碰库）")
    from core import expr

    check("昨天降、今天升 → 算向上转折",
          list(expr.f_turn_up(np.array([10.0, 9.0, 8.0, 9.0]))) == [False, False, False, True])
    check("昨天升、今天降 → 算向下转折",
          list(expr.f_turn_down(np.array([8.0, 9.0, 10.0, 9.0]))) == [False, False, False, True])
    check("⚠ 单边上涨**不**算转折（每根都算 = 信号烂成一片）",
          not expr.f_turn_up(np.array([1.0, 2, 3, 4, 5, 6])).any(),
          "这正是「只看今天>昨天」会踩的坑")
    check("⚠ 单边下跌**不**算转折",
          not expr.f_turn_down(np.array([6.0, 5, 4, 3, 2, 1])).any())
    check("横盘后转升 → 算转折（昨天<=前天 用 <= 而不是 <）",
          bool(expr.f_turn_up(np.array([5.0, 5, 5, 6]))[-1]))
    check("横盘后转降 → 算转折",
          bool(expr.f_turn_down(np.array([5.0, 5, 5, 4]))[-1]))
    check("转折点计数：V 形序列只在底那根算一次",
          int(expr.f_turn_up(np.array([10.0, 8, 6, 7, 9])).sum()) == 1)
    check("倒数第二根回落、最后一根回升 → 最后一根是转折",
          bool(expr.f_turn_up(np.array([9.0, 8.0, 7.0, 6.5]))[-1]) is False
          and bool(expr.f_turn_up(np.array([9.0, 8.0, 6.5, 7.0]))[-1]) is True)
    for n in (0, 1, 2):
        a = np.arange(n, dtype=float)
        try:
            r1 = expr.f_turn_up(a)
            r2 = expr.f_turn_down(a)
            check(f"长度 {n} 不炸且不产生信号",
                  len(r1) == n and len(r2) == n and not r1.any() and not r2.any())
        except Exception as e:                                # noqa: BLE001
            check(f"长度 {n} 不炸", False, str(e))

    # 共振 = 交集（不是「各自转折过」）
    # ⚠ `indicators.compute` 在 core/indicators.py:404 无条件取 open/high/low/close，
    #   且 409 行算 avg_price 要用 amount/vol ⇒ 合成面板这几列一个都不能少。
    n = 40
    rng = np.random.default_rng(7)
    base = 10 + np.cumsum(rng.normal(0, 0.4, n))
    df = pd.DataFrame({
        "open": base,
        "close": base + rng.normal(0, 0.1, n),
        "high": base + 0.3,
        "low": base - 0.3,
        "vol": np.full(n, 1e6),
        "amount": np.full(n, 1e7),
        "pre_close": np.roll(base, 1),
    })
    from core import indicators as ind
    d = ind.compute(df, None, ts_code="TEST", name="合成", only={"macd_dif", "macd_dea", "kdj_k", "kdj_d"})
    t_macd = expr.f_turn_up(d["macd_dif"].values) & expr.f_turn_up(d["macd_dea"].values)
    t_kdj = expr.f_turn_up(d["kdj_k"].values) & expr.f_turn_up(d["kdj_d"].values)
    inter = t_macd & t_kdj
    union = t_macd | t_kdj
    check("合成的共振买入 = 两指标转折日的**交集**（比并集少）",
          int(inter.sum()) < int(union.sum()) or int(union.sum()) == 0,
          f"交集 {int(inter.sum())} / 并集 {int(union.sum())}")
    check("交集必然是并集的子集",
          bool((inter & ~union).sum() == 0))
    # 用表达式引擎跑一遍，应等于手工交集
    ctx = {c: d[c].values for c in d.columns if not c.startswith("_") and d[c].dtype.kind in "fi"}
    got = expr.evaluate_bool("turn_up(macd_dif) and turn_up(macd_dea) and "
                            "turn_up(kdj_k) and turn_up(kdj_d)", ctx, n)
    check("表达式引擎跑出来的共振日 == 手工交集",
          bool(np.array_equal(np.asarray(got, dtype=bool), inter)),
          f"引擎 {int(np.asarray(got).sum())} / 手工 {int(inter.sum())}")


# ---------------------------------------------------------------------------
# 【3】接口契约
# ---------------------------------------------------------------------------

def part3() -> None:
    print("\n【3】接口契约")
    d, code = req("GET", "/api/strategies")
    check("GET /api/strategies 正常", code == 200 and d.get("ok"), f"http {code}")
    rows = d.get("data") or []
    check("清单里每条都带 sort 字段",
          all("sort" in r for r in rows), f"{len(rows)} 条")
    hit = [r for r in rows if r["name"] == NEW_NAME]
    check(f"清单里有「{NEW_NAME}」", len(hit) == 1)
    if hit:
        check("它是内置（builtin=1 ⇒ 不可删、清单一收窄就不会被当内置重插）",
              int(hit[0]["builtin"]) == 1, f"builtin={hit[0]['builtin']}")
        cfg = hit[0]["config"] or {}
        check("买入触发是 2 条（MACD 一条 + KDJ 一条 = 同一天都满足）",
              len(cfg.get("buy_trigger") or []) == 2,
              str(cfg.get("buy_trigger")))
        check("卖出信号是 2 条", len(cfg.get("sell_signal") or []) == 2)
        check("风控带最长持有",
              (cfg.get("risk") or {}).get("max_hold_days") is not None,
              str(cfg.get("risk")))

    # 内置：两条样例都在清单里。
    # ⚠ ⚠ 这里**故意不再断言「内置排在最前」**：2026-10-10 起内置样例也能拖，
    #   位置由用户拖出来的 sort 决定 —— 再钉「内置必须在前」等于把真功能判成 bug。
    #   「内置排在最前」只在**默认态**（从没拖过）成立，那是 list_strategies 里
    #   `builtin DESC` 兜底那一段的职责，不是这里该断言的（真实库随时可能被拖过）。
    from core import strategy as st
    bnames = [b["name"] for b in st.BUILTIN_STRATEGIES]
    check(f"内置样例都在清单里（{len(bnames)} 条，builtin=1）",
          all(any(r["name"] == n and int(r["builtin"]) == 1 for r in rows) for n in bnames),
          "、".join(bnames))
    check("清单里没有重复 id（拖拽重排别把行写重）",
          len({r["id"] for r in rows}) == len(rows), f"{len(rows)} 条")

    # --- 共振信号在真数据上出得来（不空验）---
    try:
        from core import market as mk
        from core import strategy as st
        cfg = st.selection_cfg(st.merge_config(hit[0]["config"])) if hit else None
        if cfg:
            panels = mk.load_panel(["300750.SZ"], "2023-01-01", "2026-10-09",
                                   weekly=False, only=set(st.required_daily_fields(cfg)))
            p = panels.get("300750.SZ")
            if p is None or len(p) < 60:
                skip("共振信号在真数据上出得来", "本地没取到 300750.SZ 的行情")
            else:
                res = st.compute_signals(p, cfg)
                nb = int(res["buy_trigger"].sum())
                ns = int(res["sell_signal"].sum())
                check("真数据上买点出得来（不是恒 0 = 条件写死了）", nb > 0, f"{nb} 个买点")
                check("真数据上卖点出得来", ns > 0, f"{ns} 个卖点")
                # 逐点复核：每个买点都必须「两指标同日向上转折」
                okall = True
                bad = []
                for i in np.nonzero(res["buy_trigger"])[0]:
                    for c in ("macd_dif", "macd_dea", "kdj_k", "kdj_d"):
                        a = p[c].values
                        if i < 2 or not (a[i] > a[i-1] and a[i-1] <= a[i-2]):
                            okall = False
                            bad.append(f"{c}@{p['trade_date'].iloc[i]}")
                            break
                check("每个买点都逐指标复核通过（昨降今升，四个指标无一例外）",
                      okall, "; ".join(bad[:3]) or f"{nb} 个买点全对")
                okall = True
                bad = []
                for i in np.nonzero(res["sell_signal"])[0]:
                    for c in ("macd_dif", "macd_dea", "kdj_k", "kdj_d"):
                        a = p[c].values
                        if i < 2 or not (a[i] < a[i-1] and a[i-1] >= a[i-2]):
                            okall = False
                            bad.append(f"{c}@{p['trade_date'].iloc[i]}")
                            break
                check("每个卖点都逐指标复核通过（昨升今降）",
                      okall, "; ".join(bad[:3]) or f"{ns} 个卖点全对")
    except Exception as e:                                    # noqa: BLE001
        skip("真数据信号复核", f"取数失败：{e}")

    # --- reorder 契约（⚠ 2026-10-10 起**内置样例也参与排序**，不再被跳过）---
    ALL = [r["id"] for r in rows]                        # 当前全量顺序（含内置）
    builtin = [r["id"] for r in rows if int(r["builtin"])]
    if len(ALL) < 3 or not builtin:
        skip("reorder 排序契约", f"清单 {len(ALL)} 条 / 内置 {len(builtin)} 条，拖不出花样")
        return
    ORIG = list(ALL)
    try:
        # ① 正常：把最后两条拖到最前（提交**全量**）
        want = ALL[-2:][::-1] + ALL[:-2]
        d1, c1 = req("POST", "/api/strategies/reorder", {"ids": want})
        check("reorder 正常返回 200", c1 == 200 and d1.get("ok"), f"http {c1}")
        got = [x["id"] for x in (d1.get("data") or {}).get("strategies", [])]
        check("reorder 后**全量**顺序 == 提交的顺序（内置不再被跳过）",
              got == want, f"{got[:4]} vs {want[:4]}")
        # 重查接口（不是看返回值，是真去库里读）
        d2, _ = req("GET", "/api/strategies")
        got2 = [x["id"] for x in d2["data"]]
        check("⚠ 重查策略清单，顺序仍是拖后的（**真的落库了**）",
              got2 == want,
              "只验 DOM 或只验返回值的话，纯前端排序也能假绿")

        # ② 内置真能拖：把内置全塞到**最后**，再重查接口看它到底动没动
        want_b = [x for x in ALL if x not in builtin] + builtin
        req("POST", "/api/strategies/reorder", {"ids": want_b})
        d3, _ = req("GET", "/api/strategies")
        got3 = [x["id"] for x in d3["data"]]
        check("⚠⚠ 内置被排到**最后**且真生效（builtin DESC 没把它钉回最前）",
              got3 == want_b and got3[-len(builtin):] == builtin,
              f"尾 {len(builtin)} 条 = {got3[-len(builtin):]}")
        bi3 = {int(x["id"]): int(x["builtin"]) for x in d3["data"]}
        check("⚠ 拖内置只改位置，builtin 标记一个没坏（仍然不可删）",
              all(bi3.get(i) == 1 for i in builtin), str(bi3))
        check("内置的 sort 也真写进去了（>= SORT_BASE ⇒ 参与过重排）",
              all(int(x["sort"]) >= 100000 for x in d3["data"] if int(x["builtin"])),
              str([x["sort"] for x in d3["data"] if int(x["builtin"])]))

        # ③ 脏 id 静默忽略
        d4, c4 = req("POST", "/api/strategies/reorder", {"ids": [999999, ORIG[0]]})
        check("脏 id 被忽略、不报错（批量操作别为一个脏 id 整批失败）",
              c4 == 200 and d4.get("ok") and d4["data"]["changed"] == [ORIG[0]],
              str(d4.get("data", {}).get("changed")))
        # ④ 内置塞进去**不再被跳过**（与旧行为正相反，正是本次需求的判据）
        d5, _ = req("POST", "/api/strategies/reorder", {"ids": builtin[:1] + ORIG[:2]})
        ch5 = d5["data"]["changed"]
        check("把内置 id 塞进 ids **照样生效**（changed 里含内置 —— 旧行为是跳过）",
              builtin[0] in ch5, str(ch5))
        # ⑤ 重复 id 去重
        d6, _ = req("POST", "/api/strategies/reorder", {"ids": [ORIG[0], ORIG[0], ORIG[1]]})
        check("重复 id 去重（不去重 sort 会跳号）",
              d6["data"]["changed"] == [ORIG[0], ORIG[1]], str(d6["data"]["changed"]))
        # ⑥ 字符串数字也收
        d7, c7 = req("POST", "/api/strategies/reorder",
                     {"ids": [str(ORIG[0]), str(ORIG[1])]})
        check("元素传字符串数字也收（_int 的统一口径）",
              c7 == 200 and d7["data"]["changed"] == [ORIG[0], ORIG[1]])
        # ⑦ 空列表：合法、什么都不动
        d8, c8 = req("POST", "/api/strategies/reorder", {"ids": []})
        check("空列表合法（不动任何东西）",
              c8 == 200 and d8["data"]["changed"] == [])
    finally:
        req("POST", "/api/strategies/reorder", {"ids": ORIG})   # ⚠ 必须还原

    d9, _ = req("GET", "/api/strategies")
    got9 = [x["id"] for x in d9["data"]]
    check("收尾：**全量**顺序已还原（含内置）", got9 == ORIG,
          f"{got9[:4]} vs {ORIG[:4]}")

    # --- 乱传一律 400，不许 500（C57）---
    for bad, label in [
        ({"ids": "abc"}, "ids 传字符串"),
        ({"ids": None}, "ids 传 None"),
        ({}, "整个缺 ids"),
        ({"ids": [1, "abc"]}, "元素混进非数字"),
        ({"ids": [True]}, "元素是布尔（True 是 int 的子类，要单独挡）"),
        ({"ids": {"a": 1}}, "ids 传对象"),
    ]:
        ddx, cc = req("POST", "/api/strategies/reorder", bad)
        check(f"乱传 400 不许 500：{label}", cc == 400, f"http {cc}")
        check(f"乱传 {label} 的响应体里没有 traceback（不是 500 的指纹）",
              "trace" not in json.dumps(ddx).lower(),
              dd := "" if "trace" not in json.dumps(ddx).lower() else "带 trace")


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
    print("\n【4】浏览器实测（同源 iframe，真拖）")
    if not os.path.exists(PROBE_HTML):
        skip("拖拽实测", "探针页 static/_probe_stratdrag.html 不存在")
        return
    chrome = chrome_path()
    if not chrome:
        skip("拖拽实测", "找不到 Chrome")
        return
    out_file = os.path.join(tempfile.gettempdir(), "_probe_stratdrag_dom.html")
    # ⚠ 这里**故意不传 --user-data-dir**：本机实测「headless + 自定义 profile」会
    #   让 Chrome 永久挂起（--dump-dom 一个字节都不输出，直到 subprocess 超时），
    #   去掉 profile 参数后同一条命令 13s 就跑完。别照抄别的脚本顺手加上它。
    import shutil
    if os.path.exists(out_file):
        os.remove(out_file)
    with open(out_file, "w", encoding="utf-8") as fh:
        subprocess.run([chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
                        "--no-first-run", "--window-size=1500,1200",
                        "--virtual-time-budget=90000", "--dump-dom", BASE + PROBE_DRAG],
                       stdout=fh, stderr=subprocess.DEVNULL, timeout=300)
    dom = read(out_file)
    m = re.search(r'<pre id="out">([\s\S]*?)</pre>', dom)
    if not m:
        check("探针页产出结果", False, "没抓到 <pre id=out>")
        return
    text = m.group(1)
    if "PENDING" in text or "DONE" not in text:
        check("探针跑完了（不是半路超时）", False, text.strip().splitlines()[-1][:120])
    else:
        check("探针跑完了（不是半路超时）", True)
    for line in text.splitlines():
        if line.startswith("OK   "):
            check(line[5:].split("  ")[0], True, "  ".join(line[5:].split("  ")[1:]))
        elif line.startswith("FAIL "):
            check(line[5:].split("  ")[0], False, "  ".join(line[5:].split("  ")[1:]))
        elif line.startswith("SKIP "):
            skip(line[5:].split("  ")[0], "")
        elif line.startswith("ERROR "):
            check("探针无未捕获异常", False, line[6:][:200])


def main() -> None:
    no_e2e = "--no-e2e" in sys.argv
    print("=" * 62)
    print("买卖策略回归（MACD与KDJ共振 + 卡片拖拽排序）")
    print("=" * 62)
    part1()
    part2()
    part3()
    if no_e2e:
        skip("浏览器实测", "--no-e2e")
    else:
        part4()
    print("\n" + "=" * 62)
    print(f"结果：{PASS} 通过 / {FAIL} 失败 / {SKIP} 跳过")
    if FAILS:
        print("失败项：")
        for f in FAILS:
            print("  - " + f)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
