import os
import requests
from flask import Flask, request, jsonify
from groq import Groq

app = Flask(__name__)

# שליפת משתנים מהגדרות ה-Render
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")

# חיבור ל-AI
client = Groq(api_key=GROQ_API_KEY)

def send_whatsapp_message(to, text):
    """פונקציה ששולחת הודעה חזרה למשתמש"""
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }
    response = requests.post(url, headers=headers, json=data)
    return response.json()

@app.route('/webhook', methods=['GET'])
def verify():
    """שלב האימות מול פייסבוק"""
    if request.args.get("hub.mode") == "subscribe" and request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge")
    return "Verification failed", 403

@app.route('/webhook', methods=['POST'])
def webhook():
    """קבלת הודעות מוואטסאפ ושליחת תשובה"""
    data = request.get_json()
    
    try:
        if 'messages' in data['entry'][0]['changes'][0]['value']:
            message = data['entry'][0]['changes'][0]['value']['messages'][0]
            sender_phone = message['from']
            user_text = message['text']['body']
            
            # 1. הבוט חושב על תשובה בעזרת ה-AI
            completion = client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[
                    {"role": "system", "content": "אתה עוזר חכם למכולת של עידן. תענה בקצרה ובעברית."},
                    {"role": "user", "content": user_text}
                ]
            )
            bot_reply = completion.choices[0].message.content
            
            # 2. שליחת התשובה חזרה לוואטסאפ של המשתמש
            send_whatsapp_message(sender_phone, bot_reply)
            
    except Exception as e:
        print(f"Error processing message: {e}")
        
    return "ok", 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
