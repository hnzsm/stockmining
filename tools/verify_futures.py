# -*- coding: utf-8 -*-
"""「今日期货」验证：分组口径（主连/同品种）+ 接口形状 + 前端实测。

需求原话：**添加今日期货菜单，展示期货列表，字段1-主连，字段2-涨跌幅，字段3-查看K线。
每个主连可展开查看同品种合约** ⇒ 四段分别对应：菜单与路由、列表三列、K 线与展开。

⚠ 断言纪律（C19）：期货行情是**站外的、盘中一直在跳**，所以
  - 只断言「形状 / 关系 / 降级行为」，不写死条数、不写死某个品种的涨跌幅；
  - 分组口径用**合成数据**喂真函数（`build_groups`），不依赖当时市场里正好有哪些品种；
  - 站外接口在离线环境必然失败 —— 接口段允许「取数失败」，但**结构**必须齐全；
    服务没起 / 无 Chrome 的段落走 skip，不算失败。

跑法（服务要先起着）：
    "C:/Users/ming/WorkBuddy/stockmining/.venv/Scripts/python.exe" tools/verify_futures.py
"""
import ast  # noqa: E402  （剥 docstring 用：只审计代码本体，不审计说明文字）
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import futures as F  # noqa: E402
from core import sync as SY  # noqa: E402  （主连判据的唯一来源在同步侧）

BASE = "http://127.0.0.1:8777"
PROBE = "/static/_probe_futures.html?run=1"
PASS, FAIL, SKIP = [], [], []


def check(name, ok, extra=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'OK  ' if ok else 'FAIL'} {name}{('  ' + extra) if extra else ''}")


def skip(name, why=""):
    SKIP.append(name)
    print(f"  SKIP {name}{('  ' + why) if why else ''}")


def read(p):
    with open(os.path.join(ROOT, p), encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# 无头 Chrome（前端断言只走这一条路，别用 agent-browser —— 见 C17）
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
    """渲染探针页并返回 `<pre id="out">` 里的文本。"""
    dst = os.path.join(tempfile.gettempdir(), "_fut_probe_dom.html")
    try:
        with open(dst, "w", encoding="utf-8") as fh:
            subprocess.run(
                [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
                 "--user-data-dir=" + profile, "--window-size=1560,1200",
                 "--virtual-time-budget=%d" % budget_ms, "--dump-dom", url],
                stdout=fh, stderr=subprocess.DEVNULL, timeout=timeout)
        dom = open(dst, encoding="utf-8", errors="ignore").read()
    finally:
        try:
            os.remove(dst)
        except OSError:
            pass
    m = re.search(r'<pre id="out">([\s\S]*?)</pre>', dom)
    return m.group(1) if m else ""


def http(path, timeout=30):
    """同源请求（绕开本机代理）。返回 (status, 解析后的 json 或原文)。"""
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with op.open(BASE + path, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            try:
                return r.status, __import__("json").loads(body)
            except Exception:  # noqa: BLE001
                return r.status, body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, __import__("json").loads(body)
        except Exception:  # noqa: BLE001
            return e.code, body
    except Exception:  # noqa: BLE001
        return 0, None


print("=" * 78)
print("今日期货（core/futures.py + /futures）验证")
print("=" * 78)

# ---------------------------------------------------------------------------
print("\n[1] 静态口径：数据源（本地库）/ 交易所清单 / 主连判据 / 涨跌幅口径")
src = read("core/futures.py")
sy = read("core/sync.py")
dbpy = read("core/db.py")

# 2026-09-27 换源：东财 push2.eastmoney.com **服务端持续断连**（curl 复现 (56) server
# closed abruptly，同家 push2his 正常）⇒ 六个市场全空。改走 tushare 落库后页面不出网。
# ⚠ 模块 docstring 里正写着「曾经走东财」这段换源历史，拿整份源码搜域名必然误命中
#   （自己把自己绊倒，C19）—— 判据只看**代码本体**（docstring 之后的那一段）。
def strip_docstrings(code):
    """把模块/类/函数的 docstring 整段抹掉，只留**代码本体**。

    ⚠ 别用「按三引号 split 取最后一段」那种写法：那只留文件末尾一截，import 全在前面，
    判据直接假红；而且 r 前缀的 docstring 会让段数奇偶对不上。用 ast 定位行号最稳。
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code
    drop = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            drop.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return "\n".join(l for i, l in enumerate(code.splitlines(), 1) if i not in drop)


code_only = strip_docstrings(src)
check("数据源已换成本地库（代码里不再有东财域名 / requests）",
      "eastmoney" not in code_only and "requests" not in code_only
      and "from core import db" in code_only and "futures_daily" in code_only)
check("主连判据只有一处：写库时由 sync.fut_is_main 算，读取层不重复判",
      "def fut_is_main(" in sy and "def is_main(" not in src)
check("交易所清单 6 家（SHFE/DCE/CZCE/CFFEX/INE/GFEX）",
      [e["code"] for e in F.EXCHANGES] == ["SHFE", "DCE", "CZCE", "CFFEX", "INE", "GFEX"],
      ",".join(e["code"] for e in F.EXCHANGES))
check("涨跌幅口径唯一：收盘价 vs **昨结算**（期货的规矩），不是股票的 vs 昨收",
      src.count("def pct_of(") == 1 and "change1" in src and "pre_settle" in src)
check("同品种合约 = 未退市 + symbol 带月份数字（「连续」序列不可交易，不摆进展开里）",
      "_MONTH_RE" in src and "delist_date" in src)
check("主连排序取的是分组的 main（取分组字段会恒为 None → 退化成按交易所拼接）",
      re.search(r'def _sort_key_pct\(g: dict\):[\s\S]{0,300}?g\["main"\]\.get\("pct_chg"\)',
                src) is not None)
# 默认排序 = 「最近期权到期」升序（主连行那个字段）。⚠ 不能只看 main：主连（`RU.SHF`）
# 不是可交易合约、`opts` 里没有它的 key ⇒ 那样 opt_near 恒为 None、整列变「—」且排序退化。
check("默认排序按「最近期权到期」升序，且从**品种内全部合约**聚合（不是只看主连那行）",
      "def _sort_key_opt(" in src and "groups.sort(key=_sort_key_opt)" in src
      and re.search(r'for c in mates:\n\s+for m in c\.get\("opt_maturities"\)', src) is not None
      and "str(m) >= day" in src)
check("K 线读本地库，周/月由日线**即时聚合**（不落库）",
      "FROM futures_daily" in src and "_agg(" in src
      and "INSERT" not in src.split("def kline")[1][:1500])
# ⚠ 实测踩过：`key_of = lambda s: A if freq=='week' else (lambda s: s[:7])` 的 else 分支
#   返回的是**lambda 对象**，每次新建 ⇒ key 两两不等 ⇒ 月线永不聚合（周线却是对的，极难发现）。
check("周/月聚合的 key 用显式分支（别写「lambda 里嵌三元」—— 会返回 lambda 对象）",
      "def key_of(s):" in src and "if freq == \"week\":" in src)
check("同步：主连全历史 + 具体合约近 1 年（两种粒度、两种拉法）",
      "FUT_CONTRACT_DAYS = 365" in sy and "def sync_fut_daily(" in sy
      and "def sync_fut_basic(" in sy)
check("同步增量判据：具体合约看「is_main=0 有没有这天」（只看日期会被主连全历史骗过去）",
      "b.is_main = 0" in sy)
check("建表：futures_basic（含 is_main/fut_code）与 futures_daily（含结算价/持仓量）",
      "CREATE TABLE IF NOT EXISTS futures_basic" in dbpy
      and "CREATE TABLE IF NOT EXISTS futures_daily" in dbpy
      and "pre_settle" in dbpy and "oi_chg" in dbpy)
# 「对应期权到期日」那张表：只为给展开行补一列，所以要能按标的期货合约直接查。
check("建表：futures_option（有 underlying 列 + (underlying, delist_date) 索引）",
      "CREATE TABLE IF NOT EXISTS futures_option" in dbpy
      and "underlying" in dbpy and "idx_underlying" in dbpy)
check("期权同步：翻页拉全（opt_basic 单页硬顶 12000，给 40000 也只回 12000）",
      "OPT_LIMIT = 12000" in sy and "offset=p * OPT_LIMIT" in sy
      and "def sync_fut_option(" in sy)
check("期权同步只留**在市**（delist_date < 今天 的丢掉）+ 读取端还要再过滤一遍（表里有历史残留）",
      "if dl_s and dl_s < today_y:" in sy
      and "WHERE delist_date >= %s" in src)
check("建表/读取同一条链：期货同步顺带同步期权（一个按钮管全）",
      "n_opt = sync_fut_option(jid" in sy
      and 'return {"basic": n_basic, **daily, "options": n_opt}' in sy)
check("页面/接口写明「截至上一交易日」（期货日线**收盘结算后**才更新，别让人当实时价）",
      "trade_date" in src and "上一交易日" in read("templates/futures.html")
      and "收盘结算后" in read("templates/futures.html"))

# ---------------------------------------------------------------------------
print("\n[2] 静态口径：路由 / 菜单 / 三列 / 前端实现")
# ---------------------------------------------------------------------------
app_py = read("app.py")
for route in ('@app.route("/futures")', '@app.route("/futures/kline")',
              '@app.route("/api/futures")', '@app.route("/api/futures/kline")'):
    check("路由 %s 存在" % route, route in app_py)
check("ts_code 先卡形状再拼进 SQL（现在是 RU.SHF 这种，不是东财的 113.rum）",
      "_TSCODE_RE" in app_py
      and re.search(r'_TSCODE_RE = re\.compile\(r"\^\[A-Za-z0-9\]\{1,12\}', app_py) is not None)

base = read("templates/base.html")
check("侧栏菜单加了「今日期货」且页内高亮用 page=='futures'",
      'href="/futures"' in base and "page=='futures'" in base and "今日期货" in base)

page = read("templates/futures.html")
check("页面五列 = 主连 / 涨跌幅 / 最近期权到期 / 最近合约交割 / 查看K线（表头本身由 JS 出，这里锁页面文案）",
      all(k in page for k in ("主连", "涨跌幅", "最近期权到期", "最近合约交割", "查看K线"))
      and "%}" in page)
check("页面写明默认排序 = 按「最近期权到期」从小到大，并说明没期权的垫底",
      "默认按这一列从小到大" in page and "垫底" in page)
check("页面说明「最近合约交割」列的含义（= 展开行第一行那个合约、距今天）与可排序",
      "最后交易日最早" in page and "展开行第一行" in page and "今天交割" in page
      and "这一列同样可以点表头排序" in page)
check("页面挂了 #ft-list 与 futures.js", 'id="ft-list"' in page and "js/futures.js" in page)
check("页面文案写明：tushare 落库 + 是「上一交易日」收盘（不是盘中实时）",
      "tushare" in page and "本地库" in page and "上一交易日" in page)
check("数据页能一键同步期货（按钮 + 路由 + 命令行 --only-futures 三处都有）",
      '@app.route("/api/sync/futures"' in app_py
      and 'id="btn-futures"' in read("templates/data.html")
      and "--only-futures" in read("sync_data.py"))

js = read("static/js/futures.js")
check("K 线弹窗只有一个入口 showKline", js.count("function showKline") == 1
      and js.count("showKline(") == 2)          # 定义 1 + 调用 1
check("弹窗用公共 mkModal，嵌的是同源 iframe /futures/kline?embed=1",
      "window.mkModal(" in js and "'/futures/kline' + q + '&embed=1'" in js)
check("前端**不自己画 K 线**（否则就养出第二份 K 线口径）",
      "echarts.init" not in js and "setOption" not in js)
check("表头五列在 JS 里出（唯一来源，页面不写死一份）",
      "'<th class=\"ft-th-name\">主连</th>'" in js
      and '>涨跌幅<span class="sarrow"></span></th>' in js
      and 'id="ft-th-opt">最近期权到期' in js
      and 'id="ft-th-dl">最近合约交割' in js
      and "'<th class=\"ft-th-k\">查看K线</th>'" in js)
# 主连行从 4 列变 5 列后，展开行（`tr.ft-sub` 那个通栏 td）的 colspan 必须同步 ——
# 漏改不会报错，只是展开行右侧露一截白（不好看但无声）。
check("展开行的 colspan 跟着主连列数改成 5（漏改会右侧露白，且不报错）",
      'colspan="5"' in js and 'colspan="4"' not in js)
# 「一个品种一个 tbody」：模板里这段只写 1 次，但每组都套一次（gs.map(groupHtml)），
# 展开行跟主连行天然绑在一起 ⇒ 排序/筛选时整块移动，不会错位。
check("展开用的是「一个品种一个 tbody」方案（组内配对一条 tr.ft-sub）",
      js.count('<tbody class="ft-grp"') == 1 and "gs.map(groupHtml)" in js
      and '\'<tr class="ft-sub"\'' in js and "state.open" in js)
check("点击走事件委托（列表是整块重建的，逐行绑会漏）",
      "list.addEventListener('click'" in js and "closest('button[data-k]')" in js)
# 实锤过的静默坑：分组 tbody 与 K 线按钮**都叫 data-k** 时，`closest('[data-k]')`
# 从行内往上找会先撞上 tbody，于是「点行名展开」变成拿 `142|20号胶` 当 secid 去开 K 线弹窗。
check("分组 key 与按钮 key 不同名（分组用 data-grp，data-k 只留给按钮）",
      'data-grp="' in js and "getAttribute('data-grp')" in js
      and 'closest(\'[data-k]\')' not in js and js.count('data-k="') == 2)
check("涨跌色只引用全局 fmt.cls（不新造颜色口径）",
      "fmt.cls(" in js and "#e5352b" not in js)
# 展开表 8 列（需求：展开的合约加「期权到期日」）——表头 colspan 与列数必须同一处改，
# 漏改 colspan 会让空态那行只跨 6 格、右侧露白（不报错，只是难看）。
check("展开表 8 列，含「到期日」与「期权到期日」，且空态 colspan 同步",
      js.count('>到期日</th>') == 1 and js.count('>期权到期日</th>') == 1
      and 'colspan="8"' in js and 'colspan="6"' not in js)
check("两个到期日各有独立渲染函数（期货自己的 / 对应期权的），不混用一格",
      "function futDateCell(" in js and "function optDateCell(" in js
      and js.count("function daysLeft(") == 1)
check("期权到期日支持多个（连续月份期权）⇒ 主显示最早 + 「+N」",
      "c.opt_maturities || []" in js and "mats[0]" in js and "mats.length - 1" in js)
# 需求（2026-09-28）：展开行默认按**合约到期日近→远**（原来是按持仓量降序）。
# 文案是口径的一部分 —— 排序改了文案不改，页面就在撒谎。
check("展开行标题写明「按到期日」排（不再写按持仓量），页面说明同步",
      "按到期日近→远" in js and "按持仓量排序" not in js
      and "按到期日从近到远" in read("templates/futures.html"))

# ---- 主连行的「最近期权到期」「最近合约交割」两列 + 排序 ----
check("主连行有「最近期权到期」列，天数直接用后端的 opt_near_days（前端不重算，免得差一天）",
      "function optNearCell(" in js and "g.opt_near_days" in js
      and js.count("optNearCell(") == 2)          # 定义 1 + 调用 1
check("主连行有「最近合约交割」列，天数同样直接用后端的 fut_near_days（与 opt_near_* 是两个概念）",
      "function futNearCell(" in js and "g.fut_near_days" in js
      and js.count("futNearCell(") == 2           # 定义 1 + 调用 1
      and "fut_near_days" in read("core/futures.py"))
check("两列天数展示口径：正数「N 天」/ 0「今天交割」/ 负数「已交割」（不显示 -N 天）",
      "d > 0 ? d + ' 天'" in js and "'今天交割'" in js and "'已交割'" in js)
check("排序支持三列：默认 opt 升序，点表头切换字段 / 再点反向（状态 = state.sortKey + desc）",
      "sortKey: 'opt'" in js and "desc: false" in js
      and "function toggleSort(" in js and "toggleSort('opt')" in js
      and "toggleSort('dl')" in js and "toggleSort('pct')" in js
      and "closest('#ft-th-opt')" in js and "closest('#ft-th-dl')" in js)
check("换列时的默认方向：只有涨跌幅从大到小，两列「还剩几日」都从小到大",
      "state.desc = key === 'pct';" in js)
check("两列「还剩几日」共用一套比较函数 cmpDays（口径唯一，不给第二列再抄一份）",
      "function cmpDays(" in js and "cmpDays(a, b, 'opt_near_days', 'opt_near_days', state.desc)" in js
      and "cmpDays(a, b, 'fut_near_days', 'fut_near_days', state.desc)" in js
      and js.count("if (xn) return 1;") == 1)
check("排序里「没有的垫底」（不是当成 0 天排最前，哪种方向都垫底）—— opt / dl 两列都适用",
      "const xn = (x === null || x === undefined);" in js
      and "if (xn) return 1;" in js and "if (yn) return -1;" in js)
check("三列箭头只亮当前那列（paintSortHead 遍历 SORT_COLS 全量重画）",
      "SORT_COLS" in js and "const on = state.sortKey === c.key;" in js
      and js.count("{ id: 'ft-th-") == 3)

kpage = read("templates/futures_kline.html")
check("K 线页复用 app.js 的图表基础（baseAxis/withBaseAxis/tooltipBase/CHART_LEGEND）",
      all(k in kpage for k in ("window.baseAxis", "window.withBaseAxis",
                               "window.tooltipBase", "window.CHART_LEGEND")))
check("K 线页涨跌色写死为中国习惯的 涨红跌绿",
      "const UP = '#e5352b', DOWN = '#12a06a';" in kpage)
check("多 grid 的 dataZoom/axisPointer 收的是**下标数组**（不是单个数字）",
      "const allX = [0, 1];" in kpage and "xAxisIndex: allX" in kpage
      and "link: [{ xAxisIndex: allX }]" in kpage)
check("K 线页只有一份 echarts.init", kpage.count("echarts.init") == 1)

# 内联脚本语法（模板里的 JS 写错整页都不跑）
node = r"C:\Users\ming\.workbuddy\binaries\node\versions\22.22.2-3\node.exe"
if not os.path.exists(node):
    node = "node"
blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)</script>", kpage)
js_txt = "\n;\n".join(re.sub(r"\{\{.*?\}\}", "0", b, flags=re.S) for b in blocks)
tmpf = os.path.join(tempfile.gettempdir(), "_fut_kline_inline.js")
with open(tmpf, "w", encoding="utf-8") as fh:
    fh.write(js_txt)
r = subprocess.run([node, "--check", tmpf], capture_output=True, text=True)
check("K 线页内联脚本语法通过（Jinja 先中和）", r.returncode == 0, (r.stderr or "")[:200])
r = subprocess.run([node, "--check", os.path.join(ROOT, "static", "js", "futures.js")],
                   capture_output=True, text=True)
check("futures.js 语法通过", r.returncode == 0, (r.stderr or "")[:200])

# ---------------------------------------------------------------------------
print("\n[3] 分组口径（喂合成数据给真函数 —— 不依赖当时市场里有哪些品种）")
# ---------------------------------------------------------------------------
def _r(code, sym, ex, fut, is_main, name, oi=0, close=100.0, pre_settle=100.0, delist=None):
    return {"ts_code": code, "symbol": sym, "exchange": ex, "name": name, "fut_code": fut,
            "is_main": is_main, "delist_date": delist,
            "open": 99.0, "high": 101.0, "low": 98.0, "close": close, "settle": close,
            "pre_close": pre_settle, "pre_settle": pre_settle,
            "change1": close - pre_settle, "change2": 0.0,
            "vol": 100.0, "amount": 1000.0, "oi": oi, "oi_chg": 1.0}


rows = [
    _r("RU.SHF", "RU", "SHFE", "RU", 1, "橡胶主力", oi=900),
    # ⚠ 下面三个合约**故意让「到期日序」与「持仓量序」相反**（2611 到期最早但持仓最小、
    #   2612 到期最晚持仓最大、2701 干脆没接过到期日）—— 这样「组内按什么排」才有鉴别力：
    #   若哪天又改回按持仓量降序，断言会当场红。
    _r("RU2611.SHF", "RU2611", "SHFE", "RU", 0, "橡胶2611", oi=700, delist="2026-11-16"),
    _r("RU2612.SHF", "RU2612", "SHFE", "RU", 0, "橡胶2612", oi=800, delist="2026-12-15"),
    _r("RU2701.SHF", "RU2701", "SHFE", "RU", 0, "橡胶2701", oi=650),     # 没到期日 ⇒ 垫底
    _r("RUL.SHF", "RUL", "SHFE", "RU", 0, "橡胶连续", oi=600),          # 连续序列：不可交易
    _r("RU2501.SHF", "RU2501", "SHFE", "RU", 0, "橡胶2501", oi=500,
       delist="2025-01-01"),                                            # 已退市
    _r("RU.DCE", "RU", "DCE", "RU", 1, "某品种主力", oi=400),           # 同名、不同交易所
    _r("A.DCE", "A", "DCE", "A", 1, "豆一主力", oi=300),
    # 只有**已过期**的期权（下面 opts 里给 2026-09-01，早于 today=2026-09-24）⇒
    # 用来验「过期的期权不算最快到期」，别把历史当成最紧急
    _r("A2611.DCE", "A2611", "DCE", "A", 0, "豆一2611", oi=200, delist="2026-11-17"),
]

# 「对应期权到期日」：`option_map()` 的形状 = {标的期货 ts_code: [到期日…]}（升序）。
# 这里手工喂一份，验的是**拼接口径**（真库那份由 [4] 段验）。
opts = {
    "RU2611.SHF": ["2026-10-26"],
    # ⚠ 故意让**展开里排第二**的合约（RU2612，持仓量更小）期权到期更早：
    #   这样「最近期权到期」若写成「取第一个合约的」就会错 —— 必须是全品种取 min。
    "RU2612.SHF": ["2026-09-30", "2026-12-16"],   # 连续月份期权：两个到期日
    # ⚠ 真实库里 key 全是**具体合约**（`opt_code` 去 OP 前缀就是 `RU2611.SHF`），
    #   主连 `RU.SHF` 永远查不到 —— 主连不是可交易合约，没有对应的期权。
    "A2611.DCE": ["2026-09-01"],                  # 已过期（相对 today）
}
TODAY = "2026-09-24"                              # 固定基准日：天数断言不许随运行日期飘
gs = F.build_groups(rows, "2026-09-24", opts, TODAY)
by = {(g["market_code"], g["fut_code"]): g for g in gs}
check("分组数 = 主连条数（连续 / 退市 / 月份合约都不占顶层）", len(gs) == 3, str(sorted(by)))
g1 = by.get(("SHFE", "RU")) or {}
codes = [c["code"] for c in g1.get("contracts", [])]
check("展开里只有未退市的可交易合约（连续 RUL、退市 RU2501 都不在）",
      codes == ["RU2611.SHF", "RU2612.SHF", "RU2701.SHF"], str(codes))
# 需求（2026-09-28）：展开行改按**合约到期日近→远**。合成数据里「到期日序」与「持仓量序」
# 是**相反**的（2612 持仓最大但到期最晚、2701 没到期日）⇒ 排错方向必红；顺带把
# 「缺到期日垫底」「同一天到期再看持仓量」两个分支也一起锁住。
oi_order = [c["code"] for c in sorted(g1.get("contracts", []), key=F._sort_key_oi)]
check("组内按**到期日近→远**排序（不是按持仓量 —— 这里两者故意相反）",
      codes == ["RU2611.SHF", "RU2612.SHF", "RU2701.SHF"]
      and oi_order == ["RU2612.SHF", "RU2611.SHF", "RU2701.SHF"],
      "到期日序 %s / 若按持仓量则是 %s" % (codes, oi_order))
check("缺到期日的合约垫底（不是当「0 天后到期」排最前）",
      codes[-1] == "RU2701.SHF" and g1["contracts"][-1].get("delist_date") is None,
      str([(c["code"], c.get("delist_date")) for c in g1["contracts"]]))
check("同一天到期时按持仓量降序（次级 key：`_sort_key_oi`）",
      [c["code"] for c in sorted(
          [dict(x, delist_date="2026-11-16") for x in g1["contracts"] if x.get("delist_date")],
          key=F._sort_key_delist)] == ["RU2612.SHF", "RU2611.SHF"],
      "同到期日 → 持仓量 800 的 RU2612 排在 700 的 RU2611 前面")
check("n_contracts 含主连自己", g1.get("n_contracts") == 4, str(g1.get("n_contracts")))
check("同名品种不串组（SHFE/RU 与 DCE/RU 各归各）",
      ("SHFE", "RU") in by and ("DCE", "RU") in by
      and by[("DCE", "RU")]["main"]["code"] == "RU.DCE")
check("组名去后缀、交易所带中文名",
      g1.get("product") == "橡胶" and g1.get("market") == "上期所",
      "%s / %s" % (g1.get("product"), g1.get("market")))

# ---- 两个「到期日」（需求：展开的合约加「期权到期日」）----
c11 = next((c for c in g1["contracts"] if c["code"] == "RU2611.SHF"), {})
c12 = next((c for c in g1["contracts"] if c["code"] == "RU2612.SHF"), {})
check("展开行带出**期货合约自己的**到期日（basic.delist_date，最后交易日）",
      c11.get("delist_date") == "2026-11-16" and c12.get("delist_date") == "2026-12-15",
      "%s / %s" % (c11.get("delist_date"), c12.get("delist_date")))
check("期权到期日按标的期货 ts_code 对上；多个到期日取**最早**那个、全部留在 opt_maturities",
      c11.get("opt_maturity") == "2026-10-26" and c11.get("opt_maturities") == ["2026-10-26"]
      and c12.get("opt_maturity") == "2026-09-30"
      and c12.get("opt_maturities") == ["2026-09-30", "2026-12-16"],
      "%s / %s" % (c11.get("opt_maturity"), c12.get("opt_maturities")))
check("主连行也有这两个字段（形状一致），但恒为空 —— 主连不是可交易合约，没有到期日",
      g1["main"].get("delist_date") is None and g1["main"].get("opt_maturity") is None
      and "opt_maturities" in g1["main"],
      "%r / %r" % (g1["main"].get("delist_date"), g1["main"].get("opt_maturity")))

# ---- 主连行的「最近期权到期」（需求：展示对应期权最快到期的合约还有几日）----
check("主连行聚合出「最近期权到期」= 品种内**最早**那个（不是第一个合约的、更不是主连自己的）",
      g1.get("opt_near_date") == "2026-09-30" and g1.get("opt_near_days") == 6
      and g1.get("opt_near_code") == "RU2612.SHF",
      "%s / %s 天 / 来自 %s" % (g1.get("opt_near_date"), g1.get("opt_near_days"),
                                g1.get("opt_near_code")))
check("已过期的期权不算「最快到期」（A 组只剩一个已过期的 ⇒ None，不是负数）",
      by[("DCE", "A")].get("opt_near_days") is None
      and by[("DCE", "A")].get("opt_near_date") is None
      and by[("DCE", "A")]["contracts"][0].get("opt_maturity") == "2026-09-01")
check("没有对应期权的品种：分组字段是 None（页面显「—」、排序垫底），不 KeyError",
      by[("DCE", "RU")].get("opt_near_days") is None
      and by[("DCE", "RU")].get("opt_near_code") is None
      and "opt_near_days" in by[("DCE", "RU")])
# 换一个基准日再算一次：这样既验「天数是自然日差」，又验「过期过滤会跟着基准日走」
_g_later = [g for g in F.build_groups(rows, "2026-09-24", opts, "2026-10-01")
            if (g["market_code"], g["fut_code"]) == ("SHFE", "RU")][0]
check("天数按**自然日**算、基准是传进来的 today（换基准日会连带重新过滤过期期权）",
      _g_later.get("opt_near_date") == "2026-10-26" and _g_later.get("opt_near_days") == 25
      and _g_later.get("opt_near_code") == "RU2611.SHF",
      "today=2026-10-01 → %s / %s 天 / 来自 %s（RU2612 的 09-30 这时已过期，被排除）"
      % (_g_later.get("opt_near_date"), _g_later.get("opt_near_days"),
         _g_later.get("opt_near_code")))

# ---- 主连行的「最近合约交割」（需求：最近合约还剩几日 + 可点表头排序）----
# ⚠ 与上面的 opt_near_* 是**两个概念**：一个指期货合约的交割日（futures_basic.delist_date）、
#   一个指期权到期日。合成数据里 RU2611 到期最早（2026-11-16）但**它的期权不是最早到期**
#   （RU2612 的 09-30 更早）⇒ 两列指向不同合约，正好把「别混用」钉死。
check("主连行聚合出「最近合约交割」= 品种内**最早交割**的那个（不是主连自己的）",
      g1.get("fut_near_date") == "2026-11-16" and g1.get("fut_near_days") == 53
      and g1.get("fut_near_code") == "RU2611.SHF" and g1.get("fut_near_name") == "橡胶2611",
      "%s / %s 天 / 来自 %s" % (g1.get("fut_near_date"), g1.get("fut_near_days"),
                                g1.get("fut_near_code")))
check("两列不是一回事：「最近期权到期」指向 RU2612（期权 09-30 到期），「最近合约交割」指向 RU2611",
      g1.get("opt_near_code") == "RU2612.SHF" and g1.get("fut_near_code") == "RU2611.SHF",
      "opt=%s / dl=%s" % (g1.get("opt_near_code"), g1.get("fut_near_code")))
check("「最近合约交割」就是**展开行第一行**那个合约（mates 已按到期日近→远排，两处不许分叉）",
      g1["contracts"][0]["code"] == g1.get("fut_near_code")
      and [c["code"] for c in g1["contracts"] if c.get("delist_date")][0] == g1.get("fut_near_code"),
      "展开首行 %s / 分组字段 %s" % (g1["contracts"][0]["code"], g1.get("fut_near_code")))
check("只有一个合约的品种也聚合得出（DCE/A：A2611，2026-11-17，54 天）",
      by[("DCE", "A")].get("fut_near_date") == "2026-11-17"
      and by[("DCE", "A")].get("fut_near_days") == 54
      and by[("DCE", "A")].get("fut_near_code") == "A2611.DCE",
      "%s / %s 天" % (by[("DCE", "A")].get("fut_near_date"), by[("DCE", "A")].get("fut_near_days")))
check("没有可交易合约的品种：分组字段是 None（页面显「—」、排序垫底），不 KeyError",
      by[("DCE", "RU")].get("fut_near_days") is None
      and by[("DCE", "RU")].get("fut_near_code") is None
      and "fut_near_days" in by[("DCE", "RU")])
check("天数按**自然日**算、基准是传进来的 today（与 opt_near_days 同一个 day）",
      [g for g in F.build_groups(rows, "2026-09-24", opts, "2026-10-16")
       if (g["market_code"], g["fut_code"]) == ("SHFE", "RU")][0].get("fut_near_days") == 31,
      "today=2026-10-16 → 2026-11-16 是 31 天")
# ⚠ 「刚交割完」的合约**仍在 mates 里**（它在最后一个行情日还活着）⇒ days 为负、不跳过。
#   跳过它去「只算未交割的」会让主连格与展开行第一行分叉；真库 2026-10-15（上期所集中
#   交割日）实测 21 个品种走这一支，页面显示「已交割」。
rows_neg = [
    _r("B.DCE", "B", "DCE", "B", 1, "豆二主力", oi=100),
    _r("B2610.DCE", "B2610", "DCE", "B", 0, "豆二2610", oi=90,
       delist="2026-09-22"),                       # 早于 today、晚于行情日 ⇒ 负数
]
_g_neg = F.build_groups(rows_neg, "2026-09-20", {}, "2026-09-24")[0]
check("行情日之后、今天之前交割的合约：算负数、**不跳过**（页面显示「已交割」，不是「—」）",
      _g_neg.get("fut_near_days") == -2 and _g_neg.get("fut_near_date") == "2026-09-22"
      and _g_neg["contracts"][0]["code"] == "B2610.DCE",
      "%s / %s 天" % (_g_neg.get("fut_near_date"), _g_neg.get("fut_near_days")))
_ou = [("OPPG2709.DCE", "", "PG2709.DCE"), ("OPAU2708.SHF", "", "AU2708.SHF"),
       ("OPSA2707.ZCE", "", "SA2707.ZCE"), ("OP000300.SH", "", "000300.SH"),
       ("", "AU2708P728.SHF", "AU2708.SHF"), ("", "垃圾", ""), ("", "", "")]
bad = ["%r/%r->%r(要 %r)" % (a, b, SY.opt_underlying(a, b), w)
       for a, b, w in _ou if SY.opt_underlying(a, b) != w]
check("期权→标的映射唯一来源 opt_underlying（去 OP 前缀；缺 opt_code 才从 ts_code 猜）",
      not bad, "; ".join(bad[:3]))

# 主连判据（sync 侧，唯一来源）：symbol == fut_code，中金所 30 年国债是 TL0 特例
MAIN = [("RU", "RU", "SHFE", True), ("RUL", "RU", "SHFE", False), ("RU2511", "RU", "SHFE", False),
        ("TL0", "TL", "CFFEX", True), ("TLL", "TL", "CFFEX", False), ("TS", "TS", "CFFEX", True),
        ("TSL", "TS", "CFFEX", False), ("A", "A", "DCE", True), ("", "RU", "SHFE", False),
        (None, "RU", "SHFE", False)]
bad = ["%s/%s/%s->%s" % (s, f, e, SY.fut_is_main(s, f, e))
       for s, f, e, want in MAIN if SY.fut_is_main(s, f, e) is not want]
check("主连判据 %d 例全对（连续与具体合约都不算主连）" % len(MAIN), not bad, "; ".join(bad[:3]))

PN = [("橡胶主力", "橡胶"), ("沪深300期货", "沪深300"), ("白银主力", "白银"), ("", "")]
bad = ["%r->%r" % (a, F.product_name(a)) for a, b in PN if F.product_name(a) != b]
check("品种名 = 主连名去掉「主力 / 期货」后缀", not bad, "; ".join(bad[:3]))

PCT = [({"close": 110.0, "pre_settle": 100.0, "pre_close": 105.0}, 10.0),   # 用昨结算
       ({"close": 110.0, "pre_close": 100.0}, 10.0),                        # 缺昨结算→退回昨收
       ({"close": None, "pre_settle": 100.0}, None)]
bad = ["%s->%r" % (a, F.pct_of(a)) for a, b in PCT if F.pct_of(a) != b]
check("涨跌幅 = 收盘 vs 昨结算（缺昨结算才退回 vs 昨收）", not bad, "; ".join(bad[:3]))

# 排序 key：入参是**分组**，必须取到 main 的涨跌幅（取错会静默退化成按市场原序）
k_hi = F._sort_key_pct({"main": {"pct_chg": 2.0}, "product": "x"})
k_lo = F._sort_key_pct({"main": {"pct_chg": -1.0}, "product": "y"})
k_none = F._sort_key_pct({"main": {"pct_chg": None}, "product": "z"})
check("排序 key 取的是 main.pct_chg 且没值垫底", k_hi < k_lo < k_none,
      "%.1f < %.1f < %.1f" % (k_hi, k_lo, k_none))

# 默认排序 key：天数小的在前、**没有期权的垫底**（哪怕它涨停也不许排最前 —— 那是「没信息」，
# 不是「0 天后到期」）；并列时退回按主连涨跌幅降序，与前端 compare() 同一口径。
s_a = F._sort_key_opt({"opt_near_days": 3, "main": {"pct_chg": 1.0}})
s_b = F._sort_key_opt({"opt_near_days": 9, "main": {"pct_chg": 1.0}})
s_n = F._sort_key_opt({"opt_near_days": None, "main": {"pct_chg": 9.9}})
s_t = F._sort_key_opt({"opt_near_days": 3, "main": {"pct_chg": -2.0}})
check("默认排序 key：天数小的在前、没有期权的垫底（涨停也排不到最前）",
      s_a < s_b < s_n and s_a < s_t,
      "%s < %s < %s" % (s_a, s_b, s_n))

# ---------------------------------------------------------------------------
print("\n[4] 真接口（服务没起就跳过）")
# ---------------------------------------------------------------------------
st, _ = http("/api/health", timeout=8)
if st != 200:
    for nm in ("/api/futures 形状", "/api/futures 分组自洽", "/api/futures/kline 形状",
               "周/月线是真聚合（不是日线原样）", "K 线错误分支当场 400"):
        skip(nm, "服务没起（先 python run.py）")
else:
    st, d = http("/api/futures")
    dd = (d or {}).get("data") or {}
    ok_shape = (st == 200 and (d or {}).get("ok") and isinstance(dd.get("groups"), list)
                and isinstance(dd.get("markets"), list) and isinstance(dd.get("stats"), dict)
                and isinstance(dd.get("trade_date"), str))
    if not ok_shape:
        check("/api/futures 形状", False, str(d)[:160])
    else:
        check("/api/futures 形状（groups/markets/stats/trade_date 齐全，每家市场都带 error）",
              all("error" in m for m in dd["markets"])
              and all(("main" in g and "contracts" in g) for g in dd["groups"]),
              "%d 组 / %d 家市场（截至 %s）" % (len(dd["groups"]), len(dd["markets"]),
                                       dd["trade_date"]))
        check("行情**来自本地库**（source 里写明 tushare，不再有站外字样）",
              "tushare" in (dd.get("source") or "") and "东方财富" not in (dd.get("source") or ""),
              str(dd.get("source")))
        # ⚠ 库里没同步过时也必须**返回空列表而不是崩** —— 页面照渲染，只提示去同步
        check("库里没数据时降级为空列表 + 人话说明（不抛异常）",
              all(("error" in m) for m in dd["markets"])
              and (dd["groups"] or all(m["error"] for m in dd["markets"])))
        if dd["groups"]:
            g0 = dd["groups"][0]
            bad_ex = [c["code"] for c in g0["contracts"]
                      if c["code"].split(".")[-1] != g0["main"]["code"].split(".")[-1]]
            check("每个 group 的 contracts 与主连**同交易所**且带月份数字（连续序列不算）",
                  not bad_ex and all(re.search(r"\d{3,4}$", c["code"].split(".")[0] or "")
                                     for c in g0["contracts"]),
                  g0["main"]["code"] + " + " + str(len(g0["contracts"])) + " 个")
            check("五列要用的字段都在（name / pct_chg / secid / opt_near_days / fut_near_days），secid 是 ts_code 形状",
                  g0["main"].get("name") and g0["main"].get("pct_chg") is not None
                  and re.match(r"^[A-Za-z0-9]{1,12}\.[A-Za-z]{3,5}$", g0["main"]["secid"] or "")
                  and "opt_near_days" in g0 and "fut_near_days" in g0,
                  str(g0["main"]["secid"]) + " / near=" + str(g0.get("opt_near_days"))
                  + " / dl=" + str(g0.get("fut_near_days")))
            # 两个到期日在**真数据**里的形状：期货到期日必须每行都有（basic 里 99% 有值）；
            # 期权到期日允许没有（中金所股指期权标的是指数 / 该月还没挂期权）但必须**有键值**。
            _allc = [c for g in dd["groups"] for c in g["contracts"]]
            _re_d = re.compile(r"^\d{4}-\d{2}-\d{2}$")
            check("真数据：每个展开合约都有「期货到期日」，且是 YYYY-MM-DD 或 None",
                  all(("delist_date" in c)
                      and (c["delist_date"] is None or _re_d.match(c["delist_date"]))
                      for c in _allc),
                  "共 %d 个合约" % len(_allc))
            _withopt = [c for c in _allc if c.get("opt_maturity")]
            check("真数据：期权到期日能对上（字段在 + 至少一部分合约有值 + 取值形状对）",
                  all("opt_maturities" in c for c in _allc)
                  and len(_withopt) > 0
                  and all(_re_d.match(c["opt_maturity"]) for c in _withopt)
                  and all(c["opt_maturity"] == min(c["opt_maturities"]) for c in _withopt),
                  "%d/%d 个合约有期权到期日，样例 %s"
                  % (len(_withopt), len(_allc),
                     (sorted(_withopt, key=lambda c: c["code"])[0]["code"] + " "
                      + sorted(_withopt, key=lambda c: c["code"])[0]["opt_maturity"])
                     if _withopt else "无"))
            # 「最近期权到期」在**真数据**里的形状：None（没期权）或 非负整数（天数）+ 日期
            check("真数据：每组「最近期权到期」形状对（None 或 非负整数 + YYYY-MM-DD 日期）",
                  all((g.get("opt_near_days") is None
                       or (isinstance(g["opt_near_days"], int) and g["opt_near_days"] >= 0))
                      and (g.get("opt_near_date") is None
                           or _re_d.match(g["opt_near_date"]))
                      and ("opt_near_days" in g and "opt_near_date" in g)
                      for g in dd["groups"]),
                  "有值 %d / %d 组" % (sum(1 for g in dd["groups"]
                                          if g.get("opt_near_days") is not None),
                                       len(dd["groups"])))
            check("「最近期权到期」要能追到组内某个合约（有值就必须给出 opt_near_code）",
                  all((g.get("opt_near_days") is None)
                      or g.get("opt_near_code") in [c["code"] for c in g["contracts"]]
                      for g in dd["groups"]))
            # 「最近合约交割」在**真数据**里的形状：None（没有带到期日的合约）或**整数**
            # —— 与 opt_near_days 不同，这里**允许负数**：行情日之后、今天之前交割的合约
            # 还在 mates 里（真库 2026-10-15 上期所集中交割日，21 组是 -1）。
            _dl = [g["fut_near_days"] for g in dd["groups"] if g.get("fut_near_days") is not None]
            check("真数据：每组「最近合约交割」形状对（None 或 整数（可负）+ YYYY-MM-DD 日期）",
                  all((g.get("fut_near_days") is None or isinstance(g["fut_near_days"], int))
                      and (g.get("fut_near_date") is None or _re_d.match(g["fut_near_date"]))
                      and ("fut_near_days" in g and "fut_near_date" in g)
                      for g in dd["groups"]),
                  "有值 %d / %d 组，最小 %s" % (len(_dl), len(dd["groups"]),
                                            min(_dl) if _dl else "—"))
            # ⚠ 这条是「两处不许分叉」的硬钉子：分组字段必须指向**展开行第一行**那个合约
            #   （mates 已按到期日近→远排好）。哪天有人改成「跳过已交割的」或「另找一套」，
            #   主连格与展开行就会指两个合约，页面自相矛盾。
            check("「最近合约交割」必须等于**展开行第一行**（合约与到期日都对上）",
                  all((g.get("fut_near_days") is None)
                      or (g["contracts"] and g["contracts"][0]["code"] == g.get("fut_near_code")
                          and g["contracts"][0]["delist_date"] == g.get("fut_near_date"))
                      for g in dd["groups"]),
                  "；".join("%s=%s" % (g["fut_code"], g.get("fut_near_code"))
                            for g in dd["groups"][:3]))
            # 展开行按合约到期日近→远（真数据）。判据用元组比大小：`(缺?, 日期串)` ——
            # 缺到期日的 `(True, "")` 自然排在有日期的 `(False, "…")` 之后（垫底）。
            def _dl_sorted(cs):
                ks = [(c["delist_date"] is None, c["delist_date"] or "") for c in cs]
                return all(ks[i] <= ks[i + 1] for i in range(len(ks) - 1))

            _bad = [g["fut_code"] for g in dd["groups"] if not _dl_sorted(g["contracts"])]
            check("真数据：组内按**到期日近→远**排（缺到期日的垫底）",
                  not _bad,
                  ("乱序：" + ",".join(_bad[:3])) if _bad else "%d 组合格" % len(dd["groups"]))
            _near = [g["opt_near_days"] for g in dd["groups"] if g.get("opt_near_days") is not None]
            check("默认按「最近期权到期」**升序**（跨交易所也是），没有期权的垫底",
                  _near and all(_near[i] <= _near[i + 1] for i in range(len(_near) - 1))
                  and not [g for g in dd["groups"][len(_near):]
                           if g.get("opt_near_days") is not None],
                  str(_near[:6]))
            check("stats 与实际相符",
                  dd["stats"]["n_mains"] == len(dd["groups"])
                  and dd["stats"]["n_contracts"] == sum(m["n"] for m in dd["markets"]),
                  str(dd["stats"]))
            check("分组不重不漏：同（交易所, 品种码）只出现一次",
                  len({(g["market_code"], g["fut_code"]) for g in dd["groups"]}) == len(dd["groups"]))
        else:
            skip("分组自洽", "库里还没同步期货日线")

    # K 线：日 / 周 / 月 —— 月线必须真聚合（量远大于日线），这是踩过的坑
    kl = {}
    for klt in (101, 102, 103):
        st2, d2 = http("/api/futures/kline?secid=RU.SHF&klt=%d&limit=60" % klt)
        kl[klt] = ((d2 or {}).get("data") or {}) if st2 == 200 else {}
    b1 = kl[101].get("bars") or []
    if not b1:
        check("/api/futures/kline 形状", False, str(kl[101])[:120])
    else:
        check("/api/futures/kline 形状（bars 带 date/开收高低/量额）",
              all(k in b1[-1] for k in ("date", "open", "close", "high", "low", "vol", "amount")),
              "%s %s bars=%d" % (kl[101].get("name"), b1[-1]["date"], len(b1)))
        check("K 线按时间升序（最后一根是最新的）", len(b1) < 2 or b1[-1]["date"] >= b1[-2]["date"])
        b2, b3 = kl[102].get("bars") or [], kl[103].get("bars") or []

        # ⚠⚠ 别比「最后一根」——**月初第一周**时月线与周线的最后一根**本来就是同一批交易日**
        #   （2026-10-08 实测：周线 `10-05~10-09` 与月线「10 月」都只含 10-08 一天 ⇒ vol 相等，
        #   老的 `b3[-1].vol > b2[-1].vol` 直接假红）。这属于「断言了会变的东西」（硬规矩 15）。
        #   改比**平均每根量**：一根月线 ≈ 4.3 根周线 ⇒ 月均量必定大于周均量，且与「今天几号」无关。
        def _avg_vol(bars):
            return sum(x["vol"] or 0 for x in bars) / max(len(bars), 1)

        check("周/月线是**真聚合**：根数明显少于日线、且月均量 > 周均量 > 0",
              b2 and b3 and len(b3) <= len(b2) <= len(b1)
              and _avg_vol(b3) > _avg_vol(b2) > 0,
              "日 %d 根 / 周 %d 根均量 %.0f / 月 %d 根均量 %.0f"
              % (len(b1), len(b2), _avg_vol(b2), len(b3), _avg_vol(b3)))

    bads = 0
    for q in ("", "?secid=bad", "?secid=113..rum", "?secid=RU.SHF&klt=999",
              "?secid=RU.SHF&limit=abc", "?secid=RU.SHF%3BDROP"):
        st2, d2 = http("/api/futures/kline" + q)
        if st2 != 400:
            bads += 1
    check("坏形状当场 400（空 / 非 ts_code / 坏周期 / 坏根数 / 注入字符）", bads == 0,
          "有 %d 个没挡住" % bads)
    # 形状合法、库里没有 → 502 + 人话（既不是 500，也不是静默返回空）
    st3, d3 = http("/api/futures/kline?secid=XX.YYY&limit=5")
    check("ts_code 合法但库里没有 → 502 且说是本地库没有（不是 500 / 不是空结果）",
          st3 == 502 and "本地库" in str((d3 or {}).get("error") or ""),
          "%s %s" % (st3, str((d3 or {}).get("error"))[:60]))

# ---------------------------------------------------------------------------
print("\n[5] 前端实测（无头 Chrome + 同源探针，真点真展开）")
# ---------------------------------------------------------------------------
if not CHROME:
    skip("前端实测", "没找到 Chrome/Edge")
elif st == 0:
    skip("前端实测", "服务没起（先 python run.py）")
else:
    profile = os.path.join(tempfile.gettempdir(), "_fut_chrome_profile")
    t0 = time.time()
    out = chrome_probe_out(BASE + PROBE, 45000, 300, profile)
    if not out.strip():
        check("探针有回传", False, "没抠到 <pre id=\"out\">")
    else:
        for ln in out.splitlines():
            if ln.startswith("OK   "):
                check(ln[5:].strip(), True)
            elif ln.startswith("FAIL "):
                check(ln[5:].split("  ")[0].strip(), False, ln[5:][len(ln[5:].split("  ")[0]):].strip())
            elif ln.startswith("NOTE "):
                skip(ln[5:].split("  ")[0].strip(), ln[5:])
        done = [ln for ln in out.splitlines() if ln.startswith("DONE ")]
        print("      （探针 %.1fs，%s）" % (time.time() - t0, done[0][5:] if done else "未收尾"))

# ---------------------------------------------------------------------------
print("\n" + "=" * 78)
print("通过 %d / 失败 %d / 跳过 %d" % (len(PASS), len(FAIL), len(SKIP)))
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  - " + f)
print("=" * 78)
sys.exit(1 if FAIL else 0)
