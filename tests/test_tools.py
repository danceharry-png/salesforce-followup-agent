"""The tool layer is where the mode guarantee lives. If `suggest` mode can
write to the CRM, the whole design is unsound - so that is tested first."""

from __future__ import annotations

from datetime import date, timedelta

from sfagent.config import RunContext
from sfagent.crm.models import ProposedAction

from conftest import CALDERWOOD, MARIT, NORTHWIND, TESSELLATE, tools_for


# --- the mode guarantee ---------------------------------------------------


def test_suggest_mode_records_a_proposal_without_touching_the_crm(suggest_ctx):
    crm = suggest_ctx.crm
    tools = tools_for(suggest_ctx)
    before = len(crm.tasks)

    result = tools["schedule_followup"](
        opportunity_id=NORTHWIND,
        subject="Call Marit re: overdue MSA redlines",
        description="Redlines were promised 12 days ago.",
        due_in_days=0,
        contact_id=MARIT,
        priority="High",
    )

    assert "PROPOSED" in result
    assert len(crm.tasks) == before, "suggest mode must not write"
    assert len(suggest_ctx.proposals) == 1
    assert suggest_ctx.proposals[0].committed is False


def test_auto_mode_commits_and_reports_the_record_id(auto_ctx):
    crm = auto_ctx.crm
    tools = tools_for(auto_ctx)
    before = len(crm.tasks)

    result = tools["schedule_followup"](
        opportunity_id=NORTHWIND,
        subject="Call Marit re: overdue MSA redlines",
        description="Redlines were promised 12 days ago.",
        due_in_days=1,
        contact_id=MARIT,
    )

    assert "COMMITTED" in result
    assert len(crm.tasks) == before + 1
    action = auto_ctx.proposals[0]
    assert action.committed is True
    assert action.record_id and action.record_id.startswith("00T")


def test_declining_in_review_mode_leaves_the_crm_untouched(crm):
    ctx = RunContext(crm, mode="review", approve=lambda action: False)
    tools = tools_for(ctx)
    before = len(crm.tasks)

    result = tools["schedule_followup"](
        opportunity_id=NORTHWIND,
        subject="Call Marit",
        description="...",
        due_in_days=1,
    )

    assert "DECLINED" in result
    assert "Do not retry" in result, "the model needs to be told not to loop on this"
    assert len(crm.tasks) == before
    assert ctx.declined and not ctx.proposals


def test_approving_in_review_mode_commits(crm):
    seen: list[ProposedAction] = []

    def approve(action: ProposedAction) -> bool:
        seen.append(action)
        return True

    ctx = RunContext(crm, mode="review", approve=approve)
    tools = tools_for(ctx)

    result = tools["set_next_step"](
        opportunity_id=CALDERWOOD,
        next_step="Send 2-page business case to Alicia Moreno by Friday",
        rationale="NextStep was empty and the economic buyer is disengaged.",
    )

    assert "COMMITTED" in result
    assert seen and seen[0].action == "set_next_step"
    assert crm.get_opportunity(CALDERWOOD)["NextStep"].startswith("Send 2-page")


# --- due-date policy ------------------------------------------------------


def test_weekend_due_dates_move_to_monday(auto_ctx):
    tools = tools_for(auto_ctx)
    days_to_saturday = (5 - auto_ctx.crm.today.weekday()) % 7

    tools["schedule_followup"](
        opportunity_id=NORTHWIND,
        subject="Call Marit",
        description="...",
        due_in_days=days_to_saturday,
    )

    due = date.fromisoformat(auto_ctx.proposals[0].payload["ActivityDate"])
    assert due.weekday() < 5, f"{due} landed on a weekend"


def test_negative_due_in_days_is_clamped_to_today(auto_ctx):
    tools = tools_for(auto_ctx)
    tools["schedule_followup"](
        opportunity_id=NORTHWIND, subject="Call Marit", description="...", due_in_days=-5
    )
    due = date.fromisoformat(auto_ctx.proposals[0].payload["ActivityDate"])
    assert due >= auto_ctx.crm.today


# --- validation -----------------------------------------------------------


def test_writes_against_an_unknown_opportunity_return_an_error_not_an_exception(auto_ctx):
    """A raised exception ends the run; an error string lets the model recover."""
    tools = tools_for(auto_ctx)
    for name, kwargs in [
        ("schedule_followup", dict(subject="x", description="y", due_in_days=1)),
        ("log_activity", dict(subject="x", description="y")),
        ("set_next_step", dict(next_step="x")),
    ]:
        result = tools[name](opportunity_id="006NOPE", **kwargs)
        assert result.startswith("Error:")
    assert not auto_ctx.proposals


def test_invalid_enum_values_are_rejected(auto_ctx):
    tools = tools_for(auto_ctx)
    assert tools["log_activity"](
        opportunity_id=NORTHWIND, subject="x", description="y", activity_type="Telepathy"
    ).startswith("Error:")
    assert tools["schedule_followup"](
        opportunity_id=NORTHWIND, subject="x", description="y", due_in_days=1, priority="Urgent"
    ).startswith("Error:")
    assert tools["set_next_step"](opportunity_id=NORTHWIND, next_step="   ").startswith("Error:")
    assert not auto_ctx.proposals


def test_a_contact_from_the_wrong_account_is_still_validated(auto_ctx):
    tools = tools_for(auto_ctx)
    assert tools["log_activity"](
        opportunity_id=NORTHWIND, subject="x", description="y", contact_id="003NOPE"
    ).startswith("Error:")


# --- reads ----------------------------------------------------------------


def test_review_pipeline_marks_cadence_and_hides_closed_deals(suggest_ctx):
    out = tools_for(suggest_ctx)["review_pipeline"]()
    assert "OVERDUE" in out
    assert "Arcadia Financial" not in out, "closed deals should never appear"


def test_review_pipeline_can_filter_to_deals_needing_attention(suggest_ctx):
    everything = tools_for(suggest_ctx)["review_pipeline"](include_on_track=True)
    needs_work = tools_for(suggest_ctx)["review_pipeline"](include_on_track=False)
    assert len(needs_work) < len(everything)
    assert "ON_TRACK" not in needs_work


def test_opportunity_detail_surfaces_empty_next_step_as_a_defect(suggest_ctx):
    out = tools_for(suggest_ctx)["get_opportunity_detail"](opportunity_id=CALDERWOOD)
    assert "EMPTY - this is a defect to fix" in out
    assert "Dr. Alicia Moreno" in out and "Economic Buyer" in out


def test_opportunity_detail_lists_already_scheduled_work(suggest_ctx):
    """Without this the agent has no way to avoid duplicating an open task."""
    out = tools_for(suggest_ctx)["get_opportunity_detail"](opportunity_id=TESSELLATE)
    assert "OPEN TASKS (1)" in out
    assert "security questionnaire" in out


def test_engagement_history_reports_direction_and_open_state(suggest_ctx):
    out = tools_for(suggest_ctx)["get_engagement_history"](opportunity_id=CALDERWOOD, limit=30)
    assert "[email/inbound]" in out
    assert "unopened" in out


def test_reads_are_audited(suggest_ctx):
    tools_for(suggest_ctx)["review_pipeline"]()
    assert any(c["event"] == "read" for c in suggest_ctx.tool_calls)


def test_the_audit_file_is_written_when_a_run_dir_is_given(crm, tmp_path):
    ctx = RunContext(crm, mode="auto", run_dir=tmp_path / "run")
    tools_for(ctx)["set_next_step"](opportunity_id=CALDERWOOD, next_step="Book exec review")

    lines = (tmp_path / "run" / "audit.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    assert '"event": "committed"' in lines[0]


def test_logged_activity_immediately_changes_the_cadence_picture(auto_ctx):
    """End to end through the tool layer: logging a touch should move the deal
    off OVERDUE, which is the behaviour the whole tool surface exists for."""
    tools = tools_for(auto_ctx)
    assert "OVERDUE" in tools["review_pipeline"](stage="Qualification")

    tools["log_activity"](
        opportunity_id="0065g00000XyZ05AAF",
        subject="Call - asked Denise to name the budget owner",
        description="Grounded in the two unanswered emails already on the record.",
        activity_type="Call",
        occurred_days_ago=0,
    )
    assert "OVERDUE" not in tools["review_pipeline"](stage="Qualification")
