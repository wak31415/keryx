"""What the Mac app must never show a person: a command, a slash command, a traceback, or a
path on this machine. Written out here rather than read from `keryx.doctor.SHELL_TEXT`, so a
word taken off that list does not quietly pass. A doctor check's `detail`, the setup plan's
`why` and a stream's error `message` are each held to it; the terminal's half is elsewhere
(`hint`, an error's `detail`)."""

import re

NOT_FOR_THE_APP = ("`", "keryx ", "brew ", "/login", "Traceback", "scripts/", "uv ")
#: A path: a word that starts at the root or at home. A URL's slashes are not one.
A_PATH = re.compile(r"(?:^|[\s(\[\"'])~?/(?!/)\S")


def for_the_app(text: str) -> bool:
    return not any(word in text for word in NOT_FOR_THE_APP) and not A_PATH.search(text)
