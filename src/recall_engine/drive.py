"""gcloud auth (ADC or user login) + Drive API v3: one-way download/upload of .md files."""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import google.auth
from google.auth.exceptions import DefaultCredentialsError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_APPS_PREFIX = "application/vnd.google-apps."
AUTH_HELP = (
    "Google Drive credentials are missing or lack the Drive scope. Login with either:\n"
    "  gcloud auth login --enable-gdrive-access\n"
    "or:\n"
    "  gcloud auth application-default login "
    "--scopes=https://www.googleapis.com/auth/drive,"
    "https://www.googleapis.com/auth/cloud-platform"
)


class DriveError(Exception):
    """Drive sync cannot proceed."""


def get_gcloud_user_credentials() -> Credentials | None:
    """Mint credentials from the gcloud user login (gcloud auth login), if available.

    The token is short-lived and not refreshable, which is enough for one CLI run.
    """
    gcloud = shutil.which("gcloud")
    if gcloud is None:
        return None
    result = subprocess.run(
        [gcloud, "auth", "print-access-token"],
        capture_output=True,
        text=True,
    )
    token = result.stdout.strip()
    if result.returncode != 0 or not token:
        return None
    return Credentials(token=token)


def build_drive_service() -> Any:
    """Return a Drive v3 client from ADC, falling back to the gcloud user login."""
    credentials: Any
    try:
        credentials, _ = google.auth.default(scopes=DRIVE_SCOPES)
    except DefaultCredentialsError as exc:
        credentials = get_gcloud_user_credentials()
        if credentials is None:
            raise DriveError(AUTH_HELP) from exc
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def as_drive_error(exc: HttpError) -> DriveError:
    """Map an HttpError to a DriveError; auth/scope failures include the login fix."""
    detail = str(exc)
    lowered = detail.lower()
    auth_markers = ("insufficient", "scope", "access_denied", "service_disabled")
    if exc.resp.status == 403 and any(marker in lowered for marker in auth_markers):
        return DriveError(f"Drive API returned 403: {detail}\n{AUTH_HELP}")
    return DriveError(f"Drive API error (HTTP {exc.resp.status}): {detail}")


def execute(request: Any) -> Any:
    """Run a Drive request, mapping HttpError to DriveError."""
    try:
        return request.execute()
    except HttpError as exc:
        raise as_drive_error(exc) from exc


def escape_query(value: str) -> str:
    """Escape a literal for a Drive API query string."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def resolve_folder_id(service: Any, folder: str) -> str:
    """Resolve --remote-knowledge-folder to a folder ID; accepts an ID or a name.

    The value is first tried as a folder ID (backward compatible); on a 404 it
    is looked up as a folder name, case-insensitively. Ambiguous names raise.
    """
    try:
        file = service.files().get(fileId=folder, fields="id, mimeType").execute()
        if file.get("mimeType") == FOLDER_MIME:
            return folder
    except HttpError as exc:
        if exc.resp.status != 404:
            raise as_drive_error(exc) from exc

    # 'contains' matches case-insensitively; exact-match client-side.
    response = execute(
        service.files().list(
            q=(
                f"mimeType = '{FOLDER_MIME}' and "
                f"name contains '{escape_query(folder)}' and trashed = false"
            ),
            fields="files(id, name)",
        )
    )
    matches = [
        f for f in response.get("files", []) if f["name"].lower() == folder.lower()
    ]
    if not matches:
        raise DriveError(
            f"Drive folder not found: '{folder}' (checked as both ID and name)"
        )
    if len(matches) > 1:
        candidates = ", ".join(f"{f['name']} ({f['id']})" for f in matches)
        raise DriveError(
            f"Multiple Drive folders named '{folder}': {candidates}. "
            "Pass --remote-knowledge-folder with the folder ID instead."
        )
    return matches[0]["id"]


def sync_download(service: Any, folder_id: str, dest: Path) -> list[str]:
    """Download the Drive folder's .md files into dest; return written filenames.

    Plain .md files are downloaded as-is; native Google Docs are exported as
    plain text to '<name>.md' (the Markdown export escapes literal Markdown
    in the doc, e.g. '#' becomes '\\#'). Other file types are skipped. Local
    files are overwritten by name, except a symlink, which is skipped; duplicate
    names in the folder resolve to the copy with the latest modifiedTime (with a
    warning).
    """
    files: list[dict[str, str]] = []
    page_token: str | None = None
    while True:
        response = execute(
            service.files().list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields="nextPageToken, files(id, name, mimeType, modifiedTime)",
                pageToken=page_token,
            )
        )
        files.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if page_token is None:
            break

    # Pick one source file per target filename; latest modifiedTime wins.
    selected: dict[str, dict[str, str]] = {}
    for file in files:
        mime = file.get("mimeType", "")
        name = file["name"]
        if mime == GOOGLE_DOC_MIME:
            target = name if name.endswith(".md") else f"{name}.md"
        elif name.endswith(".md") and not mime.startswith(GOOGLE_APPS_PREFIX):
            target = name
        else:
            continue
        previous = selected.get(target)
        if previous is not None:
            print(
                f"warning: multiple Drive files map to '{target}'; "
                "keeping the latest modified copy",
                file=sys.stderr,
            )
            if file.get("modifiedTime", "") <= previous.get("modifiedTime", ""):
                continue
        selected[target] = file

    dest.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for target, file in selected.items():
        destination = dest / target
        # Sync does not follow symlinks: writing through one would overwrite a
        # file outside src/ that the repo only mounted for reading. Decided
        # before the fetch, so a skipped file never depends on Drive answering.
        if destination.is_symlink():
            print(
                f"warning: skipping '{target}': it is a symlink; "
                "refusing to overwrite the file it points to",
                file=sys.stderr,
            )
            continue
        if file.get("mimeType") == GOOGLE_DOC_MIME:
            content = execute(
                service.files().export(fileId=file["id"], mimeType="text/plain")
            )
            # The plain-text export starts with a BOM and uses CRLF line endings.
            content = content.removeprefix(b"\xef\xbb\xbf").replace(b"\r\n", b"\n")
        else:
            buffer = io.BytesIO()
            downloader = MediaIoBaseDownload(
                buffer, service.files().get_media(fileId=file["id"])
            )
            done = False
            try:
                while not done:
                    _, done = downloader.next_chunk()
            except HttpError as exc:
                raise as_drive_error(exc) from exc
            content = buffer.getvalue()
        destination.write_bytes(content)
        written.append(target)
    return written


def sync_upload(service: Any, folder_id: str, src: Path) -> list[str]:
    """Upload every .md file in src (non-recursive) to the Drive folder.

    Files are matched by name: an existing non-Google-Apps Drive file is updated
    in place, otherwise a new plain-Markdown file is created (never converted
    to a native Google Doc). A symlink pointing outside src/ is skipped: search
    may serve such a note, but sync must not push external content to Drive.
    Returns the uploaded filenames.
    """
    if not src.is_dir():
        raise DriveError(f"Source directory not found: {src}. Nothing to upload.")
    md_files = sorted(src.glob("*.md"))
    if not md_files:
        raise DriveError(f"No .md files found in {src}. Nothing to upload.")

    uploaded: list[str] = []
    for path in md_files:
        if path.is_symlink() and src not in path.resolve().parents:
            print(
                f"warning: skipping '{path.name}': symlink points outside src/",
                file=sys.stderr,
            )
            continue
        escaped = escape_query(path.name)
        response = execute(
            service.files().list(
                q=(
                    f"name = '{escaped}' and '{folder_id}' in parents "
                    "and trashed = false"
                ),
                fields="files(id, name, mimeType)",
            )
        )
        matches = [
            file
            for file in response.get("files", [])
            if not file.get("mimeType", "").startswith(GOOGLE_APPS_PREFIX)
        ]
        media = MediaIoBaseUpload(io.BytesIO(path.read_bytes()), mimetype="text/markdown")
        if matches:
            execute(service.files().update(fileId=matches[0]["id"], media_body=media))
        else:
            execute(
                service.files().create(
                    body={"name": path.name, "parents": [folder_id]},
                    media_body=media,
                )
            )
        uploaded.append(path.name)
    return uploaded
