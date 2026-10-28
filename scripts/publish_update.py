"""Package committed app code and publish it to the shared OneDrive folder."""

from __future__ import annotations

import hashlib
import base64
import json
import os
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

GRAPH = "https://graph.microsoft.com/v1.0"


def graph_request(token: str, method: str, url: str, data: bytes | None = None) -> dict:
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Microsoft Graph returned HTTP {error.code}: {detail}") from error
    return json.loads(body) if body else {}


def upload_file(token: str, user: str, folder: str, name: str, content: bytes) -> dict:
    user_path = urllib.parse.quote(user, safe="")
    folder_path = "/".join(urllib.parse.quote(part, safe="") for part in folder.split("/"))
    file_name = urllib.parse.quote(name, safe="")
    url = f"{GRAPH}/users/{user_path}/drive/root:/{folder_path}/{file_name}:/content"
    request = urllib.request.Request(
        url,
        data=content,
        method="PUT",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OneDrive upload failed with HTTP {error.code}: {detail}") from error


def token_roles(token: str) -> list[str]:
    """Read role names only; never log or return the bearer token itself."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return claims.get("roles", [])
    except (IndexError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("Could not inspect the Microsoft Graph token claims.") from error


def verify_target_folder(token: str, user: str, folder: str) -> None:
    user_path = urllib.parse.quote(user, safe="")
    folder_path = "/".join(urllib.parse.quote(part, safe="") for part in folder.split("/"))
    graph_request(token, "GET", f"{GRAPH}/users/{user_path}/drive/root:/{folder_path}")


def anyone_view_link(token: str, user: str, item_id: str) -> str:
    user_path = urllib.parse.quote(user, safe="")
    item_path = urllib.parse.quote(item_id, safe="")
    permission = graph_request(
        token,
        "POST",
        f"{GRAPH}/users/{user_path}/drive/items/{item_path}/createLink",
        json.dumps({"type": "view", "scope": "anonymous"}).encode("utf-8"),
    )
    try:
        return permission["link"]["webUrl"]
    except (KeyError, TypeError) as error:
        raise RuntimeError("Microsoft Graph did not return a public view link for the update file.") from error


def main() -> None:
    user = os.environ["QB_ONEDRIVE_USER"]
    folder = os.environ["QB_ONEDRIVE_FOLDER"]
    token_result = subprocess.run(
        ["az", "account", "get-access-token", "--resource", "https://graph.microsoft.com/", "--query", "accessToken", "--output", "tsv"],
        check=True,
        capture_output=True,
        text=True,
    )
    token = token_result.stdout.strip()
    if not token:
        raise RuntimeError("Azure CLI returned an empty Microsoft Graph access token.")
    roles = token_roles(token)
    if "Files.ReadWrite.All" not in roles:
        role_list = ", ".join(roles) if roles else "none"
        raise RuntimeError(
            "Microsoft Graph token is missing the Files.ReadWrite.All application role "
            f"(token roles: {role_list}). In Entra, add Microsoft Graph > Application permissions > "
            "Files.ReadWrite.All to the app identified by AZURE_CLIENT_ID, then grant admin consent."
        )
    try:
        verify_target_folder(token, user, folder)
    except RuntimeError as error:
        raise RuntimeError(
            f"Graph has the upload role but cannot open OneDrive folder '{folder}' for '{user}'. "
            "Confirm the folder is in that account's OneDrive and the path is correct. "
            f"Graph detail: {error}"
        ) from error

    revision = subprocess.run(
        ["git", "rev-parse", "--short=12", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="qb-update-") as temp_dir:
        archive = Path(temp_dir) / "latest.zip"
        subprocess.run(
            ["git", "archive", "--format=zip", f"--output={archive}", "HEAD"], check=True
        )
        package = archive.read_bytes()

    package_item = upload_file(token, user, folder, "latest.zip", package)
    package_url = anyone_view_link(token, user, package_item["id"])
    manifest = {
        "version": revision,
        "file": "latest.zip",
        "package_url": package_url,
        "sha256": hashlib.sha256(package).hexdigest().upper(),
    }
    manifest_bytes = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    manifest_item = upload_file(token, user, folder, "release.json", manifest_bytes)
    manifest_url = anyone_view_link(token, user, manifest_item["id"])

    print(f"Published app revision {revision} to the shared OneDrive update folder.")
    print(f"Update manifest link for the one-time client setup: {manifest_url}")


if __name__ == "__main__":
    main()
