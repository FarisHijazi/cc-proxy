"""claude.ai cookie auth (claude_rc.webauth): sources, ownership, rotation,
re-read on auth failure, Cloudflare detection, redaction — offline only."""
import json
import os
import sys
import time
import types

import httpx
import pytest

from claude_rc import webauth
from claude_rc.client import RemoteControlClient
from claude_rc.credentials import CredentialsError, resolve_auth

ENV = (
    "CLAUDE_AI_SESSION_KEY", "CLAUDE_AI_COOKIES", "CLAUDE_AI_COOKIE", "CLAUDE_AI_PROXY", "CLAUDE_AI_AUTO_REFRESH",
    "CLAUDE_AI_CHROME_ACCOUNT", "CLAUDE_AI_CHROME_PROFILE", "CLAUDE_AI_CHROME_DIR",
    "CLAUDE_AI_CDP_URL", "CLAUDE_AI_USER_AGENT", "CLAUDE_AI_ACCESS_TOKEN",
    "CLAUDE_RC_ACCESS_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_RC_CSWAP_SLOT",
    "CLAUDE_AI_ORG_UUID", "CLAUDE_RC_ORG_UUID", "CLAUDE_TRUSTED_DEVICE_TOKEN",
)


@pytest.fixture(autouse=True)
def clean_env(tmp_path, monkeypatch):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setitem(sys.modules, "browser_cookie3", None)  # not installed


Cookie = types.SimpleNamespace


@pytest.fixture
def chrome(tmp_path, monkeypatch):
    """A fake Chrome dir (Local State + two profiles) and a stub browser_cookie3.
    ``stores`` maps profile -> list of jars handed out on successive reads."""
    root = tmp_path / "chrome"
    for prof, sub in (("Default", "Cookies"), ("Profile 1", "Network/Cookies")):
        f = root / prof / sub
        f.parent.mkdir(parents=True)
        f.write_text("")
    os.utime(root / "Default" / "Cookies", (1, 1))  # Profile 1 is most recent
    (root / "Local State").write_text(json.dumps({"profile": {"info_cache": {
        "Default": {"user_name": "user@example.com"},
        "Profile 1": {"user_name": "other@example.com"},
    }}}))
    stores = {
        "Default": [{"sessionKey": "sk-default", "lastActiveOrg": "org-default"}],
        "Profile 1": [{"sessionKey": "sk-p1", "lastActiveOrg": "org-p1", "cf_clearance": "cf"}],
    }
    reads = []

    def chrome_fn(cookie_file, domain_name):
        assert domain_name == "claude.ai"
        prof = os.path.relpath(cookie_file, root).split(os.sep)[0]
        reads.append(prof)
        jars = stores[prof]
        jar = jars.pop(0) if len(jars) > 1 else jars[0]
        out = [Cookie(name=k, value=v, domain=".claude.ai", path="/") for k, v in jar.items()]
        out.append(Cookie(name="other", value="x", domain=".notclaude.ai", path="/"))
        return out

    monkeypatch.setitem(sys.modules, "browser_cookie3", types.SimpleNamespace(chrome=chrome_fn))
    monkeypatch.setenv("CLAUDE_AI_CHROME_DIR", str(root))
    return types.SimpleNamespace(root=root, stores=stores, reads=reads)


def _client(handler, **kw):
    return RemoteControlClient(transport=httpx.MockTransport(handler), **kw)


def _write_state(cookies, seed=None):
    p = webauth.state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"seed": seed, "cookies": cookies}))


# --- sources ------------------------------------------------------------------------
def test_source_precedence_env_state_chrome_cdp(chrome, monkeypatch):
    monkeypatch.setenv("CLAUDE_AI_CDP_URL", "http://127.0.0.1:9222")

    async def fake_cdp(url):
        return [{"name": "sessionKey", "value": "sk-cdp", "domain": "claude.ai", "path": "/"},
                {"name": "x", "value": "y", "domain": ".example.com", "path": "/"}]

    monkeypatch.setattr(webauth, "_cdp_cookies", fake_cdp)
    monkeypatch.setenv("CLAUDE_AI_SESSION_KEY", "sk-env")
    monkeypatch.setenv("CLAUDE_AI_COOKIES", "cf_clearance=cfv; sessionKey=ignored")
    _write_state({"sessionKey": "sk-state"})

    jar = webauth.load_jar()
    assert (jar.source, jar.session_key, jar.cookies["cf_clearance"]) == ("env", "sk-env", "cfv")
    monkeypatch.delenv("CLAUDE_AI_SESSION_KEY")
    monkeypatch.delenv("CLAUDE_AI_COOKIES")
    assert webauth.load_jar().source == "state"
    webauth.state_path().unlink()
    jar = webauth.load_jar()
    assert (jar.source, jar.detail, jar.session_key) == ("chrome", "Profile 1", "sk-p1")  # most recent
    assert "other" not in jar.cookies
    monkeypatch.setitem(sys.modules, "browser_cookie3", None)
    jar = webauth.load_jar()
    assert (jar.source, jar.cookies) == ("cdp", {"sessionKey": "sk-cdp"})
    assert resolve_auth().origin == "cookies:cdp"


def test_env_key_resumes_its_rotated_state(monkeypatch):
    monkeypatch.setenv("CLAUDE_AI_SESSION_KEY", "sk-env")
    _write_state({"sessionKey": "sk-rotated", "lastActiveOrg": "o"}, seed=webauth._seed("sk-env"))
    jar = webauth.load_jar()
    assert (jar.origin, jar.session_key) == ("env:rotated", "sk-rotated")
    _write_state({"sessionKey": "sk-rotated"}, seed=webauth._seed("a-different-key"))
    assert webauth.load_jar().session_key == "sk-env"


def test_chrome_profile_pinned_by_account_email(chrome, monkeypatch):
    monkeypatch.setenv("CLAUDE_AI_CHROME_ACCOUNT", "USER@example.com")
    jar = webauth.load_jar()
    assert (jar.detail, jar.session_key) == ("Default", "sk-default")
    monkeypatch.setenv("CLAUDE_AI_CHROME_ACCOUNT", "nobody@example.com")
    with pytest.raises(CredentialsError, match="other@example.com, user@example.com"):
        webauth.load_jar()
    monkeypatch.setenv("CLAUDE_AI_CHROME_PROFILE", "Default")  # profile pin wins
    assert webauth.load_jar().detail == "Default"


def test_ownership_rule(chrome, monkeypatch):
    assert webauth.load_jar().owns_refresh() is False  # Chrome owns its cookies
    monkeypatch.setenv("CLAUDE_AI_AUTO_REFRESH", "1")
    assert webauth.load_jar().owns_refresh() is True
    monkeypatch.setenv("CLAUDE_AI_SESSION_KEY", "sk")
    monkeypatch.setenv("CLAUDE_AI_AUTO_REFRESH", "0")
    assert webauth.load_jar().owns_refresh() is False
    monkeypatch.delenv("CLAUDE_AI_AUTO_REFRESH")
    assert webauth.load_jar().owns_refresh() is True


# --- requests -----------------------------------------------------------------------
def test_cookie_mode_headers_and_last_active_org(chrome):
    seen = []
    with _client(lambda r: seen.append(r) or httpx.Response(200, json={})) as rc:
        assert rc.org_uuid == "org-p1"
        rc.get_session("cse_1")
        assert rc.cookie_header() == "sessionKey=sk-p1; lastActiveOrg=org-p1; cf_clearance=cf"
    h = seen[0].headers
    assert str(seen[0].url) == "https://claude.ai/v1/code/sessions/cse_1"
    assert h["cookie"] == "sessionKey=sk-p1; lastActiveOrg=org-p1; cf_clearance=cf"
    assert "authorization" not in h
    assert h["x-organization-uuid"] == "org-p1"
    assert h["user-agent"].startswith("Mozilla/5.0")
    assert h["anthropic-client-platform"] == "web_claude_ai"
    assert h["anthropic-client-feature"] == "ccr"
    assert h["anthropic-beta"] == "ccr-byoc-2025-07-29"
    assert h["anthropic-version"] == "2023-06-01"


def test_cookie_auth_beats_bearer_env(chrome, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "bearer")
    assert resolve_auth().is_cookie
    assert resolve_auth("explicit").credentials.access_token == "explicit"


ROTATE = [
    "sessionKey=sk-new; Path=/; Domain=.claude.ai; HttpOnly; Secure",
    "__cf_bm=bm; Path=/",
    "cf_clearance=gone; Max-Age=0",
    "evil=1; Domain=.example.com",
]


def _rotating(req):
    return httpx.Response(200, json={}, headers=[("set-cookie", c) for c in ROTATE])


def test_set_cookie_rotation_persisted_when_we_own_refresh(monkeypatch):
    monkeypatch.setenv("CLAUDE_AI_SESSION_KEY", "sk-old")
    monkeypatch.setenv("CLAUDE_AI_COOKIES", "cf_clearance=cf")
    with _client(_rotating, org_uuid="o") as rc:
        rc.get_session("cse_1")
        assert rc.cookie_jar.cookies == {"sessionKey": "sk-new", "sessionKeyV3": "sk-new", "__cf_bm": "bm"}
    path = webauth.state_path()
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    saved = json.loads(path.read_text())
    assert saved["cookies"] == {"sessionKey": "sk-new", "sessionKeyV3": "sk-new", "__cf_bm": "bm"}
    assert saved["seed"] == webauth._seed("sk-old")
    # A restart with the same (now stale) env key resumes the rotated jar.
    assert webauth.load_jar().session_key == "sk-new"


def test_set_cookie_not_persisted_for_chrome_cookies(chrome):
    with _client(_rotating) as rc:
        rc.get_session("cse_1")
        assert rc.cookie_jar.session_key == "sk-new"  # still used in memory
    assert not webauth.state_path().exists()


def test_set_cookie_from_other_hosts_ignored(monkeypatch):
    monkeypatch.setenv("CLAUDE_AI_SESSION_KEY", "sk")
    with _client(_rotating, org_uuid="o") as rc:
        rc._request("GET", "https://api.anthropic.com/x")
        assert rc.cookie_jar.session_key == "sk"


def test_401_rereads_chrome_and_retries_once(chrome):
    chrome.stores["Profile 1"] = [{"sessionKey": "sk-stale"}, {"sessionKey": "sk-fresh"}]
    seen = []

    def handler(req):
        seen.append(req.headers["cookie"])
        return httpx.Response(200 if "sk-fresh" in req.headers["cookie"] else 401, json={})

    with _client(handler, org_uuid="o") as rc:
        rc.get_session("cse_1")
    assert seen == ["sessionKey=sk-stale", "sessionKey=sk-fresh"]


def test_403_rereads_state_written_by_another_process(monkeypatch):
    _write_state({"sessionKey": "sk-a"})
    seen = []

    def handler(req):
        seen.append(req.headers["cookie"])
        if len(seen) == 1:
            _write_state({"sessionKey": "sk-b"})  # another process rotated it
            return httpx.Response(403, json={})
        return httpx.Response(200, json={})

    with _client(handler, org_uuid="o") as rc:
        rc.get_session("cse_1")
    assert seen == ["sessionKey=sk-a", "sessionKey=sk-b"]


def test_auth_failure_retried_only_once(chrome):
    chrome.stores["Profile 1"] = [{"sessionKey": "a"}, {"sessionKey": "b"}, {"sessionKey": "c"}]
    seen = []
    with _client(lambda r: seen.append(r) or httpx.Response(401, json={}), org_uuid="o") as rc:
        with pytest.raises(Exception, match="HTTP 401"):
            rc.get_session("cse_1")
    assert len(seen) == 2


@pytest.mark.parametrize("resp", [
    httpx.Response(403, headers={"cf-mitigated": "challenge"}, text=""),
    httpx.Response(403, headers={"content-type": "text/html"},
                   text="<html><title>Just a moment...</title><script src='/cdn-cgi/challenge-platform/'>"),
])
def test_cloudflare_challenge_is_a_clear_error(monkeypatch, resp):
    monkeypatch.setenv("CLAUDE_AI_SESSION_KEY", "sk")
    with _client(lambda r: resp, org_uuid="o") as rc:
        with pytest.raises(webauth.CloudflareChallenge, match="cf_clearance"):
            rc.get_session("cse_1")


def test_plain_403_is_not_a_challenge():
    assert not webauth.is_cloudflare_challenge(httpx.Response(403, json={"error": {"type": "permission_error"}}))


# --- keep-alive / redaction ----------------------------------------------------------
def test_keepalive_only_when_we_own_refresh(chrome, monkeypatch):
    hits = []

    def handler(req):
        hits.append(req.url.path)
        return httpx.Response(200, json={})

    with _client(handler, org_uuid="o") as rc:
        assert webauth.keepalive(rc, 0.01).is_set()  # Chrome-sourced: not ours
    monkeypatch.setenv("CLAUDE_AI_SESSION_KEY", "sk")
    with _client(handler, org_uuid="o") as rc:
        assert webauth.touch(rc) == 200
        stop = webauth.keepalive(rc, 0.01)
        deadline = time.time() + 2
        while hits.count("/api/account") < 3 and time.time() < deadline:
            time.sleep(0.01)
        stop.set()
    assert hits.count("/api/account") >= 3


def test_values_are_redacted(monkeypatch):
    monkeypatch.setenv("CLAUDE_AI_SESSION_KEY", "sk-ant-sid-SECRET")
    monkeypatch.setenv("CLAUDE_AI_COOKIES", "cf_clearance=CFSECRET")
    auth = resolve_auth()
    for text in (repr(auth.jar), str(auth.jar), repr(auth)):
        assert "SECRET" not in text
    assert "sessionKey" in repr(auth.jar) and "cf_clearance" in repr(auth.jar)


def test_cli_auth_prints_names_not_values(monkeypatch, capsys):
    from claude_rc import cli

    monkeypatch.setattr(cli, "_console", None)
    monkeypatch.setenv("CLAUDE_AI_SESSION_KEY", "sk-SECRET")
    monkeypatch.setenv("CLAUDE_AI_COOKIES", "lastActiveOrg=org-1")
    assert cli.main(["auth"]) == 0
    out = capsys.readouterr().out
    assert "SECRET" not in out
    assert "lastActiveOrg, sessionKey" in out and "org-1" in out and "owns refresh : True" in out


def test_singular_cookie_env_alias(monkeypatch):
    monkeypatch.setenv("CLAUDE_AI_COOKIE", "sessionKey=sk-alias; lastActiveOrg=org-9")
    jar = webauth.from_env()
    assert (jar.session_key, jar.org_uuid) == ("sk-alias", "org-9")


def test_session_key_sent_under_both_names(monkeypatch):
    monkeypatch.setenv("CLAUDE_AI_SESSION_KEY", "sk-one")
    assert {k: v for k, v in webauth.from_env().cookies.items() if k.startswith("sessionKey")} == {
        "sessionKey": "sk-one", "sessionKeyV3": "sk-one"}
    monkeypatch.delenv("CLAUDE_AI_SESSION_KEY")
    monkeypatch.setenv("CLAUDE_AI_COOKIE", "sessionKeyV3=sk-v3")
    assert webauth.from_env().session_key == "sk-v3"


def test_claude_ai_proxy_routes_only_claude_ai(monkeypatch):
    assert webauth.claude_ai_mounts() is None
    monkeypatch.setenv("CLAUDE_AI_PROXY", "http://user:pw@proxy.example.com:8080")
    assert set(webauth.claude_ai_mounts()) == {"all://claude.ai", "all://*.claude.ai"}
    assert webauth.claude_ai_mounts(httpx.MockTransport(lambda r: httpx.Response(200))) is None
