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


# Values a hostile or confused runtime could put where a name belongs. None of
# them is a legal identifier, so none may ever reach state or a comment.
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


# Opaque, secret-shaped tokens that are *legal* under the identifier grammar
# and short enough to slip past the 32-character blob rule. They carry no
# reporting value and may be credentials, so they must be refused outright.
OPAQUE_NAMES = [
    # The reviewer's sample: 29 characters, alternating case, few digits.
    "AbCdEfGhIjKlMnOpQrStUvWxYz123",
    # AWS-shaped access key id: 20 characters, all upper case, no digits.
    "AKIAABCDEFGHIJKLMNOP",
    "AKIAIOSFODNN7EXAMPLE",
    "aKbLcMdNeOfPgQhRiSjT",
    "XyZaBcDeFgHiJkLmNoPq",
    "GHIJKLMNOPQRSTUVWXYZ",
    # Mixed case with digits mixed through, still under 32 characters.
    "a1B2c3D4e5F6g7H8i9J0",
    "Tk3nAbCd5EfGh7IjKl9M",
]


@pytest.mark.parametrize("name", OPAQUE_NAMES)
def test_opaque_secret_like_names_are_rejected(name: str) -> None:
    """Rejected at ``sanitize_identifier``, so classification and stale-state
    cleaning both refuse it without either needing its own rule."""
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
        # Real names the reviewer's tightened rule must not catch: long, but
        # word-shaped rather than high-entropy.
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
        # delegate_task fans out to N children; subagent_start is authoritative.
        ("hermes-agent", "delegate_task", {"goal": "secret goal"}, None),
        ("hermes-agent", "terminal", {"command": "cat /etc/passwd"}, None),
        ("codex", "exec_command", {"command": "ls"}, None),
        ("codex", "spawn_agent", {"prompt": "secret"}, None),
        # MCP naming convention is shared by all three runtimes.
        ("claude-code", "mcp__github__search_issues", {}, ("mcp", "github/search_issues")),
        ("hermes-agent", "mcp__linear__get_issue", {}, ("mcp", "linear/get_issue")),
        ("codex", "mcp__openaiDeveloperDocs__fetch_openai_doc", {},
         ("mcp", "openaiDeveloperDocs/fetch_openai_doc")),
    ],
)
def test_classify_tool_per_runtime(runtime, tool_name, tool_input, expected) -> None:
    assert classify_tool(runtime, tool_name, tool_input) == expected


def test_classify_tool_falls_back_to_unknown_name() -> None:
    """A structurally absent name carries nothing to leak, so it stays counted."""
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
        # Plugin-qualified skills must survive untouched.
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
    """Rejected, not stored as ``unknown``: a bad name must not be counted."""
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
    # The separator is structural, never part of a segment, and never legal in
    # a plain identifier -- which is what keeps paths out of skill names.
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
    """Stale state is an untrusted input: a hostile key never survives."""
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
    # A slash is structural to MCP alone; it is not a legal skill name.
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
    # No Codex 0.145 hook event carries a skill name.
    assert unavailable_categories("codex") == ("skills",)
    assert unavailable_categories("something-else") == ()


def _decode(comment: str) -> dict:
    header, _, body = comment.partition("\n")
    payload = json.loads(body)
    assert header == usage_comment_header(payload["source"])
    # Every comment is self-describing; the version is asserted once here so
    # the per-field assertions below stay about the usage data itself.
    expected_version = 2 if "tokens" in payload else 1
    assert payload.pop("schema_version") == expected_version
    return payload


def test_claude_header_stays_byte_for_byte_compatible() -> None:
    """Consumers written against the original Claude-only comment still work."""
    comment = usage_comment(
        source="claude-code",
        model="claude-opus-5",
        usage={"skills": {"dataviz": 1}},
        unavailable=(),
    )
    header, newline, body = comment.partition("\n")
    # The header the first shipped version emitted, reproduced exactly.
    assert header == "Agent tool usage"
    assert newline == "\n"
    legacy = json.loads(body)
    assert legacy["source"] == "claude-code"
    assert legacy["skills"] == {"dataviz": 1}
    assert legacy["subagents"] == {}
    assert legacy["mcp"] == {}


def test_headers_are_source_specific() -> None:
    # Claude Code shipped first and keeps the neutral original header; only the
    # sources added afterwards may name themselves.
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
    # The marker is a plain token, safe to search for in a comment body.
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
        "a1234567",  # the shortest legal id: 8 characters
        "usage-" + "0" * 32,  # what usage_event_id itself produces
        "A" + "b.c-d_e" * 18 + "f",  # 128 characters, the longest legal id
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
        "short7",  # 6 characters: below the 8-character floor
        "abcdefg",  # 7 characters: still one short
        "-leading",  # must open with an alphanumeric
        "has space",
        "has:colon",  # the idempotency grammar has no ``:``
        "../../etc/passwd",
        "line\nbreak",
        "nul\x00byte",
        "usage-" + "0" * 200,  # 206 characters: past the 128 ceiling
        7,  # not a string at all
        "",
    ],
)
def test_usage_comment_omits_event_ids_outside_the_grammar(event_id) -> None:
    """An illegal marker is dropped, never rendered: it cannot be honoured as
    an idempotency key, and echoing it would put unvalidated text on a card."""
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
    """Regression: an oversized marker must not leak, and must not push the
    comment past its limit or out of valid JSON."""
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
    payload = _decode(comment)  # still parseable JSON, not a sliced fragment
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
    # Highest counts survive truncation.
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
    """A legal identifier at the maximum length, worst case for comment size."""
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
        # Multi-byte names are outside the whitelist, so they can never reach a
        # comment in the first place -- rejection replaces byte-budget guessing.
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
    payload = _decode(comment)  # must parse; never a hard-sliced fragment
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
    # Whatever survived must be the top-counted entries, contiguously.
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
