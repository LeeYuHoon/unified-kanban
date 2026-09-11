# 토큰 비용 기준

Kanban의 비용은 청구서가 아닙니다. 제공자가 명시적인 USD 계약으로 기록한 비용은 `$…`로 표시합니다. API 환산 추정치 `≈ $…`는 신규 요청 이벤트에 모델·단가 버전·문맥 범위·수집 시각 근거가 모두 기록된 경우에만 표시합니다. 구독·OAuth 요금과 실제 청구액은 다를 수 있습니다.

## 고정 단가 스냅샷

조회일: 2026-09-11

- Anthropic 공식 가격: https://platform.claude.com/docs/en/about-claude/pricing
- Anthropic 공식 모델 ID: https://platform.claude.com/docs/en/about-claude/models/model-ids-and-versions
- Anthropic 1M beta 종료 근거: https://platform.claude.com/docs/en/release-notes/overview
- OpenAI 공식 API 가격: https://openai.com/api/pricing/
- OpenAI GPT-5.4 모델 문서: https://developers.openai.com/api/docs/models/gpt-5.4

금액은 100만 토큰당 USD입니다.

| 제공자 | 정확히 지원하는 모델 ID | Input | Cache read | Cache write | Output |
|---|---|---:|---:|---:|---:|
| Anthropic | `claude-sonnet-4-5`, `claude-sonnet-4-5-20250929` | 3 | 0.3 | N/A | 15 |
| OpenAI | `gpt-5.4`, `gpt-5.4-2026-03-05` | 2.5 | 0.25 | N/A | 15 |

- Claude Sonnet 4.5의 1M context beta는 2026-04-30 종료됐습니다. 이 구현은 요청별 input+cache가 200K 이하이며 신규 수집 근거가 있는 표준 tier만 지원하고, 폐기된 장문 단가는 사용하지 않습니다. 단가 버전은 `anthropic-direct-standard-post-1m-retirement-2026-09-11`입니다.
- GPT-5.4 tier는 개별 호출이 아니라 full session input 기준입니다. 명시적 full-session input 근거가 있고 272K 이하인 신규 이벤트만 표준 단가를 적용합니다. 단독 요청이나 누적 aggregate로 tier를 추측하지 않으며, 272K 초과 tier는 이 스냅샷에서 지원하지 않습니다. 단가 버전은 `openai-direct-standard-2026-09-11`입니다.
- Anthropic cache write 가격은 5분/1시간 TTL에 따라 다르지만 현재 usage 이벤트에는 TTL이 없습니다. 따라서 cache write 비용은 `N/A`이며 임의 요율을 적용하지 않습니다.
- OpenAI의 현재 이벤트 계약에는 별도 cache write 의미가 없습니다. 값이 들어와도 추정하지 않습니다.
- 표에 없는 모델은 유사한 이름으로 매핑하지 않습니다. 예를 들어 `gpt-6-astra`와 `claude-opus-5`는 이 고정 스냅샷의 지원 ID가 아니므로 `N/A`입니다.

## 집계 규칙

1. finite·0 이상이며 통화와 provider-recorded 의미가 명시된 USD 비용만 총액에 한 번 사용합니다. 임의 `usage.cost`를 USD로 간주하지 않습니다. component 의미가 명시되지 않으면 Input·Cache read·Cache write·Output 비용은 `N/A`입니다.
2. 추정은 schema v2 신규 request 이벤트에 공식 direct API route/host, exact provider/model, 가격·모델 출처, pricing version, usage scope, context tier/input/limit, tier 근거, 수집 시각이 모두 있을 때만 계산합니다.
3. reasoning token은 Output에 포함된 값이므로 별도 비용으로 더하지 않습니다.
4. provider total과 component subtotal, lifetime과 선택일 subtotal을 서로 더하지 않습니다.
5. 일부 component만 계산할 수 있으면 확인된 금액과 `일부` coverage를 같이 보입니다. provider의 partial total은 complete로 승격하지 않습니다.
6. malformed·음수·무한대·USD가 아닌 비용은 `$0`으로 바꾸지 않고 `N/A` 처리합니다. 토큰 사용량 자체는 보존합니다.
7. 과거 DB를 소급 계산하거나 수정하지 않습니다. 비용이 없는 역사 이벤트는 exact model이어도 `N/A`입니다. 손상 비용 sentinel은 누락과 구분해 보존하며 추정값으로 대체하지 않습니다.
8. 100K input+10K output 요청 4개를 400K+40K 단일 요청처럼 재가격하지 않습니다. GPT full-session 근거가 없으면 비용은 `N/A`입니다.
9. 큰 정수나 여러 금액의 합이 binary64 범위를 넘으면 `Infinity`를 내보내지 않고 비용은 `N/A`, 토큰은 원래 값으로 유지합니다.

## 표시 규칙

- provider 기록값: `$87.3`
- 공식 API 환산 추정: `≈ $87.3`
- 1센트 미만 양수: `<$0.01` 또는 `≈ <$0.01`
- 정확한 0: `$0`
- 근거 없음: `N/A`
- 일부만 계산: `≈ $87.3 · 일부`

API 내부에서는 금액을 USD 숫자로 유지하며 화면에 `USD` 글자를 붙이지 않습니다.
