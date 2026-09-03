# Contributing

Thanks for looking. Jarvis is a single-tenant personal service rather than a library, so
"contributing" here mostly means running it yourself and sending back what broke.

## Releases

There is no package index in this story. Jarvis is installed from a clone, so a release is
a tag and a set of notes, not an upload.

1. Bump `version` in `pyproject.toml` by hand. Semantic versioning, and the only public
   contract it describes is the CLI plus `.env`: a removed or renamed setting or command is
   a major bump, a new one is a minor bump, everything else is a patch.
2. Add the entry to `CHANGELOG.md` under the new version, with the date.
3. Commit (`chore: release vX.Y.Z`), tag it `vX.Y.Z`, and push both.
4. Cut a GitHub Release from the tag, with the `CHANGELOG.md` entry as its body.

**Nothing is published to PyPI or any other index**, deliberately — see the README's
*Names* note. `pyproject.toml` carries the `Private :: Do Not Upload` classifier so that an
accidental `uv publish` is refused by the index rather than quietly succeeding.

## Dependencies and their licences

Every direct dependency carries a lower bound at the version `uv.lock` pins — the version
it is actually tested against — and an upper bound at the next release that may break it.
For the 0.x projects that is the next *minor*, because that is where a 0.x puts its
breaking changes. `claude-agent-sdk` is held to a single minor deliberately: it is young,
it moves fast, and every task in Jarvis runs through it.

Dependabot opens weekly PRs for `uv` (manifest and lock together, which is what CI installs
from) and for the GitHub Actions themselves. CI is the gate: a bump that fails `uv sync
--locked`, ruff or the suite does not land.

**Licence review** (2026-09-02, 68 distributions on Linux plus 11 more that only resolve on
macOS). Everything is permissive — MIT, BSD, Apache-2.0, PSF, ISC — with three exceptions
worth knowing about:

| Package | Licence | Why it is fine |
|---|---|---|
| `soxr` | **LGPL-2.1-or-later** | The only copyleft dependency, and a direct one: it is the resampler, and it is native code. Jarvis imports it as an ordinary installed library — nothing is vendored, nothing is statically linked, and no combined binary is distributed — so the LGPL's relink condition is satisfied by pip being able to replace it. Do not vendor it into a bundle without revisiting this. |
| `certifi` | MPL-2.0 | Weak, file-level copyleft on an unmodified dependency. Nothing here modifies it. |
| `tqdm` | MPL-2.0 AND MIT | Same, and macOS-only — it arrives under `openwakeword`. |

`onnxruntime` (MIT), `openwakeword` (Apache-2.0) and `sounddevice` (MIT) were checked
specifically because they pull native code; all three are permissive. Redo this review when
a direct dependency is added, and record the result here.
