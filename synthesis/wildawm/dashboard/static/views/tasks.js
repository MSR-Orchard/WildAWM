// Tasks tab: read-only browser of XAI tasks, grouped by eligible profile.
const TasksView = {
  state: { detailMode: 'task', currentTask: null },

  _esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  },

  // Collapse contiguous day ranges, e.g. "Days 4–6".
  _fmtDays(days) {
    const ds = (days || []).slice().sort((a, b) => a - b);
    if (!ds.length) return 'any day';
    const contiguous = ds.every((d, i) => i === 0 || d === ds[i - 1] + 1);
    if (ds.length >= 3 && contiguous) return `Days ${ds[0]}–${ds[ds.length - 1]}`;
    return (ds.length === 1 ? 'Day ' : 'Days ') + ds.join(', ');
  },

  _diffColor(d) {
    return { easy: '#16a34a', medium: '#d97706', hard: '#dc2626' }[d] || '#6b7280';
  },

  async load() {
    const el = document.getElementById('view-tasks');
    el.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
    try {
      const { tasks, count } = await API.xaiTasks.listGlobal();
      this._tasks = tasks;

      // A task can be eligible for several profiles, so it may appear under each.
      const byProfile = {};
      let pairCount = 0;
      for (const t of tasks) {
        for (const rule of (t.eligible || [])) {
          const prof = rule.profile || '(unscoped)';
          (byProfile[prof] = byProfile[prof] || []).push({ task: t, rule });
          pairCount++;
        }
        if (!(t.eligible || []).length) {
          (byProfile['(unscoped)'] = byProfile['(unscoped)'] || []).push({ task: t, rule: null });
        }
      }
      const profiles = Object.keys(byProfile).sort();

      el.innerHTML = `
        <div class="card" style="margin-bottom:12px">
          <div class="card-header">
            <span class="card-title">Cross-Agent-Interface (XAI) Tasks</span>
            <span class="card-subtitle">${count} task${count === 1 ? '' : 's'} · ${pairCount} profile/day slot${pairCount === 1 ? '' : 's'}</span>
          </div>
          <div style="padding:4px 4px 2px;color:var(--text-muted);font-size:0.82rem">
            Each task spans multiple agent interfaces and is only valid on specific
            profile days (when the world state makes it doable). Source:
            accepted tasks under <code>tasks_xai/</code> — read-only.
          </div>
        </div>
        <div class="layout">
          <div class="card" style="padding:0">
            <div class="card-header" style="padding:12px 16px">
              <span class="card-title">By profile</span>
            </div>
            <div class="sidebar-list" id="xaiTaskList" style="max-height:calc(100vh - 240px);overflow-y:auto"></div>
          </div>
          <div id="xaiTaskDetail">
            <div class="empty-state"><div class="empty-title">Select a task</div>
              <div style="color:var(--text-muted);font-size:0.84rem;margin-top:6px">Pick a task to see which days it applies to, its interfaces, and its rubric.</div>
            </div>
          </div>
        </div>`;

      const list = document.getElementById('xaiTaskList');
      if (!profiles.length) {
        list.innerHTML = '<div style="padding:16px;color:var(--text-muted)">No XAI tasks in the registry.</div>';
        return;
      }
      for (const prof of profiles) {
        const rows = byProfile[prof].sort((a, b) =>
          a.task.title.localeCompare(b.task.title));
        const header = document.createElement('div');
        header.style.cssText = 'padding:10px 14px 4px;font-weight:600;font-size:0.74rem;color:#7c3aed;text-transform:uppercase;letter-spacing:0.5px';
        header.textContent = `${prof} · ${rows.length}`;
        list.appendChild(header);

        for (const { task, rule } of rows) {
          const days = rule ? this._fmtDays(rule.days) : 'any day';
          const item = document.createElement('button');
          item.className = 'sidebar-item';
          item.dataset.key = prof + '/' + task.task_name;
          const dc = this._diffColor(task.difficulty);
          const warn = task.has_assets ? ''
            : '<span title="task.md, verifier/ or golden.py missing on disk" style="color:var(--danger);margin-left:4px">⚠</span>';
          item.innerHTML =
            '<div style="flex:1;min-width:0">' +
              '<div style="font-size:0.86rem;font-weight:500">' + this._esc(task.title) + warn + '</div>' +
              '<div class="item-meta" style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:3px">' +
                '<span style="background:#ede9fe;color:#6d28d9;padding:1px 7px;border-radius:4px;font-size:0.7rem;font-weight:600">' + days + '</span>' +
                '<span style="color:' + dc + ';font-size:0.7rem;font-weight:600">' + this._esc(task.difficulty) + '</span>' +
                '<span style="font-family:var(--mono);font-size:0.68rem;color:var(--text-muted)">' + (task.interfaces || []).length + ' iface</span>' +
              '</div>' +
            '</div>';
          item.addEventListener('click', () => this.showTask(prof, task.task_name, rule));
          list.appendChild(item);
        }
      }
    } catch (e) {
      el.innerHTML = '<p style="color:var(--danger);padding:16px">' + this._esc(e.message) + '</p>';
    }
  },

  async showTask(profile, taskName, rule) {
    document.querySelectorAll('#xaiTaskList .sidebar-item').forEach(i =>
      i.classList.toggle('active', i.dataset.key === profile + '/' + taskName));
    const detail = document.getElementById('xaiTaskDetail');
    detail.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
    this.state.currentTask = taskName;
    this.state.detailMode = 'task';
    try {
      const t = await API.xaiTasks.getGlobal(taskName);
      this._detailData = t;
      detail.innerHTML = this._renderDetail(t, profile);
      this._bindDetail(t);
    } catch (e) {
      detail.innerHTML = '<p style="color:var(--danger);padding:16px">' + this._esc(e.message) + '</p>';
    }
  },

  _renderDetail(t, focusProfile) {
    const dc = this._diffColor(t.difficulty);
    const ifaceChips = (t.interfaces || []).map(i =>
      '<span style="background:#f1f5f9;border:1px solid var(--border);color:#334155;padding:2px 9px;border-radius:5px;font-size:0.74rem;font-family:var(--mono)">' + this._esc(i) + '</span>'
    ).join(' ');
    const checkChips = (t.verification_checks || []).map(c =>
      '<span style="background:#ecfdf5;border:1px solid #a7f3d0;color:#047857;padding:2px 9px;border-radius:5px;font-size:0.72rem;font-family:var(--mono)">' + this._esc(c) + '</span>'
    ).join(' ');

    let avail = '';
    for (const r of (t.eligible || [])) {
      const isFocus = r.profile === focusProfile;
      avail +=
        '<tr' + (isFocus ? ' style="background:#faf5ff"' : '') + '>' +
          '<td style="padding:6px 10px;font-weight:' + (isFocus ? '700' : '500') + '">' + this._esc(r.profile || '—') + '</td>' +
          '<td style="padding:6px 10px"><span style="background:#ede9fe;color:#6d28d9;padding:1px 8px;border-radius:4px;font-size:0.74rem;font-weight:600">' + this._fmtDays(r.days) + '</span></td>' +
          '<td style="padding:6px 10px;color:var(--text-muted);font-size:0.8rem">' + this._esc(r.notes || '') + '</td>' +
        '</tr>';
    }
    if (!avail) avail = '<tr><td colspan="3" style="padding:6px 10px;color:var(--text-muted)">Not scoped to a profile/day.</td></tr>';

    const filesRows = Object.entries(t.files || {}).map(([k, v]) =>
      '<div style="font-family:var(--mono);font-size:0.74rem;color:var(--text-muted)"><span style="color:#7c3aed">' + this._esc(k) + '</span>: ' + this._esc(v) + '</div>'
    ).join('');

    const steps = (t.estimated_steps != null)
      ? '<span style="color:var(--text-muted);font-size:0.8rem;margin-left:10px">~' + this._esc(t.estimated_steps) + ' steps</span>' : '';
    // Warn only when the backend explicitly reports missing assets.
    const assetWarn = t.has_assets === false
      ? '<div style="margin:10px 0;padding:8px 12px;background:#fef2f2;border:1px solid #fecaca;border-radius:6px;color:#b91c1c;font-size:0.8rem">⚠ One or more task assets (task.md / verifier/ / golden.py) are missing on disk.</div>'
      : '';

    let html = '<div class="card">';
    html += '<div class="card-header" style="align-items:flex-start">' +
      '<div><span class="card-title">' + this._esc(t.title) + '</span>' +
      '<div style="font-family:var(--mono);font-size:0.72rem;color:var(--text-muted);margin-top:2px">' + this._esc(t.task_name) + '</div></div>' +
      '<span style="color:#fff;background:' + dc + ';padding:2px 10px;border-radius:5px;font-size:0.74rem;font-weight:600">' + this._esc(t.difficulty) + '</span>' +
      '</div>';
    html += '<div style="padding:0 4px"><p style="color:var(--text-secondary);font-size:0.88rem;margin:6px 0">' + this._esc(t.description) + steps + '</p>';

    html += '<div style="margin-top:12px"><div style="font-size:0.72rem;font-weight:700;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:5px">Available for</div>' +
      '<table style="width:100%;border-collapse:collapse;font-size:0.85rem"><thead><tr style="text-align:left;color:var(--text-muted);border-bottom:1px solid var(--border)">' +
      '<th style="padding:4px 10px">Profile</th><th style="padding:4px 10px">Days</th><th style="padding:4px 10px">Why these days</th></tr></thead><tbody>' +
      avail + '</tbody></table></div>';

    html += '<div style="margin-top:14px"><div style="font-size:0.72rem;font-weight:700;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:5px">Interfaces</div>' +
      '<div style="display:flex;gap:6px;flex-wrap:wrap">' + (ifaceChips || '<span style="color:var(--text-muted)">—</span>') + '</div>';
    if (t.interface_flow) {
      html += '<div style="margin-top:8px;font-family:var(--mono);font-size:0.76rem;color:#475569;background:#f8fafc;border:1px solid var(--border);border-radius:6px;padding:8px 10px">' + this._esc(t.interface_flow) + '</div>';
    }
    html += '</div>';

    if (checkChips) {
      html += '<div style="margin-top:14px"><div style="font-size:0.72rem;font-weight:700;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:5px">Verification checks</div>' +
        '<div style="display:flex;gap:6px;flex-wrap:wrap">' + checkChips + '</div></div>';
    }
    if (filesRows) {
      html += '<div style="margin-top:14px"><div style="font-size:0.72rem;font-weight:700;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:5px">Files</div>' + filesRows + '</div>';
    }
    html += assetWarn;
    html += '</div></div>';

    const hasSkill = t.skill_md != null;
    html += '<div class="card" style="margin-top:12px"><div class="tab-bar" id="xaiDetailTabs">' +
      '<button class="tab-btn active" data-dmode="task">task.md</button>' +
      '<button class="tab-btn" data-dmode="judge">rubrics.md</button>' +
      (hasSkill ? '<button class="tab-btn" data-dmode="skill">verify.skill.md</button>' : '') +
      '</div><div id="xaiDetailAsset"></div></div>';
    return html;
  },

  _bindDetail(t) {
    const render = (mode) => {
      this.state.detailMode = mode;
      const box = document.getElementById('xaiDetailAsset');
      if (!box) return;
      const md = mode === 'judge' ? t.judge_md : mode === 'skill' ? t.skill_md : t.task_md;
      box.innerHTML = '<div class="md-content" style="padding:4px 8px">' +
        (md != null ? App.renderMarkdown(md) : '<em style="color:var(--text-muted)">(not present)</em>') + '</div>';
    };
    document.querySelectorAll('#xaiDetailTabs .tab-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('#xaiDetailTabs .tab-btn').forEach(b => b.classList.toggle('active', b === btn));
        render(btn.dataset.dmode);
      });
    });
    render('task');
  },
};
