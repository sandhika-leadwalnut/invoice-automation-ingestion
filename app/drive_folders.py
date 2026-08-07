"""
Vendor-wise Google Drive folder resolution.

Invoices used to land in one flat folder because the Drive upload ran before
Unstract had extracted the vendor. This module resolves the canonical vendor
name (via the backend, which reads invoice_db.vendors) and returns the id of
that vendor's subfolder, creating it on first use.

Folder names come from Mongo rather than the raw OCR text so that
"Xperforce Technologies LLP" and "XPERFORCE TECHNOLOGIES LLP" do not become
two separate folders.
"""

import logging
import os
import threading

import requests

logger = logging.getLogger(__name__)

BACKEND_URL = os.getenv("BACKEND_URL", "http://192.168.200.11:8000")
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID")

# Invoices whose vendor could not be resolved go here rather than being dropped
# into the root, so the root stays clean and unmatched files are easy to find.
UNSORTED_FOLDER_NAME = os.getenv("DRIVE_UNSORTED_FOLDER", "_Unsorted")

FOLDER_MIME = "application/vnd.google-apps.folder"

# name -> folder id. Saves a Drive list() call per invoice.
_folder_cache = {}
# Guards create-if-missing so two invoices for a new vendor arriving together
# cannot create two folders with the same name (Drive permits duplicates).
_folder_lock = threading.Lock()


def _escape(name: str) -> str:
    """Escape a value for a Drive query string literal."""
    return name.replace("\\", "\\\\").replace("'", "\\'")


def resolve_vendor_name(gstin=None, vendor_name=None, vendor_id=None):
    """
    Ask the backend for the canonical vendor name.

    Returns the stored vendor_name, or None if the vendor is not mapped.
    Never raises - a lookup failure must not stop the invoice from being filed.
    """
    params = {k: v for k, v in (
        ("gstin", gstin), ("vendor_name", vendor_name), ("vendor_id", vendor_id)
    ) if v}
    if not params:
        return None

    try:
        r = requests.get(f"{BACKEND_URL}/verification/vendor/lookup",
                         params=params, timeout=10)
        if r.status_code != 200:
            logger.warning(f"Vendor lookup returned {r.status_code} for {params}")
            return None
        data = r.json()
        return data.get("vendor_name") if data.get("matched") else None
    except Exception as e:
        logger.warning(f"Vendor lookup failed for {params}: {e}")
        return None


def sanitize_folder_name(name: str) -> str:
    """
    Drive tolerates most characters, but slashes and newlines make folders
    awkward to reference. Collapse them and trim.
    """
    if not name:
        return ""
    cleaned = str(name).replace("/", "-").replace("\\", "-")
    cleaned = " ".join(cleaned.split())
    return cleaned.strip()[:120]


def get_or_create_folder(drive_service, name: str, parent_id: str) -> str:
    """
    Return the id of the subfolder `name` under `parent_id`, creating it if absent.

    Cached, and guarded by a lock so concurrent invoices for a brand-new vendor
    do not race and create duplicate folders.
    """
    name = sanitize_folder_name(name)
    if not name:
        raise ValueError("folder name is empty after sanitizing")

    cache_key = f"{parent_id}/{name}"
    if cache_key in _folder_cache:
        return _folder_cache[cache_key]

    with _folder_lock:
        # Re-check inside the lock; another thread may have created it.
        if cache_key in _folder_cache:
            return _folder_cache[cache_key]

        query = (
            f"name = '{_escape(name)}' and "
            f"mimeType = '{FOLDER_MIME}' and "
            f"'{parent_id}' in parents and trashed = false"
        )
        resp = drive_service.files().list(
            q=query, fields="files(id, name)", pageSize=10
        ).execute()
        files = resp.get("files", [])

        if files:
            folder_id = files[0]["id"]
            if len(files) > 1:
                logger.warning(
                    f"{len(files)} folders named '{name}' exist under {parent_id}; "
                    f"using {folder_id}. Consider merging them."
                )
        else:
            created = drive_service.files().create(
                body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
                fields="id",
            ).execute()
            folder_id = created["id"]
            logger.info(f"Created Drive folder '{name}' ({folder_id})")

        _folder_cache[cache_key] = folder_id
        return folder_id


def move_file_to_vendor_folder(drive_service, file_id: str,
                               gstin=None, vendor_name=None, vendor_id=None):
    """
    Re-parent an already-uploaded file into its vendor's folder.

    The PDF is uploaded to the root folder before parsing so it can never be
    lost, then moved here once Unstract has told us who the vendor is.

    Returns the destination folder id, or None if the file was left in place.
    A vendor that is not mapped in invoice_db.vendors is left in the root
    rather than being swept into a bucket - unfiled invoices should be visible.
    """
    root = GOOGLE_DRIVE_FOLDER_ID
    if not root:
        raise ValueError("GOOGLE_DRIVE_FOLDER_ID is not set")

    canonical = resolve_vendor_name(gstin=gstin, vendor_name=vendor_name, vendor_id=vendor_id)
    if not canonical:
        logger.info(
            f"Vendor unmapped (gstin={gstin!r}, name={vendor_name!r}); "
            f"leaving file {file_id} in the root folder"
        )
        return None

    folder_id = get_or_create_folder(drive_service, canonical, root)

    # Read current parents so the move is exact rather than assuming root.
    current = drive_service.files().get(fileId=file_id, fields="parents").execute()
    prev_parents = ",".join(current.get("parents", [root]))

    if folder_id in current.get("parents", []):
        logger.debug(f"File {file_id} already in '{canonical}'")
        return folder_id

    drive_service.files().update(
        fileId=file_id,
        addParents=folder_id,
        removeParents=prev_parents,
        fields="id, parents",
    ).execute()
    logger.info(f"Filed {file_id} under '{canonical}' ({folder_id})")
    return folder_id


def get_target_folder(drive_service, gstin=None, vendor_name=None, vendor_id=None) -> str:
    """
    Resolve the folder an invoice PDF should be uploaded into.

    Falls back to the root folder if anything goes wrong, so a Drive or lookup
    problem degrades to today's flat behaviour instead of losing the file.
    """
    root = GOOGLE_DRIVE_FOLDER_ID
    if not root:
        raise ValueError("GOOGLE_DRIVE_FOLDER_ID is not set")

    canonical = resolve_vendor_name(gstin=gstin, vendor_name=vendor_name, vendor_id=vendor_id)
    target_name = canonical or UNSORTED_FOLDER_NAME

    if not canonical:
        logger.info(
            f"Vendor unmapped (gstin={gstin!r}, name={vendor_name!r}); "
            f"filing under {UNSORTED_FOLDER_NAME}"
        )

    try:
        return get_or_create_folder(drive_service, target_name, root)
    except Exception as e:
        logger.error(f"Could not resolve folder '{target_name}': {e}. Using root folder.")
        return root
