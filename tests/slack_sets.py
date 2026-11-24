"""The selections of Slack permissions the tests name: every combination there is, and
the few that are said by name. What an owner ticks is a set (`integrations.slack`)."""

from itertools import combinations

from keryx.integrations.slack import BASE, EVERYTHING, PERMISSIONS, Permission, ordered

#: One box each.
REQUESTS = frozenset({Permission.REQUESTS})
READ = frozenset({Permission.READ})
SEND = frozenset({Permission.SEND})
#: Requests and reading together: the pair that also makes a mention a request.
READS = REQUESTS | READ
#: All eight: nothing ticked first, everything last.
SELECTIONS = [frozenset(some) for count in range(len(PERMISSIONS) + 1)
              for some in combinations(PERMISSIONS, count)]

__all__ = ["BASE", "EVERYTHING", "READ", "READS", "REQUESTS", "SELECTIONS", "SEND", "named",
           "stored"]


def named(allowed) -> str:
    """A selection as a test id."""
    return "+".join(ordered(allowed)) or "nothing"


def stored(allowed) -> list[str]:
    """A selection as the plugin's file holds it: `allow`."""
    return [str(each) for each in ordered(allowed)]
