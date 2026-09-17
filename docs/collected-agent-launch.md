# 새 네이티브 수집 세션 실행

기존 수집 설정과 훅 설치가 완료된 일반 터미널에서 명시적으로 실행한다.
`PATH`에는 신뢰하는 정식 `claude` 또는 `codex` 실행 파일이 있어야 한다.

```bash
UNIFIED_KANBAN_CONVERSATION_CONFIG=/absolute/path/to/existing-config.json \
  ./scripts/launch-collected-agent.sh codex
# Claude도 같은 방식으로 선택한다.
UNIFIED_KANBAN_CONVERSATION_CONFIG=/absolute/path/to/existing-config.json \
  ./scripts/launch-collected-agent.sh claude
```

설정 파일을 생성하거나 수정하지 않는다. 선택자 이후 인자는 그대로 전달한다.
`HOME`, `CODEX_HOME`, `HERMES_*`와 기존 환경을 보존하고 자격 증명을 복사하지 않는다.
`HERMES_DELEGATED_CHILD_CONTEXT` 또는 `HERMES_KANBAN_TASK`가 설정되어 있으면
빈 값이어도 거절한다. 가드를 삭제해서 우회하지 않는다.

실행기 프로세스에서만 `umask 077`을 적용한 뒤 PATH의 네이티브 실행 파일로
`exec`한다. 일반적인 생성 요청의 새 파일은 `0600`, 새 디렉터리는 `0700`이 된다.
부모 셸의 umask, 전역 프로필, Orca, 네이티브 바이너리는 변경하지 않는다.
`source`하지 말고 실행한다. 네이티브가 나중에 권한을 직접 변경하는 경우까지 보장하지 않는다.

**새 세션 전용 예방책이다.** 기존 `0644` 파일을 재개해도 권한을 복구하지 않으며
기존 엄격한 수집 검사는 계속 거절해야 한다. 기존 디렉터리도 변경하지 않는다.
설정의 존재 여부는 수집 성공의 증명이 아니며, 실제 내용·권한·출처 검증은 기존 수집 경로가 담당한다.
