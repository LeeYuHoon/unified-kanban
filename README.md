# Unified Kanban

Hermes Agent, Claude Code, Codex에서 한 작업을 한곳에 모아 보여 주는 프로젝트입니다.

> 현재 macOS만 지원합니다.

## 포함된 Hermes 버전

- Hermes Agent: `0.20.6`
- 공식 기반 commit: `10b388300a63d83857fac3ca4f8b05b64e01bc50`
- Unified Kanban release commit: `edf5e1dbd80dc71cd69b483f92a9829c58685d6e`

Hermes가 업데이트되면 이 정보도 함께 바뀌며, 실제 배포 bundle과 다르면 CI가 실패합니다.

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

터미널에서 다음 명령으로 Dashboard를 엽니다.

```bash
hermes dashboard
```

1. **Kanban** 화면에서 상황판을 만들거나 선택합니다.
2. 상황판 설정의 **Project directory**에 실제 작업 폴더의 전체 경로를 넣습니다.
3. 해당 작업 폴더에서 Hermes Agent, Claude Code 또는 Codex를 실행합니다.

이제 실제 사용자 요청마다 카드가 생기고 작업이 끝나면 결과가 기록됩니다. 카드에는 최종 응답이 남으므로 비밀번호, API 키와 같은 민감정보를 요청이나 응답에 넣지 마세요.

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
