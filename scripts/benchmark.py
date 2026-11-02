"""Time replacement and search on a generated document of at least 10 MiB.

Run from the repository root after setup:
    .venv/bin/python scripts/benchmark.py

Prints median wall-clock times over RUNS runs, plus peak memory allocated by
Python (tracemalloc) for a few operations. Numbers vary by machine and run;
they describe one machine, not a guarantee.
"""

import platform
import statistics
import sys
import time
import tracemalloc
from collections.abc import Callable

from fastapi.testclient import TestClient

from app.api import SAMPLE_DOCUMENTS_DIR, app
from app.api import store as app_store
from app.service import apply_changes_to_document, search_documents
from app.storage import DocumentStore
from app.text_ops import TextChange, find_match_starts

RUNS = 5
MIN_CHARACTERS = 10 * 1024 * 1024
DOCUMENT_ID = "large-contract"
START_LINE = "Agreement start.\n"
END_LINE = "Agreement end.\n"
PARAGRAPH = (
    "The Supplier shall deliver the Goods within thirty (30) days of each order. "
    "Payment is due on receipt — see § 4 of the Buyer’s terms.\n"
)


def build_large_document() -> str:
    paragraph_count = MIN_CHARACTERS // len(PARAGRAPH) + 1
    return START_LINE + PARAGRAPH * paragraph_count + END_LINE


def median_milliseconds(operation: Callable[[], object]) -> float:
    timings = []
    for _ in range(RUNS):
        started = time.perf_counter()
        operation()
        timings.append(time.perf_counter() - started)
    return statistics.median(timings) * 1000


def peak_megabytes(operation: Callable[[], object]) -> float:
    tracemalloc.start()
    operation()
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return peak_bytes / 1_000_000


def print_row(label: str, value: float, unit: str) -> None:
    print(f"  {label:<52} {value:9.2f} {unit}")


def main() -> None:
    text = build_large_document()
    store = DocumentStore()
    store.save_text(DOCUMENT_ID, text)
    client = TestClient(app)

    def find_many() -> object:
        return find_match_starts(text, "Supplier")

    def find_every_space() -> object:
        return find_match_starts(text, " ")

    def find_none() -> object:
        return find_match_starts(text, "Indemnification")

    def replace_near_end() -> object:
        # Restoring the original text is a dict assignment; it costs no copy.
        store.save_text(DOCUMENT_ID, text)
        change = TextChange(target="Agreement end.", replacement="Agreement ended.")
        return apply_changes_to_document(store, DOCUMENT_ID, [change])

    supplier_count = text.count("Supplier")

    def replace_last_of_many() -> object:
        store.save_text(DOCUMENT_ID, text)
        change = TextChange(
            target="Supplier", replacement="Vendor", occurrence=supplier_count
        )
        return apply_changes_to_document(store, DOCUMENT_ID, [change])

    def apply_100_changes() -> object:
        # The largest request allowed. Each change is near the end of the
        # document, so each one scans the whole text and builds a new copy.
        store.save_text(DOCUMENT_ID, text)
        changes = [TextChange(target="Agreement end.", replacement="Agreement end 1.")]
        for number in range(1, 100):
            changes.append(
                TextChange(
                    target=f"Agreement end {number}.",
                    replacement=f"Agreement end {number + 1}.",
                )
            )
        return apply_changes_to_document(store, DOCUMENT_ID, changes)

    def search_many() -> object:
        return search_documents(store, "Supplier", None, 10)

    def search_none() -> object:
        return search_documents(store, "Indemnification", None, 10)

    def search_ignore_case() -> object:
        return search_documents(store, "supplier", None, 10, ignore_case=True)

    # One "İ" in the text: the fast whole-string lower() is only used when the
    # text has no "İ" or "Σ", so this takes the slow, character-by-character path.
    dotted_i_store = DocumentStore()
    dotted_i_store.save_text(DOCUMENT_ID, "İ" + text)

    def search_ignore_case_with_dotted_i() -> object:
        return search_documents(dotted_i_store, "supplier", None, 10, ignore_case=True)

    def http_patch() -> object:
        app_store.save_text(DOCUMENT_ID, text)
        change = {
            "operation": "replace",
            "target": {"text": "Agreement end."},
            "replacement": "Agreement ended.",
        }
        # Read from the store, not a GET, so the timing is the PATCH alone.
        headers = {"If-Match": f'"{app_store.get_version(DOCUMENT_ID)}"'}
        response = client.patch(
            f"/documents/{DOCUMENT_ID}", json={"changes": [change]}, headers=headers
        )
        return response.json()

    def http_search() -> object:
        params = {"q": "Supplier", "document_id": DOCUMENT_ID}
        return client.get("/documents/search", params=params).json()

    print("Environment")
    print(f"  Python   {sys.version.split()[0]}")
    print(f"  Platform {platform.platform()} ({platform.machine()})")
    print()
    print("Document")
    print(f"  characters      {len(text):,}")
    print(f"  UTF-8 bytes     {len(text.encode('utf-8')):,}")
    print(f"  'Supplier' hits {text.count('Supplier'):,}")
    print(f"  ' ' hits        {text.count(' '):,}")
    print(f"  in-memory size  {sys.getsizeof(text):,} bytes (CPython str)")
    print()
    timed_operations = [
        ("find_match_starts 'Supplier' (many matches)", find_many),
        ("find_match_starts ' ' (every space)", find_every_space),
        ("find_match_starts 'Indemnification' (no match)", find_none),
        ("1 change, unique target near end", replace_near_end),
        ("1 change, last 'Supplier' (occurrence)", replace_last_of_many),
        ("100 changes in one request, each near the end", apply_100_changes),
        ("search_documents 'Supplier', limit 10", search_many),
        ("search_documents 'Indemnification' (no match)", search_none),
        ("search_documents 'supplier', ignore_case", search_ignore_case),
        (
            "search_documents 'supplier', ignore_case, one 'İ'",
            search_ignore_case_with_dotted_i,
        ),
        ("HTTP PATCH via TestClient (returns full text)", http_patch),
        ("HTTP GET /documents/search 'Supplier' via TestClient", http_search),
    ]
    memory_operations = [
        ("find_match_starts 'Supplier'", find_many),
        ("find_match_starts ' '", find_every_space),
        ("1 change, unique target near end", replace_near_end),
        ("100 changes in one request, each near the end", apply_100_changes),
        ("search_documents 'Supplier', limit 10", search_many),
        ("search_documents 'supplier', ignore_case", search_ignore_case),
    ]

    app_store.save_text(DOCUMENT_ID, text)
    print(f"Median time over {RUNS} runs")
    for label, operation in timed_operations:
        print_row(label, median_milliseconds(operation), "ms")
    app_store.load_directory(SAMPLE_DOCUMENTS_DIR)

    print()
    print("Peak memory allocated by Python during one call (tracemalloc)")
    for label, operation in memory_operations:
        print_row(label, peak_megabytes(operation), "MB")


if __name__ == "__main__":
    main()
