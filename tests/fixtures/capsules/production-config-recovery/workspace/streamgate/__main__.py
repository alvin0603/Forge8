"""Streamgate configuration preflight CLI."""

from __future__ import annotations

import argparse
import json
import sys

from .config import ConfigError, load_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="streamgate")
    parser.add_argument("--base", required=True)
    parser.add_argument("--overlay", required=True)
    parser.add_argument("--check", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_config(args.base, args.overlay)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.check:
        summary = {
            "environment": settings.runtime.environment,
            "workers": settings.runtime.workers,
            "collector": settings.delivery.url,
            "configuration": "valid",
        }
        print(json.dumps(summary, sort_keys=True))
        return 0

    print("Streamgate fixture only supports configuration preflight.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
