"""`keryx auth login NAME --json`'s four sign-ins, driven with fakes.

No agent's CLI runs here: a scripted child prints what `claude setup-token` and `codex login
--device-auth` print (the shapes are the ones the bundled CLIs were seen to print, see
`keryx.setup.auth_stream`), and the line the app passes back is a fake stdin. The rule
under test is the one with teeth: the token reaches the store and never the output.
"""

import dataclasses
import io
import json
import os
import threading

import pytest

from keryx.agents.registry import BACKENDS
from keryx.config.store import ConfigStore
from keryx.integrations.gmail import token_path
from keryx.setup import auth_stream, google
from keryx.setup.auth_stream import (
    CLAUDE_CHANGED,
    CODE_REFUSED,
    CODEX_CHANGED,
    CODEX_UNFINISHED,
    CODEX_UNREACHABLE,
    NO_ADDRESS,
    NO_CODE,
    TIMED_OUT,
    TOKEN_KEY,
    LoginEvents,
    StdinLines,
)

# --- what the CLIs print ------------------------------------------------------------------


def ink(text: str) -> str:
    """A line the way Ink draws it: each word placed by a cursor move, not a space."""
    column, out = 2, []
    for word in text.split(" "):
        out.append(f"\x1b[{column}G{word}")
        column += len(word) + 1
    return "".join(out) + "\r\r\n"


def link(url: str) -> str:
    """A link the way Ink draws it: an OSC 8 hyperlink around the same text, dimmed."""
    return f"\x1b]8;id=1hp4g7j;{url}\x07\x1b[38;5;246m{url}\x1b[39m\x1b]8;;\x07\r\r\n"


URL = (
    "https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-88ed-"
    "5944d1962f5e&response_type=code&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth"
    "%2Fcode%2Fcallback&scope=user%3Ainference&code_challenge=XTIPxqyiJ6K3l0-YcRJJ&"
    "code_challenge_method=S256&state=NbsRsB5KxumPSWIbX6Z5ebx0YOkB2j"
)
TOKEN = "sk-ant-oat01-" + "Zq9_x-" * 16 + "AA"
#: Something only the screen says: it must never reach the output either.
CANARY = "Welcome to Claude Code"
SPINNER = "\r\x1b[1C\x1b[1A\x1b[38;5;231m✢\x1b[39m\r\r\n"
SIGN_IN = [
    "\x1b7\x1b[r\x1b8\x1b[?25l\x1b[?2004h" + ink(f"{CANARY} v2.1.281"),
    ink("This will guide you through long-lived (1-year) auth token setup for your Claude "
        "account. Claude subscription required."),
    ink("· Opening browser to sign in…") + "\x1b[>0q\x1b[?u\x1b[c",
    SPINNER * 3,
    ink("Browser didn't open? Use the url below to sign in (c to copy)") + "\r\r\n" + link(URL),
    ink("Hold Shift (Option in iTerm2, Fn in Terminal.app) while selecting"),
    ink("Paste code here if prompted >"),
]
TOKEN_SHOWN = [
    ink("✓ Long-lived authentication token created successfully!"),
    ink("Your OAuth token (valid for 1 year):") + "\r\r\n",
    f"\x1b[2G\x1b[38;5;114m{TOKEN}\x1b[39m\r\r\n",
    ink("Store this token securely. You won't be able to see it again."),
    ink("Use this token by setting: export CLAUDE_CODE_OAUTH_TOKEN=<token>"),
]
DEVICE_URL = "https://auth.openai.com/codex/device"
DEVICE_CODE = "K7QF-2MXRA"
B, G, R = "\x1b[94m", "\x1b[90m", "\x1b[0m"
DEVICE_AUTH = (
    f"\nWelcome to Codex [v{G}0.157.1{R}]\n{G}OpenAI's command-line coding agent{R}\n\n"
    "Follow these steps to sign in with ChatGPT using device code authorization:\n\n"
    f"1. Open this link in your browser and sign in to your account\n   {B}{DEVICE_URL}{R}\n\n"
    f"2. Enter this one-time code {G}(expires in 15 minutes){R}\n   {B}{DEVICE_CODE}{R}\n\n"
    f"{G}Continue only if you started this login in Codex. If a website or another person "
    f"gave you this code, cancel.{R}\n"
).replace("\n", "\r\n")

# --- the fakes ----------------------------------------------------------------------------

#: A step in a child's script: wait here until a line has been typed into it.
TYPED = object()


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FakeChild:
    """A child that prints its script a chunk per read, and then ends (or hangs)."""

    def __init__(self, steps, clock: Clock, *, code: int = 0, hang: bool = False) -> None:
        self.steps, self.clock, self.code, self.hang = list(steps), clock, code, hang
        self.typed: list[str] = []
        self.stopped = False

    def read(self, timeout: float) -> bytes | None:
        if self.stopped:
            return None
        if not self.steps or (self.steps[0] is TYPED and not self.typed):
            if self.steps or self.hang:
                self.clock.now += timeout
                return b""
            return None
        step = self.steps.pop(0)
        if step is TYPED:
            return b""
        if isinstance(step, BaseException):
            raise step
        return step.encode()

    def submit(self, line: str) -> None:
        self.typed.append(line)

    def wait(self, timeout: float) -> int | None:
        return self.code

    def stop(self) -> None:
        self.stopped = True


class FakeLines:
    """The app's stdin: these lines, then the end of input (or, `waiting`, nothing yet)."""

    def __init__(self, *lines: str, waiting: bool = False) -> None:
        self.lines, self.waiting = list(lines), waiting

    def get(self, timeout: float) -> str | None:
        if self.lines:
            return self.lines.pop(0)
        if self.waiting:
            raise TimeoutError
        return None


class Run:
    """One stream, with what it printed and what it spawned."""

    def __init__(self, child: FakeChild | None = None) -> None:
        self.out: list[str] = []
        self.events = LoginEvents(self.out.append)
        self.child = child
        self.spawned: list[tuple[list[str], dict | None]] = []

    def spawn(self, argv, env):
        self.spawned.append((list(argv), env))
        return self.child

    def __call__(self, name, settings, *, lines=None, clock=None, **kwargs) -> int:
        return auth_stream.stream(
            name, settings, ConfigStore(), self.events,
            lines=lines or FakeLines(), spawn=self.spawn, clock=clock or Clock(), **kwargs,
        )

    @property
    def lines(self) -> list[dict]:
        return [json.loads(line) for line in self.out]

    @property
    def text(self) -> str:
        return "\n".join(self.out)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def agents(every_agent_installed):
    """Both agents' CLIs at a made-up path; nothing ever runs them."""


def ending(run: Run) -> dict:
    endings = [line for line in run.lines if line["event"] in ("done", "error", "cancelled")]
    assert len(endings) == 1 and endings[0] == run.lines[-1], run.lines
    return endings[0]


# --- claude -------------------------------------------------------------------------------


def test_claude_opens_the_link_asks_for_the_code_and_stores_the_token(
    settings, agents, clock, monkeypatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-ambient")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-ambient")
    child = FakeChild([*SIGN_IN, TYPED, *TOKEN_SHOWN], clock)
    run = Run(child)

    code = run("claude", settings, lines=FakeLines("  code123#state456 \n"), clock=clock)

    assert code == 0
    assert run.lines == [
        {"event": "open", "url": URL},
        {"event": "code"},
        {"event": "done", "name": "claude", "stored": TOKEN_KEY,
         "message": run.lines[-1]["message"]},
    ]
    assert child.typed == ["code123#state456"]
    assert ConfigStore().stored()[TOKEN_KEY] == TOKEN
    assert TOKEN not in run.text and CANARY not in run.text
    argv, env = run.spawned[0]
    assert argv == ["/venv/bin/claude", "setup-token"]
    # The app opens the link; the CLI must not open a second tab, nor see a credential.
    assert env["BROWSER"] and "ANTHROPIC_API_KEY" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert child.stopped


def test_claude_reads_a_link_and_a_token_that_arrive_in_pieces(settings, agents, clock):
    whole = "".join([*SIGN_IN, "\x00", *TOKEN_SHOWN])
    before, after = whole.split("\x00")
    pieces = [before[i:i + 7] for i in range(0, len(before), 7)]
    tail = [after[i:i + 5] for i in range(0, len(after), 5)]
    child = FakeChild([*pieces, TYPED, *tail], clock)
    run = Run(child)

    assert run("claude", settings, lines=FakeLines("c#s"), clock=clock) == 0
    assert run.lines[0] == {"event": "open", "url": URL}
    assert ConfigStore().stored()[TOKEN_KEY] == TOKEN


def test_a_token_shown_before_any_code_is_asked_for_is_still_stored(settings, agents, clock):
    child = FakeChild([*SIGN_IN[:5], *TOKEN_SHOWN], clock)
    run = Run(child)

    assert run("claude", settings, lines=FakeLines(waiting=True), clock=clock) == 0
    assert [line["event"] for line in run.lines] == ["open", "done"]
    assert child.typed == []


@pytest.mark.parametrize(
    "screen",
    [
        # A prompt that is not the one expected, and no link at all.
        [SIGN_IN[0], ink("Select login method:"), ink("1. Claude account")],
        # A link to somewhere that is not Claude's sign-in.
        [SIGN_IN[0], link(URL.replace("claude.com", "claude.example.net")),
         ink("Paste code here if prompted >")],
        # The link, but never the question it is waiting on.
        [*SIGN_IN[:5], ink("Waiting for you to sign in…")],
    ],
    ids=["other-prompt", "foreign-link", "no-question"],
)
def test_a_changed_sign_in_says_so_and_shows_nothing_of_the_screen(
    settings, agents, clock, screen
):
    child = FakeChild(screen, clock, hang=True)
    run = Run(child)

    code = run("claude", settings, clock=clock)

    assert code == 1
    assert ending(run) == {"event": "error", "message": CLAUDE_CHANGED}
    assert CANARY not in run.text and "claude.example.net" not in run.text
    assert child.stopped
    assert clock.now >= auth_stream.START_TIMEOUT_S


def test_a_cli_that_ends_before_it_asks_anything_has_changed(settings, agents, clock):
    child = FakeChild([ink("error: unknown command 'setup-token'")], clock, code=1)
    run = Run(child)

    assert run("claude", settings, clock=clock) == 1
    assert ending(run)["message"] == CLAUDE_CHANGED
    assert "unknown command" not in run.text


def test_a_token_in_a_new_shape_is_neither_stored_nor_shown(settings, agents, clock):
    changed = [ink("Your OAuth token (valid for 1 year):"), ink("ctk_live_d81f00dfeedbeef")]
    child = FakeChild([*SIGN_IN, TYPED, *changed], clock)
    run = Run(child)

    assert run("claude", settings, lines=FakeLines("c#s"), clock=clock) == 1
    assert ending(run)["message"] == CLAUDE_CHANGED
    assert "ctk_live" not in run.text
    assert TOKEN_KEY not in ConfigStore().stored()


def test_a_code_claude_refuses_says_so(settings, agents, clock):
    refused = [ink("OAuth error: Invalid authorization code"), ink("Press Enter to retry.")]
    child = FakeChild([*SIGN_IN, TYPED, *refused], clock, hang=True)
    run = Run(child)

    assert run("claude", settings, lines=FakeLines("wrong#code"), clock=clock) == 1
    assert ending(run) == {"event": "error", "message": CODE_REFUSED}
    assert "Invalid authorization" not in run.text


def test_no_code_at_all_stops_the_sign_in(settings, agents, clock):
    child = FakeChild([*SIGN_IN, TYPED], clock)
    run = Run(child)

    assert run("claude", settings, lines=FakeLines("   "), clock=clock) == 1
    assert ending(run)["message"] == NO_CODE
    assert child.stopped and child.typed == []


def test_an_owner_who_never_answers_times_out(settings, agents, clock):
    child = FakeChild([*SIGN_IN, TYPED], clock)
    run = Run(child)

    assert run("claude", settings, lines=FakeLines(waiting=True), clock=clock) == 1
    assert ending(run) == {"event": "error", "message": TIMED_OUT}
    assert clock.now >= auth_stream.LOGIN_TIMEOUT_S
    assert child.stopped


def test_cancel_stops_the_child_and_goes_on_as_an_interrupt(settings, agents, clock):
    child = FakeChild([*SIGN_IN, KeyboardInterrupt()], clock)
    run = Run(child)

    with pytest.raises(KeyboardInterrupt):
        run("claude", settings, lines=FakeLines(waiting=True), clock=clock)
    assert child.stopped
    assert [line["event"] for line in run.lines] == ["open", "code"]


def test_a_token_the_store_refuses_is_not_quoted_back(settings, agents, clock, monkeypatch):
    def refuse(self, values, **kwargs):
        raise auth_stream.ConfigError(f"no: {values[TOKEN_KEY]}")

    monkeypatch.setattr(ConfigStore, "set", refuse)
    run = Run(FakeChild([*SIGN_IN, TYPED, *TOKEN_SHOWN], clock))

    assert run("claude", settings, lines=FakeLines("c#s"), clock=clock) == 1
    assert TOKEN not in run.text and "[redacted]" in ending(run)["message"]


def test_a_child_that_cannot_start_is_an_error(settings, agents, clock):
    run = Run()

    def broken(argv, env):
        raise FileNotFoundError(2, "No such file or directory")

    run.spawn = broken
    assert run("claude", settings, clock=clock) == 1
    assert "could not start" in ending(run)["message"]


def test_an_agent_that_is_not_installed_says_how(settings, clock, monkeypatch):
    monkeypatch.setattr("keryx.agents.registry.installed", lambda agent, settings=None: False)
    run = Run()

    assert run("codex", settings, clock=clock) == 1
    assert "uv sync --extra codex" in ending(run)["message"]
    assert run.spawned == []


def test_an_agent_with_no_cli_says_how(settings, agents, clock, monkeypatch):
    spec = BACKENDS["claude"]
    monkeypatch.setitem(BACKENDS, "claude", dataclasses.replace(spec, find_cli=lambda: None))
    run = Run()

    assert run("claude", settings, clock=clock) == 1
    assert "CLI is missing" in ending(run)["message"]


# --- codex --------------------------------------------------------------------------------


def test_codex_hands_over_its_link_and_code_and_finishes(settings, agents, clock):
    pieces = [DEVICE_AUTH[:90], DEVICE_AUTH[90:300], DEVICE_AUTH[300:],
              "Successfully logged in\r\n"]
    child = FakeChild(pieces, clock, code=0)
    run = Run(child)

    assert run("codex", settings, clock=clock) == 0
    assert run.lines[0] == {"event": "open", "url": DEVICE_URL, "code": DEVICE_CODE}
    assert ending(run)["name"] == "codex" and ending(run)["event"] == "done"
    assert run.spawned[0][0] == ["/venv/bin/codex", "login", "--device-auth"]
    assert child.stopped


@pytest.mark.parametrize(
    ("script", "code", "message"),
    [
        ([DEVICE_AUTH, "Error logging in with device code: expired\r\n"], 1, CODEX_UNFINISHED),
        (["Error logging in with device code: failed to request device code\r\n"], 1,
         CODEX_UNREACHABLE),
        (["Usage: codex login [OPTIONS]\r\nunexpected argument '--device-auth'\r\n"], 2,
         CODEX_CHANGED),
        ([DEVICE_AUTH.replace(DEVICE_URL, "https://auth.example.net/device")], 0,
         CODEX_CHANGED),
    ],
    ids=["unfinished", "unreachable", "changed", "foreign-link"],
)
def test_a_codex_sign_in_that_does_not_finish_says_which_way(
    settings, agents, clock, script, code, message
):
    run = Run(FakeChild(script, clock, code=code))

    assert run("codex", settings, clock=clock) == 1
    assert ending(run) == {"event": "error", "message": message}
    assert "expired" not in run.text and "Usage" not in run.text


def test_a_codex_that_prints_nothing_has_changed(settings, agents, clock):
    run = Run(FakeChild([ink("starting")], clock, hang=True))

    assert run("codex", settings, clock=clock) == 1
    assert ending(run)["message"] == CODEX_CHANGED


def test_a_codex_sign_in_nobody_finishes_times_out(settings, agents, clock):
    run = Run(FakeChild([DEVICE_AUTH], clock, hang=True))

    assert run("codex", settings, clock=clock) == 1
    assert ending(run)["message"] == TIMED_OUT
    assert clock.now >= auth_stream.CODEX_TIMEOUT_S


# --- gmail --------------------------------------------------------------------------------


@pytest.fixture
def client_file(tmp_path):
    client = tmp_path / "client.json"
    client.write_text('{"installed": {"client_id": "cid", "client_secret": "cs"}}')
    return client


def accepted(url, data, timeout):
    return type("R", (), {"status_code": 200, "json": lambda self: {"refresh_token": "r"}})()


def test_gmail_opens_the_link_and_finishes_with_the_address_in_one_stream(
    settings, client_file
):
    run = Run()
    pending = {}

    class Address(FakeLines):
        def get(self, timeout):
            url = run.lines[0]["url"]
            pending["state"] = url.split("state=")[1].split("&")[0]
            return (f"http://localhost:1/?state={pending['state']}&code=c"
                    "&scope=https://www.googleapis.com/auth/gmail.readonly\n")

    code = run("gmail", settings, lines=Address(), client_file=client_file, post=accepted)

    assert code == 0, run.lines
    assert run.lines[0]["event"] == "open"
    assert run.lines[0]["url"].startswith(google.AUTH_URL)
    assert ending(run) == {"event": "done", "name": "gmail",
                           "message": ending(run)["message"]}
    assert token_path(settings).is_file()
    assert ConfigStore().stored()["GOOGLE_CLIENT_SECRETS_FILE"].endswith(
        google.GOOGLE_CLIENT_FILE)


def test_gmail_with_no_address_stops(settings, client_file):
    run = Run()

    assert run("gmail", settings, client_file=client_file) == 1
    assert ending(run)["message"] == NO_ADDRESS


def test_gmail_waiting_too_long_times_out(settings, client_file):
    run = Run()

    assert run("gmail", settings, lines=FakeLines(waiting=True), client_file=client_file) == 1
    assert ending(run)["message"] == TIMED_OUT


def test_gmail_with_the_wrong_address_says_what_google_flow_said(settings, client_file):
    run = Run()

    code = run("gmail", settings, lines=FakeLines("http://localhost:1/?state=x&code=c"),
               client_file=client_file)

    assert code == 1
    assert "different sign-in" in ending(run)["message"]


def test_gmail_without_a_client_says_how_to_get_one(settings):
    run = Run()

    assert run("gmail", settings) == 1
    assert ending(run) == {"event": "error", "message": google.NO_CLIENT}


def test_a_client_file_that_is_not_one_is_refused(settings, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    run = Run()

    assert run("gmail", settings, client_file=bad) == 1
    assert "not a Google OAuth client file" in ending(run)["message"]


# --- google-workspace ---------------------------------------------------------------------

CONSENT = "https://accounts.google.com/o/oauth2/auth?response_type=code&client_id=cid&state=s"


def test_google_workspace_opens_the_consent_link_then_waits_for_the_credentials(
    settings, client_file
):
    ran = {}

    def workspace(settings, *, echo, timeout_s):
        ran["timeout_s"] = timeout_s
        echo(google.INSTRUCTIONS)
        echo(f"Authorization required. Open this URL: {CONSENT} and approve.")
        echo(f"again: {CONSENT}")
        echo("credentials written; checking the access they grant…")
        return True

    run = Run()
    code = run("google-workspace", settings, client_file=client_file, workspace=workspace)

    assert code == 0, run.lines
    assert run.lines[0] == {"event": "open", "url": CONSENT}
    assert [line["event"] for line in run.lines] == ["open", "done"]
    assert ConfigStore().stored()["GOOGLE_WORKSPACE_MCP"] is True
    assert ran["timeout_s"] == auth_stream.LOGIN_TIMEOUT_S


def test_google_workspace_that_fails_says_why(settings, client_file):
    def workspace(settings, *, echo, timeout_s):
        raise google.GoogleSetupError("no new credentials appeared")

    run = Run()

    assert run("google-workspace", settings, client_file=client_file, workspace=workspace) == 1
    assert ending(run) == {"event": "error", "message": "no new credentials appeared"}


def test_the_real_workspace_flow_is_the_default(settings):
    """Without a client it refuses before starting anything: the wizard's own refusal."""
    run = Run()

    assert run("google-workspace", settings) == 1
    assert ending(run)["message"] == google.NO_CLIENT


def test_a_name_that_is_not_a_sign_in_is_refused(settings):
    run = Run()

    assert run("twitter", settings) == 2
    assert ending(run)["event"] == "error"


# --- stdin --------------------------------------------------------------------------------


def test_stdin_lines_come_one_at_a_time_then_the_end():
    lines = StdinLines(io.StringIO("first\nsecond\n"))

    assert lines.get(5) == "first\n"
    assert lines.get(5) == "second\n"
    assert lines.get(5) is None
    assert lines.get(5) is None


def test_stdin_with_nothing_yet_times_out_rather_than_blocking():
    read, write = os.pipe()
    with os.fdopen(read) as stream, os.fdopen(write, "w") as writer:
        lines = StdinLines(stream)

        with pytest.raises(TimeoutError):
            lines.get(0.05)
        writer.write("late\n")
        writer.flush()
        assert lines.get(5) == "late\n"
    assert not any(t.name == "keryx-stdin" and t.is_alive() and not t.daemon
                   for t in threading.enumerate())


def test_a_codex_that_cannot_start_is_an_error(settings, agents, clock):
    run = Run()

    def broken(argv, env):
        raise PermissionError(13, "Permission denied")

    run.spawn = broken
    assert run("codex", settings, clock=clock) == 1
    assert ending(run)["message"] == "Codex's sign-in could not start (Permission denied)."
