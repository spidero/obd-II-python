// OBD Tool web UI - plain JS, no external dependencies (works offline in the car).
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

const ui = {
  state: "disconnected",
  params: [],          // supported params [{key,label,unit,pid,group,decimals}]
  defaults: [],
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
  el.textContent = msg;
  el.classList.toggle("err", isError);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), isError ? 6000 : 3000);
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else node.setAttribute(k, v);
  }
  for (const c of children) if (c != null) node.append(c);
  return node;
}

function fmt(value, decimals = 1) {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") return value.toLocaleString("en-US", { maximumFractionDigits: decimals, minimumFractionDigits: 0 });
  return String(value);
}

// ---------------------------------------------------------------- tabs
function showTab(name) {
  $$(".tabs button").forEach(b => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  $$(".tab").forEach(t => t.toggleAttribute("data-active", t.id === "tab-" + name));
  store.set("tab", name);
  if (name === "info" && ui.state === "connected") loadInfo();
  if (name === "conn") loadPorts();
  if (name === "health" && !$("#health-result").children.length) loadLastHealth();
}
$$(".tabs button").forEach(b => b.addEventListener("click", () => showTab(b.dataset.tab)));

// ---------------------------------------------------------------- connection state
const STATE_TEXT = { connected: "Connected", connecting: "Connecting…", disconnected: "Disconnected", error: "Connection error" };

function applyState(s) {
  const prev = ui.state;
  ui.state = s.state;
  const pill = $("#conn-pill");
  pill.dataset.state = s.state;
  $("#conn-text").textContent = STATE_TEXT[s.state] + (s.settings && s.settings.simulate && s.state === "connected" ? " (simulator)" : "");
  pill.title = s.error || "";
  const err = $("#conn-error");
  err.hidden = !s.error;
  err.textContent = s.error || "";
  $("#btn-connect").disabled = s.state === "connecting";
  $("#btn-disconnect").disabled = s.state !== "connected";
  if (s.settings && prev !== s.state) {
    $("#in-sim").checked = !!s.settings.simulate;
    if (s.settings.port) $("#in-port").value = s.settings.port;
  }
  const online = s.state === "connected";
  $$("#btn-dtc-read, #btn-dtc-clear, #btn-info, #btn-params").forEach(b => (b.disabled = !online));
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

function buildTiles() {
  const grid = $("#tiles");
  grid.replaceChildren();
  ui.series = {};
  for (const key of ui.keys) {
    const p = paramOf(key);
    ui.series[key] = { values: [], min: null, max: null };
    grid.append(el("div", { class: "tile stale", "data-key": key },
      el("div", { class: "label", text: p.label, title: `${p.label} (${p.pid || key})` }),
      el("div", { class: "value" }, el("span", { class: "num", text: "—" }), el("span", { class: "unit", text: p.unit })),
      el("div", { class: "minmax", text: "" }),
      el("canvas", { height: "44" }),
    ));
  }
}

function updateTiles(readings) {
  const now = performance.now();
  ui.frames.push(now);
  ui.frames = ui.frames.filter(t => now - t < 5000);
  $("#rate").textContent = ui.frames.length > 1 ? `${(ui.frames.length / 5).toFixed(1)} reads/s` : "";

  for (const r of readings) {
    const tile = $(`.tile[data-key="${r.key}"]`);
    if (!tile) continue;
    const p = paramOf(r.key);
    const s = ui.series[r.key];
    const num = $(".num", tile);
    const valueBox = $(".value", tile);
    tile.classList.toggle("stale", r.value === null);
    if (typeof r.value === "number") {
      valueBox.classList.remove("text");
      num.textContent = fmt(r.value, p.decimals);
      s.values.push(r.value);
      if (s.values.length > HISTORY) s.values.shift();
      s.min = s.min === null ? r.value : Math.min(s.min, r.value);
      s.max = s.max === null ? r.value : Math.max(s.max, r.value);
      $(".minmax", tile).textContent = `min ${fmt(s.min, p.decimals)} · max ${fmt(s.max, p.decimals)}`;
      drawSpark($("canvas", tile), s.values);
    } else {
      valueBox.classList.toggle("text", r.value !== null);
      num.textContent = fmt(r.value);
    }
  }
  if (ui.recording) {
    const byKey = Object.fromEntries(readings.map(r => [r.key, r.value]));
    ui.recording.rows.push([new Date().toISOString(), ...ui.recording.keys.map(k => byKey[k] ?? "")]);
    $("#btn-record").textContent = `■ Stop (${ui.recording.rows.length})`;
  }
}

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
  if (ui.ws && ui.ws.readyState === WebSocket.OPEN) {
    ui.ws.send(JSON.stringify({ keys: ui.paused ? [] : ui.keys, interval: ui.interval }));
  }
}

$("#btn-pause").addEventListener("click", e => {
  ui.paused = !ui.paused;
  e.currentTarget.setAttribute("aria-pressed", String(ui.paused));
  e.currentTarget.textContent = ui.paused ? "Resume" : "Pause";
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
    btn.textContent = "■ Stop (0)";
    return;
  }
  const { keys, rows } = ui.recording;
  ui.recording = null;
  btn.classList.remove("recording");
  btn.textContent = "● Record CSV";
  if (!rows.length) return;
  const csv = [["time", ...keys], ...rows].map(r => r.join(";")).join("\n");
  const blob = new Blob([csv], { type: "text/csv" });
  const a = el("a", { href: URL.createObjectURL(blob), download: `obd-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.csv` });
  document.body.append(a);
  a.click();
  a.remove();
  toast(`Saved ${rows.length} reads to CSV`);
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
      box.append(el("label", {}, input, el("span", { text: p.label + (p.unit ? ` [${p.unit}]` : "") }), el("small", { text: p.pid })));
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
  store.set("keys", ui.keys);
  buildTiles();
  sendLiveConfig();
});

// ---------------------------------------------------------------- DTC
function renderDtc(data) {
  const box = $("#dtc-list");
  box.replaceChildren();
  const mil = data.status ? data.status.mil : null;
  $("#mil").dataset.on = String(mil);
  $("#mil-text").textContent = "MIL (check engine): " + (mil === null ? "—" : mil ? "ON" : "off");
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
    return;
  }
  for (const [title, items, cls] of [["Stored (confirmed)", data.stored, ""], ["Pending", data.pending, "pending"]]) {
    if (!items.length) continue;
    const group = el("div", { class: "dtc-group" }, el("h3", { text: `${title} · ${items.length}` }));
    for (const d of items) {
      group.append(el("div", { class: "dtc " + cls },
        el("span", { class: "code", text: d.code }),
        el("span", { class: "desc", text: d.description || "No description (probably a manufacturer code)" }),
        el("span", { class: "sys", text: d.system })));
    }
    box.append(group);
  }
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
    toast(left ? `Cleared, but ${left} code(s) came back — the fault is still present` : "Trouble codes cleared", !!left);
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

function renderHealth(r) {
  const box = $("#health-result");
  box.replaceChildren();
  const ring = el("div", { class: "score-ring", "data-grade": r.grade, style: `--p:${r.score}` },
    el("div", {}, el("div", {}, el("b", { text: String(r.score) }), el("span", { text: r.grade }))));
  const chips = el("div", { class: "chips" });
  for (const k of ["critical", "warn", "info", "ok", "skipped"]) {
    if (r.counts[k]) chips.append(el("span", { class: "chip " + k, text: `${r.counts[k]} ${SEV_LABEL[k].toLowerCase()}` }));
  }
  const v = r.vehicle || {};
  const headline = { Good: "The car looks healthy", Fair: "Some things need attention", Poor: "Problems found" }[r.grade];
  const meta = el("div", { class: "health-meta" },
    el("h2", { text: headline }),
    chips,
    el("div", { class: "muted", text: `${r.created.replace("T", " ")} · ${Math.round(r.duration)} s · ${r.samples} samples`
      + (v.simulated ? " · simulator" : "") }));
  const jsonBtn = el("button", { class: "btn", type: "button", text: "Download JSON" });
  jsonBtn.addEventListener("click", () => downloadBlob(
    new Blob([JSON.stringify(r, null, 2)], { type: "application/json" }),
    `health-report-${r.created.replace(/:/g, "-")}.json`));
  const actions = el("div", { class: "actions" },
    el("a", { class: "btn", href: "/api/health/report.html", target: "_blank", rel: "noopener", text: "Printable report" }),
    el("a", { class: "btn", href: "/api/health/report.html?download=1", text: "Download HTML" }),
    jsonBtn);
  box.append(el("div", { class: "card health-summary" }, ring, meta, el("span", { class: "spacer" }), actions));
  for (const f of r.findings) {
    box.append(el("div", { class: "finding " + f.severity },
      el("span", { class: "sev", text: SEV_LABEL[f.severity] }),
      el("span", { class: "title", text: f.title }, el("span", { class: "area", text: f.area })),
      el("span", { class: "fval", text: f.value || "" }),
      el("span", { class: "detail", text: f.detail || "" })));
  }
  box.append(el("p", { class: "disclaimer", text: r.disclaimer }));
}

function runHealth() {
  if (healthRunning) return;
  healthRunning = true;
  const btn = $("#btn-health");
  btn.disabled = true;
  $("#health-progress").hidden = false;
  $("#health-bar").style.width = "0%";
  $("#health-msg").textContent = "Starting…";
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
      $("#health-msg").textContent = msg.message;
    }
    if (msg.report) {
      renderHealth(msg.report);
      toast(`Health check: ${msg.report.score}/100 – ${msg.report.grade}`);
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
      ["Port", a.port + (a.simulated ? " (simulator)" : "")],
      ["Adapter", a.elm_version || "?"],
      ["Voltage", a.voltage != null ? `${fmt(a.voltage, 1)} V` : "?"],
      ["Protocol", `${a.protocol} (#${a.protocol_id})`],
    ]);
    fillDl($("#info-vehicle"), [
      ["VIN", info.vin || "not reported by the car"],
      ["Ignition", s.ignition_type],
      ["MIL (check engine)", s.mil ? "ON" : "off"],
      ["Stored codes", String(s.dtc_count)],
      ["Parameters", `${params.params.length} supported`],
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

// ---------------------------------------------------------------- start
showTab(store.get("tab", "dash"));
pollState();
setInterval(pollState, 3000);
window.addEventListener("resize", () => {
  for (const [key, s] of Object.entries(ui.series)) {
    const c = $(`.tile[data-key="${key}"] canvas`);
    if (c) drawSpark(c, s.values);
  }
});
