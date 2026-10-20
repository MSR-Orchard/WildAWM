/* WildAWM page core: DOM helpers, source comments, tooltip, lazy loading, tabs, section bar,
   copy buttons, module boot. Classic script (no modules) so the page also works from file://. */
(function () {
  'use strict';

  var W = (window.WILDAWM = window.WILDAWM || {});
  W.modules = W.modules || [];
  W.data = window.WILDAWM_DATA || null;
  W.reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ------------------------------------------------------------ DOM helpers */
  W.h = function (tag, attrs) {
    var el = document.createElement(tag);
    if (attrs) {
      for (var k in attrs) {
        if (!Object.prototype.hasOwnProperty.call(attrs, k)) continue;
        var v = attrs[k];
        if (v === null || v === undefined || v === false) continue;
        if (k === 'class') el.className = v;
        else if (k === 'text') el.textContent = v;
        else if (k === 'html') el.innerHTML = v;
        else if (k === 'style' && typeof v === 'object') { for (var s in v) { if (s.slice(0, 2) === '--') el.style.setProperty(s, v[s]); else el.style[s] = v[s]; } }
        else if (k.slice(0, 2) === 'on' && typeof v === 'function') el.addEventListener(k.slice(2), v);
        else if (k === 'dataset') { for (var d in v) el.dataset[d] = v[d]; }
        else el.setAttribute(k, v === true ? '' : v);
      }
    }
    for (var i = 2; i < arguments.length; i++) W.append(el, arguments[i]);
    return el;
  };
  W.append = function (el, child) {
    if (child === null || child === undefined || child === false) return el;
    if (Array.isArray(child)) { child.forEach(function (c) { W.append(el, c); }); return el; }
    if (typeof child === 'string' || typeof child === 'number') el.appendChild(document.createTextNode(String(child)));
    else el.appendChild(child);
    return el;
  };
  W.clear = function (el) { while (el.firstChild) el.removeChild(el.firstChild); return el; };
  W.$ = function (sel, root) { return (root || document).querySelector(sel); };
  W.$$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

  /* Make scrollable <pre> blocks keyboard-reachable only when their content overflows. */
  var scrollSeq = 0;
  W.focusIfScrollable = function (root) {
    requestAnimationFrame(function () {
      W.$$('pre', root).forEach(function (pre) {
        if (pre.scrollHeight > pre.clientHeight + 1 || pre.scrollWidth > pre.clientWidth + 1) {
          pre.tabIndex = 0;
          pre.setAttribute('role', 'region');
          if (!pre.getAttribute('aria-label')) pre.setAttribute('aria-label', pre.dataset.label || ('Scrollable text ' + (++scrollSeq)));
        }
      });
    });
  };

  /* Source comment: an HTML comment node placed right after a rendered statistic. */
  W.srcComment = function (src, quote) {
    var text = ' src: ' + src + (quote ? ' :: "' + String(quote).replace(/--/g, '–') + '"' : '') + ' ';
    return document.createComment(text);
  };
  W.cite = function (el, src, quote) { el.appendChild(W.srcComment(src, quote)); return el; };

  /* ------------------------------------------------------------ Formatting */
  W.fmt = {
    int: function (n) { return n === null || n === undefined ? '' : Number(n).toLocaleString('en-US'); },
    pct: function (v, d) { return (v * 100).toFixed(d === undefined ? 1 : d) + '%'; },
    secs: function (s) {
      if (s === null || s === undefined) return '';
      var m = Math.floor(s / 60), r = Math.round(s - m * 60);
      if (r === 60) { m += 1; r = 0; }
      return '+' + String(m).padStart(2, '0') + ':' + String(r).padStart(2, '0');
    },
    dur: function (s) {
      var m = Math.floor(s / 60), r = Math.round(s - m * 60);
      return m ? m + ' min ' + String(r).padStart(2, '0') + ' s' : r + ' s';
    },
    esc: function (s) {
      return String(s).replace(/[&<>"']/g, function (c) { return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]; });
    }
  };
  W.msep = function () { return W.h('span', { class: 'msep', 'aria-hidden': 'true' }); };
  W.TSEP = '<span class="tsep" aria-hidden="true"></span>';

  /* ------------------------------------------------------------ Tooltip */
  /* One shared role="tooltip" element. The active trigger gets aria-describedby="tooltip" while the tooltip
     is shown; it stays open while the pointer is over the trigger or the tooltip, and Escape, blur, or
     scrolling dismiss it. */
  var tip = null, tipTarget = null, hideTimer = null;
  function getTip() {
    if (!tip) {
      tip = document.getElementById('tooltip');
      if (tip) {
        tip.addEventListener('mouseenter', cancelHide);
        tip.addEventListener('mouseleave', function () { W.scheduleHide(); });
      }
    }
    return tip;
  }
  function cancelHide() { if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; } }
  function describedBy(el, on) {
    var ids = (el.getAttribute('aria-describedby') || '').split(/\s+/).filter(function (x) { return x && x !== 'tooltip'; });
    if (on) ids.push('tooltip');
    if (ids.length) el.setAttribute('aria-describedby', ids.join(' ')); else el.removeAttribute('aria-describedby');
  }
  function placeTip(x, y) {
    if (!tip) return;
    var r = tip.getBoundingClientRect();
    var left = Math.min(window.innerWidth - r.width - 8, Math.max(8, x + 14));
    var top = y - r.height - 14;
    if (top < 8) top = y + 18;
    tip.style.left = left + 'px';
    tip.style.top = top + 'px';
  }
  W.showTip = function (target, html, x, y) {
    var t = getTip();
    if (!t) return;
    cancelHide();
    if (tipTarget && tipTarget !== target) describedBy(tipTarget, false);
    tipTarget = target;
    describedBy(target, true);
    t.innerHTML = html;
    t.classList.add('is-on');
    t.setAttribute('aria-hidden', 'false');
    if (x === undefined) {
      var r = target.getBoundingClientRect();
      x = r.left + Math.min(r.width / 2, 120); y = r.top;
    }
    placeTip(x, y);
  };
  W.hideTip = function (target) {
    if (!tip || (target && target !== tipTarget)) return;
    cancelHide();
    tip.classList.remove('is-on');
    tip.setAttribute('aria-hidden', 'true');
    if (tipTarget) describedBy(tipTarget, false);
    tipTarget = null;
  };
  W.scheduleHide = function (target) {
    if (target && target !== tipTarget) return;
    cancelHide();
    hideTimer = setTimeout(function () { hideTimer = null; W.hideTip(); }, 260);
  };
  W.tooltip = function (el, htmlFn) {
    el.addEventListener('mouseenter', function (e) { W.showTip(el, htmlFn(), e.clientX, e.clientY); });
    el.addEventListener('mousemove', function (e) { if (tipTarget === el && !hideTimer) placeTip(e.clientX, e.clientY); });
    el.addEventListener('mouseleave', function () { W.scheduleHide(el); });
    el.addEventListener('focus', function () { W.showTip(el, htmlFn()); });
    el.addEventListener('blur', function () { W.hideTip(el); });
  };
  W.placeTip = placeTip;
  window.addEventListener('scroll', function () {
    if (tipTarget && tipTarget === document.activeElement) {
      var r = tipTarget.getBoundingClientRect();
      placeTip(r.left + Math.min(r.width / 2, 120), r.top);
    } else if (tipTarget && !tipTarget.hasAttribute('data-tip-sticky')) W.hideTip();
  }, { passive: true });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') W.hideTip(); });

  /* ------------------------------------------------------------ Lazy loading */
  var loaded = {};
  W.loadScript = function (src) {
    if (loaded[src]) return loaded[src];
    loaded[src] = new Promise(function (resolve, reject) {
      var s = document.createElement('script');
      s.src = src; s.async = true;
      s.onload = function () { resolve(); };
      s.onerror = function () { delete loaded[src]; reject(new Error('Could not load ' + src)); };
      document.head.appendChild(s);
    });
    return loaded[src];
  };
  W.whenNear = function (el, cb, margin) {
    if (!el) return;
    if (!('IntersectionObserver' in window)) { cb(); return; }
    var io = new IntersectionObserver(function (entries) {
      for (var i = 0; i < entries.length; i++) {
        if (entries[i].isIntersecting) { io.disconnect(); cb(); return; }
      }
    }, { rootMargin: margin || '600px 0px' });
    io.observe(el);
  };
  W.fail = function (root, what, err) {
    if (!root) return;
    W.clear(root);
    root.classList.remove('loading');
    root.appendChild(W.h('p', { class: 'empty', role: 'status' }, 'Could not render ' + what + '. ' + (err && err.message ? err.message : '')));
    if (window.console && console.warn) console.warn('[wildawm] ' + what, err);
  };

  /* ------------------------------------------------------------ Tabs (roving tabindex, automatic activation) */
  W.tabs = function (list, onSelect) {
    var tabs = W.$$('[role="tab"]', list);
    function select(tab, focus) {
      tabs.forEach(function (t) {
        var on = t === tab;
        t.setAttribute('aria-selected', on ? 'true' : 'false');
        t.tabIndex = on ? 0 : -1;
      });
      if (focus) tab.focus();
      onSelect(tab, tabs.indexOf(tab));
    }
    tabs.forEach(function (t, i) {
      t.addEventListener('click', function () { select(t, false); });
      t.addEventListener('keydown', function (e) {
        var j = null;
        if (e.key === 'ArrowRight' || e.key === 'ArrowDown') j = (i + 1) % tabs.length;
        else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') j = (i - 1 + tabs.length) % tabs.length;
        else if (e.key === 'Home') j = 0;
        else if (e.key === 'End') j = tabs.length - 1;
        if (j !== null) { e.preventDefault(); select(tabs[j], true); }
      });
    });
    var cur = tabs.filter(function (t) { return t.getAttribute('aria-selected') === 'true'; })[0] || tabs[0];
    select(cur, false);
    return { select: function (i) { select(tabs[i], false); } };
  };
  /* One panel per tab, located through aria-controls. */
  W.panelTabs = function (list, after) {
    return W.tabs(list, function (tab, i) {
      W.$$('[role="tab"]', list).forEach(function (t) {
        var p = document.getElementById(t.getAttribute('aria-controls'));
        if (p) p.hidden = t !== tab;
      });
      if (after) after(tab, i);
    });
  };

  /* Segmented buttons: exactly one pressed. */
  W.seg = function (group, onChange) {
    if (!group) return;
    var btns = W.$$('button', group);
    btns.forEach(function (b) {
      b.addEventListener('click', function () {
        btns.forEach(function (x) { x.setAttribute('aria-pressed', x === b ? 'true' : 'false'); });
        onChange(b);
      });
    });
  };

  /* ------------------------------------------------------------ Section bar */
  /* The bar appears once the title block has scrolled away and marks the section in view. */
  function initBar() {
    var bar = document.getElementById('bar'), hero = document.getElementById('top');
    if (!bar || !hero || !('IntersectionObserver' in window)) { if (bar) bar.classList.add('is-on'); return; }
    new IntersectionObserver(function (en) { bar.classList.toggle('is-on', !en[0].isIntersecting); }).observe(hero);

    var links = W.$$('.bar-links a', bar), map = {}, visible = {};
    links.forEach(function (a) { map[a.getAttribute('href').slice(1)] = a; });
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) { visible[en.target.id] = en.isIntersecting ? en.intersectionRatio : 0; });
      var best = null, bestR = 0;
      Object.keys(visible).forEach(function (id) { if (visible[id] > bestR) { best = id; bestR = visible[id]; } });
      links.forEach(function (a) { a.removeAttribute('aria-current'); });
      if (best && map[best]) {
        map[best].setAttribute('aria-current', 'true');
        var ul = map[best].closest('ul');
        if (ul && ul.scrollWidth > ul.clientWidth) ul.scrollLeft = map[best].parentNode.offsetLeft - 8;
      }
    }, { rootMargin: '-64px 0px -50% 0px', threshold: [0, 0.05, 0.15, 0.3, 0.5] });
    Object.keys(map).forEach(function (id) { var s = document.getElementById(id); if (s) io.observe(s); });
  }

  /* ------------------------------------------------------------ Copy buttons */
  function initCopy() {
    document.addEventListener('click', function (e) {
      var btn = e.target.closest ? e.target.closest('button.copy[data-copy]') : null;
      if (!btn) return;
      var node = document.querySelector(btn.getAttribute('data-copy'));
      var text = node ? node.textContent : '';
      function done(msg) { btn.textContent = msg; setTimeout(function () { btn.textContent = 'Copy'; }, 1600); }
      function fallback() {
        var r = document.createRange(); r.selectNodeContents(node);
        var sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(r);
        done('Selected');
      }
      if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(function () { done('Copied'); }, fallback);
      else fallback();
    });
  }

  /* ------------------------------------------------------------ Boot */
  function boot() {
    try { initBar(); } catch (e) { console.warn('[wildawm] bar', e); }
    try { initCopy(); } catch (e) { console.warn('[wildawm] copy', e); }
    if (!W.data) {
      W.$$('.loading').forEach(function (el) { el.textContent = 'Data file assets/data/site-data.js did not load.'; });
      return;
    }
    W.modules.forEach(function (m) {
      try { m.init(); } catch (e) { W.fail(m.root ? document.querySelector(m.root) : null, m.name, e); }
    });
  }
  if (document.readyState === 'complete') setTimeout(boot, 0);
  else document.addEventListener('DOMContentLoaded', boot);
})();
