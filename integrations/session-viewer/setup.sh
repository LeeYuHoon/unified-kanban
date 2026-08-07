#!/bin/sh
# AI Session Viewer — lightweight, idempotent setup.
#
# This script deliberately does NOT install anything: no pip, no venv, no
# symlinks, no PATH or shell-config edits, no files outside this repository.
# It only (1) marks the CLI executable and (2) runs a self-test in a temporary
# directory that is removed on exit. Safe to run repeatedly.
#
# The self-test uses synthetic Claude / Codex / Hermes fixtures only. It never
# reads ~/.claude, ~/.codex or ~/.hermes.

set -eu

REPO_DIR=$(cd "$(dirname "$0")" && pwd)
CLI="$REPO_DIR/claude_session_viewer.py"

say() { printf '%s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

say "AI Session Viewer — setup (read-only viewer, no install)"
say "providers: Claude Code, OpenAI Codex CLI, Hermes Agent"
say "repo: $REPO_DIR"

# 1. Python check ------------------------------------------------------------
command -v python3 >/dev/null 2>&1 || fail "python3 not found on PATH"
say "python3: $(python3 --version 2>&1)  ($(command -v python3))"

[ -f "$CLI" ] || fail "missing $CLI"

# 2. Make the CLI runnable (idempotent) --------------------------------------
# The executable keeps its historical name so old invocations keep working.
if [ -x "$CLI" ]; then
  say "executable bit: already set"
else
  chmod +x "$CLI"
  say "executable bit: set on claude_session_viewer.py"
fi

# 3. Self-test in a temp dir -------------------------------------------------
TMPDIR_SELFTEST=$(mktemp -d "${TMPDIR:-/tmp}/asv-selftest.XXXXXX")
cleanup() { rm -rf "$TMPDIR_SELFTEST"; }
trap cleanup EXIT INT TERM

say "self-test dir: $TMPDIR_SELFTEST"

ASV_CLI="$CLI" python3 - "$TMPDIR_SELFTEST" <<'PY' || fail "self-test failed"
import hashlib, json, os, sqlite3, subprocess, sys

tmp = sys.argv[1]
cli = os.environ["ASV_CLI"]

def run(args, expect=0):
    proc = subprocess.run([sys.executable, cli] + args, capture_output=True, text=True)
    if proc.returncode != expect:
        print("command failed (%s): %s\n%s\n%s"
              % (proc.returncode, args, proc.stdout, proc.stderr))
        sys.exit(1)
    return proc.stdout + proc.stderr

def digest(path):
    st = os.stat(path)
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest(), st.st_mtime_ns

# --- synthetic Claude transcript -------------------------------------------
claude_root = os.path.join(tmp, "claude", "projects", "selftest")
os.makedirs(claude_root)
claude_path = os.path.join(claude_root, "selftest-session.jsonl")
claude_records = [
    {"type": "user", "sessionId": "selftest-0001", "cwd": os.path.join(tmp, "work"),
     "timestamp": "2026-01-01T00:00:00.000Z",
     "message": {"role": "user", "content": "does the viewer work?"}},
    {"type": "user", "message": {"role": "user",
     "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "noise"}]}},
    {"type": "assistant", "timestamp": "2026-01-01T00:00:01.000Z",
     "message": {"role": "assistant", "stop_reason": "end_turn",
                 "content": [{"type": "thinking", "thinking": "private deliberation"},
                             {"type": "text", "text": "yes, it works."}]}},
]
with open(claude_path, "w", encoding="utf-8") as fh:
    for rec in claude_records:
        fh.write(json.dumps(rec) + "\n")
    fh.write('{"truncated": ')  # malformed trailing line, on purpose

# --- synthetic Codex rollout ------------------------------------------------
codex_root = os.path.join(tmp, "codex", "sessions", "2026", "01", "01")
os.makedirs(codex_root)
codex_path = os.path.join(codex_root, "rollout-selftest.jsonl")
codex_records = [
    {"timestamp": "2026-01-02T00:00:00.000Z", "type": "session_meta",
     "payload": {"id": "selftest-codex-1", "cwd": os.path.join(tmp, "work"),
                 "model_provider": "openai", "source": "cli"}},
    {"type": "event_msg", "timestamp": "2026-01-02T00:00:01.000Z",
     "payload": {"type": "user_message", "message": "does codex parse?"}},
    {"type": "response_item", "timestamp": "2026-01-02T00:00:01.500Z",
     "payload": {"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": "does codex parse?"}]}},
    {"type": "event_msg", "timestamp": "2026-01-02T00:00:02.000Z",
     "payload": {"type": "agent_reasoning", "text": "private codex reasoning"}},
    {"type": "response_item", "timestamp": "2026-01-02T00:00:03.000Z",
     "payload": {"type": "function_call", "name": "shell", "call_id": "c1"}},
    {"type": "response_item", "timestamp": "2026-01-02T00:00:03.500Z",
     "payload": {"type": "function_call_output", "call_id": "c1",
                 "output": "private codex tool output"}},
    {"type": "event_msg", "timestamp": "2026-01-02T00:00:04.000Z",
     "payload": {"type": "agent_message", "message": "yes, codex parses."}},
    {"type": "event_msg", "timestamp": "2026-01-02T00:00:05.000Z",
     "payload": {"type": "task_complete"}},
]
with open(codex_path, "w", encoding="utf-8") as fh:
    for rec in codex_records:
        fh.write(json.dumps(rec) + "\n")
    fh.write("{not json")  # malformed trailing line, on purpose

# these two must never be treated as sessions
for noise in ("history.jsonl", "session_index.jsonl"):
    with open(os.path.join(tmp, "codex", "sessions", noise), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"text": "bookkeeping, not a session"}) + "\n")

# --- synthetic Hermes state.db ----------------------------------------------
hermes_db = os.path.join(tmp, "hermes", "state.db")
os.makedirs(os.path.dirname(hermes_db))
conn = sqlite3.connect(hermes_db)
conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, source TEXT, model TEXT,"
             " title TEXT, cwd TEXT, started_at TEXT, ended_at TEXT, end_reason TEXT,"
             " message_count INTEGER, tool_call_count INTEGER)")
conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id INTEGER,"
             " role TEXT, content TEXT, tool_calls TEXT, tool_name TEXT, timestamp TEXT,"
             " finish_reason TEXT, reasoning TEXT, reasoning_content TEXT, active INTEGER,"
             " compacted INTEGER, display_kind TEXT)")
conn.execute("INSERT INTO sessions VALUES (7,'cli','hermes-large','Self test',?,"
             "'2026-01-03T00:00:00.000Z','2026-01-03T00:05:00.000Z','completed',4,1)",
             (os.path.join(tmp, "work"),))
conn.executemany(
    "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
    [
        (1, 7, "user", "does hermes parse?", None, None, "2026-01-03T00:00:01.000Z",
         None, None, None, 1, 0, None),
        (2, 7, "user", "internal planner note", None, None, None, None, None, None,
         1, 0, "internal"),
        (3, 7, "assistant", None, '[{"function":{"name":"read_file"}}]', None,
         "2026-01-03T00:00:02.000Z", "tool_calls", "private hermes reasoning", None,
         1, 0, None),
        (4, 7, "tool", "private hermes tool output", None, "read_file", None, None,
         None, None, 1, 0, None),
        (5, 7, "assistant", '[{"type":"text","text":"yes, hermes parses."}]', None,
         None, "2026-01-03T00:00:03.000Z", "stop", None, "more private reasoning",
         1, 0, None),
        (6, 7, "user", "rolled back, never shown", None, None, None, None, None, None,
         0, 0, None),
    ],
)
conn.commit()
conn.close()

sources = {
    "claude": claude_path,
    "codex": codex_path,
    "hermes": hermes_db,
}
before = dict((name, digest(path)) for name, path in sources.items())

claude_arg = os.path.join(tmp, "claude", "projects")
codex_arg = os.path.join(tmp, "codex", "sessions")

# --- 1. list, per provider and aggregated -----------------------------------
out = run(["list", "--provider", "claude", "--root", claude_arg])
assert "selftest-0001" in out and "does the viewer work?" in out, out
assert "malformed" in out, out

out = run(["list", "--provider", "codex", "--root", codex_arg])
assert "selftest-codex-1" in out and "does codex parse?" in out, out
assert "history.jsonl" not in out and "session_index" not in out, out

out = run(["list", "--provider", "hermes", "--root", hermes_db])
assert "Self test" in out and "does hermes parse?" in out, out

out = run(["list", "--provider", "all",
           "--claude-root", claude_arg,
           "--codex-root", codex_arg,
           "--hermes-root", hermes_db])
for needle in ("Claude", "Codex", "Hermes", "selftest-0001", "selftest-codex-1", "Self test"):
    assert needle in out, (needle, out)

# --- 2. prompts: synthetic/internal records filtered out --------------------
out = run(["prompts", "selftest-0001", "--provider", "claude", "--root", claude_arg])
assert "1 prompt" in out and "noise" not in out, out
out = run(["prompts", "selftest-codex-1", "--provider", "codex", "--root", codex_arg])
assert "1 prompt" in out, out              # the response_item duplicate is suppressed
out = run(["prompts", "7", "--provider", "hermes", "--root", hermes_db])
assert "1 prompt" in out, out              # display_kind=internal is suppressed
assert "internal planner note" not in out, out
assert "rolled back" not in out, out

# --- 3. timeline: replies shown, reasoning/tool output never --------------
out = run(["timeline", "selftest-0001", "--provider", "claude", "--root", claude_arg])
assert "yes, it works." in out and "Claude:" in out, out
assert "private deliberation" not in out and "1 thinking block" in out, out

out = run(["timeline", "selftest-codex-1", "--provider", "codex", "--root", codex_arg,
           "--show-activity"])
assert "yes, codex parses." in out and "Codex:" in out, out
assert "private codex reasoning" not in out, out
assert "private codex tool output" not in out, out
assert "shell" in out and "completed (task_complete)" in out, out

out = run(["timeline", "7", "--provider", "hermes", "--root", hermes_db, "--show-activity"])
assert "yes, hermes parses." in out and "Hermes:" in out, out
assert "private hermes reasoning" not in out, out
assert "private hermes tool output" not in out, out
assert "read_file" in out, out

# --- 4. exports -------------------------------------------------------------
for provider, root, selector, heading, badge in (
    ("claude", claude_arg, "selftest-0001", "# Claude Session", "badge-claude"),
    ("codex", codex_arg, "selftest-codex-1", "# Codex Session", "badge-codex"),
    ("hermes", hermes_db, "7", "# Hermes Session", "badge-hermes"),
):
    md = os.path.join(tmp, "%s.md" % provider)
    html = os.path.join(tmp, "%s.html" % provider)
    run(["export", selector, "--provider", provider, "--root", root,
         "--format", "markdown", "--out", md])
    run(["export", selector, "--provider", provider, "--root", root,
         "--format", "html", "--out", html])
    body = open(md, encoding="utf-8").read()
    assert body.startswith(heading), (provider, body[:60])
    assert "AI Session Viewer" in body, provider
    page = open(html, encoding="utf-8").read()
    assert page.startswith("<!DOCTYPE html>"), provider
    assert "Content-Security-Policy" in page, provider
    assert badge in page, provider
    for bad in ("http://", "https://", "<script src"):
        assert bad not in page, (provider, bad)

# --- 5. output-path guard for all three provider data dirs ------------------
for guarded in ("~/.claude/nope.md", "~/.codex/nope.md", "~/.hermes/nope.md"):
    out = run(["export", "selftest-0001", "--provider", "claude", "--root", claude_arg,
               "--format", "markdown", "--out", guarded], expect=2)
    assert "Refusing to write" in out, out
    assert not os.path.exists(os.path.expanduser(guarded)), "guard breached: %s" % guarded

# traversal must be caught too
out = run(["export", "selftest-0001", "--provider", "claude", "--root", claude_arg,
           "--format", "markdown", "--out", "~/docs/../.codex/nope.md"], expect=2)
assert "Refusing to write" in out, out

# --- 6. every source is byte- and mtime-identical ---------------------------
for name, path in sources.items():
    assert before[name] == digest(path), "source modified: %s" % name

print("self-test: claude+codex+hermes list/prompts/timeline/export, "
      "reasoning+tool output withheld, ~/.claude ~/.codex ~/.hermes guards, "
      "sources unmodified  OK")
PY

say "setup complete — nothing was installed and nothing outside this repo changed."
say "try: $CLI list --provider all"
