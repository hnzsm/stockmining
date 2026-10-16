# -*- coding: utf-8 -*-
"""总览页「热股榜」验证：代码映射口径 + 失败隔离 + 缓存 + 接口形状 + 静态口径。

⚠ 断言纪律：**榜单内容是站外的、每次都变**，所以这里只断言
「形状 / 口径 / 降级行为」，绝不写死某只票、某个条数（那必然是假红）。
站外接口在离线环境必定失败 —— 这类检查一律允许「取数失败」，
但**结构**必须齐全（`error` 非空即算通过）。

跑法：
    "C:/Users/ming/WorkBuddy/stockmining/.venv/Scripts/python.exe" tools/verify_hot.py
"""
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

from core import hot  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASS, FAIL, SKIP = [], [], []


# ---------------------------------------------------------------------------
# 无头 Chrome + 同源探针页（前端断言只用这一条路，别用 agent-browser —— 见 CONVENTIONS C17）
# ---------------------------------------------------------------------------
def _find_chrome() -> str:
    for p in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
              "/usr/bin/google-chrome", "/usr/bin/chromium"):
        if os.path.exists(p):
            return p
    return ""


CHROME = _find_chrome()


def chrome_probe_out(url: str, budget_ms: int, timeout: int, profile: str) -> str:
    """渲染探针页并返回 <pre id="out"> 里的文本。"""
    out = os.path.join(tempfile.gettempdir(), "_hot_probe_dom.html")
    try:
        with open(out, "w", encoding="utf-8") as fh:
            subprocess.run(
                [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
                 "--user-data-dir=" + profile, "--window-size=1560,1200",
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


def check(name, ok, extra=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'OK  ' if ok else 'FAIL'} {name}{('  ' + extra) if extra else ''}")


def skip(name, why=""):
    SKIP.append(name)
    print(f"  SKIP {name}{('  ' + why) if why else ''}")


print("=" * 78)
print("总览页 热股榜（东方财富 / 同花顺 / 雪球）验证")
print("=" * 78)

# ---------------------------------------------------------------------------
print("\n### 1. 代码映射：只认数字段，不信来源给的市场前缀")
# ---------------------------------------------------------------------------
# 期望值是**手写**的独立口径，不引用被测代码里的规则。
CASES = [
    ("SZ301686", "301686.SZ"),      # 东财正常格式
    ("SH600664", "600664.SH"),
    ("600664", "600664.SH"),
    ("000066", "000066.SZ"),
    ("SZ000066", "000066.SZ"),
    ("300750.SZ", "300750.SZ"),
    ("SZ920229", "920229.BJ"),      # ⚠ 东财把北交所标成 SZ —— 必须按数字段纠回来
    ("920229", "920229.BJ"),
    ("830799", "830799.BJ"),
    ("430047", "430047.BJ"),
    ("688662.SH", "688662.SH"),
    ("sh600664", "600664.SH"),      # 大小写不敏感
    ("00700", ""),                  # 港股（5 位）
    ("SNDK", ""),                   # 美股
    ("HK00700", ""),
    ("", ""),
    (None, ""),
]
bad = [f"{a!r}->{hot.ts_code_of(a)!r}≠{b!r}" for a, b in CASES if hot.ts_code_of(a) != b]
check(f"ts_code_of 映射 {len(CASES)} 例全对", not bad, "; ".join(bad[:4]))

# ---------------------------------------------------------------------------
print("\n### 2. 数值 / 错误信息的小工具")
# ---------------------------------------------------------------------------
num_cases = [(None, None), ("", None), ("--", None), ("abc", None),
             ("1.5", 1.5), (3, 3.0), ("-6.24", -6.24)]
bad = [f"{a!r}->{hot._num(a)!r}≠{b!r}" for a, b in num_cases if hot._num(a) != b]
check(f"_num {len(num_cases)} 例全对（--/空/非法 → None）", not bad, "; ".join(bad[:4]))

t_msg = hot._err_text(requests.exceptions.Timeout())
c_msg = hot._err_text(requests.exceptions.ConnectionError())
check("超时说人话", "超时" in t_msg, t_msg)
check("连不上说人话", "连不上" in c_msg, c_msg)

# ---------------------------------------------------------------------------
print("\n### 3. 源清单：恰好 3 个、key 唯一、且**没有新浪**（新浪无公开热股接口）")
# ---------------------------------------------------------------------------
keys = [s["key"] for s in hot.SOURCES]
check("SOURCES 恰好 3 个", len(hot.SOURCES) == 3, str(keys))
check("key 不重复且与 SOURCE_KEYS 一致",
      len(set(keys)) == len(keys) and keys == hot.SOURCE_KEYS)
check("每项都有 title / subtitle",
      all(s.get("title") and s.get("subtitle") for s in hot.SOURCES))
check("没有新浪这一源（已查实无公开接口）",
      not any("sina" in k or "新浪" in s["title"] for k, s in zip(keys, hot.SOURCES)))
check("_FETCHERS 与 SOURCES 一一对应", set(hot._FETCHERS) == set(keys))

# ---------------------------------------------------------------------------
print("\n### 4. 失败隔离：个别源挂掉不影响其它源，也不抛异常")
# ---------------------------------------------------------------------------
real4 = dict(hot._FETCHERS)
try:
    def boom(_n):
        raise RuntimeError("模拟：接口改版了")
    hot._FETCHERS["ths"] = boom
    got = hot.fetch_one("ths", 5)
    check("挂掉的源 error 非空、items 为空、不抛异常",
          got["error"] and got["items"] == [] and got["key"] == "ths",
          f'error={got["error"]!r}')
    hot._FETCHERS["eastmoney"] = boom
    d = hot.get_all(5, force=True)
    by = {s["key"]: s for s in d["sources"]}
    check("两个源挂掉后结果里仍有 3 张卡（结构完整）", len(d["sources"]) == 3)
    check("挂掉的那两张都带 error（另一张不连带失败）",
          by["ths"]["error"] and by["eastmoney"]["error"],
          f'雪球 error={by["xueqiu"]["error"]!r}')
    check("全挂时不抛异常（页面能拿到 ok 响应）",
          isinstance(d, dict) and d["sources"][0]["key"] == "eastmoney")
finally:
    hot._FETCHERS.update(real4)

# ---------------------------------------------------------------------------
print("\n### 5. 缓存：同 n 命中缓存，n 变了必须重取")
# ---------------------------------------------------------------------------
calls = []
real5 = dict(hot._FETCHERS)
try:
    def stub(k):
        def f(n):
            calls.append((k, n))
            return [{"rank": 1, "ts_code": "600000.SH", "name": f"桩{k}",
                     "pct_chg": 1.0, "hot": 2.0, "tag": None, "note": None}]
        return f
    for k in hot.SOURCE_KEYS:
        hot._FETCHERS[k] = stub(k)
    hot.get_all(7, force=True)
    n1 = len(calls)
    d2 = hot.get_all(7, force=False)
    check("同 n 第二次走缓存（cached=True 且未再取数）",
          d2["cached"] is True and len(calls) == n1, f"calls {n1}→{len(calls)}")
    hot.get_all(3, force=False)
    check("n 变了必须重取（n 是榜单长度，不是过滤条件）",
          len(calls) > n1, f"calls {n1}→{len(calls)}")
    check("缓存命中时返回的结构与取数时一致（as_of/latest_trade_date 都在）",
          all(k in d2 for k in ("as_of", "n", "latest_trade_date", "sources")))
finally:
    hot._FETCHERS.update(real5)

# ---------------------------------------------------------------------------
print("\n### 6. 静态口径：标题只在后端、颜色只走 --up/--down、不写 sync_log")
# ---------------------------------------------------------------------------
tpl = open(os.path.join(ROOT, "templates", "index.html"), encoding="utf-8").read()
css = open(os.path.join(ROOT, "static", "css", "app.css"), encoding="utf-8").read()
src = open(os.path.join(ROOT, "core", "hot.py"), encoding="utf-8").read()
app = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()

for need in ('id="hot-grid"', 'id="hot-refresh"', 'id="hot-asof"', 'class="grid g3"'):
    check(f"index.html 有 {need}", need in tpl)

check("页面不写死源的名字（唯一来源是后端 SOURCES）",
      not any(w in tpl for w in ("东方财富", "同花顺", "雪球")),
      "卡片标题应从 /api/hot 的 title 取")
check("热股榜 JS 用 fmt.cls / fmt.pct 判涨跌色（不在页面拼颜色）",
      "fmt.cls(" in tpl and "fmt.pct(" in tpl)
# ⚠ 整个 index.html 不能查「有没有 #e5352b」——涨跌停那张 ECharts 图**必须**写死涨跌色
# （ECharts 读不到 CSS 变量，见 README「一之五」）。所以这里查的是**次数**：
# 那两档色各只该出现 1 次（LIM_STYLE 里），热股榜不许再抄一份。
check("涨跌色字面量只在涨跌停图表里各出现 1 次（热股榜没另写一份）",
      tpl.count("#e5352b") == 1 and tpl.count("#12a06a") == 1,
      f"#e5352b×{tpl.count('#e5352b')}  #12a06a×{tpl.count('#12a06a')}")
check("app.css 有 .hot-row 样式", ".hot-row{" in css)
hot_css = css[css.find("热股榜卡片"):]
check("热股榜样式段不写死涨跌色（颜色只在 :root 定义一次）",
      "#e5352b" not in hot_css and "#12a06a" not in hot_css)
check("roll 行用 minmax(0,1fr) 防长名字顶出列", "minmax(0,1fr)" in hot_css)
check("app.py 有 /api/hot 路由", '@app.route("/api/hot")' in app)
check("hot.py 不 import sync（站外接口不写本地同步流水）",
      not re.search(r"import\s+sync\b|import .*\bsync\b", src),
      "sync_log 只记本地库的写入任务")
check("hot.py 不 import tushare（不消耗额度）",
      not re.search(r"(?m)^\s*import tushare|tushare\s+as\s+ts|pro_api", src))

# 行点击 → **K线预览弹窗**（2026-10-11 改）。原来是整行的 `<a href="/stock/<代码>">`
# 新标签打开诊断页；现在整行自己弹预览，诊断页入口只留在弹窗右上角的「在诊断页打开」。
# ⚠ 这张卡没有绑定选股 / 买卖策略 ⇒ 必须带 `sel_only=1`。
check("热股榜整行是可点的 K 线预览入口（div[data-k] + role=button）",
      'class="hot-row" role="button" tabindex="0" data-k=' in tpl
      and "function hotOpen(el){" in tpl,
      "点击走 klinePreview")
check("热股榜不再自己拼 /stock/ 链接（hotUrl 已删、页面里没有 target=_blank）",
      "hotUrl" not in tpl and 'target="_blank"' not in tpl,
      "别把「点行跳诊断页」加回来 —— 诊断页入口在弹窗里")
check("热股榜预览带 sel_only=1（这张卡没绑策略，不摆第 4 张「收益」卡）",
      "params: { sel_only: '1' }" in tpl and "klinePreview({ tsCode: row.dataset.k" in tpl)
check("绑定用事件委托挂在 #hot-grid 上（刷新重画 innerHTML 后仍点得动）",
      "function bindHotKline(){" in tpl
      and "grid.addEventListener('click'" in tpl and "bindHotKline();" in tpl,
      "逐行绑会在 loadHot 重画后全丢")
check("没有 ts_code 时不给 data-k（退化成不可点的行，不弹空代码的图）",
      re.search(r"return it\.ts_code\s*\n?\s*\?", tpl) is not None
      and ': `<div class="hot-row" title=' in tpl)
check("可点行样式只在 app.css 里（.hot-row[data-k] / hover 高亮 / focus 描边）",
      ".hot-row[data-k]{" in css and ".hot-row[data-k]:hover{" in css
      and ".hot-row[data-k]:focus-visible{" in css)

# ---------------------------------------------------------------------------
print("\n### 7. 接口形状（需要服务在 127.0.0.1:8777；不通就 SKIP）")
# ---------------------------------------------------------------------------
BASE = "http://127.0.0.1:8777"
try:
    up = requests.get(BASE + "/api/config", timeout=5).status_code == 200
except Exception:  # noqa: BLE001
    up = False
if not up:
    skip("接口形状", "服务没起（先 $PY run.py）")
else:
    r = requests.get(BASE + "/api/hot", timeout=60)
    j = r.json()
    check("/api/hot 返回 200 且 ok=True", r.status_code == 200 and j.get("ok"))
    d = j.get("data") or {}
    check("data 有 as_of / n / latest_trade_date / sources",
          all(k in d for k in ("as_of", "n", "latest_trade_date", "sources")))
    check("sources 恰好 3 个、key 与 SOURCE_KEYS 一致",
          [s["key"] for s in d.get("sources", [])] == hot.SOURCE_KEYS)
    shape_ok = all(
        s.get("title") and s.get("subtitle") and isinstance(s.get("items"), list)
        and "error" in s for s in d.get("sources", []))
    check("每张卡都有 title/subtitle/items/error", shape_ok)
    n_bad = [s["key"] for s in d.get("sources", [])
             if s["error"] is None and not s["items"]]
    check("取数成功的源必须有条目（成功却空列表就是解析出错）", not n_bad, str(n_bad))
    ordered = all([it["rank"] for it in s["items"]] == sorted(it["rank"] for it in s["items"])
                  for s in d.get("sources", []))
    check("每张卡的行按名次升序", ordered)
    field_ok = all(
        set(it) == {"rank", "ts_code", "name", "pct_chg", "hot", "tag", "note"}
        and len(it["ts_code"]) == 9 and re.match(r"^\d{6}\.(SH|SZ|BJ)$", it["ts_code"])
        for s in d.get("sources", []) for it in s["items"])
    check("条目字段固定，且 ts_code 一律 6 位数字 + 合法市场后缀", field_ok)

    ok_codes = [s["key"] for s in d.get("sources", []) if not s["error"]]
    print(f"      本次取数成功的源：{ok_codes or '（都失败了，可能是外网不通）'}")

    r3 = requests.get(BASE + "/api/hot?n=3", timeout=60).json()
    check("n=3 生效（每张卡不超过 3 条）",
          all(len(s["items"]) <= 3 for s in r3["data"]["sources"]))
    r0 = requests.get(BASE + "/api/hot?n=0", timeout=10).json()
    rx = requests.get(BASE + "/api/hot?n=x", timeout=10).json()
    check("n 越界给 400 不 500", r0.get("ok") is False and rx.get("ok") is False,
          str(r0.get("error")))

# ---------------------------------------------------------------------------
print("\n### 8. 浏览器实测：每一行点开就是 K线预览弹窗（iframe 里是那只票 + sel_only=1）")
# （真渲染 + 真 click 一行 + 读弹窗里的 iframe.src；另外验键盘（回车）也能开。
#   服务不通 / 无 Chrome 就 SKIP）
# ---------------------------------------------------------------------------
if not up:
    skip("浏览器实测", "服务没起（先 $PY run.py）")
elif not CHROME:
    skip("浏览器实测", "本机没找到 Chrome / Edge")
else:
    txt = chrome_probe_out(BASE + "/static/_probe_hot.html", 90000, 300,
                           os.path.join(tempfile.gettempdir(), "_hot_probe_profile"))
    lines = [ln for ln in txt.splitlines() if ln.strip()]
    if not lines or lines[0].startswith("waiting"):
        skip("浏览器实测", "探针没跑出结果（页面 90s 内没渲染完）")
    else:
        n_assert = 0
        for ln in lines:
            if ln.startswith("OK   "):
                check(ln[5:].split("  ")[0], True)
                n_assert += 1
            elif ln.startswith("FAIL "):
                check(ln[5:].split("  ")[0], False, ln.split("  ", 1)[1] if "  " in ln else "")
                n_assert += 1
            elif ln.startswith("NOTE "):
                print("  NOTE " + ln[5:])
        if not n_assert:
            skip("浏览器实测", "探针没给出任何断言结果")
        else:
            print(f"      探针断言 {n_assert} 条")

print("\n" + "=" * 78)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项，跳过 {len(SKIP)} 项")
if SKIP:
    print("跳过：" + " / ".join(SKIP))
print("=" * 78)
sys.exit(1 if FAIL else 0)
