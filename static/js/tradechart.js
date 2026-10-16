/* 单笔交易复盘图：个股 K 线 + 买卖点标注 + 可切换的下挂指标
 *
 * **一个 ECharts 实例、多个 grid**（K 线格 + 指标格，共用一条时间轴）：
 *   「拖 K 线的时间轴、下面那格跟着走」不是靠两张图互相喊，而是它们本来就在同一个
 *   dataZoom 组件下（`xAxisIndex` 收全部下标）。以前这里是两张独立实例、各带自己的
 *   dataZoom —— 拖上面那张，下面那张纹丝不动（2026-09-20 报的）。
 *
 * 指标格画哪个指标由调用方给 key（来自接口的 catalog，与个股诊断页同一份目录），
 * 切指标只需重新 setOption，窗口由调用方回传保持。
 *
 * 依赖 app.js 提供：fmt / esc / baseAxis / withBaseAxis / tooltipBase / reasonText /
 * CHART_PAL / CHART_FMT / CHART_LEGEND / mkChart。
 * option 构造是纯函数，便于在 Node 里用 ECharts SSR 校验（tools/chart_preview.js）。
 */
(function () {
  'use strict';

  const C_UP = '#e5352b', C_DOWN = '#12a06a';   // A股习惯：涨红跌绿
  const C_BUY = '#e5352b', C_SELL = '#12a06a';
  const NEUTRAL = '#8b95a9';

  const PAL = window.CHART_PAL || {};
  const FMT = window.CHART_FMT || {};
  const LEGEND_STYLE = window.CHART_LEGEND || {};

  /* 布局（像素；容器宽自适应，高度按这个算出来写回元素）。
     TOP 这一段**不是**留给图例的装饰：K 线格图例在第一行、持有段信息条在第二行，
     两者都在 K 线格**上方** —— 以前「持有收益 / 天数」是挂在持有段中点的 markPoint，
     正好盖在那段 K 线上（2026-09-20 报的）。信息条留在图外，横向用日期区间指认是哪一段。 */
  const LAY = { TOP: 54, TAG_TOP: 26, TAG_H: 20, KH: 300, GAP: 34, IH: 118, BOT: 54 };
  const FONT = '-apple-system,"Segoe UI","Microsoft YaHei",sans-serif';
  const GUIDE_PREFIX = '__guide__';   // 参考线专用（空数据）series，不进图例/tooltip
  const BS_NAME = '__bs__';           // 买卖日竖线专用（空数据）series，同理

  function money(v) { return window.fmt.price(v); }
  function pct(v) { return window.fmt.pct(v); }
  function shortD(s) { return String(s || '').slice(5); }

  /* 估算文本宽度：graphic 的矩形要自己给宽（ECharts 不会替量）。只用来定边框，
     估窄了文字会出框，所以按「中文 1 字宽、ASCII 0.56 字宽」往宽里估。 */
  function textW(s, size) {
    let w = 0;
    for (const ch of String(s)) w += (ch.charCodeAt(0) < 128 ? 0.56 : 1) * size;
    return w;
  }

  /* ---------------- 指标格：选哪一组 ---------------- */
  function pickGroup(res, indKey) {
    const cat = (res && res.indicators) || [];
    if (!cat.length) return null;
    return cat.find(g => g.key === indKey)
        || cat.find(g => g.key === 'macd')
        || cat[0] || null;
  }

  /* 空数据的组（本地没同步该数据源）不占格子 —— 前端会在下拉里标「无数据」并提示 */
  function shownGroup(res, indKey) {
    const g = pickGroup(res, indKey);
    return (g && !g.empty) ? g : null;
  }

  function tradeReviewHeight(res, indKey) {
    return LAY.TOP + LAY.KH + (shownGroup(res, indKey) ? LAY.GAP + LAY.IH : 0) + LAY.BOT;
  }

  /* ---------------- tooltip ---------------- */
  function klineTip(p, bars, mk, res) {
    const i = p[0].dataIndex, b = bars[i];
    if (!b) return '';
    const head = '<b>' + b.trade_date + '</b>　开 ' + money(b.open) + ' 高 ' + money(b.high) +
                 ' 低 ' + money(b.low) + ' 收 ' + money(b.close) + '（' + pct(b.pct_chg) + '）';
    const lines = [];
    if (mk.buy && mk.buy.date === b.trade_date) {
      const slip = (mk.buy.open && isFinite(Number(res.slippage)))
        ? (mk.buy.price / mk.buy.open - 1) * 100 : null;
      lines.push('<div style="color:#ff8a80">● 本笔买入 ' + money(mk.buy.price) +
        (slip === null ? '' : '　当日开盘 ' + money(mk.buy.open) +
          '（滑点 ' + (slip >= 0 ? '+' : '') + slip.toFixed(2) + '%）') + '</div>');
    }
    if (mk.sell && mk.sell.date === b.trade_date) {
      const rsn = (window.reasonText && window.reasonText[mk.sell.reason]) || mk.sell.reason || '';
      lines.push('<div style="color:#7ee0b0">● 本笔卖出 ' + money(mk.sell.price) +
                 '　原因：' + window.esc(rsn) + '</div>');
    }
    const tags = [];
    if (b.sig_entry) tags.push('选股成立');
    if (b.sig_buy) tags.push('买入触发');
    if (b.sig_sell) tags.push('卖出触发');
    if (tags.length) lines.push('<div style="color:#ffd479">信号：' + tags.join(' / ') + '</div>');
    const vals = p.filter(x => x.seriesName !== 'K线' && x.seriesName !== '其它信号日' &&
        x.seriesName !== '持有段' && !Array.isArray(x.value) &&
        x.value !== null && x.value !== undefined)
      .map(x => '<div>' + x.marker + x.seriesName + ': ' + money(x.value) + '</div>').join('');
    return head + lines.join('') + (vals ? '<div style="margin-top:4px">' + vals + '</div>' : '');
  }

  /* 指标格：表头固定带上 K 线的开高低收 —— 拖到哪一根都看得见形态（与诊断页一致） */
  function indTip(p, bars, group, fmtByLabel) {
    const b = bars[p[0].dataIndex];
    if (!b) return '';
    const head = '<b>' + window.esc(b.trade_date) + '</b>　开 ' + money(b.open) + ' 高 ' + money(b.high) +
                 ' 低 ' + money(b.low) + ' 收 ' + money(b.close) + '（' + pct(b.pct_chg) + '）';
    const rows = p.filter(x => x.value !== null && x.value !== undefined && !Array.isArray(x.value))
      .map(x => {
        const fn = FMT[fmtByLabel[x.seriesName] || 'plain'] || FMT.plain;
        return '<div>' + x.marker + window.esc(x.seriesName) + ': ' + fn(x.value) + '</div>';
      }).join('');
    return head + (rows ? '<div style="margin-top:4px">' + rows + '</div>' : '');
  }

  /* ---------------- 主图：K 线格 +（可选的）指标格 ---------------- */
  function tradeReviewOption(res, indKey, zoom) {
    const bars = (res && res.bars) || [];
    if (!bars.length) return null;
    const mk = (res && res.marks) || {};
    const tr = (res && res.trade) || {};
    const d = bars.map(b => b.trade_date);
    const group = shownGroup(res, indKey);

    /* --- K 线格的买卖点 --- */
    const markPt = [];
    if (mk.buy) markPt.push({
      name: '买入', coord: [mk.buy.date, mk.buy.price],
      symbol: 'triangle', symbolSize: 15, symbolOffset: [0, 22],
      itemStyle: { color: C_BUY },
      label: { show: true, formatter: 'B', color: '#fff', fontSize: 10, fontWeight: 'bold' }
    });
    if (mk.sell) markPt.push({
      name: '卖出', coord: [mk.sell.date, mk.sell.price],
      symbol: 'triangle', symbolRotate: 180, symbolSize: 15, symbolOffset: [0, -22],
      itemStyle: { color: C_SELL },
      label: { show: true, formatter: 'S', color: '#fff', fontSize: 10, fontWeight: 'bold' }
    });

    const markLn = [];
    if (mk.buy) markLn.push({
      yAxis: mk.buy.price, silent: false,
      lineStyle: { color: C_BUY, type: 'dashed', width: 1, opacity: 0.55 },
      label: { show: true, position: 'insideStartTop', color: C_BUY, fontSize: 10,
               formatter: '买入 ' + money(mk.buy.price) }
    });
    if (mk.sell) markLn.push({
      yAxis: mk.sell.price, silent: false,
      lineStyle: { color: C_SELL, type: 'dashed', width: 1, opacity: 0.55 },
      label: { show: true, position: 'insideEndTop', color: C_SELL, fontSize: 10,
               formatter: '卖出 ' + money(mk.sell.price) }
    });

    // 持有段连线：买入价 → 卖出价的直线。
    // 用独立 line series 而不是 markLine 的两点式(coords) —— 后者在 candlestick 上
    // 会抛 "Cannot read properties of undefined (reading 'coord')"。
    const idxOf = {};
    bars.forEach((b, i) => { idxOf[b.trade_date] = i; });
    const holdData = new Array(bars.length).fill(null);
    let holdTxt = '', holdColor = C_UP;
    if (mk.buy && mk.sell) {
      const bi = idxOf[mk.buy.date], si = idxOf[mk.sell.date];
      if (bi !== undefined && si !== undefined && si > bi) {
        const n = si - bi;
        for (let k = 0; k <= n; k++) {
          holdData[bi + k] = mk.buy.price + (mk.sell.price - mk.buy.price) * (k / n);
        }
        holdColor = Number(tr.pnl) >= 0 ? C_UP : C_DOWN;
        holdTxt = '持有段 ' + shortD(mk.buy.date) + ' → ' + shortD(mk.sell.date) + '　' +
                  pct(tr.pnl_pct) + (tr.hold_days === undefined || tr.hold_days === null
                                     ? '' : ' / ' + tr.hold_days + '天');
      }
    }

    // 展示区间内该股其它触发信号的日子（跳过本笔的买卖日）
    const otherSig = (res.signals || []).filter(s =>
      !(mk.buy && s.date === mk.buy.date) && !(mk.sell && s.date === mk.sell.date));

    const grids = [{ left: 58, right: 58, top: LAY.TOP, height: LAY.KH }];
    const xAxes = [window.withBaseAxis({ type: 'category', data: d, gridIndex: 0, boundaryGap: true },
                                       { show: false })];
    const yAxes = [window.withBaseAxis({ type: 'value', scale: true, gridIndex: 0 })];
    const legends = [Object.assign({}, LEGEND_STYLE, {
      data: ['K线', 'MA5', 'MA20', 'MA60', 'BOLL上轨', 'BOLL下轨', '持有段', '其它信号日'],
      top: 2, left: 0
    })];
    const series = [
      {
        name: 'K线', type: 'candlestick', xAxisIndex: 0, yAxisIndex: 0,
        data: bars.map(b => [b.open, b.close, b.low, b.high]),
        itemStyle: { color: C_UP, color0: C_DOWN, borderColor: C_UP, borderColor0: C_DOWN },
        markPoint: { data: markPt, animation: false },
        markLine: { symbol: 'none', data: markLn, animation: false }
      },
      { name: 'MA5', type: 'line', xAxisIndex: 0, yAxisIndex: 0, data: bars.map(b => b.ma5), showSymbol: false, lineStyle: { width: 1, color: '#e08a00' }, itemStyle: { color: '#e08a00' } },
      { name: 'MA20', type: 'line', xAxisIndex: 0, yAxisIndex: 0, data: bars.map(b => b.ma20), showSymbol: false, lineStyle: { width: 1, color: '#4b83ff' }, itemStyle: { color: '#4b83ff' } },
      { name: 'MA60', type: 'line', xAxisIndex: 0, yAxisIndex: 0, data: bars.map(b => b.ma60), showSymbol: false, lineStyle: { width: 1, color: '#8b5cf6' }, itemStyle: { color: '#8b5cf6' } },
      { name: 'BOLL上轨', type: 'line', xAxisIndex: 0, yAxisIndex: 0, data: bars.map(b => b.boll_upper), showSymbol: false, lineStyle: { width: 1, color: '#b0b9cc', type: 'dashed' }, itemStyle: { color: '#b0b9cc' } },
      { name: 'BOLL下轨', type: 'line', xAxisIndex: 0, yAxisIndex: 0, data: bars.map(b => b.boll_lower), showSymbol: false, lineStyle: { width: 1.4, color: C_DOWN }, itemStyle: { color: C_DOWN } },
      {
        name: '持有段', type: 'line', xAxisIndex: 0, yAxisIndex: 0,
        data: holdData, symbol: 'none', z: 3,
        lineStyle: { width: 1.8, color: holdColor, opacity: 0.85 },
        itemStyle: { color: holdColor }
      },
      {
        name: '其它信号日', type: 'scatter', xAxisIndex: 0, yAxisIndex: 0, symbolSize: 6, z: 1,
        itemStyle: { color: 'rgba(75,131,255,.55)' },
        data: otherSig.map(s => ({ value: [s.date, bars[s.i] ? bars[s.i].low : null] }))
      }
    ];

    /* 持有段信息条：画在 K 线格**上方**（grid 之外），不压 K 线。位置固定，
       内容给日期区间 —— 图内那段连线仍然是它指认的对象。 */
    const graphic = [];
    if (holdTxt) {
      const w = Math.round(textW(holdTxt, 11)) + 18;
      graphic.push({
        type: 'group', left: 58, top: LAY.TAG_TOP,
        children: [
          { type: 'rect', shape: { x: 0, y: 0, width: w, height: LAY.TAG_H, r: 4 },
            style: { fill: 'rgba(21,26,34,.92)', stroke: holdColor, lineWidth: 1 } },
          { type: 'text', style: { x: 9, y: 4, text: holdTxt, fill: holdColor,
            font: '600 11px ' + FONT } }
        ]
      });
    }

    /* --- 下挂指标格（可切换）--- */
    const fmtByLabel = {};
    if (group) {
      grids.push({ left: 58, right: 58, top: LAY.TOP + LAY.KH + LAY.GAP, height: LAY.IH });
      xAxes.push(window.withBaseAxis({ type: 'category', data: d, gridIndex: 1, boundaryGap: true },
                                     { show: true, hideOverlap: true }));
      const y = {
        type: 'value', scale: true, gridIndex: 1, name: group.name,
        nameTextStyle: { color: '#8b95a9', fontSize: 10, align: 'left' }, nameGap: 10
      };
      if (group.band) { y.min = group.band[0]; y.max = group.band[1]; }   // 只有真有界的指标才夹
      yAxes.push(window.withBaseAxis(y));

      const items = [];
      (group.series || []).forEach((s, si) => {
        items.push(s.label);
        fmtByLabel[s.label] = s.fmt || 'plain';
        if (s.type === 'bar') {
          // 柱子按涨跌着色：updown=数值正负，updown_open=当日 K 线阴阳（A股习惯：红涨绿跌）
          const data = bars.map(x => {
            const v = x[s.f];
            let color = NEUTRAL;
            if (v !== null && v !== undefined) {
              color = s.role === 'updown_open' ? (x.close >= x.open ? C_UP : C_DOWN)
                                               : (v >= 0 ? C_UP : C_DOWN);
            }
            return { value: v, itemStyle: { color } };
          });
          series.push({ name: s.label, type: 'bar', xAxisIndex: 1, yAxisIndex: 1,
                        barMaxWidth: 6, data });
        } else {
          series.push({ name: s.label, type: 'line', xAxisIndex: 1, yAxisIndex: 1,
                        data: bars.map(x => x[s.f]), showSymbol: false,
                        lineStyle: { width: 1.2, color: (PAL[group.key] || [])[si] || NEUTRAL } });
        }
      });
      legends.push(Object.assign({}, LEGEND_STYLE, {
        data: items, top: LAY.TOP + LAY.KH + LAY.GAP - 26, right: 10
      }));

      // 参考线用一条**空数据**的 series 承载：挂在数据 series 上会被图例一起点掉
      if (group.guides && group.guides.length) {
        series.push({ name: GUIDE_PREFIX + group.key, type: 'line', xAxisIndex: 1, yAxisIndex: 1,
                      data: [], silent: true, tooltip: { show: false },
                      markLine: { silent: true, symbol: 'none', label: { show: false },
                                  lineStyle: { color: '#3a4863', type: 'dashed', width: 1 },
                                  data: group.guides.map(v => ({ yAxis: v })) } });
      }

      // 买卖日的竖线同样用空 series 承载 —— 换指标时它不跟着指标一起消失
      const vline = [];
      if (mk.buy) vline.push({
        xAxis: mk.buy.date, lineStyle: { color: C_BUY, type: 'dashed', width: 1 },
        label: { show: true, formatter: 'B', position: 'insideEndTop', color: C_BUY, fontSize: 10 }
      });
      if (mk.sell) vline.push({
        xAxis: mk.sell.date, lineStyle: { color: C_SELL, type: 'dashed', width: 1 },
        label: { show: true, formatter: 'S', position: 'insideEndBottom', color: C_SELL, fontSize: 10 }
      });
      if (vline.length) {
        series.push({ name: BS_NAME, type: 'line', xAxisIndex: 1, yAxisIndex: 1,
                      data: [], silent: true, tooltip: { show: false },
                      markLine: { symbol: 'none', animation: false, data: vline } });
      }
    }

    /* 缩放与十字光标作用在**全部** x 轴上 —— 这一句就是「拖 K 线时间轴、下挂格同步」的本体 */
    const allX = xAxes.map((_, i) => i);
    const start = zoom && zoom[0] !== undefined ? zoom[0] : 0;
    const end = zoom && zoom[1] !== undefined ? zoom[1] : 100;

    return {
      animation: false,
      axisPointer: { link: [{ xAxisIndex: 'all' }], label: { backgroundColor: '#3a4863', fontSize: 11 } },
      tooltip: Object.assign({}, window.tooltipBase, {
        confine: true,
        formatter: function (p) {
          if (!p || !p.length) return '';
          const isK = p.some(x => x.seriesType === 'candlestick');
          return isK ? klineTip(p, bars, mk, res) : indTip(p, bars, group, fmtByLabel);
        }
      }),
      graphic: graphic,
      legend: legends,
      grid: grids,
      xAxis: xAxes,
      yAxis: yAxes,
      dataZoom: [
        { type: 'inside', xAxisIndex: allX, start: start, end: end },
        { type: 'slider', xAxisIndex: allX, height: 16, bottom: 12,
          borderColor: '#2a3444', fillerColor: 'rgba(75,131,255,.14)',
          handleStyle: { color: '#4b83ff' }, textStyle: { color: '#8b95a9', fontSize: 10 } }
      ],
      series: series
    };
  }

  /* ---------------- 渲染 ---------------- */
  function disposeChart(el) {
    if (!el || !window.echarts) return;
    const inst = window.echarts.getInstanceByDom(el);
    if (inst) { try { inst.dispose(); } catch (e) { /* noop */ } }
  }

  window.tradeReviewOption = tradeReviewOption;
  window.tradeReviewHeight = tradeReviewHeight;
  window.disposeChart = disposeChart;

  /* 画一版：高度按有没有指标格算出来写回元素（元素上写着固定高度时，多出来的一格会被压扁）。
     zoom = [start, end]，切指标时由调用方回传当前窗口 —— setOption(notMerge) 会把它重置。 */
  window.drawTradeReview = function (el, res, indKey, zoom) {
    const opt = tradeReviewOption(res, indKey, zoom);
    if (!el || !opt) return null;
    el.style.height = tradeReviewHeight(res, indKey) + 'px';
    return window.mkChart(el, opt);
  };
})();
