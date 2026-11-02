"""Workflows that combine storage and text operations."""

from app import llm_client
from app.storage import DocumentStore
from app.text_ops import (
    AmbiguousTargetError,
    OccurrenceOutOfRangeError,
    TargetNotFoundError,
    TextChange,
    apply_changes,
    build_snippet,
    choose_match_start,
    find_match_starts,
    find_match_starts_ignore_case,
)

# About 20,000 tokens (roughly 2.5 characters per token): fits easily in the
# model's context and keeps each call's cost and latency bounded. Typical
# contracts are shorter.
MAX_SUGGESTION_DOCUMENT_CHARS = 50_000


class DocumentNotFoundError(Exception):
    """No document has the requested ID."""

    def __init__(self, document_id: str) -> None:
        super().__init__(f"Document '{document_id}' not found.")


class VersionMismatchError(Exception):
    """The client's version is not the document's current version."""

    def __init__(self, document_id: str) -> None:
        super().__init__(
            f"Document '{document_id}' has changed since it was read; "
            "get it again and retry."
        )


class DocumentTooLargeError(Exception):
    """The document is too long to send to the model."""

    def __init__(self, document_id: str, length: int) -> None:
        super().__init__(
            f"Document '{document_id}' has {length:,} characters; suggestions "
            f"support at most {MAX_SUGGESTION_DOCUMENT_CHARS:,}."
        )


def list_documents(store: DocumentStore) -> dict:
    """Return every document ID with its length, sorted by ID. No text."""
    with store.lock:
        texts = store.snapshot()

    documents = []
    for document_id in sorted(texts.keys()):
        length = len(texts[document_id])
        documents.append({"document_id": document_id, "length": length})
    return {"documents": documents}


def read_document(store: DocumentStore, document_id: str) -> dict:
    """Return the text and its version; the route sends the version as an ETag."""
    with store.lock:
        text = store.get_text(document_id)
        version = store.get_version(document_id)
    if text is None:
        raise DocumentNotFoundError(document_id)
    return {"document_id": document_id, "text": text, "version": version}


def apply_changes_to_document(
    store: DocumentStore,
    document_id: str,
    changes: list[TextChange],
    expected_version: int | None = None,
) -> dict:
    """Apply every change in order and save the result once.

    All or nothing: any error is raised before save_text runs, so a rejected
    request never changes the stored text, even if its earlier changes fitted.
    expected_version=None skips the version check.
    """
    # Holding the lock from read to save stops two concurrent edits from both
    # starting with the same old text, which would lose one of them. The
    # version check is inside the same lock, so no edit can land between the
    # check and the save.
    with store.lock:
        text = store.get_text(document_id)
        if text is None:
            raise DocumentNotFoundError(document_id)
        current_version = store.get_version(document_id)
        if expected_version is not None and expected_version != current_version:
            raise VersionMismatchError(document_id)
        result = apply_changes(text, changes)
        store.save_text(document_id, result.text)
        new_version = store.get_version(document_id)

    applied_changes = []
    for applied in result.changes:
        applied_changes.append(
            {"start": applied.start, "occurrence": applied.occurrence}
        )
    return {
        "document_id": document_id,
        "changes": applied_changes,
        "text": result.text,
        "version": new_version,
    }


def search_documents(
    store: DocumentStore,
    query: str,
    document_ids: list[str] | None,
    limit: int,
    offset: int = 0,
    ignore_case: bool = False,
) -> dict:
    """Find query in the selected documents and return up to `limit` snippets.

    document_ids=None searches every document. Results are ordered by document
    ID, then by position. The first `offset` matches in that order are skipped.
    total_matches counts every match, not just the returned ones.
    ignore_case=True matches letters regardless of case; positions and snippets
    still come from the original text.
    """
    # Copy under the lock, then scan without it, so a long search does not
    # block edits and sees every document at the same moment.
    with store.lock:
        texts = store.snapshot()

    if document_ids is None:
        selected_ids = sorted(texts.keys())
    else:
        for document_id in document_ids:
            if document_id not in texts:
                raise DocumentNotFoundError(document_id)
        selected_ids = sorted(set(document_ids))

    results = []
    total_matches = 0
    # Offset counts matches across all selected documents, so the skip count
    # must carry over from one document to the next.
    skipped = 0
    for document_id in selected_ids:
        text = texts[document_id]
        if ignore_case:
            match_starts = find_match_starts_ignore_case(text, query)
        else:
            match_starts = find_match_starts(text, query)
        total_matches += len(match_starts)

        for start in match_starts:
            if len(results) == limit:
                break
            if skipped < offset:
                skipped += 1
                continue
            snippet = build_snippet(text, start, start + len(query))
            results.append(
                {
                    "document_id": document_id,
                    "start": snippet.start,
                    "end": snippet.end,
                    "before": snippet.before,
                    "match": snippet.match,
                    "after": snippet.after,
                }
            )

    return {"query": query, "total_matches": total_matches, "results": results}


def suggest_edit(store: DocumentStore, document_id: str, instruction: str) -> dict:
    """Ask the model for one edit and check it against the current text.

    Never saves anything. A suggestion that applies can be sent to PATCH as
    {"changes": [suggestion]}, which checks it again against the text then.
    """
    with store.lock:
        text = store.get_text(document_id)
    if text is None:
        raise DocumentNotFoundError(document_id)
    if len(text) > MAX_SUGGESTION_DOCUMENT_CHARS:
        raise DocumentTooLargeError(document_id, len(text))

    # Called without the lock, so a slow model reply does not block edits.
    suggestion = llm_client.suggest_edit(text, instruction)

    match_starts = find_match_starts(text, suggestion.target)
    applies = False
    reason = None
    try:
        choose_match_start(match_starts, suggestion.occurrence)
        applies = True
    except TargetNotFoundError:
        reason = "not_found"
    except AmbiguousTargetError:
        reason = "ambiguous"
    except OccurrenceOutOfRangeError:
        reason = "occurrence_out_of_range"

    return {
        "document_id": document_id,
        "suggestion": {
            "operation": "replace",
            "target": {"text": suggestion.target, "occurrence": suggestion.occurrence},
            "replacement": suggestion.replacement,
        },
        "explanation": suggestion.explanation,
        "applies": applies,
        "match_count": len(match_starts),
        "reason": reason,
    }
