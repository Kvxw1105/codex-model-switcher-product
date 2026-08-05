from __future__ import annotations

from codex_model_switcher.web import ControlCenterState


def test_control_center_state_has_random_csrf_token() -> None:
    first = ControlCenterState()
    second = ControlCenterState()

    assert first.csrf_token
    assert first.csrf_token != second.csrf_token
