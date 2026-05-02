import os
import logging
import threading
import requests
import psycopg2
from psycopg2 import pool
from flask import Flask, request, jsonify
from groq import Groq

# --- לוגים ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

app = Flask(__name__)

# --- הגדרות ---
DB_URL          = os.environ.get("DB_URL")
GROQ_API_KEY    = os.environ.get("GROQ_API_KEY")
WHATSAPP_TOKEN  = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
VERIFY_TOKEN    = os.environ.get("VERIFY_TOKEN", "my_secret_password")
INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET", "123")
DEBOUNCE_SECS   = 2.5

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# --- Connection Pool (במקום לפתוח חיבור חדש בכל קריאה) ---
db_pool = pool.ThreadedConnectionPool(minconn=2, maxconn=10, dsn=DB_URL)

def get_conn():
    return db_pool.getconn()

def release_conn(conn):
    db_pool.putconn(conn)

# --- Debounce state עם Lock למניעת race conditions ---
pending_messages: dict[str, list[str]] = {}
pending_timers:   dict[str, threading.Timer] = {}
debounce_lock = threading.Lock()

# ─────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────

def is_message_processed(message_id: str) -> bool:
    """בדיקה ושמירה אטומית — מונע עיבוד כפול גם תחת עומס."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO processed_messages (message_id)
            VALUES (%s)
            ON CONFLICT (message_id) DO NOTHING
            RETURNING id
            """,
            (message_id,)
        )
        inserted = cur.fetchone()
        conn.commit()
        cur.close()
        return inserted is None   # True = כבר עובד, False = חדש
    except Exception as e:
        log.error("is_message_processed error: %s", e)
        conn.rollback()
        return True               # ספק → תדלג, עדיף מאשר כפול
    finally:
        release_conn(conn)


def save_message(phone: str, role: str, content: str) -> None:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO conversation_history (phone, role, content) VALUES (%s, %s, %s)",
            (phone, role, content)
        )
        conn.commit()
        cur.close()
    except Exception as e:
        log.error("save_message error: %s", e)
        conn.rollback()
    finally:
        release_conn(conn)


def get_history(phone: str, limit: int = 8) -> list[dict]:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT role, content
            FROM conversation_history
            WHERE phone = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (phone, limit)
        )
        rows = cur.fetchall()
        cur.close()
        return [{"role": r[0], "content": r[1]} for r in reversed(rows)]
    except Exception as e:
        log.error("get_history error: %s", e)
        return []
    finally:
        release_conn(conn)


def clear_history(phone: str) -> None:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM conversation_history WHERE phone = %s", (phone,))
        conn.commit()
        cur.close()
    except Exception as e:
        log.error("clear_history error: %s", e)
        conn.rollback()
    finally:
        release_conn(conn)


def get_inventory() -> str:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT name, price FROM products WHERE stock > 0 ORDER BY name")
        items = cur.fetchall()
        cur.close()
        return ", ".join(f"{i[0]} ({i[1]}₪)" for i in items) if items else "המלאי כרגע ריק"
    except Exception as e:
        log.error("get_inventory error: %s", e)
        return "שגיאה בטעינת המלאי"
    finally:
        release_conn(conn)


def save_order(name: str, phone: str, address: str, items: str, order_type: str) -> int | None:
    conn = get_conn()
    try:
        cur = conn.cursor()
        final_address = f"{address} | WA_ID:{phone}"
        cur.execute(
            """
            INSERT INTO orders (customer_name, items, status, total_price, address, order_type)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (name, items, "ממתין לאישור", 0, final_address, order_type)
        )
        new_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        return new_id
    except Exception as e:
        log.error("save_order error: %s", e)
        conn.rollback()
        return None
    finally:
        release_conn(conn)


def save_complaint(name: str, phone: str, description: str) -> bool:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO complaints (customer_name, phone, description) VALUES (%s, %s, %s)",
            (name, phone, description)
        )
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        log.error("save_complaint error: %s", e)
        conn.rollback()
        return False
    finally:
        release_conn(conn)


# ─────────────────────────────────────────────
# WhatsApp send
# ─────────────────────────────────────────────

def normalize_phone(phone: str) -> str:
    """המרה לפורמט בינלאומי ישראלי."""
    if phone.startswith("0"):
        return "972" + phone[1:]
    if not phone.startswith("972"):
        return "972" + phone
    return phone


def send_whatsapp(phone: str, text: str) -> bool:
    """שולח הודעה ושומר ב-DB עם אותו מפתח phone (לפני normalization)."""
    wa_phone = normalize_phone(phone)
    try:
        url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
        headers = {
            "Authorization": f"Bearer {WHATSAPP_TOKEN}",
            "Content-Type": "application/json"
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": wa_phone,
            "type": "text",
            "text": {"body": text}
        }
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        if r.status_code == 200:
            # שמירה עם המפתח המקורי, אותו מפתח שבו הלקוח מזוהה בהיסטוריה
            save_message(phone, "assistant", text)
            log.info("Sent to %s", wa_phone)
            return True
        else:
            log.error("WA send failed %s: %s", r.status_code, r.text)
            return False
    except Exception as e:
        log.error("send_whatsapp error: %s", e)
        return False


# ─────────────────────────────────────────────
# Debounce — קיבוץ הודעות
# ─────────────────────────────────────────────

def enqueue_message(phone: str, text: str) -> None:
    """מוסיף הודעה לתור ומאפס את הטיימר."""
    with debounce_lock:
        pending_messages.setdefault(phone, []).append(text)

        # ביטול טיימר קיים
        t = pending_timers.get(phone)
        if t and t.is_alive():
            t.cancel()

        # טיימר חדש
        new_timer = threading.Timer(DEBOUNCE_SECS, process_messages, args=[phone])
        pending_timers[phone] = new_timer
        new_timer.start()


def process_messages(phone: str) -> None:
    """
    מופעל אחרי שקט של DEBOUNCE_SECS.
    שולף את כל הטקסטים, מקבץ, שולח ל-AI פעם אחת.
    """
    with debounce_lock:
        texts = pending_messages.pop(phone, [])
        pending_timers.pop(phone, None)

    if not texts:
        return

    combined_text = "\n".join(texts)
    log.info("Processing %d messages from %s", len(texts), phone)

    # שמירת ההודעה המאוחדת — רק עכשיו, לפני שמושכים את ה-history
    save_message(phone, "user", combined_text)

    # history כעת כולל את combined_text בתחתיתו
    history = get_history(phone)
    inventory = get_inventory()

    system_prompt = f"""אתה "חיים", המוכר האדיב במכולת "המכולת של הצדיק".
המלאי הזמין כרגע: {inventory}

חוקים קריטיים:
1. ענה בעברית פשוטה וטבעית.
2. קרא את כל ההודעות של הלקוח וענה על הכל בתשובה אחת.
3. אל תחזור על עצמך.

🚨 אם הלקוח כועס/מתלונן (מילים: מגעיל, רקוב, קרוע, זבל):
- התנצל מיד ואל תציע קניות
- כתוב: FINAL_COMPLAINT|{phone}|[שם]|[תיאור]

🛒 קניות (שלב אחרי שלב):
1. בחירת מוצרים → "תרצה להוסיף עוד משהו?"
2. כשסיים → "משלוח 🛵 או איסוף 🛒?"
3. פרטים: משלוח=שם+עיר+רחוב, איסוף=שם בלבד
4. רק כשיש שם מלא → FINAL_ORDER|{phone}|[שם]|[כתובת/איסוף]|[מוצרים]|[סוג]"""

    if not groq_client:
        send_whatsapp(phone, f"שלום! המלאי שלנו:\n{inventory}")
        return

    try:
        completion = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": system_prompt}] + history,
            temperature=0.2,
            max_tokens=300,
            timeout=15,   # לא תתקע לנצח
        )
        bot_reply = completion.choices[0].message.content.strip()
        log.info("AI reply for %s: %s", phone, bot_reply[:80])

        if "FINAL_COMPLAINT|" in bot_reply:
            parts = bot_reply.split("|")
            clean_msg = bot_reply.split("FINAL_COMPLAINT|")[0].strip()
            if clean_msg:
                send_whatsapp(phone, clean_msg)
            if len(parts) >= 4:
                save_complaint(parts[2], parts[1], parts[3])
                send_whatsapp(phone, "העברתי את התלונה ישירות לבוס לבדיקה דחופה! 🤕")
                clear_history(phone)

        elif "FINAL_ORDER|" in bot_reply:
            parts = bot_reply.split("|")
            clean_msg = bot_reply.split("FINAL_ORDER|")[0].strip()
            if len(parts) >= 6:
                name       = parts[2].strip()
                address    = parts[3].strip()
                items      = parts[4].strip()
                order_type = parts[5].strip()

                if not name or len(name) < 2 or "[שם]" in name:
                    send_whatsapp(phone, "אופס, לא קלטתי את השם. איך קוראים לך? 😊")
                else:
                    order_id = save_order(name, phone, address, items, order_type)
                    if order_id:
                        if "איסוף" in order_type.lower():
                            msg = f"פרפקט {name}! הזמנה #{order_id} התקבלה 📦 מתחילים לארוז — נעדכן מתי לבוא 🛒"
                        else:
                            msg = f"יופי {name}! הזמנה #{order_id} הועברה לבוס ⏳ נעדכן כשהמשלוח ייצא 🛵"
                        send_whatsapp(phone, msg)
                        clear_history(phone)
                    else:
                        send_whatsapp(phone, "אופס, הייתה בעיה טכנית. נסה שוב? 🙏")

        else:
            clean_reply = bot_reply.replace("FINAL_ORDER", "").replace("FINAL_COMPLAINT", "").strip()
            if clean_reply:
                send_whatsapp(phone, clean_reply)

    except Exception as e:
        log.error("process_messages AI error for %s: %s", phone, e)
        send_whatsapp(phone, "סליחה, יש לי בעיה טכנית קטנה. נסה שוב בעוד שנייה 😊")


# ─────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────

@app.route('/webhook', methods=['GET'])
def verify_webhook():
    mode      = request.args.get('hub.mode')
    token     = request.args.get('hub.verify_token')
    challenge = request.args.get('hub.challenge')
    if mode == 'subscribe' and token == VERIFY_TOKEN:
        return challenge, 200
    return 'Forbidden', 403


@app.route('/webhook', methods=['POST'])
def receive_message():
    try:
        data = request.json
        if 'entry' not in data:
            return 'EVENT_RECEIVED', 200

        for entry in data.get('entry', []):
            for change in entry.get('changes', []):
                value = change.get('value', {})
                for msg in value.get('messages', []):
                    if msg.get('type') != 'text':
                        continue

                    msg_id = msg['id']
                    sender = msg['from']
                    text   = msg['text']['body']

                    if is_message_processed(msg_id):
                        log.info("Duplicate msg %s — skipping", msg_id)
                        continue

                    log.info("New msg from %s: %s", sender, text)
                    enqueue_message(sender, text)

    except Exception as e:
        log.error("Webhook error: %s", e)

    return 'EVENT_RECEIVED', 200


@app.route('/send_update', methods=['POST'])
def send_update():
    if request.headers.get('X-Internal-Secret') != INTERNAL_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    data    = request.json
    phone   = str(data.get('phone', '')).strip()
    message = data.get('message', '')

    if not phone or not message:
        return jsonify({"error": "Missing phone or message"}), 400

    success = send_whatsapp(phone, message)
    return (jsonify({"status": "sent"}), 200) if success else (jsonify({"error": "Failed"}), 500)


@app.route('/')
def home():
    return jsonify({
        "status": "running",
        "service": "WhatsApp Bot — המכולת של הצדיק",
        "version": "4.0"
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    log.info("Bot starting on port %d", port)
    app.run(host='0.0.0.0', port=port, debug=False)
