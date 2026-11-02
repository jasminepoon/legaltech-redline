"""HTTP layer: creates the app, defines routes, and turns errors into responses.

Run locally with: uvicorn app.api:app --port 8000 --no-access-log
"""

from pathlib import Path

from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.llm_client import (
    API_KEY_ENV_VAR,
    LLMNotConfiguredError,
    LLMUnavailableError,
    MalformedSuggestionError,
)
from app.schemas import EditRequest, SuggestionRequest
from app.service import (
    DocumentNotFoundError,
    DocumentTooLargeError,
    VersionMismatchError,
    apply_changes_to_document,
    list_documents,
    read_document,
    search_documents,
    suggest_edit,
)
from app.storage import DocumentStore
from app.text_ops import (
    AmbiguousTargetError,
    ChangeFailedError,
    OccurrenceOutOfRangeError,
    RangeOutOfBoundsError,
    RangeTextMismatchError,
    TargetNotFoundError,
    TextChange,
    is_valid_unicode,
)

SAMPLE_DOCUMENTS_DIR = Path(__file__).resolve().parent.parent / "sample_documents"

store = DocumentStore()
store.load_directory(SAMPLE_DOCUMENTS_DIR)

app = FastAPI(title="Document editing and search")


def error_response(
    status_code: int, message: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    body = {"error": message, "code": status_code}
    return JSONResponse(status_code=status_code, content=body, headers=headers)


def describe_location(location: tuple) -> str:
    """Turn pydantic's error location into a dotted field path.

    ("body", "changes", 0, "target", "text") becomes "changes.0.target.text",
    and ("query", "limit") becomes "limit". A location with one part, such as
    ("body",) when the whole body is wrong, is kept as it is.
    """
    if len(location) == 1:
        return str(location[0])

    parts = []
    # The first part only says where the input came from (body or query).
    for part in location[1:]:
        parts.append(str(part))
    return ".".join(parts)


MISPLACED_RANGE_MESSAGE = (
    "Put the position in target.range, the text expected there in target.text, "
    "and the new text in replacement."
)


def change_index(location: tuple) -> int | None:
    """The N of an error inside changes.N of the body, or None."""
    if len(location) < 3:
        return None
    if location[0] != "body" or location[1] != "changes":
        return None
    if not isinstance(location[2], int):
        return None
    return location[2]


def find_misplaced_range_changes(details: list[dict]) -> list[int]:
    """Indexes of changes with "range" beside "target" instead of inside it.

    The assignment's position-only example has that shape. pydantic reports
    it as several errors (missing target, extra range, ...), so these changes
    get one plain message instead.
    """
    indexes = []
    for detail in details:
        location = detail["loc"]
        index = change_index(location)
        is_extra_field = detail["type"] == "extra_forbidden"
        if is_extra_field and index is not None and location[3:] == ("range",):
            indexes.append(index)
    return indexes


def describe_validation_error(error: RequestValidationError) -> str:
    """Join pydantic's errors into one message, e.g. "target: Field required"."""
    details = error.errors()
    misplaced_range_changes = find_misplaced_range_changes(details)
    explained_changes = []
    messages = []
    for detail in details:
        if detail["type"] == "json_invalid":
            messages.append("Request body is not valid JSON.")
            continue

        index = change_index(detail["loc"])
        if index is not None and index in misplaced_range_changes:
            # One plain message replaces all of this change's errors.
            if index not in explained_changes:
                messages.append(f"changes.{index}: {MISPLACED_RANGE_MESSAGE}")
                explained_changes.append(index)
            continue

        messages.append(describe_location(detail["loc"]) + ": " + detail["msg"])
    return "; ".join(messages)


def etag_for_version(version: int) -> str:
    # HTTP puts ETag values in double quotes, for example "3".
    return f'"{version}"'


def version_from_etag(etag: str) -> int:
    """Turn an ETag such as "3" back into 3.

    Anything else gives 0. Versions start at 1, so 0 never matches and the
    edit is rejected as outdated.
    """
    digits = etag.strip('"')
    # The length check keeps int() below its 4,300-digit limit, past which it
    # raises ValueError; no real version is anywhere near 20 digits.
    if not digits.isdecimal() or len(digits) > 20:
        return 0
    return int(digits)


# Must be registered before GET /documents/{document_id}: routes are matched in
# order, and that route would otherwise read "search" as a document ID.
@app.get("/documents/search")
def search(
    q: str = Query(min_length=1),
    # The URL repeats ?document_id=a&document_id=b; Python gets a list.
    document_ids: list[str] | None = Query(default=None, alias="document_id"),
    limit: int = Query(default=10, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    ignore_case: bool = Query(default=False),
) -> JSONResponse:
    try:
        result = search_documents(store, q, document_ids, limit, offset, ignore_case)
    except DocumentNotFoundError as error:
        return error_response(404, str(error))
    return JSONResponse(content=result)


# "/documents" has no ID segment, so "/documents/{document_id}" never matches it.
@app.get("/documents")
def get_document_list() -> JSONResponse:
    return JSONResponse(content=list_documents(store))


@app.get("/documents/{document_id}")
def get_document(document_id: str) -> JSONResponse:
    try:
        document = read_document(store, document_id)
    except DocumentNotFoundError as error:
        return error_response(404, str(error))
    # The version goes in the ETag header, not the body.
    version = document.pop("version")
    headers = {"ETag": etag_for_version(version)}
    return JSONResponse(content=document, headers=headers)


def find_target_problem(request: EditRequest) -> str | None:
    """Return a 422 message for the first target the models cannot check alone.

    These rules involve several fields at once, and need no document text.
    """
    for index, change in enumerate(request.changes):
        target = change.target
        # Empty text is only an insertion at a zero-width range. Found by
        # text, it would match between every two characters.
        if target.text == "" and target.range is None:
            return (
                f"changes.{index}.target.text: String should have at least 1 character"
            )
        if target.range is None:
            continue
        if target.occurrence is not None:
            return f"changes.{index}.target: Send occurrence or range, not both."
        if target.range.start > target.range.end:
            return f"changes.{index}.target.range: start must not be after end."
        # Such a range can never hold the expected text, whatever the document.
        range_length = target.range.end - target.range.start
        if range_length != len(target.text):
            return (
                f"changes.{index}.target.range: The range covers {range_length} "
                f"characters, but target text has {len(target.text)}."
            )
    return None


def find_invalid_unicode_field(request: EditRequest) -> str | None:
    """Return the path of the first text field with a lone surrogate, or None."""
    for index, change in enumerate(request.changes):
        if not is_valid_unicode(change.target.text):
            return f"changes.{index}.target.text"
        if not is_valid_unicode(change.replacement):
            return f"changes.{index}.replacement"
    return None


def to_text_changes(request: EditRequest) -> list[TextChange]:
    """Copy the request's changes into the plain dataclass text_ops uses."""
    text_changes = []
    for change in request.changes:
        range_start = None
        range_end = None
        if change.target.range is not None:
            range_start = change.target.range.start
            range_end = change.target.range.end
        text_changes.append(
            TextChange(
                target=change.target.text,
                replacement=change.replacement,
                occurrence=change.target.occurrence,
                range_start=range_start,
                range_end=range_end,
            )
        )
    return text_changes


def describe_failed_change(error: ChangeFailedError, document_id: str) -> str:
    """The 409 message, starting with the failing change's path, e.g. "changes.1: "."""
    reason = error.reason
    if isinstance(reason, TargetNotFoundError):
        message = f"Target text not found in document '{document_id}'."
    elif isinstance(reason, AmbiguousTargetError):
        count = reason.match_count
        message = (
            f"Target text appears {count} times in document '{document_id}'; "
            f"specify occurrence 1 to {count}."
        )
    elif isinstance(reason, OccurrenceOutOfRangeError):
        message = (
            f"Occurrence {reason.occurrence} requested, but target text appears "
            f"{reason.match_count} times in document '{document_id}'."
        )
    elif isinstance(reason, RangeOutOfBoundsError):
        message = (
            f"Range ends at {reason.end}, but document '{document_id}' has "
            f"{reason.text_length} characters at this change."
        )
    elif isinstance(reason, RangeTextMismatchError):
        message = (
            f"Text at {reason.start} to {reason.end} in document '{document_id}' "
            f"is not the target text; the position may be stale."
        )
    else:
        raise ValueError(f"No message for {type(reason).__name__}")
    return f"changes.{error.index}: {message}"


@app.patch("/documents/{document_id}")
def patch_document(
    document_id: str,
    request: EditRequest,
    if_match: str | None = Header(default=None),
) -> JSONResponse:
    target_problem = find_target_problem(request)
    if target_problem is not None:
        return error_response(422, target_problem)
    invalid_field = find_invalid_unicode_field(request)
    if invalid_field is not None:
        return error_response(422, f"{invalid_field}: Text must be valid Unicode.")

    # No If-Match header, or "*", means "edit whatever is current", as before.
    expected_version = None
    if if_match is not None and if_match != "*":
        expected_version = version_from_etag(if_match)

    try:
        result = apply_changes_to_document(
            store, document_id, to_text_changes(request), expected_version
        )
    except DocumentNotFoundError as error:
        return error_response(404, str(error))
    except VersionMismatchError as error:
        return error_response(412, str(error))
    except ChangeFailedError as error:
        return error_response(409, describe_failed_change(error, document_id))
    version = result.pop("version")
    headers = {"ETag": etag_for_version(version)}
    return JSONResponse(content=result, headers=headers)


@app.post("/documents/{document_id}/suggestions")
def suggest(document_id: str, request: SuggestionRequest) -> JSONResponse:
    if not is_valid_unicode(request.instruction):
        return error_response(422, "instruction: Text must be valid Unicode.")

    # Messages never include document text or the model's reply.
    try:
        result = suggest_edit(store, document_id, request.instruction)
    except DocumentNotFoundError as error:
        return error_response(404, str(error))
    except DocumentTooLargeError as error:
        return error_response(409, str(error))
    except LLMNotConfiguredError:
        message = f"Suggestions are disabled: set {API_KEY_ENV_VAR} on the server."
        return error_response(503, message)
    except LLMUnavailableError:
        message = "The language model could not be reached or returned an error."
        return error_response(502, message)
    except MalformedSuggestionError:
        return error_response(502, "The language model returned a malformed reply.")
    return JSONResponse(content=result)


@app.exception_handler(RequestValidationError)
def handle_validation_error(
    request: Request, error: RequestValidationError
) -> JSONResponse:
    return error_response(422, describe_validation_error(error))


@app.exception_handler(StarletteHTTPException)
def handle_http_error(request: Request, error: StarletteHTTPException) -> JSONResponse:
    # FastAPI raises these itself for unknown routes (404) and wrong methods (405).
    # A 405 carries an "Allow" header listing valid methods, so keep its headers.
    return error_response(error.status_code, str(error.detail), error.headers)


@app.exception_handler(Exception)
def handle_unexpected_error(request: Request, error: Exception) -> JSONResponse:
    # Generic message only: exception text could include document content.
    return error_response(500, "Internal server error.")
