"""The token on a report link: minted when the link is sent, checked when it is opened.

A finished task's full write-up is served from `/reports/{task_id}`, which is a public
route behind `cloudflared` and therefore reachable by anyone who knows the path. What
keeps it theirs is the `?t=` query parameter: an HMAC-SHA256 of the task id under
`report_secret`, so the ids are not enumerable and a link cannot be edited into another
task's report.

The two sides sit here because they belong to *each other*, not to either caller.
`notify/notifier.py` mints one on its way into an SMS and `server.py` checks one on its
way out of a query string, and until now the checking half lived with the minting half —
which meant a web route importing the delivery layer, the notifier's whole dependency
graph included, to compare two digests.
"""

import hashlib
import hmac

#: half a sha256, plenty against guessing and short enough for a URL
TOKEN_HEX_CHARS = 32


def report_token(task_id: int, secret: str) -> str:
    """The unguessable half of a report link: HMAC-SHA256 of the id under `secret`."""
    digest = hmac.new(secret.encode(), str(task_id).encode(), hashlib.sha256).hexdigest()
    return digest[:TOKEN_HEX_CHARS]


def verify_report_token(task_id: int, token: str, secret: str) -> bool:
    """True if `token` is the report token for `task_id` (constant-time compare).

    Compared as bytes: `hmac.compare_digest` refuses `str` operands with non-ASCII
    characters, and this one comes straight off a public query string.
    """
    expected = report_token(task_id, secret).encode()
    return hmac.compare_digest(expected, token.encode("utf-8", "surrogatepass"))
