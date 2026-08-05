from __future__ import annotations

import pytest

from codex_model_switcher.catalog import CatalogDocument
from codex_model_switcher.models import ModelCapability, ModelRoute
from codex_model_switcher.routing import (
    HostNotAllowedError,
    RouteTarget,
    RoutingTable,
    UnknownModelError,
    default_deepseek_contract,
    default_deepseek_target,
)


def route(
    model_id: str = "official-model",
    *,
    lane: str = "official",
    provider_id: str = "openai",
    upstream_model: str = "official-upstream",
) -> ModelRoute:
    return ModelRoute(
        model_id=model_id,
        display_name=f"{model_id} {'Official' if lane == 'official' else 'API'}",
        lane=lane,
        provider_id=provider_id,
        upstream_model=upstream_model,
        capability=ModelCapability(
            context_window=4096,
            supports_responses=True,
            supports_streaming=True,
            supports_tools=True,
            supports_images=False,
            supports_files=False,
            supports_compaction_context=True,
        ),
    )


def catalog(*routes: ModelRoute) -> CatalogDocument:
    return CatalogDocument("test-v1", "fixture-client", "fixture", tuple(routes))


def test_unknown_model_is_a_structured_404_without_provider_guessing() -> None:
    table = RoutingTable(catalog(route()))

    with pytest.raises(UnknownModelError) as caught:
        table.resolve("not-in-catalog")

    error = caught.value
    assert error.status_code == 404
    assert error.to_dict() == {
        "error": {
            "type": "unknown_model",
            "message": "model_id is not present in the loaded catalog",
            "model_id": "not-in-catalog",
        }
    }


def test_official_target_rejects_a_host_outside_the_explicit_allowlist() -> None:
    with pytest.raises(HostNotAllowedError) as caught:
        RouteTarget(
            route=route(),
            base_url="https://not-allowed.example.invalid/v1/responses",
            allowed_hosts={"official.example.invalid"},
            wire_api="responses",
        )

    assert caught.value.status_code == 403
    assert caught.value.to_dict()["error"]["type"] == "host_not_allowed"


def test_default_deepseek_contract_uses_the_confirmed_responses_values() -> None:
    contract = default_deepseek_contract()

    assert contract.provider_id == "deepseek"
    assert contract.upstream_model == "deepseek-v4-flash"
    assert contract.wire_api == "responses"
    assert contract.base_url == "https://api.deepseek.com"


def test_default_deepseek_target_is_responses_only() -> None:
    deepseek_route = route(
        "deepseek-model",
        lane="third_party",
        provider_id="deepseek",
        upstream_model="deepseek-v4-flash",
    )

    target = default_deepseek_target(
        deepseek_route,
        allowed_hosts={"api.deepseek.com"},
    )

    assert target.wire_api == "responses"
    assert target.endpoint == "https://api.deepseek.com/responses"
