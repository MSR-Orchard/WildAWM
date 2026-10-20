/* Method section: pipeline step tabs, the candidate progression through the quality gates, and the
   quality-gate ablation. Selecting a gate in either panel selects it in both. */
(function () {
  'use strict';
  var W = window.WILDAWM;
  var h = W.h;
  var esc = function (s) { return W.fmt.esc(s); };
  var int = function (n) { return W.fmt.int(n); };

  // Gate definitions: paper:arxiv_preprint.tex (Quality Gates). Order follows the pipeline and the ablation table.
  var GATES = [
    { k: 'unsat', label: 'Initial unsatisfaction',
      what: 'The untouched initial world must fail the verifier, so no task counts as done before the agent acts.',
      f: '<i>V</i>(<i>W</i><sub>0</sub>, <i>W</i><sub>0</sub>, ∅) = 0' },
    { k: 'judge', label: 'Judge consistency',
      what: 'The synthesized verifier, the LLM judge, and the agent judge must agree on every rollout. Disagreements go back to the generator to revise the rubric and verifier.',
      f: '<i>V</i>(<i>W</i><sub>0</sub>, <i>W<sub>j</sub></i>, <i>a<sub>j</sub></i>) = <i>V</i><sub>LLM</sub><sup>(<i>j</i>)</sup> = <i>V</i><sub>agent</sub><sup>(<i>j</i>)</sup> for every rollout <i>j</i>' },
    { k: 'solv', label: 'Task solvability',
      what: 'At least one rollout must succeed, and every successful trajectory must use each interface the task declares.',
      f: '∑<sub><i>j</i></sub> <i>V</i>(<i>W</i><sub>0</sub>, <i>W<sub>j</sub></i>, <i>a<sub>j</sub></i>) ≥ 1' },
    { k: 'diff', label: 'Task difficulty',
      what: 'Successes k among the three unhinted rollouts set the label: k ≤ 1 is hard, k = 2 is medium, and k = 3 is too easy and goes back to the generator.',
      f: '<i>k</i> = ∑<sub><i>j</i> ∈ unhinted</sub> <i>V</i>(<i>W</i><sub>0</sub>, <i>W<sub>j</sub></i>, <i>a<sub>j</sub></i>)' },
    { k: 'probe', label: 'Counterfactual probe',
      what: 'The agent judge is told the opposite verdict and asked to reinvestigate. The task passes only if the judge keeps its original verdict.',
      f: '<i>V</i><sub>probe</sub> = <i>V</i>(<i>W</i><sub>0</sub>, <i>W</i>, <i>a</i>)' }
  ];
  var NUM = {};
  GATES.forEach(function (g, i) { NUM[g.k] = i + 1; });

  // Candidate progression, paper Figure fig:quality_gates (figures/quality_gate_progression_wrap_v2.pdf). Counts are the
  // figure's rows as computed by experiments/scripts/plot_quality_gate_progression_wrap_v2.py from
  // experiments/task_synthesis/task_funnel/summary.json. The first row counts proposal sketches; its direct and
  // after-feedback split of the 642 reserved slots is estimated from the proposal logs, as in the figure.
  var FLOW_SRC = 'paper-repo:experiments/task_synthesis/task_funnel/summary.json (plot_quality_gate_progression_wrap_v2.py)';
  var FLOW = [
    { k: 'diversity', label: 'Diversity gate', count: 2551, direct: 250, after: 392, lost: 1909, sketches: true },
    { k: 'unsat', label: 'Initial unsatisfaction', count: 642, direct: 411, after: 180, lost: 51 },
    { k: 'judge', label: 'Judge consistency', count: 591, direct: 280, after: 290, lost: 21 },
    { k: 'solv', label: 'Task solvability', count: 570, direct: 79, after: 368, lost: 123 },
    { k: 'diff', label: 'Task difficulty', count: 447, direct: 45, after: 342, lost: 60 },
    { k: 'probe', label: 'Counterfactual probe', count: 387, direct: 37, after: 304, lost: 46 }
  ];
  var ACCEPTED = 341;
  var SCALE = 642; // task-slot rows share one scale; the sketch row's remainder is drawn compressed

  // Gate ablation: paper:tables/analysis_merge_verifier_gates.tex (tab:ablation_study), Qwen3.5-4B + SFT.
  var SRC = 'paper:tables/analysis_merge_verifier_gates.tex';
  var METRICS = [
    { k: 'avg', i: 4, label: 'Average', name: 'average of O2W, QCB, OSV, and Val' },
    { k: 'val', i: 3, label: 'Val', name: 'in-domain validation set' },
    { k: 'o2w', i: 0, label: 'O2W', name: 'Online-Mind2Web' },
    { k: 'qcb', i: 1, label: 'QCB', name: 'QwenClawBench' },
    { k: 'osv', i: 2, label: 'OSV', name: 'OSWorld-Verified' }
  ];
  var FULL = { label: 'All five gates', sub: 'WildAWM + SFT', v: [38.49, 50.17, 17.17, 21.59, 31.86],
    quote: '\\textbf{\\ours + SFT} & 38.49 & \\textbf{50.17} & 17.17 & \\textbf{21.59} & \\textbf{31.86}' };
  var BASE = { label: 'No WildAWM data', sub: 'Qwen3.5-4B', v: [26.39, 44.06, 18.18, 0.38, 22.25],
    quote: 'Qwen-3.5-4B & 26.39 & 44.06 & 18.18 & 0.38 & 22.25' };
  var ABL = {
    unsat: { v: [33.21, 42.36, 17.71, 3.05, 24.08], quote: 'w/o Init.\\ Unsat. & 33.21 & 42.36 & 17.71 & 3.05 & 24.08' },
    judge: { v: [29.10, 37.72, 19.62, 3.79, 22.56], quote: 'w/o Judge Cons. & 29.10 & 37.72 & 19.62 & 3.79 & 22.56' },
    solv: { v: [44.87, 44.15, 12.12, 15.91, 29.26], quote: 'w/o Task Solv. & \\textbf{44.87} & 44.15 & 12.12 & 15.91 & 29.26' },
    diff: { v: [39.05, 42.29, 16.54, 7.58, 26.37], quote: 'w/o Task Diff. & 39.05 & 42.29 & 16.54 & 7.58 & 26.37' },
    probe: { v: [31.37, 42.55, 24.09, 13.64, 27.91], quote: 'w/o CF Probe & 31.37 & 42.55 & \\textbf{24.09} & 13.64 & 27.91' }
  };

  var metric = METRICS[0], sel = 'unsat', mode = 'share', flowRoot, ablRoot;

  function select(k) { sel = sel === k ? null : k; renderFlow(); renderAblation(); }
  function refocus(root, selector) { var b = W.$(selector, root); if (b) b.focus({ preventScroll: true }); }
  function pct(n, d) { return Math.round(100 * n / d) + '%'; }
  function badge(k) {
    return NUM[k] ? h('span', { class: 'gl-n', 'aria-hidden': 'true' }, String(NUM[k])) : h('span', { class: 'gl-n is-blank', 'aria-hidden': 'true' });
  }

  /* ---------------------------------------------------------------- Candidate progression */
  function renderFlow() {
    W.clear(flowRoot);
    flowRoot.appendChild(h('div', { class: 'gates-top' },
      h('ul', { class: 'legend', 'aria-label': 'Legend' },
        h('li', null, h('span', { class: 'sw fl-sw-d' }), 'Direct'),
        h('li', null, h('span', { class: 'sw fl-sw-a' }), 'After feedback'),
        h('li', null, h('span', { class: 'sw fl-sw-r' }), 'Rejected')),
      h('div', { class: 'seg', role: 'group', 'aria-label': 'Segment labels' }, [['share', 'Share'], ['count', 'Count']].map(function (m) {
        var b = h('button', { type: 'button', 'aria-pressed': mode === m[0] ? 'true' : 'false' }, m[1]);
        b.addEventListener('click', function () { mode = m[0]; renderFlow(); });
        return b;
      }))));

    var ol = h('ol', { class: 'fl' });
    FLOW.forEach(function (r) {
      var on = sel === r.k;
      var dl = mode === 'share' ? pct(r.direct, r.count) : int(r.direct);
      var al = mode === 'share' ? pct(r.after, r.count) : int(r.after);
      var scale = h('span', { class: 'fl-scale' },
        h('span', { class: 'fl-rib', style: { width: ((r.direct + r.after) / SCALE * 100) + '%' } }),
        h('span', { class: 'fl-d', style: { width: (r.direct / SCALE * 100) + '%' } }),
        h('span', { class: 'fl-a', style: { width: (r.after / SCALE * 100) + '%' } }, h('span', null, al)),
        r.sketches ? h('span', { class: 'fl-r is-broken' }) : h('span', { class: 'fl-r', style: { width: (r.lost / SCALE * 100) + '%' } }),
        h('span', { class: 'fl-lost num' }, '−' + int(r.lost)));
      var btn = h('button', { class: 'fl-btn', type: 'button', 'aria-pressed': on ? 'true' : 'false',
        'aria-label': r.label + ': ' + int(r.count) + (r.sketches ? ' sketches' : ' candidates') + ' enter; ' + int(r.direct) + ' pass directly, ' + int(r.after) + ' pass after feedback, ' + int(r.lost) + ' are rejected.' },
        h('span', { class: 'fl-head' }, h('span', { class: 'fl-name' }, badge(r.k), h('span', null, r.label)), h('span', { class: 'fl-count num' }, int(r.count))),
        h('span', { class: 'fl-bar' }, h('span', { class: 'fl-gut num' }, dl), scale));
      btn.addEventListener('click', function () { select(r.k); refocus(flowRoot, '.fl-row[data-k="' + r.k + '"] .fl-btn'); });
      W.tooltip(btn, function () {
        return '<b>' + esc(r.label) + '</b><br>' + int(r.count) + (r.sketches ? ' proposal sketches' : ' candidates') + ' enter' +
          '<br>' + int(r.direct) + ' pass directly' + W.TSEP + int(r.after) + ' after feedback' + W.TSEP + int(r.lost) + ' rejected';
      });
      ol.appendChild(W.cite(h('li', { class: 'fl-row' + (on ? ' is-sel' : ''), 'data-k': r.k }, btn), FLOW_SRC,
        r.label + ': enter ' + r.count + ', direct ' + r.direct + ', after feedback ' + r.after + ', rejected ' + r.lost));
    });
    ol.appendChild(W.cite(h('li', { class: 'fl-row fl-end' },
      h('span', { class: 'fl-head' },
        h('span', { class: 'fl-name' }, h('span', { class: 'gl-n is-blank', 'aria-hidden': 'true' }), h('span', null, 'Accepted tasks')),
        h('span', { class: 'fl-count num' }, int(ACCEPTED)))),
      'paper:tables/task_syn_basics_full_width_v1.tex', 'Tasks & 341'));
    flowRoot.appendChild(ol);
    flowRoot.appendChild(h('p', { class: 'note' }, 'Direct: passed on the first attempt. After feedback: passed after at least one feedback-driven revision anywhere in synthesis. The first row counts proposal sketches, the others task slots. Source: the candidate-progression figure in the paper.'));
  }

  /* ---------------------------------------------------------------- Gate ablation */
  function niceMax(v) { return Math.max(10, Math.ceil(v / 10) * 10); }
  function bar(cls, value, max) {
    return h('span', { class: 'gb-track', 'aria-hidden': 'true' }, h('span', { class: 'gb-fill ' + cls, style: { width: (value / max * 100) + '%' } }));
  }
  function renderAblation() {
    var i = metric.i;
    var all = [FULL.v[i], BASE.v[i]].concat(GATES.map(function (g) { return ABL[g.k].v[i]; }));
    var max = niceMax(Math.max.apply(null, all));
    var full = FULL.v[i];
    W.clear(ablRoot);
    ablRoot.appendChild(h('div', { class: 'gates-top' },
      h('p', { class: 'gates-axis-k' }, 'Qwen3.5-4B score, ' + metric.name),
      h('div', { class: 'seg', role: 'group', 'aria-label': 'Score shown' }, METRICS.map(function (m) {
        var b = h('button', { type: 'button', 'aria-pressed': m === metric ? 'true' : 'false', title: m.name }, m.label);
        b.addEventListener('click', function () { metric = m; renderAblation(); });
        return b;
      }))));
    var list = h('ul', { class: 'gl' });
    function refRow(r, cls) {
      return W.cite(h('li', { class: 'gl-row gl-ref ' + cls },
        h('span', { class: 'gl-name' }, h('b', null, r.label), h('small', null, r.sub)),
        bar(cls === 'is-full' ? 'f-full' : 'f-base', r.v[i], max),
        h('span', { class: 'gl-val num' }, r.v[i].toFixed(2)),
        h('span', { class: 'gl-d' })), SRC, r.quote);
    }
    list.appendChild(refRow(FULL, 'is-full'));
    GATES.forEach(function (g) {
      var a = ABL[g.k], d = a.v[i] - full, isOpen = sel === g.k;
      var btn = h('button', { class: 'gl-btn', type: 'button', 'aria-expanded': isOpen ? 'true' : 'false', 'aria-controls': 'gate-' + g.k },
        h('span', { class: 'gl-name' }, badge(g.k), h('span', null, h('small', null, 'without'), ' ', h('b', null, g.label))),
        bar('f-gate', a.v[i], max),
        h('span', { class: 'gl-val num' }, a.v[i].toFixed(2)),
        h('span', { class: 'gl-d num' + (d > 0 ? ' is-up' : '') }, (d > 0 ? '+' : '−') + Math.abs(d).toFixed(2)));
      btn.addEventListener('click', function () { select(g.k); refocus(ablRoot, '[aria-controls="gate-' + g.k + '"]'); });
      var detail = h('div', { class: 'gl-detail', id: 'gate-' + g.k, hidden: !isOpen }, h('p', null, g.what), h('p', { class: 'formula', html: g.f }));
      list.appendChild(W.cite(h('li', { class: 'gl-row gl-gate' + (isOpen ? ' is-open' : '') }, btn, detail), SRC, a.quote));
    });
    list.appendChild(refRow(BASE, 'is-base'));
    ablRoot.appendChild(list);
    ablRoot.appendChild(h('div', { class: 'gl-axis', 'aria-hidden': 'true' }, h('span'),
      h('span', { class: 'gl-ticks' }, [0, 0.25, 0.5, 0.75, 1].map(function (t) { return h('span', { style: { left: (t * 100) + '%' } }, String(Math.round(t * max))); })),
      h('span'), h('span')));
    ablRoot.appendChild(h('p', { class: 'note' }, 'Each bar trains Qwen3.5-4B with SFT data synthesized with one gate removed. Val is the in-domain validation set; O2W, QCB, and OSV are Online-Mind2Web, QwenClawBench, and OSWorld-Verified. Source: the quality-gate ablation table in the paper.'));
  }

  W.modules.push({
    name: 'the method section',
    root: '#gates',
    init: function () {
      W.panelTabs(W.$('[data-tabs="pipe"]'));
      flowRoot = document.getElementById('progression');
      ablRoot = document.getElementById('gates');
      if (flowRoot) renderFlow();
      if (ablRoot) renderAblation();
    }
  });
})();
