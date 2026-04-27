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
    except Exception as e:
        print(f"Deduplication Error: {e}")
        return False 

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

# --- הפונקציה החדשה שמוחקת זיכרון אחרי סיום הזמנה ---
def clear_history(phone):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM conversation_history WHERE phone = %s", (phone,))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Clear History Error: {e}")

def get_inventory():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT name, price FROM products WHERE stock > 0")
        items = cur.fetchall()
        conn.close()
        if items: return ", ".join([f"{i[0]} ({i[1]}₪)" for i in items])
        return "אין סחורה כרגע"
    except: return "שגיאה בטעינת המלאי"

def save_order(name, phone, address, items, original_sender_id, update_note=""):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        final_address = f"{address} | WA_ID:{original_sender_id}"
        if update_note:
            final_address = f"⚠️ שים לב: {update_note} | " + final_address
            
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
                אתה "חיים", מוכר במכולת "המכולת של הצדיק".
                המלאי שלך להיום: {inventory}
                
                אופי וכללים:
                1. דבר בטבעיות, חברותיות, ומדי פעם שים אימוג'י רלוונטי (אבל לא להגזים).
                2. תמיכה בשפות: אם הלקוח מדבר שפה אחרת (כמו אנגלית או רוסית), תענה לו בשפה שלו. תמיד תהיה מנומס.
                3. אם הלקוח אומר מילות סיום או סירוב כמו "תודה", "לא", "סגור", "זהו", "לא תודה" - תענה לו בחביבות "בכיף, שיהיה יום מקסים!" ואל תיצור שום הזמנה.
                4. כדי לסגור הזמנה, חובה שיהיה לך: שם מלא, עיר, רחוב ומספר, ומה הוא רוצה לקנות. 
                
                איך מפיקים הזמנה (חשוב מאוד!):
                - לעולם אל תפיק הזמנה (FINAL_ORDER) על דעת עצמך, במיוחד אם הלקוח רק אמר "לא" לעוד מוצרים!
                - רק אם הלקוח נתן הרגע את כל הפרטים למשלוח ומחכה לסיום, תוסיף בשורה נפרדת בסוף:
                FINAL_ORDER|{sender}|[שם]|[רחוב ומספר, עיר]|[מוצרים]|[הערות למנהל]
                
                דוגמאות להערות למנהל:
                - רגיל: "רגיל"
                - עדכון: "הלקוח עדכן כתובת"
                """
                
                if client:
                    completion = client.chat.completions.create(
                        model="llama-3.3-70b-versatile",
                        messages=[{"role": "system", "content": system_prompt}] + history,
                        temperature=0.2, max_tokens=250
                    )
                    bot_reply = completion.choices[0].message.content.strip()
                    
                    if "FINAL_ORDER|" in bot_reply:
                        try:
                            order_line = [line for line in bot_reply.split('\n') if "FINAL_ORDER|" in line][0]
                            parts = order_line.split("|")
                            
                            if len(parts) >= 6:
                                name = parts[2].strip()
                                addr = parts[3].strip()
                                items = parts[4].strip()
                                update_note = parts[5].strip()
                                
                                admin_note = "הלקוח עדכן את הכתובת!" if "עדכן כתובת" in update_note else ""
                                
                                # שמירת ההזמנה
                                oid = save_order(name, sender, addr, items, sender, admin_note)
                                
                                # הודעה ללקוח בלי מספר ההזמנה!
                                final_msg = f"פיקס {name}! ההזמנה הועברה לאישור הבוס ⏳\n(נשלח אליך: {items} לכתובת: {addr})"
                                if admin_note:
                                    final_msg += "\n*שמתי לב שתיקנת את הכתובת, עדכנתי את המנהל!*"
                                    
                                send_whatsapp(sender, final_msg)
                                
                                # מחיקת הזיכרון כדי למנוע הזמנות שרשרת בהמשך!
                                clear_history(sender)
                                
                            else:
                                send_whatsapp(sender, "חסרים לי כמה פרטים. תוכל לוודא שנתת לי שם, עיר, רחוב ומוצרים?")
                                
                        except Exception as e:
                            print(f"Parsing error: {e}")
                            send_whatsapp(sender, "אופס, הייתה לי תקלה קטנה. תוכל לכתוב לי שוב מה תרצה ואיפה אתה גר (כולל עיר)?")
                    else:
                        clean_reply = "\n".join([line for line in bot_reply.split('\n') if "FINAL_ORDER|" not in line]).strip()
                        if clean_reply:
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
    # מסירים פה את התצוגה של מספר ההזמנה שנשלח מהדשבורד אם קיים
    msg_to_send = data.get('message')
    send_whatsapp(phone, msg_to_send)
    return jsonify({"status": "sent"}), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
