"""The model server, set up: the one implementation `keryx setup` and `keryx models start`
share (`keryx.localmodels.server`), driven here through fakes and a recording report."""

import contextlib
from pathlib import Path

import pytest
from wording import for_the_app

from keryx.config import Settings
from keryx.endpoints import Endpoint
from keryx.localmodels import server, voice
from keryx.localmodels.catalog import by_key
from keryx.localmodels.hardware import Hardware

MACHINE = Hardware("metal", "Apple silicon", 0.0, 36.0)
ENTRY = by_key("gpt-oss-20b")


class Recorder:
    """What the report was told, and what the fakes were asked, in one order."""

    def __init__(self) -> None:
        self.lines: list[tuple] = []

    def kinds(self) -> list[str]:
        return [line[0] for line in self.lines]

    def of(self, kind: str) -> list[tuple]:
        return [line[1:] for line in self.lines if line[0] == kind]

    @contextlib.contextmanager
    def step(self, step: str, message: str):
        self.lines.append(("step", step, message))
        yield

    def warn(self, message: str) -> None:
        self.lines.append(("warn", message))

    def error(self, message: str, *, log: Path | None = None,
              detail: str | None = None) -> None:
        # Every way `start` stops is a sentence the app can show as it is.
        assert for_the_app(message), message
        self.lines.append(("error", message, detail, log))

    def script(self, code: int = 0):
        def run(argv) -> int:
            self.lines.append(("script", list(argv)))
            return code

        return run

    def wait(self, problem: str | None = None):
        def wait_for(endpoint: Endpoint, kind: str, timeout: float) -> str | None:
            self.lines.append(("wait", kind, endpoint.base_url, timeout))
            return problem

        return wait_for


def checkout(root: Path) -> Path:
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    for name in ("install-launchd.sh", "install-systemd.sh"):
        (root / "scripts" / name).write_text("#!/bin/sh\n")
    return root


# --- llama.cpp -----------------------------------------------------------------------------


def test_homebrew_is_used_on_a_mac_that_has_it_and_only_there():
    assert server.uses_brew(platform="darwin", which=lambda name: f"/opt/homebrew/bin/{name}")
    assert not server.uses_brew(platform="darwin", which=lambda name: None)
    assert not server.uses_brew(platform="linux", which=lambda name: "/usr/bin/brew")


def test_llama_cpp_from_homebrew(tmp_path):
    log = Recorder()

    found = server.install_llama(tmp_path, MACHINE, brew=True, run_script=log.script(0),
                                 install=lambda cache, machine: pytest.fail("not downloaded"))

    assert found is None  # Homebrew puts it on PATH; the caller looks for it there
    assert log.of("script") == [(["brew", "install", "llama.cpp"],)]


def test_homebrew_that_does_not_finish_is_a_sentence(tmp_path):
    with pytest.raises(server.InstallError, match="^brew install llama.cpp did not finish$"):
        server.install_llama(tmp_path, MACHINE, brew=True, run_script=Recorder().script(1),
                             install=lambda cache, machine: "x")


def test_llama_cpp_from_its_release_builds(tmp_path):
    asked = []

    def install(cache, machine):
        asked.append((cache, machine))
        return str(cache / "llama.cpp" / "llama-server")

    found = server.install_llama(tmp_path, MACHINE, brew=False,
                                 run_script=lambda argv: pytest.fail("no script"),
                                 install=install)

    assert found == str(tmp_path / "llama.cpp" / "llama-server")
    assert asked == [(tmp_path, MACHINE)]


def test_a_release_download_that_fails_is_a_sentence(tmp_path):
    def broken(cache, machine):
        raise RuntimeError("llama.cpp has no release build for this system")

    with pytest.raises(server.InstallError,
                       match="^could not install llama.cpp: llama.cpp has no release build"):
        server.install_llama(tmp_path, MACHINE, brew=False, run_script=Recorder().script(),
                             install=broken)


# --- its unit, and waiting for it ------------------------------------------------------------


def test_the_unit_is_installed_and_the_server_waited_for(tmp_path):
    log, root = Recorder(), checkout(tmp_path / "checkout")
    settings = Settings(llm_server_port=8091)

    ok = server.serve_here(settings, log, run_script=log.script(0), wait_for_server=log.wait(),
                           root=root, platform="darwin")

    assert ok is True
    assert log.lines == [
        ("step", "unit", "Installing the model server's service"),
        ("script", [str(root / "scripts" / "install-launchd.sh"), "--llm"]),
        ("step", "start", "Loading the model into llama.cpp at http://127.0.0.1:8091/v1"),
        ("wait", "models", "http://127.0.0.1:8091/v1", server.LLM_START_S),
    ]


def test_a_unit_that_will_not_install_is_said_and_not_waited_for(tmp_path):
    log, root = Recorder(), checkout(tmp_path / "checkout")

    ok = server.serve_here(Settings(), log, run_script=log.script(3), wait_for_server=log.wait(),
                           root=root, platform="linux")

    assert ok is False
    assert log.of("error") == [("The model server's service could not be installed.",
                                "install-systemd.sh --llm exited with 3", None)]
    assert not log.of("wait")
    # Its installer writes the unit before loading it: what it left is removed, and said.
    script = str(root / "scripts" / "install-systemd.sh")
    assert log.of("script")[-1] == ([script, "--llm", "--uninstall"],)
    assert log.of("warn") == [("its service could not be removed: install-systemd.sh --llm "
                               "--uninstall exited with 3",)]
    assert log.kinds().index("warn") < log.kinds().index("error")


@pytest.mark.parametrize("removal", [0, 4, OSError("gone")])
def test_a_cancel_while_the_unit_installs_removes_it(tmp_path, removal):
    """…whatever the removal then says: the Cancel goes on."""
    log, root = Recorder(), checkout(tmp_path / "checkout")
    script = str(root / "scripts" / "install-launchd.sh")

    def run(argv) -> int:
        log.lines.append(("script", list(argv)))
        if argv[-1] == "--llm":
            raise KeyboardInterrupt
        if isinstance(removal, BaseException):
            raise removal
        return removal

    with pytest.raises(KeyboardInterrupt):
        server.serve_here(Settings(), log, run_script=run, wait_for_server=log.wait(),
                          root=root, platform="darwin")

    assert [argv for (argv,) in log.of("script")] == [
        [script, "--llm"], [script, "--llm", "--uninstall"]]
    assert not log.of("error") and not log.of("wait")


class CancelledAt(Recorder):
    """A report whose telling of one step is where the Cancel lands."""

    def __init__(self, at: str) -> None:
        super().__init__()
        self.at = at

    @contextlib.contextmanager
    def step(self, step: str, message: str):
        if step == self.at:
            raise KeyboardInterrupt
        with super().step(step, message):
            yield


def test_a_cancel_as_the_start_is_told_still_removes_the_unit(tmp_path):
    log, root = CancelledAt("start"), checkout(tmp_path / "checkout")
    script = str(root / "scripts" / "install-launchd.sh")

    with pytest.raises(KeyboardInterrupt):
        server.serve_here(Settings(), log, run_script=log.script(0), wait_for_server=log.wait(),
                          root=root, platform="darwin")

    assert [argv for (argv,) in log.of("script")] == [
        [script, "--llm"], [script, "--llm", "--uninstall"]]
    assert not log.of("wait")


def test_a_cancel_as_the_check_is_told_still_removes_the_unit(chosen, tmp_path):
    log = CancelledAt("check")
    machine = Machine(Run())
    machine.log = log

    with pytest.raises(KeyboardInterrupt):
        start(chosen, machine, log, tmp_path)

    script = str(tmp_path / "checkout" / "scripts" / "install-launchd.sh")
    assert [argv for (argv,) in log.of("script")] == [
        [script, "--llm"], [script, "--llm", "--uninstall"]]


def test_no_installer_here_is_said(tmp_path):
    log = Recorder()

    ok = server.serve_here(Settings(), log, run_script=log.script(), wait_for_server=log.wait(),
                           root=tmp_path, platform="darwin")

    assert ok is False and log.of("error")[0][0] == voice.NO_CHECKOUT
    assert "is not here" in log.of("error")[0][1]
    assert not log.of("script")


def test_a_server_that_does_not_come_up_points_at_its_log(tmp_path):
    log, root = Recorder(), checkout(tmp_path / "checkout")
    settings = Settings()

    ok = server.serve_here(settings, log, run_script=log.script(),
                           wait_for_server=log.wait("could not reach 127.0.0.1 (ConnectError)"),
                           root=root, platform="darwin")

    assert ok is False
    [(message, detail, where)] = log.of("error")
    assert message == "The model server did not come up, so its service was removed."
    assert detail.startswith("could not reach 127.0.0.1 (ConnectError)")
    assert where == settings.state_dir / "logs" / "keryx-llm.err.log"


# --- Ollama --------------------------------------------------------------------------------


def test_ollama_pulls_the_catalog_model_and_is_pointed_at():
    log = Recorder()

    assert server.pull_into_ollama(ENTRY, log.script(0)) is None
    assert log.of("script") == [(["ollama", "pull", ENTRY.ollama],)]
    assert server.pull_into_ollama(ENTRY, log.script(1)) == "ollama pull did not finish"
    assert server.ollama_values(ENTRY) == {
        "LOCAL_AGENT_BASE_URL": "http://127.0.0.1:11434/v1", "LOCAL_AGENT_MODEL": ENTRY.ollama,
        "LLM_SERVER_MODEL": None, "LOCAL_AGENT_API_KEY": None,
        "LOCAL_AGENT_API": "anthropic-messages",
    }


# --- keryx models start: the whole of it ---------------------------------------------------

CODER = by_key("qwen3-coder-30b-a3b")


class Run(Recorder):
    """A report that also takes progress and the ending, as `keryx models start` tells it."""

    def progress(self, done: int, total: int | None) -> None:
        self.lines.append(("progress", done, total))

    def done(self, url: str, served_model: str) -> None:
        self.lines.append(("done", url, served_model))


class Machine:
    """The machine `start` asks, faked: what it has, and what each question answers."""

    def __init__(self, log: Run, *, llama="/usr/bin/llama-server", ollama=None, brew=None,
                 codes=None, wait=None, served=CODER.key, listed=(), unit=False) -> None:
        self.log, self.llama, self._ollama, self.brew = log, llama, ollama, brew
        self.codes, self.wait, self._served, self.listed = codes or {}, wait, served, listed
        self.unit = unit

    def unit_installed(self, kind: str) -> bool:
        return self.unit

    def llama_server(self, cache: Path) -> str | None:
        return self.llama

    def ollama(self) -> str | None:
        return self._ollama

    def which(self, name: str) -> str | None:
        return self.brew if name == "brew" else None

    def hardware(self) -> Hardware:
        return MACHINE

    def download_llama_cpp(self, cache: Path, machine: Hardware, progress) -> str:
        self.log.lines.append(("download_llama_cpp",))
        progress(5, 10)
        progress(10, 10)
        self.llama = str(cache / "llama.cpp" / "llama-server")
        return self.llama

    def run_script(self, argv) -> int:
        self.log.lines.append(("script", list(argv)))
        code = self.codes.get(Path(argv[0]).name, 0)
        if argv[0] == "brew" and code == 0:
            self.llama = "/opt/homebrew/bin/llama-server"
        return code

    def wait_for_server(self, endpoint: Endpoint, kind: str, timeout: float):
        self.log.lines.append(("wait", kind, endpoint.base_url))
        if isinstance(self.wait, BaseException):
            raise self.wait
        return self.wait

    def served(self, url: str):
        self.log.lines.append(("served", url))
        if isinstance(self._served, BaseException):
            raise self._served
        return self._served is not None, self._served

    def models(self, endpoint: Endpoint):
        from keryx.endpoints import Probe

        self.log.lines.append(("models", endpoint.base_url))
        return Probe(True, tuple(self.listed)) if self._ollama else Probe(False, problem="no")


@pytest.fixture
def chosen(tmp_path):
    """LLM_SERVER_MODEL chosen and downloaded (a sparse file the catalog's size)."""
    from keryx.localmodels.download import model_path

    settings = Settings(llm_server_model=CODER.key)
    path = model_path(settings.cache_dir, CODER)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as out:
        out.truncate(CODER.size)
    return settings


def start(settings, machine, log, tmp_path, *, platform="darwin", runtime="llama", saved=None):
    def save(values) -> bool:
        if saved is None:
            return False
        saved.update(values)
        return True

    return server.start(settings, machine, log, runtime=runtime, save=save,
                        root=checkout(tmp_path / "checkout"), platform=platform)


def endings(log: Run) -> list[tuple]:
    return [line for line in log.lines if line[0] in ("done", "error")]


def test_a_chosen_model_is_served_and_checked(chosen, tmp_path):
    log = Run()

    code = start(chosen, Machine(log), log, tmp_path)

    assert code == 0
    url = "http://127.0.0.1:8090/v1"
    assert [line[:2] for line in log.lines if line[0] == "step"] == [
        ("step", "runtime"), ("step", "unit"), ("step", "start"), ("step", "check")]
    assert ("wait", "models", url) in log.lines and ("served", url) in log.lines
    assert endings(log) == [("done", url, CODER.key)]


@pytest.mark.parametrize("model", [None, "qwen3-coder-30b-a3b", "/nowhere/own.gguf"])
def test_no_model_chosen_or_downloaded_is_refused_before_anything(model, tmp_path):
    log = Run()

    code = start(Settings(llm_server_model=model), Machine(log), log, tmp_path)

    assert code == 2
    [(kind, message, detail, where)] = log.lines
    assert (kind, message, where) == ("error", server.NOT_READY, None)
    assert detail  # which: not chosen, not downloaded, or no file there


def test_no_installer_is_refused_before_anything(chosen, tmp_path):
    log = Run()

    code = server.start(chosen, Machine(log), log, save=lambda values: True, root=tmp_path,
                        platform="darwin")

    assert code == 2 and log.kinds() == ["error"]


def test_llama_cpp_missing_is_downloaded_with_its_progress(chosen, tmp_path):
    log = Run()

    code = start(chosen, Machine(log, llama=None), log, tmp_path, platform="linux")

    assert code == 0
    assert log.of("progress") == [(5, 10), (10, 10)]
    runtime = [line[2] for line in log.lines if line[:2] == ("step", "runtime")]
    assert runtime[0].startswith("Installing llama.cpp build")
    assert runtime[1] == f"llama.cpp: {chosen.cache_dir / 'llama.cpp' / 'llama-server'}"


def test_llama_cpp_missing_on_a_mac_with_homebrew(chosen, tmp_path):
    log = Run()

    code = start(chosen, Machine(log, llama=None, brew="/opt/homebrew/bin/brew"), log, tmp_path)

    assert code == 0
    assert log.of("script")[0] == (["brew", "install", "llama.cpp"],)
    assert ("step", "runtime", "llama.cpp: /opt/homebrew/bin/llama-server") in log.lines


def test_an_install_that_fails_stops_before_the_unit(chosen, tmp_path):
    log = Run()

    code = start(chosen, Machine(log, llama=None, brew="/b/brew", codes={"brew": 1}), log,
                 tmp_path)

    assert code == 1
    assert endings(log) == [("error", "llama.cpp could not be installed.",
                             "brew install llama.cpp did not finish", None)]
    assert [argv for (argv,) in log.of("script")] == [["brew", "install", "llama.cpp"]]


def test_llama_cpp_installed_where_keryx_does_not_look_is_said(chosen, tmp_path):
    log = Run()
    machine = Machine(log, llama=None, brew="/b/brew")
    machine.run_script = lambda argv: 0  # installed, but not on PATH

    assert start(chosen, machine, log, tmp_path) == 1
    assert "cannot be found" in endings(log)[0][1]


def test_a_server_that_does_not_come_up_removes_its_unit_first(chosen, tmp_path):
    log = Run()

    code = start(chosen, Machine(log, wait="could not reach 127.0.0.1"), log, tmp_path)

    assert code == 1
    script = str(tmp_path / "checkout" / "scripts" / "install-launchd.sh")
    assert [argv for (argv,) in log.of("script")] == [
        [script, "--llm"], [script, "--llm", "--uninstall"]]
    assert log.kinds()[-1] == "error" and log.kinds()[-2] == "script"
    [(_, message, detail, where)] = endings(log)
    assert message == "The model server did not come up, so its service was removed."
    assert detail.startswith("could not reach 127.0.0.1") and server.REMOVED in detail
    assert where == chosen.state_dir / "logs" / "keryx-llm.err.log"


def test_a_server_serving_another_model_is_not_ours(chosen, tmp_path):
    log = Run()

    code = start(chosen, Machine(log, served="someone-elses"), log, tmp_path)

    assert code == 1
    assert log.of("script")[-1][0][-1] == "--uninstall"
    [(_, message, detail, _log)] = endings(log)
    assert message == ("The model server serves someone-elses, not qwen3-coder-30b-a3b, so its "
                       "service was removed.")
    assert detail.startswith("http://127.0.0.1:8090/v1; ")


def test_a_unit_that_cannot_be_removed_says_so(chosen, tmp_path):
    log = Run()
    machine = Machine(log, wait="no answer")
    calls = []

    def run(argv):
        calls.append(argv)
        return 4 if argv[-1] == "--uninstall" else 0

    machine.run_script = run

    assert start(chosen, machine, log, tmp_path) == 1
    assert endings(log)[0][1].endswith("and its service could not be removed.")
    assert "could not be removed: install-launchd.sh --llm --uninstall exited with 4" in \
        endings(log)[0][2]


# --- --runtime ollama ----------------------------------------------------------------------


def test_ollama_pulls_checks_and_only_then_saves(tmp_path):
    log, saved = Run(), {}
    settings = Settings(llm_server_model=ENTRY.key)  # Ollama pulls its own copy
    machine = Machine(log, ollama="/usr/local/bin/ollama", listed=[ENTRY.ollama])

    code = start(settings, machine, log, tmp_path, runtime="ollama", saved=saved)

    assert code == 0
    assert log.of("script") == [(["ollama", "pull", ENTRY.ollama],)]
    assert saved == server.ollama_values(ENTRY)
    assert endings(log) == [("done", "http://127.0.0.1:11434/v1", ENTRY.ollama)]


@pytest.mark.parametrize(
    ("model", "ollama", "codes", "listed", "code", "says"),
    [("/own.gguf", "/o", {}, (), 2, "Choose and download a model first."),
     (ENTRY.key, None, {}, (), 1, "Ollama is not installed."),
     (ENTRY.key, "/o", {"ollama": 1}, (), 1, f"Ollama could not pull {ENTRY.title}."),
     (ENTRY.key, "/o", {}, ("other",), 1, f"Ollama does not list {ENTRY.ollama}.")],
)
def test_ollama_that_cannot_serve_it_saves_nothing(tmp_path, model, ollama, codes, listed,
                                                    code, says):
    log, saved = Run(), {}
    machine = Machine(log, ollama=ollama, codes=codes, listed=listed)

    assert start(Settings(llm_server_model=model), machine, log, tmp_path, runtime="ollama",
                 saved=saved) == code
    assert saved == {} and endings(log)[0][1].startswith(says)


def test_ollama_whose_settings_are_refused_ends_without_done(tmp_path):
    log = Run()
    machine = Machine(log, ollama="/o", listed=[ENTRY.ollama])

    assert start(Settings(llm_server_model=ENTRY.key), machine, log, tmp_path,
                 runtime="ollama", saved=None) == 1
    assert not log.of("done")


def test_the_machine_asks_through_each_module_when_asked(monkeypatch, tmp_path):
    from keryx.localmodels import runtimes, servers, use

    monkeypatch.setattr(runtimes, "llama_server", lambda cache: "/x/llama-server")
    monkeypatch.setattr(voice, "run_detached", lambda argv: 7)
    monkeypatch.setattr(servers, "wait_for", lambda endpoint, kind, timeout: "late")
    monkeypatch.setattr(use, "probe", lambda url: (True, "m"))
    fetched = []
    monkeypatch.setattr(runtimes, "install_llama_cpp",
                        lambda cache, machine, *, get: fetched.append(get) or "/y")

    machine = server.machine()

    assert machine.llama_server(tmp_path) == "/x/llama-server"
    assert machine.run_script(["a"]) == 7
    assert machine.wait_for_server(Endpoint.parse("http://127.0.0.1:1"), "models", 1) == "late"
    assert machine.served("http://127.0.0.1:1/v1") == (True, "m")
    assert machine.download_llama_cpp(tmp_path, MACHINE, lambda done, total: None) == "/y"
    assert fetched and fetched[0].keywords["progress"] is not None


def test_a_unit_with_no_installer_left_cannot_be_removed(tmp_path):
    said = server.remove_unit(lambda argv: pytest.fail("nothing to run"), root=tmp_path,
                              platform="darwin")

    assert said.startswith("its service could not be removed: scripts/install-launchd.sh")


def test_the_terminal_hears_the_steps_and_the_ending_but_no_progress_bar():
    lines = []
    steps = server.TextSteps(lambda text, err=False: lines.append((text, err)))

    with steps.step("unit", "Installing the model server's service"):
        steps.progress(1, 2)
    steps.done("http://127.0.0.1:8090/v1", "m")

    assert lines == [("Installing the model server's service…", False),
                     ("the model server is running at http://127.0.0.1:8090/v1, serving m; "
                      "`keryx restart` to have Keryx take it up", False)]


@pytest.mark.parametrize("where", ["wait", "served"])
def test_a_cancel_while_the_server_loads_or_is_checked_removes_its_unit(chosen, tmp_path,
                                                                        where):
    log = Run()
    machine = Machine(log, **{where: KeyboardInterrupt()})

    with pytest.raises(KeyboardInterrupt):
        start(chosen, machine, log, tmp_path)

    script = str(tmp_path / "checkout" / "scripts" / "install-launchd.sh")
    assert [argv for (argv,) in log.of("script")] == [
        [script, "--llm"], [script, "--llm", "--uninstall"]]
    assert not endings(log)  # the cancel is the caller's to tell


def test_a_cancel_whose_removal_fails_is_still_a_cancel(chosen, tmp_path):
    log = Run()
    machine = Machine(log, wait=KeyboardInterrupt())

    def run(argv):
        if argv[-1] == "--uninstall":
            raise OSError("gone")
        return 0

    machine.run_script = run

    with pytest.raises(KeyboardInterrupt):
        start(chosen, machine, log, tmp_path)


def test_ollama_over_an_installed_llama_server_removes_its_unit(tmp_path):
    log, saved = Run(), {}
    machine = Machine(log, ollama="/o", listed=[ENTRY.ollama], unit=True)

    code = start(Settings(llm_server_model=ENTRY.key), machine, log, tmp_path,
                 runtime="ollama", saved=saved)

    assert code == 0 and saved == server.ollama_values(ENTRY)
    script = str(tmp_path / "checkout" / "scripts" / "install-launchd.sh")
    assert log.of("script") == [(["ollama", "pull", ENTRY.ollama],),
                                ([script, "--llm", "--uninstall"],)]
    assert log.kinds()[-1] == "done"


def test_ollama_whose_old_unit_will_not_go_says_so_and_still_ends_done(tmp_path):
    log = Run()
    machine = Machine(log, ollama="/o", listed=[ENTRY.ollama], unit=True,
                      codes={"install-launchd.sh": 3})

    assert start(Settings(llm_server_model=ENTRY.key), machine, log, tmp_path,
                 runtime="ollama", saved={}) == 0
    assert any("could not be removed" in line[0] for line in log.of("warn"))


def test_retire_llama_runs_only_when_there_is_a_unit(tmp_path):
    root = checkout(tmp_path / "checkout")
    assert server.retire_llama(lambda argv: pytest.fail("no unit"), installed=False,
                               root=root, platform="darwin") is None
    assert server.retire_llama(lambda argv: 0, installed=True, root=root,
                               platform="darwin") == server.REMOVED


ON_OLLAMA = {"local_agent_base_url": "http://127.0.0.1:11434/v1",
             "local_agent_model": ENTRY.ollama}


def test_ollama_already_serving_is_started_again_from_its_own_model(tmp_path):
    log, saved = Run(), {}
    machine = Machine(log, ollama="/o", listed=[ENTRY.ollama])

    code = start(Settings(**ON_OLLAMA), machine, log, tmp_path, runtime="ollama", saved=saved)

    assert code == 0 and saved == server.ollama_values(ENTRY)


def test_llama_while_ollama_serves_the_model_says_so(tmp_path):
    log = Run()

    code = start(Settings(**ON_OLLAMA), Machine(log), log, tmp_path)

    assert code == 2
    [(kind, message, detail, _)] = log.lines
    assert kind == "error" and message == (
        f"Ollama serves {ENTRY.ollama}; there is no llama.cpp server to start.")
    assert "keryx models use NAME" in detail
