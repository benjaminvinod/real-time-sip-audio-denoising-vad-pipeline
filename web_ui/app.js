/**
 * app.js — RTP·VAD Pipeline Monitor
 * ===================================
 * Socket events:
 *   "connect"        — socket connected
 *   "disconnect"     — socket disconnected
 *   "connect_error"  — connection failure
 *   "processedAudio" — per-frame metrics
 *   "transcript"     — live STT segment  { call_id, text, timestamp }
 *   "audioCleared"   — backend confirmed clear
 *   "heartbeat"      — server keepalive  { ts }
 *   "call_ended"     — BYE received, LLM queued  { call_id, ended_at, llm_queued }
 *   "llm_report"     — post-call analysis result
 *                       { call_id, report|null, error|null, meta }
 *
 * LLM report shape (when report !== null):
 *   { summary, intent, sentiment, risk_level, suggested_action }
 *
 * Transcript UX — 3-state pending row:
 *   speech_start  → row injected with "🎙️ Listening…"   (pulsing)
 *   speech_end    → same row updated to "🧠 Processing…" (spinner)
 *   transcript    → same row resolved with final text    (fade-in)
 *   If no pending row exists when transcript arrives, falls back to append.
 */

"use strict";

// ── CONFIG ──────────────────────────────────────────────────────────────────
const SERVER_URL        = `http://${location.hostname}:5000`;
const ROLLING_WINDOW    = 60;
const LOG_MAX           = 40;
const TX_MAX            = 50;
const HEARTBEAT_TIMEOUT = 4000;
const DEBUG             = new URLSearchParams(location.search).has("debug");

// ── STATE ───────────────────────────────────────────────────────────────────
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

let ratioChart = null;
let perfChart  = null;
let snrChart   = null;

// ── DOM HELPER ───────────────────────────────────────────────────────────────
function $(id) {
  return document.getElementById(id) || null;
}

// ── SOCKET.IO ────────────────────────────────────────────────────────────────
const socket = io(SERVER_URL, {
  transports:           ["polling", "websocket"],
  reconnection:         true,
  reconnectionAttempts: Infinity,
  reconnectionDelay:    1000,
  reconnectionDelayMax: 5000,
  timeout:              20000,
});

socket.on("connect", () => {
  dbg(`connect — id=${socket.id}`);
  setConnected(true);
  appendLog("system", `Connected — id: ${socket.id}`);
});

socket.on("disconnect", (reason) => {
  dbg(`disconnect — ${reason}`);
  setConnected(false);
  setStateBadge("IDLE");
  appendLog("warn", `Disconnected: ${reason}`);
});

socket.on("connect_error", (err) => {
  dbg(`connect_error — ${err.message}`);
  appendLog("error", `Connection error: ${err.message}`);
});

socket.on("processedAudio", (data) => {
  dbg(`processedAudio seq=${data.seq}`);
  handleFrame(data);
});

socket.on("transcript", (data) => {
  dbg(`transcript call=${data.call_id} text="${data.text}"`);
  appendLog("event", `TRANSCRIPT [${shortCallId(data.call_id)}]: ${data.text}`);
  // Resolve the pending row if one exists, otherwise append fresh
  resolveTranscriptPending(data.call_id, data.text, data.timestamp);
});

socket.on("audioCleared", () => {
  dbg("audioCleared");
  appendLog("system", "Audio cleared by backend");
});

socket.on("heartbeat", (data) => {
  dbg("heartbeat");
  resetHeartbeatTimer();
  pulseHeartbeat();
  const el = $("hb-ts");
  if (el) el.textContent = new Date(data.ts * 1000).toLocaleTimeString();
});

socket.on("call_started", (data) => {
  dbg(`call_started call=${data.call_id}`);
  resetUIForNewCall(data.call_id);
});

socket.on("call_ended", (data) => {
  dbg(`call_ended call=${data.call_id} llm_queued=${data.llm_queued}`);
  appendLog("event", `Call ended [${shortCallId(data.call_id)}] — LLM analysis queued`);
  if (data.llm_queued) {
    handleLLMStart(data.call_id);
  }
});

socket.on("llm_report", (data) => {
  dbg(`llm_report call=${data.call_id} error=${data.error}`);
  if (data.error) {
    handleLLMError(data);
  } else {
    handleLLMReport(data);
  }
});

// ── NEW CALL RESET ────────────────────────────────────────────────────────────
/**
 * resetUIForNewCall(callId)
 * Called when "call_started" fires (fresh INVITE on the backend).
 * Wipes all metric displays, clears chart buffers, resets transcript panel,
 * and hides any leftover LLM report from the previous call.
 */
function resetUIForNewCall(callId) {
  // Metric cards
  ["m-total", "m-speech", "m-silence"].forEach(id => setMetric(id, "0"));
  setMetricHTML("m-ratio",   `0.0<span class="unit">%</span>`);
  setMetricHTML("m-latency", `0.0<span class="unit">ms</span>`);
  setMetricHTML("m-fps",     `0.0<span class="unit">fps</span>`);
  setMetricHTML("m-snr",     `0.0<span class="unit">dB</span>`);
  setVal("m-starts", "0");
  setVal("m-ends",   "0");

  // Strip + energy bars
  setStyle("health-fill", "width", "0%");
  setVal("health-val", "0.0%");
  setStyle("bar-raw",   "width", "0%");
  setStyle("bar-clean", "width", "0%");
  setStyle("bar-delta", "width", "0%");
  setVal("num-raw",   "0");
  setVal("num-clean", "0");
  setVal("num-delta", "0 dB");

  // Rolling chart buffers
  buf.labels.length = buf.ratio.length = buf.fps.length =
  buf.latency.length = buf.snr.length = 0;
  updateCharts();

  // Live chart values
  setVal("ratio-live", "—%");
  setVal("perf-live",  "— fps");
  setVal("snr-live",   "— dB");

  // Transcript panel
  const scroll = $("tx-scroll");
  if (scroll) {
    scroll.innerHTML = `<div class="tx-empty" id="tx-empty">AWAITING SPEECH…</div>`;
  }
  txCount = 0;
  setVal("tx-count", "0 segments");

  // Hide LLM panel from previous call
  const llmPanel = $("llm-panel");
  if (llmPanel) llmPanel.classList.add("hidden");

  // State badge back to idle
  setStateBadge("IDLE");

  appendLog("system", `New call started [${shortCallId(callId)}] — metrics reset`);
}


function handleFrame(d) {
  const now = new Date();

  resetHeartbeatTimer();
  pulseHeartbeat();
  const el = $("hb-ts");
  if (el) el.textContent = now.toLocaleTimeString();

  setStateBadge(d.is_speech ? "SPEAKING" : "SILENT");

  setMetric("m-total",   fmt(d.total_frames   ?? 0));
  setMetric("m-speech",  fmt(d.speech_frames  ?? 0));
  setMetric("m-silence", fmt(d.silence_frames ?? 0));
  setMetricHTML("m-ratio",   `${(d.speech_ratio ?? 0).toFixed(1)}<span class="unit">%</span>`);
  setMetricHTML("m-latency", `${(d.avg_latency  ?? 0).toFixed(1)}<span class="unit">ms</span>`);
  setMetricHTML("m-fps",     `${(d.fps          ?? 0).toFixed(1)}<span class="unit">fps</span>`);
  setMetricHTML("m-snr",     `${(d.snr_db       ?? 0).toFixed(1)}<span class="unit">dB</span>`);
  setVal("m-starts", d.speech_start ?? 0);
  setVal("m-ends",   d.speech_end   ?? 0);

  const ratio = Math.min(d.speech_ratio ?? 0, 100);
  setStyle("health-fill", "width", `${ratio}%`);
  setVal("health-val", `${ratio.toFixed(1)}%`);
  const calls = d.active_calls ?? 0;
  setVal("active-calls-label", `${calls} CALL${calls !== 1 ? "S" : ""}`);

  setVal("footer-seq",   `SEQ #${d.seq ?? "—"}`);
  setVal("footer-calls", `${calls} active call${calls !== 1 ? "s" : ""}`);
  setVal("footer-ts",    now.toLocaleTimeString());

  const rawE = d.raw_energy      ?? 0;
  const clnE = d.denoised_energy ?? 0;
  const maxE = Math.max(rawE, clnE, 1);
  setBar("bar-raw",   rawE / maxE * 100, "num-raw",   rawE.toFixed(0));
  setBar("bar-clean", clnE / maxE * 100, "num-clean", clnE.toFixed(0));
  const snrAbs = Math.abs(d.snr_db ?? 0);
  setBar("bar-delta", Math.min(snrAbs / 30, 1) * 100, "num-delta", `${(d.snr_db ?? 0).toFixed(1)} dB`);

  const ts = now.toLocaleTimeString("en-US", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
  bufPush(buf.labels,  ts);
  bufPush(buf.ratio,   d.speech_ratio ?? 0);
  bufPush(buf.fps,     d.fps          ?? 0);
  bufPush(buf.latency, d.avg_latency  ?? 0);
  bufPush(buf.snr,     d.snr_db       ?? 0);

  setVal("ratio-live", `${(d.speech_ratio ?? 0).toFixed(1)}%`);
  setVal("perf-live",  `${(d.fps ?? 0).toFixed(1)} fps`);
  setVal("snr-live",   `${(d.snr_db ?? 0).toFixed(1)} dB`);

  updateCharts();

  if (d.speech_event && d.speech_event !== "") {
    const tag = d.speech_event === "speech_start" ? "SPEECH_START" : "SPEECH_END";
    appendLog("event", `${tag}  seq=${d.seq}  SNR=${(d.snr_db ?? 0).toFixed(1)}dB`);

    // ── 3-state transcript UX ────────────────────────────────────────────────
    if (d.speech_event === "speech_start") {
      showTranscriptPending(d.call_id);       // 🎙️ Listening…
    } else if (d.speech_event === "speech_end") {
      updateTranscriptPending(d.call_id);     // 🧠 Processing…
    }
    // ────────────────────────────────────────────────────────────────────────

  } else if ((d.seq ?? 0) % 10 === 0) {
    const tag = d.is_speech ? "speech" : "silence";
    appendLog(tag, `seq=${d.seq}  fps=${(d.fps ?? 0).toFixed(1)}  lat=${(d.avg_latency ?? 0).toFixed(1)}ms  snr=${(d.snr_db ?? 0).toFixed(1)}dB`);
  }
}

// ── LLM PANEL HANDLERS ───────────────────────────────────────────────────────

/**
 * handleLLMStart(callId)
 * Shows LLM panel in PROCESSING state with spinner.
 */
function handleLLMStart(callId) {
  const panel = $("llm-panel");
  if (panel) panel.classList.remove("hidden");

  const spinner = $("llm-spinner");
  if (spinner) spinner.classList.remove("hidden");

  _setLLMStatus("PROCESSING", "processing");
  setVal("llm-call-id", shortCallId(callId));

  const reportEl = $("llm-report-content");
  if (reportEl) { reportEl.classList.add("hidden"); reportEl.innerHTML = ""; }

  const errEl = $("llm-error");
  if (errEl) { errEl.classList.add("hidden"); errEl.textContent = ""; }

  setVal("llm-meta", "");
  appendLog("system", `LLM analysis started for [${shortCallId(callId)}]`);
}

/**
 * handleLLMReport(data)
 * Hides spinner, renders structured report.
 *
 * data shape: { call_id, report: { summary, intent, sentiment, risk_level, suggested_action }, meta }
 */
function handleLLMReport(data) {
  const { call_id, report, meta } = data;

  const panel = $("llm-panel");
  if (panel) panel.classList.remove("hidden");

  const spinner = $("llm-spinner");
  if (spinner) spinner.classList.add("hidden");

  _setLLMStatus("COMPLETE", "complete");
  setVal("llm-call-id", shortCallId(call_id));

  const reportEl = $("llm-report-content");
  if (reportEl) {
    reportEl.classList.remove("hidden");

    const sentimentClass = _sentimentClass(report?.sentiment);
    const riskClass      = _riskClass(report?.risk_level);

    reportEl.innerHTML = `
      <div class="llm-field">
        <div class="llm-field-label">Summary</div>
        <div class="llm-field-value summary-text">${escHtml(report?.summary ?? "—")}</div>
      </div>

      <div class="llm-row-2">
        <div class="llm-field">
          <div class="llm-field-label">Intent</div>
          <div class="llm-field-value">${escHtml(report?.intent ?? "—")}</div>
        </div>
        <div class="llm-field">
          <div class="llm-field-label">Sentiment</div>
          <span class="llm-badge ${sentimentClass}">${escHtml((report?.sentiment ?? "unknown").toUpperCase())}</span>
        </div>
        <div class="llm-field">
          <div class="llm-field-label">Risk Level</div>
          <span class="llm-badge ${riskClass}">${escHtml((report?.risk_level ?? "unknown").toUpperCase())}</span>
        </div>
      </div>

      <div class="llm-field">
        <div class="llm-field-label">Suggested Action</div>
        <div class="llm-field-value action-text">${escHtml(report?.suggested_action ?? "—")}</div>
      </div>
    `;
  }

  const errEl = $("llm-error");
  if (errEl) { errEl.classList.add("hidden"); errEl.textContent = ""; }

  if (meta) {
    const segs  = meta.segments ?? "?";
    const ms    = meta.processing_ms != null ? `${meta.processing_ms.toFixed(0)} ms` : "?";
    const chars = meta.length ?? "?";
    setVal("llm-meta", `${chars} chars · ${segs} segments · ${ms}`);
  }

  appendLog("event", `LLM report ready [${shortCallId(call_id)}] — sentiment: ${report?.sentiment}, risk: ${report?.risk_level}`);
}

/**
 * handleLLMError(data)
 * Shows the error message in the panel.
 *
 * data shape: { call_id, report: null, error: string, meta }
 */
function handleLLMError(data) {
  const { call_id, error } = data;

  const panel = $("llm-panel");
  if (panel) panel.classList.remove("hidden");

  const spinner = $("llm-spinner");
  if (spinner) spinner.classList.add("hidden");

  _setLLMStatus("ERROR", "error");
  setVal("llm-call-id", shortCallId(call_id));

  const reportEl = $("llm-report-content");
  if (reportEl) { reportEl.classList.add("hidden"); reportEl.innerHTML = ""; }

  const errEl = $("llm-error");
  if (errEl) {
    errEl.textContent = error ?? "Unknown LLM error.";
    errEl.classList.remove("hidden");
  }

  setVal("llm-meta", "Analysis failed");
  appendLog("error", `LLM error [${shortCallId(call_id)}]: ${error}`);
}

function _setLLMStatus(text, modifier) {
  const el = $("llm-status");
  if (!el) return;
  el.textContent = text;
  el.className = `llm-status ${modifier}`;
}

function _sentimentClass(sentiment) {
  switch ((sentiment ?? "").toLowerCase()) {
    case "positive": return "sentiment-positive";
    case "negative": return "sentiment-negative";
    case "mixed":    return "sentiment-mixed";
    default:         return "sentiment-neutral";
  }
}

function _riskClass(risk) {
  switch ((risk ?? "").toLowerCase()) {
    case "high":   return "risk-high";
    case "medium": return "risk-medium";
    default:       return "risk-low";
  }
}

// ── TRANSCRIPT — 3-STATE PENDING ROWS ────────────────────────────────────────
//
// Each active call gets at most one pending row at a time.
// The row is identified via data-pending-call="<callId>" so all three
// state functions can find and mutate the same DOM node.
//
// State machine per call:
//   (none)  ──speech_start──▶  LISTENING  ──speech_end──▶  PROCESSING  ──transcript──▶  RESOLVED
//
// Edge cases handled:
//   • speech_start fires again before transcript arrives  → replaces the old pending row
//   • transcript arrives with no pending row             → falls back to a normal append
//   • STT is skipped / segment too short                 → pending row is cleaned up on next speech_start

/**
 * showTranscriptPending(callId)
 * Called on speech_start.
 * Inserts (or replaces) a pulsing "🎙️ Listening…" placeholder row.
 */
function showTranscriptPending(callId) {
  const scroll = $("tx-scroll");
  if (!scroll) return;

  // Remove any stale pending row for this call (e.g. STT was skipped last time)
  _removePendingRow(callId);

  const empty = $("tx-empty");
  if (empty) empty.remove();

  const rows = scroll.querySelectorAll(".tx-row");
  if (rows.length >= TX_MAX) rows[0].remove();

  const ts      = new Date().toLocaleTimeString("en-US", { hour12: false });
  const shortId = shortCallId(callId ?? "—");

  const row = document.createElement("div");
  row.className = "tx-row tx-pending tx-state-listening new-entry";
  row.dataset.pendingCall = callId ?? "";

  row.innerHTML = `
    <div class="tx-meta">
      <span class="tx-ts">${escHtml(ts)}</span>
      <span class="tx-call">${escHtml(shortId)}</span>
      <span class="tx-state-badge listening">🎙️ Listening…</span>
    </div>
    <span class="tx-text tx-placeholder">—</span>`;

  scroll.appendChild(row);
  row.addEventListener("animationend", () => row.classList.remove("new-entry"), { once: true });
  scroll.scrollTop = scroll.scrollHeight;

  // Count pending rows in the segment counter too
  txCount++;
  setVal("tx-count", `${txCount} segment${txCount !== 1 ? "s" : ""}`);
}

/**
 * updateTranscriptPending(callId)
 * Called on speech_end.
 * Swaps the badge on the existing pending row to "🧠 Processing…".
 * If no pending row exists (edge case), does nothing — transcript will append fresh.
 */
function updateTranscriptPending(callId) {
  const row = _findPendingRow(callId);
  if (!row) return;

  row.classList.remove("tx-state-listening");
  row.classList.add("tx-state-processing");

  const badge = row.querySelector(".tx-state-badge");
  if (badge) {
    badge.className = "tx-state-badge processing";
    badge.textContent = "🧠 Processing…";
  }
}

/**
 * resolveTranscriptPending(callId, text, timestamp)
 * Called when the "transcript" socket event arrives.
 * Finds the pending row and replaces its content with the final transcript text.
 * Falls back to a normal append if no pending row exists.
 */
function resolveTranscriptPending(callId, text, timestamp) {
  const row = _findPendingRow(callId);

  if (row) {
    // Resolve in-place — keeps the row's position in the list
    row.classList.remove("tx-pending", "tx-state-listening", "tx-state-processing");
    row.classList.add("tx-resolved", "new-entry");
    delete row.dataset.pendingCall;

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

    row.addEventListener("animationend", () => row.classList.remove("new-entry"), { once: true });

    const scroll = $("tx-scroll");
    if (scroll) scroll.scrollTop = scroll.scrollHeight;

    // txCount was already incremented in showTranscriptPending, don't double-count
    setVal("tx-count", `${txCount} segment${txCount !== 1 ? "s" : ""}`);

  } else {
    // Fallback: no pending row — append a fresh resolved row as before
    appendTranscript(callId, text, timestamp);
  }
}

/** Find the live pending row for a given callId. */
function _findPendingRow(callId) {
  const scroll = $("tx-scroll");
  if (!scroll) return null;
  return scroll.querySelector(`.tx-pending[data-pending-call="${CSS.escape(callId ?? "")}"]`) || null;
}

/** Remove any existing pending row for a callId (used when speech_start fires twice). */
function _removePendingRow(callId) {
  const row = _findPendingRow(callId);
  if (row) {
    row.remove();
    // Decrement the count we added in showTranscriptPending
    txCount = Math.max(0, txCount - 1);
  }
}

// ── TRANSCRIPT — CLASSIC APPEND (fallback / direct use) ──────────────────────
function appendTranscript(callId, text, timestamp) {
  const scroll = $("tx-scroll");
  if (!scroll) return;

  const empty = $("tx-empty");
  if (empty) empty.remove();

  const rows = scroll.querySelectorAll(".tx-row");
  if (rows.length >= TX_MAX) rows[0].remove();

  const row = document.createElement("div");
  row.className = "tx-row new-entry";

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
  row.addEventListener("animationend", () => row.classList.remove("new-entry"), { once: true });
  scroll.scrollTop = scroll.scrollHeight;

  txCount++;
  setVal("tx-count", `${txCount} segment${txCount !== 1 ? "s" : ""}`);
}

function shortCallId(id) {
  if (!id || id === "—") return "—";
  const s = String(id);
  return s.length > 8 ? "…" + s.slice(-8) : s;
}

// ── EVENT LOG ─────────────────────────────────────────────────────────────────
function appendLog(type, msg) {
  const log = $("event-log");
  if (!log) return;

  const row = document.createElement("div");
  row.className = `log-row log-${type}`;
  const ts = new Date().toLocaleTimeString("en-US", { hour12: false });
  row.innerHTML = `<span class="log-ts">${ts}</span><span class="log-msg">${escHtml(String(msg))}</span>`;

  log.prepend(row);
  logCount++;
  setVal("log-count", `${logCount} events`);

  while (log.children.length > LOG_MAX) {
    log.removeChild(log.lastChild);
  }
}

// ── CHARTS ───────────────────────────────────────────────────────────────────
function initCharts() {
  const FONT  = { family: "'IBM Plex Mono', monospace", size: 9 };
  const GRID  = "rgba(255,255,255,0.04)";
  const TICKC = "#2E3E4A";

  const BASE = {
    responsive:          true,
    maintainAspectRatio: false,
    animation:           { duration: 0 },
    plugins: {
      legend:  { display: false },
      tooltip: {
        enabled: true, mode: "index", intersect: false,
        titleFont: FONT, bodyFont: FONT,
        backgroundColor: "#0f1318",
        borderColor: "rgba(255,255,255,0.09)",
        borderWidth: 1,
      },
    },
    scales: {
      x: { display: false, grid: { display: false } },
      y: {
        grid: { color: GRID },
        border: { display: false },
        ticks: { color: TICKC, font: FONT },
      },
    },
  };

  function grad(ctx, top, bot) {
    const g = ctx.createLinearGradient(0, 0, 0, 80);
    g.addColorStop(0, top);
    g.addColorStop(1, bot);
    return g;
  }

  // Speech Ratio chart
  const rCtx = $("chart-ratio")?.getContext("2d");
  if (rCtx) {
    ratioChart = new Chart(rCtx, {
      type: "line",
      data: {
        labels: [],
        datasets: [{
          data: [],
          borderColor: "#3B9EFF",
          borderWidth: 1.5,
          backgroundColor: grad(rCtx, "rgba(59,158,255,0.18)", "rgba(59,158,255,0.01)"),
          pointRadius: 0,
          tension: 0.3,
          fill: true,
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

  // FPS + Latency dual-axis chart
  const pCtx = $("chart-perf")?.getContext("2d");
  if (pCtx) {
    perfChart = new Chart(pCtx, {
      type: "line",
      data: {
        labels: [],
        datasets: [
          {
            data: [],
            borderColor: "#34C98B",
            borderWidth: 1.5,
            backgroundColor: grad(pCtx, "rgba(52,201,139,0.15)", "rgba(52,201,139,0.01)"),
            pointRadius: 0, tension: 0.3, fill: true, yAxisID: "yFps",
          },
          {
            data: [],
            borderColor: "#E8A838",
            borderWidth: 1.5,
            backgroundColor: grad(pCtx, "rgba(232,168,56,0.10)", "rgba(232,168,56,0.01)"),
            pointRadius: 0, tension: 0.3, fill: true, yAxisID: "yLat",
            borderDash: [4, 3],
          },
        ],
      },
      options: {
        ...BASE,
        scales: {
          x: BASE.scales.x,
          yFps: { position: "left",  grid: { color: GRID }, border: { display: false },
                  ticks: { color: "#34C98B", font: FONT } },
          yLat: { position: "right", grid: { display: false }, border: { display: false },
                  ticks: { color: "#E8A838", font: FONT } },
        },
      },
    });
  }

  // SNR chart
  const sCtx = $("chart-snr")?.getContext("2d");
  if (sCtx) {
    snrChart = new Chart(sCtx, {
      type: "line",
      data: {
        labels: [],
        datasets: [{
          data: [],
          borderColor: "#2EC4B6",
          borderWidth: 1.5,
          backgroundColor: grad(sCtx, "rgba(46,196,182,0.15)", "rgba(46,196,182,0.01)"),
          pointRadius: 0, tension: 0.3, fill: true,
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

// ── UI STATE HELPERS ─────────────────────────────────────────────────────────
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
  if (state === "SPEAKING")      badge.classList.add("speaking");
  else if (state === "SILENT")   badge.classList.add("silent");
  label.textContent = state;
}

function pulseHeartbeat() {
  const icon = $("hb-icon");
  if (!icon) return;
  icon.classList.remove("pulse");
  void icon.offsetWidth;
  icon.classList.add("pulse");
}

function resetHeartbeatTimer() {
  clearTimeout(heartbeatTimer);
  heartbeatTimer = setTimeout(() => {
    setVal("hb-ts", "stale");
    setStateBadge("IDLE");
  }, HEARTBEAT_TIMEOUT);
}

// ── CONTROL BUTTONS ──────────────────────────────────────────────────────────
async function resetStats() {
  try {
    await fetch(`${SERVER_URL}/reset`);
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
    const res = await fetch(`${SERVER_URL}/clear_audio`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({}),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);

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
    setVal("health-val", "0.0%");
    appendLog("system", "Audio cleared");
  } catch (e) {
    appendLog("error", `Clear audio failed: ${e.message}`);
  }
}

// ── MICRO UTILITIES ──────────────────────────────────────────────────────────
function bufPush(arr, val) {
  arr.push(val);
  if (arr.length > ROLLING_WINDOW) arr.shift();
}

function fmt(n) {
  n = Number(n) || 0;
  return n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n);
}

function setVal(id, val) {
  const el = $(id);
  if (el) el.textContent = String(val);
}

function setMetricHTML(id, html) {
  const el = $(id);
  if (el) el.innerHTML = html;
}

function setMetric(id, text) {
  const el = $(id);
  if (el) el.textContent = text;
}

function setStyle(id, prop, val) {
  const el = $(id);
  if (el) el.style[prop] = val;
}

function setBar(barId, pct, numId, label) {
  setStyle(barId, "width", `${Math.min(Math.max(pct, 0), 100)}%`);
  setVal(numId, label);
}

function escHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function dbg(msg) {
  if (!DEBUG) return;
  console.log(`[DBG] ${msg}`);
  const line = $("debug-line");
  if (line) line.textContent = msg;
  const bar = $("debug-bar");
  if (bar) bar.classList.add("visible");
}

// ── BOOT ─────────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  initCharts();

  if (DEBUG) {
    const bar = $("debug-bar");
    if (bar) { bar.classList.remove("hidden"); bar.classList.add("visible"); }
  }

  const llmPanel = $("llm-panel");
  if (llmPanel) llmPanel.classList.add("hidden");

  appendLog("system", `Connecting to ${SERVER_URL} …`);
  setVal("footer-server", SERVER_URL);
});