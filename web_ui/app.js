/**
 * app.js — SIP·VAD Monitor
 * Pure Socket.IO — zero polling.
 * Connects to sip_server.py Socket.IO server on port 5000.
 *
 * Socket events handled:
 *   "connect"        — socket connected
 *   "disconnect"     — socket disconnected
 *   "connect_error"  — connection failure
 *   "processedAudio" — per-frame metrics  { seq, is_speech, total_frames,
 *                       speech_frames, silence_frames, speech_ratio,
 *                       avg_latency, fps, snr_db, raw_energy,
 *                       denoised_energy, speech_event, active_calls,
 *                       call_id }
 *   "transcript"     — STT result  { call_id, text, timestamp }
 *   "audioCleared"   — backend confirmed clear
 */

"use strict";

// ═══════════════════════════════════════════════════════════════════════════
// CONFIG — change SERVER_URL to match your backend machine
// ═══════════════════════════════════════════════════════════════════════════
const SERVER_URL          = "http://192.168.31.129:5000";
const ROLLING_WINDOW      = 60;      // chart data points
const LOG_MAX             = 40;      // max event log rows
const TX_MAX              = 50;      // max transcript rows
const HEARTBEAT_TIMEOUT   = 4000;    // ms before badge goes IDLE
const DEBUG               = new URLSearchParams(location.search).has("debug");

// ═══════════════════════════════════════════════════════════════════════════
// STATE
// ═══════════════════════════════════════════════════════════════════════════
let logCount       = 0;
let txCount        = 0;
let heartbeatTimer = null;

// Chart rolling buffers
const buf = {
  labels:  [],
  ratio:   [],
  fps:     [],
  latency: [],
  snr:     [],
};

// Chart instances — set up after DOM ready
let ratioChart = null;
let perfChart  = null;
let snrChart   = null;

// ═══════════════════════════════════════════════════════════════════════════
// SAFE DOM HELPER — prevents crashes if an element is missing
// ═══════════════════════════════════════════════════════════════════════════
function $(id) {
  return document.getElementById(id) || null;
}

// ═══════════════════════════════════════════════════════════════════════════
// SOCKET.IO CONNECTION
// ═══════════════════════════════════════════════════════════════════════════
const socket = io(SERVER_URL, {
  transports:            ["polling", "websocket"],  // polling first avoids WS upgrade race
  reconnection:          true,
  reconnectionAttempts:  Infinity,
  reconnectionDelay:     1000,
  reconnectionDelayMax:  5000,
  timeout:               20000,
});

// ── connect ──────────────────────────────────────────────────────────────────
socket.on("connect", () => {
  dbg(`connect — id=${socket.id}`);
  setConnected(true);
  appendLog("system", `◉ Connected  —  id: ${socket.id}`);
});

// ── disconnect ───────────────────────────────────────────────────────────────
socket.on("disconnect", (reason) => {
  dbg(`disconnect — ${reason}`);
  setConnected(false);
  setStateBadge("IDLE");
  appendLog("warn", `◎ Disconnected: ${reason}`);
});

// ── connect_error ─────────────────────────────────────────────────────────────
socket.on("connect_error", (err) => {
  dbg(`connect_error — ${err.message}`);
  appendLog("error", `⚠ Connection error: ${err.message}`);
});

// ── processedAudio ────────────────────────────────────────────────────────────
socket.on("processedAudio", (data) => {
  dbg(`processedAudio seq=${data.seq}`);
  handleFrame(data);
});

// ── transcript ────────────────────────────────────────────────────────────────
// Backend emits: { call_id: string, text: string, timestamp: number }
socket.on("transcript", (data) => {
  dbg(`transcript call=${data.call_id} text="${data.text}"`);
  appendLog("event", `🧠 TRANSCRIPT [${shortCallId(data.call_id)}]: ${data.text}`);
  appendTranscript(data.call_id, data.text, data.timestamp);
});

// ── audioCleared ─────────────────────────────────────────────────────────────
socket.on("audioCleared", () => {
  dbg("audioCleared");
  appendLog("system", "🧹 Audio cleared by backend");
});

// ═══════════════════════════════════════════════════════════════════════════
// FRAME HANDLER — called for every "processedAudio" event
// ═══════════════════════════════════════════════════════════════════════════
function handleFrame(d) {
  const now = new Date();

  // ── Heartbeat ──
  resetHeartbeatTimer();
  pulseHeartbeat();
  const el = $("hb-ts");
  if (el) el.textContent = now.toLocaleTimeString();

  // ── State badge ──
  setStateBadge(d.is_speech ? "SPEAKING" : "SILENT");

  // ── Metric cards ──
  setMetric("m-total",   fmt(d.total_frames   ?? 0));
  setMetric("m-speech",  fmt(d.speech_frames  ?? 0));
  setMetric("m-silence", fmt(d.silence_frames ?? 0));
  setMetricHTML("m-ratio",   `${(d.speech_ratio ?? 0).toFixed(1)}<span class="unit">%</span>`);
  setMetricHTML("m-latency", `${(d.avg_latency  ?? 0).toFixed(1)}<span class="unit">ms</span>`);
  setMetricHTML("m-fps",     `${(d.fps          ?? 0).toFixed(1)}<span class="unit">fps</span>`);
  setMetricHTML("m-snr",     `${(d.snr_db       ?? 0).toFixed(1)}<span class="unit">dB</span>`);
  setVal("m-starts", d.speech_start ?? 0);
  setVal("m-ends",   d.speech_end   ?? 0);

  // ── Health bar ──
  const ratio = Math.min(d.speech_ratio ?? 0, 100);
  setStyle("health-fill", "width", `${ratio}%`);
  setVal("health-val", `${ratio.toFixed(1)}%`);
  const calls = d.active_calls ?? 0;
  setVal("active-calls-label", `${calls} CALL${calls !== 1 ? "S" : ""}`);

  // ── Footer ──
  setVal("footer-seq",   `SEQ #${d.seq ?? "—"}`);
  setVal("footer-calls", `${calls} active call${calls !== 1 ? "s" : ""}`);
  setVal("footer-ts",    now.toLocaleTimeString());

  // ── Energy bars ──
  const rawE  = d.raw_energy      ?? 0;
  const clnE  = d.denoised_energy ?? 0;
  const maxE  = Math.max(rawE, clnE, 1);
  setBar("bar-raw",   rawE / maxE * 100, "num-raw",   rawE.toFixed(0));
  setBar("bar-clean", clnE / maxE * 100, "num-clean", clnE.toFixed(0));
  const snrAbs = Math.abs(d.snr_db ?? 0);
  setBar("bar-delta", Math.min(snrAbs / 30, 1) * 100, "num-delta", `${(d.snr_db ?? 0).toFixed(1)} dB`);

  // ── Rolling chart buffers ──
  const ts = now.toLocaleTimeString("en-US", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
  bufPush(buf.labels,  ts);
  bufPush(buf.ratio,   d.speech_ratio ?? 0);
  bufPush(buf.fps,     d.fps          ?? 0);
  bufPush(buf.latency, d.avg_latency  ?? 0);
  bufPush(buf.snr,     d.snr_db       ?? 0);

  // Live chart tags
  setVal("ratio-live", `${(d.speech_ratio ?? 0).toFixed(1)}%`);
  setVal("perf-live",  `${(d.fps ?? 0).toFixed(1)} fps`);
  setVal("snr-live",   `${(d.snr_db ?? 0).toFixed(1)} dB`);

  updateCharts();

  // ── Event log (throttled — only speech events + every 10th frame) ──
  if (d.speech_event && d.speech_event !== "") {
    const icon = d.speech_event === "speech_start" ? "🗣" : "🤫";
    appendLog("event", `${icon} ${d.speech_event.toUpperCase()}  seq=${d.seq}  SNR=${(d.snr_db ?? 0).toFixed(1)}dB`);
  } else if ((d.seq ?? 0) % 10 === 0) {
    const tag = d.is_speech ? "speech" : "silence";
    appendLog(tag, `seq=${d.seq}  fps=${(d.fps ?? 0).toFixed(1)}  lat=${(d.avg_latency ?? 0).toFixed(1)}ms  snr=${(d.snr_db ?? 0).toFixed(1)}dB`);
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// TRANSCRIPT — dedicated section, never competes with the log
// ═══════════════════════════════════════════════════════════════════════════

/**
 * appendTranscript(callId, text, timestamp)
 *
 * Appends a new transcript entry at the BOTTOM of the transcript panel
 * (teletype / paper-tape order — newest at bottom) and auto-scrolls.
 * Safe: does nothing if the panel elements are missing.
 */
function appendTranscript(callId, text, timestamp) {
  const scroll = $("tx-scroll");
  if (!scroll) return;

  // Remove the "awaiting" placeholder on first real entry
  const empty = $("tx-empty");
  if (empty) empty.remove();

  // Trim oldest entries if over limit
  const rows = scroll.querySelectorAll(".tx-row");
  if (rows.length >= TX_MAX) {
    rows[0].remove();
  }

  // Build row
  const row = document.createElement("div");
  row.className = "tx-row new-entry";

  // Timestamp — prefer backend timestamp if provided, else now
  const ts = timestamp
    ? new Date(timestamp * 1000).toLocaleTimeString("en-US", { hour12: false })
    : new Date().toLocaleTimeString("en-US", { hour12: false });

  const shortId = shortCallId(callId ?? "—");

  row.innerHTML = `
    <div class="tx-meta">
      <span class="tx-ts">${escHtml(ts)}</span>
      <span class="tx-call">${escHtml(shortId)}</span>
    </div>
    <span class="tx-text">${escHtml(text ?? "")}</span>`;

  scroll.appendChild(row);

  // Remove animation class after it plays so it doesn't replay on reflow
  row.addEventListener("animationend", () => row.classList.remove("new-entry"), { once: true });

  // Auto-scroll to bottom
  scroll.scrollTop = scroll.scrollHeight;

  // Update counter
  txCount++;
  setVal("tx-count", `${txCount} segment${txCount !== 1 ? "s" : ""}`);
}

// Shorten a SIP Call-ID to last 8 chars for display
function shortCallId(id) {
  if (!id || id === "—") return "—";
  const s = String(id);
  return s.length > 8 ? "…" + s.slice(-8) : s;
}

// ═══════════════════════════════════════════════════════════════════════════
// EVENT LOG
// ═══════════════════════════════════════════════════════════════════════════

/**
 * appendLog(type, msg)
 * Types: system | speech | silence | event | warn | error
 * Entries are prepended (newest at top).
 */
function appendLog(type, msg) {
  const log = $("event-log");
  if (!log) return;

  const row  = document.createElement("div");
  row.className = `log-row log-${type}`;
  const ts   = new Date().toLocaleTimeString("en-US", { hour12: false });
  row.innerHTML = `<span class="log-ts">${ts}</span><span class="log-msg">${escHtml(String(msg))}</span>`;

  log.prepend(row);
  logCount++;
  setVal("log-count", `${logCount} events`);

  // Trim old entries
  while (log.children.length > LOG_MAX) {
    log.removeChild(log.lastChild);
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// CHARTS — initialised after DOM is ready (DOMContentLoaded)
// ═══════════════════════════════════════════════════════════════════════════
function initCharts() {
  const FONT  = { family: "'Share Tech Mono', monospace", size: 9 };
  const GRID  = "rgba(255,255,255,0.04)";
  const TICKC = "#3d5560";

  const BASE = {
    responsive:          true,
    maintainAspectRatio: false,
    animation:           { duration: 0 },   // no animation — keep RTP latency low
    plugins: {
      legend:  { display: false },
      tooltip: { enabled: true, mode: "index", intersect: false,
                 titleFont: FONT, bodyFont: FONT,
                 backgroundColor: "#0b1014", borderColor: "#1a2830", borderWidth: 1 },
    },
    scales: {
      x: { display: false, grid: { display: false } },
      y: { grid: { color: GRID }, border: { display: false },
           ticks: { color: TICKC, font: FONT } },
    },
  };

  function grad(ctx, top, bot) {
    const g = ctx.createLinearGradient(0, 0, 0, 80);
    g.addColorStop(0, top);
    g.addColorStop(1, bot);
    return g;
  }

  // ── Speech Ratio ──
  const rCtx  = $("chart-ratio")?.getContext("2d");
  if (rCtx) {
    ratioChart = new Chart(rCtx, {
      type: "line",
      data: {
        labels: [],
        datasets: [{
          data: [],
          borderColor: "#00ff88", borderWidth: 2,
          backgroundColor: grad(rCtx, "rgba(0,255,136,0.3)", "rgba(0,255,136,0.01)"),
          pointRadius: 0, tension: 0.4, fill: true,
        }],
      },
      options: {
        ...BASE,
        scales: {
          ...BASE.scales,
          y: { ...BASE.scales.y, min: 0, max: 100,
               ticks: { ...BASE.scales.y.ticks, callback: v => v + "%" } },
        },
      },
    });
  }

  // ── FPS + Latency dual-axis ──
  const pCtx  = $("chart-perf")?.getContext("2d");
  if (pCtx) {
    perfChart = new Chart(pCtx, {
      type: "line",
      data: {
        labels: [],
        datasets: [
          { data: [], borderColor: "#a78bfa", borderWidth: 2,
            backgroundColor: grad(pCtx, "rgba(167,139,250,0.3)", "rgba(167,139,250,0.01)"),
            pointRadius: 0, tension: 0.4, fill: true, yAxisID: "yFps" },
          { data: [], borderColor: "#fb923c", borderWidth: 1.5,
            backgroundColor: grad(pCtx, "rgba(251,146,60,0.2)", "rgba(251,146,60,0.01)"),
            pointRadius: 0, tension: 0.4, fill: true, yAxisID: "yLat", borderDash: [4, 3] },
        ],
      },
      options: {
        ...BASE,
        scales: {
          x: BASE.scales.x,
          yFps: { position: "left",  grid: { color: GRID }, border: { display: false },
                  ticks: { color: "#a78bfa", font: FONT } },
          yLat: { position: "right", grid: { display: false }, border: { display: false },
                  ticks: { color: "#fb923c", font: FONT } },
        },
      },
    });
  }

  // ── SNR ──
  const sCtx  = $("chart-snr")?.getContext("2d");
  if (sCtx) {
    snrChart = new Chart(sCtx, {
      type: "line",
      data: {
        labels: [],
        datasets: [{
          data: [],
          borderColor: "#2dd4bf", borderWidth: 2,
          backgroundColor: grad(sCtx, "rgba(45,212,191,0.3)", "rgba(45,212,191,0.01)"),
          pointRadius: 0, tension: 0.4, fill: true,
        }],
      },
      options: {
        ...BASE,
        scales: {
          ...BASE.scales,
          y: { ...BASE.scales.y,
               ticks: { ...BASE.scales.y.ticks, callback: v => v.toFixed(0) + "dB" } },
        },
      },
    });
  }
}

function updateCharts() {
  if (ratioChart) {
    ratioChart.data.labels           = [...buf.labels];
    ratioChart.data.datasets[0].data = [...buf.ratio];
    ratioChart.update("none");
  }
  if (perfChart) {
    perfChart.data.labels            = [...buf.labels];
    perfChart.data.datasets[0].data  = [...buf.fps];
    perfChart.data.datasets[1].data  = [...buf.latency];
    perfChart.update("none");
  }
  if (snrChart) {
    snrChart.data.labels             = [...buf.labels];
    snrChart.data.datasets[0].data   = [...buf.snr];
    snrChart.update("none");
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// UI STATE HELPERS
// ═══════════════════════════════════════════════════════════════════════════
function setConnected(on) {
  const pill  = $("conn-pill");
  const dot   = $("conn-dot");
  const label = $("conn-label");
  if (pill)  pill.classList.toggle("connected", on);
  if (dot)   dot.classList.toggle("on", on);
  if (label) label.textContent = on ? "CONNECTED" : "DISCONNECTED";
  setVal("footer-server", on ? SERVER_URL : "—");
}

function setStateBadge(state) {
  const badge = $("state-badge");
  const label = $("state-label");
  if (!badge || !label) return;
  badge.className = "state-badge";
  if (state === "SPEAKING") badge.classList.add("speaking");
  else if (state === "SILENT") badge.classList.add("silent");
  label.textContent = state;
}

function pulseHeartbeat() {
  const icon = $("hb-icon");
  if (!icon) return;
  icon.classList.remove("pulse");
  void icon.offsetWidth;       // force reflow to restart CSS animation
  icon.classList.add("pulse");
}

function resetHeartbeatTimer() {
  clearTimeout(heartbeatTimer);
  heartbeatTimer = setTimeout(() => {
    setVal("hb-ts", "stale");
    setStateBadge("IDLE");
  }, HEARTBEAT_TIMEOUT);
}

// ═══════════════════════════════════════════════════════════════════════════
// CONTROL BUTTONS
// ═══════════════════════════════════════════════════════════════════════════
async function resetStats() {
  try {
    await fetch(`${SERVER_URL}/reset`);
    buf.labels.length = buf.ratio.length = buf.fps.length =
    buf.latency.length = buf.snr.length = 0;
    updateCharts();
    appendLog("system", "↺ Stats reset via /reset");
  } catch (e) {
    appendLog("error", `Reset failed: ${e.message}`);
  }
}

async function clearAudio() {
  try {
    const res = await fetch(`${SERVER_URL}/clear_audio`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({}),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);

    // Instant local reset — don't wait for socket event
    ["m-total","m-speech","m-silence"].forEach(id => setVal(id, "0"));
    setMetricHTML("m-ratio",   `0.0<span class="unit">%</span>`);
    setMetricHTML("m-latency", `0.0<span class="unit">ms</span>`);
    setMetricHTML("m-fps",     `0.0<span class="unit">fps</span>`);
    setMetricHTML("m-snr",     `0.0<span class="unit">dB</span>`);
    setVal("m-starts", "0");
    setVal("m-ends",   "0");
    setStyle("bar-raw",   "width", "0%");
    setStyle("bar-clean", "width", "0%");
    setStyle("bar-delta", "width", "0%");
    setStyle("health-fill", "width", "0%");
    setVal("health-val", "0%");
    appendLog("system", "🧹 Audio cleared");
  } catch (e) {
    appendLog("error", `Clear audio failed: ${e.message}`);
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// MICRO UTILITIES
// ═══════════════════════════════════════════════════════════════════════════
function bufPush(arr, val) {
  arr.push(val);
  if (arr.length > ROLLING_WINDOW) arr.shift();
}

function fmt(n) {
  n = Number(n) || 0;
  return n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n);
}

// Set element textContent safely
function setVal(id, val) {
  const el = $(id);
  if (el) el.textContent = String(val);
}

// Set element innerHTML safely
function setMetricHTML(id, html) {
  const el = $(id);
  if (el) el.innerHTML = html;
}

// Set element textContent via innerHTML alias (for plain text metric cards)
function setMetric(id, text) {
  const el = $(id);
  if (el) el.textContent = text;
}

// Set a style property safely
function setStyle(id, prop, val) {
  const el = $(id);
  if (el) el.style[prop] = val;
}

// Set progress bar + label
function setBar(barId, pct, numId, label) {
  setStyle(barId, "width", `${Math.min(Math.max(pct, 0), 100)}%`);
  setVal(numId, label);
}

// HTML-escape user/server-derived strings — prevents XSS
function escHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// Debug logger — only active when ?debug=1 is in URL
function dbg(msg) {
  if (!DEBUG) return;
  console.log(`[DBG] ${msg}`);
  const line = $("debug-line");
  if (line) line.textContent = msg;
  const bar = $("debug-bar");
  if (bar) bar.classList.add("visible");
}

// ═══════════════════════════════════════════════════════════════════════════
// BOOT — wait for DOM, then init charts and log startup
// ═══════════════════════════════════════════════════════════════════════════
document.addEventListener("DOMContentLoaded", () => {
  initCharts();

  // Show debug bar if ?debug=1
  if (DEBUG) {
    const bar = $("debug-bar");
    if (bar) bar.classList.add("visible");
  }

  appendLog("system", `Connecting to ${SERVER_URL} …`);
  setVal("footer-server", SERVER_URL);
});
