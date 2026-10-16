/* AI 分析 —— **一份实现两个出口**：

     · 个股诊断页 `/stock/<代码>`：**内联卡片**（`window.aiInline`）。点一下就用
       「默认语句模板 + 当前生效的模型配置」直接开跑，**不弹窗**、不让选模板/模型 ——
       诊断页上用户要的是「这一只怎么看」，不是每次挑配置（要挑去 /screen 或设置页）。
     · 今日选股结果表 `/screen`：**弹窗**（`window.aiAnalyze`），里面能挑这次用哪条
       模板、哪条模型配置（只影响这一次，不改全局默认）。

   两条出口共用同一套「怎么读流」（`streamRun`）、同一套正文渲染（`md`）与
   同一套元信息口径（`metaText` / `fmt.ms`），所以不会出现「弹窗里显示 12.0s、
   卡片里显示 12s」这种分家。

   ## 为什么是流式

   模型调用动辄十几秒到两分钟。以前是「落 running → 前端 1.2s 轮询」，用户盯着
   「模型思考中…」等到最后一次性蹦出全文；现在是服务端边生成边推（SSE），
   前端边收边画 —— 首字延迟从「整段生成完」降到「第一个 token」。
   正文仍然**一路落 MySQL**（`ai_analysis`），所以关页面、流断掉，已经生成的部分
   与错误原因都还在，历史也能回看。

   ⚠ 前端自己拼出来的字符串**不是**最终口径：`done` 事件回的是落库那一份，
     收到它要用它覆盖本地拼的（首尾空白之类的差别就没了）。 */
(function () {
  'use strict';

  let cur = null;         // 当前流的句柄 {ctl, done}
  let modal = null;       // 当前弹窗（只有 /screen 那条路用）
  let busy = false;

  function abort() {
    if (cur) { try { cur.ctl.abort(); } catch (e) { /* 已经结束 */ } cur = null; }
    busy = false;
  }

  /* 模型输出是纯文本，但往往带 **加粗** 与 # 小标题：
     先转义再上这几条最小规则（顺序不能反，否则吃自己的标签）。 */
  function md(text) {
    let h = esc(String(text === null || text === undefined ? '' : text));
    h = h.replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>');
    h = h.replace(/^#{1,6}\s*(.+)$/gm, '<b class="ai-h">$1</b>');
    h = h.replace(/^\s*[-*]\s+/gm, '· ');
    return h.replace(/\n/g, '<br>');
  }

  /* 耗时的显示口径只有一份：`fmt.ms`（app.js）。别在这里再写一份 ——
     两处早晚出现「12.0s」与「12s」两种写法。 */
  const ms = (v) => window.fmt.ms(v);

  /* 记录 → 一行说明（模板/模型/耗时/token/时间）。三处都用它，免得有的地方漏标「模型」。 */
  function metaText(r) {
    r = r || {};
    return [r.template_name ? '模板 ' + r.template_name : '',
            r.model ? '模型 ' + r.model : '',
            ms(r.elapsed_ms),
            r.tokens ? r.tokens + ' tokens' : '',
            r.created_at].filter(Boolean).join(' · ');
  }

  function q(sel) { return modal ? modal.el.querySelector(sel) : null; }

  function say(html, cls) {
    const el = q('#ai-out');
    if (!el) return;
    el.className = 'ai-out ' + (cls || '');
    el.innerHTML = html;
    el.scrollTop = el.scrollHeight;
  }

  function meta(txt) { const el = q('#ai-meta'); if (el) el.textContent = txt || ''; }

  /* 正文 + 思考过程 → 输出区的 HTML。
     ⚠ **推理型模型**（Ollama 的 qwen3 这类）只往思考流里写正文：不给它一块地方，
       用户就只能盯着「思考中」干等四十多秒。所以思考过程也**边到边显示**，
       用 <details> 折起来 —— 还没有正文时默认展开（至少让人看见字在动），
       正文一到就自动收起。 */
  function bodyHtml(think, answer) {
    let h = '';
    if (think) {
      h += `<details class="ai-think"${answer ? '' : ' open'}>`
         + `<summary>模型的思考过程（${think.length} 字）</summary>`
         + `<div class="ai-think-body">${md(think)}</div></details>`;
    }
    if (answer) h += md(answer);
    else if (think) h += '<span class="muted">（还没有正文：模型还在思考）</span>';
    return h;
  }

  /* ---------------------------------------------------------------------------
     流式收发：POST → SSE → 逐个事件交给 onEvent。**唯一**读流的地方。
     返回句柄 `{ctl, done}`（`ctl.abort()` 掐流；`done` 是 Promise，失败会 reject）。
     ⚠ 前置校验没过时服务端回 400 + `{ok:false,error}`（不是流），这里翻成异常抛出，
       调用方按「提交失败」显示 —— 卡片不该先画出来再拆掉。
     --------------------------------------------------------------------------- */
  function streamRun(o, tplId, profId, onEvent) {
    const ctl = new AbortController();
    const done = (async () => {
      const res = await fetch('/api/ai/analyze/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ts_code: o.ts_code, name: o.name, industry: o.industry, trade_date: o.trade_date,
          source: o.source || 'stock', strategy: o.strategy,
          close: o.close, pct_chg: o.pct_chg,
          template_id: tplId || undefined, profile_id: profId || undefined,
        }),
        signal: ctl.signal,
      });
      if (!(res.headers.get('Content-Type') || '').includes('text/event-stream')) {
        let msg = 'HTTP ' + res.status;
        try { const j = await res.json(); if (j && j.error) msg = j.error; } catch (e) { /* 非 JSON */ }
        throw new Error(msg);
      }
      const rd = res.body.getReader();
      const dec = new TextDecoder('utf-8');
      let buf = '';
      const drain = () => {
        for (;;) {
          const i = buf.search(/\r?\n\r?\n/);       // 一节 = data: {json} + 空行
          if (i < 0) return;
          const raw = buf.slice(0, i);
          buf = buf.slice(i).replace(/^\r?\n\r?\n/, '');
          const line = raw.split(/\r?\n/).find(l => l.indexOf('data:') === 0);
          if (!line) continue;
          let ev;
          try { ev = JSON.parse(line.slice(5).trim()); } catch (e) { continue; }
          onEvent(ev);
        }
      };
      for (;;) {
        const r = await rd.read();
        if (r.done) break;
        buf += dec.decode(r.value, { stream: true });   // stream:true 才不会把中文劈成乱码
        drain();
      }
      buf += dec.decode();
      if (buf.trim()) drain();
      return true;
    })();
    return { ctl, done };
  }

  /* ------------------------------- 历史（读 MySQL） ------------------------------- */
  async function histRows(code) {
    if (!code) return [];
    try { return await apiGet('/api/ai/analysis?limit=20&ts_code=' + encodeURIComponent(code)); }
    catch (e) { return []; }
  }

  function statusText(r) {
    return r.status === 'done' ? '完成' : (r.status === 'error' ? '失败' : '进行中');
  }

  /* ===========================================================================
     出口一：个股诊断页的内联卡片
     =========================================================================== */
  const CARD = { box: '#st-card-ai', out: '#ai-card-out', meta: '#ai-card-meta',
                 tag: '#ai-card-tag', note: '#ai-card-note', hist: '#ai-card-hist' };

  const hasCard = () => !!document.querySelector(CARD.out);
  const cq = (sel) => document.querySelector(sel);

  /* 整张卡片先藏着 —— **没分析过的票不该占一整块空卡**：
     进页面 / 换票时由 cardRefresh() 决定要不要摆出来（有历史记录、或正在跑才显示）。
     ⚠ 只藏不改 DOM：里面那些取数与渲染路径跟显示时完全一致（跟 `?embed=1` 一个思路）。 */
  (function hideCardAtStart() {
    const b = cq(CARD.box);
    if (b) b.style.display = 'none';
  })();

  /** 卡片整张显示 / 隐藏（只有「有内容」时才占位）。 */
  function cardShow(on) {
    const b = cq(CARD.box);
    if (b) b.style.display = on ? '' : 'none';
  }

  function cardSay(html, cls) {
    const el = cq(CARD.out);
    if (!el) return;
    el.className = 'ai-out ' + (cls || '');
    el.innerHTML = html;
    el.scrollTop = el.scrollHeight;         // 流式时跟着往下滚（不然只能看到开头）
  }
  function cardMeta(t) { const e = cq(CARD.meta); if (e) e.textContent = t || ''; }
  function cardNote(t) { const e = cq(CARD.note); if (e) e.textContent = t || ''; }
  function cardTag(t, cls) {
    const e = cq(CARD.tag);
    if (!e) return;
    e.textContent = t || '';
    e.className = 'tag ' + (cls || '');
    e.style.display = t ? '' : 'none';      // 没状态就把标签整个收掉，别留个空壳
  }
  function histBtn(rows) {
    const b = cq(CARD.hist);
    if (!b) return;
    b.disabled = !rows.length;
    b.title = rows.length ? ('这只票有 ' + rows.length + ' 条历史回复')
                          : '这只票还没有历史回复';
  }
  function cardBusy(on) {
    const b = document.getElementById('btn-ai');
    if (b) { b.disabled = on; b.textContent = on ? '分析中…' : 'AI 分析'; }
  }

  let cardOpts = null;      // 卡片现在显示的是哪只票（用来防「切票了还在往卡片里写」）

  /** 切票 / 进页面时调用：清卡片 → 有历史就把**最新一条**摆出来（这就是「AI 的最新回复」）。 */
  async function cardRefresh(o) {
    if (!hasCard()) return;
    abort();
    cardOpts = o || null;
    cardSay('<div class="empty">还没有分析过 —— 点「AI 分析」开始</div>');
    cardMeta(''); cardNote(''); cardTag('');
    cardShow(false);                          // 先收起来，等数回来再决定 —— 免得闪一下空卡
    const rows = await histRows(o && o.ts_code);
    histBtn(rows);
    if (!o || !rows.length || cardOpts !== o) return;
    const last = rows.find(r => r.status === 'done') || rows[0];
    if (last.status !== 'done') {
      cardShow(true);                         // 有记录（哪怕失败/没跑完）就该让人看见
      cardTag(last.status === 'error' ? '上次失败' : '');
      cardNote(last.status === 'error'
        ? '上次分析失败：' + (last.error || '') : '上一次分析还没跑完');
      return;
    }
    try {
      const r = await apiGet('/api/ai/analysis/' + last.id);
      if (cardOpts !== o) return;
      if (!r || !r.content) return;           // 记录在但正文读不到 ⇒ 仍当「没分析过」，整卡不摆
      cardShow(true);
      cardSay(md(r.content), 'done');
      cardMeta(metaText(r) + ' · 最新一条历史');
      cardTag('历史');
      cardNote('这是这只票最近一次分析的回复 —— 点「AI 分析」可重新生成。');
    } catch (e) { /* 读不到正文就留着「还没有分析过」，整卡继续藏着，不报错刷屏 */ }
  }

  /** 点「AI 分析」：**默认语句 + 当前模型**直接开跑（不弹窗、不选模板）。 */
  async function cardRun(o) {
    if (!hasCard()) return;
    if (!o || !o.ts_code) { toast('先查询一只股票，再点 AI 分析', 'warn'); return; }
    abort();
    cardOpts = o;
    busy = true;
    cardBusy(true);
    cardShow(true);                           // 一开始跑就摆出来（下面「正在提交…」得让人看见）
    cardSay('<span class="muted">正在提交…</span>');
    cardTag('生成中', 'y');
    cardMeta('');
    cardNote('用「默认语句模板 + 当前模型」分析中…');
    let text = '', think = '';
    const t0 = Date.now();
    const elapsed = () => '生成中 · 已用 ' + Math.round((Date.now() - t0) / 1000) + 's';
    const tick = setInterval(() => {
      if (cardOpts === o && busy) cardNote(elapsed());
    }, 1000);
    try {
      cur = streamRun(o, null, null, (ev) => {
        if (cardOpts !== o) return;                 // 已经切到别的票了，别写错卡片
        if (ev.type === 'start') {
          const r = ev.rec || {};
          cardMeta('模板 ' + (r.template_name || '—') + ' · 模型 ' + (r.model || '—'));
        } else if (ev.type === 'think') {
          think += ev.text || '';                   // ⚠ 思考也要显示，不然推理型模型全程没有反馈
          cardSay(bodyHtml(think, text), 'streaming');
          cardNote(elapsed());
        } else if (ev.type === 'delta') {
          text += ev.text || '';
          cardSay(bodyHtml(think, text), 'streaming');
          cardNote(elapsed());
        } else if (ev.type === 'done') {
          const r = ev.rec || {};
          if (typeof ev.text === 'string') text = ev.text;   // ⚠ 落库那一份才是最终口径
          cardSay(bodyHtml(think, text), 'done');
          cardMeta(metaText(r));
          cardTag(ev.from_think ? '只有思考过程' : '已完成', ev.from_think ? 'y' : '');
          cardNote(ev.from_think
            ? '这个模型没给正文、只回了思考过程 —— 已按正文显示（在「模型设置」里换个模型往往更直接）。'
            : '');
        } else if (ev.type === 'error') {
          cardSay(esc(ev.error || '分析失败'), 'err');
          cardTag('失败', 'y');
        }
      });
      await cur.done;
    } catch (e) {
      if (!(e && e.name === 'AbortError')) {
        cardSay(esc(e.message || String(e)), 'err');
        cardTag('失败', 'y');
        cardMeta('');
      }
    } finally {
      clearInterval(tick);
      cardBusy(false);
      busy = false;
      cur = null;
      if (cardOpts === o) histBtn(await histRows(o.ts_code));    // 刚跑完就多了一条历史
    }
  }

  /** 右下角「历史回复」：列出这只票的历史，点某条就把它的正文摆到卡片里。 */
  async function openHistory(o) {
    if (!o || !o.ts_code) return;
    const rows = await histRows(o.ts_code);
    if (!rows.length) { toast('这只票还没有历史回复', 'warn'); return; }
    if (modal) modal.close();
    modal = window.mkModal('AI 历史回复 · ' + o.ts_code + (o.name ? ' ' + o.name : ''),
      `<div class="small muted" style="margin:0 0 8px">共 ${rows.length} 条（存在 MySQL，最新的在最上）。
         点「查看」把那条的正文放到下面的卡片里。</div>
       <div class="tbl-wrap" style="max-height:420px">
       <table class="data"><thead><tr><th>时间</th><th>模板</th><th>模型</th>
         <th class="num">耗时</th><th class="num">token</th><th>状态</th><th>操作</th></tr></thead><tbody>
       ${rows.map(r => `<tr>
         <td class="mono small">${esc(r.created_at)}</td>
         <td class="small">${esc(r.template_name || '—')}</td>
         <td class="small muted">${esc(r.model || '—')}</td>
         <td class="num small">${esc(ms(r.elapsed_ms) || '—')}</td>
         <td class="num small">${r.tokens == null ? '—' : esc(String(r.tokens))}</td>
         <td class="small ${r.status === 'done' ? '' : (r.status === 'error' ? 'down' : 'muted')}">
           ${statusText(r)}</td>
         <td><button class="btn sm" data-view="${r.id}">查看</button>
             <button class="btn sm ghost" data-del="${r.id}">删除</button></td>
       </tr>`).join('')}</tbody></table></div>`,
      `<span class="small muted" style="margin-right:auto">${esc(o.ts_code)}</span>
       <button class="btn sm primary" data-close>关闭</button>`,
      () => { modal = null; }, true);

    const box = modal.el;
    $$('[data-view]', box).forEach(b => b.onclick = guardClick(b, async () => {
      try {
        const r = await apiGet('/api/ai/analysis/' + b.dataset.view);
        if (r.status !== 'done') {
          cardSay(esc(r.error || '这条记录没有正文'), 'err');
          cardTag(statusText(r), 'y');
        } else {
          cardSay(md(r.content), 'done');
          cardTag('历史', '');
        }
        cardMeta(metaText(r));
        cardNote('正在看 ' + r.created_at + ' 那条历史回复 —— 点「AI 分析」重新生成。');
        cardOpts = o;
        modal.close();
      } catch (e) { toast(e.message, 'err'); }
    }));
    $$('[data-del]', box).forEach(b => b.onclick = guardClick(b, async () => {
      try {
        await apiPost('/api/ai/analysis/' + b.dataset.del + '/delete', {});
        modal.close();
        await cardRefresh(o);                  // 删完重算「最新一条」与历史条数
      } catch (e) { toast(e.message, 'err'); }
    }));
  }

  /* ===========================================================================
     出口二：/screen 的弹窗（能挑这次的模板与模型配置）
     =========================================================================== */
  async function loadHistory(code) {
    const box = q('#ai-hist');
    if (!box) return;
    const rows = await histRows(code);
    if (!rows.length) {
      box.innerHTML = '<div class="small muted" style="margin:14px 0 4px">还没有历史记录</div>';
      return;
    }
    box.innerHTML = `<div class="small muted" style="margin:14px 0 6px">历史记录（存在 MySQL，共 ${rows.length} 条）</div>
      <table class="data"><thead><tr><th>时间</th><th>模板</th><th>模型</th>
        <th class="num">耗时</th><th>状态</th><th>操作</th></tr></thead><tbody>
      ${rows.map(r => `<tr>
        <td class="mono small">${esc(r.created_at)}</td>
        <td class="small">${esc(r.template_name || '—')}</td>
        <td class="small muted">${esc(r.model || '—')}</td>
        <td class="num small">${esc(ms(r.elapsed_ms) || '—')}</td>
        <td class="small ${r.status === 'done' ? '' : (r.status === 'error' ? 'down' : 'muted')}">
          ${statusText(r)}</td>
        <td><button class="btn sm" data-view="${r.id}">查看</button>
            <button class="btn sm ghost" data-del="${r.id}">删除</button></td>
      </tr>`).join('')}</tbody></table>`;
    $$('[data-view]', box).forEach(b => b.onclick = guardClick(b, async () => {
      try {
        const r = await apiGet('/api/ai/analysis/' + b.dataset.view);
        abort();
        say(md(r.content || ('（无正文）' + (r.error ? ' ' + esc(r.error) : ''))),
            r.status === 'done' ? 'done' : 'err');
        meta(metaText(r));
        q('#ai-tpl').value = r.template_id || q('#ai-tpl').value;
      } catch (e) { toast(e.message, 'err'); }
    }));
    $$('[data-del]', box).forEach(b => b.onclick = guardClick(b, async () => {
      try { await apiPost('/api/ai/analysis/' + b.dataset.del + '/delete', {}); loadHistory(code); }
      catch (e) { toast(e.message, 'err'); }
    }));
  }

  async function runModal(o, templateId, profileId) {
    abort();
    busy = true;
    const btn = q('#ai-run');
    const paint = (b) => { if (btn) { btn.disabled = b; btn.textContent = b ? '分析中…' : '开始分析'; } };
    paint(true);
    say('<span class="muted">正在提交…</span>');
    let text = '', think = '';
    const t0 = Date.now();
    const tick = setInterval(() => {
      if (busy) meta('生成中 · 已用 ' + Math.round((Date.now() - t0) / 1000) + 's'
                     + '（关掉弹窗不影响：正文一路存进 MySQL）');
    }, 1000);
    try {
      cur = streamRun(o, templateId, profileId, (ev) => {
        if (ev.type === 'start') {
          const r = ev.rec || {};
          meta('模板 ' + (r.template_name || '—') + ' · 模型 ' + (r.model || '—'));
        } else if (ev.type === 'think') {
          think += ev.text || '';
          say(bodyHtml(think, text), 'streaming');
        } else if (ev.type === 'delta') {
          text += ev.text || '';
          say(bodyHtml(think, text), 'streaming');
        } else if (ev.type === 'done') {
          const r = ev.rec || {};
          if (typeof ev.text === 'string') text = ev.text;   // 落库那一份才是最终口径
          say(bodyHtml(think, text), 'done');
          meta(metaText(r) + (ev.from_think ? ' · 模型只回了思考过程，已按正文显示' : ''));
        } else if (ev.type === 'error') {
          say(esc(ev.error || '分析失败'), 'err');
        }
      });
      await cur.done;
    } catch (e) {
      if (!(e && e.name === 'AbortError')) say(esc(e.message || String(e)), 'err');
    } finally {
      clearInterval(tick);
      paint(false);
      busy = false;
      cur = null;
      loadHistory(o.ts_code);
    }
  }

  /* ------------------------------- 入口 ------------------------------- */
  window.aiInline = {
    /** 切票后刷新卡片（有历史就把最新一条摆出来） */
    refresh: cardRefresh,
    /** 点「AI 分析」：默认语句 + 当前模型，直接开跑 */
    run: cardRun,
    /** 右下角「历史回复」 */
    history: openHistory,
    /** 探针/调试用：卡片里现在显示的是哪只票 */
    code: () => (cardOpts && cardOpts.ts_code) || '',
  };

  window.aiAnalyze = async function (opts) {
    const o = Object.assign({}, opts || {});
    if (!o.ts_code) { toast('缺少股票代码', 'warn'); return null; }
    if (modal) modal.close();

    const title = 'AI 分析 · ' + o.ts_code + (o.name ? ' ' + o.name : '');
    modal = window.mkModal(title,
      `<div class="row" style="align-items:flex-end">
         <div class="field"><label class="lb">分析语句模板</label><select id="ai-tpl"></select></div>
         <div class="field"><label class="lb">模型（本次用哪条配置）</label><select id="ai-prof"></select></div>
         <div class="field" style="flex:0 0 auto"><button class="btn primary" id="ai-run">开始分析</button></div>
         <div class="field" style="flex:0 0 auto">
           <a class="btn" href="/settings" target="_blank" title="配模型接入与语句模板">模板 / 模型设置</a></div>
       </div>
       <div class="small muted" id="ai-meta" style="margin:2px 0 10px"></div>
       <div class="ai-out" id="ai-out"></div>
       <div id="ai-hist"></div>`,
      `<span class="small muted" style="margin-right:auto" id="ai-note"></span>
       <button class="btn sm primary" data-close>关闭</button>`,
      () => { abort(); modal = null; }, true);

    let cfg = null;
    try { cfg = await apiGet('/api/ai/config'); } catch (e) { /* 模型设置读不到也让人看历史 */ }
    if (!modal) return null;                     // 加载过程中被关掉了

    const tpls = (cfg && cfg.templates) || [];
    const profiles = (cfg && cfg.profiles) || [];
    const sel = (tpls.find(t => t.is_default) || tpls[0] || {});
    const s = q('#ai-tpl');
    if (s) {
      s.innerHTML = tpls.map(t =>
        `<option value="${t.id}" ${String(t.id) === String(sel.id) ? 'selected' : ''}>${esc(t.name)}${t.is_default ? '（默认）' : ''}</option>`).join('');
      s.onchange = () => { if (!busy) runModal(o, s.value, profId()); };
    }

    /* 模型下拉 = 「**这一次**用哪条配置」（「模型设置」页配的那几条），默认停在当前生效那条。
       换它**不改**全局默认 —— 改默认是「模型设置」页「设为当前」的事。 */
    const ps = q('#ai-prof');
    function profId() { return (ps && ps.value) || (cfg && cfg.active_id); }
    function profNow() { return profiles.find(p => String(p.id) === String(profId())) || null; }
    if (ps) {
      ps.innerHTML = profiles.map(p =>
        `<option value="${p.id}" ${p.is_active ? 'selected' : ''}>${esc(p.name)}`
        + `${p.model ? ' · ' + esc(p.model) : ''}${p.ready ? '' : '（未配好）'}</option>`).join('');
      ps.onchange = paintNote;
    }
    function paintNote() {
      const note = q('#ai-note');
      const run0 = q('#ai-run');
      const p = profNow();
      const ok = !!(p && p.ready);
      if (note) {
        note.textContent = ok
          ? ('本次用 ' + p.name + ' · ' + p.model + (p.is_active ? '' : '（不是当前默认）'))
          : (profiles.length ? '这条配置还不能用 —— 去「模型设置」填地址与模型名'
                             : '还没配置模型接入 —— 去「模型设置」新建一条');
      }
      if (run0) run0.disabled = !ok;
    }
    paintNote();
    loadHistory(o.ts_code);

    if (!profNow() || !profNow().ready) {
      say('<span class="muted">还没有可用的模型配置。请到「模型设置」新建一条'
          + '（Ollama 本地 / 远程 API 都有预设），填好地址与模型名，必要时填 API Key，'
          + '点「测试连接」通过后回来即可分析。多条配置可以在这里随时切换。</span>', 'err');
      return modal;
    }
    runModal(o, (sel || {}).id, profId());
    return modal;
  };
})();
