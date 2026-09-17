"""고정된 Claude 생성기 버전에 한정된 메타데이터 전용 조상 관계 연결."""

# Claude 2.1.268의 네이티브 연결 구조에서 확인된 첨부 유형만 조상 관계를 연결한다.
# 이 노드는 메타데이터 전용 조상 노드이며 공개 메시지 내용이 아니다.
ATTACHMENT_TYPES = frozenset({
    "hook_success", "hook_additional_context", "environment", "model",
    "deferred_tools_delta", "agent_listing_delta", "mcp_instructions_delta",
    "skill_listing", "auto_mode", "total_tokens_reminder", "instructions",
    "session_context", "date", "remote_session_change", "prompt_snapshot",
})


def add_attachment(record, *, session, active_prompt, seen, turn_seen):
    """이전 루트에 연결된 노드 하나를 허용하며 본문은 검사하거나 투영하지 않는다.

    요청 전에는 루트에 연결된 사전 구간에만 도달할 수 있다. 요청이 활성화되면
    모든 첨부는 해당 요청의 현재 턴 집합에 속한 노드의 자손이어야 한다.
    UUID는 대화 UUID와 마찬가지로 내부를 해석하지 않는 생성기 식별자로 유지한다.
    """
    attachment = record.get("attachment")
    if (not isinstance(attachment, dict)
            or not isinstance(attachment.get("type"), str)
            or attachment["type"] not in ATTACHMENT_TYPES):
        raise PermissionError("unsupported native attachment")
    if record.get("sessionId") != session:
        raise PermissionError("native attachment session mismatch")
    if record.get("isSidechain") is not False:
        raise PermissionError("native attachment sidechain ambiguous")
    for name in ("isMeta", "isCompactSummary"):
        if name in record and record[name] is not False:
            raise PermissionError("native attachment scope ambiguous")
    if record.get("teamName") not in (None, "") or record.get("agentId") not in (None, ""):
        raise PermissionError("native attachment agent scope ambiguous")
    if "promptId" in record and record["promptId"] != active_prompt:
        raise PermissionError("cross-prompt attachment")
    uuid = record.get("uuid")
    if not isinstance(uuid, str) or not uuid or uuid in seen:
        raise PermissionError("ambiguous attachment identity")
    if "parentUuid" not in record:
        raise PermissionError("native attachment parent missing")
    parent = record["parentUuid"]
    reachable = seen if active_prompt is None else turn_seen
    if reachable:
        if not isinstance(parent, str) or parent not in reachable:
            raise PermissionError("unbound attachment parent")
    elif parent is not None or active_prompt is not None:
        raise PermissionError("unrooted attachment")
    seen.add(uuid)
    if active_prompt is not None:
        turn_seen.add(uuid)
