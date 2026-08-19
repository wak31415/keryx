"""Wake-word detection: the detector protocol, the openWakeWord adapter, and the
listener that turns a stream of scores into rate-limited detections.

`openwakeword` (and its onnxruntime model) is imported lazily inside
`OpenWakeWordDetector.__init__`, so importing this module costs nothing and works on a
machine with no models downloaded. A Porcupine (`pvporcupine`) detector would be a
drop-in fallback: implement `WakeWordDetector` and pass it to `WakeWordListener`.
"""

import logging
import time
from collections.abc import Callable
from typing import Protocol

import numpy as np

log = logging.getLogger("jarvis.wakeword")

WAKE_SAMPLE_RATE = 16000
WAKE_FRAME_SAMPLES = 1280  # 80 ms, the frame size openWakeWord expects
MODEL_MISSING_ERROR = "wake-word model missing; run `jarvis download-models`"


class WakeWordDetector(Protocol):
    """Scores one frame of mic audio for the presence of the wake word."""

    sample_rate: int
    frame_samples: int

    def score(self, frame_int16: bytes) -> float:
        """Score one `frame_samples`-sample 16-bit LE mono frame; higher = more likely."""
        ...

    def reset(self) -> None:
        """Drop the detector's internal audio history (e.g. after a session)."""
        ...


class OpenWakeWordDetector:
    """openWakeWord 0.6.0 adapter (onnx runtime), default model `hey_jarvis`.

    `predict()` returns `{model_name: score}`; we take the max over the values rather
    than hardcoding the key. Frames must be exactly `frame_samples` int16 LE samples at
    `sample_rate`.
    """

    sample_rate = WAKE_SAMPLE_RATE
    frame_samples = WAKE_FRAME_SAMPLES

    def __init__(self, model_name: str = "hey_jarvis") -> None:
        self.model_name = model_name
        self._model = _load_openwakeword_model(model_name)

    def score(self, frame_int16: bytes) -> float:
        samples = np.frombuffer(frame_int16, dtype="<i2")
        scores = self._model.predict(samples)
        return max((float(value) for value in scores.values()), default=0.0)

    def reset(self) -> None:
        self._model.reset()


def _load_openwakeword_model(model_name: str):
    """Import openWakeWord and build the model, mapping failures to a clear error."""
    try:
        from openwakeword.model import Model
    except ImportError as exc:  # pragma: no cover - openwakeword is a hard dependency
        raise RuntimeError("openwakeword is not installed") from exc

    try:
        return Model(wakeword_models=[model_name], inference_framework="onnx")
    except Exception as exc:
        raise RuntimeError(MODEL_MISSING_ERROR) from exc


class WakeWordListener:
    """Feeds frames to a detector and reports detections, one per refractory window.

    `feed` returns True when the score reaches `threshold` and the previous detection is
    more than `refractory_s` old, so a single spoken "hey jarvis" fires once instead of
    once per overlapping frame. `now` is an injectable monotonic clock for tests.
    """

    def __init__(
        self,
        detector: WakeWordDetector,
        threshold: float = 0.5,
        refractory_s: float = 2.0,
        *,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._detector = detector
        self._threshold = threshold
        self._refractory_s = refractory_s
        self._now = now
        self._last_wake: float | None = None
        self.last_score = 0.0

    def feed(self, frame_int16: bytes) -> bool:
        """Score one frame; True if it is a fresh wake-word detection."""
        self.last_score = self._detector.score(frame_int16)
        if self.last_score < self._threshold:
            return False

        now = self._now()
        if self._last_wake is not None and (now - self._last_wake) < self._refractory_s:
            return False

        self._last_wake = now
        log.info("wake word detected (score %.2f)", self.last_score)
        return True

    def reset(self) -> None:
        """Clear the detector's history and the refractory window."""
        self._detector.reset()
        self._last_wake = None
