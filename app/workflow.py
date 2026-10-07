import os
import base64
import hashlib
import requests
import logging
from io import BytesIO
from googleapiclient.http import MediaIoBaseUpload
from app.oauth import get_gmail_service, get_drive_service
from app.parsers import parse_unstract_json
from app.drive_folders import move_file_to_vendor_folder
from app.convert import (
    ConversionError, convert_to_pdf, is_supported, needs_conversion,
    pdf_filename_for,
)

logger = logging.getLogger(__name__)

BACKEND_URL = os.getenv("BACKEND_URL", "http://192.168.200.11:8000")
UNSTRACT_URL = os.getenv("UNSTRACT_URL")
UNSTRACT_API_KEY = os.getenv("UNSTRACT_API_KEY")
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
GMAIL_LABEL_ID = os.getenv("GMAIL_LABEL_ID")

# Applied to emails that carried an attachment this service cannot read. The
# invoice still is not processed, but somebody can see that it arrived - which
# is the whole point. Optional: without it the skip is only logged.
GMAIL_NEEDS_ATTENTION_LABEL_ID = os.getenv("GMAIL_NEEDS_ATTENTION_LABEL_ID")

# Every outbound call needs a deadline. A request without one waits forever,
# and a call that never returns logs nothing at all - the file just disappears
# between one log line and the next, which is how an invoice went missing with
# no error anywhere. Unstract is given slightly longer than its own 300s limit
# so its answer wins whenever it has one.
UNSTRACT_TIMEOUT_SECONDS = int(os.getenv("UNSTRACT_TIMEOUT_SECONDS", "330"))
HTTP_TIMEOUT_SECONDS = int(os.getenv("HTTP_TIMEOUT_SECONDS", "30"))

def poll_gmail_for_invoices():
    try:
        gmail_service = get_gmail_service()
        drive_service = get_drive_service()
    except Exception as e:
        logger.error(f"Failed to initialize Google API services: {e}")
        return
        
    # Deliberately not filtered to PDFs any more. Narrowing the search here is
    # what made a Word invoice vanish without trace - Gmail simply never
    # returned the message, so nothing could report it. Everything with an
    # attachment is fetched, and anything unusable is labelled below.
    query = "has:attachment is:unread"
    
    try:
        results = gmail_service.users().messages().list(userId='me', q=query).execute()
        messages = results.get('messages', [])
    except Exception as e:
        logger.error(f"Failed to fetch emails: {e}")
        return
    
    if not messages:
        logger.debug("No new unread invoice emails found.")
        return

    for msg in messages:
        try:
            process_message(msg['id'], gmail_service, drive_service)
        except Exception as e:
            logger.error(f"Failed to process message {msg['id']}: {e}")

def _claim_message(gmail_service, msg_id: str):
    """
    Mark a message read and labelled before any work is done on it.

    The Gmail query selects unread messages, so this is what stops a second
    poll picking up an email the first is still working through. It has to
    happen before the slow work, not after, or the window stays open for as
    long as conversion and extraction take.
    """
    try:
        gmail_service.users().messages().modify(
            userId='me', id=msg_id,
            body={"addLabelIds": [GMAIL_LABEL_ID], "removeLabelIds": ["UNREAD"]},
        ).execute()
        logger.info(f"Claimed message {msg_id} - marked read and labelled")
    except Exception as e:
        # The message stays unread and will be picked up again. That is the old
        # behaviour, duplicate work and all, but it is better than dropping an
        # invoice because one API call failed.
        logger.error(f"Could not claim message {msg_id}, it may be processed twice: {e}")


def _flag_for_attention(gmail_service, msg_id: str, filenames):
    """
    Mark an email whose attachments this service could not read.

    The poller selects on is:unread, so anything left unread is picked up again
    on the next pass - re-fetched, re-warned, every sixty seconds, forever. The
    label is therefore what makes the message visible, and the message is marked
    read so it stops coming back.

    Without a label configured there is nothing to make it visible, so the
    message is left unread instead. That repeats the warning every minute, which
    is noisy on purpose: an invoice nobody can see is worse than a loud log.
    """
    if not GMAIL_NEEDS_ATTENTION_LABEL_ID:
        logger.warning(
            "Message %s carried attachments this service cannot read: %s. "
            "Set GMAIL_NEEDS_ATTENTION_LABEL_ID to label these and stop this "
            "warning repeating every poll.",
            msg_id, ", ".join(filenames),
        )
        return

    try:
        gmail_service.users().messages().modify(
            userId='me', id=msg_id,
            body={
                "addLabelIds": [GMAIL_NEEDS_ATTENTION_LABEL_ID],
                "removeLabelIds": ["UNREAD"],
            },
        ).execute()
        logger.warning(
            "Message %s carried attachments this service cannot read: %s. "
            "Labelled for attention.",
            msg_id, ", ".join(filenames),
        )
    except Exception as e:
        # Left unread, so it will be retried and re-reported next poll rather
        # than disappearing because one API call failed.
        logger.error(f"Could not label message {msg_id} for attention: {e}")


def process_message(msg_id: str, gmail_service, drive_service):
    message = gmail_service.users().messages().get(userId='me', id=msg_id).execute()
    
    parts = message.get('payload', {}).get('parts', [])
    if not parts:
        # Check recursive payload parts if needed, but for now simple structure
        pass
        
    candidates = []      # (filename, bytes) this service can read
    unreadable = []      # filenames it cannot, kept so the skip can be reported

    def extract_attachments(part_list):
        for part in part_list:
            filename = part.get('filename')
            if filename:
                if not is_supported(filename):
                    # Images, spreadsheets, signatures, inline logos. Recorded
                    # rather than ignored so an invoice in an unexpected format
                    # surfaces instead of disappearing.
                    unreadable.append(filename)
                    continue
                attachment_id = part['body'].get('attachmentId')
                if attachment_id:
                    attachment = gmail_service.users().messages().attachments().get(
                        userId='me', messageId=msg_id, id=attachment_id).execute()
                    data = base64.urlsafe_b64decode(attachment['data'])
                    candidates.append((filename, data))
            elif part.get('parts'):
                extract_attachments(part['parts'])

    # Recursively find parts if multiparts contain parts
    if 'parts' in message.get('payload', {}):
        extract_attachments(message['payload']['parts'])

    # Convert Word documents here, at the edge, so everything below this point
    # deals only in PDFs. The ORIGINAL bytes are kept alongside: duplicate
    # detection hashes those, never the conversion output, because LibreOffice
    # embeds a timestamp and so produces a different PDF every run. Hashing the
    # output would mean the same document never matched itself.
    pdf_attachments = []
    for filename, data in candidates:
        if not needs_conversion(filename):
            pdf_attachments.append((filename, data, hashlib.sha256(data).hexdigest()))
            continue
        try:
            pdf_attachments.append((
                pdf_filename_for(filename),
                convert_to_pdf(filename, data),
                hashlib.sha256(data).hexdigest(),
            ))
        except ConversionError as e:
            logger.error(f"Could not convert {filename} in message {msg_id}: {e}")
            unreadable.append(filename)

    if not pdf_attachments:
        logger.info(f"No readable invoice attachments in message {msg_id}")
        if unreadable:
            _flag_for_attention(gmail_service, msg_id, unreadable)
        return

    if unreadable:
        # Some attachments worked and some did not. The message still gets
        # labelled, because the ones that failed may themselves be invoices.
        _flag_for_attention(gmail_service, msg_id, unreadable)

    logger.info(f"Found {len(pdf_attachments)} invoice file(s) in message {msg_id}")

    # Claim the message BEFORE doing any work on it.
    #
    # This used to happen at the very end. The poller selects on is:unread, so
    # a message stayed selectable for as long as it took to process - and
    # anything slow or stuck meant the next run picked up the same email and
    # started again. That is exactly what happened to Inv_sep_2026.docx: two
    # conversions, two Drive uploads, two Unstract calls, one record, and a
    # received count of two for a single invoice.
    #
    # Claiming first means a crash loses the email instead of looping on it.
    # That is the better failure: it is caught below and labelled for a human,
    # whereas silent reprocessing corrupts the counts and bills Unstract twice
    # while nobody notices.
    _claim_message(gmail_service, msg_id)

    # 3. Call the Invoice_counter backend
    metrics_payload = {
        "metrics_type": "invoice_email_metrics",
        "total_invoices_received": len(pdf_attachments)
    }
    try:
        requests.post(f"{BACKEND_URL}/verification/email_metrics",
                      json=metrics_payload, timeout=HTTP_TIMEOUT_SECONDS)
    except Exception as e:
        logger.error(f"Failed to post metrics: {e}")

    failed = []

    for filename, pdf_data, source_sha256 in pdf_attachments:
        parsed_data = None

        # 4. Upload file to Drive immediately, into the root folder.
        # The vendor is not known until Unstract has parsed the PDF, so the file
        # is banked here first and re-parented into the vendor's folder in step 9.
        # Uploading first means a parsing failure can never lose the document.
        drive_file_id = None
        try:
            file_metadata = {
                'name': filename,
                'parents': [GOOGLE_DRIVE_FOLDER_ID]
            }
            media = MediaIoBaseUpload(BytesIO(pdf_data), mimetype='application/pdf', resumable=True)
            created = drive_service.files().create(
                body=file_metadata, media_body=media, fields='id'
            ).execute()
            drive_file_id = created.get('id')
            logger.info(f"Uploaded {filename} to Google Drive ({drive_file_id})")
        except Exception as e:
            logger.error(f"Failed to upload {filename} to drive: {e}")

        # 6. HTTP Request to Unstract
        unstract_headers = {
            "Authorization": f"Bearer {UNSTRACT_API_KEY}"
        }
        unstract_data = {
            "timeout": "300",
            "include_metadata": "False",
            "include_metrics": "False"
        }
        unstract_files = {
            "files": (filename, pdf_data, "application/pdf")
        }

        try:
            logger.info(f"Sending {filename} to Unstract for parsing")
            unstract_resp = requests.post(
                UNSTRACT_URL,
                headers=unstract_headers,
                data=unstract_data,
                files=unstract_files,
                # Without this, requests waits forever. A hung call logged
                # nothing at all - no success, no failure - so the file simply
                # disappeared between "Sending to Unstract" and the next line
                # of the log. Slightly longer than Unstract's own 300s limit so
                # its answer wins when it has one.
                timeout=UNSTRACT_TIMEOUT_SECONDS,
            )
            if unstract_resp.status_code == 200:
                raw_json = unstract_resp.json()
                # 7 & 8 Parse JSON and send to backend
                parsed_data = parse_unstract_json(raw_json)
                parsed_data["base64_pdf"] = base64.b64encode(pdf_data).decode('utf-8')
                parsed_data["pdf_filename"] = filename
                # Durable link between the invoice record and the Drive file.
                # Filenames collide and get renamed; this id does not.
                parsed_data["drive_file_id"] = drive_file_id
                # The Gmail message id lets the backend recognise an email it has
                # already handled - the last line of defence if claiming the
                # message failed and it gets picked up a second time.
                parsed_data["gmail_message_id"] = msg_id
                # Hash of the file as the vendor sent it. For a PDF this is the
                # same thing the backend would compute; for a converted Word
                # document it is the only stable fingerprint, since the PDF
                # differs on every conversion.
                parsed_data["source_sha256"] = source_sha256
                requests.post(f"{BACKEND_URL}/verification/invoice",
                              json=parsed_data, timeout=HTTP_TIMEOUT_SECONDS)
                logger.info(f"Successfully processed {filename} and sent to Verification")
            else:
                logger.error(f"Unstract failed for {filename}: {unstract_resp.status_code} - {unstract_resp.text}")
                failed.append(filename)
        except Exception as e:
            logger.error(f"Error calling Unstract or sending to Verification for {filename}: {e}")
            failed.append(filename)

        # 9. Now that the vendor is known, move the file into its folder.
        # Skipped entirely if the upload failed or parsing produced no vendor -
        # in that case the file simply stays in the root folder.
        if drive_file_id and parsed_data:
            try:
                move_file_to_vendor_folder(
                    drive_service,
                    drive_file_id,
                    gstin=parsed_data.get("vendor_gstin"),
                    vendor_name=parsed_data.get("vendor_name"),
                    vendor_id=parsed_data.get("vendor_id"),
                )
            except Exception as e:
                logger.error(f"Failed to file {filename} under its vendor folder: {e}")

    # The message was claimed before the loop, so there is nothing to mark here.
    # What is left is to say so when some of it did not work: the message will
    # NOT be retried, which is the point, so a failure that nobody is told about
    # is a lost invoice.
    if failed:
        _flag_for_attention(gmail_service, msg_id, failed)
