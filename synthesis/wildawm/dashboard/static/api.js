const API = {
  async get(url) {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
    return res.json();
  },
  async post(url, body = {}) {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
    return res.json();
  },
  async del(url) {
    const res = await fetch(url, { method: 'DELETE' });
    if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
    return res.json();
  },

  worlds: {
    list: () => API.get('/api/worlds'),
    get: (id) => API.get(`/api/worlds/${id}`),
    create: (profile, day, name) => API.post('/api/worlds', { profile, day, name }),
    destroy: (id) => API.del(`/api/worlds/${id}`),
    // profile === undefined destroys ALL worlds.
    destroyAll: (profile) => API.post('/api/worlds/destroy-all',
                                      profile ? { profile } : {}),
    overview: (id) => API.get(`/api/worlds/${id}/overview`),
    today: (id) => API.get(`/api/worlds/${id}/today`),
  },
  profiles: {
    list: () => API.get('/api/profiles'),
    get: (name) => API.get(`/api/profiles/${name}`),
  },
  db: {
    list: (wid) => API.get(`/api/worlds/${wid}/db`),
    tables: (wid, app) => API.get(`/api/worlds/${wid}/db/${app}/tables`),
    table: (wid, app, tbl, page = 1) => API.get(`/api/worlds/${wid}/db/${app}/table/${tbl}?page=${page}`),
    query: (wid, app, sql) => API.get(`/api/worlds/${wid}/db/${app}/query?sql=${encodeURIComponent(sql)}`),
  },
  files: {
    list: (wid, path = '/') => API.get(`/api/worlds/${wid}/files?path=${encodeURIComponent(path)}`),
    read: (wid, path) => API.get(`/api/worlds/${wid}/files/read?path=${encodeURIComponent(path)}`),
    tree: (wid) => API.get(`/api/worlds/${wid}/files/tree`),
  },
  runtime: {
    status: (wid) => API.get(`/api/worlds/${wid}/runtime`),
    // Start the world environment; `kind` also attaches one agent immediately.
    start: (wid, kind, opts) => API.post(`/api/worlds/${wid}/runtime/start`,
                                          Object.assign({}, kind ? { kind } : {}, opts || {})),
    stop: (wid) => API.post(`/api/worlds/${wid}/runtime/stop`),
    reset: (wid) => API.post(`/api/worlds/${wid}/runtime/reset`),
    startAgent: (wid, kind, opts) =>
      API.post(`/api/worlds/${wid}/agents/${encodeURIComponent(kind)}/start`, opts || {}),
    stopAgent: (wid, kind) =>
      API.post(`/api/worlds/${wid}/agents/${encodeURIComponent(kind)}/stop`),
  },
  webharbor: {
    health: (wid) => API.get(`/api/worlds/${wid}/webharbor/health`),
    reset: (wid, site) => API.post(`/api/worlds/${wid}/webharbor/reset`,
                                    site ? { site } : {}),
  },
  models: {
    list: () => API.get('/api/models'),
  },
  agent: {
    chat: (wid, message, sessionId, kind, model) => API.post(`/api/worlds/${wid}/agent/chat`, {
      message, session_id: sessionId, kind, model,
    }),
    history: (wid, sessionId, kind) => {
      const q = new URLSearchParams();
      if (sessionId) q.set('session_id', sessionId);
      if (kind) q.set('kind', kind);
      const qs = q.toString();
      return API.get(`/api/worlds/${wid}/agent/history` + (qs ? `?${qs}` : ''));
    },
  },
  tasks: {
    activeEpisode: (wid) => API.get(`/api/worlds/${wid}/episodes/active`),
    endEpisode: (wid, episodeId) =>
      API.post(`/api/worlds/${wid}/episodes/${encodeURIComponent(episodeId)}/end`),
  },
  xaiTasks: {
    listGlobal: () => API.get('/api/xai-tasks'),
    getGlobal: (name) => API.get(`/api/xai-tasks/${encodeURIComponent(name)}`),
    list: (wid) => API.get(`/api/worlds/${wid}/xai-tasks`),
    get: (wid, name) => API.get(`/api/worlds/${wid}/xai-tasks/${encodeURIComponent(name)}`),
    startEpisode: (wid, name) =>
      API.post(`/api/worlds/${wid}/xai-tasks/${encodeURIComponent(name)}/episode/start`),
    verify: (wid, name, episodeId, mode, sessionId, kind) =>
      API.post(`/api/worlds/${wid}/xai-tasks/${encodeURIComponent(name)}/episode/${encodeURIComponent(episodeId)}/verify`,
               { mode, session_id: sessionId, kind }),
  },
  events: {
    list: () => API.get('/api/events'),
    get: (name) => API.get(`/api/events/${name}`),
  },
  stories: {
    list: (profile) => API.get(`/api/stories/${profile}`),
    get: (profile, date) => API.get(`/api/stories/${profile}/${date}`),
  },
};
