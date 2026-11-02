from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import llm_client
from app.api import SAMPLE_DOCUMENTS_DIR, app, describe_location, store

client = TestClient(app)

NDA_TEXT = (
    "The Receiving Party shall protect Confidential Information. "
    "The Receiving Party shall return all Confidential Information within 30 days.\n"
)


@pytest.fixture(autouse=True)
def reset_documents() -> None:
    store.load_directory(SAMPLE_DOCUMENTS_DIR)


@pytest.fixture(autouse=True)
def no_real_model_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    # A test that forgets to fake the model gets a 503, never a real API call.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def get_text(document_id: str) -> str:
    response = client.get(f"/documents/{document_id}")
    assert response.status_code == 200
    return response.json()["text"]


def patch_with_etag(path: str, json: object) -> httpx.Response:
    """PATCH as a client should: send the document's current ETag as If-Match."""
    etag = client.get(path).headers["etag"]
    return client.patch(path, json=json, headers={"If-Match": etag})


def patch_expecting_no_change(document_id: str, body: object) -> httpx.Response:
    """Send a PATCH that should be rejected, and check the text did not change."""
    text_before = get_text(document_id)
    response = patch_with_etag(f"/documents/{document_id}", json=body)
    assert get_text(document_id) == text_before
    return response


def patch_raw_json(document_id: str, raw_body: bytes) -> httpx.Response:
    """Send body bytes exactly as given, for JSON that httpx would not produce."""
    etag = client.get(f"/documents/{document_id}").headers["etag"]
    headers = {"Content-Type": "application/json", "If-Match": etag}
    return client.patch(f"/documents/{document_id}", content=raw_body, headers=headers)


# GET /documents


def listed_ids() -> list[str]:
    response = client.get("/documents")
    assert response.status_code == 200
    ids = []
    for entry in response.json()["documents"]:
        ids.append(entry["document_id"])
    return ids


def listed_length(document_id: str) -> int:
    response = client.get("/documents")
    for entry in response.json()["documents"]:
        if entry["document_id"] == document_id:
            return entry["length"]
    raise AssertionError(f"{document_id} is not listed")


def test_list_returns_every_document_sorted_by_id() -> None:
    assert listed_ids() == ["lease-initech", "msa-globex", "nda-acme"]


def test_list_entries_have_id_and_length_but_no_text() -> None:
    response = client.get("/documents")
    for entry in response.json()["documents"]:
        assert set(entry.keys()) == {"document_id", "length"}


def test_list_reports_length_in_characters() -> None:
    assert listed_length("nda-acme") == 138
    for document_id in ["lease-initech", "msa-globex"]:
        assert listed_length(document_id) == len(get_text(document_id))


def test_list_length_counts_code_points_not_bytes() -> None:
    store.save_text("unicode-example", "§ 3 – Zoë")
    assert listed_length("unicode-example") == 9


def test_list_includes_new_documents_in_sorted_order() -> None:
    store.save_text("aaa-example", "abc")
    assert listed_ids() == ["aaa-example", "lease-initech", "msa-globex", "nda-acme"]
    assert listed_length("aaa-example") == 3


def test_list_reflects_edits() -> None:
    body = {"changes": [replace_change(" within 30 days", "")]}
    assert patch_with_etag("/documents/nda-acme", json=body).status_code == 200
    assert listed_length("nda-acme") == 138 - len(" within 30 days")


def test_list_with_no_documents_returns_empty_list(tmp_path: Path) -> None:
    store.load_directory(tmp_path)
    response = client.get("/documents")
    assert response.status_code == 200
    assert response.json() == {"documents": []}


# GET /documents/{document_id}


def test_get_document_returns_current_text() -> None:
    response = client.get("/documents/nda-acme")
    assert response.status_code == 200
    assert response.json() == {"document_id": "nda-acme", "text": NDA_TEXT}


def test_get_unknown_document_returns_404() -> None:
    response = client.get("/documents/nda-missing")
    assert response.status_code == 404
    assert response.json() == {
        "error": "Document 'nda-missing' not found.",
        "code": 404,
    }


# PATCH /documents/{document_id}: request body helpers


def replace_change(text: str, replacement: str, occurrence: int | None = None) -> dict:
    """One change object, as a client sends it."""
    target = {"text": text}
    if occurrence is not None:
        target["occurrence"] = occurrence
    return {"operation": "replace", "target": target, "replacement": replacement}


def change_body(text: str, replacement: str, occurrence: int | None = None) -> dict:
    """A PATCH body holding one change."""
    return {"changes": [replace_change(text, replacement, occurrence)]}


def range_change(text: str, start: int, end: int, replacement: str) -> dict:
    """One change that replaces text[start:end], expected to hold `text`."""
    target = {"text": text, "range": {"start": start, "end": end}}
    return {"operation": "replace", "target": target, "replacement": replacement}


# PATCH /documents/{document_id}: successful edits


def test_patch_second_occurrence_matches_readme_example() -> None:
    body = change_body("Receiving Party", "Recipient", occurrence=2)
    response = patch_with_etag("/documents/nda-acme", json=body)
    assert response.status_code == 200
    assert response.json() == {
        "document_id": "nda-acme",
        "changes": [{"start": 64, "occurrence": 2}],
        "text": (
            "The Receiving Party shall protect Confidential Information. "
            "The Recipient shall return all Confidential Information within 30 days.\n"
        ),
    }


def test_patch_accepts_the_assignment_example_verbatim() -> None:
    store.save_text("assignment-example", "Keep the old text here.")
    body = {
        "changes": [
            {
                "operation": "replace",
                "target": {"text": "old text", "occurrence": 1},
                "replacement": "new text",
            }
        ]
    }
    response = patch_with_etag("/documents/assignment-example", json=body)
    assert response.status_code == 200
    assert response.json()["text"] == "Keep the new text here."

    # The NDA has no "old text", so the same body is a 409 there.
    response = patch_with_etag("/documents/nda-acme", json=body)
    assert response.status_code == 409
    assert response.json() == {
        "error": "changes.0: Target text not found in document 'nda-acme'.",
        "code": 409,
    }


def test_patch_unique_target_without_occurrence() -> None:
    body = change_body("within 30 days", "within 45 days")
    response = patch_with_etag("/documents/nda-acme", json=body)
    assert response.status_code == 200
    assert response.json()["changes"] == [{"start": 122, "occurrence": 1}]
    assert response.json()["text"] == NDA_TEXT.replace("30 days", "45 days")


def test_patch_null_occurrence_behaves_like_omitted() -> None:
    change = replace_change("within 30 days", "soon")
    change["target"]["occurrence"] = None
    response = patch_with_etag("/documents/nda-acme", json={"changes": [change]})
    assert response.status_code == 200
    assert response.json()["changes"] == [{"start": 122, "occurrence": 1}]


def test_patch_saves_the_new_text() -> None:
    patch_with_etag("/documents/nda-acme", json=change_body("30 days", "45 days"))
    assert get_text("nda-acme") == NDA_TEXT.replace("30 days", "45 days")


def test_patch_does_not_change_other_documents() -> None:
    lease_before = get_text("lease-initech")
    patch_with_etag("/documents/nda-acme", json=change_body("30 days", "45 days"))
    assert get_text("lease-initech") == lease_before


def test_sequential_patches_build_on_each_other() -> None:
    first = change_body("Receiving Party", "Recipient", occurrence=2)
    assert patch_with_etag("/documents/nda-acme", json=first).status_code == 200

    # Only one "Receiving Party" is left, so no occurrence is needed now.
    second = change_body("Receiving Party", "Recipient")
    assert patch_with_etag("/documents/nda-acme", json=second).status_code == 200

    third = change_body(" within 30 days", "")
    assert patch_with_etag("/documents/nda-acme", json=third).status_code == 200

    assert get_text("nda-acme") == (
        "The Recipient shall protect Confidential Information. "
        "The Recipient shall return all Confidential Information.\n"
    )


def test_patch_empty_replacement_deletes_the_target() -> None:
    response = patch_with_etag(
        "/documents/nda-acme", json=change_body(" within 30 days", "")
    )
    assert response.status_code == 200
    assert response.json()["text"] == NDA_TEXT.replace(" within 30 days", "")


def test_patch_non_ascii_text_reports_code_point_start() -> None:
    msa_text = get_text("msa-globex")
    body = change_body("José Álvarez", "Renée Côté")
    response = patch_with_etag("/documents/msa-globex", json=body)
    assert response.status_code == 200
    assert response.json()["changes"][0]["start"] == msa_text.index("José Álvarez")
    assert response.json()["text"] == msa_text.replace("José Álvarez", "Renée Côté")


# PATCH /documents/{document_id}: several changes in one request


def test_patch_applies_changes_in_order_and_reports_each_one() -> None:
    body = {
        "changes": [
            replace_change("Receiving Party", "Recipient", occurrence=2),
            replace_change("within 30 days", "within 60 days"),
        ]
    }
    response = patch_with_etag("/documents/nda-acme", json=body)
    assert response.status_code == 200
    assert response.json() == {
        "document_id": "nda-acme",
        # 116, not 122: start is measured in the text after the first change.
        "changes": [{"start": 64, "occurrence": 2}, {"start": 116, "occurrence": 1}],
        "text": (
            "The Receiving Party shall protect Confidential Information. "
            "The Recipient shall return all Confidential Information within 60 days.\n"
        ),
    }
    assert get_text("nda-acme") == response.json()["text"]


def test_patch_later_change_can_target_text_from_an_earlier_one() -> None:
    body = {
        "changes": [
            replace_change("30 days", "30 calendar days"),
            replace_change("calendar", "business"),
        ]
    }
    response = patch_with_etag("/documents/nda-acme", json=body)
    assert response.status_code == 200
    assert response.json()["text"] == NDA_TEXT.replace("30 days", "30 business days")


def test_patch_same_phrase_twice_in_order() -> None:
    # The first change leaves one "Receiving Party", so the second needs no
    # occurrence.
    body = {
        "changes": [
            replace_change("Receiving Party", "Recipient", occurrence=1),
            replace_change("Receiving Party", "Recipient"),
        ]
    }
    response = patch_with_etag("/documents/nda-acme", json=body)
    assert response.status_code == 200
    assert response.json()["text"] == NDA_TEXT.replace("Receiving Party", "Recipient")


def test_patch_failure_in_a_later_change_saves_nothing() -> None:
    # The first change fits; the second is ambiguous. Neither is saved.
    body = {
        "changes": [
            replace_change("within 30 days", "within 60 days"),
            replace_change("Receiving Party", "Recipient"),
        ]
    }
    response = patch_expecting_no_change("nda-acme", body)
    assert response.status_code == 409
    assert response.json() == {
        "error": (
            "changes.1: Target text appears 2 times in document 'nda-acme'; "
            "specify occurrence 1 to 2."
        ),
        "code": 409,
    }
    assert get_text("nda-acme") == NDA_TEXT


def test_patch_accepts_100_changes() -> None:
    # Each change turns the number into the next one: "0" -> "1" -> ... -> "100".
    store.save_text("counter-example", "0")
    changes = []
    for number in range(100):
        changes.append(replace_change(str(number), str(number + 1)))
    response = patch_with_etag("/documents/counter-example", json={"changes": changes})
    assert response.status_code == 200
    assert response.json()["text"] == "100"
    assert len(response.json()["changes"]) == 100


# PATCH /documents/{document_id}: changes by position (range)


def test_patch_range_replaces_the_expected_text() -> None:
    body = {"changes": [range_change("Receiving Party", 64, 79, "Recipient")]}
    response = patch_with_etag("/documents/nda-acme", json=body)
    assert response.status_code == 200
    assert response.json()["changes"] == [{"start": 64, "occurrence": None}]
    assert response.json()["text"] == (
        "The Receiving Party shall protect Confidential Information. "
        "The Recipient shall return all Confidential Information within 30 days.\n"
    )


def test_patch_range_after_an_earlier_change_uses_shifted_positions() -> None:
    # "Recipient" is 6 characters shorter than "Receiving Party", so "30 days"
    # moves from 129 to 123 once the first change is applied.
    body = {
        "changes": [
            replace_change("Receiving Party", "Recipient", occurrence=1),
            range_change("30 days", 123, 130, "60 days"),
        ]
    }
    response = patch_with_etag("/documents/nda-acme", json=body)
    assert response.status_code == 200
    assert response.json()["changes"][1] == {"start": 123, "occurrence": None}
    assert response.json()["text"].endswith("within 60 days.\n")


def test_patch_range_with_stale_text_returns_409() -> None:
    # 63 to 78 is one character off from "Receiving Party" (64 to 79).
    body = {"changes": [range_change("Receiving Party", 63, 78, "Recipient")]}
    response = patch_expecting_no_change("nda-acme", body)
    assert response.status_code == 409
    assert response.json() == {
        "error": (
            "changes.0: Text at 63 to 78 in document 'nda-acme' is not the "
            "target text; the position may be stale."
        ),
        "code": 409,
    }


def test_patch_range_past_the_end_returns_409() -> None:
    body = {"changes": [range_change("30 days", 200, 207, "60 days")]}
    response = patch_expecting_no_change("nda-acme", body)
    assert response.status_code == 409
    assert response.json() == {
        "error": (
            "changes.0: Range ends at 207, but document 'nda-acme' has 138 "
            "characters at this change."
        ),
        "code": 409,
    }


def test_patch_rejects_the_assignment_range_example_as_written() -> None:
    # The assignment's position-only form has no expected text, so a stale position
    # would silently edit the wrong words. The corrected form is above.
    body = {
        "changes": [
            {
                "operation": "replace",
                "range": {"start": 100, "end": 108},
                "text": "new text",
            }
        ]
    }
    response = patch_expecting_no_change("nda-acme", body)
    assert response.status_code == 422
    assert response.json() == {"error": MISPLACED_RANGE_ERROR, "code": 422}


MISPLACED_RANGE_ERROR = (
    "changes.0: Put the position in target.range, the text expected there in "
    "target.text, and the new text in replacement."
)


def test_patch_range_beside_a_valid_target_gets_the_plain_message() -> None:
    change = replace_change("Receiving Party", "Recipient", occurrence=1)
    change["range"] = {"start": 4, "end": 19}
    response = patch_expecting_no_change("nda-acme", {"changes": [change]})
    assert response.status_code == 422
    assert response.json() == {"error": MISPLACED_RANGE_ERROR, "code": 422}


def test_patch_plain_range_message_names_only_that_change() -> None:
    body = {
        "changes": [
            replace_change("30 days", "60 days"),
            {"operation": "replace", "range": {"start": 0, "end": 3}, "text": "A"},
            {"operation": "replace", "target": {"text": "x"}},
        ]
    }
    response = patch_expecting_no_change("nda-acme", body)
    assert response.status_code == 422
    assert response.json()["error"] == (
        "changes.1: Put the position in target.range, the text expected there in "
        "target.text, and the new text in replacement.; "
        "changes.2.replacement: Field required"
    )


def test_patch_other_unknown_fields_keep_the_usual_message() -> None:
    change = replace_change("30 days", "60 days")
    change["position"] = {"start": 0, "end": 3}
    response = patch_expecting_no_change("nda-acme", {"changes": [change]})
    assert response.json()["error"] == (
        "changes.0.position: Extra inputs are not permitted"
    )


def test_patch_occurrence_and_range_together_returns_422() -> None:
    change = range_change("Receiving Party", 64, 79, "Recipient")
    change["target"]["occurrence"] = 2
    response = patch_expecting_no_change("nda-acme", {"changes": [change]})
    assert response.status_code == 422
    assert response.json() == {
        "error": "changes.0.target: Send occurrence or range, not both.",
        "code": 422,
    }


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        (79, 64, "changes.1.target.range: start must not be after end."),
        (
            64,
            70,
            "changes.1.target.range: The range covers 6 characters, "
            "but target text has 15.",
        ),
        (
            -1,
            14,
            "changes.1.target.range.start: Input should be greater than or equal to 0",
        ),
    ],
)
def test_patch_impossible_range_returns_422(start: int, end: int, message: str) -> None:
    # The first change is valid; the error still names the second.
    body = {
        "changes": [
            replace_change("within 30 days", "within 60 days"),
            range_change("Receiving Party", start, end, "Recipient"),
        ]
    }
    response = patch_expecting_no_change("nda-acme", body)
    assert response.status_code == 422
    assert response.json() == {"error": message, "code": 422}


# PATCH /documents/{document_id}: insertion (empty text at a zero-width range)


@pytest.mark.parametrize("position", [0, 64, len(NDA_TEXT)])
def test_patch_zero_width_range_inserts(position: int) -> None:
    body = {"changes": [range_change("", position, position, "[NEW] ")]}
    response = patch_with_etag("/documents/nda-acme", json=body)
    assert response.status_code == 200
    assert response.json()["changes"] == [{"start": position, "occurrence": None}]
    expected_text = NDA_TEXT[:position] + "[NEW] " + NDA_TEXT[position:]
    assert response.json()["text"] == expected_text
    assert get_text("nda-acme") == expected_text


def test_patch_insertion_after_an_earlier_change_uses_shifted_positions() -> None:
    # "30" ends at 131 in the original text, and at 125 after the first change
    # shortens "Receiving Party" (15 characters) to "Recipient" (9).
    body = {
        "changes": [
            replace_change("Receiving Party", "Recipient", occurrence=2),
            range_change("", 125, 125, " calendar"),
        ]
    }
    response = patch_with_etag("/documents/nda-acme", json=body)
    assert response.status_code == 200
    assert response.json()["changes"] == [
        {"start": 64, "occurrence": 2},
        {"start": 125, "occurrence": None},
    ]
    assert response.json()["text"] == (
        "The Receiving Party shall protect Confidential Information. "
        "The Recipient shall return all Confidential Information within 30 "
        "calendar days.\n"
    )


def test_patch_zero_width_range_past_the_end_returns_409() -> None:
    body = {"changes": [range_change("", 139, 139, "x")]}
    response = patch_expecting_no_change("nda-acme", body)
    assert response.status_code == 409
    assert response.json() == {
        "error": (
            "changes.0: Range ends at 139, but document 'nda-acme' has 138 "
            "characters at this change."
        ),
        "code": 409,
    }


def test_patch_empty_text_with_non_empty_range_returns_422() -> None:
    body = {"changes": [range_change("", 5, 9, "x")]}
    response = patch_expecting_no_change("nda-acme", body)
    assert response.status_code == 422
    assert response.json() == {
        "error": (
            "changes.0.target.range: The range covers 4 characters, "
            "but target text has 0."
        ),
        "code": 422,
    }


def test_patch_empty_text_with_occurrence_returns_422() -> None:
    response = patch_expecting_no_change("nda-acme", change_body("", "x", 1))
    assert response.status_code == 422
    assert response.json() == {
        "error": "changes.0.target.text: String should have at least 1 character",
        "code": 422,
    }


# PATCH /documents/{document_id}: target does not fit the text (409)


def test_patch_repeated_target_without_occurrence_returns_409() -> None:
    body = change_body("Receiving Party", "Recipient")
    response = patch_expecting_no_change("nda-acme", body)
    assert response.status_code == 409
    assert response.json() == {
        "error": (
            "changes.0: Target text appears 2 times in document 'nda-acme'; "
            "specify occurrence 1 to 2."
        ),
        "code": 409,
    }


def test_patch_occurrence_beyond_count_returns_409() -> None:
    body = change_body("Receiving Party", "Recipient", occurrence=3)
    response = patch_expecting_no_change("nda-acme", body)
    assert response.status_code == 409
    assert response.json() == {
        "error": (
            "changes.0: Occurrence 3 requested, but target text appears 2 times "
            "in document 'nda-acme'."
        ),
        "code": 409,
    }


def test_patch_missing_target_returns_409() -> None:
    body = change_body("Disclosing Party", "Discloser")
    response = patch_expecting_no_change("nda-acme", body)
    assert response.status_code == 409
    assert response.json() == {
        "error": "changes.0: Target text not found in document 'nda-acme'.",
        "code": 409,
    }


def test_patch_whitespace_target_is_not_stripped_or_rejected() -> None:
    nda_space_count = NDA_TEXT.count(" ")
    response = patch_expecting_no_change("nda-acme", change_body(" ", "_"))
    assert response.status_code == 409
    assert f"appears {nda_space_count} times" in response.json()["error"]


# PATCH /documents/{document_id}: unknown document (404)


def test_patch_unknown_document_returns_404() -> None:
    body = change_body("Receiving Party", "Recipient")
    # Any version will do: the document is looked up before its version.
    headers = {"If-Match": '"1"'}
    response = client.patch("/documents/nda-missing", json=body, headers=headers)
    assert response.status_code == 404
    assert response.json() == {
        "error": "Document 'nda-missing' not found.",
        "code": 404,
    }


# PATCH /documents/{document_id}: invalid request body (422)


def test_patch_old_flat_body_returns_422() -> None:
    body = {"target": "Receiving Party", "replacement": "Recipient", "occurrence": 2}
    response = patch_expecting_no_change("nda-acme", body)
    assert response.status_code == 422
    assert response.json() == {
        "error": (
            "changes: Field required; target: Extra inputs are not permitted; "
            "replacement: Extra inputs are not permitted; "
            "occurrence: Extra inputs are not permitted"
        ),
        "code": 422,
    }


def test_patch_empty_change_list_returns_422() -> None:
    response = patch_expecting_no_change("nda-acme", {"changes": []})
    assert response.status_code == 422
    assert response.json() == {
        "error": "changes: List should have at least 1 item after validation, not 0",
        "code": 422,
    }


def test_patch_more_than_100_changes_returns_422() -> None:
    changes = []
    for _ in range(101):
        changes.append(replace_change("within 30 days", "within 30 days"))
    response = patch_expecting_no_change("nda-acme", {"changes": changes})
    assert response.status_code == 422
    assert response.json() == {
        "error": (
            "changes: List should have at most 100 items after validation, not 101"
        ),
        "code": 422,
    }


@pytest.mark.parametrize(
    ("operation", "message"),
    [
        ("delete", "changes.0.operation: Input should be 'replace'"),
        (None, "changes.0.operation: Field required"),
    ],
)
def test_patch_unknown_or_missing_operation_returns_422(
    operation: str | None, message: str
) -> None:
    change = replace_change("within 30 days", "within 60 days")
    if operation is None:
        del change["operation"]
    else:
        change["operation"] = operation
    response = patch_expecting_no_change("nda-acme", {"changes": [change]})
    assert response.status_code == 422
    assert response.json() == {"error": message, "code": 422}


def test_patch_empty_target_returns_422() -> None:
    response = patch_expecting_no_change("nda-acme", change_body("", "x"))
    assert response.status_code == 422
    assert response.json() == {
        "error": "changes.0.target.text: String should have at least 1 character",
        "code": 422,
    }


@pytest.mark.parametrize("occurrence", [0, -1])
def test_patch_occurrence_below_one_returns_422(occurrence: int) -> None:
    body = change_body("Receiving Party", "R", occurrence=occurrence)
    response = patch_expecting_no_change("nda-acme", body)
    assert response.status_code == 422
    assert response.json() == {
        "error": (
            "changes.0.target.occurrence: Input should be greater than or equal to 1"
        ),
        "code": 422,
    }


@pytest.mark.parametrize("occurrence", ["2", True, 1.5, 2.0])
def test_patch_non_integer_occurrence_returns_422(occurrence: object) -> None:
    change = replace_change("Receiving Party", "R")
    change["target"]["occurrence"] = occurrence
    response = patch_expecting_no_change("nda-acme", {"changes": [change]})
    assert response.status_code == 422
    assert response.json() == {
        "error": "changes.0.target.occurrence: Input should be a valid integer",
        "code": 422,
    }


def test_patch_misspelled_field_returns_422() -> None:
    change = replace_change("Receiving Party", "R")
    change["target"]["occurence"] = 2
    response = patch_expecting_no_change("nda-acme", {"changes": [change]})
    assert response.status_code == 422
    assert response.json() == {
        "error": "changes.0.target.occurence: Extra inputs are not permitted",
        "code": 422,
    }


def test_patch_missing_replacement_returns_422() -> None:
    change = replace_change("Receiving Party", "R")
    del change["replacement"]
    response = patch_expecting_no_change("nda-acme", {"changes": [change]})
    assert response.status_code == 422
    assert response.json() == {
        "error": "changes.0.replacement: Field required",
        "code": 422,
    }


def test_patch_non_string_target_returns_422() -> None:
    change = replace_change("Receiving Party", "x")
    change["target"]["text"] = 5
    response = patch_expecting_no_change("nda-acme", {"changes": [change]})
    assert response.status_code == 422
    assert response.json() == {
        "error": "changes.0.target.text: Input should be a valid string",
        "code": 422,
    }


def test_patch_reports_every_invalid_field() -> None:
    response = patch_expecting_no_change("nda-acme", {"changes": [{}]})
    assert response.status_code == 422
    assert response.json() == {
        "error": (
            "changes.0.operation: Field required; changes.0.target: Field required; "
            "changes.0.replacement: Field required"
        ),
        "code": 422,
    }


def test_patch_body_that_is_not_an_object_returns_422() -> None:
    response = patch_expecting_no_change("nda-acme", ["changes"])
    assert response.status_code == 422
    assert response.json() == {
        "error": "body: Input should be a valid dictionary or object to extract "
        "fields from",
        "code": 422,
    }


def test_patch_malformed_json_returns_422() -> None:
    response = patch_raw_json("nda-acme", b'{"changes": [')
    assert response.status_code == 422
    assert response.json() == {"error": "Request body is not valid JSON.", "code": 422}
    assert get_text("nda-acme") == NDA_TEXT


def test_patch_without_body_returns_422() -> None:
    # The current version, so only the missing body is wrong.
    headers = {"If-Match": '"1"'}
    response = client.patch("/documents/nda-acme", headers=headers)
    assert response.status_code == 422
    assert response.json() == {"error": "body: Field required", "code": 422}
    assert get_text("nda-acme") == NDA_TEXT


def test_patch_replacement_with_lone_surrogate_returns_422() -> None:
    # "\ud800" is valid JSON but not valid Unicode text; saving it would make
    # every later response for this document fail.
    raw_body = (
        b'{"changes": [{"operation": "replace", "target": {"text": "30 days"}, '
        b'"replacement": "\\ud800"}]}'
    )
    response = patch_raw_json("nda-acme", raw_body)
    assert response.status_code == 422
    assert response.json() == {
        "error": "changes.0.replacement: Text must be valid Unicode.",
        "code": 422,
    }
    assert get_text("nda-acme") == NDA_TEXT


def test_patch_target_with_lone_surrogate_returns_422() -> None:
    # pydantic currently rejects this first, with its own message; the explicit
    # check in patch_document guarantees a 422 either way.
    raw_body = (
        b'{"changes": [{"operation": "replace", "target": {"text": "\\ud800"}, '
        b'"replacement": "x"}]}'
    )
    response = patch_raw_json("nda-acme", raw_body)
    assert response.status_code == 422
    assert response.json()["code"] == 422
    assert response.json()["error"].startswith("changes.0.target.text: ")
    assert get_text("nda-acme") == NDA_TEXT


def test_patch_lone_surrogate_in_a_later_change_is_named() -> None:
    raw_body = (
        b'{"changes": ['
        b'{"operation": "replace", "target": {"text": "30 days"}, '
        b'"replacement": "60 days"}, '
        b'{"operation": "replace", "target": {"text": "Confidential"}, '
        b'"replacement": "\\ud800"}]}'
    )
    response = patch_raw_json("nda-acme", raw_body)
    assert response.status_code == 422
    assert response.json() == {
        "error": "changes.1.replacement: Text must be valid Unicode.",
        "code": 422,
    }
    assert get_text("nda-acme") == NDA_TEXT


@pytest.mark.parametrize(
    ("location", "path"),
    [
        (("body",), "body"),
        (("body", "target"), "target"),
        (("query", "limit"), "limit"),
        (("body", "changes", 0, "target", "text"), "changes.0.target.text"),
    ],
)
def test_validation_error_location_becomes_a_dotted_path(
    location: tuple, path: str
) -> None:
    assert describe_location(location) == path


# ETag and If-Match: reject an edit made from an outdated read (412)


ETAG_CHANGE = change_body("within 30 days", "within 45 days")
OTHER_ETAG_CHANGE = change_body("Receiving Party", "Recipient", occurrence=1)
MISSING_TARGET = change_body("Disclosing Party", "Discloser")

STALE_ERROR = {
    "error": "Document 'nda-acme' has changed since it was read; get it again "
    "and retry.",
    "code": 412,
}


def etag_of(document_id: str) -> str:
    response = client.get(f"/documents/{document_id}")
    assert response.status_code == 200
    return response.headers["etag"]


def patch_if_match(document_id: str, body: object, if_match: str) -> httpx.Response:
    headers = {"If-Match": if_match}
    return client.patch(f"/documents/{document_id}", json=body, headers=headers)


def test_get_returns_a_quoted_etag_and_the_same_body() -> None:
    response = client.get("/documents/nda-acme")
    etag = response.headers["etag"]
    assert len(etag) > 2
    assert etag.startswith('"')
    assert etag.endswith('"')
    assert response.json() == {"document_id": "nda-acme", "text": NDA_TEXT}


def test_etag_is_stable_while_the_document_is_unchanged() -> None:
    first = etag_of("nda-acme")
    client.get("/documents/search", params={"q": "Receiving Party"})
    assert etag_of("nda-acme") == first


def test_patch_with_current_etag_succeeds_and_returns_the_new_etag() -> None:
    old_etag = etag_of("nda-acme")
    response = patch_if_match("nda-acme", ETAG_CHANGE, old_etag)
    assert response.status_code == 200
    assert set(response.json().keys()) == {"document_id", "changes", "text"}
    new_etag = response.headers["etag"]
    assert new_etag != old_etag
    assert etag_of("nda-acme") == new_etag


def test_second_client_with_the_old_etag_gets_412() -> None:
    etag_read_by_both = etag_of("nda-acme")
    first = patch_if_match("nda-acme", ETAG_CHANGE, etag_read_by_both)
    assert first.status_code == 200

    second = patch_if_match("nda-acme", OTHER_ETAG_CHANGE, etag_read_by_both)
    assert second.status_code == 412
    assert second.json() == STALE_ERROR
    assert get_text("nda-acme") == NDA_TEXT.replace("30 days", "45 days")


def test_client_can_retry_after_reading_again() -> None:
    stale_etag = etag_of("nda-acme")
    assert patch_with_etag("/documents/nda-acme", json=ETAG_CHANGE).status_code == 200
    assert patch_if_match("nda-acme", OTHER_ETAG_CHANGE, stale_etag).status_code == 412

    fresh_etag = etag_of("nda-acme")
    assert patch_if_match("nda-acme", OTHER_ETAG_CHANGE, fresh_etag).status_code == 200


def test_patch_without_if_match_is_allowed() -> None:
    # If-Match is optional: without it, the edit is saved unchecked. This and
    # test_patch_with_if_match_star_is_allowed pin that choice.
    old_etag = etag_of("nda-acme")
    response = client.patch("/documents/nda-acme", json=ETAG_CHANGE)
    assert response.status_code == 200
    assert response.headers["etag"] != old_etag


def test_rejected_patch_does_not_change_the_etag() -> None:
    etag = etag_of("nda-acme")
    response = patch_if_match("nda-acme", MISSING_TARGET, etag)
    assert response.status_code == 409
    assert response.json() == {
        "error": "changes.0: Target text not found in document 'nda-acme'.",
        "code": 409,
    }
    assert etag_of("nda-acme") == etag


def test_each_document_has_its_own_etag() -> None:
    lease_etag = etag_of("lease-initech")
    assert patch_with_etag("/documents/nda-acme", json=ETAG_CHANGE).status_code == 200
    assert etag_of("lease-initech") == lease_etag

    lease_change = change_body("Tenant", "Renter", occurrence=1)
    response = patch_if_match("lease-initech", lease_change, lease_etag)
    assert response.status_code == 200


@pytest.mark.parametrize(
    "if_match",
    [
        "abc",
        '"abc"',
        '""',
        'W/"1"',
        # All digits but far too long: must be a 412, not a 500 from int().
        '"' + "9" * 5000 + '"',
    ],
)
def test_if_match_that_is_not_our_etag_returns_412(if_match: str) -> None:
    response = patch_if_match("nda-acme", ETAG_CHANGE, if_match)
    assert response.status_code == 412
    assert response.json() == STALE_ERROR
    assert get_text("nda-acme") == NDA_TEXT


def test_patch_with_if_match_star_is_allowed() -> None:
    # HTTP defines "*" as "any current version of a document that exists".
    assert patch_with_etag("/documents/nda-acme", json=ETAG_CHANGE).status_code == 200
    assert patch_if_match("nda-acme", OTHER_ETAG_CHANGE, "*").status_code == 200


def test_if_match_accepts_the_current_version_without_quotes() -> None:
    response = patch_if_match("nda-acme", ETAG_CHANGE, "1")
    assert response.status_code == 200
    assert response.headers["ETag"] == '"2"'


def test_unknown_document_with_if_match_returns_404() -> None:
    response = patch_if_match("nda-missing", ETAG_CHANGE, etag_of("nda-acme"))
    assert response.status_code == 404
    assert response.json() == {
        "error": "Document 'nda-missing' not found.",
        "code": 404,
    }


def test_stale_etag_is_reported_before_a_missing_target() -> None:
    stale_etag = etag_of("nda-acme")
    assert patch_with_etag("/documents/nda-acme", json=ETAG_CHANGE).status_code == 200
    response = patch_if_match("nda-acme", MISSING_TARGET, stale_etag)
    assert response.status_code == 412


def test_stale_etag_rejects_an_insertion_that_checks_no_text() -> None:
    # An insertion's expected text is "", so only the version can catch this.
    stale_etag = etag_of("nda-acme")
    assert patch_with_etag("/documents/nda-acme", json=ETAG_CHANGE).status_code == 200
    insertion = {"changes": [range_change("", 0, 0, "DRAFT. ")]}
    response = patch_if_match("nda-acme", insertion, stale_etag)
    assert response.status_code == 412
    assert get_text("nda-acme") == NDA_TEXT.replace("30 days", "45 days")


def test_invalid_body_is_reported_before_a_stale_etag() -> None:
    stale_etag = etag_of("nda-acme")
    assert patch_with_etag("/documents/nda-acme", json=ETAG_CHANGE).status_code == 200
    response = patch_if_match("nda-acme", change_body("", "x"), stale_etag)
    assert response.status_code == 422


# Errors outside the document routes


def test_unknown_route_returns_404_error_body() -> None:
    response = client.get("/no-such-route")
    assert response.status_code == 404
    assert response.json() == {"error": "Not Found", "code": 404}


def test_wrong_method_returns_405_error_body() -> None:
    response = client.delete("/documents/nda-acme")
    assert response.status_code == 405
    assert response.json() == {"error": "Method Not Allowed", "code": 405}


def test_unexpected_error_returns_generic_500(monkeypatch: pytest.MonkeyPatch) -> None:
    # Fault injection: no known request reaches the 500 handler, so force one.
    def fail(document_id: str) -> str:
        raise RuntimeError("secret document text")

    monkeypatch.setattr(store, "get_text", fail)
    client_without_reraise = TestClient(app, raise_server_exceptions=False)
    response = client_without_reraise.get("/documents/nda-acme")
    assert response.status_code == 500
    assert response.json() == {"error": "Internal server error.", "code": 500}


# GET /documents/search


def search(params: dict) -> httpx.Response:
    return client.get("/documents/search", params=params)


def result_positions(response: httpx.Response) -> list[tuple[str, int]]:
    positions = []
    for result in response.json()["results"]:
        positions.append((result["document_id"], result["start"]))
    return positions


def save_needle_documents() -> None:
    """Add small documents for selection, ordering, and limit tests.

    "needle" does not appear in the sample documents, so only these match.
    """
    store.save_text("bravo-doc", "needle needle")
    store.save_text("alpha-doc", "needle")
    store.save_text("charlie-doc", "no match")


def test_search_route_and_document_route_do_not_collide() -> None:
    search_response = search({"q": "Receiving Party", "document_id": "nda-acme"})
    assert search_response.status_code == 200
    assert search_response.json()["query"] == "Receiving Party"
    assert search_response.json()["total_matches"] == 2

    document_response = client.get("/documents/nda-acme")
    assert document_response.status_code == 200
    assert document_response.json() == {"document_id": "nda-acme", "text": NDA_TEXT}


def test_old_search_url_returns_404() -> None:
    response = client.get("/search", params={"q": "Receiving Party"})
    assert response.status_code == 404
    assert response.json() == {"error": "Not Found", "code": 404}


def test_search_matches_readme_example() -> None:
    params = {"q": "Confidential Information", "document_id": "nda-acme", "limit": 1}
    response = search(params)
    assert response.status_code == 200
    assert response.json() == {
        "query": "Confidential Information",
        "total_matches": 2,
        "results": [
            {
                "document_id": "nda-acme",
                "start": 34,
                "end": 58,
                "before": "The Receiving Party shall protect ",
                "match": "Confidential Information",
                "after": ". The Receiving Party shall return all C",
            }
        ],
    }


def test_search_without_matches_returns_empty_results() -> None:
    response = search({"q": "Indemnification"})
    assert response.status_code == 200
    assert response.json() == {
        "query": "Indemnification",
        "total_matches": 0,
        "results": [],
    }


def test_search_returns_non_overlapping_repeated_matches() -> None:
    store.save_text("overlap-example", "aaaa")
    response = search({"q": "aa", "document_id": "overlap-example"})
    assert response.status_code == 200
    assert response.json()["total_matches"] == 2
    assert response.json()["results"] == [
        {
            "document_id": "overlap-example",
            "start": 0,
            "end": 2,
            "before": "",
            "match": "aa",
            "after": "aa",
        },
        {
            "document_id": "overlap-example",
            "start": 2,
            "end": 4,
            "before": "aa",
            "match": "aa",
            "after": "",
        },
    ]


def test_search_non_ascii_text_preserves_positions_and_snippet() -> None:
    store.save_text("unicode-example", "§ 3 – Zoë’s “Fee” is due")
    response = search({"q": "“Fee”", "document_id": "unicode-example"})
    assert response.status_code == 200
    assert response.json()["results"] == [
        {
            "document_id": "unicode-example",
            "start": 12,
            "end": 17,
            "before": "§ 3 – Zoë’s ",
            "match": "“Fee”",
            "after": " is due",
        }
    ]


def test_search_without_document_id_searches_all_documents_in_order() -> None:
    save_needle_documents()
    response = search({"q": "needle"})
    assert response.status_code == 200
    assert response.json()["total_matches"] == 3
    assert result_positions(response) == [
        ("alpha-doc", 0),
        ("bravo-doc", 0),
        ("bravo-doc", 7),
    ]


def test_search_selected_documents_only() -> None:
    save_needle_documents()
    response = search({"q": "needle", "document_id": ["alpha-doc", "charlie-doc"]})
    assert response.json()["total_matches"] == 1
    assert result_positions(response) == [("alpha-doc", 0)]


def test_search_orders_by_document_id_regardless_of_request_order() -> None:
    save_needle_documents()
    response = search({"q": "needle", "document_id": ["bravo-doc", "alpha-doc"]})
    assert result_positions(response) == [
        ("alpha-doc", 0),
        ("bravo-doc", 0),
        ("bravo-doc", 7),
    ]


def test_search_ignores_duplicate_document_ids() -> None:
    save_needle_documents()
    response = search({"q": "needle", "document_id": ["bravo-doc", "bravo-doc"]})
    assert response.json()["total_matches"] == 2
    assert result_positions(response) == [("bravo-doc", 0), ("bravo-doc", 7)]


def test_search_unknown_document_id_fails_whole_request() -> None:
    response = search({"q": "30", "document_id": ["nda-acme", "nda-missing"]})
    assert response.status_code == 404
    assert response.json() == {
        "error": "Document 'nda-missing' not found.",
        "code": 404,
    }


def test_search_several_unknown_ids_names_the_first_in_request_order() -> None:
    params = {"q": "30", "document_id": ["nda-acme", "zz-missing", "aa-missing"]}
    response = search(params)
    assert response.status_code == 404
    assert response.json() == {
        "error": "Document 'zz-missing' not found.",
        "code": 404,
    }


def test_search_empty_document_id_is_an_unknown_document() -> None:
    response = search({"q": "30", "document_id": ""})
    assert response.status_code == 404
    assert response.json() == {"error": "Document '' not found.", "code": 404}


def test_search_default_limit_is_10() -> None:
    store.save_text("repeat-example", "x " * 12)
    response = search({"q": "x", "document_id": "repeat-example"})
    assert response.json()["total_matches"] == 12
    assert len(response.json()["results"]) == 10


def test_search_limit_cuts_results_across_documents() -> None:
    save_needle_documents()
    response = search({"q": "needle", "limit": 2})
    assert response.json()["total_matches"] == 3
    assert result_positions(response) == [("alpha-doc", 0), ("bravo-doc", 0)]


def test_search_limit_larger_than_match_count_returns_all() -> None:
    save_needle_documents()
    response = search({"q": "needle", "limit": 100})
    assert response.json()["total_matches"] == 3
    assert len(response.json()["results"]) == 3


@pytest.mark.parametrize(
    ("limit", "message"),
    [
        ("0", "limit: Input should be greater than or equal to 1"),
        ("101", "limit: Input should be less than or equal to 100"),
        ("500", "limit: Input should be less than or equal to 100"),
        (
            "1.5",
            "limit: Input should be a valid integer, "
            "unable to parse string as an integer",
        ),
        (
            "ten",
            "limit: Input should be a valid integer, "
            "unable to parse string as an integer",
        ),
    ],
)
def test_search_invalid_limit_returns_422(limit: str, message: str) -> None:
    response = search({"q": "shall", "limit": limit})
    assert response.status_code == 422
    assert response.json() == {"error": message, "code": 422}


def save_paging_documents() -> None:
    """The needle documents plus delta-doc: four matches, in this order:

    ("alpha-doc", 0), ("bravo-doc", 0), ("bravo-doc", 7), ("delta-doc", 0)
    """
    save_needle_documents()
    store.save_text("delta-doc", "needle")


def test_search_offset_skips_matches_in_the_stable_order() -> None:
    save_paging_documents()
    response = search({"q": "needle", "offset": 1})
    assert response.status_code == 200
    assert result_positions(response) == [
        ("bravo-doc", 0),
        ("bravo-doc", 7),
        ("delta-doc", 0),
    ]


def test_search_offset_and_limit_select_one_page() -> None:
    save_paging_documents()
    response = search({"q": "needle", "offset": 1, "limit": 2})
    assert result_positions(response) == [("bravo-doc", 0), ("bravo-doc", 7)]


def test_search_offset_can_start_in_the_middle_of_a_document() -> None:
    save_paging_documents()
    response = search({"q": "needle", "offset": 2, "limit": 1})
    assert result_positions(response) == [("bravo-doc", 7)]


def test_search_total_matches_ignores_offset() -> None:
    save_paging_documents()
    response = search({"q": "needle", "offset": 3})
    assert response.json()["total_matches"] == 4
    assert result_positions(response) == [("delta-doc", 0)]


@pytest.mark.parametrize("offset", [4, 50])
def test_search_offset_at_or_past_the_end_returns_empty_results(offset: int) -> None:
    save_paging_documents()
    response = search({"q": "needle", "offset": offset})
    assert response.status_code == 200
    assert response.json() == {"query": "needle", "total_matches": 4, "results": []}


def test_search_pages_join_up_to_the_full_result_list() -> None:
    save_paging_documents()
    everything = result_positions(search({"q": "needle", "limit": 100}))

    paged = []
    for offset in [0, 2, 4]:
        page = search({"q": "needle", "offset": offset, "limit": 2})
        paged.extend(result_positions(page))
    assert paged == everything


def test_search_offset_works_with_selected_documents() -> None:
    save_paging_documents()
    params = {"q": "needle", "document_id": ["delta-doc", "bravo-doc"], "offset": 1}
    response = search(params)
    assert response.json()["total_matches"] == 3
    assert result_positions(response) == [("bravo-doc", 7), ("delta-doc", 0)]


@pytest.mark.parametrize(
    ("offset", "message"),
    [
        ("-1", "offset: Input should be greater than or equal to 0"),
        (
            "two",
            "offset: Input should be a valid integer, "
            "unable to parse string as an integer",
        ),
    ],
)
def test_search_invalid_offset_returns_422(offset: str, message: str) -> None:
    response = search({"q": "needle", "offset": offset})
    assert response.status_code == 422
    assert response.json() == {"error": message, "code": 422}


def test_search_empty_query_returns_422() -> None:
    response = search({"q": ""})
    assert response.status_code == 422
    assert response.json() == {
        "error": "q: String should have at least 1 character",
        "code": 422,
    }


def test_search_missing_query_returns_422() -> None:
    # A document read would give 404 "Document 'search' not found." instead.
    response = client.get("/documents/search")
    assert response.status_code == 422
    assert response.json() == {"error": "q: Field required", "code": 422}


def test_search_whitespace_query_is_matched_literally() -> None:
    response = search({"q": " ", "document_id": "nda-acme"})
    assert response.status_code == 200
    assert response.json()["total_matches"] == NDA_TEXT.count(" ")
    assert len(response.json()["results"]) == 10
    for result in response.json()["results"]:
        assert result["match"] == " "


def test_search_ignores_unknown_query_parameters() -> None:
    response = search({"q": "30", "document_id": "nda-acme", "page": 5})
    assert response.status_code == 200
    assert response.json()["total_matches"] == 1


def test_search_sees_text_saved_by_patch() -> None:
    body = change_body("Receiving Party", "Recipient", occurrence=2)
    assert patch_with_etag("/documents/nda-acme", json=body).status_code == 200

    response = search({"q": "Recipient", "document_id": "nda-acme"})
    assert response.json()["total_matches"] == 1
    assert response.json()["results"][0]["start"] == 64
    old_name = search({"q": "Receiving Party", "document_id": "nda-acme"})
    assert old_name.json()["total_matches"] == 1


# GET /documents/search with ignore_case


def starts_and_matches(response: httpx.Response) -> list[tuple[int, str]]:
    found = []
    for result in response.json()["results"]:
        found.append((result["start"], result["match"]))
    return found


def test_search_is_still_case_sensitive_by_default() -> None:
    response = search({"q": "confidential information", "document_id": "nda-acme"})
    assert response.status_code == 200
    assert response.json()["total_matches"] == 0


def test_ignore_case_false_is_the_same_as_the_default() -> None:
    params = {
        "q": "confidential information",
        "document_id": "nda-acme",
        "ignore_case": "false",
    }
    assert search(params).json()["total_matches"] == 0


def test_ignore_case_returns_original_text_and_query_as_given() -> None:
    params = {
        "q": "confidential information",
        "document_id": "nda-acme",
        "limit": 1,
        "ignore_case": "true",
    }
    response = search(params)
    assert response.status_code == 200
    assert response.json() == {
        "query": "confidential information",
        "total_matches": 2,
        "results": [
            {
                "document_id": "nda-acme",
                "start": 34,
                "end": 58,
                "before": "The Receiving Party shall protect ",
                "match": "Confidential Information",
                "after": ". The Receiving Party shall return all C",
            }
        ],
    }


def test_ignore_case_matches_every_casing() -> None:
    store.save_text("case-example", "Fee FEE fee fEe")
    params = {"q": "FEE", "document_id": "case-example", "ignore_case": "true"}
    response = search(params)
    assert response.json()["total_matches"] == 4
    assert starts_and_matches(response) == [
        (0, "Fee"),
        (4, "FEE"),
        (8, "fee"),
        (12, "fEe"),
    ]


def test_ignore_case_keeps_the_non_overlapping_rule() -> None:
    store.save_text("overlap-example", "AaAa")
    params = {"q": "aa", "document_id": "overlap-example", "ignore_case": "true"}
    response = search(params)
    assert starts_and_matches(response) == [(0, "Aa"), (2, "Aa")]


def test_ignore_case_handles_non_ascii_letters_without_folding_accents() -> None:
    store.save_text("accent-doc", "Zoë signs")
    with_accent = {"q": "ZOË", "document_id": "accent-doc", "ignore_case": "true"}
    without_accent = {"q": "ZOE", "document_id": "accent-doc", "ignore_case": "true"}
    assert search(with_accent).json()["total_matches"] == 1
    assert search(without_accent).json()["total_matches"] == 0


def test_positions_stay_correct_after_a_dotted_capital_i() -> None:
    # "İ".lower() is two characters, so lowercasing this whole text moves
    # every later character one or more places to the right.
    store.save_text("dotted-i-example", "İİ Notice")
    params = {"q": "notice", "document_id": "dotted-i-example", "ignore_case": "true"}
    response = search(params)
    assert response.json()["results"] == [
        {
            "document_id": "dotted-i-example",
            "start": 3,
            "end": 9,
            "before": "İİ ",
            "match": "Notice",
            "after": "",
        }
    ]


def test_query_containing_a_dotted_capital_i() -> None:
    store.save_text("istanbul-example", "Visit İSTANBUL today")
    params = {"q": "İstanbul", "document_id": "istanbul-example", "ignore_case": "true"}
    response = search(params)
    assert response.json()["total_matches"] == 1
    result = response.json()["results"][0]
    assert (result["start"], result["end"]) == (6, 14)
    assert result["match"] == "İSTANBUL"
    assert result["after"] == " today"


def test_ignore_case_i_does_not_match_a_dotted_capital_i() -> None:
    # Our rule: a match covers whole characters of the original text. "İ"
    # lowercases to two characters, so it is compared as it is.
    store.save_text("dotted-i-example", "İi")
    lowercase_i = {"q": "i", "document_id": "dotted-i-example", "ignore_case": "true"}
    assert starts_and_matches(search(lowercase_i)) == [(1, "i")]
    capital_i = {"q": "I", "document_id": "dotted-i-example", "ignore_case": "true"}
    assert starts_and_matches(search(capital_i)) == [(1, "i")]


def test_ignore_case_greek_sigma_offsets_and_snippet() -> None:
    # Each "Σ" lowers to "σ" on its own, wherever it is in the word, and the
    # " İ" at the end does not change how the earlier text is matched.
    store.save_text("greek-example", "ΟΔΟΣ 5, ΤΕΛΟΣ İ")
    params = {"q": "οδοσ", "document_id": "greek-example", "ignore_case": "true"}
    assert search(params).json()["results"] == [
        {
            "document_id": "greek-example",
            "start": 0,
            "end": 4,
            "before": "",
            "match": "ΟΔΟΣ",
            "after": " 5, ΤΕΛΟΣ İ",
        }
    ]
    sigma = {"q": "Σ", "document_id": "greek-example", "ignore_case": "true"}
    assert starts_and_matches(search(sigma)) == [(3, "Σ"), (12, "Σ")]


def test_patch_stays_case_sensitive() -> None:
    change = {
        "operation": "replace",
        "target": {"text": "receiving party", "occurrence": 1},
        "replacement": "Recipient",
    }
    response = patch_with_etag("/documents/nda-acme", json={"changes": [change]})
    assert response.status_code == 409
    assert response.json() == {
        "error": "changes.0: Target text not found in document 'nda-acme'.",
        "code": 409,
    }


def test_invalid_ignore_case_returns_422() -> None:
    response = search({"q": "fee", "ignore_case": "sometimes"})
    assert response.status_code == 422
    assert response.json() == {
        "error": "ignore_case: Input should be a valid boolean, unable to interpret "
        "input",
        "code": 422,
    }


# POST /documents/{document_id}/suggestions
#
# The model is faked by replacing llm_client.request_model_reply, the only
# function that makes a network call. The replies below are hand-written
# examples, not recordings of a real model.

INSTRUCTION = {"instruction": "Give the recipient 60 days instead of 30"}

REPLY_60_DAYS = (
    '{"target": "within 30 days", "replacement": "within 60 days", '
    '"occurrence": null, "explanation": "Extends the return period to 60 days."}'
)


def fake_model_reply(monkeypatch: pytest.MonkeyPatch, reply_text: str) -> list[str]:
    """Make the model return reply_text. Returns a list that collects prompts."""
    prompts = []

    def fake_request_model_reply(prompt: str) -> str:
        prompts.append(prompt)
        return reply_text

    monkeypatch.setattr(llm_client, "request_model_reply", fake_request_model_reply)
    return prompts


def suggest_expecting_no_change(document_id: str, body: object) -> httpx.Response:
    """Ask for a suggestion and check that no stored document changed."""
    documents_before = store.snapshot()
    response = client.post(f"/documents/{document_id}/suggestions", json=body)
    assert store.snapshot() == documents_before
    return response


def test_suggestion_that_applies_can_be_sent_to_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts = fake_model_reply(monkeypatch, REPLY_60_DAYS)
    response = suggest_expecting_no_change("nda-acme", INSTRUCTION)

    assert response.status_code == 200
    assert response.json() == {
        "document_id": "nda-acme",
        "suggestion": {
            "operation": "replace",
            "target": {"text": "within 30 days", "occurrence": None},
            "replacement": "within 60 days",
        },
        "explanation": "Extends the return period to 60 days.",
        "applies": True,
        "match_count": 1,
        "reason": None,
    }
    assert len(prompts) == 1
    assert INSTRUCTION["instruction"] in prompts[0]
    assert NDA_TEXT in prompts[0]

    body = {"changes": [response.json()["suggestion"]]}
    patch = patch_with_etag("/documents/nda-acme", json=body)
    assert patch.status_code == 200
    assert get_text("nda-acme") == NDA_TEXT.replace("30 days", "60 days")


def test_suggestion_with_valid_occurrence_applies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reply = (
        '{"target": "Receiving Party", "replacement": "Recipient", '
        '"occurrence": 2, "explanation": "Renames the second party mention."}'
    )
    fake_model_reply(monkeypatch, reply)
    response = suggest_expecting_no_change("nda-acme", INSTRUCTION)
    assert response.status_code == 200
    assert response.json()["applies"] is True
    assert response.json()["match_count"] == 2
    assert response.json()["reason"] is None

    body = {"changes": [response.json()["suggestion"]]}
    patch = patch_with_etag("/documents/nda-acme", json=body)
    assert patch.status_code == 200
    assert patch.json()["changes"] == [{"start": 64, "occurrence": 2}]


@pytest.mark.parametrize(
    ("reply", "match_count", "reason"),
    [
        (
            '{"target": "within thirty days", "replacement": "within 60 days", '
            '"occurrence": null, "explanation": "e"}',
            0,
            "not_found",
        ),
        (
            '{"target": "Receiving Party", "replacement": "Recipient", '
            '"occurrence": null, "explanation": "e"}',
            2,
            "ambiguous",
        ),
        (
            '{"target": "Receiving Party", "replacement": "Recipient", '
            '"occurrence": 3, "explanation": "e"}',
            2,
            "occurrence_out_of_range",
        ),
    ],
)
def test_suggestion_that_does_not_apply_returns_200_with_reason(
    monkeypatch: pytest.MonkeyPatch, reply: str, match_count: int, reason: str
) -> None:
    fake_model_reply(monkeypatch, reply)
    response = suggest_expecting_no_change("nda-acme", INSTRUCTION)
    assert response.status_code == 200
    assert response.json()["applies"] is False
    assert response.json()["match_count"] == match_count
    assert response.json()["reason"] == reason


@pytest.mark.parametrize(
    "reply",
    [
        "Sure, change 30 to 60.",
        '{"target": "", "replacement": "x", "explanation": "e"}',
        '{"target": "\\ud800", "replacement": "x", "explanation": "e"}',
    ],
)
def test_malformed_model_reply_returns_502(
    monkeypatch: pytest.MonkeyPatch, reply: str
) -> None:
    fake_model_reply(monkeypatch, reply)
    response = suggest_expecting_no_change("nda-acme", INSTRUCTION)
    assert response.status_code == 502
    assert response.json() == {
        "error": "The language model returned a malformed reply.",
        "code": 502,
    }


def test_model_network_error_or_timeout_returns_502(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(prompt: str) -> str:
        raise llm_client.LLMUnavailableError("timed out")

    monkeypatch.setattr(llm_client, "request_model_reply", fail)
    response = suggest_expecting_no_change("nda-acme", INSTRUCTION)
    assert response.status_code == 502
    assert response.json() == {
        "error": "The language model could not be reached or returned an error.",
        "code": 502,
    }


@pytest.mark.parametrize("content", [None, "not a list of blocks"])
def test_model_response_with_bad_content_returns_502(
    monkeypatch: pytest.MonkeyPatch, content: object
) -> None:
    # The real request_model_reply runs; only the Messages API reply is faked.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    model_request = httpx.Request("POST", llm_client.MESSAGES_URL)
    model_response = httpx.Response(
        200,
        json={"content": content, "stop_reason": "end_turn"},
        request=model_request,
    )

    def fake_post(
        url: str, json: dict, headers: dict, timeout: float
    ) -> httpx.Response:
        return model_response

    monkeypatch.setattr(httpx, "post", fake_post)
    response = suggest_expecting_no_change("nda-acme", INSTRUCTION)
    assert response.status_code == 502
    assert response.json() == {
        "error": "The language model could not be reached or returned an error.",
        "code": 502,
    }


def test_suggestion_without_api_key_returns_503() -> None:
    # The real request_model_reply runs; the autouse fixture removed the key.
    response = suggest_expecting_no_change("nda-acme", INSTRUCTION)
    assert response.status_code == 503
    assert response.json() == {
        "error": "Suggestions are disabled: set ANTHROPIC_API_KEY on the server.",
        "code": 503,
    }


def test_suggestion_for_unknown_document_returns_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts = fake_model_reply(monkeypatch, REPLY_60_DAYS)
    response = suggest_expecting_no_change("nda-missing", INSTRUCTION)
    assert response.status_code == 404
    assert response.json() == {
        "error": "Document 'nda-missing' not found.",
        "code": 404,
    }
    assert prompts == []


def test_suggestion_for_too_large_document_returns_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts = fake_model_reply(monkeypatch, REPLY_60_DAYS)
    store.save_text("long-example", "a" * 50_001)
    response = suggest_expecting_no_change("long-example", INSTRUCTION)
    assert response.status_code == 409
    assert response.json() == {
        "error": (
            "Document 'long-example' has 50,001 characters; "
            "suggestions support at most 50,000."
        ),
        "code": 409,
    }
    assert prompts == []


def test_suggestion_at_the_size_cap_is_sent_to_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts = fake_model_reply(monkeypatch, REPLY_60_DAYS)
    store.save_text("cap-example", "a" * 50_000)
    response = suggest_expecting_no_change("cap-example", INSTRUCTION)
    assert response.status_code == 200
    assert response.json()["reason"] == "not_found"
    assert len(prompts) == 1


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({}, "instruction: Field required"),
        ({"instruction": ""}, "instruction: String should have at least 1 character"),
        (
            {"instruction": "x" * 2001},
            "instruction: String should have at most 2000 characters",
        ),
        ({"instruction": 5}, "instruction: Input should be a valid string"),
        (
            {"instruction": "Shorten it", "model": "gpt"},
            "model: Extra inputs are not permitted",
        ),
    ],
)
def test_suggestion_invalid_body_returns_422(body: object, message: str) -> None:
    response = suggest_expecting_no_change("nda-acme", body)
    assert response.status_code == 422
    assert response.json() == {"error": message, "code": 422}


def test_suggestion_instruction_with_lone_surrogate_returns_422() -> None:
    documents_before = store.snapshot()
    response = client.post(
        "/documents/nda-acme/suggestions",
        content=b'{"instruction": "\\ud800"}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["error"].startswith("instruction: ")
    assert store.snapshot() == documents_before
