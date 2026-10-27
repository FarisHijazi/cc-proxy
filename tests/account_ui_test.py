"""The dashboard's settings writes: validated, atomic, and kept to the keys it owns."""

import json
import runpy
from pathlib import Path

import pytest

ui = runpy.run_path(str(Path(__file__).parents[1] / "scripts/account_ui.py"), run_name="account_ui")


def dashboard(tmp_path):
    accounts = tmp_path / "accounts.json"
    accounts.write_text(json.dumps({"main": "a", "accounts": {"a": 1, "b": 2}, "renamed": {"old": "a"}}))
    board = ui["Dashboard"].__new__(ui["Dashboard"])
    board.accounts_file = accounts
    board.state = lambda: json.loads(accounts.read_text())
    return board, accounts


def test_settings_are_saved_with_a_backup_and_other_keys_kept(tmp_path):
    board, accounts = dashboard(tmp_path)
    saved = board.update({"order": ["b", "a"], "balance": {"strategy": "prefer", "prefer": "b"},
                          "disabled": ["a"], "traffic_days": 30})
    assert list(saved["accounts"]) == ["b", "a"]
    assert saved["balance"] == {"strategy": "prefer", "prefer": "b"}
    assert saved["disabled"] == ["a"] and saved["traffic_days"] == 30
    assert saved["renamed"] == {"old": "a"}
    assert (tmp_path / "accounts.json.before-ui").exists()
    assert oct(accounts.stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("change", [
    {"order": ["a"]},
    {"balance": {"strategy": "random"}},
    {"balance": {"strategy": "prefer", "prefer": "nobody"}},
    {"disabled": ["nobody"]},
    {"traffic_days": 0},
])
def test_invalid_settings_are_refused_and_nothing_is_written(tmp_path, change):
    board, accounts = dashboard(tmp_path)
    before = accounts.read_text()
    with pytest.raises(ValueError):
        board.update(change)
    assert accounts.read_text() == before


def test_a_rejected_login_says_how_to_fix_it():
    assert "cswap add" in ui["login_error"]("HTTP Error 401: Unauthorized")
    assert ui["login_error"](None) is None
