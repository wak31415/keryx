"""One model call: what a voice tool read, and a question, in; a few spoken sentences out.

`check_email` and `find_document` both read something far too long to say (a day of mail, a
document) and have a few seconds to answer a question about it. They share this: the
bundled `claude` CLI in print mode, stripped to the model alone. No tools, no settings, no
MCP servers and no session written, so the text it is handed can change what it says and
nothing else: there is nothing for it to act with.

It signs in the way every Claude subagent does (`CLAUDE_AUTH`: an API key, a token, or the
stored subscription login), handed over in the environment only. A failure is a
`SummaryError` whose detail is for the log and has the credential redacted; the caller
turns it into the sentence its own tool says.
"""

import asyncio
import json
import os
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from keryx.agents.auth import child_env, redact, resolve_auth
from keryx.config import Settings


class SummaryError(Exception):
    """The model call failed. `detail` is for the log: never a credential, never the text."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class Summariser(Protocol):
    """One model call: instructions and material in, a few spoken sentences out."""

    async def summarise(self, system: str, prompt: str) -> str: ...


Runner = Callable[[Sequence[str], str, dict[str, str]], Any]


async def _run_cli(argv: Sequence[str], stdin: str, env: dict[str, str]) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **env},
    )
    try:
        out, err = await process.communicate(stdin.encode())
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise
    return process.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


class ClaudeCliSummariser(Summariser):
    """The bundled `claude` CLI in print mode, stripped to one model call.

    The material goes in on standard input and the instructions as the system prompt, so
    nothing a tool read is ever on a command line.
    """

    def __init__(
        self,
        cli: str,
        settings: Settings,
        *,
        model: str,
        effort: str,
        run: Callable[..., Any] = _run_cli,
    ) -> None:
        self._cli = cli
        self._settings = settings
        self._model = model
        self._effort = effort
        self._run = run

    async def summarise(self, system: str, prompt: str) -> str:
        from keryx.agents.claude import CLAUDE_AUTH

        auth = resolve_auth(CLAUDE_AUTH, self._settings, probe=False)
        argv = [
            self._cli, "-p",
            "--model", self._model,
            "--effort", self._effort,
            "--output-format", "json",
            "--setting-sources", "",
            "--strict-mcp-config",
            "--tools", "",
            "--no-session-persistence",
            "--system-prompt", system,
        ]  # fmt: skip
        code, out, err = await self._run(argv, prompt, child_env(auth))
        try:
            result = json.loads(out) if out.strip() else {}
        except ValueError:
            result = {}
        text = str(result.get("result") or "").strip()
        if code != 0 or result.get("is_error") or not text:
            why = redact((err or str(result.get("result") or ""))[-300:], [auth.secret])
            raise SummaryError(f"claude exited {code}: {why}")
        return text


def summariser_problem(settings: Settings) -> str | None:
    """Why no summariser can be built on this machine, in a sentence; None when one can."""
    from keryx.agents.registry import BACKENDS, install_command, installed

    if not installed("claude") or BACKENDS["claude"].find_cli() is None:
        return f"the claude CLI is not installed: `{install_command('claude')}`"
    return None


def build_summariser(settings: Settings, *, model: str, effort: str) -> ClaudeCliSummariser:
    """The production summariser, answering with `model` at `effort` (a plugin's settings).

    Ask `summariser_problem` first.
    """
    from keryx.agents.registry import BACKENDS

    cli = BACKENDS["claude"].find_cli()
    assert cli is not None, "summariser_problem says so first"
    return ClaudeCliSummariser(cli, settings, model=model, effort=effort)
