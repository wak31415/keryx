#!/usr/bin/env bash
# Install the approval-bridge hook into the Claude CLI's settings.
#
# Two things, both additive and both reversible with --uninstall: copy
# `scripts/claude_hooks/keryx_approval.py` to ~/.claude/hooks/, and merge the hook
# entries it needs into ~/.claude/settings.json. Existing hooks are left alone; the
# settings file is backed up into ~/.claude/backups/ before it is touched.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${here}/lib.sh"

claude_dir="${CLAUDE_CONFIG_DIR:-${HOME}/.claude}"
settings="${claude_dir}/settings.json"
hooks_dir="${claude_dir}/hooks"
target="${hooks_dir}/keryx_approval.py"
uninstall=0
[[ "${1:-}" == "--uninstall" ]] && uninstall=1
# Where the broker listens: STATE_DIR, as the service resolves it (lib.sh). The hook reads
# KERYX_STATE_DIR and never the configuration, so it is always written into the command.
(( uninstall )) || require_paths
state_dir="${RESOLVED_STATE_DIR:-}"

mkdir -p "${hooks_dir}" "${claude_dir}/backups"
if [[ -f "${settings}" ]]; then
  cp "${settings}" "${claude_dir}/backups/settings.json.$(date +%Y%m%d-%H%M%S)"
else
  echo '{}' > "${settings}"
fi

if (( uninstall )); then
  rm -f "${target}"
else
  install -m 0755 "${here}/claude_hooks/keryx_approval.py" "${target}"
fi

python3 - "${settings}" "${target}" "${uninstall}" "${state_dir%/}" <<'PY'
import json, os, shlex, sys

path, target, uninstall = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
state_dir = sys.argv[4]
marker = os.path.join(state_dir, "approvals", "PENDING")
settings = json.loads(open(path).read() or "{}")
hooks = settings.setdefault("hooks", {})

# The permission prompt is the one that blocks; the rest only ever cancel an escalation,
# so they get a short timeout and cost one `stat` when nothing is pending.
WANTED = {
    "PermissionRequest": 630,
    "PostToolUse": 15,
    "PermissionDenied": 15,
    "Stop": 15,
    "SessionEnd": 15,
}
# Always, even at the default: the hook's own fallback is only for one wired up by hand,
# and a hook that guessed differently from the broker would leave every prompt unescalated.
raise_command = f"env KERYX_STATE_DIR={shlex.quote(state_dir)} python3 {shlex.quote(target)}"
# The resolving events fire on *every* tool call, so the common case — nothing pending —
# must not pay for a Python interpreter. `sh` costs a millisecond, and it drains stdin
# rather than leaving the CLI writing into a closed pipe. Quoted as a whole, so a path that
# needs quoting of its own cannot end the outer quotes early.
resolve_command = "sh -c " + shlex.quote(
    f"if [ -e {shlex.quote(marker)} ]; then exec {raise_command}; else cat >/dev/null; fi"
)

for event, timeout in WANTED.items():
    groups = hooks.setdefault(event, [])
    groups[:] = [
        group for group in groups
        if not any("keryx_approval.py" in str(h.get("command", "")) for h in group.get("hooks", []))
    ]
    if not uninstall:
        command = raise_command if event == "PermissionRequest" else resolve_command
        groups.append({"hooks": [{"type": "command", "command": command, "timeout": timeout}]})
    if not groups:
        hooks.pop(event, None)
if not hooks:
    settings.pop("hooks", None)

open(path, "w").write(json.dumps(settings, indent=2) + "\n")
print(("removed" if uninstall else "installed") + f" the approval hook in {path}")
PY

if (( ! uninstall )); then
  echo "hook script: ${target}"
  echo "turn it off at any time with: uv run keryx approvals --disable"
fi
