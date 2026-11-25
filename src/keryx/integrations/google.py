"""The Google account: what one sign-in asks for, whose OAuth client asks, and what it leaves.

Everything Google in Keryx hangs off one sign-in (`keryx auth login google`, the flow is in
`keryx.setup.google`), and this module is the four facts the rest reads:

- **`SERVICES`** is the only place a scope is written down: each service, the scopes it
  requests, and what granting them enables. A service is *connected* when every scope it
  needs was granted, because Google lets the owner untick any of them on the consent screen.
- **`resolve_client`** is the OAuth client a sign-in uses, which is always the owner's own
  (`Settings.google_oauth_client`). Keryx ships none: Google's API Terms say "Developer
  credentials may not be embedded in open source projects".
- **`GoogleToken`** is `DATA_DIR/google_token.json` (0600): the refresh token, the client
  that minted it, the scopes Google literally granted, and the account's address. It is the
  one token Keryx owns. The Google voice tools read it, through the one request in
  `keryx.integrations.google_api`.
- **`mcp_server`** is how the coding agents get the same sign-in: `export_for_mcp` copies
  the token into the credential file the `workspace-mcp` server reads, and the server is
  started pinned, on the connected services only, with no OAuth client in its environment,
  so it can use the token and can never start a consent flow of its own.

`WORKSPACE_MCP_VERSION`, `MCP_SCOPED_TOOLS` and `SERVICES` move together: the tools a
version has, and the scopes each one checks for, were read from that version's source.
"""

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from keryx.config import Settings, write_private

log = logging.getLogger("keryx.integrations.google")

TOKEN_URL = "https://oauth2.googleapis.com/token"
_AUTH = "https://www.googleapis.com/auth/"

#: Who signed in. Always requested, and Google never lets these be unticked.
IDENTITY_SCOPES = ("openid", _AUTH + "userinfo.email")

GMAIL_MODIFY = _AUTH + "gmail.modify"
CALENDAR_READONLY = _AUTH + "calendar.readonly"
CALENDAR_EVENTS = _AUTH + "calendar.events"
DRIVE_READONLY = _AUTH + "drive.readonly"
DRIVE_FILE = _AUTH + "drive.file"
DOCUMENTS = _AUTH + "documents"
SPREADSHEETS = _AUTH + "spreadsheets"
PRESENTATIONS = _AUTH + "presentations"

#: Scopes Keryx never requests, named only because a sign-in made before this one may hold
#: them: the Gmail-only token (`gmail_token.json`), and workspace-mcp's own consent.
GMAIL_READONLY = _AUTH + "gmail.readonly"
GMAIL_SETTINGS = _AUTH + "gmail.settings.basic"
CALENDAR_FULL = _AUTH + "calendar"
DRIVE_FULL = _AUTH + "drive"

#: A broader scope stands in for the narrower ones under it, as Google's APIs treat them.
COVERS = {
    GMAIL_MODIFY: (GMAIL_READONLY,),
    CALENDAR_FULL: (CALENDAR_READONLY, CALENDAR_EVENTS),
    DRIVE_FULL: (DRIVE_READONLY, DRIVE_FILE),
}


@dataclass(frozen=True)
class Service:
    """One thing a Google sign-in can connect."""

    label: str
    #: What it requests of its own. The narrowest set the agents' tools work with: every
    #: one has to be justified to Google, so none is here for convenience.
    scopes: tuple[str, ...]
    #: What granting it enables, as one clause.
    enables: str
    #: Services whose scopes it needs as well: a document is found and created through Drive.
    needs: tuple[str, ...] = ()


SERVICES: dict[str, Service] = {
    "gmail": Service(
        "Gmail",
        (GMAIL_MODIFY,),
        "agents read, label, draft and send mail, and email answers on a call",
    ),
    "calendar": Service(
        "Calendar",
        (CALENDAR_READONLY, CALENDAR_EVENTS),
        "agents see your calendars and create or change events, and calendar answers on a "
        "call",
    ),
    "drive": Service(
        "Drive",
        (DRIVE_READONLY, DRIVE_FILE),
        "agents find and read your files, and create or change the ones Keryx made, and "
        "document answers on a call",
    ),
    "docs": Service("Docs", (DOCUMENTS,), "agents read and edit documents", needs=("drive",)),
    "sheets": Service(
        "Sheets", (SPREADSHEETS,), "agents read and edit spreadsheets", needs=("drive",)
    ),
    "slides": Service(
        "Slides", (PRESENTATIONS,), "agents read and edit presentations", needs=("drive",)
    ),
}

#: The `workspace-mcp` release the agents' Google tools come from. Pinned exactly, because
#: `MCP_SCOPED_TOOLS` below was read from this version's tool decorators.
WORKSPACE_MCP_VERSION = "2.6.0"
#: Tools of that release that check for a scope `SERVICES` does not request, with the
#: service they belong to: comments want all of Drive, filters want Gmail's settings, and
#: creating or deleting a whole calendar wants all of Calendar. Each is switched off unless
#: the token happens to hold that scope (an older sign-in's may), so an agent never meets a
#: tool whose only answer is "authorize again".
MCP_SCOPED_TOOLS: dict[str, tuple[str, str]] = {
    "list_gmail_filters": ("gmail", GMAIL_SETTINGS),
    "manage_gmail_filter": ("gmail", GMAIL_SETTINGS),
    "manage_calendar": ("calendar", CALENDAR_FULL),
    "manage_document_comment": ("docs", DRIVE_FULL),
    "manage_spreadsheet_comment": ("sheets", DRIVE_FULL),
    "manage_presentation_comment": ("slides", DRIVE_FULL),
}
#: The server's own consent flow. Keryx signs in; the server only ever uses the result.
MCP_NEVER_TOOLS = ("start_google_auth",)
#: Where that release looks for an OAuth client of its own. The server is started through
#: `env -u NAME …`, which takes each out of its environment whatever the parent's holds:
#: it inherits Keryx's, where the owner may keep a client. Not left to an empty value in
#: the server's `env`, which an agent's CLI is free to drop on the way.
MCP_UNSET_ENV = (
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "GOOGLE_CLIENT_SECRET_PATH",
    "GOOGLE_CLIENT_SECRETS",
)

TOKEN_FILE = "google_token.json"
#: A sign-in whose link is out and whose answer has not come back: its verifier and state.
PENDING_FILE = "google_signin.json"
#: The directory `workspace-mcp` reads its credentials from, under `DATA_DIR`.
MCP_CREDENTIALS_DIR = "google"


# ------------------------------------------------------------------------------ scopes


def covered(scopes: Any) -> frozenset[str]:
    """`scopes` and every narrower scope one of them stands in for."""
    held = {scope for scope in scopes or () if isinstance(scope, str)}
    for broad, narrow in COVERS.items():
        if broad in held:
            held.update(narrow)
    return frozenset(held)


def needed_scopes(service: str) -> tuple[str, ...]:
    """Every scope `service` needs: its own, and those of the services it builds on."""
    spec = SERVICES[service]
    scopes = [scope for name in spec.needs for scope in SERVICES[name].scopes]
    return tuple(dict.fromkeys([*scopes, *spec.scopes]))


def requested_scopes(services: Any) -> tuple[str, ...]:
    """What a sign-in for `services` asks Google for."""
    scopes = [scope for name in services for scope in needed_scopes(name)]
    return tuple(dict.fromkeys([*IDENTITY_SCOPES, *scopes]))


def connected_services(scopes: Any) -> tuple[str, ...]:
    """The services whose every scope is among `scopes`, in the table's order."""
    held = covered(scopes)
    return tuple(name for name in SERVICES if all(s in held for s in needed_scopes(name)))


def holds(scopes: Any, needed: Any) -> bool:
    """Whether `scopes` grant every scope in `needed`, a broader one standing in for the
    narrower ones under it. What one voice tool asks, where a *service* being connected
    asks for everything the agents' tools use."""
    held = covered(scopes)
    return all(scope in held for scope in needed)


def reads_gmail(scopes: Any) -> bool:
    """Whether `scopes` let Gmail be read: this sign-in's `gmail.modify`, or an older
    Gmail-only sign-in's `gmail.readonly`."""
    return holds(scopes, (GMAIL_READONLY,))


def labels(services: Any) -> str:
    """`Gmail, Calendar and Drive`, for a sentence."""
    names = [SERVICES[name].label for name in services]
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def parse_services(text: str) -> tuple[str, ...]:
    """`gmail,calendar` as service names, in the table's order; ValueError names a wrong one."""
    wanted = [part.strip().lower() for part in text.split(",") if part.strip()]
    unknown = [name for name in wanted if name not in SERVICES]
    if unknown or not wanted:
        raise ValueError(
            f"{', '.join(unknown) or 'nothing'} is not a Google service; "
            f"choose from: {', '.join(SERVICES)}"
        )
    return tuple(name for name in SERVICES if name in wanted)


# ------------------------------------------------------------------------------ client


@dataclass(frozen=True)
class OAuthClient:
    """The OAuth client a sign-in uses: the owner's own, made in their Google Cloud project."""

    client_id: str
    #: Empty for a client file that carries none; then it is left out of every request.
    client_secret: str


def resolve_client(settings: Settings) -> OAuthClient | None:
    """The owner's own OAuth client; None when they have not made one yet.

    There is no other: Keryx never ships a client of its own, because Google's API Terms
    forbid embedding developer credentials in an open source project.
    """
    own = settings.google_oauth_client()
    return OAuthClient(*own) if own is not None else None


# ------------------------------------------------------------------------------- token


@dataclass(frozen=True)
class GoogleToken:
    """What a sign-in left: the one Google credential Keryx owns."""

    refresh_token: str
    client_id: str
    client_secret: str
    #: Exactly what Google said it granted, which may be less than was asked for.
    scopes: tuple[str, ...]
    email: str = ""
    token_uri: str = TOKEN_URL

    @property
    def connected(self) -> tuple[str, ...]:
        return connected_services(self.scopes)

    @property
    def reads_gmail(self) -> bool:
        return reads_gmail(self.scopes)

    def as_dict(self) -> dict[str, Any]:
        return {
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "token_uri": self.token_uri,
            "scopes": list(self.scopes),
            "email": self.email,
        }


def token_path(settings: Settings) -> Path:
    return settings.data_dir / TOKEN_FILE


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _token(data: dict[str, Any] | None, *, email: str = "") -> GoogleToken | None:
    """A credential file's contents as a token; None unless it can refresh."""
    if not data or not all(
        isinstance(data.get(key), str) and data[key] for key in ("refresh_token", "client_id")
    ):
        return None
    scopes = data.get("scopes")
    return GoogleToken(
        refresh_token=data["refresh_token"],
        client_id=data["client_id"],
        client_secret=str(data.get("client_secret") or ""),
        scopes=tuple(s for s in scopes if isinstance(s, str)) if isinstance(scopes, list) else (),
        email=str(data.get("email") or email),
        token_uri=str(data.get("token_uri") or TOKEN_URL),
    )


def read_token(settings: Settings) -> GoogleToken | None:
    """Keryx's Google token; None when nobody has signed in, or the file is unusable."""
    return _token(_read_json(token_path(settings)))


def token_from(path: Path, *, email: str = "") -> GoogleToken | None:
    """Any credential file with a refresh token in it, as a token: an older sign-in's, or
    one the agents' MCP server holds. None when it could not refresh."""
    return _token(_read_json(path), email=email)


def write_token(settings: Settings, token: GoogleToken) -> Path:
    """Save `token` as the one Google credential, atomically, readable by the owner alone."""
    return write_private(token_path(settings), json.dumps(token.as_dict()))


# ------------------------------------------------------------- the agents' MCP server


def mcp_credentials_dir(settings: Settings) -> Path:
    return settings.data_dir / MCP_CREDENTIALS_DIR


def mcp_credential_path(settings: Settings, email: str) -> Path:
    """The file `workspace-mcp` reads for `email`: it names it by the quoted address."""
    return mcp_credentials_dir(settings) / f"{quote(email, safe='@._-')}.json"


#: What makes an exported credential the same sign-in as Keryx's token.
_IDENTITY_KEYS = ("refresh_token", "client_id", "client_secret", "token_uri")


def export_for_mcp(settings: Settings, token: GoogleToken, *, only: bool = False) -> Path | None:
    """Give the agents' MCP server `token`, as the credential file it reads. None without
    an address to name it by.

    Written only when the file is missing or is a different sign-in: the server refreshes
    the access token and writes it back to this file, and replacing it on every task would
    throw that away. `token` and `expiry` start null, which makes its first use a refresh.

    `only`, which a new sign-in passes, also removes every other account's credential from
    the directory. The server's tools take an address as an argument, so a credential left
    there for the account signed in before would still be one an agent could act as.
    """
    if not token.email:
        return None
    path = mcp_credential_path(settings, token.email)
    if only:
        for other in credential_files(settings):
            if other != path:
                other.unlink(missing_ok=True)
    wanted = {**{key: getattr(token, key) for key in _IDENTITY_KEYS}, "scopes": list(token.scopes)}
    found = _read_json(path) or {}
    same = all(found.get(key) == wanted[key] for key in _IDENTITY_KEYS) and set(
        found.get("scopes") or ()
    ) == set(token.scopes)
    if not same:
        write_private(path, json.dumps({"token": None, **wanted, "expiry": None}, indent=2))
    return path


#: What the server keeps beside its credentials that is not one (its `list_users` skips it).
_NOT_CREDENTIALS = frozenset({"oauth_states"})


def credential_files(settings: Settings) -> dict[Path, str]:
    """Each credential file in the server's directory, with the address it is named for.

    As the server's own `list_users` reads the directory: a `.json` file whose name is an
    address, quoted or not, and not its own bookkeeping.
    """
    directory = mcp_credentials_dir(settings)
    found: dict[Path, str] = {}
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        address = unquote(path.stem)
        if "@" in address and path.stem not in _NOT_CREDENTIALS:
            found[path] = address
    return found


def _older_mcp_credential(settings: Settings) -> tuple[Path, GoogleToken] | None:
    """A credential `workspace-mcp` wrote itself, before Keryx signed in for it.

    The one for `USER_GOOGLE_EMAIL` when that names one: by the address, or by the name an
    older server gave the file, with every odd character an underscore. Then the address
    handed back is the saved one, since the server checks it against the account. When the
    saved address names none (an alias, or nothing saved), the only credential there is.
    Read, never written and never adopted as Keryx's token: its scopes are whatever that
    flow asked for.
    """
    files = credential_files(settings)
    wanted = (settings.user_google_email or "").strip()
    sanitised = re.sub(r"[^a-zA-Z0-9@._-]", "_", wanted).lower()
    named = {
        path: wanted if address.lower() != wanted.lower() else address
        for path, address in files.items()
        if wanted and address.lower() in (wanted.lower(), sanitised)
    }
    chosen = named or files
    if len(chosen) != 1:
        return None
    [(path, address)] = chosen.items()
    token = _token(_read_json(path), email=address)
    return (path, token) if token is not None else None


@dataclass(frozen=True)
class McpServer:
    """The agents' Google tools: how the server is started, and what it must never say."""

    config: dict[str, Any]
    #: The services its tools cover, for the sentence that tells a subagent what it has.
    services: tuple[str, ...]
    #: The credential file it reads, and writes its refreshed access token back to.
    credentials: Path
    #: Signed in by `workspace-mcp` itself, before Keryx had a sign-in of its own.
    older: bool = False

    @property
    def secrets(self) -> tuple[str, ...]:
        """Every credential in that file *now*, to be kept out of anything logged or
        spoken. Read each time it is asked for: the file starts with no access token, and
        the server mints one and writes it back while a task is running."""
        stored = _read_json(self.credentials) or {}
        found = [stored.get(key) for key in ("refresh_token", "client_secret", "token")]
        return tuple(dict.fromkeys(s for s in found if isinstance(s, str) and s))


def mcp_server(settings: Settings, *, export: bool = True) -> McpServer | None:
    """The `workspace-mcp` server a subagent gets, or None when it would have nothing to use.

    `export=False` only asks: nothing is written, for a command that reports and does not
    start anything.

    None with `GOOGLE_WORKSPACE_MCP` off, and with nothing connected: a Google server that
    can only answer "authorize first" is worse than none. With Keryx's token, the token is
    exported first. Without one, a credential an older install left in the server's own
    directory keeps working as it did, with the tools its scopes cover.
    """
    if not settings.google_workspace_mcp:
        return None
    token, older = read_token(settings), False
    if token is not None and not token.client_secret:
        # The server refreshes through a library that cannot leave a secret out: it would
        # send an empty one, which Keryx never does. Email answers still work.
        return None
    path = None
    if token is not None and export:
        path = export_for_mcp(settings, token)
    elif token is not None and token.email:
        path = mcp_credential_path(settings, token.email)
    if path is None:
        found = _older_mcp_credential(settings)
        if found is None:
            return None
        (path, token), older = found, True
    assert token is not None
    services = token.connected
    if not services:
        return None
    held = covered(token.scopes)
    disabled = [
        tool
        for tool, (service, scope) in MCP_SCOPED_TOOLS.items()
        if service in services and scope not in held
    ]
    args = [
        *(part for name in MCP_UNSET_ENV for part in ("-u", name)),
        "uvx", "--from", f"workspace-mcp=={WORKSPACE_MCP_VERSION}", "workspace-mcp",
        "--tools", *services,
        "--transport", "stdio",
        "--single-user",
        "--disabled-tools", *MCP_NEVER_TOOLS, *disabled,
    ]  # fmt: skip
    # No OAuth client and no redirect address: with neither, a tool the token cannot serve
    # is an error, never a consent flow started behind a task nobody is watching. (On the
    # way to that error this release starts its own callback listener on localhost:8000,
    # for as long as the task's server lives; with no client it can complete nothing.)
    # Nothing here is a secret, and must not be: an agent's CLI is handed this whole
    # config on its command line.
    env = {
        "WORKSPACE_MCP_CREDENTIALS_DIR": str(path.parent),
        "USER_GOOGLE_EMAIL": token.email,
    }
    return McpServer(
        config={"type": "stdio", "command": "env", "args": args, "env": env},
        services=services,
        credentials=path,
        older=older,
    )
