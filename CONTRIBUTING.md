# Contributing

Thanks for looking. Keryx is a single-tenant personal service rather than a library, so
"contributing" here mostly means running it yourself and sending back what broke. Issues
about the setup being wrong, or the README lying, are as welcome as code.

Everyone taking part is expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
Security problems do **not** go in an issue — see [SECURITY.md](SECURITY.md).

## Getting set up

```bash
uv sync                            # Python 3.12
uv run pytest -q                   # must be green, with no warnings
uv run ruff check src tests
uv run keryx doctor               # what this machine is still missing
```

You do **not** need an API key or Twilio to run the tests. The suite never
reads your own configuration or data — each test has its own `HOME`, XDG directories,
`KERYX_HOME` and working directory, so neither `~/.config/keryx` nor a `.env` in your
checkout takes part — and never reaches the network: see the fixtures in
`tests/conftest.py`, and do not weaken them.

To run the thing itself without spending agent tokens (every task comes back with a
sample answer):

```bash
uv run keryx serve --demo
uv run keryx loopback --wav sample.wav --out reply.wav
```

## The rules that are not negotiable

**No network, no hardware, no real subagent in a test.** OpenAI, Twilio, Slack, the Claude
Agent SDK and the Codex SDK are all reached through an injectable `Protocol`
with a fake in `tests/`, and a coding agent's SDK is imported only where an agent runs,
never at module scope, so the suite runs with neither installed.

**TDD, and the tests are the contract.** A refactor that needs a test changed beyond its
imports is not a refactor; stop and re-read what the test was protecting.

**Rulings live in `CLAUDE.md`.** It records decisions and *why*, including several that look
like bugs until you know them, and `SECURITY.md` is the threat model. Changing a ruling means
amending the file that holds it in the same change.

**Conventional commits**, with a body that explains the why:

```
fix(server): stop serving the OpenAI schema through the tunnel

<the reasoning, in prose>

Closes #12

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

A subagent Keryx dispatched adds `Keryx-Task: <id>` as well (`Jarvis-Task:` before the
service was renamed), which makes `git log -E --grep '^(Jarvis|Keryx)-Task:'` the record of
what was asked for out loud.

## Writing docs

The README, the wiki, `docs/`, and `SECURITY.md` follow one house style, taken from the
[Google developer documentation style guide](https://developers.google.com/style/highlights):

- **American spelling**: behavior, defense, license.
- **Address the reader as "you"**, and use contractions (it's, don't, can't). `SECURITY.md`
  is the exception: it spells them out. The author's "I" belongs only in the README's
  caption and in `docs/roadmap.md`.
- **Short sentences, one idea each.** Keep the spaced em dash ( — ) as the dash, but use at
  most one pair in a sentence, and prefer a full stop.
- **Numbered lists for steps**, and the condition before the instruction: "If your account
  can send SMS, run …", not "Run … if your account can send SMS".
- **Name the effect, not the identifier.** Keep a function or setting name only when the
  reader types it or sees it.
- **Sentence-case headings**, the serial comma, and link text that says where it goes (never
  "here" or "below").
- **Document the present.** Upgrade steps go in the CHANGELOG, not in how-to pages.

The voice model's own wording (prompts, tool descriptions, the `*_MESSAGE` constants) has
rules of its own in `CLAUDE.md`; this section is about docs people read.

## Releases

There is no package index in this story. Keryx is installed from a clone, so a release is
a tag and a set of notes, not an upload.

1. Bump `version` in `pyproject.toml` by hand. Semantic versioning, and the only public
   contract it describes is the CLI plus the settings: a removed or renamed setting or command is
   a major bump, a new one is a minor bump, everything else is a patch.
2. Add the entry to `CHANGELOG.md` under the new version, with the date.
3. Commit (`chore: release vX.Y.Z`), tag it `vX.Y.Z`, and push both.
4. Cut a GitHub Release from the tag, with the `CHANGELOG.md` entry as its body.

**Nothing is published to PyPI or any other index**, deliberately: this is a
single-tenant service you run from a clone, not a library to depend on. `pyproject.toml`
carries the `Private :: Do Not Upload` classifier so that an accidental `uv publish` is refused by the index rather than quietly succeeding.

## Coverage

```bash
uv run pytest -q --cov --cov-report=term:skip-covered
```

**The floor is 96%, and it is a ratchet rather than a target.** It was last raised on
2026-09-27 (`fail_under` in `pyproject.toml`), to the number actually measured floored to a
whole point, so a rounding wobble does not fail CI while real erosion does. Raise it when the measured number has moved up;
do not lower it to make a branch pass. `pytest --cov` fails below it, and CI prints the
per-module table into the run summary.

The modules that sit below the floor, and why, as of 2026-09-02:

| Module | | What is uncovered |
|---|---|---|
| `skills.py` | 87% | Malformed and unreadable `SKILL.md` files on disk |
| `transports/twilio_ws.py` | 89% | Media-socket error paths that need a half-closed real socket to reach honestly |
| `continuity/recall.py` | 90% | Store failures and empty-result branches |
| `approvals/policy.py` | 91% | Individual denylist entries; the classification itself is covered exhaustively |
| `integrations/cluster.py` | 91% | Parser branches for `sinfo`/`squeue` shapes the fixtures do not contain |
| `approvals/broker.py` | 91% | Socket-level failures (a client that disconnects mid-request) |
| `projects.py` | 91% | `OSError` paths on project discovery |
| `cli.py` | 93% | Argument-parsing edges and the `serve` loop, which is exercised end to end rather than by unit test |

None of them is a gap in a *rule* — the PIN gate, the `reported_at` contract, the
`can_text` gate, the read-only guarantees in `integrations/billing.py` and
`integrations/cluster.py`, and the approval policy's allowlist are each covered by tests
named after them. They are error
paths that need a broken filesystem or a half-open socket to reach honestly. If you are
touching one of these modules, adding the test is welcome.

## Dependencies and their licenses

Every direct dependency carries a lower bound at the version `uv.lock` pins — the version
it is actually tested against — and an upper bound at the next release that may break it.
For the 0.x projects that is the next *minor*, because that is where a 0.x puts its
breaking changes. `claude-agent-sdk` is held to a single minor deliberately: it is young,
it moves fast, and every task in Keryx runs through it.

Dependabot opens weekly PRs for `uv` (manifest and lock together, which is what CI installs
from) and for the GitHub Actions themselves. CI is the gate: a bump that fails `uv sync
--locked`, ruff or the suite does not land.

**License review** (2026-09-02, 68 distributions on Linux; the macOS-only audio packages
left with the wake word on 2026-09-28). Everything is permissive — MIT, BSD, Apache-2.0,
PSF, ISC — with two exceptions
worth knowing about:

| Package | License | Why it is fine |
|---|---|---|
| `soxr` | **LGPL-2.1-or-later** | The only copyleft dependency, and a direct one: it is the resampler, and it is native code. Keryx imports it as an ordinary installed library — nothing is vendored, nothing is statically linked, and no combined binary is distributed — so the LGPL's relink condition is satisfied by pip being able to replace it. Do not vendor it into a bundle without revisiting this. |
| `certifi` | MPL-2.0 | Weak, file-level copyleft on an unmodified dependency. Nothing here modifies it. |

Redo this review when
a direct dependency is added, and record the result here.
