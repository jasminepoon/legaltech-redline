import sys

import pytest

from app.text_ops import (
    AmbiguousTargetError,
    AppliedChange,
    ChangeFailedError,
    OccurrenceOutOfRangeError,
    RangeOutOfBoundsError,
    RangeTextMismatchError,
    Snippet,
    TargetNotFoundError,
    TextChange,
    apply_changes,
    build_snippet,
    find_match_starts,
    find_match_starts_ignore_case,
    lowercase_same_length,
    replace_one_occurrence,
    replace_range,
)

NDA_TEXT = (
    "The Receiving Party shall protect Confidential Information. "
    "The Receiving Party shall return all Confidential Information within 30 days.\n"
)


# find_match_starts


def test_find_returns_every_start_left_to_right() -> None:
    assert find_match_starts(NDA_TEXT, "Receiving Party") == [4, 64]


def test_find_returns_empty_list_when_target_is_missing() -> None:
    assert find_match_starts(NDA_TEXT, "Disclosing Party") == []


def test_find_does_not_count_overlapping_matches() -> None:
    assert find_match_starts("aaaa", "aa") == [0, 2]
    assert find_match_starts("aaa", "aa") == [0]


def test_find_count_matches_python_str_count() -> None:
    examples = [("aaaa", "aa"), ("abababa", "aba"), (NDA_TEXT, "Confidential")]
    for text, target in examples:
        assert len(find_match_starts(text, target)) == text.count(target)


def test_find_is_case_sensitive() -> None:
    assert find_match_starts(NDA_TEXT, "receiving party") == []


def test_find_treats_regex_characters_literally() -> None:
    assert find_match_starts("Fee is $5.00 (net).", "$5.00 (net)") == [7]
    assert find_match_starts("abc", ".") == []


def test_find_rejects_empty_target() -> None:
    with pytest.raises(ValueError):
        find_match_starts(NDA_TEXT, "")


def test_find_positions_count_unicode_code_points() -> None:
    text = "§ 3 – Zoë’s “Fee”"
    assert find_match_starts(text, "Zoë") == [6]
    assert find_match_starts(text, "“Fee”") == [12]


def test_find_does_not_normalize_quotes_or_accents() -> None:
    text = "the “Customer” and Zoë"
    assert find_match_starts(text, '"Customer"') == []
    assert find_match_starts(text, "Zoe\u0308") == []


def test_find_matches_whitespace_target_literally() -> None:
    assert find_match_starts("one  two three", "  ") == [3]


# lowercase_same_length and find_match_starts_ignore_case


def test_lowercase_same_length_lowercases_ordinary_text() -> None:
    assert lowercase_same_length("Fee FEE Zoë") == "fee fee zoë"


def test_lowercase_same_length_keeps_a_dotted_capital_i() -> None:
    # "İ".lower() is "i" plus a combining dot: two characters.
    assert len("İ".lower()) == 2
    assert lowercase_same_length("İİ Notice") == "İİ notice"
    assert len(lowercase_same_length("İİ Notice")) == len("İİ Notice")


def test_find_ignore_case_returns_original_positions() -> None:
    assert find_match_starts_ignore_case(NDA_TEXT, "receiving party") == [4, 64]
    assert find_match_starts_ignore_case("İİ Notice", "NOTICE") == [3]


def test_find_ignore_case_keeps_the_non_overlapping_rule() -> None:
    assert find_match_starts_ignore_case("AaAa", "aa") == [0, 2]


def test_find_ignore_case_does_not_match_i_with_a_dotted_capital_i() -> None:
    assert find_match_starts_ignore_case("İi", "i") == [1]
    assert find_match_starts_ignore_case("İSTANBUL", "İstanbul") == [0]


def test_lowercase_same_length_lowers_each_sigma_on_its_own() -> None:
    # "ΟΣ".lower() is "ος": a whole-text lower() turns a word-final "Σ" into
    # "ς". Lowered one character at a time, every "Σ" becomes "σ".
    assert "ΟΣ".lower() == "ος"
    assert lowercase_same_length("ΟΣ") == "οσ"


def test_find_ignore_case_matches_a_word_final_capital_sigma() -> None:
    assert find_match_starts_ignore_case("ΟΣ", "Σ") == [1]
    assert find_match_starts_ignore_case("ΟΣ", "σ") == [1]
    assert find_match_starts_ignore_case("ΟΔΟΣ", "οδοσ") == [0]


def test_find_ignore_case_does_not_fold_a_final_small_sigma() -> None:
    # No case folding: "ς" is already lowercase, so it stays "ς", not "σ".
    assert find_match_starts_ignore_case("οδος", "ΟΔΟΣ") == []


def test_unrelated_text_does_not_change_earlier_matches() -> None:
    text = "ΟΔΟΣ Notice, Zoë"
    for query in ["Σ", "οδοσ", "NOTICE", "ZOË"]:
        assert find_match_starts_ignore_case(text, query) == (
            find_match_starts_ignore_case(text + " İ", query)
        )
    assert find_match_starts_ignore_case(text + " İ", "Σ") == [3]


def test_lowercase_same_length_is_the_same_with_or_without_a_dotted_capital_i() -> None:
    # Without "İ" or "Σ" the whole text is lowered at once; an "İ" at the end
    # makes it go one character at a time. Both ways must agree for every
    # other character.
    characters = []
    for code_point in range(sys.maxunicode + 1):
        character = chr(code_point)
        if character != "İ" and character != "Σ":
            characters.append(character)
    text = "".join(characters)
    assert lowercase_same_length(text + "İ") == lowercase_same_length(text) + "İ"


# replace_one_occurrence: choosing the occurrence


def test_replace_unique_target_without_occurrence() -> None:
    result = replace_one_occurrence(NDA_TEXT, "30 days", "45 days", None)
    assert result.text == NDA_TEXT.replace("30 days", "45 days")
    assert result.occurrence == 1
    assert result.start == NDA_TEXT.index("30 days")


def test_replace_second_occurrence_matches_readme_example() -> None:
    result = replace_one_occurrence(NDA_TEXT, "Receiving Party", "Recipient", 2)
    assert result.text == (
        "The Receiving Party shall protect Confidential Information. "
        "The Recipient shall return all Confidential Information within 30 days.\n"
    )
    assert result.occurrence == 2
    assert result.start == 64


def test_replace_first_of_several_leaves_the_others() -> None:
    result = replace_one_occurrence("x-A-x-B-x", "x", "Y", 1)
    assert result.text == "Y-A-x-B-x"
    assert result.start == 0


def test_replace_last_of_several_leaves_the_others() -> None:
    result = replace_one_occurrence("x-A-x-B-x", "x", "Y", 3)
    assert result.text == "x-A-x-B-Y"
    assert result.start == 8


def test_replace_unique_occurrence_one_is_allowed() -> None:
    result = replace_one_occurrence(NDA_TEXT, "30 days", "45 days", 1)
    assert result.text == NDA_TEXT.replace("30 days", "45 days")


# replace_one_occurrence: text boundaries and length changes


def test_replace_match_at_start_of_text() -> None:
    result = replace_one_occurrence("Lease begins now", "Lease", "Sublease", None)
    assert result.text == "Sublease begins now"
    assert result.start == 0


def test_replace_match_at_end_of_text() -> None:
    result = replace_one_occurrence("Pay in 30 days", "days", "business days", None)
    assert result.text == "Pay in 30 business days"
    assert result.start == 10


def test_replace_with_empty_string_deletes_the_match() -> None:
    result = replace_one_occurrence(NDA_TEXT, " within 30 days", "", None)
    assert result.text.endswith("all Confidential Information.\n")
    assert " within 30 days" not in result.text


def test_replacement_containing_target_is_not_rescanned() -> None:
    result = replace_one_occurrence("one cat here", "cat", "cat cat", None)
    assert result.text == "one cat cat here"


def test_replace_whitespace_target() -> None:
    result = replace_one_occurrence("one  two", "  ", " ", None)
    assert result.text == "one two"


def test_replace_non_ascii_text() -> None:
    text = "Signed by José Álvarez under § 4.2."
    result = replace_one_occurrence(text, "José Álvarez", "Zoë Müller", None)
    assert result.text == "Signed by Zoë Müller under § 4.2."
    assert result.start == 10


# replace_one_occurrence: errors


def test_replace_missing_target_raises_not_found() -> None:
    with pytest.raises(TargetNotFoundError):
        replace_one_occurrence(NDA_TEXT, "Disclosing Party", "Discloser", None)


def test_replace_missing_target_with_occurrence_raises_not_found() -> None:
    with pytest.raises(TargetNotFoundError):
        replace_one_occurrence(NDA_TEXT, "Disclosing Party", "Discloser", 1)


def test_replace_repeated_target_without_occurrence_raises_ambiguous() -> None:
    with pytest.raises(AmbiguousTargetError) as error_info:
        replace_one_occurrence(NDA_TEXT, "Receiving Party", "Recipient", None)
    assert error_info.value.match_count == 2


def test_replace_occurrence_beyond_count_raises_out_of_range() -> None:
    with pytest.raises(OccurrenceOutOfRangeError) as error_info:
        replace_one_occurrence(NDA_TEXT, "Receiving Party", "Recipient", 3)
    assert error_info.value.occurrence == 3
    assert error_info.value.match_count == 2


def test_overlapping_text_has_only_two_occurrences() -> None:
    result = replace_one_occurrence("aaaa", "aa", "b", 2)
    assert result.text == "aab"
    assert result.start == 2
    with pytest.raises(OccurrenceOutOfRangeError) as error_info:
        replace_one_occurrence("aaaa", "aa", "b", 3)
    assert error_info.value.match_count == 2


def test_replace_rejects_occurrence_below_one() -> None:
    with pytest.raises(ValueError):
        replace_one_occurrence(NDA_TEXT, "Receiving Party", "Recipient", 0)


def test_replace_rejects_empty_target() -> None:
    with pytest.raises(ValueError):
        replace_one_occurrence(NDA_TEXT, "", "x", None)


# replace_range


def test_replace_range_when_text_matches() -> None:
    result = replace_range(NDA_TEXT, 64, 79, "Receiving Party", "Recipient")
    assert result.text == (
        "The Receiving Party shall protect Confidential Information. "
        "The Recipient shall return all Confidential Information within 30 days.\n"
    )
    assert result.start == 64
    assert result.occurrence is None


def test_replace_range_with_empty_replacement_deletes() -> None:
    result = replace_range("Pay in 30 days", 6, 9, " 30", "")
    assert result.text == "Pay in days"


def test_replace_range_ending_at_end_of_text() -> None:
    result = replace_range("Pay in 30 days", 10, 14, "days", "weeks")
    assert result.text == "Pay in 30 weeks"


def test_replace_range_with_different_text_is_stale() -> None:
    with pytest.raises(RangeTextMismatchError) as error_info:
        replace_range(NDA_TEXT, 63, 78, "Receiving Party", "Recipient")
    assert error_info.value.start == 63
    assert error_info.value.end == 78


def test_replace_range_past_end_of_text_is_out_of_bounds() -> None:
    with pytest.raises(RangeOutOfBoundsError) as error_info:
        replace_range("Pay in 30 days", 12, 16, "days", "weeks")
    assert error_info.value.end == 16
    assert error_info.value.text_length == 14


def test_replace_range_counts_unicode_code_points() -> None:
    text = "§ 3 – Zoë’s “Fee”"
    result = replace_range(text, 6, 9, "Zoë", "Zoé")
    assert result.text == "§ 3 – Zoé’s “Fee”"


@pytest.mark.parametrize(
    ("position", "inserted", "expected_text"),
    [
        (0, "Please ", "Please Pay in 30 days"),
        (7, "about ", "Pay in about 30 days"),
        (14, " net", "Pay in 30 days net"),
    ],
)
def test_replace_range_with_zero_width_range_inserts(
    position: int, inserted: str, expected_text: str
) -> None:
    # A zero-width range holds the empty string, so expected text "" matches.
    result = replace_range("Pay in 30 days", position, position, "", inserted)
    assert result.text == expected_text
    assert result.start == position
    assert result.occurrence is None


def test_replace_range_zero_width_past_end_is_out_of_bounds() -> None:
    with pytest.raises(RangeOutOfBoundsError) as error_info:
        replace_range("Pay in 30 days", 15, 15, "", "x")
    assert error_info.value.end == 15
    assert error_info.value.text_length == 14


@pytest.mark.parametrize(
    ("start", "end", "expected_text"),
    [
        (-1, 2, "Pay"),
        (5, 3, "ay"),
        (0, 4, "Pay"),
    ],
)
def test_replace_range_rejects_impossible_ranges(
    start: int, end: int, expected_text: str
) -> None:
    with pytest.raises(ValueError):
        replace_range("Pay in 30 days", start, end, expected_text, "x")


# apply_changes


def test_apply_changes_in_order_reports_each_change() -> None:
    changes = [
        TextChange(target="Receiving Party", replacement="Recipient", occurrence=2),
        TextChange(target="within 30 days", replacement="within 60 days"),
    ]
    result = apply_changes(NDA_TEXT, changes)
    assert result.text == (
        "The Receiving Party shall protect Confidential Information. "
        "The Recipient shall return all Confidential Information within 60 days.\n"
    )
    assert result.changes == [
        AppliedChange(start=64, occurrence=2),
        # 116, not 122: the first change made the text 6 characters shorter.
        AppliedChange(start=116, occurrence=1),
    ]


def test_apply_changes_same_phrase_twice_in_order() -> None:
    # After the first change only one "Receiving Party" is left, so the second
    # change needs no occurrence.
    changes = [
        TextChange(target="Receiving Party", replacement="Recipient", occurrence=1),
        TextChange(target="Receiving Party", replacement="Recipient"),
    ]
    result = apply_changes(NDA_TEXT, changes)
    assert result.text == NDA_TEXT.replace("Receiving Party", "Recipient")


def test_later_change_can_target_text_inserted_by_an_earlier_one() -> None:
    changes = [
        TextChange(target="30 days", replacement="30 calendar days"),
        TextChange(target="calendar", replacement="business"),
    ]
    result = apply_changes(NDA_TEXT, changes)
    assert result.text == NDA_TEXT.replace("30 days", "30 business days")


def test_range_change_uses_positions_after_earlier_changes() -> None:
    # "Recipient" is 6 characters shorter than "Receiving Party", so "30 days"
    # moves from 129 to 123 after the first change.
    changes = [
        TextChange(target="Receiving Party", replacement="Recipient", occurrence=1),
        TextChange(
            target="30 days", replacement="60 days", range_start=123, range_end=130
        ),
    ]
    result = apply_changes(NDA_TEXT, changes)
    assert result.text.endswith("within 60 days.\n")
    assert result.changes[1] == AppliedChange(start=123, occurrence=None)


def test_range_change_with_stale_original_positions_fails() -> None:
    # The first change makes the text longer, so 129 to 136 no longer holds
    # "30 days": the client used positions from before the first change.
    changes = [
        TextChange(
            target="Receiving Party", replacement="Receiving Parties", occurrence=1
        ),
        TextChange(
            target="30 days", replacement="60 days", range_start=129, range_end=136
        ),
    ]
    with pytest.raises(ChangeFailedError) as error_info:
        apply_changes(NDA_TEXT, changes)
    assert error_info.value.index == 1
    assert isinstance(error_info.value.reason, RangeTextMismatchError)


@pytest.mark.parametrize(
    ("failing_change", "reason_type"),
    [
        (TextChange(target="Disclosing Party", replacement="x"), TargetNotFoundError),
        (TextChange(target="Confidential", replacement="x"), AmbiguousTargetError),
        (
            TextChange(target="Confidential", replacement="x", occurrence=3),
            OccurrenceOutOfRangeError,
        ),
        (
            TextChange(target="x", replacement="y", range_start=500, range_end=501),
            RangeOutOfBoundsError,
        ),
    ],
)
def test_failing_change_is_named_by_its_index(
    failing_change: TextChange, reason_type: type
) -> None:
    changes = [
        TextChange(target="within 30 days", replacement="within 60 days"),
        failing_change,
    ]
    with pytest.raises(ChangeFailedError) as error_info:
        apply_changes(NDA_TEXT, changes)
    assert error_info.value.index == 1
    assert isinstance(error_info.value.reason, reason_type)


def test_apply_changes_with_no_changes_returns_the_text() -> None:
    result = apply_changes(NDA_TEXT, [])
    assert result.text == NDA_TEXT
    assert result.changes == []


@pytest.mark.parametrize(
    "change",
    [
        TextChange(target="Pay", replacement="x", range_start=0),
        TextChange(
            target="Pay", replacement="x", occurrence=1, range_start=0, range_end=3
        ),
    ],
)
def test_apply_changes_rejects_inconsistent_change(change: TextChange) -> None:
    with pytest.raises(ValueError):
        apply_changes("Pay in 30 days", [change])


def test_apply_changes_rejects_empty_target_without_range() -> None:
    # Only a zero-width range may have an empty target; found by text, "" would
    # match everywhere. The API rejects this first with a 422.
    with pytest.raises(ValueError):
        apply_changes("Pay in 30 days", [TextChange(target="", replacement="x")])


# build_snippet


def test_snippet_keeps_40_characters_on_each_side() -> None:
    text = "a" * 50 + "MATCH" + "b" * 50
    snippet = build_snippet(text, 50, 55)
    assert snippet == Snippet(
        start=50, end=55, before="a" * 40, match="MATCH", after="b" * 40
    )


def test_snippet_matches_readme_example() -> None:
    snippet = build_snippet(NDA_TEXT, 34, 58)
    assert snippet == Snippet(
        start=34,
        end=58,
        before="The Receiving Party shall protect ",
        match="Confidential Information",
        after=". The Receiving Party shall return all C",
    )


def test_snippet_with_exactly_40_characters_before_is_not_clipped() -> None:
    text = "x" * 40 + "MATCH"
    assert build_snippet(text, 40, 45).before == "x" * 40


def test_snippet_with_41_characters_before_drops_the_first() -> None:
    text = "0" + "x" * 40 + "MATCH"
    assert build_snippet(text, 41, 46).before == "x" * 40


def test_snippet_at_start_of_text_has_empty_before() -> None:
    snippet = build_snippet(NDA_TEXT, 0, 3)
    assert snippet.before == ""
    assert snippet.match == "The"
    assert snippet.after == " Receiving Party shall protect Confident"


def test_snippet_at_end_of_text_has_empty_after() -> None:
    end = len(NDA_TEXT)
    snippet = build_snippet(NDA_TEXT, end - 9, end)
    assert snippet.match == "30 days.\n"
    assert snippet.before == "urn all Confidential Information within "
    assert snippet.after == ""


def test_snippet_in_text_shorter_than_context() -> None:
    snippet = build_snippet("abc", 1, 2)
    assert snippet == Snippet(start=1, end=2, before="a", match="b", after="c")


def test_snippet_keeps_newlines() -> None:
    snippet = build_snippet("line one\nTARGET\nline three", 9, 15)
    assert snippet.before == "line one\n"
    assert snippet.after == "\nline three"


def test_snippet_counts_unicode_code_points() -> None:
    text = "§ 3 – Zoë’s “Fee” is due"
    snippet = build_snippet(text, 12, 17)
    assert snippet == Snippet(
        start=12, end=17, before="§ 3 – Zoë’s ", match="“Fee”", after=" is due"
    )


def test_snippet_rejects_range_outside_text() -> None:
    with pytest.raises(ValueError):
        build_snippet("abc", 2, 4)
    with pytest.raises(ValueError):
        build_snippet("abc", -1, 1)
