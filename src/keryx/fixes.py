"""The actions the Mac app can take for a problem: one id each, which it maps to a button.

`doctor --json` gives every check a `fix`, and `setup plan --json` every step and each of
its checks: one of these ids, or null when the app has nothing to offer (or nothing is
wrong). The app never shows a shell command, so this is how a check says what would mend it;
the command a terminal runs is the check's `hint`. The set is closed — an id outside it is
a broken promise to the app's decoder — and this module is the only place it is written down.

- `signin:<name>` — one of `keryx auth login`'s sign-ins (`SIGNINS`).
- `secret:<KEY>` / `setting:<KEY>` — a setting, by its key as `config list --json` spells
  it: `secret:` for one kept in `secrets.toml`, `setting:` for any other.
- `plugin:<name>` — a plugin in `plugins.PLUGINS`; `integration:<id>` — an account in
  `integrations --json`.
- The rest are screens of their own (`ACTIONS`): the PIN, the background service, the
  model server started or a model chosen, the voice here or its speech models downloaded,
  the Twilio account, its webhooks, the tunnel, and the migration from Jarvis.
"""

from keryx.config import env_var_name, field_for
from keryx.config.settings import is_secret

#: The sign-ins `keryx auth login NAME --json` drives.
SIGNINS = ("claude", "codex", "gmail", "google-workspace")
#: The ids that take no argument.
ACTIONS = frozenset({
    "pin", "service:install", "models:start", "models:choose", "voice:here", "voice:download",
    "phone:account", "phone:webhooks", "phone:tunnel", "migrate",
})


def signin(name: str) -> str:
    return _checked(f"signin:{name}")


def secret(key: str) -> str:
    return _checked(f"secret:{key}")


def setting(key: str) -> str:
    return _checked(f"setting:{key}")


def plugin(name: str) -> str:
    return _checked(f"plugin:{name}")


def integration(name: str) -> str:
    return _checked(f"integration:{name}")


def _checked(fix: str) -> str:
    """`fix`, or ValueError: an id is made only through here, so a typo fails a test."""
    if not is_valid(fix):
        raise ValueError(f"{fix!r} is not an action the app knows")
    return fix


def is_valid(fix: str | None) -> bool:
    """Whether `fix` is null or one of the ids the app maps to a button."""
    if fix is None or fix in ACTIONS:
        return True
    kind, _, arg = fix.partition(":")
    if kind == "signin":
        return arg in SIGNINS
    if kind in ("secret", "setting"):
        field = field_for(arg)
        # The PIN is no setting (`config set KERYX_PIN` is refused): its action is `pin`.
        return (field is not None and field != "pin" and env_var_name(field) == arg
                and is_secret(field) == (kind == "secret"))
    if kind == "plugin":
        from keryx.plugins import PLUGINS

        return arg in PLUGINS
    if kind == "integration":
        from keryx.integrations.accounts import INTEGRATIONS

        return arg in INTEGRATIONS
    return False
