"""The Codex backend: subagents on OpenAI's `openai-codex` Python SDK.

The SDK drives `codex app-server` — the CLI it bundles, never one on PATH — over JSON-RPC:
a *thread* is one conversation, and each `run()` is one *turn* on it, streamed as typed
notifications (recorded shapes in `tests/agents/fixtures/codex_app_*.jsonl`). `CodexAdapter`
turns those into the shared events `AdapterSession` (`agents/session.py`) runs every
backend's turn from:

- `item/started` for a command, an MCP call, a web search or a delegated agent is a tool
  line; `item/completed` for a file change is an edit line, and for an agent message is
  text — the last `final_answer` message is the report;
- `thread/tokenUsage/updated` carries each model call's tokens, summed per turn;
- `error` is a retry (logged quietly) or the terminal failure, and `turn/completed` says
  which of completed, interrupted or failed the turn came to.

A follow-up can go straight into the running turn (`steer`), and `interrupt()` is a real
`turn/interrupt`. The SDK client is injected (`client_factory`), so tests never start
`codex` (CLAUDE.md testing rule); `openai_codex` is imported lazily, because it is a
13,000-line generated module that `doctor` and `jarvis setup` have no use for.

What Claude gets from its SDK options, Codex gets from the thread:
- the rendered `subagent_suffix.md` as `developer_instructions` (re-sent on a resume);
- `bypassPermissions` as the full-access sandbox with approvals never asked for, because
  the phone PIN gates the dispatch, not the agent;
- MCP servers (`workspace-mcp` for Gmail and Calendar, the Slack server) as the thread's
  `mcp_servers` config, with their secrets forwarded by *name* through `env_vars` and the
  values in the app-server's environment only.

Credentials (verified against the bundled 0.157.1): the app-server ignores `CODEX_API_KEY`
in its environment, so the API-key tier logs in once, with the key on stdin, into a
`CODEX_HOME` of Jarvis's own (`data_dir/codex`, with the owner's config, instructions and
skills linked in); `CODEX_ACCESS_TOKEN` it *does* read from the environment, so the token
tier is that variable and nothing persisted; the stored login is the owner's own
`~/.codex`. The SDK hands the app-server a copy of Jarvis's whole environment, so every
credential variable not chosen is overridden with an empty value, which it treats as unset.

Codex has no `max_budget_usd` and no `max_turns`; the wall-clock cap every backend shares
(`SUBAGENT_TIMEOUT_S`, applied by the task manager) is what bounds it. It reports tokens,
not dollars, so `RunResult.cost_usd` stays None: on the ChatGPT plan a call has no price.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import subprocess
import threading
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from importlib import metadata
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Protocol

from jarvis.agents.auth import AuthMode, AuthSource, AuthStatus, redact
from jarvis.agents.base import SteerUnavailable, TokenUsage, google_mcp_server_config
from jarvis.agents.session import (
    AdapterRunner,
    AgentContext,
    AgentEvent,
    Done,
    FileEdit,
    Notice,
    SessionId,
    Text,
    ToolCall,
)
from jarvis.config import Settings, secure_dir, secure_file
from jarvis.config.files import claude_user_config
from jarvis.integrations.slack import mcp_server_config
from jarvis.tasks.models import Task

log = logging.getLogger("jarvis.agents.codex")

#: The model names that can be said out loud, and the Codex ids they mean. Blank is
#: `CODEX_MODEL`, and a blank `CODEX_MODEL` is Codex's own default, whatever that is today.
CODEX_MODELS = {
    "astra": "gpt-6-astra",
    "sol": "gpt-6-sol",
    "luna": "gpt-6-luna",
    "terra": "gpt-5.6-terra",
}

#: The distribution that carries the bundled `codex` binary.
CLI_PACKAGE = "openai-codex-cli-bin"
#: How long `codex login status` / `codex login --with-api-key` may take.
LOGIN_TIMEOUT_S = 30.0
#: Every variable the app-server would take a credential from. Whichever the chosen tier
#: does not use is set empty, so nothing inherited from Jarvis's own environment — the
#: voice model's `OPENAI_API_KEY` above all — is ever lent to Codex.
CREDENTIAL_VARIABLES = ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN")
#: The files in the owner's `~/.codex` that make Codex *their* Codex: linked into
#: Jarvis's own home for the API-key tier, so only the login differs. Not `hooks.json`:
#: hooks are the owner's automation, with side effects of their own.
SHARED_HOME_ENTRIES = ("config.toml", "AGENTS.md", "skills")
_LOGIN_STAMP = ".jarvis-login-sha256"
#: One login at a time: two tasks opening at once must not both log in to the same home.
_LOGIN_LOCK = threading.Lock()
#: An MCP server name is kept to a bare word, the only kind Codex config can always spell.
_BARE_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")
#: Global notifications worth a warning: they reach no turn, so the adapter logs them.
_WARNING_METHODS = ("warning", "configWarning", "deprecationNotice", "guardianWarning")


# ------------------------------------------------------------------------------ auth


def codex_home() -> Path:
    """The owner's Codex home: `CODEX_HOME`, else `~/.codex`."""
    return Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser()


def codex_cli() -> str | None:
    """The `codex` binary the SDK runs — the one it bundles — or None when either the SDK
    (the `codex` extra) or its binary is missing."""
    if not _importable("openai_codex"):
        return None
    try:
        from codex_cli_bin import bundled_codex_path
    except ImportError:
        return None
    try:
        return str(bundled_codex_path())
    except FileNotFoundError:
        return None


def _importable(module: str) -> bool:
    """Is `module` installed, without importing it (`openai_codex` is 13,000 lines)?"""
    return find_spec(module) is not None


def codex_cli_version() -> str | None:
    """The bundled CLI's version, for `doctor`."""
    try:
        return f"codex-cli {metadata.version(CLI_PACKAGE)}"
    except metadata.PackageNotFoundError:
        return None


def codex_stored_login(run: Callable[..., Any] = subprocess.run) -> bool:
    """Best effort: does `codex login status` say there is a login?

    It is a local file check inside the CLI — no network — and answers in milliseconds.
    Codex prints the answer on stderr, so both streams are read.
    """
    binary = codex_cli()
    if binary is None:
        return False
    try:
        result = run(
            [binary, "login", "status"], capture_output=True, text=True, timeout=LOGIN_TIMEOUT_S
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    said = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
    return result.returncode == 0 and "logged in" in said and "not logged in" not in said


CODEX_AUTH = AuthSource(
    api_key_setting="codex_api_key",
    api_key_env="CODEX_API_KEY",
    token_setting="codex_access_token",
    token_env="CODEX_ACCESS_TOKEN",
    stored_login=codex_stored_login,
    login_hint=(
        "run `codex login` once (`codex login --device-auth` on a machine with no browser),"
        " or set CODEX_API_KEY"
    ),
)


def private_codex_home(settings: Settings) -> Path:
    """Jarvis's own `CODEX_HOME`, for the API-key tier."""
    return settings.data_dir / "codex"


def ensure_login_home(
    settings: Settings,
    key: str,
    *,
    run: Callable[..., Any] = subprocess.run,
    owner_home: Path | None = None,
) -> Path:
    """A `CODEX_HOME` logged in with `key`, logging in only when the key is new.

    The key goes to `codex login --with-api-key` on stdin, never argv. A digest of it is
    stamped beside the login — atomically, and only once the login is in place and
    owner-only — so a rotated key logs in again and an unchanged one never does. Raises
    `RuntimeError`, with the key redacted, when Codex refuses it.
    """
    with _LOGIN_LOCK:
        home = secure_dir(private_codex_home(settings))
        owner_home = owner_home or codex_home()
        for name in SHARED_HOME_ENTRIES:
            link, target = home / name, owner_home / name
            if target.exists() and not link.exists() and not link.is_symlink():
                link.symlink_to(target)

        stamp, auth = home / _LOGIN_STAMP, home / "auth.json"
        digest = hashlib.sha256(key.encode()).hexdigest()
        if auth.exists() and stamp.exists() and stamp.read_text() == digest:
            return home

        binary = codex_cli()
        if binary is None:
            raise RuntimeError(f"the bundled codex CLI is missing ({CLI_PACKAGE}); run uv sync")
        blank = dict.fromkeys(CREDENTIAL_VARIABLES, "")
        result = run(
            [binary, "login", "--with-api-key"],
            input=key,
            capture_output=True,
            text=True,
            timeout=LOGIN_TIMEOUT_S,
            env={**os.environ, **blank, "CODEX_HOME": str(home)},
        )
        if result.returncode != 0 or not auth.exists():
            lines = redact(f"{result.stdout or ''}\n{result.stderr or ''}", [key]).split("\n")
            why = next((line.strip() for line in reversed(lines) if line.strip()), "no reason")
            raise RuntimeError(f"codex refused CODEX_API_KEY: {why}")
        secure_file(auth)
        pending = stamp.with_name(f"{_LOGIN_STAMP}.tmp")
        pending.write_text(digest)
        secure_file(pending)
        os.replace(pending, stamp)
        return home


# --------------------------------------------------------------------------- MCP servers


def mcp_servers(settings: Settings) -> dict[str, Mapping[str, Any]]:
    """The MCP servers a Codex subagent gets: Google when asked for, Slack when named.

    Claude reaches both through its own configuration (claude.ai connectors, the user-scope
    servers in `~/.claude.json`); Codex has neither, so they are handed over explicitly.
    Slack's server is the one the `send_to_slack` plugin names, and none while it is off.
    """
    from jarvis.plugins.slack import slack_route

    servers: dict[str, Mapping[str, Any]] = {}
    if settings.google_workspace_mcp:
        servers["google"] = google_mcp_server_config(settings)
    route = slack_route(settings)
    if route is not None and route.mcp_server:
        slack = mcp_server_config(route.mcp_server)
        if slack is not None:
            servers[route.mcp_server] = slack
        else:
            log.warning(
                "the Slack MCP server %s is not in %s", route.mcp_server, claude_user_config()
            )
    return servers


def mcp_config(
    servers: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, str]]:
    """The thread's `mcp_servers` config, and the environment values it forwards.

    Only a server's variable *names* go in the config (`env_vars`); their values go into
    the app-server's environment, which Codex hands on to the server. A server name that is
    not a bare word, or one with no command or url, is skipped with a warning.
    """
    config: dict[str, Any] = {}
    env: dict[str, str] = {}
    for name, server in servers.items():
        if not _BARE_KEY_RE.fullmatch(name):
            log.warning("skipping MCP server %r: not a name Codex config can spell", name)
            continue
        entry: dict[str, Any] = {}
        if server.get("command"):
            entry["command"] = str(server["command"])
            if server.get("args"):
                entry["args"] = [str(arg) for arg in server["args"]]
        elif server.get("url"):
            entry["url"] = str(server["url"])
        else:
            log.warning("skipping MCP server %r: it has neither a command nor a url", name)
            continue
        server_env = {str(k): str(v) for k, v in (server.get("env") or {}).items()}
        if server_env:
            entry["env_vars"] = sorted(server_env)
            env.update(server_env)
        config[name] = entry
    return ({"mcp_servers": config} if config else None), env


# ---------------------------------------------------------------------------- the client


class CodexTurn(Protocol):
    """One running turn: `AsyncTurnHandle`, as far as this module uses it."""

    def stream(self) -> AsyncIterator[Any]: ...
    async def steer(self, text: str) -> Any: ...
    async def interrupt(self) -> Any: ...


class CodexThread(Protocol):
    """One conversation: `AsyncThread`, as far as this module uses it."""

    id: str

    async def turn(self, prompt: str) -> CodexTurn: ...


class CodexClient(Protocol):
    """One app-server process (injectable for tests)."""

    async def thread_start(self, **options: Any) -> CodexThread: ...
    async def thread_resume(self, thread_id: str, **options: Any) -> CodexThread: ...

    def notices(self) -> AsyncIterator[Any]:
        """The notifications that belong to no turn, until the process is gone."""
        ...

    async def close(self) -> None: ...


CodexFactory = Callable[[dict[str, str], Path], CodexClient]


class _SdkCodex(CodexClient):
    """`AsyncCodex`, with every thread on full access and never asking for approval."""

    def __init__(self, env: dict[str, str], cwd: Path) -> None:
        from openai_codex import AsyncCodex, CodexConfig

        config = CodexConfig(env=env, cwd=str(cwd), client_name="jarvis", client_title="Jarvis")
        self._codex = AsyncCodex(config)

    @staticmethod
    def _unattended(options: dict[str, Any]) -> dict[str, Any]:
        from openai_codex import ApprovalMode, Sandbox

        return {"sandbox": Sandbox.full_access, "approval_mode": ApprovalMode.deny_all, **options}

    async def thread_start(self, **options: Any) -> CodexThread:
        return await self._codex.thread_start(**self._unattended(options))

    async def thread_resume(self, thread_id: str, **options: Any) -> CodexThread:
        # The reply need not carry the thread's history — the model keeps its context either
        # way — and asking for it is deprecated for paginated threads (seen live).
        options = {"include_turns": False, **options}
        return await self._codex.thread_resume(thread_id, **self._unattended(options))

    async def notices(self) -> AsyncIterator[Any]:
        # `AsyncCodex` has no public reader for these; its client's is the one the SDK's
        # own examples use. It ends when the reader thread fails every waiter on close.
        while True:
            try:
                yield await self._codex._client.next_notification()
            except Exception:
                return

    async def close(self) -> None:
        await self._codex.close()


def open_codex(env: dict[str, str], cwd: Path) -> CodexClient:
    """The real client: an app-server started lazily, with `env` over Jarvis's own."""
    return _SdkCodex(env, cwd)


# --------------------------------------------------------------------------- the adapter


def _error_text(error: Any) -> str:
    """A turn error as one line: the provider's own message out of a JSON body it is quoted
    as, and without the URL, ray and request id a 4xx trails."""
    text = (error.message or "").strip()
    try:
        body = json.loads(text)
    except ValueError:
        body = None
    inner = body.get("error") if isinstance(body, dict) else None
    if isinstance(inner, dict) and inner.get("message"):
        text = str(inner["message"])
    if error.additional_details:
        text = f"{text}: {error.additional_details.strip()}"
    return text.split(", url:")[0]


def _tool_call(item: Any) -> ToolCall | None:
    """The tool line for an item that has just started, or None for any other item."""
    if item.type == "commandExecution":
        return ToolCall("shell", item.command)
    if item.type == "mcpToolCall":
        return ToolCall(f"mcp__{item.server}__{item.tool}", item.arguments)
    if item.type == "webSearch":
        return ToolCall("web_search", item.query)
    if item.type == "dynamicToolCall":
        name = f"{item.namespace}.{item.tool}" if item.namespace else item.tool
        return ToolCall(name, item.arguments)
    if item.type == "collabAgentToolCall":
        return ToolCall(f"agent.{item.tool.value}", item.prompt)
    return None


def _notice_text(notification: Any) -> str | None:
    """What a global notification says worth a warning, or None for the rest."""
    payload = notification.payload
    if notification.method in _WARNING_METHODS:
        text = getattr(payload, "message", None) or getattr(payload, "summary", "")
        details = getattr(payload, "details", None)
        return f"{text} ({details})" if details else text
    if notification.method == "mcpServer/startupStatus/updated" and payload.error:
        return f"MCP server {payload.name} did not start: {payload.error}"
    return None


class CodexAdapter:
    """One Codex thread on one app-server, as the events `AdapterSession` runs on."""

    def __init__(
        self, client: CodexClient, thread: CodexThread, *, secrets: Iterable[str | None] = ()
    ) -> None:
        self._client = client
        self._thread = thread
        self.secrets = list(secrets)
        self._turn: CodexTurn | None = None
        self._notices = asyncio.create_task(self._log_notices())

    async def turn(self, prompt: str) -> AsyncIterator[AgentEvent]:
        from openai_codex.generated.v2_all import MessagePhase, TurnStatus

        yield SessionId(self._thread.id)
        self._turn = await self._thread.turn(prompt)
        usage: TokenUsage | None = None
        final: str | None = None
        try:
            async with contextlib.aclosing(self._turn.stream()) as stream:
                async for notification in stream:
                    method, payload = notification.method, notification.payload
                    if method == "item/started":
                        if call := _tool_call(payload.item.root):
                            yield call
                    elif method == "item/completed":
                        item = payload.item.root
                        if item.type == "agentMessage":
                            if item.phase is MessagePhase.final_answer:
                                final = item.text
                            yield Text(item.text)
                        elif item.type == "fileChange":
                            yield FileEdit(tuple((c.kind.root.type, c.path) for c in item.changes))
                    elif method == "thread/tokenUsage/updated":
                        last = payload.token_usage.last
                        spent = TokenUsage(
                            last.input_tokens, last.output_tokens, last.cached_input_tokens
                        )
                        usage = spent if usage is None else usage + spent
                    elif method == "error":
                        level = logging.INFO if payload.will_retry else logging.WARNING
                        yield Notice(_error_text(payload.error), level=level)
                    elif method == "turn/completed":
                        # Cleared before `Done`, so a follow-up from here on re-runs.
                        self._turn = None
                        turn = payload.turn
                        yield Done(
                            ok=turn.status is TurnStatus.completed,
                            result_text=final,
                            error=_error_text(turn.error) if turn.error else None,
                            interrupted=turn.status is TurnStatus.interrupted,
                            usage=usage,
                        )
        finally:
            self._turn = None

    async def steer(self, text: str) -> None:
        """Into the running turn; `SteerUnavailable` when the server refused it."""
        from openai_codex import CodexRpcError, InvalidRequestError

        turn = self._turn
        if turn is None:
            raise SteerUnavailable("no codex turn is running")
        try:
            await turn.steer(text)
        except CodexRpcError as exc:
            # A request the server rejected was never accepted: "no active turn to steer",
            # or a turn that cannot be steered. Anything else may have landed.
            if isinstance(exc, InvalidRequestError) or "activeTurnNotSteerable" in str(exc.data):
                raise SteerUnavailable(exc.message) from None
            raise

    async def interrupt(self) -> None:
        if self._turn is not None:
            await self._turn.interrupt()

    async def close(self) -> None:
        """Stop the app-server, which kills the commands it started (see docs/agents.md)."""
        try:
            await self._client.close()
        finally:
            self._notices.cancel()
            await asyncio.gather(self._notices, return_exceptions=True)

    async def _log_notices(self) -> None:
        """Log the warnings that belong to no turn — config, deprecation, an MCP server
        that would not start — until the app-server is gone."""
        async for notification in self._client.notices():
            try:
                text = _notice_text(notification)
            except Exception:  # a shape this build does not know is not worth a crash
                log.debug("codex: unreadable %s notification", notification.method)
                continue
            if text:
                log.warning("codex: %s", redact(text, self.secrets))


# ---------------------------------------------------------------------------- the runner


class CodexAgentRunner(AdapterRunner):
    """Opens Codex conversations (`client_factory` and `login` are injected in tests)."""

    name = "codex"

    def __init__(
        self,
        settings: Settings,
        *,
        client_factory: CodexFactory = open_codex,
        login: Callable[..., Any] = subprocess.run,
    ) -> None:
        super().__init__(settings)
        self._client_factory = client_factory
        self._login = login

    def context(self, task: Task) -> AgentContext:
        return AgentContext.build(
            task,
            self.settings,
            auth=CODEX_AUTH,
            model=task.model or self.settings.codex_model,
            mcp_servers=mcp_servers(self.settings),
        )

    async def connect(self, context: AgentContext, resume: str | None) -> CodexAdapter:
        """Start an app-server and a thread on it — or resume one — for `context`."""
        config, server_env = mcp_config(context.mcp_servers)
        env = {**server_env, **await self._credential_env(context.auth)}
        client = self._client_factory(env, context.cwd)
        options = {
            "cwd": str(context.cwd),
            "developer_instructions": context.instructions,
            "model": context.model,
            "config": config,
        }
        try:
            if resume:
                thread = await client.thread_resume(resume, **options)
            else:
                thread = await client.thread_start(**options)
        except BaseException:
            with contextlib.suppress(Exception):
                await client.close()
            raise
        return CodexAdapter(client, thread, secrets=context.secrets)

    async def _credential_env(self, auth: AuthStatus) -> dict[str, str]:
        """The credential variables for the app-server: the chosen one, the rest empty."""
        env = dict.fromkeys(CREDENTIAL_VARIABLES, "")
        if auth.mode is AuthMode.API_KEY:
            home = await asyncio.to_thread(
                ensure_login_home, self.settings, auth.secret or "", run=self._login
            )
            env["CODEX_HOME"] = str(home)
        elif auth.mode is AuthMode.TOKEN:
            env["CODEX_ACCESS_TOKEN"] = auth.secret or ""
        return env
