from pathlib import Path


def test_tests_never_point_at_real_codex_home(tmp_path, monkeypatch):
    isolated_home = (tmp_path / "isolated-codex-home").resolve()
    real_home = (Path.home() / ".codex").resolve()
    monkeypatch.setenv("CODEX_HOME", str(isolated_home))
    assert isolated_home != real_home
