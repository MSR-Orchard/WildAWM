const FilesView = {
  async render(el, worldId) {
    this._el = el;
    this._worldId = worldId;
    el.innerHTML = '<div class="loading"><div class="spinner"></div> Loading file tree...</div>';
    try {
      const { tree } = await API.files.tree(this._worldId);
      el.innerHTML = `<div class="layout layout-wide"><div class="card" style="overflow-y:auto;max-height:calc(100vh - 120px)"><div class="card-header"><span class="card-title">Workspace Files</span></div><div class="file-tree" id="fileTree"></div></div><div id="fileContent"><div class="empty-state"><div class="empty-title">Select a file to view</div></div></div></div>`;
      this.renderTree(document.getElementById('fileTree'), tree);
    } catch (e) {
      el.innerHTML = `<div class="empty-state"><p>${e.message}</p></div>`;
    }
  },

  renderTree(container, items, depth = 0) {
    items.forEach(item => {
      const div = document.createElement('div');
      if (item.type === 'dir') {
        const toggle = document.createElement('div');
        toggle.className = 'file-tree-item';
        toggle.innerHTML = `<span class="ft-icon">&#9658;</span><span class="ft-name">${item.name}/</span>`;
        div.appendChild(toggle);
        const children = document.createElement('div');
        children.className = 'file-tree-children';
        children.style.display = 'none';
        if (item.children && item.children.length) {
          this.renderTree(children, item.children, depth + 1);
        }
        div.appendChild(children);
        toggle.addEventListener('click', () => {
          const open = children.style.display !== 'none';
          children.style.display = open ? 'none' : 'block';
          toggle.querySelector('.ft-icon').innerHTML = open ? '&#9658;' : '&#9660;';
        });
        if (depth < 1) {
          children.style.display = 'block';
          toggle.querySelector('.ft-icon').innerHTML = '&#9660;';
        }
      } else {
        const fileItem = document.createElement('div');
        fileItem.className = 'file-tree-item';
        fileItem.dataset.path = item.path;
        const ext = item.name.split('.').pop();
        const icons = { py: '&#128220;', md: '&#128196;', txt: '&#128196;', json: '&#123;', yml: '&#9881;' };
        fileItem.innerHTML = `<span class="ft-icon">${icons[ext] || '&#128196;'}</span><span class="ft-name">${item.name}</span>`;
        fileItem.addEventListener('click', () => this.openFile(item.path));
        div.appendChild(fileItem);
      }
      container.appendChild(div);
    });
  },

  async openFile(path) {
    document.querySelectorAll('.file-tree-item').forEach(i => i.classList.toggle('active', i.dataset.path === path));
    const content = document.getElementById('fileContent');
    content.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
    try {
      const data = await API.files.read(this._worldId, path);
      const ext = path.split('.').pop();
      const isMd = ext === 'md';
      content.innerHTML = `
        <div class="card">
          <div class="card-header">
            <span class="card-title">${path}</span>
            <span class="card-subtitle">${(data.size / 1024).toFixed(1)} KB</span>
          </div>
          ${isMd
            ? App.withCopy(`<div class="md-content" style="padding:12px;background:#fafafa;border:1px solid #e5e7eb;border-radius:6px">${App.renderMarkdown(data.content)}</div>`)
            : App.withCopy(`<div class="code-viewer">${this.escapeHtml(data.content)}</div>`)
          }
        </div>
      `;
    } catch (e) {
      content.innerHTML = `<div class="card"><p>Error: ${e.message}</p></div>`;
    }
  },

  escapeHtml(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  },
};
