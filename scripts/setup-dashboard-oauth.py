#!/usr/bin/env python3
"""저장된 공식 Dashboard 등록 설정만 오프라인 검증한다. 인증 저장소는 읽지 않는다."""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

LOGIN = "Existing official Dashboard registration required. Run: hermes auth add nous; hermes dashboard register; then rerun setup --dashboard-oauth."
CLIENT_KEY = "HERMES_DASHBOARD_OAUTH_CLIENT_ID"


REVIEW = "Dashboard OAuth setup refused; review configuration and Nous Portal /local-dashboards. Manually reconcile any prior registration marker with Portal before retrying; setup never removes it."
PENDING = ".dashboard-oauth-registration-pending"
PORTAL = "https://portal.nousresearch.com"


def transaction():
    import importlib.util
    spec = importlib.util.spec_from_file_location("oauth_path_transaction", Path(__file__).with_name("path-transaction.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def metadata(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def read_fd(fd: int, parent: int, name: str) -> bytes:
    import stat
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_size > 8 * 1024 * 1024:
        raise RuntimeError(REVIEW)
    chunks = []
    total = 0
    while True:
        chunk = os.read(fd, min(65536, 8 * 1024 * 1024 + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > 8 * 1024 * 1024:
            raise RuntimeError(REVIEW)
    if total != before.st_size or metadata(os.fstat(fd)) != metadata(before) or metadata(os.stat(name, dir_fd=parent, follow_symlinks=False)) != metadata(before):
        raise RuntimeError(REVIEW)
    return b"".join(chunks)


def read_at(parent: int, name: str) -> bytes | None:
    import stat
    try:
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode) or before.st_size > 8 * 1024 * 1024:
        raise RuntimeError(REVIEW)
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino):
            raise RuntimeError(REVIEW)
        return read_fd(fd, parent, name)
    finally:
        os.close(fd)


def read_optional(path: Path) -> bytes | None:
    try:
        parent, name = transaction()._open_parent(path)
    except FileNotFoundError:
        return None
    try:
        return read_at(parent, name)
    finally:
        os.close(parent)


def persisted_settings() -> dict:
    return parse_settings(read_optional(Path(os.environ["HERMES_HOME"]) / ".env"))


def parse_settings(raw) -> dict:
    import io
    from dotenv.parser import parse_stream
    result = {}
    for binding in parse_stream(io.StringIO(raw.decode() if raw else "")):
        if binding.error or (binding.key is not None and binding.key in result):
            raise ValueError("Ambiguous dotenv configuration")
        if binding.key is not None:
            result[binding.key] = binding.value
    return result


def validate_settings() -> str | None:
    from dashboard_oauth_config import parse_config, unrelated_config

    home = Path(os.environ["HERMES_HOME"])
    config = parse_config(read_optional(home / "config.yaml"))
    unrelated_config(config)
    dashboard = config.get("dashboard", {})
    stored = persisted_settings()
    for source in (stored, os.environ):
        if source.get("HERMES_DASHBOARD_PUBLIC_URL"):
            raise RuntimeError(REVIEW)
        if source.get("HERMES_DASHBOARD_HOST", "127.0.0.1") not in ("127.0.0.1", "localhost", "::1"):
            raise RuntimeError(REVIEW)
    if dashboard.get("public_url") or dashboard.get("host", "127.0.0.1") not in ("127.0.0.1", "localhost", "::1"):
        raise RuntimeError(REVIEW)
    for portal in (stored.get("HERMES_DASHBOARD_PORTAL_URL", ""),
                   os.environ.get("HERMES_DASHBOARD_PORTAL_URL", ""),
                   dashboard.get("oauth", {}).get("portal_url", "")):
        if portal not in ("", PORTAL):
            raise RuntimeError(REVIEW)
    client = stored.get(CLIENT_KEY)
    if CLIENT_KEY in stored and (not isinstance(client, str) or not re.fullmatch(r"agent:[A-Za-z0-9._-]+", client)):
        raise RuntimeError(REVIEW)
    oauth = dashboard.get("oauth", {})
    if "client_id" in oauth and oauth["client_id"] != "" and oauth["client_id"] != client:
        raise RuntimeError(REVIEW)
    if CLIENT_KEY in os.environ and os.environ[CLIENT_KEY] != client:
        raise RuntimeError(REVIEW)
    # 이전 등록 표시를 보존하여 사용자가 직접 대조하고 해결하도록 한다.
    if read_optional(home / PENDING) is not None:
        raise RuntimeError(REVIEW)
    if not client:
        raise RuntimeError(LOGIN)
    return client



def validate_capability(runtime: Path) -> None:
    """공식 코드를 실행하지 않고 기본 설정의 데이터 선언만 읽는다.

    승인된 런타임의 config.py는 config_defaults.py의 DEFAULT_CONFIG를 내보낸다.
    다른 기본값은 함수를 호출하고 config를 가져오면 사용자 상태와 인증 기능까지
    불러오므로, 리터럴로 적힌 dashboard 항목만 분석한다.
    이 제한된 기능 표식 검사는 고정된 배포본의 동일성 검증을 보완할 뿐이다.
    제어 흐름·별칭·이후 변경은 해석하지 않으므로 일반적인 Python 실행 결과나
    실행 중인 서버의 인증 강제를 증명하지 않는다. 공식 소스는 절대 실행하지 않는다.
    """
    import ast

    raw = read_optional(runtime / "hermes_cli" / "config_defaults.py")
    if raw is None:
        raise RuntimeError("Native Dashboard require_auth capability missing")
    tree = ast.parse(raw)
    declarations = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == "DEFAULT_CONFIG"
                   for target in targets for t in ast.walk(target)):
            continue
        if (not isinstance(node, ast.Assign) or len(targets) != 1
                or not isinstance(targets[0], ast.Name)):
            raise RuntimeError("Native Dashboard defaults binding ambiguous")
        declarations.append(node.value)
    if len(declarations) != 1 or not isinstance(declarations[0], ast.Dict):
        raise RuntimeError("Native Dashboard defaults contract unavailable")
    if any(not isinstance(key, ast.Constant) or not isinstance(key.value, str)
           for key in declarations[0].keys):
        raise RuntimeError("Native Dashboard defaults keys ambiguous")
    blocks = [value for key, value in zip(declarations[0].keys, declarations[0].values)
              if isinstance(key, ast.Constant) and key.value == "dashboard"]
    if len(blocks) != 1 or not isinstance(blocks[0], ast.Dict):
        raise RuntimeError("Native Dashboard defaults missing")
    if any(not isinstance(key, ast.Constant) or not isinstance(key.value, str)
           for key in blocks[0].keys):
        raise RuntimeError("Native Dashboard keys ambiguous")
    values = [value for key, value in zip(blocks[0].keys, blocks[0].values)
              if isinstance(key, ast.Constant) and key.value == "require_auth"]
    if len(values) != 1 or not isinstance(values[0], ast.Constant) or values[0].value is not False:
        raise RuntimeError("Native Dashboard require_auth opt-in missing")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("runtime", type=Path)
    parser.add_argument("--registration-only", action="store_true",
                        help="Inspect the old bootstrap runtime's registration before preparing its replacement")
    args = parser.parse_args()
    try:
        if not args.runtime.is_dir() or not (args.runtime / "hermes_cli").is_dir():
            raise RuntimeError(REVIEW)
        if not args.registration_only:
            validate_capability(args.runtime)
        validate_settings()
    except Exception:
        # 예외 객체에는 자격 증명이 포함될 수 있으므로 출력하지 않는다.
        print(LOGIN + " " + REVIEW, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
