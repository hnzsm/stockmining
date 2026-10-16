#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
选股策略「列表项删除按钮」回归（静态口径 + 接口契约 + 无头 Chrome 真点一次删除）

为什么需要它：列表里每条策略名称后面那个「删」按钮，光读模板看不出它能不能用 ——
  · handler 挂在列表项里，点它可能顺手把这条选中（少一句 stopPropagation 就中招）；
  · confirm 点了「取消」会不会照样删（误删是删不回来的）；
  · 删的正是当前编辑那条时，编辑区会不会留着一份已经不存在的策略（再点保存就成了
    一条没 id 的新记录）；
  · 内置策略该不该给这个按钮（点了只会弹「内置不可删除」，不如不给）。
这四件都得真点一次，所以本脚本分三段：

  【1】静态口径 —— 列表模板里非内置项才渲染 `[data-del]`「删」按钮、装在 `.strat-acts`
                    里（与买卖策略列表同一套）、handler 有 stopPropagation 与 confirm、
                    删的是当前那条时回 `newForm()`、`#btn-new` 与清空共用同一个 newForm
  【2】接口契约 —— `DELETE /api/pools/<id>`：普通策略真删（列表里没了）；内置策略 400 且
                    仍在（删不掉）；删完再查列表确认不是「接口说删了其实还在」
  【3】浏览器实测 —— 同源 iframe 里加载 /pools：按钮该有的都有（内置没有）→ 建一条临时
                    策略，点「删」**取消** → 什么都没发生、编辑区现场没被换掉 → 确定 →
                    真删掉且编辑区回到「新建」态 → 再建一条，先选中别人再删它 → 现场留着；
                    收尾把临时策略清干净

跑法：
    C:/Users/ming/.workbuddy/binaries/python/envs/stockmining/Scripts/python.exe tools/verify_pool_del.py
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
PROBE = "/static/_probe_pool_del.html?run=1"
POOL_HTML = os.path.join(ROOT, "templates", "pool.html")
PROBE_HTML = os.path.join(ROOT, "static", "_probe_pool_del.html")
NODE = r"C:\Users\ming\.workbuddy\binaries\node\versions\22.22.2-3\node.exe"
TMP_NAME = "ZZ·删除自检（回归）"

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
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(BASE + path, data=body, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(r, timeout=timeout).read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())


def get_pools() -> list[dict]:
    return (req("GET", "/api/pools").get("data") or [])


CHROME = None
for _c in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
           r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
           os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe")):
    if os.path.exists(_c):
        CHROME = _c
        break


def chrome_probe_out(url: str, budget_ms: int, timeout: int, profile: str) -> str:
    """渲染探针页并返回 <pre id="out"> 里的文本。"""
    out = os.path.join(tempfile.gettempdir(), "_pool_del_dom.html")
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

    # ------------------------------------------------------------------ 1. 静态口径
    print("【1】静态口径（列表项删除按钮）")
    code = open(POOL_HTML, encoding="utf-8").read()

    check("非内置项才渲染删除按钮（内置不给，点了只会报错）",
          re.search(r"\$\{p\.builtin \? '' : '<div class=\"strat-acts\">", code) is not None)
    check("按钮是 `.btn.sm.danger` + `data-del=<id>`（与买卖策略列表同一套）",
          'class="btn sm danger" data-del="' in code)
    check("按钮文案是「删」（列表窄，别写「删除该策略」挤掉名称）",
          re.search(r"data-del=\"' \+ p\.id \+\n?\s*'\" title=\"[^\"]*\">删</button>", code) is not None
          or (">删</button>" in code))
    # ⚠ 别钉死「onclick = async (e) => {」这一行的确切文本：它外面套一层防连点包装
    #   （`onclick = guardClick(b, async (e) => {`）之后这行就会变 —— 那是实现细节，不是行为。
    #   这里只断言行为：handler 的**第一句**就是 stopPropagation，有没有包装都得成立。
    check("点按钮先 stopPropagation（否则删之前还会把它选中）",
          re.search(r"\[data-del\]'\)\.forEach\(\s*b\s*=>\s*b\.onclick\s*=\s*"
                    r"(?:guardClick\(b,\s*)?async \(e\) => \{\s*\n\s*e\.stopPropagation\(\);",
                    code) is not None)
    check("删除前弹确认（confirm 里点名该策略，误点删不掉）",
          "if (!confirm('确定删除选股策略「' + p.name + '」？" in code)
    check("提示写明「已完成的回测记录不受影响」（删了策略不等于删历史）",
          "已完成的回测记录不受影响" in code)
    check("走统一的 apiDel('DELETE /api/pools/<id>')（不自己拼 fetch）",
          "await apiDel('/api/pools/' + b.dataset.del);" in code)
    check("删失败（内置/网络错）时给提示且不改界面状态",
          "toast(err.message || String(err), 'err', 5000);" in code
          and re.search(r"catch \(err\) \{[\s\S]{0,120}?return;", code) is not None)
    check("删的正是当前那条 → 先清表单再重新拉列表（删完 load() 会顺手选中列表第一条，"
          "与页面刚打开时一致 —— 反正不许留着一份已不存在的策略）",
          "if (String(currentId) === b.dataset.del) newForm();" in code)
    check("newForm() 抽出一份，「新建」按钮与删除后清空共用它（两处各写一半迟早走偏）",
          code.count("function newForm()") == 1
          and "$('#btn-new').onclick = newForm;" in code
          and re.search(r"function newForm\(\)[\s\S]{0,400}?\$\('#ed-title'\)\.textContent = '策略编辑 · 新建';",
                        code) is not None)
    check("newForm 里把底部删除按钮置灰（新建态没有「该策略」可删）",
          re.search(r"function newForm\(\)[\s\S]{0,420}?\$\('#btn-del'\)\.disabled = true;", code) is not None)
    check("删完重新拉列表（不是只在本地 splice，刷新后还在就不对了）",
          re.search(r"toast\('已删除', 'ok'\);\s*\n\s*await load\(\);", code) is not None)

    blocks = re.findall(r"<script(?![^>]*src=)[^>]*>([\s\S]*?)</script>", code)
    big = max(blocks, key=len) if blocks else ""
    js = os.path.join(tempfile.gettempdir(), "_pool_inline_del.js")
    with open(js, "w", encoding="utf-8") as fh:
        fh.write(big)
    if os.path.exists(NODE):
        r = subprocess.run([NODE, "--check", js], capture_output=True, text=True)
        check(f"pool.html 内联脚本语法（{len(big)} 字符）", r.returncode == 0,
              (r.stderr or "").strip()[:140])
    else:
        skip("pool.html 内联脚本语法", "找不到 node")

    check("探针页带 ?run=1 门禁（它会真建真删两条临时策略）",
          os.path.exists(PROBE_HTML) and "params.get('run') !== '1'" in
          open(PROBE_HTML, encoding="utf-8").read())

    # ------------------------------------------------------------------ 2. 接口契约
    print("\n【2】接口契约（DELETE /api/pools/<id>）")
    made = None
    try:
        pools = get_pools()
        check("拿得到选股策略列表", len(pools) > 0, f"{len(pools)} 条")

        bi = [p for p in pools if p.get("builtin")]
        check("库里有内置策略可用来验「删不掉」", len(bi) >= 1,
              "、".join(p["name"] for p in bi[:3]))
        if bi:
            r = req("DELETE", "/api/pools/" + str(bi[0]["id"]))
            after = get_pools()
            check("内置策略删不掉：接口 400 且在库里原样还在",
                  r.get("ok") is False and "内置" in (r.get("error") or "")
                  and any(p["id"] == bi[0]["id"] for p in after),
                  str(r.get("error"))[:60])

        # 普通策略：真删一次（自建自用，不碰用户数据）
        r = req("POST", "/api/pools", {"name": TMP_NAME, "base": "all",
                                       "conds": ["close > 0"],
                                       "note": "删除按钮回归临时策略"})
        made = (r.get("data") or {}).get("id")
        check("临时策略建好了（拿它验真删）", bool(made), "id=" + str(made))
        if made:
            check("新建的是普通策略（builtin=0 ⇒ 可删）",
                  (r.get("data") or {}).get("builtin") in (0, False),
                  str((r.get("data") or {}).get("builtin")))
            r2 = req("DELETE", "/api/pools/" + str(made))
            after = get_pools()
            check("普通策略真被删掉（接口 ok 且列表里没了）",
                  r2.get("ok") is True and not any(p["id"] == made for p in after),
                  str(r2.get("error") or "")[:60])
            made = None
    except Exception as e:
        check("接口契约", False, f"{type(e).__name__}: {e}（服务起了吗？）")
    finally:
        # 收尾：任何情况下都不把临时策略留在库里
        if made:
            try:
                req("DELETE", "/api/pools/" + str(made))
            except Exception:
                pass
        try:
            left = [p for p in get_pools() if (p.get("name") or "").startswith("ZZ·删除自检")]
            check("收尾没把临时策略留在库里", not left,
                  "、".join(p["name"] + "#" + str(p["id"]) for p in left))
        except Exception:
            pass

    # ------------------------------------------------------------------ 3. 浏览器实测
    if e2e and CHROME:
        print("\n【3】浏览器实测（同源 iframe 里真点一次删除）")
        profile = os.path.join(tempfile.gettempdir(), "_pool_del_profile")
        shutil.rmtree(profile, ignore_errors=True)
        try:
            body = chrome_probe_out(BASE + PROBE, 120000, 300, profile)
            data = None
            m = re.search(r"\{[\s\S]*\}", body)
            if m:
                try:
                    data = json.loads(m.group(0))
                except Exception:
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
        skip("浏览器实测", "无 Chrome 或指定了 --no-e2e")

    print(f"\n{'=' * 62}\n选股策略列表删除按钮回归：{PASS} 通过 / {FAIL} 失败 / {SKIP} 跳过")
    if FAILS:
        print("失败项：")
        for x in FAILS:
            print("  -", x)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
