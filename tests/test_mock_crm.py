"""The fixture org has to behave enough like Salesforce for the agent's
reasoning to transfer. These tests pin the parts it actually relies on."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from conftest import ARCADIA_CLOSED, CALDERWOOD, MARIT, NORTHWIND, TESSELLATE, TODAY


def test_relative_dates_are_materialised_against_today(crm):
    opp = crm.get_opportunity(NORTHWIND)
    assert opp["CloseDate"] == (TODAY + timedelta(days=8)).isoformat()
    assert opp["DaysToClose"] == 8


def test_closed_opportunities_are_excluded_from_the_open_pipeline(crm):
    open_ids = {o["Id"] for o in crm.list_opportunities(open_only=True)}
    assert ARCADIA_CLOSED not in open_ids
    assert NORTHWIND in open_ids

    all_ids = {o["Id"] for o in crm.list_opportunities(open_only=False)}
    assert ARCADIA_CLOSED in all_ids


def test_last_activity_date_is_derived_not_stored(crm, seed):
    """Salesforce computes LastActivityDate from related activities. So do we,
    which means logging an activity has to move it."""
    before = crm.get_opportunity(NORTHWIND)["DaysSinceLastActivity"]
    assert before == 4  # the "Checking in on redlines" email

    crm.create_task(
        opportunity_id=NORTHWIND,
        contact_id=MARIT,
        subject="Call - chased redlines",
        activity_type="Call",
        status="Completed",
        priority="Normal",
        activity_date=TODAY.isoformat(),
        description="Left a voicemail.",
    )
    assert crm.get_opportunity(NORTHWIND)["DaysSinceLastActivity"] == 0


def test_future_dated_activities_do_not_count_as_a_touch(crm):
    """Tessellate has an open task due in 3 days. A scheduled task is a
    commitment, not an interaction - it must not reset the silence clock."""
    opp = crm.get_opportunity(TESSELLATE)
    assert opp["DaysSinceLastActivity"] == 1  # the inbound email, not the open task

    open_tasks = [
        a
        for a in crm.get_activity_timeline(TESSELLATE)
        if a.kind == "task" and a.status != "Completed"
    ]
    assert open_tasks and open_tasks[0].days_ago < 0


def test_timeline_merges_three_sobjects_newest_first(crm):
    entries = crm.get_activity_timeline(NORTHWIND, limit=50)
    kinds = {e.kind for e in entries}
    assert {"task", "email"} <= kinds

    dates = [e.occurred_on for e in entries]
    assert dates == sorted(dates, reverse=True)


def test_email_open_state_survives_into_the_timeline(crm):
    """An unopened outbound email is an attempt, not a touch - the agent can
    only apply that rule if the signal reaches it."""
    entries = crm.get_activity_timeline(CALDERWOOD, limit=50)
    unopened = [e for e in entries if e.kind == "email" and e.status == "unopened"]
    assert unopened, "the fixture should contain an unopened outbound email"
    assert unopened[0].direction == "outbound"


def test_create_task_returns_an_id_and_is_immediately_readable(crm):
    record_id = crm.create_task(
        opportunity_id=CALDERWOOD,
        contact_id=None,
        subject="Send two-page business case",
        activity_type="Email",
        status="Not Started",
        priority="High",
        activity_date=(TODAY + timedelta(days=1)).isoformat(),
        description="Alicia prefers written material.",
    )
    assert len(record_id) == 18 and record_id.startswith("00T")
    assert any(e.record_id == record_id for e in crm.get_activity_timeline(CALDERWOOD, limit=50))


def test_writes_against_unknown_records_raise(crm):
    with pytest.raises(KeyError):
        crm.create_task(
            opportunity_id="006DOESNOTEXIST",
            contact_id=None,
            subject="x",
            activity_type="Other",
            status="Not Started",
            priority="Normal",
            activity_date=date.today().isoformat(),
            description="",
        )
    with pytest.raises(KeyError):
        crm.update_opportunity("006DOESNOTEXIST", {"NextStep": "x"})


def test_update_opportunity_patches_only_named_fields(crm):
    before = crm.get_opportunity(CALDERWOOD)
    crm.update_opportunity(CALDERWOOD, {"NextStep": "Book exec review"})
    after = crm.get_opportunity(CALDERWOOD)
    assert after["NextStep"] == "Book exec review"
    assert after["Amount"] == before["Amount"]
    assert after["StageName"] == before["StageName"]


def test_snapshot_round_trips(crm, tmp_path):
    crm.update_opportunity(CALDERWOOD, {"NextStep": "Book exec review"})
    out = tmp_path / "after.json"
    crm.save(out)

    reloaded = type(crm).from_file(out, today=TODAY)
    assert reloaded.get_opportunity(CALDERWOOD)["NextStep"] == "Book exec review"
