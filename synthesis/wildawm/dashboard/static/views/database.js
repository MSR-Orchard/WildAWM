const DatabaseView = {
  state: { app: null, table: null, page: 1 },

  async render(el, worldId) {
    this._el = el;
    this._worldId = worldId;
    el.innerHTML = '<div class="loading"><div class="spinner"></div> Loading databases...</div>';
    try {
      const { databases } = await API.db.list(worldId);
      el.innerHTML = `<div class="layout"><div class="card" id="dbSidebar"></div><div id="dbMain"><div class="empty-state"><div class="empty-title">Select a database</div></div></div></div>`;
      const sidebar = document.getElementById('dbSidebar');
      sidebar.innerHTML = `<div class="card-header"><span class="card-title">Databases</span></div><div class="sidebar-list" id="dbList"></div>`;
      const list = document.getElementById('dbList');
      databases.forEach(db => {
        const item = document.createElement('button');
        item.className = 'sidebar-item';
        item.dataset.appId = db.app_id;
        item.innerHTML = `<div class="item-icon icon-db">${db.tables}</div><div><div>${db.app_id}</div><div class="item-meta">${db.total_rows} rows</div></div>`;
        item.addEventListener('click', () => this.selectDb(db.app_id));
        list.appendChild(item);
      });
      if (this.state.app) this.selectDb(this.state.app);
    } catch (e) {
      el.innerHTML = `<div class="empty-state"><p>${e.message}</p></div>`;
    }
  },

  async selectDb(appId) {
    this.state.app = appId;
    document.querySelectorAll('#dbList .sidebar-item').forEach(i => i.classList.toggle('active', i.dataset.appId === appId));
    const main = document.getElementById('dbMain');
    main.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
    try {
      const { tables } = await API.db.tables(this._worldId, appId);
      main.innerHTML = `
        <div class="card" style="margin-bottom:16px">
          <div class="card-header"><span class="card-title">${appId}</span><span class="card-subtitle">${tables.length} tables</span></div>
          <div class="tab-bar" id="tableTabBar"></div>
          <div id="tableContent"></div>
        </div>
        <div class="card">
          <div class="card-header"><span class="card-title">SQL Query</span></div>
          <div class="sql-box">
            <textarea class="sql-input" id="sqlInput" rows="2" placeholder="SELECT * FROM messages WHERE is_read = 0 LIMIT 20"></textarea>
            <button class="btn btn-primary" id="sqlRunBtn">Run</button>
          </div>
          <div id="sqlResult"></div>
        </div>
      `;
      const tabBar = document.getElementById('tableTabBar');
      tables.forEach(t => {
        const btn = document.createElement('button');
        btn.className = 'tab-btn';
        btn.textContent = `${t.name} (${t.row_count})`;
        btn.addEventListener('click', () => this.selectTable(appId, t.name));
        tabBar.appendChild(btn);
      });
      document.getElementById('sqlRunBtn').addEventListener('click', () => this.runQuery(appId));
      document.getElementById('sqlInput').addEventListener('keydown', e => {
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) this.runQuery(appId);
      });
      if (tables.length) this.selectTable(appId, tables[0].name);
    } catch (e) {
      main.innerHTML = `<div class="card"><p>Error: ${e.message}</p></div>`;
    }
  },

  async selectTable(appId, table, page = 1) {
    this.state.table = table;
    this.state.page = page;
    document.querySelectorAll('#tableTabBar .tab-btn').forEach(b => b.classList.toggle('active', b.textContent.startsWith(table)));
    const content = document.getElementById('tableContent');
    content.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
    try {
      const data = await API.db.table(this._worldId, appId, table, page);
      this.renderTable(content, data, appId);
    } catch (e) {
      content.innerHTML = `<p>Error: ${e.message}</p>`;
    }
  },

  renderTable(el, data, appId) {
    const cols = data.columns.map(c => c.name);
    let html = '<div class="data-table-wrap"><table class="data-table"><thead><tr>';
    cols.forEach(c => html += `<th>${c}</th>`);
    html += '</tr></thead><tbody>';
    data.rows.forEach(row => {
      html += '<tr>';
      cols.forEach(c => {
        let val = row[c];
        if (val === null) val = '<span style="color:var(--text-muted)">NULL</span>';
        else if (typeof val === 'string' && val.length > 80) val = val.substring(0, 80) + '...';
        const isNum = typeof row[c] === 'number';
        html += `<td${isNum ? ' class="cell-num"' : ''}>${val}</td>`;
      });
      html += '</tr>';
    });
    html += '</tbody></table></div>';
    html += `<div class="pagination">
      <button ${data.page <= 1 ? 'disabled' : ''} id="prevPage">&laquo; Prev</button>
      <span class="page-info">Page ${data.page}/${data.total_pages} (${data.total} rows)</span>
      <button ${data.page >= data.total_pages ? 'disabled' : ''} id="nextPage">Next &raquo;</button>
    </div>`;
    el.innerHTML = html;
    document.getElementById('prevPage')?.addEventListener('click', () => this.selectTable(appId, data.table, data.page - 1));
    document.getElementById('nextPage')?.addEventListener('click', () => this.selectTable(appId, data.table, data.page + 1));
  },

  async runQuery(appId) {
    const sql = document.getElementById('sqlInput').value.trim();
    if (!sql) return;
    const result = document.getElementById('sqlResult');
    result.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
    try {
      const data = await API.db.query(this._worldId, appId, sql);
      if (!data.rows.length) {
        result.innerHTML = '<p style="color:var(--text-muted)">No results</p>';
        return;
      }
      const cols = Object.keys(data.rows[0]);
      let html = `<p style="margin-bottom:8px;font-size:0.8rem;color:var(--text-muted)">${data.count} rows</p><div class="data-table-wrap"><table class="data-table"><thead><tr>`;
      cols.forEach(c => html += `<th>${c}</th>`);
      html += '</tr></thead><tbody>';
      data.rows.forEach(row => {
        html += '<tr>';
        cols.forEach(c => {
          let v = row[c];
          if (v === null) v = '<span style="color:var(--text-muted)">NULL</span>';
          else if (typeof v === 'string' && v.length > 120) v = v.substring(0, 120) + '...';
          html += `<td>${v}</td>`;
        });
        html += '</tr>';
      });
      html += '</tbody></table></div>';
      result.innerHTML = html;
    } catch (e) {
      result.innerHTML = `<p style="color:var(--danger)">${e.message}</p>`;
    }
  },
};
