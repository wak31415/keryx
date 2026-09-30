# Setting up Keryx, for a coding agent

You are setting up Keryx for the person at this keyboard. Everything is a command that
prints JSON or exits with a code; nothing needs a terminal UI. Settings live in
`{home}` (`config.toml`, and `secrets.toml` at 0600). Never read, print or paste a secret.

1. **See what is there.** `keryx config list --json`: every setting, whether it is set,
   where from, whether it is secret. `keryx doctor --json` says what is missing or failing,
   grouped by `section`.
2. **Set what you know.** `keryx config set KEY VALUE [KEY VALUE …]` for plain settings.
   A secret never goes on the command line: `keryx config set OPENAI_API_KEY --from-env VAR`
   (a variable already in your environment) or `… --stdin`. If you do not have a secret,
   ask the person to run `keryx setup`, which asks for it hidden; do not ask them to paste
   it to you. If `doctor` fails its `storage` check, files from before the XDG layout are
   still about: ask the person to run `keryx migrate`, which stops the service for it.
3. **Sign-ins.** `keryx auth status --json` lists each one. For a coding agent,
   `keryx auth login claude|codex` runs its own login on the terminal. For Gmail,
   `keryx auth login gmail --client-file PATH` prints a link: relay it; the person
   approves it on any device and gives you the address their browser lands on
   (`http://localhost:1/?…`); finish with
   `keryx auth login gmail --callback-url 'THAT ADDRESS'`. The person has to make the
   Google Cloud client first (the steps: `{google_guide}`).
4. **Plugins, if they want them.** `keryx plugins --json` lists the four optional voice
   tools — Slack, email, billing, cluster stats — each on or off, and why one is refused.
   `keryx plugins install NAME [--set KEY=VALUE …]` turns one on (`cluster_stats` takes
   `--cluster HOST=PARTITION`, for a host `keryx plugins hosts --json` shows with a
   ControlMaster); its secret is never a `--set`, but `keryx config set KEY --stdin` or
   `keryx auth login gmail`. Settings from before plugins: `keryx plugins install
   --from-settings`.
   Filing bug reports and feature requests about Keryx as GitHub issues is not yours to
   turn on: it is theirs, in `keryx setup` (Issue reports), and you never set
   `ISSUE_REPORTING` or `ISSUE_REPO`.
5. **About them, with their permission.** Ask before you read their folders. If they agree,
   draft one summary per project into `{projects}/<name>.md` — written to be heard, at
   most {max_brief} characters each and {max_total} in all, and nothing from `.env` files,
   keys or credential stores. A repository's own `.keryx-brief.md` wins over yours.
   Then a few standing facts, one per line: `keryx memory seed --file - --json`.
6. **Check.** `keryx doctor --json`; exit 0 means nothing stops Keryx from starting.
7. **Hand over** what only the person can do, by saying: "run `keryx setup`". It walks
   only what is left — browser sign-ins, choosing the phone PIN (never set it yourself),
   and approving the Twilio webhook.

Exit codes: 0 done, 1 refused or failing (the message says why), 2 a wrong command line.
