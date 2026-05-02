"""
streamlit_app.py — Real-Time SIP Audio Processing Monitor
Benjamin Vinod | Module 1 Frontend  [v2 — Plotly upgrade]

UI improvements over v1:
  1. Plotly charts (speech ratio + dual performance panel) replace st.line_chart
  2. Coloured status-badge row (speech / silent / offline)
  3. Six-column metric cards (unchanged layout, cleaner sub-labels)
  4. st.metric delta for speech ratio trend
  5. System-health progress bar (FPS-normalised)
  6. Frame log with emoji icons, last 20 entries
  7. Extended CSS — cards, scrollbar, Plotly embedding
  8. Live "last update" timestamp per refresh cycle
  9. FPS + TRT performance metrics with thresholded colour
 10. Organised into Title → Status → Metrics → Charts → Log → Footer

BACKEND LOGIC IS UNTOUCHED.
"""

import time
import requests
import streamlit as st
from collections import deque
from datetime import datetime

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────
BACKEND_URL   = "http://localhost:5000"
POLL_INTERVAL = 0.4   # seconds between polls
MAX_LOG_LINES = 40
MAX_CHART_PTS = 60    # rolling window length

# FPS ceiling for health bar normalisation (adjust to your stream rate)
FPS_MAX = 50.0

# ─────────────────────────────────────────────────────────────────────────────
# PAGE CONFIG — must be first Streamlit call
# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="SIP Audio Monitor",
    page_icon="🎙",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ─────────────────────────────────────────────────────────────────────────────
# GLOBAL CSS
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;600&family=IBM+Plex+Sans:wght@300;400;600&display=swap');

/* ── root variables ── */
:root {
    --bg:           #0a0b0d;
    --surface:      #111318;
    --surface2:     #181b22;
    --border:       #262b35;
    --amber:        #f0a500;
    --amber-dim:    #7a5200;
    --green:        #00c98d;
    --green-dim:    #004d36;
    --red:          #ff4455;
    --red-dim:      #5a0010;
    --blue:         #4c9fff;
    --muted:        #4a5060;
    --text:         #d8dde8;
    --text-dim:     #6b7280;
    --mono:         'IBM Plex Mono', monospace;
    --sans:         'IBM Plex Sans', sans-serif;
}

/* ── full-page dark background ── */
html, body, [data-testid="stAppViewContainer"], [data-testid="stApp"] {
    background-color: var(--bg) !important;
    color: var(--text) !important;
    font-family: var(--sans) !important;
}
[data-testid="stHeader"]  { background: transparent !important; }
[data-testid="stToolbar"] { display: none !important; }
.block-container { padding: 1.5rem 2rem 2rem 2rem !important; max-width: 100% !important; }

/* ── hide default streamlit chrome ── */
#MainMenu, footer, header { visibility: hidden; }

/* ── scrollbar styling ── */
::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: var(--surface); }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: var(--muted); }

/* ── Plotly chart container — remove white backgrounds ── */
.js-plotly-plot .plotly, .js-plotly-plot .plotly .main-svg {
    background: transparent !important;
}
[data-testid="stPlotlyChart"] > div {
    border: 1px solid var(--border);
    border-radius: 6px;
    overflow: hidden;
}

/* ── metric cards ── */
.metric-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 1.25rem 1.5rem 1rem 1.5rem;
    font-family: var(--mono);
    position: relative;
    overflow: hidden;
    height: 100%;
}
.metric-card::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 2px;
}
.metric-card.amber::before  { background: var(--amber); }
.metric-card.green::before  { background: var(--green); }
.metric-card.red::before    { background: var(--red);   }
.metric-card.blue::before   { background: var(--blue);  }
.metric-card.muted::before  { background: var(--border);}

.metric-label {
    font-size: 0.63rem;
    letter-spacing: 0.16em;
    text-transform: uppercase;
    color: var(--text-dim);
    margin-bottom: 0.4rem;
}
.metric-value {
    font-size: 2.1rem;
    font-weight: 600;
    line-height: 1;
}
.metric-value.amber { color: var(--amber); }
.metric-value.green { color: var(--green); }
.metric-value.red   { color: var(--red);   }
.metric-value.blue  { color: var(--blue);  }
.metric-value.white { color: var(--text);  }
.metric-unit {
    font-size: 0.9rem;
    font-weight: 300;
}
.metric-sub {
    font-size: 0.67rem;
    color: var(--text-dim);
    margin-top: 0.45rem;
    letter-spacing: 0.04em;
}
.metric-delta {
    font-size: 0.68rem;
    margin-top: 0.3rem;
    font-family: var(--mono);
}
.metric-delta.up   { color: var(--green); }
.metric-delta.down { color: var(--red);   }
.metric-delta.flat { color: var(--muted); }

/* ── ratio mini-bar ── */
.ratio-bar-wrap {
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 4px;
    height: 6px;
    overflow: hidden;
    margin-top: 0.55rem;
}
.ratio-bar-fill {
    height: 100%;
    border-radius: 4px;
    transition: width 0.4s ease;
}

/* ── status badge ── */
.status-pill {
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.28rem 0.85rem;
    border-radius: 999px;
    font-family: var(--mono);
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.09em;
    text-transform: uppercase;
}
.status-pill.speech {
    background: var(--green-dim);
    color: var(--green);
    border: 1px solid var(--green);
    box-shadow: 0 0 14px rgba(0,201,141,0.22);
}
.status-pill.silence {
    background: var(--surface2);
    color: var(--muted);
    border: 1px solid var(--border);
}
.status-pill.offline {
    background: var(--red-dim);
    color: var(--red);
    border: 1px solid var(--red);
}
.status-dot {
    width: 7px; height: 7px;
    border-radius: 50%;
    display: inline-block;
    flex-shrink: 0;
}
.status-dot.speech  { background: var(--green); box-shadow: 0 0 6px var(--green); }
.status-dot.silence { background: var(--muted); }
.status-dot.offline { background: var(--red);   }
.status-dot.pulse   { animation: pulse-dot 1.2s ease-in-out infinite; }
@keyframes pulse-dot {
    0%, 100% { opacity: 1; transform: scale(1); }
    50%       { opacity: 0.35; transform: scale(0.7); }
}

/* ── health progress bar ── */
.health-wrap {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 0.9rem 1.25rem;
    font-family: var(--mono);
}
.health-label {
    font-size: 0.63rem;
    letter-spacing: 0.15em;
    text-transform: uppercase;
    color: var(--text-dim);
    margin-bottom: 0.55rem;
    display: flex;
    justify-content: space-between;
}
.health-bar-track {
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 4px;
    height: 10px;
    overflow: hidden;
}
.health-bar-fill {
    height: 100%;
    border-radius: 4px;
    transition: width 0.4s ease;
}

/* ── conn indicator row ── */
.conn-row {
    display: inline-flex;
    align-items: center;
    gap: 0.45rem;
    font-family: var(--mono);
    font-size: 0.68rem;
    color: var(--text-dim);
}

/* ── log panel ── */
.log-panel {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 0.9rem 1.1rem;
    font-family: var(--mono);
    font-size: 0.7rem;
    line-height: 1.75;
    height: 320px;
    overflow-y: auto;
    color: var(--text-dim);
}
.log-panel .log-speech  { color: var(--green); }
.log-panel .log-silence { color: var(--muted); }
.log-panel .log-ts      { color: #2a3040;   margin-right: 0.5em; }
.log-panel .log-seq     { color: var(--amber-dim); margin-right: 0.5em; }
.log-panel .log-icon    { margin-right: 0.3em; }

/* ── section headers ── */
.section-header {
    font-family: var(--mono);
    font-size: 0.62rem;
    letter-spacing: 0.2em;
    text-transform: uppercase;
    color: var(--muted);
    border-bottom: 1px solid var(--border);
    padding-bottom: 0.35rem;
    margin-bottom: 0.9rem;
}

/* ── top bar ── */
.top-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-bottom: 1px solid var(--border);
    padding-bottom: 0.9rem;
    margin-bottom: 1.4rem;
}
.top-bar-title {
    font-family: var(--mono);
    font-size: 0.88rem;
    font-weight: 600;
    color: var(--text);
    letter-spacing: 0.05em;
}
.top-bar-sub {
    font-family: var(--mono);
    font-size: 0.63rem;
    color: var(--text-dim);
    letter-spacing: 0.1em;
    text-transform: uppercase;
    margin-top: 0.2rem;
}
.top-bar-time {
    font-family: var(--mono);
    font-size: 0.72rem;
    color: var(--text-dim);
    text-align: right;
}
.live-dot {
    display: inline-block;
    width: 8px; height: 8px;
    background: var(--green);
    border-radius: 50%;
    margin-right: 0.4rem;
    box-shadow: 0 0 8px var(--green);
    animation: pulse-dot 1.5s ease-in-out infinite;
}
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# SESSION STATE INIT
# ─────────────────────────────────────────────────────────────────────────────
defaults = {
    "frame_count":      0,
    "speech_count":     0,
    "silence_count":    0,
    "speech_ratio":     0.0,
    "prev_speech_ratio": 0.0,   # for delta calculation
    "last_seq":         -1,
    "last_updated":     0.0,
    "is_speech":        False,
    "backend_online":   False,
    "rtp_active":       False,
    "logs":             deque(maxlen=MAX_LOG_LINES),
    "ratio_history":    deque(maxlen=MAX_CHART_PTS),
    "fps_history":      deque(maxlen=MAX_CHART_PTS),
    "trt_history":      deque(maxlen=MAX_CHART_PTS),
    "session_start":    time.time(),
    "last_state":       "silence",
    "avg_trt":          0.0,
    "fps":              0.0,
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v


# ─────────────────────────────────────────────────────────────────────────────
# POLLING FUNCTION  (backend logic unchanged)
# ─────────────────────────────────────────────────────────────────────────────
def poll_backend() -> bool:
    try:
        r = requests.get(f"{BACKEND_URL}/latest", timeout=0.8)
        if r.status_code != 200:
            st.session_state.backend_online = False
            return False

        data = r.json()

        last_updated = data.get("last_updated", 0.0)
        st.session_state.last_updated    = last_updated
        st.session_state.backend_online  = (
            last_updated > 0 and (time.time() - last_updated) < 2.0
        )
        st.session_state.rtp_active = data.get("rtp_active", False)

        new_seq = data.get("seq", 0)
        if new_seq == st.session_state.last_seq:
            return False

        # save previous ratio for delta
        st.session_state.prev_speech_ratio = st.session_state.speech_ratio

        st.session_state.last_seq      = new_seq
        st.session_state.frame_count   = data["frame_count"]
        st.session_state.speech_count  = data["speech_count"]
        st.session_state.silence_count = data["silence_count"]
        st.session_state.speech_ratio  = data["speech_ratio"]
        st.session_state.is_speech     = data["is_speech"]
        st.session_state.last_state    = data.get("last_state", "silence")
        st.session_state.avg_trt       = data.get("avg_trt", 0.0)
        st.session_state.fps           = data.get("fps", 0.0)

        st.session_state.ratio_history.append(data["speech_ratio"])
        st.session_state.fps_history.append(st.session_state.fps)
        st.session_state.trt_history.append(st.session_state.avg_trt)

        ts    = datetime.now().strftime("%H:%M:%S.%f")[:-4]
        st.session_state.logs.appendleft(
            {"ts": ts, "seq": new_seq, "speech": data["is_speech"]}
        )
        return True

    except requests.exceptions.ConnectionError:
        st.session_state.backend_online = False
        return False
    except Exception:
        st.session_state.backend_online = False
        return False


# ─────────────────────────────────────────────────────────────────────────────
# RESET HANDLER  (backend logic unchanged)
# ─────────────────────────────────────────────────────────────────────────────
def reset_session():
    try:
        requests.get(f"{BACKEND_URL}/reset", timeout=1)
    except Exception:
        pass
    for k, v in defaults.items():
        if k not in ("session_start",):
            st.session_state[k] = v if not isinstance(v, deque) else type(v)(v.maxlen)
    st.session_state.session_start = time.time()
    st.session_state.last_state    = "silence"
    st.session_state.avg_trt       = 0.0
    st.session_state.fps           = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# PLOTLY HELPERS
# ─────────────────────────────────────────────────────────────────────────────
import plotly.graph_objects as go

_PLOTLY_LAYOUT = dict(
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="#111318",
    font=dict(family="IBM Plex Mono, monospace", color="#6b7280", size=11),
    margin=dict(l=48, r=16, t=24, b=36),
    xaxis=dict(
        showgrid=False,
        zeroline=False,
        tickfont=dict(size=10),
        color="#4a5060",
    ),
    yaxis=dict(
        gridcolor="#1e222c",
        zeroline=False,
        tickfont=dict(size=10),
        color="#4a5060",
    ),
    hovermode="x unified",
    hoverlabel=dict(
        bgcolor="#181b22",
        bordercolor="#262b35",
        font=dict(family="IBM Plex Mono, monospace", size=11, color="#d8dde8"),
    ),
)


def _smooth(series: list, window: int = 3) -> list:
    """Simple moving-average smoothing."""
    out = []
    for i, v in enumerate(series):
        lo = max(0, i - window + 1)
        out.append(sum(series[lo:i+1]) / (i - lo + 1))
    return out


def _layout(**overrides) -> dict:
    """
    Return a merged layout dict: base _PLOTLY_LAYOUT with per-chart overrides.
    Handles the case where both the base and the caller supply 'yaxis' by
    merging them at the dict level instead of passing two keyword arguments.
    """
    merged = dict(_PLOTLY_LAYOUT)   # shallow copy of base
    for k, v in overrides.items():
        if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
            merged[k] = {**merged[k], **v}  # merge nested dicts (e.g. yaxis)
        else:
            merged[k] = v           # override scalar / new keys
    return merged


def make_ratio_chart(history: list) -> go.Figure:
    """Speech-ratio rolling chart with filled area."""
    xs = list(range(len(history)))
    ys = _smooth(history, window=4)

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=xs, y=ys,
        mode="lines",
        fill="tozeroy",
        fillcolor="rgba(0,201,141,0.08)",
        line=dict(color="#00c98d", width=2.5, shape="spline", smoothing=1.0),
        name="Speech Ratio",
        hovertemplate="%{y:.1f}%<extra></extra>",
    ))
    fig.add_hline(
        y=50,
        line=dict(color="#262b35", width=1, dash="dot"),
        annotation_text="50 %",
        annotation_font=dict(size=9, color="#4a5060"),
        annotation_position="right",
    )
    fig.update_layout(**_layout(
        yaxis=dict(range=[0, 105], ticksuffix="%"),
        showlegend=False,
        height=240,
    ))
    return fig


def make_perf_chart(fps_hist: list, trt_hist: list) -> go.Figure:
    """Dual-axis: FPS (left) + TRT latency ms (right)."""
    n  = max(len(fps_hist), len(trt_hist))
    xs = list(range(n))

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=xs, y=list(fps_hist),
        mode="lines",
        line=dict(color="#4c9fff", width=2.2, shape="spline", smoothing=0.9),
        name="FPS",
        yaxis="y",
        hovertemplate="%{y:.1f} fps<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=xs, y=list(trt_hist),
        mode="lines",
        line=dict(color="#f0a500", width=2.2, shape="spline", smoothing=0.9, dash="dot"),
        name="TRT (ms)",
        yaxis="y2",
        hovertemplate="%{y:.1f} ms<extra></extra>",
    ))
    fig.update_layout(**_layout(
        yaxis=dict(title="FPS", tickfont=dict(color="#4c9fff", size=10)),
        yaxis2=dict(
            overlaying="y", side="right",
            title="Latency (ms)",
            gridcolor="#1e222c",
            zeroline=False,
            tickfont=dict(color="#f0a500", size=10),
            color="#f0a500",
        ),
        legend=dict(
            orientation="h", x=0, y=1.08,
            font=dict(size=10, color="#6b7280"),
            bgcolor="rgba(0,0,0,0)",
        ),
        height=240,
    ))
    return fig


# ─────────────────────────────────────────────────────────────────────────────
# POLL BACKEND
# ─────────────────────────────────────────────────────────────────────────────
poll_backend()

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — TITLE / HEADER
# ─────────────────────────────────────────────────────────────────────────────
now_str    = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
uptime_s   = int(time.time() - st.session_state.session_start)
uptime_str = f"{uptime_s // 3600:02d}:{(uptime_s % 3600) // 60:02d}:{uptime_s % 60:02d}"

st.markdown(f"""
<div class="top-bar">
    <div>
        <div class="top-bar-title">
            <span class="live-dot"></span>SIP AUDIO PROCESSING MONITOR
        </div>
        <div class="top-bar-sub">RNNoise + VAD Pipeline  ·  G.711 μ-law / 8 kHz → 16 kHz</div>
    </div>
    <div class="top-bar-time">
        {now_str}<br>
        <span style="color:var(--muted)">UP</span> {uptime_str}
    </div>
</div>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — STATUS ROW
# ─────────────────────────────────────────────────────────────────────────────
online    = st.session_state.backend_online
rtp_up    = st.session_state.rtp_active
last_state = st.session_state.last_state

if not online:
    vad_cls, vad_dot, vad_txt = "offline", "offline", "BACKEND OFFLINE"
elif not rtp_up:
    vad_cls, vad_dot, vad_txt = "silence", "silence", "WAITING FOR RTP"
elif last_state == "speech":
    vad_cls, vad_dot, vad_txt = "speech", "speech pulse", "SPEAKING"
else:
    vad_cls, vad_dot, vad_txt = "silence", "silence", "SILENT"

hb_age   = time.time() - st.session_state.last_updated if st.session_state.last_updated > 0 else None
hb_color = "var(--green)" if (hb_age is not None and hb_age < 2.0) else "var(--red)"
hb_str   = f"{hb_age:.1f}s ago" if hb_age is not None else "—"

backend_color = "green" if online else "red"
backend_label = "ONLINE"  if online else "OFFLINE"
rtp_color     = "green"   if rtp_up  else "muted"
rtp_label     = "ACTIVE"  if rtp_up  else "IDLE"

st.markdown(f"""
<div style="display:flex; align-items:center; gap:1.75rem; margin-bottom:1.4rem; flex-wrap:wrap;">
    <div class="status-pill {vad_cls}">
        <span class="status-dot {vad_dot}"></span>
        {vad_txt}
    </div>
    <div class="conn-row">
        <span class="status-dot {'speech' if online else 'offline'}"></span>
        Flask/Socket.IO &nbsp;<span style="color:var(--{backend_color})">{backend_label}</span>
    </div>
    <div class="conn-row">
        <span class="status-dot {rtp_color}"></span>
        RTP Stream &nbsp;<span style="color:var(--{rtp_color})">{rtp_label}</span>
    </div>
    <div class="conn-row">
        Heartbeat &nbsp;<span style="color:{hb_color};">{hb_str}</span>
    </div>
    <div class="conn-row" style="margin-left:auto;">
        Last frame &nbsp;<span style="color:var(--amber);">#{st.session_state.last_seq}</span>
    </div>
    <div class="conn-row" style="color:var(--muted);">
        ⟳ {datetime.now().strftime("%H:%M:%S.%f")[:-3]}
    </div>
</div>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — METRIC CARDS  (6 columns)
# ─────────────────────────────────────────────────────────────────────────────
ratio      = st.session_state.speech_ratio
prev_ratio = st.session_state.prev_speech_ratio
delta      = ratio - prev_ratio
avg_trt    = st.session_state.avg_trt
fps        = st.session_state.fps
bar_color  = "var(--green)" if ratio > 50 else "var(--amber)" if ratio > 20 else "var(--red)"

# delta badge
if abs(delta) < 0.05:
    delta_cls, delta_sym = "flat", "●  —"
elif delta > 0:
    delta_cls, delta_sym = "up",   f"▲ +{delta:.1f}%"
else:
    delta_cls, delta_sym = "down", f"▼ {delta:.1f}%"

trt_color = "green" if avg_trt < 20 else "amber" if avg_trt < 50 else "red"
fps_color = "green" if fps >= 25    else "amber"  if fps >= 10  else "red"

m1, m2, m3, m4, m5, m6 = st.columns(6)

with m1:
    st.markdown(f"""
    <div class="metric-card amber">
        <div class="metric-label">Total Frames</div>
        <div class="metric-value amber">{st.session_state.frame_count:,}</div>
        <div class="metric-sub">processed since session start</div>
    </div>""", unsafe_allow_html=True)

with m2:
    st.markdown(f"""
    <div class="metric-card green">
        <div class="metric-label">Speech Frames</div>
        <div class="metric-value green">{st.session_state.speech_count:,}</div>
        <div class="metric-sub">VAD positive detections</div>
    </div>""", unsafe_allow_html=True)

with m3:
    st.markdown(f"""
    <div class="metric-card muted">
        <div class="metric-label">Silence Frames</div>
        <div class="metric-value white">{st.session_state.silence_count:,}</div>
        <div class="metric-sub">VAD negative / noise only</div>
    </div>""", unsafe_allow_html=True)

with m4:
    ratio_card_cls = "green" if ratio > 50 else "amber"
    st.markdown(f"""
    <div class="metric-card {ratio_card_cls}">
        <div class="metric-label">Speech Ratio</div>
        <div class="metric-value {ratio_card_cls}">{ratio:.1f}<span class="metric-unit">%</span></div>
        <div class="ratio-bar-wrap">
            <div class="ratio-bar-fill" style="width:{ratio:.1f}%; background:{bar_color};"></div>
        </div>
        <div class="metric-delta {delta_cls}">{delta_sym}</div>
    </div>""", unsafe_allow_html=True)

with m5:
    st.markdown(f"""
    <div class="metric-card {trt_color}">
        <div class="metric-label">Avg Latency (TRT)</div>
        <div class="metric-value {trt_color}">{avg_trt:.1f}<span class="metric-unit"> ms</span></div>
        <div class="metric-sub">mean frame processing time</div>
    </div>""", unsafe_allow_html=True)

with m6:
    fps_pct   = min(fps / FPS_MAX * 100, 100)
    fps_hcol  = "var(--green)" if fps >= 25 else "var(--amber)" if fps >= 10 else "var(--red)"
    st.markdown(f"""
    <div class="metric-card {fps_color}">
        <div class="metric-label">Throughput (FPS)</div>
        <div class="metric-value {fps_color}">{fps:.1f}<span class="metric-unit"> /s</span></div>
        <div class="ratio-bar-wrap" title="System health — {fps_pct:.0f}% of {FPS_MAX:.0f} fps ceiling">
            <div class="ratio-bar-fill" style="width:{fps_pct:.1f}%; background:{fps_hcol};"></div>
        </div>
        <div class="metric-sub">health: {fps_pct:.0f}% of {FPS_MAX:.0f} fps max</div>
    </div>""", unsafe_allow_html=True)

st.markdown("<div style='margin-top:1.4rem'></div>", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4 — CHARTS  (speech ratio | perf panel)
# ─────────────────────────────────────────────────────────────────────────────
chart_l, chart_r = st.columns(2, gap="medium")

with chart_l:
    st.markdown('<div class="section-header">Speech Ratio — Rolling Window (last 60 frames)</div>',
                unsafe_allow_html=True)
    history = list(st.session_state.ratio_history)
    if len(history) >= 2:
        st.plotly_chart(
            make_ratio_chart(history),
            use_container_width=True,
            config={"displayModeBar": False},
        )
    else:
        st.markdown("""
        <div style="height:240px; display:flex; align-items:center; justify-content:center;
                    background:var(--surface); border:1px solid var(--border); border-radius:6px;
                    font-family:var(--mono); font-size:0.72rem; color:var(--muted); letter-spacing:.12em;">
            AWAITING DATA STREAM
        </div>""", unsafe_allow_html=True)

    # Pipeline diagram
    st.markdown("""
    <div style="display:flex; align-items:center; gap:0; margin-top:0.9rem;
                font-family:var(--mono); font-size:0.6rem; color:var(--muted); flex-wrap:wrap;">
        <div style="background:var(--surface2); border:1px solid var(--border); padding:.28rem .65rem; border-radius:4px;">MicroSIP</div>
        <div style="color:var(--border); padding:0 .3rem;">──▶</div>
        <div style="background:var(--surface2); border:1px solid var(--border); padding:.28rem .65rem; border-radius:4px;">SIP/RTP</div>
        <div style="color:var(--border); padding:0 .3rem;">──▶</div>
        <div style="background:var(--surface2); border:1px solid var(--border); padding:.28rem .65rem; border-radius:4px;">RNNoise</div>
        <div style="color:var(--border); padding:0 .3rem;">──▶</div>
        <div style="background:var(--surface2); border:1px solid var(--amber-dim); border-left:2px solid var(--amber); padding:.28rem .65rem; border-radius:4px; color:var(--amber);">VAD</div>
        <div style="color:var(--border); padding:0 .3rem;">──▶</div>
        <div style="background:var(--surface2); border:1px solid var(--border); padding:.28rem .65rem; border-radius:4px;">Socket.IO</div>
        <div style="color:var(--border); padding:0 .3rem;">──▶</div>
        <div style="background:var(--surface2); border:1px solid var(--green-dim); border-left:2px solid var(--green); padding:.28rem .65rem; border-radius:4px; color:var(--green);">Monitor</div>
    </div>
    """, unsafe_allow_html=True)

with chart_r:
    st.markdown('<div class="section-header">Performance — FPS &amp; Latency (last 60 frames)</div>',
                unsafe_allow_html=True)
    fps_hist = list(st.session_state.fps_history)
    trt_hist = list(st.session_state.trt_history)
    if len(fps_hist) >= 2 or len(trt_hist) >= 2:
        st.plotly_chart(
            make_perf_chart(fps_hist, trt_hist),
            use_container_width=True,
            config={"displayModeBar": False},
        )
    else:
        st.markdown("""
        <div style="height:240px; display:flex; align-items:center; justify-content:center;
                    background:var(--surface); border:1px solid var(--border); border-radius:6px;
                    font-family:var(--mono); font-size:0.72rem; color:var(--muted); letter-spacing:.12em;">
            AWAITING DATA STREAM
        </div>""", unsafe_allow_html=True)

    # System health progress bar
    fps_pct_display = min(fps / FPS_MAX * 100, 100)
    health_color    = "#00c98d" if fps >= 25 else "#f0a500" if fps >= 10 else "#ff4455"
    health_label    = "HEALTHY" if fps >= 25 else "DEGRADED" if fps >= 10 else "CRITICAL"
    st.markdown(f"""
    <div class="health-wrap" style="margin-top:0.9rem;">
        <div class="health-label">
            <span>System Throughput Health</span>
            <span style="color:{health_color};">{health_label} — {fps_pct_display:.0f}% of {FPS_MAX:.0f} fps ceiling</span>
        </div>
        <div class="health-bar-track">
            <div class="health-bar-fill" style="width:{fps_pct_display:.1f}%; background:{health_color};
                 box-shadow: 0 0 8px {health_color}55;"></div>
        </div>
    </div>
    """, unsafe_allow_html=True)


st.markdown("<div style='margin-top:1.4rem'></div>", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5 — FRAME EVENT LOG
# ─────────────────────────────────────────────────────────────────────────────
st.markdown('<div class="section-header">Frame Event Log — Latest 20</div>', unsafe_allow_html=True)

logs = list(st.session_state.logs)[:20]   # show last 20
if logs:
    rows_html = ""
    for entry in logs:
        if entry["speech"]:
            icon  = "🟢"
            cls   = "log-speech"
            label = "SPEECH"
        else:
            icon  = "⚪"
            cls   = "log-silence"
            label = "silence"
        rows_html += (
            f'<div>'
            f'<span class="log-ts">{entry["ts"]}</span>'
            f'<span class="log-seq">#{entry["seq"]:05d}</span>'
            f'<span class="log-icon">{icon}</span>'
            f'<span class="{cls}">{label}</span>'
            f'</div>'
        )
    st.markdown(f'<div class="log-panel">{rows_html}</div>', unsafe_allow_html=True)
else:
    st.markdown("""
    <div class="log-panel" style="display:flex;align-items:center;justify-content:center;
         color:var(--muted); letter-spacing:.1em; font-size:.7rem;">
        NO FRAMES RECEIVED YET
    </div>""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6 — FOOTER / CONTROLS
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("<div style='margin-top:1.25rem'></div>", unsafe_allow_html=True)
_, reset_col, _ = st.columns([4, 1, 4])
with reset_col:
    if st.button("⟳  RESET COUNTERS", use_container_width=True):
        reset_session()
        st.rerun()

st.markdown(f"""
<div style="margin-top:0.9rem; font-family:var(--mono); font-size:0.6rem;
            color:var(--muted); text-align:center; letter-spacing:.08em; padding-bottom:1rem;">
    POLL {int(POLL_INTERVAL*1000)} ms &nbsp;·&nbsp;
    BACKEND {BACKEND_URL} &nbsp;·&nbsp;
    SESSION {uptime_str} &nbsp;·&nbsp;
    TRT {st.session_state.avg_trt:.1f} ms &nbsp;·&nbsp;
    FPS {st.session_state.fps:.1f}
</div>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# AUTO REFRESH
# ─────────────────────────────────────────────────────────────────────────────
time.sleep(POLL_INTERVAL)
st.rerun()
