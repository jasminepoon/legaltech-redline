import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import SAMPLE_DOCUMENTS_DIR, app, store
from frontend.redline import (
    apply_suggestion,
    build_patch_changes,
    describe_changes,
    diff_texts,
    find_match_starts,
    render_change_list_html,
    render_clean_html,
    render_message_html,
    render_redline_html,
    render_search_results_html,
    split_into_tokens,
)

client = TestClient(app)

NDA_TEXT = (
    "The Receiving Party shall protect Confidential Information. "
    "The Receiving Party shall return all Confidential Information within 30 days.\n"
)


@pytest.fixture(autouse=True)
def reset_documents() -> None:
    store.load_directory(SAMPLE_DOCUMENTS_DIR)


def patch_with_etag(path: str, json: object) -> httpx.Response:
    """PATCH as the editor does: send the document's current ETag as If-Match."""
    etag = client.get(path).headers["etag"]
    return client.patch(path, json=json, headers={"If-Match": etag})


def changes_between(original: str, edited: str) -> list[dict]:
    return build_patch_changes(diff_texts(original, edited))


def insertion(position: int, inserted: str) -> dict:
    target = {"text": "", "range": {"start": position, "end": position}}
    return {"operation": "replace", "target": target, "replacement": inserted}


def range_change(text: str, start: int, end: int, replacement: str) -> dict:
    target = {"text": text, "range": {"start": start, "end": end}}
    return {"operation": "replace", "target": target, "replacement": replacement}


# split_into_tokens


def test_tokens_are_words_whitespace_and_single_punctuation_marks() -> None:
    tokens = split_into_tokens("Pay  José’s fee—now.\n")
    assert tokens == ["Pay", "  ", "José", "’", "s", " ", "fee", "—", "now", ".", "\n"]


@pytest.mark.parametrize("text", ["", "a", " \n\t ", "§ 4 – “Fee” 📄 is due.\n"])
def test_joining_the_tokens_gives_the_text_back(text: str) -> None:
    assert "".join(split_into_tokens(text)) == text


# diff_texts


def test_diff_positions_refer_to_the_original_text() -> None:
    pieces = diff_texts("Pay in 30 days", "Pay in 60 days")
    changed = []
    for piece in pieces:
        if piece.kind != "equal":
            changed.append(piece)
    assert len(changed) == 1
    assert changed[0].kind == "replace"
    assert (changed[0].start, changed[0].end) == (7, 9)
    assert (changed[0].old_text, changed[0].new_text) == ("30", "60")


def test_diff_insertion_has_equal_start_and_end() -> None:
    pieces = diff_texts("shall return", "shall promptly return")
    assert pieces[1].kind == "insert"
    assert (pieces[1].start, pieces[1].end) == (6, 6)
    assert pieces[1].new_text == "promptly "


# render_redline_html and render_clean_html


def test_redline_shows_replacement_as_deletion_then_insertion() -> None:
    html = render_redline_html(diff_texts("Pay in 30 days", "Pay in 60 days"))
    assert html == (
        '<div style="white-space: pre-wrap; line-height: 1.6;">Pay in '
        '<del style="color: #e5484d; text-decoration: line-through;">30</del>'
        '<ins style="color: #3b82f6; text-decoration: underline;">60</ins>'
        " days</div>"
    )


def test_redline_shows_insertion_and_deletion() -> None:
    html = render_redline_html(diff_texts("return all items", "promptly return items"))
    assert "<ins" in html and ">promptly </ins>" in html
    assert ">all </del>" in html


def test_redline_escapes_html_in_the_document() -> None:
    html = render_redline_html(diff_texts("a <b> & c", "a <i> & c"))
    assert "<b>" not in html
    assert "&lt;" in html and "&amp;" in html


def test_clean_view_escapes_and_has_no_markup() -> None:
    html = render_clean_html("Fees & <costs>\nNext line")
    assert html == (
        '<div style="white-space: pre-wrap; line-height: 1.6;">'
        "Fees &amp; &lt;costs&gt;\nNext line</div>"
    )


# describe_changes and render_change_list_html


def test_change_list_names_each_type() -> None:
    original = "The Recipient shall return all information within 30 days."
    edited = "The Recipient shall promptly return information within 60 days."
    assert describe_changes(diff_texts(original, edited)) == [
        "Inserted 'promptly'",
        "Deleted 'all'",
        "Replaced '30' with '60'",
    ]


def test_change_list_shows_whitespace_changes_visibly() -> None:
    assert describe_changes(diff_texts("a b", "a  b")) == ["Replaced ' ' with '  '"]
    assert describe_changes(diff_texts("a b", "a\nb")) == ["Replaced ' ' with '\\n'"]


def test_change_list_shortens_long_text() -> None:
    descriptions = describe_changes(diff_texts("", "word " * 30))
    twelve_words = "word word word word word word word word word word word word"
    assert descriptions == ["Inserted '" + twelve_words + "…'"]


def test_change_list_html_escapes_and_counts() -> None:
    assert render_change_list_html([]) == "<p>No changes.</p>"
    html = render_change_list_html(["Inserted '<b>'"])
    assert html == "<p>1 change(s):</p><ol><li>Inserted &#x27;&lt;b&gt;&#x27;</li></ol>"


# build_patch_changes


def test_no_edit_gives_no_changes() -> None:
    assert changes_between(NDA_TEXT, NDA_TEXT) == []
    assert changes_between("", "") == []


def test_insertion_is_a_zero_width_range_with_empty_text() -> None:
    assert changes_between("Pay in 30 days", "Please Pay in 30 days") == [
        insertion(0, "Please ")
    ]
    assert changes_between("Pay in 30 days", "Pay in 30 days net") == [
        insertion(14, " net")
    ]


def test_changes_run_from_the_end_to_the_start() -> None:
    changes = changes_between("one two three four five", "ONE two extra three five")
    assert changes == [
        range_change("four ", 14, 19, ""),
        insertion(8, "extra "),
        range_change("one", 0, 3, "ONE"),
    ]


def test_replacement_with_extra_words_is_one_change() -> None:
    # No unchanged token separates "within" from "60", so difflib reports one
    # replacement rather than an insertion next to a replacement.
    changes = changes_between("pay 30 days", "pay within 60 days")
    assert changes == [range_change("30", 4, 6, "within 60")]


def test_insertion_and_replacement_separated_by_unchanged_text() -> None:
    changes = changes_between("pay in 30 days", "pay promptly in 60 days")
    assert changes == [range_change("30", 7, 9, "60"), insertion(3, " promptly")]


def test_empty_edited_text_deletes_everything() -> None:
    assert changes_between("Pay in 30 days", "") == [
        range_change("Pay in 30 days", 0, 14, "")
    ]


def test_empty_original_text_is_one_insertion() -> None:
    assert changes_between("", "New text") == [insertion(0, "New text")]


# End to end: changes built here, applied by the real service

SAMPLE_PAIRS = [
    ("replacement", NDA_TEXT, NDA_TEXT.replace("30 days", "60 days")),
    ("deletion", NDA_TEXT, NDA_TEXT.replace("return all", "return")),
    ("insertion", NDA_TEXT, NDA_TEXT.replace("shall return", "shall promptly return")),
    (
        "several separate edits",
        NDA_TEXT,
        NDA_TEXT.replace("protect", "safeguard")
        .replace("return all", "return")
        .replace("30 days", "60 calendar days"),
    ),
    ("insertion at start", NDA_TEXT, "CONFIDENTIAL. " + NDA_TEXT),
    ("insertion at end", NDA_TEXT, NDA_TEXT + "Signed.\n"),
    ("deletion at start", NDA_TEXT, NDA_TEXT.removeprefix("The ")),
    ("deletion at end", NDA_TEXT, NDA_TEXT.removesuffix("\n")),
    (
        "replacement at start and end",
        NDA_TEXT,
        "A" + NDA_TEXT.removeprefix("The").removesuffix("days.\n") + "weeks!",
    ),
    (
        "insertion between two other changes",
        "one two three four five",
        "ONE two extra three five",
    ),
    ("insertion merged into a replacement", "pay 30 days", "pay within 60 days"),
    ("insertion next to a replacement", "pay in 30 days", "pay promptly in 60 days"),
    ("insertion next to a deletion", "a b c d", "a X b d"),
    ("insertions at start and end only", "middle", "start middle end"),
    ("double space", NDA_TEXT, NDA_TEXT.replace("shall protect", "shall  protect")),
    ("space to newline", NDA_TEXT, NDA_TEXT.replace(". The", ".\nThe")),
    ("blank lines and tabs", "a\nb\nc", "a\n\n\tb\nc\n"),
    (
        "non-ASCII",
        "§ 4 – “Fee” is due to José Álvarez.",
        "§ 5 — “Fees” are due to Renée Côté 📄.",
    ),
    ("unchanged", NDA_TEXT, NDA_TEXT),
    ("whole document deleted", NDA_TEXT, ""),
    ("empty document filled", "", "New text.\n"),
    ("repeated words", "a a a a", "a a b a a"),
    ("rewritten", "Pay in 30 days.", "Fees are due within sixty days!"),
]


@pytest.mark.parametrize(
    ("original", "edited"),
    [(pair[1], pair[2]) for pair in SAMPLE_PAIRS],
    ids=[pair[0] for pair in SAMPLE_PAIRS],
)
def test_service_applies_the_changes_to_give_the_edited_text(
    original: str, edited: str
) -> None:
    store.save_text("redline-check", original)
    changes = changes_between(original, edited)
    if len(changes) == 0:
        assert edited == original
        return

    response = patch_with_etag("/documents/redline-check", json={"changes": changes})
    assert response.status_code == 200, response.json()
    assert response.json()["text"] == edited


@pytest.mark.parametrize(
    ("original", "edited"),
    [(pair[1], pair[2]) for pair in SAMPLE_PAIRS],
    ids=[pair[0] for pair in SAMPLE_PAIRS],
)
def test_changes_hold_the_original_text_and_never_touch(
    original: str, edited: str
) -> None:
    changes = changes_between(original, edited)
    for change in changes:
        start = change["target"]["range"]["start"]
        end = change["target"]["range"]["end"]
        assert original[start:end] == change["target"]["text"]

    # Each change ends before the previous one in the list starts, so no two
    # changes share a position, even an insertion next to another change.
    for index in range(1, len(changes)):
        previous_start = changes[index - 1]["target"]["range"]["start"]
        current_end = changes[index]["target"]["range"]["end"]
        assert current_end < previous_start


@pytest.mark.parametrize("document_id", ["nda-acme", "msa-globex", "lease-initech"])
def test_service_applies_edits_to_each_sample_document(document_id: str) -> None:
    original = client.get(f"/documents/{document_id}").json()["text"]
    words = original.split(" ")
    # Edit every tenth word, insert at the start, and delete the last character.
    for index in range(0, len(words), 10):
        words[index] = words[index] + "-edited"
    edited = "Draft: " + " ".join(words)[:-1]

    changes = changes_between(original, edited)
    assert len(changes) <= 100
    response = patch_with_etag(f"/documents/{document_id}", json={"changes": changes})
    assert response.status_code == 200, response.json()
    assert response.json()["text"] == edited


def test_stale_original_gives_the_service_409() -> None:
    # Someone else edits the document after this client loaded it.
    store.save_text("redline-check", "Pay in 30 days")
    changes = changes_between("Pay in 30 days", "Pay in 60 days")
    store.save_text("redline-check", "Pay in 45 days")

    response = patch_with_etag("/documents/redline-check", json={"changes": changes})
    assert response.status_code == 409
    assert response.json()["error"] == (
        "changes.0: Text at 7 to 9 in document 'redline-check' is not the target "
        "text; the position may be stale."
    )


# apply_suggestion


def suggestion(target: str, occurrence: int | None, replacement: str) -> dict:
    """The "suggestion" object of POST /documents/{id}/suggestions."""
    return {
        "operation": "replace",
        "target": {"text": target, "occurrence": occurrence},
        "replacement": replacement,
    }


def test_find_match_starts_is_literal_and_non_overlapping() -> None:
    assert find_match_starts("aaaa", "aa") == [0, 2]
    assert find_match_starts("Fee fee", "fee") == [4]
    assert find_match_starts("a.b", ".") == [1]


def test_suggestion_with_occurrence_replaces_that_match() -> None:
    proposed = suggestion("Receiving Party", 2, "Recipient")
    assert apply_suggestion(NDA_TEXT, proposed) == (
        "The Receiving Party shall protect Confidential Information. "
        "The Recipient shall return all Confidential Information within 30 days.\n"
    )


def test_suggestion_without_occurrence_replaces_the_single_match() -> None:
    proposed = suggestion("30 days", None, "60 days")
    assert apply_suggestion(NDA_TEXT, proposed) == NDA_TEXT.replace(
        "30 days", "60 days"
    )


def test_suggestion_whose_target_is_missing_does_not_apply() -> None:
    assert apply_suggestion(NDA_TEXT, suggestion("90 days", None, "60 days")) is None


def test_suggestion_is_case_sensitive() -> None:
    assert apply_suggestion(NDA_TEXT, suggestion("30 DAYS", None, "60 days")) is None


def test_suggestion_with_a_repeated_target_and_no_occurrence_does_not_apply() -> None:
    proposed = suggestion("Receiving Party", None, "Recipient")
    assert apply_suggestion(NDA_TEXT, proposed) is None


@pytest.mark.parametrize("occurrence", [0, 3])
def test_suggestion_with_an_occurrence_out_of_range_does_not_apply(
    occurrence: int,
) -> None:
    proposed = suggestion("Receiving Party", occurrence, "Recipient")
    assert apply_suggestion(NDA_TEXT, proposed) is None


def test_suggestion_with_empty_target_does_not_apply() -> None:
    assert apply_suggestion(NDA_TEXT, suggestion("", None, "x")) is None


@pytest.mark.parametrize(
    "proposed",
    [
        suggestion("Receiving Party", 1, "Recipient"),
        suggestion("Receiving Party", 2, "Recipient"),
        suggestion("within 30 days", None, "promptly"),
        suggestion("Confidential", 2, ""),
    ],
)
def test_suggestion_gives_the_same_text_as_the_service(proposed: dict) -> None:
    response = patch_with_etag("/documents/nda-acme", json={"changes": [proposed]})
    assert response.status_code == 200
    assert apply_suggestion(NDA_TEXT, proposed) == response.json()["text"]


# render_search_results_html


def search_result(
    document_id: str, start: int, before: str, match: str, after: str
) -> dict:
    """One item of the "results" list of GET /documents/search."""
    return {
        "document_id": document_id,
        "start": start,
        "end": start + len(match),
        "before": before,
        "match": match,
        "after": after,
    }


def test_search_results_show_count_document_position_and_highlighted_match() -> None:
    reply = {
        "query": "Tenant",
        "total_matches": 5,
        "results": [
            search_result("lease-initech", 10, "The ", "Tenant", " pays"),
            search_result("nda-acme", 3, "", "Tenant", "."),
        ],
    }
    assert render_search_results_html(reply) == (
        "<p>5 match(es), showing 2:</p><ol>"
        '<li><b>lease-initech</b> at 10: <span style="white-space: pre-wrap;">'
        "The <mark>Tenant</mark> pays</span></li>"
        '<li><b>nda-acme</b> at 3: <span style="white-space: pre-wrap;">'
        "<mark>Tenant</mark>.</span></li></ol>"
    )


def test_search_results_escape_html_in_ids_and_snippets() -> None:
    reply = {
        "query": "<b>",
        "total_matches": 1,
        "results": [search_result("a&b", 0, "x < y ", "<b>", ' "q"')],
    }
    rendered = render_search_results_html(reply)
    assert "<b>a&amp;b</b>" in rendered
    assert "x &lt; y <mark>&lt;b&gt;</mark> &quot;q&quot;" in rendered


def test_search_with_no_matches_says_so() -> None:
    reply = {"query": "zzz", "total_matches": 0, "results": []}
    assert render_search_results_html(reply) == "<p>No matches.</p>"


def test_matches_without_returned_results_show_the_count_and_no_items() -> None:
    reply = {"query": "Tenant", "total_matches": 3, "results": []}
    assert render_search_results_html(reply) == (
        "<p>3 match(es), showing 0:</p><ol></ol>"
    )


def test_search_results_render_a_real_service_reply() -> None:
    params = {"q": "Confidential Information", "document_id": "nda-acme"}
    reply = client.get("/documents/search", params=params).json()
    rendered = render_search_results_html(reply)
    assert rendered.startswith("<p>2 match(es), showing 2:</p>")
    assert rendered.count("<mark>Confidential Information</mark>") == 2
    assert "<b>nda-acme</b> at 34:" in rendered


def test_message_html_escapes() -> None:
    assert render_message_html("Error 422: q <empty>") == (
        "<p>Error 422: q &lt;empty&gt;</p>"
    )
