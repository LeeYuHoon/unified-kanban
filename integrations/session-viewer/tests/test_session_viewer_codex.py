import contextlib
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import claude_session_viewer as csv_mod
import fixtures_codex

HOME = "/Users/testuser"


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = csv_mod.main(argv)
    return code, out.getvalue(), err.getvalue()


class CodexBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = os.path.join(cls.tmp.name, "sessions")
        cls.path = fixtures_codex.write_rollout(cls.root)
        cls.session = csv_mod.load_codex_session(cls.path)
        cls.timeline = csv_mod.build_timeline(cls.session)
        cls.turns = [t for t in cls.timeline if t.kind == csv_mod.KIND_TURN]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()


class CodexNormalizerTests(CodexBase):
    def test_provider_is_codex(self):
        self.assertEqual(self.session.provider, csv_mod.PROVIDER_CODEX)
        self.assertEqual(csv_mod.provider_label(self.session.provider), "Codex")
        self.assertEqual(csv_mod.assistant_label(self.session.provider), "Codex")

    def test_session_metadata_from_session_meta(self):
        self.assertEqual(self.session.session_id, fixtures_codex.SESSION_ID)
        self.assertFalse(self.session.session_id_from_filename)
        self.assertEqual(self.session.cwd, fixtures_codex.CWD)
        self.assertEqual(self.session.model, "openai")
        self.assertEqual(self.session.source, "cli")

    def test_file_order_preserved_and_malformed_counted(self):
        self.assertEqual(self.session.malformed_count, 2)
        self.assertEqual(len(self.session.events), len(fixtures_codex.records()))
        self.assertEqual(
            [e.index for e in self.session.events],
            sorted(e.index for e in self.session.events),
        )

    def test_only_user_message_events_are_prompts(self):
        texts = [p.text for p in csv_mod.real_prompts(self.session)]
        self.assertEqual(len(texts), 4, texts)
        self.assertEqual(texts[0], "Add a retry to the uploader.")
        self.assertIn("AKIAIOSFODNN7EXAMPLE", texts[1])
        self.assertIn("onerror", texts[2])
        self.assertEqual(texts[3], "and now break it")

    def test_response_item_duplicates_and_context_are_suppressed(self):
        reasons = self.session.skip_reason_counts()
        self.assertEqual(reasons.get("duplicate_response_item"), 1, reasons)
        self.assertEqual(reasons.get("developer_context"), 1, reasons)
        self.assertEqual(reasons.get("environment_context"), 1, reasons)
        # the duplicated prompt text appears exactly once among real prompts
        texts = [p.text for p in csv_mod.real_prompts(self.session)]
        self.assertEqual(texts.count("Add a retry to the uploader."), 1)

    def test_assistant_text_comes_from_agent_message_only(self):
        turn = self.turns[0]
        self.assertEqual(turn.final_text, "Added a retry with exponential backoff.")
        self.assertEqual(turn.assistant_count, 1)

    def test_tool_calls_named_and_counted_output_hidden(self):
        turn = self.turns[0]
        self.assertEqual(turn.tool_calls, ["shell", "apply_patch"])
        self.assertEqual(turn.tool_results, 1)
        summary = turn.activity_summary()
        self.assertIn("2 tool calls", summary)
        self.assertIn("apply_patch", summary)
        self.assertNotIn(fixtures_codex.TOOL_OUTPUT_TEXT, summary)

    def test_reasoning_counted_never_shown(self):
        turn = self.turns[0]
        self.assertEqual(turn.thinking_blocks, 2)
        self.assertIn("2 reasoning blocks", turn.activity_summary())
        self.assertNotIn(fixtures_codex.REASONING_TEXT, turn.final_text or "")

    def test_turn_statuses_are_honest(self):
        self.assertTrue(self.turns[0].completed)
        self.assertIn("completed", self.turns[0].status_label())

        aborted = self.turns[1]
        self.assertFalse(aborted.completed)
        self.assertIn("aborted", aborted.status_label().lower())
        self.assertIn("interrupted", aborted.status_label())

        incomplete = self.turns[2]
        self.assertFalse(incomplete.completed)
        self.assertNotIn("completed", incomplete.status_label())
        self.assertIn("incomplete", incomplete.status_label().lower())

        errored = self.turns[3]
        self.assertTrue(errored.errored)
        self.assertFalse(errored.completed)
        self.assertIn("error", errored.status_label().lower())

    def test_compaction_boundaries_are_explicit(self):
        boundaries = [t for t in self.timeline if t.kind == csv_mod.KIND_COMPACTION]
        self.assertEqual(len(boundaries), 2)
        self.assertIn("condensed", boundaries[0].text)
        self.assertNotIn("condensed", self.turns[1].final_text or "")

    def test_malformed_and_missing_payloads_do_not_raise(self):
        path = os.path.join(self.tmp.name, "weird.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"type":"event_msg"}\n')
            fh.write('{"type":"event_msg","payload":"not-a-dict"}\n')
            fh.write('{"type":"response_item","payload":{"type":"message"}}\n')
            fh.write("[1,2,3]\n")
            fh.write("{oops\n")
        session = csv_mod.load_codex_session(path)
        self.assertEqual(len(session.events) + session.malformed_count, 5)
        self.assertEqual(session.malformed_count, 2)


class CodexDiscoveryTests(CodexBase):
    def test_finds_rollouts_recursively(self):
        sessions = csv_mod.find_sessions_for(csv_mod.PROVIDER_CODEX, self.root)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].provider, csv_mod.PROVIDER_CODEX)

    def test_history_and_index_files_are_not_sessions(self):
        tmp = tempfile.mkdtemp()
        root = os.path.join(tmp, "sessions")
        fixtures_codex.write_rollout(root)
        noise = fixtures_codex.write_index_noise(root)
        self.assertTrue(all(os.path.exists(p) for p in noise))
        sessions = csv_mod.find_sessions_for(csv_mod.PROVIDER_CODEX, root)
        names = [os.path.basename(s.path) for s in sessions]
        self.assertEqual(len(sessions), 1, names)
        self.assertNotIn("history.jsonl", names)
        self.assertNotIn("session_index.jsonl", names)

    def test_default_root_is_codex_sessions(self):
        default = csv_mod.default_root_for(csv_mod.PROVIDER_CODEX)
        self.assertTrue(default.endswith(os.path.join(".codex", "sessions")))


class CodexCliTests(CodexBase):
    def test_timeline_hides_reasoning_and_tool_output(self):
        code, out, err = run(
            ["timeline", "c0de", "--provider", "codex", "--root", self.root,
             "--home", HOME, "--show-activity"]
        )
        self.assertEqual(code, 0, err)
        self.assertIn("Added a retry with exponential backoff.", out)
        self.assertNotIn(fixtures_codex.REASONING_TEXT, out)
        self.assertNotIn(fixtures_codex.TOOL_OUTPUT_TEXT, out)
        self.assertIn("shell", out)
        self.assertIn("Codex:", out)
        self.assertIn("compaction", out.lower())

    def test_prompts_command_lists_four_prompts(self):
        code, out, err = run(
            ["prompts", "c0de", "--provider", "codex", "--root", self.root, "--home", HOME]
        )
        self.assertEqual(code, 0, err)
        self.assertIn("4 prompt", out)
        self.assertNotIn("environment_context>", out)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", out)

    def test_markdown_and_html_carry_codex_badge(self):
        md = csv_mod.render_markdown(self.session, raw=False, home=HOME)
        self.assertIn("# Codex Session", md)
        self.assertIn("Codex", md)
        self.assertNotIn(fixtures_codex.REASONING_TEXT, md)
        self.assertNotIn(fixtures_codex.TOOL_OUTPUT_TEXT, md)

        html = csv_mod.render_html(self.session, raw=False, home=HOME)
        self.assertIn('class="badge badge-codex"', html)
        self.assertIn(">Codex<", html)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", html)
        self.assertNotIn("<img src=x onerror", html)
        self.assertNotIn(fixtures_codex.REASONING_TEXT, html)
        self.assertNotIn(fixtures_codex.TOOL_OUTPUT_TEXT, html)
        self.assertNotIn('"payload"', html)
        self.assertNotIn("call_id", html)


if __name__ == "__main__":
    unittest.main()
