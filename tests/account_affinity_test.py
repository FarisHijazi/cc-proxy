"""Optional shared affinity: toy credentials, no inference/auth network calls."""

import asyncio
import inspect
import json
import os
import threading
import time
from uuid import uuid4

import pytest
from account_inject_test import addon, inference, module


@pytest.fixture(autouse=True)
def isolated_credentials(monkeypatch):
    monkeypatch.delenv("CCP_AFFINITY_DSN", raising=False)
    monkeypatch.delenv("CCP_AFFINITY_NAMESPACE", raising=False)
    monkeypatch.setattr(module, "renew_in_background", lambda *args: None)
    monkeypatch.setattr(module, "slot_token", lambda slot: pytest.fail("real credentials forbidden"))


def validation_connection():
    import psycopg

    try:
        conn = psycopg.connect(os.environ["CCP_HA_TEST_DSN"])
    except psycopg.Error:
        conn = None
    if conn is None:
        pytest.fail("isolated validation database unavailable (connection detail suppressed)", pytrace=False)
    return conn


@pytest.fixture
def shared(monkeypatch):
    dsn = os.environ.get("CCP_HA_TEST_DSN")
    if not dsn:
        pytest.skip("CCP_HA_TEST_DSN must name the isolated validation database")
    pytest.importorskip("psycopg")
    namespace = "test-" + uuid4().hex
    with validation_connection() as conn:
        assert conn.info.dbname == "ccp_ha_validation", "test only in the dedicated validation DB"
        conn.execute((module.Path(module.__file__).with_name("account_affinity.sql")).read_text())
    monkeypatch.setenv("CCP_AFFINITY_DSN", dsn)
    monkeypatch.setenv("CCP_AFFINITY_NAMESPACE", namespace)
    workers = []

    def make(strategy="round-robin"):
        inject = addon(monkeypatch, [])
        inject.accounts = {"a": "1", "b": "2", "c": "3"}
        inject.strategy = strategy
        workers.append(inject)
        return inject

    yield make, namespace
    for inject in workers:
        inject.done()
    with validation_connection() as conn:
        conn.execute("DELETE FROM ccp_affinity WHERE namespace = %s", (namespace,))


def send(inject, session="s1", alias="a", pinned=False, path="/v1/messages"):
    flow = inference(f"Bearer proxy-account-{alias}" + ("+pin" if pinned else ""), path)
    if session:
        flow.request.headers[module.SESSION_HEADER] = session
    return flow


async def dispatch(inject, flow):
    await inject.requestheaders(flow)
    if flow.response is not None:
        return flow.response.status_code
    return json.loads(flow.request.headers[module.POOL_HEADER])[0]["alias"]


@pytest.mark.parametrize("strategy", ["session", "prefer", "round-robin", "least-sessions"])
def test_two_workers_preserve_policy_and_pin(shared, strategy):
    make, _ = shared
    a, b = make(strategy), make(strategy)
    a.prefer = b.prefer = "c"

    async def run():
        actual = [await dispatch(worker, send(worker, session)) for worker, session in
                  ((a, "s1"), (b, "s2"), (b, "s1"), (a, "s3"))]
        expected = {"session": ["a"] * 4, "prefer": ["c"] * 4,
                    "round-robin": ["a", "b", "a", "c"], "least-sessions": ["a", "b", "a", "c"]}
        assert actual == expected[strategy]
        assert await dispatch(b, send(b, "pinned", "b", True)) == "b"
        assert await dispatch(a, send(a, None, "b")) == ("c" if strategy == "prefer" else "b")

    asyncio.run(run())


def test_concurrent_first_assignment_is_atomic_and_survives_new_worker(shared):
    make, _ = shared
    a, b = make(), make()

    async def run():
        first = await asyncio.gather(*(dispatch(w, send(w)) for w in [a, b] * 8))
        assert first == ["a"] * 16
        assert await dispatch(b, send(b, "next")) == "b", "one first assignment advances once"
        fresh = make()
        assert await dispatch(fresh, send(fresh)) == "a"
        assert await dispatch(fresh, send(fresh, "third")) == "c"

    asyncio.run(run())


def test_concurrent_distinct_sessions_share_least_session_counts(shared):
    make, _ = shared
    workers = [make("least-sessions"), make("least-sessions")]

    async def run():
        chosen = await asyncio.gather(*(dispatch(workers[i % 2], send(workers[i % 2], f"s{i}")) for i in range(12)))
        assert [chosen.count(alias) for alias in ("a", "b", "c")] == [4, 4, 4]

    asyncio.run(run())


def test_expiry_disabled_removed_alias_and_count_tokens(shared):
    from psycopg.types.json import Jsonb

    make, namespace = shared
    a, b = make("least-sessions"), make("least-sessions")
    assert asyncio.run(dispatch(a, send(a))) == "a"
    with validation_connection() as conn:
        conn.execute("UPDATE ccp_affinity SET state = %s WHERE namespace = %s", (
            Jsonb({"sessions": {"s1": ["a", time.time() - 3601]}, "turn": -1}), namespace,
        ))
    b.disabled = {"b"}
    assert asyncio.run(dispatch(b, send(b, "s2"))) == "a", "expired sessions no longer count"
    assert asyncio.run(dispatch(a, send(a, "s2", path="/v1/messages/count_tokens"))) == "a"
    del a.accounts["a"]
    assert asyncio.run(dispatch(a, send(a, "s2", "b"))) == "b", "removed aliases are reassigned"
    b.strategy, b.prefer = "prefer", "b"
    assert asyncio.run(dispatch(b, send(b, "s3", "c"))) == "c", "disabled preference uses caller"


def test_namespace_isolation(shared):
    make, _ = shared
    a, b = make(), make()
    b.affinity.namespace += "-other"
    try:
        assert asyncio.run(dispatch(a, send(a))) == "a"
        assert asyncio.run(dispatch(a, send(a, "s2"))) == "b"
        assert asyncio.run(dispatch(b, send(b, "s2"))) == "a"
    finally:
        with validation_connection() as conn:
            conn.execute("DELETE FROM ccp_affinity WHERE namespace = %s", (b.affinity.namespace,))


@pytest.mark.parametrize("pinned", [False, True])
def test_unreachable_database_fails_closed_without_credentials(monkeypatch, pinned):
    monkeypatch.setenv("CCP_AFFINITY_DSN", "host=127.0.0.1 port=1 dbname=unreachable user=invalid")
    monkeypatch.setenv("CCP_AFFINITY_NAMESPACE", "unit")
    inject = module.AccountInject()
    inject.accounts = {"a": "1"}
    try:
        flow = send(inject, pinned=pinned)
        assert asyncio.run(dispatch(inject, flow)) == 503
        assert "authorization" not in flow.request.headers
        assert module.POOL_HEADER not in flow.request.headers
        assert b"127.0.0.1" not in flow.response.content
        assert not inject.fetching
    finally:
        inject.done()


def test_affinity_overrides_loss_tolerant_session_default(shared, monkeypatch):
    import psycopg

    make, _ = shared
    inject = make()
    connect = psycopg.connect
    observed = []

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def __enter__(self):
            self.conn.__enter__()
            observed.append(self.conn.execute("SHOW synchronous_commit").fetchone()[0])
            return self

        def __exit__(self, *args):
            return self.conn.__exit__(*args)

        def execute(self, sql, params=None):
            result = self.conn.execute(sql, params)
            if sql == "SET LOCAL synchronous_commit = on":
                observed.append(self.conn.execute("SHOW synchronous_commit").fetchone()[0])
            return result

    def loss_tolerant(*args, **kwargs):
        kwargs["options"] = kwargs.get("options", "") + " -c synchronous_commit=off"
        return Connection(connect(*args, **kwargs))

    with monkeypatch.context() as patch:
        patch.setattr(psycopg, "connect", loss_tolerant)
        assert asyncio.run(dispatch(inject, send(inject))) == "a"
    assert observed == ["off", "on"]


def test_database_loss_never_reuses_process_local_assignment(shared):
    make, _ = shared
    inject = make()
    assert asyncio.run(dispatch(inject, send(inject))) == "a"
    original = inject.affinity.dsn
    inject.affinity.dsn = "host=127.0.0.1 port=1 dbname=unreachable user=invalid"
    try:
        assert asyncio.run(dispatch(inject, send(inject))) == 503
    finally:
        inject.affinity.dsn = original
    assert asyncio.run(dispatch(inject, send(inject))) == "a"
    assert asyncio.run(dispatch(inject, send(inject, "s2"))) == "b"


def test_mock_upstream_receives_identical_session_owner_for_counts_and_messages(shared):
    make, _ = shared
    a, b = make(), make()
    seen = []

    def upstream(flow):
        pool = json.loads(flow.request.headers[module.POOL_HEADER])
        seen.append([entry["alias"] for entry in pool if entry["primary"]])
        assert flow.request.headers["authorization"] == "Bearer real-1"
        return module.http.Response.make(200, b'{"mock":true}', {"content-type": "application/json"})

    async def run():
        for worker, path in ((a, "/v1/messages"), (b, "/v1/messages/count_tokens")):
            flow = send(worker, path=path)
            assert await dispatch(worker, flow) == "a"
            flow.response = upstream(flow)
            worker.responseheaders(flow)
            assert flow.response.status_code == 200

    asyncio.run(run())
    assert seen == [["a"], ["a"]]


def test_commit_failure_is_sanitized_and_not_forwarded(monkeypatch):
    import psycopg

    monkeypatch.setenv("CCP_AFFINITY_DSN", "unused")
    monkeypatch.setenv("CCP_AFFINITY_NAMESPACE", "unit")
    inject = addon(monkeypatch, [])

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            raise psycopg.OperationalError("private connection detail")

        def execute(self, sql, params=None):
            self.sql = sql
            return self

        def fetchone(self):
            if "SELECT state" in self.sql:
                return ({"sessions": {}, "turn": -1},)
            return (time.time(),)

    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: Connection())
    try:
        flow = send(inject, alias="personal")
        assert asyncio.run(dispatch(inject, flow)) == 503
        assert b"private connection detail" not in flow.response.content
        assert module.POOL_HEADER not in flow.request.headers
        assert not inject.fetching
    finally:
        inject.done()


def test_worker_does_not_block_shared_event_loop(monkeypatch):
    monkeypatch.setenv("CCP_AFFINITY_DSN", "unused")
    monkeypatch.setenv("CCP_AFFINITY_NAMESPACE", "unit")
    inject = addon(monkeypatch, [])
    inject.cache["personal"] = ("toy-control-token", time.time() * 1000 + 3_600_000)
    inject.affinity_slots = threading.BoundedSemaphore(1)
    entered, release = threading.Event(), threading.Event()

    def blocked(*args):
        entered.set()
        assert release.wait(5)
        raise module.AffinityUnavailable()

    monkeypatch.setattr(inject.affinity, "choose", blocked)

    async def run():
        task = asyncio.create_task(inject.requestheaders(send(inject, alias="personal")))
        try:
            while not entered.is_set():
                await asyncio.sleep(0.001)
            assert not task.done()
            control = send(inject, alias="personal", path="/v1/code/sessions/test/worker/events")
            await asyncio.wait_for(inject.requestheaders(control), 0.2)
            assert control.response is None
            assert control.request.headers["authorization"] == "Bearer toy-control-token"
            saturated = send(inject, alias="personal")
            assert await asyncio.wait_for(dispatch(inject, saturated), 0.2) == 503
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await dispatch(inject, send(inject, alias="personal")) == 503, "cancel does not free a running slot"
        finally:
            release.set()

    try:
        assert inspect.iscoroutinefunction(inject.requestheaders)
        asyncio.run(asyncio.wait_for(run(), 2))
    finally:
        release.set()
        inject.done()


def test_opt_in_is_explicit_and_complete(monkeypatch):
    inject = module.AccountInject()
    assert inject.affinity is None and not inspect.iscoroutinefunction(inject.requestheaders)
    monkeypatch.setenv("CCP_AFFINITY_NAMESPACE", "unit")
    with pytest.raises(module.exceptions.OptionsError):
        module.AccountInject()
