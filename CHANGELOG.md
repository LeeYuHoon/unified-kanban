# Changelog

All notable user-visible changes to this project will be documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases will use [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Unified observation cards for Claude Code, Codex CLI, and Hermes Agent user turns.
- Per-card Skill, subagent, MCP, model, and truthful token-usage metadata.
- Repository-contained setup, uninstall, smoke, and Hermes update workflows.
- Exact supported-upstream pinning and runtime compatibility gates.
- Portable Hermes carried-commit manifest and thin bundle.
- Model-family token summaries and full result/summary Dashboard presentation.
- Filtering of automatic delegation, background, compaction, and worker-only turns.
- Open-source governance, security, contribution, CI, and maintenance documentation.

### Security

- Fail-closed Hermes compatibility checks use a repository-owned pin and no-follow descriptor identity validation.
- Runtime gates bind the frozen pin, selected immutable release, final carried commit, completion
  receipt, and `hermes version` upstream; moving checkout refs are not installation authority.
- Distribution metadata publishes no unguarded mutation console script; repository setup is the
  supported deployment path and direct module execution remains fail-closed.
- Carried bundle commit metadata uses a project noreply identity and preserves the Hermes Agent
  copyright and MIT terms in `THIRD_PARTY_NOTICES.md`.
- An unavailable or mismatched exact frozen upstream object is rejected before updater mutation;
  a later move of official `main` is recorded for the next maintenance cycle.

No release has been tagged yet. The first public release should be `0.1.0` after publication gates in `docs/maintenance.md` pass.
