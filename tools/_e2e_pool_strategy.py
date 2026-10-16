# -*- coding: utf-8 -*-
"""选股策略端到端验证：走 HTTP API，覆盖「管理页 CRUD → 回测/选股引用」全链路。

跑法：
    python tools/_e2e_pool_strategy.py

依赖：服务已在 127.0.0.1:8777 运行。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8777"
PASS: list[str] = []
FAIL: list[str] = []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"  — {detail}" if detail else ""))


def get(path):
    try:
        r = urllib.request.urlopen(BASE + path, timeout=30)
        return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())   # 404 也带 {ok:false}，交给断言判断


def post(path, payload):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=300)
        return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())


def delete(path):
    req = urllib.request.Request(BASE + path, method="DELETE")
    try:
        r = urllib.request.urlopen(req, timeout=30)
        return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())


print("=" * 78)
print("选股策略端到端验证（HTTP）")
print("=" * 78)

# 买卖策略 id：**从库里取**（老脚本写死 strategy_id=1，两策略分离后那行已被迁移删除）。
# 库里一条都没有就先建一条临时买卖策略。
_strs = get("/api/strategies").get("data") or []
if not _strs:
    post("/api/strategies", {"name": "__e2e_pool_strategy__", "config": {
        "buy_trigger": ["open > ref(close, 1)"], "sell_signal": ["open < ref(close, 1)"]}})
    _strs = get("/api/strategies").get("data") or []
SID = _strs[0]["id"]
print("使用买卖策略 #%s %s" % (SID, _strs[0].get("name")))

# ------------------------------------------------------------ 1. 列表与内置
print("\n[1] 列表与内置策略")
pools = get("/api/pools")
check("列表接口可用", isinstance(pools.get("data"), list), "")
lst = pools["data"]
# 2026-09-29 起内置只剩「首板」一条（预设在库里都是**普通**策略，可删可改）——
# 断言「恰好等于白名单」而不是「>= 6」，免得以后内置又悄悄长回来。
check("内置策略已就位（当前只有「首板」）",
      [p["name"] for p in lst if p["builtin"]] == ["首板"],
      f"{len(lst)} 条：" + "、".join(p["name"] for p in lst[:4]))
small = next(p for p in lst if p["name"] == "小市值")
# cond_count 跟随库内容（内置池在界面上改得动），别写死成 1
check("列表项含结构化描述",
      small["cond_count"] == len(small["conds"]) and small["cond_count"] >= 1
      and small["base_label"] and "小市值" in small["label"],
      json.dumps({k: small[k] for k in ("name", "cond_count", "base_label", "label")},
                 ensure_ascii=False))

# ------------------------------------------------------------ 2. CRUD
print("\n[2] 增删改查")
uniq = f"E2E池-{int(time.time())}"
r = post("/api/pools", {"name": uniq, "note": "端到端建的", "base": "all",
                        "conds": ["total_mv < 800000", "turnover_rate > 2"],
                        "pool_exit": False})
check("新建成功", r.get("ok") and r["data"]["id"], str(r.get("error") or r["data"]["id"]))
pid = r["data"]["id"]
check("新建后立即可读", get("/api/pools/%d" % pid)["data"]["name"] == uniq)

r = post("/api/pools", {"id": pid, "name": uniq, "note": "改了", "base": "000300.SH",
                        "conds": ["total_mv < 900000"], "pool_exit": True})
check("更新成功", r.get("ok") and r["data"]["base"] == "000300.SH" and r["data"]["pool_exit"],
      str(r["data"].get("base")))

bad = post("/api/pools", {"name": "坏池", "base": "all", "conds": ["total_mvv < 5"]})
check("错误字段被拒", not bad.get("ok") and "total_mvv" in str(bad.get("error")),
      str(bad.get("error"))[:60])
v = post("/api/pools/validate", {"name": "x", "base": "all", "conds": ["total_mv < 5"]})
check("校验接口（不落库）通过", v.get("ok") and v["data"]["valid"], "")

# ------------------------------------------------------------ 3. 试跑
print("\n[3] 试跑（管理页「看当日命中」）")
t = post("/api/universe/test", {"universe": {"pool_id": pid}, "trade_date": "2023-06-30",
                                "sample": 3})
check("按 id 试跑可用", t.get("ok"), str(t.get("error"))[:80])
if t.get("ok"):
    d = t["data"]
    check("试跑基础池用的是策略的 base（沪深300）", d["base_size"] < 500,
          f"base_size={d['base_size']} 入池={d['hit_count']}")

# ------------------------------------------------------------ 4. 回测引用
# 用预设「小市值」跑（2026-09-29 起它已是**普通**策略，不再是内置）：票多，能真正产生交易。上面那条 base=000300.SH 的自建池
# 只筛出 2 只票，本来就该是 0 笔，不适合验证「引用生效」。
print("\n[4] 回测引用选股策略")
small_pid = small["id"]
job = post("/api/backtest/start", {
    "strategy_id": SID,
    "universe": {"pool_id": small_pid},
    "start": "2023-01-03", "end": "2023-06-30",
    "params": {"init_cash": 1000000, "max_positions": 10, "position_pct": 10},
})
check("回测任务已创建", job.get("ok") and job["data"].get("job_id"), str(job.get("error"))[:80])
jid = job["data"].get("job_id")

deadline = time.time() + 600
res = None
while time.time() < deadline:
    j = get("/api/jobs/%s" % jid)["data"]
    if j.get("status") in ("done", "error"):
        res = j
        break
    time.sleep(2)
check("回测跑完", res and res.get("status") == "done",
      str(res.get("status")) + " " + str(res.get("error", ""))[:80])
rid = (res.get("result") or {}).get("run_id") if res else None
if rid:
    run = get("/api/backtest/runs/%d" % rid)["data"]
    check("回测记录股票池标签是策略名", "小市值" in (run.get("universe") or ""),
          str(run.get("universe")))
    trades = get("/api/backtest/runs/%d/trades" % rid)["data"]
    check("产生了交易", len(trades) > 0, f"{len(trades)} 笔")

# ------------------------------------------------------------ 5. 选股引用
print("\n[5] 选股引用选股策略")
sc = post("/api/screen/run", {"strategy_id": SID, "universe": {"pool_id": small_pid},
                              "trade_date": "2023-06-30", "limit": 30, "save": False})
check("选股接口可用", not sc.get("error"), str(sc.get("error"))[:80])
if sc.get("ok"):
    d = sc["data"]
    check("选股结果带池子标签", "小市值" in str(d.get("pool") or ""), str(d.get("pool"))[:70])

# ------------------------------------------------------------ 6. 删除与降级
print("\n[6] 删除与引用降级")
bi = next(p for p in lst if p["builtin"])
dres = delete("/api/pools/%d" % bi["id"])
check("内置策略删不掉", not dres.get("ok"), str(dres.get("error"))[:50])
dres = delete("/api/pools/%d" % pid)
check("自建策略可删除", dres.get("ok"), str(dres.get("error"))[:50])
gone = get("/api/pools/%d" % pid)
check("删除后读取返回 404", not gone.get("ok"), "")

after = post("/api/backtest/start", {
    "strategy_id": SID, "universe": {"pool_id": pid},
    "start": "2023-01-03", "end": "2023-03-31",
    "params": {"init_cash": 1000000, "max_positions": 5, "position_pct": 20},
})
# 期望：明确报错，而不是静默退化成全市场 —— 后者会让「以为在跑小市值池」的
# 回测悄悄跑在全市场上，结果完全不可信。
msg = str(after.get("error") or "")
check("引用已删除策略：明确报错而非静默跑全市场",
      (not after.get("ok")) and "已不存在" in msg, msg[:80])

# ------------------------------------------------------------ 7. 页面
print("\n[7] 页面可访问")
for p in ["/pools", "/backtest", "/screen", "/strategies"]:
    try:
        r = urllib.request.urlopen(BASE + p, timeout=20)
        check("页面 %s" % p, r.status == 200, str(r.status))
    except Exception as e:
        check("页面 %s" % p, False, str(e)[:60])

print("\n" + "=" * 78)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
for f in FAIL:
    print("  未通过:", f)
print("=" * 78)
sys.exit(1 if FAIL else 0)
