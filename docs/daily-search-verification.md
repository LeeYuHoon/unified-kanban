# 검색·날짜 기능 후보 검증

아래 기록은 2026-09-09T08:21:37Z의 초기 후보 검증 결과다. 당시 운영 미적용이었고 프로젝트 commit/push/activation 없이 전용 작업 공간에 보존했다. 이후 수정 후보와 운영 배포는 별도 검증하며 아래 과거 수치를 새로운 SHA의 성공 근거로 전용하지 않는다.

최종 검증 완료: 2026-09-09T08:21:37Z. 실행 중 테스트를 남겨 둔 완료 보고가 아니다.

## 소스와 패키지

- 프로젝트: `unified-kanban` 전용 worktree (머신별 절대 경로는 비공개 작업 기록에 보존)
- branch: `feat/kanban-daily-search`
- 프로젝트 HEAD/base: `e54c0b64c67f427f735ac752d7f964aa6f496e62` (프로젝트 commit 없음)
- 공식 frozen SHA: `f03ed94a34f47ebca57e4a1b0a890bc2aeb5e140`
- 이전 carried 마지막: `f9ab37d661d16a09014a0859f49064e8ca1ed7b8`
- 기능 commit: `4e251648401b28408c07e98d0fe72d4043ffbed8`
- 최종 후보 release SHA: `86f0e248b43e386be2a1ee9911c753a76c855d3a`
- 마지막 commit은 기존 음성 설정 테스트의 jsdom spy 두 곳만 수정한다. 제품 음성 코드는 변경하지 않는다.
- 기존 carried 8개는 순서·SHA 그대로, 기능/테스트 commit 2개를 추가한 10 refs다.
- 후보 bundle: `patches/hermes-agent-carried.bundle`, 81166 bytes
- bundle SHA-256: `909261972e1e54aba1895cfb5cfda9c0bda1a7aa45f244721dfd9de9761d3fb4`
- README의 release SHA, carried 목록, bundle metadata가 동기화된다. 공식 pin/bootstrap manifest/Hermes 버전은 변경할 이유가 없어 그대로 유지한다.
- Hermes 구현 clone: `/tmp/unified-kanban-daily-search-hermes`
- 독립 공식 저장소: `/tmp/unified-kanban-daily-search-upstream.git` — GitHub에서 frozen SHA만 fetch했다. 후보 객체가 처음에는 없음을 확인했다.
- 독립 import checkout: `/tmp/unified-kanban-daily-search-imported`

## 구현 파일

프로젝트 변경: `README.md`, `docs/daily-search.md`, 이 문서, `tests/test_daily_search_docs.py`, carried 목록/bundle/metadata.

Hermes 후보 변경:

- `plugins/kanban/dashboard/daily_query.py`: 읽기 전용 검색/전체보드/날짜 집계와 페이지화.
- `plugins/kanban/dashboard/plugin_api.py`: 추가 `GET /api/plugins/kanban/daily` 및 v2 선택적 usage_at 파싱.
- `apps/desktop/src/plugins/kanban/daily.tsx`, `daily-types.ts`, `api.ts`, `board.tsx`: React 조회 화면과 기존 조작 화면의 명시적 전환.
- 같은 경로 `daily.test.tsx`, `desktop-spec.test.tsx`, `observation.test.tsx`, `token-usage.test.tsx`: 실제 DOM과 기존 조작 회귀.
- `plugins/kanban/dashboard/dist/index.js`, `style.css`: legacy UI, 보이는 필터 및 기간 bucket 요약.
- `plugins/kanban/dashboard/daily.browser.js`, `daily-runner.mjs`: 합성 응답 기반 legacy DOM 회귀 도구. 이 실행의 승인 근거는 아래 실제 서버/허용 브라우저 검증이다.
- `tests/plugins/test_kanban_daily.py`: 28개 경계/무결성 회귀.
- `apps/desktop/src/store/voice-prefs.test.ts`: jsdom Storage.prototype spy 교정.

API/시각/null/보관 정책은 [날짜·검색 계약](daily-search.md)을 따른다.

## 실제 테스트와 로그

로그 디렉터리: `/tmp/unified-kanban-daily-search-evidence/`.

| 실행 | 실제 결과 | 로그 |
| --- | --- | --- |
| `UNIFIED_KANBAN_TEST_HERMES_SOURCE=/tmp/unified-kanban-daily-search-hermes UNIFIED_KANBAN_TEST_HERMES_REQUIRED=1 uv run pytest` | 최종 10-ref 후보 1219 passed, 817.82초, exit 0 | project-release-final.log |
| `scripts/run_tests.sh tests/plugins/test_kanban*.py tests/hermes_cli/test_kanban*.py tests/hermes_cli/test_pin_kanban_board_env.py` | 563 passed, 0 failed, 9 platform skips | backend-final.log |
| `npm run test:ui --workspace apps/desktop -- src/plugins/kanban` | 61 passed / 8 files | react-final.log |
| `npm run test:ui --workspace apps/desktop` | 7385 passed / 760 files | react-all-final.log |
| `npm run typecheck --workspace apps/desktop` | 최종 후보 exit 0 | desktop-release-typecheck.log |
| `npm run build --workspace apps/desktop` | 최종 후보 exit 0, renderer/electron 산출물 검증 통과 | desktop-release-build.log |
| `npm run build --workspace web` | 최종 후보 exit 0 | web-release-build.log |
| `npm test --workspace web` | 295 passed / 40 files | web-release-test.log |
| 독립 최종 bundle import 후 daily/token usage/schema pytest | 65 passed | imported-final.log |
| `uv run python scripts/verify-carried-bundle.py --hermes-repo /tmp/unified-kanban-daily-search-upstream.git` | 최종 10-ref/해시/크기 검증 통과 | bundle-final.log |
| `uv build --sdist --out-dir /tmp/unified-kanban-daily-search-evidence/dist` | 최종 README 포함 exit 0 | sdist-final.log |
| 새 Python 3.11 venv에 sdist 설치 후 `python -m kanban_adapter.cli --help` | exit 0, 소스 외부에서 import 성공 | sdist-install.log / sdist-import.log |

sdist는 기존 계약대로 adapter 라이브러리 패키지다. README/Hermes 버전 동기화를 검사하며 installer/bundle 전체가 들어 있는 설치 패키지라고 주장하지 않는다. 설치 후보 bundle은 프로젝트 `patches/`에 별도로 있다. 최종 README 변경 후 sdist도 다시 만들었다.

## RED→GREEN와 독립 리뷰

- backend-red / aggregate-red→green / time-red→green / guards-red→green 로그에 기능 TDD를 보존했다.
- 독립 읽기 전용 리뷰: `backend-review.md`. 리뷰어가 48개 테스트를 실행했다. 제안 재현은 당시 정적 분석이었으며 실행한 것처럼 보고하지 않았다.
- R1 고정 DB의 잘못된 board identity, R2 손상된 생성시각의 undated 누락, R3 비정규 날짜 문자열을 부모가 실제 RED로 재현하고 수정했다. `review-red.log`, `review-green.log`: 수정 후 28 passed.
- `react-notice-red.log` → `react-final.log`: 기본 오늘이 과거 카드를 숨긴다는 안내 회귀.
- 실제 브라우저에서 필터 foreground가 투명한 것을 확인하고 legacy CSS를 기존 `--color-*` 토큰으로 수정했다. `live-guidance-red.json`과 최종 screenshot을 보존했다.
- 전체 React 첫 실행은 음성 설정 테스트 2개만 실패했다. 기능 변경 전 f9ab37d…의 별도 baseline checkout에서도 같은 실패를 재현했다 (`voice-baseline-red.log`). jsdom Storage 프록시에서 인스턴스 spy가 무시되어 오류 경로가 실행되지 않는 문제다. 두 spy를 Storage.prototype으로 변경한 뒤 전체 UI 재실행이 통과했다. 테스트 제거/skip/기대값 완화는 없다.
- 추가 Codex GPT-6 독립 리뷰는 계정의 모델 미지원으로 실패했다 (`final-source-review.log`). 이를 성공/최종 exact-head 승인으로 계산하지 않는다. 확보한 독립 backend 리뷰 지적과 부모의 수정·검증을 구분한다.

## 실제 격리 웹 E2E

주소: `http://127.0.0.1:19139/kanban`.

서버는 새 HOME `/tmp/unified-kanban-daily-search-demo/home`, `HERMES_HOME`/`HERMES_KANBAN_HOME`의 `.hermes`, loopback 19139에서만 실행한다. 기존 19129/운영 9119에는 명령을 보내지 않았다. 시연의 2개 보드/12개 카드는 모두 `[시연 데이터]`로 표시된다. `seed-demo.py`, `demo-records.json`이 fixture 정의다. 현재 생성/완료 날짜는 2026-09-09 Asia/Seoul 기준이다. 실제 사용자나 제공자 비용 데이터가 아니다.

실행 서버 명령:

```sh
env -i PATH="$PATH" HOME=/tmp/unified-kanban-daily-search-demo/home HERMES_HOME=/tmp/unified-kanban-daily-search-demo/home/.hermes HERMES_KANBAN_HOME=/tmp/unified-kanban-daily-search-demo/home/.hermes .venv/bin/python -m hermes_cli.main dashboard --host 127.0.0.1 --port 19139 --skip-build --isolated --no-open
```

최종 증거는 승인된 browser_exec의 실제 브라우저 DOM 조작/읽기와 실제 서버 응답을 사용한다. 응답을 mock으로 바꾸지 않았다. 승인 차단된 별도 Playwright Chrome launch를 다른 스크립트로 우회 재실행하지 않았다. 이전 차단 뒤 얻은 launch 증거는 제외한다. 일반 execute_code도 승인 차단되어 재실행하지 않았으며, 허용된 read_file로 로그를 읽었다.

실제 통과한 화면 검증:

- 현재 보드: 오늘 4, 어제 1, 최근 7일 5, 이번 달 5, 전체기간 6개. DOM 목록 행 수와 실제 API 수를 비교했다.
- 전체보드 12개, 토큰 2100; 날짜표 카드 합계와 기간 카드 12가 일치.
- 오늘 날짜 클릭: 목록 8, 선택기간 요약 12 유지.
- 제목 한글 검색 1개, 본문 `합성 fixture` 검색 전체보드 12개, SQL 모양 문자열 검색 0개.
- 전체보드 done 필터 10개. Hermes 미수집 2개는 토큰 null/N/A, 수집된 0은 0.
- 과거 빈 직접기간: 카드 0, 이벤트 없는 토큰 null/N/A.
- 직접기간 어제~오늘: 카드 5, 토큰 525(실측 175 / 추정 350). 완료일 오늘: 목록 5 / 생성 4 / 완료 5, 오늘 토큰 175로 목록 시각과 독립임을 확인.
- Claude 생성일 추정 350, reasoning null과 ‘출력에 포함’, cache read/write 분리.
- 관찰 카드 drag 불가, 일반 카드 drag 가능. 관찰 상태 제어 노출 제한, 일반 triage/ready 버튼 유지. 읽기 전용 검증으로 실제 상태 이동/댓글 작성은 수행하지 않았다.

실제 요청·응답 기록: `live-periods-final.json`, `live-body-empty-status.json`, `live-custom-completed.json`, `live-controls.json`.

스크린샷:

- `file:///tmp/unified-kanban-daily-search-evidence/live-all-boards.png`
- `file:///tmp/unified-kanban-daily-search-evidence/live-claude-final.png`
- `file:///tmp/unified-kanban-daily-search-evidence/live-observation.png`
- `file:///tmp/unified-kanban-daily-search-evidence/live-final-default.png`

최종 재빌드 뒤 기본 오늘 화면도 다시 열어 카드 4/생성 4/완료 5와 런타임 error/unhandledrejection 0을 확인했다 (`live-final-health.json`). 최종 확인에서 프로젝트 main은 clean이고 HEAD는 기준 SHA 그대로, 이전 read-only Hermes source도 clean이며 f9ab37d… 그대로다. 보존한 시연 listener는 19139의 PID 60113이다.

초기 후보의 React는 전체 DOM 테스트와 typecheck/빌드로 검증했고, 실제 웹 서버 E2E 화면은 legacy dashboard다. Electron 앱을 별도로 launch했다고 주장하지 않는다. 당시 대규모 전체보드 성능 벤치마크, 전역 동시 snapshot, macOS 밖의 실제 실행은 검증 범위가 아니었다. 이 문서의 초기 검증은 운영 활성화 완료를 뜻하지 않는다.

## 독립 리뷰 지적 수정 후보

초기 후보의 독립 최종 리뷰는 승인 대신 5개 수정 요청을 반환했다. 아래는 그 지적을 수정한 후속 후보이며 독립 재승인이나 운영 배포 완료를 뜻하지 않는다.

- 최종 source: `74ae6aec6de309f8042d542cb0e65a130733cbba`
- source tree: `6ce66656849faeddabff20dda2f7ce093ae4f758`
- 기존 8+2개 commit은 보존하고 수정 commit `9ed905a2d9475d5af2e775f2233875fb148d3fd8`, `74ae6aec6de309f8042d542cb0e65a130733cbba`만 추가했다.
- bundle: 12 refs, 94301 bytes, SHA-256 `ec2f95cadf9665819f474232e1e46b3710c0b862e2d79aa58dd6fe618f85517d`.
- frozen upstream과 Hermes 버전은 그대로다. README와 carried manifest/metadata/bundle을 동기화했다.

수정 및 실패 재현:

1. HTTP 댓글의 예약 작성자 사칭은 실제 endpoint에서 200으로 수용됨을 재현한 뒤 400으로 거부했다. 일반 댓글은 유지하며 실제 로컬 CLI의 어댑터/Hermes 사용량 입력이 계속 집계됨을 새 프로세스로 검증했다. 로컬 계정 소유자에 대한 암호학적 인증이나 소급 진위 검증을 주장하지 않는다.
2. 과거 카드 2,000개에서 전체 카드 행 2,001개가 전송되는 실패를 재현한 뒤 최종 페이지 한 건만 전송하도록 바꿨다. 일반 댓글 파싱 제외, 변경 없는 사용량 해석 재사용, 수정/삭제/교체 무효화, 공통 작업 예산 초과 시 부분합 없는 503을 검증했다. 8개 보드 캐시와 요청 보드 수를 분리하고 큰 result 필드에도 예산을 적용하는 추가 RED→GREEN을 포함한다. legacy 연속 검색 입력 3회 요청을 재현한 뒤 300ms debounce와 unmount 취소를 검증했다.
3. React의 다른 보드 결과 상세를 열고 닫으면 검색 보드가 바뀌는 실패를 재현했다. drawer에 별도 board 문맥을 주어 기간·검색어·날짜·페이지를 보존했다. 후속 지연 dispatch도 변경 당시 보드로 고정하며 서로 다른 보드의 요청을 합치지 않는다. 잘못된 현재 보드로 dispatch되는 추가 실패도 회귀로 고정했다.
4. 성공한 변경/socket 이후 검색 결과가 갱신되지 않는 실패를 재현했다. 활성 필터 결과와 다른 보드 키에 캐시된 전체보드 검색이 함께 무효화되는지 확인했다.
5. React 날짜별 사용량 상세가 없는 실패를 재현했다. 일별 전체/실측/추정 bucket, 일부 수집, 수집된 0, N/A, reasoning 출력 포함, 이벤트·수집 카드 수를 상세에서 확인한다.

최종 source 회귀: Kanban backend 578 passed / 9 platform skips, React 전체 7390 passed / 762 files, Kanban UI 66 passed / 10 files, web 295 passed / 40 files. Desktop typecheck·build와 web build는 exit 0이다. 프로젝트 release-required 회귀와 독립 bundle import는 별도 완료 기록으로 함께 보존한다.

DOM 회귀는 fixture 응답을 사용하는 단위 테스트다. 이 수정 후보로 운영 Dashboard를 재시작하거나 운영 DB를 수정하지 않았으며, 기존 시연 화면을 새 source의 운영 증거로 전용하지 않는다. 최종 독립 리뷰와 publication/activation은 다음 승인 단계다. 상세 작업 로그는 공개 저장소 밖에 보존한다.

## 검색 UDF 전달 전 크기 제한 후속 수정

위 `74ae6aec…`의 독립 리뷰에서 frontend는 승인됐고 backend에는 제목·본문이 크기 검사 전에 Python UDF에 전달된다는 M1이 남았다. 승인된 UI 파일은 변경하지 않고 다음 source commit 하나만 추가했다.

- source: `3b76df96b074d192d22b95e0d181ab8eac12b063`
- tree: `37ced714438af3b906e2f71b6c3a97c789eeb2b1`
- bundle: 13 refs, 95689 bytes, SHA-256 `0001beff110857705e49d85da6c0b2b9858ea4630e47514d252aa240553c5351`
- 변경: `plugins/kanban/dashboard/daily_cache.py`, `tests/plugins/test_kanban_daily.py`만 변경했다. UI는 `74ae6aec…`와 바이트 동일하며 source history는 유지한다.

실제 spy 회귀에서 title/body 각각의 4MiB ASCII와 1MiB를 넘는 UTF-8 문자열이 cold/cached 조회 모두 Python UDF에 전달되는 8개 실패를 확인했다. SQL 지연 CASE에서 byte length를 검사한 뒤에만 casefold를 호출하도록 수정했다. 같은 8개 회귀가 거대 값 미전달·503·부분합 부재를 검증하며, Unicode casefold/한글/문자 그대로의 `%`·`_`/NULL 본문 회귀 3개도 통과했다. backend Kanban 전체는 589 passed / 9 platform skips다. Python 문자열 전송 전 거부를 검증했으며 SQLite 내부 할당이 없다고 주장하지 않는다.

이 후속 source의 독립 backend 재승인·게시·운영 적용은 아직 별도 단계다. 이전 UI 승인과 회귀 수치를 이번에 다시 실행한 것으로 표기하지 않는다.
