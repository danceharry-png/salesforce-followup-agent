"""The agent's tool surface.

Six tools: three reads that let the agent build a picture of a deal, and three
writes that correspond exactly to the three things this agent is allowed to
change in the CRM. The write tools do not touch the client directly - they go
through `RunContext.propose`, which owns the decision about whether a write is
committed.

Tools are built by a factory rather than declared at module scope so that each
run gets its own bound CRM client and context. It also means a test can build a
tool set over a throwaway org and call the underlying functions directly.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Callable

from anthropic import beta_tool

from .config import RunContext
from .crm.models import ProposedAction
from .playbook import cadence_status, sla_for

MAX_TIMELINE = 40

VALID_ACTIVITY_TYPES = ("Call", "Email", "Meeting", "Other")
VALID_PRIORITIES = ("Low", "Normal", "High")


def _business_day(d: date) -> date:
    """Push Saturdays and Sundays to the following Monday.

    The playbook says follow-ups never land on a weekend; enforcing it here
    rather than asking the model to do date arithmetic means it is always true.
    """
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _money(amount: Any) -> str:
    try:
        return f"${float(amount):,.0f}"
    except (TypeError, ValueError):
        return "-"


def _fmt_opportunity_row(opp: dict[str, Any]) -> str:
    status = cadence_status(
        opp.get("StageName", ""),
        opp.get("DaysSinceLastActivity"),
        opp.get("DaysToClose"),
    )
    silence = opp.get("DaysSinceLastActivity")
    silence_text = "never" if silence is None else f"{silence}d ago"
    limit = sla_for(opp.get("StageName", ""), opp.get("DaysToClose"))
    next_step = opp.get("NextStep") or "(EMPTY)"
    return (
        f"{opp['Id']}  {opp.get('Name', '')}\n"
        f"    account={opp.get('AccountName')}  stage={opp.get('StageName')}  "
        f"amount={_money(opp.get('Amount'))}  closes in {opp.get('DaysToClose')}d\n"
        f"    last touch {silence_text} (SLA {limit}d) -> {status.upper()}  "
        f"next_step={next_step}"
    )


def build_tools(ctx: RunContext) -> list[Callable[..., Any]]:
    """Return the tool list for one run, bound to `ctx`."""

    crm = ctx.crm

    # --- reads -----------------------------------------------------------

    @beta_tool
    def review_pipeline(stage: str = "", include_on_track: bool = True) -> str:
        """List open opportunities with their follow-up cadence status.

        Start here. Each row shows the stage, amount, days to close, how long
        the deal has been silent, the cadence SLA for that stage, and whether
        the deal is ON_TRACK, DUE or OVERDUE against it.

        Args:
            stage: Optional exact stage name to filter by, e.g. "Negotiation/Review". Empty means all open stages.
            include_on_track: When False, only DUE and OVERDUE opportunities are returned.
        """
        opps = crm.list_opportunities(open_only=True, stage=stage or None)
        rows = []
        for opp in opps:
            status = cadence_status(
                opp.get("StageName", ""),
                opp.get("DaysSinceLastActivity"),
                opp.get("DaysToClose"),
            )
            if not include_on_track and status == "on_track":
                continue
            rows.append(_fmt_opportunity_row(opp))

        ctx.audit("read", tool="review_pipeline", stage=stage or None, returned=len(rows))
        if not rows:
            return "No open opportunities matched."
        return f"{len(rows)} open opportunit{'y' if len(rows) == 1 else 'ies'}:\n\n" + "\n\n".join(rows)

    @beta_tool
    def get_opportunity_detail(opportunity_id: str) -> str:
        """Full detail for one opportunity: the record, its account, its contacts, and any open tasks already scheduled against it.

        Check the open tasks before scheduling anything - if a task already
        covers the next step, scheduling a second one is duplicate work.

        Args:
            opportunity_id: The Opportunity record Id, e.g. "0065g00000XyZ01AAF".
        """
        opp = crm.get_opportunity(opportunity_id)
        if opp is None:
            return f"Error: no Opportunity with Id {opportunity_id}."

        account = crm.get_account(opp.get("AccountId")) or {}
        contacts = crm.list_contacts(opp.get("AccountId", ""))
        timeline = crm.get_activity_timeline(opportunity_id, limit=MAX_TIMELINE)
        open_tasks = [a for a in timeline if a.kind == "task" and a.status not in (None, "Completed")]

        lines = [
            f"OPPORTUNITY {opp['Id']} - {opp.get('Name')}",
            f"  Stage:            {opp.get('StageName')} ({opp.get('Probability')}% probability)",
            f"  Amount:           {_money(opp.get('Amount'))}  ({opp.get('Type')})",
            f"  CloseDate:        {opp.get('CloseDate')} (in {opp.get('DaysToClose')} days)",
            f"  NextStep:         {opp.get('NextStep') or '(EMPTY - this is a defect to fix)'}",
            f"  LastActivityDate: {opp.get('LastActivityDate')} ({opp.get('DaysSinceLastActivity')} days ago)",
            f"  LeadSource:       {opp.get('LeadSource')}",
            f"  Description:      {opp.get('Description')}",
            "",
            f"ACCOUNT {account.get('Id')} - {account.get('Name')}",
            f"  {account.get('Industry')}, {account.get('NumberOfEmployees')} employees, {account.get('BillingCity')}",
            "",
            f"CONTACTS ({len(contacts)}):",
        ]
        for c in contacts:
            lines.append(
                f"  {c['Id']}  {c.get('Name')} - {c.get('Title')} "
                f"[role: {c.get('Role', 'unknown')}]  {c.get('Email')}"
            )
        if not contacts:
            lines.append("  (none - this opportunity has no contacts on its account)")

        lines += ["", f"OPEN TASKS ({len(open_tasks)}):"]
        for t in open_tasks:
            lines.append(
                f"  {t.record_id}  due {t.occurred_on}  \"{t.subject}\" "
                f"[{t.status}] -> {t.contact_name or 'no contact'}"
            )
        if not open_tasks:
            lines.append("  (none scheduled)")

        ctx.audit("read", tool="get_opportunity_detail", opportunity_id=opportunity_id)
        return "\n".join(lines)

    @beta_tool
    def get_engagement_history(opportunity_id: str, limit: int = 20) -> str:
        """The merged interaction timeline for an opportunity, newest first.

        Combines logged Tasks, Events (meetings) and email messages. Email rows
        show direction and whether the recipient opened them - an unopened
        outbound email is an attempt, not a touch.

        Args:
            opportunity_id: The Opportunity record Id.
            limit: How many entries to return, newest first.
        """
        opp = crm.get_opportunity(opportunity_id)
        if opp is None:
            return f"Error: no Opportunity with Id {opportunity_id}."

        entries = crm.get_activity_timeline(opportunity_id, limit=min(limit, MAX_TIMELINE))
        if not entries:
            return f"No activity of any kind is logged against {opportunity_id}."

        lines = [f"ENGAGEMENT HISTORY for {opp.get('Name')} ({len(entries)} entries):"]
        for e in entries:
            when = (
                f"{e.days_ago}d ago" if e.days_ago >= 0 else f"in {-e.days_ago}d"
            )
            lines.append(
                f"\n  [{e.kind}/{e.direction}] {e.occurred_on} ({when})"
                f"{f' - {e.status}' if e.status else ''}"
                f"\n    subject: {e.subject}"
                f"\n    contact: {e.contact_name or '(none)'}"
                f"\n    detail:  {e.detail}"
            )

        ctx.audit("read", tool="get_engagement_history", opportunity_id=opportunity_id, returned=len(entries))
        return "\n".join(lines)

    # --- writes ----------------------------------------------------------

    @beta_tool
    def log_activity(
        opportunity_id: str,
        subject: str,
        description: str,
        activity_type: str = "Other",
        contact_id: str = "",
        occurred_days_ago: int = 0,
    ) -> str:
        """Log a customer interaction that happened but was never captured as a Task.

        This is CRM hygiene, not invention: only log an interaction that is
        evidenced somewhere in the record you have already read, such as an
        email exchange that has no corresponding Task. Never log an interaction
        you inferred or assumed took place.

        Args:
            opportunity_id: The Opportunity the activity belongs to.
            subject: A short subject line, e.g. "Email - customer confirmed pilot scope".
            description: What actually happened, in one or two sentences, grounded in the record.
            activity_type: One of Call, Email, Meeting, Other.
            contact_id: The Contact this was with, if known. Empty if not.
            occurred_days_ago: How many days ago it happened. 0 means today.
        """
        opp = crm.get_opportunity(opportunity_id)
        if opp is None:
            return f"Error: no Opportunity with Id {opportunity_id}."
        if activity_type not in VALID_ACTIVITY_TYPES:
            return f"Error: activity_type must be one of {', '.join(VALID_ACTIVITY_TYPES)}."
        if contact_id and crm.get_contact(contact_id) is None:
            return f"Error: no Contact with Id {contact_id}."

        when = (crm.today - timedelta(days=max(0, occurred_days_ago))).isoformat()
        action = ProposedAction(
            action="log_activity",
            opportunity_id=opportunity_id,
            opportunity_name=opp.get("Name", ""),
            summary=f'log completed {activity_type} "{subject}" dated {when}',
            payload={
                "Subject": subject,
                "Type": activity_type,
                "Status": "Completed",
                "ActivityDate": when,
                "WhoId": contact_id or None,
                "Description": description,
            },
        )

        return ctx.propose(
            action,
            lambda: crm.create_task(
                opportunity_id=opportunity_id,
                contact_id=contact_id or None,
                subject=subject,
                activity_type=activity_type,
                status="Completed",
                priority="Normal",
                activity_date=when,
                description=description,
            ),
        )

    @beta_tool
    def schedule_followup(
        opportunity_id: str,
        subject: str,
        description: str,
        due_in_days: int,
        contact_id: str = "",
        priority: str = "Normal",
    ) -> str:
        """Schedule an open follow-up Task against an opportunity.

        The subject must be executable by a colleague who has not read the
        deal: name the person, the channel and the specific thing to raise.
        "Follow up with Marit" is a bad subject; "Call Marit re: overdue MSA
        redlines - offer to join legal's call" is a good one. Weekend due dates
        are moved to the following Monday automatically.

        Args:
            opportunity_id: The Opportunity to schedule against.
            subject: The executable subject line described above.
            description: Context for whoever picks this up: what happened, and what to say.
            due_in_days: Days from today the task is due. 0 means today.
            contact_id: The Contact to reach out to, if known.
            priority: One of Low, Normal, High.
        """
        opp = crm.get_opportunity(opportunity_id)
        if opp is None:
            return f"Error: no Opportunity with Id {opportunity_id}."
        if priority not in VALID_PRIORITIES:
            return f"Error: priority must be one of {', '.join(VALID_PRIORITIES)}."
        if contact_id and crm.get_contact(contact_id) is None:
            return f"Error: no Contact with Id {contact_id}."

        due = _business_day(crm.today + timedelta(days=max(0, due_in_days)))
        action = ProposedAction(
            action="schedule_followup",
            opportunity_id=opportunity_id,
            opportunity_name=opp.get("Name", ""),
            summary=f'schedule "{subject}" due {due.isoformat()} [{priority}]',
            payload={
                "Subject": subject,
                "Status": "Not Started",
                "Priority": priority,
                "ActivityDate": due.isoformat(),
                "WhoId": contact_id or None,
                "Description": description,
            },
        )

        return ctx.propose(
            action,
            lambda: crm.create_task(
                opportunity_id=opportunity_id,
                contact_id=contact_id or None,
                subject=subject,
                activity_type="Other",
                status="Not Started",
                priority=priority,
                activity_date=due.isoformat(),
                description=description,
            ),
        )

    @beta_tool
    def set_next_step(opportunity_id: str, next_step: str, rationale: str = "") -> str:
        """Set the Opportunity's NextStep field.

        NextStep should say what happens next and roughly when, in one line a
        manager can read in a pipeline review. An empty NextStep on an open
        deal is a defect worth fixing on sight.

        Args:
            opportunity_id: The Opportunity to update.
            next_step: The new NextStep value, one line, concrete and dated.
            rationale: Why this is the right next step, for the audit trail.
        """
        opp = crm.get_opportunity(opportunity_id)
        if opp is None:
            return f"Error: no Opportunity with Id {opportunity_id}."
        if not next_step.strip():
            return "Error: next_step cannot be empty."

        previous = opp.get("NextStep") or ""
        action = ProposedAction(
            action="set_next_step",
            opportunity_id=opportunity_id,
            opportunity_name=opp.get("Name", ""),
            summary=f'set NextStep to "{next_step}"',
            payload={"NextStep": next_step, "PreviousNextStep": previous},
            rationale=rationale,
        )

        def _commit() -> str | None:
            crm.update_opportunity(opportunity_id, {"NextStep": next_step})
            return opportunity_id

        return ctx.propose(action, _commit)

    return [
        review_pipeline,
        get_opportunity_detail,
        get_engagement_history,
        log_activity,
        schedule_followup,
        set_next_step,
    ]
