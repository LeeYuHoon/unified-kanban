"""유지관리자 전용 device 재증명의 읽기 전용 기본값과 CAS 경계."""
import hashlib
import json
import os
import subprocess
import sys

import pytest

from test_hermes_release_manager import build_completed_release, load_helper


@pytest.fixture
def completed(tmp_path):
    helper = load_helper()
    agent = tmp_path / "hermes-agent"
    layout = build_completed_release(helper, agent, tmp_path / "build")
    receipt = layout.release / helper._COMPLETION_RECEIPT
    payload = json.loads(receipt.read_bytes())
    layout.selector.write_bytes(helper.selector_payload(layout))
    layout.selector.chmod(0o600)
    payload["release_identity"][0] += 1
    old = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    receipt.write_bytes(old)
    return helper, agent, layout, payload, old


def invoke(completed, **kwargs):
    helper, agent, layout, payload, old = completed
    return helper.reattest_release_device(
        agent, payload["upstream"], payload["carried"],
        expected_receipt_sha256=hashlib.sha256(old).hexdigest(), **kwargs,
    )


def test_apply_and_expected_old_retry(completed):
    from kanban_adapter.compatibility import check_selected_release
    helper, agent, layout, payload, old = completed
    before = helper._tree_digest(layout.release, excluded_top_level={helper._COMPLETION_RECEIPT})
    selector = layout.selector.read_bytes(), layout.selector.stat().st_ino
    result = invoke(completed, apply=True)
    assert result["status"] == "applied"
    assert result["changed"] is True
    helper._verify_completed_release(layout, payload["upstream"], payload["carried"])
    assert check_selected_release(agent, payload["upstream"], payload["carried"]) == ""
    assert before == helper._tree_digest(layout.release, excluded_top_level={helper._COMPLETION_RECEIPT})
    assert selector == (layout.selector.read_bytes(), layout.selector.stat().st_ino)
    with pytest.raises(RuntimeError, match="SHA256 mismatch"):
        invoke(completed, apply=True)
    receipt = layout.release / helper._COMPLETION_RECEIPT
    identity = receipt.stat().st_ino
    result = helper.reattest_release_device(
        agent, payload["upstream"], payload["carried"], apply=True,
        expected_receipt_sha256=hashlib.sha256(receipt.read_bytes()).hexdigest(),
    )
    assert result["status"] == "already-current" and not result["changed"]
    assert receipt.stat().st_ino == identity


@pytest.mark.parametrize("target", ["root", "release", "receipt", "receipt-content"])
def test_late_publish_drift_is_refused(completed, monkeypatch, target):
    from kanban_adapter import private_files as private
    helper, agent, layout, payload, old = completed
    receipt = layout.release / helper._COMPLETION_RECEIPT
    original = private._write_all
    displaced = None
    changed = False
    def inject(fd, data):
        nonlocal displaced, changed
        original(fd, data)
        if changed:
            return
        changed = True
        path = {"root": layout.root, "release": layout.release,
                "receipt": receipt, "receipt-content": receipt}[target]
        if target == "receipt-content":
            path.write_bytes(old + b" ")
        elif target == "receipt":
            copy = path.with_name("replacement")
            copy.write_bytes(old)
            copy.chmod(0o600)
            os.replace(copy, path)
        else:
            displaced = path.with_name(path.name + ".displaced")
            path.rename(displaced)
            path.mkdir(mode=0o700)
            (path / "foreign").write_text("keep")
    monkeypatch.setattr(private, "_write_all", inject)
    with pytest.raises((RuntimeError, OSError)):
        invoke(completed, apply=True)
    if target in {"root", "release"}:
        foreign = layout.root if target == "root" else layout.release
        assert (foreign / "foreign").read_text() == "keep"
        old_path = (displaced / layout.release.name if target == "root" else displaced)
        assert (old_path / helper._COMPLETION_RECEIPT).read_bytes() == old
    else:
        assert receipt.read_bytes() == old + (b" " if target == "receipt-content" else b"")


def test_cli_explicit_reviewed_arguments_and_default_dry_run(completed):
    helper, agent, layout, payload, old = completed
    args = [sys.executable, helper.__file__, "reattest-device", str(agent),
            "--reviewed-upstream", payload["upstream"],
            "--reviewed-carried", payload["carried"],
            "--expected-receipt-sha256", hashlib.sha256(old).hexdigest()]
    run = subprocess.run(args, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["status"] == "dry-run"
    assert (layout.release / helper._COMPLETION_RECEIPT).read_bytes() == old
    for option in ("--reviewed-upstream", "--reviewed-carried", "--expected-receipt-sha256"):
        index = args.index(option)
        assert subprocess.run(args[:index] + args[index+2:], capture_output=True).returncode != 0
    run = subprocess.run(args + ["--apply"], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["status"] == "applied"


@pytest.mark.parametrize("field", ["version", "release_identity", "upstream", "carried",
    "git_tree", "git_config_sha256", "git_head_sha256", "git_refs", "case_collisions",
    "bytecode", "release_sha256", "extra"])
def test_every_other_payload_field_is_exact(completed, field):
    helper, agent, layout, payload, old = completed
    if field == "release_identity":
        payload[field][1] += 1
    else:
        payload[field] = "tampered"
    receipt = layout.release / helper._COMPLETION_RECEIPT
    bad = json.dumps(payload).encode()
    receipt.write_bytes(bad)
    with pytest.raises((RuntimeError, ValueError)):
        helper.reattest_release_device(agent, json.loads(old)["upstream"], json.loads(old)["carried"],
            expected_receipt_sha256=hashlib.sha256(bad).hexdigest(), apply=True)
    assert receipt.read_bytes() == bad


@pytest.mark.parametrize("target", ["runtime", "git-config", "git-ref", "git-head", "fingerprint"])
def test_actual_content_drift_is_rejected(completed, target):
    helper, agent, layout, payload, old = completed
    if target == "git-ref":
        subprocess.run(["git", "-C", str(layout.release), "update-ref", "refs/heads/foreign", payload["carried"]], check=True)
    else:
        path = {"runtime": "gc-fixture.txt", "git-config": ".git/config",
                "git-head": ".git/HEAD", "fingerprint": ".bytecode-fingerprint"}[target]
        with (layout.release / path).open("ab") as stream:
            stream.write(b"\n# drift\n")
    with pytest.raises((RuntimeError, subprocess.CalledProcessError)):
        invoke(completed, apply=True)
    assert (layout.release / helper._COMPLETION_RECEIPT).read_bytes() == old


@pytest.mark.parametrize("target", ["root", "release", "receipt"])
def test_replacement_during_expensive_inventory(completed, monkeypatch, target):
    helper, agent, layout, payload, old = completed
    original = helper._completion_payload
    def inject(*args):
        result = original(*args)
        path = {"root": layout.root, "release": layout.release,
                "receipt": layout.release / helper._COMPLETION_RECEIPT}[target]
        if target == "receipt":
            copy = path.with_name("copy")
            copy.write_bytes(old)
            copy.chmod(0o600)
            os.replace(copy, path)
        else:
            path.rename(path.with_name(path.name + ".displaced"))
            path.mkdir(mode=0o700)
            (path / "foreign").write_text("keep")
        return result
    monkeypatch.setattr(helper, "_completion_payload", inject)
    with pytest.raises((RuntimeError, OSError)):
        invoke(completed, apply=True)


@pytest.mark.parametrize("phase", ["write", "swap", "retire-fsync", "publish-fsync", "final", "foreign"])
def test_publish_failures_compensate_or_preserve_foreign(completed, monkeypatch, phase):
    from kanban_adapter import private_files as private
    helper, agent, layout, payload, old = completed
    receipt = layout.release / helper._COMPLETION_RECEIPT
    before = set(layout.release.iterdir())
    if phase in {"write", "swap"}:
        name = "_write_all" if phase == "write" else "_swap_names"
        def fail(*args):
            raise OSError("injected failure")
        monkeypatch.setattr(private, name, fail)
    elif phase in {"retire-fsync", "publish-fsync"}:
        original = os.fsync
        count = 0
        def fail(fd):
            nonlocal count
            if __import__("stat").S_ISDIR(os.fstat(fd).st_mode):
                count += 1
                if count == (1 if phase == "retire-fsync" else 2):
                    raise OSError("injected fsync failure")
            original(fd)
        monkeypatch.setattr(os, "fsync", fail)
    else:
        def fail(*args):
            if phase == "foreign":
                copy = receipt.with_name("foreign-copy")
                copy.write_bytes(b"foreign successor")
                copy.chmod(0o600)
                os.replace(copy, receipt)
            raise RuntimeError("injected final verification failure")
        monkeypatch.setattr(helper, "_verify_completed_release", fail)
    with pytest.raises((RuntimeError, OSError)):
        invoke(completed, apply=True)
    assert receipt.read_bytes() == (b"foreign successor" if phase == "foreign" else old)
    assert set(layout.release.iterdir()) == before


def test_only_current_release_is_eligible(completed):
    helper, agent, layout, payload, old = completed
    layout.selector.write_text("foreign selector\n")
    inode = (layout.release / helper._COMPLETION_RECEIPT).stat().st_ino
    with pytest.raises(RuntimeError, match="selected"):
        invoke(completed)
    assert (layout.release / helper._COMPLETION_RECEIPT).stat().st_ino == inode


@pytest.mark.parametrize("kind", ["copy", "content", "failure"])
def test_atomic_swap_boundary_preserves_generation(completed, monkeypatch, kind):
    from kanban_adapter import private_files as private
    helper, agent, layout, payload, old = completed
    receipt = layout.release / helper._COMPLETION_RECEIPT
    real_swap = private._swap_names
    once = False
    def inject(fd, first, second):
        nonlocal once
        if once:
            return real_swap(fd, first, second)
        once = True
        if kind == "copy":
            copy = receipt.with_name("foreign-copy")
            copy.write_bytes(old)
            copy.chmod(0o600)
            os.replace(copy, receipt)
        elif kind == "content":
            receipt.write_bytes(old + b" ")
        real_swap(fd, first, second)
        if kind == "failure":
            raise OSError("failure after committed swap")
    monkeypatch.setattr(private, "_swap_names", inject)
    with pytest.raises((RuntimeError, OSError)):
        invoke(completed, apply=True)
    assert receipt.read_bytes() == old + (b" " if kind == "content" else b"")
    assert not any("staged" in path.name for path in layout.release.iterdir())


def test_default_dry_run_is_read_only(completed):
    helper, agent, layout, payload, old = completed
    before = {p: (p.lstat().st_ino, p.read_bytes()) for p in
              (layout.selector, layout.release / helper._COMPLETION_RECEIPT)}
    result = invoke(completed)
    assert result["status"] == "dry-run"
    assert result["changed"] is False
    assert {p: (p.lstat().st_ino, p.read_bytes()) for p in before} == before
    with pytest.raises(RuntimeError, match="does not match"):
        helper._verify_completed_release(layout, payload["upstream"], payload["carried"])

@pytest.mark.parametrize("compensation", [False, True])
@pytest.mark.parametrize("failure", ["successor", "recovery-io"])
def test_reattest_recovery_preserves_canonical(completed, monkeypatch, failure, compensation):
    from kanban_adapter import private_files as private
    helper, agent, layout, payload, old = completed
    path = layout.release / helper._COMPLETION_RECEIPT
    real_swap, real_rename = private._swap_names, private._rename_exclusive
    active = False
    injected = False
    producer = None
    initial_done = False
    if compensation:
        def fail_final():
            raise RuntimeError("final check failed")
        monkeypatch.setattr(helper, "_verify_completed_release", lambda *args: fail_final())
    def boundary():
        nonlocal injected
        if not injected:
            injected = True
            if failure == "successor":
                other = path.with_name("second")
                other.write_bytes(b"foreign-two")
                os.replace(other, path)
            else:
                raise OSError("second recovery failed")
    def swap(fd, first, second):
        nonlocal active, producer, initial_done
        if compensation and not initial_done:
            initial_done = True
            return real_swap(fd, first, second)
        if active:
            boundary()
        else:
            other = path.with_name("first")
            other.write_bytes(b"foreign-one")
            os.replace(other, path)
            real_swap(fd, first, second)
            producer = path.read_bytes()
            active = True
            return
        real_swap(fd, first, second)
    def rename(fd, source, destination):
        if active:
            boundary()
        real_rename(fd, source, destination)
    monkeypatch.setattr(private, "_swap_names", swap)
    monkeypatch.setattr(private, "_rename_exclusive", rename)
    with pytest.raises(RuntimeError) as caught:
        invoke(completed, apply=True)
    assert path.read_bytes() == (b"foreign-two" if failure == "successor" else producer)
    assert isinstance(caught.value, private.NamespaceAuthorityError)
    assert caught.value.retained_paths
