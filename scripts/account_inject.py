"""Server mode: swap a placeholder bearer for the named account's real token.

Clients carry `Bearer proxy-account-<alias>`; this proxy holds the logins. A
real token passes through untouched, so a client's own login still works.
cswap owns every grant: the live default login is renewed by running Claude Code
itself (`renew_live_login`), any other slot through cswap's `consume_backup_grant`.
With `CCP_GRANTS_DSN` set, PostgreSQL owns every grant instead (account_grants.py).
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import runpy
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FetchTimeout
from pathlib import Path
from uuid import UUID

from mitmproxy import ctx, exceptions, http
from mitmproxy.net import server_spec

PREFIX = "Bearer proxy-account-"
# Keys this proxy issues (account_ui.py creates them; the accounts file keeps only their SHA-256).
API_KEY_PREFIX = "ccp-"
REMOTE_CONTROL_PATH = "/v1/code/"
SESSIONS_PATH = "/v1/code/sessions"
# Refresh a little early so a token never expires mid-request. Also when the live login is
# renewed: Claude Code refreshes only inside this same 5-minute window (2.1.318 `now+300000>=expiresAt`).
MARGIN_MS = 5 * 60 * 1000
# Claude Code must reach Anthropic directly to refresh, not through a proxy.
PROXY_ENV = {"HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "ANTHROPIC_BASE_URL"}
# A `claude setup-token` has no expiry or refresh grant (about a year, inference only);
# re-read it this often so a replaced one is picked up.
SETUP_TOKEN_REREAD_MS = 60 * 1000
# A rejected login is renewed at most this often, however many requests it fails.
RENEW_EVERY_S = 60
# mitmproxy serves every connection from one event loop, and reading an expired slot runs a
# network refresh. A token that is still valid keeps serving while its refresh runs in the
# background; only a request with no valid token waits, this long, and a failed fetch is not
# retried for FETCH_RETRY_S.
FETCH_WAIT_S = 0.25
FETCH_RETRY_S = 60
FETCHER = ThreadPoolExecutor(max_workers=4, thread_name_prefix="slot-token")
# Accounts file reloads in shared-login mode adopt the live login (database writes, fsyncs).
RELOADER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="accounts-reload")
# Ordered logins for the router's quota fallback (src/fallback.rs). Loopback only:
# the router strips it before anything is relayed upstream.
POOL_HEADER = "x-claude-codex-account-pool"
# The accounts file's "fallback" section (chains per model family, triggers) for the
# router. Same rules as the pool: only this addon sets it, the router strips it.
POLICY_HEADER = "x-claude-codex-fallback"
# Names the account that answered (`alias/model`), set by the router on every reply.
SERVED_BY_HEADER = "x-claude-codex-served-by"
UNUSABLE_RETRY_S = 300
# How the first account of a session is chosen (accounts file "balance.strategy"):
# session = the placeholder's own alias, prefer = "balance.prefer" for everyone,
# round-robin / least-sessions = spread new sessions, then stick to that account.
STRATEGIES = {"session", "prefer", "round-robin", "least-sessions"}
SESSION_HEADER = "x-claude-code-session-id"
# The accounts file's "session_models" (session id -> model id, set in the dashboard) for the
# router, which applies it before choosing a provider. Only this addon sets it; the router strips it.
SESSION_MODEL_HEADER = "x-claude-codex-session-model"
MODEL_PATHS = {"/v1/messages", "/v1/messages/count_tokens"}
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,127}")
# Prompt caches last at most an hour, so an idle session may be reassigned.
STICKY_IDLE_S = 3600
AFFINITY_MAX_PENDING = 16
_grants = runpy.run_path(str(Path(__file__).with_name("account_grants.py")))
GrantsUnavailable, PostgresGrants, read_key, write_private_json = (
    _grants[n] for n in ("GrantsUnavailable", "PostgresGrants", "read_key", "write_private_json")
)
CodexKeeper, CODEX_NAME = _grants["CodexKeeper"], _grants["CODEX_NAME"]


class StillFetching(GrantsUnavailable):
    """The login is being fetched in the background: retry soon, the account is not broken."""


def slot_token(slot):
    """Return (accessToken, expiresAt) for a cswap slot, refreshing only non-live slots."""
    from claude_swap import oauth
    from claude_swap.switcher import ClaudeAccountSwitcher

    switcher = ClaudeAccountSwitcher()
    live = {}
    if switcher.current_account_number() == slot:
        live = oauth.extract_oauth_data(switcher._read_credentials()) or {}
    if live.get("accessToken"):
        token = live
    else:
        # Not the live login, or a live identity with no credential to share a grant with.
        email = switcher._get_sequence_data()["accounts"][slot]["email"]
        raw, _ = switcher._read_account_credentials_ex(slot, email)
        token = oauth.extract_oauth_data(raw) or {}
    if token.get("accessToken") and "expiresAt" not in token and not token.get("refreshToken"):
        return token["accessToken"], time.time() * 1000 + SETUP_TOKEN_REREAD_MS
    if not live.get("accessToken") and token.get("expiresAt", 0) - MARGIN_MS <= time.time() * 1000:
        outcome = switcher.consume_backup_grant(slot, email, raw)
        if outcome.error is None:
            token = oauth.extract_oauth_data(outcome.credentials) or {}
    if not token.get("accessToken") or token.get("expiresAt", 0) <= time.time() * 1000:
        raise ValueError(f"cswap slot {slot} has no usable token; re-add it with cswap add")
    return token["accessToken"], token["expiresAt"]


def renew_live_login():
    """Claude Code refreshes its own login, under its own locks, when a run starts near expiry."""
    env = {k: v for k, v in os.environ.items() if k not in PROXY_ENV}
    subprocess.run(
        ["claude", "-p", "reply ok", "--model", "haiku", "--settings", '{"disableAllHooks": true}'],
        env=env, cwd=Path.home(), stdin=subprocess.DEVNULL, capture_output=True, timeout=180, check=True,
    )


def renew(slot, stale):
    """Renew a slot whose token Anthropic rejected (`stale`) or that is about to expire (`stale` None)."""
    from claude_swap import oauth
    from claude_swap.switcher import ClaudeAccountSwitcher

    switcher = ClaudeAccountSwitcher()
    email = switcher._get_sequence_data()["accounts"][slot]["email"]
    if switcher.current_account_number() == slot:
        live = oauth.extract_oauth_data(switcher._read_credentials()) or {}
        expiring = "expiresAt" in live and live["expiresAt"] - MARGIN_MS <= time.time() * 1000
        if live.get("accessToken") == stale or expiring:
            # cswap's `_fetch_active_usage` defers an expired live login whose lineage the slot
            # backup cannot vouch for ("refresh deferred to Claude Code's next use"), so do that.
            renew_live_login()
        return
    raw, _ = switcher._read_account_credentials_ex(slot, email)
    if stale and oauth.extract_access_token(raw) == stale:
        switcher.consume_backup_grant(slot, email, raw)


def live_login():
    """(slot, email) of this host's live default Claude login, read from ~/.claude.json, never its token."""
    from claude_swap.switcher import ClaudeAccountSwitcher

    switcher = ClaudeAccountSwitcher()
    identity = switcher._get_current_account()
    return switcher.current_account_number(), identity[0] if identity else None


def setup_token_slot(slot):
    """A `claude setup-token` login has no refresh grant, so every host may hold a copy of it."""
    from claude_swap import oauth
    from claude_swap.switcher import ClaudeAccountSwitcher

    switcher = ClaudeAccountSwitcher()
    email = switcher._get_sequence_data()["accounts"][slot]["email"]
    raw, _ = switcher._read_account_credentials_ex(slot, email)
    token = oauth.extract_oauth_data(raw) or {}
    return bool(token.get("accessToken")) and "expiresAt" not in token and not token.get("refreshToken")


def copyable(slot):
    try:
        return setup_token_slot(slot)
    except Exception:  # noqa: BLE001 - an unreadable slot is treated as a rotating grant
        return False


def adopt_live_login(grants, alias, home=None):
    """Take a shared alias's live login on this host away from Claude Code, which refreshes it outside any lease.

    A Claude Code process that held the login before the cutover writes it back when it refreshes. The newer
    of that copy and the stored generation is kept; the copy then leaves Claude Code's files (a private
    backup stays under ~/.local/state/ccp-adopted), keeping the MCP logins beside it.
    """
    home = home or Path.home()
    credentials = home / ".claude" / ".credentials.json"
    doc = json.loads(credentials.read_text()) if credentials.exists() else {}
    live = doc.get("claudeAiOauth")
    if live:
        generation = None
        if live.get("refreshToken"):
            generation = grants.seed(alias, json.dumps({"claudeAiOauth": live}), replace=True, newer_only=True)
        aside = home / ".local" / "state" / "ccp-adopted"
        aside.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_private_json(aside / f"{alias}-{time.strftime('%Y%m%dT%H%M%S')}.json", {"claudeAiOauth": live})
        del doc["claudeAiOauth"]
        write_private_json(credentials, doc)
        logging.warning(f"{alias}: took the live Claude login off this host"
                        + (f", stored as generation {generation}" if generation else ", the shared one was newer"))
    config = home / ".claude.json"
    data = json.loads(config.read_text()) if config.exists() else {}
    if data.pop("oauthAccount", None) is not None:
        write_private_json(config, data)


def renew_in_background(slot, stale):
    try:
        renew(slot, stale)
    except Exception as error:  # noqa: BLE001 - the next rejection or expiry check retries
        logging.warning(f"renewing cswap slot {slot} failed: {type(error).__name__}")


def key_hash(key):
    return hashlib.sha256(key.encode()).hexdigest()


def chrome_selection(placeholder):
    suffixes = placeholder.split("+")[1:]
    targets = [part[7:] for part in suffixes if part.startswith("chrome=")]
    if len(targets) > 1 or (targets and "chromes" in suffixes) or suffixes.count("chromes") > 1:
        raise ValueError("malformed Chrome account selection")
    if targets and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,63}", targets[0]):
        raise ValueError("invalid Chrome account alias")
    return "*" if "chromes" in suffixes else (targets or [None])[0]


def parse(placeholder):
    """Parse account, Remote Control ownership, privacy and native Chrome suffixes."""
    alias, *suffixes = placeholder.removeprefix("proxy-account-").split("+")
    owners = [s.removeprefix("rc=") for s in suffixes if s.startswith("rc=")]
    chrome_selection(placeholder)
    if len(owners) > 1 or any(
        s not in {"private", "chromes", "pin"} and not s.startswith(("rc=", "chrome="))
        for s in suffixes
    ):
        raise ValueError(f"malformed account placeholder {placeholder!r}")
    return alias, (owners or [None])[0], "private" in suffixes


class AffinityUnavailable(Exception):
    """Shared selection failed; never fall back to process-local assignment."""


class PostgresAffinity:
    def __init__(self, dsn, namespace):
        self.dsn, self.namespace = dsn, namespace

    def choose(self, inject, alias, session, pinned):
        try:
            import psycopg
            from psycopg.types.json import Jsonb

            with psycopg.connect(
                self.dsn, connect_timeout=3,
                options="-c statement_timeout=3000 -c lock_timeout=3000",
            ) as conn:
                conn.execute("SET LOCAL synchronous_commit = on")
                conn.execute(
                    "INSERT INTO ccp_affinity (namespace) VALUES (%s) ON CONFLICT DO NOTHING",
                    (self.namespace,),
                )
                state, = conn.execute(
                    "SELECT state FROM ccp_affinity WHERE namespace = %s FOR UPDATE",
                    (self.namespace,),
                ).fetchone()
                now, = conn.execute("SELECT extract(epoch FROM clock_timestamp())::float8").fetchone()
                inject.sticky = {s: e for s, e in state["sessions"].items() if e[1] > now - STICKY_IDLE_S}
                inject.turn = state["turn"]
                chosen = inject._start(alias, session, pinned, now)
                conn.execute(
                    "UPDATE ccp_affinity SET state = %s WHERE namespace = %s",
                    (Jsonb({"sessions": inject.sticky, "turn": inject.turn}), self.namespace),
                )
            return chosen
        except Exception:  # noqa: BLE001 - fail closed, including malformed shared state; never expose a DSN
            raise AffinityUnavailable("shared account affinity unavailable") from None


def codex_cli_auth_file():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json"


def codex_keeper(grants, name, auth_file):
    """The shared Codex login (opt-in): its row is `codex:<name>`, written for the router to `auth_file`."""
    if grants is None or not CODEX_NAME.fullmatch(name) or not auth_file:
        raise exceptions.OptionsError(
            "A shared Codex login requires the CCP_GRANTS_* settings, a valid CCP_GRANTS_CODEX_ACCOUNT"
            " and CCP_CODEX_AUTH_FILE"
        )
    path = Path(auth_file).expanduser().absolute()
    if path.resolve() == codex_cli_auth_file().expanduser().resolve():
        raise exceptions.OptionsError(
            "CCP_CODEX_AUTH_FILE must not be the Codex CLI's own auth.json, which the CLI refreshes itself"
        )
    codex = PostgresGrants(grants.dsn, grants.namespace, grants.key, grants.journal_dir,
                           refresh=_grants["codex_refresh"], parse=_grants["codex_login"])
    return CodexKeeper(codex, name, path)


class AccountInject:
    def __init__(self):
        self.accounts = {}
        self.renamed = {}
        self.cache = {}
        self.renewed = {}
        self.unusable = {}
        self.fetching = {}
        self.epoch = {}
        self.strategy, self.prefer = "session", None
        self.sticky, self.turn = {}, -1
        self.chrome = None
        self.upstream = None
        self.traffic_db = None
        self.traffic_days = None
        self.disabled = set()
        self.fallback = {}
        self.session_models = {}
        self.api_keys, self.main = {}, None
        # alias -> [(utilization %, window reset epoch)] from that account's latest reply.
        self.usage = {}
        self.chrome_profiles_file = Path.home() / ".claude/chrome-profiles.json"
        dsn = os.environ.get("CCP_AFFINITY_DSN")
        namespace = os.environ.get("CCP_AFFINITY_NAMESPACE")
        self.affinity = None
        self.affinity_worker = None
        if dsn is not None or namespace is not None:
            if not dsn or not namespace or not SESSION_ID.fullmatch(namespace):
                raise exceptions.OptionsError("Shared affinity requires a DSN and valid namespace")
            self.affinity = PostgresAffinity(dsn, namespace)
            self.affinity_slots = threading.BoundedSemaphore(AFFINITY_MAX_PENDING)
            self.affinity_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="account-affinity")
            self.requestheaders = self.shared_requestheaders
        self.grants, self.stale, self.copyable = None, {}, set()
        grants = [os.environ.get(n) for n in ("CCP_GRANTS_DSN", "CCP_GRANTS_NAMESPACE", "CCP_GRANTS_KEY_FILE")]
        if any(v is not None for v in grants):
            dsn, namespace, key_file = grants
            if not dsn or not namespace or not key_file or not SESSION_ID.fullmatch(namespace):
                raise exceptions.OptionsError(
                    "Shared logins require CCP_GRANTS_DSN, a valid CCP_GRANTS_NAMESPACE and CCP_GRANTS_KEY_FILE"
                )
            try:
                key = read_key(key_file)
            except (OSError, ValueError):
                raise exceptions.OptionsError("CCP_GRANTS_KEY_FILE must be a private file of 32 base64 bytes") from None
            self.grants = PostgresGrants(dsn, namespace, key, Path(key_file).parent / "grants-journal")
        self.codex = None
        codex = os.environ.get("CCP_GRANTS_CODEX_ACCOUNT")
        if codex is not None:
            self.codex = codex_keeper(self.grants, codex, os.environ.get("CCP_CODEX_AUTH_FILE"))
            threading.Thread(target=self.codex.run, daemon=True, name="codex-grant").start()

    def load(self, loader):
        loader.add_option(
            "proxy_accounts_file", str, "", "Alias→cswap slot map for injected logins"
        )
        loader.add_option(
            "proxy_upstream", str, "", "Next account proxy (http://HOST:PORT) for aliases not held here"
        )

    def configure(self, updated):
        if "proxy_upstream" in updated:
            self.upstream = ctx.options.proxy_upstream and server_spec.parse(ctx.options.proxy_upstream, "http")
        if "proxy_accounts_file" not in updated:
            return
        try:
            self.read_accounts(Path(ctx.options.proxy_accounts_file))
        except (OSError, ValueError, KeyError, AttributeError):
            raise exceptions.OptionsError("Invalid proxy accounts file") from None
        if self.grants is not None:
            try:
                self.grants.reconcile_all()
            except Exception as error:  # noqa: BLE001 - each account reconciles again on its next read
                logging.warning(f"reconciling shared logins failed: {type(error).__name__}")

    def read_accounts(self, path):
        self.accounts_file = path
        self.accounts_mtime = path.stat().st_mtime
        data = json.loads(path.read_text())
        accounts = {a: str(s) for a, s in data["accounts"].items()}
        if self.grants is not None:
            # Setup-tokens never rotate: they stay in cswap; only rotating grants are shared.
            self.copyable = {alias for alias, account_slot in accounts.items() if copyable(account_slot)}
            slot, email = live_login()
            for alias, account_slot in accounts.items():
                if alias in self.copyable:
                    continue
                if account_slot == slot or email and alias.lower() == email.lower():
                    try:
                        adopt_live_login(self.grants, alias)
                    except Exception as error:  # noqa: BLE001 - serving beats refusing; the next reload adopts again
                        logging.error(f"{alias} is still this host's live Claude login: {type(error).__name__}")
        self.accounts = accounts
        # Old alias -> current alias, so sessions launched before a rename keep working.
        self.renamed = dict(data.get("renamed", {}))
        # Where account_traffic.py records every flow; unset records nothing.
        self.traffic_db = data.get("traffic_db")
        self.traffic_days = data.get("traffic_days")
        # Switched off (in the UI): never tried, unless a session names the alias itself.
        self.disabled = set(data.get("disabled", []))
        fallback = data.get("fallback", {})
        threshold = fallback.get("threshold")
        if threshold is not None and not 0 < float(threshold) <= 100:
            raise ValueError("fallback.threshold must be a percentage")
        self.fallback = fallback
        session_models = data.get("session_models", {})
        if not all(isinstance(s, str) and SESSION_ID.fullmatch(s) and isinstance(m, str) and MODEL_ID.fullmatch(m)
                   for s, m in session_models.items()):
            raise ValueError("session_models maps session ids to model ids")
        self.session_models = dict(session_models)
        self.api_keys = {k["sha256"]: k["name"] for k in data.get("api_keys", [])}
        self.main = data.get("main")
        if not set(self.renamed.values()) <= set(self.accounts):
            raise ValueError("renamed must point at configured aliases")
        balance = data.get("balance", {})
        self.strategy = balance.get("strategy", "session")
        self.prefer = balance.get("prefer")
        if self.strategy not in STRATEGIES or self.strategy == "prefer" and self.prefer not in self.accounts:
            raise ValueError(f"invalid balance {balance!r}")
        self.chrome_profiles_file = Path(
            data.get("chrome_profiles_file", self.chrome_profiles_file)
        ).expanduser()

    def reload_accounts(self):
        # Added accounts join the fallback pool without restarting the proxy.
        path = getattr(self, "accounts_file", None)
        try:
            mtime = path is not None and path.stat().st_mtime
            if path is None or mtime == self.accounts_mtime:
                return
            if self.grants is None:
                self.read_accounts(path)
            else:
                self.accounts_mtime = mtime
                RELOADER.submit(self.reload_in_background, path)
        except (OSError, ValueError, KeyError, AttributeError):
            pass

    def reload_in_background(self, path):
        try:
            self.read_accounts(path)
        except (OSError, ValueError, KeyError, AttributeError) as error:
            self.accounts_mtime = None  # retried on the next request, as an inline reload was
            logging.warning(f"reloading the accounts file failed: {type(error).__name__}")

    def token(self, alias):
        cached = self.cache.get(alias)
        now = time.time() * 1000
        if cached and cached[1] - MARGIN_MS > now:
            return cached[0]
        if cached and cached[1] > now:
            try:
                self.fetch(alias, wait=False)
            except ValueError:
                pass  # the cached token is still valid; the background fetch replaces it
            self.renew(alias, None)
            return cached[0]
        epoch, fetched = self.epoch.get(alias, 0), None
        try:
            fetched = self.fetch(alias)
            if self.epoch.get(alias, 0) == epoch:
                self.cache[alias] = fetched  # never a token rejected while this request waited
        finally:
            if (fetched or (None, 0))[1] - MARGIN_MS <= time.time() * 1000:
                self.renew(alias, None)  # nothing on this host renews the live login before it dies
        return fetched[0]

    def fetch(self, alias, wait=True):
        future = self.fetching.get(alias)
        error = ValueError if self.grants is None else GrantsUnavailable
        if future is not None and not future.done():
            raise StillFetching(f"login for {alias} is still being fetched")
        if future is not None and future.exception() is not None and time.time() < future.failed_at + FETCH_RETRY_S:
            raise error(f"login for {alias} is unavailable")
        if self.grants is None or alias in self.copyable:
            future = self.fetching[alias] = FETCHER.submit(slot_token, self.accounts[alias])
        else:
            future = self.fetching[alias] = FETCHER.submit(self.grants.token, alias, self.stale.pop(alias, None))
        future.failed_at = float("inf")
        epoch = self.epoch.get(alias, 0)

        def settle(done):
            if done.exception() is not None:
                done.failed_at = time.time()
            elif self.epoch.get(alias, 0) == epoch:
                self.cache[alias] = done.result()  # never a token rejected after this fetch started

        future.add_done_callback(settle)
        if not wait:
            raise StillFetching(f"login for {alias} is still being fetched")
        try:
            return future.result(timeout=FETCH_WAIT_S)
        except FetchTimeout:
            raise StillFetching(f"login for {alias} is still being fetched") from None

    def name(self, alias):
        return self.renamed.get(alias, alias)

    def start(self, alias, session, pinned=False):
        """The ONLY first-account selector, optionally serialized across proxy workers."""
        if self.affinity is not None:
            return self.affinity.choose(self, alias, session, pinned)
        return self._start(alias, session, pinned)

    def _start(self, alias, session, pinned=False, now=None):
        """Sessions keep their account because prompt caches are per account."""
        if pinned:
            return alias
        if self.strategy == "prefer":
            return alias if self.prefer in self.disabled else self.prefer
        if self.strategy == "session" or not session:
            return alias
        now = time.time() if now is None else now
        self.sticky = {s: e for s, e in self.sticky.items() if e[1] > now - STICKY_IDLE_S}
        entry = self.sticky.get(session)
        if entry is None or entry[0] not in self.accounts:
            names = [n for n in self.accounts if n not in self.disabled] or list(self.accounts)
            if self.strategy == "round-robin":
                self.turn = (self.turn + 1) % len(names)
                chosen = names[self.turn]
            else:  # least-sessions; ties keep accounts-file order
                load = {name: 0 for name in names}
                for name, _ in self.sticky.values():
                    load[name] = load.get(name, 0) + 1
                chosen = min(names, key=lambda name: load[name])
            entry = (chosen, now)
        self.sticky[session] = (entry[0], now)
        return entry[0]

    def pool(self, alias, session=None, pinned=False):
        """Every usable login, the session's starting account first, then file order.
        A pinned placeholder (`+pin`) starts on its own alias, ignoring the balance."""
        alias = self.start(alias, session, pinned)
        names = [alias, *(a for a in self.accounts if a != alias and a not in self.disabled)]
        # Accounts at the usage threshold go last: still a fallback, never a first choice.
        # The starting account keeps "primary", so its thread deltas never move.
        names = [n for n in names if not self.hot(n)] + [n for n in names if self.hot(n)]
        pool, fetching = [], None
        for name in names:
            if self.unusable.get(name, 0) > time.time():
                continue
            try:
                pool.append({"alias": name, "authorization": "Bearer " + self.token(name), "primary": name == alias})
            except StillFetching as error:
                fetching = error  # not broken: it joins the pool once its fetch lands
            except Exception:  # noqa: BLE001 - one broken slot must not block the rest
                self.unusable[name] = time.time() + UNUSABLE_RETRY_S
        if not pool and fetching is not None:
            raise fetching
        return pool

    def hot(self, alias):
        """Whether an unexpired 5h or 7d window of the account is at the fallback threshold."""
        threshold = self.fallback.get("threshold")
        now = time.time()
        return bool(threshold) and any(
            used >= float(threshold) and reset > now for used, reset in self.usage.get(alias, [])
        )

    def responseheaders(self, flow):
        if flow.response is None:
            return
        login = flow.metadata.get("account_login")
        if flow.response.status_code == 401 and login in self.accounts:
            self.rejected(login, flow.request.headers.get("authorization", "").removeprefix("Bearer "))
        # Every reply carries the answering account's utilization; remember it for hot().
        if not flow.metadata.get("account_pool"):
            return
        headers = flow.response.headers
        alias = headers.get(SERVED_BY_HEADER, "").split("/")[0]
        if alias not in self.accounts:
            return
        windows = []
        for key in ("5h", "7d"):
            used = headers.get(f"anthropic-ratelimit-unified-{key}-utilization")
            reset = headers.get(f"anthropic-ratelimit-unified-{key}-reset")
            try:
                windows.append((float(used) * 100, float(reset) if reset else time.time() + UNUSABLE_RETRY_S))
            except (TypeError, ValueError):
                continue
        if windows:
            self.usage[alias] = windows

    def cascades(self, placeholder, path=""):
        """True when the next proxy, not this one, holds the login a placeholder names."""
        if not self.upstream:
            return False
        alias, owner, _ = parse(placeholder)
        if owner and path.startswith(REMOTE_CONTROL_PATH):
            alias = owner
        return self.name(alias) not in self.accounts

    def login_alias(self, placeholder, path=""):
        alias, owner, _ = parse(placeholder)
        if owner and path.startswith(REMOTE_CONTROL_PATH):
            alias = owner
        return self.name(alias)

    def resolve(self, placeholder, path=""):
        alias = self.login_alias(placeholder, path)
        if alias not in self.accounts:
            raise ValueError(f"unknown account alias {alias!r}")
        return self.token(alias)

    def rejected(self, alias, sent):
        """Anthropic refused a token this proxy injected: forget it and let cswap renew it."""
        cached = self.cache.get(alias)
        if not cached or cached[0] != sent:
            return  # already replaced since this request went out
        del self.cache[alias]
        self.epoch[alias] = self.epoch.get(alias, 0) + 1
        self.renew(alias, sent)

    def renew(self, alias, stale):
        if self.grants is not None:
            if alias in self.copyable:
                return  # a setup-token has nothing to renew
            # The next fetch refreshes under the shared lease; expiry needs no hint.
            if stale and time.time() - self.renewed.get(alias, 0) >= RENEW_EVERY_S:
                self.renewed[alias], self.stale[alias] = time.time(), stale
            return
        now = time.time()
        if now - self.renewed.get(alias, 0) < RENEW_EVERY_S:
            return
        self.renewed[alias] = now
        threading.Thread(target=renew_in_background, args=(self.accounts[alias], stale), daemon=True).start()

    def identity(self, alias):
        from claude_swap.paths import get_backup_root

        data = json.loads((get_backup_root() / "sequence.json").read_text())
        account = data["accounts"][self.accounts[alias]]
        return str(UUID(account["uuid"]))

    def chrome_label(self, alias, device, fallback):
        try:
            profiles = json.loads(self.chrome_profiles_file.read_text())["profiles"]
            for profile in profiles:
                if (
                    profile.get("confirmed") is True
                    and profile.get("account") == alias
                    and profile.get("deviceId") == device
                    and isinstance(profile.get("host"), str)
                    and profile["host"].strip()
                    and isinstance(profile.get("email"), str)
                    and profile["email"].strip()
                ):
                    return f"{profile['host'].strip()} - {profile['email'].strip()}"
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        return f"Unidentified browser {device[:8]}"

    def bridge(self):
        if self.chrome is None:
            namespace = runpy.run_path(str(Path(__file__).with_name("account_chrome_bridge.py")))
            self.chrome = namespace["ChromeBridge"](
                self.accounts, self.identity, self.token,
                lambda flow, client, content: ctx.master.commands.call(
                    "inject.websocket", flow, client, content, True
                ),
                label=self.chrome_label,
            )
        return self.chrome

    def websocket_message(self, flow):
        request = flow.request
        if not (
            request.scheme == "https"
            and request.host == "bridge.claudeusercontent.com"
            and request.port == 443
            and request.method == "GET"
            and re.fullmatch(r"/chrome/[0-9a-fA-F-]{36}", request.path)
            and request.headers.get("upgrade", "").lower() == "websocket"
        ):
            return
        message = flow.websocket.messages[-1]
        if message.injected:
            return
        if self.chrome is not None and self.chrome.message(flow):
            return
        if not message.from_client or not message.is_text:
            return
        try:
            frame = json.loads(message.text)
            supplied = frame.get("oauth_token") if isinstance(frame, dict) else None
            if not str(supplied).startswith("proxy-account-") or self.cascades(supplied):
                return
            alias = self.name(parse(supplied)[0])
            selected = chrome_selection(supplied)
            selected = selected if selected in {None, "*"} else self.name(selected)
            if (
                frame.get("type") != "connect"
                or frame.get("client_type") != "claude-code"
                or request.path != f"/chrome/{self.identity(alias)}"
                or selected not in {None, "*"} and selected not in self.accounts
                or flow.metadata.get("chrome_authenticated")
            ):
                raise ValueError("Invalid Chrome account authentication")
            frame["oauth_token"] = self.token(alias)
            message.text = json.dumps(frame)
            flow.metadata["chrome_authenticated"] = True
            if selected == "*" or selected is not None and selected != alias:
                self.bridge().start(flow, alias, None if selected == "*" else selected)
        except Exception:  # noqa: BLE001 - reject auth without logging tokens or native frames
            message.drop()
            flow.kill()

    def websocket_end(self, flow):
        if self.chrome is not None:
            self.chrome.end(flow)

    def done(self):
        if self.affinity_worker is not None:
            self.affinity_worker.shutdown(wait=False, cancel_futures=True)
        if self.chrome is not None:
            self.chrome.close()

    def api_key(self, flow):
        """Swap an issued API key for the main account's placeholder; False when the key is refused."""
        headers = flow.request.headers
        key = headers.get("x-api-key", "") or headers.get("authorization", "").removeprefix("Bearer ")
        if not key.startswith(API_KEY_PREFIX):
            return True
        self.reload_accounts()
        digest = key_hash(key)
        name = next((n for h, n in self.api_keys.items() if hmac.compare_digest(h, digest)), None)
        if name is None:
            flow.response = http.Response.make(
                401,
                json.dumps({"type": "error", "error": {"type": "authentication_error", "message": "invalid API key"}}),
                {"content-type": "application/json"},
            )
            return False
        headers.pop("x-api-key", None)
        main = self.main if self.main in self.accounts else next(iter(self.accounts), "")
        headers["authorization"] = PREFIX + main
        flow.metadata["api_key"] = name
        return True

    @staticmethod
    def affinity_failure(flow, message="shared account affinity unavailable"):
        for name in ("authorization", "x-api-key", POOL_HEADER, POLICY_HEADER, SESSION_MODEL_HEADER):
            flow.request.headers.pop(name, None)
        flow.response = http.Response.make(
            503,
            json.dumps({"type": "error", "error": {"type": "api_error", "message": message}}),
            {"content-type": "application/json"},
        )

    async def shared_requestheaders(self, flow):
        headers = flow.request.headers
        supplied = headers.get("authorization", "")
        key = headers.get("x-api-key", "") or supplied.removeprefix("Bearer ")
        if not (
            flow.request.method == "POST" and flow.request.path.split("?")[0] in MODEL_PATHS
            and (supplied.startswith(PREFIX) or key.startswith(API_KEY_PREFIX))
        ):
            self.requestheaders_local(flow)
            return
        if not self.affinity_slots.acquire(blocking=False):
            self.affinity_failure(flow)
            return
        try:
            future = self.affinity_worker.submit(self.requestheaders_local, flow)
        except RuntimeError:  # shutdown; no local selection after the worker is gone
            self.affinity_slots.release()
            self.affinity_failure(flow)
            return
        # Release only when the worker finishes, even if the waiting client disconnects.
        future.add_done_callback(lambda _: self.affinity_slots.release())
        await asyncio.wrap_future(future)

    def requestheaders(self, flow):
        self.requestheaders_local(flow)

    def requestheaders_local(self, flow):
        # Only this addon may name the pool; a client-supplied one never survives.
        flow.request.headers.pop(POOL_HEADER, None)
        flow.request.headers.pop(POLICY_HEADER, None)
        flow.request.headers.pop(SESSION_MODEL_HEADER, None)
        if flow.request.method == "POST" and flow.request.path.split("?")[0] in MODEL_PATHS:
            self.reload_accounts()
            forced = self.session_models.get(flow.request.headers.get(SESSION_HEADER, ""))
            if forced:
                flow.request.headers[SESSION_MODEL_HEADER] = forced
        if self.upstream and flow.request.host == "bridge.claudeusercontent.com":
            # The router reaches Anthropic through the next proxy; the Chrome bridge must too.
            flow.server_conn.via = self.upstream
        if not self.api_key(flow):
            return
        supplied = flow.request.headers.get("authorization", "")
        if not supplied and not self.upstream and flow.request.method == "GET" and (
                flow.request.host == "api.anthropic.com" and flow.request.path.split("?")[0] == "/v1/models"):
            # The router lists the Claude models a login offers; the dashboard and launcher ask without one.
            self.reload_accounts()
            names = [n for n in self.accounts if n not in self.disabled]
            try:
                if names:
                    flow.request.headers["authorization"] = "Bearer " + self.token(names[0])
            except Exception:  # noqa: BLE001 - the router then lists its built-in Claude models
                pass
            return
        if not supplied.startswith(PREFIX):
            return
        placeholder = supplied.removeprefix("Bearer ")
        self.reload_accounts()
        try:
            if self.cascades(placeholder, flow.request.path):
                return  # the next proxy swaps it
            # Counts carry the pool too: an account lacking the model 404s a count as well.
            if flow.request.method == "POST" and flow.request.path.split("?")[0] in MODEL_PATHS:
                alias, _, private = parse(placeholder)
                alias = self.name(alias)
                if alias not in self.accounts:
                    raise ValueError(f"unknown account alias {alias!r}")
                flow.metadata["account_private"] = private
                pinned = "pin" in placeholder.split("+")[1:]
                pool = self.pool(alias, flow.request.headers.get(SESSION_HEADER), pinned)
                if pool:
                    flow.request.headers["authorization"] = pool[0]["authorization"]
                else:
                    flow.request.headers.pop("authorization", None)
                flow.request.headers[POOL_HEADER] = json.dumps(pool)
                if self.fallback:
                    flow.request.headers[POLICY_HEADER] = json.dumps(self.fallback)
                flow.metadata["account_pool"] = True
                return
            token = self.resolve(placeholder, flow.request.path)
            flow.metadata["account_login"] = self.login_alias(placeholder, flow.request.path)
            private = parse(placeholder)[2]
            # account_remote_route reads this after the placeholder is gone.
            flow.metadata["account_private"] = private
            if private and flow.request.path.startswith(SESSIONS_PATH):
                # HQ's route never needs this token; without the route, Anthropic
                # must refuse the session rather than show it on claude.ai.
                del flow.request.headers["authorization"]
                return
            flow.request.headers["authorization"] = "Bearer " + token
        except AffinityUnavailable:
            self.affinity_failure(flow)
        except GrantsUnavailable as error:
            self.affinity_failure(flow, str(error))
        except Exception as error:
            # Never forward a placeholder upstream.
            flow.response = http.Response.make(
                401,
                json.dumps({"type": "error", "error": {"type": "authentication_error", "message": str(error)}}),
                {"content-type": "application/json"},
            )


inject = AccountInject()
# First, so it sees each request before the placeholder is swapped for a real token.
traffic = runpy.run_path(str(Path(__file__).with_name("account_traffic.py")))["TrafficDB"](
    lambda: inject.traffic_db, lambda: inject.traffic_days
)
addons = [traffic, inject]
