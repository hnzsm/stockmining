# -*- coding: utf-8 -*-
"""删掉「收益为负」的历史回测记录（backtest_run + 明细 trade/equity）。

判据：metrics.total_return < 0。没有 metrics 的（pending/running/error）一律保留。

用法:
    python tools/_lab_clear_neg.py            # 只统计、列出，不动库
    python tools/_lab_clear_neg.py --apply    # 真删
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import db  # noqa: E402

OUT_DIR = os.path.join(ROOT, "data")


def _jsonable(v):
    if isinstance(v, (dt.date, dt.datetime)):
        return str(v)
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", "ignore")
    if hasattr(v, "quantize"):          # Decimal
        return float(v)
    return v


def backup(ids: list[int]) -> str:
    """删之前把要删的三张表整行导出成 JSON（能整批插回去的快照）。"""
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(
        OUT_DIR, f"backup_neg_runs_{dt.datetime.now():%Y%m%d_%H%M%S}.json")
    dump: dict = {"ids": ids, "tables": {}}
    for tbl, key in (("backtest_run", "id"), ("backtest_trade", "run_id"),
                     ("backtest_equity", "run_id")):
        rows: list = []
        for i in range(0, len(ids), 200):
            chunk = ids[i:i + 200]
            ph = ",".join(["%s"] * len(chunk))
            rows.extend(db.query(f"SELECT * FROM {tbl} WHERE {key} IN ({ph})", chunk))
        dump["tables"][tbl] = [
            {k: _jsonable(v) for k, v in r.items()} for r in rows]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dump, f, ensure_ascii=False, default=_jsonable)
    return path


def metrics_of(raw):
    if raw is None:
        return {}
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "ignore")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:  # noqa: BLE001
            return {}
    return raw if isinstance(raw, dict) else {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真删；不给就只统计")
    args = ap.parse_args()

    rows = db.query(
        "SELECT id,name,strategy_id,universe,start_date,end_date,status,metrics "
        "FROM backtest_run ORDER BY id")
    neg, keep, nome = [], [], []
    for r in rows:
        m = metrics_of(r.get("metrics"))
        tr = m.get("total_return")
        if tr is None:
            nome.append(r)
        elif float(tr) < 0:
            neg.append((r, float(tr)))
        else:
            keep.append((r, float(tr)))

    print(f"总记录 {len(rows)}  负收益 {len(neg)}  非负 {len(keep)}  无收益(保留) {len(nome)}")
    print()
    print("—— 将删除（收益<0）——")
    for r, tr in neg:
        print(f"  #{r['id']:<4} {tr:>9.2f}%  {r['status']:<7} {str(r['start_date'])[:10]}~{str(r['end_date'])[:10]}"
              f"  {str(r['name'])[:44]}")
    print()
    print("—— 保留（收益>=0）——")
    for r, tr in keep:
        print(f"  #{r['id']:<4} {tr:>9.2f}%  {r['status']:<7} {str(r['start_date'])[:10]}~{str(r['end_date'])[:10]}"
              f"  {str(r['name'])[:44]}")
    if nome:
        print()
        print("—— 无 metrics（pending/running/error），一律保留 ——")
        for r in nome:
            print(f"  #{r['id']:<4} {r['status']:<7} {str(r['name'])[:50]}")

    if not args.apply:
        print("\n[干跑] 加 --apply 才真删")
        return 0
    if not neg:
        print("\n[跳过] 没有负收益记录")
        return 0

    ids = [int(r["id"]) for r, _ in neg]
    bp = backup(ids)
    print(f"\n[备份] {bp}")
    ph = ",".join(["%s"] * len(ids))
    n1 = db.execute(f"DELETE FROM backtest_trade WHERE run_id IN ({ph})", ids)
    n2 = db.execute(f"DELETE FROM backtest_equity WHERE run_id IN ({ph})", ids)
    n3 = db.execute(f"DELETE FROM backtest_run WHERE id IN ({ph})", ids)
    print(f"\n[已删] run={len(ids)} 条（trade 影响 {n1} / equity {n2} / run {n3}）")
    left = db.query("SELECT COUNT(*) AS c FROM backtest_run")
    print(f"[剩余] backtest_run={left[0]['c'] if left else '?'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
