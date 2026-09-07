"""Run configuration and the per-run context the tools write through."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal

from .crm.base import CrmClient
from .crm.models import ProposedAction

Mode = Literal["suggest", "review", "auto"]

MODES: tuple[Mode, ...] = ("suggest", "review", "auto")

MODE_HELP = {
    "suggest": "Read-only. Writes are recorded as proposals and never committed.",
    "review": "Each write pauses for a y/n at the terminal before it is committed.",
    "auto": "Writes are committed to the CRM as the agent makes them.",
}


@dataclass
class AgentConfig:
    """Everything that varies between runs."""

    model: str = os.environ.get("SFAGENT_MODEL", "claude-opus-5")
    effort: str = os.environ.get("SFAGENT_EFFORT", "high")
    max_tokens: int = 16_000
    mode: Mode = "suggest"
    org_file: Path = Path("data/org.json")
    run_dir: Path = Path("runs")
    max_turns: int = 40


class RunContext:
    """Shared state for one agent run.

    Every write tool goes through `propose()`, which is the single place that
    decides whether an action is committed, held for approval, or recorded as a
    suggestion. Keeping that decision in one method - rather than scattering
    `if mode == "auto"` through the tools - is what makes the mode guarantee
    checkable: `suggest` mode cannot write to the CRM because no tool has a
    path to the client that bypasses this method.
    """

    def __init__(
        self,
        crm: CrmClient,
        *,
        mode: Mode = "suggest",
        approve: Callable[[ProposedAction], bool] | None = None,
        run_dir: Path | None = None,
    ) -> None:
        self.crm = crm
        self.mode: Mode = mode
        self.approve = approve
        self.proposals: list[ProposedAction] = []
        self.declined: list[ProposedAction] = []
        self.tool_calls: list[dict[str, Any]] = []

        self.run_dir = run_dir
        if run_dir is not None:
            run_dir.mkdir(parents=True, exist_ok=True)
            self.audit_path: Path | None = run_dir / "audit.jsonl"
        else:
            self.audit_path = None

    # --- audit -----------------------------------------------------------

    def audit(self, event: str, **fields: Any) -> None:
        """Append one line to the run's audit trail.

        Anything the agent does to the CRM is reconstructable from this file,
        which is the price of letting it write at all.
        """
        record = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "event": event,
            "mode": self.mode,
            **fields,
        }
        self.tool_calls.append(record)
        if self.audit_path is not None:
            with open(self.audit_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")

    # --- the write gate --------------------------------------------------

    def propose(
        self, action: ProposedAction, commit: Callable[[], str | None]
    ) -> str:
        """Record an intended write, committing it if the mode allows.

        Returns the string handed back to the model as the tool result. The
        model is told plainly whether the write landed, because an agent that
        believes it saved something it did not will happily tell the user the
        work is done.
        """
        if self.mode == "suggest":
            self.proposals.append(action)
            self.audit("proposed", **action.to_dict())
            return (
                f"PROPOSED (not written to Salesforce - this run is in suggest mode): "
                f"{action.summary}"
            )

        if self.mode == "review":
            approved = self.approve(action) if self.approve else False
            if not approved:
                action.committed = False
                self.declined.append(action)
                self.audit("declined", **action.to_dict())
                return (
                    f"DECLINED by the user: {action.summary}. Do not retry this "
                    f"action. Move on to the next opportunity."
                )

        record_id = commit()
        action.committed = True
        action.record_id = record_id
        self.proposals.append(action)
        self.audit("committed", **action.to_dict())
        return f"COMMITTED to Salesforce as {record_id or 'an update'}: {action.summary}"


@dataclass
class RunResult:
    """What a completed run produced, for the CLI and for tests."""

    summary: str
    proposals: list[ProposedAction] = field(default_factory=list)
    declined: list[ProposedAction] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    turns: int = 0
    usage: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "turns": self.turns,
            "usage": self.usage,
            "proposals": [p.to_dict() for p in self.proposals],
            "declined": [d.to_dict() for d in self.declined],
            "tool_calls": self.tool_calls,
        }
