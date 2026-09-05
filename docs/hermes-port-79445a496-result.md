# Hermes 79445a496 호환성 포팅 결과

포팅과 비활성 검증을 완료했다. 프로젝트 전체 pytest는 1218 passed이며, 실제 설치·서비스 활성화나 프로젝트 commit/push는 수행하지 않았다.

이 문장의 범위는 최초 `27b9ecc1196` 포팅 검증 시점이다. 이후 승인된 운영 적용의 독립 리뷰에서 JSON decoder 제한 예외 누락을 발견해 기존 세 커밋 위에 `dfb94249a5fa3f0505c41896b6f4ec51c1b9a4be`를 추가했다. official frozen SHA는 유지했다. 깊은 JSON과 긴 정수 댓글의 회귀는 4 failed를 확인한 뒤 11 passed로 전환했고, 최초 포팅 증거는 아래에 그대로 보존한다. 현재 배포 대상은 README와 manifest의 네 번째 carried commit이며, 운영 활성화 증거는 별도 최종 handoff로 기록한다.

## 작업 범위와 고정 기준

- 작업일: 2026-09-06. macOS 26.6.2 arm64, Node v24.12.0, npm 11.6.2, uv 0.11.11에서 검증했다.
- Hermes 작업 저장소: `/tmp/unified-kanban-hermes-update` (실제 Git 루트 `/private/tmp/unified-kanban-hermes-update`).
- 프로젝트 작업 저장소: `/Users/leeyuhun/cursor-project/.worktrees/unified-kanban-hermes-79445a496`, 브랜치 `compat/hermes-79445a496`.
- frozen official upstream: `79445a496c86a19332ad786494b8384d2167e2d0`.
- Hermes 버전은 새 upstream의 실제 `hermes_constants.py`와 동일한 `0.21.0`이다. 버전 문자열을 임의로 올리지 않았다.
- 원본 `refs/unified-kanban/carried/carried-01`부터 `carried-13`까지는 보존했다. 분리된 upstream 모듈에 누적 기능을 직접 포팅했으며 원본 커밋을 무작정 cherry-pick하지 않았다.
- bundle 생성에 필요한 로컬 carried 커밋만 Hermes 임시 저장소에 만들었다. 아래 세 커밋이 frozen SHA 위의 선형 체인이다.

| 순서 | carried SHA | 내용 |
| --- | --- | --- |
| 1 | `914ab9928188c3d50c736089add3786f503a287c` | 분리 모듈의 관찰 카드, 작업자 제외, CLI 파일 입력과 멱등성 |
| 2 | `0909d00776b40f8a106fcbe4b7769398c73adbd8` | 보드 고정 API, 신뢰 사용량 집계, 엄격한 스키마 버전 검증 |
| 3 | `27b9ecc1196ccea5d8eec7e17512f23e9416a3d9` | 현재 React 및 기존 대시보드 표시·삭제 계약, 원본 사양 테스트 이식 |

## 구현한 기능

- 관찰 카드는 생성 직후 running이지만 worker claim/dispatch/review/requeue/workspace 갱신의 대상이 아니다. orphan TTL로 종료되며 완료 시에도 합성 run을 만들지 않는다.
- 관찰 카드의 완료 요약은 `completed` 이벤트의 `full_summary`에 최대 1000자로 보존하고, 기존 짧은 결과·handoff와 호환되는 최신 요약 fallback을 제공한다. 결과 파일의 본문과 이 요약 필드는 구분한다.
- `--title-file`과 `--result-file`은 일반 파일만 허용하고 symlink/FIFO 등을 거부한다. 제목은 4096 bytes, 결과는 2 MiB 이내의 UTF-8로 제한한다. 여기서 private 입력은 본문을 명령행 인자에 노출하지 않는 경로이며, 파일 권한을 임의로 0600으로 강제하는 새 계약을 추가하지 않았다.
- 댓글 멱등성 키를 task별 transaction 안에서 확인한다.
- 사용량 댓글의 신뢰 author/header, v1/v2, 이벤트 해시, 유효한 숫자와 total fallback, 중복 이벤트, null/누락 coverage, 모델 계열, archived 보드 누적을 보존했다. Python의 숫자 동등성으로 `true`, `1.0`, `2.0`이 버전으로 통과하던 경로도 거부한다.
- API의 기존 board pinning 보호를 유지했다. 삭제 요청에도 명시적인 보드가 전달되며 다른 보드의 카드를 삭제하지 않는다.
- 현재 React desktop Kanban에 관찰 표시, 읽기 전용 설명, 토큰 배지·상세·보드 합계·모델 계열을 추가했다. 긴 결과는 접기/펼치기를 제공한다. 기존 dashboard JS/CSS 기능도 별도로 보존했다.
- 웹 빌드에 포함된 upstream Node 파일 시스템 테스트에 Node 타입 참조를 추가했다. 테스트를 빌드에서 제외하거나 삭제하지 않았다.
- upstream updater의 `_git_run` 분리와 `models_catalog_static.py` 이동에 맞춰 프로젝트 릴리스 검증 계약을 갱신했다. updater 검사에서는 helper가 실제 `git_cmd + args`로 subprocess를 실행하는지 AST로 확인한다.

## 실제 변경 파일

### Hermes bundle에 포함된 파일

```text
apps/desktop/src/plugins/kanban/board-switcher.tsx
apps/desktop/src/plugins/kanban/board.tsx
apps/desktop/src/plugins/kanban/desktop-spec.test.tsx
apps/desktop/src/plugins/kanban/drawer.tsx
apps/desktop/src/plugins/kanban/i18n.ts
apps/desktop/src/plugins/kanban/observation.test.tsx
apps/desktop/src/plugins/kanban/token-usage.test.tsx
apps/desktop/src/plugins/kanban/token-usage.tsx
apps/desktop/src/plugins/kanban/types.ts
hermes_cli/kanban.py
hermes_cli/kanban_db.py
hermes_cli/kanban_db_connect.py
hermes_cli/kanban_db_dispatch.py
hermes_cli/kanban_db_workspace.py
hermes_cli/kanban_output.py
hermes_cli/kanban_parser.py
plugins/kanban/dashboard/dist/index.js
plugins/kanban/dashboard/dist/style.css
plugins/kanban/dashboard/plugin_api.py
tests/hermes_cli/test_kanban_carried_cli_port.py
tests/hermes_cli/test_kanban_observation.py
tests/hermes_cli/test_kanban_observation_dispatch_port.py
tests/hermes_cli/test_kanban_observation_review_port.py
tests/hermes_cli/test_kanban_private_title_and_comment_idempotency.py
tests/plugins/test_kanban_dashboard_plugin.py
tests/plugins/test_kanban_legacy_carried_port.py
tests/plugins/test_kanban_token_schema_port.py
tests/plugins/test_kanban_token_usage.py
web/src/lib/clipboard-usage.test.ts
```

### 프로젝트 작업 worktree의 변경 파일

```text
README.md
docs/hermes-update-checklist.md
docs/hermes-update-test-cases.md
docs/hermes-port-79445a496-result.md
patches/hermes-agent-bootstrap-manifest
patches/hermes-agent-carried-bundle-metadata.json
patches/hermes-agent-carried-commits
patches/hermes-agent-carried.bundle
patches/hermes-agent-supported-upstream
scripts/bootstrap-hermes-macos.sh
scripts/verify-carried-bundle.py
tests/test_carried_bundle.py
tests/test_hermes_release_integration.py
tests/test_macos_hermes_bootstrap.py
```

`patches/hermes-agent-version`은 실제 버전이 계속 0.21.0이므로 내용 변경 없이 일치 여부를 검증했다. 의존성 manifest/lock은 변경하지 않고 기존 dev/acp extra와 npm workspace 의존성을 격리 설치했다. Hermes의 `.port-home`, `.port-cache`, `.port-tmp`, 프로젝트의 `.venv/port-home`, `.venv/port-tmp` 및 빌드 출력은 검증용이며 프로젝트 커밋 대상이 아니다.

## 실제 실행 명령과 결과

아래 명령은 표시한 저장소 루트를 cwd로 사용했다. Hermes 로그는 그 루트의 `.port-tmp`, 프로젝트 로그는 `.venv/port-tmp`에 보존한다. 프로젝트 로그/임시 HOME은 최초 `.port-tmp`/`.port-home`에서 기존 Git 제외 영역으로 옮겼다. 이전 실행 명령에는 실행 당시 경로를 그대로 기록했다.

### Hermes 의존성과 회귀 테스트

```sh
cd /tmp/unified-kanban-hermes-update
UV_CACHE_DIR="$PWD/.port-cache/uv" UV_PROJECT_ENVIRONMENT="$PWD/.venv" TMPDIR="$PWD/.port-tmp" uv sync --frozen --extra dev --extra acp
HOME="$PWD/.port-home" HERMES_HOME="$PWD/.port-home/.hermes" TMPDIR="$PWD/.port-tmp" HERMES_TEST_FILE_RETRIES=0 scripts/run_tests.sh tests/hermes_cli/test_kanban*.py tests/plugins/test_kanban*.py -j 6 --tb=short
HOME="$PWD/.port-home" HERMES_HOME="$PWD/.port-home/.hermes" TMPDIR="$PWD/.port-tmp" HERMES_TEST_FILE_RETRIES=0 scripts/run_tests.sh tests/hermes_cli/test_update_contract.py tests/hermes_cli/test_update_apply_shallow_count.py tests/hermes_cli/test_update_post_pull_syntax_guard.py tests/hermes_cli/test_update_fetch_failure_classifier.py -j 4 --tb=short
.venv/bin/python .port-tmp/cli-smoke.py
```

- Kanban: **65 files, 516 passed, 0 failed, 6 skipped**. 로그 `focused-final.log`.
- updater focused regression: **4 files, 34 passed, 0 failed**. 로그 `upstream-update-final.log`.
- CLI 실호출: `CLI_SMOKE_PASS`. 별도 임시 HOME에서 파일 입력, 관찰 완료, 댓글 멱등성, 합성 run 부재, 보드 고정 삭제까지 왕복했다. 운영 DB를 사용하지 않았다.
- 6 skipped는 기존 upstream의 비활성 DB-size gate 및 Linux/systemd 전용 launch 계약이다. 이번 포팅 테스트를 삭제하거나 skip하지 않았다.

### 현재 React 및 웹

```sh
HOME="$PWD/.port-home" TMPDIR="$PWD/.port-tmp" npm_config_cache="$PWD/.port-cache/npm" ELECTRON_CACHE="$PWD/.port-cache/electron" npm ci --workspace apps/desktop --workspace web --include-workspace-root
HOME="$PWD/.port-home" TMPDIR="$PWD/.port-tmp" npm_config_cache="$PWD/.port-cache/npm" npm run test:ui --workspace apps/desktop -- src/plugins/kanban
HOME="$PWD/.port-home" TMPDIR="$PWD/.port-tmp" npm_config_cache="$PWD/.port-cache/npm" npm run test --workspace web
HOME="$PWD/.port-home" TMPDIR="$PWD/.port-tmp" npm_config_cache="$PWD/.port-cache/npm" npm run build --workspace apps/desktop
HOME="$PWD/.port-home" TMPDIR="$PWD/.port-tmp" npm_config_cache="$PWD/.port-cache/npm" npm run build --workspace web
```

- React Kanban: **6 files, 55 passed** (`react-final.log`).
- 웹: **40 files, 295 passed** (`web-tests-final.log`).
- 실제 desktop production build와 web production build: **exit 0** (`desktop-build-final.log`, `web-build-final.log`). Vitest 경고와 Vite 큰 chunk 경고는 실패가 아니다.
- desktop의 `apps/desktop/build/install-stamp.json`은 최종 carried SHA와 `dirty: false`를 기록했다. 실제 `apps/desktop/dist/assets/index-CwNG10db.js`에도 `tracked_tasks`, `reasoning_coverage` 집계 표시 코드가 포함된 것을 확인했다.
- Electron 앱을 실행해 사람 눈으로 화면을 검수하거나 서명·배포한 것은 아니다. UI 증거는 실제 컴포넌트 렌더링/상호작용 테스트와 production build다.

### bundle 무결성과 독립 반입

- 파일: `patches/hermes-agent-carried.bundle`.
- 크기: **44254 bytes**.
- SHA-256: `8bea9c0579bd9bc856620a05b61cc4bada840203b42c704873525e88e4f64104`.
- bootstrap installer SHA-256: `5854b15670b51a8daae8f59ddfa917062de9f74be261eb73b4b8d719710f8968` (frozen upstream의 실제 파일 내용).

```sh
git -C .port-tmp/fresh-upstream.git -c credential.helper= fetch --no-tags --depth=1 https://github.com/NousResearch/hermes-agent.git 79445a496c86a19332ad786494b8384d2167e2d0
git -C .port-tmp/fresh-upstream.git bundle verify /Users/leeyuhun/cursor-project/.worktrees/unified-kanban-hermes-79445a496/patches/hermes-agent-carried.bundle
git -C .port-tmp/fresh-upstream.git fetch /Users/leeyuhun/cursor-project/.worktrees/unified-kanban-hermes-79445a496/patches/hermes-agent-carried.bundle refs/heads/carried-01:refs/heads/carried-01 refs/heads/carried-02:refs/heads/carried-02 refs/heads/carried-03:refs/heads/carried-03
git -C .port-tmp/fresh-upstream.git rev-list --reverse 79445a496c86a19332ad786494b8384d2167e2d0..refs/heads/carried-03
```

새 bare 저장소를 만든 후 위 공식 HTTPS fetch를 수행했다. 원본 carried refs를 가져오지 않은 저장소에서도 bundle 반입과 세 커밋 선형 체인이 확인됐다.

### 프로젝트 전체 및 릴리스 게이트

```sh
cd /Users/leeyuhun/cursor-project/.worktrees/unified-kanban-hermes-79445a496
UV_CACHE_DIR="$PWD/.port-cache/uv" TMPDIR="$PWD/.port-tmp" uv sync --frozen --group dev
env -u HERMES_HOME -u HERMES_AGENT_REPO -u HERMES_KANBAN_DB -u HERMES_KANBAN_BOARD -u HERMES_PROFILE HOME="$PWD/.venv/port-home" TMPDIR="$PWD/.venv/port-tmp" UNIFIED_KANBAN_TEST_HERMES_SOURCE=/tmp/unified-kanban-hermes-update UNIFIED_KANBAN_TEST_HERMES_REQUIRED=1 .venv/bin/python -m pytest -o addopts='' -q
env -u HERMES_HOME -u HERMES_AGENT_REPO -u HERMES_KANBAN_DB -u HERMES_KANBAN_BOARD -u HERMES_PROFILE HOME="$PWD/.port-home" TMPDIR="$PWD/.port-tmp" UNIFIED_KANBAN_TEST_HERMES_SOURCE=/tmp/unified-kanban-hermes-update UNIFIED_KANBAN_TEST_HERMES_REQUIRED=1 .venv/bin/python -m pytest -o addopts='' -q tests/test_carried_bundle.py tests/test_hermes_release_integration.py
HOME="$PWD/.port-home" TMPDIR="$PWD/.port-tmp" .venv/bin/python scripts/verify-carried-bundle.py --hermes-repo /tmp/unified-kanban-hermes-update
bash -n scripts/setup.sh scripts/uninstall.sh scripts/update-hermes-if-needed.sh scripts/kanban-smoke.sh scripts/bootstrap-hermes-macos.sh
git diff --check
HOME="$PWD/.port-home" TMPDIR="$PWD/.port-tmp" UV_CACHE_DIR="$PWD/.port-cache/uv" uv build --sdist --out-dir .port-tmp/dist
.venv/bin/python .venv/port-tmp/verify-sdist.py
```

- 프로젝트 전체 pytest: **1218 passed in 798.46s (0:13:18)** (`project-all-clean.log`), `PROJECT_FULL_EXIT=0`. release integration 필수 환경변수를 활성화한 단일 전체 실행이며 skip이나 실패가 없다.
- 별도 bundle/release integration 게이트: **27 passed in 53.04s** (`release-gates-final.log`), exit 0.
- bundle verifier: `CARRIED_BUNDLE_PASS refs=3 size=44254 sha256=8bea9c0579bd9bc856620a05b61cc4bada840203b42c704873525e88e4f64104`.
- shell syntax / 양쪽 Git diff whitespace 검사: exit 0.
- 문서·호환성·셸 탐색 추가 확인: `env -u HERMES_HOME HOME="$PWD/.venv/port-home" TMPDIR="$PWD/.venv/port-tmp" .venv/bin/python -m pytest tests/test_maintenance_docs.py tests/test_documentation_style.py tests/test_compatibility.py tests/test_shell_sources.py -o addopts='' -q` → **42 passed in 2.65s** (`docs-final.log`).
- sdist build: exit 0. 기존 라이브러리 배포 계약에 따라 README·pyproject·Hermes version의 실제 아카이브 바이트 일치를 확인했다 (`SDIST_CONTRACT_PASS 3 files`). sdist에 설치기와 carried bundle을 새로 넣지는 않았다.
- release integration은 실제 frozen/carried Git 객체와 production release-manager를 사용하지만, 테스트의 `uv`/`npm`/Python version fixture는 통제된 doubles다. 실제 의존성 설치와 UI build의 증거는 위 별도 Hermes 실행 결과이며, 통제된 fixture 출력을 실서비스 활성화의 증거로 사용하지 않았다.

## 실패 후 해결한 사항

1. 원본 observation/private 테스트 복원 직후 RED는 29 failed/1 passed였다. 새 upstream으로 이동한 import/helper 관련 실패도 포함한다. 모듈 경로를 실제 정의로 교정하고 기능을 이식했다. 별도 CLI·review·schema RED 회귀도 추가했다.
2. token schema 회귀는 bool/float 버전으로 3 failed/4 passed를 확인한 뒤 7 passed로 전환했다.
3. 원본 legacy 화면 문자열만 찾던 테스트는 같은 기능을 current React/API 계약에서도 검사하도록 옮겼다. 기존 legacy 출력에는 별도 회귀를 추가했다. React 보드 헤더는 실제 titlebar contribution 영역에 렌더링되므로 테스트도 `BoardSwitcher`를 명시적으로 마운트하도록 교정했다.
4. 첫 웹 build는 desktop-only 의존성과 누락된 Node 타입 때문에 실패했다. 두 workspace의 lockfile 의존성을 함께 설치하고 해당 upstream 테스트의 타입 참조를 추가한 뒤 빌드와 웹 전체 테스트가 통과했다.
5. bootstrap pin 변경 후 내장 manifest/upstream digest도 갱신했다. updater helper 분리 검증은 RED 후 AST 경로 검사로 고쳤다.
6. 첫 프로젝트 전체 실행에서는 고정 HERMES_HOME이 fixture별 HOME 격리를 무력화했다. 모든 영향은 허용 worktree의 `.port-home`에 한정됐으며 실제 설치를 건드리지 않았다. 프로젝트 테스트에서는 관련 HERMES 환경변수를 제거하고 다시 실행했다. `tests/test_setup.py` 단독 재검증은 **127 passed in 410.78s**였다. 긴 실행의 도구 timeout은 완료로 취급하지 않았다.

7. 다음 전체 실행은 **1214 passed / 4 failed**였다. 저장소 루트의 `.port-tmp`에 권한 음성 테스트 fixture가 남아 셸 소스 탐색에 섞였기 때문이다. 탐색기나 테스트를 약화하지 않고 검증용 자료를 기존 Git 제외 영역인 `.venv/port-tmp`와 `.venv/port-home`으로 옮겼다. `tests/test_shell_sources.py` 재검증은 **14 passed**였으며 같은 위치에서 전체 pytest를 다시 실행했다. 기존 실패 로그는 `project-full-final.log`로 보존했다.

## 수행하지 않은 작업과 안전 경계

- 최종 검증에서 미해결 실패는 없다. 아래 실제 운영 활성화와 수동/다른 플랫폼 검수는 수행 범위에 포함하지 않았다.
- `/Users/leeyuhun/cursor-project/unified-kanban` 주 checkout을 수정하지 않았다. 마지막 read-only `git --no-optional-locks ... status --short` 출력은 비어 있었다.
- 실제 Hermes 설치, 사용자 설정, 실제 Kanban DB, launchd·gateway 서비스를 변경하거나 활성화하지 않았다.
- 프로젝트 commit/push, GitHub publication, 서비스 activation은 하지 않았다.
- frozen SHA를 바꾸지 않았으며 `git reset`/`git clean`을 실행하지 않았다. npm 기본 build의 `tsc --clean` 단계는 생성된 빌드 산출물을 갱신하는 과정이다.
- 테스트에서 수행된 설치/제거/서비스 명령은 임시 fixture 안의 계약 검증이다. 실제 사용자 서비스 activation으로 보고하지 않는다.
- Windows/WSL2, Linux의 systemd 실제 구동, 실제 Electron GUI 수동 검수는 이번 범위에서 검증하지 않았다.
