"""합성 OpenAI Codex CLI 롤아웃 픽스처.

Codex CLI 0.145가 ``~/.codex/sessions`` 아래에 작성하는
``{timestamp, type, payload}`` JSONL 형태를 손으로 근사해 만든 것이다. 테스트
모음은 실제 Codex 롤아웃을 절대 읽지 않는다.
"""

import json
import os

SESSION_ID = "01983b2e-c0de-4444-aaaa-bbbbccccdddd"
CWD = "/Users/testuser/work/codexdemo"

# 민감 정보 가리기를 검증하는 데 쓰는 비밀처럼 보이는 블롭이며 실제 자격 증명이 아니다.
SECRET_PROMPT = (
    "deploy with AKIAIOSFODNN7EXAMPLE and Authorization: Bearer "
    "abcdefghijklmnopqrstuvwxyz012345 from /Users/testuser/work/codexdemo"
)

HTML_PROMPT = '<img src=x onerror=alert(1)> & "quotes" — summarise this'

REASONING_TEXT = "private codex reasoning that must never be rendered"
TOOL_OUTPUT_TEXT = "sensitive codex tool output that must never be rendered"


def _rec(ts, rtype, payload):
    return {"timestamp": ts, "type": rtype, "payload": payload}


def _event(ts, ptype, **payload):
    payload["type"] = ptype
    return _rec(ts, "event_msg", payload)


def _item(ts, ptype, **payload):
    payload["type"] = ptype
    return _rec(ts, "response_item", payload)


def records():
    """파일 순서로 정렬된 표준 합성 롤아웃."""
    return [
        # 0: session_meta가 id / cwd / model_provider / source를 포함함
        _rec(
            "2026-07-03T09:00:00.000Z",
            "session_meta",
            {
                "id": SESSION_ID,
                "session_id": SESSION_ID,
                "timestamp": "2026-07-03T09:00:00.000Z",
                "cwd": CWD,
                "model_provider": "openai",
                "source": "cli",
                "originator": "codex_cli_rs",
                "cli_version": "0.145.0",
            },
        ),
        # 1: response_item 메시지로 삽입된 개발자/시스템 컨텍스트
        _item(
            "2026-07-03T09:00:00.100Z",
            "message",
            role="developer",
            content=[
                {
                    "type": "input_text",
                    "text": "<user_instructions>internal developer context"
                    "</user_instructions>",
                }
            ],
        ),
        # 2: 실제 사람의 프롬프트
        _event(
            "2026-07-03T09:00:01.000Z",
            "user_message",
            message="Add a retry to the uploader.",
            kind="plain",
        ),
        # 3: response_item으로 되돌아온 동일한 프롬프트 -> 중복
        _item(
            "2026-07-03T09:00:01.050Z",
            "message",
            role="user",
            content=[{"type": "input_text", "text": "Add a retry to the uploader."}],
        ),
        # 4: 턴 시작
        _event("2026-07-03T09:00:01.100Z", "task_started", model_context_window=272000),
        # 5/6: 두 형태의 추론 -> 집계하되 표시하지 않음
        _event("2026-07-03T09:00:02.000Z", "agent_reasoning", text=REASONING_TEXT),
        _item(
            "2026-07-03T09:00:02.100Z",
            "reasoning",
            summary=[{"type": "summary_text", "text": REASONING_TEXT}],
        ),
        # 7/8/9: 도구 호출과 도구 출력 하나
        _item(
            "2026-07-03T09:00:03.000Z",
            "function_call",
            name="shell",
            arguments='{"command":["ls","-la"]}',
            call_id="c1",
        ),
        _item(
            "2026-07-03T09:00:03.500Z",
            "function_call_output",
            call_id="c1",
            output=TOOL_OUTPUT_TEXT,
        ),
        _item(
            "2026-07-03T09:00:04.000Z",
            "custom_tool_call",
            name="apply_patch",
            input="*** Begin Patch",
            call_id="c2",
        ),
        # 10: 사용자에게 보이는 어시스턴트 답변
        _event(
            "2026-07-03T09:00:05.000Z",
            "agent_message",
            message="Added a retry with exponential backoff.",
            phase="final",
        ),
        # 11: response_item으로 되돌아온 동일한 답변 -> 중복시키면 안 됨
        _item(
            "2026-07-03T09:00:05.050Z",
            "message",
            role="assistant",
            content=[
                {"type": "output_text", "text": "Added a retry with exponential backoff."}
            ],
        ),
        # 12: 턴 완료
        _event(
            "2026-07-03T09:00:05.100Z",
            "task_complete",
            last_agent_message="Added a retry with exponential backoff.",
        ),
        # 13: user_message로 들어온 삽입된 환경 컨텍스트
        _event(
            "2026-07-03T09:00:06.000Z",
            "user_message",
            message="<environment_context>\n  <cwd>%s</cwd>\n</environment_context>" % CWD,
        ),
        # 14: 비밀 정보가 있는 두 번째 실제 프롬프트
        _event("2026-07-03T09:01:00.000Z", "user_message", message=SECRET_PROMPT),
        _event("2026-07-03T09:01:00.100Z", "task_started"),
        _event("2026-07-03T09:01:01.000Z", "agent_message", message="Starting the deploy."),
        # 17: 사용자가 턴을 중단함
        _event("2026-07-03T09:01:02.000Z", "turn_aborted", reason="interrupted"),
        # 18: 명시적 압축 레코드
        _rec(
            "2026-07-03T09:02:00.000Z",
            "compacted",
            {"message": "Earlier codex context condensed into a summary."},
        ),
        # 19: HTML과 비슷한 세 번째 실제 프롬프트
        _event("2026-07-03T09:03:00.000Z", "user_message", message=HTML_PROMPT),
        # 20: 응답은 있으나 턴은 완료되지 않음
        _event("2026-07-03T09:03:01.000Z", "agent_message", message="Working on it."),
        # 21: 스트림 오류로 응답한 네 번째 실제 프롬프트
        _event("2026-07-03T09:04:00.000Z", "user_message", message="and now break it"),
        _event("2026-07-03T09:04:01.000Z", "error", message="stream disconnected"),
        # 23: event_msg 형태의 압축
        _event(
            "2026-07-03T09:05:00.000Z",
            "context_compacted",
            message="Context compacted by the CLI.",
        ),
        # 24: 장부 관리용 잡음
        _event("2026-07-03T09:05:01.000Z", "token_count", info={"total_tokens": 1234}),
    ]


def write_rollout(root, rel=None, extra_lines=()):
    """``root`` 아래에 표준 롤아웃을 쓰고 그 경로를 반환한다."""
    if rel is None:
        rel = os.path.join(
            "2026", "07", "03", "rollout-2026-07-03T09-00-00-%s.jsonl" % SESSION_ID
        )
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = []
    for i, rec in enumerate(records()):
        lines.append(json.dumps(rec))
        if i == 4:
            lines.append("")  # 빈 행: 무시
            lines.append("{not json at all")  # 잘못된 행
    lines.extend(extra_lines)
    lines.append('{"timestamp":"2026-07-03T09:06:00.000Z","type":"event_ms')  # 잘림
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return path


def write_second_rollout(root, rel=None):
    if rel is None:
        rel = os.path.join("2026", "07", "04", "rollout-2026-07-04-second.jsonl")
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    recs = [
        _rec(
            "2026-07-04T08:00:00.000Z",
            "session_meta",
            {
                "id": "01983b2e-c0de-4444-eeee-ffff00001111",
                "cwd": "/Users/testuser/work/other",
                "model_provider": "openai",
                "source": "vscode",
            },
        ),
        _event("2026-07-04T08:00:01.000Z", "user_message", message="Say hello in Korean."),
        _event("2026-07-04T08:00:02.000Z", "agent_message", message="안녕하세요"),
        _event("2026-07-04T08:00:03.000Z", "task_complete"),
    ]
    with open(path, "w", encoding="utf-8") as fh:
        for rec in recs:
            fh.write(json.dumps(rec) + "\n")
    return path


def write_index_noise(root):
    """Codex가 롤아웃 옆에 보관하는 비세션 장부 파일을 작성한다."""
    paths = []
    for name, payload in (
        ("history.jsonl", {"session_id": "x", "ts": 1, "text": "shell history entry"}),
        ("session_index.jsonl", {"id": "x", "path": "rollout-x.jsonl"}),
    ):
        path = os.path.join(root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload) + "\n")
        paths.append(path)
    return paths
