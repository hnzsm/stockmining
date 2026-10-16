/* 表达式输入提示（自动补全）—— 选股条件 / 策略条件 / 评分表达式共用
 *
 * 条件框里写的是一套类公式的迷你语言（见 core/expr.py）：
 *   close <= boll_lower * 1.02
 *   cross_above(ma5, ma20) and vol > ref(vol, 1) * 1.5
 * 字段名有 100 个、函数 30 来个，靠记不现实。所以输入字母时弹出候选：
 * 前缀命中排在前面，子串命中排在后面。
 *
 * 两种输入位置都接同一份实现（2026-09-18 起选股逻辑只归选股策略）：
 *   - 买卖策略的条件行（买入触发 / 卖出信号 / 评分）—— 都是日线口径，weekly:false
 *   - 选股策略管理页 / 回测与选股页股票池面板的入池条件 —— 也能写 w_ 周线字段
 *     （weekly:false 时，**输入以 w_ 开头的词**才会列出周线字段，见 pool()）
 *
 * 不再有「周线过滤」那一组：它的 w_ 条件已经挪进选股策略，
 * weekly:true 这个开关保留给「只允许 w_ 字段」的输入框用。
 *
 * 字段与函数清单**不写死在前端**，从接口拉（/api/universe/fields 带说明、
 * /api/fields 给函数签名），进程内缓存一次。接口不可用时就安静地不提示，
 * 不影响输入。
 *
 * 用法：
 *   ExprAC.attach(el, { weekly: false })
 *   ExprAC.attach(el, { weekly: () => el.closest('[data-weekly]') != null })
 *
 * 键盘：↑↓ 选择 · Enter/Tab 补全 · Esc 关闭 · Ctrl+Space 空词也弹（列全部）
 * 补全后会自动派发一个 input 事件，页面原有的「标记未保存」「读回条件」逻辑照常触发。
 *
 * 弹层位置：固定定位（position:fixed），贴在控件正下方、左边缘对齐控件，
 * 宽度跟随控件；下方空间不够时翻到控件上方。滚动/缩放时直接收起。
 * 候选超过 10 条时列表自己上下滚（max-height 230），↑↓ 选到看不见的项时列表跟着滚。
 */
(function (root, factory) {
  var api = factory();
  root.ExprAC = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis, function () {
  'use strict';

  var IDENT = /^[A-Za-z_][A-Za-z0-9_]*$/;
  var TOKEN_L = /[A-Za-z_][A-Za-z0-9_]*$/;   // 光标左侧正在输入的词
  var TOKEN_R = /^[A-Za-z0-9_]*/;            // 光标右侧的词尾（补全时一并替换）
  var MAX_ITEMS = 10;
  var MIN_W = 236, MAX_W = 380;   // 弹层宽度区间（与 .ac-pop 的 min/max-width 一致）
  var GAP = 4;                    // 弹层与控件之间的缝

  /* ==================== 纯函数（不碰 DOM，可离线测） ==================== */

  function isIdent(s) {
    return IDENT.test(String(s == null ? '' : s));
  }

  /** 光标处的词。member=true 表示这是 `1.5` / `a.b` 的一部分，不该提示。 */
  function token(text, pos) {
    text = String(text == null ? '' : text);
    if (pos == null || pos > text.length || pos < 0) pos = text.length;
    var head = text.slice(0, pos);
    var m = TOKEN_L.exec(head);
    if (!m) return { word: '', start: pos, end: pos, member: false, empty: true };
    var start = pos - m[0].length;
    var prev = start > 0 ? head.charAt(start - 1) : '';
    if (prev === '.' || (prev >= '0' && prev <= '9')) {
      return { word: m[0], start: start, end: pos, member: true, empty: false };
    }
    var tail = TOKEN_R.exec(text.slice(pos))[0];
    return { word: m[0], start: start, end: pos + tail.length, member: false, empty: false };
  }

  /** 光标处是不是在 `#` 注释里（同一行、光标之前出现过 `#` 或全角 `＃`）。
   *  注释不是条件，提示字段名只会误导 —— 口径与 core/expr.py 的 `strip_comment` 一致。 */
  function inComment(text, pos) {
    text = String(text == null ? '' : text);
    if (pos == null || pos > text.length || pos < 0) pos = text.length;
    var lineStart = text.lastIndexOf('\n', pos - 1) + 1;
    var head = text.slice(lineStart, pos);
    return head.indexOf('#') >= 0 || head.indexOf('＃') >= 0;
  }

  /** 字段清单：[{name, kind:'field', desc}]。接口给的说明优先，缺的补空。 */
  function fieldsFrom(names, rich) {
    var desc = {}, order = [];
    function put(n, d) {
      n = String(n == null ? '' : n).trim();
      if (!isIdent(n) || desc[n] !== undefined) return;   // 挡掉 "ma5…ma250" 这种展示名
      desc[n] = d == null ? '' : String(d);
      order.push(n);
    }
    (rich || []).forEach(function (f) { if (f) put(f.name, f.desc); });
    (names || []).forEach(function (n) { put(n, ''); });
    return order.map(function (n) { return { name: n, kind: 'field', desc: desc[n] }; });
  }

  /** 函数清单：把 "ma(x, n) / ema(x, n)"、"sum/mean/std/count(x, n)" 这类写法拆成单个函数。 */
  function parseFuncs(raw) {
    var out = [];
    var RE = /^([A-Za-z_][A-Za-z0-9_]*(?:\s*\/\s*[A-Za-z_][A-Za-z0-9_]*)*)\s*(?:\(([^)]*)\))?\s*$/;
    (raw || []).forEach(function (f) {
      if (!f) return;
      String(f.signature || '').split(' / ').forEach(function (part) {
        var m = RE.exec(String(part).trim());
        if (!m) return;
        var args = String(m[2] == null ? '' : m[2]).trim();
        m[1].split('/').forEach(function (nm) {
          nm = nm.trim();
          if (!nm) return;
          out.push({
            name: nm, kind: 'func', desc: f.desc == null ? '' : String(f.desc),
            sig: nm + '(' + args + ')',
            args: args ? args.split(',').map(function (s) { return s.trim(); }) : []
          });
        });
      });
    });
    return out;
  }

  /** 候选排序：完全相等 → 前缀命中 → 子串命中；同级里字段在前、名字短的在前。 */
  function rank(word, items) {
    var w = String(word == null ? '' : word).toLowerCase();
    var out = [];
    (items || []).forEach(function (it, i) {
      var n = String(it.name).toLowerCase();
      var sc;
      if (!w) sc = 3;
      else if (n === w) sc = 0;
      else if (n.indexOf(w) === 0) sc = 1;
      else if (n.indexOf(w) > 0) sc = 2;
      else return;
      out.push({ sc: sc, len: n.length, kind: it.kind === 'field' ? 0 : 1, name: n, i: i, it: it });
    });
    out.sort(function (a, b) {
      return a.sc - b.sc || a.kind - b.kind || a.len - b.len
        || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0) || a.i - b.i;
    });
    return out.map(function (x) { return x.it; }).slice(0, MAX_ITEMS);
  }

  /** 按候选补全文本，返回新文本与光标位置。函数补成 name(…) 并把光标放进括号。 */
  function complete(text, tk, item) {
    text = String(text == null ? '' : text);
    var before = text.slice(0, tk.start);
    var after = text.slice(tk.end);
    var ins = item.name;
    var caret = before.length + ins.length;
    if (item.kind === 'func') {
      ins += '(' + (after.charAt(0) === ')' ? '' : ')');
      caret = before.length + item.name.length + 1;
    }
    return { text: before + ins + after, caret: caret, inserted: ins };
  }

  /** 当前输入位置该给哪些候选（周线过滤只给 w_ 字段；平时不拿 w_ 凑数）。 */
  function pool(word, items, weekly) {
    var w = String(word == null ? '' : word).toLowerCase();
    return (items || []).filter(function (it) {
      if (it.kind === 'func') return true;
      var isW = it.name.slice(0, 2) === 'w_';
      if (weekly) return isW;
      if (isW && w.indexOf('w_') !== 0) return false;
      return true;
    });
  }

  /* ==================== 清单：拉一次，缓存 ==================== */

  var _cache = null, _pending = null;

  function load(force) {
    if (_cache && !force) return Promise.resolve(_cache);
    if (_pending && !force) return _pending;
    var get = (typeof window !== 'undefined' && window.apiGet) || null;
    if (!get) return Promise.resolve({ fields: [], funcs: [] });
    _pending = Promise.resolve()
      .then(function () { return get('/api/fields'); })
      .then(function (f) {
        f = f || {};
        var help = f.field_help || {};
        var names = f.fields;
        if (!names || !names.length) {
          // 老接口：字段说明只在 /api/universe/fields 里
          return Promise.resolve()
            .then(function () { return get('/api/universe/fields'); })
            .catch(function () { return []; })
            .then(function (rich) {
              return { fields: fieldsFrom(null, rich), funcs: parseFuncs(f.functions) };
            });
        }
        var seen = {}, fields = [];
        names.forEach(function (n) {
          n = String(n == null ? '' : n).trim();
          if (!isIdent(n) || seen[n]) return;
          seen[n] = 1;
          fields.push({ name: n, kind: 'field', desc: help[n] || '' });
        });
        return { fields: fields, funcs: parseFuncs(f.functions) };
      })
      .catch(function () { return { fields: [], funcs: [] }; })
      .then(function (d) { _cache = d; return d; });
    return _pending;
  }

  /* ==================== 定位（固定定位 + 视口坐标） ====================
   * 弹层就贴在控件正下方、左边缘对齐控件——和 <select> 的下拉一个样。
   * 早先是跟着光标走的（用影子 div 量光标坐标），但条件框是 height:38px 的
   * 单行 textarea，光标行底 ≈ 框内 30px 处，弹层顶上来正好压住输入框下半截。
   * 现在只认控件自身的 rect，量歪了也只会差几像素，不会盖住输入框。
   */

  /* ==================== DOM ==================== */

  function attach(el, opts) {
    if (!el || !el.addEventListener) return null;
    opts = opts || {};
    var doc = (typeof document !== 'undefined') ? document : null;
    if (!doc || !doc.body || !doc.createElement) return null;   // Node 测试环境：只跑纯函数

    var st = { items: [], hits: [], active: 0, open: false, tk: null, suppress: false };
    var pop = null, listEl = null;

    function weekly() {
      return typeof opts.weekly === 'function' ? !!opts.weekly() : !!opts.weekly;
    }

    function ensurePop() {
      if (pop) return pop;
      pop = doc.createElement('div');
      pop.className = 'ac-pop';
      pop.style.display = 'none';
      listEl = doc.createElement('div');
      listEl.className = 'ac-list';
      pop.appendChild(listEl);
      var tip = doc.createElement('div');
      tip.className = 'ac-tip';
      tip.textContent = '↑↓ 选择 · Enter / Tab 补全 · Esc 关闭';
      pop.appendChild(tip);
      doc.body.appendChild(pop);
      if (listEl.addEventListener) {
        // mousedown 而不是 click：否则输入框先 blur，下拉已经被关掉了。
        // 无论点到行还是点到滚动条都要 preventDefault —— 不拦的话拖滚动条会让
        // 输入框失焦，blur 立刻把弹层关掉，看起来就是"滚动条拖不动"。
        listEl.addEventListener('mousedown', function (ev) {
          ev = ev || {};
          if (ev.preventDefault) ev.preventDefault();
          var row = ev.target && ev.target.closest ? ev.target.closest('.ac-row') : null;
          if (!row) return;                          // 点在滚动条/空白处：只保焦点，不补全
          apply(parseInt(row.getAttribute('data-i'), 10));
        });
      }
      if (typeof window !== 'undefined' && window.addEventListener) {
        // 页面/内层容器滚动时收起弹层；但**弹层自己列表的滚动要放行**，
        // 否则滚轮一滚就触发 scroll → hide()，候选列表永远滚不动。
        // （scroll 不冒泡，但捕获阶段 window 能收到任意元素的滚动事件。）
        window.addEventListener('scroll', onScroll, true);
        window.addEventListener('resize', hide);
      }
      return pop;
    }

    /** 弹层内部的滚动（候选太多要上下翻）当自己人，别收起弹层。 */
    function onScroll(ev) {
      var t = ev && ev.target;
      if (t && pop && (t === pop || t === listEl
        || (pop.contains && pop.contains(t)))) return;
      hide();
    }

    function hide() {
      st.open = false;
      if (pop) pop.style.display = 'none';
    }

    /** 把当前项滚进列表可见区：**只改列表自己的 scrollTop**。
     *  不用 scrollIntoView —— 它会连带滚动页面/祖先容器，而页面一滚，
     *  window 上的捕获监听（onScroll）就把弹层收掉了。
     *  行矩形随 scrollTop 移动，所以算出的偏移一步到位；越界由浏览器夹住。 */
    function reveal(row) {
      if (!row || !listEl) return;
      if (!row.getBoundingClientRect || !listEl.getBoundingClientRect) return;
      var rr = row.getBoundingClientRect(), lr = listEl.getBoundingClientRect();
      var d = 0;
      if (rr.top < lr.top) d = rr.top - lr.top;                     // 当前项在可视区上方
      else if (rr.bottom > lr.bottom) d = rr.bottom - lr.bottom;    // 在可视区下方
      if (!d) return;
      listEl.scrollTop = (listEl.scrollTop || 0) + d;
    }

    function highlight() {
      if (!listEl || !listEl.children) return;
      var cur = null;
      for (var i = 0; i < listEl.children.length; i++) {
        var c = listEl.children[i];
        if (!c.classList) continue;
        if (i === st.active) { c.classList.add('on'); cur = c; } else c.classList.remove('on');
      }
      reveal(cur);
    }

    function place() {
      var r = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
      if (!r) return;
      var vw = (typeof window !== 'undefined' && window.innerWidth) || 1200;
      var vh = (typeof window !== 'undefined' && window.innerHeight) || 800;
      // 宽度跟控件对齐（像下拉框），太窄就保底 236，太宽则被 CSS 的 max-width 收住
      pop.style.minWidth = Math.round(Math.min(Math.max(r.width || 0, MIN_W), MAX_W)) + 'px';
      var w = pop.offsetWidth || MIN_W;
      var h = pop.offsetHeight || 0;

      var left = r.left;
      if (left + w > vw - 10) left = vw - w - 10;       // 右边放不下就往左收
      if (left < 8) left = 8;

      // 首选「控件正下方」
      var top = r.bottom + GAP;
      if (h && top + h > vh - 8) {
        var up = r.top - h - GAP;                        // 下方不够：翻到控件上方
        var belowRoom = vh - 8 - top, aboveRoom = r.top - GAP - 8;
        if (aboveRoom > belowRoom) top = Math.max(8, up);
        else top = Math.max(8, vh - h - 8);
      }
      pop.style.left = Math.round(left) + 'px';
      pop.style.top = Math.round(top) + 'px';
    }

    function render(word) {
      var hits = rank(word, pool(word, st.items, weekly()));
      st.hits = hits;
      if (!hits.length) { hide(); return false; }
      ensurePop();
      if (st.active >= hits.length) st.active = 0;
      listEl.innerHTML = '';
      listEl.scrollTop = 0;                          // 重排后从第一项开始（别停在上一轮的滚动位置）
      hits.forEach(function (it, i) {
        var row = doc.createElement('div');
        row.className = 'ac-row' + (i === st.active ? ' on' : '');
        row.setAttribute('data-i', String(i));
        var n = doc.createElement('span');
        n.className = 'ac-n';
        n.textContent = it.kind === 'func' ? it.sig : it.name;
        row.appendChild(n);
        if (it.desc) {
          var d = doc.createElement('span');
          d.className = 'ac-d';
          d.textContent = it.desc;
          row.appendChild(d);
        }
        if (it.kind === 'func') {
          var k = doc.createElement('span');
          k.className = 'ac-k';
          k.textContent = '函数';
          row.appendChild(k);
        }
        listEl.appendChild(row);
      });
      pop.style.visibility = 'hidden';
      pop.style.display = 'block';
      place();
      pop.style.visibility = '';
      st.open = true;
      return true;
    }

    function update(force) {
      if (!st.items.length) return;                 // 清单还没到，先不弹
      var tk = token(el.value, el.selectionStart);
      st.tk = tk;
      if (tk.member) { hide(); return; }
      if (inComment(el.value, el.selectionStart)) { hide(); return; }   // 注释里不提示字段
      if (!tk.word && !force) { hide(); return; }
      st.active = 0;
      render(tk.word);
    }

    function apply(i) {
      var it = st.hits[i];
      if (!it) return;
      var tk = st.tk || token(el.value, el.selectionStart);
      var out = complete(el.value, tk, it);
      el.value = out.text;
      try { el.setSelectionRange(out.caret, out.caret); } catch (e) { }
      hide();
      st.suppress = true;                           // 别让自己的 input 事件把下拉又叫回来
      try {
        if (typeof Event === 'function' && el.dispatchEvent) {
          el.dispatchEvent(new Event('input', { bubbles: true }));
        } else if (el.dispatch) {
          el.dispatch('input');
        }
      } catch (e) { }
      if (opts.onInsert) opts.onInsert(el, it);
    }

    function onInput() {
      if (st.suppress) { st.suppress = false; return; }
      update(false);
    }

    function onKey(ev) {
      ev = ev || {};
      var k = ev.key;
      if (k === 'Escape') {
        if (st.open) { hide(); if (ev.preventDefault) ev.preventDefault(); }
        return;
      }
      if (!st.open) {
        if (k === ' ' && (ev.ctrlKey || ev.metaKey)) {
          if (ev.preventDefault) ev.preventDefault();
          update(true);
        }
        return;
      }
      if (k === 'ArrowDown' || k === 'ArrowUp') {
        if (!st.hits.length) return;
        var n = st.hits.length;
        st.active = (st.active + (k === 'ArrowDown' ? 1 : n - 1)) % n;
        highlight();
        if (ev.preventDefault) ev.preventDefault();
        return;
      }
      if (k === 'Enter' || k === 'Tab') {
        if (st.hits.length) {
          if (ev.preventDefault) ev.preventDefault();
          apply(st.active);
        }
      }
    }

    el.addEventListener('input', onInput);
    el.addEventListener('keydown', onKey);
    el.addEventListener('blur', hide);
    el.addEventListener('click', function () { update(false); });

    var ctrl = { el: el, hide: hide, open: function () { update(true); }, token: token };
    load().then(function (d) {
      st.items = (d.fields || []).concat(d.funcs || []);
      if (opts.onReady) opts.onReady(st.items.length);
      // 清单到得比用户手速慢：补齐一次
      if (st.items.length && doc.activeElement === el) update(false);
    });

    return ctrl;
  }

  return {
    attach: attach, load: load, token: token, rank: rank, complete: complete,
    pool: pool, fieldsFrom: fieldsFrom, parseFuncs: parseFuncs, isIdent: isIdent,
    inComment: inComment,
    MAX_ITEMS: MAX_ITEMS, _reset: function () { _cache = null; _pending = null; }
  };
});
