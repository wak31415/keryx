"""The accounts Keryx connects to, as `keryx integrations` and the Mac app's Integrations
section show them: one card each, connected or not.

Not a client, unlike the rest of this package: it reads what already says whether each is
connected and connects nothing. The two Google sign-ins are `keryx auth`'s own entries
(`setup.auth.sign_in`), a key is whether the settings hold it (never its value), and GitHub
is `ISSUE_REPORTING` plus `issues.gh_status` — asked only while reports are on, as `doctor`
asks it, so the page costs what `keryx auth status` does without `--smoke`.

Connecting and disconnecting are the commands that exist already (`config set KEY --stdin`,
`config unset`, `auth login`, `auth logout`); `secrets`, `settings` and `signin` say which a
card uses. `why` is for a card with those buttons on it: a sentence, never a command.
`used_by` is read from the plugins' own `needs`, so the two sections cannot disagree.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from keryx import issues, plugins
from keryx.config import Settings, field_for, field_title

GhProbe = Callable[[], "issues.GhStatus"]
State = tuple[bool, str | None]

NOT_SIGNED_IN = "Not signed in."
NO_CLIENT = "Needs a Google OAuth client file first."


@dataclass(frozen=True)
class Integration:
    """One account: what the app calls it, and what its card edits."""

    id: str
    title: str
    #: How a sentence names it: "Connect {called} in Integrations first."
    called: str
    description: str
    #: Secrets kept in `secrets.toml`, set with `keryx config set KEY --stdin`.
    secrets: tuple[str, ...] = ()
    #: Plain settings the card edits, which `config unset` clears to disconnect.
    settings: tuple[str, ...] = ()
    #: The sign-in that connects it: an `auth login` name, or `gh` (its own login).
    signin: str | None = None


INTEGRATIONS: dict[str, Integration] = {
    one.id: one
    for one in (
        Integration(
            "google", "Google for agents", "Google for agents",
            "Gmail and Calendar for every agent's tasks through Google's workspace server, "
            "which Codex needs and Claude has connectors of its own for.",
            settings=("GOOGLE_WORKSPACE_MCP", "USER_GOOGLE_EMAIL"), signin="google-workspace",
        ),
        Integration(
            "gmail", "Gmail", "Gmail",
            "A read-only look at your mailbox, which Email answers reads on a call.",
            signin="gmail",
        ),
        Integration(
            "slack", "Slack", "Slack",
            "The bot that posts what you ask for in writing to your Slack DM.",
            secrets=("SLACK_BOT_TOKEN",),
        ),
        Integration(
            "github", "GitHub", "GitHub",
            "Bugs and feature requests you report on a call, filed as GitHub issues with gh.",
            settings=("ISSUE_REPORTING", "ISSUE_REPO"), signin="gh",
        ),
        Integration(
            "openai-admin", "OpenAI admin key", "the OpenAI admin key",
            "An OpenAI admin key, which Billing reads what you spend on OpenAI with.",
            secrets=("OPENAI_ADMIN_KEY",),
        ),
        Integration(
            "anthropic-admin", "Anthropic admin key", "the Anthropic admin key",
            "An Anthropic admin key, which Billing reads what the agents cost with.",
            secrets=("ANTHROPIC_ADMIN_KEY",),
        ),
    )
}


def _is_set(settings: Settings, key: str) -> bool:
    name = field_for(key)
    assert name is not None, key
    return bool(getattr(settings, name))


def _google(settings: Settings, gh: GhProbe, spec: Integration) -> State:
    from keryx.agents.registry import without_google
    from keryx.setup.auth import sign_in

    if not settings.google_workspace_mcp:
        if without := without_google(settings):
            return False, f"Off, so {' and '.join(without)} tasks have no Gmail or Calendar."
        return False, "Off: Claude's tasks use Claude's own Gmail and Calendar connectors."
    if settings.google_oauth_client() is None:
        return False, NO_CLIENT
    if sign_in("google-workspace", settings)["state"] != "ok":
        return False, NOT_SIGNED_IN
    return True, None


def _gmail(settings: Settings, gh: GhProbe, spec: Integration) -> State:
    from keryx.setup.auth import sign_in

    if sign_in("gmail", settings)["state"] == "ok":
        return True, None
    if settings.google_oauth_client() is None:
        return False, f"Not signed in. {NO_CLIENT}"
    return False, NOT_SIGNED_IN


def _secrets(missing: str) -> Callable[[Settings, GhProbe, Integration], State]:
    def state(settings: Settings, gh: GhProbe, spec: Integration) -> State:
        if all(_is_set(settings, key) for key in spec.secrets):
            return True, None
        return False, missing

    return state


def _github(settings: Settings, gh: GhProbe, spec: Integration) -> State:
    if not settings.issue_reporting:
        return False, "Issue reports are off."
    checkout = settings.checkout
    if checkout is None or not (checkout / issues.SKILL).is_file():
        return False, "There is no Keryx checkout with the report skill in it."
    status = gh()
    if not status.installed:
        return False, "The GitHub command-line tool, gh, is not installed."
    if not status.signed_in:
        return False, "gh is not signed in to GitHub."
    return True, None


_STATES: dict[str, Callable[[Settings, GhProbe, Integration], State]] = {
    "google": _google,
    "gmail": _gmail,
    "slack": _secrets("No bot token yet."),
    "github": _github,
    "openai-admin": _secrets("No admin key yet."),
    "anthropic-admin": _secrets("No admin key yet."),
}


def _state(settings: Settings, name: str, gh_status: GhProbe | None) -> State:
    probe = gh_status if gh_status is not None else issues.gh_status
    return _STATES[name](settings, probe, INTEGRATIONS[name])


def used_by(name: str) -> list[str]:
    """The plugins that read the account `name`, in `PLUGINS` order."""
    return [one.name for one in plugins.PLUGINS.values() if name in one.needs]


def integrations(
    settings: Settings, *, gh_status: GhProbe | None = None, only: Iterable[str] | None = None
) -> list[dict[str, Any]]:
    """Every account (or those in `only`), as the app's cards read them."""
    names = list(INTEGRATIONS) if only is None else list(only)
    rows = []
    for name in names:
        spec = INTEGRATIONS[name]
        connected, why = _state(settings, name, gh_status)
        rows.append({
            "id": spec.id,
            "title": spec.title,
            "description": spec.description,
            "connected": connected,
            "why": why,
            "secrets": [
                {"key": key, "title": field_title(field_for(key) or key),
                 "set": _is_set(settings, key)}
                for key in spec.secrets
            ],
            "settings": list(spec.settings),
            "signin": spec.signin,
            "used_by": used_by(name),
        })
    return rows


def connected(
    settings: Settings, names: Iterable[str], *, gh_status: GhProbe | None = None
) -> dict[str, bool]:
    """Whether each of `names` is connected; nothing else is looked at."""
    return {name: _state(settings, name, gh_status)[0] for name in names}
