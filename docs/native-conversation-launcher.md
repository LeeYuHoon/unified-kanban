# 영구 선택자 기반 네이티브 실행 후보

이 문서는 `bin/collected-native-agent`와 `conversation_launch.py`만 설명한다. 설치·운영 활성화가 끝났다는 문서가 아니다. 기존 `scripts/launch-collected-agent.sh`와 별개이며 setup은 이 후보를 설치하지 않는다.

## 실행 계약

명시적으로 승인된 상태를 준비한 뒤 사용하는 형식은 다음과 같다. 이 작업에서는 실제 상태를 준비하거나 네이티브 공급자를 실행하지 않았다.

```text
/absolute/repository/bin/collected-native-agent codex [native arguments...]
/absolute/repository/bin/collected-native-agent claude [native arguments...]
```

첫 번째 인수는 반드시 공급자다. 파일을 단순히 `codex`라는 이름으로 링크하는 것만으로 공급자를 추론하지 않는다. 향후 일반 명령 연동은 공급자 인수를 넣는 승인된 명령 바인딩 또는 별도 얇은 진입점과 그 설치 테스트가 필요하다.

- 저장소의 `.venv/bin/python`과 `src`를 사용한다. Python은 `-I -B`로 실행한다. 링크 체인을 따라 저장소를 찾으며, `source`는 거부한다.
- 현재 `HERMES_HOME` 또는 `HOME/.hermes`의 `unified-kanban-conversation/activation.json`만 사용한다. 준비된 runtime 파일만 존재해도 자동 활성화하지 않는다.
- 선택자는 정확한 `schema_version=1`, `enabled`, `runtime_config`, `launcher_profiles` 구조를 가진다. `enabled=true` 및 해당 공급자의 명시적 프로필 참조가 필요하다. 부모는 소유자 전용 0700, 메타데이터는 소유자 전용 0600 단일 링크 일반 파일이어야 한다.
- 공급자 프로필은 정확히 `schema_version=1`, `provider`, `executable`, `sha256` 필드를 갖는다. executable은 원본의 절대 경로, sha256은 승인한 원본 바이트의 소문자 64자리 SHA-256이다. 이 문서는 실제 경로나 해시를 생성하지 않는다.
- 원본을 PATH로 재검색하지 않는다. 링크·비실행 파일·그룹/전체 쓰기·setuid/setgid·래퍼 자신과 그 하드링크·해시 불일치 등을 거부한다. 공급자 업데이트 뒤 해시가 바뀌면 재승인 전까지 거부한다. 래퍼 복사본이나 임의의 승인된 스크립트가 래퍼를 다시 부르는 경우에는 상속되는 재진입 표식으로 거부한다. 표식을 제거하는 악성 승인 실행 파일까지 통제하는 샌드박스는 아니다.
- `HERMES_DELEGATED_CHILD_CONTEXT`, `HERMES_KANBAN_TASK`, `UNIFIED_KANBAN_NATIVE_LAUNCH_ACTIVE` 중 하나라도 존재하면 빈 값이어도 실행을 거부한다. 표식을 제거하지 않는다.
- 검증한 선택자의 전체 내용 변경을 거부하고 검증된 runtime 경로를 사용한다. 명시적 `UNIFIED_KANBAN_CONVERSATION_CONFIG` 재정의는 유효한 절대 경로일 때만 우선한다. 재정의도 영구 선택자의 동의를 대체하지 않는다.
- 자식 실행 프로세스에서만 umask 077을 설정하고 `execve`한다. native argv, cwd, HOME, 계정별 CODEX_HOME과 나머지 환경을 유지한다. 원본 종료 코드·신호를 전달한다. 보드 재정의나 계정 홈 재작성은 없다.
- 기존 원본 파일·디렉터리 권한, 본문, 타임스탬프를 수정하지 않는다. umask는 새 파일의 생성 모드에만 영향을 주며, 기존 공개 transcript나 공급자의 이후 chmod를 고치지 않는다.

## Orca에서 확인한 실제 공개 경계

선택한 CLI는 `/opt/homebrew/bin/orca`다. `skills get orca-cli`로 해당 바이너리의 가이드를 읽고 `status --json`, `--help`, `agent-context --json`만 실행했다. runtime은 appVersion `1.4.203`, ready/connected였다. 현재 공개 schema는 캐시 `cache/organic-orca-public-schema.json`과 JSON 전체가 동일했다(schemaVersion 1, commandCount 234).

공개 `terminal create --command <text>`는 명시적 명령을 넣는 **한 번의 터미널 실행 경계**다. 승인 후 위 후보의 절대 경로와 공급자 인수를 셸에 안전하게 인용하여 넣을 수 있는 후보 표면이지, 이번 작업에서 검증한 실제 실행이나 영구 설정은 아니다. 공개 flags에는 CODEX_HOME/계정 선택 환경 주입 계약이 없다. 일반 command 실행이 Orca의 내장 계정 선택을 동일하게 적용하는지는 별도 검증이 필요하다.

`worktree create --agent <id>`는 내장 launcher를 사용하며 공개 schema에 원본 실행 경로·prefix·임의 argv 재정의가 없다. 전체 공개 명령 목록에서도 영구 provider launcher 설정 명령은 발견하지 못했다. 이는 GUI 설정 자체가 존재하지 않는다는 증명이 아니다. 지원되는 설정 표면을 확인하기 전에는 내부 config/RPC/앱 소스 수정으로 대신하지 않는다.

**PATH shim, alias, .zshrc는 GUI가 절대 vendor 경로로 호출하는 경계를 가로채지 못한다.** 이번 결과는 Orca 일반 버튼, 새 worktree의 `--agent`, 세션 resume까지 수집 활성화됐다는 증거가 아니다.

## 보안 검토 한계 및 배포 게이트

검토한 범위는 저장소 래퍼·선택자 참조·원본 검증·fixture 실행이다. 운영 키, transcript, vendor 실행 파일, 계정 설정은 읽거나 바꾸지 않았다.

- 이 구현은 검증 후 경로 기반 exec를 사용한다. 열린 원본 FD를 닫은 뒤 exec까지의 교체 경쟁, 같은 소유자의 승인 상태 변경/ABA, 실행 파일의 동적 의존성까지 원자적으로 봉인하지 않는다. SHA-256은 검증 시점의 바이트 증거이지 원자적 실행 증명이 아니다.
- 모든 native/metadata 조상을 디렉터리 FD로 열어 root 또는 현재 UID 소유와 non-owner 쓰기 금지를 확인한다. macOS `/var`, `/tmp`, `/etc`만 `/private` 아래의 OS 별칭으로 매핑한다. root 소유 sticky `/tmp` 또는 `/private/tmp`만 공개 쓰기 예외이며 그 아래 모든 구성 요소는 다시 검증한다. 임의 사용자 sticky 디렉터리는 예외가 아니다.
- macOS ACL은 mode를 검사한 동일 FD에서 `acl_get_fd_np`로 읽는다. private selector/profile/직접 부모는 모든 ACE를 거부한다. 그 밖의 원본·조상·bootstrap 소스는 deny-only ACE만 허용하고 모든 allow ACE를 보수적으로 거부한다(읽기 전용 allow도 거부). ACL 조회 실패는 거부하며 chmod/ACL 수선은 하지 않는다.
- shell bootstrap은 고정 `/usr/bin/python3 -I -S -B`에서 인라인 namespace 검증만 먼저 실행한다. 설치 링크 체인, checkout/source 전체 패키지, 선택 인터프리터의 링크 및 대상 조상을 확인한 후에만 해당 Python과 저장소 코드를 실행한다. 인터프리터 링크의 각 부모도 확인하며 단순 realpath 신뢰로 대신하지 않는다. 실행은 해석된 인터프리터의 stdlib-only `-I -S -B`이고 venv site-packages/`.pth`를 로드하지 않는다. 시스템 Python이 없으면 실패하며 PATH fallback은 없다.
- 이 bootstrap은 최초 실행된 repository-owned shell 바이트와 OS Python/stdlib/loader가 신뢰된다는 전제가 있다. 공격자가 래퍼 자체를 실행 전에 바꾸는 것을 래퍼 자체의 검사로 막을 수 없다. ownership-safe 설치와 승인된 wrapper 배포는 여전히 필수다. 인터프리터/native의 전체 동적 의존성을 재귀 인증하는 기능이 아니다.
- 기존 shell startup 및 native 환경은 보존한다. 악성 BASH_ENV·네이티브 로더 환경 등을 격리하는 범용 샌드박스가 아니다.
- 선택자 경로를 전달할 뿐 runtime의 정책·grant·reader·provider root 유효성은 여기서 검증하지 않는다. fixture의 runtime 파일은 실제 서비스가 아니다.

남은 배포 순서: 소유권 안전한 installer/명령 바인딩 및 trusted-path 검증 → 승인된 영구 selector와 원본 프로필 발행 → 지원되는 Orca 영구 실행 설정 확인 및 CODEX_HOME 검증 → 필요한 소비자 갱신 → 승인된 새 실제 turn의 카드·receipt·서명 binding·정확한 공개 projection·인증된 화면과 새로고침 검증. 각 단계는 별도 승인 범위를 따라야 한다. fixture 통과는 이 배포 게이트를 대체하지 않는다.
