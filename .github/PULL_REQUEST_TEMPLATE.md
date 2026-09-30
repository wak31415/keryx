**What this changes, and why**

<!-- The why matters more than the what — the diff already says what. If it changes a
ruling in CLAUDE.md or SECURITY.md, say which one and amend it in the same PR. -->

**Checklist**

- [ ] `uv run pytest -q` is green, with no new warnings
- [ ] `uv run ruff check src tests` is clean
- [ ] New behaviour has a test; a bug fix has one that fails without the fix
- [ ] No network, hardware or real subagent in any test — fakes behind the `Protocol`s
- [ ] Conventional commit subject (`feat:` / `fix:` / `chore:` / `docs:` / `ci:`)
- [ ] Docs updated where they would otherwise be wrong (README, `docs/configuration.md` via `python -m keryx.config.reference`, CLAUDE.md)

**Anything a reviewer should look at twice**

<!-- Widening `approvals/policy.py`, touching the PIN gate, the restart flow or the
announce-or-text paths deserves a sentence here saying why it is safe. -->
