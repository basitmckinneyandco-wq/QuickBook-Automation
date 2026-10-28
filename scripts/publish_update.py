"""Package committed app code and publish it to the shared OneDrive folder."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

GRAPH = "https://graph.microsoft.com/v1.0"


class GraphApiError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"Microsoft Graph returned HTTP {status}: {detail}")
        self.status = status


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
        raise GraphApiError(error.code, detail) from error
    return json.loads(body) if body else {}


def token_roles(token: str) -> list[str]:
    """Read role names only; never log or return the bearer token itself."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return claims.get("roles", [])
    except (IndexError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("Could not inspect the Microsoft Graph token claims.") from error


def resolve_target_folder(token: str, user: str, folder: str) -> tuple[str, str, str]:
    """Resolve the OneDrive folder by its item ID, trying library and drive-root paths."""
    candidates = [folder]
    if folder.casefold().startswith("documents/"):
        candidates.append(folder.split("/", 1)[1])
    else:
        candidates.append(f"Documents/{folder}")

    user_path = urllib.parse.quote(user, safe="")
    for candidate in candidates:
        path = "/".join(urllib.parse.quote(part, safe="") for part in candidate.split("/"))
        url = f"{GRAPH}/users/{user_path}/drive/root:/{path}"
        try:
            item = graph_request(token, "GET", url)
        except GraphApiError as error:
            if error.status == 404:
                continue
            raise
        drive_id = item.get("parentReference", {}).get("driveId")
        item_id = item.get("id")
        if drive_id and item_id and "folder" in item:
            return drive_id, item_id, candidate
        raise RuntimeError(f"Found '{candidate}', but Graph did not return a folder ID and drive ID.")

    raise RuntimeError(
        "Graph could not find the update folder using either path "
        f"'{candidates[0]}' or '{candidates[1]}'. Check that the folder is in the "
        f"OneDrive for '{user}' and its name is exactly 'Quickbook App Updates'."
    )


def upload_file(token: str, drive_id: str, folder_id: str, name: str, content: bytes) -> dict:
    drive_path = urllib.parse.quote(drive_id, safe="!")
    folder_path = urllib.parse.quote(folder_id, safe="!")
    file_name = urllib.parse.quote(name, safe="")
    url = f"{GRAPH}/drives/{drive_path}/items/{folder_path}:/{file_name}:/content"
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
        raise GraphApiError(error.code, detail) from error


def anyone_view_link(token: str, drive_id: str, item_id: str) -> str:
    drive_path = urllib.parse.quote(drive_id, safe="!")
    item_path = urllib.parse.quote(item_id, safe="")
    permission = graph_request(
        token,
        "POST",
        f"{GRAPH}/drives/{drive_path}/items/{item_path}/createLink",
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

    drive_id, folder_id, resolved_path = resolve_target_folder(token, user, folder)
    revision = subprocess.run(
        ["git", "rev-parse", "--short=12", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="qb-update-") as temp_dir:
        archive = Path(temp_dir) / "latest.zip"
        subprocess.run(
            ["git", "archive", "--format=zip", f"--output={archive}", "HEAD"], check=True
        )
        package = archive.read_bytes()

    package_item = upload_file(token, drive_id, folder_id, "latest.zip", package)
    package_url = anyone_view_link(token, drive_id, package_item["id"])
    manifest = {
        "version": revision,
        "file": "latest.zip",
        "package_url": package_url,
        "sha256": hashlib.sha256(package).hexdigest().upper(),
    }
    manifest_bytes = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    manifest_item = upload_file(token, drive_id, folder_id, "release.json", manifest_bytes)
    manifest_url = anyone_view_link(token, drive_id, manifest_item["id"])

    print(f"Published app revision {revision} to OneDrive path '{resolved_path}'.")
    print(f"Update manifest link for the one-time client setup: {manifest_url}")


if __name__ == "__main__":
    main()
