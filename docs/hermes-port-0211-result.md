# Hermes 0.21.1 호환성 및 로컬 적용 결과

상태: macOS 로컬 0.21.1 활성화와 적용 후 검증 완료. 아래는 GitHub publication 승인 전의 검증 기록이며, 당시에는 프로젝트 commit·push를 하지 않았다. 이후 사용자 승인에 따른 publication 결과는 이 문서를 포함한 PR과 해당 commit의 CI 기록에서 확인한다.
모델: GPT-6 Astra / openai-codex. macOS에서 실행했다.

## 고정 신원

- Hermes 버전: `0.21.1`.
- 공식 기반: `2237be355906fbe6065ce1815711eee52b2d646e`.
- 최종 carried: `391dfc063dd3709d44eb8a729df58566ac801b8d`.
- carried 순서: `45a2750ccdca0b7f549af056a6b857ae8e5bce79`, `b6b15fc68089da7a68672bfadc7ac0b292521f32`, `7f0d41609cea6d20bed01f0374c49441567ee4c7`, `ba1f829fe7b1c9b75899f0a04d7ac589392a3e76`.
- 위 네 커밋 다음에 독립 리뷰 수정 `391dfc063dd3709d44eb8a729df58566ac801b8d`를 추가했다. 기존 후보와 history는 그대로 보존했다.
- bundle: 5 refs, 46856 bytes, SHA-256 `d1b8c64e38ffcd7dd919aaa94054624c3b9a98182ac7ef2c25319508613d2692`.
- 공식 installer 내용은 이전 기반과 같아 SHA-256 `5854b15670b51a8daae8f59ddfa917062de9f74be261eb73b4b8d719710f8968`을 유지했다. URL·manifest·pin digest는 새 SHA로 갱신했다.

## 이식과 충돌 해결

이전 검증된 네 carried commit을 새 전용 Hermes worktree에 이식했다. upstream에서 이동한 graph 모듈을 이전 파일로 덮어쓰지 않았다. 새 creator origin, tenant 상속, PR 완료 계약, last_failure_error 출력과 delegated child 보호를 유지했다.

- 관찰 카드의 worker 제외, lifecycle, TTL, 합성 run 부재, 제목·결과 파일 안전성, 댓글 멱등성을 유지했다.
- 새 `kanban_db_graph.decompose_triage_task`에 관찰 제외 조건을 옮겼다. 관찰 카드가 PR 완료 계약과 함께 생성되는 것도 거부한다.
- 신뢰 토큰 v1/v2 검증, null coverage, 중복 제거, 모델 계열, 보관 카드 누적, JSON ValueError/RecursionError 방어를 유지했다.
- 명시적 보드 삭제, legacy UI와 현재 React UI를 모두 유지했다.

## 실행한 검증

로그는 `/tmp/unified-kanban-0211-*.log`에 보존한다. 테스트용 HOME과 TMPDIR는 `.venv` 아래에 두었으며 운영 DB와 분리했다.

- 초기 관찰 RED: 33 failed / 1 passed (`red.log`).
- 이동한 graph 및 PR 계약 RED: 2 failed / 4 passed (`graph-red-valid.log`).
- Hermes Kanban: 69 files / 531 passed / 8 skipped (`hermes-focused.log`). skip은 upstream 비활성 또는 플랫폼 전용 경로다.
- Hermes updater: 4 files / 34 passed (`updater.log`).
- React Kanban: 7 files / 58 passed (`react.log`). 웹: 40 files / 295 passed (`web-tests.log`).
- desktop 및 web production build: exit 0 (`desktop-build.log`, `web-build.log`).
- bootstrap RED: 33 failed / 22 passed. pin·fixture 동기화 후 release+bootstrap: 58 passed (`release-bootstrap.log`).
- 독립 bare 저장소에 official frozen SHA만 HTTPS fetch한 후 bundle verify/fetch 및 네 커밋 순서를 검증했다. 기존 carried 객체를 공유하지 않았다.
- sdist 실제 생성 후 README·pyproject·Hermes version의 아카이브 바이트 일치: PASS. 아카이브 SHA-256 `58319466a173c528b32f86679b005e8990f7b2cf5a9805ba708579e5f5271a49`.
- bundle verifier, Bash syntax, 양쪽 diff whitespace: PASS.

최종 프로젝트 전체 pytest는 1218 passed, 875.83초, exit 0이다 (`project-reviewed.log`). release-required 검증을 포함한다. 최종 독립 read-only 리뷰는 보안·논리 차단 사항 없이 승인됐다 (`/tmp/unified-kanban-0211-review-final.json`). 아래 운영 검증은 이 승인 이후 수행했다.

## 독립 리뷰 수정

첫 read-only 리뷰는 관찰 카드에 후속 dependency를 연결한 뒤 부모를 재개하면 관찰 카드가 todo에 갇히는 carried 결함을 발견했다. 새 회귀에서 3 failed / 6 passed를 확인한 뒤, 관찰 자식에 대한 새 연결을 거부하고 기존 연결의 descendant invalidation과 완료 dependency gate를 제외했다. 기존 사용자 데이터의 연결 자체는 지우지 않는다.

수정 후 Hermes 전체 Kanban은 534 passed / 8 skipped이며 최종 React 58 passed, web 295 passed, desktop/web build도 다시 성공했다. 최종 sdist 계약을 재검증한 SHA-256은 `697c6c572799d75c60a69c084b81667d956831f8a0df0069e0ffddc57b3b111c`다. 위 최초 후보 수치는 당시 증거로 보존한다.

legacy 화면은 이전 동작을 보존했다. 관찰 카드의 일부 불가능한 조작이 보이지만 서버가 거부하는 UX 개선 권고는 비차단 후속 사항이다. trusted token은 단일 사용자·동일 권한에서 author/header/schema/event 일치를 검증하는 계약이며 암호학적 작성자 인증이라고 주장하지 않는다.

## 운영 보호와 Git 범위

- canonical Hermes checkout의 기존 추가 사용자 커밋을 보존하며 selector 방식으로 전환한다. reset/clean하지 않았다.
- 운영 SQLite 15개를 online backup하고 각각 integrity_check=ok를 확인했다. 백업 폴더는 0700, DB는 0600이다. 인증값·원문 설정은 문서나 로그에 출력하지 않았다.
- 원래 Dashboard는 중지 상태다. 기존 CLI/TUI/Desktop 세션을 종료하지 않는다.
- 이 로컬 검증 당시 프로젝트 commit/push는 승인 전이어서 수행하지 않았다. 운영 연결은 안정적인 주 checkout 경로를 사용하며 전용 작업 worktree를 가리키지 않는다.
- Windows/WSL2 및 Linux 실제 실행은 검증 범위 밖이다.

## 최종 운영 적용

- 실행일: 2026-09-08 KST. 공식 `scripts/update-hermes-if-needed.sh`의 prepare 후 activation transaction이 exit 0으로 완료됐다 (`activation.log`). 실행 중 official main 이동은 알림만 남기고 고정 기반을 유지했다.
- 실제 설치: 관리 release 디렉터리의 `release-391dfc063dd3709d44eb8a729df58566ac801b8d`.
- 관리 launcher와 regular `current` selector를 확인했다. `hermes --version`은 0.21.1과 위 release를 반환했다. 개인별 절대 경로는 공개 기록에서 제외한다.
- Gateway는 새 carried SHA, 0.21.1, running, restart_requested=false를 보고했고 Telegram·Feishu 모두 connected였다. 적용 직후 시작 중 상태는 재조회하여 정상 상태를 확인했다 (`runtime-after.json`).
- launchd의 두 Python 실행 경로 모두 새 release venv를 가리킨다. `verify-macos-launchd-service.py --expected-release` 검증 exit 0. `hermes gateway status`의 stale 경고와 별개로 실제 plist의 봉인 환경·실행 경로와 살아 있는 Gateway 코드 SHA를 확인했다.
- 원래 중지된 운영 Dashboard는 중지 상태로 유지했다. 최종 release의 격리 Dashboard `/api/status`와 브라우저 Kanban 관찰 카드·토큰 표시를 확인했다 (`ui-health-reviewed.json`, `ui-reviewed.png`). 테스트 서버만 종료했고 기존 CLI/TUI/Desktop 세션은 종료하지 않았다. 기존 세션은 재실행 전까지 이미 로드한 코드를 유지할 수 있다.
- 안정 주 checkout에서 `scripts/setup.sh --dry-run --no-restart --skip-smoke`, 이어서 실제 `--no-restart --skip-smoke`를 두 번 실행했다. 두 snapshot이 byte-identical이었다 (`setup-first.log`, `setup-second.log`, `managed-first.json`, `managed-second.json`).
- `kanban-adapter`, `claude-kanban-hook`, `codex-kanban-hook`, `ai-session-viewer`는 모두 안정 프로젝트 checkout의 `bin/` 아래를 가리킨다. Hermes plugin도 같은 안정 checkout에 연결됐다. 운영 연결에 작업 worktree 의존은 없다.
- 설치된 launcher로 별도 HOME/DB에 보드를 만든 뒤 공식 `scripts/kanban-smoke.sh`를 실행했다. 댓글·완료 결과 readback과 archived cleanup까지 PASS (`live-smoke.log`). 운영 보드에 smoke 카드를 만들지 않았다.
- runtime 사용 후 release manager의 `_verify_completed_release` 검증 PASS. 전체 봉인 payload와 bytecode fingerprint를 확인했다 (`final-check.log`).
- 최종 updater `--check`는 UP_TO_DATE, 재실행은 SKIPPED를 반환했다 (`check-after.log`).
- canonical checkout HEAD `02d659dd2bfd87d8a2ac731c2dc106c06033d3e0`, clean 상태와 전체 refs가 적용 전후 동일하다. 비관리 Hermes YAML 설정 의미 및 기존 Claude settings/Codex config 바이트 보존 검사도 PASS다.

## 재현 명령과 증거

모든 다음 로그 접두사는 `/tmp/unified-kanban-0211-`이다. 이전 실패·중간 후보 로그를 최종 성공 증거로 혼용하지 않는다.

- 프로젝트: 격리 HOME/TMPDIR, `env -u HERMES_HOME`, `UNIFIED_KANBAN_TEST_HERMES_SOURCE=/tmp/unified-kanban-hermes-0211-bundle.git`, `UNIFIED_KANBAN_TEST_HERMES_REQUIRED=1`로 `.venv/bin/python -m pytest -q` (`project-reviewed.log`). Git worktree 자체를 source fixture에 주었을 때의 objects 경로 setup 오류는 독립 bare source로 해결했다.
- Hermes: 격리 HOME/HERMES_HOME에서 `HERMES_TEST_FILE_RETRIES=0 scripts/run_tests.sh`에 Kanban CLI·plugin 회귀를 전달했다. 최종 534 passed / 8 skipped. updater 별도 회귀 34 passed.
- UI: `npm run test:ui --workspace apps/desktop -- src/plugins/kanban`, web 테스트 및 desktop/web production build. 최종 React 58, web 295 passed, build exit 0.
- 운영 명령 환경: `/tmp/unified-kanban-0211-live-env.sh`가 실제 발견한 Python 3.12, NVM Node 24, uv의 절대 PATH와 canonical HERMES_AGENT_REPO를 설정한다. 이 임시 실행 보조 파일은 운영 launcher/plist의 의존성이 아니다.
- 최종 읽기 전용 검사: `bash /tmp/unified-kanban-0211-live-env.sh python3 /tmp/unified-kanban-0211-final-check.py`. 구현은 안정 프로젝트의 release manager를 불러 봉인을 검증한다.
- 백업 manifest는 비공개 로컬 증거로 보존했다. 운영 DB·백업의 절대 경로와 민감한 원문은 공개·커밋하지 않는다.

## 로컬 검증 종료 당시의 미수행 범위

로컬 검증 종료 당시 프로젝트 수정은 전용 `compat/hermes-0.21.1` worktree에 보존했다. 안정 운영 경로 확보를 위해 변경 전 clean main HEAD `84e8dfc703cf735894486787ae04d8f778472934`를 확인한 뒤 승인된 9개 tracked 파일과 이 결과 문서만 주 checkout에 복사했다. 당시 main 작업 파일에는 반영됐지만 main 커밋·branch merge·GitHub push/publication은 아직 수행하지 않았다. Hermes 포팅용 로컬 carried commit 5개만 생성했다. 다른 worktree와 daily automation은 변경하지 않았다. 이후 publication 단계에서는 설치·재시작 없이 승인된 변경을 검증하고 PR/CI를 거쳐 반영한다.
