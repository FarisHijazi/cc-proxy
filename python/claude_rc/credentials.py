"""OAuth credential handling for the Claude Remote Control / Sessions API.

The Claude Code CLI logs in through claude.ai (OAuth) and stores the resulting
tokens in ``~/.claude/.credentials.json`` and account metadata (including the
organization UUID that the API requires as the ``x-organization-uuid`` header)
in ``~/.claude.json``.

This module reads those files so a third-party program can authenticate exactly
the way the claude.ai/code web app does, and refreshes the access token against
``POST https://platform.claude.com/v1/oauth/token`` when it has expired.

All of the constants below were reverse-engineered from the CLI binary:

* client id          -> ``9d1c250a-e61b-44d9-88ed-5944d1962f5e``
* token endpoint     -> ``https://platform.claude.com/v1/oauth/token``
  (refresh is served by platform.claude.com, *not* api.anthropic.com)
* oauth beta header  -> ``oauth-2025-04-20``
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

# --- reverse-engineered constants ------------------------------------------
CLAUDE_CODE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
# Token refresh is served by platform.claude.com, NOT api.anthropic.com.
OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_BETA = "oauth-2025-04-20"
# The CLI's own refresh User-Agent, for parity.
OAUTH_USER_AGENT = "anthropic-sdk-typescript/0.94.0 userOAuthProvider"

# Import-time snapshots kept for compatibility; lookups use the functions below,
# which follow ``$HOME`` at call time.
DEFAULT_CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
DEFAULT_CONFIG_PATH = Path.home() / ".claude.json"

# The claude.ai origin: serves the same ``/v1/code/*`` routes to a browser's
# ``sessionKey`` cookie (plus ``/api/organizations/...`` web routes).
CLAUDE_AI_BASE_URL = "https://claude.ai"

# Environment overrides (handy in CI / other machines).
ENV_CLAUDE_AI_ACCESS_TOKEN = "CLAUDE_AI_ACCESS_TOKEN"
ENV_ACCESS_TOKEN = "CLAUDE_RC_ACCESS_TOKEN"
ENV_CLAUDE_AI_ORG_UUID = "CLAUDE_AI_ORG_UUID"
ENV_ORG_UUID = "CLAUDE_RC_ORG_UUID"
# The CLI's own env override for a bare access token.
ENV_CLI_OAUTH_TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"
# A claude.ai browser ``sessionKey`` cookie; selects cookie auth on claude.ai
# (the other cookie sources live in claude_rc.webauth).
ENV_SESSION_KEY = "CLAUDE_AI_SESSION_KEY"
# A claude-swap (cswap) slot whose stored login to borrow (access token only).
ENV_CSWAP_SLOT = "CLAUDE_RC_CSWAP_SLOT"
# The CLI's trusted-device override (orgs that enforce Trusted Devices).
ENV_TRUSTED_DEVICE_TOKEN = "CLAUDE_TRUSTED_DEVICE_TOKEN"


def default_credentials_path() -> Path:
    """``~/.claude/.credentials.json`` under the current ``$HOME``."""
    return Path.home() / ".claude" / ".credentials.json"


def default_config_path() -> Path:
    """``~/.claude.json`` under the current ``$HOME``."""
    return Path.home() / ".claude.json"


def _now_ms() -> int:
    return int(time.time() * 1000)


class CredentialsError(RuntimeError):
    """Raised when credentials cannot be located, read, or refreshed."""


@dataclass
class OAuthCredentials:
    """The ``claudeAiOauth`` block of ``~/.claude/.credentials.json``."""

    access_token: str
    refresh_token: Optional[str] = None
    expires_at_ms: Optional[int] = None
    scopes: list[str] = field(default_factory=list)
    subscription_type: Optional[str] = None
    client_id: Optional[str] = None
    trusted_device_token: Optional[str] = None
    source_path: Optional[Path] = None

    def is_expired(self, skew_seconds: int = 60) -> bool:
        """True if the token is expired (or within ``skew_seconds`` of it)."""
        if not self.expires_at_ms:
            return False
        return _now_ms() >= self.expires_at_ms - skew_seconds * 1000

    # -- (de)serialization -------------------------------------------------
    @classmethod
    def from_block(
        cls,
        block: dict,
        source_path: Optional[Path] = None,
        trusted_device_token: Optional[str] = None,
    ) -> "OAuthCredentials":
        token = block.get("accessToken")
        if not token:
            raise CredentialsError("credentials file has no claudeAiOauth.accessToken")
        return cls(
            access_token=token,
            refresh_token=block.get("refreshToken"),
            expires_at_ms=block.get("expiresAt"),
            scopes=list(block.get("scopes") or []),
            subscription_type=block.get("subscriptionType"),
            client_id=block.get("clientId"),
            trusted_device_token=trusted_device_token,
            source_path=source_path,
        )

    def to_block(self) -> dict:
        block = {
            "accessToken": self.access_token,
            "refreshToken": self.refresh_token,
            "expiresAt": self.expires_at_ms,
            "scopes": self.scopes,
            "subscriptionType": self.subscription_type,
        }
        if self.client_id:
            block["clientId"] = self.client_id
        return block


def load_credentials(path: os.PathLike | str | None = None) -> OAuthCredentials:
    """Load OAuth credentials.

    Priority:
      1. ``CLAUDE_RC_ACCESS_TOKEN`` env var (no refresh possible).
      2. ``~/.claude/.credentials.json`` (``claudeAiOauth`` block).
    """
    env_token = os.environ.get(ENV_ACCESS_TOKEN) or os.environ.get(ENV_CLI_OAUTH_TOKEN)
    if env_token:
        return OAuthCredentials(access_token=env_token)

    p = Path(path) if path else default_credentials_path()
    if not p.exists():
        raise CredentialsError(
            f"credentials file not found at {p}. Run `claude` and `/login` first, "
            f"or set {ENV_ACCESS_TOKEN}."
        )
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - io
        raise CredentialsError(f"could not read {p}: {exc}") from exc

    block = data.get("claudeAiOauth")
    if not block:
        raise CredentialsError(
            f"{p} has no `claudeAiOauth` block — are you logged in via an API key "
            f"instead of claude.ai? Remote Control requires a claude.ai OAuth login."
        )
    # A top-level `trustedDeviceToken` sibling is used when the org requires
    # Trusted Devices; harmless to carry when absent.
    return OAuthCredentials.from_block(
        block, source_path=p, trusted_device_token=data.get("trustedDeviceToken")
    )


def load_org_uuid(config_path: os.PathLike | str | None = None) -> Optional[str]:
    """The organization UUID from the environment or ``~/.claude.json``.

    Priority: ``CLAUDE_AI_ORG_UUID``, ``CLAUDE_RC_ORG_UUID``,
    ``oauthAccount.organizationUuid`` in ``~/.claude.json``, a top-level
    ``organizationUuid`` in ``.credentials.json``. ``None`` when none has it —
    :class:`~claude_rc.client.RemoteControlClient` then asks
    ``/api/oauth/profile`` (see :func:`org_uuid_from_profile`).
    """
    env_org = os.environ.get(ENV_CLAUDE_AI_ORG_UUID) or os.environ.get(ENV_ORG_UUID)
    if env_org:
        return env_org

    p = Path(config_path) if config_path else default_config_path()
    if p.exists():
        try:
            data = json.loads(p.read_text())
            account = data.get("oauthAccount") or {}
            if account.get("organizationUuid"):
                return account["organizationUuid"]
        except (OSError, json.JSONDecodeError):
            pass

    # Fallback: a top-level `organizationUuid` sibling in .credentials.json.
    cp = default_credentials_path()
    if cp.exists():
        try:
            data = json.loads(cp.read_text())
            return data.get("organizationUuid")
        except (OSError, json.JSONDecodeError):
            return None
    return None


def refresh_credentials(
    creds: OAuthCredentials,
    *,
    persist: bool = True,
    client: httpx.Client | None = None,
) -> OAuthCredentials:
    """Refresh an expired access token via the OAuth token endpoint.

    Mirrors the CLI: ``POST`` to :data:`OAUTH_TOKEN_URL` with a JSON body of
    ``{grant_type, refresh_token, client_id}`` and the ``oauth-2025-04-20`` beta
    header. On success the new tokens are (optionally) written back to the same
    credentials file so subsequent runs stay authenticated.
    """
    if not creds.refresh_token:
        raise CredentialsError(
            "access token is expired and no refresh_token is available "
            "(token came from an env var). Re-run `claude` to refresh it."
        )

    owns_client = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        resp = client.post(
            OAUTH_TOKEN_URL,
            headers={
                "Content-Type": "application/json",
                "anthropic-beta": OAUTH_BETA,
                "User-Agent": OAUTH_USER_AGENT,
            },
            json={
                "grant_type": "refresh_token",
                "refresh_token": creds.refresh_token,
                "client_id": creds.client_id or CLAUDE_CODE_CLIENT_ID,
            },
        )
    finally:
        if owns_client:
            client.close()

    if resp.status_code != 200:
        raise CredentialsError(
            f"token refresh failed: HTTP {resp.status_code} {resp.text[:200]}"
        )
    body = resp.json()
    new = OAuthCredentials(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token", creds.refresh_token),
        expires_at_ms=_now_ms() + int(body.get("expires_in", 0)) * 1000
        if body.get("expires_in")
        else creds.expires_at_ms,
        scopes=(body.get("scope") or " ".join(creds.scopes)).split()
        if body.get("scope")
        else creds.scopes,
        subscription_type=creds.subscription_type,
        client_id=creds.client_id,
        trusted_device_token=creds.trusted_device_token,
        source_path=creds.source_path,
    )

    if persist and new.source_path:
        _persist(new)
    return new


def _persist(creds: OAuthCredentials) -> None:
    """Atomically write refreshed tokens back, preserving other keys."""
    path = creds.source_path
    if not path:
        return
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        data = {}
    data["claudeAiOauth"] = creds.to_block()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def org_uuid_from_profile(body: Any) -> Optional[str]:
    """``organization.uuid`` out of a ``GET /api/oauth/profile`` reply (the
    CLI's last-resort org lookup; needs the ``user:profile`` scope)."""
    org = body.get("organization") if isinstance(body, dict) else None
    uuid = org.get("uuid") if isinstance(org, dict) else None
    return uuid if isinstance(uuid, str) and uuid else None


# --- cswap (claude-swap) slots ---------------------------------------------
# cc-proxy's mitmproxy addon owns the slot logic (`slot_token` in
# scripts/account_inject.py). That module wires mitmproxy addons at import, so
# instead of importing it whole we compile just `slot_token` (and the constants
# it reads) out of its source: one implementation, no side effects.
_SLOT_TOKEN_NAMES = {"slot_token", "MARGIN_MS", "SETUP_TOKEN_REREAD_MS"}


def _account_inject_path() -> Optional[Path]:
    """scripts/account_inject.py in a repo checkout, else inside the installed
    ``cc_proxy`` wheel (``cc_proxy/scripts``)."""
    checkout = Path(__file__).resolve().parents[2] / "scripts" / "account_inject.py"
    if checkout.exists():
        return checkout
    try:
        spec = importlib.util.find_spec("cc_proxy")
    except (ImportError, ValueError):
        spec = None
    for root in (spec.submodule_search_locations or []) if spec else []:
        candidate = Path(root) / "scripts" / "account_inject.py"
        if candidate.exists():
            return candidate
    return None


def load_slot_token() -> Callable[[str], tuple[str, float]]:
    """cc-proxy's ``slot_token(slot) -> (access_token, expires_at_ms)``.

    Reads the live default login as-is and refreshes any other slot only
    through cswap's ``consume_backup_grant`` (cswap owns every refresh grant;
    nothing here writes credentials). Needs ``claude_swap`` importable when
    called."""
    path = _account_inject_path()
    if path is None:
        raise CredentialsError("cswap slots need cc-proxy's scripts/account_inject.py (repo checkout or cc-proxy wheel)")
    tree = ast.parse(path.read_text(), str(path))
    keep = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in _SLOT_TOKEN_NAMES:
            keep.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in _SLOT_TOKEN_NAMES for t in node.targets
        ):
            keep.append(node)
    namespace: dict[str, Any] = {"time": time}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(path), "exec"), namespace)
    if "slot_token" not in namespace:
        raise CredentialsError(f"{path} defines no slot_token")
    return namespace["slot_token"]


def cswap_current_slot() -> Optional[str]:
    """cswap's live account (the login Claude Code itself uses — Keychain on
    macOS), or ``None`` when cswap is not installed or has no account."""
    try:
        from claude_swap.switcher import ClaudeAccountSwitcher
    except ImportError:
        return None
    slot = ClaudeAccountSwitcher().current_account_number()
    return None if slot is None else str(slot)

def cswap_credentials(slot: str, slot_token: Callable[[str], tuple[str, float]] | None = None) -> OAuthCredentials:
    """An access-token-only :class:`OAuthCredentials` for a cswap slot (no
    refresh token: the client re-asks cswap when it expires)."""
    try:
        token, expires_at = (slot_token or load_slot_token())(str(slot))
    except ImportError as exc:
        raise CredentialsError(f"cswap slot {slot}: claude_swap is not installed ({exc})") from exc
    except ValueError as exc:
        raise CredentialsError(str(exc)) from exc
    return OAuthCredentials(access_token=token, expires_at_ms=int(expires_at) if expires_at else None)


# --- one resolver for the client --------------------------------------------
@dataclass
class AuthSource:
    """What a client authenticates with, and how to re-read it.

    Exactly one of ``credentials`` (Bearer OAuth) or ``jar`` (claude.ai cookies,
    :class:`~claude_rc.webauth.CookieJar`) is set; a bare ``session_key`` is
    turned into a one-cookie jar. ``reload`` re-reads a bearer source
    (credentials file, cswap slot) for a rotated or expired token; a jar
    re-reads itself (:meth:`~claude_rc.webauth.CookieJar.reread`). ``origin``
    names where it came from, for messages (never the secret itself).
    """

    credentials: Optional[OAuthCredentials] = None
    session_key: Optional[str] = field(default=None, repr=False)
    origin: str = ""
    reload: Optional[Callable[[], OAuthCredentials]] = None
    jar: Any = None

    def __post_init__(self) -> None:
        from .webauth import EXPLICIT, CookieJar

        if self.jar is None and self.session_key is not None:
            self.jar = CookieJar({"sessionKey": self.session_key}, EXPLICIT)
        if self.jar is not None:
            self.session_key = self.jar.session_key

    @property
    def is_cookie(self) -> bool:
        return self.jar is not None


def resolve_auth(
    token: str | None = None,
    *,
    credentials: OAuthCredentials | None = None,
    session_key: str | None = None,
    cswap_slot: str | int | None = None,
    credentials_path: os.PathLike | str | None = None,
    slot_token: Callable[[str], tuple[str, float]] | None = None,
) -> AuthSource:
    """Pick the client's identity. First match wins:

    1. explicit arguments: ``credentials``, ``token``, ``session_key``, ``cswap_slot``;
    2. claude.ai cookies — the primary mode whenever any source yields a
       ``sessionKey`` (:func:`claude_rc.webauth.load_jar`: env
       ``CLAUDE_AI_SESSION_KEY`` / ``CLAUDE_AI_COOKIES``, the rotated-jar state
       file, local Chrome, Chrome over CDP); the client moves to claude.ai;
    3. env ``CLAUDE_AI_ACCESS_TOKEN``, then ``CLAUDE_RC_ACCESS_TOKEN``, then the
       CLI's ``CLAUDE_CODE_OAUTH_TOKEN`` (bare tokens: no refresh);
    4. env ``CLAUDE_RC_CSWAP_SLOT``, else cswap's live account (Claude Code's own login,
       Keychain on macOS) — access token only, cswap keeps owning the refresh;
    5. ``~/.claude/.credentials.json`` (or ``credentials_path``), refreshed and
       written back when it expires.

    ``slot_token`` replaces cc-proxy's slot reader (tests).
    """
    from .webauth import load_jar

    if credentials is not None:
        return AuthSource(credentials=credentials, origin="explicit credentials")
    if token:
        return AuthSource(credentials=OAuthCredentials(access_token=token), origin="explicit token")
    if session_key:
        return AuthSource(session_key=session_key, origin="explicit session key")
    slot = cswap_slot if cswap_slot not in (None, "") else None
    if slot is None:
        jar = load_jar()
        if jar is not None:
            return AuthSource(jar=jar, origin=f"cookies:{jar.origin}")
        for env in (ENV_CLAUDE_AI_ACCESS_TOKEN, ENV_ACCESS_TOKEN, ENV_CLI_OAUTH_TOKEN):
            if os.environ.get(env):
                return AuthSource(credentials=OAuthCredentials(access_token=os.environ[env]), origin=f"env:{env}")
        slot = os.environ.get(ENV_CSWAP_SLOT) or cswap_current_slot()
    if slot is not None:
        def reload_slot(slot: str = str(slot)) -> OAuthCredentials:
            return cswap_credentials(slot, slot_token)

        return AuthSource(credentials=reload_slot(), origin=f"cswap slot {slot}", reload=reload_slot)

    def reload_file() -> OAuthCredentials:
        return load_credentials(credentials_path)

    return AuthSource(credentials=reload_file(), origin="credentials file", reload=reload_file)
