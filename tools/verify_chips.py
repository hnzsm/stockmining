# -*- coding: utf-8 -*-
"""筹码与两融数据管道回归（股东户数 / 融资融券 / 股东增减持）。

    python tools/verify_chips.py

**不需要 tushare 额度**：同步正确性用 monkeypatch 掉 `sync.call`、喂构造好的
DataFrame 来验（落到一张假代码 000000.SZ 上，跑完自己清掉），所以离线可跑、可重复。
三层：

  【1】表结构 —— 三张表的主键 / 索引是否与"按公告日整体替换""按报告期归并"的设计配套；
  【2】口径单元 —— 单位换算（元 vs 万元差 1e4）、带符号文本、归并取最新公告日、
                 增长率对比相邻报告期（上一期缺数就给「—」而不是跳过拿更早的顶）；
  【3】同步幂等与脏行防御 —— 按公告日先删后插、完全重复行去重、tushare 尾部提示行剔除、
                           单日写入失败只跳过那天（不让一条脏行带走整段同步）；
  【4】真实数据不变量 —— 增减持无完全重复行、两融余额 ≈ 融资余额 + 融券余额、
                       两融日期都落在开市日、覆盖区间报告（覆盖不全只提示，不判失败：
                       数据是后台在补的，不是代码错）；
  【5】覆盖情况报告；
  【6】模块接口；
  【7】选股字段 —— 18 个字段的清单/校验是否齐全、only= 裁剪（不引用就不读库）、
                  **asof 对齐无未来函数**（户数按公告日、两融按交易日、增减持按公告日
                  窗口，全部拿 SQL 独立复算逐日对拍）、缺数据恒不成立、周线取"当周
                  最后一个交易日"的值、端到端选股真能跑。

    python tools/verify_chips.py --fast      # 跳过真实选股扫描
"""
from __future__ import annotations

import os
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import chips as ch          # noqa: E402
from core import db, sync             # noqa: E402
from core import indicators as ind    # noqa: E402
from core import market, screener, strategy as st   # noqa: E402
from core import universe as uni      # noqa: E402

FAKE = "000000.SZ"                     # 假代码：只在本脚本里出现，跑完清理
FAST = "--fast" in sys.argv            # 跳过真实选股扫描（全市场那几段慢）
PASS = FAIL = SKIP = 0
FAILS: list[str] = []

# ⚠ 本脚本会真调 sync.sync_holder_trade / sync_margin_detail 等（喂合成数据），
# 它们开口就插一条 sync_log。测试用的都是 1990 年的假区间，写进真表会把
# 「数据管理 → 同步历史」灌满 1990-01-05 / 行数 0 / running 的垃圾行
# （历史里 128 条就是这么来的）。所以离线回归一律关掉同步日志：
# 靠 `sync.SYNC_LOG_ENABLED` 这一个开关，而不是让每个测试自己去删表。
sync.SYNC_LOG_ENABLED = False
_SYNC_LOG_MAX = db.scalar("SELECT IFNULL(MAX(id),0) FROM sync_log") or 0


def _isnum(v) -> bool:
    """能当数字用吗（None / NaN / 空串都算缺）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return f == f


def _eq(a, b, tol: float = 1e-9) -> bool:
    """两个数（或缺值）相等。缺值对缺值算相等；缺值对数字算不等。"""
    an, bn = _isnum(a), _isnum(b)
    if not an or not bn:
        return an == bn
    return abs(float(a) - float(b)) <= max(tol, tol * abs(float(b)))


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  OK   {name}" + (f"  {extra}" if extra else ""))
    else:
        FAIL += 1
        FAILS.append(name)
        print(f"  FAIL {name}" + (f"  {extra}" if extra else ""))


def table_exists(name: str) -> bool:
    return bool(db.scalar(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema=%s AND table_name=%s", (db.MYSQL["database"], name)))


def pk_cols(name: str) -> list[str]:
    # information_schema 的列名在 MySQL 8 里是大写（COLUMN_NAME），统一起别名免得踩坑
    rows = db.query(
        "SELECT column_name AS nm FROM information_schema.key_column_usage "
        "WHERE table_schema=%s AND table_name=%s AND constraint_name='PRIMARY' "
        "ORDER BY ordinal_position", (db.MYSQL["database"], name))
    return [r["nm"] for r in rows]


def has_index(name: str, index: str) -> bool:
    return bool(db.scalar(
        "SELECT COUNT(*) FROM information_schema.statistics "
        "WHERE table_schema=%s AND table_name=%s AND index_name=%s",
        (db.MYSQL["database"], name, index)))


# ---------------------------------------------------------------------------

def sec1_schema() -> None:
    print("\n【1】表结构（为「按公告日整体替换」和「按报告期归并」配套）")
    for t in ("holder_number", "margin_detail", "holder_trade"):
        check(f"表 {t} 存在", table_exists(t))

    # 股东户数：同一报告期可能在多个公告日披露 —— 主键必须带 ann_date，否则会把快报/正式报告
    # 相互覆盖，丢掉"哪条是最终披露"的信息。
    check("holder_number 主键 = (ts_code, ann_date, end_date)（同一报告期的多次公告都留得住）",
          pk_cols("holder_number") == ["ts_code", "ann_date", "end_date"],
          ",".join(pk_cols("holder_number")) or "没有主键")
    check("holder_number 有 (ts_code, end_date) 索引（按票取序列不打全表）",
          has_index("holder_number", "idx_code_end"))
    check("holder_number 有 ann_date 索引（断点续传按公告日查已同步集合）",
          has_index("holder_number", "idx_ann"))

    check("margin_detail 主键 = (ts_code, trade_date)",
          pk_cols("margin_detail") == ["ts_code", "trade_date"],
          ",".join(pk_cols("margin_detail")) or "没有主键")
    check("margin_detail 有 trade_date 索引（按日核对覆盖）",
          has_index("margin_detail", "idx_trade_date"))

    # 增减持：tushare 没有唯一键、且实测有完全重复行 —— 所以去重靠"按公告日先删后插"，
    # 表上只需要"按票查"的索引，不造指纹列。
    check("holder_trade 有 (ts_code, ann_date) 索引（个股页按公告日倒序取最近几条）",
          has_index("holder_trade", "idx_code_ann"))
    check("holder_trade 有 ann_date 索引（按公告日先删后插要快）",
          has_index("holder_trade", "idx_ann"))

    # 列宽：tushare 会把同一公告里**多只私募基金的名字用逗号拼成一行**（实测 170 字符），
    # 原来 160 装不下 → 那三天整体写不进去（1406）。列宽必须留够余量。
    width = db.scalar(
        "SELECT character_maximum_length FROM information_schema.columns "
        "WHERE table_schema=%s AND table_name='holder_trade' AND column_name='holder_name'",
        (db.MYSQL["database"],))
    check("holder_trade.holder_name 列宽 ≥ 512（逗号拼接的长股东名实测到 170 字符）",
          width is not None and int(width) >= 512, f"varchar({width})")
    widths = db.WIDEN_MIGRATIONS if hasattr(db, "WIDEN_MIGRATIONS") else []
    check("老库有「只加宽不缩窄」的列宽迁移（升级后不靠手工 ALTER）",
          any(t == "holder_trade" and c == "holder_name" and w >= 512
              for t, c, w, _ in widths),
          str([(t, c, w) for t, c, w, _ in widths]))


def sec2_units() -> None:
    print("\n【2a】单位与文本口径（换算只做一次，前端不碰）")
    check("金额按【元】换算：1.71e10 → 171.24亿（不是拿 money_flow 的万元口径套）",
          ch._amt_text(1.7124e10) == "171.24亿", ch._amt_text(1.7124e10))
    check("金额万级：2265.85万（不是 0.23亿，四舍五入到亿看不出量级）",
          ch._amt_text(2.26585e7) == "2265.85万", ch._amt_text(2.26585e7))
    check("金额小到千级给原值", ch._amt_text(1234) == "1234元", ch._amt_text(1234))
    check("数量按【股/户】：296404 → 29.64万",
          ch._cnt_text(296404) == "29.64万", ch._cnt_text(296404))
    check("缺口径一律给「—」（不是 0）",
          ch._amt_text(None) == "—" and ch._cnt_text(float("nan")) == "—"
          and ch._signed_pct(None) == "—" and ch._price_text(None) == "—")
    check("带符号百分比：正号只在 >0 时出现（「+0.00%」会被读成小幅上涨）",
          ch._signed_pct(21.9) == "+21.90%" and ch._signed_pct(0) == "0.00%"
          and ch._signed_pct(-5.02) == "-5.02%",
          f"{ch._signed_pct(21.9)} / {ch._signed_pct(0)} / {ch._signed_pct(-5.02)}")
    check("涨跌色按字面正负（涨红跌绿），零与缺值不上色",
          ch._cls(1) == "up" and ch._cls(-1) == "down" and ch._cls(0) == "flat"
          and ch._cls(None) == "flat")


def sec2_merge() -> None:
    print("\n【2b】股东户数归并 + 增长率（相邻报告期，上一期缺数就给「—」）")
    # monkeypatch 掉 db.query，喂一份构造好的多公告日数据
    real_query = db.query
    rows = [
        # 2024-06-30 报告期：先出快报、后出正式报告 —— 归并时要取**正式**那份
        {"ann_date": "2024-08-10", "end_date": "2024-06-30", "holder_num": 200},
        {"ann_date": "2024-08-25", "end_date": "2024-06-30", "holder_num": 220},
        {"ann_date": "2024-10-26", "end_date": "2024-09-30", "holder_num": 242},
        # 中间这一期缺数：后面那期的增长率必须给 None（它的"上一期"就是那个缺数的报告期），
        # 再下一期才恢复成正常对比 —— 两件事都要锁住。
        {"ann_date": "2025-01-20", "end_date": "2024-12-31", "holder_num": None},
        {"ann_date": "2025-04-20", "end_date": "2025-03-31", "holder_num": 250},
        {"ann_date": "2025-07-20", "end_date": "2025-06-30", "holder_num": 260},
    ]
    try:
        db.query = lambda *a, **k: list(rows)          # type: ignore[assignment]
        seq = ch.holder_history("TEST.SZ", "2026-09-17", periods=0)
    finally:
        db.query = real_query                          # type: ignore[assignment]
    check("同一报告期的多次公告合并成 1 期（5 个报告期，不是 6 条）",
          len(seq) == 5, f"{len(seq)} 期：" + ",".join(x["end_date"] for x in seq))
    check("合并时取**公告日最新**的那条（快报 200 被正式报告 220 顶掉）",
          seq[0]["holder_num"] == 220 and seq[0]["ann_date"] == "2024-08-25",
          f"{seq[0]['ann_date']} → {seq[0]['holder_num']}")
    check("增长率 = 与相邻上一报告期相比（220→242 = +10%）",
          abs(seq[1]["growth"] - 10.0) < 1e-9, f"{seq[1]['growth']}")
    check("本期缺数时增长率给 None",
          seq[2]["growth"] is None, str(seq[2]["growth"]))
    check("上一期缺数时本期也给 None（不跳过它拿更早的顶）",
          seq[3]["growth"] is None, str(seq[3]["growth"]))
    check("缺数期过去之后，再下一期照常算（250→260 = +4%）",
          seq[4]["growth"] is not None and abs(seq[4]["growth"] - 4.0) < 1e-9,
          str(seq[4]["growth"]))
    check("序列按报告期正序（图从左到右是时间，不是倒过来的）",
          [x["end_date"] for x in seq] == sorted(x["end_date"] for x in seq))


def sec2c_trade_marks() -> None:
    """K 线图顶边 ± 标记的对齐口径（`chips.trade_marks`）。

    口径只有一条：**公告日 → 当天或之后的第一根 K 线**（公告多在盘后发布，市场最早
    下一交易日才知道；公告日恰为交易日时就是当天）。用构造好的网格 + 构造好的公告
    离线跑，不碰真实行情 —— 真实数据的对拍在 verify_stock_ui 里另有一条。

    SQL 那层（`ann_date BETWEEN lo AND hi`）不 mock 掉，而是**照参数执行这层过滤**，
    这样"窗口外不参与""只画到查询日"两条也被覆盖到（它们是靠这段 SQL 收口的）。
    """
    print("\n【2c】K 线顶边 ± 标记：公告日 → 当天或之后的第一根 K 线")
    real_query = db.query
    # 网格故意留跨周末/长假的口子：01-09(五) → 01-12(一)，01-13 之后一直跳到 01-20
    grid = ["2026-01-05", "2026-01-06", "2026-01-09", "2026-01-12", "2026-01-13",
            "2026-01-20", "2026-01-21", "2026-01-22", "2026-01-23"]
    rows = [
        # 首根前 3 天（在"前 7 天"的宽容窗里）→ 对齐到第 0 根
        {"ann_date": "2026-01-02", "in_de": "IN", "change_vol": 1000.0, "change_ratio": 0.01,
         "holder_name": "首根前", "holder_type": "P"},
        # 首根前 8 天（超出宽容窗）→ 整条丢弃，不许贴到第 0 根上
        {"ann_date": "2025-12-28", "in_de": "IN", "change_vol": 9999.0, "change_ratio": 0.09,
         "holder_name": "太老", "holder_type": "P"},
        # 公告当天就是交易日 → 落在当天
        {"ann_date": "2026-01-06", "in_de": "IN", "change_vol": 10000.0, "change_ratio": 0.02,
         "holder_name": "甲", "holder_type": "P"},
        {"ann_date": "2026-01-09", "in_de": "DE", "change_vol": 20000.0, "change_ratio": 0.03,
         "holder_name": "乙", "holder_type": "C"},
        # 周六公告 → 下一根 01-12
        {"ann_date": "2026-01-10", "in_de": "IN", "change_vol": 30000.0, "change_ratio": 0.04,
         "holder_name": "丙", "holder_type": "G"},
        # 同一天同方向两条 → 归成 1 个标记（count=2，数量合计 3000）
        {"ann_date": "2026-01-12", "in_de": "DE", "change_vol": 1000.0, "change_ratio": 0.005,
         "holder_name": "丁", "holder_type": "P"},
        {"ann_date": "2026-01-12", "in_de": "DE", "change_vol": 2000.0, "change_ratio": 0.008,
         "holder_name": "戊", "holder_type": "P"},
        # 方向既不是 IN 也不是 DE → 不画（图上只有 + 和 −，不能冒出第三种符号）
        {"ann_date": "2026-01-13", "in_de": "", "change_vol": 5000.0, "change_ratio": 0.0,
         "holder_name": "己", "holder_type": "P"},
        # 查询日之后的公告 → 不许出现（无未来函数）
        {"ann_date": "2026-01-21", "in_de": "IN", "change_vol": 8000.0, "change_ratio": 0.07,
         "holder_name": "庚", "holder_type": "C"},
    ]

    def fake_query(sql, args=None, **k):
        if "holder_trade" not in sql or "FROM daily_kline" in sql:
            return real_query(sql, args, **k) if args else real_query(sql, **k)
        _code, lo, hi = args
        return [r for r in rows if lo <= r["ann_date"] <= hi]   # 复现 SQL 的 BETWEEN

    try:
        db.query = fake_query                          # type: ignore[assignment]
        mk = ch.trade_marks("TEST.SZ", grid, "2026-01-13")
    finally:
        db.query = real_query                          # type: ignore[assignment]

    got = sorted((m["idx"], m["in_de"], m["count"]) for m in mk)
    # 排序按 (idx, in_de) —— 'DE' < 'IN'，所以同一根上「减持」排在「增持」前面
    want = [(0, "IN", 1),        # 01-02 那条（首根前 3 天，宽容窗内）→ 第 0 根
            (1, "IN", 1),        # 01-06 当天
            (2, "DE", 1),        # 01-09 当天
            (3, "DE", 2),        # 01-12 两条归并
            (3, "IN", 1)]        # 01-10 周六 → 01-12
    check("公告按「当天或之后的第一根 K 线」对齐，同日同向归并成 1 个标记",
          got == want, f"{got} vs {want}")
    check("每条标记的 date = 该行 K 线的日期（前端直接按 idx 摆点，不自己找日期）",
          all(grid[m["idx"]] == m["date"] for m in mk),
          str([(m["idx"], m["date"]) for m in mk]))
    check("窗口首根前 7 天以外的老公告不参与（不会被硬贴到第 0 根上）",
          all(m["ann_date"] != "2025-12-28" for m in mk)
          and not any(m["vol_text"].startswith("9999") or "亿" in m["vol_text"] for m in mk),
          "2025-12-28 那条超窗公告没出现")
    check("只画到查询日为止（end 之后的公告一个都不出现）",
          all(m["ann_date"] <= "2026-01-13" for m in mk)
          and all(m["date"] <= "2026-01-13" for m in mk)
          and not any(m["ann_date"] == "2026-01-21" for m in mk),
          f"标记 {[(m['idx'], m['ann_date']) for m in mk]}")
    check("方向不明的公告不画（图上只有 + / −，不冒第三种符号）",
          all(m["in_de"] in ("IN", "DE") and m["dir_text"] in ("增持", "减持") for m in mk))
    check("标记带方向/条数/数量与比例文本（前端不自己判正负、不自己拼单位）",
          all(m["count"] >= 1 and m["vol_text"] and m["ratio_text"] for m in mk)
          and [m["vol_text"] for m in mk if m["idx"] == 3 and m["in_de"] == "DE"] == ["3000"],
          str([(m["dir_text"], m["count"], m["vol_text"], m["ratio_text"]) for m in mk]))
    check("没有 K 线网格 / 没有代码时安静给空表（不抛）",
          ch.trade_marks("TEST.SZ", [], "2026-01-13") == []
          and ch.trade_marks("", grid, "2026-01-13") == [])


def sec3_sync_idempotent() -> None:
    print("\n【3】同步幂等（按公告日先删后插 / 按主键 upsert；重复跑不累积脏数据）")
    if not table_exists("holder_trade"):
        skip("同步幂等", "表还没建")
        return
    db.execute("DELETE FROM holder_trade WHERE ts_code=%s", (FAKE,))
    db.execute("DELETE FROM holder_number WHERE ts_code=%s", (FAKE,))
    real_call = sync.call
    dup = {
        "ts_code": [FAKE] * 3,
        "ann_date": ["19900105"] * 3,
        "holder_name": ["某股东"] * 3,
        "holder_type": ["C"] * 3,
        "in_de": ["DE"] * 3,
        "change_vol": [1000.0] * 3,       # 三行**完全一样** —— 必须被去重成 1 行
        "change_ratio": [0.5] * 3,
        "after_share": [9000.0] * 3,
        "after_ratio": [10.0] * 3,
        "avg_price": [None] * 3,
        "total_share": [9000.0] * 3,
    }
    hn = {
        "ts_code": [FAKE] * 2,
        "ann_date": ["19900103", "19900105"],     # 同一报告期，两天的公告
        "end_date": ["19900101", "19900101"],
        "holder_num": [1000.0, 1200.0],           # 后一条是修订值
    }

    def fake_call(api, **kw):
        if api == "stk_holdertrade":
            return pd.DataFrame(dup)
        if api == "stk_holdernumber":
            return pd.DataFrame(hn)
        return pd.DataFrame()

    try:
        sync.call = fake_call                       # type: ignore[assignment]
        r1 = sync.sync_holder_trade("1990-01-05", "1990-01-05")
        # 第二次用 force=True：否则会被"已同步公告日"跳过，验不到真正的先删后插路径
        r2 = sync.sync_holder_trade("1990-01-05", "1990-01-05", force=True)
        n = db.scalar("SELECT COUNT(*) FROM holder_trade WHERE ts_code=%s", (FAKE,))
        check("同一公告日重复同步行数不翻倍（先删后插 → 幂等）",
              n == 1 and r1["rows"] == 1 and r2["rows"] == 1,
              f"两次后共 {n} 行；第二次返回 {r2['rows']} 行（force 重跑）")
        check("tushare 返回里的完全重复行被去掉（3 条同样的 → 1 条）",
              n == 1, f"{n} 条")

        sync.sync_holder_number("1990-01-03", "1990-01-05")
        m = db.scalar("SELECT COUNT(*) FROM holder_number WHERE ts_code=%s", (FAKE,))
        check("同一报告期在两个公告日披露 → 两行都留（主键带 ann_date，不互相覆盖）",
              m == 2, f"{m} 行")
        seq = ch.holder_history(FAKE, "1990-02-01", periods=0)
        check("读取时归并成 1 期、取最新公告的修订值（1000 → 1200）",
              len(seq) == 1 and seq[0]["holder_num"] == 1200,
              f"{len(seq)} 期 / {seq[0]['holder_num'] if seq else '—'}")
        # 断点续传凭据：已同步的公告日必须能被查出来（否则每次重跑都全量扫）
        check("已同步公告日集合能查出来（断点续传不会重复拉）",
              sync.synced_ann_dates("holder_number", "1990-01-01", "1990-12-31")
              == {"1990-01-03", "1990-01-05"},
              str(sorted(sync.synced_ann_dates("holder_number", "1990-01-01", "1990-12-31"))))
    finally:
        sync.call = real_call                       # type: ignore[assignment]
        db.execute("DELETE FROM holder_trade WHERE ts_code=%s", (FAKE,))
        db.execute("DELETE FROM holder_number WHERE ts_code=%s", (FAKE,))
    left = db.scalar("SELECT COUNT(*) FROM holder_trade WHERE ts_code=%s", (FAKE,)) + \
        db.scalar("SELECT COUNT(*) FROM holder_number WHERE ts_code=%s", (FAKE,))
    check("测试数据已清理干净（假代码不留在库里）", left == 0, f"残留 {left} 行")
    # 上面那几次 sync 调用会开口就插 sync_log —— 假区间不能留在真实同步历史里。
    now_max = db.scalar("SELECT IFNULL(MAX(id),0) FROM sync_log") or 0
    check("离线回归不往「同步历史」里塞假记录（sync_log 一条没长）",
          now_max == _SYNC_LOG_MAX,
          f"跑测试前 max(id)={_SYNC_LOG_MAX} → 现在 {now_max}")


def sec3b_dirty_rows() -> None:
    """脏行防御 —— tushare 尾部"提示行"与"单日失败拖垮整段同步"。

    真实事故：`margin_detail(trade_date='20260105')` 返回的最后一行是
    `ts_code='日期：2026-01-05.BJ'`（16 字符、除 trade_date 外全 NaN，
    4284 行 vs 平常 4283 行）。它插不进 VARCHAR(12) 的 ts_code，抛 1406，
    于是**整个区间同步在第一天就中断**（2026-01-05 起 167 个交易日全空）。
    """
    print("\n【3b】脏行防御（tushare 提示行 / 单日失败不拖垮整段）")
    sentinel = "日期：2026-01-05.BJ"

    # (1) 判据单元：只有 ^\d{6}\.(SH|SZ|BJ)$ 才算代码
    df = pd.DataFrame({"trade_date": ["19900108"] * 3,
                       "ts_code": ["600519.SH", "000001.SZ", sentinel],
                       "rzye": [1.0, 2.0, None]})
    out = sync._drop_bad_codes(df, "单测")
    check("非标准 ts_code 被剔除（'日期：…' 提示行 / 长度 16 的行不落库）",
          len(out) == 2 and sentinel not in set(out["ts_code"]),
          f"{len(out)} 行 / {list(out['ts_code'])}")
    check("ts_code 是 NaN 的行也剔除（astype(str) 后是 'nan'，不匹配正则）",
          len(sync._drop_bad_codes(pd.DataFrame({"ts_code": [float("nan")]}))) == 0)
    check("干净的返回原样通过（不误杀 430047.BJ 这类北交所代码）",
          len(sync._drop_bad_codes(
              pd.DataFrame({"ts_code": ["430047.BJ", "688981.SH"]}))) == 2)
    check("None / 空表 / 没有 ts_code 列都不炸（防御性，不改变原值）",
          sync._drop_bad_codes(None) is None
          and len(sync._drop_bad_codes(pd.DataFrame())) == 0
          and len(sync._drop_bad_codes(pd.DataFrame({"a": [1]}))) == 1)
    check("正则口径 = 6 位数字 + .SH/.SZ/.BJ（恒 9 字符，稳落在 VARCHAR(12) 内）",
          sync.TS_CODE_RE.match("600519.SH") is not None
          and sync.TS_CODE_RE.match("430047.BJ") is not None
          and sync.TS_CODE_RE.match(sentinel) is None)

    # (2) 端到端：假 margin_detail 里混进提示行 → 只写 2 行、且整体不抛
    if not table_exists("margin_detail"):
        skip("融资融券脏行端到端", "表还没建")
        return
    days = ["1990-01-08", "1990-01-09"]
    db.execute("DELETE FROM margin_detail WHERE trade_date BETWEEN %s AND %s", (days[0], days[-1]))
    real_call, real_days = sync.call, sync._trade_days
    t0_cols = ["rzye", "rzmre", "rzche", "rqye", "rqyl", "rqmcl", "rqchl", "rzrqye"]

    def fake_call(api, **kw):
        assert api == "margin_detail", api
        # 注意别写 [d] * 2 —— 那是同一个 dict 对象，改一个两个都变，两行会撞主键
        good = []
        for code, amt in ((FAKE, 100.0), ("430047.BJ", 200.0)):
            rec = {"ts_code": code, "trade_date": days[0].replace("-", "")}
            rec.update({c: amt for c in t0_cols})
            good.append(rec)
        bad = {c: None for c in t0_cols}
        bad.update({"ts_code": sentinel, "trade_date": days[0].replace("-", "")})
        return pd.DataFrame(good + [bad])        # 第 3 行就是 tushare 的提示行

    try:
        sync.call = fake_call                                  # type: ignore[assignment]
        sync._trade_days = lambda s, e: days                   # type: ignore[assignment]
        res = sync.sync_margin_detail(days[0], days[-1], force=True)
        n = db.scalar("SELECT COUNT(*) FROM margin_detail WHERE trade_date=%s", (days[0],))
        # 假返回两天都是「2 行行情 + 1 行提示」，故共写 4 行；关键是第一天那 2 行在，
        # 且没有像事故里那样"整个区间一天都没写进去"。
        check("混入提示行的那一天仍然写成功（不再整段中断）",
              n == 2 and res["rows"] == 4,
              f"首日 {n} 行 / 返回 {res['rows']} 行（两天各 2 行行情，提示行已剔）")
        check("提示行没被当成行情落库（'日期：…' 查不到）",
              db.scalar("SELECT COUNT(*) FROM margin_detail WHERE ts_code LIKE %s",
                        ("日期%",)) == 0)
        check("失败日记为空（过滤后没有需要跳过的日）",
              res.get("failed_days") == [], str(res.get("failed_days")))
    finally:
        sync.call = real_call                                  # type: ignore[assignment]
        sync._trade_days = real_days                           # type: ignore[assignment]
    db.execute("DELETE FROM margin_detail WHERE trade_date BETWEEN %s AND %s", (days[0], days[-1]))

    # (3) 写库真抛异常时：只记下那天、跳过、继续，不往外抛
    real_insert = db.insert_many
    bad_days: list = []
    try:
        db.insert_many = lambda sql, rows: (_ for _ in ()).throw(  # type: ignore[assignment]
            RuntimeError("模拟 1406"))
        got = sync._insert_or_log("INSERT INTO x VALUES (%s)", [(1,)], "1990-01-10",
                                  "融资融券", None, bad_days)
        check("单日写入抛异常时只记录该日并返回 0（不中断整个区间）",
              got == 0 and bad_days == ["1990-01-10"], f"bad={bad_days}")
    finally:
        db.insert_many = real_insert                           # type: ignore[assignment]
    check("空 rows 直接返回 0，不触发任何写库", sync._insert_or_log(
        "INSERT INTO x VALUES (%s)", [], "1990-01-10", "融资融券", None, []) == 0)

    # (4) 超长股东名：tushare 会把同一公告里多只私募基金的名字**用逗号拼成一行**
    #     （2026-03-17 实测 170 字符），列宽不够会让那一整天整体写不进去。
    if not table_exists("holder_trade"):
        skip("超长股东名", "表还没建")
        return
    long_name = ",".join(f"博普资产尊享{i}号私募证券投资基金" for i in range(1, 11))
    assert len(long_name) > 160, len(long_name)          # 必须真的超过旧列宽才有意义
    rec = {"ts_code": FAKE, "ann_date": "19900112", "holder_name": long_name,
           "holder_type": "C", "in_de": "IN", "change_vol": 1.0, "change_ratio": 1.0,
           "after_share": 1.0, "after_ratio": 1.0, "avg_price": None, "total_share": 1.0}
    real_call2 = sync.call
    try:
        sync.call = lambda api, **kw: pd.DataFrame([rec])          # type: ignore[assignment]
        r = sync.sync_holder_trade("1990-01-12", "1990-01-12")
        got = db.scalar("SELECT CHAR_LENGTH(holder_name) FROM holder_trade "
                        "WHERE ann_date='19900112'")
        check(f"超长股东名（{len(long_name)} 字符）整行写得进去，而不是当天整体失败",
              got == len(long_name) and r["failed_days"] == [],
              f"落库 {got} 字符 / failed={r['failed_days']}")
    finally:
        sync.call = real_call2                                    # type: ignore[assignment]
        db.execute("DELETE FROM holder_trade WHERE ann_date='19900112'")


def sec4_invariants() -> None:
    print("\n【4】真实数据不变量")
    if db.scalar("SELECT COUNT(*) FROM holder_trade") == 0:
        skip("holder_trade 不变量", "还没同步到数据")
    else:
        dup = db.scalar(
            "SELECT COUNT(*) FROM (SELECT ts_code,ann_date,holder_name,holder_type,in_de,"
            "change_vol,change_ratio,after_share,after_ratio,avg_price,total_share,"
            "COUNT(*) c FROM holder_trade GROUP BY ts_code,ann_date,holder_name,holder_type,"
            "in_de,change_vol,change_ratio,after_share,after_ratio,avg_price,total_share "
            "HAVING c>1) t")
        check("holder_trade 没有完全重复的公告行（tushare 原始数据里是有的）",
              dup == 0, f"{dup} 组重复")
        bad = db.scalar("SELECT COUNT(*) FROM holder_trade WHERE in_de NOT IN ('IN','DE') "
                        "OR in_de IS NULL")
        check("holder_trade.in_de 只有 IN / DE", bad == 0, f"{bad} 行异常")

    if db.scalar("SELECT COUNT(*) FROM margin_detail") == 0:
        skip("margin_detail 不变量", "还没同步到数据")
    else:
        diff = db.query_one(
            "SELECT COUNT(*) c, MAX(ABS(rzrqye-(rzye+rqye))) m FROM margin_detail "
            "WHERE rzrqye IS NOT NULL AND rzye IS NOT NULL AND rqye IS NOT NULL")
        # 两融余额 = 融资余额 + 融券余额，允许分位取整带来的 1 元内误差
        check("两融余额 = 融资余额 + 融券余额（口径自洽）",
              (diff["m"] or 0) <= 1.0, f"最大偏差 {diff['m']} 元 / {diff['c']} 行")
        off = db.scalar(
            "SELECT COUNT(*) FROM margin_detail m LEFT JOIN trade_cal c "
            "ON c.cal_date=m.trade_date AND c.exchange='SSE' "
            "WHERE c.is_open IS NULL OR c.is_open=0")
        check("两融日期全部落在开市日（没有把周末/休市日当交易日拉进来）",
              off == 0, f"{off} 行不在开市日")

    print("\n【5】覆盖情况（后台在补数据，覆盖不全只提示、不判失败）")
    for t, col in (("holder_number", "ann_date"), ("margin_detail", "trade_date"),
                   ("holder_trade", "ann_date")):
        r = db.query_one(f"SELECT COUNT(*) c, MIN({col}) a, MAX({col}) b, "
                         f"COUNT(DISTINCT {col}) d FROM {t}")
        days = (str(r["a"])[:10], str(r["b"])[:10]) if r["a"] else ("—", "—")
        print(f"  {t:<14} {r['c']:>9} 行 · {r['d']:>5} 个日期 · {days[0]} ~ {days[1]}")


def sec5_module_surface() -> None:
    print("\n【6】模块接口")
    check("chips_snapshot 返回三块 + 视角日期",
          set(ch.chips_snapshot("600519.SH", "2026-09-17").keys()) ==
          {"as_of", "holder", "margin", "trade"})
    check("三档阈值是模块级常量（卡片期数 / 两融天数 / 增减持条数只定义一次）",
          (ch.HOLDER_PERIODS, ch.MARGIN_DAYS, ch.TRADE_LIMIT) == (8, 180, 12),
          f"{ch.HOLDER_PERIODS}/{ch.MARGIN_DAYS}/{ch.TRADE_LIMIT}")
    check("空代码 / 空日期不炸（返回空态而不是抛异常）",
          ch.chips_snapshot("", "")["holder"]["available"] is False
          and ch.chips_snapshot("", "")["margin"]["available"] is False)


def skip(name: str, why: str) -> None:
    global SKIP
    SKIP += 1
    print(f"  SKIP {name}  {why}")


# ---------------------------------------------------------------------------
def sec7_screen_fields() -> None:
    """筹码/两融作为**选股指标**：字段清单、裁剪、asof 口径、缺数据降级、周线、端到端。

    这一段的重点是**未来函数**：三类数据里户数与增减持都是"公告日才知道"的，
    一次写错就是"回测收益很好看、实盘根本买不到"。所以每类都拿 SQL 独立复算
    逐日对拍，并且额外对"公告日之前的那一天"下一条硬断言。
    """
    print("\n【7】选股字段（把筹码/两融接进条件与评分）")

    # ---- 7a 字段清单与校验 ----
    daily = st.condition_field_help()
    weekly = st.condition_weekly_field_help()
    names = set(st.condition_field_names())
    missing = [n for n in ch.CHIPS_DERIVED if n not in daily]
    check("18 个筹码字段全部进了日线字段清单（前端字段参考/补全自动带上）",
          not missing, f"缺 {missing}")
    check("全部有 w_ 周线版本",
          all(f"w_{n}" in weekly for n in ch.CHIPS_DERIVED),
          [n for n in ch.CHIPS_DERIVED if f"w_{n}" not in weekly][:3])
    check("全部在「可用字段」集合里（条件校验不会误报未知字段）",
          all(n in names for n in ch.CHIPS_DERIVED))
    flat = set()
    for grp, _ in ind.FIELD_HELP:
        flat |= {x.strip() for x in grp.split(" / ")}
    check("indicators.FIELD_HELP 里已并入筹码字段（/api/universe/fields 与字段参考都取自它）",
          all(n in flat for n in ch.CHIPS_DERIVED),
          f"缺 {[n for n in ch.CHIPS_DERIVED if n not in flat]}")
    bad_cond = [(n, e) for n in ch.CHIPS_DERIVED
                for e in [uni.validate({"kind": "cond", "where": [f"{n} > 0"]}, names)] if e]
    check("每个字段都能通过股票池条件校验", not bad_cond, bad_cond[:2])
    described = set()
    for grp, _ in ch.CHIP_FIELD_HELP:
        described |= {x.strip() for x in grp.split(" / ")}
    check("字段表与说明表完全对应（防「加了字段忘了加说明」→ 前端提示空着）",
          described == set(ch.CHIPS_DERIVED),
          f"多 {sorted(described - set(ch.CHIPS_DERIVED))} "
          f"缺 {sorted(set(ch.CHIPS_DERIVED) - described)}")
    # 说明必须**逐字段**写：以前写成 "hold_num / hold_chg / …" 一条，
    # 字段参考里每个字段都显示同一串"别人的含义"（用户报的就是这个）。
    dup2 = {}
    for k, d in ch.CHIP_FIELD_HELP:
        dup2.setdefault(d, []).append(k)
    same = {d: ks for d, ks in dup2.items() if len(ks) > 1}
    check("说明逐字段各写一条（不再几个字段共用一段、看着像串行）",
          len(ch.CHIP_FIELD_HELP) == len(ch.CHIPS_DERIVED) and not same,
          f"{len(ch.CHIP_FIELD_HELP)} 条说明 / 共用 {list(same.values())[:2]}")
    check("每条说明都是完整句子、且写明单位或口径",
          all(len(d) >= 12 and ("（" in d or "%" in d or "/" in d)
              for _, d in ch.CHIP_FIELD_HELP),
          next((f"{k}: {d[:20]}" for k, d in ch.CHIP_FIELD_HELP if len(d) < 12), "OK"))
    # 「可用字段参考」直接显示的就是 condition_field_help() 的值 —— 断言它逐字段各说各的
    duphelp = [k for k in ch.CHIPS_DERIVED
               if " / " in daily.get(k, "")
               or sum(1 for x in ch.CHIPS_DERIVED if daily.get(x) == daily.get(k)) > 1]
    check("字段参考里每个筹码字段显示各自的说明（不再共用一段并列文本）",
          not duphelp and len({daily.get(k) for k in ch.CHIPS_DERIVED}) == len(ch.CHIPS_DERIVED),
          duphelp[:4] or daily.get("hold_chg", "")[:28])
    # 结果表列定义：每个筹码字段都要有中文表头 + 合法数值格式
    bad_col = [(k, v) for k, v in ch.CHIP_COL_SPEC.items()
               if not v[0] or v[0] == k or v[3] not in ("num", "big", "pct", "price", "int")]
    check("每个筹码字段都有结果表列定义（中文表头 + 合法格式）", not bad_col, bad_col[:2])
    # 全字段表头覆盖：结果表按「条件引用到的字段」自动加列，任何可用字段都得有中文表头，
    # 否则表头会退回英文字段名。**新加字段忘了补 COL_SPEC 会在这里红。**
    nohead = [n for n in st.condition_field_names()
              if not n.startswith("w_") and ind.col_spec(n)["label"] == n]
    check("所有可用日线字段都有中文表头（新字段忘了补 COL_SPEC 会红）",
          not nohead, f"缺表头 {nohead[:5]}")
    check("周线字段自动复用日线表头（加「周」前缀，不用再写一遍）",
          ind.col_spec("w_hold_chg")["label"] == "周" + ind.col_spec("hold_chg")["label"]
          and ind.col_spec("w_rz_net5")["fmt"] == "big",
          ind.col_spec("w_rz_net5")["label"])
    check("结果表列序 = 字段在条件里出现的先后",
          st.referenced_fields(st.merge_config(
              {"entry": ["rz_net5 > 0 and hold_chg < -3"], "score_expr": "ma20"}))
          == ["rz_net5", "hold_chg", "ma20"],
          st.referenced_fields(st.merge_config(
              {"entry": ["rz_net5 > 0 and hold_chg < -3"], "score_expr": "ma20"})))
    check("needs_chips：面板「全算」(None) 与纯技术字段都不读筹码表",
          ch.needs_chips(None) is False
          and ch.needs_chips({"close", "ma5", "mf_main_net"}) is False)
    bad_pref = [n for n in ch.CHIPS_DERIVED if not ch.needs_chips({n})]
    check("needs_chips：每个筹码字段名都能被前缀识别（漏了会被 only= 裁成恒 NaN）",
          not bad_pref, f"认不出 {bad_pref}")

    # ---- 7b 加载裁剪 ----
    E = str(db.scalar("SELECT MAX(trade_date) FROM margin_detail"))[:10]
    if not E:
        skip("选股字段（asof 对拍）", "margin_detail 还没有数据")
        return
    S = (pd.Timestamp(E) - pd.Timedelta(days=200)).strftime("%Y-%m-%d")
    codes = ["600519.SH", "000001.SZ", "002415.SZ"]
    ONLY = set(ch.CHIPS_DERIVED) | {"close", "pct_chg"}

    calls = {"n": 0}
    real_load = ch.load

    def _spy_load(*a, **kw):
        calls["n"] += 1
        return real_load(*a, **kw)

    ch.load = _spy_load                                        # type: ignore[assignment]
    try:
        market.load_panel(codes, S, E, adjust="qfq", weekly=False, only={"close", "ma5"})
        n_plain = calls["n"]
        calls["n"] = 0
        market.load_panel(codes, S, E, adjust="qfq", weekly=False,
                          only={"close", "hold_chg"})
        n_chip = calls["n"]
        calls["n"] = 0
        market.load_panel(codes, S, E, adjust="qfq", weekly=True,
                          weekly_fields={"w_ht_net30", "w_close"},
                          only={"close", "ma5"})
        n_week = calls["n"]
    finally:
        ch.load = real_load                                    # type: ignore[assignment]
    check("不引用筹码字段时一次都不读那三张表", n_plain == 0, f"读了 {n_plain} 次")
    check("引用筹码字段时才读", n_chip > 0, f"读了 {n_chip} 次")
    check("只有 w_ 版本被引用（only 里没有筹码）时也要读 —— 否则周线过滤恒不成立",
          n_week > 0, f"读了 {n_week} 次")

    panel = market.load_panel(codes, S, E, adjust="qfq", weekly=False, only=ONLY)
    check("面板带出全部 18 个筹码列（没数据的也要有列，只是 NaN）",
          bool(panel) and all(all(c in d.columns for c in ch.CHIPS_DERIVED)
                              for d in panel.values()), f"{len(panel)} 只")

    # ---- 7c 股东户数：按公告日 asof ----
    print("    —— 股东户数：按**公告日**对齐（未公告的一律用不上）")
    bad = {"num": 0, "chg": 0, "age": 0}
    n_row = nf = 0
    for code, d in panel.items():
        d = d.sort_values("trade_date").reset_index(drop=True)
        raw = db.query("SELECT ann_date, end_date, holder_num FROM holder_number "
                       "WHERE ts_code=%s ORDER BY end_date, ann_date", (code,))
        best: dict[str, tuple] = {}                 # end_date -> (最大 ann_date, 户数)
        for r in raw:
            k, a = str(r["end_date"])[:10], str(r["ann_date"])[:10]
            num = float(r["holder_num"]) if r["holder_num"] is not None else None
            if k not in best or a > best[k][0]:
                best[k] = (a, num)
        grow: dict[str, float | None] = {}
        prev = None
        for end in sorted(best):                    # 按报告期升序算相邻环比
            num = best[end][1]
            grow[end] = ((num / prev - 1) * 100) if (prev and num and prev > 0) else None
            prev = num
        seq = sorted((best[k][0], k) for k in best)  # 按 (公告日, 报告期) 升序
        for _, row in d.iterrows():
            T = str(row["trade_date"])[:10]
            n_row += 1
            cand = [x for x in seq if x[0] <= T]
            exp_num = exp_chg = exp_age = None
            if cand:
                ann, end = cand[-1]
                exp_num, exp_chg = best[end][1], grow[end]
                exp_age = (pd.Timestamp(T) - pd.Timestamp(ann)).days
                if ann > T:                          # 只可能出现在取数写错时
                    nf += 1
            if not _eq(row["hold_num"], exp_num):
                bad["num"] += 1
            if not _eq(row["hold_chg"], exp_chg):
                bad["chg"] += 1
            if not _eq(row["hold_age"], exp_age):
                bad["age"] += 1
    check("hold_num = 「ann_date ≤ T 的最新一期」户数（逐日独立复算对拍）",
          n_row and bad["num"] == 0, f"{bad['num']}/{n_row} 不符")
    check("hold_chg = 与相邻上一报告期的环比 %（逐日对拍）",
          n_row and bad["chg"] == 0, f"{bad['chg']}/{n_row} 不符")
    check("hold_age = 距发布日天数（逐日对拍）",
          n_row and bad["age"] == 0, f"{bad['age']}/{n_row} 不符")
    check("户数贴的是「已公告」的那一期（绝不出现未来公告日）", nf == 0, f"越界 {nf}")

    # 未来函数硬断言：拿一条真实公告 A，A 前一天必须是**上一期**的户数
    ann_row = db.query_one(
        "SELECT ann_date, holder_num FROM holder_number WHERE ts_code='600519.SH' "
        "ORDER BY ann_date DESC LIMIT 1")
    if ann_row:
        A = str(ann_row["ann_date"])[:10]
        d0 = panel["600519.SH"].sort_values("trade_date").reset_index(drop=True)
        txt = pd.to_datetime(d0["trade_date"]).dt.strftime("%Y-%m-%d")
        before = d0[txt < A]
        on_after = d0[txt >= A]
        check("公告日之前的 K 线用的是上一期户数（未来函数硬断言）",
              len(before) and not _eq(before["hold_num"].iloc[-1], ann_row["holder_num"]),
              f"{A} 公告 {ann_row['holder_num']} / 前一交易日 "
              f"{before['hold_num'].iloc[-1] if len(before) else None}")
        check("公告日当天起才换成新一期",
              len(on_after) and _eq(on_after["hold_num"].iloc[0], ann_row["holder_num"]),
              f"{on_after['trade_date'].iloc[0] if len(on_after) else '—'}")

    # ---- 7d 融资融券：按交易日 asof + 单位 ----
    print("    —— 融资融券：按**交易日**对齐 + 元→万元只换算一次")
    keys = ("rz_ye", "rz_net", "rz_net5", "rz_net20", "rz_chg5", "rzrq_ye", "rzrq_ratio")
    bad = {k: 0 for k in keys}
    n_row = 0
    for code, d in panel.items():
        d = d.sort_values("trade_date").reset_index(drop=True)
        mrows = db.query(
            "SELECT trade_date,rzye,rzmre,rzche,rqye,rzrqye FROM margin_detail "
            "WHERE ts_code=%s AND trade_date<=%s ORDER BY trade_date", (code, E))
        mvrows = db.query("SELECT trade_date,circ_mv FROM daily_basic "
                          "WHERE ts_code=%s AND trade_date<=%s ORDER BY trade_date",
                          (code, E))
        seq = [(str(r["trade_date"])[:10], r) for r in mrows]
        mv = [(str(r["trade_date"])[:10],
               float(r["circ_mv"]) if r["circ_mv"] is not None else None) for r in mvrows]

        def wsum(xs):
            return None if any(x is None for x in xs) else sum(xs)

        for _, row in d.iterrows():
            T = str(row["trade_date"])[:10]
            n_row += 1
            av = [x for x in seq if x[0] <= T]
            if not av:
                for k in keys:                       # 没有两融数据 → 必须全是缺值
                    if _isnum(row[k]):
                        bad[k] += 1
                continue
            nets = [(float(x[1]["rzmre"]) - float(x[1]["rzche"])
                     if (x[1]["rzmre"] is not None and x[1]["rzche"] is not None) else None)
                    for x in av]
            last = av[-1][1]
            mvw = None
            cand_mv = [x for x in mv if x[0] <= av[-1][0]]
            if cand_mv:
                mvw = cand_mv[-1][1]
            exp = {
                "rz_ye": float(last["rzye"]) / 1e4 if last["rzye"] is not None else None,
                "rz_net": (wsum(nets[-1:]) / 1e4) if wsum(nets[-1:]) is not None else None,
                "rz_net5": (wsum(nets[-5:]) / 1e4) if wsum(nets[-5:]) is not None else None,
                "rz_net20": (wsum(nets[-20:]) / 1e4)
                if wsum(nets[-20:]) is not None else None,
                "rz_chg5": (((float(last["rzye"]) / float(av[-6][1]["rzye"]) - 1) * 100)
                            if (len(av) >= 6 and av[-6][1]["rzye"] and last["rzye"])
                            else None),
                "rzrq_ye": float(last["rzrqye"]) / 1e4
                if last["rzrqye"] is not None else None,
                "rzrq_ratio": (float(last["rzrqye"]) / (mvw * 1e4) * 100)
                if (mvw and last["rzrqye"] is not None) else None,
            }
            for k in keys:
                if not _eq(row[k], exp[k]):
                    bad[k] += 1
    names_cn = {"rz_ye": "rz_ye = rzye/1e4（元→万元）", "rz_net": "rz_net = 净买入",
                "rz_net5": "rz_net5 = 近 5 个两融日累计净买入",
                "rz_net20": "rz_net20 = 近 20 个两融日累计净买入",
                "rz_chg5": "rz_chg5 = 融资余额 5 日变化率 %",
                "rzrq_ye": "rzrq_ye = 两融余额（万元）",
                "rzrq_ratio": "rzrq_ratio = 两融余额 ÷ 同日流通市值 %"}
    for k in keys:
        check(f"{names_cn[k]}（逐日独立复算对拍）", n_row and bad[k] == 0,
              f"{bad[k]}/{n_row} 不符")

    # ---- 7e 增减持：公告日窗口（开区间）----
    print("    —— 股东增减持：公告日窗口 (T-N, T]（公告当天就算近 N 日）")
    T0 = (pd.Timestamp(S) - pd.Timedelta(days=ch._TRADE_LOOKBACK)).strftime("%Y-%m-%d")
    bad = {"net30": 0, "cnt30": 0, "rat30": 0, "net90": 0}
    n_row = nf = 0
    for code, d in panel.items():
        d = d.sort_values("trade_date").reset_index(drop=True)
        raw = db.query(
            "SELECT ann_date,in_de,change_vol,change_ratio FROM holder_trade "
            "WHERE ts_code=%s AND ann_date BETWEEN %s AND %s", (code, T0, E))
        ev = [(str(r["ann_date"])[:10], str(r["in_de"] or "").upper(),
               float(r["change_vol"] or 0), float(r["change_ratio"] or 0)) for r in raw]
        covered = bool(ev)          # 与实现同一条"有覆盖"判据：载入窗口里有记录
        for _, row in d.iterrows():
            T = str(row["trade_date"])[:10]
            n_row += 1
            if not covered:         # 该股在窗口里一条公告都没有 → 必须整列缺值
                if any(_isnum(row[k]) for k in ("ht_net30", "ht_cnt30", "ht_net90")):
                    nf += 1
                continue
            ts = pd.Timestamp(T)
            for n, key_n, key_c, key_r in ((30, "net30", "cnt30", "rat30"),
                                           (90, "net90", None, None)):
                lo = ts - pd.Timedelta(days=n)
                win = [e for e in ev if lo < pd.Timestamp(e[0]) <= ts]
                if not _eq(row[f"ht_net{n}"],
                           sum((v if k == "IN" else -v) for _, k, v, _ in win) / 1e4):
                    bad[key_n] += 1
                if key_c and not _eq(row[f"ht_cnt{n}"], float(len(win))):
                    bad[key_c] += 1
                if key_r and not _eq(row[f"ht_ratio{n}"],
                                     sum((v if k == "IN" else -v) for _, k, _, v in win)):
                    bad[key_r] += 1
    check("ht_net30 / ht_net90 = 窗口内增持量 − 减持量（万股；逐日对拍）",
          n_row and bad["net30"] == 0 and bad["net90"] == 0,
          f"30 日 {bad['net30']} / 90 日 {bad['net90']} 不符（共 {n_row} 行）")
    check("ht_cnt30 = 窗口内公告条数（逐日对拍）", n_row and bad["cnt30"] == 0,
          f"{bad['cnt30']}/{n_row} 不符")
    check("ht_ratio30 = 窗口内变动比例之和 %（逐日对拍）", n_row and bad["rat30"] == 0,
          f"{bad['rat30']}/{n_row} 不符")
    check("该股在回看窗口里没有公告记录时整列缺值（不拿 0 冒充「没有增减持」）",
          nf == 0, f"{nf} 行该缺却有值")

    # 未来函数硬断言：公告日 A 当天算进窗口、A 前一天不算
    t_ann = db.query_one(
        "SELECT ann_date, in_de, change_vol FROM holder_trade WHERE ts_code='600519.SH' "
        "AND ann_date BETWEEN '2025-12-01' AND '2025-12-31' ORDER BY ann_date LIMIT 1")
    if t_ann:
        A = str(t_ann["ann_date"])[:10]
        tw = market.load_panel(["600519.SH"], "2025-11-01", "2026-01-31", adjust="qfq",
                               weekly=False, only=ONLY)["600519.SH"]
        txt = pd.to_datetime(tw["trade_date"]).dt.strftime("%Y-%m-%d")
        b4 = tw[txt < A]["ht_net30"]
        on = tw[txt == A]["ht_net30"]
        exp_on = float(t_ann["change_vol"] or 0) / 1e4 * (1 if t_ann["in_de"] == "IN" else -1)
        check("增减持公告当天才算进「近 30 日」（窗口是开区间 (T-30, T]）",
              len(on) and _eq(on.iloc[0], exp_on),
              f"{A} 当天 ht_net30={on.iloc[0] if len(on) else None} / 期望 {exp_on}")
        check("公告日之前的那根 K 线不含这条公告（未来函数硬断言）",
              len(b4) and not _eq(b4.iloc[-1], exp_on),
              f"前一交易日 ht_net30={b4.iloc[-1] if len(b4) else None}")

    # ---- 7f 缺数据降级 ----
    print("    —— 缺数据降级（宁可少选，绝不选错）")
    real_read = ch._read
    try:
        ch._read = lambda *a, **kw: None                       # type: ignore[assignment]
        p_missing = market.load_panel(codes, S, E, adjust="qfq", weekly=False, only=ONLY)
    finally:
        ch._read = real_read                                   # type: ignore[assignment]
    dm = p_missing[codes[0]]
    c_pos = st.merge_config({"entry": ["hold_chg > 0"], "buy_trigger": ["open > 0"]})
    c_zero = st.merge_config({"entry": ["ht_net30 == 0"], "buy_trigger": ["open > 0"]})
    check("读不到三张表时面板仍有 18 列（不会 KeyError）",
          all(c in dm.columns for c in ch.CHIPS_DERIVED))
    check("缺数据时 18 列全为 NaN",
          all(dm[c].isna().all() for c in ch.CHIPS_DERIVED))
    check("缺数据时 `hold_chg > 0` 恒不成立", not st.compute_signals(dm, c_pos)["entry"].any())
    check("缺数据时 `ht_net30 == 0` 也不成立（没把缺数据当 0）",
          not st.compute_signals(dm, c_zero)["entry"].any())
    check("缺筹码数据不影响行情列", len(dm) > 0 and dm["close"].notna().any(),
          f"{len(dm)} 行")

    # ---- 7g 周线口径 ----
    print("    —— 周线：按周取「当周最后一个交易日的值」（不是求和）+ w_ 无未来")
    wp = market.compute_weekly_panel(codes, S, E, adjust="qfq", only=ONLY)
    check("纯周线面板也带 18 列",
          bool(wp) and all(all(c in d.columns for c in ch.CHIPS_DERIVED)
                           for d in wp.values()), f"{len(wp)} 只")
    bad = n = 0
    for code, wd in wp.items():
        d = panel[code].copy()
        d["wk"] = pd.to_datetime(d["trade_date"]).dt.to_period("W-FRI")
        lastv = {pd.Timestamp(r.trade_date): (r.rz_net5, r.rzrq_ratio, r.hold_num)
                 for r in d.loc[d.groupby("wk")["trade_date"].idxmax()].itertuples()}
        for r in wd.itertuples():
            k = pd.Timestamp(r.trade_date)
            if k not in lastv:
                continue
            n += 1
            a, b, c = lastv[k]
            if not (_eq(r.rz_net5, a) and _eq(r.rzrq_ratio, b) and _eq(r.hold_num, c)):
                bad += 1
    check("周线每根 = 该周最后一个交易日的日线值（逐根对拍）", n and bad == 0,
          f"{bad}/{n} 不符")

    pw = market.load_panel(codes, S, E, adjust="qfq", weekly=True,
                           weekly_fields={"w_rz_net5", "w_close"},
                           only={"close", "rz_net5"})
    bad = n = nf2 = 0
    for code, d in pw.items():
        d = d.sort_values("trade_date").reset_index(drop=True)
        base = panel[code].copy()
        ts = pd.to_datetime(base["trade_date"])
        base["lab"] = ts + pd.to_timedelta(4 - ts.dt.weekday, "D")   # W-FRI 标签
        labmap = {pd.Timestamp(r.lab): r.rz_net5
                  for r in base.loc[base.groupby("lab")["trade_date"].idxmax()].itertuples()}
        for r in d.itertuples():
            if not _isnum(r.w_rz_net5):
                continue
            T = pd.Timestamp(r.trade_date)
            cand = [k for k in labmap if k <= T]
            if not cand:
                continue
            n += 1
            lab = max(cand)
            if lab > T:
                nf2 += 1
            if not _eq(r.w_rz_net5, labmap[lab]):
                bad += 1
    check("w_ 筹码字段只用「周标签 ≤ 当日」的已收盘周（无未来）", nf2 == 0, f"越界 {nf2}")
    check("w_ 筹码字段 = 该周最后一个交易日的值（逐行对拍）", n and bad == 0,
          f"{bad}/{n} 不符")

    # ---- 7h 端到端选股 ----
    print("    —— 端到端：选股真能用这些条件")
    cfg = st.merge_config({"name": "筹码验证", "entry": ["hold_num > 0"],
                           "buy_trigger": ["open > 0"], "sell_signal": ["close < ma5"],
                           "score_expr": "rz_net5"})
    check("required_daily_fields 会带上筹码字段（保证加载器去读那三张表）",
          {"hold_num", "rz_net5"} <= st.required_daily_fields(cfg),
          sorted(st.required_daily_fields(cfg) & set(ch.CHIPS_DERIVED)))
    # 条件池的 SQL 预筛只认 K 线/基本面字段。筹码条件**必须**退回全量加载 ——
    # 万一被塞进 SQL 会静默筛错（分子字段不在 daily_kline 里，SQL 直接报错或筛空）。
    cond_spec = {"base": "all", "where": ["hold_chg < 0"]}
    check("筹码条件不会被塞进 SQL 预筛（退回全量加载，结果不受影响只是慢些）",
          uni.preselect_codes(cond_spec, S, E) is None
          and "无法用 SQL 预筛" in uni.preselect_note(cond_spec),
          uni.preselect_note(cond_spec)[:46])
    # 说明要讲**真原因**：筹码字段是 K 线表里没这列，不是"写了 ref()"。
    # 用户看到"含 ref()/指标函数"会以为是自己表达式写错了。
    check("预筛说明指名是筹码字段（而不是笼统甩锅给 ref()/指标函数）",
          "筹码" in uni.preselect_note(cond_spec)
          and "ref()" not in uni.preselect_note(cond_spec),
          uni.preselect_note(cond_spec)[:40])
    # ⚠ 2026-09-19 起 `ref(x,N) op 常量` 已能翻成 SQL（「逐条翻译、翻不了就跳过」），
    #   所以「含 ref() 就不预筛」那句老文案已经不存在。这里改验**真原因**：
    #   `close > ref(close,1)` 翻不了，是因为 `close` 是**复权价字段**（面板是前复权价，
    #   拿去和 SQL 的原始价比会错），不是因为它写了 ref()。
    note_ref = uni.preselect_note({"base": "all", "where": ["close > ref(close, 1)"]})
    check("复权价条件跳过时，说明指出「复权价字段」（而不是笼统甩锅给 ref()）",
          "复权价" in note_ref and "筹码" not in note_ref, note_ref[:44])
    # ★ 反向锁一条：ref(形态字段, N) 这类**必须真的能预筛**（旧实现会整份放弃、退回全量）。
    #   单日调用才翻 ref（区间里「第 N 根」逐行变化，故意跳过）——见 universe.preselect_codes。
    check("★ ref(形态字段,N) 单日能预筛（不再因一条 ref 放弃整份 SQL）",
          uni.preselect_codes({"base": "all", "where": ["ref(is_down, 1) = 1"]}, E, E) is not None)
    # 成交额前 120 名：端到端选股与下面的回测路径都要用同一批票，先取好
    small = [r["ts_code"] for r in db.query(
        "SELECT ts_code FROM daily_kline WHERE trade_date=%s ORDER BY amount DESC "
        "LIMIT 120", (E,))]
    if FAST:
        skip("真实选股扫描", "--fast")
    else:
        res = screener.screen(cfg, universe="000300.SH", trade_date=E, limit=40, save=False)
        its = res.get("items") or []
        check("沪深300 上跑 `hold_num > 0` 能出结果",
              res.get("count", 0) > 0 and bool(its),
              f"命中 {res.get('count')} / 扫描 {res.get('scanned')}（{res.get('trade_date')}）")
        # 结果表列 = 「策略条件/评分里引用到的字段」− 已有固定列的那几个。
        # cfg 引用：hold_num(entry) / open(buy_trigger) / close,ma5(sell_signal) / rz_net5(score)
        want = ["hold_num", "open", "ma5", "rz_net5"]
        got = [c["key"] for c in res.get("extra_cols") or []]
        check("结果表列 = 条件引用到的字段（顺序同条件出现先后；close 已有固定列故不重复）",
              got == want and "close" not in got, f"{got}（期望 {want}）")
        check("这些列都带中文表头与数值格式（前端不写死字段名）",
              all(c.get("label") and c["label"] != c["key"] and c.get("fmt") in
                  ("num", "big", "mv", "pct", "price", "int") for c in res.get("extra_cols") or []),
              [(c["key"], c["label"], c["fmt"]) for c in res.get("extra_cols") or []])
        check("每只命中都带出了这些列的值（不必点开明细才能核对）",
              bool(its) and all(all(k in it for k in want) for it in its),
              f"{len(its)} 条 · 键 "
              f"{sorted(k for k in (its[0] if its else {}) if k[:2] in ('ho','rz','ht','rq'))}")
        check("表头/小数位/格式来自后端 col_spec（与 chips 字段表同源）",
              all(c == ind.col_spec(c["key"]) for c in res.get("extra_cols") or []),
              {c["key"]: c for c in res.get("extra_cols") or []}.get("hold_num"))
        check("评分表达式用了筹码字段且算出了值（score 非空）",
              bool(its) and all(it["score"] is not None for it in its),
              f"首条 score={its[0]['score'] if its else None}")
        check("命中明细的「关键指标」里能看到筹码字段",
              bool(its) and "hold_num" in (its[0].get("detail") or {}).get("values", {}))
        if its:
            code0 = its[0]["ts_code"]
            exp0 = db.query_one(
                "SELECT holder_num FROM holder_number WHERE ts_code=%s AND ann_date<=%s "
                "ORDER BY end_date DESC, ann_date DESC LIMIT 1", (code0, E))
            check("命中里的 hold_num 与库里最新已公告一期一致（抽 1 只独立复算）",
                  exp0 is not None and _eq(its[0]["hold_num"], exp0["holder_num"]),
                  f"{code0} 明细 {its[0]['hold_num']} / 库 {exp0 and exp0['holder_num']}")

        res2 = screener.screen(st.merge_config(
            {"name": "极端筹码条件", "entry": ["hold_num > 1e12"], "buy_trigger": ["open > 0"]}),
            universe={"codes": small}, trade_date=E, limit=5, save=False)
        check("不可能满足的筹码条件 → 0 命中（不误报）",
              res2.get("count", -1) == 0, f"命中 {res2.get('count')}")

        res3 = screener.screen(st.merge_config(
            {"name": "纯技术条件", "entry": ["close > ma5"], "buy_trigger": ["open > 0"]}),
            universe={"codes": small}, trade_date=E, limit=5, save=False)
        its3 = res3.get("items") or []
        check("不引用筹码的策略：命中里不带筹码键（也就没白读那三张表）",
              bool(its3) and "hold_num" not in its3[0],
              f"{len(its3)} 条 · 键 "
              f"{sorted(k for k in (its3[0] if its3 else {}) if k[:2] in ('ho','rz','ht','rq'))}")
        # 不引用筹码时，表头也不会多出筹码列；技术字段照样自动补列
        # （close 已有固定列；open 来自 buy_trigger，ma5 来自 entry，顺序同条件出现先后）
        got3 = [c["key"] for c in res3.get("extra_cols") or []]
        check("纯技术条件也自动补列，且表头里没有筹码列",
              got3 == ["ma5", "open"] and not [k for k in got3 if k in set(ch.CHIPS_DERIVED)],
              [(c["key"], c["label"]) for c in res3.get("extra_cols") or []])

        # 回测路径：prepare_data 把面板压成 numpy 数组（只留 entry/score 等信号），
        # 所以这里要验的是「筹码条件在回测里真的算出了信号」，而不是「带出筹码列」。
        from core import backtest as bt                                # noqa: PLC0415
        data = bt.prepare_data(small[:30], S, E, cfg)
        hit = [c for c, d in data.items() if d["entry"].any()]
        check("回测路径（prepare_data）里筹码条件算出了 entry 信号",
              bool(data) and {"entry", "score", "dates"} <= set(next(iter(data.values())))
              and bool(hit),
              f"{len(data)} 只 · 有 entry 命中 {len(hit)} 只")
        sc = [x for d in data.values() for x in d["score"] if x == x]   # x==x 排除 NaN
        check("回测路径里筹码评分（score_expr=rz_net5）算出了非空值",
              bool(sc), f"非空 {len(sc)} 个")


def main() -> int:
    db.init_schema()
    sec1_schema()
    sec2_units()
    sec2_merge()
    sec2c_trade_marks()
    sec3_sync_idempotent()
    sec3b_dirty_rows()
    sec4_invariants()
    sec5_module_surface()
    sec7_screen_fields()
    print(f"\n{'=' * 62}\n筹码与两融回归：{PASS} 通过 / {FAIL} 失败 / {SKIP} 跳过")
    if FAILS:
        print("失败项：")
        for x in FAILS:
            print("  -", x)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
