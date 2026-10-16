# -*- coding: utf-8 -*-
"""选股策略（条件股票池）持久化与引用的验证。

跑法：
    python verify_pool_strategy.py

覆盖：建表 / 内置落库 / 工具产出一律落成普通策略 / CRUD / 按 id 引用展开 / 预筛等价 /
真实回测 / 删除降级。
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import backtest as bt, db, market, pool as pool_mod, screener  # noqa: E402
from core import strategy as st, universe  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"  — {detail}" if detail else ""))


print("=" * 78)
print("选股策略（条件股票池）验证")
print("=" * 78)

# ---------------------------------------------------------------- 1. 建表
print("\n[1] 表结构与内置策略")
db.init_schema()
cols = db.query("SHOW COLUMNS FROM pool_strategy")
names = {str(list(c.values())[0]).lower() for c in cols}
need = {"id", "name", "note", "base", "conds", "filt", "builtin"}
check("pool_strategy 表已建立且字段齐全", need <= names,
      f"缺少 {need - names}" if need - names else f"{len(names)} 列")
# 「掉出池子即卖」彻底移除（2026-10-10）：这一列必须不在表里 ——
# 留一列无人读的字段，等于库里和代码两套口径，下一个人照旧列写查询就是坑。
check("pool_strategy 已无 pool_exit 列（该能力彻底移除）",
      "pool_exit" not in names, f"多出来的列 {names - need if names - need else '无'}")

pool_mod.install_builtins()
built = pool_mod.list_pools()
# 2026-09-29 起「内置」与「预设」**解耦**（用户要求「保留第一条，其余取消内置」）：
#   · 内置 = 不可删的开箱样例，只有 `pool.BUILTIN_POOLS` 里那些（当前 1 条「首板」）；
#   · `universe.PRESETS` 仍然全部落库，但落成**普通**策略（builtin=0 ⇒ 可改名、可删），
#     它真正服务的对象是前端「一键套用预设」下拉。
# 所以不变量从「每条预设都带内置标记」改成「预设都落库 + 内置恰好等于白名单」。
_binames = [p["name"] for p in pool_mod.BUILTIN_POOLS]
_bi = [p["name"] for p in built if p["builtin"]]
check("内置选股策略恰好是白名单那些（当前 1 条「首板」）", _bi == _binames,
      f"内置 {_bi} / 白名单 {_binames}")
_missing = [p["name"] for p in universe.PRESETS
            if not any(r["name"] == p["name"] for r in built)]
_pnames = {p["name"] for p in universe.PRESETS}
check("预设仍然全部落库（幂等）", not _missing,
      ("未落库：" + "、".join(_missing)) if _missing else
      f"预设 {len(universe.PRESETS)} 条都在")
check("预设行不再带内置标记（带了就又变回「删不掉」，与本次口径相悖）",
      not [p["name"] for p in built if p["builtin"] and p["name"] in _pnames],
      f"预设 {len(universe.PRESETS)} 条，带标记的 "
      f"{[p['name'] for p in built if p['builtin'] and p['name'] in _pnames]}")

# 预设行现在都是**普通**策略，用户在界面上改得动。所以「与 PRESETS 逐字一致」这条断言只对
# **没被改动过**的行成立，判据是 updated_at == created_at。
# 拿死字符串去比一条可编辑的行，用户一调参就假红 —— 反而盖住真问题。
pristine, edited, bad = 0, [], []
for p in universe.PRESETS:
    row = next((r for r in built if r["name"] == p["name"]), None)
    if row is None:
        bad.append(f"「{p['name']}」未落库")
        continue
    if str(row.get("updated_at")) != str(row.get("created_at")):
        edited.append(p["name"])
        continue
    pristine += 1
    if list(row["conds"]) != list(p["where"]) or row["base"] != p["base"]:
        bad.append(f"「{p['name']}」{row['conds']} != {p['where']}")
check("预设行落库内容与 PRESETS 逐字一致（仅未被改动的）", not bad,
      str(bad) if bad else
      f"未改动 {pristine} 条 / 界面改过 {len(edited)} 条"
      + (f"（{'、'.join(edited)}）" if edited else ""))

small = next((p for p in built if p["name"] == "小市值"), None)
check("预设「小市值」已落库、但**不再**带内置标记（可改名可删）",
      small is not None and not small["builtin"] and bool(small["conds"])
      and all(isinstance(c, str) and c.strip() for c in small["conds"]),
      (str(small["conds"]) + f" / builtin={small['builtin']}") if small else "未找到")

# 内置那条「首板」的内容必须与代码定义（`pool.BUILTIN_POOLS`，唯一来源）逐字一致 ——
# 它是不可删行，一旦库里内容漂了，用户就没法通过界面纠回来（只能 force 恢复）。
_ba = next((p for p in built if p["name"] == "首板"), None)
_bp = pool_mod.BUILTIN_POOLS[0]
check("内置「首板」的条件与代码定义逐字一致、且带内置标记",
      _ba is not None and _ba["builtin"]
      and list(_ba["conds"]) == list(_bp["where"])
      and _ba["base"] == _bp["base"],
      ("%s / builtin=%s" % (_ba["conds"], _ba["builtin"])) if _ba else "未找到")

# --- 工具产出的策略也必须是**普通**策略（builtin=0） --------------------------
# 用户 2026-09-29 原话：「取消内置 = 就得变成普通策略」。源码里改完还不够 —— 只要还有别的
# 入口把产出标成 builtin=1（`tools/auto_strategies.py --install` / `tools/summarize_auto.py`
# 就是），跑一次内置就又长回来，本次收窄等于白做。
# 判据 = **真跑一遍**那些工具的写库函数（合成结果、跑完即删），比 grep 源码可信。
import contextlib  # noqa: E402
import importlib.util  # noqa: E402
import io  # noqa: E402

_here = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "auto_strategies_chk", os.path.join(_here, "tools", "auto_strategies.py"))
_auto = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_auto)
_tag = f"_T_VPS_{int(time.time())}"
_pool_name = _tag + " · 选股"
with contextlib.redirect_stdout(io.StringIO()):          # 工具自己会打印一大段
    _auto.install_results([{
        "name": _tag, "note": "回归用（跑完即删）", "universe": universe.UNIVERSE_ALL,
        "range": "2024-01-01~2025-01-01", "pool": ["amount > 0"],
        "config": {"buy_trigger": ["close > pre_close"],
                   "sell_signal": ["close < pre_close"]},
        "metrics": {"total_return": 1.0, "trade_count": 5, "win_rate": 50.0,
                    "avg_hold_days": 3.0},
    }], min_trades=1)
_pr = db.query_one("SELECT id, builtin FROM pool_strategy WHERE name=%s", (_pool_name,))
_sr = db.query_one("SELECT id, builtin FROM strategy WHERE name=%s", (_tag,))
check("工具产出的选股策略落成普通策略（builtin=0）",
      _pr is not None and _pr["builtin"] == 0,
      f"builtin={_pr and _pr['builtin']}")
check("工具产出的买卖策略落成普通策略（builtin=0）",
      _sr is not None and _sr["builtin"] == 0,
      f"builtin={_sr and _sr['builtin']}")
_del_p = bool(_pr) and pool_mod.delete_pool(_pr["id"]) is True
_del_s = bool(_sr) and db.execute("DELETE FROM strategy WHERE id=%s AND builtin=0",
                                  (_sr["id"],)) == 1
check("工具产出的选股策略真删得掉（内置才删不掉）", _del_p, f"delete_pool={_del_p}")
check("工具产出的买卖策略真删得掉", _del_s, f"DELETE ... AND builtin=0 影响 1 行={_del_s}")
db.execute("DELETE FROM pool_strategy WHERE name LIKE %s", (_tag + "%",))
db.execute("DELETE FROM strategy WHERE name LIKE %s", (_tag + "%",))
check("工具回归用的临时行已清理干净",
      not db.query("SELECT id FROM pool_strategy WHERE name LIKE %s", (_tag + "%",))
      and not db.query("SELECT id FROM strategy WHERE name LIKE %s", (_tag + "%",)))

# `tools/summarize_auto.py` 的入库块是行内写的（抽不出函数，跑一遍要全量回测），只能用源码
# 文本当绊线：它一旦又写出 `builtin=1`，跑一次多口径汇总，内置就又长回来。
for _f in ("auto_strategies.py", "summarize_auto.py"):
    _src = open(os.path.join(_here, "tools", _f), encoding="utf-8").read()
    check(f"tools/{_f} 不再把产出标成内置（源码里无 builtin=1）",
          "builtin=1" not in _src,
          "找到了 builtin=1 的写法" if "builtin=1" in _src else "")

# ---------------------------------------------------------------- 2. CRUD
print("\n[2] 增删改查")
uniq = f"测试池-{int(time.time())}"
new_id = pool_mod.save_pool(uniq, "脚本建的临时池", "all",
                            ["total_mv < 800000", "turnover_rate > 2"])
check("新建成功并返回 id", bool(new_id), f"id={new_id}")

got = pool_mod.get_pool(new_id)
check("读回内容一致",
      got and got["name"] == uniq and got["conds"] == ["total_mv < 800000", "turnover_rate > 2"]
      and got["base"] == "all",
      json.dumps(got["conds"], ensure_ascii=False) if got else "None")

pool_mod.save_pool(uniq, "改了说明", "000300.SH", ["total_mv < 900000"], pid=new_id)
got2 = pool_mod.get_pool(new_id)
check("更新生效", got2 and got2["base"] == "000300.SH"
      and got2["conds"] == ["total_mv < 900000"],
      f"base={got2['base']}" if got2 else "None")

errs = pool_mod.validate("坏池", "all", ["total_mvv < 5"])
check("错误字段被校验拦住", bool(errs), errs[0] if errs else "没报错（不应发生）")
errs_empty = pool_mod.validate("", "all", ["total_mv < 5"])
check("空名称被校验拦住", any("名称" in e for e in errs_empty), str(errs_empty))
check("合法条件校验通过", pool_mod.validate("好池", "all", ["total_mv < 500000"]) == [], "")

# ⚠ 拿**内置那条**（首板）试删除，别拿「小市值」—— 2026-09-29 起预设是普通策略、删得掉，
# 用它试删除会真的把记录删掉（第一版就是这么踩的），后面按 id 引用的断言全跟着崩。
check("内置策略删不掉（拿内置那条试）",
      pool_mod.delete_pool(_ba["id"]) is False and
      pool_mod.get_pool(_ba["id"]) is not None, "")
check("自建策略可删除", pool_mod.delete_pool(new_id) is True and
      pool_mod.get_pool(new_id) is None, "")

# ------------------------------------------------- 3. 按 id 引用与展开
print("\n[3] 按 id 引用（universe.expand）")
pid = small["id"]
# 期望值取自**库里的那一行**（走 get_pool，独立于 universe.expand 的 3 秒缓存路径），
# 而不是预设字面量 —— 这样内置池被界面调过参也照样成立，断言的是真契约：
# 「引用要忠实透传库内容」。
db_small = pool_mod.get_pool(pid)
want_where, want_base = list(db_small["conds"]), db_small["base"]
for spec, tag in [({"pool_id": pid}, "对象形式"),
                  (f"pool:{pid}", "字符串形式")]:
    s = universe.parse(spec)
    check(f"{tag} 能识别为条件池", universe.is_cond(spec) and s["kind"] == "cond", str(s["kind"]))
    check(f"{tag} 展开出正确条件", s["where"] == want_where and s["base"] == want_base,
          str(s["where"]))
    check(f"{tag} 带上策略名", s["pool_name"] == "小市值", str(s["pool_name"]))

lab = universe.label({"pool_id": pid})
check("标签显示策略名而非裸表达式", lab == "全部A股 · 小市值" or "小市值" in lab, lab)

d = universe.describe({"pool_id": pid})
check("describe 回传 pool_id", d["pool_id"] == pid and d["label"] == lab, json.dumps(d["label"], ensure_ascii=False))

exp = universe.expand({"pool_id": pid})
check("expand 把引用展开成实体条件（where 具体化 + 保留策略名）",
      exp["where"] == want_where and exp["base"] == want_base
      and exp["pool_name"] == "小市值", str(exp))

# 删除后引用：应降级而不是崩
tmp_id = pool_mod.save_pool(f"待删-{int(time.time())}", "", "all", ["total_mv < 100000"])
pool_mod.delete_pool(tmp_id)
s_del = universe.parse({"pool_id": tmp_id})
check("引用已删除策略时降级不崩溃",
      s_del["kind"] == "cond" and s_del["where"] == [] and s_del["pool_name"] is None,
      str(s_del))
check("降级标签带「已删除」", "已删除" in universe.label({"pool_id": tmp_id}),
      universe.label({"pool_id": tmp_id}))

# ------------------------------------------------- 4. 预筛等价性
print("\n[4] 按 id 引用时的预筛正确性")
START, END = "2023-06-01", "2023-06-30"
pool_pid = next(p for p in pool_mod.list_pools() if p["name"] == "小市值")["id"]

codes_all = market.resolve_universe("all")
codes_ref = market.resolve_universe({"pool_id": pool_pid}, start=START, end=END)

# 这条池子是**用户在界面上可编辑**的（内置行只拦删除）。它可能已经含筹码/两融条件
# （如 hold_num < 50000）—— 那些字段 SQL 里没有，预筛会正确退回全量。所以期望值
# 必须按「这条池子当前能不能预筛」分叉，否则用户一加筹码条件这里就假红。
#
# ⚠ 判据**不能**用 `preselect_note()` 是否为空：2026-09-19 起预筛改成「逐条翻译、
#   翻不了就跳过」，说明也改成**逐条**报 —— 只要有一条被跳过就有文案
#   （"N 条用于 SQL 预筛、M 条（指标/复权价/筹码字段）跳过"），但那**不代表**整份退回全量。
#   唯一判据是 `preselect_codes()` 是否返回 None。
note_ref = universe.preselect_note({"pool_id": pool_pid})
# 判据唯一来源：能不能预筛。**与区间无关** —— 只跟「有没有可翻的条件」有关
# （单日会额外翻 ref，区间丢 ref，但「一条都翻不了 → None」两边一致）。
pid_spec = universe.expand({"pool_id": pool_pid})
presel_ok = universe.preselect_codes(pid_spec, START, END) is not None
if not presel_ok:
    check("含不可预筛字段（筹码等）时如实退回全量，且说明里讲清楚",
          len(codes_ref) == len(codes_all) and "全量" in note_ref,
          f"全市场 {len(codes_all)} → {len(codes_ref)}；说明「{note_ref[:40]}」")
else:
    check("按 id 引用时预筛生效（子集且非空）",
          0 < len(codes_ref) < len(codes_all),
          f"全市场 {len(codes_all)} → 预筛 {len(codes_ref)}"
          + (f"；说明「{note_ref[:24]}」" if note_ref else ""))

# 逐日门控集合必须与预筛集合一致 —— 两端用同一批票、同一段区间，否则不可比
sub = codes_all[:1200]
cfg_pool = st.merge_config({
    "entry": ["close > 0"],
    "_pool": universe.expand({"pool_id": pool_pid}),
})
panel = market.load_panel(sub, START, END, adjust="qfq",
                          only=st.required_daily_fields(cfg_pool))
ever = {c for c, df in panel.items() if st.compute_pool(df, cfg_pool).any()}
presel = set(codes_ref) & set(sub)
check("逐日入池的股票全部被预筛留下（不漏票）",
      ever <= presel,
      f"逐日入池 {len(ever)} / 预筛命中 {len(presel)} / 漏 {len(ever - presel)}")
del panel

# ------------------------------------------------- 5. 真实回测
print("\n[5] 用已保存的选股策略跑回测")
cfg0 = st.merge_config({
    "entry": ["close <= boll_lower * 1.02"],
    "buy_trigger": ["open > ref(close, 1)"],
    "sell_signal": ["open < ref(close, 1)"],
    "risk": {"max_hold_days": 10},
})
params = {"init_cash": 1000000, "max_positions": 5, "position_pct": 20}
codes300 = market.resolve_universe("000300.SH")

data0 = bt.prepare_data(codes300, "2023-01-03", "2023-06-30", cfg0, batch=250)
res0 = bt.run_backtest(data0, cfg0, "2023-01-03", "2023-06-30", params)
n0 = res0["metrics"].get("trade_count", 0)

cfg1 = {**cfg0, "_pool": universe.expand({"pool_id": pool_pid})}
codes1 = market.resolve_universe(universe.expand({"pool_id": pool_pid}),
                                 start="2023-01-03", end="2023-06-30")
data1 = bt.prepare_data(codes1, "2023-01-03", "2023-06-30", cfg1, batch=250)
res1 = bt.run_backtest(data1, cfg1, "2023-01-03", "2023-06-30", params)
n1 = res1["metrics"].get("trade_count", 0)
check("引用选股策略后回测正常出交易", n1 > 0, f"无池 {n0} 笔 → 小市值池 {n1} 笔")
# ⚠ 这里曾经也是 `if note_ref:` 分叉 —— 与 [4] 同一个坑。`presel_ok` 才是判据：
#   池子含筹码条件时「部分跳过、部分照样预筛」，说明文案非空，但它**确实收窄了加载量**。
if presel_ok:
    check("可预筛的池子：预筛后加载量收窄，回测照常出交易",
          n1 > 0 and len(codes1) < len(codes_all),
          f"无池 {n0} 笔 → 小市值池 {n1} 笔 / 加载 {len(codes1)} 只（全市场 {len(codes_all)}；说明「{note_ref[:22]}」）")
else:
    # 不可预筛 → 池子由逐日门控决定，全市场入参是对的；断言「回测照样跑出交易」即可
    check("含筹码条件的池子：全量加载后回测照常出交易（结果不受预筛退化影响）",
          n1 > 0 and len(codes1) == len(codes_all),
          f"无池 {n0} 笔 → 小市值池 {n1} 笔 / {len(codes1)} 只")

# 「基础池 × 条件」两层都要生效：临时建一条 base=000300.SH 的策略
tmp_cap = pool_mod.save_pool(f"沪深300小市值-{int(time.time())}", "",
                             "000300.SH", ["total_mv < 800000"])
codes_cap = market.resolve_universe({"pool_id": tmp_cap},
                                    start="2023-01-03", end="2023-06-30")
check("基础池生效：引用后是 300 的子集",
      set(codes_cap) <= set(codes300) and len(codes_cap) < len(codes300),
      f"沪深300 {len(codes300)} → {len(codes_cap)} 只")
check("基础池标签写入描述",
      universe.base_label("000300.SH") in universe.label({"pool_id": tmp_cap}),
      universe.label({"pool_id": tmp_cap}))
pool_mod.delete_pool(tmp_cap)

# 与「就地写同样条件」结果必须一致（证明引用只是语法糖）。条件用上面那条库里的真值，
# 别写死成预设 —— 否则用户调过参之后，两侧比的根本不是同一组条件（199 笔 vs 193 笔）。
cfg2 = {**cfg0, "_pool": universe.parse({"base": want_base, "where": list(want_where)})}
data2 = bt.prepare_data(codes1, "2023-01-03", "2023-06-30", cfg2, batch=250)
res2 = bt.run_backtest(data2, cfg2, "2023-01-03", "2023-06-30", params)
n2 = res2["metrics"].get("trade_count", 0)
check("引用 == 就地写同条件（笔数一致）", n1 == n2, f"引用 {n1} 笔 / 就地 {n2} 笔")

# ------------------------------------------------- 6. 选股
print("\n[6] 用已保存的选股策略跑选股")
r = screener.screen(cfg0, {"pool_id": pool_pid}, "2023-06-30", limit=50)
check("选股接口接受按 id 引用", not r.get("error"), str(r.get("error") or ""))
check("选股返回了池子描述", "小市值" in str(r.get("pool") or ""), str(r.get("pool"))[:60])

# ------------------------------------------------- 7. 出池即卖已彻底移除
print("\n[7] pool_exit（掉出池子即卖）已彻底移除")
# 2026-10-10 用户要求：「选股策略中的掉出池子即卖出这个不需要了吧，这个属于买卖策略，
# 选股策略只管选股」。彻底移除后，这里钉**反向**不变量 —— 谁把它加回来就红。
check("PRESETS 里不再有带卖出语义的样例", 
      not [p for p in universe.PRESETS if p.get("pool_exit")],
      f"预设 {len(universe.PRESETS)} 条：{[p['name'] for p in universe.PRESETS]}")
check("预设清单里不再有「即卖」字样的样例",
      not [p for p in universe.PRESETS if "即卖" in p["name"]],
      str([p["name"] for p in universe.PRESETS if "即卖" in p["name"]]))
_p = universe.parse({"base": "all", "where": ["close >= boll_upper"], "pool_exit": True})
check("parse 不再认 pool_exit（外面多传也丢掉）", "pool_exit" not in _p, str(sorted(_p)))
check("describe 不再下发 pool_exit", "pool_exit" not in universe.describe(
      {"base": "all", "where": ["close >= boll_upper"]}),
      str(sorted(universe.describe({"base": "all", "where": ["close >= boll_upper"]}))))
check("pool.describe 不再下发 pool_exit",
      "pool_exit" not in pool_mod.describe(pool_mod.get_pool(_ba["id"])),
      str(sorted(pool_mod.describe(pool_mod.get_pool(_ba["id"])))))
_boll = next(p for p in pool_mod.list_pools() if p["name"] == "BOLL 上轨")
cfg3 = {**cfg0, "_pool": universe.expand({"pool_id": _boll["id"]})}
codes3 = market.resolve_universe(universe.expand({"pool_id": _boll["id"]}),
                                 start="2023-01-03", end="2023-06-30")
data3 = bt.prepare_data(codes3, "2023-01-03", "2023-06-30", cfg3, batch=250)
res3 = bt.run_backtest(data3, cfg3, "2023-01-03", "2023-06-30", params)
reasons = [t.get("sell_reason") for t in res3["trades"]]
check("回测不再产生 pool_out 离场", "pool_out" not in reasons,
      f"共 {len(reasons)} 笔，离场分布 {sorted(set(reasons))}")

print("\n" + "=" * 78)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
for f in FAIL:
    print("  未通过:", f)
print("=" * 78)
sys.exit(1 if FAIL else 0)
