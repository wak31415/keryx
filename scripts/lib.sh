# Shared helpers for the scripts in this directory. Source it; do not run it.

env_value() {
  # env_value NAME [FILE] — the value of NAME in an env file, without surrounding quotes.
  # Deliberately not `source`: the file holds JSON (PROJECTS={"a": "/b"}), and sourcing
  # that under `set -e` is a syntax error at best and arbitrary code at worst.
  local name="$1" file="${2:-.env}" value
  value="$(grep -E "^[[:space:]]*${name}=" "$file" | tail -n 1 | cut -d= -f2-)" || true
  value="${value%\"}"; value="${value#\"}"
  value="${value%\'}"; value="${value#\'}"
  printf '%s' "$value"
}
