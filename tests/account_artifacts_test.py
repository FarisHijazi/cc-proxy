"""Account selection, partial failure and secret boundaries for artifact access."""

import importlib.util
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from claude_artifact_cli.api import ApiError
from fastmcp import Client

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts/account_artifacts.py"
spec = importlib.util.spec_from_file_location("account_artifacts", MODULE_PATH)
artifacts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(artifacts)


@pytest.fixture
def manifest(tmp_path):
    accounts = {}
    for name in ("work", "personal"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / ".credentials.json").write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": f"toy-{name}",
                        "expiresAt": (time.time() + 60) * 1000,
                        "scopes": ["user:profile"],
                    }
                }
            )
        )
        accounts[name] = {
            "secure_storage_dir": str(directory),
            "config_dir": str(tmp_path / "unused"),
        }
    return {"main": "work", "accounts": accounts}


SLUG = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_aggregate_partial_failure_does_not_fallback(manifest):
    called = []

    def factory(token, timeout):
        called.append(token)
        if token == "toy-personal":
            raise ApiError("contains secret bearer xyz", 401)
        return SimpleNamespace(
            list_frames=lambda limit: [
                {
                    "slug": SLUG,
                    "title": "Page",
                    "assetToken": "secret",
                    "thumbImgURL": "https://example.com/?token=secret",
                }
            ]
        )

    result = artifacts.ArtifactAccounts(manifest, factory).list_artifacts()
    assert called == ["toy-work", "toy-personal"]
    assert [item["status"] for item in result["accounts"]] == ["ok", "error"]
    assert result["accounts"][0]["artifacts"][0]["account"] == "work"
    assert "artifacts" not in result["accounts"][1]
    assert "secret" not in json.dumps(result)


def test_read_account_selected_and_metadata_secrets_removed(manifest):
    called = []

    def factory(token, timeout):
        called.append(token)
        return SimpleNamespace(
            read=lambda slug: {
                "title": "Page",
                "assetToken": "secret",
                "share_key": "secret",
                "perm": {"role": "owner", "share_key": "secret"},
                "files": [
                    {
                        "path": "index.html",
                        "url": "https://example.com/?__frame_t=secret",
                    }
                ],
            }
        )

    adapter = artifacts.ArtifactAccounts(manifest, factory)
    result = adapter.read_artifact(SLUG, "personal")
    assert result["account"] == "personal"
    assert result["metadata"]["perm"] == {"role": "owner"}
    assert "secret" not in json.dumps(result)
    assert adapter.read_artifact(SLUG)["account"] == "work"
    assert called == ["toy-personal", "toy-work"]


def test_unknown_and_unavailable_sources_never_use_main(manifest):
    manifest["accounts"]["expired"] = {
        "error": "do not expose arbitrary metadata secrets"
    }
    adapter = artifacts.ArtifactAccounts(
        manifest, lambda *a, **kw: pytest.fail("must not reach network")
    )
    for account in ("unknown", "expired"):
        assert adapter.read_artifact(SLUG, account)["status"] == "error"
        row = adapter.list_artifacts(account)["accounts"][0]
        assert row["status"] == "error"
        assert "artifacts" not in row
        assert "secrets" not in json.dumps(row)


def test_expired_snapshot_never_calls_upstream(manifest):
    path = (
        Path(manifest["accounts"]["work"]["secure_storage_dir"]) / ".credentials.json"
    )
    document = json.loads(path.read_text())
    document["claudeAiOauth"]["expiresAt"] = 0
    path.write_text(json.dumps(document))
    before = path.read_bytes()
    adapter = artifacts.ArtifactAccounts(
        manifest, lambda *a, **kw: pytest.fail("must not refresh")
    )
    assert "expired" in adapter.read_artifact(SLUG)["error"]
    assert path.read_bytes() == before


@pytest.mark.parametrize("path", ["../secret", "/absolute", "a//b", "bad%20path"])
def test_invalid_file_path_rejected_before_network(manifest, path):
    adapter = artifacts.ArtifactAccounts(
        manifest, lambda *a, **kw: pytest.fail("must not access network")
    )
    assert adapter.read_artifact(SLUG, path=path)["status"] == "error"


@pytest.mark.parametrize(
    "content,encoding", [(b"hello", "utf-8"), (b"\xff\x00", "base64")]
)
def test_file_transport_delegates_to_upstream_reader(manifest, content, encoding):
    paths = []
    reader = SimpleNamespace(
        files={"index.html": {"size": len(content)}},
        version="v1",
        fetch=lambda path: paths.append(path) or content,
    )
    adapter = artifacts.ArtifactAccounts(
        manifest, lambda *a, **kw: SimpleNamespace(files=lambda slug: reader)
    )
    result = adapter.read_artifact(SLUG, path="index.html")
    assert paths == ["index.html"]
    assert result["status"] == "ok"
    assert result["encoding"] == encoding
    assert result["bytes"] == len(content)


def test_file_size_limit_before_fetch(manifest):
    reader = SimpleNamespace(
        files={"index.html": {"size": artifacts.MAX_FILE_BYTES + 1}},
        fetch=lambda path: pytest.fail("should not fetch"),
    )
    adapter = artifacts.ArtifactAccounts(
        manifest, lambda *a, **kw: SimpleNamespace(files=lambda slug: reader)
    )
    assert adapter.read_artifact(SLUG, path="index.html")["status"] == "error"


@pytest.mark.asyncio
async def test_mcp_exposes_readonly_tools_and_explicit_account_errors(manifest):
    manifest["accounts"] = {"work": {"error": "expired"}}
    async with Client(artifacts.create_server(manifest)) as client:
        tools = await client.list_tools()
        assert {tool.name for tool in tools} == {"list_artifacts", "read_artifact"}
        assert all(tool.annotations.read_only_hint for tool in tools)
        result = await client.call_tool("list_artifacts", {})
        assert result.data["accounts"][0]["status"] == "error"
        result = await client.call_tool("read_artifact", {"artifact": SLUG})
        assert result.data["account"] == "work"
        assert result.data["status"] == "error"
