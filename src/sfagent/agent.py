"""The agent loop.

Uses the Anthropic SDK's tool runner, which drives the request -> execute ->
feed-results cycle so this module only has to supply the tools, the prompt and
the stopping conditions. The interesting decisions here are the prompt
construction and the two guards: a turn cap, and a mode statement that tells
the model plainly whether its writes are landing.
"""

from __future__ import annotations

from typing import Any

import anthropic

from .config import AgentConfig, RunContext, RunResult
from .playbook import PLAYBOOK
from .tools import build_tools

ROLE = """
You are a sales operations agent working inside a Salesforce org. You keep the
pipeline's activity records honest and its follow-up cadence consistent, on
behalf of the account executive who owns these deals.

You work by reading the CRM and then making small, specific, well-grounded
changes to it. You are good at this because you are disciplined about three
things: you never assert a fact that is not in the records you read, you prefer
one precise action to three vague ones, and you leave deals alone when they do
not need you.
""".strip()

MODE_INSTRUCTIONS = {
    "suggest": """
This run is in SUGGEST mode. Your write tools will record what you intend but
will NOT change Salesforce - they return "PROPOSED". Work exactly as you would
if the writes were real, because a human will review and apply them.
""".strip(),
    "review": """
This run is in REVIEW mode. Each write pauses for the account executive to
approve or decline it. If a write comes back DECLINED, accept that decision:
do not retry it, do not work around it with a different tool, and move on.
""".strip(),
    "auto": """
This run is in AUTO mode. Your writes are committed to Salesforce immediately
and are visible to the whole sales team. Act accordingly: be conservative, make
only changes you can justify from the record, and prefer taking no action over
taking a speculative one.
""".strip(),
}

CLOSING_INSTRUCTIONS = """
# How to work

1. Call `review_pipeline` first to see which deals are OVERDUE or DUE.
2. For each deal that needs attention - and only those - read
   `get_opportunity_detail` and `get_engagement_history` before deciding
   anything. Do not act on a deal you have not read.
3. Take the actions the policy calls for. Batch your reads where you can:
   you may call several read tools in one turn.
4. Deals that are on track and already have a live commitment need nothing.
   Say so rather than manufacturing an action for them.

# How to finish

When you are done, write a short report for the account executive. Cover, per
deal you acted on: what the record showed, what you concluded, and what you
did about it. Then list the deals you deliberately left alone and why. Finally,
flag anything you could not resolve from the record - a missing contact, an
unknown budget owner, a decision that needs a human.

Write it as prose with a heading per deal. No preamble, no restating these
instructions. Be specific about names, dates and amounts - the reader knows
these deals and will notice vagueness.
""".strip()


def build_system_prompt(mode: str) -> list[dict[str, Any]]:
    """Assemble the system prompt as cacheable blocks.

    Role, policy and working instructions are identical on every run, so they
    go in one block with a cache breakpoint on it. Only the mode statement
    changes between runs, and it sits after the breakpoint so switching modes
    does not invalidate the cached prefix.
    """
    stable = "\n\n".join([ROLE, PLAYBOOK, CLOSING_INSTRUCTIONS])
    return [
        {"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": MODE_INSTRUCTIONS[mode]},
    ]


def run_agent(
    task: str,
    ctx: RunContext,
    config: AgentConfig,
    *,
    client: anthropic.Anthropic | None = None,
    on_turn: Any = None,
) -> RunResult:
    """Run the agent to completion against `task` and return what it did.

    `on_turn` is an optional callback invoked with each assistant message as it
    arrives, which is how the CLI streams progress without this module knowing
    anything about terminals.
    """
    client = client or anthropic.Anthropic()
    tools = build_tools(ctx)

    ctx.audit("run_started", task=task, model=config.model, effort=config.effort)

    runner = client.beta.messages.tool_runner(
        model=config.model,
        max_tokens=config.max_tokens,
        system=build_system_prompt(ctx.mode),
        thinking={"type": "adaptive"},
        output_config={"effort": config.effort},
        tools=tools,
        messages=[{"role": "user", "content": task}],
    )

    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}
    turns = 0
    summary = ""

    for message in runner:
        turns += 1
        for key in usage:
            usage[key] += getattr(message.usage, key, 0) or 0

        if on_turn is not None:
            on_turn(message)

        text = "\n".join(b.text for b in message.content if b.type == "text").strip()
        if text:
            summary = text

        if message.stop_reason == "refusal":
            detail = getattr(message, "stop_details", None)
            summary = (
                "The model declined this request"
                f"{f' ({detail.category})' if detail else ''}. Nothing was written."
            )
            break

        if turns >= config.max_turns:
            summary = (
                summary
                + f"\n\n[Run stopped at the {config.max_turns}-turn cap. "
                "Any work not covered above was not attempted.]"
            )
            ctx.audit("turn_cap_reached", turns=turns)
            break

    ctx.audit(
        "run_finished",
        turns=turns,
        proposals=len(ctx.proposals),
        declined=len(ctx.declined),
        **usage,
    )

    return RunResult(
        summary=summary,
        proposals=ctx.proposals,
        declined=ctx.declined,
        tool_calls=ctx.tool_calls,
        turns=turns,
        usage=usage,
    )


# --- prompts the CLI hands to the agent ----------------------------------

PIPELINE_TASK = (
    "Review my whole pipeline against the follow-up policy. Fix what needs "
    "fixing and leave alone what does not."
)


def opportunity_task(opportunity_id: str) -> str:
    return (
        f"Review opportunity {opportunity_id} against the follow-up policy. "
        "Read it fully before deciding anything, then take whatever action the "
        "policy calls for - including no action, if that is the right answer."
    )
