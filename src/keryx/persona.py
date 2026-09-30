"""Who answers the phone: the assistant's name, and the voice that comes with it.

The service and the assistant are two names. The assistant is whatever `ASSISTANT_NAME`
says, and a built-in persona brings a Realtime voice of its own, so choosing Jarvis gets
Jarvis's voice back without a second setting. Any other name is free-form and speaks in
`DEFAULT_VOICE`. An explicit `OPENAI_VOICE` beats both (`Settings.voice`).

A voice must be one the organization's key may use: the Realtime API refuses one it may
not, and the refusal discards the whole `session.update` it came in — the instructions and
the tools with it — so a persona's voice is a constant here, never a guess.
"""

from dataclasses import dataclass

#: The voice of a name no persona claims.
DEFAULT_VOICE = "marin"


@dataclass(frozen=True)
class Persona:
    """A built-in assistant: how its name is spelled, and the voice it speaks in."""

    name: str
    voice: str


#: The built-in personas, by lower-cased name. The first is the default `ASSISTANT_NAME`.
PERSONAS = {
    "lyra": Persona("Lyra", "marin"),
    "jarvis": Persona("Jarvis", "cedar"),
}
DEFAULT_NAME = PERSONAS["lyra"].name


def voice_for(name: str) -> str:
    """The voice an assistant called `name` speaks in, matched case-insensitively."""
    persona = PERSONAS.get(name.strip().lower())
    return persona.voice if persona is not None else DEFAULT_VOICE
