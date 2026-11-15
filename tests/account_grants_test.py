"""Shared Postgres logins: toy credentials and a fake provider, never a real auth endpoint."""

import base64
import json
import os
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
                 "CCP_GRANTS_KEY_FILE"):
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
    a.seed("a", provider.issue(1))
    results = [None, None]

    def run(i, grants):
        results[i] = grants.token("a")

    threads = [threading.Thread(target=run, args=(i, g)) for i, g in enumerate((a, b))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
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
def test_live_default_login_cannot_be_a_shared_login(monkeypatch, tmp_path, live):
    inject = opted_in(monkeypatch, tmp_path, "unused", "unit")
    monkeypatch.setattr(module, "live_login", lambda: live)
    accounts = tmp_path / "accounts.json"
    accounts.write_text(json.dumps({"accounts": {"a@example.com": "1"}}))
    with pytest.raises(module.LiveLoginShared, match="live Claude login"):
        inject.read_accounts(accounts)
    monkeypatch.setattr(module, "live_login", lambda: ("2", "b@example.com"))
    inject.read_accounts(accounts)
    assert inject.accounts == {"a@example.com": "1"}
