# Unified Kanban

[English](README.en.md) | 한국어

Hermes Agent, Claude Code, Codex의 작업을 하나의 Hermes Kanban에 기록하는 저장소 내장형 통합 계층입니다.
구현 파일은 이 Git 저장소에만 두고, 설치 과정에서는 관리형 심볼릭 링크와 로컬 상태만 생성합니다.

---

## 목차

- [프로젝트 소개](#프로젝트-소개)
- [해결한 문제](#해결한-문제)
- [기본 Hermes Kanban과의 차이](#기본-hermes-kanban과의-차이)
- [아키텍처와 데이터 흐름](#아키텍처와-데이터-흐름)
- [기술적 난제와 해결](#기술적-난제와-해결)
- [구체적인 장점](#구체적인-장점)
- [검증 근거](#검증-근거)
- [설치](#설치)
  - [1. 지원 환경과 준비물](#1-지원-환경과-준비물)
  - [2. Hermes Agent 설치 및 초기 설정](#2-hermes-agent-설치-및-초기-설정)
  - [3. Unified Kanban clone](#3-unified-kanban-clone)
  - [4. 선택 사항: 비파괴 사전 점검](#4-선택-사항-비파괴-사전-점검)
  - [5. 설치](#5-설치)
  - [6. 설치 옵션](#6-설치-옵션)
  - [7. Dashboard와 프로젝트 연결](#7-dashboard와-프로젝트-연결)
  - [8. 클라이언트 재시작 및 설치 확인](#8-클라이언트-재시작-및-설치-확인)
  - [9. 자주 발생하는 설치 오류](#9-자주-발생하는-설치-오류)
- [카드별 실행 정보](#카드별-실행-정보)
  - [도구 사용량](#도구-사용량)
  - [토큰 사용량](#토큰-사용량)
- [개인정보와 멱등성](#개인정보와-멱등성)
- [업데이트와 호환성](#업데이트와-호환성)
- [사용](#사용)
  - [Claude, Codex, Hermes 세션 읽기](#claude-codex-hermes-세션-읽기)
- [디렉터리 구조와 코드 관리](#디렉터리-구조와-코드-관리)
- [테스트와 프로젝트 유지관리](#테스트와-프로젝트-유지관리)
- [기여, 보안, 라이선스](#기여-보안-라이선스)
- [제거](#제거)
- [검증](#검증)

---

## 프로젝트 소개

세 개의 코딩 에이전트(Hermes Agent, Claude Code, Codex CLI)를 오가며 일할 때, "무엇을 언제 어디서 시켰고 그 결과가 무엇이었는지"는 각 도구의 로컬 세션 파일에 흩어져 남습니다. 이 프로젝트는 세 도구의 **실제 사용자 턴**을 각 런타임의 공식 훅으로 관측해 하나의 Hermes Kanban 보드에 카드로 기록하는 통합 계층입니다.

- **추가 범위**: 외부 도구 → 보드 관찰 방향. Claude Code와 Codex에서 이미 실행된 턴을 기록하며, 기존 Hermes Kanban의 native task dispatch는 그대로 유지합니다.
- **카드 단위**: 세션이 아니라 사용자 프롬프트 1회 = 카드 1장. 긴 세션이 하나의 뭉뚱그린 카드가 되지 않습니다.
- **경계**: 프롬프트 원문, 도구 인자, 중간 transcript는 카드에 저장하지 않습니다. 카드에 남는 프롬프트 파생 텍스트는 120자 제목 하나뿐입니다.
- **배포 형태**: 구현은 이 저장소 안에만 존재하고, 설치는 `~/.local/bin`, `~/.claude`, `~/.codex`, `~/.hermes`에 저장소를 가리키는 심볼릭 링크와 병합된 훅 항목만 만듭니다.

구성: Python 3.11 CLI 어댑터 + Claude/Codex 훅 실행 파일 + Hermes Agent 플러그인 + Bash 설치·업데이트·스모크 스크립트. 핵심 통합 계층은 전체 자동화 suite로 검증하며, 줄 수처럼 쉽게 낡는 지표보다 module 책임과 실제 검증 범위를 문서화합니다.

## 해결한 문제

| 문제 | 이 저장소의 해결 |
| --- | --- |
| 세 도구의 작업 기록이 서로 다른 로컬 포맷에 흩어짐 | 세 런타임의 공식 훅에서 turn 단위로 같은 보드 DB에 카드 생성·완료 |
| 어떤 도구가 실행했는지 카드만 봐서는 알 수 없음 | 생성 시점에 `tenant`(`claude`/`codex`/`hermes`)로 출처 확정, Dashboard tenant 필터로 분리 |
| 프로젝트마다 보드를 지정하는 설정 파일·환경변수 관리 부담 | Dashboard의 Project directory(`default_workdir`)와 현재 디렉터리를 최장 prefix로 매칭해 보드를 자동 선택 |
| "이 턴에서 무엇을 얼마나 썼는가"를 사후에 알 수 없음 | Skill/서브에이전트/MCP 호출과 토큰 buckets를 카드당 구조화 코멘트 1개로 기록 |
| 훅은 crash·retry·resume이 흔한데 그때마다 카드/코멘트가 중복됨 | 카드에서 결정론적으로 파생한 `event_id`를 마커로 삼아 append 전에 카드를 재확인 |
| 에이전트 최종 응답이 길어 카드가 읽히지 않거나 실행이 실패함 | 원문은 `result`, 최대 1,000자 구조 요약은 `summary`로 분리하고, 큰 원문은 argv 대신 0600 파일로 전달 |
| Hermes 업데이트가 local carried commit을 되돌려 조용히 기록이 끊김 | 설치 전 upstream SHA·carried commit 적용 여부를 검사하고 불일치 시 write 전에 중단 |

## 기본 Hermes Kanban과의 차이

기본 Hermes Kanban은 Hermes 내부의 task와 dispatcher를 중심으로 동작합니다. 카드는 Hermes가 실행할 작업이고, dispatcher가 assignee 프로필을 spawn해 처리합니다. 이 프로젝트는 그 위에 **외부 에이전트의 실제 사용자 턴을 관측해 기록하는 레인**을 추가합니다.

| 항목 | 기본 Hermes Kanban | Unified Kanban |
| --- | --- | --- |
| 카드의 의미 | dispatcher가 실행할 Hermes task | 이미 일어난 사용자 턴의 observation 카드 (`--observation`, `initial-status running`) |
| 기록 대상 | Hermes 내부 워커 | Claude Code, Codex CLI, Hermes Agent의 실제 사용자 턴 |
| dispatcher 관계 | 카드가 dispatcher의 입력 | observation 카드는 spawn 불가 assignee(`claude-code-external` 등) + running 상태로 dispatcher 입력에서 격리 |
| 결과 표시 | 카드 결과 필드 | 원문(`result`)과 최대 1,000자 구조 요약(`summary`) 분리, Dashboard는 요약을 먼저 보여주고 클릭 시 원문 펼침 |
| 긴 결과 | argv 경유 | 16,000바이트 초과 시 0600 임시 파일 + `--result-file`로 전달해 OS 명령행 한도 회피 |
| 재시도 | — | 카드 파생 `event_id` 마커 + 완료 전까지 0600 로컬 상태 보존으로 crash 후 재시도가 중복을 만들지 않음 |
| 토큰 정보 | — | input/output/cache read/cache write/reasoning/requests bucket을 분리 보존, 제공되지 않은 bucket은 `0`이 아니라 `null`/미추적 |
| 집계 범위 | — | archived 카드를 포함한 보드 all-time 누적 합계 + coverage(추적된 카드 비율) |
| 설치 | Hermes 자체 설치 | repository-contained setup/update — 구현은 저장소에만 두고 심볼릭 링크로 설치, Hermes 호환성 검증 후에만 write |

Dashboard 쪽 토큰 집계·badge·상세 UI와 observation 카드 지원은 Hermes Agent 체크아웃에 얹는 carried commit으로 제공되며, 목록과 재적용 절차는 `patches/hermes-agent-carried-commits`와 `scripts/update-hermes-if-needed.sh`에 있습니다.

## 아키텍처와 데이터 흐름

```text
 Claude Code            Codex CLI              Hermes Agent
 (settings.json)        (hooks.json)           (plugin: hermes-kanban)
 UserPromptSubmit       UserPromptSubmit       pre_llm_call
 PostToolUse            PostToolUse            post_tool_call
 Stop / SessionEnd      SubagentStart          subagent_start
        │               Stop / SessionEnd      post_llm_call / post_api_request
        │                      │               on_session_end
        │                      │                      │
        ▼                      ▼                      ▼
 claude-kanban-hook     codex-kanban-hook       TurnTracker
        └──────────┬───────────┘                      │
                   ▼                                  │
        ~/.cache/kanban-adapter/<kind>/        ~/.cache/unified-kanban/
        <sha256(session)>.json (0600)          hermes-turns/<sha256>.json (0600)
                   │                                  │
                   ▼                                  ▼
              kanban-adapter  ──────────────►  hermes kanban CLI
              (start/update/done/block)        (create/comment/complete/block)
                                                      │
                                                      ▼
                                       ~/.hermes/kanban/boards/<slug>/kanban.db
                                                      │  WebSocket (task_events)
                                                      ▼
                                              hermes dashboard (Kanban)
```

턴 하나의 데이터 흐름:

1. **시작** — 사용자 프롬프트 훅이 현재 디렉터리를 받아 `kanban-adapter start`를 호출합니다. 어댑터는 `hermes kanban boards list --json`의 `default_workdir` 중 현재 디렉터리를 포함하는 가장 구체적인 보드를 고르고, running observation 카드를 만들어 task id를 돌려줍니다.
2. **상태 보관** — task id, cwd, 모델, 토큰 baseline을 세션 키 해시 파일(0600, 심볼릭 링크 거부, atomic replace)에 기록합니다. 훅 간 전달 경로는 이 파일 하나입니다.
3. **누적** — 도구/서브에이전트 훅이 도착할 때마다 sanitize한 이름의 카운터만 상태 파일에 증가시킵니다. Hermes는 `post_api_request`의 canonical usage를, Claude/Codex는 종료 시점 transcript snapshot의 delta를 씁니다.
4. **완료** — 턴 종료 훅이 사용량 코멘트 1개를 멱등 append한 뒤 카드를 완료합니다. 원문은 `--result`(또는 큰 경우 `--result-file`), 요약은 `--summary`로 나눠 전달하고, 성공 후에만 상태·임시 파일을 지웁니다.

보드 해석 우선순위는 명시적 `--board` → Dashboard `default_workdir` 최장 prefix 매칭 → `HERMES_KANBAN_BOARD` 폴백입니다. 같은 깊이의 보드가 둘 이상 일치하면 임의 선택하지 않고 설정 오류로 실패합니다.

## 기술적 난제와 해결

**1. 훅은 언제든 중복 실행된다.** 훅 프로세스가 코멘트를 올린 직후 상태를 기록하기 전에 죽으면, 재시도가 같은 코멘트를 한 번 더 올립니다. 해결은 `sha256(source, task_id)`에서 파생한 `event_id`를 코멘트 본문에 넣고, append 전에 `hermes kanban show --json` 결과를 스캔해 같은 마커가 있는지 확인하는 것입니다. id가 프로세스가 아니라 카드에서 파생되므로 재시도가 같은 값을 다시 계산합니다. 마커 스캔은 스키마를 가정하지 않고 디코드된 카드 전체를 순회하므로 Hermes가 코멘트 위치를 바꿔도 중복 탐지가 조용히 꺼지지 않고, 토큰 경계까지 정확히 일치할 때만 매치합니다.

**2. 최종 응답은 카드에 넣기엔 길고, 버리기엔 유일한 결과다.** 원문을 그대로 `--result`에 넣으면 긴 응답에서 OS의 명령행 크기 한도에 걸립니다. 요약만 남기면 정보가 사라집니다. 그래서 원문은 `result`, 최대 1,000자 요약은 `summary`로 저장하고 Dashboard가 요약을 먼저 보여주도록 나눴습니다. 16,000바이트를 넘는 원문은 0600 임시 파일에 쓰고 `--result-file`로 넘깁니다. 요약 생성은 추가 모델 호출 없이 fenced code block을 제외한 뒤 완료/변경/검증/미완료 섹션을 우선 인식하고, 섹션이 없으면 키워드 기반 폴백으로 분류합니다. 1,000자는 섹션별로 길이 비례 배분하되 남는 자리는 미완료 → 검증 → 완료 → 변경 순으로 줍니다.

Observation 카드는 dispatcher가 실행한 run이 아니므로 의도적으로 `task_runs` 행이 없습니다. 이 경우 bounded summary를 `completed`/`edited` 이벤트의 `full_summary`에 보존하고, Dashboard 조회는 run summary가 없을 때만 해당 이벤트를 fallback으로 사용합니다. run-backed task에서는 기존 run summary가 계속 우선합니다. 이 구분 덕분에 외부 턴을 Hermes run으로 가장하지 않으면서도 원문과 요약을 분리해 표시할 수 있습니다.

**3. 토큰을 "합치면" 틀린다.** provider마다 bucket 의미가 다릅니다. Codex는 `input_tokens`에 캐시 읽기를 포함하고 cache write bucket이 없으며, Claude는 별도 reasoning bucket을 제공하지 않습니다. 그래서 provider가 신뢰 가능한 `total`을 주면 그 값을 보존하고, 없을 때만 의미가 명확한 bucket으로 계산합니다. cache와 reasoning은 이미 total에 포함될 수 있으므로 무조건 더하지 않습니다. 제공되지 않은 bucket은 `0`이 아니라 `null`로 남기고, 토큰 코멘트가 없는 과거 카드는 미추적으로 표시해 coverage를 100% 미만으로 보고합니다. 값은 세션 누적 카운터의 시작·종료 snapshot delta로 구해 이전 턴을 다시 세지 않습니다.

**4. Codex 훅은 모델 이름을 항상 주지 않는다.** SessionEnd에는 `model`이 없습니다. `config.toml`의 머신 전역 기본값을 쓰면 그럴듯하지만 그 세션의 모델이라는 보장이 없습니다. 그래서 유일한 폴백을 로컬 state SQLite의 **해당 세션 행**으로 한정했습니다. DB는 `mode=ro` URI로 열고, 경로는 연결 전후로 심볼릭 링크가 아닌 일반 파일인지 확인하며(중간 교체 시 실패), 조회는 바인드 파라미터와 `SELECT model` 단일 컬럼 projection입니다. 읽을 수 없으면 추정하지 않고 모델을 비웁니다. 같은 방식으로 rollout 경로도 복구해 토큰 snapshot에 씁니다.

**5. 관측 카드가 dispatcher에 잡히면 이미 한 일을 Hermes가 다시 한다.** 실측에서 spawn 불가 assignee 카드는 실행되지 않지만 `skipped_nonspawnable` 이벤트가 DB에 남지 않았습니다. 그래서 이벤트 존재를 감사 근거로 쓰지 않고, 격리를 이중으로 겁니다 — `claude-code-external`/`codex-external` assignee와 `--observation` + running 초기 상태입니다. 또한 `create --json` 응답이 running observation 카드가 아니면 실패로 처리합니다. 멱등 create가 기존 행을 돌려줄 수 있어, 소유권을 증명할 수 없는 카드를 보상 삼아 완료시키면 남의 실행 중인 턴을 끝낼 수 있기 때문입니다.

**6. 훅은 사용자 홈 디렉터리에서 신뢰 경계를 넘는다.** 상태·잠금·결과 파일은 모두 `O_NOFOLLOW`로 열고, 열기 전후 `st_dev`/`st_ino`를 비교해 중간 교체를 거부하며, 0600 권한과 atomic replace를 씁니다. 카드에 기록되는 Skill/서브에이전트/MCP 이름은 화이트리스트 문법을 통과해야 하고, 고엔트로피 문자열(API 키·해시·UUID 형태)은 placeholder로도 남기지 않고 버립니다. 코멘트 길이는 문자열을 자르는 대신 카운트가 낮은 항목부터 빼고 다시 직렬화해 JSON이 절대 깨지지 않게 맞춥니다.

**7. Hermes는 릴리스 주기가 빠르고, 업데이트가 local commit을 되돌린다.** Dashboard의 토큰 집계·observation 지원은 Hermes 위에 얹는 carried commit이라, 체크아웃을 제자리에서 업데이트하면 사라질 수 있고 그러면 기록이 조용히 끊깁니다. 그래서 이 프로젝트는 사용자의 Hermes 체크아웃을 아예 건드리지 않습니다. 검증된 upstream과 carried chain으로 `<HERMES_AGENT_REPO>.releases/release-<carried>`에 불변 release를 따로 구성하고, 관리형 launcher가 읽는 selector 파일 하나만 바꿔 그 release를 선택합니다. 체크아웃은 release root 위치를 정하는 읽기 전용 입력일 뿐입니다. release 시작 시 official provenance를 확인한 SHA를 `patches/hermes-agent-supported-upstream`에 고정합니다. setup은 어떤 write보다 먼저 고정 SHA를 official HTTPS 저장소에서 exact object로 가져와 identity를 확인하고, carried manifest와 bundle이 그 upstream 위의 선형 chain인지, 그리고 `--help` 출력에 어댑터가 의존하는 플래그가 모두 있는지를 검사합니다. 검증 중 official `main`이 이동하면 현재 release를 폐기하지 않고 다음 maintenance cycle에서 처리합니다.

**8. 세 런타임의 훅 어휘가 다르다.** Claude Code, Codex, Hermes는 이벤트 이름도 payload 필드명도 서로 다릅니다. Codex 어댑터가 snake/camel/legacy 별칭을 공통 어휘로 정규화하고, Claude와 Codex는 같은 `handle_event` 상태 기계를 `source`만 바꿔 공유합니다. 런타임이 구조적으로 제공할 수 없는 범주는 0으로 채우지 않고 `"unavailable"`에 명시합니다(예: Codex Skills).

## 구체적인 장점

- **설정 파일 없는 프로젝트 라우팅** — `.envrc`, `direnv`, 환경변수, 반복적인 `--board` 없이 Dashboard에서 지정한 Project directory가 곧 보드입니다. 하위 디렉터리에서도 동작하고, 모호한 매칭은 실패로 처리합니다.
- **턴 단위 카드** — 세션이 아니라 프롬프트 단위라 카드 하나가 하나의 요청과 결과에 대응합니다. Stop이 누락되면 다음 프롬프트가 이전 카드를 "Superseded" 사유로 닫아 카드가 running에 방치되지 않습니다.
- **읽을 수 있는 완료 카드** — 목록에서는 최대 1,000자 구조 요약, 클릭하면 원문 전체. 비정상 종료는 턴의 표현 대신 명시적 문구로 남깁니다.
- **접근 가능한 원문 열기** — Result 요약 영역은 실제 button semantics, `aria-expanded`, `aria-controls`, 키보드 활성화와 focus ring을 사용합니다. 별도 원문이 없는 카드에는 불필요한 펼치기 컨트롤을 만들지 않습니다.
- **재시도 안전** — 카드 생성, 사용량 코멘트, 완료가 각각 멱등합니다. 완료 성공 전까지 원문을 0600 상태에 보존하므로 Stop 실패 후 SessionEnd나 다음 프롬프트가 이어받습니다.
- **정직한 사용량 보고** — 제공되지 않은 값을 0으로 채우지 않습니다. archived 카드를 포함한 all-time 누적 합계와 coverage를 함께 표시해, 필터로 화면을 좁혀도 보드 총량이 흔들리지 않습니다.
- **fail-open 관측** — 토큰 수집이나 코멘트 게시가 실패해도 카드 수명주기를 막지 않습니다. 훅 실패는 카드를 방치하는 대신 로컬 로그로 빠집니다.
- **저장소에 갇힌 설치** — 설치 대상은 저장소를 가리키는 심볼릭 링크와 병합된 훅 항목뿐입니다. 기존 Claude/Codex 훅을 제거하지 않고 멱등 병합하며, `--dry-run`으로 머신을 바꾸지 않고 전체 절차를 미리 볼 수 있고, uninstall은 자기가 만든 링크만 지우고 보드·카드는 보존합니다.
- **읽기 전용 원칙** — 토큰과 모델을 얻기 위해 JSONL과 SQLite를 로컬에서 읽기만 합니다. 원본 세션 store에는 쓰지 않고, 대시보드 HTTP 인증을 우회하거나 세션 토큰을 스크레이핑하지 않습니다.

## 검증 근거

- **자동화 테스트** — CI가 수집된 전체 테스트를 실행합니다. 커버 범위는 어댑터 CLI와 백엔드, Claude/Codex/Hermes 훅 수명주기, 자동 위임·백그라운드 알림 제외, 사용량·토큰 정규화, Codex 모델 폴백, 훅 설치기, setup/uninstall, Hermes 플러그인 등록과 업데이터입니다.
- **계약 스모크** — `scripts/kanban-smoke.sh`가 실제 Hermes CLI로 카드 생성 → 코멘트 → 완료 → archive를 왕복하고 각 단계를 `hermes kanban show` 출력으로 확인합니다. setup 마지막에 실행되며 실패 시 설치가 비정상 종료합니다.
- **설치 전 호환성 검사** — setup이 `hermes kanban --help` 계열 출력에서 `--observation`, `--tenant`, `--created-by`, `--initial-status`, `--json` 등 의존 플래그의 존재를 토큰 단위로 확인하고, frozen upstream exact object·reviewed carried chain·immutable release receipt를 검사한 뒤에만 파일을 씁니다.
- **런타임 실측 기록** — Hermes REST 인증 경계, `skipped_nonspawnable`의 실제 동작과 이벤트 부재, tenant 사후 변경 불가, `gateway:startup` 훅 발화 등 설계 전제를 로컬에서 확인한 결과가 `docs/unified-kanban-spec.md` §6에 항목별로 기록되어 있습니다. 문서와 실제가 달랐던 항목은 설계를 실측에 맞춰 바꿨습니다.
- **비파괴 예행 연습** — `./scripts/setup.sh --dry-run --no-restart --skip-smoke`로 머신을 변경하지 않고 설치 절차를 확인할 수 있습니다.
- **실제 Dashboard 상호작용** — 요약 버튼의 `aria-expanded=false → true → false`, 원문 DOM 생성·제거, 긴 결과의 마지막 marker까지 브라우저에서 확인했습니다. UI 번들의 syntax와 focus 스타일도 회귀 테스트에 포함됩니다.

---

## 설치

### 1. 지원 환경과 준비물

현재 설치와 실제 smoke 검증이 완료된 환경은 **macOS**입니다. Windows와 WSL2는 이 프로젝트의 지원 대상이 아니며, Linux도 실제 머신 검증을 마치기 전까지 공식 지원 환경으로 표기하지 않습니다.

| 구분 | 요구 사항 |
| --- | --- |
| 검증 환경 | macOS |
| 지원 제외 | Windows, WSL2 |
| 필수 | Git, Bash, `curl`, Python 3.11 이상, Hermes Agent |
| 개발·업데이트 검증 | `uv` — lockfile 기반 전체 pytest와 isolated Hermes 회귀 테스트 실행에 사용 |
| Hermes 경로 | 기본값 `~/.hermes/hermes-agent`; 다른 위치라면 절대 경로를 `HERMES_AGENT_REPO`로 지정 |
| 선택 | Claude Code, Codex CLI — 해당 소스의 작업도 관측하려는 경우에만 필요 |
| 설치 위치 | 저장소를 계속 유지할 절대 경로. 설치 링크가 이 checkout을 직접 가리키므로 설치 후 임의로 이동하거나 삭제하지 않음 |

버전을 먼저 확인할 수 있습니다.

```bash
git --version
bash --version
python3 --version   # 3.11 이상
curl --version
```

### 2. Hermes Agent 설치 및 초기 설정

Hermes Agent가 없다면 [공식 설치 문서](https://hermes-agent.nousresearch.com/docs/getting-started/installation)의 CLI 설치 명령을 사용합니다.

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
```

새 셸을 열거나 셸 설정을 다시 읽은 뒤 Hermes를 설정하고 상태를 확인합니다.

```bash
source ~/.zshrc  # Bash를 사용한다면: source ~/.bashrc
hermes setup
hermes doctor
hermes version
test -d "${HERMES_AGENT_REPO:-$HOME/.hermes/hermes-agent}"
```

`hermes` 명령을 찾지 못하면 현재 셸의 PATH에 사용자 실행 파일 디렉터리를 추가한 뒤 다시 확인하세요.

```bash
export PATH="$HOME/.local/bin:$PATH"
command -v hermes
```

### 3. Unified Kanban clone

다른 컴퓨터에서는 저장소를 **계속 유지할 위치**에 clone합니다. 아래 예시는 홈 디렉터리 아래에 설치합니다.

```bash
cd "$HOME"
git clone https://github.com/LeeYuHoon/unified-kanban.git
cd unified-kanban
```

기존 checkout을 사용하는 경우에는 먼저 최신 변경과 bundle 파일이 있는지 확인합니다.

```bash
git pull --ff-only
test -f patches/hermes-agent-carried.bundle
```

### 4. 선택 사항: 비파괴 사전 점검

실제 파일, 훅, 플러그인, Gateway를 변경하지 않고 설치 계획만 확인하려면 다음을 실행합니다.

```bash
./scripts/setup.sh --dry-run --no-restart --skip-smoke
```

`DRY RUN:` 출력이 이어지고 오류 없이 끝나면 실제 설치를 진행합니다. dry-run은 새 Hermes Git object를 import하거나 설정 파일을 수정하지 않습니다.

### 5. 설치

기본 smoke 보드를 최초 1회 만든 뒤 setup을 실행합니다.

```bash
hermes kanban boards create --name "Unified Kanban Smoke" unified-kanban-smoke
./scripts/setup.sh
```

설치 스크립트는 다음 작업을 순서대로 수행합니다.

1. Python/Hermes/Git과 필요한 `hermes kanban` CLI 옵션을 검사합니다.
2. 저장소가 검증한 `patches/hermes-agent-supported-upstream`의 frozen SHA를 fixed official HTTPS 저장소에서 exact object로 가져와 identity를 확인합니다.
3. `patches/hermes-agent-carried.bundle`의 prerequisite, ordered refs, 선형 parent chain과 최종 carried commit을 검증합니다.
4. 검증된 object로 checkout과 분리된 불변 Hermes release를 구성하거나 completion receipt까지 정확히 재검증한 뒤 설치를 계속합니다.
5. 다음 명령을 저장소 파일로 향하는 관리형 심볼릭 링크로 설치합니다.
   - `~/.local/bin/kanban-adapter`
   - `~/.local/bin/claude-kanban-hook`
   - `~/.local/bin/codex-kanban-hook`
   - `~/.local/bin/ai-session-viewer`
6. 기존 항목을 보존하면서 Claude/Codex 훅을 `~/.claude/settings.json`과 `~/.codex/hooks.json`에 멱등 병합합니다.
7. `integrations/hermes/hermes-kanban`을 `~/.hermes/plugins/hermes-kanban`에 연결하고 플러그인을 활성화합니다.
8. Hermes Gateway를 재시작합니다.
9. 미리 생성된 Hermes Kanban 보드에서 카드 생성 → 코멘트 → 완료 → archive를 검증하는 smoke test를 실행합니다. 기본 smoke 보드는 `unified-kanban-smoke`이며 테스트 카드는 마지막에 archive됩니다. Board 생성은 transaction ownership을 안전하게 증명할 수 없어 setup과 smoke가 대신 수행하지 않습니다.

bundle은 이 저장소에 포함되므로 별도의 개인 fork remote나 기존 컴퓨터의 Git object database가 필요하지 않습니다. 동일한 checkout에서 `setup.sh`를 다시 실행해도 관리 항목을 중복 생성하지 않습니다. 반대로 기존 일반 파일이나 다른 대상을 가리키는 심볼릭 링크를 발견하면 덮어쓰지 않고 안전하게 중단합니다.

레거시 `--project-dir ... --board SLUG` 라우팅을 사용할 때는 해당 보드를 먼저 `hermes kanban boards create`로 만들어야 합니다. Setup은 slug만으로 생성 ownership을 안전하게 증명할 수 없으므로 보드를 자동 생성하지 않으며, 보드가 없거나 목록 응답이 잘못됐으면 host 파일을 쓰기 전에 중단합니다.

### 6. 설치 옵션

```text
./scripts/setup.sh [options]
  --dry-run                 변경하지 않고 수행 예정 작업 출력
  --skip-smoke              마지막 실제 Kanban smoke test 생략
  --no-restart              Hermes Gateway 재시작 생략
  --project-dir ABS_PATH    레거시 .envrc 라우팅을 설정할 절대 경로
  --board SLUG              --project-dir과 함께 사용할 보드 slug
```

일반 설치에서는 옵션이 필요하지 않습니다. `--project-dir`/`--board`는 하위 호환용이며, 새 설치는 Dashboard의 **Project directory**를 사용하는 것이 권장됩니다. 문제를 숨길 수 있으므로 최초 설치에서 `--skip-smoke`를 기본처럼 사용하지 마세요.

### 7. Dashboard와 프로젝트 연결

설치가 끝나면 Dashboard를 엽니다.

```bash
hermes dashboard
```

1. Dashboard에서 **Kanban** 화면을 엽니다.
2. 새 보드를 만들거나 기존 보드를 선택합니다.
3. 보드 설정의 **Project directory**에 관측할 프로젝트의 절대 경로를 지정합니다. Unified Kanban 저장소 경로가 아니라 Claude/Codex/Hermes로 실제 작업할 프로젝트 경로입니다.
4. 저장한 뒤 해당 프로젝트 디렉터리 또는 하위 디렉터리에서 에이전트를 실행합니다.

Hermes는 이 값을 보드의 `default_workdir`로 저장합니다. `kanban-adapter`는 현재 작업 디렉터리와 가장 구체적으로 일치하는 보드를 자동 선택합니다. 동일 깊이의 중복 매핑처럼 선택이 모호하면 임의의 보드를 고르지 않고 오류로 중단합니다.

일반적인 사용에는 `.envrc`, `direnv`, 환경변수, 반복적인 `--board` 옵션이 필요하지 않습니다.

### 8. 클라이언트 재시작 및 설치 확인

설치 전에 실행 중이던 **Hermes CLI/TUI/Desktop, Claude Code, Codex CLI 프로세스를 완전히 종료하고 다시 실행**하세요. Gateway 재시작만으로 이미 실행 중인 클라이언트 프로세스의 플러그인 코드까지 다시 로드되지는 않습니다.

설치 링크와 기본 CLI를 확인합니다.

```bash
command -v kanban-adapter
command -v claude-kanban-hook
command -v codex-kanban-hook
command -v ai-session-viewer
kanban-adapter --help
hermes kanban boards list --json
```

명령을 찾지 못하면 새 셸을 열거나 다음을 현재 셸에 적용합니다.

```bash
export PATH="$HOME/.local/bin:$PATH"
```

설치 당시 smoke test를 생략했다면 나중에 직접 실행할 수 있습니다.

```bash
./scripts/kanban-smoke.sh
```

이제 `setup.sh`가 세 소스를 같은 Dashboard 보드에 연결합니다.

- **Claude Code / Codex**: 저장소가 관리하는 훅을 기존 훅을 제거하지 않고 `~/.claude/settings.json`과 `~/.codex/hooks.json`에 병합합니다. 설치 후 각 CLI를 재시작하세요. 실제 사용자 프롬프트마다 실행 중인 Hermes Kanban 카드가 생성됩니다. CLI의 `Stop` 이벤트는 최종 응답 원문과 구조 우선·결과 중심의 최대 1,000자 요약을 별도로 저장해 카드를 완료하고, `SessionEnd`는 정리용 폴백으로 동작합니다. 카드 상세에는 요약이 기본 표시되며 요약을 클릭하면 원문 전체를 열거나 닫을 수 있습니다. Codex가 처음 실행할 때 새 훅 명령을 신뢰할지 물으면 Codex 자체 신뢰 프롬프트에서 승인하세요.
- **Hermes Agent**: 저장소의 `integrations/hermes/hermes-kanban` 플러그인을 `~/.hermes/plugins/hermes-kanban`에 심볼릭 링크하고 활성화합니다. 실제 Hermes 사용자 턴마다 실행 중인 카드(`tenant=hermes`)가 생성되고 턴이 끝나면 완료됩니다. 자동 delegation 완료 알림, background process 알림, context compaction/todo 복원 알림과 delegated child·Kanban worker 자체 실행은 보조 내부 작업이므로 별도 카드를 만들지 않습니다. 예전 독립형 `hermes-kanban` 저장소가 남긴 심볼릭 링크는 자동으로 마이그레이션하지만, 해당 경로의 무관한 파일이나 심볼릭 링크는 덮어쓰지 않고 설치를 거부합니다. 설치 마지막의 Gateway 재시작으로 플러그인을 로드합니다.

### 9. 자주 발생하는 설치 오류

| 메시지/증상 | 해결 방법 |
| --- | --- |
| `hermes: command not found` | 새 셸을 열거나 `export PATH="$HOME/.local/bin:$PATH"` 후 `command -v hermes` 확인 |
| `Hermes Agent checkout not found` | 공식 Hermes 설치를 완료하거나 실제 checkout 절대 경로를 `HERMES_AGENT_REPO`로 지정 |
| `Hermes version mismatch` | `./scripts/update-hermes-if-needed.sh` 실행 후 `./scripts/setup.sh` 재실행 |
| frozen upstream object unavailable/mismatch | fixed official HTTPS 저장소에서 저장소 pin의 exact object를 가져와 identity를 확인할 수 없습니다. setup을 우회하지 말고 network·pin·official provenance를 확인하세요. |
| dirty Hermes checkout | activation blocker가 아닙니다. checkout은 release root 위치를 정하는 읽기 전용 입력이며 setup/updater는 reset·stash·revert하거나 파일을 변경하지 않습니다. |
| `Refusing foreign ...` | 해당 경로를 무작정 삭제하지 말고 `readlink`/백업으로 소유자를 확인. 이전 Unified Kanban 설치라면 그 checkout의 `./scripts/uninstall.sh`을 먼저 실행 |
| `kanban-adapter: command not found` | 새 셸을 열거나 `~/.local/bin`을 PATH에 추가 |
| smoke test 실패 | 오류 출력 확인 후 `hermes doctor`, `hermes kanban boards list --json`, `./scripts/kanban-smoke.sh` 순서로 재검증 |

## 카드별 실행 정보

카드가 완료되기 전에 소스별 공식 훅에서 수집한 Skill, 서브에이전트, MCP, 모델, 토큰 사용량을 **하나의 구조화된 코멘트**로 기록합니다.

```text
Codex tool usage
{"event_id":"usage-3f0a…","mcp":{"github/search_issues":2},"model":"gpt-5.6-sol","schema_version":2,"skills":{},"source":"codex","subagents":{"reviewer":1},"tokens":{"cache_read":0,"cache_write":null,"input":17553,"output":13,"reasoning":0,"requests":1,"total":17566},"unavailable":["skills"]}
```

첫 줄은 소스별 헤더(`Agent tool usage`, `Codex tool usage`, `Hermes Agent tool usage`)이고, 둘째 줄은 검증 가능한 JSON 페이로드입니다. 토큰이 포함된 페이로드는 `schema_version` 2를 사용합니다.

### 도구 사용량

| 소스 | 사용하는 훅 이벤트 | Skills | 서브에이전트 / 에이전트 | MCP 도구 | 모델 |
| --- | --- | --- | --- | --- | --- |
| Claude Code | `UserPromptSubmit`, `PostToolUse`, `Stop`, `SessionEnd` | `Skill` 도구의 `skill` | `Task` 도구의 `subagent_type` | `mcp__<server>__<tool>` | 페이로드가 제공할 때만 |
| Hermes Agent | `pre_llm_call`, `post_tool_call`, `subagent_start`, `post_llm_call`, `on_session_end` | `skill_view`의 `args.name` | `subagent_start`의 `child_role` | `mcp__<server>__<tool>` | `pre_llm_call` / `post_llm_call` |
| Codex CLI 0.145 | `UserPromptSubmit`, `PostToolUse`, `SubagentStart`, `Stop`, `SessionEnd` | **제공되지 않음** | `SubagentStart`의 `agent_type` | `mcp__<server>__<tool>` | 페이로드, 없으면 로컬 상태 DB의 해당 세션 행 |

- **Codex Skills는 0이 아니라 제공되지 않음으로 기록합니다.** Codex 0.145 훅은 Skill 이름을 제공하지 않으므로 `"skills": {}`와 `"unavailable": ["skills"]`를 함께 기록합니다. Skill을 사용하지 않았다고 추정하지 않습니다.
- 서브에이전트 이벤트가 있는 런타임은 그 이벤트를 기준으로 집계합니다. Hermes의 `delegate_task` 호출 하나가 여러 자식을 생성할 수 있기 때문입니다.
- **Codex 모델 폴백은 해당 세션의 행만 사용합니다.** 훅 페이로드의 명시적 `model`이 항상 우선합니다. 값이 없으면 로컬 Codex 상태 DB(`~/.codex/state_<n>.sqlite`)의 `threads.model`에서 정확한 세션을 바인드 파라미터와 `SELECT model` 프로젝션으로 조회합니다. `~/.codex/config.toml`의 머신 전역 기본 모델은 해당 세션의 모델이 아니므로 사용하지 않습니다. DB는 읽기 전용으로 열고, 연결 전후에 경로가 심볼릭 링크가 아닌 일반 파일인지 확인합니다. 검증에 실패하면 모델을 기록하지 않습니다.

### 토큰 사용량

토큰 페이로드는 가능한 범위에서 다음 값을 분리합니다.

- `input`
- `output`
- `cache_read`
- `cache_write`
- `reasoning`
- `requests`
- `total`

표시 위치는 다음과 같습니다.

- **보드 합계**: 선택한 보드의 모든 카드에서 중복 제거된 사용 기록을 합쳐 보드 헤더에 누적 `Total`, `Input`, `Cache`, `Output`, `Reasoning`, 추적 카드 수와 coverage를 표시합니다. archived 카드를 화면에서 숨기거나 tenant/workflow 필터로 카드 목록을 좁혀도 보드의 all-time 누적 합계는 바뀌지 않습니다.
- **모델 계열 합계**: 보드 헤더에서 Claude와 GPT 사용량을 별도 카드로 표시합니다. Claude Code/Codex는 신뢰 가능한 source로, Hermes Agent는 명시된 model 이름으로 분류하며, 판정할 수 없는 모델은 추정하지 않고 `Other`로 표시합니다.
- **카드별 사용량**: 각 카드에 전체 토큰 badge를 표시합니다.
- **카드 상세**: 카드별 input/output/cache read/cache write/reasoning/request breakdown과 runtime, model, coverage를 표시합니다.
- **보드 목록**: 각 보드의 누적 token total을 함께 표시합니다.

집계 원칙:

- `board total`은 선택 보드의 고유 usage record 합계입니다.
- `card total`은 해당 카드의 고유 usage record 합계입니다.
- 결정론적 `event_id`로 hook retry, resume, lifecycle 이벤트 재전송을 중복 제거합니다.
- provider가 신뢰 가능한 `total`을 제공하면 그 값을 보존합니다. 없거나 유효하지 않을 때만 의미가 명확한 bucket으로 계산합니다.
- cache와 reasoning이 provider total에 이미 포함될 수 있으므로 무조건 다시 더하지 않습니다.
- 토큰 코멘트가 없는 기존 카드는 `0`이 아니라 **미추적**으로 표시합니다. 따라서 coverage가 100% 미만일 수 있습니다.
- provider가 실제로 `0`을 반환한 경우와 필드를 제공하지 않은 경우를 구분합니다.
- Claude는 별도 reasoning bucket을 제공하지 않습니다. reasoning을 0으로 추정하지 않고 **Output에 포함** 또는 `N/A`로 표시합니다.
- compact 표시는 1,000 미만은 전체 숫자를 유지하고 큰 값은 locale의 K/M/만 등 적절한 단위를 사용합니다. 모든 값을 K로 강제하지 않으며 단위 라벨은 `tok`가 아니라 `tokens`로 표기합니다.

수집 방식:

- **Claude Code**: 허용된 Claude runtime root 안의 JSONL을 읽기 전용으로 열고 카드 시작·종료 snapshot의 delta를 기록합니다. 같은 request ID가 여러 content block에 반복돼도 한 번만 집계합니다.
- **Codex**: 허용된 `CODEX_HOME`/Codex runtime root 안의 session JSONL에서 누적 `total_token_usage` 시작·종료 snapshot의 delta만 기록합니다. hook payload에 rollout 경로가 없으면 검증된 로컬 state SQLite의 정확한 세션 행을 읽기 전용으로 조회합니다. Codex가 제공하지 않는 cache write bucket은 `0`이 아니라 `null`로 보존합니다.
- **Hermes Agent**: 공식 `post_api_request` 훅이 제공하는 canonical usage를 요청별로 누적하고, 안전한 request identity digest로 재시도를 중복 제거합니다.

JSONL과 SQLite는 token 숫자를 계산하기 위해 로컬에서 읽기만 합니다. 코멘트에는 원본 프롬프트, 응답, transcript 행, tool payload, 명령어, 파일 경로를 저장하지 않습니다.

## 개인정보와 멱등성

- 사용량 코멘트에는 source, model 식별자, 정제된 Skill/서브에이전트/MCP 이름과 횟수, token 숫자, 안전한 event/request 식별자만 포함합니다.
- 범주별 이름은 최대 25개로 제한하고, 초과하면 `"truncated": true`를 기록합니다.
- 각 코멘트의 `event_id`는 source와 카드에서 결정론적으로 파생됩니다. 어댑터는 같은 ID가 있는지 다시 확인한 뒤 추가하므로 코멘트 게시 직후 상태 기록 전에 프로세스가 종료돼도 재시도가 중복을 만들지 않습니다.
- 비정상 완료는 카드 요약에 명시적으로 남깁니다.

사용자 또는 assistant에서 파생된 텍스트는 다음 범위에서만 사용합니다.

- **카드 제목**은 사용자 요청을 한 줄로 축약하고 120자로 제한한 발췌입니다. Kanban에 게시되는 유일한 프롬프트 파생 텍스트입니다.
- **작업 디렉터리**는 같은 세션의 이후 이벤트가 동일한 보드를 찾도록 로컬 훅 상태(`~/.cache/kanban-adapter/…`; Hermes는 `~/.cache/unified-kanban/hermes-turns/`, 권한 `0600`)에만 보관합니다. Hermes turn 상태에는 완료 전까지 최종 응답 원문, 제한된 결과 요약과 누적 token counter도 함께 보관합니다.
- assistant의 **최종 응답 원문**은 카드의 `result`에, 최대 1,000자 요약은 run summary 또는 observation completion/edit 이벤트에 별도로 저장합니다. Dashboard는 요약을 기본 표시하고 사용자가 클릭한 경우에만 원문을 펼칩니다. 완료 재시도 전에는 원문을 권한 `0600`의 로컬 상태에 보존하고, 큰 결과는 운영체제 명령행 크기 제한을 피하도록 별도의 `0600` 임시 파일로 전달한 뒤 성공 시 제거합니다. 요약 생성은 fenced code block을 제외하며 명시적인 완료/변경/검증/미완료 섹션을 우선하고, 추가 모델 호출은 하지 않습니다. prompt, tool payload, 중간 transcript 원문은 계속 저장하지 않으며 token telemetry 코멘트에도 원문을 넣지 않습니다. 이 경로는 비밀정보 마스킹이 아니므로 에이전트의 최종 결과 문장에 credential을 포함하면 안 됩니다.

## 업데이트와 호환성

설치는 파일을 쓰기 전에 `patches/hermes-agent-supported-upstream`의 고정 SHA를 official HTTPS 저장소에서 exact object로 가져와 identity를 확인하고, `patches/hermes-agent-carried-commits`의 모든 커밋이 그 upstream 위의 검증된 선형 chain인지 확인합니다. pin 경로는 환경변수로 바꿀 수 없고 파일은 symlink를 따르지 않는 descriptor로 열어 inode를 재검증합니다. updater도 같은 frozen SHA와 bundle identity에 묶인 release만 구성·선택하며, 이후 official `main` 이동은 현재 activation을 막지 않고 다음 maintenance cycle에서 처리합니다. 이미 설치한 뒤 사용자가 직접 `hermes update`한 경우에도 Hermes plugin은 등록 시점과 매 새 turn 시작 전에, 설치된 Claude/Codex hook과 `kanban-adapter`는 매 실행 전에 검사합니다. 검사 대상은 pin, selector와 completion receipt, 그리고 실제로 실행되는 artifact입니다. checkout은 변경되지 않는 읽기 전용 입력이므로 checkout `HEAD`는 보지 않고, selector가 `<HERMES_AGENT_REPO>.releases/release-<carried manifest 최종 commit>`을 정확히 가리키는지, 그 release가 실제 디렉터리이며 실행 가능한 `venv/bin/hermes`와 자기 디렉터리 identity에 묶인 completion receipt를 갖는지, `hermes version`의 upstream이 pin과 일치하는지 확인합니다. mismatch면 hook은 외부 CLI를 방해하지 않도록 기록 없이 종료하고 adapter는 오류로 중단하므로 새 버전에서 이 프로젝트를 계속 적용하지 않습니다.

지원되는 배포 경로는 `scripts/setup.sh`입니다. wheel은 library/build 검증 artifact일 뿐이며 repository pin·carried manifest·wrapper·installer를 포함하지 않으므로 mutation console script를 공개하지 않습니다. module CLI를 직접 실행해도 동일한 compatibility gate가 적용되고 policy 파일이 없으면 fail-closed합니다.

업데이트 전후에 확인할 명령, carried bundle 검증, 전체 테스트, 실제 smoke, 서비스 복원, 두 번째 실행의 멱등성, 실패 복구와 증거 기록 형식은 [`docs/hermes-update-checklist.md`](docs/hermes-update-checklist.md)에 정리되어 있습니다. Hermes를 업데이트할 때마다 이 체크리스트를 처음부터 끝까지 실행하세요.

검증이 실패하면 다음을 실행한 뒤 다시 설치하세요.

```bash
./scripts/update-hermes-if-needed.sh
./scripts/setup.sh
```

업데이터는 Hermes 체크아웃에 아무것도 쓰지 않습니다. 검증된 release가 이미 선택돼 있고 중단된 활성화도 없으면 `SKIPPED`로 끝나며, 이 판정은 lock을 잡기 전에 끝나므로 release 구성도 서비스 재시작도 state·lock write도 하지 않습니다. 선택이 달라야 할 때만 frozen pin의 exact official object와 reviewed bundle을 검증해 불변 release를 구성하거나 이미 있으면 source·Git metadata·dependency inventory·completion receipt를 정확히 재검증한 뒤, path transaction 안에서 selector를 교체하고, 원래 실행 중이던 서비스만 재시작합니다. 어느 단계에서 실패하든 이전 selector로 되돌리고 서비스를 복원하며, 그 사이 제3자가 selector를 바꿨다면 그 값을 지우거나 덮어쓰지 않고 보존한 채 실패를 보고합니다. 이전 release directory는 아직 그 경로로 실행 중인 프로세스가 없다고 증명할 수 없으므로 삭제하지 않습니다. `--check`는 selector만 읽고 아무것도 바꾸지 않습니다.

머신을 변경하지 않고 설치 과정을 미리 확인하려면:

```bash
./scripts/setup.sh --dry-run --no-restart --skip-smoke
```

## 사용

Kanban 화면에서 설정한 프로젝트 디렉터리에서:

```bash
TASK_ID="$(kanban-adapter start --title "Implement checkout" --source manual)"
kanban-adapter update --task "$TASK_ID" --message "Tests passing"
kanban-adapter done --task "$TASK_ID" --summary "Implemented and verified"
```

명시적인 `--board`가 항상 최우선입니다. 현재 디렉터리가 Dashboard에 매핑되지 않았을 때는 `HERMES_KANBAN_BOARD`를 폴백으로 사용합니다. 레거시 설치 옵션 `--project-dir`와 `--board`도 하위 호환을 위해 남아 있지만 Dashboard 기반 워크플로에는 필요하지 않습니다.

### Claude, Codex, Hermes 세션 읽기

설치는 Claude Code, OpenAI Codex CLI, Hermes Agent 세션을 읽는 로컬 전용·읽기 전용 timeline viewer인 `ai-session-viewer`도 함께 설치합니다.

```bash
ai-session-viewer list --provider all
ai-session-viewer prompts codex:<session-id>
ai-session-viewer timeline hermes:<session-id> --show-activity
ai-session-viewer export claude:<session-id> --format html --out ~/Documents/session.html
```

viewer는 세 로컬 포맷을 정규화하고, 합성/내부 메시지를 걸러내며, reasoning과 tool payload를 숨기고, 최선 노력 기반 secret redaction을 적용해 독립 실행형 Markdown 또는 HTML을 생성합니다. Hermes SQLite는 `mode=ro`로 열고 원본 session store에는 쓰지 않으며 `~/.claude`, `~/.codex`, `~/.hermes` 아래로 출력하지 않습니다. 자세한 동작과 제약은 `integrations/session-viewer/README.md`를 참고하세요.

## 디렉터리 구조와 코드 관리

핵심 경계는 `bin/`(설치 wrapper), `scripts/`(setup/update/uninstall/smoke), `src/kanban_adapter/`(공통 상태·usage·CLI backend), `integrations/`(Hermes plugin과 session viewer), `patches/`(검증된 Hermes pin과 carried stack), `tests/`로 분리되어 있습니다. 각 디렉터리와 모듈의 책임, 의존 방향, 주석/docstring 원칙, 공개 전 리팩터링 판단은 [`docs/project-structure.md`](docs/project-structure.md)에 정리했습니다.

코드는 명백한 문법을 반복 설명하지 않고 신뢰 경계, 실패 순서, race 방어, provider별 token 의미처럼 코드만으로 드러나지 않는 이유를 주석으로 남깁니다. 비자명 Python 모듈에는 책임을 설명하는 module docstring을, 공개 통합 API와 상태 전이에는 method docstring을 둡니다. 첫 공개 직전의 광범위한 재작성은 피하고, 테스트로 보호된 명확한 책임 분리만 수행합니다.

## 테스트와 프로젝트 유지관리

개발 환경과 테스트는 `uv.lock`으로 고정합니다.

```bash
uv sync --frozen --group dev
uv run pytest -o addopts='' -q
bash -n scripts/setup.sh scripts/uninstall.sh scripts/kanban-smoke.sh \
  scripts/update-hermes-if-needed.sh bin/claude-kanban-hook \
  bin/codex-kanban-hook bin/kanban-adapter
git diff --check
```

pytest는 순수 단위 테스트뿐 아니라 임시 HOME/Git 저장소를 이용한 setup·uninstall·updater·symlink·TOCTOU·호환성 회귀 테스트와 session viewer 테스트를 함께 실행합니다. 실제 Hermes CLI, Gateway, Dashboard가 필요한 계약 smoke/UI 검증은 로컬 release gate로 별도 실행하며 CI 성공만으로 대체하지 않습니다.

GitHub Actions는 `main` push와 모든 PR에서 macOS의 잠긴 환경, 전체 pytest, shell syntax, Python compile, whitespace를 검사합니다. 매일 **03:00 KST**에는 Hermes `main`과 검증 pin의 차이를 확인합니다. 새로운 upstream을 자동 승인하거나 pin을 이동하지 않고 실패 신호를 내며, maintainer가 carried stack 재적용과 전체 검증을 마친 후 한 PR로 갱신합니다. 이유와 release/backup/rollback 절차는 [`docs/maintenance.md`](docs/maintenance.md)에 있습니다.

## 기여, 보안, 라이선스

- 개발·PR 절차: [`CONTRIBUTING.md`](CONTRIBUTING.md)
- 비공개 취약점 신고와 trust boundary: [`SECURITY.md`](SECURITY.md)
- 변경 이력: [`CHANGELOG.md`](CHANGELOG.md)
- 행동 강령: [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)
- 라이선스: [MIT](LICENSE)
- 제3자 고지(Hermes Agent carried bundle): [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)
- 공개 준비 감사와 남은 gate: [`docs/open-source-readiness-2026-08-07.md`](docs/open-source-readiness-2026-08-07.md)

## 제거

보드, 카드, 프로젝트 metadata를 삭제하지 않고 설치된 링크, Hermes plugin 심볼릭 링크와 레거시 관리형 routing 항목을 제거합니다. `hermes-kanban` plugin도 비활성화합니다. 안전한 재설치와 interrupted-update 진단을 위해 `${XDG_CACHE_HOME:-$HOME/.cache}/kanban-adapter`, `${XDG_CACHE_HOME:-$HOME/.cache}/unified-kanban/hermes-turns`, `${HERMES_HOME:-$HOME/.hermes}/state/hermes-kanban-*`, Hermes checkout의 `refs/unified-kanban/carried/*`는 보존합니다. 완전 삭제가 필요하면 실행 중인 hook/updater가 없음을 확인한 뒤 이 경로를 별도로 검토해 제거하세요.

```bash
./scripts/uninstall.sh
```

## 검증

```bash
uv sync --frozen --group dev
uv run pytest -o addopts='' -q
./scripts/kanban-smoke.sh
```

CLI adapter, Dashboard 디렉터리 routing, Claude Code/Codex 관측 훅, Hermes Agent 턴 단위 plugin이 구현되어 있습니다.

단계별 구현 명세는 `docs/unified-kanban-spec.md`를 참고하세요.