import os
import requests
import psycopg2
from flask import Flask, request, jsonify
from groq import Groq
import json

app = Flask(__name__)

# הגדרות סביבה
DB_URL = os.environ.get("DB_URL")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "my_verify_token")

# בדיקה שכל המשתנים קיימים
if not all([DB_URL, GROQ_API_KEY, WHATSAPP_TOKEN, PHONE_NUMBER_ID]):
    print("⚠️ חסרים משתני סביבה חשובים!")
    print(f"DB_URL: {'✓' if DB_URL else '✗'}")
    print(f"GROQ_API_KEY: {'✓' if GROQ_API_KEY else '✗'}")
    print(f"WHATSAPP_TOKEN: {'✓' if WHATSAPP_TOKEN else '✗'}")
    print(f"PHONE_NUMBER_ID: {'✓' if PHONE_NUMBER_ID else '✗'}")

client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

def get_db_connection():
    """חיבור למסד נתונים"""
    return psycopg2.connect(DB_URL)

def send_whatsapp_message(to, text):
    """שליחת הודעת WhatsApp עם טיפול משופר"""
    try:
        # הסרת 0 מתחילת המספר אם קיים והוספת קידומת ישראל
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
        
        print(f"📤 שולח הודעה ל-{to}")
        print(f"📝 תוכן: {text[:50]}...")
        
        response = requests.post(url, headers=headers, json=data, timeout=10)
        
        print(f"📊 סטטוס: {response.status_code}")
        
        if response.status_code == 200:
            print(f"✅ ההודעה נשלחה בהצלחה!")
            return True
        else:
            print(f"❌ שגיאה בשליחה: {response.text}")
            return False
            
    except Exception as e:
        print(f"❌ חריגה בשליחת הודעה: {e}")
        return False

def get_inventory():
    """קבלת מלאי מהמסד נתונים"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT name, price FROM products WHERE stock > 0 ORDER BY name")
        items = cur.fetchall()
        cur.close()
        conn.close()
        
        if items:
            return "\n".join([f"• {item[0]} - ₪{item[1]}" for item in items])
        else:
            return "אין מוצרים זמינים כרגע"
    except Exception as e:
        print(f"❌ שגיאה בטעינת מלאי: {e}")
        return "שגיאה בטעינת המלאי"

def save_order(customer_phone, details):
    """שמירת הזמנה במסד נתונים"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # בדיקה אם הטבלה תומכת בשדות אלו
        cur.execute("""
            INSERT INTO orders (customer_name, items, total_price, address, status) 
            VALUES (%s, %s, %s, %s, %s) RETURNING id
        """, (
            customer_phone,  # ישתמש כ-customer_name
            details,         # פרטי ההזמנה
            0,              # סכום זמני
            f"WhatsApp: {customer_phone}",  # כתובת עם מספר טלפון
            'ממתין לאישור'
        ))
        
        order_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        
        print(f"✅ הזמנה #{order_id} נשמרה בהצלחה!")
        return order_id
        
    except Exception as e:
        print(f"❌ שגיאה בשמירת הזמנה: {e}")
        return None

@app.route('/')
def home():
    """דף בית לבדיקה"""
    return jsonify({
        "status": "running",
        "service": "WhatsApp Bot",
        "version": "2.0"
    })

@app.route('/send_update', methods=['POST'])
def send_update():
    """נקודת קצה לשליחת עדכונים (משמש את Streamlit)"""
    try:
        data = request.get_json()
        phone = data.get('phone')
        message = data.get('message')
        
        if not phone or not message:
            return jsonify({"error": "Missing phone or message"}), 400
        
        success = send_whatsapp_message(phone, message)
        
        if success:
            return jsonify({"status": "sent"}), 200
        else:
            return jsonify({"error": "Failed to send"}), 500
            
    except Exception as e:
        print(f"❌ שגיאה ב-send_update: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/webhook', methods=['GET'])
def verify():
    """אימות webhook"""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    
    print(f"🔐 ניסיון אימות webhook")
    print(f"Mode: {mode}, Token: {token}")
    
    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("✅ Webhook verified!")
        return challenge, 200
    else:
        print("❌ Verification failed!")
        return "Failed", 403

@app.route('/webhook', methods=['POST'])
def webhook():
    """קבלת הודעות מ-WhatsApp"""
    try:
        data = request.get_json()
        print("=" * 50)
        print(f"📨 התקבלה הודעה חדשה:")
        print(json.dumps(data, indent=2, ensure_ascii=False))
        print("=" * 50)
        
        # בדיקה שיש entry
        if 'entry' not in data:
            print("⚠️ אין entry בנתונים")
            return jsonify({"status": "no entry"}), 200
        
        for entry in data['entry']:
            for change in entry.get('changes', []):
                value = change.get('value', {})
                
                # בדיקה שיש הודעות
                if 'messages' not in value:
                    print("⚠️ אין messages בנתונים")
                    continue
                
                for message in value['messages']:
                    # קבלת פרטי המשתמש וההודעה
                    sender = message.get('from')
                    message_type = message.get('type')
                    
                    print(f"👤 שולח: {sender}")
                    print(f"📋 סוג הודעה: {message_type}")
                    
                    # תמיכה רק בהודעות טקסט
                    if message_type != 'text':
                        print(f"⚠️ סוג הודעה לא נתמך: {message_type}")
                        send_whatsapp_message(sender, "אני יכול לענות רק להודעות טקסט 😊")
                        continue
                    
                    user_text = message.get('text', {}).get('body', '')
                    print(f"💬 תוכן: {user_text}")
                    
                    # טעינת מלאי
                    print("📦 טוען מלאי...")
                    inventory = get_inventory()
                    print(f"✅ מלאי נטען: {len(inventory)} תווים")
                    
                    # יצירת תשובה עם AI
                    if client:
                        print("🤖 מכין תשובה עם AI...")
                        
                        system_prompt = f"""
אתה עוזר חמוד ונחמד במכולת. תמיד תהיה חביב וסבלני.

המוצרים הזמינים:
{inventory}

הנחיות:
1. ענה בעברית פשוטה וקצרה
2. אם שואלים על מחיר - ספר מיד
3. אם מזמינים - אשר והסבר שהמנהל יצטרך לאשר
4. אם מוצר לא קיים - הצע חלופה
5. תמיד היה חביב ושמח לעזור

דוגמאות:
לקוח: "כמה עולה חלב?"
אתה: "חלב עולה 6 ש״ח 🥛 רוצה להזמין?"

לקוח: "אני רוצה חלב"
אתה: "מעולה! חלב זה 6 ש״ח 🥛 עוד משהו?"
"""
                        
                        try:
                            completion = client.chat.completions.create(
                                model="llama-3.1-8b-instant",
                                messages=[
                                    {"role": "system", "content": system_prompt},
                                    {"role": "user", "content": user_text}
                                ],
                                temperature=0.7,
                                max_tokens=300
                            )
                            
                            bot_reply = completion.choices[0].message.content
                            print(f"🤖 תשובת AI: {bot_reply}")
                            
                        except Exception as e:
                            print(f"❌ שגיאה ב-AI: {e}")
                            bot_reply = "סליחה, יש לי בעיה טכנית. נסה שוב בעוד רגע 😊"
                    
                    else:
                        bot_reply = "הבוט פועל! המלאי שלנו:\n" + inventory
                    
                    # זיהוי הזמנה ושמירה
                    if any(word in user_text.lower() for word in ["להזמין", "הזמנה", "אני רוצה", "תן לי"]):
                        print("🛒 מזהה כהזמנה - שומר במסד נתונים...")
                        order_id = save_order(sender, user_text)
                        if order_id:
                            bot_reply += f"\n\n✅ ההזמנה שלך נרשמה (מספר #{order_id}) וממתינה לאישור המנהל!"
                    
                    # שליחת התשובה
                    print("📤 שולח תשובה ללקוח...")
                    send_whatsapp_message(sender, bot_reply)
                    print("✅ תהליך הסתיים בהצלחה!")
        
        return jsonify({"status": "ok"}), 200
        
    except Exception as e:
        print("=" * 50)
        print(f"💥 שגיאה חמורה: {e}")
        print("=" * 50)
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    """בדיקת תקינות"""
    try:
        # בדיקת חיבור למסד נתונים
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        conn.close()
        
        return jsonify({
            "status": "healthy",
            "database": "connected",
            "groq": "initialized" if client else "missing",
            "whatsapp": "configured" if WHATSAPP_TOKEN else "missing"
        }), 200
    except Exception as e:
        return jsonify({
            "status": "unhealthy",
            "error": str(e)
        }), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    print("=" * 50)
    print(f"🚀 Starting WhatsApp Bot on port {port}")
    print("=" * 50)
    app.run(host='0.0.0.0', port=port, debug=False)
