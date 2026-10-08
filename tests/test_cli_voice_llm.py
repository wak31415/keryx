"""`keryx voice llm shared|here|remote`, `keryx models start --voice-llm`, and the voice LLM
a first `keryx voice use here` chooses (the owner's rule for a new install)."""

import json

import pytest
from typer.testing import CliRunner
from wording import for_the_app

from keryx.cli import app
from keryx.config import Settings
from keryx.config.store import ConfigStore
from keryx.endpoints import Probe
from keryx.localmodels import download, hardware, pull, runtimes, servers, speech, use, voice
from keryx.localmodels.catalog import by_key
from keryx.localmodels.hardware import Hardware

runner = CliRunner()
TERMINAL = {"done", "error", "cancelled"}
SMALL, NINE = by_key("qwen3.5-4b"), by_key("qwen3.5-9b")
OWN = "http://127.0.0.1:8091/v1"
WORDS = {"LOCAL_AGENT_BASE_URL": "http://127.0.0.1:8090/v1",
         "LOCAL_AGENT_MODEL": "qwen3-coder-30b-a3b"}
#: 48 GB for models: room for a voice LLM of the voice's own beside nothing else here.
MAC_64 = Hardware("metal", "Apple silicon", 0.0, 64.0)
#: 6 GB for models: no room for one.
MAC_8 = Hardware("metal", "Apple silicon", 0.0, 8.0)


class Machine:
    """What the machine says: its units, which model each loopback port serves, the
    installers' codes, the waits, and a server elsewhere's model list."""

    def __init__(self) -> None:
        self.units: set[str] = set()
        #: What each loopback `…/v1` serves: (up, model).
        self.serving: dict[str, tuple[bool, str | None]] = {}
        self.scripts: list[list[str]] = []
        self.codes: dict[tuple[str, ...], int] = {}
        self.wait: object = None
        self.pulls: list[str] = []
        self.listed = Probe(True, ("gpt-5-mini",))
        self.live: int | None = None
        self.asked: list[tuple[str, str | None]] = []

    def run(self, argv) -> int:
        self.scripts.append(list(argv))
        tail = tuple(argv[1:])
        if tail == ("--voice-llm",) and self.codes.get(tail, 0) == 0:
            self.units.add("voice_llm")
        if tail == ("--voice-llm", "--uninstall"):
            self.units.discard("voice_llm")
        return self.codes.get(tail, 0)

    def flags(self) -> list[list[str]]:
        return [argv[1:] for argv in self.scripts if argv[0].endswith(".sh")]


def on_disk(settings: Settings, entry) -> None:
    path = download.model_path(settings.cache_dir, entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as out:
        out.truncate(entry.size)


@pytest.fixture
def machine(monkeypatch, tmp_path, every_agent_installed):
    found = Machine()
    scripts = tmp_path / "checkout" / "scripts"  # `keryx.cli.repo_root`, the suite's own
    scripts.mkdir(parents=True)
    for name in ("install-launchd.sh", "install-systemd.sh"):
        (scripts / name).write_text("#!/bin/sh\n")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-owner")
    monkeypatch.setattr(voice, "PLATFORM", "darwin")
    monkeypatch.setattr(voice, "run_detached", found.run)
    monkeypatch.setattr(use, "unit_installed", lambda kind: kind in found.units)
    monkeypatch.setattr(use, "probe", lambda url: found.serving.get(url, (False, None)))
    monkeypatch.setattr(servers, "free_port", lambda port: port)
    monkeypatch.setattr(runtimes, "llama_server", lambda cache_dir, **_: "/usr/bin/llama-server")
    monkeypatch.setattr(hardware, "detect", lambda: MAC_8)
    # Keryx's live calls, never the running service's own: none is live unless a test says.
    monkeypatch.setattr(voice, "live_calls", lambda settings: found.live)
    monkeypatch.setattr(hardware, "free_bytes", lambda path: 10**12)

    def wait_for(endpoint, kind, timeout):
        if isinstance(found.wait, BaseException):
            raise found.wait
        if found.wait is None and kind == "models":  # it loaded: it serves what it was given
            model = ConfigStore().stored().get("VOICE_LLM_MODEL")
            found.serving[endpoint.base_url] = (True, model)
        return found.wait

    monkeypatch.setattr(servers, "wait_for", wait_for)

    def fake_pull(settings, name, report):
        found.pulls.append(name)
        with report.downloading(by_key(name).title, 100) as progress:
            progress(100)
        on_disk(settings, by_key(name))
        return 0

    monkeypatch.setattr(pull, "pull", fake_pull)

    def remote(endpoint, get):
        found.asked.append((endpoint.base_url, endpoint.api_key))
        return found.listed

    monkeypatch.setattr("keryx.endpoints.probe", remote)
    return found


def events_of(result) -> list[dict]:
    return [json.loads(line) for line in result.stdout.splitlines()]


def ends_once(result, ending: str, code: int) -> list[dict]:
    events = events_of(result)
    assert [e["event"] for e in events if e["event"] in TERMINAL] == [ending], events
    assert events[-1]["event"] == ending and result.exit_code == code, events
    for event in events:
        if event["event"] in ("step", "error"):
            assert "detail" in event and for_the_app(event["message"]), event
    assert result.stderr == ""
    return events


# --- keryx voice llm here ------------------------------------------------------------------


def test_voice_llm_here_downloads_starts_checks_then_saves_it(machine, monkeypatch):
    monkeypatch.setattr(hardware, "detect", lambda: MAC_64)  # room for it: no warning

    result = runner.invoke(app, ["voice", "llm", "here", SMALL.key, "--json"])

    events = ends_once(result, "done", 0)
    assert [e["step"] for e in events if e["event"] == "step"] == [
        "download", "runtime", "unit", "start", "check", "configure"]
    assert [(e["step"], e["done"]) for e in events if e["event"] == "progress"] == [
        ("download", 100)]
    assert events[-1] == {"event": "done", "source": "here", "model": SMALL.key, "url": OWN,
                          "voice_restarted": False, "voice_restart_needed": False}
    assert machine.pulls == [SMALL.key] and machine.flags() == [["--voice-llm"]]
    stored = ConfigStore().stored()
    assert (stored["VOICE_LLM"], stored["VOICE_LLM_MODEL"], stored["VOICE_LLM_SERVER_PORT"]) \
        == ("here", SMALL.key, 8091)


def test_voice_llm_here_on_a_small_machine_warns_and_goes_on(machine):
    result = runner.invoke(app, ["voice", "llm", "here", SMALL.key, "--json"])

    events = ends_once(result, "done", 0)
    [warning] = [e for e in events if "may not all fit" in e.get("message", "")]
    assert (warning["event"], warning["step"]) == ("step", "download")


def test_voice_llm_here_in_words(machine):
    result = runner.invoke(app, ["voice", "llm", "here", SMALL.key])

    assert result.exit_code == 0, result.output
    assert f"the voice's words now come from {SMALL.key}, on its own server here" \
        in result.stdout


def test_voice_llm_here_restarts_a_running_voice_server_onto_it(machine):
    ConfigStore().set({"VOICE_MODE": "here"})
    machine.units.add("voice")

    plain = runner.invoke(app, ["voice", "llm", "here", SMALL.key])
    result = runner.invoke(app, ["voice", "llm", "here", SMALL.key, "--json"])

    assert "the voice server here was restarted onto them, and answers" in plain.stdout
    events = ends_once(result, "done", 0)
    assert "voice" in [e["step"] for e in events if e["event"] == "step"]
    assert (events[-1]["voice_restarted"], events[-1]["voice_restart_needed"]) == (True, False)
    assert machine.flags()[-1] == ["--voice"]


def test_voice_llm_here_says_a_voice_here_with_no_server_takes_it_when_started(machine):
    ConfigStore().set({"VOICE_MODE": "here"})

    plain = runner.invoke(app, ["voice", "llm", "here", SMALL.key])

    assert "`keryx voice use here` starts it" in plain.stdout


def test_a_voice_llm_here_that_does_not_come_up_leaves_the_previous_settings(machine):
    ConfigStore().set(WORDS)
    before = ConfigStore().stored()
    machine.wait = "could not reach 127.0.0.1"

    result = runner.invoke(app, ["voice", "llm", "here", SMALL.key, "--json"])

    events = ends_once(result, "error", 1)
    assert events[-1]["message"] == ("The voice LLM server did not come up, so its service "
                                     "was removed.")
    assert "keryx-voice-llm.err.log" in events[-1]["detail"]
    assert machine.flags()[-1] == ["--voice-llm", "--uninstall"] and machine.units == set()
    assert ConfigStore().stored() == before


def test_a_voice_llm_here_cancelled_while_it_loads_removes_its_unit(machine):
    machine.wait = KeyboardInterrupt()

    result = runner.invoke(app, ["voice", "llm", "here", SMALL.key, "--json"])

    ends_once(result, "cancelled", 130)
    assert machine.units == set() and "VOICE_LLM" not in ConfigStore().stored()


def test_voice_llm_here_in_words_is_cancelled_by_ctrl_c(machine):
    machine.wait = KeyboardInterrupt()

    result = runner.invoke(app, ["voice", "llm", "here", SMALL.key])

    assert result.exit_code == 130 and "picks up where this stopped" in result.stderr


def test_voice_llm_here_refuses_a_model_it_does_not_know_before_anything(machine):
    result = runner.invoke(app, ["voice", "llm", "here", "no-such-model", "--json"])

    [event] = ends_once(result, "error", 2)
    assert event["message"] == "There is no model called no-such-model."
    assert machine.scripts == [] and ConfigStore().stored() == {}


@pytest.mark.parametrize("command", [
    ["voice", "llm", "here", SMALL.key, "--json"], ["voice", "llm", "shared", "--json"],
    ["voice", "llm", "remote", "https://api.openai.com/v1", "--model", "m", "--json"],
])
def test_voice_llm_is_refused_to_the_service_with_one_error_event(machine, monkeypatch,
                                                                  command):
    monkeypatch.setenv("KERYX_ACTOR", "service")

    result = runner.invoke(app, command)

    [event] = ends_once(result, "error", 1)
    assert event["message"].startswith("only the owner may choose the voice LLM")
    assert machine.scripts == [] and machine.asked == [] and ConfigStore().stored() == {}


@pytest.mark.parametrize("command", [
    ["voice", "llm", "here", SMALL.key], ["voice", "llm", "shared"],
    ["voice", "llm", "remote", "https://api.openai.com/v1", "--model", "m"],
])
def test_voice_llm_in_words_is_refused_to_the_service(machine, monkeypatch, command):
    monkeypatch.setenv("KERYX_ACTOR", "service")

    result = runner.invoke(app, command)

    assert result.exit_code == 1 and "only the owner may" in result.stderr
    assert result.stdout == "" and machine.scripts == []


# --- keryx voice llm remote ----------------------------------------------------------------


def test_voice_llm_remote_takes_its_key_on_stdin_and_never_says_it(machine):
    machine.listed = Probe(True, ())
    result = runner.invoke(app, ["voice", "llm", "remote", "https://llm.example.com",
                                 "--model", "small", "--key-stdin", "--json"],
                           input="vk-secret\n")

    events = ends_once(result, "done", 0)
    assert events[-1] == {"event": "done", "source": "remote", "model": "small",
                          "url": "https://llm.example.com/v1", "voice_restarted": False,
                          "voice_restart_needed": False}
    assert machine.asked == [("https://llm.example.com/v1", "vk-secret")]
    assert "vk-secret" not in result.output
    assert ConfigStore()._secrets()["VOICE_LLM_API_KEY"] == "vk-secret"
    assert ConfigStore().stored()["VOICE_LLM_BASE_URL"] == "https://llm.example.com/v1"


def test_voice_llm_remote_on_openai_is_asked_with_the_owners_key_and_saves_none(machine):
    result = runner.invoke(app, ["voice", "llm", "remote", "https://api.openai.com/v1",
                                 "--model", "gpt-5-mini", "--json"])

    ends_once(result, "done", 0)
    assert machine.asked == [("https://api.openai.com/v1", "sk-owner")]
    assert "VOICE_LLM_API_KEY" not in ConfigStore()._secrets()
    assert "sk-owner" not in result.output


def test_voice_llm_remote_keeps_the_stored_key_unless_told_to_forget_it(machine):
    ConfigStore().set({"VOICE_LLM_API_KEY": "vk-stored"})
    machine.listed = Probe(True, ())

    kept = runner.invoke(app, ["voice", "llm", "remote", "http://gpu-box:11434", "--model",
                               "m", "--json"])
    cleared = runner.invoke(app, ["voice", "llm", "remote", "http://gpu-box:11434", "--model",
                                  "m", "--clear-key", "--json"])

    assert kept.exit_code == cleared.exit_code == 0
    assert [key for _url, key in machine.asked] == ["vk-stored", None]
    assert "VOICE_LLM_API_KEY" not in ConfigStore()._secrets()


@pytest.mark.parametrize(("extra", "stdin", "said"), [
    (["--key-stdin", "--clear-key"], "vk\n", "not both"),
    (["--key-stdin"], "\n", "There was no key on standard input."),
])
def test_voice_llm_remote_refuses_a_key_it_cannot_take(machine, extra, stdin, said):
    result = runner.invoke(app, ["voice", "llm", "remote", "http://gpu-box:11434", "--model",
                                 "m", *extra, "--json"], input=stdin)

    [event] = ends_once(result, "error", 2)
    assert said in event["message"] and machine.asked == []


def test_voice_llm_remote_that_does_not_list_the_model_saves_nothing(machine):
    machine.listed = Probe(True, ("gpt-4.1",))

    result = runner.invoke(app, ["voice", "llm", "remote", "https://api.openai.com/v1",
                                 "--model", "gpt-5-mini", "--json"])

    events = ends_once(result, "error", 1)
    assert events[-1]["message"] == "api.openai.com does not list gpt-5-mini."
    assert ConfigStore().stored() == {}


# --- keryx voice llm shared ----------------------------------------------------------------


def test_voice_llm_shared_saves_it_and_removes_the_voice_llms_own_unit(machine):
    ConfigStore().set({**WORDS, "VOICE_LLM": "here", "VOICE_LLM_MODEL": SMALL.key})
    machine.units.add("voice_llm")

    result = runner.invoke(app, ["voice", "llm", "shared", "--json"])

    events = ends_once(result, "done", 0)
    assert [e["step"] for e in events if e["event"] == "step"] == ["configure", "unit"]
    assert events[-1]["source"] == "shared" and events[-1]["model"] == "qwen3-coder-30b-a3b"
    assert machine.flags() == [["--voice-llm", "--uninstall"]]
    assert ConfigStore().stored()["VOICE_LLM"] == "shared"


def test_voice_llm_shared_in_words(machine):
    ConfigStore().set(WORDS)

    result = runner.invoke(app, ["voice", "llm", "shared"])

    assert result.exit_code == 0, result.output
    assert "the voice's words now come from the local agent's model" in result.stdout


def test_voice_llm_shared_with_no_words_for_a_voice_here_is_refused(machine):
    ConfigStore().set({"VOICE_MODE": "here", "VOICE_LLM": "here", "VOICE_LLM_MODEL": "m"})

    result = runner.invoke(app, ["voice", "llm", "shared", "--json"])

    ends_once(result, "error", 2)
    assert ConfigStore().stored()["VOICE_LLM"] == "here"


# --- keryx models start --voice-llm --------------------------------------------------------


def test_models_start_voice_llm_streams_the_voice_llms_start(machine):
    ConfigStore().set({"VOICE_LLM_MODEL": SMALL.key})
    on_disk(Settings(), SMALL)

    result = runner.invoke(app, ["models", "start", "--voice-llm", "--json"])

    events = ends_once(result, "done", 0)
    assert [e["step"] for e in events if e["event"] == "step"] == [
        "runtime", "unit", "start", "check"]
    assert events[-1] == {"event": "done", "url": OWN, "served_model": SMALL.key}
    assert ConfigStore().stored() == {"VOICE_LLM_MODEL": SMALL.key}  # nothing saved


def test_models_start_voice_llm_with_no_model_is_refused(machine):
    result = runner.invoke(app, ["models", "start", "--voice-llm", "--json"])

    [event] = ends_once(result, "error", 2)
    assert event["message"] == "Choose and download the voice LLM's model first."


def test_models_start_voice_llm_on_ollama_is_refused(machine):
    result = runner.invoke(app, ["models", "start", "--voice-llm", "--runtime", "ollama",
                                 "--json"])

    [event] = ends_once(result, "error", 2)
    assert "llama.cpp only" in event["message"] and machine.scripts == []


def test_models_start_voice_llm_in_words(machine):
    ConfigStore().set({"VOICE_LLM_MODEL": SMALL.key})
    on_disk(Settings(), SMALL)

    result = runner.invoke(app, ["models", "start", "--voice-llm"])

    assert result.exit_code == 0, result.output
    assert f"serving {SMALL.key}" in result.stdout


# --- the first keryx voice use here --------------------------------------------------------


@pytest.fixture
def speech_ready(machine, monkeypatch):
    """speech-to-speech installed and its speech models in the cache: the voice's own steps
    are `configure`, `unit` and `start`."""
    monkeypatch.setattr(runtimes, "speech_to_speech", lambda: "/bin/s2s")
    monkeypatch.setattr(speech, "models_ready", lambda settings: True)
    return machine


def test_a_first_voice_here_with_room_brings_up_a_voice_llm_of_its_own_first(speech_ready,
                                                                             monkeypatch):
    """No local agent here and 48 GB for models: 0 + 9 + 3 fits, so the voice gets its own."""
    monkeypatch.setattr(hardware, "detect", lambda: MAC_64)

    result = runner.invoke(app, ["voice", "use", "here", "--json"])

    events = ends_once(result, "done", 0)
    steps = [e["step"] for e in events if e["event"] == "step"]
    assert steps[:6] == ["voice_llm"] * 6  # its download, runtime, unit, start, check, save
    assert steps[6:] == ["configure", "unit", "start"]
    assert speech_ready.pulls == [NINE.key]
    assert speech_ready.flags() == [["--voice-llm"], ["--voice"]]
    stored = ConfigStore().stored()
    assert (stored["VOICE_MODE"], stored["VOICE_LLM"], stored["VOICE_LLM_MODEL"]) == (
        "here", "here", NINE.key)


def test_a_first_voice_here_with_no_room_keeps_the_local_agents_words(speech_ready):
    ConfigStore().set(WORDS)
    speech_ready.serving[WORDS["LOCAL_AGENT_BASE_URL"]] = (True, WORDS["LOCAL_AGENT_MODEL"])

    result = runner.invoke(app, ["voice", "use", "here", "--json"])

    events = ends_once(result, "done", 0)
    assert "voice_llm" not in [e["step"] for e in events if e["event"] == "step"]
    assert speech_ready.pulls == [] and ConfigStore().stored()["VOICE_LLM"] == "shared"


def test_a_first_voice_here_whose_voice_llm_does_not_come_up_installs_no_voice(speech_ready,
                                                                               monkeypatch):
    monkeypatch.setattr(hardware, "detect", lambda: MAC_64)
    speech_ready.wait = "no answer"

    result = runner.invoke(app, ["voice", "use", "here", "--json"])

    ends_once(result, "error", 1)
    assert speech_ready.flags() == [["--voice-llm"], ["--voice-llm", "--uninstall"]]
    assert {"VOICE_MODE", "VOICE_LLM", "VOICE_LLM_MODEL"}.isdisjoint(ConfigStore().stored())


@pytest.mark.parametrize("stored", [
    {"VOICE_LLM": "shared"},  # chosen by the owner: never changed
    {"VOICE_BASE_URL": "http://127.0.0.1:8765/v1"},  # an existing install's voice here
])
def test_a_voice_llm_already_chosen_or_a_voice_already_here_is_never_chosen_again(
    speech_ready, monkeypatch, stored
):
    monkeypatch.setattr(hardware, "detect", lambda: MAC_64)
    ConfigStore().set({**WORDS, **stored})
    speech_ready.serving[WORDS["LOCAL_AGENT_BASE_URL"]] = (True, WORDS["LOCAL_AGENT_MODEL"])

    result = runner.invoke(app, ["voice", "use", "here", "--json"])

    events = ends_once(result, "done", 0)
    assert "voice_llm" not in [e["step"] for e in events if e["event"] == "step"]
    assert speech_ready.pulls == []
    assert ConfigStore().stored().get("VOICE_LLM") == stored.get("VOICE_LLM")


# --- keryx voice: the words line and the llm block -----------------------------------------


@pytest.mark.parametrize(("stored", "line"), [
    ({**WORDS}, "its words: qwen3-coder-30b-a3b, the local agent's model, at "
                "http://127.0.0.1:8090/v1 (answers: yes)"),
    ({"VOICE_LLM": "here", "VOICE_LLM_MODEL": SMALL.key},
     f"its words: {SMALL.key}, a model of its own, on its own server here, at {OWN} "
     "(answers: no)"),
    ({"VOICE_LLM": "remote", "VOICE_LLM_BASE_URL": "https://api.openai.com/v1",
      "VOICE_LLM_MODEL": "gpt-5-mini"},
     "its words: gpt-5-mini, a server elsewhere, at https://api.openai.com/v1 "
     "(answers: not asked)"),
    ({"VOICE_LLM": "here"}, f"its words: {voice.NO_VOICE_LLM} {voice.NO_VOICE_LLM_HINT}"),
    ({}, f"its words: {voice.NO_WORDS} {voice.NO_WORDS_HINT}"),
])
def test_voice_says_where_its_words_come_from(machine, stored, line):
    if stored:
        ConfigStore().set(stored)
    machine.serving[WORDS["LOCAL_AGENT_BASE_URL"]] = (True, WORDS["LOCAL_AGENT_MODEL"])

    result = runner.invoke(app, ["voice"])

    assert result.exit_code == 0, result.output
    assert line in result.stdout


def test_voice_json_carries_the_voice_llm_and_the_pipelines(machine):
    ConfigStore().set({"VOICE_LLM": "here", "VOICE_LLM_MODEL": SMALL.key})
    machine.units.add("voice_llm")
    machine.serving[OWN] = (True, SMALL.key)

    document = json.loads(runner.invoke(app, ["voice", "--json"]).stdout)

    assert document["llm"] == {"source": "here", "model": SMALL.key, "url": OWN,
                               "reachable": True, "unit_installed": True}
    assert document["pipelines"] == {"openai": ["speech-to-speech"], "here": ["three-model"],
                                     "server": ["speech-to-speech"]}
    assert document["needs"] is None and document["here"]["words_model"] == SMALL.key


def test_voice_use_openai_takes_a_pipeline_it_does_not_offer_away(machine):
    ConfigStore().set({"VOICE_MODE": "here", "VOICE_PIPELINE": "three-model"})

    result = runner.invoke(app, ["voice", "use", "openai", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["saved"]["VOICE_PIPELINE"] is None
    assert ConfigStore().stored() == {"VOICE_MODE": "openai"}


def test_voice_llm_remote_never_says_a_key_the_server_echoes(machine):
    machine.listed = Probe(True, ("vk-echoed",))

    result = runner.invoke(app, ["voice", "llm", "remote", "https://llm.example.com",
                                 "--model", "small", "--key-stdin", "--json"],
                           input="vk-echoed\n")

    ends_once(result, "error", 1)
    assert "vk-echoed" not in result.output


def test_voice_llm_shared_puts_the_old_words_back_when_the_voice_does_not_answer(machine,
                                                                                 monkeypatch):
    ConfigStore().set({**WORDS, "VOICE_MODE": "here", "VOICE_LLM": "here",
                       "VOICE_LLM_MODEL": SMALL.key})
    machine.units.update({"voice", "voice_llm"})
    answers = ["no Realtime socket", None]
    monkeypatch.setattr(servers, "wait_for", lambda endpoint, kind, timeout: answers.pop(0))

    result = runner.invoke(app, ["voice", "llm", "shared", "--json"])

    events = ends_once(result, "error", 1)
    assert events[-1]["message"].endswith("it answers on the ones it had.")
    assert ConfigStore().stored()["VOICE_LLM"] == "here"
    assert machine.flags() == [["--voice"], ["--voice"]] and "voice_llm" in machine.units


@pytest.mark.parametrize("command", [
    ["voice", "llm", "here", SMALL.key, "--json"], ["voice", "llm", "shared", "--json"],
    ["voice", "llm", "remote", "https://api.openai.com/v1", "--model", "gpt-5-mini", "--json"],
])
def test_voice_llm_is_refused_while_a_call_is_live_on_the_voice_here(machine, command):
    from keryx.localmodels import voice_llm

    ConfigStore().set({**WORDS, "VOICE_MODE": "here"})
    before = ConfigStore().stored()
    machine.units.add("voice")
    machine.live = 1

    result = runner.invoke(app, command)

    [event] = ends_once(result, "error", 1)
    assert (event["message"], event["detail"]) == (voice_llm.LIVE_CALL, "1 live call(s)")
    assert machine.scripts == [] and machine.asked == [] and machine.pulls == []
    assert ConfigStore().stored() == before


# --- what the Mac app is told is what `voice use here` brings up -----------------------------

AGENT_HERE = {"AGENT_SERVER_MODEL": "qwen3-coder-30b-a3b", **WORDS}


@pytest.mark.parametrize(("machine_", "agent", "brought_up"), [
    (Hardware("cuda", "RTX 5090", 31.8, 62.0), {}, NINE.key),       # the agent elsewhere
    (Hardware("metal", "Apple silicon", 0.0, 48.0), {}, NINE.key),
    (Hardware("metal", "Apple silicon", 0.0, 48.0), AGENT_HERE, SMALL.key),  # 25 + 6 + 3
    (Hardware("metal", "Apple silicon", 0.0, 48.0),
     {**AGENT_HERE, "AGENT_SERVER_MODEL": "gpt-oss-20b"}, NINE.key),  # 16 + 9 + 3
    (Hardware("metal", "Apple silicon", 0.0, 36.0), AGENT_HERE, None),   # 34 > 27: shared
])
def test_models_list_recommends_exactly_the_voice_llm_voice_use_here_brings_up(
    speech_ready, monkeypatch, machine_, agent, brought_up
):
    monkeypatch.setattr(hardware, "detect", lambda: machine_)
    if agent:
        ConfigStore().set(agent)
    speech_ready.serving[WORDS["LOCAL_AGENT_BASE_URL"]] = (True, WORDS["LOCAL_AGENT_MODEL"])

    told = json.loads(runner.invoke(app, ["models", "list", "--json"]).stdout)
    ends_once(runner.invoke(app, ["voice", "use", "here", "--json"]), "done", 0)

    stored = ConfigStore().stored()
    assert told["voice_llm_recommended"] == stored.get("VOICE_LLM_MODEL") == brought_up
    assert told["voice_llm_separate"] is (stored["VOICE_LLM"] == "here")
    if brought_up is not None:
        # What it recommends is also what the voice-model list itself says fits beside the
        # agent (use.fits_as_voice_llm), so the two can never disagree.
        row = next(row for row in told["models"] if row["name"] == brought_up)
        assert row["fits_as_voice_llm"] is True
    # And the agent's model it recommends beside that fits with it and the speech models.
    pair = by_key(told["recommended_with_local_voice"])
    voice_gb = by_key(brought_up).memory_gb if brought_up else 0.0
    assert pair.memory_gb + voice_gb + 3 <= machine_.model_memory_gb
    # Once chosen, it is what the app is told.
    after = json.loads(runner.invoke(app, ["models", "list", "--json"]).stdout)
    assert after["voice_llm_recommended"] == brought_up
