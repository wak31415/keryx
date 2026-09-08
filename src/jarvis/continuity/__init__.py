"""What survives the end of a call, and how the next one gets it back (spec §3.3).

A realtime session starts blank — the provider keeps nothing across sockets — so
everything Jarvis knows at the top of a call is assembled from disk, every time. Five
modules, and the first three are the three pieces CLAUDE.md names:

- `briefing` builds what a session opens with: the digest of tasks that finished
  unreported, and the memory.
- `memory` is `data_dir/memory.md` — the file API, and the subagent dispatched on
  `SessionEnded` to fold the call that just ended into it.
- `recall` searches past transcripts and past task summaries on demand, literally rather
  than fuzzily, because the query is speech that transcription has already mangled once.
- `transcripts` is the per-session call log the other three read from.
- `retention` prunes exactly these artefacts, and is off by default.

Nothing is re-exported here; import from the modules themselves. A package whose members
must be importable independently re-exports nothing, and `transcripts` is the case that
matters: `notify/notifier.py` and `restart/coordinator.py` both want `read_tail` and
neither wants the memory subagent.
"""
