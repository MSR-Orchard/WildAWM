// Agent tab: world runtime, per-harness chat, XAI tasks, and the verify panel.
const AgentView = {
  state: {
    history: {},        // { worldId: [history items from server] }
    pending: false,
    today: {},          // { worldId: {current_date, current_day} }
    xaiTasks: {},       // { worldId: [XAI task rows eligible for this world] }
    activeEpisode: {},  // { worldId: {event, episode_id, is_xai?} }
    lastVerdict: {},    // { worldId+":"+episode_id: verdict response }
    // Harness the chat is driving; each keeps its own conversation.
    activeKind: {},
    // Cached model registry and the per-(world,kind) model pick (a switch re-attaches).
    models: null,
    selectedModel: {},
  },

  KINDS: ['nanobot', 'opencode', 'claude-code', 'codex', 'hermes', 'openclaw'],

  _sessionId(worldId, kind) { return `${worldId}:${kind}`; },

  // Explicit selection, else the first running agent, else nanobot.
  _activeKind(worldId, runtime) {
    const agents = (runtime && runtime.agents) || {};
    const sel = this.state.activeKind[worldId];
    if (sel && agents[sel]) return sel;
    const firstRunning = this.KINDS.find(k => agents[k] && agents[k].running);
    return firstRunning || sel || 'nanobot';
  },

  async render(el, worldId) {
    el.innerHTML = '<div class="card" id="agentCard"><div class="loading"><div class="spinner"></div> Checking runtime...</div></div>';
    await this.refresh(worldId);
  },

  async refresh(worldId) {
    if (!this.state.models) {
      try { this.state.models = await API.models.list(); }
      catch { this.state.models = { providers: [], default: null }; }
    }
    let runtime;
    try {
      runtime = await API.runtime.status(worldId);
    } catch (e) {
      const card = document.getElementById('agentCard');
      if (card) card.innerHTML = '<div class="empty-state"><div class="empty-title">Runtime check failed</div><p>' + this._esc(e.message) + '</p></div>';
      return;
    }
    if (runtime.running) {
      try {
        this.state.today[worldId] = await API.worlds.today(worldId);
      } catch { this.state.today[worldId] = {}; }
      try {
        const xaiRes = await API.xaiTasks.list(worldId);
        this.state.xaiTasks[worldId] = xaiRes.tasks || [];
      } catch { this.state.xaiTasks[worldId] = []; }
      // Restore an in-flight server episode so the Verify panel survives a reload.
      if (!this.state.activeEpisode[worldId]) {
        try {
          const res = await API.tasks.activeEpisode(worldId);
          if (res && res.active) {
            this.state.activeEpisode[worldId] = {
              event: res.active.event,
              episode_id: res.active.episode_id,
            };
          }
        } catch { /* tolerate; just don't restore */ }
      }
    }
    this._renderShell(worldId, runtime);
  },

  _esc(s) {
    return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  },

  _formatJSON(s) {
    try { return JSON.stringify(JSON.parse(s), null, 2); }
    catch { return s || ''; }
  },

  _renderShell(worldId, runtime) {
    const card = document.getElementById('agentCard');
    if (!card) return;
    // `running` means the world environment is up; agents are tracked in runtime.agents.
    const running = !!(runtime.running || runtime.env_running);
    const whPort = runtime.webharbor_port;
    const whState = runtime.webharbor_state;
    const whBadge = whPort
      ? ` <span class="badge ${whState === 'running' ? 'badge-ok' : 'badge-muted'}" title="WebHarbor control plane (15 web sites: www.amazon.com, github.com, arxiv.org, ...). Click 'Reset web' to wipe all 15 site DBs.">web:${whPort}</span>`
      : '';
    const envBadges = running
      ? `<span class="badge badge-ok">mcp:${runtime.mcp_port}</span>${whBadge}`
      : '<span class="badge badge-muted">stopped</span>';

    const activeKind = this._activeKind(worldId, runtime);
    if (running) this.state.activeKind[worldId] = activeKind;

    const pendingWh = (this.state.pendingWebharbor || {})[worldId];
    const whChecked = pendingWh === undefined ? true : pendingWh;
    const whToggle = running ? '' : `
      <label id="agentWebharborLabel" style="display:inline-flex;align-items:center;gap:4px;font-size:0.78rem;color:var(--text-muted, #4b5563);padding:0 6px;cursor:pointer"
             title="Bring up WebHarbor sidecar — 15 web GUI mirrors (www.amazon.com, github.com, arxiv.org, etc.) reachable from the agent over HTTPS via docker DNS. Adds ~10s to startup; ~3GB image.">
        <input type="checkbox" id="agentWebharborToggle" ${whChecked ? 'checked' : ''}> web
      </label>`;

    card.innerHTML = `
      <div class="card-header" style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
        <div>
          <span class="card-title">Agent${running ? ` — ${this._esc(activeKind)}` : ''}</span>
          <span class="card-subtitle" style="margin-left:8px">${envBadges}</span>
        </div>
        <div style="display:flex;gap:6px;align-items:center">
          ${whToggle}
          ${running
            ? '<button class="btn btn-secondary" id="agentStopBtn" title="Tear down the whole world — env + every attached agent.">Stop world</button>'
            : '<button class="btn btn-primary" id="agentStartBtn" title="Bring up the world environment (mcp [+ webharbor]). Pick an agent to chat with afterwards.">Start world</button>'}
          <button class="btn btn-secondary" id="agentResetBtn"
                  style="border-color:#fca5a5;color:#b91c1c"
                  title="Wipe all agent activity and rebuild the world from its day snapshot. Erases sessions, MEMORY, agent-written files, and DB modifications.">Reset world</button>
          ${(running && whPort) ? `<button class="btn btn-secondary" id="agentWebharborResetBtn"
                  style="border-color:#fcd34d;color:#92400e"
                  title="Reset every WebHarbor site's DB back to its instance_seed/ baseline (sub-second). Agent's accounts, orders, repos etc. on the 15 web mirrors get wiped.">Reset web</button>` : ''}
          <button class="btn btn-secondary" id="agentRefreshBtn">Refresh</button>
        </div>
      </div>
      ${running ? this._renderAgentBar(worldId, runtime, activeKind) : ''}
      ${running ? this._renderToday(worldId) : ''}
      ${running ? this._renderXaiTasks(worldId) : ''}
      ${(running && this.state.activeEpisode[worldId]) ? this._renderActiveEpisode(worldId) : ''}
      <div id="agentBody"></div>
    `;

    document.getElementById('agentRefreshBtn').addEventListener('click', () => this.refresh(worldId));
    document.getElementById('agentResetBtn').addEventListener('click', () => this.resetWorld(worldId));
    const whTog = document.getElementById('agentWebharborToggle');
    if (whTog) {
      this.state.pendingWebharbor = this.state.pendingWebharbor || {};
      whTog.addEventListener('change', () => {
        this.state.pendingWebharbor[worldId] = whTog.checked;
      });
    }
    const whResetBtn = document.getElementById('agentWebharborResetBtn');
    if (whResetBtn) {
      whResetBtn.addEventListener('click', () => this.resetWebharbor(worldId));
    }
    if (running) {
      document.getElementById('agentStopBtn').addEventListener('click', () => this.stopWorld(worldId));
      this._wireAgentBar(worldId, runtime);
      this._wireXaiTasks(worldId);
      this._wireActiveEpisode(worldId);
      this._renderChat(worldId);
    } else {
      document.getElementById('agentStartBtn').addEventListener('click', () => this.startWorld(worldId));
      this._renderEmptyState();
    }
  },

  // One chip per harness; agents attach lazily on the first message.
  _renderAgentBar(worldId, runtime, activeKind) {
    const agents = runtime.agents || {};
    const chips = this.KINDS.map(kind => {
      const a = agents[kind] || {};
      const isRunning = !!a.running;
      const isActive = kind === activeKind;
      const dot = `<span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:${isRunning ? '#16a34a' : '#9ca3af'};margin-right:5px"></span>`;
      const portTxt = (isRunning && a.agent_port) ? `:${a.agent_port}` : '';
      const detach = isRunning
        ? `<span class="agent-detach" data-kind="${this._esc(kind)}" title="Detach ${this._esc(kind)} (stop just this agent container)" style="margin-left:6px;color:#b91c1c;font-weight:700;cursor:pointer">×</span>`
        : '';
      return `
        <button class="agent-chip" data-kind="${this._esc(kind)}"
                style="display:inline-flex;align-items:center;padding:4px 10px;border-radius:14px;font-size:0.78rem;cursor:pointer;
                       border:1px solid ${isActive ? '#6366f1' : 'var(--border-light, #e5e7eb)'};
                       background:${isActive ? '#eef2ff' : 'white'};
                       color:${isActive ? '#3730a3' : 'var(--text, #374151)'};font-weight:${isActive ? 600 : 400}">
          ${dot}${this._esc(kind)}${portTxt}${detach}
        </button>`;
    }).join('');
    return `
      <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:12px;padding:8px;background:var(--bg-muted,#fafafa);border:1px solid var(--border-light,#e5e7eb);border-radius:6px">
        <span style="font-size:0.72rem;color:var(--text-muted);margin-right:2px">Chat with:</span>
        ${chips}
        ${this._renderModelSelect(worldId, runtime, activeKind)}
        <span style="font-size:0.68rem;color:var(--text-muted);margin-left:auto">Agents share this world · separate conversations</span>
      </div>`;
  },

  // Model picker for the active agent; a switch re-attaches it on the next send.
  _renderModelSelect(worldId, runtime, activeKind) {
    const reg = this.state.models || { providers: [], default: null };
    if (!reg.providers || !reg.providers.length) return '';
    const agents = runtime.agents || {};
    const runningModel = (agents[activeKind] || {}).model;  // ref it's on now (or null)
    const key = worldId + ':' + activeKind;
    const selected = this.state.selectedModel[key] || runningModel || reg.default || '';
    const groups = reg.providers.map(p => {
      const opts = (p.models || []).map(m =>
        `<option value="${this._esc(m.ref)}"${m.ref === selected ? ' selected' : ''}>${this._esc(m.name)}</option>`
      ).join('');
      return `<optgroup label="${this._esc(p.provider)} · ${this._esc(p.provider_type)}">${opts}</optgroup>`;
    }).join('');
    const runTxt = runningModel
      ? `<span style="font-size:0.66rem;color:#16a34a" title="${this._esc(activeKind)} is currently attached on this model">▶ ${this._esc(runningModel)}</span>`
      : `<span style="font-size:0.66rem;color:var(--text-muted)">not attached</span>`;
    return `
      <span style="font-size:0.72rem;color:var(--text-muted);margin-left:8px">Model:</span>
      <select id="agentModelSelect" title="Model for ${this._esc(activeKind)}. Switching re-attaches the agent on next send."
              style="font-size:0.76rem;padding:3px 6px;border:1px solid var(--border-light,#e5e7eb);border-radius:6px;max-width:230px">${groups}</select>
      ${runTxt}`;
  },

  _wireAgentBar(worldId, runtime) {
    const sel = document.getElementById('agentModelSelect');
    if (sel) {
      sel.addEventListener('change', () => {
        const kind = this._activeKind(worldId, runtime);
        this.state.selectedModel[worldId + ':' + kind] = sel.value;
      });
    }
    document.querySelectorAll('#agentCard .agent-chip').forEach(chip => {
      chip.addEventListener('click', (e) => {
        if (e.target.closest('.agent-detach')) return;  // handled below
        const kind = chip.dataset.kind;
        if (this.state.activeKind[worldId] === kind) return;
        this.state.activeKind[worldId] = kind;
        this._renderShell(worldId, runtime);
      });
    });
    document.querySelectorAll('#agentCard .agent-detach').forEach(x => {
      x.addEventListener('click', async (e) => {
        e.stopPropagation();
        const kind = x.dataset.kind;
        if (!confirm(`Detach the ${kind} agent? Its container stops; the world and other agents stay up.`)) return;
        App.toast(`Detaching ${kind}…`);
        try { await API.runtime.stopAgent(worldId, kind); App.toast(`${kind} detached`); }
        catch (err) { App.toast('Detach failed: ' + err.message, 6000); }
        await this.refresh(worldId);
      });
    });
  },

  _renderEmptyState() {
    const body = document.getElementById('agentBody');
    if (!body) return;
    body.innerHTML = `
      <div class="empty-state">
        <div class="empty-title">World is not running</div>
        <p>Click "Start world" to bring up the world environment (mcp [+ webharbor]). Then pick any agent — nanobot, opencode, claude-code, codex, hermes, openclaw — to chat with. They share the world and run concurrently.</p>
      </div>
    `;
  },

  _renderToday(worldId) {
    const today = this.state.today[worldId] || {};
    if (today.current_date == null) {
      return '';
    }

    const startedDay = today.started_at_day;
    const startedDate = today.started_at_date;
    const subtitle = (startedDay != null)
      ? `morning after Day ${startedDay}${startedDate ? ` (events through ${this._esc(startedDate)})` : ''}`
      : '';

    return `
      <div style="margin-bottom:12px;padding:10px;background:#f5f7ff;border:1px solid #c7d2fe;border-radius:6px">
        <div style="font-size:0.85rem;font-weight:600">
          🕘 Frozen clock: <code style="background:#fff;padding:2px 6px;border-radius:3px;font-size:0.78rem">${this._esc(today.current_date)} ${this._esc((today.current_time || '').slice(0,5))}</code>
          ${subtitle ? `<span style="color:var(--text-muted);font-weight:400;margin-left:6px">${subtitle}</span>` : ''}
        </div>
      </div>`;
  },

  _renderXaiTasks(worldId) {
    const tasks = this.state.xaiTasks[worldId] || [];
    if (!tasks.length) return '';
    const ready = tasks.filter(t => t.has_assets);
    if (!ready.length) return '';

    const iface_icon = (iface) => {
      if (iface.startsWith('web')) return '🌐';
      if (iface === 'cli') return '💻';
      if (iface === 'email') return '📧';
      if (iface === 'slack') return '💬';
      if (iface === 'calendar') return '📅';
      return '🔗';
    };

    const diff_color = (d) => {
      if (d === 'easy') return '#16a34a';
      if (d === 'medium') return '#ca8a04';
      if (d === 'hard') return '#dc2626';
      return '#6b7280';
    };

    const rowsHtml = ready.map(t => {
      const ifaceBadges = (t.interfaces || []).map(i =>
        `<span style="background:#e0f2fe;color:#075985;padding:1px 6px;border-radius:3px;font-size:0.68rem;font-family:JetBrains Mono, monospace">${iface_icon(i)} ${this._esc(i)}</span>`
      ).join(' ');
      return `
        <div class="xai-task-row" data-task="${this._esc(t.task_name)}"
             style="display:flex;justify-content:space-between;align-items:center;padding:8px 10px;background:white;border:1px solid #c084fc;border-radius:5px">
          <div style="flex:1;min-width:0">
            <div style="font-family:JetBrains Mono, ui-monospace, monospace;font-size:0.78rem;color:#7c3aed;font-weight:600">${this._esc(t.title)}</div>
            <div style="display:flex;gap:4px;align-items:center;margin-top:4px;flex-wrap:wrap">
              <span style="background:${diff_color(t.difficulty)};color:white;padding:1px 6px;border-radius:3px;font-size:0.68rem">${this._esc(t.difficulty)}</span>
              ${ifaceBadges}
            </div>
            <div style="font-size:0.68rem;color:var(--text-muted);margin-top:2px">${this._esc(t.interface_flow || '')}</div>
          </div>
          <div style="display:flex;gap:4px;margin-left:8px;flex-shrink:0">
            <button class="btn btn-secondary xai-view-btn" data-task="${this._esc(t.task_name)}">View</button>
            <button class="btn btn-primary xai-send-btn" data-task="${this._esc(t.task_name)}"
                    style="background:#7c3aed;border-color:#7c3aed"
                    title="Snapshot world + load cross-interface task into chat">Use</button>
          </div>
        </div>`;
    }).join('');

    return `
      <div style="margin-bottom:12px;padding:10px;background:#faf5ff;border:1px solid #c084fc;border-radius:6px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
          <div style="font-size:0.85rem;font-weight:600;color:#7c3aed">
            Cross-Agent-Interface Tasks
            <span style="font-weight:400;color:var(--text-muted);font-size:0.78rem;margin-left:6px">${ready.length} available</span>
          </div>
          <span style="font-size:0.68rem;color:var(--text-muted);background:#ede9fe;padding:2px 8px;border-radius:3px">Web + CLI + MCP</span>
        </div>
        <div style="display:flex;flex-direction:column;gap:4px">${rowsHtml}</div>
      </div>`;
  },

  _wireXaiTasks(worldId) {
    document.querySelectorAll('#agentCard .xai-view-btn').forEach(btn => {
      btn.addEventListener('click', () => this._viewXaiTask(worldId, btn.dataset.task));
    });
    document.querySelectorAll('#agentCard .xai-send-btn').forEach(btn => {
      btn.addEventListener('click', () => this._sendXaiTask(worldId, btn.dataset.task));
    });
  },

  async _viewXaiTask(worldId, taskName) {
    let task;
    try { task = await API.xaiTasks.get(worldId, taskName); }
    catch (e) { App.toast('Load failed: ' + e.message); return; }

    const overlay = document.createElement('div');
    overlay.id = 'taskModal';
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:9999;display:flex;align-items:center;justify-content:center;padding:20px';
    const modal = document.createElement('div');
    modal.style.cssText = 'background:white;border-radius:8px;max-width:1100px;width:100%;max-height:92vh;display:flex;flex-direction:column;overflow:hidden';
    const close = () => overlay.remove();
    modal.innerHTML = `
      <div style="padding:14px 18px;border-bottom:1px solid #c084fc;display:flex;justify-content:space-between;align-items:center;background:#faf5ff">
        <div>
          <div style="font-weight:600;font-family:JetBrains Mono, monospace;color:#7c3aed">${this._esc(taskName)}</div>
          <div style="font-size:0.78rem;color:var(--text-muted)">Cross-Agent-Interface Task</div>
        </div>
        <button class="btn btn-secondary" id="taskModalCloseBtn">Close</button>
      </div>
      <div style="overflow:auto;padding:14px 18px">
        <details open>
          <summary style="cursor:pointer;font-weight:600;margin:6px 0">task.md <span style="font-weight:400;color:var(--text-muted)">— what the agent sees</span></summary>
          ${App.withCopy(`<pre style="background:#fafafa;border:1px solid #e5e7eb;border-radius:6px;padding:8px;white-space:pre-wrap;font-size:0.85rem;font-family:DM Sans, sans-serif">${this._esc(task.task_md || '')}</pre>`)}
        </details>
        <details>
          <summary style="cursor:pointer;font-weight:600;margin:10px 0 6px">rubrics.md <span style="font-weight:400;color:var(--text-muted)">— verification rubric</span></summary>
          ${App.withCopy(`<pre style="background:#fafafa;border:1px solid #e5e7eb;border-radius:6px;padding:8px;white-space:pre-wrap;font-size:0.82rem">${this._esc(task.judge_md || '')}</pre>`)}
        </details>
        <details>
          <summary style="cursor:pointer;font-weight:600;margin:10px 0 6px">verify.skill.md <span style="font-weight:400;color:var(--text-muted)">— verification skill</span></summary>
          ${App.withCopy(`<pre style="background:#fafafa;border:1px solid #e5e7eb;border-radius:6px;padding:8px;white-space:pre-wrap;font-size:0.82rem">${this._esc(task.skill_md || '')}</pre>`)}
        </details>
      </div>
    `;
    overlay.appendChild(modal);
    overlay.addEventListener('click', (ev) => { if (ev.target === overlay) close(); });
    document.body.appendChild(overlay);
    document.getElementById('taskModalCloseBtn').addEventListener('click', close);
  },

  async _sendXaiTask(worldId, taskName) {
    if (this.state.pending) {
      App.toast('Agent is still busy — wait for the current turn to finish.');
      return;
    }
    const prev = this.state.activeEpisode[worldId];
    if (prev && prev.episode_id) {
      API.tasks.endEpisode(worldId, prev.episode_id).catch(() => {});
    }
    let ep;
    try { ep = await API.xaiTasks.startEpisode(worldId, taskName); }
    catch (e) { App.toast('Start XAI episode failed: ' + e.message, 6000); return; }
    this.state.activeEpisode[worldId] = {
      event: taskName, episode_id: ep.episode_id, is_xai: true,
    };
    delete this.state.lastVerdict[`${worldId}:${ep.episode_id}`];
    this._renderShell(
      worldId,
      await API.runtime.status(worldId).catch(() => ({ running: true })),
    );
    const input = document.getElementById('agentInput');
    if (input) {
      input.value = ep.task_md;
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
      input.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
    App.toast('XAI task loaded — review/edit, then click Send.', 4000);
  },

  _renderActiveEpisode(worldId) {
    const ep = this.state.activeEpisode[worldId];
    const verdict = this.state.lastVerdict[`${worldId}:${ep.episode_id}`];
    const verdictPanel = verdict ? this._renderVerdictPanel(verdict) : '';
    const isXai = !!ep.is_xai;
    const borderColor = isXai ? '#7c3aed' : '#4F46E5';
    const bgColor = isXai ? '#faf5ff' : '#eef2ff';
    const badge = isXai
      ? '<span style="background:#7c3aed;color:white;padding:1px 6px;border-radius:3px;font-size:0.68rem;margin-right:6px">XAI</span>'
      : '';
    return `
      <div style="margin-bottom:12px;padding:10px;border:2px solid ${borderColor};border-radius:6px;background:${bgColor}">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <div>
            <strong>Active episode:</strong>
            ${badge}
            <code style="background:#fff;padding:2px 6px;border-radius:4px;font-size:0.78rem">${this._esc(ep.event)}</code>
            <span style="font-size:0.7rem;color:var(--text-muted);margin-left:6px">${this._esc(ep.episode_id)}</span>
          </div>
          <div style="display:flex;gap:6px">
            <button class="btn btn-primary" id="verifyLightBtn" title="Run the task verifier plus a single LLM judge call">Verify (lightweight)</button>
            <button class="btn btn-secondary" id="verifyHeavyBtn" title="Spawn the configured heavy judge harness to explore the snapshots directly (5–10 min)">Verify (heavy)</button>
            <button class="btn btn-secondary" id="verifyCancelBtn">End</button>
          </div>
        </div>
        <div style="margin-top:6px;font-size:0.78rem;color:var(--text-muted)">
          Task sent. Watch the agent's reply below, then click Verify when it's done.
        </div>
        ${verdictPanel}
      </div>
    `;
  },

  _wireActiveEpisode(worldId) {
    if (!this.state.activeEpisode[worldId]) return;
    const lv = document.getElementById('verifyLightBtn');
    const hv = document.getElementById('verifyHeavyBtn');
    const cancel = document.getElementById('verifyCancelBtn');
    if (lv) lv.addEventListener('click', () => this._verify(worldId, 'lightweight'));
    if (hv) hv.addEventListener('click', () => this._verify(worldId, 'heavy'));
    if (cancel) cancel.addEventListener('click', async () => {
      const ep = this.state.activeEpisode[worldId];
      delete this.state.activeEpisode[worldId];
      // Tell the server too, so a reload does not resurrect this episode.
      if (ep && ep.episode_id) {
        try { await API.tasks.endEpisode(worldId, ep.episode_id); }
        catch (e) { App.toast('End episode failed on server: ' + e.message, 6000); }
      }
      this._renderShell(worldId, { running: true, mcp_port: '?' });
      this.refresh(worldId);
    });
  },

  _formatDuration(secs) {
    if (secs == null) return '';
    if (secs < 60) return `${secs.toFixed(1)}s`;
    const m = Math.floor(secs / 60);
    const s = Math.round(secs - m * 60);
    return `${m}m ${s}s`;
  },

  _formatTokens(n) {
    if (n == null) return '—';
    if (n >= 1000) return `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k`;
    return String(n);
  },

  _chip(text, title, bg = '#f1f5f9', color = '#334155') {
    return `<span title="${this._esc(title || '')}" style="background:${bg};color:${color};padding:3px 8px;border-radius:4px;font-size:0.72rem;font-family:JetBrains Mono, ui-monospace, monospace;line-height:1.1">${text}</span>`;
  },

  // Metadata chips next to the verdict (elapsed, tokens, turns).
  _renderMetaChips(v) {
    const meta = v.meta || {};
    const chips = [];
    if (meta.elapsed_s != null) {
      chips.push(this._chip(
        `⏱ ${this._formatDuration(meta.elapsed_s)}`,
        `Wall-clock time spent on the verify call (${meta.elapsed_s}s)`,
        '#fef3c7', '#92400e',
      ));
    }
    if (meta.input_tokens != null || meta.output_tokens != null) {
      const inn = this._formatTokens(meta.input_tokens);
      const out = this._formatTokens(meta.output_tokens);
      const tipBits = [];
      if (meta.input_tokens != null) tipBits.push(`input=${meta.input_tokens}`);
      if (meta.output_tokens != null) tipBits.push(`output=${meta.output_tokens}`);
      if (meta.cache_read_input_tokens) tipBits.push(`cache_read=${meta.cache_read_input_tokens}`);
      if (meta.cache_creation_input_tokens) tipBits.push(`cache_create=${meta.cache_creation_input_tokens}`);
      chips.push(this._chip(
        `📥 ${inn} in / 📤 ${out} out`,
        tipBits.join(', '),
        '#e0f2fe', '#075985',
      ));
    }
    if (meta.mode === 'heavy' && meta.turns != null) {
      chips.push(this._chip(
        `🔁 ${meta.turns} turn${meta.turns === 1 ? '' : 's'}`,
        'Number of conversation turns the heavy judge agent took',
        '#ede9fe', '#5b21b6',
      ));
    }
    if (meta.total_cost_usd != null) {
      chips.push(this._chip(
        `$${meta.total_cost_usd.toFixed(meta.total_cost_usd < 0.1 ? 4 : 2)}`,
        `Estimated USD cost reported by claude (${meta.total_cost_usd})`,
        '#dcfce7', '#166534',
      ));
    }
    return chips.length ? `<span style="display:inline-flex;gap:4px;align-items:center;flex-wrap:wrap;margin-left:6px">${chips.join('')}</span>` : '';
  },

  _renderVerdictPanel(v) {
    const verdict = v.verdict || {};
    const cls = verdict.verdict || 'unknown';
    const colors = {
      completed: '#16a34a',
      partially_completed: '#ca8a04',
      agent_fail: '#dc2626',
      world_imperfect: '#7c3aed',
      unknown: '#6b7280',
    };
    const color = colors[cls] || colors.unknown;
    const conf = verdict.confidence || {};
    const confBars = ['completed', 'partially_completed', 'agent_fail', 'world_imperfect']
      .map(k => {
        const pct = Math.round((conf[k] || 0) * 100);
        const isPicked = k === cls;
        return `
          <div style="display:flex;align-items:center;gap:6px;font-size:0.75rem">
            <div style="width:160px;font-family:JetBrains Mono, monospace;color:${isPicked ? color : '#6b7280'};font-weight:${isPicked ? '600' : '400'}">${k}</div>
            <div style="flex:1;background:#e5e7eb;border-radius:4px;height:8px;overflow:hidden">
              <div style="background:${isPicked ? color : '#9ca3af'};height:100%;width:${pct}%"></div>
            </div>
            <div style="width:40px;text-align:right;font-variant-numeric:tabular-nums">${pct}%</div>
          </div>`;
      }).join('');

    const metaChips = this._renderMetaChips(v);

    const modeColor = (v.mode === 'heavy') ? '#7c3aed' : '#0891b2';
    return `
      <div style="margin-top:10px;padding:10px;background:#fff;border-radius:6px;border:1px solid var(--border-light, #e5e7eb)">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap">
          <span id="verdictModeBadge" style="background:${modeColor};color:white;padding:4px 10px;border-radius:6px;font-weight:600;font-family:JetBrains Mono, monospace;font-size:0.78rem;text-transform:uppercase;letter-spacing:0.4px">${this._esc(v.mode || '')}</span>
          <span style="font-size:0.78rem;color:var(--text-muted)">verdict:</span>
          <span id="verdictBadge" style="background:${color};color:white;padding:4px 10px;border-radius:6px;font-weight:600;font-family:JetBrains Mono, monospace">${this._esc(cls)}</span>
          ${metaChips}
        </div>
        <div style="display:flex;flex-direction:column;gap:4px;margin-bottom:8px">${confBars}</div>
        <details style="margin-top:6px">
          <summary style="cursor:pointer;font-size:0.78rem;color:var(--text-muted)">Reasoning</summary>
          <div style="margin-top:4px;font-size:0.82rem;white-space:pre-wrap">${this._esc(verdict.reasoning || '')}</div>
        </details>
        <details style="margin-top:6px">
          <summary style="cursor:pointer;font-size:0.78rem;color:var(--text-muted)">Evidence cited</summary>
          <ul style="margin:4px 0 0 18px;font-size:0.78rem">${(verdict.evidence_cited || []).map(e => `<li>${this._esc(e)}</li>`).join('')}</ul>
        </details>
        <details style="margin-top:6px">
          <summary style="cursor:pointer;font-size:0.78rem;color:var(--text-muted)">Judge input (verifier evidence)</summary>
          ${App.withCopy(`<pre style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:4px;padding:6px;font-size:0.72rem;max-height:300px;overflow:auto;margin-top:4px;white-space:pre-wrap">${this._esc(v.judge_input || v.judge_py_stdout || '')}</pre>`)}
        </details>
        <details style="margin-top:6px">
          <summary style="cursor:pointer;font-size:0.78rem;color:var(--text-muted)">Agent reply</summary>
          ${App.withCopy(`<pre style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:4px;padding:6px;font-size:0.78rem;max-height:200px;overflow:auto;margin-top:4px;white-space:pre-wrap">${this._esc(v.agent_reply || '')}</pre>`)}
        </details>
      </div>
    `;
  },

  async _verify(worldId, mode) {
    const ep = this.state.activeEpisode[worldId];
    if (!ep) { App.toast('No active episode.'); return; }
    const lightBtn = document.getElementById('verifyLightBtn');
    const heavyBtn = document.getElementById('verifyHeavyBtn');
    if (lightBtn) lightBtn.disabled = true;
    if (heavyBtn) heavyBtn.disabled = true;
    App.toast(`Verifying (${mode})... ${mode === 'heavy' ? 'this may take 5–10 minutes' : 'about 30–60 seconds'}`);
    // Read the reply from the agent the task ran with.
    const kind = this.state.activeKind[worldId] || 'nanobot';
    const sid = this._sessionId(worldId, kind);
    try {
      const res = await API.xaiTasks.verify(worldId, ep.event, ep.episode_id, mode, sid, kind);
      this.state.lastVerdict[`${worldId}:${ep.episode_id}`] = res;
      this._renderShell(worldId, await API.runtime.status(worldId).catch(() => ({running: true})));
    } catch (e) {
      App.toast('Verify failed: ' + e.message, 8000);
      if (lightBtn) lightBtn.disabled = false;
      if (heavyBtn) heavyBtn.disabled = false;
    }
  },

  _renderChat(worldId) {
    const body = document.getElementById('agentBody');
    if (!body) return;
    body.innerHTML = `
      <div id="agentMessages" style="max-height:520px;overflow:auto;padding:8px;background:var(--bg-muted, #fafafa);border-radius:6px;font-size:0.9rem"><div style="color:var(--text-muted);padding:24px;text-align:center">Loading history...</div></div>
      <div style="display:flex;gap:6px;margin-top:8px">
        <textarea id="agentInput" rows="3" style="flex:1;padding:8px;border:1px solid var(--border-light, #e5e7eb);border-radius:6px;font-family:inherit;font-size:0.9rem;resize:vertical" placeholder="Type a free-form message, or click Use on a task above to load it. Cmd/Ctrl+Enter to send."></textarea>
        <div style="display:flex;flex-direction:column;gap:6px">
          <button class="btn btn-primary" id="agentSendBtn">Send</button>
          <button class="btn btn-secondary" id="agentClearBtn" title="Clear local view; server-side history is preserved">Clear view</button>
        </div>
      </div>
      <div id="agentStatus" style="margin-top:6px;font-size:0.78rem;color:var(--text-muted)"></div>
    `;
    document.getElementById('agentSendBtn').addEventListener('click', () => this.sendMessage(worldId));
    document.getElementById('agentClearBtn').addEventListener('click', () => {
      this.state.history[worldId] = [];
      this._renderMessages(worldId);
    });
    const input = document.getElementById('agentInput');
    input.addEventListener('keydown', (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
        e.preventDefault();
        this.sendMessage(worldId);
      }
    });
    input.focus();
    this._reloadHistory(worldId);
  },

  async _reloadHistory(worldId) {
    const kind = this.state.activeKind[worldId] || 'nanobot';
    const sid = this._sessionId(worldId, kind);
    try {
      const res = await API.agent.history(worldId, sid, kind);
      this.state.history[worldId] = res.history || [];
    } catch (e) {
      if (!this.state.history[worldId]) this.state.history[worldId] = [];
    }
    this._renderMessages(worldId);
  },

  _renderMessageBubble(item) {
    const isUser = item.role === 'user';
    const bg = isUser ? '#eef2ff' : '#ffffff';
    const align = isUser ? 'flex-end' : 'flex-start';
    const label = isUser ? 'You' : 'Agent';
    const time = item.ts ? new Date(item.ts).toLocaleTimeString() : '';
    const safe = this._esc(item.content);
    // Wall-clock duration of the assistant turn, including tool round-trips.
    let durationChip = '';
    if (!isUser && item.turn_ms != null) {
      const human = this._formatMs(item.turn_ms);
      durationChip = `<span title="Wall-clock time from your message to this reply (${item.turn_ms} ms)" style="margin-left:6px;background:#fef3c7;color:#92400e;padding:1px 6px;border-radius:3px;font-family:JetBrains Mono, ui-monospace, monospace;font-size:0.66rem">⏱ ${human}</span>`;
    }
    return `
      <div style="display:flex;justify-content:${align};margin-bottom:8px">
        <div style="max-width:78%;background:${bg};border:1px solid var(--border-light, #e5e7eb);border-radius:8px;padding:8px 10px">
          <div style="font-size:0.7rem;color:var(--text-muted);margin-bottom:4px">${label} &middot; ${time}${durationChip}</div>
          <div style="white-space:pre-wrap;word-wrap:break-word">${safe}</div>
        </div>
      </div>`;
  },

  _formatMs(ms) {
    const s = ms / 1000;
    if (s < 1) return `${ms} ms`;
    if (s < 60) return `${s.toFixed(s < 10 ? 2 : 1)}s`;
    const m = Math.floor(s / 60);
    return `${m}m ${Math.round(s - m * 60)}s`;
  },

  _renderToolGroup(steps) {
    const calls = steps.filter(s => s.type === 'tool_call');
    const results = steps.filter(s => s.type === 'tool_result');
    const resultById = {};
    for (const r of results) {
      if (r.tool_call_id) resultById[r.tool_call_id] = r;
    }
    const stepHtml = calls.map((c, i) => {
      const argsPretty = this._esc(this._formatJSON(c.arguments));
      const r = (c.id && resultById[c.id]) || results[i];
      const resultText = r ? this._esc(r.content).slice(0, 4000) : '<em style="color:var(--text-muted)">(no result captured)</em>';
      return `
        <div style="border-left:2px solid #c7d2fe;padding:6px 0 6px 10px;margin:6px 0">
          <div style="font-family:JetBrains Mono, ui-monospace, monospace;font-size:0.75rem;color:#3730a3;margin-bottom:4px">
            <strong>${this._esc(c.name)}</strong>
          </div>
          <details style="margin:2px 0">
            <summary style="cursor:pointer;font-size:0.72rem;color:var(--text-muted)">arguments</summary>
            ${App.withCopy(`<pre style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:4px;padding:6px;font-size:0.72rem;overflow:auto;margin:4px 0;max-height:200px">${argsPretty}</pre>`)}
          </details>
          <details style="margin:2px 0" open>
            <summary style="cursor:pointer;font-size:0.72rem;color:var(--text-muted)">result</summary>
            ${App.withCopy(`<pre style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:4px;padding:6px;font-size:0.72rem;overflow:auto;margin:4px 0;max-height:200px;white-space:pre-wrap;word-wrap:break-word">${resultText}</pre>`)}
          </details>
        </div>`;
    }).join('');
    return `
      <div style="display:flex;justify-content:flex-start;margin-bottom:8px">
        <details style="max-width:78%;background:#fafbff;border:1px dashed #c7d2fe;border-radius:8px;padding:6px 10px;width:100%">
          <summary style="cursor:pointer;font-size:0.78rem;color:#4338ca">
            🔧 Agent thinking — ${calls.length} tool call${calls.length === 1 ? '' : 's'}
          </summary>
          <div style="margin-top:6px">${stepHtml}</div>
        </details>
      </div>`;
  },

  _renderMessages(worldId) {
    const list = document.getElementById('agentMessages');
    if (!list) return;
    const items = this.state.history[worldId] || [];
    if (!items.length) {
      list.innerHTML = '<div style="color:var(--text-muted);padding:24px;text-align:center">No messages yet. Pick a task above or type a free-form message below.</div>';
      return;
    }
    const groups = [];
    let pendingTools = [];
    const flushTools = () => {
      if (pendingTools.length) {
        groups.push({ kind: 'tools', items: pendingTools });
        pendingTools = [];
      }
    };
    for (const it of items) {
      if (it.type === 'tool_call' || it.type === 'tool_result') {
        pendingTools.push(it);
      } else {
        flushTools();
        groups.push({ kind: 'message', item: it });
      }
    }
    flushTools();
    list.innerHTML = groups.map(g =>
      g.kind === 'message' ? this._renderMessageBubble(g.item) : this._renderToolGroup(g.items)
    ).join('');
    list.scrollTop = list.scrollHeight;
  },

  _appendMessage(worldId, role, content) {
    if (!this.state.history[worldId]) this.state.history[worldId] = [];
    this.state.history[worldId].push({ type: 'message', role, content, ts: Date.now() });
    this._renderMessages(worldId);
  },

  async sendMessage(worldId) {
    if (this.state.pending) return;
    const input = document.getElementById('agentInput');
    const text = (input.value || '').trim();
    if (!text) return;
    input.value = '';
    const kind = this.state.activeKind[worldId] || 'nanobot';
    const sid = this._sessionId(worldId, kind);
    // Empty model keeps the running model (or the registry default on first attach).
    const model = this.state.selectedModel[worldId + ':' + kind] || undefined;
    this._appendMessage(worldId, 'user', text);
    this.state.pending = true;
    document.getElementById('agentSendBtn').disabled = true;
    document.getElementById('agentStatus').textContent =
      `${kind} is thinking…${model ? ` (model ${model}; switching re-attaches the container)` : ''} (first message may spin up the agent container, ~10–30s)`;
    try {
      await API.agent.chat(worldId, text, sid, kind, model);
      await this._reloadHistory(worldId);
      document.getElementById('agentStatus').textContent = '';
      this.refresh(worldId);
    } catch (e) {
      this._appendMessage(worldId, 'assistant', '[Error] ' + e.message);
      document.getElementById('agentStatus').textContent = 'Failed: ' + e.message;
    } finally {
      this.state.pending = false;
      const btn = document.getElementById('agentSendBtn');
      if (btn) btn.disabled = false;
    }
  },

  async startWorld(worldId) {
    const card = document.getElementById('agentCard');
    const pendingWh = (this.state.pendingWebharbor || {})[worldId];
    const enableWebharbor = pendingWh === undefined ? true : pendingWh;
    const tag = enableWebharbor ? 'env + webharbor' : 'env';
    card.innerHTML = `<div class="loading"><div class="spinner"></div> Starting world ${this._esc(tag)} (mcp${enableWebharbor ? ' + 15 web mirrors' : ''}; ~30s on first run while images build)...</div>`;
    // Environment only; an agent attaches on its first message.
    try { await API.runtime.start(worldId, null, { enable_webharbor: enableWebharbor }); App.toast(`World started (${tag})`); }
    catch (e) { App.toast('Start failed: ' + e.message, 6000); }
    await this.refresh(worldId);
  },

  async resetWebharbor(worldId) {
    if (!confirm(
      'Reset all 15 WebHarbor site DBs to their seed baseline?\n\n' +
      'Wipes anything the agent did on www.amazon.com, github.com, etc. ' +
      'in this instance. Other surfaces (MCP DBs, workspace files) are untouched. ' +
      'Sub-second.'
    )) return;
    try {
      const r = await API.webharbor.reset(worldId);
      const n = r && r.results ? Object.keys(r.results).length : 15;
      App.toast(`WebHarbor reset (${n} sites)`);
    } catch (e) { App.toast('WebHarbor reset failed: ' + e.message, 6000); }
    await this.refresh(worldId);
  },

  async stopWorld(worldId) {
    if (!confirm('Stop the docker stack for this world?')) return;
    const card = document.getElementById('agentCard');
    card.innerHTML = '<div class="loading"><div class="spinner"></div> Stopping...</div>';
    try { await API.runtime.stop(worldId); App.toast('World stopped'); }
    catch (e) { App.toast('Stop failed: ' + e.message, 6000); }
    await this.refresh(worldId);
  },

  async resetWorld(worldId) {
    const ok = confirm(
      `Reset the world "${worldId}"?\n\n` +
      'This will:\n' +
      '  · stop the docker stack\n' +
      '  · DELETE every modification the agent made (sessions, MEMORY, ' +
      'edited files, DB rows)\n' +
      '  · rebuild the world from its pre-baked day snapshot\n' +
      '  · drop every saved episode snapshot\n' +
      '  · restart the docker stack if it was running\n\n' +
      'There is no undo. Continue?',
    );
    if (!ok) return;
    const card = document.getElementById('agentCard');
    card.innerHTML = '<div class="loading"><div class="spinner"></div> Resetting world (~30–60s)...</div>';
    // Drop cached chat/episode state so stale data never renders during the rebuild.
    this.state.history[worldId] = [];
    delete this.state.activeEpisode[worldId];
    Object.keys(this.state.lastVerdict)
      .filter(k => k.startsWith(worldId + ':'))
      .forEach(k => delete this.state.lastVerdict[k]);
    try {
      const res = await API.runtime.reset(worldId);
      const note = res.restarted
        ? 'World reset and restarted.'
        : (res.was_running
            ? 'World reset; docker stack stopped (start failed — check logs).'
            : 'World reset; click Start to bring it up.');
      App.toast(note, 5000);
    } catch (e) {
      App.toast('Reset failed: ' + e.message, 8000);
    }
    await this.refresh(worldId);
  },
};
