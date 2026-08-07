import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import claude_session_viewer as csv_mod
import fixtures


class AssistantExtractionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session = csv_mod.load_session(fixtures.write_transcript(self.tmp.name))
        self.timeline = csv_mod.build_timeline(self.session)
        self.turns = [t for t in self.timeline if t.kind == csv_mod.KIND_TURN]

    def test_turns_are_paired_with_human_prompts(self):
        self.assertEqual(len(self.turns), 4)
        self.assertEqual(self.turns[0].prompt, "Refactor the JSONL parser, please.")

    def test_final_visible_text_is_the_last_assistant_text(self):
        self.assertEqual(
            self.turns[0].final_text,
            "Done: extracted `normalize()` and added tests.",
        )

    def test_thinking_and_tool_use_never_appear_in_final_text(self):
        self.assertNotIn("internal deliberation", self.turns[0].final_text)
        self.assertNotIn("tool_use", self.turns[0].final_text)
        self.assertNotIn("file_path", self.turns[0].final_text)

    def test_hidden_activity_is_summarised_compactly(self):
        turn = self.turns[0]
        self.assertEqual(turn.tool_calls, ["Read", "Grep"])
        self.assertEqual(turn.thinking_blocks, 1)
        summary = turn.activity_summary()
        self.assertIn("2 tool calls", summary)
        self.assertIn("Read", summary)
        self.assertIn("1 thinking block", summary)

    def test_subagent_activity_counted_not_shown(self):
        turn = self.turns[0]
        self.assertEqual(turn.subagent_events, 2)
        self.assertIn("2 subagent events", turn.activity_summary())
        self.assertNotIn("Found 3 TODOs", turn.final_text)

    def test_truncated_turn_is_not_reported_as_complete(self):
        turn = self.turns[1]
        self.assertEqual(turn.final_text, "Starting the deploy checklist")
        self.assertFalse(turn.completed)
        self.assertEqual(turn.stop_reason, "max_tokens")
        self.assertIn("max_tokens", turn.status_label())
        self.assertNotIn("complete", turn.status_label().lower())

    def test_completed_turn_says_so(self):
        self.assertTrue(self.turns[0].completed)
        self.assertEqual(self.turns[0].stop_reason, "end_turn")

    def test_api_error_turn_is_flagged(self):
        turn = self.turns[2]
        self.assertTrue(turn.errored)
        self.assertFalse(turn.completed)
        self.assertIn("error", turn.status_label().lower())

    def test_turn_without_assistant_reply(self):
        path = os.path.join(self.tmp.name, "dangling.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"type":"user","message":{"role":"user","content":"anyone there?"}}\n')
        session = csv_mod.load_session(path)
        turns = [t for t in csv_mod.build_timeline(session) if t.kind == csv_mod.KIND_TURN]
        self.assertEqual(len(turns), 1)
        self.assertIsNone(turns[0].final_text)
        self.assertFalse(turns[0].completed)
        self.assertIn("no assistant reply", turns[0].status_label().lower())

    def test_assistant_text_before_any_prompt_is_kept_as_preamble_turn(self):
        path = os.path.join(self.tmp.name, "preamble.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(
                '{"type":"assistant","message":{"role":"assistant",'
                '"content":[{"type":"text","text":"resuming"}],"stop_reason":"end_turn"}}\n'
            )
        session = csv_mod.load_session(path)
        turns = [t for t in csv_mod.build_timeline(session) if t.kind == csv_mod.KIND_TURN]
        self.assertEqual(len(turns), 1)
        self.assertIsNone(turns[0].prompt)
        self.assertEqual(turns[0].final_text, "resuming")


if __name__ == "__main__":
    unittest.main()
