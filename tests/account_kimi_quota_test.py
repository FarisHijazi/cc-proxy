"""Moonshot balance is credit, never a token quota or percentage."""

import json
import runpy
import urllib.error
from pathlib import Path

import pytest

MODULE = Path(__file__).parents[1] / "scripts/account_kimi_quota.py"


def reader():
    return runpy.run_path(str(MODULE), run_name="account_kimi_quota")


@pytest.mark.parametrize("amount", [0, 24.99967])
def test_balance_preserves_zero_without_inventing_limits(amount):
    q = reader()["kimi_quota"](base_url="https://api.moonshot.ai/v1", login="toy", now=123,
        call=lambda token, endpoint: {"code": 0, "status": True, "data": {
            "available_balance": amount, "voucher_balance": 4.99984, "cash_balance": 19.99983}})
    assert q["state"] == "available" and q["at"] == 123
    assert q["balances"]["available"] == amount
    assert q["currency"] == "credits" and q["windows"] == []
    assert not {"percent", "used", "limit", "tokens"} & q.keys()


@pytest.mark.parametrize("data", [{}, {"available_balance": None}, {"available_balance": True},
                                   {"available_balance": float("nan")}])
def test_missing_or_invalid_balance_is_not_zero(data):
    q = reader()["kimi_quota"](base_url="https://api.moonshot.ai/v1", login="toy",
                              call=lambda *_: {"code": 0, "status": True, "data": data})
    assert q["state"] == "unavailable" and "balances" not in q


def test_coding_endpoint_never_receives_open_platform_request():
    def forbidden(*_):
        pytest.fail("Coding quota endpoint is not verified")
    q = reader()["kimi_quota"](base_url="https://api.kimi.com/coding/v1", login="toy", call=forbidden)
    assert q["state"] == "unavailable"


def test_auth_failure_never_leaks_vendor_or_token():
    def fail(*_):
        raise urllib.error.HTTPError("https://example.test", 401, "secret-vendor-text", {}, None)
    q = reader()["kimi_quota"](base_url="https://api.moonshot.ai/v1", login="secret-token", call=fail)
    assert q["state"] == "error" and "401" in q["error"]
    assert "secret" not in json.dumps(q)


def test_access_store_is_read_only_and_matches_router_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("CCP_CONFIG_DIR", str(tmp_path))
    path = tmp_path / "kimi" / "auth.json"
    path.parent.mkdir()
    original = json.dumps({"access": "toy-access", "refresh": "must-not-use", "expires": 0})
    path.write_text(original)
    assert reader()["read_login"]() == "toy-access"
    assert path.read_text() == original
