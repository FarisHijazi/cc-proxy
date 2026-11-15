"""Opt-in shared Claude logins: one refresh grant per account, held only in PostgreSQL.

Nobody POSTs generation G to the provider without a committed lease on G. A refresh is
lease (commit) -> POST (no transaction open) -> fsync an encrypted journal of G+1 -> CAS
commit on G. The journal stays until a newer generation is committed, so a commit lost to
a failover or a crash before the commit is written back by `reconcile`.
"""

import base64
import hashlib
import json
import os
import socket
import time
import uuid
from pathlib import Path

MARGIN_MS = 5 * 60 * 1000
# Far above the provider POST timeout, so a lease cannot expire mid-refresh.
LEASE_S = 120
POST_TIMEOUT_S = 10
LEASE_WAIT_S = 15
LEASE_POLL_S = 0.5
NEEDS_RECONCILE = "needs-reconcile"
# The grant itself is dead: only a new login (seed) or another host's journal can fix it.
DEAD = {"invalid_grant", "no_refresh_token"}


class GrantsUnavailable(ValueError):
    """The shared login cannot be served; the message never carries a DSN or a token."""


def read_key(path):
    path = Path(path)
    if path.stat().st_mode & 0o077:
        raise GrantsUnavailable("grants key file must be private (mode 0600)")
    key = base64.b64decode(path.read_text().strip(), validate=True)
    if len(key) != 32:
        raise GrantsUnavailable("grants key file must hold 32 base64-encoded bytes")
    return key


def associated(namespace, account, generation):
    return f"{namespace}\0{account}\0{generation}".encode()


def seal(key, namespace, account, generation, credentials):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, credentials.encode(), associated(namespace, account, generation))


def unseal(key, namespace, account, generation, blob):
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        blob = bytes(blob)
        return AESGCM(key).decrypt(blob[:12], blob[12:], associated(namespace, account, generation)).decode()
    except (InvalidTag, ValueError):
        raise GrantsUnavailable(f"shared login for {account} failed its integrity check") from None


def oauth_of(credentials):
    oauth = json.loads(credentials).get("claudeAiOauth")
    if not isinstance(oauth, dict) or not oauth.get("accessToken") or not oauth.get("expiresAt"):
        raise GrantsUnavailable("shared login has no access token and expiry")
    return oauth


def default_refresh(credentials):
    from claude_swap import oauth

    return oauth.try_refresh_oauth_credentials(credentials, timeout_s=POST_TIMEOUT_S)


class PostgresGrants:
    def __init__(self, dsn, namespace, key, journal_dir, refresh=default_refresh, sleep=time.sleep):
        self.dsn, self.namespace, self.key = dsn, namespace, key
        self.journal_dir = Path(journal_dir)
        self.journal_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.refresh, self.sleep = refresh, sleep
        self.host = socket.gethostname()
        self.lease_wait_s = LEASE_WAIT_S

    def connect(self):
        import psycopg

        return psycopg.connect(
            self.dsn, connect_timeout=3, autocommit=True,
            options="-c statement_timeout=3000 -c lock_timeout=3000",
        )

    def transaction(self, statements):
        """Run (sql, params) pairs in one durable transaction; return each cursor's rows and rowcount."""
        try:
            with self.connect() as conn, conn.transaction():
                conn.execute("SET LOCAL synchronous_commit = on")
                results = []
                for sql, params in statements:
                    cursor = conn.execute(sql, params)
                    results.append((cursor.fetchall() if cursor.description else [], cursor.rowcount))
                return results
        except GrantsUnavailable:
            raise
        except Exception:  # noqa: BLE001 - fail closed; never expose a DSN or server detail
            raise GrantsUnavailable("shared login store unavailable") from None

    def row(self, account):
        (rows, _), = self.transaction([(
            "SELECT ciphertext, generation, state, lease_until > now() FROM ccp_grants"
            " WHERE namespace = %s AND account = %s",
            (self.namespace, account),
        )])
        if not rows:
            raise GrantsUnavailable(f"no shared login for {account}")
        return rows[0]

    def token(self, account, stale=None):
        """(accessToken, expiresAt ms) for an account, refreshing at most once across every host."""
        self.reconcile(account)
        deadline = time.monotonic() + self.lease_wait_s
        while True:
            ciphertext, generation, state, leased = self.row(account)
            if state == NEEDS_RECONCILE:
                raise GrantsUnavailable(f"shared login for {account} needs a new login")
            oauth = oauth_of(unseal(self.key, self.namespace, account, generation, ciphertext))
            due = oauth["expiresAt"] - MARGIN_MS <= time.time() * 1000 or oauth["accessToken"] == stale
            if not due:
                return oauth["accessToken"], oauth["expiresAt"]
            if not leased:
                fresh = self.refresh_generation(account, generation)
                if fresh is not None:
                    return fresh
            if time.monotonic() >= deadline:
                raise GrantsUnavailable(f"shared login for {account} is being renewed elsewhere")
            self.sleep(LEASE_POLL_S)

    def refresh_generation(self, account, generation):
        holder = f"{self.host}:{os.getpid()}:{uuid.uuid4().hex}"
        (rows, _), = self.transaction([(
            "UPDATE ccp_grants SET lease_holder = %s, lease_until = now() + make_interval(secs => %s)"
            " WHERE namespace = %s AND account = %s AND generation = %s AND state = 'ok'"
            " AND (lease_until IS NULL OR lease_until < now()) RETURNING ciphertext",
            (holder, LEASE_S, self.namespace, account, generation),
        )])
        if not rows:
            return None  # another host holds G; wait for its commit instead of POSTing
        credentials = unseal(self.key, self.namespace, account, generation, rows[0][0])
        outcome = self.refresh(credentials)
        if outcome.error is not None or outcome.credentials is None:
            dead = outcome.error in DEAD
            self.transaction([(
                "UPDATE ccp_grants SET lease_holder = NULL, lease_until = NULL,"
                " state = CASE WHEN %s THEN 'needs-reconcile' ELSE state END"
                " WHERE namespace = %s AND account = %s AND generation = %s AND lease_holder = %s",
                (dead, self.namespace, account, generation, holder),
            )])
            raise GrantsUnavailable(
                f"shared login for {account} " + ("needs a new login" if dead else "could not be renewed")
            )
        oauth = oauth_of(outcome.credentials)
        sealed = seal(self.key, self.namespace, account, generation + 1, outcome.credentials)
        try:
            self.write_journal(account, generation + 1, oauth["expiresAt"], sealed)
        except OSError:
            pass  # G is already spent: committing G+1 matters more than the local copy
        try:
            self.commit(account, generation, sealed, oauth["expiresAt"])
        except GrantsUnavailable:
            pass  # the journal writes G+1 back on the next read; the token in hand is valid
        return oauth["accessToken"], oauth["expiresAt"]

    def commit(self, account, generation, sealed, expires_ms):
        # CAS on G only, not on the lease holder: G is spent once POSTed, so G+1 must land.
        self.transaction([(
            "UPDATE ccp_grants SET ciphertext = %s, generation = %s, expires_at = to_timestamp(%s),"
            " lease_holder = NULL, lease_until = NULL, state = 'ok'"
            " WHERE namespace = %s AND account = %s AND generation = %s",
            (sealed, generation + 1, expires_ms / 1000, self.namespace, account, generation),
        )])

    def journal(self, account):
        name = hashlib.sha256(f"{self.namespace}\0{account}".encode()).hexdigest()
        return self.journal_dir / f"{name}.json"

    def write_journal(self, account, generation, expires_ms, sealed):
        path = self.journal(account)
        temporary = path.with_suffix(".tmp")
        record = json.dumps({
            "namespace": self.namespace, "account": account, "generation": generation,
            "expires_at_ms": expires_ms, "ciphertext": base64.b64encode(sealed).decode(),
        })
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(record)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(self.journal_dir, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def reconcile(self, account):
        """Write this host's journaled generation back when the row is older; drop it when newer."""
        path = self.journal(account)
        if not path.exists():
            return
        record = json.loads(path.read_text())
        sealed = base64.b64decode(record["ciphertext"])
        generation = record["generation"]
        unseal(self.key, self.namespace, account, generation, sealed)
        (_, written), (rows, _) = self.transaction([
            (
                "INSERT INTO ccp_grants (namespace, account, ciphertext, generation, expires_at, state)"
                " VALUES (%s, %s, %s, %s, to_timestamp(%s), 'ok')"
                " ON CONFLICT (namespace, account) DO UPDATE SET ciphertext = excluded.ciphertext,"
                " generation = excluded.generation, expires_at = excluded.expires_at,"
                " lease_holder = NULL, lease_until = NULL, state = 'ok'"
                " WHERE ccp_grants.generation < excluded.generation",
                (self.namespace, account, sealed, generation, record["expires_at_ms"] / 1000),
            ),
            (
                "SELECT generation FROM ccp_grants WHERE namespace = %s AND account = %s",
                (self.namespace, account),
            ),
        ])
        if not written and rows[0][0] > generation:
            path.unlink(missing_ok=True)

    def reconcile_all(self):
        for path in self.journal_dir.glob("*.json"):
            record = json.loads(path.read_text())
            if record.get("namespace") == self.namespace:
                self.reconcile(record["account"])

    def seed(self, account, credentials, replace=False):
        """Store a login as the account's newest generation; returns that generation."""
        oauth = oauth_of(credentials)
        if not oauth.get("refreshToken"):
            raise GrantsUnavailable("a shared login needs a refresh token")
        (rows, _), = self.transaction([(
            "SELECT generation FROM ccp_grants WHERE namespace = %s AND account = %s",
            (self.namespace, account),
        )])
        if rows and not replace:
            raise GrantsUnavailable(f"{account} already has a shared login; pass --replace")
        generation = rows[0][0] + 1 if rows else 1
        sealed = seal(self.key, self.namespace, account, generation, credentials)
        (_, written), = self.transaction([(
            "INSERT INTO ccp_grants (namespace, account, ciphertext, generation, expires_at, state)"
            " VALUES (%s, %s, %s, %s, to_timestamp(%s), 'ok')"
            " ON CONFLICT (namespace, account) DO UPDATE SET ciphertext = excluded.ciphertext,"
            " generation = excluded.generation, expires_at = excluded.expires_at,"
            " lease_holder = NULL, lease_until = NULL, state = 'ok'"
            " WHERE ccp_grants.generation = %s",
            (self.namespace, account, sealed, generation, oauth["expiresAt"] / 1000, generation - 1),
        )])
        if not written:
            raise GrantsUnavailable(f"{account} changed while seeding; run it again")
        return generation
