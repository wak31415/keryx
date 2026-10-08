"""The Local models section's voice on this machine, through the same `voice.here` as
`keryx voice use here`: never installed while nothing serves its words, and never left
restarting a server that did not come up."""

from keryx.config.store import ConfigStore
from keryx.localmodels import voice, voice_llm
from keryx.localmodels.catalog import by_key
from keryx.localmodels.download import model_path
from keryx.localmodels.hardware import Hardware
from keryx.setup import local_models

from .fakes import DEFAULT

CODER = by_key("qwen3-coder-30b-a3b")
WORDS = {"LOCAL_AGENT_BASE_URL": "http://127.0.0.1:8090/v1", "LOCAL_AGENT_MODEL": CODER.key}


def scripts(world) -> list[list[str]]:
    return [argv for kind, argv in (c[:2] for c in world.calls if c[0] == "script")]


def test_the_voice_waits_for_its_words_server(make_ctx, world):
    ConfigStore().set(WORDS)
    world.words = False
    ctx = make_ctx([("model run", "keep"), ("voice on a call", "here")])

    local_models.run_section(ctx)

    assert ctx.ui.lines("error") == [voice.NO_WORDS_SERVER]
    assert ("words", "http://127.0.0.1:8090/v1") in world.calls
    assert scripts(world) == [] and ctx.refresh().voice_base_url is None


def test_a_voice_server_that_does_not_come_up_has_its_unit_removed(make_ctx, world):
    ConfigStore().set(WORDS)
    world.s2s = "/bin/s2s"
    world.server_problem = {"realtime": "no answer"}
    ctx = make_ctx([("model run", "keep"), ("voice on a call", "here"),
                    ("Which voice?", "af_bella")])

    local_models.run_section(ctx)

    assert [argv[1:] for argv in scripts(world)] == [["--voice"], ["--voice", "--uninstall"]]
    assert ctx.ui.lines("error")[0].endswith("so its service was removed.")
    assert any(voice.REMOVED in line for line in ctx.ui.lines("note"))


def test_the_units_are_asked_of_the_fake_world_never_the_service_manager(make_ctx, world):
    world.units = {"llm"}

    probes = world.probes()

    assert probes.unit_installed("llm") is True and probes.unit_installed("voice") is False
    assert ("unit_installed", "llm") in world.calls


def test_the_voice_downloads_its_speech_models_before_its_unit(make_ctx, world):
    ConfigStore().set(WORDS)
    world.s2s, world.speech_ready = "/bin/s2s", False
    ctx = make_ctx([("model run", "keep"), ("voice on a call", "here"),
                    ("Which voice?", "af_bella")])

    local_models.run_section(ctx)

    kinds = [call[0] for call in world.calls]
    assert kinds.index("fetch") < kinds.index("script")
    assert ctx.refresh().voice_base_url == "http://127.0.0.1:8765/v1"


def test_a_download_that_stops_installs_no_voice_unit(make_ctx, world):
    ConfigStore().set(WORDS)
    world.s2s, world.speech_ready = "/bin/s2s", False
    world.fetched = "a/one could not be downloaded: OSError: full"
    ctx = make_ctx([("model run", "keep"), ("voice on a call", "here"),
                    ("Which voice?", "af_bella")])

    local_models.run_section(ctx)

    assert ctx.ui.lines("error") == ["The voice's speech models could not be downloaded."]
    assert world.fetched in ctx.ui.lines("note") and scripts(world) == []


def test_the_voice_here_is_not_restarted_over_a_live_call(make_ctx, world):
    ConfigStore().set({**WORDS, "VOICE_BASE_URL": "http://127.0.0.1:8765/v1"})
    world.s2s, world.live = "/bin/s2s", 1
    ctx = make_ctx([("model run", "keep"), ("voice on a call", "here")])

    local_models.run_section(ctx)

    assert ctx.ui.lines("error") == [voice.LIVE_CALL]
    assert scripts(world) == []


# --- the voice's words: asked before the voice, from what the agent's model leaves -----------

NINE, SMALL = by_key("qwen3.5-9b"), by_key("qwen3.5-4b")
#: The agent's model served here, so its size is known: 25 GB.
CODER_HERE = {"AGENT_SERVER_MODEL": CODER.key, **WORDS}
MAC_64 = Hardware("metal", "Apple silicon", 0, 64)  # 48 GB for models
WORDS_QUESTION = "voice's words"


def installers(world) -> list[str]:
    return [argv[1] for argv in scripts(world) if len(argv) > 1]


def test_a_voice_here_gets_a_model_of_its_own_where_one_fits(make_ctx, world):
    """25 + 9 + 3 = 37 of 48: the owner's rule offers the 9b first, and it is brought up —
    downloaded, its unit, its check — before the voice that reads it."""
    ConfigStore().set(CODER_HERE)
    world.machine, world.s2s, world.serving = MAC_64, "/bin/s2s", NINE.key
    ctx = make_ctx([("model run", "keep"), ("voice on a call", "here"),
                    (WORDS_QUESTION, DEFAULT), ("Which voice?", DEFAULT)])

    local_models.run_section(ctx)

    assert ctx.ui.done(), ctx.ui.answers
    choices = {c.value: c for c in ctx.ui.choices["Where should the voice's words come from?"]}
    assert "recommended" in choices[NINE.key].hint and choices[SMALL.key].disabled is None
    assert choices["shared"].disabled is None
    assert choices["gpt-oss-120b"].disabled == "needs 98 GB with the agent's and the speech models"
    assert ("download", NINE.url) in world.calls  # with the wizard's own progress bar
    assert installers(world) == ["--voice-llm", "--voice"]  # the words first
    assert ("served", "http://127.0.0.1:8091/v1") in world.calls
    settings = ctx.refresh()
    assert (settings.voice_llm, settings.voice_llm_model) == ("here", NINE.key)
    assert settings.voice_mode == "here"
    assert any("on a server of its own" in line for line in ctx.ui.lines("success"))


def test_the_owner_may_keep_the_voice_on_the_agents_model(make_ctx, world):
    ConfigStore().set(CODER_HERE)
    world.machine, world.s2s = MAC_64, "/bin/s2s"
    ctx = make_ctx([("model run", "keep"), ("voice on a call", "here"),
                    (WORDS_QUESTION, "shared"), ("Which voice?", DEFAULT)])

    local_models.run_section(ctx)

    assert installers(world) == ["--voice"]
    settings = ctx.refresh()
    assert settings.voice_llm == "shared" and settings.voice_mode == "here"
    # Written by this first set-up, once the voice answered: no longer the default.
    assert ctx.store.source_of("VOICE_LLM", settings) == "config.toml"


def test_a_tight_machine_keeps_the_voices_words_on_the_agents_model_without_asking(
    make_ctx, world
):
    """A 5090: 25 + 6 + 3 = 34 > 31.8, so the agent's model is all there is to offer."""
    ConfigStore().set(CODER_HERE)
    world.s2s = "/bin/s2s"
    ctx = make_ctx([("model run", "keep"), ("voice on a call", "here"),
                    ("Which voice?", DEFAULT)])

    local_models.run_section(ctx)

    assert ctx.ui.done(), ctx.ui.answers
    assert any("No model of the voice's own fits" in line for line in ctx.ui.lines("note"))
    assert installers(world) == ["--voice"] and ctx.refresh().voice_llm == "shared"


def test_with_no_local_agent_the_voice_runs_on_a_model_of_its_own(make_ctx, world):
    """Nothing to share: only the models that fit alone with the speech models (12 GB)."""
    world.machine = Hardware("metal", "Apple silicon", 0, 16)
    world.s2s, world.serving = "/bin/s2s", NINE.key
    ctx = make_ctx([("model run", "none"), ("voice on a call", "here"),
                    (WORDS_QUESTION, DEFAULT), ("Which voice?", DEFAULT)])

    local_models.run_section(ctx)

    offered = [c.value for c in ctx.ui.choices["Where should the voice's words come from?"]
               if c.disabled is None]
    assert offered == [NINE.key, SMALL.key]
    assert ctx.refresh().voice_llm_model == NINE.key


def test_a_voice_model_that_does_not_come_up_sets_up_no_voice(make_ctx, world):
    ConfigStore().set(CODER_HERE)
    world.machine, world.s2s = MAC_64, "/bin/s2s"
    world.server_problem = {"models": "it did not answer in time"}
    ctx = make_ctx([("model run", "keep"), ("voice on a call", "here"),
                    (WORDS_QUESTION, SMALL.key)])

    local_models.run_section(ctx)

    assert installers(world) == ["--voice-llm", "--voice-llm"]  # installed, then removed
    assert any("keryx-voice-llm.err.log" in line for line in ctx.ui.lines("note"))
    settings = ctx.refresh()
    assert settings.voice_llm == "shared" and settings.voice_llm_model is None
    assert settings.voice_mode != "here"


def test_a_voice_model_already_serving_is_kept_and_not_restarted(make_ctx, world):
    ConfigStore().set({**CODER_HERE, "VOICE_LLM": "here", "VOICE_LLM_MODEL": SMALL.key})
    world.machine, world.s2s = MAC_64, "/bin/s2s"
    ctx = make_ctx([("model run", "keep"), ("voice on a call", "here"),
                    (WORDS_QUESTION, DEFAULT), ("Which voice?", DEFAULT)])

    local_models.run_section(ctx)

    choices = ctx.ui.choices["Where should the voice's words come from?"]
    assert choices[0].value == "keep" and "Qwen3.5 4B" in choices[0].label
    assert installers(world) == ["--voice"]
    assert ctx.refresh().voice_llm_model == SMALL.key


def test_choosing_the_agents_model_again_retires_the_voices_own(make_ctx, world):
    """Through `keryx voice llm shared`'s code: saved, and the unit holding the old model
    removed."""
    ConfigStore().set({**CODER_HERE, "VOICE_LLM": "here", "VOICE_LLM_MODEL": SMALL.key})
    world.machine, world.s2s = MAC_64, "/bin/s2s"
    world.units = {"voice_llm"}
    ctx = make_ctx([("model run", "keep"), ("voice on a call", "here"),
                    (WORDS_QUESTION, "shared"), ("Which voice?", DEFAULT)])

    local_models.run_section(ctx)

    assert installers(world)[:1] == ["--voice-llm"]  # its removal
    assert ctx.refresh().voice_llm == "shared"


def test_the_voices_own_model_is_kept_free_when_the_agents_is_chosen(make_ctx, world):
    ConfigStore().set({"VOICE_LLM": "here", "VOICE_LLM_MODEL": NINE.key})
    world.machine = Hardware("metal", "Apple silicon", 0, 48)  # 36: 25 + 9 + 3 = 37
    world.s2s = "/bin/s2s"
    ctx = make_ctx([("model run", "here"), ("voice on a call", "here"),
                    ("Which model?", DEFAULT), ("do the work", False),
                    (WORDS_QUESTION, "keep"), ("Which voice?", DEFAULT)])

    local_models.run_section(ctx)

    assert ctx.refresh().agent_server_model == "qwen3.8-27b"  # 24 + 9 + 3 = 36
    assert any("kept for the voice's own model" in line for line in ctx.ui.lines("note"))


def test_a_download_of_the_voices_model_that_fails_sets_up_nothing(make_ctx, world):
    from keryx.localmodels.download import DownloadError

    ConfigStore().set(CODER_HERE)
    world.machine, world.s2s = MAC_64, "/bin/s2s"
    world.download_error = DownloadError("the download stopped (ReadTimeout)")
    ctx = make_ctx([("model run", "keep"), ("voice on a call", "here"),
                    (WORDS_QUESTION, DEFAULT)])

    local_models.run_section(ctx)

    assert ctx.ui.lines("error") == ["the download stopped (ReadTimeout)"]
    assert scripts(world) == [] and ctx.refresh().voice_llm == "shared"
    assert not model_path(ctx.settings.cache_dir, NINE).exists()
    assert voice_llm.FLAG == "--voice-llm"


def test_choosing_the_model_already_serving_leaves_it_as_it_is(make_ctx, world):
    ConfigStore().set({**CODER_HERE, "VOICE_LLM": "here", "VOICE_LLM_MODEL": SMALL.key})
    world.machine, world.s2s = MAC_64, "/bin/s2s"
    ctx = make_ctx([("model run", "keep"), ("voice on a call", "here"),
                    (WORDS_QUESTION, SMALL.key), ("Which voice?", DEFAULT)])

    local_models.run_section(ctx)

    assert ("voice_llm", "http://127.0.0.1:8091/v1") in world.calls  # asked, not restarted
    assert installers(world) == ["--voice"]


def test_a_switch_of_the_words_is_refused_over_a_live_call_and_the_voice_left_alone(
    make_ctx, world
):
    ConfigStore().set({**CODER_HERE, "VOICE_LLM": "here", "VOICE_LLM_MODEL": SMALL.key,
                       "VOICE_MODE": "here"})
    world.machine, world.s2s, world.live = MAC_64, "/bin/s2s", 1
    world.units = {"voice_llm", "voice"}
    ctx = make_ctx([("model run", "keep"), ("voice on a call", "here"),
                    (WORDS_QUESTION, "shared")])

    local_models.run_section(ctx)

    assert ctx.ui.lines("error") == [voice_llm.LIVE_CALL]
    assert scripts(world) == [] and ctx.refresh().voice_llm == "here"


def test_a_voice_llm_elsewhere_already_chosen_is_offered_to_keep(make_ctx, world):
    ConfigStore().set({**CODER_HERE, "VOICE_LLM": "remote",
                       "VOICE_LLM_BASE_URL": "http://gpu-box:8000/v1",
                       "VOICE_LLM_MODEL": "small"})
    world.machine, world.s2s = MAC_64, "/bin/s2s"
    ctx = make_ctx([("model run", "keep"), ("voice on a call", "here"),
                    (WORDS_QUESTION, DEFAULT), ("Which voice?", DEFAULT)])

    local_models.run_section(ctx)

    keep = ctx.ui.choices["Where should the voice's words come from?"][0]
    assert (keep.value, keep.label) == ("keep", "Keep small at gpu-box")
    assert ctx.refresh().voice_llm == "remote"
