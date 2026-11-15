// OBDruid web UI - plain JS, no external dependencies (works offline in the car).
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const HISTORY = 120; // points kept per sparkline

const store = {
  get(key, fallback) {
    try { const v = localStorage.getItem("obd." + key); return v === null ? fallback : JSON.parse(v); }
    catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem("obd." + key, JSON.stringify(value)); } catch { /* storage unavailable */ }
  },
};

// ---------------------------------------------------------------- language
// English texts are the keys. pl.json holds {strings: {english: polish}, patterns: [[regex, replacement]],
// words: {english: polish}}; the same file is used by the server for the printable report.
const i18n = {
  lang: store.get("lang", (navigator.language || "en").toLowerCase().startsWith("pl") ? "pl" : "en"),
  strings: {}, patterns: [], words: null,
};

function t(text, params) {
  if (typeof text !== "string" || !text) return text;
  let out = i18n.strings[text];
  if (out === undefined) {
    out = text;
    for (const [re, to] of i18n.patterns) {
      if (re.test(text)) { out = text.replace(re, (...m) => to.replace(/\$(\d)/g, (_, n) => t(m[Number(n)]))); break; }
    }
  }
  if (params) out = out.replace(/\{(\w+)\}/g, (m, k) => (k in params ? params[k] : m));
  return out;
}

// Replace whole words of a short English summary (history list).
function tWords(text) {
  if (!i18n.words || typeof text !== "string") return text;
  return text.replace(i18n.words.re, m => i18n.words.map[m]);
}

// A health finding in the current language (built from its template and parameters when it has them).
function findingText(f) {
  const fill = tpl => {
    if (!tpl) return "";
    const params = {};
    for (const [k, v] of Object.entries(f.params || {})) params[k] = Array.isArray(v) ? v.map(x => t(x)).join(", ") : t(v);
    return t(tpl, params);
  };
  return {
    title: f.title_t ? fill(f.title_t) : t(f.title),
    detail: f.title_t ? fill(f.detail_t) : t(f.detail || ""),
    area: t(f.area), value: t(f.value || ""),
  };
}

const I18N_ATTRS = ["title", "placeholder", "aria-label"];
function translateStatic() {
  document.documentElement.lang = i18n.lang;
  for (const node of $$("[data-i18n-html]")) {
    const key = node.innerHTML.replace(/\s+/g, " ").trim();
    if (i18n.strings[key] !== undefined) node.innerHTML = i18n.strings[key];
  }
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const node of nodes) {
    const parent = node.parentElement;
    if (!parent || parent.closest("script, style, [data-i18n-html]")) continue;
    const key = node.nodeValue.replace(/\s+/g, " ").trim();
    if (!key || i18n.strings[key] === undefined) continue;
    node.nodeValue = node.nodeValue.replace(node.nodeValue.trim(), i18n.strings[key]);
  }
  for (const attr of I18N_ATTRS) {
    for (const node of $$(`[${attr}]`)) node.setAttribute(attr, t(node.getAttribute(attr)));
  }
}

async function initLanguage() {
  const btn = $("#btn-lang");
  btn.textContent = i18n.lang.toUpperCase();
  btn.addEventListener("click", () => {
    store.set("lang", i18n.lang === "pl" ? "en" : "pl");
    location.reload();  // simplest way to redraw every text
  });
  if (i18n.lang === "en") return;
  try {
    const data = await (await fetch(`${i18n.lang}.json`)).json();
    i18n.strings = data.strings || {};
    i18n.patterns = (data.patterns || []).map(([re, to]) => [new RegExp(re), to]);
    const words = Object.keys(data.words || {});
    if (words.length) {
      const esc = words.sort((a, b) => b.length - a.length).map(w => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
      i18n.words = { map: data.words, re: new RegExp(`(?<![\\w])(${esc.join("|")})(?![\\w])`, "g") };
    }
    translateStatic();
  } catch { i18n.lang = "en"; btn.textContent = "EN"; }
}

const ui = {
  state: "disconnected",
  params: [],          // supported params [{key,label,unit,pid,group,decimals}]
  defaults: [],
  tileDefaults: {},    // key -> default scale and limits from the server
  tiles: {},           // key -> the user's tile settings (type, scale, limits)
  sound: false,
  vehicle: null,
  keys: store.get("keys", null),
  interval: store.get("interval", 0.5),
  paused: false,
  series: {},          // key -> {values: [], min, max}
  ws: null,
  recording: null,     // {keys, rows}
  lastFrame: 0,
  frames: [],
};

// ---------------------------------------------------------------- helpers
async function api(path, options = {}) {
  const opts = { headers: { "Content-Type": "application/json" }, ...options };
  if (opts.body && typeof opts.body !== "string") opts.body = JSON.stringify(opts.body);
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) throw new Error((data && data.detail) || `${res.status} ${res.statusText}`);
  return data;
}

let toastTimer;
function toast(msg, isError = false) {
  const el = $("#toast");
  el.textContent = t(msg);
  el.classList.toggle("err", isError);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), isError ? 6000 : 3000);
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = t(v);
    else node.setAttribute(k, I18N_ATTRS.includes(k) ? t(v) : v);
  }
  for (const c of children) if (c != null) node.append(c);
  return node;
}

function fmt(value, decimals = 1) {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") return value.toLocaleString(i18n.lang === "pl" ? "pl-PL" : "en-US", { maximumFractionDigits: decimals, minimumFractionDigits: 0 });
  return t(String(value));
}

// ---------------------------------------------------------------- tabs
function showTab(name) {
  $$(".tabs button").forEach(b => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  $$(".tab").forEach(t => t.toggleAttribute("data-active", t.id === "tab-" + name));
  store.set("tab", name);
  if (name === "info" && ui.state === "connected") loadInfo();
  if (name === "conn") loadPorts();
  if (name === "conn") loadAdapters();
  if (name === "health" && !$("#health-result").children.length) loadLastHealth();
  if (name === "history") loadHistory();
  if (name === "tests" && ui.state === "connected" && !$(".mon-group")) readTests(true);
}
$$(".tabs button").forEach(b => b.addEventListener("click", () => showTab(b.dataset.tab)));

// ---------------------------------------------------------------- connection state
const STATE_TEXT = { connected: "Connected", connecting: "Connecting…", disconnected: "Disconnected", error: "Connection error" };

function applyState(s) {
  const prev = ui.state;
  ui.state = s.state;
  const pill = $("#conn-pill");
  pill.dataset.state = s.state;
  $("#conn-text").textContent = t(STATE_TEXT[s.state]) + (s.settings && s.settings.simulate && s.state === "connected" ? " " + t("(simulator)") : "");
  pill.title = t(s.error || "");
  const err = $("#conn-error");
  err.hidden = !s.error;
  err.textContent = t(s.error || "");
  $("#btn-connect").disabled = s.state === "connecting";
  $("#btn-disconnect").disabled = s.state !== "connected";
  if (s.settings && prev !== s.state) {
    $("#in-sim").checked = !!s.settings.simulate;
    if (s.settings.port) $("#in-port").value = s.settings.port;
  }
  if (s.vehicle) {
    const changed = ui.vehicle && ui.vehicle.id !== s.vehicle.id;
    ui.vehicle = s.vehicle;
    showLastConnection();
    if (changed && s.state === "connected" && prev === "connected") loadLayout().then(() => { buildTiles(); sendLiveConfig(); });
  }
  const online = s.state === "connected";
  $$("#btn-dtc-read, #btn-dtc-clear, #btn-info, #btn-params, #btn-tests, #btn-trip-reset").forEach(b => (b.disabled = !online));
  $("#btn-health").disabled = !online || healthRunning;
  $("#dash-empty").hidden = online;
  if (online && prev !== "connected") onConnected();
  if (!online && prev === "connected") closeLive();
}

async function pollState() {
  try { applyState(await api("/api/state")); }
  catch { applyState({ state: "error", error: "No connection to the server" }); }
}

async function onConnected() {
  try {
    const data = await api("/api/params");
    ui.params = data.params;
    ui.defaults = data.default;
    ui.tileDefaults = data.tiles || {};
    await loadLayout();
    const supported = new Set(ui.params.map(p => p.key));
    if (!ui.keys || !ui.keys.some(k => supported.has(k))) ui.keys = data.default;
    ui.keys = ui.keys.filter(k => supported.has(k));
    buildTiles();
    openLive();
    readDtc(true);
    if ($("#tab-info").hasAttribute("data-active")) loadInfo();
  } catch (e) { toast(e.message, true); }
}

// ---------------------------------------------------------------- dashboard
function paramOf(key) { return ui.params.find(p => p.key === key) || { key, label: key, unit: "", decimals: 1 }; }

// The layout (parameters, tile types, limits, sound) is stored on the server per vehicle.
async function loadLayout() {
  try {
    const { layout } = await api("/api/dashboard");
    if (layout) {
      ui.keys = layout.keys && layout.keys.length ? layout.keys : ui.keys;
      ui.tiles = layout.tiles || {};
      ui.sound = !!layout.sound;
    } else {
      ui.tiles = {};
    }
  } catch { /* keep what we have */ }
  applySound();
}

let saveTimer;
function saveLayout() {
  store.set("keys", ui.keys);
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    api("/api/dashboard", { method: "PUT", body: { keys: ui.keys, tiles: ui.tiles, sound: ui.sound } })
      .catch(e => toast(t("Could not save the dashboard: {error}", { error: t(e.message) }), true));
  }, 400);
}

const LIMIT_FIELDS = ["min", "max", "warn_low", "warn_high", "alarm_low", "alarm_high"];

function tileConfig(key) {
  const cfg = { type: "value", ...(ui.tileDefaults[key] || {}), ...(ui.tiles[key] || {}) };
  for (const f of LIMIT_FIELDS) if (cfg[f] === undefined) cfg[f] = null;
  return cfg;
}

function alertLevel(value, cfg) {
  if (typeof value !== "number") return null;
  if ((cfg.alarm_high !== null && value >= cfg.alarm_high) || (cfg.alarm_low !== null && value <= cfg.alarm_low)) return "alarm";
  if ((cfg.warn_high !== null && value >= cfg.warn_high) || (cfg.warn_low !== null && value <= cfg.warn_low)) return "warn";
  return null;
}

const SVG_NS = "http://www.w3.org/2000/svg";
function svg(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

// Half-circle gauge: centre (100,100), radius 80, from 180° (left) to 0° (right).
function arcPath(from, to, r = 80) {
  const pt = f => [100 - r * Math.cos(Math.PI * f), 100 - r * Math.sin(Math.PI * f)];
  const [x1, y1] = pt(from), [x2, y2] = pt(to);
  return `M${x1.toFixed(1)} ${y1.toFixed(1)} A${r} ${r} 0 0 1 ${x2.toFixed(1)} ${y2.toFixed(1)}`;
}

function gaugeRange(key, cfg) {
  const s = ui.series[key] || {};
  let lo = cfg.min, hi = cfg.max;
  if (lo === null) lo = s.min !== null && s.min !== undefined ? Math.min(0, s.min) : 0;
  if (hi === null) hi = s.max !== null && s.max !== undefined && s.max > lo ? s.max : lo + 100;
  if (hi <= lo) hi = lo + 1;
  return [lo, hi];
}

function buildGauge(key, cfg) {
  const [lo, hi] = gaugeRange(key, cfg);
  const frac = v => Math.min(1, Math.max(0, (v - lo) / (hi - lo)));
  const g = svg("svg", { viewBox: "0 0 200 112", class: "gauge", "aria-hidden": "true" });
  g.append(svg("path", { d: arcPath(0, 1), class: "g-track" }));
  const zones = [
    [cfg.warn_low, cfg.alarm_low, "low", "g-warn"], [cfg.alarm_low, null, "low", "g-alarm"],
    [cfg.warn_high, cfg.alarm_high, "high", "g-warn"], [cfg.alarm_high, null, "high", "g-alarm"],
  ];
  for (const [start, end, side, cls] of zones) {
    if (start === null) continue;
    const a = side === "high" ? frac(start) : (end === null ? 0 : frac(end));
    const b = side === "high" ? (end === null ? 1 : frac(end)) : frac(start);
    if (b > a) g.append(svg("path", { d: arcPath(a, b, 93), class: cls }));
  }
  g.append(svg("path", { d: arcPath(0, 1), class: "g-value", pathLength: "100", "stroke-dasharray": "0 100" }));
  g.append(Object.assign(svg("text", { x: "22", y: "110", class: "g-tick" }), { textContent: fmt(lo, 1) }));
  g.append(Object.assign(svg("text", { x: "178", y: "110", class: "g-tick", "text-anchor": "end" }), { textContent: fmt(hi, 1) }));
  return g;
}

function buildTiles() {
  const grid = $("#tiles");
  grid.replaceChildren();
  const old = ui.series;
  ui.series = {};
  for (const key of ui.keys) {
    const p = paramOf(key);
    const cfg = tileConfig(key);
    ui.series[key] = old[key] || { values: [], min: null, max: null };
    const gear = el("button", { class: "tile-gear", type: "button", title: "Tile settings", "aria-label": t("Settings: {name}", { name: t(p.label) }) }, "⚙");
    gear.addEventListener("click", () => openTileDialog(key));
    const tile = el("div", { class: `tile stale type-${cfg.type}`, "data-key": key, draggable: "true" },
      el("div", { class: "label", text: p.label, title: `${t(p.label)} (${p.pid || key})` }),
      gear,
      cfg.type === "gauge" ? buildGauge(key, cfg) : null,
      el("div", { class: "value" }, el("span", { class: "num", text: "—" }), el("span", { class: "unit", text: p.unit })),
      el("div", { class: "minmax", text: "" }),
      cfg.type === "gauge" ? null : el("canvas", { height: cfg.type === "chart" ? "110" : "44" }),
    );
    grid.append(tile);
  }
}

// drag and drop reordering
let dragKey = null;
$("#tiles").addEventListener("dragstart", e => {
  const tile = e.target.closest(".tile");
  if (!tile) return;
  dragKey = tile.dataset.key;
  e.dataTransfer.effectAllowed = "move";
  e.dataTransfer.setData("text/plain", dragKey);
  tile.classList.add("dragging");
});
$("#tiles").addEventListener("dragover", e => {
  if (!dragKey) return;
  e.preventDefault();
  const over = e.target.closest(".tile");
  $$(".tile.drop").forEach(t => t.classList.remove("drop"));
  if (over && over.dataset.key !== dragKey) over.classList.add("drop");
});
$("#tiles").addEventListener("drop", e => {
  e.preventDefault();
  const over = e.target.closest(".tile");
  if (dragKey && over && over.dataset.key !== dragKey) moveTile(dragKey, ui.keys.indexOf(over.dataset.key));
});
$("#tiles").addEventListener("dragend", () => {
  dragKey = null;
  $$(".tile.dragging, .tile.drop").forEach(t => t.classList.remove("dragging", "drop"));
});

function moveTile(key, index) {
  const from = ui.keys.indexOf(key);
  if (from < 0 || index < 0 || index >= ui.keys.length || from === index) return;
  ui.keys.splice(from, 1);
  ui.keys.splice(index, 0, key);
  buildTiles();
  saveLayout();
}

// tile settings dialog
let tileKey = null;
function fillTileDialog(cfg) {
  $("#tile-type").value = cfg.type;
  for (const f of LIMIT_FIELDS) $("#tile-" + f).value = cfg[f] === null || cfg[f] === undefined ? "" : cfg[f];
}
function openTileDialog(key) {
  tileKey = key;
  const p = paramOf(key);
  $("#tile-title").textContent = t(p.label) + (p.unit ? ` [${p.unit}]` : "");
  fillTileDialog(tileConfig(key));
  $("#dlg-tile").showModal();
}
$("#btn-tile-reset").addEventListener("click", () => fillTileDialog({ type: "value", ...(ui.tileDefaults[tileKey] || {}) }));
$("#btn-tile-left").addEventListener("click", () => moveTile(tileKey, ui.keys.indexOf(tileKey) - 1));
$("#btn-tile-right").addEventListener("click", () => moveTile(tileKey, ui.keys.indexOf(tileKey) + 1));
$("#dlg-tile").addEventListener("close", () => {
  if ($("#dlg-tile").returnValue !== "ok" || !tileKey) return;
  const cfg = { type: $("#tile-type").value };
  for (const f of LIMIT_FIELDS) {
    const raw = $("#tile-" + f).value.trim();
    cfg[f] = raw === "" || Number.isNaN(Number(raw)) ? null : Number(raw);
  }
  ui.tiles[tileKey] = cfg;
  buildTiles();
  saveLayout();
});

// alarm sound (Web Audio, no files needed)
let audioCtx = null, lastBeep = 0;
function beep() {
  if (!ui.sound || performance.now() - lastBeep < 4000) return;
  lastBeep = performance.now();
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    for (const at of [0, 0.22]) {
      const osc = audioCtx.createOscillator(), gain = audioCtx.createGain();
      osc.frequency.value = 880;
      gain.gain.setValueAtTime(0.15, audioCtx.currentTime + at);
      gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + at + 0.18);
      osc.connect(gain).connect(audioCtx.destination);
      osc.start(audioCtx.currentTime + at);
      osc.stop(audioCtx.currentTime + at + 0.2);
    }
  } catch { /* audio not available */ }
}
function applySound() {
  const btn = $("#btn-sound");
  btn.setAttribute("aria-pressed", String(ui.sound));
  btn.textContent = t(ui.sound ? "Alarm sound: on" : "Alarm sound: off");
}
$("#btn-sound").addEventListener("click", () => {
  ui.sound = !ui.sound;
  applySound();
  if (ui.sound) { lastBeep = 0; beep(); }  // also unlocks audio in the browser
  saveLayout();
});

function updateTiles(readings) {
  const now = performance.now();
  ui.frames.push(now);
  ui.frames = ui.frames.filter(t => now - t < 5000);
  // Rate over the real time span of the window (it is shorter than 5 s right after start or a pause).
  const span = (now - ui.frames[0]) / 1000;
  const fps = ui.frames.length > 2 && span > 0 ? (ui.frames.length - 1) / span : 0;
  $("#rate").textContent = fps ? t("{n} refresh/s · {p} PID/s", { n: fps.toFixed(1), p: Math.round(fps * readings.length) }) : "";

  let alarm = false;
  for (const r of readings) {
    const tile = $(`.tile[data-key="${r.key}"]`);
    if (!tile) continue;
    const p = paramOf(r.key);
    const cfg = tileConfig(r.key);
    const s = ui.series[r.key];
    const num = $(".num", tile);
    const valueBox = $(".value", tile);
    tile.classList.toggle("stale", r.value === null);
    const level = alertLevel(r.value, cfg);
    tile.classList.toggle("alert-warn", level === "warn");
    tile.classList.toggle("alert-alarm", level === "alarm");
    if (level === "alarm") alarm = true;
    if (typeof r.value === "number") {
      valueBox.classList.remove("text");
      num.textContent = fmt(r.value, p.decimals);
      s.values.push(r.value);
      if (s.values.length > HISTORY) s.values.shift();
      s.min = s.min === null ? r.value : Math.min(s.min, r.value);
      s.max = s.max === null ? r.value : Math.max(s.max, r.value);
      $(".minmax", tile).textContent = `min ${fmt(s.min, p.decimals)} · max ${fmt(s.max, p.decimals)}`;
      const gauge = $(".g-value", tile);
      if (gauge) {
        const [lo, hi] = gaugeRange(r.key, cfg);
        const frac = Math.min(1, Math.max(0, (r.value - lo) / (hi - lo)));
        gauge.setAttribute("stroke-dasharray", `${(frac * 100).toFixed(1)} 100`);
      } else {
        drawSpark($("canvas", tile), s.values);
      }
    } else {
      valueBox.classList.toggle("text", r.value !== null);
      num.textContent = fmt(r.value);
    }
  }
  if (alarm) beep();
  if (ui.recording) {
    const byKey = Object.fromEntries(readings.map(r => [r.key, r.value]));
    ui.recording.rows.push([new Date().toISOString(), ...ui.recording.keys.map(k => byKey[k] ?? "")]);
    $("#btn-record").textContent = t("■ Stop ({n})", { n: ui.recording.rows.length });
  }
}

// trip summary strip
function hms(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const m = Math.floor(seconds / 60), s = Math.round(seconds % 60);
  return m >= 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${m}:${String(s).padStart(2, "0")} min`;
}
function updateTrip(trip) {
  if (!trip) return;
  $("#trip").hidden = false;
  $("#trip-distance").textContent = trip.distance_km === null ? "—" : `${fmt(trip.distance_km, 2)} km`;
  $("#trip-fuel").textContent = trip.fuel_l === null || !ui.params.some(p => p.key === "TRIP_FUEL") ? "—" : `${fmt(trip.fuel_l, 2)} l`;
  $("#trip-avg").textContent = trip.average_l_100km === null ? "—" : `${fmt(trip.average_l_100km, 1)} l/100 km`;
  $("#trip-time").textContent = hms(trip.duration_s);
  $("#trip-idle").textContent = hms(trip.idle_s);
  const hasFuel = ui.params.some(p => p.key === "FUEL_FLOW");
  $("#trip-note").textContent = (ui.vehicle ? t(ui.vehicle.name) + " · " : "") + t(trip.fuel)
    + (hasFuel ? "" : " · " + t("fuel use not available for this car"));
}
$("#btn-trip-reset").addEventListener("click", async () => {
  try {
    const res = await api("/api/trip/reset", { method: "POST", body: { save: true } });
    updateTrip(res.trip);
    toast(res.saved ? t("Trip saved to the history ({summary})", { summary: tWords(res.saved.summary) }) : "Trip reset");
  } catch (e) { toast(e.message, true); }
});

function drawSpark(canvas, values) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (!w || !h) return;
  if (canvas.width !== w * dpr) { canvas.width = w * dpr; canvas.height = h * dpr; }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  if (values.length < 2) return;
  let lo = Math.min(...values), hi = Math.max(...values);
  // minimum vertical span (5% of the value) so 1-unit jitter does not look like a big swing
  const minSpan = Math.max(Math.abs((lo + hi) / 2) * 0.05, 1e-6);
  if (hi - lo < minSpan) { const mid = (lo + hi) / 2; lo = mid - minSpan / 2; hi = mid + minSpan / 2; }
  const pad = 3;
  const x = i => (i / (values.length - 1)) * w;  // stretch the history over the full width
  const y = v => h - pad - ((v - lo) / (hi - lo)) * (h - pad * 2);
  const color = getComputedStyle(document.documentElement).getPropertyValue("--spark").trim();
  const offset = 0;
  ctx.beginPath();
  values.forEach((v, i) => (i ? ctx.lineTo(x(i + offset), y(v)) : ctx.moveTo(x(i + offset), y(v))));
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.6;
  ctx.lineJoin = "round";
  ctx.stroke();
  ctx.lineTo(x(values.length - 1 + offset), h);
  ctx.lineTo(x(offset), h);
  ctx.closePath();
  ctx.globalAlpha = 0.12;
  ctx.fillStyle = color;
  ctx.fill();
  ctx.globalAlpha = 1;
}

function openLive() {
  closeLive();
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/live`);
  ui.ws = ws;
  ws.onopen = sendLiveConfig;
  ws.onmessage = ev => {
    const msg = JSON.parse(ev.data);
    if (msg.error) { if (msg.state && msg.state !== "connected") pollState(); return; }
    if (!ui.paused) updateTiles(msg.readings);
    updateTrip(msg.trip);
  };
  ws.onclose = () => {
    if (ui.ws === ws && ui.state === "connected") setTimeout(() => ui.ws === ws && openLive(), 1500);
  };
}

function closeLive() {
  const ws = ui.ws;
  ui.ws = null;
  if (ws) ws.close();
}

function sendLiveConfig() {
  ui.frames = [];  // start the rate window again: a pause or a new interval would skew it
  if (ui.ws && ui.ws.readyState === WebSocket.OPEN) {
    ui.ws.send(JSON.stringify({ keys: ui.paused ? [] : ui.keys, interval: ui.interval }));
  }
}

$("#btn-pause").addEventListener("click", e => {
  ui.paused = !ui.paused;
  e.currentTarget.setAttribute("aria-pressed", String(ui.paused));
  e.currentTarget.textContent = t(ui.paused ? "Resume" : "Pause");
  sendLiveConfig();
});

const selInterval = $("#sel-interval");
selInterval.value = String(ui.interval);
selInterval.addEventListener("change", () => {
  ui.interval = parseFloat(selInterval.value);
  store.set("interval", ui.interval);
  sendLiveConfig();
});

$("#btn-record").addEventListener("click", () => {
  const btn = $("#btn-record");
  if (!ui.recording) {
    ui.recording = { keys: [...ui.keys], rows: [] };
    btn.classList.add("recording");
    btn.textContent = t("■ Stop ({n})", { n: 0 });
    return;
  }
  const { keys, rows } = ui.recording;
  ui.recording = null;
  btn.classList.remove("recording");
  btn.textContent = t("● Record CSV");
  if (!rows.length) return;
  const csv = [["time", ...keys], ...rows].map(r => r.join(";")).join("\n");
  const blob = new Blob([csv], { type: "text/csv" });
  const a = el("a", { href: URL.createObjectURL(blob), download: `obd-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.csv` });
  document.body.append(a);
  a.click();
  a.remove();
  toast(t("Saved {n} reads to CSV", { n: rows.length }));
});

// parameter picker
function buildPicker(selected) {
  const box = $("#picker");
  box.replaceChildren();
  const groups = {};
  for (const p of ui.params) (groups[p.group] ||= []).push(p);
  for (const [group, items] of Object.entries(groups)) {
    box.append(el("h4", { text: group }));
    for (const p of items) {
      const input = el("input", { type: "checkbox", value: p.key });
      input.checked = selected.includes(p.key);
      box.append(el("label", {}, input, el("span", { text: t(p.label) + (p.unit ? ` [${p.unit}]` : "") }), el("small", { text: p.pid })));
    }
  }
}

$("#btn-params").addEventListener("click", () => {
  buildPicker(ui.keys);
  $("#dlg-params").showModal();
});
$("#btn-params-default").addEventListener("click", () => buildPicker(ui.defaults));
$("#dlg-params").addEventListener("close", () => {
  if ($("#dlg-params").returnValue !== "ok") return;
  const chosen = $$("#picker input:checked").map(i => i.value);
  // keep the user's previous order, append new ones in PID order
  ui.keys = [...ui.keys.filter(k => chosen.includes(k)), ...chosen.filter(k => !ui.keys.includes(k))];
  buildTiles();
  sendLiveConfig();
  saveLayout();
});

// ---------------------------------------------------------------- DTC
function renderDtc(data) {
  const box = $("#dtc-list");
  box.replaceChildren();
  const mil = data.status ? data.status.mil : null;
  $("#mil").dataset.on = String(mil);
  $("#mil-text").textContent = t("MIL (check engine): {state}", { state: mil === null ? "—" : t(mil ? "ON" : "off") });
  const total = data.stored.length + data.pending.length;
  const badge = $("#dtc-badge");
  badge.hidden = !total;
  badge.textContent = total;
  if (!total) {
    box.append(el("div", { class: "card all-clear" },
      Object.assign(document.createElementNS("http://www.w3.org/2000/svg", "svg"), {
        innerHTML: '<path d="M5 13l4 4L19 7"/>',
      }),
      el("span", { text: "No trouble codes — neither stored nor pending." })));
    $("svg", box).setAttribute("viewBox", "0 0 24 24");
  }
  const groups = [
    ["Stored (confirmed)", data.stored, "", ""],
    ["Pending", data.pending, "pending", "Detected once, not confirmed yet."],
    ["Permanent", data.permanent || [], "permanent",
      "Cannot be cleared with a scan tool. They go away by themselves once the ECU has confirmed the fault is fixed."],
  ];
  for (const [title, items, cls, note] of groups) {
    if (!items.length) continue;
    const group = el("div", { class: "dtc-group" }, el("h3", { text: `${t(title)} · ${items.length}` }),
      note ? el("p", { class: "muted group-note", text: note }) : null);
    for (const d of items) group.append(dtcRow(d, cls));
    box.append(group);
  }
  box.append(freezeFrameCard(data.freeze_frame));
}

function dtcRow(d, cls = "") {
  return el("div", { class: "dtc " + cls },
    el("span", { class: "code", text: d.code }),
    el("span", { class: "desc", text: d.description || "No description (probably a manufacturer code)" }),
    el("span", { class: "sys", text: d.system }));
}

function freezeFrameCard(ff) {
  const card = el("div", { class: "card freeze" });
  if (!ff || !ff.supported) {
    card.append(el("h3", { text: "Freeze frame" }), el("p", { class: "muted", text: "Not supported by this car." }));
    return card;
  }
  if (!ff.present) {
    card.append(el("h3", { text: "Freeze frame" }),
      el("p", { class: "muted", text: "No freeze frame stored. The ECU saves one when it confirms a fault." }));
    return card;
  }
  card.append(el("h3", { text: `${t("Freeze frame")} · ${ff.dtc.code}` }),
    el("p", { class: "muted", text: t("Engine conditions when the ECU detected {code}", { code: ff.dtc.code })
      + (ff.dtc.description ? ` (${ff.dtc.description})` : "") + "." }));
  const grid = el("div", { class: "ff-grid" });
  for (const r of ff.readings) {
    grid.append(el("div", { class: "ff-item" }, el("span", { text: r.label }),
      el("b", { text: fmt(r.value, 2) + (r.unit ? " " + r.unit : "") })));
  }
  card.append(grid);
  return card;
}

async function readDtc(silent = false) {
  const btn = $("#btn-dtc-read");
  btn.disabled = true;
  try {
    renderDtc(await api("/api/dtc"));
    if (!silent) toast("Trouble codes read");
  } catch (e) { if (!silent) toast(e.message, true); }
  finally { btn.disabled = ui.state !== "connected"; }
}
$("#btn-dtc-read").addEventListener("click", () => readDtc());

const dlgClear = $("#dlg-clear");
$("#btn-dtc-clear").addEventListener("click", () => {
  $("#clear-ack").checked = false;
  $("#btn-clear-ok").disabled = true;
  dlgClear.showModal();
});
$("#clear-ack").addEventListener("change", e => ($("#btn-clear-ok").disabled = !e.target.checked));
dlgClear.addEventListener("close", async () => {
  if (dlgClear.returnValue !== "ok" || !$("#clear-ack").checked) return;
  try {
    const after = await api("/api/dtc/clear", { method: "POST", body: { confirm: true } });
    const left = after.stored.length + after.pending.length;
    toast(left ? t("Cleared, but {n} code(s) came back — the fault is still present", { n: left })
      : t("Trouble codes cleared") + (after.backup ? " · " + t("backup saved to History") : ""), !!left);
    $("#tests-list").replaceChildren(el("p", { class: "empty", text: "Click “Read test results”." }));
    $("#tests-chips").replaceChildren();
    await readDtc(true);
  } catch (e) { toast(e.message, true); }
});

// ---------------------------------------------------------------- health check
const SEV_LABEL = { critical: "Critical", warn: "Warning", info: "Info", ok: "OK", skipped: "Not evaluated" };
let healthRunning = false;

function downloadBlob(blob, filename) {
  const a = el("a", { href: URL.createObjectURL(blob), download: filename });
  document.body.append(a);
  a.click();
  a.remove();
}

function findingRow(f) {
  const x = findingText(f);
  return el("div", { class: "finding " + f.severity },
    el("span", { class: "sev", text: SEV_LABEL[f.severity] }),
    el("span", { class: "title", text: x.title }, el("span", { class: "area", text: x.area })),
    el("span", { class: "fval", text: x.value }),
    el("span", { class: "detail", text: x.detail }));
}

function renderHealth(r) {
  const box = $("#health-result");
  box.replaceChildren();
  const ring = el("div", { class: "score-ring", "data-grade": r.grade, style: `--p:${r.score}` },
    el("div", {}, el("div", {}, el("b", { text: String(r.score) }), el("span", { text: r.grade }))));
  const chips = el("div", { class: "chips" });
  for (const k of ["critical", "warn", "info", "ok", "skipped"]) {
    if (r.counts[k]) chips.append(el("span", { class: "chip " + k, text: t(`{n} ${SEV_LABEL[k].toLowerCase()}`, { n: r.counts[k] }) }));
  }
  const v = r.vehicle || {};
  const headline = { Good: "The car looks healthy", Fair: "Some things need attention", Poor: "Problems found" }[r.grade];
  const meta = el("div", { class: "health-meta" },
    el("h2", { text: headline }),
    chips,
    el("div", { class: "muted", text: `${r.created.replace("T", " ")} · ${Math.round(r.duration)} s · ` + t("{n} samples", { n: r.samples })
      + (v.simulated ? " · " + t("simulator") : "") }));
  const jsonBtn = el("button", { class: "btn", type: "button", text: "Download JSON" });
  jsonBtn.addEventListener("click", () => downloadBlob(
    new Blob([JSON.stringify(r, null, 2)], { type: "application/json" }),
    `health-report-${r.created.replace(/:/g, "-")}.json`));
  const actions = el("div", { class: "actions" },
    el("a", { class: "btn", href: `/api/health/report.html?lang=${i18n.lang}`, target: "_blank", rel: "noopener", text: "Printable report" }),
    el("a", { class: "btn", href: `/api/health/report.html?print=1&lang=${i18n.lang}`, target: "_blank", rel: "noopener", text: "Save PDF" }),
    el("a", { class: "btn", href: `/api/health/report.html?download=1&lang=${i18n.lang}`, text: "Download HTML" }),
    jsonBtn);
  box.append(el("div", { class: "card health-summary" }, ring, meta, el("span", { class: "spacer" }), actions));
  for (const f of r.findings) box.append(findingRow(f));
  box.append(el("p", { class: "disclaimer", text: r.disclaimer }));
}

function runHealth() {
  if (healthRunning) return;
  healthRunning = true;
  const btn = $("#btn-health");
  btn.disabled = true;
  $("#health-progress").hidden = false;
  $("#health-bar").style.width = "0%";
  $("#health-msg").textContent = t("Starting…");
  // pause the dashboard so the check gets the full adapter bandwidth
  const wasPaused = ui.paused;
  ui.paused = true;
  sendLiveConfig();
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/health`);
  ws.onopen = () => ws.send(JSON.stringify({ duration: parseInt($("#sel-health-duration").value, 10) }));
  ws.onmessage = ev => {
    const msg = JSON.parse(ev.data);
    if (msg.error) { toast(msg.error, true); return; }
    if (msg.progress !== undefined) {
      $("#health-bar").style.width = `${Math.round(msg.progress * 100)}%`;
      $("#health-msg").textContent = t(msg.message);
    }
    if (msg.report) {
      renderHealth(msg.report);
      toast(t("Health check: {score}/100 – {grade}", { score: msg.report.score, grade: t(msg.report.grade) }));
    }
  };
  ws.onclose = () => {
    healthRunning = false;
    btn.disabled = ui.state !== "connected";
    $("#health-progress").hidden = true;
    ui.paused = wasPaused;
    sendLiveConfig();
  };
  ws.onerror = () => toast("Health check connection failed", true);
}
$("#btn-health").addEventListener("click", runHealth);

async function loadLastHealth() {
  try { renderHealth(await api("/api/health/last")); } catch { /* no report yet */ }
}

// ---------------------------------------------------------------- monitor tests (mode 06)
const TEST_LABEL = { pass: "Pass", near: "Near limit", fail: "Fail" };

function limitText(x) {
  const u = x.unit ? " " + x.unit : "";
  if (x.min !== null && x.max !== null) return t("allowed {range}", { range: `${fmt(x.min, 3)} – ${fmt(x.max, 3)}${u}` });
  if (x.max !== null) return t("limit {value}", { value: fmt(x.max, 3) + u });
  if (x.min !== null) return t("minimum {value}", { value: fmt(x.min, 3) + u });
  return t("no limit");
}

function testBar(x) {
  if (x.margin === null) return el("div", { class: "mon-bar none" });
  const bar = el("div", { class: "mon-bar" });
  if (x.min !== null && x.max !== null) {
    // position inside the allowed window
    const pos = Math.min(100, Math.max(0, (x.value - x.min) / (x.max - x.min) * 100));
    bar.classList.add("window");
    bar.append(el("span", { class: "mon-dot", style: `left:${pos.toFixed(1)}%` }));
  } else {
    // how much of the way to the limit is used up
    bar.append(el("span", { class: "mon-fill", style: `width:${(100 - Math.min(100, x.margin)).toFixed(1)}%` }));
  }
  return bar;
}

function renderTests(data) {
  const box = $("#tests-list");
  const chips = $("#tests-chips");
  box.replaceChildren();
  chips.replaceChildren();
  if (!data.supported) {
    box.append(el("p", { class: "empty", text: "This car does not report monitor test results (mode 06 needs a CAN protocol)." }));
    return;
  }
  if (!data.tests.length) {
    box.append(el("p", { class: "empty", text: "No test results yet — the monitors have not run since the codes were cleared." }));
    return;
  }
  for (const [k, cls] of [["fail", "critical"], ["near", "warn"], ["pass", "ok"]]) {
    if (data.counts[k]) chips.append(el("span", { class: "chip " + cls, text: t(`{n} ${TEST_LABEL[k].toLowerCase()}`, { n: data.counts[k] }) }));
  }
  const groups = new Map();
  for (const x of data.tests) (groups.get(x.monitor) || groups.set(x.monitor, []).get(x.monitor)).push(x);
  for (const [monitor, tests] of groups) {
    const worst = tests.some(y => y.status === "fail") ? "fail" : tests.some(y => y.status === "near") ? "near" : "pass";
    const group = el("div", { class: "mon-group " + worst }, el("h3", { text: monitor }));
    for (const x of tests) {
      group.append(el("div", { class: "mon-test " + x.status },
        el("span", { class: "mon-status", text: TEST_LABEL[x.status] }),
        el("span", { class: "mon-name", text: x.name, title: `Monitor $${x.id.replace(":", " test $")}` }),
        el("span", { class: "mon-value", text: fmt(x.value, 3) + (x.unit ? " " + x.unit : "") }),
        testBar(x),
        el("span", { class: "mon-limit", text: limitText(x) + (x.margin !== null ? " · " + t("{n} % headroom", { n: fmt(x.margin, 0) }) : "") })));
    }
    box.append(group);
  }
}

async function readTests(silent = false) {
  const btn = $("#btn-tests");
  btn.disabled = true;
  try {
    renderTests(await api("/api/monitors"));
    if (!silent) toast("Monitor test results read");
  } catch (e) { if (!silent) toast(e.message, true); }
  finally { btn.disabled = ui.state !== "connected"; }
}
$("#btn-tests").addEventListener("click", () => readTests());

// ---------------------------------------------------------------- history and vehicle profiles
const KIND_LABEL = { health: "Health report", "dtc-backup": "Pre-clear backup", trip: "Trip" };
let vehicles = [];

async function loadVehicles() {
  const data = await api("/api/vehicles");
  vehicles = data.vehicles;
  const active = vehicles.find(v => v.id === data.active) || vehicles[0];
  ui.vehicle = active;
  $("#veh-select").replaceChildren(...vehicles.map(v => {
    const o = el("option", { value: v.id, text: v.name + (v.vin ? ` · ${v.vin}` : "") });
    o.selected = v.id === active.id;
    return o;
  }));
  $("#veh-name").value = active.name;
  $("#veh-fuel").value = active.fuel;
  $("#veh-vin").value = active.vin || "";
  $("#veh-notes").value = active.notes || "";
  $("#veh-meta").textContent = t("created {date}", { date: active.created.replace("T", " ") });
  $("#btn-veh-delete").disabled = vehicles.length < 2;
  showLastConnection();
}

async function loadHistory() {
  try {
    await loadVehicles();
    const kind = $("#hist-kind").value;
    const data = await api("/api/history" + (kind ? `?kind=${kind}` : ""));
    renderHistory(data.records);
    const about = await api("/api/about");
    $("#hist-dir").textContent = t("Files are stored in {dir}", { dir: about.data_dir });
  } catch (e) { toast(e.message, true); }
}

function renderHistory(records) {
  const box = $("#hist-list");
  box.replaceChildren();
  $("#hist-compare").replaceChildren();
  updateCompareButton();
  if (!records.length) {
    box.append(el("p", { class: "empty", text: "No records yet. Run a health check, or clear codes — a backup is saved first." }));
    return;
  }
  for (const r of records) {
    const pick = el("input", { type: "checkbox", value: r.id, "aria-label": "Select for comparison" });
    pick.hidden = r.kind !== "health";
    pick.addEventListener("change", updateCompareButton);
    const view = el("button", { class: "btn", type: "button", text: "View" });
    view.addEventListener("click", () => viewRecord(r.id));
    const del = el("button", { class: "btn btn-danger", type: "button", text: "Delete" });
    del.addEventListener("click", async () => {
      if (!confirm(`${t("Delete this record?")}\n${t(KIND_LABEL[r.kind])} · ${r.created.replace("T", " ")}`)) return;
      try { await api(`/api/history/${r.id}`, { method: "DELETE" }); loadHistory(); }
      catch (e) { toast(e.message, true); }
    });
    box.append(el("div", { class: "hist-row kind-" + r.kind },
      el("span", { class: "hist-pick" }, pick),
      el("span", { class: "hist-date", text: r.created.replace("T", " ") }),
      el("span", { class: "hist-kind", text: KIND_LABEL[r.kind] || r.kind }),
      el("span", { class: "hist-summary", text: tWords(r.summary) }),
      el("span", { class: "actions" },
        r.kind === "health" ? el("a", { class: "btn", href: `/api/history/${r.id}/report.html?lang=${i18n.lang}`, target: "_blank", rel: "noopener", text: "Report" }) : null,
        view, del)));
  }
}

function pickedReports() { return $$("#hist-list input:checked").map(i => i.value); }
function updateCompareButton() { $("#btn-hist-compare").disabled = pickedReports().length !== 2; }

async function viewRecord(id) {
  try {
    const rec = await api(`/api/history/${id}`);
    const body = $("#record-body");
    body.replaceChildren();
    $("#record-title").textContent = `${t(KIND_LABEL[rec.kind] || rec.kind)} · ${rec.created.replace("T", " ")}`;
    const d = rec.data;
    if (rec.kind === "dtc-backup") {
      for (const [title, items, cls] of [["Stored", d.dtcs.stored, ""], ["Pending", d.dtcs.pending, "pending"], ["Permanent", d.dtcs.permanent || [], "permanent"]]) {
        if (!items.length) continue;
        body.append(el("h4", { text: `${t(title)} · ${items.length}` }));
        for (const c of items) body.append(dtcRow(c, cls));
      }
      body.append(freezeFrameCard(d.freeze_frame));
    } else if (rec.kind === "trip") {
      const dl = el("dl");
      fillDl(dl, [
        ["Distance", `${fmt(d.distance_km, 2)} km`],
        ["Fuel used", d.fuel_l === null ? "—" : `${fmt(d.fuel_l, 2)} l (${t(d.fuel)})`],
        ["Average consumption", d.average_l_100km === null ? "—" : `${fmt(d.average_l_100km, 1)} l/100 km`],
        ["Average speed", d.average_speed_kmh === null ? "—" : `${fmt(d.average_speed_kmh, 0)} km/h`],
        ["Time", hms(d.duration_s)],
        ["Standing", hms(d.idle_s)],
      ]);
      body.append(dl);
    } else {
      body.append(el("p", { text: t("Score {score}/100 – {grade}", { score: d.score, grade: t(d.grade) }) }));
      for (const f of d.findings) {
        if (f.severity === "ok" || f.severity === "skipped") continue;
        body.append(findingRow(f));
      }
      if (!$(".finding", body)) body.append(el("p", { class: "muted", text: "No problems found." }));
    }
    $("#dlg-record").showModal();
  } catch (e) { toast(e.message, true); }
}

const SEV_RANK = { critical: 0, warn: 1, info: 2, ok: 3, skipped: 4 };

async function compareReports() {
  const ids = pickedReports();
  if (ids.length !== 2) return;
  try {
    let [a, b] = await Promise.all(ids.map(id => api(`/api/history/${id}`)));
    if (a.created > b.created) [a, b] = [b, a];  // older report first
    const before = Object.fromEntries(a.data.findings.map(f => [f.key, f]));
    const after = Object.fromEntries(b.data.findings.map(f => [f.key, f]));
    const keys = [...new Set([...Object.keys(before), ...Object.keys(after)])];
    const rows = [];
    for (const key of keys) {
      const x = before[key], y = after[key];
      let change = "same";
      if (!x) change = SEV_RANK[y.severity] <= 2 ? "new" : "same";
      else if (!y) change = SEV_RANK[x.severity] <= 2 ? "gone" : "same";
      else if (SEV_RANK[y.severity] > SEV_RANK[x.severity]) change = "better";
      else if (SEV_RANK[y.severity] < SEV_RANK[x.severity]) change = "worse";
      if (change === "same" && (x || y).severity === "skipped" && (!x || !y || y.severity === "skipped")) continue;
      rows.push({ key, x, y, change });
    }
    const order = { worse: 0, new: 1, better: 2, gone: 3, same: 4 };
    rows.sort((p, q) => order[p.change] - order[q.change]);
    const CHANGE = { worse: "Worse", new: "New problem", better: "Improved", gone: "Fixed", same: "No change" };
    const cell = f => f ? el("td", {}, el("span", { class: "sev-tag " + f.severity, text: SEV_LABEL[f.severity] }),
      el("span", { class: "mono", text: f.value ? " " + t(f.value) : "" })) : el("td", { class: "muted", text: "—" });
    const table = el("table", { class: "table wrap" },
      el("thead", {}, el("tr", {}, ...["Finding", `${t("Before")} · ${a.created.replace("T", " ")}`, `${t("After")} · ${b.created.replace("T", " ")}`, "Change"].map(h => el("th", { text: h })))));
    const body = el("tbody");
    for (const r of rows) {
      const f = r.y || r.x;
      body.append(el("tr", { class: "cmp-" + r.change },
        el("td", {}, el("strong", { text: findingText(f).title }), el("div", { class: "muted", text: f.area })),
        cell(r.x), cell(r.y), el("td", { class: "cmp-change", text: CHANGE[r.change] })));
    }
    table.append(body);
    const diff = b.data.score - a.data.score;
    const card = el("div", { class: "card" },
      el("h3", { text: "Comparison" }),
      el("p", { class: "cmp-score" },
        el("b", { text: `${a.data.score} → ${b.data.score}` }),
        el("span", { class: diff > 0 ? "up" : diff < 0 ? "down" : "muted", text: " " + (diff ? t("({diff} points)", { diff: (diff > 0 ? "+" : "") + diff }) : t("(no change)")) })),
      el("div", { class: "table-wrap" }, table));
    $("#hist-compare").replaceChildren(card);
    card.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) { toast(e.message, true); }
}
$("#btn-hist-compare").addEventListener("click", compareReports);
$("#btn-hist-refresh").addEventListener("click", loadHistory);
$("#hist-kind").addEventListener("change", loadHistory);

$("#veh-select").addEventListener("change", async e => {
  try {
    await api(`/api/vehicles/${e.target.value}/activate`, { method: "POST" });
    await pollState();
    await loadHistory();
    if (ui.state === "connected") { await loadLayout(); buildTiles(); sendLiveConfig(); }
  } catch (err) { toast(err.message, true); }
});
$("#veh-form").addEventListener("submit", async e => {
  e.preventDefault();
  try {
    await api(`/api/vehicles/${$("#veh-select").value}`, { method: "PUT", body: {
      name: $("#veh-name").value, fuel: $("#veh-fuel").value, vin: $("#veh-vin").value.trim().toUpperCase(), notes: $("#veh-notes").value } });
    toast("Profile saved");
    await pollState();
    await loadVehicles();
    if (ui.state === "connected") onConnected();  // the fuel type changes the calculated parameters
  } catch (err) { toast(err.message, true); }
});
$("#btn-veh-new").addEventListener("click", () => {
  $("#new-veh-name").value = "";
  $("#dlg-vehicle").showModal();
});
$("#dlg-vehicle").addEventListener("close", async () => {
  if ($("#dlg-vehicle").returnValue !== "ok") return;
  try {
    await api("/api/vehicles", { method: "POST", body: { name: $("#new-veh-name").value, fuel: $("#new-veh-fuel").value } });
    toast("Vehicle profile created");
    await pollState();
    await loadHistory();
    if (ui.state === "connected") { ui.keys = null; onConnected(); }
  } catch (err) { toast(err.message, true); }
});
$("#btn-veh-delete").addEventListener("click", async () => {
  const v = vehicles.find(x => x.id === $("#veh-select").value);
  if (!v || !confirm(t("Delete the profile “{name}” with its dashboard layout and all history records?", { name: v.name }))) return;
  try {
    await api(`/api/vehicles/${v.id}`, { method: "DELETE" });
    await pollState();
    await loadHistory();
  } catch (err) { toast(err.message, true); }
});

// ---------------------------------------------------------------- about, adapters
let aboutData = null;
async function loadAbout() {
  if (!aboutData) aboutData = await api("/api/about");
  return aboutData;
}

$("#btn-about").addEventListener("click", async () => {
  try {
    const a = await loadAbout();
    $("#about-version").textContent = "v" + a.version;
    const links = $("#about-support-links");
    links.replaceChildren(...a.support.map(l => el("a", { class: "btn btn-primary", href: l.url, target: "_blank", rel: "noopener", text: l.name })));
    $("#about-support-text").textContent = t(a.support.length
      ? "OBDruid is free and open source. If it saved you a trip to the workshop, you can support its development:"
      : "OBDruid is free and open source. A star on GitHub, a bug report or telling a friend all help.");
    const lic = a.supporter && a.supporter.active;
    $("#about-licensed").hidden = !lic;
    $("#about-licensed").textContent = lic ? t("Licensed to: {name}", { name: a.supporter.name }) : "";
    $("#btn-about-key").hidden = !!lic;
    const dl = $("#about-links");
    dl.replaceChildren(el("dt", { text: "Author" }), el("dd", { text: a.author }),
                       el("dt", { text: "License" }), el("dd", { text: a.license }));
    for (const [k, url] of [["Source code", a.repo], ["Report a problem", a.issues]]) {
      dl.append(el("dt", { text: k }), el("dd", {}, el("a", { href: url, target: "_blank", rel: "noopener", text: url })));
    }
    dl.append(el("dt", { text: "Data folder" }), el("dd", { text: a.data_dir }));
    $("#dlg-about").showModal();
  } catch (e) { toast(e.message, true); }
});

const ADAPTER_LABEL = { tested: "Tested", "should work": "Should work", "not supported": "Not supported" };
async function loadAdapters() {
  try {
    const a = await loadAbout();
    const table = $("#adapters-table");
    table.replaceChildren(el("thead", {}, el("tr", {}, ...["Adapter", "Status", "Notes"].map(h => el("th", { text: h })))));
    const body = el("tbody");
    for (const ad of a.adapters) {
      body.append(el("tr", {}, el("td", { text: ad.name }),
        el("td", {}, el("span", { class: "adapter-status " + ad.status.replace(/ /g, "-"), text: ADAPTER_LABEL[ad.status] || ad.status })),
        el("td", { class: "muted", text: ad.notes })));
    }
    table.append(body);
    $("#adapters-note").textContent = t(a.vehicles);
  } catch { /* ignore */ }
}

function showLastConnection() {
  const c = ui.vehicle && ui.vehicle.connection;
  $("#last-conn").hidden = !c;
  if (!c) return;
  $("#last-conn-text").textContent = t("Last working settings for {name}: {port}", { name: ui.vehicle.name, port: c.port })
    + (c.baudrate ? `, ${c.baudrate} baud` : "") + (c.protocol_name ? `, ${c.protocol_name}` : "");
}
$("#btn-last-conn").addEventListener("click", () => {
  const c = ui.vehicle && ui.vehicle.connection;
  if (!c) return;
  $("#in-port").value = c.port || "";
  $("#in-baud").value = c.baudrate ? String(c.baudrate) : "";
  $("#in-protocol").value = [...$("#in-protocol").options].some(o => o.value === c.protocol) ? c.protocol : "";
  $("#in-sim").checked = false;
  toast("Settings filled in — click Connect");
});

// ---------------------------------------------------------------- info
function fillDl(dl, rows) {
  dl.replaceChildren();
  for (const [k, v] of rows) dl.append(el("dt", { text: k }), el("dd", { text: v }));
}

async function loadInfo() {
  try {
    const [info, params] = await Promise.all([api("/api/info"), api("/api/params")]);
    const a = info.adapter, s = info.status;
    fillDl($("#info-adapter"), [
      ["Port", a.port + (a.simulated ? " " + t("(simulator)") : "")],
      ["Adapter", a.elm_version || "?"],
      ["Voltage", a.voltage != null ? `${fmt(a.voltage, 1)} V` : "?"],
      ["Protocol", `${a.protocol} (#${a.protocol_id})`],
    ]);
    fillDl($("#info-vehicle"), [
      ["VIN", info.vin || "not reported by the car"],
      ["Ignition", s.ignition_type],
      ["MIL (check engine)", s.mil ? "ON" : "off"],
      ["Stored codes", String(s.dtc_count)],
      ["Parameters", t("{n} supported", { n: params.params.length })],
    ]);
    const ready = $("#readiness");
    ready.replaceChildren();
    for (const t of s.readiness) {
      ready.append(el("div", { class: "ready-item " + (t.complete ? "ok" : "no") },
        el("span", { class: "ico" }), el("span", { text: t.label }),
        el("small", { text: t.complete ? "complete" : "incomplete" })));
    }
    const table = $("#params-table");
    table.replaceChildren(el("thead", {}, el("tr", {}, ...["PID", "Name", "Unit", "Group", "Key"].map(h => el("th", { text: h })))));
    const body = el("tbody");
    for (const p of params.params) {
      body.append(el("tr", {}, el("td", { class: "mono", text: p.pid }), el("td", { text: p.label }),
        el("td", { text: p.unit }), el("td", { text: p.group }), el("td", { class: "mono muted", text: p.key })));
    }
    table.append(body);
  } catch (e) { toast(e.message, true); }
}
$("#btn-info").addEventListener("click", loadInfo);

// ---------------------------------------------------------------- connection form
async function loadPorts() {
  try {
    const { ports } = await api("/api/ports");
    $("#ports").replaceChildren(...ports.map(p => el("option", { value: p })));
  } catch { /* ignore */ }
}

$("#conn-form").addEventListener("submit", async e => {
  e.preventDefault();
  const body = {
    port: $("#in-port").value.trim() || null,
    baudrate: $("#in-baud").value ? parseInt($("#in-baud").value, 10) : null,
    protocol: $("#in-protocol").value || null,
    simulate: $("#in-sim").checked,
  };
  applyState({ state: "connecting", error: null, settings: body });
  closeLive();
  try {
    applyState(await api("/api/connect", { method: "POST", body }));
    toast("Connected");
    showTab("dash");
  } catch (err) { toast(err.message, true); pollState(); }
});

$("#btn-disconnect").addEventListener("click", async () => {
  closeLive();
  try { applyState(await api("/api/disconnect", { method: "POST" })); } catch (e) { toast(e.message, true); }
});

// ---------------------------------------------------------------- support window / supporter key
async function showSupport(force = false) {
  const a = await loadAbout();
  if (a.supporter && a.supporter.active && !force) return;
  $("#support-links").replaceChildren(...a.support.map(l =>
    el("a", { class: "btn btn-primary", href: l.url, target: "_blank", rel: "noopener", text: l.name })));
  // "... write to {email} ..." with the address as a mailto link
  const [before, after] = t("After supporting, write to {email} with the payment confirmation and the name to show in the program. The key comes by e-mail.").split("{email}");
  $("#support-how").replaceChildren(before, el("a", { href: a.key_request }, a.contact), after || "");
  $("#support-key-error").hidden = true;
  if (force) $("#support-key-box").open = true;
  const dlg = $("#dlg-support");
  if (!dlg.open) dlg.showModal();
}

$("#btn-activate-key").addEventListener("click", async () => {
  const error = $("#support-key-error");
  error.hidden = true;
  try {
    const s = await api("/api/supporter", { method: "POST", body: { key: $("#in-supporter-key").value } });
    aboutData = null;  // reload the About data with the new state
    $("#in-supporter-key").value = "";
    $("#dlg-support").close();
    toast(t("Thank you! Licensed to: {name}", { name: s.name }));
  } catch (e) {
    error.textContent = t(e.message);
    error.hidden = false;
  }
});

$("#btn-about-key").addEventListener("click", () => {
  $("#dlg-about").close();
  showSupport(true).catch(e => toast(e.message, true));
});

// ---------------------------------------------------------------- Bluetooth adapter search
$("#btn-bt-scan").addEventListener("click", async () => {
  const btn = $("#btn-bt-scan"), msg = $("#bt-scan-msg"), list = $("#bt-list");
  btn.disabled = true;
  msg.textContent = t("Searching (about 10 s)…");
  list.replaceChildren();
  try {
    const { devices } = await api("/api/bluetooth/scan?seconds=8", { method: "POST" });
    const obdLike = devices.filter(d => d.likely_obd);
    const shown = obdLike.length ? obdLike : devices;
    msg.textContent = devices.length
      ? t("{n} found – click one to use it", { n: shown.length })
      : t("Nothing found. Is the adapter plugged in and the ignition on?");
    for (const d of shown) {
      const item = el("button", { class: "bt-item", type: "button" },
        el("b", { text: d.name || "?" }), " ",
        el("span", { class: "chip", text: d.kind === "ble" ? "BLE" : "Bluetooth" }), " ",
        el("span", { class: "mono muted", text: d.address }));
      item.addEventListener("click", () => {
        $("#in-port").value = d.url;
        $("#in-baud").value = "";
        msg.textContent = t("Port set to {url} – press Connect.", { url: d.url });
      });
      list.append(item);
    }
  } catch (e) {
    msg.textContent = t(e.message);
  } finally {
    btn.disabled = false;
  }
});

// ---------------------------------------------------------------- start
initLanguage().then(() => {
  showSupport().catch(() => { /* the support window is optional */ });
  showTab(store.get("tab", "dash"));
  pollState();
  setInterval(pollState, 3000);
});
window.addEventListener("resize", () => {
  for (const [key, s] of Object.entries(ui.series)) {
    const c = $(`.tile[data-key="${key}"] canvas`);
    if (c) drawSpark(c, s.values);
  }
});
