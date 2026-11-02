"""Pure text functions for finding, replacing, and quoting literal text.

Matching rule shared by replacement and search: literal, case-sensitive,
non-overlapping, left to right. After a match, scanning resumes at the end of
that match, so "aaaa" contains "aa" twice (at 0 and 2), not three times.
Search can opt out of case-sensitivity with find_match_starts_ignore_case.
"""

from dataclasses import dataclass

SNIPPET_CONTEXT_CHARS = 40


class TargetNotFoundError(Exception):
    """The target text does not appear in the document."""


class AmbiguousTargetError(Exception):
    """The target appears more than once and no occurrence was given."""

    def __init__(self, match_count: int) -> None:
        super().__init__(f"target appears {match_count} times")
        self.match_count = match_count


class OccurrenceOutOfRangeError(Exception):
    """The requested occurrence is larger than the number of matches."""

    def __init__(self, occurrence: int, match_count: int) -> None:
        super().__init__(
            f"occurrence {occurrence} requested, target appears {match_count} times"
        )
        self.occurrence = occurrence
        self.match_count = match_count


class RangeOutOfBoundsError(Exception):
    """The range ends past the end of the text."""

    def __init__(self, end: int, text_length: int) -> None:
        super().__init__(f"range end {end} is past the text length {text_length}")
        self.end = end
        self.text_length = text_length


class RangeTextMismatchError(Exception):
    """The text at the range is not the expected text: the position is stale."""

    def __init__(self, start: int, end: int) -> None:
        super().__init__(f"text at {start} to {end} is not the expected text")
        self.start = start
        self.end = end


class ChangeFailedError(Exception):
    """One change in a list could not be applied. index is 0-based."""

    def __init__(self, index: int, reason: Exception) -> None:
        super().__init__(f"change {index} failed: {reason}")
        self.index = index
        self.reason = reason


@dataclass
class ReplacementResult:
    text: str
    # None for a range replacement, which is not chosen by occurrence.
    occurrence: int | None
    start: int


@dataclass
class TextChange:
    """One replacement. With range_start and range_end set, target is the text
    expected at that range; otherwise target is found by the occurrence rules."""

    target: str
    replacement: str
    occurrence: int | None = None
    range_start: int | None = None
    range_end: int | None = None


@dataclass
class AppliedChange:
    start: int
    occurrence: int | None


@dataclass
class EditResult:
    text: str
    changes: list[AppliedChange]


@dataclass
class Snippet:
    start: int
    end: int
    before: str
    match: str
    after: str


def is_valid_unicode(text: str) -> bool:
    """Return False for text containing a lone surrogate such as "\\ud800".

    JSON allows such escapes, but the text cannot be encoded as UTF-8, so
    saving it would make every later response for the document fail.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def find_match_starts(text: str, target: str) -> list[int]:
    """Return the start index of every non-overlapping match, left to right."""
    if target == "":
        raise ValueError("target must not be empty")

    match_starts = []
    position = text.find(target)
    while position != -1:
        match_starts.append(position)
        position = text.find(target, position + len(target))
    return match_starts


def lowercase_same_length(text: str) -> str:
    """Lowercase each character on its own, so every index still lines up.

    A character whose lowercase form is not exactly one character is kept as
    it is: only "İ", which becomes "i" plus a combining dot. Because each
    character is lowered independently, the result for one part of the text
    never depends on any other part.
    """
    # Fast path: text.lower() on the whole text gives the same result, except
    # for "İ" (two characters) and "Σ" (lowered to "ς" at the end of a word,
    # but to "σ" on its own). A test checks both ways agree for every other
    # character.
    if "İ" not in text and "Σ" not in text:
        return text.lower()

    characters = []
    for character in text:
        lowered_character = character.lower()
        if len(lowered_character) == 1:
            characters.append(lowered_character)
        else:
            characters.append(character)
    return "".join(characters)


def find_match_starts_ignore_case(text: str, target: str) -> list[int]:
    """Like find_match_starts, but letters match regardless of case.

    Searches same-length lowercase copies, so each start is also an index into
    the original text, and a match is len(target) characters long there too.
    Used by search only; edits stay case-sensitive.
    """
    return find_match_starts(lowercase_same_length(text), lowercase_same_length(target))


def choose_match_start(match_starts: list[int], occurrence: int | None) -> int:
    """Pick the start index to replace, applying the occurrence rules.

    occurrence is 1-based. When it is None, the target must appear exactly once.
    """
    match_count = len(match_starts)
    if match_count == 0:
        raise TargetNotFoundError()

    if occurrence is None:
        if match_count > 1:
            raise AmbiguousTargetError(match_count)
        return match_starts[0]

    if occurrence < 1:
        raise ValueError("occurrence must be 1 or greater")
    if occurrence > match_count:
        raise OccurrenceOutOfRangeError(occurrence, match_count)
    return match_starts[occurrence - 1]


def replace_one_occurrence(
    text: str, target: str, replacement: str, occurrence: int | None
) -> ReplacementResult:
    """Replace exactly one match of target and return the new text.

    The replacement text is inserted as-is and never re-scanned.
    """
    match_starts = find_match_starts(text, target)
    start = choose_match_start(match_starts, occurrence)
    end = start + len(target)
    new_text = text[:start] + replacement + text[end:]

    if occurrence is None:
        replaced_occurrence = 1
    else:
        replaced_occurrence = occurrence
    return ReplacementResult(text=new_text, occurrence=replaced_occurrence, start=start)


def replace_range(
    text: str, start: int, end: int, expected_text: str, replacement: str
) -> ReplacementResult:
    """Replace text[start:end] with replacement, if it still holds expected_text.

    Positions go stale when the text changes after a client read it. Checking
    the expected text turns a stale position into an error, not a wrong edit.
    """
    if start < 0 or start > end:
        raise ValueError("range must satisfy 0 <= start <= end")
    if end - start != len(expected_text):
        raise ValueError("range length must equal the expected text's length")
    if end > len(text):
        raise RangeOutOfBoundsError(end, len(text))
    if text[start:end] != expected_text:
        raise RangeTextMismatchError(start, end)

    new_text = text[:start] + replacement + text[end:]
    return ReplacementResult(text=new_text, occurrence=None, start=start)


def apply_one_change(text: str, change: TextChange) -> ReplacementResult:
    if change.range_start is None and change.range_end is None:
        return replace_one_occurrence(
            text, change.target, change.replacement, change.occurrence
        )
    if change.range_start is None or change.range_end is None:
        raise ValueError("range_start and range_end must be given together")
    if change.occurrence is not None:
        raise ValueError("a change has an occurrence or a range, not both")
    return replace_range(
        text, change.range_start, change.range_end, change.target, change.replacement
    )


def apply_changes(text: str, changes: list[TextChange]) -> EditResult:
    """Apply changes in order, each to the text produced by the ones before it.

    Raises ChangeFailedError for the first change that does not fit. The text
    passed in is never modified, so a failure leaves nothing half-applied.
    """
    current_text = text
    applied_changes = []
    for index, change in enumerate(changes):
        try:
            result = apply_one_change(current_text, change)
        except (
            TargetNotFoundError,
            AmbiguousTargetError,
            OccurrenceOutOfRangeError,
            RangeOutOfBoundsError,
            RangeTextMismatchError,
        ) as error:
            raise ChangeFailedError(index, error) from error

        current_text = result.text
        # Keep only start and occurrence: keeping each result would hold one
        # full copy of the document per change.
        applied_changes.append(
            AppliedChange(start=result.start, occurrence=result.occurrence)
        )
    return EditResult(text=current_text, changes=applied_changes)


def build_snippet(text: str, start: int, end: int) -> Snippet:
    """Quote text[start:end] with up to SNIPPET_CONTEXT_CHARS on each side.

    Context is clipped at the start and end of text. Nothing is trimmed,
    padded, or marked with an ellipsis, and newlines are kept as they are.
    """
    if start < 0 or end < start or end > len(text):
        raise ValueError("start and end must describe a range inside text")

    # max() matters: a negative slice index would count from the end of text.
    before_start = max(0, start - SNIPPET_CONTEXT_CHARS)
    after_end = min(len(text), end + SNIPPET_CONTEXT_CHARS)
    return Snippet(
        start=start,
        end=end,
        before=text[before_start:start],
        match=text[start:end],
        after=text[end:after_end],
    )
