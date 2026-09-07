"""Argument parsing.

`--org` is attached to each subcommand rather than the top-level parser
because of a real argparse trap: a top-level argument and a subcommand
argument that share a `dest` write into the same `Namespace`, and the
subcommand's own default silently overwrites whatever the top-level flag set,
even when the subcommand doesn't re-declare the flag itself. This was found by
actually running the CLI with `--org` placed the way a user naturally types
it - after the subcommand - and hitting `unrecognized arguments`. These tests
pin both the fix and the trap it replaces.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sfagent.cli import build_parser


def test_org_is_accepted_after_the_subcommand():
    """The natural place to type it: `sfagent run --org custom.json`."""
    args = build_parser().parse_args(["run", "--org", "custom.json"])
    assert args.org == Path("custom.json")

    args = build_parser().parse_args(["inspect", "--org", "custom.json"])
    assert args.org == Path("custom.json")


def test_org_defaults_to_the_fixture_for_every_subcommand():
    assert build_parser().parse_args(["run"]).org == Path("data/org.json")
    assert build_parser().parse_args(["inspect"]).org == Path("data/org.json")


def test_org_before_the_subcommand_is_rejected_rather_than_silently_dropped():
    """It cannot work before the subcommand - `--org` isn't a top-level flag.
    The important thing is that this fails loudly instead of the pre-fix
    behaviour, where a top-level `--org` parsed but was then silently
    overwritten by the subcommand's own default."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--org", "custom.json", "run"])


def test_run_specific_flags_still_parse_alongside_org():
    args = build_parser().parse_args(
        ["run", "--org", "x.json", "--mode", "auto", "--opportunity", "006ABC", "--effort", "low"]
    )
    assert (args.org, args.mode, args.opportunity, args.effort) == (
        Path("x.json"), "auto", "006ABC", "low",
    )
