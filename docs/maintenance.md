# Project maintenance

## Branch and release policy

- `main` is the intended releasable integration branch. Before publication, configure a
  GitHub ruleset that requires CI and review; until then this is policy, not an enforced setting.
- Work happens on focused branches with tests and documentation in the same pull request.
- User-visible changes are recorded under `CHANGELOG.md` → `Unreleased`.
- Releases use semantic versions and signed/annotated Git tags after the publication gate passes.
- The first intended public tag is `v0.1.0`; no release is currently tagged.

## Required pull-request gates

1. Locked development environment: `uv sync --frozen --group dev`.
2. Full test suite: `uv run pytest -o addopts='' -q`.
3. Shell syntax, Python compile, and `git diff --check`.
4. Installer/updater focused tests when host configuration or Hermes compatibility changes.
5. Real setup dry-run and Kanban smoke for release candidates.
6. Dashboard/browser checks for UI carried-patch changes.
7. Independent security/logic review for compatibility, installer, updater, or data-boundary changes.

CI reproduces deterministic tests on macOS. A green CI job does not replace real local Hermes/Gateway/Dashboard smoke evidence.

## Daily 03:00 KST automation

`.github/workflows/hermes-upstream-check.yml` runs at 18:00 UTC, which is 03:00 KST the following day. It checks the current Hermes `main` SHA against the repository-owned supported pin and verifies manifest/bundle structure.

This job intentionally does **not** move the pin, rewrite the bundle, or publish a release. Automatically accepting an unreviewed Hermes revision would contradict the exact-upstream safety contract. A mismatch is a maintenance signal and must fail visibly.

A maintainer then follows this sequence:

1. Read the upstream diff from the old pin to the candidate SHA.
2. Rebase carried commits in manifest order and resolve conflicts deliberately.
3. Run the project, Hermes regression, updater, setup, smoke, and Dashboard gates.
4. Regenerate and independently verify the portable thin bundle.
5. Update the pin, manifest, bundle, checklist, test cases, and dated verification report in one PR.
6. Merge only after CI and independent review pass.

For a trusted maintainer workstation, `scripts/update-hermes-if-needed.sh` may be scheduled locally at 03:00. Its pre-mutation gate updates only when fetched `origin/main` equals the already-reviewed repository pin; an unknown newer upstream is refused before `hermes update`, marker creation, or checkout mutation. Local scheduling is not installed by default because modifying a contributor's scheduler is a separate operational consent boundary.

## Dependency management

- `uv.lock` pins development dependencies.
- Dependency changes go through pull requests and the full suite.
- Hermes is not a loose package dependency: compatibility is pinned to an exact upstream Git SHA.
- GitHub dependency updates may propose Python/Actions changes, but they do not bypass tests or review.

## Security and privacy management

- Security reports use private GitHub advisories (`SECURITY.md`).
- Never commit `.env`, Kanban databases, transcripts, credentials, caches, local paths, or generated session state.
- Observation hooks fail open so coding tools remain usable; explicit compatibility and management gates fail closed.
- Release review includes a tracked-file secret/path scan and bundle/manifest verification.

## Backup and rollback

Runtime Kanban databases are user data and are not kept in Git. Back them up separately before upgrades. `scripts/uninstall.sh` removes managed integration links/config entries but preserves boards and cards. Git tags plus the carried bundle provide implementation rollback; a Hermes rollback must restore the matching upstream pin and carried stack together.

## Maintainer responsibilities

The current maintainer is `@LeeYuHoon`. Maintainers triage issues, preserve scope and compatibility boundaries, publish security fixes, keep the changelog and verification reports factual, and avoid claiming support for platforms that have not been exercised. New maintainers should be added through a reviewed governance change rather than inferred from contribution volume.
