"""Command-line entry points for the local control center."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from typing import Any


def run_control_center(**kwargs: Any) -> Any:
    """Start the web control center through its public web-module entry point."""

    from .web import run_control_center as _run_control_center

    return _run_control_center(**kwargs)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-model-switcher")
    subparsers = parser.add_subparsers(dest="command", required=True)

    gui = subparsers.add_parser("gui", help="start the loopback web control center")
    gui.add_argument("--host", default="127.0.0.1")
    gui.add_argument("--port", type=int, default=4317)

    subparsers.add_parser("status", help="print safe local control-center status")
    return parser


def _status_payload() -> dict[str, dict[str, object]]:
    return {
        "router": {"state": "stopped"},
        "config": {"managed": False},
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "status":
        print(json.dumps(_status_payload(), ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "gui":
        if args.host not in {"127.0.0.1", "localhost", "::1"}:
            raise SystemExit("gui must bind to loopback")
        run_control_center(host=args.host, port=args.port)
        return 0
    raise SystemExit(f"unsupported command: {args.command}")


__all__ = ["main", "run_control_center"]
