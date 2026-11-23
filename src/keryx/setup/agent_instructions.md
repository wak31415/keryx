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
   it to you.
3. **Sign-ins.** `keryx auth status --json` lists each one; Google and Slack are marked
   `optional` there, since Keryx runs without them, and never make it exit 1. Each
   `keryx auth login` takes its own options and refuses another's. For a coding agent,
   `keryx auth login claude|codex` runs its own login on the terminal. Google is one
   sign-in for Gmail, Calendar, Drive, Docs, Sheets and Slides:
   `keryx auth login google --headless` prints a link: relay it; the person approves it
   on any device and gives you the address their browser lands on
   (`http://localhost:1/?…`). That address holds a one-time code, so never put it in a
   command: write it to a file with your file tool, finish with
   `keryx auth login google --callback-url - < THAT_FILE`, and delete the file. If it is
   refused because the account is not the one saved, ask them which they mean and add
   `--account ADDRESS`. Only when the first command
   says there is no Google OAuth client does the person have to make one (the steps:
   `{google_guide}`), which you then pass once with `--client-file PATH`.
   For Slack, `keryx auth login slack` first says the four access levels (what Keryx
   may see there, where it can be set to work from, and at the highest that it can send
   messages as them) and then prints a link and its steps. The level is the person's to
   choose: read them what it printed and ask. Never choose `converse`, `read` or `write`
   for them, since at those any message of theirs to the Keryx app (and one that
   mentions it elsewhere) can start work without the phone PIN, and at `write` Keryx can post in their name; with no
   answer it is `announce`, which the commands here set up (a higher one adds `--access
   LEVEL` to both, and its extra tokens to standard input, one to a line). Relay the link
   and the steps; the
   person creates and installs the Slack app it describes and copies its bot token. That
   token is not yours to see: they run `keryx auth login slack --stdin --email ADDRESS`
   themselves, with the address they use in Slack, and it asks for the token hidden
   (only with the token already in a variable of your environment do you run it:
   `printenv VAR | keryx auth login slack --stdin --email ADDRESS`). It checks the
   token, finds them by that address, turns `slack_report` on and sends them one test
   message; ask whether it arrived. If it could not find them, it says what to run
   instead.
4. **Plugins, if they want them.** `keryx plugins --json` lists the optional voice tools —
   Slack, reading Slack (which needs a workspace at the `read` level), sending Slack
   messages as them (the `write` level, and its own yes), email, reading the
   calendar, adding to it, documents, billing, cluster stats —
   each on or off, and why one is refused.
   `keryx plugins install NAME [--set KEY=VALUE …]` turns one on (`cluster_stats` takes
   `--cluster HOST=PARTITION`, for a host `keryx plugins hosts --json` shows with a
   ControlMaster); its secret is never a `--set`, but `keryx config set KEY --stdin`,
   `keryx auth login google`, or for Slack `keryx auth login slack`, which turns the
   plugin on as well. `keryx auth logout google` ends the Google sign-in, and is theirs
   to ask for.
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
