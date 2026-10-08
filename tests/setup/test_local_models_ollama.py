"""The Local models section moving the agent onto Ollama: the llama.cpp server it leaves
behind is removed, never left restarting a model that is no longer set."""

from keryx.config.store import ConfigStore
from keryx.localmodels.catalog import by_key
from keryx.setup import local_models

ENTRY = by_key("gpt-oss-20b")


def test_ollama_over_a_llama_server_removes_its_unit(make_ctx, world):
    ConfigStore().set({"AGENT_SERVER_MODEL": "qwen3.5-4b"})
    world.llama, world.ollama = None, "/usr/local/bin/ollama"
    ctx = make_ctx([("model run", "here"), ("voice on a call", "openai"),
                    ("What should run the model?", "ollama"), ("Which model?", ENTRY.key),
                    ("do the work", False)])

    local_models.run_section(ctx)

    ran = [argv for kind, argv in (c[:2] for c in world.calls if c[0] == "script")]
    assert ran[0] == ["ollama", "pull", ENTRY.ollama]
    assert ran[1][1:] == ["--llm", "--uninstall"]
    assert ctx.refresh().agent_server_model is None
    assert any("Ollama serves the model now" in line for line in ctx.ui.lines("note"))


def test_ollama_with_no_llama_server_before_removes_nothing(make_ctx, world):
    world.llama, world.ollama = None, "/usr/local/bin/ollama"
    ctx = make_ctx([("model run", "here"), ("voice on a call", "openai"),
                    ("What should run the model?", "ollama"), ("Which model?", ENTRY.key),
                    ("do the work", False)])

    local_models.run_section(ctx)

    assert [c[1] for c in world.calls if c[0] == "script"] == [["ollama", "pull", ENTRY.ollama]]
