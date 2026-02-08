import os
import requests
import psycopg2
from flask import Flask, request, jsonify
import google.generativeai as genai

app = Flask(__name__)

# --- משתני סביבה ---
DB_URL = os.environ.get("DB_URL")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "my_verify_token")
INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET", "123")

# הגדרת Gemini
if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)

# --- פונקציות עזר ---
def get_db_connection():
    return psycopg2.connect(DB_URL)

def get_inventory_text():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT name, price FROM products WHERE stock > 0")
        items = cur.fetchall()
        conn.close()
        if items: return ", ".join([f"{i[0]} ({i[1]}₪)" for i in items])
        return "אין סחורה כרגע"
    except: return "תקלה בטעינת מלאי"

def save_order(name, phone, address, items):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO orders (customer_name, items, status, total_price, address) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                    (name, items, 'ממתין לאישור', 0, f"{address} | טלפון: {phone}"))
        new_id = cur.fetchone()[0]
        conn.commit()
        conn.close()
        return new_id
    except Exception as e:
        print(f"DB Error: {e}")
        return None

def send_whatsapp_message(to, text):
    try:
        if to.startswith("0"): to = "972" + to[1:]
        url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
        headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
        data = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
        requests.post(url, headers=headers, json=data)
    except Exception as e:
        print(f"WhatsApp Error: {e}")

# --- הבוט עצמו ---
@app.route('/webhook', methods=['GET'])
def verify():
    """אימות מול פייסבוק"""
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge")
    return "Error", 403

@app.route('/webhook', methods=['POST'])
def webhook():
    """קבלת הודעות"""
    data = request.get_json()
    try:
        if 'messages' in data['entry'][0]['changes'][0]['value']:
            message = data['entry'][0]['changes'][0]['value']['messages'][0]
            sender = message['from']
            text = message['text']['body']
            
            # 1. הכנת נתונים ל-AI
            inventory = get_inventory_text()
            
            # 2. הנחיה ל-Gemini
            system_instruction = f"""
            אתה העוזר של מכולת "הזוג". המלאי: {inventory}
            הוראות:
            1. היה נחמד וקצר. אל תמציא מוצרים.
            2. בקש מהלקוח שם, כתובת ומוצרים.
            3. רק כשיש לך את הכל, כתוב בדיוק כך:
            FINAL_ORDER|{sender}|[שם]|[כתובת]|[מוצרים]
            
            אל תכתוב FINAL_ORDER אם חסר משהו!
            """
            
            # 3. שליחה ל-Google
            model = genai.GenerativeModel('gemini-1.5-flash', system_instruction=system_instruction)
            # (בגרסה פשוטה זו אין היסטוריה מלאה כדי למנוע סיבוכים, אבל המודל חכם מספיק להבין מהקשר קצר)
            response = model.generate_content(f"הלקוח כתב: {text}")
            bot_reply = response.text.strip()

            # 4. בדיקה אם זו הזמנה
            if "FINAL_ORDER|" in bot_reply:
                try:
                    parts = bot_reply.split("|")
                    name = parts[2]
                    addr = parts[3]
                    items = parts[4]
                    
                    oid = save_order(name, sender, addr, items)
                    final_msg = f"תודה {name}! הזמנה {oid} התקבלה ונשלח ל-{addr}."
                    send_whatsapp_message(sender, final_msg)
                except:
                    send_whatsapp_message(sender, "ההזמנה נקלטה במערכת! תודה.")
            else:
                send_whatsapp_message(sender, bot_reply)

    except Exception as e:
        print(f"Error: {e}")

    return "ok", 200

# --- נקודת קצה לדשבורד (חשוב!) ---
@app.route('/send_update', methods=['POST'])
def send_dashboard_update():
    auth = request.headers.get('X-Internal-Secret')
    if auth != INTERNAL_SECRET: return jsonify({"error": "Unauthorized"}), 401
    
    data = request.json
    phone = str(data.get('phone')).replace("WhatsApp:", "").strip()
    send_whatsapp_message(phone, data.get('message'))
    return jsonify({"status": "sent"}), 200

if __name__ == '__main__':
    # הרצה מקומית או דרך Gunicorn
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
