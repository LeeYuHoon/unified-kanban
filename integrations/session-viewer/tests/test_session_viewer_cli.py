import contextlib
import hashlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import claude_session_viewer as csv_mod
import fixtures


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = csv_mod.main(argv)
    return code, out.getvalue(), err.getvalue()


def tree_digest(root):
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            p = os.path.join(dirpath, name)
            st = os.stat(p)
            h.update(os.path.relpath(p, root).encode())
            h.update(str(st.st_mtime_ns).encode())
            with open(p, "rb") as fh:
                h.update(fh.read())
    return h.hexdigest()


def read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


class CliBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "projects")
        self.path_a = fixtures.write_transcript(self.root)
        self.path_b = fixtures.write_second_transcript(self.root)
        self.outdir = os.path.join(self.tmp.name, "out")
        os.makedirs(self.outdir)


class ListTests(CliBase):
    def test_list_reports_required_columns(self):
        code, out, _ = run(["list", "--root", self.root])
        self.assertEqual(code, 0, out)
        self.assertIn(fixtures.SESSION_ID[:8], out)
        self.assertIn("session-a", out)
        self.assertIn("session-b", out)
        self.assertIn("21", out)  # event count
        self.assertIn("Refactor the JSONL", out)  # first real prompt preview
        self.assertIn("UTC", out)
        self.assertNotIn("Session resumed from checkpoint", out)  # meta record

    def test_list_redacts_cwd_by_default(self):
        _, out, _ = run(["list", "--root", self.root, "--home", "/Users/testuser"])
        self.assertIn("~/work/demo", out)
        self.assertNotIn("/Users/testuser/work/demo", out)

    def test_list_recurses_and_reports_malformed(self):
        _, out, _ = run(["list", "--root", self.root])
        self.assertIn("malformed", out.lower())

    def test_list_empty_root(self):
        empty = os.path.join(self.tmp.name, "empty")
        os.makedirs(empty)
        code, out, err = run(["list", "--root", empty])
        self.assertEqual(code, 1)
        self.assertIn("No .jsonl", out + err)

    def test_list_missing_root(self):
        code, _, err = run(["list", "--root", os.path.join(self.tmp.name, "nope")])
        self.assertEqual(code, 2)
        self.assertIn("not found", err.lower())

    def test_json_paths_are_not_assumed_to_be_session_ids(self):
        sessions = csv_mod.find_sessions(self.root)
        target = [s for s in sessions if s.file_stem == "session-a"][0]
        self.assertNotEqual(target.session_id, target.file_stem)
        self.assertFalse(target.session_id_from_filename)


class SelectorTests(CliBase):
    def test_unique_partial_filename(self):
        code, out, _ = run(["prompts", "session-a", "--root", self.root])
        self.assertEqual(code, 0)
        self.assertIn("Refactor the JSONL parser", out)

    def test_unique_partial_session_id(self):
        code, out, _ = run(["prompts", fixtures.SESSION_ID[:8], "--root", self.root])
        self.assertEqual(code, 0)
        self.assertIn("Refactor the JSONL parser", out)

    def test_zero_matches_fails_helpfully(self):
        code, out, err = run(["prompts", "zzz-nothing", "--root", self.root])
        self.assertEqual(code, 2)
        msg = out + err
        self.assertIn("No session", msg)
        self.assertIn("zzz-nothing", msg)
        self.assertIn("list", msg)

    def test_multiple_matches_lists_candidates(self):
        code, out, err = run(["prompts", "session", "--root", self.root])
        self.assertEqual(code, 2)
        msg = out + err
        self.assertIn("Multiple sessions", msg)
        self.assertIn("session-a", msg)
        self.assertIn("session-b", msg)

    def test_prompts_only_shows_human_prompts(self):
        _, out, _ = run(["prompts", "session-a", "--root", self.root])
        self.assertNotIn("Session resumed from checkpoint", out)
        self.assertNotIn("<command-name>", out)
        self.assertNotIn("file contents", out)
        self.assertNotIn("Search the repo for TODOs", out)
        self.assertIn("4 prompt", out)

    def test_timeline_hides_tools_but_indicates_them(self):
        _, out, _ = run(["timeline", "session-a", "--root", self.root])
        self.assertIn("2 tool calls", out)
        self.assertNotIn("internal deliberation", out)
        self.assertIn("Done: extracted", out)
        self.assertIn("max_tokens", out)
        self.assertIn("compaction", out.lower())

    def test_timeline_show_activity_flag_lists_tool_names(self):
        _, out, _ = run(["timeline", "session-a", "--root", self.root, "--show-activity"])
        self.assertIn("Read", out)
        self.assertIn("Grep", out)

    def test_prompts_redacts_by_default_and_raw_opts_out(self):
        _, redacted, _ = run(["prompts", "session-a", "--root", self.root, "--home", "/Users/testuser"])
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", redacted)
        _, rawout, _ = run(
            ["prompts", "session-a", "--root", self.root, "--home", "/Users/testuser", "--raw"]
        )
        self.assertIn("AKIAIOSFODNN7EXAMPLE", rawout)


class ExportTests(CliBase):
    def test_export_markdown(self):
        out_path = os.path.join(self.outdir, "s.md")
        code, out, err = run(
            ["export", "session-a", "--format", "markdown", "--out", out_path, "--root", self.root]
        )
        self.assertEqual(code, 0, err)
        with open(out_path, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("# Claude Session", body)
        self.assertIn(out_path, out)
        self.assertEqual(os.stat(out_path).st_mode & 0o777, 0o600)

    def test_export_html(self):
        out_path = os.path.join(self.outdir, "s.html")
        code, _, err = run(
            ["export", "session-a", "--format", "html", "--out", out_path, "--root", self.root]
        )
        self.assertEqual(code, 0, err)
        with open(out_path, encoding="utf-8") as fh:
            body = fh.read()
        self.assertTrue(body.startswith("<!DOCTYPE html>"))
        self.assertIn("Content-Security-Policy", body)

    def test_out_is_mandatory(self):
        with self.assertRaises(SystemExit) as ctx:
            run(["export", "session-a", "--format", "markdown", "--root", self.root])
        self.assertEqual(ctx.exception.code, 2)

    def test_export_refuses_existing_file_without_force(self):
        out_path = os.path.join(self.outdir, "s.md")
        run(["export", "session-a", "--format", "markdown", "--out", out_path, "--root", self.root])
        code, out, err = run(
            ["export", "session-a", "--format", "markdown", "--out", out_path, "--root", self.root]
        )
        self.assertEqual(code, 2)
        self.assertIn("--force", out + err)

    def test_export_force_overwrites(self):
        out_path = os.path.join(self.outdir, "s.md")
        run(["export", "session-a", "--format", "markdown", "--out", out_path, "--root", self.root])
        code, _, err = run(
            [
                "export", "session-a", "--format", "markdown", "--out", out_path,
                "--root", self.root, "--force",
            ]
        )
        self.assertEqual(code, 0, err)

    def test_export_refuses_selected_source_as_output(self):
        before = read_bytes(self.path_a)
        code, out, err = run(
            [
                "export", "session-a", "--format", "markdown",
                "--out", self.path_a, "--root", self.root, "--force",
            ]
        )
        self.assertEqual(code, 2)
        self.assertIn("source session", (out + err).lower())
        self.assertEqual(read_bytes(self.path_a), before)

    def test_export_refuses_hard_link_to_any_loaded_source(self):
        alias = os.path.join(self.outdir, "source-alias.md")
        try:
            os.link(self.path_b, alias)
        except (OSError, NotImplementedError):  # pragma: no cover - platform guard
            self.skipTest("hard links unavailable")
        before = read_bytes(self.path_b)
        code, out, err = run(
            [
                "export", "session-a", "--format", "markdown",
                "--out", alias, "--root", self.root, "--force",
            ]
        )
        self.assertEqual(code, 2)
        self.assertIn("source session", (out + err).lower())
        self.assertEqual(read_bytes(self.path_b), before)

    def test_export_refuses_symlink_to_source(self):
        alias = os.path.join(self.outdir, "source-link.md")
        try:
            os.symlink(self.path_a, alias)
        except (OSError, NotImplementedError):  # pragma: no cover - platform guard
            self.skipTest("symlinks unavailable")
        before = read_bytes(self.path_a)
        code, out, err = run(
            [
                "export", "session-a", "--format", "markdown",
                "--out", alias, "--root", self.root, "--force",
            ]
        )
        self.assertEqual(code, 2)
        self.assertIn("source session", (out + err).lower())
        self.assertEqual(read_bytes(self.path_a), before)

    def test_atomic_writer_aborts_if_target_identity_changes(self):
        out_path = os.path.join(self.outdir, "race.md")
        with open(out_path, "wb") as handle:
            handle.write(b"keep")
        initial = os.stat(out_path)
        first = (initial.st_dev, initial.st_ino, initial.st_mode)
        changed = (initial.st_dev, initial.st_ino + 1, initial.st_mode)

        with mock.patch.object(csv_mod, "_stat_identity", side_effect=[first, changed]):
            with self.assertRaises(csv_mod.OutputPathError):
                csv_mod.write_output_atomic(out_path, "replacement", force=True)

        self.assertEqual(read_bytes(out_path), b"keep")
        leftovers = [
            name for name in os.listdir(self.outdir)
            if name.startswith(".ai-session-viewer-")
        ]
        self.assertEqual(leftovers, [])

    def test_non_force_export_preserves_foreign_racing_destination(self):
        out_path = os.path.join(self.outdir, "non-force-race.md")
        real_link = os.link

        def racing_link(source, destination, **kwargs):
            destination_fd = kwargs["dst_dir_fd"]
            foreign = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=destination_fd,
            )
            os.write(foreign, b"foreign")
            os.close(foreign)
            return real_link(source, destination, **kwargs)

        with mock.patch.object(csv_mod.os, "link", side_effect=racing_link):
            with self.assertRaises(csv_mod.OutputPathError):
                csv_mod.write_output_atomic(out_path, "replacement")

        self.assertEqual(read_bytes(out_path), b"foreign")

    def test_force_export_restores_protected_inode_racing_destination(self):
        out_path = os.path.join(self.outdir, "force-race.md")
        with open(out_path, "wb") as handle:
            handle.write(b"expected")
        protected = self.path_a
        protected_before = read_bytes(protected)
        protected_identity = csv_mod._inode_identity(os.stat(protected))
        real_rename = os.rename
        raced = False

        def racing_rename(source, destination, **kwargs):
            nonlocal raced
            if source == "force-race.md" and not raced:
                raced = True
                os.unlink(source, dir_fd=kwargs["src_dir_fd"])
                os.link(
                    protected,
                    source,
                    dst_dir_fd=kwargs["src_dir_fd"],
                    follow_symlinks=False,
                )
            return real_rename(source, destination, **kwargs)

        with mock.patch.object(csv_mod.os, "rename", side_effect=racing_rename):
            with self.assertRaises(csv_mod.OutputPathError):
                csv_mod.write_output_atomic(
                    out_path,
                    "replacement",
                    force=True,
                    source_inodes={protected_identity},
                )

        self.assertEqual(read_bytes(protected), protected_before)
        self.assertEqual(os.stat(out_path).st_ino, os.stat(protected).st_ino)

    def test_atomic_writer_rejects_replaced_parent_directory(self):
        out_path = os.path.join(self.outdir, "parent-race.md")
        resolved, parent_identity, source_inodes = csv_mod._resolved_destination(
            out_path,
            source_paths=(self.path_a, self.path_b),
            require_parent=True,
        )
        moved = self.outdir + "-original"
        os.rename(self.outdir, moved)
        os.mkdir(self.outdir)

        with self.assertRaises(csv_mod.OutputPathError):
            csv_mod.write_output_atomic(
                resolved,
                "replacement",
                source_inodes=source_inodes,
                expected_parent_identity=parent_identity,
            )
        self.assertFalse(os.path.exists(os.path.join(self.outdir, "parent-race.md")))
        self.assertFalse(os.path.exists(os.path.join(moved, "parent-race.md")))

    def test_atomic_writer_rejects_parent_symlink_swap(self):
        out_path = os.path.join(self.outdir, "parent-link-race.md")
        resolved, parent_identity, source_inodes = csv_mod._resolved_destination(
            out_path,
            source_paths=(self.path_a, self.path_b),
            require_parent=True,
        )
        source_before = tree_digest(self.root)
        moved = self.outdir + "-original"
        os.rename(self.outdir, moved)
        os.symlink(self.root, self.outdir)

        with self.assertRaises(OSError):
            csv_mod.write_output_atomic(
                resolved,
                "replacement",
                source_inodes=source_inodes,
                expected_parent_identity=parent_identity,
            )
        self.assertEqual(tree_digest(self.root), source_before)

    def test_cleanup_failure_reports_primary_error_and_closes_fds(self):
        out_path = os.path.join(self.outdir, "cleanup-race.md")
        with open(out_path, "wb") as handle:
            handle.write(b"keep")
        initial = os.stat(out_path)
        first = (initial.st_dev, initial.st_ino, initial.st_mode)
        changed = (initial.st_dev, initial.st_ino + 1, initial.st_mode)
        real_open = os.open
        opened_fds = []

        def tracking_open(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            opened_fds.append(fd)
            return fd

        with mock.patch.object(csv_mod.os, "open", side_effect=tracking_open):
            with mock.patch.object(csv_mod, "_stat_identity", side_effect=[first, changed]):
                with mock.patch.object(csv_mod.os, "unlink", side_effect=PermissionError("busy")):
                    with self.assertWarnsRegex(RuntimeWarning, "cleanup failed"):
                        with self.assertRaises(csv_mod.OutputPathError):
                            csv_mod.write_output_atomic(
                                out_path, "replacement", force=True
                            )

        for fd in opened_fds:
            with self.assertRaises(OSError):
                os.fstat(fd)
        for name in os.listdir(self.outdir):
            if name.startswith(".ai-session-viewer-"):
                os.unlink(os.path.join(self.outdir, name))


class OutputPathGuardTests(CliBase):
    def test_rejects_claude_home_directly(self):
        with self.assertRaises(csv_mod.OutputPathError):
            csv_mod.validate_out_path(
                os.path.join(self.tmp.name, "fakehome", ".claude", "x.md"),
                home=os.path.join(self.tmp.name, "fakehome"),
            )

    def test_rejects_nested_paths_and_traversal(self):
        home = os.path.join(self.tmp.name, "fakehome")
        os.makedirs(os.path.join(home, ".claude", "projects"), exist_ok=True)
        for candidate in (
            os.path.join(home, ".claude"),
            os.path.join(home, ".claude", "projects", "deep", "x.md"),
            os.path.join(home, "docs", "..", ".claude", "x.md"),
            "~/.claude/x.md",
        ):
            with self.assertRaises(csv_mod.OutputPathError, msg=candidate):
                csv_mod.validate_out_path(candidate, home=home)

    def test_accepts_paths_outside_claude_home(self):
        home = os.path.join(self.tmp.name, "fakehome")
        ok = csv_mod.validate_out_path(os.path.join(self.outdir, "fine.md"), home=home)
        self.assertTrue(str(ok).endswith("fine.md"))

    def test_similarly_named_sibling_is_allowed(self):
        home = os.path.join(self.tmp.name, "fakehome")
        ok = csv_mod.validate_out_path(os.path.join(home, ".claude-export", "a.md"), home=home)
        self.assertIn(".claude-export", str(ok))

    def test_cli_rejects_claude_home_destination(self):
        code, out, err = run(
            [
                "export", "session-a", "--format", "markdown",
                "--out", "~/.claude/leak.md", "--root", self.root,
            ]
        )
        self.assertEqual(code, 2)
        self.assertIn(".claude", out + err)
        self.assertFalse(os.path.exists(os.path.expanduser("~/.claude/leak.md")))


class ReadOnlyTests(CliBase):
    def test_no_command_modifies_the_transcript_tree(self):
        before = tree_digest(self.root)
        run(["list", "--root", self.root])
        run(["prompts", "session-a", "--root", self.root])
        run(["timeline", "session-a", "--root", self.root])
        run(
            ["export", "session-a", "--format", "html",
             "--out", os.path.join(self.outdir, "ro.html"), "--root", self.root]
        )
        self.assertEqual(before, tree_digest(self.root))

    def test_transcripts_are_opened_read_only(self):
        seen = []
        real_open = io.open

        def spy(file, mode="r", *a, **kw):
            seen.append((str(file), mode))
            return real_open(file, mode, *a, **kw)

        csv_mod.io.open = spy
        try:
            csv_mod.load_session(self.path_a)
        finally:
            csv_mod.io.open = real_open
        self.assertTrue(seen)
        for _, mode in seen:
            self.assertNotIn("w", mode)
            self.assertNotIn("a", mode)
            self.assertNotIn("+", mode)


class HelpTests(unittest.TestCase):
    def test_top_level_help(self):
        with self.assertRaises(SystemExit) as ctx:
            run(["--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_no_args_shows_usage(self):
        code, out, err = run([])
        self.assertEqual(code, 2)
        self.assertIn("usage", (out + err).lower())

    def test_subcommand_help(self):
        for sub in ("list", "prompts", "timeline", "export"):
            with self.assertRaises(SystemExit) as ctx:
                run([sub, "--help"])
            self.assertEqual(ctx.exception.code, 0, sub)

    def test_default_root_is_claude_projects_but_not_touched(self):
        parser = csv_mod.build_parser()
        ns = parser.parse_args(["list"])
        self.assertTrue(str(ns.root).endswith(os.path.join(".claude", "projects")))


if __name__ == "__main__":
    unittest.main()
