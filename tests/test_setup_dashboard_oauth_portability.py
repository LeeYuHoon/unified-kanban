"""OAuth 테스트 자료는 다른 위치로 옮긴 독립 저장소에서도 동작해야 한다."""
import os
from pathlib import Path
import shutil
import subprocess
import sys


def test_oauth_fixtures_without_sibling_checkout(tmp_path):
    root = Path(__file__).resolve().parents[1]
    relocated = tmp_path / 'checkout'
    relocated.mkdir()
    for directory in ('bin', 'scripts', 'tests', 'patches', 'src', 'integrations'):
        shutil.copytree(root / directory, relocated / directory,
                        ignore=shutil.ignore_patterns('__pycache__'))
    assert not (tmp_path / 'hermes-local-dashboard-oauth').exists()
    env = os.environ.copy()
    import yaml
    import dotenv
    dependencies = tmp_path / 'dependencies'
    shutil.copytree(Path(yaml.__file__).resolve().parent, dependencies / 'yaml')
    shutil.copytree(Path(dotenv.__file__).resolve().parent, dependencies / 'dotenv')
    env['PYTHONPATH'] = str(dependencies)
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    result = subprocess.run([
        sys.executable, '-m', 'pytest', '-o', 'addopts=', '-q', '-p', 'no:cacheprovider',
        'tests/test_setup_dashboard_oauth.py::test_missing_registration_no_mutations',
        'tests/test_setup_dashboard_oauth_closure.py',
        'tests/test_setup_dashboard_oauth_hardening.py',
        'tests/test_setup_dashboard_oauth_capability.py::test_old_installed_new_prepared_upgrade',
    ], cwd=relocated, env=env, text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
