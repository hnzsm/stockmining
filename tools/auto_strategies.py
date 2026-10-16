# -*- coding: utf-8 -*-
"""批量回测「有名短线策略」候选，只保留收益率为正的并写入策略库（落成**普通**策略）。

用法:
    python tools/auto_strategies.py --dry                 # 只校验表达式，不回测
    python tools/auto_strategies.py --pool 000300.SH      # 小池子快速试
    python tools/auto_strategies.py --pool all --install  # 全市场跑并入库（慢）
    python tools/auto_strategies.py --only 首板打板        # 只跑名字含关键字的

说明:
  * 所有候选共用同一套执行口径：T 日收盘选股 → 次日满足开盘条件买入（开盘价含滑点）→
    持仓期用**收盘价**判定卖出（避免"收盘信号开盘成交"的未来函数）→ 到期/止损/止盈。
  * 统一参数：初始 100 万、最多 10 只、每只 10% 仓位、佣金万2.5、印花税千1（卖出）、滑点千1。
  * 基准：沪深300。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from config import BACKTEST_DEFAULTS  # noqa: E402
from core import backtest as bt, db, market, pool as pool_mod, strategy as st  # noqa: E402

OUT_JSON = os.path.join(ROOT, "tools", "_auto_strategies_result.json")

# 统一执行口径：次日开盘买、收盘判定卖
EXEC = {"buy_price": "open", "sell_price": "close", "max_hold_price": "close"}


# ---------------------------------------------------------------------------
# 实战过滤：全市场每天命中的票太多 → 每天满仓换手 → 手续费+滑点吃掉一切。
# 真实短线只在「有流动性、有弹性」的那部分票里做，所以给候选统一加一道池子条件。
# amount 单位千元、total_mv/circ_mv 单位万元、turnover_rate 单位 %
# ---------------------------------------------------------------------------
FILTERS = {
    # 流动性 + 中小市值（短线最常见的基本盘）
    "liq": ["amount >= 50000",           # 日成交额 ≥ 5000 万
            "circ_mv >= 100000",         # 流通市值 ≥ 10 亿
            "total_mv <= 5000000",       # 总市值 ≤ 500 亿（排除超级大盘）
            "turnover_rate >= 1"],       # 换手率 ≥ 1%
    # 只做流动性，不限市值
    "amount": ["amount >= 100000"],      # 日成交额 ≥ 1 亿
}


def C(entry, sell, risk, note, extra=None, buy=None):
    """组装一个候选：**选股条件**（→ 选股策略）+ **买卖规则**（→ 买卖策略）。

    2026-09-18 起选股逻辑只归「选股策略」，所以 entry 单独放在 `pool` 键上，
    `config` 只留买卖时点。跑回测时把 pool 装成 `cfg["_pool"]` —— 与页面
    「选股策略 × 买卖策略」两个下拉完全等价（`strategy.selection_cfg` 会把
   池子条件抬进 entry）。
    """
    cfg = {
        "timeframe": "daily",
        "buy_trigger": buy or ["open > ref(close, 1)"],
        "sell_signal": sell,
        "risk": risk,
        "exec": dict(EXEC),
    }
    if extra:
        cfg.update(extra)
    return {"note": note, "pool": list(entry), "config": cfg}


# ---------------------------------------------------------------------------
# 候选策略：有名的短线打法
# ---------------------------------------------------------------------------
CANDIDATES = [
    {
        "name": "首板打板（次日高开追）",
        **C(["limit_up == 1"],
           ["close < ref(close, 1)"],
           {"max_hold_days": 3, "stop_loss_pct": -6},
           "【打板】当日涨停（首板），次日高开买入；收盘跌破买入前一日收盘价即走，最长持 3 天。"
           "追的是涨停惯性，回测口径为「次日开盘可买入」，未模拟封板买不进的情况。"),
    },
    {
        "name": "涨停回调低吸（缩量回踩5日线）",
        **C(["ref(limit_up, 1) == 1", "close < ref(close, 1)", "close > ma5 * 0.99",
             "vol < ref(vol, 1) * 0.9"],
           ["close < ref(close, 1)"],
           {"max_hold_days": 5, "stop_loss_pct": -6, "take_profit_pct": 12},
           "【接力低吸】昨日涨停，当日缩量回调但不破 5 日线 → 次日高开买入。"
           "买的是涨停后的第一次分歧，缩量说明抛压不大。"),
    },
    {
        "name": "连板接力（二板放量）",
        **C(["ref(limit_up, 1) == 1", "ref(limit_up, 2) == 1", "vol > vma5 * 1.2",
             "close > ma5"],
           ["close < ref(close, 1)"],
           {"max_hold_days": 4, "stop_loss_pct": -8, "take_profit_pct": 15},
           "【龙头接力】连续两天涨停且放量、股价在 5 日线上 → 次日高开买入。"
           "典型的高位接力打法，波动大，止损要硬。"),
    },
    {
        "name": "阳包阴（看涨吞没）",
        **C(["ref(is_down, 1) == 1", "is_up == 1", "close > ref(open, 1)",
             "open < ref(close, 1)"],
           ["close < ref(close, 1)"],
           {"max_hold_days": 6, "stop_loss_pct": -6, "take_profit_pct": 12},
           "【K线形态】昨日收阴，今日收阳且实体完全包住昨日实体 → 次日高开买入。"
           "最经典的反转形态之一，常出现在短期底部。"),
    },
    {
        "name": "早晨之星（启明星）",
        **C(["ref(body, 2) < -2", "abs(ref(body, 1)) < 1.5", "body > 2",
             "close > ref(close, 2) * 0.99"],
           ["close < ref(close, 1)"],
           {"max_hold_days": 6, "stop_loss_pct": -6, "take_profit_pct": 15},
           "【K线形态】大阴 + 小星 + 大阳的三根组合，第三根收复第一根大部分跌幅 → 次日高开买入。"),
    },
    {
        "name": "锤子线（长下影反转）",
        **C(["lower_shadow > 1.5", "lower_shadow > abs(body) * 2", "close_pos > 0.6",
             "ret5 < -0.03"],
           ["close < ref(close, 1)"],
           {"max_hold_days": 6, "stop_loss_pct": -6, "take_profit_pct": 12},
           "【K线形态】前 5 日下跌，当日长下影线（下影 > 实体 2 倍）且收在当日高位 → "
           "次日高开买入。下影线代表盘中被砸后被买回。"),
    },
    {
        "name": "缩量回踩MA10（均线多头低吸）",
        **C(["ma_bull == 1", "close <= ma10 * 1.01", "close >= ma10 * 0.96",
             "vol < vma5 * 0.8"],
           ["close < ma10"],
           {"max_hold_days": 10, "stop_loss_pct": -7, "take_profit_pct": 15},
           "【趋势低吸】均线多头排列（MA5≥MA10≥MA20），股价缩量回踩到 MA10 附近 → 次日高开买入，"
           "收盘跌破 MA10 卖出。买的是上升趋势中的洗盘结束。"),
    },
    {
        "name": "龙回头（强势股回调）",
        **C(["ret20 > 0.15", "close < ma10", "close > ma20", "vol < vma5 * 0.9"],
           ["close < ma20"],
           {"max_hold_days": 8, "stop_loss_pct": -8, "take_profit_pct": 20},
           "【强势回调】近 20 日涨幅超 15% 的强势股，回调到 MA10 下方但守住 MA20，且缩量 → "
           "次日高开买入，跌破 MA20 走人。"),
    },
    {
        "name": "MACD水上金叉 + 放量",
        **C(["cross_above(macd_dif, macd_dea)", "macd_dif > 0", "vol > vma5 * 1.3"],
           ["close < ma5"],
           {"max_hold_days": 12, "stop_loss_pct": -7, "take_profit_pct": 15},
           "【指标金叉】MACD 在零轴之上金叉（水上金叉）且放量 → 次日高开买入，收盘跌破 MA5 卖出。"
           "零轴之上代表中期趋势向上，金叉是加速信号。"),
    },
    {
        "name": "底部放量启动",
        **C(["vol > vma5 * 2", "ret20 < -0.05", "close > ma5", "close > ref(close, 1)"],
           ["close < ma5"],
           {"max_hold_days": 10, "stop_loss_pct": -7, "take_profit_pct": 15},
           "【量价】近 20 日下跌，突然放出 2 倍以上量能并站上 MA5 收阳 → 次日高开买入。"
           "放量是资金进场的直接证据。"),
    },
    {
        "name": "跳空缺口不补（突破缺口）",
        **C(["gap_open > 2", "low > ref(high, 1)", "vol > vma5 * 1.5"],
           ["close < ref(low, 1)"],
           {"max_hold_days": 8, "stop_loss_pct": -6, "take_profit_pct": 18},
           "【缺口】高开 2% 以上且全天最低价高于昨日最高价（留下向上缺口），放量 → 次日高开买入，"
           "收盘回补缺口（跌破昨日最高价）卖出。缺口不补视为强势延续。"),
    },
    {
        "name": "三连阴反抽",
        **C(["ref(is_down, 1) == 1", "ref(is_down, 2) == 1", "ref(is_down, 3) == 1",
             "is_up == 1", "vol < vma5"],
           ["close < ref(close, 1)"],
           {"max_hold_days": 5, "stop_loss_pct": -5, "take_profit_pct": 10},
           "【超跌反抽】连收三根阴线后首次收阳且缩量（卖压衰竭）→ 次日高开买入，快进快出。"),
    },
    {
        "name": "平台突破（窄幅整理后放量新高）",
        **C(["hh20 / ll20 < 1.15", "close >= hh20", "vol > vma5 * 1.5"],
           ["close < ma10"],
           {"max_hold_days": 10, "stop_loss_pct": -6, "take_profit_pct": 20},
           "【平台突破】近 20 日振幅不到 15%（横盘整理），当日放量创 20 日新高 → 次日高开买入，"
           "跌破 MA10 卖出。横久必决的典型打法。"),
    },
    {
        "name": "60日新高趋势跟随",
        **C(["close >= hh60", "vol > vma5 * 1.3", "ma_bull == 1"],
           ["close < ma20"],
           {"max_hold_days": 15, "stop_loss_pct": -8, "take_profit_pct": 25},
           "【趋势跟随】创 60 日新高 + 均线多头 + 放量 → 次日高开买入，收盘跌破 MA20 卖出。"
           "持股时间最长的趋势型打法。"),
    },
    {
        "name": "超跌反弹（20日超跌放量收阳）",
        **C(["ret20 < -0.2", "close > ref(close, 1)", "vol > vma5 * 1.2", "close > ma5"],
           ["close < ma5"],
           {"max_hold_days": 8, "stop_loss_pct": -6, "take_profit_pct": 15},
           "【超跌反弹】近 20 日跌幅超 20%，当日放量收阳且站上 MA5 → 次日高开买入，跌破 MA5 卖出。"),
    },
]


# ---------------------------------------------------------------------------
# 跑回测
# ---------------------------------------------------------------------------

def benchmark_series(start: str, end: str, index_code: str = "000300.SH") -> dict:
    rows = db.query(
        "SELECT trade_date, close FROM index_daily WHERE ts_code=%s "
        "AND trade_date BETWEEN %s AND %s ORDER BY trade_date",
        (index_code, start, end))
    return {str(r["trade_date"])[:10]: float(r["close"]) for r in rows if r["close"]}


def run_one(cand: dict, universe: str, start: str, end: str, params: dict) -> dict:
    # 选股条件当「选股策略」用（base=universe + where=cand["pool"]）；
    # selection_cfg 会把它抬进 entry，所以下游一行不用改。
    strat_cfg = st.merge_config(cand["config"])
    run_cfg = {**strat_cfg, "_pool": {"base": universe, "where": list(cand["pool"])}}
    errs = st.validate_config(run_cfg)
    if errs:
        return {"name": cand["name"], "error": "；".join(errs)}

    t0 = time.time()
    codes = market.resolve_universe(universe)
    data = bt.prepare_data(codes, start, end, run_cfg, batch=300)
    res = bt.run_backtest(data, run_cfg, start, end, params,
                          benchmark=benchmark_series(start, end))
    m = res.get("metrics") or {}
    return {
        "name": cand["name"],
        "note": cand["note"],
        # 落库用「纯买卖配置」（不含 _pool）；选股条件单独放在 pool 键上
        "config": strat_cfg,
        "pool": list(cand["pool"]),
        "universe": universe,
        "range": f"{start}~{end}",
        "codes": len(codes),
        "metrics": m,
        "elapsed": round(time.time() - t0, 1),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", default="000300.SH", help="all / 000300.SH / 000905.SH")
    ap.add_argument("--start", default="2023-01-03")
    ap.add_argument("--end", default="")
    ap.add_argument("--only", default="", help="只跑名字含关键字的（逗号分隔多个）")
    ap.add_argument("--dry", action="store_true", help="只校验表达式")
    ap.add_argument("--install", action="store_true", help="把正收益的写入策略库")
    ap.add_argument("--min-trades", type=int, default=30, help="入库最少交易笔数")
    ap.add_argument("--shard", default="", help="分片 i/n，全市场太慢时可多进程并行")
    ap.add_argument("--out", default=OUT_JSON, help="结果 JSON 路径")
    ap.add_argument("--filter", default="",
                    help=f"给候选统一加池子条件（收窄候选）：{ '/'.join(FILTERS) }")
    args = ap.parse_args()

    end = args.end or (market.latest_trade_date() or "2026-09-11")
    params = {**BACKTEST_DEFAULTS, "init_cash": 1_000_000, "max_positions": 10,
              "position_pct": 10.0}

    cands = CANDIDATES
    if args.only:
        keys = [k.strip() for k in args.only.split(",") if k.strip()]
        cands = [c for c in cands if any(k in c["name"] for k in keys)]
        print(f"按关键字筛选：{len(cands)} 个")
    if args.filter:
        conds = FILTERS.get(args.filter)
        if not conds:
            print(f"未知 --filter {args.filter}，可选：{list(FILTERS)}")
            return
        for c in cands:
            c["pool"] = list(c["pool"]) + list(conds)
        print(f"实战过滤[{args.filter}]：{'；'.join(conds)}")
    if args.shard:
        i, n = (int(x) for x in args.shard.split("/"))
        cands = [c for k, c in enumerate(cands) if k % n == i - 1]
        print(f"分片 {i}/{n}：本进程 {len(cands)} 个")

    print("=" * 96)
    print(f"有名短线策略批量回测  池={args.pool}  区间={args.start} ~ {end}")
    print("=" * 96)

    if args.dry:
        bad = 0
        for c in cands:
            cfg = st.merge_config(c["config"])
            cfg["_pool"] = {"base": args.pool, "where": list(c["pool"])}
            errs = st.validate_config(cfg)
            flag = "OK  " if not errs else "FAIL"
            if errs:
                bad += 1
            print(f"  {flag} {c['name']}" + ("" if not errs else f"  — {errs}"))
        print(f"\n校验 {len(cands)} 个，错误 {bad} 个")
        return

    results = []
    for c in cands:
        try:
            r = run_one(c, args.pool, args.start, end, params)
        except Exception as e:  # noqa: BLE001
            r = {"name": c["name"], "error": f"{type(e).__name__}: {e}"}
        results.append(r)
        if r.get("error"):
            print(f"  !! {r['name']}  失败：{r['error']}")
            continue
        m = r["metrics"]
        print(f"  {r['name']:<24} 收益 {m.get('total_return'):>8.2f}%  "
              f"基准 {m.get('benchmark_return'):>7.2f}%  回撤 {m.get('max_drawdown'):>7.2f}%  "
              f"笔数 {m.get('trade_count'):>5}  胜率 {m.get('win_rate'):>5.1f}%  "
              f"耗时 {r['elapsed']}s")

    ok = [r for r in results if not r.get("error") and r["metrics"].get("total_return", 0) > 0]
    ok.sort(key=lambda r: -r["metrics"]["total_return"])

    print("\n" + "=" * 96)
    print(f"收益为正：{len(ok)} / {len(results)}")
    print("=" * 96)
    print(f"{'策略':<26}{'收益%':>9}{'年化%':>9}{'基准%':>9}{'超额%':>9}"
          f"{'回撤%':>9}{'夏普':>7}{'笔数':>7}{'胜率%':>8}{'盈亏比':>8}{'持股天':>8}")
    for r in ok:
        m = r["metrics"]
        print(f"{r['name']:<26}{m.get('total_return', 0):>9.2f}{m.get('annual_return', 0):>9.2f}"
              f"{m.get('benchmark_return') or 0:>9.2f}{m.get('alpha') or 0:>9.2f}"
              f"{m.get('max_drawdown', 0):>9.2f}{(m.get('sharpe') or 0):>7.2f}"
              f"{m.get('trade_count', 0):>7}{m.get('win_rate', 0):>8.1f}"
              f"{(m.get('profit_factor') or 0):>8.2f}{m.get('avg_hold_days', 0):>8.1f}")

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"pool": args.pool, "range": f"{args.start}~{end}",
                   "tag": f"{'全市场' if args.pool == 'all' else args.pool}"
                          f"{'+流动性过滤' if args.filter else ''}",
                   "params": params, "results": results}, f,
                  ensure_ascii=False, indent=2, default=str)
    print(f"\n明细已保存：{args.out}")

    if args.install:
        install_results(ok, args.min_trades)


def install_results(ok: list, min_trades: int = 1) -> list:
    """把「收益为正」的回测结果写进策略库，返回入库的策略名列表。

    两路（选股 / 买卖）都落成**普通**策略（`builtin=0` ⇒ 界面上可改名、可删除）——
    这里产出的是「候选」，不是不可删的内置样例（2026-09-29 起两者解耦）。

    抽成函数是为了能**单测**：直接喂合成结果跑真实写库路径，验库里 `builtin=0`
    且 `delete_pool()` 真删得掉（`tools/_check_auto_install.py`）。
    """
    picked = [r for r in ok if r["metrics"].get("trade_count", 0) >= min_trades]
    skipped = [r for r in ok if r not in picked]
    print(f"\n写入策略库：{len(picked)} 条"
          + (f"（{len(skipped)} 条因笔数<{min_trades} 未入库："
             + "、".join(r["name"] for r in skipped) + "）" if skipped else ""))
    print("口径：**选股条件 → 选股策略**（/pools），**买卖规则 → 买卖策略**（/strategies）；"
          "回测时两个下拉一配就是它。")
    print("落库一律是**普通**策略（`builtin=0` ⇒ 界面上可改名、可删除），不占内置样例的名额。")
    names = []
    for r in picked:
        m = r["metrics"]
        note = (r["note"] + f"\n\n【实测】{r['universe']} {r['range']}："
                f"收益 {m.get('total_return')}%，年化 {m.get('annual_return')}%，"
                f"最大回撤 {m.get('max_drawdown')}%，{m.get('trade_count')} 笔交易，"
                f"胜率 {m.get('win_rate')}%，平均持股 {m.get('avg_hold_days')} 天。")
        # ① 选股策略：条件 = 候选的 entry，base = 本次回测用的池子
        #    落成**普通**策略（`pool.save_pool` 不写 builtin ⇒ 默认 0）——
        #    这里产出的是「候选」，要能改名/删除，不跟不可删的内置样例混为一谈（2026-09-29）。
        pool_name = r["name"] + " · 选股"
        pid = pool_mod.save_pool(pool_name, note, r["universe"],
                                 list(r["pool"]))
        print(f"  选股策略 {pool_name}（id={pid}，{len(r['pool'])} 条条件，"
              f"base={r['universe']}）")
        # ② 买卖策略：只留买卖时点（选股条件已由上面那条池子承载）
        #    同名已存在时只改内容、**不动 builtin**（万一撞上内置样例「开盘确认」，
        #    不能顺手把它的内置标记抹掉）；新建的显式写 builtin=0。
        exist = db.query_one("SELECT id FROM strategy WHERE name=%s", (r["name"],))
        if exist:
            db.execute("UPDATE strategy SET note=%s, config=%s WHERE id=%s",
                       (note, st.config_json(r["config"]), exist["id"]))
            print(f"  买卖策略 {r['name']}（id={exist['id']}，已更新）")
        else:
            sid = db.insert(
                "INSERT INTO strategy (name, note, timeframe, config, builtin) "
                "VALUES (%s,%s,%s,%s,0)",
                (r["name"], note, "daily", st.config_json(r["config"])))
            print(f"  买卖策略 {r['name']}（id={sid}）")
        names.append(r["name"])
    return names


if __name__ == "__main__":
    main()
