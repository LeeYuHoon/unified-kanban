# 관찰 카드의 대시보드 동작

관찰 카드는 외부 CLI 작업의 기록이다. 대시보드에서 외부 프로세스를 시작·중지·재배정하는 작업 카드가 아니다. 다만 모든 수정을 금지하는 읽기 전용 카드도 아니다. 아래 표는 frozen `f03ed94a34f47ebca57e4a1b0a890bc2aeb5e140` 위 carried 서버의 실제 보호 조건을 따른다.

## 허용 행위 결정표

| 동작 | 관찰 카드 | 서버 근거 |
| --- | --- | --- |
| 상세·결과·사용량·댓글·첨부 조회 | 허용 | 기존 조회 API |
| 댓글, 제목·설명·우선순위, 첨부와 알림 관리 | 기존 일반 제약 내 허용 | `plugin_api.py` PATCH 일반 필드와 댓글·첨부 경로 |
| 기록 완료·보관·삭제 | 기존 일반 제약 내 허용 | `kanban_db.complete_task`, `archive_task`, `delete_task`는 관찰 기록도 처리 |
| 담당자 지정·변경·해제 | 금지 | `assign_task`, `reassign_task`의 observation 보호 |
| 모델·provider·reasoning override | 금지 | `_set_task_override`의 observation 보호 |
| claim·reclaim·재실행·block·unblock·review·worker 상태 이동 | 금지 | 해당 DB 함수의 observation 보호와 `plugin_api._set_status_direct` |
| 마우스·터치 드래그 | 금지 | 실행 상태 이동을 뜻하는 UI 제스처 차단. 관찰 카드가 포함된 혼합 선택 드롭도 거부 |
| 관찰 카드에 부모 추가 | 금지 | `link_tasks`는 child가 observation이면 거부 |
| 일반 작업이 관찰 카드를 부모로 참조 | 허용 | 같은 함수는 observation parent를 금지하지 않음 |
| 기존 의존성 연결 제거 | 허용 | `unlink_tasks`의 기존 계약 유지 |
| 진단의 조회·댓글·CLI 명령 복사 | 허용 | 외부 실행 제어 요청이 아님 |
| 진단의 재배정·reclaim·unblock, specify·decompose | 금지 | worker 실행 경로를 UI에서 노출하지 않음 |

완료·보관은 외부 실행의 성공이나 중지를 증명하지 않는다. 댓글 작성도 외부 작업을 재실행·중지하지 않는다. 서버 보호는 UI와 별개로 그대로 유지한다.

## 구현 범위

- 실제 legacy 대시보드는 `plugins/kanban/dashboard/dist/index.js`를 직접 실행하는 IIFE다. 이 파일에는 별도 번들 빌드가 없다. CSS 재설계는 하지 않았다.
- 상세의 worker 상태 버튼을 숨기고 담당자·모델을 읽기 표시로 바꿨다. 제목·설명·우선순위 편집은 남겼다.
- 혼합 선택의 worker 이동·배정·reclaim을 비활성화하고 완료·보관·삭제는 남겼다. 드래그 콜백에서도 관찰 카드를 거부한다.
- 의존성 선택은 방향별로 제한한다. 모든 의존성 편집을 막지 않는다.
- 진단 복구 버튼과 specify·decompose 경로도 같은 기준으로 제한했다.
- current React는 이미 카드 드래그·위조 drop·상태 context menu를 제한하고 있었다. 그 구현은 유지하고 누락된 bulk 배정, 상세 배정·모델·reasoning·Note & requeue·진단 reclaim을 막았다. 관찰 기록의 완료 진입점을 남기고 보관·삭제·댓글·설명·의존성 조회를 유지했다.

## 재현 테스트

`plugins/kanban/dashboard/observation.browser.js`는 실제 DOM과 React 이벤트를 검사한다. `observation-runner.mjs`는 Playwright의 Chrome channel을 사용하며, 명시적으로 준비된 격리 서버·카드·인증 상태 파일을 인자로 받는다.

```bash
node plugins/kanban/dashboard/observation-runner.mjs \
  http://127.0.0.1:19129/kanban \
  t_d748bd83 t_8c7aea79 \
  /tmp/kanban-observation-browser-state.json /tmp/kanban-legacy-demo-fixed.png
```

인증 상태 파일은 시연 origin에서만 복사한 임시 파일이며 저장소에 포함하지 않는다. 다른 환경에서는 그 환경의 격리 카드 ID와 인증 파일을 사용한다.

회귀는 관찰 제어 금지와 일반 카드 기능 유지, 상세·댓글 조회, 혼합 선택·드롭, 모델, 의존성 후보, 진단 복구 동작을 검사한다. 위험한 혼합 드롭의 HTTP 쓰기는 테스트 중 가로채 요청 발생 자체를 검증한다. 진단은 응답의 화면 전용 fixture를 주입한다. 이 두 검증을 실제 서버가 해당 쓰기를 실행한 증거로 해석하지 않는다. 별도 실제 UI 검증에서는 시연 댓글을 전송하고 설명을 편집했으며 일반 카드 ready→triage→ready 이동의 HTTP 200 및 서버 readback을 확인했다.

React는 `apps/desktop/src/plugins/kanban/observation.test.tsx`의 DOM 회귀와 기존 Kanban suite로 검증한다. 문자열 검사만으로 완료 판정하지 않는다. 실행 로그·정확한 release SHA·bundle 검증은 [격리 호환성 검증 기록](hermes-f03ed94a-verification.md)의 후속 절을 참조한다.
