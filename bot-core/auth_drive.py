import os
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

SCOPES = ['https://www.googleapis.com/auth/drive']

def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    token_path = os.path.join(script_dir, 'token.json')
    credentials_path = os.path.join(script_dir, 'credentials.json')
    
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    
    if not creds or not creds.valid:
        success = False
        if creds and creds.expired and creds.refresh_token:
            try:
                print("Attempting to refresh Google Drive token automatically...")
                creds.refresh(Request())
                success = True
            except Exception as e:
                print(f"Failed to refresh token: {e}")
                print("Starting new login flow...")
        
        if not success:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)
            
        with open(token_path, 'w') as token:
            token.write(creds.to_json())
    
    print("SUCCESS: token.json has been generated successfully!")

if __name__ == '__main__':
    main()
