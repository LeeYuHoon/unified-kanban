import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import claude_session_viewer as csv_mod

HOME = "/Users/testuser"


def red(text):
    return csv_mod.redact(text, home=HOME)


class RedactionTests(unittest.TestCase):
    def test_home_path_shortened(self):
        out = red("see /Users/testuser/work/demo/app.py for details")
        self.assertEqual(out, "see ~/work/demo/app.py for details")
        self.assertNotIn("testuser", out)

    def test_home_path_bare(self):
        self.assertEqual(red("cd /Users/testuser"), "cd ~")

    def test_bearer_token_masked(self):
        out = red("Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345")
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz012345", out)
        self.assertIn("Bearer", out)
        self.assertIn(csv_mod.REDACTED, out)

    def test_anthropic_and_generic_sk_keys_masked(self):
        out = red("sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHH and sk-abcdef0123456789abcdef")
        self.assertNotIn("AAAABBBBCCCCDDDD", out)
        self.assertNotIn("abcdef0123456789abcdef", out)
        self.assertEqual(out.count(csv_mod.REDACTED), 2, out)

    def test_aws_access_key_masked(self):
        out = red("key AKIAIOSFODNN7EXAMPLE here")
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", out)
        self.assertIn(csv_mod.REDACTED, out)

    def test_pem_private_key_block_masked(self):
        out = red(
            "before\n-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\nZZZZ==\n"
            "-----END RSA PRIVATE KEY-----\nafter"
        )
        self.assertIn("before", out)
        self.assertIn("after", out)
        self.assertNotIn("MIIEowIBAAKCAQEA", out)
        self.assertIn("PRIVATE KEY", out)

    def test_github_and_slack_style_tokens_masked(self):
        out = red("ghp_0123456789012345678901234567890123 xoxb-123456789012-abcdefghijkl")
        self.assertNotIn("0123456789012345678901234567890123", out)
        self.assertNotIn("abcdefghijkl", out)

    def test_ordinary_text_untouched(self):
        text = "the sky is blue; skip-this and asking questions are fine"
        self.assertEqual(red(text), text)

    def test_non_string_input_is_safe(self):
        self.assertEqual(red(None), None)
        self.assertEqual(red(""), "")

    def test_redaction_is_declared_best_effort(self):
        note = csv_mod.REDACTION_CAVEAT.lower()
        self.assertIn("best-effort", note)
        self.assertIn("not", note)

    def test_raw_mode_bypasses_redaction_in_renderers(self):
        session = _fake_session("token Bearer abcdefghijklmnopqrstuvwxyz012345")
        redacted = csv_mod.render_markdown(session, raw=False, home=HOME)
        raw = csv_mod.render_markdown(session, raw=True, home=HOME)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz012345", redacted)
        self.assertIn("abcdefghijklmnopqrstuvwxyz012345", raw)
        self.assertIn("REDACTION: OFF", raw)


def _fake_session(prompt_text):
    import json
    import tempfile

    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "fake.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "message": {"content": prompt_text}}) + "\n")
    return csv_mod.load_session(path)


if __name__ == "__main__":
    unittest.main()
