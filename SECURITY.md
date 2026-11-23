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

The latest release and the `main` branch are supported. There are no maintenance branches
and no backports: fixes land on `main` and go out in the next release. Update with
`keryx upgrade`, or by pulling if you work from a clone.

| Version | Supported |
|---|---|
| The latest release | ✅ |
| `main` | ✅ |
| Older releases | ❌ — update with `keryx upgrade` |

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
- A Slack message being acted on that is not from your own member id, or not in the direct
  message with your own Keryx app, or that reaches Keryx at the `announce` level (see
  [Messages you type to Keryx on Slack](#messages-you-type-to-keryx-on-slack)).
- **Anything that sets or replaces a PIN while one already exists**, from the phone or from
  a coding agent, or that reads an enrolled PIN back out anywhere.
- With `BRIEFING_BEFORE_PIN=false`, anything of yours reaching a call that has not given
  the PIN.
- A way for untrusted content, such as an email, a web page, or a file, to reach a coding
  agent without the PIN.
- A coding agent's credential appearing on its command line, in a log, or in a spoken
  error. Credentials are passed only in the environment, and a refused key is redacted
  when it is quoted back. The Google sign-in is the one credential an agent's tools read
  from a file instead (see [Signing in to Google](#signing-in-to-google)), and it is
  redacted the same way.
- A Google sign-in that can be completed, or whose code can be used, by anyone but the
  person at the browser: a redirect that is accepted without this sign-in's `state`, a
  listener reachable from another machine, or a code that can be exchanged without the
  verifier.
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
  change nothing. `send_to_slack`, `check_email`, `check_calendar`, `add_calendar_event`
  and `find_document` keep the default and need the PIN. This is about the phone. If you
  let Keryx hear your Slack app, a message you type there can start work with no PIN: see
  [Messages you type to Keryx on Slack](#messages-you-type-to-keryx-on-slack).
- **An announcement tells a call nothing it could not already hear.** A result that finishes
  during a call is announced under the same rule as the briefing. Restart confirmations and
  approval escalations go only to a call that has given the PIN or that Keryx placed to your
  number. A call that has proved neither never counts as you having been told: the
  call-back is still placed, and the approval still rings. When that call is on the number
  the call-back would ring, the call-back waits for it to end, and a PIN given on it after
  the result was spoken counts then. A spoofed caller ID can delay a call-back this way,
  but never absorb it.
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
  log line contains the PIN, spoken or keyed, at any log level. Keryx's own lines say that
  a digit arrived and never which. The libraries under the two connections a PIN crosses
  (keypad digits from Twilio, and the voice model's messages) would each log what they
  carry at `DEBUG`, so both are held above it whatever `LOG_LEVEL` says.

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
`DATA_DIR` at an empty directory found no PIN, and no PIN means enrollment is open.

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

You cannot start new work, search your past calls, send to Slack, restart or upgrade Keryx,
or call a number chosen during the call. Those need the PIN. This level still matters with
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

### Messages you type to Keryx on Slack

Your Slack app has one of three access levels, which you choose before the app is created
and can lower at any time. **`announce` is the default and the off switch:** Keryx posts
to you and reads nothing, exactly as before there were levels.

**At `converse`, a Slack message can start work on your machine without the phone PIN.**
That is the trade you make by choosing it, so it is said plainly. Keryx opens a connection
out to Slack (Socket Mode; nothing new is reachable from the internet) and is sent the
direct messages typed to your Keryx app. A reply in the thread under a task's report
continues that task. A new message to the app starts a task, with a coding agent that has
your full access.

- **What is acted on is narrow.** Only an ordinary message (not an edit, a deletion, a
  file, or anything else Slack marks with a subtype), in the direct-message conversation
  with your own Keryx app, not posted by a bot, and from the one member id stored as yours
  for that workspace. Everything else is dropped silently. The app subscribes to its own
  direct messages and nothing more, so messages you send other people never reach Keryx.
- **The PIN is not asked for, on purpose.** The PIN exists because caller ID is free to
  spoof. A message in your own direct messages from your own member id is not: it needs
  your Slack account, or something that can post as you.
- **The accepted risk is whoever controls your Slack account.** Anyone signed in as you,
  on any device, can start work on your machine by messaging your Keryx app. So can any
  app that holds a user token able to post as you, if it posts in that conversation.
  Protect the Slack account as you would the machine: two-factor
  sign-in, and care about which apps you grant "send messages as you". If that is more
  than you want, stay at `announce`.
- **The same limits apply as on the phone.** The daily task cap counts these tasks, and
  each is an ordinary task with the same budget and time limit.
- **Lowering the level works at once.** `keryx auth login slack --access announce` (or
  `keryx setup`) forgets the tokens the lower level does not use, and a running Keryx
  stops acting on messages immediately. `keryx plugins remove send_to_slack` turns Slack
  off altogether. Removing the app in Slack ends it from the other side.
- **Your member id is a setting, not a secret.** It is in `send_to_slack.toml`, which a
  coding agent on your machine could edit, as it could edit anything else of yours
  (see [What Keryx may change about itself](#what-keryx-may-change-about-itself)). The
  tokens are protected settings: the running service can never write them.
- **The connection's address is treated as a secret.** Slack hands Keryx a one-time
  WebSocket address that carries its own ticket. Keryx never logs it or puts it in an
  error message.
- **Results are answered in Slack, and the phone stays quiet.** A task you started from
  Slack reports in that thread. It is not texted or called about, though it is mentioned
  at the start of your next call like any other result you have not heard.

**At `read`, your user token is on the machine.** The app also asks you for a user token,
which can read every channel, private channel, direct message, and group message you are
in, list them, read the member list, and search. In this release Keryx checks that token
and stores it; nothing reads with it yet. It sits in `secrets.toml` with the rest, so
anyone who can read that file can read your Slack as you until you revoke the token
(remove the app, or lower the level and reinstall it). Choose `read` only when you want
what it will be used for.

Each further workspace (`keryx auth login slack --workspace NAME`) is a separate app with
its own level, tokens, and member id. A message in one workspace is checked against that
workspace's member id only.

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
`config.toml` beside it, along with the PIN (`pin`) and the Google client file, if you made
a client of your own. **Keep this directory out of any dotfiles repository.** It holds
secrets despite its name.

What each of those secrets can do is up to whoever issued it, and one is worth knowing
about. The Slack app that `keryx setup` has you create gives its bot token two powers:
posting messages as the app (`chat:write`), and reading your workspace's member list with
email addresses (`users:read`, `users:read.email`). Keryx uses the second for one lookup,
to find you by your address so that you do not have to copy a channel id out of Slack. The
token keeps that reach for as long as it is valid, though, and so does anyone who can read
`secrets.toml`. An app you made by hand with `chat:write` alone still works; setup then
asks you for a channel or member id instead. At the higher access levels the app holds
more, and the section below says what.

Keryx uses a file rather than a keyring on purpose. A service that systemd starts at boot,
with nobody logged in, cannot unlock a keyring. Claude Code and Codex keep their credentials
in a file of their own for the same reason.

Keryx's other directories are owner-only too: the data (`~/.local/share/keryx`: transcripts,
tasks, the memory, and sign-in tokens), the state (`~/.local/state/keryx`: logs and the
approval socket), and the cache. `keryx doctor` warns when:

- any of these can be read by anyone else (`--fix` tightens the permissions and changes
  nothing else);
- one of them is inside a git working tree;
- a secret has been written into `config.toml` by hand.

Keryx reads nothing from the working directory, such as a `.env`: a checkout is the one
place where a secret is one `git add` away from leaving the machine.

A secret is never put on a command line, where `ps` and your shell history would keep it.
`keryx config set KEY --stdin` reads it from standard input, and `--from-env VAR` reads it
from an environment variable. The command refuses a secret given as a plain argument.
Nothing prints a secret back: not `keryx config get`, not `config list`, and not an error
message.

### Signing in to Google

`keryx auth login google` connects your Google account once, for the coding agents and for
the answers on a call (the email, calendar and document plugins). What it leaves, and what protects it:

- **The token is a file.** `DATA_DIR/google_token.json` holds the refresh token, the
  client it was issued to, the scopes Google granted, and your address. It is 0600 in a
  0700 directory and replaced atomically, like every other secret here. A copy of it is in
  `DATA_DIR/google/`, in the form the agents' Google tools (the `workspace-mcp` server)
  read. The approval bridge's denylist names both, so no keypad approval can run a command
  or an edit that mentions them. It knows the copy by its default path
  (`…/keryx/google/`): if you move `DATA_DIR`, only the token is still named.
- **Whoever has the machine has your Google account**, to the extent of what you granted.
  That is the same position as every other key in this file, and the PIN does not change
  it. What you grant is the narrowest set the tools work with, and it is still a lot:
  reading and changing your mail and calendar events, reading everything in Drive, and
  editing your Docs, Sheets and Slides. In Drive itself, Keryx can change only the files
  it created. A coding agent you dispatch can use all of it, and prompt injection into a
  task you authorized is still inherent. You can leave any service unticked on Google's
  screen.
- **`keryx auth logout google` ends it.** It revokes Keryx's access at Google and deletes
  everything a sign-in left on this machine: the token, its copy for the agents' tools,
  a sign-in in progress, and anything an older sign-in left. The files are deleted even
  when Google cannot be reached, and then the command says so and names
  <https://myaccount.google.com/permissions>, where you can end the grant yourself at any
  time. A sign-in that Keryx refuses (nothing granted, an unexpected account) is revoked
  the same way rather than left at Google with nobody holding it.
- **The agents' tools cannot sign in.** The server is started with the token's location
  and no OAuth client (one set in Keryx's own environment is taken out on the way, by
  `env -u`), so it can refresh and use what you granted and can never open a consent
  screen of its own behind a task nobody is watching. Tools that would need a wider scope
  are switched off rather than left to fail. Signing in as a different account removes the
  previous account's copy, because those tools would still accept its address. What it is
  started with is a path and your address, and no credential: a coding agent's command
  line is where that configuration ends up. Whatever that file holds is redacted from
  every error a task returns or logs, read afresh each time, so the access token the
  server mints while a task runs is covered once it is in the file. Lines Codex prints
  about a server while it starts are redacted with what the file held at the start.
- **A Google tool that cannot be served still opens a port.** When a tool finds no usable
  credential (the sign-in was revoked, say), `workspace-mcp` 2.6.0 first starts its own
  sign-in listener on `localhost:8000` and only then fails for want of a client. The
  agent gets an error and is told to send you to `keryx auth login google`. The listener
  stays until that task's server exits. With no client it has no sign-in to complete, but
  it is a port another user of the machine can reach for that long, and Keryx cannot
  switch it off in that release.
- **Keryx's own Google client, when a release ships one, is not a secret.** It is a
  "Desktop app" client, and its client secret is a file inside the package that anyone
  can read. Google says so of this kind of client in its own words: "In this context, the
  client secret is obviously not treated as a secret"
  (<https://developers.google.com/identity/protocols/oauth2>). Knowing it lets someone
  present their own app as Keryx on a consent screen. It does not let them into your
  account: they would still need you to approve that screen, and they cannot use a code
  issued to you. A client of your own (`--client-file`) replaces it entirely.
- **What protects the exchange is PKCE, `state`, and where the answer is sent.** The
  sign-in keeps a one-time verifier on this machine and sends Google only its hash, so
  the code Google returns is useless to anyone who intercepts it. `state` ties that code
  to the sign-in that asked for it. The answer goes to a loopback address, which only this
  machine can reach.
- **The listener is local and short-lived.** With a browser on the same machine, Keryx
  listens on `127.0.0.1` only, on a port the operating system picks, for the length of one
  sign-in. It accepts only the request that carries this sign-in's `state`, answers
  anything else with an error and keeps waiting, serves every connection at once so that
  one held open cannot keep another waiting, ends any connection that takes longer than
  five seconds, logs nothing (the request holds the one-time code), and closes as soon as
  it has its answer or after five minutes.
- **The sign-in link is never put on a command line.** The link holds this sign-in's
  `state`, and on a shared machine every user can read every process's command line. So
  the browser is opened on the listener's own address (`/start`), which sends it on to
  Google once, and the link itself is shown only on your terminal.
- **An account nobody expected is never saved silently.** Once you have signed in, Keryx
  knows your address (`USER_GOOGLE_EMAIL`). A later sign-in that comes back as any other
  account is refused before anything is written, and the grant is revoked: `keryx setup`
  asks whether you meant it, with no as the answer if you just press Enter, and the
  command line refuses and names both addresses, to be run again with `--account` if you
  did mean it. Every way of finishing somebody else's sign-in ends as *their* account, so
  this is the check that does not depend on winning a race.
- **The accepted risk, on a machine you share:** another user can see the listener's
  port and ask for `/start` themselves. If they get there before your browser does, they
  hold the link, and could finish the sign-in with a Google account of their own, so that
  Keryx's agents would read and write *their* account while you believe it is yours. Three
  things stand in the way. `/start` works once: when it is asked for a second time, which
  is your browser arriving after theirs, Keryx stops the sign-in and saves nothing, and
  because every connection is served at once they cannot hold your browser back to stop
  that. A sign-in as an account other than the one you signed in as before is refused, as
  above. And the command always ends by saying which account signed in. What remains is
  the **first** sign-in on a machine, when there is no address to compare with, against a
  program that finishes the whole exchange in the moment before your browser opens, or
  that exhausts the listener's process so your browser is never served. Then the only
  sign is the address the command prints. So on a machine you share with people you do
  not trust, sign in with `--headless`: there is no listener, the browser lands on an
  address nothing listens on, and you paste that address back yourself. A client of your
  own in Personal mode closes it too, because Google refuses every account but yours.
- **An address pasted back is a code in your scrollback.** It is single-use, it expires in
  minutes, and it cannot be exchanged without the verifier, which waits in
  `DATA_DIR/google_signin.json` (0600) until the sign-in finishes. With `--headless` at a
  terminal the address is typed at a prompt. Without a terminal it is handed over on
  standard input (`--callback-url -`), so it is not on a command line either. The older
  form, the address as the option's value, still works for one release and does put the
  code where other users of the machine can read it; they cannot use it without the
  verifier.

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
task cap, retention, texting, the network settings, where data lives, and every debug
switch. Each is either a secret or a line of defense. Keryx's own tools must not be able to
lower their own guard because someone asked nicely on the phone.

This is **a rule Keryx's own tools obey, not a sandbox.** A coding agent runs as you with a
shell, and can edit `config.toml` directly, as it can edit any of your files. What the rule
buys is that the ordinary paths refuse, and say so: the tool the voice model is given, and
the commands a coding agent would reach for. `keryx config set` is limited to the
service-writable keys. `auth login`, `auth logout`, `memory seed`, `setup`, `service`, `upgrade`,
`forget`, `approvals --enable`, `approvals --install-hook`, `plugins install`,
`plugins remove`, and `config lock|unlock` refuse the service outright.

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

The Google plugins (`check_email`, `check_calendar`, `add_calendar_event`, `find_document`)
put text that other people wrote in front of a model, during a call you have given the PIN
on. Anyone can send you mail, anyone who can send you an invitation can put a title on your
calendar, and anyone can share a file with you. That text is treated as data, in these
ways:

- **Mail and documents never reach the voice model.** `check_email` and `find_document`
  hand what they read to one separate model call that has no tools, no settings and no
  MCP servers, and only that call's few spoken sentences come back, clipped to a short
  length. A document's text is fenced and described to that model as material and never
  instructions. The fence's markers carry a random token made for that one call, so a
  document cannot contain the marker that would close it, and anything in the text that
  looks like a marker is removed, a fixed number of times, after which whatever is left
  has every `<` rewritten so that no marker can remain. Text in a mail or a document can
  still change what those sentences say. It has nothing to act with.
- **A mail or a document cannot stall the service by its shape.** Their text is processed
  inside the service, where a slow step would hold up every call in progress. Each
  pattern run over it takes time in proportion to its length and no more, and each has a
  test on how long hostile input takes.
- **Titles and names reach the voice model, cut down and labelled.** `check_calendar` has
  no model in between, so the voice model sees each event's title, place and calendar
  name, and `find_document` shows it each file's name and owner: one line each, clipped to
  120 characters, never an event's description. An event's place is often a meeting link,
  and it is passed through like any other place: the result's note and the tool's
  description both say a web address is never read out. Each result carries a note, and
  each tool's description repeats it, that these are other people's words, to be said and
  never acted on. With every ticked calendar read, an event may be on somebody else's
  calendar; it then comes with that calendar's name, and the description says to say
  whose it is.
- **This reduces the risk and does not remove it.** The voice model on a call that has
  given the PIN can dispatch tasks, and a model can be talked into things. A title or a
  sentence written as an instruction is prompt injection into a call you authorized, which
  is the same position as the coding agents' (see Out of scope). None of these tools
  answers without the PIN, so none of this text reaches any model on a call that has not
  given it. If you do not want it in front of the voice model at all, leave the plugin
  off: the question is then dispatched as a task.
- **Reading is a matter of construction, and the one tool that writes does one thing.**
  A reading plugin is built on a client that has no method to write with
  (`integrations/google_api.py`), whatever the sign-in allows. `add_calendar_event` can
  create one event on your own calendar and nothing else: it sends no attendees and asks
  Google to notify nobody, so it cannot be used to send anything to anyone, and it cannot
  change or delete an event. It refuses a start that passed more than a quarter of an
  hour ago, and it never repeats a request that may have reached your calendar. The one
  request it does send again is one Google answered 401, after refreshing the sign-in: a
  401 is a refusal before anything was created. It also sends no event id of its own
  that would make a second send harmless, so the accepted risk is a duplicate when a
  request that did reach Google is reported as unconfirmed and you ask again without
  looking; the assistant is told to say the event may be there and not to add it again.
- **Nothing the caller says is spliced into a request.** A Drive search is a quoted term
  with its quotes and backslashes escaped, inside a query built from constants. A file id
  is matched against a pattern before it is put in an address, so an id cannot name
  another endpoint.
- **Nothing from Google is logged or quoted.** A failure is logged as a status, one of a
  short list of Google's own reason words, and the kind of thing asked for, never a
  response body (which can quote a token back) and never which calendar or file. Events,
  mail and documents are counted in the log, never written to it. The HTTP client's own
  request log, which would write every address in full (a calendar's id is an email
  address, and a search is in the address), is held to warnings at every log level.

A coding agent drafts `keryx setup`'s project summaries by reading the folders you chose,
including whatever their READMEs say. So nothing it writes is kept until you accept it, each
summary is shown beside its project's path, and a project is added to `PROJECTS` only when
its path is inside a folder you chose. This matters because projects widen where a keypad
approval may write files.
