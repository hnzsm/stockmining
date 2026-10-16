# -*- coding: utf-8 -*-
"""全局配置。可用环境变量覆盖（STOCKMINING_ 前缀）。

用法：复制本文件为 config.py，填入你自己的 tushare token 与数据库口令：

    cp config.example.py config.py

config.py 已在 .gitignore 里，不会入库。
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# ----------------------------------------------------------------------------
# Tushare
# ----------------------------------------------------------------------------
TUSHARE_TOKEN = os.getenv(
    "STOCKMINING_TUSHARE_TOKEN",
    "在这里填你的 tushare token（https://tushare.pro 注册后免费获取）",
)
# 接口限流：每秒最多请求次数（tushare 按积分限流，免费/低积分账户建议 2~5）
TUSHARE_QPS = float(os.getenv("STOCKMINING_TUSHARE_QPS", "4"))
TUSHARE_MAX_RETRY = int(os.getenv("STOCKMINING_TUSHARE_MAX_RETRY", "5"))

# ----------------------------------------------------------------------------
# MySQL
# ----------------------------------------------------------------------------
MYSQL = {
    "host": os.getenv("STOCKMINING_DB_HOST", "127.0.0.1"),
    "port": int(os.getenv("STOCKMINING_DB_PORT", "3306")),
    "user": os.getenv("STOCKMINING_DB_USER", "root"),
    "password": os.getenv("STOCKMINING_DB_PASSWORD", "你的 MySQL 密码"),
    "database": os.getenv("STOCKMINING_DB_NAME", "stockmining"),
    "charset": "utf8mb4",
}
DB_POOL_SIZE = 6
# 批量导入行情时关闭 binlog 并放宽事务落盘策略，可显著提升写入速度。
# 仅影响本程序的连接；若该 MySQL 实例参与主从复制，请设为 False。
DB_FAST_IMPORT = os.getenv("STOCKMINING_FAST_IMPORT", "1") == "1"

# ----------------------------------------------------------------------------
# 数据同步默认值
# ----------------------------------------------------------------------------
DEFAULT_SYNC_START = "2023-01-01"
# 复权方式：qfq(前复权) / hfq(后复权) / none(不复权)
ADJUST_MODE = "qfq"

# ----------------------------------------------------------------------------
# 回测默认值
# ----------------------------------------------------------------------------
BACKTEST_DEFAULTS = {
    "init_cash": 1_000_000.0,
    "max_positions": 10,          # 最大同时持仓只数
    "position_pct": 10.0,         # 单票仓位占初始资金比例(%)，0=等分
    "commission": 0.00025,        # 佣金（单边）
    "stamp_tax": 0.0005,          # 印花税（卖出）
    "slippage": 0.001,            # 滑点
    "min_commission": 5.0,        # 最低佣金
    "exclude_st": True,
    "exclude_kcb": False,         # 科创板 688
    "exclude_bj": True,           # 北交所
    "min_list_days": 60,
    "min_amount": 0.0,            # 最小日成交额（千元）
    "max_hold_days": 10,
}

# 回测数据缓存：把 prepare_data（读行情 + 算指标 + 算信号）的产物落盘复用。
# 它只取决于「股票池 + 区间 + 指标口径 + 信号条件」，与上面的仓位/成本参数无关，
# 所以「同池同信号、只改止损/滑点」时能整块省掉（实测全市场 182s → <1s）。
BACKTEST_CACHE = True
BACKTEST_CACHE_TTL_DAYS = 7      # 兜底过期（数据同步后另会主动清空）
BACKTEST_CACHE_MAX_MB = 3000     # 目录容量上限，超了按最旧先删

# 涨跌颜色（中国习惯：涨红跌绿）
COLOR_UP = "#e5352b"
COLOR_DOWN = "#12a06a"

FLASK_HOST = os.getenv("STOCKMINING_HOST", "127.0.0.1")
FLASK_PORT = int(os.getenv("STOCKMINING_PORT", "8777"))
FLASK_DEBUG = os.getenv("STOCKMINING_DEBUG", "0") == "0"

# ----------------------------------------------------------------------------
# 日志
# ----------------------------------------------------------------------------
# 级别：DEBUG / INFO / WARN / ERROR。命令行开 DEBUG：set STOCKMINING_LOG_LEVEL=DEBUG
LOG_LEVEL = os.getenv("STOCKMINING_LOG_LEVEL", "").upper() or ("DEBUG" if FLASK_DEBUG else "INFO")
LOG_DIR = Path(os.getenv("STOCKMINING_LOG_DIR", str(BASE_DIR / "logs")))
LOG_FILE = LOG_DIR / "app.log"
LOG_TO_FILE = os.getenv("STOCKMINING_LOG_FILE", "1") == "1"
LOG_TO_CONSOLE = os.getenv("STOCKMINING_LOG_CONSOLE", "1") == "1"
LOG_MAX_BYTES = int(os.getenv("STOCKMINING_LOG_MAX_BYTES", str(10 * 1024 * 1024)))  # 单文件 10MB
LOG_BACKUPS = int(os.getenv("STOCKMINING_LOG_BACKUPS", "5"))
# SQL 日志：每条语句 + 耗时 + 行数（DEBUG 级别，量大，默认开）
LOG_SQL = os.getenv("STOCKMINING_LOG_SQL", "1") == "1"
# SQL 文本最大长度，超出截断
LOG_SQL_MAX = int(os.getenv("STOCKMINING_LOG_SQL_MAX", "400"))
# 慢查询阈值（毫秒），超过打 WARNING
LOG_SLOW_MS = float(os.getenv("STOCKMINING_LOG_SLOW_MS", "500"))
# 慢请求阈值（毫秒），超过打 WARNING
LOG_SLOW_REQ_MS = float(os.getenv("STOCKMINING_LOG_SLOW_REQ_MS", "1000"))
