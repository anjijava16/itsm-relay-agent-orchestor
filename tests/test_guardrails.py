import pytest

from app.agents.nodes.guardrails import output_guardrail, redact


def test_redacts_api_keys():
    text = "here is my key sk-abcdefghijklmnopqrstuvwxyz123456"
    assert "sk-abcdefghij" not in redact(text)


def test_redacts_inline_password():
    assert "hunter2" not in redact("password: hunter2")


def test_leaves_normal_text_alone():
    text = "The VPN drops every ten minutes."
    assert redact(text) == text


@pytest.mark.asyncio
async def test_output_guardrail_flags_ungrounded():
    state = {"answer": "Just restart it.", "citations": [], "resolution_path": "kb_resolution",
             "risk_flags": []}
    result = await output_guardrail(state)
    assert "no_grounding" in result["risk_flags"]
