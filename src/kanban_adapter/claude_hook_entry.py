"""본문 입력 전에 호환성을 검증하고 수집 거부를 안전하게 알린다."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

from .hook_diagnostics import Recorder, diagnostic_directory


def check_hermes_compatibility():
    # 가장 이른 관측 경계 안에서 런타임 모듈을 가져온다.
    from .compatibility import check_hermes_compatibility as check
    return check()


def report_diagnostic(event: str, code: str, *, exception: BaseException | None = None) -> None:
    """진단 전용 FD에는 허용된 메타데이터만 명시적으로 쓴다."""
    event = event if event in {"prompt", "stop", "session-end", "post-tool-use", "subagent-start"} else "unknown"
    code = code if code in {
        "compatibility-rejected", "collection-failed", "conversation-receipt-failed",
        "conversation-prepare-failed", "conversation-preserve-failed",
        "conversation-recovery-failed", "conversation-transcript-missing",
        "conversation-receipt-missing", "conversation-prepared-missing",
    } else "collection-failed"
    try:
        record = {"component": "kanban-hook", "event": event,
                  "time": datetime.now(timezone.utc).isoformat(), "exit": 0,
                  "error": code,
                  "detail": "unsupported Hermes" if code == "compatibility-rejected" else "collection failed"}
        if exception is not None:
            # 예외, 예외 인자, 지역 변수, 소스 텍스트를 절대 문자열로 서식화하지 않는다.
            record["exception_class"] = type(exception).__name__
            tb = exception.__traceback__
            if tb is not None:
                while tb.tb_next is not None:
                    tb = tb.tb_next
                record.update(source_basename=os.path.basename(tb.tb_frame.f_code.co_filename),
                              source_function=tb.tb_frame.f_code.co_name, source_line=tb.tb_lineno)
        message = json.dumps(record)
        if os.environ.get("UNIFIED_KANBAN_DIAGNOSTIC_FD") == "3":
            os.write(3, (message + "\n").encode("utf-8"))
        else:
            print(message, file=sys.stderr)
    except Exception:
        pass


def report_failure(event: str, code: str, *, record: bool = True) -> None:
    """자유 형식 오류나 입력 대신 허용된 메타데이터만 출력한다."""
    event = event if event in {"prompt", "stop", "session-end", "post-tool-use", "subagent-start"} else "unknown"
    code = code if code in {"compatibility-rejected", "collection-failed"} else "collection-failed"
    report_diagnostic(event, code)
    if record:
        recorder = Recorder.open(diagnostic_directory())
        if recorder:
            try:
                recorder.emit(event, code)
            finally:
                recorder.close()
    try:
        print(json.dumps({"systemMessage": f"unified-kanban: {event} collection unavailable ({code}); Claude may continue."}))
    except Exception:
        pass


def _observed_main(event: str, recorder: Recorder) -> int:
    """전역 패치나 재실행 없이 공개 handler/adapter 경계를 관측한다."""
    from . import claude_hook
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("invalid hook object")
    except Exception as error:
        recorder.emit(event, "input-failed", error)
        report_failure(event, "collection-failed", record=False)
        return 0
    if payload.get("session_id") != recorder.session_id:
        # 다른 세션은 개별 이벤트를 관측하지 않고 정상 처리를 유지한다.
        recorder.emit(event, "session-mismatch")
        try:
            claude_hook.handle_event(event, payload)
        except Exception:
            report_failure(event, "collection-failed", record=False)
        return 0
    recorder.matched = True
    recorder.emit(event, "session-matched")

    def observed_adapter(argv, cwd):
        creating = bool(argv) and argv[0] == "start"
        if creating:
            recorder.emit(event, "create-enter")
        try:
            output = claude_hook.run_adapter(argv, cwd)
        except Exception as error:
            if creating:
                recorder.emit(event, "create-failed", error)
            raise
        if creating:
            # 반환은 adapter 성공이며 DB 삽입을 별도로 검증했다는 뜻은 아니다.
            # 멱등 start는 기존 카드를 반환할 수 있으며 식별자는 저장하지 않는다.
            recorder.emit(event, "create-returned")
        return output

    recorder.emit(event, "handler-enter")
    try:
        claude_hook.handle_event(event, payload, adapter=observed_adapter)
    except Exception as error:
        recorder.emit(event, "handler-failed", error)
        report_failure(event, "collection-failed", record=False)
    else:
        recorder.emit(event, "handler-returned")
    return 0


def main() -> int:
    """검증 실패는 본문 실행 없이 사용자 작업만 계속하도록 처리한다."""
    event = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    recorder = Recorder.open(diagnostic_directory())
    if recorder:
        recorder.emit(event, "entry")
    try:
        try:
            compatible, _reason = check_hermes_compatibility()
        except Exception as error:
            if recorder:
                recorder.emit(event, "compatibility-failed", error)
            report_failure(event, "compatibility-rejected", record=False)
            return 0
        if not compatible:
            if recorder:
                recorder.emit(event, "compatibility-rejected")
            report_failure(event, "compatibility-rejected", record=False)
            return 0
        try:
            from .claude_hook import main as hook_main
        except Exception as error:
            if recorder:
                recorder.emit(event, "runtime-import-failed", error)
            report_failure(event, "collection-failed", record=False)
            return 0
        if recorder:
            recorder.emit(event, "runtime-import")
            if len(sys.argv) == 2:
                return _observed_main(event, recorder)
        return hook_main()
    except Exception as error:
        if recorder:
            recorder.emit(event, "collection-failed", error)
        report_failure(event, "collection-failed", record=False)
        return 0
    finally:
        if recorder:
            recorder.close()


if __name__ == "__main__":
    raise SystemExit(main())
