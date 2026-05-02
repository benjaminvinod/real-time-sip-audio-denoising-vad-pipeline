"""
sip_server.py  –  Multi-call SIP/RTP/VAD/STT/LLM pipeline
===========================================================
Architecture overview:
  RTP → DenoiseVAD → STT (speech_end triggered, faster-whisper)
                   → transcript accumulated per call
  BYE  → LLM post-call analysis (Ollama llama3.1)
       → emit "llm_report" via Socket.IO

CRITICAL constraints respected:
  • eventlet.monkey_patch() is unconditionally first
  • RTP / VAD pipeline is NOT modified
  • STT pipeline (ThreadPoolExecutor, max_workers=1) is NOT modified
  • LLM runs ONLY post-call (triggered by BYE) in its own executor
  • LLM NEVER touches RTP, VAD, or STT logic
  • Ollama called locally via requests.post (no external API)
  • Output is strict JSON: summary, intent, sentiment, risk_level, suggested_action
  • Result emitted via sio.emit("llm_report", payload)
"""

# ── MUST be absolutely first ──────────────────────────────────────────────────
import eventlet
eventlet.monkey_patch(os=True, select=True, socket=True, thread=True, time=True)
# ─────────────────────────────────────────────────────────────────────────────

import audioop
import heapq
import json
import random
import re
import socket
import struct
import threading
import time
import base64
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import requests
import samplerate

from flask import Flask, jsonify, request
import socketio

from db_manager import init_db, save_call, get_recent_calls, get_call

from denoiseVADHandler import DenoiseVADHandler
from metricsLogger import MetricsLogger
from ai_client import send_to_ai  # stub — safe import


# ── Socket.IO / Flask setup ───────────────────────────────────────────────────

sio = socketio.Server(
    cors_allowed_origins="*",
    async_mode="eventlet",
    ping_timeout=60,
    ping_interval=25,
    max_http_buffer_size=1_000_000,
    logger=False,
    engineio_logger=False,
)
app = Flask(__name__)
app.wsgi_app = socketio.WSGIApp(sio, app.wsgi_app)

init_db()


# ── Socket.IO lifecycle ───────────────────────────────────────────────────────

@sio.event
def connect(sid, environ):
    print(f"🔌 WS client connected: {sid}")

@sio.event
def disconnect(sid):
    print(f"🔌 WS client disconnected: {sid}")


# ── Global metrics state ──────────────────────────────────────────────────────

LATEST_DATA = {
    "seq":              0,
    "is_speech":        False,
    "frame_count":      0,
    "speech_count":     0,
    "silence_count":    0,
    "speech_ratio":     0.0,
    "last_updated":     0.0,
    "connected":        False,
    "rtp_active":       False,
    "last_state":       "silence",
    "avg_trt":          0.0,
    "fps":              0.0,
    "raw_energy":       0.0,
    "denoised_energy":  0.0,
    "snr_db":           0.0,
    "speech_start_count": 0,
    "speech_end_count":   0,
    "active_calls":     0,
    "server_ts":        0.0,
}

_trt_sum:   float = 0.0
_trt_count: int   = 0
_frame_timestamps: deque = deque(maxlen=500)
_data_lock = threading.Lock()


# ── SocketEmitter ─────────────────────────────────────────────────────────────

class SocketEmitter:
    """Writes per-frame results into LATEST_DATA and emits via Socket.IO."""

    def __init__(self, sio_instance, call_id: str):
        self.sio     = sio_instance
        self.call_id = call_id

    def send(self, seq: int, pcm16: np.ndarray, is_speech: bool,
             trt_ms: float = 0.0,
             raw_energy: float = 0.0, denoised_energy: float = 0.0,
             snr_db: float = 0.0,
             speech_event: str = ""):

        global _trt_sum, _trt_count

        now = time.time()

        payload = {
            "seq":             seq,
            "data":            base64.b64encode(pcm16.tobytes()).decode(),
            "is_speech":       bool(is_speech),
            "call_id":         self.call_id,
            "raw_energy":      float(raw_energy),
            "denoised_energy": float(denoised_energy),
            "snr_db":          float(snr_db),
            "speech_event":    speech_event,
        }

        with _data_lock:
            LATEST_DATA["seq"]          = seq
            LATEST_DATA["is_speech"]    = bool(is_speech)
            LATEST_DATA["frame_count"] += 1

            if is_speech:
                LATEST_DATA["speech_count"] += 1
            else:
                LATEST_DATA["silence_count"] += 1

            total = LATEST_DATA["frame_count"]
            LATEST_DATA["speech_ratio"] = (
                (LATEST_DATA["speech_count"] / total) * 100.0
                if total > 0 else 0.0
            )
            LATEST_DATA["last_updated"]    = now
            LATEST_DATA["rtp_active"]      = True
            LATEST_DATA["last_state"]      = "speech" if is_speech else "silence"
            LATEST_DATA["raw_energy"]      = raw_energy
            LATEST_DATA["denoised_energy"] = denoised_energy
            LATEST_DATA["snr_db"]          = snr_db
            LATEST_DATA["server_ts"]       = now

            if speech_event == "speech_start":
                LATEST_DATA["speech_start_count"] += 1
            elif speech_event == "speech_end":
                LATEST_DATA["speech_end_count"] += 1

            if trt_ms > 0:
                _trt_sum   += trt_ms
                _trt_count += 1
                LATEST_DATA["avg_trt"] = _trt_sum / _trt_count if _trt_count > 0 else 0.0

            _frame_timestamps.append(now)
            cutoff = now - 1.0
            LATEST_DATA["fps"] = float(
                sum(1 for t in _frame_timestamps if t >= cutoff)
            )

            payload.update({
                "total_frames":   LATEST_DATA["frame_count"],
                "speech_frames":  LATEST_DATA["speech_count"],
                "silence_frames": LATEST_DATA["silence_count"],
                "speech_ratio":   LATEST_DATA["speech_ratio"],
                "avg_latency":    LATEST_DATA.get("avg_trt", 0.0),
                "fps":            LATEST_DATA.get("fps", 0.0),
                "speech_start":   LATEST_DATA["speech_start_count"],
                "speech_end":     LATEST_DATA["speech_end_count"],
                "active_calls":   LATEST_DATA.get("active_calls", 1),
                "timestamp":      now,
            })

        try:
            self.sio.emit("processedAudio", payload)
        except Exception as e:
            print(f"⚠️ Socket emit error: {e}")


# ── Flask endpoints ───────────────────────────────────────────────────────────

@app.route("/latest")
def latest():
    with _data_lock:
        data = dict(LATEST_DATA)
    data["active_calls"] = len(CALL_SESSIONS)
    return jsonify(data)


@app.route("/health")
def health():
    return jsonify({
        "status":       "ok",
        "rtp_active":   LATEST_DATA["rtp_active"],
        "active_calls": len(CALL_SESSIONS),
    }), 200


@app.route("/reset")
def reset_endpoint():
    global _trt_sum, _trt_count
    _trt_sum   = 0.0
    _trt_count = 0
    _frame_timestamps.clear()
    with _data_lock:
        LATEST_DATA.update({
            "seq": 0, "is_speech": False,
            "frame_count": 0, "speech_count": 0,
            "silence_count": 0, "speech_ratio": 0.0,
            "last_updated": time.time(), "rtp_active": False,
            "last_state": "silence", "avg_trt": 0.0, "fps": 0.0,
            "raw_energy": 0.0, "denoised_energy": 0.0, "snr_db": 0.0,
            "speech_start_count": 0, "speech_end_count": 0,
            "server_ts": time.time(),
        })
    return jsonify({"status": "reset"})


@app.route("/clear_audio", methods=["POST"])
def clear_audio():
    global _trt_sum, _trt_count

    with _data_lock:
        LATEST_DATA.update({
            "seq": 0,
            "frame_count": 0,
            "speech_count": 0,
            "silence_count": 0,
            "speech_ratio": 0.0,
            "avg_trt": 0.0,
            "fps": 0.0,
            "speech_start_count": 0,
            "speech_end_count": 0,
        })
        _trt_sum = 0.0
        _trt_count = 0
        _frame_timestamps.clear()

    sio.emit("audioCleared", {"status": "ok"})
    return jsonify({"status": "cleared"})


@app.route("/calls")
def calls_endpoint():
    result = []
    for cid, sess in CALL_SESSIONS.items():
        result.append({
            "call_id":    cid,
            "state":      sess.get("state", "unknown"),
            "started_at": sess.get("started_at", 0),
        })
    return jsonify(result)

@app.route("/history")
def history():
    return jsonify(get_recent_calls(10))


@app.route("/call/<call_id>")
def call_detail(call_id):
    result = get_call(call_id)
    if not result:
        return jsonify({"error": "not found"}), 404
    return jsonify(result)


# ── Heartbeat ─────────────────────────────────────────────────────────────────

def _heartbeat_loop():
    while True:
        time.sleep(1)
        with _data_lock:
            LATEST_DATA["server_ts"] = time.time()

threading.Thread(target=_heartbeat_loop, daemon=True).start()


# ── Constants ─────────────────────────────────────────────────────────────────

PCMU_SAMPLE_RATE   = 8_000
TARGET_SAMPLE_RATE = 16_000
FRAME_MS           = 30
FRAME_SAMPLES_16K  = int(TARGET_SAMPLE_RATE * FRAME_MS / 1000)
RESAMPLE_RATIO_UP  = TARGET_SAMPLE_RATE / PCMU_SAMPLE_RATE
RESAMPLE_RATIO_DN  = PCMU_SAMPLE_RATE  / TARGET_SAMPLE_RATE
RESAMPLE_CONVERTER = "sinc_fastest"
RTP_PAYLOAD_TYPE   = 0

_RTP_PORT_POOL = list(range(7000, 7200, 10))
_port_lock     = threading.Lock()
_ports_in_use: set = set()

CALL_SESSIONS: dict = {}   # call_id → session dict
_sessions_lock = threading.Lock()

# ── Per-call transcript store ─────────────────────────────────────────────────
# Keyed by call_id; each value is a list of transcript strings.
# Written by STTManager._run() after each speech_end.
# Read by LLMAnalyser._run() after BYE.
# Protected by _transcripts_lock.
CALL_TRANSCRIPTS: dict = {}   # call_id → list[str]
_transcripts_lock = threading.Lock()


def _alloc_rtp_port() -> int:
    with _port_lock:
        for p in _RTP_PORT_POOL:
            if p not in _ports_in_use:
                _ports_in_use.add(p)
                return p
    raise RuntimeError("No free RTP ports in pool")


def _free_rtp_port(port: int):
    with _port_lock:
        _ports_in_use.discard(port)


# ── RTP Sender ────────────────────────────────────────────────────────────────

class RTPSender:
    def __init__(self, dest_ip: str, dest_port: int, src_port: int = 5074):
        self.dest  = (dest_ip, dest_port)
        self.sock  = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", src_port))

        self._ssrc    = random.randint(0, 0xFFFFFFFF)
        self._seq     = random.randint(0, 0xFFFF)
        self._ts      = random.randint(0, 0xFFFFFFFF)
        self._ts_step = int(PCMU_SAMPLE_RATE * FRAME_MS / 1000)

    def send(self, pcm16_at_16k: np.ndarray):
        pcm8 = samplerate.resample(pcm16_at_16k, RESAMPLE_RATIO_DN, RESAMPLE_CONVERTER)
        pcm8 = np.clip(pcm8, -32768, 32767).astype(np.int16)
        ulaw_bytes = audioop.lin2ulaw(pcm8.tobytes(), 2)

        header = struct.pack(
            "!BBHII",
            0x80, RTP_PAYLOAD_TYPE,
            self._seq & 0xFFFF,
            self._ts  & 0xFFFFFFFF,
            self._ssrc,
        )
        self._seq += 1
        self._ts  += self._ts_step

        try:
            self.sock.sendto(header + ulaw_bytes, self.dest)
        except Exception as exc:
            print(f"⚠️  RTP send error: {exc}")

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


# ── RTP Jitter Buffer ─────────────────────────────────────────────────────────

class JitterBuffer:
    MIN_PACKETS = 5
    MAX_PACKETS = 20

    def __init__(self):
        self._heap: list           = []
        self._expected_seq: int | None = None

    def push(self, seq: int, payload: bytes):
        if self._expected_seq is not None and seq < self._expected_seq:
            return
        if len(self._heap) < self.MAX_PACKETS:
            heapq.heappush(self._heap, (seq, payload))

    def pop_ready(self) -> list[tuple[int, bytes]]:
        if len(self._heap) < self.MIN_PACKETS:
            return []

        ready = []
        while self._heap:
            seq, payload = heapq.heappop(self._heap)
            if self._expected_seq is None:
                self._expected_seq = seq
            if seq < self._expected_seq:
                continue
            ready.append((seq, payload))
            self._expected_seq = seq + 1

        return ready

    def clear(self):
        self._heap.clear()
        self._expected_seq = None


# ── RTP Receiver ──────────────────────────────────────────────────────────────

class RTPReceiver:
    def __init__(self, port: int, handler: "DenoiseVADHandler",
                 metrics: "MetricsLogger", sender: "RTPSender",
                 call_id: str):
        self.port    = port
        self.handler = handler
        self.metrics = metrics
        self.sender  = sender
        self.call_id = call_id

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", self.port))
        self.sock.settimeout(1.0)

        self.running    = False
        self._seq       = 0
        self._buf       = np.zeros(0, dtype=np.int16)
        self._pkt_count = 0
        self.emitter: SocketEmitter | None = None

        self._jitter        = JitterBuffer()
        self._speech_buffer = []
        self._speech_history = deque(maxlen=5)

    def _send_dummy_rtp(self, addr):
        client_ip, client_port = addr
        header = struct.pack(
            "!BBHII",
            0x80, RTP_PAYLOAD_TYPE,
            self._seq & 0xFFFF,
            self._seq * 160,
            0,
        )
        payload = b'\xff' * 160
        try:
            self.sock.sendto(header + payload, (client_ip, client_port))
        except Exception as e:
            print(f"⚠️ Dummy RTP send error: {e}")

    def listen(self):
        print(f"👂 RTP Listener active on UDP port {self.port} (call: {self.call_id})")
        self.running = True

        while self.running:
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except Exception as exc:
                print(f"⚠️  RTP recv error [{self.call_id}]: {exc}")
                break

            if len(data) <= 12:
                continue

            self._pkt_count += 1
            if self._pkt_count <= 5 or self._pkt_count % 50 == 0:
                print(f"📦 RTP #{self._pkt_count} [{self.call_id}] from {addr} ({len(data)}B)")

            self._send_dummy_rtp(addr)

            t_recv = time.perf_counter()

            rtp_seq     = struct.unpack("!H", data[2:4])[0]
            rtp_payload = data[12:]

            self._jitter.push(rtp_seq, rtp_payload)
            ready = self._jitter.pop_ready()

            for pkt_seq, payload in ready:
                self._process_payload(payload, pkt_seq, t_recv)

        print(f"🛑 RTP Listener stopped [{self.call_id}].")

    def _process_payload(self, rtp_payload: bytes, pkt_seq: int, t_recv: float):
        pcm8_bytes = audioop.ulaw2lin(rtp_payload, 2)
        pcm8 = np.frombuffer(pcm8_bytes, dtype=np.int16)

        pcm16 = samplerate.resample(pcm8, RESAMPLE_RATIO_UP, RESAMPLE_CONVERTER)
        pcm16 = np.clip(pcm16, -32768, 32767).astype(np.int16)

        self._buf = np.concatenate((self._buf, pcm16))

        while len(self._buf) >= FRAME_SAMPLES_16K:
            frame     = self._buf[:FRAME_SAMPLES_16K]
            self._buf = self._buf[FRAME_SAMPLES_16K:]

            denoised, is_speech, snr_info, speech_event = self.handler.handle_raw_frame(
                self._seq, frame, t_recv, self.metrics
            )

            if is_speech:
                self._speech_buffer.append(denoised.copy())

            trt_ms = (time.perf_counter() - t_recv) * 1000.0

            self.sender.send(denoised)

            self._speech_history.append(1 if is_speech else 0)
            smoothed_speech = sum(self._speech_history) >= 3

            if self.emitter:
                self.emitter.send(
                    self._seq,
                    denoised,
                    smoothed_speech,
                    trt_ms=trt_ms,
                    raw_energy=snr_info.get("raw_energy", 0.0),
                    denoised_energy=snr_info.get("denoised_energy", 0.0),
                    snr_db=snr_info.get("snr_db", 0.0),
                    speech_event=speech_event,
                )

                try:
                    sio.emit("heartbeat", {"ts": time.time()})
                except Exception:
                    pass

            if smoothed_speech:
                send_to_ai(self._seq, denoised, self.call_id)

            if speech_event == "speech_end" and self._speech_buffer:
                segment = np.concatenate(self._speech_buffer)
                self._speech_buffer.clear()
                _stt_manager.submit(segment, self.call_id)

            self._seq += 1

    def close(self):
        self.running = False
        try:
            self.sock.close()
        except Exception:
            pass


# ── STT Manager ───────────────────────────────────────────────────────────────
#
# Design goals (unchanged):
#   1. Never block the RTP thread
#   2. Single worker — no concurrent Whisper runs
#   3. Cooldown between submissions
#   4. Lazy model load (post monkey_patch)
#   5. Transcript is appended to CALL_TRANSCRIPTS after each successful run

class STTManager:
    MIN_SAMPLES   = 16_000
    COOLDOWN_SECS = 2.0

    def __init__(self):
        self._pool        = ThreadPoolExecutor(max_workers=1)
        self._model       = None
        self._model_lock  = threading.Lock()
        self._last_run_ts = 0.0
        self._busy        = False

    def _load_model(self):
        if self._model is not None:
            return self._model
        with self._model_lock:
            if self._model is None:
                from faster_whisper import WhisperModel
                print("📦 [STT] Loading Whisper tiny (CPU)…")
                self._model = WhisperModel(
                    "tiny",
                    device="cpu",
                    compute_type="int8",
                    local_files_only=True,
                )
                print("✅ [STT] Whisper model ready")
        return self._model

    def submit(self, audio_np: np.ndarray, call_id: str) -> bool:
        now = time.monotonic()

        if len(audio_np) < self.MIN_SAMPLES:
            print(f"⏭️  [STT] Segment too short ({len(audio_np)} samples) — skipped")
            return False

        if (now - self._last_run_ts) < self.COOLDOWN_SECS:
            print(f"⏳ [STT] Cooldown active — skipping speech_end for call {call_id}")
            return False

        if self._busy:
            print(f"⏭️  [STT] Previous transcription still running — skipping")
            return False

        audio_copy        = audio_np.copy()
        self._last_run_ts = now
        self._busy        = True

        self._pool.submit(self._run, audio_copy, call_id)
        return True

    def _run(self, audio_np: np.ndarray, call_id: str):
        """Runs in the ThreadPoolExecutor worker — never in the RTP thread."""
        try:
            duration_sec = len(audio_np) / 16000.0

            if duration_sec < 1.0:
                return

            print(f"🎙️  [STT] Transcribing {duration_sec:.2f}s for call [{call_id}]…")
            t0 = time.perf_counter()

            model = self._load_model()

            audio_f32 = audio_np.astype(np.float32) / 32768.0

            segments, _ = model.transcribe(
                audio_f32,
                language="en",
                beam_size=1,
                best_of=1,
                temperature=0.0,
                vad_filter=False,
            )

            text    = " ".join(seg.text.strip() for seg in segments).strip()
            elapsed = (time.perf_counter() - t0) * 1000

            if text:
                print(f"\n🧠 [STT] [{call_id}] ({elapsed:.0f} ms)\n➡️  {text}\n")

                # ── Accumulate into per-call transcript store ─────────────────
                with _transcripts_lock:
                    if call_id not in CALL_TRANSCRIPTS:
                        CALL_TRANSCRIPTS[call_id] = []
                    CALL_TRANSCRIPTS[call_id].append(text)

                # ── Emit live segment to frontend ─────────────────────────────
                try:
                    sio.emit("transcript", {
                        "call_id":   call_id,
                        "text":      text,
                        "timestamp": time.time(),
                    })
                except Exception as e:
                    print(f"⚠️  [STT] Emit failed: {e}")

            else:
                print(f"⚠️  [STT] [{call_id}] Empty transcription ({elapsed:.0f} ms)")

        except Exception as exc:
            print(f"⚠️  [STT] Error for call [{call_id}]: {exc}")

        finally:
            self._busy = False

    def shutdown(self):
        self._pool.shutdown(wait=True)


# ── Global STT manager ────────────────────────────────────────────────────────
_stt_manager = STTManager()


# ── LLM Analyser ─────────────────────────────────────────────────────────────
#
# Runs ONLY after call ends (triggered by BYE teardown).
# Uses a separate single-worker ThreadPoolExecutor so it NEVER competes with STT.
# Calls Ollama via requests.post — no external API dependencies.
# Emits "llm_report" on success, "llm_error" on failure.

OLLAMA_URL   = "http://localhost:11434/api/chat"
OLLAMA_MODEL = "llama3.1:8b"

# Strict JSON schema Ollama must return
_LLM_SCHEMA = """{
  "summary":          "<2-3 sentence factual summary of the conversation>",
  "intent":           "<primary intent of the caller, e.g. complaint, inquiry, support request>",
  "sentiment":        "<one of: positive | neutral | negative | mixed>",
  "risk_level":       "<one of: low | medium | high>",
  "suggested_action": "<actionable next step for the agent or automated system>"
}"""

_LLM_SYSTEM_PROMPT = (
    "You are an AI system that analyzes call transcripts.\n\n"

    "Your task is to extract structured information from the transcript.\n\n"

    "Return ONLY a valid JSON object with EXACTLY the following fields:\n"
    "{\n"
    "  \"summary\": \"short 1-2 sentence summary\",\n"
    "  \"intent\": \"primary user intent (short phrase)\",\n"
    "  \"sentiment\": \"positive | neutral | negative\",\n"
    "  \"risk_level\": \"low | medium | high\",\n"
    "  \"suggested_action\": \"clear next step\"\n"
    "}\n\n"

    "STRICT RULES:\n"
    "- Do NOT include explanations\n"
    "- Do NOT include markdown\n"
    "- Do NOT include text before or after JSON\n"
    "- Output must be valid JSON only\n"
)


class LLMAnalyser:
    """
    Post-call LLM analysis using Ollama (llama3.1).

    Workflow:
      1. Called from _handle_bye after session teardown.
      2. Snapshots the full transcript for the call_id.
      3. Submits to Ollama in a background thread (separate pool from STT).
      4. Parses the strict JSON response.
      5. Emits "llm_report" via Socket.IO.
      6. Cleans up the transcript store for the finished call.
    """

    def __init__(self):
        # Separate pool from STT — LLM jobs don't block STT queue
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="llm")

    def analyse_call(self, call_id: str):
        """
        Non-blocking entry point — called from BYE handler.
        Snapshots the transcript immediately (still in BYE thread),
        then hands off to the worker pool.
        """

        start_ts = time.perf_counter()

        # ── Snapshot transcript safely ─────────────────────────────
        with _transcripts_lock:
            segments = list(CALL_TRANSCRIPTS.get(call_id, []))

        segment_count = len(segments)
        full_text = " ".join(segments).strip()

        # ── Handle empty transcript ────────────────────────────────
        if not full_text:
            print(f"ℹ️  [LLM] No transcript for call [{call_id}] — skipping analysis")

            processing_ms = (time.perf_counter() - start_ts) * 1000.0

            try:
                sio.emit("llm_report", {
                    "call_id": call_id,
                    "report":  None,
                    "error":   "No transcript captured for this call.",
                    "meta": {
                        "length": 0,
                        "segments": segment_count,
                        "processing_ms": processing_ms,
                    },
                })
            except Exception as e:
                print(f"⚠️ [LLM] Emit failed (no transcript): {e}")

            return

        # ── Normal path ────────────────────────────────────────────
        print(
            f"📋 [LLM] Queuing post-call analysis for [{call_id}] "
            f"({len(full_text)} chars, {segment_count} segments)"
        )

        try:
            self._pool.submit(self._run, call_id, full_text)
        except Exception as e:
            print(f"⚠️ [LLM] Failed to submit job for [{call_id}]: {e}")

            processing_ms = (time.perf_counter() - start_ts) * 1000.0

            # Emit failure so UI doesn't hang
            try:
                sio.emit("llm_report", {
                    "call_id": call_id,
                    "report": None,
                    "error":  "Failed to start LLM analysis.",
                    "meta": {
                        "length": len(full_text),
                        "segments": segment_count,
                        "processing_ms": processing_ms,
                    },
                })
            except Exception:
                pass

    def _run(self, call_id: str, full_text: str):
        """Executed in the LLM worker thread — never in RTP or STT threads."""
        t0 = time.perf_counter()
        print(f"🤖 [LLM] Analysing call [{call_id}]…")

        try:
            # ── Build Ollama request ──────────────────────────────────────────
            payload = {
                "model":  OLLAMA_MODEL,
                "stream": False,
                "messages": [
                    {"role": "system", "content": _LLM_SYSTEM_PROMPT},
                    {"role": "user",   "content": f"TRANSCRIPT:\n{full_text}"},
                ],
                "options": {
                    "temperature": 0.1,   # low temp → consistent, deterministic JSON
                    "num_predict": 256,   # enough for the JSON response
                },
            }

            response = requests.post(
                OLLAMA_URL,
                json=payload,
                timeout=120,   # Ollama on CPU can take a while; generous timeout
            )
            response.raise_for_status()

            raw = response.json()
            # Ollama non-stream response: {"message": {"role": "assistant", "content": "..."}}
            content = raw.get("message", {}).get("content", "").strip()

            if not content:
                raise ValueError("Ollama returned empty content")

            # ── Strip any accidental markdown fences ─────────────────────────
            # Even with the system prompt, some models wrap JSON in ```json ... ```
            content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.MULTILINE)
            content = re.sub(r"\s*```$", "", content, flags=re.MULTILINE)
            content = content.strip()

            # ── Parse JSON ───────────────────────────────────────────────────
            try:
                report = json.loads(content)
            except:
                report = {
                    "summary": content[:200],
                    "intent": "unknown",
                    "sentiment": "neutral",
                    "risk_level": "low",
                    "suggested_action": "manual review"
                }

            # ── Validate required keys ────────────────────────────────────────
            required = {"summary", "intent", "sentiment", "risk_level", "suggested_action"}
            missing  = required - set(report.keys())
            if missing:
                raise ValueError(f"LLM response missing keys: {missing}")

            # ── Normalise values to known enums (safe defaults) ───────────────
            valid_sentiments = {"positive", "neutral", "negative", "mixed"}
            valid_risks      = {"low", "medium", "high"}
            if report["sentiment"].lower() not in valid_sentiments:
                report["sentiment"] = "neutral"
            if report["risk_level"].lower() not in valid_risks:
                report["risk_level"] = "low"

            elapsed_ms = (time.perf_counter() - t0) * 1000

            print(f"✅ [LLM] [{call_id}] complete in {elapsed_ms:.0f} ms")
            print(f"   sentiment={report['sentiment']}  risk={report['risk_level']}")
            print(f"   summary: {report['summary'][:80]}…")

            # ── Emit result to all connected frontend clients ─────────────────
            emit_payload = {
                "call_id": call_id,
                "report":  report,
                "error":   None,
                "meta": {
                    "length":        len(full_text),
                    "segments":      0,           # populated below after lock
                    "processing_ms": round(elapsed_ms, 1),
                },
            }

            # ── SAVE TO DATABASE ─────────────────────────────
            save_call(
                call_id=call_id,
                transcript=full_text,
                report=report,
                meta={
                    "length": len(full_text),
                    "processing_ms": round(elapsed_ms, 1),
                }
            )

            # Grab segment count safely
            with _transcripts_lock:
                emit_payload["meta"]["segments"] = len(CALL_TRANSCRIPTS.get(call_id, []))

            try:
                sio.emit("llm_report", emit_payload)
            except Exception as e:
                print(f"⚠️  [LLM] Emit failed: {e}")

        except requests.exceptions.ConnectionError:
            err = "Ollama is not running. Start it with: ollama serve"
            print(f"❌ [LLM] [{call_id}] {err}")
            self._emit_error(call_id, err)

        except requests.exceptions.Timeout:
            err = f"Ollama request timed out after 120 s for call [{call_id}]"
            print(f"❌ [LLM] {err}")
            self._emit_error(call_id, err)

        except json.JSONDecodeError as e:
            err = f"LLM response was not valid JSON: {e}"
            print(f"❌ [LLM] [{call_id}] {err}")
            self._emit_error(call_id, err)

        except ValueError as e:
            err = str(e)
            print(f"❌ [LLM] [{call_id}] {err}")
            self._emit_error(call_id, err)

        except Exception as exc:
            err = f"Unexpected LLM error: {exc}"
            print(f"❌ [LLM] [{call_id}] {err}")
            self._emit_error(call_id, err)

        finally:
            # ── Clean up transcript store for this call ───────────────────────
            # Done here (after LLM finishes) so the full transcript is available
            # for the entire analysis window, even if it takes a while.
            with _transcripts_lock:
                CALL_TRANSCRIPTS.pop(call_id, None)

    def _emit_error(self, call_id: str, error_msg: str):
        try:
            with _transcripts_lock:
                segments = len(CALL_TRANSCRIPTS.get(call_id, []))

            sio.emit("llm_report", {
                "call_id": call_id,
                "report": None,
                "error": error_msg,
                "meta": {
                    "length": 0,
                    "segments": segments,
                    "processing_ms": 0
                }
            })
        except Exception as e:
            print(f"⚠️  [LLM] Error emit failed: {e}")

    def shutdown(self):
        self._pool.shutdown(wait=False)   # don't block server exit on slow LLM


# ── Global LLM analyser (singleton) ──────────────────────────────────────────
_llm_analyser = LLMAnalyser()


# ── SIP header helpers ────────────────────────────────────────────────────────

def parse_sip_headers(msg: str) -> dict:
    headers = {}
    lines = msg.split("\r\n")
    compact_map = {"v": "via", "f": "from", "t": "to", "i": "call-id"}

    for line in lines[1:]:
        if not line or ":" not in line:
            continue
        name, _, value = line.partition(":")
        name  = compact_map.get(name.strip().lower(), name.strip().lower())
        value = value.strip()
        if name in ("via", "from", "to", "call-id", "cseq") and name not in headers:
            headers[name] = value

    return headers


def extract_rtp_port(msg: str) -> int:
    for line in msg.split("\r\n"):
        if line.startswith("m=audio"):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    return int(parts[1])
                except ValueError:
                    pass
    return 5070


def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def fix_via_rport(via_value: str, src_addr: tuple) -> str:
    src_ip, src_port = src_addr
    via_value = re.sub(r";rport(?!=)", f";rport={src_port}", via_value)
    if "received=" not in via_value:
        via_value = via_value.replace(";branch=", f";received={src_ip};branch=")
    return via_value


def build_100_trying(headers: dict, server_ip: str, src_addr: tuple) -> str:
    via = fix_via_rport(headers["via"], src_addr)
    return (
        "SIP/2.0 100 Trying\r\n"
        f"Via: {via}\r\n"
        f"From: {headers['from']}\r\n"
        f"To: {headers['to']}\r\n"
        f"Call-ID: {headers['call-id']}\r\n"
        f"CSeq: {headers['cseq']}\r\n"
        "Content-Length: 0\r\n"
        "\r\n"
    )


def build_200_ok(headers: dict, server_ip: str, server_port: int,
                 rtp_port: int, src_addr: tuple) -> str:
    via      = fix_via_rport(headers["via"], src_addr)
    to_value = headers["to"]
    if "tag=" not in to_value:
        to_value += f";tag={random.randint(100000, 999999)}"

    sdp_body = (
        f"v=0\r\n"
        f"o=- 0 0 IN IP4 {server_ip}\r\n"
        f"s=Python SIP Server\r\n"
        f"c=IN IP4 {server_ip}\r\n"
        f"t=0 0\r\n"
        f"m=audio {rtp_port} RTP/AVP 0\r\n"
        f"a=rtpmap:0 PCMU/8000\r\n"
    )
    content_length = len(sdp_body.encode("utf-8"))

    return (
        "SIP/2.0 200 OK\r\n"
        f"Via: {via}\r\n"
        f"From: {headers['from']}\r\n"
        f"To: {to_value}\r\n"
        f"Call-ID: {headers['call-id']}\r\n"
        f"CSeq: {headers['cseq']}\r\n"
        f"Contact: <sip:{server_ip}:{server_port}>\r\n"
        "Content-Type: application/sdp\r\n"
        f"Content-Length: {content_length}\r\n"
        "\r\n"
        f"{sdp_body}"
    )


# ── SIP Signaling Server ──────────────────────────────────────────────────────

class SIPSignalingServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 5060):
        self.host = host
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))

        self._shared_metrics = MetricsLogger()
        self._local_ip       = get_local_ip()
        print(f"🌐 Local IP: {self._local_ip}")

    def start(self):
        print(f"☎️  SIP Listener on {self.host}:{self.port}")
        while True:
            try:
                data, addr = self.sock.recvfrom(4096)
                msg        = data.decode("utf-8", errors="replace").strip()
                if not msg:
                    continue

                first_line = msg.split("\r\n")[0]
                print(f"📨 SIP {first_line} from {addr}")

                if first_line.startswith("INVITE"):
                    self._handle_invite(msg, addr)
                elif first_line.startswith("ACK"):
                    self._handle_ack(msg, addr)
                elif first_line.startswith("BYE"):
                    self._handle_bye(msg, addr)
                elif first_line.startswith("REGISTER"):
                    print("ℹ️  REGISTER ignored")
                elif first_line.startswith("OPTIONS"):
                    print("ℹ️  OPTIONS ignored")
                else:
                    print(f"⚠️  Unknown SIP message: {first_line}")

            except Exception as exc:
                print(f"⚠️  SIP loop error: {exc}")
                continue

    # ── INVITE ────────────────────────────────────────────────────────────────

    def _handle_invite(self, msg: str, addr):
        headers  = parse_sip_headers(msg)
        missing  = [h for h in ("via", "from", "to", "call-id", "cseq") if h not in headers]
        if missing:
            print(f"⚠️  INVITE missing headers {missing} — dropping")
            return

        call_id = headers["call-id"]

        with _sessions_lock:
            session = CALL_SESSIONS.get(call_id)

        if session:
            state = session.get("state")
            if state == "active":
                print(f"⚠️  Re-INVITE on active call {call_id} — ignored")
                return
            if state == "ringing":
                print(f"🔁 INVITE retransmit [{call_id}] — resending 200 OK")
                resp = build_200_ok(
                    session["headers"], self._local_ip, self.port,
                    session["server_rtp_port"], addr,
                )
                self.sock.sendto(resp.encode(), addr)
                return

        print(f"📞 Fresh INVITE [{call_id}]")

        trying = build_100_trying(headers, self._local_ip, addr)
        self.sock.sendto(trying.encode(), addr)
        print("📤 100 Trying sent")

        rtp_port    = extract_rtp_port(msg)
        rtp_ip      = addr[0]
        server_rtp  = _alloc_rtp_port()
        sender_port = server_rtp + 2

        rtp_sender   = RTPSender(dest_ip=rtp_ip, dest_port=rtp_port, src_port=sender_port)
        handler      = DenoiseVADHandler(call_id)
        rtp_receiver = RTPReceiver(
            port=server_rtp, handler=handler,
            metrics=self._shared_metrics, sender=rtp_sender,
            call_id=call_id,
        )
        emitter = SocketEmitter(sio, call_id)
        rtp_receiver.emitter = emitter

        # ── Initialise per-call transcript store ──────────────────────────────
        with _transcripts_lock:
            CALL_TRANSCRIPTS[call_id] = []

        with _sessions_lock:
            CALL_SESSIONS[call_id] = {
                "state":           "ringing",
                "headers":         headers,
                "addr":            addr,
                "rtp_sender":      rtp_sender,
                "rtp_receiver":    rtp_receiver,
                "handler":         handler,
                "server_rtp_port": server_rtp,
                "started_at":      time.time(),
            }

        with _data_lock:
            LATEST_DATA["active_calls"] = len(CALL_SESSIONS)

        resp = build_200_ok(headers, self._local_ip, self.port, server_rtp, addr)
        self.sock.sendto(resp.encode(), addr)
        print(f"📤 200 OK sent [{call_id}] — waiting for ACK")

    # ── ACK ───────────────────────────────────────────────────────────────────

    def _handle_ack(self, msg: str, addr):
        headers = parse_sip_headers(msg)
        call_id = headers.get("call-id")

        if not call_id:
            print("⚠️  ACK missing Call-ID — ignored")
            return

        with _sessions_lock:
            session = CALL_SESSIONS.get(call_id)

        if not session or session["state"] != "ringing":
            print(f"ℹ️  ACK ignored — call {call_id} state: {session and session['state']}")
            return

        print(f"✅ ACK [{call_id}] — starting RTP")
        session["state"] = "active"

        threading.Thread(
            target=session["rtp_receiver"].listen,
            daemon=True,
        ).start()

        print(f"🎙️  RTP live for call [{call_id}]")

    # ── BYE ───────────────────────────────────────────────────────────────────

    def _handle_bye(self, msg: str, addr):
        headers = parse_sip_headers(msg)
        call_id = headers.get("call-id", "")
        print(f"📴 BYE [{call_id}]")

        # ── Send 200 OK for BYE ───────────────────────────────────────────────
        if "via" in headers:
            response = (
                "SIP/2.0 200 OK\r\n"
                f"Via: {headers['via']}\r\n"
                f"From: {headers['from']}\r\n"
                f"To: {headers['to']}\r\n"
                f"Call-ID: {call_id}\r\n"
                f"CSeq: {headers['cseq']}\r\n"
                "Content-Length: 0\r\n"
                "\r\n"
            )
            self.sock.sendto(response.encode(), addr)
            print(f"📤 200 OK for BYE [{call_id}]")

        # ── Teardown RTP + VAD resources ──────────────────────────────────────
        with _sessions_lock:
            session = CALL_SESSIONS.pop(call_id, None)

        if session:
            rr = session.get("rtp_receiver")
            rs = session.get("rtp_sender")
            if rr:
                rr.running = False
                rr._speech_buffer.clear()
                _free_rtp_port(rr.port)
            if rs:
                rs.close()
            h = session.get("handler")
            if h:
                h.teardown()

        with _data_lock:
            LATEST_DATA["active_calls"] = len(CALL_SESSIONS)
            if not CALL_SESSIONS:
                LATEST_DATA["rtp_active"] = False

        print(f"📭 Call [{call_id}] torn down — ready for next INVITE")

        # ── Notify frontend: call ended, LLM starting ─────────────────────────
        # This fires BEFORE the LLM job is queued so the UI can show the spinner
        # immediately. The LLM job is then queued asynchronously.
        try:
            sio.emit("call_ended", {
                "call_id":    call_id,
                "ended_at":   time.time(),
                "llm_queued": True,
            })
        except Exception as e:
            print(f"⚠️  call_ended emit failed: {e}")

        # ── Queue post-call LLM analysis (non-blocking) ───────────────────────
        # _llm_analyser.analyse_call() returns immediately; the actual Ollama
        # request runs in the LLM ThreadPoolExecutor worker.
        # The STT pool is NOT involved — completely separate executor.
        _llm_analyser.analyse_call(call_id)


# ── Main entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    local_ip = get_local_ip()
    print(f"🌐 Server IP: {local_ip}")

    server = SIPSignalingServer(port=5060)
    threading.Thread(target=server.start, daemon=True).start()

    print("🌐 Starting Socket.IO/Flask server on port 5000…")
    print(f"   Dashboard URL: http://{local_ip}:5000")
    eventlet.wsgi.server(
        eventlet.listen(("0.0.0.0", 5000)),
        app,
        log_output=False,
    )
