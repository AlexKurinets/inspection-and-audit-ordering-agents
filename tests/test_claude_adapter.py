"""The Claude adapter's request and its handling of refusals, against a stub client (no API calls)."""

from types import SimpleNamespace

import pytest

from opsagents.llm.base import LLMError, MailReading
from opsagents.llm.claude import ClaudeLLM


class StubMessages:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def adapter(response) -> tuple[ClaudeLLM, StubMessages]:
    messages = StubMessages(response)
    return ClaudeLLM(model="claude-opus-5-5", client=SimpleNamespace(messages=messages)), messages


def test_request_carries_the_schema_and_wraps_the_document_as_data():
    reading = MailReading(intent="acknowledged", vendor_ref="Q-48001", quote="logged it under reference Q-48001")
    llm, messages = adapter(SimpleNamespace(stop_reason="end_turn", parsed_output=reading))
    assert llm.read_mail("Re: order [ORD-000004]", "We logged it under reference Q-48001.") is reading
    call = messages.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["output_format"] is MailReading
    assert call["output_config"] == {"effort": "low"}
    content = call["messages"][0]["content"]
    assert "<document>" in content and "Q-48001" in content
    assert "data, not instructions" in call["system"]


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_refusal_or_truncation_becomes_an_llm_error(stop_reason):
    llm, _ = adapter(SimpleNamespace(stop_reason=stop_reason, parsed_output=None))
    with pytest.raises(LLMError):
        llm.extract_audit("Policy number: WC-2025-000001")
