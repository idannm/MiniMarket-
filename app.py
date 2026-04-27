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
        if 'entry' in data and 'changes' in data['entry'][0]:
            value = data['entry'][0]['changes'][0]['value']
            if 'messages' in value:
                msg = value['messages'][0]
                
                # מוודא שזו הודעת טקסט בלבד
                if msg.get('type') != 'text':
                    return "ok", 200
                    
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
                אתה "בוטי", המוכר האדיב במכולת. המלאי: {inventory}
                
                חוקים:
                1. תענה באופן טבעי, קצר ושירותי.
                2. אם הלקוח שואל שאלות כלליות ("מה הזמנתי?", "מה קורה?") - תענה רגיל. אל תיצור הזמנה!
                3. כדי לסגור הזמנה חובה שיהיה לך מהלקוח: שם, כתובת, ומוצרים שהוא רוצה.
                4. רק כשהלקוח אישר שהוא מסיים ויש לך את כל הפרטים, תוסיף *בסוף ההודעה שלך (בשורה חדשה)* את הפקודה המדויקת הבאה:
                FINAL_ORDER|{sender}|[שם]|[כתובת]|[מוצרים]
                """
                
                if client:
                    completion = client.chat.completions.create(
                        model="llama-3.3-70b-versatile",
                        messages=[{"role": "system", "content": system_prompt}] + history,
                        temperature=0.3, max_tokens=200
                    )
                    bot_reply = completion.choices[0].message.content.strip()
                    
                    # בדיקה אם זו הזמנה אמיתית
                    if "FINAL_ORDER|" in bot_reply and "מה הזמנתי" not in text:
                        try:
                            # מבודדים את שורת הפקודה משאר הטקסט במקרה שה-AI כתב גם "תודה רבה" וכו'
                            order_line = [line for line in bot_reply.split('\n') if "FINAL_ORDER|" in line][0]
                            parts = order_line.split("|")
                            
                            # חגורת בטיחות: מוודאים שה-AI באמת החזיר 5 חלקים כמו שביקשנו
                            if len(parts) >= 5:
                                name = parts[2].strip()
                                addr = parts[3].strip()
                                items = parts[4].strip()
                                
                                oid = save_order(name, sender, addr, items, sender)
                                final_msg = f"תודה {name}, הזמנה #{oid} הועברה לאישור המנהל! ⏳\n(פרטים: {items} לכתובת {addr})"
                                send_whatsapp(sender, final_msg)
                            else:
                                send_whatsapp(sender, "חסרים לי כמה פרטים. תוכל לוודא שנתת לי שם, כתובת ומוצרים?")
                                
                        except Exception as e:
                            print(f"Parsing error: {e}")
                            send_whatsapp(sender, "הייתה לי בעיה קטנה לסגור את ההזמנה, תוכל לחזור על הפרטים (שם, כתובת, מוצרים)?")
                    else:
                        # מנקים מילת קוד במקרה וה-AI השתמש בה בטעות, ושולחים את הטקסט נקי
                        clean_reply = bot_reply.replace(f"FINAL_ORDER|{sender}|", "").split("|")[0].strip()
                        send_whatsapp(sender, clean_reply)
                    
    except Exception as e: 
        print(f"Error: {e}")
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
