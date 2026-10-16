# -*- coding: utf-8 -*-
"""周线「只写已收盘的完整周」验证。

背景：周线用 `resample('W-FRI')` 合成，**标签是"该周的周五"**，不一定是交易日。
窗口截在周中时标签会落在未来，那根周线其实只有周一~周二的数据 ——
`dropna(subset=['close'])` 挡不住它（它有 close）。于是库里会出现
`trade_date = 2026-09-18` 这种行：内容是周一+周二合成的半周线，日期还是未来。

本脚本压住的不变式：
  1. 判据只有一个来源（`market.complete_week_mask`），且按「该周最后交易日」判断，
     不是拿标签（周五）跟日期比；
  2. `weekly_kline` 里**没有任何未来日期的行**，最大日期 <= 最近一个周收盘日；
  3. 周线面板（`compute_weekly_panel`）最后一根永远是已收盘的完整周 ——
     纯周线回测/选股不会读到半周线；
  4. `build_weekly` 能自愈：把人为插进去的"未来周线"删掉、把被改坏的周线刷回来；
  5. `pre_close` 不会因为取数窗口被切成半周而变成 NULL（它在裁剪之前算）；
  6. 纯周线策略在周中跑会给出明确提示，而不是静默返回 0 只；
  7. **只重算「该重算的几周」**：周线是日线的派生数据，日线没动过的那几百周不该重算
     （老写法固定 `since=start`，2023 起每次同步都把 190 周 ≈ 101.8 万行原样重写一遍，
     而那次日线其实只写了 1 个交易日 —— 白干 180 倍）。重算窗口由 `sync.weekly_since`
     一个函数决定，第 6 组把它的每条规则钉住。

用法：
    python tools/verify_weekly.py            # 全部（第 4 组自愈 + 第 6 组真跑两次周线合成，约 1 分钟）
    python tools/verify_weekly.py --no-db    # 只跑第 1、2 组，不碰库
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from core import db, market, screener, sync

# 本脚本第 4 组会真调 `sync.build_weekly`（自愈），而它现在**会往 sync_log 写一行**。
# 关掉日志开关，别让回归脚本用真实日期区间给「同步历史」灌记录（与 verify_chips 同一套做法）。
sync.SYNC_LOG_ENABLED = False

CODE = "600519.SH"
NO_DB = "--no-db" in sys.argv

PASS = FAIL = 0
FAILS: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  OK   {name}" + (f"  {extra}" if extra else ""))
    else:
        FAIL += 1
        FAILS.append(name)
        print(f"  FAIL {name}" + (f"  {extra}" if extra else ""))


def _s(v) -> str:
    return str(v)[:10] if v is not None else "-"


def week_of(d) -> pd.Timestamp:
    """把某个日期映射到它所在自然周的 W-FRI 周标签（周线表的 `trade_date` 就是它）。

    ⚠ `market.last_week_close_day()` 返回的是**该周最后一个交易日**（真实交易日），
    而 `weekly_kline.trade_date` / `compute_weekly_panel` 的 `trade_date` 用的是**周标签**
    （W-FRI 的周五，未必是交易日）。两者只在「周五恰好是交易日」的周里相等 ——
    节假日周会差开（如国庆周：标签 10-02、最后交易日 09-30）。
    凡是要拿去和 `weekly_kline` 的 `trade_date` / 面板末根比的地方，都得先过这个函数。
    """
    ts = pd.Timestamp(d)
    return (ts + pd.Timedelta(days=4 - ts.weekday())).normalize()


# ---------------------------------------------------------------------------
# 1. 判据函数
# ---------------------------------------------------------------------------
def t1(today: str, lwc: str, lwlab: str):
    print("\n【1】周线「已收盘」判据（week_bounds / complete_week_mask / is_week_close_day）")
    first, last = market.week_bounds(force=True)
    check("week_bounds 非空", len(last) > 0, f"{len(last)} 周")
    check("每个标签都是周五", all(pd.Timestamp(x).weekday() == 4 for x in last.index))
    check("首交易日 <= 末交易日", bool((first.dropna() <= last.dropna()).all()))
    # 必须存在「标签 ≠ 该周最后交易日」的周（周五休市），否则"拿标签比日期"的写法也能混过去
    diff = (last.dropna() != last.dropna().index.to_series())
    check("存在「标签≠该周最后交易日」的周（周五休市）", bool(diff.any()),
          f"{int(diff.sum())} 周")

    # 标签必须和 resample('W-FRI') 完全一致，否则过滤会打到空处
    ds = market.get_trade_dates()[-60:]
    idx = pd.to_datetime(pd.Series(ds, dtype="object"))
    s = pd.Series(idx.values, index=idx.values)
    want = set(s.resample("W-FRI").max().index)
    got = set(last.index)
    check("标签与 resample('W-FRI') 一致", want.issubset(got),
          f"缺 {len(want - got)} 个")

    check("last_week_close_day 有值", bool(lwc), str(lwc))
    check("last_week_close_day 自己就是周收盘日", market.is_week_close_day(lwc))
    check("last_week_close_day <= 最新日线日期", bool(lwc) and lwc <= today,
          f"{lwc} <= {today}")

    td = pd.Timestamp(today)
    this_lbl = (td + pd.Timedelta(days=4 - td.weekday())).normalize()
    if market.is_week_close_day(today):
        check("今天是周收盘日 -> 本周算完整",
              bool(market.complete_week_mask(pd.Series([this_lbl]), today).iloc[0]),
              _s(this_lbl))
    else:
        m = market.complete_week_mask(pd.Series([this_lbl, pd.Timestamp(lwlab)]), today)
        check("本周（未收盘）不算完整周", not bool(m.iloc[0]), _s(this_lbl))
        check("上一根完整周算完整", bool(m.iloc[1]), _s(lwlab))
        wend = market.week_bounds()[1].get(this_lbl)
        if wend is not None:
            check("cutoff 推到本周最后交易日 -> 变完整",
                  bool(market.complete_week_mask(pd.Series([this_lbl]), wend).iloc[0]),
                  _s(wend))
        wstart = market.week_bounds()[0].get(this_lbl)
        if wstart is not None and wend is not None:
            check("since 晚于该周首交易日 -> 不算完整（防窗口切出半周）",
                  not bool(market.complete_week_mask(
                      pd.Series([this_lbl]), wend,
                      since=wstart + pd.Timedelta(days=1)).iloc[0]))
    return this_lbl


# ---------------------------------------------------------------------------
# 2. 周线面板
# ---------------------------------------------------------------------------
def t2(today: str, lwc: str, lwlab: str):
    print("\n【2】周线面板（compute_weekly_panel）不含半周线")
    p = market.compute_weekly_panel([CODE], "2023-01-01", today)
    df = p.get(CODE)
    check("面板非空", df is not None and len(df) > 0)
    if df is None or not len(df):
        return
    tail = _s(df["trade_date"].iloc[-1])
    check("最后一根 = 最近完整周的标签", tail == lwlab, f"{tail} vs {lwlab}")
    check("最后一根是已收盘的完整周（complete_week_mask 判据）",
          bool(market.complete_week_mask(pd.Series([pd.Timestamp(tail)]), today).iloc[0]))
    p2 = market.compute_weekly_panel([CODE], "2023-01-01", lwc)
    d2 = p2.get(CODE)
    check("end 本身就是周收盘日时不丢最后一根",
          d2 is not None and _s(d2["trade_date"].iloc[-1]) == lwlab)


# ---------------------------------------------------------------------------
# 3~5. 需要读库
# ---------------------------------------------------------------------------
def t3(today: str, lwc: str, lwlab: str):
    print("\n【3】weekly_kline 不变式")
    mx = db.scalar("SELECT MAX(trade_date) FROM weekly_kline")
    check("最大日期不超过最新日线日期", _s(mx) <= today, f"max={_s(mx)} today={today}")
    check("最大日期不超过最近完整周的标签", _s(mx) <= lwlab, f"max={_s(mx)} lwlab={lwlab}")
    n = db.scalar("SELECT COUNT(*) FROM weekly_kline WHERE trade_date > %s", (today,))
    check("未来日期的行 = 0", int(n or 0) == 0, f"{n} 行")

    # 抽样：最后一根完整周的 OHLCV 必须等于该周日线的聚合
    _, wlast = market.week_bounds()
    wf, wl2 = market.week_bounds()
    wend = wlast.get(pd.Timestamp(lwlab))
    wstart = wf.get(pd.Timestamp(lwlab))
    if wend is None or wstart is None:
        check("该周有交易日", False)
        return
    d = db.query(
        "SELECT open,close,vol FROM daily_kline WHERE ts_code=%s "
        "AND trade_date BETWEEN %s AND %s ORDER BY trade_date",
        (CODE, wstart.strftime("%Y-%m-%d"), wend.strftime("%Y-%m-%d")))
    w = db.query_one("SELECT open,close,vol,pre_close FROM weekly_kline "
                     "WHERE ts_code=%s AND trade_date=%s", (CODE, lwlab))
    if not d or not w:
        check("样本周线/日线存在", False)
        return
    check("周线 open = 该周首个交易日开盘",
          abs(float(w["open"]) - float(d[0]["open"])) < 1e-6,
          f"{w['open']} vs {d[0]['open']}")
    check("周线 close = 该周最后交易日收盘",
          abs(float(w["close"]) - float(d[-1]["close"])) < 1e-6,
          f"{w['close']} vs {d[-1]['close']}")
    check("周线 vol = 该周日线成交量之和",
          abs(float(w["vol"]) - sum(float(x["vol"]) for x in d)) < 1e-4)
    check("周线 pre_close 不是 NULL（窗口切半周也不会算丢）",
          w["pre_close"] is not None, f"pre_close={w['pre_close']}")


def t4(today: str, lwc: str, this_lbl, lwlab: str):
    print("\n【4】build_weekly 自愈（会写库，跑完还原）")
    from core import sync
    fut = this_lbl.strftime("%Y-%m-%d")
    # 基准值必须在动手之前取。今天是周收盘日时 this_lbl == today == lwc，
    # 先播种再取基准，取到的就是刚改坏的值 → 假红（见 MEMORY 第 16 条）。
    good = db.query_one("SELECT close,pre_close FROM weekly_kline "
                        "WHERE ts_code=%s AND trade_date=%s", (CODE, lwlab))
    # 只有标签真的落在未来，"人造半周线"才有意义；周收盘日跑时它就是合法的完整周。
    seed_future = fut > today
    if seed_future:
        db.execute("DELETE FROM weekly_kline WHERE ts_code=%s AND trade_date=%s",
                   (CODE, fut))
        db.execute("INSERT INTO weekly_kline (ts_code,trade_date,open,high,low,close,vol)"
                   " VALUES (%s,%s,1,1,1,1,1)", (CODE, fut))
    if good is None:
        check("样本周线存在（可做自愈验证）", False, f"{CODE} {lwlab} 无行")
        return
    db.execute("UPDATE weekly_kline SET close=1, pre_close=NULL "
               "WHERE ts_code=%s AND trade_date=%s", (CODE, lwlab))
    print(f"       —— 已把 {lwlab} 的 close/pre_close 改坏"
          + (f"；并种入未来日期行 {CODE} {fut}" if seed_future
             else f"（今天 {today} 就是周收盘日，{fut} 不算未来日期 → 跳过造半周线）"))

    start = (pd.Timestamp(lwlab) - pd.Timedelta(days=14)).strftime("%Y-%m-%d")
    lid0 = db.scalar("SELECT IFNULL(MAX(id),0) FROM sync_log") or 0
    sync.build_weekly(start, today)
    check("回归跑周线不留同步日志（SYNC_LOG_ENABLED=False，历史里不该多出记录）",
          (db.scalar("SELECT IFNULL(MAX(id),0) FROM sync_log") or 0) == lid0,
          f"max(id) {lid0} → {db.scalar('SELECT IFNULL(MAX(id),0) FROM sync_log')}")

    if seed_future:
        n_fut = db.scalar("SELECT COUNT(*) FROM weekly_kline "
                          "WHERE ts_code=%s AND trade_date=%s", (CODE, fut))
    else:
        n_fut = db.scalar("SELECT COUNT(*) FROM weekly_kline WHERE trade_date > %s",
                          (today,))
    check("未来日期的半周线被清掉", int(n_fut or 0) == 0, f"{n_fut} 行")
    after = db.query_one("SELECT close,pre_close FROM weekly_kline "
                         "WHERE ts_code=%s AND trade_date=%s", (CODE, lwlab))
    check("被改坏的周线刷回来了",
          after is not None and good is not None
          and abs(float(after["close"]) - float(good["close"])) < 1e-6,
          f"{after['close'] if after else '-'} vs {good['close'] if good else '-'}")
    check("pre_close 也刷回来了（不是 NULL）",
          after is not None and after["pre_close"] is not None,
          f"pre_close={after['pre_close'] if after else '-'}")
    mx = db.scalar("SELECT MAX(trade_date) FROM weekly_kline")
    check("自愈后最大日期仍 <= 最近完整周的标签", _s(mx) <= lwlab, f"max={_s(mx)}")


def t5(today: str, lwc: str):
    print("\n【5】纯周线策略在周中的提示")
    if market.is_week_close_day(today):
        print("  --   今天是周收盘日，跳过（改天再跑这一组）")
        return
    r = screener.screen({"timeframe": "weekly"}, trade_date=today)
    msg = r.get("error") or ""
    check("周中跑周线策略返回明确提示（不是静默 0 只）",
          "不是本周的最后一个交易日" in msg)
    check("提示里给出最近一个周收盘日", bool(lwc) and lwc in msg)
    check("确实没返回候选", not r.get("items"))
    # 守卫必须挡在分批加载之前：源码里 is_week_close_day 那句要早于 for 循环
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "core", "screener.py"), encoding="utf-8").read()
    i_guard = src.find("is_week_close_day")
    i_loop = src.find("for i in range(0, n, BATCH_SIZE)")
    check("守卫在分批加载之前（不用扫完全市场才报错）",
          0 <= i_guard < i_loop, f"{i_guard} < {i_loop}")


def t6(today: str, lwc: str, lwlab: str):
    """【6】周线只重算该重算的几周（判据 = sync.weekly_since，纯函数喂合成 cutoff）。"""
    print("\n【6】周线重算窗口 weekly_since（增量，而不是把区间内所有周重写一遍）")
    S, E = "2023-01-01", today
    first, _ = market.week_bounds()
    if first.empty:
        check("周标签表非空（否则第 6 组无从判起）", False)
        return

    ws_lwc = first.get(week_of(lwc))
    check("上一完整周的首交易日在日历里", ws_lwc is not None and not pd.isna(ws_lwc),
          f"{lwc} → {_s(ws_lwc)}")
    if ws_lwc is None or pd.isna(ws_lwc):
        return
    ws_lwc = str(_s(ws_lwc))

    # --- 核心：只动了「本周还没收盘的那一天」时，重算窗口 = 最近一个完整周
    got = sync.weekly_since(S, E, today, changed=today)
    check("changed=今天（周二这种周中）→ 只重算最近一个完整周，不是整段",
          got == ws_lwc, f"since={got} 期望 {ws_lwc}")
    check("因此重算起点**不是**区间起点（老写法就是这个，才写出 101.8 万行）",
          got != S, f"since={got}")

    # --- 对齐：变动日落在周三/周五也要归到「那一周的首交易日」
    check("changed=完整周收盘日(周五) → 对齐到该周首交易日（不是拿周五当 since，"
          "否则 complete_week_mask 会把这一周自己筛掉）",
          sync.weekly_since(S, E, today, changed=lwc) == ws_lwc,
          f"since={sync.weekly_since(S, E, today, changed=lwc)}")

    # --- 兜底：这次日线一天都没写（都同步过了）也得重算最近一个完整周
    check("changed=None（本次日线没写任何一天）→ 仍重算最近一个完整周（不漏周）",
          sync.weekly_since(S, E, today, changed=None) == ws_lwc)

    # --- 历史补数：变动日落在很久以前 → 从那周起（不是只重算那一周：pre_close 要看上一周）
    ds = market.get_trade_dates() or []
    if len(ds) > 200:
        old_day = ds[-200]
        ws_old = first.get(week_of(old_day))
        got_old = sync.weekly_since(S, E, today, changed=old_day)
        check("changed=200 个交易日前（补历史缺口）→ 从那周首交易日起重算",
              ws_old is not None and not pd.isna(ws_old) and got_old == _s(ws_old),
              f"{old_day} → since={got_old}")
        check("补历史缺口时窗口比「最近一周」更早（说明确实按 changed 走）",
              got_old < ws_lwc, f"{got_old} < {ws_lwc}")

    # --- 整段重算：force 全量重拉时 daily 的 todo 就是整段 → changed=start
    check("changed=区间起点 → since=区间起点（force 整段重拉就走这条，与老行为一致）",
          sync.weekly_since(S, E, today, changed=S) == S)

    # --- cutoff 落在周中时，当周不算完整周（与 complete_week_mask 同一口径）
    # ⚠ 这个用例的前提是「**最新交易日所在的那一周还没收盘**」（平时今天 = 周二~周四）。
    #   库里最新交易日恰好是**周五**（例如周六来跑）时，`lwc` 与 `today` 落在同一周，
    #   于是 `ws_lwc`（"上一个完整周"）就等于 `mon` 本身，这句退化成
    #   「cutoff = 本周首日 → since 也是本周首日」—— 而 `weekly_since` 本来就是
    #   **再往前退一周**的语义，必然红。红的是用例自己，不是被测代码。
    #   2026-10-10（周六，最新交易日 10-09 周五）实测：cutoff=2026-10-08 → since=2026-09-28。
    #   那种时候这一组没有意义，跳过 —— 与上面 `is_week_close_day(today)` 那句同一个道理。
    mon = first.get(week_of(today))
    if (mon is not None and not pd.isna(mon)
            and week_of(lwc) != week_of(today)
            and not market.is_week_close_day(pd.Timestamp(mon).strftime("%Y-%m-%d"))):
        m = pd.Timestamp(mon).strftime("%Y-%m-%d")
        check("cutoff 落在本周（未收盘）→ 本周不算完整周，窗口仍停在上一个完整周",
              sync.weekly_since(S, E, m, changed=m) == ws_lwc,
              f"cutoff={m} → since={sync.weekly_since(S, E, m, changed=m)}")

    # --- 夹回用户给的区间：起点比「最近完整周」还晚时，不许越过 start
    check("start 晚于最近完整周 → 夹回 start（否则会写出区间起点之前那根半周线）",
          sync.weekly_since(today, today, today, changed=None) == today,
          f"since={sync.weekly_since(today, today, today, changed=None)}")

    # --- 静态：这条信息是怎么从日线传到周线的
    import io as _io
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sy = _io.open(os.path.join(root, "core", "sync.py"), encoding="utf-8").read()
    ap = _io.open(os.path.join(root, "app.py"), encoding="utf-8").read()
    check("build_weekly 收 changed（重算窗口由「日线真的动过哪几天」决定）",
          "changed: str | None = None" in sy and "since = weekly_since(" in sy)
    check("sync_daily_kline 把「本次真正写过的最早一天」报出来给周线用",
          '"first_day": todo[0] if todo else None' in sy)
    check("full_sync 把它传给周线合成",
          'changed=(res.get("daily") or {}).get("first_day")' in sy)
    check("手动「重新合成周线」也收 force（勾了就整段重算）",
          "changed=start if force else None" in ap)
    check("可写周的判据仍是 market.complete_week_mask 一个来源（只是全市场算一次）",
          "market.complete_week_mask(" in sy and "ok_label.get(" in sy)
    check("日志说明里写明重算窗口（页面上那一列不再是天书）",
          "（只重算 {since} 起的周线）" in sy and "（整段重算）" in sy)

    # --- 真跑一次：行数应该等于「最新完整周那一周的行数」，而不是整张表
    total = int(db.scalar("SELECT COUNT(*) FROM weekly_kline") or 0)
    cnt = int(db.scalar("SELECT COUNT(*) FROM weekly_kline WHERE trade_date=%s",
                        (lwlab,)) or 0)
    n = sync.build_weekly(S, E, changed=today)
    check("真跑一次（changed=今天）只重算最新完整周这一周",
          n == cnt, f"合成 {n} 行 / 该周周线 {cnt} 行")
    check("重算量远小于整张表（老写法每次 = 整张表）",
          0 < n < max(total // 10, 1), f"{n} vs 全表 {total}")
    check("重跑幂等（同一个数）", sync.build_weekly(S, E, changed=today) == n)


def main() -> int:
    print("=" * 72)
    print("周线「只写已收盘的完整周」验证")
    print("=" * 72)
    today = market.latest_trade_date()
    if not today:
        print("本地无行情数据，先去「数据管理」同步")
        return 1
    lwc = market.last_week_close_day(today)
    if not lwc:
        print("trade_cal 无数据，先同步基础信息")
        return 1
    # lwc 是**该周最后一个交易日**（真实交易日）；lwlab 是它的**周标签**（W-FRI）。
    # 周线表 / 面板的 trade_date 用的是后者 —— 节假日周两者会差开（国庆：交易日 09-30 / 标签 10-02）。
    lwlab = week_of(lwc).strftime("%Y-%m-%d")
    print(f"  最新日线日期 {today} ｜ 最近周收盘日 {lwc}（周标签 {lwlab}）")

    this_lbl = t1(today, lwc, lwlab)
    t2(today, lwc, lwlab)
    if not NO_DB:
        t3(today, lwc, lwlab)
        t4(today, lwc, this_lbl, lwlab)
        t5(today, lwc)
        t6(today, lwc, lwlab)

    print("\n" + "=" * 72)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAILS:
        for f in FAILS:
            print(f"  - {f}")
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
