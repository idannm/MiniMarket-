import os
import requests
import psycopg2
from flask import Flask, request, jsonify
from groq import Groq
import time

app = Flask(__name__)

# --- הגדרות ---
DB_URL = os.environ.get("DB_URL")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "my_secret_password")
INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET", "123")

client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

def get_db_connection():
    return psycopg2.connect(DB_URL)

def is_message_processed(message_id):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM processed_messages WHERE message_id = %s", (message_id,))
        exists = cur.fetchone()
        if exists:
            conn.close()
            return True 
        cur.execute("INSERT INTO processed_messages (message_id) VALUES (%s)", (message_id,))
        conn.commit()
        conn.close()
        return False 
    except: return False 

def save_message(phone, role, content):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO conversation_history (phone, role, content) VALUES (%s, %s, %s) RETURNING id", (phone, role, content))
        new_id = cur.fetchone()[0]
        conn.commit()
        conn.close()
        return new_id
    except: return None

def get_history(phone):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT role, content FROM conversation_history WHERE phone = %s ORDER BY created_at DESC LIMIT 10", (phone,))
        rows = cur.fetchall()
        conn.close()
        return [{"role": r[0], "content": r[1]} for r in rows][::-1]
    except: return []

def clear_history(phone):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM conversation_history WHERE phone = %s", (phone,))
        conn.commit()
        conn.close()
    except: pass

def get_inventory():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT name, price FROM products WHERE stock > 0")
        items = cur.fetchall()
        conn.close()
        return ", ".join([f"{i[0]} ({i[1]}₪)" for i in items]) if items else "המלאי כרגע ריק"
    except: return "שגיאה בטעינת המלאי"

def save_order(name, phone, address, items, original_sender_id, order_type):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        final_address = f"{address} | WA_ID:{original_sender_id}"
        cur.execute("INSERT INTO orders (customer_name, items, status, total_price, address, order_type) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                    (name, items, 'ממתין לאישור', 0, final_address, order_type))
        new_id = cur.fetchone()[0]
        conn.commit()
        conn.close()
        return new_id
    except Exception as e:
        print(f"Error saving order: {e}")
        return None

def save_complaint(name, phone, description):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO complaints (customer_name, phone, description) VALUES (%s, %s, %s)",
                    (name, phone, description))
        conn.commit()
        conn.close()
        return True
    except: return False

def send_whatsapp(to, text):
    try:
        if to.startswith("0"): to = "972" + to[1:]
        url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
        headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
        data = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
        requests.post(url, headers=headers, json=data)
        save_message(to, "assistant", text)
    except: pass

@app.route('/webhook', methods=['GET'])
def verify_webhook():
    mode = request.args.get('hub.mode')
    token = request.args.get('hub.verify_token')
    challenge = request.args.get('hub.challenge')
    if mode == 'subscribe' and token == VERIFY_TOKEN:
        return challenge, 200
    return 'Forbidden', 403

@app.route('/webhook', methods=['POST'])
def receive_message():
    data = request.json
    try:
        if 'entry' in data and 'changes' in data['entry'][0]:
            value = data['entry'][0]['changes'][0]['value']
            if 'messages' in value:
                msg = value['messages'][0]
                
                if msg.get('type') != 'text':
                    return "ok", 200
                    
                msg_id = msg['id']
                sender = msg['from']
                text = msg['text']['body']
                
                if is_message_processed(msg_id): 
                    return "ok", 200
                
                # שומרים את ההודעה ומקבלים את המספר הסידורי שלה
                my_msg_id = save_message(sender, "user", text)
                
                if not my_msg_id:
                    return "ok", 200
                
                # --- הפתרון להפצצת הודעות דרך מסד הנתונים ---
                time.sleep(3.5) # עוצרים ל-3.5 שניות
                
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute("SELECT MAX(id) FROM conversation_history WHERE phone = %s AND role = 'user'", (sender,))
                latest_msg_id = cur.fetchone()[0]
                conn.close()
                
                # אם מישהו שלח הודעה חדשה יותר בזמן שחיכינו - נשתוק וניתן להודעה החדשה לענות על הכל!
                if latest_msg_id and latest_msg_id > my_msg_id:
                    return "ok", 200
                # ---------------------------------------------
                
                # אם זו ההודעה האחרונה - נמשוך את כל ההיסטוריה ונענה!
                history = get_history(sender)
                inventory = get_inventory()
                
                system_prompt = f"""
                אתה "חיים", המוכר האדיב במכולת. המלאי: {inventory}
                
                שים לב - יש לך שני מצבים:
                
                🚨 מצב 1: תלונות ובעיות (עדיפות עליונה!) 🚨
                אם הלקוח כועס, אומר שמשהו מגעיל, קרוע, או רוצה להתלונן:
                1. התנצל מיד ("אוי, אני ממש מצטער לשמוע...").
                2. בשום אופן אל תציע לו קניות ואל תשאל "תרצה להוסיף עוד משהו?".
                3. תוציא בשורה נפרדת את הפקודה הבאה:
                FINAL_COMPLAINT|{sender}|[שם אם יש, אחרת 'לקוח']|[תיאור קצר של התלונה]
                
                🛒 מצב 2: קניות רגילות (חובה לעבוד שלב אחרי שלב) 🛒
                שלב 1 - הוספת מוצרים: שים בעגלה ושאל: "תרצה להוסיף עוד משהו? 🍞🥛" (אסור לשאול פרטים אחרים).
                שלב 2 - משלוח/איסוף: רק כשהלקוח סיים לבחור לגמרי, שאל: "תרצה משלוח 🛵 או לבוא לקחת 🛒?"
                שלב 3 - פרטים:
                - איסוף עצמי: בקש שם מלא בלבד. 
                - משלוח: בקש שם מלא, עיר, רחוב ומספר בית.
                שלב 4 - סגירה: רק כשיש שם מלא, הוצא פקודה:
                FINAL_ORDER|{sender}|[שם]|[כתובת או 'איסוף']|[מוצרים]|[סוג]
                """
                
                if client:
                    completion = client.chat.completions.create(
                        model="llama-3.3-70b-versatile",
                        messages=[{"role": "system", "content": system_prompt}] + history,
                        temperature=0.2, 
                        max_tokens=300
                    )
                    bot_reply = completion.choices[0].message.content.strip()
                    
                    if "FINAL_COMPLAINT|" in bot_reply:
                        parts = bot_reply.split("|")
                        if len(parts) >= 4:
                            save_complaint(parts[2], parts[1], parts[3])
                            send_whatsapp(sender, f"אני ממש מתנצל על זה! 🤕 העברתי את התלונה ישירות לבוס לבדיקה דחופה מולך.")
                            clear_history(sender)
                    elif "FINAL_ORDER|" in bot_reply:
                        parts = bot_reply.split("|")
                        if len(parts) >= 6:
                            name = parts[2].strip()
                            address = parts[3].strip()
                            items = parts[4].strip()
                            order_type = parts[5].strip()
                            
                            if "[שם]" in name or name == "" or "name" in name.lower():
                                send_whatsapp(sender, "שכחתי לשאול - איך קוראים לך? (שם מלא בבקשה) כדי שאוכל לרשום על ההזמנה.")
                            else:
                                oid = save_order(name, sender, address, items, sender, order_type)
                                
                                if "איסוף" in order_type or "לקחת" in order_type:
                                    final_msg = f"פיקס {name}! ההזמנה התקבלה. מתחילים לארוז 🛒 ומיד תקבל הודעה מתי לבוא לקחת.\n\n*נ.ב. זמן ההמתנה המקסימלי לאישור הוא 20 דקות!*"
                                else:
                                    final_msg = f"פיקס {name}! ההזמנה הועברה לאישור הבוס ⏳ נעדכן מיד כשהמשלוח ייצא."
                                
                                send_whatsapp(sender, final_msg)
                                clear_history(sender)
                    else:
                        clean_reply = bot_reply.replace("FINAL_ORDER", "").replace("FINAL_COMPLAINT", "").strip()
                        if clean_reply:
                            send_whatsapp(sender, clean_reply)
                            
    except Exception as e: 
        print(f"Webhook Error: {e}")
        
    return 'EVENT_RECEIVED', 200

@app.route('/send_update', methods=['POST'])
def send_update():
    auth = request.headers.get('X-Internal-Secret')
    if auth != INTERNAL_SECRET: 
        return jsonify({"error": "Unauthorized"}), 401
    
    data = request.json
    phone = str(data.get('phone')).strip()
    send_whatsapp(phone, data.get('message'))
    return jsonify({"status": "sent"}), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
