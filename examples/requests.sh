#!/usr/bin/env bash
# Sample requests for listing, search, suggestions, and replacement.
#
# Start the server first, from the repository root:
#   .venv/bin/uvicorn app.api:app --port 8000 --no-access-log
# Then run:
#   bash examples/requests.sh
#
# The search examples run first, against the unedited documents. The
# replacement examples then edit documents, so later requests depend on earlier
# ones. Edits live in memory only: restart the server to reset every document.
#
# Search requests use curl -G with --data-urlencode, which URL-encodes each
# value (spaces, §, quotes) and appends it to the URL as a query parameter.

set -u

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"

show() {
    echo
    echo "### $1"
}

send() {
    curl -sS -w '\nHTTP %{http_code}\n' "$@"
}

# The document's current ETag, read with a GET just before an edit, as a client
# would. Every PATCH sends it as If-Match, except the two version-check examples,
# which send fixed values on purpose. Nothing is printed for this GET.
etag_of() {
    curl -sS -o /dev/null -D - "$BASE_URL/documents/$1" \
        | tr -d '\r' | awk 'tolower($1) == "etag:" { print $2 }'
}

echo "======== Documents ========"

show "List every document: ID and length in characters, no text (200)"
send "$BASE_URL/documents"

echo
echo "======== Search ========"

show "Search one document, first result only (200; the example in README.md)"
send -G "$BASE_URL/documents/search" \
    --data-urlencode "q=Confidential Information" \
    --data-urlencode "document_id=nda-acme" \
    --data-urlencode "limit=1"

show "Search two documents given out of order, with a duplicate ID; limit below total (200)"
send -G "$BASE_URL/documents/search" \
    --data-urlencode "q=Confidential Information" \
    --data-urlencode "document_id=nda-acme" \
    --data-urlencode "document_id=msa-globex" \
    --data-urlencode "document_id=nda-acme" \
    --data-urlencode "limit=3"

show "Next page of that search: offset 3 (0 + 3 results returned) gives the last of 4 matches (200)"
send -G "$BASE_URL/documents/search" \
    --data-urlencode "q=Confidential Information" \
    --data-urlencode "document_id=nda-acme" \
    --data-urlencode "document_id=msa-globex" \
    --data-urlencode "limit=3" \
    --data-urlencode "offset=3"

show "ignore_case=true: a lowercase query finds both matches; match and positions come from the original text (200)"
send -G "$BASE_URL/documents/search" \
    --data-urlencode "q=confidential information" \
    --data-urlencode "document_id=nda-acme" \
    --data-urlencode "ignore_case=true"

show "Search all documents (no document_id), non-ASCII query (200)"
send -G "$BASE_URL/documents/search" \
    --data-urlencode "q=§ 3"

show "No matches: straight quotes do not match curly quotes (200)"
send -G "$BASE_URL/documents/search" \
    --data-urlencode 'q="Customer"' \
    --data-urlencode "document_id=msa-globex"

show "Unknown document_id fails the whole search (404)"
send -G "$BASE_URL/documents/search" \
    --data-urlencode "q=30" \
    --data-urlencode "document_id=nda-acme" \
    --data-urlencode "document_id=nda-missing"

show "Empty query (422)"
send -G "$BASE_URL/documents/search" \
    --data-urlencode "q="

show "Limit above 100 (422)"
send -G "$BASE_URL/documents/search" \
    --data-urlencode "q=term" \
    --data-urlencode "limit=500"

echo
echo "======== Suggestion (never saves) ========"

# 200 with a suggestion when the server has a working ANTHROPIC_API_KEY; 502 if
# the model call fails. The label states the result without a key.
show "Ask the model for an edit (503 when the server has no ANTHROPIC_API_KEY)"
send -X POST "$BASE_URL/documents/nda-acme/suggestions" \
    -H 'Content-Type: application/json' \
    -d '{"instruction": "Give the recipient 60 days instead of 30"}'

echo
echo "======== Replacement ========"

show "Read the original NDA, unchanged by the suggestion (200)"
send "$BASE_URL/documents/nda-acme"

show "Target appears twice and no occurrence given (409)"
send -X PATCH "$BASE_URL/documents/nda-acme" \
    -H 'Content-Type: application/json' \
    -H "If-Match: $(etag_of nda-acme)" \
    -d '{"changes": [{"operation": "replace", "target": {"text": "Receiving Party"}, "replacement": "Recipient"}]}'

show "Occurrence larger than the match count (409)"
send -X PATCH "$BASE_URL/documents/nda-acme" \
    -H 'Content-Type: application/json' \
    -H "If-Match: $(etag_of nda-acme)" \
    -d '{"changes": [{"operation": "replace", "target": {"text": "Receiving Party", "occurrence": 3}, "replacement": "Recipient"}]}'

show "Replace the second occurrence (200; the example in README.md)"
send -X PATCH "$BASE_URL/documents/nda-acme" \
    -H 'Content-Type: application/json' \
    -H "If-Match: $(etag_of nda-acme)" \
    -d '{"changes": [{"operation": "replace", "target": {"text": "Receiving Party", "occurrence": 2}, "replacement": "Recipient"}]}'

show "Read the NDA again with headers: the edit is stored, and the ETag header is now \"2\" (200)"
send -i "$BASE_URL/documents/nda-acme"

show "An edit sent with If-Match \"1\", the ETag read before that edit (412)"
send -X PATCH "$BASE_URL/documents/nda-acme" \
    -H 'Content-Type: application/json' \
    -H 'If-Match: "1"' \
    -d '{"changes": [{"operation": "replace", "target": {"text": "", "range": {"start": 0, "end": 0}}, "replacement": "DRAFT. "}]}'

show "By position, with If-Match \"2\" (current): replace 20 to 33, which must still hold the target text (200)"
send -X PATCH "$BASE_URL/documents/nda-acme" \
    -H 'Content-Type: application/json' \
    -H 'If-Match: "2"' \
    -d '{"changes": [{"operation": "replace", "target": {"text": "shall protect", "range": {"start": 20, "end": 33}}, "replacement": "shall safeguard"}]}'

show "The same request again: 20 to 33 no longer holds that text, so it is stale (409)"
send -X PATCH "$BASE_URL/documents/nda-acme" \
    -H 'Content-Type: application/json' \
    -H "If-Match: $(etag_of nda-acme)" \
    -d '{"changes": [{"operation": "replace", "target": {"text": "shall protect", "range": {"start": 20, "end": 33}}, "replacement": "shall safeguard"}]}'

show "Range past the end of the document (409)"
send -X PATCH "$BASE_URL/documents/nda-acme" \
    -H 'Content-Type: application/json' \
    -H "If-Match: $(etag_of nda-acme)" \
    -d '{"changes": [{"operation": "replace", "target": {"text": "30 days", "range": {"start": 500, "end": 507}}, "replacement": "60 days"}]}'

show "Insert at the start: empty target text at the zero-width range 0 to 0 (200)"
send -X PATCH "$BASE_URL/documents/nda-acme" \
    -H 'Content-Type: application/json' \
    -H "If-Match: $(etag_of nda-acme)" \
    -d '{"changes": [{"operation": "replace", "target": {"text": "", "range": {"start": 0, "end": 0}}, "replacement": "CONFIDENTIAL. "}]}'

show "Occurrence and range together (422)"
send -X PATCH "$BASE_URL/documents/nda-acme" \
    -H 'Content-Type: application/json' \
    -H "If-Match: $(etag_of nda-acme)" \
    -d '{"changes": [{"operation": "replace", "target": {"text": "Recipient", "occurrence": 1, "range": {"start": 64, "end": 73}}, "replacement": "Receiver"}]}'

show "The assignment's position-only example, without expected text (422)"
send -X PATCH "$BASE_URL/documents/nda-acme" \
    -H 'Content-Type: application/json' \
    -H "If-Match: $(etag_of nda-acme)" \
    -d '{"changes": [{"operation": "replace", "range": {"start": 100, "end": 108}, "text": "new text"}]}'

show "Delete text with an empty replacement; the target is unique (200)"
send -X PATCH "$BASE_URL/documents/nda-acme" \
    -H 'Content-Type: application/json' \
    -H "If-Match: $(etag_of nda-acme)" \
    -d '{"changes": [{"operation": "replace", "target": {"text": " within 30 days"}, "replacement": ""}]}'

show "Non-ASCII target in another document (200)"
send -X PATCH "$BASE_URL/documents/msa-globex" \
    -H 'Content-Type: application/json' \
    -H "If-Match: $(etag_of msa-globex)" \
    -d '{"changes": [{"operation": "replace", "target": {"text": "José Álvarez"}, "replacement": "Renée Côté"}]}'

show "Two changes in one request, applied in order (200)"
send -X PATCH "$BASE_URL/documents/lease-initech" \
    -H 'Content-Type: application/json' \
    -H "If-Match: $(etag_of lease-initech)" \
    -d '{"changes": [
        {"operation": "replace", "target": {"text": "monthly rent of $8,500"}, "replacement": "monthly rent of $9,000"},
        {"operation": "replace", "target": {"text": "thirty-six (36) months"}, "replacement": "forty-eight (48) months"}
    ]}'

show "The second change is ambiguous, so neither is saved; the error names changes.1 (409)"
send -X PATCH "$BASE_URL/documents/lease-initech" \
    -H 'Content-Type: application/json' \
    -H "If-Match: $(etag_of lease-initech)" \
    -d '{"changes": [
        {"operation": "replace", "target": {"text": "Security Deposit"}, "replacement": "Deposit"},
        {"operation": "replace", "target": {"text": "Tenant"}, "replacement": "Lessee"}
    ]}'

show "Read the lease: \"Security Deposit\" is still there (200)"
send "$BASE_URL/documents/lease-initech"

show "Target not found (409)"
send -X PATCH "$BASE_URL/documents/nda-acme" \
    -H 'Content-Type: application/json' \
    -H "If-Match: $(etag_of nda-acme)" \
    -d '{"changes": [{"operation": "replace", "target": {"text": "Disclosing Party"}, "replacement": "Discloser"}]}'

show "Unknown document, with any If-Match (404)"
send -X PATCH "$BASE_URL/documents/nda-missing" \
    -H 'Content-Type: application/json' \
    -H 'If-Match: "1"' \
    -d '{"changes": [{"operation": "replace", "target": {"text": "Receiving Party"}, "replacement": "Recipient"}]}'

show "Old flat body without a changes list (422)"
send -X PATCH "$BASE_URL/documents/nda-acme" \
    -H 'Content-Type: application/json' \
    -H "If-Match: $(etag_of nda-acme)" \
    -d '{"target": "Recipient", "replacement": "Receiver"}'

show "Empty change list (422)"
send -X PATCH "$BASE_URL/documents/nda-acme" \
    -H 'Content-Type: application/json' \
    -H "If-Match: $(etag_of nda-acme)" \
    -d '{"changes": []}'

show "Empty target text without a range (422)"
send -X PATCH "$BASE_URL/documents/nda-acme" \
    -H 'Content-Type: application/json' \
    -H "If-Match: $(etag_of nda-acme)" \
    -d '{"changes": [{"operation": "replace", "target": {"text": ""}, "replacement": "x"}]}'

show "Occurrence sent as a string (422)"
send -X PATCH "$BASE_URL/documents/nda-acme" \
    -H 'Content-Type: application/json' \
    -H "If-Match: $(etag_of nda-acme)" \
    -d '{"changes": [{"operation": "replace", "target": {"text": "Recipient", "occurrence": "2"}, "replacement": "Receiver"}]}'

show "Misspelled field name (422)"
send -X PATCH "$BASE_URL/documents/nda-acme" \
    -H 'Content-Type: application/json' \
    -H "If-Match: $(etag_of nda-acme)" \
    -d '{"changes": [{"operation": "replace", "target": {"text": "Recipient", "occurence": 2}, "replacement": "Receiver"}]}'

show "Malformed JSON (422)"
send -X PATCH "$BASE_URL/documents/nda-acme" \
    -H 'Content-Type: application/json' \
    -H "If-Match: $(etag_of nda-acme)" \
    -d '{"changes": ['

echo
echo "======== Search after editing ========"

show "Search sees the edited NDA text (200)"
send -G "$BASE_URL/documents/search" \
    --data-urlencode "q=Recipient" \
    --data-urlencode "document_id=nda-acme"
