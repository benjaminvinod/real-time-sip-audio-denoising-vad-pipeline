"""
metricsLogger.py  –  Benjamin Vinod | Module 1

Collects per-frame RTF and TRT metrics and publishes them to AWS CloudWatch
in batches of up to 20 data-points (CloudWatch PutMetricData limit).

Environment variables read (all optional)
-----------------------------------------
AWS_REGION          – default "ap-south-1"
CW_NAMESPACE        – default "DenoisePipeline"
CW_FLUSH_INTERVAL   – seconds between CloudWatch flushes (default 60)

Falls back gracefully: if boto3 is not installed or AWS credentials are
absent, metrics are printed to stdout only (useful for local dev).
"""

import os
import queue
import threading
import time
from datetime import datetime, timezone

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
    _BOTO3_AVAILABLE = True
except ImportError:
    _BOTO3_AVAILABLE = False


class MetricsLogger:
    def __init__(self):
        self._region    = os.getenv("AWS_REGION", "ap-south-1")
        self._namespace = os.getenv("CW_NAMESPACE", "DenoisePipeline")
        self._interval  = float(os.getenv("CW_FLUSH_INTERVAL", "60"))

        self._q: queue.Queue = queue.Queue()

        # running totals (for in-process summary)
        self._count         = 0
        self._total_rtf     = 0.0
        self._total_trt     = 0.0
        self._speech_frames = 0

        if _BOTO3_AVAILABLE:
            try:
                self._cw = boto3.client("cloudwatch", region_name=self._region)
                self._cw_ok = True
            except Exception:
                self._cw_ok = False
        else:
            self._cw_ok = False

        if not self._cw_ok:
            print("⚠️  CloudWatch unavailable – metrics printed to stdout only.")

        # background flush thread
        self._flush_thread = threading.Thread(target=self._flush_loop,
                                              daemon=True)
        self._flush_thread.start()

    # ── public ───────────────────────────────────────────────────────────────

    def log(self, *, seq: int, is_speech: bool,
            processing_ms: float, trt_ms: float, rtf: float):
        """Called once per processed frame (from any thread)."""
        self._count         += 1
        self._total_rtf     += rtf
        self._total_trt     += trt_ms
        self._speech_frames += int(is_speech)

        self._q.put({
            "seq":           seq,
            "is_speech":     is_speech,
            "processing_ms": processing_ms,
            "trt_ms":        trt_ms,
            "rtf":           rtf,
            "ts":            datetime.now(tz=timezone.utc),
        })

    def summary(self) -> dict:
        if self._count == 0:
            return {}
        return {
            "frames_total":   self._count,
            "speech_frames":  self._speech_frames,
            "avg_rtf":        self._total_rtf  / self._count,
            "avg_trt_ms":     self._total_trt  / self._count,
        }

    # ── internal ─────────────────────────────────────────────────────────────

    def _flush_loop(self):
        while True:
            time.sleep(self._interval)
            self._flush()

    def _flush(self):
        batch = []
        while not self._q.empty():
            try:
                batch.append(self._q.get_nowait())
            except queue.Empty:
                break

        if not batch:
            return

        avg_rtf = sum(m["rtf"]    for m in batch) / len(batch)
        avg_trt = sum(m["trt_ms"] for m in batch) / len(batch)
        speech  = sum(1 for m in batch if m["is_speech"])

        print(f"📊 Metrics flush | frames={len(batch)} | "
              f"avg_RTF={avg_rtf:.4f} | avg_TRT={avg_trt:.1f}ms | "
              f"speech={speech}/{len(batch)}")

        if not self._cw_ok:
            return

        now = datetime.now(tz=timezone.utc)
        metric_data = [
            {
                "MetricName": "AverageRTF",
                "Timestamp":  now,
                "Value":      avg_rtf,
                "Unit":       "None",
            },
            {
                "MetricName": "AverageTRT_ms",
                "Timestamp":  now,
                "Value":      avg_trt,
                "Unit":       "Milliseconds",
            },
            {
                "MetricName": "SpeechFrameCount",
                "Timestamp":  now,
                "Value":      float(speech),
                "Unit":       "Count",
            },
        ]

        try:
            self._cw.put_metric_data(
                Namespace=self._namespace,
                MetricData=metric_data,
            )
            print("✅ CloudWatch metrics published.")
        except (BotoCoreError, ClientError) as exc:
            print(f"⚠️  CloudWatch publish failed: {exc}")
