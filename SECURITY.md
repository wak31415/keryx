# Security policy

Jarvis answers a phone number and hands what it hears to a subagent running with your full
user access. Please treat a bug in the PIN gate, the Twilio signature check, the stream
token, the approval bridge's policy allowlist, or the report links as a security issue
rather than an ordinary one.

## Threat model: the phone before the PIN

**Caller ID is spoofable.** `ALLOWED_CALLERS` keeps strangers from reaching the voice
model at all, but anyone who fakes an allowed number does reach it, and the source being
public makes that cheap. So the allowlist is not authentication; the PIN is. The rule the
code holds: **on the phone, before the PIN, nothing private is read out, nothing is
announced into the call, and nothing the caller says or does outlives the call.**

- **Nothing private is read out.** A phone call opens without what Jarvis remembers, the
  results you have not heard, and your project names, project briefs and installed skills;
  they reach the session only once the PIN is accepted. Every voice tool answers
  `pin_required` until then, except five that read nothing of yours or are how a call gets
  past the PIN: `check_billing`, `cluster_stats`, `web_search`, `submit_pin` and
  `end_session`. A test walks every registered tool, so a new one is gated unless it is
  added to that list on purpose.
- **Nothing is announced into the call.** Finished tasks, restart confirmations and
  approval escalations are spoken only into sessions past the PIN, and a session that has
  not given it never counts as having told you: the call-back is still placed and the
  approval still rings.
- **Nothing outlives the call.** No memory update is dispatched for it, no result can be
  marked as heard, no call-back or call-back note can be arranged, and nothing is sent to
  Slack. Its transcript is kept on disk, as the record of what was tried, but it is marked
  as never authorized and `recall` never reads it back to a model.
- **Calls Jarvis places itself** (a call-back, a restart's confirmation, an approval
  escalation) go to your own number or to one named by a caller past the PIN, and open
  with the reason for the call before any PIN; that is what they are for. Anything more
  still needs it.
- **A spoken PIN is never written down.** Transcript lines are redacted as they are
  written, in digits or in words, and everything that reads a transcript back to a model
  redacts again, because logs from before this still hold it. No log line carries it.

In scope, then: anything that gets a caller who has not given the PIN a private fact, an
announcement, or a change that persists past the call. That **includes the memory
writer**, a `bypassPermissions` subagent whose whole input is a call's transcript, and
which must run only for calls that were authorized.

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
  `permission_mode="bypassPermissions"` by design; that is documented, not a vulnerability.
  A way to *reach* that access without the PIN is very much in scope.
- **Anything that requires the host account already.** Someone who can read
  `~/.jarvis` or write `~/.claude/settings.json` is already you. Text that reaches a
  Claude Code session on the host — an issue, a pull request, a web page it reads — is
  not that: a way for it to get the approval bridge to ring you about one command and
  run another, or to run something the policy should never have offered, is in scope.
- **Vulnerabilities in dependencies**, unless this project's use of one makes an otherwise
  harmless bug exploitable. Report those upstream; Dependabot watches the versions here.
- **Missing rate limits or hardening on `/health`**, which returns `ok` and a session
  count and nothing else.
