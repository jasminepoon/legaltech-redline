"""Correctness on a generated document of at least 10 MiB. No timing asserts.

The document is START_LINE, then PARAGRAPH repeated PARAGRAPH_COUNT times, then
END_LINE: 78,252 paragraphs, 10,485,800 characters, 10,877,060 UTF-8 bytes.
Expected positions and counts are computed from that structure, not by calling
the code under test.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.api import SAMPLE_DOCUMENTS_DIR, app, store
from app.service import search_documents
from app.storage import DocumentStore
from app.text_ops import (
    AmbiguousTargetError,
    OccurrenceOutOfRangeError,
    replace_one_occurrence,
)

MIN_CHARACTERS = 10 * 1024 * 1024
START_LINE = "Agreement start.\n"
END_LINE = "Agreement end.\n"
PARAGRAPH = (
    "The Supplier shall deliver the Goods within thirty (30) days of each order. "
    "Payment is due on receipt — see § 4 of the Buyer’s terms.\n"
)
PARAGRAPH_COUNT = MIN_CHARACTERS // len(PARAGRAPH) + 1
SUPPLIER_OFFSET = PARAGRAPH.index("Supplier")


def build_large_document() -> str:
    return START_LINE + PARAGRAPH * PARAGRAPH_COUNT + END_LINE


def supplier_start(occurrence: int) -> int:
    """Where the Nth "Supplier" begins: one per paragraph, at the same offset."""
    return len(START_LINE) + (occurrence - 1) * len(PARAGRAPH) + SUPPLIER_OFFSET


@pytest.fixture(scope="module")
def large_text() -> str:
    return build_large_document()


def test_large_document_is_at_least_10_mib(large_text: str) -> None:
    assert len(large_text) >= MIN_CHARACTERS
    # The paragraph contains "—", "§", and "’", so UTF-8 bytes exceed characters.
    assert len(large_text.encode("utf-8")) > len(large_text)


def test_replace_unique_target_near_end(large_text: str) -> None:
    end_start = len(large_text) - len(END_LINE)
    result = replace_one_occurrence(
        large_text, "Agreement end.", "Agreement ended.", None
    )
    assert result.start == end_start
    assert result.text == large_text[:end_start] + "Agreement ended.\n"


def test_replace_unique_target_near_start(large_text: str) -> None:
    result = replace_one_occurrence(large_text, "Agreement start.", "Begin.", None)
    assert result.start == 0
    assert result.text == "Begin." + large_text[len("Agreement start.") :]


def test_replace_middle_occurrence_among_many(large_text: str) -> None:
    occurrence = PARAGRAPH_COUNT // 2
    start = supplier_start(occurrence)
    result = replace_one_occurrence(large_text, "Supplier", "Vendor", occurrence)
    assert result.start == start
    assert result.text == large_text[:start] + "Vendor" + large_text[start + 8 :]


def test_replace_last_occurrence_among_many(large_text: str) -> None:
    result = replace_one_occurrence(large_text, "Supplier", "Vendor", PARAGRAPH_COUNT)
    assert result.start == supplier_start(PARAGRAPH_COUNT)


def test_repeated_target_without_occurrence_reports_full_count(
    large_text: str,
) -> None:
    with pytest.raises(AmbiguousTargetError) as error_info:
        replace_one_occurrence(large_text, "Supplier", "Vendor", None)
    assert error_info.value.match_count == PARAGRAPH_COUNT


def test_occurrence_one_past_the_last_is_out_of_range(large_text: str) -> None:
    with pytest.raises(OccurrenceOutOfRangeError):
        replace_one_occurrence(large_text, "Supplier", "Vendor", PARAGRAPH_COUNT + 1)


def test_search_counts_every_match_and_returns_first_results(
    large_text: str,
) -> None:
    large_store = DocumentStore()
    large_store.save_text("large-contract", large_text)

    response = search_documents(large_store, "Supplier", None, 3)

    assert response["total_matches"] == PARAGRAPH_COUNT
    starts = []
    for result in response["results"]:
        starts.append(result["start"])
    assert starts == [supplier_start(1), supplier_start(2), supplier_start(3)]


def test_search_offset_returns_the_last_page(large_text: str) -> None:
    large_store = DocumentStore()
    large_store.save_text("large-contract", large_text)

    # Skip all but the last two matches: a page far into the document.
    offset = PARAGRAPH_COUNT - 2
    response = search_documents(large_store, "Supplier", None, 10, offset)

    assert response["total_matches"] == PARAGRAPH_COUNT
    starts = []
    for result in response["results"]:
        starts.append(result["start"])
    assert starts == [
        supplier_start(PARAGRAPH_COUNT - 1),
        supplier_start(PARAGRAPH_COUNT),
    ]


def test_search_non_ascii_query_count(large_text: str) -> None:
    large_store = DocumentStore()
    large_store.save_text("large-contract", large_text)
    response = search_documents(large_store, "§ 4", None, 1)
    assert response["total_matches"] == PARAGRAPH_COUNT


def test_search_without_matches(large_text: str) -> None:
    large_store = DocumentStore()
    large_store.save_text("large-contract", large_text)
    response = search_documents(large_store, "Indemnification", None, 10)
    assert response["total_matches"] == 0
    assert response["results"] == []


def test_search_snippet_at_document_end(large_text: str) -> None:
    large_store = DocumentStore()
    large_store.save_text("large-contract", large_text)

    response = search_documents(large_store, "Agreement end.", None, 10)

    assert response["total_matches"] == 1
    result = response["results"][0]
    assert result["start"] == len(large_text) - len(END_LINE)
    assert result["before"] == PARAGRAPH[-40:]
    assert result["after"] == "\n"


@pytest.fixture
def client_with_large_document(large_text: str) -> Iterator[TestClient]:
    store.save_text("large-contract", large_text)
    yield TestClient(app)
    # Restore the sample documents so later tests see the usual store.
    store.load_directory(SAMPLE_DOCUMENTS_DIR)


def test_api_patch_and_search_large_document(
    client_with_large_document: TestClient, large_text: str
) -> None:
    client = client_with_large_document
    change = {
        "operation": "replace",
        "target": {"text": "Agreement end."},
        "replacement": "Agreement ended.",
    }

    etag = client.get("/documents/large-contract").headers["etag"]
    patch_response = client.patch(
        "/documents/large-contract",
        json={"changes": [change]},
        headers={"If-Match": etag},
    )

    assert patch_response.status_code == 200
    applied = patch_response.json()["changes"]
    assert applied == [{"start": len(large_text) - len(END_LINE), "occurrence": 1}]
    assert len(patch_response.json()["text"]) == len(large_text) + 2

    params = {"q": "Supplier", "document_id": "large-contract", "limit": 2}
    search_response = client.get("/documents/search", params=params)
    assert search_response.status_code == 200
    assert search_response.json()["total_matches"] == PARAGRAPH_COUNT
    assert len(search_response.json()["results"]) == 2
