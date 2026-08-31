from __future__ import annotations

import subprocess
import tarfile
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def test_sdist_contains_only_the_intended_library_contract(tmp_path: Path) -> None:
    out_dir = tmp_path / "dist"
    completed = subprocess.run(
        ["uv", "build", "--sdist", "--out-dir", str(out_dir)],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    archives = list(out_dir.glob("unified_kanban-*.tar.gz"))
    assert len(archives) == 1
    with tarfile.open(archives[0], "r:gz") as archive:
        names = set(archive.getnames())

    root = next(name.split("/", 1)[0] for name in names)
    assert f"{root}/src/kanban_adapter/__init__.py" in names
    assert f"{root}/README.md" in names
    assert f"{root}/README.en.md" not in names
    assert f"{root}/LICENSE" in names
    assert f"{root}/THIRD_PARTY_NOTICES.md" in names
    assert not any(name.startswith(f"{root}/tests/") for name in names)
