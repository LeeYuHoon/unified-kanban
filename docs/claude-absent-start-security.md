# Claude 첫 요청: LIMITED PARTIAL 보안 계약

## 기존 파일과 처음 나타나는 파일의 차이

기존 파일이 UserPromptSubmit 시점에 있으면 이전과 같이 그 inode와 당시 EOF를 기록합니다. Stop에서 같은 세션 문자열을 가진 다른 inode로 교체해도 거부합니다. 이번 변경은 이 연속성 검사를 완화하지 않습니다.

파일이 아직 없으면 **trusted-local-writer first-open** 계약만 적용합니다. 파일 부재부터 최초 안전한 open 사이에 동일 UID가 정상 작성자와 관찰상 구별할 수 없는 파일을 넣는 공격은 이 제한된 계약의 보호 범위 밖입니다. 부재 증거는 미래 inode의 소유권이나 작성자 인증이 아닙니다. 처음 열린 이후에는 실제 inode, 소유자, 단일 hardlink, private mode 및 선택 구간 digest를 검증합니다.

## 수집 허용 조건

- 활성화 이후 생성된 새 관찰 카드의 kernel 서명 receipt, board/task membership, 정책 version/generation을 확인합니다. 기존 카드나 binding을 복구·보충하지 않습니다.
- UserPromptSubmit의 실제 `session_id`와 UUID 형식의 `prompt_id`를 사용합니다. ID 생성, 제목/본문 유사도 매칭, 전체 세션 검색으로 경계를 추측하지 않습니다. 서로 다른 prompt ID는 같은 요청 문구여도 별도 카드 생성 키를 사용합니다.
- 설정된 root 및 이미 존재하는 전체 조상을 no-follow FD로 열고 identity를 기록합니다. 정확한 후보 파일 또는 아직 없는 하위 디렉터리에 대한 ENOENT를 확인합니다. 경로, 조상 identity, prompt, 정책, receipt nonce는 private hook state의 MAC으로 묶습니다.
- 나중에 Stop/SessionEnd가 오면 같은 prompt ID, receipt와 정책 및 기존 조상을 다시 검증합니다. symlink, 신뢰할 수 없는 소유자/권한, hardlink와 최초 pin 이후 교체는 거부합니다.
- bounded snapshot의 최초 공개 user 레코드 `promptId`가 hook `prompt_id`와 정확히 같아야 합니다. `sessionId`, `uuid`, `parentUuid` 연결을 검증하며 다른 prompt나 중복 경계는 채택하지 않습니다. 다음 사용자 prompt가 시작되면 그 앞에서 구간을 끝냅니다. 첫 prompt 앞에 다른 대화가 있으면 전체를 거부합니다.
- byte/line/record/deadline 한도를 적용합니다. 비동기 쓰기의 미완성 마지막 줄은 제외합니다. 공개 user/final만 기존 projector 규칙으로 노출하고 thinking, 도구 인자/결과 등은 그대로 제외합니다.
- 승인된 snapshot을 늦은 쓰기나 반복 Stop으로 재수집하지 않습니다. 부재, 잘못된 prompt, 불명확한 자료는 unavailable로 남기되 정상 카드 완료와 분리합니다. 과거 prompt의 늦은 Stop은 현재 카드도 완료시키지 않습니다.

## 왜 항상 partial인가

[공식 hook 문서](https://code.claude.com/docs/en/hooks#common-input-fields)는 transcript가 **비동기**로 기록되어 hook 시점의 최신 메시지가 아직 없을 수 있다고 명시합니다. Stop은 파일 flush 증거가 아닙니다. 따라서 Claude snapshot은 최종 답변이 포함되어도 공개 응답에서 항상 `partial`입니다. 마지막 답변이 아직 없으면 그것을 만들어 내거나 나중에 backfill하지 않습니다. 파일에 실제로 존재하는 첫 요청/최종 답변의 fixture projection은 검증하지만, 인증/모델 실행을 통한 실사용 테스트를 대신한다고 주장하지 않습니다.

`UserPromptSubmit.prompt`와 `Stop.last_assistant_message`도 공식 입력이지만, 현재 source 계약은 실제 파일 identity/range/digest에 묶여 있습니다. 별도 본문 저장소를 만들거나 hook payload를 가짜 transcript로 합성하지 않았습니다.

## 확인한 지원 자료

- Claude Code **2.1.268**. 공식 문서는 hook `prompt_id`를 v2.1.196부터 지원하는 현재 prompt UUID로 설명합니다.
- 공식 npm `@anthropic-ai/claude-code-darwin-arm64@2.1.268` 배포물의 내장 JavaScript를 정적으로 확인했습니다. hook `prompt_id:mne()??void 0`와 transcript user 작성의 `promptId:Te.type==="user"?mne()??void 0:void 0`가 같은 현재 prompt를 참조하며, user message의 명시적 `promptId`가 유지됩니다. `uuid`와 `parentUuid`도 실제 작성 필드입니다. 추측한 JSON 키가 아닙니다.
- 다운로드: `https://registry.npmjs.org/@anthropic-ai/claude-code-darwin-arm64/-/claude-code-darwin-arm64-2.1.268.tgz`
- tgz SHA-256: `318b90751b61d09a20e92ddf7f57172ea375ca0bc5dfecd3e65bfa6e3babedc9`
- `package/claude` SHA-256: `06a96d5423f83770f120859f1c58e60d7252cc4c122aa13043b7e7cd716bc76a`

새 버전에서 이 필드나 연결 관계가 바뀌면 검증하지 않은 대응 키를 추가하지 말고 unavailable을 유지한 뒤 새 배포물과 fixture를 검토해야 합니다. 이번 검증은 공개 문서와 배포물의 정적 확인이며 CLI 실행, 인증, 개인 기록 접근, 모델 추론은 하지 않았습니다.
