"""An in-memory Salesforce org.

This implements `CrmClient` against a JSON fixture instead of a live org. It is
deliberately faithful to the real thing in the places that matter for the
agent's reasoning:

* Records use real sObject API field names (`StageName`, `ActivityDate`,
  `WhatId`, `WhoId`, `NextStep`), so the prompt the model sees here is the
  prompt it would see against a real org.
* `LastActivityDate` is derived from the activity records rather than stored,
  exactly as Salesforce derives it.
* Writes return an 18-character record id and are immediately visible to
  subsequent reads, so the agent can log an activity and then see it in the
  timeline within one run.

What it does *not* do is emulate SOQL, validation rules, record types or
sharing. Those are the reasons the real-org path is a separate implementation
rather than a subclass.
"""

from __future__ import annotations

import json
import random
import string
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .models import ActivityEntry

CLOSED_STAGES = {"Closed Won", "Closed Lost"}

# sObject key prefixes, matching the real ones.
PREFIX_TASK = "00T"
PREFIX_EVENT = "00U"


def _materialise(value: Any, today: date) -> Any:
    """Turn a relative date offset from the fixture into an ISO string.

    The fixture stores `{"days_ago": 12}` rather than a literal date so the
    sample org is always the same *shape* relative to now - a deal that has
    been quiet for twelve days stays twelve days quiet no matter when the repo
    is cloned.
    """
    if not isinstance(value, dict):
        return value
    if "days_ago" in value:
        return (today - timedelta(days=int(value["days_ago"]))).isoformat()
    if "days_from_now" in value:
        return (today + timedelta(days=int(value["days_from_now"]))).isoformat()
    return value


def _as_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _days_ago(value: str | None, today: date) -> int | None:
    d = _as_date(value)
    return None if d is None else (today - d).days


class MockSalesforceClient:
    """`CrmClient` backed by a JSON fixture held in memory."""

    def __init__(self, seed: dict[str, Any], *, today: date | None = None) -> None:
        self.today = today or date.today()
        self._rng = random.Random(20260101)  # stable ids across runs of the same seed

        self.users = self._load(seed.get("users", []))
        self.accounts = self._load(seed.get("accounts", []))
        self.contacts = self._load(seed.get("contacts", []))
        self.opportunities = self._load(seed.get("opportunities", []))
        self.tasks = self._load(seed.get("tasks", []))
        self.events = self._load(seed.get("events", []))
        self.emails = self._load(seed.get("email_messages", []))

    # --- construction ----------------------------------------------------

    @classmethod
    def from_file(
        cls, path: str | Path, *, today: date | None = None
    ) -> "MockSalesforceClient":
        with open(path, encoding="utf-8") as fh:
            return cls(json.load(fh), today=today)

    def _load(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {k: _materialise(v, self.today) for k, v in rec.items() if k != "_comment"}
            for rec in records
        ]

    def _new_id(self, prefix: str) -> str:
        body = "".join(self._rng.choices(string.ascii_uppercase + string.digits, k=12))
        return f"{prefix}5g{body}AAF"[:18]

    # --- reads -----------------------------------------------------------

    def list_opportunities(
        self,
        *,
        open_only: bool = True,
        stage: str | None = None,
        owner_id: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = []
        for opp in self.opportunities:
            if open_only and opp.get("StageName") in CLOSED_STAGES:
                continue
            if stage and opp.get("StageName", "").lower() != stage.lower():
                continue
            if owner_id and opp.get("OwnerId") != owner_id:
                continue
            rows.append(self._decorate(opp))
        rows.sort(key=lambda r: r.get("CloseDate") or "9999-12-31")
        return rows

    def get_opportunity(self, opportunity_id: str) -> dict[str, Any] | None:
        opp = self._find(self.opportunities, opportunity_id)
        return self._decorate(opp) if opp else None

    def get_account(self, account_id: str) -> dict[str, Any] | None:
        return self._copy(self._find(self.accounts, account_id))

    def get_contact(self, contact_id: str) -> dict[str, Any] | None:
        return self._copy(self._find(self.contacts, contact_id))

    def list_contacts(self, account_id: str) -> list[dict[str, Any]]:
        return [dict(c) for c in self.contacts if c.get("AccountId") == account_id]

    def get_activity_timeline(
        self, opportunity_id: str, *, limit: int = 25
    ) -> list[ActivityEntry]:
        entries: list[ActivityEntry] = []

        for t in self.tasks:
            if t.get("WhatId") != opportunity_id:
                continue
            completed = t.get("Status") == "Completed"
            entries.append(
                ActivityEntry(
                    kind="task",
                    record_id=t["Id"],
                    occurred_on=t.get("ActivityDate", ""),
                    days_ago=_days_ago(t.get("ActivityDate"), self.today) or 0,
                    subject=t.get("Subject", ""),
                    direction="outbound" if completed else "scheduled",
                    contact_id=t.get("WhoId"),
                    contact_name=self._contact_name(t.get("WhoId")),
                    detail=t.get("Description", ""),
                    status=t.get("Status"),
                )
            )

        for e in self.events:
            if e.get("WhatId") != opportunity_id:
                continue
            when = e.get("StartDateTime", "")
            ago = _days_ago(when, self.today) or 0
            entries.append(
                ActivityEntry(
                    kind="event",
                    record_id=e["Id"],
                    occurred_on=when,
                    days_ago=ago,
                    subject=e.get("Subject", ""),
                    direction="internal" if ago >= 0 else "scheduled",
                    contact_id=e.get("WhoId"),
                    contact_name=self._contact_name(e.get("WhoId")),
                    detail=e.get("Description", ""),
                    status=f"{e.get('DurationInMinutes', 0)} min",
                )
            )

        for m in self.emails:
            if m.get("RelatedToId") != opportunity_id:
                continue
            entries.append(
                ActivityEntry(
                    kind="email",
                    record_id=m["Id"],
                    occurred_on=m.get("MessageDate", ""),
                    days_ago=_days_ago(m.get("MessageDate"), self.today) or 0,
                    subject=m.get("Subject", ""),
                    direction="inbound" if m.get("Incoming") else "outbound",
                    contact_id=m.get("ContactId"),
                    contact_name=self._contact_name(m.get("ContactId")),
                    detail=m.get("Snippet", ""),
                    status="opened" if m.get("IsOpened") else "unopened",
                )
            )

        entries.sort(key=lambda a: a.occurred_on, reverse=True)
        return entries[:limit]

    # --- writes ----------------------------------------------------------

    def create_task(
        self,
        *,
        opportunity_id: str,
        contact_id: str | None,
        subject: str,
        activity_type: str,
        status: str,
        priority: str,
        activity_date: str,
        description: str,
    ) -> str:
        if self._find(self.opportunities, opportunity_id) is None:
            raise KeyError(f"No Opportunity with Id {opportunity_id}")
        if contact_id and self._find(self.contacts, contact_id) is None:
            raise KeyError(f"No Contact with Id {contact_id}")

        record_id = self._new_id(PREFIX_TASK)
        self.tasks.append(
            {
                "Id": record_id,
                "WhatId": opportunity_id,
                "WhoId": contact_id,
                "Subject": subject,
                "Type": activity_type,
                "Status": status,
                "Priority": priority,
                "ActivityDate": activity_date,
                "Description": description,
                "OwnerId": self.users[0]["Id"] if self.users else None,
                "CreatedBy": "sfagent",
            }
        )
        return record_id

    def update_opportunity(self, opportunity_id: str, fields: dict[str, Any]) -> None:
        opp = self._find(self.opportunities, opportunity_id)
        if opp is None:
            raise KeyError(f"No Opportunity with Id {opportunity_id}")
        opp.update(fields)

    # --- helpers ---------------------------------------------------------

    def _decorate(self, opp: dict[str, Any]) -> dict[str, Any]:
        """Add the fields Salesforce computes rather than stores.

        `LastActivityDate` is a real Salesforce field derived from related
        activities; `DaysSinceLastActivity` and `DaysToClose` are conveniences
        that save the model from doing date arithmetic in its head, which is
        exactly the sort of thing it should not be doing.
        """
        rec = dict(opp)
        account = self._find(self.accounts, rec.get("AccountId"))
        rec["AccountName"] = account.get("Name") if account else None

        past = [
            a.occurred_on
            for a in self.get_activity_timeline(rec["Id"], limit=200)
            if a.days_ago >= 0
        ]
        last = max(past) if past else None
        rec["LastActivityDate"] = last
        rec["DaysSinceLastActivity"] = _days_ago(last, self.today)
        rec["DaysToClose"] = (
            -(_days_ago(rec.get("CloseDate"), self.today) or 0)
            if rec.get("CloseDate")
            else None
        )
        rec["IsClosed"] = rec.get("StageName") in CLOSED_STAGES
        return rec

    def _contact_name(self, contact_id: str | None) -> str | None:
        c = self._find(self.contacts, contact_id)
        return c.get("Name") if c else None

    @staticmethod
    def _find(
        records: list[dict[str, Any]], record_id: str | None
    ) -> dict[str, Any] | None:
        if not record_id:
            return None
        return next((r for r in records if r.get("Id") == record_id), None)

    @staticmethod
    def _copy(record: dict[str, Any] | None) -> dict[str, Any] | None:
        return dict(record) if record else None

    # --- persistence -----------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Current org state with absolute dates, for writing out after a run."""
        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "as_of": self.today.isoformat(),
            "users": self.users,
            "accounts": self.accounts,
            "contacts": self.contacts,
            "opportunities": self.opportunities,
            "tasks": self.tasks,
            "events": self.events,
            "email_messages": self.emails,
        }

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.snapshot(), indent=2) + "\n", encoding="utf-8"
        )
