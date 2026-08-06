from __future__ import annotations

import json


def test_gui_command_delegates_to_loopback_control_center(monkeypatch, capsys):
    from codex_model_switcher import cli

    called: dict[str, object] = {}

    def fake_run_control_center(**kwargs):
        called.update(kwargs)

    monkeypatch.setattr(cli, "run_control_center", fake_run_control_center)

    assert cli.main(["gui", "--host", "127.0.0.1", "--port", "0"]) == 0
    assert called == {"host": "127.0.0.1", "port": 0}
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
