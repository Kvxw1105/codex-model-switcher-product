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


def test_status_command_is_safe_json_and_does_not_read_credentials(capsys):
    from codex_model_switcher import cli

    assert cli.main(["status"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["router"]["state"] == "stopped"
    assert payload["config"]["managed"] is False
    assert "credential" not in repr(payload).lower()
