from __future__ import annotations

import json


def test_gui_command_delegates_to_loopback_control_center(monkeypatch, capsys):
    from codex_model_switcher import cli

    called: dict[str, object] = {}

    def fake_run_control_center(**kwargs):
        called.update(kwargs)

    monkeypatch.setattr(cli, "run_control_center", fake_run_control_center)

    assert cli.main(["gui", "--host", "127.0.0.1", "--port", "0"]) == 0
    assert called == {"host": "127.0.0.1", "port": 0, "smoke": False}
    assert capsys.readouterr().out == ""


def test_gui_command_prints_url_and_keeps_server_alive(monkeypatch, capsys):
    from codex_model_switcher import cli

    class FakeThread:
        def __init__(self) -> None:
            self.joined = False

        def join(self) -> None:
            self.joined = True

    class FakeServer:
        address = ("127.0.0.1", 4317)

        def __init__(self) -> None:
            self.thread = FakeThread()

    server = FakeServer()
    monkeypatch.setattr(cli, "run_control_center", lambda **_kwargs: server)

    assert cli.main(["gui"]) == 0
    assert server.thread.joined is True
    assert capsys.readouterr().out == "Control center: http://127.0.0.1:4317\n"


def test_default_state_lists_deepseek_without_exposing_a_secret(monkeypatch):
    from codex_model_switcher import cli

    class FakeStore:
        def exists(self, _provider_id: str) -> bool:
            return False

    monkeypatch.setattr(cli, "_build_credential_store", lambda: FakeStore())

    state = cli.default_control_center_state()
    payload = state.public_status()

    assert payload["providers"][0]["id"] == "deepseek"
    assert payload["models"][0]["name"] == "DeepSeek V4 Flash API"
    assert "credential" not in repr(payload["providers"][0]["base_url"]).lower()


def test_default_state_wires_router_lifecycle_callbacks(monkeypatch):
    from codex_model_switcher import cli

    class FakeStore:
        def exists(self, _provider_id: str) -> bool:
            return True

        def get(self, _provider_id: str) -> str:
            return "fixture"

    monkeypatch.setattr(cli, "_build_credential_store", lambda: FakeStore())

    state = cli.default_control_center_state()

    assert callable(state.router_start_callback)
    assert callable(state.router_stop_callback)
    assert callable(state.config_apply_callback)
    assert callable(state.config_restore_callback)


def test_default_state_router_callbacks_start_and_stop_service(monkeypatch):
    from codex_model_switcher import cli

    class FakeStore:
        def exists(self, _provider_id: str) -> bool:
            return True

        def get(self, _provider_id: str) -> str:
            return "fixture"

    class FakeService:
        address = ("127.0.0.1", 4318)
        running = True

        def __init__(self) -> None:
            self.stopped = False

        def stop(self) -> None:
            self.stopped = True

    service = FakeService()
    monkeypatch.setattr(cli, "_build_credential_store", lambda: FakeStore())
    monkeypatch.setattr(cli, "start_router_http", lambda *_args, **_kwargs: service)

    state = cli.default_control_center_state()
    started = state.router_start_callback({})
    stopped = state.router_stop_callback({})

    assert started["status"] == "ok"
    assert started["running"] is True
    assert started["address"] == "http://127.0.0.1:4318/v1"
    assert stopped["status"] == "ok"
    assert stopped["running"] is False
    assert service.stopped is True


def test_default_state_config_callbacks_report_picker_gate_without_writing(monkeypatch):
    from codex_model_switcher import cli

    class FakeStore:
        def exists(self, _provider_id: str) -> bool:
            return False

    monkeypatch.setattr(cli, "_build_credential_store", lambda: FakeStore())
    state = cli.default_control_center_state()

    result = state.config_apply_callback({})

    assert result["status"] == "blocked"
    assert result["reason"] == "picker_verification_required"
    assert result["configured"] is False


def test_status_command_is_safe_json_and_does_not_read_credentials(capsys):
    from codex_model_switcher import cli

    assert cli.main(["status"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["router"]["state"] == "stopped"
    assert payload["config"]["managed"] is False
    assert "credential" not in repr(payload).lower()


def test_default_state_config_callbacks_require_explicit_smoke_switch(monkeypatch):
    from codex_model_switcher import cli

    class FakeStore:
        def exists(self, _provider_id: str) -> bool:
            return False

    monkeypatch.setattr(cli, "_build_credential_store", lambda: FakeStore())
    state = cli.default_control_center_state()

    apply_result = state.config_apply_callback(
        {
            "config_path": "C:/x/config.toml",
            "catalog_path": "C:/x/catalog.json",
            "bundled_catalog_path": "C:/x/bundled.json",
        }
    )
    assert apply_result["status"] == "blocked"
    assert apply_result["reason"] == "picker_verification_required"

    restore_result = state.config_restore_callback({})
    assert restore_result["status"] == "blocked"
    assert restore_result["reason"] == "no_config_apply_receipt"


def test_smoke_apply_requires_explicit_paths_and_returns_sha256_evidence(
    tmp_path, monkeypatch
):
    from pathlib import Path

    from codex_model_switcher import cli

    class FakeStore:
        def exists(self, _provider_id: str) -> bool:
            return False

    fixture_dir = Path(__file__).parents[1] / "fixtures" / "catalogs"
    config_path = tmp_path / "config.toml"
    config_path.write_bytes(b'model = "original"\n')
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_bytes((fixture_dir / "safe-picker.json").read_bytes())
    bundled_path = tmp_path / "bundled-native.json"
    bundled_path.write_bytes((fixture_dir / "bundled-native.json").read_bytes())

    monkeypatch.setattr(cli, "_build_credential_store", lambda: FakeStore())
    state = cli.default_control_center_state(smoke=True)

    missing = state.config_apply_callback({"config_path": str(config_path)})
    assert missing["status"] == "failed"
    assert missing["reason"] == "smoke_paths_required"

    applied = state.config_apply_callback(
        {
            "config_path": str(config_path),
            "catalog_path": str(catalog_path),
            "bundled_catalog_path": str(bundled_path),
        }
    )
    assert applied["status"] == "ok"
    assert applied["smoke"] is True
    assert len(applied["original_sha256"]) == 64
    assert len(applied["written_sha256"]) == 64
    assert applied["original_sha256"] != applied["written_sha256"]
    assert "backup_path" in applied
    assert applied["backup_path"] != ""

    restored = state.config_restore_callback({})
    assert restored["status"] == "ok"
    assert restored["smoke"] is True
    assert config_path.read_bytes() == b'model = "original"\n'


def test_smoke_flag_is_parsed_by_gui_parser():
    from codex_model_switcher import cli

    args = cli._build_parser().parse_args(["gui", "--smoke"])
    assert args.smoke is True

    args = cli._build_parser().parse_args(["gui"])
    assert args.smoke is False
