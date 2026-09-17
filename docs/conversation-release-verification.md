# 대화 수집 릴리스 후보 검증

> 이 문서는 Hermes 0.21.1과 carried commit `7b6c1856d60384116d2a3d496586989308bc463d` 후보를 검증하던 당시의 기록입니다. 아래 pin·bundle·실행 결과는 현재 후보의 통과 또는 운영 적용 증거가 아닙니다. 현재 버전과 commit은 [README](../README.md#포함된-hermes-버전), 수집 보안 계약은 [Claude·Codex 수집 계약](claude-absent-start-security.md)을 확인하세요. 현재 후보의 최종 검증은 해당 후보와 같은 소스 목록에 연결된 별도 결과로 판단합니다.

## 당시 고정한 배포 입력

- Hermes Agent 버전: `0.21.1`.
- 공식 기반: `f03ed94a34f47ebca57e4a1b0a890bc2aeb5e140`.
- 최종 carried commit: `7b6c1856d60384116d2a3d496586989308bc463d`.
- bundle SHA-256: `d7a11dbe640867887eda9c80c5245ecf8852eafa6c8ea361a8c8bec3aaf1252c`.
- bundle 크기: 276926 bytes. 순서가 있는 carried ref: 39개.
- 당시 검증에서는 위 pin을 이동하지 않았고, upstream 최신판 조회·갱신은 별도 작업으로 남겼다.

이번 native commit은 외부 binding 부재 시 GET의 HTTP 503 처리와 해당 API 회귀 테스트 두 파일만 포함한다. bundle은 별도의 Git 제어 저장소에서 생성하며 공유 source 저장소의 carried ref는 바꾸지 않는다.

## 수집 범위와 실제 관찰

이 절의 카드 ID와 관찰 결과는 당시 후보의 이력이며 현재 네이티브 final 경로의 완료 증거가 아닙니다. 현재 보안·운영 계약은 [Claude·Codex 수집 계약](claude-absent-start-security.md)을 따릅니다. 현재 후보는 요청만 있는 binding을 먼저 발행하지 않고 final 준비까지 유한하게 기다립니다. 아래의 Claude 요청만 있는 부분 기록을 새 동작의 기대 결과나 실사용 재검증으로 해석하지 않습니다.

새로운 대화 구간만 수집하며 과거 기록은 소급 수집하지 않는다. Claude 시작 시 transcript가 없으면 검증된 이후 구간만 부분 기록으로 표시한다. 카드의 최종 결과와 대화의 최종 assistant 응답은 별개의 기록이다. 대화 응답이 없을 때 카드 결과로 채우지 않는다.

기존 실제 실행·브라우저 확인은 작업 소유자가 제공한 증거다. 이번 검증에서 새로운 에이전트를 실행하거나 운영 기록을 조회하지 않는다.

- Claude `t_f2ad356d`: 완료된 카드의 실제 결과가 있고, 수집된 대화는 user 1개·최종 응답 0개인 부분 기록이다. malformed는 0개다.
- Codex `t_a76f4bcd`: 공개 이벤트 2개, malformed 0개이며 브라우저 확인이 끝났다.
- 외부 binding이 아직 없으면 비공개 캐시 금지 헤더와 HTTP 503으로 응답한다. binding이 생기면 재조회한다.

## 검증 경계

실제 릴리스 준비는 격리된 `.venv/conversation-e2e-release-fixture/agent_repo-final-503.releases` namespace에서 지원 생산자 `prepare_release`로 수행한다. npm `11.17.0`을 사용해 실제 의존성 설치·frontend build·bytecode와 completion receipt 봉인을 수행한다. 운영 selector 활성화, 설정 변경, launchd 조작은 이 단계에 포함하지 않는다.

전체 Unified 테스트는 수집된 node ID를 JSON으로 고정하고 파일별로 실행한다. 각 batch의 수집 목록과 setup/call/teardown 결과를 보존하며, 최종 집계에서 중복·누락·예상 밖 항목·실패·건너뛰기를 확인한다. 테스트 수집 성공만으로 통과를 선언하지 않는다.

`tests/test_hermes_release_integration.py`의 `UNIFIED_KANBAN_TEST_HERMES_SOURCE`는 검토된 upstream 객체를 보유한 **공유 Git 객체 저장소**를 가리킨다. linked worktree의 Git 관리 디렉터리와 혼동하지 않는다. 반대로 `tests/test_conversation_owner_cli.py`의 실제 native receipt 테스트는 `hermes_cli/kanban_conversation_receipt.py`가 존재하는 **runtime 소스 worktree**를 요구한다. 두 batch에 다른 경로를 전달한다.

Hermes 전체 테스트 모음을 통과했다고 주장하지 않는다. native 검증 범위는 `tests/plugins/test_kanban*.py`, `tests/hermes_cli/test_kanban*.py`, `tests/hermes_cli/test_conversation_journal.py`다. Unified 전체 테스트와 함께 별도로 실행한다.

CI와 동일하게 `scripts/list-shell-sources.py`의 전체 목록을 순회하여 각 interpreter의 `-n`을 실행하고 `compileall -q src scripts integrations tests` 및 실제 bundle 검증기를 실행한다.

## 로컬 검토 자료와 최종 승인

비공개 실행 로그와 JSON은 무시되는 `.venv/conversation-final-candidate/`에 보관한다. 핵심 파일은 `source.json`, `bundle-verify.txt`, `progress.json`, `unified-summary.json`, `native.json`, `shell-results.json`, `candidate-manifest.json`이다. 장시간 검증이 진행 중이면 결과는 미완료이며 이 문서는 통과 증명서가 아니다.

최종 snapshot은 기존 사용자 index를 바꾸지 않는 임시 index에 모든 추적 파일과 무시되지 않은 새 파일을 담는다. tree ID, base 대비 binary diff SHA-256, 파일별 SHA-256을 검토 자료에 남긴다. 독립적인 최종 artifact 검토 전에는 Unified commit·push·merge 또는 운영 활성화를 하지 않는다.
