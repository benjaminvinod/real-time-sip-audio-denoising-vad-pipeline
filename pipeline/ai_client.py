"""
ai_client.py  –  Downstream AI client stub
Receives denoised audio frames and forwards them to an external AI API.

Replace the stub body with a real implementation (e.g. OpenAI Whisper,
AWS Transcribe streaming, or your proprietary endpoint).
"""

import threading
import queue
import time
from typing import Optional

import numpy as np


# ── Configuration ─────────────────────────────────────────────────────────────

AI_API_URL    = "https://api.example.com/transcribe"   # TODO: replace
AI_API_KEY    = ""                                      # TODO: load from env
BATCH_FRAMES  = 10                                      # buffer N frames before send
ENABLED       = False                                   # set True to activate


# ── Internal queue for non-blocking dispatch ─────────────────────────────────

_queue: queue.Queue = queue.Queue(maxsize=200)
_worker_started     = False
_worker_lock        = threading.Lock()


def _worker():
    """Background thread — drains queue and sends batches to AI API."""
    buffer: dict[str, list] = {}    # call_id → list of (seq, pcm16)

    while True:
        try:
            item = _queue.get(timeout=2.0)
        except queue.Empty:
            # Flush any pending partial batches older than 2s
            for cid in list(buffer.keys()):
                if buffer[cid]:
                    _flush(cid, buffer.pop(cid))
            continue

        seq, pcm16, call_id = item
        buffer.setdefault(call_id, []).append((seq, pcm16))

        if len(buffer[call_id]) >= BATCH_FRAMES:
            _flush(call_id, buffer.pop(call_id))


def _flush(call_id: str, frames: list):
    """Send a batch of frames to the AI API (stub)."""
    if not ENABLED:
        return

    # Concatenate frames into a single audio blob
    audio = np.concatenate([f for _, f in frames], axis=0)
    total_ms = len(frames) * 30

    print(f"[AI] Sending {len(frames)} frames ({total_ms}ms) for call [{call_id}]")

    # ── TODO: replace with real HTTP/gRPC call ────────────────────────────────
    # Example (using requests):
    #
    # import requests, io, soundfile as sf
    # buf = io.BytesIO()
    # sf.write(buf, audio, 16000, format="WAV", subtype="PCM_16")
    # buf.seek(0)
    # resp = requests.post(
    #     AI_API_URL,
    #     headers={"Authorization": f"Bearer {AI_API_KEY}"},
    #     files={"audio": ("audio.wav", buf, "audio/wav")},
    #     data={"call_id": call_id},
    #     timeout=5,
    # )
    # if resp.ok:
    #     transcript = resp.json().get("text", "")
    #     print(f"[AI] Transcript [{call_id}]: {transcript}")
    # ─────────────────────────────────────────────────────────────────────────


def _ensure_worker():
    global _worker_started
    if not _worker_started:
        with _worker_lock:
            if not _worker_started:
                t = threading.Thread(target=_worker, daemon=True, name="ai-client-worker")
                t.start()
                _worker_started = True


# ── Public API ────────────────────────────────────────────────────────────────

def send_to_ai(seq: int, pcm16: np.ndarray, call_id: str = "default"):
    """
    Queue a single denoised audio frame for async delivery to the AI API.
    Non-blocking — drops frames if the queue is full (backpressure protection).
    """
    _ensure_worker()
    try:
        _queue.put_nowait((seq, pcm16.copy(), call_id))
    except queue.Full:
        pass   # drop frame to avoid blocking the RTP pipeline
