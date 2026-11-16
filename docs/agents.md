# Agent discovery and historical sources

Station Search discovers live agents from Herdr metadata, including the status application's id, and reads terminal text through `hc`. It does not limit discovery or `/agent-id` filters to a hard-coded allowlist. Unknown agents use a generic terminal icon. The filter catalog is refreshed when the dialog opens; additional agent types found in results are added while searching.

## Built-in history readers

| Agent | Source | Resume action |
| --- | --- | --- |
| Claude Code | `~/.claude/projects/*/*.jsonl` | `claude --resume <id>` |
| Codex | `$CODEX_HOME/sessions/**/rollout-*.jsonl` | `codex resume <id>` |
| Hermes Agent | `$HERMES_HOME/state.db`, read-only | `hermes --resume <id>` |
| Antigravity CLI | `$ANTIGRAVITY_HOME/conversation_summaries.db`, read-only | `agy --conversation <id>` |
| Pi | `$PI_CODING_AGENT_DIR/sessions/*/*.jsonl` | `pi --session <file>` |

Default homes are `~/.codex`, `~/.hermes`, `~/.gemini/antigravity-cli` and `~/.pi/agent`, respectively. Existing index databases migrate automatically; deleting your cache is not required.

Antigravity provides searchable `user_body` and `agent_body` chunks through its own `conversations_fts` table. Station Search joins these to conversation titles and decoded workspace URIs. A preview record can contain several original messages; the mod cannot reconstruct exact per-message boundaries or timestamps from these chunks. It does not parse private trajectory protobuf payloads. Conversations absent from Antigravity's native index are not imported. This reader targets Antigravity CLI's format, not arbitrary desktop IDE databases.

An open Antigravity conversation is associated with its terminal when the foreground argv includes `--conversation <id>` or `--conversation=<id>`. Pi supports `--session` ids or file paths. Claude, Codex and Hermes retain their existing mappings. Every other live agent can still be searched/focused without a history mapping. No agent is launched merely to discover it.

Each source is updated transactionally. If its database is locked, missing a required table or malformed, other sources remain available and its last successful import is preserved. Diagnostic warnings go to stderr. Sources deliberately removed from the configuration/filesystem are removed from the search index on the next update.

## Add another historical format through exports

Put an array of source definitions in `~/.config/station-search/sources.json`, or set `STATION_SEARCH_SOURCES` to another configuration file. No personal source configuration is shipped or installed automatically. Export/convert another agent's histories to the schema below; pointing at arbitrary proprietary JSON or databases does not automatically parse them.

```json
[
  {
    "agent": "my-agent",
    "paths": ["~/agent-exports/**/*.json"],
    "resume": ["my-agent", "resume", "{id}"]
  }
]
```

Agent ids are lowercase letters/digits, hyphens or underscores, beginning with a letter. Use a distinct id from the built-in readers. Paths support home/environment expansion and recursive glob patterns. Use a narrow export directory so unrelated files are not scanned. The optional `resume` field is an argv array, passed directly to Herdr's new-tab command when you click a result; it is not a shell string. Only `{id}` and `{path}` are replaced. Review the executable/arguments before enabling them. Omit `resume` for read-only previews.

A normalized `.json` file contains one session:

```json
{
  "id": "session-001",
  "cwd": "/projects/example",
  "title": "Fix the login flow",
  "messages": [
    {"role": "user", "content": "Check the login flow", "timestamp": "2026-11-16T10:00:00Z"},
    {"role": "assistant", "content": "The login fix is ready", "timestamp": "2026-11-16T10:01:00Z"}
  ]
}
```

Alternatively, a `.jsonl` file begins with a `{"type":"session","id":"session-001","cwd":"/projects/example"}` record, followed by one `{"role":"user","content":"…","timestamp":"…"}` or assistant record per line. Message envelopes of the form `{"type":"message","message":{...}}` are also accepted. `content` can be text or an array of text blocks. Only user/assistant roles are indexed. Prefer UTC ISO timestamps; absent timestamps remain blank.

Source definitions are trusted local configuration. No account credentials belong in them. Exports and the resulting local search index contain conversation text; keep them private and outside the public repository.
