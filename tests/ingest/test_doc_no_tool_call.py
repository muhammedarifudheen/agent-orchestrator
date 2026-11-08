"""doc.py calls the model with tool_choice "auto", once. A reply without the tool call is an EXPLICIT,
recoverable failure (DocNoToolCall, code DOC_NO_TOOL_CALL, recovery=agent), never a crash and never a silent
fallback. The pipeline retries exactly once with a stricter prompt and reports that it did.

Deviation from the request, on purpose: doc.py is an ingestor, not an MCP tool, so the error is an IngestError
subclass carrying the same code/retryable/recovery fields and the same text format as ToolFailure, instead of
importing the MCP tool error type into the ingest package."""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from ingest import doc
from ingest.doc import DocNoToolCall, IngestError, TOOL_NAME, ingest
from ingest.testing import StubAnthropic
from pipeline import process
from pipeline_helpers import layout_in

FIX = Path(__file__).parent / "fixtures"
DOGCEO = (FIX / "dogceo.doc.md").read_text(encoding="utf-8")
LOWCONF_DOC = (FIX / "lowconf.doc.md").read_text(encoding="utf-8")


def stub(name="dogceo", **kw):
    return StubAnthropic(str(FIX / f"{name}.stub_response.json"), **kw)


def test_text_only_reply_raises_doc_no_tool_call_not_a_crash():
    client = stub(text_first=1)
    with pytest.raises(DocNoToolCall) as excinfo:
        ingest(DOGCEO, client=client)
    err = excinfo.value
    assert isinstance(err, IngestError)  # callers that handle IngestError still see it
    assert (err.code, err.retryable, err.recovery) == ("DOC_NO_TOOL_CALL", True, "agent")
    assert str(err).startswith("[DOC_NO_TOOL_CALL] retryable=true recovery=agent ")
    assert "stop_reason=end_turn" in str(err) and "Sure!" in str(err)  # says what the model did instead


def test_empty_reply_and_wrong_tool_are_also_doc_no_tool_call():
    class Empty:
        class messages:
            @staticmethod
            def create(**kw):
                from types import SimpleNamespace as NS
                return NS(content=[], stop_reason="end_turn")

    class WrongTool:
        class messages:
            @staticmethod
            def create(**kw):
                from types import SimpleNamespace as NS
                return NS(content=[NS(type="tool_use", name="some_other_tool", input={})], stop_reason="tool_use")

    for client in (Empty(), WrongTool()):
        with pytest.raises(DocNoToolCall):
            ingest(DOGCEO, client=client)


def test_auto_first_exactly_one_call_and_no_forced_choice_anywhere():
    client = stub()
    ingest(DOGCEO, client=client)
    assert len(client.calls) == 1 and client.calls[0]["tool_choice"] == {"type": "auto"}
    source = inspect.getsource(doc)
    assert '"type": "tool"' not in source, "a forced tool_choice crept back into doc.py"
    assert "forced" not in source.split("def _call_model")[1].split("# -- deterministic")[0].lower().replace("no forced choice", "")


def test_api_error_is_an_ingest_error_with_a_single_attempt_and_no_fallback():
    class Broken:
        calls = 0

        class messages:
            @staticmethod
            def create(**kw):
                Broken.calls += 1
                raise RuntimeError("400 invalid_request_error: tool_choice is not supported")  # mentions tool_choice on purpose

    with pytest.raises(IngestError) as excinfo:
        ingest(DOGCEO, client=Broken())
    assert not isinstance(excinfo.value, DocNoToolCall)
    assert "Claude API call failed" in str(excinfo.value)
    assert Broken.calls == 1, "doc.py must not retry or fall back on an API error"


def test_strict_prompt_is_stricter_and_still_auto():
    client = stub()
    ingest(DOGCEO, client=client, strict=True)
    call = client.calls[0]
    assert call["tool_choice"] == {"type": "auto"}
    assert "did not call the tool" in call["system"] and TOOL_NAME in call["system"]
    plain = stub()
    ingest(DOGCEO, client=plain)
    assert "did not call the tool" not in plain.calls[0]["system"]


# -- the pipeline's single, visible retry ----------------------------------------------------------

def test_pipeline_retries_once_with_a_stricter_prompt_and_says_so(tmp_path):
    layout = layout_in(tmp_path)
    source = layout.inbox / "lowconf.doc.md"
    source.write_text(LOWCONF_DOC, encoding="utf-8")
    client = stub("lowconf", text_first=1)  # first reply forgets the tool, the retry works

    outcome = process(source, layout, doc_client=client)

    assert len(client.calls) == 2
    assert "did not call the tool" not in client.calls[0]["system"] and "did not call the tool" in client.calls[1]["system"]
    assert outcome.status == "review"  # the lowconf fixture is held for review (the point here is the retry worked)
    assert any("RETRIED once" in w and "DOC_NO_TOOL_CALL" in w for w in outcome.warnings), outcome.warnings


def test_pipeline_fails_loudly_when_the_retry_also_gets_no_tool_call(tmp_path):
    layout = layout_in(tmp_path)
    source = layout.inbox / "dogceo.doc.md"
    source.write_text(DOGCEO, encoding="utf-8")
    client = stub(text_first=2)

    outcome = process(source, layout, doc_client=client)

    assert len(client.calls) == 2, "exactly one retry, never a loop"
    assert outcome.status == "failed" and "DOC_NO_TOOL_CALL" in outcome.detail
    assert any("RETRIED once" in w for w in outcome.warnings)
    assert not any(layout.servers.iterdir()) and not any(layout.specs.iterdir())
