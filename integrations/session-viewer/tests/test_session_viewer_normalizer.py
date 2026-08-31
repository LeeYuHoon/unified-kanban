import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import claude_session_viewer as csv_mod
import fixtures


class NormalizerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = fixtures.write_transcript(self.tmp.name)
        self.session = csv_mod.load_session(self.path)

    def test_malformed_records_are_skipped_and_counted(self):
        self.assertEqual(self.session.malformed_count, 2, self.session.malformed_count)
        self.assertEqual(len(self.session.events), len(fixtures.records()))

    def test_blank_lines_are_not_malformed(self):
        path = os.path.join(self.tmp.name, "blank.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n\n   \n")
        s = csv_mod.load_session(path)
        self.assertEqual(s.malformed_count, 0)
        self.assertEqual(s.events, [])

    def test_file_order_is_logical_order(self):
        idx = [e.index for e in self.session.events]
        self.assertEqual(idx, sorted(idx))
        kinds = [e.kind for e in self.session.events]
        self.assertEqual(kinds[0], csv_mod.KIND_USER)
        self.assertEqual(kinds[1], csv_mod.KIND_ASSISTANT)
        self.assertEqual(kinds[12], csv_mod.KIND_COMPACTION)

    def test_content_shapes_string_list_and_block_dict(self):
        # 문자열 콘텐츠
        self.assertEqual(
            self.session.events[0].text, "Refactor the JSONL parser, please."
        )
        # 블록 목록 콘텐츠
        self.assertIn("Looking at the parser now.", self.session.events[1].text)
        # 단일 블록 딕셔너리 콘텐츠
        self.assertIn("Bearer", self.session.events[13].text)
        # 일반 문자열 콘텐츠가 있는 어시스턴트
        self.assertEqual(self.session.events[18].text, "Rotated. Nothing else to do.")

    def test_missing_and_unknown_fields_are_safe(self):
        empty = self.session.events[19]
        self.assertEqual(empty.text, "")
        self.assertIsNone(empty.timestamp)
        unknown = self.session.events[20]
        self.assertEqual(unknown.kind, csv_mod.KIND_OTHER)

    def test_unparseable_content_does_not_raise(self):
        path = os.path.join(self.tmp.name, "weird.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "user", "message": {"content": 12345}}) + "\n")
            fh.write(json.dumps({"type": "assistant", "message": None}) + "\n")
            fh.write(json.dumps([1, 2, 3]) + "\n")  # 최상위 비객체
        s = csv_mod.load_session(path)
        self.assertEqual(len(s.events) + s.malformed_count, 3)

    def test_compaction_is_an_explicit_boundary_and_never_merged(self):
        comp = self.session.events[12]
        self.assertEqual(comp.kind, csv_mod.KIND_COMPACTION)
        self.assertIn("condensed", comp.text)
        # 요약 텍스트가 인접한 어시스턴트/사용자 텍스트에 섞이면 안 된다.
        self.assertNotIn("condensed", self.session.events[11].text)
        self.assertNotIn("condensed", self.session.events[13].text)
        timeline = csv_mod.build_timeline(self.session)
        boundaries = [t for t in timeline if t.kind == csv_mod.KIND_COMPACTION]
        self.assertEqual(len(boundaries), 1)
        # 경계는 분리하는 두 턴 사이에 순서대로 위치한다.
        kinds = [t.kind for t in timeline]
        self.assertLess(kinds.index(csv_mod.KIND_COMPACTION), len(kinds) - 1)

    def test_session_metadata(self):
        self.assertEqual(self.session.session_id, fixtures.SESSION_ID)
        self.assertEqual(self.session.cwd, fixtures.CWD)
        self.assertEqual(self.session.file_stem, "session-a")
        self.assertIsInstance(self.session.mtime, float)
        self.assertRegex(
            csv_mod.format_mtime(self.session.mtime),
            r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC$",
        )

    def test_session_id_falls_back_to_filename_when_absent(self):
        path = os.path.join(self.tmp.name, "no-id.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
        s = csv_mod.load_session(path)
        self.assertEqual(s.session_id, "no-id")
        self.assertTrue(s.session_id_from_filename)

    def test_streaming_reader_does_not_load_whole_file(self):
        # iter_raw_records는 목록 생성기가 아니라 지연 실행 제너레이터여야 한다.
        gen = csv_mod.iter_raw_records(self.path)
        self.assertTrue(hasattr(gen, "__next__"))
        first = next(gen)
        self.assertEqual(first[0], 1)
        gen.close()


if __name__ == "__main__":
    unittest.main()
