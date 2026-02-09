import os
import requests
import psycopg2
from flask import Flask, request, jsonify
from groq import Groq
import json

app = Flask(__name__)

# --- הגדרות ---
DB_URL = os.environ.get("DB_URL")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "my_verify_token")
INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET", "123")

client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

def get_db_connection():
    return psycopg2.connect(DB_URL)

# --- פונקציה חדשה למניעת כפילויות ---
def is_message_processed(message_id):
    """בודק אם ההודעה כבר טופלה בעבר. אם לא - רושם אותה."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # בדיקה אם קיים
        cur.execute("SELECT 1 FROM processed_messages WHERE message_id = %s", (message_id,))
        exists = cur.fetchone()
        
        if exists:
            conn.close()
            return True # ההודעה כבר טופלה!
            
        # אם לא קיים - נרשום אותה
        cur.execute("INSERT INTO processed_messages (message_id) VALUES (%s)", (message_id,))
        conn.commit()
        conn.close()
        return False # הודעה חדשה, אפשר לטפל בה
    except Exception as e:
        print(f"Deduplication Error: {e}")
        return False # במקרה של שגיאה, ננסה לענות בכל זאת

def save_message(phone, role, content):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO conversation_history (phone, role, content) VALUES (%s, %s, %s)", (phone, role, content))
        conn.commit()
        conn.close()
    except: pass

def get_history(phone):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT role, content FROM conversation_history WHERE phone = %s ORDER BY created_at DESC LIMIT 10", (phone,))
        rows = cur.fetchall()
        conn.close()
        return [{"role": r[0], "content": r[1]} for r in rows][::-1]
    except: return []

def get_inventory():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT name, price FROM products WHERE stock > 0")
        items = cur.fetchall()
        conn.close()
        if items: return ", ".join([f"{i[0]} ({i[1]}₪)" for i in items])
        return "אין סחורה"
    except: return "שגיאה בטעינה"

def save_order(name, phone, address, items, original_sender_id):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # שומרים את ה-ID המקורי בתוך הכתובת כדי שנוכל לחזור אליו מהדשבורד
        final_address = f"{address} | WA_ID:{original_sender_id}"
        
        cur.execute("INSERT INTO orders (customer_name, items, status, total_price, address) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                    (name, items, 'ממתין לאישור', 0, final_address))
        new_id = cur.fetchone()[0]
        conn.commit()
        conn.close()
        return new_id
    except Exception as e:
        print(f"Save Error: {e}")
        return None

def send_whatsapp(to, text):
    try:
        if to.startswith("0"): to = "972" + to[1:]
        url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
        headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
        data = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
        r = requests.post(url, headers=headers, json=data)
        if r.status_code == 200:
            save_message(to, "assistant", text)
    except: pass

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    try:
        if 'messages' in data['entry'][0]['changes'][0]['value']:
            msg = data['entry'][0]['changes'][0]['value']['messages'][0]
            msg_id = msg['id'] # המזהה הייחודי של ההודעה
            sender = msg['from']
            text = msg['text']['body']
            
            # --- תיקון קריטי: בדיקת כפילויות ---
            if is_message_processed(msg_id):
                print(f"Duplicate message ignored: {msg_id}")
                return "ok", 200 # מחזירים אישור לוואטסאפ אבל לא עושים כלום
            
            # מכאן ממשיכים רגיל רק אם ההודעה חדשה
            save_message(sender, "user", text)
            history = get_history(sender)
            inventory = get_inventory()
            
            system_prompt = f"""
            אתה "בוטי", המוכר במכולת. המלאי: {inventory}
            
            חוקים:
            1. אם הלקוח שואל שאלות כלליות ("מה הזמנתי?", "מה קורה?") - תענה רגיל. אל תיצור הזמנה.
            2. רק אם הלקוח מבקש במפורש לקנות ("תזמין לי", "אני רוצה", "סגור הזמנה"), תוציא את הפקודה:
            FINAL_ORDER|{sender}|[שם]|[כתובת]|[מוצרים]
            
            אל תכתוב FINAL_ORDER סתם.
            """
            
            if client:
                completion = client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[{"role": "system", "content": system_prompt}] + history,
                    temperature=0.3, max_tokens=200
                )
                bot_reply = completion.choices[0].message.content.strip()
                
                # בדיקה אם זו הזמנה אמיתית (ולא סתם שאלה)
                if "FINAL_ORDER|" in bot_reply and not "מה הזמנתי" in text:
                    try:
                        parts = bot_reply.split("|")
                        name = parts[2]
                        addr = parts[3]
                        items = parts[4]
                        
                        oid = save_order(name, sender, addr, items, sender)
                        final_msg = f"תודה {name}, הזמנה #{oid} הועברה לאישור המנהל! ⏳\n(פרטים: {items} לכתובת {addr})"
                        send_whatsapp(sender, final_msg)
                    except:
                        send_whatsapp(sender, "ההזמנה נקלטה וממתינה לאישור.")
                else:
                    # מנקים שאריות אם הבוט התבלבל
                    clean_reply = bot_reply.replace("FINAL_ORDER", "").split("|")[-1]
                    send_whatsapp(sender, clean_reply)
                    
    except Exception as e: print(f"Error: {e}")
    return "ok", 200

@app.route('/webhook', methods=['GET'])
def verify():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN: return request.args.get("hub.challenge")
    return "Error", 403

@app.route('/send_update', methods=['POST'])
def send_update():
    auth = request.headers.get('X-Internal-Secret')
    if auth != INTERNAL_SECRET: return jsonify({"error": "Unauthorized"}), 401
    
    data = request.json
    phone = str(data.get('phone')).strip()
    send_whatsapp(phone, data.get('message'))
    return jsonify({"status": "sent"}), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
