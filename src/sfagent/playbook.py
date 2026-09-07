"""The sales policy the agent reasons against.

This is the part of the system that encodes judgement, and it is kept in one
file on purpose. A rep's manager should be able to read this without reading
any Python, disagree with a number, change it, and get different agent
behaviour on the next run.

`STAGE_SLA` is used twice: it is rendered into the system prompt, and the
pipeline tool uses it to mark each opportunity as on-cadence or overdue. Those
two must not drift apart, which is why neither one hardcodes the numbers.
"""

from __future__ import annotations

# Maximum days a deal in each stage should go without a logged customer touch.
# Later stages get tighter SLAs because the cost of silence rises as the deal
# approaches a decision.
STAGE_SLA: dict[str, int] = {
    "Prospecting": 14,
    "Qualification": 10,
    "Needs Analysis": 10,
    "Value Proposition": 7,
    "Id. Decision Makers": 7,
    "Perception Analysis": 7,
    "Proposal/Price Quote": 5,
    "Negotiation/Review": 3,
}

DEFAULT_SLA = 10

# Inside this many days of the close date, the SLA tightens to a hard 2 days
# regardless of stage: a deal that is about to close either closes or slips,
# and silence is how it slips.
CLOSING_WINDOW_DAYS = 14
CLOSING_WINDOW_SLA = 2


def sla_for(stage: str, days_to_close: int | None = None) -> int:
    """Days of allowed silence for a deal in `stage` closing in `days_to_close`."""
    base = STAGE_SLA.get(stage, DEFAULT_SLA)
    if days_to_close is not None and 0 <= days_to_close <= CLOSING_WINDOW_DAYS:
        return min(base, CLOSING_WINDOW_SLA)
    return base


def cadence_status(stage: str, days_since_activity: int | None, days_to_close: int | None) -> str:
    """`on_track`, `due`, or `overdue` for one opportunity."""
    limit = sla_for(stage, days_to_close)
    if days_since_activity is None:
        return "overdue"
    if days_since_activity > limit:
        return "overdue"
    if days_since_activity == limit:
        return "due"
    return "on_track"


def _sla_table() -> str:
    rows = [f"  - {stage}: {days} days" for stage, days in STAGE_SLA.items()]
    return "\n".join(rows)


PLAYBOOK = f"""
# Follow-up policy

## Cadence SLAs
Maximum days a deal should go without a logged customer touch, by stage:
{_sla_table()}
  - any stage not listed: {DEFAULT_SLA} days

Override: within {CLOSING_WINDOW_DAYS} days of the close date, the limit drops to
{CLOSING_WINDOW_SLA} days regardless of stage. A deal that is about to close either
closes or slips, and silence is how it slips.

## What counts as a touch
A logged Task, a completed Event, or an outbound email that the contact opened.
An unopened outbound email is an attempt, not a touch - it tells you the message
did not land, and repeating the same channel is unlikely to work better the
second time.

## Reading the engagement signals
- **Inbound reply** - the strongest signal there is. If the last activity is an
  inbound message containing a request ("send me X"), the correct next step is
  to do that thing on a specific date, not to check in.
- **Two or more unanswered outbound touches** - the channel or the person is
  wrong. Change one of them: switch channel, or go to a different contact.
  Do not send a third identical nudge.
- **A promise with a date attached that has passed** - follow up on the promise
  specifically, referencing what was promised and when. This is a materially
  different message from a generic check-in and should be scheduled tightly.
- **Single-threaded deal** - only one contact on an opportunity above
  ~$100k, or no contact with an Economic Buyer role, is a risk in its own
  right. The follow-up should aim at widening the relationship, not just
  advancing the current thread.
- **A named blocker** - do not route around them silently. The next step should
  give the blocker what they asked for, in writing.

## Choosing the action
For each opportunity that needs attention, choose the smallest action that
actually moves it:
1. `log_activity` - an interaction visible in the record's history but not yet
   captured as a Task. Log it so `LastActivityDate` and the timeline are true.
   Only log interactions supported by evidence in the record. Never invent one.
2. `schedule_followup` - an open Task with a concrete due date and a subject a
   colleague could execute without asking what it means. "Follow up with
   Marit" is a bad subject. "Call Marit re: overdue MSA redlines - offer to
   join legal's call" is a good one.
3. `set_next_step` - the Opportunity's NextStep field should always say what
   happens next and roughly when. An empty NextStep on an open deal is a
   defect; fix it whenever you see one.

## Due dates
- Overdue against SLA, or inside the closing window: due today or tomorrow.
- On track but with an open commitment: due on the committed date.
- Early-stage nurture: due 5-10 days out.
Never schedule a follow-up onto a Saturday or Sunday; move it to the Monday.

## Restraint
These matter as much as the actions:
- If an open Task already covers the next step, do not create a second one.
  Say so and move on.
- If a deal is on track and has a live commitment already booked, take no
  action on it.
- Ignore closed deals entirely.
- Never state a fact about the customer that is not in the record you read.
  If you need something the record does not contain, say what is missing rather
  than assuming it.
""".strip()
