"""
Pick up invoices that vendors upload to a Drive folder.

Some vendors do not email invoices; they drop them in a folder shared with us.
One folder per vendor, which is worth more than it looks: the folder says who
sent the invoice, so the vendor is known before extraction reads a single word.
That is stronger evidence than a name on the page, and it is exactly what was
missing when an INNOVATECTURE rental was booked against "Royal Lotus View".

Everything after the download is the shared pipeline - the same conversion,
the same Unstract call, the same duplicate keys. An invoice that arrives by
both email and upload is recognised as one, because the backend matches on the
file hash and on vendor + number + amount + date, neither of which cares how
it got here.

Configured in .env as JSON:

    DRIVE_INBOX_FOLDERS=[
      {"folder_id": "1AsOEMNQoGHvfA0f7biWbiMGmCg48BBOI", "vendor": "Sumo Technologies"}
    ]

Needs the drive.readonly scope. The default drive.file scope only reaches files
THIS app created, so a vendor's upload is invisible under it - see app/oauth.py.
"""
import json
import logging
import os
from io import BytesIO

import requests
from googleapiclient.http import MediaIoBaseDownload

from app.convert import is_supported
from app.oauth import get_drive_service
from app.pipeline import (
    BACKEND_URL, HTTP_TIMEOUT_SECONDS, DocumentSource, prepare,
    process_document, report_received,
)

logger = logging.getLogger(__name__)

# Google Docs and the like have no bytes to download; only real uploads matter.
LISTABLE = "mimeType != 'application/vnd.google-apps.folder' and trashed = false"


def configured_folders():
    """[{folder_id, vendor}] from the environment, or [] when none are set."""
    raw = (os.getenv("DRIVE_INBOX_FOLDERS") or "").strip()
    if not raw:
        return []
    try:
        folders = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("DRIVE_INBOX_FOLDERS is not valid JSON, ignoring it: %s", e)
        return []

    out = []
    for entry in folders if isinstance(folders, list) else []:
        if isinstance(entry, str):
            out.append({"folder_id": entry, "vendor": None})
        elif isinstance(entry, dict) and entry.get("folder_id"):
            out.append({"folder_id": entry["folder_id"], "vendor": entry.get("vendor")})
    return out


def already_handled(file_ids):
    """
    Which of these the backend has seen before, whatever the outcome.

    Asked BEFORE downloading or parsing: re-sending a file the backend would
    only reject as a duplicate still costs an Unstract call and still inflates
    the received count. The backend is the one source of truth here - a local
    list would diverge the moment the container is replaced.

    On error, treat everything as unseen. Processing a file twice is caught
    downstream by the duplicate check; skipping one silently is not caught by
    anything.
    """
    if not file_ids:
        return set()
    try:
        response = requests.get(
            f"{BACKEND_URL}/verification/drive-files/seen",
            params={"ids": ",".join(file_ids)},
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return set(response.json().get("seen") or [])
    except Exception as e:
        logger.error("Could not ask the backend which Drive files are known: %s", e)
        return set()


def download(drive_service, file_id: str) -> bytes:
    buffer = BytesIO()
    downloader = MediaIoBaseDownload(buffer, drive_service.files().get_media(fileId=file_id))
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buffer.getvalue()


def poll_folder(drive_service, folder_id: str, vendor: str = None):
    try:
        listing = drive_service.files().list(
            q=f"'{folder_id}' in parents and {LISTABLE}",
            fields="files(id, name, mimeType, size, createdTime)",
            pageSize=100,
            # A folder shared by a vendor lives on their drive, not ours.
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
    except Exception as e:
        logger.error("Could not list Drive folder %s: %s", folder_id, e)
        return

    files = listing.get("files", [])
    if not files:
        return

    readable = [f for f in files if is_supported(f.get("name", ""))]
    ignored = [f["name"] for f in files if f not in readable]
    if ignored:
        logger.warning(
            "Drive folder %s holds files this service cannot read: %s",
            folder_id, ", ".join(ignored[:10]),
        )

    seen = already_handled([f["id"] for f in readable])
    new_files = [f for f in readable if f["id"] not in seen]
    if not new_files:
        return

    logger.info("Drive folder %s: %d new file(s)%s",
                folder_id, len(new_files), f" for {vendor}" if vendor else "")

    prepared = []
    for item in new_files:
        try:
            data = download(drive_service, item["id"])
        except Exception as e:
            logger.error("Could not download %s (%s): %s", item["name"], item["id"], e)
            continue

        ready = prepare(item["name"], data)
        if ready is None:
            continue
        prepared.append((item, ready))

    # Counted once the files are in hand, so a download that failed is not
    # reported as received.
    report_received(len(prepared))

    for item, (filename, pdf_data, source_sha256) in prepared:
        source = DocumentSource(
            label=f"drive:{folder_id}",
            drive_file_id=item["id"],
            vendor_hint=vendor,
            # It is already in Drive - this IS the Drive copy. Uploading it
            # again would leave two files and two ids for one invoice.
            already_in_drive=True,
        )
        if not process_document(drive_service, filename, pdf_data, source_sha256, source):
            # Left unmarked on purpose, so the next poll tries again. The
            # backend only records a file id once it has actually handled it.
            logger.error("Leaving %s for the next poll: it did not get through",
                         item["name"])


def poll_drive_for_invoices():
    folders = configured_folders()
    if not folders:
        return

    try:
        drive_service = get_drive_service()
    except Exception as e:
        logger.error("Failed to initialise the Drive service: %s", e)
        return

    for folder in folders:
        try:
            poll_folder(drive_service, folder["folder_id"], folder.get("vendor"))
        except Exception as e:
            # One bad folder must not stop the others.
            logger.error("Failed polling Drive folder %s: %s", folder["folder_id"], e)
