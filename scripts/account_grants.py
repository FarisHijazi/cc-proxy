"""Opt-in shared logins: one refresh grant per account, held only in PostgreSQL.

Nobody POSTs generation G to the provider without a committed lease on G. A refresh is
lease (commit) -> POST (no transaction open) -> fsync an encrypted journal of G+1 -> CAS
commit on G. The journal stays until a newer generation is committed, so a commit lost to
a failover or a crash before the commit is written back by `reconcile`.

Claude logins and the Codex CLI login differ only in their credential shape (`parse`) and
token endpoint (`refresh`); both run this one protocol. A Codex row is named `codex:<name>`,
which no Claude alias (an email-like `[A-Za-z0-9._@-]` name) can be.
"""

import base64
import hashlib
import json
import logging
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import namedtuple
from pathlib import Path

MARGIN_MS = 5 * 60 * 1000
# Far above the provider POST timeout, so a lease cannot expire mid-refresh.
LEASE_S = 120
POST_TIMEOUT_S = 10
LEASE_WAIT_S = 15
LEASE_POLL_S = 0.5
NEEDS_RECONCILE = "needs-reconcile"
# Exported back to a local holder for a rollback: no host serves or refreshes it until a reseed.
EXPORTED = "exported"
OUT_OF_SERVICE = {NEEDS_RECONCILE: "needs a new login", EXPORTED: "was exported; reseed it with --replace"}
GRANTS_ENV = ("CCP_GRANTS_DSN", "CCP_GRANTS_NAMESPACE", "CCP_GRANTS_KEY_FILE")
# The grant itself is dead: only a new login (seed) or another host's journal can fix it.
DEAD = {"invalid_grant", "no_refresh_token"}
CODEX_PREFIX = "codex:"
CODEX_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,63}")
# How often the keeper re-reads the Codex row; it refreshes once inside MARGIN_MS of expiry.
KEEP_EVERY_S = 60
# The Codex CLI's OAuth client and token endpoint, as src/providers/codex/auth/ uses them.
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_TOKEN_ENDPOINT = "https://auth.openai.com/oauth/token"
# Codex answers a spent refresh token with one of these codes, or plain OAuth invalid_grant.
CODEX_DEAD_CODES = {"invalid_grant", "refresh_token_expired", "refresh_token_reused", "refresh_token_invalidated"}

Login = namedtuple("Login", "access refresh expires")
Outcome = namedtuple("Outcome", "credentials error")


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


def claude_login(credentials):
    oauth = json.loads(credentials).get("claudeAiOauth")
    if not isinstance(oauth, dict) or not oauth.get("accessToken") or not oauth.get("expiresAt"):
        raise GrantsUnavailable("shared login has no access token and expiry")
    return Login(oauth["accessToken"], oauth.get("refreshToken"), oauth["expiresAt"])


def default_refresh(credentials):
    from claude_swap import oauth

    return oauth.try_refresh_oauth_credentials(credentials, timeout_s=POST_TIMEOUT_S)


def jwt_exp_ms(token):
    try:
        payload = token.split(".")[1]
        exp = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["exp"]
        return int(exp) * 1000
    except (IndexError, ValueError, KeyError, TypeError):
        return None


def codex_login(credentials):
    """The Codex CLI's auth.json: the access token's JWT `exp`, else the expiry stored at refresh."""
    doc = json.loads(credentials)
    tokens = doc.get("tokens") if isinstance(doc, dict) else None
    access = tokens.get("access_token") if isinstance(tokens, dict) else None
    expires = isinstance(access, str) and access and (jwt_exp_ms(access) or doc.get("ccp_expires_at_ms"))
    if not expires:
        raise GrantsUnavailable("shared Codex login has no access token and expiry")
    return Login(access, tokens.get("refresh_token"), expires)


def codex_post(form):
    """(status, JSON body or None) from the Codex token endpoint; never raises on an HTTP status."""
    request = urllib.request.Request(
        CODEX_TOKEN_ENDPOINT, data=urllib.parse.urlencode(form).encode(),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=POST_TIMEOUT_S) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, None


def codex_refresh(credentials, post=codex_post):
    """Spend the Codex refresh token once; the outcome holds the new auth.json, unowned fields kept."""
    doc = json.loads(credentials)
    tokens = doc["tokens"]
    if not tokens.get("refresh_token"):
        return Outcome(None, "no_refresh_token")
    try:
        status, body = post({
            "client_id": CODEX_CLIENT_ID, "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
        })
    except (OSError, urllib.error.URLError):
        return Outcome(None, "network")
    if status != 200:
        error = body.get("error") if isinstance(body, dict) else None
        code = error.get("code") if isinstance(error, dict) else error
        return Outcome(None, "invalid_grant" if status == 401 or code in CODEX_DEAD_CODES else f"http_{status}")
    if not isinstance(body, dict) or not body.get("access_token") or not body.get("refresh_token"):
        return Outcome(None, "malformed_response")
    tokens = {**tokens, "access_token": body["access_token"], "refresh_token": body["refresh_token"]}
    if body.get("id_token"):
        tokens["id_token"] = body["id_token"]
    expires_in = body.get("expires_in") if isinstance(body.get("expires_in"), int) else 3600
    refreshed = {
        **doc, "tokens": tokens, "ccp_expires_at_ms": int(time.time() * 1000) + expires_in * 1000,
        "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    return Outcome(json.dumps(refreshed), None)


def write_codex_auth(path, credentials):
    """Atomically write the router's auth.json: the current access token and no refresh token."""
    doc = json.loads(credentials)
    doc["tokens"] = {k: v for k, v in doc["tokens"].items() if k != "refresh_token"}
    write_private_json(path, doc)


def write_private_json(path, doc):
    """Temp file (0600), fsync, rename, directory fsync."""
    path = Path(path)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(doc, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    fsync_directory(path.parent)


def fsync_directory(directory):
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def store_from_env(parse=claude_login):
    """The PostgresGrants the proxy would build from CCP_GRANTS_*, for the operator scripts."""
    if not all(os.environ.get(n) for n in GRANTS_ENV):
        raise GrantsUnavailable("set " + ", ".join(GRANTS_ENV))
    dsn, namespace, key_file = (os.environ[n] for n in GRANTS_ENV)
    return PostgresGrants(dsn, namespace, read_key(key_file), Path(key_file).parent / "grants-journal", parse=parse)


class PostgresGrants:
    def __init__(self, dsn, namespace, key, journal_dir, refresh=default_refresh, sleep=time.sleep,
                 parse=claude_login):
        self.dsn, self.namespace, self.key = dsn, namespace, key
        self.journal_dir = Path(journal_dir)
        self.journal_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.refresh, self.sleep, self.parse = refresh, sleep, parse
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
        """(access token, expiry ms) for an account, refreshing at most once across every host."""
        login = self.parse(self.current(account, stale))
        return login.access, login.expires

    def current(self, account, stale=None):
        """The account's decrypted credentials, refreshed first when due (at most once across hosts)."""
        self.reconcile(account)
        deadline = time.monotonic() + self.lease_wait_s
        while True:
            ciphertext, generation, state, leased = self.row(account)
            if state in OUT_OF_SERVICE:
                raise GrantsUnavailable(f"shared login for {account} {OUT_OF_SERVICE[state]}")
            credentials = unseal(self.key, self.namespace, account, generation, ciphertext)
            login = self.parse(credentials)
            if login.expires - MARGIN_MS > time.time() * 1000 and login.access != stale:
                return credentials
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
        expires = self.parse(outcome.credentials).expires
        sealed = seal(self.key, self.namespace, account, generation + 1, outcome.credentials)
        try:
            self.write_journal(account, generation + 1, expires, sealed)
        except OSError:
            pass  # G is already spent: committing G+1 matters more than the local copy
        try:
            self.commit(account, generation, sealed, expires)
        except GrantsUnavailable:
            pass  # the journal writes G+1 back on the next read; the token in hand is valid
        return outcome.credentials

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
        fsync_directory(self.journal_dir)

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
                " WHERE ccp_grants.generation < excluded.generation AND ccp_grants.state <> 'exported'",
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

    def export(self, account):
        """(credentials, generation) of the newest generation, taken out of service first.

        Only once no live lease is held, so no host is mid-refresh and none refreshes it after.
        Exporting an exported row again is allowed: it is the retry when writing the copy failed.
        """
        self.reconcile(account)
        deadline = time.monotonic() + self.lease_wait_s
        while True:
            (rows, _), = self.transaction([(
                "UPDATE ccp_grants SET state = 'exported'"
                " WHERE namespace = %s AND account = %s AND state IN ('ok', 'exported')"
                " AND (lease_until IS NULL OR lease_until < now()) RETURNING ciphertext, generation",
                (self.namespace, account),
            )])
            if rows:
                ciphertext, generation = rows[0]
                return unseal(self.key, self.namespace, account, generation, ciphertext), generation
            _, _, state, _ = self.row(account)
            if state == NEEDS_RECONCILE:
                raise GrantsUnavailable(f"shared login for {account} {OUT_OF_SERVICE[state]}")
            if time.monotonic() >= deadline:
                raise GrantsUnavailable(f"shared login for {account} is being renewed elsewhere; try again")
            self.sleep(LEASE_POLL_S)

    def seed(self, account, credentials, replace=False):
        """Store a login as the account's newest generation; returns that generation."""
        login = self.parse(credentials)
        if not login.refresh:
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
            (self.namespace, account, sealed, generation, login.expires / 1000, generation - 1),
        )])
        if not written:
            raise GrantsUnavailable(f"{account} changed while seeding; run it again")
        return generation


class CodexKeeper:
    """Keeps the router's auth.json on the shared Codex login; the refresh token stays in PostgreSQL.

    The router runs with CCP_CODEX_EXTERNAL_REFRESH=1, so it only re-reads this file.
    """

    def __init__(self, grants, name, path):
        self.grants, self.account, self.path, self.written = grants, CODEX_PREFIX + name, Path(path), None

    def keep(self):
        credentials = self.grants.current(self.account)
        if credentials != self.written or not self.path.exists():
            write_codex_auth(self.path, credentials)
            self.written = credentials

    def run(self, every=KEEP_EVERY_S):
        while True:
            try:
                self.keep()
            except Exception as error:  # noqa: BLE001 - retried next tick; never log a token or DSN
                reason = error if isinstance(error, GrantsUnavailable) else type(error).__name__
                logging.warning(f"shared Codex login: {reason}")
            time.sleep(every)
