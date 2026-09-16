# Update Jarvis's memory

A call just ended. Fold what happened in it into the memory document Jarvis reads at the
start of every future call, so the next conversation opens knowing him rather than
starting from nothing.

## The two files

- **The transcript of the call that just ended:** `{transcript_path}`
- **The memory document to update:** `{memory_path}`

Read the transcript first. Then read the memory document if it exists — if it does not,
create it with the structure below. Rewrite it in place with the Write tool when you are
done. That file is the entire deliverable: nothing else on the machine should change.

## What the memory is for

It is loaded into the system prompt of a *spoken* assistant, on every call. So it is
written for someone who has thirty seconds to skim before the phone is answered, not for
an archive. Facts that will still matter in a month, and enough about the last few days
that "the thing we talked about yesterday" resolves to something.

Keep this exact structure:

```
# What Jarvis knows about {owner}

## Standing facts
## Ongoing threads
## Recent calls
```

- **Standing facts** — things that stay true: how he works, what he cares about, people
  and places and tools that keep coming up, preferences he has stated. Merge new facts
  into the existing lines rather than repeating them. Delete one when the call shows it
  is no longer true.
- **Ongoing threads** — open loops. What he is in the middle of, what he is waiting on,
  what he said he would come back to. Each line dated. Remove a thread the moment a call
  shows it closed; a stale open loop is worse than no note at all.
- **Recent calls** — one short paragraph per call, newest first, dated, saying what it
  was about and what came of it. Keep at most the last fifteen; drop the oldest, but
  before you drop one, promote anything in it that is still true into the sections above.

## Rules

- **Under {max_chars} characters, total.** This is the hard constraint. When it does not
  fit, compress the oldest calls and merge duplicate standing facts — do not simply
  truncate the file.
- Write plain sentences. No markdown beyond the headings and simple bullets: it is read
  by a model that speaks out loud, and it must not be tempted to read punctuation.
- Record what he said and what happened. Never record a PIN, a token, a password, or the
  contents of the env file, even if one appears in the transcript.
- Do not invent. A call that was two seconds of silence gets no entry, and a call whose
  transcript you cannot read means you leave the memory exactly as you found it.
- Never delete the file, and never leave it empty or half-written. If you cannot improve
  it, write it back unchanged.

## This call

- Session id: `{session_id}`
- Channel: {channel}
- Ended because: {reason}
- Tasks dispatched during it: {tasks}
