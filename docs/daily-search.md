# 날짜별 토큰 달력 후보와 보존 API

초기 후보 검증 완료 시점(2026-09-09T08:21:37Z)에는 운영 미적용이었다. 저장소 게시와 사용자 머신 활성화는 별도 검증이며, 아래 설명은 날짜·검색 기능의 계약이다. 운영 적용 여부는 해당 배포의 실제 설치 경로와 서비스 검증 결과로 판단한다.

## 현재 UI 사용 기준

기존 칸반 컬럼·카드와 요약 디자인을 유지한다. 전체기간 누적 요약은 보드 선택 위에 유지하고 기존 요약 자리는 선택일 Total/Input/Cache/Output/Reasoning과 실행 경로별 토큰·카드 수로 바꾼다. 작은 날짜 달력 하나의 기본은 현재 보드·한국 시간 오늘(Asia/Seoul)이다. 날짜 변경은 카드 상세·누적 토큰을 바꾸지 않지만, 카드 목록은 해당 날짜의 실제 실행 증거로 필터링한다. 원래 칸반의 검색·담당자 필터와 관찰 보호는 유지한다. 이 UI 후보는 운영 미적용이다.

Legacy와 React는 동일한 날짜/실측 UX다. `/daily`에 현재 board, scope=current, period=custom, start=end=day=선택일, timezone=Asia/Seoul, `basis=execution`, source=status=all, limit=500을 보낸다. 서버의 summary.measured 전체 묶음을 표시하며 summary.estimated나 카드 누적량으로 대체하지 않는다. 서버는 페이지와 분리한 bounded `task_ids`에 실행 카드 ID 전체를 제공하므로 500개를 넘는 날에도 hydration 페이지 누락으로 카드를 숨기지 않는다. 하위호환 fallback은 `has_more === false`, `offset === 0`, `tasks.length === total`을 모두 만족할 때만 현재 페이지를 완전한 ID 집합으로 인정한다. model_groups는 Claude Code를 CLAUDE, Codex를 CODEX로 분류하고 Hermes는 기록된 실제 model metadata로 CLAUDE/CODEX/모델미상/기타 중 하나에 넣는다. 기존 model_families는 하위호환용이며 표시 그룹에 더하지 않는다. 한 이벤트는 정확히 한 그룹에 속하고, 누적과 선택일도 서로 더하지 않는다.

실측은 제공된 사용시각이 있는 수집분이라는 뜻이며 제공자 청구 총량 보증이 아니다. 수집된 0은 0, 실행은 있으나 토큰 이벤트가 없으면 N/A · 토큰 수집 안 됨으로 표시한다. 미래 날짜 등 실행 증거가 없으면 N/A · 기록된 실행 없음, 조회 실패 시 N/A · 조회 실패, 날짜를 지우면 N/A · 날짜 선택이며 전체기간 요청으로 바꾸지 않는다. 늦게 도착한 이전 날짜/기준/보드 응답은 현재 표시를 덮어쓰지 않는다.

React의 기존 mutation/socket 일별 캐시 무효화는 보존한다. Legacy는 보드 새로고침·이벤트/주기 갱신 때 같은 선택일을 다시 조회한다. 과거의 사용시각 없는 producer 기록은 누적에는 남지만 선택일에서는 정상적으로 N/A가 될 수 있고 소급 backfill하지 않는다.

## 보존한 backend 계약 (UI 필터가 아님)

아래 기간·검색·목록 집계 API는 기존 검증과 안전수정을 유지하기 위해 보존한다. 달력 UI에 기간 선택이나 검색 결과 표를 다시 노출하지 않는다. API의 생성일/완료일은 목록 기준이고 생성수/완료수는 각각의 시각으로 센다. 기간의 양끝 날짜는 포함하되 서버 시각 구간은 [start,end), 종료일 다음날 자정 미포함이다. 날짜 형식은 YYYY-MM-DD, 최대 3661일이다. IANA 시간대 DST를 따른다.

API 제목·본문 검색은 대소문자 무시 부분일치이며 `%`, `_`, SQL 문자열도 문자 그대로 검색한다. API의 day는 목록만 한정하되 period가 `all`이 아니면 요청 기간 안의 날짜여야 하며, 범위 밖 day는 run/event 증거 종류와 무관하게 HTTP 400으로 거부한다. matched_cards는 기간 전체 목록 수, total은 day까지 적용한 목록 수다. 보관 카드도 토큰 집계에 포함한다. 이러한 API 선택과 무관하게 현재 달력 UI는 현재 보드의 선택일 실측 수집분만 요청한다.

## 토큰의 시각·미수집

Hermes hook은 `post_api_request`의 request ID, `response_model`, `started_at`/`ended_at`을 받아 요청별 delta와 실제 종료시각을 `request_hash`, `model`, `usage_at`으로 기록한다. request hash를 task-bound event ID에 포함해 replay를 중복 합산하지 않는다. timestamp가 없으면 일별 실측으로 꾸미지 않는다. 이전 진행중 state의 누적 `tokens`만 있고 `token_events`가 없거나 일부만 있는 경우에는 새 요청 event 합을 빼고 남은 양을 날짜·모델 미상 누적 event로 한 번 보존한다. Claude/Codex 완료 hook의 누적 snapshot은 `completion_at`과 `usage_timing=completion`으로 보존해 누적에는 포함하지만 `usage_at` 실측과 분리한다. 원래 v1/v2 기록은 토큰 수치가 정확하더라도 날짜 귀속은 **생성일 추정**이다. `task_comments.created_at`은 저장시각이며 실제 사용시각으로 쓰지 않는다. 과거 운영 기록을 추정 backfill하지 않는다.

추가 v2 `usage_at` 필드는 명시적으로 제공된 Unix 정수 초만 허용한다. 시연의 실측시각 fixture는 **시연 데이터**이며 실제 제공자 호출 비용/사용량이 아니다. 신뢰된 v2 필드가 있을 때 `measured`, 없으면 생성일 `estimated`로 분리한다. 잘못된 명시 시각이나 귀속할 수 없는 생성시각은 `undated_events`로 별도 보고하며 기간 토큰에는 넣지 않는다. v1의 동명 추가 필드는 시각 계약이 아니므로 무시한다.

`basis=execution`의 카드 집합은 선택일과 겹치는 `task_runs.started_at`~`completed_at` 구간 또는 선택일의 신뢰된 `usage_at` 이벤트에서 만든다. 생성일은 실행 증거가 아니며, 생성만 된 카드는 포함하지 않는다. `period=all`도 다일 run의 시작일·모든 중간일·종료일 overlap을 순서와 무관하게 열거하며 날짜 확장 작업을 공유 요청 예산에 계상해 초과 시 부분합 없이 503을 반환한다. 자정에 걸친 실행은 겹치는 각 날짜의 카드 목록에 나타나지만 토큰은 실제 usage_at이 속한 날짜에만 합산한다. 완료 시각에 누적 토큰을 한꺼번에 기록한 legacy 이벤트를 여러 날의 실측으로 나누지 않는다. 카드 상세/‘카드 누적 토큰’은 기간과 무관한 누적값이므로 목록에 보이는 누적 토큰의 합이 기간 요약과 다를 수 있다.

수집된 0은 0, 미수집/빈 이벤트 집계는 null → N/A다. 일부 bucket만 알려진 경우 알려진 양과 partial coverage를 함께 표시한다. Claude reasoning 미분리는 ‘출력에 포함’이며 0으로 만들지 않는다. reasoning은 output의 부분집합이므로 total에 재가산하지 않는다. cache read/write는 각각 보존한다. JS의 정확한 정수 한계를 넘는 합계는 null과 `overflow_fields`로 알린다.

기존 author/header/source/schema(v1/v2)/task-bound hash·음수·과대값·손상 JSON 방어와 첫 유효 이벤트 우선 중복 제거를 유지한다. 해시/작성자 검사는 암호학적 제공자 인증이 아니다. 같은 task ID/event hash라도 **보드가 다르면 별개**, 한 보드 안의 같은 이벤트는 한 번이다.

일반 HTTP 댓글 경로는 런타임 전용 작성자 `kanban-adapter`, `hermes-agent`를 지정하면 400으로 거부한다. 실제 어댑터의 로컬 CLI 댓글 입력은 유지한다. 이는 웹 댓글에서 런타임을 사칭하는 입력을 막는 경계이며, DB나 로컬 CLI를 조작할 권한이 있는 계정 소유자로부터 사용량을 인증하는 장치는 아니다. 기존 댓글에 대한 소급 진위 판정을 주장하지 않는다.

보관된 카드는 기본 합계에 남는다. 실제로 제거/이동되어 일반 보드 목록에서 사라진 보드와 영구 삭제된 카드는 복원 집계하지 않는다. 보드마다 별도 읽기 트랜잭션이므로 전체보드는 전역 동시 스냅샷이 아니다.

## API

`GET /api/plugins/kanban/daily`는 기존 `/board`, `/tasks/...` 응답을 변경하지 않는 추가 endpoint다. 기존 Dashboard 인증 middleware 안에 mounted된다.

| query | 기본·허용값 |
| --- | --- |
| board | 기존 slug, 생략 시 현재 보드 |
| scope | current / all |
| period | today / yesterday / 7d / month / custom / all |
| start, end | custom에서 YYYY-MM-DD |
| timezone | Asia/Seoul, 유효한 IANA 이름 |
| basis | created / completed / execution |
| q | 제목·본문, 최대 500자 |
| source | all / hermes-agent / claude-code / codex |
| status | all / 기존 상태(보관·scheduled 포함) |
| day | 목록 한정 YYYY-MM-DD; period가 all이 아니면 해당 기간 밖은 400 |
| limit, offset | 기본 100/0, limit 1~500, offset 0~10000000 |

응답은 `timezone, period, start, end, basis, scope, boards, summary, days, tasks, task_ids, total, limit, offset, has_more`다. `tasks`만 pagination하며 `task_ids`는 50,000행 요청 상한 안에서 필터용 전체 ID를 원자적으로 반환한다. `summary`와 `days`의 `completion_attributed`는 완료 snapshot을 별도로 보여 주며 `measured`에 더하지 않는다. start/end는 Unix 초 또는 전체기간 null. summary는 `created, completed, matched_cards, token_usage, measured, completion_attributed, estimated, undated_events, untracked_cards`. days는 `date, created, completed, cards, token_usage, measured, completion_attributed, estimated`. 사용량 묶음은 `tokens, event_count, tracked_tasks, reasoning_coverage, bucket_coverage, overflow_fields`다. 각 task는 board를 포함하며 전체보드 key는 board/id다.

새 조회는 SQLite `mode=ro`, `query_only=ON`, BEGIN 읽기 트랜잭션만 사용한다. DB init/migration·인덱스 쓰기를 부르지 않는다. SQL 값은 바인딩한다. 카드의 작은 메타데이터와 런타임 작성자·사용량 header에 해당하는 댓글만 해석하고, 전체 카드 본문·결과는 최종 페이지에 대해서만 같은 보드 스냅샷에서 읽는다. 변경 없는 보드는 프로세스 캐시의 해석된 사용량을 재사용하며 data_version과 파일 inode로 수정·삭제·DB 교체를 감지한다.

첫 조회·DB 변경 후에는 이력 scan이 필요하며, 제목·본문 부분검색도 인덱스 검색이라고 주장하지 않는다. 요청 전체(모든 보드 합산)에 50,000개 메타데이터/사용량 후보 행, 계상 입력 32MiB, 단일 값·반환 카드 행 1MiB, SQLite 약 5백만 VM 단계, 동시 보드 연결 64개 상한을 둔다. 보관 캐시는 최대 8개 보드·계상 입력 32MiB이며 실제 Python 객체 메모리가 정확히 32MiB라는 뜻은 아니다. 캐시 잠금은 최대 1초 대기한다. 예산 초과는 HTTP 503이며 summary나 부분합을 반환하지 않는다. 이 경우 보드 범위를 줄이고, 큰 단일 보드는 유지관리자가 별도로 조사해야 한다. 기간·limit 축소만으로 전체 이력 비용이 사라진다고 안내하지 않는다.

격리된 과거 카드 2,000개와 댓글 4,000개 회귀에서 전체 행 전송은 최종 페이지 한 건, 일반 댓글의 파서 입력은 0건, 변경 없는 재조회에서 과거 사용량 재해석은 0건임을 확인했다. 무제한 규모 성능이나 운영 데이터의 응답시간을 검증한 것은 아니다. 오래된 카드의 오늘 실측 사용량과 기간 전체 합계는 계속 포함하며 임의 truncate는 하지 않는다.

제목·본문 검색은 SQL의 지연 CASE에서 UTF-8 바이트 길이를 먼저 검사한다. 상한을 넘으면 Python casefold UDF에 거대 문자열을 전달하지 않고 거부한다. 허용된 문자열의 Unicode 대소문자 무시 검색, 한글·문자 그대로의 `%`/`_` 검색과 NULL 본문은 유지한다. 이는 Python 전달 전 제한이며 SQLite 자체의 내부 메모리 할당이 전혀 없다는 보장은 아니다.

`HERMES_KANBAN_DB`가 있으면 전체보드는 403이다. current 조회도 명시 `HERMES_KANBAN_BOARD` identity가 필요하고 다른 board 선택은 403으로 거부한다. 임의 DB 경로에 보드 이름을 추측해 붙이지 않는다. 별도 보드별 ACL을 신설하지 않으며 기존 인증/보드 접근 범위를 따른다.

## 과거 검증 및 현재 후보

공식 frozen source: `f03ed94a34f47ebca57e4a1b0a890bc2aeb5e140`.
기존 carried 8개 마지막: `f9ab37d661d16a09014a0859f49064e8ca1ed7b8`.
전용 clone: `/tmp/unified-kanban-daily-search-hermes`.
프로젝트 기준: `e54c0b64c67f427f735ac752d7f964aa6f496e62`.

위 source/clone/프로젝트 기준은 과거 검색 후보의 기록이며 [과거 검증](daily-search-verification.md)을 따른다. 현재 단순 달력 후보는 프로젝트 `821289a3e5a8d55652c5fd31ad2dd4cbbf38808b`와 Hermes `3b76df96b074d192d22b95e0d181ab8eac12b063` 위에서 별도 구현한다. 실제 검증 결과는 [달력 후보 검증](compact-calendar-verification.md)에 기록한다. 테스트 fixture의 DOM 성공은 실제 서버 E2E와 구분하며 승인 차단된 실행을 우회하지 않는다.
