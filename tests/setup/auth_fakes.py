"""What `claude setup-token` and `codex login --device-auth` print, and the fakes that print
it: shared by the sign-in streams' tests and the Mac app's contract test.

The shapes are the ones the bundled CLIs were seen to print, or (after the code, which needs
a real sign-in) read from their own strings; see `keryx.setup.auth_stream`.
"""

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
        self.waits: list[float] = []
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
        self.waits.append(timeout)
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


#: The consent link workspace-mcp prints for Google for agents.
CONSENT = "https://accounts.google.com/o/oauth2/auth?response_type=code&client_id=cid&state=s"


#: Every secret Keryx's own environment may hold, by the name it travels under.
AMBIENT = {
    "OPENAI_API_KEY": "sk-proj-ambient", "CODEX_API_KEY": "sk-codex-ambient",
    "CODEX_ACCESS_TOKEN": "codex-access-ambient", "ANTHROPIC_API_KEY": "sk-ant-api-ambient",
    "ANTHROPIC_AUTH_TOKEN": "ant-auth-ambient", "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-ambient",
    "KERYX_PIN": "48291573", "JARVIS_PIN": "73829154", "TWILIO_AUTH_TOKEN": "twilio-ambient",
    "SLACK_BOT_TOKEN": "xoxb-ambient", "OPENAI_ADMIN_KEY": "sk-admin-ambient",
    "VOICE_API_KEY": "voice-ambient", "GOOGLE_OAUTH_CLIENT_SECRET": "gocspx-ambient",
    # Settings reads a variable whatever its case, so a child must not be lent one either.
    "keryx_pin": "59302684", "twilio_auth_token": "twilio-lower", "Slack_Bot_Token": "xoxb-mixed",
    "openai_api_key": "sk-proj-lower", "Codex_Access_Token": "codex-mixed",
}


def assert_holds_no_secret(env: dict[str, str]) -> None:
    secrets = set(AMBIENT.values())
    leaked = sorted(name for name, value in env.items() if value in secrets)
    assert not leaked, leaked  # the names alone: the environment itself is not printed
    # Codex reads these three from its environment: blank, so nothing inherited is lent.
    assert {env[name] for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN")} \
        == {""}
    assert not {"openai_api_key", "Codex_Access_Token"} & env.keys()
    assert env["KERYX_HARMLESS"] == "kept"
