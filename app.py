import os
import requests
import psycopg2
from flask import Flask, request
from groq import Groq

app = Flask(__name__)

# הגדרות מה-Environment
DB_URL = os.environ.get("DB_URL")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")

client = Groq(api_key=GROQ_API_KEY)

# --- פונקציות עזר למסד הנתונים ---

def get_inventory():
    """שליפת המלאי מהדאטהבייס"""
    try:
        conn = psycopg2.connect(DB_URL)
        cur = conn.cursor()
        cur.execute("SELECT name, price, stock FROM products;")
        items = cur.fetchall()
        cur.close()
        conn.close()
        inventory_text = "\n".join([f"{item[0]}: {item[1]}₪ (מלאי: {item[2]})" for item in items])
        return inventory_text
    except:
        return "לא הצלחתי לשלוף את המלאי כרגע."

def save_order(customer_phone, order_text):
    """שמירת הזמנה חדשה בטבלה"""
    try:
        conn = psycopg2.connect(DB_URL)
        cur = conn.cursor()
        cur.execute("INSERT INTO orders (customer_phone, details, status) VALUES (%s, %s, %s)", 
                    (customer_phone, order_text, "חדש"))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        print(f"Error saving order: {e}")
        return False

# --- שליחת הודעה לוואטסאפ ---

def send_whatsapp_message(to, text):
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    data = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    requests.post(url, headers=headers, json=data)

# --- ניהול ההודעות ---

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
            
            # שליפת המלאי העדכני כדי שה-AI ידע מה לענות
            current_stock = get_inventory()
            
            # בניית הנחיות ל-AI
            system_prompt = f"""
            אתה העוזר של המכולת של עידן. 
            המלאי הנוכחי הוא:
            {current_stock}
            
            אם לקוח מבקש להזמין משהו, תענה לו בחיוב ותגיד שההזמנה נרשמה.
            תענה תמיד בעברית, קצר ולעניין.
            """
            
            completion = client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text}
                ]
            )
            bot_reply = completion.choices[0].message.content
            
            # אם הלקוח אמר "להזמין" או "הזמנה", נשמור את זה בדאטהבייס
            if "להזמין" in user_text or "הזמנה" in user_text:
                save_order(sender, user_text)
                bot_reply += "\n\n(ההזמנה שלך נשמרה במערכת  ✅)"

            send_whatsapp_message(sender, bot_reply)
            
    except Exception as e:
        print(f"Error: {e}")
        
    return "ok", 200
