# -*- coding: utf-8 -*-
"""端到端：用「条件股票池」对象真跑一次回测与选股，确认前端发出的 spec 后端能吃下。

跑法（需服务在 8777 运行）：
    "...python.exe" tools/_e2e_pool.py
"""
import json
import sys
import time
import urllib.error
import urllib.request

B = "http://127.0.0.1:8777"
PASS, FAIL = [], []


def check(name, ok, extra=""):
    (PASS if ok else FAIL).append(name)
    print("  %s %s%s" % ("OK  " if ok else "FAIL", name, ("  " + extra) if extra else ""))


def post(p, d):
    req = urllib.request.Request(
        B + p, data=json.dumps(d).encode(), headers={"Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=300).read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())


def get(p):
    return json.loads(urllib.request.urlopen(B + p, timeout=60).read().decode())


def wait(jid, limit=900):
    t0 = time.time()
    while time.time() - t0 < limit:
        j = get("/api/jobs/" + jid)
        d = j.get("data", j)
        st = d.get("status")
        if st in ("done", "error", "cancelled"):
            return d
        time.sleep(2)
    return {"status": "timeout"}


print("=" * 74)
print("端到端：条件股票池 → 回测 / 选股")
print("=" * 74)

# 买卖策略 id：**从库里取**（老脚本写死 strategy_id=1，两策略分离后那行已被迁移删除）。
# 库里一条都没有就先建一条临时买卖策略。
_strs = get("/api/strategies").get("data") or []
if not _strs:
    post("/api/strategies", {"name": "__e2e_pool__", "config": {
        "buy_trigger": ["open > ref(close, 1)"], "sell_signal": ["open < ref(close, 1)"]}})
    _strs = get("/api/strategies").get("data") or []
SID = _strs[0]["id"]
print("使用买卖策略 #%s %s" % (SID, _strs[0].get("name")))

# ---------------------------------------------------------------- 回测
print("\n### 1. 回测：沪深300 里只做小市值（total_mv < 300亿）")
r = post("/api/backtest/start", {
    "strategy_id": SID,
    "universe": {"base": "000300.SH", "where": ["total_mv < 8000000"], "pool_exit": False},
    "start": "2023-01-03", "end": "2023-06-30",
    "params": {"init_cash": 1000000, "max_positions": 10, "position_pct": 10},
})
if not r.get("ok"):
    check("提交回测", False, str(r.get("error")))
else:
    jid = r["data"].get("job_id")
    check("提交回测", True, "job=" + str(jid))
    d = wait(str(jid))
    check("回测完成", d.get("status") == "done", str(d.get("status")) + " " + str(d.get("error", "")))
    res = d.get("result") or {}
    print("      result keys:", sorted(res.keys()))
    print("      job trade_count:", res.get("trade_count"),
          " metrics.trade_count:", (res.get("metrics") or {}).get("trade_count"))
    print("      回测日志尾:", (res.get("logs") or d.get("logs") or [])[-3:])
    rid = d.get("run_id") or (d.get("result") or {}).get("run_id")
    if rid:
        run = get("/api/backtest/runs/%s" % rid)["data"]
        check("回测记录股票池标签可读", "小市值" in (run.get("universe") or "") or "条件池" in (run.get("universe") or ""),
              "universe=" + str(run.get("universe")))
        # 详情接口只在 metrics 里有 trade_count，交易笔数以 trades 接口为准
        trades = get("/api/backtest/runs/%s/trades" % rid)["data"]
        m = run.get("metrics") or {}
        check("产生了交易", len(trades) > 0, "trades=%s metrics.trade_count=%s"
              % (len(trades), m.get("trade_count")))
        check("trades 与 metrics 一致", len(trades) == m.get("trade_count"),
              "%s vs %s" % (len(trades), m.get("trade_count")))
        print("      日志:", " | ".join((run.get("log") or "").split("\n")[:2]))
    else:
        check("拿到 run_id", False, json.dumps(d)[:200])

# ---------------------------------------------------------------- 出池即卖
print("\n### 2. 回测：BOLL 上轨池 + 掉出即卖")
r = post("/api/backtest/start", {
    "strategy_id": SID,
    "universe": {"base": "000300.SH", "where": ["close >= boll_upper"], "pool_exit": True},
    "start": "2023-01-03", "end": "2023-06-30",
    "params": {"init_cash": 1000000, "max_positions": 10, "position_pct": 10},
})
if r.get("ok"):
    d = wait(str(r["data"].get("job_id")))
    check("回测完成", d.get("status") == "done", str(d.get("status")))
    rid = d.get("run_id") or (d.get("result") or {}).get("run_id")
    if rid:
        trades = get("/api/backtest/runs/%s/trades" % rid)["data"]
        reasons = {}
        for t in trades:
            k = t.get("sell_reason") or "?"
            reasons[k] = reasons.get(k, 0) + 1
        check("产生了交易", len(trades) > 0, "%s 笔" % len(trades))
        check("离场原因含「掉出池子」", "pool_out" in reasons, str(reasons))
        out = [t for t in trades if t.get("sell_reason") == "pool_out"]
        check("掉出池子的笔数 > 0", len(out) > 0,
              ("示例：" + str(out[0]["ts_code"]) + " " + str(out[0]["buy_date"])
               + "→" + str(out[0]["sell_date"])) if out else "")

# ---------------------------------------------------------------- 选股
print("\n### 3. 选股：全市场小市值 + 高换手池")
r = post("/api/screen/run", {
    "strategy_id": SID,
    "universe": {"base": "all", "where": ["total_mv < 800000", "turnover_rate > 3"]},
    "trade_date": "2023-06-30", "limit": 50, "save": False,
})
if not r.get("ok"):
    check("提交选股", False, str(r.get("error")))
else:
    d = r["data"]
    check("选股返回", True, "扫描 %s 只 / 命中 %s 只" % (d.get("scanned"), d.get("count")))
    # 80亿+换手>3 不是内置预设，标签应回退成「条件池(2条) · 首条条件」而不是乱码
    lab = d.get("universe") or ""
    check("池子标签带条件说明", "条件池" in lab and "total_mv" in lab, lab)
    # 基础池是全市场 5901，预筛后应明显更小
    check("预筛大幅缩小加载范围", (d.get("scanned") or 0) < 3000,
          "scanned=%s" % d.get("scanned"))

# ---------------------------------------------------------------- 兼容性
print("\n### 4. 向后兼容：字符串股票池仍然可用")
r = post("/api/screen/run", {
    "strategy_id": SID, "universe": "000300.SH",
    "trade_date": "2023-06-30", "limit": 20, "save": False,
})
check("字符串池子照常工作", bool(r.get("ok")), str(r.get("error") or ("命中 %s" % (r.get("data") or {}).get("count"))))

r = post("/api/screen/run", {
    "strategy_id": SID, "universe": {"base": "all", "where": ["total_mvv < 5"]},
    "trade_date": "2023-06-30", "limit": 20, "save": False,
})
check("未知字段被拒绝且给出原因", (not r.get("ok")) and "未知字段" in str(r.get("error")),
      str(r.get("error"))[:80])

print("\n" + "=" * 74)
print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  未通过:", f)
print("=" * 74)
sys.exit(1 if FAIL else 0)
