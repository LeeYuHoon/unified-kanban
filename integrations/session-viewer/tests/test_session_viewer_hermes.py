import contextlib
import hashlib
import io
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import claude_session_viewer as csv_mod
import fixtures_hermes

HOME = "/Users/testuser"


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = csv_mod.main(argv)
    return code, out.getvalue(), err.getvalue()


def digest(path):
    st = os.stat(path)
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest(), st.st_mtime_ns, st.st_size


class HermesBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = fixtures_hermes.write_db(os.path.join(cls.tmp.name, "hermes", "state.db"))
        cls.sessions = csv_mod.load_hermes_sessions(cls.db)
        cls.session = [s for s in cls.sessions if s.session_id == "101"][0]
        cls.timeline = csv_mod.build_timeline(cls.session)
        cls.turns = [t for t in cls.timeline if t.kind == csv_mod.KIND_TURN]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()


class HermesReadOnlyTests(HermesBase):
    def test_connection_uses_sqlite_read_only_uri(self):
        seen = []
        real_connect = sqlite3.connect

        def spy(target, *a, **kw):
            seen.append((str(target), kw))
            return real_connect(target, *a, **kw)

        csv_mod.sqlite3.connect = spy
        try:
            csv_mod.load_hermes_sessions(self.db)
        finally:
            csv_mod.sqlite3.connect = real_connect
        self.assertTrue(seen)
        for target, kw in seen:
            self.assertTrue(target.startswith("file:"), target)
            self.assertIn("mode=ro", target)
            self.assertTrue(kw.get("uri"), kw)

    def test_database_bytes_and_mtime_unchanged(self):
        before = digest(self.db)
        csv_mod.load_hermes_sessions(self.db)
        run(["timeline", "101", "--provider", "hermes", "--root", self.db])
        self.assertEqual(before, digest(self.db))

    def test_write_through_the_viewer_connection_is_impossible(self):
        conn = csv_mod.open_hermes_db(self.db)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO sessions (id) VALUES (999)")
        finally:
            conn.close()


class HermesNormalizerTests(HermesBase):
    def test_provider_and_metadata(self):
        self.assertEqual(self.session.provider, csv_mod.PROVIDER_HERMES)
        self.assertEqual(csv_mod.provider_label(csv_mod.PROVIDER_HERMES), "Hermes")
        self.assertEqual(self.session.title, "Refactor the uploader")
        self.assertEqual(self.session.model, "hermes-large")
        self.assertEqual(self.session.source, "cli")
        self.assertEqual(self.session.cwd, fixtures_hermes.CWD)
        self.assertEqual(self.session.session_id, "101")
        self.assertFalse(self.session.session_id_from_filename)

    def test_two_sessions_are_returned_in_id_order(self):
        self.assertEqual([s.session_id for s in self.sessions], ["101", "102"])

    def test_inactive_rows_are_never_loaded(self):
        blob = "\n".join(e.text for e in self.session.events)
        self.assertNotIn(fixtures_hermes.INACTIVE_TEXT, blob)

    def test_prompts_exclude_internal_and_system_rows(self):
        texts = [p.text for p in csv_mod.real_prompts(self.session)]
        self.assertEqual(len(texts), 4, texts)
        self.assertEqual(texts[0], "Explain the retry policy.")
        self.assertIn("AKIAIOSFODNN7EXAMPLE", texts[1])
        self.assertEqual(texts[2], "dict shaped prompt content")
        self.assertIn("onerror", texts[3])
        self.assertNotIn(fixtures_hermes.INTERNAL_TEXT, texts)
        reasons = self.session.skip_reason_counts()
        self.assertEqual(reasons.get("hidden_display_kind"), 1, reasons)

    def test_content_decoding_is_tolerant(self):
        # JSON list of blocks
        self.assertEqual(self.turns[0].final_text, "Retries use exponential backoff.")
        # JSON string
        self.assertEqual(self.turns[1].final_text, "a JSON string reply")
        # malformed JSON degrades to the raw text
        self.assertEqual(self.turns[2].final_text, "{not json at all")

    def test_tool_calls_counted_and_named_content_omitted(self):
        turn = self.turns[0]
        self.assertEqual(turn.tool_calls, ["read_file", "grep"])
        self.assertEqual(turn.tool_results, 1)
        summary = turn.activity_summary()
        self.assertIn("2 tool calls", summary)
        self.assertIn("read_file", summary)
        self.assertNotIn(fixtures_hermes.TOOL_OUTPUT_TEXT, summary)

    def test_reasoning_is_never_exposed(self):
        blob = "\n".join(e.text for e in self.session.events)
        self.assertNotIn(fixtures_hermes.REASONING_TEXT, blob)
        self.assertNotIn(fixtures_hermes.REASONING_CONTENT_TEXT, blob)
        self.assertEqual(self.turns[0].thinking_blocks, 2)

    def test_compaction_row_is_an_explicit_boundary(self):
        boundaries = [t for t in self.timeline if t.kind == csv_mod.KIND_COMPACTION]
        self.assertEqual(len(boundaries), 1)
        self.assertIn("condensed", boundaries[0].text)
        self.assertNotIn("condensed", self.turns[1].final_text or "")

    def test_finish_reason_semantics(self):
        self.assertTrue(self.turns[0].completed)          # stop
        self.assertIn("completed", self.turns[0].status_label())

        self.assertTrue(self.turns[1].errored)            # error
        self.assertFalse(self.turns[1].completed)
        self.assertIn("error", self.turns[1].status_label().lower())

        self.assertFalse(self.turns[2].completed)         # length
        self.assertIn("length", self.turns[2].status_label())

        self.assertTrue(self.turns[3].completed)          # end_turn

    def test_tool_calls_finish_reason_is_intermediate_not_complete(self):
        other = [s for s in self.sessions if s.session_id == "102"][0]
        turns = [t for t in csv_mod.build_timeline(other) if t.kind == csv_mod.KIND_TURN]
        self.assertEqual(len(turns), 1)
        self.assertFalse(turns[0].completed)
        label = turns[0].status_label()
        self.assertIn("tool call", label.lower())
        self.assertIn("incomplete", label.lower())

    def test_missing_columns_and_rows_degrade_safely(self):
        path = os.path.join(self.tmp.name, "minimal.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, title TEXT)")
        conn.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id INTEGER, "
            "role TEXT, content TEXT)"
        )
        conn.execute("INSERT INTO sessions (id, title) VALUES (7, 'tiny')")
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content) "
            "VALUES (1, 7, 'user', 'hello there')"
        )
        conn.commit()
        conn.close()
        sessions = csv_mod.load_hermes_sessions(path)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].title, "tiny")
        self.assertEqual([p.text for p in csv_mod.real_prompts(sessions[0])], ["hello there"])

    def test_non_database_file_raises_transcript_error(self):
        path = os.path.join(self.tmp.name, "not-a-db.db")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("this is plain text, not sqlite\n")
        with self.assertRaises(csv_mod.TranscriptError):
            csv_mod.load_hermes_sessions(path)

    def test_default_root_is_hermes_state_db(self):
        default = csv_mod.default_root_for(csv_mod.PROVIDER_HERMES)
        self.assertTrue(default.endswith(os.path.join(".hermes", "state.db")))


class HermesCliTests(HermesBase):
    def test_list_shows_titles_and_both_sessions(self):
        code, out, err = run(["list", "--provider", "hermes", "--root", self.db, "--home", HOME])
        self.assertEqual(code, 0, err)
        self.assertIn("Refactor the uploader", out)
        self.assertIn("Greeting", out)
        self.assertIn("Hermes", out)

    def test_timeline_omits_reasoning_and_tool_output(self):
        code, out, err = run(
            ["timeline", "101", "--provider", "hermes", "--root", self.db,
             "--home", HOME, "--show-activity"]
        )
        self.assertEqual(code, 0, err)
        self.assertIn("Retries use exponential backoff.", out)
        self.assertIn("Hermes:", out)
        self.assertNotIn(fixtures_hermes.REASONING_TEXT, out)
        self.assertNotIn(fixtures_hermes.REASONING_CONTENT_TEXT, out)
        self.assertNotIn(fixtures_hermes.TOOL_OUTPUT_TEXT, out)
        self.assertNotIn(fixtures_hermes.INACTIVE_TEXT, out)
        self.assertIn("read_file", out)

    def test_html_export_has_badge_and_no_raw_rows(self):
        html = csv_mod.render_html(self.session, raw=False, home=HOME)
        self.assertIn('class="badge badge-hermes"', html)
        self.assertIn(">Hermes<", html)
        self.assertIn("Refactor the uploader", html)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", html)
        self.assertNotIn("<img src=x onerror", html)
        self.assertNotIn(fixtures_hermes.REASONING_TEXT, html)
        self.assertNotIn(fixtures_hermes.TOOL_OUTPUT_TEXT, html)
        # documented column names may appear in the heuristics list, but no raw
        # database row content ever may
        self.assertNotIn('"tool_calls"', html)
        self.assertNotIn('"function"', html)
        self.assertNotIn(fixtures_hermes.REASONING_CONTENT_TEXT, html)
        self.assertNotIn('{"type":"text"', html)
        self.assertNotIn('{"text":"dict shaped', html)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", html)

    def test_markdown_header_names_the_provider(self):
        md = csv_mod.render_markdown(self.session, raw=False, home=HOME)
        self.assertIn("# Hermes Session", md)
        self.assertIn("Refactor the uploader", md)
        self.assertIn("hermes-large", md)


if __name__ == "__main__":
    unittest.main()
