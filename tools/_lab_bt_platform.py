# -*- coding: utf-8 -*-
"""把「收紧实验」挑出来的方案，用**平台回测**跑成正式记录（/backtest 里能看、能点详情）。

为什么要走平台而不是实验室脚本：实验室跑的是内存里的 `run_backtest`，跑完只剩一行
数字；平台跑一次会落 `backtest_run` + 逐笔 `backtest_trade` + 资金曲线，历史回测记录
页能看到、能点开看每笔买卖 —— 这才是「能复盘」的形态。

⚠ 口径提醒（2026-09-30）：`_lab_tight_result.md` 里的收益是**旧引擎**（卖出侧未来函数）
跑出来的，虚高一大截（S4 形状 `close<ma10` 尤其严重）。本脚本跑的是**修好的引擎**，
同样的组合数字会低很多 —— 两边的差不是 bug，是那个 bug 被修掉了。

流程（`--mode` 决定收紧条件落在哪）：
    none     原池不动，拿**现在的**条件跑（基线对照）
    copy     收紧条件写进**新建的副本池**（原名 + 「·收紧」），原池一字不动
    inplace  收紧条件**原地追加**到原池（`_lab_apply_tight` 的同款加法，带 `# 收紧：…` 注释）

然后逐条提交 `POST /api/backtest/start`（universe = `pool:<id>`），轮询 job 到 done，
把 run_id / 收益 / 回撤 / 笔数 / 耗时打成表，并写 json。

    python tools/_lab_bt_platform.py --mode copy --strategy 101 --start 2025-01-01 --end 2025-12-31
    python tools/_lab_bt_platform.py --mode none --pools 223 --strategy 101   # 先跑一条试试
    python tools/_lab_bt_platform.py --mode copy --holds 20 --pick cut --out tools/_lab_bt_runs.json
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

BASE = "http://127.0.0.1:8777"
HOLDS = (10, 20)


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
           hold: int | None = None) -> dict:
    payload = {"strategy_id": strategy_id, "universe": f"pool:{pool_id}",
               "start": start, "end": end, "name": name}
    if hold:
        # ⚠ 持仓天数**不进回测缓存键**（重跑引擎很便宜），但 `resolve_params` 的优先级是
        #   「默认值 ← 表单 ← 策略自带」⇒ 策略自带 risk 会**盖掉**这里传的值。所以默认
        #   不传（`--holds` 留空），让策略自带口径生效，名字也不标持有天数 —— 免得记录
        #   写着「持20」其实是策略自带的 15 天。
        payload["params"] = {"max_hold_days": hold}
    r = api("POST", "/api/backtest/start", payload)
    if not r.get("ok"):
        raise RuntimeError(r.get("error") or r)
    return r["data"]


def wait_run(run_id: int, job_id: str, budget: int = 3600) -> dict:
    """轮询 job，再取一次 run 详情（metrics 只在 run 里）。"""
    t0 = time.time()
    while time.time() - t0 < budget:
        j = (api("GET", "/api/jobs/" + job_id).get("data") or {})
        stt = j.get("status")
        if stt in ("done", "failed", "canceled", "error"):
            break
        time.sleep(2)
    r = api("GET", f"/api/backtest/runs/{run_id}").get("data") or {}
    if r.get("status") != "done":
        raise RuntimeError(f"run={run_id} 状态 {r.get('status')} "
                           f"{r.get('progress_msg') or ''}")
    r["_elapsed"] = round(time.time() - t0, 1)
    return r


# ------------------------------------------------------------------- 收紧方案挑选
def pick_plans(min_cut: float, max_loss: float, min_avg: float, pick: str,
               only: set[int] | None) -> list[dict]:
    """复用 `_lab_apply_tight` 的挑选规则产出 (池 → 最优收紧变体)。"""
    from tools import _lab_apply_tight as ap
    from tools import _lab_tighten as tight

    rows = ap.load_rows()
    by = {(r["pool_id"], r["var"]): r for r in rows}
    out = []
    for pid in sorted({r["pool_id"] for r in rows}):
        if only is not None and pid not in only:
            continue
        base = by.get((pid, "T0"))
        if not base or not base.get("avg"):
            continue
        cands = []
        for (qid, v), r in by.items():
            if qid != pid or v == "T0":
                continue
            cut = (base["avg"] - r["avg"]) / base["avg"] * 100
            # ⚠ 判收益用的档位固定 10/20 —— 收紧结果里只存了这两档（h10/h20），
            #   跟「这次跑几档」无关（不传 holds 时 holds=[None]）。
            deltas = []
            ok = True
            for h in HOLDS:
                rb = (base.get(f"h{h}") or {}).get("ret")
                rv = (r.get(f"h{h}") or {}).get("ret")
                if rb is None or rv is None:
                    ok = False
                    break
                deltas.append(float(rv) - float(rb))
            if not ok:
                continue
            if cut < min_cut or min(deltas) < -max_loss or r["avg"] < min_avg:
                continue
            cands.append((cut, sum(deltas) / len(deltas), r, deltas))
        if not cands:
            continue
        cands.sort(key=lambda x: -x[0] if pick == "cut" else -x[1])
        cut, _g, r, deltas = cands[0]
        extra = r.get("extra") or ap.VARIANT_EXTRA.get(r["var"], [])
        out.append({"pool_id": pid, "pool": base["pool"], "var": r["var"],
                    "base_avg": base["avg"], "avg": r["avg"], "cut": cut,
                    "deltas": deltas, "extra": list(extra),
                    # 旧引擎（卖出侧未来函数）跑出来的数字，只用来做对照 —— 别当真
                    "old_ret": {h: (r.get(f"h{h}") or {}).get("ret") for h in HOLDS},
                    "old_base_ret": {h: (base.get(f"h{h}") or {}).get("ret")
                                     for h in HOLDS}})
    return out


def apply_tight(plan: dict, mode: str, tag: str) -> int:
    """返回这次回测要用的 pool_id。"""
    from core import pool as pool_mod
    cur = pool_mod.get_pool(plan["pool_id"])
    if not cur:
        raise RuntimeError(f"池#{plan['pool_id']} 不存在")
    if mode == "none":
        return plan["pool_id"]
    add = [c for c in plan["extra"]
           if str(c).split("#", 1)[0].strip()
           not in {str(x).split("#", 1)[0].strip() for x in (cur.get("conds") or [])}]
    if not add:
        return plan["pool_id"]          # 已经有了，幂等
    note = (f"【{tag}】收紧 {plan['var']}：日均 {plan['base_avg']:.0f}→{plan['avg']:.0f}"
            f"（-{plan['cut']:.0f}%）")
    conds = list(cur.get("conds") or []) + [f"{c}   # {note}" for c in add]
    if mode == "inplace":
        new_note = (cur.get("note") or "").rstrip() + "\n\n" + note
        pool_mod.save_pool(cur["name"], new_note.strip(), cur.get("base") or "all",
                           conds, bool(cur.get("pool_exit")), pid=cur["id"])
        return cur["id"]
    if mode == "copy":
        name = f"{cur['name']}·收紧"
        old = pool_mod.get_pool_by_name(name)
        new_note = (cur.get("note") or "").rstrip() + "\n\n" + note \
            + f"（原池 #{cur['id']} 的副本，原池未动）"
        if old:                          # 幂等：重跑就更新同一条，不越建越多
            pool_mod.save_pool(name, new_note.strip(), cur.get("base") or "all",
                               conds, bool(cur.get("pool_exit")), pid=old["id"])
            return old["id"]
        return pool_mod.save_pool(name, new_note.strip(), cur.get("base") or "all",
                                  conds, bool(cur.get("pool_exit")))
    raise ValueError("mode 只能是 none / copy / inplace")


def main() -> int:
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("--mode", choices=("none", "copy", "inplace"), default="copy")
    ap_.add_argument("--strategy", type=int, required=True, help="买卖策略 id")
    ap_.add_argument("--start", default="2025-01-01")
    ap_.add_argument("--end", default="2025-12-31")
    ap_.add_argument("--holds", default="", help="持仓天数；留空=用策略自带（默认，见 submit 注释）")
    ap_.add_argument("--pools", default="", help="只跑这些池（逗号分隔 id）")
    ap_.add_argument("--min-cut", type=float, default=20.0)
    ap_.add_argument("--max-loss", type=float, default=5.0)
    ap_.add_argument("--min-avg", type=float, default=3.0)
    ap_.add_argument("--pick", choices=("cut", "gain"), default="cut")
    ap_.add_argument("--limit", type=int, default=0, help="只跑前 N 个池（先探路）")
    ap_.add_argument("--tag", default="收紧实验", help="写进副本池说明与回测名里的标记")
    ap_.add_argument("--out", default="")
    args = ap_.parse_args()

    only = {int(x) for x in args.pools.split(",") if x.strip()} or None
    holds = [int(x) for x in args.holds.split(",") if x.strip()] or [None]
    sname = ((api("GET", f"/api/strategies/{args.strategy}").get("data") or {})
             .get("name") or f"#{args.strategy}")
    print(f"买卖策略 #{args.strategy} {sname}\n")

    # ⚠ `mode=none`（跑基线）也走同一份 plan：只有它带着「旧引擎数字」和「该池有没有
    #   收紧方案」，基线才能跟收紧那条**成对**出现在记录页里对比 —— 否则「收紧是变好
    #   还是变坏」没有分母。
    plans = pick_plans(args.min_cut, args.max_loss, args.min_avg, args.pick, only)
    if args.mode == "none" and only:
        from core import pool as pool_mod
        got = {p["pool_id"] for p in plans}
        for p in pool_mod.list_pools():      # 指定了没方案的池 → 也补一条基线
            if p["id"] in only and p["id"] not in got:
                plans.append({"pool_id": p["id"], "pool": p["name"], "var": "T0",
                              "base_avg": None, "avg": None, "cut": 0.0,
                              "deltas": [], "extra": [],
                              "old_ret": {}, "old_base_ret": {}})
    if args.limit:
        plans = plans[:args.limit]
    if not plans:
        print("没有要跑的组合。")
        return 1

    print(f"模式 {args.mode} · 策略 #{args.strategy} · 区间 {args.start}~{args.end}"
          f" · 持有 {args.holds} · 共 {len(plans)} 个池\n")
    results = []
    for i, pl in enumerate(plans, 1):
        try:
            pid = apply_tight(pl, args.mode, args.tag)
        except Exception as e:
            print(f"  !! 池#{pl['pool_id']} {pl['pool']} 收紧失败：{e}")
            continue
        for h in holds:
            tight = f"·收紧{pl['var']}" if args.mode != "none" else "·原池基线"
            # ⚠ 跑多年区间时名字里只写起始年会跟「只跑那一年」的记录混（2023 起 ≠ 2023 年）
            span = args.start[:4] + ("起" if args.end[:4] != args.start[:4] else "")
            nm = f"{pl['pool']}{tight} · {sname} · {span}" \
                + (f" · 持{h}" if h else "")
            t0 = time.time()
            try:
                sub = submit(pid, args.strategy, args.start, args.end, nm, hold=h)
                r = wait_run(sub["run_id"], sub["job_id"])
            except Exception as e:
                print(f"  !! [{i}/{len(plans)}] {nm} 回测失败：{e}")
                continue
            m = r.get("metrics") or {}
            row = {"pool_id": pl["pool_id"], "pool": pl["pool"], "var": pl["var"],
                   "run_pool_id": pid, "hold": h, "run_id": sub["run_id"],
                   "name": nm, "ret": m.get("total_return"),
                   "dd": m.get("max_drawdown"), "trades": m.get("trade_count"),
                   "win": m.get("win_rate"), "elapsed": round(time.time() - t0, 1),
                   "base_avg": pl["base_avg"], "avg": pl["avg"], "cut": pl["cut"],
                   "extra": pl["extra"], "old_ret": pl.get("old_ret"),
                   "old_base_ret": pl.get("old_base_ret"), "strategy": sname}
            results.append(row)
            oldmap = pl.get("old_ret") if args.mode != "none" else pl.get("old_base_ret")
            old = (oldmap or {}).get(10) or (oldmap or {}).get(20)
            print(f"  [{i}/{len(plans)}] {nm[:34]:<36} run={row['run_id']:<4}"
                  f" 收益 {row['ret']}%  回撤 {row['dd']}%  {row['trades']} 笔"
                  f"  {row['elapsed']}s" + (f"   （旧引擎 {old}%）" if old else ""))

    if args.out:
        with open(os.path.join(ROOT, args.out), "w", encoding="utf-8") as fh:
            json.dump(results, fh, ensure_ascii=False, indent=1)
        print(f"\n已写 {args.out}（{len(results)} 条）")
    if results:
        print("\n回测记录页：/backtest —— 上面每个 run_id 都能点开看逐笔明细。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
