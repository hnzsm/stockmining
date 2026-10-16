# -*- coding: utf-8 -*-
"""把若干张配置表导出成可回灌的 SQL（本机没有 mysqldump，就自己拼 INSERT）。

跑法：
    python tools/backup_tables.py strategy pool_strategy -o data/backup_pre_buysell

产出 `<前缀>_<表名>.sql`，内容形如：
    DELETE FROM `strategy` WHERE 1=1;
    INSERT INTO `strategy` (`id`,`name`,...) VALUES (1,'...',...);

回灌：
    在「数据管理」的 SQL 框里贴进对应文件，或用 core.db 执行。

为什么不用 mysqldump：这台机器 PATH 里没有 mysql 客户端（实测 `shutil.which` 为 None），
而这两张表加起来只有几十行 —— 自己拼更省事，也不引入外部依赖。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db  # noqa: E402


def _lit(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (dict, list)):
        v = json.dumps(v, ensure_ascii=False)
    if isinstance(v, (bytes, bytearray)):
        v = bytes(v).decode("utf-8", "replace")
    s = str(v).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{s}'"


def dump(table: str, out_path: str) -> tuple[int, int]:
    cols = [str(list(c.values())[0]) for c in db.query(f"SHOW COLUMNS FROM `{table}`")]
    rows = db.query(f"SELECT * FROM `{table}`")
    lines = [
        f"-- {table}: {len(rows)} 行 / {len(cols)} 列",
        f"DELETE FROM `{table}` WHERE 1=1;",
    ]
    if rows:
        col_sql = ",".join(f"`{c}`" for c in cols)
        vals = ",\n".join(
            "(" + ",".join(_lit(r.get(c)) for c in cols) + ")" for r in rows)
        lines.append(f"INSERT INTO `{table}` ({col_sql}) VALUES\n{vals};")
    lines.append("")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return len(rows), os.path.getsize(out_path)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if len(args) < 2:
        print(__doc__)
        return 2
    prefix = args[-1]
    tables = args[:-1]
    os.makedirs(os.path.dirname(os.path.abspath(prefix)) or ".", exist_ok=True)
    rc = 0
    for t in tables:
        path = f"{prefix}_{t}.sql"
        try:
            n, size = dump(t, path)
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL {t}: {type(e).__name__}: {e}")
            rc = 1
            continue
        print(f"  OK   {t}: {n} 行 → {path}（{size} 字节）")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
