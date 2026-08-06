import copy
import json
from pathlib import Path

import pytest

from codex_model_switcher.catalog import (
    CatalogValidationError,
    PickerContractResult,
    PickerSchemaEvidence,
    PickerVerificationReceipt,
    TrustedPickerVerifier,
    build_catalog,
    build_catalog_from_model_cache,
    build_native_catalog,
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


def test_native_adapter_preserves_internal_route_and_provider_reference() -> None:
    route = ModelRoute(
        model_id="cms-deepseek-v4-flash",
        display_name="DeepSeek V4 Flash API",
        lane="third_party",
        provider_id="deepseek",
        upstream_model="deepseek-v4-flash",
        capability=ModelCapability(
            context_window=64_000,
            supports_responses=True,
            supports_streaming=True,
            supports_tools=True,
            supports_images=False,
            supports_files=False,
            supports_compaction_context=False,
        ),
    )
    generated = build_catalog([route], client_version="0.133.0")
    assert generated["providers"] == [
        {
            "provider_id": "deepseek",
            "model": "deepseek-v4-flash",
            "wire_api": "responses",
            "credential_ref": "deepseek",
        }
    ]
    document = catalog_from_mapping(
        {
            "schema_version": "route-v1",
            "client_version": "0.133.0",
            "provider_id": "codex-model-switcher",
            "providers": [
                {
                    "provider_id": "deepseek",
                    "model": "deepseek-v4-flash",
                    "wire_api": "responses",
                    "credential_ref": "deepseek",
                }
            ],
            "models": [
                {
                    "id": route.model_id,
                    "display_name": route.display_name,
                    "lane": route.lane,
                    "provider_id": route.provider_id,
                    "upstream_model": route.upstream_model,
                    "capability": {
                        "context_window": route.capability.context_window,
                        "supports_responses": route.capability.supports_responses,
                        "supports_streaming": route.capability.supports_streaming,
                        "supports_tools": route.capability.supports_tools,
                        "supports_images": route.capability.supports_images,
                        "supports_files": route.capability.supports_files,
                        "supports_compaction_context": route.capability.supports_compaction_context,
                    },
                }
            ],
        }
    )
    native = build_native_catalog(
        document,
        bundled_catalog={"models": [_bundled_official_model()]},
    )

    internal = document.to_mapping()
    assert internal["providers"] == [
        {
            "provider_id": "deepseek",
            "model": "deepseek-v4-flash",
            "wire_api": "responses",
            "credential_ref": "deepseek",
        }
    ]
    deepseek = next(model for model in native["models"] if model["slug"] == route.model_id)
    assert deepseek["slug"] == "cms-deepseek-v4-flash"
    assert deepseek["display_name"] == "DeepSeek V4 Flash API"
    assert "deepseek-chat" not in json.dumps(document.to_mapping())
    assert "provider_id" not in deepseek
    assert "credential_ref" not in deepseek
    assert "chat_completions" not in json.dumps(native)


def test_native_adapter_does_not_replace_bundled_official_entry() -> None:
    internal = catalog_from_mapping(
        {
            "schema_version": "route-v1",
            "client_version": "0.133.0",
            "provider_id": "codex-model-switcher",
            "models": [
                {
                    "id": "gpt-5.5",
                    "display_name": "Spoofed Official API",
                    "lane": "official",
                    "provider_id": "codex-model-switcher",
                    "upstream_model": "spoofed-gpt-5.5",
                    "capability": {
                        "context_window": 1,
                        "supports_responses": True,
                        "supports_streaming": True,
                        "supports_tools": False,
                        "supports_images": False,
                        "supports_files": False,
                        "supports_compaction_context": False,
                    },
                },
                _route_to_record(),
            ],
        }
    )
    official = _bundled_official_model()
    native = build_native_catalog(internal, bundled_catalog={"models": [official]})

    assert native["models"][0] == official
    assert [model["slug"] for model in native["models"]] == ["gpt-5.5", "cms-example-chat"]


def _bundled_official_model() -> dict[str, object]:
    return {
        "slug": "gpt-5.5",
        "display_name": "GPT-5.5",
        "description": "Bundled official fixture",
        "default_reasoning_level": "medium",
        "supported_reasoning_levels": [],
        "shell_type": "shell_command",
        "visibility": "list",
        "supported_in_api": True,
        "priority": 0,
        "additional_speed_tiers": [],
        "service_tiers": [],
        "availability_nux": None,
        "upgrade": None,
        "base_instructions": "Bundled official instructions",
        "model_messages": None,
        "supports_reasoning_summaries": True,
        "default_reasoning_summary": "none",
        "support_verbosity": True,
        "default_verbosity": "low",
        "apply_patch_tool_type": "freeform",
        "web_search_tool_type": "text_and_image",
        "truncation_policy": {"mode": "tokens", "limit": 10_000},
        "supports_parallel_tool_calls": True,
        "supports_image_detail_original": True,
        "context_window": 272_000,
        "max_context_window": 272_000,
        "effective_context_window_percent": 95,
        "experimental_supported_tools": [],
        "input_modalities": ["text", "image"],
        "supports_search_tool": True,
    }


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
    native_path = tmp_path / "native.json"
    native_path.write_text(
        json.dumps(build_native_catalog(load_catalog(catalog_path))), encoding="utf-8"
    )
    (isolated_home / "config.toml").write_text(
        'model_provider = "example-provider"\n'
        f"model_catalog_json = {json.dumps(str(native_path))}\n",
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
    native_path = tmp_path / "native.json"
    native_path.write_text(
        json.dumps(build_native_catalog(load_catalog(catalog_path))), encoding="utf-8"
    )
    (isolated_home / "config.toml").write_text(
        'model_provider = "example-provider"\n'
        f"model_catalog_json = {json.dumps(str(native_path))}\n",
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


def test_untrusted_verifier_subclass_cannot_issue_a_receipt() -> None:
    catalog = {
        "schema_version": "picker-v1",
        "client_version": "9.9.9",
        "provider_id": "example-provider",
        "models": [_route_to_record()],
    }
    document = catalog_from_mapping(catalog)
    class CallerVerifier(TrustedPickerVerifier):
        def _read_current_client_evidence(self, candidate):
            return PickerSchemaEvidence(
                schema_version=candidate.schema_version,
                client_version=candidate.client_version,
                source="caller-forged-runtime",
            )

    verifier = CallerVerifier()

    assert not hasattr(CallerVerifier, "issue_receipt")
    with pytest.raises(AttributeError, match="issue_receipt"):
        verifier.issue_receipt(document)


def test_receipt_factories_are_not_exposed() -> None:
    import codex_model_switcher.catalog as catalog_module

    assert not hasattr(catalog_module, "PickerVerifier")
    assert not hasattr(TrustedPickerVerifier, "issue_receipt")
    assert not hasattr(PickerVerificationReceipt, "_from_verifier")


def test_object_new_receipt_copy_is_rejected() -> None:
    import codex_model_switcher.catalog as catalog_module

    forged = object.__new__(PickerVerificationReceipt)
    for field_name, value in {
        "schema_version": "picker-v1",
        "client_version": "9.9.9",
        "catalog_sha256": "0" * 64,
        "source": "current-client-artifact",
        "_writer_capability": getattr(catalog_module, "_WRITER_CAPABILITY_TOKEN", object()),
    }.items():
        object.__setattr__(forged, field_name, value)

    assert forged._is_authentic() is False


def test_registered_receipt_clones_are_rejected_by_identity_registry() -> None:
    import codex_model_switcher.catalog as catalog_module

    receipt = catalog_module._register_receipt_for_registry_test(
        schema_version="picker-v1",
        client_version="9.9.9",
        catalog_sha256="0" * 64,
    )
    clones = [copy.copy(receipt), copy.deepcopy(receipt)]
    object_clone = object.__new__(PickerVerificationReceipt)
    for field_name in ("schema_version", "client_version", "catalog_sha256", "source"):
        object.__setattr__(object_clone, field_name, getattr(receipt, field_name))
    clones.append(object_clone)

    assert receipt._is_authentic() is True
    assert all(clone is not receipt for clone in clones)
    assert all(clone._is_authentic() is False for clone in clones)


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


def test_native_record_matches_official_model_info_contract() -> None:
    """Lock the Gate 1 contract: official ModelInfo field names from openai/codex.

    Evidence source: codex-rs/protocol/src/openai_models.rs (main branch),
    recorded in docs/gate1-evidence-2026-08-06.md. These are field *names*
    (official snake_case JSON keys), never real client values.
    """

    route = _route()
    native = build_native_catalog(
        build_catalog([route], client_version="0.133.0"),
        bundled_catalog=None,
    )
    record = next(model for model in native["models"] if model["slug"] == route.model_id)

    # Official ModelInfo JSON keys (from the openai/codex source above).
    assert record["slug"] == route.model_id
    assert record["display_name"] == route.display_name
    assert record["supported_reasoning_levels"] == []
    assert record["shell_type"] == "disabled"
    assert record["visibility"] == "list"
    assert record["supported_in_api"] is True
    assert record["priority"] == 0
    assert record["support_verbosity"] is False
    assert record["truncation_policy"] == {"mode": "tokens", "limit": 10_000}
    assert record["supports_parallel_tool_calls"] is False
    assert record["experimental_supported_tools"] == []
    assert "text" in record["input_modalities"]
    # Current installed client (codex-cli 0.133.0) serializes the field as
    # supports_reasoning_summaries (verified via `codex debug models --bundled`);
    # GitHub main renamed it later to supports_reasoning_summary_parameter.
    assert "supports_reasoning_summaries" in record
    assert record["supports_reasoning_summaries"] is False
    assert "supports_reasoning_summary_parameter" not in record
    # No upstream, credential, provider, or lane fields may leak into native records.
    for leaked in ("upstream_model", "credential_ref", "provider_id", "lane", "wire_api"):
        assert leaked not in record


def test_native_record_keys_are_subset_of_current_client_bundled_fields(
    tmp_path,
) -> None:
    """Lock the runtime-load contract against the installed client's bundled catalog.

    Evidence: `codex debug models --bundled` on codex-cli 0.133.0 (see
    docs/gate1-evidence-2026-08-06.md). The installed client accepts exactly
    these ModelInfo keys; our native records must not add unknown keys.
    """
    from codex_model_switcher.catalog import build_native_catalog

    bundled_fixture = (
        Path(__file__).parents[1] / "fixtures" / "catalogs" / "bundled-native.json"
    )
    bundled = json.loads(bundled_fixture.read_text(encoding="utf-8"))
    official_keys = set(bundled["models"][0].keys())

    route = _route()
    native = build_native_catalog(
        build_catalog([route], client_version="0.133.0"),
        bundled_catalog=None,
    )
    record = native["models"][0]
    extra = set(record.keys()) - official_keys

    assert extra == set(), f"native record introduces keys unknown to installed client: {extra}"
