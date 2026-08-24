"""Reading back what was said in an earlier call.

A call-back is a new realtime session: the provider keeps no history across sockets, so
the only record of the conversation that asked for the call is the transcript the session
wrote to `data_dir/calls/<session_id>.log`. This reads the end of one back, so the
call-back can open knowing how the last call went instead of only what the task returned.

The tail, not the whole thing: it goes into a system prompt that is spoken from, and the
last few exchanges are where the callback-worthy part of a call always is.
"""

import logging
from pathlib import Path

log = logging.getLogger("jarvis.transcripts")

#: How much of the previous call the context carries.
MAX_TRANSCRIPT_CHARS = 1200
#: Lines that are session bookkeeping (`--- session … ended`), not conversation.
_MARKER_PREFIX = "---"


def transcript_path(data_dir: Path, session_id: str) -> Path:
    """Where the session with this id wrote its transcript."""
    return data_dir / "calls" / f"{session_id}.log"


def read_tail(data_dir: Path, session_id: str, *, max_chars: int = MAX_TRANSCRIPT_CHARS) -> str:
    """The last of what was said in `session_id`, as `user:`/`assistant:` lines.

    Empty when there is no readable transcript — a call-back is still worth placing with
    only the task result, so nothing here is allowed to be fatal.
    """
    if not session_id:
        return ""
    path = transcript_path(data_dir, session_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        log.info("no readable transcript for session %s at %s", session_id, path)
        return ""

    spoken: list[str] = []
    for line in raw.splitlines():
        # Drop the timestamp the session writes, and keep only what someone said.
        text = line.split("] ", 1)[-1].strip() if line.startswith("[") else line.strip()
        if not text or text.startswith(_MARKER_PREFIX):
            continue
        if text.startswith(("user:", "assistant:")):
            spoken.append(text)

    transcript = "\n".join(spoken)
    if len(transcript) <= max_chars:
        return transcript
    # Keep the end, and start at a line boundary so it does not open mid-word.
    clipped = transcript[-max_chars:]
    _, newline, rest = clipped.partition("\n")
    return rest if newline else clipped
