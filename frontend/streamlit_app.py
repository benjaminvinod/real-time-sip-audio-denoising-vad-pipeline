"""
streamlit_app.py — Real-Time SIP Audio Processing Monitor  [v3]
Features added over v2:
  1. SNR / denoise quality chart (raw_energy vs denoised_energy)
  2. Speech boundary event log (speech_start / speech_end)
  3. Multi-call panel (shows active calls from /calls endpoint)
  4. audioClear interrupt button → POST /clear_audio
  5. Backend heartbeat display (server_ts from backend, not just last_updated)
  6. Persistent session across page reloads via session_state
  7. Larger log shows speech_event badges
  8. Footer shows active_calls count
"""

import time
import requests
import streamlit as st
from collections import deque
from datetime import datetime
import plotly.graph_objects as go

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────
BACKEND_URL   = "http://localhost:5000"
POLL_INTERVAL = 0.4
MAX_LOG_LINES = 60
MAX_CHART_PTS = 60
FPS_MAX       = 50.0

# ─────────────────────────────────────────────────────────────────────────────
# PAGE CONFIG
# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="SIP Audio Monitor",
    page_icon="🎙",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ─────────────────────────────────────────────────────────────────────────────
# CSS
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;600&family=IBM+Plex+Sans:wght@300;400;600&display=swap');
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
    --purple:       #a78bfa;
    --muted:        #4a5060;
    --text:         #d8dde8;
    --text-dim:     #6b7280;
    --mono:         'IBM Plex Mono', monospace;
    --sans:         'IBM Plex Sans', sans-serif;
}
html, body, [data-testid="stAppViewContainer"], [data-testid="stApp"] {
    background-color: var(--bg) !important;
    color: var(--text) !important;
    font-family: var(--sans) !important;
}
[data-testid="stHeader"]  { background: transparent !important; }
[data-testid="stToolbar"] { display: none !important; }
.block-container { padding: 1.5rem 2rem 2rem 2rem !important; max-width: 100% !important; }
#MainMenu, footer, header { visibility: hidden; }
::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: var(--surface); }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: var(--muted); }
.js-plotly-plot .plotly, .js-plotly-plot .plotly .main-svg { background: transparent !important; }
[data-testid="stPlotlyChart"] > div { border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
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
.metric-card::before { content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px; }
.metric-card.amber::before  { background: var(--amber); }
.metric-card.green::before  { background: var(--green); }
.metric-card.red::before    { background: var(--red);   }
.metric-card.blue::before   { background: var(--blue);  }
.metric-card.purple::before { background: var(--purple);}
.metric-card.muted::before  { background: var(--border);}
.metric-label { font-size: 0.63rem; letter-spacing: 0.16em; text-transform: uppercase; color: var(--text-dim); margin-bottom: 0.4rem; }
.metric-value { font-size: 2.1rem; font-weight: 600; line-height: 1; }
.metric-value.amber  { color: var(--amber); }
.metric-value.green  { color: var(--green); }
.metric-value.red    { color: var(--red);   }
.metric-value.blue   { color: var(--blue);  }
.metric-value.purple { color: var(--purple);}
.metric-value.white  { color: var(--text);  }
.metric-unit  { font-size: 0.9rem; font-weight: 300; }
.metric-sub   { font-size: 0.67rem; color: var(--text-dim); margin-top: 0.45rem; letter-spacing: 0.04em; }
.metric-delta { font-size: 0.68rem; margin-top: 0.3rem; font-family: var(--mono); }
.metric-delta.up   { color: var(--green); }
.metric-delta.down { color: var(--red);   }
.metric-delta.flat { color: var(--muted); }
.ratio-bar-wrap { background: var(--surface2); border: 1px solid var(--border); border-radius: 4px; height: 6px; overflow: hidden; margin-top: 0.55rem; }
.ratio-bar-fill { height: 100%; border-radius: 4px; transition: width 0.4s ease; }
.status-pill { display: inline-flex; align-items: center; gap: 0.5rem; padding: 0.28rem 0.85rem; border-radius: 999px; font-family: var(--mono); font-size: 0.72rem; font-weight: 600; letter-spacing: 0.09em; text-transform: uppercase; }
.status-pill.speech  { background: var(--green-dim); color: var(--green); border: 1px solid var(--green); box-shadow: 0 0 14px rgba(0,201,141,0.22); }
.status-pill.silence { background: var(--surface2); color: var(--muted); border: 1px solid var(--border); }
.status-pill.offline { background: var(--red-dim); color: var(--red); border: 1px solid var(--red); }
.status-dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; flex-shrink: 0; }
.status-dot.speech  { background: var(--green); box-shadow: 0 0 6px var(--green); }
.status-dot.silence { background: var(--muted); }
.status-dot.offline { background: var(--red);   }
.status-dot.pulse   { animation: pulse-dot 1.2s ease-in-out infinite; }
@keyframes pulse-dot { 0%, 100% { opacity: 1; transform: scale(1); } 50% { opacity: 0.35; transform: scale(0.7); } }
.health-wrap { background: var(--surface); border: 1px solid var(--border); border-radius: 6px; padding: 0.9rem 1.25rem; font-family: var(--mono); }
.health-label { font-size: 0.63rem; letter-spacing: 0.15em; text-transform: uppercase; color: var(--text-dim); margin-bottom: 0.55rem; display: flex; justify-content: space-between; }
.health-bar-track { background: var(--surface2); border: 1px solid var(--border); border-radius: 4px; height: 10px; overflow: hidden; }
.health-bar-fill { height: 100%; border-radius: 4px; transition: width 0.4s ease; }
.conn-row { display: inline-flex; align-items: center; gap: 0.45rem; font-family: var(--mono); font-size: 0.68rem; color: var(--text-dim); }
.log-panel { background: var(--surface); border: 1px solid var(--border); border-radius: 6px; padding: 0.9rem 1.1rem; font-family: var(--mono); font-size: 0.7rem; line-height: 1.75; height: 320px; overflow-y: auto; color: var(--text-dim); }
.log-panel .log-speech  { color: var(--green); }
.log-panel .log-silence { color: var(--muted); }
.log-panel .log-boundary { color: var(--purple); font-weight: 600; }
.log-panel .log-ts      { color: #2a3040; margin-right: 0.5em; }
.log-panel .log-seq     { color: var(--amber-dim); margin-right: 0.5em; }
.log-panel .log-icon    { margin-right: 0.3em; }
.section-header { font-family: var(--mono); font-size: 0.62rem; letter-spacing: 0.2em; text-transform: uppercase; color: var(--muted); border-bottom: 1px solid var(--border); padding-bottom: 0.35rem; margin-bottom: 0.9rem; }
.top-bar { display: flex; align-items: center; justify-content: space-between; border-bottom: 1px solid var(--border); padding-bottom: 0.9rem; margin-bottom: 1.4rem; }
.top-bar-title { font-family: var(--mono); font-size: 0.88rem; font-weight: 600; color: var(--text); letter-spacing: 0.05em; }
.top-bar-sub { font-family: var(--mono); font-size: 0.63rem; color: var(--text-dim); letter-spacing: 0.1em; text-transform: uppercase; margin-top: 0.2rem; }
.top-bar-time { font-family: var(--mono); font-size: 0.72rem; color: var(--text-dim); text-align: right; }
.live-dot { display: inline-block; width: 8px; height: 8px; background: var(--green); border-radius: 50%; margin-right: 0.4rem; box-shadow: 0 0 8px var(--green); animation: pulse-dot 1.5s ease-in-out infinite; }
.calls-panel { background: var(--surface); border: 1px solid var(--border); border-radius: 6px; padding: 0.75rem 1rem; font-family: var(--mono); font-size: 0.68rem; color: var(--text-dim); }
.calls-row { display: flex; gap: 1.5rem; flex-wrap: wrap; }
.call-badge { display: inline-flex; align-items: center; gap: 0.4rem; background: var(--surface2); border: 1px solid var(--border); border-radius: 4px; padding: 0.2rem 0.6rem; color: var(--blue); font-size: 0.65rem; }
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# SESSION STATE INIT  (persists across reloads)
# ─────────────────────────────────────────────────────────────────────────────
defaults = {
    "frame_count":        0,
    "speech_count":       0,
    "silence_count":      0,
    "speech_ratio":       0.0,
    "prev_speech_ratio":  0.0,
    "last_seq":           -1,
    "last_updated":       0.0,
    "is_speech":          False,
    "backend_online":     False,
    "rtp_active":         False,
    "logs":               deque(maxlen=MAX_LOG_LINES),
    "ratio_history":      deque(maxlen=MAX_CHART_PTS),
    "fps_history":        deque(maxlen=MAX_CHART_PTS),
    "trt_history":        deque(maxlen=MAX_CHART_PTS),
    "raw_energy_history": deque(maxlen=MAX_CHART_PTS),
    "den_energy_history": deque(maxlen=MAX_CHART_PTS),
    "snr_history":        deque(maxlen=MAX_CHART_PTS),
    "session_start":      time.time(),
    "last_state":         "silence",
    "avg_trt":            0.0,
    "fps":                0.0,
    "raw_energy":         0.0,
    "denoised_energy":    0.0,
    "snr_db":             0.0,
    "speech_start_count": 0,
    "speech_end_count":   0,
    "active_calls":       0,
    "active_call_list":   [],
    "server_ts":          0.0,
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def poll_backend() -> bool:
    try:
        r = requests.get(f"{BACKEND_URL}/latest", timeout=0.8)
        if r.status_code != 200:
            st.session_state.backend_online = False
            return False

        data = r.json()

        last_updated = data.get("last_updated", 0.0)
        st.session_state.last_updated   = last_updated
        st.session_state.backend_online = (
            last_updated > 0 and (time.time() - last_updated) < 2.0
        )
        st.session_state.rtp_active   = data.get("rtp_active", False)
        st.session_state.server_ts    = data.get("server_ts", 0.0)
        st.session_state.active_calls = data.get("active_calls", 0)

        new_seq = data.get("seq", 0)
        if new_seq == st.session_state.last_seq:
            return False

        st.session_state.prev_speech_ratio = st.session_state.speech_ratio
        st.session_state.last_seq          = new_seq
        st.session_state.frame_count       = data["frame_count"]
        st.session_state.speech_count      = data["speech_count"]
        st.session_state.silence_count     = data["silence_count"]
        st.session_state.speech_ratio      = data["speech_ratio"]
        st.session_state.is_speech         = data["is_speech"]
        st.session_state.last_state        = data.get("last_state", "silence")
        st.session_state.avg_trt           = data.get("avg_trt", 0.0)
        st.session_state.fps               = data.get("fps", 0.0)
        st.session_state.raw_energy        = data.get("raw_energy", 0.0)
        st.session_state.denoised_energy   = data.get("denoised_energy", 0.0)
        st.session_state.snr_db            = data.get("snr_db", 0.0)
        st.session_state.speech_start_count = data.get("speech_start_count", 0)
        st.session_state.speech_end_count   = data.get("speech_end_count", 0)

        st.session_state.ratio_history.append(data["speech_ratio"])
        st.session_state.fps_history.append(st.session_state.fps)
        st.session_state.trt_history.append(st.session_state.avg_trt)
        st.session_state.raw_energy_history.append(st.session_state.raw_energy)
        st.session_state.den_energy_history.append(st.session_state.denoised_energy)
        st.session_state.snr_history.append(st.session_state.snr_db)

        ts = datetime.now().strftime("%H:%M:%S.%f")[:-4]
        speech_event = data.get("speech_event", "")
        st.session_state.logs.appendleft({
            "ts":           ts,
            "seq":          new_seq,
            "speech":       data["is_speech"],
            "speech_event": speech_event,
        })
        return True

    except requests.exceptions.ConnectionError:
        st.session_state.backend_online = False
        return False
    except Exception:
        st.session_state.backend_online = False
        return False


def poll_calls():
    """Fetch list of active calls for the multi-call panel."""
    try:
        r = requests.get(f"{BACKEND_URL}/calls", timeout=0.5)
        if r.status_code == 200:
            st.session_state.active_call_list = r.json()
    except Exception:
        pass


def reset_session():
    try:
        requests.get(f"{BACKEND_URL}/reset", timeout=1)
    except Exception:
        pass
    for k, v in defaults.items():
        if k == "session_start":
            continue
        if isinstance(v, deque):
            st.session_state[k] = type(v)(v.maxlen)
        else:
            st.session_state[k] = v
    st.session_state.session_start = time.time()


def clear_audio(call_id: str = ""):
    """Send audioClear interrupt to backend."""
    try:
        payload = {"call_id": call_id} if call_id else {}
        requests.post(f"{BACKEND_URL}/clear_audio", json=payload, timeout=1)
    except Exception:
        pass
    # Also reset local charts
    for key in ("ratio_history", "fps_history", "trt_history",
                "raw_energy_history", "den_energy_history", "snr_history"):
        st.session_state[key] = deque(maxlen=MAX_CHART_PTS)


# ─────────────────────────────────────────────────────────────────────────────
# PLOTLY HELPERS
# ─────────────────────────────────────────────────────────────────────────────
_PLOTLY_LAYOUT = dict(
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="#111318",
    font=dict(family="IBM Plex Mono, monospace", color="#6b7280", size=11),
    margin=dict(l=48, r=16, t=24, b=36),
    xaxis=dict(showgrid=False, zeroline=False, tickfont=dict(size=10), color="#4a5060"),
    yaxis=dict(gridcolor="#1e222c", zeroline=False, tickfont=dict(size=10), color="#4a5060"),
    hovermode="x unified",
    hoverlabel=dict(
        bgcolor="#181b22", bordercolor="#262b35",
        font=dict(family="IBM Plex Mono, monospace", size=11, color="#d8dde8"),
    ),
)


def _layout(**overrides) -> dict:
    merged = dict(_PLOTLY_LAYOUT)
    for k, v in overrides.items():
        if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
            merged[k] = {**merged[k], **v}
        else:
            merged[k] = v
    return merged


def _smooth(series: list, window: int = 3) -> list:
    out = []
    for i, v in enumerate(series):
        lo = max(0, i - window + 1)
        out.append(sum(series[lo:i+1]) / (i - lo + 1))
    return out


def make_ratio_chart(history: list) -> go.Figure:
    xs = list(range(len(history)))
    ys = _smooth(history, window=4)
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=xs, y=ys, mode="lines",
        fill="tozeroy", fillcolor="rgba(0,201,141,0.08)",
        line=dict(color="#00c98d", width=2.5, shape="spline", smoothing=1.0),
        name="Speech Ratio", hovertemplate="%{y:.1f}%<extra></extra>",
    ))
    fig.add_hline(y=50, line=dict(color="#262b35", width=1, dash="dot"),
                  annotation_text="50 %",
                  annotation_font=dict(size=9, color="#4a5060"),
                  annotation_position="right")
    fig.update_layout(**_layout(yaxis=dict(range=[0, 105], ticksuffix="%"),
                                showlegend=False, height=240))
    return fig


def make_perf_chart(fps_hist: list, trt_hist: list) -> go.Figure:
    n  = max(len(fps_hist), len(trt_hist))
    xs = list(range(n))
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=xs, y=list(fps_hist), mode="lines",
        line=dict(color="#4c9fff", width=2.2, shape="spline", smoothing=0.9),
        name="FPS", yaxis="y", hovertemplate="%{y:.1f} fps<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=xs, y=list(trt_hist), mode="lines",
        line=dict(color="#f0a500", width=2.2, shape="spline", smoothing=0.9, dash="dot"),
        name="TRT (ms)", yaxis="y2", hovertemplate="%{y:.1f} ms<extra></extra>",
    ))
    fig.update_layout(**_layout(
        yaxis=dict(title="FPS", tickfont=dict(color="#4c9fff", size=10)),
        yaxis2=dict(overlaying="y", side="right", title="Latency (ms)",
                    gridcolor="#1e222c", zeroline=False,
                    tickfont=dict(color="#f0a500", size=10), color="#f0a500"),
        legend=dict(orientation="h", x=0, y=1.08,
                    font=dict(size=10, color="#6b7280"), bgcolor="rgba(0,0,0,0)"),
        height=240,
    ))
    return fig


def make_snr_chart(raw_hist: list, den_hist: list, snr_hist: list) -> go.Figure:
    """Three-trace chart: raw energy (dB), denoised energy (dB), SNR improvement (dB)."""
    n  = max(len(raw_hist), len(den_hist), len(snr_hist))
    xs = list(range(n))
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=xs, y=list(raw_hist), mode="lines",
        line=dict(color="#ff4455", width=1.8, shape="spline", smoothing=0.8, dash="dot"),
        name="Raw Energy (dB)", hovertemplate="%{y:.1f} dB<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=xs, y=list(den_hist), mode="lines",
        line=dict(color="#00c98d", width=2.0, shape="spline", smoothing=0.8),
        name="Denoised Energy (dB)", hovertemplate="%{y:.1f} dB<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=xs, y=list(snr_hist), mode="lines",
        line=dict(color="#a78bfa", width=1.8, shape="spline", smoothing=0.8),
        name="SNR Improvement (dB)", yaxis="y2",
        hovertemplate="%{y:+.1f} dB<extra></extra>",
    ))
    fig.update_layout(**_layout(
        yaxis=dict(title="Energy (dBFS)", tickfont=dict(size=10)),
        yaxis2=dict(overlaying="y", side="right", title="SNR ΔdB",
                    gridcolor="#1e222c", zeroline=True, zerolinecolor="#262b35",
                    tickfont=dict(color="#a78bfa", size=10), color="#a78bfa"),
        legend=dict(orientation="h", x=0, y=1.08,
                    font=dict(size=10, color="#6b7280"), bgcolor="rgba(0,0,0,0)"),
        height=240,
    ))
    return fig


# ─────────────────────────────────────────────────────────────────────────────
# POLL
# ─────────────────────────────────────────────────────────────────────────────
poll_backend()
poll_calls()

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — TITLE
# ─────────────────────────────────────────────────────────────────────────────
now_str    = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
uptime_s   = int(time.time() - st.session_state.session_start)
uptime_str = f"{uptime_s // 3600:02d}:{(uptime_s % 3600) // 60:02d}:{uptime_s % 60:02d}"

st.markdown(f"""
<div class="top-bar">
    <div>
        <div class="top-bar-title"><span class="live-dot"></span>SIP AUDIO PROCESSING MONITOR</div>
        <div class="top-bar-sub">RNNoise + VAD Pipeline  ·  G.711 μ-law / 8 kHz → 16 kHz</div>
    </div>
    <div class="top-bar-time">{now_str}<br><span style="color:var(--muted)">UP</span> {uptime_str}</div>
</div>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — STATUS ROW
# ─────────────────────────────────────────────────────────────────────────────
online     = st.session_state.backend_online
rtp_up     = st.session_state.rtp_active
last_state = st.session_state.last_state

if not online:
    vad_cls, vad_dot, vad_txt = "offline", "offline", "BACKEND OFFLINE"
elif not rtp_up:
    vad_cls, vad_dot, vad_txt = "silence", "silence", "WAITING FOR RTP"
elif last_state == "speech":
    vad_cls, vad_dot, vad_txt = "speech", "speech pulse", "SPEAKING"
else:
    vad_cls, vad_dot, vad_txt = "silence", "silence", "SILENT"

# Heartbeat using server_ts (backend clock, not local)
server_ts  = st.session_state.server_ts
hb_age     = time.time() - server_ts if server_ts > 0 else None
hb_color   = "var(--green)" if (hb_age is not None and hb_age < 2.0) else "var(--red)"
hb_str     = f"{hb_age:.1f}s ago" if hb_age is not None else "—"

active_calls = st.session_state.active_calls

st.markdown(f"""
<div style="display:flex; align-items:center; gap:1.75rem; margin-bottom:1.4rem; flex-wrap:wrap;">
    <div class="status-pill {vad_cls}"><span class="status-dot {vad_dot}"></span>{vad_txt}</div>
    <div class="conn-row"><span class="status-dot {'speech' if online else 'offline'}"></span>
        Backend &nbsp;<span style="color:var({'--green' if online else '--red'})">{'ONLINE' if online else 'OFFLINE'}</span>
    </div>
    <div class="conn-row"><span class="status-dot {'speech' if rtp_up else 'muted'}"></span>
        RTP &nbsp;<span style="color:var({'--green' if rtp_up else '--muted'})">{'ACTIVE' if rtp_up else 'IDLE'}</span>
    </div>
    <div class="conn-row">Heartbeat &nbsp;<span style="color:{hb_color};">{hb_str}</span></div>
    <div class="conn-row">
        Calls &nbsp;<span style="color:var(--blue);">{active_calls}</span>
    </div>
    <div class="conn-row" style="margin-left:auto;">
        Last frame &nbsp;<span style="color:var(--amber);">#{st.session_state.last_seq}</span>
    </div>
    <div class="conn-row" style="color:var(--muted);">⟳ {datetime.now().strftime("%H:%M:%S.%f")[:-3]}</div>
</div>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2b — MULTI-CALL PANEL
# ─────────────────────────────────────────────────────────────────────────────
call_list = st.session_state.active_call_list
if call_list:
    badges = "".join(
        f'<span class="call-badge">🔵 {c["call_id"][:24]} · {c["state"]}</span>'
        for c in call_list
    )
    st.markdown(f"""
    <div class="calls-panel" style="margin-bottom:1.2rem;">
        <div style="font-size:0.6rem; letter-spacing:.15em; text-transform:uppercase;
                    color:var(--muted); margin-bottom:.45rem;">Active Calls</div>
        <div class="calls-row">{badges}</div>
    </div>
    """, unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — METRIC CARDS (8 columns)
# ─────────────────────────────────────────────────────────────────────────────
ratio      = st.session_state.speech_ratio
prev_ratio = st.session_state.prev_speech_ratio
delta      = ratio - prev_ratio
avg_trt    = st.session_state.avg_trt
fps        = st.session_state.fps
snr        = st.session_state.snr_db
bar_color  = "var(--green)" if ratio > 50 else "var(--amber)" if ratio > 20 else "var(--red)"

if abs(delta) < 0.05:
    delta_cls, delta_sym = "flat", "●  —"
elif delta > 0:
    delta_cls, delta_sym = "up", f"▲ +{delta:.1f}%"
else:
    delta_cls, delta_sym = "down", f"▼ {delta:.1f}%"

trt_color = "green" if avg_trt < 20 else "amber" if avg_trt < 50 else "red"
fps_color = "green" if fps >= 25    else "amber"  if fps >= 10  else "red"
snr_color = "green" if snr  > 3     else "amber"  if snr > 0    else "red"

m1, m2, m3, m4, m5, m6, m7, m8 = st.columns(8)

with m1:
    st.markdown(f"""<div class="metric-card amber">
        <div class="metric-label">Total Frames</div>
        <div class="metric-value amber">{st.session_state.frame_count:,}</div>
        <div class="metric-sub">since session start</div>
    </div>""", unsafe_allow_html=True)

with m2:
    st.markdown(f"""<div class="metric-card green">
        <div class="metric-label">Speech Frames</div>
        <div class="metric-value green">{st.session_state.speech_count:,}</div>
        <div class="metric-sub">VAD positive</div>
    </div>""", unsafe_allow_html=True)

with m3:
    st.markdown(f"""<div class="metric-card muted">
        <div class="metric-label">Silence Frames</div>
        <div class="metric-value white">{st.session_state.silence_count:,}</div>
        <div class="metric-sub">VAD negative</div>
    </div>""", unsafe_allow_html=True)

with m4:
    st.markdown(f"""<div class="metric-card {'green' if ratio > 50 else 'amber'}">
        <div class="metric-label">Speech Ratio</div>
        <div class="metric-value {'green' if ratio > 50 else 'amber'}">{ratio:.1f}<span class="metric-unit">%</span></div>
        <div class="ratio-bar-wrap"><div class="ratio-bar-fill" style="width:{ratio:.1f}%; background:{bar_color};"></div></div>
        <div class="metric-delta {delta_cls}">{delta_sym}</div>
    </div>""", unsafe_allow_html=True)

with m5:
    st.markdown(f"""<div class="metric-card {trt_color}">
        <div class="metric-label">Avg Latency (TRT)</div>
        <div class="metric-value {trt_color}">{avg_trt:.1f}<span class="metric-unit"> ms</span></div>
        <div class="metric-sub">mean processing time</div>
    </div>""", unsafe_allow_html=True)

with m6:
    fps_pct  = min(fps / FPS_MAX * 100, 100)
    fps_hcol = "var(--green)" if fps >= 25 else "var(--amber)" if fps >= 10 else "var(--red)"
    st.markdown(f"""<div class="metric-card {fps_color}">
        <div class="metric-label">Throughput (FPS)</div>
        <div class="metric-value {fps_color}">{fps:.1f}<span class="metric-unit"> /s</span></div>
        <div class="ratio-bar-wrap"><div class="ratio-bar-fill" style="width:{fps_pct:.1f}%; background:{fps_hcol};"></div></div>
        <div class="metric-sub">health: {fps_pct:.0f}% of {FPS_MAX:.0f} fps</div>
    </div>""", unsafe_allow_html=True)

with m7:
    st.markdown(f"""<div class="metric-card {snr_color}">
        <div class="metric-label">SNR Improvement</div>
        <div class="metric-value {snr_color}">{snr:+.1f}<span class="metric-unit"> dB</span></div>
        <div class="metric-sub">denoised − raw energy</div>
    </div>""", unsafe_allow_html=True)

with m8:
    st.markdown(f"""<div class="metric-card purple">
        <div class="metric-label">Speech Boundaries</div>
        <div class="metric-value purple">{st.session_state.speech_start_count}</div>
        <div class="metric-sub">starts · {st.session_state.speech_end_count} ends</div>
    </div>""", unsafe_allow_html=True)

st.markdown("<div style='margin-top:1.4rem'></div>", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4 — CHARTS (3 columns)
# ─────────────────────────────────────────────────────────────────────────────
chart_l, chart_m, chart_r = st.columns(3, gap="medium")

with chart_l:
    st.markdown('<div class="section-header">Speech Ratio — Rolling (last 60 frames)</div>',
                unsafe_allow_html=True)
    history = list(st.session_state.ratio_history)
    if len(history) >= 2:
        st.plotly_chart(make_ratio_chart(history), use_container_width=True,
                        config={"displayModeBar": False})
    else:
        st.markdown("""<div style="height:240px;display:flex;align-items:center;
            justify-content:center;background:var(--surface);border:1px solid var(--border);
            border-radius:6px;font-family:var(--mono);font-size:0.72rem;color:var(--muted);
            letter-spacing:.12em;">AWAITING DATA STREAM</div>""", unsafe_allow_html=True)

with chart_m:
    st.markdown('<div class="section-header">Performance — FPS &amp; Latency</div>',
                unsafe_allow_html=True)
    fps_hist = list(st.session_state.fps_history)
    trt_hist = list(st.session_state.trt_history)
    if len(fps_hist) >= 2 or len(trt_hist) >= 2:
        st.plotly_chart(make_perf_chart(fps_hist, trt_hist), use_container_width=True,
                        config={"displayModeBar": False})
    else:
        st.markdown("""<div style="height:240px;display:flex;align-items:center;
            justify-content:center;background:var(--surface);border:1px solid var(--border);
            border-radius:6px;font-family:var(--mono);font-size:0.72rem;color:var(--muted);
            letter-spacing:.12em;">AWAITING DATA STREAM</div>""", unsafe_allow_html=True)

with chart_r:
    st.markdown('<div class="section-header">Denoise Quality — Energy &amp; SNR</div>',
                unsafe_allow_html=True)
    raw_hist = list(st.session_state.raw_energy_history)
    den_hist = list(st.session_state.den_energy_history)
    snr_hist = list(st.session_state.snr_history)
    if len(raw_hist) >= 2:
        st.plotly_chart(make_snr_chart(raw_hist, den_hist, snr_hist),
                        use_container_width=True, config={"displayModeBar": False})
    else:
        st.markdown("""<div style="height:240px;display:flex;align-items:center;
            justify-content:center;background:var(--surface);border:1px solid var(--border);
            border-radius:6px;font-family:var(--mono);font-size:0.72rem;color:var(--muted);
            letter-spacing:.12em;">AWAITING DATA STREAM</div>""", unsafe_allow_html=True)

# System health bar under perf chart
fps_pct_display = min(fps / FPS_MAX * 100, 100)
health_color    = "#00c98d" if fps >= 25 else "#f0a500" if fps >= 10 else "#ff4455"
health_label    = "HEALTHY" if fps >= 25 else "DEGRADED" if fps >= 10 else "CRITICAL"
st.markdown(f"""
<div class="health-wrap" style="margin-top:0.9rem;">
    <div class="health-label">
        <span>System Throughput Health</span>
        <span style="color:{health_color};">{health_label} — {fps_pct_display:.0f}% of {FPS_MAX:.0f} fps</span>
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
st.markdown('<div class="section-header">Frame Event Log — Latest 20</div>',
            unsafe_allow_html=True)
logs = list(st.session_state.logs)[:20]
if logs:
    rows_html = ""
    for entry in logs:
        ev = entry.get("speech_event", "")
        if ev == "speech_start":
            icon  = "▶️"
            cls   = "log-boundary"
            label = "SPEECH START"
        elif ev == "speech_end":
            icon  = "⏹️"
            cls   = "log-boundary"
            label = "SPEECH END"
        elif entry["speech"]:
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
    st.markdown("""<div class="log-panel" style="display:flex;align-items:center;
        justify-content:center;color:var(--muted);letter-spacing:.1em;font-size:.7rem;">
        NO FRAMES RECEIVED YET</div>""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6 — CONTROLS
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("<div style='margin-top:1.25rem'></div>", unsafe_allow_html=True)
ctrl1, ctrl2, ctrl3 = st.columns([3, 1, 1])

with ctrl2:
    if st.button("⟳  RESET COUNTERS", use_container_width=True):
        reset_session()
        st.rerun()

with ctrl3:
    if st.button("🔇  CLEAR AUDIO", use_container_width=True):
        clear_audio()
        st.rerun()

st.markdown(f"""
<div style="margin-top:0.9rem; font-family:var(--mono); font-size:0.6rem;
            color:var(--muted); text-align:center; letter-spacing:.08em; padding-bottom:1rem;">
    POLL {int(POLL_INTERVAL*1000)} ms &nbsp;·&nbsp;
    BACKEND {BACKEND_URL} &nbsp;·&nbsp;
    SESSION {uptime_str} &nbsp;·&nbsp;
    TRT {st.session_state.avg_trt:.1f} ms &nbsp;·&nbsp;
    FPS {st.session_state.fps:.1f} &nbsp;·&nbsp;
    CALLS {st.session_state.active_calls} &nbsp;·&nbsp;
    SNR {st.session_state.snr_db:+.1f} dB
</div>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# AUTO REFRESH
# ─────────────────────────────────────────────────────────────────────────────
time.sleep(POLL_INTERVAL)
st.rerun()
