# -*- coding: utf-8 -*-
"""结构级调参台：在 run38 的紧凑缓存上直接换「买入触发 / 卖出信号」数组。

run38 缓存里每只票已存有 open/high/low/close 与 entry/in_pool 掩码；
「高开买 / 无条件买 / 不追高」这类触发规则只是 open 与 ref(close,1) 的比较，
可以直接在缓存数组上重算，**不用重跑 prepare_data**。
只有改「池条件」（需要 body/turnover/limit_up 等指标）才要重算（stage B）。

用法:
    python tools/tune_eat2.py --verify        # gapup 数组必须与原 buy_trigger 逐值相等
    python tools/tune_eat2.py --stage 1       # 随机搜结构×风控
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402

from tune_eat1 import (OUT_JSON, TRAIN, VALID, base_cfg, evaluate, load_data,  # noqa: E402
                       prune, fmt)

OUT2 = os.path.join(ROOT, "tools", "_tune_eat2_result.json")


def build_arrays(data: dict) -> dict:
    """给每只票补 prev_close 与派生掩码，返回 {(code,key): array} 不改原数据。"""
    extra = {}
    for code, v in data.items():
        op, cl = v["open"], v["close"]
        prev = np.empty_like(cl)
        prev[0] = np.nan
        prev[1:] = cl[:-1]
        with np.errstate(invalid="ignore"):
            gapup = op > prev
            extra[(code, "prev_close")] = prev
            extra[(code, "buy:gapup")] = gapup
            extra[(code, "buy:always")] = np.ones(len(cl), dtype=bool)
            extra[(code, "buy:smallgap")] = gapup & (op < prev * 1.03)
            extra[(code, "buy:midgap")] = gapup & (op >= prev * 1.02) & (op < prev * 1.05)
            extra[(code, "sell:loser_open")] = op < prev
            extra[(code, "sell:red_close")] = cl < prev
    return extra


BUY_VARS = ["orig", "gapup", "always", "smallgap", "midgap"]
SELL_VARS = ["none", "loser_open", "red_close"]


def variant_data(data: dict, extra: dict, buy: str, sell: str) -> dict:
    """浅拷贝替换 buy_trigger / sell_signal 两个数组。"""
    out = {}
    for code, v in data.items():
        d = dict(v)  # 浅拷贝：numpy 数组按引用，只换两个键
        d["buy_trigger"] = (extra[(code, f"buy:{buy}")] if buy != "orig"
                            else v["buy_trigger"])
        d["sell_signal"] = (extra[(code, f"sell:{sell}")] if sell != "none"
                            else v["sell_signal"])
        out[code] = d
    return out


SPACE2 = {
    "buy":  BUY_VARS,
    "sell": SELL_VARS,
    "pool_exit": [True, False],
    "stop": [None, -2, -3, -5, -8],
    "tp":   [None, 2, 3, 5, 8],
    "hold": [1, 2, 3, 5, 8],
    "sell_px": ["open", "close"],
    "hold_px": ["close", "open"],
    "slip": [0.005, 0.002],
    "npos": [5, 10],
}


def sample(rng, space):
    return {k: rng.choice(v) for k, v in space.items()}


def to_runq(v: dict) -> dict:
    """eat2 向量 → eat1 的 run 参数向量（buy 走变体，不进 exec）。

    ⚠ 未来函数修正：`red_close`（当日收盘 < 昨收）与 `pool_exit`（当日不在池）
    都是**收盘才知道**的信号，若再按当日开盘价成交就是偷看未来
    （auto_strategies.py 注释里同一条规矩）。这里强制它们只能收盘成交。
    """
    sell_px = v["sell_px"]
    if v["sell"] == "red_close" or v["pool_exit"]:
        sell_px = "close"
    return {"pool_exit": v["pool_exit"], "stop": v["stop"], "tp": v["tp"],
            "hold": v["hold"], "buy_px": "open", "sell_px": sell_px,
            "hold_px": v["hold_px"], "slip": v["slip"], "npos": v["npos"],
            "ppct": 10}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--stage", type=int, default=0)
    ap.add_argument("--seed", type=int, default=11)
    args = ap.parse_args()

    cache_info: dict = {}
    cfg = base_cfg()
    data = load_data(cfg, cache_info)
    print(f"数据 {len(data)} 只 cache={cache_info.get('cache')}", flush=True)
    data = prune(data, cfg)
    extra = build_arrays(data)

    if args.verify:
        bad = 0
        for code, v in data.items():
            if not np.array_equal(v["buy_trigger"], extra[(code, "buy:gapup")]):
                bad += 1
        print(f"gapup vs 原 buy_trigger 不一致票数：{bad}/{len(data)}")
        return

    if args.stage:
        rng = random.Random(args.seed)
        rows = []
        t0 = time.time()
        cur_key = None
        cur_dv = None
        for i in range(args.stage):
            q = sample(rng, SPACE2)
            key = (q["buy"], q["sell"])
            if key != cur_key:
                cur_dv = variant_data(data, extra, q["buy"], q["sell"])
                cur_key = key
            try:
                m = evaluate(cur_dv, cfg, to_runq(q), {"TRAIN": TRAIN, "VALID": VALID})
            except Exception as e:  # noqa: BLE001
                print(f"  !! {q} → {type(e).__name__}: {e}")
                continue
            rows.append({"q": q, "metrics": m})
            if i % 100 == 0:
                print(f"[{i+1}/{args.stage}] {time.time()-t0:.0f}s  "
                      f"{q['buy']}/{q['sell']}  " + fmt({"q": to_runq(q), "metrics": m}),
                      flush=True)
        with open(OUT2, "w", encoding="utf-8") as f:
            json.dump({"space": {k: [str(x) for x in v] for k, v in SPACE2.items()},
                       "rows": rows}, f, ensure_ascii=False, indent=1)
        print(f"\n共 {len(rows)} 次 {time.time()-t0:.0f}s → {OUT2}")
        rows.sort(key=lambda r: -(r["metrics"]["TRAIN"].get("annual_return") or -100))
        print("\nTOP 30（TRAIN 年化）：")
        for r in rows[:30]:
            q = r["q"]
            print(f"{q['buy']:>8}/{q['sell']:<10} "
                  + fmt({"q": to_runq(q), "metrics": r["metrics"]}))


if __name__ == "__main__":
    main()
