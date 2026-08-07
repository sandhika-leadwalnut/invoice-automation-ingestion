import os
import base64
import requests
import logging
from io import BytesIO
from googleapiclient.http import MediaIoBaseUpload
from app.oauth import get_gmail_service, get_drive_service
from app.parsers import parse_unstract_json
from app.drive_folders import move_file_to_vendor_folder

logger = logging.getLogger(__name__)

BACKEND_URL = os.getenv("BACKEND_URL", "http://192.168.200.11:8000")
UNSTRACT_URL = os.getenv("UNSTRACT_URL")
UNSTRACT_API_KEY = os.getenv("UNSTRACT_API_KEY")
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
GMAIL_LABEL_ID = os.getenv("GMAIL_LABEL_ID")

def poll_gmail_for_invoices():
    try:
        gmail_service = get_gmail_service()
        drive_service = get_drive_service()
    except Exception as e:
        logger.error(f"Failed to initialize Google API services: {e}")
        return
        
    query = "has:attachment filename:pdf is:unread"
    
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

def process_message(msg_id: str, gmail_service, drive_service):
    message = gmail_service.users().messages().get(userId='me', id=msg_id).execute()
    
    parts = message.get('payload', {}).get('parts', [])
    if not parts:
        # Check recursive payload parts if needed, but for now simple structure
        pass
        
    pdf_attachments = []
    
    def extract_attachments(part_list):
        for part in part_list:
            if part.get('filename') and part.get('filename').lower().endswith('.pdf'):
                attachment_id = part['body'].get('attachmentId')
                if attachment_id:
                    attachment = gmail_service.users().messages().attachments().get(
                        userId='me', messageId=msg_id, id=attachment_id).execute()
                    data = base64.urlsafe_b64decode(attachment['data'])
                    pdf_attachments.append((part['filename'], data))
            elif part.get('parts'):
                extract_attachments(part['parts'])

    # Recursively find parts if multiparts contain parts
    if 'parts' in message.get('payload', {}):
        extract_attachments(message['payload']['parts'])
    
    if not pdf_attachments:
        logger.info(f"No PDF attachments found in message {msg_id}")
        return
        
    logger.info(f"Found {len(pdf_attachments)} PDF(s) in message {msg_id}")
    
    # 3. Call the Invoice_counter backend
    metrics_payload = {
        "metrics_type": "invoice_email_metrics",
        "total_invoices_received": len(pdf_attachments)
    }
    try:
        requests.post(f"{BACKEND_URL}/verification/email_metrics", json=metrics_payload)
    except Exception as e:
        logger.error(f"Failed to post metrics: {e}")

    for filename, pdf_data in pdf_attachments:
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
                files=unstract_files
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
                requests.post(f"{BACKEND_URL}/verification/invoice", json=parsed_data)
                logger.info(f"Successfully processed {filename} and sent to Verification")
            else:
                logger.error(f"Unstract failed for {filename}: {unstract_resp.status_code} - {unstract_resp.text}")
        except Exception as e:
            logger.error(f"Error calling Unstract or sending to Verification for {filename}: {e}")

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

    # 5. Add Label and mark message as read
    try:
        add_labels = {"addLabelIds": [GMAIL_LABEL_ID], "removeLabelIds": ["UNREAD"]}
        gmail_service.users().messages().modify(userId='me', id=msg_id, body=add_labels).execute()
        logger.info(f"Message {msg_id} marked as read and labeled")
    except Exception as e:
        logger.error(f"Failed to label/mark read for message {msg_id}: {e}")
