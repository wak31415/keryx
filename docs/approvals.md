# Answer Claude Code prompts by phone

If you use Claude Code on the same machine, Jarvis can ring you when a session on your
screen stops to ask you something and you haven't answered within five minutes. It reads
the question out, and you answer on the keypad. Install the hook once:

```bash
scripts/install-claude-hook.sh
```

It copies `scripts/claude_hooks/jarvis_approval.py` into `~/.claude/hooks/` and adds it to
`~/.claude/settings.json`. It keeps a backup and any hooks you already have. Re-run it after
pulling changes to `scripts/claude_hooks/`, because Jarvis ignores an outdated copy.

- Answering at the keyboard always wins.
- Any failure leaves the prompt on your screen as usual. No error ever approves anything.
- Only routine commands can be approved by phone.
- `uv run jarvis approvals` shows what it has asked, and `--disable` turns it off without a
  restart.

It works for Claude Code sessions only, whichever agent Jarvis itself dispatches to
([agents](agents.md)). The
[wiki](https://github.com/wak31415/jarvis-voice-agent/wiki/The-Approval-Bridge) has the
details, and [SECURITY.md](../SECURITY.md) has the threat model.
