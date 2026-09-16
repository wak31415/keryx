"""Tests for the wake-word listener (fake detector: no openWakeWord, no model files)."""

import inspect
import sys

from jarvis.wakeword import (
    WAKE_FRAME_SAMPLES,
    WAKE_SAMPLE_RATE,
    OpenWakeWordDetector,
    WakeWordDetector,
    WakeWordListener,
    wakeword_unavailable,
)

FRAME = b"\x01\x00" * WAKE_FRAME_SAMPLES


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeDetector:
    """Scripted detector: returns the queued scores in order, then 0.0."""

    sample_rate = WAKE_SAMPLE_RATE
    frame_samples = WAKE_FRAME_SAMPLES

    def __init__(self, *scores: float) -> None:
        self.scores = list(scores)
        self.frames: list[bytes] = []
        self.resets = 0

    def score(self, frame_int16: bytes) -> float:
        self.frames.append(frame_int16)
        return self.scores.pop(0) if self.scores else 0.0

    def reset(self) -> None:
        self.resets += 1


def test_feed_detects_when_the_score_reaches_the_threshold():
    listener = WakeWordListener(FakeDetector(0.9), threshold=0.5)

    assert listener.feed(FRAME) is True


def test_feed_ignores_scores_below_the_threshold():
    detector = FakeDetector(0.1, 0.49)
    listener = WakeWordListener(detector, threshold=0.5)

    assert listener.feed(FRAME) is False
    assert listener.feed(FRAME) is False
    assert detector.frames == [FRAME, FRAME]


def test_feed_detects_exactly_at_the_threshold():
    listener = WakeWordListener(FakeDetector(0.5), threshold=0.5)

    assert listener.feed(FRAME) is True


def test_detections_inside_the_refractory_window_are_suppressed():
    clock = FakeClock()
    listener = WakeWordListener(FakeDetector(0.9, 0.9, 0.9), refractory_s=2.0, now=clock)

    assert listener.feed(FRAME) is True

    clock.advance(1.9)
    assert listener.feed(FRAME) is False

    clock.advance(0.2)
    assert listener.feed(FRAME) is True


def test_reset_forwards_to_the_detector_and_clears_the_refractory_window():
    clock = FakeClock()
    detector = FakeDetector(0.9, 0.9)
    listener = WakeWordListener(detector, now=clock)
    assert listener.feed(FRAME) is True

    listener.reset()

    assert detector.resets == 1
    assert listener.feed(FRAME) is True


def test_last_score_records_the_most_recent_detector_score():
    listener = WakeWordListener(FakeDetector(0.3, 0.8))

    listener.feed(FRAME)
    assert listener.last_score == 0.3

    listener.feed(FRAME)
    assert listener.last_score == 0.8


def test_fake_detector_matches_the_detector_protocol():
    detector = FakeDetector()
    method_names = [name for name in vars(WakeWordDetector) if not name.startswith("_")]

    assert set(method_names) == {"score", "reset"}
    for name in method_names:
        expected = list(inspect.signature(getattr(WakeWordDetector, name)).parameters)[1:]
        assert list(inspect.signature(getattr(detector, name)).parameters) == expected


def test_openwakeword_detector_declares_the_wake_frame_contract():
    assert OpenWakeWordDetector.sample_rate == WAKE_SAMPLE_RATE == 16000
    assert OpenWakeWordDetector.frame_samples == WAKE_FRAME_SAMPLES == 1280

    method_names = [name for name in vars(WakeWordDetector) if not name.startswith("_")]
    for name in method_names:
        expected = list(inspect.signature(getattr(WakeWordDetector, name)).parameters)
        assert list(inspect.signature(getattr(OpenWakeWordDetector, name)).parameters) == expected


def test_openwakeword_is_never_imported_at_module_scope():
    assert "openwakeword" not in sys.modules


# --- whether this machine can run it at all --------------------------------


def test_the_wake_word_is_available_when_both_packages_are_installed():
    assert wakeword_unavailable(platform="darwin", find_spec=lambda name: object()) is None


def test_off_macos_the_wake_word_is_simply_not_available():
    """Linux has no wheel for it: that is the platform, not a broken install."""
    why = wakeword_unavailable(platform="linux", find_spec=lambda name: None)

    assert why == "the wake word needs macOS"


def test_on_macos_a_missing_package_is_named():
    def find_spec(name):
        return None if name == "openwakeword" else object()

    why = wakeword_unavailable(platform="darwin", find_spec=find_spec)

    assert why is not None
    assert "openwakeword" in why
    assert "uv sync" in why


def test_asking_whether_it_is_available_imports_nothing():
    before = set(sys.modules)

    wakeword_unavailable()

    assert not {"openwakeword", "sounddevice"} & (set(sys.modules) - before)
