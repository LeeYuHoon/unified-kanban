"""실제 Git fixture로 worktree 라우팅과 fail-closed 경계를 검증한다."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from kanban_adapter.backend import BoardNotMappedError, HermesCliBackend


def git(root: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
    return subprocess.run(['/usr/bin/git', '-C', str(root), *args], env=env,
                          check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo_pair(tmp_path: Path) -> tuple[Path, Path]:
    main = tmp_path / 'main'
    main.mkdir()
    git(main, 'init')
    git(main, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
        '-c', 'core.hooksPath=/dev/null', 'commit', '--allow-empty', '-m', 'fixture')
    linked = tmp_path / 'sibling'
    git(main, '-c', 'core.hooksPath=/dev/null', 'worktree', 'add', '--detach', str(linked))
    return main.resolve(), linked.resolve()


def backend(*mappings: tuple[str, Path | None]) -> HermesCliBackend:
    payload = json.dumps([{'slug': slug, 'default_workdir': str(root) if root else None}
                          for slug, root in mappings])
    def runner(argv: list[str]) -> str:
        assert argv == ['hermes', 'kanban', 'boards', 'list', '--json']
        return payload
    return HermesCliBackend(runner=runner)


def test_registered_sibling_and_nested_cwd_inherit_unique_repo_board(repo_pair):
    main, linked = repo_pair
    nested = linked / 'src'
    nested.mkdir()
    service = backend(('default', None), ('project-a', main))
    assert service.resolve_board(cwd=linked) == 'project-a'
    assert service.resolve_board(cwd=nested) == 'project-a'


def test_explicit_deepest_mapping_beats_repo_fallback(repo_pair):
    main, linked = repo_pair
    nested = linked / 'src'
    nested.mkdir()
    service = backend(('main', main), ('linked', linked), ('nested', nested))
    assert service.resolve_board(cwd=nested) == 'nested'
    assert service.resolve_board(cwd=linked) == 'linked'


def test_multiple_mapped_roots_same_repo_are_ambiguous(repo_pair):
    main, linked = repo_pair
    third = main.parent / 'third'
    git(main, '-c', 'core.hooksPath=/dev/null', 'worktree', 'add', '--detach', str(third))
    with pytest.raises(RuntimeError, match='multiple Kanban boards') as error:
        backend(('main', main), ('linked', linked)).resolve_board(cwd=third)
    assert not isinstance(error.value, BoardNotMappedError)


def test_mapped_linked_root_can_route_another_registered_worktree(repo_pair):
    main, linked = repo_pair
    assert backend(('project-a', linked)).resolve_board(cwd=main) == 'project-a'


@pytest.mark.parametrize('kind', ['missing', 'subdirectory', 'parent', 'unrelated'])
def test_only_existing_exact_repo_root_mapping_is_inherited(repo_pair, kind):
    main, linked = repo_pair
    root = {'missing': main / 'missing', 'subdirectory': main / 'src',
            'parent': main.parent / 'container', 'unrelated': main.parent / 'other'}[kind]
    if kind != 'missing':
        root.mkdir()
    if kind == 'unrelated':
        git(root, 'init')
    with pytest.raises(BoardNotMappedError):
        backend(('project-a', root), ('default', None)).resolve_board(cwd=linked)


@pytest.mark.parametrize('kind', ['copied-pointer', 'missing-registration', 'wrong-backlink',
                                 'bad-commondir', 'missing-head', 'symlink-marker'])
def test_unregistered_or_damaged_worktree_is_rejected(repo_pair, kind):
    main, linked = repo_pair
    marker = linked / '.git'
    admin = Path(marker.read_text().strip()[8:])
    cwd = linked
    if kind == 'copied-pointer':
        cwd = main.parent / 'forged'
        cwd.mkdir()
        (cwd / '.git').write_bytes(marker.read_bytes())
    elif kind == 'missing-registration':
        (admin / 'gitdir').unlink()
    elif kind == 'wrong-backlink':
        (admin / 'gitdir').write_text(str(main / '.git') + '\n')
    elif kind == 'bad-commondir':
        (admin / 'commondir').write_text('../missing\n')
    elif kind == 'missing-head':
        (admin / 'HEAD').unlink()
    else:
        saved = linked / 'saved-marker'
        marker.rename(saved)
        marker.symlink_to(saved)
    with pytest.raises(BoardNotMappedError):
        backend(('project-a', main)).resolve_board(cwd=cwd)


def test_unmapped_parents_missing_cwd_and_non_git_workspace_stay_unmapped(repo_pair):
    main, linked = repo_pair
    ordinary = main.parent / 'workspace'
    ordinary.mkdir()
    for cwd in (main.parent, ordinary, linked / 'missing'):
        with pytest.raises(BoardNotMappedError):
            backend(('project-a', main), ('default', None)).resolve_board(cwd=cwd)


def test_nested_unrelated_repository_does_not_inherit_outer_worktree(repo_pair):
    main, linked = repo_pair
    nested = linked / 'inner'
    nested.mkdir()
    git(nested, 'init')
    with pytest.raises(BoardNotMappedError):
        backend(('project-a', main)).resolve_board(cwd=nested)


def test_hostile_git_environment_config_and_path_are_never_executed(repo_pair, monkeypatch):
    main, linked = repo_pair
    # 해석조차 해서는 안 되는 설정과 실행 금지 경계로 환경 독립성을 확인한다.
    (main / '.git' / 'config').write_text('not valid git config!\n')
    for key in ('GIT_DIR', 'GIT_COMMON_DIR', 'GIT_WORK_TREE', 'GIT_CONFIG_GLOBAL',
                'GIT_CONFIG_SYSTEM', 'GIT_CONFIG_COUNT', 'GIT_CONFIG_PARAMETERS'):
        monkeypatch.setenv(key, 'hostile-value')
    monkeypatch.setenv('PATH', str(main))
    monkeypatch.setenv('HERMES_KANBAN_BOARD', 'default')
    def forbidden(*args, **kwargs):
        raise AssertionError('routing must not execute Git/config/hooks')
    monkeypatch.setattr(subprocess, 'run', forbidden)
    assert backend(('project-a', main)).resolve_board(cwd=linked) == 'project-a'


def test_malformed_board_metadata_still_fails_before_git_fallback(repo_pair):
    main, linked = repo_pair
    service = HermesCliBackend(runner=lambda _: json.dumps([
        {'slug': 'project-a', 'default_workdir': str(main)}, {'slug': 'broken'}]))
    with pytest.raises(RuntimeError, match='missing default_workdir'):
        service.resolve_board(cwd=linked)
