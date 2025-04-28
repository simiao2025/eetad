import os
import schedule
import time
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from datetime import datetime
import shutil

# Configurações
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
CSV_FILE = "pagamentos.csv"
SCOPES = ["https://www.googleapis.com/auth/drive"]

# Configuração do Google Drive
creds = None
if os.path.exists("token.json"):
    creds = Credentials.from_authorized_user_file("token.json", SCOPES)
if not creds or not creds.valid:
    flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
    creds = flow.run_local_server(port=0)
    with open("token.json", "w") as token:
        token.write(creds.to_json())
drive_service = build("drive", "v3", credentials=creds)

def backup_csv():
    """Faz backup do CSV no Google Drive."""
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = f"pagamentos_backup_{timestamp}.csv"
        shutil.copy(CSV_FILE, backup_file)
        file_metadata = {
            "name": backup_file,
            "parents": [GOOGLE_DRIVE_FOLDER_ID]
        }
        media = MediaFileUpload(backup_file, mimetype="text/csv")
        drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id"
        ).execute()
        os.remove(backup_file)
        print(f"Backup {backup_file} concluído.")
    except Exception as e:
        print(f"Erro no backup: {str(e)}")

# Agendar backup diário às 23:59
schedule.every().day.at("23:59").do(backup_csv)

if __name__ == "__main__":
    while True:
        schedule.run_pending()
        time.sleep(60)