"""Word-level redline between two texts, the PATCH changes between them,
applying a model's suggested change, and search results as HTML.

Pure functions with no Gradio or HTTP imports, so they can be tested directly.
"""

import difflib
import html
import re
from dataclasses import dataclass

# A token is a word, a run of whitespace, or one punctuation mark. Every
# character matches one of the three, so joining the tokens gives the text back.
TOKEN_PATTERN = re.compile(r"\w+|\s+|[^\w\s]")

# Mid-tone colors, readable on both Gradio's light and dark themes.
DELETION_STYLE = "color: #e5484d; text-decoration: line-through;"
INSERTION_STYLE = "color: #3b82f6; text-decoration: underline;"
# pre-wrap keeps newlines and repeated spaces visible, as in the text box.
DOCUMENT_STYLE = "white-space: pre-wrap; line-height: 1.6;"
SNIPPET_STYLE = "white-space: pre-wrap;"

MAX_QUOTED_CHARS = 60


@dataclass
class DiffPiece:
    """One stretch of the diff. start and end are positions in the original
    text; for an insertion they are equal."""

    kind: str  # "equal", "replace", "delete", or "insert" (difflib's names)
    start: int
    end: int
    old_text: str
    new_text: str


def split_into_tokens(text: str) -> list[str]:
    return TOKEN_PATTERN.findall(text)


def token_start_positions(tokens: list[str]) -> list[int]:
    """Where each token starts in the text, plus one last entry: the text's end."""
    positions = []
    position = 0
    for token in tokens:
        positions.append(position)
        position += len(token)
    positions.append(position)
    return positions


def diff_texts(original: str, edited: str) -> list[DiffPiece]:
    """Compare the texts word by word and return the pieces in document order."""
    original_tokens = split_into_tokens(original)
    edited_tokens = split_into_tokens(edited)
    original_positions = token_start_positions(original_tokens)

    # autojunk=False: otherwise tokens as common as " " would be ignored in long
    # texts, which gives much worse diffs.
    matcher = difflib.SequenceMatcher(
        None, original_tokens, edited_tokens, autojunk=False
    )
    pieces = []
    for kind, old_first, old_last, new_first, new_last in matcher.get_opcodes():
        pieces.append(
            DiffPiece(
                kind=kind,
                start=original_positions[old_first],
                end=original_positions[old_last],
                old_text="".join(original_tokens[old_first:old_last]),
                new_text="".join(edited_tokens[new_first:new_last]),
            )
        )
    return pieces


def render_redline_html(pieces: list[DiffPiece]) -> str:
    """Deletions in red strikethrough, insertions underlined in blue.

    A replacement is shown as a deletion followed by an insertion.
    """
    parts = []
    for piece in pieces:
        if piece.kind == "equal":
            parts.append(html.escape(piece.old_text))
            continue
        if piece.kind == "replace" or piece.kind == "delete":
            deleted = html.escape(piece.old_text)
            parts.append(f'<del style="{DELETION_STYLE}">{deleted}</del>')
        if piece.kind == "replace" or piece.kind == "insert":
            inserted = html.escape(piece.new_text)
            parts.append(f'<ins style="{INSERTION_STYLE}">{inserted}</ins>')
    return f'<div style="{DOCUMENT_STYLE}">{"".join(parts)}</div>'


def render_clean_html(text: str) -> str:
    """The text with no markup, for when redline mode is off."""
    return f'<div style="{DOCUMENT_STYLE}">{html.escape(text)}</div>'


def quote_for_summary(text: str) -> str:
    """Quote changed text for the change list.

    Whitespace-only text is shown with escapes, such as '\\n', so it is visible.
    Other text loses its surrounding whitespace and is shortened if long.
    """
    if text.strip() == "":
        return repr(text)
    shown = text.strip()
    if len(shown) > MAX_QUOTED_CHARS:
        shown = shown[:MAX_QUOTED_CHARS].rstrip() + "…"
    return "'" + shown + "'"


def describe_changes(pieces: list[DiffPiece]) -> list[str]:
    """One line per change, in document order, e.g. "Inserted 'promptly'"."""
    descriptions = []
    for piece in pieces:
        old = quote_for_summary(piece.old_text)
        new = quote_for_summary(piece.new_text)
        if piece.kind == "replace":
            descriptions.append(f"Replaced {old} with {new}")
        elif piece.kind == "delete":
            descriptions.append(f"Deleted {old}")
        elif piece.kind == "insert":
            descriptions.append(f"Inserted {new}")
    return descriptions


def render_change_list_html(descriptions: list[str]) -> str:
    if len(descriptions) == 0:
        return "<p>No changes.</p>"
    items = []
    for description in descriptions:
        items.append(f"<li>{html.escape(description)}</li>")
    return f"<p>{len(descriptions)} change(s):</p><ol>{''.join(items)}</ol>"


def build_patch_changes(pieces: list[DiffPiece]) -> list[dict]:
    """The PATCH "changes" list that turns the original text into the edited one.

    Every change is by range, with positions in the original text and the text
    expected there. An insertion is a zero-width range (start == end) whose
    expected text is "". The list runs from the end of the document to the
    start: a change only moves text after it, so the positions of the changes
    still to come stay correct.
    """
    changes = []
    for piece in pieces:
        if piece.kind == "equal":
            continue
        changes.append(
            {
                "operation": "replace",
                "target": {
                    "text": piece.old_text,
                    "range": {"start": piece.start, "end": piece.end},
                },
                "replacement": piece.new_text,
            }
        )
    changes.reverse()
    return changes


def find_match_starts(text: str, target: str) -> list[int]:
    """Start of every match of target: literal, case-sensitive, non-overlapping,
    left to right.

    The same rule as the service's app/text_ops.find_match_starts. The front
    end never imports app/, so it has its own copy.
    """
    match_starts = []
    position = text.find(target)
    while position != -1:
        match_starts.append(position)
        position = text.find(target, position + len(target))
    return match_starts


def apply_suggestion(text: str, suggestion: dict) -> str | None:
    """Apply a suggestion from POST /documents/{id}/suggestions to text.

    suggestion is {"target": {"text", "occurrence"}, "replacement", ...}.
    occurrence is 1-based; None means the target must appear exactly once, as
    in PATCH. Returns the new text, or None if the change does not fit.
    """
    target = suggestion["target"]["text"]
    occurrence = suggestion["target"]["occurrence"]
    if target == "":
        return None

    match_starts = find_match_starts(text, target)
    if occurrence is None:
        if len(match_starts) != 1:
            return None
        start = match_starts[0]
    else:
        if occurrence < 1 or occurrence > len(match_starts):
            return None
        start = match_starts[occurrence - 1]

    end = start + len(target)
    return text[:start] + suggestion["replacement"] + text[end:]


def render_message_html(message: str) -> str:
    return f"<p>{html.escape(message)}</p>"


def render_search_results_html(reply: dict) -> str:
    """The reply of GET /documents/search as HTML: the match count, then one
    item per result with its document, position, and snippet. The match is
    highlighted, and all text from the documents is escaped."""
    if reply["total_matches"] == 0:
        return render_message_html("No matches.")

    items = []
    for result in reply["results"]:
        document_id = html.escape(result["document_id"])
        before = html.escape(result["before"])
        match = html.escape(result["match"])
        after = html.escape(result["after"])
        snippet = f"{before}<mark>{match}</mark>{after}"
        items.append(
            f"<li><b>{document_id}</b> at {result['start']}: "
            f'<span style="{SNIPPET_STYLE}">{snippet}</span></li>'
        )
    summary = f"{reply['total_matches']} match(es), showing {len(items)}:"
    return f"<p>{summary}</p><ol>{''.join(items)}</ol>"
