import os
import requests
import psycopg2
from flask import Flask, request, jsonify
import google.generativeai as genai
import json

app = Flask(__name__)

# --- הגדרות ---
DB_URL = os.environ.get("DB_URL")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY") # שים לב לשם המשתנה החדש
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "my_verify_token")
INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET", "123")

# הגדרת המודל של גוגל
if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)

def get_db_connection():
    return psycopg2.connect(DB_URL)

# --- ניהול זיכרון והיסטוריה ---
def save_message_to_history(phone, role, content):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # ב-Gemini הבוט נקרא 'model' והמשתמש 'user'
        if role == 'assistant': role = 'model'
        cur.execute("INSERT INTO conversation_history (phone, role, content) VALUES (%s, %s, %s)", (phone, role, content))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"History Save Error: {e}")

def get_chat_history_for_gemini(phone):
    """שליפת היסטוריה בפורמט ש-Gemini מבין"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT role, content FROM conversation_history WHERE phone = %s ORDER BY created_at ASC LIMIT 20", (phone,))
        rows = cur.fetchall()
        conn.close()
        
        history = []
        for r in rows:
            role = r[0]
            if role == "assistant": role = "model" # המרה לפורמט של גוגל
            history.append({"role": role, "parts": [r[1]]})
        return history
    except:
        return []

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
    except: return None

def send_whatsapp_message(to, text):
    try:
        if to.startswith("0"): to = "972" + to[1:]
        url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
        headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
        data = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
        requests.post(url, headers=headers, json=data)
        save_message_to_history(to, "model", text)
    except: pass

# --- הבוט (Webhook) ---
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    try:
        if 'messages' in data['entry'][0]['changes'][0]['value']:
            message = data['entry'][0]['changes'][0]['value']['messages'][0]
            sender = message['from']
            text = message['text']['body']
            
            # 1. שמירה
            save_message_to_history(sender, "user", text)
            
            # 2. הכנת נתונים
            inventory = get_inventory_text()
            history = get_chat_history_for_gemini(sender)
            
            # 3. הגדרת המודל עם System Instruction
            system_instruction = f"""
            אתה העוזר של מכולת "הזוג". המלאי: {inventory}
            
            הוראות:
            1. היה אנושי, קצר ונחמד.
            2. אל תמציא מוצרים.
            3. כשלקוח רוצה להזמין, בקש שם וכתובת.
            4. כשיש לך את כל הפרטים (מוצרים, שם, כתובת), הוצא *רק* את הפקודה הזו:
            FINAL_ORDER|{sender}|[שם]|[כתובת]|[מוצרים]
            
            לדוגמה: FINAL_ORDER|972501234567|דני|הרצל 5|חלב ולחם
            אל תוציא את הפקודה הזו אם חסר משהו!
            """
            
            if GOOGLE_API_KEY:
                model = genai.GenerativeModel('gemini-1.5-flash', system_instruction=system_instruction)
                chat = model.start_chat(history=history)
                
                response = chat.send_message(text)
                bot_reply = response.text.strip()

                # 4. זיהוי הזמנה
                if "FINAL_ORDER|" in bot_reply:
                    try:
                        parts = bot_reply.split("|")
                        # parts[0] is FINAL_ORDER, parts[1] is phone (sender)
                        name = parts[2]
                        addr = parts[3]
                        items = parts[4]
                        
                        oid = save_order(name, sender, addr, items)
                        final_msg = f"תודה {name}, ההזמנה (#{oid}) התקבלה! נשלח ל-{addr}."
                        send_whatsapp_message(sender, final_msg)
                    except:
                        send_whatsapp_message(sender, "תודה, ההזמנה התקבלה (הייתה שגיאה קטנה בפיענוח, אני בודק).")
                else:
                    send_whatsapp_message(sender, bot_reply)

    except Exception as e:
        print(f"Error: {e}")

    return "ok", 200

# --- אימות ודשבורד ---
@app.route('/webhook', methods=['GET'])
def verify():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge")
    return "Error", 403

@app.route('/send_update', methods=['POST'])
def send_dashboard_update():
    auth = request.headers.get('X-Internal-Secret')
    if auth != INTERNAL_SECRET: return jsonify({"error": "Unauthorized"}), 401
    
    data = request.json
    clean_phone = str(data.get('phone')).replace("WhatsApp:", "").strip()
    send_whatsapp_message(clean_phone, data.get('message'))
    return jsonify({"status": "sent"}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
