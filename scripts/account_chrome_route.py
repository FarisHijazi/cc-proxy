"""Select native Chrome's bridge identity without changing the main CLI login.

mitmproxy owns TLS/WebSocket transport. Only the exact account path and the
initial Claude Code connect token change; tool and permission traffic is opaque.
"""

import hmac
import json
import time
from pathlib import Path
from uuid import UUID

from mitmproxy import ctx, exceptions


def read_identity(entry):
    if entry.get("error"):
        raise ValueError("Chrome account snapshot unavailable")
    profile = json.loads((Path(entry["config_dir"]) / ".claude.json").read_text())[
        "oauthAccount"
    ]
    identity = str(UUID(profile["accountUuid"]))
    oauth = json.loads(
        (Path(entry["secure_storage_dir"]) / ".credentials.json").read_text()
    )["claudeAiOauth"]
    if (
        not isinstance(oauth.get("accessToken"), str)
        or not oauth["accessToken"]
        or oauth.get("refreshToken")
        or "user:profile" not in oauth.get("scopes", [])
        or oauth.get("expiresAt", 0) <= time.time() * 1000
    ):
        raise ValueError("Chrome requires an unexpired access-only full login")
    return identity, oauth["accessToken"]


class NativeChromeRoute:
    def load(self, loader):
        loader.add_option(
            "chrome_account_manifest", str, "", "Private account manifest path"
        )

    def configure(self, updated):
        if "chrome_account_manifest" not in updated:
            return
        try:
            manifest = json.loads(Path(ctx.options.chrome_account_manifest).read_text())
            self.source = manifest["accounts"][manifest["main"]]
            self.target = manifest["accounts"][manifest["chrome_account"]]
            self.source_id, _ = read_identity(self.source)
            self.target_id, _ = read_identity(self.target)
        except (OSError, ValueError, TypeError, KeyError):
            raise exceptions.OptionsError(
                "Native Chrome account snapshots are unavailable or invalid"
            ) from None

    def request(self, flow):
        request = flow.request
        if (
            request.scheme == "https"
            and request.host == "bridge.claudeusercontent.com"
            and request.port == 443
            and request.method == "GET"
            and request.path == f"/chrome/{self.source_id}"
            and request.headers.get("upgrade", "").lower() == "websocket"
        ):
            request.path = f"/chrome/{self.target_id}"
            flow.metadata["native_chrome_route"] = True

    def websocket_message(self, flow):
        if not flow.metadata.get("native_chrome_route"):
            return
        message = flow.websocket.messages[-1]
        if not message.from_client or flow.metadata.get("native_chrome_authenticated"):
            return
        try:
            payload = json.loads(message.content)
            source_id, source_token = read_identity(self.source)
            target_id, target_token = read_identity(self.target)
            supplied = payload.get("oauth_token")
            if (
                source_id != self.source_id
                or target_id != self.target_id
                or payload.get("type") != "connect"
                or payload.get("client_type") != "claude-code"
                or not isinstance(supplied, str)
                or not hmac.compare_digest(supplied, source_token)
            ):
                raise ValueError("Unexpected bridge authentication")
            payload["oauth_token"] = target_token
            message.content = json.dumps(payload).encode()
            flow.metadata["native_chrome_authenticated"] = True
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            # Never forward the wrong account credential, or echo token payloads.
            message.drop()
            flow.kill()


addons = [NativeChromeRoute()]
