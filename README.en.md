# Unified Kanban

[한국어 전체 문서](README.md)

Unified Kanban records real user turns from Hermes Agent, Claude Code, and Codex CLI as observation cards in one Hermes Kanban board. The implementation remains in this Git repository; setup installs managed symlinks and narrowly merged hook entries.

## Why it is different

- **One user prompt, one card** instead of one long card per session.
- **One project route** derived from the Dashboard board's project directory—no per-project environment variables.
- **Truthful telemetry** for Skills, subagents, MCP tools, model identifiers, and input/output/cache/reasoning token buckets. Missing provider data remains unknown rather than becoming zero.
- **Readable results** with a concise summary and an expandable full response.
- **Retry-safe observations** that do not enter the Hermes dispatcher queue.
- **Repository-contained installation** with dry-run, idempotent merge, ownership-aware uninstall, and real smoke verification.
- **Exact Hermes compatibility**: unsupported upstream revisions disable only this integration before it can write incorrect data; normal Hermes use remains available.
- **No auxiliary clutter**: automatic delegation/background/compaction notifications and child-worker turns do not create duplicate cards.

## Supported environment

The installation and real smoke flow are currently verified on macOS with Python 3.11+ and a configured Hermes Agent checkout. Windows and WSL2 are unsupported. Linux is not claimed as supported until a real-machine smoke is recorded.

## Quick start

```bash
git clone https://github.com/LeeYuHoon/unified-kanban.git
cd unified-kanban
./scripts/setup.sh --dry-run --no-restart --skip-smoke
hermes kanban boards create --name "Unified Kanban Smoke" unified-kanban-smoke
./scripts/setup.sh
```

The setup command refuses to modify host configuration unless the frozen official SHA,
repository pin, and carried stack agree. Release construction fetches that exact reviewed SHA from
the fixed official HTTPS repository; a later move of `main` is handled by the next maintenance cycle.
The Hermes checkout is never mutated, so
runtime entry points do not look at its `HEAD`; they require the release selector to name exactly
`<HERMES_AGENT_REPO>.releases/release-<final carried commit>` and that release to carry the
producer's completion receipt for its own directory identity. The wheel is
a library/build artifact and intentionally exposes no mutation console script; supported deployment
uses `scripts/setup.sh`, and direct module CLI execution fails closed without repository policy files.
Setup and smoke never auto-create boards because Hermes does not expose a conditional
creation receipt that can prove rollback ownership. Create the smoke board first; likewise,
`--project-dir ... --board SLUG` requires an existing board.
See the Korean README for complete prerequisites, troubleshooting, Dashboard setup, update behavior,
and uninstall instructions.

## Development

```bash
uv sync --frozen --group dev
uv run pytest -o addopts='' -q
```

- [Architecture and project structure](docs/project-structure.md)
- [Maintenance and release process](docs/maintenance.md)
- [Hermes update checklist](docs/hermes-update-checklist.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Changelog](CHANGELOG.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)

Licensed under the [MIT License](LICENSE).
