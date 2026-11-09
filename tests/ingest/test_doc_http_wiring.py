"""HttpDocExtractionClient wiring, tested WITHOUT a key through FakeAnthropicTransport (the real wire format).

Verifies request construction, response parsing, the explicit no-tool-use failure and the error paths. It says nothing
about whether a real model extracts well: the report it serves is whatever the test queues. Doc ingestion's WIRING is
also tested unconditionally against StubDocExtractionClient; real model judgment remains opt-in and unverified.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from fake_anthropic import FakeAnthropicTransport, error_body, text_response, tool_use_message
from ingest.anthropic_http import AnthropicAPIError, AnthropicHTTP
from ingest.doc import (STRICT_SUFFIX, SYSTEM_PROMPT, TOOL_NAME, TOOL_SCHEMA, DocNoToolCall, HttpDocExtractionClient,
                        IngestError, ingest)
from ingest.testing import StubDocExtractionClient

FIX = Path(__file__).parent / "fixtures"
KEY = "sk-ant-test-not-a-real-key"
TEXT = (FIX / "dogceo.doc.md").read_text(encoding="utf-8")
REPORT = json.loads((FIX / "dogceo.stub_response.json").read_text(encoding="utf-8"))


def http(*responses):
    transport = FakeAnthropicTransport(*responses)
    return HttpDocExtractionClient(AnthropicHTTP(KEY, transport=transport), model="m-under-test"), transport


def test_request_carries_the_document_the_extraction_tool_and_nothing_secret():
    client, fake = http((200, tool_use_message(TOOL_NAME, REPORT)))
    client.extract(TEXT)
    [req] = fake.requests
    body = fake.bodies[0]
    assert (req.method, req.url.path) == ("POST", "/v1/messages")
    assert req.headers["x-api-key"] == KEY and req.headers["anthropic-version"] == "2023-06-01"
    assert body["model"] == "m-under-test"
    assert body["system"] == SYSTEM_PROMPT
    assert body["tools"] == [TOOL_SCHEMA], "exactly the extraction tool, with its exact schema"
    assert body["tool_choice"] == {"type": "auto"}, "a forced tool_choice is rejected by the current default model (D16)"
    [msg] = body["messages"]
    assert msg["role"] == "user" and TEXT in msg["content"], "the documentation text goes to the model"
    assert KEY not in req.content.decode(), "the key travels in a header only"


def test_a_valid_tool_use_reply_is_returned_as_the_models_report_and_flows_through_ingest():
    client, _ = http((200, tool_use_message(TOOL_NAME, REPORT)))
    assert client.extract(TEXT) == REPORT
    client, _ = http((200, tool_use_message(TOOL_NAME, REPORT)))
    spec = ingest(TEXT, client=client)
    assert [o["name"] for o in spec["operations"]] == [o["name"] for o in REPORT["operations"]]


def test_the_http_path_and_the_stub_path_produce_identical_operations_from_the_same_report():
    """Same report, two clients: everything downstream of the client must be byte-identical (wiring equivalence)."""
    via_http, _ = http((200, tool_use_message(TOOL_NAME, REPORT)))
    assert ingest(TEXT, client=via_http) == ingest(TEXT, client=StubDocExtractionClient(REPORT))


def test_a_reply_without_the_tool_call_raises_doc_no_tool_call_after_one_request():
    client, fake = http((200, text_response("Sure! Here is a summary of the docs...")))
    with pytest.raises(DocNoToolCall, match=r"DOC_NO_TOOL_CALL.*stop_reason=end_turn"):
        client.extract(TEXT)
    assert len(fake.requests) == 1, "no silent retry inside the client: the pipeline retries once, visibly"


def test_strict_mode_adds_the_stricter_prompt_and_is_still_auto():
    client, fake = http((200, tool_use_message(TOOL_NAME, REPORT)))
    client.extract(TEXT, strict=True)
    body = fake.bodies[0]
    assert body["system"] == SYSTEM_PROMPT + STRICT_SUFFIX and body["tool_choice"] == {"type": "auto"}


def test_the_pipelines_single_strict_retry_works_over_the_real_wire_format():
    """pipeline.run_ingestor: first reply has no tool call -> ONE retry with strict=True -> success."""
    from pipeline import run_ingestor
    client, fake = http((200, text_response("Let me think...")), (200, tool_use_message(TOOL_NAME, REPORT)))
    notes: list[str] = []
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        doc = Path(d) / "dogceo.md"
        doc.write_text(TEXT, encoding="utf-8")
        spec = run_ingestor("doc", doc, client, notes)
    assert spec["source"] == "doc-unverified" and len(fake.requests) == 2
    assert fake.bodies[1]["system"].endswith(STRICT_SUFFIX) and not fake.bodies[0]["system"].endswith(STRICT_SUFFIX)
    assert any("DOC_NO_TOOL_CALL" in n for n in notes), "the retry is reported, not hidden"


def test_api_errors_are_wrapped_not_retried_and_leak_no_key():
    client, fake = http((529, error_body("overloaded_error", "Overloaded")))
    with pytest.raises(IngestError, match="Claude API call failed: AnthropicAPIError: HTTP 529 overloaded_error") as exc:
        client.extract(TEXT)
    assert len(fake.requests) == 1 and KEY not in str(exc.value)


def test_a_tool_input_that_is_not_an_object_is_refused():
    client, _ = http((200, tool_use_message(TOOL_NAME, "not an object")))  # type: ignore[arg-type]
    with pytest.raises(IngestError, match="not a JSON object"):
        client.extract(TEXT)


AUTH_TOKEN_MESSAGE = "ANTHROPIC_AUTH_TOKEN is set but no longer supported; set ANTHROPIC_API_KEY."


def test_auth_token_without_an_api_key_is_a_loud_error_naming_the_fix_at_startup(monkeypatch):
    """The removed SDK fallback must not fail silently: no request is made, and the message says what to set."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-that-used-to-work")
    with pytest.raises(AnthropicAPIError) as exc:
        AnthropicHTTP()
    assert AUTH_TOKEN_MESSAGE in str(exc.value) and "tok-that-used-to-work" not in str(exc.value)
    with pytest.raises(IngestError) as via_doc:
        HttpDocExtractionClient()
    assert AUTH_TOKEN_MESSAGE in str(via_doc.value)
    with pytest.raises(IngestError, match="ANTHROPIC_AUTH_TOKEN is set but no longer supported"):
        ingest(TEXT)  # the pipeline's default path (no client given) fails the same way, before any network call


def test_an_api_key_wins_when_both_variables_are_set_and_neither_variable_is_ever_sent_as_a_bearer(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    fake = FakeAnthropicTransport((200, tool_use_message(TOOL_NAME, REPORT)))
    HttpDocExtractionClient(AnthropicHTTP(transport=fake)).extract(TEXT)  # key read from the environment
    [req] = fake.requests
    assert req.headers["x-api-key"] == KEY and "authorization" not in req.headers


def test_without_a_key_the_default_client_fails_loudly_and_never_calls_out(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with pytest.raises(IngestError, match="ANTHROPIC_API_KEY is not set"):
        ingest(TEXT)  # no client given: builds HttpDocExtractionClient() from the environment
    with pytest.raises(AnthropicAPIError, match="no_api_key"):
        AnthropicHTTP()
