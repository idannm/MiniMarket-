import os
import json
import requests
import psycopg2
from flask import Flask, request, jsonify
from groq import Groq

app = Flask(__name__)

DB_URL = os.environ.get("DB_URL")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "my_verify_token")

client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

user_conversations = {}

def get_db_connection():
    return psycopg2.connect(DB_URL)

def is_message_processed(message_id):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM processed_messages WHERE message_id = %s", (message_id,))
        exists = cur.fetchone() is not None
        cur.close()
        conn.close()
        return exists
    except:
        return False

def mark_message_processed(message_id):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO processed_messages (message_id) VALUES (%s) ON CONFLICT DO NOTHING", (message_id,))
        conn.commit()
        cur.close()
        conn.close()
    except:
        pass

def send_whatsapp_message(to, text):
    try:
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
        return response.status_code == 200
    except:
        return False

def get_inventory():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT name, price, stock FROM products WHERE stock > 0 ORDER BY name")
        items = cur.fetchall()
        cur.close()
        conn.close()
        
        if items:
            return "\n".join([f"• {item[0]} - ₪{item[1]} (במלאי: {item[2]})" for item in items])
        else:
            return "אין מוצרים זמינים כרגע"
    except:
        return "שגיאה בטעינת המלאי"

def save_order(order_data):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("""
            INSERT INTO orders (customer_name, items, total_price, address, status) 
            VALUES (%s, %s, %s, %s, %s) RETURNING id
        """, (
            order_data.get('name', 'לקוח'),
            order_data.get('items', ''),
            float(order_data.get('total', 0)),
            order_data.get('address', ''),
            'ממתין לאישור'
        ))
        
        order_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        
        return order_id
    except Exception as e:
        print(f"Error saving order: {e}")
        return None

def extract_order_details(chat_history):
    try:
        prompt = f"""
חלץ מהשיחה הבאה את פרטי ההזמנה בפורמט JSON בלבד, ללא טקסט נוסף:

{chat_history}

החזר JSON בפורמט הזה בדיוק:
{{"name": "שם מלא", "address": "כתובת מלאה", "items": "רשימת מוצרים", "total": סכום_מספרי}}

אם חסר מידע, השאר ריק אבל החזר JSON תקין.
"""
        
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": "אתה מחלץ מידע מדויק. החזר רק JSON תקין, ללא הסבר."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.1,
            max_tokens=300
        )
        
        result = response.choices[0].message.content.strip()
        
        if "{" in result and "}" in result:
            result = result[result.find("{"):result.rfind("}")+1]
            return json.loads(result)
        
        return None
    except:
        return None

@app.route('/')
def home():
    return jsonify({"status": "running", "service": "WhatsApp Bot - המכולת של הצדיק"})

@app.route('/webhook', methods=['GET'])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    else:
        return "Failed", 403

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json()
        
        if 'entry' not in data:
            return jsonify({"status": "no entry"}), 200
        
        for entry in data['entry']:
            for change in entry.get('changes', []):
                value = change.get('value', {})
                
                if 'messages' not in value:
                    continue
                
                for message in value['messages']:
                    message_id = message.get('id')
                    sender = message.get('from')
                    message_type = message.get('type')
                    
                    if is_message_processed(message_id):
                        continue
                    
                    mark_message_processed(message_id)
                    
                    if message_type != 'text':
                        send_whatsapp_message(sender, "אני יכול לענות רק להודעות טקסט 😊")
                        continue
                    
                    user_text = message.get('text', {}).get('body', '')
                    
                    if sender not in user_conversations:
                        user_conversations[sender] = []
                    
                    user_conversations[sender].append({"role": "user", "content": user_text})
                    
                    if len(user_conversations[sender]) > 20:
                        user_conversations[sender] = user_conversations[sender][-20:]
                    
                    inventory = get_inventory()
                    
                    if client:
                        system_prompt = f"""
אתה חיים, בעל המכולת "המכולת של הצדיק". אתה ישראלי חביב, שירותי ואינטליגנטי.

המוצרים הזמינים כרגע:
{inventory}

הנחיות:
1. ענה בעברית טבעית וחמה (אלוף, בשמחה, מעולה)
2. הבן כל שפה שהלקוח כותב, אבל תמיד ענה בעברית
3. כשלקוח שואל על מחיר - ספר מיד מהמלאי
4. אם מוצר אזל - הצע חלופה זמינה
5. כשלקוח מזמין, אסוף פריטים ושאל "עוד משהו?"
6. כשלקוח אומר "זה הכל" או "סיימתי":
   - בקש שם מלא (שם פרטי + משפחה)
   - בקש כתובת מדויקת (רחוב + מספר + עיר)
   - בקש מספר טלפון
7. רק כשיש לך את כל 3 הפרטים (שם, כתובת, טלפון) - כתוב בסוף ההודעה שלך: FINALIZE_ORDER

אל תהיה רובוטי. תהיה טבעי וחכם.
"""
                        
                        try:
                            completion = client.chat.completions.create(
                                model="llama-3.1-8b-instant",
                                messages=[
                                    {"role": "system", "content": system_prompt}
                                ] + user_conversations[sender],
                                temperature=0.7,
                                max_tokens=400
                            )
                            
                            bot_reply = completion.choices[0].message.content
                            
                            if "FINALIZE_ORDER" in bot_reply:
                                clean_reply = bot_reply.replace("FINALIZE_ORDER", "").strip()
                                
                                chat_history = "\n".join([
                                    f"{msg['role']}: {msg['content']}" 
                                    for msg in user_conversations[sender]
                                ])
                                
                                order_data = extract_order_details(chat_history)
                                
                                if order_data:
                                    order_id = save_order(order_data)
                                    
                                    if order_id:
                                        final_message = f"{clean_reply}\n\n✅ ההזמנה שלך נשלחה בהצלחה וממתינה לאישור של הבוס. נעדכן אותך בוואטסאפ ברגע שהוא יאשר!"
                                        send_whatsapp_message(sender, final_message)
                                        user_conversations[sender] = []
                                    else:
                                        send_whatsapp_message(sender, "סליחה, הייתה בעיה בשמירת ההזמנה. נסה שוב בבקשה 🙏")
                                else:
                                    send_whatsapp_message(sender, "לא הצלחתי לאסוף את כל הפרטים. תוכל לשלוח שוב את השם, הכתובת והטלפון? 📝")
                            else:
                                user_conversations[sender].append({"role": "assistant", "content": bot_reply})
                                send_whatsapp_message(sender, bot_reply)
                            
                        except Exception as e:
                            print(f"AI Error: {e}")
                            send_whatsapp_message(sender, "סליחה, יש לי בעיה טכנית. נסה שוב בעוד רגע 😊")
                    
                    else:
                        send_whatsapp_message(sender, f"שלום! המלאי שלנו:\n{inventory}")
        
        return jsonify({"status": "ok"}), 200
        
    except Exception as e:
        print(f"Webhook Error: {e}")
        return jsonify({"status": "ok"}), 200

@app.route('/send_update', methods=['POST'])
def send_update():
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
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)
