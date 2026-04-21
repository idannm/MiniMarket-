import os
import requests
import psycopg2
from flask import Flask, request, jsonify
from groq import Groq
import json
from datetime import datetime

app = Flask(__name__)

# --- הגדרות סביבה ---
DB_URL = os.environ.get("DB_URL")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "Idan1234")

client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# מאגר שיחות בזיכרון (כדי שהבוט יזכור מה נאמר קודם)
user_conversations = {}

# --- פונקציות מסד נתונים ---
def get_db_connection():
    return psycopg2.connect(DB_URL)

def is_message_processed(message_id):
    """בדיקה בטבלה שיצרנו למניעת כפילויות שמטא שולחת"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM processed_messages WHERE message_id = %s", (message_id,))
        exists = cur.fetchone() is not None
        if not exists:
            cur.execute("INSERT INTO processed_messages (message_id) VALUES (%s)", (message_id,))
            conn.commit()
        cur.close()
        conn.close()
        return exists
    except Exception as e:
        print(f"DB Error checking message: {e}")
        return False

def get_inventory():
    """משיכת המלאי העדכני ממסד הנתונים"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT name, price FROM products WHERE stock > 0 ORDER BY name")
        items = cur.fetchall()
        cur.close()
        conn.close()
        if items:
            return "\n".join([f"• {i[0]} - ₪{i[1]}" for i in items])
        return "המלאי כרגע ריק"
    except:
        return "שגיאה בטעינת המלאי"

def extract_and_save_order(chat_history, user_id):
    """המוח ששולף נתונים מהשיחה ושומר לאתר"""
    prompt = f"חלץ מהשיחה הבאה JSON בדיוק בפורמט הזה: {{'name': 'שם מלא', 'address': 'כתובת', 'items': 'רשימת מוצרים', 'total': מספר}}\n\nהשיחה:\n{chat_history}"
    try:
        res = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": "אתה מחלץ מידע. החזר אך ורק JSON תקין ללא שום טקסט נוסף לפני או אחרי."}, 
                {"role": "user", "content": prompt}
            ],
            temperature=0.1
        ).choices[0].message.content
        
        # ניקוי וחילוץ ה-JSON
        data = json.loads(res[res.find("{"):res.rfind("}")+1])
        
        # שמירה למסד נתונים
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO orders (customer_name, items, total_price, address, status) VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (data['name'], data['items'], data['total'], f"{data['address']} | טלפון: {user_id}", 'ממתין לאישור')
        )
        order_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        return True, order_id
    except Exception as e:
        print(f"Error saving order: {e}")
        return False, str(e)

# --- תקשורת לוואטסאפ ---
def send_whatsapp_message(to, text):
    """שליחת תשובה ללקוח לוואטסאפ"""
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
    requests.post(url, headers=headers, json=data)

# --- נקודות הקצה של השרת ---
@app.route('/webhook', methods=['GET'])
def verify():
    """אימות מול מטא"""
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge")
    return "Verification failed", 403

@app.route('/webhook', methods=['POST'])
def webhook():
    """קבלת הודעות, עיבוד עם חיים הבוט, ושליחת תשובה"""
    data = request.get_json()
    if not data:
        return "ok", 200

    try:
        if 'entry' in data and 'changes' in data['entry'][0]:
            value = data['entry'][0]['changes'][0]['value']
            
            if 'messages' in value:
                msg = value['messages'][0]
                if msg['type'] != 'text':
                    return "ok", 200 # נתעלם מתמונות/סטיקרים כרגע
                
                sender = msg['from']
                msg_id = msg['id']
                user_text = msg['text']['body']
                
                # מניעת כפילויות מטא
                if is_message_processed(msg_id): 
                    return "ok", 200
                
                # ניהול זיכרון - שומרים עד 8 הודעות אחרונות
                if sender not in user_conversations:
                    user_conversations[sender] = []
                user_conversations[sender].append({"role": "user", "content": user_text})
                if len(user_conversations[sender]) > 8:
                    user_conversations[sender] = user_conversations[sender][-8:]
                
                inventory = get_inventory()
                
                # האופי של חיים!
                system_prompt = f"""
אתה המוכר הווירטואלי של "המכולת של הצדיק". השם שלך הוא "חיים". 
אתה לא בוט רובוטי - אתה בן אדם חם, סבלני, ישראלי ואדיב מאוד שמכיר את כל הלקוחות.

המלאי המעודכן שלנו להיום (אל תמציא מוצרים שאין כאן!):
{inventory}

### 🎭 האופי וסגנון הדיבור שלך:
1. פתיחה חמה: תמיד תהיה שמח, תשתמש במילים כמו "אלוף", "צדיק", "בשמחה רבה", "אח יקר", "שפע".
2. אימוג'ים: תשתמש באימוג'ים שקשורים לאוכל ולשמחה (🍞, 🥛, 🎉, 🛒, 😊) אבל בטעם טוב.
3. שפה חברית אבל מקצועית: דבר בגובה העיניים, אבל תהיה ממוקד במטרה - לעזור ללקוח לקנות.

### 📜 חוקי הברזל של המכולת:
1. שקיפות מחירים: אם לקוח שואל על מוצר, תגיד מיד אם יש אותו ובאיזה מחיר (לפי המלאי בלבד).
2. חוסר במלאי: אם לקוח מבקש משהו שאין במלאי, תגיד שנגמר ותציע חלופה מהמלאי הקיים.
3. הגדלת מכירה (Upsell עדין): כשהלקוח מוסיף משהו, תשאל בטבעיות אם חסר לו עוד משהו.

### 🤝 תהליך סגירת ההזמנה (סופר קריטי):
כשהלקוח מראה סימנים שהוא סיים:
1. תן לו סיכום יפה של העגלה שלו עם הסכום הכולל.
2. תגיד לו שאתה צריך 3 פרטים זריזים למשלוח: שם מלא, כתובת מדויקת (רחוב ומספר בית), ומספר טלפון.
3. אתה **חייב** לוודא שיש לך את כל ה-3. אם חסר משהו, תשאל ספציפית על מה שחסר.
4. רק כאשר יש לך את **כל הפרטים במלואם**, תסיים את ההודעה שלך בדיוק עם המילה הזו באנגלית (ובלי שום טקסט אחריה): FINALIZE_ORDER

### 💡 דוגמאות לאיך אתה אמור לענות:
לקוח: "יש לכם חלב?"
חיים (אתה): "ברוך השם צדיק! בטח שיש, חלב עולה 6 שקלים 🥛. להוסיף לך להזמנה?"

לקוח: "זהו אחי, רק החלב."
חיים (אתה): "פיקס! סה"כ 6 שקלים. רק תרשום לי שם מלא, כתובת מדויקת וטלפון והמשלוח טס אליך! 🛵"
"""
                # קריאה ל-AI
                completion = client.chat.completions.create(
                    model="llama-3.1-8b-instant",
                    messages=[{"role": "system", "content": system_prompt}] + user_conversations[sender],
                    temperature=0.7,
                    max_tokens=500
                )
                bot_reply = completion.choices[0].message.content
                
                # שמירת תשובת הבוט בזיכרון
                user_conversations[sender].append({"role": "assistant", "content": bot_reply})
                
                # סגירת הזמנה אם ה-AI החליט שהכל מוכן
                if "FINALIZE_ORDER" in bot_reply:
                    history = "\n".join([f"{m['role']}: {m['content']}" for m in user_conversations[sender]])
                    success, res = extract_and_save_order(history, sender)
                    
                    if success:
                        bot_reply = bot_reply.replace("FINALIZE_ORDER", "").strip()
                        bot_reply += f"\n\n✅ הזמנה #{res} נשלחה בהצלחה למערכת וממתינה לאישור המנהל! ניצור קשר ברגע שהיא תאושר."
                        user_conversations[sender] = [] # מחיקת הזיכרון לקראת ההזמנה הבאה
                    else:
                        bot_reply = "אופס צדיק, חסרים לי כמה פרטים. תוכל לוודא שרשמת שם מלא, כתובת וטלפון?"

                # משלוח התשובה חזרה לוואטסאפ (תוך מחיקת מילת הקוד)
                clean_reply = bot_reply.replace("FINALIZE_ORDER", "").strip()
                send_whatsapp_message(sender, clean_reply)
            
    except Exception as e:
        print(f"Error in webhook: {e}")
        
    return "ok", 200

# נתיב למנהל לשלוח הודעות (למשל כשההזמנה מאושרת בדשבורד)
@app.route('/send_update', methods=['POST'])
def send_update():
    try:
        data = request.get_json()
        phone = data.get('phone')
        message = data.get('message')
        
        if phone and message:
            send_whatsapp_message(phone, message)
            return jsonify({"status": "sent"}), 200
        return jsonify({"error": "Missing phone or message"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/')
def home():
    return jsonify({"status": "running", "bot": "Chaim the Tzadik"})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)
