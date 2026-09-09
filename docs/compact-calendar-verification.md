# 단일 달력 UI 후보 검증

## 후보 신원과 범위

- 프로젝트 기준: `4db4d38810d0aee6b6a93029cb6f9db584236032`.
- Hermes 기준: `898adeb837d4879df80189e1b5f6dcbf97542649`.
- UI 비교 기준: `f9ab37d661d16a09014a0859f49064e8ca1ed7b8`.
- 후보 source: `6bf9ab55ad8614a3f498b152694dea36f5ff92d9`.
- 후보 tree: `fce0a6fd3993691c3715b306c309b971eff23124`.
- 공식 frozen base: `f03ed94a34f47ebca57e4a1b0a890bc2aeb5e140`, Hermes `0.21.1` 유지.
- bundle: 기존 compact/KMB/두 요약 후보와 선택일 실행 필터 후보를 보존한 20 refs, 147548 bytes.
- bundle SHA-256: `731250385ffa795cb570ca1c6ead41ebaf9435fba711deaacc7a1b5ff57710b1`.

프로젝트 commit/push, 설치, 서비스 재시작, 운영 DB 변경은 하지 않았다. source commit은 후보 bundle 재현을 위해 격리 clone에만 만들었다. 이 문서는 운영 활성화 또는 최종 화면 승인 기록이 아니다.

## 구현

Legacy와 React는 전체기간 누적 요약을 보드 선택 위에 두고 기존 요약 자리에 선택일 상세 요약을 표시한다. 날짜는 기본 KST 오늘이며 현재 보드만 조회한다. Legacy도 날짜 state를 보드 선택보다 상위 `KanbanPage`에 두므로 로딩 중 보드를 바꿔도 input과 새 요청의 start/end/day가 유지된다. 작은 달력 하나를 유지하며 중복 실측 한 줄과 큰 검색 패널은 없다. 날짜를 지워도 전체기간 요청으로 바꾸지 않는다.

`summary.measured`의 Total/Input/Cache/Output/Reasoning/실행 경로별 토큰·고유 카드 수를 읽으며 추정량·누적량으로 대체하지 않는다. Claude·Codex 완료 snapshot은 `completion_attributed`로 분리해 선택일 실측에 합치지 않되 누적에는 보존하며 Legacy response state도 이 필드를 버리지 않고 `완료시각 귀속` 문구로 전달한다. 미수집은 N/A, 수집 0은 0, 조회 실패는 N/A · 조회 실패다. Claude Code는 CLAUDE, Codex는 CODEX, Hermes는 실제 모델에 따라 CLAUDE/CODEX/모델미상/기타로 정확히 한 번 분류한다. 카드 목록은 선택일 실행 증거로 필터링하고 누적은 날짜와 독립이다. 양 UI는 `task_ids`가 없을 때 `has_more === false`, offset 0, page 길이와 total 일치를 모두 확인하기 전에는 hydration page를 완전한 필터 집합으로 간주하지 않는다.

`plugin_api.py`와 `daily_query.py`는 같은 모델 분류 helper와 중복 제거된 이벤트를 사용한다. `basis=execution`은 `task_runs` 구간 overlap과 신뢰된 `usage_at`을 실행 증거로 사용하며 생성일은 사용하지 않는다. 자정에 걸친 run은 겹친 날짜마다 카드로 표시하지만 토큰은 해당 날짜의 이벤트만 센다. 기존 source/model_families 호환 필드는 유지했다. 관찰 보호, 댓글 작성자 사칭 방어, bounded 읽기/503, 보드 문맥 고정, mutation/socket 캐시 무효화는 보존한다.

## 선택일 실행 필터 후보 검증

운영 9119와 기존 시연 서버를 중지·재시작하지 않고, 격리 HOME의 `daily-demo` 보드와 포트 19284에서 exact candidate `7706053948edf90a7e347fa718b30141bc0bb274` Dashboard `/kanban`을 열었다. 공식 loader는 `plugins.enabled: [kanban]`인 user plugin의 backend를 `/tmp/unified-kanban-daily-review-demo-home-d/.hermes/plugins/kanban/dashboard/plugin_api.py`에서 선택했고 frontend도 같은 plugin directory의 asset과 실제 HTTP served bytes가 일치했다. KST `2026-09-10` 실제 API는 요청시각 실측 2700, 완료시각 귀속 1500000, 합계 1502700이며 화면에 실행 카드 4개, CODEX 200, 기타 2.5K, `완료시각 귀속 1.5M tokens`가 표시됐다. 날짜 input을 실제 React change로 `2026-09-09`로 바꾸면 자정 overlap 카드 2개와 CLAUDE 100 tokens만 남고 보드 전체기간 누적 문자열은 그대로였다. 이 수치는 보존된 합성 fixture이며 실제 제공자 청구 증거가 아니다. 상세 hash와 screenshot 경로는 `/tmp/unified-kanban-daily-execution-runtime-verification.md`에 기록했다.

합성 DB는 실제 제공자 청구 증거가 아니며 운영 DB에 backfill하지 않았다. 과거 timestamp 없는 누적은 누적에만 남고 선택일 실측으로 가장하지 않는다. publication·운영 activation은 수행하지 않았고, 다음 단계는 독립 최종 리뷰다.

## P2 독립 재리뷰 보완

Legacy 진행중 state의 누적 token과 새 request event를 잔여량으로 분리하고 날짜·모델을 추측하지 않는 회귀, period=all 다일 run의 모든 overlap 날짜와 공유 work budget, 기간 밖 day의 일관된 400, Legacy 상위 날짜 state와 completion-attributed 전달, 양 UI의 명시적 완전성 증거를 추가했다. 최신 Hermes candidate는 `7706053948edf90a7e347fa718b30141bc0bb274`, tree `4c38d890bae34beeec211239428373e4400fde6b`이며 carried bundle은 22 refs다. 제품 publication·운영 activation은 독립 재리뷰 PASS 전까지 수행하지 않는다.

## 최신 두 요약바 검증

독립 frontend 리뷰 M1 후속: Legacy `/board`가 이전 보드 응답으로 현재 카드·누적을 덮는 race를 재현했다. success/error/finally를 보드·필터 문맥과 요청 세대로 보호하며 오래된 콜백 호출 및 unmount 응답도 무시한다. 실제 전체 bundle DOM에서 이전 보드의 성공/오류/로딩 종료와 같은 보드의 역순 응답을 검증했다. 승인 레이아웃·`/daily`·backend 제품 코드는 변경하지 않았다. 아래 16-ref 및 6364 단계 수치는 후속 이전의 이력이다. 최종 M1 검증·release-required 결과와 독립 재리뷰 gate는 로컬 release 결과 보고서를 따른다.

이 변경을 커밋하기 직전 backend 104개, Kanban UI 91개, Desktop 전체 UI 7415개와 typecheck/build가 통과했다. 이후 제품 코드는 변경하지 않았다. 한 카드의 복수일·모델, 중복 이벤트, 기간 밖 생성일, limit=1, null/0/output 포함을 검증했다.

최종 16-ref bundle을 공식 object DB 기준으로 검증하고 별도 clone에 import하여 HEAD/tree 일치와 clean checkout을 확인했다. 독립 import의 backend 104개도 통과했다. 프로젝트 문서·bundle·producer 대상 pytest와 sdist 생성은 exit 0이다. 최초 단계의 프로젝트 전체 1220개 결과와 이번 대상 회귀는 서로 다른 실행이며 혼용하지 않는다.

실제 Legacy 19149와 React 19150에서 날짜 네 개를 변경했다. 합성 선택일 값은 2026-09-09 2.7K, 09-08 2.4K, 09-07 0, 08-01 N/A다. 두 화면 모두 카드 13개와 누적 문자열이 그대로였다. 누적에는 GPT/Claude/모델미상/기타모델이 보이며 사용일 없는 이력은 일별로 옮겨지지 않는다. 복수일 시연의 잘못된 Claude header만 실제 producer의 `Agent tool usage`로 교정한 뒤 최종 화면을 다시 검증했다. 제품 parser를 잘못된 fixture에 맞춰 느슨하게 만들지 않았다.

기존 격리 서버는 부모가 승인 terminal에서 복구했다. 서버 중복 실행과 seed 재실행은 하지 않았다. 이 결과는 합성 데이터와 후보 컴포넌트 harness의 검증이며 운영 실측·native Electron 전체 shell 검증이 아니다. 아래는 이전 단계의 역사적 검증이며 현재 결과로 재집계하지 않는다.

## 최초 compact 후보에서 완료한 검증

| 검증 | 결과 |
| --- | --- |
| 프로젝트 release-required 전체 pytest | 1220 passed, 888.94초 |
| 카드 목록 유지 요구 RED | 이전 UI에서 과거 카드가 보이지 않아 실패 확인 |
| Kanban React + legacy 전체 bundle DOM 회귀 | 67 passed, 10 files |
| Desktop 전체 UI | 7391 passed, 762 files |
| Kanban backend/CLI 회귀 | 589 passed, 9 플랫폼 skips |
| Desktop typecheck | exit 0 |
| Desktop build | exit 0, postbuild 산출물 확인 포함 |
| Web build | exit 0 |
| Web 테스트 | 295 passed, 40 files |
| 독립 공식 object DB 기반 bundle 검증 | 14 refs/순서/단일 부모 chain/prerequisite/hash/크기 검증 통과 |
| 독립 bundle import | source/tree 일치, clean checkout |
| 독립 import 뒤 날짜/ingestion/token/schema 회귀 | 91 passed |
| sdist 생성 및 새 Python 3.11 venv 설치 | 성공, 소스 외부에서 CLI --help 실행 성공 |
| 한국어 README/날짜 안내 회귀 | 통과 |

DOM 회귀는 합성 API fixture이며 실제 서버 화면 검증을 대신하지 않는다. KST 자정, 날짜/보드 늦은 응답, 미수집·0·오류, 빈 날짜 요청 억제, 날짜 변경 전후 카드 DOM 유지와 mutation/socket 갱신을 확인한다. legacy 테스트는 일부 함수 소스 문자열을 검사하지 않고 전체 bundle을 실행한다. 전체 UI 테스트에는 기존 jsdom 미구현 window.open/canvas 안내와 npm/Vite 설정 경고가 있으나 실패로 집계되지 않았다.

## 격리 화면 검증과 승인 경계

다음 최초 화면 검증 이후 K/M/B 표기 교정을 추가했다. 현재 React 전체 UI 7413개와 Kanban UI 89개, typecheck 및 Desktop build가 통과했다. 모든 토큰 표시(보드·카드·상세·선택일·작업 추정)를 K/M/B로 통일했고 작은 값·0·N/A와 output-included 구분을 유지한다. 파서·집계·일별 제외 규칙은 변경하지 않았다.

시연에는 Claude 합성 기록을 별도로 추가했다. 현재 보드마다 카드 8개이며 demo-alpha에서 Claude 2M과 GPT 9.5K가 함께 보인다. 오늘 선택일은 합성 Claude 1500과 Codex 175를 합친 1.7K로 표시한다. 날짜를 바꾸어도 Claude 누적·카드 목록이 유지되는 것을 두 화면에서 확인했다. 사용시각 없는 Claude 누적은 선택일 실측으로 대체하지 않는다. 운영은 읽기 전용 집계만 확인했으며 사용시각 없는 정상 Claude 기록이 누적에 보존되는 것을 확인했다. 운영 집계의 숫자·경로는 공개 문서가 아닌 로컬 결과 보고서에만 보존했다.

부모가 격리 seed를 검토하고 정상 승인 terminal에서 실행한 뒤 기존 시연 HOME을 그대로 사용했다. seed는 재실행하지 않았다. Legacy는 `http://127.0.0.1:19149/kanban`, React 후보 컴포넌트 시연은 `http://127.0.0.1:19150/`에서 확인했다. 두 화면 모두 합성 데이터이며 운영 실측의 증거가 아니다.

각 화면에서 KST 기본 날짜 `2026-09-09`의 175, 전날 350, 이틀 전 수집된 0, `2026-08-01`의 N/A를 확인했다. 날짜 변경 전후 카드 6개의 식별자 또는 제목·관찰 drag 속성이 동일했다. 다른 시연 보드로 전환하면 1,750으로 바뀌고 원래 보드로 복귀하면 175로 돌아왔다. 큰 일별 검색 패널·기간 필터·상세표는 없고 기존 카드와 누적 요약은 남는다. 관찰 카드 안내와 제어 제한도 확인했다.

React 시연은 실제 후보 컴포넌트와 exact-head build CSS를 사용하되 Desktop 전체 shell 대신 별도 조회검증용 연결 harness로 렌더링했다. API 응답을 mock하지 않고 격리 서버의 정상 세션 인증을 사용했다. native Electron 실행 및 socket 실시간 갱신의 실화면 검증은 아니다. 날짜 변경은 실제 브라우저 input/change 이벤트와 서버 응답으로 확인했으며 OS 네이티브 달력 팝업 자체를 캡처했다고 주장하지 않는다.

스크린샷과 브라우저 assertion JSON은 로컬 증거 디렉터리에 보존했다. 운영 9119의 기존 PID와 프로젝트 main은 유지됐다. 프로젝트 commit/push·운영 배포는 하지 않았다. 최종 선택적 증거 집계 명령은 승인 gate에 막혀 실행하지 않았으며 정확한 명령을 로컬 보고서에 기록했다. 완료 근거는 기존 실제 테스트 로그와 개별 브라우저 assertion이다.

차단 명령·전체 실제 로그·로컬 작업 공간·후속 검증 상태는 `/tmp/unified-kanban-compact-calendar-result.md`에 기록한다. 공개 문서에는 머신별 사용자 절대 경로나 실 데이터 내용을 싣지 않는다. 현재 producer가 실제 사용시각을 제공하지 않는 과거 기록에서 N/A가 나오는 것은 정직한 제한이며 가짜 일별 수치를 채우지 않는다.
