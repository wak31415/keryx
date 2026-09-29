---
name: jarvis-custom-tools
description: >-
  Write, change or remove one of the owner's own Jarvis voice tools — a Python file in
  Jarvis's data directory (`DATA_DIR/tools`) that the voice model can call on the phone.
  Use when the owner wants Jarvis itself to be able to do or answer something on a call
  ("give yourself a way to check X", "I want to ask you for Y"), not when they want a
  one-off answer or a change to Jarvis's built-in tools.
---

# Writing a Jarvis voice tool

Jarvis's voice model can call two kinds of tools. The built-in ones live in the Jarvis
repository (`src/jarvis/tools/builtin_*.py`) and are not yours to change here. The owner's
own tools are Python files in Jarvis's data directory — `~/.local/share/jarvis/tools/` by
default; the path in your instructions is the one on this machine. They are the owner's
data, not Jarvis's code:

- **Never put one in the Jarvis repository, and never commit one.** The directory is not a
  git checkout and should not become one.
- **No restart.** Every call reads the directory afresh when it starts, so a new or edited
  tool is offered from the next call. Do not write a `RESTART_REQUIRED:` line for it.

## The file

One self-contained module per tool (a file may define more than one, if they belong
together). The file name is free; the tool's name is the function's name unless you pass
`name=`.

```python
"""Tide times for the harbour the owner sails from."""

import httpx

from jarvis.tools.custom import custom_tool

STATION = "8443970"


@custom_tool(
    description=(
        "The next high and low tide at the owner's harbour. Use it when they ask about "
        "the tide or whether they can go out. Say the two times in one sentence and "
        "nothing else; do not read the station or the heights."
    ),
    parameters={
        "type": "object",
        "properties": {
            "day": {"type": "string", "enum": ["today", "tomorrow"]},
        },
        "required": [],
    },
    needs_pin=False,
)
def tides(ctx, args):
    day = args.get("day", "today")
    response = httpx.get(f"https://…/stations/{STATION}", params={"day": day}, timeout=10)
    response.raise_for_status()
    ...
    return {"high": "4:12 pm", "low": "10:30 pm"}
```

- **The handler** takes `(ctx, args)`: the call's `ToolContext` and the model's arguments
  (a dict matching `parameters`). It may be `async def` or plain `def`; a plain one runs in
  a thread, so blocking I/O is fine there. Return a small dict — it is what the model
  speaks from, so short strings and numbers, not a page of JSON.
- **`ctx`** has `ctx.trust` (`jarvis.trust.TrustLevel`), `ctx.channel` (`"phone"` or
  `"local"`), `ctx.caller` and `ctx.session.session_id`. Most tools need none of it.
- **What is importable** is what Jarvis's own environment has: the standard library,
  `httpx`, `numpy`, `pydantic` and Jarvis itself. Nothing else is installed, and you may
  not install anything into Jarvis's environment for a tool.
- **Nothing at import time** beyond definitions: every call imports the file again, so a
  network request or a slow computation at module level delays every call's start.
- **`timeout_s`** (default 20) is how long the caller will wait. A tool that could take
  longer is not a voice tool — the owner should dispatch that as a task instead.
- A file whose name starts with `_` is not loaded: use it for a draft.

## The gate: `needs_pin`

Caller ID is spoofable, so the gate is the one decision here that is about security, and it
is in the code rather than the description.

- **`needs_pin=True`** (the default): the tool answers only once the caller has given the
  PIN. Right for anything that **acts** (sends, books, buys, writes, deletes), and for
  anything that reads the owner's own data — their accounts, messages, files, devices,
  location.
- **`needs_pin=False`**: anyone who rings can use it. Only for what is public anyway:
  weather, tides, a public timetable, a calculation.

When in doubt, leave the default. Never lower a gate because the owner finds the PIN
inconvenient on the phone — say in your report that the tool needs it, and why.

## The wording

The description is the whole of what the voice model knows about the tool. Jarvis's rule
is **one action is one sentence**: the failure is never silence, it is the model saying the
same thing twice, or reading out something nobody asked for. So the description says

1. what the tool gives and **when** to call it (in the owner's words, not an API's);
2. **how to say the result** — usually one sentence;
3. **what not to say**: the raw fields, ids, URLs, units nobody needs.

Parameter descriptions are for the model too: say what a value means, with an `enum` where
there are only a few. A tool that fails returns `{"error": "a sentence the model can say"}`
rather than raising — though a raised exception is caught and reported for you.

## Credentials

A key the tool needs is never in the `.py` file. Put it in a file beside the tool that is
not a `.py` file — `tides.key`, say — readable by the owner alone (`chmod 600`), and read it
in the handler, at call time. If the owner has not given you the key, write the tool,
say in the report where the key goes, and ask for it in your spoken summary. Never put one
in Jarvis's configuration or its `secrets.toml`.

## Check it

The loader refuses, and logs, a file that will not import, defines nothing, reuses a name
(a built-in tool's or another file's), has an invalid description, parameters or `needs_pin`,
or that anybody but the owner could write. Run Jarvis's own check — the exact command is in
your instructions — and fix what it reports until it exits 0 and lists your tool:

    python -m jarvis tools

Then call the handler once yourself, with the arguments a caller would plausibly give,
and read what it returns the way the model would receive it:

    python -c "import runpy; tool = runpy.run_path('tides.py')['tides']; \
      print(tool.handler(None, {'day': 'today'}))"

Wrap the call in `asyncio.run(...)` for an `async def`. Use the same interpreter as the
check, so the imports are the ones Jarvis has.

## Changing or removing one

Edit the file in place; delete it to remove the tool. Either takes effect from the next
call. Renaming a tool changes what the voice model calls it, so say the new name in the
report.

## Your report

Say which file you wrote, the tool's name, its gate, and what the owner can now ask for.
Your spoken summary says what they can ask for and that it works from their next call —
not the file name, and not the gate unless the tool needs the PIN.
