import os
import json
import csv
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import requests
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from unidecode import unidecode
from datetime import datetime
import shutil

# Carregar variáveis de ambiente
load_dotenv()

# Configurações
PAGSEGURO_TOKEN = os.getenv("PAGSEGURO_TOKEN")
EVOLUTION_API_URL = os.getenv("EVOLUTION_API_URL", "http://evolution-api-url:8080")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY")
XAI_API_KEY = os.getenv("XAI_API_KEY")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
SECRETARIA_NUMBER = "+556392261578"
FORM_URL = "https://admissaoprv.com.br/ensino/"
CSV_FILE = "pagamentos.csv"

# Configuração do Flask
app = Flask(__name__)

# Configuração do Google Sheets e Drive
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
creds = None
if os.path.exists("token.json"):
    creds = Credentials.from_authorized_user_file("token.json", SCOPES)
if not creds or not creds.valid:
    flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
    creds = flow.run_local_server(port=0)
    with open("token.json", "w") as token:
        token.write(creds.to_json())
sheets_service = build("sheets", "v4", credentials=creds)
drive_service = build("drive", "v3", credentials=creds)

# Inicializar CSV se não existir
if not os.path.exists(CSV_FILE):
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Data", "Nome", "WhatsApp", "Valor", "Status", "Livro", "TransactionID"])

def normalize_name(name):
    """Normaliza nomes para fuzzy matching."""
    return unidecode(name.lower()).replace(" ", "")

def find_student_by_name(payer_name, students):
    """Encontra aluno na planilha pelo nome completo."""
    payer_name_normalized = normalize_name(payer_name)
    for student in students:
        student_name_normalized = normalize_name(student["Nome"])
        if (
            payer_name_normalized in student_name_normalized
            or student_name_normalized in payer_name_normalized
        ):
            return student
    return None

def append_log(action, details):
    """Registra uma ação na aba Logs do Google Sheets."""
    try:
        values = [[datetime.now().isoformat(), action, details]]
        body = {"values": values}
        sheets_service.spreadsheets().values().append(
            spreadsheetId=GOOGLE_SHEET_ID,
            range="Logs!A:C",
            valueInputOption="USER_ENTERED",
            body=body
        ).execute()
    except Exception as e:
        send_whatsapp_message(SECRETARIA_NUMBER, f"Erro ao registrar log: {str(e)}")

def append_payment(data, student, transaction_id):
    """Adiciona pagamento ao CSV."""
    try:
        with open(CSV_FILE, "r", newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            if transaction_id in [row[-1] for row in reader if row]:
                return  # Evita duplicatas
        with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                datetime.now().isoformat(),
                data.get("sender", {}).get("name"),
                student["WhatsApp"] if student else data.get("sender", {}).get("phone", ""),
                data.get("amount", ""),
                student["Status"] if student else "NÃO MATRICULADO",
                student["Livro"] if student else "",
                transaction_id
            ])
    except Exception as e:
        send_whatsapp_message(SECRETARIA_NUMBER, f"Erro ao registrar pagamento no CSV: {str(e)}")

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
    except Exception as e:
        send_whatsapp_message(SECRETARIA_NUMBER, f"Erro no backup do CSV: {str(e)}")

def send_whatsapp_message(number, text):
    """Envia mensagem via Evolution API."""
    headers = {
        "Content-Type": "application/json",
        "apikey": EVOLUTION_API_KEY
    }
    payload = {"number": number, "text": text}
    try:
        response = requests.post(
            f"{EVOLUTION_API_URL}/message/sendText",
            headers=headers,
            json=payload
        )
        return response.status_code == 200
    except Exception as e:
        append_log("Erro ao enviar mensagem", f"{number}: {str(e)}")
        return False

@app.route("/pagseguro-notification", methods=["POST"])
def pagseguro_notification():
    """Processa notificações de pagamento PIX do PagSeguro."""
    data = request.json
    transaction_id = data.get("transaction_id", "")
    if (
        data.get("status") == "SUCCESS"
        and data.get("payment_method", {}).get("type") == "PIX"
    ):
        payer_name = data.get("sender", {}).get("name")
        sender_phone = data.get("sender", {}).get("phone")

        # Consultar planilha da Secretaria
        try:
            result = sheets_service.spreadsheets().values().get(
                spreadsheetId=GOOGLE_SHEET_ID,
                range="Alunos!A:E"
            ).execute()
            students = [
                {
                    "Nome": row[0],
                    "Email": row[1],
                    "WhatsApp": row[2],
                    "Status": row[3],
                    "Livro": row[4]
                } for row in result.get("values", [])[1:]  # Ignora cabeçalho
            ]
        except Exception as e:
            send_whatsapp_message(SECRETARIA_NUMBER, f"Erro ao consultar planilha: {str(e)}")
            return jsonify({"status": "error"}), 500

        student = find_student_by_name(payer_name, students)
        append_payment(data, student, transaction_id)

        if student:
            if student["Status"] == "ATIVO":
                send_whatsapp_message(student["WhatsApp"], "Pagamento confirmado")
                send_whatsapp_message(
                    SECRETARIA_NUMBER,
                    f"Aluno(a) {student['Nome']}, pagamento efetuado - {student['Livro']}"
                )
                append_log("Pagamento Confirmado (Ativo)", student["Nome"])
            else:  # INATIVO
                send_whatsapp_message(
                    student["WhatsApp"],
                    "Seja bem vindo(a) de volta, bons estudos. Pagamento efetuado"
                )
                send_whatsapp_message(
                    SECRETARIA_NUMBER,
                    f"Aluno(a) {student['Nome']} INATIVA. Pagamento efetuado"
                )
                append_log("Pagamento Confirmado (Inativo)", student["Nome"])
        else:
            send_whatsapp_message(
                sender_phone or (student["WhatsApp"] if student else ""),
                f"Você ainda não fez sua matrícula, preencha a ficha de inscrição, seu pagamento só será confirmado após o preenchimento da ficha de inscrição. Me informa assim que preencher a ficha de inscrição. Link: {FORM_URL}"
            )
            append_log("Solicitação de Matrícula", payer_name)

        backup_csv()
        return jsonify({"status": "success"}), 200
    return jsonify({"status": "ignored"}), 200

@app.route("/receive-comprovante", methods=["POST"])
def receive_comprovante():
    """Recebe comprovantes via WhatsApp e armazena no Google Drive."""
    data = request.json
    message = data.get("body", {}).get("message", {})
    if message.get("hasMedia"):
        media_url = message.get("mediaUrl")
        mimetype = message.get("mimetype")
        from_number = message.get("from")

        try:
            response = requests.get(media_url)
            if response.status_code == 200:
                extension = "pdf" if "pdf" in mimetype else "jpg"
                file_name = f"comprovante_{from_number}_{datetime.now().isoformat()}.{extension}"
                file_path = f"/tmp/{file_name}"
                with open(file_path, "wb") as f:
                    f.write(response.content)

                file_metadata = {
                    "name": file_name,
                    "parents": [GOOGLE_DRIVE_FOLDER_ID]
                }
                media = MediaFileUpload(file_path, mimetype=mimetype)
                drive_service.files().create(
                    body=file_metadata,
                    media_body=media,
                    fields="id"
                ).execute()

                os.remove(file_path)
                append_log("Comprovante Armazenado", from_number)
                return jsonify({"status": "success"}), 200
        except Exception as e:
            send_whatsapp_message(SECRETARIA_NUMBER, f"Erro ao armazenar comprovante: {str(e)}")
            return jsonify({"status": "error"}), 500
    return jsonify({"status": "ignored"}), 200

@app.route("/confirm-registration", methods=["POST"])
def confirm_registration():
    """Analisa mensagens de confirmação de matrícula com Grok."""
    data = request.json
    message_text = data.get("body", {}).get("message", {}).get("text")
    from_number = data.get("body", {}).get("message", {}).get("from")

    if message_text:
        # Sanitizar entrada para evitar prompt injection
        message_text = "".join(c for c in message_text if ord(c) < 128)

        # Chamar a API do Grok
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {XAI_API_KEY}"
        }
        payload = {
            "model": "grok-beta",
            "messages": [
                {
                    "role": "system",
                    "content": "Você é um assistente que verifica se uma mensagem indica que um aluno preencheu uma ficha de matrícula. Responda com um JSON: `{ \"confirmed\": true }` se a mensagem confirmar o preenchimento, ou `{ \"confirmed\": false }` se não confirmar. Exemplo de mensagens confirmatórias: 'Ficha preenchida', 'Inscrição concluída', 'Já enviei a ficha'. Ignore mensagens irrelevantes."
                },
                {
                    "role": "user",
                    "content": message_text
                }
            ],
            "temperature": 0.2,
            "max_tokens": 50
        }
        try:
            response = requests.post(
                "https://api.x.ai/v1/chat/completions",
                headers=headers,
                json=payload
            )
            if response.status_code == 200:
                result = response.json()
                confirmed = json.loads(result["choices"][0]["message"]["content"]).get("confirmed")

                if confirmed:
                    # Consultar aluno na planilha
                    result = sheets_service.spreadsheets().values().get(
                        spreadsheetId=GOOGLE_SHEET_ID,
                        range="Alunos!A:E"
                    ).execute()
                    students = [
                        {
                            "Nome": row[0],
                            "Email": row[1],
                            "WhatsApp": row[2],
                            "Status": row[3],
                            "Livro": row[4]
                        } for row in result.get("values", [])[1:]
                    ]
                    student = next(
                        (s for s in students if s["WhatsApp"] == from_number),
                        None
                    )

                    if student:
                        send_whatsapp_message(
                            SECRETARIA_NUMBER,
                            f"Aluno(a) {student['Nome']} preencheu a ficha de matrícula e efetuou o pagamento do {student['Livro']}"
                        )
                        append_log("Confirmação de Matrícula", student["Nome"])
                        return jsonify({"status": "success"}), 200
            else:
                send_whatsapp_message(SECRETARIA_NUMBER, f"Erro na API Grok: {response.text}")
        except Exception as e:
            send_whatsapp_message(SECRETARIA_NUMBER, f"Erro ao analisar matrícula: {str(e)}")
            return jsonify({"status": "error"}), 500

    return jsonify({"status": "ignored"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)