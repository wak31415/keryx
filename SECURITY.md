# Security policy

Jarvis answers a phone number and hands what it hears to a subagent running with your full
user access. Please treat a bug in the PIN gate, the Twilio signature check, the stream
token, the approval bridge's policy allowlist, or the report links as a security issue
rather than an ordinary one.

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
  `~/.jarvis` or write `~/.claude/settings.json` is already you.
- **Vulnerabilities in dependencies**, unless this project's use of one makes an otherwise
  harmless bug exploitable. Report those upstream; Dependabot watches the versions here.
- **Missing rate limits or hardening on `/health`**, which returns `ok` and a session
  count and nothing else.
