# -*- coding: utf-8 -*-
"""把「放量十字星（吃一口就走）」装进平台：选股策略 + 买卖策略，再跑一次回测。

来源：tools/doji_pool_lab.py（信号层三轮）+ tools/doji_backtest.py / doji_final.py
（组合层：池 × 持有期 × 止盈 × 槽位 × 滑点 × 评分表达式）。条件本体只有一份，
在 tools/doji_pools.py 的 POOLS["u_star"]，这里只引用不复制。

写入的都是**新建记录**（同名不重复插），不碰 #156 / #58，也不碰其它池子。

    python tools/install_doji_pool.py            # 干跑：打印将写入的内容 + 入库前校验
    python tools/install_doji_pool.py --commit   # 真正写入 pool_strategy + strategy
    python tools/install_doji_pool.py --verify   # 读回来：分年/样本内外回测 + 最新交易日命中
    python tools/install_doji_pool.py --api      # 走页面同一个接口跑回测（落进「历史回测记录」）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import urllib.request  # noqa: E402

from config import BACKTEST_DEFAULTS  # noqa: E402
from core import backtest as bt, db, market, universe as uni  # noqa: E402
from core import pool, strategy as st  # noqa: E402

from doji_pools import POOLS  # noqa: E402
from tune_eat1 import TRAIN, VALID, bench  # noqa: E402
from tune_eat3 import FILT  # noqa: E402

POOL_NAME = "放量十字星（吃一口就走）"
STRAT_NAME = "十字星吃一口（持1日+止盈3%）"
API = "http://127.0.0.1:8777"
START, END = "2023-01-01", "2026-09-18"
WINS = {"2023": ("2023-01-01", "2023-12-31"),
        "2024": ("2024-01-01", "2024-12-31"),
        "2025": ("2025-01-01", "2025-12-31"),
        "2026": ("2026-01-01", END),
        "TRAIN": TRAIN, "VALID": VALID, "FULL": (START, END)}

CONDS = list(POOLS["u_star"][0])
NOTE_POOL = (
    "放量十字星池（2026-09-25 由 tools/doji_pool_lab.py 三轮前向收益研究 + "
    "tools/doji_backtest.py 组合层网格选出）。\n"
    "形态口径：abs(body) <= 0.3 是十字星（引擎里 body 带正负，必须取绝对值），"
    "amplitude >= 2 排掉一字线/僵尸线，vol_ratio5 >= 1.5 是放量（相对 5 日均量），"
    "再叠非涨停、5~100 元、市值>30 亿、成交额>5000 万三条流动性下限。\n"
    "⚠ 缩量十字星（vol_ratio5 <= 0.7）经信号层与组合层双向验证**不可用**：持 20 日"
    "按日对齐超额 −0.66%（≤0.5 倍量 −1.67%），组合层最好也只有 −9.29%/年，故未建池。\n"
    "⚠ 本池的边际很薄（每笔 ~0.25%）：千一滑点年化 +24%，千二只剩 +7%。")
STRAT_CFG = {
    "timeframe": "daily",
    "entry": [],
    "buy_trigger": [],          # 次日开盘直接买 —— 加确认反而亏（见 note）
    "sell_signal": [],          # 不设卖出信号，靠持有期到期
    "score_expr": "-ref(vol_ratio5, 1)",
    "risk": {"stop_loss_pct": None, "take_profit_pct": 3.0, "max_hold_days": 1},
    "exec": {"buy_price": "open", "sell_price": "open",
             "max_hold_price": "open", "slippage": 0.001},
}
NOTE_STRAT = (
    "「吃一口就走」：T 日收盘命中「放量十字星」→ T+1 开盘买 → 盘中触及 +3% 限价卖，"
    "否则 T+2 开盘卖（持有期 1 日、买卖都按开盘价、滑点千一）。\n"
    "评分表达式 -ref(vol_ratio5, 1)：**候选多于槽位时选放量最温和的那批**。"
    "比无评分好 ~1pp，且与信号层结论一致（量越暴越差：≥1.5 倍超额 −0.82、≥2.0 倍 −1.13）。\n"
    "三条已被数据否掉的做法，别再改回去：\n"
    "  1) 持有期 ≥2 日一律崩（同样配置 hold=2 → 年化 −7.6%）；\n"
    "  2) 加次日确认（高开才买 / 突破星线高点才买）把 4748 笔砍到 2275/191 笔，"
    "年化 24.3 → 12.0 / −0.35；\n"
    "  3) 止盈放大到 5% 只在千一好看（+29%@5 槽），千二直接转负。")


def _ids():
    p = db.query("SELECT id FROM pool_strategy WHERE name=%s", (POOL_NAME,))
    s = db.query("SELECT id FROM strategy WHERE name=%s", (STRAT_NAME,))
    return (int(p[0]["id"]) if p else None), (int(s[0]["id"]) if s else None)


def same_cond_pools() -> list[dict]:
    """库里已经有条件完全一样的池子吗（顺序无关）—— 避免重复建池。"""
    want = sorted(CONDS)
    out = []
    for r in pool.list_pools():
        if sorted(r["conds"]) == want:
            out.append({"id": r["id"], "name": r["name"]})
    return out


def build_cfg(pid: int, slip: float = 0.001) -> dict:
    cfg = json.loads(json.dumps(STRAT_CFG))
    cfg["exec"]["slippage"] = slip
    cfg["_pool"] = uni.parse({"pool_id": pid})
    return st.merge_config(cfg)


def verify(pid: int, slip: float, npos: int, ppct: float) -> None:
    cfg = build_cfg(pid, slip)
    errs = st.validate_config(cfg)
    print("校验：", errs or "通过")
    if errs:
        return
    params = bt.resolve_params(cfg, {**BACKTEST_DEFAULTS, "init_cash": 1_000_000,
                                     "max_positions": npos, "position_pct": ppct,
                                     "slippage": slip})
    codes = market.resolve_universe(cfg["_pool"], start=START, end=END)
    info: dict = {}
    data = bt.prepare_data(codes, START, END, cfg, batch=250, cache_info=info)
    print(f"预筛 {len(codes)} 只 / 有效 {len(data)} 只 / 缓存={info.get('cache')}"
          f"｜实际参数：滑点 {params['slippage']}、持仓 {params['max_positions']}"
          f"×{params['position_pct']}%、持有 {params['max_hold_days']} 日、"
          f"止盈 {cfg['risk']['take_profit_pct']}%", flush=True)
    for name, (s, e) in WINS.items():
        m = bt.run_backtest(data, cfg, s, e, params,
                            benchmark=bench(s, e)).get("metrics") or {}
        print(f"  {name:<6} 年化 {m.get('annual_return'):>7.2f}%  "
              f"累计 {m.get('total_return'):>8.2f}%  回撤 {m.get('max_drawdown'):>7.2f}%  "
              f"{m.get('trade_count'):>5}笔  胜率 {m.get('win_rate') or 0:>5.1f}%  "
              f"均笔 {m.get('avg_pnl_pct') or 0:>+6.3f}%  "
              f"基准 {m.get('benchmark_return') or 0:>6.2f}%", flush=True)
    from core import screener
    td = market.latest_trade_date()
    items = (screener.screen(cfg, universe=cfg["_pool"], trade_date=td,
                             limit=500, save=False).get("items") or [])
    print(f"最新交易日 {td} 命中 {len(items)} 只："
          + "、".join(f"{i.get('name') or ''}({i.get('ts_code')})" for i in items[:12]))


def api_run(sid: int, pid: int, npos: int, ppct: float) -> None:
    payload = {"strategy_id": sid, "universe": {"pool_id": pid},
               "start": START, "end": END,
               "name": f"{STRAT_NAME} · 放量十字星 · {npos}×{ppct}%",
               "params": {**BACKTEST_DEFAULTS, "init_cash": 1_000_000,
                          "max_positions": npos, "position_pct": ppct}}

    def post(path: str, obj: dict | None = None) -> dict:
        req = urllib.request.Request(
            API + path, data=None if obj is None else
            json.dumps(obj).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST" if obj is not None else "GET")
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))

    res = post("/api/backtest/start", payload)
    rid = (res.get("data") or {}).get("run_id")
    if not rid:
        sys.exit(f"提交失败：{res}")
    print(f"已提交，回测记录 id={rid}（页面「历史回测记录」可见），等待完成…",
          flush=True)
    t0 = time.time()
    while time.time() - t0 < 1800:
        time.sleep(10)
        row = (post(f"/api/backtest/runs/{rid}").get("data") or {})
        stt, prog = row.get("status"), float(row.get("progress") or 0) * 100
        print(f"  {stt} {prog:>5.1f}% {row.get('progress_msg') or ''}", flush=True)
        if stt in ("done", "error"):
            break
    m = row.get("metrics") or {}
    if isinstance(m, str):
        m = json.loads(m or "{}")
    print(f"\n回测记录 #{rid}：年化 {m.get('annual_return')}% / "
          f"累计 {m.get('total_return')}% / 回撤 {m.get('max_drawdown')}% / "
          f"{m.get('trade_count')} 笔 / 胜率 {m.get('win_rate')}% / "
          f"基准 {m.get('benchmark_return')}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--api", action="store_true")
    ap.add_argument("--slip", type=float, default=0.001)
    ap.add_argument("--npos", type=int, default=10)
    ap.add_argument("--ppct", type=float, default=10.0)
    args = ap.parse_args()

    pid, sid = _ids()
    if args.verify:
        if not pid:
            sys.exit("尚未入库：先跑 --commit")
        verify(pid, args.slip, args.npos, args.ppct)
        return
    if args.api:
        if not (pid and sid):
            sys.exit("尚未入库：先跑 --commit")
        api_run(sid, pid, args.npos, args.ppct)
        return

    print("选股策略条件（一条一行，别加 # 注释，否则翻不成 SQL 预筛）：")
    for c in CONDS:
        print(f"  {c}")
    print("\n买卖策略 config =", json.dumps(STRAT_CFG, ensure_ascii=False))
    errs = pool.validate(POOL_NAME, "ALL", CONDS)
    cfg = {k: v for k, v in STRAT_CFG.items() if k != "_pool"}
    cfg["_pool"] = uni.parse({"base": "ALL", "where": CONDS, "filt": FILT})
    errs += st.validate_config(st.merge_config(cfg))
    print("\n入库前校验：", "；".join(errs) or "通过")
    if errs:
        return
    print(f"现有记录：pool_strategy#{pid} / strategy#{sid}")
    dup_conds = same_cond_pools()
    if dup_conds:
        print("⚠ 库里已有条件完全相同的池子（不会改动它们）："
              + "、".join(f"#{d['id']}「{d['name']}」" for d in dup_conds))
    if pid and sid:
        print("两条都已存在，直接 --verify 或 --api。")
        return
    if not args.commit:
        print("\n以上是将要写入的内容，确认后加 --commit")
        return
    if not pid:
        pid = pool.save_pool(POOL_NAME, NOTE_POOL, "ALL", CONDS, filt=FILT)
        print(f"已入库：pool_strategy id={pid}「{POOL_NAME}」")
    if not sid:
        sid = st.save_strategy(STRAT_NAME, NOTE_STRAT, STRAT_CFG)
        print(f"已入库：strategy id={sid}「{STRAT_NAME}」")
    print(f"\n下一步：python tools/install_doji_pool.py --api   # 页面口径跑一次并落记录")


if __name__ == "__main__":
    main()
