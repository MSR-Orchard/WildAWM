/* Dataset section: persona column chart, val33 charts (linked filters), sortable/filterable task table. */
(function () {
  'use strict';
  var W = window.WILDAWM;
  var h = W.h;

  var SITE_NAMES = { amazon: 'Amazon', booking: 'Booking', google_map: 'Google Maps', allrecipes: 'AllRecipes', apple: 'Apple', huggingface: 'Hugging Face' };
  var IFACE = { web: 'GUI', mcp: 'MCP', cli: 'CLI' };
  var DIFF_ORDER = { hard: 0, medium: 1 };

  var LIMIT = 8;
  var state = { sort: 'id', dir: 1, site: '', domain: '', diff: '', q: '', hops: '', open: {}, all: false };
  var tasks = [], agg = null, tbody = null, linked = [];

  function siteName(s) { return SITE_NAMES[s] || s; }
  var TOML = 'repo:wildawm-harbor/dataset/val/<task>/task.toml';
  function taskToml(id) { return 'repo:wildawm-harbor/dataset/val/' + id + '/task.toml'; }
  var TSEP = W.TSEP;
  function esc(x) { return W.fmt.esc(x); }

  /* ---------------------------------------------------------------- Persona column chart */
  function renderPersonas(metric) {
    var root = document.getElementById('persona-chart');
    var src = W.data.personas;
    var rows = src.rows.slice();
    var val = function (r) { return metric === 'states' ? r.states : r.hard + r.medium; };
    rows.sort(function (a, b) { return val(b) - val(a) || a.name.localeCompare(b.name); });
    var max = Math.ceil(Math.max.apply(null, rows.map(val)) / 10) * 10;
    W.clear(root);
    root.appendChild(W.srcComment(src.source, 'Total & & 640 & 273 & 68'));

    var grid = h('div', { class: 'pc-grid', 'aria-hidden': 'true' });
    for (var t = 0; t <= max; t += 10) grid.appendChild(h('span', { style: { bottom: (t / max * 100) + '%' } }, h('i', null, String(t))));
    var cols = h('div', { class: 'pc-cols', role: 'group', 'aria-label': (metric === 'states' ? 'World states' : 'Accepted tasks') + ' per persona, sorted' });
    rows.forEach(function (r) {
      var b = h('button', { class: 'pc-col', type: 'button',
        'aria-label': r.name + ', ' + r.occupation + ': ' + r.hard + ' hard and ' + r.medium + ' medium tasks, ' + r.states + ' world states' });
      if (metric === 'states') b.appendChild(h('span', { class: 'pc-states', style: { height: (r.states / max * 100) + '%' } }));
      else {
        b.appendChild(h('span', { class: 'pc-hard', style: { height: (r.hard / max * 100) + '%' } }));
        b.appendChild(h('span', { class: 'pc-med', style: { height: (r.medium / max * 100) + '%' } }));
      }
      W.cite(b, src.source, 'row ' + r.name + ': States ' + r.states + ', Hard ' + r.hard + ', Medium ' + r.medium);
      W.tooltip(b, function () {
        return '<b>' + esc(r.name) + '</b><br>' + esc(r.occupation) + '<br>' + r.hard + ' hard' + TSEP + r.medium + ' medium' + TSEP + r.states + ' world states';
      });
      cols.appendChild(b);
    });
    root.appendChild(h('div', { class: 'pc-plot' }, grid, cols));
    var legend = metric === 'states'
      ? [h('li', null, h('span', { class: 'sw sw-states' }), 'World states')]
      : [h('li', null, h('span', { class: 'sw sw-hard' }), 'Hard'), h('li', null, h('span', { class: 'sw sw-med' }), 'Medium')];
    root.appendChild(h('div', { class: 'pc-foot' },
      h('span', null, '36 personas, sorted. Hover or tap a column for the persona.'),
      h('ul', { class: 'legend' }, legend)));
  }

  /* ---------------------------------------------------------------- Val33 charts */
  function barList(container, counts, key, label) {
    W.clear(container);
    container.classList.remove('loading');
    container.appendChild(W.srcComment(agg.source));
    var keys = Object.keys(counts);
    var max = Math.max.apply(null, keys.map(function (k) { return counts[k]; }));
    var ul = h('ul', { class: 'bars', 'aria-label': label });
    keys.forEach(function (k) {
      var name = key === 'site' ? siteName(k) : k.replace(/_/g, ' ');
      var b = h('button', { class: 'bar-row', type: 'button', 'aria-pressed': 'false', 'data-filter': key, 'data-value': k,
        'aria-label': label + ': ' + name + ', ' + counts[k] + ' tasks. Select to filter the table.' },
        h('span', { class: 'bar-label' }, name),
        h('span', { class: 'bar-track', 'aria-hidden': 'true' }, h('span', { class: 'bar-fill', style: { width: (counts[k] / max * 100) + '%' } })),
        h('span', { class: 'bar-val num' }, String(counts[k])));
      b.addEventListener('click', function () { toggleFilter(key, k); });
      linked.push(b);
      ul.appendChild(W.cite(h('li', null, b), TOML, '[metadata] ' + (key === 'site' ? 'sites' : 'mcp_domains') + ' contains "' + k + '": ' + counts[k] + ' tasks'));
    });
    container.appendChild(ul);
  }

  function renderDifficulty(container) {
    W.clear(container);
    container.classList.remove('loading');
    container.appendChild(W.srcComment(agg.source));
    var c = agg.difficulty, total = agg.n;
    var ul = h('ul', { class: 'bars', 'aria-label': 'Tasks per difficulty' });
    ['hard', 'medium'].forEach(function (k) {
      var b = h('button', { class: 'bar-row', type: 'button', 'aria-pressed': 'false', 'data-filter': 'diff', 'data-value': k,
        'aria-label': k + ': ' + c[k] + ' of ' + total + ' tasks. Select to filter the table.' },
        h('span', { class: 'bar-label' }, k === 'hard' ? 'Hard' : 'Medium'),
        h('span', { class: 'bar-track', 'aria-hidden': 'true' }, h('span', { class: 'bar-fill ' + (k === 'hard' ? 'f-hard' : 'f-med'), style: { width: (c[k] / total * 100) + '%' } })),
        h('span', { class: 'bar-val num' }, String(c[k])));
      b.addEventListener('click', function () { toggleFilter('diff', k); });
      linked.push(b);
      ul.appendChild(W.cite(h('li', null, b), TOML, '[metadata] difficulty = "' + k + '": ' + c[k] + ' tasks'));
    });
    container.appendChild(ul);
    var start = agg.start;
    container.appendChild(W.cite(h('p', { class: 'vchart-note' },
      'First interface: ' + Object.keys(start).map(function (k) { return (IFACE[k] || k) + ' ' + start[k]; }).join(', ')), TOML, '[metadata] starting_interface'));
  }

  function renderHops(container) {
    W.clear(container);
    container.classList.remove('loading');
    container.appendChild(W.srcComment(agg.source));
    var c = agg.hops, keys = Object.keys(c);
    var max = Math.max.apply(null, keys.map(function (k) { return c[k]; }));
    var ul = h('ul', { class: 'bars', 'aria-label': 'Tasks per number of interface hops' });
    keys.forEach(function (k) {
      var b = h('button', { class: 'bar-row', type: 'button', 'aria-pressed': 'false', 'data-filter': 'hops', 'data-value': k,
        'aria-label': k + ' hops: ' + c[k] + ' tasks. Select to filter the table.' },
        h('span', { class: 'bar-label' }, k + ' hops'),
        h('span', { class: 'bar-track', 'aria-hidden': 'true' }, h('span', { class: 'bar-fill', style: { width: (c[k] / max * 100) + '%' } })),
        h('span', { class: 'bar-val num' }, String(c[k])));
      b.addEventListener('click', function () { toggleFilter('hops', k); });
      linked.push(b);
      ul.appendChild(W.cite(h('li', null, b), TOML, '[metadata] hops = ' + k + ': ' + c[k] + ' tasks'));
    });
    container.appendChild(ul);
  }

  /* ---------------------------------------------------------------- Filters */
  function toggleFilter(key, value) {
    state[key] = state[key] === value ? '' : value;
    syncControls();
    renderTable();
  }
  function syncControls() {
    W.$('#f-site').value = state.site;
    W.$('#f-domain').value = state.domain;
    W.$('#f-diff').value = state.diff;
    W.$('#f-search').value = state.q;
    linked.forEach(function (b) {
      var k = b.getAttribute('data-filter'), v = b.getAttribute('data-value');
      b.setAttribute('aria-pressed', state[k] === v ? 'true' : 'false');
    });
  }
  function matches(t) {
    if (state.site && t.sites.indexOf(state.site) < 0) return false;
    if (state.domain && t.domains.indexOf(state.domain) < 0) return false;
    if (state.diff && t.difficulty !== state.diff) return false;
    if (state.hops && String(t.hops) !== state.hops) return false;
    if (state.q) {
      var q = state.q.toLowerCase();
      var hay = [t.id, t.area, t.archetype, t.desc, t.persona, t.sites.map(siteName).join(' '), t.domains.join(' ')].join(' ').toLowerCase();
      if (hay.indexOf(q) < 0) return false;
    }
    return true;
  }
  function sortVal(t, key) {
    if (key === 'sites') return siteName(t.sites[0]);
    if (key === 'domains') return t.domains.join(',');
    if (key === 'difficulty') return DIFF_ORDER[t.difficulty];
    return t[key];
  }

  /* ---------------------------------------------------------------- Table */
  function ifaceTag(i) { return h('span', { class: 'if if-' + (i === 'web' ? 'gui' : i) }, IFACE[i] || i); }

  function detailRow(t) {
    var td = h('td', { colspan: '5' });
    var tr = h('tr', { class: 'detail-row', id: 'task-detail-' + t.id }, td);
    var tt = taskToml(t.id);
    var flow = h('ol', { class: 'legs' });
    t.flow.forEach(function (leg, k) { flow.appendChild(W.cite(h('li', null, ifaceTag(leg.i), h('span', null, leg.t)), tt, '[metadata] interface_flow, leg ' + (k + 1))); });
    var pre = h('pre', { class: 'instr' }, 'Loading instruction…');
    td.appendChild(h('div', { class: 'detail' },
      h('div', { class: 'detail-flow' },
        h('h4', null, 'Interface flow'),
        flow,
        W.cite(h('p', { class: 'detail-meta' }, 'Persona ', h('code', null, t.persona), ', day ' + t.day + ', clock frozen at ' + t.clock + '.'), tt, '[metadata] persona, day, frozen_clock_date')),
      h('div', { class: 'detail-instr' },
        h('h4', null, 'Instruction given to the agent'),
        pre,
        W.srcComment('repo:wildawm-harbor/dataset/val/' + t.id + '/instruction.md'))));
    W.loadScript('assets/data/instructions.js').then(function () {
      pre.textContent = (window.WILDAWM_INSTRUCTIONS || {})[t.id] || 'Instruction not found.';
      pre.dataset.label = 'Instruction for ' + t.id;
      W.focusIfScrollable(pre.parentNode);
    }, function (e) { pre.textContent = e.message; });
    return tr;
  }

  function renderTable() {
    var rows = tasks.filter(matches);
    var key = state.sort, dir = state.dir;
    rows.sort(function (a, b) {
      var x = sortVal(a, key), y = sortVal(b, key);
      if (x < y) return -dir; if (x > y) return dir;
      return a.id < b.id ? -1 : 1;
    });
    W.clear(tbody);
    if (!rows.length) tbody.appendChild(h('tr', null, h('td', { colspan: '5', class: 'empty' }, 'No task matches these filters. Reset to see all 33.')));
    var shown = state.all ? rows : rows.slice(0, LIMIT);
    shown.forEach(function (t) {
      var open = !!state.open[t.id];
      var toggle = h('button', { class: 'row-toggle', type: 'button', 'aria-expanded': open ? 'true' : 'false', 'aria-controls': open ? 'task-detail-' + t.id : null },
        h('span', { class: 'caret', 'aria-hidden': 'true' }),
        h('span', { class: 'row-main' }, h('span', { class: 'row-desc' }, t.desc), h('span', { class: 'task-id' }, t.id)));
      toggle.addEventListener('click', function () {
        state.open[t.id] = !state.open[t.id];
        renderTable();
        var btn = W.$('#task-row-' + CSS.escape(t.id) + ' .row-toggle');
        if (btn) btn.focus();
      });
      var tt = taskToml(t.id);
      var tr = h('tr', { id: 'task-row-' + t.id, class: open ? 'is-open' : '' },
        W.cite(h('td', { class: 'c-task' }, toggle), tt, '[task] name, description'),
        W.cite(h('td', null, t.sites.map(function (s) { return h('span', { class: 'chip' }, siteName(s)); })), tt, '[metadata] sites'),
        W.cite(h('td', null, t.domains.map(function (d) { return h('span', { class: 'chip' }, d.replace(/_/g, ' ')); })), tt, '[metadata] mcp_domains'),
        W.cite(h('td', { class: 'num c-hops' }, String(t.hops)), tt, '[metadata] hops'),
        W.cite(h('td', { class: 'c-diff' }, h('span', { class: 'diff diff-' + t.difficulty }, t.difficulty)), tt, '[metadata] difficulty'));
      tbody.appendChild(tr);
      if (open) tbody.appendChild(detailRow(t));
    });
    if (rows.length > LIMIT) {
      var more = h('button', { class: 'btn-text', type: 'button' }, state.all ? 'Show fewer' : 'Show all ' + rows.length + ' tasks');
      more.addEventListener('click', function () {
        state.all = !state.all;
        renderTable();
        if (!state.all) W.$('#task-table').scrollIntoView({ block: 'nearest' });
      });
      tbody.appendChild(h('tr', { class: 'more' }, h('td', { colspan: '5' }, more)));
    }
    W.$('#task-count').textContent = rows.length === tasks.length ? tasks.length + ' tasks' : rows.length + ' of ' + tasks.length + ' tasks';
    W.$$('#task-table thead th[data-key]').forEach(function (th) {
      var k = th.getAttribute('data-key');
      var arrow = W.$('.arrow', th);
      if (k === state.sort) { th.setAttribute('aria-sort', state.dir > 0 ? 'ascending' : 'descending'); arrow.textContent = state.dir > 0 ? '▲' : '▼'; }
      else { th.removeAttribute('aria-sort'); arrow.textContent = '↕'; }
    });
  }

  function initTable() {
    tbody = W.$('#task-table tbody');
    var site = W.$('#f-site'), dom = W.$('#f-domain'), diff = W.$('#f-diff'), q = W.$('#f-search');
    Object.keys(agg.sites).forEach(function (k) { site.appendChild(h('option', { value: k }, siteName(k) + ' (' + agg.sites[k] + ')')); });
    Object.keys(agg.domains).forEach(function (k) { dom.appendChild(h('option', { value: k }, k.replace(/_/g, ' ') + ' (' + agg.domains[k] + ')')); });
    Object.keys(agg.difficulty).forEach(function (k) { diff.appendChild(h('option', { value: k }, k + ' (' + agg.difficulty[k] + ')')); });
    site.addEventListener('change', function () { state.site = site.value; syncControls(); renderTable(); });
    dom.addEventListener('change', function () { state.domain = dom.value; syncControls(); renderTable(); });
    diff.addEventListener('change', function () { state.diff = diff.value; syncControls(); renderTable(); });
    var timer = null;
    q.addEventListener('input', function () { clearTimeout(timer); timer = setTimeout(function () { state.q = q.value.trim(); syncControls(); renderTable(); }, 120); });
    W.$('#f-reset').addEventListener('click', function () { state.site = state.domain = state.diff = state.q = state.hops = ''; syncControls(); renderTable(); });
    W.$$('#task-table thead th[data-key]').forEach(function (th) {
      W.$('button', th).addEventListener('click', function () {
        var k = th.getAttribute('data-key');
        if (state.sort === k) state.dir = -state.dir; else { state.sort = k; state.dir = 1; }
        renderTable();
      });
    });
    renderTable();
  }

  W.modules.push({
    name: 'the dataset',
    root: '#dataset',
    init: function () {
      tasks = W.data.dataset.tasks;
      agg = W.data.dataset.agg;
      renderPersonas('tasks');
      W.seg(W.$('#dataset .seg'), function (b) { renderPersonas(b.getAttribute('data-pmetric')); });
      renderDifficulty(document.getElementById('chart-difficulty'));
      barList(document.getElementById('chart-sites'), agg.sites, 'site', 'Tasks per website');
      barList(document.getElementById('chart-domains'), agg.domains, 'domain', 'Tasks per MCP domain');
      renderHops(document.getElementById('chart-hops'));
      initTable();
    }
  });
})();
