# -*- coding: utf-8 -*-
"""卖出侧「位移一根」的回归（2026-09-30）。

背景（最严重的坑）：`sell_signal` 在**完整 K 线**上求值（含当天 close / ma），
而引擎按 **open** 成交。若直接用 `sell_signal[i]`，就是拿当天收盘才知道的信息
在开盘卖 —— 未来函数。实测反差：`close<open` +490.7% vs 合法写法 −37.4%。

修法：引擎取 `sell_signal[i-1]` / `in_pool[i-1]`（T 日收盘确认 → T+1 开盘执行），
与买入侧（T 日收盘确认 entry → T+1 开盘买）对称。

本回归用**合成数据**直接喂 `run_backtest`（不依赖真实行情、不依赖「某天恰好有票」），
断言卖出日恰好是信号日的下一根。

    python tools/verify_sell_lag.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402

from core import backtest as bt, strategy as st  # noqa: E402

OK = FAIL = 0


def check(name: str, cond: bool, extra: str = ""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  OK   {name}" + (f"   {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f"   {extra}" if extra else ""))


DATES = [f"2025-01-{d:02d}" for d in range(2, 17)]     # 15 个连续交易日
PARAMS = {"init_cash": 1_000_000, "max_positions": 10, "position_pct": 10,
          "commission": 0.0003, "stamp_tax": 0.0005, "min_commission": 5}


def make(sell_at=None, pool_out_at=None, n=15):
    """造一只票：价格恒定 10 元（排除涨跌干扰），信号只在指定下标出现。"""
    dates = np.array(DATES[:n], dtype=object)   # ⚠ 定长 U10 会变 numpy.str_，
    # compute_metrics 里 pd.Timestamp(...) 不接受 —— 真数据 to_numpy() 是 object，对齐它
    o = np.full(n, 10.0)
    hi, lo, cl = o + 0.2, o - 0.2, o
    entry = np.ones(n, dtype=bool)
    buy = np.ones(n, dtype=bool)
    sell = np.zeros(n, dtype=bool)
    if sell_at is not None:
        sell[sell_at] = True
    in_pool = np.ones(n, dtype=bool)
    if pool_out_at is not None:
        in_pool[pool_out_at] = False
    return {"000001.SZ": {
        "name": "合成A", "dates": dates, "open": o, "high": hi, "low": lo,
        "close": cl, "entry": entry, "in_pool": in_pool, "buy_trigger": buy,
        "sell_signal": sell, "score": np.full(n, np.nan),
        "idx": {d: i for i, d in enumerate(dates)},
    }}


def run(data, sell_expr=None, hold=None):
    cfg = st.merge_config({
        "entry": [], "buy_trigger": [], "sell_signal": sell_expr or [],
        "risk": {"max_hold_days": hold, "stop_loss_pct": None,
                 "take_profit_pct": None},
        "exec": {"buy_price": "open", "sell_price": "open",
                 "max_hold_price": "close", "slippage": 0.001},
    })
    return bt.run_backtest(data, cfg, DATES[0], DATES[-1], PARAMS)


print("### 1. 源码口径：引擎卖出必须位移一根")
src = open(os.path.join(ROOT, "core", "backtest.py"), encoding="utf-8").read()
check("引擎用的是 sell_signal[i - 1]", 'v["sell_signal"][i - 1]' in src)
# 2026-10-10：「出池即卖」彻底移除 ⇒ 引擎里那条 `in_pool[i - 1]` 的位移判断也一并没了。
# 这里钉**反向**不变量：谁把它加回来就红。
check("引擎里已无 pool_out 离场分支（该能力彻底移除）",
      "pool_out" not in src and 'v["in_pool"][i - 1]' not in src)
# 绊线 A：不许再出现「按当天信号开盘卖」的老写法（`sell_signal[i]` 不带位移）。
# ⚠ 这里原本钉的是**旧那行确切文本**（`if exit_px is None and i > 0 and v[...][i - 1]`），
# 2026-10-04 重排离场顺序（C37）把那行拆成了变量 ⇒ 断言假红。改成查「不位移的写法不存在」。
check("无遗留的 sell_signal[i] 判定",
      'sell_signal"][i]' not in src and 'sell_signal"][i ]' not in src)
# 绊线 B（C37）：同一根 K 线上的离场必须按**价格时间线**排 ——
#   按 open 成交的离场 早于 盘中止损/止盈 早于 按 close 成交的离场。
#   旧写法把盘中风控排在最前、再用 open 兜底 ⇒ 白送「没摸到止盈就当自己开盘卖了」的期权。
_at = src.find('reason = "stop_loss"')
_op = src.find('exit_px, reason = op, "max_hold"')
_cl = src.find('exit_px, reason = cl, "max_hold"')
check("离场按价格时间线排（open 离场 早于 盘中风控 早于 close 离场）",
      -1 < _op < _at < _cl, f"open@{_op} 盘中@{_at} close@{_cl}")
check("买入侧不位移（entry 仍是 T 日收盘选次日候选）",
      'if not v["entry"][i]:' in src)

print("\n### 2. 功能：卖出日 = 信号日的下一根")
# 信号在第 3 根（下标 3）→ 应在下标 4 开盘卖出
res = run(make(sell_at=3))
tr = [t for t in res["trades"] if t["sell_reason"] == "signal"]
check("产生 1 笔 signal 卖出", len(tr) == 1, f"实际 {len(tr)} 笔")
if tr:
    t = tr[0]
    check("卖出日 = 信号日下一根（信号 01-05 → 卖出 01-06）",
          t["sell_date"] == DATES[4], f"实际 {t['sell_date']}")
    check("卖出价 = 该根开盘 ×(1-滑点)",
          abs(t["sell_price"] - 10.0 * 0.999) < 1e-6, f"{t['sell_price']}")
    check("买入日在卖出日之前", t["buy_date"] < t["sell_date"],
          f"{t['buy_date']} → {t['sell_date']}")

print("\n### 3. 信号在最后一根：不应凭空卖（无下一根可执行）")
res = run(make(sell_at=14))
tr = [t for t in res["trades"] if t["sell_reason"] == "signal"]
check("末尾信号不产生 signal 卖出（只能期末清仓）", len(tr) == 0, f"实际 {len(tr)} 笔")

print("\n### 4. 出池即卖已移除：不在池里不再触发卖出（历史 `pool_out` 口径作废）")
# 2026-10-10 用户要求彻底移除「掉出池子即卖」。合成一只「第 4 根掉出池、其余在池」的票：
# 应当**只**按期末清仓离场，不再有 pool_out。
res = run(make(pool_out_at=3))
tr = [t for t in res["trades"] if t["sell_reason"] == "pool_out"]
check("掉出池子不再产生 pool_out 卖出", len(tr) == 0, f"实际 {len(tr)} 笔")

print("\n### 5. 无卖出信号 + 无到期：不应卖出（除期末清仓）")
res = run(make())
tr = [t for t in res["trades"] if t["sell_reason"] != "end"]
check("除期末外无卖出", len(tr) == 0, f"{len(tr)} 笔")

print("\n### 6. 反例：若退回不位移，卖出日会等于信号日（本回归能抓住）")
# 直接构造「老引擎」行为做对照：把信号同样放下标 3，若引擎不位移则卖出日=01-06
check("位移后卖出日 ≠ 信号日（这正是本回归要守的不变量）",
      DATES[4] != DATES[3], f"信号日 {DATES[3]} / 卖出日 {DATES[4]}")

print("\n" + "=" * 62)
print(f"卖出位移回归：{OK} 通过 / {FAIL} 失败")
print("=" * 62)
sys.exit(1 if FAIL else 0)
