"""模型设置页的布局回归：表格不许顶破卡片、常见宽度下不出现横向滚动。

为什么单独有这一个脚本
    模型配置列表那 5 列的宽度由**用户填的内容**决定（配置名、模型名、接口地址都不受控）。
    2026-09-22 实测：填一条长地址 + 长模型名之后，表格最小宽涨到 1069px，在 1280 宽的窗口里
    顶出容器 120px —— 「操作」列的按钮被推到卡片外，得先横向拖才点得到。用户报的就是这个。
    修法是把「列表」从「左右各半」改成整行、再给长内容列限宽（`.prof-*` / `.tpl-vars`），
    本文把这套结论锁住，免得将来谁又把它塞回半幅或把 max-width 删掉。

量的是什么
    真渲染数据（无头 Chrome + 同源 iframe 探针 static/_probe_layout.html），不是猜 CSS：
      · 每条 `.tbl-wrap` 的 scrollWidth ≤ clientWidth —— 整表放得下，不用横向拖
      · 整页不出现横向滚动条（内容没撑破视口）
      · 除滚动容器外，没有元素右边缘越过父元素（真的压到隔壁去那种）
      · 长内容列确实被限宽了（.prof-url / .prof-model / .prof-name / .tpl-vars）
    为了真的触发「长内容」，脚本会临时建一条长地址 + 长模型名的配置，量完删掉。

跑法
    C:/Users/ming/.workbuddy/binaries/python/envs/stockmining/Scripts/python.exe tools/verify_layout.py
    依赖：服务在 127.0.0.1:8777；本机 Chrome（找不到就 SKIP，不算失败）。
    副作用：临时建 1 条探针配置（名字「探针-超长地址」，结束时删掉，并核对配置 id 列表没变）。
"""

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BASE = "http://127.0.0.1:8777"
PROBE = "/static/_probe_layout.html"
WIDTHS = ("1500", "1280", "1100")

# 一条「长得不讲理」的配置：自定义网关 + 长路径 + HuggingFace 风格的长模型名。
# 它只是为了把表格的最小宽度顶出来，量完就删。
LONG_NAME = "探针-超长地址"
LONG_URL = ("https://gateway.internal.corp.cn/v1/proxy/team-quant/llm-router/"
            "very-long-path-segment-abcdefghijklmnopqrstuvwxyz-0123456789/v1")
LONG_MODEL = "Qwen/Qwen2.5-72B-Instruct-AWQ-Int4-very-long-suffix-name"

PASS = 0
FAIL = 0
SKIP = 0
FAILS = []


def check(name, ok, extra=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok   {name}" + (f"   {extra}" if extra else ""))
    else:
        FAIL += 1
        FAILS.append(name)
        print(f"  FAIL {name}" + (f"   {extra}" if extra else ""))


def skip(name, why):
    global SKIP
    SKIP += 1
    print(f"  --   跳过：{name}（{why}）")


def req(path, data=None, timeout=30):
    url = BASE + path
    if data is None:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    body = json.dumps(data).encode("utf-8")
    rq = urllib.request.Request(url, headers={"Content-Type": "application/json"}, data=body)
    with urllib.request.urlopen(rq, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def find_chrome():
    for c in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe")):
        if os.path.exists(c):
            return c
    return None


def shot(chrome, width):
    """按指定窗口宽度跑一次探针页，取回它写在 <pre id="out"> 里的 JSON。"""
    out = os.path.join(tempfile.gettempdir(), "_lay_dom_%s.html" % width)
    profile = os.path.join(tempfile.gettempdir(), "_lay_prof")
    with io.open(out, "w", encoding="utf-8") as fh:
        subprocess.run(
            [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
             "--user-data-dir=" + profile, "--dump-dom",
             "--virtual-time-budget=25000", "--window-size=%s,1200" % width,
             BASE + PROBE],
            stdout=fh, stderr=subprocess.DEVNULL, timeout=180)
    dom = io.open(out, encoding="utf-8", errors="ignore").read()
    try:
        os.remove(out)          # Chrome 有时候还没松手（Windows 独占），删不掉就算了
    except OSError:
        pass
    m = re.search(r'<pre id="out">([\s\S]*?)</pre>', dom)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return {"error": m.group(1)[:300]}


def main():
    chrome = find_chrome()
    print("【1】静态口径")
    css = io.open(os.path.join(ROOT, "static", "css", "app.css"), encoding="utf-8").read()
    tpl = io.open(os.path.join(ROOT, "templates", "settings.html"), encoding="utf-8").read()
    check("两张表都包了横向滚动兜底（.tbl-wrap）",
          'class="tbl-wrap" id="prof-list"' in tpl and 'class="tbl-wrap" id="tpl-list"' in tpl)
    check("列表写在表单栅格之前（整行布局）",
          tpl.find('id="prof-list"') < tpl.find('class="grid g2"')
          and tpl.find('id="tpl-list"') > 0)
    for cls, cap in ((".prof-name", 115), (".prof-model", 175), (".prof-preset", 75),
                     (".prof-url", 175), (".tpl-vars", 265)):
        m = re.search(re.escape(cls) + r"\{[^}]*max-width:(\d+)px", css)
        w = int(m.group(1)) if m else 0
        check(f"长内容列有限宽：{cls} ≤ {cap}px", bool(m) and w <= cap, f"实际 {w}px")
    check("限宽那几列都写了 title（截断后还能看全）",
          tpl.count('class="prof-') >= 3 and 'title="${esc(p.base_url)}"' in tpl
          and 'title="${esc(t.vars.join' in tpl)
    nav = io.open(os.path.join(ROOT, "templates", "base.html"), encoding="utf-8").read()
    check("导航里这页叫「模型设置」", ">模型设置</a>" in nav)

    if not chrome:
        skip("真渲染量宽（无头 Chrome）", "没找到 Chrome")
    else:
        print("\n【2】真渲染量宽（临时塞一条长地址 + 长模型名，量完删掉）")
        before = [p["id"] for p in (req("/api/ai/config").get("data") or {}).get("profiles") or []]
        d = req("/api/ai/profiles", {"profile": {
            "name": LONG_NAME, "preset": "custom", "kind": "openai",
            "base_url": LONG_URL, "model": LONG_MODEL,
            "temperature": 0.3, "max_tokens": 1024, "timeout": 120}})
        pid = next((p["id"] for p in (d.get("data") or {}).get("profiles") or []
                    if p["name"] == LONG_NAME), None)
        check("临时配置建起来了（拿它当「最长内容」的样本）", pid is not None and d.get("ok") is True)
        try:
            for w in WIDTHS:
                got = shot(chrome, w)
                if not got or got.get("error"):
                    check(f"窗口宽 {w}：探针页跑完并回报", False, str((got or {}).get("error"))[:120])
                    continue
                for it in got["res"]:
                    check(f"窗口宽 {w}：{it['name']}", it["ok"], it.get("extra", ""))
        finally:
            if pid:
                req("/api/ai/profiles/%d/delete" % pid, {})
                after = [p["id"] for p in
                         (req("/api/ai/config").get("data") or {}).get("profiles") or []]
                check("量完把临时配置删掉了（用户原来那几条 id 没变）", after == before,
                      f"现在 {after}，原来 {before}")

    print(f"\n通过 {PASS} / 失败 {FAIL} / 跳过 {SKIP}")
    if FAILS:
        print("失败项：")
        for f in FAILS:
            print("  -", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
