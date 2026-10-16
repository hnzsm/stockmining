# -*- coding: utf-8 -*-
"""后台 debug 日志验证：落文件、SQL/HTTP/任务打点、tail 过滤、级别切换。

用法: python tools/verify_log.py        （不需要服务在运行，用 Flask test_client）
"""
import json
import logging
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"  — {detail}" if detail else ""))


class Cap(logging.Handler):
    """把日志同时收进内存，便于断言。"""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.setFormatter(logging.Formatter(logx.FMT, datefmt=logx.DATEFMT))
        self.addFilter(logx.RidFilter())
        self.lines: list[str] = []

    def emit(self, record):
        try:
            self.lines.append(self.format(record))
        except Exception:  # noqa: BLE001
            pass

    def has(self, *keys):
        return any(all(k in ln for k in keys) for ln in self.lines)

    def find(self, *keys):
        return [ln for ln in self.lines if all(k in ln for k in keys)]


print("=" * 78)
print("后台 debug 日志验证")
print("=" * 78)

# ---------------------------------------------------------------- 1. 初始化
print("\n[1] 初始化与落文件")
from core import log as logx  # noqa: E402

logx.setup_logging("DEBUG")
root = logging.getLogger()
n1 = len(root.handlers)
logx.setup_logging("DEBUG")
check("setup_logging 幂等（不重复挂 handler）", n1 == len(root.handlers) and n1 >= 2,
      f"{n1} → {len(root.handlers)} 个 handler")
check("root 级别为 DEBUG", root.level == logging.DEBUG, logging.getLevelName(root.level))

path = logx.log_path()
check("日志文件已创建", bool(path and path.exists()), str(path))
backup = path.read_text(encoding="utf-8", errors="replace") if path and path.exists() else ""

from app import app  # noqa: E402  （import 时会再 setup 一次，仍应幂等）
check("import app 后 handler 数量不变", len(root.handlers) == n1, str(len(root.handlers)))

cap = Cap()
root.addHandler(cap)
check("启动行已写入文件", "日志已启动" in backup or cap.has("日志已启动"), "")

# ---------------------------------------------------------------- 2. 格式与打点
print("\n[2] 格式与耗时打点")
log = logx.get("verify")
log.debug("一行测试日志 关键字=Alpha")
check("DEBUG 能被记录", cap.has("一行测试日志", "Alpha"))
check("含级别与模块名", cap.has("DEBUG", "stockmining.verify"))

with logx.timer("验证-耗时打点") as t:
    time.sleep(0.02)
check("timer 输出耗时", cap.has("验证-耗时打点 耗时"), cap.find("验证-耗时打点")[-1][-40:])
check("timer 返回毫秒数", t.ms >= 15, f"{t.ms:.1f}ms")


@logx.timed("验证-装饰器")
def _slow():
    time.sleep(0.01)
    return 1


_slow()
check("timed 装饰器生效", cap.has("验证-装饰器 耗时"))

with logx.step("验证-step"):
    pass
check("step 上下文生效", cap.has("验证-step 耗时"))

logx.set_rid("abcd1234")
log.info("带请求 id")
logx.set_rid("-")
check("请求 id 进入日志", cap.has("[", "abcd1234") or cap.find("带请求 id") and
      any("abcd1234" in ln for ln in cap.find("带请求 id")),
      cap.find("带请求 id")[-1][:80] if cap.find("带请求 id") else "")

# ---------------------------------------------------------------- 3. SQL 日志
print("\n[3] SQL 明细 / 慢查询 / 失败")
from core import db  # noqa: E402

cap.lines.clear()
rows = db.query("SELECT 1 AS x")
check("SELECT 有明细日志（含 rows）", cap.has("SQL SELECT", "rows=1"), str(rows))

old_slow = logx.LOG_SLOW_MS
logx.LOG_SLOW_MS = 0
cap.lines.clear()
db.query("SELECT 2 AS y")
check("超过阈值触发慢查询 WARNING", cap.has("慢查询"), cap.find("慢查询")[-1][:90] if cap.find("慢查询") else "无")
logx.LOG_SLOW_MS = old_slow

cap.lines.clear()
try:
    db.query("SELECT * FROM __no_such_table__")
    check("坏 SQL 记 ERROR 并抛出", False, "没抛异常")
except Exception as e:  # noqa: BLE001
    check("坏 SQL 记 ERROR 并抛出", cap.has("SQL 失败") and cap.has("__no_such_table__"),
          f"{type(e).__name__}")

cap.lines.clear()
n = db.scalar("SELECT COUNT(*) FROM stock_basic")
check("scalar 也走 SQL 日志", cap.has("SQL SELECT"), f"stock_basic {n} 行")

# ---------------------------------------------------------------- 4. HTTP 请求日志
print("\n[4] 请求进出日志")
client = app.test_client()
cap.lines.clear()
r = client.get("/api/health")
check("/api/health 返回 200", r.status_code == 200, str(r.status_code))
check("记录请求进入 →", cap.has("→ GET /api/health"), cap.find("→ GET")[-1][:90] if cap.find("→ GET") else "无")
check("记录响应 ←（含状态码与耗时）",
      cap.has("← GET /api/health 200") and cap.has("ms"),
      cap.find("← GET")[-1][:90] if cap.find("← GET") else "无")
check("响应头带回 X-Request-Id", bool(r.headers.get("X-Request-Id")),
      str(r.headers.get("X-Request-Id")))

cap.lines.clear()
r2 = client.post("/api/pools", json={"name": "__日志测试池__", "base": "all",
                                     "conds": ["total_mv < 800000"]})
check("提交参数进日志（body 可见）", cap.has("__日志测试池__"), "")
check("保存动作记 INFO", cap.has("选股策略已保存"),
      cap.find("选股策略已保存")[-1][:90] if cap.find("选股策略已保存") else "无")
pid = None
if r2.status_code == 200:
    pid = json.loads(r2.data.decode())["data"]["id"]
    client.delete(f"/api/pools/{pid}")
    check("删除动作记 INFO", cap.has("选股策略已删除"), "")

# ---------------------------------------------------------------- 5. 任务日志
print("\n[5] 后台任务日志")
from core import jobs  # noqa: E402

cap.lines.clear()
jid = jobs.new_job("verify", "日志验证任务")
jobs.update(jid, 0.5, "阶段一", log="阶段一完成")
jobs.finish(jid, {"x": 1}, "完成")
check("任务创建记 INFO", cap.has("任务创建"))
check("任务进度 log 进 DEBUG", cap.has("阶段一完成"))
check("任务完成记 INFO 与耗时", cap.has("任务完成") and cap.has("耗时"))

jobs.new_job("verify", "失败任务")
cap.lines.clear()
jobs.fail(jid, "模拟失败")
check("任务失败记 ERROR", cap.has("任务失败"))

# ---------------------------------------------------------------- 6. tail / clear
print("\n[6] 日志查看与清空")
for i in range(30):
    log.info("tail 测试行 %02d", i)
log.error("tail 测试错误行")
lines = logx.tail(10)
check("tail 返回最后 N 行", len(lines) == 10, f"{len(lines)} 行")
check("tail 末尾是最新一行", "tail 测试错误行" in (lines[-1] if lines else ""),
      (lines[-1][-60:] if lines else "空"))
err_lines = logx.tail(50, level="ERROR")
check("tail 可按级别过滤", bool(err_lines) and all("ERROR" in ln for ln in err_lines),
      f"{len(err_lines)} 行")
grep_lines = logx.tail(50, grep="tail 测试行 07")
check("tail 可按关键字过滤", len(grep_lines) == 1, f"{len(grep_lines)} 行")

check("clear 清空文件", logx.clear() and len(logx.tail(100)) == 0,
      f"剩余 {len(logx.tail(100))} 行")
if backup:
    with open(path, "w", encoding="utf-8") as f:
        f.write(backup)
    check("恢复原日志内容", len(logx.tail(100)) > 0 or not backup.strip(), "")

# ---------------------------------------------------------------- 7. 级别切换 + 工作台无日志入口
print("\n[7] 级别切换（模块级）与工作台不暴露日志")
root = logging.getLogger()
n_before = len(root.handlers)   # 含本脚本自己挂的内存 handler
logx.setup_logging("INFO")
check("切到 INFO 成功", root.level == logging.INFO, logging.getLevelName(root.level))
logx.setup_logging("NOPE")
check("非法级别退回默认（不报错）", root.level == logging.INFO,
      logging.getLevelName(root.level))
check("级别切换不重复挂 handler", len(root.handlers) == n_before,
      f"{n_before} → {len(root.handlers)}")
logx.setup_logging("DEBUG")
check("切回 DEBUG", root.level == logging.DEBUG)

r4 = client.get("/logs")
check("工作台已无日志页（404）", r4.status_code == 404, str(r4.status_code))
check("导航栏已无「运行日志」", "运行日志" not in client.get("/").data.decode("utf-8", "ignore"), "")
check("已无 /api/logs 接口（404）", client.get("/api/logs").status_code == 404, "")
check("已无 /api/log/level 接口（404）",
      client.post("/api/log/level", json={"level": "INFO"}).status_code == 404, "")

# ---------------------------------------------------------------- 8. HTTP 语义错误
print("\n[8] 404/405 与 favicon（不该被当成未捕获异常）")
cap.lines.clear()
rf = client.get("/favicon.ico")
check("GET /favicon.ico 返回 200", rf.status_code == 200, str(rf.status_code))
check("favicon 是图片", (rf.headers.get("Content-Type") or "").startswith("image/"),
      str(rf.headers.get("Content-Type")))
check("favicon 不再记 ERROR", not cap.has("未捕获异常"), "")

cap.lines.clear()
r404 = client.get("/api/__no_such_api__")
check("未知 API 返回 404（不是 500）", r404.status_code == 404, str(r404.status_code))
d404 = json.loads(r404.data.decode())
check("404 仍是 JSON 且带 code", d404.get("ok") is False and d404.get("code") == 404,
      str(d404))
check("404 不写 ERROR 日志", not cap.has("未捕获异常"), "")

cap.lines.clear()
rp404 = client.get("/__no_such_page__")
check("未知页面返回 404（不是 500）", rp404.status_code == 404, str(rp404.status_code))
check("页面 404 不写 ERROR 日志", not cap.has("未捕获异常"), "")

r405 = client.delete("/api/health")
check("方法不允许返回 405（不是 500）", r405.status_code == 405, str(r405.status_code))


def _boom():
    raise RuntimeError("故意炸一下")


# 注意：app.add_url_rule 在首个请求之后会断言失败（Flask 的 setup 保护），
# 所以直接往 url_map 里塞一条规则，用来验证"真异常仍走 500 + traceback"。
from werkzeug.routing import Rule  # noqa: E402

app.url_map.add(Rule("/__boom__", endpoint="_boom"))
app.view_functions["_boom"] = _boom
cap.lines.clear()
rb = client.get("/__boom__")
check("真异常仍然 500", rb.status_code == 500, str(rb.status_code))
check("真异常仍然记 ERROR + traceback",
      cap.has("未捕获异常") and cap.has("故意炸一下") and cap.has("Traceback"), "")
app.view_functions.pop("_boom", None)
# 路由规则不清理：本脚本跑完即退出，且 Flask 禁止首个请求后再改 url_map。

# ---------------------------------------------------------------- 结果
print("\n" + "=" * 78)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
for f in FAIL:
    print("  未通过:", f)
print("=" * 78)
sys.exit(1 if FAIL else 0)
