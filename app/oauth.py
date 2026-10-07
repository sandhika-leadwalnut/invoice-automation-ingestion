import logging
import os.path
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = [
    'https://www.googleapis.com/auth/gmail.modify',
    'https://www.googleapis.com/auth/drive.file'
]

# Relative paths for credentials
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CREDENTIALS_FILE = os.path.join(BASE_DIR, 'credentials.json')
TOKEN_FILE = os.path.join(BASE_DIR, 'token.json')

# Must match an Authorised redirect URI on the OAuth client in the Cloud
# console: http://localhost:8080/
OAUTH_LOCAL_PORT = int(os.getenv("OAUTH_LOCAL_PORT", "8080"))

def get_google_credentials():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    
    if not creds or not creds.valid:
        refreshed = False
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                refreshed = True
            except RefreshError as e:
                # Google rejects a refresh token outright when the account
                # password is changed, when access is revoked, or when the
                # OAuth consent screen is still in Testing (those expire after
                # seven days). The stored token is now worthless, so fall
                # through to a fresh sign-in rather than raising - otherwise a
                # dead token permanently blocks re-authorisation and the only
                # way out is deleting the file by hand.
                logger.error(
                    "Stored Google token was rejected (%s). A new sign-in is "
                    "needed - run `python app/oauth.py` on a machine with a "
                    "browser and copy token.json to the server.", e,
                )
                creds = None

        if not refreshed and not creds:
            flow = InstalledAppFlow.from_client_secrets_file(
                CREDENTIALS_FILE, SCOPES)
            # Fixed port, not 0. credentials.json holds a "web" client, and a
            # web client only accepts redirect URIs registered against it in
            # the Cloud console - a random port can never match one, which is
            # what produced redirect_uri_mismatch. http://localhost:8080/ is
            # registered there; keep the two in step if either changes.
            # A desktop-type client would accept any port, so this is also
            # safe if the credentials are ever swapped for one.
            creds = flow.run_local_server(port=OAUTH_LOCAL_PORT)

        with open(TOKEN_FILE, 'w') as token:
            token.write(creds.to_json())

    return creds

def get_gmail_service():
    creds = get_google_credentials()
    return build('gmail', 'v1', credentials=creds)

def get_drive_service():
    creds = get_google_credentials()
    return build('drive', 'v3', credentials=creds)

if __name__ == "__main__":
    # Run this script directly to generate the token.json
    print("Initializing Google OAuth...")
    get_google_credentials()
    print("OAuth setup complete. token.json generated.")
