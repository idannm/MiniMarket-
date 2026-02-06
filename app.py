import os
import requests
from flask import Flask, request

app = Flask(__name__)

# פונקציית השליחה
def send_whatsapp_message(to, text):
    url = f"https://graph.facebook.com/v18.0/{os.environ.get('PHONE_NUMBER_ID')}/messages"
    headers = {"Authorization": f"Bearer {os.environ.get('WHATSAPP_TOKEN')}", "Content-Type": "application/json"}
    data = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    requests.post(url, headers=headers, json=data)

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    if 'messages' in data['entry'][0]['changes'][0]['value']:
        message = data['entry'][0]['changes'][0]['value']['messages'][0]
        sender = message['from']
        # שלח הודעת ניסיון חזרה מיד
        send_whatsapp_message(sender, "קיבלתי את ההודעה שלך! הבוט עובד!")
    return "ok", 200

# שים לב לשם של ה-Webhook גם ב-GET בשביל האימות
@app.route('/webhook', methods=['GET'])
def verify():
    return request.args.get("hub.challenge")
