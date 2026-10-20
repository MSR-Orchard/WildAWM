// Worlds view: sidebar of instances grouped by profile, with per-world sub-tabs.
const WorldsView = {
  state: { activeWorld: null, subTab: 'overview', _subTabGen: 0 },

  async load() {
    const el = document.getElementById('view-worlds');
    el.innerHTML = `
      <div class="layout" id="worldsLayout">
        <div class="card" id="worldsSidebar">
          <div class="loading"><div class="spinner"></div> Loading…</div>
        </div>
        <div class="sidebar-resizer" id="sidebarResizer"
             title="Drag to resize the sidebar (double-click to reset)"></div>
        <div id="worldsMain">
          <div class="empty-state">
            <div class="empty-title">Pick a world or start a new one</div>
            <p style="color:var(--text-muted);font-size:0.85rem">Choose a profile from the sidebar to spin up a parallel world snapshot.</p>
          </div>
        </div>
      </div>
    `;
    this._wireSidebarResizer();
    await this._loadSidebar();
  },

  _wireSidebarResizer() {
    // Restore the persisted width, clamped so a stale value cannot hide the sidebar.
    const stored = parseInt(localStorage.getItem('worldsSidebarWidth') || '', 10);
    const view = document.getElementById('view-worlds');
    if (stored && stored >= 180 && stored <= 720) {
      view.style.setProperty('--sidebar-w', stored + 'px');
    }
    const resizer = document.getElementById('sidebarResizer');
    if (!resizer) return;
    let dragging = false, startX = 0, startW = 0;
    const onMove = (e) => {
      if (!dragging) return;
      const dx = e.clientX - startX;
      let w = Math.round(startW + dx);
      if (w < 180) w = 180;
      if (w > 720) w = 720;
      view.style.setProperty('--sidebar-w', w + 'px');
    };
    const onUp = () => {
      if (!dragging) return;
      dragging = false;
      resizer.classList.remove('dragging');
      document.body.classList.remove('resizing-sidebar');
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      const w = parseInt(view.style.getPropertyValue('--sidebar-w'), 10);
      if (w) localStorage.setItem('worldsSidebarWidth', String(w));
    };
    resizer.addEventListener('mousedown', (e) => {
      e.preventDefault();
      dragging = true;
      startX = e.clientX;
      const sidebar = document.getElementById('worldsSidebar');
      startW = sidebar ? sidebar.getBoundingClientRect().width : 260;
      resizer.classList.add('dragging');
      document.body.classList.add('resizing-sidebar');
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
    resizer.addEventListener('dblclick', () => {
      view.style.removeProperty('--sidebar-w');
      localStorage.removeItem('worldsSidebarWidth');
    });
  },

  async _loadSidebar() {
    let profiles, worlds;
    try {
      [profiles, worlds] = await Promise.all([
        API.profiles.list(),
        API.worlds.list(),
      ]);
    } catch (e) {
      document.getElementById('worldsSidebar').innerHTML =
        '<div class="empty-state"><div class="empty-title">Sidebar load failed</div><p>' + this._esc(e.message) + '</p></div>';
      return;
    }
    this._renderSidebar(profiles.profiles || [], worlds.worlds || []);
  },

  _esc(s) {
    return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  },

  _renderSidebar(profiles, worlds) {
    const sidebar = document.getElementById('worldsSidebar');
    const byProfile = {};
    for (const w of worlds) {
      const p = w.profile || '(unknown)';
      (byProfile[p] = byProfile[p] || []).push(w);
    }

    // Only profiles with a compiled master can host new worlds.
    const usable = profiles.filter(p => p.master_ready);
    const skipped = profiles.length - usable.length;

    let html = `
      <div style="padding:8px 8px 4px 8px">
        <button class="btn btn-primary" id="startNewWorldBtn"
                style="width:100%;font-weight:600">+ Start new world</button>
      </div>
      <div style="padding:0 8px 6px 8px;font-size:0.7rem;color:var(--text-muted);border-bottom:1px solid var(--border-light, #e5e7eb);display:flex;align-items:center;justify-content:space-between;gap:6px">
        <span>${worlds.length} world snapshot${worlds.length === 1 ? '' : 's'} · ${usable.length} profile${usable.length === 1 ? '' : 's'} ready${skipped ? ` · ${skipped} not compiled` : ''}</span>
        ${worlds.length ? `<button id="destroyAllWorldsBtn" title="Destroy every world snapshot"
                style="background:none;border:none;cursor:pointer;color:#b91c1c;font-size:0.7rem;font-weight:600;white-space:nowrap;padding:2px 4px">Destroy all</button>` : ''}
      </div>
    `;

    for (const p of usable) {
      const insts = byProfile[p.name] || [];
      const open = insts.length > 0 ? 'open' : '';
      html += `
        <details ${open} style="border-bottom:1px solid var(--border-light, #e5e7eb)">
          <summary style="cursor:pointer;padding:8px 10px;font-weight:600;font-size:0.85rem;display:flex;align-items:center;justify-content:space-between;gap:6px">
            <span>${this._esc(p.name)}</span>
            <span style="display:flex;align-items:center;gap:8px">
              ${insts.length ? `<button class="profile-destroy-all" data-profile="${this._esc(p.name)}"
                      title="Destroy all ${insts.length} world${insts.length === 1 ? '' : 's'} for ${this._esc(p.name)}"
                      style="background:none;border:none;cursor:pointer;color:#b91c1c;font-size:0.68rem;font-weight:600;padding:0">Destroy all</button>` : ''}
              <span style="font-weight:400;font-size:0.72rem;color:var(--text-muted)">${insts.length} world${insts.length === 1 ? '' : 's'}</span>
            </span>
          </summary>
          <div style="padding:4px 4px 8px 4px;display:flex;flex-direction:column;gap:4px">
      `;
      if (!insts.length) {
        html += `<div style="padding:8px 10px;font-size:0.78rem;color:var(--text-muted)">No worlds yet for this profile. Click + Start new world.</div>`;
      } else {
        for (const w of insts) {
          const dot = w.running
            ? '<span title="running" style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#16a34a;margin-right:6px"></span>'
            : '<span title="stopped" style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#9ca3af;margin-right:6px"></span>';
          html += `
            <div class="world-row sidebar-item" data-world-id="${this._esc(w.id)}"
                 style="display:flex;align-items:center;justify-content:space-between;padding:6px 8px;cursor:pointer;border-radius:4px"
                 title="${this._esc(w.id)}">
              <div style="display:flex;align-items:center;min-width:0;flex:1;gap:4px">
                ${dot}
                <div style="min-width:0;flex:1">
                  <div style="font-family:JetBrains Mono, ui-monospace, monospace;font-size:0.75rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${this._esc(w.id)}</div>
                  <div style="font-size:0.68rem;color:var(--text-muted)">Day ${w.started_at_day} · ${this._esc(w.started_at_date || '')}</div>
                </div>
              </div>
              <button class="btn btn-secondary world-row-menu" data-world-id="${this._esc(w.id)}"
                      title="Lifecycle"
                      style="font-size:0.7rem;padding:2px 6px">⋯</button>
            </div>
          `;
        }
      }
      html += `</div></details>`;
    }
    sidebar.innerHTML = html;

    document.getElementById('startNewWorldBtn').addEventListener('click',
      () => this.openStartNewWorldDialog());
    const destroyAllBtn = document.getElementById('destroyAllWorldsBtn');
    if (destroyAllBtn) {
      destroyAllBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        this._destroyAll(null, worlds.length);
      });
    }
    sidebar.querySelectorAll('.profile-destroy-all').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        e.preventDefault();
        const profile = btn.dataset.profile;
        this._destroyAll(profile, (byProfile[profile] || []).length);
      });
    });
    sidebar.querySelectorAll('.world-row').forEach(row => {
      row.addEventListener('click', (e) => {
        if (e.target.closest('.world-row-menu')) return;
        this.selectWorld(row.dataset.worldId);
      });
    });
    sidebar.querySelectorAll('.world-row-menu').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        this._openLifecycleMenu(btn);
      });
    });
    if (this.state.activeWorld) {
      const cur = sidebar.querySelector(`.world-row[data-world-id="${CSS.escape(this.state.activeWorld)}"]`);
      if (cur) cur.style.background = 'var(--accent-light, #eef2ff)';
    }
  },

  _openLifecycleMenu(anchor) {
    const worldId = anchor.dataset.worldId;
    const existing = document.getElementById('lifecycleMenu');
    if (existing) existing.remove();
    const rect = anchor.getBoundingClientRect();
    const menu = document.createElement('div');
    menu.id = 'lifecycleMenu';
    menu.style.cssText = `
      position:fixed; top:${rect.bottom + 4}px; left:${rect.left - 120}px;
      z-index:9999; background:white;
      border:1px solid var(--border-light, #e5e7eb); border-radius:6px;
      box-shadow:0 6px 20px rgba(0,0,0,0.12); padding:4px; min-width:160px;
    `;
    const items = [
      { label: 'Start',   action: 'start',   color: '#0f766e' },
      { label: 'Stop',    action: 'stop',    color: '#92400e' },
      { label: 'Reset',   action: 'reset',   color: '#b91c1c' },
      { label: 'Destroy', action: 'destroy', color: '#7f1d1d', danger: true },
    ];
    menu.innerHTML = items.map(i =>
      `<button class="lifecycle-item" data-action="${i.action}"
               style="display:block;width:100%;text-align:left;padding:6px 10px;background:none;border:none;cursor:pointer;font-size:0.82rem;color:${i.color};border-radius:4px${i.danger ? ';font-weight:600' : ''}">${i.label}</button>`
    ).join('');
    document.body.appendChild(menu);
    const close = () => { menu.remove(); document.removeEventListener('click', close); };
    setTimeout(() => document.addEventListener('click', close), 0);
    menu.querySelectorAll('.lifecycle-item').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        this._lifecycleAction(worldId, btn.dataset.action);
        close();
      });
    });
  },

  async _lifecycleAction(worldId, action) {
    if (action === 'destroy') {
      if (!confirm(`Destroy world "${worldId}"?\n\nThis is permanent: every DB row, file, agent session and memory is deleted. The instance directory is removed.`)) return;
      App.toast(`Destroying ${worldId}…`);
      try {
        await API.worlds.destroy(worldId);
        if (this.state.activeWorld === worldId) {
          this.state.activeWorld = null;
          document.getElementById('worldsMain').innerHTML =
            '<div class="empty-state"><div class="empty-title">World destroyed</div></div>';
        }
        App.toast('Destroyed.');
      } catch (e) { App.toast('Destroy failed: ' + e.message, 6000); }
      await this._loadSidebar();
      return;
    }
    if (action === 'reset') {
      if (!confirm(`Reset "${worldId}"?\n\nThis re-creates the world from the same starting day, deleting every agent modification (sessions, MEMORY, edited files, DB changes, episode snapshots).`)) return;
      App.toast(`Resetting ${worldId}…`);
      try { await API.runtime.reset(worldId); App.toast('Reset complete.'); }
      catch (e) { App.toast('Reset failed: ' + e.message, 6000); }
      await this._loadSidebar();
      if (this.state.activeWorld === worldId) await this.selectWorld(worldId);
      return;
    }
    if (action === 'start') {
      App.toast(`Starting ${worldId}…`);
      try { await API.runtime.start(worldId); App.toast('Started.'); }
      catch (e) { App.toast('Start failed: ' + e.message, 6000); }
      await this._loadSidebar();
      if (this.state.activeWorld === worldId) await this.selectWorld(worldId);
      return;
    }
    if (action === 'stop') {
      App.toast(`Stopping ${worldId}…`);
      try { await API.runtime.stop(worldId); App.toast('Stopped.'); }
      catch (e) { App.toast('Stop failed: ' + e.message, 6000); }
      await this._loadSidebar();
      if (this.state.activeWorld === worldId) await this.selectWorld(worldId);
    }
  },

  // profile === null destroys every world.
  async _destroyAll(profile, count) {
    if (!count) { App.toast('No worlds to destroy.'); return; }
    const scope = profile ? `all ${count} world${count === 1 ? '' : 's'} for "${profile}"` : `ALL ${count} world snapshot${count === 1 ? '' : 's'} (every profile)`;
    if (!confirm(`Destroy ${scope}?\n\nThis is permanent: every DB row, file, agent session, memory and episode snapshot is deleted. The instance directories are removed.`)) return;
    const phrase = profile || 'ALL';
    const typed = prompt(`This cannot be undone. Type "${phrase}" to confirm destroying ${scope}.`);
    if (typed === null) return;
    if (typed.trim() !== phrase) { App.toast('Confirmation text did not match — aborted.'); return; }

    App.toast(`Destroying ${scope}…`);
    try {
      const res = await API.worlds.destroyAll(profile || undefined);
      const n = (res.destroyed || []).length;
      const fails = (res.failed || []).length;
      if (this.state.activeWorld && (res.destroyed || []).includes(this.state.activeWorld)) {
        this.state.activeWorld = null;
        document.getElementById('worldsMain').innerHTML =
          '<div class="empty-state"><div class="empty-title">Worlds destroyed</div></div>';
      }
      App.toast(`Destroyed ${n} world${n === 1 ? '' : 's'}${fails ? ` · ${fails} failed` : ''}.`, fails ? 6000 : 3000);
    } catch (e) {
      App.toast('Destroy all failed: ' + e.message, 6000);
    }
    await this._loadSidebar();
  },

  async openStartNewWorldDialog() {
    let profiles;
    try { profiles = (await API.profiles.list()).profiles || []; }
    catch (e) { App.toast('Profiles load failed: ' + e.message); return; }
    profiles = profiles.filter(p => p.master_ready);
    if (!profiles.length) {
      App.toast('No built profiles. Run `awm syn profile --name <name>` then `awm syn world --profile <id> --to_day N` first.', 8000);
      return;
    }

    const overlay = document.createElement('div');
    overlay.id = 'startNewWorldDialog';
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:9999;display:flex;align-items:center;justify-content:center;padding:20px';
    const dlg = document.createElement('div');
    dlg.style.cssText = 'background:white;border-radius:8px;max-width:760px;width:100%;max-height:90vh;display:flex;flex-direction:column;overflow:hidden';
    let html = `
      <div style="padding:14px 18px;border-bottom:1px solid var(--border-light, #e5e7eb);display:flex;justify-content:space-between;align-items:center">
        <div>
          <div style="font-weight:600">Start a new world snapshot</div>
          <div style="font-size:0.78rem;color:var(--text-muted)">Pick a profile + starting day. Each world is fully isolated.</div>
        </div>
        <button class="btn btn-secondary" id="snwCloseBtn">Cancel</button>
      </div>
      <div style="padding:14px 18px;overflow:auto">
        <label style="display:block;margin-top:8px;font-weight:600;font-size:0.82rem">Profile</label>
        <select id="snwProfile" style="width:100%;padding:6px;border:1px solid var(--border-light, #e5e7eb);border-radius:4px;margin-top:4px">
          ${profiles.map(p => `<option value="${this._esc(p.name)}">${this._esc(p.name)}</option>`).join('')}
        </select>
        <label style="display:block;margin-top:14px;font-weight:600;font-size:0.82rem">Starting day</label>
        <div id="snwDays" style="margin-top:6px;display:flex;flex-direction:column;gap:4px"></div>
        <label style="display:block;margin-top:14px;font-weight:600;font-size:0.82rem">Name (optional)</label>
        <input id="snwName" type="text" placeholder="leave blank for auto: {profile}-day{N}-{timestamp}"
               style="width:100%;padding:6px;border:1px solid var(--border-light, #e5e7eb);border-radius:4px;margin-top:4px;font-family:JetBrains Mono, ui-monospace, monospace;font-size:0.8rem">
        <div style="font-size:0.7rem;color:var(--text-muted);margin-top:4px">Lowercase letters/digits/-_. ≤80 chars. Must be unique.</div>
      </div>
      <div style="padding:14px 18px;border-top:1px solid var(--border-light, #e5e7eb);display:flex;justify-content:flex-end;gap:6px">
        <button class="btn btn-primary" id="snwCreateBtn">Create</button>
      </div>
    `;
    dlg.innerHTML = html;
    overlay.appendChild(dlg);
    document.body.appendChild(overlay);
    const close = () => overlay.remove();
    document.getElementById('snwCloseBtn').addEventListener('click', close);
    overlay.addEventListener('click', e => { if (e.target === overlay) close(); });

    const renderDays = (profileName) => {
      const p = profiles.find(x => x.name === profileName) || { days: [] };
      const list = document.getElementById('snwDays');
      list.innerHTML = p.days.length
        ? p.days.map(d => {
            const ready = d.snapshot_ready;
            const eventsPreview = (d.events || []).map(e => e.event).slice(0, 4).join(', ');
            const more = (d.events || []).length > 4 ? `, +${d.events.length - 4} more` : '';
            const xaiCount = d.xai_task_count || 0;
            const xaiBadge = xaiCount
              ? `<span style="background:#7c3aed;color:white;padding:1px 7px;border-radius:4px;font-size:0.68rem;font-weight:600;margin-left:6px" title="${xaiCount} cross-agent-interface task${xaiCount > 1 ? 's' : ''} available at this day">${xaiCount} XAI task${xaiCount > 1 ? 's' : ''}</span>`
              : '';
            return `
              <label class="snw-day-row" data-day="${d.day}"
                     style="display:flex;gap:8px;align-items:flex-start;padding:8px 10px;border:1px solid var(--border-light, #e5e7eb);border-radius:6px;cursor:pointer;${ready ? '' : 'opacity:0.5;cursor:not-allowed'}">
                <input type="radio" name="snwDay" value="${d.day}" ${ready ? '' : 'disabled'} style="margin-top:3px">
                <div style="flex:1;min-width:0">
                  <div style="font-weight:600;font-size:0.85rem">Day ${d.day}<span style="font-weight:400;color:var(--text-muted);margin-left:6px">${this._esc(d.date || '(no date)')}</span>${xaiBadge}</div>
                  <div style="font-size:0.78rem;color:var(--text-muted);margin-top:2px">${d.event_count} event${d.event_count === 1 ? '' : 's'}${eventsPreview ? ': ' + this._esc(eventsPreview) + this._esc(more) : ''}</div>
                  ${ready ? '' : '<div style="font-size:0.7rem;color:#b91c1c;margin-top:2px">Snapshot not pre-baked — run awm syn world</div>'}
                </div>
              </label>
            `;
          }).join('')
        : '<div style="font-size:0.78rem;color:var(--text-muted)">No days available for this profile.</div>';
    };
    document.getElementById('snwProfile').addEventListener('change', e => renderDays(e.target.value));
    renderDays(profiles[0].name);

    document.getElementById('snwCreateBtn').addEventListener('click', async () => {
      const profile = document.getElementById('snwProfile').value;
      const dayInput = overlay.querySelector('input[name="snwDay"]:checked');
      if (!dayInput) { App.toast('Pick a starting day.'); return; }
      const day = parseInt(dayInput.value, 10);
      const name = document.getElementById('snwName').value.trim() || null;
      App.toast('Creating world snapshot…');
      try {
        const res = await API.worlds.create(profile, day, name);
        close();
        App.toast(`Created ${res.id}`);
        await this._loadSidebar();
        await this.selectWorld(res.id);
      } catch (e) {
        App.toast('Create failed: ' + e.message, 8000);
      }
    });
  },

  async selectWorld(worldId) {
    this.state.activeWorld = worldId;
    App.setActiveWorld(worldId);
    const main = document.getElementById('worldsMain');
    main.innerHTML = '<div class="loading"><div class="spinner"></div> Loading world…</div>';
    try {
      const data = await API.worlds.get(worldId);
      this.renderWorldShell(main, data);
    } catch (e) {
      main.innerHTML = '<div class="card"><p>Error: ' + this._esc(e.message) + '</p></div>';
    }
    document.querySelectorAll('#worldsSidebar .world-row').forEach(r => {
      r.style.background = r.dataset.worldId === worldId ? 'var(--accent-light, #eef2ff)' : '';
    });
  },

  // Open a world on a given sub-tab (used by the `awm agent chat` deep link).
  async openWorldOnTab(worldId, subTab) {
    await this.selectWorld(worldId);
    if (subTab && subTab !== 'overview' && this._worldData) {
      this.state.subTab = subTab;
      document.querySelectorAll('#worldSubTabs .tab-btn').forEach(b =>
        b.classList.toggle('active', b.dataset.sub === subTab));
      this.loadSubTab(this._worldData);
    }
  },

  renderWorldShell(el, data) {
    this._worldData = data;
    const meta = data.meta || {};
    const dayLine = (meta.started_at_day != null)
      ? `Day ${meta.started_at_day}<span style="font-weight:400;color:var(--text-muted);margin-left:6px">${this._esc(meta.started_at_date || '')}</span>`
      : '';
    el.innerHTML = `
      <div class="card" style="margin-bottom:12px">
        <div class="card-header" style="margin-bottom:8px;display:flex;align-items:center;justify-content:space-between">
          <div>
            <span class="card-title">${this._esc(data.id)}</span>
            <span class="card-subtitle" style="margin-left:8px">${this._esc(meta.profile || '')} · ${dayLine}</span>
          </div>
          <div style="font-size:0.7rem;color:var(--text-muted)">created ${this._esc(meta.created_at || '')}</div>
        </div>
      </div>
      <div class="tab-bar" id="worldSubTabs">
        <button class="tab-btn active" data-sub="overview">Overview</button>
        <button class="tab-btn" data-sub="database">Database</button>
        <button class="tab-btn" data-sub="files">Files</button>
        <button class="tab-btn" data-sub="prompt">System Prompt</button>
        <button class="tab-btn" data-sub="agent">Agent</button>
      </div>
      <div id="worldSubContent"></div>
    `;
    document.querySelectorAll('#worldSubTabs .tab-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        this.state.subTab = btn.dataset.sub;
        document.querySelectorAll('#worldSubTabs .tab-btn').forEach(b => b.classList.toggle('active', b === btn));
        this.loadSubTab(data);
      });
    });
    this.state.subTab = 'overview';
    this.loadSubTab(data);
  },

  loadSubTab(data) {
    const el = document.getElementById('worldSubContent');
    this.state._subTabGen = (this.state._subTabGen || 0) + 1;
    switch (this.state.subTab) {
      case 'overview': this.renderOverview(el, data.id); break;
      case 'database': DatabaseView.render(el, data.id); break;
      case 'files': FilesView.render(el, data.id); break;
      case 'prompt': this.renderSystemPrompt(el, data.id); break;
      case 'agent': AgentView.render(el, data.id); break;
    }
  },

  async renderOverview(el, worldId) {
    const myGen = this.state._subTabGen;
    el.innerHTML = '<div class="loading"><div class="spinner"></div> Loading overview…</div>';
    try {
      const ov = await API.worlds.overview(worldId);
      if (myGen !== this.state._subTabGen) return;
      let html = '<div class="card" style="margin-bottom:16px">';
      html += '<div class="card-header"><span class="card-title">World Summary</span>';
      html += `<span class="badge badge-success">Day ${ov.deployed_day != null ? ov.deployed_day : '—'} horizon</span>`;
      html += '</div>';
      html += '<div class="stats-grid">';
      html += '<div class="stat-card"><div class="stat-value">' + ov.total_events_deployed + '</div><div class="stat-label">Events Materialized</div></div>';
      html += '<div class="stat-card"><div class="stat-value">' + ov.unique_event_type_count_deployed + '</div><div class="stat-label">Event Types</div></div>';
      html += '<div class="stat-card"><div class="stat-value">' + ov.total_db_rows + '</div><div class="stat-label">DB Rows</div></div>';
      html += '<div class="stat-card"><div class="stat-value">' + ov.ws_file_count + '</div><div class="stat-label">Workspace Files</div></div>';
      html += '<div class="stat-card"><div class="stat-value">' + (ov.ws_total_size_bytes / 1024).toFixed(0) + 'K</div><div class="stat-label">Workspace Size</div></div>';
      html += '<div class="stat-card"><div class="stat-value">' + ov.total_files_produced_deployed + '</div><div class="stat-label">Files Produced</div></div>';
      if (ov.xai_task_count != null && ov.xai_task_count > 0) {
        html += '<div class="stat-card" style="border-color:#c084fc"><div class="stat-value" style="color:#7c3aed">' + ov.xai_task_ready + '/' + ov.xai_task_count + '</div><div class="stat-label">XAI Tasks Ready</div></div>';
      }
      html += '</div>';
      html += '<div style="margin-top:12px"><strong style="font-size:0.85rem">DB Rows by App</strong></div>';
      html += '<div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:6px">';
      for (const [app, count] of Object.entries(ov.db_rows_by_app || {})) {
        html += '<span class="badge badge-accent" style="font-size:0.75rem;padding:3px 10px">' + this._esc(app) + ': ' + count + '</span>';
      }
      html += '</div></div>';

      el.innerHTML = html;
    } catch (e) {
      el.innerHTML = '<div class="card"><p style="color:var(--danger)">' + this._esc(e.message) + '</p></div>';
    }
  },

  async renderSystemPrompt(el, worldId) {
    const myGen = this.state._subTabGen;
    el.innerHTML = '<div class="loading"><div class="spinner"></div> Loading system prompt…</div>';
    try {
      const res = await API.files.read(worldId, 'WORLD.md');
      if (myGen !== this.state._subTabGen) return;
      const content = res.content || '';
      if (!content.trim()) {
        el.innerHTML = `
          <div class="card">
            <div class="empty-state">
              <div class="empty-title">No WORLD.md found</div>
              <p>WORLD.md is empty. Reset the world or create a new instance to regenerate it.</p>
            </div>
          </div>`;
        return;
      }
      const rendered = typeof marked !== 'undefined' ? marked.parse(content) : '<pre>' + this._esc(content) + '</pre>';
      el.innerHTML = `
        <div class="card" style="margin-bottom:16px">
          <div class="card-header" style="display:flex;justify-content:space-between;align-items:center">
            <div>
              <span class="card-title">System Prompt</span>
              <span style="color:var(--text-muted);font-size:0.78rem;margin-left:8px">WORLD.md — injected into every agent turn</span>
            </div>
            <div style="display:flex;gap:6px">
              <button class="btn btn-secondary" id="promptCopyBtn" title="Copy raw markdown">Copy</button>
              <button class="btn btn-secondary" id="promptRawBtn" title="Toggle raw markdown / rendered">Raw</button>
            </div>
          </div>
          <div id="promptBody" style="padding:12px 16px;font-size:0.85rem;line-height:1.6;max-height:70vh;overflow-y:auto">
            ${rendered}
          </div>
          <pre id="promptRaw" style="display:none;padding:12px 16px;font-size:0.78rem;white-space:pre-wrap;max-height:70vh;overflow-y:auto;background:var(--bg-muted, #f9fafb);border-top:1px solid var(--border-light, #e5e7eb)">${this._esc(content)}</pre>
        </div>`;
      const rawPre = document.getElementById('promptRaw');
      const renderedDiv = document.getElementById('promptBody');
      const rawBtn = document.getElementById('promptRawBtn');
      let showRaw = false;
      rawBtn.addEventListener('click', () => {
        showRaw = !showRaw;
        rawPre.style.display = showRaw ? 'block' : 'none';
        renderedDiv.style.display = showRaw ? 'none' : 'block';
        rawBtn.textContent = showRaw ? 'Rendered' : 'Raw';
      });
      document.getElementById('promptCopyBtn').addEventListener('click', () => {
        navigator.clipboard.writeText(content).then(() => App.toast('Copied to clipboard'));
      });
    } catch (e) {
      if (myGen !== this.state._subTabGen) return;
      el.innerHTML = `
        <div class="card">
          <div class="empty-state">
            <div class="empty-title">WORLD.md not available</div>
            <p>${this._esc(e.message)}</p>
            <p style="color:var(--text-muted);font-size:0.78rem;margin-top:8px">Create a new instance or reset this world to generate the system prompt.</p>
          </div>
        </div>`;
    }
  },
};
