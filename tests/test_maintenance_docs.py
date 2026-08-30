from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HEADING = "### Guarded immutable-release garbage collection"
EXPECTED_SECTION = """### Guarded immutable-release garbage collection

Release construction and update activation never delete a previously published release. A maintainer may instead run `python3 scripts/hermes-release-manager.py gc <HERMES_AGENT_REPO>` for a deterministic, write-free JSON plan. Planning validates the release-root ancestry and each candidate's private completion receipt and sealed contents; it preserves unrecognized, incomplete, malformed, tampered, or foreign entries rather than treating them as candidates.

Deletion is a separate macOS-only action using the same command with `--apply`. Apply holds `$HERMES_HOME/state/hermes-kanban-update.lock`, retains descriptor and inode authority for the release root, and protects `current` and `previous` together with references found in the configured or loaded launchd service and stable same-UID runtime process and open-file probes. An unavailable, malformed, or non-convergent reference probe prevents deletion of the affected candidate and aborts subsequent work; candidates already deleted earlier in the same apply run are not restored.

A candidate is first moved descriptor-relatively to a fresh private retirement name and bound to a durable retirement record. References and the shared lock are checked again before deletion; a newly referenced release is restored only when its canonical pathname is still absent and owned by that operation. On crash recovery, the record, retirement identity, complete receipt and contents, and fresh runtime references are revalidated before deletion resumes. A partial or tampered retirement is retained with its record and is neither deleted nor restored to the canonical pathname. Foreign successors, replacement roots, malformed records, and indeterminate recovery state are preserved for deliberate inspection.
"""


def test_maintenance_documents_guarded_release_gc_lifecycle() -> None:
    maintenance = (ROOT / "docs/maintenance.md").read_text(encoding="utf-8")

    assert maintenance.count(HEADING) == 1
    section = maintenance.split(HEADING, 1)[1].split("## Dependency management", 1)[0]
    assert HEADING + section.rstrip() + "\n" == EXPECTED_SECTION
    for obsolete_or_unsafe in (
        "A previously published release directory is never deleted",
        "An unavailable, malformed, or non-convergent reference probe fails closed without deletion",
    ):
        assert obsolete_or_unsafe not in maintenance

    checklist = (ROOT / "docs/hermes-update-checklist.md").read_text(encoding="utf-8")
    assert "내용을 확인한 뒤 수동으로 지워도 된다." not in checklist
    assert (
        "남은 retirement와 그 내용을 수동 삭제하지 않는다. 자동 recovery authority가 "
        "없으므로 그대로 보존하고, operation log 및 canonical namespace와 함께 maintainer가 "
        "별도로 조사한다."
        in checklist
    )
