"""
streamlit_app.py — Real-Time SIP Audio Processing Monitor
Benjamin Vinod | Module 1 Frontend
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
MAX_CHART_PTS = 60    # rolling window for speech ratio sparkline

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
# GLOBAL CSS — industrial dark theme
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
[data-testid="stHeader"] { background: transparent !important; }
[data-testid="stToolbar"] { display: none !important; }
.block-container { padding: 1.5rem 2rem 2rem 2rem !important; max-width: 100% !important; }

/* ── hide default streamlit chrome ── */
#MainMenu, footer, header { visibility: hidden; }

/* ── metric cards ── */
.metric-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 1.25rem 1.5rem;
    font-family: var(--mono);
    position: relative;
    overflow: hidden;
}
.metric-card::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 2px;
}
.metric-card.amber::before { background: var(--amber); }
.metric-card.green::before { background: var(--green); }
.metric-card.red::before   { background: var(--red); }
.metric-card.muted::before { background: var(--border); }

.metric-label {
    font-size: 0.65rem;
    letter-spacing: 0.15em;
    text-transform: uppercase;
    color: var(--text-dim);
    margin-bottom: 0.35rem;
}
.metric-value {
    font-size: 2.2rem;
    font-weight: 600;
    line-height: 1;
}
.metric-value.amber { color: var(--amber); }
.metric-value.green { color: var(--green); }
.metric-value.red   { color: var(--red); }
.metric-value.white { color: var(--text); }
.metric-sub {
    font-size: 0.7rem;
    color: var(--text-dim);
    margin-top: 0.4rem;
}

/* ── status badge ── */
.status-pill {
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.3rem 0.9rem;
    border-radius: 999px;
    font-family: var(--mono);
    font-size: 0.75rem;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
}
.status-pill.speech {
    background: var(--green-dim);
    color: var(--green);
    border: 1px solid var(--green);
    box-shadow: 0 0 12px rgba(0,201,141,0.25);
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
}
.status-dot.speech  { background: var(--green); box-shadow: 0 0 6px var(--green); }
.status-dot.silence { background: var(--muted); }
.status-dot.offline { background: var(--red); }
.status-dot.pulse {
    animation: pulse-dot 1.2s ease-in-out infinite;
}
@keyframes pulse-dot {
    0%, 100% { opacity: 1; transform: scale(1); }
    50%       { opacity: 0.4; transform: scale(0.75); }
}

/* ── log panel ── */
.log-panel {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 1rem 1.25rem;
    font-family: var(--mono);
    font-size: 0.72rem;
    line-height: 1.7;
    height: 300px;
    overflow-y: auto;
    color: var(--text-dim);
}
.log-panel .log-speech  { color: var(--green); }
.log-panel .log-silence { color: var(--muted); }
.log-panel .log-ts      { color: #2a3040; margin-right: 0.6em; }
.log-panel .log-seq     { color: var(--amber-dim); margin-right: 0.5em; }

/* ── section headers ── */
.section-header {
    font-family: var(--mono);
    font-size: 0.65rem;
    letter-spacing: 0.2em;
    text-transform: uppercase;
    color: var(--muted);
    border-bottom: 1px solid var(--border);
    padding-bottom: 0.4rem;
    margin-bottom: 1rem;
}

/* ── top bar ── */
.top-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-bottom: 1px solid var(--border);
    padding-bottom: 1rem;
    margin-bottom: 1.5rem;
}
.top-bar-title {
    font-family: var(--mono);
    font-size: 0.9rem;
    font-weight: 600;
    color: var(--text);
    letter-spacing: 0.05em;
}
.top-bar-sub {
    font-family: var(--mono);
    font-size: 0.65rem;
    color: var(--text-dim);
    letter-spacing: 0.1em;
    text-transform: uppercase;
    margin-top: 0.15rem;
}
.top-bar-time {
    font-family: var(--mono);
    font-size: 0.75rem;
    color: var(--muted);
}

/* ── ratio bar ── */
.ratio-bar-wrap {
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 4px;
    height: 8px;
    overflow: hidden;
    margin-top: 0.5rem;
}
.ratio-bar-fill {
    height: 100%;
    border-radius: 4px;
    transition: width 0.4s ease;
}

/* ── conn indicator ── */
.conn-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-family: var(--mono);
    font-size: 0.7rem;
    color: var(--text-dim);
}
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# SESSION STATE INIT
# ─────────────────────────────────────────────────────────────────────────────
defaults = {
    "frame_count":    0,
    "speech_count":   0,
    "silence_count":  0,
    "speech_ratio":   0.0,
    "last_seq":       -1,
    "last_updated":   0.0,
    "is_speech":      False,
    "backend_online": False,
    "rtp_active":     False,
    "logs":           deque(maxlen=MAX_LOG_LINES),
    "ratio_history":  deque(maxlen=MAX_CHART_PTS),
    "session_start":  time.time(),
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v


# ─────────────────────────────────────────────────────────────────────────────
# POLLING FUNCTION
# ─────────────────────────────────────────────────────────────────────────────
def poll_backend() -> bool:
    """
    Fetch /latest from Flask backend.
    Returns True if new data was received (seq changed), False otherwise.
    """
    try:
        r = requests.get(f"{BACKEND_URL}/latest", timeout=0.8)
        if r.status_code != 200:
            st.session_state.backend_online = False
            return False

        data = r.json()
        st.session_state.backend_online = True
        st.session_state.rtp_active     = data.get("rtp_active", False)

        new_seq = data.get("seq", 0)
        if new_seq == st.session_state.last_seq:
            return False   # no new frame yet

        # ── update state ──
        st.session_state.last_seq      = new_seq
        st.session_state.frame_count   = data["frame_count"]
        st.session_state.speech_count  = data["speech_count"]
        st.session_state.silence_count = data["silence_count"]
        st.session_state.speech_ratio  = data["speech_ratio"]
        st.session_state.is_speech     = data["is_speech"]
        st.session_state.last_updated  = data["last_updated"]

        # ── append to rolling ratio chart ──
        st.session_state.ratio_history.append(data["speech_ratio"])

        # ── append to log ──
        ts    = datetime.now().strftime("%H:%M:%S.%f")[:-4]
        label = "SPEECH" if data["is_speech"] else "silence"
        st.session_state.logs.appendleft(
            {"ts": ts, "seq": new_seq, "label": label, "speech": data["is_speech"]}
        )
        return True

    except requests.exceptions.ConnectionError:
        st.session_state.backend_online = False
        return False
    except Exception:
        st.session_state.backend_online = False
        return False


# ─────────────────────────────────────────────────────────────────────────────
# RESET HANDLER
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


# ─────────────────────────────────────────────────────────────────────────────
# POLL
# ─────────────────────────────────────────────────────────────────────────────
poll_backend()


# ─────────────────────────────────────────────────────────────────────────────
# RENDER — TOP BAR
# ─────────────────────────────────────────────────────────────────────────────
now_str    = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
uptime_s   = int(time.time() - st.session_state.session_start)
uptime_str = f"{uptime_s // 3600:02d}:{(uptime_s % 3600) // 60:02d}:{uptime_s % 60:02d}"

col_title, col_time = st.columns([3, 1])
with col_title:
    st.markdown(f"""
    <div class="top-bar">
        <div>
            <div class="top-bar-title">SIP AUDIO PROCESSING MONITOR</div>
            <div class="top-bar-sub">RNNoise + VAD Pipeline  ·  G.711 μ-law / 8 kHz → 16 kHz</div>
        </div>
        <div class="top-bar-time">{now_str} &nbsp;·&nbsp; UP {uptime_str}</div>
    </div>
    """, unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# RENDER — STATUS ROW
# ─────────────────────────────────────────────────────────────────────────────
online  = st.session_state.backend_online
rtp_up  = st.session_state.rtp_active
speech  = st.session_state.is_speech

if not online:
    vad_cls  = "offline"; vad_dot = "offline"; vad_txt = "BACKEND OFFLINE"
elif not rtp_up:
    vad_cls  = "silence"; vad_dot = "silence"; vad_txt = "WAITING FOR RTP"
elif speech:
    vad_cls  = "speech";  vad_dot = "speech pulse"; vad_txt = "SPEECH DETECTED"
else:
    vad_cls  = "silence"; vad_dot = "silence"; vad_txt = "SILENCE"

backend_dot  = "green"  if online else "red"
rtp_dot      = "speech" if rtp_up  else "silence"
backend_pill = f'<span style="color:var(--{"green" if online else "red"})">{"ONLINE" if online else "OFFLINE"}</span>'
rtp_pill     = f'<span style="color:var(--{"green" if rtp_up else "muted"})">{"ACTIVE" if rtp_up else "IDLE"}</span>'

st.markdown(f"""
<div style="display:flex; align-items:center; gap:2rem; margin-bottom:1.5rem; flex-wrap:wrap;">
    <div class="status-pill {vad_cls}">
        <span class="status-dot {vad_dot}"></span>
        {vad_txt}
    </div>
    <div class="conn-row">
        <span class="status-dot {'speech' if online else 'offline'}"></span>
        Flask/Socket.IO &nbsp;{backend_pill}
    </div>
    <div class="conn-row">
        <span class="status-dot {rtp_dot}"></span>
        RTP Stream &nbsp;{rtp_pill}
    </div>
    <div class="conn-row" style="margin-left:auto; color:var(--text-dim)">
        Last frame: <span style="color:var(--amber); margin-left:.4em;">#{st.session_state.last_seq}</span>
    </div>
</div>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# RENDER — METRIC CARDS
# ─────────────────────────────────────────────────────────────────────────────
ratio      = st.session_state.speech_ratio
ratio_fill = f"{ratio:.1f}%"
bar_color  = "var(--green)" if ratio > 50 else "var(--amber)" if ratio > 20 else "var(--red)"

m1, m2, m3, m4 = st.columns(4)

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
    st.markdown(f"""
    <div class="metric-card {'green' if ratio > 50 else 'amber'}">
        <div class="metric-label">Speech Ratio</div>
        <div class="metric-value {'green' if ratio > 50 else 'amber'}">{ratio:.1f}<span style="font-size:1rem;font-weight:300">%</span></div>
        <div class="ratio-bar-wrap">
            <div class="ratio-bar-fill" style="width:{ratio:.1f}%; background:{bar_color};"></div>
        </div>
    </div>""", unsafe_allow_html=True)

st.markdown("<div style='margin-top:1.5rem'></div>", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# RENDER — CHART + LOG (side by side)
# ─────────────────────────────────────────────────────────────────────────────
chart_col, log_col = st.columns([1, 1], gap="medium")

with chart_col:
    st.markdown('<div class="section-header">Speech Ratio — Rolling Window (last 60 frames)</div>', unsafe_allow_html=True)

    history = list(st.session_state.ratio_history)
    if len(history) >= 2:
        import pandas as pd
        df = pd.DataFrame({"Speech Ratio (%)": history})
        st.line_chart(
            df,
            color="#00c98d",
            height=260,
            use_container_width=True,
        )
    else:
        st.markdown("""
        <div style="height:260px; display:flex; align-items:center; justify-content:center;
                    background:var(--surface); border:1px solid var(--border); border-radius:6px;
                    font-family:var(--mono); font-size:0.75rem; color:var(--muted); letter-spacing:.1em;">
            AWAITING DATA STREAM
        </div>""", unsafe_allow_html=True)

    # Mini pipeline diagram below chart
    st.markdown("""
    <div style="display:flex; align-items:center; gap:0; margin-top:1rem;
                font-family:var(--mono); font-size:0.62rem; color:var(--muted);">
        <div style="background:var(--surface2); border:1px solid var(--border); padding:.3rem .7rem; border-radius:4px;">MicroSIP</div>
        <div style="color:var(--border); padding:0 .4rem;">──▶</div>
        <div style="background:var(--surface2); border:1px solid var(--border); padding:.3rem .7rem; border-radius:4px;">SIP/RTP</div>
        <div style="color:var(--border); padding:0 .4rem;">──▶</div>
        <div style="background:var(--surface2); border:1px solid var(--border); padding:.3rem .7rem; border-radius:4px;">RNNoise</div>
        <div style="color:var(--border); padding:0 .4rem;">──▶</div>
        <div style="background:var(--surface2); border:1px solid var(--amber-dim); border-left:2px solid var(--amber); padding:.3rem .7rem; border-radius:4px; color:var(--amber);">VAD</div>
        <div style="color:var(--border); padding:0 .4rem;">──▶</div>
        <div style="background:var(--surface2); border:1px solid var(--border); padding:.3rem .7rem; border-radius:4px;">Socket.IO</div>
        <div style="color:var(--border); padding:0 .4rem;">──▶</div>
        <div style="background:var(--surface2); border:1px solid var(--green-dim); border-left:2px solid var(--green); padding:.3rem .7rem; border-radius:4px; color:var(--green);">Monitor</div>
    </div>
    """, unsafe_allow_html=True)


with log_col:
    st.markdown('<div class="section-header">Frame Event Log — Latest 40</div>', unsafe_allow_html=True)

    logs = list(st.session_state.logs)
    if logs:
        rows_html = ""
        for entry in logs:
            cls   = "log-speech" if entry["speech"] else "log-silence"
            label = "◆ SPEECH" if entry["speech"] else "· silence"
            rows_html += (
                f'<div>'
                f'<span class="log-ts">{entry["ts"]}</span>'
                f'<span class="log-seq">#{entry["seq"]:05d}</span>'
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
# RENDER — FOOTER / CONTROLS
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("<div style='margin-top:1.5rem'></div>", unsafe_allow_html=True)
_, reset_col, _ = st.columns([4, 1, 4])
with reset_col:
    if st.button("⟳  RESET COUNTERS", use_container_width=True):
        reset_session()
        st.rerun()

st.markdown(f"""
<div style="margin-top:1rem; font-family:var(--mono); font-size:0.62rem;
            color:var(--muted); text-align:center; letter-spacing:.08em;">
    POLL INTERVAL {int(POLL_INTERVAL*1000)} ms &nbsp;·&nbsp;
    BACKEND {BACKEND_URL} &nbsp;·&nbsp;
    SESSION {uptime_str}
</div>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# AUTO REFRESH
# ─────────────────────────────────────────────────────────────────────────────
time.sleep(POLL_INTERVAL)
st.rerun()
