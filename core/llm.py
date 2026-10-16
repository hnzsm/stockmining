# -*- coding: utf-8 -*-
"""大模型接入层：接入预设、配置存取、以及「怎么跟模型说这一句话」。

这里只放三件事，都是**口径唯一**的那一层：

  1. `PRESETS`   —— 接入预设（Ollama 本地 / 各家 OpenAI 兼容的远程 API）；
  2. 配置读写与归一（`load_cfg` / `save_cfg` / `normalize_cfg` / `public_cfg`）；
  3. 传输（`chat` / `chat_stream` / `test`）—— 把 messages 发出去、把文本拿回来，
     所有网络/HTTP 错误在这一层翻成**人话**（上层只负责展示，不再解析状态码）。
     `chat` 一次拿全文，`chat_stream` 逐段 yield（**唯一的流式实现**）。

分析业务（语句模板、本地数据上下文、异步任务、落库）在 `core/ai.py`，别混进来。

## 两种 kind

  · `ollama` —— 本地或远程的 Ollama，POST `{base}/api/chat`；
  · `openai` —— OpenAI 兼容的 `{base}/v1/chat/completions`
    （DeepSeek、通义 DashScope 兼容模式、Kimi、智谱、硅基流动、vLLM、LM Studio、
      one-api 这类网关都走这一条）。

## base_url 的容错（照着各家文档直接粘就能用）

Ollama 给 `http://127.0.0.1:11434`；OpenAI 兼容那边的文档地址一会儿带 `/v1`、
一会儿是完整路径，所以 `_endpoint()` 按后缀判断：已经是 `/chat/completions` 就用它，
只到 `/v1` 就补一段，什么都没写就补 `/v1/chat/completions`。

**安全**：api_key 明文存在本机 `ai_profile` 表（自己的机器、自己的 Key），
但**绝不回给前端** —— `list_profiles()` / `public_cfg()` 只回「有没有存」+ 尾 4 位提示。

## 多条配置（profile）

需求原话是「可先配置多个模型，可切换的」，所以配置**不是一份**而是一个列表：

  · 一条 `ai_profile` 记录 = 一个「可切换的接入」（Ollama 本地 / DeepSeek / 内网网关……）；
  · `is_active=1` 的那条就是「现在用哪个」，全局恰好一条（`set_active()` 唯一维护点）；
  · `load_cfg()` 的含义不变 —— **当前生效那条**，所以 `ai.py` / `app.py` 不用关心有几个模型。

老库（配置只有一份、存在 `app_setting.k='ai'`）由 `ensure_profiles()` 在启动时搬成第一条，
搬完把那行换成一个标记（一份旧 Key 不能在被清空时又冒出来）。
"""
from __future__ import annotations

import json
import re
import time

import requests

from core import db, log as _log

_log_llm = _log.get("llm")

# ---------------------------------------------------------------------------
# 接入预设
# ---------------------------------------------------------------------------
# 每个预设给一套「开箱能填上」的默认值：地址 + 建议模型名。kind 决定走哪条协议。
# need_key=False 表示本地服务通常不需要 Key（填了也不报错，会照样带头）。
PRESETS: list[dict] = [
    {
        "key": "ollama", "label": "Ollama（本地）", "kind": "ollama",
        "base_url": "http://127.0.0.1:11434", "model": "qwen2.5:7b",
        "need_key": False,
        "note": "本机跑：先 `ollama serve`，再 `ollama pull qwen2.5:7b`。"
                "模型名写 `ollama list` 里的那一列。",
    },
    {
        "key": "deepseek", "label": "DeepSeek（远程）", "kind": "openai",
        "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat",
        "need_key": True,
        "note": "平台申请 API Key；deepseek-chat 通用、deepseek-reasoner 偏推理。",
    },
    {
        "key": "dashscope", "label": "通义千问 · DashScope 兼容模式（远程）", "kind": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus",
        "need_key": True, "note": "阿里云百炼的 OpenAI 兼容入口。",
    },
    {
        "key": "moonshot", "label": "Kimi · Moonshot（远程）", "kind": "openai",
        "base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k",
        "need_key": True, "note": "moonshot-v1-8k / 32k / 128k 按上下文长度分档。",
    },
    {
        "key": "zhipu", "label": "智谱 GLM（远程）", "kind": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash",
        "need_key": True, "note": "glm-4-flash 便宜快；v4 路径已是完整前缀。",
    },
    {
        "key": "siliconflow", "label": "硅基流动 SiliconFlow（远程）", "kind": "openai",
        "base_url": "https://api.siliconflow.cn/v1", "model": "Qwen/Qwen2.5-7B-Instruct",
        "need_key": True, "note": "一个 Key 打多家开源模型。",
    },
    {
        "key": "openai", "label": "OpenAI（远程）", "kind": "openai",
        "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini",
        "need_key": True, "note": "需要能访问 api.openai.com 的网络环境。",
    },
    {
        "key": "custom", "label": "自定义（OpenAI 兼容）", "kind": "openai",
        "base_url": "", "model": "", "need_key": False,
        "note": "任何 OpenAI 兼容端点（含 one-api / vLLM / LM Studio / 内网网关）。"
                "地址写基础地址即可，`/v1/chat/completions` 会自动补。",
    },
]
"""接入预设。改这里就等于改「下拉里有哪些家」，前端不写死任何一家。"""

PRESET_BY_KEY = {p["key"]: p for p in PRESETS}

DEFAULT_SYSTEM = (
    "你是一名帮 A 股短线交易者做个股排查的助手。要求：\n"
    "1) 只依据下面给出的数据和你自己的知识回答，数据里没有的信息（例如最新公告、"
    "研报、突发消息）要明确说「未知，需自行核实」，**不要编造**具体公告、金额、"
    "机构名或新闻标题；\n"
    "2) 结论先行，先给一句话判断，再列依据；\n"
    "3) 用简体中文，不要写免责声明套话，不要复述我给你的数据。"
)

DEFAULT_CFG: dict = {
    "preset": "ollama",
    "kind": "ollama",
    "base_url": "http://127.0.0.1:11434",
    "api_key": "",
    "model": "qwen2.5:7b",
    "temperature": 0.3,
    "max_tokens": 1024,
    "timeout": 120,          # 读超时（秒）。本地小模型通常几秒；远程看服务端
    "system_prompt": DEFAULT_SYSTEM,
}
"""(preset, kind, base_url, model) 四件套默认走 Ollama 本地 —— 不填 Key 就能跑起来。"""

SETTING_KEY = "ai"
"""老版本（配置只有一份时）把整份配置存 `app_setting` 的这一行里。现在配置在下面
`ai_profile` 表里，这一行只在 `ensure_profiles()` 里读一次做迁移，读完换成一个标记。"""

PROFILE_TABLE = "ai_profile"
"""多条接入配置的表名。**口径唯一**：所有 SQL 都从这里取，别在别处硬写表名。"""


# ---------------------------------------------------------------------------
# 配置：读 / 写 / 归一
# ---------------------------------------------------------------------------

def _num(v, default, lo, hi, cast):
    try:
        n = cast(v)
    except (TypeError, ValueError):
        return default
    if n != n:                      # NaN
        return default
    return max(lo, min(hi, n))


def normalize_cfg(raw: dict | None) -> dict:
    """把任意来源的配置收成一份合法配置（**唯一口径**，读写两侧都走它）。

    预设只决定 `kind` 与 `key_required`：

      · 选了「自定义」→ **不动** kind（内网网关两种协议都可能是，由调用方决定），
        也不要求 Key（vLLM / LM Studio 这类自建服务常常裸跑）；
      · 选了别的预设 → 按预设的 kind 走，免得留下「下拉选了 Ollama、kind 还是 openai」
        这种拆不开的状态。

    ⚠ 只有调用方**明确给了 preset** 时才让预设覆盖 kind —— 不然传个半截 dict
      （比如只想试个地址）会被默认预设悄悄改成 Ollama。
    """
    src = dict(DEFAULT_CFG)
    explicit_preset = isinstance(raw, dict) and "preset" in raw
    if isinstance(raw, dict):
        src.update({k: v for k, v in raw.items() if k in DEFAULT_CFG})
    p = PRESET_BY_KEY.get(str(src.get("preset") or ""))
    if explicit_preset and p and p["key"] != "custom":
        src["kind"] = p["kind"]
    kind = str(src.get("kind") or "openai").strip().lower()
    if kind not in ("ollama", "openai"):
        kind = "openai"
    # 要不要 Key：**跟预设走**，不看协议 —— 远程付费接口都要，自建网关基本都不要。
    key_required = bool(p.get("need_key")) if (p and p["key"] != "custom") else False
    return {
        "preset": str(src.get("preset") or "custom"),
        "kind": kind,
        "key_required": key_required,
        "base_url": str(src.get("base_url") or "").strip().rstrip("/"),
        "api_key": str(src.get("api_key") or "").strip(),
        "model": str(src.get("model") or "").strip(),
        "temperature": _num(src.get("temperature"), DEFAULT_CFG["temperature"], 0.0, 2.0, float),
        "max_tokens": int(_num(src.get("max_tokens"), DEFAULT_CFG["max_tokens"], 64, 32768, float)),
        "timeout": int(_num(src.get("timeout"), DEFAULT_CFG["timeout"], 5, 600, float)),
        "system_prompt": str(src.get("system_prompt") if src.get("system_prompt") is not None
                             else DEFAULT_SYSTEM).strip(),
    }


def _key_hint(key: str) -> str:
    """Key 的提示：前 3 + 后 4（短 Key 直接全遮）。**只用于展示**。"""
    if not key:
        return ""
    if len(key) <= 8:
        return "•" * len(key)
    return key[:3] + "…" + key[-4:]


def public_cfg(cfg: dict | None = None) -> dict:
    """给前端的配置：**不含 api_key**，只给「有没有存」+ 尾号提示。"""
    c = dict(cfg or load_cfg())
    key = c.pop("api_key", "")
    c["api_key_set"] = bool(key)
    c["api_key_hint"] = _key_hint(key)
    c["ready"] = is_ready(cfg)
    return c


def is_ready(cfg: dict | None = None) -> bool:
    """够不够发起一次分析：地址与模型名都得有；按需 Key 的接入还必须有 Key。"""
    c = cfg or load_cfg()
    if not c.get("base_url") or not c.get("model"):
        return False
    if c.get("key_required") and not c.get("api_key"):
        return False
    return True


# ---------------------------------------------------------------------------
# 多条接入配置（profile）：读当前 / 列表 / 切换 / 增删改 / 老库迁移
# ---------------------------------------------------------------------------
# 这一层只回答「用哪一条」；一条配置长什么样由上面的 normalize_cfg 说了算。

_ROW_CFG_FIELDS = tuple(DEFAULT_CFG.keys())
"""表里除 `id`/`name`/`is_active` 之外就是这份配置 —— 字段清单不再手抄第二遍。"""


def preset_label(preset: str) -> str:
    """预设的显示名（新配置没起名字时拿它当默认名）。"""
    p = PRESET_BY_KEY.get(str(preset or ""))
    return p["label"] if p else "默认接入"


def _row_cfg(row: dict) -> dict:
    """一行的字段 → 归一后的配置（含 api_key）。"""
    return normalize_cfg({k: row.get(k) for k in _ROW_CFG_FIELDS})


def _cfg_of(row: dict | None) -> dict | None:
    """一行 → 归一后的配置，外加 `id`/`name`/`is_active` 三个身份字段。"""
    if not row:
        return None
    return {"id": int(row["id"]), "name": str(row.get("name") or ""),
            "is_active": bool(row.get("is_active")), **_row_cfg(row)}


def list_profiles() -> list[dict]:
    """给前端的配置列表：**不含 api_key**，每条带 `ready` 与 Key 尾号提示；当前那条排最前。"""
    try:
        rows = db.query(f"SELECT * FROM {PROFILE_TABLE} ORDER BY is_active DESC, id ASC")
    except Exception as e:  # noqa: BLE001  表还没建好时别让模型设置页整个打不开
        _log_llm.warning("读取 AI 配置列表失败：%s", e)
        return []
    out = []
    for r in rows:
        cfg = _row_cfg(r)
        key = str(r.get("api_key") or "")
        out.append({"id": int(r["id"]), "name": str(r.get("name") or ""),
                    "is_active": bool(r.get("is_active")), "ready": is_ready(cfg),
                    "api_key_set": bool(key), "api_key_hint": _key_hint(key),
                    **{k: v for k, v in cfg.items() if k != "api_key"}})
    return out


def get_profile(pid: int) -> dict | None:
    """按 id 取一条（**含 api_key**，只有后端自己用；给前端的走 `list_profiles`）。"""
    if not pid:
        return None
    return _cfg_of(db.query_one(f"SELECT * FROM {PROFILE_TABLE} WHERE id=%s", (int(pid),)))


def active_id() -> int | None:
    """当前生效那条的 id（没有 → None）。"""
    try:
        v = db.scalar(f"SELECT id FROM {PROFILE_TABLE} WHERE is_active=1 ORDER BY id LIMIT 1")
    except Exception as e:  # noqa: BLE001
        _log_llm.warning("读取当前 AI 配置失败：%s", e)
        return None
    return int(v) if v else None


def active_profile() -> dict | None:
    """当前生效那条（含 api_key）。

    万一库里没有 `is_active=1`（老数据/手改过），退化成第一条 —— 「没有当前配置」这种
    中间态不该往外冒，`ensure_profiles()` 会在启动时把它修好。
    """
    p = get_profile(active_id())
    if p:
        return p
    try:
        return _cfg_of(db.query_one(f"SELECT * FROM {PROFILE_TABLE} ORDER BY id LIMIT 1"))
    except Exception:  # noqa: BLE001
        return None


def load_cfg() -> dict:
    """**当前生效**那条配置 —— 含义与「只有一份配置」的时代完全一样，
    所以 `ai.py` / `app.py` 不需要知道有几个模型。"""
    p = active_profile()
    if p:
        return normalize_cfg(p)
    return normalize_cfg(_legacy_cfg() or None)


def _legacy_cfg() -> dict | None:
    """老版本存在 `app_setting(k='ai')` 里的那份配置。已迁移 / 没有 / 坏了 → None。

    ⚠ 迁移过之后那行会被写成一个标记（带 `migrated_to`）—— 这里必须认它，否则
      「配置表被清空」时一份旧 Key 会自己冒出来。
    """
    try:
        row = db.query_one("SELECT v FROM app_setting WHERE k=%s", (SETTING_KEY,))
    except Exception as e:  # noqa: BLE001
        _log_llm.warning("读取旧版 AI 设置失败：%s", e)
        return None
    if not row or not row.get("v"):
        return None
    try:
        raw = json.loads(row["v"])
    except (ValueError, TypeError):
        _log_llm.warning("旧版 AI 设置不是合法 JSON，忽略")
        return None
    if not isinstance(raw, dict) or raw.get("migrated_to"):
        return None
    return raw


def _check_name(name: str, pid: int | None) -> str:
    """配置名：必填、唯一（列表里靠它区分两条都用同一家的配置）。"""
    name = str(name or "").strip()[:60]
    if not name:
        raise LLMError("给这条配置起个名字 —— 列表里靠它区分（例如「本地 Ollama」「DeepSeek 主力」）")
    row = db.query_one(f"SELECT id FROM {PROFILE_TABLE} WHERE name=%s", (name,))
    if row and int(row["id"]) != int(pid or 0):
        raise LLMError(f"已经有一条叫「{name}」的配置了 —— 换个名字，或去改那一条")
    return name


def _with_preset_defaults(raw: dict) -> dict:
    """新建时：选了预设但地址/模型还空着 → 拿预设的默认值补上，不建空壳。"""
    out = dict(raw or {})
    p = PRESET_BY_KEY.get(str(out.get("preset") or ""))
    if p and p["key"] != "custom":
        if not str(out.get("base_url") or "").strip():
            out["base_url"] = p["base_url"]
        if not str(out.get("model") or "").strip():
            out["model"] = p["model"]
    return out


def save_profile(raw: dict, pid: int | None = None) -> int:
    """新建（`pid` 空）或更新（`pid` 给定）一条配置，返回它的 id。

    `api_key` 三态：
      · 传了 `clear_api_key` → 清空；
      · 传了非空 `api_key`   → 换成新的；
      · `api_key` 空/没传    → **保留这条原来那把**
        （表单里从不回显 Key，否则每次保存都会被空框冲掉）。
    ⚠ Key 是**按条**保留的：改 A 条不会碰到 B 条的 Key（这正是「多条配置」最容易写错的地方）。
    """
    raw = dict(raw or {})
    pid = int(pid or raw.get("id") or 0) or None

    if pid:
        row = db.query_one(f"SELECT * FROM {PROFILE_TABLE} WHERE id=%s", (pid,))
        if not row:
            raise LLMError("这条配置不存在（可能刚被删掉，刷新看看）")
        key = str(row.get("api_key") or "")
        if raw.get("clear_api_key"):
            key = ""
        elif str(raw.get("api_key") or "").strip():
            key = str(raw["api_key"]).strip()
        merged = {**{k: v for k, v in row.items() if k in DEFAULT_CFG},
                  **{k: v for k, v in raw.items() if k in DEFAULT_CFG and k != "api_key"}}
        merged["api_key"] = key
        cfg = normalize_cfg(merged)
        name = _check_name(raw.get("name", row.get("name")), pid)
        db.execute(
            f"UPDATE {PROFILE_TABLE} SET name=%s, preset=%s, kind=%s, base_url=%s, model=%s, "
            "api_key=%s, temperature=%s, max_tokens=%s, timeout=%s, system_prompt=%s WHERE id=%s",
            (name, cfg["preset"], cfg["kind"], cfg["base_url"], cfg["model"], cfg["api_key"],
             cfg["temperature"], cfg["max_tokens"], cfg["timeout"], cfg["system_prompt"], pid))
        _log_llm.info("AI 配置已更新 id=%s name=%s preset=%s model=%s key=%s",
                      pid, name, cfg["preset"], cfg["model"], "有" if cfg["api_key"] else "无")
        return pid

    cfg = normalize_cfg(_with_preset_defaults(raw))
    name = _check_name(raw.get("name") or preset_label(cfg["preset"]), None)
    active = 1 if (raw.get("is_active") or not active_id()) else 0
    new_id = int(db.insert(
        f"INSERT INTO {PROFILE_TABLE} (name, preset, kind, base_url, model, api_key, temperature, "
        "max_tokens, timeout, system_prompt, is_active) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (name, cfg["preset"], cfg["kind"], cfg["base_url"], cfg["model"], cfg["api_key"],
         cfg["temperature"], cfg["max_tokens"], cfg["timeout"], cfg["system_prompt"], active)))
    _log_llm.info("AI 配置已新建 id=%s name=%s preset=%s model=%s active=%s",
                  new_id, name, cfg["preset"], cfg["model"], bool(active))
    return new_id


def set_active(pid: int) -> bool:
    """切换「现在用哪个」。`is_active` 只有这一处会写 —— 全局恰好一条。"""
    pid = int(pid or 0)
    if not db.query_one(f"SELECT id FROM {PROFILE_TABLE} WHERE id=%s", (pid,)):
        return False
    db.execute(f"UPDATE {PROFILE_TABLE} SET is_active=0 WHERE is_active=1 AND id<>%s", (pid,))
    db.execute(f"UPDATE {PROFILE_TABLE} SET is_active=1 WHERE id=%s", (pid,))
    _log_llm.info("AI 配置已切换：id=%s", pid)
    return True


def delete_profile(pid: int) -> bool:
    """删一条配置。

    **至少留一条** —— 一条都没有就等于「没有可用接入」，模型设置页会变成空壳。
    删掉的正好是当前那条时，把剩下的第一条顶上来，不留「没有当前」的中间态。
    """
    pid = int(pid or 0)
    row = db.query_one(f"SELECT id, name, is_active FROM {PROFILE_TABLE} WHERE id=%s", (pid,))
    if not row:
        raise LLMError("这条配置不存在（可能已被删）")
    if int(db.scalar(f"SELECT COUNT(*) FROM {PROFILE_TABLE}") or 0) <= 1:
        raise LLMError("至少要留一条配置 —— 想换掉它，可以先新建一条再把这条删了")
    db.execute(f"DELETE FROM {PROFILE_TABLE} WHERE id=%s", (pid,))
    if row.get("is_active"):
        nxt = db.scalar(f"SELECT id FROM {PROFILE_TABLE} ORDER BY id LIMIT 1")
        if nxt:
            set_active(int(nxt))
    _log_llm.info("AI 配置已删除 id=%s name=%s", pid, row.get("name"))
    return True


def ensure_profiles() -> None:
    """启动时跑一次（幂等）：**至少一条配置，且恰好一条是当前**。

    老库（整份配置存在 `app_setting.k='ai'` 一行里）在这里搬成第一条 —— 只是搬家，
    不是丢弃：Key 一起搬，搬完把那行换成一个标记（`_legacy_cfg()` 认这个标记）。
    """
    try:
        n = int(db.scalar(f"SELECT COUNT(*) FROM {PROFILE_TABLE}") or 0)
    except Exception as e:  # noqa: BLE001
        _log_llm.warning("AI 配置表还没建好，跳过初始化：%s", e)
        return
    if n == 0:
        legacy = _legacy_cfg()
        cfg = normalize_cfg(legacy if legacy else None)
        pid = save_profile({**cfg, "name": preset_label(cfg["preset"]), "is_active": 1})
        if legacy:
            db.execute("INSERT INTO app_setting (k, v) VALUES (%s,%s) "
                       "ON DUPLICATE KEY UPDATE v=VALUES(v)",
                       (SETTING_KEY, json.dumps({"migrated_to": PROFILE_TABLE, "profile_id": pid},
                                                ensure_ascii=False)))
            _log_llm.info("旧版 AI 配置已搬到 %s id=%s（app_setting 那行只留迁移标记）",
                          PROFILE_TABLE, pid)
        else:
            _log_llm.info("AI 配置为空，已建默认一条：id=%s %s（%s）",
                          pid, preset_label(cfg["preset"]), cfg["model"])
    if active_id() is None:
        first = db.scalar(f"SELECT id FROM {PROFILE_TABLE} ORDER BY id LIMIT 1")
        if first:
            set_active(int(first))
            _log_llm.info("AI 配置里没有标记当前的，已把 id=%s 设为当前", first)


# ---------------------------------------------------------------------------
# 传输
# ---------------------------------------------------------------------------

class LLMError(RuntimeError):
    """模型接入层的错误 —— message 已经是**给人看的中文**，上层直接显示即可。"""


def _endpoint(base: str, kind: str) -> str:
    b = (base or "").strip().rstrip("/")
    if not b:
        return ""
    if kind == "ollama":
        if b.endswith("/chat"):
            return b
        if b.endswith("/api"):
            return b + "/chat"
        return b + "/api/chat"
    if b.endswith("/chat/completions"):
        return b
    if b.endswith("/v1"):
        return b + "/chat/completions"
    return b + "/v1/chat/completions"


_MODEL_MISS = ("not found", "not exist", "does not exist", "unknown model", "no such model")
"""响应正文里出现这些词 = 「模型名不存在」而不是「路径写错」（Ollama / OpenAI 兼容都这么回）。"""


def _tags_url(base: str) -> str:
    """Ollama 的模型清单地址（`/api/chat` → `/api/tags`）。"""
    u = _endpoint(base, "ollama")
    return u[:-5] + "/tags" if u.endswith("/chat") else u


def _ollama_models(base: str) -> list[str]:
    """Ollama 上现在装了哪些模型 —— **只在报错时**用来给提示（拿不到就空，别影响主流程）。"""
    try:
        r = requests.get(_tags_url(base), timeout=3)
        data = r.json()
    except Exception:  # noqa: BLE001
        return []
    return [str(m.get("name")) for m in (data.get("models") or []) if m.get("name")]


def _http_msg(r: requests.Response, url: str, cfg: dict | None = None) -> str:
    """把非 2xx 翻成人话 —— 401/404/模型名错是三种最常见的翻车。

    ⚠ 404 有两种完全不同的成因，**别混成一句**：
      · 路径不对（少写/多写 `/v1`）→ 让人去改地址；
      · **模型名不存在**（Ollama 就回 404 + `{"error":"model 'x' not found"}`）→ 让人去改模型名。
    实测踩过：本机 Ollama 没装默认那个 `qwen2.5:7b`，页面却提示「地址可能少写 /v1」，
    照着改地址只会越改越远。
    """
    c = normalize_cfg(cfg) if cfg else {}
    body = ""
    try:
        body = (r.text or "").strip().replace("\n", " ")[:300]
    except Exception:  # noqa: BLE001
        pass
    low = body.lower()
    if r.status_code == 404 and "model" in low and any(k in low for k in _MODEL_MISS):
        head = (f"这个模型名不存在（404）—— 服务上没有「{c.get('model') or '?'}」"
                f"（试的是 {url}）")
        models = _ollama_models(c.get("base_url") or "") if c.get("kind") == "ollama" else []
        head += ("。该 Ollama 现有模型：" + " / ".join(models[:12]) + "，把「模型名」改成其中一个"
                 if models else "。Ollama 用 `ollama list` 看现有模型，或 `ollama pull <模型>` 拉一个")
        return f"{head}。返回内容：{body or '（空）'}"
    tips = {
        401: "鉴权失败（401）—— api_key 是否正确 / 是否过期（去「模型设置」页核对）",
        403: "拒绝访问（403）—— Key 没有该模型权限，或余额/实名未完成",
        404: f"接口不存在（404）—— 地址可能少写或多写了 /v1（试的是 {url}）",
        429: "太频繁或额度用尽（429）—— 稍后再试，或换模型/加额度",
    }
    head = tips.get(r.status_code, f"模型服务返回 HTTP {r.status_code}")
    return f"{head}。返回内容：{body or '（空）'}"


def _parse(data: dict, kind: str) -> tuple[str, int | None]:
    """从两种响应里取正文 + token 数（拿不到 token 就 None，不猜）。

    ⚠ **推理型模型**（Ollama 的 qwen3 这类）会把正文放 `message.thinking`、
      而把 `message.content` 留成空串（OpenAI 兼容口是 `reasoning_content`）。
      正文为空时回退读它 —— 否则页面上只会得到一句「模型返回了空内容」，
      而模型其实**答了**，只是放错了字段（实测踩过）。
    """
    if kind == "ollama":
        msg = data.get("message") or {}
        text = msg.get("content") or data.get("response") or ""
        if not str(text).strip():
            text = msg.get("thinking") or ""
        used = None
        if data.get("prompt_eval_count") is not None or data.get("eval_count") is not None:
            used = int(data.get("prompt_eval_count") or 0) + int(data.get("eval_count") or 0)
        return str(text), used
    d: dict = {}
    try:
        d = (data.get("choices") or [{}])[0].get("message") or {}
    except (AttributeError, IndexError, TypeError):
        d = {}
    text = d.get("content") or ""
    if not str(text).strip():
        text = d.get("reasoning_content") or ""
    u = data.get("usage") or {}
    used = None
    if u.get("total_tokens") is not None:
        used = int(u["total_tokens"])
    elif u:
        used = int(u.get("prompt_tokens") or 0) + int(u.get("completion_tokens") or 0)
    return str(text), used


# ---------------------------------------------------------------------------
# 传输：非流式（chat）与流式（chat_stream）
# ---------------------------------------------------------------------------
# 两者的**前置校验、请求头、超时、错误人话**完全共用下面三个小助手 ——
# 流式不是「另一条链路」，只是同一句话的另一种收法。


def _precheck(c: dict) -> str:
    """地址/模型名/Key 三件套。返回请求 URL（校验不过就抛 LLMError）。"""
    if not c["base_url"]:
        raise LLMError("还没配置模型地址（去「模型设置」页填）")
    if not c["model"]:
        raise LLMError("还没填模型名（去「模型设置」页填）")
    if c["kind"] == "openai" and c["key_required"] and not c["api_key"]:
        raise LLMError("这个接入需要 API Key（去「模型设置」页填）；"
                       "自建网关（vLLM / LM Studio 等）请在预设里选「自定义」")
    return _endpoint(c["base_url"], c["kind"])


def _headers(c: dict) -> dict:
    h = {"Content-Type": "application/json"}
    if c["api_key"]:
        h["Authorization"] = "Bearer " + c["api_key"]
    return h


def _send(url: str, payload: dict, headers: dict, tmo: int, c: dict,
          stream: bool = False) -> requests.Response:
    """发一次请求，把网络异常翻成人话。**不重试**（见 `chat` 的说明）。"""
    try:
        return requests.post(url, json=payload, headers=headers,
                             timeout=(10, tmo), stream=stream)
    except requests.exceptions.ConnectTimeout:
        raise LLMError(f"连接超时：{c['base_url']} 10 秒没响应 —— 地址/端口是否写对？")
    except requests.exceptions.ReadTimeout:
        raise LLMError(f"等待模型响应超时（{tmo}s）—— 本地小模型可调低 max_tokens，"
                       f"或把「超时」改大（在「模型设置」页）")
    except requests.exceptions.SSLError as e:
        raise LLMError(f"TLS 握手失败：{e} —— 自签证书的内网网关可改用 http://")
    except requests.exceptions.ConnectionError as e:
        hint = ("—— 本地 Ollama 起了吗？`ollama serve`" if c["kind"] == "ollama"
                else "—— 网络不通 / 端口不对 / 服务未启动")
        raise LLMError(f"连不上 {c['base_url']} {hint}（{type(e).__name__}）")
    except requests.exceptions.RequestException as e:
        raise LLMError(f"请求失败：{type(e).__name__}: {e}")


def chat(cfg: dict, messages: list[dict], timeout: int | None = None,
         max_tokens: int | None = None) -> dict:
    """发一次对话请求，返回 `{text, model, url, elapsed_ms, tokens}`。

    所有失败都抛 `LLMError`（中文、可展示）。**不重试** —— 模型调用不是幂等便宜操作，
    而且失败原因多半是配置错，重试只会让用户多等一个超时。
    """
    c = normalize_cfg(cfg)
    url = _precheck(c)
    mt = int(max_tokens or c["max_tokens"])
    if c["kind"] == "ollama":
        payload = {"model": c["model"], "messages": messages, "stream": False,
                   "options": {"temperature": c["temperature"], "num_predict": mt}}
    else:
        payload = {"model": c["model"], "messages": messages, "stream": False,
                   "temperature": c["temperature"], "max_tokens": mt}

    tmo = int(timeout or c["timeout"])
    t0 = time.perf_counter()
    r = _send(url, payload, _headers(c), tmo, c)

    if r.status_code >= 400:
        raise LLMError(_http_msg(r, url, c))
    try:
        data = r.json()
    except ValueError:
        raise LLMError(f"接口没返回 JSON（HTTP {r.status_code}）：{(r.text or '')[:200]}")

    text, tokens = _parse(data, c["kind"])
    if not text.strip():
        raise LLMError(f"模型返回了空内容（model={c['model']}）—— "
                       f"确认模型名是否存在于该服务；推理型模型可能只回 reasoning_content")
    elapsed = int((time.perf_counter() - t0) * 1000)
    _log_llm.info("模型调用成功 kind=%s model=%s %dms tokens=%s len=%d",
                  c["kind"], data.get("model") or c["model"], elapsed, tokens, len(text))
    return {"text": text.strip(), "model": str(data.get("model") or c["model"]),
            "url": url, "elapsed_ms": elapsed, "tokens": tokens}


def _stream_line(raw, kind: str) -> dict | None:
    """流式响应的一行 → dict（认不出/心跳行 → None；`[DONE]` → `{"__done__": True}`）。

    · **ollama**：一行一个 JSON（NDJSON）；
    · **openai 兼容**：SSE，`data: {...}`，收尾是 `data: [DONE]`。
    ⚠ 按**行**解码而不是 `iter_lines(decode_unicode=True)`：一行 JSON 里不会出现
      0x0A 字节，所以 UTF-8 多字节序列不可能被行边界劈开，中文不会变乱码。
    """
    s = raw.decode("utf-8", "ignore") if isinstance(raw, (bytes, bytearray)) else str(raw)
    s = s.strip()
    if not s:
        return None
    if kind == "openai":
        if s.startswith("data:"):
            s = s[5:].strip()
        elif s.startswith(":"):
            return None                     # SSE 注释/心跳
        if s == "[DONE]":
            return {"__done__": True}
    try:
        obj = json.loads(s)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def chat_stream(cfg: dict, messages: list[dict], timeout: int | None = None,
                max_tokens: int | None = None):
    """**流式**发一次对话请求 —— 生成器，逐段 yield 事件：

        {"delta": "文本"}        正文增量（可能只有一两个字）
        {"think": "文本"}        思考增量（推理型模型才有；只用来显示进度）
        {"done": True, "text": 全文, "think_text": 全部思考,
         "model": ..., "tokens": ..., "elapsed_ms": ...}

    ⚠ 这是**唯一**的流式传输点：`core/ai.py` 只负责往里塞 messages 和往外搬事件，
      别在业务层再拼一次协议（那就会出现第二套「怎么读流」的写法）。

    与 `chat()` 的三点不同：
      · 错误仍然是 `LLMError`（同一套人话），但**已经在推流之后**出的错
        （例如读一半断了）由调用方落进记录的 error 列；
      · token 数不一定拿得到（OpenAI 兼容要 `stream_options.include_usage`，
        个别网关不认这个字段 → 见下面的降级重试），拿不到就是 `None`，**不猜**；
      · 正文**空而思考非空**时（推理型模型把答案写在 thinking 里），把 thinking
        当正文回 —— 否则用户看到的是一片空白，而模型其实答了。
    """
    c = normalize_cfg(cfg)
    url = _precheck(c)
    mt = int(max_tokens or c["max_tokens"])
    tmo = int(timeout or c["timeout"])

    def payload_of(with_usage: bool) -> dict:
        if c["kind"] == "ollama":
            return {"model": c["model"], "messages": messages, "stream": True,
                    "options": {"temperature": c["temperature"], "num_predict": mt}}
        p = {"model": c["model"], "messages": messages, "stream": True,
             "temperature": c["temperature"], "max_tokens": mt}
        if with_usage:
            p["stream_options"] = {"include_usage": True}
        return p

    t0 = time.perf_counter()
    r = _send(url, payload_of(c["kind"] == "openai"), _headers(c), tmo, c, stream=True)
    # 个别自建网关不认 stream_options → 只针对这一种 400 降级重试一次（换个写法继续，
    # 别因为一个「顺带要点统计」的可选字段把整次分析挡掉）。
    if r.status_code == 400 and c["kind"] == "openai" and "stream_options" in (r.text or "").lower():
        try:
            r.close()
        except Exception:  # noqa: BLE001
            pass
        _log_llm.info("网关不认 stream_options，去掉它重试")
        r = _send(url, payload_of(False), _headers(c), tmo, c, stream=True)
    if r.status_code >= 400:
        raise LLMError(_http_msg(r, url, c))

    parts: list[str] = []
    thinks: list[str] = []
    model = c["model"]
    tokens: int | None = None
    try:
        for raw in r.iter_lines():
            obj = _stream_line(raw, c["kind"])
            if not obj:
                continue
            if obj.get("__done__"):
                break
            if obj.get("model"):
                model = str(obj["model"])
            if c["kind"] == "ollama":
                msg = obj.get("message") or {}
                piece, think = str(msg.get("content") or ""), str(msg.get("thinking") or "")
                pe, ec = obj.get("prompt_eval_count"), obj.get("eval_count")
                if pe is not None or ec is not None:
                    tokens = int(pe or 0) + int(ec or 0)
            else:
                ch = obj.get("choices") or []
                d = (ch[0].get("delta") or {}) if ch else {}
                piece = str(d.get("content") or "")
                think = str(d.get("reasoning_content") or "")
                u = obj.get("usage") or {}
                if u.get("total_tokens") is not None:
                    tokens = int(u["total_tokens"])
                elif u:
                    tokens = int(u.get("prompt_tokens") or 0) + int(u.get("completion_tokens") or 0)
            if think:
                thinks.append(think)
                yield {"think": think}
            if piece:
                parts.append(piece)
                yield {"delta": piece}
    except requests.exceptions.ChunkedEncodingError as e:
        raise LLMError(f"流式响应中断（{e}）—— 网络抖动或服务端提前关闭了连接")
    except requests.exceptions.RequestException as e:
        raise LLMError(f"读流失败：{type(e).__name__}: {e}")
    finally:
        try:
            r.close()
        except Exception:  # noqa: BLE001
            pass

    text = "".join(parts)
    think_text = "".join(thinks)
    from_think = False
    if not text.strip() and think_text.strip():
        # 推理型模型把答案写进了思考流：整段拿来当正文（页面上会标注这一点）
        text = think_text
        from_think = True
    elapsed = int((time.perf_counter() - t0) * 1000)
    _log_llm.info("模型流式调用完成 kind=%s model=%s %dms tokens=%s len=%d think=%d",
                  c["kind"], model, elapsed, tokens, len(text), len(think_text))
    yield {"done": True, "text": text.strip(), "think_text": think_text,
           "from_think": from_think, "model": model, "tokens": tokens,
           "elapsed_ms": elapsed, "url": url}


def test(cfg: dict | None = None) -> dict:
    """测一次连接：让模型只回两个字，验证「地址+Key+模型名」三件都对。"""
    c = normalize_cfg(cfg or load_cfg())
    res = chat(c, [{"role": "user", "content": "只回复两个字：可用"}], max_tokens=32)
    res["kind"] = c["kind"]
    res["configured_model"] = c["model"]
    return res


# ---------------------------------------------------------------------------
# 语句模板：变量表与渲染（**唯一口径**）
# ---------------------------------------------------------------------------
# 变量只有这一份清单：模型设置页的「可插入变量」、后端渲染、回归断言都从这里取。
# `key` 是 `core/ai.py` 里上下文字典的键；两个名字（中文占位符 / 英文键）都能被替换，
# 但**界面上只露中文那套** —— 写提示词的人没必要记英文。
TEMPLATE_VARS: list[tuple[str, str, str]] = [
    ("{股票名称}", "股票名称（如 平安银行）", "name"),
    ("{股票代码}", "TS 代码（如 000001.SZ）", "code"),
    ("{行业}", "所属行业", "industry"),
    ("{最新价}", "查询日收盘价", "close"),
    ("{涨跌幅}", "查询日涨跌幅 %", "pct_chg"),
    ("{日期}", "查询日（基准日）", "date"),
    ("{策略名}", "挂的买卖策略名，没有则为空", "strategy"),
    ("{选股条件}", "命中的选股条件描述", "conds"),
    ("{本地数据}", "本地行情/指标/筹码快照 —— 写上才附带（不写就不生成、不占字数）", "data"),
]
"""变量表：(占位符, 说明, 上下文字段)。"""

_ALIAS = {ph.strip("{}"): key for ph, _d, key in TEMPLATE_VARS}
"""中文占位符 → 上下文字段。"""

_TOK = re.compile(r"\{([^{}\n]{1,24})\}")


def var_help() -> list[dict]:
    """给前端的变量清单（模型设置页「可插入变量」按它渲染）。"""
    return [{"token": ph, "desc": d, "key": k} for ph, d, k in TEMPLATE_VARS]


def tokens_of(tpl: str) -> set[str]:
    """模板里出现的变量名（上下文字段名）。认不出来的忽略。"""
    out = set()
    for m in _TOK.finditer(tpl or ""):
        k = _ALIAS.get(m.group(1)) or _ALIAS.get(m.group(1).lower())
        if k:
            out.add(k)
    return out


def raw_tokens(tpl: str) -> list[str]:
    """模板里**原文**写出来的占位符（带花括号、保持用户写法），按出现顺序去重。

    ⚠ 与 `tokens_of()` 的区别：那个返回**内部字段名**（`name` / `data` …），是给程序判断用的；
    这个给界面看 —— 用户写的是 `{股票名称}`，列表里就该显示 `{股票名称}`，显示成 `name`
    等于把中文写法翻译成英文 key，看的人反而认不出来那是哪个变量。
    """
    out: list[str] = []
    seen = set()
    for m in _TOK.finditer(tpl or ""):
        t = "{%s}" % m.group(1)
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def wants_data(tpl: str) -> bool:
    """模板里写了 `{本地数据}` → 才去生成快照（生成要走一次选股口径，不是白拿的）。"""
    return "data" in tokens_of(tpl)


def render(tpl: str, ctx: dict) -> tuple[str, list[str]]:
    """渲染模板 → `(文本, 认不出来的占位符列表)`。

    认不出来的**原样留着**并由调用方提示（模板里写 JSON 示例时的大括号不该被吃掉，
    但写错一个变量的名字必须让人看见 —— 静默替换成空最难查）。
    """
    unknown: list[str] = []

    def sub(m: re.Match) -> str:
        raw = m.group(1)
        k = _ALIAS.get(raw) or _ALIAS.get(raw.lower())
        if not k:
            unknown.append(m.group(0))
            return m.group(0)
        v = ctx.get(k)
        if v is None:
            return ""
        return str(v)

    return _TOK.sub(sub, tpl or ""), sorted(set(unknown))
