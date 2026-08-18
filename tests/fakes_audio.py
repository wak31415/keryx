"""Hardware-free doubles for the PortAudio streams `LocalAudioDevice` owns.

`FakeStreamFactory` has the same shape as the device's `stream_factory`
(`(kind, *, samplerate, blocksize, callback)`), records every stream it hands out, and
lets a test drive the audio callbacks the way PortAudio would: `push_mic(pcm)` delivers
one input frame, `pull_speaker()` renders one output block and returns the bytes the
speaker would have played.
"""

import numpy as np


class FakeStream:
    """Stand-in for a `sounddevice` stream: remembers its callback and lifecycle state."""

    def __init__(self, kind: str, *, samplerate: int, blocksize: int, callback) -> None:
        self.kind = kind
        self.samplerate = samplerate
        self.blocksize = blocksize
        self.callback = callback
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def close(self) -> None:
        self.closed = True


class FakeStreamFactory:
    """Callable stream factory that records the streams it creates."""

    def __init__(self) -> None:
        self.streams: list[FakeStream] = []

    def __call__(self, kind: str, *, samplerate: int, blocksize: int, callback) -> FakeStream:
        stream = FakeStream(kind, samplerate=samplerate, blocksize=blocksize, callback=callback)
        self.streams.append(stream)
        return stream

    def _stream(self, kind: str) -> FakeStream:
        for stream in self.streams:
            if stream.kind == kind:
                return stream
        raise AssertionError(f"no {kind} stream was created")

    @property
    def input(self) -> FakeStream:
        return self._stream("input")

    @property
    def output(self) -> FakeStream:
        return self._stream("output")

    def push_mic(self, pcm: bytes, *, status: object = None) -> None:
        """Deliver one mic frame, shaped `(frames, 1)` int16 the way sounddevice does."""
        indata = np.frombuffer(pcm, dtype="<i2").reshape(-1, 1)
        self.input.callback(indata, len(indata), None, status)

    def pull_speaker(self, frames: int | None = None, *, status: object = None) -> bytes:
        """Render one output block and return what the speaker would have played."""
        stream = self.output
        frames = stream.blocksize if frames is None else frames
        buffer = bytearray(frames * 2)
        stream.callback(memoryview(buffer), frames, None, status)
        return bytes(buffer)
