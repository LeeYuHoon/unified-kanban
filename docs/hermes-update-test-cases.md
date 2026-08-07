# Hermes 업데이트 테스트 케이스

Hermes Agent 업데이트 후 Unified Kanban 통합을 승인하기 위한 반복 가능한 테스트 케이스다. 명령은 저장소 루트에서 실행하며, 상세 절차와 복구 규칙은 [`hermes-update-checklist.md`](hermes-update-checklist.md)를 따른다.

## 판정 원칙

- 자동화 테스트, carried patch, 설치, 실제 CLI 왕복, 서비스, Dashboard UI를 모두 확인한다.
- 비밀정보와 device code는 문서나 로그에 복사하지 않는다.
- 테스트가 수집되지 않았거나 모델 호출이 실패한 경우 성공으로 계산하지 않는다.
- Windows/WSL2/Linux는 실제 검증 전까지 지원 대상으로 표기하지 않는다.

## 테스트 매트릭스

| ID | 영역 | 실행 또는 조건 | 통과 기준 |
| --- | --- | --- | --- |
| HU-01 | 버전 | `hermes version` | install directory가 대상 checkout이며 upstream SHA와 carried commit 수가 표시된다. |
| HU-02 | 인증 | `hermes auth status openai-codex` | credential 값을 노출하지 않고 `logged in`이다. |
| HU-03 | update check | `./scripts/update-hermes-if-needed.sh --check` | 최신이면 `UP_TO_DATE`, 뒤처졌으면 정확한 commit 수를 보고한다. 서비스나 pending marker를 변경하지 않는다. |
| HU-04 | clean skip | 최신 상태에서 updater 재실행 | `SKIPPED`, Dashboard PID 불변, setup과 서비스 restart 없음. |
| HU-05 | stale applied state | upstream은 최신이고 state SHA만 이전 값 | `REPAIRING` 후 setup을 재실행하고 현재 upstream SHA를 기록한다. |
| HU-06 | CRLF state | state SHA가 현재 SHA와 같고 CRLF 종료 | 불필요한 repair 없이 `SKIPPED`한다. |
| HU-07 | unreadable upstream ref | state 점검 중 `origin/main` 해석 실패 | setup을 실행하지 않고 오류로 종료한다. |
| HU-08 | carried recovery | manifest commit의 patch-equivalent가 누락 | bundle에서 object를 가져와 순서대로 재적용한다. |
| HU-09 | bundle integrity | `git bundle verify`, `git bundle list-heads` | bundle이 유효하고 `carried-01`부터 `carried-11`까지 11개 ref가 있다. |
| HU-10 | fresh import | fresh upstream clone에서 bundle fetch | manifest의 11개 commit object를 모두 조회할 수 있다. |
| HU-11 | setup preflight | `./scripts/setup.sh --dry-run --no-restart --skip-smoke` | 파일·설정·서비스를 바꾸지 않고 전체 명령을 출력한다. |
| HU-12 | setup idempotency | 실제 setup을 같은 옵션으로 2회 실행 | 두 번째 실행이 hook, link, plugin 항목을 중복 생성하지 않는다. |
| HU-13 | project tests | `uv sync --frozen --group dev && uv run pytest -o addopts='' -q` | 현재 기준 654 passed. |
| HU-14 | Hermes regression | checklist의 isolated 5-file pytest 명령 | 현재 기준 93 passed, 1 skipped. |
| HU-15 | shell/static | `bash -n ...` 및 `git diff --check` | syntax 및 whitespace 오류가 없다. |
| HU-16 | CLI smoke | `./scripts/kanban-smoke.sh` | create → comment → complete → archive 왕복 후 `SMOKE PASS`. |
| HU-17 | services | `hermes gateway status`, `hermes dashboard --status` | Gateway가 supervised/running이고 Dashboard가 9119에서 실행 중이다. |
| HU-18 | Dashboard UI | 브라우저에서 Kanban plugin 화면 확인 | 보드·카드·토큰 total/input/cache/output/reasoning/coverage가 렌더링되고 JS 오류가 없다. |
| HU-19 | update cleanup | 성공 또는 skip 후 state directory 확인 | pending marker와 lock이 남지 않는다. |
| HU-20 | archived count contract | token usage 회귀 테스트 | 기본 total은 live 카드만, `include_archived=true`에서는 archived 카드도 포함한다. all-time token 합계는 유지한다. |
| HU-21 | observation contract | observation 회귀 테스트 | observation은 worker 실행 옵션과 `reasoning_effort`를 거부하고 dispatcher lifecycle과 격리된다. |
| HU-22 | supported upstream pin | checkout SHA를 pin과 일치/불일치시켜 setup 실행 | 일치하면 진행하고, 불일치 또는 malformed pin이면 어떤 설치 write도 하지 않고 실패한다. |
| HU-23 | runtime update guard | plugin 등록 후 `origin/main`을 다른 SHA로 변경하고 다음 turn 실행 | plugin이 새 카드를 만들지 않고 기록을 중단한다. |
| HU-24 | model-family tokens | Claude Code, Codex, Hermes의 Claude/GPT/unknown model usage를 집계 | 상단에서 Claude/GPT를 분리하고 unknown은 `Other`로 보존하며 `tok` 문자열을 노출하지 않는다. |
| HU-25 | updater pin gate | fetch된 `origin/main`을 지원 pin보다 새 SHA로 설정 | updater가 `hermes update`와 pending marker 생성 전에 실패한다. |
| HU-26 | non-overridable pin | `HERMES_EXPECTED_UPSTREAM_FILE`을 다른 파일로 설정하고 setup 실행 | 환경변수를 무시하고 저장소 pin만 검증한다. pin은 no-follow open과 inode 재검증을 사용한다. |
| HU-27 | installed entrypoint gate | unsupported `origin/main`, stale ref와 변경된 active `HEAD`, 다른 CLI upstream에서 Claude/Codex hook과 adapter 실행 | 두 hook은 카드 기록 없이 exit 0으로 fail-open하고 adapter는 exit 1로 거부한다. repository wrapper와 module CLI 모두 같은 gate를 적용하고 wheel은 console script를 공개하지 않는다. |
| HU-28 | auxiliary Hermes exclusion | async delegation/background/compaction 알림과 delegated child/Kanban worker turn을 재생 | Kanban create 및 turn-state 파일이 생성되지 않고 일반 사용자 prompt는 계속 카드로 기록된다. |

## 2026-08-07 실행 결과

환경: macOS, Hermes Agent v0.20.0, upstream `0957277f2f468bac22bbfcfa7c43029858c9597e`, local carried stack 11개, uv 0.11.11.

| 범위 | 결과 |
| --- | --- |
| Unified Kanban 전체 pytest | PASS — 654 passed, 94.01s |
| Hermes carried-path pytest | PASS — 93 passed, 1 skipped, 6.75s |
| updater pytest | PASS — 21 passed |
| setup dry-run | PASS |
| setup 실제 2회 멱등성 | PASS |
| shell syntax + `git diff --check` | PASS |
| actual CLI smoke | PASS — archived card on `unified-kanban-smoke` |
| bundle verify/list-heads | PASS — 11 refs |
| fresh upstream clone bundle import | PASS |
| updater 재실행 | PASS — `SKIPPED`, Dashboard PID 불변 |
| Gateway/Dashboard | PASS — launchd supervised, Dashboard `127.0.0.1:9119` |
| Dashboard Kanban UI | PASS — token usage와 68개 카드 렌더링, console/JS error 0 |
| update state cleanup | PASS — pending marker와 lock 없음 |
| OpenAI Codex 상태 | PASS — `logged in` |

Hermes 회귀 테스트에서 Starlette TestClient/httpx2 전환 관련 upstream deprecation warning 1건이 발생했다. 기능 실패는 아니며 upstream 추적 대상으로 남긴다.
