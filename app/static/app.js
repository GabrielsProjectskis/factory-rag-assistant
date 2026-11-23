/* Factory Assistant front end. No framework, no CDN: everything is served from this machine. */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const icon = (name) => `<svg class="icon"><use href="#i-${name}"/></svg>`;
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const state = { convId: null, image: null, busy: false, status: null, settings: null, pollTimer: null };

async function api(path, options = {}) {
  if (options.json !== undefined) {
    options.body = JSON.stringify(options.json);
    options.headers = { 'Content-Type': 'application/json' };
  }
  const resp = await fetch(path, options);
  if (!resp.ok) {
    let detail = resp.statusText;
    try { detail = (await resp.json()).detail || detail; } catch { /* not JSON */ }
    throw new Error(typeof detail === 'string' ? detail : 'Request failed');
  }
  return resp.json();
}

let toastTimer;
function toast(text) {
  const el = $('#toast');
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 3600);
}

/* ---------- markdown (escaped first, so model output can never inject HTML) ---------- */
function inline(text) {
  return esc(text)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>')
    .replace(/\[(\d{1,2})\]/g, '<button class="cite" data-n="$1" title="Show source $1">$1</button>');
}
function markdown(src) {
  const out = [];
  const lines = src.split('\n');
  let para = [];
  let list = null;
  const flushPara = () => { if (para.length) { out.push(`<p>${para.map(inline).join('<br>')}</p>`); para = []; } };
  const flushList = () => {
    if (list) { out.push(`<${list.tag}>${list.items.map((i) => `<li>${inline(i)}</li>`).join('')}</${list.tag}>`); list = null; }
  };
  const item = (tag, text) => {
    flushPara();
    if (!list || list.tag !== tag) { flushList(); list = { tag, items: [] }; }
    list.items.push(text);
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    let m;
    if (line.startsWith('```')) {
      flushPara(); flushList();
      const code = [];
      for (i++; i < lines.length && !lines[i].startsWith('```'); i++) code.push(lines[i]);
      out.push(`<pre><code>${esc(code.join('\n'))}</code></pre>`);
    } else if ((m = line.match(/^#{1,4}\s+(.*)/))) {
      flushPara(); flushList(); out.push(`<h3>${inline(m[1])}</h3>`);
    } else if ((m = line.match(/^\s*[-*•]\s+(.*)/))) {
      item('ul', m[1]);
    } else if ((m = line.match(/^\s*\d+[.)]\s+(.*)/))) {
      item('ol', m[1]);
    } else if (!line.trim()) {
      flushPara();
    } else {
      flushList(); para.push(line);
    }
  }
  flushPara(); flushList();
  return out.join('');
}

/* ---------- navigation ---------- */
function show(view) {
  $$('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${view}`));
  $$('.nav button').forEach((b) => b.classList.toggle('active', b.dataset.view === view));
  $('#sidebar').classList.remove('open');
  if (view === 'parts') loadParts();
  if (view === 'library') loadDocs();
  if (view === 'settings') loadSettings();
  if (view === 'chat') $('#input').focus();
}
$$('.nav button').forEach((b) => b.addEventListener('click', () => show(b.dataset.view)));
$$('.menu-btn').forEach((b) => b.addEventListener('click', () => $('#sidebar').classList.add('open')));
$('#scrim').addEventListener('click', () => $('#sidebar').classList.remove('open'));

/* ---------- status / privacy badge ---------- */
async function loadStatus() {
  const s = state.status = await api('/api/status');
  document.title = s.app_name;
  $('#appName').textContent = s.app_name;
  const badge = $('#privacy');
  badge.className = `privacy ${s.all_local ? 'local' : 'cloud'}`;
  badge.innerHTML = s.all_local
    ? `${icon('lock')}<span>Private · runs locally</span>`
    : `${icon('cloud')}<span>Using a cloud API</span>`;
  badge.title = Object.entries(s.roles).map(([k, r]) => `${k}: ${r.model} (${r.provider})`).join('\n');
  const stale = $('#staleBanner');
  if (s.reindex.running) {
    stale.hidden = false; stale.className = 'banner warn';
    stale.textContent = `Re-indexing… ${s.reindex.done} of ${s.reindex.total}`;
  } else if (s.stale_chunks > 0) {
    stale.hidden = false; stale.className = 'banner warn';
    stale.innerHTML = '<span>The embedding model changed. Re-index so every document is searchable by meaning.</span><button class="btn" id="staleReindex">Re-index</button>';
    $('#staleReindex').addEventListener('click', startReindex);
  } else {
    stale.hidden = true;
  }
  return s;
}

/* ---------- conversations ---------- */
async function loadConversations() {
  const { conversations } = await api('/api/conversations');
  const list = $('#convList');
  list.innerHTML = '';
  for (const c of conversations) {
    const row = document.createElement('div');
    row.className = `conv${c.id === state.convId ? ' active' : ''}`;
    row.tabIndex = 0;
    row.setAttribute('role', 'button');
    row.innerHTML = `<span>${esc(c.title || 'Conversation')}</span><button class="del" aria-label="Delete conversation">${icon('x')}</button>`;
    const open = () => openConversation(c.id, c.title);
    row.addEventListener('click', open);
    row.addEventListener('keydown', (e) => { if (e.key === 'Enter') open(); });
    $('.del', row).addEventListener('click', async (e) => {
      e.stopPropagation();
      await api(`/api/conversations/${c.id}`, { method: 'DELETE' });
      if (state.convId === c.id) newChat();
      loadConversations();
    });
    list.append(row);
  }
}

async function openConversation(id, title) {
  if (state.busy) return;
  state.convId = id;
  show('chat');
  $('#chatTitle').textContent = title || 'Assistant';
  const { messages } = await api(`/api/conversations/${id}`);
  const box = $('#messages');
  box.innerHTML = '';
  for (const m of messages) {
    if (m.role === 'user') {
      addUserMessage(m.content, m.has_image ? `/api/messages/${m.id}/image` : null);
    } else {
      const ui = addAssistantMessage();
      ui.setText(m.content);
      ui.setSources(m.sources, [], null);
      ui.finish();
    }
  }
  box.scrollTop = box.scrollHeight;
  loadConversations();
}

async function newChat() {
  if (state.busy) return;
  state.convId = null;
  $('#chatTitle').textContent = 'Assistant';
  show('chat');
  await renderEmpty();
  loadConversations();
}
$('#newChat').addEventListener('click', newChat);

async function renderEmpty() {
  const box = $('#messages');
  let parts = [];
  try { parts = (await api('/api/ics')).parts.slice(0, 3); } catch { /* offline */ }
  const docs = state.status ? state.status.documents : 0;
  const prompts = [];
  if (parts[0]) prompts.push(`What is ${parts[0].code} for?`);
  if (parts[1]) prompts.push(`How do I maintain ${parts[1].name || parts[1].code}?`);
  if (parts[2]) prompts.push(`How do I start up ${parts[2].name || parts[2].code} safely?`);
  box.innerHTML = `<div class="empty">
    <span class="brand-mark"></span>
    <h2>How can I help?</h2>
    <p>${docs ? 'Ask what a part is for, how to operate it, or how to maintain it. You can also attach a photo.'
              : 'Add manuals, drawings and photos in the Library, then ask about any part of the factory.'}</p>
    <div class="chips">${prompts.map((p) => `<button class="chip">${esc(p)}</button>`).join('')}
      ${docs ? '' : '<button class="chip" data-go="library">Open the Library</button>'}</div>
  </div>`;
  $$('.chip', box).forEach((chip) => chip.addEventListener('click', () => {
    if (chip.dataset.go) return show(chip.dataset.go);
    send(chip.textContent);
  }));
}

/* ---------- messages ---------- */
function scrollDown() {
  const box = $('#messages');
  if (box.scrollHeight - box.scrollTop - box.clientHeight < 160) box.scrollTop = box.scrollHeight;
}
function clearEmpty() { const e = $('#messages .empty'); if (e) e.remove(); }

function addUserMessage(text, imageUrl) {
  clearEmpty();
  const el = document.createElement('div');
  el.className = 'msg user';
  if (imageUrl) { const img = document.createElement('img'); img.src = imageUrl; img.alt = 'Attached photo'; el.append(img); }
  if (text) { const b = document.createElement('div'); b.className = 'bubble'; b.textContent = text; el.append(b); }
  $('#messages').append(el);
}

function addAssistantMessage() {
  clearEmpty();
  const el = document.createElement('div');
  el.className = 'msg assistant';
  el.innerHTML = '<div class="status"><span class="dots"><i></i><i></i><i></i></span><span class="label"></span></div><div class="body"></div><div class="extras"></div>';
  $('#messages').append(el);
  const body = $('.body', el);
  const extras = $('.extras', el);
  let text = '';
  let sources = [];
  let frame = 0;
  const paint = () => { frame = 0; body.innerHTML = markdown(text); scrollDown(); };

  body.addEventListener('click', (e) => {
    const cite = e.target.closest('.cite');
    if (!cite) return;
    const card = $(`.source[data-n="${cite.dataset.n}"]`, el);
    if (!card) return;
    card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    card.classList.add('flash');
    setTimeout(() => card.classList.remove('flash'), 1200);
  });

  function renderSources(parts, warning) {
    // Once the answer exists, show only the sources it actually cites.
    const cited = new Set([...text.matchAll(/\[(\d{1,2})\]/g)].map((m) => Number(m[1])));
    const shown = cited.size ? sources.filter((s) => cited.has(s.n)) : sources;
    let html = '';
    if (warning) html += `<p class="notice">${esc(warning)}</p>`;
    if (parts && parts.length) {
      html += `<div class="part-tags">${parts.map((p) => `<button class="tag" data-code="${esc(p.code)}" title="${esc(p.name)}">${esc(p.code)}</button>`).join('')}</div>`;
    }
    if (shown.length) {
      html += `<div class="sources">${shown.map((s) => {
        const href = `/api/documents/${s.doc_id}/file${s.page ? `#page=${s.page}` : ''}`;
        const thumb = s.has_image ? `<img src="/api/chunks/${s.chunk_id}/image" alt="" loading="lazy">` : `<span class="num">${s.n}</span>`;
        return `<a class="source" data-n="${s.n}" href="${href}" target="_blank" rel="noopener">${thumb}
          <span class="meta"><span class="name">${s.has_image ? `${s.n} · ` : ''}${esc(s.filename)}${s.page ? ` · p.${s.page}` : ''}</span>
          <span class="snip">${esc(s.snippet)}</span></span></a>`;
      }).join('')}</div>`;
    }
    extras.innerHTML = html;
    $$('.tag', extras).forEach((t) => t.addEventListener('click', () => openPart(t.dataset.code)));
    scrollDown();
  }

  let lastParts = [];
  let lastWarning = null;
  return {
    setStatus(label) { $('.status .label', el).textContent = label; },
    addToken(t) { text += t; $('.status', el).hidden = true; if (!frame) frame = requestAnimationFrame(paint); },
    setText(t) { text = t; $('.status', el).hidden = true; body.innerHTML = markdown(text); },
    setSources(list, parts, warning) { sources = list || []; lastParts = parts; lastWarning = warning; renderSources(parts, warning); },
    finish() { $('.status', el).hidden = true; if (frame) { cancelAnimationFrame(frame); paint(); } renderSources(lastParts, lastWarning); },
    fail(message) {
      $('.status', el).hidden = true;
      const err = document.createElement('div');
      err.className = 'error';
      err.textContent = message;
      el.append(err);
      scrollDown();
    },
  };
}

async function* readEvents(resp) {
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf('\n\n')) >= 0) {
      const raw = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      let event = 'message';
      let data = '';
      for (const line of raw.split('\n')) {
        if (line.startsWith('event: ')) event = line.slice(7);
        else if (line.startsWith('data: ')) data += line.slice(6);
      }
      if (data) yield { event, data: JSON.parse(data) };
    }
  }
}

async function send(text) {
  text = (text ?? $('#input').value).trim();
  const image = state.image;
  if (state.busy || (!text && !image)) return;
  state.busy = true;
  $('#input').value = '';
  autosize();
  setImage(null);
  updateSend();

  addUserMessage(text, image ? URL.createObjectURL(image) : null);
  const ui = addAssistantMessage();
  ui.setStatus('Thinking');
  $('#messages').scrollTop = $('#messages').scrollHeight;
  const isNew = !state.convId;

  const form = new FormData();
  form.append('message', text);
  if (state.convId) form.append('conversation_id', state.convId);
  if (image) form.append('image', image, image.name || 'photo.jpg');

  try {
    const resp = await fetch('/api/chat', { method: 'POST', body: form });
    if (!resp.ok) {
      let detail = resp.statusText;
      try { detail = (await resp.json()).detail || detail; } catch { /* not JSON */ }
      throw new Error(detail);
    }
    for await (const { event, data } of readEvents(resp)) {
      if (event === 'meta') state.convId = data.conversation_id;
      else if (event === 'status') ui.setStatus(data.text);
      else if (event === 'sources') ui.setSources(data.sources, data.parts, data.warning);
      else if (event === 'token') ui.addToken(data.text);
      else if (event === 'error') ui.fail(data.message);
    }
    ui.finish();
  } catch (err) {
    ui.fail(`Couldn't reach the assistant. ${err.message}`);
  } finally {
    state.busy = false;
    updateSend();
    if (isNew) { $('#chatTitle').textContent = (text || 'Photo question').slice(0, 60); }
    loadConversations();
  }
}

/* ---------- composer ---------- */
const input = $('#input');
function autosize() { input.style.height = 'auto'; input.style.height = `${Math.min(input.scrollHeight, 180)}px`; }
function updateSend() { $('#sendBtn').disabled = state.busy || (!input.value.trim() && !state.image); }
function setImage(file) {
  state.image = file;
  $('#attachment').hidden = !file;
  if (file) {
    $('#attachmentImg').src = URL.createObjectURL(file);
    $('#attachmentName').textContent = file.name || 'Photo';
  }
  $('#imageInput').value = '';
  updateSend();
}
input.addEventListener('input', () => { autosize(); updateSend(); });
input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
});
input.addEventListener('paste', (e) => {
  const file = [...(e.clipboardData?.files || [])].find((f) => f.type.startsWith('image/'));
  if (file) { e.preventDefault(); setImage(file); }
});
$('#composer').addEventListener('submit', (e) => { e.preventDefault(); send(); });
$('#attachBtn').addEventListener('click', () => $('#imageInput').click());
$('#imageInput').addEventListener('change', (e) => { if (e.target.files[0]) setImage(e.target.files[0]); });
$('#attachmentClear').addEventListener('click', () => setImage(null));

/* ---------- sheet ---------- */
function openSheet(html) {
  $('#sheetBody').innerHTML = html;
  $('#sheetBackdrop').hidden = false;
}
function closeSheet() { $('#sheetBackdrop').hidden = true; }
$('#sheetClose').addEventListener('click', closeSheet);
$('#sheetBackdrop').addEventListener('click', (e) => { if (e.target.id === 'sheetBackdrop') closeSheet(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeSheet(); });

/* ---------- parts ---------- */
let partTimer;
async function loadParts() {
  const q = $('#partSearch').value.trim();
  const { parts } = await api(`/api/ics?q=${encodeURIComponent(q)}`);
  const grid = $('#partGrid');
  if (!parts.length) {
    grid.innerHTML = `<div class="blank">${q ? 'No parts match your search.' : 'No parts yet. Import the ICS code list as a CSV file, or add parts one by one.'}</div>`;
    return;
  }
  grid.innerHTML = parts.map((p) => `<button class="part" data-code="${esc(p.code)}">
    <span class="tag">${esc(p.code)}</span>
    <span class="title">${esc(p.name || 'Unnamed part')}</span>
    <span class="desc">${esc(p.description)}</span>
    ${p.area ? `<span class="area">${esc(p.area)}</span>` : ''}</button>`).join('');
  $$('.part', grid).forEach((b) => b.addEventListener('click', () => openPart(b.dataset.code)));
}
$('#partSearch').addEventListener('input', () => { clearTimeout(partTimer); partTimer = setTimeout(loadParts, 180); });

async function openPart(code) {
  const p = await api(`/api/ics/${encodeURIComponent(code)}`);
  const extra = Object.entries(p.extra || {});
  openSheet(`
    <span class="tag">${esc(p.code)}</span>
    <h2>${esc(p.name || (p.unregistered ? 'Not in the registry yet' : 'Unnamed part'))}</h2>
    <p>${esc(p.description)}</p>
    ${(p.area || p.parent || extra.length) ? `<dl class="kv">
      ${p.area ? `<dt>Area</dt><dd>${esc(p.area)}</dd>` : ''}
      ${p.parent ? `<dt>Part of</dt><dd><button class="tag" data-code="${esc(p.parent)}">${esc(p.parent)}</button></dd>` : ''}
      ${extra.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl>` : ''}
    ${p.children.length ? `<h3>Contains</h3><div class="part-tags">${p.children.map((c) => `<button class="tag" data-code="${esc(c.code)}" title="${esc(c.name)}">${esc(c.code)}</button>`).join('')}</div>` : ''}
    ${p.images.length ? `<h3>Images</h3><div class="thumbs">${p.images.map((i) => `<a href="/api/chunks/${i.chunk_id}/image" target="_blank" rel="noopener"><img src="/api/chunks/${i.chunk_id}/image" alt="${esc(i.filename)}" loading="lazy"></a>`).join('')}</div>` : ''}
    <h3>Documents</h3>
    ${p.documents.length ? `<div class="links">${p.documents.map((d) => `<a href="/api/documents/${d.id}/file" target="_blank" rel="noopener">${esc(d.filename)}</a>`).join('')}</div>` : '<p class="muted">Nothing in the library mentions this code yet.</p>'}
    <div class="actions">
      <button class="btn primary" id="askPart">Ask about this part</button>
      ${p.unregistered ? '' : '<button class="btn danger" id="deletePart">Remove from registry</button>'}
    </div>`);
  $$('#sheetBody .tag[data-code]').forEach((t) => t.addEventListener('click', () => openPart(t.dataset.code)));
  $('#askPart').addEventListener('click', async () => {
    closeSheet();
    await newChat();
    send(`What is ${p.code} for, and how do I operate and maintain it?`);
  });
  const del = $('#deletePart');
  if (del) del.addEventListener('click', async () => {
    if (!confirm(`Remove ${p.code} from the registry? Documents are kept.`)) return;
    await api(`/api/ics/${encodeURIComponent(p.code)}`, { method: 'DELETE' });
    closeSheet(); loadParts();
  });
}

$('#addPartBtn').addEventListener('click', () => {
  openSheet(`<h2>Add a part</h2>
    <form id="partForm">
      <label>ICS code<input name="code" required autocomplete="off"></label>
      <label>Name<input name="name" autocomplete="off"></label>
      <label>What it is for<textarea name="description"></textarea></label>
      <label>Area<input name="area" autocomplete="off"></label>
      <label>Part of (parent ICS code)<input name="parent" autocomplete="off"></label>
      <div class="actions"><button class="btn primary" type="submit">Add part</button></div>
    </form>`);
  $('#partForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      await api('/api/ics', { method: 'POST', json: Object.fromEntries(new FormData(e.target)) });
      closeSheet(); loadParts(); toast('Part added');
    } catch (err) { toast(err.message); }
  });
});
$('#importIcsBtn').addEventListener('click', () => $('#icsFile').click());
$('#icsFile').addEventListener('change', async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const form = new FormData();
  form.append('file', file);
  try {
    const { imported } = await api('/api/ics/import', { method: 'POST', body: form });
    toast(`Imported ${imported} parts`);
    loadParts();
  } catch (err) { toast(err.message); }
  e.target.value = '';
});

/* ---------- library ---------- */
const fmtSize = (n) => (n > 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`);
async function loadDocs() {
  const { documents } = await api('/api/documents');
  const list = $('#docList');
  list.innerHTML = documents.map((d) => {
    const sub = d.status === 'processing' ? 'Reading and indexing…'
      : d.status === 'error' ? esc(d.error || 'Failed')
      : `${fmtSize(d.size)} · ${d.n_chunks} ${d.n_chunks === 1 ? 'passage' : 'passages'}${d.codes.length ? ` · ${esc(d.codes.join(', '))}` : ''}`;
    const thumb = d.kind === 'image' && d.status !== 'error'
      ? `<img class="thumb" src="/api/documents/${d.id}/file" alt="" loading="lazy">`
      : `<span class="thumb">${icon('library')}</span>`;
    return `<div class="doc">${thumb}
      <div class="info"><a class="name" href="/api/documents/${d.id}/file" target="_blank" rel="noopener">${esc(d.filename)}</a>
      <div class="sub${d.status === 'error' ? ' err' : ''}">${sub}</div></div>
      <button class="icon-btn" data-del="${d.id}" aria-label="Delete ${esc(d.filename)}">${icon('x')}</button></div>`;
  }).join('');
  $$('[data-del]', list).forEach((b) => b.addEventListener('click', async () => {
    if (!confirm('Delete this file from the library?')) return;
    await api(`/api/documents/${b.dataset.del}`, { method: 'DELETE' });
    loadDocs(); loadStatus();
  }));
  clearTimeout(state.pollTimer);
  const working = documents.some((d) => d.status === 'processing') || (state.status && state.status.reindex.running);
  if (working && $('#view-library').classList.contains('active')) {
    state.pollTimer = setTimeout(() => { loadDocs(); loadStatus(); }, 1500);
  }
}
async function uploadFiles(files) {
  if (!files.length) return;
  const form = new FormData();
  for (const f of files) form.append('files', f);
  form.append('codes', $('#uploadCodes').value);
  try {
    const { created, rejected } = await api('/api/documents', { method: 'POST', body: form });
    if (rejected.length) toast(`${rejected[0].filename}: ${rejected[0].reason}`);
    else toast(`Added ${created.length} ${created.length === 1 ? 'file' : 'files'}`);
  } catch (err) { toast(err.message); }
  $('#fileInput').value = '';
  loadDocs(); loadStatus();
}
const drop = $('#drop');
$('#browseBtn').addEventListener('click', () => $('#fileInput').click());
$('#fileInput').addEventListener('change', (e) => uploadFiles([...e.target.files]));
['dragenter', 'dragover'].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add('over'); }));
['dragleave', 'drop'].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
drop.addEventListener('drop', (e) => uploadFiles([...e.dataTransfer.files]));

async function startReindex() {
  await api('/api/reindex', { method: 'POST' });
  toast('Re-indexing the library');
  const tick = async () => { const s = await loadStatus(); if (s.reindex.running) setTimeout(tick, 1200); else if (s.reindex.error) toast(s.reindex.error); };
  setTimeout(tick, 600);
}
$('#reindexBtn').addEventListener('click', startReindex);

/* ---------- settings ---------- */
const ROLES = [
  ['chat', 'Chat model', 'Writes the answers'],
  ['vision', 'Vision model', 'Reads photos and drawings'],
  ['embedding', 'Embedding model', 'Powers search'],
];
async function loadSettings() {
  const s = state.settings = await api('/api/settings');
  const note = $('#privacyNote');
  note.className = `banner ${s.private_mode ? 'local' : 'cloud'}`;
  note.innerHTML = s.private_mode
    ? `${icon('lock')}<span>Private mode is on. Only models running on this network can be used, so no factory data leaves it.</span>`
    : `${icon('cloud')}<span>Private mode is off. Cloud providers are allowed: questions, document text and photos are sent to them.</span>`;
  $('#roleCard').innerHTML = ROLES.map(([id, label, help]) => `
    <div class="row"><span>${label}<small>${help}</small></span>
      <div class="role-fields">
        <select data-role="${id}" aria-label="${label} provider">${s.provider_list.map((p) =>
          `<option value="${p.id}"${p.id === s.roles[id].provider ? ' selected' : ''}${s.private_mode && !p.local ? ' disabled' : ''}>${esc(p.label)}</option>`).join('')}</select>
        <input data-model="${id}" list="models-${id}" value="${esc(s.roles[id].model)}" aria-label="${label} name" spellcheck="false" autocomplete="off">
        <datalist id="models-${id}"></datalist>
      </div></div>`).join('');
  for (const [id] of ROLES) {
    const select = $(`[data-role="${id}"]`);
    fillModels(id, select.value);
    select.addEventListener('change', () => {
      const first = (s.suggested[select.value] || {})[id] || [];
      $(`[data-model="${id}"]`).value = first[0] || '';
      fillModels(id, select.value);
    });
  }
  $('#ollamaUrl').value = s.providers.ollama.base_url;
  $('#openaiUrl').value = s.providers.openai.base_url;
  $('#googleKey').value = '';
  $('#openaiKey').value = '';
  $('#googleKey').placeholder = s.providers.google.api_key_set ? 'Saved ••••••' : 'Not set';
  $('#openaiKey').placeholder = s.providers.openai.api_key_set ? 'Saved ••••••' : 'Not set';
  $('#icsPattern').value = s.ics_pattern;
  $('#topK').value = s.top_k;
}
async function fillModels(role, provider) {
  const list = $(`#models-${role}`);
  const suggested = ((state.settings.suggested[provider] || {})[role]) || [];
  const render = (names) => { list.innerHTML = names.map((n) => `<option value="${esc(n)}">`).join(''); };
  render(suggested);
  try {
    const { models } = await api(`/api/models?provider=${provider}`);
    render([...new Set([...suggested, ...models])]);
  } catch { /* suggestions only */ }
}
$('#saveSettings').addEventListener('click', async () => {
  const roles = {};
  for (const [id] of ROLES) roles[id] = { provider: $(`[data-role="${id}"]`).value, model: $(`[data-model="${id}"]`).value };
  try {
    await api('/api/settings', { method: 'PUT', json: {
      roles,
      providers: {
        ollama: { base_url: $('#ollamaUrl').value },
        google: { api_key: $('#googleKey').value },
        openai: { base_url: $('#openaiUrl').value, api_key: $('#openaiKey').value },
      },
      ics_pattern: $('#icsPattern').value,
      top_k: Number($('#topK').value) || 6,
    } });
    const s = await loadStatus();
    await loadSettings();
    toast(s.stale_chunks > 0 ? 'Saved. Re-index the library to use the new embedding model.' : 'Settings saved');
  } catch (err) { toast(err.message); }
});

/* ---------- start ---------- */
(async function start() {
  try { await loadStatus(); } catch { toast('The server is not responding.'); }
  await renderEmpty();
  loadConversations();
  input.focus();
})();
