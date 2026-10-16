/* 通用工具：API 封装、提示、数字格式化、任务轮询、图表 */
(function () {
  'use strict';

  window.$ = (s, r) => (r || document).querySelector(s);
  window.$$ = (s, r) => Array.from((r || document).querySelectorAll(s));

  window.esc = function (v) {
    if (v === null || v === undefined) return '';
    return String(v).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  };

  /* ---------------- 请求指示（全站共用） ----------------
     「点了没反应」是上一版最常挨的反馈：任何一页在打 /api 时，顶部走一条细进度条。
     放在 api() 里 = **所有页面自动拥有**，不用每页各写一套（各写一套必然有漏的）。

     ⚠ 两个时间参数都是在治「闪」：
       · BUSY_DELAY 之内就回来的请求（库里查一下、几十毫秒）**根本不显示** —— 否则
         每点一下都闪一下，看久了比没有更烦。
       · 一旦显示就至少留 BUSY_MIN —— 否则进度条出现一帧立刻消失，像画面抽了一下。
     ⚠ 轮询类调用（任务进度 900ms 一拍、侧栏状态）要传 `{quiet:1}`：它们天生高频，
       跟着走会变成「进度条常驻」，真正的取数反而看不出来。 */
  const BUSY_DELAY = 130, BUSY_MIN = 350;
  let busyN = 0, busyTimer = null, busyAt = 0;
  function busyEl() { return $('#busy-bar'); }
  function busyShow() {
    const el = busyEl();
    if (el) el.classList.add('on');
    busyAt = Date.now();
  }
  function busyHide() {
    clearTimeout(busyTimer);
    busyTimer = setTimeout(() => {
      if (busyN) return;                       // 期间又来新请求了，接着亮
      const el = busyEl();
      if (el) el.classList.remove('on');
    }, Math.max(0, BUSY_MIN - (Date.now() - busyAt)));
  }
  function busyInc() {
    busyN++;
    if (busyN === 1) {
      clearTimeout(busyTimer);
      busyTimer = setTimeout(() => { if (busyN) busyShow(); }, BUSY_DELAY);
    }
  }
  function busyDec() {
    busyN = Math.max(0, busyN - 1);
    if (!busyN) busyHide();
  }
  /* 探针用它读「现在有几发在飞」（验证脚本不依赖内部变量名） */
  window.busyCount = () => busyN;
  window.busyBarOn = () => { const el = busyEl(); return !!(el && el.classList.contains('on')); };

  /* ---------------- API ---------------- */
  async function api(url, opts) {
    opts = opts || {};
    const cfg = { method: opts.method || 'GET', headers: {} };
    if (opts.body !== undefined) {
      cfg.headers['Content-Type'] = 'application/json';
      cfg.body = JSON.stringify(opts.body);
    }
    if (!opts.quiet) busyInc();
    try {
      const res = await fetch(url, cfg);
      let json;
      try { json = await res.json(); } catch (e) { throw new Error('服务器返回异常 (' + res.status + ')'); }
      if (!json.ok) {
        // 带上 HTTP 状态码：调用方要区分「你传错了(4xx)」与「服务端炸了(5xx)」时用得上
        const err = new Error(json.error || ('请求失败 ' + res.status));
        err.status = res.status;
        throw err;
      }
      return json.data;
    } finally {
      if (!opts.quiet) busyDec();
    }
  }
  window.api = api;

  window.apiGet = (u, o) => api(u, o);
  window.apiPost = (u, b, o) => api(u, Object.assign({ method: 'POST', body: b || {} }, o || {}));
  window.apiDel = (u, o) => api(u, Object.assign({ method: 'DELETE' }, o || {}));

  /* ---------------- 容器状态块（加载中 / 出错 / 空） ----------------
     列表卡「正在取数 / 取失败 / 没数据」都摆同一种块。
     以前失败只弹一个 toast，3 秒后消失、页面上什么都不留 —— 用户看到的就是
     「点了没反应」，或者更糟：留着上一次的数据，当成查成功了。

     `kind`：'loading' | 'error' | 'empty'（传 null/'' = 清空）
     ⚠ loading 的文案里保留「加载中…」四个字 —— 既有探针靠它判断列表画完没有。 */
  const BOX_ICO = { error: '⚠', empty: '○' };
  window.boxHTML = function (kind, title, desc) {
    if (!kind) return '';
    const spin = kind === 'loading' ? '<span class="spin"></span>' : '';
    const ico = BOX_ICO[kind] || '';
    return '<div class="box-st ' + kind + '">'
      + (spin || (ico ? '<span class="box-ico">' + ico + '</span>' : ''))
      + '<div class="box-t">' + esc(title || '') + '</div>'
      + (desc ? '<div class="box-d">' + esc(desc) + '</div>' : '')
      + '</div>';
  };
  window.boxState = function (el, kind, title, desc) {
    if (typeof el === 'string') el = $(el);
    if (!el) return;
    el.innerHTML = window.boxHTML(kind, title, desc);
  };
  /* 取数失败的统一收尾：**toast + 页面上留痕**，两样都要。
     只 toast → 三秒后消失，看着像没反应；只留痕 → 用户盯着自己点的地方等提示。 */
  window.fail = function (el, e, title) {
    const msg = (e && e.message) || '请求失败';
    toast(msg, 'err', 7000);
    if (el) window.boxState(el, 'error', title || '加载失败', msg);
    return msg;
  };

  /* ---------------- toast ---------------- */
  window.toast = function (msg, type, ms) {
    const wrap = $('#toast-wrap');
    if (!wrap) return null;
    const el = document.createElement('div');
    el.className = 'toast ' + (type || '');
    el.textContent = msg;
    el.title = '点击关闭';
    wrap.appendChild(el);
    /* 出错 / 警告停留久一点：这两类是要人读完再动手的，3 秒飘走等于没提示。
       （原来一律 3.2 秒 —— 用户正低头看代码框，抬头时提示已经没了。） */
    const life = ms || (type === 'err' ? 6000 : (type === 'warn' ? 4200 : 3200));
    let t = setTimeout(kill, life);
    function kill() {
      clearTimeout(t);
      el.style.transition = '.3s';
      el.style.opacity = '0';
      setTimeout(() => el.remove(), 320);
    }
    el.addEventListener('click', kill);        // 看完了自己点掉，不必等
    return el;
  };

  /* ---------------- 提交类按钮防连点 ----------------
     提交类按钮（开始回测 / 重测 / 同步 / 保存 / 删除 …）被点两下 = 两条记录、两个后台任务，
     `fetch` 不会自动去重。这里给三道保护，共用同一个锁标志（`el.dataset.busy`）：

       ① 请求在飞 → 后续点击**直接忽略**。这是主防线，覆盖「网络慢、以为没点上、又点一下」
       ② 两次点击间隔 < LOCK_MS → 忽略。防手抖连点；人双击间隔约 80~300ms，600ms 足够
       ③ 锁住期间按钮置 `disabled`，用户**看得见**自己点到了，不用猜
       ④ 同时挂 `.is-busy`（CSS 转圈 + 变暗）—— 同步那种跑几分钟的任务，
          「按钮灰了」和「按钮在转」是两种不同的安心程度

     用法：`el.onclick = guardClick(el, async () => { ... })`。
     也接受现成的 handler：`guardClick(el, loadRuns)`。

     ⚠ 解锁必须写在 `finally` 里。否则一次报错就把按钮永久锁死，「点不动」比「能连点」更糟。
     ⚠ 别改成「延时执行」的 debounce：提交类按钮要的是**立刻响应 + 忽略重复**，
       延迟 300ms 才发请求只会让页面显得卡。 */
  const LOCK_MS = 600;
  window.guardClick = function (el, fn) {
    if (!el) return fn;
    return async function (ev) {
      const now = Date.now();
      if (el.dataset.busy === '1') return;                              // ① 上一发还在飞
      if (now - Number(el.dataset.lockTs || 0) < LOCK_MS) return;       // ② 手抖连点
      el.dataset.busy = '1';
      el.dataset.lockTs = String(now);
      const wasDisabled = el.disabled;
      el.disabled = true;                                               // ③ 看得见的反馈
      el.classList.add('is-busy');                                      // ④ 转圈（长任务靠它）
      try {
        return await fn.call(this, ev);
      } finally {
        el.dataset.busy = '';
        el.classList.remove('is-busy');
        el.disabled = wasDisabled;
      }
    };
  };

  /* ---------------- 数字格式化 ---------------- */
  const nf = (v, d) => (v === null || v === undefined || isNaN(v)) ? '—'
    : Number(v).toLocaleString('zh-CN', { minimumFractionDigits: d === undefined ? 2 : d, maximumFractionDigits: d === undefined ? 2 : d });

  window.fmt = {
    num: nf,
    int: (v) => (v === null || v === undefined || isNaN(v)) ? '—' : Number(v).toLocaleString('zh-CN'),
    pct: (v, d) => (v === null || v === undefined || isNaN(v)) ? '—' : (Number(v) > 0 ? '+' : '') + Number(v).toFixed(d === undefined ? 2 : d) + '%',
    price: (v) => (v === null || v === undefined || isNaN(v)) ? '—' : Number(v).toFixed(2),
    big: (v) => {   // 单位：元 / 千元 自适应（**不含市值** —— 市值是万元，见 fmt.mv）
      if (v === null || v === undefined || isNaN(v)) return '—';
      const n = Number(v);
      if (Math.abs(n) >= 1e8) return (n / 1e8).toFixed(2) + '亿';
      if (Math.abs(n) >= 1e4) return (n / 1e4).toFixed(2) + '万';
      return n.toFixed(2);
    },
    /* 市值：`daily_basic.total_mv / circ_mv` 库里单位是**万元**（22704900 万元 = 2270.49 亿）。
       ⚠ 不能走 fmt.big —— 那是给「元」写的自适应，万元值永远落在「万」档，
       2270 亿会显示成「2270.49万」。口径与后端 `indicators._mv_text`（诊断页基本面
       卡片）一致：万元 → 亿，上万亿换单位。**结果是带单位的字符串**，只用于展示格。 */
    mv: (v) => {
      if (v === null || v === undefined || isNaN(v)) return '—';
      const yi = Number(v) / 1e4;
      if (Math.abs(yi) >= 1e4) return (yi / 1e4).toFixed(2) + '万亿';
      return yi.toFixed(2) + '亿';
    },
    /* 同上，但只要**数值**（单位写在列头里）—— 导出 CSV 用：表里显示「总市值(亿)」，
       导出就得是同一口径的数值，否则导出来跟屏幕上看到的不是一个量级。 */
    mvNum: (v) => (v === null || v === undefined || isNaN(v)) ? '' : (Number(v) / 1e4).toFixed(2),
    money: (v) => (v === null || v === undefined || isNaN(v)) ? '—' : '¥' + Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 2 }),
    cls: (v) => (v === null || v === undefined || isNaN(v) || Number(v) === 0) ? 'flat' : (Number(v) > 0 ? 'up' : 'down'),
    sign: (v) => (v === null || v === undefined || isNaN(v)) ? '—' : (Number(v) > 0 ? '+' : '') + nf(v),
    /* 耗时（毫秒 → 人读的秒/毫秒）。模型调用、任务耗时都显示成这个口径，
       别在页面里各写一份 `(ms/1000).toFixed(1)+'s'`（那里早晚出现 12.0s 与 12s 两种写法）。 */
    ms: (v) => (v === null || v === undefined || isNaN(v)) ? '—'
      : (Number(v) >= 1000 ? (Number(v) / 1000).toFixed(1) + 's' : Math.round(Number(v)) + 'ms'),
  };

  /* ---------------- 结果表的「额外列」 ----------------
     列定义**由后端给**（`indicators.col_spec`）：列 = 条件/评分/条件池里真正引用到的字段
     （筹码/两融、技术指标、估值都算），后端没给的字段名前端一个也不写死 ——
     所以这里只按后端的 `fmt` 去调 `fmt.*`、按 `color` 决定要不要染涨跌色。
     ⚠ 实现在这里**只有一份**：今日选股页（`screen.html`）与选股策略试跑页（`pool.html`）
       共用。别在页面里再写一遍 —— 两份早晚跑偏（一处改了另一处忘）。
     `extraCols` 收「带 extra_cols 的响应」本身（选股结果 / 试跑结果都长这样）。 */
  window.extraCols = (r) => (r && r.extra_cols) || [];

  window.colText = function (c, v) {
    if (v === null || v === undefined || isNaN(v)) return '—';
    switch (c.fmt) {
      case 'big': return window.fmt.big(v);
      case 'mv': return window.fmt.mv(v);
      case 'pct': return window.fmt.pct(v, c.dec);
      case 'price': return window.fmt.price(v);
      case 'int': return window.fmt.int(v);
      default: return window.fmt.num(v, c.dec);
    }
  };

  window.colCls = (c, v) => (c.color === 'updown' ? window.fmt.cls(v) : '');

  /* ---------------- 条件文本里的 `#` 注释 ----------------
     口径与后端 `core/expr.py::strip_comment` 一致：`#` 之后（含 `#`）都算注释，
     行首 `#` ⇒ 整行都是注释。例：`total_mv > 300000  # 市值大于 30 亿（万元）`。
     ⚠ 前端只在「到底有没有真条件」这类**判断**上用它（试跑前的空条件提示、行数统计）；
     存库与回显一律保留原文 —— 那是用户写给自己的批注，别在这儿悄悄删掉。
     ⚠ 别叫 `COND`：买卖策略页的内联脚本里已经有 `const COND = ['buy_trigger',…]`
       （全局词法作用域是脚本间共享的，重名会直接 SyntaxError）。 */
  window.condText = {
    /* 与后端 `expr.strip_comment` 同一个口径：先把全角 `＃`（中文输入法全角标点打出来的
       就是它）归一到半角，再按 `#[^\n]*` 剥 —— 注释只吃到行尾，前一行注释不连坐下一行 */
    strip: (s) => String(s === null || s === undefined ? '' : s)
      .replace(/＃/g, '#').replace(/#[^\n]*/g, '').trim(),
    active: (s) => window.condText.strip(s) !== '',
    /* 多行文本 → 真正要执行的条件条数（空行与纯注释行不算） */
    count: (text) => String(text || '').split('\n').filter(x => window.condText.active(x)).length,
  };

  /* ---------------- 滑点单位 ----------------
     库里与回测参数一律是**小数**（0.001 = 千分之1 = 0.1%），界面统一按**百分数**填
     （填 0.1 表示 0.1%）。换算只在这里做一次 —— 买卖策略页、回测页都调它，
     页面里别再各写一份（写两处早晚对不上）。范围（min/max）不在这里，
     由 /api/config 的 slippage_range 给（唯一来源 core/strategy.SLIPPAGE_MIN/MAX）。 */
  window.SLIP = {
    STEP: 0.05,                                        // 界面步长（百分点）
    fromCfg: (v) => (v === null || v === undefined || v === '' || isNaN(Number(v)))
      ? '' : Number((Number(v) * 100).toFixed(4)),
    toCfg: (v) => (v === null || v === undefined || String(v).trim() === ''
      || isNaN(Number(v))) ? null : Number((Number(v) / 100).toFixed(6)),
  };

  window.reasonText = {
    signal: '信号卖出', stop_loss: '止损', take_profit: '止盈',
    max_hold: '持有到期', end: '回测结束', entry: '策略选股'
  };
  window.reasonTag = function (r) {
    return ({ signal: 'b', stop_loss: 'g', take_profit: 'r', max_hold: 'y',
              end: '' })[r] || '';
  };

  /* ---------------- 任务轮询 ---------------- */
  window.watchJob = function (jobId, onTick, onDone, onErr) {
    let stopped = false;
    const timer = setInterval(async () => {
      if (stopped) return;
      let j;
      // quiet：900ms 一拍，跟着顶部进度条走会让它常驻，真取数反而看不出来
      try { j = await apiGet('/api/jobs/' + jobId, { quiet: 1 }); }
      catch (e) { clearInterval(timer); onErr && onErr(e); return; }
      if (onTick) onTick(j);
      if (j.status === 'done') { clearInterval(timer); onDone && onDone(j); }
      else if (j.status === 'error') { clearInterval(timer); onErr && onErr(new Error(j.error || j.message)); }
    }, 900);
    return () => { stopped = true; clearInterval(timer); };
  };

  /* ---------------- 图表 ---------------- */
  const charts = {};
  window.mkChart = function (el, option) {
    if (typeof el === 'string') el = $(el);
    if (!el || !window.echarts) return null;
    let c = charts[el.id || (el.id = 'c' + Math.random().toString(36).slice(2))];
    if (c && c.isDisposed && c.isDisposed()) c = null;   // 弹窗关闭后实例已销毁
    if (!c) { c = echarts.init(el); charts[el.id] = c; }
    c.setOption(option, true);
    return c;
  };
  /* 弹窗里的图会被销毁，这里跳过已 dispose 的实例，避免 resize 抛错 */
  window.resizeCharts = function () {
    Object.keys(charts).forEach(k => {
      const c = charts[k];
      if (!c || (c.isDisposed && c.isDisposed())) { delete charts[k]; return; }
      try { c.resize(); } catch (e) { delete charts[k]; }
    });
  };
  window.addEventListener('resize', () => window.resizeCharts());

  /* 图表基础样式：走深色系（ECharts 拿不到 CSS 变量，只能在这写值）。
     改主题时**这两个对象要跟着 app.css 一起改** —— 否则会出现
     「白底坐标轴压在深色卡片上」那种半深不深的观感。 */
  window.baseAxis = {
    axisLine: { lineStyle: { color: '#2a3444' } },
    axisLabel: { color: '#8b95a9', fontSize: 11 },
    splitLine: { lineStyle: { color: '#1f2733' } }
  };

  window.tooltipBase = {
    trigger: 'axis',
    backgroundColor: 'rgba(16,21,29,.96)',
    borderColor: '#2a3444',
    borderWidth: 1,
    textStyle: { color: '#e6ebf4', fontSize: 12 },
    axisPointer: { type: 'cross', label: { backgroundColor: '#3a4863' } }
  };

  /* 合并坐标轴配置：**不能直接 Object.assign({...}, baseAxis)** —— baseAxis 里也带着
     axisLabel/splitLine，会把刚设的（比如 axisLabel.show:false）整个顶掉，
     症状是「本来只想让最下面一格显示日期，结果三格全冒出来了」。
     个股诊断页与交易复盘图都是「一个实例、多个 grid」，共用这一份。 */
  window.withBaseAxis = function (extra, axisLabel) {
    const out = Object.assign({}, window.baseAxis, extra);
    out.axisLabel = Object.assign({}, window.baseAxis.axisLabel, axisLabel || {});
    return out;
  };

  /* 图表图例样式（个股诊断页与交易复盘图共用一套） */
  window.CHART_LEGEND = {
    type: 'scroll', textStyle: { fontSize: 11, color: '#9aa6ba' },
    itemWidth: 14, itemHeight: 8,
    pageIconColor: '#4b83ff', pageIconInactiveColor: '#3a4863', pageTextStyle: { color: '#8b95a9' },
  };

  /* 下挂指标格的**线色与数值格式**：个股诊断页与交易复盘图都要画这套格子，
     定义放这一处（ECharts 拿不到 CSS 变量，颜色只能写值）。
     两页各写一份的话，同一次改动总会漏掉一边 —— 表现是「同一个 KDJ，
     诊断页是蓝/橙/紫、复盘图变成默认灰」。键与后端 catalog 的 group.key 对齐。 */
  window.CHART_PAL = {
    macd: ['#4b83ff', '#e08a00', '#8b95a9'],
    kdj:  ['#4b83ff', '#e08a00', '#8b5cf6'],
    rsi:  ['#4b83ff', '#e08a00', '#8b5cf6'],
    dmi:  ['#4b83ff', '#e08a00', '#8b5cf6'],
    cci:  ['#4b83ff', '#8b95a9'],
    wr:   ['#e08a00'],
    obv:  ['#4b83ff', '#e08a00'],
    vr:   ['#4b83ff'],
    atr:  ['#4b83ff'],
    vol:  ['#e08a00', '#4b83ff', '#8b5cf6'],
    mf:   ['#4b83ff', '#8b5cf6'],
  };

  /* 数值格式 token —— 后端的指标目录里每个 series 给一个（单位口径只在后端定一次） */
  window.CHART_FMT = {
    plain: v => Number(v).toFixed(2),
    price: v => window.fmt.price(v),
    vol: v => Math.abs(v) >= 1e4 ? (v / 1e4).toFixed(2) + '万手' : Number(v).toFixed(0) + '手',
    count: v => Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 0 }),
    money_wan: v => window.fmt.big(v),
    mv: v => window.fmt.mv(v),
  };

  /* ---------------- 侧栏状态 ---------------- */
  /* quiet：这两发是跟着页面主数据一起打的装饰性状态，让它去点亮顶部进度条
     会把「主数据在取」和「侧栏在刷」混成一条，看不出真正在等的是哪件事。 */
  async function refreshStatus(force) {
    try {
      const cfg = await apiGet('/api/config', { quiet: 1 });
      const st = await apiGet('/api/stats' + (force ? '?force=1' : ''), { quiet: 1 });
      const e1 = $('#side-end'); if (e1) e1.textContent = st.daily_end || '无数据';
      const e2 = $('#side-db'); if (e2) { e2.textContent = 'MySQL 正常'; e2.style.color = '#3fbc8a'; }
      const p1 = $('#pill-latest'); if (p1) p1.textContent = '最新交易日 ' + (cfg.latest_trade_date || '—');
      const p2 = $('#pill-rows'); if (p2) p2.textContent = '日线 ' + fmt.int(st.daily_rows) + ' 行 / ' + fmt.int(st.daily_codes) + ' 只';
      window.__stats = st; window.__cfg = cfg;
      document.dispatchEvent(new CustomEvent('status-loaded', { detail: { st: st, cfg: cfg } }));
    } catch (e) {
      const e2 = $('#side-db'); if (e2) { e2.textContent = '连接失败'; e2.style.color = '#ff7b70'; }
    }
  }
  window.refreshStatus = refreshStatus;
  document.addEventListener('DOMContentLoaded', refreshStatus);

  /* ---------------- 下拉框填充 ---------------- */
  window.fillSelect = function (el, items, valueKey, labelKey, selected) {
    if (!el) return;
    el.innerHTML = items.map(it => {
      const v = typeof it === 'string' ? it : it[valueKey];
      const l = typeof it === 'string' ? it : it[labelKey];
      return `<option value="${esc(v)}" ${String(v) === String(selected) ? 'selected' : ''}>${esc(l)}</option>`;
    }).join('');
  };

  window.loadStrategies = async function () { return await apiGet('/api/strategies'); };

  window.mkModal = function (title, bodyHtml, footHtml, onClose, wide) {
    const bg = document.createElement('div');
    bg.className = 'modal-bg';
    bg.innerHTML = `<div class="modal${wide ? ' wide' : ''}">
        <div class="modal-head"><h3>${esc(title)}</h3><div class="modal-acts"><button class="btn ghost" data-close>✕</button></div></div>
        <div class="modal-body">${bodyHtml}</div>
        ${footHtml ? `<div class="modal-foot">${footHtml}</div>` : ''}
      </div>`;
    document.body.appendChild(bg);
    let closed = false;
    const close = () => {
      if (closed) return;
      closed = true;
      bg.remove();
      if (onClose) { try { onClose(); } catch (e) { /* noop */ } }
    };
    $$('[data-close]', bg).forEach(b => b.onclick = close);
    bg.addEventListener('click', e => { if (e.target === bg) close(); });
    document.addEventListener('keydown', function esc(e) {
      if (e.key === 'Escape') { close(); document.removeEventListener('keydown', esc); }
    });
    return { el: bg, close: close };
  };

  /* ---------------- K线预览弹窗（列表里的「K线」按钮唯一入口） ----------------
     弹窗里嵌的是**诊断页的预览模式**（/stock/<代码>?embed=1，同源 iframe）。
     这样 K线 + 均线/BOLL + 下挂指标 + 信号标记 + 缩放同步只有诊断页那一份实现；
     在列表页里再画一遍，就等于养出第二个口径（改了一处、另一处不变，形状对口径错）。
     params 原样拼进 URL，由诊断页转给 /api/stock/<code>/detail
     （strategy_id / config / pool_id / bars / freq / trade_date）。

     列表页还会把**当页这些票**一起传进来（`list` + `index`）：弹窗右上角 ✕ 左边
     就多一个「下一个」，不关弹窗、不丢现场地一只只往下看。翻页**只换 iframe 的 src**
     （连标题与「在诊断页打开」的链接一起换），不重建弹窗 —— 重建会让遮罩和入场动画
     重放一次，看着像闪了一下。列表用的是**当页那张表的行序**，所以「下一个」就是
     「表里的下一行」，跟屏幕上看到的顺序一致。
     `paramsOf(item, i)` 可选：逐行口径不同的列表（如「已保存的选股结果」每行一个
     trade_date）用它算，翻过去仍是**那一行自己的**查询日，不会串成同一张表的口径。 */
  let klineModal = null;
  // 弹窗里用户手动切过的 K 线周期（'' = 还没切过，跟着每行自己的参数走）。
  // 翻「下一个」时保留 —— 切到周K是想按周线看这一批，不该翻一只就弹回日线。
  let klineFreq = '';
  // 由 klinePreview 挂上；iframe 里切了周期就调它。
  let klineRefreshFull = null;
  /* iframe（诊断页预览模式）里切了周期 → 只把「在诊断页打开」的链接换掉。
     不重建 iframe：重建会把用户刚切的那个周期又冲回去（src 一换就重新按 URL 取数）。
     只认**当前这个弹窗的那只 iframe** 发来的消息（比对 e.source），
     免得页面上别处的 postMessage 把链接改跑偏。 */
  window.addEventListener('message', function (e) {
    if (!klineModal || !klineRefreshFull) return;
    if (e.origin !== location.origin) return;
    const frame = klineModal.el.querySelector('iframe.kl-frame');
    if (!frame || e.source !== frame.contentWindow) return;
    const msg = e.data || {};
    if (msg.type === 'st-freq') klineRefreshFull(msg.freq);
  });

  window.klinePreview = function (opts) {
    const o = opts || {};
    klineFreq = '';            // 新开的弹窗：回到每行自己的参数，上一次切的不带过来
    const list = Array.isArray(o.list) ? o.list : [];
    const codeOf = x => String((x && (x.ts_code || x.tsCode || x.code)) || '');
    const nameOf = x => String((x && x.name) || '');
    let idx = Number.isInteger(o.index) ? o.index : -1;
    if (idx < 0 && list.length) idx = list.findIndex(x => codeOf(x) === String(o.tsCode || ''));
    if (idx < 0 || idx >= list.length) idx = -1;   // 单只看（没列表 / 不在列表里）→ 不摆「下一个」

    const keep = (k, v) => (v !== null && v !== undefined && v !== '' ? [k, v] : null);
    const paramsOf = i => ((typeof o.paramsOf === 'function' && i >= 0
      ? o.paramsOf(list[i], i) : o.params) || {});
    const urlsOf = (code, i) => {
      const p = Object.assign({}, paramsOf(i));
      if (klineFreq) p.freq = klineFreq;   // 弹窗里切过周期 → 一律按它来（含翻页）
      const q = new URLSearchParams(
        Object.keys(p).map(k => keep(k, p[k])).filter(Boolean)).toString();
      const base = '/stock/' + encodeURIComponent(code);
      return { embed: base + '?' + (q ? q + '&' : '') + 'embed=1',
               full: base + (q ? '?' + q : '') };
    };
    const titleOf = (code, name) => (o.title || (code + (name ? ' ' + name : '')))
      + ' · K线预览' + (idx >= 0 && list.length > 1 ? `（${idx + 1}/${list.length}）` : '');

    if (klineModal) klineModal.close();          // 同一时刻只留一个预览
    const cur = idx >= 0 ? list[idx] : null;
    const code0 = cur ? codeOf(cur) : String(o.tsCode || '');
    const u0 = urlsOf(code0, idx);
    klineModal = window.mkModal(titleOf(code0, cur ? nameOf(cur) : o.name),
      `<iframe class="kl-frame" src="${esc(u0.embed)}" title="K线预览"></iframe>`,
      `<span class="small muted" style="margin-right:auto">与个股诊断页同一份图表</span>` +
      `<a class="btn sm" data-open-full href="${esc(u0.full)}" target="_blank">在诊断页打开</a>` +
      `<button class="btn sm primary" data-close>关闭</button>`,
      () => { klineModal = null; klineRefreshFull = null; }, true);

    if (!cur) return klineModal;                 // 没列表 → 只有看 / 关
    const head = klineModal.el.querySelector('.modal-head');
    const h3 = head.querySelector('h3');
    const frame = klineModal.el.querySelector('iframe.kl-frame');
    const fullA = klineModal.el.querySelector('[data-open-full]');
    const next = document.createElement('button');
    next.className = 'btn sm';
    next.setAttribute('data-next', '1');
    next.textContent = '下一个';
    head.querySelector('.modal-acts').insertBefore(next, head.querySelector('[data-close]'));
    const paint = () => {
      const code = codeOf(list[idx]), nm = nameOf(list[idx]), u = urlsOf(code, idx);
      frame.src = u.embed;                       // iframe 里的内容每次现取，别缓存
      fullA.href = u.full;
      h3.textContent = titleOf(code, nm);
      const last = idx + 1 >= list.length;
      next.disabled = last;
      next.title = last ? '已经是最后一只'
        : ('预览下一只：' + codeOf(list[idx + 1]) + ' ' + nameOf(list[idx + 1])).trim();
    };
    next.onclick = () => { if (idx + 1 < list.length) { idx += 1; paint(); } };
    klineRefreshFull = (f) => {
      klineFreq = (f === 'weekly' || f === 'daily') ? f : '';
      fullA.href = urlsOf(codeOf(list[idx]), idx).full;
    };
    paint();
    return klineModal;
  };

  /* ------------- 「可用字段参考」里的「示例」：看这个字段的实际取值 -------------
     需求原话：「选股的时候填写条件，有些字段的值我不知道该填多少，希望在可用字段
     参考那块加个示例链接，点击弹出示例弹窗，展示可用字段及其值」。
     值由后端 `/api/fields/sample` 出（screener.field_sample → market.load_panel，
     与选股**同源**：前复权、周线 asof、筹码、资金流都是那一份）。前端只负责画。
     行渲染 + 点击都收在这里：选股策略页（pool.html）与买卖策略页（strategy.html）
     共用，别在两处各写一遍（列名、文案、格式化都容易走形）。 */
  window.fieldHelpRow = function (name, desc) {
    return '<div class="fh"><span class="n">' + esc(name) + '</span>'
      + '<span class="d">' + esc(desc || '')
      + '<a class="fh-ex" data-f="' + esc(name)
      + '" title="看这个字段的实际取值">示例</a></span></div>';
  };

  window.bindFieldHelp = function (el) {
    if (!el || el.__fhBound) return;
    el.__fhBound = true;
    el.addEventListener('click', function (e) {
      const a = e.target && e.target.closest ? e.target.closest('[data-f]') : null;
      if (!a) return;
      e.preventDefault();
      window.showFieldSample(a.getAttribute('data-f'));
    });
  };

  // 取值展示专用：**不用 fmt.big** —— 字段单位各不相同（amount 千元、total_mv 万元、
  // hold_num 户…），自动加「亿/万」会把两个「万」叠在一起，看着像又造了个口径。
  const sampleNum = function (v) {
    if (v === null || v === undefined || !isFinite(v)) return '—';
    const a = Math.abs(v);
    if (a >= 1000) return v.toLocaleString('zh-CN', { maximumFractionDigits: 0 });
    if (a >= 1) return v.toFixed(2);
    if (a === 0) return '0';
    return v.toFixed(4);
  };

  window.showFieldSample = function (name, date) {
    const m = window.mkModal(name + ' · 取值示例',
      '<div class="fs-top"><div class="fs-name">' + esc(name)
      + '<span class="fs-desc" data-desc></span></div>'
      + '<div class="fs-ctl"><label>交易日</label><input type="date" data-date>'
      + '<span data-basis></span></div></div>'
      + '<div data-out><div class="fs-load">正在按选股口径算样本值…</div></div>',
      '<span class="small muted" style="margin-right:auto">与选股同一套计算口径</span>'
      + '<button class="btn sm primary" data-close>关闭</button>');
    const out = m.el.querySelector('[data-out]');
    const dateEl = m.el.querySelector('[data-date]');
    const draw = function (r) {
      dateEl.value = r.trade_date || '';
      m.el.querySelector('[data-desc]').textContent = r.desc ? '· ' + r.desc : '';
      m.el.querySelector('[data-basis]').textContent = r.sample.basis;
      if (r.empty) {
        out.innerHTML = '<div class="fs-empty">' + esc(r.note) + '</div>';
        return;
      }
      out.innerHTML =
        '<div class="fs-stats">' + r.stats.map(function (s) {
          return '<div class="fs-stat"><span class="k">' + esc(s.k)
            + '</span><span class="v">' + esc(sampleNum(s.v)) + '</span></div>';
        }).join('') + '</div>'
        + '<table class="fs-tbl"><thead><tr><th>代码</th><th>名称</th><th class="num">'
        + esc(r.name) + '</th></tr></thead><tbody>'
        + r.rows.map(function (x) {
          return '<tr><td class="mono">' + esc(x.ts_code) + '</td><td>' + esc(x.name)
            + '</td><td class="num">' + esc(sampleNum(x.value)) + '</td></tr>';
        }).join('') + '</tbody></table>'
        + '<div class="small muted" style="margin-top:10px">' + esc(r.note) + '</div>';
    };
    const load = async function (d) {
      out.innerHTML = '<div class="fs-load">正在按选股口径算样本值…</div>';
      try {
        const r = await apiGet('/api/fields/sample?name=' + encodeURIComponent(name)
          + (d ? '&trade_date=' + encodeURIComponent(d) : ''));
        draw(r);
      } catch (e) {
        out.innerHTML = '<div class="fs-load err">'
          + esc(e && e.message ? e.message : '取不到示例') + '</div>';
      }
    };
    dateEl.onchange = function () { load(dateEl.value); };
    load(date);
    return m;
  };

  /* 让表格数值统一着色 */
  window.pctCell = (v, d) => `<td class="num ${fmt.cls(v)}">${fmt.pct(v, d)}</td>`;
})();
