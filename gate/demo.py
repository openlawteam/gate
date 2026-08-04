"""Zero-credential deterministic Gate demo entry point."""

from __future__ import annotations

import argparse
import json
import sys

from gate.review_store import ReplayReviewDataSource

SCENARIOS = ("risky-auth-refactor",)
SPEEDS = (0.25, 0.5, 1.0, 2.0, 4.0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gate demo",
        description="Replay a synthetic Gate review without credentials or network access.",
    )
    parser.add_argument("--scenario", choices=SCENARIOS, default=SCENARIOS[0])
    parser.add_argument("--speed", choices=SPEEDS, type=float, default=1.0)
    parser.add_argument(
        "--headless", action="store_true", help="Validate without launching the TUI"
    )
    parser.add_argument(
        "--json", action="store_true", help="Print machine-readable validation output"
    )
    return parser


def run(args: list[str]) -> int:
    parsed = build_parser().parse_args(args)
    if parsed.json and not parsed.headless:
        print("error: --json requires --headless")
        return 2
    try:
        source = ReplayReviewDataSource(parsed.scenario, speed=parsed.speed)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: malformed demo resources: {exc}", file=sys.stderr)
        return 1
    if parsed.headless:
        source.finish()
        result = source.validation_result()
        if parsed.json:
            print(json.dumps(result, sort_keys=True))
        else:
            marker = "PASS" if result["valid"] else "FAIL"
            print(
                f"{marker}: {result['scenario']} -> {result['outcome']} "
                f"({result['network_calls']} network calls)"
            )
        return 0 if result["valid"] else 1

    from gate.flight_recorder import run_demo_tui

    return run_demo_tui(source)
