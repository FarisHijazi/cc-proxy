# /// script
# requires-python = ">=3.11"
# dependencies = ["fastmcp==4.1.1"]
# ///
"""Aggregate account-isolated native Claude Chrome MCP servers over stdio.

The launcher owns credential snapshots. This process only reads its manifest and
delegates Chrome transport, policy and tool execution to the installed Claude CLI.
"""

import argparse
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import ClassVar

import anyio
from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from pydantic import ConfigDict, TypeAdapter

ALIASES = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,23}\Z")
RESULT = TypeAdapter(types.CallToolResult | types.InputRequiredResult)
UPLOAD_LIMITATION = (
    "File uploads require native Claude-in-Chrome client file-permission preprocessing, "
    "which account-chrome cannot access. Use native Chrome in a session whose main "
    "account owns the intended browser."
)
AUTH_ENV = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_UNIX_SOCKET",
    "CLAUDE_CODE_ACCOUNT_UUID",
    "CLAUDE_CODE_ORGANIZATION_UUID",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
    "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
    "CLAUDE_CODE_HOST_CREDS_FILE",
    "CLAUDE_CODE_HOST_AUTH_ENV_VAR",
    "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
    "CLAUDE_CODE_GATEWAY_TOKEN_FILE_DESCRIPTOR",
    "CLAUDE_CODE_WEBSOCKET_AUTH_FILE_DESCRIPTOR",
    "CLAUDE_CODE_SESSION_ACCESS_TOKEN",
    "CLAUDE_CODE_REMOTE_SESSION_ID",
    "SESSION_INGRESS_URL",
    "CLAUDE_CHROME_PAIRED_DEVICE_ID",
    "CLAUDE_CODE_CHROME_MCP_ORG_DENIED",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_MANTLE",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD",
    "USE_LOCAL_OAUTH",
    "USE_STAGING_OAUTH",
    "LOCAL_BRIDGE",
}


def result_text(text, *, error=False, structured=None):
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        isError=error,
        structuredContent=structured,
    )


def read_manifest(path):
    data = json.loads(Path(path).read_text())
    accounts = data.get("accounts")
    if not isinstance(accounts, dict) or not accounts:
        raise ValueError("manifest must contain accounts")
    if data.get("main") not in accounts:
        raise ValueError("manifest main must name a configured account")
    if not isinstance(data.get("claude"), str) or not data["claude"]:
        raise ValueError("manifest must specify the Claude executable")
    for alias, account in accounts.items():
        if not ALIASES.fullmatch(alias) or not isinstance(account, dict):
            raise ValueError("manifest contains an invalid account alias or entry")
        if "error" in account:
            continue
        for name in ("config_dir", "secure_storage_dir"):
            value = account.get(name)
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise ValueError("account snapshot directories must be absolute")
            if Path(value).resolve() == (Path.home() / ".claude").resolve():
                raise ValueError("Chrome accounts require isolated login snapshots")
    return data


def snapshot_error(account):
    """Refuse refresh-capable or expired credentials before spawning Claude."""
    if "error" in account:
        return "account snapshot unavailable; renew the source login and relaunch"
    try:
        data = json.loads(
            (Path(account["secure_storage_dir"]) / ".credentials.json").read_text()
        )
        oauth = data.get("claudeAiOauth", {})
        if oauth.get("refreshToken"):
            return "snapshot contains a refresh grant; recreate it with the launcher"
        if not oauth.get("accessToken"):
            return "snapshot has no access token"
        if "user:profile" not in oauth.get("scopes", []):
            return "snapshot needs a full Claude login with user:profile scope"
        if float(oauth.get("expiresAt", 0)) <= time.time() * 1000:
            return "access token expired; renew the source login and relaunch"
    except (OSError, ValueError, TypeError, AttributeError):
        return "account snapshot could not be read"
    return None


def child_environment(account, environ=None):
    env = dict(os.environ if environ is None else environ)
    for key in AUTH_ENV:
        env.pop(key, None)
    env.update(
        CLAUDE_CONFIG_DIR=account["config_dir"],
        CLAUDE_SECURESTORAGE_CONFIG_DIR=account["secure_storage_dir"],
        ANTHROPIC_BASE_URL="https://api.anthropic.com",
        CLAUDECODE="1",
    )
    # Keep session HTTP proxy/CA and session IDs; they belong to this launcher.
    return env


class NativeSession(ClientSession):
    """Retain vendor notifications which the SDK's core schema does not know."""

    def __init__(self, *args, forward_notification, **kwargs):
        super().__init__(*args, **kwargs)
        self.forward_notification = forward_notification

    async def _on_notify(self, context, method, params):
        if method == "notifications/cancelled":
            return await super()._on_notify(context, method, params)
        await self.forward_notification(method, dict(params or {}))


class VendorNotificationParams(types.NotificationParams):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")


class NativeAccount:
    def __init__(self, alias, account, command, notify):
        self.alias = alias
        self.account = account
        self.command = command
        self.notify = notify
        self.error = snapshot_error(account)
        self.session = None

    async def serve(self, *, task_status=anyio.TASK_STATUS_IGNORED):
        if self.error:
            task_status.started()
            return
        started = False
        try:
            params = StdioServerParameters(
                command=self.command,
                args=["--claude-in-chrome-mcp"],
                env=child_environment(self.account),
            )
            # Native debug logs may contain account identifiers or browser data.
            with open(os.devnull, "w") as stderr:  # noqa: ASYNC230 - local null device
                async with (
                    stdio_client(params, errlog=stderr) as (read, write),
                    NativeSession(
                        read,
                        write,
                        forward_notification=self.notify,
                        read_timeout_seconds=150,
                    ) as session,
                ):
                    with anyio.fail_after(20):
                        await session.initialize()
                    self.session = session
                    task_status.started()
                    started = True
                    await anyio.sleep_forever()
        except Exception:  # noqa: BLE001 - isolate native failures and redact their payloads
            # Never include exception bodies: native failures can contain tokens.
            self.error = "native Chrome connection unavailable; relaunch after checking the source login"
            if not started:
                task_status.started()
        finally:
            self.session = None

    async def list_tools(self):
        if self.error or self.session is None:
            raise RuntimeError("native account unavailable")
        tools = []
        cursor = None
        while True:
            page = await self.session.list_tools(
                params=types.PaginatedRequestParams(cursor=cursor) if cursor else None
            )
            tools.extend(page.tools)
            if not page.next_cursor:
                return tools
            cursor = page.next_cursor

    async def call(self, params):
        if self.error or self.session is None:
            return result_text(
                f"Account {self.alias}: {self.error or 'not connected'}", error=True
            )
        error = snapshot_error(self.account)
        if error:
            self.error = error
            return result_text(f"Account {self.alias}: {error}", error=True)
        # send_request skips output-schema revalidation and accepts hidden native
        # tools such as set_permission_mode; the original _meta survives intact.
        return await self.session.send_request(
            types.CallToolRequest(params=params), RESULT
        )


class ChromeAccounts:
    def __init__(self, main, accounts):
        self.main = main
        self.accounts = accounts
        self.selected = main
        self.selected_device = None
        self.devices = {}
        self.statuses = {}
        self.lock = anyio.Lock()
        self.frontend = None

    async def notification(self, alias, method, params):
        if self.frontend is None:
            return
        params = dict(params)
        params["_meta"] = {**params.get("_meta", {}), "claude-codex/account": alias}
        if isinstance(params.get("prompt"), str):
            params["prompt"] = (
                f"[Browser account {alias}; select its browser before acting] "
                + params["prompt"]
            )
        notification = types.Notification(
            method=method, params=VendorNotificationParams.model_validate(params)
        )
        await self.frontend.send_notification(notification)

    async def _call(self, alias, params):
        try:
            return await self.accounts[alias].call(params)
        except Exception:  # noqa: BLE001 - errors from another process may contain credentials
            return result_text(
                f"Account {alias}: native Chrome request failed; retry this account or relaunch",
                error=True,
            )

    async def tools(self, context, params):
        self.frontend = context.session
        tools = {}
        for alias, account in self.accounts.items():
            try:
                with anyio.fail_after(20):
                    for tool in await account.list_tools():
                        tools.setdefault(tool.name, tool)
            except Exception:  # noqa: BLE001 - one failed account must not hide healthy accounts
                self.statuses[alias] = "native tool discovery unavailable"
        for name in ("list_connected_browsers", "switch_browser"):
            if name in tools:
                tools[name] = tools[name].model_copy(
                    update={
                        "description": (
                            "List connected browsers across configured Claude accounts. "
                            "The browser name includes its account alias. Select the intended "
                            "browser using select_browser with the returned deviceId. "
                            "Unavailable accounts are reported separately."
                        )
                    }
                )
        for name in ("file_upload", "browser_batch"):
            if name in tools:
                tools[name] = tools[name].model_copy(
                    update={
                        "description": UPLOAD_LIMITATION
                        + (
                            " Other browser batch actions are supported. "
                            if name == "browser_batch"
                            else " "
                        )
                        + (tools[name].description or "")
                    }
                )
        return types.ListToolsResult(tools=list(tools.values()))

    async def browsers(self, params):
        rows = []
        devices = {}
        statuses = {}

        async def discover(alias):
            call = params.model_copy(
                update={"name": "list_connected_browsers", "arguments": {}}
            )
            response = await self._call(alias, call)
            if not isinstance(response, types.CallToolResult) or response.is_error:
                statuses[alias] = (
                    self.accounts[alias].error or "browser discovery failed"
                )
                return
            try:
                text = next(
                    block.text for block in response.content if block.type == "text"
                )
                browsers = json.loads(text)
                if not isinstance(browsers, list):
                    raise TypeError("not a browser list")
                for browser in browsers:
                    device = browser["deviceId"]
                    if not isinstance(device, str) or not re.fullmatch(
                        r"[A-Za-z0-9_-]{1,64}", device
                    ):
                        raise ValueError("invalid browser ID")
            except (ValueError, TypeError, KeyError, StopIteration, AttributeError):
                statuses[alias] = "native browser discovery returned an invalid list"
                return
            statuses[alias] = "available"
            for browser in browsers:
                device = browser["deviceId"]
                # Escaping underscores makes the separator unambiguous even
                # when an alias and a native ID both contain double underscores.
                public = f"{alias.replace('_', '_u')}__{device}"
                row = {**browser, "deviceId": public, "account": alias}
                row["name"] = f"{alias}: {browser.get('name') or 'Chrome'}"
                row.pop("inUse", None)
                if alias == self.selected and (
                    device == self.selected_device
                    or self.selected_device is None
                    and browser.get("inUse")
                ):
                    row["inUse"] = True
                rows.append(row)
                devices[public] = (alias, device)

        async with anyio.create_task_group() as group:
            for alias in self.accounts:
                group.start_soon(discover, alias)
        self.devices = devices
        self.statuses = statuses
        rows.sort(
            key=lambda row: (
                row["account"] != self.main,
                row["account"],
                row["name"],
                row["deviceId"],
            )
        )
        result = result_text(
            json.dumps(rows),
            error=all(status != "available" for status in statuses.values()),
            structured={"browsers": rows, "accountStatuses": statuses},
        )
        failures = [
            f"{alias}: {status}"
            for alias, status in statuses.items()
            if status != "available"
        ]
        if failures:
            result.content.append(
                types.TextContent(
                    type="text", text="Unavailable accounts: " + "; ".join(failures)
                )
            )
        if params.name == "switch_browser":
            result.content.append(
                types.TextContent(
                    type="text",
                    text="Choose a browser from this list with select_browser. Cross-account selection uses this list instead of an account-local pairing popup.",
                )
            )
        return result

    async def call(self, context, params):
        self.frontend = context.session
        async with self.lock:
            actions = (params.arguments or {}).get("actions", [])
            if params.name == "file_upload" or (
                params.name == "browser_batch"
                and isinstance(actions, list)
                and any(
                    isinstance(action, dict)
                    and str(action.get("name", "")).strip().lower()
                    in {"file_upload", "browser_batch"}
                    for action in actions
                )
            ):
                # The parent CLI only applies file permissions and prepares
                # uploads for its reserved native Chrome server name. Reject a
                # whole upload batch before any preceding action can execute.
                return result_text(
                    UPLOAD_LIMITATION + " No browser action ran.", error=True
                )
            if params.name in {"list_connected_browsers", "switch_browser"}:
                return await self.browsers(params)
            if params.name == "select_browser":
                public = (params.arguments or {}).get("deviceId")
                # Refresh membership before routing so stale/forged IDs cannot
                # select a different account or an unlisted browser.
                await self.browsers(params)
                if not isinstance(public, str) or public not in self.devices:
                    return result_text(
                        "Browser is not connected. Call list_connected_browsers and select a returned deviceId.",
                        error=True,
                    )
                alias, device = self.devices[public]
                forwarded = params.model_copy(
                    update={"arguments": {**params.arguments, "deviceId": device}}
                )
                result = await self._call(alias, forwarded)
                if isinstance(result, types.CallToolResult) and not result.is_error:
                    self.selected = alias
                    self.selected_device = device
                return result
            if params.name == "set_permission_mode":
                failures = []
                available = 0
                for alias, account in self.accounts.items():
                    if account.error:
                        continue
                    available += 1
                    response = await self._call(alias, params)
                    if (
                        not isinstance(response, types.CallToolResult)
                        or response.is_error
                    ):
                        failures.append(alias)
                if failures:
                    return result_text(
                        "Permission mode was not acknowledged by accounts: "
                        + ", ".join(failures),
                        error=True,
                    )
                if not available:
                    return result_text(
                        "No browser accounts are available to acknowledge permission mode.",
                        error=True,
                    )
                return result_text(
                    "Permission mode forwarded to available browser accounts."
                )
            return await self._call(self.selected, params)


def make_server(manifest):
    aggregate = ChromeAccounts(manifest["main"], {})
    for alias, account in manifest["accounts"].items():

        async def notify(method, params, owner=alias):
            await aggregate.notification(owner, method, params)

        aggregate.accounts[alias] = NativeAccount(
            alias, account, manifest["claude"], notify
        )

    @asynccontextmanager
    async def lifespan(server):
        async with anyio.create_task_group() as group:
            for account in aggregate.accounts.values():
                await group.start(account.serve)
            try:
                yield aggregate
            finally:
                group.cancel_scope.cancel()

    return Server(
        "Claude in Chrome",
        version="1.0.0",
        lifespan=lifespan,
        on_list_tools=aggregate.tools,
        on_call_tool=aggregate.call,
    )


async def serve(manifest):
    server = make_server(manifest)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    # SDK validation errors can include the invalid payload; do not log them.
    logging.disable(logging.CRITICAL)
    try:
        manifest = read_manifest(args.manifest)
        anyio.run(serve, manifest)
    except (OSError, ValueError):
        parser.exit(1, "Chrome account manifest could not be loaded.\n")


if __name__ == "__main__":
    main()
