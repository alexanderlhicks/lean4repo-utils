"""Edge-case suite for the canonical escape-hatch keyword matcher (C5).

The matcher is the single source of truth for classifying kernel-bypass
keywords across review, summary, and sorry-tracker. Its one job is a correct
identifier-token boundary: a keyword must match only as a standalone token, not
as a substring of a larger identifier and not as the stem of a primed
identifier. Callers pass already-scrubbed code, so these tests exercise the
boundary rule, not comment/string handling (that is scrub_line's job, tested
separately)."""

from leanrepo_common.lean_utils import (
    KERNEL_BYPASS_KEYWORDS,
    find_keywords,
    keyword_pattern,
    keywords_pattern,
    scrub_line,
)


class TestKeywordBoundary:
    def test_standalone_keyword_matches(self):
        assert keyword_pattern("sorry").search("  sorry")
        assert keyword_pattern("sorry").search("exact sorry")
        assert keyword_pattern("admit").search("  admit")

    def test_substring_of_identifier_does_not_match(self):
        # `sorry` is a substring here but not a standalone token.
        assert not keyword_pattern("sorry").search("my_sorry_lemma")
        assert not keyword_pattern("sorry").search("sorryHelper")

    def test_primed_identifier_does_not_match(self):
        # The `\b`-based matcher this replaced WOULD match here: a trailing
        # prime is a word boundary. The canonical boundary excludes it.
        assert not keyword_pattern("sorry").search("sorry'")
        assert not keyword_pattern("sorry").search("theorem sorry' : True")
        assert not keyword_pattern("admit").search("admit'")

    def test_sorry_does_not_match_inside_sorryAx(self):
        # `sorry` and `sorryAx` are distinct hatches; scanning for `sorry`
        # must not fire on the `sorryAx` axiom name.
        assert not keyword_pattern("sorry").search("exact sorryAx")
        assert keyword_pattern("sorryAx").search("exact sorryAx")

    def test_decide_does_not_match_inside_native_decide(self):
        assert not keyword_pattern("decide").search("by native_decide")
        assert keyword_pattern("native_decide").search("by native_decide")
        # A standalone `decide` still matches.
        assert keyword_pattern("decide").search("by decide")

    def test_attribute_keywords_match(self):
        assert keyword_pattern("implemented_by").search("@[implemented_by foo]")
        assert keyword_pattern("extern").search('@[extern "c_name"]')

    def test_matches_against_real_lean_delimiters(self):
        # The common real occurrence is a keyword hugging a bracket/paren/comma,
        # not surrounded by whitespace. Every non-word, non-prime neighbour must
        # still count as a boundary.
        pat = keyword_pattern("sorry")
        assert pat.search("refine ⟨sorry, admit⟩")
        assert pat.search("exact (sorry)")
        assert pat.search("sorry;")
        assert pat.search(":= sorry")
        assert pat.search("sorry")            # bare, at both string ends

    def test_boundary_delta_is_exactly_the_prime(self):
        # Characterization of the deliberate behaviour change from `\b`: the
        # keyword matches next to every non-word neighbour EXCEPT a prime.
        for neighbour in ["(", ")", ",", ":", ";", "⟨", "⟩", " ", "]"]:
            assert keyword_pattern("sorry").search(f"x {neighbour}sorry{neighbour} y"), neighbour
        # ...and fails ONLY on the prime (and on identifier characters).
        assert not keyword_pattern("sorry").search("sorry'")
        assert not keyword_pattern("sorry").search("'sorry")
        assert not keyword_pattern("sorry").search("xsorry")
        assert not keyword_pattern("sorry").search("sorry9")

    def test_bv_decide_in_vocabulary_and_disjoint_from_decide(self):
        assert "bv_decide" in KERNEL_BYPASS_KEYWORDS
        assert keyword_pattern("bv_decide").search("by bv_decide")
        # `decide` must not fire inside `bv_decide` (preceded by `_`).
        assert not keyword_pattern("decide").search("by bv_decide")
        assert find_keywords("by bv_decide", KERNEL_BYPASS_KEYWORDS) == ["bv_decide"]


class TestFindKeywords:
    def test_preserves_keyword_order_and_dedupes(self):
        code = "sorry admit sorry"
        assert find_keywords(code, ("sorry", "admit")) == ["sorry", "admit"]

    def test_reports_only_present_keywords(self):
        code = "by decide"
        assert find_keywords(code, KERNEL_BYPASS_KEYWORDS) == ["decide"]

    def test_empty_on_no_match(self):
        assert find_keywords("theorem foo : True := trivial", ("sorry", "admit")) == []


class TestKeywordsPattern:
    def test_group_reports_matched_keyword(self):
        pat = keywords_pattern(("sorry", "admit", "native_decide"))
        assert pat.search("by native_decide").group() == "native_decide"
        assert pat.search("exact sorry").group() == "sorry"

    def test_alternation_respects_boundary(self):
        pat = keywords_pattern(("sorry", "admit"))
        assert not pat.search("sorry'")
        assert not pat.search("my_admit_helper")

    def test_finditer_collects_all_standalone_tokens(self):
        pat = keywords_pattern(("sorry", "admit"))
        found = {m.group() for m in pat.finditer("sorry then admit")}
        assert found == {"sorry", "admit"}

    def test_cache_returns_stable_object_and_order_is_equivalent(self):
        # Same key → cached object (the module-global cache).
        assert keywords_pattern(("sorry", "admit")) is keywords_pattern(("sorry", "admit"))
        # Reordered keys are behaviourally equivalent over a shared input, even
        # though they are distinct cache entries.
        a = keywords_pattern(("sorry", "admit"))
        b = keywords_pattern(("admit", "sorry"))
        text = "exact ⟨sorry, admit⟩"
        assert {m.group() for m in a.finditer(text)} == {m.group() for m in b.finditer(text)}


class TestScrubThenMatchIntegration:
    """The intended call pattern: scrub first, then match. A keyword that only
    appears inside a string or comment must not be found."""

    def test_keyword_in_string_not_found(self):
        code, _, _ = scrub_line('IO.println "use sorry here"', 0, False)
        assert find_keywords(code, ("sorry",)) == []

    def test_keyword_after_line_comment_not_found(self):
        code, _, _ = scrub_line("exact trivial -- not a sorry", 0, False)
        assert find_keywords(code, ("sorry",)) == []

    def test_real_keyword_alongside_string_mention_found(self):
        code, _, _ = scrub_line('exact sorry -- "admit" mentioned', 0, False)
        assert find_keywords(code, ("sorry", "admit")) == ["sorry"]

    def test_keyword_in_docstring_not_found(self):
        # scrub_line treats `/-- ... -/` like any block comment, so a keyword in
        # a docstring must not reach the matcher (real caller path, not theater).
        code, _, _ = scrub_line("/-- a doc that says sorry -/", 0, False)
        assert find_keywords(code, ("sorry",)) == []

    def test_hatch_resumes_after_inline_closed_docstring(self):
        # A real hatch on the SAME line as a closing docstring must still be
        # found — scrub_line resumes emitting code after the `-/`.
        code, _, _ = scrub_line("/-- doc -/ theorem f : True := sorry", 0, False)
        assert find_keywords(code, ("sorry",)) == ["sorry"]

    def test_hatch_after_closed_string_on_same_line(self):
        code, _, _ = scrub_line('def s := "x"; theorem g := sorry', 0, False)
        assert find_keywords(code, ("sorry",)) == ["sorry"]

    def test_known_limitation_raw_string_desync_hides_later_hatch(self):
        # KNOWN LIMITATION (tracked as a C4 follow-up in ROADMAP): scrub_line
        # does not model Lean raw strings, so `r"\"` (a raw string ending in a
        # backslash) is misread as an escaped quote, the string never closes, and
        # a real hatch on the NEXT line is silently dropped. This pins the current
        # (false-negative) behaviour — UPDATE this assertion when scrub_line
        # learns raw-string syntax.
        depth, in_string = 0, False
        found_on_line_2 = None
        for line in [r'def x := r"\"', "theorem t := sorry"]:
            code, depth, in_string = scrub_line(line, depth, in_string)
            found_on_line_2 = find_keywords(code, ("sorry",))
        assert found_on_line_2 == []  # evasion NOT yet caught — tracked gap

    def test_known_limitation_interpolation_hatch_dropped(self):
        # KNOWN LIMITATION (tracked, C4): a hatch inside `s!"…{e}…"` string
        # interpolation is treated as string content and dropped, though Lean
        # evaluates `e`. Pins current behaviour; update when fixed.
        code, _, _ = scrub_line('def y := s!"val {sorry}"', 0, False)
        assert find_keywords(code, ("sorry",)) == []
