import httpx
import pytest

from app import llm_client
from app.llm_client import (
    LLMNotConfiguredError,
    LLMUnavailableError,
    MalformedSuggestionError,
    Suggestion,
    build_prompt,
    parse_suggestion,
    request_model_reply,
)

# build_prompt


def test_prompt_contains_instruction_and_document() -> None:
    prompt = build_prompt("The Tenant shall pay rent.", "Rename Tenant to Lessee")
    assert "Rename Tenant to Lessee" in prompt
    assert "The Tenant shall pay rent." in prompt


def test_prompt_asks_for_json_with_exact_target() -> None:
    prompt = build_prompt("text", "instruction")
    assert "JSON" in prompt
    assert "EXACTLY" in prompt
    assert '"occurrence"' in prompt


def test_prompt_keeps_braces_in_document_text() -> None:
    prompt = build_prompt('Fee {"amount": 5}', "Change the fee")
    assert 'Fee {"amount": 5}' in prompt


# parse_suggestion: well-formed replies


def test_parse_suggestion_with_null_occurrence() -> None:
    raw = (
        '{"target": "within 30 days", "replacement": "within 60 days", '
        '"occurrence": null, "explanation": "Extends the return period."}'
    )
    assert parse_suggestion(raw) == Suggestion(
        target="within 30 days",
        replacement="within 60 days",
        occurrence=None,
        explanation="Extends the return period.",
    )


def test_parse_suggestion_with_occurrence_and_empty_replacement() -> None:
    raw = (
        '{"target": "Tenant", "replacement": "", "occurrence": 2, '
        '"explanation": "Deletes the second Tenant."}'
    )
    suggestion = parse_suggestion(raw)
    assert suggestion.occurrence == 2
    assert suggestion.replacement == ""


def test_parse_suggestion_without_occurrence_key_means_null() -> None:
    raw = '{"target": "a", "replacement": "b", "explanation": "c"}'
    assert parse_suggestion(raw).occurrence is None


def test_parse_suggestion_keeps_non_ascii_text() -> None:
    raw = '{"target": "“Customer”", "replacement": "“Client”", "explanation": "é"}'
    assert parse_suggestion(raw).target == "“Customer”"


# parse_suggestion: malformed replies


@pytest.mark.parametrize(
    "raw",
    [
        "Sure! Here is the edit.",
        '{"target": "a",',
        '["a", "b"]',
        '"just a string"',
        '{"replacement": "b", "explanation": "c"}',
        '{"target": 5, "replacement": "b", "explanation": "c"}',
        '{"target": "", "replacement": "b", "explanation": "c"}',
        '{"target": "a", "explanation": "c"}',
        '{"target": "a", "replacement": null, "explanation": "c"}',
        '{"target": "a", "replacement": "b"}',
        '{"target": "a", "replacement": "b", "explanation": 7}',
        '{"target": "a", "replacement": "b", "occurrence": "2", "explanation": "c"}',
        '{"target": "a", "replacement": "b", "occurrence": true, "explanation": "c"}',
        '{"target": "a", "replacement": "b", "occurrence": 1.5, "explanation": "c"}',
        '{"target": "a", "replacement": "b", "occurrence": 0, "explanation": "c"}',
        '{"target": "\\ud800", "replacement": "b", "explanation": "c"}',
        '{"target": "a", "replacement": "\\udfff", "explanation": "c"}',
    ],
)
def test_parse_suggestion_rejects_malformed_reply(raw: str) -> None:
    with pytest.raises(MalformedSuggestionError):
        parse_suggestion(raw)


# request_model_reply: httpx.post is replaced, so nothing leaves the machine.

FAKE_REQUEST = httpx.Request("POST", llm_client.MESSAGES_URL)

THINKING_BLOCK = {"type": "thinking", "thinking": "", "signature": "abc"}


def messages_response(content: object, stop_reason: str) -> httpx.Response:
    """A Messages API reply with the given content blocks and stop reason."""
    body = {
        "type": "message",
        "role": "assistant",
        "content": content,
        "stop_reason": stop_reason,
        "usage": {"input_tokens": 400, "output_tokens": 60},
    }
    return httpx.Response(200, json=body, request=FAKE_REQUEST)


def fake_post_returning(
    monkeypatch: pytest.MonkeyPatch, response: httpx.Response
) -> dict:
    """Replace httpx.post. Returns a dict that records what was sent."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    sent = {}

    def fake_post(
        url: str, json: dict, headers: dict, timeout: float
    ) -> httpx.Response:
        sent["url"] = url
        sent["json"] = json
        sent["headers"] = headers
        sent["timeout"] = timeout
        return response

    monkeypatch.setattr(httpx, "post", fake_post)
    return sent


def test_request_without_api_key_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LLMNotConfiguredError):
        request_model_reply("prompt")


def test_request_sends_messages_api_request_and_returns_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    response = messages_response(
        [{"type": "text", "text": '{"target": "x"}'}], "end_turn"
    )
    sent = fake_post_returning(monkeypatch, response)

    assert request_model_reply("the prompt") == '{"target": "x"}'
    assert sent["url"] == "https://api.anthropic.com/v1/messages"
    assert sent["headers"] == {
        "Authorization": "Bearer test-key",
        "anthropic-version": "2026-10-21",
    }
    assert sent["json"]["model"] == "claude-haiku-5-5"
    assert sent["json"]["max_tokens"] == 4096
    assert sent["json"]["messages"] == [{"role": "user", "content": "the prompt"}]
    assert sent["json"]["output_config"]["effort"] == "low"
    assert sent["json"]["output_config"]["format"]["type"] == "json_schema"
    assert "temperature" not in sent["json"]
    assert sent["timeout"] == 30.0


def test_request_uses_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
    response = messages_response([{"type": "text", "text": "{}"}], "end_turn")
    sent = fake_post_returning(monkeypatch, response)
    request_model_reply("prompt")
    assert sent["json"]["model"] == "claude-sonnet-5-5"


def test_request_skips_thinking_blocks_before_the_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = [THINKING_BLOCK, {"type": "text", "text": '{"target": "x"}'}]
    fake_post_returning(monkeypatch, messages_response(content, "end_turn"))
    assert request_model_reply("prompt") == '{"target": "x"}'


def test_request_timeout_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    def fake_post(
        url: str, json: dict, headers: dict, timeout: float
    ) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=FAKE_REQUEST)

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(LLMUnavailableError):
        request_model_reply("prompt")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(429, json={"type": "error"}, request=FAKE_REQUEST),
        httpx.Response(529, json={"type": "error"}, request=FAKE_REQUEST),
        httpx.Response(200, text="not json", request=FAKE_REQUEST),
        httpx.Response(200, json={"type": "message"}, request=FAKE_REQUEST),
        messages_response([THINKING_BLOCK], "end_turn"),
        messages_response([{"type": "text", "text": '{"targ'}], "max_tokens"),
        messages_response([THINKING_BLOCK], "max_tokens"),
        messages_response([{"type": "text", "text": "I can't help."}], "refusal"),
        messages_response(None, "end_turn"),
        messages_response('{"target": "x"}', "end_turn"),
        messages_response({"type": "text", "text": "{}"}, "end_turn"),
        messages_response(["not a block", None], "end_turn"),
    ],
)
def test_request_error_status_odd_reply_or_early_stop_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, response: httpx.Response
) -> None:
    fake_post_returning(monkeypatch, response)
    with pytest.raises(LLMUnavailableError):
        request_model_reply("prompt")
