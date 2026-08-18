"""Command-line entry point for Content Studio.

Usage::

    content-studio evaluate --dataset evaluation/topics.yaml --output evaluation/runs

The ``evaluate`` subcommand runs the offline evaluation harness defined in
:mod:`studio.evaluation`. New subcommands can be added over time
(``worker``, ``server``, ``migrate``).
"""

from __future__ import annotations

import argparse
import json
import sys

from studio.evaluation import (
    DEFAULT_FIXTURES_DIR,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_RUBRIC_PATH,
    DEFAULT_TOPICS_PATH,
    evaluate,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="content-studio")
    subparsers = parser.add_subparsers(dest="command", required=True)

    evaluate_cmd = subparsers.add_parser(
        "evaluate",
        help="Run the offline evaluation harness against a topic dataset.",
    )
    evaluate_cmd.add_argument("--dataset", type=str, default=str(DEFAULT_TOPICS_PATH))
    evaluate_cmd.add_argument("--rubric", type=str, default=str(DEFAULT_RUBRIC_PATH))
    evaluate_cmd.add_argument("--fixtures", type=str, default=str(DEFAULT_FIXTURES_DIR))
    evaluate_cmd.add_argument("--output", type=str, default=str(DEFAULT_OUTPUT_DIR))

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "evaluate":
        results_path, ballot_path = evaluate(
            topics_path=__import__("pathlib").Path(args.dataset),
            rubric_path=__import__("pathlib").Path(args.rubric),
            fixtures_dir=__import__("pathlib").Path(args.fixtures),
            output_dir=__import__("pathlib").Path(args.output),
        )
        print(f"results.json -> {results_path}")
        print(f"ballot.csv   -> {ballot_path}")
        envelope = json.loads(results_path.read_text(encoding="utf-8"))
        aggregate_pass_rate = envelope.get("aggregate_pass_rate", 0.0)
        print(f"aggregate_pass_rate -> {aggregate_pass_rate:.2f}")
        # Spec §11.3 mandates 100% on the per-topic gates (verification,
        # preservation, mutation) and 90% on the pitch difference rate.
        # The only soft threshold is the 75% blind preference rate, which
        # is a separate metric not represented in the rubric's gates.
        # Treat aggregate_pass_rate < 1.0 as a CI failure so a single
        # weak topic surfaces immediately; loosen only if a future batch
        # genuinely needs the 75% slack.
        if aggregate_pass_rate < 1.0:
            print(
                "ERROR: aggregate_pass_rate < 1.0 — at least one topic "
                "failed the §11.3 acceptance thresholds.",
                file=sys.stderr,
            )
            return 1
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
