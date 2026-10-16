# -*- coding: utf-8 -*-
"""「某日选出 N 只、回测挑哪 max_positions 只」的真值回归（离线，不用起服务 / 不碰数据库）。

为什么单独立一个：这套挑票规则**没有任何界面参数能直接看到**，而且有三处反直觉行为
（见下面每条断言的中文说明）。它们全部藏在 `core/backtest.run_backtest` 的日循环里，
改引擎时极容易无声漂移 —— 之前只靠人读代码，没有断言。

做法：喂**合成 data**（结构对齐 `pack_one` 的产物）给线上引擎本身，断言真实成交结果。
不重写一遍撮合逻辑来对拍 —— 那种测试改坏了两边一起错。

用法：`$PY -W ignore tools/verify_pick.py`（退出码 0 = 全通过）
"""
import sys
import os
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import backtest as bt                       # noqa: E402
from core import strategy as st                       # noqa: E402

DATES = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]
OK, BAD = 0, 0


def check(name, ok, extra=""):
    global OK, BAD
    if ok:
        OK += 1
        print("  OK   " + name)
    else:
        BAD += 1
        print("  FAIL " + name + (("   → " + str(extra)) if extra else ""))


def mk(n, score="none", entry_day=0, trig_day=1, price=10.0, reverse=False):
    """n 只票：entry_day 命中（进候选）、trig_day 满足买入触发。

    score: 'none' = 不设评分（引擎里落成 NaN → 当 0；**只有策略设了 score_expr 才有值**）
           'up'   = 装载序越靠后分越高（用来证明"按评分、不按顺序/代码"）
    reverse: 装载顺序倒过来（证明"顺序"用的是**装载序**，不是代码序）
    """
    data = {}
    keys = list(range(n))
    if reverse:
        keys = keys[::-1]
    for k in keys:
        code = "%06d.SZ" % k
        L = len(DATES)
        d = {
            "name": "票%02d" % k,
            # ⚠ dtype=object：pack_one 走 pandas strftime().to_numpy()，元素是 Python str；
            #    用 np.array(DATES) 会得到 np.str_，下游 pd.Timestamp() 直接 TypeError。
            "dates": np.array(DATES, dtype=object),
            "open": np.full(L, price), "high": np.full(L, price),
            "low": np.full(L, price), "close": np.full(L, price),
            "entry": np.zeros(L, bool), "in_pool": np.ones(L, bool),
            "buy_trigger": np.zeros(L, bool), "sell_signal": np.zeros(L, bool),
            "score": np.full(L, np.nan),
        }
        d["entry"][entry_day] = True
        d["buy_trigger"][trig_day] = True
        if score == "up":
            d["score"][:] = k + 1.0
        elif score == "zero":
            d["score"][:] = 0.0
        d["idx"] = {x: i for i, x in enumerate(DATES)}
        data[code] = d
    return data


PROG = []          # 最近一次 run() 收到的进度回调序列（【8】要断言它的收尾）


def run(data, **params):
    cfg = st.merge_config({"name": "verify_pick", "buy_trigger": "", "risk": {}, "exec": {}})
    PROG.clear()
    return bt.run_backtest(data, cfg, DATES[0], DATES[-1], params=params,
                           progress=lambda msg, pr: PROG.append((float(pr), str(msg))))


def bought(r, day):
    """当天的买入（入库序 = 引擎买入顺序）。⚠ 返回的是 dict（已序列化）。"""
    return [t["ts_code"][:6] for t in r["trades"] if t["buy_date"] == day]


print("【1】挑票主规则：候选多于名额 → 按「评分」降序取前 max_positions")
r = run(mk(20, "up"), max_positions=10)
b = bought(r, DATES[1])
check("20 只候选买 10 只", len(b) == 10, b)
check("买的是评分最高的 10 只（装载序越靠后分越高 → 应买 19…10）",
      b == ["%06d" % k for k in range(19, 9, -1)], b)
check("买入顺序 = 评分降序（不是代码升序）",
      b == sorted(b, reverse=True), b)

print("\n【2】没设 score_expr 时：评分全为 0 → 排序退化为「装载顺序」，**不是按代码**")
r = run(mk(20, "none", reverse=True), max_positions=10)
b = bought(r, DATES[1])
# 装载序是倒着的：19,18,…,0 → 无评分时买"最先装载"的 19…10
check("无评分 → 买装载序前 10（19…10）", b == ["%06d" % k for k in range(19, 9, -1)], b)
check("显式 0 分与不设评分同行为",
      bought(run(mk(20, "zero", reverse=True), max_positions=10), DATES[1]) == b)

print("\n【3】max_positions 是买入名额上限，不保证买满")
r = run(mk(20, "up"), max_positions=5)
check("max_positions=5 → 只买 5 只", len(bought(r, DATES[1])) == 5, bought(r, DATES[1]))
r = run(mk(3, "up"), max_positions=10)
check("候选只有 3 只 → 就买 3 只（名额不用满）", len(bought(r, DATES[1])) == 3)

print("\n【4】⚠ 隐藏上限 max_candidates=100（界面不可配）：候选 >100 时后面的永远买不到")
r = run(mk(150, "none"), max_positions=150, position_pct=0, init_cash=1e9)
b = bought(r, DATES[1])
check("候选 150 / 名额 150 / 资金充足 → 仍只买 100 只", len(b) == 100, len(b))
check("末位 = 装载序第 100 只（000099）", b[-1] == "000099", b[-1])
r = run(mk(150, "up"), max_positions=150, position_pct=0, init_cash=1e9)
b = bought(r, DATES[1])
check("有评分时被截的是「评分第 101~150 名」（末位 = 000050）", b[-1] == "000050", b[-1])

print("\n【5】买入时机：entry 命中日的**次日**才有机会；买入触发只认那一天")
r = run(mk(20, "up", trig_day=1), max_positions=10)
check("entry=day0 / 触发=day1 → day1 买 10 只", len(bought(r, DATES[1])) == 10)
r = run(mk(20, "up", trig_day=2), max_positions=10)
check("⚠ 触发=day2（隔一天）→ 一笔不买（候选只在次日有效，隔日作废）",
      len(bought(r, DATES[2])) == 0, bought(r, DATES[2]))
r = run(mk(20, "up", entry_day=4), max_positions=10)
check("⚠ 区间最后一天才选出来的票没有次日 → 一笔不买",
      sum(len(bought(r, d)) for d in DATES) == 0)

print("\n【6】资金与手数：单只预算 = init_cash × position_pct%（按**初始资金**算，不是当前现金）")
r = run(mk(20, "up", price=100.0), max_positions=10, init_cash=50000, position_pct=0)
check("预算 5000 元 ÷ 100 元 = 50 股 < 一手 → 直接跳过，不买碎股",
      len(bought(r, DATES[1])) == 0, bought(r, DATES[1]))
# 默认 position_pct=10 → 每只吃掉 10% 初始资金，10 只就见底；第 11 只只能"捡剩"
r = run(mk(150, "none"), max_positions=120)
tr = [t for t in r["trades"] if t["buy_date"] == DATES[1]]
check("⚠ 名额 120 也买不到 120：钱花完就停（实测 11 只）", len(tr) == 11, len(tr))
check("最后一只是残仓（钱不够一整份预算）", tr and tr[-1]["shares"] < tr[0]["shares"],
      "%s vs %s" % (tr[-1]["shares"] if tr else "-", tr[0]["shares"] if tr else "-"))

print("\n【7】真实回测佐证（回测 #39 / max_positions=10 / 每日最多 10 笔）")
try:
    from core import db
    rows = db.query("SELECT buy_date, COUNT(*) n FROM backtest_trade "
                    "WHERE run_id=39 GROUP BY buy_date")
    mx = max(r["n"] for r in rows)
    check("历史每日买入笔数从未超过 max_positions=10", mx <= 10, mx)
    day = max(rows, key=lambda r: r["n"])["buy_date"]
    tr = db.query("SELECT ts_code FROM backtest_trade WHERE run_id=39 AND buy_date=%s "
                  "ORDER BY id", (day,))
    cs = [t["ts_code"][:6] for t in tr]
    check("满仓日的买入顺序是代码升序（= 装载序，说明这些策略没设评分）",
          cs == sorted(cs) and len(cs) == 10, cs)
except Exception as e:                                    # noqa: BLE001
    print("  SKIP 跳过数据库佐证（%s）" % e)

print("\n【8】进度回调契约：单调不减、**最后一帧必须回到 1.0**")
# 为什么单独立一条：调用方（app.py）把这根 0~1 线性映射到自己的进度区间（0.62 + 0.36*pr）。
# 引擎不发最后那一帧，映射出来就**永远差一截** —— 实测表现为「进度条卡在 97%，
# 而库里那条记录已经是 done」（2026-09-20 报的）。不锁住，改引擎时又会悄悄漂回去。
run(mk(5, "up"), max_positions=2)
seq = [p for p, _ in PROG]
check("进度回调确实被调用过", len(seq) >= 2, len(seq))
check("进度单调不减", all(b >= a for a, b in zip(seq, seq[1:])), seq)
check("最后一帧 == 1.0（进度空间必须回到 1，不能停在 0.98）",
      bool(seq) and seq[-1] == 1.0, seq[-3:])
check("最后一帧的说明是「回测完成」",
      bool(PROG) and PROG[-1][1] == "回测完成", PROG[-1] if PROG else None)

print("\n" + "=" * 60)
print("挑票规则回归：%d/%d 通过，%d 失败" % (OK, OK + BAD, BAD))
sys.exit(1 if BAD else 0)
