"""Tests of the editor's event functions, with the HTTP calls replaced.

No server or model is involved: httpx.get, httpx.patch, and httpx.post are
replaced by fakes that record each request and return a ready-made response.
"""

import httpx
import pytest

# The editor imports Gradio, which is only in requirements-frontend.txt. Without
# it, these tests are skipped instead of failing.
redline_app = pytest.importorskip("frontend.redline_app")
gr = pytest.importorskip("gradio")

NDA_TEXT = (
    "The Receiving Party shall protect Confidential Information. "
    "The Receiving Party shall return all Confidential Information within 30 days.\n"
)


def suggestion_reply(
    target: str,
    occurrence: int | None,
    replacement: str,
    applies: bool = True,
    match_count: int = 1,
    reason: str | None = None,
) -> dict:
    return {
        "document_id": "nda-acme",
        "suggestion": {
            "operation": "replace",
            "target": {"text": target, "occurrence": occurrence},
            "replacement": replacement,
        },
        "explanation": "Changed the return period as instructed.",
        "applies": applies,
        "match_count": match_count,
        "reason": reason,
    }


class FakePost:
    """Stands in for httpx.post: records each call and returns one response."""

    def __init__(self, status_code: int, body: dict) -> None:
        self.response = httpx.Response(status_code, json=body)
        self.calls = []

    def __call__(self, url: str, json: dict, timeout: float) -> httpx.Response:
        self.calls.append({"url": url, "json": json})
        return self.response


def fake_post(
    monkeypatch: pytest.MonkeyPatch, status_code: int, body: dict
) -> FakePost:
    fake = FakePost(status_code, body)
    monkeypatch.setattr(httpx, "post", fake)
    return fake


LOADED_ETAG = '"1"'


def request(instruction: str, edited_text: str = NDA_TEXT) -> tuple:
    """Suggest's first step, for nda-acme as loaded. Returns (pending, status)."""
    return redline_app.request_suggestion(
        "nda-acme", instruction, LOADED_ETAG, NDA_TEXT, edited_text
    )


def suggest(instruction: str) -> tuple:
    """Both steps of Suggest, with the editor unchanged in between.

    Returns show_suggestion's outputs.
    """
    pending, _ = request(instruction)
    return redline_app.show_suggestion(
        pending, "nda-acme", LOADED_ETAG, NDA_TEXT, NDA_TEXT, True
    )


def nothing_changed(outputs: tuple) -> bool:
    """True if every output except the status is left as it was."""
    for output in outputs[:-1]:
        if output != gr.skip():
            return False
    return True


# request_suggestion and show_suggestion


def test_applicable_suggestion_becomes_an_unsaved_edit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = fake_post(monkeypatch, 200, suggestion_reply("30 days", None, "60 days"))

    text, preview, change_list, status = suggest("Make it 60 days")

    assert fake.calls == [
        {
            "url": f"{redline_app.BACKEND_URL}/documents/nda-acme/suggestions",
            "json": {"instruction": "Make it 60 days"},
        }
    ]
    assert text == NDA_TEXT.replace("30 days", "60 days")
    assert "<del" in preview and "30" in preview
    assert "<ins" in preview and "60" in preview
    # The change list is HTML, so its quotes are escaped.
    assert "Replaced &#x27;30&#x27; with &#x27;60&#x27;" in change_list
    assert status == (
        "Suggested, not saved: Changed the return period as instructed. "
        "Save to accept it, or Reload to discard it."
    )


def test_suggestion_with_an_occurrence_replaces_that_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_post(monkeypatch, 200, suggestion_reply("Receiving Party", 2, "Recipient"))
    text = suggest("Shorten the second party name")[0]
    assert text == (
        "The Receiving Party shall protect Confidential Information. "
        "The Recipient shall return all Confidential Information within 30 days.\n"
    )


def test_unsaved_edits_block_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = fake_post(monkeypatch, 200, suggestion_reply("30 days", None, "60 days"))
    pending, status = request("Make it 60 days", edited_text=NDA_TEXT + "Extra.")
    assert fake.calls == []
    assert pending is None
    assert status == "Save or reload before asking for a suggestion."


def test_empty_instruction_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = fake_post(monkeypatch, 200, suggestion_reply("30 days", None, "60 days"))
    pending, status = request("   ")
    assert fake.calls == []
    assert pending is None
    assert status == "Type an instruction first."


def test_no_loaded_document_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = fake_post(monkeypatch, 200, suggestion_reply("30 days", None, "60 days"))
    pending, status = redline_app.request_suggestion("", "Make it 60 days", "", "", "")
    assert fake.calls == []
    assert pending is None
    assert status == "No document is loaded. Pick one or press Reload."


def test_suggestion_that_does_not_apply_gives_nothing_to_show(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reply = suggestion_reply(
        "Receiving Party",
        None,
        "Recipient",
        applies=False,
        match_count=2,
        reason="ambiguous",
    )
    fake_post(monkeypatch, 200, reply)
    pending, status = request("Rename the party")
    assert pending is None
    assert status == (
        "The suggestion does not apply (ambiguous): its target text appears "
        "2 time(s). Nothing changed."
    )


def test_suggestion_that_does_not_fit_locally_gives_nothing_to_show(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The service said it applies, but its target is not in the loaded text
    # (for example, the document changed on the server after it was loaded).
    fake_post(monkeypatch, 200, suggestion_reply("90 days", None, "60 days"))
    pending, status = request("Make it 60 days")
    assert pending is None
    assert status == (
        "The suggestion does not fit the loaded text, so nothing changed. "
        "Reload and try again."
    )


@pytest.mark.parametrize(
    "status_code, message",
    [
        (503, "Suggestions are unavailable: ANTHROPIC_API_KEY is not set."),
        (502, "The language model returned a malformed reply."),
        (404, "Document 'nda-acme' not found."),
    ],
)
def test_service_errors_show_the_service_message(
    monkeypatch: pytest.MonkeyPatch, status_code: int, message: str
) -> None:
    fake_post(monkeypatch, status_code, {"error": message, "code": status_code})
    pending, status = request("Make it 60 days")
    assert pending is None
    assert status == f"No suggestion. Error {status_code}: {message}"


def test_unreachable_service_gives_nothing_to_show(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_post(url: str, json: dict, timeout: float) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", failing_post)
    pending, status = request("Make it 60 days")
    assert pending is None
    assert status.startswith("No suggestion: could not reach the service at")


def test_no_pending_suggestion_changes_nothing() -> None:
    outputs = redline_app.show_suggestion(
        None, "nda-acme", LOADED_ETAG, NDA_TEXT, NDA_TEXT, True
    )
    for output in outputs:
        assert output == gr.skip()


# Several events in a row, with the editor's values kept in a dict.
#
# Gradio runs one event function per click and then copies its outputs into
# the components. A reply can arrive after the user has started another event,
# so these tests run the functions in the order their replies would arrive.

LEASE_TEXT = "The Tenant shall pay rent on the first day of each month.\n"

# The editor values each event function returns, in order.
LOAD_OUTPUTS = [
    "text",
    "document_id",
    "original_text",
    "etag",
    "preview",
    "change_list",
    "status",
]


class FakeService:
    """Stands in for GET and PATCH /documents/{id} and POST .../suggestions.

    PATCH does not apply the changes it receives: a test sets text_after_patch
    to the text the save should produce.
    """

    def __init__(self) -> None:
        self.texts = {"nda-acme": NDA_TEXT, "lease-initech": LEASE_TEXT}
        self.versions = {"nda-acme": 1, "lease-initech": 1}
        self.text_after_patch = ""
        self.patch_calls = []

    def document_id_from(self, url: str) -> str:
        path = url.removeprefix(f"{redline_app.BACKEND_URL}/documents/")
        return path.removesuffix("/suggestions")

    def document_response(self, document_id: str) -> httpx.Response:
        body = {"document_id": document_id, "text": self.texts[document_id]}
        headers = {"ETag": f'"{self.versions[document_id]}"'}
        return httpx.Response(200, json=body, headers=headers)

    def edit_elsewhere(self, document_id: str, new_text: str) -> None:
        """Another client saves new text, so the version goes up."""
        self.texts[document_id] = new_text
        self.versions[document_id] += 1

    def get(self, url: str, timeout: float) -> httpx.Response:
        return self.document_response(self.document_id_from(url))

    def patch(
        self, url: str, json: dict, headers: dict, timeout: float
    ) -> httpx.Response:
        self.patch_calls.append({"url": url, "json": json, "headers": headers})
        document_id = self.document_id_from(url)
        self.edit_elsewhere(document_id, self.text_after_patch)
        return self.document_response(document_id)

    def post(self, url: str, json: dict, timeout: float) -> httpx.Response:
        return httpx.Response(200, json=suggestion_reply("30 days", None, "60 days"))


def fake_service(monkeypatch: pytest.MonkeyPatch) -> FakeService:
    service = FakeService()
    monkeypatch.setattr(httpx, "get", service.get)
    monkeypatch.setattr(httpx, "patch", service.patch)
    monkeypatch.setattr(httpx, "post", service.post)
    return service


def apply_outputs(editor: dict, names: list[str], outputs: tuple) -> None:
    """Copy outputs into the editor, as Gradio does; gr.skip() keeps a value."""
    for name, value in zip(names, outputs, strict=True):
        if value != gr.skip():
            editor[name] = value


def load(editor: dict, document_id: str) -> None:
    apply_outputs(editor, LOAD_OUTPUTS, redline_app.load_document(document_id, True))


def save(editor: dict) -> tuple:
    """Run Save with the editor's values and return its outputs, not yet applied."""
    return redline_app.save_changes(
        editor["document_id"],
        editor["etag"],
        editor["original_text"],
        editor["text"],
        True,
    )


def test_late_save_reply_keeps_the_loaded_id_text_and_etag_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = fake_service(monkeypatch)
    editor = {}
    load(editor, "nda-acme")
    editor["text"] = NDA_TEXT + "Signed."
    service.text_after_patch = NDA_TEXT + "Signed."

    # Save is clicked, then another document is picked, and that load's
    # reply arrives before Save's.
    save_outputs = save(editor)
    load(editor, "lease-initech")
    apply_outputs(editor, LOAD_OUTPUTS, save_outputs)

    assert editor["document_id"] == "nda-acme"
    assert editor["text"] == NDA_TEXT + "Signed."
    assert editor["original_text"] == NDA_TEXT + "Signed."
    assert editor["etag"] == '"2"'
    assert editor["status"] == "Saved nda-acme."
    assert len(service.patch_calls) == 1


# Suggest: the model's reply can arrive many seconds after the click.

SHOW_SUGGESTION_OUTPUTS = ["text", "preview", "change_list", "status"]
DISCARDED = (
    "Suggestion discarded: the document or its text changed while the model "
    "was working. Ask again if you still want it."
)


def click_suggest(editor: dict) -> object:
    """Run Suggest's first step with the editor's values at the click."""
    pending, status = redline_app.request_suggestion(
        editor["document_id"],
        "Make it 60 days",
        editor["etag"],
        editor["original_text"],
        editor["text"],
    )
    editor["status"] = status
    assert pending is not None
    return pending


def suggestion_arrives(editor: dict, pending: object) -> None:
    """Run Suggest's second step with the editor's values when it starts."""
    outputs = redline_app.show_suggestion(
        pending,
        editor["document_id"],
        editor["etag"],
        editor["original_text"],
        editor["text"],
        True,
    )
    apply_outputs(editor, SHOW_SUGGESTION_OUTPUTS, outputs)


def assert_discarded(editor: dict, editor_before: dict) -> None:
    assert editor["status"] == DISCARDED
    for name in LOAD_OUTPUTS:
        if name != "status":
            assert editor[name] == editor_before[name]


def test_suggestion_is_shown_and_saved_when_the_editor_did_not_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = fake_service(monkeypatch)
    editor = {}
    load(editor, "nda-acme")
    pending = click_suggest(editor)
    suggestion_arrives(editor, pending)
    assert editor["text"] == NDA_TEXT.replace("30 days", "60 days")

    service.text_after_patch = editor["text"]
    apply_outputs(editor, LOAD_OUTPUTS, save(editor))
    assert editor["status"] == "Saved nda-acme."
    assert service.patch_calls[0]["url"].endswith("/documents/nda-acme")
    assert service.patch_calls[0]["headers"] == {"If-Match": '"1"'}


def test_suggestion_is_discarded_after_switching_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = fake_service(monkeypatch)
    editor = {}
    load(editor, "nda-acme")
    pending = click_suggest(editor)
    load(editor, "lease-initech")
    editor_before = dict(editor)

    suggestion_arrives(editor, pending)
    assert_discarded(editor, editor_before)
    assert editor["text"] == LEASE_TEXT

    apply_outputs(editor, LOAD_OUTPUTS, save(editor))
    assert editor["status"] == "No changes to save."
    assert service.patch_calls == []


def test_suggestion_is_discarded_after_typing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = fake_service(monkeypatch)
    editor = {}
    load(editor, "nda-acme")
    pending = click_suggest(editor)
    editor["text"] = NDA_TEXT + "Signed."
    editor_before = dict(editor)

    suggestion_arrives(editor, pending)
    assert_discarded(editor, editor_before)

    # Save sends the typed edit only.
    service.text_after_patch = NDA_TEXT + "Signed."
    apply_outputs(editor, LOAD_OUTPUTS, save(editor))
    assert editor["status"] == "Saved nda-acme."
    changes = service.patch_calls[0]["json"]["changes"]
    assert len(changes) == 1
    assert changes[0]["replacement"] == "Signed."


def test_suggestion_is_discarded_after_reloading_a_newer_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = fake_service(monkeypatch)
    editor = {}
    load(editor, "nda-acme")
    pending = click_suggest(editor)
    service.edit_elsewhere("nda-acme", NDA_TEXT + "Amended.\n")
    load(editor, "nda-acme")
    editor_before = dict(editor)
    assert editor["etag"] == '"2"'

    suggestion_arrives(editor, pending)
    assert_discarded(editor, editor_before)
    assert editor["text"] == NDA_TEXT + "Amended.\n"

    apply_outputs(editor, LOAD_OUTPUTS, save(editor))
    assert editor["status"] == "No changes to save."
    assert service.patch_calls == []


def test_suggestion_is_discarded_after_saving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = fake_service(monkeypatch)
    editor = {}
    load(editor, "nda-acme")
    pending = click_suggest(editor)
    editor["text"] = NDA_TEXT + "Signed."
    service.text_after_patch = NDA_TEXT + "Signed."
    apply_outputs(editor, LOAD_OUTPUTS, save(editor))
    editor_before = dict(editor)
    assert editor["etag"] == '"2"'

    suggestion_arrives(editor, pending)
    assert_discarded(editor, editor_before)
    assert editor["text"] == NDA_TEXT + "Signed."

    apply_outputs(editor, LOAD_OUTPUTS, save(editor))
    assert editor["status"] == "No changes to save."
    assert len(service.patch_calls) == 1


# search_all_documents


class FakeGet:
    """Stands in for httpx.get: records each call and returns one response."""

    def __init__(self, status_code: int, body: dict) -> None:
        self.response = httpx.Response(status_code, json=body)
        self.calls = []

    def __call__(self, url: str, params: dict, timeout: float) -> httpx.Response:
        self.calls.append({"url": url, "params": params})
        return self.response


def fake_get(monkeypatch: pytest.MonkeyPatch, status_code: int, body: dict) -> FakeGet:
    fake = FakeGet(status_code, body)
    monkeypatch.setattr(httpx, "get", fake)
    return fake


SEARCH_REPLY = {
    "query": "confidential",
    "total_matches": 1,
    "results": [
        {
            "document_id": "nda-acme",
            "start": 34,
            "end": 46,
            "before": "shall protect ",
            "match": "Confidential",
            "after": " Information",
        }
    ],
}


@pytest.mark.parametrize("ignore_case", [True, False])
def test_search_sends_query_limit_and_ignore_case(
    monkeypatch: pytest.MonkeyPatch, ignore_case: bool
) -> None:
    fake = fake_get(monkeypatch, 200, SEARCH_REPLY)
    html = redline_app.search_all_documents("confidential", ignore_case)
    assert fake.calls == [
        {
            "url": f"{redline_app.BACKEND_URL}/documents/search",
            "params": {"q": "confidential", "limit": 20, "ignore_case": ignore_case},
        }
    ]
    assert html.startswith("<p>1 match(es), showing 1:</p>")
    assert "<mark>Confidential</mark>" in html


def test_empty_search_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = fake_get(monkeypatch, 200, SEARCH_REPLY)
    assert redline_app.search_all_documents("", False) == (
        "<p>Type something to search for.</p>"
    )
    assert fake.calls == []


def test_search_error_shows_the_service_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = {"error": "limit: Input should be less than or equal to 100", "code": 422}
    fake_get(monkeypatch, 422, body)
    assert redline_app.search_all_documents("fee", False) == (
        "<p>Search failed. Error 422: limit: Input should be less than or equal "
        "to 100</p>"
    )


def test_unreachable_service_during_search(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_get(url: str, params: dict, timeout: float) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "get", failing_get)
    html = redline_app.search_all_documents("fee", False)
    assert html.startswith("<p>Could not reach the service at")
