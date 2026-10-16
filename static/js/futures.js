/* 今日期货：主连列表。
   ---------------------------------------------------------------
   数据来自 `/api/futures`（`core/futures.py`：读本地库 `futures_basic` / `futures_daily`
   / `futures_option`，由 tushare 同步），页面摆五列 —— **主连 / 涨跌幅 / 最近期权到期
   / 最近合约交割 / 查看K线**（前两列 + K线是需求原话；第三列「最近期权到期」= 主连行展示
   对应期权最快到期的那个合约还有几日，列表**默认按它从小到大排**；第四列「最近合约交割」
   = 该品种最快交割的可交易合约（展开行第一行）距今天还剩几天，同样点表头可排），
   同品种合约收在展开行里（含「到期日」「期权到期日」两列）。

   两个实现上的取舍：
   1. **一个品种一个 `<tbody>`**。展开行跟主连行天然绑在一起，筛选/排序时整块移动，
      不用另外维护「哪一行配哪一行」的映射（也顺手避开了「排序后展开行错位」这个经典坑）。
   2. **筛选在内存里做、每次重建 DOM**（90 组量级，重建只要几毫秒），而不是给每行塞
      `data-*` 再靠 CSS 藏 —— 后者在改排序时要来回改内联样式，状态会互相打架。
      展开状态存在 `state.open` 里，重建后照原样恢复。

   ⚠「最近期权到期」「最近合约交割」两列的**天数都由后端算**（`build_groups` 的
   `opt_near_days` / `fut_near_days`）：排序要按它、显示也用它，前端不再算一遍 ——
   两边各算早晚会差一天（跨零点、时区）。
   展开行里那两个到期日的「还有几天」是另一件事（每行自己的），才用本地 `daysLeft()`。

   ⚠ K 线弹窗嵌的是**同源 iframe** `/futures/kline?embed=1`（与个股那条 `/stock/<代码>?embed=1`
   同一个做法）：K 线只有那一份实现，弹窗里不会养出第二个口径。期货没有基本面/筹码/信号，
   所以它单独一页，套不进个股诊断页。 */
(function () {
  'use strict';

  const state = {
    data: null,          // /api/futures 的 data
    mkt: '',             // 市场筛选（'' = 全部）
    q: '',               // 搜索词（小写）
    sortKey: 'opt',      // 排序字段：'opt'（最近期权到期，**默认**）/ 'dl'（最近合约交割）
                         // / 'pct'（涨跌幅）
    desc: false,         // 当前字段的方向：opt 与 dl 默认**升序**（最快到期/交割的排最前），
                         // pct 默认降序（toggleSort 里按列给）
    open: new Set(),     // 已展开的品种 key
  };

  const el = (id) => document.getElementById(id);
  /* ⚠ 换 tushare 落库后分组不再有 `mkt`（那是东财市场号）。现在是
     `market_code`（SHFE/DCE…）+ `fut_code`（品种码），两个合起来才唯一 ——
     不同交易所可以有同名品种码，只用一个会串。 */
  const keyOf = (g) => g.market_code + '|' + g.fut_code;

  /* ---------------- K 线弹窗（唯一入口） ---------------- */

  function showKline(secid, name) {
    if (!secid) return;
    const q = '?secid=' + encodeURIComponent(secid)
      + '&name=' + encodeURIComponent(name || '');
    const embed = '/futures/kline' + q + '&embed=1';
    const full = '/futures/kline' + q;
    window.mkModal(
      (name || secid) + ' · 期货K线',
      '<iframe class="kl-frame" src="' + esc(embed) + '" title="期货K线"></iframe>',
      '<span class="small muted" style="margin-right:auto">行情来自 tushare · 本地库'
      + '（日线收盘结算后才更新，盘中看到的是上一交易日）</span>'
      + '<a class="btn sm" href="' + esc(full) + '" target="_blank" rel="noopener">新标签打开</a>'
      + '<button class="btn sm primary" data-close>关闭</button>',
      null, true);
  }

  /* ---------------- 渲染 ---------------- */

  function priceText(v) {
    // 期货价格量级跨得很大（橡胶 1.9 万 / 国债 102.59 / 黄金 940.12），统一两位小数 + 千分位。
    return fmt.num(v, 2);
  }

  /* 距今天还有几天（负数=已过）。用**本地零点**对差，别用 toISOString()（会被时区推一天）。 */
  function daysLeft(d) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(d || ''));
    if (!m) return null;
    const t = new Date(+m[1], +m[2] - 1, +m[3]);
    const n = new Date();
    const today = new Date(n.getFullYear(), n.getMonth(), n.getDate());
    return Math.round((t - today) / 86400000);
  }

  function leftTip(d) {
    const n = daysLeft(d);
    if (n === null) return '';
    return n >= 0 ? '（还有 ' + n + ' 天）' : '（已过 ' + (-n) + ' 天）';
  }

  /* 期货合约**自己的**最后交易日（`delist_date`）。主连是连续序列，这一列恒空。 */
  function futDateCell(c) {
    if (!c.delist_date) return '<td class="num muted" title="连续合约没有到期日">—</td>';
    return '<td class="num mono small" title="期货合约最后交易日 ' + esc(c.delist_date)
      + leftTip(c.delist_date) + '">' + esc(c.delist_date) + '</td>';
  }

  /* 该期货合约**对应的期权**的到期日（按标的合约 ts_code 对上 `futures_option`）。
     少数品种（连续月份期权）会有两个到期日 ⇒ 主显示最早的，其余折成「+N」塞进 title。 */
  function optDateCell(c) {
    const mats = c.opt_maturities || [];
    if (!mats.length) {
      return '<td class="num muted" title="这个合约没有对应的期权（中金所股指期权的标的'
        + '是**指数**不是期货；也可能这个月份还没挂期权）">—</td>';
    }
    const first = mats[0];
    const tip = '对应期权 ' + mats.length + ' 个到期日：' + mats.join('、') + leftTip(first);
    const more = mats.length > 1
      ? '<span class="muted"> +' + (mats.length - 1) + '</span>' : '';
    return '<td class="num mono small" title="' + esc(tip) + '">' + esc(first) + more + '</td>';
  }

  /* 主连行的「最近期权到期」= 该品种**全部可交易合约**里，对应期权最快到期的那个还剩几天。
     口径在后端 `build_groups`：主连自己（`RU.SHF`）不是可交易合约、没有对应期权，所以必须
     在**品种内聚合**；已过期的期权不算。
     ⚠ 天数直接用后端的 `opt_near_days`，**别在前端重算** —— 排序也按它，两处各算会差一天。 */
  function optNearCell(g) {
    const d = g.opt_near_days, date = g.opt_near_date;
    if (d === null || d === undefined) {
      return '<td class="num ft-near muted" title="这个品种没有对应的期权（中金所股指期权的标的是'
        + '**指数**不是期货；也可能它的合约都还没挂期权）">—</td>';
    }
    const tip = '最快的对应期权 ' + date + ' 到期' + leftTip(date)
      + (g.opt_near_code ? '（来自 ' + g.opt_near_code + ' ' + (g.opt_near_name || '') + '）' : '');
    return '<td class="num ft-near" title="' + esc(tip) + '">' + d + ' 天</td>';
  }

  /* 主连行的「最近合约交割」= 该品种内**最后交易日最早**的可交易期货合约（`mates` 第一个
     带到期日的，**就是展开行第一行**）距今天还剩几天。口径在后端 `build_groups`：
     主连自己是连续序列、没有到期日，必须在品种内聚合；已退市的（最后交易日早于行情日）
     不在 mates 里。⚠ 与上面「最近期权到期」是两个概念：这个指**期货合约交割日**，
     那个指**期权到期日**，同一行两列各指各的。
     ⚠ 天数同样直接用后端的 `fut_near_days`，别在前端重算。**负数是合法值**：行情日
     （上周五）之后、今天之前交割的合约还在 mates 里（它在最后一个行情日还活着）——
     那是「刚交割完」，显示成「已交割」而不是「-3 天」；`0` 是今天最后交易日，
     显示「今天交割」。 */
  function futNearCell(g) {
    const d = g.fut_near_days, date = g.fut_near_date;
    if (d === null || d === undefined) {
      return '<td class="num ft-dl muted" title="这个品种没有带到期日的可交易合约'
        + '（展开行里全是连续序列，或 futures_basic 还没给到期日）">—</td>';
    }
    const tip = '最近合约 ' + (g.fut_near_code || '') + ' ' + (g.fut_near_name || '')
      + ' ' + (date || '') + ' 交割（最后交易日）'
      + (d > 0 ? '，还有 ' + d + ' 天'
         : (d === 0 ? '，就是今天（收盘后退市）' : '，已过 ' + (-d) + ' 天'));
    const txt = d > 0 ? d + ' 天' : (d === 0 ? '今天交割' : '已交割');
    return '<td class="num ft-dl" title="' + esc(tip) + '">' + txt + '</td>';
  }

  function contractRow(c) {
    const title = '成交额 ' + fmt.big(c.amount) + ' / 成交量 ' + fmt.int(c.vol)
      + (c.oi === null || c.oi === undefined ? '' : ' / 持仓量 ' + fmt.int(c.oi));
    return '<tr title="' + esc(title) + '">'
      + '<td class="ft-ct-nm">' + esc(c.name)
      + (c.is_sub ? '<span class="tag">次主连</span>' : '') + '</td>'
      + '<td class="mono">' + esc(c.code) + '</td>'
      + '<td class="num">' + priceText(c.price) + '</td>'
      + '<td class="num ' + fmt.cls(c.pct_chg) + '">' + fmt.pct(c.pct_chg) + '</td>'
      + '<td class="num">' + (c.oi === null || c.oi === undefined ? '—' : fmt.int(c.oi)) + '</td>'
      + futDateCell(c)
      + optDateCell(c)
      + '<td class="ft-k"><button class="btn sm" type="button" data-k="' + esc(c.secid)
      + '" data-n="' + esc(c.name) + '">K线</button></td>'
      + '</tr>';
  }

  function groupHtml(g) {
    const k = keyOf(g);
    const isOpen = state.open.has(k);
    const m = g.main, ct = g.contracts || [];
    const head = isOpen ? '收起' : '展开同品种合约';
    /* ⚠ 分组 key 用 `data-grp`，**不能**跟着叫 `data-k`：K 线按钮也是 `data-k`，
       而点击判定用的是 `t.closest('button[data-k]')` / `closest('[data-grp]')` ——
       两边同名的话，从行内任意位置往上找会先撞上 `<tbody>` 上的那个，
       「点行名展开」就变成拿着 `142|20号胶`（分组 key，不是 secid）去开 K 线弹窗（实测踩过）。 */
    return '<tbody class="ft-grp" data-grp="' + esc(k) + '">'
      + '<tr class="ft-main' + (isOpen ? ' open' : '') + '">'
      + '<td class="ft-name">'
      + '<button class="ft-exp" type="button" aria-expanded="' + (isOpen ? 'true' : 'false')
      + '" title="' + head + '">▸</button>'
      + '<span class="ft-nm">' + esc(m.name) + '</span>'
      + '<span class="ft-cd mono">' + esc(m.code) + '</span>'
      + '<span class="tag">' + esc(g.market) + '</span>'
      + '</td>'
      + '<td class="num ft-pct"><span class="' + fmt.cls(m.pct_chg) + '">'
      + fmt.pct(m.pct_chg) + '</span></td>'
      + optNearCell(g)
      + futNearCell(g)
      + '<td class="ft-k"><button class="btn sm" type="button" data-k="' + esc(m.secid)
      + '" data-n="' + esc(m.name) + '">查看K线</button></td>'
      + '</tr>'
      + '<tr class="ft-sub"' + (isOpen ? '' : ' hidden') + '>'
      + '<td colspan="5"><div class="ft-subwrap">'
      + '<div class="ft-subhd">同品种合约 <b>' + ct.length + '</b> 个 · 按到期日近→远'
      + '（主连：' + esc(m.name) + ' ' + priceText(m.price) + ' 参考）'
      + '<span class="muted">　到期日＝该期货合约的最后交易日；'
      + '期权到期日＝该合约对应期权的到期日（没有对应期权显示「—」）</span></div>'
      + '<table class="data sm ft-ct"><thead><tr>'
      + '<th>合约</th><th>代码</th><th class="num">最新价</th><th class="num">涨跌幅</th>'
      + '<th class="num">持仓量</th><th class="num">到期日</th><th class="num">期权到期日</th>'
      + '<th>K线</th>'
      + '</tr></thead><tbody>'
      + (ct.length ? ct.map(contractRow).join('')
        : '<tr><td colspan="8" class="muted">这个品种暂时没有别的合约</td></tr>')
      + '</tbody></table></div></td></tr>'
      + '</tbody>';
  }

  /* 按主连涨跌幅比（没有涨跌幅的**垫底** —— 哪个方向都是，它不是「跌幅最大」） */
  function cmpPct(a, b, desc) {
    const x = a.main.pct_chg, y = b.main.pct_chg;
    if (x === null && y === null) return 0;
    if (x === null) return 1;
    if (y === null) return -1;
    return desc ? y - x : x - y;
  }

  /* 「还剩几日」类字段的排序（**opt / dl 两列共用这一套**，别给第二列再抄一份 ——
     两处各写早晚会分叉）：`fa`/`fb` 是分组上的字段名（同一列时两边同名字）。
     - null / undefined（没有这个信息）无论方向都**垫底**：那不是「0 天后到期」，
       排最前会误导。已交割的负数**不算** null，照数值排（升序时排最前 = 最紧急）。
     - 并列时退回按主连涨跌幅降序，与后端 `_sort_key_opt` 保持同一口径，
       免得同一份数据两处排出不同顺序（页面刷新前后跳）。 */
  function cmpDays(a, b, fa, fb, desc) {
    const x = a[fa], y = b[fb];
    const xn = (x === null || x === undefined);
    const yn = (y === null || y === undefined);
    if (xn && yn) return cmpPct(a, b, true);
    if (xn) return 1;
    if (yn) return -1;
    if (x !== y) return desc ? y - x : x - y;
    return cmpPct(a, b, true);
  }

  /* 排序（页面默认顺序）：`state.sortKey` 选字段、`state.desc` 选方向。
     - `'opt'`（**默认**）：最近期权到期**升序** —— 「按这个字段从小到大」。
       没有期权的品种（`opt_near_days` 为 null）无论方向都垫底（理由见 cmpDays）。
     - `'dl'`：最近合约交割还剩几日，同样**升序**（快交割的排最前）；没有带到期日的
       可交易合约 ⇒ `fut_near_days` 为 null，同样垫底。
     - `'pct'`：主连涨跌幅。 */
  function compare(a, b) {
    if (state.sortKey === 'opt') {
      return cmpDays(a, b, 'opt_near_days', 'opt_near_days', state.desc);
    }
    if (state.sortKey === 'dl') {
      return cmpDays(a, b, 'fut_near_days', 'fut_near_days', state.desc);
    }
    return cmpPct(a, b, state.desc);
  }

  /* 搜索：品种名 / 主连名与代码 / 任一合约的代码与名称，命中任一即可。
     在内存里算（不塞 data-*），筛选与排序共用同一份可见集合。 */
  function visibleGroups() {
    const d = state.data;
    if (!d) return [];
    let gs = d.groups || [];
    if (state.mkt) gs = gs.filter(g => String(g.market_code) === String(state.mkt));
    if (state.q) {
      gs = gs.filter(function (g) {
        const hay = [g.product, g.market, g.main.name, g.main.code]
          .concat((g.contracts || []).map(c => c.name + ' ' + c.code))
          .join(' ').toLowerCase();
        return hay.indexOf(state.q) >= 0;
      });
    }
    return gs.slice().sort(compare);
  }

  function render() {
    const d = state.data;
    if (!d) return;
    /* ⚠ 行情日 ≠ 现在：期货日线**收盘结算后**才更新，盘中看到的还是上一交易日，
       标题栏必须写清「截至哪一天收盘」，别让人误以为是实时价。 */
    if (el('ft-asof')) {
      el('ft-asof').textContent = (d.trade_date ? '截至 ' + d.trade_date + ' 收盘' : '暂无数据')
        + (d.cached ? '（缓存）' : '');
    }
    if (el('ft-stat')) {
      el('ft-stat').textContent = '主连 ' + d.stats.n_mains + ' · 涨 ' + d.stats.up
        + ' / 跌 ' + d.stats.down + ' · 合约 ' + fmt.int(d.stats.n_contracts);
    }

    // 市场失败提示（按市场隔离：只报错的那几家，其余照常显示）
    const bad = (d.markets || []).filter(m => m.error);
    const warn = el('ft-warn');
    if (warn) {
      warn.innerHTML = bad.length
        ? '<div class="ft-warn">' + bad.map(m => '<b>' + esc(m.name) + '</b> 取数失败：'
          + esc(m.error)).join('　') + '（其余市场照常）</div>'
        : '';
    }

    const gs = visibleGroups();
    if (!gs.length) {
      el('ft-list').innerHTML = '<div class="empty"><span class="big">◌</span>'
        + (state.data.groups && state.data.groups.length
            ? '没有匹配的品种'
            : '本地库还没有期货日线 —— 去「数据」页跑一次期货同步')
        + '</div>';
      return;
    }
    el('ft-list').innerHTML = '<table class="data ft-tbl" id="ft-tbl">'
      + '<thead><tr>'
      + '<th class="ft-th-name">主连</th>'
      + '<th class="num sortable" id="ft-th-pct">涨跌幅<span class="sarrow"></span></th>'
      + '<th class="num sortable" id="ft-th-opt">最近期权到期<span class="sarrow"></span></th>'
      + '<th class="num sortable" id="ft-th-dl">最近合约交割<span class="sarrow"></span></th>'
      + '<th class="ft-th-k">查看K线</th>'
      + '</tr></thead>'
      + gs.map(groupHtml).join('')
      + '</table>';
    paintSortHead();
  }

  /* 三列都可点排序，箭头只挂在**当前那列**上（三列同时亮会看不出到底按谁排的）。
     ⚠ 表头是每次 render 重建的 ⇒ 这里必须把三列状态都重画，不能只画被点的那列。 */
  const SORT_COLS = [
    { id: 'ft-th-pct', key: 'pct', name: '涨跌幅', defDesc: true },
    { id: 'ft-th-opt', key: 'opt', name: '最近期权到期', defDesc: false },  // 默认从小到大
    { id: 'ft-th-dl', key: 'dl', name: '最近合约交割', defDesc: false },    // 默认从小到大
  ];

  function paintSortHead() {
    SORT_COLS.forEach(function (c) {
      const th = el(c.id);
      if (!th) return;
      const on = state.sortKey === c.key;
      th.classList.toggle('sort-desc', on && state.desc);
      th.classList.toggle('sort-asc', on && !state.desc);
      th.title = on
        ? '当前按' + c.name + (state.desc ? '从大到小' : '从小到大') + '排序（点击反向）'
        : '点击按' + c.name + '排序（' + (c.defDesc ? '从大到小' : '从小到大') + '）';
    });
  }

  /* 点表头：同列再点反向；换列时用**该列的直觉默认方向**（涨跌幅从大到小；
     「还剩几日」两列从大到小没意义 —— 都是从小到大，快到期/快交割的排最前）。 */
  function toggleSort(key) {
    if (state.sortKey === key) {
      state.desc = !state.desc;
    } else {
      state.sortKey = key;
      state.desc = key === 'pct';
    }
    render();
  }

  function paintMktSelect() {
    const d = state.data;
    const sel = el('ft-mkt');
    if (!sel || !d) return;
    const opts = ['<option value="">全部市场（' + d.stats.n_mains + '）</option>'];
    (d.markets || []).forEach(function (m) {
      opts.push('<option value="' + m.code + '">' + esc(m.name) + '（' + m.n_mains + '）'
        + (m.error ? ' · 无数据' : '') + '</option>');
    });
    sel.innerHTML = opts.join('');
    sel.value = state.mkt;
  }

  /* ---------------- 取数 ---------------- */

  async function load(force) {
    const box = el('ft-list');
    // 加载/失败都走 app.js 那套状态块（全站同一种长相，见 boxState 的注释）
    if (box && !state.data) boxState(box, 'loading', '正在取期货行情…');
    try {
      state.data = await apiGet('/api/futures' + (force ? '?force=1' : ''));
      paintMktSelect();
      render();
    } catch (e) {
      boxState(box, 'error', '取期货行情失败', (e && e.message) || '');
    }
  }

  /* ---------------- 事件 ---------------- */

  document.addEventListener('DOMContentLoaded', function () {
    load(false);

    if (el('ft-refresh')) {
      el('ft-refresh').onclick = function () {
        const b = this;
        b.disabled = true;
        load(true).finally(function () { b.disabled = false; });
      };
    }

    if (el('ft-mkt')) {
      el('ft-mkt').onchange = function () { state.mkt = this.value; render(); };
    }

    if (el('ft-q')) {
      let t = null;
      el('ft-q').oninput = function () {
        const v = this.value.trim().toLowerCase();
        clearTimeout(t);
        t = setTimeout(function () { state.q = v; render(); }, 120);
      };
    }

    // 表头排序 + 展开 + 看 K 线，全走一次事件委托（列表是整块重建的，逐行绑会漏）
    const list = el('ft-list');
    if (list) {
      list.addEventListener('click', function (e) {
        const t = e.target;
        if (!t || !t.closest) return;

        if (t.closest('#ft-th-pct')) { toggleSort('pct'); return; }
        if (t.closest('#ft-th-opt')) { toggleSort('opt'); return; }
        if (t.closest('#ft-th-dl')) { toggleSort('dl'); return; }

        // 只认真正的 K 线按钮（`button[data-k]`）—— 只写 `[data-k]` 会撞上分组 tbody 上的属性
        const kbtn = t.closest('button[data-k]');
        if (kbtn) {
          e.preventDefault();
          showKline(kbtn.getAttribute('data-k'), kbtn.getAttribute('data-n'));
          return;
        }

        // 点主连行（除按钮外的任意位置）都算展开/收起
        const tr = t.closest('tr.ft-main');
        const tb = t.closest('tbody.ft-grp');
        if (tr && tb) {
          const k = tb.getAttribute('data-grp');
          if (state.open.has(k)) state.open.delete(k); else state.open.add(k);
          render();
        }
      });
    }
  });
})();
