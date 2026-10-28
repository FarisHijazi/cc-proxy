"""Core of RemoteControlClient: header policy, auth resolution, org lookup,
retries and cookie mode — all offline through httpx.MockTransport."""
import json
from pathlib import Path

import httpx
import pytest

from claude_rc import client as client_mod
from claude_rc.client import (
    CCR_BETA,
    FILES_BETAS,
    TRIGGERS_BETA,
    APIError,
    RemoteControlClient,
    build_headers,
)
from claude_rc.credentials import (
    CredentialsError,
    OAuthCredentials,
    load_slot_token,
    resolve_auth,
)
from claude_rc.environments import EnvironmentsMixin
from claude_rc.files import FilesMixin
from claude_rc.interaction import InteractionMixin
from claude_rc.lifecycle import LifecycleMixin
from claude_rc.triggers import TriggersMixin

AUTH_ENV = (
    "CLAUDE_AI_ACCESS_TOKEN",
    "CLAUDE_RC_ACCESS_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_AI_SESSION_KEY",
    "CLAUDE_AI_COOKIES",
    "CLAUDE_AI_AUTO_REFRESH",
    "CLAUDE_AI_CHROME_ACCOUNT",
    "CLAUDE_AI_CHROME_PROFILE",
    "CLAUDE_AI_CHROME_DIR",
    "CLAUDE_AI_CDP_URL",
    "XDG_STATE_HOME",
    "CLAUDE_RC_CSWAP_SLOT",
    "CLAUDE_AI_ORG_UUID",
    "CLAUDE_RC_ORG_UUID",
    "CLAUDE_TRUSTED_DEVICE_TOKEN",
)


@pytest.fixture(autouse=True)
def clean_env(tmp_path, monkeypatch):
    """Empty HOME and none of the auth env vars, so nothing leaks from the machine."""
    for name in AUTH_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: SLEEPS.append(s))
    SLEEPS.clear()


SLEEPS: list[float] = []


def _write_creds(home: Path, token="file-token", refresh="file-refresh", expires_at=9999999999999):
    d = home / ".claude"
    d.mkdir(exist_ok=True)
    (d / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": token, "refreshToken": refresh, "expiresAt": expires_at,
        "scopes": ["user:profile", "user:sessions:claude_code"]}}))


def _client(handler, **kw) -> RemoteControlClient:
    kw.setdefault("org_uuid", "org-1")
    kw.setdefault("token", "tok")
    return RemoteControlClient(transport=httpx.MockTransport(handler), **kw)


# --- scaffolding ---------------------------------------------------------------
def test_client_inherits_every_mixin():
    for mixin in (LifecycleMixin, EnvironmentsMixin, TriggersMixin, InteractionMixin, FilesMixin):
        assert issubclass(RemoteControlClient, mixin)


# --- header policy ---------------------------------------------------------------
def test_default_headers_are_the_web_apps():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"data": []})

    with _client(handler) as rc:
        rc.list_sessions(limit=5)
    h = seen[0].headers
    assert str(seen[0].url) == "https://api.anthropic.com/v1/code/sessions?limit=5"
    assert h["authorization"] == "Bearer tok"
    assert h["anthropic-version"] == "2023-06-01"
    assert h["anthropic-beta"] == CCR_BETA
    assert h["anthropic-client-platform"] == "web_claude_ai"
    assert h["anthropic-client-feature"] == "ccr"
    assert h["x-organization-uuid"] == "org-1"
    assert h["content-type"] == "application/json"
    assert h["user-agent"].startswith("claude-rc-api-python/")


def test_per_call_betas_platform_and_bare_policy():
    seen = []

    def handler(req):
        seen.append(req.headers)
        return httpx.Response(200, json={})

    with _client(handler) as rc:
        rc._request("GET", "/v1/code/triggers", betas=(TRIGGERS_BETA,))
        rc._request("POST", "/v1/files", betas=FILES_BETAS, client_platform="desktop_app", files={"file": ("a.txt", b"x")})
        rc._request("GET", "/api/oauth/profile", web=False, org=False)
    assert seen[0]["anthropic-beta"] == f"{CCR_BETA},{TRIGGERS_BETA}"
    assert seen[1]["anthropic-beta"] == f"{CCR_BETA},files-api-2025-04-14,oauth-2025-04-20"
    assert seen[1]["anthropic-client-platform"] == "desktop_app"
    assert seen[1]["content-type"].startswith("multipart/form-data")
    assert "anthropic-beta" not in seen[2] and "anthropic-client-platform" not in seen[2]
    assert "anthropic-client-feature" not in seen[2] and "x-organization-uuid" not in seen[2]


def test_build_headers_dedupes_betas_and_extra_wins():
    h = build_headers(token="t", betas=[CCR_BETA, "b1,b2"], extra={"anthropic-version": "x"})
    assert h["anthropic-beta"] == f"{CCR_BETA},b1,b2"
    assert h["anthropic-version"] == "x"


def test_trusted_device_token_from_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_TRUSTED_DEVICE_TOKEN", "td")
    seen = []
    with _client(lambda r: seen.append(r.headers) or httpx.Response(200, json={})) as rc:
        rc.get_session("cse_1")
    assert seen[0]["x-trusted-device-token"] == "td"


# --- auth resolution order -------------------------------------------------------
def test_resolve_auth_order(tmp_path, monkeypatch):
    _write_creds(tmp_path)
    assert resolve_auth().credentials.access_token == "file-token"
    assert resolve_auth().origin == "credentials file"

    monkeypatch.setenv("CLAUDE_RC_CSWAP_SLOT", "3")
    slot = resolve_auth(slot_token=lambda s: (f"slot-{s}", 9999999999999))
    assert slot.credentials.access_token == "slot-3" and slot.credentials.refresh_token is None

    monkeypatch.setenv("CLAUDE_RC_ACCESS_TOKEN", "rc-env")
    assert resolve_auth().credentials.access_token == "rc-env"

    monkeypatch.setenv("CLAUDE_AI_ACCESS_TOKEN", "ai-env")
    assert resolve_auth().credentials.access_token == "ai-env"

    # Cookie auth is primary whenever a cookie source yields a sessionKey.
    monkeypatch.setenv("CLAUDE_AI_SESSION_KEY", "sk")
    assert resolve_auth().session_key == "sk" and resolve_auth().credentials is None

    assert resolve_auth("explicit").credentials.access_token == "explicit"
    explicit_slot = resolve_auth(cswap_slot=7, slot_token=lambda s: (f"slot-{s}", 9999999999999))
    assert explicit_slot.credentials.access_token == "slot-7"


def test_resolve_auth_without_any_source_raises():
    with pytest.raises(CredentialsError):
        resolve_auth()


def test_cswap_slot_is_reread_when_expired(monkeypatch):
    tokens = iter([("slot-old", 1), ("slot-new", 9999999999999)])
    auth = resolve_auth(cswap_slot="2", slot_token=lambda s: next(tokens))
    seen = []
    with RemoteControlClient(auth=auth, org_uuid="o", transport=httpx.MockTransport(
        lambda r: seen.append(r.headers["authorization"]) or httpx.Response(200, json={})
    )) as rc:
        monkeypatch.setattr(client_mod, "refresh_credentials", lambda *a, **k: pytest.fail("cswap owns refresh"))
        rc.get_session("cse_1")
    assert seen == ["Bearer slot-new"]


def test_load_slot_token_reuses_account_inject_without_side_effects():
    slot_token = load_slot_token()
    assert slot_token.__name__ == "slot_token"
    assert slot_token.__globals__["MARGIN_MS"] == 5 * 60 * 1000
    # claude_swap is not installed here: the reader fails as a credentials error.
    from claude_rc.credentials import cswap_credentials
    try:
        import claude_swap  # noqa: F401
    except ImportError:
        with pytest.raises(CredentialsError):
            cswap_credentials("1")


# --- org uuid --------------------------------------------------------------------
def test_org_uuid_from_profile_lazily_and_once():
    calls = []

    def handler(req):
        calls.append(req)
        if req.url.path == "/api/oauth/profile":
            return httpx.Response(200, json={"account": {"uuid": "a"}, "organization": {"uuid": "org-from-profile"}})
        return httpx.Response(200, json={"data": []})

    with _client(handler, org_uuid=None) as rc:
        rc.list_sessions()
        rc.list_sessions()
    paths = [c.url.path for c in calls]
    assert paths == ["/api/oauth/profile", "/v1/code/sessions", "/v1/code/sessions"]
    profile = calls[0].headers
    assert profile["authorization"] == "Bearer tok" and profile["cache-control"] == "no-cache"
    assert "x-organization-uuid" not in profile and "anthropic-client-platform" not in profile
    assert calls[1].headers["x-organization-uuid"] == "org-from-profile"


def test_org_uuid_lookup_failure_is_cached_as_none():
    calls = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(403 if req.url.path == "/api/oauth/profile" else 200, json={})

    with _client(handler, org_uuid=None) as rc:
        rc.get_session("cse_1")
        rc.get_session("cse_1")
        assert rc.org_uuid is None
    assert calls.count("/api/oauth/profile") == 1


def test_org_uuid_env_and_config_precedence(tmp_path, monkeypatch):
    (tmp_path / ".claude.json").write_text(json.dumps({"oauthAccount": {"organizationUuid": "org-config"}}))
    rc = _client(lambda r: httpx.Response(200, json={}), org_uuid=None)
    assert rc.org_uuid == "org-config"
    monkeypatch.setenv("CLAUDE_RC_ORG_UUID", "org-rc")
    assert _client(lambda r: httpx.Response(200), org_uuid=None).org_uuid == "org-rc"
    monkeypatch.setenv("CLAUDE_AI_ORG_UUID", "org-ai")
    assert _client(lambda r: httpx.Response(200), org_uuid=None).org_uuid == "org-ai"
    assert _client(lambda r: httpx.Response(200), org_uuid="org-explicit").org_uuid == "org-explicit"


# --- retries ---------------------------------------------------------------------
def _scripted(*statuses, headers=None):
    calls = []
    script = iter(statuses)

    def handler(req):
        calls.append(req)
        return httpx.Response(next(script), json={"type": "error", "error": {"type": "x"}}, headers=headers or {})

    return calls, handler


def test_get_retries_5xx_with_backoff():
    calls, handler = _scripted(503, 502, 200)
    with _client(handler) as rc:
        rc.get_session("cse_1")
    assert len(calls) == 3 and SLEEPS == [2.0, 4.0]


def test_get_gives_up_after_four_retries():
    calls, handler = _scripted(500, 500, 500, 500, 500)
    with _client(handler) as rc, pytest.raises(APIError) as exc:
        rc.get_session("cse_1")
    assert len(calls) == 5 and SLEEPS == [2.0, 4.0, 8.0, 16.0] and exc.value.status == 500


def test_post_is_not_resent_after_5xx():
    calls, handler = _scripted(503)
    with _client(handler) as rc, pytest.raises(APIError):
        rc.send_message("cse_1", "hi")
    assert len(calls) == 1 and SLEEPS == []


def test_429_is_retried_for_post_honouring_retry_after():
    calls, handler = _scripted(429, 200, headers={"retry-after": "3"})
    with _client(handler) as rc:
        rc.send_message("cse_1", "hi")
    assert len(calls) == 2 and SLEEPS == [3.0]
    # the resend carries the same event uuid (ingest dedupes on it)
    assert json.loads(calls[0].content) == json.loads(calls[1].content)


def test_network_errors_retry_only_idempotent():
    attempts = []

    def handler(req):
        attempts.append(req.method)
        if len(attempts) == 1:
            raise httpx.ConnectError("down", request=req)
        return httpx.Response(200, json={})

    with _client(handler) as rc:
        rc.get_session("cse_1")
    assert attempts == ["GET", "GET"]

    attempts.clear()
    with _client(handler) as rc, pytest.raises(httpx.ConnectError):
        rc.send_message("cse_1", "hi")
    assert attempts == ["POST"]


def test_401_refreshes_once_and_resends(tmp_path, monkeypatch):
    _write_creds(tmp_path, token="stale", refresh="r1")
    seen = []

    def handler(req):
        if req.url.host == "platform.claude.com":
            assert json.loads(req.content)["refresh_token"] == "r1"
            return httpx.Response(200, json={"access_token": "fresh", "refresh_token": "r2", "expires_in": 3600})
        seen.append(req.headers["authorization"])
        return httpx.Response(401 if len(seen) == 1 else 200, json={})

    with RemoteControlClient(org_uuid="o", transport=httpx.MockTransport(handler)) as rc:
        rc.get_session("cse_1")
    assert seen == ["Bearer stale", "Bearer fresh"]
    saved = json.loads((tmp_path / ".claude" / ".credentials.json").read_text())["claudeAiOauth"]
    assert saved["accessToken"] == "fresh" and saved["refreshToken"] == "r2"


def test_bare_token_401_raises_without_retry():
    calls, handler = _scripted(401)
    with _client(handler) as rc, pytest.raises(APIError) as exc:
        rc.get_session("cse_1")
    assert len(calls) == 1 and exc.value.status == 401


def test_api_error_exposes_envelope_and_archive_409_is_success():
    def handler(req):
        if req.url.path.endswith("/archive"):
            return httpx.Response(409, json={})
        return httpx.Response(409, json={"type": "error", "error": {"type": "session_not_active", "message": "m"}},
                              headers={"request-id": "req_1"})

    with _client(handler) as rc:
        assert rc.archive_session("cse_1") is True
        with pytest.raises(APIError) as exc:
            rc.send_message("cse_1", "hi")
    assert exc.value.error_type == "session_not_active" and exc.value.request_id == "req_1"
    assert APIError(403, '{"error":{"resource":"untrusted_device"}}').resource == "untrusted_device"
    assert APIError(500, "not json").error == {}


# --- cookie mode ------------------------------------------------------------------
def test_cookie_mode_moves_to_claude_ai(monkeypatch):
    monkeypatch.setenv("CLAUDE_AI_SESSION_KEY", "sk-cookie")
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(401 if req.url.path.endswith("/archive") else 200, json={})

    with RemoteControlClient(org_uuid="o", transport=httpx.MockTransport(handler)) as rc:
        assert rc.base_url == "https://claude.ai"
        rc.get_session("cse_1")
        with pytest.raises(APIError):
            rc.archive_session("cse_1")
    first = seen[0]
    assert str(first.url) == "https://claude.ai/v1/code/sessions/cse_1"
    assert first.headers["cookie"] == "sessionKey=sk-cookie; sessionKeyV3=sk-cookie"
    assert "authorization" not in first.headers
    assert first.headers["anthropic-client-platform"] == "web_claude_ai"
    assert len(seen) == 2  # the 401 was not retried: a cookie cannot be refreshed


def test_cookie_mode_org_lookup_unverified_route():
    def handler(req):
        if req.url.path == "/api/organizations":
            return httpx.Response(200, json=[{"uuid": "org-web"}])
        return httpx.Response(200, json={})

    with RemoteControlClient(session_key="sk", transport=httpx.MockTransport(handler)) as rc:
        assert rc.org_uuid == "org-web"


def test_absolute_urls_pass_through():
    seen = []
    with _client(lambda r: seen.append(str(r.url)) or httpx.Response(200, json={"ok": True})) as rc:
        assert rc._request("POST", "https://claude.ai/api/x", json={}) == {"ok": True}
    assert seen == ["https://claude.ai/api/x"]


# --- streaming through the waist ----------------------------------------------------
def test_stream_events_uses_the_waist_and_resumes():
    frames = (
        'event: client_event\nid: 4\ndata: {"sequence_num": 4, "payload": {"type": "assistant", '
        '"message": {"role": "assistant", "content": [{"type": "text", "text": "hey"}]}}}\n\n'
    )
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, content=frames.encode(), headers={"content-type": "text/event-stream"})

    with _client(handler) as rc:
        events = list(rc.stream_events("cse_1", from_sequence_num=3, reconnect=False))
    assert [e.text() for e in events] == ["hey"]
    req = seen[0]
    assert req.url.params["from_sequence_num"] == "3"
    assert req.headers["last-event-id"] == "3" and req.headers["accept"] == "text/event-stream"
    assert req.headers["x-organization-uuid"] == "org-1"


def test_stream_events_raises_api_error_on_refusal():
    with _client(lambda r: httpx.Response(404, text="gone")) as rc, pytest.raises(APIError) as exc:
        list(rc.stream_events("cse_1", reconnect=False))
    assert exc.value.body == "gone"


def test_explicit_credentials_object_still_works():
    seen = []
    creds = OAuthCredentials(access_token="obj")
    with RemoteControlClient(creds, "o", transport=httpx.MockTransport(
        lambda r: seen.append(r.headers["authorization"]) or httpx.Response(200, json={})
    )) as rc:
        rc.mark_read("cse_1", event_id="e1")
    assert seen == ["Bearer obj"]


def test_cswap_live_account_is_the_default_login(tmp_path, monkeypatch):
    _write_creds(tmp_path)
    monkeypatch.setattr("claude_rc.credentials.cswap_current_slot", lambda: "2")
    auth = resolve_auth(slot_token=lambda s: (f"live-{s}", 9999999999999))
    assert (auth.credentials.access_token, auth.origin) == ("live-2", "cswap slot 2")
