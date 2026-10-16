# -*- coding: utf-8 -*-
"""安全的条件表达式引擎。

策略条件用一段类似公式的字符串描述，例如::

    close <= boll_lower * 1.02
    open > ref(close, 1)
    cross_above(ma5, ma20) and vol > ref(vol, 1) * 1.5
    close / ref(close, 1) - 1 < -0.05

支持：
  * 四则运算 + - * / // % **、比较、and/or/not、括号
  * 行情字段：open/high/low/close/pre_close/pct_chg/vol/amount/turnover_rate 等
  * 技术指标：ma/ema/boll/macd/kdj/rsi/atr/cci/wr/obv/...
  * 函数：ref, shift, cross_above, cross_below, ma, ema, sum, mean, std,
          highest, lowest, count, exist, every, barslast, iif, nz, pct,
          abs, max, min, sqrt, log, exp, sign, between

求值使用受限 AST 解释器，不调用 eval/exec，不允许属性访问与 import。

注释：`#` 之后（含 `#`）都算注释，行首 `#` 则整行都是注释 —— 例如::

    total_mv > 300000        # 市值大于 30 亿（单位万元）
    # 下面这条是放量确认
    vol > vma5 * 1.5

注释**不是**条件：`strip_comment()` / `split_conds()` / `clean_conds()` 是唯一判据，
条件从「用户写的文本」变成「要执行的表达式」的地方都必须先过它们。**存库与回显保留
注释原文**（那是用户写给自己的批注），只在归一化/求值这一层剥掉。
"""
from __future__ import annotations

import ast
import math
import re
from typing import Any, Callable

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _as_array(x: Any) -> np.ndarray:
    if isinstance(x, np.ndarray):
        return x
    if isinstance(x, pd.Series):
        return x.to_numpy()
    return np.asarray(x, dtype=float)


def _as_series(x: Any, index=None) -> pd.Series:
    if isinstance(x, pd.Series):
        return x
    arr = _as_array(x)
    if arr.ndim == 0:
        return pd.Series(np.full(len(index) if index is not None else 0, arr))
    return pd.Series(arr, index=index)


def _bool(x: Any) -> np.ndarray:
    """转成布尔数组，NaN 视为 False。"""
    a = _as_array(x)
    if a.dtype == bool:
        return a
    a = a.astype(float, copy=False)
    with np.errstate(invalid="ignore"):
        return np.where(np.isnan(a), False, a != 0)


def _num(x: Any) -> np.ndarray:
    return _as_array(x).astype(float, copy=False)


def _lens(*vals) -> int:
    for v in vals:
        a = _as_array(v)
        if a.ndim > 0:
            return len(a)
    return 0


# ---------------------------------------------------------------------------
# 指标与函数实现（全部向量化，输入 numpy / pandas）
# ---------------------------------------------------------------------------

def _s(x, ref=None) -> pd.Series:
    a = _as_array(x)
    if ref is not None and a.ndim == 0:
        return pd.Series(np.full(len(ref), float(a)), index=getattr(ref, "index", None))
    if isinstance(x, pd.Series):
        return x
    return pd.Series(a)


def f_ref(x, n=1):
    return _s(x).shift(int(n))


def f_cross_above(a, b):
    a, b = _num(a), _num(b)
    L = max(len(a), len(b))
    a = np.broadcast_to(a, (L,)) if a.ndim == 1 else np.full(L, a)
    b = np.broadcast_to(b, (L,)) if b.ndim == 1 else np.full(L, b)
    prev_a, prev_b = np.roll(a, 1), np.roll(b, 1)
    prev_a[0] = np.nan
    prev_b[0] = np.nan
    with np.errstate(invalid="ignore"):
        return (a > b) & (prev_a <= prev_b)


def f_cross_below(a, b):
    a, b = _num(a), _num(b)
    L = max(len(a), len(b))
    a = np.broadcast_to(a, (L,)) if a.ndim == 1 else np.full(L, a)
    b = np.broadcast_to(b, (L,)) if b.ndim == 1 else np.full(L, b)
    prev_a, prev_b = np.roll(a, 1), np.roll(b, 1)
    prev_a[0] = np.nan
    prev_b[0] = np.nan
    with np.errstate(invalid="ignore"):
        return (a < b) & (prev_a >= prev_b)


def f_ma(x, n=5):
    return _s(x).rolling(int(n), min_periods=1).mean()


def f_ema(x, n=12):
    return _s(x).ewm(span=int(n), adjust=False).mean()


def f_sum(x, n=5):
    return _s(x).rolling(int(n), min_periods=1).sum()


def f_mean(x, n=5):
    return _s(x).rolling(int(n), min_periods=1).mean()


def f_std(x, n=20):
    return _s(x).rolling(int(n), min_periods=2).std()


def f_highest(x, n=20):
    return _s(x).rolling(int(n), min_periods=1).max()


def f_lowest(x, n=20):
    return _s(x).rolling(int(n), min_periods=1).min()


def f_count(cond, n=5):
    return _s(_bool(cond).astype(float)).rolling(int(n), min_periods=1).sum()


def f_exist(cond, n=5):
    return _s(_bool(cond).astype(float)).rolling(int(n), min_periods=1).max().fillna(0) > 0


def f_every(cond, n=5):
    return _s(_bool(cond).astype(float)).rolling(int(n), min_periods=1).min().fillna(0) > 0


def f_barslast(cond):
    c = _bool(cond)
    idx = np.arange(len(c))
    last = np.maximum.accumulate(np.where(c, idx, -1))
    return np.where(last < 0, np.nan, idx - last).astype(float)


def f_turn_up(x):
    """向上转折：**昨降今升** —— `x[i] > x[i-1]` 且 `x[i-1] <= x[i-2]`。

    这是用户口径的「低位转折」判据（2026-10-09「MACD与KDJ共振」策略时定下）：
    前一根还在下行（或持平）、这一根转为上行 ⇒ 拐点刚出现。

    ⚠ 只有「今 > 昨」是不够的（那叫上涨，不叫转折）：必须**昨不涨**才算拐点，
      否则在一段持续上涨里每根都算「转折」，信号会烂成一片。
    ⚠ 与前两根比较（用到 `x[i-1] <= x[i-2]`）⇒ 前两根必须是有效值；
      开头两根 `np.roll` 会绕回尾部，所以显式把前 2 位判为 False，别让首尾串成环。
    ⚠ 用 `<=` 而不是 `<`：港股/A股里常有「横盘后再涨」的形态，
      若要求严格 `<` 会漏掉「昨平今升」这一档。真正要排除的是「昨也在涨」。"""
    a = _num(x)
    if a.ndim == 0:
        return np.zeros(0, dtype=bool)
    if len(a) < 3:
        return np.zeros(len(a), dtype=bool)
    prev = np.roll(a, 1)      # x[i-1]
    prev2 = np.roll(a, 2)     # x[i-2]
    with np.errstate(invalid="ignore"):
        r = (a > prev) & (prev <= prev2)
    r[:2] = False
    return r


def f_turn_down(x):
    """向下转折：**昨升今降** —— `x[i] < x[i-1]` 且 `x[i-1] >= x[i-2]`。

    与 `f_turn_up` 完全对称（用户口径的「高位转折」）。⚠ 两处实现必须一起改：
    判据只有这一对函数，别在策略字符串里手写 `ref` 拼。"""
    a = _num(x)
    if a.ndim == 0:
        return np.zeros(0, dtype=bool)
    if len(a) < 3:
        return np.zeros(len(a), dtype=bool)
    prev = np.roll(a, 1)
    prev2 = np.roll(a, 2)
    with np.errstate(invalid="ignore"):
        r = (a < prev) & (prev >= prev2)
    r[:2] = False
    return r


def f_iif(cond, a, b):
    c = _bool(cond)
    av = np.asarray(a, dtype=float)
    bv = np.asarray(b, dtype=float)
    L = max(len(c), av.size, bv.size)
    if av.ndim == 0:
        av = np.full(L, float(av))
    if bv.ndim == 0:
        bv = np.full(L, float(bv))
    c = np.broadcast_to(c, (L,))
    return np.where(c, av, bv)


def f_nz(x, v=0.0):
    a = _num(x)
    return np.where(np.isnan(a), float(v), a)


def f_pct(x, n=1):
    a = _s(x)
    return a / a.shift(int(n)) - 1


def f_hhv(x, n=20):
    return f_highest(x, n)


def f_llv(x, n=20):
    return f_lowest(x, n)


def f_between(x, a, b):
    x, a, b = _num(x), _num(a), _num(b)
    with np.errstate(invalid="ignore"):
        return (x >= a) & (x <= b)


def f_abs(x):
    return np.abs(_num(x))


def f_max(a, b):
    return np.maximum(_num(a), _num(b))


def f_min(a, b):
    return np.minimum(_num(a), _num(b))


def f_sqrt(x):
    with np.errstate(invalid="ignore"):
        return np.sqrt(_num(x))


def f_log(x):
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.log(np.where(_num(x) > 0, _num(x), np.nan))


def f_exp(x):
    return np.exp(_num(x))


def f_sign(x):
    return np.sign(_num(x))


def f_round(x, n=2):
    return np.round(_num(x), int(n))


def f_roc(x, n=1):
    """N 日涨幅（%）"""
    a = _s(x)
    return (a / a.shift(int(n)) - 1) * 100


FUNCTIONS: dict[str, Callable] = {
    "ref": f_ref,
    "shift": f_ref,
    "cross_above": f_cross_above,
    "cross": f_cross_above,
    "cross_below": f_cross_below,
    "ma": f_ma,
    "sma": f_ma,
    "ema": f_ema,
    "sum": f_sum,
    "mean": f_mean,
    "avg": f_mean,
    "std": f_std,
    "highest": f_highest,
    "hhv": f_hhv,
    "lowest": f_lowest,
    "llv": f_llv,
    "count": f_count,
    "exist": f_exist,
    "every": f_every,
    "barslast": f_barslast,
    "turn_up": f_turn_up,
    "turn_down": f_turn_down,
    "iif": f_iif,
    "if_else": f_iif,
    "nz": f_nz,
    "pct": f_pct,
    "between": f_between,
    "abs": f_abs,
    "max": f_max,
    "min": f_min,
    "sqrt": f_sqrt,
    "log": f_log,
    "exp": f_exp,
    "sign": f_sign,
    "round": f_round,
    "roc": f_roc,
}

# 供 UI 提示
FUNC_HELP = [
    ("ref(x, n)", "n 周期前的值，如 ref(close, 1) 为昨收"),
    ("cross_above(a, b)", "a 上穿 b（金叉）"),
    ("cross_below(a, b)", "a 下穿 b（死叉）"),
    ("ma(x, n) / ema(x, n)", "简单/指数移动平均"),
    ("highest(x, n) / lowest(x, n)", "n 周期内最高/最低值"),
    ("sum/mean/std/count(x, n)", "n 周期滚动统计"),
    ("exist(cond, n)", "近 n 周期内存在满足条件"),
    ("every(cond, n)", "近 n 周期内全部满足"),
    ("barslast(cond)", "距上次条件成立经过的周期数"),
    ("turn_up(x)", "向上转折：昨降今升（x 由下行转为上行）"),
    ("turn_down(x)", "向下转折：昨升今降（x 由上行转为下行）"),
    ("pct(x, n)", "x 相对 n 周期前的涨跌幅（小数）"),
    ("roc(x, n)", "x 相对 n 周期前的涨跌幅（%）"),
    ("iif(cond, a, b)", "三元表达式"),
    ("nz(x, v)", "把 NaN 替换成 v"),
    ("between(x, a, b)", "x 是否落在 [a, b] 区间"),
    ("abs/min/max/sqrt/log/exp/sign/round", "基础数学函数"),
]

IDENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*(?=\()|\b([A-Za-z_][A-Za-z0-9_]*)\b")

# 单个 = 当成相等比较。通达信/同花顺里写 `is_down = 1`，直接喂给 ast 会报语法错误，
# 而用户很难意识到"少写一个等号"。这里统一归一化，注意别误伤 <= >= != ==。
_EQ_RE = re.compile(r"(?<![<>=!])=(?!=)")

# `#` 起注释（跟 Python 一样，一直到行尾）。注释**不是**条件的一部分，用户拿它给条件
# 做批注：`total_mv > 300000  # 市值大于 30 亿`。行首 `#` ⇒ 整行都是注释。
_COMMENT_RE = re.compile(r"#[^\n]*")


def strip_comment(line) -> str:
    """剥掉一条文本里的注释，返回**真正要执行**的那部分（可能为空串 = 整行是注释）。

    `#` 之后（含 `#`）都算注释，所以 `#` 之后写什么都不会被拿来求值。只此一处判据：
    「用户写的文本」→「要执行的表达式」的地方全走它，别各处再写一遍正则。

    ⚠ 全角 `＃` 一并认（中文输入法切到全角标点时打出来的就是这个）：不认的话它会被当成
    表达式里的一个字符 → 用户收到一句莫名其妙的「语法错误」。
    """
    return _COMMENT_RE.sub("", str(line or "").replace("＃", "#")).strip()


def split_conds(text) -> list[str]:
    """把「一段条件文本」拆成**真正要执行**的条件列表 —— 唯一口径。

    分隔符：换行 / 中英文分号；`#` 起注释（行内或整行）；空行与纯注释行丢弃。
    传列表 / 元组时等价于 `clean_conds`（接口与前端两种形态都收）。
    """
    if isinstance(text, (list, tuple, set)):
        return clean_conds(text)
    s = str(text or "").replace("；", "\n").replace(";", "\n")
    return [t for t in (strip_comment(x) for x in s.split("\n")) if t]


def clean_conds(seq) -> list[str]:
    """条件列表归一化：逐条剥注释、丢掉空条目（**纯注释条目也丢** —— 它不是条件）。"""
    if isinstance(seq, str):
        return split_conds(seq)
    return [t for t in (strip_comment(x) for x in (seq or [])) if t]


def is_active(cond) -> bool:
    """这条文本是不是一条真正的条件（空 / 纯注释 ⇒ False）。"""
    return bool(strip_comment(cond))


def normalize(expr: str) -> str:
    """表达式预处理：剥 `#` 注释、全角符号转半角、单个 = 视作 ==。

    ⚠ 注释按**行**剥：注释只吃到行尾，跨行的表达式不会被前一行注释连坐。
    """
    expr = "\n".join(strip_comment(x) for x in str(expr or "").splitlines()).strip()
    if not expr:
        return expr
    expr = expr.replace("，", ",").replace("（", "(").replace("）", ")")
    expr = _EQ_RE.sub("==", expr)
    return expr


def extract_identifiers(expr: str) -> set[str]:
    """提取表达式里用到的变量名（排除函数名与常量）。"""
    out = set()
    try:
        tree = ast.parse(normalize(expr), mode="eval")
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            out.add(node.func.id)
        elif isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
    out -= set(FUNCTIONS)
    out -= {"True", "False", "None", "and", "or", "not"}
    return out


def identifiers_in_order(expr: str) -> list[str]:
    """按**在表达式里出现的先后**返回变量名（去重，排除函数名与常量）。

    选股结果表按这个顺序加列 —— 用户扫一眼列头就能和自己的条件对上。
    `extract_identifiers` 返回的是 set（顺序随机），所以这里在文本上重扫一遍、
    用它做过滤，保证"哪些算变量"仍只有那一条判据。
    """
    known = extract_identifiers(expr)
    out: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r"[A-Za-z_][A-Za-z0-9_]*", expr):
        n = m.group(0)
        if n in known and n not in seen:
            seen.add(n)
            out.append(n)
    return out


# ---------------------------------------------------------------------------
# 受限 AST 求值器
# ---------------------------------------------------------------------------

_CMP = {
    ast.Lt: np.less,
    ast.LtE: np.less_equal,
    ast.Gt: np.greater,
    ast.GtE: np.greater_equal,
    ast.Eq: np.equal,
    ast.NotEq: np.not_equal,
}


class ExprError(ValueError):
    pass


class Evaluator:
    def __init__(self, variables: dict[str, Any], length: int):
        self.vars = variables
        self.length = length

    # --- 入口 ---
    def eval(self, expr: str):
        expr = normalize(expr)
        if not expr:
            raise ExprError("空表达式")
        try:
            tree = ast.parse(expr, mode="eval")
        except SyntaxError as e:
            raise ExprError(f"表达式语法错误: {expr} ({e.msg})") from e
        return self._node(tree.body)

    # --- 递归求值 ---
    def _node(self, node):
        m = getattr(self, "_n_" + type(node).__name__, None)
        if m is None:
            raise ExprError(f"不支持的语法: {type(node).__name__}")
        return m(node)

    def _n_Constant(self, node):
        return node.value

    def _n_Num(self, node):  # py<3.8 兼容
        return node.n

    def _n_Name(self, node):
        if node.id in self.vars:
            return self.vars[node.id]
        if node.id == "True":
            return True
        if node.id == "False":
            return False
        if node.id == "None":
            return None
        raise ExprError(f"未知字段或函数: {node.id}")

    def _n_UnaryOp(self, node):
        v = self._node(node.operand)
        if isinstance(node.op, ast.USub):
            return -_num(v)
        if isinstance(node.op, ast.UAdd):
            return _num(v)
        if isinstance(node.op, ast.Not):
            return ~_bool(v)
        raise ExprError("不支持的一元运算")

    def _n_BinOp(self, node):
        a = self._node(node.left)
        b = self._node(node.right)
        op = node.op
        with np.errstate(invalid="ignore", divide="ignore", over="ignore"):
            if isinstance(op, ast.Add):
                return _num(a) + _num(b)
            if isinstance(op, ast.Sub):
                return _num(a) - _num(b)
            if isinstance(op, ast.Mult):
                return _num(a) * _num(b)
            if isinstance(op, ast.Div):
                return _num(a) / _num(b)
            if isinstance(op, ast.FloorDiv):
                return np.floor(_num(a) / _num(b))
            if isinstance(op, ast.Mod):
                return np.mod(_num(a), _num(b))
            if isinstance(op, ast.Pow):
                return np.power(_num(a), _num(b))
            if isinstance(op, ast.BitAnd):
                return _bool(a) & _bool(b)
            if isinstance(op, ast.BitOr):
                return _bool(a) | _bool(b)
        raise ExprError("不支持的运算符")

    def _n_BoolOp(self, node):
        vals = [_bool(self._node(v)) for v in node.values]
        out = vals[0]
        for v in vals[1:]:
            out = (out & v) if isinstance(node.op, ast.And) else (out | v)
        return out

    def _n_Compare(self, node):
        left = self._node(node.left)
        result = None
        for op, comp in zip(node.ops, node.comparators):
            right = self._node(comp)
            fn = _CMP.get(type(op))
            if fn is None:
                if isinstance(op, ast.In):
                    raise ExprError("不支持 in 运算")
                raise ExprError("不支持的比较运算")
            with np.errstate(invalid="ignore"):
                cur = fn(_num(left), _num(right))
            result = cur if result is None else (result & cur)
            left = right
        return result

    def _n_IfExp(self, node):
        return f_iif(self._node(node.test), self._node(node.body), self._node(node.orelse))

    def _n_Call(self, node):
        if not isinstance(node.func, ast.Name):
            raise ExprError("只允许调用内置函数")
        fname = node.func.id
        fn = FUNCTIONS.get(fname)
        if fn is None:
            raise ExprError(f"未知函数: {fname}")
        if node.keywords:
            raise ExprError(f"函数 {fname} 不支持关键字参数")
        args = [self._node(a) for a in node.args]
        try:
            return fn(*args)
        except TypeError as e:
            raise ExprError(f"函数 {fname} 参数错误: {e}") from e

    def _n_List(self, node):
        return [self._node(e) for e in node.elts]

    def _n_Tuple(self, node):
        return tuple(self._node(e) for e in node.elts)


def evaluate(expr: str, variables: dict[str, Any], length: int | None = None) -> np.ndarray:
    """对表达式求值，返回结果（numpy 数组或标量）。"""
    if length is None:
        length = _lens(variables) or 1
    return Evaluator(variables, length).eval(expr)


def evaluate_bool(expr: str, variables: dict[str, Any], length: int | None = None) -> np.ndarray:
    """求值并强制转成布尔数组（NaN -> False）。"""
    v = evaluate(expr, variables, length)
    arr = _bool(v)
    if length is not None and arr.ndim == 1 and len(arr) != length:
        raise ExprError("表达式结果长度不匹配")
    return arr


def check_expr(expr: str, known_names: set[str] | None = None) -> tuple[bool, str]:
    """离线校验表达式（不依赖数据）。"""
    try:
        tree = ast.parse(normalize(expr), mode="eval")
    except SyntaxError as e:
        return False, f"语法错误: {e.msg}"
    names = extract_identifiers(expr)
    bad_funcs = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
                fname = getattr(node.func, "id", "?")
                bad_funcs.add(fname)
        if isinstance(node, (ast.Attribute, ast.Lambda, ast.Dict, ast.Set, ast.ListComp,
                             ast.GeneratorExp, ast.Subscript, ast.Await, ast.NamedExpr)):
            return False, f"不支持的语法: {type(node).__name__}"
    if bad_funcs:
        return False, f"未知函数: {', '.join(sorted(bad_funcs))}"
    if known_names is not None:
        unknown = {n for n in names if n not in known_names}
        if unknown:
            return False, f"未知字段: {', '.join(sorted(unknown))}"
    return True, ""


def describe_functions() -> list[dict]:
    return [{"signature": s, "desc": d} for s, d in FUNC_HELP]
