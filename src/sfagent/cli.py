"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from .agent import PIPELINE_TASK, opportunity_task, run_agent
from .config import MODE_HELP, MODES, AgentConfig, RunContext
from .crm.models import ProposedAction
from .crm.mock import MockSalesforceClient
from .playbook import cadence_status, sla_for

USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


DIM, BOLD, RED, YELLOW, GREEN, CYAN = "2", "1", "31", "33", "32", "36"

STATUS_COLOR = {"overdue": RED, "due": YELLOW, "on_track": GREEN}


# --- shared -----------------------------------------------------------------


def load_org(path: Path) -> MockSalesforceClient:
    if not path.exists():
        sys.exit(f"No org fixture at {path}. Pass --org, or run from the repo root.")
    return MockSalesforceClient.from_file(path)


def print_pipeline(crm: MockSalesforceClient) -> None:
    opps = crm.list_opportunities(open_only=True)
    print(c(f"\nOpen pipeline ({len(opps)} opportunities)\n", BOLD))
    for opp in opps:
        status = cadence_status(
            opp.get("StageName", ""),
            opp.get("DaysSinceLastActivity"),
            opp.get("DaysToClose"),
        )
        silence = opp.get("DaysSinceLastActivity")
        limit = sla_for(opp.get("StageName", ""), opp.get("DaysToClose"))
        amount = opp.get("Amount") or 0
        print(
            f"  {c(status.upper().ljust(8), STATUS_COLOR[status])} "
            f"{opp.get('Name')}  {c(f'${amount:,.0f}', DIM)}"
        )
        print(
            c(
                f"           {opp.get('StageName')} | closes in {opp.get('DaysToClose')}d | "
                f"silent {silence if silence is not None else '-'}d of {limit}d allowed",
                DIM,
            )
        )
        print(c(f"           next step: {opp.get('NextStep') or '(empty)'}", DIM))
        print()


def print_actions(proposals: list[ProposedAction], declined: list[ProposedAction]) -> None:
    if not proposals and not declined:
        print(c("\nNo CRM changes were made.\n", DIM))
        return

    print(c(f"\nActions ({len(proposals)}):\n", BOLD))
    for p in proposals:
        marker = c("committed", GREEN) if p.committed else c("proposed", YELLOW)
        print(f"  [{marker}] {c(p.opportunity_name, CYAN)}")
        print(f"           {p.action}: {p.summary}")
        if p.record_id:
            print(c(f"           record: {p.record_id}", DIM))
        print()

    if declined:
        print(c(f"Declined ({len(declined)}):\n", BOLD))
        for d in declined:
            print(f"  [{c('declined', RED)}] {d.opportunity_name}: {d.summary}\n")


def approver(action: ProposedAction) -> bool:
    """Interactive y/n gate used in review mode."""
    print(c(f"\n  proposed on {action.opportunity_name}:", BOLD))
    print(f"    {action.action}: {action.summary}")
    for key, value in action.payload.items():
        if value:
            print(c(f"      {key}: {value}", DIM))
    try:
        answer = input(c("  apply this? [y/N] ", YELLOW)).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("y", "yes")


# --- commands ---------------------------------------------------------------


def cmd_inspect(args: argparse.Namespace) -> int:
    """Show the org and its cadence status. No API key needed."""
    crm = load_org(args.org)
    print_pipeline(crm)
    if args.opportunity:
        entries = crm.get_activity_timeline(args.opportunity, limit=30)
        print(c(f"Activity timeline for {args.opportunity}\n", BOLD))
        for e in entries:
            when = f"{e.days_ago}d ago" if e.days_ago >= 0 else f"in {-e.days_ago}d"
            print(f"  {e.occurred_on} ({when})  [{e.kind}/{e.direction}] {e.subject}")
            print(c(f"      {e.detail}", DIM))
        print()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    # Checked here rather than left to the SDK, which raises a much less
    # helpful error several steps later, after a run directory already exists.
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print(
            c(
                "No ANTHROPIC_API_KEY set - the agent cannot run.\n"
                "Set one (see .env.example), or run `sfagent inspect` to explore "
                "the fixture org without calling the API.",
                YELLOW,
            )
        )
        return 2

    crm = load_org(args.org)
    run_dir = args.run_dir / datetime.now().strftime("%Y%m%d-%H%M%S")
    ctx = RunContext(
        crm,
        mode=args.mode,
        approve=approver if args.mode == "review" else None,
        run_dir=run_dir,
    )
    config = AgentConfig(
        model=args.model,
        effort=args.effort,
        mode=args.mode,
        org_file=args.org,
        run_dir=args.run_dir,
        max_turns=args.max_turns,
    )

    task = opportunity_task(args.opportunity) if args.opportunity else PIPELINE_TASK

    print(c(f"\nmode: {args.mode} - {MODE_HELP[args.mode]}", DIM))
    print(c(f"model: {config.model} (effort {config.effort})", DIM))
    print(c(f"run:   {run_dir}\n", DIM))

    def on_turn(message) -> None:
        for block in message.content:
            if block.type == "tool_use":
                print(c(f"  -> {block.name}({_brief(block.input)})", DIM))

    try:
        result = run_agent(task, ctx, config, on_turn=on_turn)
    except Exception as exc:  # surfaced rather than swallowed; the audit file has the rest
        print(c(f"\nRun failed: {type(exc).__name__}: {exc}", RED))
        return 1

    print(c("\n" + "=" * 72, DIM))
    print(result.summary)
    print(c("=" * 72, DIM))

    print_actions(result.proposals, result.declined)

    (run_dir / "result.json").write_text(
        json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8"
    )
    if args.mode != "suggest":
        crm.save(run_dir / "org-after.json")

    usage = result.usage
    print(
        c(
            f"{result.turns} turns | {usage['input_tokens']:,} in "
            f"({usage['cache_read_input_tokens']:,} cached) / "
            f"{usage['output_tokens']:,} out | artifacts in {run_dir}\n",
            DIM,
        )
    )
    return 0


def _brief(payload: dict, width: int = 88) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in payload.items() if v not in ("", None))
    return text if len(text) <= width else text[: width - 3] + "..."


# --- argument parsing -------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sfagent",
        description="An agent that keeps Salesforce activity records and follow-up cadence honest.",
    )
    parser.add_argument(
        "--org",
        type=Path,
        default=Path("data/org.json"),
        help="Path to the org fixture (default: data/org.json).",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    inspect = sub.add_parser("inspect", help="Show the org and cadence status. No API calls.")
    inspect.add_argument("--opportunity", help="Also print this opportunity's activity timeline.")
    inspect.set_defaults(func=cmd_inspect)

    run = sub.add_parser("run", help="Run the agent over the pipeline.")
    run.add_argument(
        "--mode",
        choices=MODES,
        default="suggest",
        help="; ".join(f"{m}: {h}" for m, h in MODE_HELP.items()),
    )
    run.add_argument("--opportunity", help="Restrict the run to one Opportunity Id.")
    run.add_argument("--model", default=AgentConfig.model, help="Claude model id.")
    run.add_argument("--effort", default=AgentConfig.effort, choices=["low", "medium", "high", "xhigh", "max"])
    run.add_argument("--max-turns", type=int, default=40, dest="max_turns")
    run.add_argument("--run-dir", type=Path, default=Path("runs"), dest="run_dir")
    run.set_defaults(func=cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
