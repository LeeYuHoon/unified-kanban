# Unified Kanban

Hermes Agent, Claude Code, Codex에서 한 작업을 한곳에 모아 보여 주는 프로젝트입니다.

> 현재 macOS만 지원합니다.

## 포함된 Hermes 버전

- Hermes Agent: `0.21.1`
- 공식 기반 commit: `f03ed94a34f47ebca57e4a1b0a890bc2aeb5e140`
- Unified Kanban release commit: `7b6c1856d60384116d2a3d496586989308bc463d`

Hermes가 업데이트되면 이 정보도 함께 바뀌며, 실제 배포 bundle과 다르면 CI가 실패합니다.
이 버전에는 Claude Fable 5.1 모델 목록 지원이 포함되어 있습니다.

## 무엇을 하는 프로젝트인가요?

AI 도구에 일을 요청하면 진행 상황판에 카드가 생깁니다. 작업이 끝나면 결과와 사용량도 같은 카드에 기록됩니다.

- Hermes Agent, Claude Code, Codex의 작업을 한 화면에서 봅니다.
- 작업 폴더에 맞는 상황판을 자동으로 찾습니다.
- 기존 설정은 지우지 않고 필요한 연결만 추가합니다.
- 내부 확인 작업이나 자동 알림은 별도 카드로 만들지 않습니다.

설치의 자세한 안전 규칙은 [구현 명세](docs/unified-kanban-spec.md)와 [유지관리 문서](docs/maintenance.md)에 있습니다.

## 설치

### 설치 전에 확인하세요

- macOS가 필요합니다.
- 새 Mac에는 Git, Bash, `curl`이 필요합니다. 나머지 Hermes 도구는 설치 스크립트가 준비합니다.
- Hermes Agent를 이미 사용 중이라면 Python 3.11 이상, Hermes CLI, Git, `uv`, Node와 `npm`이 준비되어 있어야 합니다.
- 비공개 저장소라면 GitHub에서 이 저장소를 읽을 권한이 있어야 합니다.

`Repository not found`가 나오면 GitHub 로그인과 저장소 권한을 먼저 확인하세요.

설치한 프로젝트 폴더는 계속 사용합니다. 설치 후 옮기거나 삭제하지 마세요.

### 새 Mac에 설치하기

Hermes Agent가 없는 Mac에서도 아래 명령만 실행하면 됩니다. Hermes 설치기를 따로 실행하지 마세요.

```bash
cd "$HOME"
git clone https://github.com/LeeYuHoon/unified-kanban.git
cd unified-kanban
./scripts/setup.sh
```

설치가 끝나면 Hermes Agent에 로그인하고 설치 확인용 상황판을 만듭니다.

```bash
export PATH="$HOME/.local/bin:$PATH"
hermes setup
hermes kanban boards create --name "Unified Kanban Smoke" unified-kanban-smoke
./scripts/kanban-smoke.sh
```

마지막 명령은 테스트 카드를 만들고 완료한 뒤 보관함으로 보내면서 설치 상태를 확인합니다.

### Hermes Agent를 이미 사용하고 있다면

기존 Hermes 소스 폴더는 초기화하거나 수정하지 않습니다. 기존 설정, 로그인 정보, 상황판과 카드 데이터도 지우지 않습니다. 대신 이 프로젝트가 확인한 Hermes 복사본을 별도 폴더에 만들고, 앞으로 그 복사본을 사용하도록 연결합니다. Hermes 설정에는 Unified Kanban plugin을 쓰는 데 필요한 관리 항목만 추가합니다.

먼저 실제 설치 없이 필요한 조건과 충돌 여부만 확인할 수 있습니다.

```bash
./scripts/setup.sh --dry-run --no-restart --skip-smoke
```

문제가 없다면 실제 설치를 실행합니다.

```bash
./scripts/setup.sh
```

Hermes 소스 폴더가 기본 위치가 아니라면 절대 경로를 지정하세요.

```bash
HERMES_AGENT_REPO="/absolute/path/to/hermes-agent" ./scripts/setup.sh
```

기존 Hermes 소스 폴더에 사용자가 바꾼 내용이 있어도 setup은 그 폴더를 고치거나 실행 대상으로 쓰지 않습니다. 다만 Unified Kanban이 관리할 연결이나 서비스가 다른 설치와 충돌하거나, 설치 상태가 불완전하거나, 파일 권한이 안전하지 않으면 아무것도 덮어쓰지 않고 멈춥니다. 오류가 나도 Hermes 폴더를 임의로 삭제하거나 다시 설치하지 말고 오류 메시지를 먼저 확인하세요.

설치가 끝나면 실행 중인 Hermes CLI/TUI/Desktop, Claude Code, Codex CLI를 모두 종료하고 다시 여세요.

## 사용 방법

### 날짜별 토큰 보기

보드 선택 위에는 **전체기간 누적**, 기존 요약 자리에는 **선택일 실측 사용량**을 표시합니다. 작은 달력의 기본은 한국 시간(KST) 오늘입니다. 날짜를 바꾸면 해당 날짜의 실행 카드와 함께 필터링하며 누적값은 그대로입니다. 자정에 걸친 실행은 겹친 날짜마다 카드에 보이지만, 토큰은 실제 `usage_at`이 속한 날짜에만 합산합니다.

두 요약 모두 전체·입력·캐시·출력·추론과 실행 경로별 토큰·카드 수를 보여 줍니다. Claude Code는 CLAUDE, Codex는 CODEX, Hermes는 기록된 실제 모델에 따라 CLAUDE·CODEX·모델미상·기타로 분류하며 각 이벤트는 한 번만 셉니다. 누적과 선택일은 서로 겹치므로 더하지 않습니다.

토큰 옆 비용은 제공자가 명시적으로 기록한 USD 값은 `$…`, 신규 요청에 정확한 모델·단가 버전·문맥 범위 근거가 모두 있을 때만 공식 API 환산값을 `≈ $…`로 표시합니다. 과거 기록이나 확인할 근거가 없으면 0 대신 **N/A**, 일부만 계산할 수 있으면 **일부**로 표시합니다. 이 값은 구독·OAuth 청구서와 다를 수 있습니다.

실제 사용시각이 기록된 수집분만 선택일 토큰으로 표시합니다. **N/A**는 확인할 수 없다는 뜻이며 0과 다릅니다. 실행 기록은 있지만 토큰 이벤트가 없으면 `토큰 수집 안 됨`, 미래 날짜에 실행이 없으면 `기록된 실행 없음`, 실패하면 `조회 실패`로 구분합니다. 과거 기록의 생성일 추정량은 실측에 합치거나 소급 보정하지 않습니다.

카드의 **모델 설정**은 다음 실행에 쓸 override이고, **실제 사용 모델**은 usage에서 관측된 값입니다. 여러 모델은 모두 표시하며 기록이 없으면 추측하지 않고 **미확인**으로 둡니다.

토큰은 **K(천)·M(백만)·B(십억)** 단위로 표시합니다. Hermes는 각 API 요청의 실제 모델·종료시각과 delta를 별도 이벤트로 기록합니다. Claude·Codex의 완료 시점 누적 snapshot과 날짜 없는 과거 사용량은 누적에는 남지만 선택일 실측에는 포함하지 않고 완료 귀속 또는 미수집으로 구분합니다.

자세한 기준은 [날짜별 토큰 안내](docs/daily-search.md)와 [토큰 비용 기준](docs/token-cost.md)을 보세요. 이 달력 UI는 검증 중인 후보이며 운영에는 아직 적용하지 않았습니다.

터미널에서 다음 명령으로 Dashboard를 엽니다.

```bash
hermes dashboard
```

1. **Kanban** 화면에서 상황판을 만들거나 선택합니다.
2. 상황판 설정의 **Project directory**에 실제 작업 폴더의 전체 경로를 넣습니다.
3. 해당 작업 폴더에서 Hermes Agent, Claude Code 또는 Codex를 실행합니다.

이제 실제 사용자 요청마다 카드가 생기고 작업이 끝나면 결과가 기록됩니다. 카드에는 최종 응답이 남으므로 비밀번호, API 키와 같은 민감정보를 요청이나 응답에 넣지 마세요.

외부 CLI 작업은 **관찰 카드**로 기록됩니다. 조회·댓글·설명 편집과 기록의 완료·보관·삭제는 가능하지만, 대시보드에서 외부 작업을 재실행하거나 담당자·모델을 바꾸고 작업 상태를 이동·드래그할 수는 없습니다. 실제 작업은 원래 CLI에서 관리합니다.

### 저장된 대화 보기

대화 수집은 설치만으로 켜지지 않습니다. 활성화하면 그 뒤 새로 만들어진 관찰 카드부터 공개 사용자 요청, 최종 답변과 도구·스킬·MCP·보조 작업의 제한된 활동 정보가 카드에 연결됩니다. private analysis, thinking, 도구 인자·결과와 과거 기록은 가져오지 않습니다. 본문은 로컬에만 두고 best-effort로 민감정보를 가리지만, 완전한 제거를 보장하지는 않습니다.

Claude의 첫 요청 전에 대화 파일이 아직 없으면, 실제 `prompt_id`와 검증된 파일 부재가 있는 새 카드에만 **LIMITED PARTIAL** 수집을 허용합니다. 파일이 처음 열릴 때까지 로컬 작성자를 신뢰하는 제한된 방식이며, 그 사이 동일 UID가 파일을 바꿔 놓는 공격을 막는다는 보장은 없습니다. Stop 뒤에도 파일 쓰기가 늦을 수 있어 Claude 대화는 항상 `partial`로 표시하며, 확인할 수 없는 내용은 가져오지 않습니다. [정확한 보안 범위와 지원 형식](docs/claude-absent-start-security.md)을 참고하세요.

기본 보관 기간은 7일(설정 가능 1~30일), profile 전체 한도는 64 MiB입니다. 한도·권한·원본 identity를 확인할 수 없으면 저장이나 삭제를 성공으로 꾸미지 않고 중단합니다.

이 기능은 Dashboard의 **interactive 인증**과 선택한 보드에 대한 명시적 사용자 권한이 필요합니다. `/api/health`가 `auth_required:false`인 운영 환경에서는 켜지 마세요. 먼저 사용할 인증 provider와 그 provider가 검증한 안정적인 user id의 권한 범위를 확인해야 합니다. access token, refresh token, cookie나 비밀번호를 principal로 넣으면 안 됩니다.

권한을 확인한 뒤 owner가 다음처럼 선택한 보드에서만 활성화합니다. 이 명령은 앞으로의 Hermes 관찰 기록을 시작하고 owner 전용 로컬 key를 준비합니다. Claude Code와 Codex 원본 투영은 별도의 private runtime 설정까지 검토된 경우에만 동작하며, 설정이 없으면 닫힌 상태로 비활성화됩니다.

```bash
hermes kanban --board BOARD conversation-config --enable \
  --principal PROVIDER:STABLE_USER_ID \
  --retention-days 7 \
  --max-bytes 67108864
```

외부 Claude/Codex용 설정 파일만 준비하려면 저장소의 Python 환경에서 아래 명령을 실행합니다. `STATE_ROOT`는 아직 없는 절대 경로이고 부모는 현재 사용자 소유 `0700`이어야 합니다. `KERNEL_KEY`는 이미 존재하는 native key의 절대 경로입니다. native가 선택한 `HERMES_KANBAN_CONVERSATION_KERNEL_SECRET_FILE`, 또는 `HERMES_HOME/conversation-journal/kernel-receipt.key`와 정확히 일치해야 하며 새 native key는 만들지 않습니다.

```bash
PYTHONPATH=src python -m kanban_adapter.conversation_owner_cli init \
  --state-root /ABS/STATE_ROOT --kernel-secret-file /ABS/KERNEL_KEY \
  --board BOARD --principal PROVIDER:STABLE_USER_ID \
  --provider-root claude=/ABS/CLAUDE_ROOT \
  --expires-at-ns FUTURE_UNIX_NANOSECONDS
```

필요하면 `--provider-root codex=/ABS/CODEX_ROOT`와 검증된 `--principal`을 반복합니다. 보드는 소문자 slug 하나만, 원본 provider는 `claude`·`codex`만 허용합니다. provider root와 key 부모도 현재 사용자 소유 `0700`, key는 `0600` 일반 파일·단일 링크여야 합니다. provider root는 projector와 동일한 no-follow 검사로 시스템 별칭도 거절합니다. macOS에서는 `/var` 대신 `/private/var` 같은 정식 경로를 직접 지정해야 하며 입력을 자동 변환하지 않습니다. 사용자 심볼릭 링크 경로와 기존 state는 거절합니다. 실패한 준비 폴더는 자동 삭제하거나 덮어쓰지 않습니다.

새 authority key·서명 정책을 먼저 기록하고 `runtime.json`을 마지막에 준비합니다. 파일 안의 `enabled:true`는 준비 상태일 뿐 **환경 변수나 launchd 설정은 변경하지 않습니다**. 별도 검토 후 `UNIFIED_KANBAN_CONVERSATION_CONFIG`를 연결하기 전에는 외부 수집이 켜지지 않으며 Hermes 수집도 이 명령으로 켜지지 않습니다. 테스트는 임시 key와 합성 hook 기록의 투영 검증이며 실제 provider 추론·운영 활성화 검증이 아닙니다. 저장장치 장애나 동일 사용자에 의한 동시 변조까지 원자적 삭제로 복구한다고 보장하지 않습니다. 마지막 publication 실패 뒤 무효화도 실패하면 원래 오류와 cleanup 진단을 함께 출력하며 설정이 사용 가능한 상태로 남을 수 있습니다.

선택적 native 회귀 검증은 `UNIFIED_KANBAN_TEST_HERMES_SOURCE=/ABS/REVIEWED_HERMES_SOURCE .venv/bin/python -m pytest tests/test_conversation_owner_cli.py -k real_native -s`로 실행합니다. 실제 source API의 issue_observation_receipt → pipe → capture/seal → projection 경로를 임시 HOME·무작위 key·허용 목록 환경에서 검증합니다. membership과 provider transcript는 합성이며 실제 카드 생성·provider 추론 E2E가 아닙니다. source 및 운영 설정·데이터는 변경하지 않습니다.

즉시 새 수집을 멈추려면 다음을 실행합니다. 이미 저장된 owner 확인 기록은 자동 만료 또는 명시 삭제 전까지 남습니다.

```bash
hermes kanban --board BOARD conversation-config --disable
hermes kanban --board BOARD conversation-delete TASK_ID --json
```

Dashboard 카드의 **저장된 대화 삭제**도 같은 인증·보드·task 범위를 다시 확인합니다. 결과가 `verified:false`이면 삭제 완료가 아닙니다. identity 없는 보조 작업은 연결을 추측하지 않고 `partial`로 표시합니다.

설치 상태를 다시 확인하려면 다음 명령을 실행하세요.

```bash
command -v kanban-adapter
hermes kanban boards list --json
./scripts/kanban-smoke.sh
```

### 업데이트

```bash
cd "$HOME/unified-kanban"  # 실제 설치 폴더로 바꾸세요
git pull --ff-only
./scripts/update-hermes-if-needed.sh
./scripts/setup.sh
```

업데이트할 때도 기존 Hermes 폴더, 사용자 설정, 로그인 정보와 카드 데이터는 지우지 않습니다.

### 문제가 생기면

| 증상 | 확인할 것 |
| --- | --- |
| `Repository not found` | GitHub 로그인과 저장소 권한을 확인합니다. |
| `hermes: command not found` | 새 터미널을 열거나 `export PATH="$HOME/.local/bin:$PATH"`를 실행합니다. |
| `Hermes Agent checkout not found` | 새 Mac에서는 미리보기 없이 실제 설치를 실행합니다. 기존 사용자라면 `HERMES_AGENT_REPO`에 Hermes 폴더의 전체 경로를 넣습니다. |
| `Hermes version mismatch` | 업데이트 명령과 설치 명령을 차례로 다시 실행합니다. |
| `Refusing foreign ...` | 파일을 바로 지우지 말고, 이전 설치 폴더에서 삭제 명령을 먼저 실행했는지 확인합니다. |
| 설치 확인 실패 | `hermes doctor`, `hermes kanban boards list --json`, `./scripts/kanban-smoke.sh` 순서로 확인합니다. |

문제가 계속되면 오류 메시지를 그대로 보존하고 [업데이트 체크리스트](docs/hermes-update-checklist.md)를 확인하세요.

## 삭제

Unified Kanban이 추가한 연결과 설정만 제거합니다.

```bash
cd "$HOME/unified-kanban"  # 실제 설치 폴더로 바꾸세요
./scripts/uninstall.sh
```

기존 Hermes 폴더, 설정, 로그인 정보, 상황판과 카드 데이터는 삭제하지 않습니다. 안전한 재설치와 문제 확인에 필요한 일부 상태 파일도 남겨 둡니다.

개발 참여 방법은 [CONTRIBUTING.md](CONTRIBUTING.md), 보안 문제 제보는 [SECURITY.md](SECURITY.md)를 참고하세요.
