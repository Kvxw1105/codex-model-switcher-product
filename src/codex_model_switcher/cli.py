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

from .catalog import CatalogDocument
from .config import (
    apply_managed_config,
    restore_managed_config,
)
from .credentials import KeyringCredentialStore
from .models import ModelCapability, ModelRoute
from .router import Router
from .router_http import start_router_http
from .routing import default_deepseek_target


def run_control_center(**kwargs: Any) -> Any:
    """Start the web control center through its public web-module entry point."""

    from .web import run_control_center as _run_control_center

    kwargs.setdefault("state", default_control_center_state(smoke=bool(kwargs.get("smoke"))))
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


def _build_deepseek_router(credential_store: KeyringCredentialStore) -> Router:
    route = ModelRoute(
        model_id="cms-deepseek-v4-flash",
        display_name="DeepSeek V4 Flash API",
        lane="third_party",
        provider_id="deepseek",
        upstream_model="deepseek-v4-flash",
        capability=ModelCapability(
            context_window=1,
            supports_responses=True,
            supports_streaming=True,
            supports_tools=False,
            supports_images=False,
            supports_files=False,
            supports_compaction_context=False,
        ),
    )
    catalog = CatalogDocument(
        schema_version="cms-router-v1",
        client_version="control-center",
        provider_id="deepseek",
        models=(route,),
        verification_status="UNVERIFIED",
    )
    return Router(
        catalog,
        targets={route.model_id: default_deepseek_target(route)},
        credential_store=credential_store,
        provider_id_verifier={"deepseek"},
    )


def default_control_center_state(*, smoke: bool = False) -> Any:
    """Build the safe default state for the local DeepSeek control flow.

    ``smoke`` is the explicit opt-in switch for real desktop smoke tests.
    Without it, config apply/restore stay blocked with ``412`` and never
    touch a real Codex config.
    """

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
    router_service: list[Any | None] = [None]
    applied: list[Any] = []
    restored: list[Any] = []

    def router_start(_payload: object) -> dict[str, object]:
        service = router_service[0]
        if service is not None and service.running:
            address = service.address
            return {
                "status": "ok",
                "running": True,
                "address": f"http://{address[0]}:{address[1]}/v1",
            }
        try:
            if not credential_store.exists("deepseek"):
                return {
                    "status": "unconfigured",
                    "configured": False,
                    "running": False,
                    "message": "save the DeepSeek credential first",
                }
            router = _build_deepseek_router(credential_store)
            service = start_router_http(
                router,
                host="127.0.0.1",
                port=4318,
                model_aliases={"deepseek-v4-flash": "cms-deepseek-v4-flash"},
            )
        except (OSError, RuntimeError):
            return {
                "status": "failed",
                "configured": False,
                "running": False,
                "message": "router could not bind to its loopback port",
            }
        router_service[0] = service
        address = service.address
        return {
            "status": "ok",
            "running": True,
            "address": f"http://{address[0]}:{address[1]}/v1",
        }

    def router_stop(_payload: object) -> dict[str, object]:
        service = router_service[0]
        if service is not None:
            service.stop()
            router_service[0] = None
        return {"status": "ok", "running": False}

    def config_apply(payload: object) -> dict[str, object]:
        if not smoke:
            return {
                "status": "blocked",
                "configured": False,
                "reason": "picker_verification_required",
                "message": (
                    "真实 Codex 配置未修改；需要当前 picker 的外部验证收据，"
                    "或用 --smoke 显式开启桌面验收"
                ),
            }
        paths = _smoke_paths(payload)
        if paths is None:
            return {
                "status": "failed",
                "configured": False,
                "reason": "smoke_paths_required",
                "message": (
                    "smoke 模式必须显式提供 config_path、catalog_path "
                    "与 bundled_catalog_path"
                ),
            }
        config_path, catalog_path, bundled_catalog_path, native_catalog_path = paths
        try:
            receipt = apply_managed_config(
                config_path,
                catalog_path,
                native_catalog_path=native_catalog_path,
                bundled_catalog_path=bundled_catalog_path,
                router_base_url="http://127.0.0.1:4318/v1",
                smoke=True,
            )
        except Exception as error:
            return {
                "status": "failed",
                "configured": False,
                "reason": "smoke_apply_failed",
                "message": str(error),
            }
        applied.append(receipt)
        return {
            "status": "ok",
            "configured": True,
            "smoke": True,
            "backup_path": str(receipt.backup_path),
            "config_path": str(receipt.config_path),
            "original_sha256": receipt.original_hash,
            "written_sha256": receipt.written_hash,
            "timestamp": receipt.timestamp,
        }

    def config_restore(payload: object) -> dict[str, object]:
        if not smoke:
            return {
                "status": "blocked",
                "configured": False,
                "reason": "no_config_apply_receipt",
                "message": "当前没有本工具生成的配置收据可恢复",
            }
        if not applied:
            return {
                "status": "failed",
                "configured": False,
                "reason": "no_config_apply_receipt",
                "message": "smoke 模式尚未应用任何配置，没有可恢复的收据",
            }
        receipt = applied[-1]
        try:
            restore_managed_config(receipt.config_path, receipt)
        except Exception as error:
            return {
                "status": "failed",
                "configured": False,
                "reason": "smoke_restore_failed",
                "message": str(error),
            }
        restored.append(receipt)
        return {
            "status": "ok",
            "configured": False,
            "smoke": True,
            "restored_path": str(receipt.config_path),
            "original_sha256": receipt.original_hash,
            "written_sha256": receipt.written_hash,
            "timestamp": receipt.timestamp,
        }

    return ControlCenterState(
        providers=providers,
        models=models,
        credential_store=credential_store,
        smoke=smoke,
        probe_callback=lambda provider_id, provider: _probe_deepseek(
            provider_id,
            dict(provider),
            credential_store,
        ),
        config_apply=config_apply,
        config_restore=config_restore,
        router_start=router_start,
        router_stop=router_stop,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-model-switcher")
    subparsers = parser.add_subparsers(dest="command", required=True)

    gui = subparsers.add_parser("gui", help="start the loopback web control center")
    gui.add_argument("--host", default="127.0.0.1")
    gui.add_argument("--port", type=int, default=4317)
    gui.add_argument(
        "--smoke",
        action="store_true",
        help=(
            "explicitly enable real desktop smoke apply/restore (requires explicit "
            "config_path, catalog_path and bundled_catalog_path in each apply request); "
            "without it, config apply/restore stay blocked with 412"
        ),
    )

    subparsers.add_parser("status", help="print safe local control-center status")
    return parser


def _smoke_paths(payload: object) -> tuple[Any, Any, Any, Any] | None:
    """Extract explicit smoke paths from an apply payload; never auto-discover."""
    if not isinstance(payload, dict):
        return None
    config_path = payload.get("config_path")
    catalog_path = payload.get("catalog_path")
    bundled_catalog_path = payload.get("bundled_catalog_path")
    native_catalog_path = payload.get("native_catalog_path")
    required = (config_path, catalog_path, bundled_catalog_path)
    if not all(isinstance(value, str) and value.strip() for value in required):
        return None
    native = native_catalog_path if isinstance(native_catalog_path, str) else None
    return (
        config_path.strip(),
        catalog_path.strip(),
        bundled_catalog_path.strip(),
        native.strip() if native else None,
    )


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
        server = run_control_center(host=args.host, port=args.port, smoke=args.smoke)
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
