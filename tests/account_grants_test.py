"""Shared Postgres logins: toy credentials and a fake provider, never a real auth endpoint."""

import base64
import json
import os
import runpy
import sys
import threading
import time
from uuid import uuid4

import pytest
from account_inject_test import module, request
from claude_swap.oauth import RefreshOutcome

KEY = bytes(range(32))
HOUR_MS = 3_600_000


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for name in ("CCP_AFFINITY_DSN", "CCP_AFFINITY_NAMESPACE", "CCP_GRANTS_DSN", "CCP_GRANTS_NAMESPACE",
                 "CCP_GRANTS_KEY_FILE", "CCP_GRANTS_CODEX_ACCOUNT", "CCP_CODEX_AUTH_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(module, "renew_in_background", lambda *args: pytest.fail("cswap renewal forbidden"))
    monkeypatch.setattr(module, "slot_token", lambda slot: pytest.fail("cswap credentials forbidden"))
    monkeypatch.setattr(module, "live_login", lambda: (None, None))


def login(access, refresh, expires_ms):
    return json.dumps({"claudeAiOauth": {"accessToken": access, "refreshToken": refresh, "expiresAt": expires_ms}})


def fake_provider(delay=0.0):
    """A token endpoint that rotates each refresh token once, like the real one."""
    lock = threading.Lock()

    def refresh(credentials):
        token = json.loads(credentials)["claudeAiOauth"]["refreshToken"]
        with lock:
            refresh.posts.append(token)
            alive = token in refresh.live
            refresh.live.discard(token)
            n = len(refresh.posts)
        time.sleep(refresh.delay)
        if not alive:
            return RefreshOutcome(None, "invalid_grant")
        refresh.live.add(f"refresh-new{n}")
        return RefreshOutcome(login(f"access-new{n}", f"refresh-new{n}", time.time() * 1000 + HOUR_MS), None)

    def issue(n):
        refresh.live.add(f"refresh-{n}")
        return login(f"access-{n}", f"refresh-{n}", time.time() * 1000 - 1)

    refresh.live, refresh.posts, refresh.delay, refresh.issue = set(), [], delay, issue
    return refresh


def key_file(tmp_path, mode=0o600):
    path = tmp_path / "grants.key"
    path.write_text(base64.b64encode(KEY).decode())
    path.chmod(mode)
    return path


def connection():
    import psycopg

    try:
        return psycopg.connect(os.environ["CCP_HA_TEST_DSN"], autocommit=True)
    except psycopg.Error:
        pytest.fail("isolated validation database unavailable (connection detail suppressed)", pytrace=False)


@pytest.fixture
def db(tmp_path):
    dsn = os.environ.get("CCP_HA_TEST_DSN")
    if not dsn:
        pytest.skip("CCP_HA_TEST_DSN must name the isolated validation database")
    pytest.importorskip("psycopg")
    namespace = "test-" + uuid4().hex
    with connection() as conn:
        assert conn.info.dbname == "ccp_ha_validation", "test only in the dedicated validation DB"
        conn.execute((module.Path(module.__file__).with_name("account_grants.sql")).read_text())
    provider = fake_provider()

    def host(name, refresh=provider):
        grants = module.PostgresGrants(dsn, namespace, KEY, tmp_path / name, refresh=refresh)
        grants.lease_wait_s = 3
        return grants

    yield host, provider, namespace, dsn
    with connection() as conn:
        conn.execute("DELETE FROM ccp_grants WHERE namespace = %s", (namespace,))


def row(namespace, account="a"):
    with connection() as conn:
        return conn.execute(
            "SELECT ciphertext, generation, state, lease_holder FROM ccp_grants WHERE namespace = %s AND account = %s",
            (namespace, account),
        ).fetchone()


def test_two_hosts_racing_a_refresh_post_once(db):
    host, provider, namespace, _ = db
    provider.delay = 0.5
    a, b = host("a"), host("b")
    a.lease_wait_s = b.lease_wait_s = 15
    a.seed("a", provider.issue(1))
    results = [None, None]

    def run(i, grants):
        results[i] = grants.token("a")

    threads = [threading.Thread(target=run, args=(i, g)) for i, g in enumerate((a, b))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert provider.posts == ["refresh-1"]
    assert results[0] == results[1] and results[0][0] == "access-new1"
    assert row(namespace)[1:] == (2, "ok", None)


def test_crash_after_post_before_commit_is_recovered_from_the_journal(db, monkeypatch):
    host, provider, namespace, _ = db
    a = host("a")
    a.seed("a", provider.issue(1))
    monkeypatch.setattr(a, "commit", lambda *args: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        a.token("a")
    assert row(namespace)[1] == 1 and row(namespace)[3] is not None, "G still leased by the crashed host"
    assert (a.journal("a").stat().st_mode & 0o777) == 0o600
    with pytest.raises(module.GrantsUnavailable, match="renewed elsewhere"):
        host("b").token("a")
    restarted = host("a")
    restarted.reconcile_all()
    assert row(namespace)[1:] == (2, "ok", None)
    assert host("b").token("a")[0] == "access-new1"
    assert provider.posts == ["refresh-1"]


def test_lost_commit_is_repaired_by_the_journal(db):
    host, provider, namespace, _ = db
    a, b = host("a"), host("b")
    a.seed("a", provider.issue(1))
    before = row(namespace)
    assert a.token("a")[0] == "access-new1"
    with connection() as conn:
        conn.execute(
            "UPDATE ccp_grants SET ciphertext = %s, generation = %s WHERE namespace = %s AND account = 'a'",
            (before[0], before[1], namespace),
        )
    with pytest.raises(module.GrantsUnavailable, match="needs a new login"):
        b.token("a")
    assert row(namespace)[2] == "needs-reconcile"
    assert a.token("a")[0] == "access-new1", "the refreshing host writes G+1 back"
    assert b.token("a")[0] == "access-new1"
    assert provider.posts == ["refresh-1", "refresh-1"]
    assert row(namespace)[1:3] == (2, "ok")


def test_invalid_grant_marks_needs_reconcile_and_never_loops(db):
    host, provider, namespace, _ = db
    a = host("a")
    a.seed("a", login("access-1", "revoked", time.time() * 1000 - 1))
    for _ in range(3):
        with pytest.raises(module.GrantsUnavailable, match="needs a new login"):
            a.token("a")
    assert provider.posts == ["revoked"]
    assert row(namespace)[1:] == (1, "needs-reconcile", None)
    assert a.seed("a", provider.issue(2), replace=True) == 2
    assert a.token("a")[0] == "access-new2"


def test_tampered_ciphertext_or_wrong_associated_data_fails_closed(db):
    host, provider, namespace, _ = db
    a = host("a")
    a.seed("a", login("access-1", "refresh-1", time.time() * 1000 + HOUR_MS))
    assert a.token("a")[0] == "access-1"
    good = row(namespace)[0]
    tampered = bytearray(good)
    tampered[-1] ^= 1
    for account, ciphertext, generation in (("a", bytes(tampered), 1), ("a", good, 7), ("b", good, 1)):
        with connection() as conn:
            conn.execute(
                "UPDATE ccp_grants SET account = %s, ciphertext = %s, generation = %s WHERE namespace = %s",
                (account, ciphertext, generation, namespace),
            )
        with pytest.raises(module.GrantsUnavailable, match="integrity"):
            a.token(account)
    assert provider.posts == []


def test_associated_data_binds_namespace_account_and_generation():
    sealed = module._grants["seal"](KEY, "ns", "a", 1, "secret")
    assert module._grants["unseal"](KEY, "ns", "a", 1, sealed) == "secret"
    for args in (("ns2", "a", 1), ("ns", "b", 1), ("ns", "a", 2)):
        with pytest.raises(module.GrantsUnavailable):
            module._grants["unseal"](KEY, *args, sealed)
    with pytest.raises(module.GrantsUnavailable):
        module._grants["unseal"](bytes(32), "ns", "a", 1, sealed)


def wait_fetched(inject, alias):
    future = inject.fetching.get(alias)
    if future is not None:
        future.exception(timeout=10)


def served(inject, alias="a"):
    for _ in range(3):
        flow = request(f"Bearer proxy-account-{alias}")
        inject.requestheaders(flow)
        if flow.response is None or b"still being fetched" not in flow.response.content:
            return flow
        wait_fetched(inject, alias)
    return flow


def opted_in(monkeypatch, tmp_path, dsn, namespace):
    monkeypatch.setenv("CCP_GRANTS_DSN", dsn)
    monkeypatch.setenv("CCP_GRANTS_NAMESPACE", namespace)
    monkeypatch.setenv("CCP_GRANTS_KEY_FILE", str(key_file(tmp_path)))
    inject = module.AccountInject()
    inject.accounts = {"a": "1"}
    return inject


def test_proxy_serves_the_shared_login_and_renews_a_rejected_one_once(db, monkeypatch, tmp_path):
    host, provider, namespace, dsn = db
    host("seed").seed("a", login("access-1", "refresh-1", time.time() * 1000 + HOUR_MS))
    provider.live.add("refresh-1")
    inject = opted_in(monkeypatch, tmp_path, dsn, namespace)
    inject.grants.refresh = provider
    assert served(inject).request.headers["authorization"] == "Bearer access-1"
    for _ in range(2):
        inject.rejected("a", "access-1")
    assert served(inject).request.headers["authorization"] == "Bearer access-new1"
    assert provider.posts == ["refresh-1"]


def test_needs_reconcile_answers_a_sanitized_503(db, monkeypatch, tmp_path):
    host, _, namespace, dsn = db
    host("seed").seed("a", login("access-1", "revoked", time.time() * 1000 - 1))
    inject = opted_in(monkeypatch, tmp_path, dsn, namespace)
    inject.grants.refresh = fake_provider()
    flow = served(inject)
    assert flow.response.status_code == 503
    assert "authorization" not in flow.request.headers
    assert b"needs a new login" in flow.response.content or b"unavailable" in flow.response.content


def test_unreachable_store_fails_closed_without_detail(monkeypatch, tmp_path):
    inject = opted_in(monkeypatch, tmp_path, "host=127.0.0.1 port=1 dbname=unreachable user=invalid", "unit")
    flow = served(inject)
    assert flow.response.status_code == 503
    assert "authorization" not in flow.request.headers
    assert b"127.0.0.1" not in flow.response.content and b"unreachable" not in flow.response.content


def test_feature_off_keeps_slot_token_and_cswap_renew(monkeypatch):
    fetched, renewed = [], threading.Event()
    monkeypatch.setattr(module, "slot_token", lambda slot: fetched.append(slot) or ("real-1", time.time() * 1000 + HOUR_MS))
    monkeypatch.setattr(module, "renew_in_background", lambda slot, stale: renewed.set() if (slot, stale) == ("1", "real-1") else None)
    inject = module.AccountInject()
    inject.accounts = {"a": "1"}
    assert inject.grants is None
    assert served(inject).request.headers["authorization"] == "Bearer real-1"
    inject.rejected("a", "real-1")
    assert renewed.wait(5) and fetched == ["1"] and not inject.stale


def test_opt_in_is_complete_and_key_file_private(monkeypatch, tmp_path):
    monkeypatch.setenv("CCP_GRANTS_DSN", "unused")
    with pytest.raises(module.exceptions.OptionsError, match="CCP_GRANTS_NAMESPACE"):
        module.AccountInject()
    monkeypatch.setenv("CCP_GRANTS_NAMESPACE", "unit")
    monkeypatch.setenv("CCP_GRANTS_KEY_FILE", str(key_file(tmp_path, 0o644)))
    with pytest.raises(module.exceptions.OptionsError, match="private"):
        module.AccountInject()


@pytest.mark.parametrize("live", [("1", None), ("9", "A@example.com")])
def test_live_login_of_a_shared_alias_is_adopted_not_refused(monkeypatch, tmp_path, live):
    inject = opted_in(monkeypatch, tmp_path, "unused", "unit")
    adopted = []
    monkeypatch.setattr(module, "adopt_live_login", lambda grants, alias: adopted.append(alias))
    monkeypatch.setattr(module, "live_login", lambda: live)
    accounts = tmp_path / "accounts.json"
    accounts.write_text(json.dumps({"accounts": {"a@example.com": "1"}}))
    inject.read_accounts(accounts)
    assert adopted == ["a@example.com"] and inject.accounts == {"a@example.com": "1"}
    monkeypatch.setattr(module, "live_login", lambda: ("2", "b@example.com"))
    inject.read_accounts(accounts)
    assert adopted == ["a@example.com"]


def test_a_failed_adoption_still_serves(monkeypatch, tmp_path):
    inject = opted_in(monkeypatch, tmp_path, "unused", "unit")
    monkeypatch.setattr(module, "adopt_live_login", lambda grants, alias: (_ for _ in ()).throw(OSError("db down")))
    monkeypatch.setattr(module, "live_login", lambda: ("1", None))
    accounts = tmp_path / "accounts.json"
    accounts.write_text(json.dumps({"accounts": {"a": "1"}}))
    inject.read_accounts(accounts)
    assert inject.accounts == {"a": "1"}


class SeedRecorder:
    def __init__(self, generation):
        self.generation, self.seeded = generation, []

    def seed(self, account, credentials, replace=False, newer_only=False):
        self.seeded.append((account, json.loads(credentials), replace, newer_only))
        return self.generation


def claude_home(tmp_path, credentials, config):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text(json.dumps(credentials))
    (home / ".claude.json").write_text(json.dumps(config))
    return home


@pytest.mark.parametrize("generation", [3, None])
def test_adoption_moves_only_the_claude_login_off_the_host(tmp_path, generation):
    live = {"accessToken": "access-9", "refreshToken": "refresh-9", "expiresAt": 1}
    home = claude_home(tmp_path, {"claudeAiOauth": live, "mcpOAuth": {"server": "kept"}},
                       {"oauthAccount": {"emailAddress": "a@example.com"}, "theme": "dark"})
    grants = SeedRecorder(generation)
    module.adopt_live_login(grants, "a@example.com", home)
    assert grants.seeded == [("a@example.com", {"claudeAiOauth": live}, True, True)]
    assert json.loads((home / ".claude" / ".credentials.json").read_text()) == {"mcpOAuth": {"server": "kept"}}
    assert json.loads((home / ".claude.json").read_text()) == {"theme": "dark"}
    backup, = (home / ".local" / "state" / "ccp-adopted").iterdir()
    assert json.loads(backup.read_text()) == {"claudeAiOauth": live} and backup.stat().st_mode & 0o777 == 0o600


def test_adoption_of_an_identity_without_a_login_only_drops_the_identity(tmp_path):
    home = claude_home(tmp_path, {"mcpOAuth": {}}, {"oauthAccount": {"emailAddress": "a@example.com"}})
    grants = SeedRecorder(1)
    module.adopt_live_login(grants, "a@example.com", home)
    assert grants.seeded == [] and json.loads((home / ".claude.json").read_text()) == {}
    assert not (home / ".local").exists()


def test_seed_newer_only_keeps_the_stored_login_unless_the_copy_expires_later(db):
    host, _, namespace, _ = db
    a = host("a")
    now = time.time() * 1000
    a.seed("a", login("access-1", "refresh-1", now + 2 * HOUR_MS))
    assert a.seed("a", login("access-old", "refresh-old", now + HOUR_MS), replace=True, newer_only=True) is None
    assert row(namespace)[1] == 1
    assert a.seed("a", login("access-new", "refresh-new", now + 3 * HOUR_MS), replace=True, newer_only=True) == 2
    assert a.token("a") == ("access-new", now + 3 * HOUR_MS)


def jwt(exp_ms, label):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": int(exp_ms // 1000), "jti": label}).encode()).decode()
    return f"eyJhbGciOiJub25lIn0.{payload.rstrip('=')}.sig"


def codex_auth(label, refresh, expires_ms):
    return json.dumps({
        "auth_mode": "chatgpt", "OPENAI_API_KEY": None, "last_refresh": "2026-01-01T00:00:00Z",
        "tokens": {"id_token": "id.toy.sig", "access_token": jwt(expires_ms, label), "refresh_token": refresh,
                   "account_id": "acct-toy"},
    })


def fake_codex_endpoint(delay=0.0):
    """Stands in for the Codex token endpoint: each refresh token works once."""
    lock = threading.Lock()

    def post(form):
        assert form["client_id"] == module._grants["CODEX_CLIENT_ID"] and form["grant_type"] == "refresh_token"
        with lock:
            post.posts.append(form["refresh_token"])
            alive = form["refresh_token"] in post.live
            post.live.discard(form["refresh_token"])
            n = len(post.posts)
        time.sleep(post.delay)
        if not alive:
            return 401, {"error": {"code": "refresh_token_reused"}}
        post.live.add(f"codex-refresh-new{n}")
        return 200, {"access_token": jwt(time.time() * 1000 + HOUR_MS, f"new{n}"),
                     "refresh_token": f"codex-refresh-new{n}", "id_token": f"id.new{n}.sig", "expires_in": 3600}

    def issue(n, expires_ms=None):
        post.live.add(f"codex-refresh-{n}")
        return codex_auth(f"old{n}", f"codex-refresh-{n}", time.time() * 1000 - 1 if expires_ms is None else expires_ms)

    post.live, post.posts, post.delay, post.issue = set(), [], delay, issue
    return post


@pytest.fixture
def codex(db, tmp_path):
    _, _, namespace, dsn = db
    endpoint = fake_codex_endpoint()

    def refresh(credentials):
        return module._grants["codex_refresh"](credentials, post=endpoint)

    def keeper(name, auth_file=None):
        grants = module.PostgresGrants(dsn, namespace, KEY, tmp_path / name, refresh=refresh,
                                       parse=module._grants["codex_login"])
        grants.lease_wait_s = 3
        return module.CodexKeeper(grants, "work", auth_file or tmp_path / f"{name}-auth.json")

    yield keeper, endpoint, namespace


def access_of(path):
    return json.loads(path.read_text())["tokens"]["access_token"]


def test_codex_two_hosts_racing_post_once_and_write_access_only_files(codex):
    keeper, endpoint, namespace = codex
    endpoint.delay = 0.5
    a, b = keeper("a"), keeper("b")
    a.grants.lease_wait_s = b.grants.lease_wait_s = 15
    a.grants.seed("codex:work", endpoint.issue(1))
    threads = [threading.Thread(target=k.keep) for k in (a, b)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert endpoint.posts == ["codex-refresh-1"]
    assert access_of(a.path) == access_of(b.path)
    assert json.loads(base64.urlsafe_b64decode(access_of(a.path).split(".")[1] + "=="))["jti"] == "new1"
    for path in (a.path, b.path):
        doc = json.loads(path.read_text())
        assert "refresh_token" not in doc["tokens"] and b"codex-refresh" not in path.read_bytes()
        assert doc["tokens"]["id_token"] == "id.new1.sig" and doc["auth_mode"] == "chatgpt"
        assert (path.stat().st_mode & 0o777) == 0o600
    assert row(namespace, "codex:work")[1:] == (2, "ok", None)


def test_codex_crash_after_post_is_recovered_from_the_journal(codex, monkeypatch):
    keeper, endpoint, namespace = codex
    a = keeper("a")
    a.grants.seed("codex:work", endpoint.issue(1))
    monkeypatch.setattr(a.grants, "commit", lambda *args: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        a.keep()
    assert not a.path.exists()
    assert row(namespace, "codex:work")[1] == 1
    with pytest.raises(module.GrantsUnavailable, match="renewed elsewhere"):
        keeper("b").keep()
    restarted = keeper("a")
    restarted.keep()
    assert row(namespace, "codex:work")[1:] == (2, "ok", None)
    b = keeper("b")
    b.keep()
    assert access_of(b.path) == access_of(restarted.path)
    assert endpoint.posts == ["codex-refresh-1"]


def test_codex_invalid_grant_marks_needs_reconcile_without_looping(codex):
    keeper, endpoint, namespace = codex
    a = keeper("a")
    a.grants.seed("codex:work", codex_auth("old", "codex-revoked", time.time() * 1000 - 1))
    for _ in range(3):
        with pytest.raises(module.GrantsUnavailable, match="needs a new login"):
            a.keep()
    assert endpoint.posts == ["codex-revoked"]
    assert row(namespace, "codex:work")[1:] == (1, "needs-reconcile", None)
    assert not a.path.exists()


def test_codex_valid_login_is_written_without_a_post_and_rewritten_after_renewal(codex):
    keeper, endpoint, _ = codex
    a = keeper("a")
    a.grants.seed("codex:work", endpoint.issue(1, time.time() * 1000 + HOUR_MS))
    a.keep()
    first = access_of(a.path)
    a.path.unlink()
    a.keep()
    assert access_of(a.path) == first and endpoint.posts == []


def test_codex_refresh_maps_provider_answers_without_network():
    refresh = module._grants["codex_refresh"]
    login = codex_auth("old", "codex-refresh-1", 0)
    sent = []
    outcome = refresh(login, post=lambda form: sent.append(form) or (200, {
        "access_token": "opaque-access", "refresh_token": "codex-refresh-2", "expires_in": 60}))
    assert sent == [{"client_id": "app_EMoamEEZ73f0CkXaXp7hrann", "grant_type": "refresh_token",
                     "refresh_token": "codex-refresh-1"}]
    parsed = module._grants["codex_login"](outcome.credentials)
    assert parsed.access == "opaque-access" and parsed.refresh == "codex-refresh-2"
    assert abs(parsed.expires - (time.time() * 1000 + 60_000)) < 5_000, "no JWT exp: expiry from expires_in"
    assert json.loads(outcome.credentials)["tokens"]["id_token"] == "id.toy.sig"
    for status, body, error in (
        (400, {"error": "invalid_grant"}, "invalid_grant"),
        (401, None, "invalid_grant"),
        (400, {"error": {"code": "refresh_token_expired"}}, "invalid_grant"),
        (503, None, "http_503"),
        (200, {"access_token": "only-access"}, "malformed_response"),
    ):
        assert refresh(login, post=lambda form: (status, body)) == (None, error)
    assert refresh(login, post=lambda form: (_ for _ in ()).throw(OSError("down"))) == (None, "network")
    assert refresh(codex_auth("old", "", 0), post=lambda form: pytest.fail("posted")) == (None, "no_refresh_token")


def grants_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CCP_GRANTS_DSN", "unused")
    monkeypatch.setenv("CCP_GRANTS_NAMESPACE", "unit")
    monkeypatch.setenv("CCP_GRANTS_KEY_FILE", str(key_file(tmp_path)))


def test_codex_feature_off_starts_no_keeper(monkeypatch, tmp_path):
    started = []
    monkeypatch.setattr(module.CodexKeeper, "run", lambda self: started.append(self))
    monkeypatch.delenv("CCP_GRANTS_CODEX_ACCOUNT", raising=False)
    assert module.AccountInject().codex is None
    grants_env(monkeypatch, tmp_path)
    assert module.AccountInject().codex is None
    time.sleep(0.1)
    assert started == []


def test_codex_opt_in_needs_grants_and_a_dedicated_auth_file(monkeypatch, tmp_path):
    started = threading.Event()
    monkeypatch.setattr(module.CodexKeeper, "run", lambda self: started.set())
    monkeypatch.setenv("CCP_GRANTS_CODEX_ACCOUNT", "work")
    monkeypatch.setenv("CCP_CODEX_AUTH_FILE", str(tmp_path / "router-auth.json"))
    with pytest.raises(module.exceptions.OptionsError, match="CCP_GRANTS_"):
        module.AccountInject()
    grants_env(monkeypatch, tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "cli"))
    for bad in ("codex:work", "bad name"):
        monkeypatch.setenv("CCP_GRANTS_CODEX_ACCOUNT", bad)
        with pytest.raises(module.exceptions.OptionsError, match="CCP_GRANTS_CODEX_ACCOUNT"):
            module.AccountInject()
    monkeypatch.setenv("CCP_GRANTS_CODEX_ACCOUNT", "work")
    monkeypatch.setenv("CCP_CODEX_AUTH_FILE", str(tmp_path / "cli" / "auth.json"))
    with pytest.raises(module.exceptions.OptionsError, match="Codex CLI's own"):
        module.AccountInject()
    monkeypatch.setenv("CCP_CODEX_AUTH_FILE", str(tmp_path / "router-auth.json"))
    inject = module.AccountInject()
    assert inject.codex.account == "codex:work" and inject.codex.grants.parse is module._grants["codex_login"]
    assert started.wait(5)


def router_env_of(monkeypatch, tmp_path):
    launcher = module.runpy.run_path(str(module.Path(module.__file__).with_name("claude-account-router")))
    names = launcher["serve"].__globals__
    seen = []

    class Stop(Exception):
        pass

    def popen(command, env=None, **kwargs):
        seen.append(env)
        raise Stop

    monkeypatch.setattr(names["subprocess"], "Popen", popen)
    args = names["argparse"].Namespace(
        accounts_file=tmp_path / "accounts.json", port=1, listen_host="127.0.0.1", api_port=None,
        remote_control_backend=None, via=None, via_ca=None, router_bin="claude-codex",
    )
    try:
        launcher["serve"](args, tmp_path)
    except Stop:
        pass
    return seen[0]


def test_serve_turns_on_external_refresh_only_for_a_shared_codex_login(monkeypatch, tmp_path):
    monkeypatch.delenv("CCP_GRANTS_CODEX_ACCOUNT", raising=False)
    monkeypatch.delenv("CCP_CODEX_EXTERNAL_REFRESH", raising=False)
    assert "CCP_CODEX_EXTERNAL_REFRESH" not in router_env_of(monkeypatch, tmp_path)
    monkeypatch.setenv("CCP_GRANTS_CODEX_ACCOUNT", "work")
    monkeypatch.delenv("CCP_CODEX_AUTH_FILE", raising=False)
    with pytest.raises(ValueError, match="CCP_CODEX_AUTH_FILE"):
        router_env_of(monkeypatch, tmp_path)
    monkeypatch.setenv("CCP_CODEX_AUTH_FILE", str(tmp_path / "router-auth.json"))
    env = router_env_of(monkeypatch, tmp_path)
    assert env["CCP_CODEX_EXTERNAL_REFRESH"] == "1" and env["CCP_CODEX_AUTH_FILE"] == str(tmp_path / "router-auth.json")


def run_script(name, monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", [name, *argv])
    runpy.run_path(str(module.Path(module.__file__).with_name(name)), run_name="__main__")


@pytest.fixture
def operator(db, monkeypatch, tmp_path):
    """The operator scripts' environment, with cswap rooted in a temp home holding slot 1."""
    host, provider, namespace, dsn = db
    home = tmp_path / "home"
    home.mkdir()
    for name, value in (("HOME", home), ("XDG_DATA_HOME", home / "data"), ("CLAUDE_CONFIG_DIR", home / ".claude")):
        monkeypatch.setenv(name, str(value))
    monkeypatch.setenv("CCP_GRANTS_DSN", dsn)
    monkeypatch.setenv("CCP_GRANTS_NAMESPACE", namespace)
    monkeypatch.setenv("CCP_GRANTS_KEY_FILE", str(key_file(tmp_path)))
    from claude_swap.switcher import ClaudeAccountSwitcher
    from claude_swap.transfer import import_accounts

    switcher = ClaudeAccountSwitcher()
    assert str(switcher.backup_dir).startswith(str(home)), "cswap must never touch the real store"
    envelope = tmp_path / "identity.json"
    envelope.write_text(json.dumps({"version": 1, "encrypted": False, "accounts": [{
        "number": 1, "email": "a@example.com", "uuid": "u-toy", "organizationUuid": "",
        "credentials": json.loads(login("access-pre", "refresh-pre", time.time() * 1000 + HOUR_MS)),
        "config": {"oauthAccount": {"emailAddress": "a@example.com", "accountUuid": "u-toy"}},
    }]}))
    import_accounts(switcher, str(envelope))

    def slot_credentials():
        fresh = ClaudeAccountSwitcher()
        return fresh._read_account_credentials_ex("1", "a@example.com")[0]

    yield host, provider, namespace, slot_credentials


def test_export_to_cswap_then_reseed_round_trips_the_same_login(operator, monkeypatch, capsys):
    host, provider, namespace, slot_credentials = operator
    a = host("a")
    a.seed("a@example.com", provider.issue(1))
    assert a.token("a@example.com")[0] == "access-new1"
    capsys.readouterr()
    run_script("account_grants_export.py", monkeypatch, "a@example.com", "1")
    printed = capsys.readouterr().out
    assert "a@example.com generation 2 exported to cswap slot 1" in printed
    assert "access-" not in printed and "refresh-" not in printed
    exported = json.loads(slot_credentials())["claudeAiOauth"]
    assert (exported["accessToken"], exported["refreshToken"]) == ("access-new1", "refresh-new1")
    assert row(namespace, "a@example.com")[1:3] == (2, "exported")
    run_script("account_grants_seed.py", monkeypatch, "1", "a@example.com", "--replace")
    assert "refresh-" not in capsys.readouterr().out
    assert row(namespace, "a@example.com")[1:3] == (3, "ok")
    assert a.token("a@example.com")[0] == "access-new1"
    assert provider.posts == ["refresh-1"]


def test_an_exported_row_refuses_token_and_is_never_posted(db):
    host, provider, namespace, _ = db
    a = host("a")
    a.seed("a", provider.issue(1))
    credentials, generation = a.export("a")
    assert generation == 1 and json.loads(credentials)["claudeAiOauth"]["refreshToken"] == "refresh-1"
    for grants, stale in ((a, None), (host("b"), "access-1")):
        with pytest.raises(module.GrantsUnavailable, match="was exported"):
            grants.token("a", stale)
    a.write_journal("a", 5, time.time() * 1000 + HOUR_MS, module._grants["seal"](KEY, namespace, "a", 5, credentials))
    with pytest.raises(module.GrantsUnavailable, match="was exported"):
        a.token("a")
    assert provider.posts == [] and row(namespace)[1:] == (1, "exported", None), "a journal never revives it"
    assert a.export("a")[1] == 1, "exporting again is the retry after a failed write"


def test_export_waits_for_a_refresh_in_flight_and_refuses_a_lease_that_stays(db):
    host, provider, namespace, _ = db
    provider.delay = 1.0
    a, b = host("a"), host("b")
    a.seed("a", provider.issue(1))
    refreshing = threading.Thread(target=a.token, args=("a",))
    refreshing.start()
    deadline = time.monotonic() + 5
    while row(namespace)[3] is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert row(namespace)[3] is not None, "the refresh holds the lease"
    credentials, generation = b.export("a")
    refreshing.join(10)
    assert generation == 2 and json.loads(credentials)["claudeAiOauth"]["accessToken"] == "access-new1"
    assert provider.posts == ["refresh-1"]
    with connection() as conn:
        conn.execute(
            "UPDATE ccp_grants SET state = 'ok', lease_holder = 'elsewhere', lease_until = now() + interval '60 seconds'"
            " WHERE namespace = %s",
            (namespace,),
        )
    with pytest.raises(module.GrantsUnavailable, match="renewed elsewhere"):
        b.export("a")
    assert row(namespace)[1:3] == (2, "ok")


def test_codex_export_writes_the_full_login_privately_and_never_overwrites(codex, monkeypatch, tmp_path, capsys):
    keeper, endpoint, namespace = codex
    a = keeper("a")
    a.grants.seed("codex:work", endpoint.issue(1, time.time() * 1000 + HOUR_MS))
    monkeypatch.setenv("CCP_GRANTS_DSN", os.environ["CCP_HA_TEST_DSN"])
    monkeypatch.setenv("CCP_GRANTS_NAMESPACE", namespace)
    monkeypatch.setenv("CCP_GRANTS_KEY_FILE", str(key_file(tmp_path)))
    target = tmp_path / "cli" / "auth.json"
    target.parent.mkdir()
    run_script("account_grants_export.py", monkeypatch, "--codex", "work", str(target))
    printed = capsys.readouterr().out
    assert f"codex:work generation 1 exported to {target}." in printed and "codex-refresh" not in printed
    assert json.loads(target.read_text())["tokens"]["refresh_token"] == "codex-refresh-1"
    assert (target.stat().st_mode & 0o777) == 0o600
    assert row(namespace, "codex:work")[1:3] == (1, "exported")
    with pytest.raises(module.GrantsUnavailable, match="was exported"):
        a.keep()
    target.write_text("kept")
    with pytest.raises(SystemExit, match="exists; pass --force"):
        run_script("account_grants_export.py", monkeypatch, "--codex", "work", str(target))
    assert target.read_text() == "kept"
    run_script("account_grants_export.py", monkeypatch, "--codex", "work", str(target), "--force")
    assert json.loads(target.read_text())["tokens"]["refresh_token"] == "codex-refresh-1"
    assert endpoint.posts == []
