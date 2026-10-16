# -*- coding: utf-8 -*-
"""验 `tools/auto_strategies.install_results()` 落库的确实是**普通**策略（builtin=0）。

「取消内置」的落地含义 = 「变成普通策略」⇒ 不能只看列值，必须验**删得掉**。
做法：喂合成结果跑真实写库路径（`install_results`），断言

  1) 选股策略 / 买卖策略落库后 `builtin=0`；
  2) 两条都真删得掉（`pool.delete_pool()` 返回 True / strategy 的 DELETE 影响 1 行）；
  3) 同名再跑一次走 UPDATE 分支时**不动 builtin**（先手工把测试行标成 1，跑完还得是 1）；
  4) 收尾：本次造的行全清掉，且库里 builtin=1 的条数回到跑之前（没留垃圾）。

用法：python tools/_check_auto_install.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import db, pool as pool_mod, strategy as st, universe  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "auto_strategies_chk", os.path.join(ROOT, "tools", "auto_strategies.py"))
auto = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(auto)

TAG = "_T_CHK_%d" % int(time.time())
POOL_NAME = TAG + " · 选股"
fails = []


def check(desc, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + desc + ("" if cond else f"   —— {detail}"))
    if not cond:
        fails.append(desc)


def _bi_state():
    return (
        [r["name"] for r in db.query(
            "SELECT name FROM strategy WHERE builtin=1 ORDER BY id")],
        [r["name"] for r in db.query(
            "SELECT name FROM pool_strategy WHERE builtin=1 ORDER BY id")],
    )


def _cleanup():
    db.execute("DELETE FROM pool_strategy WHERE name LIKE %s", (TAG + "%",))
    db.execute("DELETE FROM strategy WHERE name LIKE %s", (TAG + "%",))


def _fake():
    """合成一条「收益为正」的结果 —— 字段形状照抄 `run_one()` 的返回值。"""
    return {
        "name": TAG,
        "note": "自动检查用（跑完即删）",
        "universe": universe.UNIVERSE_ALL,
        "range": "2024-01-01~2025-01-01",
        "pool": ["amount > 0"],
        "config": {"buy_trigger": ["close > pre_close"], "sell_signal": ["close < pre_close"]},
        "metrics": {"total_return": 1.0, "annual_return": 1.0, "max_drawdown": 1.0,
                    "trade_count": 5, "win_rate": 50.0, "avg_hold_days": 3.0},
    }


def main():
    print(f"测试前缀 {TAG}")
    _cleanup()                      # 清掉同秒重复跑的残留
    bi_before = _bi_state()

    # ---- 第一轮：新建（INSERT 分支）------------------------------------------
    print("\n[1] 新建分支：install_results 落库")
    names = auto.install_results([_fake()], min_trades=1)
    check("返回了入库的策略名", names == [TAG], str(names))

    p = db.query_one("SELECT id, name, builtin FROM pool_strategy WHERE name=%s", (POOL_NAME,))
    check("选股策略已落库", p is not None, "没查到 " + POOL_NAME)
    check("选股策略 builtin=0（普通策略）", p is not None and p["builtin"] == 0,
          f"builtin={p and p['builtin']}")

    s = db.query_one("SELECT id, name, builtin FROM strategy WHERE name=%s", (TAG,))
    check("买卖策略已落库", s is not None, "没查到 " + TAG)
    check("买卖策略 builtin=0（普通策略）", s is not None and s["builtin"] == 0,
          f"builtin={s and s['builtin']}")

    # ---- 第二轮：真删得掉（这才是「普通策略」的落地含义）--------------------
    print("\n[2] 可删除性：内置删不掉，普通策略必须删得掉")
    check("pool.delete_pool() 返回 True（真删了）", pool_mod.delete_pool(p["id"]) is True,
          "返回 False ⇒ 库里仍标着内置")
    gone = db.query_one("SELECT id FROM pool_strategy WHERE id=%s", (p["id"],))
    check("选股策略行已从库里消失", gone is None, str(gone))
    n = db.execute("DELETE FROM strategy WHERE id=%s AND builtin=0", (s["id"],))
    check("买卖策略 DELETE ... AND builtin=0 影响 1 行", n == 1, f"affected={n}")

    # ---- 第三轮：同名（UPDATE 分支）不动 builtin ----------------------------
    print("\n[3] 更新分支：同名已存在时只改内容、不动 builtin")
    sid = db.insert(
        "INSERT INTO strategy (name, note, timeframe, config, builtin) VALUES (%s,%s,%s,%s,1)",
        (TAG, "占位（把 builtin 先标成 1，看更新会不会抹掉）", "daily",
         st.config_json({"buy_trigger": ["close > pre_close"]})))
    auto.install_results([_fake()], min_trades=1)          # 同名 ⇒ 走 UPDATE
    s2 = db.query_one("SELECT note, builtin FROM strategy WHERE id=%s", (sid,))
    check("更新分支没有把 builtin 从 1 改成 0", s2 is not None and s2["builtin"] == 1,
          f"builtin={s2 and s2['builtin']}")
    check("更新分支确实改了 note（走的是 UPDATE 而不是没动）",
          s2 is not None and s2["note"] != "占位（把 builtin 先标成 1，看更新会不会抹掉）",
          str(s2 and s2["note"])[:40])
    check("笔数不足的候选不入库（min_trades 门槛仍在）",
          auto.install_results([_fake()], min_trades=999) == [])

    # ---- 收尾 ---------------------------------------------------------------
    print("\n[4] 收尾：清理 + 库里内置条数回到原样")
    _cleanup()
    left = db.query("SELECT id FROM pool_strategy WHERE name LIKE %s", (TAG + "%",)) \
        + db.query("SELECT id FROM strategy WHERE name LIKE %s", (TAG + "%",))
    check("本次造的行已全部删除", not left, str(left))
    check("builtin=1 的条数回到跑之前", _bi_state() == bi_before,
          f"{bi_before} → {_bi_state()}")

    print("\n" + ("全部通过" if not fails else f"FAIL {len(fails)} 条：{fails}"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
