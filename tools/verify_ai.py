#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
AI 分析回归（静态口径 + 接口契约 + 假模型服务真跑一次 + 流式接口 + 无头 Chrome 实测点击）

为什么要跑这么重：这条链路的每一段都「看着像对的」——
  · 配置能不能存、Key 会不会漏给前端（接口回显里不该出现它，只看代码看不出来）；
  · 模板渲染到底有没有生效（`{股票名称}` 没被替换也不会报错，只是答案里没有它）；
  · 大模型调用有**两条**：`/api/ai/analyze` 是后台线程 + 轮询（列表页弹窗的老路），
    `/api/ai/analyze/stream` 是 SSE 边生成边推（诊断页内联卡片）。两条都「看着像对的」，
    真正发出去的提示词、最终落库的正文，只有真打一次才知道；
  · 流式最容易假对：不接上真流的话，把「一次性回一个 JSON」当流式也能跑通界面。
所以本脚本自己起一个**假模型服务**（同时冒充 Ollama 的 /api/chat 与 OpenAI 的
/v1/chat/completions，**非流式与流式两条都冒充**），把后端配置指过去，然后：

  【1】静态口径   —— 三张表在 SCHEMA_SQL 里、预设清单、变量表、内置模板（含需求里那句
                     原话）、页面按钮/导航/脚本是否都挂上了；**流式只有一处实现**
                     （`llm.chat_stream` → `ai.start_analysis_stream`/`_run_stream` →
                     `app` 的 SSE 路由 → 前端 `streamRun`），诊断页「查询条件」下面是
                     内联的 AI 卡片（不是弹窗）
  【2】接口契约   —— **多条模型配置**的建/切/改/删（重名与空名要拒、Key 按条保留、
                     「当前」恰好一条、最后一条不给删）、Key 脱敏（存了但绝不下发）、
                     模板 CRUD（重名/内置不可删/默认唯一）、发起分析的前置校验
                     （没配好要 400 而不是排队等超时，且报的是**哪一条**没配好）
  【3】真跑一次   —— 假模型收请求：路径对不对（ollama 走 /api/chat）、提示词里有没有
                     渲染好的股票名、`{本地数据}` 有没有带上行情快照、认不出的占位符会不会
                     被静默吃掉、指定 profile_id 用的是不是那条配置；结果有没有写进 MySQL；
                     连不上/超时走的是不是「人话错误」；同一只票重复点是不是复用同一条
                     running（不叠请求）
  【3.5】流式接口 —— 假模型**真按流式回**（Ollama NDJSON 分片 / OpenAI SSE 的 `data:` 分片），
                     验：内容类型是 `text/event-stream`、正文是**分片**推的（≥2 片）、
                     分片累积 == `done` 事件里那一份 == 库里那份、`done.rec.status=done`、
                     前置校验不过时**不开流**直接 400、推理型模型（正文空只回 thinking）
                     也推得出来且 `from_think=True`
  【4】浏览器实测 —— 诊断页真点「AI 分析」（**不该弹窗**，正文字节流进卡片、历史按钮转可点）、
                     今日选股结果表真点行内「AI分析」（弹窗里能挑模板/模型、历史来自 MySQL、
                     关掉不留残壳、列表传的是这一行）

跑法：
    C:/Users/ming/.workbuddy/binaries/python/envs/stockmining/Scripts/python.exe tools/verify_ai.py
    加 --no-e2e 跳过【4】（不用起浏览器）。
依赖：服务在 127.0.0.1:8777；本机 Chrome（找不到就把【4】标 SKIP，不算失败）。
副作用：会临时新建几条探针配置（结束后**按表原样还回去**，含每条各自的 Key 与当前标记），
        会建几条探针记录（结束前删掉）。

⚠ 【4】不能用 `--virtual-time-budget` + `--dump-dom` 那套「跑完 dump DOM」：虚拟时间会把
  预算飞快烧完（定时器照跑）而网络流还在慢慢来 —— 实测卡片停在「正在提交…」就 dump 了，
  于是「流式有没有真出内容」永远验不出（假红）。改成探针把结果**回传**给本脚本起的 sink
  （`fetch(..., {mode:'no-cors'})` → `?sink=<port>`），本脚本等回传，实时长效都行。
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BASE = "http://127.0.0.1:8777"
PROBE = "/static/_probe_ai.html?run=1"
STUB_TEXT = "假模型回答：这只票的亮点是假模型编的，用于回归。"
# 「正文全空、只有思考过程」的模型（qwen3 这类推理型模型实测就是这样）：
# 流式与非流式都得把它当正文回，否则用户看到的是一片空白而模型其实答了。
STUB_THINK = "假模型思考：先看行情再看资金面，这只票的亮点是假模型编的。"


def _pieces(text: str, n: int = 6) -> list[str]:
    """把一段话切成 ≤n 片 —— 流式回的时候逐片发，用来验「分片累积 == 全文」。"""
    step = max(1, (len(text) + n - 1) // n)
    return [text[i:i + step] for i in range(0, len(text), step)]

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


def _req(path: str, data=None, timeout: int = 120):
    url = BASE + path
    if data is None:
        req = urllib.request.Request(url)
    else:
        req = urllib.request.Request(url, data=json.dumps(data).encode(),
                                     headers={"Content-Type": "application/json"})
    try:
        raw = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except ValueError:
        return {"ok": False, "error": "非 JSON 响应", "raw": raw[:200]}


def get(path: str):
    return _req(path)


def post(path: str, data: dict):
    return _req(path, data)


def wait_done(rid: int, budget: float = 90.0, quiet: bool = True):
    """轮询到 status != running（或超时返回 None）。"""
    t0 = time.time()
    while time.time() - t0 < budget:
        r = get(f"/api/ai/analysis/{rid}")
        d = r.get("data") or {}
        if d.get("status") != "running":
            return d
        time.sleep(0.4)
    return None


def post_stream(path: str, data: dict, timeout: int = 120):
    """读一次 SSE，返回 `(正常响应 JSON 或 None, 事件列表, 内容类型)`。

    · 前置校验没过时服务端回的是**普通 JSON**（400）→ 第一个返回值是那个 JSON、事件列表空；
    · 真开流了 → 第一个返回 `None`，事件是逐个 `data: {json}` 解析出来的。
    ⚠ 用 `for line in resp`（http.client 的 readline）而不是 `read(n)`：后者会一直等
      够 n 个字节，流式的「边到边」就变成「等到最后一次性收」。
    """
    req = urllib.request.Request(BASE + path, data=json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return json.loads(raw), [], e.headers.get("Content-Type") or ""
        except ValueError:
            return {"ok": False, "error": "非 JSON 响应", "raw": raw[:200]}, [], ""
    ctype = resp.headers.get("Content-Type") or ""
    if "text/event-stream" not in ctype:
        raw = resp.read().decode("utf-8", "replace")
        resp.close()
        try:
            return json.loads(raw), [], ctype
        except ValueError:
            return {"ok": False, "error": "非 JSON 响应", "raw": raw[:200]}, [], ctype
    evs: list[dict] = []
    for line in resp:
        s = line.decode("utf-8", "replace").strip()
        if not s.startswith("data:"):
            continue
        try:
            obj = json.loads(s[5:].strip())
        except ValueError:
            continue
        if isinstance(obj, dict):
            evs.append(obj)
    resp.close()
    return None, evs, ctype


# ---------------------------------------------------------------------------
# 假模型服务：同时冒充 Ollama 与 OpenAI 兼容两条协议（**非流式与流式都冒充**）
# ---------------------------------------------------------------------------

class _Stub(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def log_message(self, *a):        # 别刷屏
        pass

    def _wjson(self, obj, code: int = 200) -> None:
        payload = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _ndjson(self, obj) -> None:
        """Ollama 的流式就是 NDJSON：一行一个 JSON（不是一次性一个 JSON 数组）。"""
        self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())
        self.wfile.flush()

    def _sse(self, obj) -> None:
        """OpenAI 兼容的流式是 SSE：`data: {...}` + 空行（收尾是 `data: [DONE]`）。"""
        data = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
        self.wfile.write(("data: " + data + "\n\n").encode())
        self.wfile.flush()

    def _stream_out(self, model: str) -> None:
        ollama = self.path.startswith("/api/chat")
        think_only = (model == "verify-think-only")
        full = STUB_THINK if think_only else STUB_TEXT
        # 没有 Content-Length（HTTP/1.0 靠关闭连接定界）—— 这样浏览器/requests 会边收边给，
        # 「攒成一块再发」就验不出来了。
        self.send_response(200)
        self.send_header("Content-Type",
                         "application/x-ndjson" if ollama else "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for p in _pieces(full):
                if ollama:
                    msg = {"role": "assistant", "content": ""}
                    if think_only:
                        msg["thinking"] = p
                    else:
                        msg["content"] = p
                    self._ndjson({"model": "verify-stub", "done": False, "message": msg})
                else:
                    delta = {"reasoning_content": p} if think_only else {"content": p}
                    self._sse({"model": "verify-stub",
                               "choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
            if ollama:
                self._ndjson({"model": "verify-stub", "done": True,
                              "message": {"role": "assistant", "content": ""},
                              "prompt_eval_count": 11, "eval_count": 7})
            else:
                # usage 帧：真网关只在 `stream_options.include_usage` 时才给
                if (self._req_body.get("stream_options") or {}).get("include_usage"):
                    self._sse({"model": "verify-stub", "choices": [],
                               "usage": {"prompt_tokens": 11, "completion_tokens": 7,
                                         "total_tokens": 18}})
                self._sse("[DONE]")
        except (BrokenPipeError, ConnectionResetError):
            pass                          # 前端掐流是**预期**行为（探针会主动 abort），不算错

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            body = {}
        self._req_body = body
        msgs = body.get("messages") or []
        user = ""
        for m in reversed(msgs):
            if m.get("role") == "user":
                user = m.get("content") or ""
                break
        model = body.get("model")
        _Stub.seen.append({
            "path": self.path, "model": model, "user": user,
            "system": next((m.get("content") or "" for m in msgs if m.get("role") == "system"), ""),
            "auth": bool(self.headers.get("Authorization")),
            "temperature": body.get("temperature"),
            "num_predict": (body.get("options") or {}).get("num_predict"),
            "stream": bool(body.get("stream")),
            "stream_options": body.get("stream_options"),
        })
        if model == "verify-missing":
            # 冒充 Ollama 的「模型名不存在」：**404 + 一段带 model 的正文**
            # （真实 Ollama 就这么回；不做这条就永远测不到「404 的两种成因」）
            self._wjson({"error": "model 'verify-missing' not found"}, 404)
            return
        if body.get("stream"):
            self._stream_out(model)
            return
        if model == "verify-think-only":
            # 非流式下的「只想不答」：`content` 空、`thinking` 有 —— `_parse` 得回退读它
            if self.path.startswith("/api/chat"):
                out = {"message": {"role": "assistant", "content": "", "thinking": STUB_THINK},
                       "prompt_eval_count": 11, "eval_count": 7, "model": "verify-stub"}
            else:
                out = {"choices": [{"message": {"content": "",
                                                "reasoning_content": STUB_THINK}}],
                       "usage": {"prompt_tokens": 11, "completion_tokens": 7},
                       "model": "verify-stub"}
            self._wjson(out)
            return
        if self.path.startswith("/api/chat"):
            out = {"message": {"role": "assistant", "content": STUB_TEXT},
                   "prompt_eval_count": 11, "eval_count": 7, "model": "verify-stub"}
        else:
            out = {"choices": [{"message": {"content": STUB_TEXT}}],
                   "usage": {"prompt_tokens": 11, "completion_tokens": 7},
                   "model": "verify-stub"}
        self._wjson(out)


def start_stub() -> tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


# ---------------------------------------------------------------------------
def main() -> int:
    e2e = "--no-e2e" not in sys.argv

    # ---------------------------------------------------------------- 1. 静态口径
    print("【1】静态口径（表结构 / 预设 / 变量 / 页面挂载）")
    llm_src = open(os.path.join(ROOT, "core", "llm.py"), encoding="utf-8").read()
    ai_src = open(os.path.join(ROOT, "core", "ai.py"), encoding="utf-8").read()
    db_src = open(os.path.join(ROOT, "core", "db.py"), encoding="utf-8").read()
    app_src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
    base_html = open(os.path.join(ROOT, "templates", "base.html"), encoding="utf-8").read()
    stock_html = open(os.path.join(ROOT, "templates", "stock.html"), encoding="utf-8").read()
    screen_html = open(os.path.join(ROOT, "templates", "screen.html"), encoding="utf-8").read()
    set_html = os.path.join(ROOT, "templates", "settings.html")
    settings_html = open(set_html, encoding="utf-8").read() if os.path.exists(set_html) else ""
    js = open(os.path.join(ROOT, "static", "js", "ai.js"), encoding="utf-8").read() \
        if os.path.exists(os.path.join(ROOT, "static", "js", "ai.js")) else ""

    for t in ("app_setting", "ai_profile", "ai_prompt", "ai_analysis"):
        check(f"建表里有 {t}", f"CREATE TABLE IF NOT EXISTS {t}" in db_src)
    check("ai_analysis 记了 status/content/provider/model/耗时/token",
          all(k in db_src for k in ("status", "content", "provider", "model",
                                    "elapsed_ms", "tokens")))
    check("模型配置是**多条**（ai_profile 有 name/is_active + 唯一名）",
          "uk_ai_profile_name" in db_src and "is_active" in db_src
          and "COMMENT='AI 模型接入配置（多条可切换）'" in db_src)
    check("「当前生效」只有一个维护点 set_active（别处不许手写 is_active）",
          "def set_active" in llm_src and "SET is_active=0 WHERE is_active=1" in llm_src)
    check("启动时保证「至少一条、恰好一条当前」（ensure_profiles）",
          "def ensure_profiles" in llm_src and "llm.ensure_profiles()" in app_src)
    check("老库那份单人配置会被搬过来（而不是丢 Key）",
          "def _legacy_cfg" in llm_src and "migrated_to" in llm_src)
    check("预设表里有 Ollama 本地与「自定义」兜底",
          '"key": "ollama"' in llm_src and '"key": "custom"' in llm_src)
    check("两条协议都实现了（ollama /api/chat 与 openai /chat/completions）",
          '"/api/chat"' in llm_src and '"/chat/completions"' in llm_src)
    check("base_url 容错：已是 /chat/completions 或 /v1 都不再重复拼",
          'endswith("/chat/completions")' in llm_src and 'endswith("/v1")' in llm_src)
    check("变量表是唯一口径（TEMPLATE_VARS 一处定义）",
          "TEMPLATE_VARS" in llm_src and "{本地数据}" in llm_src and "{股票名称}" in llm_src)
    check("模板里的 `{本地数据}` 才去拉快照（不写就不生成）",
          "def wants_data" in llm_src and "wants_data" in ai_src)
    check("内置模板含需求里那句原话",
          "分析{股票名称}有何亮点，近期有无利好利空消息。" in ai_src)
    check("内置模板幂等补齐、启动时清理孤儿 running",
          "def ensure_builtins" in ai_src and "def abort_orphan_runs" in ai_src
          and "aia.ensure_builtins()" in app_src and "aia.abort_orphan_runs()" in app_src)
    check("先落 running 再后台跑（线程 + 轮询，不是同步等模型）",
          "threading.Thread(target=_run" in ai_src and "status='running'" in ai_src)
    check("同一只票重复点复用进行中的记录（不叠请求）",
          "_running_rec" in ai_src and "RUNNING_REUSE_SEC" in ai_src)
    check("分析可以指定用哪条模型配置（不传=当前生效那条）",
          "profile_id" in ai_src and "llm.get_profile" in ai_src and '"profile_id"' in app_src)
    check("本地快照走 screener.screen_detail（与诊断页同一份口径）",
          "screen_detail" in ai_src and "sel_only=True" in ai_src)
    check("上下文只取到查询日（查询日之后那几根不进提示词）",
          "win = rows[:ap + 1]" in ai_src)

    for path in ("/settings", "/api/ai/config", "/api/ai/profiles", "/api/ai/profiles/test",
                 "/api/ai/profiles/<int:pid>/activate", "/api/ai/profiles/<int:pid>/delete",
                 "/api/ai/analyze", "/api/ai/analyze/stream", "/api/ai/analysis",
                 "/api/ai/templates"):
        check(f"路由 {path} 存在", f'"{path}"' in app_src)
    check("导航里有「设置」入口", 'href="/settings"' in base_html)
    check("base.html 加载 static/js/ai.js（两个出口只有一份实现）",
          "js/ai.js" in base_html and js.count("window.aiAnalyze =") == 1
          and js.count("window.aiInline =") == 1)
    check("设置页有配置列表 + 新建 + 切换 + 测试连接 + 模板编辑",
          all(k in settings_html for k in ("prof-list", "btn-new-prof", "设为当前",
                                           "测试连接", "保存模板", "ai-preset")))
    check("设置页说明了 Key 不回落（留空 = 不改）",
          "留空" in settings_html and "清除已保存的 Key" in settings_html)
    check("设置页每条配置都能「编辑 / 设为当前」",
          'data-edit="${p.id}"' in settings_html and 'data-act="${p.id}"' in settings_html)
    check("诊断页有「AI 分析」按钮", 'id="btn-ai"' in stock_html)
    check("诊断页把这一只的代码/名称/收盘/涨跌幅交给**内联卡片**（不再弹窗）",
          "function aiPayload()" in stock_html and "aiInline.run(p)" in stock_html
          and "window.aiAnalyze(p)" not in stock_html)
    check("诊断页「查询条件」卡片下面就是 AI 卡片（回复长在同一页里）",
          'id="st-card-ai"' in stock_html and 'id="st-card-query"' in stock_html
          and stock_html.index('id="st-card-query"') < stock_html.index('id="st-card-ai"'))
    check("卡片里有输出区 / 状态标签 / 元信息 / 右下角「历史回复」按钮",
          all(k in stock_html for k in ('id="ai-card-out"', 'id="ai-card-tag"',
                                        'id="ai-card-meta"', 'id="ai-card-hist"')))
    check("卡片右下角按钮随历史条数启用/禁用（没历史就灰掉、title 也说清）",
          "b.disabled = !rows.length" in js and "还没有历史回复" in js)
    check("进页面 / 换票就摆出**最新一条**回复（不是空白等用户点）",
          "cardRefresh" in js and "最新一条历史" in js)
    check("探针钩子仍暴露 aiPayload（用来核对带出去的参数）",
          "aiPayload: () => aiPayload()" in stock_html)
    check("今日选股结果表有「操作」列", "<th>操作</th>" in screen_html)
    check("今日选股每行「操作」里有 AI分析 按钮（data-ai）",
          re.search(r'data-ai="\$\{i\}"', screen_html) is not None)
    check("列表把**这一行**的数喂给弹窗（不是整页共用一份）",
          "$$('[data-ai]', $('#sc-result'))" in screen_html and "window.aiAnalyze({" in screen_html)
    check("弹窗里能选模型（列的是设置页配的那几条）",
          'id="ai-prof"' in js and "profiles" in js)
    check("弹窗换模型只影响这一次（改全局默认只在设置页「设为当前」）",
          "profile_id: profId || undefined" in js and "不是当前默认" in js)

    print("\n--- 流式口径（一处读协议 / 一处产事件 / 不弹窗的内联卡片）")
    check("流式传输只有一处：llm.chat_stream（协议解析在 _stream_line 里）",
          "def chat_stream" in llm_src and "def _stream_line" in llm_src)
    check("两条流式协议都认（ollama NDJSON / openai 的 `data:` 行 + [DONE]）",
          'if kind == "openai"' in llm_src and '"[DONE]"' in llm_src)
    check("⚠ 按**行**读而不是 iter_lines(decode_unicode)（中文多字节不会被劈乱码）",
          "for raw in r.iter_lines()" in llm_src)
    check("流式业务只有一处产事件：ai.start_analysis_stream / _run_stream",
          "def start_analysis_stream" in ai_src and "def _run_stream" in ai_src)
    check("事件类型就那五种（start/think/delta/done/error，页面别自己解析协议）",
          all(f'"type": "{t}"' in ai_src for t in ("start", "think", "delta", "done", "error")))
    check("正文以**落库那一份**为准（done 事件带 text 回来，前端拿它覆盖本地拼的）",
          '"text": done_ev["text"]' in ai_src and "typeof ev.text === 'string'" in js)
    check("推理型模型：正文空而思考非空 → 把思考当正文（前后端都认 from_think）",
          "from_think" in llm_src and "reasoning_content" in llm_src
          and "from_think" in ai_src and "from_think" in js)
    check("思考增量要**边到边显示**（推理型模型全程只有思考，不能干等）",
          "function bodyHtml" in js and "ai-think" in js)
    check("流式**不复用** running（旧的标成「已被新的分析取代」，否则留条假的「进行中」）",
          "已被新的分析取代" in ai_src and "RUNNING_REUSE_SEC" in ai_src)
    check("前端断了就把记录收口（不留半截 running）",
          "except GeneratorExit" in ai_src and "前端已断开，分析被中断" in ai_src)
    check("SSE 出口带 X-Accel-Buffering: no（否则反代会把流攒成一块，流式白做）",
          "X-Accel-Buffering" in app_src and 'mimetype="text/event-stream"' in app_src)
    check("⚠ SSE 出口显式 Connection: close（不复用，否则流之后那一个请求会卡死）",
          '"Connection": "close"' in app_src.replace("'", '"'))
    check("前置校验在**开流之前**（不合规当场 400 JSON，不是先 200 再在流里报错）",
          "def api_ai_analyze_stream" in app_src and "except aia.AIError as e:" in app_src)
    check("前端读流只有一处实现（streamRun），两条出口都走它",
          js.count("function streamRun") == 1 and js.count("getReader()") == 1
          and js.count("/api/ai/analyze/stream") == 1 and js.count("streamRun(") >= 3)
    check("前端会主动掐流（切票 / 关页面不留后台连接）",
          "AbortController" in js and "cur.ctl.abort()" in js)
    check("弹窗关掉就掐流（不留后台请求）", "abort(); modal = null;" in js)
    check("前端不在本地重定义变量/预设（只有一份口径）",
          "TEMPLATE_VARS" not in js and "PRESETS" not in js)

    # ---------------------------------------------------------------- 2. 接口契约
    print("\n【2】接口契约（多条配置 CRUD / 切换 / Key 脱敏 / 模板 CRUD）")
    h = get("/api/health")
    if not (h.get("data") or {}).get("db"):
        print("  !! 服务没起来（127.0.0.1:8777），后续接口段无法进行")
        return 1

    # ⚠ 必须在**动配置之前**留底：下面是「多条配置」的增删改切换，一路会写很多轮。
    #   而且现场不是「一份配置」而是一整张表 + 老版本那行标记 —— 还原要按**表**来，
    #   只存一份 cfg 的话跑完就会把用户配好的另外几条抹掉（踩过一次，见日志）。
    from core import llm as llm_mod          # noqa: E402  仅用于「原样还回去」
    from core import db as db_mod            # noqa: E402  直接核对 MySQL 里存了什么
    saved_rows = db_mod.query("SELECT * FROM ai_profile ORDER BY id")
    saved_active = llm_mod.active_id()
    saved_setting = db_mod.query_one("SELECT v FROM app_setting WHERE k='ai'")
    # ⚠ 「有没有 Key」的基准必须是**那一条自己**跑之前的状态，不是「库里任意一条有 Key」——
    #   多条配置时后者会让下面「Key 没被牵连」那条假红（用户建了 2 条后踩到）。
    saved_keys = {r["id"]: bool(r.get("api_key") or "") for r in saved_rows}

    d = get("/api/ai/config")
    check("/api/ai/config 可读", d.get("ok") is True, str(d.get("error") or "")[:80])
    payload = d.get("data") or {}
    cfg = payload.get("cfg") or {}
    presets = payload.get("presets") or []
    prows = payload.get("profiles") or []
    check("接口给了全部模型配置 + 哪条是当前",
          isinstance(prows, list) and prows and payload.get("active_id"),
          f"{len(prows)} 条，当前 id={payload.get('active_id')}")
    check("「当前」恰好一条（列表里只有一个亮着）",
          len([p for p in prows if p["is_active"]]) == 1)
    check("**每条配置的 api_key 都不下发**（只给 has/hint）",
          all("api_key" not in p and "api_key_set" in p and "api_key_hint" in p for p in prows))
    check("列表里每条带 ready（哪条能用一眼看出来）",
          all("ready" in p and "name" in p and "model" in p for p in prows))
    check("预设清单够用（≥8 家，含 ollama / custom）",
          len(presets) >= 8 and {p["key"] for p in presets} >= {"ollama", "custom"},
          f"{len(presets)} 家")
    check("每个预设都带 key/label/kind/base_url/model",
          all(all(k in p for k in ("key", "label", "kind", "base_url", "model")) for p in presets))
    check("配置**不含 api_key 字段**（只给 has/hint）",
          "api_key" not in cfg and "api_key_set" in cfg and "api_key_hint" in cfg,
          "含 " + ",".join(sorted(cfg.keys()))[:90])
    check("配置带 kind / key_required / ready",
          all(k in cfg for k in ("kind", "key_required", "ready")))
    vars_ = payload.get("vars") or []
    check("变量清单给了中文占位符 + 说明",
          any(v["token"] == "{本地数据}" for v in vars_) and all("desc" in v for v in vars_))

    tpls = payload.get("templates") or []
    check("内置模板已装（亮点与消息面 / 技术面与资金面速评 / 短线买卖点参考）",
          {"亮点与消息面", "技术面与资金面速评", "短线买卖点参考"} <= {t["name"] for t in tpls},
          f"{len(tpls)} 条")
    dflt = [t for t in tpls if t.get("is_default")]
    check("有且只有一条默认模板", len(dflt) == 1, (dflt[0]["name"] if dflt else "无"))
    base_tpl = next((t for t in tpls if t["name"] == "亮点与消息面"), {})
    check("「亮点与消息面」就是需求里那句话",
          base_tpl.get("template") == "分析{股票名称}有何亮点，近期有无利好利空消息。",
          str(base_tpl.get("template"))[:50])

    # ------------------------------------------------------------ 多条模型配置
    print("\n--- 多条模型配置：建 / 切 / 改 / 删（需求：可先配置多个模型，可切换）")
    d = post("/api/ai/profiles", {"profile": {"name": "探针-远程A", "preset": "deepseek",
                                              "base_url": "", "model": ""}})
    ps = (d.get("data") or {}).get("profiles") or []
    prof_a = next((p for p in ps if p["name"] == "探针-远程A"), None)
    check("新建一条配置成功", d.get("ok") is True and bool(prof_a), str(d.get("error") or "")[:60])
    check("选了预设、地址留空 → 自动补预设默认地址/模型（不建空壳）",
          bool(prof_a) and prof_a["base_url"] == "https://api.deepseek.com/v1"
          and prof_a["model"] == "deepseek-chat",
          f"{prof_a['base_url'] if prof_a else ''} / {prof_a['model'] if prof_a else ''}")
    check("新建**不抢**当前（当前还是原来那条）",
          (d.get("data") or {}).get("active_id") == saved_active)
    d = post("/api/ai/profiles", {"profile": {"name": "探针-远程A", "preset": "openai"}})
    check("重名配置被拒", d.get("ok") is False and "已经有一条" in str(d.get("error")))
    d = post("/api/ai/profiles", {"profile": {"name": "  ", "preset": "openai"}})
    check("空名配置被拒", d.get("ok") is False and "起个名字" in str(d.get("error")))

    # --- Key 是**按条**的：改这条不该碰到那条（多条配置最容易写错的地方）
    SECRET = "sk-verify-secret-1234"
    d = post("/api/ai/profiles", {"id": prof_a["id"], "profile": {"api_key": SECRET}})
    body_txt = json.dumps(d, ensure_ascii=False)
    ps = (d.get("data") or {}).get("profiles") or []
    prof_a = next(p for p in ps if p["name"] == "探针-远程A")
    orig = next(p for p in ps if p["id"] == saved_active)
    check("给某条存 Key 后接口仍不下发它（脱敏）",
          SECRET not in body_txt and prof_a["api_key_set"] is True,
          "hint=" + str(prof_a.get("api_key_hint")))
    check("Key 提示只露尾 4 位", str(prof_a.get("api_key_hint", "")).endswith("1234"))
    check("**另一条配置的 Key 没被牵连**（每条各存各的）",
          orig.get("api_key_set") is saved_keys.get(saved_active),
          f"另一条（id={saved_active}）api_key_set={orig.get('api_key_set')}，"
          f"跑之前是 {saved_keys.get(saved_active)}")
    d = post("/api/ai/profiles", {"id": prof_a["id"], "profile": {"temperature": 0.45}})
    prof_a = next(p for p in (d.get("data") or {}).get("profiles") or [] if p["id"] == prof_a["id"])
    check("不传 api_key 时**保留**这条已存的 Key（空框不会冲掉它）",
          prof_a.get("api_key_set") is True and abs(float(prof_a.get("temperature", 0)) - 0.45) < 1e-9)
    d = post("/api/ai/profiles", {"id": prof_a["id"], "profile": {"clear_api_key": True}})
    prof_a = next(p for p in (d.get("data") or {}).get("profiles") or [] if p["id"] == prof_a["id"])
    check("clear_api_key 才清空", prof_a.get("api_key_set") is False)
    d = post("/api/ai/profiles", {"id": prof_a["id"],
                                  "profile": {"temperature": 99, "max_tokens": 1, "timeout": 99999}})
    prof_a = next(p for p in (d.get("data") or {}).get("profiles") or [] if p["id"] == prof_a["id"])
    check("数值越界被夹回合理区间（temperature≤2 / max_tokens≥64 / 超时≤600）",
          float(prof_a["temperature"]) <= 2 and int(prof_a["max_tokens"]) >= 64
          and int(prof_a["timeout"]) <= 600,
          f"t={prof_a['temperature']} mt={prof_a['max_tokens']} tmo={prof_a['timeout']}")

    # --- 切换：切换的是「现在用哪个」
    d = post(f"/api/ai/profiles/{prof_a['id']}/activate", {})
    ps = (d.get("data") or {}).get("profiles") or []
    check("切换当前配置成功", d.get("ok") is True
          and (d.get("data") or {}).get("active_id") == prof_a["id"])
    check("切换后**仍然恰好一条**是当前（不会两条都亮）",
          len([p for p in ps if p["is_active"]]) == 1)
    check("后端 load_cfg 跟着切过去（分析用的就是新那条）",
          llm_mod.active_id() == prof_a["id"]
          and llm_mod.load_cfg()["base_url"] == prof_a["base_url"])
    check("切不存在的配置 → 404", post("/api/ai/profiles/99999999/activate", {}).get("ok") is False)

    # --- 删除：删掉当前那条 → 剩下里 id 最小的顶上；只剩一条时不许再删
    # ⚠ 别写死「顶上来的就是 saved_active」。规则是「剩下里 id 最小的顶上」（`delete_profile`
    #   里 ORDER BY id LIMIT 1），库里本来就有 2+ 条配置时那不是原来当前那条（踩过假红）。
    keep = sorted(r["id"] for r in saved_rows)
    d = post(f"/api/ai/profiles/{prof_a['id']}/delete", {})
    check("删掉「当前」那条 → 剩下里 id 最小的自动顶上来",
          d.get("ok") is True and (d.get("data") or {}).get("active_id") == keep[0],
          f"active={(d.get('data') or {}).get('active_id')}，期望 {keep[0]}")
    if len(keep) == 1:
        d = post(f"/api/ai/profiles/{keep[0]}/delete", {})
        check("**最后一条不给删**（删完就没可用接入了）",
              d.get("ok") is False and "至少要留一条" in str(d.get("error")),
              str(d.get("error"))[:50])
    else:
        # 库里本来就不止一条 —— 没法在不删用户配置的前提下造出「只剩一条」的现场。
        # 那就直接验**规则本身**：把 COUNT(*) 打桩成 1，看它还拒不拒。
        # 这条路径走不到 DELETE（先抛错），不会真删东西。
        real_scalar = db_mod.scalar
        db_mod.scalar = lambda sql, args=None: (1 if "COUNT(*)" in str(sql)
                                                else real_scalar(sql, args))
        try:
            try:
                llm_mod.delete_profile(keep[0])
                refused, msg = False, "（没报错，违反了规则）"
            except llm_mod.LLMError as e:
                refused, msg = True, str(e)
        finally:
            db_mod.scalar = real_scalar
        check(f"**最后一条不给删**（库里现有 {len(keep)} 条，改用打桩把条数当 1 来验规则）",
              refused and "至少要留一条" in msg, msg[:60])

    # --- 模板 CRUD
    d = post("/api/ai/templates", {"name": "探针模板A", "template": "看看{股票名称}"})
    check("新建模板成功", d.get("ok") is True and (d.get("data") or {}).get("id"), str(d.get("error") or ""))
    tid = (d.get("data") or {}).get("id")
    d = post("/api/ai/templates", {"name": "探针模板A", "template": "重名"})
    check("重名模板被拒", d.get("ok") is False and "已存在" in str(d.get("error")))
    d = post("/api/ai/templates", {"name": "", "template": "x"})
    check("空模板名被拒", d.get("ok") is False)
    d = post(f"/api/ai/templates/{tid}", {"name": "探针模板A", "template": "看看{股票名称}（{日期}）"})
    check("改模板成功", d.get("ok") is True
          and any(t["template"].endswith("{日期}）") for t in (d.get("data") or [])))
    # 「变量」列显示的是**原文**写法（用户写 `{股票名称}` 就该看到 `{股票名称}`），
    # 不是内部字段名（`name`）—— 后者是给程序判断用的，给界面看等于没说。
    vt = next((t for t in (d.get("data") or []) if t["id"] == tid), {})
    check("模板列表的「变量」按原文写法显示（`{股票名称}`，不是内部 key `name`）",
          vt.get("vars") == ["{股票名称}", "{日期}"], str(vt.get("vars")))
    d = post(f"/api/ai/templates/{tid}/default", {})
    check("设为默认成功且默认唯一",
          d.get("ok") is True
          and len([t for t in (d.get("data") or []) if t["is_default"]]) == 1
          and [t for t in (d.get("data") or []) if t["is_default"]][0]["id"] == tid)
    bid = base_tpl.get("id")
    d = post(f"/api/ai/templates/{bid}/delete", {})
    check("内置模板不给删（可改文案）", d.get("ok") is False and "内置" in str(d.get("error")))
    d = post(f"/api/ai/templates/{tid}/delete", {})
    check("默认模板不给删（先换默认）", d.get("ok") is False and "默认" in str(d.get("error")))
    post(f"/api/ai/templates/{bid}/default", {})       # 默认换回内置那条
    post("/api/ai/templates", {"name": "探针模板A", "template": "看看{股票名称}"})
    tid = next((t["id"] for t in (get("/api/ai/templates").get("data") or [])
                if t["name"] == "探针模板A"), None)
    d = post(f"/api/ai/templates/{tid}/delete", {})
    check("非默认自定义模板能删", d.get("ok") is True)
    check("默认回到内置「亮点与消息面」",
          next(t["name"] for t in (get("/api/ai/templates").get("data") or []) if t["is_default"])
          == "亮点与消息面")

    # --- 发起分析的前置校验（配没配好不该排到队里才发现）
    check("空代码被拒", post("/api/ai/analyze", {}).get("ok") is False)
    d = post("/api/ai/profiles", {"profile": {"name": "探针-空壳", "preset": "custom",
                                              "kind": "openai", "base_url": "", "model": ""}})
    shell = next((p for p in (d.get("data") or {}).get("profiles") or []
                  if p["name"] == "探针-空壳"), None)
    check("建一条空壳配置（自定义 + 空地址）", bool(shell) and shell["ready"] is False)
    post(f"/api/ai/profiles/{shell['id']}/activate", {})
    d = post("/api/ai/analyze", {"ts_code": "000001.SZ"})
    check("没填地址/模型时当场 400（不是排队等超时）",
          d.get("ok") is False and "还没配好" in str(d.get("error")), str(d.get("error"))[:70])
    d = post("/api/ai/profiles", {"profile": {"name": "探针-要Key", "preset": "deepseek"}})
    needkey = next((p for p in (d.get("data") or {}).get("profiles") or []
                    if p["name"] == "探针-要Key"), None)
    d = post("/api/ai/analyze", {"ts_code": "000001.SZ", "profile_id": needkey["id"]})
    check("需要 Key 的预设没 Key 时也当场 400",
          d.get("ok") is False and "API Key" in str(d.get("error")), str(d.get("error"))[:70])
    check("**报的是这条自己的名字**（多条配置下才有的坑）",
          "探针-要Key" in str(d.get("error")), str(d.get("error"))[:40])
    d = post("/api/ai/analyze", {"ts_code": "000001.SZ", "profile_id": 99999999})
    check("指定了不存在的配置 → 当场 400",
          d.get("ok") is False and "不存在" in str(d.get("error")), str(d.get("error"))[:50])

    # ---------------------------------------------------------------- 3. 真跑一次
    print("\n【3】真跑一次（假模型服务冒充 Ollama / OpenAI）")
    srv, stub = start_stub()
    made: list[int] = []
    # 记录「跑之前已有的最大 id」：收尾时按它划界删探针新建的那些
    # （浏览器那段自己也会建记录、id 不经过本脚本）。
    base_max = int(db_mod.scalar("SELECT IFNULL(MAX(id),0) FROM ai_analysis") or 0)
    try:
        _Stub.seen.clear()
        d = post("/api/ai/profiles", {"profile": {"name": "探针-假模型", "preset": "custom",
                                                  "kind": "ollama", "base_url": stub,
                                                  "model": "verify-stub", "timeout": 30}})
        fake = next((p for p in (d.get("data") or {}).get("profiles") or []
                     if p["name"] == "探针-假模型"), None)
        check("新建一条指向假模型服务的配置（自定义 + ollama 协议）",
              d.get("ok") is True and bool(fake) and fake["ready"] is True,
              str(d.get("error") or "")[:60])
        d = post("/api/ai/profiles/test", {"id": fake["id"],
                                           "profile": {"base_url": stub, "model": "verify-stub"}})
        check("「测试连接」真打到模型并回话",
              d.get("ok") is True and STUB_TEXT[:6] in str((d.get("data") or {}).get("text")),
              str(d.get("error") or (d.get("data") or {}).get("model")))
        check("测试连接报的是真耗时", int((d.get("data") or {}).get("elapsed_ms", -1)) >= 0)
        check("测的是一条**还没启用**的配置（不必先切成当前）",
              (d.get("data") or {}).get("profile") == "探针-假模型"
              and llm_mod.active_id() != fake["id"], f"active={llm_mod.active_id()}")
        # 404 的两种成因必须分开说清 —— 实测踩过：本机 Ollama 没装默认那个模型，
        # 页面却提示「地址可能少写 /v1」，照着改地址只会越改越远。
        d = post("/api/ai/profiles/test", {"profile": {"preset": "custom", "kind": "ollama",
                                                       "base_url": stub,
                                                       "model": "verify-missing"}})
        check("模型名不存在时人话点名**模型名**，而不是「地址少写 /v1」",
              d.get("ok") is False and "模型名" in str(d.get("error"))
              and "/v1" not in str(d.get("error")), str(d.get("error"))[:76])
        d = post(f"/api/ai/profiles/{fake['id']}/activate", {})
        check("把它切成当前（之后不带 profile_id 的分析都用它）",
              d.get("ok") is True and llm_mod.active_id() == fake["id"])

        r = post("/api/ai/analyze", {"ts_code": "000001.SZ", "name": "平安银行",
                                     "source": "stock"})
        rec = r.get("data") or {}
        check("发起分析立刻回 running 记录（不阻塞等模型）",
              r.get("ok") is True and rec.get("status") == "running" and rec.get("id"),
              str(r.get("error") or ""))
        rid = int(rec["id"])
        made.append(rid)
        r2 = post("/api/ai/analyze", {"ts_code": "000001.SZ", "name": "平安银行"})
        check("同一只票连点两次 → 复用同一条 running（不叠请求）",
              int((r2.get("data") or {}).get("id") or -1) == rid)
        done = wait_done(rid)
        check("轮询到 done 且正文就是模型回的", bool(done) and done.get("status") == "done"
              and STUB_TEXT in str(done.get("content")), str(done or "")[:60])
        check("记录里存了 provider/model/耗时/token",
              done and done.get("provider") == "ollama" and done.get("model") == "verify-stub"
              and int(done.get("elapsed_ms") or -1) >= 0 and int(done.get("tokens") or 0) == 18,
              f"tokens={done.get('tokens')}" if done else "")

        seen = _Stub.seen[-1] if _Stub.seen else {}
        check("ollama 协议走的是 /api/chat（不是拼成 /v1/...）",
              seen.get("path") == "/api/chat", str(seen.get("path")))
        check("提示词里的 {股票名称} 真被替换成「平安银行」",
              "平安银行" in str(seen.get("user")) and "{股票名称}" not in str(seen.get("user")),
              str(seen.get("user"))[:40])
        check("系统提示也带上了（约束模型别编数据）", len(str(seen.get("system"))) > 20)

        row = db_mod.query_one("SELECT ts_code,status,content,prompt,source FROM ai_analysis "
                               "WHERE id=%s", (rid,))
        check("MySQL 里真存了正文与提示词（需求：ai分析内容 mysql 存储）",
              row and row["status"] == "done" and STUB_TEXT in (row["content"] or "")
              and "平安银行" in (row["prompt"] or ""))
        check("记录带入口来源（stock）", (row or {}).get("source") == "stock")

        # --- 指定 profile_id：这一次换「另一条配置」（模型名不同，发出去的请求能看出来）
        _Stub.seen.clear()
        d = post("/api/ai/profiles", {"profile": {"name": "探针-假模型2", "preset": "custom",
                                                  "kind": "ollama", "base_url": stub,
                                                  "model": "verify-stub-2", "timeout": 30}})
        fake2 = next((p for p in (d.get("data") or {}).get("profiles") or []
                      if p["name"] == "探针-假模型2"), None)
        r = post("/api/ai/analyze", {"ts_code": "601398.SH", "profile_id": fake2["id"]})
        rid5 = int((r.get("data") or {}).get("id") or 0)
        if rid5:
            made.append(rid5)
            wait_done(rid5)
        sent = _Stub.seen[-1] if _Stub.seen else {}
        check("「本次用哪条配置」真生效（发出去的是那条的模型名，且没改当前默认）",
              sent.get("model") == "verify-stub-2" and llm_mod.active_id() != fake2["id"],
              f"model={sent.get('model')} active={llm_mod.active_id()}")

        # --- {本地数据} 模板：提示词里要真带上本地快照
        _Stub.seen.clear()
        d = post("/api/ai/templates", {"name": "探针-带数据",
                                       "template": "看看{股票名称}（{股票代码}）：\n{本地数据}"})
        dtid = (d.get("data") or {}).get("id")
        r = post("/api/ai/analyze", {"ts_code": "600519.SH", "template_id": dtid,
                                     "source": "screen"})
        check("用自定义模板发起分析", r.get("ok") is True, str(r.get("error") or "")[:60])
        rid2 = int((r.get("data") or {}).get("id") or 0)
        if rid2:
            made.append(rid2)
        done2 = wait_done(rid2) if rid2 else None
        prompt2 = str((_Stub.seen[-1] or {}).get("user")) if _Stub.seen else ""
        check("模板里的 {本地数据} 真展开了（含行情/均线/基本面快照）",
              "【行情】" in prompt2 and "【均线】" in prompt2 and "【基本面】" in prompt2,
              prompt2.split("\n")[0][:40] if prompt2 else "")
        check("{股票代码} 也替换了", "600519.SH" in prompt2 and "{股票代码}" not in prompt2)
        check("本地快照截止查询日（提示词里注明日期）", "【查询日】" in prompt2)
        check("跑完的记录是 screen 来源", (done2 or {}).get("source") == "screen")

        # --- 认不出的占位符不许静默吃掉
        _Stub.seen.clear()
        d = post("/api/ai/templates", {"name": "探针-坏变量",
                                       "template": "看{股票名称}和{不存在的变量}"})
        btid = (d.get("data") or {}).get("id")
        r = post("/api/ai/analyze", {"ts_code": "000002.SZ", "template_id": btid})
        rid3 = int((r.get("data") or {}).get("id") or 0)
        if rid3:
            made.append(rid3)
        if rid3:
            wait_done(rid3)
        p3 = str((_Stub.seen[-1] or {}).get("user"))
        check("认不出的占位符原样留着（且日志有告警，不当成空串）",
              "{不存在的变量}" in p3 and "万科" in p3, p3[:40])

        # --- 错误路径：地址不通 → 记录里是「人话」
        # 用一条**独立的**配置（指向死端口），别去改当前那条 —— 多条配置下这是两回事。
        d = post("/api/ai/profiles", {"profile": {"name": "探针-死端口", "preset": "custom",
                                                  "kind": "ollama", "base_url": "http://127.0.0.1:1",
                                                  "model": "verify-stub"}})
        dead = next((p for p in (d.get("data") or {}).get("profiles") or []
                     if p["name"] == "探针-死端口"), None)
        r = post("/api/ai/analyze", {"ts_code": "300750.SZ", "profile_id": dead["id"]})
        rid4 = int((r.get("data") or {}).get("id") or 0)
        if rid4:
            made.append(rid4)
        if rid4:
            bad = wait_done(rid4, budget=40)
            err = str((bad or {}).get("error") or "")
            # ⚠ 只断言「是人话、不是 traceback」，别写死文案：本机若挂了 HTTP 代理，
            #   `127.0.0.1:1` 这一跳是**代理**去连的 —— 有时回 ProxyError（→「连不上…」），
            #   有时回 502 + 中文 socket 错误（→「模型服务返回 HTTP 502…」），两种都算合格。
            check("连不上时不吞异常：记录变 error，且是人话（不是 traceback）",
                  bad and bad.get("status") == "error" and err and "Traceback" not in err
                  and any(k in err for k in ("连不上", "无法连接", "ProxyError", "拒绝")),
                  err[:76])
        check("跑挂一次之后「当前配置」没被带歪（还是假模型那条）",
              llm_mod.active_id() == fake["id"], f"active={llm_mod.active_id()}")

        # --- 历史接口
        d = get("/api/ai/analysis?ts_code=000001.SZ&limit=10")
        rows = d.get("data") or []
        check("历史接口按代码过滤", d.get("ok") is True and any(r["id"] == rid for r in rows),
              f"{len(rows)} 条")
        check("历史列表**不带正文**、只给预览（别把一整页正文塞进列表）",
              rows and "content" not in rows[0] and "preview" in rows[0])
        d = get(f"/api/ai/analysis/{rid}")
        check("单条详情带正文与提示词",
              STUB_TEXT in str((d.get("data") or {}).get("content"))
              and "平安银行" in str((d.get("data") or {}).get("prompt")))
        check("记录不存在时 404", get("/api/ai/analysis/99999999").get("ok") is False)

        # ------------------------------------------------------------ 3.5 流式接口
        # 假模型**真按流式回**（Ollama 一行一个 JSON；OpenAI 是 `data: {...}` 到 `[DONE]`），
        # 所以下面「分片累积 == 全文」是真的在验流式，而不是「一次性回一个 JSON 也当流式」。
        print("\n【3.5】流式接口（POST /api/ai/analyze/stream，SSE 边生成边推）")

        # 前置校验必须在**开流之前**：坏输入要 400 + JSON。否则前端得先把卡片画出来再拆掉，
        # 而 HTTP 状态码一旦开始推流就改不了了。
        r0, _e0, ct0 = post_stream("/api/ai/analyze/stream", {})
        check("空代码：当场 400 + JSON（根本没开流）",
              r0 is not None and r0.get("ok") is False and "ts_code" in str(r0.get("error")),
              str(r0.get("error"))[:60] if r0 else f"竟然开了流 ct={ct0}")
        r0, _e0, ct0 = post_stream("/api/ai/analyze/stream",
                                   {"ts_code": "000001.SZ", "profile_id": 99999999})
        check("指定不存在的模型配置：也是当场 400（不开流）",
              r0 is not None and r0.get("ok") is False and "不存在" in str(r0.get("error")),
              str(r0.get("error"))[:60] if r0 else f"竟然开了流 ct={ct0}")

        _Stub.seen.clear()
        _r1, evs, ct1 = post_stream("/api/ai/analyze/stream",
                                    {"ts_code": "000001.SZ", "name": "平安银行"})
        types = [e.get("type") for e in evs]
        check("真发起：内容类型是 text/event-stream（不是 application/json）",
              "text/event-stream" in ct1, ct1)
        check("是一串事件而不是一个整块 JSON（首事件就是 start）",
              len(evs) >= 3 and types[:1] == ["start"], f"{len(evs)} 个事件 {types[:4]}…")
        srec = (evs[0].get("rec") or {}) if evs else {}
        sid = int(srec.get("id") or 0)
        if sid:
            made.append(sid)
        check("start 事件带回已落库的记录（含 id，状态 running）",
              sid > 0 and srec.get("status") == "running", f"id={sid}")
        dl = [e.get("text") or "" for e in evs if e.get("type") == "delta"]
        d_ev = next((e for e in evs if e.get("type") == "done"), None)
        check("正文是**分片**推出来的（≥2 片，否则等于没流式）", len(dl) >= 2, f"{len(dl)} 片")
        check("分片累积 == 模型回的全文", "".join(dl) == STUB_TEXT, ("".join(dl))[:40])
        check("done 事件带回**落库那一份**正文（显示口径只有一处）",
              d_ev is not None and d_ev.get("text") == STUB_TEXT,
              str((d_ev or {}).get("text"))[:40])
        check("done 里记录已收口（status=done，不是还挂着 running）",
              d_ev is not None and (d_ev.get("rec") or {}).get("status") == "done")
        row = db_mod.query_one("SELECT status,content,provider FROM ai_analysis WHERE id=%s", (sid,))
        check("流式结果照旧落 MySQL（关页面也能回看）",
              row and row["status"] == "done" and STUB_TEXT in (row["content"] or ""),
              str((row or {}).get("status")))
        seen1 = _Stub.seen[-1] if _Stub.seen else {}
        check("流式请求体里 stream=true（真按流式问的模型，不是假装）",
              seen1.get("stream") is True, str(seen1.get("stream")))
        check("流式仍然只走一处协议（假模型收到的还是 /api/chat）",
              seen1.get("path") == "/api/chat", str(seen1.get("path")))
        check("流式也把提示词渲染好了（{股票名称} → 平安银行）",
              "平安银行" in str(seen1.get("user")) and "{股票名称}" not in str(seen1.get("user")))

        # 推理型模型：正文全空、只有 thinking（本机那个 qwen3:30b-a3b 实测就这样）——
        # 若只推 delta，用户从头到尾看不到一个字。
        _Stub.seen.clear()
        d = post("/api/ai/profiles", {"profile": {"name": "探针-只想不答", "preset": "custom",
                                                 "kind": "ollama", "base_url": stub,
                                                 "model": "verify-think-only", "timeout": 30}})
        tprof = next((p for p in (d.get("data") or {}).get("profiles") or []
                      if p["name"] == "探针-只想不答"), None)
        _r2, evs2, _ct2 = post_stream("/api/ai/analyze/stream",
                                      {"ts_code": "600000.SH", "profile_id": tprof["id"]})
        sid2 = int((((evs2[0].get("rec") or {}) if evs2 else {}).get("id")) or 0)
        if sid2:
            made.append(sid2)
        th = "".join(e.get("text") or "" for e in evs2 if e.get("type") == "think")
        d2 = next((e for e in evs2 if e.get("type") == "done"), None)
        check("模型只回思考过程时，思考增量也推给前端（不能全程干等）",
              th == STUB_THINK, f"{len(th)} 字")
        check("正文空而思考非空 → done.from_think=True 且把思考当正文",
              d2 is not None and d2.get("from_think") is True and d2.get("text") == STUB_THINK,
              f"from_think={(d2 or {}).get('from_think')}")
        row2 = db_mod.query_one("SELECT content FROM ai_analysis WHERE id=%s", (sid2,))
        check("落库的也是那份思考内容（不是一片空白）",
              row2 and STUB_THINK in (row2["content"] or ""),
              str((row2 or {}).get("content"))[:30])

        # OpenAI 兼容那条协议：SSE（`data:` 行 + usage 帧 + `[DONE]`）
        _Stub.seen.clear()
        d = post("/api/ai/profiles", {"profile": {"name": "探针-假模型-OpenAI", "preset": "custom",
                                                 "kind": "openai", "base_url": stub,
                                                 "model": "verify-stub", "timeout": 30}})
        oprof = next((p for p in (d.get("data") or {}).get("profiles") or []
                      if p["name"] == "探针-假模型-OpenAI"), None)
        _r3, evs3, _ct3 = post_stream("/api/ai/analyze/stream",
                                      {"ts_code": "000002.SZ", "profile_id": oprof["id"]})
        sid3 = int((((evs3[0].get("rec") or {}) if evs3 else {}).get("id")) or 0)
        if sid3:
            made.append(sid3)
        d3 = next((e for e in evs3 if e.get("type") == "done"), None)
        check("OpenAI 兼容的 SSE（data: 行 / usage 帧 / [DONE]）也读得对",
              d3 is not None and d3.get("text") == STUB_TEXT, str((d3 or {}).get("text"))[:30])
        check("token 数按 usage 帧落库（=18；拿不到就不猜）",
              d3 is not None and (d3.get("rec") or {}).get("tokens") == 18,
              f"tokens={(d3 or {}).get('rec', {}).get('tokens')}")
        check("走的是 /v1/chat/completions（没把 kind=openai 拼成 /api/chat）",
              (_Stub.seen[-1] if _Stub.seen else {}).get("path") == "/v1/chat/completions",
              str((_Stub.seen[-1] if _Stub.seen else {}).get("path")))

        # ------------------------------------------------------------ 4. 浏览器实测
        print("\n【4】浏览器实测（真点「AI 分析」按钮）")
        if not (e2e and _find_chrome()):
            skip("浏览器实测", "指定了 --no-e2e 或没找到 Chrome")
        else:
            res = _run_probe()
            if res is None:
                check("探针页跑完并回报结果", False, "没读到 <pre id=\"out\"> 的结果")
            else:
                for it in res:
                    check("浏览器：" + it["name"], it["ok"], it.get("extra", ""))
    finally:
        srv.shutdown()
        # 配置**按表原样还回去**（含每条各自的 Key 与「当前」标记）：探针建的几条要消失，
        # 原来那几条要照原样回来。⚠ 只留「一份 cfg」是不够的 —— 那样会把用户配好的
        # 另外几条一起抹掉（多条配置之后，还原的粒度也必须跟着变）。
        try:
            db_mod.execute("DELETE FROM ai_profile")
            for r in saved_rows:
                db_mod.execute(
                    "INSERT INTO ai_profile (id, name, preset, kind, base_url, model, api_key, "
                    "temperature, max_tokens, timeout, system_prompt, is_active, created_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (r["id"], r["name"], r["preset"], r["kind"], r["base_url"], r["model"],
                     r["api_key"], r["temperature"], r["max_tokens"], r["timeout"],
                     r["system_prompt"], r["is_active"], r.get("created_at")))
            if saved_setting is None:
                db_mod.execute("DELETE FROM app_setting WHERE k='ai'")
            else:
                db_mod.execute("INSERT INTO app_setting (k, v) VALUES ('ai', %s) "
                               "ON DUPLICATE KEY UPDATE v=VALUES(v)", (saved_setting["v"],))
            back = llm_mod.list_profiles()
            # ⚠ 比之前先**排序**：`list_profiles()` 是「当前那条排最前」的序，底稿是
            #   `ORDER BY id` —— 直接逐个比会每次都报「不一致」（实际还原是好的），假红。
            same = (sorted(p["id"] for p in back) == sorted(r["id"] for r in saved_rows)
                    and llm_mod.active_id() == saved_active)
            print(f"  --   已还原 {len(saved_rows)} 条模型配置（当前 id={llm_mod.active_id()}，"
                  f"库里 {sorted(p['id'] for p in back)}）"
                  + ("" if same else "  ⚠ 与跑之前不一致，请检查"))
        except Exception as e:  # noqa: BLE001
            print(f"  !! 还原 AI 配置失败：{e}")
        # 清掉探针建的记录与模板（历史里不该留「假模型回答」）。
        # ⚠ 浏览器那段（【4】）自己也会建记录、而且 id 不经过本脚本，所以除了按 id 删，
        #   还要按「正文里带假模型标记 / 错误是探针那个死端口」把这一段新建的都收掉。
        try:
            for t in (get("/api/ai/templates").get("data") or []):
                if t["name"].startswith("探针"):
                    post(f"/api/ai/templates/{t['id']}/delete", {})
            for rid in made:
                post(f"/api/ai/analysis/{rid}/delete", {})
            n1 = db_mod.execute("DELETE FROM ai_analysis WHERE id > %s AND content LIKE %s",
                                (base_max, "%假模型%"))
            n2 = db_mod.execute("DELETE FROM ai_analysis WHERE id > %s AND IFNULL(error,'') LIKE %s",
                                (base_max, "%127.0.0.1:1%"))
            print(f"  --   已清理探针记录 {len(made) + int(n1 or 0) + int(n2 or 0)} 条、"
                  f"探针模板（配置已还原）")
        except Exception as e:  # noqa: BLE001
            print(f"  !! 清理探针数据失败：{e}")

    print(f"\n通过 {PASS} / 失败 {FAIL} / 跳过 {SKIP}")
    if FAILS:
        print("失败项：")
        for f in FAILS:
            print("  -", f)
    return 1 if FAIL else 0


def _find_chrome():
    for c in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe")):
        if os.path.exists(c):
            return c
    return None


# ---------------------------------------------------------------------------
# 探针回传用的 sink
#
# 为什么不用 `--dump-dom --virtual-time-budget`：探针要等**真实的流**（几十秒），
# 而虚拟时间会把预算飞快烧完（定时器照跑、网络流还得慢慢来）→ 提前 dump，
# 卡片还停在「正在提交…」，于是「流式有没有真出内容」被验成假红。改成探针把结果
# POST 回来（`fetch(..., {mode:'no-cors'})`，简单请求、没有预检），本脚本在真实时间里等。
# ---------------------------------------------------------------------------

class _Sink(BaseHTTPRequestHandler):
    got: dict | None = None

    def log_message(self, *a):        # 别刷屏
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            _Sink.got = json.loads(raw.decode("utf-8", "replace") or "{}")
        except ValueError:
            _Sink.got = {"error": "探针回传的不是 JSON：%r" % raw[:120], "res": []}
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", "0")
        self.end_headers()


def _run_probe():
    chrome = _find_chrome()
    if not chrome:
        return None
    sink = ThreadingHTTPServer(("127.0.0.1", 0), _Sink)
    threading.Thread(target=sink.serve_forever, daemon=True).start()
    port = sink.server_address[1]
    _Sink.got = None
    profile = os.path.join(tempfile.gettempdir(), "_ai_probe_profile")
    url = BASE + PROBE + f"&sink={port}"
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
         "--user-data-dir=" + profile, "--window-size=1500,900", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.time()
    try:
        while time.time() - t0 < 240:
            g = _Sink.got or {}
            if g.get("done") or g.get("error"):
                break
            time.sleep(0.3)
    finally:
        try:
            proc.kill()
        except OSError:
            pass
        sink.shutdown()
        sink.server_close()
    d = _Sink.got
    if d is None:
        return None
    if d.get("error"):
        return [{"name": "探针页没抛异常", "ok": False, "extra": str(d["error"])[:300]}]
    out = list(d.get("res") or [])
    if not d.get("done"):
        out.append({"name": "探针页跑完并回报结果", "ok": False,
                    "extra": f"只跑到 {len(out)} 条就超时了（后面没跑完）"})
    return out


if __name__ == "__main__":
    sys.exit(main())
