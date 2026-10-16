#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
深色主题回归（静态口径 + 无头 Chrome 实测计算样式）

为什么需要它：改主题最容易出的事**不是**改错某个变量，而是「某个页面漏了」——
自己在模板里写死的内联样式、ECharts 配置里的浅色坐标轴，静态读文本读不出来，
只能把页面真渲染出来量 getComputedStyle。所以本脚本分两段：

  【1】静态口径   —— :root 变量齐不齐、涨跌色是否被误改、有没有浅色残留字面量、
                      各页是否都继承同一套样式（没有页面自己另起炉灶）
  【2】浏览器实测 —— 8 张页面逐个渲染，量「背景有多暗 / 正文对比度够不够 /
                      图表画布四角像素是不是暗的 / 有没有大块浅底元素」

跑法：
    C:/Users/ming/.workbuddy/binaries/python/envs/stockmining/Scripts/python.exe tools/verify_theme.py
    加 --no-e2e 只跑静态段（不用起服务）。
依赖：服务在 127.0.0.1:8777；本机 Chrome（找不到就跳过【2】并标注）。
"""

import glob
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BASE = "http://127.0.0.1:8777"
PROBE = "/static/_probe_theme.html?run=1"
CSS = os.path.join(ROOT, "static", "css", "app.css")
PAGES = ["/", "/monitor", "/screen", "/backtest", "/strategies", "/pools", "/data", "/stock/600519.SH"]

PASS = FAIL = 0
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


def strip_css_comments(s: str) -> str:
    return re.sub(r"/\*[\s\S]*?\*/", "", s)


CHROME = None
for _c in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
           r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
           os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe")):
    if os.path.exists(_c):
        CHROME = _c
        break


def chrome_dump(url: str, budget_ms: int, timeout: int, profile: str) -> str:
    out = os.path.join(tempfile.gettempdir(), "_theme_dom.html")
    with open(out, "w", encoding="utf-8") as fh:
        subprocess.run(
            [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
             "--user-data-dir=" + profile, "--dump-dom",
             f"--virtual-time-budget={budget_ms}", "--window-size=1440,1000", url],
            stdout=fh, stderr=subprocess.DEVNULL, timeout=timeout,
        )
    return open(out, encoding="utf-8", errors="ignore").read()


def main() -> int:
    e2e = "--no-e2e" not in sys.argv

    print("【1】静态口径（颜色定义与残留）")
    css = open(CSS, encoding="utf-8").read()
    code = strip_css_comments(css)

    # 「面」的颜色必须来自变量：改主题只动 :root，不必满文件找 background
    def rule(sel: str) -> str:
        # (?![\w-]) 是必须的：不然 `.pool-stat` 会先匹配到 `.pool-status{...}`，
        # `.modal` 会先匹配到 `.modal-bg{...}` —— 拿到的是别人的规则体。
        m = re.search(re.escape(sel) + r"(?![\w-])[\s\S]{0,40}?\{([^}]*)\}", code)
        return m.group(1) if m else ""

    check("声明了 color-scheme:dark（原生控件/滚动条跟着变深）", "color-scheme:dark" in code)

    need = ["--bg", "--panel", "--panel-2", "--panel-3", "--line", "--line-2", "--line-hi",
            "--text", "--text-2", "--text-3", "--brand", "--brand-2", "--brand-soft",
            "--brand-solid", "--brand-solid-hi",
            "--up", "--down", "--warn", "--danger", "--up-soft", "--up-line",
            "--down-soft", "--down-line", "--warn-soft", "--warn-line",
            "--terminal", "--overlay", "--shadow", "--mono", "--radius", "--radius-s"]
    miss = [v for v in need if (v + ":") not in code]
    check(f":root 变量齐备（{len(need)} 个）", not miss, ("缺 " + ",".join(miss)) if miss else "")

    # 变量「定义 / 引用」必须对得上：写错一个字母（--line-2 打成 --line2）不会报错，
    # 只会让描边/底色静默继承，页面上看着"少了条线"，很难查。
    defined = set(re.findall(r"--([a-z0-9-]+)\s*:", code))
    used = set()
    for f in [CSS] + sorted(glob.glob(os.path.join(ROOT, "templates", "*.html"))):
        s = strip_css_comments(open(f, encoding="utf-8").read())
        used |= set(re.findall(r"var\(--([a-z0-9-]+)", s))
    used.discard("xxx")          # 注释里的示例写法
    miss = sorted(used - defined)
    check(f"CSS 变量引用都有定义（{len(defined)} 定义 / {len(used)} 引用）", not miss,
          ("引用了未定义 " + ",".join(miss)) if miss else "")

    # 填充按钮的白字必须压得住：--brand(#4b83ff) 上白字只有 3.5:1，不能拿它当按钮底
    check("实心按钮用压暗的 --brand-solid（--brand 只做文字/线条）",
          "background:var(--brand-solid)" in rule(".btn.primary")
          and "background:var(--brand-solid)" in rule(".btn.sm.on")
          and "background:var(--brand)" not in rule(".btn.primary"))

    # 涨跌色是**约定**，不是主题的一部分：改主题不许动它（蜡烛/回测/报告都引用同一组值）
    check("涨跌色锁定在 #e5352b / #12a06a（中国习惯：涨红跌绿）",
          re.search(r"--up:#e5352b\s*;\s*--down:#12a06a", code) is not None)
    check("body 用变量取色（没有写死底色/字色）",
          re.search(r"body\{[^}]*background:var\(--bg\)[^}]*color:var\(--text\)", code) is not None)

    # 浅色残留：CSS 正文里不该再出现浅**底/浅描边**。注意 `color:#fff` 是合法的 ——
    # 那是「彩色底上的白字」（品牌按钮 / 深底 toast），不是浅色底。所以先摘掉它再查。
    code_no_whitetext = re.sub(r"color\s*:\s*#fff(?:fff)?\b", "", code)
    light = ["#fff", "#ffffff", "#f4f6fa", "#fafbfd", "#fcfdff", "#f8faff", "#eef3fb",
             "#fdf2f2", "#fdf3f3", "#fdeaea", "#f0faf5", "#eefaf3", "#fdf0f0", "#fff8e8",
             "#fffafa", "#f7f9fc", "#d5dced", "#eaf0ff", "#f3c9c9", "#bfe6d3", "#d3e0ff",
             "#c6ecda", "#f4cccc", "#e3e8f0", "#eef1f6", "#dfe5ef", "#cfd9ec", "#c8d2e4",
             "#f0d0d0", "#c9d2e2", "#111827", "#96650a", "#0f7a52", "#b32b2b"]
    low = code_no_whitetext.lower()
    found = sorted({h for h in light if h in low})
    check("app.css 里没有浅色底/浅描边残留", not found,
          ("命中 " + ",".join(found)) if found else f"（黑名单 {len(light)} 色）")

    # 「面」的颜色必须来自变量：改主题只动 :root，不必满文件找 background
    surface = [(".sidebar", "--panel"), (".topbar", "--panel"), (".card", "--panel"),
               (".modal", "--panel"), (".ac-pop", "--panel-2"), (".strat-item", "--panel-2"),
               (".pool-panel", "--panel-2"), (".pool-stat", "--panel-2"),
               (".add-cond", "--panel-2"), (".logbox", "--terminal"),
               (".mon-empty", "--panel-2"), (".mon-code", "--panel-2")]
    bad = [f"{s}→{v}" for s, v in surface if f"background:var({v})" not in rule(s)]
    m_inp = re.search(r"input\[type=text\][\s\S]{0,320}?\{([^}]*)\}", code)
    if not (m_inp and "background:var(--panel-2)" in m_inp.group(1)):
        bad.append("input→--panel-2")
    check("各处「面」的颜色都取变量（改主题只改 :root）", not bad,
          ("未走变量 " + ",".join(bad)) if bad else f"（{len(surface) + 1} 处）")

    # 图表侧：ECharts 拿不到 CSS 变量，颜色只能写死在配置里 —— 所以单独做黑名单
    js_files = [f for f in glob.glob(os.path.join(ROOT, "templates", "*.html")) +
                glob.glob(os.path.join(ROOT, "static", "js", "*.js")) if "echarts.min" not in f]
    chart_light = ["#2f6bff", "#5b6579", "#dfe5ef", "#eef1f6", "#e3e8f0", "#f7f9fc",
                   "#33415c", "rgba(27,34,51", "rgba(255,255,255,.94"]
    hits = []
    for f in js_files:
        s = open(f, encoding="utf-8").read()
        for h in chart_light:
            if h in s:
                hits.append(os.path.basename(f) + ":" + h)
    check("模板/JS 里没有浅色图表配色残留", not hits, ("命中 " + ",".join(hits)) if hits else "")

    # ⚠ 图例样式（`#9aa6ba` 那套）与下挂指标线色表**只有一份，在 app.js**
    #   （CHART_LEGEND / CHART_PAL / CHART_FMT，个股诊断页与交易复盘图共用）——
    #   所以这个 token 跟着它落在 app.js 上，页面模板里不再各存一份。
    dark_tokens = {"static/js/app.js": ["#2a3444", "#1f2733", "#e6ebf4", "#9aa6ba"],
                   "templates/index.html": ["#9aa6ba"],
                   "templates/backtest.html": ["#9aa6ba", "#4b83ff", "#151a22"],
                   "templates/stock.html": ["#4b83ff"],
                   "static/js/tradechart.js": ["#4b83ff", "rgba(21,26,34"],
                   "templates/monitor.html": ["#4b83ff", "rgba(16,21,29"]}
    bad = []
    for f, toks in dark_tokens.items():
        s = open(os.path.join(ROOT, f), encoding="utf-8").read()
        for t in toks:
            if t not in s:
                bad.append(f + ":" + t)
    check("图表侧已换成深色token（坐标轴/图例/提示框）", not bad,
          ("缺 " + ",".join(bad)) if bad else f"（{len(dark_tokens)} 个文件）")

    base = open(os.path.join(ROOT, "templates", "base.html"), encoding="utf-8").read()
    check("base.html 有 color-scheme / theme-color（防首屏闪白）",
          'name="color-scheme" content="dark"' in base and 'name="theme-color"' in base)

    tpls = sorted(glob.glob(os.path.join(ROOT, "templates", "*.html")))
    ext = [t for t in tpls if t.endswith("base.html") is False and "extends" not in open(t, encoding="utf-8").read()]
    check(f"全部 {len(tpls)} 个模板都继承 base（主题一处生效、页面不各自为政）", not ext,
          ("游离 " + ",".join(os.path.basename(t) for t in ext)) if ext else "")
    own_root = [os.path.basename(t) for t in tpls
                if re.search(r":root\{[^}]*--bg", open(t, encoding="utf-8").read())]
    check("没有页面自己重定义 --bg（避免半深不深）", not own_root,
          (",".join(own_root)) if own_root else "")

    probe = os.path.join(ROOT, "static", "_probe_theme.html")
    check("主题探针页在（静态资源目录，同源可量真实计算样式）", os.path.exists(probe))

    # ---------------- 【2】浏览器实测 ----------------
    print("\n【2】浏览器实测（无头 Chrome，量真实计算样式）")
    if not (e2e and CHROME):
        print("  跳过：%s" % ("未加 --no-e2e 但没找到 Chrome" if e2e else "指定了 --no-e2e"))
    else:
        profile = tempfile.mkdtemp(prefix="themeverify_")
        try:
            dom = chrome_dump(BASE + PROBE, 40000, 240, profile)
            m = re.search(r'<pre id="out">([\s\S]*?)</pre>', dom)
            data = None
            if m:
                try:
                    data = json.loads(m.group(1).strip())
                except Exception:
                    data = None
            check("探针返回可解析结果", isinstance(data, list) and len(data) == len(PAGES),
                  f"拿到 {len(data) if isinstance(data, list) else 0} / {len(PAGES)} 张页面")

            if isinstance(data, list):
                by_url = {d.get("url"): d for d in data}
                for url in PAGES:
                    d = by_url.get(url)
                    if not d:
                        check(f"{url} 渲染成功", False, "探针没返回该页")
                        continue
                    if not d.get("ok"):
                        check(f"{url} 渲染成功", False, d.get("err", ""))
                        continue
                    ch = d["checks"]
                    cs = ch.get("color-scheme", {})
                    details = []
                    ok = True

                    if "dark" not in str(cs.get("value", "")):
                        ok = False
                        details.append("color-scheme=" + str(cs.get("value")))
                    for base_sel in ("body", ".sidebar", ".topbar"):
                        if base_sel not in ch:
                            ok = False
                            details.append("缺 " + base_sel)
                    for sel, c in ch.items():
                        if sel == "color-scheme":
                            continue
                        if c.get("bgLum", 1) > 0.16:
                            ok = False
                            details.append(f"{sel} 底色亮 lum={c.get('bgLum')}")
                        if c.get("contrast", 0) < 4.5:
                            ok = False
                            details.append(f"{sel} 对比度 {c.get('contrast')}")
                    for cv in d.get("canvases", []):
                        if cv.get("maxLum", 0) > 0.12:
                            ok = False
                            details.append(f"canvas#{cv.get('i')} 四角不暗 lum={cv.get('maxLum')}")
                    if d.get("bright"):
                        ok = False
                        details.append("大块浅底元素 " + " | ".join(d["bright"][:3]))

                    n_sel = len([k for k in ch if k != "color-scheme"])
                    absent = d.get("absent") or []
                    check(f"{url} 全暗 + 对比度达标", ok,
                          f"（量了 {n_sel} 个元素 / {len(d.get('canvases', []))} 块画布"
                          + (f" / 本页无 {'、'.join(absent)}" if absent else "") + "）"
                          if ok else "; ".join(details))
        except Exception as e:
            check("浏览器实测执行完成", False, f"{type(e).__name__}: {e}")
        finally:
            import shutil
            shutil.rmtree(profile, ignore_errors=True)

    print(f"\n{'=' * 62}\n主题回归：{PASS}/{PASS + FAIL} 通过，{FAIL} 失败")
    if FAILS:
        print("失败项：")
        for f in FAILS:
            print("  -", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
