# -*- coding: utf-8 -*-
"""把桌面 Table_2285.xlsx 的股票按**题材分类**导入「我的关注」。

- 分组口径：按**题材概念**（不用 `所属行业` 那列），见 Temp 里的 wt_map.txt。
- 备注口径：写那条 `文本标记` 原文。
- 走 `core.watch` 的同一套函数（与页面 /api/watch/* 同路径），不另造一套。

用法：PYTHONPATH=. .venv/Scripts/python.exe tools/_import_watch.py
"""
from __future__ import annotations

import csv
import io
import json
import os

from core import watch

TMP = os.environ.get("TEMP", r"C:\Users\ming\AppData\Local\Temp")
RAW = os.path.join(TMP, "tbl_raw.json")
# 分类映射（code<TAB>题材分组）随脚本落在 tools/ 下，保证可复现；TEMP 里那份是当时的中间产物。
MAP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_import_watch_map.txt")
if not os.path.exists(MAP):
    MAP = os.path.join(TMP, "wt_map.txt")

# 分组建立顺序（DB 里 sort 按建立先后自增，即页面显示顺序）。
GROUP_ORDER = [
    "半导体·芯片", "算力·数据中心·通信", "消费·零售·传媒", "化工·新材料",
    "储能·电池", "稀土·磁材·小金属", "电网·电力·风电", "PCB·电子元件",
    "医药·医疗", "商业航天·卫星", "光伏", "机器人·具身智能", "航运·物流·海洋",
    "液冷·散热", "机械·工业母机·自动化", "地产·基建·建筑", "能源·煤炭·油气",
    "军工·航空航天", "光模块·光通信", "汽车·低空·智驾", "农业·食品·环保",
    "核电·可控核聚变", "金融·券商", "长鑫·长江存储概念", "显示·面板",
    "量子科技", "消费电子", "绿氢·绿氨·绿醇", "AI应用·软件·信创",
    "并购重组·壳资源",
]


def main() -> None:
    rows = list(csv.reader(io.StringIO(
        json.load(open(RAW, encoding="utf-8"))["csv_data"])))[1:]
    code2mark = {r[1].strip(): r[5].strip() for r in rows}
    code2name = {r[1].strip(): r[2].strip() for r in rows}

    grp: dict[str, list[str]] = {}
    for ln in open(MAP, encoding="utf-8"):
        if not ln.strip():
            continue
        c, g = ln.rstrip("\n").split("\t")
        grp.setdefault(g, []).append(c)

    # 表里的顺序（保持每只票在原始清单里的相对次序）
    order = {r[1].strip(): i for i, r in enumerate(rows)}

    unknown_total, summary = [], []
    for g in GROUP_ORDER:
        codes = sorted(grp.get(g, []), key=lambda c: order.get(c, 1 << 30))
        if not codes:
            continue
        gid = watch.ensure_group(g)
        res = watch.add_stocks(gid, codes)
        # ⚠ 两处坑：① set_note 要**完整 ts_code**（`600156.SH`）；② add_stocks 返回的
        #   `added/dup` 已经是**规范化后的** ts_code（`002153.SZ`）⇒ 反查备注要用前 6 位，
        #   否则 code2mark 取到空、备注静默被写成空串。
        for c in res["added"] + res["dup"]:
            watch.set_note(gid, c, code2mark.get(c[:6], ""))
        unknown_total += res["unknown"]
        summary.append((g, gid, len(res["added"]), len(res["dup"]), res["unknown"]))

    print(f"{'分组':<22}{'gid':>4}{'新增':>6}{'已存在':>7}")
    for g, gid, a, d, u in summary:
        print(f"{g:<22}{gid:>4}{a:>6}{d:>7}" + (f"   未知:{u}" if u else ""))
    print("\n合计分组:", len(summary),
          " 新增自选:", sum(s[2] for s in summary),
          " 未知代码:", len(unknown_total), unknown_total[:30])
    print("库内自选股总数:", watch.db.scalar("SELECT COUNT(*) FROM watch_stock"))
    print("库内分组数:", watch.db.scalar("SELECT COUNT(*) FROM watch_group"))


if __name__ == "__main__":
    main()
