# -*- coding: utf-8 -*-
"""一次性数据迁移：把「选股逻辑」从买卖策略剥离到选股策略。

2026-09-18 概念归位后：

    pool_strategy = 选股策略（选哪些票：base + conds + filt + pool_exit）
    strategy      = 买卖策略（何时买卖：buy_trigger / sell_signal / score / risk / exec）

而在此之前，`strategy.config` 里塞着 `entry` / `entry_any` / `weekly_filter` /
`pool.where` 这四坨选股逻辑。本脚本把它们搬成**同名的选股策略**，再把 strategy
里这些字段清空 —— 之后买卖策略页就只剩买卖规则，回测时「选股策略 × 买卖策略」
两个下拉一配即可。

旧的内置买卖策略样例（那批 entry 里写了选股条件的）**直接删除**，
买卖策略页只保留 `strategy.BUILTIN_STRATEGIES` 里的两个纯买卖样例
（用户当时的要求是「留两个样例」）。

用法::

    python tools/migrate_to_buysell.py            # 预演（只打印计划，不改库）
    python tools/migrate_to_buysell.py --apply    # 真跑
    python tools/migrate_to_buysell.py --apply --keep-old   # 不删旧内置策略

设计要点：

* **幂等**：选股策略按「base + conds 完全一致」去重，重跑不会再建一遍；
  已经搬空（entry 全空）的策略不会再有动作。
* **周线策略的选股条件要加 `w_` 前缀**：老配置里 `timeframe=weekly` 的策略，
  它的 entry 是在**周K**上求值的；搬到选股策略后会跟着买卖策略的周期走，
  不翻译就会悄悄变成「日线布林下轨」（形状对、口径错）。所以整条条件里的
  日线字段名统一加 `w_`，语义落到「最近一根已收盘周K」。
* **不动有风险的数据**：非内置策略只清选股字段、绝不删行；
  只有 builtin=1 且不在 BUILTIN_STRATEGIES 里的行才会被删。

回滚：迁移前的备份在 `data/backup_pre_buysell_{strategy,pool_strategy,backtest_run}.sql`
（由 `tools/backup_tables.py` 产出），直接灌回即可。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db, expr, pool as pool_mod, strategy as st, universe  # noqa: E402

# ---------------------------------------------------------------------------


def _weekly_ish(cond: str) -> str:
    """把条件里的日线字段名统一加上 `w_` 前缀（周线策略的选股条件搬进池子时用）。"""
    out = cond
    for n in expr.identifiers_in_order(cond):
        if n.startswith("w_"):
            continue
        out = re.sub(r"(?<![A-Za-z0-9_])" + re.escape(n) + r"(?![A-Za-z0-9_])",
                     "w_" + n, out)
    return out


def _sel_conds(cfg: dict) -> tuple[list[str], list[str]]:
    """从买卖策略配置里抽出「选股逻辑」→ (池子条件, 丢弃的或条件)。

    池子条件是 **AND** 语义，所以 `entry_any` 这种 OR 组要合成一条
    `(a) or (b)` 再放进去；合成后校验不过就丢掉（老数据里可能有写法不适配的）。
    """
    weekly = cfg.get("timeframe") == "weekly"
    tf = (lambda c: _weekly_ish(c)) if weekly else (lambda c: c)

    conds: list[str] = []
    for src in ("entry", "weekly_filter"):
        conds += [tf(str(c).strip()) for c in (cfg.get(src) or []) if str(c).strip()]
    pool = cfg.get("pool") or {}
    conds += [tf(str(c).strip()) for c in (pool.get("where") or []) if str(c).strip()]

    dropped: list[str] = []
    any_ = [str(c).strip() for c in (cfg.get("entry_any") or []) if str(c).strip()]
    if any_:
        merged = "(" + ") or (".join(tf(c) for c in any_) + ")"
        ok, _msg = expr.check_expr(merged)
        (conds if ok else dropped).append(merged)
    return conds, dropped


def _filt_of(cfg: dict) -> dict:
    """老配置里 strategy.config.pool 的静态过滤项（剔ST/板块/上市天数/成交额）。"""
    p = cfg.get("pool") or {}
    return {k: p[k] for k in universe.DEFAULT_FILT if k in p}


def _sel_fields_present(cfg: dict) -> bool:
    p = cfg.get("pool") or {}
    return bool(cfg.get("entry") or cfg.get("entry_any") or cfg.get("weekly_filter")
                or p.get("where") or p.get("pool_exit"))


def _unique_pool_name(base_name: str, taken: set[str]) -> str:
    name = (base_name or "未命名选股策略").strip()
    if name not in taken:
        return name
    for i in range(2, 100):
        cand = f"{name} #{i}"
        if cand not in taken:
            return cand
    return f"{name} #{id(base_name) % 1000}"


# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="把选股逻辑从买卖策略迁到选股策略")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只预演）")
    ap.add_argument("--keep-old", action="store_true",
                    help="保留旧的内置买卖策略（默认删除，只留两个样例）")
    args = ap.parse_args()

    db.init_schema()

    print("=" * 78)
    print("步骤 1  确保内置内容就位（买卖策略 2 个样例 + 选股策略预设）")
    print("=" * 78)
    keep_names = {b["name"] for b in st.BUILTIN_STRATEGIES}
    print(f"  买卖策略保留样例：{sorted(keep_names)}")
    if args.apply:
        n_bs = st.install_builtins()
        n_pl = pool_mod.install_builtins()
        print(f"  新增内置买卖策略 {n_bs} 个 / 内置选股策略 {n_pl} 个")
    else:
        have_s = {r["name"] for r in st.list_strategies()}
        have_p = {q["name"] for q in pool_mod.list_pools()}
        miss_s = [b["name"] for b in st.BUILTIN_STRATEGIES if b["name"] not in have_s]
        miss_p = [p["name"] for p in universe.PRESETS if p["name"] not in have_p]
        print(f"  [预演] 待补内置买卖策略 {len(miss_s)} 个 {miss_s}")
        print(f"  [预演] 待补内置选股策略 {len(miss_p)} 个 {miss_p}")

    rows = st.list_strategies()
    pools = pool_mod.list_pools()
    # 预演时把「待补的预设」也算进候选，否则去重判据看不到它们，会误报要新建一堆
    if not args.apply:
        have = {q["name"] for q in pools}
        for p in universe.PRESETS:
            if p["name"] not in have:
                pools.append({"id": None, "name": p["name"], "base": p.get("base"),
                              "conds": list(p["where"]), "builtin": 1})
    taken = {p["name"] for p in pools}

    print()
    print("=" * 78)
    print("步骤 2  扫描需要迁移的买卖策略")
    print("=" * 78)

    plans: list[dict] = []
    for r in rows:
        cfg = r["config"]
        if isinstance(cfg, (str, bytes)):
            cfg = json.loads(cfg)
        if not _sel_fields_present(cfg):
            continue
        conds, dropped = _sel_conds(cfg)
        plan = {
            "row": r,
            "conds": conds,
            "dropped": dropped,
            "filt": _filt_of(cfg),
            "pool_exit": bool((cfg.get("pool") or {}).get("pool_exit")),
            "weekly": cfg.get("timeframe") == "weekly",
            "will_delete": bool(r["builtin"]) and r["name"] not in keep_names
                           and not args.keep_old,
        }
        plans.append(plan)

    if not plans:
        print("  （无需迁移：没有策略带选股条件）")
    for p in plans:
        act = "迁移 + 删除旧行" if p["will_delete"] else "迁移（保留行）"
        print(f"\n  · [{p['row']['id']}] {p['row']['name']!r}  tf={p['row']['timeframe']}"
              f"  builtin={p['row']['builtin']}  → {act}")
        print(f"      选股条件 {len(p['conds'])} 条：" +
              ("; ".join(p["conds"]) if p["conds"] else "(无)"))
        if p["weekly"]:
            print("      ⚠ 原为周线策略 → 条件已加 w_ 前缀")
        if p["filt"]:
            print(f"      静态过滤项：{p['filt']}")
        if p["pool_exit"]:
            print("      pool_exit=True（掉出池子即卖）")
        if p["dropped"]:
            print(f"      ⚠ 或条件合成失败被丢弃：{p['dropped']}")

    print()
    print("=" * 78)
    print("步骤 3  写入选股策略")
    print("=" * 78)
    created: list[tuple[str, int]] = []
    reused: list[tuple[str, str]] = []
    for p in plans:
        conds = p["conds"]
        if not conds and not p["filt"]:
            print(f"  · {p['row']['name']!r}：没有条件也没有过滤项，跳过")
            continue
        same = [q for q in pools if (q.get("base") or "all") == universe.UNIVERSE_ALL
                and (q.get("conds") or []) == conds]
        if same:
            reused.append((p["row"]["name"], same[0]["name"]))
            print(f"  · {p['row']['name']!r}：已存在等价选股策略 #{same[0]['id']} "
                  f"{same[0]['name']!r}，跳过新建")
            continue
        name = _unique_pool_name(p["row"]["name"], taken)
        note = (f"由买卖策略 #{p['row']['id']} {p['row']['name']!r} 迁移而来"
                f"（2026-09-18 概念拆分：选股逻辑归选股策略）。")
        if p["weekly"]:
            note += " 原策略是周线策略，条件已加 w_ 前缀。"
        if p["dropped"]:
            note += f" 或条件无法用 AND 表达，已丢弃：{p['dropped']}"
        if args.apply:
            pid = pool_mod.save_pool(
                name=name, note=note, base=universe.UNIVERSE_ALL, conds=conds,
                pool_exit=p["pool_exit"], filt=p["filt"] or universe.DEFAULT_FILT,
            )
            taken.add(name)
            print(f"  · 新建选股策略 #{pid} {name!r}（{len(conds)} 条条件）")
        else:
            taken.add(name)
            print(f"  · [预演] 将新建选股策略 {name!r}（{len(conds)} 条条件）")
        created.append((name, -1))

    print()
    print("=" * 78)
    print("步骤 4  清空买卖策略里的选股逻辑 / 删除旧内置策略")
    print("=" * 78)
    for p in plans:
        r = p["row"]
        cfg = r["config"]
        if isinstance(cfg, (str, bytes)):
            cfg = json.loads(cfg)
        if p["will_delete"]:
            if args.apply:
                db.execute("DELETE FROM strategy WHERE id=%s AND builtin=1", (r["id"],))
                print(f"  · 删除旧内置买卖策略 #{r['id']} {r['name']!r}")
            else:
                print(f"  · [预演] 将删除旧内置买卖策略 #{r['id']} {r['name']!r}")
            continue
        cfg["entry"] = []
        cfg["entry_any"] = []
        cfg["weekly_filter"] = []
        cfg["pool"] = {**universe.DEFAULT_FILT, "where": [], "pool_exit": False}
        if args.apply:
            st.save_strategy(r["name"], r.get("note") or "", cfg, sid=r["id"])
            print(f"  · 已清空 #{r['id']} {r['name']!r} 的选股条件（保留买卖规则）")
        else:
            print(f"  · [预演] 将清空 #{r['id']} {r['name']!r} 的选股条件（保留买卖规则）")

    # 兜底：旧内置里「一条选股条件都没有」的也在删除范围内（它们不在 BUILTIN_STRATEGIES 里）
    if not args.keep_old:
        for r in st.list_strategies():
            if not r["builtin"] or r["name"] in keep_names:
                continue
            if any(p["row"]["id"] == r["id"] for p in plans):
                continue   # 上面已处理
            if args.apply:
                db.execute("DELETE FROM strategy WHERE id=%s AND builtin=1", (r["id"],))
                print(f"  · 删除旧内置买卖策略 #{r['id']} {r['name']!r}（无选股条件）")
            else:
                print(f"  · [预演] 将删除旧内置买卖策略 #{r['id']} {r['name']!r}（无选股条件）")

    print()
    print("=" * 78)
    if args.apply:
        print("完成。最终状态：")
    else:
        print("预演结束（未改动数据库）。加 --apply 才真正执行。")
        print("下面是**当前**状态（预演不改库，所以还没变）：")
    print("=" * 78)
    for r in st.list_strategies():
        print(f"  买卖策略 #{r['id']} {r['name']!r} builtin={r['builtin']}")
    for q in pool_mod.list_pools():
        print(f"  选股策略 #{q['id']} {q['name']!r} builtin={q['builtin']} "
              f"conds={len(q['conds'])}")
    if reused:
        print("\n复用已有选股策略：")
        for old, new in reused:
            print(f"  · {old!r} → 已有 {new!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
