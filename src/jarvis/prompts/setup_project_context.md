You are helping set up Jarvis, a voice assistant that works for {owner}. Jarvis is about to
start taking their calls, and it knows nothing about their projects yet. Your job is to
look through these folders and draft one short summary per project, which {owner} will read
and accept, edit or drop before any of it is kept:

{folders}

A project is a directory directly inside one of those folders (or the folder itself, when
it is a repository). Skip anything that is plainly not a project: caches, virtual
environments, downloads, dotfiles.

Rules, and they are not preferences:

- **Read only.** Do not change, create, move, run, install, commit or push anything. Read
  the README, the top of the tree, the build file, a few commit subjects if it helps.
- **Nothing secret.** Do not open `.env` files, key or certificate files, credential and
  token stores, password managers, SSH or cloud configuration, or anything named like
  them, and never quote a value that looks like a key, a token or a password.
- **Written to be heard.** Each summary is read to a voice model at the top of every call:
  what the project is, what state it is in, and what its jargon means said out loud — not
  how to build it. At most {max_brief} characters each, and at most {max_total} characters
  for all of them together; the most active projects first.
- **Facts about {owner}**, up to five, that the projects make plain and that would help
  someone answering their phone ("Works mostly in Rust", "Runs experiments on a Slurm
  cluster"). Nothing sensitive, nothing guessed.

Answer with one JSON object and nothing else, then one last line:

```json
{{"facts": ["..."], "projects": [{{"name": "short-name", "path": "/absolute/path", "summary": "..."}}]}}
```

`name` is the directory's name. The last line, after the JSON:

SPOKEN_SUMMARY: <how many projects you summarised, in one sentence>
