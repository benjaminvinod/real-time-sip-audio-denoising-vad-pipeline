"""
denoiseVADHandler.py  –  Benjamin Vinod | Module 1

Two entry-points
----------------
handle_stream_media(data)   – original Socket.IO path (unchanged)
handle_raw_frame(seq, frame, t_recv, metrics)
                            – new SIP/RTP path: receives a numpy int16
                              array @ 16 kHz, runs the full pipeline,
                              and returns (denoised_pcm16, is_speech).
                              Also records RTF + TRT via MetricsLogger.
"""

import base64
import time
import numpy as np
import samplerate
import webrtcvad
from pyrnnoise.rnnoise import create, process_mono_frame, FRAME_SIZE


class DenoiseVADHandler:
    """
    One instance per connected client (Socket.IO sid) OR per active SIP call.

    Socket.IO path  → instantiated with (socket_id, sio)
    SIP/RTP path    → instantiated with (call_id,) – sio is None
    """

    _instances: dict[str, "DenoiseVADHandler"] = {}

    # ── construction ─────────────────────────────────────────────────────────

    def __init__(self, socket_id: str, sio=None, *,
                 vad_aggr: int = 2, converter: str = "sinc_fastest"):
        self.socket_id = socket_id
        self.sio       = sio                        # None for SIP calls

        self.rn_state  = create()
        self.vad       = webrtcvad.Vad(vad_aggr)
        self.converter = converter
        self.ratio_up   = 48_000 / 16_000           # 3.0
        self.ratio_down = 16_000 / 48_000           # 1/3

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

    # ── shared DSP core ───────────────────────────────────────────────────────

    def _process_pcm16(self, frame16: np.ndarray) -> tuple[np.ndarray, bool]:
        """
        Core DSP: PCM16 @ 16 kHz  →  denoised PCM16 @ 16 kHz + VAD flag.
        """

        # 1) upsample
        frame48 = samplerate.resample(frame16, self.ratio_up, self.converter)
        frame48 = np.clip(frame48, -32768, 32767).astype(np.int16)

        # 2) RNNoise
        chunks = []
        for i in range(0, len(frame48), FRAME_SIZE):
            chunk = frame48[i: i + FRAME_SIZE]
            if len(chunk) < FRAME_SIZE:
                chunk = np.pad(chunk, (0, FRAME_SIZE - len(chunk)))
            clean, _ = process_mono_frame(self.rn_state, chunk)
            chunks.append(clean)

        denoised48 = (np.concatenate(chunks)
                      if chunks else np.zeros(0, dtype=np.int16))

        # 3) downsample
        if denoised48.size:
            denoised16 = samplerate.resample(denoised48, self.ratio_down,
                                             self.converter)
            denoised16 = np.clip(denoised16, -32768, 32767).astype(np.int16)
        else:
            denoised16 = np.zeros_like(frame16)

        # 4) VAD
        vad_frame = denoised16[:480] if len(denoised16) >= 480 else np.pad(
            denoised16, (0, 480 - len(denoised16)))
        buf       = memoryview(vad_frame.astype(np.int16)).cast("B")
        is_speech = self.vad.is_speech(buf, 16_000)

        # 5) gate
        if not is_speech:
            denoised16 = np.zeros_like(denoised16)

        return denoised16, is_speech

    # ── SIP / RTP entry-point ─────────────────────────────────────────────────

    def handle_raw_frame(self, seq: int, frame16: np.ndarray,
                         t_recv: float, metrics, emitter=None) -> tuple[np.ndarray, bool]:
        """
        Called directly by RTPReceiver with a numpy int16 array @ 16 kHz.
        """

        t_start          = time.perf_counter()
        denoised, speech = self._process_pcm16(frame16)
        t_end            = time.perf_counter()

        processing_ms = (t_end - t_start) * 1000
        trt_ms        = (t_end - t_recv)  * 1000
        rtf           = (t_end - t_start) / (30 / 1000)

        metrics.log(seq=seq, is_speech=speech,
                    processing_ms=processing_ms,
                    trt_ms=trt_ms, rtf=rtf)

        label = "💬 SPEECH" if speech else "🔇 silence"
        print(f"[RTP] frame {seq:05d} | {label} | "
              f"proc={processing_ms:.1f}ms | RTF={rtf:.3f} | TRT={trt_ms:.1f}ms")

        # ✅ NEW: emit to Socket.IO (Module 4)
        if emitter:
            emitter.send(seq, denoised, speech)

        return denoised, speech

    # ── Socket.IO entry-point (unchanged) ────────────────────────────────────

    def handle_stream_media(self, data: dict):
        """
        Expects:
            data = { "seq": int, "data": "<base64 PCM16 30ms @ 16kHz>" }
        Emits back to same sid:
            { "seq": int, "data": base64, "is_speech": bool }
        """
        seq       = data["seq"]
        raw_bytes = base64.b64decode(data["data"])
        frame16   = np.frombuffer(raw_bytes, dtype=np.int16)

        denoised, is_speech = self._process_pcm16(frame16)

        out_b64 = base64.b64encode(denoised.tobytes()).decode()
        payload = {"seq": seq, "data": out_b64, "is_speech": is_speech}
        self.sio.emit("streamMedia", payload, to=self.socket_id)