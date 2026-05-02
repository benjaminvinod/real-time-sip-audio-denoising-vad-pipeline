/**
 * app.js — SIP·VAD Monitor
 * Pure WebSocket (Socket.IO) — zero polling.
 * Connects to sip_server.py's Socket.IO on port 5000.
 */

"use strict";

// ── Config ────────────────────────────────────────────────────────────────────
const SERVER_URL = "http://192.168.31.129:5000";
const ROLLING_WINDOW = 60;   // data points kept in charts
const LOG_MAX        = 40;   // max event log entries
const HEARTBEAT_TIMEOUT_MS = 4000;  // ms before marking stale

// ── State ─────────────────────────────────────────────────────────────────────
let logCount        = 0;
let lastUpdateTime  = null;
let heartbeatTimer  = null;

// Rolling buffers for charts
const buf = {
  labels:  [],
  ratio:   [],
  fps:     [],
  latency: [],
  snr:     [],
};

// ── Socket.IO ─────────────────────────────────────────────────────────────────
const socket = io(SERVER_URL, {
  transports: ["polling", "websocket"],
  reconnection: true,
  reconnectionAttempts: Infinity,
  reconnectionDelay: 1000,
  reconnectionDelayMax: 5000,
  timeout: 20000
});

socket.on("connect", () => {
  console.log("✅ CONNECTED", socket.id);
  setConnected(true);
  appendLog("system", `Socket connected — id: ${socket.id}`);
});

socket.on("disconnect", (reason) => {
  console.log("❌ DISCONNECTED", reason);
  setConnected(false);
  appendLog("warn", `Disconnected: ${reason}`);
  setStateBadge("IDLE");
});

socket.on("connect_error", (err) => {
  console.log("❌ CONNECT ERROR", err.message);
  appendLog("error", `Connection error: ${err.message}`);
});

socket.on("processedAudio", (data) => {
  console.log("📡 DATA RECEIVED", data.seq);
  handleFrame(data);
});

// ── Frame handler ─────────────────────────────────────────────────────────────
function handleFrame(d) {
  const now = new Date();
  lastUpdateTime = now;

  // ── Heartbeat pulse ──
  resetHeartbeatTimer();
  pulseHeartbeat();
  document.getElementById("hb-ts").textContent = now.toLocaleTimeString();

  // ── State badge ──
  const isSpeech = d.is_speech;
  setStateBadge(isSpeech ? "SPEAKING" : "SILENT");

  // ── Metric cards ──
  setMetric("m-total",    fmt(d.total_frames ?? 0));
  setMetric("m-speech",   fmt(d.speech_frames ?? 0));
  setMetric("m-silence",  fmt(d.silence_frames ?? 0));
  setMetric("m-ratio",    `${(d.speech_ratio ?? 0).toFixed(1)}<span class="unit">%</span>`);
  setMetric("m-latency",  `${(d.avg_latency ?? 0).toFixed(1)}<span class="unit">ms</span>`);
  setMetric("m-fps",      `${(d.fps ?? 0).toFixed(1)}<span class="unit">fps</span>`);
  setMetric("m-snr",      `${(d.snr_db ?? 0).toFixed(1)}<span class="unit">dB</span>`);
  document.getElementById("m-starts").textContent = d.speech_start ?? 0;
  document.getElementById("m-ends").textContent   = d.speech_end   ?? 0;

  // ── Health bar ──
  const ratio = Math.min(d.speech_ratio ?? 0, 100);
  document.getElementById("health-fill").style.width = `${ratio}%`;
  document.getElementById("health-val").textContent  = `${ratio.toFixed(1)}%`;
  document.getElementById("active-calls-label").textContent =
    `${d.active_calls ?? 0} CALL${(d.active_calls ?? 0) !== 1 ? "S" : ""}`;

  // ── Footer ──
  document.getElementById("footer-seq").textContent   = `SEQ #${d.seq ?? "—"}`;
  document.getElementById("footer-calls").textContent =
    `${d.active_calls ?? 0} active call${(d.active_calls ?? 0) !== 1 ? "s" : ""}`;

  // ── Energy bars ──
  const rawE  = d.raw_energy      ?? 0;
  const clnE  = d.denoised_energy ?? 0;
  const maxE  = Math.max(rawE, clnE, 1);
  setBar("bar-raw",   rawE / maxE * 100, "num-raw",   rawE.toFixed(0));
  setBar("bar-clean", clnE / maxE * 100, "num-clean", clnE.toFixed(0));
  const delta = (d.snr_db ?? 0);
  const deltaFrac = Math.min(Math.abs(delta) / 30, 1) * 100;
  setBar("bar-delta", deltaFrac, "num-delta", `${delta.toFixed(1)} dB`);

  // ── Rolling buffers ──
  const ts = now.toLocaleTimeString("en-US", { hour12: false });
  push(buf.labels,  ts);
  push(buf.ratio,   d.speech_ratio ?? 0);
  push(buf.fps,     d.fps ?? 0);
  push(buf.latency, d.avg_latency  ?? 0);
  push(buf.snr,     d.snr_db       ?? 0);

  // Update live chart tags
  document.getElementById("ratio-live").textContent = `${(d.speech_ratio ?? 0).toFixed(1)}%`;
  document.getElementById("perf-live").textContent  = `${(d.fps ?? 0).toFixed(1)} fps`;
  document.getElementById("snr-live").textContent   = `${(d.snr_db ?? 0).toFixed(1)} dB`;

  updateCharts();

  // ── Event log ──
  if (d.speech_event && d.speech_event !== "") {
    const icon = d.speech_event === "speech_start" ? "🗣" : "🤫";
    appendLog("event",
      `${icon} ${d.speech_event.toUpperCase()} — seq ${d.seq}  |  SNR ${(d.snr_db ?? 0).toFixed(1)} dB`);
  } else {
    // Log every N-th frame to avoid flooding
    if ((d.seq ?? 0) % 10 === 0) {
      const tag = isSpeech ? "speech" : "silence";
      appendLog(tag,
        `[${ts}] seq=${d.seq}  fps=${(d.fps ?? 0).toFixed(1)}  lat=${(d.avg_latency ?? 0).toFixed(1)}ms  snr=${(d.snr_db ?? 0).toFixed(1)}dB`);
    }
  }
}

// ── Charts setup ─────────────────────────────────────────────────────────────
const CHART_DEFAULTS = {
  responsive: true,
  maintainAspectRatio: false,
  animation: { duration: 150 },
  plugins: { legend: { display: false }, tooltip: { enabled: true } },
  scales: {
    x: {
      display: false,
      grid: { display: false },
    },
    y: {
      grid: { color: "rgba(255,255,255,0.05)" },
      ticks: {
        color: "#6b7280",
        font: { family: "'Space Mono', monospace", size: 10 },
      },
      border: { display: false },
    },
  },
};

function makeGradient(ctx, colorTop, colorBot) {
  const g = ctx.createLinearGradient(0, 0, 0, 120);
  g.addColorStop(0, colorTop);
  g.addColorStop(1, colorBot);
  return g;
}

// Speech ratio chart
const ratioCtx = document.getElementById("chart-ratio").getContext("2d");
const ratioGrad = makeGradient(ratioCtx, "rgba(34,211,238,0.55)", "rgba(34,211,238,0.02)");
const ratioChart = new Chart(ratioCtx, {
  type: "line",
  data: {
    labels: [],
    datasets: [{
      data: [],
      borderColor: "#22d3ee",
      backgroundColor: ratioGrad,
      borderWidth: 2,
      pointRadius: 0,
      tension: 0.4,
      fill: true,
    }],
  },
  options: {
    ...CHART_DEFAULTS,
    scales: {
      ...CHART_DEFAULTS.scales,
      y: { ...CHART_DEFAULTS.scales.y, min: 0, max: 100,
           ticks: { ...CHART_DEFAULTS.scales.y.ticks, callback: v => v + "%" } },
    },
  },
});

// FPS + Latency dual-axis chart
const perfCtx = document.getElementById("chart-perf").getContext("2d");
const fpsGrad = makeGradient(perfCtx, "rgba(167,139,250,0.55)", "rgba(167,139,250,0.02)");
const latGrad = makeGradient(perfCtx, "rgba(251,146,60,0.45)",  "rgba(251,146,60,0.02)");
const perfChart = new Chart(perfCtx, {
  type: "line",
  data: {
    labels: [],
    datasets: [
      { data: [], borderColor: "#a78bfa", backgroundColor: fpsGrad,
        borderWidth: 2, pointRadius: 0, tension: 0.4, fill: true, yAxisID: "yFps" },
      { data: [], borderColor: "#fb923c", backgroundColor: latGrad,
        borderWidth: 2, pointRadius: 0, tension: 0.4, fill: true, yAxisID: "yLat", borderDash: [4, 3] },
    ],
  },
  options: {
    ...CHART_DEFAULTS,
    scales: {
      x: CHART_DEFAULTS.scales.x,
      yFps: { position: "left",  grid: { color: "rgba(255,255,255,0.04)" },
              ticks: { color: "#a78bfa", font: { family: "'Space Mono', monospace", size: 9 } }, border: { display: false } },
      yLat: { position: "right", grid: { display: false },
              ticks: { color: "#fb923c", font: { family: "'Space Mono', monospace", size: 9 } }, border: { display: false } },
    },
  },
});

// SNR chart
const snrCtx = document.getElementById("chart-snr").getContext("2d");
const snrGrad = makeGradient(snrCtx, "rgba(52,211,153,0.55)", "rgba(52,211,153,0.02)");
const snrChart = new Chart(snrCtx, {
  type: "line",
  data: {
    labels: [],
    datasets: [{
      data: [],
      borderColor: "#34d399",
      backgroundColor: snrGrad,
      borderWidth: 2,
      pointRadius: 0,
      tension: 0.4,
      fill: true,
    }],
  },
  options: {
    ...CHART_DEFAULTS,
    scales: {
      ...CHART_DEFAULTS.scales,
      y: { ...CHART_DEFAULTS.scales.y,
           ticks: { ...CHART_DEFAULTS.scales.y.ticks, callback: v => v.toFixed(0) + " dB" } },
    },
  },
});

function updateCharts() {
  ratioChart.data.labels              = [...buf.labels];
  ratioChart.data.datasets[0].data   = [...buf.ratio];
  ratioChart.update("none");

  perfChart.data.labels               = [...buf.labels];
  perfChart.data.datasets[0].data     = [...buf.fps];
  perfChart.data.datasets[1].data     = [...buf.latency];
  perfChart.update("none");

  snrChart.data.labels                = [...buf.labels];
  snrChart.data.datasets[0].data      = [...buf.snr];
  snrChart.update("none");
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function push(arr, val) {
  arr.push(val);
  if (arr.length > ROLLING_WINDOW) arr.shift();
}

function fmt(n) {
  return n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n);
}

function setMetric(id, html) {
  const el = document.getElementById(id);
  if (el) el.innerHTML = html;
}

function setBar(barId, pct, numId, label) {
  const bar = document.getElementById(barId);
  const num = document.getElementById(numId);
  if (bar) bar.style.width = `${Math.min(pct, 100)}%`;
  if (num) num.textContent = label;
}

function setConnected(on) {
  const pill  = document.getElementById("conn-pill");
  const dot   = document.getElementById("conn-dot");
  const label = document.getElementById("conn-label");
  pill.classList.toggle("connected", on);
  dot.classList.toggle("on", on);
  label.textContent = on ? "CONNECTED" : "DISCONNECTED";
}

function setStateBadge(state) {
  const badge = document.getElementById("state-badge");
  const label = document.getElementById("state-label");
  badge.className = "state-badge";
  if (state === "SPEAKING") badge.classList.add("state-speaking");
  else if (state === "SILENT") badge.classList.add("state-silent");
  else badge.classList.add("state-idle");
  label.textContent = state;
}

function pulseHeartbeat() {
  const icon = document.getElementById("hb-icon");
  icon.classList.remove("pulse");
  void icon.offsetWidth; // reflow to restart animation
  icon.classList.add("pulse");
}

function resetHeartbeatTimer() {
  clearTimeout(heartbeatTimer);
  heartbeatTimer = setTimeout(() => {
    document.getElementById("hb-ts").textContent = "stale";
    setStateBadge("IDLE");
  }, HEARTBEAT_TIMEOUT_MS);
}

function appendLog(type, msg) {
  const log = document.getElementById("event-log");
  const row = document.createElement("div");
  row.className = `log-row log-${type}`;

  const ts   = new Date().toLocaleTimeString("en-US", { hour12: false });
  row.innerHTML = `<span class="log-ts">${ts}</span><span class="log-msg">${escHtml(msg)}</span>`;

  log.prepend(row);
  logCount++;
  document.getElementById("log-count").textContent = `${logCount} events`;

  // Trim log
  while (log.children.length > LOG_MAX) {
    log.removeChild(log.lastChild);
  }
}

function escHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

// ── Control buttons ───────────────────────────────────────────────────────────
async function resetStats() {
  try {
    await fetch(`${SERVER_URL}/reset`);
    // Clear local buffers
    buf.labels.length = buf.ratio.length = buf.fps.length =
    buf.latency.length = buf.snr.length = 0;
    updateCharts();
    appendLog("system", "Stats reset via /reset");
  } catch (e) {
    appendLog("error", `Reset failed: ${e.message}`);
  }
}

async function clearAudio() {
  try {
    // 🔥 call backend
    const res = await fetch(`${SERVER_URL}/clear_audio`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({})
    });

    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }

    // 🧹 INSTANT UI RESET (do NOT wait for socket event)
    document.getElementById("m-total").textContent = "0";
    document.getElementById("m-speech").textContent = "0";
    document.getElementById("m-silence").textContent = "0";
    document.getElementById("m-ratio").textContent = "0.0%";
    document.getElementById("m-latency").textContent = "0.0 ms";
    document.getElementById("m-fps").textContent = "0.0 fps";
    document.getElementById("m-snr").textContent = "0.0 dB";

    document.getElementById("m-starts").textContent = "0";
    document.getElementById("m-ends").textContent = "0";

    // reset bars
    document.getElementById("bar-raw").style.width = "0%";
    document.getElementById("bar-clean").style.width = "0%";
    document.getElementById("bar-delta").style.width = "0%";

    // reset health bar
    document.getElementById("health-fill").style.width = "0%";
    document.getElementById("health-val").textContent = "0%";

    // reset state
    document.getElementById("state-label").textContent = "IDLE";

    appendLog("system", "🧹 Audio cleared successfully");

  } catch (e) {
    console.error("Clear audio failed:", e);
    appendLog("error", `Clear audio failed: ${e.message}`);
  }
}

// ── Init log ──────────────────────────────────────────────────────────────────
appendLog("system", `Connecting to ${SERVER_URL} …`);
