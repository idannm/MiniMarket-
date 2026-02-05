from flask import Flask, request, jsonify
import os
from groq import Groq
import psycopg2

app = Flask(__name__)

# הגדרת המשתנים
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "my_secret_password")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
DB_URL = os.environ.get("DB_URL")

# נתיב לאימות מול פייסבוק (החלק הכי חשוב!)
@app.route('/webhook', methods=['GET'])
def verify_webhook():
    mode = request.args.get('hub.mode')
    token = request.args.get('hub.verify_token')
    challenge = request.args.get('hub.challenge')

    if mode and token:
        if mode == 'subscribe' and token == VERIFY_TOKEN:
            print("WEBHOOK_VERIFIED")
            return challenge, 200
        else:
            return 'Forbidden', 403
    return 'Hello World', 200

# נתיב לקבלת הודעות מוואטסאפ
@app.route('/webhook', methods=['POST'])
def receive_message():
    data = request.json
    print(f"Received message: {data}")
    # כאן נכניס אחר כך את הלוגיקה של Groq והדאטהבייס
    return 'EVENT_RECEIVED', 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
