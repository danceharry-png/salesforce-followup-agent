"""The CRM boundary.

Everything above this line (tools, agent, CLI) talks to `CrmClient` and never
to Salesforce directly. That is the whole point of the interface: the agent's
reasoning, the tool surface and the audit trail are identical whether the
records come from the in-memory fixture org or a live Salesforce instance.

`MockSalesforceClient` in `mock.py` is the implementation this repo ships with.
A `SimpleSalesforceClient` wrapping `simple_salesforce.Salesforce` implements
the same eight methods against real SOQL and the sObject REST endpoints; see
the "Pointing it at a real org" section of the README.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Protocol, runtime_checkable

from .models import ActivityEntry


@runtime_checkable
class CrmClient(Protocol):
    """Read and write access to the pipeline objects the agent works with."""

    today: date
    """The date the client reasons from.

    The client owns "now" rather than each caller reaching for `date.today()`,
    for two reasons: every due date, silence calculation and activity date in a
    run then agrees with every other one, and a test can pin a date and make
    assertions about weekday behaviour that do not flip depending on when the
    suite happens to run. A live client sets this to today at construction.
    """

    # --- reads -----------------------------------------------------------

    def list_opportunities(
        self,
        *,
        open_only: bool = True,
        stage: str | None = None,
        owner_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Opportunity records, newest close date first.

        `open_only` filters out anything in a Closed Won / Closed Lost stage.
        """
        ...

    def get_opportunity(self, opportunity_id: str) -> dict[str, Any] | None:
        """One Opportunity record, or None if the id does not resolve."""
        ...

    def get_account(self, account_id: str) -> dict[str, Any] | None:
        ...

    def get_contact(self, contact_id: str) -> dict[str, Any] | None:
        ...

    def list_contacts(self, account_id: str) -> list[dict[str, Any]]:
        """Contacts on an account, which is how the agent finds who to write to."""
        ...

    def get_activity_timeline(
        self, opportunity_id: str, *, limit: int = 25
    ) -> list[ActivityEntry]:
        """Tasks, Events and EmailMessages merged and sorted newest first."""
        ...

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
        """Insert a Task and return its new record id.

        A Completed task is how Salesforce represents "this interaction
        happened"; a Not Started one with a future ActivityDate is how it
        represents "this interaction is due". The agent uses both.
        """
        ...

    def update_opportunity(
        self, opportunity_id: str, fields: dict[str, Any]
    ) -> None:
        """Patch fields on an Opportunity (NextStep, CloseDate, ...)."""
        ...
