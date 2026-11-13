"""
What happens to one invoice file, whatever brought it in.

Gmail and the Drive inbox both end here. That is deliberate: the n8n workflow
and the Python service each had their own version of this and stored
`items_table` differently, which produced records nobody could account for.
Two ingestion paths that drift are worse than one awkward shared function.

A source only has to say where the file came from. Everything after that -
conversion, Drive filing, Unstract, the duplicate keys the backend needs - is
identical.
"""
import base64
import hashlib
import logging
import os
from io import BytesIO
from typing import Optional

import requests
from googleapiclient.http import MediaIoBaseUpload

from app.convert import ConversionError, convert_to_pdf, needs_conversion, pdf_filename_for
from app.drive_folders import move_file_to_vendor_folder
from app.parsers import parse_unstract_json

logger = logging.getLogger(__name__)

BACKEND_URL = os.getenv("BACKEND_URL", "http://192.168.200.11:8000")
UNSTRACT_URL = os.getenv("UNSTRACT_URL")
UNSTRACT_API_KEY = os.getenv("UNSTRACT_API_KEY")
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID")

UNSTRACT_TIMEOUT_SECONDS = int(os.getenv("UNSTRACT_TIMEOUT_SECONDS", "330"))
HTTP_TIMEOUT_SECONDS = int(os.getenv("HTTP_TIMEOUT_SECONDS", "30"))


class DocumentSource:
    """
    Where a file came from, and what the backend should know about it.

    gmail_message_id and drive_file_id are both duplicate keys on the backend,
    so the same invoice arriving by email and by upload is recognised as one.
    vendor_hint comes from a per-vendor Drive folder: the folder says who sent
    it, which is better evidence than a name read off the page.
    """

    def __init__(self, label: str, gmail_message_id: Optional[str] = None,
                 drive_file_id: Optional[str] = None, vendor_hint: Optional[str] = None,
                 already_in_drive: bool = False):
        self.label = label
        self.gmail_message_id = gmail_message_id
        self.drive_file_id = drive_file_id
        self.vendor_hint = vendor_hint
        # A file picked up FROM Drive is already filed. Uploading it again
        # would leave two copies and a second id for the same invoice.
        self.already_in_drive = already_in_drive


def prepare(filename: str, data: bytes):
    """
    (pdf_filename, pdf_bytes, source_sha256) or None if it cannot be converted.

    The hash is of the ORIGINAL bytes, never the converted PDF: LibreOffice
    stamps a creation time into every file it writes, so the same .docx hashes
    differently each run and would never match itself.
    """
    source_sha256 = hashlib.sha256(data).hexdigest()

    if not needs_conversion(filename):
        return filename, data, source_sha256

    try:
        return pdf_filename_for(filename), convert_to_pdf(filename, data), source_sha256
    except ConversionError as e:
        logger.error("Could not convert %s: %s", filename, e)
        return None


def report_received(count: int):
    """Tell the backend how many invoice files arrived."""
    if count <= 0:
        return
    try:
        requests.post(
            f"{BACKEND_URL}/verification/email_metrics",
            json={"metrics_type": "invoice_email_metrics", "total_invoices_received": count},
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except Exception as e:
        logger.error("Failed to post metrics: %s", e)


def file_into_drive(drive_service, filename: str, pdf_data: bytes) -> Optional[str]:
    """
    Bank the file in Drive before parsing, so a parsing failure cannot lose it.
    Returns the Drive file id, or None if the upload failed.
    """
    try:
        created = drive_service.files().create(
            body={"name": filename, "parents": [GOOGLE_DRIVE_FOLDER_ID]},
            media_body=MediaIoBaseUpload(BytesIO(pdf_data),
                                         mimetype="application/pdf", resumable=True),
            fields="id",
        ).execute()
        logger.info("Uploaded %s to Google Drive (%s)", filename, created.get("id"))
        return created.get("id")
    except Exception as e:
        logger.error("Failed to upload %s to drive: %s", filename, e)
        return None


def process_document(drive_service, filename: str, pdf_data: bytes,
                     source_sha256: str, source: DocumentSource) -> bool:
    """
    Parse one PDF and hand it to the backend. True when it got there.

    False means the file needs a human: Unstract refused it, timed out, or the
    backend could not be reached. The caller decides what to do about that -
    label the email, leave the Drive file unmarked - because only it knows how
    to make the failure visible.
    """
    drive_file_id = source.drive_file_id
    if not source.already_in_drive:
        drive_file_id = file_into_drive(drive_service, filename, pdf_data)

    parsed_data = None
    try:
        logger.info("Sending %s to Unstract for parsing", filename)
        response = requests.post(
            UNSTRACT_URL,
            headers={"Authorization": f"Bearer {UNSTRACT_API_KEY}"},
            data={"timeout": "300", "include_metadata": "False", "include_metrics": "False"},
            files={"files": (filename, pdf_data, "application/pdf")},
            # Without this, requests waits forever. A hung call logged nothing
            # at all, so a file simply disappeared between "Sending to Unstract"
            # and the next line of the log.
            timeout=UNSTRACT_TIMEOUT_SECONDS,
        )
    except Exception as e:
        logger.error("Error calling Unstract for %s: %s", filename, e)
        return False

    if response.status_code != 200:
        logger.error("Unstract failed for %s: %s - %s",
                     filename, response.status_code, response.text[:300])
        return False

    try:
        parsed_data = parse_unstract_json(response.json())
        parsed_data["base64_pdf"] = base64.b64encode(pdf_data).decode("utf-8")
        parsed_data["pdf_filename"] = filename
        # Durable link between the invoice record and the Drive file. Filenames
        # collide and get renamed; this id does not.
        parsed_data["drive_file_id"] = drive_file_id
        # Duplicate keys. Either one is enough for the backend to recognise a
        # file it has already handled, whichever way it arrived.
        parsed_data["gmail_message_id"] = source.gmail_message_id
        parsed_data["source_sha256"] = source_sha256
        # The folder a vendor uploads to identifies them more reliably than a
        # name read off the page. The backend prefers its own vendor match and
        # falls back to this.
        if source.vendor_hint:
            parsed_data["vendor_hint"] = source.vendor_hint

        requests.post(f"{BACKEND_URL}/verification/invoice",
                      json=parsed_data, timeout=HTTP_TIMEOUT_SECONDS)
        logger.info("Successfully processed %s (%s) and sent to Verification",
                    filename, source.label)
    except Exception as e:
        logger.error("Error sending %s to Verification: %s", filename, e)
        return False

    # Now that the vendor is known, move the file into its folder. Skipped when
    # the upload failed or parsing produced no vendor: the file then simply
    # stays where it is.
    if drive_file_id and parsed_data and not source.already_in_drive:
        try:
            move_file_to_vendor_folder(
                drive_service, drive_file_id,
                gstin=parsed_data.get("vendor_gstin"),
                vendor_name=parsed_data.get("vendor_name"),
                vendor_id=parsed_data.get("vendor_id"),
            )
        except Exception as e:
            logger.error("Failed to file %s under its vendor folder: %s", filename, e)

    return True
