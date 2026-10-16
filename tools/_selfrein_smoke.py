# -*- coding: utf-8 -*-
"""收尾冒烟：确认新池/新策略在**接口层**可读、可引用，且三条记录都在。

只读，不改库。
"""
import json
import os
import sys
import urllib.request

BASE = "http://127.0.0.1:8777"


def get(p):
    with urllib.request.urlopen(BASE + p, timeout=30) as r:
        return json.loads(r.read().decode())


pools = get("/api/pools").get("data") or []
p284 = [p for p in pools if int(p.get("id", -1)) == 284]
print("池 #284 在 /api/pools:", bool(p284))
if p284:
    p = p284[0]
    print("   name =", p.get("name"), "| builtin =", p.get("builtin"))
    print("   base =", p.get("base"), "| conds =", p.get("conds"))

st = get("/api/strategies").get("data") or []
s231 = [s for s in st if int(s.get("id", -1)) == 231]
print("策略 #231 在 /api/strategies:", bool(s231))
if s231:
    s = s231[0]
    print("   name =", s.get("name"), "| builtin =", s.get("builtin"))
    cfg = s.get("config") or {}
    if isinstance(cfg, str):
        cfg = json.loads(cfg)
    print("   risk =", cfg.get("risk"))
    print("   exec =", cfg.get("exec"))
    print("   buy_trigger =", cfg.get("buy_trigger"))

runs = get("/api/backtest/runs").get("data") or []
got = {int(r["id"]): r for r in runs if int(r["id"]) in (154, 155, 156)}
print("三条记录在 /api/backtest/runs:", sorted(got))
for rid in (154, 155, 156):
    r = got.get(rid)
    if not r:
        print("   !! #%s 不在列表前 20 条里，直接取详情" % rid)
        r = get("/api/backtest/runs/%d" % rid).get("data") or {}
    m = r.get("metrics") or {}
    if isinstance(m, str):
        m = json.loads(m)
    print("   #%s %-28s 状态=%s 收益=%s%% 笔数=%s" % (
        rid, (r.get("name") or "")[:28], r.get("status"),
        m.get("total_return"), m.get("trade_count")))

# 池条件能否真求值（预筛/试跑口径）——这是「加了条件会不会静默 0 命中」的那道闸
d = get("/api/backtest/runs/154").get("data") or {}
print("记录 #154 的池快照 =", json.dumps(d.get("universe_spec"), ensure_ascii=False))
print("OK")
