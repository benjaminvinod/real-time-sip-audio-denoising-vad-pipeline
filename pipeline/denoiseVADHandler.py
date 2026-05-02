"""
denoiseVADHandler.py  –  Full pipeline handler
Features added:
  - SNR computation (raw_energy, denoised_energy, snr_db)
  - Speech boundary tracking (speech_start / speech_end events)
  - Advanced VAD smoothing via rolling window + debounce
  - audioClear reset() method to flush all state
  - Socket.IO path unchanged
"""

import base64
import time
from collections import deque

import numpy as np
import samplerate
import webrtcvad
from pyrnnoise.rnnoise import create, process_mono_frame, FRAME_SIZE


# ── Smoothing / debounce parameters ──────────────────────────────────────────
VAD_WINDOW_SIZE  = 5   # rolling window of VAD decisions
VAD_SPEECH_VOTES = 3   # minimum "speech" votes in window to call speech
VAD_DEBOUNCE_OFF = 8   # frames of silence needed to flip from speech → silence
VAD_DEBOUNCE_ON  = 2   # frames of speech needed to flip from silence → speech


def _energy_db(pcm16: np.ndarray) -> float:
    """RMS energy in dB (float32 safe). Returns -120 dB for silent frames."""
    if pcm16.size == 0:
        return -120.0
    rms = np.sqrt(np.mean(pcm16.astype(np.float32) ** 2))
    if rms < 1e-6:
        return -120.0
    return float(20.0 * np.log10(rms / 32768.0))


def _snr_db(raw_energy_db: float, denoised_energy_db: float) -> float:
    """
    Approximate SNR improvement in dB:  denoised_energy - raw_energy.
    Positive value = denoiser amplified signal relative to noise floor.
    """
    return denoised_energy_db - raw_energy_db


class DenoiseVADHandler:
    """
    One instance per SIP call (call_id) or Socket.IO client (socket_id).

    Socket.IO path  → instantiated via add_instance(socket_id, sio)
    SIP/RTP path    → instantiated directly with (call_id,)
    """

    _instances: dict[str, "DenoiseVADHandler"] = {}

    # ── construction ──────────────────────────────────────────────────────────

    def __init__(self, socket_id: str, sio=None, *,
                 vad_aggr: int = 2, converter: str = "sinc_fastest"):
        self.socket_id = socket_id
        self.sio       = sio

        self.rn_state  = create()
        self.vad       = webrtcvad.Vad(vad_aggr)
        self.converter = converter
        self.ratio_up   = 48_000 / 16_000
        self.ratio_down = 16_000 / 48_000

        # ── VAD smoothing state ───────────────────────────────────────────────
        self._vad_window: deque[bool] = deque(maxlen=VAD_WINDOW_SIZE)
        self._debounce_counter: int   = 0
        self._smoothed_speech: bool   = False   # current stable VAD state
        self._prev_speech:     bool   = False   # previous stable state

    # ── class-level registry (Socket.IO path) ────────────────────────────────

    @classmethod
    def add_instance(cls, socket_id: str, sio, *,
                     vad_aggr: int = 2, converter: str = "sinc_fastest"):
        if socket_id not in cls._instances:
            cls._instances[socket_id] = cls(socket_id, sio,
                                            vad_aggr=vad_aggr,
                                            converter=converter)
            print(f"Instance added for socket_id: {socket_id}")
        return cls._instances[socket_id]

    @classmethod
    def remove_instance(cls, socket_id: str):
        if socket_id in cls._instances:
            del cls._instances[socket_id]
            print(f"Instance removed for socket_id: {socket_id}")

    @classmethod
    def get_instance(cls, socket_id: str):
        return cls._instances.get(socket_id)

    def teardown(self):
        """Called by SIPSignalingServer on BYE."""
        DenoiseVADHandler.remove_instance(self.socket_id)

    def reset(self):
        """
        audioClear interrupt — flush all internal buffers and counters.
        Called by /clear_audio endpoint.
        """
        self._vad_window.clear()
        self._debounce_counter = 0
        self._smoothed_speech  = False
        self._prev_speech      = False
        print(f"🔄 DenoiseVADHandler reset for [{self.socket_id}]")

    # ── VAD smoothing ─────────────────────────────────────────────────────────

    def _smooth_vad(self, raw_speech: bool) -> tuple[bool, str]:
        """
        Apply rolling-window + debounce to reduce flicker.

        Returns (smoothed_is_speech, speech_event)
        speech_event is one of: "speech_start", "speech_end", ""
        """
        self._vad_window.append(raw_speech)
        votes = sum(self._vad_window)

        # Rolling window decision
        window_says_speech = votes >= VAD_SPEECH_VOTES

        # Debounce transitions
        if window_says_speech != self._smoothed_speech:
            self._debounce_counter += 1
            threshold = VAD_DEBOUNCE_ON if window_says_speech else VAD_DEBOUNCE_OFF
            if self._debounce_counter >= threshold:
                self._smoothed_speech  = window_says_speech
                self._debounce_counter = 0
        else:
            self._debounce_counter = 0

        # Detect boundary events
        event = ""
        if self._smoothed_speech and not self._prev_speech:
            event = "speech_start"
        elif not self._smoothed_speech and self._prev_speech:
            event = "speech_end"
        self._prev_speech = self._smoothed_speech

        return self._smoothed_speech, event

    # ── shared DSP core ───────────────────────────────────────────────────────

    def _process_pcm16(self, frame16: np.ndarray) -> tuple[np.ndarray, bool, dict, str]:
        """
        Core DSP: PCM16 @ 16 kHz  →
            denoised PCM16 @ 16 kHz,
            smoothed VAD flag,
            snr_info dict,
            speech_event str
        """

        # ── Raw energy (before denoising) ─────────────────────────────────────
        raw_energy_db = _energy_db(frame16)

        # 1) Upsample to 48 kHz for RNNoise
        frame48 = samplerate.resample(frame16, self.ratio_up, self.converter)
        frame48 = np.clip(frame48, -32768, 32767).astype(np.int16)

        # 2) RNNoise chunks
        chunks = []
        for i in range(0, len(frame48), FRAME_SIZE):
            chunk = frame48[i: i + FRAME_SIZE]
            if len(chunk) < FRAME_SIZE:
                chunk = np.pad(chunk, (0, FRAME_SIZE - len(chunk)))
            clean, _ = process_mono_frame(self.rn_state, chunk)
            chunks.append(clean)

        denoised48 = (np.concatenate(chunks)
                      if chunks else np.zeros(0, dtype=np.int16))

        # 3) Downsample back to 16 kHz
        if denoised48.size:
            denoised16 = samplerate.resample(denoised48, self.ratio_down, self.converter)
            denoised16 = np.clip(denoised16, -32768, 32767).astype(np.int16)
        else:
            denoised16 = np.zeros_like(frame16)

        # ── Denoised energy (after denoising, before gating) ──────────────────
        denoised_energy_db = _energy_db(denoised16)
        snr               = _snr_db(raw_energy_db, denoised_energy_db)
        snr_info          = {
            "raw_energy":      raw_energy_db,
            "denoised_energy": denoised_energy_db,
            "snr_db":          snr,
        }

        # 4) Raw WebRTC VAD
        vad_frame = denoised16[:480] if len(denoised16) >= 480 else np.pad(
            denoised16, (0, 480 - len(denoised16)))
        buf         = memoryview(vad_frame.astype(np.int16)).cast("B")
        raw_speech  = self.vad.is_speech(buf, 16_000)

        # 5) Smooth VAD
        is_speech, speech_event = self._smooth_vad(raw_speech)

        # 6) Gate output — zero out if silence
        if not is_speech:
            denoised16 = np.zeros_like(denoised16)

        return denoised16, is_speech, snr_info, speech_event

    # ── SIP / RTP entry-point ─────────────────────────────────────────────────

    def handle_raw_frame(self, seq: int, frame16: np.ndarray,
                         t_recv: float,
                         metrics) -> tuple[np.ndarray, bool, dict, str]:
        """
        Called by RTPReceiver.
        Returns (denoised_pcm16, is_speech, snr_info, speech_event).
        """
        t_start = time.perf_counter()
        denoised, speech, snr_info, speech_event = self._process_pcm16(frame16)
        t_end   = time.perf_counter()

        processing_ms = (t_end - t_start) * 1000
        trt_ms        = (t_end - t_recv)  * 1000
        rtf           = (t_end - t_start) / (30 / 1000)

        metrics.log(seq=seq, is_speech=speech,
                    processing_ms=processing_ms,
                    trt_ms=trt_ms, rtf=rtf)

        label = "💬 SPEECH" if speech else "🔇 silence"
        ev    = f" [{speech_event}]" if speech_event else ""
        print(f"[RTP] frame {seq:05d} | {label}{ev} | "
              f"proc={processing_ms:.1f}ms | RTF={rtf:.3f} | TRT={trt_ms:.1f}ms | "
              f"SNR={snr_info['snr_db']:+.1f}dB")

        return denoised, speech, snr_info, speech_event

    # ── Socket.IO entry-point (unchanged) ────────────────────────────────────

    def handle_stream_media(self, data: dict):
        """
        Expects:
            data = { "seq": int, "data": "<base64 PCM16 30ms @ 16kHz>" }
        Emits back to same sid:
            { "seq": int, "data": base64, "is_speech": bool, "snr_db": float }
        """
        seq       = data["seq"]
        raw_bytes = base64.b64decode(data["data"])
        frame16   = np.frombuffer(raw_bytes, dtype=np.int16)

        denoised, is_speech, snr_info, speech_event = self._process_pcm16(frame16)

        out_b64 = base64.b64encode(denoised.tobytes()).decode()
        payload = {
            "seq":          seq,
            "data":         out_b64,
            "is_speech":    is_speech,
            "snr_db":       snr_info["snr_db"],
            "raw_energy":   snr_info["raw_energy"],
            "speech_event": speech_event,
        }
        self.sio.emit("streamMedia", payload, to=self.socket_id)
