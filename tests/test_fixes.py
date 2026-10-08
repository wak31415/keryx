"""`keryx.fixes`: the closed set of actions the Mac app maps to a button."""

import pytest

from keryx import fixes, plugins
from keryx.integrations.accounts import INTEGRATIONS


@pytest.mark.parametrize("fix", [
    None, "pin", "service:install", "models:start", "models:choose", "voice:here",
    "voice:download", "phone:account", "phone:webhooks", "phone:tunnel", "migrate",
    "signin:claude", "signin:codex", "signin:gmail", "signin:google-workspace",
    "secret:OPENAI_API_KEY", "secret:TWILIO_AUTH_TOKEN", "setting:OWNER_NAME",
    "setting:PUBLIC_HOST", *(f"plugin:{name}" for name in plugins.PLUGINS),
    *(f"integration:{name}" for name in INTEGRATIONS),
])
def test_the_ids_the_app_knows(fix):
    assert fixes.is_valid(fix)


@pytest.mark.parametrize("fix", [
    "", "doctor --fix", "signin:gh", "signin:", "secret:OWNER_NAME", "setting:OPENAI_API_KEY",
    "setting:owner_name", "setting:NOT_A_SETTING", "setting:KERYX_PIN", "secret:KERYX_PIN",
    "setting:JARVIS_PIN", "plugin:nope", "integration:nope", "models:pull", "keryx setup",
])
def test_anything_else_is_not_one(fix):
    """A key as `config list --json` spells it, a secret only as `secret:`, and never the
    PIN, which has an action of its own."""
    assert not fixes.is_valid(fix)


def test_an_id_is_made_only_through_the_validator():
    assert fixes.secret("OPENAI_API_KEY") == "secret:OPENAI_API_KEY"
    assert fixes.integration("github") == "integration:github"
    for make, arg in ((fixes.setting, "OPENAI_API_KEY"), (fixes.secret, "OWNER_NAME"),
                      (fixes.plugin, "nope"), (fixes.signin, "gh")):
        with pytest.raises(ValueError, match="not an action"):
            make(arg)


def test_the_prefixes_are_exactly_the_ids_that_take_an_argument():
    """`PREFIXES` is what the app's copy is checked against: each takes an argument, and
    nothing else does."""
    examples = {"secret:": "OPENAI_API_KEY", "setting:": "ASSISTANT_NAME",
                "plugin:": "check_billing", "integration:": "slack"}

    assert set(fixes.PREFIXES) == set(examples)
    assert all(fixes.is_valid(prefix + arg) for prefix, arg in examples.items())
    assert not fixes.is_valid("pin:OPENAI_API_KEY")
