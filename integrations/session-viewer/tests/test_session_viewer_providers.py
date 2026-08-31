import contextlib
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import claude_session_viewer as csv_mod
import fixtures
import fixtures_codex
import fixtures_hermes

HOME = "/Users/testuser"


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = csv_mod.main(argv)
    return code, out.getvalue(), err.getvalue()


class ProviderRegistryTests(unittest.TestCase):
    def test_three_providers_with_labels(self):
        self.assertEqual(
            list(csv_mod.PROVIDER_IDS),
            [csv_mod.PROVIDER_CLAUDE, csv_mod.PROVIDER_CODEX, csv_mod.PROVIDER_HERMES],
        )
        self.assertEqual(csv_mod.provider_label("claude"), "Claude")
        self.assertEqual(csv_mod.provider_label("codex"), "Codex")
        self.assertEqual(csv_mod.provider_label("hermes"), "Hermes")

    def test_defaults(self):
        self.assertTrue(
            csv_mod.default_root_for("claude").endswith(os.path.join(".claude", "projects"))
        )
        self.assertTrue(
            csv_mod.default_root_for("codex").endswith(os.path.join(".codex", "sessions"))
        )
        self.assertTrue(
            csv_mod.default_root_for("hermes").endswith(os.path.join(".hermes", "state.db"))
        )

    def test_each_provider_has_documented_prompt_rules(self):
        for pid in csv_mod.PROVIDER_IDS:
            rules = csv_mod.prompt_filter_rules(pid)
            self.assertTrue(rules, pid)
            for rule in rules:
                self.assertIsInstance(rule, str)
                self.assertTrue(rule.strip())

    def test_title_is_provider_neutral(self):
        self.assertEqual(csv_mod.TITLE, "AI Session Viewer")


class ThreeProviderCliBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.claude_root = os.path.join(cls.tmp.name, "claude", "projects")
        fixtures.write_transcript(cls.claude_root)
        cls.codex_root = os.path.join(cls.tmp.name, "codex", "sessions")
        fixtures_codex.write_rollout(cls.codex_root)
        cls.hermes_db = fixtures_hermes.write_db(
            os.path.join(cls.tmp.name, "hermes", "state.db")
        )
        cls.outdir = os.path.join(cls.tmp.name, "out")
        os.makedirs(cls.outdir)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def all_args(self):
        return [
            "--provider", "all",
            "--claude-root", self.claude_root,
            "--codex-root", self.codex_root,
            "--hermes-root", self.hermes_db,
            "--home", HOME,
        ]


class ProviderSelectionTests(ThreeProviderCliBase):
    def test_list_all_aggregates_every_provider(self):
        code, out, err = run(["list"] + self.all_args())
        self.assertEqual(code, 0, err)
        self.assertIn("Claude", out)
        self.assertIn("Codex", out)
        self.assertIn("Hermes", out)
        self.assertIn("session-a", out)
        self.assertIn(fixtures_codex.SESSION_ID[:8], out)
        self.assertIn("Refactor the uploader", out)

    def test_list_single_provider_excludes_others(self):
        code, out, err = run(
            ["list", "--provider", "codex", "--root", self.codex_root, "--home", HOME]
        )
        self.assertEqual(code, 0, err)
        self.assertIn("Codex", out)
        self.assertNotIn("session-a", out)

    def test_root_without_provider_is_claude_for_compatibility(self):
        code, out, err = run(["list", "--root", self.claude_root])
        self.assertEqual(code, 0, err)
        self.assertIn("session-a", out)
        self.assertIn("Claude", out)

    def test_root_conflicts_with_provider_all(self):
        code, out, err = run(["list", "--provider", "all", "--root", self.claude_root])
        self.assertEqual(code, 2)
        self.assertIn("--claude-root", out + err)

    def test_ambiguous_selector_across_providers_lists_qualified_candidates(self):
        # "1"은 모든 제공자의 세션 ID에 나타난다.
        code, out, err = run(["prompts", "1"] + self.all_args())
        self.assertEqual(code, 2)
        msg = out + err
        self.assertIn("Multiple sessions", msg)
        self.assertIn("claude:", msg)
        self.assertIn("codex:", msg)
        self.assertIn("hermes:", msg)

    def test_provider_qualified_selector_disambiguates(self):
        code, out, err = run(["prompts", "hermes:101"] + self.all_args())
        self.assertEqual(code, 0, err)
        self.assertIn("Explain the retry policy.", out)

    def test_provider_flag_narrows_the_search(self):
        code, out, err = run(["prompts", "101", "--provider", "hermes",
                              "--root", self.hermes_db, "--home", HOME])
        self.assertEqual(code, 0, err)
        self.assertIn("Explain the retry policy.", out)

    def test_unknown_provider_qualifier_is_reported(self):
        code, out, err = run(["prompts", "gemini:abc"] + self.all_args())
        self.assertEqual(code, 2)
        self.assertIn("gemini", (out + err).lower())

    def test_missing_provider_roots_are_skipped_not_fatal(self):
        code, out, err = run(
            ["list", "--provider", "all",
             "--claude-root", self.claude_root,
             "--codex-root", os.path.join(self.tmp.name, "nope"),
             "--hermes-root", os.path.join(self.tmp.name, "nope.db"),
             "--home", HOME]
        )
        self.assertEqual(code, 0, err)
        self.assertIn("session-a", out)
        self.assertIn("not found", (out + err).lower())


class ProviderBadgeTests(ThreeProviderCliBase):
    def test_terminal_output_labels_the_provider_and_assistant(self):
        code, out, _ = run(
            ["timeline", "session-a", "--provider", "claude",
             "--root", self.claude_root, "--home", HOME]
        )
        self.assertEqual(code, 0)
        self.assertIn("Claude:", out)
        self.assertIn("You:", out)

        _, out, _ = run(
            ["timeline", "c0de", "--provider", "codex",
             "--root", self.codex_root, "--home", HOME]
        )
        self.assertIn("Codex:", out)
        self.assertNotIn("Claude:", out)

        _, out, _ = run(
            ["timeline", "101", "--provider", "hermes",
             "--root", self.hermes_db, "--home", HOME]
        )
        self.assertIn("Hermes:", out)
        self.assertNotIn("Claude:", out)

    def test_html_badge_class_per_provider_and_no_external_assets(self):
        for provider, root, selector, badge in (
            ("claude", self.claude_root, "session-a", "badge-claude"),
            ("codex", self.codex_root, "c0de", "badge-codex"),
            ("hermes", self.hermes_db, "101", "badge-hermes"),
        ):
            out_path = os.path.join(self.outdir, "%s.html" % provider)
            code, _, err = run(
                ["export", selector, "--provider", provider, "--root", root,
                 "--format", "html", "--out", out_path, "--home", HOME, "--force"]
            )
            self.assertEqual(code, 0, err)
            with open(out_path, encoding="utf-8") as fh:
                body = fh.read()
            self.assertIn(badge, body)
            self.assertTrue(body.startswith("<!DOCTYPE html>"))
            self.assertIn("Content-Security-Policy", body)
            for bad in ("http://", "https://", "<link", "<img ", "@import", "url("):
                self.assertNotIn(bad, body, "%s: %r" % (provider, bad))

    def test_markdown_badge_per_provider(self):
        for provider, root, selector, expected in (
            ("claude", self.claude_root, "session-a", "# Claude Session"),
            ("codex", self.codex_root, "c0de", "# Codex Session"),
            ("hermes", self.hermes_db, "101", "# Hermes Session"),
        ):
            out_path = os.path.join(self.outdir, "%s.md" % provider)
            code, _, err = run(
                ["export", selector, "--provider", provider, "--root", root,
                 "--format", "markdown", "--out", out_path, "--home", HOME, "--force"]
            )
            self.assertEqual(code, 0, err)
            with open(out_path, encoding="utf-8") as fh:
                body = fh.read()
            self.assertTrue(body.startswith(expected), body[:80])
            self.assertIn("AI Session Viewer", body)


class ProviderOutputGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "fakehome")
        for name in (".claude", ".codex", ".hermes"):
            os.makedirs(os.path.join(self.home, name, "deep"), exist_ok=True)

    def test_rejects_every_provider_home(self):
        cases = {
            ".claude": "Claude",
            ".codex": "Codex",
            ".hermes": "Hermes",
        }
        for directory, label in cases.items():
            for candidate in (
                os.path.join(self.home, directory),
                os.path.join(self.home, directory, "x.md"),
                os.path.join(self.home, directory, "deep", "x.md"),
                os.path.join(self.home, "docs", "..", directory, "x.md"),
                "~/%s/x.md" % directory,
            ):
                with self.assertRaises(csv_mod.OutputPathError, msg=candidate) as ctx:
                    csv_mod.validate_out_path(candidate, home=self.home)
                self.assertIn(label, str(ctx.exception))
                self.assertIn(directory, str(ctx.exception))

    def test_rejects_symlink_into_a_provider_home(self):
        link = os.path.join(self.tmp.name, "link-to-codex")
        try:
            os.symlink(os.path.join(self.home, ".codex"), link)
        except (OSError, NotImplementedError):  # pragma: no cover - 플랫폼별 보호 조건
            self.skipTest("symlinks unavailable")
        with self.assertRaises(csv_mod.OutputPathError) as ctx:
            csv_mod.validate_out_path(os.path.join(link, "leak.md"), home=self.home)
        self.assertIn("Codex", str(ctx.exception))

    def test_similar_names_are_allowed(self):
        for name in (".claude-export", ".codex-notes", ".hermes.bak"):
            ok = csv_mod.validate_out_path(os.path.join(self.home, name, "a.md"),
                                           home=self.home)
            self.assertIn(name, str(ok))

    def test_cli_refuses_codex_and_hermes_destinations(self):
        root = os.path.join(self.tmp.name, "projects")
        fixtures.write_transcript(root)
        for target in ("~/.codex/leak.md", "~/.hermes/leak.md", "~/.claude/leak.md"):
            code, out, err = run(
                ["export", "session-a", "--root", root, "--format", "markdown",
                 "--out", target]
            )
            self.assertEqual(code, 2, target)
            self.assertIn("Refusing to write", out + err)
            self.assertFalse(os.path.exists(os.path.expanduser(target)))


class BackwardCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "projects")
        fixtures.write_transcript(self.root)

    def test_legacy_invocations_still_work(self):
        for argv in (
            ["list", "--root", self.root],
            ["prompts", "session-a", "--root", self.root],
            ["timeline", "session-a", "--root", self.root, "--show-activity"],
        ):
            code, out, err = run(argv)
            self.assertEqual(code, 0, "%s\n%s" % (argv, err))
            self.assertTrue(out.strip())

    def test_default_root_flag_still_points_at_claude_projects(self):
        ns = csv_mod.build_parser().parse_args(["list"])
        self.assertTrue(str(ns.root).endswith(os.path.join(".claude", "projects")))
        self.assertFalse(ns.root_explicit)

    def test_provider_flag_accepts_all_four_values(self):
        for value in ("claude", "codex", "hermes", "all"):
            ns = csv_mod.build_parser().parse_args(["list", "--provider", value])
            self.assertEqual(ns.provider, value)


if __name__ == "__main__":
    unittest.main()
