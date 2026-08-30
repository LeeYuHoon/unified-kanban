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
5a. Real release production for release candidates and for any change to the pin, the carried
   bundle, or `scripts/hermes-release-manager.py`:
   `UNIFIED_KANBAN_TEST_HERMES_SOURCE=<repo holding the reviewed upstream>
   UNIFIED_KANBAN_TEST_HERMES_REQUIRED=1 uv run pytest -o addopts='' -q
   tests/test_hermes_release_integration.py`.
   CI skips this file because it needs a local Hermes object database; the `REQUIRED` variable
   turns that skip into a failure so the local gate cannot pass by skipping it.
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

For a trusted maintainer workstation, `scripts/update-hermes-if-needed.sh` may be scheduled locally at 03:00. It never mutates the Hermes checkout: `HERMES_AGENT_REPO` is a read-only input that only names the sibling release root, and a newer live upstream is recorded for the next maintenance cycle without invalidating the reviewed frozen snapshot. An unavailable or mismatched exact frozen object is refused before any release is constructed. An update builds a fresh immutable release, then activates it by swapping the `current` selector inside a path transaction, so a failed prepare, activation, restart, or health check leaves the previously selected release in place. Local scheduling is not installed by default because modifying a contributor's scheduler is a separate operational consent boundary.

The selector is the applied state; there is no separate applied-SHA file to drift out of agreement with it.

### Guarded immutable-release garbage collection

Release construction and update activation never delete a previously published release. A maintainer may instead run `python3 scripts/hermes-release-manager.py gc <HERMES_AGENT_REPO>` for a deterministic, write-free JSON plan. Planning validates the release-root ancestry and each candidate's private completion receipt and sealed contents; it preserves unrecognized, incomplete, malformed, tampered, or foreign entries rather than treating them as candidates.

Deletion is a separate macOS-only action using the same command with `--apply`. Apply holds `$HERMES_HOME/state/hermes-kanban-update.lock`, retains descriptor and inode authority for the release root, and protects `current` and `previous` together with references found in the configured or loaded launchd service and stable same-UID runtime process and open-file probes. An unavailable, malformed, or non-convergent reference probe prevents deletion of the affected candidate and aborts subsequent work; candidates already deleted earlier in the same apply run are not restored.

A candidate is first moved descriptor-relatively to a fresh private retirement name and bound to a durable retirement record. References and the shared lock are checked again before deletion; a newly referenced release is restored only when its canonical pathname is still absent and owned by that operation. On crash recovery, the record, retirement identity, complete receipt and contents, and fresh runtime references are revalidated before deletion resumes. A partial or tampered retirement is retained with its record and is neither deleted nor restored to the canonical pathname. Foreign successors, replacement roots, malformed records, and indeterminate recovery state are preserved for deliberate inspection.

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

### macOS local-filesystem threat boundary

Setup and update transactions treat canonical pathnames as raceable. Managed namespace
changes use operation receipts, retained directory descriptors, expected inode identities,
durable rollback, and foreign-successor preservation. Fresh 128-bit retirement names are
private capabilities only for the immediate operation that creates and verifies them.

An operation receipt is never its own rollback authority: it records one operation but carries no
snapshotted entries and no ledger token. A setup interrupted between an operation and its
checkpoint is therefore recovered by validating the leftover stage receipt exactly as a checkpoint
would — against the manifest the token ledger still authorizes — restoring the changed paths from
the manifest baseline, and only then rolling the manifest back. A leaf that a third party
substituted in the meantime stops matching the operation's recorded identity, so recovery refuses
and names that leaf instead of adopting or deleting the successor.

The managed Hermes launcher validates its selector, release directory, and executable
immediately before invocation. It deliberately does not read the completion receipt: proving a
release complete means re-hashing its whole dependency tree, which is not viable on every CLI
invocation. The receipt is validated where that cost is affordable — by the release manager before
a release may be reused, and by the runtime compatibility gate before a Hermes turn or an adapter
command records anything. Darwin provides neither `fexecve` nor
`execveat(AT_EMPTY_PATH)`, so an unprivileged launcher cannot bind the final interpreter
execution to an already-open executable inode. A continuously racing same-UID process that
changes the runtime between this validation and `exec`, arbitrary same-UID namespace
surveillance, and ptrace-style process control are therefore outside the supported threat
model. Transaction-time pathname substitution and foreign successors remain in scope and
must still fail closed; this exception does not permit setup or update code to overwrite or
delete an inode it did not create and revalidate.

Release construction also runs the release-local Python over every source file before publishing
the completion receipt. It forces checked-hash bytecode, rejects a missing, orphaned, malformed,
source-mismatched, or non-stable-path `.pyc`, fsyncs the bytecode and its directory chain, and binds
a bytecode inventory into the receipt. Before computing that receipt it also durably writes Hermes'
own `.bytecode-fingerprint` value for the exact `refs/heads/main` carried commit. Hermes' full CLI
entry point otherwise treats a missing stamp as a checkout change and removes every non-venv
`__pycache__` on its first launch, violating both setup idempotency and the sealed release digest.
The stamp is a single-link private regular file, is included in the release digest, and is verified
again on reuse; its publication follows the same identity-bound persist-or-compensate rule as other
managed leaves. If construction stops during compilation or stamp publication, only validated
checked-hash bytecode and the exact producer stamp/candidate are treated as producer-owned output
during incomplete-release retirement; any other untracked path still fails closed.

Uninstall must decide whether to restore a retained launcher or remove the managed one, and
the retained backup lives in a same-UID-writable state directory, so the backup itself is not
trusted. Setup embeds a producer-issued binding — the digest of the exact bytes it displaced,
or `absent` when it displaced nothing — in the managed launcher it installs. Uninstall
re-derives that binding from the frozen transaction snapshot of the backup and requires the
installed launcher to be the byte-exact rendering for it. A foreign replacement of the backup,
an injection where no original existed, and a deletion of the retained backup therefore all
fail closed instead of being adopted. Setup applies the same check in reverse: it asks the
release manager whether an existing launcher is already one of ours before retaining it, so a
rerun never records the managed launcher as the user's original, and a rerun whose retained
backup has since been deleted is refused rather than silently rebound.

### Case-folding source paths on a supported volume

The supported platform is macOS, whose default APFS volume compares filenames after Unicode
normalization *and* full case folding: `Agents-Mac-mini.local`, `agents-Mac-mini.local`, an NFD
spelling of an NFC name, and `straße`/`strasse` all name one file. Two reviewed source paths that
fold together therefore cannot both exist in a release worktree, and the reviewed Hermes upstream
contains exactly one such pair.

`scripts/hermes-release-manager.py` reads the folded groups out of the exact Git tree — never out
of the volume — so the verdict is identical on every filesystem, and it fails the build closed on
any group that is not permitted. Exactly one namespace is permitted:

- `contributors/emails/<commit-author-email>` — upstream's contributor attribution map, one file
  per commit-author address holding a GitHub login. It is consumed only by upstream's own
  `Contributor Attribution Check` workflow and release tooling. It is never imported, never
  executed, never read by the Hermes runtime, and never an input to dependency resolution or
  configuration.

Every member of a permitted group must be a plain `100644` blob directly inside that namespace.
A folded group anywhere else — a module, a lockfile, a configuration file, a directory — and any
member that is executable, a symlink, a submodule, or a subdirectory is a hard build failure,
because an aliased runtime path would silently change what the release executes.

For a permitted group the producer picks the byte-greatest member path as the representative,
unlinks whatever the checkout aliased into the slot, and rewrites the representative's exact blob
under its exact spelling, so the surviving name and bytes are a reviewed decision rather than a
checkout-order side effect. It then requires `git status` to contain exactly the records that
normalization predicts and nothing else. The group, its members, the representative, and the
SHA-256 of what actually materialized are all recorded in the completion receipt and re-derived on
every reuse, so edited bytes, a swapped spelling, or a changed policy fail verification. A volume
that keeps the members apart — a case-sensitive one — keeps all of them, and the receipt records
that instead.

`tests/test_hermes_release_integration.py` proves this against the real reviewed upstream object
database and the real carried bundle. It is opt-in; see the module docstring.

## Backup and rollback

Runtime Kanban databases are user data and are not kept in Git. Back them up separately before upgrades. `scripts/uninstall.sh` removes managed integration links/config entries but preserves boards and cards. Git tags plus the carried bundle provide implementation rollback; a Hermes rollback must restore the matching upstream pin and carried stack together.

## Maintainer responsibilities

The current maintainer is `@LeeYuHoon`. Maintainers triage issues, preserve scope and compatibility boundaries, publish security fixes, keep the changelog and verification reports factual, and avoid claiming support for platforms that have not been exercised. New maintainers should be added through a reviewed governance change rather than inferred from contribution volume.
