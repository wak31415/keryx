"""`keryx voice`: where the voice is, and setting it up from the command line (the Mac app)."""

import json
import os
import signal

import pytest
from typer.testing import CliRunner

from keryx.cli import app
from keryx.config.store import ConfigStore
from keryx.localmodels import runtimes, servers, use, voice

runner = CliRunner()
WORDS = {"LOCAL_AGENT_BASE_URL": "http://127.0.0.1:8090/v1",
         "LOCAL_AGENT_MODEL": "qwen3-coder-30b-a3b"}
TERMINAL = {"done", "error", "cancelled"}


class Machine:
    """The machine `keryx voice` sees: its speech-to-speech, its units, its servers."""

    def __init__(self) -> None:
        self.s2s: str | None = None
        self.unit = False
        self.up = False
        self.server = True
        self.script_code = 0
        self.wait_problem: str | None = None
        self.realtime: str | None = None
        self.scripts: list[list[str]] = []
        self.realtimes: list = []
        self.servers: list = []

    def run(self, argv):
        self.scripts.append(list(argv))
        return self.script_code


def checkout(root) -> None:
    """The installers beside the code (`keryx.cli.repo_root`, the suite's own directory)."""
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    for name in ("install-launchd.sh", "install-systemd.sh"):
        (root / "scripts" / name).write_text("#!/bin/sh\n")


@pytest.fixture
def machine(monkeypatch, tmp_path, every_agent_installed):
    found = Machine()
    checkout(tmp_path / "checkout")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(runtimes, "speech_to_speech", lambda: found.s2s)
    monkeypatch.setattr(use, "unit_installed", lambda kind: found.unit)
    monkeypatch.setattr(use, "probe", lambda url: (found.up, None))
    monkeypatch.setattr(voice, "server_reachable",
                        lambda endpoint: found.servers.append(endpoint) or found.server)
    monkeypatch.setattr(voice, "run_detached", found.run)
    monkeypatch.setattr(servers, "free_port", lambda port: port)
    monkeypatch.setattr(servers, "wait_for",
                        lambda endpoint, kind, timeout: found.wait_problem)
    monkeypatch.setattr("keryx.realtime.openai.realtime_problem",
                        lambda endpoint: found.realtimes.append(endpoint) or found.realtime)
    monkeypatch.setattr("keryx.localmodels.hardware.free_bytes", lambda path: 10**12)
    return found


def events_of(result) -> list[dict]:
    return [json.loads(line) for line in result.stdout.splitlines()]


# --- keryx voice ---------------------------------------------------------------------------


def test_voice_json_on_openai_needs_a_local_model_for_here(machine):
    result = runner.invoke(app, ["voice", "--json"])

    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["mode"] == "openai" and document["base_url"] is None
    assert document["openai_key_set"] is True and document["needs"] == "local_agent_model"
    assert document["here"]["words_model"] is None
    assert document["server"] == {"reachable": None} and machine.servers == []


def test_voice_json_here(machine):
    ConfigStore().set({**WORDS, "VOICE_BASE_URL": "http://127.0.0.1:8765/v1",
                       "VOICE_SERVER_VOICE": "bf_emma"})
    machine.s2s, machine.unit, machine.up = "/bin/s2s", True, True

    document = json.loads(runner.invoke(app, ["voice", "--json"]).stdout)

    assert document["mode"] == "here" and document["voice"] == "bf_emma"
    assert document["needs"] is None
    assert document["here"] == {"installed": True, "program": "/bin/s2s", "voice": "bf_emma",
                                "port": 8765, "unit_installed": True, "reachable": True,
                                "words_model": "qwen3-coder-30b-a3b"}


def test_voice_json_server_says_whether_it_answers_and_never_its_key(machine):
    ConfigStore().set({"VOICE_BASE_URL": "http://gpu-box:8765/v1", "VOICE_API_KEY": "vk-secret"})
    machine.server = False

    result = runner.invoke(app, ["voice", "--json"])

    document = json.loads(result.stdout)
    assert document["mode"] == "server" and document["server"] == {"reachable": False}
    assert [endpoint.api_key for endpoint in machine.servers] == ["vk-secret"]
    assert "vk-secret" not in result.output


@pytest.mark.parametrize(
    ("stored", "said"),
    [({"VOICE_BASE_URL": "http://gpu-box:8765/v1"},
      "the voice: a voice server elsewhere: http://gpu-box:8765/v1 (answers: yes)"),
     ({**WORDS, "VOICE_BASE_URL": "http://127.0.0.1:8765/v1"},
      "the voice: speech-to-speech on this machine: http://127.0.0.1:8765/v1"),
     ({}, "the voice: OpenAI's Realtime API")],
)
def test_voice_without_json_says_it_in_lines(machine, stored, said):
    if stored:
        ConfigStore().set(stored)

    result = runner.invoke(app, ["voice"])

    assert result.exit_code == 0
    assert said in result.stdout
    assert "speech-to-speech here: not installed; voice af_heart on port 8765" in result.stdout
    words = "qwen3-coder-30b-a3b" if "LOCAL_AGENT_MODEL" in stored else voice.NO_WORDS
    assert f"its words: {words}" in result.stdout


def test_voice_on_openai_without_a_key_says_so(machine, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY")

    assert "OPENAI_API_KEY is not set" in runner.invoke(app, ["voice"]).stdout


# --- keryx voice use openai ----------------------------------------------------------------


def test_use_openai_forgets_the_voice_server(machine):
    ConfigStore().set({"VOICE_BASE_URL": "http://gpu-box:8765/v1", "VOICE_API_KEY": "vk"})

    result = runner.invoke(app, ["voice", "use", "openai", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "saved": {"VOICE_BASE_URL": None, "VOICE_API_KEY": None}, "restart_needed": True,
    }
    assert ConfigStore().stored() == {}


def test_use_openai_in_words_says_what_it_needs(machine, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY")

    result = runner.invoke(app, ["voice", "use", "openai"])

    assert result.exit_code == 0
    assert "calls use OpenAI's Realtime API again" in result.stdout
    assert "OPENAI_API_KEY" in result.stdout and "keryx restart" in result.stdout


# --- keryx voice use server ----------------------------------------------------------------


def test_use_server_that_does_not_answer_saves_nothing(machine):
    machine.realtime = "could not open gpu-box's Realtime socket"

    result = runner.invoke(app, ["voice", "use", "server", "https://gpu-box:8765", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "saved": None, "problem": "could not open gpu-box's Realtime socket", "warnings": [],
        "restart_needed": False,
    }
    assert ConfigStore().stored() == {}


def test_use_server_kept_anyway_with_its_key_from_stdin(machine):
    machine.realtime = "could not open voice.example.com's Realtime socket"

    result = runner.invoke(
        app, ["voice", "use", "server", "https://voice.example.com", "--key-stdin",
              "--keep-anyway", "--json"], input="vk-secret\n",
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "saved": {"VOICE_BASE_URL": "https://voice.example.com/v1", "VOICE_API_KEY": "(set)"},
        "problem": "could not open voice.example.com's Realtime socket", "warnings": [],
        "restart_needed": True,
    }
    assert "vk-secret" not in result.output
    assert ConfigStore()._secrets()["VOICE_API_KEY"] == "vk-secret"
    assert [endpoint.api_key for endpoint in machine.realtimes] == ["vk-secret"]


def test_use_server_that_answers_is_saved_with_its_warnings(machine):
    result = runner.invoke(app, ["voice", "use", "server", "http://192.168.1.20:8765", "--json"])

    assert result.exit_code == 0
    document = json.loads(result.stdout)
    assert document["saved"] == {"VOICE_BASE_URL": "http://192.168.1.20:8765/v1"}
    assert document["problem"] is None and document["restart_needed"] is True
    [warning] = document["warnings"]
    assert "spoken PIN" in warning
    assert ConfigStore().stored() == {"VOICE_BASE_URL": "http://192.168.1.20:8765/v1"}


def test_use_server_keeps_the_stored_key_and_asks_with_it(machine):
    """Re-checking or moving the address must not lose a key, nor ask without it."""
    ConfigStore().set({"VOICE_BASE_URL": "https://old.example.com/v1",
                       "VOICE_API_KEY": "vk-stored"})

    result = runner.invoke(app, ["voice", "use", "server", "https://voice.example.com", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["saved"] == {"VOICE_BASE_URL": "https://voice.example.com/v1"}
    assert [endpoint.api_key for endpoint in machine.realtimes] == ["vk-stored"]
    assert ConfigStore()._secrets()["VOICE_API_KEY"] == "vk-stored"
    assert "vk-stored" not in result.output


def test_use_server_replaces_the_key_from_stdin(machine):
    ConfigStore().set({"VOICE_API_KEY": "vk-old"})

    result = runner.invoke(app, ["voice", "use", "server", "https://voice.example.com",
                                 "--key-stdin", "--json"], input="vk-new\n")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["saved"]["VOICE_API_KEY"] == "(set)"
    assert ConfigStore()._secrets()["VOICE_API_KEY"] == "vk-new"
    assert [endpoint.api_key for endpoint in machine.realtimes] == ["vk-new"]
    assert "vk-new" not in result.output and "vk-old" not in result.output


def test_use_server_wants_a_key_on_stdin_when_it_says_so(machine):
    ConfigStore().set({"VOICE_API_KEY": "vk-old"})

    result = runner.invoke(app, ["voice", "use", "server", "https://voice.example.com",
                                 "--key-stdin", "--json"], input="\n")

    assert result.exit_code == 2 and "no key on stdin" in result.stderr
    assert result.stdout == "" and machine.realtimes == []
    assert ConfigStore()._secrets()["VOICE_API_KEY"] == "vk-old"


def test_use_server_clear_key_forgets_it_and_asks_without_one(machine):
    ConfigStore().set({"VOICE_API_KEY": "vk-old"})

    result = runner.invoke(app, ["voice", "use", "server", "http://192.168.1.20:8765",
                                 "--clear-key", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["saved"] == {
        "VOICE_BASE_URL": "http://192.168.1.20:8765/v1", "VOICE_API_KEY": None,
    }
    assert "VOICE_API_KEY" not in ConfigStore()._secrets()
    assert [endpoint.api_key for endpoint in machine.realtimes] == [None]


def test_use_server_takes_a_key_or_clears_it_never_both(machine):
    result = runner.invoke(app, ["voice", "use", "server", "http://gpu-box:8765", "--key-stdin",
                                 "--clear-key", "--json"], input="vk\n")

    assert result.exit_code == 2 and "--clear-key" in result.stderr
    assert result.stdout == "" and machine.realtimes == []


def test_use_server_in_words(machine):
    machine.realtime = "no answer"

    refused = runner.invoke(app, ["voice", "use", "server", "http://gpu-box:8765"])
    kept = runner.invoke(app, ["voice", "use", "server", "http://gpu-box:8765", "--keep-anyway"])

    assert refused.exit_code == 1 and "no answer" in refused.stderr
    assert "--keep-anyway" in refused.stderr
    assert kept.exit_code == 0 and "VOICE_BASE_URL saved" in kept.stdout


def test_use_server_refuses_an_address_with_a_password_in_it(machine):
    result = runner.invoke(app, ["voice", "use", "server", "http://me:pw@gpu-box:8765", "--json"])

    assert result.exit_code == 2
    assert "user or password" in result.stderr and "pw@" not in result.stderr
    assert result.stdout == "" and ConfigStore().stored() == {}
    assert machine.realtimes == []


# --- keryx voice use here ------------------------------------------------------------------


def test_use_here_streams_its_steps_then_done(machine):
    ConfigStore().set(WORDS)

    result = runner.invoke(app, ["voice", "use", "here", "--voice", "bm_george", "--json"])

    assert result.exit_code == 0, result.output
    events = events_of(result)
    assert [e["step"] for e in events if e["event"] == "step"] == [
        "install", "configure", "unit", "start",
    ]
    assert all(e["message"] for e in events if e["event"] == "step")
    assert events[-1] == {"event": "done", "base_url": "http://127.0.0.1:8765/v1"}
    assert machine.scripts[0] == runtimes.s2s_install_argv()
    assert machine.scripts[1][1:] == ["--voice"]
    settings = ConfigStore().stored()
    assert settings["VOICE_SERVER_VOICE"] == "bm_george"
    assert settings["VOICE_BASE_URL"] == "http://127.0.0.1:8765/v1"


def test_use_here_without_a_voice_takes_the_personas_and_warns_of_an_unknown_one(machine):
    ConfigStore().set({**WORDS, "ASSISTANT_NAME": "Jarvis"})
    machine.s2s = "/bin/s2s"

    runner.invoke(app, ["voice", "use", "here", "--json"])
    assert ConfigStore().stored()["VOICE_SERVER_VOICE"] == "bm_george"

    result = runner.invoke(app, ["voice", "use", "here", "--voice", "zf_xiaoxiao", "--json"])
    assert result.exit_code == 0
    warned = [e for e in events_of(result) if "zf_xiaoxiao" in e.get("message", "")]
    assert warned and warned[0]["event"] == "step" and warned[0]["step"] == "configure"


def test_use_here_in_words(machine):
    ConfigStore().set(WORDS)
    machine.s2s = "/bin/s2s"

    result = runner.invoke(app, ["voice", "use", "here"])

    assert result.exit_code == 0, result.output
    assert "calls now use the voice server on this machine" in result.stdout
    assert "keryx restart" in result.stdout


def test_use_here_with_no_checkout_says_to_run_it_from_a_clone(machine, monkeypatch, tmp_path):
    ConfigStore().set(WORDS)
    machine.s2s = "/bin/s2s"
    monkeypatch.setattr("keryx.cli.repo_root", lambda: tmp_path / "not-a-checkout")

    result = runner.invoke(app, ["voice", "use", "here", "--json"])

    assert result.exit_code == 2
    assert events_of(result) == [{"event": "error", "message": "scripts/install-launchd.sh is "
                                  "not here: run it with --voice from a clone of Keryx"}]


def test_use_here_that_does_not_come_up_says_where_its_log_is(machine):
    ConfigStore().set(WORDS)
    machine.s2s, machine.wait_problem = "/bin/s2s", "no Realtime socket"

    plain = runner.invoke(app, ["voice", "use", "here"])
    result = runner.invoke(app, ["voice", "use", "here", "--json"])

    assert result.exit_code == plain.exit_code == 1
    message = events_of(result)[-1]["message"]
    assert "no Realtime socket" in message and "keryx-voice.err.log" in message
    assert "keryx-voice.err.log" in plain.stderr
    assert "VOICE_BASE_URL" not in ConfigStore().stored()


def test_use_here_in_words_refuses_without_a_local_model(machine):
    result = runner.invoke(app, ["voice", "use", "here"])

    assert result.exit_code == 2
    assert result.stderr.strip() == voice.NO_WORDS and machine.scripts == []


def test_use_here_in_words_is_cancelled_by_ctrl_c(machine, monkeypatch):
    ConfigStore().set(WORDS)

    def interrupted(argv):
        raise KeyboardInterrupt

    monkeypatch.setattr(voice, "run_detached", interrupted)

    result = runner.invoke(app, ["voice", "use", "here"])

    assert result.exit_code == 130 and "picks up where this stopped" in result.stderr


def test_use_here_as_the_service_is_refused_and_saves_no_voice(machine, monkeypatch):
    """A subagent of the running service cannot point the voice anywhere."""
    ConfigStore().set(WORDS)
    machine.s2s = "/bin/s2s"
    monkeypatch.setenv("KERYX_ACTOR", "service")

    result = runner.invoke(app, ["voice", "use", "here", "--json"])

    assert result.exit_code == 1
    assert events_of(result)[-1]["event"] == "error"
    assert "VOICE_BASE_URL" not in ConfigStore().stored()


def test_use_here_is_cancelled_by_a_terminate(machine, monkeypatch):
    """What the Mac app's Cancel sends; the half-done install is the next run's to finish."""
    ConfigStore().set(WORDS)
    before = signal.getsignal(signal.SIGTERM)

    def terminated(argv):
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("the terminate should have stopped it")

    monkeypatch.setattr(voice, "run_detached", terminated)

    result = runner.invoke(app, ["voice", "use", "here", "--json"])

    assert result.exit_code == 130
    assert events_of(result)[-1] == {"event": "cancelled"}
    assert signal.getsignal(signal.SIGTERM) is before


def _done(monkeypatch, machine, tmp_path):
    ConfigStore().set(WORDS)


def _no_checkout(monkeypatch, machine, tmp_path):
    ConfigStore().set(WORDS)
    monkeypatch.setattr("keryx.cli.repo_root", lambda: tmp_path / "not-a-checkout")


def _unsupported(monkeypatch, machine, tmp_path):
    ConfigStore().set(WORDS)
    monkeypatch.setattr(voice.sys, "platform", "freebsd14")


def _bad_voice(monkeypatch, machine, tmp_path):
    ConfigStore().set(WORDS)
    return ["--voice", "af heart"]


def _no_words(monkeypatch, machine, tmp_path):
    pass


def _install_failed(monkeypatch, machine, tmp_path):
    ConfigStore().set(WORDS)
    machine.script_code = 1


def _too_little_disk(monkeypatch, machine, tmp_path):
    ConfigStore().set(WORDS)
    monkeypatch.setattr("keryx.localmodels.hardware.free_bytes", lambda path: 10**9)


def _unit_failed(monkeypatch, machine, tmp_path):
    ConfigStore().set(WORDS)
    machine.s2s, machine.script_code = "/bin/s2s", 4


def _never_up(monkeypatch, machine, tmp_path):
    ConfigStore().set(WORDS)
    machine.s2s, machine.wait_problem = "/bin/s2s", "no answer"


def _migration(monkeypatch, machine, tmp_path):
    ConfigStore().set(WORDS)
    old = tmp_path / "home" / ".local" / "share" / "jarvis"
    old.mkdir(parents=True)
    (old / "memory.md").write_text("# Memory\n")


def _cancelled(monkeypatch, machine, tmp_path):
    ConfigStore().set(WORDS)

    def interrupted(argv):
        raise KeyboardInterrupt

    monkeypatch.setattr(voice, "run_detached", interrupted)


def _unexpected(monkeypatch, machine, tmp_path):
    ConfigStore().set(WORDS)

    def broken(argv):
        raise PermissionError("uv is not executable")

    monkeypatch.setattr(voice, "run_detached", broken)


@pytest.mark.parametrize(
    ("arrange", "ending", "code", "message"),
    [(_done, "done", 0, None), (_no_words, "error", 2, voice.NO_WORDS),
     (_no_checkout, "error", 2, "from a clone of Keryx"),
     (_unsupported, "error", 2, "launchd or systemd"),
     (_bad_voice, "error", 2, "is not a Kokoro voice's name"),
     (_install_failed, "error", 1, "the install did not finish"),
     (_too_little_disk, "error", 1, "needs about 5 GB free"),
     (_unit_failed, "error", 1, "--voice exited with 4"),
     (_never_up, "error", 1, "did not come up"),
     (_migration, "error", 2, "keryx cannot do that yet"),
     (_cancelled, "cancelled", 130, None),
     (_unexpected, "error", 1, "uv is not executable")],
)
def test_use_here_json_ends_with_exactly_one_ending(
    machine, monkeypatch, tmp_path, arrange, ending, code, message
):
    extra = arrange(monkeypatch, machine, tmp_path) or []

    result = runner.invoke(app, ["voice", "use", "here", "--json", *extra])

    events = events_of(result)
    assert [e["event"] for e in events if e["event"] in TERMINAL] == [ending]
    assert events[-1]["event"] == ending
    assert all(e["event"] == "step" for e in events[:-1])
    if message is not None:
        assert message in events[-1]["message"]
    assert result.exit_code == code
    assert result.stderr == ""
    if code == 2:  # a refusal: nothing was tried
        assert machine.scripts == [] and "VOICE_SERVER_VOICE" not in ConfigStore().stored()
