import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import claude_session_viewer as csv_mod
import fixtures


class UserPromptFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = fixtures.write_transcript(self.tmp.name)
        self.session = csv_mod.load_session(self.path)
        self.prompts = csv_mod.real_prompts(self.session)

    def test_only_real_human_prompts_survive(self):
        texts = [p.text for p in self.prompts]
        self.assertEqual(len(texts), 4, texts)
        self.assertEqual(texts[0], "Refactor the JSONL parser, please.")
        self.assertIn("deploy with", texts[1])
        self.assertIn("```python", texts[2])
        self.assertIn("please rotate it", texts[3])

    def test_excluded_categories(self):
        by_index = {e.index: e for e in self.session.events}
        expected = {
            2: "tool_result",
            4: "meta",
            5: "slash_command",
            6: "local_command_output",
            7: "system_reminder_only",
            8: "hook_output",
            9: "caveat_preamble",
            10: "sidechain",
            19: "empty",
        }
        for index, reason in expected.items():
            event = by_index[index]
            self.assertFalse(event.is_real_prompt, "index %d should be excluded" % index)
            self.assertEqual(event.skip_reason, reason, "index %d" % index)

    def test_system_reminder_is_stripped_but_surrounding_human_text_kept(self):
        text, reason = csv_mod.classify_user_record(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": "please fix the flake\n<system-reminder>ignore me</system-reminder>",
                },
            }
        )
        self.assertIsNone(reason)
        self.assertEqual(text, "please fix the flake")

    def test_mixed_tool_result_and_text_keeps_the_text(self):
        text, reason = csv_mod.classify_user_record(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t", "content": "out"},
                        {"type": "text", "text": "now try again"},
                    ],
                },
            }
        )
        self.assertIsNone(reason)
        self.assertEqual(text, "now try again")

    def test_non_user_records_are_not_prompts(self):
        text, reason = csv_mod.classify_user_record({"type": "assistant", "message": {}})
        self.assertIsNone(text)
        self.assertEqual(reason, "not_user_role")

    def test_heuristics_are_documented_and_honest(self):
        self.assertTrue(csv_mod.USER_PROMPT_FILTER_RULES)
        for rule in csv_mod.USER_PROMPT_FILTER_RULES:
            self.assertIsInstance(rule, str)
            self.assertTrue(rule.strip())
        self.assertIn("heuristic", csv_mod.PROMPT_FILTER_CAVEAT.lower())

    def test_first_prompt_preview_is_single_line_and_bounded(self):
        preview = csv_mod.preview(self.session.first_prompt, width=30)
        self.assertNotIn("\n", preview)
        self.assertLessEqual(len(preview), 30)
        self.assertTrue(preview.startswith("Refactor the JSONL"))

    def test_preview_handles_missing_prompt(self):
        self.assertEqual(csv_mod.preview(None, width=20), "(no prompt found)")


if __name__ == "__main__":
    unittest.main()
