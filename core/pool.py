# -*- coding: utf-8 -*-
"""选股策略（条件股票池）的持久化。

「股票池」不再是一份写死的代码列表，也不是每次回测手敲的临时表达式，
而是一条**有名字、可复用、可管理**的记录：

    小市值          base=all   conds=["total_mv < 500000"]
    昨日大阴        base=all   conds=["ref(pct_chg, 1) <= -5"]
    小市值+活跃     base=all   conds=["total_mv < 1000000", "turnover_rate > 3"]

回测 / 选股时通过 `{"pool_id": 3}` 或字符串 `"pool:3"` 引用它，
加载时再展开成完整条件 —— 改一条策略，所有引用它的回测口径一起变。

## 周期（2026-10-10）

用户需求：「选股策略中也要有日k和周k的选择项，策略名称后面如果是周k则标记周」。

每条策略自带 `timeframe`（`universe.FREQS`，默认 `daily`）：
  · `daily`  —— 条件在**日线**上逐日求值（老行为）；
  · `weekly` —— **整条链按周线跑**（条件里 `close` 就是周收盘、`ma5` 就是 5 周均线）。
引用它就等于连它的周期一起带上 —— 判定只有一处：`strategy.selection_cfg()`
（池子 weekly ⇒ 整个 cfg 走 `to_weekly_view`）。名字后缀「（周K）」的唯一来源
是 `universe.name_tag()`，日K 不标。

## 和 strategy 表的区别

`strategy` 是**交易策略**：选股条件 + 买入触发 + 卖出信号 + 风控，回答"什么时候买、什么时候卖"。
`pool_strategy` 是**选股策略**：只回答"哪些股票有资格进入候选范围"。
两者语义不同，所以分开存 —— 混在一张表里会让"改选股条件"同时影响回测信号，很难解释。
"""
from __future__ import annotations

import json

from core import db, expr, universe

# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def _norm(row: dict | None) -> dict | None:
    """把 DB 行里的 JSON 列解成 Python 对象。"""
    if not row:
        return None
    c = row.get("conds")
    if isinstance(c, (str, bytes)):
        try:
            c = json.loads(c)
        except (ValueError, TypeError):
            c = []
    row["conds"] = [str(x).strip() for x in (c or []) if str(x).strip()]
    f = row.get("filt")
    if isinstance(f, (str, bytes)):
        try:
            f = json.loads(f or "{}")
        except (ValueError, TypeError):
            f = {}
    row["filt"] = universe.norm_filt(f)
    row["builtin"] = bool(row.get("builtin"))
    # 周期归一化（不认的值一律 daily）—— 唯一实现 `universe.norm_freq`
    row["timeframe"] = universe.norm_freq(row.get("timeframe"))
    return row


def list_pools() -> list[dict]:
    rows = db.query(
        "SELECT id, name, note, base, timeframe, conds, filt, builtin, "
        "created_at, updated_at FROM pool_strategy ORDER BY builtin DESC, id ASC"
    )
    return [_norm(r) for r in rows]


def get_pool(pid: int) -> dict | None:
    r = db.query_one("SELECT * FROM pool_strategy WHERE id=%s", (pid,))
    return _norm(r)


def get_pool_by_name(name: str) -> dict | None:
    r = db.query_one("SELECT * FROM pool_strategy WHERE name=%s", (name,))
    return _norm(r)


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------


def validate(name: str, base: str, conds: list[str],
             known_names: set[str] | None = None) -> list[str]:
    """返回错误列表，空表示通过。复用 universe 的条件校验，口径与运行时一致。

    known_names 省略时自动取全量可用字段 —— 否则「未知字段」这类错误会静默漏掉
    （`total_mvv < 5` 语法上完全合法，只有对着字段表才知道拼错了）。
    """
    if known_names is None:
        from core import strategy as st      # 延迟导入：strategy 会 import universe
        known_names = set(st.condition_field_names())
    errs: list[str] = []
    if not (name or "").strip():
        errs.append("选股策略名称不能为空")
    errs += universe.validate(
        {"base": base or universe.UNIVERSE_ALL, "where": conds or []},
        known_names,
    )
    return errs


def save_pool(name: str, note: str, base: str, conds: list[str],
              pid: int | None = None,
              filt: dict | None = None, timeframe: str = universe.DEFAULT_FREQ) -> int:
    name = (name or "").strip()
    base = (base or "").strip() or universe.UNIVERSE_ALL
    conds = [str(x).strip() for x in (conds or []) if str(x).strip()]
    filt = universe.norm_filt(filt)
    # 周期：不认的值归一化成 daily（`norm_freq` 是唯一实现），别让脏值进库
    fq = universe.norm_freq(timeframe)
    payload = (name, note or "", base, fq, json.dumps(conds, ensure_ascii=False),
               json.dumps(filt, ensure_ascii=False))
    if pid:
        db.execute(
            "UPDATE pool_strategy SET name=%s, note=%s, base=%s, timeframe=%s, conds=%s, "
            "filt=%s WHERE id=%s",
            payload + (pid,),
        )
        universe.invalidate_pool(pid)
        return pid
    new_id = db.insert(
        "INSERT INTO pool_strategy (name, note, base, timeframe, conds, filt) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        payload,
    )
    # 展开结果带 3 秒缓存，写完必须失效，否则紧接着的回测会读到改之前的条件
    universe.invalidate_pool(new_id)
    return new_id


def delete_pool(pid: int) -> bool:
    """删除；内置策略删不掉，返回是否真的删了。"""
    n = db.execute("DELETE FROM pool_strategy WHERE id=%s AND builtin=0", (pid,))
    if n:
        universe.invalidate_pool(pid)
    return bool(n)


# ---------------------------------------------------------------------------
# 内置样例 / 预设：两件事，2026-09-29 起**解耦**
#   · 「内置」= 不带删的开箱样例（只有 BUILTIN_POOL_NAMES / BUILTIN_POOLS 里那些）；
#   · `universe.PRESETS` = 通用预设，仍然全部落库，但落成**普通**策略（builtin=0 ⇒ 可改名、
#     可删除）。它真正服务的对象是前端「一键套用预设」下拉（`GET /api/universe/presets`）。
#   ⚠ 以前两者共用一份数据（PRESETS 逐条落成内置），于是「预设」既不可删、又跟下拉绑死。
# ---------------------------------------------------------------------------

# 名字在这个元组里的预设才带 builtin=1。当前为空 —— 现有预设在库里都是可删可改的普通策略。
BUILTIN_POOL_NAMES: tuple = ()

# 不在 PRESETS 里的内置样例（从策略库直接建的那条），单独定义以保证「新库 / 恢复内置」都能造出来。
# ⚠ 这是它的**唯一来源**：内容必须与库里那条内置「首板」一致（回归会逐字比）。
BUILTIN_POOLS: list[dict] = [
    {
        "name": "首板",
        "note": "首板 = 今日涨停、且前两日都没涨停（第一个板）。再叠三道活跃度门槛："
                "总市值 < 500 亿（total_mv 单位万元，5000000 即 500 亿）、收盘价 < 30 元、"
                "换手率 >= 3%；默认剔 ST、科创板与北交所、上市满 60 天。",
        "base": "all",
        "where": [
            "total_mv < 5000000",
            "close < 30",
            "turnover_rate >= 3",
            "limit_up = 1          # 今日涨停",
            "ref(limit_up, 1) = 0  # 昨日不涨停",
            "ref(limit_up, 2) = 0  # 前日不涨停",
        ],
        "filt": {"exclude_bj": True, "exclude_st": True, "min_amount": 0,
                 "exclude_cyb": False, "exclude_kcb": True, "min_list_days": 60},
    },
]


def _install_one(p: dict, flag: int, force: bool) -> int:
    """落一条（同名已存在则跳过，force=True 时连 `builtin` 一起覆盖）。返回新增/覆盖条数。"""
    r = db.query_one("SELECT id FROM pool_strategy WHERE name=%s", (p["name"],))
    # 预设 / 内置样例没写 timeframe 就是 daily（老内容逐字不变）
    payload = (p["note"], p.get("base") or universe.UNIVERSE_ALL,
               universe.norm_freq(p.get("timeframe")),
               json.dumps(list(p["where"]), ensure_ascii=False),
               json.dumps(universe.norm_filt(p.get("filt")), ensure_ascii=False))
    if r:
        if force:
            db.execute(
                "UPDATE pool_strategy SET note=%s, base=%s, timeframe=%s, conds=%s, "
                "filt=%s, builtin=%s WHERE id=%s",
                payload + (flag, r["id"]),
            )
            return 1
        return 0
    db.execute(
        "INSERT INTO pool_strategy (name, note, base, timeframe, conds, filt, "
        "builtin) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (p["name"],) + payload + (flag,),
    )
    return 1


def install_builtins(force: bool = False) -> int:
    """补齐**预设**（不带内置标记）与**内置样例**（带内置标记）。同名已存在则跳过。

    返回值 = 新增 / 覆盖的条数（幂等：重复调、且都已在库里时返回 0）。
    """
    cnt = 0
    for p in universe.PRESETS:
        cnt += _install_one(p, 1 if p["name"] in BUILTIN_POOL_NAMES else 0, force)
    for p in BUILTIN_POOLS:
        cnt += _install_one(p, 1, force)
    return cnt


def ensure_builtins() -> None:
    """启动时补齐内置策略。

    不能只在「一条内置都没有」时装（老库会永远看不到新加的预设）；
    install_builtins 是同名跳过、不 force 不覆盖，所以每次启动跑一遍是安全且幂等的。
    """
    try:
        install_builtins()
    except Exception:  # noqa: BLE001  内置策略只是开箱内容，失败不该拖垮启动
        pass


# ---------------------------------------------------------------------------
# 描述（给页面列表用）
# ---------------------------------------------------------------------------


def describe(p: dict) -> dict:
    """给前端的结构化描述：条件条数、预筛能力、可读标签。"""
    spec = {"pool_id": p["id"], "base": p.get("base"), "where": p.get("conds") or [],
            "filt": universe.norm_filt(p.get("filt"))}
    fq = universe.norm_freq(p.get("timeframe"))
    nm = p.get("name") or ""
    return {
        "id": p["id"],
        "name": nm,
        # 周期：`weekly` 策略整条链按周线跑（见 `strategy.selection_cfg`）
        "timeframe": fq,
        "timeframe_label": universe.FREQS[fq],
        # ⚠ 名字后缀的唯一来源（周K 才带、日K 不带）—— 列表 / 下拉 / 回测记录都取它，
        #   前端别自己拼「+ '周'」，否则同一件事三个地方各写一遍早晚跑偏。
        "name_label": nm + universe.name_tag(fq),
        "note": p.get("note") or "",
        "base": p.get("base") or universe.UNIVERSE_ALL,
        "base_label": universe.base_label(p.get("base")),
        # ⚠ 两个口径分工不同：`conds` 是**给编辑器回显的原文**（带 `#` 注释，别削），
        #    `cond_count` 是**真条件条数**（注释不算）—— 列表上写「N 条条件」就得是
        #    实际跑的那几条，否则跟 label 里的条数、跟真正筛出来的票都对不上。
        "conds": p.get("conds") or [],
        "cond_count": len(universe.parse(spec)["where"]),
        "filt": universe.norm_filt(p.get("filt")),
        "builtin": bool(p.get("builtin")),
        "label": universe.label(spec),
        # ⚠ 判据用 preselectable()，**不是** preselect_note()=="" —— 后者是逐条说明文案，
        #    「部分条件跳过」时也非空，但那种池子确实走预筛（见 universe.preselectable）。
        "preselectable": universe.preselectable(spec),
        "updated_at": str(p.get("updated_at") or ""),
    }


def fields_used(p: dict) -> set[str]:
    """策略条件里用到的字段（页面提示"需要哪些数据"用）。"""
    names: set[str] = set()
    for e in (p.get("conds") or []):
        names |= expr.extract_identifiers(e)
    return names
