"""Run with uv run --no-project --with pytest pytest tests/claude_model_picker_test.py."""

import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/claude-model-picker"
SERVED = ["claude-opus-5 (anthropic)", "glm-5.4 (zai)", "kimi-k3 (kimi)", "agy:gemini-4-high (agy)"]


@pytest.fixture
def run(tmp_path, monkeypatch):
    picker = runpy.run_path(str(SCRIPT))
    asked = []

    def served_models(proxy, ca):
        asked.append((proxy, ca))
        return list(served)

    served = list(SERVED)
    monkeypatch.setitem(picker["router"], "served_models", served_models)
    accounts = tmp_path / "accounts.json"
    accounts.write_text(json.dumps({"via": ["http://127.0.0.1:18790"], "via_ca": str(tmp_path / "ca.pem")}))

    def go(settings, *flags, models=None):
        if models is not None:
            served[:] = models
        monkeypatch.setattr(sys, "argv", ["claude-model-picker", "--accounts-file", str(accounts),
                                          "--settings-file", str(settings), *flags])
        picker["main"]()
        return asked

    return go


def settings_file(tmp_path, content):
    real = tmp_path / "home/settings.json"
    real.parent.mkdir()
    real.write_text(json.dumps(content))
    link = tmp_path / "profile-settings.json"
    link.symlink_to(real)
    return link, real


def models(path):
    return [row["model"] for row in json.loads(path.read_text())["modelPicker"]["options"]]


def test_writes_every_non_claude_model_through_the_symlink_keeping_other_settings(tmp_path, run):
    link, real = settings_file(tmp_path, {"theme": "dark", "modelPicker": {"replaceBuiltInOptions": False, "options": [
        {"model": "mine", "label": "Mine"},
        {"model": "glm-5.4", "label": "hand-added"},
        {"model": "gone-model", "label": "old", "description": "zai via cc-proxy"},
    ]}})
    asked = run(link)
    assert asked == [("http://127.0.0.1:18790", str(tmp_path / "ca.pem"))]
    assert link.is_symlink()
    saved = json.loads(real.read_text())
    assert saved["theme"] == "dark" and saved["modelPicker"]["replaceBuiltInOptions"] is False
    assert models(real) == ["mine", "glm-5.4", "kimi-k3", "agy:gemini-4-high"]
    assert saved["modelPicker"]["options"][1] == {
        "model": "glm-5.4", "label": "glm-5.4 (zai)", "description": "zai via cc-proxy"}
    assert len(list(real.parent.glob("settings.json.bak-model-picker-*"))) == 1


def test_second_run_changes_nothing_and_makes_no_backup(tmp_path, run):
    link, real = settings_file(tmp_path, {})
    run(link)
    written = real.read_text()
    run(link)
    assert real.read_text() == written
    assert len(list(real.parent.glob("settings.json.bak-model-picker-*"))) == 1


def test_a_removed_backend_disappears_on_the_next_run(tmp_path, run):
    link, real = settings_file(tmp_path, {})
    run(link)
    run(link, models=["glm-5.4 (zai)"])
    assert models(real) == ["glm-5.4"]


def test_dry_run_writes_nothing(tmp_path, run, capsys):
    link, real = settings_file(tmp_path, {"theme": "dark"})
    run(link, "--dry-run")
    assert json.loads(real.read_text()) == {"theme": "dark"}
    assert "+ glm-5.4" in capsys.readouterr().out
    assert not list(real.parent.glob("settings.json.bak-*"))


def test_an_unreachable_proxy_leaves_settings_untouched(tmp_path, run):
    link, real = settings_file(tmp_path, {"modelPicker": {"options": [{"model": "glm-5.4"}]}})
    before = real.read_text()
    with pytest.raises(RuntimeError, match="left unchanged"):
        run(link, models=[])
    assert real.read_text() == before


def test_the_command_exits_non_zero_when_the_proxy_cannot_be_asked(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--via", "http://127.0.0.1:9", "--via-ca", str(tmp_path / "none.pem"),
         "--settings-file", str(settings)], check=False, capture_output=True, text=True)
    assert result.returncode == 1
    assert "left unchanged" in result.stderr
    assert settings.read_text() == "{}"
