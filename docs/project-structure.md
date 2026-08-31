# Project structure

This repository is the sole source of implementation. Runtime databases, agent transcripts, credentials, logs, and generated state stay outside Git.

```text
unified-kanban/
├── bin/                         # symlink-safe user-facing hook/adapter wrappers
├── docs/                        # specification, maintenance, update and verification records
├── integrations/
│   ├── hermes/hermes-kanban/    # Hermes plugin registration and event boundary
│   └── session-viewer/          # read-only cross-provider session viewer
├── patches/                     # exact Hermes pin, carried manifest, portable thin bundle
├── scripts/                     # setup, uninstall, updater, smoke and config merge tools
├── src/kanban_adapter/          # provider normalization, state, usage and Hermes CLI backend
├── tests/                       # adapter, hook, installer, updater and compatibility tests
├── .github/                     # CI, scheduled compatibility checks and contribution templates
├── README.md                    # 한국어 설치 및 사용 안내
├── CONTRIBUTING.md              # development and review policy
├── SECURITY.md                  # private reporting and trust boundaries
├── THIRD_PARTY_NOTICES.md       # Hermes Agent copyright and MIT notice for the carried bundle
├── CHANGELOG.md                 # release-facing changes
├── LICENSE                      # MIT license
├── pyproject.toml               # package metadata and pytest configuration (no public console script)
└── uv.lock                      # reproducible development dependencies
```

## Runtime layers

### Wrappers and installation (`bin/`, `scripts/`)

Wrappers resolve their repository source through installed symlinks and perform runtime Hermes compatibility checks before invoking Python. `setup.sh` merges owned hook entries and links without replacing unrelated user files. `uninstall.sh` removes only artifacts whose ownership it can prove.

The supported deployment path is `scripts/setup.sh`. The wheel is a library/build-integrity
artifact and intentionally publishes no mutation console script because it does not contain the
repository pin, carried manifest, wrappers, or installer. Direct module execution still applies
the compatibility gate and fails closed without those repository policy files.

### Adapter core (`src/kanban_adapter/`)

| Module | Responsibility |
| --- | --- |
| `backend.py` | Validates board/task identities and invokes the Hermes Kanban CLI. |
| `cli.py` | Parses explicit start/update/done/block management operations. |
| `claude_hook.py` | Shared hook state machine, private state files and Claude event handling. |
| `codex_hook.py` | Normalizes Codex event aliases and delegates to the shared state machine. |
| `codex_model.py` | Read-only, session-scoped Codex model and rollout lookup. |
| `hermes_hook.py` | Tracks one Hermes user turn and excludes automatic auxiliary turns. |
| `compatibility.py` | Reads the repository pin safely and enforces exact upstream compatibility. |
| `token_usage.py` | Extracts provider snapshots and computes per-turn deltas. |
| `usage.py` | Sanitizes and serializes Skill/subagent/MCP/model/token summaries. |

Dependencies point inward: wrappers/plugins call adapter modules; adapter modules do not import installer or Dashboard code. The Hermes plugin is intentionally thin and forwards only bounded event fields.

### Hermes carried patches (`patches/`)

The project depends on Hermes behavior not yet guaranteed by upstream. `hermes-agent-supported-upstream` pins the exact reviewed upstream SHA, `hermes-agent-carried-commits` preserves ordered local changes, and `hermes-agent-carried.bundle` makes those objects portable. These three files are one release unit and must never be updated independently.

### Session viewer (`integrations/session-viewer/`)

The viewer is read-only and separately documented. Its current single-file implementation is mature but large; see the maintainability assessment below before adding provider or rendering responsibilities.

## Comments and docstrings

Comments document intent, trust boundaries, failure ordering, provider semantics, and race defenses. They should not narrate obvious syntax. Non-trivial Python modules require a module docstring; public integration APIs and non-obvious state transitions require method docstrings. Tests act as executable documentation for edge cases.

## Maintainability assessment

The core adapter has clear source boundaries and shared Claude/Codex normalization, avoiding three independent state machines. Compatibility, token semantics, and backend CLI calls are separated and well covered. No broad refactor is required before publication.

Known follow-up candidates:

1. `integrations/session-viewer/claude_session_viewer.py` is about 2,800 lines and combines provider parsing, timeline normalization, terminal rendering, and export. Split it by responsibility only through behavior-preserving commits with the existing 159 tests as a baseline.
2. `scripts/setup.sh` is intentionally sequential because write ordering is a security contract. Extracting helpers is acceptable only if preflight-before-write and final identity rechecks remain explicit and tested.
3. `usage.py` is large but cohesive around schema-v2 usage normalization. Split provider adapters only when a fourth provider creates demonstrable conditional complexity.

These are maintainability improvements, not correctness blockers. Avoid speculative rewrites immediately before the first public release.
