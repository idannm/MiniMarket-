import os
import requests
import psycopg2
from flask import Flask, request, jsonify
from groq import Groq
import json

app = Flask(__name__)

# שליפת משתני סביבה
DB_URL = os.environ.get("DB_URL")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "my_verify_token")

client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

def get_db_connection():
    return psycopg2.connect(DB_URL)

def send_whatsapp_message(to, text):
    """שליחת הודעה לוואטסאפ (משמש גם את הבוט וגם את הדשבורד)"""
    try:
        if to.startswith("0"): to = "972" + to[1:]
        
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
        r = requests.post(url, headers=headers, json=data)
        return r.status_code == 200
    except Exception as e:
        print(f"Error sending msg: {e}")
        return False

def get_inventory_text():
    """שליפת המלאי כטקסט פשוט"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT name, price FROM products WHERE stock > 0")
        items = cur.fetchall()
        cur.close()
        conn.close()
        if items:
            return ", ".join([f"{i[0]} - {i[1]}₪" for i in items])
        return "אין מוצרים במלאי כרגע."
    except:
        return "שגיאה בטעינת המלאי."

def save_order(name, phone, address, items_summary):
    """שמירת הזמנה מלאה"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO orders (customer_name, items, status, total_price, address) 
            VALUES (%s, %s, %s, %s, %s) RETURNING id
        """, (name, items_summary, 'ממתין לאישור', 0, f"{address} | טלפון: {phone}"))
        
        new_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        return new_id
    except Exception as e:
        print(f"Error saving order: {e}")
        return None

# --- נקודת קצה לדשבורד (כדי לשלוח הודעות ללקוח) ---
@app.route('/send_update', methods=['POST'])
def send_update_from_dashboard():
    data = request.json
    phone = data.get('phone')
    message = data.get('message')
    if send_whatsapp_message(phone, message):
        return jsonify({"status": "sent"}), 200
    return jsonify({"error": "failed"}), 500

@app.route('/webhook', methods=['GET'])
def verify():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge")
    return "Error", 403

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    try:
        # בדיקה אם יש הודעה חדשה
        if 'messages' in data['entry'][0]['changes'][0]['value']:
            message = data['entry'][0]['changes'][0]['value']['messages'][0]
            sender = message['from']
            text = message['text']['body']
            
            # 1. טעינת המלאי
            inventory = get_inventory_text()

            # 2. הנחיה ל-AI (System Prompt) - קשוחה ומדויקת
            # הטריק: אומרים ל-AI להוציא פקודה מיוחדת SAVE_ORDER רק כשיש הכל
            system_prompt = f"""
            אתה מוכר במכולת. שמך "בוטי".
            המלאי הקיים: {inventory}

            הנחיות התנהגות:
            1. דבר קצר, ענייני ובעברית בלבד. בלי שטויות ובלי להמציא.
            2. אם לקוח מבקש משהו שאין - תגיד שאין.
            3. כדי לסגור הזמנה, אתה חייב לקבל מהלקוח: **רשימת מוצרים סופית, שם מלא, וכתובת**.
            4. אם חסר משהו (למשל יש מוצרים אבל אין כתובת) - תשאל את הלקוח: "חסרה לי כתובת ושם למשלוח".
            
            פקודה מיוחדת לסגירה:
            רק כאשר יש לך את כל הפרטים (מוצרים + שם + כתובת), אל תענה מלל רגיל אלא כתוב רק את השורה הבאה:
            SAVE_ORDER | Name: [שם הלקוח] | Address: [כתובת הלקוח] | Items: [סיכום המוצרים]

            דוגמה לשיחה תקינה:
            לקוח: אני רוצה חלב ולחם.
            אתה: בשמחה. לאן לשלוח ומה שמך?
            לקוח: דוד, רחוב הרצל 1.
            אתה (פלט נסתר): SAVE_ORDER | Name: דוד | Address: רחוב הרצל 1 | Items: חלב, לחם
            """

            bot_reply = ""
            
            # שליחה ל-Groq
            if client:
                chat = client.chat.completions.create(
                    model="llama-3.1-8b-instant",
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": text} # שים לב: אין כאן היסטוריה מלאה, הבוט מסתמך על ההודעה הנוכחית.
                        # אם רוצים היסטוריה, צריך לשמור ב-DB ולטעון. לבינתיים, נבקש מהלקוח לרשום הכל בסוף.
                    ],
                    temperature=0.1, # יצירתיות נמוכה - מונע הזיות
                    max_tokens=200
                )
                ai_response = chat.choices[0].message.content.strip()

                # 3. זיהוי אם הבוט החליט לשמור הזמנה
                if ai_response.startswith("SAVE_ORDER"):
                    try:
                        # פירוק התשובה (Parsing)
                        parts = ai_response.split("|")
                        name = parts[1].split(":")[1].strip()
                        address = parts[2].split(":")[1].strip()
                        items = parts[3].split(":")[1].strip()
                        
                        # שמירה לדאטהבייס
                        order_id = save_order(name, sender, address, items)
                        
                        if order_id:
                            bot_reply = f"תודה {name}, ההזמנה שלך (מספר {order_id}) התקבלה! נעדכן כשהיא תאושר."
                        else:
                            bot_reply = "הייתה תקלה בשמירת ההזמנה. נסה שוב."
                            
                    except Exception as e:
                        print(f"Error parsing save command: {e}")
                        bot_reply = "סליחה, לא הצלחתי להבין את הפרטים. אנא כתוב שוב את ההזמנה עם שם וכתובת."
                else:
                    # סתם תשובה רגילה של הבוט
                    bot_reply = ai_response

            # שליחת התשובה ללקוח
            send_whatsapp_message(sender, bot_reply)

    except Exception as e:
        print(f"Main Error: {e}")

    return "ok", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
