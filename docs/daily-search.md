# 날짜·검색 Kanban 후보

초기 후보 검증 완료 시점(2026-09-09T08:21:37Z)에는 운영 미적용이었다. 저장소 게시와 사용자 머신 활성화는 별도 검증이며, 아래 설명은 날짜·검색 기능의 계약이다. 운영 적용 여부는 해당 배포의 실제 설치 경로와 서비스 검증 결과로 판단한다.

## 사용 기준

기본은 현재 보드·오늘·생성일, 시간대 Asia/Seoul이다. 오늘 이전 카드를 숨기지만 삭제하지 않는다. 전체기간으로 과거 카드를 찾는다. 생성일/완료일은 카드 목록의 기준이고 생성수/완료수는 항상 각각의 시각으로 센다. 어제 생성하여 오늘 완료한 카드는 오늘 완료수에 포함된다. 현재 완료시각 필드 기준이며 과거 모든 완료 전환 횟수를 재구성하지 않는다.

오늘, 어제, 최근 7일(오늘 포함), 이번 달(월초~오늘), 직접 선택, 전체기간을 지원한다. 달력의 시작·종료 날짜는 모두 포함하되 서버 내부 시각 구간은 [start,end), 종료일 다음날 자정 미포함이다. IANA 시간대의 DST를 따르며 자정에 고정 86400초를 더하지 않는다. 날짜 형식은 YYYY-MM-DD, 직접 선택은 최대 3661일이다. 전체기간 표는 이벤트/카드가 있는 날짜만, 유한 기간 표는 빈 날짜도 카드 0으로 제공한다.

검색은 제목·본문의 대소문자 무시 부분일치다. `%`, `_`, SQL 문자열도 문자 그대로 검색한다. 상태·작업도구·검색어는 카드 수와 토큰 양쪽에 동일 적용한다. 기본 상태는 보관 카드 포함이며 상태 선택으로 한정한다. 작업도구는 신뢰된 사용량 출처 우선, 미수집은 어댑터 created_by·tenant/관찰 여부로 분류한다. 제목이나 모델 이름으로 작업도구를 추측하지 않는다.

날짜 클릭은 목록만 한정하고 상단/표의 선택기간 요약은 유지한다. `matched_cards`는 기간 전체 목록 수, `total`은 날짜 클릭까지 적용한 목록 전체 수다. 페이지는 그 목록의 일부다. 브라우저는 현재 페이지 카드의 토큰을 더해 전체 합계로 쓰지 않는다. 기존 카드 편집/이동/관찰 보호는 명시적인 ‘전체 보드 조작 보기’에서 유지한다. 이 보기에는 일별 검색 필터가 적용되지 않는다.

React에서 다른 보드의 결과 상세를 열어도 검색 보드·직접 기간·날짜 선택·페이지를 유지한다. 상세의 조회·수정 요청만 해당 결과의 보드로 고정한다. 성공한 카드·보드 변경과 수신한 보드 socket 이벤트는 전체 일별 검색 캐시를 무효화한다. 선택 보드 밖에서 수신하지 않은 변경은 60초 주기 갱신이나 새로고침으로 확인한다. 일별 토큰 상세에서 실측/추정 각각의 bucket·일부 수집·이벤트 수·수집 카드 수를 펼쳐 볼 수 있다. Legacy 검색어 입력은 마지막 입력 뒤 300ms 지연으로 묶는다.

## 토큰의 시각·미수집

현재 producer `src/kanban_adapter/usage.py:usage_comment`, Claude/Codex 누적 snapshot/delta, Hermes hook의 댓글에는 실제 사용시각이 없다. 원래 v1/v2 기록은 토큰 수치가 정확하더라도 날짜 귀속은 **생성일 추정**이다. `task_comments.created_at`은 저장시각이며 실제 사용시각으로 쓰지 않는다. 운영 댓글 원문/PII를 읽어 확인하지 않고 소스로 추적했다.

추가 v2 `usage_at` 필드는 명시적으로 제공된 Unix 정수 초만 허용한다. 현재 어댑터가 이 필드를 자동 수집한다고 주장하지 않는다. 시연의 실측시각 fixture도 **시연 데이터**이며 실제 제공자 호출 비용/사용량이 아니다. 신뢰된 v2 필드가 있을 때 `measured`, 없으면 생성일 `estimated`로 분리한다. 잘못된 명시 시각이나 귀속할 수 없는 생성시각은 `undated_events`로 별도 보고하며 기간 토큰에는 넣지 않는다. v1의 동명 추가 필드는 시각 계약이 아니므로 무시한다.

토큰 기간은 목록 날짜 기준·날짜 클릭·페이지와 독립이다. 생성일이 기간 밖이어도 명시된 사용시각이 기간 안이면 토큰은 포함될 수 있다. 반대로 완료일이 오늘이어도 usage_at이 없고 생성일이 어제면 오늘의 추정 토큰에는 포함되지 않는다. 카드 상세/‘카드 누적 토큰’은 기간과 무관한 누적값이므로 목록에 보이는 누적 토큰의 합이 기간 요약과 다를 수 있다.

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
| basis | created / completed |
| q | 제목·본문, 최대 500자 |
| source | all / hermes-agent / claude-code / codex |
| status | all / 기존 상태(보관·scheduled 포함) |
| day | 목록 한정 YYYY-MM-DD |
| limit, offset | 기본 100/0, limit 1~500, offset 0~10000000 |

응답은 `timezone, period, start, end, basis, scope, boards, summary, days, tasks, total, limit, offset, has_more`다. start/end는 Unix 초 또는 전체기간 null. summary는 `created, completed, matched_cards, token_usage, measured, estimated, undated_events, untracked_cards`. days는 `date, created, completed, cards, token_usage, measured, estimated`. 사용량 묶음은 `tokens, event_count, tracked_tasks, reasoning_coverage, bucket_coverage, overflow_fields`다. 각 task는 board를 포함하며 전체보드 key는 board/id다.

새 조회는 SQLite `mode=ro`, `query_only=ON`, BEGIN 읽기 트랜잭션만 사용한다. DB init/migration·인덱스 쓰기를 부르지 않는다. SQL 값은 바인딩한다. 카드의 작은 메타데이터와 런타임 작성자·사용량 header에 해당하는 댓글만 해석하고, 전체 카드 본문·결과는 최종 페이지에 대해서만 같은 보드 스냅샷에서 읽는다. 변경 없는 보드는 프로세스 캐시의 해석된 사용량을 재사용하며 data_version과 파일 inode로 수정·삭제·DB 교체를 감지한다.

첫 조회·DB 변경 후에는 이력 scan이 필요하며, 제목·본문 부분검색도 인덱스 검색이라고 주장하지 않는다. 요청 전체(모든 보드 합산)에 50,000개 메타데이터/사용량 후보 행, 계상 입력 32MiB, 단일 값·반환 카드 행 1MiB, SQLite 약 5백만 VM 단계, 동시 보드 연결 64개 상한을 둔다. 보관 캐시는 최대 8개 보드·계상 입력 32MiB이며 실제 Python 객체 메모리가 정확히 32MiB라는 뜻은 아니다. 캐시 잠금은 최대 1초 대기한다. 예산 초과는 HTTP 503이며 summary나 부분합을 반환하지 않는다. 이 경우 보드 범위를 줄이고, 큰 단일 보드는 유지관리자가 별도로 조사해야 한다. 기간·limit 축소만으로 전체 이력 비용이 사라진다고 안내하지 않는다.

격리된 과거 카드 2,000개와 댓글 4,000개 회귀에서 전체 행 전송은 최종 페이지 한 건, 일반 댓글의 파서 입력은 0건, 변경 없는 재조회에서 과거 사용량 재해석은 0건임을 확인했다. 무제한 규모 성능이나 운영 데이터의 응답시간을 검증한 것은 아니다. 오래된 카드의 오늘 실측 사용량과 기간 전체 합계는 계속 포함하며 임의 truncate는 하지 않는다.

제목·본문 검색은 SQL의 지연 CASE에서 UTF-8 바이트 길이를 먼저 검사한다. 상한을 넘으면 Python casefold UDF에 거대 문자열을 전달하지 않고 거부한다. 허용된 문자열의 Unicode 대소문자 무시 검색, 한글·문자 그대로의 `%`/`_` 검색과 NULL 본문은 유지한다. 이는 Python 전달 전 제한이며 SQLite 자체의 내부 메모리 할당이 전혀 없다는 보장은 아니다.

`HERMES_KANBAN_DB`가 있으면 전체보드는 403이다. current 조회도 명시 `HERMES_KANBAN_BOARD` identity가 필요하고 다른 board 선택은 403으로 거부한다. 임의 DB 경로에 보드 이름을 추측해 붙이지 않는다. 별도 보드별 ACL을 신설하지 않으며 기존 인증/보드 접근 범위를 따른다.

## 검증·후보 신원

공식 frozen source: `f03ed94a34f47ebca57e4a1b0a890bc2aeb5e140`.
기존 carried 8개 마지막: `f9ab37d661d16a09014a0859f49064e8ca1ed7b8`.
전용 clone: `/tmp/unified-kanban-daily-search-hermes`.
프로젝트 기준: `e54c0b64c67f427f735ac752d7f964aa6f496e62`.

최종 후보 SHA, 실제 검증 명령/결과, 로그와 screenshot은 [검증 결과](daily-search-verification.md)에 기록한다. 시연 주소는 `http://127.0.0.1:19139/kanban`, HOME/DB는 `/tmp/unified-kanban-daily-search-demo/home/.hermes`이다. 테스트 mock의 DOM 성공은 실제 서버 E2E와 구분한다. 승인 차단된 브라우저 launch를 스크립트로 우회한 이전 증거는 최종 승인 근거에서 제외한다.
