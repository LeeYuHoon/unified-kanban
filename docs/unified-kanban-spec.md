# 통합 에이전트 칸반 — 구현 명세서

> 작성일: 2026-07-09
> 구현 주체: Hermes Agent
> 근거: Hermes 공식 문서, GitHub 이슈/릴리스 노트, 커뮤니티 사례 조사 (본문에 출처 표기)
> ⚠️ Hermes는 릴리스 주기가 매우 빠름(3주에 메이저 4회 사례). 본 문서의 CLI 인자·경로는
> 조사 시점 기준이며, **구현 전 로컬에서 `--help`로 재검증할 것** (§6 참조).

---

## 1. 내가 원하는 바

### 1.1 목표

- **Hermes Agent / Claude Code / Codex** — 어느 도구에서 작업하든, 모든 작업 과정이
  **하나의 칸반 보드**에 기록된다.
- 보드는 **프로젝트별로 분리**된다.
- 보드는 **웹 대시보드(브라우저)**에서 본다.
- 모든 카드에 **어디서 실행된 작업인지 출처가 표기**된다 (`hermes` / `claude` / `codex`).
  **체인 포함**: Hermes에서 시작해 Claude Code나 Codex를 호출한 작업은
  `hermes&claude`, `hermes&codex`처럼 시작 주체와 실행 주체를 함께 표기.

### 1.2 동작 방향 — 양방향

| 방향 | 설명 | 예시 |
|---|---|---|
| **관찰 (도구 → 보드)** | 내가 각 도구에서 자유롭게 작업하면, 진행 상황이 자동으로 카드에 기록됨 | Claude Code 세션 시작 → 카드 자동 생성 |
| **지휘 (보드 → 도구)** | 칸반에 태스크를 만들면 Hermes가 워커로 dispatch, 실행·결과가 카드에 반영됨 | 카드 생성 → Hermes 워커가 처리 |

상황에 따라 두 방향을 섞어 쓴다.

### 1.3 우선순위

빠른 기성 도구 채택보다 **내 워크플로우에 정확히 맞는 커스텀 배선**을 우선한다.
단, 커스텀의 취약점(Hermes 변경으로 인한 파손)은 §5의 방어책으로 상쇄한다.

---

## 2. 조사로 확정된 전제 (구현 판단의 근거)

구현 전에 알아야 할, 조사로 확인된 사실들.

### 2.1 Hermes Kanban 아키텍처 (✅ 공식 문서 확인)

- 단일 진실 소스는 SQLite: 기본 보드는 `~/.hermes/kanban.db`,
  추가 보드는 `~/.hermes/kanban/boards/<slug>/kanban.db`.
- 세 개의 접근 표면 — **대시보드 / CLI(`hermes kanban <verb>`) / 워커 툴(`kanban_*`)** —
  이 전부 같은 DB를 읽고 쓴다. 문서 표현: "the three surfaces can never drift".
- 대시보드는 append-only `task_events` 테이블을 WebSocket으로 tail →
  **누가(CLI든 외부 프로세스든) DB를 바꾸든 즉시 UI 반영**.
- 대시보드의 드래그앤드롭은 `PATCH /api/plugins/kanban/tasks/:id` REST 호출이며,
  CLI와 동일한 `kanban_db` 코드를 경유.
- 대시보드 HTTP auth 미들웨어는 public allowlist를 제외한 **모든 `/api/` 경로**를
  보호하며 `/api/plugins/` 예외가 없다. loopback에서도 HTTP는 임시 세션 토큰이
  필요하고, WebSocket도 공통 WS 인증 게이트를 통과해야 한다 (§6-6 실측).
- 보드 해석 우선순위: `--board <slug>` 플래그 → `HERMES_KANBAN_BOARD` 환경변수 →
  `~/.hermes/kanban/current` 파일 → `default`.
- 태스크 상태: `triage | todo | scheduled | ready | running | blocked | review | done | archived`.
- dispatcher는 기본적으로 게이트웨이에 내장(`kanban.dispatch_in_gateway: true`)되어
  ready 태스크를 **Hermes 프로필**로 spawn.
- assignee가 resolve되지 않는 태스크는 실행되지 않고 `ready`에 남는다.
  dispatcher 출력에는 `Skipped (non-spawnable assignee)`가 표시되지만 현행 실측상
  `task_events`에는 별도 이벤트가 기록되지 않는다 (§6-5).
- `triage` 컬럼은 기본 설정(`kanban.auto_decompose: true`)에서 decomposer가 자동 분해.
  대시보드 상단 **Orchestration: Auto/Manual** 토글로 제어 가능.

### 2.2 서드파티 에이전트 연동의 현재 상태 (✅ 확인)

- **이슈 #18629** (2026-05-02 등록): `hermes kanban create --acp-command/--acp-args`로
  Claude Code/Codex/OpenCode를 직접 dispatch하자는 제안. **미머지로 판단**
  (현행 CLI 문서의 `kanban create` 플래그 목록에 해당 옵션 부재, 라벨 P3).
- **`kanban-codex-lane` 스킬**: Hermes 워커가 Codex를 격리된 구현 보조 라인으로 쓰는
  규약이었으나, 최근 릴리스에서 **삭제됨**(dead skill, 옵션 이동 아님).
- 공식 **worker-lanes 문서**가 신설되어 서드파티 CLI 워커 레인의 계약(contract)을
  정의하지만, 명시적으로 "**not yet a paved path**"(아직 닦인 길 아님).
  Codex 전용 PR #19924는 closed-not-merged.
- dispatcher의 spawn 함수는 플러그인화 가능(`spawn_fn` 파라미터). 비-Hermes assignee용
  spawn_fn을 플러그인으로 등록하는 경로가 문서상 열려 있음 (장기 옵션).

**⇒ 결론: 서드파티 도구를 칸반에 붙이는 공식 네이티브 경로는 아직 없다.
   따라서 §3의 커스텀 배선이 현재의 정답이며, §5.4의 이관 계획을 병행한다.**

### 2.3 참고 사례 (✅ 확인)

- **Shubham Saboo 워크플로우**: Hermes `/goal`이 Codex/Claude Code를 교체 가능한
  워커로 취급, 각 goal을 칸반 카드로 추적. 지휘 방향의 실운영 선례.
- **Adam-Dangerfield/Agent-Kanban**: 에이전트가 4개 호출(create/claim/comment/done)로
  자기 작업을 보드에 기록하는 철학. `.claude/skills/kanban/`에 넣는 이식형
  Claude Code 스킬 포함. → §3.3의 관찰 방향 배선의 참고 구현.
- **amanning3390/hermes-agent-kanban**: Hermes Web UI에 네이티브 칸반 탭을 추가하는
  대시보드 확장(패치 방식). 필요시 대시보드 보강 옵션.

### 2.4 모델/구독 관련 주의 (이전 논의에서 확정, 칸반과 간접 연관)

- Claude Max 5x로 서드파티(OpenCode 등)에서 Claude를 쓰는 것은 2026-02 약관상 금지.
  6-15에 Agent SDK 크레딧 분리가 **유예**되어 현재는 회색지대이나,
  **규정상 깨끗한 경로는 공식 Claude Code CLI 직접 실행**. 본 설계는 이를 따름
  (Claude Code를 공식 CLI로 돌리고, 칸반은 기록/오케스트레이션만 담당).
- Codex는 ChatGPT 구독 인증이 공식 지원되므로 문제 없음.
- GPT-5.5가 Codex Pro 티어에 포함되는지는 미확인 → Codex 설치·로그인 후
  `codex debug models`와 대화형 `/model`로 확인 필요 (§6-7).

---

## 3. 구현

### 3.1 아키텍처

```
        ┌─────────────────────────────────────────┐
        │   hermes dashboard (브라우저 웹 UI)        │  ← 관찰 지점
        │   보드 전환: project-a / project-b / ...  │
        └───────────────┬─────────────────────────┘
                        │ WebSocket (task_events tail)
        ┌───────────────┴─────────────────────────┐
        │  ~/.hermes/kanban/boards/<slug>/kanban.db │  ← 단일 진실 소스
        └───────┬───────────────┬────────────┬─────┘
                │               │            │
        ┌───────┴─────┐   ┌────┴─────┐  ┌───┴──────┐
        │ Hermes      │   │ kanban-  │  │ kanban-  │
        │ (네이티브    │   │ adapter  │  │ adapter  │
        │  워커/지휘)  │   └────┬─────┘  └───┬──────┘
        └─────────────┘        │            │
                        ┌──────┴─────┐ ┌────┴───────┐
                        │ Claude Code│ │ Codex      │
                        │ (훅)       │ │ (래퍼/훅)   │
                        └────────────┘ └────────────┘
```

핵심 원칙:
1. **DB가 허브.** 세 도구는 모두 같은 보드 DB에 쓰는 클라이언트.
2. **외부 도구는 어댑터를 통해서만 쓴다.** Hermes 인터페이스 변경의 파급을 1곳에 가둠 (§5.1).
3. **보드 라우팅은 Kanban board metadata로 자동화.** Dashboard에서 설정한
   `default_workdir`와 현재 작업 디렉터리를 비교해 프로젝트가 곧 보드를 결정한다 (§3.5).

### 3.2 단계 0 — 보드 생성 (프로젝트별)

```bash
hermes kanban init   # 최초 1회 (자동 init되지만 명시적으로)
hermes kanban boards create --name "Project A" --icon 🚀 project-a
hermes kanban boards create --name "Project B" --icon 🔧 project-b
hermes dashboard     # http://127.0.0.1:9119 → Kanban 탭
```

### 3.3 단계 1 — 어댑터 (`kanban-adapter`)

**목적**: 세 도구의 훅이 Hermes CLI/API를 직접 알지 못하게 하는 안정화 계층.

**노출 인터페이스 (안정 계약 — 바뀌지 않음)**:

```
kanban-adapter start   [--board <slug>] --title <t> --source <claude-code|codex|manual>
                       → 카드 생성, task_id를 stdout으로 반환
kanban-adapter update  --task <id> --message <msg>       → 코멘트 추가
kanban-adapter done    --task <id> [--summary <s>]        → 완료 처리
kanban-adapter block   --task <id> --reason <r>           → 블록 (리뷰 대기 등)
```

**내부 구현 (교체 가능 — 여기만 Hermes를 안다)**:

- **1차 백엔드: CLI** — `hermes kanban --board <slug> create/comment/complete/block ...`
  (§6-1, §6-6 실측: REST는 존재하지만 HTTP 인증이 필수이고 세션 토큰은 프로세스별 임시값)
- **선택 백엔드: REST** — `http://127.0.0.1:9119/api/plugins/kanban/...`
  안정적인 서비스 토큰 또는 공식 비대화형 인증 경로를 별도로 마련한 경우에만 사용.
- 백엔드 선택은 어댑터 설정 파일 한 줄로 전환한다.
- adapter board 해석 우선순위는 명시적 `--board` →
  `hermes kanban boards list --json`의 `default_workdir`와 현재 디렉터리 최장-prefix 일치 →
  매핑이 없을 때만 `HERMES_KANBAN_BOARD` 호환 fallback이다. 같은 디렉터리가 여러 board에
  중복 지정되면 임의 선택하지 않고 설정 오류로 실패한다.

> REST 카드 생성은 `POST /tasks?board=<slug>`로 존재하며 페이로드는 §6-1에 실측 기록했다.
> 인증을 우회하거나 대시보드의 임시 세션 토큰을 스크레이핑하지 않는다.

**관찰용 카드의 dispatcher 격리 (중요)**:

- `--source claude-code|codex`로 생성되는 카드는
  assignee를 **spawn 불가 식별자**(`claude-code-external`, `codex-external`)로 설정.
- §2.1의 확인된 동작에 따라 dispatcher는 이를 실행하지 않고 ready 상태로 남긴다.
  → 외부 도구가 이미 하는 일을 Hermes가 중복 실행하는 사고 방지.
- 추가 안전장치: CLI의 `--initial-status running`으로 관찰용 카드를 처음부터
  `running` 상태로 생성한다. 이 경로는 실제 워커 claim 없이 외부 실행을 나타내기 위한
  terminal lane이다. 보드 전체 Orchestration을 Manual로 바꾸지는 않는다.
- §6-5 실측에서 non-spawnable ready 카드는 실제 spawn되지 않았지만
  `skipped_nonspawnable` 이벤트는 DB에 남지 않았다. 따라서 이벤트 존재를 감사 근거로 쓰지 않는다.

### 3.4 단계 2 — 도구별 배선

**① Hermes (배선 불필요 — 지휘 방향 담당)**

- 네이티브. `hermes kanban create --assignee <profile>`로 태스크를 만들면
  dispatcher가 프로필 워커를 spawn — 기존 기능 그대로.
- Codex/Claude Code를 지휘 방향으로 쓰려면 (Saboo 패턴):
  `claude -p` 또는 `codex exec`만 호출하는 얇은 전용 프로필을 만들어 assignee로 지정.
  (#18629가 지적한 "프로필 래퍼" 오버헤드가 있으나, 현재로선 이게 정석)

**② Claude Code (관찰 방향 — 훅)**

Claude Code 훅 시스템(`settings.json`)에 어댑터 호출을 건다:

| 훅 시점 | 어댑터 호출 |
|---|---|
| 사용자 prompt (`UserPromptSubmit`) | stdin JSON의 `session_id`, `cwd`, `prompt`로 프로젝트 디렉터리에서 `kanban-adapter start --title "<prompt 요약>" --source claude-code` |
| 주요 진행(선택) | `kanban-adapter update --task $TASK_ID --message "<진행>"` |
| 응답 종료 (`Stop`) | `last_assistant_message`로 `kanban-adapter done --task $TASK_ID --summary "<결과>"` |
| 세션 종료 (`SessionEnd`) | Stop이 실행되지 않은 잔여 카드의 cleanup fallback |

- task_id는 훅 간 전달 필요 → `session_id`를 키로 한 사용자 캐시
  (`~/.cache/kanban-adapter/claude/<sha256(session_id)>.json`)에 원자적으로 보관.
- 사용자가 기대하는 작업 단위가 prompt/response 턴이므로 `Stop`을 해당 카드 완료에 사용한다.
- `SessionEnd`는 인터럽트 등으로 Stop이 누락된 경우에만 남은 상태를 완료한다.
- 참고 구현: Adam-Dangerfield의 이식형 Claude Code 칸반 스킬 구조를 차용하되,
  타깃을 어댑터로 교체.
- 정확한 이벤트명과 페이로드는 §6-2에서 Claude Code 2.1.201 + 최신 공식 문서 기준으로 확정했다.

**③ Codex (관찰 방향 — 네이티브 훅)**

- `~/.codex/hooks.json`의 `UserPromptSubmit`에서 prompt/작업 디렉터리를 읽어
  `kanban-adapter start --source codex`를 호출한다.
- `Stop`에서 같은 session ID의 카드를 완료한다. 카드 단위를 사용자 prompt/response
  턴으로 정의하므로 Codex에 `SessionEnd`가 없어도 lifecycle이 닫힌다.
- setup은 기존 Codex hook을 보존하고 repository-owned command만 멱등 추가한다.
- Codex의 보안 모델상 새 hook은 첫 실행 시 자체 trust 승인이 필요할 수 있다. setup은
  `config.toml` 내부 trusted hash를 위조하거나 전역 trust bypass를 설정하지 않는다.
- 장기 `/goal` 작업은 지휘 방향(①의 전용 프로필)으로 흡수하는 것을 권장한다.

### 3.5 단계 3 — 프로젝트별 자동 라우팅

Dashboard의 Kanban 화면에서 board를 만들 때 **Project directory**를 설정한다.
Hermes는 이 값을 board metadata의 `default_workdir`로 저장한다. adapter는 매 호출마다
`hermes kanban boards list --json`을 읽고 현재 작업 디렉터리를 포함하는 가장 구체적인
`default_workdir`의 board를 선택한다.

따라서 `.envrc`, `direnv`, shell profile 수정, 프로젝트별 setup 인자가 필요 없다.
명시적 `--board`는 자동 해석을 덮어쓰는 고급 경로로 유지하고,
`HERMES_KANBAN_BOARD`는 Dashboard 매핑이 없는 디렉터리에서만 호환 fallback으로 사용한다.
도구별 설정 불필요 — Kanban 화면에서 지정한 디렉터리가 곧 보드다.

### 3.6 단계 4 — 출처(provenance) 표기

**요구**: 카드만 봐도 (a) 어디서 시작됐고 (b) 무엇이 실제 실행했는지 알 수 있어야 한다.
Hermes가 시작해 다른 도구에 위임한 작업은 체인으로 표기한다.

**표기 규칙** (구분자 `&`, 순서 = 시작주체&실행주체):

| 시나리오 | tenant 태그 |
|---|---|
| Claude Code에서 직접 작업 (관찰) | `claude` |
| Codex에서 직접 작업 (관찰) | `codex` |
| Hermes 네이티브 워커가 처리 | `hermes` |
| Hermes → Claude Code 위임 (`claude -p` 래퍼 프로필) | `hermes&claude` |
| Hermes → Codex 위임 (`codex exec` 래퍼 프로필) | `hermes&codex` |
| 사람이 대시보드/CLI에서 직접 생성·관리 | `manual` |

**저장 위치 — `--tenant` 필드 재활용**:
- 프로젝트 분리는 board가 담당하므로(§3.2, §3.5) tenant는 우리 설계에서 비어 있음.
- 대시보드 카드는 tenant 태그를 표시하고 tenant 필터를 지원함(공식 문서 확인) →
  출처 표기 + 출처별 필터링이 추가 개발 없이 확보됨.
- `dashboard.kanban.default_tenant` 설정으로 기본 필터 지정도 가능.

**설정 주체 (누가 태그를 붙이나)**:
- **관찰 방향**: 어댑터가 `--source` 값에서 tenant를 도출해 카드 생성 시 지정.
- **지휘 방향(네이티브)**: Hermes 오케스트레이터가 `kanban_create --tenant hermes`.
- **지휘 방향(위임)**: 위임 래퍼 프로필의 실행 주체는 **배선 시점에 확정**됨
  (프로필 = `claude -p` 전용이면 executor는 항상 claude). 따라서 오케스트레이터가
  `assignee → executor` 매핑 테이블을 갖고, 카드 생성 시 `--tenant hermes&claude`를
  결정적으로 지정. 추론이 아니라 배선 사실에 근거하므로 오표기 없음.

**보조 표기 — assignee 명명 규칙**:
위임 래퍼 프로필 이름을 `claude-lane`, `codex-lane`으로 통일. 카드에는 assignee도
표시되므로(공식 문서 확인) tenant와 assignee 두 필드로 출처를 이중 확인 가능.

**엣지 케이스 — 실행 중 위임**:
Hermes 워커가 작업 도중에 동적으로 Codex/Claude를 호출하는 경우(생성 시점엔 `hermes`로
태깅됨), 워커가 위임 시점에:
1. `kanban_comment`로 구조화 기록 추가 — `executors: hermes,codex` (worker-lanes 문서의
   "comments are the durable annotation channel" 원칙을 따름)
2. tenant는 사후 변경할 수 없으므로(§6-8 실측) comment 기록을 단일 진실로 삼고
   카드 태그는 생성 시점 기준으로 남긴다.

### 3.7 배포 단위 — 독립 Git 저장소 + `setup.sh`

**원칙**: Phase 1 이후 작성하는 구현 코드·테스트·훅·래퍼·설치 로직은 모두 하나의
독립 Git 저장소 안에서만 관리한다. `~/.hermes/`, `~/.claude/`, `~/.codex/`,
`~/.local/bin/`에는 구현 원본을 직접 작성하지 않는다.

권장 저장소 구조:

```text
unified-kanban/
├── README.md
├── LICENSE
├── .gitignore
├── pyproject.toml
├── src/kanban_adapter/
│   ├── __init__.py
│   ├── cli.py
│   ├── backend.py
│   └── state.py
├── bin/
│   ├── kanban-adapter
│   └── codex-kanban
├── integrations/
│   ├── claude-code/
│   │   ├── session_start.py
│   │   └── session_end.py
│   └── hermes/kanban-guard/
│       ├── HOOK.yaml
│       └── handler.py
├── scripts/
│   ├── setup.sh
│   ├── uninstall.sh
│   └── kanban-smoke.sh
├── tests/
└── docs/
    └── unified-kanban-spec.md
```

`scripts/setup.sh`의 계약:

1. **idempotent** — 여러 번 실행해도 중복 훅·중복 JSON 항목·깨진 링크를 만들지 않는다.
2. 필수 명령(`hermes`, `python3`, 선택적으로 `claude`/`codex`)과 버전을 검사한다.
3. 저장소의 `bin/*`를 `~/.local/bin/`에 **심볼릭 링크**한다. 복사본을 만들지 않아
   실제 구현 원본은 Git 저장소에만 남긴다.
4. Claude Code 훅 스크립트도 저장소 파일을 직접 가리키도록 `~/.claude/settings.json`을
   병합한다. 기존 설정은 timestamp 백업 후 수정하며 다른 사용자의 설정을 덮어쓰지 않는다.
5. Hermes `kanban-guard`는 `~/.hermes/hooks/kanban-guard`에 저장소 디렉터리를 가리키는
   심볼릭 링크로 설치한다. 게이트웨이를 재시작하고 로그/probe로 로드를 검증한다.
6. 기본 설치는 프로젝트 repo를 수정하지 않는다. Project directory routing은 Dashboard의
   board metadata를 사용한다. 기존 `--project-dir <path> --board <slug>` 기반 `.envrc`
   경로는 이전 설치와의 호환을 위해서만 유지한다.
7. 로컬 런타임 상태는 `${XDG_STATE_HOME:-~/.local/state}/unified-kanban/`, 캐시는
   `${XDG_CACHE_HOME:-~/.cache}/unified-kanban/`에 저장하며 Git 추적 대상이 아니다.
8. API 키·토큰·세션 토큰·개인 경로를 저장소에 기록하지 않는다. 필요한 값은 환경변수나
   로컬 전용 설정 파일에서 읽고 `.gitignore`로 차단한다.
9. 기존의 non-bootstrap-managed 설치에서는 마지막에 `scripts/kanban-smoke.sh`를 실행하고
   실패하면 비정상 종료한다. receipt-backed `bootstrap-managed` setup은 최초 설치와 이후 재실행
   모두 자동 인증 smoke를 실행하지 않는다. provider credential과 smoke board의 존재를 bootstrap
   receipt만으로 증명할 수 없으므로 매번 `authenticated smoke deferred`와 후속 수동 명령을 출력한다.
   `--skip-smoke`는 기존-host smoke뿐 아니라 이 deferred 안내도 생략한다.
10. `scripts/uninstall.sh`는 setup이 만든 링크와 자신이 추가한 설정 항목만 제거하며,
    Kanban DB와 사용자 카드는 삭제하지 않는다.

Git 추적 범위:

- **추적**: 소스, 테스트, 훅/래퍼, setup/uninstall/smoke 스크립트, 문서, 예시 설정.
- **비추적**: Kanban DB, task state/cache, 로그, 백업본, 실제 `.envrc`, 인증정보,
  Claude/Codex/Hermes 사용자 설정 전체 파일.
- setup이 설치한 대상은 가능한 한 저장소 원본을 가리키는 심볼릭 링크로 유지한다.

---

## 4. 검증

### 4.1 수용 기준 (이게 다 되면 완성)

- [ ] `project-a` 디렉터리에서 Claude Code 세션 시작 → 대시보드 project-a 보드에 카드 자동 생성
- [ ] 세션 종료 → 해당 카드가 done(또는 blocked)으로 이동
- [ ] `project-b`에서 Codex 래퍼 실행 → project-b 보드에만 카드 생성 (교차 오염 없음)
- [ ] 관찰용 카드(`*-external` assignee)를 Hermes dispatcher가 **실행하지 않음**
  (`skipped_nonspawnable` 확인)
- [ ] 지휘 방향: 대시보드에서 카드 생성 + Hermes 프로필 assignee → 워커 spawn·완료 반영
- [ ] 세 도구의 카드가 한 대시보드에서 출처 구분 가능 — tenant 태그가 카드에 표시됨
- [ ] Hermes가 Claude Code에 위임한 작업의 카드에 `hermes&claude`가 표기됨 (§3.6 체인 표기)
- [ ] tenant 필터로 출처별 카드 필터링이 동작함 (예: `hermes&codex`만 보기)

### 4.2 스모크 테스트 (`kanban-smoke.sh`)

어댑터와 Hermes 간 계약이 살아있는지 확인하는 최소 테스트. **Hermes 업데이트 직후 필수 실행.**

```
1. 테스트 보드(_smoke)에 adapter start → task_id 획득 확인
2. adapter update → 코멘트가 hermes kanban show <id>에 나타나는지 확인
3. adapter done → 상태가 done인지 확인
4. REST 백엔드 사용 시: 동일 시나리오를 REST로 반복
5. 카드 정리(archive) 후 종료코드 0
→ 어느 단계든 실패 시 종료코드 ≠ 0 + 알림 (Hermes 게이트웨이로 Telegram 통지 가능)
```

### 4.3 업데이트 절차 (핀과 결합)

```
hermes update --check          # 뒤처짐 확인만
(의도적 판단 후)
hermes update                  # 실제 업데이트
./kanban-smoke.sh              # 즉시 검증 — 실패 시 어댑터 수정 전까지 사용 보류
```

`hermes update`를 무인 자동화하지 말 것. 업데이트는 항상 스모크 테스트와 한 쌍.
→ 이 "한 쌍"을 §4.4로 자동화한다. 사람은 update 실행만 트리거하면 검증·수리는 자동.

### 4.4 자동 검증·자기수리 루프 (update-triggered)

**메커니즘** — 공식 기능 두 개의 조합. 커스텀 데몬 불필요:
1. `hermes update` 완료 시 실행 중인 게이트웨이가 **자동 재시작**됨 (네이티브 동작, 공식 문서 확인).
2. Hermes 이벤트 훅 시스템의 **`gateway:startup`** 이벤트에 훅을 걸 수 있음
   (공식 BOOT.md 튜토리얼 패턴 — startup 훅이 원샷 에이전트를 spawn해 지시 실행).

```
hermes update (또는 게이트웨이 /update)
  → 게이트웨이 자동 재시작                    [네이티브]
  → gateway:startup 훅 발화                  [네이티브]
  → 훅: 버전 스탬프 비교
      ├─ 버전 동일 (일반 재시작) → 아무것도 안 함
      └─ 버전 변경 (업데이트 직후) → kanban-smoke.sh 실행
           ├─ 통과 → 스탬프 갱신, 조용히 종료
           └─ 실패 → 원샷 수리 에이전트 spawn + 알림
```

**훅 구조** (`~/.hermes/hooks/kanban-guard/`):

```yaml
# HOOK.yaml
name: kanban-guard
description: update 후 칸반 어댑터 계약 검증 + 자기수리 트리거
events:
  - gateway:startup
```

```python
# handler.py (의사코드 — 구현 시 실제 훅 컨텍스트에 맞출 것)
async def handle(event_type: str, context: dict):
    current = get_hermes_version()          # hermes --version 또는 git HEAD 해시
    stamp   = read_file(STAMP)              # ~/.hermes/kanban-guard/last-verified
    if current == stamp:
        return                              # 업데이트 아님 — 일반 재시작
    if run("kanban-smoke.sh") == OK:
        write_file(STAMP, current)          # 검증 통과 → 스탬프 갱신
    else:
        spawn_oneshot_agent(REPAIR_PROMPT + smoke_log)   # 자기수리
        notify("kanban-guard: 스모크 실패 — 수리 에이전트 가동")
```

**수리 에이전트 프롬프트 골격** (범위 제한이 핵심):

```
역할: kanban-adapter 수리 전담.

수정 허용: ~/.hermes/kanban-adapter/ 내 파일만.
읽기 전용: Hermes 소스(~/.hermes/hermes-agent), config.yaml, 스모크 스크립트.

절차:
1. 스모크 실패 로그 분석: <로그 첨부>
2. cd ~/.hermes/hermes-agent &&
   git diff <last-verified-hash>..HEAD -- plugins/kanban/
   → 어댑터가 의존하는 인터페이스(REST 경로/페이로드, CLI 플래그, DB 스키마)의
     변경분을 특정한다. 추측 금지 — diff에 근거해서만 판단.
3. kanban-adapter의 백엔드 구현만 변경분에 맞게 패치.
4. kanban-smoke.sh 재실행:
   - 통과 → 스탬프 갱신, "수리 완료" 보고 (변경 diff 첨부)
   - 실패 → 수정 원복, "수리 실패 — 롤백 권고" 보고 후 종료 (fail-closed)

금지: Hermes 소스 수정, config 수정, 스모크 스크립트 수정,
      2회 이상 반복 수리 시도(재귀 방지), 어댑터 인터페이스(§3.3 계약) 변경.
```

**필수 주의사항**:

1. **gateway 훅 등록 검증** — `~/.hermes/hooks/<name>/HOOK.yaml + handler.py` 방식은
   Python gateway 훅이며 shell-hook allowlist/exec bit 및 `hermes hooks doctor`의 대상이 아니다
   (§6-9 실측). 생성 후 게이트웨이를 재시작하고 `~/.hermes/logs/gateway.log`의
   `Loaded hook` 로그와 실제 probe 산출물로 등록·발화를 검증한다.
2. **훅은 사용자 전체 권한으로 실행** — cron 항목과 동일한 신뢰 경계.
   직접 작성한 스크립트만, `~/.hermes/hooks/` 안에 보관.
3. **완전 무인 update는 여전히 금지** — cron은 `hermes update --check`
   (파일 무변경, 스크립트/cron 게이트 용도로 공식 안내)로 "업데이트 있음" 알림까지만.
   update 실행은 사람이 트리거. 본 루프는 "실행 후"를 자동화하는 것.
4. **최후 방어선** — 수리 실패 시: 업데이트 전 자동 저장되는 스냅샷 복원 또는
   git 롤백. 롤백 후 `hermes config check`로 config 비호환 정리 (공식 절차).

---

## 5. 방어책

커스텀 배선의 취약점("Hermes가 바뀌면 깨진다")에 대한 4겹 방어. **대체재가 아니라 보완재** —
1·2는 깨질 확률을 낮추고, 3은 그래도 깨졌을 때 즉시 알게 하고, 4는 커스텀의 수명을 관리한다.

| # | 방어책 | 막는 것 | 구현 위치 |
|---|---|---|---|
| 1 | **어댑터 계층** — 훅은 어댑터만 알고, Hermes는 어댑터 내부만 안다 | 변경 파급 지점 3곳→1곳 | §3.3 |
| 2 | **CLI 기본 + 계약 스모크** — 현행 REST 인증을 우회하지 않고 CLI를 쓰되, 모든 CLI 의존을 어댑터 한 곳에 격리하고 업데이트 직후 스모크로 파손 감지 | 인증 우회 방지 + CLI 변경의 조기 탐지 | §3.3 내부 백엔드, §4.2 |
| 3 | **버전 핀 + 스모크 테스트 + 자기수리** — 의도적 업데이트 + 직후 자동 검증·수리 (§4.4 루프로 자동화) | **조용한 기록 유실** (목표 "모든 과정 기록"의 최대 적) | §4.2~§4.4 |
| 4 | **공식 경로 이관 감시** — 어댑터 내부를 나중에 공식 인터페이스로 교체 | 커스텀 영구화 | 아래 |

### 5.4 이관 감시 대상

다음이 머지/출시되면 어댑터 **내부만** 교체 (훅·래퍼는 무변경):

- **#18629**: `kanban create --acp-command` 네이티브 지원 → 지휘 방향의 프로필 래퍼 제거 가능
- **#5257**: generalized ACP client (`claude-acp`/`codex-acp` provider) → 지휘 방향 단순화
- **worker-lanes plugin spawn_fn**: 비-Hermes 레인 플러그인이 정식화되면 관찰 방향을 정식 레인으로 승격
- **amanning3390 네이티브 칸반 대시보드 PR**: 대시보드 강화

분기별 1회 위 이슈들의 상태 확인 (Hermes 릴리스 노트 + 이슈 트래커).

---

## 6. ⚠️ 구현 전 반드시 로컬에서 확인할 것 (역제안)

문서 조사만으로 확정 못 한 항목들. **Hermes Agent가 구현 착수 시 가장 먼저 수행할 체크리스트.**

> **Phase 0 실측 기준**: 2026-07-15, macOS 26.5.2,
> Hermes Agent v0.18.2 (2026.7.7.2, upstream `6997dc81`),
> Claude Code 2.1.201. Codex CLI는 로컬에 설치되어 있지 않음.

1. **칸반 REST의 카드 생성 엔드포인트**
   - 문서에서 확인된 건 `PATCH /tasks/:id`와 WebSocket뿐. POST(생성) 존재 여부·페이로드는
     `plugins/kanban/dashboard/plugin_api.py` 소스를 직접 읽어 확정.
   - 없으면 어댑터 1차 백엔드를 CLI로 전환 (설계상 이미 폴백 준비됨).
   - → 확인됨: `POST /api/plugins/kanban/tasks?board=<slug>`가 존재한다
     (`plugin_api.py`의 `CreateTaskBody`/`create_task`). 페이로드는 `title`, `body`,
     `assignee`, `tenant`, `priority`, `workspace_kind`, `workspace_path`, `parents`,
     `triage`, `idempotency_key`, `max_runtime_seconds`, `skills`, `goal_mode`,
     `goal_max_turns`를 받는다. 단, 현행 HTTP API는 세션 토큰/쿠키 인증이 필수이므로
     무인 어댑터의 1차 백엔드는 **CLI**로 확정한다. REST는 안정적인 서비스 토큰 경로를
     별도로 마련한 뒤 선택 백엔드로만 사용한다.

2. **Claude Code 훅의 최신 스펙**
   - 훅 이벤트명, settings.json 형식, 훅에 전달되는 환경변수/페이로드.
   - 공식 문서(code.claude.com/docs) 기준으로 확정. 조사 시점 이후 변경 가능성 높음.
   - → 확인됨: 로컬 Claude Code는 2.1.201. 훅은 `settings.json`의
     `hooks.<Event>[].hooks[]` 구조이며 command 훅은 JSON을 stdin으로 받는다.
     공통 필드는 `session_id`, `transcript_path`, `cwd`, `permission_mode`,
     `hook_event_name` 등이다. 세션 시작은 `SessionStart`(`source` 및 선택적 `model`,
     `agent_type`, `session_title`), 실제 세션 종료/정리는 `SessionEnd`(`reason`)이다.
     `Stop`은 **세션 종료가 아니라 턴 종료**이며, 본 구현은 카드 단위도 사용자 턴으로
     정했으므로 `last_assistant_message`를 결과로 해당 카드를 완료한다.
     상태 파일은 문서에 없는 `$CLAUDE_SESSION_DIR` 대신 `session_id`를 키로 관리한다.

3. **Codex CLI의 훅/이벤트 지원 여부**
   - 네이티브 훅과 세션 종료 이벤트 지원 여부를 `codex --help` 및 공식 문서에서 확인.
     훅이 있어도 세션 종료/종료코드 계약이 없으면 래퍼를 유지.
   - → 확인됨(부분): 로컬에는 Codex CLI가 설치되어 있지 않아 로컬 실행 실측은 불가했다.
     최신 공식 문서에는 네이티브 훅이 있으며 `~/.codex/hooks.json`,
     `~/.codex/config.toml`, repo의 `.codex/`에서 `SessionStart`, `Stop` 등을 설정한다.
     그러나 문서화된 이벤트 목록에는 `SessionEnd`가 없고 `Stop`은 턴 단위다. 따라서
     "훅 존재 = 래퍼 불필요"는 성립하지 않는다. 바운드 `codex exec`의 종료코드 기반
     done/block 처리를 위해 래퍼를 유지하고, 설치 후 네이티브 훅은 보조 기록에 사용한다.

4. **`hermes kanban create`의 현행 플래그**
   - `hermes kanban create --help` 실행. 문서 기준 플래그:
     `--body --assignee --parent --workspace --branch --project --tenant --priority --triage
      --idempotency-key --max-runtime --created-by --skill --max-retries --goal
      --goal-max-turns --initial-status --json`. `--board`는 상위 명령 플래그.
   - 특히 **초기 상태를 running으로 만드는 방법**(관찰용 카드에 필요)이 있는지 확인.
     없으면 create 직후 상태 전이 호출을 어댑터에 추가.
   - → 확인됨: `--board`는 `create` 하위 명령 플래그가 아니라
     `hermes kanban --board <slug> create ...` 형태의 상위 플래그다. 현행 create에는
     기존 목록 외에 `--branch`, `--project`, `--created-by`, `--goal`,
     `--goal-max-turns`, `--initial-status {blocked,running}`, `--json`이 있다.
     관찰 카드는 `--initial-status running`으로 직접 생성할 수 있다.

5. **`skipped_nonspawnable` 동작 실증**
   - 존재하지 않는 assignee로 카드를 만들어 dispatcher가 정말 건드리지 않는지 실험.
     (문서상 동작이지만, 관찰 방향 전체가 이 동작에 의존하므로 실증 필수)
   - → 확인됨(동작), 불일치(이벤트): 임시 보드 `phase0-measure`에서 존재하지 않는
     assignee `nonexistent-external-probe`로 ready 카드를 만들고 dispatch한 결과
     `Spawned: 0`, `Skipped (non-spawnable assignee — terminal lane, OK)`가 출력되며
     카드는 ready에 남았다. 다만 카드 `task_events`에는 `skipped_nonspawnable` 이벤트가
     기록되지 않았다(생성 이벤트만 존재). 따라서 **미실행 보장에는 의존 가능하지만,
     해당 이벤트의 영속 기록에는 의존하지 않는다**. 관찰 카드는 추가로
     `--initial-status running`을 사용해 dispatcher 입력 자체에서 격리한다.

6. **대시보드 REST의 보안 경계**
   - `/api/plugins/`의 HTTP/WS 인증 적용 여부를 소스와 실제 요청으로 확인.
     대시보드를 localhost 밖에 노출할 때는 내장 인증을 유지하고 필요시 Tailscale/VPN 경유.
   - → 확인됨(명세와 불일치): 현행 `auth_middleware`는 public allowlist를 제외한
     **모든 `/api/` 경로**에 인증을 요구하며 `/api/plugins/` 예외가 없다. loopback에서도
     SPA가 주입받는 임시 `_SESSION_TOKEN`을 `X-Hermes-Session-Token` 또는 Bearer로
     보내야 한다. 비-loopback은 OAuth/비밀번호 세션 게이트가 강제된다. 따라서 REST를
     "localhost 무인 무인증"으로 호출하는 설계는 폐기하고 CLI를 기본 백엔드로 쓴다.

7. **(칸반 외) Codex Pro의 GPT-5.5 포함 여부**
   - 이전 논의의 PRD→코딩→크로스리뷰 파이프라인을 이 칸반 위에 얹을 계획이라면,
     `codex debug models`와 대화형 `/model`로 GPT-5.5 접근 가능 여부를 먼저 확인.
   - → 확인됨(부분/차단): 로컬 Codex CLI 미설치로 구독 계정의 실제 접근 여부는
     확인하지 못했다. 최신 공식 CLI 문서에는 `codex models`가 아니라
     `codex debug models`가 모델 카탈로그 확인 명령으로 기재되어 있고, 대화형 `/model`
     설명에는 GPT-5.5가 예시로 등장한다. **로컬 설치·로그인 후 접근 실측 전까지
     Phase 5 착수 금지**.

8. **tenant 사후 변경 가능 여부** (§3.6 엣지 케이스에 필요)
   - `hermes kanban` CLI 또는 REST PATCH로 기존 카드의 tenant를 수정할 수 있는지 확인.
   - 가능: 실행 중 위임 시 태그를 `hermes` → `hermes&codex`로 갱신.
   - 불가: comment 구조화 기록(`executors: ...`)을 단일 진실로 삼고 태그는 생성 시점 유지.
   - → 확인됨: 현행 CLI의 `edit`/`assign`에는 tenant 변경 옵션이 없고 REST
     `UpdateTaskBody`에도 tenant가 없다. tenant는 지원 표면에서 사후 변경 **불가**.
     실행 중 동적 위임은 `executors: hermes,codex` 형식의 구조화 comment를 단일 진실로
     삼고, 카드 tenant는 생성 시점 값을 유지한다.

9. **`gateway:startup` 훅 실측** (§4.4 전제)
   - 정확한 이벤트명과 handler에 전달되는 context 형식을 훅 문서/소스에서 확인.
   - Python gateway 훅 등록 후 gateway 로그와 probe 산출물로 로드·발화를 확인.
     (`hermes hooks doctor`는 별도 shell-hook 시스템용이므로 사용하지 않음.)
   - 게이트웨이 재시작으로 훅 발화를 실측한 뒤에 §4.4를 신뢰할 것.
   - → 확인됨(동작), 불일치(검증 절차): 임시 Python gateway 훅을 등록하고 launchd
     게이트웨이를 재시작한 결과 `gateway:startup`이 1회 발화했고 context는
     `{"platforms": ["telegram"]}`였다. 단, `~/.hermes/hooks/<name>/HOOK.yaml + handler.py`
     방식의 **Python gateway 훅**과 `hermes hooks doctor`가 검사하는 `config.yaml` 기반
     **shell hook**은 서로 다른 시스템이다. Python gateway 훅에는 exec bit/allowlist가
     적용되지 않으며 `hermes hooks doctor`는 `No shell hooks configured`를 출력했다.
     Phase 4 검증은 게이트웨이 로그 + 실제 재시작 프로브로 수행한다.

10. **게이트웨이 상시 실행 전제** (§2.1 dispatcher + §4.4 훅의 공통 전제)
    - dispatcher(`dispatch_in_gateway`)와 startup 훅 모두 게이트웨이가 돌아야 성립.
    - systemd(Linux)/launchd(macOS) 서비스로 등록해 상시 실행 보장.
      update 시 서비스 매니저 경유 자동 재시작도 이 경로가 가장 확실함.
    - → 확인됨: `~/Library/LaunchAgents/ai.hermes.gateway.plist`가 설치되어 있고
      `RunAtLoad=true`, `KeepAlive=true`다. `launchctl`에서 `state=running`으로 확인했으며
      현재 gateway는 launchd 감독 하에 실행 중이다. `config.yaml`의
      `kanban.dispatch_in_gateway: true`도 확인했다. 별도 서비스 등록 작업은 불필요하다.

### 추가 권고 (요청엔 없었지만 필요한 것)

- **백업**: `~/.hermes/kanban/` 전체를 주기 백업. 보드가 "모든 작업의 기록"이 되는 순간
  그 자체가 유실되면 안 되는 데이터가 됨. (`hermes backup` 또는 cron + rsync)
- **카드 폭증 관리**: 관찰 방향은 세션마다 카드를 만들므로 빠르게 쌓임.
  아카이브 정책(예: done 후 7일 경과 시 archive)을 어댑터나 cron에 포함 권장.
- **작업 단위 정의**: "Claude Code 세션 1개 = 카드 1개"가 기본이지만, 긴 세션에서
  여러 작업을 하면 카드가 뭉뚱그려짐. 필요시 훅 대신 슬래시 커맨드(`/kanban-task <제목>`)로
  카드 경계를 수동 제어하는 옵션을 어댑터에 추가.
- **크로스리뷰 파이프라인과의 결합**: 이전 논의(PRD=Fable 5 → 코딩=GPT-5.5 →
  리뷰=Opus 4.8+GPT-5.5)는 이 칸반의 **지휘 방향** 위에 자연스럽게 얹힘 —
  단계별 카드 + parent 링크로 의존성 표현. 칸반이 안정화된 뒤 2단계로 진행 권장.

---

## 7. 구현 순서 (Hermes Agent 실행 지침)

**원칙**: 각 Phase는 이전 Phase의 검증 통과 후에만 진행. Phase 0의 실측 결과는
이 문서의 해당 섹션에 직접 기록해 문서를 living document로 유지한다.

### Phase 0 — 실측 (코드 작성 전, §6 체크리스트 전체)
- §6-1~10을 순서대로 실측하고 결과를 §6 각 항목 아래에 `→ 확인됨: ...` 형식으로 기록.
- 특히 §6-1(REST POST 존재 여부)의 결과가 어댑터 1차 백엔드를 결정하고,
  §6-5(non-spawnable 미실행)의 결과가 관찰 방향 설계 전체를 확정한다.
- 게이트웨이를 서비스(systemd/launchd)로 등록 (§6-10). 현 로컬은 launchd 등록·실행 확인 완료.
- **게이트**: §6-1~10 기록 완료 + 본문 불일치 수정 완료. Codex 관련 §6-3/7은
  로컬 CLI 미설치로 부분 확인 상태이며, Phase 2의 Codex 단계와 Phase 5만 차단한다.

### Phase 1 — 기반
1. 독립 Git 저장소 생성 + §3.7 디렉터리 구조와 `.gitignore` 확정
2. `scripts/setup.sh`/`uninstall.sh`의 idempotent 설치 골격 구현 및 dry-run 검증
3. Dashboard의 board `default_workdir`를 이용한 현재 디렉터리 자동 라우팅 (§3.5)
4. `kanban-adapter` 구현 (§3.3) — Phase 0 결과에 따라 **CLI 백엔드**로 구현
5. `scripts/kanban-smoke.sh` 구현 (§4.2) + setup을 통한 실제 설치 후 통과 확인
- **게이트**: 구현 파일이 저장소 밖에 복제되지 않음, setup 2회 연속 성공,
  uninstall→setup 재설치 성공, 스모크 테스트 전 항목 통과.

### Phase 2 — 관찰 방향
1. Claude Code 훅 배선 (§3.4-②, task_id 전달 포함)
2. 수용 기준 검증: 카드 자동 생성/완료, 보드 라우팅, dispatcher 미개입 (§4.1)
3. 통과 후 Codex CLI 설치·로그인 실측을 먼저 통과한 뒤 Codex 래퍼 (§3.4-③) 동일 절차
- **게이트**: §4.1의 관찰 방향 항목 전체 체크.

### Phase 3 — 지휘 방향 + 출처 표기
1. `claude -p` / `codex exec` 위임 래퍼 프로필 생성 (§3.4-①)
2. tenant 기반 provenance 배선 (§3.6) — `hermes&claude` 체인 표기 확인
- **게이트**: §4.1의 지휘·출처 항목 전체 체크.

### Phase 4 — 가드
1. `kanban-guard` Python gateway 훅 구현 (§4.4) + gateway 로그/실제 probe로 등록 확인
2. 게이트웨이 재시작으로 훅 발화 실측 → 의도적으로 스모크를 깨뜨려 수리 루프 시험
3. `~/.hermes/kanban/` 백업 cron + `hermes update --check` 알림 cron 등록
- **게이트**: 모의 파손 → 자동 감지 → 수리(또는 fail-closed 알림)까지 1회 완주.

### Phase 5 — (선택, 안정화 후) 크로스리뷰 파이프라인 결합
- PRD → 코딩 → 크로스리뷰를 지휘 방향 카드 + parent 링크로 구성.
- 착수 전 `codex debug models`와 대화형 `/model`로 GPT-5.5 실제 계정 가용 확인 (§6-7).

---

## 부록 A — Hermes Agent 턴 관찰 통합 (2026-07-29)

standalone `hermes-kanban` 저장소가 본 저장소로 흡수되었다. 관찰 방향의 세 번째
소스인 **Hermes Agent 자체의 사용자 턴**도 동일한 대시보드 보드에 기록된다.

- **플러그인**: `integrations/hermes/hermes-kanban/` (플러그인 ID `hermes-kanban` 유지).
  setup이 `~/.hermes/plugins/hermes-kanban` 심링크를 설치·enable 한다.
  기존 standalone 심링크(`…/hermes-kanban/plugin/hermes-kanban`)는 자동 마이그레이션,
  그 외의 경로/파일은 거부한다.
- **수명주기**: `src/kanban_adapter/hermes_hook.py`의 `TurnTracker`.
  `pre_llm_call` → running 카드 생성(tenant=`hermes`, created-by=`hermes-agent`,
  턴별 idempotency key, 제목 정규화 120자), `on_session_end` → 동일 카드 complete.
  턴 상태는 `${XDG_CACHE_HOME:-~/.cache}/unified-kanban/hermes-turns`에
  원자적(0600, non-symlink 검증)으로 보관하며, 훅은 fail-open이다.
- **보조 실행 제외**: 자동 delegation 완료, background process, context compaction,
  todo 복원 알림은 사용자 작업이 아니므로 카드를 생성하지 않는다.
  `HERMES_DELEGATED_CHILD_CONTEXT`의 subagent와 `HERMES_KANBAN_TASK`의 Kanban worker도
  부모 작업 또는 기존 Kanban 카드의 내부 실행이므로 중복 observation을 만들지 않는다.
  같은 문자열을 문장 중간에서 언급하는 실제 사용자 prompt는 정상적으로 기록한다.
- **보드 라우팅**: 전용 보드를 만들지 않는다. Claude/Codex와 동일하게
  `kanban_adapter.backend.HermesCliBackend.resolve_board`의
  대시보드 Project directory(=`default_workdir`) 조상 매칭을 재사용한다.
- **업데이트/호환**: `patches/hermes-agent-carried-commits`와
  `scripts/update-hermes-if-needed.sh`가 이관됨. setup은 어떤 쓰기 작업 전에
  `patches/hermes-agent-supported-upstream`의 frozen full SHA를 fixed official HTTPS
  저장소에서 exact object로 가져와 identity를 확인하고 carried commit chain과 bundle을
  검증한다. pin 경로는 환경변수로 대체할 수 없고, no-follow descriptor open과
  lstat/fstat inode 일치 검사로 읽는다. pin mismatch 또는 malformed pin이면 어떤 설치
  write도 하지 않고 중단한다. updater는 checkout을 변경하지 않는다. 이후 공식 `main` 이동은
  다음 maintenance cycle로 넘기며, 검증된 release가 이미 선택돼 있으면
  release 구성과 서비스 재시작을 모두 건너뛴다. 선택이 달라야 할 때만 불변 release를
  `<HERMES_AGENT_REPO>.releases/release-<carried>`에 구성하거나 정확히 재검증한 뒤 path
  transaction 안에서 selector 하나를 교체하고 원래 실행 중이던 서비스만 재시작한다. 실패
  시 이전 selector를 복원하고 foreign successor는 보존한다. Hermes Agent 체크아웃이 없으면
  기본적으로 호환성을 검증할 수 없으므로 setup은 Claude/Codex를 포함한 모든 설치 write 전에
  중단한다. 유일한 예외는 checkout·Hermes data home·release root가 모두 안전하게 absent이고
  `foreign Hermes` command가 없으며 dry-run이 아닌 `genuine empty supported macOS per-user host`다.
  이 경우에만 reviewed manifest의 frozen official installer를 exact digest·argv·environment로 실행하고
  authenticated receipt와 managed toolchain을 검증한 뒤 setup을 계속한다. `dry-run does not bootstrap`:
  empty host dry-run, foreign/existing/dirty/newer/partial checkout, unsafe ancestry 또는 receipt authority
  불일치는 모두 어떤 bootstrap/install write보다 먼저 fail closed한다. 이미
  로드된 Hermes plugin도 등록 시점과 각 새 turn 시작에서 pin과 selector가 가리키는
  release(=`release-<carried manifest 최종 commit>`), 그 release의 실행 파일과 자기 디렉터리
  identity에 묶인 completion receipt, `hermes version`이 보고한 upstream을 다시 검사하고, 설치된
  Claude/Codex hook과 `kanban-adapter` wrapper 및 module CLI도 매 실행 전에 검사한다. checkout은
  어떤 흐름에서도 carried tip으로 이동하지 않는 읽기 전용 입력이므로 checkout `HEAD`는 gate의
  판단 근거가 아니다. direct
  `hermes update` 이후 mismatch 상태에서는 어느 진입점도 새 Kanban 기록을 만들지 않는다.
  외부 Claude/Codex 수명주기는 hook exit 0으로 계속 진행하고, 수동 adapter 호출은 exit 1로
  거부해 차단 사실을 숨기지 않는다. wheel은 repository policy 파일과 installer를 포함하지
  않는 library artifact이므로 mutation console script를 공개하지 않는다.
- **제거**: `scripts/uninstall.sh`는 본 저장소 소유의 플러그인 심링크만
  disable/삭제하고 보드·카드 데이터는 보존한다. 외부 심링크/경로는 거부한다.
