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
    """בדיקה ושמירה אטומית - מונע עיבוד כפול"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # ננסה להכניס - אם כבר קיים יזרק שגיאה
        cur.execute(
            "INSERT INTO processed_messages (message_id) VALUES (%s) ON CONFLICT (message_id) DO NOTHING RETURNING id",
            (message_id,)
        )
        result = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
        
        # אם החזיר None = ההודעה כבר עובדה
        return result is None
        
    except:
        return True  # במקרה של שגיאה - נניח שעובד

def save_message(phone, role, content):
    """שמירת הודעה"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO conversation_history (phone, role, content) VALUES (%s, %s, %s) RETURNING id", 
            (phone, role, content)
        )
        new_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        return new_id
    except:
        return None

def get_history(phone, limit=8):
    """קבלת היסטוריה - רק 8 הודעות אחרונות"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT role, content FROM conversation_history WHERE phone = %s ORDER BY created_at DESC LIMIT %s", 
            (phone, limit)
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [{"role": r[0], "content": r[1]} for r in rows][::-1]
    except:
        return []

def clear_history(phone):
    """מחיקת היסטוריה"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM conversation_history WHERE phone = %s", (phone,))
        conn.commit()
        cur.close()
        conn.close()
    except:
        pass

def get_inventory():
    """קבלת מלאי"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT name, price FROM products WHERE stock > 0 ORDER BY name")
        items = cur.fetchall()
        cur.close()
        conn.close()
        return ", ".join([f"{i[0]} ({i[1]}₪)" for i in items]) if items else "המלאי כרגע ריק"
    except:
        return "שגיאה בטעינת המלאי"

def save_order(name, phone, address, items, original_sender_id, order_type):
    """שמירת הזמנה"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        final_address = f"{address} | WA_ID:{original_sender_id}"
        cur.execute(
            "INSERT INTO orders (customer_name, items, status, total_price, address, order_type) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (name, items, 'ממתין לאישור', 0, final_address, order_type)
        )
        new_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        return new_id
    except Exception as e:
        print(f"❌ Error saving order: {e}")
        return None

def save_complaint(name, phone, description):
    """שמירת תלונה"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO complaints (customer_name, phone, description) VALUES (%s, %s, %s)",
            (name, phone, description)
        )
        conn.commit()
        cur.close()
        conn.close()
        return True
    except:
        return False

def send_whatsapp(to, text):
    """שליחת הודעת WhatsApp"""
    try:
        original_phone = to
        
        # המרת מספר ישראלי
        if to.startswith("0"):
            to = "972" + to[1:]
        elif not to.startswith("972"):
            to = "972" + to
        
        url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
        headers = {
            "Authorization": f"Bearer {WHATSAPP_TOKEN}",
            "Content-Type": "application/json"
        }
        data = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text}
        }
        
        response = requests.post(url, headers=headers, json=data, timeout=10)
        
        if response.status_code == 200:
            save_message(original_phone, "assistant", text)
            print(f"✅ נשלח ל-{to}")
            return True
        else:
            print(f"❌ שגיאה בשליחה: {response.status_code}")
            return False
            
    except Exception as e:
        print(f"❌ שגיאה: {e}")
        return False

@app.route('/webhook', methods=['GET'])
def verify_webhook():
    """אימות Webhook"""
    mode = request.args.get('hub.mode')
    token = request.args.get('hub.verify_token')
    challenge = request.args.get('hub.challenge')
    
    if mode == 'subscribe' and token == VERIFY_TOKEN:
        print("✅ Webhook verified")
        return challenge, 200
    
    print("❌ Verification failed")
    return 'Forbidden', 403

@app.route('/webhook', methods=['POST'])
def receive_message():
    """קבלת הודעות מ-WhatsApp"""
    try:
        data = request.json
        
        if 'entry' not in data:
            return "ok", 200
        
        for entry in data['entry']:
            for change in entry.get('changes', []):
                value = change.get('value', {})
                
                if 'messages' not in value:
                    continue
                
                for msg in value['messages']:
                    # בדיקת סוג הודעה
                    if msg.get('type') != 'text':
                        continue
                    
                    msg_id = msg['id']
                    sender = msg['from']
                    text = msg['text']['body']
                    
                    # ✅ מניעת עיבוד כפול - קריטי!
                    if is_message_processed(msg_id):
                        print(f"⚠️ הודעה {msg_id} כבר עובדה - מדלג")
                        continue
                    
                    print(f"📨 הודעה חדשה מ-{sender}: {text}")
                    
                    # שמירת הודעת המשתמש
                    save_message(sender, "user", text)
                    
                    # קבלת היסטוריה ומלאי
                    history = get_history(sender, limit=8)
                    inventory = get_inventory()
                    
                    # בניית prompt חכם
                    system_prompt = f"""אתה "חיים", המוכר האדיב במכולת "המכולת של הצדיק".

המלאי הזמין כרגע: {inventory}

חוקים קריטיים:
1. ענה בעברית פשוטה וטבעית - אל תהיה רובוטי!
2. קרא את כל ההודעות האחרונות של הלקוח וענה על הכל בתשובה אחת.
3. אל תחזור על עצמך - אם כבר אמרת משהו, אל תאמר שוב.

🚨 אם הלקוח כועס/מתלונן (מילים כמו: מגעיל, רקוב, קרוע, זבל):
- התנצל מיד ואמפתי
- אל תציע לו קניות!
- כתוב בשורה נפרדת: FINAL_COMPLAINT|{sender}|[שם או 'לקוח']|[תיאור קצר]

🛒 אם הלקוח קונה (מצב רגיל):
שלב 1 - בחירת מוצרים: שאל "תרצה להוסיף עוד משהו?" רק אחרי שהוא בחר משהו.
שלב 2 - סיום: כשהלקוח אומר "לא/זהו/סיימתי" → שאל: "משלוח 🛵 או איסוף 🛒?"
שלב 3 - פרטים:
  - משלוח: בקש שם מלא + עיר + רחוב ומספר
  - איסוף: רק שם מלא
שלב 4 - סגירה: רק כשיש את כל הפרטים, כתוב:
FINAL_ORDER|{sender}|[שם מלא]|[כתובת או 'איסוף']|[רשימת מוצרים]|[משלוח/איסוף]

דוגמאות:
לקוח: "אני רוצה לחם"
אתה: "מעולה! לחם זה 3.50₪. תרצה להוסיף עוד משהו?"

לקוח: "לא תודה"
אתה: "סבבה! משלוח 🛵 או איסוף 🛒?"

לקוח: "משלוח"
אתה: "בשמחה! מה השם המלא שלך?"

היה טבעי, חכם ומהיר!"""

                    # קריאה ל-AI
                    if client:
                        try:
                            completion = client.chat.completions.create(
                                model="llama-3.3-70b-versatile",
                                messages=[
                                    {"role": "system", "content": system_prompt}
                                ] + history,
                                temperature=0.3,
                                max_tokens=250
                            )
                            
                            bot_reply = completion.choices[0].message.content.strip()
                            print(f"🤖 תשובת AI: {bot_reply}")
                            
                            # טיפול בתלונה
                            if "FINAL_COMPLAINT|" in bot_reply:
                                parts = bot_reply.split("|")
                                if len(parts) >= 4:
                                    clean_msg = bot_reply.split("FINAL_COMPLAINT|")[0].strip()
                                    if clean_msg:
                                        send_whatsapp(sender, clean_msg)
                                    
                                    save_complaint(parts[2], parts[1], parts[3])
                                    send_whatsapp(sender, "העברתי את התלונה ישירות לבוס לבדיקה דחופה! 🤕")
                                    clear_history(sender)
                            
                            # טיפול בהזמנה
                            elif "FINAL_ORDER|" in bot_reply:
                                parts = bot_reply.split("|")
                                if len(parts) >= 6:
                                    clean_msg = bot_reply.split("FINAL_ORDER|")[0].strip()
                                    
                                    name = parts[2].strip()
                                    address = parts[3].strip()
                                    items = parts[4].strip()
                                    order_type = parts[5].strip()
                                    
                                    # ולידציה - שם לא יכול להיות ריק
                                    if not name or len(name) < 2 or "שם" in name.lower():
                                        send_whatsapp(sender, "אופס, לא קלטתי את השם. איך קוראים לך? (שם מלא בבקשה)")
                                    else:
                                        order_id = save_order(name, sender, address, items, sender, order_type)
                                        
                                        if order_id:
                                            if "איסוף" in order_type.lower():
                                                final_msg = f"פרפקט {name}! ההזמנה #{order_id} התקבלה 📦\n\nמתחילים לארוז ותקבל הודעה מתי לבוא 🛒"
                                            else:
                                                final_msg = f"יופי {name}! ההזמנה #{order_id} הועברה לבוס ⏳\n\nמיד נעדכן כשהמשלוח ייצא 🛵"
                                            
                                            send_whatsapp(sender, final_msg)
                                            clear_history(sender)
                                        else:
                                            send_whatsapp(sender, "אופס, הייתה בעיה טכנית. נסה שוב? 🙏")
                            
                            # תשובה רגילה
                            else:
                                clean_reply = bot_reply.replace("FINAL_ORDER", "").replace("FINAL_COMPLAINT", "").strip()
                                if clean_reply:
                                    send_whatsapp(sender, clean_reply)
                        
                        except Exception as e:
                            print(f"❌ AI Error: {e}")
                            send_whatsapp(sender, "סליחה, יש לי בעיה טכנית קטנה. נסה שוב בעוד שנייה 😊")
                    
                    else:
                        send_whatsapp(sender, f"שלום! המלאי שלנו:\n{inventory}")
        
        return "EVENT_RECEIVED", 200
        
    except Exception as e:
        print(f"❌ Webhook Error: {e}")
        return "EVENT_RECEIVED", 200

@app.route('/send_update', methods=['POST'])
def send_update():
    """שליחת עדכון מהדאשבורד"""
    auth = request.headers.get('X-Internal-Secret')
    if auth != INTERNAL_SECRET:
        return jsonify({"error": "Unauthorized"}), 401
    
    data = request.json
    phone = str(data.get('phone')).strip()
    message = data.get('message')
    
    success = send_whatsapp(phone, message)
    
    if success:
        return jsonify({"status": "sent"}), 200
    else:
        return jsonify({"error": "Failed"}), 500

@app.route('/')
def home():
    """בדיקת תקינות"""
    return jsonify({
        "status": "running",
        "service": "WhatsApp Bot - המכולת של הצדיק",
        "version": "3.0"
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    print("=" * 50)
    print(f"🚀 Bot Started on port {port}")
    print("=" * 50)
    app.run(host='0.0.0.0', port=port, debug=False)
