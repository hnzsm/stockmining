# -*- coding: utf-8 -*-
"""MySQL 访问层：连接、建库建表、通用查询/写入工具。"""
from __future__ import annotations

import contextlib
import datetime
import threading
import time
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd
import pymysql
from pymysql.cursors import DictCursor

from config import DB_FAST_IMPORT, MYSQL
from core import log as _log

_sql_log = _log.get("db")

np_bool = np.bool_
np_int = np.integer
np_float = np.floating
np_generic = np.generic
pd_Timestamp = pd.Timestamp
pd_NaT = pd.NaT
pd_isna = pd.isna

_local = threading.local()

SCHEMA_SQL = [
    # ---------------- 基础信息 ----------------
    """
    CREATE TABLE IF NOT EXISTS stock_basic (
        ts_code      VARCHAR(12)  NOT NULL COMMENT 'TS代码 000001.SZ',
        symbol       VARCHAR(10)  NOT NULL COMMENT '股票代码 000001',
        name         VARCHAR(40)           COMMENT '名称',
        area         VARCHAR(20)           COMMENT '地域',
        industry     VARCHAR(40)           COMMENT '所属行业',
        market       VARCHAR(20)           COMMENT '市场类别 主板/创业板/科创板/北交所',
        exchange     VARCHAR(10)           COMMENT '交易所 SSE/SZSE/BSE',
        list_date    DATE                  COMMENT '上市日期',
        list_status  VARCHAR(4)   DEFAULT 'L' COMMENT 'L上市 D退市 P暂停',
        is_hs        VARCHAR(4)            COMMENT '沪深港通标的 N/H/S',
        updated_at   DATETIME     DEFAULT CURRENT_TIMESTAMP COMMENT '更新时间',
        PRIMARY KEY (ts_code),
        KEY idx_symbol (symbol),
        KEY idx_industry (industry),
        KEY idx_list_date (list_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='股票基础信息'
    """,
    # ---------------- 交易日历 ----------------
    """
    CREATE TABLE IF NOT EXISTS trade_cal (
        exchange      VARCHAR(10) NOT NULL COMMENT '交易所',
        cal_date      DATE        NOT NULL COMMENT '日历日期',
        is_open       TINYINT     NOT NULL DEFAULT 0 COMMENT '是否交易 1是0否',
        pretrade_date DATE                 COMMENT '上一交易日',
        PRIMARY KEY (exchange, cal_date),
        KEY idx_cal_open (cal_date, is_open)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='交易日历'
    """,
    # ---------------- 指数成分 ----------------
    """
    CREATE TABLE IF NOT EXISTS index_member (
        index_code VARCHAR(12) NOT NULL COMMENT '指数代码',
        ts_code    VARCHAR(12) NOT NULL COMMENT '成分股票代码',
        in_date    DATE                 COMMENT '纳入日期',
        out_date   DATE                 COMMENT '剔除日期',
        weight     FLOAT                COMMENT '权重',
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (index_code, ts_code),
        KEY idx_ts (ts_code)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='指数成分股'
    """,
    # ---------------- 日线 ----------------
    """
    CREATE TABLE IF NOT EXISTS daily_kline (
        ts_code      VARCHAR(12)  NOT NULL,
        trade_date   DATE         NOT NULL,
        open         DECIMAL(14,4)         COMMENT '开盘价',
        high         DECIMAL(14,4)         COMMENT '最高价',
        low          DECIMAL(14,4)         COMMENT '最低价',
        close        DECIMAL(14,4)         COMMENT '收盘价',
        pre_close    DECIMAL(14,4)         COMMENT '昨收价',
        change_amt   DECIMAL(14,4)         COMMENT '涨跌额',
        pct_chg      DECIMAL(12,4)         COMMENT '涨跌幅%',
        vol          DECIMAL(24,4)         COMMENT '成交量(手)',
        amount       DECIMAL(24,4)         COMMENT '成交额(千元)',
        adj_factor   DECIMAL(20,8) DEFAULT 1 COMMENT '复权因子',
        PRIMARY KEY (ts_code, trade_date),
        KEY idx_trade_date (trade_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='日线行情(不复权+复权因子)'
    """,
    # ---------------- 周线 ----------------
    """
    CREATE TABLE IF NOT EXISTS weekly_kline (
        ts_code      VARCHAR(12)  NOT NULL,
        trade_date   DATE         NOT NULL COMMENT '周最后一个交易日',
        open         DECIMAL(14,4),
        high         DECIMAL(14,4),
        low          DECIMAL(14,4),
        close        DECIMAL(14,4),
        pre_close    DECIMAL(14,4),
        change_amt   DECIMAL(14,4),
        pct_chg      DECIMAL(12,4),
        vol          DECIMAL(24,4),
        amount       DECIMAL(24,4),
        adj_factor   DECIMAL(20,8) DEFAULT 1,
        PRIMARY KEY (ts_code, trade_date),
        KEY idx_trade_date (trade_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='周线行情(由日线合成)'
    """,
    # ---------------- 每日指标 ----------------
    """
    CREATE TABLE IF NOT EXISTS daily_basic (
        ts_code        VARCHAR(12) NOT NULL,
        trade_date     DATE        NOT NULL,
        turnover_rate  FLOAT                COMMENT '换手率%',
        volume_ratio   FLOAT                COMMENT '量比',
        pe             FLOAT                COMMENT '市盈率',
        pb             FLOAT                COMMENT '市净率',
        ps             FLOAT                COMMENT '市销率',
        total_mv       FLOAT                COMMENT '总市值(万元)',
        circ_mv        FLOAT                COMMENT '流通市值(万元)',
        PRIMARY KEY (ts_code, trade_date),
        KEY idx_trade_date (trade_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='每日指标'
    """,
    # ---------------- 个股资金流向 ----------------
    """
    CREATE TABLE IF NOT EXISTS money_flow (
        ts_code         VARCHAR(12) NOT NULL,
        trade_date      DATE        NOT NULL,
        buy_sm_vol      DOUBLE COMMENT '小单买入量(手)',
        buy_sm_amount   DOUBLE COMMENT '小单买入额(万元)',
        sell_sm_vol     DOUBLE COMMENT '小单卖出量(手)',
        sell_sm_amount  DOUBLE COMMENT '小单卖出额(万元)',
        buy_md_vol      DOUBLE COMMENT '中单买入量(手)',
        buy_md_amount   DOUBLE COMMENT '中单买入额(万元)',
        sell_md_vol     DOUBLE COMMENT '中单卖出量(手)',
        sell_md_amount  DOUBLE COMMENT '中单卖出额(万元)',
        buy_lg_vol      DOUBLE COMMENT '大单买入量(手)',
        buy_lg_amount   DOUBLE COMMENT '大单买入额(万元)',
        sell_lg_vol     DOUBLE COMMENT '大单卖出量(手)',
        sell_lg_amount  DOUBLE COMMENT '大单卖出额(万元)',
        buy_elg_vol     DOUBLE COMMENT '特大单买入量(手)',
        buy_elg_amount  DOUBLE COMMENT '特大单买入额(万元)',
        sell_elg_vol    DOUBLE COMMENT '特大单卖出量(手)',
        sell_elg_amount DOUBLE COMMENT '特大单卖出额(万元)',
        net_mf_vol      DOUBLE COMMENT '净流入量(手)',
        net_mf_amount   DOUBLE COMMENT '净流入额(万元)',
        PRIMARY KEY (ts_code, trade_date),
        KEY idx_trade_date (trade_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='个股资金流向(分档买卖额)'
    """,
    # ---------------- 指数日线（基准） ----------------
    """
    CREATE TABLE IF NOT EXISTS index_daily (
        ts_code    VARCHAR(12) NOT NULL COMMENT '指数代码',
        trade_date DATE        NOT NULL,
        open       DECIMAL(14,4),
        high       DECIMAL(14,4),
        low        DECIMAL(14,4),
        close      DECIMAL(14,4),
        pre_close  DECIMAL(14,4),
        pct_chg    DECIMAL(12,4),
        vol        DECIMAL(24,4),
        amount     DECIMAL(24,4),
        PRIMARY KEY (ts_code, trade_date),
        KEY idx_trade_date (trade_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='指数日线（基准用）'
    """,
    # ---------------- 期货 ----------------
    # 与股票行情同一条链路：**tushare → 本地库 → 页面**。
    # ⚠ 曾经这里走的是东财站外接口（core/futures.py 的老实现），站外一被拒就整页取不到
    #   （2026-09-25 起 push2.eastmoney.com 持续断连）。落库后页面不再依赖外网。
    """
    CREATE TABLE IF NOT EXISTS futures_basic (
        ts_code     VARCHAR(20) NOT NULL COMMENT 'TS代码（RU2511.SHF / 主连 RU.SHF）',
        symbol      VARCHAR(20)          COMMENT '交易所内代码（RU2511）',
        exchange    VARCHAR(8)           COMMENT 'SHFE/DCE/CZCE/CFFEX/INE/GFEX',
        name        VARCHAR(64)          COMMENT '合约名称（橡胶2511）',
        fut_code    VARCHAR(16)          COMMENT '品种码（RU）—— 同一品种归一组用',
        is_main     TINYINT     NOT NULL DEFAULT 0 COMMENT '1=主连连续合约（symbol 不含月份数字）',
        list_date   DATE                 COMMENT '上市日',
        delist_date DATE                 COMMENT '退市日（最后交易日）',
        PRIMARY KEY (ts_code),
        KEY idx_fut_code (fut_code),
        KEY idx_main (is_main, exchange)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='期货合约基本信息'
    """,
    """
    CREATE TABLE IF NOT EXISTS futures_daily (
        ts_code     VARCHAR(20) NOT NULL,
        trade_date  DATE        NOT NULL,
        open        DECIMAL(14,4),
        high        DECIMAL(14,4),
        low         DECIMAL(14,4),
        close       DECIMAL(14,4)         COMMENT '收盘价',
        settle      DECIMAL(14,4)         COMMENT '结算价（期货特有，涨跌用它算）',
        pre_close   DECIMAL(14,4)         COMMENT '昨收盘',
        pre_settle  DECIMAL(14,4)         COMMENT '昨结算',
        change1     DECIMAL(14,4)         COMMENT '涨跌1 = 收盘价 − 昨结算',
        change2     DECIMAL(14,4)         COMMENT '涨跌2 = 结算价 − 昨结算',
        vol         DECIMAL(24,4)         COMMENT '成交量(手)',
        amount      DECIMAL(24,4)         COMMENT '成交额(万元)',
        oi          DECIMAL(24,4)         COMMENT '持仓量(手)',
        oi_chg      DECIMAL(24,4)         COMMENT '持仓量变化',
        PRIMARY KEY (ts_code, trade_date),
        KEY idx_trade_date (trade_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='期货日线（tushare fut_daily）'
    """,
    # 期货**期权**：只用来给「展开的同品种合约」补一列「对应期权到期日」。
    # 一个期货合约（如 PG2709.DCE）对应的是一大批期权（各档行权价 × 购/沽），
    # 但它们的到期日是**同一个** ⇒ 读取端按 `underlying` 取到期日集合即可。
    # ⚠ `underlying` 由 `opt_code` 去掉 `OP` 前缀得到（OPPG2709.DCE → PG2709.DCE），
    #   直接就是期货合约的 ts_code；中金所股指期权的标的是**指数**（000300.SH），
    #   对不到任何期货合约，读取端自然落成「—」。
    """
    CREATE TABLE IF NOT EXISTS futures_option (
        ts_code        VARCHAR(32) NOT NULL COMMENT '期权TS代码（OPPG2709-C-5100.DCE）',
        symbol         VARCHAR(32)          COMMENT '交易所内代码',
        exchange       VARCHAR(8)           COMMENT 'SHFE/DCE/CZCE/CFFEX/INE/GFEX',
        name           VARCHAR(64)          COMMENT '期权名称（液化石油气购9月5100）',
        opt_type       VARCHAR(16)          COMMENT '期货期权 / 指数期权',
        call_put       CHAR(1)              COMMENT 'C=购 / P=沽',
        exercise_price DECIMAL(16,4)        COMMENT '行权价',
        per_unit       DECIMAL(16,4)        COMMENT '合约单位',
        s_month        VARCHAR(8)           COMMENT '标的月份（202709）',
        underlying     VARCHAR(20)          COMMENT '**标的期货合约的 ts_code**（对不上期货时为指数代码）',
        opt_code       VARCHAR(24)          COMMENT 'tushare 原值（OP+标的代码）',
        maturity_date  DATE                 COMMENT '到期日（= 最后交易日）',
        list_date      DATE                 COMMENT '上市日',
        delist_date    DATE                 COMMENT '退市日',
        PRIMARY KEY (ts_code),
        KEY idx_underlying (underlying, delist_date),
        KEY idx_delist (delist_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='期货期权合约（只同步在市，给期货页补「期权到期日」）'
    """,
    # ---------------- 策略 ----------------
    """
    CREATE TABLE IF NOT EXISTS strategy (
        id         INT AUTO_INCREMENT PRIMARY KEY,
        name       VARCHAR(80) NOT NULL COMMENT '策略名称',
        note       TEXT                 COMMENT '策略说明',
        timeframe  VARCHAR(10) NOT NULL DEFAULT 'daily' COMMENT '主K线 daily/weekly',
        config     JSON        NOT NULL COMMENT '策略配置',
        builtin    TINYINT     NOT NULL DEFAULT 0 COMMENT '是否内置',
        sort       INT         NOT NULL DEFAULT 0 COMMENT '列表显示顺序（拖拽改；同值时按 id）',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uk_name (name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='选股/回测策略'
    """,
    # ---------------- 选股策略（= 条件股票池，可被回测/选股直接引用） ----------------
    """
    CREATE TABLE IF NOT EXISTS pool_strategy (
        id         INT AUTO_INCREMENT PRIMARY KEY,
        name       VARCHAR(80) NOT NULL COMMENT '选股策略名称',
        note       TEXT                 COMMENT '说明',
        base       VARCHAR(80) NOT NULL DEFAULT 'all' COMMENT '基础池 all/000300.SH/industry:银行',
        -- 周期（2026-10-10 需求「选股策略中也要有日k和周k的选择项」）：
        --   `daily`（默认）＝条件在**日线**上逐日求值，老行为；
        --   `weekly` ＝整条链在**周线**上跑（条件里 `close` 就是周收盘、`ma5` 就是 5 周均线），
        --   引用它的选股 / 回测 / 提醒全按周线算 —— 判定只有一处：`strategy.selection_cfg()`。
        timeframe  VARCHAR(8)  NOT NULL DEFAULT 'daily' COMMENT '周期：daily 日K / weekly 周K',
        conds      JSON        NOT NULL COMMENT '条件列表，逐日全部成立才算池内',
        filt       JSON                 COMMENT '静态过滤项（剔ST/板块/上市天数/最小成交额），选股逻辑只归选股策略',
        builtin    TINYINT     NOT NULL DEFAULT 0 COMMENT '是否内置',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uk_name (name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='选股策略（条件股票池）'
    """,
    # ---------------- 回测任务 ----------------
    """
    CREATE TABLE IF NOT EXISTS backtest_run (
        id          INT AUTO_INCREMENT PRIMARY KEY,
        strategy_id INT          NOT NULL,
        name        VARCHAR(160) NOT NULL,
        universe    VARCHAR(80)           COMMENT '股票池（人类可读标签，列表直接显示）',
        universe_spec JSON                COMMENT '回测当时的选股条件快照（base/where/filt/timeframe）',
        start_date  DATE         NOT NULL,
        end_date    DATE         NOT NULL,
        init_cash   DECIMAL(20,2) NOT NULL,
        params      JSON                  COMMENT '回测参数',
        metrics     JSON                  COMMENT '绩效指标',
        status      VARCHAR(20)  DEFAULT 'pending' COMMENT 'pending/running/done/error',
        log         MEDIUMTEXT            COMMENT '运行日志',
        progress    DOUBLE       DEFAULT 0 COMMENT '运行进度 0~1（刷新页面后进度条靠它接回来）',
        progress_msg VARCHAR(200)         COMMENT '当前阶段说明（进度条上那行字）',
        job_id      VARCHAR(20)           COMMENT '内存任务 id：进度/日志的实时来源，服务重启即失效',
        created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
        finished_at DATETIME,
        KEY idx_strategy (strategy_id),
        KEY idx_created (created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='回测任务'
    """,
    # ---------------- 回测交易明细 ----------------
    """
    CREATE TABLE IF NOT EXISTS backtest_trade (
        id          BIGINT AUTO_INCREMENT PRIMARY KEY,
        run_id      INT NOT NULL,
        ts_code     VARCHAR(12) NOT NULL,
        name        VARCHAR(40),
        buy_date    DATE NOT NULL,
        buy_price   DECIMAL(14,4) NOT NULL,
        buy_reason  VARCHAR(80),
        sell_date   DATE,
        sell_price  DECIMAL(14,4),
        shares      INT,
        cost        DECIMAL(20,2),
        revenue     DECIMAL(20,2),
        pnl         DECIMAL(20,2),
        pnl_pct     DECIMAL(12,4),
        hold_days   INT,
        sell_reason VARCHAR(20) COMMENT 'signal/stop_loss/take_profit/max_hold/end',
        fee         DECIMAL(16,2) COMMENT '买入+卖出手续费合计（含印花税）',
        slip_cost   DECIMAL(16,2) COMMENT '滑点成本(买入上浮+卖出下调合计)',
        KEY idx_run (run_id),
        KEY idx_pnl (run_id, pnl_pct)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='回测交易明细'
    """,
    # ---------------- 回测资金曲线 ----------------
    """
    CREATE TABLE IF NOT EXISTS backtest_equity (
        run_id         INT NOT NULL,
        trade_date     DATE NOT NULL,
        total_asset    DECIMAL(20,2) NOT NULL,
        cash           DECIMAL(20,2) NOT NULL,
        position_value DECIMAL(20,2) NOT NULL,
        hold_num       INT DEFAULT 0,
        benchmark      DECIMAL(20,4) COMMENT '基准净值',
        PRIMARY KEY (run_id, trade_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='回测资金曲线'
    """,
    # ---------------- 选股结果 ----------------
    """
    CREATE TABLE IF NOT EXISTS screen_result (
        id          BIGINT AUTO_INCREMENT PRIMARY KEY,
        strategy_id INT NOT NULL,
        trade_date  DATE NOT NULL,
        ts_code     VARCHAR(12) NOT NULL,
        name        VARCHAR(40),
        close       DECIMAL(14,4),
        pct_chg     DECIMAL(12,4),
        score       FLOAT,
        detail      JSON COMMENT '命中明细',
        created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uk_str_date_code (strategy_id, trade_date, ts_code),
        KEY idx_date (trade_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='选股结果'
    """,
    # ---------------- 股东户数 ----------------
    # 一个报告期可能在**多个公告日**被披露/修订（例如年报先出快报、再出正式报告），
    # 所以主键带上 ann_date 才能无损落库；读取时按 end_date 归并、取最新一次公告
    # （见 core/chips.py 的 `_holder_series`），**别在写库这层就把旧的覆盖掉**。
    """
    CREATE TABLE IF NOT EXISTS holder_number (
        ts_code    VARCHAR(12) NOT NULL COMMENT 'TS代码',
        ann_date   DATE        NOT NULL COMMENT '公告日期（发布时间）',
        end_date   DATE        NOT NULL COMMENT '数据截止日期（报告期）',
        holder_num BIGINT               COMMENT '股东户数',
        PRIMARY KEY (ts_code, ann_date, end_date),
        KEY idx_ann (ann_date),
        KEY idx_code_end (ts_code, end_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='股东户数（按报告期，含公告日）'
    """,
    # ---------------- 融资融券 ----------------
    # 单位保持 tushare 原样：金额是**元**（不是万元，与 money_flow 不同）、量是**股**。
    # 换算与派生（融资净买入、两融占流通市值）统一在 core/chips.py 做一次。
    """
    CREATE TABLE IF NOT EXISTS margin_detail (
        ts_code    VARCHAR(12) NOT NULL COMMENT 'TS代码',
        trade_date DATE        NOT NULL COMMENT '交易日',
        rzye       DOUBLE COMMENT '融资余额(元)',
        rzmre      DOUBLE COMMENT '融资买入额(元)',
        rzche      DOUBLE COMMENT '融资偿还额(元)',
        rqye       DOUBLE COMMENT '融券余额(元)',
        rqyl       DOUBLE COMMENT '融券余量(股)',
        rqmcl      DOUBLE COMMENT '融券卖出量(股)',
        rqchl      DOUBLE COMMENT '融券偿还量(股)',
        rzrqye     DOUBLE COMMENT '融资融券余额(元)',
        PRIMARY KEY (ts_code, trade_date),
        KEY idx_trade_date (trade_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='融资融券明细（单位：元 / 股）'
    """,
    # ---------------- 股东增减持 ----------------
    # tushare 的 stk_holdertrade **没有唯一键**，且实测同一公告日存在完全重复的行
    # （600519.SH 2025-12-30 同一股东同样数字出现 3 次）。所以不去造指纹列，
    # 改用「**按公告日整体替换**」：同步某天先删该天再插入(已去重)，
    # 天然幂等，重复跑不会累积脏数据（见 sync.sync_holder_trade）。
    """
    CREATE TABLE IF NOT EXISTS holder_trade (
        id           BIGINT AUTO_INCREMENT PRIMARY KEY,
        ts_code      VARCHAR(12)  NOT NULL COMMENT 'TS代码',
        ann_date     DATE         NOT NULL COMMENT '公告日期',
        holder_name  VARCHAR(512)         COMMENT '股东名称（tushare 会把多个基金名用逗号拼成一行，实测到 170 字符，别按 160 截）',
        holder_type  VARCHAR(4)            COMMENT 'G高管 P个人 C公司',
        in_de        VARCHAR(4)            COMMENT 'IN增持 DE减持',
        change_vol   DOUBLE                COMMENT '变动数量(股)',
        change_ratio DOUBLE                COMMENT '变动比例%',
        after_share  DOUBLE                COMMENT '变动后持股(股)',
        after_ratio  DOUBLE                COMMENT '变动后持股占比%',
        avg_price    DOUBLE                COMMENT '交易均价(元)',
        total_share  DOUBLE                COMMENT '持股总数(股)',
        KEY idx_code_ann (ts_code, ann_date),
        KEY idx_ann (ann_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='股东增减持（按公告日整体替换）'
    """,
    # ---------------- 东财概念（板块 / 个股所属概念） ----------------
    # 来源：东方财富 F10「核心题材」公开接口（`datacenter-web.eastmoney.com` 的
    # `RPT_F10_CORETHEME_BOARDTYPE`）。**不是 tushare**、不消耗额度，与 `core/hot.py`
    # （热股榜）同性质：站外接口，随时可能不通 ⇒ 同步失败不牵连别的任务，页面只读本地库。
    #
    # ⚠ 为什么不用 push2 的「个股所属板块」（`spt=3`）：那是个**混装列表** —— 实测浦发银行
    #   24 条 = 行业(银行Ⅱ) + 地域(上海板块) + 指数成分(HS300_/上证180_) + 标签(融资融券/
    #   沪股通/MSCI中国) + 概念(区块链/蚂蚁金服概念)，真概念只有 3~5 条，**且没有入选理由**。
    #   F10 这份带 `SELECTED_BOARD_REASON`（入选理由原文，含日期），所以**只认 F10**。
    #
    # ⚠ 「是不是概念」的判据（2026-10-07 实测敲定）：**看 `SELECTED_BOARD_REASON` 有没有值**。
    #   东财的 `IS_PRECISE` 靠不住 —— 它标成 1 的里面混着行业（银行 / 白酒Ⅱ / 医药生物）、
    #   风格（大盘股 / 权重股 / 低市净率 / 先进制造风格）、指数成分（上证50_ / 深证100R）。
    #   而**有入选理由的那批恰好全是真概念**（蚂蚁金服概念 / 区块链 / 麒麟电池 / CAR-T细胞疗法），
    #   连 `锂电池`(行业) 与 `锂电池概念`(概念) 都能分开。所以 `is_concept` = 「有理由」，
    #   `IS_PRECISE` 不再使用。
    #
    # ⚠ 与 `stock_basic.industry` 的分工：那是**东财一级行业、一股一个**（~100 类），
    #   概念是**多对多**（一只票能同时是 CRO + 重组蛋白 + 减肥药）。要做「板块共振」这类
    #   判断只能用概念，用行业会把颗粒度做粗一个量级。
    #
    # `pct_chg` 那一组是**同步时点的板块行情快照**（东财只给当下，没有历史），不是历史序列 ——
    # 拿它做回测会变成未来函数，页面展示用。
    """
    CREATE TABLE IF NOT EXISTS concept_basic (
        board_code  VARCHAR(16) NOT NULL COMMENT '东财板块代码 BK0899',
        board_name  VARCHAR(80) NOT NULL COMMENT '概念名（CRO）',
        pct_chg     FLOAT               COMMENT '板块涨跌幅%（同步时点快照，非历史）',
        up_cnt      INT                 COMMENT '上涨家数',
        down_cnt    INT                 COMMENT '下跌家数',
        lead_name   VARCHAR(40)         COMMENT '领涨股名称',
        member_cnt  INT                 COMMENT '本库该概念覆盖到多少只（非东财官方口径）',
        quoted_at   DATETIME            COMMENT '这组行情快照的时间（东财=取数时刻；local=该交易日 00:00）',
        quote_src   VARCHAR(12)         COMMENT '行情来源 eastmoney=东财 push2 / local=本库行情等权自算',
        updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (board_code),
        KEY idx_board_name (board_name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='东财概念板块（行情列为同步时点快照）'
    """,
    """
    CREATE TABLE IF NOT EXISTS stock_concept (
        ts_code     VARCHAR(12) NOT NULL COMMENT 'TS代码 000001.SZ',
        board_code  VARCHAR(16) NOT NULL COMMENT '东财板块代码 BK0899',
        board_name  VARCHAR(80) NOT NULL COMMENT '概念名（冗余一份：板块表缺行也能显示）',
        reason      TEXT                COMMENT '东财「入选理由」原文 —— **它的有无就是「是不是概念」的判据**',
        is_concept  TINYINT NOT NULL DEFAULT 0 COMMENT '1=真概念（东财给了入选理由） 0=行业/地域/指数成分/风格等非概念标记',
        board_rank  INT                 COMMENT '东财给出的排序（越小越靠前）',
        synced_at   DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '最近一次抓到的时间',
        PRIMARY KEY (ts_code, board_code),
        KEY idx_board (board_code),
        KEY idx_concept (ts_code, is_concept, board_rank)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='个股所属东财概念（F10 核心题材）'
    """,
    # ---------------- 东财「新概念速览」（另一个数据源，与上面 F10 题材库不是一回事） ----------------
    # 来源：https://emdata.eastmoney.com/appdc/gnsl/index.html （移动端「新概念速览」）
    # 真实接口只有两条报表（无鉴权、不耗 tushare 额度）：
    #   ① RPT_CUSTOM_BOARD_NEWCONCEPT     —— 新概念清单（实测 35 条，含概念简介 + 3 只代表股）
    #   ② RPT_CUSTOM_BOARD_EMBOARD_DYNAMIC —— 概念板块成分变更（调入/调出，含「入选理由」原文）
    # ⚠ 与 stock_concept/F10 的区别：F10 是**每只票的题材名单**（多对多、有入选理由），
    #   这里 ① 是**板块视角**（这个概念哪来的、讲什么、代表股是谁），② 是**每日变更流水**。
    #   三个口径不要混着解释（页面文案里也要分开写）。
    """
    CREATE TABLE IF NOT EXISTS em_newconcept (
        board_code   VARCHAR(16)  NOT NULL COMMENT '东财板块代码 BK1660',
        board_name   VARCHAR(80)  NOT NULL COMMENT '概念名（光纤概念）',
        org_board_code VARCHAR(16)         COMMENT '东财原始板块号 1660',
        start_date   DATETIME              COMMENT '概念创建/生效时间（START_DATE）',
        start_text   VARCHAR(24)           COMMENT '东财给的展示串 04-03 15:29',
        new_date     VARCHAR(24)           COMMENT 'NEW_DATE 原文（有的行为空）',
        profile      TEXT                  COMMENT '概念简介原文（BOARD_PROFILE）',
        c1_code      VARCHAR(12)           COMMENT '代表股1 代码（带后缀）',
        c1_name      VARCHAR(40),
        c1_pct       FLOAT                 COMMENT '代表股1 在概念创建当日的涨跌幅%（东财原值，不是最新）',
        c2_code      VARCHAR(12),
        c2_name      VARCHAR(40),
        c2_pct       FLOAT,
        c3_code      VARCHAR(12),
        c3_name      VARCHAR(40),
        c3_pct       FLOAT,
        fetched_at   DATE                  COMMENT '抓取之日（「今天抓过就不再抓」的判据）',
        fetched_ts   DATETIME DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (board_code),
        KEY idx_new_start (start_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='东财新概念清单（「新概念速览」页面，非 F10 题材库）'
    """,
    """
    CREATE TABLE IF NOT EXISTS em_concept_event (
        id          BIGINT AUTO_INCREMENT PRIMARY KEY,
        board_code  VARCHAR(16)  NOT NULL COMMENT '东财板块代码',
        board_name  VARCHAR(80)  NOT NULL,
        change_date DATE         NOT NULL COMMENT '变更日（东财按这个筛）',
        change_time VARCHAR(12)           COMMENT '变更时刻 HH:MM',
        direction   VARCHAR(8)   NOT NULL COMMENT '新增 / 剔除',
        ts_code     VARCHAR(12)           COMMENT '该股代码（东财 SECUCODE）',
        code        VARCHAR(8)            COMMENT '纯代码',
        name        VARCHAR(40),
        pct_chg     FLOAT                 COMMENT '该股当日涨跌幅%',
        board_pct   FLOAT                 COMMENT '该板块当日涨跌幅%',
        change_num  INT                   COMMENT '该板块本次变更只数（东财给的是 1）',
        reason      TEXT                  COMMENT '东财「入选理由」原文（ORG_PROFILE）',
        fetched_at  DATE,
        UNIQUE KEY uk_ev (board_code, change_date, change_time, ts_code, direction),
        KEY idx_date (change_date),
        KEY idx_code (ts_code)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='东财概念板块成分变更（调入/调出流水）'
    """,
    # ---------------- 我的关注（自选股 + 提醒） ----------------
    # 三层：分组 → 自选股 → 提醒规则；命中落 watch_alert_log。
    # ⚠ `watch_alert` 有**两种规则**，由 `kind` 分：
    #   · `kind='price'`（默认）—— 指标/价位规则，四个字段：`freq`（判日K还是周K）/
    #     `line`（判哪条线）/ `value`（或判一个数）/ `op`（cross_up 上穿 · cross_down 下穿 ·
    #     touch 触及）。`line` 为空时用 `value` 判绝对价位。
    #     **判定只用第 i-1 与第 i 两根 K 线**（页面文案要写明「收盘价口径」，见 watch.py）。
    #     ⚠ `freq='weekly'` 判的是**周线那一根**（数据源 `weekly_kline`，只含已收盘的完整周），
    #       不是「把周线均线贴到日线上」—— 详见 watch.check() 与 CONVENTIONS C48。
    #   · `kind='pool'`（2026-10-10 需求「绑定选股策略规则，选股命中则提醒」）—— 只认 `pool_id`
    #     （引用 `pool_strategy.id`）；这天按**选股策略**逐日求值，票入了池就提醒。
    #     `freq/line/value/op` 这四个字段对它是空的，别拿 price 那一套去读它。
    #     ⚠ `pool_id` 允许为空（策略被删）—— 检测时如实跳过并记日志，别静默按空条件当「全命中」。
    """
    CREATE TABLE IF NOT EXISTS watch_group (
        id         INT AUTO_INCREMENT PRIMARY KEY,
        name       VARCHAR(40) NOT NULL COMMENT '分组名（我的自选 / 短线池…）',
        note       VARCHAR(200),
        sort       INT NOT NULL DEFAULT 0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uk_wg_name (name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='我的关注 · 分组'
    """,
    """
    CREATE TABLE IF NOT EXISTS watch_stock (
        id       INT AUTO_INCREMENT PRIMARY KEY,
        group_id INT NOT NULL,
        ts_code  VARCHAR(12) NOT NULL,
        name     VARCHAR(40)            COMMENT '加自选时的名字快照（改名也不影响展示）',
        note     VARCHAR(512)           COMMENT '备注（批量导入的题材标记实测到 400 字符，别按 200 截）',
        sort     INT NOT NULL DEFAULT 0,
        added_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uk_ws (group_id, ts_code),
        KEY idx_ws_code (ts_code)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='我的关注 · 自选股'
    """,
    """
    CREATE TABLE IF NOT EXISTS watch_alert (
        id      INT AUTO_INCREMENT PRIMARY KEY,
        ts_code VARCHAR(12) NOT NULL,
        kind    VARCHAR(8) NOT NULL DEFAULT 'price' COMMENT '规则类型：price 指标/价位 · pool 选股命中',
        pool_id INT                    COMMENT 'kind=pool 时引用的选股策略 id（pool_strategy.id）',
        line    VARCHAR(24) NOT NULL DEFAULT '' COMMENT '判哪条线：ma5/ma10/ma20/ma60/boll_mid/boll_upper/boll_lower；空=判绝对价位',
        value   FLOAT                COMMENT 'line 为空时用的绝对价位',
        op      VARCHAR(16) NOT NULL COMMENT 'cross_up 上穿 / cross_down 下穿 / touch 触及（kind=pool 时为空）',
        freq    VARCHAR(8) NOT NULL DEFAULT 'daily' COMMENT '判哪张图：daily 日K / weekly 周K（周K 只判已收盘的完整周）',
        note    VARCHAR(200),
        enabled TINYINT NOT NULL DEFAULT 1,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        KEY idx_wa_code (ts_code)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='我的关注 · 提醒规则'
    """,
    """
    CREATE TABLE IF NOT EXISTS watch_alert_log (
        id         BIGINT AUTO_INCREMENT PRIMARY KEY,
        alert_id   INT NOT NULL,
        ts_code    VARCHAR(12) NOT NULL,
        trade_date DATE NOT NULL COMMENT '在哪一根 K 线上判出来的',
        message    VARCHAR(255) COMMENT '人话：收盘 12.34 上穿 MA10 12.10',
        ts_value   FLOAT        COMMENT '当根收盘/触发价',
        line_value FLOAT        COMMENT '当根那条线的值',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uk_wal (alert_id, trade_date),
        KEY idx_wal_date (trade_date),
        KEY idx_wal_code (ts_code)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='我的关注 · 提醒触发记录（同一规则同一天只记一条）'
    """,
    # ---------------- 应用设置 ----------------
    # 通用 key/value 设置表。
    # ⚠ AI 接入配置**不在这里了**（原先整份 JSON 存 k='ai' 一行，现在挪到 ai_profile
    #   表，因为要能配多条、切来切去）。老库那行由 `llm.ensure_profiles()` 一次性搬走，
    #   搬完把它换成一个「已迁移」标记 —— 不然一份旧 Key 会在表被清空时又冒出来。
    # ⚠ MySQL 里 `key` 是保留字（要用反引号），所以列名用 `k` / `v`。
    """
    CREATE TABLE IF NOT EXISTS app_setting (
        k          VARCHAR(64) NOT NULL COMMENT '设置项名字（如 ai）',
        v          MEDIUMTEXT           COMMENT '设置内容（JSON）',
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (k)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='应用设置（key/value，值按 JSON 存）'
    """,
    # ---------------- AI 模型接入配置（可配多条，切换当前） ----------------
    # 一条记录 = 一个「可切换的模型接入」：Ollama 本地、DeepSeek、内网网关……
    # `is_active=1` 全局**恰好一条** —— 它就是「现在用哪个」（由 `llm.set_active()`
    # 维护，不要手写 UPDATE 绕过它）。列表里能切、能测、能改，就是需求里那句
    # 「可先配置多个模型，可切换」。
    # ⚠ Key 是明文存本地库（自己的机器、自己的 Key）；但**绝不允许回给前端** ——
    #   `llm.list_profiles()` / `public_cfg()` 只回「有没有存」和尾号提示。
    # ⚠ name 唯一：列表里靠它区分两条都用 DeepSeek 的配置（模型名不同）。
    """
    CREATE TABLE IF NOT EXISTS ai_profile (
        id            INT AUTO_INCREMENT PRIMARY KEY,
        name          VARCHAR(60)  NOT NULL COMMENT '配置名（唯一，列表里靠它区分）',
        preset        VARCHAR(24)  NOT NULL DEFAULT 'custom' COMMENT '接入预设 key',
        kind          VARCHAR(16)  NOT NULL DEFAULT 'openai' COMMENT '协议：ollama / openai',
        base_url      VARCHAR(255) NOT NULL DEFAULT '' COMMENT '接口地址',
        model         VARCHAR(120) NOT NULL DEFAULT '' COMMENT '模型名',
        api_key       VARCHAR(255) NOT NULL DEFAULT '' COMMENT 'API Key（明文存本地，绝不下发）',
        temperature   DECIMAL(4,2) NOT NULL DEFAULT 0.30 COMMENT '采样温度',
        max_tokens    INT          NOT NULL DEFAULT 1024 COMMENT '单次回复上限',
        timeout       INT          NOT NULL DEFAULT 120 COMMENT '读超时（秒）',
        system_prompt TEXT                  COMMENT '系统提示词',
        is_active     TINYINT      NOT NULL DEFAULT 0 COMMENT '当前使用（全局恰好一条）',
        created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at    DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uk_ai_profile_name (name),
        KEY idx_ai_profile_active (is_active)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='AI 模型接入配置（多条可切换）'
    """,
    # ---------------- AI 分析语句模板 ----------------
    # 「AI 分析」用的提示词模板。变量用 `{股票名称}` / `{本地数据}` 这种带花括号的写法
    # （唯一口径在 core/llm.TEMPLATE_VARS），渲染同样只有 `llm.render()` 一处。
    # name 唯一：内置模板按名字幂等补齐（与 pool_strategy 的内置策略同一套做法）。
    """
    CREATE TABLE IF NOT EXISTS ai_prompt (
        id         INT AUTO_INCREMENT PRIMARY KEY,
        name       VARCHAR(60)  NOT NULL COMMENT '模板名（唯一）',
        template   TEXT         NOT NULL COMMENT '提示词模板（含 {股票名称} 这类变量）',
        is_default TINYINT      NOT NULL DEFAULT 0 COMMENT '是否默认模板（全局只留一条）',
        builtin    TINYINT      NOT NULL DEFAULT 0 COMMENT '内置（可改文案，不可删）',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uk_ai_prompt_name (name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='AI 分析语句模板'
    """,
    # ---------------- AI 分析记录 ----------------
    # 点一次「AI 分析」就落一行：**先插 running 再后台跑**，跑完回写 content。
    # 这样页面刷新/服务重启都不至于把结果丢了，历史也能回看（这是需求里
    # 「ai分析内容mysql存储」的落点）。
    # `status`：running / done / error —— 前端靠轮询这一列决定「还在转」还是「出结果」。
    """
    CREATE TABLE IF NOT EXISTS ai_analysis (
        id            BIGINT AUTO_INCREMENT PRIMARY KEY,
        ts_code       VARCHAR(12) NOT NULL COMMENT 'TS代码',
        name          VARCHAR(40)          COMMENT '股票名称（当时的）',
        trade_date    DATE                 COMMENT '基准日（列表里那一行是哪天）',
        source        VARCHAR(20) NOT NULL DEFAULT 'stock' COMMENT '入口：stock 诊断页 / screen 今日选股',
        template_id   INT                  COMMENT '用的模板 id（模板删了会变 NULL）',
        template_name VARCHAR(60)          COMMENT '模板名（留一份快照，模板改名也能回看）',
        prompt        TEXT                 COMMENT '渲染后真正发给模型的用户提示词',
        provider      VARCHAR(20)          COMMENT 'ollama / openai',
        model         VARCHAR(80)          COMMENT '模型名',
        status        VARCHAR(16) NOT NULL DEFAULT 'running' COMMENT 'running/done/error',
        content       MEDIUMTEXT           COMMENT '模型输出',
        error         VARCHAR(500)         COMMENT '失败原因（人话）',
        elapsed_ms    INT                  COMMENT '耗时毫秒',
        tokens        INT                  COMMENT '总 token（接口给了才有）',
        created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
        ended_at      DATETIME,
        KEY idx_code_time (ts_code, created_at),
        KEY idx_time (created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='AI 分析记录（个股亮点/消息面等）'
    """,
    # ---------------- 同步日志 ----------------
    """
    CREATE TABLE IF NOT EXISTS sync_log (
        id         INT AUTO_INCREMENT PRIMARY KEY,
        task       VARCHAR(40) NOT NULL,
        date_from  DATE,
        date_to    DATE,
        rows_cnt   INT DEFAULT 0,
        status     VARCHAR(20) DEFAULT 'running',
        message    TEXT,
        started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        ended_at   DATETIME
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='数据同步日志'
    """,
]


def _connect(with_db: bool = True):
    kw = dict(MYSQL)
    if not with_db:
        kw.pop("database", None)
    kw["autocommit"] = True
    kw["cursorclass"] = DictCursor
    conn = pymysql.connect(**kw)
    if DB_FAST_IMPORT:
        # 批量导入大量行情时降低写入开销（仅影响本连接）
        stmts = [
            "SET SESSION innodb_flush_log_at_trx_commit=2",
            "SET SESSION sql_log_bin=0",
        ]
        with conn.cursor() as cur:
            for stmt in stmts:
                try:
                    cur.execute(stmt)
                except Exception:  # noqa: BLE001
                    pass
    return conn


def get_conn():
    """线程内复用连接。"""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect()
        _local.conn = conn
    else:
        try:
            conn.ping(reconnect=True)
        except Exception:
            conn = _connect()
            _local.conn = conn
    return conn


@contextlib.contextmanager
def cursor(dict_row: bool = True):
    conn = get_conn()
    cur = conn.cursor() if dict_row else conn.cursor(pymysql.cursors.Cursor)
    try:
        yield cur
    finally:
        cur.close()


def _logged(op: str, sql: str, args, fn) -> Any:
    """包一层：成功记 DEBUG（含耗时/行数），失败记 ERROR 后原样抛出。"""
    t0 = time.perf_counter()
    try:
        out = fn()
    except Exception as e:  # noqa: BLE001
        _sql_log.error("SQL 失败 %s %.1fms | %s | %s: %s", op,
                       (time.perf_counter() - t0) * 1000, _log.sqlish(sql),
                       type(e).__name__, e)
        raise
    _log.log_sql(op, sql, args, (time.perf_counter() - t0) * 1000,
                 len(out) if isinstance(out, list) else None)
    return out


def query(sql: str, args: Sequence | dict | None = None) -> list[dict]:
    def _go():
        with cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()
    return _logged("SELECT", sql, args, _go)


def query_one(sql: str, args: Sequence | dict | None = None) -> dict | None:
    rows = query(sql, args)
    return rows[0] if rows else None


def scalar(sql: str, args: Sequence | dict | None = None) -> Any:
    row = query_one(sql, args)
    if not row:
        return None
    return list(row.values())[0]


def execute(sql: str, args: Sequence | dict | None = None) -> int:
    """执行写操作，返回**受影响行数**（不是自增 id，取 id 请用 insert）。"""
    if isinstance(args, (list, tuple)):
        args = _clean_row(args)
    t0 = time.perf_counter()
    try:
        with cursor() as cur:
            n = cur.execute(sql, args)
    except Exception as e:  # noqa: BLE001
        _sql_log.error("SQL 失败 WRITE %.1fms | %s | %s: %s",
                       (time.perf_counter() - t0) * 1000, _log.sqlish(sql),
                       type(e).__name__, e)
        raise
    _log.log_sql("WRITE", sql, args, (time.perf_counter() - t0) * 1000, n)
    return n


def insert(sql: str, args: Sequence | dict | None = None) -> int:
    """执行 INSERT 并返回新行的自增 id。

    注意别写成 `db.execute(...) or db.scalar("SELECT LAST_INSERT_ID()")` ——
    execute 返回的是受影响行数，INSERT 成功时恒为 1（truthy），
    那个 or 永远不走，拿到的是 1 而不是真实 id。
    """
    if isinstance(args, (list, tuple)):
        args = _clean_row(args)
    t0 = time.perf_counter()
    try:
        with cursor() as cur:
            cur.execute(sql, args)
            rid = int(cur.lastrowid or 0)
    except Exception as e:  # noqa: BLE001
        _sql_log.error("SQL 失败 INSERT %.1fms | %s | %s: %s",
                       (time.perf_counter() - t0) * 1000, _log.sqlish(sql),
                       type(e).__name__, e)
        raise
    _log.log_sql("INSERT", sql, args, (time.perf_counter() - t0) * 1000)
    _sql_log.debug("lastrowid=%s", rid)
    return rid


def executemany(sql: str, args: Iterable[Sequence]) -> int:
    rows = [_clean_row(r) for r in args]
    t0 = time.perf_counter()
    with cursor() as cur:
        n = cur.executemany(sql, rows)
    _log.log_sql("MANY", sql, None, (time.perf_counter() - t0) * 1000, n)
    return n


def _clean_row(row: Sequence) -> tuple:
    """把 numpy 类型 / NaN / NaT / Timestamp 转成 pymysql 能处理的 Python 值。"""
    out = []
    for v in row:
        if v is None:
            out.append(None)
            continue
        if isinstance(v, (np_bool,)):
            out.append(bool(v))
            continue
        if isinstance(v, (np_int,)):
            out.append(int(v))
            continue
        if isinstance(v, (np_float,)):
            v = float(v)
        if isinstance(v, float):
            out.append(None if v != v else v)
            continue
        if isinstance(v, (np_generic,)):
            out.append(v.item())
            continue
        if isinstance(v, (pd_Timestamp, datetime.datetime, datetime.date)):
            out.append(str(v)[:10] if not isinstance(v, datetime.datetime) else v)
            continue
        # pandas NaT / NA
        try:
            if v is pd_NaT or (not isinstance(v, (str, bytes, int, float, bool))
                               and pd_isna(v)):
                out.append(None)
                continue
        except (TypeError, ValueError):
            pass
        out.append(v)
    return tuple(out)


def insert_many(sql: str, rows: list[Sequence], chunk: int = 2000) -> int:
    """分批写入，返回 **MySQL 的受影响行数**（自动清洗 NaN / numpy 类型）。

    ⚠ 返回值**不是「你传进来多少行」**，也**不是「写了几行」**：`INSERT … ON DUPLICATE
    KEY UPDATE` 命中已有行且值没变时受影响行数是 **0**（新插入算 1、真改动了算 2）。
    所以它**不能**直接当「写入行数」展示给用户 —— 重新合成周线时整段都覆盖写了一遍，
    回报却是 0，现象就是页面上永远「周线写入 0 行」（数据其实是对的）。
    要展示「本次处理/写入了多少行」，就自己数传进来的 `len(rows)`（见 `sync.build_weekly`）。
    """
    if not rows:
        return 0
    total = 0
    t0 = time.perf_counter()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for i in range(0, len(rows), chunk):
                part = [_clean_row(r) for r in rows[i: i + chunk]]
                total += cur.executemany(sql, part)
    except Exception as e:  # noqa: BLE001
        _sql_log.error("SQL 失败 BATCH %.1fms rows=%s | %s | %s: %s",
                       (time.perf_counter() - t0) * 1000, len(rows),
                       _log.sqlish(sql), type(e).__name__, e)
        raise
    ms = (time.perf_counter() - t0) * 1000
    _log.log_sql(f"BATCH({(len(rows) + chunk - 1) // chunk}批)", sql, None, ms, total)
    if ms >= 1000:
        _sql_log.info("批量写入 %s 条，耗时 %.0fms", total, ms)
    return total


def safe_count(table: str, timeout_ms: int = 1500) -> tuple[int, bool]:
    """快速计数：优先精确 COUNT(*)，超过 timeout_ms 未完成则退回估算值。

    返回 (行数, 是否为精确值)。
    """
    cur = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(f"SET SESSION max_execution_time={int(timeout_ms)}")
        cur.execute(f"SELECT COUNT(*) AS c FROM `{table}`")
        row = cur.fetchone()
        cur.execute("SET SESSION max_execution_time=0")
        return int(row["c"] if isinstance(row, dict) else row[0]), True
    except Exception:
        try:
            if cur is not None:
                cur.execute("SET SESSION max_execution_time=0")
        except Exception:  # noqa: BLE001
            pass
        try:
            est = scalar(
                "SELECT table_rows FROM information_schema.tables "
                "WHERE table_schema=%s AND table_name=%s",
                (MYSQL["database"], table))
            return int(est or 0), False
        except Exception:  # noqa: BLE001
            return 0, False
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:  # noqa: BLE001
                pass


def init_schema() -> None:
    """创建数据库与全部表。"""
    db_name = MYSQL["database"]
    conn = _connect(with_db=False)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
                f"DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci"
            )
    finally:
        conn.close()

    conn = _connect()
    try:
        with conn.cursor() as cur:
            for ddl in SCHEMA_SQL:
                cur.execute(ddl)
    finally:
        conn.close()
    _migrate()


# 增量迁移：老库缺列时补上，避免升级后报 Unknown column
MIGRATIONS: list[tuple[str, str, str]] = [
    ("backtest_trade", "fee",
     "ALTER TABLE backtest_trade ADD COLUMN fee DECIMAL(16,2) "
     "COMMENT '买入+卖出手续费合计（含印花税）' AFTER sell_reason"),
    # 买卖策略列表支持拖拽排序（2026-10-09 用户需求）：加一列存显示顺序。
    # ⚠ 老库里所有行都是 0 ⇒ 排序退化成「同值按 id」，跟以前完全一样，
    #   所以这个迁移不动任何既有顺序，用户拖一次才开始生效。
    ("strategy", "sort",
     "ALTER TABLE strategy ADD COLUMN sort INT NOT NULL DEFAULT 0 "
     "COMMENT '列表显示顺序（拖拽改；同值时按 id）' AFTER builtin"),
    # 选股逻辑统一归「选股策略」之后，静态过滤项（剔ST/板块/上市天数/最小成交额）
    # 从 strategy.config.pool 搬到 pool_strategy.filt。老库没这列要补上，
    # 否则保存选股策略直接 Unknown column。
    ("pool_strategy", "filt",
     "ALTER TABLE pool_strategy ADD COLUMN filt JSON "
     "COMMENT '静态过滤项(剔ST/板块/上市天数/最小成交额)' AFTER conds"),
    # 滑点成本与手续费分开记：fee 是交给券商的（佣金+印花税），滑点是成交价吃亏的隐性成本，
    # 以前只藏在买卖价里、统计里看不见。老库没这列要补上，否则回测落库直接 Unknown column。
    ("backtest_trade", "slip_cost",
     "ALTER TABLE backtest_trade ADD COLUMN slip_cost DECIMAL(16,2) "
     "COMMENT '滑点成本(买入上浮+卖出下调合计)' AFTER fee"),
    # 回测进度落库：任务线程只活在内存（core/jobs._JOBS），页面一刷新就找不到它了 ——
    # 进度条凭空消失、列表里的 running 也永远停在原地。老库没这几列要补上，
    # 否则「刷新后接回进度」和列表里的进度百分比直接 Unknown column。
    ("backtest_run", "progress",
     "ALTER TABLE backtest_run ADD COLUMN progress DOUBLE DEFAULT 0 "
     "COMMENT '运行进度 0~1（刷新页面后进度条靠它接回来）' AFTER log"),
    ("backtest_run", "progress_msg",
     "ALTER TABLE backtest_run ADD COLUMN progress_msg VARCHAR(200) "
     "COMMENT '当前阶段说明（进度条上那行字）' AFTER progress"),
    ("backtest_run", "job_id",
     "ALTER TABLE backtest_run ADD COLUMN job_id VARCHAR(20) "
     "COMMENT '内存任务 id：进度/日志的实时来源，服务重启即失效' AFTER progress_msg"),
    # 选股条件快照：`universe` 列只是一句人话标签（「ALL · 昨日涨停今日大阴」），
    # 池子一改，标签背后的条件就跟当时跑了什么没关系了。列表里悬浮要看的是**当时**筛了什么，
    # 所以启动回测时把池子描述原样存下来（老记录为 NULL → 只能按标签反查当前池，界面会标注）。
    ("backtest_run", "universe_spec",
     "ALTER TABLE backtest_run ADD COLUMN universe_spec JSON "
     "COMMENT '回测当时的选股条件快照（base/where/filt/timeframe）' AFTER universe"),
    # 概念板块行情的**来源标记**：东财 push2 限频很狠（实测连打十几发就整段断连，
    # 换节点/带 ut/预热 cookie 都没用），所以 core/concept.py 有个「用本库行情等权自算」
    # 的兜底。两种口径**必须能区分** —— 否则页面上那个「+3.2%」到底是谁算的都说不清。
    # 老库没这列要补上，否则概念板板块行情写库直接 Unknown column。
    ("concept_basic", "quote_src",
     "ALTER TABLE concept_basic ADD COLUMN quote_src VARCHAR(12) "
     "COMMENT '行情来源 eastmoney=东财 push2 / local=本库行情等权自算' AFTER quoted_at"),
    # 提醒支持周K（2026-10-08，需求原话「提醒也可以添加周线指标提醒」）：`freq` 决定判哪张图 ——
    # daily 判日线那根、weekly 判**周线**那根（数据源 `weekly_kline`，只含已收盘的完整周）。
    # 老库没这列要补上，否则页面一存规则就 Unknown column（增量迁移幂等，不会动已有数据）。
    ("watch_alert", "freq",
     "ALTER TABLE watch_alert ADD COLUMN freq VARCHAR(8) NOT NULL DEFAULT 'daily' "
     "COMMENT '判哪张图：daily 日K / weekly 周K' AFTER op"),
    # 提醒的**第二种规则**：绑定选股策略，入池即提醒（2026-10-10 需求
    # 「我的关注里面的提醒，希望可以通过绑定选股策略规则，如果选股命中则提醒」）。
    # 老行 kind 默认 'price' ⇒ 与加这两列之前的行为**逐字一致**（老库不需要回填）。
    ("watch_alert", "kind",
     "ALTER TABLE watch_alert ADD COLUMN kind VARCHAR(8) NOT NULL DEFAULT 'price' "
     "COMMENT '规则类型：price 指标/价位 · pool 选股命中' AFTER ts_code"),
    ("watch_alert", "pool_id",
     "ALTER TABLE watch_alert ADD COLUMN pool_id INT "
     "COMMENT 'kind=pool 时引用的选股策略 id' AFTER kind"),
    # 选股策略自带周期（2026-10-10 需求「选股策略中也要有日k和周k的选择项，策略名称后面
    # 如果是周k则标记周」）。老行默认 'daily' ⇒ 与加这列之前的行为**逐字一致**，不需要回填。
    ("pool_strategy", "timeframe",
     "ALTER TABLE pool_strategy ADD COLUMN timeframe VARCHAR(8) NOT NULL DEFAULT 'daily' "
     "COMMENT '周期：daily 日K / weekly 周K（条件在周线上求值）' AFTER base"),
]

# 增量迁移：老库缺索引时补上。
# idx_limit_stat 是**覆盖索引**，专供总览页「涨跌停家数」按日期区间统计用：
# 该查询只需要 trade_date/ts_code/close/pre_close 四列，有它就能走 index-only 扫描
# （实测 3 个月区间：无此索引退化为全表扫 340 万行 ≈10s；有则 ≈0.3s）。
INDEX_MIGRATIONS: list[tuple[str, str, str]] = [
    ("daily_kline", "idx_limit_stat",
     "ALTER TABLE daily_kline ADD INDEX idx_limit_stat "
     "(trade_date, ts_code, close, pre_close)"),
]

# 增量迁移：老库列宽不够时**加宽**（只加宽、不缩窄，避免截断已有数据）。
# 起因：holder_trade.holder_name 原本是 VARCHAR(160)，但 tushare 会把同一公告里
# 多只私募基金的名字用逗号拼成一行（实测 170 字符）→ 插入抛 1406、那天整体写不进去。
# (table, column, 目标字符长度, DDL)
WIDEN_MIGRATIONS: list[tuple[str, str, int, str]] = [
    ("holder_trade", "holder_name", 512,
     "ALTER TABLE holder_trade MODIFY COLUMN holder_name VARCHAR(512) "
     "COMMENT '股东名称（tushare 会把多个基金名用逗号拼成一行，实测到 170 字符，别按 160 截）'"),
    # 自选股备注原本 200：批量导入一份题材清单时，那条「文本标记」实测最长 400 字符
    # （31/515 条超 200）⇒ 按 200 截会**静默丢尾**。只加宽不缩窄。
    ("watch_stock", "note", 512,
     "ALTER TABLE watch_stock MODIFY COLUMN note VARCHAR(512) "
     "COMMENT '备注（批量导入的题材标记实测到 400 字符，别按 200 截）'"),
]

# 增量迁移：老库**多出来的列**要删掉（只删、不加；加列走上面的 MIGRATIONS）。
# 场景：某个能力被彻底移除后，建表语句里那列没了，但老库还留着 —— 留着不报错，
# 却会让「库里有什么」和「代码认什么」两套口径并存，下一个人照着旧列写查询就是坑。
# ⚠ 删列是**不可逆**的，所以这里只放「已经确定不再需要、且没有别的东西再读它」的列。
DROP_MIGRATIONS: list[tuple[str, str]] = [
    # 「掉出池子即卖出」彻底移除（2026-10-10 用户要求「这个属于买卖策略，选股策略只管选股」）：
    # 选股策略不再承担任何卖出语义，这一列在整个代码库里已无引用。
    ("pool_strategy", "pool_exit"),
]


def _migrate() -> None:
    """幂等地补齐新增列 / 索引。"""
    for table, column, ddl in MIGRATIONS:
        try:
            hit = scalar(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name=%s AND column_name=%s",
                (MYSQL["database"], table, column),
            )
            if not hit:
                execute(ddl)
        except Exception:  # noqa: BLE001
            pass

    for table, index, ddl in INDEX_MIGRATIONS:
        try:
            hit = scalar(
                "SELECT COUNT(*) FROM information_schema.statistics "
                "WHERE table_schema=%s AND table_name=%s AND index_name=%s",
                (MYSQL["database"], table, index),
            )
            if not hit:
                execute(ddl)
        except Exception:  # noqa: BLE001
            pass

    # 列宽只往大改：老库 VARCHAR(160) 装不下 170 字符的股东名（会抛 1406）
    for table, column, want, ddl in WIDEN_MIGRATIONS:
        try:
            cur_len = scalar(
                "SELECT character_maximum_length FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name=%s AND column_name=%s",
                (MYSQL["database"], table, column),
            )
            if cur_len is not None and int(cur_len) < want:
                _sql_log.info("加宽列 %s.%s：%s → %s", table, column, cur_len, want)
                execute(ddl)
        except Exception as e:  # noqa: BLE001
            # 迁移失败**不能静默**：列宽没加上，下次同步还会在同一个地方抛 1406
            _sql_log.warning("加宽列 %s.%s 失败：%s: %s", table, column, type(e).__name__, e)

    # 删列：老库还留着、现在已无人读的列。先查存在性再删（幂等；新库里本来就没有）。
    for table, column in DROP_MIGRATIONS:
        try:
            hit = scalar(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name=%s AND column_name=%s",
                (MYSQL["database"], table, column),
            )
            if hit:
                _sql_log.info("删除废弃列 %s.%s", table, column)
                execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        except Exception as e:  # noqa: BLE001
            # 删列失败不拖垮启动，但必须留痕：列还在，下次启动会再试一次
            _sql_log.warning("删除废弃列 %s.%s 失败：%s: %s", table, column, type(e).__name__, e)


if __name__ == "__main__":
    _log.setup_logging()
    init_schema()
    _sql_log.info("schema ok")
