"""What may be written down about a call, and in what shape.

A phone number identifies a person. Keryx's logs go to `STATE_DIR/logs/keryx.log`,
to the service manager's journal, and into the terminal of whoever is debugging — none of
which is the right home for the owner's number or a caller's, and the last four digits are
enough to tell two callers apart while reading a log. Everything that writes a number to a
log or a terminal goes through here.
"""

#: Every log line Keryx writes, the service's and the restart watchdog's alike.
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def mask_number(number: str | None) -> str:
    """A phone number as it may appear in a terminal or a log: last four digits only."""
    if not number:
        return "nobody"
    return f"…{number[-4:]}" if len(number) > 4 else number
