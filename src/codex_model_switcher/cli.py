"""Command-line entry points for the local control center."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import datetime, timezone
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit

import httpx

from .credentials import KeyringCredentialStore


def run_control_center(**kwargs: Any) -> Any:
    """Start the web control center through its public web-module entry point."""

    from .web import run_control_center as _run_control_center

    kwargs.setdefault("state", default_control_center_state())
    return _run_control_center(**kwargs)


def _build_credential_store() -> KeyringCredentialStore:
    import keyring

    return KeyringCredentialStore(
        keyring_module=keyring,
        provider_id_verifier={"deepseek"},
    )


def _probe_deepseek(
    provider_id: str,
    provider: dict[str, object],
    credential_store: KeyringCredentialStore,
) -> dict[str, object]:
    if provider_id != "deepseek":
        return {"status": "failed"}
    base_url = provider.get("base_url")
    if not isinstance(base_url, str):
        return {"status": "failed"}
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or parsed.hostname != "api.deepseek.com":
        return {"status": "failed"}
    try:
        secret = credential_store.get(provider_id)
    except Exception:
        return {"status": "failed"}
    started = perf_counter()
    try:
        with httpx.Client(timeout=20.0) as client:
            response = client.post(
                parsed._replace(path="/responses", query="", fragment="").geturl(),
                headers={"Authorization": f"Bearer {secret}"},
                json={
                    "model": "deepseek-v4-flash",
                    "input": "Reply with exactly OK.",
                    "stream": False,
                    "store": False,
                    "max_output_tokens": 128,
                },
            )
        if response.status_code != 200:
            return {"status": "failed"}
        if not isinstance(response.json(), dict):
            return {"status": "failed"}
    except (httpx.HTTPError, ValueError):
        return {"status": "failed"}
    return {
        "status": "ok",
        "latency_ms": round((perf_counter() - started) * 1000, 1),
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def default_control_center_state() -> Any:
    """Build the safe default state for the local DeepSeek control flow."""

    from .web import ControlCenterState

    credential_store = _build_credential_store()
    providers = [
        {
            "id": "deepseek",
            "name": "DeepSeek V4 Flash",
            "base_url": "https://api.deepseek.com",
            "protocol": "responses",
            "model": "deepseek-v4-flash",
            "lane": "third_party",
            "capabilities": {
                "supports_responses": True,
                "supports_streaming": True,
            },
        }
    ]
    models = [
        {
            "id": "cms-deepseek-v4-flash",
            "name": "DeepSeek V4 Flash API",
            "lane": "third_party",
            "provider_id": "deepseek",
            "upstream_model": "deepseek-v4-flash",
            "capabilities": {
                "supports_responses": True,
                "supports_streaming": True,
            },
        }
    ]
    return ControlCenterState(
        providers=providers,
        models=models,
        credential_store=credential_store,
        probe_callback=lambda provider_id, provider: _probe_deepseek(
            provider_id,
            dict(provider),
            credential_store,
        ),
    )


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
        server = run_control_center(host=args.host, port=args.port)
        if server is None:
            return 0
        address = getattr(
            server,
            "address",
            getattr(server, "server_address", (args.host, args.port)),
        )
        print(f"Control center: http://{address[0]}:{address[1]}")
        thread = getattr(server, "thread", None)
        try:
            if thread is None:
                server.serve_forever()
            else:
                thread.join()
        except KeyboardInterrupt:
            shutdown = getattr(server, "shutdown", None)
            if callable(shutdown):
                shutdown()
        finally:
            close = getattr(server, "server_close", None)
            if callable(close):
                close()
        return 0
    raise SystemExit(f"unsupported command: {args.command}")


__all__ = ["main", "run_control_center"]
