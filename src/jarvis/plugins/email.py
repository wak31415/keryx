"""The `check_email` plugin: a question about their email, answered in about five seconds.

One Gmail pass and one model call (`integrations.gmail`), rather than a subagent reading the
inbox for minutes. It needs the PIN, as `recall` does: the mail is not in the briefing, and
the question steers what is read. Its settings are `check_email.toml` (`model`, `effort`);
the sign-in is `jarvis auth login gmail`, and the answer comes from the bundled `claude`
CLI, so a machine without either refuses the file with the reason, and `jarvis tools` and
`doctor` say which.
"""

import logging
from pathlib import Path

from jarvis import plugins
from jarvis.integrations.gmail import (
    ANSWER_TIMEOUT_S,
    EmailError,
    build_email_reader,
    email_problem,
)

log = logging.getLogger("jarvis.plugins.email")

TOOL = "check_email"

DESCRIPTION = (
    "Answer a question about their email in about five seconds, rather than dispatching it. "
    "Needs the PIN — call it anyway and let it ask. With day it reads that whole day, one "
    "entry per thread, leaving out "
    'threads they already answered: "anything I need to do from today\'s email", "what came '
    'in yesterday". Without day it searches Gmail and reads the few newest matches in full: '
    '"did Susan answer about the kickoff", "when is the camera-ready due", "anything from '
    'the bank this week". Put what they named into gmail_query and their question, in their '
    'words, into question. Say "one moment", call it, then say the answer as it comes back, '
    "once, and stop — no preamble, no summary of the summary, and no caveats about threads "
    "or replies. Replying, attachments, and anything that needs more than a handful of "
    "emails go to dispatch_task instead."
)

PARAMETERS = {
    "type": "object",
    "properties": {
        "question": {"type": "string", "description": "What they asked, in their words."},
        "gmail_query": {
            "type": "string",
            "description": "Gmail search terms for what they asked about: from:name, "
            "subject:word, is:unread, newer_than:7d, or plain words. Leave it out to search "
            "on the question itself, or with day for everything that day.",
        },
        "day": {
            "type": "string",
            "enum": ["today", "yesterday"],
            "description": "Only when they asked about a whole day's email (\"what do I need "
            "to do from today's email\"). Leave it out for a question about something in "
            "particular.",
        },
    },
    "required": ["question"],
}


def check_email_tool(config_path: Path, *, reader=None):
    """The tool `check_email.py` defines, configured by the TOML at `config_path`."""
    from jarvis.tools.custom import CustomTool, ToolUnavailable

    settings = plugins.loading_settings()
    try:
        values = plugins.read_config_file(TOOL, config_path)
    except plugins.PluginConfigError as error:
        raise ToolUnavailable(str(error)) from None
    if reader is None:
        if problem := email_problem(settings):
            raise ToolUnavailable(problem)
        reader = build_email_reader(settings, model=values["model"], effort=values["effort"])

    async def check_email(ctx, arguments: dict) -> dict:
        question = str(arguments.get("question") or "").strip()
        query = str(arguments.get("gmail_query") or "").strip() or None
        day = str(arguments.get("day") or "").strip().lower() or None
        if day not in (None, "today", "yesterday"):
            return {"error": "day is today or yesterday; for anything else, use gmail_query"}
        if not (question or query or day):
            return {"error": "question is required: say what they want to know"}
        try:
            return await reader.ask(question, query=query, day=day)
        except EmailError as exc:
            log.warning("check_email failed: %s (%s)", exc.code, exc.detail)
            return {"status": exc.code, "message": exc.spoken}

    return CustomTool(
        name=TOOL,
        description=DESCRIPTION,
        parameters=PARAMETERS,
        handler=check_email,
        needs_pin=True,
        # The reader stops itself at ANSWER_TIMEOUT_S with a sentence to say; this is only
        # the backstop behind it, so it must not win the race.
        timeout_s=ANSWER_TIMEOUT_S + 5,
    )
