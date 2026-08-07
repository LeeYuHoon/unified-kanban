# Hermes 업데이트 검증 결과 — 2026-08-07

## 결론

Hermes Agent를 최종 upstream `0957277f2f468bac22bbfcfa7c43029858c9597e`로 업데이트하고 Unified Kanban carried stack 11개를 재적용했다. 자동화 테스트, bundle 복구, setup 멱등성, 실제 Kanban CLI 왕복, Gateway/Dashboard, 브라우저 UI를 검증했으며 모두 통과했다.

## upstream 변경과 호환성 판단

- 최초 비교 기준: `98105f31f`
- 중간 업데이트 target: `9d4ef04ed00055414c13fcf33925d85790221a3f`
- 검증 중 추가로 반영된 최종 upstream: `0957277f2f468bac22bbfcfa7c43029858c9597e`
- 최종 추가 upstream commit은 Polymarket skill을 optional finance catalog로 이동하는 문서·skill 배치 변경이며 Kanban 실행 경로에는 영향이 없었다.
- Hermes checkout 최종 상태: v0.20.0, upstream `0957277f`, local carried commit 11개.

## 적용한 호환성 수정

1. upstream의 `reasoning_effort` 필드와 observation create SQL 충돌을 병합했다.
2. observation 카드가 worker 전용 `reasoning_effort` 옵션을 받지 못하도록 invariant와 회귀 테스트를 추가했다.
3. archived 카드 표시 계약을 보존했다. 기본 보드 total은 live 카드만 집계하고, `include_archived=true`에서는 archived 카드도 total에 포함한다. 보드 token 합계는 all-time 의미를 유지한다.
4. updater가 최신 upstream에서도 오래된 applied-state SHA를 감지하면 setup과 서비스 복구를 수행하도록 했다.
5. applied-state의 CRLF 종료를 정상화하고, `origin/main`을 해석할 수 없으면 잘못된 repair를 실행하지 않고 fail-closed하도록 했다.
6. carried manifest와 bundle을 11개 commit으로 갱신했다. bundle은 supported upstream `0957277f`를 prerequisite로 하는 thin bundle이며 크기는 35,111 bytes, SHA-256은 `376683f2e2572633f4bc6f6548a8b2cc064e70eb8b04d904d0613882887f7cd3`다. `scripts/verify-carried-bundle.py`가 metadata, prerequisite, ordered unique refs, regular non-symlink inputs와 pack checksum을 검증하고, CI에서는 isolated Git clone에 실제 unbundle까지 수행한다.
7. `patches/hermes-agent-supported-upstream`에 검증된 full upstream SHA를 고정했다. pin은 환경변수로 대체할 수 없고 no-follow descriptor와 inode 재검증으로 읽는다. setup은 CLI, checkout, pin이 모두 일치할 때만 설치 write를 진행하고 updater는 unsupported `origin/main`에서 `hermes update` 전에 중단한다. 설치된 Hermes plugin, Claude/Codex hook, adapter도 runtime mismatch를 차단한다.

8. Hermes 자동 delegation/background/compaction/todo 알림과 delegated child/Kanban worker turn은 보조 내부 실행으로 분류해 별도 Kanban observation을 생성하지 않도록 했다.

## carried stack

manifest 순서:

1. `f64bb1a9630f87f7a5e3e89ac4f6e25282f5b352` — selected-board deletion
2. `bb95882dd8c093310cd6592b40866e8d730e42df` — observation cards + reasoning invariant reconciliation
3. `6f6e90aebda2ee841d1e40779d3e0f185eeec898` — observation lifecycle isolation
4. `a8dadf2d05159d48acad21f6f805c1d3908f3db7` — observation invariant hardening
5. `a9145de8df20cff24add16db8261b04b0dbac12d` — token usage UI
6. `c44300a0f524fe7549d8d810a007d2d1c70138d6` — token schema v2
7. `cc9b8759a38498b494d5a25c74059fb32b06df4b` — token rollup hardening
8. `3b2913dd268370dc01df40f4ab7bc0ef677bd2c0` — full task results
9. `84da7f3ee780ea2ec29749df1ea006e8fc0d3200` — observation summary fallback
10. `abc0fa4ece406864ba760e88da77a6552bfcbd5a` — archived board count contract
11. `a98adc0305020cebd9209a7182e09bf74e5e6759` — model-family token totals, full token labels, and locale-aware compact formatting

최종 upstream 위에는 위 patch와 동등한 새 local commit 11개가 적용됐다. updater는 manifest SHA 자체가 아니라 `git cherry` patch-equivalence로 적용 여부를 판단한다.

## 검증 결과

- Unified Kanban: 654 passed in 94.01s
- Hermes carried paths: 93 passed, 1 skipped in 6.75s
- updater suite: 21 passed
- setup compatibility suites: PASS, including exact pin match and mismatch/malformed/missing/symlink pin refusal
- bundle verify/list-heads: 11 refs, PASS
- fresh upstream clone bundle import: PASS
- setup dry-run: PASS
- setup 실제 2회: PASS, 중복 생성 없음
- Bash syntax 및 `git diff --check`: PASS
- actual Kanban smoke: PASS
- Gateway: launchd supervised/running
- Dashboard: `127.0.0.1:9119` running
- 실제 Kanban UI: 보드, 카드, token usage, coverage 렌더링 확인; console/JS error 0
- 최종 updater 재실행: `SKIPPED`; Dashboard PID 불변
- pending marker 및 update lock: 없음

## 모델별 수행 내역

| 모델 | 작업 | 결과 |
| --- | --- | --- |
| `gpt-5.6-sol` | 주 구현, 충돌 해결, 테스트, bundle/setup/runtime/UI 검증 | 성공 |
| `claude-opus-5` | 전체 upstream 호환성 조사 | 모델 실행은 성공했으나 12-turn 제한 전에 최종 보고를 만들지 못해 결과로 채택하지 않음 |
| `claude-fable-5` | 압축 diff 기반 독립 리뷰 | 성공. CRLF state와 unreadable `origin/main` fail-closed 문제를 발견했고 둘 다 수정·테스트함 |
| `gpt-5.6-sol-pro` | 독립 코드 리뷰 시도 | 실패. ChatGPT 계정의 OpenAI Codex provider에서 해당 모델이 지원되지 않음 |

`fable5` 별칭은 404였고 canonical model ID `claude-fable-5`로 다시 실행했다. 첫 Fable 전체 저장소 리뷰는 600초 timeout이었으며, 입력을 diff로 제한한 1-turn 리뷰만 검증 근거로 사용했다.

## OpenAI Codex 인증

- `hermes auth add openai-codex` device-code flow를 실행했으나 사용자 승인이 완료되지 않아 15분 후 timeout됐다.
- 기존 OpenAI Codex credential은 계속 유효하며 `hermes auth status openai-codex` 결과는 `logged in`이다.
- credential 값과 device code는 저장하거나 문서화하지 않았다.

## 알려진 비차단 사항

- Hermes isolated regression suite에서 Starlette TestClient/httpx2 관련 upstream deprecation warning 1건이 발생한다.
- `gpt-5.6-sol-pro`는 현재 ChatGPT Codex 계정에서 사용할 수 없다.
- Windows/WSL2/Linux 지원은 검증하지 않았으며 지원 대상으로 표기하지 않는다.