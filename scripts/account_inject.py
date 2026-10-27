"""Server mode: swap a placeholder bearer for the named account's real token.

Clients carry `Bearer proxy-account-<alias>`; this proxy holds the logins. A
real token passes through untouched, so a client's own login still works.
cswap owns every grant: the live default login is only read (claude refreshes
it), any other slot is refreshed through cswap's `consume_backup_grant`.
"""

import json
import re
import runpy
import time
from pathlib import Path
from uuid import UUID

from mitmproxy import ctx, exceptions, http
from mitmproxy.net import server_spec

PREFIX = "Bearer proxy-account-"
REMOTE_CONTROL_PATH = "/v1/code/"
SESSIONS_PATH = "/v1/code/sessions"
# Refresh a little early so a token never expires mid-request.
MARGIN_MS = 5 * 60 * 1000
# A `claude setup-token` has no expiry or refresh grant (about a year, inference only);
# re-read it this often so a replaced one is picked up.
SETUP_TOKEN_REREAD_MS = 60 * 60 * 1000
# Ordered logins for the router's quota fallback (src/fallback.rs). Loopback only:
# the router strips it before anything is relayed upstream.
POOL_HEADER = "x-claude-codex-account-pool"
UNUSABLE_RETRY_S = 300
# How the first account of a session is chosen (accounts file "balance.strategy"):
# session = the placeholder's own alias, prefer = "balance.prefer" for everyone,
# round-robin / least-sessions = spread new sessions, then stick to that account.
STRATEGIES = {"session", "prefer", "round-robin", "least-sessions"}
SESSION_HEADER = "x-claude-code-session-id"
# Prompt caches last at most an hour, so an idle session may be reassigned.
STICKY_IDLE_S = 3600


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
        if token.get("expiresAt", 0) - MARGIN_MS <= time.time() * 1000:
            outcome = switcher.consume_backup_grant(slot, email, raw)
            if outcome.error is None:
                token = oauth.extract_oauth_data(outcome.credentials) or {}
    if not token.get("accessToken") or token.get("expiresAt", 0) <= time.time() * 1000:
        raise ValueError(f"cswap slot {slot} has no usable token; re-add it with cswap add")
    return token["accessToken"], token["expiresAt"]


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


class AccountInject:
    def __init__(self):
        self.accounts = {}
        self.renamed = {}
        self.cache = {}
        self.unusable = {}
        self.strategy, self.prefer = "session", None
        self.sticky, self.turn = {}, -1
        self.chrome = None
        self.upstream = None
        self.traffic_db = None
        self.traffic_days = None
        self.disabled = set()
        self.chrome_profiles_file = Path.home() / ".claude/chrome-profiles.json"

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

    def read_accounts(self, path):
        self.accounts_file = path
        self.accounts_mtime = path.stat().st_mtime
        data = json.loads(path.read_text())
        self.accounts = {a: str(s) for a, s in data["accounts"].items()}
        # Old alias -> current alias, so sessions launched before a rename keep working.
        self.renamed = dict(data.get("renamed", {}))
        # Where account_traffic.py records every flow; unset records nothing.
        self.traffic_db = data.get("traffic_db")
        self.traffic_days = data.get("traffic_days")
        # Switched off (in the UI): never tried, unless a session names the alias itself.
        self.disabled = set(data.get("disabled", []))
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
            if path is not None and path.stat().st_mtime != self.accounts_mtime:
                self.read_accounts(path)
        except (OSError, ValueError, KeyError, AttributeError):
            pass

    def token(self, alias):
        cached = self.cache.get(alias)
        if cached and cached[1] - MARGIN_MS > time.time() * 1000:
            return cached[0]
        self.cache[alias] = slot_token(self.accounts[alias])
        return self.cache[alias][0]

    def name(self, alias):
        return self.renamed.get(alias, alias)

    def start(self, alias, session):
        """The account a session tries first. Sessions never round-robin per request:
        a session keeps its account, because prompt caches are per account."""
        if self.strategy == "prefer":
            return alias if self.prefer in self.disabled else self.prefer
        if self.strategy == "session" or not session:
            return alias
        now = time.time()
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
        alias = alias if pinned else self.start(alias, session)
        pool = []
        for name in [alias, *(a for a in self.accounts if a != alias and a not in self.disabled)]:
            if self.unusable.get(name, 0) > time.time():
                continue
            try:
                pool.append({"alias": name, "authorization": "Bearer " + self.token(name), "primary": name == alias})
            except Exception:  # noqa: BLE001 - one broken slot must not block the rest
                self.unusable[name] = time.time() + UNUSABLE_RETRY_S
        return pool

    def cascades(self, placeholder, path=""):
        """True when the next proxy, not this one, holds the login a placeholder names."""
        if not self.upstream:
            return False
        alias, owner, _ = parse(placeholder)
        if owner and path.startswith(REMOTE_CONTROL_PATH):
            alias = owner
        return self.name(alias) not in self.accounts

    def resolve(self, placeholder, path=""):
        alias, owner, _ = parse(placeholder)
        if owner and path.startswith(REMOTE_CONTROL_PATH):
            alias = owner
        alias = self.name(alias)
        if alias not in self.accounts:
            raise ValueError(f"unknown account alias {alias!r}")
        return self.token(alias)

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
        if self.chrome is not None:
            self.chrome.close()

    def requestheaders(self, flow):
        # Only this addon may name the pool; a client-supplied one never survives.
        flow.request.headers.pop(POOL_HEADER, None)
        if self.upstream and flow.request.host == "bridge.claudeusercontent.com":
            # The router reaches Anthropic through the next proxy; the Chrome bridge must too.
            flow.server_conn.via = self.upstream
        supplied = flow.request.headers.get("authorization", "")
        if not supplied.startswith(PREFIX):
            return
        placeholder = supplied.removeprefix("Bearer ")
        self.reload_accounts()
        try:
            if self.cascades(placeholder, flow.request.path):
                return  # the next proxy swaps it
            if flow.request.method == "POST" and flow.request.path.split("?")[0] == "/v1/messages":
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
                return
            token = self.resolve(placeholder, flow.request.path)
            private = parse(placeholder)[2]
            # account_remote_route reads this after the placeholder is gone.
            flow.metadata["account_private"] = private
            if private and flow.request.path.startswith(SESSIONS_PATH):
                # HQ's route never needs this token; without the route, Anthropic
                # must refuse the session rather than show it on claude.ai.
                del flow.request.headers["authorization"]
                return
            flow.request.headers["authorization"] = "Bearer " + token
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
