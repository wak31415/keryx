"""The Mac app's JSON contract: every key its decoders rely on is in what the CLI prints,
with the JSON type the decoder needs.

`macos/Tests/KeryxKitTests/Fixtures/contract.json` (schema 2) maps a command line to the
keys the Swift side decodes, by path, each with its type: `""` is the document,
`"servers.llm"` a nested object, `"tasks[]"` each element of a list (and `"[]"` each
element of a document that is one). A type is `string`, `integer`, `number`, `boolean`,
`array`, `object` or `any` (a key the decoder takes in whatever JSON type it comes), and
`|null` after it is a key the decoder also takes as null; without it, null is a broken
promise. Its `streams` block is the same promise for the JSON lines of `models pull --json`
and `voice use here --json`: each event's keys, exactly one ending, and the exit code each
ending carries. Renaming a field here without the app fails this test, not the owner's
menu bar.

That file is the only source: one that is missing, will not parse, or is not schema 2 fails
the test, naming the file and what is wrong.

Each command runs against a store seeded so that no list it walks is empty and as few
values as possible are their defaults — a running task and a told one, a setting saved, a
model on disk, a model server that answers — with the same fakes as the rest of the suite:
no service manager, no `git`, and nothing that leaves the process (the model server is
httpx's own transport, faked, answering on loopback only).
"""

import asyncio
import json
import sys
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import httpx
import pytest
from setup.test_auth_stream import (
    CONSENT,
    DEVICE_AUTH,
    SIGN_IN,
    TOKEN_SHOWN,
    TYPED,
    Clock,
    FakeChild,
)
from typer.testing import CliRunner

from keryx import plugins, status, units
from keryx.cli import app
from keryx.config import Settings
from keryx.config.store import ConfigStore
from keryx.continuity.memory import memory_path
from keryx.integrations.gmail import token_path
from keryx.issues import GhStatus
from keryx.localmodels import catalog, download, hardware, runtimes, servers, use, voice
from keryx.localmodels.hardware import Hardware
from keryx.restart.service import ServiceTarget
from keryx.restart.store import RECORD_NAME, RestartRecord, RestartStore
from keryx.setup import google
from keryx.tasks.models import Task, TaskKind, TaskStatus, task_log_path
from keryx.tasks.store import TaskStore

CONTRACT = Path(__file__).parents[1] / "macos/Tests/KeryxKitTests/Fixtures/contract.json"
SCHEMA = 2
#: Each contract type, as the Python value `json.loads` gives for it. A bool is an int to
#: Python and never to the decoder, so `type_problem` refuses one as a number. `any` is a
#: key the decoder takes in whatever JSON type it comes (a `JSONValue`): any value but null.
ANY = "any"
TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,),
    "array": (list,), "object": (dict,), ANY: (object,),
}
NULLABLE = "|null"
PULL = "models pull --json"
VOICE_HERE = "voice use here --json"
MODELS_START = "models start --json"
#: The four sign-ins the app drives, each its own stream.
AUTH_LOGINS = {name: f"auth login {name} --json"
               for name in ("claude", "codex", "gmail", "google-workspace")}
#: The command lines the app reads as JSON lines; the pull's is the one every contract has.
STREAMS = (PULL, VOICE_HERE, MODELS_START, *AUTH_LOGINS.values())
#: The command lines the app reads as one JSON document.
COMMANDS = {
    "status --json", "doctor --json", "tasks list --json", "tasks show ID --json",
    "tasks usage --json", "models list --json", "models use NAME --json",
    "config list --json", "config options KEY --json", "approvals --json", "memory --json",
    "voice --json", "voice use openai --json", "voice use server URL --json",
    "service --json", "service install --json", "service uninstall --json",
    "models stop --json",
    "setup plan --json", "setup mark STEP --json", "pin --json", "pin set --json",
    "config verify KEY --json",
    "integrations --json", "auth logout google-workspace --json", "auth logout gmail --json",
    "plugins install NAME --json", "plugins remove NAME --json", "plugins set NAME --json",
}
#: What a command line reads on stdin (with `--stdin` added to it): the new PIN, twice;
#: the key to check, unsaved.
STDIN = {"pin set --json": "482915\n482915\n", "config verify KEY --json": "sk-typed\n"}
#: A placeholder that stands for something else in one command line: the key to check, the
#: plugin (the seed's, installed), and for `plugins set` its setting too.
WORDS_FOR = {
    "config verify KEY --json": {"KEY": "OPENAI_API_KEY"},
    "plugins install NAME --json": {"NAME": "check_billing"},
    "plugins remove NAME --json": {"NAME": "check_billing"},
    "plugins set NAME --json": {"NAME": "check_billing monthly_budget=40"},
}
#: What the placeholders in a contract command line stand for.
TASK_ID = 1
MODEL = catalog.by_key("qwen3-coder-30b-a3b")
#: What the faked model server says it has loaded.
SERVED = "qwen3-coder-q4"
#: A voice server elsewhere, for `voice use server URL`: its handshake is faked.
VOICE_SERVER = "http://192.168.1.20:8765"
#: The speech-to-speech the seeded machine has.
S2S = "/Users/you/.local/bin/speech-to-speech"

runner = CliRunner()


def load_contract(path: Path) -> tuple[dict | None, str | None]:
    """The schema 2 contract at `path` and None, or None and why it is not one."""
    try:
        real = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return None, f"{path} cannot be read as JSON: {error}"
    if not isinstance(real, dict):
        return None, f"{path} is a JSON {type(real).__name__}, not an object"
    if "schema" not in real:
        return None, f"{path} has no schema (a v1 contract); this test reads schema {SCHEMA}"
    if real["schema"] != SCHEMA:
        return None, f"{path} says schema {real['schema']!r}; this test reads schema {SCHEMA}"
    return real, None


def contract() -> dict:
    """The Swift side's contract, or a failure naming the file and what is wrong with it."""
    real, problem = load_contract(CONTRACT)
    if problem:
        pytest.fail(problem, pytrace=False)
    return real


def argv(command: str) -> list[str]:
    # The realtime model on the seeded voice server: a list read live, the current appended.
    words = {"ID": str(TASK_ID), "NAME": MODEL.key, "KEY": "OPENAI_REALTIME_MODEL",
             "URL": VOICE_SERVER, "STEP": "google", **WORDS_FOR.get(command, {})}
    stdin = ["--stdin"] if command in STDIN else []
    return [part for word in command.split() for part in words.get(word, word).split()] + stdin


# --- the rules ---------------------------------------------------------------------------


def type_problem(value: object, expected: str) -> str | None:
    """Why `value` does not meet the contract type `expected`, or None when it does."""
    base, _, nullable = expected.partition("|")
    if base not in TYPES or nullable not in ("", "null"):
        return f"has the unknown type {expected!r}"
    if value is None:
        return None if nullable == "null" else "is null"
    if base == ANY:
        return None
    wanted = TYPES[base]
    if isinstance(value, bool) and bool not in wanted:
        return f"is a boolean, not {base}"
    if not isinstance(value, wanted):
        return f"is {type(value).__name__}, not {base}"
    return None


def key_problems(node: object, keys: dict[str, str]) -> list[str]:
    """Every key in `keys` that `node` lacks or holds as the wrong type."""
    if not isinstance(node, dict):
        return [f"is {type(node).__name__}, not an object"]
    problems = []
    for key, expected in keys.items():
        if key not in node:
            problems.append(f"has no {key!r}")
        elif why := type_problem(node[key], expected):
            problems.append(f"{key!r} {why}")
    return problems


def nodes_at(document: object, path: str) -> list[object]:
    """Every object at `path`: dotted keys, where `name[]` (or `[]` alone, for the
    document) is each element of that list. A list on the way must not be empty, or what
    is behind it was never checked."""
    found = [document]
    for part in [p for p in path.split(".") if p]:
        name, each = (part[:-2], True) if part.endswith("[]") else (part, False)
        stepped = []
        for node in found:
            if name:
                assert isinstance(node, dict) and name in node, f"no {name!r} on {path!r}"
                node = node[name]
            if each:
                assert isinstance(node, list), f"{path!r}: {name or 'the document'} is no list"
                assert node, f"{path!r} is an empty list; seed it so the contract is checked"
                stepped.extend(node)
            else:
                stepped.append(node)
        found = stepped
    return found


def _type_problems(where: str, keys: object) -> list[str]:
    if not isinstance(keys, dict):
        return [f"{where} is not an object of keys"]
    return [
        f"{where}: {key!r} has no type {expected!r}"
        for key, expected in keys.items()
        if not isinstance(expected, str)
        or expected.removesuffix(NULLABLE) not in TYPES
    ]


def shape_problems(doc: object) -> list[str]:
    """What keeps `doc` from being a schema 2 contract."""
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        return ["not schema 2"]
    problems = []
    commands = doc.get("commands")
    if not isinstance(commands, dict) or not commands:
        problems.append("no commands")
    else:
        for command, paths in commands.items():
            if not isinstance(paths, dict) or not paths:
                problems.append(f"{command}: no paths")
                continue
            for path, keys in paths.items():
                problems += _type_problems(f"{command} {path!r}", keys)
    streams = doc.get("streams") or {}
    if not isinstance(streams.get(PULL), dict):
        return [*problems, f"no {PULL} stream"]
    for command, stream in streams.items():
        problems += _stream_problems(command, stream)
    return problems


def _stream_problems(command: str, stream: object) -> list[str]:
    """What keeps `stream` from being a stream's promise: its events' keys, its endings
    among them, and an exit code for each ending."""
    if not isinstance(stream, dict):
        return [f"{command}: not an object"]
    problems = []
    events = stream.get("events") or {}
    for name, keys in events.items():
        problems += _type_problems(f"{command} {name}", keys)
    terminal = stream.get("terminal") or []
    exits = stream.get("exit") or {}
    if not terminal or not set(terminal) <= set(events):
        problems.append(f"{command}: the endings are not all events")
    for ending in terminal:
        code = exits.get(ending)
        codes = code if isinstance(code, list) else [code]
        if not codes or not all(isinstance(c, int) and not isinstance(c, bool) for c in codes):
            problems.append(f"{command}: {ending} has no exit code")
    return problems


#: The smallest schema 2 contract, every rule in it: the document (`""`), a nested path, a
#: list's elements, a root list (`"[]"`), every type, `|null`, and the pull stream.
SMALL_V2 = {
    "schema": 2,
    "commands": {
        "status --json": {"": {"schema": "integer"}, "restart": {"failure": "string|null"}},
        "tasks list --json": {"tasks[]": {"id": "integer", "cost_usd": "number|null"}},
        "models list --json": {"servers.llm": {"reachable": "boolean"}},
        "config list --json": {"[]": {"choices": "array|null", "value": "any|null"}},
        "models use NAME --json": {"": {"saved": "object"}},
    },
    "streams": {PULL: {
        "events": {"progress": {"done": "integer", "total": "integer"}, "cancelled": {}},
        "terminal": ["cancelled"], "exit": {"cancelled": 130},
    }},
}


def test_the_small_contract_is_schema_2_and_has_every_rule():
    paths = [path for command in SMALL_V2["commands"].values() for path in command]
    types = [kind for command in SMALL_V2["commands"].values()
             for keys in command.values() for kind in keys.values()]

    assert shape_problems(SMALL_V2) == []
    assert "" in paths and "[]" in paths
    assert any("." in path for path in paths) and any(path.endswith("[]") for path in paths)
    assert {kind.removesuffix(NULLABLE) for kind in types} == set(TYPES)
    assert any(kind.endswith(NULLABLE) for kind in types)


def test_the_contract_read_is_schema_2():
    assert shape_problems(contract()) == []


def test_a_schema_2_contract_is_read_as_it_is(tmp_path, monkeypatch):
    v2 = tmp_path / "contract.json"
    v2.write_text(json.dumps(SMALL_V2))
    monkeypatch.setattr(sys.modules[__name__], "CONTRACT", v2)

    assert contract() == SMALL_V2


@pytest.mark.parametrize(
    ("text", "why"),
    [("{not json", "cannot be read as JSON"), ("[]", "is a JSON list, not an object"),
     ('{"status --json": {"": ["schema"]}}', "has no schema \\(a v1 contract\\)"),
     ('{"schema": 3, "commands": {}}', "says schema 3; this test reads schema 2"),
     ('{"schema": "2"}', "says schema '2'")],
)
def test_a_contract_that_is_not_schema_2_fails_naming_the_file(
    tmp_path, monkeypatch, text, why
):
    broken = tmp_path / "contract.json"
    broken.write_text(text)
    monkeypatch.setattr(sys.modules[__name__], "CONTRACT", broken)

    with pytest.raises(pytest.fail.Exception, match=f"{broken}.*{why}"):
        contract()


def test_a_missing_contract_fails_naming_the_file(tmp_path, monkeypatch):
    missing = tmp_path / "contract.json"
    monkeypatch.setattr(sys.modules[__name__], "CONTRACT", missing)

    with pytest.raises(pytest.fail.Exception, match=f"{missing} cannot be read"):
        contract()


@pytest.mark.parametrize(
    ("doc", "problem"),
    [({"schema": 1}, "not schema 2"),
     ({"schema": 2, "commands": {}}, "no commands"),
     ({"schema": 2, "commands": {"x": {"": {"a": "text"}}}}, "x '': 'a' has no type 'text'"),
     ({"schema": 2, "commands": {"x": {"": {"a": "any|none"}}}},
      "x '': 'a' has no type 'any|none'"),
     ({"schema": 2, "commands": {"x": {"": {"a": "string"}}}}, f"no {PULL} stream"),
     ({"schema": 2, "commands": {"x": {"": {"a": "string"}}}, "streams": {PULL: {
         "events": {"done": {}}, "terminal": ["done", "error"], "exit": {"done": 0}}}},
      f"{PULL}: the endings are not all events"),
     ({"schema": 2, "commands": {"x": {"": {"a": "string"}}}, "streams": {
         PULL: SMALL_V2["streams"][PULL], VOICE_HERE: {
             "events": {"done": {}}, "terminal": ["done"], "exit": {}}}},
      f"{VOICE_HERE}: done has no exit code")],
)
def test_a_contract_that_is_not_schema_2_says_why(doc, problem):
    assert problem in shape_problems(doc)


@pytest.mark.parametrize(
    ("value", "expected", "problem"),
    [("a", "string", None), (None, "string|null", None), (None, "string", "is null"),
     (3, "integer", None), (3.5, "integer", "is float, not integer"),
     (True, "integer", "is a boolean, not integer"), (3, "number", None),
     (2.5, "number", None), (False, "number", "is a boolean, not number"),
     (False, "boolean", None), (0, "boolean", "is int, not boolean"),
     ([], "array", None), ({}, "object", None), ([], "object", "is list, not object"),
     ("a", "any", None), (False, "any", None), (0, "any", None), ([1], "any", None),
     ({}, "any", None), (None, "any", "is null"), (None, "any|null", None),
     ("a", "anything", "has the unknown type 'anything'"),
     (None, "anything|null", "has the unknown type 'anything|null'"),
     ("a", "string|none", "has the unknown type 'string|none'")],
)
def test_a_value_meets_its_type_or_says_why_not(value, expected, problem):
    assert type_problem(value, expected) == problem


def test_a_missing_key_a_wrong_type_and_an_unknown_type_are_all_named():
    keys = {"id": "integer", "text": "string|null", "value": "anything", "request_id": "any"}
    assert key_problems({"id": "1", "value": 1}, keys) == [
        "'id' is str, not integer", "has no 'text'",
        "'value' has the unknown type 'anything'", "has no 'request_id'",
    ]
    assert key_problems([], {"id": "integer"}) == ["is list, not an object"]


def test_a_path_steps_into_objects_and_through_every_element_of_a_list():
    document = {"servers": {"llm": {"port": 1}}, "tasks": [{"id": 1}, {"id": 2}]}

    assert nodes_at(document, "") == [document]
    assert nodes_at(document, "servers.llm") == [{"port": 1}]
    assert nodes_at(document, "tasks[]") == [{"id": 1}, {"id": 2}]
    assert nodes_at([{"key": "A"}], "[]") == [{"key": "A"}]
    with pytest.raises(AssertionError, match="empty list"):
        nodes_at({"tasks": []}, "tasks[]")


# --- the commands ------------------------------------------------------------------------


def llama_server(request: httpx.Request) -> httpx.Response:
    """httpx's transport, faked: a model server on loopback that has `SERVED` loaded, and
    nothing anywhere else."""
    if request.url.host not in {"127.0.0.1", "localhost"}:
        raise httpx.ConnectError("no network in tests", request=request)
    if request.url.path.endswith("/models"):
        return httpx.Response(200, json={"object": "list", "data": [{"id": SERVED}]},
                              request=request)
    return httpx.Response(404, request=request)


@pytest.fixture
def seeded(tmp_path, monkeypatch, every_agent_installed):
    """A store with a told task (report, log, usage), a running one, saved settings, a
    plugin on, a failed restart, approvals, a memory, a model on disk and a model server
    that answers."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("PROJECTS_ROOT", str(tmp_path / "projects"))
    ConfigStore().set({"ASSISTANT_NAME": "Vega", "LLM_SERVER_MODEL": MODEL.key,
                       "VOICE_BASE_URL": "http://127.0.0.1:8765/v1",
                       "LOCAL_AGENT_BASE_URL": "http://127.0.0.1:8090/v1",
                       "LOCAL_AGENT_MODEL": MODEL.key})
    settings = Settings()
    settings.ensure_dirs()

    report = settings.data_dir / "tasks" / f"{TASK_ID}.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("# Task 1\n\ncheck the logs\n\n---\n\nAll fine.\n")
    now = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
    told = Task(id=None, kind=TaskKind.AGENT, description="check the logs", project="keryx",
                status=TaskStatus.DONE, report_path=str(report), summary="All fine.",
                input_tokens=10, output_tokens=2, cost_usd=0.01, claude_session_id="s-1",
                callback_requested=True, needs_restart=True, started_at=now,
                finished_at=now, reported_at=now)
    running = Task(id=None, kind=TaskKind.AGENT, description="build it", project="keryx",
                   status=TaskStatus.RUNNING, started_at=now)

    async def seed() -> None:
        store = TaskStore(settings.data_dir / "tasks.db")
        try:
            assert (await store.create(told)).id == TASK_ID
            await store.create(running)
        finally:
            await store.close()

    asyncio.run(seed())
    task_log_path(settings.data_dir, TASK_ID).write_text(
        "[09:00:01] read the logs\n[09:00:05] [followup] and the old ones\n"
    )
    audit = settings.state_dir / "approvals" / "audit.jsonl"
    audit.parent.mkdir(parents=True, exist_ok=True)
    audit.write_text("".join(json.dumps(line) + "\n" for line in [
        {"ts": "2026-10-05T09:00:00+00:00", "event": "raised", "request_id": 1,
         "summary": "Claude wants to run: ls"},
        {"ts": "2026-10-05T09:01:00+00:00", "event": "settled", "request_id": 1,
         "escalation": "cancelled", "outcome": "resolved_elsewhere"},
        {"ts": None, "event": 3, "summary": 7},  # an odd line still decodes as strings
    ]))
    memory_path(settings.data_dir).write_text("## Standing facts\n\n- Likes it short.\n")
    RestartStore(settings.state_dir / RECORD_NAME).save(RestartRecord(
        requested_at=now.isoformat(), state="failed", error="launchctl exited 5",
    ))

    # A plugin on, for the plugin commands to change and turn off.
    plugins.install(settings, "check_billing")
    # The integrations: `gh` signed in, though only issue reports turned on would ask it.
    monkeypatch.setattr("keryx.issues.gh_status",
                        lambda: GhStatus(installed=True, signed_in=True, account="ada"))
    # The status command's probes: a service installed and running, two calls live.
    monkeypatch.setattr(status, "default_probes", lambda: status.StatusProbes(
        target=lambda settings: ServiceTarget("launchd", "dev.keryx.agent"),
        installed=lambda target: True, active=lambda target: True,
        health=lambda settings: 2, running_version=lambda settings: "v0.1.0-3-g1ad6420",
        checkout_version=lambda settings: "v0.1.0-5-gabc1234",
    ))
    # The models commands: a known machine, its units, and one model on disk (sparse).
    monkeypatch.setattr(hardware, "detect", lambda: Hardware("metal", "Apple silicon", 0.0, 36.0))
    monkeypatch.setattr(hardware, "free_bytes", lambda path: 10**12)
    monkeypatch.setattr(use, "unit_installed", lambda kind: True)
    monkeypatch.setattr(servers, "free_port", lambda preferred: preferred)
    monkeypatch.setattr("keryx.localmodels.runtimes.llama_server",
                        lambda cache_dir, **_: "/opt/homebrew/bin/llama-server")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request",
                        lambda self, request: llama_server(request))
    # The voice commands: speech-to-speech installed, and a server elsewhere that answers.
    monkeypatch.setattr(runtimes, "speech_to_speech", lambda: S2S)
    monkeypatch.setattr("keryx.realtime.openai.realtime_problem", lambda endpoint: None)
    model = download.model_path(settings.cache_dir, MODEL)
    model.parent.mkdir(parents=True, exist_ok=True)
    with model.open("wb") as out:
        out.truncate(MODEL.size)
    # The service commands: every unit installed, the tunnel stopped, the installers faked.
    monkeypatch.setattr(units, "status", partial(
        units.status, installed=lambda target: True,
        active=lambda target: target.unit != "dev.keryx.tunnel"))
    monkeypatch.setattr(units, "run_captured", lambda argv: (0, "loaded\n", ""))
    monkeypatch.setattr(voice, "PLATFORM", "darwin")
    installers = tmp_path / "checkout" / "scripts"  # `keryx.cli.repo_root`, the suite's own
    installers.mkdir(parents=True)
    for name in ("install-launchd.sh", "install-systemd.sh"):
        (installers / name).write_text("#!/bin/sh\n")
    return settings


def test_the_contract_names_every_command_the_app_reads_as_json():
    assert set(contract()["commands"]) == COMMANDS


def test_the_contract_names_every_stream_the_app_reads_as_json_lines():
    assert set(contract()["streams"]) == set(STREAMS)


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_every_key_the_app_needs_is_printed_with_its_type(command, seeded):
    if command not in contract()["commands"]:
        # The cross-check above fails for it, once: a promise the app has yet to make.
        pytest.skip(f"the contract does not name {command} yet")
    result = runner.invoke(app, argv(command), input=STDIN.get(command))

    assert result.exit_code in ((0, 1) if command == "doctor --json" else (0,)), result.output
    document = json.loads(result.stdout)
    # A command the contract has yet to name is the cross-check's failure, not this one's.
    problems = [
        f"{path or 'the document'} {problem}"
        for path, keys in contract()["commands"].get(command, {}).items()
        for node in nodes_at(document, path)
        for problem in key_problems(node, keys)
    ]
    assert not problems, f"{command}: {problems}"


def test_the_seed_makes_the_values_the_contract_checks_real_ones(seeded):
    """A null meets any `|null`, so the seed must make the nullable values worth checking."""
    models = json.loads(runner.invoke(app, ["models", "list", "--json"]).stdout)
    tasks = json.loads(runner.invoke(app, ["tasks", "list", "--json"]).stdout)["tasks"]
    report = json.loads(runner.invoke(app, ["status", "--json"]).stdout)
    options = json.loads(runner.invoke(app, argv("config options KEY --json")).stdout)
    voice_status = json.loads(runner.invoke(app, ["voice", "--json"]).stdout)
    voice_server = json.loads(runner.invoke(app, argv("voice use server URL --json")).stdout)

    assert models["servers"]["llm"]["reachable"] is True
    assert models["servers"]["llm"]["served_model"] == SERVED
    assert models["servers"]["llm"]["model"] == MODEL.key
    assert {task["status"] for task in tasks} == {"running", "done"}
    assert any(task["told"] for task in tasks)
    assert report["restart"]["failure"] == "launchctl exited 5"
    assert report["health"]["live_sessions"] == 2
    assert report["assistant"]["name"] == "Vega"
    assert (options["live"], options["source"]) == (True, "server")
    assert {option["detail"] for option in options["options"]} == {None, "current"}
    assert voice_status["base_url"] == "http://127.0.0.1:8765/v1"
    assert voice_status["here"]["program"] == S2S
    assert voice_status["here"]["words_model"] == MODEL.key
    assert voice_server["saved"]["VOICE_BASE_URL"] == f"{VOICE_SERVER}/v1"


# --- the stream: models pull --json ------------------------------------------------------


def _missing(settings: Settings) -> None:
    """The seeded copy gone, so there is something to download."""
    download.model_path(settings.cache_dir, MODEL).unlink()


def _done(monkeypatch, settings):
    _missing(settings)
    monkeypatch.setattr(download, "already_on_disk", lambda entry: None)

    def fetch(url, dest, *, size, sha256, progress):
        progress(0)
        progress(size)
        return dest

    monkeypatch.setattr(download, "download", fetch)


def _adopted(monkeypatch, settings):
    _missing(settings)
    found = settings.data_dir / MODEL.file  # anywhere that is not CACHE_DIR/models
    found.write_bytes(b"x")
    monkeypatch.setattr(download, "already_on_disk", lambda entry: found)


def _failed(monkeypatch, settings):
    _missing(settings)
    monkeypatch.setattr(download, "already_on_disk", lambda entry: None)

    def broken(url, dest, *, size, sha256, progress):
        progress(1)
        raise download.DownloadError("did not arrive whole")

    monkeypatch.setattr(download, "download", broken)


def _unknown(monkeypatch, settings):
    return ["models", "pull", "no-such-model", "--json"]


def _migration_refused(monkeypatch, settings):
    old = Path.home() / ".local" / "share" / "jarvis"
    old.mkdir(parents=True)
    (old / "memory.md").write_text("# Memory\n")
    assert Settings().storage_refusal()


def _cancelled(monkeypatch, settings):
    _missing(settings)
    monkeypatch.setattr(download, "already_on_disk", lambda entry: None)

    def interrupted(url, dest, *, size, sha256, progress):
        progress(1)
        raise KeyboardInterrupt  # what the SIGTERM handler turns the app's Cancel into

    monkeypatch.setattr(download, "download", interrupted)


#: Each way a pull ends: how to arrange it, its ending, its exit code, and whether a
#: download ran (so `progress` came first).
PULLS = [(_done, "done", 0, True), (_adopted, "adopted", 0, False),
         (_failed, "error", 1, True), (_unknown, "error", 2, False),
         (_migration_refused, "error", 2, False), (_cancelled, "cancelled", 130, True)]


def test_the_pulls_below_reach_every_event_the_contract_names():
    stream = contract()["streams"][PULL]

    assert {ending for _, ending, _, _ in PULLS} == set(stream["terminal"])
    assert set(stream["events"]) == {*stream["terminal"], "progress"}


@pytest.mark.parametrize(("arrange", "ending", "code", "progressed"), PULLS)
def test_a_pull_streams_the_events_the_app_reads_and_ends_once(
    seeded, monkeypatch, arrange, ending, code, progressed
):
    stream = contract()["streams"][PULL]
    command = arrange(monkeypatch, seeded) or ["models", "pull", MODEL.key, "--json"]

    result = runner.invoke(app, command)

    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert events, "no events at all"
    for event in events:
        assert event.get("event") in stream["events"], event
        assert not key_problems(event, stream["events"][event["event"]]), event
    endings = [event["event"] for event in events if event["event"] in stream["terminal"]]
    assert endings == [ending] and events[-1]["event"] == ending
    assert (events[0]["event"] == "progress") is progressed
    allowed = stream["exit"][ending]
    assert result.exit_code == code
    assert code in (allowed if isinstance(allowed, list) else [allowed])


# --- the stream: voice use here --json ---------------------------------------------------


def _voice_done(monkeypatch, settings, scripts):
    scripts.code = 0


def _voice_failed(monkeypatch, settings, scripts):
    scripts.code = 1  # the speech-to-speech install does not finish


def _voice_refused(monkeypatch, settings, scripts):
    ConfigStore().set({"LOCAL_AGENT_BASE_URL": None, "LOCAL_AGENT_MODEL": None})


def _voice_cancelled(monkeypatch, settings, scripts):
    def interrupted(argv):
        raise KeyboardInterrupt  # what the SIGTERM handler turns the app's Cancel into

    monkeypatch.setattr(voice, "run_detached", interrupted)


class Scripts:
    """The installers `voice use here` runs, faked: what they were asked, and their code."""

    def __init__(self) -> None:
        self.code = 0
        self.ran: list[list[str]] = []

    def __call__(self, argv) -> int:
        self.ran.append(list(argv))
        return self.code


#: Each way the voice set-up ends: how to arrange it, its ending, its exit code, and
#: whether a step was told first.
VOICES = [(_voice_done, "done", 0, True), (_voice_failed, "error", 1, True),
          (_voice_refused, "error", 2, False), (_voice_cancelled, "cancelled", 130, True)]


def test_the_voice_set_ups_below_reach_every_event_the_contract_names():
    stream = contract()["streams"][VOICE_HERE]

    assert {ending for _, ending, _, _ in VOICES} == set(stream["terminal"])
    assert set(stream["events"]) == {*stream["terminal"], "step"}


@pytest.mark.parametrize(("arrange", "ending", "code", "stepped"), VOICES)
def test_a_voice_set_up_streams_the_events_the_app_reads_and_ends_once(
    seeded, monkeypatch, tmp_path, arrange, ending, code, stepped
):
    stream = contract()["streams"][VOICE_HERE]
    scripts = Scripts()
    monkeypatch.setattr(runtimes, "speech_to_speech", lambda: None)  # so it is installed
    monkeypatch.setattr(voice, "run_detached", scripts)
    monkeypatch.setattr(servers, "wait_for", lambda endpoint, kind, timeout: None)
    arrange(monkeypatch, seeded, scripts)

    result = runner.invoke(app, VOICE_HERE.split())

    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert events, "no events at all"
    for event in events:
        assert event.get("event") in stream["events"], event
        assert not key_problems(event, stream["events"][event["event"]]), event
    endings = [event["event"] for event in events if event["event"] in stream["terminal"]]
    assert endings == [ending] and events[-1]["event"] == ending
    assert (events[0]["event"] == "step") is stepped
    allowed = stream["exit"][ending]
    assert result.exit_code == code
    assert code in (allowed if isinstance(allowed, list) else [allowed])


# --- the stream: models start --json -----------------------------------------------------


def _start_done(monkeypatch, settings, scripts):
    pass


def _start_failed(monkeypatch, settings, scripts):
    monkeypatch.setattr(servers, "wait_for", lambda endpoint, kind, timeout: "no answer")


def _start_refused(monkeypatch, settings, scripts):
    ConfigStore().set({"LLM_SERVER_MODEL": None})


def _start_cancelled(monkeypatch, settings, scripts):
    def interrupted(argv):
        raise KeyboardInterrupt  # what the SIGTERM handler turns the app's Cancel into

    monkeypatch.setattr(voice, "run_detached", interrupted)


#: Each way `models start` ends: how to arrange it, its ending, its exit code, and whether a
#: step was told first.
STARTS = [(_start_done, "done", 0, True), (_start_failed, "error", 1, True),
          (_start_refused, "error", 2, False), (_start_cancelled, "cancelled", 130, True)]


def start_stream() -> dict:
    """The contract's promise for `models start --json`, once the app has made it."""
    stream = contract()["streams"].get(MODELS_START)
    if stream is None:
        # The streams' cross-check fails for it, once: a promise the app has yet to make.
        pytest.skip(f"the contract does not name the {MODELS_START} stream yet")
    return stream


def test_the_starts_below_reach_every_event_the_contract_names():
    stream = start_stream()

    assert {ending for _, ending, _, _ in STARTS} == set(stream["terminal"])
    assert set(stream["events"]) == {*stream["terminal"], "step", "progress"}


@pytest.mark.parametrize(("arrange", "ending", "code", "stepped"), STARTS)
def test_a_model_server_start_streams_the_events_the_app_reads_and_ends_once(
    seeded, monkeypatch, arrange, ending, code, stepped
):
    stream = start_stream()
    scripts = Scripts()
    monkeypatch.setattr(voice, "run_detached", scripts)
    monkeypatch.setattr(servers, "wait_for", lambda endpoint, kind, timeout: None)
    monkeypatch.setattr(use, "probe", lambda url: (True, MODEL.key))
    arrange(monkeypatch, seeded, scripts)

    result = runner.invoke(app, MODELS_START.split())

    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert events, "no events at all"
    for event in events:
        assert event.get("event") in stream["events"], event
        assert not key_problems(event, stream["events"][event["event"]]), event
    endings = [event["event"] for event in events if event["event"] in stream["terminal"]]
    assert endings == [ending] and events[-1]["event"] == ending
    assert (events[0]["event"] == "step") is stepped
    allowed = stream["exit"][ending]
    assert result.exit_code == code
    assert code in (allowed if isinstance(allowed, list) else [allowed])


# --- the streams: auth login NAME --json -------------------------------------------------


def _child(monkeypatch, script, *, code=0):
    """The agent's CLI, scripted, in place of a real one behind a terminal."""
    from keryx.setup import auth_stream

    monkeypatch.setattr(auth_stream, "PtyChild",
                        lambda argv, env: FakeChild(script, Clock(), code=code))


def _google_client(settings) -> list[str]:
    client = settings.data_dir / "client.json"
    client.write_text('{"installed": {"client_id": "cid", "client_secret": "cs"}}')
    return ["--client-file", str(client)]


def _signed_in(settings):
    return lambda settings, redirected_to, *, post=None: token_path(settings)


def _consented(settings, *, echo, timeout_s):
    echo(f"Open {CONSENT} to authorize")
    return True


def _interrupted(*args, **kwargs):
    raise KeyboardInterrupt  # what the SIGTERM handler turns the app's Cancel into


def _refused(*args, **kwargs):
    raise google.GoogleSetupError("no new credentials appeared")


#: Each way a sign-in ends, per name: how to arrange it (returning extra argv and stdin), its
#: ending, and its exit code.
SIGN_INS = {
    "claude": [
        (lambda mp, s: _child(mp, [*SIGN_IN, TYPED, *TOKEN_SHOWN]) or ([], "c#s\n"), "done", 0),
        (lambda mp, s: _child(mp, ["error: unknown command\r\n"], code=1) or ([], ""),
         "error", 1),
        (lambda mp, s: _child(mp, [*SIGN_IN[:5], KeyboardInterrupt()]) or ([], ""),
         "cancelled", 130),
    ],
    "codex": [
        (lambda mp, s: _child(mp, [DEVICE_AUTH]) or ([], ""), "done", 0),
        (lambda mp, s: _child(mp, ["Usage: codex login\r\n"], code=2) or ([], ""), "error", 1),
        (lambda mp, s: _child(mp, [DEVICE_AUTH, KeyboardInterrupt()]) or ([], ""),
         "cancelled", 130),
    ],
    "gmail": [
        (lambda mp, s: mp.setattr(google, "finish_signin", _signed_in(s))
         or (_google_client(s), "http://localhost:1/?code=c\n"), "done", 0),
        (lambda mp, s: (_google_client(s), ""), "error", 1),
        (lambda mp, s: mp.setattr(google, "finish_signin", _interrupted)
         or (_google_client(s), "http://localhost:1/?code=c\n"), "cancelled", 130),
    ],
    "google-workspace": [
        (lambda mp, s: mp.setattr(google, "run_google_setup", _consented)
         or (_google_client(s), ""), "done", 0),
        (lambda mp, s: mp.setattr(google, "run_google_setup", _refused)
         or (_google_client(s), ""), "error", 1),
        (lambda mp, s: mp.setattr(google, "run_google_setup", _interrupted)
         or (_google_client(s), ""), "cancelled", 130),
    ],
}
#: The events each sign-in has besides its endings.
SIGN_IN_EVENTS = {"claude": {"open", "code"}, "codex": {"open"}, "gmail": {"open"},
                  "google-workspace": {"open"}}


def sign_in_stream(name: str) -> dict:
    """The contract's promise for `auth login NAME --json`, once the app has made it."""
    stream = contract()["streams"].get(AUTH_LOGINS[name])
    if stream is None:
        # The streams' cross-check fails for it, once: a promise the app has yet to make.
        pytest.skip(f"the contract does not name the {AUTH_LOGINS[name]} stream yet")
    return stream


@pytest.mark.parametrize("name", sorted(SIGN_INS))
def test_the_sign_ins_below_reach_every_event_the_contract_names(name):
    stream = sign_in_stream(name)

    assert {ending for _, ending, _ in SIGN_INS[name]} == set(stream["terminal"])
    assert set(stream["events"]) == {*stream["terminal"], *SIGN_IN_EVENTS[name]}


@pytest.mark.parametrize(
    ("name", "arrange", "ending", "code"),
    [(name, *way) for name, ways in sorted(SIGN_INS.items()) for way in ways],
)
def test_a_sign_in_streams_the_events_the_app_reads_and_ends_once(
    seeded, monkeypatch, every_agent_installed, name, arrange, ending, code
):
    stream = sign_in_stream(name)
    extra, stdin = arrange(monkeypatch, seeded)

    result = runner.invoke(app, [*AUTH_LOGINS[name].split(), *extra], input=stdin)

    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert events, "no events at all"
    for event in events:
        assert event.get("event") in stream["events"], event
        assert not key_problems(event, stream["events"][event["event"]]), event
    endings = [event["event"] for event in events if event["event"] in stream["terminal"]]
    assert endings == [ending] and events[-1]["event"] == ending
    allowed = stream["exit"][ending]
    assert result.exit_code == code
    assert code in (allowed if isinstance(allowed, list) else [allowed])


def test_every_way_a_sign_in_ends_streams_one_ending_whatever_the_contract_says(
    seeded, monkeypatch, every_agent_installed
):
    """Until the app names these streams, the rule every stream keeps is still checked."""
    for name, ways in SIGN_INS.items():
        for arrange, ending, code in ways:
            with monkeypatch.context() as patch:
                extra, stdin = arrange(patch, seeded)
                result = runner.invoke(app, [*AUTH_LOGINS[name].split(), *extra], input=stdin)
            events = [json.loads(line) for line in result.stdout.splitlines()]
            assert [e["event"] for e in events if e["event"] in ("done", "error", "cancelled")] \
                == [ending], (name, events)
            assert events[-1]["event"] == ending and result.exit_code == code, (name, events)
