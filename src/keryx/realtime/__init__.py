"""Realtime model providers: the shared interfaces, the OpenAI Realtime client, and
`make_provider`, the one place a session's provider is built.

Nothing else is re-exported here; import from the modules themselves.
"""

from keryx.config import Settings
from keryx.realtime.base import RealtimeProvider
from keryx.realtime.openai import OpenAIRealtimeClient


def make_provider(settings: Settings) -> RealtimeProvider:
    """A fresh, unconnected provider for one session, on `settings.voice_endpoint`.

    The phone server and `keryx loopback` both build theirs here, so a voice server of the
    owner's own is the same one wherever a session opens.
    """
    return OpenAIRealtimeClient(settings.voice_endpoint)
