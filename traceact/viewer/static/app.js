/* app.js — TraceAct viewer front-end logic.
 *
 * Responsibilities:
 *   - talk to the local server: list/add sources, open the live SSE stream
 *   - keep a bounded, newest-first list of traces (a ring buffer)
 *   - render the log table, the inspector, and the trace map
 *   - handle navigation, tabs, search, settings, and the add-source modal
 *
 * No framework and no build step: plain DOM APIs. Everything is one module-level
 * `state` object plus render functions that read from it. Any change to state
 * is followed by a call to the relevant render function.
 *
 * Live data path (single SSE connection per source):
 *   The server sends one "snapshot" message (the most recent N traces) followed
 *   by "append" messages as new traces are written. We replace on snapshot and
 *   prepend on append, capping the list at the configured row limit so the page
 *   never grows unbounded no matter how busy the source is. */

const state = {
  sources: [],            // [{name, path}]
  currentSource: null,    // source name currently streamed
  traces: [],             // newest-first, capped at settings.limit
  selected: null,         // the trace object shown in the inspector
  activeTab: "log",       // "log" | "map"
  search: "",
  stream: null,           // the EventSource, or null
  replayPaused: false,    // whether the map replay is paused
  settings: loadSettings(),
};

/* ---- Boot ------------------------------------------------------------ */

function init() {
  applySettings();
  wireNav();
  wireTabs();
  wireSearch();
  wireSettings();
  wireModal();
  wireReplayControls();
  wireDoctor();

  refreshSources().then(() => {
    // If a source was seeded on the command line, stream the first one.
    if (state.sources.length > 0) {
      selectSource(state.sources[0].name);
    } else {
      renderLog();
    }
  });
}

/* ---- Sources --------------------------------------------------------- */

async function refreshSources() {
  try {
    const res = await fetch("/api/sources");
    state.sources = await res.json();
  } catch (e) {
    state.sources = [];
  }
  renderSourceList();
}

async function addSource(path) {
  if (!path || !path.trim()) return;
  try {
    const res = await fetch("/api/sources", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: path.trim() }),
    });
    const added = await res.json();
    if (added && added.name) {
      await refreshSources();
      selectSource(added.name);
      closeModal();
    }
  } catch (e) {
    /* ignore; the field stays open for a retry */
  }
}

function selectSource(name) {
  const source = state.sources.find((s) => s.name === name);
  if (!source) return;
  state.currentSource = name;
  state.traces = [];
  state.selected = null;

  document.getElementById("source-name").textContent = source.name;
  document.getElementById("source-name").classList.remove("muted");
  document.getElementById("source-path").textContent = source.path;
  document.getElementById("source-picker").classList.remove("empty");

  openStream(name);
  renderLog();
  renderInspector();
}

/* ---- Live stream ----------------------------------------------------- */

function openStream(name) {
  if (state.stream) state.stream.close();

  const url = `/api/stream?source=${encodeURIComponent(name)}&limit=${state.settings.limit}`;
  const es = new EventSource(url);
  state.stream = es;

  es.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }

    if (msg.kind === "snapshot") {
      // Snapshot arrives newest-first already.
      state.traces = msg.traces.slice(0, state.settings.limit);
    } else if (msg.kind === "append") {
      // New traces arrive oldest-first; prepend so newest ends up on top.
      for (const t of msg.traces) state.traces.unshift(t);
      if (state.traces.length > state.settings.limit) {
        state.traces.length = state.settings.limit;
      }
    }
    scheduleRender();
  };

  // On error EventSource auto-reconnects; nothing to do here.
}

/* Batch renders so a burst of appends doesn't thrash the DOM. */
let renderQueued = false;
function scheduleRender() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => {
    renderQueued = false;
    renderLog();
  });
}

/* ---- Log table ------------------------------------------------------- */

function renderLog() {
  const body = document.getElementById("log-body");
  const empty = document.getElementById("log-empty");
  const rows = filteredTraces();

  body.innerHTML = "";
  empty.hidden = rows.length > 0;

  for (const t of rows) {
    const tr = document.createElement("tr");
    tr.className = "log-row";
    if (t.status === "failed") tr.classList.add("failed");
    if (state.selected && state.selected.trace_id === t.trace_id) {
      tr.classList.add("selected");
    }
    tr.innerHTML = `
      <td>${fmtTime(t.started_at)}</td>
      <td class="col-action">${esc(t.action)}</td>
      <td><span class="status status-${t.status}">${esc(t.status)}</span></td>
      <td>${fmtDurShort(t.duration_ms)}</td>
      <td class="col-meta">${countTEB(t)}</td>
    `;
    tr.addEventListener("click", () => selectTrace(t));
    body.appendChild(tr);
  }
}

function filteredTraces() {
  if (!state.search) return state.traces;
  const q = state.search.toLowerCase();
  // Search across action, kind, status, correlation id, and touched targets —
  // the fields a developer is most likely to filter by ("only db", "anything
  // with button", or a correlation id to see one job's traces end to end).
  return state.traces.filter((t) => {
    if ((t.action || "").toLowerCase().includes(q)) return true;
    if ((t.kind || "").toLowerCase().includes(q)) return true;
    if ((t.status || "").toLowerCase().includes(q)) return true;
    if ((t.correlation_id || "").toLowerCase().includes(q)) return true;
    return (t.touches || []).some((x) =>
      (x.target || "").toLowerCase().includes(q)
    );
  });
}

/* ---- Selection + inspector ------------------------------------------ */

function selectTrace(t) {
  state.selected = t;
  // "Default trace view" governs which tab opens when a trace is picked.
  setTab(state.settings.defaultView);
  renderLog();       // refresh selected-row highlight
  renderInspector();
  if (state.activeTab === "map") renderMap();
}

function renderInspector() {
  const el = document.getElementById("inspector");
  const t = state.selected;
  if (!t) {
    el.innerHTML = `<div class="muted">Select a trace to inspect it.</div>`;
    return;
  }
  el.innerHTML = state.activeTab === "map"
    ? inspectorFull(t)
    : inspectorSummary(t);
  wireInspectorButtons();
}

/* Log-tab inspector: a compact summary card + actions. */
function inspectorSummary(t) {
  const touches = (t.touches || []).length;
  const errors = (t.errors || []).length;

  // Always show this trace's own id. Parent and root are only shown when they
  // add information: a root trace has no parent and is its own root, so those
  // lines would be noise. A child trace shows both, so the chain is visible.
  const lines = [`Trace:    ${shortId(t.trace_id)}`];
  if (t.parent_trace_id) {
    lines.push(`Parent:   ${shortId(t.parent_trace_id)}`);
  }
  if (t.root_trace_id && t.root_trace_id !== t.trace_id) {
    lines.push(`Root:     ${shortId(t.root_trace_id)}`);
  }
  if (t.correlation_id) {
    // Shown in full (not shortId-truncated): the point of a correlation id is
    // to copy/search it outside the app (search box, jq, grep), so a 6-char
    // truncation would defeat the purpose.
    lines.push(`Corr:     ${t.correlation_id}`);
  }
  lines.push(`Kind:     ${t.kind || ""}`);
  lines.push(`Duration: ${fmtDurLong(t.duration_ms)}`);
  lines.push(`Touches:  ${touches}`);
  lines.push(`Errors:   ${errors}`);

  return `
    <div class="insp-title">${esc(t.action)}</div>
    <div class="status status-${t.status}">● ${esc(t.status)}</div>
    <div class="insp-summary">${esc(lines.join("\n"))}</div>
    <div class="insp-actions">
      <button class="btn btn-primary" id="btn-view-map">View trace</button>
      <button class="btn" id="btn-copy">Copy JSON</button>
    </div>
  `;
}

/* Map-tab inspector: full steps / events / touches / errors breakdown. */
function inspectorFull(t) {
  return `
    <div class="insp-section-label">TRACE INSPECTOR</div>
    <div class="insp-title">${esc(t.action)}
      <span class="status-pill status-${t.status}">${esc(t.status)}</span></div>
    ${sectionSteps(t)}
    ${sectionEvents(t)}
    ${sectionTouches(t)}
    ${sectionErrors(t)}
  `;
}

function sectionSteps(t) {
  const steps = t.steps || [];
  const rows = steps.map((s) =>
    `<div class="insp-step"><span class="tick">✓</span>
       <span>${esc(s.label || "")}</span></div>`
  ).join("");
  return `<div class="insp-section-label">STEPS (${steps.length})</div>
    <div class="insp-list">${rows || `<span class="muted">none</span>`}</div>`;
}

function sectionEvents(t) {
  const events = t.events || [];
  const rows = events.map((e) => {
    const ok = e.status !== "failed";
    const arrow = `${esc(e.operation || "")} → ${esc(e.target || "")}`;
    const sub = eventSubline(e);
    return `<div class="insp-event">
      <div class="insp-event-head">${kindBadge(e.kind)}
        <span>${ok ? "✓" : "✕"} ${arrow}</span></div>
      ${sub ? `<div class="insp-event-sub">${esc(sub)}</div>` : ""}
    </div>`;
  }).join("");
  return `<div class="insp-section-label">EVENTS (${events.length})</div>
    <div class="insp-list">${rows || `<span class="muted">none</span>`}</div>`;
}

function eventSubline(e) {
  // Show the most useful bit of the event's result, if present.
  const r = e.result;
  if (r && typeof r === "object") {
    const key = Object.keys(r)[0];
    if (key) return `${key}: ${r[key]}`;
  }
  return "";
}

function sectionTouches(t) {
  const touches = t.touches || [];
  const chips = touches.map((x) =>
    `<span class="touch-chip"><span class="tk">${esc(x.kind || "")}</span>${esc(x.target || "")}</span>`
  ).join("");
  return `<div class="insp-section-label">TOUCHES (${touches.length})</div>
    <div class="touch-chips">${chips || `<span class="muted">none</span>`}</div>`;
}

function sectionErrors(t) {
  const errors = t.errors || [];
  if (errors.length === 0) {
    return `<div class="insp-section-label">ERRORS (0)</div>
      <div class="muted">none</div>`;
  }
  const rows = errors.map((e) =>
    `<div class="insp-error">${esc(e.type || "error")}: ${esc(e.message || "")}</div>`
  ).join("");
  return `<div class="insp-section-label">ERRORS (${errors.length})</div>
    <div class="insp-list">${rows}</div>`;
}

function wireInspectorButtons() {
  const view = document.getElementById("btn-view-map");
  if (view) view.addEventListener("click", () => setTab("map"));
  const copy = document.getElementById("btn-copy");
  if (copy) {
    copy.addEventListener("click", () => {
      navigator.clipboard?.writeText(JSON.stringify(state.selected, null, 2));
      copy.textContent = "Copied";
      setTimeout(() => (copy.textContent = "Copy JSON"), 1200);
    });
  }
}

/* ---- Trace map ------------------------------------------------------- */

function renderMap() {
  const wrap = document.getElementById("map-wrap");
  const caption = document.getElementById("map-caption");
  const t = state.selected;
  if (!t) {
    caption.textContent = "SELECT A TRACE";
    wrap.innerHTML = `<div class="empty-state">Pick a trace from the log.</div>`;
    return;
  }
  caption.textContent = `${(t.action || "").toUpperCase()} · ${shortId(t.trace_id).toUpperCase()}`;
  const { svg, order } = buildMap(t);
  wrap.innerHTML = svg;
  startReplay(order);
}

/* Build a columnar map: the trace as ORIGIN, its top-level events in THROUGH,
 * and child events (those with a parent_event_id) in ONWARD. Edges flow left to
 * right with an animated dash so the trace reads as "playing" on repeat.
 * Falls back to touches as THROUGH nodes when a trace has no events. */
function buildMap(t) {
  const COLS = [40, 300, 560];
  const NODE_W = 168, NODE_H = 56, GAP = 26, TOP = 56;

  const events = t.events || [];
  const origin = {
    id: "origin",
    title: t.project || t.action || "trace",
    kind: t.kind || "app",
    status: t.status,
  };

  let through, onward;
  if (events.length > 0) {
    through = events
      .filter((e) => !e.parent_event_id)
      .map((e) => nodeFromEvent(e));
    onward = events
      .filter((e) => e.parent_event_id)
      .map((e) => nodeFromEvent(e));
    // If nothing is marked top-level, treat all events as THROUGH.
    if (through.length === 0) { through = events.map(nodeFromEvent); onward = []; }
  } else {
    through = (t.touches || []).map((x, i) => ({
      id: "t" + i, title: x.target || "", kind: x.kind || "app", status: t.status,
    }));
    onward = [];
  }

  const columns = [[origin], through, onward];
  const height = Math.max(
    TOP * 2 + columns[1].length * (NODE_H + GAP),
    TOP * 2 + NODE_H,
    360
  );
  const width = 760;

  // Position nodes: each column centred vertically.
  const placed = {};
  columns.forEach((col, ci) => {
    const totalH = col.length * NODE_H + (col.length - 1) * GAP;
    let y = (height - totalH) / 2;
    col.forEach((n) => {
      placed[n.id] = { ...n, x: COLS[ci], y, ci };
      y += NODE_H + GAP;
    });
  });

  // Edges: origin → each THROUGH node; each THROUGH → each ONWARD node.
  const edges = [];
  for (const n of through) edges.push(["origin", n.id]);
  for (const n of onward) for (const th of through) edges.push([th.id, n.id]);

  const labels = ["ORIGIN", "THROUGH", "ONWARD"]
    .map((lab, i) => columns[i].length
      ? `<text class="map-col-label" x="${COLS[i]}" y="34">${lab}</text>` : "")
    .join("");

  const edgeSvg = edges.map(([a, b]) => {
    const s = placed[a], d = placed[b];
    if (!s || !d) return "";
    const x1 = s.x + NODE_W, y1 = s.y + NODE_H / 2;
    const x2 = d.x, y2 = d.y + NODE_H / 2;
    const mx = (x1 + x2) / 2;
    const failed = d.status === "failed" ? " failed" : "";
    return `<path class="map-edge${failed}" data-target="${esc(b)}" d="M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}"/>`;
  }).join("");

  const nodesSvg = Object.values(placed)
    .map((n) => nodeSvg(n, NODE_W, NODE_H)).join("");

  const svg = `<svg viewBox="0 0 ${width} ${height}" width="${width}" height="${height}"
    xmlns="http://www.w3.org/2000/svg">${labels}${edgeSvg}${nodesSvg}</svg>`;

  // The reveal order for the sequential replay: origin first, then the THROUGH
  // column, then ONWARD — so the trace "plays" left to right along its path.
  const order = [origin.id, ...through.map((n) => n.id), ...onward.map((n) => n.id)];
  return { svg, order };
}

function nodeFromEvent(e) {
  return {
    id: e.event_id || Math.random().toString(36).slice(2),
    title: e.target || e.operation || e.kind || "event",
    kind: e.kind || "app",
    status: e.status || "completed",
    sub: e.operation ? `${e.kind}.${e.operation}` : e.kind,
    parent: e.parent_event_id || null,
  };
}

function nodeSvg(n, w, h) {
  const color = statusColor(n.status);
  const mark = n.status === "failed" ? "✕" : "✓";
  return `
    <g class="map-node" data-node-id="${esc(n.id)}" transform="translate(${n.x},${n.y})">
      <rect class="map-node-box" width="${w}" height="${h}" rx="8"></rect>
      <text class="map-node-title" x="16" y="24">${esc(truncate(n.title, 18))}</text>
      <text class="map-node-kind" x="16" y="42" fill="${kindColor(n.kind)}">${esc(n.sub || n.kind)}</text>
      <circle cx="${w}" cy="${h}" r="11" fill="${color}"></circle>
      <text x="${w}" y="${h + 4}" text-anchor="middle" font-size="12" fill="#0a0c0d">${mark}</text>
    </g>`;
}

/* ---- Trace map replay ------------------------------------------------
 *
 * A sequential step-through: the map starts dim and lights up one node at a
 * time along the trace's path, then loops. Speed is configurable (0.5× / 1× /
 * 2×) and the whole thing can be paused, which freezes the full map in view. */

const replay = { timer: null, order: [], index: 0 };

/* Milliseconds per step, derived from the chosen speed (1×–10×). Base beat is
 * 900ms: 1× → 900ms (slow), 10× → 90ms (fast). */
function replayBeatMs() {
  return 900 / (state.settings.replaySpeed || 1);
}

function startReplay(order) {
  stopReplay();
  replay.order = order || [];
  replay.index = 0;

  // Paused: show the whole map at once and don't animate.
  if (state.replayPaused) { revealAll(); return; }
  if (replay.order.length === 0) return;

  resetReveal();
  // The beat is read fresh each step (not captured once), so dragging the speed
  // slider takes effect on the very next step without restarting the replay.
  const tick = () => {
    if (replay.index < replay.order.length) {
      revealNode(replay.order[replay.index], /*active=*/true);
      replay.index += 1;
      replay.timer = setTimeout(tick, replayBeatMs());
    } else {
      // Hold the completed map briefly, then loop from the start.
      replay.timer = setTimeout(() => {
        resetReveal();
        replay.index = 0;
        tick();
      }, replayBeatMs() * 2.5);
    }
  };
  tick();
}

function stopReplay() {
  if (replay.timer) { clearTimeout(replay.timer); replay.timer = null; }
}

function mapSvg() { return document.getElementById("map-wrap"); }

function revealNode(id, active) {
  const svg = mapSvg();
  if (!svg) return;
  if (active) {
    svg.querySelectorAll(".map-node.active")
      .forEach((n) => n.classList.remove("active"));
  }
  const sel = (window.CSS && CSS.escape) ? CSS.escape(id) : id;
  const node = svg.querySelector(`[data-node-id="${sel}"]`);
  if (node) {
    node.classList.add("revealed");
    if (active) node.classList.add("active");
  }
  // Light up every edge that leads into this node.
  svg.querySelectorAll(`[data-target="${sel}"]`)
    .forEach((e) => e.classList.add("revealed"));
}

function resetReveal() {
  const svg = mapSvg();
  if (!svg) return;
  svg.querySelectorAll(".map-node").forEach((n) => n.classList.remove("revealed", "active"));
  svg.querySelectorAll(".map-edge").forEach((e) => e.classList.remove("revealed"));
}

function revealAll() {
  const svg = mapSvg();
  if (!svg) return;
  svg.querySelectorAll(".map-node").forEach((n) => n.classList.add("revealed"));
  svg.querySelectorAll(".map-edge").forEach((e) => e.classList.add("revealed"));
  svg.querySelectorAll(".map-node.active").forEach((n) => n.classList.remove("active"));
}

function wireReplayControls() {
  // Two sliders control the same replay speed: the one in the map toolbar
  // (on-the-fly changes) and the one on the Settings page (the default). Both
  // call setReplaySpeed, which keeps the pair in sync.
  ["speed-slider", "settings-speed-slider"].forEach((id) => {
    const slider = document.getElementById(id);
    if (slider) slider.addEventListener("input", () => setReplaySpeed(slider.value));
  });
  document.getElementById("replay-toggle").addEventListener("click", toggleReplay);
  syncSpeedControls();
}

function setReplaySpeed(value) {
  state.settings.replaySpeed = parseFloat(value);
  saveSettings();
  syncSpeedControls();
  // No restart needed: the running replay reads replayBeatMs() fresh each step,
  // so a new speed takes effect on the next step, even mid-drag.
}

/* Reflect the current speed onto both sliders and both labels, so the map
 * toolbar and the Settings page always agree. */
function syncSpeedControls() {
  const v = state.settings.replaySpeed;
  ["speed-slider", "settings-speed-slider"].forEach((id) => {
    const s = document.getElementById(id);
    if (s) s.value = String(v);
  });
  ["speed-value", "settings-speed-value"].forEach((id) => {
    const l = document.getElementById(id);
    if (l) l.textContent = `${v}×`;
  });
}

function toggleReplay() {
  state.replayPaused = !state.replayPaused;
  const btn = document.getElementById("replay-toggle");
  if (state.replayPaused) {
    stopReplay();
    revealAll();
    btn.textContent = "▶ Play";
  } else {
    btn.textContent = "⏸ Pause";
    renderMap();  // rebuild and start the sequential replay again
  }
}

/* ---- Navigation, tabs, search --------------------------------------- */

function wireNav() {
  document.querySelectorAll("[data-nav]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("[data-nav]").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      const nav = btn.dataset.nav;
      document.getElementById("traces-view").hidden = nav !== "traces";
      document.getElementById("settings-view").hidden = nav !== "settings";
    });
  });
}

function wireTabs() {
  document.querySelectorAll("[data-tab]").forEach((btn) => {
    btn.addEventListener("click", () => setTab(btn.dataset.tab));
  });
}

function setTab(tab) {
  state.activeTab = tab;
  // Stop any running replay when leaving the map; renderMap restarts it on entry.
  if (tab !== "map") stopReplay();
  document.querySelectorAll("[data-tab]").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === tab));
  document.getElementById("view-log").hidden = tab !== "log";
  document.getElementById("view-map").hidden = tab !== "map";
  renderInspector();
  if (tab === "map") renderMap();
}

function wireSearch() {
  document.getElementById("search").addEventListener("input", (e) => {
    state.search = e.target.value;
    renderLog();
  });
}

/* ---- Settings -------------------------------------------------------- */

function wireSettings() {
  document.querySelectorAll(".choice-group").forEach((group) => {
    const key = group.dataset.setting;
    group.querySelectorAll(".choice").forEach((choice) => {
      choice.addEventListener("click", () => {
        group.querySelectorAll(".choice").forEach((c) => c.classList.remove("active"));
        choice.classList.add("active");
        setSetting(key, choice.dataset.value);
      });
    });
  });
}

function setSetting(key, value) {
  state.settings[key] = key === "limit" ? parseInt(value, 10) : value;
  saveSettings();
  applySettings();
  if (key === "limit" && state.currentSource) {
    // Re-open the stream so the server's snapshot honours the new row count.
    openStream(state.currentSource);
  }
}

function applySettings() {
  document.documentElement.setAttribute("data-accent", state.settings.accent);
  document.documentElement.setAttribute("data-density", state.settings.density);
  // Reflect stored settings onto the choice buttons.
  document.querySelectorAll(".choice-group").forEach((group) => {
    const key = group.dataset.setting;
    const val = String(state.settings[key]);
    group.querySelectorAll(".choice").forEach((c) =>
      c.classList.toggle("active", c.dataset.value === val));
  });
}

function loadSettings() {
  const defaults = { accent: "green", density: "comfortable", defaultView: "log", limit: 100, replaySpeed: 1 };
  try {
    return { ...defaults, ...JSON.parse(localStorage.getItem("traceact.settings") || "{}") };
  } catch (e) {
    return defaults;
  }
}

function saveSettings() {
  try { localStorage.setItem("traceact.settings", JSON.stringify(state.settings)); }
  catch (e) { /* private mode; ignore */ }
}

/* ---- Diagnostics (Settings > Run diagnostics) ------------------------ */
/* Runs the same checks as `traceact doctor` on the CLI, via GET /api/doctor,
 * and renders them as a staggered checklist with a progress bar. The server
 * runs every check synchronously in one request (they're all near-instant —
 * a version check, a directory-permission check, an optional file read) so
 * there's nothing to poll; the stagger below is a readability aid so results
 * appear as a checklist filling in rather than all at once, not a simulation
 * of slow work. */

function wireDoctor() {
  document.getElementById("run-doctor").addEventListener("click", runDoctor);
}

async function runDoctor() {
  const btn = document.getElementById("run-doctor");
  const progress = document.getElementById("doctor-progress");
  const fill = document.getElementById("doctor-progress-fill");
  const label = document.getElementById("doctor-progress-label");
  const results = document.getElementById("doctor-results");

  btn.disabled = true;
  results.hidden = true;
  results.innerHTML = "";
  progress.hidden = false;
  fill.style.width = "0%";
  label.textContent = "Running diagnostics…";

  let data;
  try {
    const params = state.currentSource
      ? `?source=${encodeURIComponent(state.sources.find((s) => s.name === state.currentSource)?.path || "")}`
      : "";
    const res = await fetch(`/api/doctor${params}`);
    data = await res.json();
  } catch (e) {
    progress.hidden = true;
    results.hidden = false;
    results.innerHTML = `<div class="doctor-check fail">
      <span class="doctor-icon">✗</span>
      <div><div class="doctor-message">Could not reach the viewer server to run diagnostics.</div></div>
    </div>`;
    btn.disabled = false;
    return;
  }

  const checks = data.checks || [];
  const total = checks.length;

  for (let i = 0; i < total; i++) {
    label.textContent = `Running check ${i + 1}/${total}…`;
    fill.style.width = `${Math.round(((i + 1) / total) * 100)}%`;
    appendDoctorCheck(results, checks[i]);
    results.hidden = false;
    // Short stagger so the checklist visibly fills in rather than jumping to
    // a wall of text — all checks have already run server-side by this point.
    if (i < total - 1) await new Promise((r) => setTimeout(r, 120));
  }

  progress.hidden = true;
  const summary = document.createElement("div");
  summary.className = `doctor-summary ${data.ok ? "ok" : "fail"}`;
  summary.textContent = data.ok
    ? "All checks passed."
    : "Some checks failed — see above.";
  results.appendChild(summary);

  btn.disabled = false;
}

function appendDoctorCheck(container, check) {
  const row = document.createElement("div");
  row.className = `doctor-check ${check.status}`;
  const icon = check.status === "pass" ? "✓" : check.status === "fail" ? "✗" : "·";
  row.innerHTML = `
    <span class="doctor-icon">${icon}</span>
    <div>
      <div class="doctor-message">${esc(check.message)}</div>
      ${check.hint ? `<div class="doctor-hint">${esc(check.hint)}</div>` : ""}
    </div>
  `;
  container.appendChild(row);
}

/* ---- Modal (add source) --------------------------------------------- */

function wireModal() {
  document.getElementById("source-picker").addEventListener("click", openModal);
  document.getElementById("empty-add-source").addEventListener("click", (e) => { e.preventDefault(); openModal(); });

  // Path-input fallback (inside <details>).
  document.getElementById("source-add").addEventListener("click", () => {
    addSource(document.getElementById("source-input").value);
  });
  document.getElementById("source-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") addSource(e.target.value);
  });

  // Native OS picker buttons.
  document.getElementById("pick-file").addEventListener("click", () => pickSource("file"));
  document.getElementById("pick-folder").addEventListener("click", () => pickSource("folder"));

  // Drop zone.
  const zone = document.getElementById("drop-zone");
  zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("drag-over"); });
  zone.addEventListener("dragleave", () => zone.classList.remove("drag-over"));
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    zone.classList.remove("drag-over");
    const file = e.dataTransfer.files[0];
    if (!file) return;
    if (!file.name.endsWith(".jsonl")) {
      setDropStatus("Only .jsonl files are supported.", true);
      return;
    }
    importDroppedFile(file);
  });

  // Close on backdrop click.
  document.getElementById("source-modal").addEventListener("click", (e) => {
    if (e.target.id === "source-modal") closeModal();
  });
}

/* Open the native OS file or folder picker via the server.  The server runs
 * osascript (macOS) or tkinter so it can return the real filesystem path,
 * which is what we need for live tailing. */
async function pickSource(type) {
  setDropStatus(type === "folder" ? "Opening folder picker…" : "Opening file picker…");
  try {
    const resp = await fetch(`/api/pick?type=${type}`);
    const data = await resp.json();
    if (data.cancelled || !data.path) {
      setDropStatus("");
      return;
    }
    setDropStatus("Adding source…");
    await addSource(data.path);
    setDropStatus("");
  } catch (err) {
    setDropStatus("Picker unavailable. Use the path input below.", true);
  }
}

/* Accept a dragged-and-dropped .jsonl file.  The browser reads the contents
 * and POSTs them to /api/import, which saves a copy to ~/.traceact/imports/
 * and registers it as a source.  Because we copy the file rather than tail
 * the original, this is a static snapshot — new writes to the original won't
 * appear.  The UI labels it "(imported)". */
async function importDroppedFile(file) {
  setDropStatus(`Importing ${file.name}…`);
  const content = await file.text();
  try {
    const resp = await fetch("/api/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: file.name, content, label: file.name.replace(".jsonl", "") }),
    });
    const data = await resp.json();
    if (data.error) { setDropStatus(data.error, true); return; }
    await refreshSources();
    selectSource(data.name);
    closeModal();
    setDropStatus("");
  } catch (err) {
    setDropStatus("Import failed. Try the path input below.", true);
  }
}

function setDropStatus(msg, isError = false) {
  const el = document.getElementById("drop-status");
  if (!el) return;
  el.textContent = msg;
  el.className = "drop-status" + (isError ? " error" : "");
}

function openModal() {
  renderSourceList();
  setDropStatus("");
  document.getElementById("source-modal").hidden = false;
}
function closeModal() {
  document.getElementById("source-modal").hidden = true;
  document.getElementById("source-input").value = "";
  setDropStatus("");
}

function renderSourceList() {
  const list = document.getElementById("source-list");
  if (!list) return;
  if (state.sources.length === 0) {
    list.innerHTML = `<div class="muted" style="padding:6px 0">No sources yet.</div>`;
    return;
  }
  list.innerHTML = state.sources.map((s) => `
    <div class="source-option" data-name="${esc(s.name)}">
      <div class="name">${esc(s.name)}</div>
      <div class="path">${esc(s.path)}</div>
    </div>`).join("");
  list.querySelectorAll(".source-option").forEach((row) => {
    row.addEventListener("click", () => { selectSource(row.dataset.name); closeModal(); });
  });
}

/* ---- Formatting helpers --------------------------------------------- */

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return esc(iso);
  return d.toTimeString().slice(0, 8);
}

function fmtDurShort(ms) {
  if (ms == null) return "";
  return ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(1)}s`;
}
function fmtDurLong(ms) {
  if (ms == null) return "";
  return ms < 1000 ? `${ms.toFixed(1)}ms` : `${(ms / 1000).toFixed(2)}s`;
}

function countTEB(t) {
  const touches = (t.touches || []).length;
  const errors = (t.errors || []).length;
  const budget = t.budget_hit ? "Y" : "—";
  return `${touches}·${errors}·${budget}`;
}

function shortId(id) {
  return String(id || "").replace(/^trc_/, "").slice(0, 6);
}

function truncate(s, n) {
  s = String(s || "");
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

function kindBadge(kind) {
  const k = String(kind || "").toLowerCase();
  return `<span class="badge badge-${k}">${esc(k)}</span>`;
}

function statusColor(status) {
  return {
    completed: "#4ade80", failed: "#f87171", running: "#eab308",
    cancelled: "#6b7280", pending: "#8b949e",
  }[status] || "#4ade80";
}

function kindColor(kind) {
  return {
    app: "#60a5fa", db: "#c084fc", http: "#fb923c", file: "#34d399",
    model: "#f472b6", job: "#38bdf8", log: "#a3a3a3", notify: "#fbbf24",
  }[String(kind || "").toLowerCase()] || "#8b949e";
}

document.addEventListener("DOMContentLoaded", init);
