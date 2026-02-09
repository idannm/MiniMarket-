import os
import requests
import psycopg2
from flask import Flask, request, jsonify
from groq import Groq
import json

app = Flask(__name__)

# --- הגדרות משתנים ---
DB_URL = os.environ.get("DB_URL")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "my_verify_token")
INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET", "123")

# חיבור ל-Groq
client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# --- פונקציות עזר למסד הנתונים ---
def get_db_connection():
    return psycopg2.connect(DB_URL)

def save_message(phone, role, content):
    """שמירת הודעה בהיסטוריה"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO conversation_history (phone, role, content) VALUES (%s, %s, %s)", (phone, role, content))
        conn.commit()
        conn.close()
    except: pass

def get_history(phone):
    """שליפת היסטוריית שיחה"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT role, content FROM conversation_history WHERE phone = %s ORDER BY created_at DESC LIMIT 15", (phone,))
        rows = cur.fetchall()
        conn.close()
        return [{"role": r[0], "content": r[1]} for r in rows][::-1]
    except: return []

def get_inventory():
    """שליפת מוצרים זמינים"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT name, price FROM products WHERE stock > 0")
        items = cur.fetchall()
        conn.close()
        if items: return ", ".join([f"{i[0]} ({i[1]}₪)" for i in items])
        return "אין סחורה כרגע"
    except: return "שגיאה בטעינת מלאי"

def save_order(name, phone, address, items):
    """שמירת הזמנה חדשה (בסטטוס ממתין)"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # שים לב: הסטטוס הוא 'ממתין לאישור'
        cur.execute("INSERT INTO orders (customer_name, items, status, total_price, address) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                    (name, items, 'ממתין לאישור', 0, f"{address} | טלפון: {phone}"))
        new_id = cur.fetchone()[0]
        conn.commit()
        conn.close()
        return new_id
    except: return None

def send_whatsapp(to, text):
    """שליחת הודעה לוואטסאפ"""
    try:
        if to.startswith("0"): to = "972" + to[1:]
        url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
        headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
        data = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
        requests.post(url, headers=headers, json=data)
        save_message(to, "assistant", text)
    except: pass

# --- הבוט (Webhook) ---
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    try:
        if 'messages' in data['entry'][0]['changes'][0]['value']:
            msg = data['entry'][0]['changes'][0]['value']['messages'][0]
            sender = msg['from']
            text = msg['text']['body']
            
            # 1. שמירה וטעינת הקשר
            save_message(sender, "user", text)
            history = get_history(sender)
            inventory = get_inventory()
            
            # 2. המוח של הבוט
            system_prompt = f"""
            אתה "בוטי", המוכר במכולת "הזוג".
            המלאי הקיים: {inventory}
            
            הוראות:
            1. היה נחמד, קצר וענייני. דבר עברית בלבד.
            2. המטרה שלך: להבין מה הלקוח רוצה, ואז לבקש שם וכתובת למשלוח.
            3. כשלקוח נותן את כל הפרטים (מוצרים, שם, כתובת), שלח את הפקודה:
            FINAL_ORDER|{sender}|[שם]|[כתובת]|[רשימת מוצרים]
            
            אם חסר משהו, תשאל את הלקוח. אל תמציא פרטים.
            """
            
            if client:
                completion = client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[{"role": "system", "content": system_prompt}] + history,
                    temperature=0.3, max_tokens=200
                )
                bot_reply = completion.choices[0].message.content.strip()
                
                # 3. זיהוי הזמנה
                if "FINAL_ORDER|" in bot_reply:
                    try:
                        parts = bot_reply.split("|")
                        name = parts[2]
                        addr = parts[3]
                        items = parts[4]
                        
                        oid = save_order(name, sender, addr, items)
                        
                        # --- השינוי שביקשת: הודעה על המתנה לאישור ---
                        final_msg = f"תודה {name}! ההזמנה (#{oid}) נקלטה במערכת.\nהיא הועברה לאישור המנהל, נעדכן אותך בוואטסאפ ברגע שתאושר (כולל זמן הגעה)! ⏳"
                        
                        send_whatsapp(sender, final_msg)
                    except:
                        send_whatsapp(sender, "ההזמנה נקלטה וממתינה לאישור!")
                else:
                    send_whatsapp(sender, bot_reply)
                    
    except Exception as e:
        print(f"Error: {e}")

    return "ok", 200

# --- אימות וחיבור לדשבורד ---
@app.route('/webhook', methods=['GET'])
def verify():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge")
    return "Error", 403

@app.route('/send_update', methods=['POST'])
def send_update():
    # אבטחה: רק הדשבורד יכול לשלוח הודעות דרך כאן
    auth = request.headers.get('X-Internal-Secret')
    if auth != INTERNAL_SECRET: return jsonify({"error": "Unauthorized"}), 401
    
    data = request.json
    phone = str(data.get('phone')).replace("WhatsApp:", "").strip()
    send_whatsapp(phone, data.get('message'))
    return jsonify({"status": "sent"}), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
