#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["claude-artifact-cli==0.3.0", "fastmcp==4.1.1"]
# ///
"""Read artifacts across access-only account snapshots using upstream FrameClient.

The launcher owns the manifest and credential snapshots. This adapter owns only
account dispatch; claude-artifact-cli owns artifact metadata and file transport.
"""

import argparse
import base64
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

from claude_artifact_cli.api import ApiError, FrameClient, clean_path, slug_from
from fastmcp import FastMCP


class SnapshotError(ValueError):
    """A static, credential-free account availability message."""


MAX_FILE_BYTES = 2 * 1024 * 1024
LIST_FIELDS = {
    "slug",
    "title",
    "description",
    "icon",
    "icon_key",
    "source_surface",
    "live",
    "rel",
    "updatedAt",
    "created_at",
    "audience",
    "soft_deleted",
}
META_FIELDS = {
    "kind",
    "ver",
    "files",
    "title",
    "favicon",
    "icon",
    "perm",
    "author",
    "created_at",
    "updated_at",
    "live",
    "history",
    "typeInstance",
}
SECRET_KEYS = {"sharekey", "authorization", "cookie", "password", "secret"}


def safe_metadata(value):
    """Exclude capability credentials and signed URLs from tool-visible metadata."""
    if isinstance(value, dict):
        return {
            key: safe_metadata(item)
            for key, item in value.items()
            if "token" not in key.lower()
            and key.lower().replace("_", "").replace("-", "") not in SECRET_KEYS
        }
    if isinstance(value, list):
        return [safe_metadata(item) for item in value]
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        parsed = urlsplit(value)
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            return "[signed or private URL omitted]"
    return value


def error_message(exc):
    # Upstream exceptions can contain a response body, URL or path. Never echo it.
    if isinstance(exc, SnapshotError):
        return str(exc)
    if isinstance(exc, ApiError):
        status = exc.status
        if status in (401, 403):
            return "Account credentials or artifact permissions do not allow this read."
        if status == 404:
            return "Artifact or file is unavailable to this account."
        if status == 429:
            return "Artifact service rate limited this account; retry later."
        return "Artifact service could not complete this read."
    return "Account snapshot is unavailable or invalid."


class ArtifactAccounts:
    def __init__(self, manifest, client_factory=FrameClient):
        self.manifest = manifest
        self.client_factory = client_factory
        self.accounts = manifest["accounts"]
        self.main = manifest["main"]
        if not isinstance(self.accounts, dict) or self.main not in self.accounts:
            raise ValueError("manifest requires a main account in accounts")

    def client(self, account):
        entry = self.accounts[account]
        if entry.get("error"):
            raise SnapshotError(
                "Account is unavailable; renew its source login and relaunch."
            )
        directory = Path(entry["secure_storage_dir"])
        if not directory.is_absolute():
            raise ValueError("credential snapshot path must be absolute")
        credentials = json.loads((directory / ".credentials.json").read_text())
        oauth = credentials["claudeAiOauth"]
        token = oauth.get("accessToken")
        expiry = oauth.get("expiresAt")
        if not isinstance(token, str) or not token:
            raise ValueError("snapshot needs an access token")
        if not isinstance(expiry, (float, int)) or expiry <= time.time() * 1000:
            raise SnapshotError(
                "Account access token expired; renew its source login and relaunch."
            )
        if "user:profile" not in oauth.get("scopes", []):
            raise ValueError("snapshot needs a full account login")
        return self.client_factory(token, timeout=30)

    def list_artifacts(self, account: str | None = None, limit: int = 200) -> dict:
        """List all account sources, or one source; each failure stays explicit."""
        if not isinstance(limit, int) or not 1 <= limit <= 500:
            return {"status": "error", "error": "limit must be between 1 and 500"}
        names = list(self.accounts) if account is None else [account]
        results = []
        for name in names:
            try:
                rows = self.client(name).list_frames(limit=limit)
                artifacts = []
                for row in rows:
                    item = safe_metadata(
                        {k: v for k, v in row.items() if k in LIST_FIELDS}
                    )
                    item["account"] = name
                    artifacts.append(item)
                results.append(
                    {"account": name, "status": "ok", "artifacts": artifacts}
                )
            except (ApiError, OSError, ValueError, KeyError, TypeError) as exc:
                results.append(
                    {"account": name, "status": "error", "error": error_message(exc)}
                )
        return {"accounts": results}

    def read_artifact(
        self, artifact: str, account: str | None = None, path: str | None = None
    ) -> dict:
        """Read metadata or a published file, using main unless account is explicit."""
        name = self.main if account is None else account
        result = {"account": name}
        try:
            slug = slug_from(artifact)
            result["artifact"] = slug
            if path is not None and clean_path(path) is None:
                return {
                    **result,
                    "status": "error",
                    "error": "path must be a clean relative artifact file path",
                }
            client = self.client(name)
            if path is None:
                meta = client.read(slug)
                return {
                    **result,
                    "status": "ok",
                    "metadata": safe_metadata(
                        {k: v for k, v in meta.items() if k in META_FIELDS}
                    ),
                }
            reader = client.files(slug)
            entry = reader.files.get(path, {})
            size = entry.get("size")
            if isinstance(size, (int, float)) and size > MAX_FILE_BYTES:
                return {
                    **result,
                    "status": "error",
                    "error": "File exceeds the 2 MiB tool response limit.",
                }
            content = reader.fetch(path)
            if len(content) > MAX_FILE_BYTES:
                return {
                    **result,
                    "status": "error",
                    "error": "File exceeds the 2 MiB tool response limit.",
                }
            try:
                data, encoding = content.decode("utf-8"), "utf-8"
            except UnicodeDecodeError:
                data, encoding = base64.b64encode(content).decode("ascii"), "base64"
            return {
                **result,
                "status": "ok",
                "path": path,
                "version": reader.version,
                "encoding": encoding,
                "bytes": len(content),
                "content": data,
            }
        except (ApiError, OSError, ValueError, KeyError, TypeError) as exc:
            return {**result, "status": "error", "error": error_message(exc)}


def create_server(manifest):
    accounts = ArtifactAccounts(manifest)
    server = FastMCP("account-artifacts")
    annotations = {
        "readOnlyHint": True,
        "destructiveHint": False,
        "openWorldHint": True,
    }
    server.tool(accounts.list_artifacts, annotations=annotations)
    server.tool(accounts.read_artifact, annotations=annotations)
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    create_server(manifest).run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
