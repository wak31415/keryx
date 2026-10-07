"""The Local models section's voice on this machine, through the same `voice.here` as
`keryx voice use here`: never installed while nothing serves its words, and never left
restarting a server that did not come up."""

from keryx.config.store import ConfigStore
from keryx.localmodels import voice
from keryx.localmodels.catalog import by_key
from keryx.setup import local_models

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
    assert voice.REMOVED in ctx.ui.lines("error")[0]


def test_the_units_are_asked_of_the_fake_world_never_the_service_manager(make_ctx, world):
    world.units = {"llm"}

    probes = world.probes()

    assert probes.unit_installed("llm") is True and probes.unit_installed("voice") is False
    assert ("unit_installed", "llm") in world.calls
