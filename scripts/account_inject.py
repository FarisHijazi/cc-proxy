"""Server mode: swap a placeholder bearer for the named account's real token.

Clients carry `Bearer proxy-account-<alias>`; this proxy holds the logins. A
real token passes through untouched, so a client's own login still works.
cswap owns every grant: the live default login is only read (claude refreshes
it), any other slot is refreshed through cswap's `consume_backup_grant`.
"""

import json
import time
from pathlib import Path

from mitmproxy import ctx, exceptions, http

PREFIX = "Bearer proxy-account-"
# Refresh a little early so a token never expires mid-request.
MARGIN_MS = 5 * 60 * 1000


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
        if token.get("expiresAt", 0) - MARGIN_MS <= time.time() * 1000:
            outcome = switcher.consume_backup_grant(slot, email, raw)
            if outcome.error is None:
                token = oauth.extract_oauth_data(outcome.credentials) or {}
    if not token.get("accessToken") or token.get("expiresAt", 0) <= time.time() * 1000:
        raise ValueError(f"cswap slot {slot} has no usable token; re-add it with cswap add")
    return token["accessToken"], token["expiresAt"]


class AccountInject:
    def __init__(self):
        self.accounts = {}
        self.cache = {}

    def load(self, loader):
        loader.add_option(
            "proxy_accounts_file", str, "", "Alias→cswap slot map for injected logins"
        )

    def configure(self, updated):
        if "proxy_accounts_file" not in updated:
            return
        try:
            data = json.loads(Path(ctx.options.proxy_accounts_file).read_text())
            self.accounts = {a: str(s) for a, s in data["accounts"].items()}
        except (OSError, ValueError, KeyError, AttributeError):
            raise exceptions.OptionsError("Invalid proxy accounts file") from None

    def token(self, alias):
        cached = self.cache.get(alias)
        if cached and cached[1] - MARGIN_MS > time.time() * 1000:
            return cached[0]
        self.cache[alias] = slot_token(self.accounts[alias])
        return self.cache[alias][0]

    def resolve(self, placeholder):
        alias = placeholder.removeprefix(PREFIX.removeprefix("Bearer "))
        if alias not in self.accounts:
            raise ValueError(f"unknown account alias {alias!r}")
        return self.token(alias)

    def websocket_message(self, flow):
        # The Chrome bridge authenticates in its first frame, not a header.
        message = flow.websocket.messages[-1]
        if not message.from_client or not message.is_text:
            return
        try:
            frame = json.loads(message.text)
        except ValueError:
            return
        supplied = frame.get("oauth_token") if isinstance(frame, dict) else None
        if frame.get("type") == "connect" and str(supplied).startswith("proxy-account-"):
            try:
                frame["oauth_token"] = self.resolve(supplied)
                message.text = json.dumps(frame)
            except Exception:
                message.drop()

    def requestheaders(self, flow):
        supplied = flow.request.headers.get("authorization", "")
        if not supplied.startswith(PREFIX):
            return
        try:
            flow.request.headers["authorization"] = "Bearer " + self.resolve(
                supplied.removeprefix("Bearer ")
            )
        except Exception as error:
            # Never forward a placeholder upstream.
            flow.response = http.Response.make(
                401,
                json.dumps({"type": "error", "error": {"type": "authentication_error", "message": str(error)}}),
                {"content-type": "application/json"},
            )


addons = [AccountInject()]
