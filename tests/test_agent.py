"""Agent-loop tests.

These run against a stub client rather than the API: they check the wiring this
repo owns - prompt assembly, turn accounting, usage totals, the turn cap and the
refusal path - not the model's judgement. Nothing here spends money or needs a
key, which is what lets them run in CI.
"""

from __future__ import annotations

from types import SimpleNamespace

from sfagent.agent import build_system_prompt, opportunity_task, run_agent
from sfagent.config import AgentConfig, RunContext

from conftest import NORTHWIND


# --- stub client ----------------------------------------------------------


def block(type_: str, **fields):
    return SimpleNamespace(type=type_, **fields)


def message(text: str = "", *, stop_reason: str = "end_turn", tool_uses=(), **usage):
    content = [block("tool_use", name=n, input=i) for n, i in tool_uses]
    if text:
        content.append(block("text", text=text))
    totals = {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 0}
    totals.update(usage)
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        stop_details=None,
        usage=SimpleNamespace(**totals),
    )


class StubClient:
    """Stands in for `anthropic.Anthropic`, replaying a fixed message sequence."""

    def __init__(self, messages):
        self.messages = messages
        self.calls: list[dict] = []
        client = self

        class _Messages:
            def tool_runner(self, **kwargs):
                client.calls.append(kwargs)
                return iter(client.messages)

        self.beta = SimpleNamespace(messages=_Messages())


# --- prompt ---------------------------------------------------------------


def test_system_prompt_puts_the_cache_breakpoint_after_the_stable_half():
    prompt = build_system_prompt("suggest")
    assert len(prompt) == 2
    assert prompt[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in prompt[1], (
        "the mode statement varies per run and must sit after the breakpoint"
    )


def test_the_stable_half_is_identical_across_modes():
    """Switching modes must not invalidate the cached prefix."""
    assert build_system_prompt("suggest")[0] == build_system_prompt("auto")[0]


def test_each_mode_tells_the_model_whether_its_writes_land():
    assert "will NOT change Salesforce" in build_system_prompt("suggest")[1]["text"]
    assert "committed to Salesforce immediately" in build_system_prompt("auto")[1]["text"]
    assert "do not retry it" in build_system_prompt("review")[1]["text"]


def test_the_policy_reaches_the_model():
    stable = build_system_prompt("suggest")[0]["text"]
    assert "Cadence SLAs" in stable
    assert "Negotiation/Review: 3 days" in stable


# --- the loop -------------------------------------------------------------


def test_run_returns_the_last_text_and_totals_usage(suggest_ctx):
    client = StubClient(
        [
            message("Reading the pipeline.", stop_reason="tool_use", input_tokens=1000),
            message("## Northwind\nChased the redlines.", input_tokens=200, output_tokens=300),
        ]
    )
    result = run_agent("go", suggest_ctx, AgentConfig(), client=client)

    assert result.summary.startswith("## Northwind")
    assert result.turns == 2
    assert result.usage["input_tokens"] == 1200
    assert result.usage["output_tokens"] == 350


def test_the_request_carries_the_tools_prompt_and_effort(suggest_ctx):
    client = StubClient([message("done")])
    run_agent("go", suggest_ctx, AgentConfig(effort="medium"), client=client)

    sent = client.calls[0]
    assert sent["model"] == "claude-opus-5"
    assert sent["output_config"] == {"effort": "medium"}
    assert sent["thinking"] == {"type": "adaptive"}
    assert {t.name for t in sent["tools"]} == {
        "review_pipeline",
        "get_opportunity_detail",
        "get_engagement_history",
        "log_activity",
        "schedule_followup",
        "set_next_step",
    }
    assert sent["messages"] == [{"role": "user", "content": "go"}]


def test_the_turn_cap_stops_the_loop_and_says_so(suggest_ctx):
    client = StubClient([message("working", stop_reason="tool_use") for _ in range(50)])
    result = run_agent("go", suggest_ctx, AgentConfig(max_turns=3), client=client)

    assert result.turns == 3
    assert "3-turn cap" in result.summary
    assert any(c["event"] == "turn_cap_reached" for c in result.tool_calls)


def test_a_refusal_ends_the_run_without_claiming_work_was_done(suggest_ctx):
    client = StubClient([message("", stop_reason="refusal")])
    result = run_agent("go", suggest_ctx, AgentConfig(), client=client)

    assert "declined" in result.summary
    assert "Nothing was written" in result.summary


def test_the_run_is_bracketed_in_the_audit_trail(crm, tmp_path):
    ctx = RunContext(crm, mode="suggest", run_dir=tmp_path / "run")
    run_agent("go", ctx, AgentConfig(), client=StubClient([message("done")]))

    events = [line.split('"event": "')[1].split('"')[0] for line in
              (tmp_path / "run" / "audit.jsonl").read_text().strip().splitlines()]
    assert events[0] == "run_started"
    assert events[-1] == "run_finished"


def test_on_turn_sees_every_message(suggest_ctx):
    seen = []
    client = StubClient([message("a", stop_reason="tool_use"), message("b")])
    run_agent("go", suggest_ctx, AgentConfig(), client=client, on_turn=seen.append)
    assert len(seen) == 2


def test_single_opportunity_task_names_the_record(suggest_ctx):
    assert NORTHWIND in opportunity_task(NORTHWIND)
