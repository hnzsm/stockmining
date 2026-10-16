#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
「可用字段参考」的字段清单与「示例」取值回归（静态口径 + 接口契约 + 无头浏览器实测）

为什么需要它：这一块有两类错，读代码/读文本都发现不了 ——
  1) **字段清单漂移**：面板上的字段是从后端拼出来的，拼法一改就可能「少一批、多一个假的」。
     2026-09-20 修的就是这个：清单以前拿 `FIELD_HELP` 逐行拆名字，而 `ma5…ma250`
     这种省略号写法拆不出真字段名（过不了 `FIELD_RE`），于是 ma5/ma20/ma60、boll_dev、
     ret60、pos120、ma_bull… 十几个真能用的字段在面板里**查不到**，反倒把
     「ma5…ma250」这个不是字段名的东西列了出来。
  2) **示例值与选股口径不一致**：弹窗里的数是「参考值」，一旦自己写条 SQL 去统计，
     价格类字段立刻错（选股用前复权、表里存未复权），而且不会有人发现。
     所以这里做**对拍**：拿同一只票、同一天，用 `market.load_panel` 自己算一遍，
     与接口给的值逐值相等才算过。

分三段：
  【1】静态口径   —— 清单来源（condition_field_help 唯一来源）、FIELD_HELP 里不许有省略号名字、
                     示例链接的渲染与点击都收在 app.js（两页共用）、CSS 齐全
  【2】接口契约   —— 两个字段清单接口**集合一致**、无重复、desc 齐全；
                     /api/fields/sample 的形状（7 档分位、单调不减、样例行）；
                     **值与 load_panel 对拍**；错字段/空日期要给人话错误
  【3】浏览器实测 —— 把 /pools、/strategies 塞进同源 iframe，逐行抠「示例」链接（字段名要对得上、
                     不串行），真点一次出弹窗（分位卡片/样例行/样本口径/换日期报错）

跑法：
    .venv/Scripts/python.exe tools/verify_fields.py
    加 --no-e2e 只跑【1】【2】（不用起浏览器）。
依赖：服务在 127.0.0.1:8777；本机 Chrome（找不到就把【3】标 SKIP，不算失败）。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BASE = "http://127.0.0.1:8777"
PROBE = "/static/_probe_field_sample.html?run=1"
APP_PY = os.path.join(ROOT, "app.py")
IND_PY = os.path.join(ROOT, "core", "indicators.py")
APP_JS = os.path.join(ROOT, "static", "js", "app.js")
POOL_JS = os.path.join(ROOT, "static", "js", "pool.js")
CSS = os.path.join(ROOT, "static", "css", "app.css")
POOL_HTML = os.path.join(ROOT, "templates", "pool.html")
STRAT_HTML = os.path.join(ROOT, "templates", "strategy.html")

# 2026-09-20 之前「选股策略页字段参考」里查不到的那一批（省略号写法整行被丢）
LOST_BEFORE = ["ma5", "ma10", "ma20", "ma30", "ma60", "ma120", "ma250", "ma_bull",
               "boll_dev", "ret60", "ret120", "pos120", "hh120", "ll120",
               "close_pos", "change_amt", "mtmpct12", "obv_ma10", "vol_ratio10"]

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


def get_json(path: str, timeout: int = 300):
    """返回 (payload, status)。payload 是解析后的整包（含 ok/data/error）。"""
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            return json.loads(r.read().decode()), r.status
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode()), e.code
        except Exception:  # noqa: BLE001
            return {}, e.code


CHROME = None
for _c in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
           r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
           os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe")):
    if os.path.exists(_c):
        CHROME = _c
        break


def chrome_probe_out(url: str, budget_ms: int, timeout: int, profile: str) -> str:
    out = os.path.join(tempfile.gettempdir(), "_fields_probe_dom.html")
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

    # ------------------------------------------------------------ 1. 静态口径
    print("【1】静态口径（清单来源 / 省略号名字 / 渲染入口）")
    apppy = open(APP_PY, encoding="utf-8").read()
    indpy = open(IND_PY, encoding="utf-8").read()
    appjs = open(APP_JS, encoding="utf-8").read()
    css = open(CSS, encoding="utf-8").read()
    pool = open(POOL_HTML, encoding="utf-8").read()
    strat = open(STRAT_HTML, encoding="utf-8").read()

    uf = apppy[apppy.find("def api_universe_fields"):]
    uf = uf[:uf.find("\n@app.route")]
    check("清单接口以 condition_field_help 为准（不再从 FIELD_HELP 直接拼）",
          "st.condition_field_help()" in uf and "in daily" in uf)
    check("清单接口把 FIELD_HELP 只用来取分组/顺序（不再直接 append desc）",
          "group.setdefault(part, names)" in uf and "out.append({\"name\": part" not in uf)
    check("清单接口仍带周线字段（w_ 前缀）", "condition_weekly_field_help()" in uf)

    # FIELD_HELP：名字列里不许出现省略号/区间写法 —— 那类写法拆不出真字段名
    fh = indpy[indpy.find("FIELD_HELP = ["):]
    fh = fh[:fh.find("\n]\n") + 3]
    names_blob = " | ".join(re.findall(r'^\s*\("([^"]+)",', fh, re.M))
    bad_tokens = [t for t in re.split(r"\s*/\s*|\s*\|\s*", names_blob)
                  if t.strip() and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", t.strip())]
    check("FIELD_HELP 的名字列全是能拆出真字段名的写法（没有 ma5…ma250 这种）",
          not bad_tokens, "可疑：" + ",".join(bad_tokens[:6]))
    for n in ("ma5", "ma250", "boll_dev", "ret120", "pos120", "ma_bull",
              "mtmpct12", "obv_ma10", "vol_ratio10", "close_pos", "change_amt"):
        if n not in fh:
            check(f"FIELD_HELP 里补上了 {n}（这行曾经整行被丢）", False, n)
            break
    else:
        check("以前整行被丢的那批字段都写进 FIELD_HELP 了", True,
              f"{len(LOST_BEFORE)} 个")

    check("示例行的渲染只有一份实现（app.js 的 fieldHelpRow）",
          appjs.count("window.fieldHelpRow =") == 1
          and "window.bindFieldHelp =" in appjs)
    check("点击走事件委托、只绑一次（列表再渲染也不会绑丢/绑重）",
          "el.__fhBound" in appjs and "closest('[data-f]')" in appjs)
    check("选股策略页用同一个渲染（不再自己拼 .fh 行）",
          "window.fieldHelpRow" in pool
          and '<div class="fh"><span class="n">${esc(x.name)}' not in pool)
    check("买卖策略页也用同一个渲染",
          "window.fieldHelpRow" in strat
          and "const fh = n => window.fieldHelpRow" in strat)
    check("示例值弹窗走 mkModal（不是又造一层弹层）",
          "window.mkModal(name + ' · 取值示例'" in appjs)
    pooljs = open(POOL_JS, encoding="utf-8").read()
    check("池编辑器（回测页内嵌）的字段 chip 也接了同一个示例弹窗",
          'data-f="' in pooljs and "window.bindFieldHelp(hp)" in pooljs)
    check("示例值不用 fmt.big（字段单位各异，自动加亿/万会读成第二个口径）",
          "sampleNum" in appjs and "不用 fmt.big" in appjs)
    check("弹窗样式在 app.css 里（.fh-ex / .fs-stat / .fs-tbl）",
          ".fieldhelp .fh .d .fh-ex" in css and ".fs-stat" in css and ".fs-tbl" in css)

    # ------------------------------------------------------------ 2. 接口契约
    print("【2】接口契约（两份清单一致 / 示例形状 / 与选股口径对拍）")
    fields_pack, st1 = get_json("/api/fields")
    uni_pack, st2 = get_json("/api/universe/fields")
    check("两个字段清单接口都 200", st1 == 200 and st2 == 200, f"{st1}/{st2}")
    daily_names = set(((fields_pack.get("data") or {}).get("fields")) or [])
    uni_rows = uni_pack.get("data") or []
    uni_names = [x.get("name") for x in uni_rows]
    check("清单接口返回非空", len(daily_names) > 100 and len(uni_names) > 100,
          f"/api/fields {len(daily_names)} · /api/universe/fields {len(uni_names)}")

    check("**两份清单字段集合完全一致**（面板查不到的字段 = 实际不能用的字段）",
          set(uni_names) == daily_names,
          "只在 /api/fields：" + str(sorted(daily_names - set(uni_names))[:6])
          + " 只在 universe/fields：" + str(sorted(set(uni_names) - daily_names)[:6]))
    check("清单没有重复行", len(uni_names) == len(set(uni_names)),
          f"{len(uni_names)} 行 / 去重 {len(set(uni_names))}")
    check("字段名都是合法标识符（没有 ma5…ma250 这类混进来）",
          all(re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", n or "") for n in uni_names),
          ",".join([n for n in uni_names if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", n or "")][:5]))
    check("每行都有中文说明（desc 非空）",
          all((x.get("desc") or "").strip() for x in uni_rows),
          ",".join([str(x.get("name")) for x in uni_rows if not (x.get("desc") or "").strip()][:6]))
    miss_lost = [n for n in LOST_BEFORE if n not in set(uni_names)]
    check("以前在面板里查不到的那批字段现在都在", not miss_lost, "缺 " + ",".join(miss_lost))
    wk_rows = [x for x in uni_rows if (x.get("name") or "").startswith("w_")]
    check("周线字段（w_ 前缀）也在这份清单里，且带说明",
          len(wk_rows) > 50 and all((x.get("desc") or "").startswith("周线·") for x in wk_rows),
          f"{len(wk_rows)} 个周线字段")

    # ---- 示例接口：形状
    SM = 7
    QS = ["最小值", "10 分位", "25 分位", "中位数", "75 分位", "90 分位", "最大值"]
    d, code = get_json("/api/fields/sample?name=turnover_rate")
    x = d.get("data") or {}
    check("GET /api/fields/sample 返回 ok", code == 200 and d.get("ok") is True, f"HTTP {code}")
    check("形状齐：name/desc/trade_date/sample/stats/rows/note/empty",
          all(k in x for k in ("name", "desc", "trade_date", "sample", "stats",
                               "rows", "note", "empty")),
          "缺 " + ",".join(k for k in ("name", "desc", "trade_date", "sample", "stats",
                                       "rows", "note", "empty") if k not in x))
    check("分位档位与后端定义一致（7 档、名字对上）",
          [s.get("k") for s in x.get("stats") or []] == QS,
          ",".join(str(s.get("k")) for s in x.get("stats") or []))
    vals = [s.get("v") for s in x.get("stats") or []]
    check("分位单调不减（数据乱了第一步就看出来）",
          all(isinstance(v, (int, float)) for v in vals)
          and all(vals[i] <= vals[i + 1] for i in range(len(vals) - 1)),
          " ".join(f"{v:.4g}" for v in vals if isinstance(v, (int, float))))
    check("样例行不超过 8 行、代码/名称/值都在",
          len(x.get("rows") or []) <= 8
          and all(re.match(r"^\d{6}\.(SH|SZ|BJ)$", r.get("ts_code") or "")
                  and isinstance(r.get("value"), (int, float))
                  for r in x.get("rows") or []),
          f"{len(x.get('rows') or [])} 行")
    basis = (x.get("sample") or {}).get("basis") or ""
    check("写明样本口径（多少只 / 怎么抽的 / 多少天的值）",
          "全市场" in basis and "等距抽" in basis
          and (x.get("sample") or {}).get("days", 0) > 0, basis)
    check("desc 与字段清单同一份（不是另写一句）",
          x.get("desc") == next((r["desc"] for r in uni_rows if r["name"] == "turnover_rate"), None),
          str(x.get("desc")))

    # ---- 示例接口：与选股口径对拍
    from core import market, screener, strategy as st  # noqa: E402
    for fld in ("turnover_rate", "ma20"):
        d2, _c = get_json(f"/api/fields/sample?name={fld}")
        r2 = d2.get("data") or {}
        rows = r2.get("rows") or []
        end = r2.get("trade_date")
        if not rows or not end:
            check(f"「{fld}」拿得到样例行用来对拍", False, "没有样例行")
            continue
        code0, v0 = rows[0]["ts_code"], rows[0]["value"]
        start = screener._lookback_start(end, 300)
        only = {fld}
        panel = market.load_panel([code0], start, end, adjust="qfq", only=only)
        df = panel.get(code0)
        if df is None or fld not in getattr(df, "columns", []):
            check(f"「{fld}」对拍：load_panel 能算出这个字段", False, f"{code0} 没有 {fld}")
            continue
        dts = df["trade_date"].astype(str).str.slice(0, 10).tolist()
        pos = [i for i, v in enumerate(dts) if v == end]
        mine = float(df[fld].iloc[pos[-1]]) if pos else None
        check(f"「{fld}」示例值 == load_panel 现算（同一只票、同一天，前复权同口径）",
              mine is not None and abs(mine - v0) <= 1e-9 * max(1.0, abs(mine)),
              f"{code0} @{end} 接口 {v0} vs 现算 {mine}")

    # ---- 示例接口：错误分支
    e1, c1 = get_json("/api/fields/sample?name=definitely_not_a_field")
    check("未知字段 → 400 + ok:false + 人话（不静默给个空弹窗）",
          c1 == 400 and e1.get("ok") is False and "未知字段" in str(e1.get("error")), f"HTTP {c1}")
    e2, c2 = get_json("/api/fields/sample?name=ma20&trade_date=2019-01-02")
    check("没有数据的交易日 → 400 + 写明是那天没数据",
          c2 == 400 and e2.get("ok") is False and "没有行情数据" in str(e2.get("error")),
          f"HTTP {c2} {e2.get('error')}")
    e3, c3 = get_json("/api/fields/sample?name=")
    check("空字段名 → 400（不是拿全表第一列糊弄过去）",
          c3 == 400 and e3.get("ok") is False, f"HTTP {c3}")

    # ------------------------------------------------------------ 3. 浏览器实测
    print("【3】浏览器实测（真点「示例」，看弹窗里的东西）")
    if CHROME and e2e:
        profile = tempfile.mkdtemp(prefix="fields_probe_")
        try:
            body = chrome_probe_out(BASE + PROBE, 240000, 600, profile)
            data = None
            m = re.search(r"\{[\s\S]*\}", body)
            if m:
                try:
                    data = json.loads(m.group(0))
                except Exception:  # noqa: BLE001
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
    else:
        skip("浏览器实测", "无 Chrome 或指定了 --no-e2e")

    print(f"\n{'=' * 62}\n可用字段参考回归：{PASS} 通过 / {FAIL} 失败 / {SKIP} 跳过")
    if FAILS:
        print("失败项：")
        for x2 in FAILS:
            print("  -", x2)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
