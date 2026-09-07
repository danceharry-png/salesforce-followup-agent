from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from sfagent.config import RunContext
from sfagent.crm.mock import MockSalesforceClient
from sfagent.tools import build_tools

ORG_FILE = Path(__file__).resolve().parents[1] / "data" / "org.json"

# Pin "today" so every assertion about days-since-last-activity is stable, and
# so weekday-sensitive assertions (the weekend rule) do not flip depending on
# when the suite runs. 2026-06-10 is a Wednesday.
TODAY = date(2026, 6, 10)


@pytest.fixture
def seed() -> dict:
    with open(ORG_FILE, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture
def crm(seed) -> MockSalesforceClient:
    return MockSalesforceClient(seed, today=TODAY)


@pytest.fixture
def suggest_ctx(crm) -> RunContext:
    return RunContext(crm, mode="suggest")


@pytest.fixture
def auto_ctx(crm) -> RunContext:
    return RunContext(crm, mode="auto")


def tools_for(ctx: RunContext) -> dict:
    """Tool set keyed by name, so tests can call one tool by hand."""
    return {t.name: t for t in build_tools(ctx)}


# Ids from the fixture, named so tests read as sentences.
NORTHWIND = "0065g00000XyZ01AAF"       # Negotiation/Review, silent 4 days, closes in 8
CALDERWOOD = "0065g00000XyZ02AAF"      # Proposal, empty NextStep
TESSELLATE = "0065g00000XyZ03AAF"      # healthy, has an open task
ARCADIA_CLOSED = "0065g00000XyZ06AAF"  # Closed Won
MARIT = "0035g00000PqR01AAF"
