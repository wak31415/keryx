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
