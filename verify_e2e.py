# -*- coding: utf-8 -*-
"""端到端验证：真实数据回测 + 选股。

两策略分离（2026-09-18）后的口径：
  · **选股策略**（`pool_strategy`）决定「选哪些票」—— 下面的 `POOLS` 就是就地写的选股策略；
  · **买卖策略**（`strategy`）决定「何时买卖」—— 这里就地构造，不吃库里的行。
⚠ 别再按 `st.get_strategy(1)` 这类**行号**取策略：迁移脚本已经把旧的「选股+买卖合体」策略删掉了，
   而且内置行在界面上改得动 —— 拿行号当断言是假红的来源。
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import db, market, strategy as st, backtest as bt, screener

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"  — {detail}" if detail else ""))


START, END = "2023-01-03", "2023-09-06"

# 买卖策略：就地构造（只描述买卖时点，不含选股条件）
BUY_SELL = {"buy_trigger": ["open > ref(close, 1)"],
            "sell_signal": ["open < ref(close, 1)"]}

# 选股策略：就地写（等价于「选股策略管理」里的一条）
# ⚠ 沪深300 里没有「50 亿以下」的票（`total_mv` 单位万元，500000 = 50 亿），
#   照抄界面上的「小市值」预设会得到 0 笔交易 —— 脚本不报错、但这段什么也没验到。
#   所以沪深300 用小市值**相对**条件（< 2000 亿），保证真出交易。
POOLS = [("沪深300 × 小市值（< 2000 亿）", "000300.SH", ["total_mv < 20000000"]),
         ("中证500 × 高换手", "000905.SH", ["turnover_rate > 5"])]

print("=" * 70)
print(f"真实数据端到端验证  {START} ~ {END}")
print("=" * 70)

for name, base, where in POOLS:
    cfg = st.merge_config({**BUY_SELL, "_pool": {"base": base, "where": list(where)}})
    cfg["_name"] = name
    print(f"\n### {name}  [{cfg['timeframe']}]")
    print("   选股:", where, "| 买入触发:", cfg["buy_trigger"],
          "| 卖出:", cfg["sell_signal"], "| 风控:", cfg["risk"])

    codes = market.resolve_universe(base)
    print(f"   股票池 {len(codes)} 只")
    t0 = time.time()
    data = bt.prepare_data(codes, START, END, cfg, batch=250)
    bench = {r["trade_date"].strftime("%Y-%m-%d"): float(r["close"])
             for r in db.query("SELECT trade_date,close FROM index_daily WHERE ts_code='000300.SH'"
                               " AND trade_date BETWEEN %s AND %s", (START, END)) if r["close"]}
    res = bt.run_backtest(data, cfg, START, END,
                          {"init_cash": 1000000, "max_positions": 10, "position_pct": 10},
                          benchmark=bench)
    m = res["metrics"]
    print(f"   加载+回测 {time.time()-t0:.1f}s")
    print(f"   交易 {m.get('trade_count')} 笔 | 总收益 {m.get('total_return')}% | "
          f"年化 {m.get('annual_return')}% | 最大回撤 {m.get('max_drawdown')}%")
    print(f"   胜率 {m.get('win_rate')}% | 盈亏比 {m.get('profit_factor')} | "
          f"夏普 {m.get('sharpe')} | 平均持有 {m.get('avg_hold_days')} 天")
    print(f"   基准 {m.get('benchmark_return')}% | 超额 {m.get('alpha')}% | "
          f"离场分布 {m.get('sell_reason_stat')}")
    check(f"{name}：真出了交易（0 笔 = 这段没验到东西）", int(m.get("trade_count") or 0) > 0,
          f"{m.get('trade_count')} 笔")
    for t in (res["trades"] or [])[:3]:
        print(f"     · {t['ts_code']} {t['name']:8s} {t['buy_date']}@{t['buy_price']} "
              f"-> {t['sell_date']}@{t['sell_price']}  {t['pnl_pct']:+.2f}%  "
              f"{t['sell_reason']} 持有{t['hold_days']}天")

print("\n" + "=" * 70)
print("滑点：买卖策略自带 → 回测真金白银地亏，且亏损单列")
print("=" * 70)

# 滑点口径（2026-09-18 起）：
#   · 来源：**买卖策略** `exec.slippage`（回测页那个框只是「策略没设时的默认值」）；
#   · 单位：库里/参数一律**小数**（0.001 = 千1），界面填**百分数**；
#   · 方向：买入 ×(1+slip) 上浮、卖出 ×(1-slip) 下调 —— **永远算成亏损**；
#   · 亏损额单列成 `trade.slip_cost` / `metrics.slippage_cost`，不再只藏在成交价里。
SP_START, SP_END = "2023-01-03", "2023-06-30"
SP_POOL = {"base": "000300.SH", "where": []}
SP_BASE = {**BUY_SELL, "_pool": SP_POOL}
_codes = market.resolve_universe("000300.SH")[:150]
_cfg = st.merge_config(SP_BASE)
_t0 = time.time()
_sp_data = bt.prepare_data(_codes, SP_START, SP_END, _cfg, batch=250)
print(f"   {len(_sp_data)} 只标的，加载 {time.time()-_t0:.1f}s")
P_BASE = {"init_cash": 1000000, "max_positions": 5, "position_pct": 20}


def _sp_run(exec_cfg, params):
    cfg = st.merge_config({**SP_BASE, **({"exec": exec_cfg} if exec_cfg else {})})
    return bt.run_backtest(_sp_data, cfg, SP_START, SP_END, params)["metrics"]


# 1) 合成规则：策略自带滑点**压过**回测参数（与止损/止盈同口径）
rt = bt.resolve_params(st.merge_config({**SP_BASE, "exec": {"slippage": 0.002}}),
                       {"slippage": 0.0})
check("resolve_params：策略自带滑点优先于运行参数", abs(rt["slippage"] - 0.002) < 1e-12,
      f"slippage={rt['slippage']}")
rt2 = bt.resolve_params(st.merge_config(SP_BASE), {"slippage": 0.0007})
check("resolve_params：策略没设就用运行参数", abs(rt2["slippage"] - 0.0007) < 1e-12,
      f"slippage={rt2['slippage']}")
rt3 = bt.resolve_params(st.merge_config(SP_BASE), None)
check("resolve_params：两边都没给 → 回落到默认值",
      abs(rt3["slippage"] - 0.001) < 1e-12, f"slippage={rt3['slippage']}")

# 2) 真回测：无滑点 / 页面参数 0.2% / 策略自带 0.2%（参数传 0，应等同前者）
m_no = _sp_run(None, {**P_BASE, "slippage": 0})
m_pg = _sp_run(None, {**P_BASE, "slippage": 0.002})
m_st = _sp_run({"slippage": 0.002}, {**P_BASE, "slippage": 0})
check("真回测：加了滑点收益确实变差",
      m_no["total_return"] > m_pg["total_return"],
      f"{m_no['total_return']}% → {m_pg['total_return']}%（{m_pg['trade_count']} 笔）")
check("真回测：策略自带滑点 == 页面参数设同值（同一套合成规则）",
      abs(m_st["total_return"] - m_pg["total_return"]) < 1e-9,
      f"策略 {m_st['total_return']}% / 参数 {m_pg['total_return']}%")
check("无滑点那次不产生滑点成本", not m_no.get("slippage_cost"))
# ⚠ 这两者**不是恒等**：滑点抬高买入价 → 同样预算买到的**股数更少** → 之后每一笔的
#   成本和复利路径都跟着变（还有滑点把某些票的买卖时点挤出行情区间的情形）。
#   所以只断言「收益落差的主体就是滑点成本」，别写成相等 —— 那是假断言，必红。
_gap = (m_no["total_return"] - m_pg["total_return"]) / 100 * m_pg["init_cash"]
check("收益落差与滑点成本同量级（滑点是要向成本；差额来自少买股数与复利路径）",
      abs(_gap - m_pg["slippage_cost"]) <= 0.25 * max(m_pg["slippage_cost"], 1),
      f"收益差 {_gap:.0f} 元 vs 单列滑点 {m_pg['slippage_cost']:.0f} 元"
      f"（偏差 {abs(_gap - m_pg['slippage_cost']) / max(m_pg['slippage_cost'], 1) * 100:.1f}%）")
check("交易成本 = 手续费 + 滑点（README 口径终于对得上）",
      abs(m_pg["total_cost"] - (m_pg["total_fee"] + m_pg["slippage_cost"])) < 0.02,
      f"{m_pg['total_fee']} + {m_pg['slippage_cost']} = {m_pg['total_cost']}，"
      f"占初始资金 {m_pg['cost_pct_of_init']}%")
check("滑点占比 = 滑点成本 / 初始资金",
      abs(m_pg["slippage_pct_of_init"]
          - m_pg["slippage_cost"] / m_pg["init_cash"] * 100) < 0.01,
      f"{m_pg['slippage_pct_of_init']}%")

# 3) 单笔滑点成本 = 买端上浮 + 卖端下调（≈ 买卖成交额 × 滑点）
_tr = bt.run_backtest(_sp_data, st.merge_config(
    {**SP_BASE, "exec": {"slippage": 0.002}}), SP_START, SP_END, P_BASE)["trades"]
_bad = [t for t in _tr if not (0.6 * (t["cost"] + t["revenue"]) * 0.002
                               <= t["slip_cost"] <= 1.4 * (t["cost"] + t["revenue"]) * 0.002)]
check("每笔滑点成本量级正确（≈(成本+收入)×滑点）", not _bad,
      f"{len(_tr)} 笔，越界 {len(_bad)} 笔"
      + (f" 如 {_bad[0]['slip_cost']}" if _bad else ""))

# 4) 校验：范围外/非数字被拦，报错文案按**百分数**说（界面单位）
_e1 = st.validate_config({"exec": {"slippage": 0.5}})
check("滑点 0.5(=50%) 被范围校验拦下", bool(_e1), str(_e1))
check("报错文案按百分数说（否则用户不知道怎么改）",
      any("50%" in e and "3%" in e for e in _e1), str(_e1))
check("滑点 0.002 合法", st.validate_config({"exec": {"slippage": 0.002}}) == [])
check("滑点留空合法（= 跟随回测参数）", st.validate_config({"exec": {"slippage": None}}) == [])
_mn, _mx = st.SLIPPAGE_MIN, st.SLIPPAGE_MAX
check("范围上下限就是 strategy 常量（唯一来源，前端不写死）",
      (bt.resolve_params({"exec": {"slippage": _mx}}, None)["slippage"] == _mx
       and st.validate_config({"exec": {"slippage": _mx * 1.0001}}) != []),
      f"{_mn} ~ {_mx}")

# 5) 落库列（明细要能按笔看到滑点）
_cols = {str(list(c.values())[0]).lower()
         for c in db.query("SHOW COLUMNS FROM backtest_trade")}
check("backtest_trade 有 slip_cost 列（明细/导出能按笔看滑点）", "slip_cost" in _cols,
      f"{len(_cols)} 列")
del _sp_data, _tr, _bad

print("\n" + "=" * 70)
print("选股验证：交易日 2023-09-06，选股策略「小市值」")
print("=" * 70)
cfg = st.merge_config({**BUY_SELL, "_pool": {"base": "all", "where": ["total_mv < 500000"]}})
cfg["_name"] = "小市值"
t0 = time.time()
sr = screener.screen(cfg, cfg["_pool"], "2023-09-06", limit=500, save=False)
print(f"耗时 {time.time()-t0:.1f}s  股票池 {sr.get('universe_size')} 只  "
      f"扫描 {sr.get('scanned')} 只  命中 {sr.get('count')} 只  "
      f"（入池被剔 {sr.get('pool_skipped')} 只）")
for it in (sr.get("items") or [])[:8]:
    vals = (it["detail"] or {}).get("values") or {}
    print(f"  {it['ts_code']} {it['name']:8s} 收{it['close']:>8} ({it['pct_chg']:>6}%) "
          f"总市值={vals.get('total_mv')} 换手={vals.get('turnover_rate')}")

print("\n" + "=" * 70)
print(f"滑点等断言：{len(PASS)} 通过 / {len(FAIL)} 失败")
for f in FAIL:
    print("  FAIL " + f)
print("完成")
sys.exit(1 if FAIL else 0)
