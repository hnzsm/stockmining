# -*- coding: utf-8 -*-
"""数据同步历史（sync_log）验证：自增 id、状态收尾、区间与行数、启动清理、前端口径。

背景（这是本脚本存在的理由）：五处同步入口原先写成
`log_id = db.execute("INSERT INTO sync_log …")`，而 `db.execute()` 返回的是**受影响行数**
（INSERT 成功恒为 1）。于是每一次收尾都 UPDATE 到 id=1，历史里除第一条外**全是 running、
行数全是 0、结束时间全是空**，第一条又被后来的任务反复覆盖成一条四不像的记录。

用法: python tools/verify_sync_log.py     （不需要起服务；HTTP 段服务没起就 SKIP）
"""
from __future__ import annotations

import io
import os
import re
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PASS: list[str] = []
FAIL: list[str] = []
SKIP: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"  — {detail}" if detail else ""))


def skip(name: str, why: str) -> None:
    SKIP.append(name)
    print(f"  SKIP {name}  — {why}")


def _src(p: str) -> str:
    with io.open(os.path.join(ROOT, p), encoding="utf-8") as f:
        return f.read()


def _code(p: str) -> str:
    """源码去掉注释行后的部分 —— 静态检查必须只看代码，不看注释。

    `core/sync.py` 里刻意留了一段 `log_id = db.execute(INSERT…)` 的反面教材注释，
    不去注释的话检查会被自己写的说明绊倒。
    """
    lines = [ln for ln in _src(p).split("\n") if not ln.lstrip().startswith("#")]
    return "\n".join(lines)


from core import db, jobs, sync   # noqa: E402

BASE = "http://127.0.0.1:8777"
# 本脚本造的数据一律带这个前缀/假日期，收尾时按它精确清理
_T0, _T1 = "1990-01-01", "1990-01-02"
_MADE: list[int] = []


def _mk(task: str) -> int:
    """开一条测试日志（走真函数 _open_log，不是自己拼 SQL）。"""
    lid = sync._open_log(task, _T0, _T1)
    assert lid, "SYNC_LOG_ENABLED 被关掉了，测试前提不成立"
    _MADE.append(lid)
    return lid


def _row(lid: int) -> dict:
    return db.query("SELECT * FROM sync_log WHERE id=%s", (lid,))[0]


print("=" * 78)
print("数据同步历史（sync_log）验证")
print("=" * 78)

# ------------------------------------------------------------------ 【1】静态口径
print("\n【1】静态口径：id 只能取自增、日志只在两处开合")
sy = _code("core/sync.py")
bad = [m.start() for m in re.finditer(r"log_id\s*=\s*db\.execute\(", sy)]
check("core/sync.py 里没有 `log_id = db.execute(INSERT…)` 的误用（execute 返回受影响行数、恒为 1）",
      not bad, f"命中 {len(bad)} 处")
check("sync_log 的 INSERT 只有一处（集中在 _open_log，不再散落 5 份）",
      len(re.findall(r"INSERT INTO sync_log", sy)) == 1,
      f"{len(re.findall('INSERT INTO sync_log', sy))} 处")
check("状态词表唯一来源 STATUS_LABEL，五种状态齐全",
      all(f'"{k}"' in sy for k in
          ("running", "done", "failed", "cancelled", "interrupted")))
check("启动清理 close_stale_logs() 存在且被 bootstrap 调用",
      "def close_stale_logs(" in sy and "sync.close_stale_logs()" in _src("app.py"))

# 入口清单：任务名 = 库里 task 列的取值，也是后面按名断言的依据。
# 周线原先压根不写日志（页面上那条「写入 0 行」就是在没有记录的状态下看 job 日志）。
# 2026-09-27 加两个期货入口：期货日线改成 tushare 落本地库后，它就**是**本地库写入任务
# ⇒ 必须跟其它入口一样进 sync_log（原先走东财站外接口时不入库，也就不记日志）。
TASKS = ("daily_kline", "money_flow", "holder_number", "margin_detail",
         "holder_trade", "weekly_kline", "futures_basic", "futures_daily",
         "futures_option")
opened = re.findall(r'log_id = _open_log\("([a-z_]+)"', sy)
check("9 个同步入口（含周线 weekly_kline、期货三个）都开了日志，任务名与 task 列一致",
      sorted(opened) == sorted(TASKS), f"开到 {sorted(opened)}")
check("开合成对：每个入口正好一处 _open_log 对一处 _close_log(log_id, …)",
      len(re.findall(r'_close_log\(log_id,', sy)) == len(TASKS),
      f"开 {len(opened)} / 收 {len(re.findall(r'_close_log[(]log_id,', sy))}")
check("9 个同步入口都挂了 logged_sync 兜底（异常逃逸时不留 running）",
      len(re.findall(r"^@logged_sync$", sy, re.M)) == len(TASKS),
      f"{len(re.findall(r'^@logged_sync$', sy, re.M))} 个")
# 行数口径：不能把 MySQL 的受影响行数当「写了几行」报出去
check("周线报的行数是「本次合成行数」(len(out))，不是你 execute 回来的受影响行数",
      "affected += db.insert_many(sql, out)" in sy
      and "total_rows += len(out)" in sy,
      "受影响行数只进应用日志与说明")
_leftover = re.findall(r"(?:rows_cnt|rows_daily|rows_basic|total)\s*\+?=\s*db\.insert_many\("
                       r"|return db\.insert_many\(", sy)
check("六个入口没有任何一处的「行数」直接吃 db.insert_many 的返回值（含日线/资金流/股东/两融）",
      not _leftover, f"命中 {len(_leftover)} 处：{_leftover[:3]}")
check("_insert_or_log 返回的是处理行数 len(rows)（受影响行数只用来判「纯覆盖写」）",
      "return len(rows)" in sy.split("def _insert_or_log")[1].split("def ")[0]
      and "if not affected and jid:" in sy)
check("db.insert_many 的注释写明它返回的是受影响行数（ON DUPLICATE KEY 值不变时为 0）",
      "受影响行数" in _src("core/db.py").split("def insert_many")[1][:700])

ap = _src("app.py")
check("/api/sync/logs 由后端给 status_label / status_cls（前端不自己映射英文状态）",
      "status_label" in ap and "status_cls" in ap)
for tpl in ("templates/data.html", "templates/index.html"):
    h = _src(tpl)
    check(f"{tpl} 用后端给的标签铺状态格（且不再自己判 status==='done'）",
          "status_label" in h and "'done'?'g'" not in h and "'done' ? 'g'" not in h)

# ------------------------------------------------------------------ 【2】库契约
print("\n【2】库契约：自增 id、各自收尾、启动清理（全用合成数据，跑完删干净）")
lid1 = _mk("__t_a__")
lid2 = _mk("__t_b__")
check("_open_log 返回的是**自增 id**（不是恒为 1 的受影响行数）",
      lid1 > 1 and lid2 == lid1 + 1, f"id1={lid1} id2={lid2}")
r1, r2 = _row(lid1), _row(lid2)
check("两条新日志刚开出来都是 running、行数 0、结束时间空",
      r1["status"] == "running" and r2["status"] == "running"
      and not r1["rows_cnt"] and not r1["ended_at"])

sync._close_log(lid1, 7, "a=7")
sync._close_log(lid2, 9, "b=9")
r1, r2 = _row(lid1), _row(lid2)
check("收尾后 status=done 且补上结束时间",
      r1["status"] == "done" and r2["status"] == "done"
      and r1["ended_at"] is not None and r2["ended_at"] is not None)
check("两条日志**各自**被收尾（原 bug 是都打到 id=1，第二条永远 running）",
      r2["status"] == "done", f"id={lid2} → {r2['status']}")
check("行数按各自的实参写入（不是都写第一条）",
      r1["rows_cnt"] == 7 and r2["rows_cnt"] == 9, f"{r1['rows_cnt']} / {r2['rows_cnt']}")
check("说明文字按各自的实参写入",
      r1["message"] == "a=7" and r2["message"] == "b=9",
      f"{r1['message']!r} / {r2['message']!r}")
check("区间（date_from / date_to）如实落库",
      str(r1["date_from"]) == _T0 and str(r1["date_to"]) == _T1,
      f"{r1['date_from']} ~ {r1['date_to']}")

# 异常逃逸 → logged_sync 兜底成 failed


@sync.logged_sync
def _boom():
    sync._open_log("__t_c__", _T0, _T1)
    raise RuntimeError("故意炸一下")


try:
    _boom()
except RuntimeError:
    pass
    # 期望：装饰器把这条日志收成 failed（否则永远挂着 running）
c_like = db.query("SELECT * FROM sync_log WHERE task='__t_c__' ORDER BY id DESC LIMIT 2")
if c_like:
    _MADE += [x["id"] for x in c_like]
    boom = c_like[0]
    check("异常逃逸时 logged_sync 把日志标成 failed 并补结束时间（不留 running）",
          boom["status"] == "failed" and boom["ended_at"] is not None,
          f"{boom['status']} / {boom['ended_at']}")
    check("failed 的说明里带上异常类型",
          "RuntimeError" in (boom["message"] or ""), (boom["message"] or "")[:60])
else:
    check("异常逃逸时 logged_sync 把日志标成 failed 并补结束时间（不留 running）", False,
          "没找到 __t_c__ 的日志行")

# close_stale_logs：启动时把孤儿 running 标成已中断
live = db.scalar(
    "SELECT COUNT(*) FROM sync_log WHERE status='running' AND task NOT LIKE '\\_\\_t%' "
    "AND started_at > NOW() - INTERVAL 10 MINUTE") or 0
stale_id = _mk("__t_stale__")
if live:
    skip("启动清理把孤儿 running 标成已中断", f"现在有 {live} 条正在跑的同步，跳过以免误伤")
else:
    n = sync.close_stale_logs()
    s = _row(stale_id)
    check("启动清理把孤儿 running 标成 interrupted、补结束时间并说明原因",
          n >= 1 and s["status"] == "interrupted" and s["ended_at"] is not None
          and "未收尾" in (s["message"] or ""),
          f"清理 {n} 条 → {s['status']} / {(s['message'] or '')[:40]}")
    check("启动清理可重复执行（第二次没有 running 可清 → 返回 0）",
          sync.close_stale_logs() == 0)

check("状态标签映射：库里的状态都有中文标签，未知状态原样显示且按红处理",
      sync.status_label("done") == ("已完成", "g")
      and sync.status_label("running")[1] == "y"
      and sync.status_label("__x__") == ("__x__", "r"),
      str(sync.status_label("interrupted")))

# ------------------------------------------------------------------ 【3】端到端
print("\n【3】端到端：喂合成数据真跑一次 sync_holder_trade，看它自己的日志")
import pandas as pd   # noqa: E402

FAKE = "000000.SZ"
df = pd.DataFrame({"ts_code": [FAKE], "ann_date": ["19900105"],
                   "holder_name": ["某股东"], "holder_type": ["C"], "in_de": ["DE"],
                   "change_vol": [1000.0], "change_ratio": [0.5],
                   "after_share": [9000.0], "after_ratio": [10.0],
                   "avg_price": [None], "total_share": [9000.0]})
real_call = sync.call
before = db.scalar("SELECT IFNULL(MAX(id),0) FROM sync_log") or 0
try:
    sync.call = lambda api, **kw: df if api == "stk_holdertrade" else pd.DataFrame()
    out = sync.sync_holder_trade(_T0, _T1, force=True)
    new = db.query("SELECT * FROM sync_log WHERE id>%s ORDER BY id", (before,))
    _MADE += [x["id"] for x in new]
    lg = new[0] if new else {}
    check("跑完一次真同步：日志是 done、行数与返回值一致、结束时间非空",
          bool(new) and lg.get("status") == "done"
          and lg.get("rows_cnt") == out["rows"] and lg.get("ended_at") is not None,
          f"{lg.get('status')} / rows={lg.get('rows_cnt')} vs {out['rows']}")
    check("这条日志的区间与任务名就是这次调用的实参（不是被别的任务覆盖的）",
          lg.get("task") == "holder_trade" and str(lg.get("date_from")) == _T0,
          f"{lg.get('task')} {lg.get('date_from')} ~ {lg.get('date_to')}")

    # 取消：循环一进来就 break，收尾必须是「已取消」而不是「已完成」
    jid = jobs.new_job("verify_sync_log", "取消路径")
    jobs.cancel(jid)
    out2 = sync.sync_holder_trade(_T0, _T1, jid=jid, force=True)
    new2 = db.query("SELECT * FROM sync_log WHERE id>%s ORDER BY id", (before,))
    _MADE += [x["id"] for x in new2]
    lg2 = new2[-1] if new2 else {}
    check("中途取消 → 日志标成 cancelled（不谎报 done）",
          lg2.get("status") == "cancelled", f"{lg2.get('status')} / out={out2['days']} 天")

    # 日线（最常用的入口）也走一遍：挑一个已同步完的交易日 → todo 为空、一次网络请求都不发，
    # 但日志照样要开出来、照样要收尾（这条能廉价地把 daily_kline 那条路径也钉住）。
    filled = db.query("SELECT trade_date FROM daily_kline ORDER BY trade_date DESC LIMIT 1")
    if filled:
        day = filled[0]["trade_date"].strftime("%Y-%m-%d")
        b2 = db.scalar("SELECT IFNULL(MAX(id),0) FROM sync_log") or 0
        out3 = sync.sync_daily_kline(day, day, force=False, with_basic=True)
        new3 = db.query("SELECT * FROM sync_log WHERE id>%s ORDER BY id", (b2,))
        _MADE += [x["id"] for x in new3]
        lg3 = new3[0] if new3 else {}
        check("日线入口（已同步区间 → 0 天可做）也会如实收尾，不留 running",
              bool(new3) and lg3.get("status") == "done"
              and lg3.get("ended_at") is not None
              and lg3.get("task") == "daily_kline"
              and lg3.get("message", "").startswith("daily="),
              f"{day} → {lg3.get('status')} / {lg3.get('message')} / 实做 {out3['days']} 天")
    else:
        skip("日线入口的日志收尾", "库里没有日线数据，挑不出「已同步完的交易日」")

    # 周线（原先**一条日志都没有**；行数报的又是 MySQL 受影响行数 ⇒ 永远「写入 0 行」）。
    # 输入域压到 3 只票：全市场跑一遍 90 秒，3 只票 0.05 秒 —— 这样才进得了回归。
    w_codes = ["000001.SZ", "600000.SH", "600519.SH"]
    W0, W1 = "2026-09-14", "2026-09-18"        # 已收盘的完整周（周五）
    wargs = tuple(w_codes) + (W0, W1)
    real_codes = sync._weekly_codes
    sync._weekly_codes = lambda s, e: list(w_codes)
    try:
        wcnt = "SELECT COUNT(*) FROM weekly_kline WHERE ts_code IN (%s,%s,%s) " \
               "AND trade_date BETWEEN %s AND %s"
        n_before = db.scalar(wcnt, wargs)
        b3 = db.scalar("SELECT IFNULL(MAX(id),0) FROM sync_log") or 0
        w1 = sync.build_weekly(W0, W1)
        w2 = sync.build_weekly(W0, W1)          # 幂等重跑：受影响行数会是 0
        n_after = db.scalar(wcnt, wargs)
        wnew = db.query("SELECT * FROM sync_log WHERE id>%s ORDER BY id", (b3,))
        _MADE += [x["id"] for x in wnew]
        lgw = wnew[-1] if wnew else {}
        check("周线合成会在同步历史里留一条 weekly_kline 记录（原先一条都没有）",
              len(wnew) >= 1 and lgw.get("task") == "weekly_kline",
              f"{len(wnew)} 条 / task={lgw.get('task')}")
        check("周线的行数是「本次合成行数」而不是受影响行数（重跑同一个数、且不为 0）",
              w1 == w2 == len(w_codes) and w1 > 0,
              f"返回 {w1} / {w2}；提交 {len(w_codes)} 行，受影响行数恒为 0")
        check("日志里的行数与返回值一致（页面上「行数」那一列就是它）",
              lgw.get("rows_cnt") == w1, f"库 {lgw.get('rows_cnt')} vs 返回 {w1}")
        check("说明里点破「覆盖写入、值无变化」，免得又被当成没写",
              "覆盖写入" in (lgw.get("message") or ""), (lgw.get("message") or "")[:36])
        check("重跑是覆盖写、不追加（这 3 只票这周的周线行数不变）",
              n_before == n_after == len(w_codes), f"{n_before} → {n_after}")
        check("周线日志的区间就是这次调用的实参",
              str(lgw.get("date_from")) == W0 and str(lgw.get("date_to")) == W1,
              f"{lgw.get('date_from')} ~ {lgw.get('date_to')}")

        # 空区间：没有日线也要留一条日志说明为什么 0 行。
        # ⚠ 这一段必须换回**真**输入域 —— 它考的就是「区间里查不到日线」那条分支，
        #   带着 3 只票的桩跑，会走成「有输入但一行都没算出来」，测不到这条。
        sync._weekly_codes = real_codes
        wj = sync.build_weekly("2099-01-03", "2099-01-07")
        sync._weekly_codes = lambda s, e: list(w_codes)
        wnew2 = db.query("SELECT * FROM sync_log WHERE id>%s ORDER BY id", (b3,))
        _MADE += [x["id"] for x in wnew2]
        lg0 = wnew2[-1] if wnew2 else {}
        check("空区间也照开照收，并在说明里写清为什么是 0 行",
              wj == 0 and lg0.get("status") == "done" and lg0.get("rows_cnt") == 0
              and "没有日线数据" in (lg0.get("message") or ""),
              f"{(lg0.get('message') or '(没日志)')[:40]}")

        jidw = jobs.new_job("verify_sync_log", "周线取消路径")
        jobs.cancel(jidw)
        b4 = db.scalar("SELECT IFNULL(MAX(id),0) FROM sync_log") or 0
        sync.build_weekly(W0, W1, jid=jidw)
        wnew3 = db.query("SELECT * FROM sync_log WHERE id>%s ORDER BY id", (b4,))
        _MADE += [x["id"] for x in wnew3]
        check("周线中途取消 → 日志标成 cancelled（原先它压根不看 jid）",
              bool(wnew3) and wnew3[-1]["status"] == "cancelled",
              f"{(wnew3[-1]['status'] if wnew3 else '(没日志)')}")
    finally:
        sync._weekly_codes = real_codes
finally:
    sync.call = real_call
    db.execute("DELETE FROM holder_trade WHERE ts_code=%s", (FAKE,))
    if _MADE:
        db.execute(f"DELETE FROM sync_log WHERE id IN ({','.join(['%s'] * len(_MADE))})",
                   tuple(_MADE))
left = db.scalar("SELECT COUNT(*) FROM sync_log WHERE task LIKE '\\_\\_t%'") or 0
check("本脚本造的行已清理干净（同步历史里不留测试痕迹）",
      left == 0 and not db.scalar("SELECT COUNT(*) FROM holder_trade WHERE ts_code=%s",
                                  (FAKE,)),
      f"残留 {left} 行")

# ------------------------------------------------------------------ 【4】HTTP 契约
print("\n【4】接口契约：/api/sync/logs 的状态字段")
try:
    with urllib.request.urlopen(BASE + "/api/sync/logs", timeout=30) as r:
        import json
        data = json.loads(r.read().decode("utf-8"))["data"]
except Exception as e:  # noqa: BLE001
    skip("接口 /api/sync/logs 的状态字段", f"服务没起（{type(e).__name__}）")
    data = None
if data is not None:
    check("每条记录都带 status_label / status_cls（前端只铺格子）",
          bool(data) and all(x.get("status_label") and x.get("status_cls") in ("g", "y", "r")
                             for x in data),
          f"{len(data)} 条")
    check("接口里没有「挂着 running 却早就结束」的记录",
          not [x for x in data if x.get("status") == "running"],
          f"{len([x for x in data if x.get('status') == 'running'])} 条 running")
    check("状态标签是中文，英文状态只留在 title 里",
          all(re.search(r"[\u4e00-\u9fa5]", x["status_label"]) for x in data),
          " / ".join(sorted({x["status_label"] for x in data})))

# ------------------------------------------------------------------ 【5】历史卫生
# 放在最后：本脚本自己也会造 1990 年的假区间行，必须等【3】清理完再查。
print("\n【5】历史卫生：假区间与孤儿 running（跑完全部合成数据后再查，否则会被自己绊倒）")
fake = db.scalar("SELECT COUNT(*) FROM sync_log WHERE date_from < '2000-01-01'") or 0
check("同步历史里没有测试脚本灌的假区间（1990 年代的假日期一条都不该留）",
      fake == 0, f"{fake} 条")
# ⚠ 判据只能是「1990 年代」：用户真拿 2022-01-01 起跑过（id 18/19），
#   用「早于数据起点 2023-01-01」当判据会误伤真记录。
ghost = db.scalar("SELECT COUNT(*) FROM sync_log WHERE status='running' "
                  "AND started_at < NOW() - INTERVAL 3 HOUR") or 0
check("没有「早该结束却还挂着 running」的老记录（超过 3 小时未收尾即视为孤儿）",
      ghost == 0, f"{ghost} 条")
tot = db.scalar("SELECT COUNT(*) FROM sync_log") or 0
check("同步历史里每条都有结束时间，状态只有 done / cancelled / failed / interrupted 之一",
      not db.scalar("SELECT COUNT(*) FROM sync_log WHERE ended_at IS NULL "
                    "AND status <> 'running'")
      and not db.scalar("SELECT COUNT(*) FROM sync_log WHERE status NOT IN "
                        "('running','done','cancelled','failed','interrupted')"),
      f"共 {tot} 条")

print("\n" + "=" * 78)
print(f"数据同步历史验证：{len(PASS)} 通过 / {len(FAIL)} 失败 / {len(SKIP)} 跳过")
for f in FAIL:
    print("  未通过:", f)
print("=" * 78)
sys.exit(1 if FAIL else 0)
