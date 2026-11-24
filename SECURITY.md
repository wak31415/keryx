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
- A Slack message being acted on that is not from your own member id, or does not mention
  your app, or that reaches Keryx while acting on what you type is not ticked; a mention
  in a channel acted on without reading ticked as well; a report posted anywhere
  but your own destination or the thread you asked in; Keryx reading a message with your
  user token while reading is not ticked, posting anything with it while sending is not
  ticked, or with sending ticked doing anything with it but posting one
  message (an edit, a deletion, a reaction, a file, a channel-wide mention); or the
  `slack_message` voice tool sending without the second, confirming call (see
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
  change nothing. `slack_report`, `check_email`, `check_calendar`, `add_calendar_event`
  and `find_document` keep the default and need the PIN, and so do `slack_read` and
  `slack_message`. This is about the phone. If you let Keryx hear your Slack app, a message you type there that
  mentions it can start work with no PIN: see
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

Your Slack app always sends you reports and files, and can do three more things, each of
which you tick or leave unticked before the app is created and can untick at any time:
act on what you type to it, read your Slack as you, and send messages as you. They are
independent, and nothing is ticked for you. **With nothing ticked, Keryx posts to you
and reads nothing; unticking everything is the off switch.** Whatever is ticked, Keryx
holds your Slack user token, because it is how Slack says which member you are; with
neither reading nor sending ticked that token can see nothing but the workspace's
custom emoji, and Keryx uses it once, when you connect
(see [Where secrets live](#where-secrets-live)). What is ticked is what Keryx goes by,
never what a token on file could do: each tool and command asks again every time it is
used.

**With acting on what you type ticked, a Slack message can start work on your machine
without the phone PIN.** That is the trade you make by ticking it, so it is said plainly. Keryx opens a connection
out to Slack (Socket Mode; nothing new is reachable from the internet) and is sent the
direct messages typed to your app, which carries your assistant's name (Lyra by default).

- **In your direct messages with the app, the only check is your member id.**
  Every message there from the one member id stored as yours for that workspace is a
  request. No mention is needed, and nothing else is checked: not the PIN, not a
  mention, not what the message says. A new message starts a task with a coding agent
  that has your full access; a reply under a task's report continues that task. So
  "thanks" typed there is a request too, and each one counts against the daily task cap.
- **Everywhere else Keryx acts only when you mention it.** With reading ticked as well,
  in a conversation the app has been added to, a message is acted on only if it is from your member id
  and mentions the app (`@Lyra`). A mention is of the app's member id, never of its
  name, so what the app is called changes nothing here. With no mention nothing happens: no reaction, no
  reply.
- **Keryx never writes to itself as you.** With sending ticked Keryx can send messages
  in your name; it refuses to send one to its own app, by any name, because that message would
  be a request from nobody. This is the tool's and the command's refusal: a coding agent
  with a shell and the token file can still post there as you, and the daily task cap is
  what bounds the loop that could start.
- **Keryx checks that the conversation is the app's own before acting on it.** Keryx's
  app is sent only the direct messages typed to the app itself. If the app were edited in
  Slack to subscribe to your own direct messages as a user (a user-level `message.im`
  event), every direct message you write to anyone would reach Keryx looking exactly
  like one typed to the app. So before the first message with no mention is acted on in
  a conversation, Keryx asks Slack, as the app, for that conversation's latest message:
  Slack answers only for a conversation the app is in. That is one request per
  conversation after each start; the message it reads is not kept. A conversation that
  is not the app's is never acted on without a mention, and the log says once that the
  app is receiving direct messages that are not its own and which subscription to
  remove. If Slack cannot be asked, the message is not acted on and the next one tries
  again. Do not add user events to the app all the same: a message there that mentions
  the app would still be taken as a request, as a mention is anywhere.
- **Everything else is dropped silently.** An edit (a mention you add by editing a
  message does nothing: send a new one), a deletion, a message from a bot, a message
  from anyone else, or a message from another workspace. A mention counts only where
  you wrote it: one on a quoted line, after a `>>>` quote, or inside a pasted code
  block, is not yours, and quoted lines are left out of what Keryx is asked. A workspace whose own id, your
  member id, or the app's member id is not on file is not listened to at all. Each
  dropped event leaves one log line with a reason code and nothing of the message.
- **The PIN is not asked for, on purpose.** The PIN exists because caller ID is free to
  spoof. A message from your own member id is not: it needs your Slack account, or
  something that can post as you.
- **The accepted risk is whoever can act as you in that workspace.** Anyone signed in as
  you, on any device, can start work on your machine by mentioning your app. So can
  any app that holds a user token able to post as you. **So can whoever controls sign-in
  to that workspace: an employer's or a school's Slack administrator can reset your
  password, add a session, or install such an app.** If you connect a workspace somebody
  else administers, you are trusting them with a way to run work on your machine. Protect
  the account as you would the machine, and if that is more than you want, leave acting
  on what you type unticked there.
- **Re-runs are counted.** Each reply that sets a task going again counts against the
  daily task cap like a new task, because nothing else limits it here: there is no PIN
  and no keypress on this channel. Over the cap, Keryx says so in one sentence and does
  nothing. Follow-ups on the phone are not counted, as before.
- **There is one owner, fixed when you connect.** Whose messages start work is settled
  once, by Slack itself: it is the member your user token belongs to, whatever is ticked.
  Never an address, and never where reports are sent, which may be somebody else. It is
  named on a line of its own each time you connect. Nothing changes it afterwards:
  not changing where reports are sent, not a user token (somebody else's is refused), and
  there is no option for it. To connect as somebody else, `keryx auth logout slack` and
  log in again.
- **An app's message is not told from yours by `app_id` alone.** A message with a
  `bot_id` is dropped. One that carries only an `app_id`, your member id, and a mention
  is taken to be yours: that is how Slack marks a message sent with a token that posts
  as you, which is already inside the accepted risk above.
- **Unticking works at once, whether or not you ever change the app in Slack.** `keryx
  auth login slack` (or `keryx setup`), with a box unticked, saves that at once: the
  tokens nothing ticked uses any more are forgotten, the voice tool that went with the
  box is turned off, and a running Keryx stops acting, reading or sending immediately.
  The app in Slack still holds the permission it was made with until you paste the
  manifest the command prints into it and reinstall it; the command says so. `keryx auth
  logout slack` forgets a workspace altogether. Neither revokes anything at Slack: the
  app is yours, and removing it there is what ends its tokens.
- **Your member id is a setting, not a secret.** It is in `slack_report.toml`
  (`send_to_slack.toml` on a machine that installed the plugin under its old name), which a
  coding agent on your machine could edit, as it could edit anything else of yours
  (see [What Keryx may change about itself](#what-keryx-may-change-about-itself)). The
  tokens are protected settings: the running service can never write them.
- **The connection's address is treated as a secret.** Slack hands Keryx a one-time
  WebSocket address that carries its own ticket. Keryx never logs it or puts it in an
  error message.
- **Results are answered where you asked, and the phone stays quiet.** A task you
  started from Slack reports in the thread you asked in. It is not texted or called
  about, though it is mentioned at the start of your next call like any other result you
  have not heard.

**With reading ticked, Keryx can read Slack as you; with acting on what you type ticked
as well, you can also mention it in other conversations.**

- **Mentions elsewhere, only with both.** The app subscribes to mentions of itself only
  when acting on what you type and reading are both ticked: something has to be
  listening, and the thread around a mention is other people's words, which only your
  own token may read. Then the app is sent every message that mentions it in
  conversations it has been added to, anyone's, and nothing else from them. It acts only
  on yours: a colleague mentioning it does nothing and is told nothing.
- **A report you ask for in a shared conversation is posted there, and its members can
  read it.** Keryx answers in the thread under your message: the reaction, the report,
  later reports for that task, and a refusal. A report is written by a coding agent with
  your full access, so it can contain anything from your machine that the request led it
  to. The agent is told who will read it. That is an instruction, not a filter: ask in a
  shared conversation only what you would be content to have answered in front of it,
  and ask in your direct messages with the app for anything else.
- **What the user token can see.** Every channel, private channel, direct message, and
  group message you are in, the list of them, the member list, and search across them.
  The token is in `secrets.toml` with the rest, so anyone who can read that file can read
  your Slack as you until you revoke it (remove the app, or untick reading and reinstall
  it). Keryx itself only ever reads with it: the one request the reader can make is a
  read of a fixed list of methods, so it cannot post, react, or mark anything as read
  with your token. The token is never given to a coding agent; an agent reads Slack
  by running a command that prints what it read.
- **Reading only when you ask.** Nothing reads your Slack in the background. It is read
  when you ask about it on a call (the `slack_read` plugin, which needs the PIN), and by
  a task whose request asks about Slack messages. The second is a rule in the coding
  agent's instructions, not something the code can enforce: an agent that has the command
  could run it when it should not. What it can do with the result is bounded by the same
  things as the rest of its work.
- **Without reading ticked, nothing of Keryx's reads your Slack, and a coding agent is
  told not to with anything else.** A coding agent runs with your own Claude Code or Codex
  configuration, which may hold Slack tools of its own (an MCP server, a connector),
  signed in as whatever they are signed in as. Keryx does not control what those see.
  While reading is not ticked, Keryx says on a call that reading is not switched on
  instead of handing the question over, and an agent is told to say the same rather
  than read your Slack some other way. With it ticked the agent is told to read with
  Keryx's own command and nothing else. Both are instructions to the agent, not enforcements.
- **Other people's messages are data, never instructions.** Whatever is read from Slack
  is kept apart from what you asked. Your own request is the only instruction. Every
  message read is wrapped between markers that carry a token made for that one read,
  under a header saying these are other people's words, each attributed to who wrote it
  and held to one line. A message cannot name the marker that would end the wrapping.
  That is how every reader gets it: the voice model on a call (short clipped lines, with
  a note to say them and never act on them), a coding agent, and the JSON a program
  reads. People's and channels' names are treated the same way. A question about the
  messages is answered by a model with no tools.
- **This reduces the risk of prompt injection and does not remove it.** Anyone who can
  put a message where you might ask about it (a direct message, a channel you are in, a
  Slack Connect channel with another company) can try to instruct the model that reads
  it. The wrapping, the attribution, and the instructions make that harder. A coding
  agent that summarises a thread still reads the attacker's words with a shell at hand.
  Ask about conversations you would be content to have an agent read, and treat a summary
  of an untrusted channel as you would the channel.
- **What is read is kept.** An answer said on a call is in that call's transcript, and a
  task's report is on disk and posted where you asked. That is why search uses
  Slack's older search method and not its newer one, whose terms forbid keeping results.

**With sending ticked, Keryx can send messages as you.** This is the one box with which
Keryx says something to somebody else in your name, so read it as that.

- **What it can do.** Post one new message, or one reply in a thread, as you: to a
  person (their direct conversation with you, opened if you have never written to them),
  to a channel you are in, or in a thread. The user token gains `chat:write` and
  `im:write` for this.
- **What sending without reading can see.** To find who you mean, the token needs the
  list of the conversations you are in and the workspace's member list (`channels:read`,
  `groups:read`, `im:read`, `mpim:read`, `users:read`): names and ids. With reading
  unticked it is given no history and no search, so no message can be read with it, by
  Keryx or by anyone who takes the token. The part of Keryx that sends can make two Slack requests and no
  others, so it cannot edit, delete, react, upload, join, schedule, or broadcast a reply
  to its channel, and a message containing `@channel`, `@here`, or `@everyone` is refused.
  The text is posted with Slack's own markup characters escaped, so nothing in it can be
  a mention of a person or a group, or a link with other words on it. Whether Slack
  turns a bare web address in it into a link is Slack's doing and has not been checked.
- **On a call it takes two steps, and the second is yours.** The first call of the
  `slack_message` tool sends nothing. It works out who the message goes to and hands
  back the recipient as Slack names them, the exact words, and a one-time value. Only a
  second call carrying that value sends, and it sends what was shown, not what the second
  call says. Keryx is instructed to say the recipient and the message and to send only
  after you say yes. The two steps are enforced in code; that it waits for your yes
  between them is an instruction to a language model, not a lock. It needs the PIN.
- **A name is never guessed.** If "Sam" could be two people, or a person and a channel,
  nothing is sent and you are asked which. The same holds when the workspace has more
  members than Keryx reads in one go: the one it found is offered and you are asked,
  because another may be on a page it did not read. A person's or a channel's name is
  handed to the model inside the same wrapping as a message, as words to say and never
  to act on.
- **The accepted risk: a coding agent can send as you too.** With sending ticked, a
  task's coding agent can run `keryx slack message`, which sends as you. It is instructed to do so only
  when your request asks for exactly that message to exactly that recipient, and never
  because something it read told it to. **That is an instruction, not an enforcement.**
  An agent that has been steered by text it read (a Slack message, a web page, a file in
  a repository) could send a message in your name to anyone in the workspace, with any
  words but a channel-wide mention. The request-versus-context rule above reduces this
  and does not remove it. Nothing asks you first when an agent sends, because nobody is
  on the line.
- **Unticking it removes the capability from Keryx, not from the token.** `keryx auth
  login slack` with sending unticked takes effect at once, a call in progress included:
  the tool and the command read what is ticked again every time they are used. With
  reading still ticked, the user token stays on disk with its `chat:write` scope; only
  reinstalling the app from the manifest the command prints, or `keryx auth logout slack`
  and removing the app, takes the permission away at Slack.
- **The bounds are the tool's and the command's, not an injected agent's.** No edit, no
  delete, no channel-wide mention, one message, the recipient shown first: those hold
  for `slack_message` and `keryx slack message`. A coding agent has a shell and can read
  `secrets.toml`, so one that has been steered can use the token directly, whatever is
  ticked, for whatever its scopes allow. If you would not give a capable stranger
  who could be talked into things the ability to post as you in that workspace, leave
  sending unticked, and know that with reading ticked the same stranger can read as you.
- **What a coding agent posts to you goes to you.** `keryx slack report` posts as Keryx,
  to where your reports go, or into the thread its own task was asked in, which in a
  shared conversation its members read. The command holds an agent to its own task's
  thread; it is not a sandbox, and an agent with a shell is bounded by what the bot
  token can do, not by this command.
- **A coding agent can put a file into Slack, and a file is whatever it can read.**
  `keryx slack report --file` sends files to those same two places and no other: there
  is no option that names a conversation. The agent runs as you, so the file can be
  anything your account can read on that machine: a plot, a diff, a private key. It is
  instructed to send only what your request asks for; that is an instruction, not an
  enforcement, and an agent steered by text it read could send a file you did not ask
  for. This adds no reach it lacked: it could already read that file and post its
  contents as text. **Where it lands decides who reads it.** For a task you typed in
  your direct messages with the app, or asked for on a call, that is you. For a task you
  typed in a shared conversation, the file goes into that thread and every member of the
  conversation can open it, download it and keep it, and removing it from Slack
  afterwards does not take back a copy. Ask in your direct messages for anything you
  would not hand to that room. Sending takes the bot permission `files:write`, which
  also lets the bot token delete a file (`files.delete`); Keryx never calls that, and a
  steered agent that read the token could.
- **A few paths are refused, as a guard against a mistake and not as a wall.** The
  command sends only a regular file that is readable, not empty, at most 1 GB, and at
  most ten at once, and refuses anything inside Keryx's configuration directory
  (`secrets.toml`, the PIN), through a link as well. An agent with a shell can copy a
  file elsewhere first, so this stops an accident and nothing more. The upload goes to
  an address Slack issues for it, over HTTPS, without the bot token; that address is not
  logged or kept.
- **A named MCP server is not used once Keryx has its own app.** If `slack_report.toml`
  names a Slack MCP server from before the app existed, subagents are no longer pointed
  at it and Codex is no longer handed it. A Claude subagent still has whatever servers
  your own Claude configuration gives every session, that one included; it is told to
  send through Keryx's command and no other Slack tool, which is an instruction too.
- **What is sent is kept like everything else.** The words are in the call's transcript
  or the task's report.

Each further workspace (`keryx auth login slack --workspace NAME`) is a separate app with
its own ticks, tokens, and member id. A message in one workspace is checked against that
workspace's ids only, and a report never leaves the workspace its task was asked in.
A workspace here is the one Slack knows by its id, not the name you connected it under:
if you log out of one and log in to another under the same name, a task asked for in the
first is not reported into the second, and a reply there does not resume it.

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
  the call that is guessing), posted to Slack if the `slack_report` plugin is on, and
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

What each of those secrets can do is up to whoever issued it, and Slack's are worth
knowing about. The Slack app that `keryx setup` has you create gives its bot token two
powers whatever you tick: posting messages as the app (`chat:write`), and uploading
files as the app, which also covers deleting one (`files:write`). It can no longer
read your workspace's member list or anybody's email address, which an earlier version
used to find you by your address. **Keryx also holds a Slack user token whatever you
tick**, because a bot token does not say who installed the app and a user token does: it
is how Keryx knows which member you are. With neither reading nor sending ticked that
token has one permission, `emoji:read`, which lets it list the workspace's custom emoji
and nothing else; Keryx uses the token once, when you connect, to ask Slack whose it is,
and never again. With reading ticked the same token can read your Slack, and with
sending ticked post as you: the section above says what. Whatever a token can do, it
can do for as long as it is valid, and so can anyone who can read `secrets.toml`.
**Unticking a box does not shrink a token**: one issued with reading ticked keeps its
permissions at Slack until you reinstall the app from the manifest for what is ticked
now, so Keryx forgets that token once neither reading nor sending is ticked, and asks
for the new one at your next login. (With one of the two still ticked the token is
kept, since that one needs it, and Keryx simply stops using it for the other.) An app connected
before any of this, with a bot token alone, keeps working as it was.

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
