"""Request body models. Invalid bodies become 422 errors before a route runs."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# strict=True rejects "2", true, and 1.5 for an integer instead of converting
# them. extra="forbid" rejects misspelled fields like "occurence".
STRICT_FIELDS = ConfigDict(strict=True, extra="forbid")

MAX_CHANGES_PER_REQUEST = 100


class TextRange(BaseModel):
    model_config = STRICT_FIELDS

    start: int = Field(ge=0)
    end: int = Field(ge=0)


class TextTarget(BaseModel):
    # Rules that involve several fields (empty text only with a zero-width
    # range; occurrence or range, not both; the range length) are checked in
    # api.py's find_target_problem.
    model_config = STRICT_FIELDS

    text: str
    occurrence: int | None = Field(default=None, ge=1)
    range: TextRange | None = None


class Change(BaseModel):
    model_config = STRICT_FIELDS

    # Only "replace" exists. api.py's to_text_changes does not copy operation,
    # and text_ops.apply_one_change always replaces, so a name added here alone
    # would still run a replacement. A new operation must also be passed
    # through to_text_changes into a new TextChange field and handled in
    # apply_one_change.
    operation: Literal["replace"]
    target: TextTarget
    replacement: str


class EditRequest(BaseModel):
    model_config = STRICT_FIELDS

    changes: list[Change] = Field(min_length=1, max_length=MAX_CHANGES_PER_REQUEST)


class SuggestionRequest(BaseModel):
    model_config = STRICT_FIELDS

    instruction: str = Field(min_length=1, max_length=2000)
