"""Small typed structures that sit alongside raw Salesforce records.

Salesforce REST (and `simple_salesforce`) hands back plain dicts keyed by API
field name, so the CRM layer passes dicts around rather than wrapping every
sObject in a class. The dataclasses here are for the things Salesforce has no
native representation of: a normalised activity timeline and a write the agent
wants to make but may not be allowed to commit yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Literal

ActivityKind = Literal["task", "event", "email"]


@dataclass(frozen=True)
class ActivityEntry:
    """One entry in an opportunity's engagement timeline.

    Tasks, Events and EmailMessages are three different sObjects with three
    different date fields. Merging them into a single shape is what lets the
    agent reason about "days since last touch" without re-deriving it from
    three record types every time.
    """

    kind: ActivityKind
    record_id: str
    occurred_on: str          # ISO date or datetime
    days_ago: int             # negative when the activity is in the future
    subject: str
    direction: str            # "outbound" | "inbound" | "internal" | "scheduled"
    contact_id: str | None
    contact_name: str | None
    detail: str
    status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProposedAction:
    """A write the agent decided on, whether or not it was committed.

    Every write tool produces one of these. In `suggest` mode they are recorded
    and returned to the model as proposals; in `auto` mode they are committed
    first and recorded with the resulting record id. Either way the run ends
    with a complete list, which is the audit trail.
    """

    action: str                       # log_activity | schedule_followup | set_next_step
    opportunity_id: str
    opportunity_name: str
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
    committed: bool = False
    record_id: str | None = None
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
