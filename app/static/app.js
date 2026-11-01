'use strict';

/* ------------------------------------------------------------------ helpers */
const $ = (sel) => document.querySelector(sel);

// Tiny DOM builder. Text is always inserted as text nodes, never as HTML.
function h(tag, props = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'value' || k === 'checked' || k === 'disabled') el[k] = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const fmtDist = (m) => (m < 1000 ? `${Math.round(m)} m` : `${(m / 1000).toFixed(2)} km`);
function fmtDur(s) {
  if (s == null) return '';
  const hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60);
  return hh ? `${hh} h ${mm} min` : `${mm} min`;
}
function fmtTime(iso) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleString();
}

let toastTimer;
function toast(msg, isError = false) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'show' + (isError ? ' error' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = ''), isError ? 6000 : 3500);
}

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = `${r.status} ${r.statusText}`;
    try { const j = await r.json(); if (j.detail) msg = j.detail; } catch { /* not JSON */ }
    throw new Error(msg);
  }
  return r;
}
const getJson = async (p, o) => (await api(p, o)).json();
const post = (p, body = {}) =>
  getJson(p, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

function haversine(a, b) {
  const R = 6371008.8, rad = Math.PI / 180;
  const dLat = (b[0] - a[0]) * rad, dLon = (b[1] - a[1]) * rad;
  const s = Math.sin(dLat / 2) ** 2 + Math.cos(a[0] * rad) * Math.cos(b[0] * rad) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(s)));
}

function downloadBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  const a = h('a', { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

/* -------------------------------------------------------------------- state */
const PALETTE = ['#2563eb', '#dc2626', '#059669', '#9333ea', '#d97706', '#0891b2', '#db2777', '#4b5563'];
const state = {
  files: [],
  cur: null,      // current file detail from the API
  active: -1,     // index of the active track
  selStart: 0,
  selEnd: 0,
  cum: [],        // cumulative distance (m) along the active track
  pick: null,     // 'start' | 'end' | 'wpt' | null
};
const activeTrack = () => (state.cur && state.active >= 0 ? state.cur.tracks[state.active] : null);

/* ---------------------------------------------------------------------- map */
const map = L.map('map', { preferCanvas: true });
const osm = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19, attribution: '&copy; OpenStreetMap contributors',
});
const topo = L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png', {
  maxZoom: 17, subdomains: 'abc',
  attribution: 'Map data &copy; OpenStreetMap contributors, SRTM | Style &copy; OpenTopoMap (CC-BY-SA)',
});
osm.addTo(map);
L.control.layers({ OpenStreetMap: osm, OpenTopoMap: topo }).addTo(map);
map.setView([54, -2], 5);
const trackLayer = L.layerGroup().addTo(map);
const wptLayer = L.layerGroup().addTo(map);
const selLayer = L.layerGroup().addTo(map);

const ll = (p) => [p[0], p[1]];
const textEl = (s) => h('span', {}, s);

function pinIcon(letter, cls) {
  return L.divIcon({ className: 'pin-icon', html: `<div class="pin ${cls}">${letter}</div>`, iconSize: [26, 26], iconAnchor: [13, 13] });
}

function renderMap(fit) {
  trackLayer.clearLayers();
  wptLayer.clearLayers();
  const d = state.cur;
  const order = d.tracks.map((_, i) => i).sort((a, b) => (a === state.active) - (b === state.active));
  for (const i of order) {
    const t = d.tracks[i];
    const active = i === state.active;
    const line = L.polyline(t.points.map(ll), {
      color: PALETTE[i % PALETTE.length], weight: active ? 5 : 3, opacity: active ? 0.95 : 0.55,
    });
    line.on('click', () => { if (!state.pick && i !== state.active) activateTrack(i); });
    line.bindTooltip(textEl(t.name), { sticky: true });
    line.addTo(trackLayer);
  }
  for (const w of d.waypoints) {
    const m = L.marker([w.lat, w.lon]);
    const pop = h('div', {}, h('b', {}, w.name || '(unnamed)'));
    if (w.desc) pop.append(h('div', {}, w.desc));
    if (w.ele != null) pop.append(h('div', { class: 'meta' }, `${Math.round(w.ele)} m`));
    m.bindPopup(pop);
    m.addTo(wptLayer);
  }
  if (fit && d.stats.bounds) map.fitBounds(d.stats.bounds, { padding: [30, 30], maxZoom: 16 });
  drawSelection();
}

function drawSelection() {
  selLayer.clearLayers();
  const t = activeTrack();
  if (!t) return;
  const n = t.points.length, s = state.selStart, e = state.selEnd;
  if (!(s === 0 && e === n - 1)) {
    L.polyline(t.points.slice(s, e + 1).map(ll), { color: '#f59e0b', weight: 8, opacity: 0.9, interactive: false }).addTo(selLayer);
  }
  const mk = (idx, letter, cls, setter) => {
    const m = L.marker(ll(t.points[idx]), { icon: pinIcon(letter, cls), draggable: true, zIndexOffset: 1000 });
    m.on('dragend', () => { setter(nearestIndex(m.getLatLng())); refreshSelection(); });
    m.addTo(selLayer);
  };
  mk(s, 'A', 'a', setStart);
  mk(e, 'B', 'b', setEnd);
}

function nearestIndex(latlng) {
  const t = activeTrack();
  const k = Math.cos((latlng.lat * Math.PI) / 180);
  let best = 0, bestD = Infinity;
  t.points.forEach((p, i) => {
    const dx = (p[1] - latlng.lng) * k, dy = p[0] - latlng.lat;
    const dd = dx * dx + dy * dy;
    if (dd < bestD) { bestD = dd; best = i; }
  });
  return best;
}

/* ---------------------------------------------------------------- selection */
function setStart(i) { state.selStart = i; if (state.selEnd < i) state.selEnd = i; }
function setEnd(i) { state.selEnd = i; if (state.selStart > i) state.selStart = i; }

let rafPending = false;
function refreshSelection() {
  if (rafPending) return;
  rafPending = true;
  requestAnimationFrame(() => {
    rafPending = false;
    drawSelection();
    renderSelectionPanel();
    drawProfile();
  });
}

function computeCum() {
  const t = activeTrack();
  state.cum = [];
  if (!t) return;
  let acc = 0;
  state.cum = t.points.map((p, i) => (i === 0 ? 0 : (acc += haversine(t.points[i - 1], p))));
}

function resetSelection() {
  const t = activeTrack();
  state.selStart = 0;
  state.selEnd = t ? t.points.length - 1 : 0;
}

function pointLabel(i) {
  const t = activeTrack(), p = t.points[i];
  let s = `Point ${i + 1} of ${t.points.length} · ${fmtDist(state.cum[i])} from start`;
  if (p[2] != null) s += ` · ${Math.round(p[2])} m`;
  if (p[3]) s += ` · ${fmtTime(p[3])}`;
  return s;
}

function renderSelectionPanel() {
  const t = activeTrack();
  $('#selection-panel').hidden = !t;
  if (!t) return;
  const n = t.points.length, s = state.selStart, e = state.selEnd;
  for (const [id, v] of [['#sel-start', s], ['#sel-end', e]]) {
    const el = $(id);
    el.max = n - 1;
    el.value = v;
  }
  $('#lbl-start').textContent = pointLabel(s);
  $('#lbl-end').textContent = pointLabel(e);
  const whole = s === 0 && e === n - 1;
  $('#sel-summary').textContent =
    `Selected A–B: ${e - s + 1} points · ${fmtDist(state.cum[e] - state.cum[s])}` + (whole ? ' (whole track)' : '');
  $('#btn-crop').disabled = whole || e - s < 1;
  $('#btn-del-split').disabled = whole;
  $('#btn-del-join').disabled = whole;
  $('#btn-split').disabled = s === 0 || s === n - 1;
}

/* ---------------------------------------------------------- elevation chart */
function drawProfile() {
  const t = activeTrack();
  const wrap = $('#profile-wrap');
  wrap.hidden = !t;
  if (!t) return;
  const cv = $('#profile');
  const dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth, H = cv.clientHeight;
  if (!W || !H) return;
  cv.width = W * dpr;
  cv.height = H * dpr;
  const g = cv.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, W, H);
  const css = getComputedStyle(document.body);
  const muted = css.getPropertyValue('--muted').trim() || '#888';
  const accent = css.getPropertyValue('--accent').trim() || '#2563eb';

  const idx = [];
  t.points.forEach((p, i) => { if (p[2] != null) idx.push(i); });
  if (idx.length < 2) {
    g.fillStyle = muted;
    g.font = '12px system-ui, sans-serif';
    g.fillText('No elevation data in this track', 10, H / 2);
    return;
  }
  const total = state.cum[state.cum.length - 1] || 1;
  let lo = Infinity, hi = -Infinity;
  for (const i of idx) { lo = Math.min(lo, t.points[i][2]); hi = Math.max(hi, t.points[i][2]); }
  if (hi - lo < 10) { hi += 5; lo -= 5; }
  const padT = 6, padB = 16, padL = 4;
  const X = (i) => padL + (state.cum[i] / total) * (W - padL - 4);
  const Y = (e) => padT + (1 - (e - lo) / (hi - lo)) * (H - padT - padB);
  const step = Math.max(1, Math.floor(idx.length / (W * 2)));

  const path = (from, to) => {
    g.beginPath();
    let first = true, lastI = from;
    for (let k = 0; k < idx.length; k += step) {
      const i = idx[k];
      if (i < from || i > to) continue;
      if (first) { g.moveTo(X(i), Y(t.points[i][2])); first = false; } else g.lineTo(X(i), Y(t.points[i][2]));
      lastI = i;
    }
    return [first, lastI];
  };

  g.globalAlpha = 0.9;
  g.strokeStyle = muted;
  g.lineWidth = 1.5;
  path(0, t.points.length - 1);
  g.stroke();

  const s = state.selStart, e = state.selEnd;
  if (!(s === 0 && e === t.points.length - 1)) {
    g.strokeStyle = '#f59e0b';
    g.lineWidth = 3;
    path(s, e);
    g.stroke();
  }
  g.globalAlpha = 1;
  for (const [i, col] of [[s, '#10b981'], [e, '#ef4444']]) {
    g.strokeStyle = col;
    g.lineWidth = 2;
    g.beginPath();
    g.moveTo(X(i), padT - 2);
    g.lineTo(X(i), H - padB);
    g.stroke();
  }
  g.fillStyle = muted;
  g.font = '11px system-ui, sans-serif';
  g.fillText(`${Math.round(hi)} m`, padL + 2, padT + 9);
  g.fillText(`${Math.round(lo)} m`, padL + 2, H - padB - 3);
  g.fillText('0', padL, H - 3);
  const endLbl = fmtDist(total);
  g.fillText(endLbl, W - 6 - g.measureText(endLbl).width, H - 3);
  g.fillStyle = accent;
}

$('#profile').addEventListener('click', (ev) => {
  const t = activeTrack();
  if (!t) return;
  const r = ev.target.getBoundingClientRect();
  const total = state.cum[state.cum.length - 1] || 1;
  const dist = Math.max(0, Math.min(1, (ev.clientX - r.left - 4) / (r.width - 8))) * total;
  // binary search for the nearest point by distance
  let lo = 0, hi = state.cum.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (state.cum[mid] < dist) lo = mid + 1; else hi = mid;
  }
  const i = lo > 0 && dist - state.cum[lo - 1] < state.cum[lo] - dist ? lo - 1 : lo;
  // move whichever handle is closer to the click
  if (Math.abs(state.cum[i] - state.cum[state.selStart]) <= Math.abs(state.cum[i] - state.cum[state.selEnd])) setStart(i);
  else setEnd(i);
  refreshSelection();
});

/* ---------------------------------------------------------------- rendering */
function renderTitle() {
  const d = state.cur, st = d.stats;
  $('#file-title').textContent = d.name;
  const bits = [
    `${st.tracks} track${st.tracks === 1 ? '' : 's'}`,
    `${st.waypoints} waypoint${st.waypoints === 1 ? '' : 's'}`,
    fmtDist(st.distance_m),
  ];
  if (st.ascent_m || st.descent_m) bits.push(`↑ ${st.ascent_m} m  ↓ ${st.descent_m} m`);
  if (st.duration_s) bits.push(fmtDur(st.duration_s));
  if (st.start_time) bits.push(fmtTime(st.start_time));
  bits.push(`imported as ${d.source_format.toUpperCase()}`);
  $('#file-meta').textContent = bits.join(' · ');
  $('#btn-undo').disabled = !d.can_undo;
  $('#btn-gpx').href = `/api/files/${d.id}/export?format=gpx`;
  $('#btn-kml').href = `/api/files/${d.id}/export?format=kml`;
}

function renderLists() {
  const d = state.cur;
  const tl = $('#track-list');
  tl.replaceChildren();
  if (!d.tracks.length) tl.append(h('li', { class: 'meta' }, 'No tracks in this file.'));
  d.tracks.forEach((t, i) => {
    const st = t.stats;
    const sub = [`${st.points} points`, fmtDist(st.distance_m)];
    if (st.ascent_m) sub.push(`↑ ${st.ascent_m} m`);
    if (st.duration_s) sub.push(fmtDur(st.duration_s));
    tl.append(
      h('li', { class: i === state.active ? 'active' : '' },
        h('div', { class: 'info', onclick: () => activateTrack(i) },
          h('b', {}, h('span', { class: 'swatch', style: `background:${PALETTE[i % PALETTE.length]}` }), t.name),
          h('span', { class: 'meta' }, sub.join(' · '))),
        h('button', { onclick: () => renameTrack(i) }, 'Rename'),
        h('button', { onclick: () => doEdit({ op: 'reverse', track: i }, 'Track reversed (timestamps removed)') }, 'Reverse'),
        i < d.tracks.length - 1 &&
          h('button', { title: 'Join this track and the next one', onclick: () => doEdit({ op: 'merge', tracks: [i, i + 1] }, 'Tracks merged') }, 'Merge ↓'),
        h('button', { class: 'danger', onclick: () => confirm(`Delete track "${t.name}"?`) && doEdit({ op: 'delete_track', track: i }, 'Track deleted') }, 'Delete')));
  });

  const wl = $('#wpt-list');
  wl.replaceChildren();
  if (!d.waypoints.length) wl.append(h('li', { class: 'meta' }, 'No waypoints.'));
  d.waypoints.forEach((w, i) => {
    wl.append(
      h('li', {},
        h('div', { class: 'info', onclick: () => map.setView([w.lat, w.lon], Math.max(map.getZoom(), 15)) },
          h('b', {}, w.name || '(unnamed)'),
          h('span', { class: 'meta' }, w.desc || `${w.lat.toFixed(5)}, ${w.lon.toFixed(5)}`)),
        h('button', { onclick: () => editWaypoint(i) }, 'Edit'),
        h('button', { class: 'danger', onclick: () => doEdit({ op: 'delete_waypoint', waypoint: i }, 'Waypoint deleted') }, 'Delete')));
  });
}

function renderFileList() {
  const ul = $('#file-list');
  ul.replaceChildren();
  if (!state.files.length) ul.append(h('li', { class: 'hint' }, 'The library is empty.'));
  for (const f of state.files) {
    ul.append(
      h('li', {},
        h('button', { class: 'file-item' + (state.cur && state.cur.id === f.id ? ' active' : ''), onclick: () => openFile(f.id) },
          h('span', { class: 'name' }, f.name, h('span', { class: 'badge' }, f.source_format)),
          h('span', { class: 'sub' }, `${fmtDist(f.distance_m || 0)} · ${f.n_tracks} track${f.n_tracks === 1 ? '' : 's'} · ${f.n_waypoints} wpt`))));
  }
}

function renderAll(fit) {
  $('#empty').hidden = true;
  $('#editor').hidden = false;
  map.invalidateSize();
  renderTitle();
  renderMap(fit);
  renderLists();
  renderSelectionPanel();
  drawProfile();
  renderFileList();
}

/* ------------------------------------------------------------------ actions */
async function loadList() {
  state.files = (await getJson('/api/files')).files;
  renderFileList();
}

function setCurrent(d, { fit, keepActive }) {
  state.cur = d;
  state.active = d.tracks.length ? (keepActive ? Math.min(Math.max(state.active, 0), d.tracks.length - 1) : 0) : -1;
  cancelPick();
  computeCum();
  resetSelection();
  renderAll(fit);
  if (fit && activeTrack()) map.fitBounds(L.latLngBounds(activeTrack().points.map(ll)), { padding: [30, 30] });
}

async function openFile(id) {
  try {
    setCurrent(await getJson(`/api/files/${id}`), { fit: true, keepActive: false });
  } catch (e) { toast(e.message, true); }
}

function activateTrack(i) {
  state.active = i;
  cancelPick();
  computeCum();
  resetSelection();
  renderMap(false);
  renderLists();
  renderSelectionPanel();
  drawProfile();
  map.fitBounds(L.latLngBounds(activeTrack().points.map(ll)), { padding: [30, 30] });
}

async function doEdit(body, okMsg) {
  try {
    const d = await post(`/api/files/${state.cur.id}/edit`, body);
    setCurrent(d, { fit: false, keepActive: true });
    if (okMsg) toast(okMsg);
    loadList();
  } catch (e) { toast(e.message, true); }
}

function selBody(op, extra = {}) {
  return { op, track: state.active, start: state.selStart, end: state.selEnd, ...extra };
}

function renameTrack(i) {
  const name = prompt('Track name', state.cur.tracks[i].name);
  if (name && name.trim()) doEdit({ op: 'rename_track', track: i, name }, 'Track renamed');
}

function editWaypoint(i) {
  const w = state.cur.waypoints[i];
  const name = prompt('Waypoint name', w.name);
  if (name === null) return;
  const desc = prompt('Description (optional)', w.desc || '');
  if (desc === null) return;
  doEdit({ op: 'update_waypoint', waypoint: i, name, desc }, 'Waypoint updated');
}

/* ---------------------------------------------------------- map pick modes */
function startPick(mode) {
  if (state.pick === mode) return cancelPick();
  state.pick = mode;
  $('#map').classList.add('picking');
  const hint = $('#pick-hint');
  hint.hidden = false;
  hint.textContent = {
    start: 'Click the map near the track to place point A (Esc to cancel)',
    end: 'Click the map near the track to place point B (Esc to cancel)',
    wpt: 'Click the map to add a waypoint (Esc to cancel)',
  }[mode];
  for (const [id, m] of [['#btn-pick-start', 'start'], ['#btn-pick-end', 'end'], ['#btn-add-wpt', 'wpt']])
    $(id).classList.toggle('on', m === mode);
}

function cancelPick() {
  state.pick = null;
  $('#map').classList.remove('picking');
  $('#pick-hint').hidden = true;
  for (const id of ['#btn-pick-start', '#btn-pick-end', '#btn-add-wpt']) $(id).classList.remove('on');
}

map.on('click', (ev) => {
  const mode = state.pick;
  if (!mode) return;
  if (mode === 'wpt') {
    cancelPick();
    const name = prompt('Waypoint name (optional)', '');
    if (name === null) return;
    doEdit({ op: 'add_waypoint', lat: ev.latlng.lat, lon: ev.latlng.lng, name }, 'Waypoint added');
    return;
  }
  if (!activeTrack()) return cancelPick();
  const i = nearestIndex(ev.latlng);
  if (mode === 'start') setStart(i); else setEnd(i);
  cancelPick();
  refreshSelection();
});
document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') cancelPick(); });

/* ------------------------------------------------------------------ uploads */
async function uploadFiles(fileList) {
  const files = [...fileList];
  if (!files.length) return;
  const fd = new FormData();
  files.forEach((f) => fd.append('files', f, f.name));
  try {
    const { results } = await getJson('/api/files', { method: 'POST', body: fd });
    const ok = results.filter((r) => r.ok), bad = results.filter((r) => !r.ok);
    if (bad.length) toast(bad.map((r) => `${r.filename}: ${r.error}`).join('\n'), true);
    else toast(`Imported ${ok.length} file${ok.length === 1 ? '' : 's'}`);
    await loadList();
    if (ok.length) openFile(ok[0].id);
  } catch (e) { toast(e.message, true); }
}

async function quickConvert(file) {
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file, file.name);
  try {
    const r = await api(`/api/convert?to=${$('#convert-to').value}`, { method: 'POST', body: fd });
    const cd = r.headers.get('content-disposition') || '';
    const m = /filename\*=UTF-8''([^;]+)/i.exec(cd) || /filename="([^"]+)"/i.exec(cd);
    downloadBlob(await r.blob(), m ? decodeURIComponent(m[1]) : 'converted');
  } catch (e) { toast(e.message, true); }
}

/* --------------------------------------------------------------- wiring */
$('#btn-upload').onclick = () => $('#file-input').click();
$('#file-input').onchange = (e) => { uploadFiles(e.target.files); e.target.value = ''; };
$('#btn-convert').onclick = () => $('#convert-input').click();
$('#convert-input').onchange = (e) => { quickConvert(e.target.files[0]); e.target.value = ''; };
$('#btn-export-all').onclick = () => { window.location.href = `/api/export-all?format=${$('#export-all-fmt').value}`; };
$('#btn-rescan').onclick = async () => {
  try {
    const r = await post('/api/rescan');
    await loadList();
    toast(`Rescan done: ${r.added} added, ${r.removed} removed, ${r.rejected} rejected`);
    if (state.cur && !state.files.some((f) => f.id === state.cur.id)) showEmpty();
  } catch (e) { toast(e.message, true); }
};

function showEmpty() {
  state.cur = null; state.active = -1;
  $('#editor').hidden = true;
  $('#empty').hidden = false;
  renderFileList();
}

$('#btn-rename').onclick = () => {
  const name = prompt('File name', state.cur.name);
  if (name && name.trim()) doEdit({ op: 'rename_file', name }, 'Renamed');
};
$('#btn-undo').onclick = async () => {
  try {
    setCurrent(await post(`/api/files/${state.cur.id}/undo`), { fit: false, keepActive: true });
    toast('Undone');
    loadList();
  } catch (e) { toast(e.message, true); }
};
$('#btn-delete-file').onclick = async () => {
  if (!confirm(`Permanently delete "${state.cur.name}" from the library? This cannot be undone.`)) return;
  try {
    await api(`/api/files/${state.cur.id}`, { method: 'DELETE' });
    showEmpty();
    await loadList();
    toast('File deleted');
  } catch (e) { toast(e.message, true); }
};

$('#sel-start').oninput = (e) => { setStart(+e.target.value); refreshSelection(); };
$('#sel-end').oninput = (e) => { setEnd(+e.target.value); refreshSelection(); };
$('#btn-select-all').onclick = () => { resetSelection(); refreshSelection(); };
$('#btn-pick-start').onclick = () => startPick('start');
$('#btn-pick-end').onclick = () => startPick('end');
$('#btn-add-wpt').onclick = () => startPick('wpt');

$('#btn-crop').onclick = () => doEdit(selBody('crop'), 'Track cropped to A–B');
$('#btn-del-split').onclick = () => doEdit(selBody('delete_section', { mode: 'split' }), 'Section removed');
$('#btn-del-join').onclick = () => doEdit(selBody('delete_section', { mode: 'join' }), 'Section removed and ends joined');
$('#btn-split').onclick = () => doEdit({ op: 'split', track: state.active, index: state.selStart }, 'Track split at A');

window.addEventListener('resize', () => { if (state.cur) { map.invalidateSize(); drawProfile(); } });

// drag & drop anywhere on the page
let dragDepth = 0;
window.addEventListener('dragenter', (e) => { if (e.dataTransfer?.types?.includes('Files')) { dragDepth++; document.body.classList.add('dragging'); } });
window.addEventListener('dragleave', () => { if (--dragDepth <= 0) { dragDepth = 0; document.body.classList.remove('dragging'); } });
window.addEventListener('dragover', (e) => e.preventDefault());
window.addEventListener('drop', (e) => {
  e.preventDefault();
  dragDepth = 0;
  document.body.classList.remove('dragging');
  if (e.dataTransfer?.files?.length) uploadFiles(e.dataTransfer.files);
});

loadList().catch((e) => toast(e.message, true));
