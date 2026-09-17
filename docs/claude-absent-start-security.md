# Claude·Codex 네이티브 대화 수집: 보안·운영 계약

현재 후보 구현을 소스와 격리 fixture 테스트로 대조한 문서입니다. 실제 CLI 모델 실행, 운영 설치본, 인증된 HTTP·브라우저 drawer와 새로고침까지의 실사용 검증은 하지 않았습니다. 아래 수집 준비 상태는 카드의 Ready/Running/Done 상태와 다른 개념입니다.

## 기존 파일과 처음 나타나는 파일의 차이

네이티브 `prompt_id`가 없는 Claude legacy 경로와 공통 EOF 봉인 자체는 기존 inode/EOF 계약을 유지합니다. 네이티브 ID가 있는 Claude 기존 파일은 별도 `claude-file-provenance-v2`, Codex 네이티브 파일은 `codex-file-provenance-v1` 준비/봉인 경로를 사용합니다. Codex를 Claude의 첫 파일 허용 계약과 혼동하지 않습니다. Stop에서 같은 세션 문자열을 가진 다른 inode로 교체해도 거부하며 공통 봉인의 `start_offset == captured size` 검사를 완화하지 않습니다.

Claude 파일이 아직 없으면 **trusted-local-writer first-open** 계약만 적용합니다. 파일 부재부터 최초 안전한 open 사이에 동일 UID가 정상 작성자와 관찰상 구별할 수 없는 파일을 넣는 공격은 이 제한된 계약의 보호 범위 밖입니다. 부재 증거는 미래 inode의 소유권이나 작성자 인증이 아닙니다. 처음 열린 이후에는 실제 inode, 소유자, 단일 hardlink, private mode 및 선택 구간 digest를 검증합니다.

## Claude absent 시작의 수집 허용 조건

- 활성화 이후 생성된 새 관찰 카드의 kernel 서명 receipt, board/task membership, 정책 version/generation을 확인합니다. 과거 카드에 새 수집 권한을 소급 부여하지 않습니다. 같은 카드의 인증된 pending 작업 재개와 이미 발행된 동일 binding의 일치 확인만 허용하며, 기존 binding을 보충·확장하지 않습니다.
- UserPromptSubmit의 실제 `session_id`와 UUID 형식의 `prompt_id`를 사용합니다. ID 생성, 제목/본문 유사도 매칭, 전체 세션 검색으로 경계를 추측하지 않습니다. 서로 다른 prompt ID는 같은 요청 문구여도 별도 카드 생성 키를 사용합니다.
- 설정된 root 및 이미 존재하는 전체 조상을 no-follow FD로 열고 identity를 기록합니다. 정확한 후보 파일 또는 아직 없는 하위 디렉터리에 대한 ENOENT를 확인합니다. 경로, 조상 identity, prompt, 정책, receipt nonce는 private hook state의 MAC으로 묶습니다.
- Stop/SessionEnd 또는 다음 prompt에서 이전 요청을 정리할 때, 이전 state의 prompt ID·receipt·정책·조상을 다시 검증합니다. worker도 같은 검증을 수행합니다. 새 요청의 ID나 파일 경로를 이전 요청의 권한으로 쓰지 않습니다. symlink, 신뢰할 수 없는 소유자/권한, hardlink와 최초 pin 이후 교체는 거부합니다.
- bounded snapshot의 최초 공개 user 레코드 `promptId`가 hook `prompt_id`와 정확히 같아야 합니다. `sessionId`, `uuid`, `parentUuid` 연결을 검증하며 다른 prompt나 중복 경계는 채택하지 않습니다. 다음 사용자 prompt가 시작되면 그 앞에서 구간을 끝냅니다. 첫 prompt 앞에 다른 대화가 있으면 전체를 거부합니다.
- byte/line/record/deadline 한도를 적용합니다. 비동기 쓰기의 미완성 마지막 줄은 제외합니다. 공개 user/final만 기존 projector 규칙으로 노출하고 thinking, 도구 인자/결과 등은 그대로 제외합니다.
- 발행된 snapshot을 늦은 쓰기나 반복 Stop으로 재수집하지 않습니다. final 전에는 아래 pending 작업으로 최초 발행을 기다립니다. 검증 실패는 단순 final 지연과 다르며 카드 완료와 다음 관찰 카드 생성을 막을 수 있습니다. 과거 prompt의 늦은 Stop은 현재 카드도 완료시키지 않습니다.

## 기존 파일: file-provenance-v2의 좁은 추가 계약

- Claude 2.1.268의 실제 `promptId`만 사용합니다. UserPromptSubmit 전에 이미 기록된 요청은 bounded snapshot에서 정확한 ID로 찾으며, 요청의 원본 byte offset과 `uuid`를 인증 준비에 고정합니다. 이전 요청과 본문이 같아도 ID가 다르면 다른 턴입니다.
- 준비 MAC은 별도 버전 도메인으로 board/task, receipt nonce/생성 시각, session/prompt ID, 정책 version/generation, 원본 경로, 전체 조상 dev/ino, 파일 identity, captured EOF, 전체 captured prefix digest, 요청 offset/uuid를 묶습니다. 본문은 준비 파일에 저장하지 않습니다.
- 요청이 아직 없으면 같은 inode의 captured EOF를 인증된 하한으로 사용합니다. 불완전한 줄 한가운데에서는 대기 준비를 만들지 않습니다. 나중에 정확한 요청이 하한 이후에 나타나야 하며 prefix가 단 한 바이트라도 바뀌면 거부합니다.
- 이전 턴은 `uuid`/`parentUuid` 검증에만 사용합니다. 선택 요청의 parent는 앞에서 확인한 메시지 또는 null이어야 하고, 선택 이후 assistant/tool-result의 parent는 현재 요청에 연결된 메시지여야 합니다. 이전 대화와 다음 사용자 요청은 projection에 포함하지 않습니다.
- 열린 FD를 유지한 채 원본 파싱, 재해시, fstat, 전체 조상 및 정규 경로 재열기를 확인합니다. inode 교체, 같은 inode의 prefix 수정, 조상 교체, MAC/정책/영수증 변경은 unavailable입니다. mode 삭제만으로 legacy 봉인으로 내려가지 않습니다.
- 별도 schema pin과 binding version 2를 발급하되 기존 binding의 generation/range를 갱신하지 않습니다. 공통 legacy EOF 봉인을 완화하지 않으며, absent 시작의 final 대기는 별도 최초 open pin과 봉인 경로를 사용합니다.

이 경로 역시 **LIMITED PARTIAL**입니다. 전체 파일의 byte/record/deadline 한도를 넘는 세션, 이전 요청에 네이티브 ID가 없는 혼합 버전 기록, 끊어진 parent 그래프, 비공개/모호한 대화 레코드는 추측 없이 거부합니다. 신뢰하는 로컬 Claude 작성자를 전제로 하며 MAC은 준비 증거를 인증하는 것이지 생산자 자체의 암호학적 서명은 아닙니다. 기존 private hook state 및 키 저장소를 완전히 장악한 동일 UID 공격자를 격리하는 프로토콜은 아닙니다.

## Codex 네이티브 파일: 별도의 세션·턴 계약

- 준비와 매 seal/reconcile의 동일한 bounded snapshot에서 `session_meta.payload.id`가 기대 세션과 정확히 같고 유일한지 확인합니다. metadata 부재·중복·충돌은 거부합니다. 목표 `task_complete` 뒤에 붙은 metadata도 검사하므로, caller의 세션 문자열을 MAC에 넣거나 목표 turn ID만 맞추는 것으로 원본 세션을 인증하지 않습니다.
- `event_msg`의 정확한 `task_started`/`task_complete`와 `turn_id`, 연속적인 원본 ordinal을 검증합니다. 공개 `response_item`의 `internal_chat_message_metadata_passthrough.turn_id`도 목표와 일치해야 합니다. 공개 user와 `phase=final_answer`인 assistant가 모두 있고 terminal이 확인되어야 하며, 최종 projector의 공개 요청/final 검증도 통과해야 발행합니다. Stop payload 자체는 terminal 증거가 아닙니다.
- 준비 MAC은 board/task·receipt·정책·session/turn·원본 경로/조상/identity·captured EOF/prefix digest·선택 시작 offset/ordinal을 묶습니다. 같은 inode라도 prefix가 바뀌면 거부합니다. 이미 기록된 정확한 목표 턴은 선택할 수 있지만 본문 유사도나 다른 세션에서 턴을 추측하지 않습니다.
- 기존 binding과의 reconcile에서도 세션 metadata, 권한, 원본, 선택 범위와 digest를 다시 검증합니다. 동일 binding의 확인일 뿐 range/generation 확장이나 다른 원본 채택이 아닙니다. 이 계약을 legacy Codex 파일 전체의 지원 또는 Claude의 항상 `partial` 규칙과 동일하다고 설명하지 않습니다.

## ready / pending / rejected와 카드 수명주기

아래는 인증된 준비가 있는 네이티브 A 요청의 Stop/SessionEnd 또는 다음 B prompt 처리 기준입니다. 수집이 처음부터 비활성화되었거나 준비를 얻지 못한 카드의 일반 완료 경로와 구별합니다.

| 수집 판정 | 공개 binding | A 상태와 B 관찰 카드에 대한 효과 |
| --- | --- | --- |
| `ready` | 검증된 공개 요청과 final을 함께 최초 발행하거나 이미 발행된 정확히 동일한 binding을 확인 | A 완료를 진행하고, 다음 prompt라면 그 뒤 B 관찰 카드 생성을 진행 |
| `pending` | 요청만 있는 binding을 발행하지 않음 | 인증 작업을 내구 저장하고 별도 worker 프로세스 생성이 성공하면 A 완료 및 B 관찰 카드 생성을 진행할 수 있음. worker 내부 성공까지 기다리는 것은 아님 |
| `rejected` / `expired`, 인증·보존·worker 시작 실패 | 새 binding 발행 없음. 기존 binding이 있더라도 변경·삭제하지 않음 | A lifecycle state를 보존하고 이 경로에서 A를 완료하지 않으며 B 관찰 카드 생성도 하지 않음 |

Claude final 준비는 선택 구간에서 공개 assistant 텍스트와 `stop_reason=end_turn` 또는 `stop_sequence`를 확인합니다. 요청만 있거나 final이 아직 없으면 `pending`입니다. 잘못된 ID, MAC/receipt/정책/원본 검증 실패를 단순 지연으로 바꾸지 않습니다. 이 request-only 발행 금지는 위 네이티브 final 경로에 대한 것이며, 기존 legacy 봉인의 동작을 새 보장으로 소급 해석하지 않습니다.

Stop이 빠진 A 뒤에 B가 오면 **A의 저장된 권한으로 먼저 보존·검증**합니다. A의 binding이 즉시 ready라면 `get_hook_final`로 인증된 공개 final을 읽어 A의 `tasks.result`로 전달할 수 있습니다. 이전 완료 시도가 실패해 fallback이 state에 남았어도 확인된 A final을 우선합니다. A 완료가 성공한 뒤에야 B 생성을 진행하며 B의 문구·final을 A 결과로 쓰지 않습니다.

반면 A를 fallback으로 이미 완료한 뒤 worker가 지연된 final을 찾으면 **A의 대화 binding만 최초 발행**합니다. 이미 완료된 A의 `tasks.result`는 fallback 그대로이며 자동으로 고치지 않습니다. 카드 결과 복구와 대화 수집 복구는 같은 보장이 아닙니다.

늦은 A Stop/SessionEnd가 B 진행 중 도착하면 native prompt/turn ID를 대조하여 무시합니다. ID가 누락되거나 다르면 세션만으로 현재 카드를 완료하지 않습니다. A의 기존 pending 작업은 B의 state와 독립적으로 재시도할 수 있습니다.

위 거부는 **관찰 수집의 fail-closed**입니다. 호출한 Claude/Codex CLI의 실제 작업은 **fail-open**으로 계속됩니다. hook의 정상 종료 코드만으로 카드 생성·완료·수집 성공을 판정할 수 없으며, 관찰 카드가 계속 running이거나 B 카드가 없을 수 있습니다.

## final pending worker와 불변성

- Claude absent/file-v2 및 Codex 네이티브 작업은 provider별 private queue에 저장됩니다. 작업에는 인증된 준비·receipt·범위·예산이 들어가며 요청/응답 본문은 저장하지 않습니다. MAC, owner-only 파일/디렉터리, retained FD, task 잠금 및 예상 inode에 대한 CAS로 검증·갱신합니다.
- 최초 enqueue 기준 TTL은 **60초**, 인증된 최대 시도는 **12회**입니다. 중복 Stop, 다음 prompt, worker 재시작이 기한이나 시도 예산을 리셋하지 않습니다. 만료·거부된 작업은 자동으로 새 권한을 받지 않습니다.
- absent 시작은 파일이 아직 없으면 안전한 부재·조상 확인으로 기다립니다. 최초 open 직전에 재사용 금지 상태를 저장하고 성공한 최초 inode/prefix pin만 재시도에 사용합니다. 최초 open/검증 또는 pin 저장이 실패하면 `rejected`로 남기며 다른 파일로 first-open을 다시 시도하지 않습니다.
- worker는 hook과 분리된 유한 프로세스이며 검증된 선택 release/runtime 및 저장소 코드로 실행합니다. 위임 child/task 권한으로는 실행하지 않습니다. 별도 상주 daemon은 없습니다. 중단된 작업은 다음 **prompt hook**이 private queue를 제한된 시간·탐색·실행 예산으로 확인하여 재개할 수 있습니다. 유휴 시간의 자동 재기동이나 모든 queue 항목의 즉시 처리를 보장하지 않습니다.
- binding 발행 뒤 작업 상태 저장에 실패한 경우에는 같은 binding과 현재 검증 결과의 정확한 일치만 reconcile합니다. request-only binding을 먼저 발행한 후 확장하는 방식이 아닙니다. 이미 발행된 range/generation을 바꾸거나 과거 카드에 새 source를 붙이는 복구는 지원하지 않습니다.

## 운영 확인과 지원되는 수동 조치

1. 정확한 board/task를 확인하고 카드 상태·결과와 대화 응답을 별도로 확인합니다. 원문·receipt·키를 진단 로그에 복사하지 않습니다. 단순 비동기 지연이면 원래 예산 안에서 worker의 결과를 기다립니다. 다음 정상 prompt는 재개 계기가 될 수 있지만 이미 거부·만료된 A를 해제하지는 않습니다.
2. 반복 거부가 발생하면 원본/권한/정책 변경과 선택 runtime의 호환성을 조사합니다. 아래 명령은 저장소 루트에서 사용하는 실제 지원 parser 계약입니다. `python3`는 프로젝트 요구 버전인 Python 3.11 이상이어야 하며 placeholder를 운영자가 확인한 값으로 바꿔 사용합니다. 이 문서의 검증에서는 운영 변경 명령을 실행하지 않았습니다.

   ```sh
   # 선택한 board의 추가 수집을 중지하는 owner 정책 명령
   PYTHONPATH=src python3 -m kanban_adapter.conversation_policy_cli --policy-file /ABS/OWNER/policy.json --secret-file /ABS/OWNER/authority.key disable --board BOARD

   # 확인된 카드에 운영 조사 사실을 남기는 명령
   bin/kanban-adapter update --board BOARD --task TASK_ID --message='수집 검증 실패: 운영 확인 필요'

   # 운영자가 완료 여부를 별도로 확인한 카드만 수동 완료
   bin/kanban-adapter done --board BOARD --task TASK_ID --result='수동 완료: 대화 수집은 복구되지 않음' --summary='운영 확인 후 수동 완료'
   ```

3. 정책 `disable`은 generation을 바꾸므로 기존 준비를 복구하지 않습니다. `update`/`done`도 대화 binding이나 hook lifecycle state를 복구·해제하는 명령이 아닙니다. 이 명령을 실행했다고 차단된 B 관찰 생성이 다시 가능해진다고 보장하지 않습니다. 일반 관찰 카드를 `block`하는 것을 복구 절차로 안내하지 않습니다.
4. 현재 지원되는 owner/adapter CLI에는 거부된 pending 작업이나 A lifecycle state를 안전하게 재인증·초기화하는 전용 복구 명령이 없습니다. 따라서 여기서 자동 복구가 불가능하면 상태를 보존하고 구현 검토로 이관합니다. 임의 관리자 API, 가짜 hook payload 재전송, 수동 state/MAC 편집, 파일·카드 삭제를 복구 방법으로 제시하지 않습니다.

## Claude가 항상 partial인 이유

[공식 hook 문서](https://code.claude.com/docs/en/hooks#common-input-fields)는 transcript가 **비동기**로 기록되어 hook 시점의 최신 메시지가 아직 없을 수 있다고 명시합니다. Stop은 파일 flush 증거가 아닙니다. 따라서 Claude snapshot은 최종 답변이 포함되어도 공개 응답에서 항상 `partial`입니다. final 준비 판정과 유한 재시도는 flush 완료 증명이 아닙니다. 마지막 답변이 아직 없으면 본문을 만들어 내지 않고 최초 발행 전에만 기다립니다. 발행 뒤 snapshot을 backfill하지 않습니다. 파일에 실제로 존재하는 첫 요청/최종 답변의 fixture projection은 검증하지만, 인증/모델 실행을 통한 실사용 테스트를 대신한다고 주장하지 않습니다.

`UserPromptSubmit.prompt`와 `Stop.last_assistant_message`도 공식 입력이지만, 현재 source 계약은 실제 파일 identity/range/digest에 묶여 있습니다. 별도 본문 저장소를 만들거나 hook payload를 가짜 transcript로 합성하지 않았습니다.

## 확인한 지원 자료

- Claude Code **2.1.268**. 공식 문서는 hook `prompt_id`를 v2.1.196부터 지원하는 현재 prompt UUID로 설명합니다.
- 공식 npm `@anthropic-ai/claude-code-darwin-arm64@2.1.268` 배포물의 내장 JavaScript를 정적으로 확인했습니다. hook `prompt_id:mne()??void 0`와 transcript user 작성의 `promptId:Te.type==="user"?mne()??void 0:void 0`가 같은 현재 prompt를 참조하며, user message의 명시적 `promptId`가 유지됩니다. `uuid`와 `parentUuid`도 실제 작성 필드입니다. 추측한 JSON 키가 아닙니다.
- 다운로드: `https://registry.npmjs.org/@anthropic-ai/claude-code-darwin-arm64/-/claude-code-darwin-arm64-2.1.268.tgz`
- tgz SHA-256: `318b90751b61d09a20e92ddf7f57172ea375ca0bc5dfecd3e65bfa6e3babedc9`
- `package/claude` SHA-256: `06a96d5423f83770f120859f1c58e60d7252cc4c122aa13043b7e7cd716bc76a`

새 버전에서 이 필드나 연결 관계가 바뀌면 검증하지 않은 대응 키를 추가하지 말고 unavailable을 유지한 뒤 새 배포물과 fixture를 검토해야 합니다. 이번 검증은 공개 문서와 배포물의 정적 확인이며 CLI 실행, 인증, 개인 기록 접근, 모델 추론은 하지 않았습니다.
