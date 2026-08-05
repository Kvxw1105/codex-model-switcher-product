import json

import pytest

from codex_model_switcher.catalog import (
    CatalogValidationError,
    PickerContractResult,
    PickerSchemaEvidence,
    PickerVerifier,
    build_catalog_from_model_cache,
    catalog_from_mapping,
    load_catalog,
    verify_isolated_picker_contract,
)
from codex_model_switcher.models import ModelCapability, ModelRoute


def _route() -> ModelRoute:
    return ModelRoute(
        model_id="cms-example-chat",
        display_name="Example API",
        lane="third_party",
        provider_id="example-provider",
        upstream_model="example-chat",
        capability=ModelCapability(
            context_window=32_000,
            supports_responses=True,
            supports_streaming=True,
            supports_tools=True,
            supports_images=False,
            supports_files=True,
            supports_compaction_context=False,
        ),
    )


def test_catalog_generation_reads_client_version_from_model_cache(tmp_path) -> None:
    cache_path = tmp_path / "model-cache.json"
    cache_path.write_text(json.dumps({"client_version": "9.9.9"}), encoding="utf-8")

    catalog = build_catalog_from_model_cache(cache_path, [_route()])

    assert catalog["client_version"] == "9.9.9"
    assert catalog["schema_version"] is None
    assert catalog["verification_status"] == "UNVERIFIED"
    assert "0.147.0" not in json.dumps(catalog)
    assert catalog["models"][0]["id"] == "cms-example-chat"


def test_load_catalog_rejects_a_route_without_complete_capability(tmp_path) -> None:
    catalog_path = tmp_path / "safe-picker.json"
    catalog_path.write_text(
        json.dumps(
            {
                "schema_version": "picker-v1",
                "client_version": "9.9.9",
                "provider_id": "example-provider",
                "models": [
                    {
                        "id": "cms-example-chat",
                        "display_name": "Example API",
                        "lane": "third_party",
                        "provider_id": "example-provider",
                        "upstream_model": "example-chat",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(CatalogValidationError, match="capability"):
        load_catalog(catalog_path)


def test_isolated_picker_contract_fails_without_current_client_evidence(tmp_path) -> None:
    isolated_home = tmp_path / "isolated-codex-home"
    isolated_home.mkdir()
    catalog_path = tmp_path / "safe-picker.json"
    catalog_path.write_text(
        json.dumps(
            {
                "schema_version": "picker-v1",
                "client_version": "9.9.9",
                "provider_id": "example-provider",
                "models": [_route_to_record()],
            }
        ),
        encoding="utf-8",
    )
    (isolated_home / "config.toml").write_text(
        'model_provider = "example-provider"\n'
        f"model_catalog_json = {json.dumps(catalog_path.read_text(encoding='utf-8'))}\n",
        encoding="utf-8",
    )

    result = verify_isolated_picker_contract(isolated_home, catalog_path)

    assert result.passed is False
    assert "evidence" in result.reason.lower()
    assert result.evidence.schema_version == "picker-v1"
    assert str(isolated_home) not in result.to_safe_dict().__repr__()


def test_isolated_picker_contract_never_claims_native_pass_from_fixture_evidence(tmp_path) -> None:
    isolated_home = tmp_path / "isolated-codex-home"
    isolated_home.mkdir()
    catalog_path = tmp_path / "safe-picker.json"
    catalog = {
        "schema_version": "picker-v1",
        "client_version": "9.9.9",
        "provider_id": "example-provider",
        "models": [_route_to_record()],
    }
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    (isolated_home / "config.toml").write_text(
        'model_provider = "example-provider"\n'
        f"model_catalog_json = {json.dumps(json.dumps(catalog))}\n",
        encoding="utf-8",
    )

    evidence = PickerSchemaEvidence(
        schema_version="picker-v1",
        client_version="9.9.9",
        source="fixture",
    )
    result = verify_isolated_picker_contract(isolated_home, catalog_path, evidence=evidence)

    assert result.passed is False
    assert result.candidate_matches is True
    assert result.status == "UNVERIFIED"
    assert result.to_safe_dict() == {
        "passed": False,
        "status": "UNVERIFIED",
        "candidate_matches": True,
        "schema_version": "picker-v1",
        "client_version": "9.9.9",
        "provider_id": "example-provider",
    }


def test_picker_contract_result_cannot_be_constructed_as_native_pass() -> None:
    evidence = PickerSchemaEvidence(
        schema_version="picker-v1",
        client_version="9.9.9",
        source="fixture",
    )

    with pytest.raises(ValueError, match="UNVERIFIED"):
        PickerContractResult(True, "fake", evidence, "example-provider", True)


def test_only_verifier_issued_receipt_can_be_created_for_apply() -> None:
    catalog = {
        "schema_version": "picker-v1",
        "client_version": "9.9.9",
        "provider_id": "example-provider",
        "models": [_route_to_record()],
    }
    document = catalog_from_mapping(catalog)
    verifier = PickerVerifier(
        lambda candidate: PickerSchemaEvidence(
            schema_version=candidate.schema_version,
            client_version=candidate.client_version,
            source="current-client-runtime",
        )
    )

    receipt = verifier.issue_receipt(document)

    assert receipt.schema_version == "picker-v1"
    assert receipt.client_version == "9.9.9"
def _route_to_record() -> dict[str, object]:
    return {
        "id": "cms-example-chat",
        "display_name": "Example API",
        "lane": "third_party",
        "provider_id": "example-provider",
        "upstream_model": "example-chat",
        "capability": {
            "context_window": 32_000,
            "supports_responses": True,
            "supports_streaming": True,
            "supports_tools": True,
            "supports_images": False,
            "supports_files": True,
            "supports_compaction_context": False,
        },
    }
