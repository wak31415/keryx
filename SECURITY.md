# Security policy

Keryx answers a phone number and passes what it hears to a coding agent that runs with your
full user access. Treat a bug in any of these as a security issue, not an ordinary one:

- the PIN check
- the Twilio signature check
- the stream token that authenticates a call's audio
- the approval bridge's allowlist
- the signed report links

The first half of this file says how to report a problem and what is in scope. The second
half is the [threat model](#threat-model): what each defense is for, and which risks are
accepted on purpose.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: open the repository's **Security** tab and
select **Report a vulnerability**. This opens a private advisory that only you and the
maintainer can see. It is the only channel this project publishes. There is no security
email address, and a security problem should never be opened as a public issue.

A useful report says:

- what an attacker needs to start with: a phone number, a shell on the host, or nothing;
- the smallest sequence of steps that reproduces the problem;
- what the attacker gains.

A proof of concept is welcome but never required.

You can expect an acknowledgment within a week, and an honest answer about whether and
when the problem will be fixed. Keryx is a personal project maintained by one person in
their own time. There is no bounty, no SLA, and no security team. If a report has no answer
after two weeks, assume it was missed and comment on the advisory again.

Please allow a reasonable window before you disclose publicly. Ninety days is more than
enough for anything here. If a fix lands sooner, you may disclose sooner.

## Supported versions

Only the `main` branch is supported. There are no maintenance branches and no backports:
fixes land on `main`, and you update by pulling. Tagged releases mark points in that
history. They are not supported separately.

| Version | Supported |
|---|---|
| `main` | ✅ |
| tagged releases | ❌ — update to `main` |

## Scope

### In scope

Anything that lets a caller who has not given the PIN **act**: change anything, leave
anything behind, or reach a coding agent. In particular:

- A way to authorize a phone call without the PIN.
- The memory update running for a call that did not give the PIN. The memory updater is a
  coding agent with full permissions whose whole input is the call's transcript. Reading
  the memory aloud before the PIN is intended. Rewriting it is not.
- An announcement that tells a call more than it could hear at the greeting.
- `recall`, or any similar unbounded search, answering without the PIN.
- Anything private that the briefing does not already contain being read out without the
  PIN.
- A call being treated as one Keryx placed to your number when Keryx did not place it, or
  placed it to a number that is not yours (see
  [Calls Keryx places itself](#calls-keryx-places-itself)).
- **Anything that sets or replaces a PIN while one already exists**, from the phone or from
  a coding agent, or that reads an enrolled PIN back out anywhere.
- With `BRIEFING_BEFORE_PIN=false`, anything of yours reaching a call that has not given
  the PIN.
- A way for untrusted content, such as an email, a web page, or a file, to reach a coding
  agent without the PIN.
- A coding agent's credential appearing on its command line, in a log, or in a spoken
  error. Credentials are passed only in the environment, and a refused key is redacted
  when it is quoted back.
- Your Anthropic or OpenAI credentials reaching a model server of your own (the `local`
  agent's or the voice server's), or the running service changing where a model is
  (see [Models of your own](#models-of-your-own)).
- Text that reaches a Claude Code session on your machine (an issue, a pull request, a web
  page) and gets the approval bridge to ring you about one command while running another,
  or to run a command its policy should never have offered.

### Out of scope

These are the design, not flaws in it:

- **The coding agents have your full access.** Claude runs with
  `permission_mode="bypassPermissions"` and Codex with
  `--dangerously-bypass-approvals-and-sandbox`. So anyone who gives the PIN, and any content
  that reaches a task you authorized, effectively has a shell as you. Prompt injection into
  an authorized task is inherent. The PIN gates dispatch the same way for both agents, and
  naming an agent on a call is an argument to that one gated tool, not a way around it. A
  way to reach that access *without* the PIN is in scope, on either agent.
- **Attacks that already need your account on the host.** Someone who can read
  `~/.config/keryx` or write `~/.claude/settings.json` is already you. Deleting
  `KERYX_HOME/pin` is one example: it reopens enrollment for the next caller rather than
  changing the PIN in place (see
  [Setting the first PIN](#setting-the-first-pin-on-the-first-call)).
- **Vulnerabilities in dependencies**, unless the way Keryx uses one makes a harmless bug
  exploitable. Report those upstream. Dependabot watches the versions here.
- **Missing rate limits or hardening on `/health`**, which returns `ok`, a session count,
  and nothing else.

## Threat model

### What the PIN protects against

**The PIN defends against someone on the phone, not someone on your machine.**

Caller ID can be spoofed. `ALLOWED_CALLERS` keeps strangers from reaching the voice model at
all, but anyone who spoofs an allowed number does reach it, and the public source code makes
that cheap. So the allowlist is not authentication. The PIN is, and its one job is to stop a
caller who has spoofed an allowed number.

The PIN is **not** a defense against a compromised machine. Anyone who can read your files
can read `~/.config/keryx/secrets.toml` and `~/.config/keryx/pin`, which hold the API keys,
the Twilio token, the PIN, and everything else. Against that attacker the PIN is worthless,
and they can read Keryx's directories directly.

### Reading before the PIN

Because the PIN cannot protect your files from someone who has the machine, a call may
**read before the PIN**. Gating reads protected nothing from that attacker, and it cost you
a keypad entry on every ordinary call. The line the PIN draws is between **reading and
acting**. On an inbound call before the PIN, you can hear what Keryx knows, and nothing the
caller says or does changes anything or outlasts the call.

- **Nothing is read out until a PIN exists.** The trade assumes there is a PIN to enter. On
  a machine that has never had one, a call cannot authenticate at all, so without this rule
  an allowed caller would hear your memory on every call, indefinitely. Until a PIN is set,
  by `keryx setup` or on the first call, Keryx withholds the briefing whatever
  `BRIEFING_BEFORE_PIN` says, and refuses the read-only tools that cover the same material.
- **A call starts knowing what Keryx knows.** From the greeting, the call has the results
  you have not heard yet, what Keryx remembers about you (`memory.md`), your project names
  and briefs, and your installed skills. This is `BRIEFING_BEFORE_PIN`, on by default. The
  four voice tools that read the same material back (`list_tasks`, `get_task_status`,
  `get_task_result`, and `list_projects`) also answer without the PIN. The accepted cost is
  that someone who spoofs an allowed number hears all of it. Set `BRIEFING_BEFORE_PIN=false`
  to withhold it until the PIN. That also puts the four tools back behind the PIN.
- **`recall` still needs the PIN, on purpose.** The briefing is bounded and curated: you
  can read it with `keryx memory` and prune it, and it is the same whatever the caller
  says. `recall` is an unbounded search that the caller steers, over every transcript Keryx
  has written. That is far more exposure, and it is the one thing on the phone a spoofer
  could mine.
- **Nothing that acts works without the PIN.** Every voice tool that hands work to a coding
  agent, writes something down, or changes anything answers `pin_required` first. Of the
  built-in tools, only `web_search`, `submit_pin`, and `end_session` answer at every trust
  level whatever the settings say. A test walks every registered tool, so a new tool is
  gated unless someone adds it to one of those lists on purpose. Of the plugins,
  `check_billing` and `cluster_stats` set `needs_pin=False`, because they read a number and
  change nothing. `send_to_slack` and `check_email` keep the default and need the PIN.
- **An announcement tells a call nothing it could not already hear.** A result that finishes
  during a call is announced under the same rule as the briefing. Restart confirmations and
  approval escalations go only to a call that has given the PIN or that Keryx placed to your
  number. A call that has proved neither never counts as you having been told: the
  call-back is still placed, and the approval still rings.
- **Nothing outlasts the call.** **Keryx does not update its memory from the call.** A call
  that heard the memory read out still cannot rewrite it. This is the invariant that matters
  most: hearing what Keryx believes about you is recoverable, and an edit to it is not. The
  call can mark as heard only the results it read out itself. It cannot arrange a call-back
  or a call-back note, and it cannot send anything in writing. Its transcript is kept on
  disk as a record of what was tried, but it is marked as unauthorized, and `recall` never
  reads it back to a model.
- **A spoken PIN is never written down.** Transcript lines are redacted as they are
  written, whether the PIN was said as digits or as words. Everything that reads a
  transcript back to a model redacts it again, because older logs may still contain it. No
  log line contains the PIN.

### Setting the first PIN, on the first call

Until a PIN is set, the phone is of no use to you: every dispatch is refused, and nothing of
yours is read out. The PIN is the one setup step that the phone itself would otherwise be
unable to do. So **the first call may enter a PIN on the keypad**, and that is the only way
a PIN is ever set from the phone.

**Enrollment works only once.** While no PIN exists anywhere (none in the environment and no
`KERYX_HOME/pin` file), a caller enters six to eight digits and then enters them again. When
the two entries match, that is the PIN. Once a PIN exists, enrollment is closed, and the
file write itself closes it rather than a separate check: Keryx creates `KERYX_HOME/pin` with
`O_CREAT | O_EXCL`, so the kernel refuses a second write. On purpose, no voice tool and no
`keryx config set` can change an enrolled PIN. Only `keryx setup` at a terminal can.

`keryx setup`, run at a terminal, is you at the keyboard. It writes a first PIN the same
way. It replaces an existing PIN only after you confirm twice and type the new digits twice,
and it swaps the file by an atomic rename, so a failure partway through leaves the old PIN in
place. A running Keryx keeps the PIN it started with until `keryx restart`.

**The accepted risk: whoever calls first sets the PIN.** The window is the few minutes
between starting Keryx and making the first call. It closes on first use, and nobody is
likely to spoof your number inside it. The caller also has to be on `ALLOWED_CALLERS` to
reach the voice model at all. If you would rather not take that risk, choose the PIN in
`keryx setup` before the first call, and there is no window. `keryx doctor` reports where
the active PIN came from, never what it is.

**The digits are entered on the keypad, never spoken.** Saying a PIN aloud cannot enroll
one, because transcription mishears digits, and a wrong PIN that nothing can change is the
worst outcome here. Keypad digits take the same path as any other keypad PIN, and they never
reach the model, the transcript, or a log line. After three unusable entries, the call stops
accepting a PIN. This is a cap, not a lockout: nothing has been set, so there is nothing to
guess, and the next call can still enroll.

**The PIN is stored as digits, not as a hash.** It is in a 0600 file inside the 0700
`~/.config/keryx` directory. Any hash of six digits can be reversed in microseconds, so
hashing would suggest a protection that does not exist. The file sits beside
`secrets.toml`, which is just as private.

**Where the PIN is kept does not depend on a setting.** It is in the configuration
directory, which only the `KERYX_HOME` environment variable can move. It is not in
`DATA_DIR`, which a setting can move. When the PIN was kept at `DATA_DIR/pin`, pointing
`DATA_DIR` at an empty directory found no PIN, and no PIN means enrollment is open. A PIN
still in an old `~/.jarvis` directory also keeps enrollment closed, until `keryx migrate`
moves it.

**What a coding agent can and cannot do to the PIN.** A coding agent runs as you with full
permissions, so it can delete `KERYX_HOME/pin`, as it can edit any of your files. It cannot
rewrite an enrolled PIN, because of `O_EXCL`, so the PIN cannot be swapped silently.
Deleting the file locks you out and opens enrollment for whoever calls next. That is loud,
and `keryx doctor` shows it, which is better than a PIN quietly becoming someone else's. If
this worries you, set `KERYX_PIN` in the service's own environment (the unit's
`Environment=` line), which always takes precedence over the file.

**Why Keryx has no PIN-setting script it can call.** Any sudoers rule that lets Keryx run a
setter without a password also lets a coding agent run it. That would give back exactly the
ability the `O_EXCL` write removes. A root-owned setter that you run with your own sudo
password is fine. What matters is that Keryx has no privileged way to run it.

### Calls Keryx places itself

Call-backs, restart confirmations, and approval escalations are outbound calls, and they are
different in kind. Keryx dialed a number you configured, so whoever answers is holding that
phone.

The proof is the single-use token Keryx creates for the call's audio stream. The token
records that Keryx placed the call and which number it dialed. A call whose token says so,
and whose dialed number is one of yours, starts with that much proved. "One of yours" means
`ALLOWED_CALLERS`, plus `OWNER_NUMBER` if it is set to a number not already in that list.
Keryx has a single owner, so a second allowed number is your second phone. Nothing else
proves possession: not Twilio's `From` or `To`, which the caller's carrier supplies, and so
not any inbound call, however it presents itself.

On such a call, beyond what any call may hear, you can:

- answer the question a task came back with (`send_followup`);
- arrange a call-back **on the same number**;
- mark any result as heard;
- answer a waiting approval on the keypad. This is safe because the approval allowlist
  already limits what a key can run, its denylist still takes precedence, and
  `keryx approvals --disable` overrides everything.

You cannot start new work, search your past calls, send to Slack, restart Keryx, or call a
number chosen during the call. Those need the PIN. This level still matters with
`BRIEFING_BEFORE_PIN=false`: it is what lets a call-back read you the result it called about.

**Voicemail is the remaining risk, and it is handled.** An answering machine can pick up an
outbound call and hear a result read out. Before acting on anything *said* on such a call,
the assistant asks for one keypress, which a machine cannot make. The keypad is still the
only way to answer an approval. Detecting answering machines directly (Twilio's
`machine_detection`) is tracked in issue #50 and not built.

**Entering the PIN on such a call.** Two things can want the keypad at once: an approval
menu that has been read out, and the PIN, which would give the call full access. Press **`*`**
to switch the keypad to the PIN, then enter the PIN as usual. Press `*` again to switch back
to the menu. `*` is never part of a PIN and never a menu option, so it cannot be mistaken for
either. A wrong PIN leaves the keypad where it is, so you can try again. Saying the digits
aloud also works, because `submit_pin` answers at every trust level.

### PIN and brute force

Caller ID can be spoofed, so anyone who knows an allowed number can reach a phone session.
From there, the PIN is the only thing between them and a coding agent with your access. This
section describes what protects the PIN, and what that protection costs you.

**Per call.** Three wrong PINs, spoken or entered on the keypad, end the call. After that,
the call accepts no PIN, not even the right one. A spoken PIN is reduced to its digits
before it is compared, because people say a PIN with dashes, spaces, or words between the
digits. Anything that is not six to eight digits after that, such as half a PIN that the
line cut off or a `#` pressed too early, is refused without being compared. It does not
count here or below: it cannot be the PIN, so it is not a guess.

**Across calls.** A per-call limit alone would not be enough. At three guesses a call, a
6-digit PIN takes about 167,000 calls, or about a day and a half at twenty calls in
parallel. So every wrong PIN on every call is also counted in `DATA_DIR/pin-failures.json`.
The count survives a restart. It is kept with the data rather than the state, because
people treat state as disposable, and losing the file would give a guesser a fresh budget.

| Setting | Default | Meaning |
|---|---|---|
| `PIN_FAILURE_LIMIT` | 10 | wrong PINs, across all calls, before PIN entry locks |
| `PIN_FAILURE_WINDOW_HOURS` | 24 | how long a wrong PIN is remembered |
| `PIN_LOCKOUT_MINUTES` | 60 | how long PIN entry stays locked |

- While PIN entry is locked, **every PIN is refused before it is compared, the right one
  included.** The assistant says so in one sentence and ends the call.
- **Nothing resets the count early**: not the lock ending, and not a right PIN. Past the
  limit, each further wrong PIN inside the window locks entry again. An attack that keeps
  going gets one guess an hour, about 24 a day. That puts a 6-digit PIN decades away and an
  8-digit PIN out of reach. If a right PIN reset the count, each of your own calls would give
  an attacker ten fresh guesses.
- **You are told once**, when entry first locks, and again only if it is still locking a
  full window later. The alert is spoken into a call that has already given the PIN (never
  the call that is guessing), posted to Slack if the `send_to_slack` plugin is on, and
  texted only if `SMS_ENABLED` is on.
- **An unreadable count file is treated as a lock that began when Keryx found it.** That
  lock lasts one lockout period, is written back so a restart cannot extend it, and is never
  longer. An unreadable file is the one state where "nobody has guessed" and "someone has
  nearly used up the budget" look the same, and treating it as a fresh start would make a
  corrupted file a free reset. A *missing* file is a fresh start. If the file cannot be
  written, Keryx keeps counting in memory.
- **At most `MAX_PHONE_SESSIONS` (2) phone calls run at once.** Another caller hears that
  the line is busy and never reaches the model. This caps parallel guessing as well as the
  Realtime bill.

**The trade-off you accept.** Whoever can make those calls can also keep *your* PIN locked:
one wrong guess an hour keeps PIN entry closed for as long as they like. This is
deliberate. You can wait out or clear a lock, but a guessed PIN hands over the machine.
While entry is locked, calls still connect, anything that needs no PIN still works, and the
command line on the machine is unaffected. You lose whatever the PIN gates on the phone.

What you can do about it:

- Change `KERYX_PIN`, as the alert says: with `keryx setup` if the PIN is in
  `KERYX_HOME/pin`, or wherever you set it if it is in the service's environment. Then
  restart Keryx, because a running service keeps the PIN it started with. An attacker who
  has used up their guesses has learned nothing about the new PIN.
- Clear the lock: stop Keryx, delete `DATA_DIR/pin-failures.json`, and start Keryx again.
- Remove the spoofed number from `ALLOWED_CALLERS` until the attempts stop.

Raising `PIN_FAILURE_LIMIT` makes you harder to lock out, and makes the PIN proportionally
easier to guess.

**Signature validation cannot be turned off behind a tunnel.** If
`DEBUG_SKIP_TWILIO_VALIDATION=true` and `PUBLIC_HOST` is set, `keryx serve` refuses to start
the phone channel, and `keryx doctor` reports a hard failure. Without validation, anyone who
could reach the tunnel could pose as Twilio and enter PINs at machine speed.

**Known issues, not yet fixed:**

- Keypad digits are checked as soon as the caller has typed as many digits as the PIN has,
  which tells a caller how long the PIN is. That narrows a search by about a tenth at most,
  and with the cross-call limit it brings no guess close. Fixing it changes how you enter
  the PIN, so it is tracked rather than rushed.
- The audio socket accepts a connection for up to five seconds before the stream token
  authenticates it.

The local wake-word channel is developed on `feat/local-wakeword` and is not part of `main`.
It has its own rule: a session there is authorized from its first word. That rule is
documented on the branch and does not apply to anything built from `main`.

### Where secrets live

Every key, token, and password is in `~/.config/keryx/secrets.toml` (`KERYX_HOME` moves
it). Keryx creates the file with mode 0600 inside a 0700 directory and replaces it
atomically, so it never exists with looser permissions. The plain settings are in
`config.toml` beside it, along with the PIN (`pin`) and the Google client file. **Keep this
directory out of any dotfiles repository.** It holds secrets despite its name.

Keryx uses a file rather than a keyring on purpose. A service that systemd starts at boot,
with nobody logged in, cannot unlock a keyring. Claude Code and Codex keep their credentials
in a file of their own for the same reason.

Keryx's other directories are owner-only too: the data (`~/.local/share/keryx`: transcripts,
tasks, the memory, and sign-in tokens), the state (`~/.local/state/keryx`: logs and the
approval socket), and the cache. `keryx doctor` warns when:

- any of these can be read by anyone else (`--fix` tightens the permissions and changes
  nothing else);
- one of them is inside a git working tree;
- a secret has been written into `config.toml` by hand;
- an imported copy of an old `.env` is still on disk.

Keryx reads nothing from the working directory. It used to read a `.env` in the checkout,
and a checkout is the one place where a secret is one `git add` away from leaving the
machine. `keryx serve` refuses to start while a `.env` is there, or while files remain in an
old `~/.jarvis`, until `keryx migrate` has moved them.

A secret is never put on a command line, where `ps` and your shell history would keep it.
`keryx config set KEY --stdin` reads it from standard input, and `--from-env VAR` reads it
from an environment variable. The command refuses a secret given as a plain argument.
Nothing prints a secret back: not `keryx config get`, not `config list`, and not an error
message.

### What Keryx may change about itself

Keryx can change some of its own settings in two ways:

- through the voice model's `set_config` tool, when you ask on a call (this needs the PIN);
- through any coding agent that runs `keryx config set` inside a task, because everything
  `keryx serve` starts is marked as the running service.

Either way, it can change only a *service-writable* setting. By default these are the
settings you might plausibly ask for aloud: the voice, turn-taking, which model, a few
timeouts and limits, quiet hours, and the log level. A plugin's settings are in its own file
(`DATA_DIR/tools/<name>.toml`), which `set_config` cannot reach.

Keryx may tune a limit but never turn it off. The task timeout, the call length, and the
local silence timeout all mean "no limit" at 0, and turning a limit off is a spending
decision. No value Keryx saves can stop it from starting again. `keryx config lock KEY` and
`keryx config unlock KEY` change which of the other settings it may write, and
`keryx config list` shows where each one stands.

Some settings can never be unlocked: every credential, the PIN, who may call and which
number is yours, `BRIEFING_BEFORE_PIN`, the approval bridge's switch and allowlist, whether
and where Keryx files issues about itself (an issue is public), the spending cap, the daily
task cap, retention, texting, the network settings, where a model is (every `*_BASE_URL`,
and the local voice server's flags), where data lives, and every debug switch. Each is either a secret or a line of defense. Keryx's own tools must not be able to
lower their own guard because someone asked nicely on the phone.

This is **a rule Keryx's own tools obey, not a sandbox.** A coding agent runs as you with a
shell, and can edit `config.toml` directly, as it can edit any of your files. What the rule
buys is that the ordinary paths refuse, and say so: the tool the voice model is given, and
the commands a coding agent would reach for. `keryx config set` is limited to the
service-writable keys. `config import-env`, `auth login`, `memory seed`, `setup`, `migrate`,
`plugins install`, `plugins remove`, and `config lock|unlock` refuse the service outright.

### Models of your own

With `VOICE_BASE_URL` set, a server of yours hears every call: the audio, and so a spoken
PIN; with `LOCAL_AGENT_BASE_URL`, a server of yours does the work, with the task and every
file it reads (see [docs/local-models.md](docs/local-models.md)). Three rules hold that line:

- **Where a model is cannot be changed by Keryx itself.** Every `*_BASE_URL`, and the flags
  of the voice server it runs, are protected settings: neither `set_config` nor a subagent
  may write them, and nothing unlocks them. Pointing the voice elsewhere would carry every
  call to whoever is there.
- **Your credentials stay with their own providers.** A local server is handed its own key
  or a placeholder, never `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, `CODEX_API_KEY` or
  `OPENAI_API_KEY`: each is overridden with an empty value in the agent's environment, and
  the voice server's process is given its local model's key in place of `OPENAI_API_KEY`.
  Codex runs a local task in a home of Keryx's own, away from your ChatGPT login.
- **The servers Keryx runs listen on `127.0.0.1` only.** speech-to-speech checks no key, and
  llama.cpp is started without one. A server for other machines is one you run, behind a
  tailnet or a proxy that checks a key.

The accepted risks, which `keryx doctor` warns about rather than refuses: a voice server on
another machine over plain `http://` carries the call, the spoken PIN included, unencrypted
across your network (a tailnet encrypts it; so does `https`), and a server on a public
address with no key can be used by anyone who finds it.

### Your own tools and plugins

Your own voice tools are code that Keryx runs. At the start of each call, Keryx imports
every `.py` file in `DATA_DIR/tools` into the service (`keryx.tools.custom`). A coding agent
writes one there when you ask for a new ability on the phone. Three rules apply:

- Each tool declares whether it needs the PIN, and it needs the PIN by default.
- No tool can take a built-in tool's name, so none can stand in for `submit_pin` or
  `dispatch_task`.
- Keryx refuses, unread, any file or directory that anyone but you can write.

Those are the only rules. A tool file can do anything you can do, and a coding agent that
can write one could just as well edit Keryx's source. A tool that declares
`needs_pin=False` answers every caller. So each file decides what it reads and who may hear
it, and `keryx tools` shows what each one declares.

Plugins (`keryx plugins`) are files of exactly this kind, with the same rules: a one-line
`.py` that `keryx plugins install` copies in with mode 0600, and a TOML settings file beside
it. Only you, at a terminal, can install or remove a plugin, because both commands refuse
the running service. But a coding agent can write into `DATA_DIR/tools` as it can write any
of your files, so plugins get no stronger protection than your own tools. Every value
written into a plugin's TOML is validated and quoted first, so no value can add a key of its
own. The `cluster_stats` host and partition names may contain only letters, digits, dots,
underscores, and hyphens, because both reach a remote shell.

`cluster_stats` never opens its own connection to a cluster. Its built-in guard first asks
the local ssh ControlMaster socket (`ssh -O check HOST`, with no network traffic and no
authentication). It then runs the query only over that live connection, in `BatchMode`, so
nothing can wait at a prompt. If no master connection exists, the tool says once that the
login has expired, and never retries. Where login uses two-factor authentication, a
connection that nobody can answer hangs, and a storm of them can get an address banned. The
setup wizard and `keryx plugins hosts` offer only hosts with a ControlMaster in
`~/.ssh/config`. The remote command is built from constants and refuses anything but
`squeue` and `sinfo`.

A coding agent drafts `keryx setup`'s project summaries by reading the folders you chose,
including whatever their READMEs say. So nothing it writes is kept until you accept it, each
summary is shown beside its project's path, and a project is added to `PROJECTS` only when
its path is inside a folder you chose. This matters because projects widen where a keypad
approval may write files.
