"""What a pending approval is, and what answering one comes to.

One `ApprovalRequest` is one prompt sitting on the owner's screen that nobody has answered.
It is created by the hook the Claude CLI runs when it is *about* to ask him something, it
lives only in memory (a pending prompt cannot outlive the process that is blocked on it),
and it ends in exactly one `Outcome`.

The full `tool_input` is deliberately **not** here. It can contain the whole contents of a
file, which is the last thing that should reach a phone line or an audit file, so what
survives the classification step is a bounded spoken `summary` plus `input_sha` — enough to
recognise the same request again when Claude reports the tool ran, and nothing more.
"""

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum


class Kind(StrEnum):
    """What answering this request means."""

    #: A tool call waiting for yes or no.
    APPROVAL = "approval"
    #: `AskUserQuestion` / `ExitPlanMode`: a question with options, answered rather than allowed.
    QUESTION = "question"


class Outcome(StrEnum):
    """How a request ended. `PENDING` is the only one that is not final."""

    PENDING = "pending"
    #: A verdict was applied: he answered it on the phone.
    ANSWERED = "answered"
    #: He answered at the keyboard, or the session went away, before we ever applied one.
    RESOLVED_ELSEWHERE = "resolved_elsewhere"
    #: The hook's process disappeared: the prompt is not there to answer any more.
    ABANDONED = "abandoned"
    #: Nobody answered inside the window; the prompt is left exactly as it was.
    EXPIRED = "expired"
    #: He heard it and chose to leave it on screen.
    LEFT = "left"


@dataclass(frozen=True)
class Verdict:
    """What the hook prints back to the Claude CLI, or None for "say nothing".

    `behavior` is the CLI's own vocabulary: `allow` runs the tool, `deny` carries `message`
    back to Claude as feedback — which is how a *question* is answered (spec note: a plain
    `allow` falls through to the on-screen picker for tools that need interaction, so the
    answer has to ride on a denial).
    """

    behavior: str  # "allow" | "deny"
    message: str

    def payload(self) -> dict:
        return {"behavior": self.behavior, "message": self.message}


@dataclass
class ApprovalRequest:
    """One unanswered prompt, and everything that has happened to it so far."""

    id: int
    session_id: str
    cwd: str
    tool_name: str
    kind: Kind
    #: One sentence, safe to say out loud: no file contents, no credentials.
    summary: str
    #: The keypad menu, in order. Digit 1 is `options[0]`; `0` always means "leave it".
    options: list[str]
    input_sha: str
    raised_at: float
    outcome: Outcome = Outcome.PENDING
    escalated_at: float | None = None
    #: How he was told: "call", "announce", or "riding" (a call was already going out).
    escalated_via: str | None = None
    answered_at: float | None = None
    #: The option he chose, for the audit line — never the digit he pressed.
    answer: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def pending(self) -> bool:
        return self.outcome is Outcome.PENDING

    def menu(self) -> str:
        """The options as a spoken sentence: "press 1 to approve, 2 to reject…"."""
        parts = [f"press {index} for {label}" for index, label in enumerate(self.options, 1)]
        parts.append("or 0 to leave it on screen")
        return ", ".join(parts)

    def audit(self) -> dict:
        """The fields every audit line carries about this request."""
        return {
            "request_id": self.id,
            "claude_session": self.session_id,
            "cwd": self.cwd,
            "tool": self.tool_name,
            "kind": str(self.kind),
            "summary": self.summary,
            "input_sha": self.input_sha,
            "outcome": str(self.outcome),
        }


def input_digest(tool_input: object) -> str:
    """A stable SHA-256 of a tool input, so the same call can be recognised again.

    `PostToolUse` hands back the same `tool_input` the request carried, which is how a
    prompt answered at the keyboard is matched to the pending record it resolves. Keys are
    sorted and anything unserialisable is stringified, because a digest that raises would
    cost us the match — and the match is what stops a stale prompt being rung about.
    """
    try:
        canonical = json.dumps(tool_input, sort_keys=True, default=str)
    except (TypeError, ValueError):  # pragma: no cover - `default=str` makes this unreachable
        canonical = repr(tool_input)
    return hashlib.sha256(canonical.encode("utf-8", "surrogatepass")).hexdigest()
