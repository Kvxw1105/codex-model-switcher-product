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


def test_status_command_is_safe_json_and_does_not_read_credentials(capsys):
    from codex_model_switcher import cli

    assert cli.main(["status"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["router"]["state"] == "stopped"
    assert payload["config"]["managed"] is False
    assert "credential" not in repr(payload).lower()
