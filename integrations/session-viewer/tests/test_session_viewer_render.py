import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import claude_session_viewer as csv_mod
import fixtures

HOME = "/Users/testuser"


class FenceTests(unittest.TestCase):
    def test_plain_text_uses_three_backticks(self):
        self.assertEqual(csv_mod.choose_fence("hello"), "```")

    def test_text_with_triple_fence_escalates(self):
        self.assertEqual(csv_mod.choose_fence("a\n```python\nx\n```\n"), "````")

    def test_text_with_longer_fence_escalates_further(self):
        self.assertEqual(csv_mod.choose_fence("`````\nnested\n`````"), "``````")

    def test_inline_backticks_do_not_escalate(self):
        self.assertEqual(csv_mod.choose_fence("use `x` and ``y``"), "```")


class MarkdownRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.session = csv_mod.load_session(fixtures.write_transcript(cls.tmp.name))
        cls.md = csv_mod.render_markdown(cls.session, raw=False, home=HOME)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_header_block(self):
        self.assertIn("# Claude Session", self.md)
        self.assertIn(fixtures.SESSION_ID, self.md)
        self.assertIn("~/work/demo", self.md)
        self.assertIn("REDACTION: ON", self.md)
        self.assertIn("read-only", self.md.lower())

    def test_reports_counts_including_malformed(self):
        self.assertIn("2 malformed", self.md)
        self.assertRegex(self.md, r"Events\s*[:|]\s*21")

    def test_turns_have_timestamps_and_separators(self):
        self.assertIn("2026-07-01 10:00:00 UTC", self.md)
        self.assertIn("---", self.md)
        self.assertIn("Done: extracted `normalize()` and added tests.", self.md)

    def test_prompt_with_fence_is_wrapped_in_longer_fence(self):
        self.assertIn("````", self.md)
        # the inner fence survives intact
        self.assertIn("```python", self.md)

    def test_activity_is_summarised_not_expanded(self):
        self.assertIn("2 tool calls", self.md)
        self.assertNotIn("internal deliberation", self.md)
        self.assertNotIn("file contents", self.md)

    def test_compaction_boundary_rendered_explicitly(self):
        self.assertIn("Context compaction boundary", self.md)
        self.assertIn("condensed", self.md)

    def test_secrets_and_home_are_redacted(self):
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", self.md)
        self.assertNotIn("sk-ant-api03-AAAABBBB", self.md)
        self.assertNotIn("/Users/testuser", self.md)
        self.assertNotIn("MIIEowIBAAKCAQEA", self.md)

    def test_stopped_turn_flagged_not_claimed_successful(self):
        self.assertIn("max_tokens", self.md)

    def test_prose_is_wrapped(self):
        # Assistant prose is soft-wrapped; verbatim prompt blocks (inside fences)
        # are left exactly as the human typed them.
        session = _long_prose_session()
        md = csv_mod.render_markdown(session, raw=False, home=HOME, width=72)
        prose_lines = []
        in_fence = False
        for line in md.splitlines():
            if line.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence or not line or line.startswith(("|", "#", "-", ">", "_", "*")):
                continue
            prose_lines.append(line)
        self.assertTrue(prose_lines)
        self.assertTrue(all(len(ln) <= 80 for ln in prose_lines), max(prose_lines, key=len))


class HtmlRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.session = csv_mod.load_session(fixtures.write_transcript(cls.tmp.name))
        cls.html = csv_mod.render_html(cls.session, raw=False, home=HOME)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_standalone_document(self):
        self.assertTrue(self.html.startswith("<!DOCTYPE html>"))
        self.assertIn("<style>", self.html)
        self.assertIn("</html>", self.html.strip()[-20:])

    def test_csp_blocks_external_and_remote_content(self):
        meta = re.search(
            r'<meta http-equiv="Content-Security-Policy" content="([^"]+)"', self.html
        )
        self.assertIsNotNone(meta, "CSP meta tag missing")
        policy = meta.group(1)
        self.assertIn("default-src 'none'", policy)
        self.assertIn("style-src 'unsafe-inline'", policy)
        self.assertIn("form-action 'none'", policy)
        self.assertIn("base-uri 'none'", policy)
        self.assertNotIn("http:", policy)
        self.assertNotIn("https:", policy)

    def test_no_external_assets_or_network_references(self):
        for bad in ("http://", "https://", "//cdn", "<link", "<img", "@import", "url("):
            self.assertNotIn(bad, self.html, "found %r in HTML" % bad)
        self.assertNotIn("<script src", self.html)

    def test_all_generated_text_is_escaped(self):
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", self.html)
        self.assertNotIn("<img src=x onerror", self.html)
        self.assertNotIn("onerror=alert(1)>", self.html)
        self.assertIn("&amp;", self.html)

    def test_raw_source_json_is_not_embedded(self):
        self.assertNotIn('"parentUuid"', self.html)
        self.assertNotIn("toolUseResult", self.html)
        self.assertNotIn("u-2026-07-01T10:00:00.000Z", self.html)
        self.assertNotIn("leafUuid", self.html)

    def test_search_input_and_collapsible_sections(self):
        self.assertIn('type="search"', self.html)
        self.assertIn("<details", self.html)
        self.assertIn("<summary", self.html)

    def test_accessibility_affordances(self):
        self.assertIn('lang="en"', self.html)
        self.assertIn('name="viewport"', self.html)
        self.assertIn("prefers-color-scheme", self.html)
        self.assertIn("system-ui", self.html)
        self.assertIn("aria-label", self.html)

    def test_secrets_and_home_redacted_in_html(self):
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", self.html)
        self.assertNotIn("/Users/testuser", self.html)
        self.assertIn("~/work/demo", self.html)

    def test_status_is_honest(self):
        self.assertIn("max_tokens", self.html)
        self.assertIn("2 malformed", self.html)

    def test_code_blocks_preserved_as_pre(self):
        self.assertIn("<pre", self.html)
        self.assertIn("print(&#x27;hi&#x27;)", self.html)


def _long_prose_session():
    import json

    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "prose.jsonl")
    words = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet " * 12).strip()
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "message": {"content": words}}) + "\n")
        fh.write(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": words}],
                        "stop_reason": "end_turn",
                    },
                }
            )
            + "\n"
        )
    return csv_mod.load_session(path)


if __name__ == "__main__":
    unittest.main()
