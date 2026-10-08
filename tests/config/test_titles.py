"""Every setting's display title: what the Mac app shows beside a control, in place of the
environment name a person types at `keryx config set`."""

import re

import pytest

from keryx.config.settings import Settings, env_var_name, field_title, setting

#: Acronyms and product names a title keeps as their owners write them; any other word in
#: capitals is an environment name leaking through. SID is Twilio's, as its console labels it.
ACRONYMS = frozenset({
    "OpenAI", "API", "PIN", "Twilio", "URL", "SMS", "Slack", "Gmail", "Google", "Codex",
    "Claude", "Realtime", "VAD", "MCP", "GGUF", "Kokoro", "llama.cpp", "Ollama", "ID", "USD",
    "TTS", "STT", "SSH", "macOS", "SID", "LLM",
})
#: Proper nouns that are not acronyms: capitalised mid-title because they are names.
PROPER_NOUNS = frozenset({"Anthropic", "Cloudflare", "GitHub", "Keryx", "OAuth"})
#: The ones the owner's request (and the review after it) named, word for word.
NAMED = {
    "OPENAI_API_KEY": "OpenAI API key",
    "OPENAI_REALTIME_MODEL": "Realtime model",
    "ASSISTANT_NAME": "Assistant name",
    "SUBAGENT_MAX_BUDGET_USD": "Budget per task (USD)",
    "AGENT_SERVER_PORT": "Agent model server port",
    "AGENTS_ENABLED": "Enabled agents",
    "APPROVAL_ESCALATE_SECONDS": "Ring after (seconds)",
    # The label Twilio's console uses, which the owner copies the value from.
    "TWILIO_ACCOUNT_SID": "Twilio account SID",
}
TITLES = {name: field_title(name) for name in Settings.model_fields}


def _words(title: str) -> list[str]:
    return [word.strip("()") for word in title.split()]


def test_the_titles_the_owner_named_are_the_ones_used():
    by_key = {env_var_name(name): title for name, title in TITLES.items()}

    assert {key: by_key[key] for key in NAMED} == NAMED


def test_every_setting_has_a_short_title_of_its_own():
    for name, title in TITLES.items():
        assert title.strip() == title and title, name
        assert len(title) <= 32, title
        assert title != env_var_name(name), title
        assert not re.search(r"[.,:;!?]$", title), title


def test_no_two_settings_share_a_title():
    seen: dict[str, str] = {}
    for name, title in TITLES.items():
        assert title.lower() not in seen, f"{name} and {seen.get(title.lower())}: {title}"
        seen[title.lower()] = name


@pytest.mark.parametrize("name", list(TITLES))
def test_a_title_is_in_sentence_case(name):
    title = TITLES[name]
    first, *rest = _words(title)

    assert first[0].isupper(), title
    for word in [first, *rest]:
        if word.isupper() and len(word) > 1:
            assert word in ACRONYMS, f"{title}: {word} is not a known acronym"
    for word in rest:
        if any(letter.isupper() for letter in word):
            assert word.removesuffix("s") in ACRONYMS | PROPER_NOUNS, f"{title}: {word}"


def test_a_setting_cannot_be_declared_without_a_title():
    with pytest.raises(TypeError):
        setting(1, "A number.", group="voice", advanced=False)  # type: ignore[call-arg]
    with pytest.raises(AssertionError):
        setting(1, "A number.", title=" ", group="voice", advanced=False)
