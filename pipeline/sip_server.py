"""
sip_server.py  –  Benjamin Vinod | Module 1
FIXED: Proper SIP 3-way handshake with ACK-triggered RTP start
"""

import audioop
import random
import re
import socket
import struct
import threading
import time
import base64

import numpy as np
import samplerate

from flask import Flask, jsonify
import socketio

from denoiseVADHandler import DenoiseVADHandler
from metricsLogger import MetricsLogger

# ── Socket.IO setup ───────────────────────────────────────────────────────────

sio = socketio.Server(cors_allowed_origins="*")
app = Flask(__name__)
app.wsgi_app = socketio.WSGIApp(sio, app.wsgi_app)


# ── Global polling state (written by SocketEmitter, read by /latest) ─────────

LATEST_DATA = {
    "seq":           0,
    "is_speech":     False,
    "frame_count":   0,
    "speech_count":  0,
    "silence_count": 0,
    "speech_ratio":  0.0,
    "last_updated":  0.0,       # epoch timestamp — frontend uses this for heartbeat
    "connected":     False,
    "rtp_active":    False,
    # ── NEW fields ────────────────────────────────────────────────────────────
    "last_state":    "silence", # "speech" or "silence" — current VAD state
    "avg_trt":       0.0,       # running average processing latency in ms
    "fps":           0.0,       # frames processed in the last 1-second window
}

# Internal accumulator for avg_trt (not exposed to frontend directly)
_trt_sum:   float = 0.0
_trt_count: int   = 0

# Internal deque for FPS sliding window — stores epoch timestamps of recent frames
from collections import deque as _deque
_frame_timestamps: _deque = _deque(maxlen=500)  # 500-frame cap on window


class SocketEmitter:
    def __init__(self, sio):
        self.sio = sio

    def send(self, seq, pcm16, is_speech, trt_ms: float = 0.0):
        # ── Emit via Socket.IO (kept for future use) ──────────────────────────
        payload = {
            "seq":       seq,
            "data":      base64.b64encode(pcm16.tobytes()).decode(),
            "is_speech": is_speech,
        }
        self.sio.emit("processedAudio", payload)

        # ── Write to global polling state ─────────────────────────────────────
        global _trt_sum, _trt_count

        now = time.time()

        LATEST_DATA["seq"]       = seq
        LATEST_DATA["is_speech"] = is_speech
        LATEST_DATA["frame_count"] += 1
        if is_speech:
            LATEST_DATA["speech_count"] += 1
        else:
            LATEST_DATA["silence_count"] += 1

        total = LATEST_DATA["frame_count"]
        LATEST_DATA["speech_ratio"] = (
            LATEST_DATA["speech_count"] / total * 100 if total > 0 else 0.0
        )
        LATEST_DATA["last_updated"] = now
        LATEST_DATA["rtp_active"]   = True

        # ── Feature 2: last_state ─────────────────────────────────────────────
        # Plain string label derived from VAD result — "speech" or "silence"
        LATEST_DATA["last_state"] = "speech" if is_speech else "silence"

        # ── Feature 3: avg_trt ────────────────────────────────────────────────
        # Accumulate running average of per-frame processing latency (ms).
        # trt_ms is passed in from RTPReceiver which already measures t_recv.
        if trt_ms > 0:
            _trt_sum   += trt_ms
            _trt_count += 1
            LATEST_DATA["avg_trt"] = _trt_sum / _trt_count

        # ── Feature 4: fps ────────────────────────────────────────────────────
        # Append current timestamp to the sliding window deque, then count
        # how many entries fall within the last 1 second.
        _frame_timestamps.append(now)
        cutoff = now - 1.0
        fps = sum(1 for t in _frame_timestamps if t >= cutoff)
        LATEST_DATA["fps"] = float(fps)


# ── Flask polling endpoints ───────────────────────────────────────────────────

@app.route("/latest")
def latest():
    """Polling endpoint for Streamlit. Returns latest processed frame state."""
    return jsonify(LATEST_DATA)


@app.route("/health")
def health():
    """Liveness probe so Streamlit can detect when the backend comes online."""
    return jsonify({"status": "ok", "rtp_active": LATEST_DATA["rtp_active"]})


@app.route("/reset")
def reset():
    """Reset counters — callable from Streamlit's Reset button."""
    global _trt_sum, _trt_count
    _trt_sum   = 0.0
    _trt_count = 0
    _frame_timestamps.clear()
    LATEST_DATA.update({
        "seq": 0, "is_speech": False,
        "frame_count": 0, "speech_count": 0,
        "silence_count": 0, "speech_ratio": 0.0,
        "last_updated": time.time(), "rtp_active": False,
        "last_state": "silence", "avg_trt": 0.0, "fps": 0.0,
    })
    return jsonify({"status": "reset"})


# ── constants ────────────────────────────────────────────────────────────────
PCMU_SAMPLE_RATE   = 8_000
TARGET_SAMPLE_RATE = 16_000
FRAME_MS           = 30
FRAME_SAMPLES_16K  = int(TARGET_SAMPLE_RATE * FRAME_MS / 1000)
RESAMPLE_RATIO_UP  = TARGET_SAMPLE_RATE / PCMU_SAMPLE_RATE
RESAMPLE_RATIO_DN  = PCMU_SAMPLE_RATE / TARGET_SAMPLE_RATE
RESAMPLE_CONVERTER = "sinc_fastest"

RTP_PAYLOAD_TYPE   = 0


# ── RTP sender ────────────────────────────────────────────────────────────────

class RTPSender:
    def __init__(self, dest_ip: str, dest_port: int, src_port: int = 5074):
        self.dest   = (dest_ip, dest_port)
        self.sock   = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", src_port))

        self._ssrc     = random.randint(0, 0xFFFFFFFF)
        self._seq      = random.randint(0, 0xFFFF)
        self._ts       = random.randint(0, 0xFFFFFFFF)
        self._ts_step  = int(PCMU_SAMPLE_RATE * FRAME_MS / 1000)

    def send(self, pcm16_at_16k: np.ndarray):
        pcm8 = samplerate.resample(pcm16_at_16k, RESAMPLE_RATIO_DN,
                                   RESAMPLE_CONVERTER)
        pcm8 = np.clip(pcm8, -32768, 32767).astype(np.int16)

        ulaw_bytes = audioop.lin2ulaw(pcm8.tobytes(), 2)

        header = struct.pack(
            "!BBHII",
            0x80,
            RTP_PAYLOAD_TYPE,
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


# ── RTP receiver ──────────────────────────────────────────────────────────────

class RTPReceiver:
    def __init__(self, port: int, handler: "DenoiseVADHandler",
                 metrics: "MetricsLogger", sender: "RTPSender"):
        self.port    = port
        self.handler = handler
        self.metrics = metrics
        self.sender  = sender

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", self.port))
        self.sock.settimeout(1.0)

        self.running    = False
        self._seq       = 0
        self._buf       = np.zeros(0, dtype=np.int16)
        self._pkt_count = 0

        self.emitter = None

    # 🔥 NEW: Dummy RTP sender to keep call alive
    def _send_dummy_rtp(self, addr):
        client_ip, client_port = addr

        header = struct.pack(
            "!BBHII",
            0x80,
            RTP_PAYLOAD_TYPE,
            self._seq & 0xFFFF,
            self._seq * 160,
            0
        )

        payload = b'\xff' * 160  # 20ms fake audio

        try:
            self.sock.sendto(header + payload, (client_ip, client_port))
        except Exception as e:
            print(f"⚠️ Dummy RTP send error: {e}")

    def listen(self):
        print(f"👂 RTP Listener active on UDP port {self.port}")
        self.running = True

        while self.running:
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except Exception as exc:
                print(f"⚠️  RTP recv error: {exc}")
                break

            if len(data) <= 12:
                continue

            self._pkt_count += 1
            if self._pkt_count <= 5 or self._pkt_count % 50 == 0:
                print(f"📦 RTP packet #{self._pkt_count} from {addr} "
                      f"({len(data)} bytes)")

            # 🔥 NEW: send RTP back immediately (CRITICAL FIX)
            self._send_dummy_rtp(addr)

            t_recv = time.perf_counter()

            rtp_payload = data[12:]

            pcm8_bytes = audioop.ulaw2lin(rtp_payload, 2)
            pcm8 = np.frombuffer(pcm8_bytes, dtype=np.int16)

            pcm16 = samplerate.resample(pcm8, RESAMPLE_RATIO_UP,
                                        RESAMPLE_CONVERTER)
            pcm16 = np.clip(pcm16, -32768, 32767).astype(np.int16)

            self._buf = np.concatenate((self._buf, pcm16))
            while len(self._buf) >= FRAME_SAMPLES_16K:
                frame     = self._buf[:FRAME_SAMPLES_16K]
                self._buf = self._buf[FRAME_SAMPLES_16K:]

                denoised, is_speech = self.handler.handle_raw_frame(
                    self._seq, frame, t_recv, self.metrics, emitter=self.emitter
                )

                # Compute per-frame processing latency in ms
                trt_ms = (time.perf_counter() - t_recv) * 1000.0

                self.sender.send(denoised)
                # Pass trt_ms to emitter so avg_trt stays accurate
                if self.emitter:
                    self.emitter.send(self._seq, denoised, is_speech, trt_ms=trt_ms)
                self._seq += 1

        print("🛑 RTP Listener stopped.")


# ── SIP header helpers ────────────────────────────────────────────────────────

def parse_sip_headers(msg: str) -> dict:
    """
    Extract key SIP headers from an incoming message.
    Returns a dict with lowercase keys: via, from, to, call_id, cseq.

    SIP headers are case-insensitive and may use compact forms:
        v  = Via
        f  = From
        t  = To
        i  = Call-ID
    We handle both long and compact forms here.
    """
    headers = {}
    lines = msg.split("\r\n")

    for line in lines[1:]:           # skip request/status line
        if not line or ":" not in line:
            continue

        name, _, value = line.partition(":")
        name  = name.strip().lower()
        value = value.strip()

        # Normalize compact header names
        compact_map = {"v": "via", "f": "from", "t": "to", "i": "call-id"}
        name = compact_map.get(name, name)

        # Only capture the FIRST occurrence of each header we care about
        if name in ("via", "from", "to", "call-id", "cseq") and name not in headers:
            headers[name] = value

    return headers


def extract_rtp_port(msg: str) -> int:
    """Extract the audio port from the SDP body of an INVITE."""
    for line in msg.split("\r\n"):
        if line.startswith("m=audio"):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    return int(parts[1])
                except ValueError:
                    pass
    return 5070   # safe fallback


def get_local_ip() -> str:
    """
    Return the machine's outbound LAN IP — NOT '0.0.0.0'.
    This is what must go into Contact and SDP so MicroSIP can route ACK back.
    Uses a UDP connect trick: no packet is sent, but the OS picks the right
    source interface.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def fix_via_rport(via_value: str, src_addr: tuple) -> str:
    """
    RFC 3581 compliance — fill in rport and received in the Via header.

    MicroSIP sends:   Via: SIP/2.0/UDP 192.168.x.x:5060;rport;branch=z9hG4bKxxx
    We must return:   Via: SIP/2.0/UDP 192.168.x.x:5060;rport=5060;received=192.168.x.x;branch=z9hG4bKxxx

    If rport already has a value, leave it alone.
    If received is already present, leave it alone.
    """
    src_ip, src_port = src_addr

    # Replace bare ;rport (no value) with ;rport=<actual_port>
    # Matches ;rport at end-of-string or ;rport followed by ; but NOT ;rport=
    via_value = re.sub(r";rport(?!=)", f";rport={src_port}", via_value)

    # Add received= if not already present
    # RFC 3581 §4: always add received when source IP differs from Via address;
    # safe to always add for NAT traversal
    if "received=" not in via_value:
        via_value = via_value.replace(
            ";branch=", f";received={src_ip};branch="
        )

    return via_value


def build_100_trying(headers: dict, server_ip: str, src_addr: tuple) -> str:
    """
    100 Trying — provisional response, not transaction-completing.
    Must include Via (with rport filled), From, To, Call-ID, CSeq.
    No To-tag is added for 1xx responses.
    """
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
    """
    server_ip   — real routable IP (NOT 0.0.0.0), used in Contact + SDP
    server_port — SIP listen port (5060), used in Contact
    rtp_port    — server's RTP listen port (7000), advertised in SDP
    src_addr    — (ip, port) of the packet source, used for rport/received
    """
    via = fix_via_rport(headers["via"], src_addr)

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
        f"Contact: <sip:{server_ip}:{server_port}>\r\n"   # REAL IP, not 0.0.0.0
        "Content-Type: application/sdp\r\n"
        f"Content-Length: {content_length}\r\n"
        "\r\n"
        f"{sdp_body}"
    )


# ── SIP server ────────────────────────────────────────────────────────────────

class SIPSignalingServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 5060):
        self.host = host
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))

        self._rtp_receiver = None
        self._rtp_sender   = None
        self._handler      = None
        self._metrics      = MetricsLogger()

        # FIX: Track call state properly
        # "idle"      → no active call
        # "ringing"   → INVITE processed, 200 OK sent, waiting for ACK
        # "active"    → ACK received, RTP running
        self._call_state   = "idle"
        self._pending_call = {}     # stores context between INVITE and ACK

        self._emitter = SocketEmitter(sio)

        # Real routable IP for Contact + SDP — never "0.0.0.0"
        self._local_ip = get_local_ip()
        print(f"🌐 Local IP detected: {self._local_ip}")

    def start(self):
        print(f"☎️  SIP Listener started on {self.host}:{self.port}")
        while True:
            try:
                data, addr = self.sock.recvfrom(4096)
                msg        = data.decode("utf-8", errors="replace").strip()

                if not msg:
                    continue

                first_line = msg.split("\r\n")[0]
                print(f"📨 SIP [{self._call_state}] {first_line} from {addr}")

                if first_line.startswith("INVITE"):
                    self._handle_invite(msg, addr)

                elif first_line.startswith("ACK"):
                    self._handle_ack(msg, addr)

                elif first_line.startswith("BYE"):
                    self._handle_bye(msg, addr)

                elif first_line.startswith("REGISTER"):
                    print(f"ℹ️  REGISTER ignored from {addr}")

                elif first_line.startswith("OPTIONS"):
                    print(f"ℹ️  OPTIONS ignored from {addr}")

                else:
                    print(f"⚠️  Unknown SIP message ignored: {first_line}")

            except Exception as exc:
                print(f"⚠️  SIP error: {exc}")
                continue

    def _handle_invite(self, msg: str, addr):
        if self._call_state == "active":
            # Re-INVITE (mid-call hold/transfer) — out of scope, ignore safely
            print("⚠️  Re-INVITE during active call ignored")
            return

        if self._call_state == "ringing":
            # Client is retransmitting INVITE because it hasn't received 200 OK
            # yet (or our 200 OK got lost). Resend 200 OK with the same headers.
            print("🔁 INVITE retransmit detected — resending 200 OK")
            response = build_200_ok(
                self._pending_call["headers"],
                self._local_ip,
                self.port,
                self._pending_call["server_rtp_port"],
                addr,
            )
            self.sock.sendto(response.encode(), addr)
            return

        # ── Fresh INVITE ──────────────────────────────────────────────────────
        print("📞 INVITE received — setting up media session …")

        headers = parse_sip_headers(msg)
        missing = [h for h in ("via", "from", "to", "call-id", "cseq")
                   if h not in headers]
        if missing:
            print(f"⚠️  INVITE missing required headers: {missing} — dropping")
            return

        # Step 1: Send 100 Trying immediately so client stops aggressive retransmit
        trying = build_100_trying(headers, self._local_ip, addr)
        self.sock.sendto(trying.encode(), addr)
        print("📤 100 Trying sent")

        # Step 2: Parse client RTP target
        rtp_port = extract_rtp_port(msg)
        rtp_ip   = addr[0]
        print(f"🎯 Client RTP target: {rtp_ip}:{rtp_port}")

        SERVER_RTP_PORT = 7000

        # Step 3: Pre-build RTP components (but don't start the listener yet)
        rtp_sender = RTPSender(dest_ip=rtp_ip, dest_port=rtp_port, src_port=7002)
        call_id    = headers.get("call-id", "sip-call")
        handler    = DenoiseVADHandler(call_id)
        rtp_receiver = RTPReceiver(
            port=SERVER_RTP_PORT,
            handler=handler,
            metrics=self._metrics,
            sender=rtp_sender,
        )
        rtp_receiver.emitter = self._emitter

        # Step 4: Stash everything — RTP starts only on ACK
        self._pending_call = {
            "headers":          headers,
            "addr":             addr,
            "rtp_sender":       rtp_sender,
            "rtp_receiver":     rtp_receiver,
            "handler":          handler,
            "server_rtp_port":  SERVER_RTP_PORT,
        }

        # Step 5: Send 200 OK
        response = build_200_ok(
            headers,
            self._local_ip,     # real routable IP, not "0.0.0.0"
            self.port,          # SIP port (5060)
            SERVER_RTP_PORT,    # RTP port (7000)
            addr,               # src_addr for rport/received fix
        )
        self.sock.sendto(response.encode(), addr)
        print("📤 200 OK sent — waiting for ACK …")

        self._call_state = "ringing"

    def _handle_ack(self, msg: str, addr):
        if self._call_state != "ringing":
            print(f"ℹ️  ACK ignored (call state: {self._call_state})")
            return

        print("✅ ACK received — call confirmed, starting RTP listener")

        # Promote pending components to active
        self._rtp_sender   = self._pending_call["rtp_sender"]
        self._rtp_receiver = self._pending_call["rtp_receiver"]
        self._handler      = self._pending_call["handler"]
        self._pending_call = {}

        # NOW start RTP
        threading.Thread(
            target=self._rtp_receiver.listen,
            daemon=True,
        ).start()

        self._call_state = "active"
        print("🎙️  RTP listener is live — audio should flow now")

    def _handle_bye(self, msg: str, addr):
        print("📴 BYE received — tearing down call")
        headers = parse_sip_headers(msg)

        # Send 200 OK for BYE
        if "via" in headers:
            response = (
                "SIP/2.0 200 OK\r\n"
                f"Via: {headers['via']}\r\n"
                f"From: {headers['from']}\r\n"
                f"To: {headers['to']}\r\n"
                f"Call-ID: {headers['call-id']}\r\n"
                f"CSeq: {headers['cseq']}\r\n"
                "Content-Length: 0\r\n"
                "\r\n"
            )
            self.sock.sendto(response.encode(), addr)
            print("📤 200 OK sent for BYE")

        if self._rtp_receiver:
            self._rtp_receiver.running = False
        if self._rtp_sender:
            self._rtp_sender.close()

        self._rtp_receiver = None
        self._rtp_sender   = None
        self._handler      = None
        self._call_state   = "idle"
        print("📭 Call torn down — ready for next INVITE")


if __name__ == "__main__":
    server = SIPSignalingServer(port=5060)

    threading.Thread(target=server.start, daemon=True).start()

    print("🌐 Starting Socket.IO server on port 5000...")
    import eventlet
    eventlet.wsgi.server(eventlet.listen(("0.0.0.0", 5000)), app)