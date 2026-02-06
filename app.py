from flask import Flask, request, jsonify
from groq import Groq
import psycopg2
import os
import json
from datetime import datetime

app = Flask(__name__)

# --- הגדרות ---
DB_URL = os.environ.get('DB_URL', "postgresql://neondb_owner:npg_0VQiZe7Dmulp@ep-still-morning-ah32xqpq-pooler.c-3.us-east-1.aws.neon.tech/neondb?sslmode=require")
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', "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")

client = Groq(api_key=GROQ_API_KEY)

# מאגר שיחות בזיכרון
user_conversations = {}

# --- פונקציות עזר ---
@app.route('/webhook', methods=['GET'])

def verify():
    # שלב האימות מול פייסבוק
    token = os.environ.get("VERIFY_TOKEN")
    if request.args.get("hub.mode") == "subscribe" and request.args.get("hub.verify_token") == token:
        return request.args.get("hub.challenge")
    return "Verification failed", 403

@app.route('/webhook', methods=['POST'])

def webhook():
    # כאן מקבלים את ההודעה האמיתית מוואטסאפ
    data = request.get_json()
    # צריך להוסיף כאן קוד ששולח את ההודעה ל-process_message
    return "ok", 200

def get_db_connection():
    return psycopg2.connect(DB_URL)

def validate_address(address):
    """בדיקת תקינות כתובת"""
    if len(address) < 5:
        return False, "הכתובת קצרה מדי"
    
    has_letter = any(c.isalpha() for c in address)
    has_number = any(c.isdigit() for c in address)
    
    if not has_letter or not has_number:
        return False, "נא להזין כתובת מלאה הכוללת שם רחוב ומספר בית"
    
    return True, address

def validate_name(name):
    """בדיקת תקינות שם"""
    if len(name) < 2:
        return False, "השם קצר מדי"
    
    words = name.split()
    if len(words) < 2:
        return False, "נא להזין שם מלא (שם פרטי ושם משפחה)"
    
    if any(len(word) < 2 for word in words):
        return False, "כל חלק בשם חייב להכיל לפחות 2 תווים"
    
    return True, name

def get_inventory():
    """קבלת רשימת מוצרים זמינים"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT name, price FROM products WHERE stock > 0 ORDER BY name")
        products = cur.fetchall()
        cur.close()
        conn.close()
        
        if products:
            return "\n".join([f"• {p[0]} - ₪{p[1]}" for p in products])
        return "אין מוצרים זמינים כרגע"
    except:
        return "שגיאה בטעינת המלאי"

def save_order_to_db(chat_history, user_id):
    """שמירת הזמנה למסד הנתונים"""
    prompt = f"""
    קרא את השיחה הבאה וחלץ את המידע הבא בדיוק:
    
    {chat_history}
    
    החזר JSON בפורמט הזה בדיוק (ללא טקסט נוסף):
    {{
        "name": "שם הלקוח המלא",
        "address": "הכתובת המלאה",
        "items": "רשימת כל המוצרים שהוזמנו",
        "total": הסכום_הכולל_כמספר
    }}
    """
    try:
        res = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": "אתה מחלץ מידע מדויק. החזר רק JSON תקין."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.1,
            max_tokens=500
        ).choices[0].message.content.strip()
        
        if "{" in res and "}" in res:
            res = res[res.find("{"):res.rfind("}")+1]
            data = json.loads(res)
            
            name = str(data.get('name', '')).strip()
            address = str(data.get('address', '')).strip()
            items = str(data.get('items', '')).strip()
            total = float(data.get('total', 0))
            
            # ולידציה
            errors = []
            
            name_valid, name_msg = validate_name(name)
            if not name_valid:
                errors.append(f"❌ שם: {name_msg}")
            
            address_valid, address_msg = validate_address(address)
            if not address_valid:
                errors.append(f"❌ כתובת: {address_msg}")
            
            if not items or len(items) < 3:
                errors.append("❌ פריטים: לא נמצאו פריטים בהזמנה")
            
            if total <= 0:
                errors.append("❌ סכום: הסכום חייב להיות גדול מ-0")
            
            if errors:
                return False, "\n".join(errors)
            
            # שמירה למסד נתונים
            full_info = f"{address} | User ID: {user_id}"
            
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO orders (customer_name, items, total_price, address, status) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (name, items, total, full_info, 'ממתין לאישור')
            )
            order_id = cur.fetchone()[0]
            conn.commit()
            cur.close()
            conn.close()
            
            return True, order_id
            
    except Exception as e:
        print(f"Error saving order: {e}")
        return False, str(e)

def process_message(user_id, message_text):
    """עיבוד הודעה והחזרת תשובה"""
    # אתחול שיחה חדשה אם צריך
    if user_id not in user_conversations:
        user_conversations[user_id] = {
            'messages': [],
            'order_id': None
        }
    
    conversation = user_conversations[user_id]
    
    # בדיקת סטטוס הזמנה קיימת
    if conversation['order_id']:
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute(
                "SELECT status, delivery_time, cancellation_reason FROM orders WHERE id=%s",
                (conversation['order_id'],)
            )
            result = cur.fetchone()
            cur.close()
            conn.close()
            
            if result:
                if result[0] == 'אושר':
                    response = f"🎉 ההזמנה שלך אושרה!\n⏰ זמן הגעה משוער: {result[1]}\n\n✨ ההזמנה בהכנה ובדרך אליך!"
                    # איפוס השיחה
                    user_conversations[user_id] = {'messages': [], 'order_id': None}
                    return response
                elif result[0] == 'בוטל':
                    reason = result[2] if result[2] else "לא צוין"
                    response = f"😔 ההזמנה שלך בוטלה\nסיבה: {reason}\n\nאפשר להתחיל הזמנה חדשה!"
                    # איפוס השיחה
                    user_conversations[user_id] = {'messages': [], 'order_id': None}
                    return response
        except:
            pass
    
    # הוספת ההודעה להיסטוריה
    conversation['messages'].append({"role": "user", "content": message_text})
    
    # קבלת המלאי
    inventory = get_inventory()
    
    # הכנת פרומפט
    system_prompt = f"""
אתה עוזר חמוד ונחמד במכולת 'המכולת של הצדיק'. תמיד תהיה חביב, סבלני ועוזר.

המוצרים שיש לנו במכולת:
{inventory}

איך להתנהג:
1. תהיה טבעי ונחמד, כמו חבר
2. כשלקוח שואל על מחיר - ספר לו ישר
3. כשלקוח מזמין מוצר - ספר מחיר ושאל אם רוצה עוד
4. אם מוצר לא קיים - תגיד "מצטער, אין לנו את זה. יש לנו [הצע חלופה]"
5. כשלקוח אומר "זה הכל" - תן סיכום ובקש פרטים
6. השתמש בעברית פשוטה, ללא קיצורים

חשוב מאוד - בקש פרטים מלאים:
- שם מלא (שם פרטי ושם משפחה)
- כתובת מלאה (רחוב ומספר בית)

דוגמאות:
לקוח: "כמה עולה לחם?"
אתה: "לחם עולה 8.5 ש״ח 🍞"

לקוח: "אני רוצה חלב"
אתה: "בטח! חלב זה 6 ש״ח 🥛 רוצה להוסיף עוד משהו?"

לקוח: "זה הכל"
אתה: "מעולה! 
🛒 הזמנת: חלב
💰 סה״כ: 6 ש״ח

עכשיו רק צריך ממך:
👤 שם מלא (שם פרטי ושם משפחה)
📍 כתובת מלאה למשלוח (רחוב ומספר בית)"

חשוב:
- דבר ישירות ובפשטות
- רק אחרי שיש לך שם מלא וכתובת מלאה - כתוב בסוף: FINALIZE_ORDER
- אם חסרים פרטים - בקש אותם שוב
"""
    
    try:
        # קריאה ל-AI
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": system_prompt}
            ] + conversation['messages'],
            temperature=0.7,
            max_tokens=800
        ).choices[0].message.content
        
        # הוספת התשובה להיסטוריה
        conversation['messages'].append({"role": "assistant", "content": response})
        
        # בדיקה אם צריך לשמור הזמנה
        if "FINALIZE_ORDER" in response:
            clean_response = response.replace("FINALIZE_ORDER", "").strip()
            
            # שמירת ההזמנה
            history = "\n".join([f"{m['role']}: {m['content']}" for m in conversation['messages']])
            success, result = save_order_to_db(history, user_id)
            
            if success:
                conversation['order_id'] = result
                return clean_response + "\n\n🎉 ההזמנה נשלחה בהצלחה!\n⏳ ההזמנה שלך ממתינה לאישור המנהל."
            else:
                return f"⚠️ יש בעיות בפרטי ההזמנה:\n\n{result}\n\n💡 בבקשה תקן את הפרטים ונסה שוב"
        
        return response.replace("FINALIZE_ORDER", "").strip()
        
    except Exception as e:
        print(f"Error processing message: {e}")
        return "❌ מצטער, הייתה שגיאה. נסה שוב בבקשה."

# --- נקודות קצה (Endpoints) ---

@app.route('/')
def home():
    return jsonify({
        "status": "running",
        "service": "Grocery Bot API",
        "version": "1.0"
    })

@app.route('/chat', methods=['POST'])
def chat():
    """נקודת קצה לשליחת הודעות וקבלת תשובות"""
    try:
        data = request.get_json()
        user_id = data.get('user_id')
        message = data.get('message')
        
        if not user_id or not message:
            return jsonify({"error": "Missing user_id or message"}), 400
        
        # עיבוד ההודעה
        response = process_message(user_id, message)
        
        return jsonify({
            "status": "success",
            "response": response
        }), 200
        
    except Exception as e:
        print(f"Error in chat: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    """בדיקת תקינות השרת"""
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
            "timestamp": datetime.now().isoformat()
        }), 200
    except Exception as e:
        return jsonify({
            "status": "unhealthy",
            "error": str(e)
        }), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
