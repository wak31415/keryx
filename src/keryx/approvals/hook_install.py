"""`keryx approvals --install-hook`: put the approval bridge's hook into Claude Code.

Two things, both additive and both undone by `--uninstall-hook`: copy the hook script to
`~/.claude/hooks/keryx_approval.py`, and merge its entries into `~/.claude/settings.json`,
which is backed up into `~/.claude/backups/` first. Anyone else's hooks are left alone, and
installing again replaces the hook's own entries rather than adding a second set, which
would hand every prompt to the broker twice.

The hook runs as a copy, never out of a checkout, because a checkout several agents edit at
once would change under a live session mid-prompt.
"""

import json
import shlex
import shutil
from datetime import datetime
from pathlib import Path

from keryx.approvals.broker import MARKER_NAME, STATE_DIR_NAME
from keryx.config.files import claude_config_dir
from keryx.config.settings import SOURCE_ROOT

HOOK_FILE = "keryx_approval.py"
#: The permission prompt is the one that blocks; the rest only ever cancel an escalation, so
#: they get a short timeout and cost one `stat` when nothing is pending.
WANTED = {
    "PermissionRequest": 630,
    "PostToolUse": 15,
    "PermissionDenied": 15,
    "Stop": 15,
    "SessionEnd": 15,
}


def hook_source() -> Path:
    """The canonical hook: packaged beside this module, or `scripts/claude_hooks` in a clone."""
    packaged = Path(__file__).with_name(HOOK_FILE)
    return packaged if packaged.is_file() else SOURCE_ROOT / "scripts" / "claude_hooks" / HOOK_FILE


def hook_commands(target: Path, state_dir: Path) -> tuple[str, str]:
    """The command for `PermissionRequest`, and the one for the events that resolve it.

    `KERYX_STATE_DIR` is always written in, even at the default: the hook never reads the
    configuration, and one that guessed differently from the broker would leave every prompt
    unescalated. The resolving events fire on *every* tool call, so nothing pending must not
    cost a Python interpreter: `sh` costs a millisecond, and drains stdin rather than leaving
    the CLI writing into a closed pipe. Quoted whole, so a path needing quotes of its own
    cannot end the outer ones early.
    """
    marker = state_dir / STATE_DIR_NAME / MARKER_NAME
    raise_command = (f"env KERYX_STATE_DIR={shlex.quote(str(state_dir))} "
                     f"python3 {shlex.quote(str(target))}")
    resolve_command = "sh -c " + shlex.quote(
        f"if [ -e {shlex.quote(str(marker))} ]; then exec {raise_command}; else cat >/dev/null; fi"
    )
    return raise_command, resolve_command


def install_hook(state_dir: Path, *, uninstall: bool = False) -> Path:
    """Install (or remove) the hook; the settings file it changed."""
    claude = claude_config_dir()
    settings_path, hooks_dir = claude / "settings.json", claude / "hooks"
    target = hooks_dir / HOOK_FILE
    hooks_dir.mkdir(parents=True, exist_ok=True)
    if settings_path.is_file():
        backups = claude / "backups"
        backups.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(settings_path, backups / f"settings.json.{stamp}")
        settings = json.loads(settings_path.read_text(encoding="utf-8") or "{}")
    else:
        settings = {}

    target.unlink(missing_ok=True)
    if not uninstall:
        shutil.copyfile(hook_source(), target)
        target.chmod(0o755)

    raise_command, resolve_command = hook_commands(target, state_dir)
    hooks = settings.setdefault("hooks", {})
    for event, timeout in WANTED.items():
        groups = [
            group for group in hooks.get(event, [])
            if not any(HOOK_FILE in str(hook.get("command", ""))
                       for hook in group.get("hooks", []))
        ]
        if not uninstall:
            command = raise_command if event == "PermissionRequest" else resolve_command
            groups.append({"hooks": [{"type": "command", "command": command,
                                      "timeout": timeout}]})
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    if not hooks:
        settings.pop("hooks", None)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return settings_path
