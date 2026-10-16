#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
滑点口径回归（静态口径 + 接口契约 + 无头 Chrome 实测）

为什么需要它：滑点这条链路上有三处**坏了也看不出来**的地方 ——
  ① 回测页那个滑点框在「策略自带滑点」时应当置灰并显示策略的值。不置灰的话，
     用户改了框里的数字、跑出来却没变（值被策略压过），是纯隐形的坑。
  ② 界面填**百分数**（0.2）、库里存**小数**（0.002），换算错一位就是 10 倍差异，
     页面上完全看不出来。
  ③ 上下限来自 `/api/config`，页面一旦写死，改范围就会忘了同步。
这三件事都只能读 DOM / 读接口契约才验得出来，静态读模板读不出来。

三段：
  【1】静态口径   —— 换算是否只有 `app.js` 的 `window.SLIP` 一处、页面有没有自己
                     再写一遍 `×100`、上下限是否取自 `/api/config`、内联脚本语法
  【2】接口契约   —— `/api/config.slippage_range` 是否等于 `strategy.SLIPPAGE_MIN/MAX`、
                     存取往返是否保持小数（不被 % 化）、范围外是否被校验拦下
  【3】浏览器实测 —— `static/_probe_slip.html`：建两条临时策略，在 /strategies 里
                     选中看是否按 % 显示、改 0.25 保存后库里是否为 0.0025；在 /backtest
                     里选中带滑点的策略看框是否置灰跟随、换成没设的看是否回落默认 0.1

跑法：
    C:/Users/ming/.workbuddy/binaries/python/envs/stockmining/Scripts/python.exe tools/verify_slippage.py
    加 --no-e2e 只跑【1】【2】（不用起浏览器）。
依赖：服务在 127.0.0.1:8777；本机 Chrome（找不到就把【3】标 SKIP，不算失败）。
退出码只看 FAIL。
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

from core import strategy as st   # noqa: E402

BASE = "http://127.0.0.1:8777"
PROBE = "/static/_probe_slip.html?run=1"
PROBE_HTML = os.path.join(ROOT, "static", "_probe_slip.html")
APP_JS = os.path.join(ROOT, "static", "js", "app.js")
STRAT_HTML = os.path.join(ROOT, "templates", "strategy.html")
BT_HTML = os.path.join(ROOT, "templates", "backtest.html")
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


def post(path: str, data: dict, timeout: int = 120) -> dict:
    req = urllib.request.Request(BASE + path, data=json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())


def api(path: str, timeout: int = 60) -> dict:
    return json.loads(urllib.request.urlopen(BASE + path, timeout=timeout).read().decode())


def read(p: str) -> str:
    return open(p, encoding="utf-8").read()


CHROME = None
for _c in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
           r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
           os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe")):
    if os.path.exists(_c):
        CHROME = _c
        break


def chrome_probe_out(url: str, budget_ms: int, timeout: int, profile: str) -> str:
    """渲染探针页并返回 <pre id="out"> 里的文本。"""
    out = os.path.join(tempfile.gettempdir(), "_slip_probe_dom.html")
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


def node_syntax(html_path: str, tag: str) -> None:
    """把模板里最大的那段内联 <script> 抠出来给 node --check。"""
    html = read(html_path)
    blocks = re.findall(r"<script(?![^>]*src=)[^>]*>([\s\S]*?)</script>", html)
    big = max(blocks, key=len) if blocks else ""
    if not os.path.exists(NODE):
        skip(f"{tag} 内联脚本语法", "找不到 node")
        return
    js = os.path.join(tempfile.gettempdir(), f"_slip_{tag}.js")
    with open(js, "w", encoding="utf-8") as fh:
        fh.write(big)
    r = subprocess.run([NODE, "--check", js], capture_output=True, text=True)
    check(f"{tag} 内联脚本语法（{len(big)} 字符）", r.returncode == 0,
          (r.stderr or "").strip()[:140])


def main() -> int:
    e2e = "--no-e2e" not in sys.argv

    # ---------------------------------------------------------------- 1. 静态口径
    print("【1】静态口径（换算唯一处 / 上下限来自后端）")
    s_html, b_html, a_js = read(STRAT_HTML), read(BT_HTML), read(APP_JS)

    check("换算入口 window.SLIP 只在 app.js 定义一次",
          a_js.count("window.SLIP") == 1
          and "fromCfg:" in a_js and "toCfg:" in a_js)
    check("买卖策略页用 SLIP 做换算（不再自己 ×100）",
          "SLIP.toCfg($('#ed-slip')" in s_html and "SLIP.fromCfg(e.slippage)" in s_html)
    check("回测页用 SLIP 做换算（不再是 Number(#bt-slip)）",
          "SLIP.toCfg($('#bt-slip')" in b_html and "Number($('#bt-slip')" not in b_html)
    check("页面里没有第二处滑点换算（找不到 slippage 附近的 ×100 / ÷100）",
          not re.search(r"(slippage|滑点)[^\n]{0,40}[*/]\s*100", s_html + b_html))
    check("买卖策略页的上下限取自 /api/config（不写死 0~3）",
          "slippage_range" in s_html and "ed-slip-hint" in s_html)
    check("回测页的上下限同样取自 /api/config",
          "slippage_range" in b_html and "applySlipRange" in b_html)
    check("回测页有「跟随买卖策略」这条连线（置灰 + 标签）",
          b_html.count("function syncSlipFromStrategy") == 1
          and "跟随买卖策略" in b_html
          and "onchange = syncSlipFromStrategy" in b_html)
    check("买卖策略编辑页把滑点写进 exec.slippage",
          re.search(r"exec:\s*\{[\s\S]{0,600}?\n\s*slippage:", s_html) is not None)
    check("探针页存在且带 ?run=1 门禁（它会建/删临时策略）",
          os.path.exists(PROBE_HTML) and "params.get('run') !== '1'" in read(PROBE_HTML))
    node_syntax(STRAT_HTML, "strategy.html")
    node_syntax(BT_HTML, "backtest.html")

    # ---------------------------------------------------------------- 2. 接口契约
    print("\n【2】接口契约（/api/config 与存取往返）")
    try:
        cfg = api("/api/config").get("data") or {}
        rng = cfg.get("slippage_range") or {}
        check("接口暴露了滑点上下限", rng.get("min") == st.SLIPPAGE_MIN
              and rng.get("max") == st.SLIPPAGE_MAX,
              f"{rng.get('min')} ~ {rng.get('max')}")
        check("默认值仍在 defaults 里（策略未设时用它）",
              (cfg.get("defaults") or {}).get("slippage") == 0.001,
              str((cfg.get("defaults") or {}).get("slippage")))

        okc = str(st.validate_config({"exec": {"slippage": 0.5}}))
        check("范围外（50%）被校验拦下且文案按百分数说",
              okc != "[]" and "50%" in okc and "3%" in okc, okc)
        check("合法值 / 留空都通过",
              st.validate_config({"exec": {"slippage": 0.002}}) == []
              and st.validate_config({"exec": {"slippage": None}}) == [])
        check("上下限本身可填（边界含在内）",
              st.validate_config({"exec": {"slippage": st.SLIPPAGE_MAX}}) == []
              and st.validate_config({"exec": {"slippage": st.SLIPPAGE_MIN}}) == [])

        # 存取往返：库里永远是小数，不被 % 化（防双重换算）
        made = post("/api/strategies", {
            "name": "__probe_slip_api__", "note": "滑点接口契约（用完即删）",
            "config": {"buy_trigger": ["open > 0"], "sell_signal": ["close < ma5"],
                       "exec": {"slippage": 0.0025}}})
        sid = (made.get("data") or {}).get("id")
        check("建策略接口返回 id", bool(sid), f"id={sid}")
        if sid:
            got = [x for x in (api("/api/strategies").get("data") or []) if x["id"] == sid]
            val = (((got[0] if got else {}).get("config") or {}).get("exec") or {}).get("slippage")
            check("存 0.0025 读回还是 0.0025（存储口径=小数，不做 % 化）",
                  val == 0.0025, f"读回 {val}")
            # 编辑页提交的形态：0.25(%) → 0.0025，再存一次应等价
            post("/api/strategies", {
                "id": sid, "name": "__probe_slip_api__",
                "config": {"buy_trigger": ["open > 0"], "sell_signal": ["close < ma5"],
                           "exec": {"slippage": 0.0025}}})
            val2 = [x for x in (api("/api/strategies").get("data") or [])
                    if x["id"] == sid][0]
            check("同一值反复保存不漂移（不会 0.0025 → 0.25 → 25）",
                  (((val2.get("config") or {}).get("exec") or {}).get("slippage")) == 0.0025,
                  str(((val2.get("config") or {}).get("exec") or {}).get("slippage")))
            urllib.request.urlopen(urllib.request.Request(
                BASE + f"/api/strategies/{sid}", method="DELETE"), timeout=30).read()
            left = [x for x in (api("/api/strategies").get("data") or []) if x["id"] == sid]
            check("临时策略已删", not left)
    except Exception as e:
        check("接口契约（服务起了吗？）", False, f"{type(e).__name__}: {e}")

    # ---------------------------------------------------------------- 3. 浏览器实测
    if e2e and CHROME:
        print("\n【3】浏览器实测（建两条临时策略，真点编辑页与回测页）")
        import shutil
        profile = os.path.join(tempfile.gettempdir(), "_slip_probe_profile")
        shutil.rmtree(profile, ignore_errors=True)
        try:
            body = chrome_probe_out(BASE + PROBE, 150000, 420, profile)
            data = None
            m = re.search(r"\{[\s\S]*\}", body)
            if m:
                try:
                    data = json.loads(m.group(0))
                except Exception:
                    data = None
            if not data:
                check("探针页返回了结果 JSON", False, (body or "").strip()[:160])
            else:
                for c in data.get("checks") or []:
                    check(c["name"], c["ok"], c.get("extra", ""))
                check("探针跑完没有中途异常", not data.get("error"), str(data.get("error"))[:160])
        except Exception as e:
            check("浏览器实测（Chrome 起了吗？）", False, f"{type(e).__name__}: {e}")
    else:
        skip("浏览器实测", "加了 --no-e2e" if not e2e else "找不到本机 Chrome")

    print("\n" + "=" * 66)
    print(f"滑点回归：{PASS} 通过 / {FAIL} 失败 / {SKIP} 跳过")
    for f in FAILS:
        print("   FAIL " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
