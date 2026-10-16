# -*- coding: utf-8 -*-
"""形态期望诊断：不看仓位引擎，直接统计「入池日 T → 次日 T+1」的收益分布。

回答三个问题：
  1. 形态命中后次日开盘买、当日收盘卖的期望是多少？（=run38 的毛期望口径）
  2. 高开买入过滤（gap-up）本身是不是在挑「更差」的票？
  3. 次日开盘/收盘、两日开盘分别什么符号？→ 决定买卖结构往哪边调。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import numpy as np  # noqa: E402

from tune_eat1 import base_cfg, load_data, prune  # noqa: E402


def stats(name, x: np.ndarray):
    if len(x) == 0:
        print(f"  {name:<28} 样本 0")
        return
    print(f"  {name:<28} n={len(x):<6} 均值 {x.mean()*100:+.3f}%  中位 "
          f"{np.median(x)*100:+.3f}%  胜率 {(x>0).mean()*100:.1f}%  P10 "
          f"{np.percentile(x,10)*100:+.2f}%  P90 {np.percentile(x,90)*100:+.2f}%")


def main():
    variant = sys.argv[1] if len(sys.argv) > 1 else "P0"
    if variant == "P0":
        cfg = base_cfg()
        data = prune(load_data(cfg), cfg)
    else:
        import tune_eat3 as e3
        cfg, data, _ = e3.load_variant(variant, e3.VARIANTS[variant])
        data = prune(data, cfg)
    print(f"[{variant}] 有效标的 {len(data)} 只\n")
    rows = []
    for code, v in data.items():
        op, cl = v["open"], v["close"]
        prev = np.concatenate(([np.nan], cl[:-1]))
        e = v["entry"] & v["in_pool"]
        idx = np.nonzero(e[:-2])[0]        # T，需要 T+2 存在
        for i in idx:
            t1 = i + 1
            if np.isnan(op[t1]) or np.isnan(cl[t1]) or np.isnan(prev[t1]) or np.isnan(cl[t1+1] if t1+1 < len(cl) else np.nan):
                continue
            gap = op[t1] / cl[i] - 1.0     # 次日开盘跳空
            rows.append((
                gap,
                cl[t1] / op[t1] - 1.0,     # T+1 开 → T+1 收
                op[t1+1] / op[t1] - 1.0,   # T+1 开 → T+2 开
                cl[t1+1] / op[t1] - 1.0,   # T+1 开 → T+2 收
                (cl[t1] > prev[t1]),       # T+1 是否收阳（对照上收）
            ))
    A = np.array(rows)
    gap, r1c, r2o, r2c = A[:, 0], A[:, 1], A[:, 2], A[:, 3]
    print(f"形态命中（次日可交易）总样本 {len(A)}")
    stats("全部：开→当日收", r1c)
    stats("全部：开→次日开", r2o)
    stats("全部：开→次日收", r2c)
    stats("仅高开(gap>0)：开→当日收", r1c[gap > 0])
    stats("仅高开(gap>0)：开→次日收", r2c[gap > 0])
    stats("不高开(gap<=0)：开→当日收", r1c[gap <= 0])
    stats("不高开(gap<=0)：开→次日收", r2c[gap <= 0])
    stats("高开1~3%：开→当日收", r1c[(gap > 0.01) & (gap < 0.03)])
    stats("高开>3%：开→当日收", r1c[gap > 0.03])
    stats("微高开0~1%：开→当日收", r1c[(gap > 0) & (gap <= 0.01)])


if __name__ == "__main__":
    main()
