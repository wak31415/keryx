---
name: Bug report
about: Something does not work the way the README says it does
labels: bug
---

**What happened**

<!-- What you did, and what Keryx did instead. If it was a phone call, the words matter. -->

**What you expected**

**Reproducing it**

<!-- The smallest sequence that shows it. `uv run keryx serve --fake-agents` costs no
tokens and reproduces most things that are not provider-specific. -->

**`keryx doctor`**

<!-- Paste `uv run keryx doctor`. It never prints a secret — it says "set" or
"not set" — so it is safe to include, and it answers half of the questions that would
otherwise be asked here. -->

```
```

**Environment**

- OS and version:
- `uv run keryx --version` or the commit:

**Anything from the logs**

<!-- `~/.local/state/keryx/logs/keryx.log` (`STATE_DIR/logs`). Please check for phone numbers and keys before pasting;
numbers are masked to their last four digits in the log, but transcripts are not. -->

> **Do not open a security issue here.** Use the Security tab → Report a vulnerability.
