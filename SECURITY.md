# Security policy

Jarvis answers a phone number and hands what it hears to a subagent running with your full
user access. Please treat a bug in the PIN gate, the Twilio signature check, the stream
token, the approval bridge's policy allowlist, or the report links as a security issue
rather than an ordinary one.

## Threat model: what the PIN is for, and what it is not

**The PIN defends against somebody on the phone, not against somebody on your machine.**
Caller ID is spoofable: `ALLOWED_CALLERS` keeps strangers from reaching the voice model at
all, but anyone who fakes an allowed number does reach it, and the source being public
makes that cheap. So the allowlist is not authentication; the PIN is, and a phone-side
spoofer is the whole of what it stands between you and.

It is **not** a defence against a compromised machine, and it was never going to be.
Anyone who can read your files has `.env`, and `.env` has `JARVIS_PIN` — along with the
API keys, the Twilio token and everything else. Against that attacker the PIN is worth
nothing, and `~/.jarvis` is already theirs to read directly.

That is why **reads happen before the PIN**. Gating them bought nothing against the
attacker who matters, and charged a keypad entry to every ordinary call you make. The
trade is deliberate, and the line it draws is **reading versus acting**: before the PIN,
on an inbound call, you can hear what Jarvis knows, and nothing the caller says or does
changes anything or outlives the call.

- **Until a PIN exists, none of this happens.** The trade above is a read against a keypad
  entry, and it presumes there is an entry to make: on a machine that has never had a PIN
  a phone call cannot authenticate at all, so an allowed caller would otherwise hear the
  memory read out on every call for ever. Until one is set — in `.env`, or on the first
  call — the briefing is withheld whatever `BRIEFING_BEFORE_PIN` says, and the read-only
  tools over the same material are refused with it.
- **A call opens knowing what Jarvis knows.** The results you have not been told about,
  what Jarvis remembers about you (`memory.md`), your project names, your project briefs
  and your installed skills all reach the session at the greeting
  (`BRIEFING_BEFORE_PIN`, on by default), and the four voice tools that read the same
  material back — `list_tasks`, `get_task_status`, `get_task_result` and `list_projects` —
  answer without the PIN too. The accepted cost is that somebody who has spoofed an
  allowed number hears it. Set `BRIEFING_BEFORE_PIN=false` to hold all of it back until
  the PIN, which also puts those four tools back behind it.
- **`recall` is the read that still needs the PIN**, and the distinction is deliberate.
  The briefing is bounded and curated: you can read it with `jarvis memory`, prune it, and
  it is the same page whatever the caller says. `recall` is an unbounded query the *caller*
  steers, over every raw transcript Jarvis has ever written — a different quantity of
  exposure, and the one thing on the phone a spoofer could actually mine.
- **Nothing that acts happens without it.** Every voice tool that hands work to Claude,
  writes something down or changes anything answers `pin_required` first; only
  `check_billing`, `cluster_stats`, `web_search`, `submit_pin` and `end_session` answer at
  any level whatever the setting says. A test walks every registered tool, so a new one is
  gated unless it is added to one of those lists on purpose.
- **Nothing is announced that it could not hear anyway.** A result that lands mid-call is
  announced under the same rule as the digest. Restart confirmations and approval
  escalations are not: they go only to a call that has proved something. A call that has
  proved nothing never counts as having told *you* either way — the call-back is still
  placed and the approval still rings.
- **Nothing outlives the call.** **No memory update is dispatched for it** — a call that
  read the memory out still cannot rewrite it, which is the invariant that matters most
  here: hearing what Jarvis believes about you is recoverable, editing it is not. No result
  can be marked as heard except the ones this call itself read out, no call-back or
  call-back note can be arranged, and nothing is sent to Slack. Its transcript is kept on
  disk, as the record of what was tried, but it is marked as never authorized and `recall`
  never reads it back to a model.
- **A spoken PIN is never written down.** Transcript lines are redacted as they are
  written, in digits or in words, and everything that reads a transcript back to a model
  redacts again, because logs from before this still hold it. No log line carries it.

## Setting the first PIN, on the first call

`JARVIS_PIN` is set at the keyboard, and until it is, the phone is no use to you: every
dispatch is refused, and nothing of yours is read out at all (above). That is the one
setup step the thing being set up cannot do for itself — so **the first call may key a PIN
in**, and that is the only way a PIN is ever set from the phone.

**It is a one-way door.** While no PIN exists anywhere — none in the environment, no
`DATA_DIR/pin` — a caller keys six to eight digits, is asked to key the same digits again,
and once the two match that is the PIN from then on. The moment a PIN exists the door is
shut, and it is shut by the write itself rather than by a check: `DATA_DIR/pin` is created
with `O_CREAT | O_EXCL`, so a second write fails in the kernel. There is deliberately no
setter anywhere — no voice tool, no CLI command — that can change an enrolled PIN.

**The accepted risk: whoever calls first sets it.** The window is the few minutes between
starting Jarvis and making the first call, it closes on first use, and nobody is going to
spoof your number inside it; the caller still has to be on `ALLOWED_CALLERS` to reach the
voice model at all. If you would rather not take that bet, set `JARVIS_PIN` in `.env`
before you start — an environment PIN always wins, and there is then no window to close.
`jarvis init` prints a `JARVIS_PIN=` line with six random digits for you to paste, and
`jarvis doctor` says which of the two sources the PIN in use came from.

**The digits are keyed, never spoken.** Saying a PIN out loud cannot enrol one, because
transcription mishears digits and a mis-set PIN that nothing can change is the worst
outcome here; the keyed digits take the same path as any other keyed PIN and never reach
the model, the transcript or a log line. Three unusable entries leave the PIN for the rest
of that call — a cap, not a lockout: nothing has been set, so there is nothing to guess
at, and the next call may still enrol. An enrolled PIN is stored as digits rather than a
hash, in a 0600 file inside a 0700 `~/.jarvis`: six digits fall to any hash in
microseconds, so hashing would imply a protection that is not there, and the file sits
beside your call transcripts and `memory.md`, which are no less private.

**What a subagent can do to it, and what it cannot.** A subagent runs as you with
`bypassPermissions`, so it can delete `DATA_DIR/pin` exactly as it can edit `.env`. What
it cannot do is *rewrite* an enrolled PIN — that is what `O_EXCL` buys — so there is no
silent swap. Deleting the file is a lockout plus a fresh enrolment window for whoever
calls next: loud, and visible in `jarvis doctor`, rather than a PIN quietly becoming
somebody else's. The answer, once a PIN has been enrolled, is to move it into `.env`,
where the environment wins for ever after; that is what `jarvis doctor` tells you to do
with one.

**Why there is no "config setter script Jarvis can call"**, so that nobody proposes one
later: any sudoers rule that lets Jarvis run a setter without a password lets a subagent
run it too, which hands straight back the ability the `O_EXCL` write exists to remove. A
root-owned setter that *you* run with your own sudo password is fine — the point is only
that Jarvis must have no privileged way to invoke it.

## Calls Jarvis places itself

A call-back, a restart's confirmation and an approval escalation are outbound, and that
makes them different in kind: Jarvis dialled a number *you* configured, so reaching it
means holding that phone. The proof is the single-use token Jarvis mints for the call's
own media stream, which records that Jarvis placed it and what it dialled; a call whose
token says so, and whose dialled number is one of yours, opens with that much proved.
"One of yours" is `ALLOWED_CALLERS` (plus `OWNER_NUMBER` if you set it to something else),
because this is a single-owner agent and a second allowed number is your second handset.
Nothing else confers it — not Twilio's `From`/`To`, which your caller's carrier supplies,
and so not any inbound call however it presents itself.

What it buys, over and above what any call may hear: answering the question Claude came
back with (`send_followup`), arranging a call back **on that same number**, marking any
result as told, and answering a waiting approval on the keypad — the last because the
approval allowlist is already the filter on what a key may ever run, its denylist still
wins, and `jarvis approvals --disable` still wins over everything. What it does not buy:
starting new work, searching your past calls, sending to Slack, restarting Jarvis, or
calling a number chosen during the call. Those are the PIN. (It also still matters with
`BRIEFING_BEFORE_PIN` off, where it is what lets a call-back read you the result it rang
about.)

Voicemail is the residual risk here, and it is handled rather than ignored: an answering
machine can take an outbound call and be read a result. So before acting on anything
*said* on such a call, Jarvis asks for one keypress — a machine cannot press a key — and
the keypad is still the only thing that can answer an approval. Detecting the answering
machine itself (Twilio's `machine_detection`) is filed as issue #50, not built.

**Getting to the PIN on such a call.** Two things can want the keypad at once there: an
approval menu that has been read out, and the PIN that would take the call the rest of the
way. Press **`*`** to give the keypad to the PIN — `*` is never part of a PIN and never an
option on a menu, so it cannot be mistaken for either — then key the PIN in as usual;
press `*` again to put the keypad back on the menu. A wrong PIN leaves the keypad where it
is, so you can simply try again. Saying the digits out loud works too and always did:
`submit_pin` is one of the handful of tools that answer at any level.

In scope, then: anything that lets a caller who has not given the PIN **act** — change
anything, leave anything behind, or reach a subagent. That **includes the memory writer**,
a `bypassPermissions` subagent whose whole input is a call's transcript, and which must
run only for calls that were authorized: reading the memory out is the design, rewriting
it is not. Also in scope: an announcement beyond what the call could hear at the greeting;
`recall`, or any comparable unbounded search, answering without the PIN; anything private
that the briefing does *not* already carry being read out without it; anything that
confers possession on a call Jarvis did not place, or on one it placed to a number other
than `OWNER_NUMBER`; **anything that sets or replaces a PIN while one already exists**, from
the phone or from a subagent, or that reads an enrolled PIN back out anywhere; and, with
`BRIEFING_BEFORE_PIN=false`, anything of yours reaching such a call at all.

Out of scope, because it is the design rather than a flaw in it: **a caller with the PIN,
or any content that reaches a subagent, effectively has a shell as you.** The subagents run
with your full access and act on what they read (a transcript, an email, a web page, a
file in a repository), so prompt injection into a task you authorized is inherent. A way
for untrusted content to reach a subagent *without* the PIN is not inherent, and is in
scope.

## Reporting a vulnerability

**Use GitHub's private vulnerability reporting**: the repository's **Security** tab →
**Report a vulnerability**. That opens a private advisory only you and the maintainer can
see, and it is the only channel this project publishes — there is deliberately no email
address here, and no security issue should be opened as a public GitHub issue.

What helps: what an attacker would need to already have (a phone number? a shell on the
host? nothing?), the smallest sequence that reproduces it, and what it gets them. A
proof-of-concept is welcome and never required.

What to expect: an acknowledgement within a week, and an honest answer about whether and
when it will be fixed. This is a personal project maintained by one person in their own
time — there is no bounty, no SLA, and no security team. If a report goes unanswered for
two weeks, please assume it was missed and comment on the advisory again.

Please give a reasonable window before disclosing publicly. Ninety days is more than
enough for anything here; if a fix lands sooner, so does the disclosure.

## Supported versions

The `main` branch, and only `main`. There are no maintenance branches and no backports:
fixes land on `main` and you update by pulling. Tagged releases are markers in that
history, not separately supported lines.

| Version | Supported |
|---|---|
| `main` | ✅ |
| tagged releases | ❌ — update to `main` |

## PIN, brute force and the wake word

Caller ID is spoofable, so anyone who knows an allowed number can reach a phone session, and
from there `JARVIS_PIN` is the only thing between them and a subagent with your access. This
section is what stands behind that PIN, what it costs you, and the one channel that has no
PIN at all.

**Per call.** Three wrong PINs, spoken or keyed, end the call, and a call that has locked
takes no further PIN — not even the right one.

**Across calls.** A per-call limit alone was worth little: three guesses a call is roughly
167,000 calls for a 6-digit PIN, about a day and a half at twenty in parallel. So every wrong
PIN on every call is also counted in `DATA_DIR/pin-failures.json`, which survives a restart:

| Setting | Default | |
|---|---|---|
| `PIN_FAILURE_LIMIT` | 10 | wrong PINs, across all calls, before PIN entry locks |
| `PIN_FAILURE_WINDOW_HOURS` | 24 | how long a wrong PIN is remembered |
| `PIN_LOCKOUT_MINUTES` | 60 | how long PIN entry stays locked |

- While it is locked, **every PIN is refused, the right one included**, before it is
  compared. Jarvis says so in one sentence and ends the call.
- **Nothing resets the count early** — not the lock lifting, not a right PIN. Past the limit,
  each further wrong PIN inside the window locks it again, so a campaign that keeps going
  gets one guess an hour: about 24 a day, which puts a 6-digit PIN decades away and an 8-digit
  one out of reach. A right PIN that reset the count would let each of your own calls hand
  an attacker a fresh ten.
- **You are told once**, when it first locks, and again only if it is still locking a full
  window later: spoken into a call that has already given the PIN (never the one guessing),
  posted to Slack if there is a Slack app, and texted only if `SMS_ENABLED` is on.
- **An unreadable count file** is treated as a lock that began when it was found: one
  cooldown, written back so a restart cannot extend it, and never longer. That is the one
  state where "nobody has guessed" and "someone has nearly used the budget up" look the same,
  and failing open there would make a corrupted file a free reset. A *missing* file is a
  fresh start; a file that cannot be written keeps counting in memory.
- **At most `MAX_PHONE_SESSIONS` (2) phone calls run at once.** A call past that hears that
  the line is busy and never reaches the model, so parallel guessing is capped as well as
  the realtime bill.

**The trade-off you are accepting.** Whoever can make those calls can also keep *your* PIN
locked: one wrong guess an hour holds PIN entry shut for as long as they care to. That is
deliberate. A lock can be waited out or cleared; a guessed PIN hands over the machine. While
it is locked a call still connects and anything that needs no PIN still answers, the wake
word and the terminal are unaffected, and what you lose is whatever the PIN gates on the
phone. What you can do about it: change `JARVIS_PIN` (the alert says to — a campaign that
has spent its guesses has learned nothing about the new one); clear the lock by stopping
Jarvis, deleting `DATA_DIR/pin-failures.json` and starting it again; and take the spoofed
number out of `ALLOWED_CALLERS` until it stops. Raising `PIN_FAILURE_LIMIT` makes you harder
to lock out and the PIN proportionally easier to guess.

**Signature validation cannot be switched off behind a tunnel.**
`DEBUG_SKIP_TWILIO_VALIDATION=true` with `PUBLIC_HOST` set makes `jarvis serve` refuse to
start the phone channel, and `jarvis doctor` reports it as a hard failure; with it off
anyone who can reach the tunnel could pose as Twilio and key PINs in at machine speed.

**Known and not yet fixed.** Keyed digits are checked the moment as many have been typed as
the PIN is long, which tells a caller how long the PIN is. It narrows a search by at most
about a tenth, and with the cross-call count it buys nothing close to a guess; closing it
changes how the owner keys the PIN in, so it is tracked rather than rushed. The media socket
also accepts a connection before the stream token authenticates it, for at most five seconds.

**The wake word has no PIN.** A local session is authorized from its first word, on the
ruling that whoever can speak in the room is you. That means *anything* that reaches the
Mac's microphone and says "hey jarvis" — a video call, a video, a phone on speaker, a
television — can dispatch a subagent running with `bypassPermissions`. That is a property
of the design, documented here rather than hidden; if the machine's microphone hears rooms
or audio you do not control, run `jarvis serve --no-wakeword`. Sound reaching the microphone
and being obeyed is this ruling working as written; a way to authorize a *phone* session
without the PIN is very much in scope.

## Out of scope

- **The subagents having your full access.** They run with
  `permission_mode="bypassPermissions"` (Claude) or
  `--dangerously-bypass-approvals-and-sandbox` (Codex) by design; that is documented, not a
  vulnerability. The PIN gates the dispatch the same way whichever agent runs it, and
  naming an agent out loud is an argument to that one gated tool, not a way around it.
  A way to *reach* that access without the PIN is very much in scope, on either agent.
  So is a subagent's credential reaching its command line, a log or a spoken error: it is
  handed over in the environment only, and what a refused key is quoted back as is redacted.
- **Anything that requires the host account already.** Someone who can read
  `~/.jarvis` or write `~/.claude/settings.json` is already you — deleting
  `DATA_DIR/pin` is that, and it re-opens enrolment for the next caller rather than
  changing the PIN in place (above). Text that reaches a
  Claude Code session on the host — an issue, a pull request, a web page it reads — is
  not that: a way for it to get the approval bridge to ring you about one command and
  run another, or to run something the policy should never have offered, is in scope.
- **Vulnerabilities in dependencies**, unless this project's use of one makes an otherwise
  harmless bug exploitable. Report those upstream; Dependabot watches the versions here.
- **Missing rate limits or hardening on `/health`**, which returns `ok` and a session
  count and nothing else.
