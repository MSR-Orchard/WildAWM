const ProfilesView = {
  state: { mode: 'profiles' },

  async load() {
    const el = document.getElementById('view-profiles');
    el.innerHTML = `
      <div class="tab-bar" id="profilesTabBar">
        <button class="tab-btn active" data-mode="profiles">Profiles</button>
        <button class="tab-btn" data-mode="events">Event Skills</button>
        <button class="tab-btn" data-mode="stories">Stories</button>
      </div>
      <div id="profilesContent"></div>
    `;
    document.querySelectorAll('#profilesTabBar .tab-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        this.state.mode = btn.dataset.mode;
        document.querySelectorAll('#profilesTabBar .tab-btn').forEach(b => b.classList.toggle('active', b === btn));
        this.loadContent();
      });
    });
    this.loadContent();
  },

  async loadContent() {
    const el = document.getElementById('profilesContent');
    if (this.state.mode === 'profiles') await this.loadProfiles(el);
    else if (this.state.mode === 'events') await this.loadEvents(el);
    else if (this.state.mode === 'stories') await this.loadStories(el);
  },

  async loadProfiles(el) {
    el.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
    try {
      const { profiles } = await API.profiles.list();
      el.innerHTML = '<div class="layout"><div class="card"><div class="card-header"><span class="card-title">Profiles</span><span class="card-subtitle">' + profiles.length + '</span></div><div class="sidebar-list" id="profileList"></div></div><div id="profileDetail"><div class="empty-state"><div class="empty-title">Select a profile</div></div></div></div>';
      const list = document.getElementById('profileList');
      profiles.forEach(p => {
        const item = document.createElement('button');
        item.className = 'sidebar-item';
        item.dataset.name = p.name;
        item.innerHTML = '<div class="item-icon" style="background:#ede9fe;color:#7c3aed">' + p.name[0].toUpperCase() + '</div><div><div>' + (p.title || p.name) + '</div><div class="item-meta">' + (p.has_stories ? 'has stories' : 'no stories') + '</div></div>';
        item.addEventListener('click', () => this.showProfile(p.name));
        list.appendChild(item);
      });
    } catch (e) { el.innerHTML = '<p>' + e.message + '</p>'; }
  },

  async showProfile(name) {
    document.querySelectorAll('#profileList .sidebar-item').forEach(i => i.classList.toggle('active', i.dataset.name === name));
    const detail = document.getElementById('profileDetail');
    detail.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
    try {
      const data = await API.profiles.get(name);
      const md = data.profile_md ?? '';
      let html = '<div class="card"><div class="md-content">' + App.renderMarkdown(md) + '</div></div>';
      if (data.days && data.days.length) {
        html += '<div class="card" style="margin-top:12px"><div class="card-header"><span class="card-title">Compiled days</span><span class="card-subtitle">' + data.days.length + ' day' + (data.days.length === 1 ? '' : 's') + '</span></div>';
        html += '<table style="width:100%;font-size:0.85rem"><thead><tr style="text-align:left;color:var(--text-muted)"><th style="padding:4px 8px">Day</th><th style="padding:4px 8px">Date</th><th style="padding:4px 8px">Events</th><th style="padding:4px 8px">Snapshot</th></tr></thead><tbody>';
        for (const d of data.days) {
          html += '<tr><td style="padding:4px 8px">Day ' + d.day + '</td><td style="padding:4px 8px">' + (d.date || '—') + '</td><td style="padding:4px 8px">' + d.event_count + '</td><td style="padding:4px 8px">' + (d.snapshot_ready ? '✓ ready' : '— not baked') + '</td></tr>';
        }
        html += '</tbody></table></div>';
      }
      detail.innerHTML = html;
    } catch (e) { detail.innerHTML = '<p style="color:var(--danger)">' + e.message + '</p>'; }
  },

  async loadEvents(el) {
    el.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
    try {
      const { events, count } = await API.events.list();
      el.innerHTML = '<div class="layout"><div class="card"><div class="card-header"><span class="card-title">Event Skills</span><span class="card-subtitle">' + count + '</span></div><div class="sidebar-list" id="eventList" style="max-height:calc(100vh - 200px);overflow-y:auto"></div></div><div id="eventDetail"><div class="empty-state"><div class="empty-title">Select an event skill</div></div></div></div>';
      const list = document.getElementById('eventList');
      events.forEach(ev => {
        const item = document.createElement('button');
        item.className = 'sidebar-item';
        item.dataset.name = ev.name;
        item.innerHTML = '<div><div style="font-family:var(--mono);font-size:0.8rem">' + ev.name + '</div><div class="item-meta">' + (ev.description || '').substring(0, 60) + '</div></div>';
        item.addEventListener('click', () => this.showEvent(ev.name));
        list.appendChild(item);
      });
    } catch (e) { el.innerHTML = '<p>' + e.message + '</p>'; }
  },

  async showEvent(name) {
    document.querySelectorAll('#eventList .sidebar-item').forEach(i => i.classList.toggle('active', i.dataset.name === name));
    const detail = document.getElementById('eventDetail');
    detail.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
    try {
      const data = await API.events.get(name);
      detail.innerHTML = '<div class="card"><div class="card-header"><span class="card-title" style="font-family:var(--mono)">' + name + '</span></div><div class="md-content">' + App.renderMarkdown(data.content) + '</div></div>';
    } catch (e) { detail.innerHTML = '<p>' + e.message + '</p>'; }
  },

  async loadStories(el) {
    el.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
    try {
      const { profiles } = await API.profiles.list();
      const withStories = profiles.filter(p => p.has_stories);
      el.innerHTML = '<div class="layout"><div class="card"><div class="card-header"><span class="card-title">Stories</span></div><div class="sidebar-list" id="storyList"></div></div><div id="storyDetail"><div class="empty-state"><div class="empty-title">Select a story</div></div></div></div>';
      const list = document.getElementById('storyList');
      for (const p of withStories) {
        const { dates } = await API.stories.list(p.name);
        const header = document.createElement('div');
        header.style.cssText = 'padding:8px 12px;font-weight:600;font-size:0.8rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.5px;margin-top:8px';
        header.textContent = p.name;
        list.appendChild(header);
        dates.forEach(date => {
          const item = document.createElement('button');
          item.className = 'sidebar-item';
          item.dataset.key = p.name + '/' + date;
          item.innerHTML = '<div style="font-size:0.85rem">' + date + '</div>';
          item.addEventListener('click', () => this.showStory(p.name, date));
          list.appendChild(item);
        });
      }
    } catch (e) { el.innerHTML = '<p>' + e.message + '</p>'; }
  },

  async showStory(profile, date) {
    document.querySelectorAll('#storyList .sidebar-item').forEach(i => i.classList.toggle('active', i.dataset.key === profile + '/' + date));
    const detail = document.getElementById('storyDetail');
    detail.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
    try {
      const data = await API.stories.get(profile, date);
      detail.innerHTML = '<div class="card"><div class="md-content">' + App.renderMarkdown(data.content) + '</div></div>';
    } catch (e) { detail.innerHTML = '<p>' + e.message + '</p>'; }
  },
};
