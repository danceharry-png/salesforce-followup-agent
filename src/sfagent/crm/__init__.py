"""CRM access layer: the `CrmClient` interface and its implementations."""

from .base import CrmClient
from .mock import MockSalesforceClient
from .models import ActivityEntry, ProposedAction

__all__ = ["CrmClient", "MockSalesforceClient", "ActivityEntry", "ProposedAction"]
