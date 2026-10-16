#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
提交类按钮「防连点」回归（静态口径 + 无头 Chrome 真点两次）

为什么需要它：提交类按钮被点两下 = 两条记录 / 两个后台任务，而 `fetch` 不会自动去重。
  · 「开始回测」「重测」双击 → 历史记录里多一条一模一样的；重测还会连带跑两遍回测
  · 同步那六个按钮（全量拉取几分钟）双击 → 两个同步任务互相抢连接池
  · 试跑 / 选股 / AI 分析双击 → 两遍全市场扫描、两条 SSE 流
真正会踩的坑：
  ① 只在「点的时候」判一次，请求返回后没解锁 → 一次报错就把按钮永久锁死（比不锁更糟）
  ② 用「延时执行」的 debounce 代替「立刻响应 + 忽略重复」→ 页面显得卡
  ③ 锁得太宽：把本地瞬时按钮（恢复默认 / 翻页 / 排序）也锁上 → 用户快速翻页会被吃掉
  ④ 只锁按钮、不锁「整组」：长任务（同步）点一次后要等几分钟，用户不知道在跑又点别的

分三段：
  【1】实现不变量 —— app.js 里那道锁确实三样都有（在飞忽略 / 600ms 窗口 / 置灰 + finally 解锁）
  【2】接线表   —— 每个提交类按钮都挂了 guardClick；本地瞬时按钮**没有**被误锁
  【3】浏览器实测 —— 探针 static/_probe_guard_click.html：单元验证三道保护 + 在同源 iframe
                    里真点「开始回测 / 重测」各两次（apiPost 被换掉，只数请求数、不写库）

跑法：
    C:/Users/ming/.workbuddy/binaries/python/envs/stockmining/Scripts/python.exe tools/verify_guard_click.py
    加 --no-e2e 只跑【1】【2】（不用起浏览器）
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
PROBE_REL = "/static/_probe_guard_click.html"
PROBE_HTML = os.path.join(ROOT, "static", "_probe_guard_click.html")
PAGES_PROBE_REL = "/static/_probe_guard_pages.html"

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


def read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def has(rel: str, needle: str) -> bool:
    return needle in read(rel)


def req_ok() -> bool:
    try:
        urllib.request.urlopen(BASE + "/api/config", timeout=6).read()
        return True
    except Exception:  # noqa: BLE001
        return False


CHROME = None
for _c in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
           r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
           os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe")):
    if os.path.exists(_c):
        CHROME = _c
        break


def chrome_probe_out(url: str, budget_ms: int, timeout: int, profile: str) -> str:
    """渲染探针页并返回 <pre id="out"> 里的文本。"""
    out = os.path.join(tempfile.gettempdir(), "_guard_click_dom.html")
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


# --------------------------------------------------------------------------- 【1】实现不变量
def part_impl() -> None:
    print("\n【1】实现不变量（app.js 的这道锁）")
    src = read("static/js/app.js")
    i = src.find("window.guardClick")
    check("app.js 定义了 window.guardClick", i >= 0)
    if i < 0:
        return
    # 常量与那段「为什么不用 debounce」的注释都在函数**之前** → 从段落开头切
    j = src.find("提交类按钮防连点")
    block = src[max(0, j - 40):i + 1300]
    body = src[i:i + 1300]

    check("① 在飞保护：靠 dataset.busy 判重入（不是只看时间）",
          "dataset.busy" in body and "'1'" in body)
    check("② 连点窗口：真的有个毫秒数在挡（LOCK_MS = 600）",
          bool(re.search(r"const LOCK_MS = \d+;", block)) and "lockTs" in body)
    check("③ 置灰反馈：锁住期间 el.disabled = true", "el.disabled = true" in body)
    check("解锁写在 finally 里（否则一次报错 = 按钮永久锁死）",
          re.search(r"\}\s*finally\s*\{[\s\S]{0,200}el\.disabled = wasDisabled", body) is not None)
    check("恢复的是「点之前」的 disabled 值（本来就灰的按钮不会被点亮）",
          "const wasDisabled = el.disabled" in body)
    check("注释里点明了「不要用延时执行的 debounce」这条口径",
          "debounce" in block and "立刻响应" in block)


# --------------------------------------------------------------------------- 【2】接线表
# 每个提交类按钮必须走 guardClick：直接写 onclick = async/() => 的，就是漏了
GUARDED = [
    ("templates/backtest.html", "$('#btn-run').onclick = guardClick($('#btn-run')"),
    ("templates/backtest.html", "$('#btn-reload-runs').onclick = guardClick"),
    ("templates/backtest.html", "$('#btn-export').onclick = guardClick($('#btn-export')"),
    ("templates/backtest.html", "$$('[data-view]').forEach(b=>b.onclick=guardClick"),
    ("templates/backtest.html", "$$('[data-retest]').forEach(b=>b.onclick=guardClick"),
    ("templates/backtest.html", "$$('[data-del]').forEach(b=>b.onclick=guardClick"),
    ("templates/backtest.html", "$$('[data-kline]', $('#trades-list')).forEach(b => b.onclick = guardClick"),
    ("templates/screen.html", "$('#btn-screen').onclick = guardClick($('#btn-screen'), runScreen)"),
    ("templates/screen.html", "$('#btn-history').onclick = guardClick"),
    ("templates/screen.html", "$('#btn-export-screen').onclick = guardClick"),
    ("templates/screen.html", "$$('[data-detail]').forEach(b=>b.onclick=guardClick"),
    ("templates/screen.html", "$$('[data-kline]', $('#sc-result')).forEach(b=>b.onclick=guardClick"),
    ("templates/screen.html", "$$('[data-ai]', $('#sc-result')).forEach(b=>b.onclick=guardClick"),
    ("templates/pool.html", "$('#btn-validate').onclick = guardClick"),
    ("templates/pool.html", "$('#btn-save').onclick = guardClick($('#btn-save')"),
    ("templates/pool.html", "$('#btn-save-as').onclick = guardClick"),
    ("templates/pool.html", "$('#btn-del').onclick = guardClick"),
    ("templates/pool.html", "$('#btn-builtins').onclick = guardClick"),
    ("templates/pool.html", "$('#btn-test').onclick = guardClick($('#btn-test'), test)"),
    ("templates/pool.html", "$$('#pool-list [data-del]').forEach(b => b.onclick = guardClick"),
    ("templates/pool.html", "$$('[data-kline]', box).forEach(b => b.onclick = guardClick"),
    ("templates/strategy.html", "$('#btn-validate').onclick = guardClick"),
    ("templates/strategy.html", "$('#btn-save').onclick = guardClick($('#btn-save')"),
    ("templates/strategy.html", "$('#btn-save-as').onclick = guardClick"),
    ("templates/strategy.html", "$('#btn-builtins').onclick = guardClick"),
    ("templates/strategy.html", "$$('#strat-list [data-del]').forEach(b => b.onclick = guardClick"),
    ("templates/data.html", "$('#btn-sync').onclick = guardClick"),
    ("templates/data.html", "$('#btn-base').onclick = guardClick"),
    ("templates/data.html", "$('#btn-money').onclick = guardClick"),
    ("templates/data.html", "$('#btn-holders').onclick = guardClick"),
    ("templates/data.html", "$('#btn-futures').onclick = guardClick"),
    ("templates/data.html", "$('#btn-concept').onclick = guardClick"),
    ("templates/data.html", "$('#btn-newconcept').onclick = guardClick"),
    ("templates/data.html", "$('#btn-weekly').onclick = guardClick"),
    ("templates/data.html", "$('#btn-cancel').onclick = guardClick"),
    ("templates/data.html", "$('#btn-reload-uni').onclick = guardClick"),
    ("templates/settings.html", "$('#btn-save').onclick = guardClick($('#btn-save')"),
    ("templates/settings.html", "$('#btn-test').onclick = guardClick($('#btn-test')"),
    ("templates/settings.html", "$('#btn-activate').onclick = guardClick"),
    ("templates/settings.html", "$('#btn-del').onclick = guardClick($('#btn-del')"),
    ("templates/settings.html", "$('#btn-tpl-save').onclick = guardClick"),
    ("templates/settings.html", "$('#btn-tpl-default').onclick = guardClick"),
    ("templates/settings.html", "$('#btn-tpl-del').onclick = guardClick"),
    ("templates/settings.html", "$$('[data-act]', box).forEach(b => b.onclick = guardClick"),
    ("templates/stock.html", "$('#btn-load').onclick = guardClick($('#btn-load'), load)"),
    ("templates/stock.html", "$('#btn-ai').onclick = guardClick($('#btn-ai')"),
    ("templates/stock.html", "$('#ai-card-hist').onclick = guardClick"),
    ("templates/index.html", "$('#hot-refresh').onclick = guardClick"),
    ("templates/index.html", "$('#wf-check').onclick = guardClick"),
    ("templates/index.html", "$('#nc-refresh').onclick = guardClick"),
    ("templates/watch.html", "$('#btn-new-group').onclick = guardClick"),
    ("templates/watch.html", "$('#btn-add-stock').onclick = guardClick"),
    ("templates/watch.html", "$('#btn-check').onclick = guardClick"),
    ("templates/watch.html", "$('#al-add').onclick = guardClick"),
    ("templates/watch.html", "$$('#grp-list [data-del]').forEach(b => b.onclick = guardClick"),
    ("templates/watch.html", "$$('#stk-list [data-rm]').forEach(b => b.onclick = guardClick"),
    ("templates/watch.html", "$$('#al-list [data-del]').forEach(b => b.onclick = guardClick"),
    ("templates/watch.html", "$$('#al-list [data-tog]').forEach(b => b.onclick = guardClick"),
    ("templates/futures_kline.html", "guardClick(document.getElementById('fk-refresh'), load)"),
    ("static/js/ai.js", "$$('[data-view]', box).forEach(b => b.onclick = guardClick"),
    ("static/js/ai.js", "$$('[data-del]', box).forEach(b => b.onclick = guardClick"),
    ("static/js/pool.js", "addEventListener('click', guardClick(bTest,"),
    ("static/js/pool.js", "addEventListener('click', guardClick(bSave,"),
]

# 本地瞬时动作：**不该**被锁（锁了会让快速翻页/排序/恢复默认变得黏手）
LOCAL_UNLOCKED = [
    ("templates/backtest.html", "$('#btn-reset').onclick = () => {"),
    ("templates/backtest.html", "$$('[data-page]', $('#trades-list')).forEach(b => b.onclick = () => gotoPage("),
    ("templates/backtest.html", "$$('th[data-sort]', $('#trades-list')).forEach(th => th.onclick = () => toggleSort("),
    ("templates/backtest.html", "$('#btn-draft-clear')"),
    ("templates/stock.html", "$('#st-freq').addEventListener('change'"),
    # K线卡片上的「日K / 周K」分段按钮：与旁边那个周期下拉同一档（切视图）。
    # 来回点两下比对日线/周线是正常操作，锁了会黏手。
    ("templates/stock.html", "$$('#st-freq-seg button').forEach(b => b.onclick = () => setFreq("),
    # 我的关注页：换「最近 N 个交易日」只是本地重查一次，锁了会黏手
    ("templates/watch.html", "$('#feed-days').onchange = loadFeed;"),
]


def part_wiring() -> None:
    print(f"\n【2】接线表（{len(GUARDED)} 处提交类按钮必须挂上）")
    miss = [(f, n) for f, n in GUARDED if not has(f, n)]
    check(f"全部 {len(GUARDED)} 处提交类按钮都走 guardClick", not miss,
          "" if not miss else "漏了：" + "; ".join(f"{f} :: {n[:46]}" for f, n in miss[:6]))

    # 反向：不能出现「同一个按钮既挂 guardClick 又保留裸赋值」的重复绑定
    dupes = []
    for f in sorted({f for f, _ in GUARDED}):
        src = read(f)
        # 形如 $('#btn-x').onclick = 出现两次以上 = 一处挂了、一处没挂（后者会覆盖前者）
        for sel in re.findall(r"\$\('#(btn-[\w-]+)'\)\.onclick\s*=", src):
            if src.count(f"$('#{sel}').onclick") > 1:
                dupes.append(f"{f}::{sel}")
    check("没有「同一个按钮被绑两次」的实现（后写的会覆盖 guardClick）", not dupes,
          "重复：" + ", ".join(dupes[:6]))

    print(f"\n【2b】本地瞬时动作别一起锁上（{len(LOCAL_UNLOCKED)} 处）")
    over = []
    for f, n in LOCAL_UNLOCKED:
        src = read(f)
        if n not in src:
            over.append(f"{f} :: 找不到 {n[:40]}")
    check("本地瞬时按钮仍是直接绑定（没被误加锁）", not over, "; ".join(over[:4]))
    check("monitor 页全是本地动作（localStorage + 站外行情），整页不该出现 guardClick",
          "guardClick" not in read("templates/monitor.html"))
    check("今日期货列表的「刷新」自带置灰（它本来就防住了，不必再套一层）",
          "b.disabled = true" in read("static/js/futures.js"))

    # 长任务那组：任务在跑期间必须锁住**整组**，不是只锁被点的那一个
    d = read("templates/data.html")
    check("同步按钮有「整组锁」：SYNC_BTNS + lockSyncBtns()", "SYNC_BTNS" in d and "function lockSyncBtns" in d)
    check("六个同步按钮都在整组锁名单里",
          all(b in d for b in ("'#btn-sync'", "'#btn-base'", "'#btn-money'",
                               "'#btn-holders'", "'#btn-futures'", "'#btn-weekly'")))
    check("任务启动时锁住整组（startWatch 里）",
          re.search(r"lockSyncBtns\(true\);", d) is not None)
    check("任务结束（成功与失败两条路）都放开整组",
          d.count("lockSyncBtns(false);") >= 2, f"放开 {d.count('lockSyncBtns(false);')} 处")

    _sh = read("templates/stock.html")
    check("回车与按钮共用一条入口（stock 页回车走按钮，两条入口不会一紧一松）",
          "$('#btn-load').click()" in _sh)
    check("回车绑在整张「查询条件」卡上、且只有这一处（不是逐框各绑一套）",
          "$('#st-card-query').addEventListener('keydown'" in _sh
          and _sh.count("'keydown'") == 1,
          "keydown 绑了 %d 处" % _sh.count("'keydown'"))


# --------------------------------------------------------------------------- 【3】浏览器实测
def part_browser(e2e: bool) -> None:
    print("\n【3】浏览器实测（单元三道保护 + 真点两次按钮）")
    if not e2e:
        skip("浏览器实测", "--no-e2e")
        return
    if not CHROME:
        skip("浏览器实测", "找不到本机 Chrome")
        return
    if not req_ok():
        skip("浏览器实测", "服务没在 127.0.0.1:8777")
        return

    profile = os.path.join(tempfile.gettempdir(), "_guard_click_profile")
    shutil.rmtree(profile, ignore_errors=True)
    try:
        body = chrome_probe_out(BASE + PROBE_REL, 300000, 420, profile)
        m = re.search(r"\{[\s\S]*\}", body)
        data = None
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:  # noqa: BLE001
                data = None
        if not isinstance(data, dict) or not data.get("checks"):
            check("探针返回可解析结果", False, (body or "（探针没输出）")[:300].replace("\n", " "))
            return
        check("探针跑完了（不是半路超时）", bool(data.get("done")), "done=" + str(data.get("done")))
        for c in data["checks"]:
            check("探针 · " + c["name"], bool(c.get("ok")), c.get("extra", ""))
    except Exception as e:  # noqa: BLE001
        check("浏览器实测执行完成", False, f"{type(e).__name__}: {e}")
    finally:
        shutil.rmtree(profile, ignore_errors=True)


# --------------------------------------------------------------------------- 【5】「删」的配套
def part_delete(e2e: bool) -> None:
    """「删」按钮的防连点只是第一层 —— 真删掉一条**还在跑**的记录会留下一堆孤儿明细：
    跑着那个任务只看 run_id、不查父行在不在，跑完照插。所以删除路径要有两道配套。"""
    print("\n【5】「删」的配套（删掉在跑的记录别留下孤儿明细）")
    src = read("app.py")
    i = src.find("def api_backtest_delete")
    body = src[i:i + 1200] if i >= 0 else ""
    check("删除接口存在（POST /api/backtest/runs/<id>/delete）",
          '@app.route("/api/backtest/runs/<int:rid>/delete"' in src)
    check("删「还在跑」的记录时先叫停那个任务（jobs.cancel）",
          "jobs.cancel(" in body and 'status") == "running"' in body)
    check("任务收尾写库前确认记录还在，不在就跳过（不再写孤儿行）",
          re.search(r'if not db\.scalar\("SELECT id FROM backtest_run WHERE id=%s", \(run_id,\)\):',
                    src) is not None)

    if not e2e:
        skip("库里无孤儿明细", "--no-e2e")
        return
    try:
        from core import db  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        skip("库里无孤儿明细", f"连不上库：{type(e).__name__}")
        return
    try:
        bad = []
        for t in ("backtest_trade", "backtest_equity"):
            n = db.query("SELECT COUNT(*) c FROM " + t + " x LEFT JOIN backtest_run r "
                         "ON r.id=x.run_id WHERE r.id IS NULL")[0]["c"]
            if n:
                bad.append(f"{t}={n}")
        check("库里没有指向已删记录的孤儿明细", not bad, ", ".join(bad))
    except Exception as e:  # noqa: BLE001
        skip("库里无孤儿明细", f"查询失败：{type(e).__name__}")


def probe_json(rel: str, budget_ms: int, timeout: int, tag: str) -> dict | None:
    """跑一个探针页，把它 <pre id="out"> 里的 JSON 解出来。"""
    profile = os.path.join(tempfile.gettempdir(), "_guard_click_profile_" + tag)
    shutil.rmtree(profile, ignore_errors=True)
    try:
        body = chrome_probe_out(BASE + rel, budget_ms, timeout, profile)
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    m = re.search(r"\{[\s\S]*\}", body)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- 【4】全页冒烟
def part_pages(e2e: bool) -> None:
    print("\n【4】各页加载冒烟（改了 10 个模板的内联脚本，得证明没弄坏哪一页）")
    if not e2e:
        skip("各页冒烟", "--no-e2e")
        return
    if not CHROME:
        skip("各页冒烟", "找不到本机 Chrome")
        return
    if not req_ok():
        skip("各页冒烟", "服务没在 127.0.0.1:8777")
        return
    data = probe_json(PAGES_PROBE_REL, 300000, 420, "pages")
    if not isinstance(data, dict) or not data.get("checks"):
        check("各页冒烟探针返回可解析结果", False, "没拿到结果")
        return
    check("各页冒烟跑完了（不是半路超时）", bool(data.get("done")), "done=" + str(data.get("done")))
    for c in data["checks"]:
        check("冒烟 · " + c["name"], bool(c.get("ok")), c.get("extra", ""))


def main() -> int:
    print("=" * 62)
    print("提交类按钮「防连点」回归")
    print("=" * 62)
    args = sys.argv[1:]
    e2e = "--no-e2e" not in args

    part_impl()
    part_wiring()
    part_browser(e2e)
    part_pages(e2e)
    part_delete(e2e)

    print(f"\n{'=' * 62}")
    print(f"按钮防连点回归：{PASS} 通过 / {FAIL} 失败 / {SKIP} 跳过")
    if FAILS:
        print("失败项：")
        for f in FAILS:
            print("  ·", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
