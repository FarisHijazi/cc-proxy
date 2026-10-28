"""claude.ai web (cookie) authentication, kept fresh the way a browser is.

The claude.ai web app authenticates with cookies, not a bearer token: the
``sessionKey`` cookie (``sk-ant-sid…``) plus the rest of the claude.ai jar
(``lastActiveOrg``, ``anthropic-device-id``, ``cf_clearance``, ``__cf_bm``, …).
This module finds that jar and keeps it current. Cookie sources, first with a
``sessionKey`` wins:

1. env ``CLAUDE_AI_SESSION_KEY`` (+ optional ``CLAUDE_AI_COOKIES``, a full
   ``name=value; …`` header for extras such as ``cf_clearance``). If the state
   file holds a jar rotated from this same key, that newer jar is used.
2. the state file ``$XDG_STATE_HOME/claude-rc/cookies.json`` (mode 0600): the
   latest jar we rotated ourselves.
3. the local Chrome cookie store via ``browser_cookie3`` (optional install,
   ``cc-proxy[web]``). Pin the profile with ``CLAUDE_AI_CHROME_ACCOUNT`` (email,
   looked up in Chrome's ``Local State``) or ``CLAUDE_AI_CHROME_PROFILE``;
   otherwise the most recently used profile wins. ``CLAUDE_AI_CHROME_DIR``
   overrides the user-data directory.
4. a logged-in Chrome over CDP (``CLAUDE_AI_CDP_URL``, e.g.
   ``http://127.0.0.1:9222``): ``Storage.getCookies`` over the DevTools
   websocket (needs ``websockets``).

Ownership (same rule as gemini-web-api's ``rotate_cookies_ourselves``): whoever
supplies the cookies keeps them fresh. Cookies read from Chrome/CDP belong to
Chrome — we read, never write, and re-read on an auth failure. Cookies supplied
through the env or state file have no other owner, so we keep them fresh:
every ``Set-Cookie`` is merged and the jar persisted, and :func:`keepalive`
touches the session periodically. ``CLAUDE_AI_AUTO_REFRESH=1/0`` overrides.

There is no documented refresh call for ``sessionKey``; nothing here invents
one. Freshness relies only on the server's ``Set-Cookie`` replies and on
keeping the session in use. UNVERIFIED (needs live observation): how long a
``sessionKey`` lives, whether activity extends it, and whether the server ever
rotates it via ``Set-Cookie``.

Cookie values are never logged; ``repr`` shows cookie names only.
"""

from __future__ import annotations

import asyncio
import glob
import hashlib
import json
import os
import sys
import threading
import time
import urllib.request
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Optional

import httpx

from .credentials import CredentialsError

DOMAIN = "claude.ai"
SESSION_COOKIE = "sessionKey"
SESSION_COOKIE_V3 = "sessionKeyV3"
ORG_COOKIE = "lastActiveOrg"
# A cheap authenticated route to keep the session in use. UNVERIFIED as a
# keep-alive (not in the 2026-10-28 HAR capture); override with `path=`.
KEEPALIVE_PATH = "/api/account"
# cf_clearance is bound to the browser's User-Agent; set CLAUDE_AI_USER_AGENT
# to that browser's exact string when sending a copied cf_clearance.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36"
)

ENV_SESSION_KEY = "CLAUDE_AI_SESSION_KEY"
ENV_COOKIES = "CLAUDE_AI_COOKIES"
ENV_COOKIE_ALIAS = "CLAUDE_AI_COOKIE"  # singular spelling, accepted too
ENV_PROXY = "CLAUDE_AI_PROXY"  # e.g. a home-IP proxy, so Cloudflare does not challenge claude.ai
ENV_AUTO_REFRESH = "CLAUDE_AI_AUTO_REFRESH"
ENV_CHROME_ACCOUNT = "CLAUDE_AI_CHROME_ACCOUNT"
ENV_CHROME_PROFILE = "CLAUDE_AI_CHROME_PROFILE"
ENV_CHROME_DIR = "CLAUDE_AI_CHROME_DIR"
ENV_CDP_URL = "CLAUDE_AI_CDP_URL"
ENV_USER_AGENT = "CLAUDE_AI_USER_AGENT"

# Sources. `owns_refresh` is True for the first two.
ENV, STATE, CHROME, CDP, EXPLICIT = "env", "state", "chrome", "cdp", "explicit"


class CloudflareChallenge(CredentialsError):
    """claude.ai answered with a Cloudflare challenge page, not an auth error."""


def user_agent() -> str:
    return os.environ.get(ENV_USER_AGENT) or BROWSER_USER_AGENT


def proxy_url() -> Optional[str]:
    """The proxy for claude.ai traffic only (HTTP and the TTS websocket)."""
    return os.environ.get(ENV_PROXY) or None


def claude_ai_mounts(transport: Optional[httpx.BaseTransport] = None) -> Optional[dict]:
    """httpx ``mounts`` routing just claude.ai through :func:`proxy_url`;
    api.anthropic.com stays direct. ``None`` with a test transport or no proxy."""
    url = proxy_url()
    if transport is not None or not url:
        return None
    return {"all://claude.ai": httpx.HTTPTransport(proxy=url), "all://*.claude.ai": httpx.HTTPTransport(proxy=url)}

def is_claude_domain(domain: str) -> bool:
    d = (domain or "").lstrip(".").lower()
    return d == DOMAIN or d.endswith("." + DOMAIN)


def parse_cookie_header(header: str) -> dict[str, str]:
    """``"a=1; b=2"`` -> ``{"a": "1", "b": "2"}``."""
    out: dict[str, str] = {}
    for part in (header or "").split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name:
            out[name.strip()] = value.strip()
    return out


def state_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "claude-rc" / "cookies.json"


def _seed(session_key: str) -> str:
    """A short fingerprint linking a state jar to the env key it grew from."""
    return hashlib.sha256(session_key.encode()).hexdigest()[:16]


def _owns(source: str) -> bool:
    override = os.environ.get(ENV_AUTO_REFRESH)
    if override is not None:
        return override.strip().lower() not in ("", "0", "false", "no", "off")
    return source in (ENV, STATE)


class CookieJar:
    """The claude.ai cookies a client sends, where they came from, and how to
    keep them fresh. Thread-safe for :meth:`merge` (a keep-alive thread shares
    it). ``seed`` ties a persisted jar to the env key it was rotated from."""

    def __init__(self, cookies: dict[str, str], source: str, *, seed: str | None = None, detail: str = ""):
        self.cookies = dict(cookies)
        self.source = source
        self.detail = detail
        self.seed = seed
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"CookieJar(source={self.origin!r}, names={self.names()})"

    __str__ = __repr__

    @property
    def origin(self) -> str:
        return f"{self.source}:{self.detail}" if self.detail else self.source

    @property
    def session_key(self) -> Optional[str]:
        return self.cookies.get(SESSION_COOKIE) or None

    @property
    def org_uuid(self) -> Optional[str]:
        return self.cookies.get(ORG_COOKIE) or None

    def names(self) -> list[str]:
        return sorted(self.cookies)

    def header(self) -> str:
        """The ``Cookie`` header value (also for websockets such as TTS)."""
        with self._lock:
            return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    def owns_refresh(self) -> bool:
        return _owns(self.source)

    # -- refresh ----------------------------------------------------------
    def merge(self, resp: httpx.Response) -> bool:
        """Fold a claude.ai response's ``Set-Cookie`` into the jar; persist when
        we own refresh. ``True`` if anything changed."""
        if not is_claude_domain(resp.request.url.host if resp.request else ""):
            return False
        changed = False
        pair = {SESSION_COOKIE, SESSION_COOKIE_V3}
        with self._lock:
            lines = resp.headers.get_list("set-cookie")
            for line in lines:
                changed |= self._apply(line)
            # A rotation that names only one of the pair must not leave the other stale.
            touched = pair & {line.split("=", 1)[0].strip() for line in lines}
            if len(touched) == 1 and (value := self.cookies.get(touched.pop())):
                self.cookies.update(dict.fromkeys(pair, value))
            if changed and self.owns_refresh():
                self._save()
        return changed

    def _apply(self, line: str) -> bool:
        first, *attrs = line.split(";")
        name, sep, value = first.strip().partition("=")
        if not sep or not name:
            return False
        meta = {}
        for a in attrs:
            k, _, v = a.strip().partition("=")
            meta[k.lower()] = v.strip()
        if "domain" in meta and not is_claude_domain(meta["domain"]):
            return False
        expired = meta.get("max-age", "").lstrip("-").isdigit() and int(meta["max-age"]) <= 0
        if not expired and meta.get("expires"):
            try:
                expired = parsedate_to_datetime(meta["expires"]).timestamp() <= time.time()
            except (TypeError, ValueError):
                pass
        if expired:
            return self.cookies.pop(name, None) is not None
        if self.cookies.get(name) == value.strip():
            return False
        self.cookies[name] = value.strip()
        return True

    def _save(self) -> None:
        if self.source in (ENV, EXPLICIT) and self.seed is None and self.session_key:
            self.seed = _seed(self.session_key)
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"seed": self.seed, "updated": int(time.time()), "cookies": self.cookies}, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def reread(self) -> bool:
        """Re-read the cookie source after an auth failure (another process or
        Chrome may hold a newer jar). ``True`` if the jar changed. A stale state
        file falls through to the browser sources."""
        if self.source == EXPLICIT:
            return False
        fresh = load_jar()
        if self.source == STATE and (fresh is None or fresh.cookies == self.cookies):
            fresh = load_jar(use_state=False)
        if fresh is None or fresh.cookies == self.cookies:
            return False
        with self._lock:
            self.cookies, self.source, self.detail, self.seed = fresh.cookies, fresh.source, fresh.detail, fresh.seed
        return True


# --- sources -------------------------------------------------------------------
def from_env() -> Optional[CookieJar]:
    cookies = parse_cookie_header(os.environ.get(ENV_COOKIES) or os.environ.get(ENV_COOKIE_ALIAS, ""))
    if os.environ.get(ENV_SESSION_KEY):
        cookies[SESSION_COOKIE] = os.environ[ENV_SESSION_KEY].strip()
    # claude.ai sets the same value under both names; send both like the browser.
    key = cookies.get(SESSION_COOKIE) or cookies.get(SESSION_COOKIE_V3)
    if not key:
        return None
    cookies[SESSION_COOKIE] = cookies[SESSION_COOKIE_V3] = key
    seed = _seed(cookies[SESSION_COOKIE])
    state = from_state()
    if state is not None and state.seed == seed:
        return CookieJar(state.cookies, ENV, seed=seed, detail="rotated")
    return CookieJar(cookies, ENV, seed=seed)


def from_state() -> Optional[CookieJar]:
    try:
        data = json.loads(state_path().read_text())
    except (OSError, ValueError):
        return None
    cookies = data.get("cookies") if isinstance(data, dict) else None
    if not isinstance(cookies, dict) or not cookies.get(SESSION_COOKIE):
        return None
    return CookieJar({str(k): str(v) for k, v in cookies.items()}, STATE, seed=data.get("seed"))


def chrome_dir() -> str:
    if os.environ.get(ENV_CHROME_DIR):
        return os.environ[ENV_CHROME_DIR]
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Google/Chrome")
    if os.name == "nt":
        return os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data")
    return os.path.expanduser("~/.config/google-chrome")


def profile_accounts() -> dict[str, str]:
    """Chrome profile directory -> signed-in account email, from Local State."""
    try:
        with open(os.path.join(chrome_dir(), "Local State"), encoding="utf-8") as f:
            cache = json.load(f).get("profile", {}).get("info_cache", {})
    except (OSError, ValueError):
        return {}
    return {prof: (meta.get("user_name") or "") for prof, meta in cache.items()}


def pinned_profile() -> Optional[str]:
    """The pinned profile directory, or None to try them all (most recent first).
    A pinned account that is not signed in is a hard error: falling back would
    silently speak as whichever account won."""
    if os.environ.get(ENV_CHROME_PROFILE):
        return os.environ[ENV_CHROME_PROFILE]
    account = (os.environ.get(ENV_CHROME_ACCOUNT) or "").strip().lower()
    if not account:
        return None
    accounts = profile_accounts()
    for prof, email in accounts.items():
        if email.strip().lower() == account:
            return prof
    known = sorted(e for e in accounts.values() if e)
    raise CredentialsError(
        f"{ENV_CHROME_ACCOUNT}={account!r} is not signed in to Chrome at {chrome_dir()}. "
        f"Signed-in accounts: {', '.join(known) or '(none)'}"
    )


def _cookie_stores() -> list[str]:
    root = chrome_dir()
    pin = pinned_profile()
    profiles = [os.path.join(root, pin)] if pin else glob.glob(os.path.join(root, "*"))
    paths = [
        p
        for prof in profiles
        for p in (os.path.join(prof, "Network", "Cookies"), os.path.join(prof, "Cookies"))
        if os.path.isfile(p)
    ]
    paths.sort(key=lambda p: (-os.path.getmtime(p), p))
    return paths


def from_chrome() -> Optional[CookieJar]:
    try:
        import browser_cookie3
    except ImportError:
        return None
    root = chrome_dir()
    for store in _cookie_stores():
        try:
            cj = browser_cookie3.chrome(cookie_file=store, domain_name=DOMAIN)
        except Exception:  # noqa: BLE001 - locked keyring, schema drift, ...
            continue
        jar = {c.name: c.value for c in cj if is_claude_domain(c.domain) and (c.path or "/") == "/"}
        if jar.get(SESSION_COOKIE):
            profile = os.path.relpath(store, root).split(os.sep)[0]
            return CookieJar(jar, CHROME, detail=profile)
    return None


async def _cdp_cookies(cdp_url: str) -> list[dict]:
    import websockets

    def ws_url() -> str:
        with urllib.request.urlopen(cdp_url.rstrip("/") + "/json/version", timeout=5) as r:
            return json.load(r)["webSocketDebuggerUrl"]

    url = await asyncio.get_running_loop().run_in_executor(None, ws_url)
    async with websockets.connect(url, max_size=None, open_timeout=10) as ws:
        for n, method in enumerate(("Storage.getCookies", "Network.getAllCookies"), 1):
            await ws.send(json.dumps({"id": n, "method": method}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == n:
                    break
            if "result" in msg:
                return msg["result"].get("cookies", [])
    return []


def from_cdp() -> Optional[CookieJar]:
    """The claude.ai jar from a Chrome started with ``--remote-debugging-port``.
    Runs on its own loop in a worker thread, so it works under a running loop."""
    cdp_url = os.environ.get(ENV_CDP_URL)
    if not cdp_url:
        return None
    box: dict[str, list[dict]] = {}

    def run() -> None:
        try:
            box["cookies"] = asyncio.run(_cdp_cookies(cdp_url))
        except Exception:  # noqa: BLE001 - unreachable browser, no websockets
            box["cookies"] = []

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(30)
    jar = {
        c["name"]: c["value"]
        for c in box.get("cookies", [])
        if is_claude_domain(c.get("domain", "")) and c.get("path", "/") == "/"
    }
    return CookieJar(jar, CDP) if jar.get(SESSION_COOKIE) else None


def load_jar(*, use_state: bool = True) -> Optional[CookieJar]:
    """The first cookie source yielding a ``sessionKey`` (see module docstring),
    or ``None``."""
    sources = (from_env, from_state, from_chrome, from_cdp) if use_state else (from_env, from_chrome, from_cdp)
    for source in sources:
        jar = source()
        if jar is not None:
            return jar
    return None


# --- responses -------------------------------------------------------------------
def is_cloudflare_challenge(resp: httpx.Response) -> bool:
    """A Cloudflare interstitial (bot check), as opposed to a claude.ai auth error."""
    if resp.headers.get("cf-mitigated", "").lower() == "challenge":
        return True
    if "text/html" not in resp.headers.get("content-type", ""):
        return False
    try:
        head = resp.read()[:4096].decode(errors="replace").lower()
    except httpx.HTTPError:
        return False
    return "challenge-platform" in head or "just a moment" in head or "cf-chl" in head


def challenge_error(resp: httpx.Response) -> CloudflareChallenge:
    return CloudflareChallenge(
        f"claude.ai returned a Cloudflare challenge (HTTP {resp.status_code}), not an auth error. "
        f"Pass the browser's cf_clearance in {ENV_COOKIES} with its exact User-Agent in "
        f"{ENV_USER_AGENT}, read cookies from that browser ({ENV_CHROME_ACCOUNT} / {ENV_CDP_URL}), "
        f"or use a browser-impersonating transport."
    )


# --- keep-alive ------------------------------------------------------------------
def touch(client: Any, path: str = KEEPALIVE_PATH) -> int:
    """One cheap authenticated GET through the client's request path (so any
    ``Set-Cookie`` is merged). Returns the HTTP status."""
    return client._send("GET", path, web=False, org=False, client_platform="web_claude_ai",
                        accept=(200, 204, 304)).status_code


def keepalive(
    client: Any, interval_s: float = 1800.0, *, path: str = KEEPALIVE_PATH, force: bool = False
) -> threading.Event:
    """Touch the session every ``interval_s`` seconds on a daemon thread, so the
    server can keep extending/rotating it. Runs only when the client's jar is
    ours to refresh (or ``force``). Set the returned event to stop. Failures are
    swallowed (the next request surfaces them). UNVERIFIED: whether activity
    extends a ``sessionKey`` at all."""
    stop = threading.Event()
    jar = getattr(client, "cookie_jar", None)
    if jar is None or not (force or jar.owns_refresh()):
        stop.set()
        return stop

    def loop() -> None:
        while not stop.wait(interval_s):
            try:
                touch(client, path)
            except Exception:  # noqa: BLE001 - keep the thread alive
                pass

    threading.Thread(target=loop, name="claude-rc-keepalive", daemon=True).start()
    return stop
