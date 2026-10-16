#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
历史回测记录「重测」回归（静态口径 + 素材契约 + 无头 Chrome 真点一次重测）

为什么需要它：「重测」看着只是多一个按钮，真正会踩的坑全在**取哪份配置**上 ——
  · 取表单当前值 = 用户改了池子/区间之后再点，跑出来根本不是那条记录的东西；
  · 取池子引用（pool:<id>）而不取快照 = 池子后来改过，重测悄悄换了口径；
  · 引用已被删掉的选股策略时不拦（后端会 400）或静默退回全市场（数字完全不可信）；
  · 参考记录的买卖策略被删了还硬跑；
  · 「开始回测」和「重测」各写一份提交代码 → 一边改了另一边落后。
再加上一个界面事实：操作列从 2 个按钮变 3 个，可能把 14 列的表顶破卡片。
这些都得真点一次才知道，所以分三段：

  【1】静态口径 —— 按钮渲染 + 唯一提交路径 + 重测的配置来源是「记录快照」而非表单 +
                    策略/快照缺失时明确拒绝 + pool_id 已删时的兜底 + 名字后缀
  【2】素材契约 —— GET /api/backtest/runs/<id> 必须给全重测要吃的东西
                    （strategy_id / start_date / end_date / universe_spec / params）
  【3】浏览器实测 —— 同源 iframe 里真开 /backtest、真点「重测」，但把提交拦下来（换掉
                    同源的 window.apiPost）：把表单改乱后再点，发出去的仍是**记录里**的
                    区间/资金/持仓/费率，选股条件用当时那份快照 —— 这是「取哪份配置」的
                    直接证据；再验「策略已删 → 拒绝」「池已删 → 去掉 pool_id 照跑」
  【4】端到端真跑 —— 按同样的语义构造 body，真提交一次、真等到终态：新记录的四项与源记录
                    一致、收益同口径复现；收尾把这条测试记录删掉（不污染历史）

跑法：
    C:/Users/ming/.workbuddy/binaries/python/envs/stockmining/Scripts/python.exe tools/verify_bt_retest.py
    加 --no-e2e 只跑【1】【2】（不用起浏览器）
    加 --src 110 指定拿来重测的源记录（默认挑一条区间最短的）
    加 --no-run 跳过【4】（【3】照样跑，浏览器那段是秒级的）
依赖：服务在 127.0.0.1:8777；本机 Chrome（找不到就把【3】标 SKIP，不算失败）。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BASE = "http://127.0.0.1:8777"
PROBE_REL = "/static/_probe_bt_retest.html?run=1&src="
PROBE_HTML = os.path.join(ROOT, "static", "_probe_bt_retest.html")
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


def req(method: str, path: str, data: dict | None = None, timeout: int = 60) -> dict:
    payload = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(BASE + path, data=payload, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(r, timeout=timeout).read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())


CHROME = None
for _c in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
           r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
           os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe")):
    if os.path.exists(_c):
        CHROME = _c
        break


def chrome_probe_out(url: str, budget_ms: int, timeout: int, profile: str) -> str:
    """渲染探针页并返回 <pre id="out"> 里的文本。"""
    out = os.path.join(tempfile.gettempdir(), "_bt_retest_dom.html")
    try:
        with open(out, "w", encoding="utf-8") as fh:
            subprocess.run(
                [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
                 "--user-data-dir=" + profile, "--dump-dom",
                 f"--virtual-time-budget={budget_ms}", "--window-size=1500,2200", url],
                stdout=fh, stderr=subprocess.DEVNULL, timeout=timeout)
        dom = open(out, encoding="utf-8", errors="ignore").read()
    finally:
        try:
            os.remove(out)
        except OSError:
            pass
    m = re.search(r'<pre id="out">([\s\S]*?)</pre>', dom)
    return m.group(1) if m else ""


def pick_src(runs: list[dict]) -> int | None:
    """挑一条**跑得快**的记录来重测：池子收紧过（选股条件多 ⇒ 标的小）+ 区间短的最优。
    拿不到合适的就是列表里第一条 done。"""
    done = [r for r in runs if r.get("status") == "done"]
    if not done:
        return None
    def score(r):
        days = 0
        try:
            days = (int(str(r["end_date"])[:4]) - int(str(r["start_date"])[:4]))
        except Exception:  # noqa: BLE001
            days = 9
        return (days, -r["id"])
    return sorted(done, key=score)[0]["id"]


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# 快照里**真正决定选股行为**的那几个键。
# ⚠ 别拿整串 JSON 直接比：快照里还夹着 `label` / `derived` / `preset_id` / `pool_name` 这类
#   **元信息**，而且 `universe.describe()` 后来加过一个键（`timeframe`，2026-10-10）——
#   于是「新记录 vs 历史冻结快照」永远不相等，报的还是「选股条件不一致」，纯属误导。
#   要比就比这几个：kind / base / where / filt / pool_id。
#   ⚠ 2026-10-10 起 `pool_exit` 也**从这份清单里去掉了** —— 它已彻底移除（选股策略不再
#     承担卖出语义），历史快照里那份旧键不该再参与比对。
_SPEC_KEYS = ("kind", "base", "where", "filt", "pool_id")


def _spec_core(spec) -> dict:
    spec = spec or {}
    return {k: spec.get(k) for k in _SPEC_KEYS}


def build_retest_body(detail: dict, sids: set[int], pids: set[int]) -> dict | None:
    """Python 侧按**前端同一套语义**构造重测请求体（即 templates/backtest.html 的 retestBody）。

    ⚠ 两边是同一套规则的两个实现（一个跑在浏览器、一个跑在回归里），改了一边就要改另一边 ——
      所以这里逐行对着模板写：策略必须在库里、条件取 `universe_spec` 快照、
      快照里的 pool_id 只在池还在时保留。端到端那段拿它提交，等于替前端的构造结果背书。
    """
    sid = int(detail.get("strategy_id") or 0)
    if not sid or sid not in sids:
        return None
    spec = detail.get("universe_spec") or {}
    if not spec:
        return None
    if spec.get("kind") == "cond":
        universe: object = {"base": spec.get("base") or "all",
                            "where": list(spec.get("where") or [])}
        if spec.get("filt"):
            universe["filt"] = spec["filt"]
        pid = int(spec.get("pool_id") or 0)
        if pid and pid in pids:
            universe["pool_id"] = pid
    else:
        universe = spec.get("base") or "all"
    p = detail.get("params") or {}
    return {
        "strategy_id": sid,
        "universe": universe,
        "start": str(detail.get("start_date"))[:10],
        "end": str(detail.get("end_date"))[:10],
        "params": {k: p.get(k) for k in ("init_cash", "max_positions", "position_pct",
                                        "commission", "stamp_tax", "slippage")},
        "name": ((detail.get("name") or "回测") + " · 重测")[:160],
    }


def main() -> int:
    e2e = "--no-e2e" not in sys.argv
    src = None
    if "--src" in sys.argv:
        try:
            src = int(sys.argv[sys.argv.index("--src") + 1])
        except (IndexError, ValueError):
            src = None

    # ------------------------------------------------------------------ 1. 静态口径
    print("【1】静态口径（按钮 + 取哪份配置）")
    code = open(BT_HTML, encoding="utf-8").read()

    check("每行都渲染「重测」按钮（data-retest = 该行 #id）",
          'data-retest="${r.id}"' in code)
    check("按钮是「查看 / 重测 / 删」这个顺序（重测夹在中间，别跟删除挨着）",
          re.search(r'data-view="\$\{r\.id\}">查看</button>\s*\n\s*'
                    r'<button class="btn sm" data-retest=', code) is not None)
    check("「重测」用普通样式（danger 是删除专用，别混）",
          re.search(r'data-retest="\$\{r\.id\}" title="[^"]*">重测</button>', code) is not None)
    check("title 说清是「原样再跑一次」（不然会被当成「对比」）",
          '原样再跑一次' in code)
    # ⚠ 只断言「绑的是 retestRun 这个函数」这个行为，别钉死外面那层包装的写法 ——
    #   按钮为了防连点会套 `guardClick(b, ...)`，写法一变这条就会假红。
    check("handler 绑到 retestRun（不是内联拼一坨）",
          re.search(r"\$\$\('\[data-retest\]'\)\.forEach\(\s*b\s*=>\s*b\.onclick\s*=\s*"
                    r"(?:guardClick\(\s*b\s*,\s*)?\(\)\s*=>\s*retestRun\(b\.dataset\.retest\)",
                    code) is not None)

    check("重测的配置来源是**记录**（universe_spec / params / strategy_id），不是表单",
          re.search(r"function retestBody\(run\)[\s\S]{0,400}?run\.universe_spec", code) is not None
          and "run.params" in code and "run.strategy_id" in code)
    check("买卖策略已不存在 → 明确拒绝，不静默换一个策略跑",
          re.search(r"const sid = Number\(run\.strategy_id \|\| 0\);[\s\S]{0,220}?"
                    r"toast\('该记录用的买卖策略已不存在[\s\S]{0,80}?return null;", code) is not None)
    check("没有条件快照（老记录反查不到）→ 明确拒绝",
          re.search(r"if \(!spec\) \{ toast\('该记录没有存下选股条件快照[\s\S]{0,60}?return null;", code)
          is not None)
    check("快照里的 pool_id：池还在才带上（池已删就去掉，免得后端按「引用已删除策略」拒掉）",
          re.search(r"if \(pid && POOLS\.some\(p => Number\(p\.id\) === pid\)\) universe\.pool_id = pid;",
                    code) is not None)
    check("POOLS 清单进了页面（判「池还在不在」要用）",
          "let POOLS = [];" in code and "POOLS = pools || [];" in code)
    check("「开始回测」与「重测」共用**同一条**提交路径（submitBacktest）",
          code.count("apiPost('/api/backtest/start'") == 1
          and code.count("await submitBacktest(") == 2)
    check("重测记录名带「 · 重测」后缀，且按 name 上限截断",
          re.search(r"\(\(run\.name \|\| '回测'\) \+ ' · 重测'\)\.slice\(0, 160\)", code) is not None)
    check("跑着的那条不给重测（等它跑完再说）",
          re.search(r"if \(run\.status === 'running'\) return toast\('#' \+ id \+ ' 还在跑", code) is not None)

    blocks = re.findall(r"<script(?![^>]*src=)[^>]*>([\s\S]*?)</script>", code)
    big = max(blocks, key=len) if blocks else ""
    js = os.path.join(tempfile.gettempdir(), "_bt_inline_retest.js")
    with open(js, "w", encoding="utf-8") as fh:
        fh.write(big)
    if os.path.exists(NODE):
        r = subprocess.run([NODE, "--check", js], capture_output=True, text=True)
        check(f"backtest.html 内联脚本语法（{len(big)} 字符）", r.returncode == 0,
              (r.stderr or "").strip()[:140])
    else:
        skip("backtest.html 内联脚本语法", "找不到 node")

    check("探针页带 ?run=1 门禁（它会真提交一次回测并删掉那条测试记录）",
          os.path.exists(PROBE_HTML)
          and "qs.get('run') !== '1'" in open(PROBE_HTML, encoding="utf-8").read())
    pjs = os.path.join(tempfile.gettempdir(), "_bt_probe_retest.js")
    if os.path.exists(PROBE_HTML):
        pblocks = re.findall(r"<script(?![^>]*src=)[^>]*>([\s\S]*?)</script>",
                             open(PROBE_HTML, encoding="utf-8").read())
        with open(pjs, "w", encoding="utf-8") as fh:
            fh.write("\n;\n".join(pblocks))
        if os.path.exists(NODE):
            r = subprocess.run([NODE, "--check", pjs], capture_output=True, text=True)
            check("探针页脚本语法", r.returncode == 0, (r.stderr or "").strip()[:140])

    # ------------------------------------------------------------------ 2. 素材契约
    print("\n【2】素材契约（记录里得存着够重测用的东西）")
    runs: list[dict] = []
    try:
        runs = req("GET", "/api/backtest/runs?limit=60").get("data") or []
        check("拿得到历史回测记录", len(runs) > 0, f"{len(runs)} 条")
        strategies = req("GET", "/api/strategies").get("data") or []
        sids = {int(s["id"]) for s in strategies}
        check("拿得到买卖策略清单", len(sids) > 0, f"{len(sids)} 条")

        probe_ids = [r["id"] for r in runs[:8]]
        bad: list[str] = []
        for rid in probe_ids:
            d = (req("GET", f"/api/backtest/runs/{rid}").get("data")) or {}
            for k in ("strategy_id", "start_date", "end_date", "universe_spec", "params"):
                if d.get(k) in (None, "", {}):
                    bad.append(f"#{rid}.{k}")
        check("最近 8 条记录都带 strategy_id / 区间 / 条件快照 / 参数", not bad,
              "、".join(bad[:5]) if bad else "齐全")

        # 条件池的快照必须是**展开过**的（base/where/filt），否则重测拿不到条件
        wid = [r["id"] for r in runs if r.get("universe")][:8]
        shapes: list[str] = []
        for rid in wid:
            d = (req("GET", f"/api/backtest/runs/{rid}").get("data")) or {}
            s = d.get("universe_spec") or {}
            if s.get("kind") == "cond":
                shapes.append("cond:" + str(len(s.get("where") or [])))
                if not (s.get("base") and "filt" in s):
                    bad.append(f"#{rid}.snapshot")
            else:
                shapes.append("static")
        check("条件池快照是展开过的（含 base / where / filt）", not bad,
              "、".join(shapes[:6]) if shapes else "（近期全是静态池）")

        # 重测能被拒的两种情况，在库里现在是**有**素材的（策略都在）
        miss = [r["id"] for r in runs[:8] if int(r.get("strategy_id") or 0) not in sids]
        check("最近 8 条记录的买卖策略都还在（重测不会被拒）", not miss,
              "缺策略：" + "、".join(map(str, miss)) if miss else "都在")
    except Exception as e:
        check("素材契约", False, f"{type(e).__name__}: {e}（服务起了吗？）")

    if src is None:
        src = pick_src(runs) if runs else None
    print(f"  （本次拿来重测的源记录：#"
          f"{src if src else '—'}）")

    # ------------------------------------------------------------------ 3. 浏览器实测
    if e2e and CHROME and src:
        print("\n【3】浏览器实测（真点一次「重测」，提交被探针拦下）")
        profile = os.path.join(tempfile.gettempdir(), "_bt_retest_profile")
        shutil.rmtree(profile, ignore_errors=True)
        try:
            body = chrome_probe_out(BASE + PROBE_REL + str(src), 180000, 300, profile)
            m = re.search(r"\{[\s\S]*\}", body)
            data = None
            if m:
                try:
                    data = json.loads(m.group(0))
                except Exception:  # noqa: BLE001
                    data = None
            if not isinstance(data, dict) or not data.get("checks"):
                check("探针返回可解析结果", False, (body or "（探针没输出）")[:300].replace("\n", " "))
            else:
                check("探针跑完了（不是半路超时）", bool(data.get("done")),
                      "done=" + str(data.get("done")))
                for c in data["checks"]:
                    check("探针 · " + c["name"], bool(c.get("ok")), c.get("extra", ""))
        except Exception as e:
            check("浏览器实测执行完成", False, f"{type(e).__name__}: {e}")
        finally:
            shutil.rmtree(profile, ignore_errors=True)
    else:
        skip("浏览器实测", "无 Chrome / --no-e2e / 没有可用的源记录")

    # ------------------------------------------------------------------ 4. 端到端真跑
    if e2e and "--no-run" not in sys.argv and src:
        print("\n【4】端到端真跑（真提交一次重测，等它跑完再删）")
        made = None
        try:
            detail = req("GET", f"/api/backtest/runs/{src}", timeout=30).get("data") or {}
            strategies = req("GET", "/api/strategies").get("data") or []
            sids = {int(s["id"]) for s in strategies}
            pools = req("GET", "/api/pools").get("data") or []
            pids = {int(p["id"]) for p in pools}
            b = build_retest_body(detail, sids, pids)
            check(f"按记录 #{src} 能构造出重测请求体", b is not None,
                  "（策略已删 ⇒ 前端会拦下来，属预期）")
            if not b:
                skip("端到端真跑", "源记录的买卖策略不在库里，前端会拒绝重测")
            else:
                r = req("POST", "/api/backtest/start", b)
                made = (r.get("data") or {}).get("run_id")
                check("重测请求被接受（后端没报错）", r.get("ok") is True and bool(made),
                      (str(r.get("error"))[:80] if not r.get("ok") else f"run_id={made}"))
                if made:
                    fin = None
                    t0 = time.time()
                    while time.time() - t0 < 600:
                        d = req("GET", f"/api/backtest/runs/{made}", timeout=30).get("data") or {}
                        if d.get("status") != "running" and not d.get("writing"):
                            fin = d
                            break
                        time.sleep(3)
                    check("重测跑到了终态", fin is not None,
                          (f"{fin.get('status')} 用时 {time.time()-t0:.0f}s" if fin
                           else "600s 超时"))
                    if fin:
                        nd, sd = fin, detail
                        check("新记录名带「 · 重测」后缀", (nd.get("name") or "").endswith(" · 重测"),
                              nd.get("name"))
                        check("买卖策略 / 区间与源记录一致",
                              int(nd.get("strategy_id") or 0) == int(sd.get("strategy_id") or 0)
                              and str(nd.get("start_date"))[:10] == str(sd.get("start_date"))[:10]
                              and str(nd.get("end_date"))[:10] == str(sd.get("end_date"))[:10],
                              f"#{nd.get('strategy_id')} {nd.get('start_date')}~{nd.get('end_date')}")
                        core_nd, core_sd = (_spec_core(nd.get("universe_spec")),
                                            _spec_core(sd.get("universe_spec")))
                        check("选股条件快照与源记录一致（比 kind/base/where/filt/pool_id）",
                              core_sd.get("kind") is not None and core_nd == core_sd,
                              nd.get("universe") or "")
                        keys = ["init_cash", "max_positions", "position_pct",
                                "commission", "stamp_tax", "slippage"]
                        diff = [k for k in keys
                                if _num((nd.get("params") or {}).get(k))
                                != _num((sd.get("params") or {}).get(k))]
                        check("运行时参数与源记录一致（资金 / 持仓 / 费率 / 滑点）", not diff,
                              "不一致：" + "、".join(diff) if diff else "六项全同")
                        if nd.get("status") == "done":
                            a = _num(((nd.get("metrics") or {}) or {}).get("total_return"))
                            b0 = _num(((sd.get("metrics") or {}) or {}).get("total_return"))
                            check("收益同口径复现（差 < 0.05pt；引擎与数据都没变就该完全一致）",
                                  abs(a - b0) < 0.05,
                                  f"重测 {a}% vs 原 {b0}%")
                            tr = req("GET", f"/api/backtest/runs/{made}/trades",
                                     timeout=60).get("data")
                            check("重测记录能点开看逐笔明细（不是一条空壳记录）",
                                  isinstance(tr, list) and len(tr) > 0,
                                  f"{len(tr) if isinstance(tr, list) else '?'} 笔")
                        else:
                            check("重测失败时落下了原因（不是静默躺着）",
                                  bool(nd.get("progress_msg")), str(nd.get("progress_msg"))[:60])
        except Exception as e:
            check("端到端真跑", False, f"{type(e).__name__}: {e}")
        finally:
            # 收尾：测试记录不留库（先等它到终态，别让后台线程往一条已删的行上写）
            if made:
                try:
                    for _ in range(40):
                        d = req("GET", f"/api/backtest/runs/{made}", timeout=30).get("data") or {}
                        if d.get("status") != "running" and not d.get("writing"):
                            break
                        time.sleep(3)
                    req("POST", f"/api/backtest/runs/{made}/delete", {})
                    left = [r["id"] for r in (req("GET", "/api/backtest/runs?limit=60").get("data") or [])]
                    check("收尾把测试记录删干净了", made not in left, f"#{made}")
                except Exception as e:  # noqa: BLE001
                    check("收尾删除测试记录", False, f"{type(e).__name__}: {e}")
    elif "--no-run" in sys.argv:
        skip("端到端真跑", "--no-run")
    elif e2e:
        skip("端到端真跑", "没有可用的源记录")
    else:
        skip("端到端真跑", "--no-e2e")

    print(f"\n{'=' * 62}\n历史回测记录「重测」回归：{PASS} 通过 / {FAIL} 失败 / {SKIP} 跳过")
    if FAILS:
        print("失败项：")
        for x in FAILS:
            print("  -", x)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
