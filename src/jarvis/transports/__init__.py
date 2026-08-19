"""Transports: the audio pipe of a session (shared protocol + the local mic/speaker)."""

from jarvis.transports.local_audio import LocalAudioDevice, LocalTransport

__all__ = ["LocalAudioDevice", "LocalTransport"]
