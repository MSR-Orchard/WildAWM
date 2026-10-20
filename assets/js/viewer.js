/* Trajectory viewer (styled by viewer.css): case tabs, progress rail with key-moment marks, prompt-token sparkline, player
   controls, family filter, and a collapsible step timeline. Case data loads on demand from
   assets/data/traj/<case>.js; step bodies render on first expansion. */
(function () {
  'use strict';
  var W = window.WILDAWM;
  var h = W.h;
  var esc = function (s) { return W.fmt.esc(s); };

  var FAM = {
    web: { b: 'GUI', name: 'Browser', long: 'Browser (Playwright MCP, browser_*)' },
    vision: { b: 'VIS', name: 'Vision', long: 'Vision helper (vision_describe_image)' },
    cli: { b: 'CLI', name: 'Shell/files', long: 'Shell and file tools (bash, read, write, edit, grep, glob, apply_patch)' },
    mcp: { b: 'MCP', name: 'MCP', long: 'MCP domain tools (calendar, email, direct_messages, …)' },
    harness: { b: 'AGT', name: 'Harness', long: 'Harness built-ins (todowrite)' }
  };
  var FAM_ORDER = ['web', 'vision', 'cli', 'mcp', 'harness'];
  var KIND = { user: 'Task', compaction: 'Compaction', message: 'Final message' };
  var PLAY_MS = 1100;
  var MARK_W = 20;   // key-step marker hit width in px
  var MARK_GAP = 24; // minimum distance between marker centres (leaves >= 4 px between hit boxes)

  // Short task titles, paraphrasing [metadata] interface_flow in repo:wildawm-harbor/dataset/val/<task>/task.toml.
  var TITLES = {
    'qwen-16bfe8-d8-h1': ['Race-day watch', 'Re-rank GPS watches, add the visually matched one to an Amazon cart, log it, and message a partner.'],
    'glm-844f1c-d19-h1': ['Shanghai trip prep', 'Parse the itinerary, check the calendar, save the Uniqlo store whose photo matches on Google Maps, and email Lily.'],
    'gpt-4d03aa-d9-h2': ['Laptop upgrade', 'Audit compute needs, visually match the requested variant on apple.com, write it up, and add it to the cart.']
  };
  function titleOf(id, fallback) { return (TITLES[id] || [fallback])[0]; }
  function blurbOf(id) { return (TITLES[id] || [null, ''])[1]; }
  // Case order and model names as the authors present them (2026-10-19); the GLM job metadata records glm/GLM-5.3-NVFP4.
  var ORDER = ['glm-844f1c-d19-h1', 'gpt-4d03aa-d9-h2', 'qwen-16bfe8-d8-h1'];
  var MODEL = { 'glm-844f1c-d19-h1': 'GLM-5.2-NVFP4', 'gpt-4d03aa-d9-h2': 'GPT-5.6-Sol', 'qwen-16bfe8-d8-h1': 'Qwen3.6-35B-A3B' };
  function modelOf(id, fallback) { return MODEL[id] || fallback; }
  // Total output tokens: completion tokens plus reasoning tokens, which opencode's ATIF trajectories count separately.
  function outputTokens(d) {
    var reasoning = d.steps.reduce(function (t, s) { return t + (s.tok && s.tok[2] ? s.tok[2] : 0); }, 0);
    var completion = d.tokens && d.tokens.total_completion_tokens;
    return completion === undefined || completion === null ? null : completion + reasoning;
  }
  function kTokens(n) { return n >= 1e6 ? (n / 1e6).toFixed(2) + 'M' : (n / 1e3).toFixed(1) + 'K'; }

  var root, tabsEl, panel;
  var cases = [];
  var S = null; // state of the active case

  /* ---------------------------------------------------------------- helpers */
  function stepClass(s) {
    if (s.kind !== 'action') return s.kind;
    var cnt = {}, best = null;
    (s.c || []).forEach(function (c) { cnt[c.f] = (cnt[c.f] || 0) + 1; if (best === null || cnt[c.f] > cnt[best]) best = c.f; });
    return best || 'message';
  }
  function chip(c, count) {
    var el = h('span', { class: 'tc f-' + c.f + (c.e ? ' is-err' : ''), title: c.n + (c.e ? ' (returned an error)' : '') },
      h('span', { class: 'b' }, FAM[c.f].b),
      h('span', { class: 'n' }, c.l.split(' · ').reduce(function (acc, part, k) { return k ? acc.concat(msep(), part) : acc.concat(part); }, [])));
    if (count > 1) el.appendChild(h('span', { class: 'tc-count' }, '×' + count));
    return el;
  }
  function groupedChips(calls) {
    var out = [], i = 0;
    while (i < calls.length) {
      var j = i, err = false;
      while (j < calls.length && calls[j].n === calls[i].n) { err = err || !!calls[j].e; j++; }
      var c = Object.assign({}, calls[i]); if (err) c.e = 1;
      out.push(chip(c, j - i));
      i = j;
    }
    return out;
  }
  function firstLine(s) {
    if (!s) return '';
    var t = s.replace(/\s+/g, ' ').trim();
    return t.length > 220 ? t.slice(0, 220) + '…' : t;
  }
  function stepMatches(s) {
    if (S.filter === 'all') return true;
    return (s.c || []).some(function (c) { return c.f === S.filter; });
  }

  /* ---------------------------------------------------------------- skeleton */
  function buildTabs() {
    tabsEl = h('div', { class: 'tv-cases', role: 'tablist', 'aria-label': 'Trajectory cases' });
    cases.forEach(function (c, i) {
      var t = h('button', { class: 'tv-tab', role: 'tab', id: 'tv-tab-' + c.id, 'aria-controls': 'tv-panel', 'aria-selected': i === 0 ? 'true' : 'false', tabindex: i === 0 ? '0' : '-1' },
        h('span', { class: 't' }, titleOf(c.id, c.title)),
        h('span', { class: 's' },
          h('span', null, modelOf(c.id, c.tab)),
          W.cite(h('span', null, c.n_steps + ' steps'), 'repo:wildawm-harbor/' + c.job + '/' + c.trial + '/agent/trajectory.json', 'len(steps)'),
          W.cite(h('span', { class: 'ok' }, 'reward ' + c.reward), 'repo:wildawm-harbor/' + c.job + '/' + c.trial + '/result.json', 'verifier_result.rewards.reward')));
      tabsEl.appendChild(t);
    });
    panel = h('div', { id: 'tv-panel', role: 'tabpanel', 'aria-labelledby': 'tv-tab-' + cases[0].id });
    W.clear(root);
    root.appendChild(tabsEl);
    root.appendChild(panel);
    W.tabs(tabsEl, function (tab, i) {
      panel.setAttribute('aria-labelledby', tab.id);
      loadCase(cases[i]);
    });
  }

  /* Each selection gets a request number; success and error callbacks of an older selection are ignored,
     so a slow response cannot replace the panel of the case that is selected now. */
  var requestSeq = 0, activeCaseId = null;
  function loadCase(c) {
    var req = ++requestSeq;
    activeCaseId = c.id;
    stopPlay();
    if (S && S.ro) S.ro.disconnect();
    S = null;
    W.clear(panel);
    panel.setAttribute('aria-busy', 'true');
    panel.appendChild(h('p', { class: 'loading' }, 'Loading the ' + modelOf(c.id, c.tab) + ' trajectory…'));
    function current() { return req === requestSeq && activeCaseId === c.id; }
    W.loadScript(c.file).then(function () {
      if (!current()) return;
      var data = (window.WILDAWM_TRAJ || {})[c.id];
      if (!data) throw new Error('Case data missing for ' + c.id);
      renderCase(data);
      panel.setAttribute('aria-busy', 'false');
    }).catch(function (e) {
      if (!current()) return;
      panel.setAttribute('aria-busy', 'false');
      W.fail(panel, 'this trajectory', e);
    });
  }

  /* ---------------------------------------------------------------- header */
  var msep = W.msep, TSEP = W.TSEP;

  function header(d) {
    var P = 'repo:wildawm-harbor/' + d.job + '/' + d.trial + '/';
    var card = h('div', { class: 'card tv-head' });
    var checksPassed = d.checks.filter(function (x) { return x.pass; }).length;
    card.appendChild(W.srcComment(d.source));
    card.appendChild(h('div', { class: 'tv-head-top' },
      h('div', null,
        h('h3', null, titleOf(d.id, d.title)),
        h('p', { class: 'tv-blurb' }, blurbOf(d.id)),
        W.cite(h('p', { class: 'tv-meta' }, h('code', null, d.task), ' ', msep(), ' ', modelOf(d.id, d.model.replace(/^[^/]+\//, '')), ' ', msep(), ' ', d.harness), P + 'result.json', 'task_name, agent model, agent version')),
      h('span', { class: 'tv-reward', html: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3 3 7-7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>' },
        'Reward ' + d.reward, ' ', msep(), ' ', checksPassed + '/' + d.checks.length + ' checks', W.srcComment(P + 'result.json; ' + P + 'verifier/l1_verdict.json', 'verifier_result.rewards.reward; scripts[].checks'))));
    var tok = d.tokens || {};
    var stats = [
      ['Steps', W.fmt.int(d.n_steps), P + 'agent/trajectory.json', 'len(steps)'],
      ['Tool calls', W.fmt.int(d.n_calls), P + 'agent/trajectory.json', 'sum of len(steps[].tool_calls)'],
      ['Output tokens (total)', outputTokens(d) === null ? 'n/r' : kTokens(outputTokens(d)), P + 'agent/trajectory.json', 'final_metrics.total_completion_tokens + sum(steps[].metrics.extra.reasoning_tokens)'],
      ['Prompt tokens', tok.total_prompt_tokens ? (tok.total_prompt_tokens / 1e6).toFixed(2) + 'M' : 'n/r', P + 'agent/trajectory.json', 'final_metrics.total_prompt_tokens']
    ];
    var dl = h('dl', { class: 'tv-stats' });
    stats.forEach(function (s) { dl.appendChild(W.cite(h('div', null, h('dt', null, s[0]), h('dd', null, s[1])), s[2], s[3])); });
    card.appendChild(dl);
    var fams = h('ul', { class: 'tv-fams', 'aria-label': 'Tool calls by family' });
    FAM_ORDER.forEach(function (f) {
      if (!d.families[f]) return;
      var li = W.cite(h('li', null, h('span', { class: 'tc f-' + f, title: FAM[f].long }, h('span', { class: 'b' }, FAM[f].b), h('span', { class: 'n' }, FAM[f].name), h('span', { class: 'tc-count' }, String(d.families[f])))), P + 'agent/trajectory.json', 'steps[].tool_calls[].function_name, family ' + f);
      fams.appendChild(li);
    });
    card.appendChild(fams);
    var instr = h('details', { class: 'tv-details' }, h('summary', null, 'Task instruction (exact, as given to the agent)'),
      h('pre', { class: 'instr', 'data-label': 'Task instruction' }, d.steps[0].msg));
    card.appendChild(instr);
    instr.addEventListener('toggle', function () { if (instr.open) W.focusIfScrollable(instr); });
    var cl = h('ul', { class: 'tv-checks' });
    d.checks.forEach(function (x) {
      cl.appendChild(W.cite(h('li', null, h('span', { class: x.pass ? 'ok' : '', 'aria-label': x.pass ? 'passed' : 'failed' }, x.pass ? '✓' : '✕'),
        h('span', null, h('code', null, x.script + ':' + x.check), h('span', { class: 'd' }, x.detail))), P + 'verifier/l1_verdict.json', 'scripts[name=' + x.script + '].checks.' + x.check));
    });
    card.appendChild(h('details', { class: 'tv-details' }, h('summary', null, 'Verifier checks (l1_verdict.json)'), cl));
    return card;
  }

  /* ---------------------------------------------------------------- rail + sparkline + controls */
  function railBlock(d) {
    var N = d.steps.length;
    var wrap = h('div', { class: 'card tv-rail-wrap' });
    wrap.appendChild(h('p', { class: 'tv-rail-label', 'aria-hidden': 'true' }, h('span', null, 'Steps, colored by dominant tool family'), h('span', null, N + ' steps')));
    var rail = h('div', { class: 'tv-rail' + (N > 90 ? ' is-dense' : ''), role: 'slider', tabindex: '0', 'aria-label': 'Step position', 'aria-valuemin': '1', 'aria-valuemax': String(N) });
    S.cells = d.steps.map(function (s) {
      var c = h('span', { class: 'tv-cell k-' + stepClass(s) });
      rail.appendChild(c);
      return c;
    });
    S.cursor = h('span', { class: 'tv-cursor', 'aria-hidden': 'true' });
    rail.appendChild(S.cursor);
    wrap.appendChild(rail);
    S.rail = rail;

    // Key-moment marks. Markers whose centres are closer than MARK_GAP pixels are merged into one group button, so hit
    // areas never overlap and every key step stays reachable. A group button is a disclosure button (aria-expanded,
    // aria-controls) for a list of its steps; the list follows the button in DOM order, so Tab moves into it.
    // Layout reruns when the rail resizes; when the clustering is unchanged, the existing buttons are only moved,
    // so keyboard focus and an open list are kept.
    var marks = h('div', { class: 'tv-marks', role: 'group', 'aria-label': 'Key steps on the rail' });
    S.marks = marks;
    S.markEls = {};
    S.markKey = null;
    wrap.appendChild(marks);
    function clusterMarks(width) {
      var half = MARK_W / 2;
      var clamp = function (x) { return Math.max(half, Math.min(width - half, x)); };
      var clusters = d.moments.map(function (m) {
        var idx = S.index[m.i];
        return { members: [{ m: m, idx: idx }], x: clamp((idx + 0.5) / N * width) };
      }).sort(function (a, b) { return a.x - b.x; });
      var merged = true;
      while (merged) {
        merged = false;
        for (var k = 1; k < clusters.length; k++) {
          if (clusters[k].x - clusters[k - 1].x < MARK_GAP) {
            var A = clusters[k - 1];
            A.members = A.members.concat(clusters[k].members);
            A.x = clamp(A.members.reduce(function (t, p) { return t + (p.idx + 0.5) / N * width; }, 0) / A.members.length);
            clusters.splice(k, 1);
            merged = true;
            break;
          }
        }
      }
      return clusters;
    }
    function placePop(pop, x) {
      var left = Math.min(Math.max(0, x - 140), Math.max(0, marks.clientWidth - 280));
      pop.style.left = left + 'px';
      pop.style.setProperty('--caret', Math.max(12, Math.min(268, x - left)) + 'px');
    }
    function layoutMarks() {
      if (!S || S.marks !== marks) return;
      var width = rail.clientWidth || 1;
      var clusters = clusterMarks(width);
      var key = clusters.map(function (c) { return c.members.map(function (p) { return p.m.i; }).join(','); }).join('|');
      if (key === S.markKey) {
        // Same clustering: move the existing buttons (and an open list) without rebuilding them.
        var btns = marks.querySelectorAll('.tv-mark');
        clusters.forEach(function (c, k) {
          btns[k].style.left = c.x + 'px';
          if (btns[k].pop) placePop(btns[k].pop, c.x);
        });
        return;
      }
      var active = document.activeElement, focusStep = null;
      if (active && marks.contains(active)) focusStep = Number(String(active.getAttribute('data-steps') || active.getAttribute('data-step')).split(',')[0]);
      closeMarkPop();
      W.clear(marks);
      S.markEls = {};
      S.markKey = key;
      clusters.forEach(function (c) {
        var steps = c.members.map(function (p) { return p.m.i; });
        var b, pop = null;
        if (c.members.length === 1) {
          var p = c.members[0];
          b = h('button', { class: 'tv-mark', type: 'button', 'data-steps': String(p.m.i), 'aria-label': 'Key step ' + p.m.i + ': ' + p.m.text });
          b.addEventListener('click', function () { jumpTo(p.idx, { expand: true, scroll: true }); });
          W.tooltip(b, function () { return '<b>Step ' + p.m.i + '</b><br>' + esc(p.m.text); });
        } else {
          var popId = 'tv-mark-list-' + steps.join('-');
          b = h('button', { class: 'tv-mark is-group', type: 'button', 'data-steps': steps.join(','), 'aria-expanded': 'false', 'aria-controls': popId,
            'aria-label': 'Key steps ' + steps.join(', ') }, String(c.members.length));
          pop = h('div', { class: 'tv-mark-pop', id: popId, role: 'group', 'aria-label': 'Key steps ' + steps.join(', '), hidden: true });
          c.members.forEach(function (p) {
            var item = h('button', { type: 'button', 'data-step': String(p.m.i) }, h('span', { class: 'sid' }, '#' + p.m.i), h('span', null, p.m.text));
            item.addEventListener('click', function () {
              closeMarkPop();
              jumpTo(p.idx, { expand: true, scroll: true });
              S.items[p.idx].btn.focus({ preventScroll: true });
            });
            pop.appendChild(item);
          });
          pop.addEventListener('keydown', function (e) { if (e.key === 'Escape') { e.stopPropagation(); closeMarkPop(true); } });
          b.pop = pop;
          pop.owner = b;
          b.addEventListener('click', function () { if (S.markPop === pop) closeMarkPop(); else openMarkPop(pop); });
          b.addEventListener('keydown', function (e) { if (e.key === 'Escape' && S.markPop === pop) { e.stopPropagation(); closeMarkPop(true); } });
          // Close the list when keyboard focus leaves both the button and the list.
          var onFocusOut = function (e) {
            var to = e.relatedTarget;
            if (S && S.markPop === pop && to && !pop.contains(to) && to !== b) closeMarkPop();
          };
          b.addEventListener('focusout', onFocusOut);
          pop.addEventListener('focusout', onFocusOut);
          W.tooltip(b, function () { return c.members.map(function (p) { return '<b>Step ' + p.m.i + '</b> ' + esc(p.m.text); }).join('<br>'); });
          placePop(pop, c.x);
        }
        b.style.left = c.x + 'px';
        c.members.forEach(function (p) { S.markEls[p.idx] = b; });
        if (c.members.some(function (p) { return p.idx === S.cur; })) b.classList.add('is-current');
        marks.appendChild(b);
        if (pop) marks.appendChild(pop);
        if (focusStep !== null && steps.indexOf(focusStep) >= 0) b.focus({ preventScroll: true });
      });
    }
    function openMarkPop(pop) {
      closeMarkPop();
      pop.hidden = false;
      pop.owner.setAttribute('aria-expanded', 'true');
      S.markPop = pop;
      W.hideTip();
    }
    S.layoutMarks = layoutMarks;
    if ('ResizeObserver' in window) {
      var pending = false;
      S.ro = new ResizeObserver(function () {
        if (pending) return;
        pending = true;
        requestAnimationFrame(function () { pending = false; layoutMarks(); });
      });
      S.ro.observe(rail);
    } else {
      window.addEventListener('resize', layoutMarks);
    }

    // Prompt-token sparkline.
    var pts = d.steps.map(function (s) { return s.tok && s.tok[0] ? s.tok[0] : null; });
    var max = Math.max.apply(null, pts.filter(function (x) { return x !== null; }).concat([1]));
    var NS = 'http://www.w3.org/2000/svg';
    var svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('class', 'tv-spark'); svg.setAttribute('viewBox', '0 0 ' + N + ' 100'); svg.setAttribute('preserveAspectRatio', 'none');
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', 'Prompt tokens per step, maximum ' + W.fmt.int(max) + '. Dashed lines mark context compactions.');
    var line = '', area = '', started = false;
    pts.forEach(function (p, i) {
      if (p === null) return;
      var x = (i + 0.5), y = 100 - (p / max) * 92 - 4;
      line += (started ? ' L' : 'M') + x.toFixed(2) + ' ' + y.toFixed(2);
      if (!started) area = 'M' + x.toFixed(2) + ' 100 L' + x.toFixed(2) + ' ' + y.toFixed(2);
      else area += ' L' + x.toFixed(2) + ' ' + y.toFixed(2);
      started = true;
    });
    var lastX = (N - 0.5).toFixed(2);
    area += ' L' + lastX + ' 100 Z';
    var pa = document.createElementNS(NS, 'path'); pa.setAttribute('class', 'area'); pa.setAttribute('d', area); svg.appendChild(pa);
    d.steps.forEach(function (s, i) {
      if (s.kind !== 'compaction') return;
      var l = document.createElementNS(NS, 'line');
      l.setAttribute('class', 'comp'); l.setAttribute('x1', i + 0.5); l.setAttribute('x2', i + 0.5); l.setAttribute('y1', 0); l.setAttribute('y2', 100);
      svg.appendChild(l);
    });
    var pl = document.createElementNS(NS, 'path'); pl.setAttribute('class', 'line'); pl.setAttribute('d', line); svg.appendChild(pl);
    var sc = document.createElementNS(NS, 'line');
    sc.setAttribute('y1', 0); sc.setAttribute('y2', 100); sc.setAttribute('class', 'cur');
    svg.appendChild(sc);
    S.sparkCursor = sc;
    wrap.appendChild(svg);
    var lg = h('ul', { class: 'tv-legend', 'aria-label': 'Rail legend' });
    FAM_ORDER.forEach(function (f) { if (d.families[f]) lg.appendChild(h('li', null, h('span', { class: 'lg sw-' + f }), FAM[f].name)); });
    lg.appendChild(h('li', null, h('span', { class: 'lg sw-user' }), 'Task'));
    if (d.n_compaction) lg.appendChild(h('li', null, h('span', { class: 'lg sw-compaction' }), 'Compaction'));
    lg.appendChild(h('li', null, h('span', { class: 'lg sw-message' }), 'Final message'));
    lg.appendChild(h('li', null, h('span', { class: 'lg lg-mark' }), 'Key step'));
    wrap.appendChild(h('div', { class: 'tv-spark-label' }, W.cite(h('span', { 'aria-hidden': 'true' }, 'Prompt tokens per step, max ' + W.fmt.int(max) + ' (dashed line: compaction)'), S.P + 'agent/trajectory.json', 'steps[].metrics.prompt_tokens'), lg));

    // Controls.
    var prev = h('button', { class: 'tv-btn', type: 'button', 'aria-label': 'Previous step', html: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M10 3L5 8l5 5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>' });
    var play = h('button', { class: 'tv-btn', type: 'button', 'aria-pressed': 'false', 'aria-label': 'Play' });
    var next = h('button', { class: 'tv-btn', type: 'button', 'aria-label': 'Next step', html: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M6 3l5 5-5 5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>' });
    S.playBtn = play; setPlayLabel(false);
    prev.addEventListener('click', function () { step(-1); });
    next.addEventListener('click', function () { step(1); });
    play.addEventListener('click', function () { if (S.timer) stopPlay(); else startPlay(); });
    var expandAll = h('button', { class: 'tv-btn', type: 'button' }, 'Expand all');
    var collapseAll = h('button', { class: 'tv-btn', type: 'button' }, 'Collapse all');
    expandAll.addEventListener('click', function () { S.items.forEach(function (it, i) { if (!it.li.hidden) setOpen(i, true); }); });
    collapseAll.addEventListener('click', function () { S.items.forEach(function (it, i) { setOpen(i, false); }); });
    S.counter = h('span', { class: 'tv-counter' });

    var filter = h('div', { class: 'tv-filter', role: 'group', 'aria-label': 'Show steps with tool family' }, h('span', { class: 'lbl', 'aria-hidden': 'true' }, 'Show'));
    var fbtns = S.fbtns = [];
    [['all', 'All']].concat(FAM_ORDER.filter(function (f) { return d.families[f]; }).map(function (f) { return [f, FAM[f].name]; })).forEach(function (pair) {
      var b = h('button', { type: 'button', 'data-f': pair[0], 'aria-pressed': pair[0] === 'all' ? 'true' : 'false' }, pair[0] !== 'all' ? h('span', { class: 'swatch sw-' + pair[0] }) : null, pair[1]);
      b.addEventListener('click', function () { setFilter(pair[0]); });
      fbtns.push(b);
      filter.appendChild(b);
    });

    var keys = h('details', { class: 'tv-keys' }, h('summary', null, 'Keyboard'),
      h('p', { class: 'tv-kbd' },
        h('kbd', null, '←'), ' ', h('kbd', null, '→'), ' previous / next step on the rail', h('br'),
        h('kbd', null, 'PgUp'), ' ', h('kbd', null, 'PgDn'), ' jump 10 steps, ', h('kbd', null, 'Home'), ' ', h('kbd', null, 'End'), h('br'),
        h('kbd', null, '↑'), ' ', h('kbd', null, '↓'), ' or ', h('kbd', null, 'k'), ' ', h('kbd', null, 'j'), ' move through the timeline', h('br'),
        h('kbd', null, 'Enter'), ' expand or collapse the focused step'));
    wrap.appendChild(h('div', { class: 'tv-controls' },
      h('div', { class: 'tv-btns' }, prev, play, next), S.counter,
      h('div', { class: 'tv-btns' }, expandAll, collapseAll, keys),
      filter));

    // Rail pointer + keyboard.
    function idxFromX(clientX) {
      var r = rail.getBoundingClientRect();
      var f = Math.min(0.9999, Math.max(0, (clientX - r.left) / r.width));
      return Math.floor(f * N);
    }
    var dragging = false;
    rail.addEventListener('pointerdown', function (e) { dragging = true; rail.setPointerCapture(e.pointerId); go(idxFromX(e.clientX), { scroll: true }); });
    rail.addEventListener('pointermove', function (e) {
      var i = nearestVisible(idxFromX(e.clientX));
      if (i < 0) return;
      var s = d.steps[i];
      if (dragging) go(i, { scroll: true });
      W.showTip(rail, '<b>Step ' + s.i + '</b><br>' + esc(describe(s)), e.clientX, e.clientY);
    });
    rail.addEventListener('pointerup', function (e) { if (dragging) { dragging = false; go(idxFromX(e.clientX), { expand: true, scroll: true }); } });
    rail.addEventListener('pointerleave', function () { W.scheduleHide(rail); });
    rail.addEventListener('keydown', function (e) {
      var k = e.key, t = null;
      if (k === 'ArrowRight' || k === 'ArrowUp') t = nextVisible(S.cur, 1);
      else if (k === 'ArrowLeft' || k === 'ArrowDown') t = nextVisible(S.cur, -1);
      else if (k === 'PageUp') t = moveVisible(10);
      else if (k === 'PageDown') t = moveVisible(-10);
      else if (k === 'Home') t = nextVisible(-1, 1);
      else if (k === 'End') t = nextVisible(N, -1);
      else if (k === 'Enter' || k === ' ') { e.preventDefault(); setOpen(S.cur, !S.items[S.cur].open); return; }
      if (t !== null) { e.preventDefault(); go(t, { scroll: true }); }
    });
    return wrap;
  }

  function describe(s) {
    if (s.kind !== 'action') return KIND[s.kind];
    /* Tool labels are frozen payload data ("server · tool"); rendering replaces the separator. */
    var names = (s.c || []).map(function (c) { return c.l.split(' · ').join(' / '); });
    var uniq = names.filter(function (n, i) { return names.indexOf(n) === i; });
    return uniq.slice(0, 3).join(', ') + (uniq.length > 3 ? ' …' : '') + (names.length > 1 ? ' (' + names.length + ' calls)' : '');
  }

  /* ---------------------------------------------------------------- timeline */
  function stepHead(s, i, isMoment) {
    var cls = stepClass(s);
    var line = h('span', { class: 'tv-line' });
    if (s.kind !== 'action') line.appendChild(h('span', { class: 'tv-kind ' + s.kind }, KIND[s.kind]));
    if (isMoment) line.appendChild(h('span', { class: 'tv-kind moment' }, 'Key step'));
    if (s.c) groupedChips(s.c).forEach(function (c) { line.appendChild(c); });
    var snippet = s.kind === 'user' ? firstLine(s.msg) : s.kind === 'compaction' ? 'Context summarized by opencode auto-compaction.' : firstLine(s.msg);
    if (isMoment) snippet = S.momentText[s.i] + (snippet ? ' — ' + snippet : '');
    line.appendChild(h('span', { class: 'tv-snip' }, snippet));
    if (s.c && s.c.length) {
      var prev = s.c.map(function (c) { return c.p; }).filter(function (p) { return p; });
      var uniq = prev.filter(function (p, k) { return prev.indexOf(p) === k; });
      if (uniq.length) line.appendChild(h('span', { class: 'tv-prev', title: uniq.join('\n') }, uniq.slice(0, 2).join('   ') + (uniq.length > 2 ? '   +' + (uniq.length - 2) : '')));
    }
    // aria-controls is set when the body is first rendered (setOpen), so it never references a missing element.
    var btn = h('button', { class: 'tv-step-head', type: 'button', id: 'tv-h-' + s.i, 'aria-expanded': 'false' },
      h('span', { class: 'tv-idx' }, '#' + s.i),
      line);
    var li = h('li', { class: 'tv-step', 'data-k': cls }, h('span', { class: 'dot k-' + cls, 'aria-hidden': 'true' }), btn,
      W.srcComment(S.P + 'agent/trajectory.json', 'steps[step_id=' + s.i + '].timestamp, tool_calls' + (s.kind === 'compaction' ? '; compaction: opencode.db message mode = compaction' : '')));
    btn.addEventListener('click', function () { setOpen(i, !S.items[i].open); go(i, {}); });
    btn.addEventListener('focus', function () { if (S.cur !== i) go(i, { quiet: true }); });
    return { li: li, btn: btn, body: null, open: false };
  }

  function stepBody(s) {
    var body = h('div', { class: 'tv-step-body', id: 'tv-b-' + s.i, role: 'region', 'aria-labelledby': 'tv-h-' + s.i });
    if (s.kind === 'user') {
      body.appendChild(h('pre', { class: 'tv-msg user', 'data-label': 'Step ' + s.i + ' task message' }, s.msg));
    } else if (s.kind === 'compaction') {
      body.appendChild(h('p', { class: 'tv-tok', style: { marginBottom: '8px' } }, 'opencode replaced the earlier context with this summary (session message mode = compaction). Trimmed:'));
      body.appendChild(h('pre', { class: 'tv-msg compaction', 'data-label': 'Step ' + s.i + ' compaction summary' }, s.msg));
    } else if (s.msg) {
      body.appendChild(h('pre', { class: 'tv-msg', 'data-label': 'Step ' + s.i + ' message' }, s.msg));
    }
    if (s.rs) {
      body.appendChild(h('details', { class: 'tv-rs' }, h('summary', null, 'Reasoning (' + W.fmt.int(s.rsn) + ' chars; first part shown)'), h('p', null, s.rs)));
    }
    (s.c || []).forEach(function (c, k) {
      var tag = 'Step ' + s.i + ' call ' + (k + 1) + ' ';
      var call = h('div', { class: 'tv-call' },
        h('div', { class: 'tv-call-h' }, chip(c, 1), h('code', null, c.n), c.s ? h('span', { class: 'sum' }, 'result summarized') : null, c.e ? h('span', { class: 'tag tag-bad' }, 'error') : null),
        h('div', { class: 'tv-io' },
          h('div', null, 'Args'), h('pre', { 'data-label': tag + 'arguments' }, c.a || '{}'),
          h('div', null, c.f === 'vision' ? 'Answer' : 'Result'), h('pre', { class: c.e ? 'err' : (c.f === 'vision' ? 'vis' : ''), 'data-label': tag + 'result' }, c.o || '(empty)')));
      body.appendChild(call);
    });
    var meta = [];
    if (s.tok) meta.push('prompt ' + W.fmt.int(s.tok[0]) + '  completion ' + W.fmt.int(s.tok[1]) + (s.tok[2] !== null && s.tok[2] !== undefined ? '  reasoning ' + W.fmt.int(s.tok[2]) : '') + ' tokens');
    if (meta.length) body.appendChild(W.cite(h('p', { class: 'tv-tok' }, meta.join('  ')), S.P + 'agent/trajectory.json', 'steps[step_id=' + s.i + '].timestamp, metrics'));
    return body;
  }

  function setOpen(i, open) {
    var it = S.items[i];
    if (!it) return;
    var fresh = false;
    if (open && !it.body) { it.body = stepBody(S.data.steps[i]); it.li.appendChild(it.body); it.btn.setAttribute('aria-controls', it.body.id); fresh = true; }
    it.open = open;
    it.btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (it.body) it.body.hidden = !open;
    if (fresh) W.focusIfScrollable(it.body);
  }

  /* Hide the open key-step list; refocus moves focus back to its disclosure button (Escape). */
  function closeMarkPop(refocus) {
    if (!S || !S.markPop) return;
    var pop = S.markPop, owner = pop.owner;
    pop.hidden = true;
    S.markPop = null;
    if (owner) { owner.setAttribute('aria-expanded', 'false'); if (refocus) owner.focus(); }
  }
  document.addEventListener('click', function (e) {
    if (S && S.markPop && !S.markPop.contains(e.target) && e.target !== S.markPop.owner) closeMarkPop();
  });

  /* ---------------------------------------------------------------- navigation */
  function isVisible(i) { return !!S.items[i] && !S.items[i].li.hidden; }
  function nextVisible(i, dir) {
    var j = i + dir;
    while (j >= 0 && j < S.items.length) { if (!S.items[j].li.hidden) return j; j += dir; }
    return i;
  }
  /* Nearest visible step to index i (i itself when visible); -1 when the filter hides every step. */
  function nearestVisible(i) {
    if (isVisible(i)) return i;
    for (var d = 1; d < S.items.length; d++) {
      if (isVisible(i - d)) return i - d;
      if (isVisible(i + d)) return i + d;
    }
    return -1;
  }
  /* Move by n visible steps (n may be negative). */
  function moveVisible(n) {
    var i = S.cur, dir = n > 0 ? 1 : -1;
    for (var k = 0; k < Math.abs(n); k++) { var j = nextVisible(i, dir); if (j === i) break; i = j; }
    return i;
  }
  function setFilter(f) {
    S.fbtns.forEach(function (x) { x.setAttribute('aria-pressed', x.getAttribute('data-f') === f ? 'true' : 'false'); });
    applyFilter(f);
  }
  /* Explicit jump to one step (key-step list, rail markers): a step hidden by the filter clears the filter first. */
  function jumpTo(i, opt) {
    if (!isVisible(i)) setFilter('all');
    go(i, opt);
  }
  function step(dir) { go(nextVisible(S.cur, dir), { expand: true, scroll: true }); }

  function go(i, opt) {
    opt = opt || {};
    var N = S.items.length;
    i = Math.max(0, Math.min(N - 1, i));
    i = nearestVisible(i);
    if (i < 0) return;
    if (S.items[S.cur]) S.items[S.cur].li.classList.remove('is-current');
    if (S.markEls[S.cur]) S.markEls[S.cur].classList.remove('is-current');
    if (S.momentBtns[S.cur]) S.momentBtns[S.cur].classList.remove('is-current');
    S.cur = i;
    var it = S.items[i], s = S.data.steps[i];
    it.li.classList.add('is-current');
    if (S.markEls[i]) S.markEls[i].classList.add('is-current');
    if (S.momentBtns[i]) S.momentBtns[i].classList.add('is-current');
    var pos = ((i + 0.5) / N * 100) + '%';
    S.cursor.style.left = pos;
    S.sparkCursor.setAttribute('x1', i + 0.5); S.sparkCursor.setAttribute('x2', i + 0.5);
    S.rail.setAttribute('aria-valuenow', String(i + 1));
    S.rail.setAttribute('aria-valuetext', 'Step ' + s.i + ' of ' + N + ': ' + describe(s));
    W.clear(S.counter);
    S.counter.appendChild(document.createTextNode('Step '));
    S.counter.appendChild(h('b', null, String(s.i)));
    S.counter.appendChild(document.createTextNode(' / ' + N));
    if (opt.expand) setOpen(i, true);
    if (opt.scroll && !it.li.hidden) {
      var tl = S.timeline;
      var top = it.li.offsetTop - 8;
      if (top < tl.scrollTop || top + 64 > tl.scrollTop + tl.clientHeight) tl.scrollTop = top;
    }
  }

  function setPlayLabel(on) {
    S.playBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
    S.playBtn.setAttribute('aria-label', on ? 'Pause' : 'Play');
    S.playBtn.innerHTML = on
      ? '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M5 3v10M11 3v10" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/></svg> Pause'
      : '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4.5 2.5v11l9-5.5z" fill="currentColor"/></svg> Play';
  }
  function startPlay() {
    if (!S) return;
    if (S.cur >= S.items.length - 1) go(0, { scroll: true });
    setPlayLabel(true);
    S.timer = setInterval(function () {
      var n = nextVisible(S.cur, 1);
      if (n === S.cur) { stopPlay(); return; }
      if (S.items[S.cur].open && S.autoOpened[S.cur]) setOpen(S.cur, false);
      S.autoOpened[n] = !S.items[n].open;
      go(n, { expand: true, scroll: true });
    }, PLAY_MS);
  }
  function stopPlay() {
    if (S && S.timer) { clearInterval(S.timer); S.timer = null; setPlayLabel(false); }
  }

  function applyFilter(f) {
    S.filter = f;
    var firstVisible = -1;
    S.data.steps.forEach(function (s, i) {
      var vis = stepMatches(s);
      S.items[i].li.hidden = !vis;
      S.cells[i].classList.toggle('is-dim', !vis);
      if (vis && firstVisible < 0) firstVisible = i;
    });
    if (S.items[S.cur].li.hidden && firstVisible >= 0) go(nextVisible(S.cur, 1) !== S.cur ? nextVisible(S.cur, 1) : firstVisible, { scroll: true });
    var shown = S.items.filter(function (it) { return !it.li.hidden; }).length;
    S.timeline.setAttribute('aria-label', 'Steps (' + shown + ' shown)');
  }

  /* ---------------------------------------------------------------- render */
  function renderCase(d) {
    stopPlay();
    S = { data: d, cur: 0, filter: 'all', timer: null, index: {}, items: [], markEls: {}, momentBtns: {}, momentText: {}, autoOpened: {},
      P: 'repo:wildawm-harbor/' + d.job + '/' + d.trial + '/' };
    d.steps.forEach(function (s, i) { S.index[s.i] = i; });
    d.moments.forEach(function (m) { S.momentText[m.i] = m.text; });
    W.clear(panel);
    panel.appendChild(header(d));
    panel.appendChild(railBlock(d));

    var aside = h('aside', { class: 'card tv-moments', 'aria-label': 'Key steps' }, h('h4', null, 'Key steps'));
    var mol = h('ol');
    d.moments.forEach(function (m) {
      var idx = S.index[m.i];
      var b = h('button', { type: 'button' }, h('span', { class: 'sid' }, '#' + m.i), h('span', null, m.text));
      b.addEventListener('click', function () { jumpTo(idx, { expand: true, scroll: true }); S.items[idx].btn.focus({ preventScroll: true }); });
      S.momentBtns[idx] = b;
      mol.appendChild(h('li', null, b));
    });
    aside.appendChild(mol);
    aside.appendChild(h('p', { class: 'data-note' }, 'Curated from this trajectory; each listed step contains the named tool call.'));

    var tl = h('ol', { class: 'card tv-timeline', 'aria-label': 'Steps' });
    d.steps.forEach(function (s, i) {
      var it = stepHead(s, i, S.momentText[s.i] !== undefined);
      S.items.push(it);
      tl.appendChild(it.li);
    });
    tl.addEventListener('keydown', function (e) {
      if (!e.target.classList || !e.target.classList.contains('tv-step-head')) return;
      var k = e.key, t = null;
      if (k === 'ArrowDown' || k === 'j') t = nextVisible(S.cur, 1);
      else if (k === 'ArrowUp' || k === 'k') t = nextVisible(S.cur, -1);
      else if (k === 'Home') t = nextVisible(-1, 1);
      else if (k === 'End') t = nextVisible(S.items.length, -1);
      if (t !== null) { e.preventDefault(); go(t, { scroll: true }); S.items[t].btn.focus({ preventScroll: true }); }
    });
    S.timeline = tl;
    panel.appendChild(h('div', { class: 'tv-body' }, aside, tl));
    S.layoutMarks();
    go(0, {});
    setOpen(0, false);
  }

  W.modules.push({
    name: 'the trajectory viewer',
    root: '#viewer',
    init: function () {
      root = document.getElementById('viewer');
      cases = W.data.cases.slice().sort(function (a, b) { return ORDER.indexOf(a.id) - ORDER.indexOf(b.id); });
      W.whenNear(root, function () {
        try { buildTabs(); } catch (e) { W.fail(root, 'the trajectory viewer', e); }
      }, '800px 0px');
      document.addEventListener('visibilitychange', function () { if (document.hidden) stopPlay(); });
    }
  });
})();
