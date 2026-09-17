# Open-source readiness review — updated 2026-08-14

> 이 문서는 과거 공개 준비 후보의 검토 이력입니다. 아래 전체 테스트·bundle·빌드·원격 저장소 상태는 당시 관찰에만 해당하며 현재 후보를 보증하지 않습니다. 현재 pin은 [README](../README.md#포함된-hermes-버전)에서 확인하세요. 현재 후보의 전체 검증·운영 확인·공개 승인은 별도로 완료해야 합니다.

## Verdict

당시 저장소는 알파 공개에 필요한 문서·라이선스·기여·보안·CI·유지보수·패키지 메타데이터와 코드 책임 설명을 갖췄다고 평가했습니다. 당시 후보에서 수집한 테스트 결과를 아래에 보존합니다. 현재 후보의 전체 테스트 통과 여부는 이 문서로 판단하지 않습니다. 당시에는 저장소가 비공개였고 후보는 미커밋 상태였으며 해당 파일의 원격 Actions는 실행되지 않았습니다. 현재 원격 상태는 재조회하지 않았습니다. 공개 전에는 같은 동결 소스에 대한 독립 검토와 승인을 별도로 완료해야 합니다.

## 1. README and user documentation

### Before the audit

The Korean README already described the user problem, architecture, installation, privacy, token semantics, Hermes compatibility, updater behavior, uninstall, and real verification evidence. Its main gaps were an English entry point, a navigable repository structure, contribution/security/license links, explicit CI/maintenance policy, and a safe 03:00 automation explanation.

### Added or clarified

- `README.md`: 한국어 프로젝트 소개, 지원 범위, 설치와 사용 안내.
- `docs/project-structure.md`: directory tree, module responsibilities, dependency direction, comments/docstrings policy, refactoring assessment.
- `docs/maintenance.md`: branch/release policy, PR gates, dependencies, security, backup/rollback, maintainer duties, daily upstream workflow.
- `CHANGELOG.md`: Keep a Changelog / semantic-version release record beginning with `Unreleased`.

## 2. Directory and code quality review

### Structure

The repository has useful boundaries:

- `bin/`: installed wrappers only;
- `scripts/`: host configuration, update, uninstall and smoke operations;
- `src/kanban_adapter/`: provider-neutral card, state, usage and compatibility logic;
- `integrations/`: thin Hermes plugin and read-only session viewer;
- `patches/`: one exact Hermes pin plus ordered carried stack and portable bundle;
- `tests/`: unit and temporary-environment integration tests;
- `.github/`: CI, scheduled compatibility check, issue and PR templates.

Generated `src/unified_kanban.egg-info` metadata was removed from version control; `.gitignore` already prevents regeneration from being committed.

### Comments and docstrings

All non-test Python modules now have module-level responsibility descriptions. Critical integration entry points—backend mutations, CLI dispatch, hook dispatch, Hermes lifecycle, compatibility command, plugin registration, symlink management and session-viewer command construction—have concise docstrings. Existing comments explain failure ordering, race defenses, provider token semantics and privacy boundaries rather than restating syntax.

Not every small rendering/property helper receives a docstring. That is intentional: public integration contracts and non-obvious behavior are documented; obvious local helpers are kept readable through names and tests.

### Refactoring assessment

No broad pre-release refactor is recommended. The adapter already shares Claude/Codex lifecycle logic and separates compatibility, backend, token and usage concerns. Large rewrites immediately before publication would increase risk without changing user behavior.

Tracked follow-up candidates:

1. The approximately 2,800-line session viewer combines parsing, normalization, terminal rendering and export. Split it only as behavior-preserving work protected by its 159 tests.
2. `setup.sh` is large, but its visible preflight-before-write order is a security contract. Extract only helpers that preserve and test that order.
3. `usage.py` is sizeable but cohesive. Split provider details only when another provider causes measurable conditional complexity.

These are maintainability items, not release blockers.

## 3. Test execution model

The locked development environment is created with:

```bash
uv sync --frozen --group dev
```

The complete deterministic suite runs with:

```bash
uv run pytest -o addopts='' -q
```

It includes:

- pure normalization/serialization unit tests;
- hook lifecycle, retry and idempotency tests;
- temporary HOME and Git-repository setup/uninstall/updater tests;
- symlink, permission, descriptor-identity and TOCTOU defenses;
- exact Hermes compatibility and installed-wrapper subprocess tests;
- session viewer provider, redaction, CLI and render tests.

Release candidates additionally require local Hermes CLI smoke, setup dry-run, Gateway/Dashboard and browser checks where applicable. CI cannot substitute for those host integration checks.

과거 로컬 후보 증거 (2026-09-04 동결본; 현재 후보의 검증으로 재사용하지 않음):

- full pytest: **1,214 passed, 2 skipped**;
- `uv build`: source distribution and wheel built successfully with the MIT license included;
- shell syntax, Python compile and `git diff --check`: passed;
- the frozen exact pin is provenance-verified against the official repository;
- real immutable-release construction passed; production selector, Gateway, and `hermes --version` binding are separate pre-publication host gates;
- carried manifest/bundle: **13 manifest entries / 13 refs**;
- bundle integrity metadata: **42,585 bytes**, SHA-256
  `9b84e3801bbaf6fcde13879648315680ded6626ae29cedab3ea7919a1b8ba1c8`, prerequisite
  `63279301bcbdc185c1b07b98a9312eb0c862f26d` equals the
  reviewed pin; CI and `tests/test_carried_bundle.py` validate these values, pack checksum, unique
  ordered refs, regular non-symlink inputs, strict metadata, and an isolated Git unbundle;
- fresh bundle import: 13 sanitized project-noreply identities, no personal maintainer email;
- Hermes Agent copyright and MIT terms preserved in `THIRD_PARTY_NOTICES.md`;
- wheel smoke: no mutation console script or embedded mutable pin; direct module CLI failed closed;
- ignored `dist/` build outputs cleared after wheel verification to prevent accidental stale upload;
- GitHub workflow audit (`zizmor`): **no findings**;
- tracked runtime files: none;
- personal home-directory or macOS temporary paths in publication files: none;
- credential scan findings: no real credentials (redaction detector patterns and synthetic test tokens were reviewed as fixtures).

### Independent-review hardening

The first publication reviews failed closed on three real blockers. Runtime compatibility had historically trusted
only `origin/main`, which could be stale while another checkout was active; distribution metadata
published a console script that bypassed the repository wrapper; and compressed carried commit
metadata exposed a maintainer email without preserving the upstream license notice alongside the
bundle. 다음 설명은 당시 후보를 동결하기 전에 수정한 결과에 한정합니다: runtime now binds the frozen pin, selected immutable release, completion receipt, final carried
commit and CLI-reported upstream; moving checkout refs are not authority; the wheel publishes no
mutation console script, while direct module execution applies the same gate and fails closed without
repository policy files; the 13 carried commits use a project noreply identity
with identical final trees, and the full Hermes Agent MIT notice is distributed. New regressions and
fresh-import checks cover stale refs, a different active CLI, the unguarded-entry-point prohibition,
packaged-wheel behavior, ordered bundle refs, identity privacy and third-party notice inclusion.
Earlier independent fail-closed reviews passed some prior-tree corrections, but those verdicts are
not reused for that historical candidate. 더 엄격한 당시 후속 검토에서 오래된 SHA와 검증 기록의 bundle checksum 누락을 발견했습니다.
당시 기록은 13개 manifest SHA를 순서대로 사용하고 다음 검증값을 보존했습니다: checksum,
size and prerequisite; repository tests and the scheduled workflow execute the same deterministic
bundle verifier. Successive focused reviews then exercised undeclared refs, malformed manifests,
duplicate metadata keys and commit SHAs, payload corruption, symlinks, path swaps and same-inode
in-place writes. The verifier now rejects each case, validates pack integrity, and performs an isolated
Git verify/unbundle against the reviewed prerequisite. The final read-only re-review reproduced these
cases and passed with no security, logic or documentation findings.

## 4. Project management and 03:00 automation

### Pull requests and releases

`.github/workflows/ci.yml` runs the locked full suite, shell syntax, Python compile and whitespace checks on macOS. Actions are pinned to full commit SHAs and checkout credentials are not persisted. Dependabot proposes monthly Python and Actions updates with a seven-day cooldown.

Pull requests use a checklist for compatibility, privacy, exact commands and rollback. User-visible work enters `CHANGELOG.md`. The first planned public release is `v0.1.0`; no tag currently exists.

### Daily 03:00 KST

`.github/workflows/hermes-upstream-check.yml` runs daily at 18:00 UTC (03:00 KST). It compares Hermes `main` with the repository-owned reviewed pin and validates manifest/bundle counts.

It deliberately does not move the pin or accept a new upstream automatically. An unreviewed automatic update would invalidate the exact-upstream safety promise. A mismatch becomes a visible failed maintenance run; the maintainer then executes the update checklist, rebases carried commits, regenerates the bundle, runs all gates, and updates the release unit in one reviewed PR.

A local maintainer may schedule `scripts/update-hermes-if-needed.sh` at 03:00. That script never writes into the Hermes checkout. It selects an immutable release built from the reviewed upstream and carried chain, refuses an unsupported upstream before constructing or selecting anything, and skips release construction and service restarts entirely when the reviewed release is already selected. Scheduler installation is not forced on contributors during setup.

## 5. Advantages over the original Hermes Kanban flow

- Records Claude Code, Codex and Hermes user turns in one board while preserving native Hermes dispatch.
- Uses one prompt per observation card instead of treating an entire session as one task.
- Routes by Dashboard project directory without per-project environment configuration.
- Keeps full results accessible while showing bounded summaries in lists.
- Reports model and token families truthfully, preserving unknown/unavailable data instead of fabricating zero.
- Tracks Skill, subagent and MCP usage on the parent card while suppressing duplicate auxiliary-worker cards.
- Keeps observation cards outside dispatcher execution semantics.
- Survives retries through deterministic event IDs and private atomic state.
- Installs from one Git source with dry-run, ownership-aware uninstall and foreign-config preservation.
- Refuses unsupported Hermes upstream revisions at setup, updater and runtime boundaries without blocking normal Hermes use.
- Preserves the carried Hermes patch stack in a reproducible manifest and portable thin bundle.

## 6. Open-source governance files

Added:

- `LICENSE` — MIT (permissive default; confirm before the first public tag if a different policy is desired);
- `CONTRIBUTING.md`;
- `SECURITY.md` with private advisory reporting;
- `CODE_OF_CONDUCT.md`;
- `CHANGELOG.md`;
- `.editorconfig`;
- issue forms and pull-request template;
- CI, scheduled upstream compatibility and Dependabot configuration.

## 7. Remaining publication gates

1. Receive a passing independent review of the current diff.
2. Review the selected MIT license and copyright holder before tagging.
3. Commit the current focused working tree and push it to `main` through the chosen review workflow.
4. Confirm the new GitHub Actions runs pass remotely.
5. Enable branch protection/rulesets for `main`, required CI, and private vulnerability reporting in GitHub settings.
6. Decide whether to rewrite the existing private Git history before publication. Historical Unified
   Kanban commit author metadata contains the maintainer's personal email even though the current
   carried bundle has been sanitized. Rewriting/force-pushing history requires explicit maintainer
   approval; otherwise that attribution email will become public with the repository history.
7. Change repository visibility from **PRIVATE** to **PUBLIC** only after the above checks.
8. Create the first annotated `v0.1.0` tag and GitHub release from the verified commit.

Until these gates are complete, the code is locally publication-ready but should not be described as a released public project.
