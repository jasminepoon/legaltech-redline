"""A small redline editor for the document service, built with Gradio.

It is only an HTTP client of the running service: it lists documents with
GET /documents, reads one with GET /documents/{id}, shows edits as tracked
changes, and saves them all with one PATCH /documents/{id}, keeping any
typing or document switch made while the PATCH runs. Suggest asks
POST /documents/{id}/suggestions for one edit and shows it as an unsaved edit,
unless the editor changed while the model was working.
Search lists matches in every document with GET /documents/search.

Run, with the service already running:
    python -m frontend.redline_app
"""

import os
from dataclasses import dataclass

import gradio as gr
import httpx

from frontend.redline import (
    apply_suggestion,
    build_patch_changes,
    describe_changes,
    diff_texts,
    render_change_list_html,
    render_clean_html,
    render_message_html,
    render_redline_html,
    render_search_results_html,
)

BACKEND_URL = os.environ.get("BACKEND_URL", "http://127.0.0.1:8000")
# The service accepts at most 100 changes in one PATCH.
MAX_CHANGES_PER_SAVE = 100
REQUEST_TIMEOUT_SECONDS = 10
# The service waits up to 30 s for each network step of the model call.
SUGGESTION_TIMEOUT_SECONDS = 60
# One page of results; there is no paging in the editor.
SEARCH_LIMIT = 20


def describe_error(response: httpx.Response) -> str:
    """The service's error message, e.g. "Error 409: changes.0: ..."."""
    try:
        message = response.json()["error"]
    except (ValueError, KeyError, TypeError):
        message = response.text
    return f"Error {response.status_code}: {message}"


def show_redline(original_text: str, edited_text: str, redline_on: bool):
    """Outputs: the preview and the change list."""
    pieces = diff_texts(original_text, edited_text)
    change_list = render_change_list_html(describe_changes(pieces))
    if redline_on:
        return render_redline_html(pieces), change_list
    return render_clean_html(edited_text), change_list


def status_only(output_count: int, status: str) -> tuple:
    """Outputs that leave every component unchanged except the last, the status."""
    outputs = []
    for _ in range(output_count - 1):
        outputs.append(gr.skip())
    outputs.append(status)
    return tuple(outputs)


def load_document(document_id: str | None, redline_on: bool):
    """Outputs (7): text box, loaded ID, original text, ETag, preview, change
    list, status.

    Unsaved edits are discarded. If the request fails, only the status changes.
    """
    # None when the document list could not be read, so nothing is selected.
    if document_id is None or document_id == "":
        status = "No document selected. Refresh the page once the service is running."
        return status_only(7, status)

    url = f"{BACKEND_URL}/documents/{document_id}"
    try:
        response = httpx.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    except httpx.HTTPError as error:
        status = f"Could not reach the service at {BACKEND_URL}: {error}"
        return status_only(7, status)
    if response.status_code != 200:
        return status_only(7, describe_error(response))

    text = response.json()["text"]
    # Sent back as If-Match on Save, so the service can refuse an outdated save.
    etag = response.headers.get("ETag", "")
    preview, change_list = show_redline(text, text, redline_on)
    status = f"Loaded {document_id}."
    return text, document_id, text, etag, preview, change_list, status


def start_page(redline_on: bool):
    """Outputs (8): the document dropdown, then the 7 outputs of load_document.

    Fills the dropdown from GET /documents and loads the first document. If the
    list cannot be read, only the status changes.
    """
    url = f"{BACKEND_URL}/documents"
    try:
        response = httpx.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    except httpx.HTTPError as error:
        status = f"Could not reach the service at {BACKEND_URL}: {error}"
        return status_only(8, status)
    if response.status_code != 200:
        return status_only(8, "Could not list documents. " + describe_error(response))

    document_ids = []
    for entry in response.json()["documents"]:
        document_ids.append(entry["document_id"])
    if len(document_ids) == 0:
        return status_only(8, "The service has no documents.")

    dropdown = gr.Dropdown(choices=document_ids, value=document_ids[0])
    return (dropdown,) + load_document(document_ids[0], redline_on)


@dataclass
class SaveResult:
    """A successful save: the document, the text box at the click, and the
    text and ETag the service saved."""

    document_id: str
    sent_text: str
    saved_text: str
    etag: str


def save_changes(
    document_id: str,
    etag: str,
    original_text: str,
    edited_text: str,
):
    """Outputs (2): the save result (None if nothing was saved), status.

    Sends every change in one PATCH, so either all are saved or none is. The
    editor is not changed here: the user may type or load another document
    while the PATCH runs. show_save_result runs next and decides.
    """
    # Empty when no load has succeeded yet; the URL would then name no document.
    if document_id == "":
        return None, "Not saved: no document is loaded. Pick one or press Reload."

    changes = build_patch_changes(diff_texts(original_text, edited_text))
    if len(changes) == 0:
        return None, "No changes to save."
    if len(changes) > MAX_CHANGES_PER_SAVE:
        status = (
            f"Not saved: {len(changes)} changes, but the service accepts at most "
            f"{MAX_CHANGES_PER_SAVE} per save. Save part of the edits first."
        )
        return None, status

    url = f"{BACKEND_URL}/documents/{document_id}"
    # If-Match: save only if the document is still the version that was loaded.
    headers = {}
    if etag != "":
        headers["If-Match"] = etag
    try:
        response = httpx.patch(
            url,
            json={"changes": changes},
            headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as error:
        status = f"Not saved: could not reach the service at {BACKEND_URL}: {error}"
        return None, status
    if response.status_code == 412:
        status = (
            "Not saved: the document changed since you loaded it. "
            "Reload it and make your edits again."
        )
        return None, status
    if response.status_code != 200:
        return None, "Not saved. " + describe_error(response)

    result = SaveResult(
        document_id=document_id,
        sent_text=edited_text,
        saved_text=response.json()["text"],
        etag=response.headers.get("ETag", ""),
    )
    return result, f"Saved {document_id}."


def show_save_result(
    result: SaveResult | None,
    document_id: str,
    edited_text: str,
    redline_on: bool,
):
    """Outputs (6): text box, original text, ETag, preview, change list, status.

    Runs after save_changes, with the editor's values as they are now. The
    saved text and ETag replace the loaded ones only if the editor still shows
    the saved document, and the text box only if nothing was typed since the
    click. So a save never erases newer typing, and never pairs one
    document's ETag with another document.
    """
    # save_changes's status already says why nothing was saved.
    if result is None:
        return gr.skip(), gr.skip(), gr.skip(), gr.skip(), gr.skip(), gr.skip()

    if result.document_id != document_id:
        status = (
            f"Saved {result.document_id}. Still showing {document_id}, "
            "which was loaded during the save."
        )
        return status_only(6, status)

    if edited_text != result.sent_text:
        preview, change_list = show_redline(result.saved_text, edited_text, redline_on)
        status = (
            f"Saved {result.document_id}. Edits typed during the save are "
            "kept as unsaved changes."
        )
        return (
            gr.skip(),
            result.saved_text,
            result.etag,
            preview,
            change_list,
            status,
        )

    preview, change_list = show_redline(
        result.saved_text, result.saved_text, redline_on
    )
    status = f"Saved {result.document_id}."
    return (
        result.saved_text,
        result.saved_text,
        result.etag,
        preview,
        change_list,
        status,
    )


@dataclass
class PendingSuggestion:
    """A suggested text, and the loaded document and version it was made for."""

    document_id: str
    etag: str
    original_text: str
    suggested_text: str
    explanation: str


def request_suggestion(
    document_id: str,
    instruction: str,
    etag: str,
    original_text: str,
    edited_text: str,
):
    """Outputs (2): the pending suggestion (None if there is none), status.

    Asks the model for one edit to the saved text. The editor is not changed
    here: the model can take many seconds, and the user may load, save, or
    type in the meantime. show_suggestion runs next and decides.
    """
    if document_id == "":
        return None, "No document is loaded. Pick one or press Reload."
    # The service suggests against the saved text, so unsaved edits would be
    # lost or mixed up with the suggestion.
    if edited_text != original_text:
        return None, "Save or reload before asking for a suggestion."
    if instruction.strip() == "":
        return None, "Type an instruction first."

    url = f"{BACKEND_URL}/documents/{document_id}/suggestions"
    try:
        response = httpx.post(
            url,
            json={"instruction": instruction},
            timeout=SUGGESTION_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as error:
        status = f"No suggestion: could not reach the service at {BACKEND_URL}: {error}"
        return None, status
    if response.status_code != 200:
        return None, "No suggestion. " + describe_error(response)

    reply = response.json()
    if not reply["applies"]:
        status = (
            f"The suggestion does not apply ({reply['reason']}): its target text "
            f"appears {reply['match_count']} time(s). Nothing changed."
        )
        return None, status

    suggested_text = apply_suggestion(original_text, reply["suggestion"])
    if suggested_text is None:
        status = (
            "The suggestion does not fit the loaded text, so nothing changed. "
            "Reload and try again."
        )
        return None, status

    pending = PendingSuggestion(
        document_id=document_id,
        etag=etag,
        original_text=original_text,
        suggested_text=suggested_text,
        explanation=reply["explanation"],
    )
    return pending, "Suggestion received."


def show_suggestion(
    pending: PendingSuggestion | None,
    document_id: str,
    etag: str,
    original_text: str,
    edited_text: str,
    redline_on: bool,
):
    """Outputs (4): text box, preview, change list, status.

    Runs after request_suggestion, with the editor's values as they are now.
    Puts the suggestion in the text box as an unsaved edit (Save accepts it,
    Reload discards it), but only if the editor still holds exactly the
    document, version, and text it was made for. Otherwise the suggestion is
    discarded, so it can never replace newer work or be saved into another
    document.
    """
    # request_suggestion's status already says why there is no suggestion.
    if pending is None:
        return gr.skip(), gr.skip(), gr.skip(), gr.skip()

    same_document = pending.document_id == document_id
    # A Reload or Save may have brought a new version or new text.
    same_version = pending.etag == etag and pending.original_text == original_text
    no_new_edits = edited_text == original_text
    if not (same_document and same_version and no_new_edits):
        status = (
            "Suggestion discarded: the document or its text changed while the "
            "model was working. Ask again if you still want it."
        )
        return status_only(4, status)

    preview, change_list = show_redline(
        original_text, pending.suggested_text, redline_on
    )
    status = (
        "Suggested, not saved: "
        + pending.explanation
        + " Save to accept it, or Reload to discard it."
    )
    return pending.suggested_text, preview, change_list, status


def search_all_documents(query: str, ignore_case: bool) -> str:
    """Output: the results as HTML, or a message.

    Searches the saved text of every document, not unsaved edits.
    """
    if query == "":
        return render_message_html("Type something to search for.")

    url = f"{BACKEND_URL}/documents/search"
    params = {"q": query, "limit": SEARCH_LIMIT, "ignore_case": ignore_case}
    try:
        response = httpx.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
    except httpx.HTTPError as error:
        message = f"Could not reach the service at {BACKEND_URL}: {error}"
        return render_message_html(message)
    if response.status_code != 200:
        return render_message_html("Search failed. " + describe_error(response))
    return render_search_results_html(response.json())


def build_app() -> gr.Blocks:
    # analytics_enabled=False: send no usage statistics to Gradio's servers.
    with gr.Blocks(title="Redline editor", analytics_enabled=False) as demo:
        gr.Markdown(
            "# Redline editor\n"
            "Edit the text on the left. With redline mode on, the right side shows "
            "deletions struck through in red and insertions underlined in blue. "
            "Save sends every change in one request."
        )
        # The document the text was loaded from, its text at that time, and its
        # ETag. Save uses these, not the dropdown, which may show another choice.
        loaded_document_id = gr.State("")
        original_text = gr.State("")
        loaded_etag = gr.State("")
        # Set by save_changes, read by show_save_result.
        save_result = gr.State(None)
        # Set by request_suggestion, read by show_suggestion.
        pending_suggestion = gr.State(None)

        with gr.Row():
            # Filled from GET /documents when the page opens (start_page).
            document_dropdown = gr.Dropdown(choices=[], value=None, label="Document")
            reload_button = gr.Button("Reload (discards unsaved edits)")
        redline_checkbox = gr.Checkbox(value=True, label="Redline mode")
        with gr.Row():
            text_box = gr.Textbox(label="Document text", lines=16, max_lines=40)
            preview = gr.HTML()
        change_list = gr.HTML()
        save_button = gr.Button("Save", variant="primary")
        status_box = gr.Textbox(label="Status", interactive=False)

        gr.Markdown("## Ask the model for one edit")
        with gr.Row():
            instruction_box = gr.Textbox(
                label="Instruction",
                placeholder="For example: change the return period to 60 days",
            )
            suggest_button = gr.Button("Suggest")

        gr.Markdown("## Search all documents")
        with gr.Row():
            search_box = gr.Textbox(label="Search for")
            ignore_case_checkbox = gr.Checkbox(value=False, label="Ignore case")
            search_button = gr.Button("Search")
        search_results = gr.HTML()

        load_outputs = [
            text_box,
            loaded_document_id,
            original_text,
            loaded_etag,
            preview,
            change_list,
            status_box,
        ]
        demo.load(
            start_page,
            inputs=[redline_checkbox],
            outputs=[document_dropdown] + load_outputs,
        )
        document_dropdown.input(
            load_document,
            inputs=[document_dropdown, redline_checkbox],
            outputs=load_outputs,
        )
        reload_button.click(
            load_document,
            inputs=[document_dropdown, redline_checkbox],
            outputs=load_outputs,
        )
        # .change, not .input: in Gradio 6 a Textbox fires .input only on
        # keypress, which Backspace, Delete, cut, and paste do not trigger.
        # always_last: while one update runs, keep only the latest one waiting,
        # so the preview always ends up matching the final text.
        text_box.change(
            show_redline,
            inputs=[original_text, text_box, redline_checkbox],
            outputs=[preview, change_list],
            trigger_mode="always_last",
            show_progress="hidden",
        )
        redline_checkbox.input(
            show_redline,
            inputs=[original_text, text_box, redline_checkbox],
            outputs=[preview, change_list],
            show_progress="hidden",
        )
        # Save and Suggest are two steps each: a step chained with .then()
        # gets the component values as they are when it starts, so
        # show_save_result and show_suggestion see any load, save, or typing
        # that happened while the first step was waiting for the service.
        save_button.click(
            save_changes,
            inputs=[loaded_document_id, loaded_etag, original_text, text_box],
            outputs=[save_result, status_box],
        ).then(
            show_save_result,
            inputs=[save_result, loaded_document_id, text_box, redline_checkbox],
            outputs=[
                text_box,
                original_text,
                loaded_etag,
                preview,
                change_list,
                status_box,
            ],
        )
        suggest_button.click(
            request_suggestion,
            inputs=[
                loaded_document_id,
                instruction_box,
                loaded_etag,
                original_text,
                text_box,
            ],
            outputs=[pending_suggestion, status_box],
        ).then(
            show_suggestion,
            inputs=[
                pending_suggestion,
                loaded_document_id,
                loaded_etag,
                original_text,
                text_box,
                redline_checkbox,
            ],
            outputs=[text_box, preview, change_list, status_box],
        )
        search_button.click(
            search_all_documents,
            inputs=[search_box, ignore_case_checkbox],
            outputs=[search_results],
        )
    return demo


def main() -> None:
    build_app().launch()


if __name__ == "__main__":
    main()
