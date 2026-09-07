"""End-to-end against the real Anthropic SDK, with only the HTTP layer mocked.

`test_agent.py` stubs `client.beta.messages.tool_runner` itself with a
`SimpleNamespace` sequence, which tests this repo's own logic but never
exercises the SDK boundary: real Pydantic response parsing, real dict-shaped
tool dispatch through Pydantic validation, and real request/response wire
shapes. Those are exactly the places a plausible-looking assumption can be
wrong without a stubbed test ever catching it - so this file drives the actual
`tool_runner` against `httpx2.MockTransport`, which returns literal Messages
API JSON. No network call, no API key, no cost - and no gap between what is
tested and what the SDK actually does with our tools.
"""

from __future__ import annotations

import json

import anthropic
import httpx2
import pytest
from anthropic import DefaultHttpxClient

from sfagent.agent import PIPELINE_TASK, run_agent
from sfagent.config import AgentConfig, RunContext

from conftest import NORTHWIND

FAKE_KEY = "sk-ant-fake-for-transport-mock-tests"


def scripted_client(turns: list[dict]) -> anthropic.Anthropic:
    """An Anthropic client whose transport replays `turns` (one dict per API
    call) as literal Messages API response bodies, in order."""
    calls: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(json.loads(request.content))
        payload = turns[len(calls) - 1]
        return httpx2.Response(200, json=payload, request=request)

    client = anthropic.Anthropic(
        api_key=FAKE_KEY, http_client=DefaultHttpxClient(transport=httpx2.MockTransport(handler))
    )
    client._test_calls = calls  # type: ignore[attr-defined]
    return client


def usage(**over):
    base = {"input_tokens": 100, "output_tokens": 50, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    base.update(over)
    return base


def tool_use_turn(msg_id: str, name: str, input_: dict, tool_id: str, **usage_over):
    return {
        "id": msg_id, "type": "message", "role": "assistant", "model": "claude-opus-5",
        "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": input_}],
        "stop_reason": "tool_use", "stop_sequence": None, "usage": usage(**usage_over),
    }


def text_turn(msg_id: str, text: str, **usage_over):
    return {
        "id": msg_id, "type": "message", "role": "assistant", "model": "claude-opus-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn", "stop_sequence": None, "usage": usage(**usage_over),
    }


# --- the real dispatch path ------------------------------------------------


def test_the_real_tool_runner_dispatches_a_write_and_feeds_back_a_matching_result(auto_ctx):
    client = scripted_client(
        [
            tool_use_turn(
                "msg_1", "schedule_followup",
                {
                    "opportunity_id": NORTHWIND,
                    "subject": "Call Marit re: overdue MSA redlines",
                    "description": "Redlines were promised on the 10th and have not arrived.",
                    "due_in_days": 0,
                    "contact_id": "0035g00000PqR01AAF",
                    "priority": "High",
                },
                tool_id="toolu_1",
            ),
            text_turn("msg_2", "Scheduled a call with Marit about the overdue redlines."),
        ]
    )

    before = len(auto_ctx.crm.tasks)
    result = run_agent(PIPELINE_TASK, auto_ctx, AgentConfig(), client=client)

    assert result.turns == 2
    assert len(auto_ctx.crm.tasks) == before + 1, "the write must actually land in the CRM"
    assert result.proposals[0].committed is True

    second_request = client._test_calls[1]  # type: ignore[attr-defined]
    tool_result = second_request["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == "toolu_1", "result must reference the exact tool_use id"
    assert "COMMITTED" in tool_result["content"]


def test_conversation_history_accumulates_correctly_across_three_real_turns(suggest_ctx):
    client = scripted_client(
        [
            tool_use_turn("msg_1", "review_pipeline", {}, tool_id="toolu_1"),
            tool_use_turn(
                "msg_2", "get_opportunity_detail", {"opportunity_id": NORTHWIND}, tool_id="toolu_2"
            ),
            text_turn("msg_3", "## Northwind\nOn track, no action needed."),
        ]
    )
    run_agent(PIPELINE_TASK, suggest_ctx, AgentConfig(), client=client)

    third_request = client._test_calls[2]  # type: ignore[attr-defined]
    ids = [
        b["tool_use_id"]
        for m in third_request["messages"]
        if m["role"] == "user" and isinstance(m["content"], list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ]
    assert ids == ["toolu_1", "toolu_2"], "both prior tool results must carry forward, in order"


def test_a_malformed_tool_call_becomes_an_error_result_instead_of_crashing_the_run(suggest_ctx):
    """The model can send bad arguments (missing a required field, an
    unexpected extra one). The SDK validates via Pydantic and is documented to
    turn the resulting exception into an `is_error` tool result rather than
    propagating it - this pins that our tools behave correctly under that
    contract, using a real request/response round trip rather than trusting
    the docs alone."""
    client = scripted_client(
        [
            tool_use_turn(
                "msg_1", "schedule_followup",
                {"opportunity_id": NORTHWIND, "subject": "x"},  # missing required fields
                tool_id="toolu_1",
            ),
            text_turn("msg_2", "Correcting my call."),
        ]
    )
    result = run_agent(PIPELINE_TASK, suggest_ctx, AgentConfig(), client=client)

    second_request = client._test_calls[1]  # type: ignore[attr-defined]
    tool_result = second_request["messages"][-1]["content"][0]
    assert tool_result["is_error"] is True
    assert result.turns == 2, "the run must continue past the bad call, not crash"


def test_request_shape_matches_what_the_agent_module_promises(suggest_ctx):
    client = scripted_client([text_turn("msg_1", "done")])
    run_agent(PIPELINE_TASK, suggest_ctx, AgentConfig(effort="medium"), client=client)

    sent = client._test_calls[0]  # type: ignore[attr-defined]
    assert sent["model"] == "claude-opus-5"
    assert sent["thinking"] == {"type": "adaptive"}
    assert sent["output_config"] == {"effort": "medium"}
    assert {t["name"] for t in sent["tools"]} == {
        "review_pipeline", "get_opportunity_detail", "get_engagement_history",
        "log_activity", "schedule_followup", "set_next_step",
    }
    assert "cache_control" in sent["system"][0], "the stable half of the prompt must be cached"
    assert "cache_control" not in sent["system"][1], "the mode statement must sit after the breakpoint"


def test_a_real_refusal_response_is_read_correctly(suggest_ctx):
    """`stop_details` only appears on a refusal and has its own shape
    (`category`, `explanation`) - confirmed against a literal API response
    rather than a stand-in object."""
    client = scripted_client(
        [
            {
                "id": "msg_refusal", "type": "message", "role": "assistant", "model": "claude-opus-5",
                "content": [], "stop_reason": "refusal", "stop_sequence": None,
                "stop_details": {"type": "refusal", "category": "cyber", "explanation": "test"},
                "usage": usage(),
            }
        ]
    )
    result = run_agent(PIPELINE_TASK, suggest_ctx, AgentConfig(), client=client)
    assert "cyber" in result.summary
    assert "Nothing was written" in result.summary
    assert result.turns == 1
