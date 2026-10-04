"""The Local models section: the agent and the voice on this machine, or on a server."""

from keryx.agents.base import RunResult
from keryx.config.store import ConfigStore
from keryx.endpoints import Probe
from keryx.localmodels.catalog import VOICES, by_key
from keryx.localmodels.download import DownloadError, model_path
from keryx.localmodels.hardware import Hardware
from keryx.localmodels.runtimes import s2s_install_argv
from keryx.setup import local_models

from .fakes import DEFAULT

CODER = by_key("qwen3-coder-30b-a3b")


def scripts(world) -> list[list[str]]:
    return [argv for kind, argv in (c[:2] for c in world.calls if c[0] == "script")]


def test_a_model_on_this_machine_is_downloaded_served_and_tried(make_ctx, world):
    ctx = make_ctx([
        ("local agent's model run", "here"),
        ("voice on a call", "openai"),
        ("Which model?", DEFAULT),  # the recommendation: the tested one fits a 5090
        ("do the work when you do not name an agent", True),
    ])

    local_models.run_section(ctx)

    assert ctx.ui.done(), ctx.ui.answers
    settings = ctx.refresh()
    assert settings.llm_server_model == CODER.key
    assert settings.local_agent_base_url == "http://127.0.0.1:8090/v1"
    assert settings.local_agent_model == CODER.key
    assert settings.local_agent_api == "anthropic-messages"
    assert settings.agent_backend == "local" and "local" in settings.enabled_agents
    assert ("download", CODER.url) in world.calls
    assert ctx.ui.progressed[-1] == CODER.size
    [installer] = scripts(world)
    assert installer[0].endswith(("install-systemd.sh", "install-launchd.sh"))
    assert installer[1:] == ["--llm"]
    assert ("wait", "models", "http://127.0.0.1:8090/v1") in world.calls
    assert ("smoke", "local") in world.calls
    assert "the local model ran a task" in ctx.ui.lines("success")
    recommended = next(c for c in ctx.ui.choices["Which model?"] if c.value == CODER.key)
    assert "recommended" in recommended.hint and "tested" in recommended.hint
    too_big = next(c for c in ctx.ui.choices["Which model?"] if c.value == "gpt-oss-120b")
    assert too_big.disabled == "needs 70 GB"


def test_a_copy_hugging_face_already_has_is_linked_not_downloaded(make_ctx, world, tmp_path):
    found = tmp_path / "hub" / CODER.file
    found.parent.mkdir()
    found.write_bytes(b"x" * 10)
    world.found_model = found
    ctx = make_ctx([("model run", "here"), ("voice on a call", "openai"), ("Which model?", DEFAULT),
                    ("do the work", False)])

    local_models.run_section(ctx)

    assert not [c for c in world.calls if c[0] == "download"]
    assert model_path(ctx.settings.cache_dir, CODER).read_bytes() == b"x" * 10
    assert any("linked, not copied" in line for line in ctx.ui.lines("success"))


def test_a_disk_too_full_for_the_model_saves_nothing(make_ctx, world):
    world.free = 2 * 10**9
    ctx = make_ctx([("model run", "here"), ("voice on a call", "openai"),
                    ("Which model?", DEFAULT)])

    local_models.run_section(ctx)

    assert "needs 19.6 GB free" in ctx.ui.lines("error")[0]
    assert ctx.refresh().local_agent_base_url is None and not scripts(world)


def test_a_download_that_fails_says_so_and_saves_nothing(make_ctx, world):
    world.download_error = DownloadError("the download stopped (ReadTimeout)")
    ctx = make_ctx([("model run", "here"), ("voice on a call", "openai"),
                    ("Which model?", "gpt-oss-20b")])

    local_models.run_section(ctx)

    assert ctx.ui.lines("error") == ["the download stopped (ReadTimeout)"]
    assert ctx.refresh().llm_server_model is None


def test_the_voice_on_the_same_gpu_keeps_room_for_itself(make_ctx, world):
    world.machine = Hardware("cuda", "RTX 4090", 27.0, 64)
    world.s2s = "/bin/s2s"
    ctx = make_ctx([("model run", "here"), ("voice on a call", "here"),
                    ("Which model?", DEFAULT), ("do the work", False), ("Which voice?", DEFAULT)])

    local_models.run_section(ctx)

    # 25 GB for the tested model and 3 for the voice do not fit in 27: the next one does.
    assert ctx.refresh().llm_server_model == "qwen3.8-27b"
    assert any("kept for the voice server" in line for line in ctx.ui.lines("note"))


def test_ollama_already_here_pulls_the_model_and_serves_it(make_ctx, world):
    world.llama, world.ollama = None, "/usr/local/bin/ollama"
    ctx = make_ctx([("model run", "here"), ("voice on a call", "openai"),
                    ("What should run the model?", "ollama"), ("Which model?", "gpt-oss-20b"),
                    ("do the work", False)])

    local_models.run_section(ctx)

    entry = by_key("gpt-oss-20b")
    assert scripts(world) == [["ollama", "pull", entry.ollama]]
    settings = ctx.refresh()
    assert settings.local_agent_base_url == "http://127.0.0.1:11434/v1"
    assert settings.local_agent_model == entry.ollama and settings.llm_server_model is None
    assert not [c for c in world.calls if c[0] in ("download", "wait")]
    # Ollama has no "a file of my own": that choice is llama.cpp's.
    assert local_models.OWN_FILE not in [c.value for c in ctx.ui.choices["Which model?"]]


def test_no_runtime_installs_llama_cpp(make_ctx, world, monkeypatch):
    world.llama = None
    monkeypatch.setattr(local_models.sys, "platform", "linux")
    ctx = make_ctx([("model run", "here"), ("voice on a call", "openai"),
                    ("install it", True), ("Which model?", "qwen3.5-4b"), ("do the work", False)])

    local_models.run_section(ctx)

    assert ("install_llama_cpp",) in world.calls
    assert ctx.refresh().llm_server_model == "qwen3.5-4b"


def test_declining_llama_cpp_stops_there(make_ctx, world):
    world.llama = None
    ctx = make_ctx([("model run", "here"), ("voice on a call", "openai"), ("install it", False)])

    local_models.run_section(ctx)

    assert ctx.ui.done() and ctx.refresh().local_agent_base_url is None


def test_a_machine_nothing_fits_can_bring_its_own_gguf(make_ctx, world, tmp_path):
    world.machine = Hardware("cpu", None, 0, 4)
    own = tmp_path / "small-model.gguf"
    own.write_bytes(b"gguf")
    ctx = make_ctx([("model run", "here"), ("voice on a call", "openai"),
                    ("Which model?", DEFAULT), ("GGUF file's path", str(own)),
                    ("do the work", False)])

    local_models.run_section(ctx)

    assert any("Nothing in the list fits" in line for line in ctx.ui.lines("warn"))
    settings = ctx.refresh()
    assert settings.llm_server_model == str(own) and settings.local_agent_model == "small-model"


def test_a_model_server_that_does_not_come_up_points_at_its_log(make_ctx, world):
    world.server_problem = {"models": "could not reach 127.0.0.1 (ConnectError)"}
    ctx = make_ctx([("model run", "here"), ("voice on a call", "openai"),
                    ("Which model?", DEFAULT)])

    local_models.run_section(ctx)

    assert "did not come up" in ctx.ui.lines("error")[0]
    assert any("keryx-llm.err.log" in line for line in ctx.ui.lines("note"))
    assert ("smoke", "local") not in world.calls


def test_a_server_elsewhere_is_asked_for_its_models(make_ctx, world):
    ctx = make_ctx([
        ("model run", "elsewhere"), ("voice on a call", "openai"),
        ("server's address", "http://gpu-box:11434"), ("Its key", "lk-remote"),
        ("Which of its models?", "gpt-oss-20b"), ("Which API", "openai-responses"),
        ("do the work", False),
    ])

    local_models.run_section(ctx)

    settings = ctx.refresh()
    assert settings.local_agent_base_url == "http://gpu-box:11434/v1"
    assert settings.local_agent_model == "gpt-oss-20b"
    assert settings.local_agent_api == "openai-responses"
    assert ConfigStore()._secrets()["LOCAL_AGENT_API_KEY"] == "lk-remote"
    assert ("models", "http://gpu-box:11434/v1") in world.calls
    assert ctx.ui.lines("warn") == []  # a bare host name is the LAN


def test_a_public_server_without_a_key_is_warned_about(make_ctx, world):
    world.models = Probe(True, ())
    ctx = make_ctx([
        ("model run", "elsewhere"), ("voice on a call", "openai"),
        ("server's address", "http://llm.example.com"), ("Its key", ""),
        ("model's name", "qwen3"), ("Which API", DEFAULT), ("do the work", False),
    ])

    local_models.run_section(ctx)

    assert len(ctx.ui.lines("warn")) == 2
    assert ctx.refresh().local_agent_model == "qwen3"


def test_a_server_that_does_not_answer_is_kept_only_if_asked(make_ctx, world):
    world.models = Probe(False, problem="could not reach gpu-box (ConnectError)")
    ctx = make_ctx([("model run", "elsewhere"), ("voice on a call", "openai"),
                    ("server's address", "http://gpu-box:11434"), ("Its key", ""),
                    ("Keep it anyway", False)])

    local_models.run_section(ctx)

    assert ctx.refresh().local_agent_base_url is None


def test_a_smoke_that_fails_says_why(make_ctx, world):
    world.smoke_result = RunResult(ok=False, error="the model does not call tools")
    ctx = make_ctx([("model run", "elsewhere"), ("voice on a call", "openai"),
                    ("server's address", "http://gpu-box:11434"), ("Its key", ""),
                    ("Which of its models?", DEFAULT), ("Which API", DEFAULT),
                    ("do the work", False)])

    local_models.run_section(ctx)

    assert "the model does not call tools" in ctx.ui.lines("error")[-1]


def test_the_voice_on_this_machine_is_installed_voiced_and_only_then_used(make_ctx, world):
    ConfigStore().set({"LOCAL_AGENT_BASE_URL": "http://127.0.0.1:8090/v1",
                       "LOCAL_AGENT_MODEL": CODER.key, "ASSISTANT_NAME": "Jarvis"})
    world.ports = {8765: 8766}  # something else has speech-to-speech's own port
    ctx = make_ctx([("model run", "keep"), ("voice on a call", "here"),
                    ("Which voice?", DEFAULT)])

    local_models.run_section(ctx)

    assert scripts(world)[0] == s2s_install_argv()
    assert scripts(world)[1][1:] == ["--voice"]
    settings = ctx.refresh()
    assert settings.voice_server_voice == "bm_george"  # Jarvis's
    assert settings.voice_server_port == 8766
    assert settings.voice_base_url == "http://127.0.0.1:8766/v1"
    assert ("wait", "realtime", "http://127.0.0.1:8766/v1") in world.calls
    assert [c.value for c in ctx.ui.choices["Which voice?"]] == [name for name, _ in VOICES]


def test_a_voice_server_that_does_not_come_up_is_not_made_the_voice(make_ctx, world):
    ConfigStore().set({"LOCAL_AGENT_BASE_URL": "http://127.0.0.1:8090/v1",
                       "LOCAL_AGENT_MODEL": CODER.key})
    world.s2s = "/bin/s2s"
    world.server_problem = {"realtime": "could not open 127.0.0.1's Realtime socket"}
    ctx = make_ctx([("model run", "keep"), ("voice on a call", "here"),
                    ("Which voice?", "af_bella")])

    local_models.run_section(ctx)

    settings = ctx.refresh()
    assert settings.voice_base_url is None and settings.voice_server_voice == "af_bella"
    assert any("keryx-voice.err.log" in line for line in ctx.ui.lines("note"))


def test_the_voice_needs_a_local_model_for_its_words(make_ctx, world):
    ctx = make_ctx([("model run", "none"), ("voice on a call", "here")])

    local_models.run_section(ctx)

    assert "takes its words from a local model" in ctx.ui.lines("error")[0]


def test_too_little_disk_for_the_voice_server(make_ctx, world):
    ConfigStore().set({"LOCAL_AGENT_BASE_URL": "http://127.0.0.1:8090/v1",
                       "LOCAL_AGENT_MODEL": CODER.key})
    world.free = 10**9
    ctx = make_ctx([("model run", "keep"), ("voice on a call", "here")])

    local_models.run_section(ctx)

    assert "needs about 5 GB free" in ctx.ui.lines("error")[0] and not scripts(world)


def test_a_voice_server_elsewhere_is_asked_for_a_session(make_ctx, world):
    ctx = make_ctx([("model run", "none"), ("voice on a call", "elsewhere"),
                    ("voice server's address", "http://192.168.1.20:8765"), ("Its key", "")])

    local_models.run_section(ctx)

    assert ctx.refresh().voice_base_url == "http://192.168.1.20:8765/v1"
    assert ("realtime", "http://192.168.1.20:8765/v1") in world.calls
    assert "spoken PIN" in ctx.ui.lines("warn")[0]
    assert any("no web search" in line for line in ctx.ui.lines("note"))


def test_a_voice_server_that_refuses_is_kept_only_if_asked(make_ctx, world):
    world.realtime = "could not open gpu's Realtime socket (ConnectionRefusedError)"
    ctx = make_ctx([("model run", "none"), ("voice on a call", "elsewhere"),
                    ("voice server's address", "http://gpu:8765"), ("Its key", ""),
                    ("Keep it anyway", False)])

    local_models.run_section(ctx)

    assert ctx.refresh().voice_base_url is None


def test_back_to_openai_and_no_local_agent(make_ctx, world):
    ConfigStore().set({"VOICE_BASE_URL": "http://127.0.0.1:8765/v1",
                       "LOCAL_AGENT_BASE_URL": "http://127.0.0.1:8090/v1",
                       "LOCAL_AGENT_MODEL": "m", "LLM_SERVER_MODEL": CODER.key,
                       "AGENTS_ENABLED": "claude,local", "AGENT_BACKEND": "local"})
    ctx = make_ctx([("model run", "none"), ("voice on a call", "openai")])

    local_models.run_section(ctx)

    settings = ctx.refresh()
    assert settings.voice_base_url is None and settings.local_agent_base_url is None
    assert settings.agent_backend == "claude" and settings.enabled_agents == ("claude",)
    assert any("--uninstall" in line for line in ctx.ui.lines("note"))
    assert any("OPENAI_API_KEY" in line for line in ctx.ui.lines("note"))


def test_what_is_set_is_offered_to_keep(make_ctx, world):
    ConfigStore().set({"VOICE_BASE_URL": "http://127.0.0.1:8765/v1",
                       "LOCAL_AGENT_BASE_URL": "http://127.0.0.1:8090/v1",
                       "LOCAL_AGENT_MODEL": "m"})
    ctx = make_ctx([("model run", DEFAULT), ("voice on a call", DEFAULT)])

    local_models.run_section(ctx)

    assert ctx.ui.done() and world.calls == []
    assert ctx.ui.choices["Where should the local agent's model run?"][0].value == "keep"


def test_a_model_already_downloaded_is_marked_and_not_fetched_again(make_ctx, world):
    ctx = make_ctx([("model run", "here"), ("voice on a call", "openai"),
                    ("Which model?", DEFAULT), ("do the work", False)])
    path = model_path(ctx.settings.cache_dir, CODER)
    path.parent.mkdir(parents=True)
    with path.open("wb") as sparse:  # the full size, without writing 18 GB
        sparse.truncate(CODER.size)

    local_models.run_section(ctx)

    choice = next(c for c in ctx.ui.choices["Which model?"] if c.value == CODER.key)
    assert "downloaded" in choice.hint
    assert not [c for c in world.calls if c[0] == "download"]
    assert f"{CODER.title} is already downloaded" in ctx.ui.lines("success")


def test_ollama_here_but_llama_cpp_chosen_on_a_mac_with_homebrew(make_ctx, world, monkeypatch):
    world.llama, world.ollama = None, "/opt/homebrew/bin/ollama"
    monkeypatch.setattr(local_models.sys, "platform", "darwin")
    monkeypatch.setattr(local_models, "which", lambda name: f"/opt/homebrew/bin/{name}")
    ctx = make_ctx([("model run", "here"), ("voice on a call", "openai"),
                    ("What should run the model?", "llama"), ("install it", True),
                    ("Which model?", "qwen3.5-4b"), ("do the work", False)])

    local_models.run_section(ctx)

    assert scripts(world)[0] == ["brew", "install", "llama.cpp"]
    assert ctx.refresh().llm_server_model == "qwen3.5-4b"


def test_a_failed_install_or_pull_stops_with_the_reason(make_ctx, world, monkeypatch):
    world.llama = None

    def broken(cache, machine):
        raise RuntimeError("llama.cpp has no release build for this system")

    monkeypatch.setattr(local_models.sys, "platform", "linux")
    ctx = make_ctx([("model run", "here"), ("voice on a call", "openai"), ("install it", True)])
    ctx.probes.install_llama_cpp = broken

    local_models.run_section(ctx)

    assert "no release build" in ctx.ui.lines("error")[0]

    world.ollama, world.script_code = "/bin/ollama", 1
    ctx = make_ctx([("model run", "here"), ("voice on a call", "openai"),
                    ("What should run the model?", "ollama"), ("Which model?", "gpt-oss-20b")])
    local_models.run_section(ctx)
    assert ctx.ui.lines("error") == ["ollama pull did not finish"]


def test_a_failed_brew_install_or_unit_install_is_said(make_ctx, world, monkeypatch):
    world.llama, world.script_code = None, 3
    monkeypatch.setattr(local_models.sys, "platform", "darwin")
    monkeypatch.setattr(local_models, "which", lambda name: "/opt/homebrew/bin/brew")
    ctx = make_ctx([("model run", "here"), ("voice on a call", "openai"), ("install it", True)])
    local_models.run_section(ctx)
    assert ctx.ui.lines("error") == ["brew install llama.cpp did not finish"]

    world.llama = "/bin/llama-server"
    monkeypatch.setattr(local_models.sys, "platform", "linux")
    ctx = make_ctx([("model run", "here"), ("voice on a call", "openai"),
                    ("Which model?", "qwen3.5-4b")])
    local_models.run_section(ctx)
    assert ctx.ui.lines("error") == ["install-systemd.sh --llm exited with 3"]


def test_no_installer_beside_the_code_is_said(make_ctx, world, monkeypatch, tmp_path):
    monkeypatch.setattr(local_models, "repo_root", lambda: tmp_path)
    ctx = make_ctx([("model run", "here"), ("voice on a call", "openai"),
                    ("Which model?", "qwen3.5-4b")])

    local_models.run_section(ctx)

    assert "is not here" in ctx.ui.lines("error")[0]


def test_the_gguf_question_wants_a_gguf_that_is_there(tmp_path):
    there = tmp_path / "m.gguf"
    there.write_bytes(b"")
    assert local_models._gguf(str(there)) is None
    assert local_models._gguf(str(tmp_path / "gone.gguf")) == "There is no file there."
    assert local_models._gguf(str(tmp_path / "m.bin")) == "A .gguf file."
    assert local_models._address("gpu-box") is not None
    assert local_models._address("http://x") is None
    assert local_models._not_blank(" ") is not None


def test_ollama_with_nothing_that_fits_offers_nothing(make_ctx, world):
    world.llama, world.ollama = None, "/bin/ollama"
    world.machine = Hardware("cpu", None, 0, 4)
    ctx = make_ctx([("model run", "here"), ("voice on a call", "openai"),
                    ("What should run the model?", "ollama")])

    local_models.run_section(ctx)

    assert ctx.ui.done() and ctx.refresh().local_agent_base_url is None


def test_a_voice_server_kept_although_it_did_not_answer(make_ctx, world):
    world.realtime = "could not open gpu's Realtime socket"
    ConfigStore().set({"OPENAI_API_KEY": "sk-x"})
    ctx = make_ctx([("model run", "none"), ("voice on a call", "elsewhere"),
                    ("voice server's address", "https://voice.example.com"), ("Its key", "vk"),
                    ("Keep it anyway", True)])

    local_models.run_section(ctx)

    assert ctx.refresh().voice_base_url == "https://voice.example.com/v1"
    assert ConfigStore()._secrets()["VOICE_API_KEY"] == "vk"
    assert not any("web search" in line for line in ctx.ui.lines("note"))


def test_the_voice_unit_that_will_not_install_saves_no_voice(make_ctx, world):
    ConfigStore().set({"LOCAL_AGENT_BASE_URL": "http://127.0.0.1:8090/v1",
                       "LOCAL_AGENT_MODEL": CODER.key, "VOICE_SERVER_VOICE": "bf_emma"})
    world.script_code = 1
    ctx = make_ctx([("model run", "keep"), ("voice on a call", "here")])

    local_models.run_section(ctx)

    assert ctx.ui.lines("error") == ["the install did not finish"]

    world.s2s = "/bin/s2s"
    ctx = make_ctx([("model run", "keep"), ("voice on a call", "here"), ("Which voice?", DEFAULT)])
    local_models.run_section(ctx)
    assert ctx.ui.lines("error")[0].endswith("--voice exited with 1")
    assert ctx.refresh().voice_server_voice == "bf_emma"  # the one chosen before is offered
    assert ctx.refresh().voice_base_url is None


def test_the_voice_already_ours_keeps_its_port(make_ctx, world):
    ConfigStore().set({"LOCAL_AGENT_BASE_URL": "http://127.0.0.1:8090/v1",
                       "LOCAL_AGENT_MODEL": CODER.key, "VOICE_SERVER_PORT": 18765,
                       "VOICE_BASE_URL": "http://127.0.0.1:18765/v1"})
    world.s2s = "/bin/s2s"
    world.ports = {18765: 18766}  # it is ours that is listening there
    ctx = make_ctx([("model run", "keep"), ("voice on a call", "here"), ("Which voice?", DEFAULT)])

    local_models.run_section(ctx)

    assert ctx.refresh().voice_base_url == "http://127.0.0.1:18765/v1"


def test_no_local_agent_when_there_is_none_changes_nothing(make_ctx, world):
    ctx = make_ctx([("model run", "none"), ("voice on a call", "openai")])

    local_models.run_section(ctx)

    assert ConfigStore().stored() == {} and world.calls == []
