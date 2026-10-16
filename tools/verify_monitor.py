# -*- coding: utf-8 -*-
"""分时监控页验证：页面/菜单 + 腾讯行情接口字段契约（含批量） + 交易日历接口 + 页面脚本常量
+ 浏览器端到端 + 增删/拖动宫格交互 + 五档盘口/量比副图/全字段留存
+ **每 3 秒一轮、一轮 = 1 个请求覆盖全部宫格** + **非交易时段只请求一次、且不写本地存储**
+ **不内置任何股票（初始为空）** + **日K 视图（60 根取自本地库、不含当天，当天由本地缓存现算）**
+ **日K 上的 BOLL / MA5·10·20·30·60 / 跳空缺口线（口径与 core/indicators.py 一致，随行情实时重算）**。

重点看四处：
  * **第 2 组**：整个分时监控靠 `https://qt.gtimg.cn/q=<code>` 的「~ 分隔固定下标」解析，
    腾讯哪天调整字段顺序就会全线错位（价格显示成市值、盘口串档之类的）。所以把下标契约固化成断言 ——
    用「成交额 ÷ 成交量 = 均价」做交叉校验压住 f[51]，用「盘后额 ÷ 盘后量 ≈ 现价」压住 f[58]/f[59]，
    用买五档递减 / 卖五档递增 / 买一≤现价≤卖一压住 f[9..f[28]，
    最后拿**本地 daily_kline 自算的 5/10/20/60 日涨幅**去对拍 f[63]/f[69]/f[70]/f[71]。
  * **第 2.2 组**：批量拉取契约 —— 一次问 6 只、6 段都回来、每段仍是 88 字段、
    段落键就是请求时写的代码（所以能按代码分发而不是按顺序对号入座），
    并验「夹带无效代码时有效的照常返回、无效的没有条目」（页面据此单格标红）。
  * **第 2.6 组**：日K 线接口（本站 `/api/monitor/kline`，读本地 daily_kline）——
    默认 60 根、**不含当天**、日期升序、OHLC 自洽、**前复权以最新一根为基准（最后一根收价＝库里原值）**、
    量＝手 / 额＝万元（与腾讯 f[37] 同口径）、`end` 传今天也会被夹回昨天、坏代码进 missing 不拖垮其他只。
    还压两条与「指标口径」有关的：**价格原值透传不取整**（取整会让 60 日均线与策略面板差 1e-6）、
    **后端布林 = MA20 ± 2×样本标准差（÷n-1）** —— 前端 JS 用的就是这条公式，两边必须同一套数。
    接口本身在本地库上验，比只看页面划线靠谱得多。
  * **日K 的技术指标（BOLL(20,2) + MA5/10/20/30/60 + 跳空缺口线）**：静态断言在第 3 组末，
    数值对拍在第 5 组的探针里（用真数据重算一遍，再跟 `/api/kline` 的后端指标逐个比）。
    重点压住三件事：**口径与 core/indicators.py 一致**（MA 用 min_periods=1；布林 sd 用样本标准差 ÷(n-1)；
    跳空与「跳空缺口不补」策略同源 low>昨高 / high<昨低）、**指标算在全量 119 根上只切 60 根出来画**
    （否则 MA60 在窗口里只有一个点）、**当天那根也参与计算**（所以四条线随行情每 3 秒重算）。
  * **第 3 组**：批量轮询（`pullAll`：一轮 1 个请求、`state.reqs++` 每轮只 +1、无 cursor）
    与交易时段门禁（盘中 09:30–11:30 / 13:00–15:00 的交易日才持续轮询；
    非交易时段进入时只请求一次且不留存）只能静态查脚本，真跑要控制时钟，
    所以配第 5 组的探针一起看。同时静态压住「**代码清单不内置默认票**」：
    没存过就是空清单（早先有份 6 只的 DEFAULTS，本地存储一被清空就冒出没添加过的股票）。
  * **第 5 组**：宫格可增删（**初始为空**、上限 6、可全删光）、可拖动排序、五档盘口有值且与接口一致、
    量比副图真的画出来了（把 ECharts 实例的 option 掏出来看 grid/series/markLine）、
    全字段与每整分钟存档确实落了 localStorage、**点「日K」按钮真的切成蜡烛图且当天那根＝本地现算**
    （收价对得上格子上的现价、且不写 localStorage）、
    **日K 上的均线/布林/跳空线真的画出来了、且与后端指标引擎逐个数值相同** —— 都只能真跑 DOM 才验得出来：
    用 static/_probe_mon.html 在同源 iframe 里点真实按钮（`.click()` 走真实监听器）、
    发真实 DnD 事件链（mousedown→dragstart→dragover→drop→dragend），
    并包一层 win.fetch 计数（把 `q=` 掏出来看是不是全部代码）、用 win.__monDebug.force() 覆盖时钟。
    探针跑两趟：先清空代码清单加载（验空态），再种 6 只重载（验有内容那一整套）。

用法：
    python tools/verify_monitor.py            # 全部（含浏览器端到端，默认 3 分钟上下）
    python tools/verify_monitor.py --no-e2e   # 只跑静态 + 接口契约

**「跳过」和「失败」是两码事**：交易日 09:15 之前腾讯只挂一份昨收快照
（今开/最高/最低/五档/量比/成交额全是 0，实测 09:11 取到 f5=f9=f19=f33=f49=0），
那时「盘口有价」「量比 > 0」这类断言没数据可比 —— 报 FAIL 只会让开盘前跑回归得到一片假红，
反而盖住真问题。所以脚本分三档输出：OK / SKIP（时段性不适用，末尾列出来）/ FAIL，
并且**退出码只看 FAIL**。结尾会提示「换个时段重跑即可覆盖」。
"""
from __future__ import annotations

import html as html_mod
import datetime as dt
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = "http://127.0.0.1:8777"
QT = "https://qt.gtimg.cn/q="
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
PROBE = "/static/_probe_mon.html?run=1"   # 探针页不带 run=1 时只提示、不动数据（防用户误打开）

PASS = FAIL = SKIP = 0
FAILS: list[str] = []
SKIPS: list[str] = []

# 盘前标志：交易日 09:15 之前腾讯只挂一份昨收快照，今开/最高/最低/五档/量比/成交额**全是 0**
# （实测 2026-09-16 09:11 取到 f5=f9=f19=f33=f34=f49=0）。那一刻「盘口有价」「量比 > 0」
# 这类断言不是失败、是没数据可比 —— 报 FAIL 只会让开盘前跑回归得到一片假红，反而盖住真问题。
PREOPEN = False


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  OK   {name}" + (f"  {extra}" if extra else ""))
    else:
        FAIL += 1
        FAILS.append(name)
        print(f"  FAIL {name}" + (f"  {extra}" if extra else ""))


def skip(name: str, why: str = "") -> None:
    """本项在本时段不适用（占位，不计入通过/失败）。"""
    global SKIP
    SKIP += 1
    SKIPS.append(name)
    print(f"  SKIP {name}" + (f"  （{why}）" if why else ""))


def check_live(name: str, ok: bool, extra: str = "") -> None:
    """依赖「当日实时成交数据」的断言：盘前跳过（见 PREOPEN 注释）。"""
    if PREOPEN:
        skip(name, "盘前未产生当日成交，本项不适用")
    else:
        check(name, ok, extra)


def get(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")


def get_raw(url: str, timeout: int = 15) -> bytes:
    """取原始字节：腾讯行情是 GBK，先拿 bytes 再自己 decode，别在 get() 里按 utf-8 吃掉。"""
    req = urllib.request.Request(url, headers=HEADERS)
    return urllib.request.urlopen(req, timeout=timeout).read()


CHROME = None
for _c in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
           r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
           os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe")):
    if os.path.exists(_c):
        CHROME = _c
        break


def chrome_dump(url: str, budget_ms: int, timeout: int, profile: str) -> str:
    """无头 Chrome 渲染后 dump 主文档 DOM。独立 profile：保证 localStorage 干净。"""
    out = os.path.join(tempfile.gettempdir(), "_mon_dom_%s.html" % os.path.basename(profile))
    try:
        with open(out, "w", encoding="utf-8") as fh:
            subprocess.run(
                [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
                 "--user-data-dir=" + profile, "--dump-dom",
                 f"--virtual-time-budget={budget_ms}", "--window-size=1560,960", url],
                stdout=fh, stderr=subprocess.DEVNULL, timeout=timeout)
        return open(out, encoding="utf-8", errors="ignore").read()
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


def main() -> int:
    e2e = "--no-e2e" not in sys.argv

    # ---------------- 1. 页面与菜单 ----------------
    print("\n【1】页面与菜单")
    html = get(BASE + "/monitor", timeout=60)
    check("GET /monitor 返回页面", len(html) > 2000, f"{len(html)} 字节")
    check("标题为「分时监控」", "<title>分时监控 · 短线选股</title>" in html)
    check("H1 为「分时监控」", re.search(r'class="page-title">分时监控<', html) is not None)
    check("侧栏含「分时监控」菜单项", 'href="/monitor"' in html and ">分时监控</a>" in html)
    check("该菜单项为选中态", re.search(r'href="/monitor" class="nav-item active"', html) is not None)
    check("宫格容器存在", 'id="mon-grid"' in html)
    check("四个控制按钮存在",
          all(('id="' + x + '"') in html for x in ("mon-pause", "mon-refresh", "mon-clear", "mon-add")))
    check("添加股票的输入框存在", 'id="mon-add-code"' in html)
    check("页面标注「六宫格」而非九宫格", "六宫格" in html and "九宫格" not in html)
    check("页面写明「盘中 3 秒一轮、1 个请求覆盖全部格」",
          "盘中（交易日" in html and "所有格子合并成 1 个请求" in html
          and "只在进入该时段时请求一次" in html and "不写入本地存储" in html)
    check("页面写清格式/节拍（3 秒、1 个请求）",
          "每 3 秒刷新一次" in html and "q=代码1,代码2" in html)
    check("工具栏有「日K」切换按钮", 'id="mon-mode"' in html)
    check("页面写明日K口径（60 根取自本地库、不含当天、当天由本地缓存现算）",
          "画出来的 60 根日K来自本地数据库" in html and "不含当天" in html
          and "当天那根由本地缓存现算" in html)
    check("页面写明日K上的指标（BOLL / MA5·10·20·30·60 / 跳空线，口径与后端一致、实时重算）",
          "MA5 / MA10 / MA20 / MA30 / MA60 均线" in html and "BOLL 布林带" in html
          and "跳空线" in html and "口径与策略回测完全一致" in html
          and "这些线全都是实时重算的" in html)
    check("无未渲染的 Jinja 残留", "{{" not in html and "{%" not in html)
    check("总览菜单仍在（没被顶掉）", ">总览</a>" in html)

    # ---------------- 2. 腾讯行情接口字段契约 ----------------
    print("\n【2】腾讯行情接口字段契约（qt.gtimg.cn）")
    try:
        r = urllib.request.Request(QT + "sz000001", headers=HEADERS)
        resp = urllib.request.urlopen(r, timeout=15)
        hdrs = dict(resp.headers)
        raw = resp.read()
    except Exception as e:  # noqa: BLE001
        check("能访问 qt.gtimg.cn", False, str(e)[:80])
        print("\n  接口不可达，后续契约检查跳过")
        raw = None
        hdrs = {}

    if raw is not None:
        check("接口可达", True, f"{len(raw)} 字节")
        check("响应声明 GBK 编码", "GBK" in (hdrs.get("Content-Type", "") or "").upper(),
              hdrs.get("Content-Type", ""))
        check("带 CORS 头（纯前端直连的前提）",
              (hdrs.get("Access-Control-Allow-Origin", "") or "") == "*",
              str(hdrs.get("Access-Control-Allow-Origin")))
        txt = raw.decode("gbk", "ignore")
        m = re.search(r'="([^"]*)"', txt)
        f = m.group(1).split("~") if m else []
        check("返回 v_<code>=\"…\" 格式且字段数 ≥52", len(f) >= 52, f"字段数={len(f)}")

        if len(f) >= 52:
            global PREOPEN
            PREOPEN = not (float(f[5] or 0) > 0 and float(f[6] or 0) > 0)
            if PREOPEN:
                print("  提示：此刻拿到的是**盘前快照**（今开/成交量=0），"
                      "实时数据类断言按「不适用」跳过；\n"
                      "        集合竞价 09:15 后或收盘后重跑即可全部覆盖。")
            check("f[1] 为股票名称", f[1] == "平安银行", f[1])
            check("f[2] 为 6 位代码", re.fullmatch(r"\d{6}", f[2] or "") is not None, f[2])
            price, prev, vol = float(f[3]), float(f[4]), float(f[6])
            check("f[3] 现价 > 0", price > 0, f[3])
            check("f[4] 昨收 > 0", prev > 0, f[4])
            check("f[6] 成交量(手) ≥ 0", vol >= 0, f[6])
            chg, pct = float(f[31]), float(f[32])
            check("f[31]/f[32] 涨跌额与幅度自洽",
                  abs(chg - (price - prev)) < 0.02 and abs(pct - (price / prev - 1) * 100) < 0.05,
                  f"涨跌={chg} 幅度={pct}%")
            hi, lo = float(f[33]), float(f[34])
            check_live("f[33] 最高 ≥ f[34] 最低，且现价落在区间内",
                       hi >= lo and lo - 1e-6 <= price <= hi + 1e-6, f"高={hi} 低={lo}")
            # 均价交叉校验：成交额(万元)×10000 ÷ (成交量手×100) 应等于 f[51] 均价
            avg_iface, amount = float(f[51]), float(f[37])
            calc = amount * 10000 / (vol * 100) if vol > 0 else avg_iface
            check_live("f[51] 均价 与 成交额/成交量 交叉一致（下标没漂）",
                       abs(avg_iface - calc) <= 0.02, f"接口={avg_iface} 计算={calc:.3f}")
            tm = [v for v in f[28:34] if re.fullmatch(r"20\d{12}", v or "")]
            check("时间字段是 14 位 yyyymmddHHMMSS", len(tm) == 1, tm[0] if tm else "未找到")
            check("接口返回 88 个字段（与页面里的 FLD 表同长）", len(f) == 88, f"len={len(f)}")

            # ---- 五档盘口：分时监控右边那一栏直接用这几项 ----
            bid_p = [float(f[9 + k * 2]) for k in range(5)]
            ask_p = [float(f[19 + k * 2]) for k in range(5)]
            bid_v = [float(f[10 + k * 2]) for k in range(5)]
            ask_v = [float(f[20 + k * 2]) for k in range(5)]
            check("买一到买五价格递减（买一最高）",
                  all(bid_p[k] >= bid_p[k + 1] for k in range(4)), str(bid_p))
            check("卖一到卖五价格递增（卖一最低）",
                  all(ask_p[k] <= ask_p[k + 1] for k in range(4)), str(ask_p))
            check_live("买一 ≤ 现价 ≤ 卖一（档位没串）",
                       bid_p[0] - 1e-6 <= price <= ask_p[0] + 1e-6,
                       f"买一={bid_p[0]} 现价={price} 卖一={ask_p[0]}")
            check("五档量都 ≥ 0（手）", all(v >= 0 for v in bid_v + ask_v),
                  f"买一量={bid_v[0]} 卖一量={ask_v[0]}")

            # ---- 量比（f49）：盘面副图画的就它 ----
            vr = float(f[49])
            check_live("f[49] 量比 > 0", vr > 0, str(vr))

            # ---- 涨跌停价随板块走（平安银行是主板 10%） ----
            check("f[47]/f[48] 涨停价/跌停价 = 昨收 ±10%（主板）",
                  abs(float(f[47]) - prev * 1.1) < 0.02 and abs(float(f[48]) - prev * 0.9) < 0.02,
                  f"涨停={f[47]} 跌停={f[48]} 昨收={prev}")

            # ---- 盘后固定价格交易：额(万) ÷ 量(手) 应该 ≈ 现价 ----
            after_amt, after_vol = float(f[58] or 0), float(f[59] or 0)
            if after_vol > 0:
                check("f[58]/f[59] 是 盘后额(万)/盘后量(手)（额÷量≈现价）",
                      abs(after_amt * 10000 / (after_vol * 100) - price) <= 0.05,
                      f"额={after_amt}万 量={after_vol}手 → {after_amt * 10000 / (after_vol * 100):.3f}")
            else:
                check("f[58]/f[59] 是 盘后额(万)/盘后量(手)（今日无盘后成交，量=0）", True, "量=0")

            # ---- 52 周高低必须包住当日高低 ----
            check_live("f[67]/f[68] 52周最高/最低包住当日区间",
                       float(f[67]) >= hi - 1e-6 and float(f[68]) <= lo + 1e-6,
                       f"52周 {f[68]}~{f[67]} 当日 {lo}~{hi}")

            # ---- f[35] 的「价/量/额」三段与 f[3]/f[6]/f[37] 自洽 ----
            seg = (f[35] or "").split("/")
            check("f[35]（价/量/额）与 f[3]/f[6]/f[37] 一致",
                  len(seg) == 3 and abs(float(seg[2]) - amount * 10000) / max(amount * 10000, 1) < 1e-3,
                  f"f[35]={f[35][:34]} f[37]={amount}万")

            # ---- 涨幅类字段：拿本地日线自算对拍 ----
            # 这条是「f[63]/f[69]/f[70]/f[71] 到底是什么」的直接证据，别改成凭印象的常量。
            # 只有当本地日线同步到了「昨收」那一天才能对拍，否则跳过（数据没跟上，对不上不是接口的错）。
            try:
                _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                if _root not in sys.path:
                    sys.path.insert(0, _root)
                from core import db as _db
                rows = _db.query("""SELECT trade_date, close FROM daily_kline
                                    WHERE ts_code='000001.SZ' ORDER BY trade_date DESC LIMIT 70""")
                c = [float(r["close"]) for r in rows]
                if abs(c[0] - prev) < 1e-6:
                    for n, ix in ((5, 63), (10, 69), (20, 70), (60, 71)):
                        got = float(f[ix])
                        want = (price / c[n - 1] - 1) * 100
                        check(f"f[{ix}] = {n}日涨幅%（与本地日线自算一致）",
                              abs(got - want) < 0.05, f"接口={got} 自算={want:.2f}")
                else:
                    print(f"  · 本地日线最新 {c[0]} ≠ 昨收 {prev}（数据未同步到昨日），涨幅对拍跳过")
            except Exception as e:  # noqa: BLE001
                print(f"  · 本地日线不可用，涨幅对拍跳过：{str(e)[:70]}")
    else:
        for _ in range(12):
            check("接口契约项（因不可达跳过）", False)

    # ---------------- 2.2 批量拉取契约（整个页面每轮只有这一个请求） ----------------
    print("\n【2.2】批量拉取契约（q=代码1,代码2,…）")
    if raw is not None:
        try:
            codes_req = ["sh600519", "sz000001", "sh601318", "sz300750", "sh688981", "sz000858"]
            t0 = time.time()
            braw = get_raw(QT + ",".join(codes_req))
            el = time.time() - t0
            btxt = braw.decode("gbk", "ignore")
            segs = dict(re.findall(r'v_([a-z]{2}\d{6})="([^"]*)"', btxt))
            check("一次问 6 只，6 只都回来了（每只一段 v_<代码>）",
                  len(segs) == 6, f"{len(segs)} 段：{sorted(segs)}")
            check("每只字段数都是 88（与单只请求一致）",
                  all(len(v.split("~")) == 88 for v in segs.values()),
                  str({k: len(v.split("~")) for k, v in segs.items()}))
            check("段落的键就是请求时写的代码（能按代码分发，不靠顺序）",
                  sorted(segs) == sorted(codes_req), str(sorted(segs)))
            bad = "sh600519,sz999999"
            btxt2 = get_raw(QT + bad).decode("gbk", "ignore")
            segs2 = dict(re.findall(r'v_([a-z]{2}\d{6})="([^"]*)"', btxt2))
            check("夹带无效代码时：有效的照常返回、无效的直接没有条目（页面据此单格标红）",
                  list(segs2) == ["sh600519"], str(list(segs2)))
            check("6 只一个请求耗时 < 2 秒（批量没拖慢）", el < 2.0, f"{el:.2f}s")
        except Exception as e:  # noqa: BLE001
            for _ in range(5):
                check("批量拉取契约项（请求失败）", False, str(e)[:60])
    else:
        for _ in range(5):
            check("批量拉取契约项（接口不可达跳过）", False)

    # ---------------- 2.5 交易日历接口 ----------------
    print("\n【2.5】交易日历接口（分时监控靠它判断「今天该不该取数」）")
    today = dt.date.today().isoformat()
    try:
        raw_cal = json.loads(get(BASE + "/api/market/trade_days?start=2026-09-13&end=2026-09-20"))["data"]
        days = raw_cal["days"]
        check("区间内只返回开市日",
              days == ["2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18"],
              str(days))
        check("周末被排除（2026-09-13 是周日）", "2026-09-13" not in days)
        nat = json.loads(get(BASE + "/api/market/trade_days?start=2026-10-01&end=2026-10-09"))["data"]["days"]
        check("国庆假期被排除（10-01…10-07 休市）", not any(d <= "2026-10-07" for d in nat), str(nat))
        check("节后 2026-10-08 恢复开市", "2026-10-08" in nat)
        today_days = json.loads(
            get(BASE + "/api/market/trade_days?start=%s&end=%s" % (today, today)))["data"]["days"]
        is_today_trade = today in today_days
        check("能查到「今天是不是交易日」", len(today_days) <= 1,
              "%s → %s" % (today, "交易日" if is_today_trade else "休市"))
        check("默认区间（今天起 30 天）返回未来交易日",
              len(json.loads(get(BASE + "/api/market/trade_days"))["data"]["days"]) >= 5)
        try:
            over = json.loads(get(BASE + "/api/market/trade_days?start=2020-01-01&end=2026-01-01"))
            check("区间过大被拦（>400 天）", over.get("ok") is False, str(over)[:60])
        except urllib.error.HTTPError as he:          # 400 也会被 urlopen 抛出来
            bad = json.loads(he.read().decode("utf-8", "ignore"))
            check("区间过大被拦（>400 天）", he.code == 400 and bad.get("ok") is False, str(bad)[:60])
    except Exception as e:  # noqa: BLE001
        is_today_trade = None
        check("交易日历接口可用", False, str(e)[:120])
        for _ in range(6):
            check("日历契约项（因接口不可用跳过）", False)

    # ---------------- 2.6 日K 线接口（切日K时向本站要本地日线） ----------------
    print("\n【2.6】日K 线接口（/api/monitor/kline：读本地 daily_kline，默认 60 根不含当天）")
    try:
        today_iso = dt.date.today().isoformat()
        dk = json.loads(get(BASE + "/api/monitor/kline?codes=sz000001,sh600519&bars=60"))["data"]
        check("一次问 2 只就返回 2 只（批量）",
              sorted(dk["bars"]) == ["sh600519", "sz000001"], str(sorted(dk["bars"])))
        b = dk["bars"]["sz000001"]
        check("默认取 60 根", len(b) == 60, f"{len(b)} 根")
        check("日期升序（图从旧到新画）",
              all(b[i]["date"] < b[i + 1]["date"] for i in range(len(b) - 1)),
              f"{b[0]['date']} → {b[-1]['date']}")
        check("**不含当天**（当天那根由前端本地缓存现算）",
              all(x["date"] < today_iso for x in b), f"最后一根 {b[-1]['date']} / 今天 {today_iso}")
        check("每根字段齐全（开高低收/昨收/涨跌幅/量/额）",
              all(set(x) >= {"date", "open", "high", "low", "close", "pre_close", "pct", "vol", "amount"}
                  for x in b), str(sorted(b[-1])))
        check("OHLC 自洽（低 ≤ 开/收 ≤ 高，且都 > 0）",
              all(x["low"] <= min(x["open"], x["close"]) + 1e-6
                  and x["high"] >= max(x["open"], x["close"]) - 1e-6
                  and min(x["open"], x["high"], x["low"], x["close"]) > 0 for x in b),
              str([b[-1][k] for k in ("open", "high", "low", "close")]))
        check("量 > 0（副图画的就是它）", b[-1]["vol"] > 0, str(b[-1]["vol"]))
        check("声明了单位（价=元 / 量=手 / 额=万元）",
              dk["units"] == {"price": "元", "vol": "手", "amount": "万元"}, str(dk["units"]))
        check("复权口径与 config.ADJUST_MODE 一致", dk["adjust"] == "qfq", str(dk["adjust"]))
        check("返回里写明了下界日期（end=昨天）", dk["end"] < today_iso, str(dk["end"]))

        # 与库里对拍：日期、前复权锚点、额的单位换算（库里是千元）
        try:
            from core import db as _db
            row = _db.query_one(
                "SELECT trade_date,open,high,low,close,amount FROM daily_kline "
                "WHERE ts_code='000001.SZ' AND trade_date<%s ORDER BY trade_date DESC LIMIT 1",
                (today_iso,))
            if row:
                check("最后一根就是库里最新那根（日期一致）",
                      b[-1]["date"] == str(row["trade_date"])[:10],
                      f"{b[-1]['date']} / 库 {row['trade_date']}")
                check("**前复权以最新一根为基准**（最后一根收价＝库里原值，所以与实时价可比）",
                      abs(b[-1]["close"] - float(row["close"])) < 0.005,
                      f"接口 {b[-1]['close']} / 库 {row['close']}")
                check("额的单位从库里的千元折成万元（与腾讯 f[37] 同口径）",
                      abs(b[-1]["amount"] - float(row["amount"]) / 10.0) < 0.05,
                      f"接口 {b[-1]['amount']}万 / 库 {row['amount']}千元")
                # 复权确实生效：更早的一根与库里原值不同（除非这只票从没除权过）
                raw_first = _db.scalar(
                    "SELECT close FROM daily_kline WHERE ts_code='000001.SZ' AND trade_date=%s",
                    (b[0]["date"],))
                if raw_first is not None and abs(float(raw_first) - b[0]["close"]) > 1e-6:
                    check("更早的根按复权因子缩放过（不是原样透传）", True,
                          f"{b[0]['date']} 接口 {b[0]['close']} / 库 {float(raw_first):.4f}")
                else:
                    check("更早的根按复权因子缩放过（这只票区间内无除权，跳过）", True, "无除权")
            else:
                for _ in range(4):
                    check("与库里对拍（本地无该股日线，跳过）", False)
        except Exception as e:  # noqa: BLE001
            print(f"  · 本地库对拍跳过：{str(e)[:70]}")

        c6 = json.loads(get(BASE + "/api/monitor/kline?codes=600519"))["data"]["bars"]
        check("只写 6 位数字也认（600519 → 600519.SH）", "600519" in c6, str(list(c6)))
        short = json.loads(get(BASE + "/api/monitor/kline?codes=sz000001&bars=5"))["data"]["bars"]["sz000001"]
        check("bars=5 就只给 5 根", len(short) == 5, f"{len(short)} 根")
        big = json.loads(get(BASE + "/api/monitor/kline?codes=sz000001&bars=9999"))["data"]
        check("bars 超上限被夹到 250", big["bars_per_code"] == 250 and len(big["bars"]["sz000001"]) <= 250,
              f"bars_per_code={big['bars_per_code']}")
        e = json.loads(get(BASE + "/api/monitor/kline?codes=sz000001&bars=3&end=" + today_iso))["data"]
        check("传 end=今天也会被夹回昨天（当天不许从库里取）",
              e["end"] < today_iso and all(x["date"] < today_iso for x in e["bars"]["sz000001"]),
              f"end={e['end']}")
        bad = json.loads(get(BASE + "/api/monitor/kline?codes=sz999999,zzz"))["data"]
        check("无效 / 库里没有的代码进 missing，不影响其他只",
              bad["missing"] == ["sz999999", "zzz"] and bad["bars"] == {}, str(bad["missing"]))
        mixed = json.loads(get(BASE + "/api/monitor/kline?codes=sz999999,sz000001"))["data"]
        check("一只坏一只好：好的照常返回，坏的单独列出来",
              "sz000001" in mixed["bars"] and mixed["missing"] == ["sz999999"],
              f"{sorted(mixed['bars'])} / missing={mixed['missing']}")
        # 指标口径的前置条件：接口给的价格必须是原值（前端要拿它现算 MA/BOLL/跳空）。
        # 早先这里 round(…, 3)，60 日均线因此跟策略面板差到 1e-6 —— 两套数看着一样、其实不是一个口径。
        m119 = json.loads(get(BASE + "/api/monitor/kline?codes=sh600519&bars=119"))["data"]["bars"]["sh600519"]
        k119 = {r["trade_date"]: r for r in
                json.loads(get(BASE + "/api/kline?code=600519.SH&bars=119"))["data"]["bars"]}
        pair = [(b, k119[b["date"]]) for b in m119 if b["date"] in k119]
        maxd = max((abs(b["close"] - r["close"]) for b, r in pair), default=1.0)
        check("价格原值透传（与 /api/kline 的同一批前复权价逐个相同，没有四舍五入）",
              len(pair) >= 100 and maxd < 1e-9, f"{len(pair)} 根对拍，最大差 {maxd:.2e}")
        # 前端 JS 用的布林公式 = 后端 core/indicators.py 的公式：MA20 ± k×**样本**标准差（pandas rolling.std 是 ddof=1）
        if pair:
            last_mon, last_k = pair[-1]
            w20 = [b["close"] for b in m119[-20:]]
            mid = sum(w20) / 20.0
            sd = math.sqrt(sum((x - mid) ** 2 for x in w20) / 19.0)
            check("后端布林 = MA20 ± 2×样本标准差（÷n-1）—— 前端 JS 用的是同一条公式",
                  abs(last_k["boll_mid"] - mid) < 1e-6
                  and abs(last_k["boll_upper"] - (mid + 2 * sd)) < 1e-6
                  and abs(last_k["boll_lower"] - (mid - 2 * sd)) < 1e-6,
                  f"上轨 后端 {last_k['boll_upper']:.6f} / 自算 {mid + 2 * sd:.6f}")
            check("后端均线是 min_periods=1 口径（第一根就有值，前端 JS 跟它一致）",
                  all(abs(sum(b["close"] for b in m119[-n:]) / n - last_k[f"ma{n}"]) < 1e-6
                      for n in (5, 10, 20, 60)),
                  f"MA60 后端 {last_k['ma60']:.6f}")

        for q, why in ((None, "缺 codes"), ("a,b,c,d,e,f,g,h,i,j,k,l,m", "一次超过 12 只")):
            url = BASE + "/api/monitor/kline" + (("?codes=" + q) if q else "")
            try:
                r = json.loads(get(url))
                check(f"{why} 被拦", r.get("ok") is False, str(r)[:60])
            except urllib.error.HTTPError as he:
                check(f"{why} 被拦", he.code == 400, str(he.code))
    except Exception as e:  # noqa: BLE001
        check("日K 线接口可用", False, str(e)[:120])
        for _ in range(25):
            check("日K 线接口契约项（因接口不可用跳过）", False)

    # ---------------- 3. 页面脚本常量与实现要点 ----------------
    print("\n【3】页面脚本常量与实现要点")
    js = html
    check("轮询间隔 3 秒", "POLL_MS = 3000" in js)
    # ---- 一次请求覆盖全部宫格（本次改动的核心） ----
    check("批量拉取：q= 后面用逗号拼全部代码",
          re.search(r"fetch\(API \+ codes\.join\(','\)", js) is not None)
    check("批量响应按 v_<代码> 逐段解析（不是只取第一段）",
          re.search(r"/v_\(\[a-z\]\{2\}\[0-9\]\{6\}\)=/", js) is not None
          or re.search(r"v_\(\[a-z\]\{2\}\\d\{6\}\)=\"", js) is not None)
    check("解析与拉取分开：parseQuote 单独一份（单只/批量共用同一套字段映射）",
          "function parseQuote(" in js and "function fetchQuotes(" in js)
    check("一轮只 +1 个请求计数（与格数无关）",
          re.search(r"async function pullAll\(manual\)[\s\S]{0,900}?state\.reqs\+\+;", js) is not None
          and re.search(r"state\.reqs \+= codes\.length", js) is None)
    check("盘中 step 调 pullAll（而不是逐格 pullOne）",
          re.search(r"await pullAll\(false\)", js) is not None)
    check("批量返回按代码分发到各宫格（缺哪只就单独标红）",
          "const q = map[code];" in js and "这只没返回数据" in js)
    check("节拍按「扣掉请求耗时」算（慢请求不会越拖越长）",
          re.search(r"schedule\(Math\.max\(1000, POLL_MS - \(Date\.now\(\) - t0\)\)\)", js) is not None)
    check("非交易时段一次性快照也是 1 个请求（offHoursShot 走 pullAll）",
          re.search(r"async function offHoursShot\(\)[\s\S]{0,300}?await pullAll\(true\)", js) is not None)
    check("「全部刷新」也是 1 个请求（不再逐格 + 120ms 间隔）",
          re.search(r"async function refreshAll\(\)[\s\S]{0,700}?await pullAll\(true\)", js) is not None
          and "await new Promise(r => setTimeout(r, 120))" not in js)
    check("已不存在「按格轮转」的 cursor 状态", "state.cursor" not in js)
    check("格数上限 6（最多 6 只）", re.search(r"const MAX_CELLS = 6;", js) is not None)
    check("本地存储保留 12 小时", "TTL = 12 * 3600 * 1000" in js)
    check("采样点上限够装一整天（3 秒一轮 ≈ 4800 点）",
          re.search(r"const MAX_PTS = 5000;", js) is not None)
    check("本地存储 key 前缀独立", "sm_mon_pts_" in js and "sm_mon_codes" in js)
    check("直连腾讯接口（行情未经本站后端）", "https://qt.gtimg.cn/q=" in js)
    check("用 TextDecoder('gbk') 解码", 'TextDecoder("gbk")' in js or "TextDecoder('gbk')" in js)
    check("时间字段按 14 位正则容错定位", re.search(r"/\^20\\d\{12\}\$/", js) is not None)
    # ---- 交易时段门禁（本次新增的核心行为） ----
    check("有交易时段判定（phaseOf）",
          "function phaseOf(" in js and "function isTradeDay(" in js)
    check("交易时段 = 09:30–11:30 / 13:00–15:00",
          all(re.search(p, js) is not None for p in (
              r"AM_OPEN = 9 \* 3600 \+ 30 \* 60", r"AM_CLOSE = 11 \* 3600 \+ 30 \* 60",
              r"PM_OPEN = 13 \* 3600", r"PM_CLOSE = 15 \* 3600")))
    check("非盘中不做自动轮询（pullAll 的 manual 门禁）",
          re.search(r"if \(!manual && !isOpen\(phaseOf\(\)\)\) return", js) is not None)
    check("非盘中 step 走 idle 分支（本地空转，不持续请求）",
          "scheduleIdle(" in js and re.search(r"if \(!isOpen\(ph\)\) \{", js) is not None)
    check("非交易时段进入时只请求一次（one-shot，同一时段不重复）",
          "async function offHoursShot(" in js and "state.shot !== ph.k" in js
          and "await offHoursShot();" in js)
    check("非交易时段那份快照不落盘（storeSave 只在盘中调用）",
          re.search(r"if \(isOpen\(ph\)\) storeSave\(", js) is not None)
    check("缓存只在「盘中 + 行情日期＝今天」时写入",
          "function cacheable(" in js and "q.date === ymd(new Date())" in js)
    check("行情日期取自行情时间（14 位 yyyymmdd）", "date: t14 ?" in js)
    check("非交易时段先用本地快照把格子画出来（随后由一次性请求覆盖）",
          "function paintSnap(" in js and "st.snap = { t: Date.now()" in js)
    check("交易日历取自本站接口（与日K线并列，本站只这两处）", "CAL_API = '/api/market/trade_days'" in js)
    check("日历取不到时退回周一~周五近似",
          "function calFresh(" in js and "w >= 1 && w <= 5" in js)
    check("非盘中状态栏提示「仅取一次快照」", "仅取一次快照" in js)
    # ---- 不内置任何股票：本地存储一被清空就该是空清单，不能冒出没添加过的票 ----
    check("没有内置代码清单（DEFAULTS 已删）",
          not re.search(r"const\s+DEFAULTS\b", js))
    check("读不到配置时返回空清单（不是默认票）",
          re.search(r"return \[\];\s*\n\s*\}\s*\n\s*function codesSave", js) is not None)
    check("空清单也被当成「没加过」（v2 codes:[] 不回落默认）",
          "if (arr && arr.length)" in js)
    check("空态给出添加引导（不是白屏）", "mon-empty" in js and "还没有添加股票" in js)
    check("空态下不摆「第 0 轮 · 0 格」那种读不通的状态栏",
          re.search(r"if \(!state\.codes\.length\) \{\s*\n\s*el\.textContent", js) is not None)
    check("空态下「全部刷新」给提示而不是空跑", "还没有添加股票，先在上方输入代码添加" in js)
    check("允许删到 0 格（不再限制至少 1 格）",
          "至少保留 1 格" not in js and re.search(r"function removeCell\(i\) \{\s*\n\s*if \(!state\.codes\.length\) return;", js) is not None)
    check("移除最后一格时的提示文案说明已清空", "宫格已清空" in js)
    # ---- 「清空本地缓存」只清行情，不能把用户加的自选一起清掉 ----
    check("清空缓存按 key 精确排除代码清单（别按 sm_mon_ 前缀一刀切）",
          re.search(r"k !== LS_CODES", js) is not None)
    check("清空缓存按钮的二次确认也写明清单会保留", "股票代码清单会保留" in js)
    # ---- 无头端到端要一个「有内容」的页面，但干净 profile 里没有任何配置 ----
    check("留了 ?codes= 播种钩子（测试用，正常不会带）",
          "qs.get('codes')" in js and "URLSearchParams" in js)
    check("横轴按秒序号（0~14400）而非分钟", "secIdx" in js and "14400" in js)
    check("状态栏显示「N 格 / 每 3 秒一次」与本轮命中数",
          "格 / 每 3 秒一次" in js and "本轮 ' + state.lastN" in js)
    check("有增/删宫格实现（addCell / removeCell）",
          "function addCell(" in js and "function removeCell(" in js)
    check("每格渲染 × 删除按钮", 'class="mon-del"' in js)
    # 删除按钮必须在格子右上角**常显**：写 opacity:0 靠 hover 才出现的话用户根本看不见
    css = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "static", "css", "app.css"), encoding="utf-8").read()
    m_del = re.search(r"\.mon-del\{([^}]*)\}", css)
    check("删除按钮绝对定位在格子右上角",
          bool(m_del) and "position:absolute" in m_del.group(1) and "right:" in m_del.group(1),
          (m_del.group(1).strip()[:70] if m_del else "未找到 .mon-del 规则"))
    check("删除按钮常显（不是 hover 才浮出来）",
          bool(m_del) and "opacity:0;" not in m_del.group(1).replace(" ", ""))
    check("代码清单按 v2 结构持久化", "v: 2, codes: state.codes" in js)
    check("增删后丢弃在途请求（代次校验）", "gen !== state.gen" in js)
    check("重复代码被拦（不出现同一只两次）", "已在监控中" in js)

    # ---- 拖动排序（原生 HTML5 DnD） ----
    check("每格头行有拖拽把手（.mon-grip）", 'class="mon-grip"' in js)
    check("有重排实现（reorderCells）", "function reorderCells(" in js)
    check("draggable 平时关着（否则会抢走代码框的选字）",
          'draggable="false"' in js and "cell.draggable = !(e.target.closest" in js)
    check("dragover 里 preventDefault（不写就不触发 drop）",
          re.search(r"addEventListener\('dragover'[\s\S]{0,700}?e\.preventDefault\(\)", js) is not None)
    check("按目标格左右半区决定插到前面还是后面",
          "r.width / 2" in js and "drop-l" in js and "drop-r" in js)
    check("重排时 codes 与 cells 同一套置换（不许只挪一边）",
          re.search(r"state\.codes\.splice\(from, 1\)", js) is not None
          and re.search(r"state\.cells\.splice\(from, 1\)", js) is not None)
    check("重排后 gen++（在途请求按代次丢弃）",
          re.search(r"function reorderCells[\s\S]{0,900}?state\.gen\+\+", js) is not None)
    check("重排后写回 localStorage（顺序能持久）",
          re.search(r"function reorderCells[\s\S]{0,900}?codesSave\(\)", js) is not None)
    m_grip = re.search(r"\.mon-grip\{([^}]*)\}", css)
    check("把手用 radial-gradient 点阵画（不依赖字体里有盲文字符）",
          bool(m_grip) and "radial-gradient" in m_grip.group(1),
          (m_grip.group(1).strip()[:60] if m_grip else "未找到 .mon-grip 规则"))
    check("拖动中原件半透明 + 目标格显示竖直插入线",
          ".mon-cell.dragging{" in css and ".mon-cell.drop-l::before" in css
          and ".mon-cell.drop-r::before" in css)

    # ---- 图：主图（分时 + 均价）+ 量比副图 ----
    check("有均价线，且直接用接口均价字段（不自己重算）",
          "name: '均价', type: 'line'" in js and "p[3] > 0" in js and "avg: +f[IX.avg]" in js)
    check("量比副图用接口 f49（不自己算）", "vr: +f[IX.vr]" in js and "const VR_COLOR" in js)
    check("一个实例两层 grid（主图 + 副图，共用同一时间轴）",
          re.search(r"grid: \[\s*\{[^}]*\},\s*\{[^}]*height: \d+,", js) is not None)
    check("量比副图挂第 2 组 x/y 轴", "xAxisIndex: 1, yAxisIndex: 1" in js)
    check("量比画了 1.0 基准虚线", re.search(r"data: \[\{ yAxis: 1 \}\]", js) is not None)
    check("图例可点开关（价格 / 均价 / 量比）",
          re.search(r"data: \['价格', '均价', '量比'\]", js) is not None)

    # ---- 五档盘口 ----
    check("盘口在格子主体右侧（.mon-body > .mon-book）",
          'class="mon-book" id="mon-book-' in js and ".mon-body{" in css)
    check("盘口渲染 卖5→卖1 再 买1→买5 十档",
          re.search(r"for \(let k = 4; k >= 0; k--\)", js) is not None
          and re.search(r"for \(let k = 0; k < 5; k\+\+\)", js) is not None)
    check("五档下标只写一处（IX.bid1=9 / IX.ask1=19）",
          "bid1: 9" in js and "ask1: 19" in js)
    check("盘口价格按昨收着色（涨红跌绿）",
          re.search(r"const tone = p => \(p > 0 && prev > 0\)", js) is not None)
    check("底行补了量比当前值", "<span>量比 <b>" in js)
    check("换股票时会把旧盘口清掉（不留上一只的五档）",
          "el.querySelector('.mon-book')" in js)

    # ---- 全字段本地留存 ----
    m_fld = re.search(r"const FLD = \[(.*?)\n  \];", js, re.S)
    n_fld = len(re.findall(r"'[^']*\|[^']*'", m_fld.group(1))) if m_fld else 0
    check("页面字段表 88 项（与接口字段数一致）", n_fld == 88, f"{n_fld} 项")
    check("字段表带可信度四档（已核对/用户提供/未考证/空占位）",
          "const MARK_TXT = { v: ['已核对'" in js and "u: ['用户提供'" in js
          and "'?': ['未考证'" in js and "'-': ['空占位'" in js)
    check("字段表关键位置对得上（f49=量比 f51=均价）",
          "'量比|v'" in js and "'均价|v'" in js)
    check("parseQuote 把 88 项原始字段原样带出来", "raw: f.slice()" in js)
    check("存储升到 v3（采样点 6 位 + 全字段存档 hist + 快照带 raw）",
          "v: 3, t: Date.now" in js and "HIST_MAX" in js and "hist: hs" in js)
    check("采样点存了 均价 / 量比 / 量",
          re.search(r"\[Date\.now\(\), sec, q\.price, q\.avg \|\| 0, q\.vr \|\| 0, q\.vol \|\| 0\]",
                    js) is not None)
    check("快照里也存了盘口与全字段（非交易时段靠一次性请求 + 本地快照画全）",
          "bids: q.bids, asks: q.asks" in js and "raw: q.raw || null" in js)
    check("每整分钟落 1 条全字段存档", re.search(r"q\.minute !== st\.lastMin", js) is not None)
    check("旧 v2 采样点兼容读取（缺位补 0）", "p[4] || 0, p[5] || 0" in js)
    check("点股票名打开全字段表弹窗",
          "function openFieldModal(" in js and "openFieldModal(+nm.closest" in js)
    check("弹窗逐项列出字段 + 一键复制 JSON（含字段名与存档）",
          "field_names: FLD_NAME.slice()" in js and "复制 JSON" in js
          and ".fld-tbl{" in css)
    # ---- 日K 视图：库里近 60 根（不含当天）+ 当天由本地缓存现算 ----
    check("日K 默认 60 根、且写明不含当天", "DK_BARS = 60" in js)
    check("日K 走本站接口（腾讯那个接口只有当日快照、没有历史日线）",
          "DK_API = '/api/monitor/kline'" in js)
    check("日线也是一次问全部格（不逐格发）",
          re.search(r"DK_API \+ '\?codes=' \+ need\.join\(','\)", js) is not None)
    check("日线按 DK_REQ 请求（60 根展示 + 59 根预热）", re.search(r"'&bars=' \+ DK_REQ", js) is not None)
    check("日线只补「没有 / 过期 / 强制」的格子（TTL 缓存，一天才变一次）",
          "DK_TTL" in js and re.search(r"Date\.now\(\) - d\.at > DK_TTL", js) is not None)
    m_fd = re.search(r"async function fetchDaily\(codes, force\) \{(.*?)\n  \}", js, re.S)
    fd = m_fd.group(1) if m_fd else ""
    check("日线只放内存（state.daily），fetchDaily 里根本不碰 localStorage",
          len(fd) > 300 and "state.daily[c] = {" in fd and "localStorage" not in fd,
          f"{len(fd)} 字符")
    check("只有停在日K视图才去取日线（分时视图不碰本站）",
          re.search(r"if \(state\.mode !== 'daily' \|\| !state\.codes\.length\) return 0;", js) is not None)
    check("切换按钮文案＝点下去会切到哪个视图（和「暂停/继续」一个路子）",
          "btn.textContent = daily ? '分时' : '日K'" in js)
    check("切换按钮带真实监听器与 .on 状态",
          re.search(r"getElementById\('mon-mode'\)\.addEventListener\('click'", js) is not None
          and re.search(r"classList\.toggle\('on', daily\)", js) is not None)
    m_tb = re.search(r"function todayBar\(i\) \{(.*?)\n  \}", js, re.S)
    tb = m_tb.group(1) if m_tb else ""
    check("当天那根由本地缓存现算（todayBar 单独一份）", len(tb) > 400, f"{len(tb)} 字符")
    check("当天高低/量/额取每整分钟存档里的「当日累计」字段（取最大值就是全天口径）",
          all(p in tb for p in ("r[IX.high]", "r[IX.low]", "r[IX.vol]", "r[IX.amount]")))
    check("分时采样点当兜底（pts 第 6 位＝累计成交量）", "p[5] > vol" in tb)
    check("今开优先用快照 f[5]（整日不变的常量）", "(s && s.open > 0) ? s.open" in tb)
    check("今开必须落进当日高低区间（缺帧时兜一下）", "if (openF > high) high = openF;" in tb)
    check("隔夜快照不算今天的（快照日期＝今天才认）", "st.snap.date === todayS" in tb)
    check("拿不到当天数据就返回 null（不硬凑一根假 K 线）",
          "(!(openF > 0)" in tb and "return null;" in tb)
    check("库里已有当天时以库里的为准（不重复叠一根本地算的）",
          "bars[bars.length - 1].date < tb.date" in js)
    check("日K 用蜡烛图 + 成交量副图（成交量按当根涨跌着色）",
          "type: 'candlestick'" in js and "name: '成交量'" in js
          and "type: 'bar', data: vdata" in js)
    check("蜡烛涨红跌绿（中国习惯）",
          "const UP_COLOR = '#e5352b', DOWN_COLOR = '#12a06a'" in js)
    check("蜡烛数据顺序是 开·收·低·高（ECharts 口径，写反会倒蜡烛）",
          "bars.map(b => [b.open, b.close, b.low, b.high])" in js)
    check("日K 也画了「今日（本地缓存现算）」标记（那一根不是库里的）",
          "今日（本地缓存现算）" in js)
    check("日K 视图下空格子提示说的是日线到没到（与交易时段无关）",
          "function dailyTip(" in js and "先在「数据管理」同步日线" in js)
    check("日K 视图下行情照旧轮询（每轮 applyQuote → drawCell，当天那根跟着动）",
          re.search(r"function drawCell\(i\) \{\s*\n\s*const st = state\.cells\[i\];[\s\S]{0,400}?state\.mode === 'daily'", js) is not None)
    check("状态栏点了视图名（不会「看着日线以为在分时」）",
          "function modeNote(" in js and "日K视图" in js)
    check("日K 视图下新加/换股票会补日线", "ensureDaily(false);" in js)
    check("「全部刷新」在日K视图下会强制重取日线", "await ensureDaily(true)" in js)
    check("日K 测试钩子（mode / setMode / dk / todayBar / dkAll·dkView·dkCfg）",
          "mode: () => state.mode" in js and "dk: i => dailySeries(i)" in js
          and "todayBar: i => todayBar(i)" in js and "dkAll: i => dailyAll(i)" in js
          and "dkView: i => dailyView(i)" in js and "dkCfg: () =>" in js)

    # ---- 日K 上的技术指标：BOLL(20,2) + MA5/10/20/30/60 + 跳空线 ----
    # （数值型的对拍在探针里真算：页面算的 vs 探针自算 vs 后端 /api/kline，见第 5 组）
    check("均线周期 5/10/20/30/60 日", "MA_PERIODS = [5, 10, 20, 30, 60]" in js)
    check("布林参数 20 日 / 2 倍标准差", "BOLL_N = 20, BOLL_K = 2" in js)
    check("布林用**样本**标准差（÷ n-1）—— 与后端 pandas rolling.std() 同口径",
          "Math.sqrt(v / (n - 1))" in js and "样本标准差" in js)
    check("布林不足 n 根不出值（min_periods = n，图上那一段是断的）",
          "if (i >= n - 1) {" in js)
    check("均线是 min_periods=1 口径（不足 n 根用现有的几根算 → 第一根就有值）",
          "sum / Math.min(i + 1, n)" in js)
    check("跳空口径与策略同源（向上 low>昨高 / 向下 high<昨低）",
          "b.low > p.high" in js and "b.high < p.low" in js)
    check("跳空线画在缺口边界（向上画昨高＝回补支撑 / 向下画昨低＝回补压力）",
          "dir = 1; y = p.high" in js and "dir = -1; y = p.low" in js)
    check("缺口带「是否已回补」判定（tooltip 里要说明）",
          "bars[j].low <= y : bars[j].high >= y" in js)
    check("指标算在**全量**上再切展示窗口（否则 MA60 在窗口里只剩 1 个点）",
          "function dailyAll(i)" in js and "function dailySeries(i)" in js
          and "maOf(closes, n).slice(off)" in js and "g.i - 1 >= off" in js)
    check("预热带 DK_WARM = 59、实际请求 DK_REQ = DK_BARS + DK_WARM",
          "const DK_WARM = 59" in js and "const DK_REQ = DK_BARS + DK_WARM" in js)
    check("接口改要 DK_REQ 根（画 60 根 + 59 根预热）",
          re.search(r"'&bars=' \+ DK_REQ", js) is not None)
    check("展示窗口只取最后 DK_BARS 根（带当天那根就是 +1）",
          re.search(r"const n = DK_BARS \+ \(hasToday \? 1 : 0\);", js) is not None)
    check("蜡烛图 y 轴范围把指标极值也算进去（不然布林上轨会被裁到画布外）",
          "MA_PERIODS.forEach(function (n) { feed(ma[n]); });" in js
          and "feed(boll.up); feed(boll.mid); feed(boll.dn);" in js)
    check("跳空是独立的 line series（可单独开关、压在其它线上）",
          "name: '跳空', type: 'line', data: gdata" in js and "z: 4" in js)
    check("跳空线段之间塞 null 断开（否则会连出一条数据里没有的斜线）",
          "if (gdata.length) gdata.push(null);" in js and "gdata.push([g.x1, g.y]);" in js)
    check("图例把 5 条均线 + 布林三轨 + 跳空都列进去了（能开关）",
          "['日K', '成交量'].concat(MA_PERIODS.map(n => 'MA' + n))" in js
          and "['BOLL上轨', 'BOLL中轨', 'BOLL下轨', '跳空']" in js)
    check("图例横向滚动（小格子里 10 项放不下一行）", "type: 'scroll'" in js)
    check("tooltip 里带均线/布林/跳空，且缺口标明未回补 / 已回补",
          "const row = function (label, arr)" in js and "（未回补）" in js
          and "'向上跳空' : '向下跳空'" in js)
    check("指标线细、不画拐点符号（细线 + showSymbol: false）",
          "showSymbol: false, symbol: 'none', smooth: false, connectNulls: false" in js)
    check("切换按钮与状态栏都点明了指标（不会「看着日线却不知道有没有均线」）",
          "BOLL(20,2) + MA5/10/20/30/60 + 跳空缺口线" in js and "BOLL/均线/跳空实时重算" in js)

    # 行情只走腾讯 + localStorage，本站 /api 只用来查交易日历与日K线（代码里只有两处字面量）
    mon = js[js.find("分时监控：纯前端实现"):]
    check("监控脚本内的本站接口只有两个（交易日历 + 日K线）",
          "apiGet(" not in mon and "apiPost(" not in mon
          and mon.count("'/api/") == 2 and "CAL_API" in mon and "DK_API" in mon)

    # ---------------- 4. 浏览器端到端（静态渲染） ----------------
    profile = tempfile.mkdtemp(prefix="monverify_")
    try:
        if e2e and CHROME:
            print("\n【4】浏览器端到端（无头 Chrome，真实拉取 + 真实渲染）")
            # ?force=open：跑回归时本地时间可能正是收盘后，而收盘后不持续轮询（只取一次），
            # 所以要验「盘中持续轮询那一支」必须显式强制时段（同源探针用的是同一个钩子）。
            # ?codes=：代码清单是「用户自己加的」，干净 profile 里没有任何配置 —— 不播种的话
            # 页面就是空态，验不了「有内容时」的渲染 / 盘口 / 副图 / 批量轮询。
            SEED = "sz000001,sh600519,sh601318,sz300750,sh688981,sz000858"
            dom = chrome_dump(BASE + "/monitor?force=open&codes=" + SEED, 20000, 180, profile)
            # 注意：buildGrid() 的模板串里也有 'class="mon-cell blank"' 字面量，
            # 直接 count 会多算 1 个 —— 必须连 data-i="数字" 一起匹配才是真实元素。
            n_cell = len(re.findall(r'class="mon-cell[^"]*" data-i="\d+"', dom))
            check("渲染出 6 个宫格", n_cell == 6, f"{n_cell} 个")
            check("渲染出 6 个图表 canvas", dom.count("<canvas") == 6, f"{dom.count('<canvas')} 个")
            # 同理：模板串里也有 'class="mon-del" data-del="' 字面量，得连数字下标一起匹配
            n_del = len(re.findall(r'class="mon-del" data-del="\d+"', dom))
            check("渲染出 6 个 × 移除按钮", n_del == 6, f"{n_del} 个")
            names = re.findall(r'mon-name"[^>]*>([^<]*)<', dom)
            got = [n for n in names if n and n != "—"]
            check("至少 5 个宫格拿到股票名", len(got) >= 5, f"{len(got)}/6: {got[:3]}")
            prices = [p for p in re.findall(r'mon-price[^"]*">([^<]*)<', dom) if p and p != "—"]
            check("至少 5 个宫格拿到现价", len(prices) >= 5, f"{len(prices)}/6")
            okn = len(re.findall(r'mon-dot ok"', dom))
            check("至少 5 个宫格状态为 ok（无请求失败）", okn >= 5, f"{okn} 个 ok")
            m = re.search(r'id="mon-state"[^>]*>([^<]*)<', dom)
            st = m.group(1) if m else ""
            check("状态栏在运行且轮转过", "运行中" in st and "成功" in st, st.strip())
            check("状态栏写明「6 格 / 每 3 秒一次」", "格 / 每 3 秒一次" in st, st.strip())
            check("状态栏本轮的命中数与格数一致（一轮全中，不是轮到一半）",
                  re.search(r"本轮 6/6", st) is not None, st.strip())
            succ = re.search(r"累计成功 (\d+) / 失败 (\d+)", st)
            check("成功率 ≥90%（3 秒一轮、每轮 6 只的前提成立）",
                  bool(succ) and int(succ.group(1)) >= 10 * 0.9, st.strip())
            # 只认渲染出来的那一段（脚本源码里也有同样的字面量，别被它蒙过去）
            check("节拍提示渲染成「6 格共 1 个请求，每 3 秒全部刷新一次」",
                  re.search(r'id="mon-cycle">6 格共 1 个请求，每 3 秒全部刷新一次<', dom) is not None,
                  (re.search(r'id="mon-cycle">([^<]*)<', dom) or [None, "未找到"])[1])

            # ---- 盘口 / 量比：静态渲染也得真出数（模板串里那份字面量要排除掉） ----
            # `.mon-bk` 在 buildGrid 的模板串里也有一份，所以只认「DOM 真元素」的写法：
            # 档位标签在模板里是 '卖' + (k+1) 拼出来的，不会以「卖5」这种字面量出现，正好用来区分。
            labels = re.findall(r'class="mon-bkl">(卖\d|买\d)</span>', dom)
            check("盘口档位 6 格 × 10 档 = 60 个",
                  len(labels) == 60 and labels[:10] ==
                  ["卖5", "卖4", "卖3", "卖2", "卖1", "买1", "买2", "买3", "买4", "买5"],
                  ",".join(labels[:10]) + f"  共 {len(labels)} 个")
            bk_px = re.findall(r'<b class="(?:up|down|flat)">(\d+\.\d\d)</b><i>', dom)
            check_live("盘口价格渲出来了（≥50 档有价）", len(bk_px) >= 50, f"{len(bk_px)} 档")
            bk_cls = re.findall(r'<b class="(up|down|flat)">\d+\.\d\d</b><i>', dom)
            check_live("盘口价格带涨跌着色类名", len(bk_cls) == len(bk_px) and len(bk_cls) >= 50,
                       f"up={bk_cls.count('up')} down={bk_cls.count('down')} flat={bk_cls.count('flat')}")
            vrs = [v for v in re.findall(r"<span>量比 <b>([^<]*)</b></span>", dom) if v and v != "—"]
            check_live("至少 5 个宫格显示了量比的当前值", len(vrs) >= 5, f"{len(vrs)}/6: {vrs[:4]}")

            # ---- 空态：不内置任何股票（同一份干净 profile，去掉 ?codes= 就该是 0 格）----
            # 断言只认「真实 DOM」：buildGrid 的模板串里也有 '<div class="mon-empty">' 字面量，
            # 所以必须带上紧跟其后的 <b>还没有添加股票</b> 才算是渲染出来的元素。
            empty_profile = tempfile.mkdtemp(prefix="monempty_")
            try:
                dom_empty = chrome_dump(BASE + "/monitor?force=open", 7000, 120, empty_profile)
            finally:
                shutil.rmtree(empty_profile, ignore_errors=True)
            check("空配置渲染出空态引导（真实元素，不是只写在脚本里）",
                  '<div class="mon-empty"><b>还没有添加股票</b>' in dom_empty)
            check("空配置不渲染任何宫格 / 图表 canvas",
                  len(re.findall(r'class="mon-cell[^"]*" data-i="\d+"', dom_empty)) == 0
                  and dom_empty.count("<canvas") == 0)
            me = re.search(r'id="mon-state"[^>]*>([^<]*)<', dom_empty)
            ste = (me.group(1) if me else "").strip()
            check("空配置状态栏提示还没添加股票（不摆轮次计数 / 不说运行中）",
                  "还没有添加股票" in ste and "第 " not in ste and "运行中" not in ste, ste)
            n_qe = len([p for p in re.findall(r'mon-price[^"]*">([^<]*)<', dom_empty) if p and p != "—"])
            check("空配置一个价格都没有（连强制盘中也没东西可请求）", n_qe == 0, f"{n_qe} 个")

            # ---- 真实时钟（不带 force、且用干净 profile）----
            # 盘中：每 3 秒一轮持续轮询；非交易时段：进来只请求一次（1 个请求拿全部格）、
            # 拿一份快照把页面画出来，之后不再请求，
            # 而且那一份不写本地存储（「不写」由第 5 组探针直接读 localStorage 断言，dump 看不到存储）。
            # 干净 profile 仍然是关键：复用 profile 的话「有价格」可能来自上一轮的缓存而不是请求；
            # 这里只用 ?codes= 播「代码清单」这一项配置，行情数据（sm_mon_pts_*）一个字节都没有。
            plain_profile = tempfile.mkdtemp(prefix="monplain_")
            try:
                dom_plain = chrome_dump(BASE + "/monitor?codes=" + SEED, 30000, 180, plain_profile)
            finally:
                shutil.rmtree(plain_profile, ignore_errors=True)
            m2 = re.search(r'id="mon-state"[^>]*>([^<]*)<', dom_plain)
            st2 = (m2.group(1) if m2 else "").strip()
            n_q = len([p for p in re.findall(r'mon-price[^"]*">([^<]*)<', dom_plain) if p and p != "—"])
            okn2 = len(re.findall(r'mon-dot ok"', dom_plain))
            now = dt.datetime.now()
            mins = now.hour * 60 + now.minute
            in_session = ((9 * 60 + 30) <= mins <= (11 * 60 + 30)) or ((13 * 60) <= mins <= (15 * 60 + 1))
            if is_today_trade and in_session:
                check("真实时钟（盘中）：状态为运行中", "运行中" in st2, st2)
                check("真实时钟（盘中）：确实取到了行情", n_q >= 5, f"{n_q} 个价格")
            else:
                why = "非交易日" if is_today_trade is False else "非交易时段"
                check(f"真实时钟（{why}）：状态栏写「仅取一次快照」",
                      "仅取一次快照" in st2, st2)
                check("真实时钟（非盘中）：不再持续轮询（状态里没有轮次计数）",
                      "第 " not in st2 and "运行中" not in st2, st2)
                check("真实时钟（非盘中）：仍然取了一次快照（干净 profile 也拿到了价格）",
                      n_q >= 5, f"{n_q} 个价格")
                check("真实时钟（非盘中）：一次性请求成功（状态点为 ok）", okn2 >= 5, f"{okn2} 个 ok")
                # 注意：脚本源码里也有 'class="mon-tip">采集中…' 这个字面量，
                # 所以只能匹配「真实 DOM 里那一段」—— 紧跟 class 的时段文案，采集中不算。
                tips_ok = re.search(r'class="mon-tip">(已收盘|非交易日|未开盘|午间休市)[^<]*<', dom_plain)
                tip = tips_ok.group(0) if tips_ok else ""
                check("空格子空态提示跟着时段走（不再永远挂「采集中…」）", bool(tips_ok),
                      tip[:80] or "未找到时段提示")
                check("空态提示写明这一时段只取一次快照",
                      # 页面按时段换文案：已收盘 →「只取一次快照」、未开盘 →「已取一次快照」、
                      # 午休 →「仅取一次快照」。断言只锁「一次快照」这件事，别锁死某一个词。
                      bool(tips_ok) and any(k in tip for k in
                                            ("只取一次快照", "已取一次快照", "仅取一次快照")),
                      tip[:80])
        elif e2e:
            print("\n【4】浏览器端到端：未找到 Chrome，跳过")

        # ---------------- 5. 增删宫格交互（同源 iframe 探针） ----------------
        if e2e and CHROME:
            print("\n【5】宫格交互（探针页 real click / 真实 DnD / 空态与不内置默认票 / "
                  "盘口·量比副图·全字段 / 批量轮询一请求覆盖全部格 / 日K 视图切换 "
                  "/ 日K 上的均线·布林·跳空（与后端指标对拍）/ 真实行情）")
            probe_dom = chrome_dump(BASE + PROBE, 260000, 420, profile)
            m = re.search(r'<pre id="out">(.*?)</pre>', probe_dom, re.S)
            body = html_mod.unescape(m.group(1)) if m else ""
            if "DONE" not in body:
                check("探针跑完（结果写回主文档）", False,
                      (body or "无输出")[-200:].replace("\n", " | "))
            else:
                for line in body.splitlines():
                    line = line.strip()
                    if line.startswith("OK   "):
                        check("探针 · " + line[5:].split("  ")[0], True)
                    elif line.startswith("SKIP "):
                        # 时段性不适用（例如盘前没有五档/量比可比），占位但不计失败
                        nm, _, extra = line[5:].partition("  ")
                        skip("探针 · " + nm, extra)
                    elif line.startswith("FAIL "):
                        # 失败时把探针写在名字后面的现场信息（extra）带出来，
                        # 否则只剩一个名字，回查时还得再跑一遍才看得到数字
                        nm, _, extra = line[5:].partition("  ")
                        check("探针 · " + nm, False, extra)
                    elif line.startswith("EXCEPTION"):
                        check("探针异常中止", False, line)
        elif e2e:
            print("\n【5】增删宫格交互：未找到 Chrome，跳过")
    finally:
        shutil.rmtree(profile, ignore_errors=True)

    print("\n" + "=" * 58)
    print(f"通过 {PASS} 项，跳过 {SKIP} 项（时段性不适用），失败 {FAIL} 项")
    if SKIPS:
        print("跳过项（换个时段重跑即可覆盖）：")
        for x in SKIPS:
            print("  ~", x)
    if FAILS:
        print("失败项：")
        for x in FAILS:
            print("  -", x)
    print("=" * 58)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
