"""The voice, set up and asked about: the one implementation `keryx setup` and `keryx voice`
share (`keryx.localmodels.voice`), driven here through fake probes and a recording report."""

import contextlib
from pathlib import Path

import httpx
import pytest

from keryx.config import Settings
from keryx.config.store import ConfigStore
from keryx.endpoints import Endpoint
from keryx.localmodels import voice
from keryx.localmodels.runtimes import s2s_install_argv

WORDS = {"local_agent_base_url": "http://127.0.0.1:8090/v1",
         "local_agent_model": "qwen3-coder-30b-a3b"}


class Log:
    """What the probes were asked and what the report was told, in one order."""

    def __init__(self) -> None:
        self.lines: list[tuple] = []

    def kinds(self) -> list[str]:
        return [line[0] for line in self.lines]

    def of(self, kind: str) -> list[tuple]:
        return [line[1:] for line in self.lines if line[0] == kind]


class FakeMachine:
    def __init__(self, log: Log, *, s2s=None, free=500 * 10**9, script_code=0,
                 ports=None, server_problem=None, realtime=None, words=True) -> None:
        self.log, self.s2s, self.free, self.script_code = log, s2s, free, script_code
        self.ports, self.server_problem, self.realtime = ports or {}, server_problem, realtime
        self.words = words

    def speech_to_speech(self):
        return self.s2s

    def free_bytes(self, path: Path) -> int:
        return self.free

    def run_script(self, argv) -> int:
        self.log.lines.append(("script", list(argv)))
        return self.script_code

    def free_port(self, port: int) -> int:
        self.log.lines.append(("free_port", port))
        return self.ports.get(port, port)

    def wait_for_server(self, endpoint: Endpoint, kind: str, timeout: float):
        self.log.lines.append(("wait", kind, endpoint.base_url, timeout))
        if isinstance(self.server_problem, BaseException):
            raise self.server_problem
        return self.server_problem

    def words_up(self, endpoint: Endpoint | None) -> bool:
        self.log.lines.append(("words", endpoint.base_url if endpoint else None))
        return self.words

    #: speech-to-speech's own Python, and what fetching the speech models says.
    python: str | None = "/tools/s2s/bin/python"
    fetched: str | None = None

    def speech_python(self) -> str | None:
        return self.python

    def fetch_speech(self, python, repos, progress) -> str | None:
        self.log.lines.append(("fetch", python, [repo.id for repo in repos]))
        progress(0, 100)
        progress(100, 100)
        return self.fetched

    def realtime_problem(self, endpoint: Endpoint):
        self.log.lines.append(("realtime", endpoint.base_url, endpoint.api_key))
        return self.realtime


class Recorder:
    def __init__(self, log: Log) -> None:
        self.log = log

    @contextlib.contextmanager
    def step(self, step: str, message: str):
        self.log.lines.append(("step", step, message))
        yield

    def warn(self, message: str) -> None:
        self.log.lines.append(("warn", message))

    def error(self, message: str, *, log: Path | None = None) -> None:
        self.log.lines.append(("error", message, log))

    def done(self, *ended: str) -> None:
        self.log.lines.append(("done", *ended))

    def progress(self, done: int, total: int | None) -> None:
        self.log.lines.append(("progress", done, total))


@pytest.fixture
def root(tmp_path) -> Path:
    """A checkout with both installers in it."""
    scripts = tmp_path / "checkout" / "scripts"
    scripts.mkdir(parents=True)
    for name in ("install-launchd.sh", "install-systemd.sh"):
        (scripts / name).write_text("#!/bin/sh\n")
    return tmp_path / "checkout"


def run_here(settings, machine, log, root, *, chosen="af_heart", save_ok=True,
             platform="linux", **kwargs):
    saved: list[dict] = []

    def save(values):
        log.lines.append(("save", dict(values)))
        saved.append(dict(values))
        return save_ok

    code = voice.here(settings, machine, Recorder(log), choose_voice=lambda: chosen, save=save,
                      root=root, platform=platform, **kwargs)
    return code, saved


# --- here ----------------------------------------------------------------------------------


def test_here_installs_saves_installs_the_unit_starts_and_only_then_is_the_voice(
    settings, root
):
    log = Log()
    machine = FakeMachine(log)
    words = settings.model_copy(update=WORDS)

    code, saved = run_here(words, machine, log, root)

    assert code == 0
    steps = [step for step, _message in log.of("step")]
    assert steps == ["install", "configure", "unit", "start"]
    # Each step is told before the slow work it names begins.
    kinds = log.kinds()
    assert kinds.index("step") < kinds.index("script")
    install, unit = log.of("script")
    assert install == (s2s_install_argv(),)
    assert unit == ([str(root / "scripts" / "install-systemd.sh"), "--voice"],)
    start = next(i for i, line in enumerate(log.lines) if line[:2] == ("step", "start"))
    assert log.lines[start + 1][0] == "wait"
    assert log.of("wait") == [("realtime", "http://127.0.0.1:8765/v1", voice.VOICE_START_S)]
    assert saved == [
        {"VOICE_SERVER_VOICE": "af_heart", "VOICE_SERVER_PORT": 8765},
        {"VOICE_BASE_URL": "http://127.0.0.1:8765/v1", "VOICE_API_KEY": None},
    ]
    assert log.of("done") == [("http://127.0.0.1:8765/v1",)]
    assert log.of("error") == [] and log.of("warn") == []


def test_here_already_installed_skips_the_install(settings, root):
    log = Log()
    code, _saved = run_here(settings.model_copy(update=WORDS), FakeMachine(log, s2s="/bin/s2s"),
                            log, root)

    assert code == 0
    assert "install" not in [step for step, _ in log.of("step")]
    assert [argv[0][1:] for argv in log.of("script")] == [["--voice"]]


def test_here_wants_a_local_model_for_its_words(settings, root):
    log = Log()

    code, saved = run_here(settings, FakeMachine(log), log, root)

    assert code == voice.REFUSED == 2
    assert log.lines == [("error", voice.NO_WORDS, None)] and saved == []
    assert voice.NO_WORDS == "set up a local model first: keryx models use NAME"


def test_here_says_no_words_in_the_callers_own_sentence(settings, root):
    log = Log()

    run_here(settings, FakeMachine(log), log, root, no_words="choose one first")

    assert log.of("error") == [("choose one first", None)]


def test_here_with_too_little_disk_installs_nothing(settings, root):
    log = Log()

    code, saved = run_here(settings.model_copy(update=WORDS), FakeMachine(log, free=10**9),
                           log, root)

    assert code == 1 and saved == [] and log.of("script") == []
    [(message, _log)] = log.of("error")
    assert "needs about 5 GB free" in message and "there is 1.0 GB" in message


def test_an_install_that_fails_saves_nothing(settings, root):
    log = Log()

    code, saved = run_here(settings.model_copy(update=WORDS), FakeMachine(log, script_code=1),
                           log, root)

    assert code == 1 and saved == []
    assert log.of("error") == [("the install did not finish", None)]


def test_no_installer_beside_the_code_refuses_before_anything_is_installed(settings, tmp_path):
    log = Log()

    code, saved = run_here(settings.model_copy(update=WORDS), FakeMachine(log), log,
                           tmp_path / "not-a-checkout")

    assert code == voice.REFUSED
    [(message, _log)] = log.of("error")
    assert message.startswith("scripts/install-systemd.sh is not here")
    assert "from a clone of Keryx" in message
    assert saved == [] and log.of("script") == [] and log.of("step") == []


def test_a_machine_with_neither_launchd_nor_systemd_is_refused(settings, root):
    log = Log()

    code, saved = run_here(settings.model_copy(update=WORDS), FakeMachine(log), log, root,
                           platform="freebsd14")

    assert code == voice.REFUSED and saved == [] and log.of("script") == []
    assert log.of("error") == [("Keryx runs its servers under launchd or systemd, and "
                                "freebsd14 has neither", None)]
    assert voice.install_unit("--llm", FakeMachine(log).run_script, root=root,
                              platform="freebsd14").startswith("Keryx runs its servers")


@pytest.mark.parametrize(("name", "problem"), [
    ("af_heart", False), ("zf_xiaoxiao", False), ("af_new2", False),
    ("", True), ("heart", True), ("af heart", True), ("--af_heart", True), ("AF_HEART", True),
])
def test_a_kokoro_voice_name_has_its_shape(name, problem):
    assert (voice.voice_name_problem(name) is not None) is problem


def test_a_unit_that_will_not_install_is_said_and_no_voice_is_saved(settings, root):
    log = Log()

    code, saved = run_here(settings.model_copy(update=WORDS),
                           FakeMachine(log, s2s="/bin/s2s", script_code=3), log, root,
                           platform="darwin")

    assert code == 1
    assert log.of("error") == [("install-launchd.sh --voice exited with 3", None)]
    assert log.of("script")[0][0][0].endswith("install-launchd.sh")
    assert [values for values in saved if "VOICE_BASE_URL" in values] == []


def test_a_server_that_never_answers_points_at_its_log(settings, root):
    log = Log()
    problem = "could not open 127.0.0.1's Realtime socket"

    code, saved = run_here(settings.model_copy(update=WORDS),
                           FakeMachine(log, s2s="/bin/s2s", server_problem=problem), log, root)

    assert code == 1
    assert log.of("error") == [(f"the voice server did not come up: {problem}; {voice.REMOVED}",
                                settings.state_dir / "logs" / "keryx-voice.err.log")]
    assert [values for values in saved if "VOICE_BASE_URL" in values] == []
    assert log.of("done") == []


def test_a_server_that_never_answers_has_its_unit_removed_before_the_error(settings, root):
    """No unit is left behind for launchd's KeepAlive to restart in a loop."""
    log = Log()
    script = str(root / "scripts" / "install-launchd.sh")

    code, _saved = run_here(settings.model_copy(update=WORDS),
                            FakeMachine(log, s2s="/bin/s2s", server_problem="no answer"),
                            log, root, platform="darwin")

    assert code == 1
    assert [argv for (argv,) in log.of("script")] == [
        [script, "--voice"], [script, "--voice", "--uninstall"]]
    assert log.kinds().index("error") > max(
        i for i, kind in enumerate(log.kinds()) if kind == "script")


def test_a_unit_that_cannot_be_removed_says_so(settings, root):
    log = Log()
    machine = FakeMachine(log, s2s="/bin/s2s", server_problem="no answer")
    machine.run_script = lambda argv: (log.lines.append(("script", list(argv)))
                                       or (4 if argv[-1] == "--uninstall" else 0))

    run_here(settings.model_copy(update=WORDS), machine, log, root, platform="darwin")

    [(message, _log)] = log.of("error")
    assert message.endswith("its service could not be removed: install-launchd.sh --voice "
                            "--uninstall exited with 4")


@pytest.mark.parametrize("removal", [0, 4, OSError("gone")])
def test_a_cancel_while_the_server_starts_removes_its_unit_and_stays_a_cancel(
    settings, root, removal
):
    log = Log()
    machine = FakeMachine(log, s2s="/bin/s2s", server_problem=KeyboardInterrupt())
    script = str(root / "scripts" / "install-launchd.sh")

    def run(argv):
        log.lines.append(("script", list(argv)))
        if argv[-1] == "--uninstall" and isinstance(removal, BaseException):
            raise removal
        return removal if argv[-1] == "--uninstall" else 0

    machine.run_script = run

    with pytest.raises(KeyboardInterrupt):
        run_here(settings.model_copy(update=WORDS), machine, log, root, platform="darwin")

    assert [argv for (argv,) in log.of("script")] == [
        [script, "--voice"], [script, "--voice", "--uninstall"]]
    assert log.of("error") == [] and log.of("done") == []  # the cancel is the caller's to tell


def test_here_refuses_while_its_words_server_does_not_answer(settings, root):
    """The crash loop of 2026-10-07: a voice server installed while nothing served its
    words. Refused before anything is installed, saved or started."""
    log = Log()

    code, saved = run_here(settings.model_copy(update=WORDS), FakeMachine(log, words=False),
                           log, root)

    assert code == voice.REFUSED
    assert voice.NO_WORDS_SERVER == "The local model server isn't running: start it first."
    assert log.lines == [("words", "http://127.0.0.1:8090/v1"),
                         ("error", voice.NO_WORDS_SERVER, None)]
    assert saved == []


def test_the_words_server_is_asked_on_loopback_without_a_proxy_and_elsewhere_with_its_key(
    monkeypatch
):
    from keryx.localmodels import use

    asked: list = []
    monkeypatch.setattr(use, "probe", lambda url: asked.append(("probe", url)) or (True, "m"))
    monkeypatch.setattr(voice, "server_reachable",
                        lambda endpoint: asked.append(("server", endpoint.api_key)) or False)

    assert voice.words_up(Endpoint.parse("http://127.0.0.1:8090/v1")) is True
    assert voice.words_up(Endpoint.parse("http://gpu-box:11434/v1", api_key="lk")) is False
    assert voice.words_up(None) is False
    assert asked == [("probe", "http://127.0.0.1:8090/v1"), ("server", "lk")]
    assert voice.machine().words_up is voice.words_up


def test_a_save_that_is_refused_stops_there(settings, root):
    log = Log()

    code, saved = run_here(settings.model_copy(update=WORDS), FakeMachine(log, s2s="/bin/s2s"),
                           log, root, save_ok=False)

    assert code == 1 and len(saved) == 1
    assert log.of("script") == [] and log.of("done") == []


def test_a_voice_kokoro_is_not_known_to_have_is_kept_with_a_warning(settings, root):
    log = Log()

    code, saved = run_here(settings.model_copy(update=WORDS), FakeMachine(log, s2s="/bin/s2s"),
                           log, root, chosen="zf_xiaoxiao")

    assert code == 0 and saved[0]["VOICE_SERVER_VOICE"] == "zf_xiaoxiao"
    [(warning,)] = log.of("warn")
    assert "zf_xiaoxiao" in warning
    # Said while the voice is configured, which is the step it belongs to.
    configure = next(i for i, line in enumerate(log.lines) if line[:2] == ("step", "configure"))
    assert log.lines.index(("warn", warning)) > configure


def test_a_port_taken_by_another_is_moved_and_our_own_is_kept(settings, root):
    log = Log()
    words = settings.model_copy(update=WORDS)
    _code, saved = run_here(words, FakeMachine(log, s2s="/bin/s2s", ports={8765: 8766}), log, root)
    assert saved[0]["VOICE_SERVER_PORT"] == 8766
    assert saved[-1]["VOICE_BASE_URL"] == "http://127.0.0.1:8766/v1"

    log = Log()
    ours = words.model_copy(update={"voice_base_url": "http://127.0.0.1:18765/v1",
                                    "voice_server_port": 18765})
    _code, saved = run_here(ours, FakeMachine(log, s2s="/bin/s2s", ports={18765: 18766}),
                            log, root)
    assert saved[0]["VOICE_SERVER_PORT"] == 18765 and log.of("free_port") == []


# --- the speech models --------------------------------------------------------------------


def run_download(settings, machine, log, **kwargs):
    return voice.download(settings, machine, Recorder(log), platform="darwin", **kwargs)


def test_download_installs_speech_to_speech_then_fetches_what_the_settings_need(settings):
    from keryx.localmodels import speech

    log = Log()

    code = run_download(settings, FakeMachine(log), log)

    assert code == 0
    assert [step for step, _message in log.of("step")] == ["install", "download"]
    assert log.of("script") == [(s2s_install_argv(),)]
    assert log.of("fetch") == [("/tools/s2s/bin/python",
                                [repo.id for repo in speech.needed(settings, platform="darwin")])]
    assert log.of("progress") == [(0, 100), (100, 100)]
    assert log.of("done") == [("parakeet-tdt", "kokoro")]
    # Each step is told before the slow work it names begins.
    assert log.kinds().index("step") < log.kinds().index("script")
    assert log.kinds().index("fetch") > [i for i, k in enumerate(log.kinds()) if k == "step"][1]


def test_download_with_speech_to_speech_installed_only_fetches(settings):
    log = Log()

    code = run_download(settings, FakeMachine(log, s2s="/bin/s2s"), log)

    assert code == 0 and log.of("script") == []
    assert [step for step, _message in log.of("step")] == ["download"]


def test_download_of_models_keryx_cannot_name_is_refused_before_anything(settings):
    log = Log()

    code = run_download(settings.model_copy(update={"voice_server_stt": "whisper-mlx"}),
                        FakeMachine(log), log)

    assert code == voice.REFUSED
    [(message, _log)] = log.of("error")
    assert "--stt whisper-mlx" in message
    assert log.of("script") == [] and log.of("step") == [] and log.of("fetch") == []


@pytest.mark.parametrize(("arrange", "said"), [
    ({"script_code": 1}, "the install did not finish"),
    ({"free": 10**9}, "needs about 5 GB free"),
])
def test_download_whose_install_fails_fetches_nothing(settings, arrange, said):
    log = Log()

    code = run_download(settings, FakeMachine(log, **arrange), log)

    assert code == 1 and log.of("fetch") == [] and log.of("done") == []
    assert said in log.of("error")[0][0]


def test_download_without_speech_to_speechs_own_python_says_so(settings):
    log = Log()
    machine = FakeMachine(log, s2s="/bin/s2s")
    machine.python = None

    code = run_download(settings, machine, log)

    assert code == 1 and log.of("fetch") == []
    assert "its own Python" in log.of("error")[0][0]


def test_download_that_stops_says_why_and_is_not_done(settings):
    log = Log()
    machine = FakeMachine(log, s2s="/bin/s2s")
    machine.fetched = "a/one could not be downloaded: OSError: disk full"

    code = run_download(settings, machine, log)

    assert code == 1 and log.of("done") == []
    assert log.of("error") == [("a/one could not be downloaded: OSError: disk full", None)]


def test_the_machine_downloads_through_speech_to_speechs_own_python(monkeypatch):
    from keryx.localmodels import runtimes, speech

    monkeypatch.setattr(runtimes, "speech_to_speech_python", lambda: "/tool/bin/python")
    monkeypatch.setattr(speech, "fetch", lambda python, repos, progress: f"{python} asked")
    machine = voice.machine()

    assert machine.speech_python() == "/tool/bin/python"
    assert machine.fetch_speech("/p", [], lambda done, total: None) == "/p asked"


def test_the_download_is_told_as_json_lines_ending_once():
    lines: list[str] = []
    events = voice.DownloadJsonSteps(lines.append)

    events.step("install", "Installing")
    events.progress(5, None)
    events.step("download", "Downloading")
    events.progress(50, 100)
    events.done("parakeet-tdt", "kokoro")
    events.error("late")

    import json

    told = [json.loads(line) for line in lines]
    assert told[1] == {"event": "progress", "step": "install", "done": 5, "total": None}
    assert told[-2] == {"event": "progress", "step": "download", "done": 50, "total": 100}
    assert told[-1] == {"event": "done", "stt": "parakeet-tdt", "tts": "kokoro"}


def test_the_download_is_told_in_lines_on_a_terminal():
    said: list[tuple] = []
    steps = voice.DownloadTextSteps(lambda message, err=False: said.append((message, err)))

    steps.progress(0, None)
    steps.progress(1 * 10**9, 4 * 10**9)
    steps.progress(int(1.1 * 10**9), 4 * 10**9)  # not a tenth further on: not said
    steps.progress(4 * 10**9, 4 * 10**9)
    steps.done("parakeet-tdt", "kokoro")

    assert [message for message, _err in said] == [
        "1.0 of 4.0 GB", "4.0 of 4.0 GB",
        "the voice's speech models are downloaded: parakeet-tdt to hear, kokoro to speak",
    ]


# --- the voice the wizard offers first -----------------------------------------------------


def test_the_current_voice_is_the_one_chosen_else_the_personas():
    store = ConfigStore()
    assert voice.current_voice(Settings(_env_file=None, openai_api_key="t"), store) == "af_heart"

    store.set({"ASSISTANT_NAME": "Jarvis"})
    assert voice.current_voice(Settings(_env_file=None, openai_api_key="t"), store) == "bm_george"

    store.set({"VOICE_SERVER_VOICE": "bf_emma"})
    assert voice.current_voice(Settings(_env_file=None, openai_api_key="t"), store) == "bf_emma"


# --- a server elsewhere, and OpenAI --------------------------------------------------------


def test_a_server_elsewhere_is_warned_about_and_asked_for_a_session():
    log = Log()
    machine = FakeMachine(log, realtime="could not open gpu's Realtime socket")

    check = voice.check_server("http://192.168.1.20:8765", "vk", probes=machine,
                               report=Recorder(log))

    assert check.endpoint.base_url == "http://192.168.1.20:8765/v1"
    assert check.problem == "could not open gpu's Realtime socket"
    assert len(check.warnings) == 1 and "spoken PIN" in check.warnings[0]
    assert log.of("warn") == [(check.warnings[0],)]
    assert log.of("realtime") == [("http://192.168.1.20:8765/v1", "vk")]
    assert log.kinds().index("step") < log.kinds().index("realtime")
    assert check.values() == {"VOICE_BASE_URL": "http://192.168.1.20:8765/v1",
                              "VOICE_API_KEY": "vk"}
    blank = voice.check_server("http://gpu:8765", "", probes=machine, report=Recorder(Log()))
    assert blank.values()["VOICE_API_KEY"] is None


def test_an_address_with_credentials_in_it_is_refused():
    with pytest.raises(ValueError, match="user or password"):
        voice.check_server("http://me:pw@gpu:8765", None, probes=FakeMachine(Log()),
                           report=Recorder(Log()))


def test_openai_is_no_voice_server_and_no_key_for_one():
    assert voice.to_openai() == {"VOICE_BASE_URL": None, "VOICE_API_KEY": None}


# --- status --------------------------------------------------------------------------------


class Asked:
    def __init__(self, *, program=None, unit=False, up=False, server=True,
                 ready=False) -> None:
        self.program, self.unit, self.up, self.server = program, unit, up, server
        self.ready = ready
        self.probed: list[str] = []
        self.servers: list[Endpoint] = []

    def status(self, settings):
        return voice.status(
            settings, program=lambda: self.program,
            installed=lambda kind: kind == "voice" and self.unit,
            probe=lambda url: self.probed.append(url) or (self.up, None),
            server_reachable=lambda endpoint: self.servers.append(endpoint) or self.server,
            models_ready=lambda settings: self.ready,
        )


def test_status_on_openai_without_a_local_model_needs_one(settings):
    asked = Asked()

    document = asked.status(settings)

    assert document == {
        "mode": "openai", "base_url": None,
        "realtime_model": settings.openai_realtime_model, "voice": settings.voice,
        "assistant": settings.assistant_name, "openai_key_set": True,
        "here": {"installed": False, "program": None, "voice": settings.voice_server_voice,
                 "port": 8765, "unit_installed": False, "reachable": False,
                 "words_model": None, "stt": "parakeet-tdt", "tts": "kokoro",
                 "models_ready": False},
        "needs": "local_agent_model",
        "server": {"reachable": None},
    }
    assert asked.servers == []  # nothing elsewhere is asked when it is not the voice


def test_status_here_is_the_voice_server_on_this_machine(settings):
    asked = Asked(program="/bin/s2s", unit=True, up=True, ready=True)
    here = settings.model_copy(update={**WORDS, "voice_base_url": "http://127.0.0.1:8765/v1",
                                       "voice_server_voice": "bm_george"})

    document = asked.status(here)

    assert document["mode"] == "here" and document["voice"] == "bm_george"
    assert document["base_url"] == "http://127.0.0.1:8765/v1"
    assert document["here"] == {"installed": True, "program": "/bin/s2s", "voice": "bm_george",
                                "port": 8765, "unit_installed": True, "reachable": True,
                                "words_model": "qwen3-coder-30b-a3b", "stt": "parakeet-tdt",
                                "tts": "kokoro", "models_ready": True}
    assert document["needs"] is None and document["server"] == {"reachable": None}
    assert asked.probed == ["http://127.0.0.1:8765/v1"]


def test_status_server_asks_the_server_and_never_says_its_key(settings):
    asked = Asked(server=False)
    there = settings.model_copy(update={"voice_base_url": "http://gpu:8765/v1",
                                        "voice_api_key": "vk-secret", "openai_api_key": None})

    document = asked.status(there)

    assert document["mode"] == "server" and document["server"] == {"reachable": False}
    assert document["openai_key_set"] is False
    assert [endpoint.api_key for endpoint in asked.servers] == ["vk-secret"]
    assert "vk-secret" not in str(document)
    # A voice server elsewhere on our own port is still not ours.
    assert voice.mode(there.model_copy(update={"voice_base_url": "http://gpu:8765/v1"})) == "server"


def test_a_server_that_answers_anything_short_of_a_server_error_is_reachable():
    calls: list[dict] = []

    def answering(code):
        def get(url, **kwargs):
            calls.append({"url": url, **kwargs})
            return httpx.Response(code, request=httpx.Request("GET", url))
        return get

    endpoint = Endpoint.parse("http://gpu:8765", api_key="vk")
    assert voice.server_reachable(endpoint, get=answering(404)) is True
    assert voice.server_reachable(endpoint, get=answering(503)) is False
    assert voice.server_reachable(endpoint, get=answering(407)) is False  # a proxy, not it
    assert calls[0]["url"] == "http://gpu:8765/v1/models"
    assert calls[0]["headers"] == {"Authorization": "Bearer vk"}
    assert calls[0]["timeout"] == voice.SERVER_REACH_S == 2.0
    assert calls[0]["trust_env"] is True  # a server elsewhere may be reached through one

    tunnel = Endpoint.parse("http://127.0.0.1:18765")  # an ssh tunnel: never via a proxy
    assert voice.server_reachable(tunnel, get=answering(200)) is True
    assert calls[-1]["trust_env"] is False

    def refused(url, **kwargs):
        raise httpx.ConnectError("refused")

    assert voice.server_reachable(endpoint, get=refused) is False


def test_a_voice_that_answered_but_could_not_be_saved_is_not_done(settings, root):
    log = Log()
    answers = iter([True, False])
    code = voice.here(settings.model_copy(update=WORDS), FakeMachine(log, s2s="/bin/s2s"),
                      Recorder(log), choose_voice=lambda: "af_heart",
                      save=lambda values: next(answers), root=root, platform="linux")

    assert code == 1 and log.of("done") == [] and log.of("wait")


def test_an_installer_runs_with_nothing_on_its_stdin_and_its_output_off_stdout(capfd):
    import sys

    code = voice.run_detached([sys.executable, "-c",
                               "import sys; print('installing'); print(sys.stdin.read() == '')"])

    out, err = capfd.readouterr()
    assert code == 0 and out == "" and err.split() == ["installing", "True"]
    assert voice.run_detached(["/no/such/installer"]) == 127


def test_a_units_installer_that_hangs_is_stopped_and_said_to_have_failed(
    tmp_path, monkeypatch, capfd
):
    import sys

    monkeypatch.setattr(voice, "INSTALL_TIMEOUT_S", 0.2)
    script = tmp_path / "install-launchd.sh"
    script.write_text(f"#!{sys.executable}\nimport time; time.sleep(30)\n")
    script.chmod(0o755)

    code = voice.run_detached([str(script), "--voice"])

    assert code == voice.TIMED_OUT == 124
    assert "install-launchd.sh did not finish within 0.2 s" in capfd.readouterr().err
    assert voice.exited("install-launchd.sh --voice", voice.TIMED_OUT) == (
        "install-launchd.sh --voice did not finish within 0.2 s")
    assert voice.exited("install-launchd.sh --voice", 3) == "install-launchd.sh --voice exited " \
                                                            "with 3"


def test_only_a_units_installer_is_timed_never_a_download(monkeypatch):
    """uv installing speech-to-speech, brew or ollama pulling take as long as they take."""
    import subprocess

    timeouts: dict = {}

    def run(argv, **kwargs):
        timeouts[argv[0]] = kwargs.get("timeout")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", run)

    voice.run_detached(["/repo/scripts/install-systemd.sh", "--voice", "--uninstall"])
    voice.run_detached(["uv", "tool", "install", "speech-to-speech"])

    assert timeouts == {"/repo/scripts/install-systemd.sh": voice.INSTALL_TIMEOUT_S, "uv": None}
