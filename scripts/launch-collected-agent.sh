#!/bin/bash
# 이 프로세스의 새 파일에만 제한적인 기본 권한을 적용한다.
# source로 호출하면 부모 셸을 변경하므로 실행만 허용한다.
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  printf '%s\n' 'Execute this launcher; do not source it.' >&2
  return 2
fi
set -euo pipefail
case "${1-}" in
  claude|codex) agent="$1" ;;
  *) printf '%s\n' 'Usage: launch-collected-agent.sh claude|codex [native args...]' >&2; exit 2 ;;
esac
shift
# 빈 값도 명시된 가드이므로 삭제하거나 우회하지 않는다.
if [[ ${HERMES_DELEGATED_CHILD_CONTEXT+x} || ${HERMES_KANBAN_TASK+x} ]]; then
  printf '%s\n' 'Refusing delegated/task context.' >&2
  exit 2
fi
if [[ -z ${UNIFIED_KANBAN_CONVERSATION_CONFIG:-} || ! -f ${UNIFIED_KANBAN_CONVERSATION_CONFIG:-} || ! -r ${UNIFIED_KANBAN_CONVERSATION_CONFIG:-} ]]; then
  printf '%s\n' 'An explicit existing readable UNIFIED_KANBAN_CONVERSATION_CONFIG file is required.' >&2
  exit 2
fi
# 셸 함수가 아닌 PATH의 실행 파일을 선택하며 설정 내용은 읽거나 복사하지 않는다.
native="$(type -P -- "$agent")" || { printf '%s\n' "Native executable not found in PATH: $agent" >&2; exit 127; }
umask 077
exec "$native" "$@"
