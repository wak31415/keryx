"""What may be written down about a call, and in what shape.

A phone number identifies a person. Keryx's logs go to `STATE_DIR/logs/keryx.log`,
to the service manager's journal, and into the terminal of whoever is debugging — none of
which is the right home for the owner's number or a caller's, and the last four digits are
enough to tell two callers apart while reading a log. Everything that writes a number to a
log or a terminal goes through here.

So does everything that turns logging on (`configure_logging`), because a library can
write down what Keryx's own lines are careful not to: `httpx` logs the whole address of
every request at INFO, and for a Google request that is somebody's email address (a
calendar's id), the words that were searched for, and the ids of files; `websockets` logs
every handshake header and both ends of every frame at DEBUG, and for the Realtime socket
that is the OpenAI key and whatever was said on the call.

uvicorn, the phone server, is the third: at DEBUG it logs every frame of the Twilio media
socket (a keypad digit of the PIN, the caller's number), and at INFO its access log writes
each request's whole address, which for a report link ends in the token that opens it.
`uvicorn_log_level` and `drop_queries_from_the_access_log` are what `keryx serve` runs it
through.
"""

import logging

#: Every log line Keryx writes, the service's and the restart watchdog's alike.
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

#: The HTTP and websocket clients' own loggers. Held at WARNING whatever `LOG_LEVEL` says,
#: DEBUG included: their INFO and DEBUG lines are request addresses and headers, and for
#: `websockets` (the parent of `websockets.client`, `.server` and `.protocol`) the
#: `Authorization` header of the Realtime handshake and both ends of every frame.
HTTP_LOGGERS = ("httpx", "httpcore", "websockets")

#: uvicorn's request log: one line per request, `client - "METHOD path?query HTTP/v" status`.
ACCESS_LOGGER = "uvicorn.access"
#: Where the address sits among the five arguments uvicorn hands that line.
_ACCESS_ARGS, _ACCESS_PATH = 5, 2


def configure_logging(level: int | str) -> None:
    """Turn logging on at `level`, the one way every entry point does.

    The HTTP and websocket clients are kept to warnings. What a request was for is logged
    by whoever made it, in words that name no one (`keryx.integrations.google_api`).
    """
    logging.basicConfig(level=level, format=LOG_FORMAT)
    quiet_http_loggers()


def quiet_http_loggers() -> None:
    """Hold every logger in `HTTP_LOGGERS` to warnings. Safe to call any number of times.

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


def uvicorn_log_level(level: str) -> str:
    """What `LOG_LEVEL` is for uvicorn: the same level, but never below `info`.

    uvicorn hands its `uvicorn.error` logger to the websocket protocol under the Twilio
    media socket, which at DEBUG logs every handshake header and both ends of every frame:
    each keypad digit as it is pressed, which is the PIN, and the caller's number in the
    frame that opens the stream. `LOG_LEVEL=DEBUG` is for Keryx's own lines, which say that
    a digit arrived and never which.
    """
    return "info" if level.upper() == "DEBUG" else level.lower()


class _WithoutQuery(logging.Filter):
    """Cuts the query string out of an access line. It never drops a line or raises: a
    record that is not the five arguments uvicorn logs is passed on as it came."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == _ACCESS_ARGS:
            path = args[_ACCESS_PATH]
            if isinstance(path, str) and "?" in path:
                before, after = args[:_ACCESS_PATH], args[_ACCESS_PATH + 1 :]
                record.args = (*before, path.partition("?")[0], *after)
        return True


#: One instance, so asking twice adds it once (`Logger.addFilter` skips one it has).
_WITHOUT_QUERY = _WithoutQuery()


def drop_queries_from_the_access_log() -> None:
    """Keep the access log, without the query string of any address in it.

    The access log is how a Twilio webhook that never arrived is told from one that was
    refused, so it stays. But `GET /reports/<id>?t=<token>` is a link whose query is the
    whole of its protection, and the access line would write it down on every fetch. No
    route of Keryx's has a query worth reading back in a log. Safe to call any number of
    times, and it is called after uvicorn has configured its own logging.
    """
    logging.getLogger(ACCESS_LOGGER).addFilter(_WITHOUT_QUERY)
