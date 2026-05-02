"""
sip_server.py  –  Multi-call capable SIP/RTP processing server
Features added:
  - Multi-call handling via Call-ID session tracking
  - RTP jitter buffer with packet reordering (5–20 packets)
  - Health endpoint for ALB/NLB
  - Clean BYE teardown per call
  - SNR metrics exposed on /latest
  - Speech boundary events (speech_start / speech_end)
  - audioClear interrupt endpoint
  - Downstream AI client stub
  - Per-call heartbeat timestamps
  - Socket.IO emit per call
"""

import audioop
import heapq
import random
import re
import socket
import struct
import threading
import time
import base64
from collections import deque

import numpy as np
import samplerate

from flask import Flask, jsonify, request
import socketio

from denoiseVADHandler import DenoiseVADHandler
from metricsLogger import MetricsLogger
from ai_client import send_to_ai  # stub — safe import

# ── Socket.IO / Flask setup ───────────────────────────────────────────────────

sio = socketio.Server(cors_allowed_origins="*")
app = Flask(__name__)
app.wsgi_app = socketio.WSGIApp(sio, app.wsgi_app)


# ── Global polling state ──────────────────────────────────────────────────────
# Aggregated view across all active calls; per-call data also available.

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
    # SNR / denoise quality
    "raw_energy":       0.0,
    "denoised_energy":  0.0,
    "snr_db":           0.0,
    # Speech boundary events
    "speech_start_count": 0,
    "speech_end_count":   0,
    # Active calls count
    "active_calls":     0,
    # Heartbeat
    "server_ts":        0.0,
}

_trt_sum:   float = 0.0
_trt_count: int   = 0
_frame_timestamps: deque = deque(maxlen=500)
_data_lock = threading.Lock()


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

        payload = {
            "seq":             seq,
            "data":            base64.b64encode(pcm16.tobytes()).decode(),
            "is_speech":       is_speech,
            "call_id":         self.call_id,
            "raw_energy":      raw_energy,
            "denoised_energy": denoised_energy,
            "snr_db":          snr_db,
            "speech_event":    speech_event,
        }
        self.sio.emit("processedAudio", payload)

        now = time.time()
        with _data_lock:
            LATEST_DATA["seq"]           = seq
            LATEST_DATA["is_speech"]     = is_speech
            LATEST_DATA["frame_count"]  += 1
            if is_speech:
                LATEST_DATA["speech_count"] += 1
            else:
                LATEST_DATA["silence_count"] += 1

            total = LATEST_DATA["frame_count"]
            LATEST_DATA["speech_ratio"] = (
                LATEST_DATA["speech_count"] / total * 100 if total > 0 else 0.0
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
                LATEST_DATA["avg_trt"] = _trt_sum / _trt_count

            _frame_timestamps.append(now)
            cutoff = now - 1.0
            LATEST_DATA["fps"] = float(sum(1 for t in _frame_timestamps if t >= cutoff))


# ── Flask endpoints ───────────────────────────────────────────────────────────

@app.route("/latest")
def latest():
    """Polling endpoint for Streamlit."""
    with _data_lock:
        data = dict(LATEST_DATA)
    data["active_calls"] = len(CALL_SESSIONS)
    return jsonify(data)


@app.route("/health")
def health():
    """ALB / NLB liveness probe."""
    return jsonify({
        "status":       "ok",
        "rtp_active":   LATEST_DATA["rtp_active"],
        "active_calls": len(CALL_SESSIONS),
    }), 200


@app.route("/reset")
def reset_endpoint():
    """Reset counters – callable from Streamlit."""
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
    """
    audioClear interrupt — resets all active call handlers and counters.
    Called by Streamlit interrupt button.
    """
    call_id = request.json.get("call_id") if request.is_json else None
    if call_id and call_id in CALL_SESSIONS:
        session = CALL_SESSIONS[call_id]
        if session.get("handler"):
            session["handler"].reset()
    else:
        # Reset all
        for sess in CALL_SESSIONS.values():
            if sess.get("handler"):
                sess["handler"].reset()

    global _trt_sum, _trt_count
    _trt_sum   = 0.0
    _trt_count = 0
    _frame_timestamps.clear()
    with _data_lock:
        LATEST_DATA.update({
            "frame_count": 0, "speech_count": 0,
            "silence_count": 0, "speech_ratio": 0.0,
            "last_state": "silence", "avg_trt": 0.0, "fps": 0.0,
            "raw_energy": 0.0, "denoised_energy": 0.0, "snr_db": 0.0,
        })
    return jsonify({"status": "cleared"})


@app.route("/calls")
def calls_endpoint():
    """Return info on all active calls."""
    result = []
    for cid, sess in CALL_SESSIONS.items():
        result.append({
            "call_id":    cid,
            "state":      sess.get("state", "unknown"),
            "started_at": sess.get("started_at", 0),
        })
    return jsonify(result)


# ── Heartbeat background thread ───────────────────────────────────────────────

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

# Port pool for multi-call RTP (even ports 7000, 7010, 7020 …)
_RTP_PORT_POOL = list(range(7000, 7200, 10))
_port_lock     = threading.Lock()
_ports_in_use: set = set()

CALL_SESSIONS: dict = {}   # call_id → session dict
_sessions_lock = threading.Lock()


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
    """
    Small priority-queue based jitter buffer (5–20 packets).
    Packets are held until the buffer has MIN_PACKETS entries,
    then released in sequence-number order.
    Late packets (seq < expected) are dropped.
    """

    MIN_PACKETS = 5
    MAX_PACKETS = 20

    def __init__(self):
        self._heap: list      = []   # (seq, payload_bytes)
        self._expected_seq: int | None = None

    def push(self, seq: int, payload: bytes):
        if self._expected_seq is not None and seq < self._expected_seq:
            # Late / duplicate packet — drop
            return
        if len(self._heap) < self.MAX_PACKETS:
            heapq.heappush(self._heap, (seq, payload))

    def pop_ready(self) -> list[tuple[int, bytes]]:
        """
        Return a list of (seq, payload) packets that are ready to process.
        Packets are withheld until MIN_PACKETS are buffered.
        """
        if len(self._heap) < self.MIN_PACKETS:
            return []

        ready = []
        while self._heap:
            seq, payload = heapq.heappop(self._heap)
            if self._expected_seq is None:
                self._expected_seq = seq

            if seq < self._expected_seq:
                # Stale — discard
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

        self._jitter   = JitterBuffer()

    # ── Dummy RTP keep-alive ──────────────────────────────────────────────────

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

    # ── Main listen loop ──────────────────────────────────────────────────────

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

            # Keep-alive
            self._send_dummy_rtp(addr)

            t_recv = time.perf_counter()

            # Parse RTP sequence number
            rtp_seq = struct.unpack("!H", data[2:4])[0]
            rtp_payload = data[12:]

            # Push into jitter buffer
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

            trt_ms = (time.perf_counter() - t_recv) * 1000.0

            self.sender.send(denoised)

            if self.emitter:
                self.emitter.send(
                    self._seq, denoised, is_speech, trt_ms=trt_ms,
                    raw_energy=snr_info.get("raw_energy", 0.0),
                    denoised_energy=snr_info.get("denoised_energy", 0.0),
                    snr_db=snr_info.get("snr_db", 0.0),
                    speech_event=speech_event,
                )

            # Send to downstream AI (stub)
            if is_speech:
                send_to_ai(self._seq, denoised, self.call_id)

            self._seq += 1

    def close(self):
        self.running = False
        try:
            self.sock.close()
        except Exception:
            pass


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

        self._shared_metrics = MetricsLogger()   # one logger, shared across calls
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

        # 100 Trying
        trying = build_100_trying(headers, self._local_ip, addr)
        self.sock.sendto(trying.encode(), addr)
        print("📤 100 Trying sent")

        rtp_port     = extract_rtp_port(msg)
        rtp_ip       = addr[0]
        server_rtp   = _alloc_rtp_port()
        sender_port  = server_rtp + 2

        rtp_sender   = RTPSender(dest_ip=rtp_ip, dest_port=rtp_port, src_port=sender_port)
        handler      = DenoiseVADHandler(call_id)
        rtp_receiver = RTPReceiver(
            port=server_rtp, handler=handler,
            metrics=self._shared_metrics, sender=rtp_sender,
            call_id=call_id,
        )
        emitter = SocketEmitter(sio, call_id)
        rtp_receiver.emitter = emitter

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

        # Send 200 OK for BYE
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

        with _sessions_lock:
            session = CALL_SESSIONS.pop(call_id, None)

        if session:
            rr = session.get("rtp_receiver")
            rs = session.get("rtp_sender")
            if rr:
                rr.running = False
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


# ── Main entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    server = SIPSignalingServer(port=5060)
    threading.Thread(target=server.start, daemon=True).start()

    print("🌐 Starting Socket.IO/Flask server on port 5000…")
    import eventlet
    eventlet.wsgi.server(eventlet.listen(("0.0.0.0", 5000)), app)
