"""What may be written down about a call, and in what shape.

A phone number identifies a person. Keryx's logs go to `STATE_DIR/logs/keryx.log`,
to the service manager's journal, and into the terminal of whoever is debugging — none of
which is the right home for the owner's number or a caller's, and the last four digits are
enough to tell two callers apart while reading a log. Everything that writes a number to a
log or a terminal goes through here.

So does everything that turns logging on (`configure_logging`), because a library can
write down what Keryx's own lines are careful not to: `httpx` logs the whole address of
every request at INFO, and for a Google request that is somebody's email address (a
calendar's id), the words that were searched for, and the ids of files.
"""

import logging

#: Every log line Keryx writes, the service's and the restart watchdog's alike.
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

#: The HTTP clients' own loggers. Held at WARNING whatever `LOG_LEVEL` says, DEBUG
#: included: their INFO and DEBUG lines are request addresses and headers.
HTTP_LOGGERS = ("httpx", "httpcore")


def configure_logging(level: int | str) -> None:
    """Turn logging on at `level`, the one way every entry point does.

    The HTTP clients are kept to warnings. What a request was for is logged by whoever
    made it, in words that name no one (`keryx.integrations.google_api`).
    """
    logging.basicConfig(level=level, format=LOG_FORMAT)
    quiet_http_loggers()


def quiet_http_loggers() -> None:
    """Hold the HTTP clients' loggers to warnings. Safe to call any number of times.

    `configure_logging` does it for every entry point. Whatever is about to send a
    request whose address is private does it again for itself (the Google request layer),
    so the rule does not depend on how the process around it set logging up.
    """
    for name in HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def mask_number(number: str | None) -> str:
    """A phone number as it may appear in a terminal or a log: last four digits only."""
    if not number:
        return "nobody"
    return f"…{number[-4:]}" if len(number) > 4 else number
