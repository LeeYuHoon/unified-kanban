# AI Session Viewer

A small, dependency-free CLI that turns local AI coding-agent sessions into
something a human can actually read: a session list, the real prompts you typed,
a prompt/reply timeline, and Markdown or self-contained HTML exports.

Three providers, one normalized view:

| provider | source | default location |
| --- | --- | --- |
| **Claude** (Claude Code) | JSONL transcripts | `~/.claude/projects` |
| **Codex** (OpenAI Codex CLI) | JSONL rollouts | `~/.codex/sessions` |
| **Hermes** (Hermes Agent) | SQLite database | `~/.hermes/state.db` |

Read-only by construction. Python 3.9+ standard library only (3.8 also works).
No install step, no packages, no server, no network access.

## Unified Kanban usage

```sh
./scripts/setup.sh
ai-session-viewer --help
# Direct repository execution without setup:
./bin/ai-session-viewer --help
```

The implementation remains self-contained under `integrations/session-viewer`;
the repository-owned `bin/ai-session-viewer` wrapper is what setup installs.

### Commands

```sh
# 1. Everything you have, from every provider
ai-session-viewer list --provider all

# 2. One provider at a time
ai-session-viewer list --provider claude
ai-session-viewer list --provider codex
ai-session-viewer list --provider hermes

# 3. Just the prompts you actually typed
ai-session-viewer prompts 3f9c1a --provider claude
ai-session-viewer prompts codex:01983b2e          # provider-qualified
ai-session-viewer prompts hermes:142

# 4. Prompts paired with the agent's visible replies
ai-session-viewer timeline codex:01983b2e
ai-session-viewer timeline hermes:142 --show-activity   # add tool names

# 5. Export (--out is mandatory; must be outside ~/.claude, ~/.codex, ~/.hermes)
ai-session-viewer export claude:3f9c1a --format markdown --out /tmp/claude.md
ai-session-viewer export codex:01983b2e --format html   --out /tmp/codex.html
ai-session-viewer export hermes:142     --format html   --out /tmp/hermes.html
```

`<selector>` is any unique substring of a session ID or of a transcript
filename, optionally prefixed with `claude:`, `codex:` or `hermes:`. Zero
matches and ambiguous matches both fail with a message that tells you what to do
next; ambiguous matches list **provider-qualified** candidates:

```
error: Multiple sessions match '01983' (2). Be more specific, or qualify it with
a provider (e.g. codex:01983b2e-…):
  codex:   01983b2e-c0de-4444-aaaa-bbbbccccdddd  (rollout-2026-07-03….jsonl)
  hermes:  101                                    (Refactor the uploader)
```

### Flags

| flag | meaning |
| --- | --- |
| `--provider {claude,codex,hermes,all}` | which agent to read (default: `all`) |
| `--root PATH` | location for the *selected* provider — a directory for claude/codex, a `state.db` for hermes. Cannot be combined with `--provider all`. |
| `--claude-root` / `--codex-root` / `--hermes-root` | per-provider overrides, usable together with `--provider all` (handy for fixtures and backups) |
| `--raw` | disable redaction |
| `--home DIR` | override the home directory used for redaction and output-path checks (mostly for testing) |
| `--show-activity` | include tool names in the hidden-activity summary (`timeline`) |
| `--force` | allow overwriting an existing `--out` file (`export`) |

## What it reads, and what it never writes

* JSONL transcripts are opened `r` (read-only), streamed line by line, closed.
* The Hermes database is opened through a **`file:…?mode=ro` SQLite URI**, so the
  connection itself is incapable of writing; `INSERT` through it raises
  `OperationalError`. Only `active = 1` rows are read, in insertion (`id`) order.
* Nothing under `~/.claude`, `~/.codex` or `~/.hermes` is ever created, modified
  or deleted.
* `--out` is **required** for file output. The destination is expanded and
  resolved (`~`, `..`, symlinks) and rejected if it lands inside *any* of the
  three provider data directories; the error names which one:
  `Refusing to write inside the Codex data directory (/home/you/.codex): …`
* Existing files are not overwritten without `--force`. Even with `--force`,
  output is rejected when it is a loaded source file or a symlink/hard-link
  alias to one. Exports are written to a mode-`0600` temporary file and
  atomically replaced only after the destination identity is rechecked.
* `~/.codex/history.jsonl` and `~/.codex/session_index.jsonl` are bookkeeping
  indexes, not sessions, and are never scanned as transcripts.
* `integrations/session-viewer/setup.sh` is an optional standalone self-test;
  the repository-level `scripts/setup.sh` installs the managed command link.

## How the output is built

One normalized `Session` / `Event` / `Turn` model is shared by all three
providers; the adapters only translate. There is a single Markdown renderer, a
single HTML renderer and a single terminal renderer.

**Tolerant normalizer.** Each line/row is parsed independently. Malformed lines
(including the truncated final line of a session still being written) are
skipped and *counted* — the count appears in `list`, `prompts`, `timeline`, and
every export. Blank lines are ignored and not counted as malformed. Content is
accepted as a string, a single block dict, a list of blocks, or a JSON-encoded
column; missing and unknown fields degrade to empty values instead of raising.
File / insertion order is preserved as the logical order — no re-sorting by
timestamp.

**Compaction boundaries** are always shown as their own explicit marker with the
carried-forward summary, and are never merged into the surrounding prompt or
reply: Claude `type: "summary"` / `isCompactSummary`, Codex `compacted` records
and `event_msg context_compacted`, Hermes rows with `compacted = 1`.

**Session identity.** A recorded session id wins when present; the filename is
only a fallback (flagged as `id from filename`). Filenames are not assumed to
equal session IDs, and Claude Code's project-directory name encoding is not
assumed to be reversible — the `cwd` shown is the `cwd` recorded *inside* the
session data, or `(no cwd)`. Where the provider records a title, model or
source, they are shown too (Hermes titles, Codex `model_provider`/`source`).

### Prompt filtering (heuristics, not exact)

Every agent writes many user-role records you never typed. The rules are
per-provider, printed with the output, and embedded in every export.

**Claude Code**

| reason | what it is |
| --- | --- |
| `not_user_role` | top-level `type` is not `user` |
| `meta` | carries `isMeta` (resume notices, checkpoints) |
| `sidechain` | carries `isSidechain` (subagent conversation) |
| `tool_result` | content holds only `tool_result` blocks |
| `system_reminder_only` | content is nothing but `<system-reminder>` blocks |
| `slash_command` | starts with `<command-name>` / `<command-args>` |
| `local_command_output` | starts with `<local-command-stdout/stderr>` |
| `hook_output` | starts with a `<…hook…>` wrapper |
| `caveat_preamble` | the "Caveat: The messages below were generated…" preamble |
| `empty` | nothing visible left after stripping the wrappers above |

A `<system-reminder>` embedded in an otherwise real prompt is stripped and the
human text is kept.

**Codex CLI** — only `event_msg` records with `payload.type = user_message` are
treated as human input.

| reason | what it is |
| --- | --- |
| `duplicate_response_item` | a `response_item` `message` with `role: user` replaying a prompt already logged as an `event_msg` |
| `developer_context` | a `response_item` `message` with role `developer`/`system` (instructions, tool policy) |
| `environment_context` | a `user_message` that is only an `<environment_context>` / `<user_instructions>` block injected by the CLI |
| `empty` | nothing visible left |

**Hermes Agent** — `messages` rows with `role = 'user'`.

| reason | what it is |
| --- | --- |
| `inactive` | `active = 0` rows are rolled back and never loaded at all |
| `hidden_display_kind` | `display_kind` marks the row hidden/internal/system |
| `system_role` | `system` / `developer` rows are context, not prompts |
| `empty` | the decoded content held no visible text |

### Assistant text and honest status

Only visible assistant text is shown — Claude `text` blocks (last one per turn),
Codex `event_msg agent_message`, Hermes assistant row content. Reasoning
(`thinking`, `agent_reasoning`, `reasoning`, `reasoning_content`), tool inputs
and tool output are **counted, never printed**. Subagent (sidechain) replies are
counted, not printed. Every turn carries a status derived from the source, never
an assumption of success:

| provider | complete | not complete |
| --- | --- | --- |
| Claude | `completed (end_turn / stop_sequence)` | `stopped early (stop_reason=max_tokens)`, `transcript records an error`, `no stop_reason recorded (possibly truncated)`, `no assistant reply recorded` |
| Codex | `completed (task_complete)` | `aborted before completing (reason=interrupted)` from `turn_aborted`, `no completion signal recorded (incomplete)` when `task_complete` never arrives, `transcript records an error` |
| Hermes | `completed (stop / end_turn / stop_sequence)` | `incomplete: last step was a tool call, no final reply recorded` for `finish_reason=tool_calls`, `stopped early (finish_reason=length)`, `transcript records an error` for error-like reasons |

Hidden activity is summarised compactly (`2 tool calls (read_file, grep);
1 tool result (omitted); 2 reasoning blocks`) — collapsible in HTML, one dim
line in the terminal.

## Privacy and limitations

* **Redaction is ON by default and is best-effort only.** It shortens your home
  directory to `~` and masks common secret shapes: `Bearer <token>`, `sk-…` and
  `sk-ant-…` keys, AWS `AKIA`/`ASIA` IDs, GitHub `ghp_`/`github_pat_` tokens,
  Slack `xox…` tokens, Google `AIza…` keys, `api_key = …` assignments, and PEM
  `PRIVATE KEY` blocks. **It is not a guarantee.** Secrets in unusual formats,
  in prose, or inside pasted code will survive. Review any file before sharing
  it, especially before attaching one to an issue.
* `--raw` disables redaction entirely. Exports made with `--raw` say
  `REDACTION: OFF` in the header.
* **Prompt filtering is heuristic**, over undocumented fields that these tools
  change between releases. It can drop an unusual real prompt or keep an unusual
  synthetic one. The caveat is printed with every command.
* Sessions routinely contain source code, file paths, credentials you pasted,
  and command output. Treat both the sessions and these exports as sensitive.
* **HTML exports** are single files with no external assets, no CDN references,
  no `<img>`/`<link>` tags, and no network calls. Every piece of generated text
  is HTML-escaped; raw transcript JSON, Codex payload objects and Hermes
  database rows are never embedded. A `Content-Security-Policy` meta tag pins
  `default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`.
  One exception, stated plainly: the search box needs `script-src
  'unsafe-inline'` for a ~12-line inline filter function. If you want a
  zero-script file, export Markdown instead.
* Exports are a *view*, not an archive. Tool calls, tool results, reasoning and
  subagent transcripts are deliberately omitted, so an export cannot reconstruct
  the session.
* Timestamps are rendered in UTC. Values that cannot be parsed are passed
  through verbatim rather than guessed at.
* Provider schemas are moving targets (observed: Codex CLI 0.145, Hermes Agent
  0.19). Unknown record types degrade to counted-but-unrendered events rather
  than errors.

## Migration from "Claude Session Viewer"

Nothing was removed.

* The public repository command is `ai-session-viewer`; the internal
  implementation filename remains `claude_session_viewer.py` for compatibility.
* `list`, `prompts`, `timeline`, `export` and all their flags behave as before.
* `--root DIR` **without** `--provider` still means Claude, exactly as it did.
  `--root` defaults to `~/.claude/projects`.
* What changed: `--provider` now exists and defaults to `all` when you do *not*
  pass `--root`, so a bare `list` shows Claude, Codex and Hermes together;
  output carries a provider badge and the assistant is labelled `Claude`,
  `Codex` or `Hermes`; the output-path guard now also covers `~/.codex` and
  `~/.hermes`.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

159 tests, standard-library `unittest`, driven entirely by synthetic fixtures
under `integrations/session-viewer/tests/`.
The suite never reads a real session and never writes outside its own temporary
directories. It includes a read-only assertion (the fixture tree is
byte-and-mtime identical after every command), an assertion that transcripts are
never opened in a writable mode, an assertion that the Hermes database is opened
through a `mode=ro` URI and cannot be written through, and output-path guard
coverage for all three provider data directories.
