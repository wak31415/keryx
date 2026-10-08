"""The voice LLM, set up (`keryx.localmodels.voice_llm`): its own server here, a server
elsewhere, or the local agent's, and the owner's rule for a new install — driven through
fakes and a recording report."""

import contextlib
from pathlib import Path

import pytest
from wording import for_the_app

from keryx.config import Settings
from keryx.config.store import FROM_CONFIG, FROM_DEFAULT
from keryx.endpoints import Endpoint, Probe
from keryx.localmodels import voice, voice_llm
from keryx.localmodels.catalog import by_key
from keryx.localmodels.download import model_path
from keryx.localmodels.hardware import Hardware

MAC_48 = Hardware("metal", "Apple silicon", 0.0, 48.0)
SMALL, NINE = by_key("qwen3.5-4b"), by_key("qwen3.5-9b")
OWN = "http://127.0.0.1:8091/v1"
#: What the server elsewhere lists, unless a test says.
LISTED = Probe(True, ("gpt-5-mini",))
AGENT = {"local_agent_base_url": "http://127.0.0.1:8090/v1",
         "local_agent_model": "qwen3-coder-30b-a3b"}


class Run:
    """What the report was told and what the machine was asked, in one order."""

    def __init__(self) -> None:
        self.lines: list[tuple] = []

    def kinds(self) -> list[str]:
        return [line[0] for line in self.lines]

    def of(self, kind: str) -> list[tuple]:
        return [line[1:] for line in self.lines if line[0] == kind]

    def steps(self) -> list[str]:
        return [line[1] for line in self.lines if line[0] == "step"]

    @contextlib.contextmanager
    def step(self, step: str, message: str, *, detail: str | None = None):
        assert for_the_app(message), message
        self.lines.append(("step", step, message))
        yield

    def warn(self, message: str, *, detail: str | None = None) -> None:
        assert for_the_app(message), message
        self.lines.append(("warn", message, detail))

    def error(self, message: str, *, log: Path | None = None,
              detail: str | None = None) -> None:
        assert for_the_app(message), message
        self.lines.append(("error", message, detail, log))

    def progress(self, done: int, total: int | None) -> None:
        self.lines.append(("progress", done, total))

    def done(self, *args, **kwargs) -> None:
        self.lines.append(("done", *args, *kwargs.values()))


class Machine:
    """The machine, faked: a model server's probes, a free port, a download and a server
    elsewhere's model list."""

    def __init__(self, run: Run, *, llama="/usr/bin/llama-server", wait=None, served=SMALL.key,
                 units=(), pulled=0, listed=LISTED, hardware=MAC_48,
                 ports=None, codes=None, realtime=None, live=None) -> None:
        #: `wait`, `served` and `realtime` (the voice server's handshake) are each one answer,
        #: or a list of answers given in turn.
        self.run, self.llama, self.wait, self._served = run, llama, wait, served
        self.realtime, self.live = realtime, live
        self.units, self.pulled, self.listed, self._hardware = set(units), pulled, listed, hardware
        self.ports, self.codes = ports or {}, codes or {}

    def unit_installed(self, kind: str) -> bool:
        return kind in self.units

    def llama_server(self, cache: Path) -> str | None:
        return self.llama

    def ollama(self) -> str | None:
        return None

    def which(self, name: str) -> str | None:
        return None

    def hardware(self) -> Hardware:
        return self._hardware

    def download_llama_cpp(self, cache: Path, machine: Hardware, progress) -> str:
        self.run.lines.append(("download_llama_cpp",))
        self.llama = str(cache / "llama.cpp" / "llama-server")
        return self.llama

    def run_script(self, argv) -> int:
        self.run.lines.append(("script", [Path(argv[0]).name, *argv[1:]]))
        return self.codes.get(tuple(argv[1:]), 0)

    def wait_for_server(self, endpoint: Endpoint, kind: str, timeout: float):
        self.run.lines.append(("wait", kind, endpoint.base_url))
        said = _next(self.realtime if kind == "realtime" else self.wait)
        if isinstance(said, BaseException):
            raise said
        return said

    def served(self, url: str):
        self.run.lines.append(("served", url))
        said = _next(self._served)
        return said is not None, said

    def models(self, endpoint: Endpoint):
        return Probe(False, problem="not asked")

    def live_calls(self, settings) -> int | None:
        self.run.lines.append(("live_calls",))
        return self.live

    def free_port(self, port: int) -> int:
        self.run.lines.append(("free_port", port))
        return self.ports.get(port, port)

    def pull(self, settings, name: str, report) -> int:
        self.run.lines.append(("pull", name))
        if self.pulled:
            report.error("Qwen3.5 4B needs 3.2 GB free, and there is 1.0 GB.")
            return self.pulled
        with report.downloading(SMALL.title, 100) as progress:
            progress(100)
        return 0

    def remote_models(self, endpoint: Endpoint) -> Probe:
        self.run.lines.append(("remote", endpoint.base_url, endpoint.api_key))
        return self.listed


def _next(answers):
    """The next of a list of answers, or the one answer."""
    return answers.pop(0) if isinstance(answers, list) else answers


@pytest.fixture
def root(tmp_path) -> Path:
    scripts = tmp_path / "checkout" / "scripts"
    scripts.mkdir(parents=True)
    for name in ("install-launchd.sh", "install-systemd.sh"):
        (scripts / name).write_text("#!/bin/sh\n")
    return tmp_path / "checkout"


def on_disk(settings: Settings, entry=SMALL) -> None:
    path = model_path(settings.cache_dir, entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as out:
        out.truncate(entry.size)


class Saved(list):
    """What `save` was handed, each call a dict, and whether it said yes."""

    def __init__(self, ok: bool = True) -> None:
        super().__init__()
        self.ok = ok

    def __call__(self, values) -> bool:
        self.append(dict(values))
        return self.ok


def here(settings, machine, run, root, model=SMALL.key, saved=None):
    saved = Saved() if saved is None else saved
    code = voice_llm.here(settings, model, machine, run, save=saved, root=root,
                          platform="darwin")
    return code, saved


# --- here ------------------------------------------------------------------------------------


def test_here_downloads_installs_starts_checks_and_only_then_is_the_voice_llm(settings, root):
    run = Run()

    code, saved = here(settings, Machine(run), run, root)

    assert code == 0
    assert run.steps() == ["download", "runtime", "unit", "start", "check", "configure"]
    assert run.of("pull") == [(SMALL.key,)] and run.of("progress") == [(100, 100)]
    assert ("script", ["install-launchd.sh", "--voice-llm"]) in run.lines
    assert run.of("wait") == [("models", OWN)] and run.of("served") == [(OWN,)]
    # The unit reads its model and port; the source is saved once it serves that model.
    assert saved == [{"VOICE_LLM_MODEL": SMALL.key, "VOICE_LLM_SERVER_PORT": 8091},
                     {"VOICE_LLM": "here", "VOICE_LLM_MODEL": SMALL.key,
                      "VOICE_LLM_SERVER_PORT": 8091}]
    assert run.kinds().index("served") < run.kinds().index("done")
    assert run.of("done") == [("here", SMALL.key, OWN, False, False)]


def test_here_on_disk_already_downloads_nothing(settings, root):
    on_disk(settings)
    run = Run()

    code, _saved = here(settings, Machine(run), run, root)

    assert code == 0 and "download" not in run.steps() and run.of("pull") == []


def test_here_steps_past_the_agents_and_the_voice_servers_ports(settings, root):
    run = Run()
    taken = settings.model_copy(update={"agent_server_port": 8092})

    code, saved = here(taken, Machine(run, ports={8091: 8092}), run, root)

    assert code == 0
    assert saved[-1]["VOICE_LLM_SERVER_PORT"] == 8093  # 8091 was busy; 8092 is the agent's


def test_here_keeps_the_port_its_own_running_server_has(settings, root):
    run = Run()
    ours = settings.model_copy(update={"voice_llm": "here", "voice_llm_model": "qwen3.5-9b"})

    code, saved = here(ours, Machine(run, units={"voice_llm"}, ports={8091: 8092}), run, root)

    assert code == 0 and run.of("free_port") == []
    assert saved[-1]["VOICE_LLM_SERVER_PORT"] == 8091


@pytest.mark.parametrize(("model", "message"), [
    ("no-such-model", "There is no model called no-such-model."),
    ("/nowhere/voice.gguf", "That GGUF file is not there."),
])
def test_here_refuses_a_model_that_is_not_there_before_anything(settings, root, model,
                                                                 message):
    run = Run()

    code, saved = here(settings, Machine(run), run, root, model=model)

    assert code == 2 and saved == []
    [(kind, said, detail, _log)] = run.lines
    assert (kind, said) == ("error", message) and detail


def test_here_takes_a_gguf_of_your_own_by_its_path(settings, root, tmp_path):
    own = tmp_path / "voice.gguf"
    own.write_bytes(b"x")
    run = Run()

    code, saved = here(settings, Machine(run, served=str(own)), run, root, model=str(own))

    assert code == 0 and run.of("pull") == []
    assert saved[-1]["VOICE_LLM_MODEL"] == str(own)


def test_here_with_no_installer_is_refused_before_anything(settings, tmp_path):
    run = Run()

    code, saved = here(settings, Machine(run), run, tmp_path / "not-a-checkout")

    assert code == 2 and saved == [] and run.kinds() == ["error"]
    assert run.of("error")[0][0] == voice.NO_CHECKOUT


def test_here_refuses_a_port_another_server_is_set_to(settings, root):
    """Its own server's port is kept, and that port is the agent's: no unit, no download."""
    run = Run()
    clash = settings.model_copy(update={"voice_llm": "here", "voice_llm_model": SMALL.key,
                                        "voice_llm_server_port": 8090})

    code, saved = here(clash, Machine(run, units={"voice_llm"}), run, root)

    assert code == 2 and saved == [] and run.kinds() == ["error"]
    assert "both set to port 8090" in run.of("error")[0][0]


def test_here_whose_download_stops_installs_no_unit_and_saves_nothing(settings, root):
    run = Run()

    code, saved = here(settings, Machine(run, pulled=1), run, root)

    assert code == 1 and saved == [] and run.of("script") == []
    assert run.kinds()[-1] == "error"


@pytest.mark.parametrize(("arrange", "said"), [
    ({"wait": "could not reach 127.0.0.1"},
     "The voice LLM server did not come up, so its service was removed."),
    ({"served": "someone-elses"},
     "The voice LLM server serves another model, not Qwen3.5 4B, so its service was removed."),
])
def test_a_here_switch_that_fails_leaves_the_previous_settings_and_removes_its_unit(
    settings, root, arrange, said
):
    run = Run()
    before = settings.model_copy(update={**AGENT, "voice_llm_model": "qwen3.5-9b"})

    code, saved = here(before, Machine(run, **arrange), run, root)

    assert code == 1
    assert run.of("script")[-1] == (["install-launchd.sh", "--voice-llm", "--uninstall"],)
    # Saved for the unit to read, then put back as they were: the source never changed.
    assert saved == [{"VOICE_LLM_MODEL": SMALL.key, "VOICE_LLM_SERVER_PORT": 8091},
                     {"VOICE_LLM": None, "VOICE_LLM_MODEL": "qwen3.5-9b",
                      "VOICE_LLM_SERVER_PORT": None}]
    error = run.of("error")[-1]
    assert error[0] == said and error[2].name == "keryx-voice-llm.err.log"
    assert run.of("done") == []


def test_a_here_switch_cancelled_while_it_loads_removes_its_unit_and_puts_back_settings(
    settings, root
):
    run = Run()

    with pytest.raises(KeyboardInterrupt):
        here(settings, Machine(run, wait=KeyboardInterrupt()), run, root)

    assert run.of("script")[-1] == (["install-launchd.sh", "--voice-llm", "--uninstall"],)
    assert run.of("done") == [] and run.of("error") == []


@pytest.mark.parametrize(("again", "said"), [
    (None, "The voice LLM it replaced, Qwen3.5 9B, is running again."),
    ("still no answer", "The voice LLM it replaced, Qwen3.5 9B, did not come back either, so "
                        "the voice here has no words until one is started."),
])
def test_a_failed_switch_from_a_voice_llm_of_its_own_starts_the_old_one_again(
    settings, root, again, said
):
    """B replaced A under the one unit: A is started and checked again before the error."""
    run = Run()
    own = settings.model_copy(update={"voice_llm": "here", "voice_llm_model": NINE.key})
    machine = Machine(run, units={"voice_llm"}, wait=["no answer", again],
                      served=[NINE.key] if again is None else None)

    code, saved = here(own, machine, run, root)

    assert code == 1
    assert saved[-1] == {"VOICE_LLM": "here", "VOICE_LLM_MODEL": NINE.key,
                         "VOICE_LLM_SERVER_PORT": None}
    assert [argv[1:] for (argv,) in run.of("script")] == [
        ["--voice-llm"], ["--voice-llm", "--uninstall"], ["--voice-llm"],
        *([] if again is None else [["--voice-llm", "--uninstall"]])]
    # Its unit, start and check again, told as `restore`; no check for one that never loaded.
    assert run.steps().count("restore") == (3 if again is None else 2)
    message, detail, _log = run.of("error")[-1]
    assert message == "The voice LLM server did not come up, so its service was removed." + \
        " " + said
    assert "no answer" in detail and (again is None or "still no answer" in detail)
    assert run.kinds()[-1] == "error"


def test_here_warns_when_it_and_the_agents_model_may_not_both_fit(settings, root):
    run = Run()
    big = settings.model_copy(update={"agent_server_model": "gpt-oss-120b"})

    code, _saved = here(big, Machine(run), run, root)

    assert code == 0
    [(message, _detail)] = run.of("warn")
    assert "need about 79 GB together" in message and "36 GB" in message


def test_here_whose_settings_are_refused_installs_no_unit(settings, root):
    run = Run()

    code, saved = here(settings, Machine(run), run, root, saved=Saved(ok=False))

    assert code == 1 and len(saved) == 1
    assert ["install-launchd.sh", "--voice-llm"] not in [argv for (argv,) in run.of("script")]


def test_here_restarts_a_running_voice_server_onto_it_once_it_answers(settings, root):
    run = Run()
    voice_here = settings.model_copy(update={"voice_mode_explicit": "here"})

    code, saved = here(voice_here, Machine(run, units={"voice"}), run, root)

    assert code == 0
    assert run.steps()[-2:] == ["configure", "voice"]
    assert [argv[1:] for (argv,) in run.of("script")] == [["--voice-llm"], ["--voice"]]
    assert run.of("wait")[-1] == ("realtime", "http://127.0.0.1:8765/v1")
    assert saved[-1]["VOICE_LLM"] == "here"
    assert run.of("done") == [("here", SMALL.key, OWN, True, False)]


def test_here_says_a_voice_here_with_no_server_running_takes_it_when_started(settings, root):
    run = Run()

    here(settings.model_copy(update={"voice_mode_explicit": "here"}), Machine(run), run, root)

    assert run.of("done") == [("here", SMALL.key, OWN, False, True)]


@pytest.mark.parametrize(("previous", "units"), [
    ({"voice_llm": "here", "voice_llm_model": "qwen3.5-9b"}, {"voice", "voice_llm"}),
    ({**AGENT}, {"voice"}),
])
def test_a_voice_server_that_does_not_answer_on_the_new_llm_goes_back_to_the_old(
    settings, root, previous, units
):
    """B came up, but the voice did not answer on it: the old words are put back — A's own
    server started again, or B's removed when the words were the agent's — and the voice
    restarted onto them."""
    run = Run()
    before = settings.model_copy(update={**previous, "voice_mode_explicit": "here"})
    machine = Machine(run, units=units, served=[SMALL.key, NINE.key],
                      realtime=["no Realtime socket", None])

    code, saved = here(before, machine, run, root)

    assert code == 1 and run.of("done") == []
    assert saved[-1]["VOICE_LLM_MODEL"] == previous.get("voice_llm_model")
    flags = [argv[1:] for (argv,) in run.of("script")]
    assert flags[:2] == [["--voice-llm"], ["--voice"]]
    if previous.get("voice_llm") == "here":
        assert flags[2:] == [["--voice-llm"], ["--voice"]]  # A again, then the voice on A
    else:
        assert flags[2:] == [["--voice"]]  # the agent's server was never touched
    message, detail, _log = run.of("error")[-1]
    assert message == ("The voice server here did not answer on its new words, so they were "
                       "put back: it answers on the ones it had.")
    assert detail == "no Realtime socket"


# --- models start --voice-llm ---------------------------------------------------------------


class Start(Run):
    def done(self, url: str, served_model: str) -> None:
        self.lines.append(("done", url, served_model))


def start(settings, machine, run, root):
    return voice_llm.start(settings, machine, run, root=root, platform="darwin")


def test_the_voice_llms_server_starts_for_the_model_chosen_and_saves_nothing(settings, root):
    chosen = settings.model_copy(update={"voice_llm_model": SMALL.key})
    on_disk(chosen)
    run = Start()

    code = start(chosen, Machine(run), run, root)

    assert code == 0
    assert run.steps() == ["runtime", "unit", "start", "check"]
    assert run.of("done") == [(OWN, SMALL.key)]


@pytest.mark.parametrize("model", [None, SMALL.key, "/nowhere/voice.gguf"])
def test_the_voice_llms_server_is_refused_until_its_model_is_chosen_and_there(
    settings, root, model
):
    run = Start()

    code = start(settings.model_copy(update={"voice_llm_model": model}), Machine(run), run, root)

    assert code == 2 and run.kinds() == ["error"]
    message, detail, _log = run.of("error")[0]
    assert message == voice_llm.NOT_READY and detail


def test_the_voice_llms_server_is_refused_on_a_port_another_has(settings, root):
    chosen = settings.model_copy(update={"voice_llm_model": SMALL.key,
                                         "voice_llm_server_port": 8765})
    on_disk(chosen)
    run = Start()

    assert start(chosen, Machine(run), run, root) == 2
    assert "the voice server" in run.of("error")[0][0] and run.of("script") == []


def test_the_voice_llms_server_with_no_installer_is_refused(settings, tmp_path):
    chosen = settings.model_copy(update={"voice_llm_model": SMALL.key})
    on_disk(chosen)
    run = Start()

    assert start(chosen, Machine(run), run, tmp_path / "nowhere") == 2


def test_the_voice_llms_server_that_does_not_come_up_has_its_unit_removed(settings, root):
    chosen = settings.model_copy(update={"voice_llm_model": SMALL.key})
    on_disk(chosen)
    run = Start()

    code = start(chosen, Machine(run, wait="no answer"), run, root)

    assert code == 1
    assert run.of("script")[-1] == (["install-launchd.sh", "--voice-llm", "--uninstall"],)


def test_the_voice_llms_server_installs_llama_cpp_when_it_is_missing(settings, root):
    chosen = settings.model_copy(update={"voice_llm_model": SMALL.key})
    on_disk(chosen)
    run = Start()

    code = start(chosen, Machine(run, llama=None), run, root)

    assert code == 0 and ("download_llama_cpp",) in run.lines


def test_a_unit_that_will_not_install_is_said_and_never_waited_for(settings, root):
    chosen = settings.model_copy(update={"voice_llm_model": SMALL.key})
    on_disk(chosen)
    run = Start()

    code = start(chosen, Machine(run, codes={("--voice-llm",): 3}), run, root)

    assert code == 1 and run.of("wait") == []


# --- remote ----------------------------------------------------------------------------------


def remote(settings, machine, run, root, url="https://api.openai.com/v1", model="gpt-5-mini",
           key=None, new_key=False, saved=None):
    saved = Saved() if saved is None else saved
    code = voice_llm.remote(settings, url, model, machine, run, key=key, new_key=new_key,
                            save=saved, root=root, platform="darwin")
    return code, saved


def test_remote_on_openai_is_asked_with_the_owners_key_and_saved_once_it_lists_the_model(
    settings, root
):
    run = Run()

    code, saved = remote(settings, Machine(run), run, root)

    assert code == 0
    assert run.of("remote") == [("https://api.openai.com/v1", "test")]
    assert saved == [{"VOICE_LLM": "remote", "VOICE_LLM_BASE_URL": "https://api.openai.com/v1",
                      "VOICE_LLM_MODEL": "gpt-5-mini"}]
    assert run.of("done") == [("remote", "gpt-5-mini", "https://api.openai.com/v1", False,
                               False)]


def test_remote_elsewhere_is_never_sent_the_owners_openai_key(settings, root):
    run = Run()

    code, _saved = remote(settings, Machine(run, listed=Probe(True, ())), run, root,
                          url="http://gpu-box:11434", model="qwen3:4b")

    assert code == 0
    assert run.of("remote") == [("http://gpu-box:11434/v1", None)]


def test_remote_with_a_key_of_its_own_asks_with_it_and_saves_it_only_when_new(settings, root):
    run = Run()

    _code, kept = remote(settings, Machine(run), run, root, key="vk-stored")
    _code, typed = remote(settings, Machine(run), run, root, key="vk-new", new_key=True)
    _code, cleared = remote(settings, Machine(run), run, root, key=None, new_key=True)

    assert [asked[1] for asked in run.of("remote")] == ["vk-stored", "vk-new", "test"]
    assert "VOICE_LLM_API_KEY" not in kept[0]
    assert typed[0]["VOICE_LLM_API_KEY"] == "vk-new"
    assert "VOICE_LLM_API_KEY" in cleared[0] and cleared[0]["VOICE_LLM_API_KEY"] is None


@pytest.mark.parametrize(("listed", "said"), [
    (Probe(False, problem="api.openai.com does not accept that key"),
     "The voice LLM's server did not answer."),
    (Probe(True, ("gpt-4.1",)), "api.openai.com does not list gpt-5-mini."),
])
def test_remote_that_does_not_answer_or_list_the_model_saves_nothing(settings, root, listed,
                                                                     said):
    run = Run()

    code, saved = remote(settings, Machine(run, listed=listed), run, root)

    assert code == 1 and saved == []
    assert run.of("error")[0][0] == said


@pytest.mark.parametrize(("url", "model"), [("ftp://box", "m"), ("http://me:pw@box", "m"),
                                            ("http://box", " ")])
def test_remote_refuses_what_is_no_address_or_no_model_before_asking(settings, root, url,
                                                                     model):
    run = Run()

    code, saved = remote(settings, Machine(run), run, root, url=url, model=model)

    assert code == 2 and saved == [] and run.of("remote") == []
    assert "pw" not in str(run.lines)


def test_remote_warns_of_a_public_address_with_no_key(settings, root):
    run = Run()

    remote(settings, Machine(run, listed=Probe(True, ())), run, root,
           url="http://llm.example.com", model="m")

    assert any("public address" in message for message, _ in run.of("warn"))


def test_remote_removes_the_voice_llms_own_server_which_nothing_uses_then(settings, root):
    run = Run()

    code, _saved = remote(settings, Machine(run, units={"voice_llm"}), run, root)

    assert code == 0
    assert run.of("script") == [(["install-launchd.sh", "--voice-llm", "--uninstall"],)]
    assert run.steps() == ["check", "configure", "unit"]


# --- shared ----------------------------------------------------------------------------------


def shared(settings, machine, run, root, saved=None):
    saved = Saved() if saved is None else saved
    code = voice_llm.shared(settings, machine, run, save=saved, root=root, platform="darwin")
    return code, saved


def test_shared_saves_the_local_agents_model_and_removes_the_voice_llms_own_unit(settings,
                                                                                 root):
    run = Run()

    code, saved = shared(settings.model_copy(update=AGENT), Machine(run, units={"voice_llm"}),
                         run, root)

    assert code == 0 and saved == [{"VOICE_LLM": "shared"}]
    assert run.of("script") == [(["install-launchd.sh", "--voice-llm", "--uninstall"],)]
    assert run.of("done") == [("shared", "qwen3-coder-30b-a3b", "http://127.0.0.1:8090/v1",
                               False, False)]


def test_shared_with_no_unit_of_its_own_removes_nothing(settings, root):
    run = Run()

    code, _saved = shared(settings.model_copy(update=AGENT), Machine(run), run, root)

    assert code == 0 and run.of("script") == [] and run.steps() == ["configure"]


def test_shared_whose_unit_will_not_go_still_saves_and_says_so(settings, root):
    run = Run()
    machine = Machine(run, units={"voice_llm"}, codes={("--voice-llm", "--uninstall"): 1})

    code, saved = shared(settings.model_copy(update=AGENT), machine, run, root)

    assert code == 0 and saved == [{"VOICE_LLM": "shared"}]
    assert "still installed" in run.of("warn")[0][0]


def test_shared_is_refused_while_the_voice_is_here_and_the_agent_has_no_model(settings, root):
    run = Run()

    code, saved = shared(settings.model_copy(update={"voice_mode_explicit": "here"}),
                         Machine(run, units={"voice_llm"}), run, root)

    assert code == 2 and saved == [] and run.of("script") == []
    assert run.of("error")[0][0] == voice_llm.NO_WORDS_LEFT


def test_shared_with_no_local_model_and_no_voice_here_is_saved_with_a_warning(settings, root):
    run = Run()

    code, saved = shared(settings, Machine(run), run, root)

    assert code == 0 and saved == [{"VOICE_LLM": "shared"}]
    assert "no model yet" in run.of("warn")[0][0]
    assert run.of("done") == [("shared", None, None, False, False)]


def test_shared_restarts_a_running_voice_server_before_it_retires_the_old_unit(settings,
                                                                               root):
    run = Run()
    here_ = settings.model_copy(update={**AGENT, "voice_mode_explicit": "here",
                                        "voice_llm": "here", "voice_llm_model": SMALL.key})

    code, _saved = shared(here_, Machine(run, units={"voice", "voice_llm"}), run, root)

    assert code == 0
    assert run.steps() == ["configure", "voice", "unit"]
    assert [argv[1:] for (argv,) in run.of("script")] == [
        ["--voice"], ["--voice-llm", "--uninstall"]]
    assert run.of("done")[0][-2:] == (True, False)


@pytest.mark.parametrize(("realtime", "said", "voice_removed"), [
    (["no Realtime socket", None], "so they were put back: it answers on the ones it had.",
     False),
    (["no Realtime socket", "still none"], "and it did not answer on the ones it had either, "
                                           "so its service was removed.", True),
])
def test_a_switch_the_voice_does_not_answer_on_keeps_the_old_unit_and_settings(
    settings, root, realtime, said, voice_removed
):
    run = Run()
    here_ = settings.model_copy(update={**AGENT, "voice_mode_explicit": "here",
                                        "voice_llm": "here", "voice_llm_model": SMALL.key})

    code, saved = shared(here_, Machine(run, units={"voice", "voice_llm"}, realtime=realtime),
                         run, root)

    assert code == 1 and run.of("done") == []
    assert saved == [{"VOICE_LLM": "shared"}, {"VOICE_LLM": "here"}]
    flags = [argv[1:] for (argv,) in run.of("script")]
    assert ["--voice-llm", "--uninstall"] not in flags  # the old words' server is kept
    assert flags == [["--voice"], ["--voice"], *([["--voice", "--uninstall"]]
                                                 if voice_removed else [])]
    assert run.of("error")[-1][0].endswith(said)


def test_a_switch_cancelled_while_the_voice_restarts_puts_the_old_words_back(settings, root):
    run = Run()
    here_ = settings.model_copy(update={**AGENT, "voice_mode_explicit": "here",
                                        "voice_llm": "here", "voice_llm_model": SMALL.key})

    with pytest.raises(KeyboardInterrupt):
        shared(here_, Machine(run, units={"voice", "voice_llm"}, realtime=KeyboardInterrupt()),
               run, root)

    assert [argv[1:] for (argv,) in run.of("script")] == [["--voice"], ["--voice"]]


def test_remote_restarts_a_running_voice_server_onto_it(settings, root):
    run = Run()
    here_ = settings.model_copy(update={"voice_mode_explicit": "here"})

    code, _saved = remote(here_, Machine(run, units={"voice"}), run, root)

    assert code == 0 and run.steps() == ["check", "configure", "voice"]


def test_shared_whose_save_is_refused_ends_without_done(settings, root):
    run = Run()

    code, _saved = shared(settings.model_copy(update=AGENT), Machine(run, units={"voice_llm"}),
                          run, root, saved=Saved(ok=False))

    assert code == 1 and run.of("done") == [] and run.of("script") == []


# --- the owner's rule for a new install ------------------------------------------------------


@pytest.mark.parametrize(("source", "mode", "first"), [
    (FROM_DEFAULT, "openai", True), (FROM_DEFAULT, "server", True),
    (FROM_DEFAULT, "here", False),  # an existing install's voice: it keeps `shared`
    (FROM_CONFIG, "openai", False),  # a VOICE_LLM set by anyone is never changed
])
def test_only_a_first_set_up_with_voice_llm_never_set_chooses_it(settings, source, mode,
                                                                 first):
    chosen = settings.model_copy(update={"voice_mode_explicit": mode})

    assert voice_llm.first_set_up(chosen, source) is first


def test_the_first_choice_is_the_catalogs_rule_for_the_agents_model_here(settings):
    coder = settings.model_copy(update={"agent_server_model": "qwen3-coder-30b-a3b"})

    assert voice_llm.first_choice(coder, MAC_48).key == "qwen3.5-4b"  # 25 + 6 + 3 <= 36
    assert voice_llm.first_choice(coder, Hardware("metal", "M", 0, 36)) is None  # 34 > 27


def test_under_tells_every_step_as_one(settings):
    run = Run()
    under = voice_llm.Under(run, voice_llm.VOICE_STEP)

    with under.step("download", "Downloading"):
        under.progress(1, 2)
    under.warn("careful")
    under.error("stopped")

    assert run.lines[0] == ("step", "voice_llm", "Downloading")
    assert run.kinds() == ["step", "progress", "warn", "error"]


def test_the_machine_asks_through_each_module_when_asked(monkeypatch):
    from keryx.localmodels import pull, servers

    machine = voice_llm.machine()

    assert machine.pull is pull.pull and machine.free_port is servers.free_port
    seen = []
    monkeypatch.setattr("keryx.endpoints.probe",
                        lambda endpoint, get: seen.append(get.keywords) or Probe(True))
    machine.remote_models(Endpoint.parse("http://127.0.0.1:11434"))
    machine.remote_models(Endpoint.parse("https://api.openai.com/v1"))
    assert seen == [{"trust_env": False}, {"trust_env": True}]


# --- the rarer ways it stops -----------------------------------------------------------------


def test_here_with_no_free_port_is_refused_before_anything(settings, root):
    run = Run()

    class Full(Machine):
        def free_port(self, port: int) -> int:
            raise OSError("no free port from 8091 up")

    code, saved = here(settings, Full(run), run, root)

    assert code == 2 and saved == []
    assert run.of("error") == [("There is no free port for the voice LLM's server.",
                                "no free port from 8091 up", None)]


def test_here_whose_llama_cpp_cannot_be_installed_saves_nothing(settings, root):
    on_disk(settings)
    run = Run()

    class NoLlama(Machine):
        def download_llama_cpp(self, cache, machine, progress) -> str:
            raise RuntimeError("no release build")

    code, saved = here(settings, NoLlama(run, llama=None), run, root)

    assert code == 1 and saved == [] and run.of("script") == []


def test_here_whose_source_cannot_be_saved_ends_without_done(settings, root):
    run = Run()

    class Second(Saved):
        def __call__(self, values) -> bool:
            super().__call__(values)
            return "VOICE_LLM" not in values

    code, _saved = here(settings, Machine(run), run, root, saved=Second())

    assert code == 1 and run.of("done") == []


def test_remote_whose_choice_cannot_be_saved_removes_nothing(settings, root):
    run = Run()

    code, _saved = remote(settings, Machine(run, units={"voice_llm"}), run, root,
                          saved=Saved(ok=False))

    assert code == 1 and run.of("script") == [] and run.of("done") == []


def test_a_model_already_on_this_machine_is_told_as_the_download_step(settings):
    run = Run()
    pulled = voice_llm._Pulled(run)

    pulled.adopted(Path("/models/a.gguf"), Path("/hf/a.gguf"))
    pulled.done(Path("/models/a.gguf"))
    pulled.error("It needs more room.", detail="on /models")

    assert run.lines == [("step", "download", "The model is already on this machine: Keryx "
                                              "uses that copy"),
                         ("error", "It needs more room.", "on /models", None)]


def test_the_voice_llms_server_whose_llama_cpp_cannot_be_installed_starts_nothing(settings,
                                                                                   root):
    chosen = settings.model_copy(update={"voice_llm_model": SMALL.key})
    on_disk(chosen)
    run = Start()

    class NoLlama(Machine):
        def download_llama_cpp(self, cache, machine, progress) -> str:
            raise RuntimeError("no release build")

    assert start(chosen, NoLlama(run, llama=None), run, root) == 1
    assert run.of("script") == [] and run.of("done") == []


# --- a server elsewhere never hears its key read back -----------------------------------------


@pytest.mark.parametrize("listed", [
    Probe(True, ("vk-secret-echoed",)),
    Probe(False, problem="llm.example.com refused vk-secret-echoed"),
])
def test_a_server_that_echoes_the_key_never_has_it_said(settings, root, listed):
    run = Run()

    code, _saved = remote(settings, Machine(run, listed=listed), run, root,
                          url="https://llm.example.com", model="small", key="vk-secret-echoed",
                          new_key=True)

    assert code == 1
    assert "vk-secret-echoed" not in str([line for line in run.lines if line[0] != "remote"])
    assert "[redacted]" in str(run.of("error"))


def test_a_voice_that_answers_on_neither_llm_has_its_unit_removed(settings, root):
    """B came up but the voice did not answer on it, and A would not come back: no voice
    unit is left restarting with no words."""
    run = Run()
    before = settings.model_copy(update={"voice_llm": "here", "voice_llm_model": NINE.key,
                                         "voice_mode_explicit": "here"})
    machine = Machine(run, units={"voice", "voice_llm"}, wait=[None, "no answer"],
                      served=[SMALL.key], realtime="no Realtime socket")

    code, _saved = here(before, machine, run, root)

    assert code == 1
    assert [argv[1:] for (argv,) in run.of("script")][-1] == ["--voice", "--uninstall"]
    message, detail, _log = run.of("error")[-1]
    assert "the voice LLM it had did not come back, so its service was removed" in message
    assert "no Realtime socket" in detail and "no answer" in detail


def test_a_switch_cancelled_while_it_loads_puts_the_one_it_replaced_back_at_once(settings,
                                                                                 root):
    """Cancelled: A's settings and A's unit go back together, Cancel held off between them,
    with no wait for A to load — the owner asked to stop."""
    run = Run()
    own = settings.model_copy(update={"voice_llm": "here", "voice_llm_model": NINE.key})
    machine = Machine(run, units={"voice_llm"}, wait=[KeyboardInterrupt()])

    with pytest.raises(KeyboardInterrupt):
        here(own, machine, run, root)

    assert [argv[1:] for (argv,) in run.of("script")] == [
        ["--voice-llm"], ["--voice-llm", "--uninstall"], ["--voice-llm"]]
    assert run.of("wait") == [("models", OWN)] and run.of("served") == []


def test_a_cancelled_switch_holds_off_a_second_cancel_while_it_puts_a_back(settings, root,
                                                                           monkeypatch):
    held: list[bool] = []
    real = voice.cancel_held_off

    @contextlib.contextmanager
    def watched():
        with real():
            held.append(True)
            yield

    monkeypatch.setattr(voice, "cancel_held_off", watched)
    run = Run()
    own = settings.model_copy(update={"voice_llm": "here", "voice_llm_model": NINE.key})

    with pytest.raises(KeyboardInterrupt):
        here(own, Machine(run, units={"voice_llm"}, wait=[KeyboardInterrupt()]), run, root)

    assert len(held) >= 2  # around the save and A's unit, and around the reinstall itself


def test_a_cancel_with_no_words_to_go_back_to_removes_the_voice_unit(settings, root):
    """Cancelled while the voice restarts, and the old words could not be saved back: no
    voice unit is left restarting with words that are not there."""
    run = Run()
    before = settings.model_copy(update={"voice_llm": "here", "voice_llm_model": NINE.key,
                                         "voice_mode_explicit": "here"})
    machine = Machine(run, units={"voice", "voice_llm"}, served=[SMALL.key],
                      realtime=KeyboardInterrupt())

    class ThirdRefused(Saved):
        def __call__(self, values) -> bool:
            super().__call__(values)
            return len(self) < 3

    with pytest.raises(KeyboardInterrupt):
        here(before, machine, run, root, saved=ThirdRefused())

    assert [argv[1:] for (argv,) in run.of("script")][-1] == ["--voice", "--uninstall"]


def test_a_cancel_while_the_voice_restarts_puts_a_back_and_the_voice_on_it(settings, root):
    run = Run()
    before = settings.model_copy(update={"voice_llm": "here", "voice_llm_model": NINE.key,
                                         "voice_mode_explicit": "here"})
    machine = Machine(run, units={"voice", "voice_llm"}, served=[SMALL.key],
                      realtime=KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        here(before, machine, run, root)

    assert [argv[1:] for (argv,) in run.of("script")] == [
        ["--voice-llm"], ["--voice"], ["--voice-llm"], ["--voice"]]


def test_a_source_that_cannot_be_saved_and_cannot_be_put_back_says_both(settings, root):
    run = Run()
    own = settings.model_copy(update={"voice_llm": "here", "voice_llm_model": NINE.key})

    class AllButFirstRefused(Saved):
        def __call__(self, values) -> bool:
            super().__call__(values)
            return len(self) == 1

    code, _saved = here(own, Machine(run, units={"voice_llm"}), run, root,
                        saved=AllButFirstRefused())

    assert code == 1
    assert run.of("error")[-1][0].startswith(voice_llm.NOT_PUT_BACK)


# --- a live call is never cut off -----------------------------------------------------------


VOICE_HERE = {"voice_mode_explicit": "here", **AGENT}


@pytest.mark.parametrize("switch", ["here", "shared", "remote"])
def test_a_switch_is_refused_while_a_call_is_live_on_the_voice_server_here(settings, root,
                                                                           switch):
    run = Run()
    live = settings.model_copy(update=VOICE_HERE)
    machine = Machine(run, units={"voice", "voice_llm"}, live=2)

    if switch == "here":
        code, saved = here(live, machine, run, root)
    elif switch == "shared":
        code, saved = shared(live, machine, run, root)
    else:
        code, saved = remote(live, machine, run, root)

    assert code == 1 and saved == []
    assert run.kinds() == ["live_calls", "error"]
    assert run.of("error") == [(voice_llm.LIVE_CALL, "2 live call(s)", None)]


@pytest.mark.parametrize(("update", "units", "live", "asked"), [
    (VOICE_HERE, {"voice"}, 0, True),       # no call: it goes on
    (VOICE_HERE, {"voice"}, None, True),    # Keryx not answering: it goes on, as restart does
    (VOICE_HERE, set(), 3, False),          # no voice server running here: nothing to cut off
    (AGENT, {"voice"}, 3, False),           # the voice is not here: calls do not use it
])
def test_a_switch_goes_on_when_no_call_could_be_cut_off(settings, root, update, units, live,
                                                       asked):
    run = Run()

    code, _saved = shared(settings.model_copy(update=update),
                          Machine(run, units=units, live=live), run, root)

    assert code == 0 and (("live_calls",) in run.lines) is asked
