const App = {
  state: { activeView: 'worlds', activeWorld: null },

  init() {
    document.querySelectorAll('.nav-tab').forEach(tab => {
      tab.addEventListener('click', () => this.switchView(tab.dataset.view));
    });
    // Deep link `?world=<id>&harness=<kind>` (from `awm agent chat`) opens the Agent tab.
    const params = new URLSearchParams(location.search);
    const dlWorld = params.get('world');
    if (dlWorld && typeof WorldsView !== 'undefined') {
      const harness = params.get('harness');
      if (harness && typeof AgentView !== 'undefined') {
        AgentView.state.activeKind[dlWorld] = harness;
      }
      this.switchView('worlds');
      WorldsView.openWorldOnTab(dlWorld, 'agent');
      return;
    }
    this.switchView('worlds');
  },

  switchView(view) {
    this.state.activeView = view;
    document.querySelectorAll('.nav-tab').forEach(t => t.classList.toggle('active', t.dataset.view === view));
    document.querySelectorAll('.view').forEach(v => v.classList.toggle('active', v.id === `view-${view}`));
    if (view === 'worlds') WorldsView.load();
    else if (view === 'tasks') TasksView.load();
    else if (view === 'profiles') ProfilesView.load();
  },

  setActiveWorld(worldId) {
    this.state.activeWorld = worldId;
    const badge = document.getElementById('activeWorldBadge');
    const name = document.getElementById('activeWorldName');
    if (worldId) {
      badge.style.display = '';
      name.textContent = worldId;
    } else {
      badge.style.display = 'none';
    }
  },

  toast(msg, duration = 3000) {
    let c = document.querySelector('.toast-container');
    if (!c) { c = document.createElement('div'); c.className = 'toast-container'; document.body.appendChild(c); }
    const t = document.createElement('div');
    t.className = 'toast';
    t.textContent = msg;
    c.appendChild(t);
    setTimeout(() => t.remove(), duration);
  },

  renderMarkdown(md) {
    const text = (md == null) ? '' : String(md);
    if (typeof marked !== 'undefined') return marked.parse(text);
    return `<pre>${text}</pre>`;
  },

  // Wrap a preformatted block with a top-right Copy button.
  withCopy(innerHtml, label = 'Copy') {
    return `<div class="copy-wrap"><button type="button" class="copy-btn">${label}</button>${innerHtml}</div>`;
  },

  async copyToClipboard(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch {}
    // Fallback for non-secure contexts.
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;top:-9999px;left:-9999px;opacity:0';
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch {}
    ta.remove();
    return ok;
  },
};

// Delegated .copy-btn handler: copies a sibling <pre>/.code-viewer or `data-copy-target`.
document.addEventListener('click', async (e) => {
  const btn = e.target.closest('.copy-btn');
  if (!btn) return;
  e.preventDefault();
  e.stopPropagation();
  let target = null;
  if (btn.dataset.copyTarget) {
    target = document.querySelector(btn.dataset.copyTarget);
  } else {
    const wrap = btn.closest('.copy-wrap');
    if (wrap) target = wrap.querySelector('pre, .code-viewer, code, textarea');
  }
  if (!target) return;
  const text = target.value !== undefined ? target.value : (target.innerText || target.textContent || '');
  const ok = await App.copyToClipboard(text);
  const original = btn.textContent;
  btn.classList.add('copied');
  btn.textContent = ok ? '✓ Copied' : 'Copy failed';
  setTimeout(() => {
    btn.classList.remove('copied');
    btn.textContent = original;
  }, 1400);
});

document.addEventListener('DOMContentLoaded', () => App.init());
