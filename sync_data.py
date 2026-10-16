# -*- coding: utf-8 -*-
"""批量同步指定区间（后台执行）。

用法:
    python sync_data.py 2023-01-01 2026-09-12
    python sync_data.py 2023-01-01 2024-06-30 --no-weekly   # 并行分片时用
    python sync_data.py 2023-01-01 2026-09-12 --only-weekly # 只补周线
    python sync_data.py 2023-01-01 2026-09-12 --only-index  # 只补指数（回测基准）
    python sync_data.py 2023-01-01 2026-09-12 --only-moneyflow  # 只补个股资金流向
    python sync_data.py 2023-01-01 2026-09-12 --only-holders    # 只补股东户数/两融/增减持
    python sync_data.py --only-futures          # 只补期货（主连全历史 + 具体合约近 1 年 + 在市期权）
    python sync_data.py 2023-01-01 2026-09-12 --no-moneyflow    # 跳过资金流向
    python sync_data.py 2023-01-01 2026-09-12 --no-futures      # 跳过期货
    python sync_data.py --only-concept          # 只补东财概念（走东财接口，不耗 tushare 额度）
    python sync_data.py --only-newconcept       # 只补东财「新概念速览」（站外接口，每次真抓）
"""
import sys, os, time, traceback, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
FLAGS = {a for a in sys.argv[1:] if a.startswith("--")}

START = ARGS[0] if len(ARGS) > 0 else "2023-01-01"
END = ARGS[1] if len(ARGS) > 1 else datetime.date.today().strftime("%Y-%m-%d")
NO_WEEKLY = "--no-weekly" in FLAGS
NO_BASE = "--no-base" in FLAGS
NO_MONEYFLOW = "--no-moneyflow" in FLAGS
NO_HOLDERS = "--no-holders" in FLAGS
NO_CONCEPT = "--no-concept" in FLAGS
NO_NEWCONCEPT = "--no-newconcept" in FLAGS
NO_FUTURES = "--no-futures" in FLAGS
ONLY_WEEKLY = "--only-weekly" in FLAGS
ONLY_INDEX = "--only-index" in FLAGS
ONLY_MONEYFLOW = "--only-moneyflow" in FLAGS
ONLY_HOLDERS = "--only-holders" in FLAGS
ONLY_FUTURES = "--only-futures" in FLAGS
ONLY_CONCEPT = "--only-concept" in FLAGS
ONLY_NEWCONCEPT = "--only-newconcept" in FLAGS

from core import sync, market
from core import concept
from core import newconcept
from core import watch
from core import log as _log

_log.setup_logging()
_log.get("sync").info("批量同步 %s ~ %s（weekly=%s）", START, END, not NO_WEEKLY)

t0 = time.time()
print(f"[{START} ~ {END}] 开始同步 (weekly={not NO_WEEKLY})", flush=True)


class J:
    def update(self, jid, progress=None, message=None, log=None):
        if message:
            print(f"  [{START}] {(progress*100 if progress else 0):5.1f}% {message}", flush=True)


try:
    if ONLY_WEEKLY:
        # 只补周线（日线分片用 --no-weekly 拉完后，统一收口）。
        # ⚠ 这里传 `changed=START`（整段重算）：本模式的前提就是「区间里的日线刚刚被整段
        #   重拉过」，哪几周变了无从得知 —— 只按「最近一个完整周」重算会**静默漏掉**前面那几百周。
        #   （普通同步那条路不用管：它拿得到日线真正写过的最早一天，会自动只补变动到的周。）
        w = sync.build_weekly(START, END, jid=J(), changed=START)
        print(f"[{START} ~ {END}] 周线: {w} 行  耗时 {time.time()-t0:.1f}s", flush=True)
    elif ONLY_INDEX:
        # 只补指数（回测基准线 + 成分股）
        n = sync.sync_index_daily(START, jid=J())
        m = sync.sync_index_member(START, jid=J())
        print(f"[{START} ~ {END}] 指数日线 {n} 行 / 成分 {m} 行  耗时 {time.time()-t0:.1f}s",
              flush=True)
    elif ONLY_MONEYFLOW:
        # 只补个股资金流向（可与日线分片并行，按日期区间互不重叠）
        r = sync.sync_moneyflow(START, END, jid=J())
        print(f"[{START} ~ {END}] 资金流向: {r}  耗时 {time.time()-t0:.1f}s", flush=True)
    elif ONLY_HOLDERS:
        # 只补「股东户数 / 融资融券 / 股东增减持」三类（可与日线并行，互不重叠）
        r = {}
        for key, fn, band in (("holder_number", sync.sync_holder_number, (0.0, 0.34)),
                              ("margin_detail", sync.sync_margin_detail, (0.34, 0.67)),
                              ("holder_trade", sync.sync_holder_trade, (0.67, 1.0))):
            r[key] = fn(START, END, jid=J(), p0=band[0], p1=band[1])
            print(f"[{START} ~ {END}] {key}: {r[key]}", flush=True)
        print(f"[{START} ~ {END}] 股东/两融数据 耗时 {time.time()-t0:.1f}s", flush=True)
    elif ONLY_FUTURES:
        # 只补期货（主连全历史 + 具体合约近 1 年）。与股票那几块互不相干，
        # 不用传起止日期 —— 「今日期货」页读的就是这两张表。
        r = sync.sync_futures(jid=J())
        print(f"期货同步: {r}  耗时 {time.time()-t0:.1f}s", flush=True)
    elif ONLY_CONCEPT:
        # 只补东财概念（F10 核心题材）。**不走 tushare、不耗额度**，与日期区间无关。
        # ⚠ 耗时 ~8 分钟（全市场 5900 只 × 限速），且东财会限频——别和别的抓取并行。
        r = concept.sync_concepts(jid=J())
        print(f"概念同步: {r}", flush=True)
        print(f"概念覆盖: {concept.coverage()}", flush=True)
    elif ONLY_NEWCONCEPT:
        # 只补东财「新概念速览」（新概念清单 + 概念板块成分变更）。
        # 站外公开报表、**不耗 tushare 额度**；⚠ 已取消本地缓存，**每次都是真抓**
        # （`--force` 现在没作用了，留着只是不破坏老命令）。
        r = newconcept.sync(force="--force" in FLAGS)
        print(f"新概念同步: {r}", flush=True)
        print(f"新概念覆盖: {newconcept.coverage()}", flush=True)
    else:
        if not NO_BASE:
            sync.sync_stock_basic(jid=J())
            sync.sync_trade_cal("2010-01-01", jid=J())
        r = sync.sync_daily_kline(START, END, jid=J(), with_basic=True, force=False)
        print(f"[{START} ~ {END}] 日线: {r}", flush=True)
        if not NO_MONEYFLOW:
            mf = sync.sync_moneyflow(START, END, jid=J())
            print(f"[{START} ~ {END}] 资金流向: {mf}", flush=True)
        if not NO_HOLDERS:
            for key, fn in (("holder_number", sync.sync_holder_number),
                            ("margin_detail", sync.sync_margin_detail),
                            ("holder_trade", sync.sync_holder_trade)):
                print(f"[{START} ~ {END}] {key}: {fn(START, END, jid=J())}", flush=True)
        if not NO_WEEKLY:
            # 只重算「日线这次真正写过的那几周 + 最近一个完整周」（`r["first_day"]`）——
            # 日线是增量的（跳过已同步日期），周线没道理每次都把整段重写一遍。
            w = sync.build_weekly(START, END, jid=J(), changed=r.get("first_day"))
            print(f"[{START} ~ {END}] 周线: {w} 行", flush=True)
        if not NO_FUTURES:
            # 期货：与股票行情**不同源、不同日期区间** —— 主连全历史 + 具体合约近 1 年 +
            # 在市期权，永远只补「最新到哪一天」（增量判据在 sync_fut_daily 里）。
            # ⚠ 只喂「今日期货」页，不进 prepare_data，所以它跑不跑都不影响选股/回测。
            try:
                f = sync.sync_futures(jid=J())
                print(f"[{START} ~ {END}] 期货: {f}", flush=True)
            except Exception as e:              # noqa: BLE001 —— 增强数据，别拖垮同步
                print(f"[{START} ~ {END}] 期货: 失败 {e}", flush=True)
        if not NO_CONCEPT:
            # 东财概念（F10 核心题材）：独立于行情，放最后跑（~8 分钟，走站外接口）。
            # 不进 prepare_data ⇒ 不清回测缓存，也不影响选股/回测口径。
            c = concept.sync_concepts(jid=J())
            print(f"[{START} ~ {END}] 概念: {c}", flush=True)
        if not NO_NEWCONCEPT:
            # 东财「新概念速览」：几十条，秒级。⚠ 已取消本地缓存 ⇒ 每次真抓
            # （总览卡片自己也每次刷新就抓一次，两边都会落到同一批表上，upsert 幂等）。
            try:
                nc = newconcept.sync()
                print(f"[{START} ~ {END}] 新概念: {nc}", flush=True)
            except Exception as e:              # noqa: BLE001 —— 站外，失败不该拖垮同步
                print(f"[{START} ~ {END}] 新概念: 失败 {e}", flush=True)
        # 提醒检测放在**最后**：这时日线已经写最新，均线/布林才判得准。
        # ⚠ 只判最新一两根，不是全历史回放（见 core/watch.py 的说明）。
        try:
            w = watch.check()
            print(f"[{START} ~ {END}] 提醒检测: {w['codes']} 只 / 命中 {w['fired']} 条", flush=True)
        except Exception as e:                  # noqa: BLE001
            print(f"[{START} ~ {END}] 提醒检测: 失败 {e}", flush=True)
        print(f"[{START} ~ {END}] 耗时 {time.time()-t0:.1f}s", flush=True)
except Exception:
    traceback.print_exc()
else:
    # 行情 / 资金流一变，老的 prepare_data 产物（指标、in_pool）就不再可信 ——
    # 主动清空。指数与筹码（股东户数/两融/增减持）不进那份产物，无需清。
    # 概念（东财 F10 题材）同理：它只做展示，还没进任何选股条件 ⇒ 也不必清。
    # 东财「新概念」与提醒检测同理，只读展示、不进 prepare_data。
    # 期货（主连/合约/期权）只在「今日期货」页展示，不进 prepare_data ⇒ 也不必清。
    if not (ONLY_INDEX or ONLY_HOLDERS or ONLY_CONCEPT or ONLY_NEWCONCEPT
            or ONLY_FUTURES):
        from core import btcache
        n = btcache.clear("行情已同步")
        if n:
            print(f"回测数据缓存已清空 {n} 个文件（行情已变动，老产物作废）", flush=True)
