/* Results section: base -> WildAWM gains per benchmark, and the full sortable OOD table (paper main results). */
(function () {
  'use strict';
  var W = window.WILDAWM;
  var h = W.h;
  var esc = function (s) { return W.fmt.esc(s); };
  var TSEP = W.TSEP;
  var CAT = { Web: 'Web agents', Claw: 'Claw agents', CUA: 'Computer-use agents' };

  function sgn(v) { return (v >= 0 ? '+' : '−') + Math.abs(v).toFixed(2); }

  /* ---------------------------------------------------------------- Gains */
  function renderGains() {
    var root = document.getElementById('gains');
    var MR = W.data.main_results;
    function row(group, model) { return MR.rows.filter(function (r) { return r.group === group && r.model === model; })[0]; }
    var b35 = row('35B-A3B-Level Models', 'Qwen3.6'), o35 = row('35B-A3B-Level Models', 'WildAWM');
    var b4 = row('4B-Level Models', 'Qwen3.5'), o4 = row('4B-Level Models', 'WildAWM');
    var ref = row('Frontier Models', 'GPT-6-Astra');
    root.appendChild(W.srcComment(MR.source, 'rows Qwen3.6 / WildAWM (35B-A3B), Qwen3.5 / WildAWM (4B), GPT-6-Astra'));
    root.appendChild(h('div', { class: 'g-row g-axis', 'aria-hidden': 'true' }, h('span'),
      h('div', { class: 'g-ticks' }, [0, 25, 50, 75, 100].map(function (v) { return h('span', { style: { left: v + '%' } }, String(v)); })),
      h('div', { class: 'g-dh' }, h('span', null, '35B'), h('span', null, '4B'))));
    var lastCat = null;
    MR.benchmarks.forEach(function (b, i) {
      if (MR.categories[i] !== lastCat) { lastCat = MR.categories[i]; root.appendChild(h('p', { class: 'g-group' }, CAT[lastCat] || lastCat)); }
      var plot = h('div', { class: 'g-plot', 'aria-hidden': 'true' });
      [[b35, o35, 'l35'], [b4, o4, 'l4']].forEach(function (p) {
        var lo = Math.min(p[0].scores[i], p[1].scores[i]), hi = Math.max(p[0].scores[i], p[1].scores[i]);
        var lane = h('span', { class: 'g-lane ' + p[2] });
        lane.appendChild(h('span', { class: 'g-line', style: { left: lo + '%', width: (hi - lo) + '%' } }));
        lane.appendChild(h('span', { class: 'g-dot base', style: { left: p[0].scores[i] + '%' } }));
        lane.appendChild(h('span', { class: 'g-dot ours', style: { left: p[1].scores[i] + '%' } }));
        plot.appendChild(lane);
      });
      if (ref && ref.scores[i] !== null) plot.appendChild(h('span', { class: 'g-ref', style: { left: ref.scores[i] + '%' } }));
      var d35 = o35.scores[i] - b35.scores[i], d4 = o4.scores[i] - b4.scores[i];
      var r = h('div', { class: 'g-row', tabindex: '0', role: 'group',
        'aria-label': MR.benchmark_names[i] + ': 35B-A3B from ' + b35.raw[i] + ' to ' + o35.raw[i] + ', 4B from ' + b4.raw[i] + ' to ' + o4.raw[i] + (ref ? ', GPT-6-Astra ' + ref.raw[i] : '') },
        h('div', { class: 'g-name' }, h('b', null, b), h('span', null, MR.benchmark_names[i])),
        plot,
        h('div', { class: 'g-d' }, h('span', { class: 'num d35' }, sgn(d35)), h('span', { class: 'num d4' }, sgn(d4))));
      W.tooltip(r, function () {
        return '<b>' + esc(MR.benchmark_names[i]) + '</b>' + TSEP + esc(b) +
          '<br>35B-A3B: Qwen3.6 ' + b35.raw[i] + ' → WildAWM ' + o35.raw[i] +
          '<br>4B: Qwen3.5 ' + b4.raw[i] + ' → WildAWM ' + o4.raw[i] + (ref ? '<br>GPT-6-Astra ' + ref.raw[i] : '');
      });
      root.appendChild(r);
    });
    root.appendChild(h('div', { class: 'g-row g-avg' },
      h('div', { class: 'g-name' }, h('b', null, 'Avg.'), h('span', null, 'Eight benchmarks')),
      h('div', { class: 'g-avgtext' }, '35B-A3B ', h('span', { class: 'num' }, b35.avg_raw + ' → ' + o35.avg_raw), '   4B ', h('span', { class: 'num' }, b4.avg_raw + ' → ' + o4.avg_raw)),
      h('div', { class: 'g-d' }, h('span', { class: 'num d35' }, o35.delta), h('span', { class: 'num d4' }, o4.delta))));
  }

  /* ---------------------------------------------------------------- OOD table */
  var oodSort = null, oodHeads = [];
  function buildOODHead() {
    var MR = W.data.main_results;
    var thead = W.$('#ood-table thead');
    W.clear(thead);
    var cats = [['', 2], ['Web agent', 2], ['Claw agent', 2], ['Computer-use agent', 4], ['', 1]];
    thead.appendChild(h('tr', { class: 'cat-row' }, cats.map(function (c) { return c[0] ? h('th', { colspan: String(c[1]), scope: 'colgroup' }, c[0]) : h('td', { colspan: String(c[1]) }); })));
    var hdr = h('tr');
    hdr.appendChild(h('th', { scope: 'col' }, 'Model'));
    hdr.appendChild(h('th', { scope: 'col' }, 'Harness'));
    MR.benchmarks.concat(['Avg.']).forEach(function (b, i) {
      var arrow = h('span', { class: 'arrow', 'aria-hidden': 'true' }, '↕');
      var btn = h('button', { class: 'sort-btn', type: 'button', title: i < 8 ? MR.benchmark_names[i] : 'Average over the eight benchmarks' }, b, arrow);
      var th = h('th', { scope: 'col', class: 'num' }, btn);
      btn.addEventListener('click', function () { oodSort = oodSort === i ? null : i; updateOODHead(); renderOODBody(); });
      oodHeads.push({ th: th, arrow: arrow });
      hdr.appendChild(th);
    });
    thead.appendChild(hdr);
  }
  function updateOODHead() {
    oodHeads.forEach(function (o, i) {
      if (oodSort === i) { o.th.setAttribute('aria-sort', 'descending'); o.arrow.textContent = '▼'; }
      else { o.th.removeAttribute('aria-sort'); o.arrow.textContent = '↕'; }
    });
  }
  function renderOODBody() {
    var MR = W.data.main_results;
    var tbody = W.$('#ood-table tbody');
    W.clear(tbody);
    var rows = MR.rows.slice();
    var val = function (r, i) { return i === 8 ? r.avg : r.scores[i]; };
    if (oodSort !== null) rows.sort(function (a, b) {
      var x = val(a, oodSort), y = val(b, oodSort);
      if (x === null && y === null) return 0; if (x === null) return 1; if (y === null) return -1; return y - x;
    });
    var lastGroup = null;
    rows.forEach(function (r) {
      if (oodSort === null && r.group !== lastGroup) {
        tbody.appendChild(h('tr', { class: 'group-sep' }, h('td', { colspan: '11' }, r.group)));
        lastGroup = r.group;
      }
      var tr = h('tr', { class: r.ours ? 'ours' : '' },
        h('td', { class: 'm' }, r.model + (r.ours ? ' ' + r.group.replace(' Models', '').replace('-Level', '') : ''), W.srcComment(MR.source, 'row ' + r.group + ' / ' + r.model)),
        h('td', { class: 'hn' }, r.harness));
      for (var i = 0; i < 9; i++) {
        var raw = i === 8 ? r.avg_raw : r.raw[i];
        var td = h('td', { class: 'num' + (r.bold[i] ? ' best' : '') }, raw === null ? '–' : raw);
        if (i === 8 && r.delta) td.appendChild(h('span', { class: 'delta' }, r.delta));
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    });
  }

  W.modules.push({
    name: 'the results',
    root: '#gains',
    init: function () {
      renderGains();
      buildOODHead(); updateOODHead(); renderOODBody();
      var gains = document.getElementById('gains'), wrap = document.getElementById('ood-wrap'), legend = document.getElementById('gains-legend');
      W.seg(W.$('#results .seg'), function (b) {
        var table = b.getAttribute('data-rview') === 'table';
        gains.hidden = table;
        wrap.hidden = !table;
        legend.classList.toggle('is-off', table);
      });
    }
  });
})();
