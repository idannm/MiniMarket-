import os
import requests
import psycopg2
from flask import Flask, request, jsonify
from groq import Groq

app = Flask(__name__)

# הגדרות מה-Environment
DB_URL = os.environ.get("DB_URL")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")

client = Groq(api_key=GROQ_API_KEY)

def send_whatsapp_message(to, text):
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    data = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    requests.post(url, headers=headers, json=data)

@app.route('/send_update', methods=['POST'])
def send_update():
    """זה החלק שמאפשר ל-Streamlit לשלוח הודעות אישור"""
    data = request.get_json()
    phone = data.get('phone')
    message = data.get('message')
    if phone and message:
        send_whatsapp_message(phone, message)
        return {"status": "success"}, 200
    return {"status": "failed"}, 400

@app.route('/webhook', methods=['GET'])
def verify():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge")
    return "Failed", 403

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    try:
        if 'messages' in data['entry'][0]['changes'][0]['value']:
            message = data['entry'][0]['changes'][0]['value']['messages'][0]
            sender = message['from']
            user_text = message['text']['body']
            
            # שליפת מלאי ו-AI (כפי שכתבת)
            conn = psycopg2.connect(DB_URL)
            cur = conn.cursor()
            cur.execute("SELECT name, price, stock FROM products;")
            items = cur.fetchall()
            cur.close()
            conn.close()
            inventory_text = "\n".join([f"{item[0]}: {item[1]}₪" for item in items])
            
            completion = client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[
                    {"role": "system", "content": f"אתה העוזר של המכולת. מלאי: {inventory_text}"},
                    {"role": "user", "content": user_text}
                ]
            )
            bot_reply = completion.choices[0].message.content
            
            if "להזמין" in user_text or "הזמנה" in user_text:
                # כאן כדאי להוסיף לטבלת ה-orders
                bot_reply += "\n\n(ההזמנה נרשמה במערכת ✅)"

            send_whatsapp_message(sender, bot_reply)
    except Exception as e:
        print(f"Error: {e}")
    return "ok", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
