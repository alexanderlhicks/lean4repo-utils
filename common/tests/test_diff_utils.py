import unittest

from leanrepo_common.diff_utils import parse_git_diff_header


class ParseGitDiffHeaderTests(unittest.TestCase):
    def test_plain_ascii_path(self):
        self.assertEqual(
            parse_git_diff_header("diff --git a/Foo/Bar.lean b/Foo/Bar.lean"),
            ("Foo/Bar.lean", "Foo/Bar.lean"),
        )

    def test_rename_plain(self):
        self.assertEqual(
            parse_git_diff_header("diff --git a/Old.lean b/New.lean"),
            ("Old.lean", "New.lean"),
        )

    def test_quoted_unicode_path_both_sides(self):
        # git core.quotePath=true output for ünicode.lean (UTF-8 ü = 0xC3 0xBC)
        line = 'diff --git "a/\\303\\274nicode.lean" "b/\\303\\274nicode.lean"'
        self.assertEqual(
            parse_git_diff_header(line), ("ünicode.lean", "ünicode.lean")
        )

    def test_quoted_rename_one_side_quoted(self):
        line = 'diff --git "a/M\\303\\266bius.lean" b/Moebius.lean'
        self.assertEqual(
            parse_git_diff_header(line), ("Möbius.lean", "Moebius.lean")
        )
        line = 'diff --git a/Moebius.lean "b/M\\303\\266bius.lean"'
        self.assertEqual(
            parse_git_diff_header(line), ("Moebius.lean", "Möbius.lean")
        )

    def test_quoted_escape_sequences(self):
        line = 'diff --git "a/we\\"ird\\\\p.lean" "b/we\\"ird\\\\p.lean"'
        self.assertEqual(
            parse_git_diff_header(line), ('we"ird\\p.lean', 'we"ird\\p.lean')
        )

    def test_path_with_spaces_unquoted(self):
        # git does not quote plain spaces
        self.assertEqual(
            parse_git_diff_header("diff --git a/my file.lean b/my file.lean"),
            ("my file.lean", "my file.lean"),
        )

    def test_path_containing_b_slash_mirror_split(self):
        # "dir b/x.lean" contains the ' b/' separator itself; the mirror-split
        # heuristic must not truncate it to "x.lean"
        self.assertEqual(
            parse_git_diff_header("diff --git a/dir b/x.lean b/dir b/x.lean"),
            ("dir b/x.lean", "dir b/x.lean"),
        )

    def test_non_utf8_octal_path_does_not_crash(self):
        # invalid UTF-8 byte sequence degrades to replacement chars, not a crash
        line = 'diff --git "a/\\377bad.lean" "b/\\377bad.lean"'
        result = parse_git_diff_header(line)
        self.assertIsNotNone(result)
        self.assertTrue(result[1].endswith("bad.lean"))

    def test_non_header_lines_return_none(self):
        self.assertIsNone(parse_git_diff_header("+++ b/Foo.lean"))
        self.assertIsNone(parse_git_diff_header("--- a/Foo.lean"))
        self.assertIsNone(parse_git_diff_header("+ diff --git a/x b/x"))
        self.assertIsNone(parse_git_diff_header(""))

    def test_trailing_newline_tolerated(self):
        self.assertEqual(
            parse_git_diff_header("diff --git a/A.lean b/A.lean\n"),
            ("A.lean", "A.lean"),
        )


if __name__ == "__main__":
    unittest.main()
