"""The Codex backend: subagents on OpenAI's Codex CLI, driven as `codex exec --json`.

There is no Python SDK for Codex, so a turn is one process: the prompt goes in on stdin,
and stdout is a stream of JSON events (recorded shapes are in
`tests/agents/fixtures/codex_*.jsonl`):

- `thread.started` carries the session id, which is what `codex exec resume <id>` takes;
- `item.started` / `item.completed` are the work — `agent_message` (the model talking),
  `command_execution`, `file_change`, `mcp_tool_call`, `web_search` — and become progress
  lines; the last `agent_message` is the final text;
- `turn.completed` carries token usage, and `turn.failed` / `error` say what went wrong.

The process is spawned through an injectable `CodexSpawner`, so tests never start `codex`
(CLAUDE.md testing rule).

What Claude gets from its SDK options, Codex gets from flags:
- the rendered `subagent_suffix.md` as `-c developer_instructions=…` (verified to reach
  the model; it is re-sent on a resume, since config is per invocation);
- `bypassPermissions` as `--dangerously-bypass-approvals-and-sandbox`, because the phone
  PIN gates the dispatch, not the agent;
- MCP servers (`workspace-mcp` for Gmail and Calendar, the Slack server) as
  `-c mcp_servers.<name>.*`, with their secrets forwarded by *name* through `env_vars`
  and the values in the child's environment only — never in argv.

Codex has no `max_budget_usd` and no `max_turns`; the wall-clock cap every backend shares
(`SUBAGENT_TIMEOUT_S`, applied by the task manager) is what bounds it. It reports tokens,
not dollars, so `RunResult.cost_usd` stays None: on the ChatGPT plan a call has no price.

The headless token tier (`CODEX_ACCESS_TOKEN`) is the odd one out. `codex exec` does not
read that variable — checked against Codex 0.156 — so the token is instead handed to
`codex login --with-access-token` once, on stdin, into a `CODEX_HOME` of Jarvis's own
(`data_dir/codex`), which every run then points at. The owner's own login in `~/.codex` is
never touched; their config, instructions and skills are linked in so the agent is the
same one they use.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from jarvis.agents.auth import AuthMode, AuthSource, AuthStatus, child_env, redact, resolve_auth
from jarvis.agents.base import (
    AgentRunner,
    AgentSession,
    RunResult,
    emit_progress,
    extract_restart_request,
    extract_spoken_summary,
    failure_summary,
    google_mcp_server_config,
    render_subagent_suffix,
    workspace_dir,
)
from jarvis.config import Settings, secure_dir, secure_file
from jarvis.integrations.slack import mcp_server_config
from jarvis.tasks.models import Task

log = logging.getLogger("jarvis.agents.codex")

CODEX_BINARY = "codex"

#: The model names that can be said out loud, and the Codex ids they mean. Blank is
#: `CODEX_MODEL`, and a blank `CODEX_MODEL` is Codex's own default, whatever that is today.
CODEX_MODELS = {
    "sol": "gpt-5.6-sol",
    "terra": "gpt-5.6-terra",
    "luna": "gpt-5.6-luna",
}

#: One stdout line can be a whole command's output (`aggregated_output`); the asyncio
#: default of 64 KiB per line is the same trap the Claude SDK's 1 MiB buffer was.
CODEX_LINE_LIMIT_BYTES = 64 * 1024 * 1024
#: How much of the process's stderr is kept, for an exit that no event explains.
STDERR_TAIL_LINES = 40
#: How long `close()` waits after SIGTERM before it kills the process group.
TERMINATE_GRACE_S = 5.0
#: How long `codex login status` / `codex login --with-access-token` may take.
LOGIN_TIMEOUT_S = 30.0

_MAX_TOOL_INPUT_CHARS = 200
#: An MCP server name has to be a bare TOML key to be spelt in a `-c` path.
_BARE_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")
#: The error of a turn that `interrupt()` ended: SIGINT makes `codex exec` exit 1 at once,
#: with no `turn.failed` to say why.
INTERRUPTED_ERROR = "interrupted"
#: The files in the owner's `~/.codex` that make Codex *their* Codex: linked into
#: Jarvis's own home for the token tier, so only the login differs.
SHARED_HOME_ENTRIES = ("config.toml", "AGENTS.md", "skills")
_TOKEN_STAMP = ".jarvis-token-sha256"


# ------------------------------------------------------------------------------ auth


def codex_home() -> Path:
    """The owner's Codex home: `CODEX_HOME`, else `~/.codex`."""
    return Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser()


def codex_cli() -> str | None:
    """The `codex` binary on PATH, or None when it is not installed."""
    return shutil.which(CODEX_BINARY)


def codex_stored_login(run: Callable[..., Any] = subprocess.run) -> bool:
    """Best effort: does `codex login status` say there is a login?

    It is a local file check inside the CLI — no network — and answers in milliseconds.
    Codex 0.156 prints the answer on stderr, so both streams are read.
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
    """Jarvis's own `CODEX_HOME`, for the token tier."""
    return settings.data_dir / "codex"


def ensure_token_home(
    settings: Settings,
    token: str,
    *,
    run: Callable[..., Any] = subprocess.run,
    owner_home: Path | None = None,
) -> Path:
    """A `CODEX_HOME` logged in with `token`, created and logged in only when it is new.

    The token goes to `codex login --with-access-token` on stdin. A digest of it is kept
    beside the login, so a rotated token logs in again and an unchanged one never does.
    Raises `RuntimeError` (with the token redacted) when Codex refuses it.
    """
    home = secure_dir(private_codex_home(settings))
    owner_home = owner_home or codex_home()
    for name in SHARED_HOME_ENTRIES:
        link, target = home / name, owner_home / name
        if target.exists() and not link.exists() and not link.is_symlink():
            link.symlink_to(target)

    stamp = home / _TOKEN_STAMP
    digest = hashlib.sha256(token.encode()).hexdigest()
    if (home / "auth.json").exists() and stamp.exists() and stamp.read_text() == digest:
        return home

    binary = codex_cli() or CODEX_BINARY
    result = run(
        [binary, "login", "--with-access-token"],
        input=token,
        capture_output=True,
        text=True,
        timeout=LOGIN_TIMEOUT_S,
        env={**os.environ, "CODEX_HOME": str(home)},
    )
    if result.returncode != 0:
        lines = redact(f"{result.stdout or ''}\n{result.stderr or ''}", [token]).split("\n")
        why = next((line.strip() for line in reversed(lines) if line.strip()), "no reason given")
        raise RuntimeError(f"codex refused CODEX_ACCESS_TOKEN: {why}")
    stamp.write_text(digest)
    secure_file(stamp)
    secure_file(home / "auth.json")
    return home


# ------------------------------------------------------------------------- the command


def mcp_overrides(servers: Mapping[str, Mapping[str, Any]]) -> tuple[list[str], dict[str, str]]:
    """MCP server configs as `-c mcp_servers.<name>.*` flags, plus the env they forward.

    Only a server's variable *names* reach argv (`env_vars`); their values go into the
    child's environment, which Codex hands on to the server. A server name that is not a
    bare word, or one with no command or url, is skipped with a warning.
    """
    argv: list[str] = []
    env: dict[str, str] = {}
    for name, config in servers.items():
        if not _BARE_KEY_RE.fullmatch(name):
            log.warning("skipping MCP server %r: not a name Codex config can spell", name)
            continue
        prefix = f"mcp_servers.{name}"
        if config.get("command"):
            argv += ["-c", f"{prefix}.command={json.dumps(config['command'])}"]
            if config.get("args"):
                argv += ["-c", f"{prefix}.args={json.dumps([str(a) for a in config['args']])}"]
        elif config.get("url"):
            argv += ["-c", f"{prefix}.url={json.dumps(config['url'])}"]
        else:
            log.warning("skipping MCP server %r: it has neither a command nor a url", name)
            continue
        server_env = {str(k): str(v) for k, v in (config.get("env") or {}).items()}
        if server_env:
            argv += ["-c", f"{prefix}.env_vars={json.dumps(sorted(server_env))}"]
            env.update(server_env)
    return argv, env


def mcp_servers(settings: Settings) -> dict[str, Mapping[str, Any]]:
    """The MCP servers a Codex subagent gets: Google when asked for, Slack when named.

    Claude reaches both through its own configuration (claude.ai connectors, the user-scope
    servers in `~/.claude.json`); Codex has neither, so they are handed over explicitly.
    """
    servers: dict[str, Mapping[str, Any]] = {}
    if settings.google_workspace_mcp:
        servers["google"] = google_mcp_server_config(settings)
    if settings.slack_mcp_server:
        slack = mcp_server_config(settings.slack_mcp_server)
        if slack is not None:
            servers[settings.slack_mcp_server] = slack
        else:
            log.warning("SLACK_MCP_SERVER %s is not in ~/.claude.json", settings.slack_mcp_server)
    return servers


def build_command(
    task: Task,
    settings: Settings,
    *,
    resume: str | None = None,
    binary: str = CODEX_BINARY,
) -> tuple[list[str], dict[str, str], Path]:
    """`(argv, extra env, cwd)` for one turn of `task`; the prompt goes on stdin (`-`).

    The env holds only what the MCP servers forward — the credential is the runner's to
    add. `exec resume` has no `-C`, so the working directory is also the process's own.
    """
    cwd = workspace_dir(task, settings)
    argv = [binary, "exec"]
    if resume:
        argv += ["resume", resume]
    argv += ["--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox"]
    if not resume:
        argv += ["-C", str(cwd)]
    model = task.model or settings.codex_model
    if model:
        argv += ["-m", model]
    suffix = render_subagent_suffix(
        task, slack_mcp_server=settings.slack_mcp_server, owner=settings.owner_label
    )
    argv += ["-c", f"developer_instructions={json.dumps(suffix)}"]
    server_argv, env = mcp_overrides(mcp_servers(settings))
    argv += server_argv
    argv.append("-")
    return argv, env, cwd


# ------------------------------------------------------------------------- the process


class CodexProcess(Protocol):
    """One running `codex exec` (injectable for tests)."""

    def lines(self) -> AsyncIterator[str]:
        """Stdout, one line at a time, until the process closes it."""
        ...

    async def wait(self) -> int: ...
    def stderr_tail(self) -> str: ...
    def interrupt(self) -> None: ...
    async def terminate(self) -> None: ...


CodexSpawner = Callable[[list[str], Path, dict[str, str], str], Awaitable[CodexProcess]]


class _SubprocessCodex(CodexProcess):
    """`codex exec` as an asyncio subprocess in a process group of its own.

    Its own group, so an interrupt or a close reaches the shell commands it started too,
    not just the CLI. Stderr is drained continuously into a bounded tail, because a pipe
    nobody reads fills up and stalls the process.
    """

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        self._stderr: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        self._drain = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        stream = self._process.stderr
        while stream is not None and (line := await stream.readline()):
            self._stderr.append(line.decode(errors="replace").rstrip())

    async def lines(self) -> AsyncIterator[str]:
        stream = self._process.stdout
        while stream is not None and (line := await stream.readline()):
            yield line.decode(errors="replace")

    async def wait(self) -> int:
        code = await self._process.wait()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._drain, 1.0)
        return code

    def stderr_tail(self) -> str:
        return "\n".join(self._stderr)

    def _signal(self, sig: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self._process.pid, sig)

    def interrupt(self) -> None:
        self._signal(signal.SIGINT)

    async def terminate(self) -> None:
        if self._process.returncode is not None:
            return
        self._signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(self._process.wait(), TERMINATE_GRACE_S)
        except TimeoutError:
            self._signal(signal.SIGKILL)
            await self._process.wait()


async def spawn_codex(argv: list[str], cwd: Path, env: dict[str, str], prompt: str) -> CodexProcess:
    """Start `argv` in `cwd` with `env` added to ours, and write `prompt` to its stdin."""
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env={**os.environ, **env},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=CODEX_LINE_LIMIT_BYTES,
        start_new_session=True,
    )
    assert process.stdin is not None
    process.stdin.write(prompt.encode())
    await process.stdin.drain()
    process.stdin.close()
    return _SubprocessCodex(process)


# ---------------------------------------------------------------------------- progress


def _arguments(value: Any) -> str:
    try:
        text = json.dumps(value, default=str)
    except (TypeError, ValueError):  # pragma: no cover - json.dumps(default=str) rarely fails
        text = str(value)
    return text[:_MAX_TOOL_INPUT_CHARS]


def progress_line(item: Mapping[str, Any], *, started: bool) -> str | None:
    """A short, loggable one-liner for an item event; None for one that is not worth one.

    Tool calls are reported when they start (that is when they are news), file changes and
    messages when they complete.
    """
    kind = item.get("type")
    if kind == "command_execution" and started:
        return f"[tool] shell {_arguments(item.get('command', ''))}"
    if kind == "mcp_tool_call" and started:
        tool = f"mcp__{item.get('server', '?')}__{item.get('tool', '?')}"
        return f"[tool] {tool} {_arguments(item.get('arguments') or {})}"
    if kind == "web_search" and started:
        return f"[tool] web_search {_arguments(item.get('query', ''))}"
    if kind == "file_change" and not started:
        changes = item.get("changes") or []
        paths = ", ".join(f"{c.get('kind', '?')} {c.get('path', '?')}" for c in changes)
        return f"[edit] {paths[:_MAX_TOOL_INPUT_CHARS]}"
    return None


# ------------------------------------------------------------------------- the session


class CodexAgentSession(AgentSession):
    """One task's Codex conversation: each `run()` is one `codex exec` process.

    The first run starts the thread; every later one resumes it by the id the first one
    printed, so one session is one conversation however many processes it takes.
    """

    def __init__(
        self,
        task: Task,
        settings: Settings,
        *,
        resume: str | None,
        auth: AuthStatus,
        env: dict[str, str],
        spawner: CodexSpawner,
        binary: str,
    ) -> None:
        self._task = task
        self._settings = settings
        self._session_id = resume
        self._auth = auth
        self._env = env
        self._spawner = spawner
        self._binary = binary
        self._process: CodexProcess | None = None
        self._interrupted = False

    def _redact(self, text: str) -> str:
        return redact(text, [self._auth.secret])

    async def run(self, prompt: str, *, on_progress: Callable[[str], Any]) -> RunResult:
        """Run one `codex exec` to its end; never raises."""
        self._interrupted = False
        messages: list[str] = []
        failure: str | None = None
        last_error: str | None = None
        try:
            argv, server_env, cwd = build_command(
                self._task, self._settings, resume=self._session_id, binary=self._binary
            )
            self._process = await self._spawner(argv, cwd, {**server_env, **self._env}, prompt)
            async for line in self._process.lines():
                event = _parse(line)
                if event is None:
                    continue
                kind = event.get("type")
                if kind == "thread.started":
                    self._session_id = event.get("thread_id") or self._session_id
                elif kind in ("item.started", "item.completed"):
                    item = event.get("item") or {}
                    started = kind == "item.started"
                    if item.get("type") == "agent_message" and not started:
                        text = (item.get("text") or "").strip()
                        if text:
                            messages.append(text)
                            await emit_progress(on_progress, text)
                    elif item.get("type") == "error":
                        log.warning("codex: %s", self._redact(str(item.get("message", ""))))
                    elif line_text := progress_line(item, started=started):
                        await emit_progress(on_progress, line_text)
                elif kind == "turn.completed":
                    usage = event.get("usage") or {}
                    log.info(
                        "codex turn done for task %s: %s input / %s output tokens",
                        self._task.id,
                        usage.get("input_tokens"),
                        usage.get("output_tokens"),
                    )
                elif kind == "turn.failed":
                    failure = _error_text(event.get("error")) or "the turn failed"
                elif kind == "error":
                    last_error = _error_text(event)
            code = await self._process.wait()
        except Exception as exc:
            log.exception("codex turn failed")
            error = self._redact(f"{type(exc).__name__}: {exc}")
            return RunResult(
                ok=False,
                final_text=messages[-1] if messages else "",
                spoken_summary=failure_summary(error),
                session_id=self._session_id,
                error=error,
            )

        final_text = messages[-1] if messages else ""
        if failure is None and code != 0:
            if self._interrupted:
                failure = INTERRUPTED_ERROR
            else:
                tail = self._process.stderr_tail().strip().splitlines()
                failure = last_error or (tail[-1] if tail else f"codex exited with {code}")
        if failure is not None:
            # The spoken line is the failure, not the last message: that is usually the
            # model announcing what it was about to do, which reads as a result it is not.
            error = self._redact(failure)
            return RunResult(
                ok=False,
                final_text=final_text,
                spoken_summary=failure_summary(error),
                session_id=self._session_id,
                error=error,
            )
        return RunResult(
            ok=True,
            final_text=final_text,
            spoken_summary=extract_spoken_summary(final_text),
            session_id=self._session_id,
            restart_reason=extract_restart_request(final_text),
        )

    async def send(self, text: str) -> None:
        """Not supported mid-turn: the manager turns follow-ups into a resumed run."""
        raise NotImplementedError("codex takes follow-ups as a resumed run")

    async def interrupt(self) -> None:
        """SIGINT to the process group, which ends the turn within a moment."""
        if self._process is not None:
            self._interrupted = True
            self._process.interrupt()

    async def close(self) -> None:
        """Terminate the process group if it is still there; safe to call more than once."""
        if self._process is not None:
            try:
                await self._process.terminate()
            except Exception:
                log.exception("terminating the codex process failed")


def _parse(line: str) -> dict[str, Any] | None:
    """One JSONL event; anything else on stdout (there should be nothing) is logged."""
    line = line.strip()
    if not line:
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        log.debug("codex printed a non-JSON line: %.200s", line)
        return None
    return event if isinstance(event, dict) else None


def _error_text(error: Any) -> str | None:
    """The message out of an error event, unwrapping the JSON body a 4xx is quoted as."""
    if not isinstance(error, dict):
        return None
    message = str(error.get("message") or "").strip()
    try:
        body = json.loads(message)
    except (json.JSONDecodeError, TypeError):
        return message or None
    inner = body.get("error") if isinstance(body, dict) else None
    if isinstance(inner, dict) and inner.get("message"):
        return str(inner["message"])
    return message or None


# -------------------------------------------------------------------------- the runner


class CodexAgentRunner(AgentRunner):
    """Opens Codex conversations (`spawner` and `login` are injected in tests)."""

    def __init__(
        self,
        settings: Settings,
        *,
        spawner: CodexSpawner = spawn_codex,
        login: Callable[..., Any] = subprocess.run,
    ) -> None:
        self._settings = settings
        self._spawner = spawner
        self._login = login

    async def open(self, task: Task, *, resume: str | None = None) -> AgentSession:
        """A session for `task`; nothing is spawned until its first `run()`."""
        auth = resolve_auth(CODEX_AUTH, self._settings, probe=False)
        if auth.mode is AuthMode.TOKEN:
            home = await asyncio.to_thread(
                ensure_token_home, self._settings, auth.secret or "", run=self._login
            )
            env = {"CODEX_HOME": str(home)}
        else:
            env = child_env(auth)
        binary = codex_cli() or CODEX_BINARY
        log.info(
            "codex subagent opened for task %s (model=%s, auth=%s, resume=%s)",
            task.id,
            task.model or self._settings.codex_model or "codex default",
            auth.mode,
            resume,
        )
        return CodexAgentSession(
            task,
            self._settings,
            resume=resume,
            auth=auth,
            env=env,
            spawner=self._spawner,
            binary=binary,
        )
