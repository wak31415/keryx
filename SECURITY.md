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
