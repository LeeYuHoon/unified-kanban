# Contributing to Unified Kanban

Thank you for improving Unified Kanban. This repository integrates with user-level agent configuration and a pinned Hermes checkout, so changes must be reproducible and fail closed at compatibility boundaries.

## Development setup

Requirements: macOS, Git, Bash, Python 3.11+, `uv`, and a configured Hermes Agent checkout.

```bash
git clone https://github.com/LeeYuHoon/unified-kanban.git
cd unified-kanban
uv sync --frozen --group dev
uv run pytest -o addopts='' -q
```

Use `./scripts/setup.sh --dry-run --no-restart --skip-smoke` before any real installation. Do not run setup against a production home directory while developing installer changes; the installer tests use temporary homes.

## Change workflow

1. Open an issue for behavior or compatibility changes.
2. Create a focused branch from `main`.
3. Add or update tests before changing runtime behavior.
4. Keep implementation in this repository. Installed files should be managed links or narrowly merged hook entries, not copied source trees.
5. Update README/spec/checklists when a user-visible contract changes.
6. Run the quality gates below and include the exact results in the pull request.

## Quality gates

```bash
uv sync --frozen --group dev
uv run pytest -o addopts='' -q
bash -n scripts/setup.sh scripts/uninstall.sh scripts/kanban-smoke.sh \
  scripts/update-hermes-if-needed.sh bin/claude-kanban-hook \
  bin/codex-kanban-hook bin/kanban-adapter
git diff --check
```

For installer changes, also run the setup and updater test modules. For Hermes carried-patch changes, follow `docs/hermes-update-checklist.md` and record real results in a dated verification document.

## Hermes compatibility changes

Never change only `patches/hermes-agent-supported-upstream`. A supported-upstream update requires:

- reviewing the exact upstream diff;
- rebasing and testing every carried commit in manifest order;
- regenerating and verifying the thin bundle;
- running project, installer, updater, Hermes regression, smoke, and Dashboard checks;
- updating the dated verification report.

Unknown, malformed, or unverified compatibility must remain fail closed. Do not add an environment-variable override for the repository-owned pin.

## Code and documentation style

- Add a module docstring describing each non-trivial Python module's responsibility and trust boundary.
- Document public APIs and non-obvious state transitions. Do not add comments that merely restate syntax.
- Keep user data out of logs, tests, fixtures, screenshots, and commits.
- Preserve fail-open behavior for observation hooks, but fail closed for compatibility and explicit management commands.
- Prefer small modules with one reason to change. Record a follow-up issue before splitting a mature module whose behavior is covered but tightly coupled.

## Pull requests

Use the pull request template. A PR is ready only when CI passes, security-sensitive changes have an independent review, and documentation matches the executed commands and counts. Maintainers use squash or focused conventional commits and update `CHANGELOG.md` for user-visible behavior.

By participating, you agree to follow `CODE_OF_CONDUCT.md`.
