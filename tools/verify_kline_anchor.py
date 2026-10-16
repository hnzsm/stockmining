#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""K线「查询日」回归：窗口不截断在查询日 + 查询日画竖虚线（2026-09-21 加）。

需求原话：「假设选股是按照9月15日去查询的，那个K线不要从9月15日截断，而是起码要从
11月15日截断，9月15日那一天用个竖虚线来代表查询日」——用户选的口径是**往右显示查询日
之后**（查询日落在窗口里，竖虚线是「已知 / 之后」的分界），默认显示 60 根
（2026-09-21 由 250 调小：一屏 250 根挤成一片看不出 K 线本身）。

为什么需要它：这条口径横跨三层，任何一层单独"看着对"都不算数：

  · 后端：K 线要**多加载到最新交易日**，但选股命中/买卖信号/基本面/收益**只能算到
    查询日**（多出来的那几天若也算了信号，就是未来函数 —— 选股、回测里明令禁止）；
  · 前端：默认窗口（dataZoom）要落在查询日附近、查询日之后有多少显示多少；
  · 图：查询日那根上要有竖虚线，且它不能混进图例/tooltip（挂 K 线 series 上就会被
    图例开关带走 —— 下挂格参考线踩过同一个坑）。

所以分三段：

  【1】静态口径 —— 后端有 `_cut_after_anchor` 且被调用、窗口是「之前 bars 根 + 之后
                   全部」（不是 `tail(bars)`）、前端默认根数是具名常量、竖虚线走空数据
                   series、文案来自后端（模板里不许出现「查询日 …」这种拼出来的字面量）。
  【2】接口契约 —— 真调 `/api/stock/<code>/detail`：锚点位置、查询日之后信号全空、
                   窗口根数、基本面/收益的截止日、不带 trade_date 时的退化行为。
  【3】浏览器实测 —— 同源 iframe 挂真诊断页（预览模式），读真 dataZoom 与原始 option。

依赖：服务在 127.0.0.1:8777；本机 Chrome（找不到就把【3】标 SKIP，不算失败）。
用法：python tools/verify_kline_anchor.py [--no-e2e]
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
STOCK_HTML = os.path.join(ROOT, "templates", "stock.html")
SCREENER_PY = os.path.join(ROOT, "core", "screener.py")
PROBE = "/static/_probe_kline_anchor.html"
NODE = r"C:\Users\ming\.workbuddy\binaries\node\versions\22.22.2-3\node.exe"

CODE = "000001.SZ"
TD = "2026-09-15"          # 查询日（用户举的例子）
BARS = 260                 # 诊断页表单默认「K线根数」

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


CHROME = None
for _c in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
           r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
           os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe")):
    if os.path.exists(_c):
        CHROME = _c
        break


def chrome_probe_out(url: str, budget_ms: int, timeout: int, profile: str) -> str:
    """渲染探针页并返回 <pre id="out"> 里的文本。"""
    out = os.path.join(tempfile.gettempdir(), "_kla_probe_dom.html")
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


# ---------------------------------------------------------------------------
def sec1_static() -> None:
    print("【1】静态口径（窗口不截断 / 竖虚线走空数据 series / 文案来自后端）")
    py = open(SCREENER_PY, encoding="utf-8").read()
    html = open(STOCK_HTML, encoding="utf-8").read()
    # 去掉注释再查，免得把说明文字当成代码
    code = re.sub(r"/\*[\s\S]*?\*/", "", html)
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)

    check("后端有 _cut_after_anchor（查询日之后的信号一律清掉）",
          "def _cut_after_anchor(" in py)
    check("screen_detail 真的调了它", "df = _cut_after_anchor(df, apos)" in py)
    check("窗口 = 「查询日之前 bars 根 + 之后全部」（不再是 tail(bars)）",
          "keep_from = max(0, apos - bars + 1)" in py
          and "rows = df[cols].iloc[keep_from:].copy()" in py)
    check("K线多加载到最新交易日（load_end）",
          "load_end = max(anchor, market.latest_trade_date()" in py)
    check("基本面/收益以**查询日**为准，不是 K 线最后一根",
          "last_row = df.iloc[apos].to_dict()" in py and "end=anchor_date or None" in py)
    check("接口回传 anchor（date/index/requested/extended）",
          '"anchor": {' in py and '"extended": bool(bars_after > 0)' in py)

    check("前端默认根数是具名常量（不再是写死的 120）",
          re.search(r"const DEF_SPAN\s*=\s*60;", code) is not None
          and "(1 - 120 /" not in code)
    check("默认窗口落在查询日附近（用 anchor.index 定位）",
          "function defaultSpan(" in code and "curAnchor.index" in code)
    check("竖虚线走**空数据** series（不进图例 / 不进 tooltip）",
          re.search(r"ANCHOR_NAME\s*=\s*'__anchor__'", code) is not None
          and "data: [], silent:true" in code and "tooltip:{ show:false }" in code)
    check("竖虚线只在「查询日之后有行情」时才画",
          "curAnchor.extended" in code)
    check("竖虚线位置 = 后端给的日期（前端不自己算）",
          "data: [{ xAxis: curAnchor.date }]" in code)
    check("竖虚线文案来自后端（模板里不出现拼出来的「查询日 …」）",
          "anchor_label" in code and "formatter: (curView || {}).anchor_label" in code)
    check("「查询日」徽标文案也来自后端",
          "'#st-anchor-tag'" in code and "view.anchor_label" in code)
    check("整页诊断透传 URL 上的 trade_date（弹窗「在诊断页打开」不丢查询日）",
          "get('trade_date')" in code and "'&trade_date=' + encodeURIComponent(td)" in code)

    # 内联脚本语法（先中和 Jinja，再交给 node --check）
    if os.path.exists(NODE):
        js = re.sub(r"\{\{.*?\}\}", "0", html, flags=re.S)
        js = re.sub(r"\{%.*?%\}", "", js, flags=re.S)
        js = re.sub(r"\{#.*?#\}", "", js, flags=re.S)
        blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)</script>", js)
        src = "\n;\n".join(blocks)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                        encoding="utf-8") as fh:
            fh.write(src)
            tmp = fh.name
        r = subprocess.run([NODE, "--check", tmp], capture_output=True, text=True)
        os.unlink(tmp)
        check("stock.html 内联脚本语法通过", r.returncode == 0,
              (r.stderr or "").strip().splitlines()[:1] and
              (r.stderr or "").strip().splitlines()[0][:120] or "")
    else:
        skip("stock.html 内联脚本语法", "找不到 node")


# ---------------------------------------------------------------------------
def sec2_api() -> None:
    print("【2】接口契约（/api/stock/<code>/detail）")
    d = get_json(f"/api/stock/{CODE}/detail?trade_date={TD}&sel_only=1&bars={BARS}")
    r = d.get("data") or {}
    if r.get("error") or not r.get("rows"):
        check("接口有数据（先确认本地库里有这只票）", False, str(r.get("error")))
        return
    rows, anchor, view = r["rows"], r.get("anchor") or {}, r.get("view") or {}
    n, ai = len(rows), anchor.get("index")

    check("anchor.requested / date = 查询日", anchor.get("requested") == TD
          and anchor.get("date") == TD, json.dumps(anchor, ensure_ascii=False))
    check("anchor.index 落在 rows 里, 且那一根就是查询日",
          isinstance(ai, int) and 0 <= ai < n and rows[ai]["trade_date"] == TD,
          f"index={ai} rows[{ai}]={rows[ai]['trade_date'] if isinstance(ai, int) and 0 <= ai < n else '?'}")
    check("查询日之前恰好 bars 根（窗口起点 = 查询日往前数 bars 根）",
          ai == BARS - 1, f"anchor.index={ai}, bars={BARS}")
    check("查询日之后的行都在（extended=True，窗口到最新交易日）",
          anchor.get("extended") is True and rows[-1]["trade_date"] > TD,
          f"{TD} → {rows[-1]['trade_date']}（之后 {n - 1 - ai} 根）")

    post = rows[ai + 1:]
    bad = [(x["trade_date"], k) for x in post
           for k in ("entry", "buy_trigger", "sell_signal") if x.get(k)]
    check("查询日之后的 entry/buy_trigger/sell_signal 全是 False（未来函数）",
          not bad, str(bad[:3]))
    badsc = [x["trade_date"] for x in post
             if x.get("score") is not None]
    check("查询日之后的 score 全是空", not badsc, str(badsc[:3]))
    check("查询日**当天**的信号是照常算的（清的是它右边的，不是它自己）",
          any(x.get("entry") for x in rows[:ai + 1]),
          f"命中 {sum(1 for x in rows[:ai + 1] if x.get('entry'))} 根（截至查询日）")

    check("view.anchor_label 由后端拼好", view.get("anchor_label") == f"查询日 {TD}",
          repr(view.get("anchor_label")))
    check("view.notes 说明了「右边只画行情、不算信号」",
          any("查询日" in x and "不算" in x for x in (view.get("notes") or [])),
          (view.get("notes") or [""])[-1][:60])
    check("基本面卡片取的是**查询日**那根（不是 K 线末根）",
          (r.get("fundamental") or {}).get("as_of") == TD,
          f"as_of={(r.get('fundamental') or {}).get('as_of')} / 末根 {rows[-1]['trade_date']}")

    # 收益区间必须止于查询日（否则「本区间收益」变成「拿到今天」的收益）
    d2 = get_json(f"/api/stock/{CODE}/detail?trade_date={TD}&bars={BARS}")
    r2 = d2.get("data") or {}
    p = (r2.get("perf") or {}).get("range") or {}
    check("策略收益区间止于查询日（不是拿到今天）", p.get("end") == TD,
          f"{p.get('start')} ~ {p.get('end')}（{p.get('bars')} 根）")
    check("策略收益区间起点 = 图上第一根",
          p.get("start") == (r2.get("rows") or [{}])[0].get("trade_date"),
          f"{p.get('start')}")

    # 退化：不带 trade_date（看最新那天）
    d3 = get_json(f"/api/stock/{CODE}/detail?sel_only=1&bars={BARS}")
    r3 = d3.get("data") or {}
    rows3, a3 = r3.get("rows") or [], r3.get("anchor") or {}
    check("不带 trade_date：锚点 = 最后一根、extended=False",
          a3.get("index") == len(rows3) - 1 and a3.get("extended") is False,
          f"index={a3.get('index')}/{len(rows3) - 1} extended={a3.get('extended')}")
    check("不带 trade_date：窗口仍是 bars 根（没有「之后」可加载）",
          len(rows3) == BARS, f"{len(rows3)} 根")


# ---------------------------------------------------------------------------
def sec3_probe() -> None:
    print("【3】浏览器实测（真 dataZoom / 真 option）")
    if not CHROME:
        skip("无头探针", "本机没找到 Chrome")
        return
    profile = os.path.join(tempfile.gettempdir(), "_chrome_kla_prof")
    raw = chrome_probe_out(BASE + PROBE + "?run=1", 150000, 420, profile)
    m = re.search(r"\{[\s\S]*\}", raw or "")
    if not m:
        check("探针返回了结果", False, (raw or "")[:160])
        return
    try:
        data = json.loads(m.group(0))
    except Exception as e:  # noqa: BLE001
        check("探针返回了合法 JSON", False, f"{e}: {(raw or '')[:120]}")
        return
    for c in data.get("checks") or []:
        check(c["name"], bool(c.get("ok")), c.get("extra") or "")
    check("探针跑完了全部检查", data.get("done") is True)


def main() -> int:
    e2e = "--no-e2e" not in sys.argv
    sec1_static()
    sec2_api()
    if e2e:
        sec3_probe()
    else:
        skip("【3】浏览器实测", "--no-e2e")
    print("\nK线查询日回归：%d 通过 / %d 失败 / %d 跳过" % (PASS, FAIL, SKIP))
    if FAILS:
        print("失败项：")
        for x in FAILS:
            print("  - " + x)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
