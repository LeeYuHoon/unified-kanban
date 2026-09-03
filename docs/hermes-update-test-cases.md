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
| HU-01 | 버전 | `hermes --version` | install directory가 selector가 가리키는 sibling immutable release이며 frozen upstream SHA와 carried commit 수가 표시된다. |
| HU-02 | 인증 | `hermes auth status openai-codex` | credential 값을 노출하지 않고 `logged in`이다. |
| HU-03 | update check | `./scripts/update-hermes-if-needed.sh --check` | 검증된 release가 선택돼 있으면 `UP_TO_DATE`, 아니면 `UPDATE_AVAILABLE`을 보고한다. selector, release, lock, pending marker, 서비스를 전혀 건드리지 않는다. |
| HU-04 | clean skip | 선택된 release가 검증된 release와 같고 pending marker가 없는 상태에서 updater 재실행 | `SKIPPED`. release 준비, bundle 검증, upstream 조회, 서비스 restart가 모두 일어나지 않고 selector inode가 그대로다. no-change 판정이 lock·state directory 생성보다 먼저 이뤄지므로 state directory가 없던 호스트에서는 생성되지 않는다. |
| HU-05 | interrupted activation | selector 교체 후 restart 전에 중단된 실행의 pending marker가 남은 상태 | `RESUMING`으로 서비스 restart만 완료하고 pending marker를 제거한다. `SKIPPED`로 오판하지 않는다. |
| HU-06 | selector integrity | selector를 symlink, 빈 파일, release 이름 형식이 아닌 값으로 교체 | 어떤 활성화도 하지 않고 실패하며 selector를 그대로 보존한다. |
| HU-07 | unavailable frozen object | fixed official HTTPS 저장소에서 frozen pin exact fetch 실패 | release 준비와 selector 교체 전에 중단한다. live `main` 조회 실패 자체는 현재 snapshot authority가 아니다. |
| HU-08 | release reuse gate | 이미 존재하는 release를 재사용 | source tree, Git metadata, dependency inventory, completion receipt를 정확히 검증한 뒤에만 선택한다. 검증 실패 시 이전 selector를 유지한다. |
| HU-09 | bundle integrity | `git bundle verify`, `git bundle list-heads` | bundle이 유효하고 `carried-01`부터 `carried-13`까지 13개 ref가 있다. |
| HU-10 | fresh import | fresh upstream clone에서 bundle fetch | manifest의 13개 commit object를 모두 조회할 수 있다. |
| HU-11 | setup preflight | `./scripts/setup.sh --dry-run --no-restart --skip-smoke` | 파일·설정·서비스를 바꾸지 않고 전체 명령을 출력한다. |
| HU-12 | setup idempotency | 실제 setup을 같은 옵션으로 2회 실행 | 두 번째 실행이 hook, link, plugin 항목을 중복 생성하지 않는다. |
| HU-13 | project tests | `uv sync --frozen --group dev && uv run pytest -o addopts='' -q` | 수집된 전체 suite가 모두 통과한다. 개수 변경은 diff로 설명한다. |
| HU-14 | Hermes regression | checklist의 isolated 5-file pytest 명령 | 현재 기준 114 passed, 1 skipped. |
| HU-15 | shell/static | `bash -n ...` 및 `git diff --check` | syntax 및 whitespace 오류가 없다. |
| HU-16 | CLI smoke | `./scripts/kanban-smoke.sh` | create → comment → complete → archive 왕복 후 `SMOKE PASS`. |
| HU-17 | services | `hermes gateway status`, `hermes dashboard --status` | Gateway가 supervised/running이고 Dashboard가 9119에서 실행 중이다. |
| HU-18 | Dashboard UI | 브라우저에서 Kanban plugin 화면 확인 | 보드·카드·토큰 total/input/cache/output/reasoning/coverage가 렌더링되고 JS 오류가 없다. |
| HU-19 | update cleanup | 성공 또는 skip 후 state directory 확인 | pending marker와 lock이 남지 않는다. |
| HU-20 | archived count contract | token usage 회귀 테스트 | 기본 total은 live 카드만, `include_archived=true`에서는 archived 카드도 포함한다. all-time token 합계는 유지한다. |
| HU-21 | observation contract | observation 회귀 테스트 | observation은 worker 실행 옵션과 `reasoning_effort`를 거부하고 dispatcher lifecycle과 격리된다. |
| HU-22 | supported upstream pin | live `main`을 pin보다 전진시키거나 pin을 malformed로 바꾸고 setup 실행 | moved main에서는 frozen exact fetch로 진행하고, malformed pin 또는 exact object mismatch면 어떤 설치 write도 하지 않고 실패한다. |
| HU-23 | runtime update guard | read-only checkout ref를 변경하거나 selected release receipt/CLI upstream을 mismatch시킨 뒤 다음 turn 실행 | stale checkout ref만으로는 중단하지 않지만 selected release authority mismatch에서는 plugin이 새 카드를 만들지 않는다. |
| HU-24 | model-family tokens | Claude Code, Codex, Hermes의 Claude/GPT/unknown model usage를 집계 | 상단에서 Claude/GPT를 분리하고 unknown은 `Other`로 보존하며 `tok` 문자열을 노출하지 않는다. |
| HU-25 | updater frozen pin gate | official `main`을 지원 pin보다 전진시키고 frozen object는 유지 | updater가 main 이동을 notice로 기록하고 exact frozen release 준비·선택을 계속한다. frozen object mismatch면 write 전에 실패한다. |
| HU-26 | non-overridable pin | `HERMES_EXPECTED_UPSTREAM_FILE`을 다른 파일로 설정하고 setup 실행 | 환경변수를 무시하고 저장소 pin만 검증한다. pin은 no-follow open과 inode 재검증을 사용한다. |
| HU-27 | installed entrypoint gate | 선택되지 않았거나 foreign한 selector, completion receipt가 없거나 다른 디렉터리에 발급된 release, 다른 CLI upstream에서 Claude/Codex hook과 adapter 실행 | 두 hook은 카드 기록 없이 exit 0으로 fail-open하고 adapter는 exit 1로 거부한다. repository wrapper와 module CLI 모두 같은 gate를 적용하고 wheel은 console script를 공개하지 않는다. |
| HU-28 | auxiliary Hermes exclusion | async delegation/background/compaction 알림과 delegated child/Kanban worker turn을 재생 | Kanban create 및 turn-state 파일이 생성되지 않고 일반 사용자 prompt는 계속 카드로 기록된다. |
| HU-29 | read-only checkout | 활성화 전후로 `HERMES_AGENT_REPO` 체크아웃 전체의 파일 목록·내용·inode를 비교 | 완전히 동일하다. updater는 체크아웃에 어떤 write도 하지 않고 `hermes update`, reset, cherry-pick, merge를 실행하지 않는다. |
| HU-30 | launcher provenance | `~/.local/bin/hermes`를 foreign script, symlink, 삭제 상태로 두고 updater 실행 | release 준비 전에 거부하고 selector를 만들지 않으며 `./scripts/setup.sh` 재실행을 안내한다. |
| HU-31 | foreign selector successor | selector 교체 직후 제3자가 selector를 다른 내용으로 바꾸는 상황 | successor를 삭제하거나 덮어쓰지 않고 실패한다. |
| HU-32 | restart compensation | 활성화 후 gateway restart 실패 | 이전 selector로 롤백하고, 서비스를 복원된 release로 되돌리며, pending marker를 남긴다. |
| HU-33 | custom checkout consistency | 사용자 지정 `HERMES_AGENT_REPO`로 setup, updater, launcher, uninstall 실행 | 네 경로 모두 같은 sibling release root(`<HERMES_AGENT_REPO>.releases`)와 selector를 사용하고 기본 경로를 만들지 않는다. |
| HU-34 | denormalized checkout path | `HERMES_AGENT_REPO`를 `/`, `//`, `/./`로 끝나게 두고 setup·updater·uninstall 실행 | shell과 Python이 하나의 normal form을 쓰므로 release root는 checkout 안이 아니라 sibling에 남고, 설치된 launcher가 읽는 selector와 setup이 쓴 selector가 같다. `..`가 포함되면 어떤 write나 release 구성 전에 거부한다. |
| HU-35 | uncheckpointed setup crash | replace-file 성공 직후 checkpoint 전에 setup을 SIGKILL | 다음 setup이 stage operation receipt를 manifest 권한으로 검증해 되돌린 뒤 정상 완료(exit 0)한다. 그 사이 제3자가 해당 leaf를 바꿨다면 leaf 경로와 identity 불일치를 명시하며 거부하고 successor를 보존한다. |
| HU-36 | incomplete release recovery | dependency sync가 실패해 completion receipt 없는 release가 남은 상태에서 재실행 | 선택되지 않았고 구성 이후 변경되지 않은 release만 durable하게 retire한 뒤 재구성해 완료한다. selector가 그 release를 가리키거나 tracked 파일이 변경됐다면 거부하고 보존한다. |
| HU-37 | bytecode sealing | 실제 release를 준비해 short-circuit인 `hermes --version`이 아니라 full entry point인 `hermes kanban --help`를 두 번 실행하고 실행 전후 receipt digest, bytecode inventory, inode를 비교 | 모든 source의 `.pyc`가 stable path의 checked-hash mode로 생성·fsync되고 exact carried HEAD의 `.bytecode-fingerprint`가 receipt 전에 durable하게 기록된다. 첫 full CLI 실행, 두 번째 실행, 재사용이 release tree를 변경하지 않는다. precompile 또는 stamp publication 도중 중단되면 검증된 producer-owned output만 incomplete-release recovery가 retire한다. |

## 2026-08-07 실행 결과

> Historical snapshot only. These values describe the 2026-08-07 candidate and are superseded by
> the repository-owned pin, manifest, bundle metadata, and a fresh test run. Do not use this section
> as publication evidence for a later candidate.

환경: macOS, Hermes Agent v0.20.0, upstream `0957277f2f468bac22bbfcfa7c43029858c9597e`, local carried stack 11개, uv 0.11.11.

| 범위 | 결과 |
| --- | --- |
| Unified Kanban 전체 pytest | PASS — 654 passed, 82.19s |
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
