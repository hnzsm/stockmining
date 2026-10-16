# -*- coding: utf-8 -*-
"""把收紧实验挑出的过滤条件**写回**选股策略（默认 dry-run，加 `--apply` 才落库）。

⚠ 只做**加法**：原条件一字不动，追加的过滤写在**末尾**，并带一行 `# 收紧：…` 注释
   （注释不是条件，见 `expr.strip_comment`；界面上能一眼看出这条是后来加的、为什么加）。
⚠ 幂等：原条件里已经有同一条表达式（剥掉注释后比对）就**跳过**，重复跑不会越加越多。
⚠ 只写**普通**池；内置那条「首板」要改的话得 `--force-builtin`（它受保护不是因为内置，
   而是因为它同时是界面预设的兜底，改了影响面大 —— 默认不强改，先拿给你看）。

挑选规则（先过滤再挑）：降幅 >= `--min-cut`、两档持有期的收益 Δ 都 >= `-max-loss`、
收紧后日均 >= `--min-avg`；在剩下的里按 `--pick` 挑：
  * `cut`（默认）= 降幅最大的那个（你要的是「少」）；
  * `gain`       = 收益提升最多的那个。

    python tools/_lab_apply_tight.py                 # 只看不改
    python tools/_lab_apply_tight.py --apply         # 落库
    python tools/_lab_apply_tight.py --pools 116,119 --apply
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import pool as pool_mod  # noqa: E402
from tools import _lab_tighten as tight  # noqa: E402

HOLDS = (10, 20)
# 变体 → 条件。⚠ 从 `_lab_tighten.VARIANTS` 回查，而不是信结果里的 `extra`：
#   从**日志**解析出来的行没有 extra 字段（日志只打了变体名），照它写会一条都加不上。
VARIANT_EXTRA = {k: list(c) for k, _n, c in tight.VARIANTS}


def load_rows():
    rows, seen = [], set()
    for f in sorted(glob.glob(os.path.join(ROOT, "tools", "_lab_tight_*.json"))):
        with open(f, encoding="utf-8") as fh:
            data = json.load(fh)
        for r in (data if isinstance(data, list) else data.get("rows", [])):
            k = (r.get("pool_id"), r.get("var"))
            if k in seen:
                continue
            seen.add(k)
            rows.append(r)
    return rows


def strip_comment(c: str) -> str:
    return str(c).split("#", 1)[0].strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-cut", type=float, default=20.0)
    ap.add_argument("--max-loss", type=float, default=5.0)
    ap.add_argument("--min-avg", type=float, default=3.0)
    ap.add_argument("--pick", choices=("cut", "gain"), default="cut")
    ap.add_argument("--pools", default="")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--force-builtin", action="store_true")
    args = ap.parse_args()

    rows = load_rows()
    by: dict[tuple, dict] = {}
    for r in rows:
        by[(r["pool_id"], r["var"])] = r
    pool_ids = sorted({r["pool_id"] for r in rows})
    if args.pools:
        want = {int(x) for x in args.pools.split(",") if x.strip()}
        pool_ids = [p for p in pool_ids if p in want]

    print(f"{'':<2}{'池':<22}{'日均':>8}{'→':>3}{'新':>7}{'降幅':>7}"
          f"{'持10Δ':>8}{'持20Δ':>8}  追加条件")
    plan = []
    for pid in pool_ids:
        base = by.get((pid, "T0"))
        if not base:
            print(f"  池#{pid} 缺 T0 基线，跳过")
            continue
        cands = []
        for (qid, v), r in by.items():
            if qid != pid or v == "T0":
                continue
            if base["avg"] <= 0:
                continue
            cut = (base["avg"] - r["avg"]) / base["avg"] * 100
            deltas = []
            for h in HOLDS:
                rb = (base.get(f"h{h}") or {}).get("ret") or 0.0
                rv = (r.get(f"h{h}") or {}).get("ret") or 0.0
                deltas.append(float(rv) - float(rb))
            if cut < args.min_cut or min(deltas) < -args.max_loss \
                    or r["avg"] < args.min_avg:
                continue
            cands.append((cut, sum(deltas) / len(deltas), r, deltas))
        if not cands:
            print(f"  池#{pid} {base['pool'][:20]:<20}  无满足条件的收紧（保持原样）")
            continue
        cands.sort(key=lambda x: -x[0] if args.pick == "cut" else -x[1])
        cut, _avgd, r, deltas = cands[0]
        extra = r.get("extra") or VARIANT_EXTRA.get(r["var"], [])
        plan.append((pid, base, r, cut, deltas, extra))
        print(f"  #{pid:<4}{base['pool'][:20]:<22}{base['avg']:>8.1f}{'→':>3}"
              f"{r['avg']:>7.1f}{cut:>6.0f}%{deltas[0]:>+8.1f}{deltas[1]:>+8.1f}"
              f"  {' + '.join(extra)}")

    if not plan:
        print("\n没有要改的池。"); return
    print(f"\n共 {len(plan)} 个池待改；"
          f"{'【落库】' if args.apply else '（dry-run：加 --apply 才写库）'}")

    if not args.apply:
        return

    done = 0
    for pid, base, r, cut, deltas, extra in plan:
        cur = pool_mod.get_pool(pid)
        if not cur:
            print(f"  !! 池#{pid} 已不存在，跳过"); continue
        if cur.get("builtin") and not args.force_builtin:
            print(f"  !! 池#{pid} {cur['name']} 是内置样例，跳过（要改加 --force-builtin）")
            continue
        conds = list(cur.get("conds") or [])
        have = {strip_comment(c) for c in conds}
        add = [c for c in extra if strip_comment(c) not in have]
        if not add:
            print(f"  ·  池#{pid} {cur['name']}：条件已存在，跳过（幂等）"); continue
        tag = (f"收紧 {r['var']}：日均 {base['avg']}→{r['avg']}（-%.0f%%），"
               % cut + " / ".join(
                   f"持{h} {(base.get('h'+str(h)) or {}).get('ret')}%→"
                   f"{(r.get('h'+str(h)) or {}).get('ret')}%"
                   for h in HOLDS))
        conds += [f"{c}   # {tag}" for c in add]
        note = (cur.get("note") or "").rstrip()
        note += f"\n\n【{tag[:4]}·2026-09-29】{tag}"
        pool_mod.save_pool(cur["name"], note.strip(), cur.get("base") or "all",
                           conds, bool(cur.get("pool_exit")), pid=pid)
        print(f"  ✅ 池#{pid} {cur['name']}：+{len(add)} 条 → "
              f"{' ；'.join(add)}")
        done += 1
    print(f"\n落库 {done} 个池。")


if __name__ == "__main__":
    main()
