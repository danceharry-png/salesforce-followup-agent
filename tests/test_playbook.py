"""The cadence policy is the part a sales manager is most likely to change, so
its edges are pinned explicitly."""

from __future__ import annotations

import pytest

from sfagent.playbook import (
    CLOSING_WINDOW_DAYS,
    CLOSING_WINDOW_SLA,
    DEFAULT_SLA,
    PLAYBOOK,
    STAGE_SLA,
    cadence_status,
    sla_for,
)


def test_later_stages_have_tighter_slas():
    assert STAGE_SLA["Negotiation/Review"] < STAGE_SLA["Proposal/Price Quote"]
    assert STAGE_SLA["Proposal/Price Quote"] < STAGE_SLA["Qualification"]


def test_unknown_stage_falls_back_to_the_default():
    assert sla_for("Some Custom Stage") == DEFAULT_SLA


def test_closing_window_tightens_the_sla_regardless_of_stage():
    assert sla_for("Qualification", days_to_close=CLOSING_WINDOW_DAYS - 1) == CLOSING_WINDOW_SLA
    assert sla_for("Qualification", days_to_close=CLOSING_WINDOW_DAYS + 1) == STAGE_SLA["Qualification"]


def test_closing_window_never_loosens_an_already_tighter_sla():
    """Negotiation is a 3-day SLA; the window is 2. The window must win, but a
    hypothetical stage tighter than the window must not be relaxed by it."""
    assert sla_for("Negotiation/Review", days_to_close=5) == CLOSING_WINDOW_SLA
    assert sla_for("Negotiation/Review", days_to_close=5) <= STAGE_SLA["Negotiation/Review"]


def test_past_close_dates_do_not_trigger_the_window():
    """A negative days_to_close means the close date has already slipped past.
    That is a different problem, and it should not silently retune the SLA."""
    assert sla_for("Qualification", days_to_close=-3) == STAGE_SLA["Qualification"]


@pytest.mark.parametrize(
    "days_silent,expected",
    [(0, "on_track"), (2, "on_track"), (3, "due"), (4, "overdue")],
)
def test_cadence_status_boundaries(days_silent, expected):
    assert cadence_status("Negotiation/Review", days_silent, 40) == expected


def test_a_deal_with_no_activity_at_all_is_overdue():
    assert cadence_status("Qualification", None, 30) == "overdue"


def test_playbook_prompt_renders_every_stage_sla():
    """The prompt and the code must not drift: if a stage is added to the table
    it has to appear in the text the model reads."""
    for stage, days in STAGE_SLA.items():
        assert f"{stage}: {days} days" in PLAYBOOK
