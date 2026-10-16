# -*- coding: utf-8 -*-
"""把调优方案 A（dip 池 × 次日无条件开盘买 + 止盈5%/持有1日）落库。

入库的就是 verify_ui_cfg.py 跑通的那套「页面口径」配置，拆成两条记录：
  * pool_strategy：conds = ref(limit_up,1)=1 / body<=-5，filt 沿用池 #5
  * strategy：buy_trigger/sell_signal/score_expr 全空，risk 止盈5 + 持有1日，
              exec 三段全 open + slippage 千一（exec.slippage 会经
              backtest.resolve_params 覆盖回测页表单，见该函数文档）

仓位（20 只 × 5%）不在策略里，回测页表单填 —— resolve_params 只让
risk / exec.slippage 覆盖表单，别的键策略说了不算。

    python tools/install_plan_a.py            # 只打印将要写入的内容
    python tools/install_plan_a.py --commit    # 真正 INSERT
    python tools/install_plan_a.py --verify    # 从库里读回来，按 app 的组 cfg 顺序复跑
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from config import BACKTEST_DEFAULTS  # noqa: E402
from core import backtest as bt, db, market, universe as uni  # noqa: E402
from core import pool, strategy as st  # noqa: E402

from tune_eat1 import TRAIN, VALID, bench  # noqa: E402

POOL_NAME = "昨日涨停今日大阴"
STRAT_NAME = "次日开盘吃一口"
POOL_CONDS = ["ref(limit_up, 1) = 1", "body <= -5"]
POOL_FILT = {"exclude_st": True, "exclude_bj": True, "min_amount": 0,
             "min_list_days": 60, "exclude_kcb": False, "exclude_cyb": False}
POOL_NOTE = ("调优方案 A（2026-09-19）。信号：昨日涨停、今日大阴（body<=-5%）。"
             "配买卖策略「次日开盘吃一口」。样本内/外都复核过；"
             "结论强依赖滑点千一，滑点千二即转负。")

STRAT_CFG = {
    "timeframe": "daily",
    "entry": [],
    "buy_trigger": [],        # 空 = 次日无条件开盘买
    "sell_signal": [],        # 空 = 只按止盈 / 到期离场
    "score_expr": "",
    "risk": {"stop_loss_pct": None, "take_profit_pct": 5, "max_hold_days": 1},
    "exec": {"buy_price": "open", "sell_price": "open",
             "max_hold_price": "open", "slippage": 0.001},
    "pool": {"where": [], **POOL_FILT},
}
STRAT_NOTE = ("调优方案 A。买入触发留空（信号日次日开盘无条件买），止盈 5%（盘中触及即按"
              "+5% 成交）、max_hold_days=1（未触及则次日收盘卖）、exec 三段全 open、"
              "滑点千一。回测页请配 20 只 × 5% 仓位（集中持仓会打死收益）。"
              "全程 2023-01-01~2026-09-18 年化 +21.94%（TRAIN +23.18 / VALID +31.11，"
              "最大回撤 -19.78%，7684 笔，胜率 46.7%，均笔 +0.289%）。"
              "注意：净利全部来自约 36% 盘中触及 +5% 的单子；2.28% 的卖出日开盘即跌停/近跌停，"
              "回测按开盘成交、实盘可能卖不掉。")

START, END = "2023-01-01", "2026-09-18"
WINS = {"2023": ("2023-01-01", "2023-12-31"),
        "2024": ("2024-01-01", "2024-12-31"),
        "2025": ("2025-01-01", "2025-12-31"),
        "2026": ("2026-01-01", "2026-09-18"),
        "TRAIN": TRAIN, "VALID": VALID, "FULL": (START, END)}


def existing(name, table):
    return db.query(f"SELECT id FROM {table} WHERE name=%s", (name,))


def commit():
    # 走 app 自己的写入入口：universe 的 3 秒池缓存由 save_pool 负责失效
    pid = pool.save_pool(POOL_NAME, POOL_NOTE, "ALL", POOL_CONDS, filt=POOL_FILT)
    sid = st.save_strategy(STRAT_NAME, STRAT_NOTE, STRAT_CFG)
    return int(pid), int(sid)


def load_ids():
    p = existing(POOL_NAME, "pool_strategy")
    s = existing(STRAT_NAME, "strategy")
    if not p or not s:
        sys.exit("尚未入库：先跑 --commit")
    return int(p[0]["id"]), int(s[0]["id"])


def verify(npos: int = 20, ppct: float = 5.0, score: str | None = None):
    """完全按 app.api_backtest_start 的组 cfg 顺序，从库里读回来跑一遍。

    score 非空时临时覆盖策略的 score_expr（排序研究用，不写库）。
    """
    pid, sid = load_ids()
    cfg = st.get_strategy(sid)["config"]
    if score is not None:
        cfg["score_expr"] = score
    spec = uni.parse({"pool_id": pid})
    cfg = st.merge_config({**cfg, "_pool": spec})
    errs = st.validate_config(cfg)
    print(f"pool=#{pid}({spec['pool_name']}) strategy=#{sid} 校验：", errs or "通过")
    if errs:
        return
    params = bt.resolve_params(cfg, {**BACKTEST_DEFAULTS, "init_cash": 1_000_000,
                                     "max_positions": npos, "position_pct": ppct})
    print(f"实际参数：滑点 {params['slippage']} 持仓 {params['max_positions']}×"
          f"{params['position_pct']}% 止盈 {params.get('take_profit_pct')} "
          f"max_hold {params.get('max_hold_days')}")
    codes = market.resolve_universe(spec, start=START, end=END)
    cinfo: dict = {}
    data = bt.prepare_data(codes, START, END, cfg, batch=250, cache_info=cinfo)
    print(f"池 {len(codes)} 只 / 有效 {len(data)} 只 / cache={cinfo.get('cache')}",
          flush=True)
    for name, (s, e) in WINS.items():
        m = bt.run_backtest(data, cfg, s, e, params,
                            benchmark=bench(s, e)).get("metrics") or {}
        print(f"  {name:<6} 年化 {m.get('annual_return'):>7.2f}%  "
              f"累计 {m.get('total_return'):>8.2f}%  回撤 {m.get('max_drawdown'):>7.2f}%  "
              f"{m.get('trade_count'):>4}笔  胜率 {m.get('win_rate'):>5.1f}%  "
              f"均笔 {m.get('avg_pnl_pct') or 0:>+.3f}%  "
              f"基准 {m.get('benchmark_return') or 0:>6.2f}%", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--npos", type=int, default=20)
    ap.add_argument("--ppct", type=float, default=5.0)
    ap.add_argument("--score", default=None,
                    help="临时覆盖买卖策略的 score_expr（只影响本次复算，不写库）")
    args = ap.parse_args()

    dup_p = existing(POOL_NAME, "pool_strategy")
    dup_s = existing(STRAT_NAME, "strategy")
    if args.verify:
        verify(args.npos, args.ppct, args.score)
        return
    errs = pool.validate(POOL_NAME, "ALL", POOL_CONDS)
    cfg = st.merge_config({**STRAT_CFG,
                          "_pool": uni.parse({"base": "ALL", "where": POOL_CONDS,
                                              "filt": POOL_FILT})})
    errs += st.validate_config(cfg)
    print("入库前校验：", "；".join(errs) or "通过")
    if errs:
        return
    print(f"选股策略 {POOL_NAME!r}：{'已存在 id=' + str(dup_p[0]['id']) if dup_p else '待新建'}")
    print(f"买卖策略 {STRAT_NAME!r}：{'已存在 id=' + str(dup_s[0]['id']) if dup_s else '待新建'}")
    print("\npool_strategy.conds =", json.dumps(POOL_CONDS, ensure_ascii=False))
    print("strategy.config =", json.dumps(STRAT_CFG, ensure_ascii=False))
    if dup_p or dup_s:
        print("\n同名记录已存在（uk_name 唯一），不重复插入。跑 --verify 复核。")
        return
    if not args.commit:
        print("\n以上是将要写入的内容，确认无误后加 --commit")
        return
    pid, sid = commit()
    print(f"\n已入库：pool_strategy id={pid}「{POOL_NAME}」 × strategy id={sid}「{STRAT_NAME}」")


if __name__ == "__main__":
    main()
