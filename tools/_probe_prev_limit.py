# -*- coding: utf-8 -*-
"""探针：验证「昨日未涨停」条件表达式 ref(limit_up, 1) = 0 的语义。

造 4 组合成日线，逐根对拍 indicators.compute 出来的 limit_up 与 expr 的求值结果。
跑法： .venv/Scripts/python.exe tools/_probe_prev_limit.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from core import expr as E
from core import indicators as I

COND = "ref(limit_up, 1) = 0"


def mk(ts_code, name, rets):
    """rets: 每日相对昨收的涨跌幅（小数），首日为基准。返回升序日线 df。"""
    close = [10.0]
    for r in rets:
        close.append(round(close[-1] * (1 + r), 4))
    n = len(close)
    df = pd.DataFrame({
        "trade_date": pd.date_range("2026-01-05", periods=n, freq="D").strftime("%Y%m%d"),
        "open": close, "high": [c * 1.001 for c in close], "low": [c * 0.999 for c in close],
        "close": close, "vol": [10000.0] * n, "amount": [10000.0] * n,
    })
    df["pre_close"] = df["close"].shift(1)
    df["pct_chg"] = (df["close"] / df["pre_close"] - 1) * 100   # 库里 raw 字段，非 indicators 派生
    return df, ts_code, name


CASES = [
    # 名称, 代码, 涨跌幅序列（第一天是基准 10 元→10 元，涨跌幅序列对应后面每天）
    ("主板·昨日涨停", "600000.SH", "主板", [0.0, 0.10, 0.02]),
    ("主板·昨日未涨停", "600000.SH", "主板", [0.0, 0.03, 0.02]),
    ("创业板·昨日+10%(未涨停)", "300001.SZ", "创业板", [0.0, 0.10, 0.02]),
    ("创业板·昨日+20%(涨停)", "300001.SZ", "创业板", [0.0, 0.20, -0.02]),
    ("ST·昨日+5%(涨停)", "600002.SH", "*ST测试", [0.0, 0.05, 0.01]),
]

print(f"表达式：{COND!r}")
print(f"字段名集合：{sorted(E.extract_identifiers(COND))}")
ok, msg = E.check_expr(COND, {"limit_up"})
print(f"check_expr -> {ok} {msg}")
print()

print(f"{'场景':<26}{'T-1涨跌%':>10}{'T-1 limit_up':>14}{'T limit_up':>12}{'T日条件值':>11}")
print("-" * 74)
bad = 0
for label, code, name, rets in CASES:
    df, tc, nm = mk(code, name, rets)
    d = I.compute(df, ts_code=code, name=name, only={"limit_up"})
    lu = d["limit_up"].to_numpy()
    pct = (d["close"] / d["pre_close"] - 1) * 100
    cond = E.evaluate_bool(COND, d, len(d))
    last = int(cond[-1])
    exp = 0 if lu[-2] == 1 else 1     # 昨日涨停 ⇒ 条件应为 False(0)
    flag = "OK" if last == exp else "MISMATCH"
    if last != exp:
        bad += 1
    print(f"{label:<26}{pct.iloc[-2]:>10.2f}{lu[-2]:>14d}{lu[-1]:>12d}"
          f"{str(bool(last)):>11}  {flag}")

print()
print(f"对拍结果：{'全部一致' if bad == 0 else f'{bad} 项不一致'}")

# 反例：为什么会踩坑 —— 用 pct_chg 阈值判「涨停」在创业板/ST 上会错
print()
print("反例核对（用 ref(pct_chg,1) >= 9.8 当「昨日涨停」）：")
for label, code, name, rets in CASES:
    df, tc, nm = mk(code, name, rets)
    d = I.compute(df, ts_code=code, name=name, only={"limit_up"})
    lu = int(d["limit_up"].iloc[-2])
    wrong = bool(E.evaluate_bool("ref(pct_chg, 1) >= 9.8", d, len(d))[-1])
    print(f"  {label:<26} 真涨停={lu}  阈值法判定={int(wrong)}"
          f"{'   ← 误判' if wrong != bool(lu) else ''}")
