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
                
                save_message(sender, "user", text)
                history = get_history(sender)
                inventory = get_inventory()
                
                system_prompt = f"""
                אתה "חיים", המוכר האדיב במכולת. המלאי: {inventory}
                
                חוקי ברזל - חובה לעבוד שלב אחרי שלב, לעולם אל תשאל שאלות של שלב מתקדם לפני שסיימת את הנוכחי:
                
                שלב 1 - בחירת מוצרים: 
                כשהלקוח מבקש מוצר, הוסף אותו והגב משהו כמו: "מעולה, שמתי לך. תרצה להוסיף עוד משהו? 🍞🥛". (אל תשאל על משלוח או כתובת בשלב זה!).
                
                שלב 2 - סיכום וסוג קבלה:
                *רק אחרי* שהלקוח אמר מפורשות שהוא סיים (למשל "זהו", "לא תודה", "אני רוצה את הכל"), הצג לו את המחיר הסופי ושאל שאלה אחת בלבד: "תרצה משלוח עד הבית 🛵 או לבוא לקחת מהמקום (איסוף עצמי) 🛒?"
                
                שלב 3 - איסוף פרטים:
                - אם הלקוח בחר איסוף עצמי: בקש ממנו שם מלא בלבד.
                - אם הלקוח בחר משלוח: בקש ממנו שם מלא, עיר, רחוב ומספר בית.
                
                שלב 4 - סגירה:
                רק אחרי שהלקוח ענה וסיפק את כל הפרטים הרלוונטיים, הוצא את הפקודה FINAL_ORDER.
                
                כללים נוספים:
                - שפות: ענה ללקוח בשפה שבה הוא פונה אליך (למשל, אנגלית).
                - טלפון: השתמש תמיד במספר המזהה {sender}. אל תבקש מהלקוח מספר טלפון!
                - תלונות: אם הלקוח מתלונן, הוצא FINAL_COMPLAINT.
                
                פורמטים (בסוף ההודעה בלבד, בשורה נפרדת):
                FINAL_ORDER|{sender}|[שם]|[כתובת או 'איסוף עצמי']|[מוצרים]|[משלוח או איסוף עצמי]
                FINAL_COMPLAINT|{sender}|[שם]|[תיאור התלונה]
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
                            send_whatsapp(sender, "מצטער לשמוע צדיק. התלונה הועברה למנהל לטיפול מיידי! 🙏")
                            clear_history(sender)
                    elif "FINAL_ORDER|" in bot_reply:
                        parts = bot_reply.split("|")
                        if len(parts) >= 6:
                            oid = save_order(parts[2], parts[1], parts[3], parts[4], sender, parts[5])
                            send_whatsapp(sender, f"פיקס {parts[2]}! ההזמנה הועברה לאישור הבוס ⏳")
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
