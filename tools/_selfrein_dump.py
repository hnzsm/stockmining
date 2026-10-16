# -*- coding: utf-8 -*-
"""只读：把原池 156 与买卖策略 58 的口径 dump 出来，供落库新池/新策略复用。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import pool as P, strategy as S          # noqa: E402

RUN151_POOL = 156
RUN151_STRAT = 58

p = P.get_pool(RUN151_POOL)
print("=" * 70)
print("POOL", RUN151_POOL)
print(json.dumps(p, ensure_ascii=False, indent=2, default=str))

s = S.get_strategy(RUN151_STRAT)
print("=" * 70)
print("STRATEGY", RUN151_STRAT)
print(json.dumps(s, ensure_ascii=False, indent=2, default=str))
