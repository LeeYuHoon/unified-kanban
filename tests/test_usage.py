from __future__ import annotations

import json

import pytest

from kanban_adapter.usage import (
    USAGE_CATEGORIES,
    bump,
    classify_subagent,
    classify_tool,
    clean_tokens,
    clean_usage,
    concise_summary,
    sanitize_identifier,
    sanitize_model,
    unavailable_categories,
    usage_comment,
    usage_comment_header,
    usage_event_id,
)


def test_clean_tokens_normalizes_buckets_and_preserves_provider_total() -> None:
    assert clean_tokens({
        "input": 11,
        "output": 7,
        "cache_read": 13,
        "cache_write": 17,
        "reasoning": None,
        "requests": 3,
        "total": 999_999,
        "secret": 42,
    }) == {
        "input": 11,
        "output": 7,
        "cache_read": 13,
        "cache_write": 17,
        "reasoning": None,
        "requests": 3,
        "total": 999_999,
    }


def test_clean_tokens_computes_total_when_provider_total_is_invalid() -> None:
    assert clean_tokens({"input": 11, "output": 7, "total": -1}) == {
        "input": 11,
        "output": 7,
        "total": 18,
    }


@pytest.mark.parametrize("bad", [-1, True, 1.5, "12", 10**18])
def test_clean_tokens_drops_invalid_counts(bad) -> None:
    assert clean_tokens({"input": bad, "output": 5}) == {
        "output": 5,
        "total": 5,
    }


def test_usage_comment_includes_sanitized_token_breakdown() -> None:
    payload = json.loads(usage_comment(
        source="claude-code",
        model="claude-opus-5",
        usage={},
        tokens={
            "input": 2,
            "output": 5,
            "cache_read": 7,
            "cache_write": 11,
            "reasoning": None,
            "requests": 4,
        },
    ).split("\n", 1)[1])
    assert payload["tokens"] == {
        "input": 2,
        "output": 5,
        "cache_read": 7,
        "cache_write": 11,
        "reasoning": None,
        "requests": 4,
        "total": 25,
    }


# 적대적이거나 잘못된 런타임이 이름 자리에 넣을 수 있는 값이다. 어느 것도 유효한
# 식별자가 아니므로 상태나 주석에 절대 도달해서는 안 된다.
HOSTILE_NAMES = [
    "/Users/alice/private/key.pem",
    "../../etc/passwd",
    "..",
    "./relative",
    "~/.ssh/id_rsa",
    "C:\\Users\\alice\\secret.txt",
    "we\nird",
    "tab\tname",
    "nul\x00byte",
    "bell\x07",
    "\u202eevil",
    "name with spaces",
    "skill; rm -rf /",
    "sk-ant-api03-" + "A" * 40,
    "0123456789abcdef0123456789abcdef",
    "x" * 200,
    "trailing-",
    "-leading",
]


# 식별자 문법상 *유효*하고 32자 블롭 규칙을 빠져나갈 만큼 짧지만 비밀처럼 생긴
# 불투명 토큰이다. 보고 가치가 없고 자격 증명일 수 있으므로 즉시 거부해야 한다.
OPAQUE_NAMES = [
    # 검토자가 제시한 예: 29자이며 대소문자가 번갈아 나오고 숫자는 적다.
    "AbCdEfGhIjKlMnOpQrStUvWxYz123",
    # AWS 형태의 액세스 키 ID: 20자이며 모두 대문자이고 숫자는 없다.
    "AKIAABCDEFGHIJKLMNOP",
    "AKIAIOSFODNN7EXAMPLE",
    "aKbLcMdNeOfPgQhRiSjT",
    "XyZaBcDeFgHiJkLmNoPq",
    "GHIJKLMNOPQRSTUVWXYZ",
    # 대소문자와 숫자가 섞여 있지만 여전히 32자 미만이다.
    "a1B2c3D4e5F6g7H8i9J0",
    "Tk3nAbCd5EfGh7IjKl9M",
]


@pytest.mark.parametrize("name", OPAQUE_NAMES)
def test_opaque_secret_like_names_are_rejected(name: str) -> None:
    """``sanitize_identifier``에서 거부되므로 분류와 오래된 상태 정리 모두 별도
    규칙 없이 이 값을 거부한다."""
    assert sanitize_identifier(name) is None
    assert classify_tool("claude-code", "Skill", {"skill": name}) is None
    assert classify_tool("claude-code", "Task", {"subagent_type": name}) is None
    assert classify_tool("hermes-agent", "skill_view", {"name": name}) is None
    assert classify_tool("claude-code", f"mcp__{name}__read", {}) is None
    assert classify_tool("claude-code", f"mcp__github__{name}", {}) is None
    assert classify_subagent(name) is None
    assert clean_usage({"skills": {name: 3}, "mcp": {f"github/{name}": 1}}) == {}
    assert name not in usage_comment(
        source="claude-code", model=None, usage={"skills": {name: 3}}
    )


@pytest.mark.parametrize(
    "name",
    [
        # 검토자가 강화한 규칙이 잡아서는 안 되는 실제 이름이다. 길지만 고엔트로피가
        # 아닌 단어 형태다.
        "openaiDeveloperDocs",
        "general-purpose",
        "plugin:skill",
        "fetch_openai_doc",
        "searchIssuesInRepositoryByLabel",
        "DeveloperDocumentation",
        "getUserAccountSettings",
        "claude-api",
        "gpt5CodexReviewer",
    ],
)
def test_legitimate_identifiers_survive_the_opaque_filter(name: str) -> None:
    assert sanitize_identifier(name) == name
    assert classify_subagent(name) == name
    assert clean_usage({"skills": {name: 2}}) == {"skills": {name: 2}}


def test_usage_categories_are_stable() -> None:
    assert USAGE_CATEGORIES == ("skills", "subagents", "mcp")


@pytest.mark.parametrize(
    ("runtime", "tool_name", "tool_input", "expected"),
    [
        ("claude-code", "Skill", {"skill": "dataviz"}, ("skills", "dataviz")),
        ("claude-code", "Task", {"subagent_type": "Explore"}, ("subagents", "Explore")),
        ("claude-code", "Bash", {"command": "rm -rf /"}, None),
        ("hermes-agent", "skill_view", {"name": "axolotl"}, ("skills", "axolotl")),
        ("hermes-agent", "skill_view", {"name": "a", "file_path": "refs/x.md"}, ("skills", "a")),
        ("hermes-agent", "skills_list", {}, None),
        # delegate_task는 N개의 자식으로 분기하며 subagent_start가 권위 있는 기준이다.
        ("hermes-agent", "delegate_task", {"goal": "secret goal"}, None),
        ("hermes-agent", "terminal", {"command": "cat /etc/passwd"}, None),
        ("codex", "exec_command", {"command": "ls"}, None),
        ("codex", "spawn_agent", {"prompt": "secret"}, None),
        # MCP 명명 규칙은 세 런타임이 공유한다.
        ("claude-code", "mcp__github__search_issues", {}, ("mcp", "github/search_issues")),
        ("hermes-agent", "mcp__linear__get_issue", {}, ("mcp", "linear/get_issue")),
        ("codex", "mcp__openaiDeveloperDocs__fetch_openai_doc", {},
         ("mcp", "openaiDeveloperDocs/fetch_openai_doc")),
    ],
)
def test_classify_tool_per_runtime(runtime, tool_name, tool_input, expected) -> None:
    assert classify_tool(runtime, tool_name, tool_input) == expected


def test_classify_tool_falls_back_to_unknown_name() -> None:
    """구조적으로 이름이 없으면 유출할 내용도 없으므로 계속 집계한다."""
    assert classify_tool("claude-code", "Skill", {}) == ("skills", "unknown")
    assert classify_tool("hermes-agent", "skill_view", {"name": 7}) == ("skills", "unknown")
    assert classify_tool("codex", "mcp__srv__", {}) == ("mcp", "srv/unknown")


def test_classify_tool_rejects_non_string_tool_names() -> None:
    assert classify_tool("claude-code", None, {}) is None
    assert classify_tool("claude-code", "", {}) is None


@pytest.mark.parametrize(
    "name",
    [
        "dataviz",
        "claude-api",
        "general-purpose",
        # 플러그인 한정 스킬은 변경 없이 유지되어야 한다.
        "kanban:review",
        "my-plugin:deploy-app",
        "a.b_c-d:e",
        "Explore",
        "u",
        "unknown",
        "openaiDeveloperDocs",
        "searchIssuesInRepositoryByLabel",
        ("long-" * 12 + "name")[:64],
    ],
)
def test_legitimate_identifiers_are_preserved(name: str) -> None:
    assert sanitize_identifier(name) == name
    assert classify_tool("claude-code", "Skill", {"skill": name}) == ("skills", name)
    assert classify_subagent(name) == name


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_hostile_identifiers_are_rejected_outright(name: str) -> None:
    """``unknown``으로 저장하지 않고 거부한다. 잘못된 이름은 집계하면 안 된다."""
    assert sanitize_identifier(name) is None
    assert classify_tool("claude-code", "Skill", {"skill": name}) is None
    assert classify_tool("hermes-agent", "skill_view", {"name": name}) is None
    assert classify_tool("claude-code", "Task", {"subagent_type": name}) is None
    assert classify_subagent(name) is None


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_hostile_mcp_segments_are_rejected(name: str) -> None:
    assert classify_tool("claude-code", f"mcp__{name}__read", {}) is None
    assert classify_tool("claude-code", f"mcp__github__{name}", {}) is None


def test_mcp_names_keep_exactly_one_structural_slash() -> None:
    assert classify_tool("claude-code", "mcp__github__search_issues", {}) == (
        "mcp",
        "github/search_issues",
    )
    # 구분자는 구조적 요소로 세그먼트의 일부가 아니며 일반 식별자에서는 절대
    # 유효하지 않다. 덕분에 경로가 스킬 이름에 들어오지 못한다.
    assert sanitize_identifier("srv/tool") is None
    assert classify_tool("claude-code", "Skill", {"skill": "srv/tool"}) is None
    assert classify_subagent("srv/tool") is None
    assert classify_tool("claude-code", "mcp__a/b__c", {}) is None
    assert classify_tool("claude-code", "mcp__a__b/c", {}) is None


def test_classify_subagent_sanitizes_names() -> None:
    assert classify_subagent("Explore") == "Explore"
    assert classify_subagent("  leaf  ") == "leaf"
    assert classify_subagent(None) == "unknown"
    assert classify_subagent("") == "unknown"
    assert classify_subagent(7) == "unknown"
    assert classify_subagent("a\nb\tc") is None
    assert classify_subagent("x" * 200) is None


def test_bump_counts_into_nested_buckets() -> None:
    usage: dict = {}
    bump(usage, "skills", "dataviz")
    bump(usage, "skills", "dataviz")
    bump(usage, "mcp", "github/x")
    assert usage == {"skills": {"dataviz": 2}, "mcp": {"github/x": 1}}


def test_clean_usage_drops_unknown_categories_and_bad_counts() -> None:
    assert clean_usage(
        {
            "skills": {"good": 2, "zero": 0, "neg": -1, "bool": True, "str": "3"},
            "bogus": {"x": 1},
            "mcp": "not-a-dict",
            "subagents": {"ok": 1},
        }
    ) == {"skills": {"good": 2}, "subagents": {"ok": 1}}
    assert clean_usage(None) == {}
    assert clean_usage({"skills": {}}) == {}


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_clean_usage_drops_invalid_stale_names(name: str) -> None:
    """오래된 상태는 신뢰할 수 없는 입력이므로 적대적 키는 절대 남지 않는다."""
    cleaned = clean_usage(
        {"skills": {name: 3, "dataviz": 1}, "subagents": {name: 2}, "mcp": {name: 4}}
    )
    assert cleaned == {"skills": {"dataviz": 1}}
    assert name not in json.dumps(cleaned)


def test_clean_usage_enforces_the_mcp_two_segment_shape() -> None:
    assert clean_usage(
        {
            "mcp": {
                "github/search_issues": 1,
                "a/b/c": 2,
                "/leading": 3,
                "trailing/": 4,
                "bare": 5,
            }
        }
    ) == {"mcp": {"github/search_issues": 1}}
    # 슬래시는 MCP에서만 구조적 요소이며 유효한 스킬 이름이 아니다.
    assert clean_usage({"skills": {"github/search_issues": 1}}) == {}


def test_clean_usage_merges_valid_name_collisions_instead_of_overwriting() -> None:
    assert clean_usage(
        {"skills": {" dataviz ": 2, "dataviz": 3, "dataviz\n": 4}}
    ) == {"skills": {"dataviz": 9}}
    assert clean_usage({"mcp": {"github/x": 1, " github/x ": 2}}) == {
        "mcp": {"github/x": 3}
    }


def test_clean_usage_caps_merged_counts() -> None:
    huge = 10**18
    cleaned = clean_usage({"skills": {"dataviz": huge, " dataviz": huge}})
    assert cleaned["skills"]["dataviz"] == 1_000_000


def test_sanitize_model_accepts_known_shapes_and_rejects_junk() -> None:
    assert sanitize_model("claude-fable-5") == "claude-fable-5"
    assert sanitize_model("gpt-5.6-sol") == "gpt-5.6-sol"
    assert sanitize_model("us.anthropic.claude:1") == "us.anthropic.claude:1"
    assert sanitize_model("openai/gpt-5.6-sol") == "openai/gpt-5.6-sol"
    assert sanitize_model("  gpt-5.6-sol  ") == "gpt-5.6-sol"
    assert sanitize_model("") is None
    assert sanitize_model(None) is None
    assert sanitize_model(123) is None
    assert sanitize_model("bad model with spaces") is None
    assert sanitize_model("x" * 200) is None


def test_unavailable_categories_documents_runtime_limits() -> None:
    assert unavailable_categories("claude-code") == ()
    assert unavailable_categories("hermes-agent") == ()
    # Codex 0.145 훅 이벤트에는 스킬 이름이 들어 있지 않다.
    assert unavailable_categories("codex") == ("skills",)
    assert unavailable_categories("something-else") == ()


def _decode(comment: str) -> dict:
    header, _, body = comment.partition("\n")
    payload = json.loads(body)
    assert header == usage_comment_header(payload["source"])
    # 모든 주석은 자체 설명적이다. 아래의 필드별 검증이 사용량 데이터 자체에만
    # 집중하도록 여기서 버전을 한 번 검증한다.
    expected_version = 2 if "tokens" in payload else 1
    assert payload.pop("schema_version") == expected_version
    return payload


def test_claude_header_stays_byte_for_byte_compatible() -> None:
    """기존 Claude 전용 주석을 기준으로 작성된 소비자도 계속 동작한다."""
    comment = usage_comment(
        source="claude-code",
        model="claude-opus-5",
        usage={"skills": {"dataviz": 1}},
        unavailable=(),
    )
    header, newline, body = comment.partition("\n")
    # 최초 배포 버전이 출력한 헤더를 정확히 재현한다.
    assert header == "Agent tool usage"
    assert newline == "\n"
    legacy = json.loads(body)
    assert legacy["source"] == "claude-code"
    assert legacy["skills"] == {"dataviz": 1}
    assert legacy["subagents"] == {}
    assert legacy["mcp"] == {}


def test_headers_are_source_specific() -> None:
    # Claude Code가 먼저 배포되어 중립적인 원본 헤더를 유지하며, 이후 추가된 소스만
    # 자체 이름을 표시할 수 있다.
    assert usage_comment_header("claude-code") == "Agent tool usage"
    assert usage_comment_header("codex") == "Codex tool usage"
    assert usage_comment_header("hermes-agent") == "Hermes Agent tool usage"
    assert usage_comment_header("something-else") == "Agent tool usage"


def test_usage_event_id_is_deterministic_per_card() -> None:
    first = usage_event_id("claude-code", "t_12345678")
    assert first == usage_event_id("claude-code", "t_12345678")
    assert first != usage_event_id("claude-code", "t_87654321")
    assert first != usage_event_id("codex", "t_12345678")
    assert first.startswith("usage-")
    # 마커는 일반 토큰이므로 주석 본문에서 안전하게 검색할 수 있다.
    assert first == "".join(first.split())


def test_usage_comment_carries_the_event_marker_when_given_one() -> None:
    event_id = usage_event_id("hermes-agent", "t_12345678")
    comment = usage_comment(
        source="hermes-agent",
        model=None,
        usage={},
        unavailable=(),
        event_id=event_id,
    )
    assert event_id in comment
    assert _decode(comment)["event_id"] == event_id


@pytest.mark.parametrize(
    "event_id",
    [
        "a1234567",  # 가장 짧은 유효 ID: 8자
        "usage-" + "0" * 32,  # usage_event_id 자체가 생성하는 값
        "A" + "b.c-d_e" * 18 + "f",  # 가장 긴 유효 ID인 128자
    ],
)
def test_usage_comment_keeps_event_ids_inside_the_grammar(event_id: str) -> None:
    comment = usage_comment(
        source="claude-code", model=None, usage={}, event_id=event_id
    )
    assert _decode(comment)["event_id"] == event_id


@pytest.mark.parametrize(
    "event_id",
    [
        "short7",  # 6자: 최소 길이 8자 미만
        "abcdefg",  # 7자: 여전히 한 자 부족함
        "-leading",  # 영숫자로 시작해야 함
        "has space",
        "has:colon",  # 멱등성 문법에는 ``:``가 없음
        "../../etc/passwd",
        "line\nbreak",
        "nul\x00byte",
        "usage-" + "0" * 200,  # 206자: 최대 길이 128자를 초과함
        7,  # 문자열이 전혀 아님
        "",
    ],
)
def test_usage_comment_omits_event_ids_outside_the_grammar(event_id) -> None:
    """잘못된 마커는 렌더링하지 않고 버린다. 멱등성 키로 인정할 수 없으며 이를
    그대로 출력하면 검증되지 않은 텍스트가 카드에 들어간다."""
    comment = usage_comment(
        source="claude-code",
        model="claude-opus-5",
        usage={"skills": {"dataviz": 1}},
        event_id=event_id,
    )
    payload = _decode(comment)
    assert "event_id" not in payload
    if event_id:
        assert str(event_id) not in comment
    assert payload["skills"] == {"dataviz": 1}


def test_usage_comment_survives_a_10000_char_event_id() -> None:
    """회귀 테스트: 지나치게 큰 마커는 유출되거나 주석을 제한 밖으로 밀어내거나
    유효하지 않은 JSON으로 만들어서는 안 된다."""
    event_id = "u" + "a1" * 4999 + "z"
    assert len(event_id) == 10_000
    comment = usage_comment(
        source="hermes-agent",
        model="claude-fable-5",
        usage=_all_categories_at_limit(),
        unavailable=("skills",),
        event_id=event_id,
    )
    assert len(comment) <= 4000
    payload = _decode(comment)  # 잘린 조각이 아니라 여전히 파싱 가능한 JSON이다.
    assert "event_id" not in payload
    assert event_id not in comment
    assert event_id[:200] not in comment


def test_usage_comment_records_source_model_and_all_categories() -> None:
    comment = usage_comment(
        source="hermes-agent",
        model="claude-fable-5",
        usage={"skills": {"dataviz": 2}, "subagents": {"leaf": 1}},
        unavailable=(),
    )
    assert _decode(comment) == {
        "source": "hermes-agent",
        "model": "claude-fable-5",
        "skills": {"dataviz": 2},
        "subagents": {"leaf": 1},
        "mcp": {},
    }


def test_usage_comment_omits_unknown_model_and_marks_unavailable() -> None:
    comment = usage_comment(
        source="codex",
        model=None,
        usage={"mcp": {"github/x": 3}},
        unavailable=("skills",),
    )
    assert _decode(comment) == {
        "source": "codex",
        "skills": {},
        "subagents": {},
        "mcp": {"github/x": 3},
        "unavailable": ["skills"],
    }


def test_usage_comment_is_bounded_and_flags_truncation() -> None:
    usage = {"mcp": {f"srv/tool{i:03d}": i + 1 for i in range(200)}}
    comment = usage_comment(
        source="codex", model=None, usage=usage, unavailable=()
    )
    payload = _decode(comment)
    assert len(payload["mcp"]) == 25
    assert payload["truncated"] is True
    # 가장 높은 집계 값은 잘린 뒤에도 남는다.
    assert "srv/tool199" in payload["mcp"]
    assert len(comment) <= 4000


def test_usage_comment_never_leaks_inputs() -> None:
    comment = usage_comment(
        source="claude-code",
        model="claude-opus-5",
        usage={"skills": {"dataviz": 1}},
        unavailable=(),
    )
    assert "secret" not in comment
    assert "/" not in comment.split("\n", 1)[0]


def test_concise_summary_prefers_structured_result_sections() -> None:
    response = """
    작업을 모두 마쳤습니다.

    ## 완료
    - 결제 재시도 로직 구현
    ## 변경
    - 오류 응답에 원인 코드 추가
    - 중복 요청 방지
    ## 검증
    - pytest 42개 통과
    ## 미완료
    - 운영 배포는 진행하지 않음
    ## 상세 설명
    이 내용은 Result 요약에서 제외되어야 합니다.
    """

    assert concise_summary(response) == (
        "완료: 결제 재시도 로직 구현\n"
        "변경: 오류 응답에 원인 코드 추가; 중복 요청 방지\n"
        "검증: pytest 42개 통과\n"
        "미완료: 운영 배포는 진행하지 않음"
    )


def test_concise_summary_ignores_structured_aliases_inside_code_fences() -> None:
    response = """
    설정 예시는 다음과 같습니다.
    ```yaml
    changes:
      - secret_token: sk-live-example-secret
    done: true
    ```
    ## 완료
    - 설정 문서 작성
    ## 검증
    - 예제 구문 검사 통과
    """

    summary = concise_summary(response)

    assert summary == "완료: 설정 문서 작성\n검증: 예제 구문 검사 통과"
    assert "secret" not in summary


def test_concise_summary_ignores_nested_code_fences() -> None:
    response = """마크다운 예시를 추가했습니다.
````markdown
```bash
export API_KEY=sk-test-secret  # 추가함
```
````
테스트가 통과했습니다."""

    summary = concise_summary(response)

    assert summary == (
        "변경: 마크다운 예시를 추가했습니다.\n"
        "검증: 테스트가 통과했습니다."
    )
    assert "sk-test-secret" not in summary


def test_concise_summary_extracts_outcomes_without_structured_sections() -> None:
    response = """
    요청 내용을 확인하고 관련 코드를 살펴봤습니다.
    결제 재시도 로직을 구현했습니다.
    구현 세부사항과 호출 흐름에 관한 긴 설명입니다.
    pytest 42개가 통과했습니다.
    운영 배포는 아직 하지 않았습니다.
    """

    assert concise_summary(response) == (
        "변경: 결제 재시도 로직을 구현했습니다.\n"
        "검증: pytest 42개가 통과했습니다.\n"
        "미완료: 운영 배포는 아직 하지 않았습니다."
    )


def test_concise_summary_does_not_mark_fixed_failures_as_incomplete() -> None:
    assert concise_summary(
        "작업 결과입니다.\n실패했던 로그인 테스트를 수정했습니다."
    ) == "변경: 실패했던 로그인 테스트를 수정했습니다."
    assert concise_summary(
        "Review notes follow.\nFixed the failed login test."
    ) == "변경: Fixed the failed login test."


def test_concise_summary_bounds_untrusted_single_line_input() -> None:
    response = "Fixed " + ("token " * 20_000) + "failed tail-marker"

    summary = concise_summary(response)

    assert len(summary) <= 1_000
    assert "tail-marker" not in summary


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            "작업 결과입니다.\n테스트 실패를 수정했습니다.",
            "변경: 테스트 실패를 수정했습니다.",
        ),
        (
            "작업 결과입니다.\n- 실패한 3개 테스트 수정",
            "변경: 실패한 3개 테스트 수정",
        ),
        (
            "Review follows.\nThe failed login test is now fixed.",
            "변경: The failed login test is now fixed.",
        ),
        (
            "작업 결과입니다.\n실패하던 테스트를 고쳤습니다.",
            "변경: 실패하던 테스트를 고쳤습니다.",
        ),
    ],
)
def test_concise_summary_recognizes_resolved_failure_variants(
    response: str,
    expected: str,
) -> None:
    assert concise_summary(response) == expected


def test_concise_summary_extracts_inflected_english_list_items() -> None:
    response = """
    Work summary follows.
    - Implemented the retry logic
    - Refactored duplicate-charge handling
    - Tests passed
    """

    assert concise_summary(response) == (
        "변경: Implemented the retry logic; Refactored duplicate-charge handling\n"
        "검증: Tests passed"
    )


def test_concise_summary_does_not_treat_bare_signoff_as_a_section() -> None:
    response = "Some narration here.\nMore narration about the design.\nDone"

    assert concise_summary(response) == response


def test_concise_summary_does_not_absorb_prose_after_inline_section() -> None:
    response = """Tests: 42 passed
이제 내부 구조를 자세히 설명합니다.
토큰 sk-test-secret을 사용해 재현했습니다.
배포하지 않았습니다."""

    summary = concise_summary(response)

    assert summary == "검증: 42 passed"
    assert "sk-test-secret" not in summary


@pytest.mark.parametrize(
    "response",
    [
        "작업 결과입니다.\n실패한 테스트를 수정하지 못했습니다.",
        "Review follows.\nThe build failed and could not be resolved.",
        "작업 결과입니다.\n로그인 실패가 계속 재현됩니다. 문서를 수정했습니다.",
        "작업 결과입니다.\n실패한 테스트를 수정하려 했으나 실패했습니다.",
        "작업 결과입니다.\n실패한 테스트는 아직 수정 중입니다.",
        "Review follows.\n- Fixed the parser but the login test still fails",
        "Review follows.\n- Migration is unresolved and still failing",
        "Review follows.\nThe deploy step continues to fail after the fix.",
    ],
)
def test_concise_summary_keeps_unresolved_failures_in_incomplete(
    response: str,
) -> None:
    summary = concise_summary(response)

    assert summary.startswith("미완료:")
    assert "변경:" not in summary


def test_concise_summary_keeps_list_after_inline_section_pointer() -> None:
    response = """결과: 아래와 같습니다
- 결제 재시도 구현
- pytest 통과
후속 설명은 카드에 포함하지 않습니다."""

    assert concise_summary(response) == (
        "완료: 아래와 같습니다; 결제 재시도 구현; pytest 통과"
    )


def test_concise_summary_keeps_every_structured_category_within_limit() -> None:
    detail = "상세결과" * 200
    response = "\n".join(
        (
            f"## 완료\n{detail}",
            f"## 변경\n{detail}",
            f"## 검증\n{detail}",
            f"## 미완료\n{detail}",
        )
    )

    summary = concise_summary(response)

    assert len(summary) <= 1_000
    assert all(f"{category}:" in summary for category in ("완료", "변경", "검증", "미완료"))


def test_concise_summary_keeps_every_fallback_category_within_limit() -> None:
    detail = "detail " * 300
    response = "\n".join(
        (
            f"Completed the work: {detail}",
            f"Implemented the change: {detail}",
            f"Tests passed: {detail}",
            f"Remaining deployment: {detail}",
        )
    )

    summary = concise_summary(response)

    assert len(summary) <= 1_000
    assert all(f"{category}:" in summary for category in ("완료", "변경", "검증", "미완료"))


def test_concise_summary_is_idempotent() -> None:
    response = """
    ## 완료
    - Result summarizer implemented
    ## 검증
    - Tests passed
    """

    once = concise_summary(response)

    assert concise_summary(once) == once


def test_concise_summary_collapses_and_bounds() -> None:
    assert concise_summary("  a  \n\n  b  ") == "a\nb"
    assert concise_summary("") == ""
    assert concise_summary(None) == ""
    long = "x" * 1_500
    out = concise_summary(long)
    assert len(out) == 1_000
    assert out.endswith("…")
    assert concise_summary("abcdef", limit=4) == "abc…"


def _stress_name(prefix: str, index: int, *, mcp: bool = False) -> str:
    """주석 크기에 최악인 최대 길이의 유효 식별자."""
    body = (f"{prefix}-{index:04d}" + "-ab" * 24)[:63] + "z"
    return f"srv-{index:04d}/{body}" if mcp else body


def _all_categories_at_limit() -> dict:
    return {
        category: {
            _stress_name(category, i, mcp=category == "mcp"): i + 1
            for i in range(200)
        }
        for category in USAGE_CATEGORIES
    }


@pytest.mark.parametrize(
    "name",
    [
        # 멀티바이트 이름은 허용 목록 밖이므로 애초에 주석에 도달할 수 없다. 바이트
        # 예산을 추측하는 대신 거부한다.
        "도구이름",
        "🙂🙃",
        "ЖЖЖ",
        "café",
    ],
)
def test_non_ascii_names_never_reach_a_comment(name: str) -> None:
    assert sanitize_identifier(name) is None
    assert clean_usage({"skills": {name: 2}}) == {}
    assert name not in usage_comment(
        source="hermes-agent", model=None, usage={"skills": {name: 2}}
    )


def test_usage_comment_stays_valid_json_at_every_limit() -> None:
    comment = usage_comment(
        source="hermes-agent",
        model="claude-fable-5",
        usage=_all_categories_at_limit(),
        unavailable=("skills",),
        event_id=usage_event_id("hermes-agent", "t_12345678"),
    )
    assert len(comment) <= 4000
    payload = _decode(comment)  # 반드시 파싱되어야 하며 강제로 잘린 조각이어서는 안 된다.
    assert payload["truncated"] is True
    assert payload["source"] == "hermes-agent"
    assert payload["model"] == "claude-fable-5"
    assert payload["unavailable"] == ["skills"]
    for category in USAGE_CATEGORIES:
        assert isinstance(payload[category], dict)
        assert len(payload[category]) <= 25


def test_usage_comment_keeps_highest_counts_when_it_must_shrink() -> None:
    usage = {"mcp": {_stress_name("mcp", i, mcp=True): i + 1 for i in range(200)}}
    payload = _decode(
        usage_comment(source="codex", model=None, usage=usage, unavailable=())
    )
    kept = payload["mcp"]
    assert kept
    # 남은 것은 집계 값이 가장 높은 엔트리들이며 연속되어야 한다.
    lowest_kept = min(kept.values())
    assert all(count > len(usage["mcp"]) - len(kept) - 1 for count in kept.values())
    assert lowest_kept == max(kept.values()) - len(kept) + 1


def test_usage_comment_survives_a_pathological_single_category() -> None:
    usage = {"skills": {_stress_name("skill", i): 1 for i in range(400)}}
    comment = usage_comment(
        source="claude-code", model=None, usage=usage, unavailable=(),
        event_id=usage_event_id("claude-code", "t_12345678"),
    )
    assert len(comment) <= 4000
    assert _decode(comment)["truncated"] is True


def test_empty_usage_still_produces_a_full_zero_report() -> None:
    payload = _decode(
        usage_comment(
            source="claude-code", model="claude-opus-5", usage={}, unavailable=()
        )
    )
    assert payload == {
        "source": "claude-code",
        "model": "claude-opus-5",
        "skills": {},
        "subagents": {},
        "mcp": {},
    }


def test_every_tracked_card_reports_usage_even_when_empty() -> None:
    from kanban_adapter.usage import has_reportable_usage

    for source in ("claude-code", "hermes-agent", "codex"):
        assert has_reportable_usage(source, {}) is True
        assert has_reportable_usage(source, {"skills": {"x": 1}}) is True
