"""`jarvis setup-gmail`: sign Jarvis in to Gmail, read-only, for `check_email`.

Two steps, because the machine Jarvis runs on usually has no browser, and a `!` command in
Claude Code has no stdin to paste into:

- `jarvis setup-gmail` prints a Google consent link (read-only Gmail, PKCE). Open it on
  any device and approve. You land on `http://localhost:1/?...`, which does not load.
- `jarvis setup-gmail --finish 'URL'` takes that whole URL, checks its `state`, exchanges
  its one-time `code` (with the PKCE verifier, which never left this machine) for a refresh
  token, and writes `data_dir/gmail_token.json` at 0600.

Between the two, the verifier and state wait in `data_dir/gmail_signin.json` (0600), which
finishing deletes. The OAuth client is the one `setup-google` uses
(`Settings.google_oauth_client`: the id/secret pair, else `.secrets/client_secret.json`).

A Google Cloud app still in "Testing" issues refresh tokens that stop working after seven
days; `check_email` then says it has been signed out. Publishing the app ("In production";
unverified is fine for your own account) is what makes the sign-in last.
"""

import base64
import hashlib
import json
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from jarvis.config import Settings, secure_dir, secure_file
from jarvis.integrations.gmail import SCOPE, TOKEN_URL, token_path

AUTH_URL = "https://accounts.google.com/o/oauth2/auth"
#: Where Google sends the browser afterwards. Nothing listens there: the code is read out
#: of the address bar instead, so this works from a phone while Jarvis runs headless.
REDIRECT_URI = "http://localhost:1"
PENDING_FILE = "gmail_signin.json"


class GmailSetupError(RuntimeError):
    """A sign-in that cannot go on, in one sentence to print."""


def _pending_path(settings: Settings) -> Path:
    return settings.data_dir / PENDING_FILE


def _write_private(path: Path, text: str) -> None:
    secure_dir(path.parent)
    path.touch(mode=0o600, exist_ok=True)
    secure_file(path)
    path.write_text(text, encoding="utf-8")


def _client(settings: Settings) -> tuple[str, str]:
    client = settings.google_oauth_client()
    if client is None:
        raise GmailSetupError(
            "no Google OAuth client: set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET, "
            f"or put the client JSON at {settings.google_client_secrets_file}"
        )
    return client


def start_signin(settings: Settings) -> str:
    """The consent URL; the verifier and state are kept for `finish_signin`."""
    client_id, _ = _client(settings)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    state = secrets.token_urlsafe(24)
    _write_private(
        _pending_path(settings), json.dumps({"state": state, "code_verifier": verifier})
    )
    return f"{AUTH_URL}?" + urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPE,
            "state": state,
            "code_challenge": challenge.decode().rstrip("="),
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
        }
    )


def finish_signin(
    settings: Settings,
    redirected_to: str,
    *,
    post: Callable[..., Any] = httpx.post,
) -> Path:
    """Exchange the code in `redirected_to` for a refresh token, saved at 0600."""
    pending_path = _pending_path(settings)
    try:
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GmailSetupError("no sign-in in progress: run `jarvis setup-gmail` first") from exc
    query = parse_qs(urlparse(redirected_to.strip()).query)
    if query.get("error"):
        raise GmailSetupError(f"Google said: {query['error'][0]}")
    if query.get("state", [""])[0] != pending["state"]:
        raise GmailSetupError(
            "that URL belongs to a different sign-in; run `jarvis setup-gmail` again"
        )
    code = query.get("code", [""])[0]
    if not code:
        raise GmailSetupError("that URL has no code in it; copy the whole address bar")
    if SCOPE not in query.get("scope", [SCOPE])[0].split():
        raise GmailSetupError("read access to Gmail was not granted; approve it and try again")

    client_id, client_secret = _client(settings)
    response = post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
            "code_verifier": pending["code_verifier"],
        },
        timeout=30,
    )
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200 or "refresh_token" not in body:
        raise GmailSetupError(
            f"Google refused the code ({body.get('error', response.status_code)}); "
            "run `jarvis setup-gmail` again"
        )
    path = token_path(settings)
    _write_private(
        path,
        json.dumps(
            {
                "refresh_token": body["refresh_token"],
                "client_id": client_id,
                "client_secret": client_secret,
                "token_uri": TOKEN_URL,
                "scopes": [SCOPE],
            }
        ),
    )
    pending_path.unlink(missing_ok=True)
    return path
