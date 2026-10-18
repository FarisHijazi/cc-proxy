"""Account routing checks plus an actual SDK stdio subprocess round trip."""

import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, stdio_client

SCRIPT = Path(__file__).parents[1] / "scripts/account_chrome.py"
spec = importlib.util.spec_from_file_location("account_chrome", SCRIPT)
chrome = importlib.util.module_from_spec(spec)
spec.loader.exec_module(chrome)


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Account:
    def __init__(self, alias, *, error=None):
        self.alias = alias
        self.error = error
        self.calls = []
        self.fail_select = False

    async def list_tools(self):
        if self.error:
            raise RuntimeError("unavailable")
        return [
            types.Tool(name=name, inputSchema={"type": "object"})
            for name in (
                "list_connected_browsers",
                "select_browser",
                "read_page",
                "switch_browser",
            )
        ]

    async def call(self, params):
        self.calls.append(params)
        if self.error:
            return chrome.result_text("unavailable", error=True)
        if params.name == "list_connected_browsers":
            return chrome.result_text(
                json.dumps(
                    [{"deviceId": "same-device-id", "name": "Chrome", "inUse": True}]
                )
            )
        if params.name == "select_browser" and self.fail_select:
            return chrome.result_text("selection refused", error=True)
        return types.CallToolResult(
            content=[
                types.ImageContent(type="image", data="AA==", mimeType="image/png")
            ],
            structuredContent={"account": self.alias},
            _meta={"vendor": "preserved"},
            isError=params.name == "failing_tool",
        )


class Frontend:
    def __init__(self):
        self.notifications = []

    async def send_notification(self, notification):
        self.notifications.append(notification.model_dump(by_alias=True))


def setup_accounts(error=None):
    accounts = {
        alias: Account(alias, error=error if alias == "personal" else None)
        for alias in ("work", "personal")
    }
    aggregate = chrome.ChromeAccounts("work", accounts)
    return aggregate, accounts, SimpleNamespace(session=Frontend())


def request(name, arguments=None, meta=None):
    return types.CallToolRequestParams(name=name, arguments=arguments, _meta=meta)


@pytest.mark.anyio
async def test_discovery_namespaces_duplicate_devices_and_main_default():
    aggregate, accounts, context = setup_accounts()
    result = await aggregate.call(context, request("list_connected_browsers"))
    rows = json.loads(result.content[0].text)
    assert {row["deviceId"] for row in rows} == {
        "work__same-device-id",
        "personal__same-device-id",
    }
    assert [row["account"] for row in rows if row.get("inUse")] == ["work"]
    await aggregate.call(context, request("read_page", {"tabId": 4}))
    assert accounts["work"].calls[-1].name == "read_page"
    assert accounts["personal"].calls[-1].name == "list_connected_browsers"


@pytest.mark.anyio
async def test_device_namespace_is_injective_for_adversarial_aliases():
    class NamedAccount(Account):
        async def call(self, params):
            if params.name == "list_connected_browsers":
                device = "team__abc" if self.alias == "work" else "abc"
                return chrome.result_text(
                    json.dumps([{"deviceId": device, "name": "Chrome"}])
                )
            return await super().call(params)

    accounts = {alias: NamedAccount(alias) for alias in ("work", "work__team")}
    aggregate = chrome.ChromeAccounts("work", accounts)
    context = SimpleNamespace(session=Frontend())
    result = await aggregate.call(context, request("list_connected_browsers"))
    rows = json.loads(result.content[0].text)
    assert len({row["deviceId"] for row in rows}) == 2
    for row in rows:
        await aggregate.call(
            context, request("select_browser", {"deviceId": row["deviceId"]})
        )
        assert aggregate.selected == row["account"]


@pytest.mark.anyio
async def test_upload_requires_native_client_preprocessing_before_any_batch_action():
    class UploadAccount(Account):
        async def list_tools(self):
            return [
                types.Tool(name=name, inputSchema={"type": "object"})
                for name in ("file_upload", "browser_batch")
            ]

    accounts = {"work": UploadAccount("work")}
    aggregate = chrome.ChromeAccounts("work", accounts)
    context = SimpleNamespace(session=Frontend())
    listing = await aggregate.tools(context, None)
    assert all("cannot access" in tool.description for tool in listing.tools)
    for call in (
        request("file_upload", {"paths": ["/tmp/test.txt"], "tabId": 1}),
        request("file_upload", {"files": [{"data": "AA=="}], "tabId": 1}),
        request(
            "browser_batch",
            {
                "actions": [
                    {"name": "navigate", "input": {"tabId": 1}},
                    {"name": "file_upload", "input": {"paths": ["/tmp/test.txt"]}},
                ]
            },
        ),
        request("browser_batch", {"actions": [{"name": " FILE_UPLOAD "}]}),
        request("browser_batch", {"actions": [{"name": "browser_batch"}]}),
    ):
        result = await aggregate.call(context, call)
        assert result.is_error
        assert "No browser action ran" in result.content[0].text
    assert accounts["work"].calls == []
    await aggregate.call(
        context, request("browser_batch", {"actions": [{"name": "read_page"}]})
    )
    assert accounts["work"].calls[-1].name == "browser_batch"


@pytest.mark.anyio
async def test_selection_preserves_metadata_images_errors_and_owner():
    aggregate, accounts, context = setup_accounts()
    meta = {"claudecode/toolUseId": "call-1", "vendor": {"nested": True}}
    result = await aggregate.call(
        context,
        request("select_browser", {"deviceId": "personal__same-device-id"}, meta),
    )
    assert not result.is_error
    selected = accounts["personal"].calls[-1]
    assert selected.arguments == {"deviceId": "same-device-id"}
    assert selected.meta == meta
    result = await aggregate.call(context, request("failing_tool", {"tabId": 4}, meta))
    assert result.is_error
    assert result.content[0].type == "image"
    assert result.meta == {"vendor": "preserved"}
    assert result.structured_content == {"account": "personal"}


@pytest.mark.anyio
async def test_unavailable_secondary_is_explicit_without_poisoning_main():
    aggregate, _, context = setup_accounts(error="access token expired")
    result = await aggregate.call(context, request("list_connected_browsers"))
    assert not result.is_error
    assert len(json.loads(result.content[0].text)) == 1
    assert (
        result.structured_content["accountStatuses"]["personal"]
        == "access token expired"
    )
    assert "personal" in result.content[1].text


@pytest.mark.anyio
async def test_failed_selection_and_disconnect_never_fallback():
    aggregate, accounts, context = setup_accounts()
    accounts["personal"].fail_select = True
    result = await aggregate.call(
        context, request("select_browser", {"deviceId": "personal__same-device-id"})
    )
    assert result.is_error
    assert aggregate.selected == "work"
    accounts["personal"].fail_select = False
    await aggregate.call(
        context, request("select_browser", {"deviceId": "personal__same-device-id"})
    )
    accounts["personal"].error = "disconnected"
    main_calls = len(accounts["work"].calls)
    result = await aggregate.call(context, request("read_page", {"tabId": 4}))
    assert result.is_error
    assert len(accounts["work"].calls) == main_calls
    result = await aggregate.call(
        context, request("select_browser", {"deviceId": "other__same-device-id"})
    )
    assert result.is_error
    assert aggregate.selected == "personal"


@pytest.mark.anyio
async def test_native_names_hidden_permission_mode_and_switch_choices():
    aggregate, accounts, context = setup_accounts()
    listing = await aggregate.tools(context, None)
    assert {tool.name for tool in listing.tools} == {
        "list_connected_browsers",
        "select_browser",
        "read_page",
        "switch_browser",
    }
    result = await aggregate.call(
        context, request("set_permission_mode", {"mode": "ask"})
    )
    assert not result.is_error
    assert all(
        account.calls[-1].name == "set_permission_mode" for account in accounts.values()
    )
    result = await aggregate.call(context, request("switch_browser"))
    assert len(json.loads(result.content[0].text)) == 2
    assert "select_browser" in result.content[-1].text


@pytest.mark.anyio
async def test_vendor_notification_retains_payload_and_account_provenance():
    aggregate, _, context = setup_accounts()
    aggregate.frontend = context.session
    await aggregate.notification(
        "personal",
        "notifications/message",
        {"prompt": "Please read this tab", "tabId": 42, "_meta": {"vendor": True}},
    )
    wire = context.session.notifications[0]
    assert wire["params"]["tabId"] == 42
    assert wire["params"]["_meta"] == {
        "vendor": True,
        "claude-codex/account": "personal",
    }
    assert "personal" in wire["params"]["prompt"]


def test_child_environment_keeps_session_transport_but_isolates_identity():
    env = chrome.child_environment(
        {"config_dir": "/tmp/work/config", "secure_storage_dir": "/tmp/work/creds"},
        {
            "ANTHROPIC_API_KEY": "toy-key",
            "CLAUDE_CODE_OAUTH_TOKEN": "toy-token",
            "CLAUDE_CODE_OAUTH_REFRESH_TOKEN": "toy-refresh",
            "CLAUDE_CODE_ACCOUNT_UUID": "toy-account",
            "CLAUDE_CHROME_PAIRED_DEVICE_ID": "toy-browser",
            "HTTPS_PROXY": "http://127.0.0.1:9999",
            "NODE_EXTRA_CA_CERTS": "/tmp/session-ca.pem",
            "CLAUDE_CODE_SESSION_ID": "toy-session",
        },
    )
    assert not (chrome.AUTH_ENV & env.keys())
    assert env["CLAUDE_CODE_SESSION_ID"] == "toy-session"
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:9999"
    assert env["CLAUDE_SECURESTORAGE_CONFIG_DIR"] == "/tmp/work/creds"
    assert env["ANTHROPIC_BASE_URL"] == "https://api.anthropic.com"


def snapshot(tmp_path, alias):
    directory = tmp_path / alias
    directory.mkdir()
    (directory / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "toy-token",
                    "expiresAt": int((time.time() + 3600) * 1000),
                    "scopes": ["user:profile", "user:inference"],
                }
            }
        )
    )
    return {"config_dir": str(directory), "secure_storage_dir": str(directory)}


def test_snapshot_refuses_refresh_and_expired_credentials(tmp_path):
    account = snapshot(tmp_path, "work")
    path = Path(account["secure_storage_dir"]) / ".credentials.json"
    assert chrome.snapshot_error(account) is None
    data = json.loads(path.read_text())
    data["claudeAiOauth"]["refreshToken"] = "toy-refresh"
    path.write_text(json.dumps(data))
    assert "refresh grant" in chrome.snapshot_error(account)
    data["claudeAiOauth"].pop("refreshToken")
    data["claudeAiOauth"]["expiresAt"] = 1
    path.write_text(json.dumps(data))
    assert "expired" in chrome.snapshot_error(account)


@pytest.mark.anyio
async def test_real_stdio_native_children_keep_state_and_terminate(tmp_path):
    native = tmp_path / "native"
    native.write_text(
        f"#!{sys.executable}\n"
        + """
import json, os
from pathlib import Path
import anyio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

account = Path(os.environ["CLAUDE_CONFIG_DIR"])
(account / "pid").write_text(str(os.getpid()))
selected = None

async def tools(context, params):
    return types.ListToolsResult(tools=[types.Tool(name=name, inputSchema={"type":"object"}) for name in ["list_connected_browsers", "select_browser", "read_page"]])

async def call(context, params):
    global selected
    if params.name == "list_connected_browsers":
        data = [{"deviceId":"browser-1", "name":"Chrome"}]
    elif params.name == "select_browser":
        selected = params.arguments["deviceId"]
        data = {"selected":selected}
    else:
        data = {"account":account.name, "selected":selected, "meta":params.meta}
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(data))], _meta={"native":True})

async def main():
    server = Server("Native fixture", on_list_tools=tools, on_call_tool=call)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
anyio.run(main)
"""
    )
    native.chmod(0o700)
    accounts = {alias: snapshot(tmp_path, alias) for alias in ("work", "personal")}
    manifest = tmp_path / "accounts.json"
    manifest.write_text(
        json.dumps({"main": "work", "accounts": accounts, "claude": str(native)})
    )
    params = StdioServerParameters(
        command=sys.executable, args=[str(SCRIPT), "--manifest", str(manifest)]
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write, read_timeout_seconds=20) as client,
    ):
        await client.initialize()
        listing = await client.list_tools()
        assert len(listing.tools) == 3
        browsers = await client.call_tool("list_connected_browsers")
        assert len(json.loads(browsers.content[0].text)) == 2
        await client.call_tool("select_browser", {"deviceId": "personal__browser-1"})
        result = await client.call_tool(
            "read_page", {"tabId": 1}, meta={"vendor": "kept"}
        )
        data = json.loads(result.content[0].text)
        assert data == {
            "account": "personal",
            "selected": "browser-1",
            "meta": {"vendor": "kept"},
        }
        assert result.meta == {"native": True}
        # Hidden tools are routed even though tools/list did not advertise them.
        result = await client.call_tool("set_permission_mode", {"mode": "ask"})
        assert not result.is_error
    for account in accounts.values():
        pid = int((Path(account["config_dir"]) / "pid").read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


@pytest.mark.anyio
@pytest.mark.skipif(
    not os.environ.get("CHROME_ACCOUNT_LIVE_MANIFEST"),
    reason="requires private access-only snapshots",
)
async def test_live_native_accounts_discover_and_select():
    manifest = os.environ["CHROME_ACCOUNT_LIVE_MANIFEST"]
    configured = json.loads(Path(manifest).read_text())
    params = StdioServerParameters(
        command=sys.executable, args=[str(SCRIPT), "--manifest", manifest]
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write, read_timeout_seconds=90) as client,
    ):
        await client.initialize()
        tools = await client.list_tools()
        assert {"list_connected_browsers", "select_browser", "read_page"} <= {
            tool.name for tool in tools.tools
        }
        result = await client.call_tool("list_connected_browsers")
        rows = json.loads(result.content[0].text)
        statuses = result.structured_content["accountStatuses"]
        assert all(status == "available" for status in statuses.values()), statuses
        for alias in configured["accounts"]:
            candidates = [row for row in rows if row["account"] == alias]
            assert candidates, f"no connected browser for {alias}"
            selected = await client.call_tool(
                "select_browser", {"deviceId": candidates[0]["deviceId"]}
            )
            assert not selected.is_error, f"native selection failed for {alias}"
            after = await client.call_tool("list_connected_browsers")
            used = [
                row["account"]
                for row in json.loads(after.content[0].text)
                if row.get("inUse")
            ]
            assert used == [alias]
            print(
                f"Native account {alias}: discovered {len(candidates)} browser(s), selected successfully"
            )
