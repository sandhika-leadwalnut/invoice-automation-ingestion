import os
import base64
import logging

from app.oauth import get_gmail_service, get_drive_service
from app.convert import is_supported
# Conversion, Drive filing, Unstract and the duplicate keys all live in the
# pipeline now, shared with the Drive inbox. This module's only job is Gmail:
# find the attachments, claim the message, hand each file over.
from app.pipeline import (
    DocumentSource, prepare, process_document, report_received,
)

logger = logging.getLogger(__name__)

GMAIL_LABEL_ID = os.getenv("GMAIL_LABEL_ID")

# Applied to emails that carried an attachment this service cannot read. The
# invoice still is not processed, but somebody can see that it arrived - which
# is the whole point. Optional: without it the skip is only logged.
GMAIL_NEEDS_ATTENTION_LABEL_ID = os.getenv("GMAIL_NEEDS_ATTENTION_LABEL_ID")

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
        ready = prepare(filename, data)
        if ready is None:
            unreadable.append(filename)
            continue
        pdf_attachments.append(ready)

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

    report_received(len(pdf_attachments))

    failed = []

    for filename, pdf_data, source_sha256 in pdf_attachments:
        # One shared pipeline for Gmail and the Drive inbox. Everything from
        # here - Drive filing, Unstract, the duplicate keys - is identical
        # whatever brought the file in, and keeping two copies of it is how
        # the n8n and service paths ended up storing different shapes.
        if not process_document(
            drive_service, filename, pdf_data, source_sha256,
            DocumentSource(label=f"gmail:{msg_id}", gmail_message_id=msg_id),
        ):
            failed.append(filename)

    # The message was claimed before the loop, so there is nothing to mark
    # here. What is left is to say so when some of it did not work: the message
    # will NOT be retried, which is the point, so a failure nobody is told
    # about is a lost invoice.
    if failed:
        _flag_for_attention(gmail_service, msg_id, failed)
    return
