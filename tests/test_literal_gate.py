"""Literal-faithfulness gate helpers (pure — no Postgres, no embedder).

``hard_literals`` names the gateable tokens in a claim value;
``literal_violations`` returns the ones the source corpus does not back.
The spec is conservative by construction (2026-08-02 design doc): date-like
spans are exempt, spelled-out numbers are never gated, and matching is
fail-open on separator/format variance — the gate exists to catch fabricated
numbers and identifiers, not to litigate formatting.
"""
from pseudolife_memory.memory.dream import hard_literals, literal_violations


def test_fabricated_number_is_a_violation():
    assert literal_violations("32", "saw a flicker, that makes 41 species now") == ["32"]


def test_supported_number_is_clean():
    assert literal_violations("32", "that makes 32 species at the park now") == []


def test_thousand_separator_normalises():
    assert literal_violations("1,500", "the invoice came to 1500 exactly") == []
    assert literal_violations("1500", "the invoice came to 1,500 exactly") == []


def test_leading_zero_and_decimal_normalise():
    assert literal_violations("08", "chapter 8 is the long one") == []
    assert literal_violations("3.20", "runs in 3.2 seconds") == []


def test_spelled_number_is_not_gated():
    # No digits in the value -> nothing gateable, regardless of corpus.
    assert hard_literals("thirty-two") == []
    assert literal_violations("thirty-two", "unrelated corpus") == []


def test_date_like_value_is_exempt():
    # Format variance makes digit-matching unsafe for dates; the prompt rule
    # owns dates, the gate owns fabricated numbers (design-doc boundary).
    assert literal_violations("2026-09-30", "due September 30, 2026") == []
    assert literal_violations("due 2026-09-30", "due end of September") == []
    assert literal_violations("9/30/2026", "due end of September") == []


def test_version_and_identifier_substring_match():
    assert literal_violations("v0.12.0", "released 0.12.0 today") == []
    assert literal_violations("PR-81", "merged #81 this morning") == []


def test_pure_digit_tokens_do_not_substring_match():
    # "8" must not ride inside "48": substring leniency is for
    # identifier-like tokens only, or the gate loses its teeth.
    assert literal_violations("8", "we counted 48 boxes") == ["8"]


def test_percent_and_currency_strip():
    assert literal_violations("40%", "usage sits at 40 percent") == []
    assert literal_violations("$1,500", "quoted 1500 for the job") == []


def test_ordinal_suffix_strips():
    assert literal_violations("3rd", "finished 3 out of ten") == []


def test_multiple_literals_any_violation_flags():
    assert literal_violations(
        "v0.12.0 shipped with 7 fixes", "released 0.12.0 with 9 fixes") == ["7"]


def test_empty_corpus_is_clean():
    # No corpus to check against -> the gate abstains rather than drops.
    assert literal_violations("32", "") == []


def test_no_gateable_tokens_is_clean():
    assert hard_literals("prod-eu") == []
    assert literal_violations("prod-eu", "moved to prod-eu") == []


# ── normalization-gap classes from the 2026-08-02 firing probe ──────────────
# evals/results/gate-firing-verdict.json triaged 15/17 batch flags as gate
# normalization gaps in three classes; these pin the extension that closes
# them without blunting the gate on real fabrications.


def test_tilde_approximation_is_exempt():
    # "adopted ~3 months ago" from a note that never writes the digit: a
    # value the extractor itself marks approximate is not a hard literal
    # (same rationale as the date exemption).
    assert literal_violations("adopted ~3 months ago", "we adopted her a few months back") == []
    assert hard_literals("~2 weeks") == []


def test_plus_suffix_matches_spelled_minimum():
    # "2+ years" from "at least two years of experience".
    assert literal_violations("2+ years experience", "needs at least two years of experience") == []


def test_hyphenated_range_matches_spaced_range():
    # "1-3" from "1 to 3": both endpoints backed individually.
    assert literal_violations("1-3 purchases per week", "I buy 1 to 3 things online a week") == []
    assert literal_violations("0-100 scale", "ranges from 0 to 100") == []


def test_hyphenated_range_with_fabricated_endpoint_still_flags():
    assert literal_violations("1-5 purchases", "I buy 1 to 3 things") == ["5"]


def test_hyphenated_compound_matches_spelled_component():
    # "3-week break" from "three week break" (either hyphenation side).
    assert literal_violations("a 3-week exercise break", "took a three week break from the gym") == []


def test_bare_number_matches_hyphenated_corpus_unit():
    # "66 acres" from "66-acre garden": corpus tokens split on hyphens too.
    assert literal_violations("66 acres", "the 66-acre botanical garden") == []


def test_spelled_number_backing_is_exact_word_only():
    # "hundreds" must not back a literal 100.
    assert literal_violations("100 attendees", "hundreds of people came") == ["100"]


def test_fabricated_number_still_flags_after_extension():
    # The gate keeps its teeth: 37 vs a corpus that says 38.
    assert literal_violations("37 coins", "the collection reached 38 coins") == ["37"]
    assert literal_violations("3+ sightings", "saw it twice") == ["3"]


def test_identifier_paths_unchanged_by_hyphen_split():
    assert literal_violations("pr-81", "merged #81 today") == []
    assert literal_violations("v0.12.0", "released 0.12.0") == []
