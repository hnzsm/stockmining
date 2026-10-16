/* 股票池选择器 —— 回测页与选股页共用
 *
 * 下拉框三组，回答三个不同问题：
 *
 *   静态股票池   市场范围写死的那份名单（全部A股 / 沪深300 / 行业…）
 *   选股策略     已保存的条件池，按 id 引用。改一条，所有引用它的回测口径一起变
 *   自定义       临时写条件试效果，满意了点「保存为选股策略」沉淀下来
 *
 * 前端只负责产出这个描述：'all' | '000300.SH' | {pool_id:N} | {base,where,filt}
 * 判定全部在后端做，保证回测与选股口径一致。
 *
 * 用法：
 *   const pe = new PoolEditor({
 *     select: '#bt-universe', panel: '#bt-pool', date: '#bt-end',
 *     universes: [...], pools: [...], presets: [...], fields: [...],
 *     groups: ['static','pool','custom'],   // 默认三组都给；今天选股页只给 ['pool']
 *     onChange: spec => {}
 *   });
 *   pe.value()      // 'all' | {pool_id:3} | {base, where, filt}
 *   pe.setValue(x)  // 回显
 *   pe.setPools(l)  // 选股策略有增删后重建下拉（保持当前选中）
 */
(function () {
  var CUSTOM = '__custom__';
  var PRE = 'preset:';
  var POOLPRE = 'pool:';

  function q(sel, root) {
    return (root || document).querySelector(sel);
  }
  function esc(v) {
    return window.esc ? window.esc(v) : String(v == null ? '' : v);
  }

  /* ---------- 纯函数部分（不碰 DOM，便于离线测试） ---------- */

  /** 归一化成 {kind, base, where[], pool_id}。
   *  kind: 'static' 静态池 | 'poolref' 引用已保存的选股策略 | 'cond' 就地写的条件池。 */
  function normalize(spec) {
    if (spec && typeof spec === 'object' && !Array.isArray(spec)
      && ('where' in spec || 'base' in spec || 'pool_id' in spec)) {
      if (spec.pool_id) {
        return { kind: 'poolref', pool_id: Number(spec.pool_id), base: spec.base || null,
          where: spec.where || [] };
      }
      var base = spec.base == null || spec.base === '' ? 'all' : spec.base;
      var w = spec.where || [];
      if (typeof w === 'string') {
        w = w.replace(/；/g, '\n').replace(/;/g, '\n').split('\n');
      }
      w = w.map(function (x) { return String(x).trim(); })
        .filter(function (x) { return x; });
      return { kind: 'cond', base: base, where: w };
    }
    if (typeof spec === 'string' && spec.indexOf(POOLPRE) === 0) {
      var pid = parseInt(spec.slice(POOLPRE.length), 10);
      if (pid) return { kind: 'poolref', pool_id: pid, base: null, where: [] };
    }
    return { kind: 'static', base: spec, where: [], pool_id: null };
  }

  function sameWhere(a, b) {
    if (a.length !== b.length) return false;
    for (var i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
    return true;
  }

  /** 命中的预设 id，没命中返回 null。 */
  function matchPreset(spec, presets) {
    var s = normalize(spec);
    if (s.kind !== 'cond') return null;
    for (var i = 0; i < presets.length; i++) {
      var p = presets[i];
      if (p.base === s.base && sameWhere(p.where || [], s.where)) return p.id;
    }
    return null;
  }

  /** 人类可读标签（下拉里已有后端给的 label，这里用于面板标题/试跑提示）。 */
  function specLabel(spec, universes, presets, pools) {
    var s = normalize(spec);
    if (s.kind === 'static') {
      for (var i = 0; i < universes.length; i++) {
        if (universes[i].code === spec) return universes[i].name;
      }
      return String(spec);
    }
    if (s.kind === 'poolref') {
      var po = findById(pools || [], s.pool_id);
      return po ? (po.name + (po.cond_count ? '（' + po.cond_count + ' 条条件）' : ''))
        : ('选股策略#' + s.pool_id);
    }
    var pid = matchPreset(spec, presets);
    var core = pid ? (findById(presets, pid) || {}).name
      : (s.where[0] || '条件池');
    if (s.where.length > 1 && !pid) core += ' 等 ' + s.where.length + ' 条';
    return core;
  }

  function findById(list, id) {
    for (var i = 0; i < list.length; i++) if (list[i].id === id) return list[i];
    return null;
  }

  /* ---------- 编辑器 ---------- */

  function PoolEditor(opt) {
    this.sel = typeof opt.select === 'string' ? q(opt.select) : opt.select;
    this.host = typeof opt.panel === 'string' ? q(opt.panel) : opt.panel;
    this.dateInput = typeof opt.date === 'string' ? q(opt.date) : (opt.date || null);
    this.universes = opt.universes || [];
    this.pools = opt.pools || [];
    this.presets = opt.presets || [];
    this.fields = opt.fields || [];
    this.onChange = opt.onChange || function () { };
    // 下拉里给哪几组。今天选股页只要「选股策略」一组（用户的要求：只选选股策略），
    // 回测页则默认三组都给（静态池跑基准、临时条件试参数都还有用）。
    this.groups = opt.groups || ['static', 'pool', 'custom'];
    this.state = { base: 'all', where: [] };
    this._buildSelect();
    if (this._has('custom')) this._buildPanel();
    var self = this;
    this.sel.addEventListener('change', function () { self._onSelect(); });
    if (opt.default !== undefined) this.setValue(opt.default);
    else this._sync();
  }

  PoolEditor.prototype._has = function (g) {
    return this.groups.indexOf(g) >= 0;
  };

  PoolEditor.prototype._buildSelect = function () {
    var parts = [];
    if (this._has('static')) {
      parts.push('<optgroup label="静态股票池">');
      this.universes.forEach(function (u) {
        parts.push('<option value="' + esc(u.code) + '">' + esc(u.name) + '</option>');
      });
      parts.push('</optgroup>');
    }

    // 选股策略：按 id 引用，改策略 → 所有引用它的回测口径一起变
    // ⚠ 显示名用后端给的 `name_label`（周K 策略自带「（周K）」后缀，日K 不带）——
    //   后缀的唯一来源是 `universe.name_tag()`，前端别自己拼。
    if (this._has('pool')) {
      parts.push('<optgroup label="选股策略（已保存）">');
      (this.pools || []).forEach(function (p) {
        var sub = (p.cond_count ? p.cond_count + ' 条条件 · ' : '') + (p.base_label || '');
        parts.push('<option value="' + POOLPRE + esc(p.id) + '"'
          + ' title="' + esc(p.note || '') + '">'
          + esc(p.name_label || p.name) + (sub ? '（' + esc(sub) + '）' : '') + '</option>');
      });
      parts.push('</optgroup>');
    }

    // 临时写条件：不落库，满意了可以点「保存为选股策略」
    if (this._has('custom')) {
      parts.push('<optgroup label="临时">');
      parts.push('<option value="' + CUSTOM + '">自定义条件池…</option>');
      parts.push('</optgroup>');
    }
    this.sel.innerHTML = parts.join('');
  };

  /** 选股策略有增删改后重建下拉，尽量保持当前选中项。 */
  PoolEditor.prototype.setPools = function (pools) {
    this.pools = pools || [];
    var keep = this.sel.value;
    this._buildSelect();
    var has = Array.prototype.some.call(this.sel.options, function (o) {
      return o.value === keep;
    });
    this.sel.value = has ? keep : (this.sel.options[0] || {}).value;
    this._sync();
  };

  PoolEditor.prototype._buildPanel = function () {
    var host = this.host;
    if (!host) return;
    var self = this;

    var baseOpts = this.universes.map(function (u) {
      return '<option value="' + esc(u.code) + '">' + esc(u.name) + '</option>';
    }).join('');

    var presetBtns = this.presets.map(function (p) {
      return '<button class="btn sm ghost" data-preset="' + esc(p.id) + '"'
        + ' title="' + esc(p.note || '') + '">' + esc(p.name) + '</button>';
    }).join('');

    // 字段 chip：**点一下看这个字段的实际取值**（示例弹窗，与「可用字段参考」那块同一个
    // 入口 `window.showFieldSample`）。title 里保留中文说明 —— 悬停能读，点开能看量级。
    var help = this.fields.map(function (f) {
      return '<span class="chip" title="' + esc(f.desc || '') + '（点一下看实际取值）"'
        + ' data-f="' + esc(f.name) + '">' + esc(f.name) + '</span>';
    }).join('');

    host.innerHTML =
      '<div class="pool-panel">' +
      '<div class="pool-row">' +
      '<div class="field"><label class="lb">基础池</label>' +
      '<select class="pool-base">' + baseOpts + '</select></div>' +
      '</div>' +
      '<div class="field"><label class="lb">入池条件' +
      '<span class="muted small">（每行一条，<b>全部</b>成立才算入池；' +
      '输入字段名会自动提示）</span></label>' +
      '<textarea class="pool-where mono" rows="3" spellcheck="false"' +
      ' placeholder="total_mv < 500000&#10;turnover_rate > 5"></textarea></div>' +
      '<div class="pool-presets"><span class="small muted">一键套用：</span>' +
      presetBtns + '</div>' +
      '<div class="pool-actions">' +
      '<input type="date" class="pool-date">' +
      '<button class="btn sm pool-test">试跑</button>' +
      '<button class="btn sm ghost pool-save" title="把当前条件存成一条可复用的选股策略">'
      + '保存为选股策略</button>' +
      '<button class="btn sm ghost pool-help-toggle">可用字段</button>' +
      '<span class="pool-status small muted"></span>' +
      '</div>' +
      '<div class="pool-help" style="display:none">' + help + '</div>' +
      '<div class="pool-note small muted"></div>' +
      '<div class="pool-result"></div>' +
      '</div>';

    this.baseEl = q('.pool-base', host);
    this.whereEl = q('.pool-where', host);
    this.dateEl = q('.pool-date', host);
    this.statusEl = q('.pool-status', host);
    this.noteEl = q('.pool-note', host);
    this.resultEl = q('.pool-result', host);

    // 面板内改动 → 说明当前是自定义池
    this.baseEl.addEventListener('change', function () { self._read(); self._sync(); });
    this.whereEl.addEventListener('input', function () { self._read(); self._sync(); });

    // 试跑 / 另存为选股策略：都会发请求（试跑还是全市场扫描），点两下就是跑两遍
    var bTest = q('.pool-test', host), bSave = q('.pool-save', host);
    bTest.addEventListener('click', guardClick(bTest, function () { self.test(); }));
    bSave.addEventListener('click', guardClick(bSave, function () { self.saveAsPool(); }));
    var ht = q('.pool-help-toggle', host);
    var hp = q('.pool-help', host);
    ht.addEventListener('click', function () {
      var show = hp.style.display === 'none';
      hp.style.display = show ? '' : 'none';
      ht.textContent = show ? '收起字段' : '可用字段';
    });
    // chip 点击（事件委托）→ 示例弹窗；复用 app.js 那一份，别在这里再写一遍取数逻辑
    if (window.bindFieldHelp) window.bindFieldHelp(hp);

    host.querySelectorAll('[data-preset]').forEach(function (b) {
      b.addEventListener('click', function () { self.applyPreset(b.dataset.preset); });
    });

    // 条件框的字段/函数自动补全（可选模块，没加载就不影响输入）
    if (window.ExprAC && this.whereEl) {
      this.ac = window.ExprAC.attach(this.whereEl, { weekly: false });
    }
  };

  /** 把面板控件读回 state。 */
  PoolEditor.prototype._read = function () {
    if (!this.host || !this.baseEl) return;   // 只选「选股策略」时面板根本没建
    this.state = {
      base: this.baseEl.value || 'all',
      where: String(this.whereEl.value || '').split('\n')
        .map(function (x) { return x.trim(); })
        .filter(function (x) { return x; })
    };
  };

  /** 把 state 写回面板控件。 */
  PoolEditor.prototype._write = function () {
    if (!this.host || !this.baseEl) return;
    this.baseEl.value = this.state.base;
    this.whereEl.value = (this.state.where || []).join('\n');
  };

  PoolEditor.prototype._onSelect = function () {
    var v = this.sel.value;
    if (v.indexOf(PRE) === 0) {
      this.applyPreset(v.slice(PRE.length));
      return;
    }
    this._sync();
  };

  PoolEditor.prototype.applyPreset = function (pid) {
    var p = findById(this.presets, pid);
    if (!p) return;
    this.state = {
      base: p.base || 'all',
      where: (p.where || []).slice()
    };
    this.sel.value = PRE + pid;      // 下拉同步到该预设，用户能看出当前是哪个
    this._write();
    this._showNote(p.note || '');
    this._sync();
  };

  PoolEditor.prototype._showNote = function (txt) {
    if (this.noteEl) this.noteEl.innerHTML = esc(txt);
  };

  PoolEditor.prototype._sync = function () {
    var v = this.sel.value;
    var isCustom = v === CUSTOM;
    if (this.host) this.host.style.display = isCustom ? '' : 'none';
    if (this.statusEl) {
      if (isCustom) {
        this.statusEl.textContent = '';
      } else if (v.indexOf(POOLPRE) === 0) {
        // 选中的是一条已保存的选股策略：显示它的说明，让用户知道池子在筛什么
        var p = findById(this.pools || [], parseInt(v.slice(POOLPRE.length), 10));
        this.statusEl.innerHTML = p
          ? '<b>' + esc(p.name) + '</b>' + (p.note ? ' · ' + esc(p.note) : '')
          : '<span class="warn">该选股策略已被删除</span>';
      } else {
        this.statusEl.textContent = specLabel(this.value(), this.universes,
          this.presets, this.pools);
      }
    }
    this.onChange(this.value());
  };

  /** 当前池子描述：静态池→字符串；选股策略→{pool_id}；临时条件→{base,where,filt}。 */
  PoolEditor.prototype.value = function () {
    var v = this.sel.value;
    if (v === CUSTOM) {
      this._read();
      if (!this.state.where.length) return this.state.base;   // 没写条件 → 退化成静态池
      return {
        base: this.state.base,
        where: this.state.where.slice()
      };
    }
    if (v.indexOf(POOLPRE) === 0) {
      var pid = parseInt(v.slice(POOLPRE.length), 10);
      if (pid) return { pool_id: pid };
    }
    if (v.indexOf(PRE) === 0) {
      var p = findById(this.presets, v.slice(PRE.length));
      if (p) {
        return { base: p.base || 'all', where: (p.where || []).slice() };
      }
    }
    return v;                                                  // 静态池
  };

  /** 回显：静态字符串 / {pool_id} / {base,where} 都能吃。 */
  PoolEditor.prototype.setValue = function (spec) {
    var s = normalize(spec);
    if (s.kind === 'static') {
      var code = s.base == null ? 'all' : String(s.base);
      var has = Array.prototype.some.call(this.sel.options, function (o) {
        return o.value === code;
      });
      this.sel.value = has ? code : (this.sel.options[0] || {}).value;
      this._sync();
      return;
    }
    if (s.kind === 'poolref') {
      var target = POOLPRE + s.pool_id;
      var hit = Array.prototype.some.call(this.sel.options, function (o) {
        return o.value === target;
      });
      if (hit) {
        this.sel.value = target;
        this._sync();
        return;
      }
      // 引用的策略没了：退回静态池，别让页面卡在一个不存在的选项上
      this.sel.value = (this.sel.options[0] || {}).value;
      this._sync();
      return;
    }
    this.state = { base: s.base, where: s.where.slice() };
    var pid = matchPreset(spec, this.presets);
    this.sel.value = pid ? (PRE + pid) : CUSTOM;
    this._write();
    // 命中的预设不一定覆盖当前基础池，note 只在完全命中时展示
    this._showNote(pid ? ((findById(this.presets, pid) || {}).note || '') : '');
    this._sync();
  };

  /** 把临时写的条件存成一条选股策略，并立刻切换成引用它。 */
  PoolEditor.prototype.saveAsPool = function () {
    var self = this;
    this._read();
    if (!this.state.where.length) {
      if (window.toast) window.toast('先写至少一条入池条件再保存', 'warn');
      return;
    }
    var name = (window.prompt || function () { return null; })(
      '选股策略名称（保存后股票池下拉框可直接选用）', this.state.where[0]);
    if (!name) return Promise.resolve(null);
    // 返回 promise，便于调用方（与测试）知道保存何时结束
    return window.apiPost('/api/pools', {
      name: name, base: this.state.base,
      conds: this.state.where.slice()
    }).then(function (saved) {
      return window.apiGet('/api/pools').then(function (list) {
        self.setPools(list || []);
        self.sel.value = POOLPRE + saved.id;
        self._sync();
        if (window.toast) {
          window.toast('已保存为选股策略「' + saved.name + '」，当前已切换为引用它', 'ok', 4200);
        }
        return saved;
      });
    }).catch(function (e) {
      if (window.toast) window.toast(String(e && e.message || e), 'err', 6000);
      return null;
    });
  };

  /** 试跑：拿某一天的真实数据看看这个池子能圈出多少只。 */
  PoolEditor.prototype.test = function () {
    var self = this;
    var spec = this.value();
    var s = normalize(spec);
    // poolref（已保存的选股策略）也能试跑，后端会按 id 展开条件
    if (s.kind === 'static' || (s.kind === 'cond' && !s.where.length)) {
      if (this.resultEl) {
        this.resultEl.innerHTML =
          '<div class="empty">当前是静态股票池，没有条件可试跑。' +
          '要试跑请先在条件框里写一条，或一键套用上面的预设。</div>';
      }
      return;
    }
    var date = (this.dateEl && this.dateEl.value)
      || (this.dateInput && this.dateInput.value) || '';
    if (this.resultEl) {
      this.resultEl.innerHTML = '<div class="small muted">试跑中…（全市场约需 10~60 秒）</div>';
    }
    window.apiPost('/api/universe/test', { universe: spec, trade_date: date, sample: 12 })
      .then(function (r) {
        if (!r || !r.ok) {
          self.resultEl.innerHTML = '<div class="empty">' +
            esc((r && r.error) || '试跑失败') + '</div>';
          return;
        }
        var d = r.data;
        var rows = (d.samples || []).map(function (x) {
          return '<tr><td class="mono small">' + esc(x.ts_code) + '</td>' +
            '<td>' + esc(x.name || '') + '</td>' +
            '<td class="small muted">' + esc(x.industry || '') + '</td>' +
            '<td class="num">' + esc(x.close == null ? '' : x.close) + '</td>' +
            '<td class="num">' + esc(x.total_mv == null ? '' : window.fmt.mv(x.total_mv)) + '</td>' +
            '<td class="num">' + esc(x.turnover_rate == null ? '' : x.turnover_rate) + '</td>' +
            '</tr>';
        }).join('');
        var presel = d.preselected
          ? '<span class="tag b">已预筛</span>'
          : '<span class="tag y">含指标，无法预筛</span>';
        self.resultEl.innerHTML =
          '<div class="pool-stat">' +
          '<span>基准日 <b>' + esc(d.trade_date) + '</b></span>' +
          '<span>基础池 <b>' + esc(d.base_size) + '</b> 只</span>' +
          '<span>当日入池 <b class="' + (d.hit_count ? 'up' : 'down') + '">' +
          esc(d.hit_count) + '</b> 只</span>' +
          '<span>' + presel + '</span>' +
          '<span class="muted">' + esc(d.elapsed) + 's</span>' +
          '</div>' +
          (rows
            ? '<table class="data sm"><thead><tr><th>代码</th><th>名称</th><th>行业</th>' +
            '<th class="num">收盘</th><th class="num">总市值(亿)</th>' +
            '<th class="num">换手%</th></tr></thead><tbody>' + rows + '</tbody></table>'
            + '<div class="small muted">仅显示前 12 只样例</div>'
            : '<div class="empty">该日没有任何股票满足池子条件，请把阈值放宽一些</div>');
      })
      .catch(function (e) {
        self.resultEl.innerHTML = '<div class="empty">' + esc(String(e)) + '</div>';
      });
  };

  window.PoolEditor = PoolEditor;
  window.PoolMatch = { normalize: normalize, matchPreset: matchPreset, specLabel: specLabel };
})();
