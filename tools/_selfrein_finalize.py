# -*- coding: utf-8 -*-
"""自我强化收尾：把挑出来的「强化池 + 强化买卖策略」落库，并走**平台回测**出正式记录。

⚠ 纪律：
  * **绝不改**原池 #156 / 原买卖策略 #58 —— 一律新建（`builtin=0` 的普通池/普通策略）。
  * 幂等：同名已存在就**更新同一条**，不越跑越多（照 `_lab_bt_platform.apply_tight` 的做法）。
  * 条件里的 `# 批注` **原样落库**（`pool.save_pool` / `strategy.save_strategy(keep_comments=True)`），
    注释永远不进求值（C15）。

用法：
    python tools/_selfrein_finalize.py --dry                 # 只看要写什么，不落库
    python tools/_selfrein_finalize.py --run                 # 落库 + 跑平台回测
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

BASE = "http://127.0.0.1:8777"
SRC_POOL = 156          # 原池：昨日涨停今日大阴
SRC_STRAT = 58          # 原买卖策略：次日开盘吃一口

# ---------------- 强化后的口径（由 _selfrein_* 三轮搜索定稿） ----------------
NEW_POOL_NAME = "昨日涨停今日大阴·放量版"
NEW_STRAT_NAME = "放量大阴·低吸·持1日"

EXTRA_CONDS = [
    "vol / ref(vol, 1) >= 3.2   # 自我强化：高收益样本 T0 共同点＝巨量（≥3.2×前日量）",
]
# ⚠ 这条是**买入时机**（T+1 开盘那一刻就知道），不是选股条件。放在买卖策略的
#   buy_trigger 里（见下），不能混进池条件 —— 池条件只答「谁进候选」。
BUY_TRIGGER: list[str] = ["open <= ref(close, 1) * 0.98"]   # T+1 低开 ≤-2% 才买

POOL_NOTE = (
    "【自我强化 · 第2轮】由池 #156「昨日涨停今日大阴」派生，原池一字未动。\n"
    "做法：从回测 #151 的逐笔明细里挑出收益高的那批，反查其**信号日 T0** 的特征，"
    "在 65 个 T0 形态特征上做 AUC / 阈值搜索（`tools/_selfrein_rank.py`）。\n"
    "结论：T0 形态整体判别力**极弱**（最好 |AUC-0.5| = 0.048，是 KDJ 的 D 值），"
    "把均笔从 -0.945% 抬到 -0.202% 的唯一有效单条件 = **T0 量能 ≥3.2× 前日**"
    "（放量大阴：TRAIN n=134 胜率 38.1% / VALID n=70 胜率 37.1%）。\n"
    "真引擎复核（`_selfrein_engine.py`）：单加这一条，原样配置从 -99.96% 抬到 -38.45%。\n"
    "⚠ 单笔期望仍是负的 —— 「放量」只把亏损大幅收窄，翻正靠的是**买入时机 + 持有口径**。"
)

STRAT_NOTE = (
    "【自我强化 · 第3轮】由买卖策略 #58 派生，原策略未动。两处改动：\n"
    "① risk 去掉止损 -2% / 止盈 +5%，改**纯持有 1 日后收盘卖出**"
    "（回测里「持 1 日」= 买入根的下一根收盘卖）。\n"
    "   依据：风控本身在赔钱 —— #151 的 1348 笔里止损成交 860 笔（63.8%），"
    "止损价按 min(触发价, 当日开盘) 成交，大阴次日就是 -5%~-7%；"
    "而止盈把右尾截掉。离线反事实模拟同样：裸持 1 天 -0.438%/笔 vs 带风控 -0.690%/笔。\n"
    "② buy_trigger = T+1 开盘比前收低 ≤-2% 才买（低吸）。"
    "这是**买入那一刻**就知道的信息，不是未来函数。\n"
    "exec 沿用 #58：买开盘、卖收盘、到期收盘，滑点千一。\n\n"
    "配套选股策略「昨日涨停今日大阴·放量版」（原池 #156 + 量能≥3.2×前日）。\n"
    "样本内外复核（`_selfrein_round3.py`，5 只×20% 仓位）："
    "TRAIN 2023-01~2024-12 **+7.40%**（176 笔）/ VALID 2025-01~2026-09 **+22.36%**（123 笔），两段都正。\n"
    "⚠ 仓位要在回测页给：**5 只 × 20%** 优于默认 10 只 × 10%（+7.40/+22.36 vs +3.44/+11.09）——"
    "该池每日命中数少，集中持仓更划算，代价是回撤从 -16.6% 放大到 -34.3%。\n"
    "⚠ 全样本（2023-01~2026-09）5×20% = +27.43%、10×10% = +13.37%。\n"
    "⚠ 与 #58 一样**强依赖滑点千一**：滑点千二即转负。⚠ 未加「低位」条件 —— "
    "`ref(ret20,1) <= 0.147` 在 TRAIN 段是负的（-5.74%，仅 49 笔），是假信号，已剔除。"
)


# --------------------------------------------------------------------------- 接口
def api(method: str, path: str, data: dict | None = None, timeout: int = 120) -> dict:
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(BASE + path, data=body, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())


def submit(pool_id: int, strategy_id: int, start: str, end: str, name: str,
           params: dict | None = None) -> dict:
    payload = {"strategy_id": strategy_id, "universe": f"pool:{pool_id}",
               "start": start, "end": end, "name": name}
    if params:
        # ⚠ 仓位参数**只能走表单**：`resolve_params` 里策略自带的只有 risk / exec.slippage，
        #   max_positions / position_pct 不在其中（策略自带也会被忽略）。
        payload["params"] = params
    r = api("POST", "/api/backtest/start", payload)
    if not r.get("ok"):
        raise RuntimeError(r.get("error") or r)
    return r["data"]


def wait_run(run_id: int, job_id: str, budget: int = 3600) -> dict:
    t0 = time.time()
    while time.time() - t0 < budget:
        j = (api("GET", "/api/jobs/" + job_id).get("data") or {})
        if j.get("status") in ("done", "failed", "canceled", "error"):
            break
        time.sleep(2)
    r = api("GET", f"/api/backtest/runs/{run_id}").get("data") or {}
    if r.get("status") != "done":
        raise RuntimeError(f"run={run_id} 状态 {r.get('status')} {r.get('progress_msg') or ''}")
    r["_elapsed"] = round(time.time() - t0, 1)
    return r


# --------------------------------------------------------------------------- 落库
def build_and_save() -> tuple[int, int]:
    from core import pool as pool_mod, strategy as st

    src_pool = pool_mod.get_pool(SRC_POOL)
    src_strat = st.get_strategy(SRC_STRAT)
    if not src_pool or not src_strat:
        raise RuntimeError("原池 #156 / 原策略 #58 读不到")

    conds = list(src_pool.get("conds") or []) + list(EXTRA_CONDS)
    errs = pool_mod.validate(NEW_POOL_NAME, src_pool.get("base") or "ALL", conds)
    if errs:
        raise RuntimeError("池条件校验失败：" + "；".join(errs))
    old = pool_mod.get_pool_by_name(NEW_POOL_NAME)
    pid = pool_mod.save_pool(NEW_POOL_NAME, POOL_NOTE, src_pool.get("base") or "ALL",
                             conds, bool(src_pool.get("pool_exit")),
                             pid=old["id"] if old else None)
    print(f"池 {'更新' if old else '新建'} #{pid} {NEW_POOL_NAME}  条件 {len(conds)} 条")

    scfg = dict(src_strat.get("config") or {})
    scfg["risk"] = {"max_hold_days": 1, "stop_loss_pct": None, "take_profit_pct": None}
    scfg["buy_trigger"] = list(BUY_TRIGGER)
    scfg["sell_signal"] = []
    scfg["entry"] = []
    scfg["exec"] = {"buy_price": "open", "sell_price": "close",
                    "max_hold_price": "close", "slippage": 0.001}
    # 池条件归「选股策略」，这里只留买卖口径（选股逻辑不许写进买卖策略）
    scfg.pop("pool", None)
    scfg.pop("_pool", None)
    errs = st.validate_config(scfg)
    if errs:
        raise RuntimeError("买卖策略校验失败：" + "；".join(errs))
    exist = None
    for s in st.list_strategies():
        if s["name"] == NEW_STRAT_NAME:
            exist = s
            break
    sid = st.save_strategy(NEW_STRAT_NAME, STRAT_NOTE, scfg,
                           sid=exist["id"] if exist else None)
    print(f"策略 {'更新' if exist else '新建'} #{sid} {NEW_STRAT_NAME}  "
          f"risk={scfg['risk']} buy={scfg['buy_trigger']}")
    return pid, sid


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只打印，不落库、不回测")
    ap.add_argument("--run", action="store_true", help="落库 + 平台回测")
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--end", default="2026-09-18")
    ap.add_argument("--oos-start", default="2025-01-01", help="样本外起点；空串=不跑")
    ap.add_argument("--poss", default="5,20;10,10",
                    help="仓位档，格式 `max_positions,position_pct`，分号分隔")
    ap.add_argument("--out", default="tools/_selfrein_final.json")
    args = ap.parse_args()

    if not args.run:
        from core import pool as pool_mod
        print("池", NEW_POOL_NAME, "\n  条件：")
        for c in (pool_mod.get_pool(SRC_POOL).get("conds") or []) + EXTRA_CONDS:
            print("   ", c)
        print("策略", NEW_STRAT_NAME, " hold=1 无止损止盈  buy=", BUY_TRIGGER)
        print("\n（--dry：什么都没写）")
        return 0

    pid, sid = build_and_save()
    poss = []
    for x in args.poss.split(";"):
        if x.strip():
            mp, pct = x.split(",")
            poss.append((int(mp), float(pct)))
    out = []
    jobs = [(args.start, args.end, "全样本", True)]     # True = 所有仓位档
    if args.oos_start and args.oos_start > args.start:
        jobs.append((args.oos_start, args.end, "样本外", False))
    for stt, en, tag, allpos in jobs:
        span = stt[:4] + ("起" if en[:4] != stt[:4] else "")
        for (mp, pct) in (poss if allpos else poss[:1]):
            nm = f"{NEW_POOL_NAME}·{NEW_STRAT_NAME}·{span}·{mp}×{pct:g}%（{tag}）"
            t0 = time.time()
            sub = submit(pid, sid, stt, en, nm,
                         {"max_positions": mp, "position_pct": pct})
            r = wait_run(sub["run_id"], sub["job_id"])
            m = r.get("metrics") or {}
            row = {"run_id": sub["run_id"], "name": nm, "start": stt, "end": en, "tag": tag,
                   "pool_id": pid, "strategy_id": sid, "mp": mp, "pct": pct,
                   "ret": m.get("total_return"), "dd": m.get("max_drawdown"),
                   "trades": m.get("trade_count"), "win": m.get("win_rate"),
                   "avg": m.get("avg_pnl_pct"), "annual": m.get("annual_return"),
                   "elapsed": round(time.time() - t0, 1)}
            out.append(row)
            print(f"  [{tag} {mp}×{pct:g}%] run={row['run_id']} 收益 {row['ret']}%  "
                  f"年化 {row['annual']}%  回撤 {row['dd']}%  {row['trades']} 笔  "
                  f"胜率 {row['win']}%  均笔 {row['avg']}%  ({row['elapsed']}s)", flush=True)
    json.dump({"pool_id": pid, "strategy_id": sid, "runs": out},
              open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("→", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
