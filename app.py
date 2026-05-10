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

# --- Connection Pool ---
db_pool = pool.ThreadedConnectionPool(minconn=2, maxconn=10, dsn=DB_URL)

def get_conn():
    return db_pool.getconn()

def release_conn(conn):
    db_pool.putconn(conn)

# --- Debounce state ---
pending_messages: dict[str, list[str]] = {}
pending_timers:   dict[str, threading.Timer] = {}
debounce_lock = threading.Lock()

# ─────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────

def is_message_processed(message_id: str) -> bool:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO processed_messages (message_id) VALUES (%s) ON CONFLICT (message_id) DO NOTHING RETURNING id",
            (message_id,)
        )
        inserted = cur.fetchone()
        conn.commit()
        cur.close()
        return inserted is None
    except Exception as e:
        log.error("is_message_processed error: %s", e)
        conn.rollback()
        return True
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
            "SELECT role, content FROM conversation_history WHERE phone = %s ORDER BY created_at DESC LIMIT %s",
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


def get_pending_order(phone: str) -> dict | None:
    """מחזיר הזמנה פתוחה של הלקוח אם קיימת."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, customer_name, items, address, order_type
            FROM orders
            WHERE address LIKE %s AND status = 'ממתין לאישור'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (f"%WA_ID:{phone}%",)
        )
        row = cur.fetchone()
        cur.close()
        if row:
            return {
                "id":            row[0],
                "customer_name": row[1],
                "items":         row[2],
                "address":       row[3],
                "order_type":    row[4],
            }
        return None
    except Exception as e:
        log.error("get_pending_order error: %s", e)
        return None
    finally:
        release_conn(conn)


def update_order_address(order_id: int, new_address: str, phone: str) -> bool:
    """עדכון כתובת הזמנה — שומר את ה-WA_ID"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        final_address = f"{new_address} | WA_ID:{phone}"
        cur.execute(
            "UPDATE orders SET address = %s WHERE id = %s AND status = 'ממתין לאישור'",
            (final_address, order_id)
        )
        affected = cur.rowcount
        conn.commit()
        cur.close()
        return affected > 0
    except Exception as e:
        log.error("update_order_address error: %s", e)
        conn.rollback()
        return False
    finally:
        release_conn(conn)


def update_order_items(order_id: int, new_items: str) -> bool:
    """עדכון מוצרים בהזמנה קיימת"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE orders SET items = %s WHERE id = %s AND status = 'ממתין לאישור'",
            (new_items, order_id)
        )
        affected = cur.rowcount
        conn.commit()
        cur.close()
        return affected > 0
    except Exception as e:
        log.error("update_order_items error: %s", e)
        conn.rollback()
        return False
    finally:
        release_conn(conn)


def cancel_order_by_customer(order_id: int) -> bool:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE orders SET status='בוטל', cancellation_reason='ביקוש לקוח' WHERE id=%s AND status='ממתין לאישור'",
            (order_id,)
        )
        affected = cur.rowcount
        conn.commit()
        cur.close()
        return affected > 0
    except Exception as e:
        log.error("cancel_order_by_customer error: %s", e)
        conn.rollback()
        return False
    finally:
        release_conn(conn)


def save_order(name: str, phone: str, address: str, items: str, order_type: str) -> int | None:
    conn = get_conn()
    try:
        cur = conn.cursor()
        final_address = f"{address} | WA_ID:{phone}"
        cur.execute(
            "INSERT INTO orders (customer_name, items, status, total_price, address, order_type) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
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
            "INSERT INTO complaints (customer_name, phone, description) VALUES (%s,%s,%s)",
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
    if phone.startswith("0"):
        return "972" + phone[1:]
    if not phone.startswith("972"):
        return "972" + phone
    return phone


def send_whatsapp(phone: str, text: str) -> bool:
    wa_phone = normalize_phone(phone)
    try:
        url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
        headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
        payload = {
            "messaging_product": "whatsapp",
            "to": wa_phone,
            "type": "text",
            "text": {"body": text}
        }
        r = requests.post(url, headers=headers, json=payload, timeout=10)
        if r.status_code == 200:
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
# Debounce
# ─────────────────────────────────────────────

def enqueue_message(phone: str, text: str) -> None:
    with debounce_lock:
        pending_messages.setdefault(phone, []).append(text)
        t = pending_timers.get(phone)
        if t and t.is_alive():
            t.cancel()
        new_timer = threading.Timer(DEBOUNCE_SECS, process_messages, args=[phone])
        pending_timers[phone] = new_timer
        new_timer.start()


def process_messages(phone: str) -> None:
    with debounce_lock:
        texts = pending_messages.pop(phone, [])
        pending_timers.pop(phone, None)

    if not texts:
        return

    combined_text = "\n".join(texts)
    log.info("Processing %d messages from %s: %s", len(texts), phone, combined_text[:80])

    save_message(phone, "user", combined_text)
    history   = get_history(phone)
    inventory = get_inventory()

    # ── הזמנה פתוחה קיימת? ──
    pending_order = get_pending_order(phone)

    if pending_order:
        address_clean = pending_order['address'].split('|')[0].strip()
        pending_ctx = f"""
ℹ️ ללקוח יש הזמנה פתוחה שממתינה לאישור הבעל הבית (לא הלקוח מאשר — הבעל הבית מאשר!):
  מספר הזמנה: #{pending_order['id']}
  שם: {pending_order['customer_name']}
  מוצרים כרגע: {pending_order['items']}
  כתובת: {address_clean}
  סוג: {pending_order['order_type']}

מה הלקוח יכול לעשות עם ההזמנה הפתוחה:

✅ להוסיף מוצרים — אם הלקוח רוצה להוסיף מוצרים לרשימה, עדכן את כל הרשימה ורשום:
UPDATE_ITEMS|{pending_order['id']}|[רשימה מלאה של כל המוצרים כולל הישנים והחדשים]

✅ לשנות כתובת / מספר דירה / קומה / כניסה — רשום:
UPDATE_ADDRESS|{pending_order['id']}|[הכתובת המלאה החדשה]

✅ לבטל את ההזמנה — רשום:
CANCEL_ORDER|{pending_order['id']}

✅ לעשות הזמנה חדשה בנוסף — מותר לחלוטין! תמשיך בתהליך הזמנה רגיל (שאל מוצרים, משלוח/איסוף, שם וכו').
   כלומר אם הלקוח רוצה להזמין עוד דברים כהזמנה נפרדת — תן לו!

חשוב: אל תגיד ללקוח שהוא "לא יכול" לעשות הזמנה חדשה. הוא יכול!
"""
    else:
        pending_ctx = "אין הזמנות פתוחות ללקוח זה — ניתן לקבל הזמנה חדשה."

    system_prompt = f"""אתה "חיים", המוכר האדיב במכולת "המכולת של הצדיק".
המלאי: {inventory}

{pending_ctx}

חוקים:
1. ענה בעברית פשוטה וטבעית.
2. ענה על הכל בתשובה אחת בלבד.
3. אל תחזור על עצמך.
4. לעולם אל תגיד ללקוח שהוא "לא יכול" לעשות הזמנה חדשה — הוא תמיד יכול!

🚨 תלונות (מגעיל / רקוב / קרוע / זבל):
- התנצל, אל תציע קניות
- רשום: FINAL_COMPLAINT|{phone}|[שם]|[תיאור]

🛒 קניות (גם אם יש הזמנה פתוחה — מותר!):
שלב 1 — בחירת מוצרים → "תרצה להוסיף עוד משהו?"
שלב 2 — כשסיים → "משלוח 🛵 או איסוף 🛒?"
שלב 3 — פרטים: משלוח=שם+עיר+רחוב+מספר בית, איסוף=שם בלבד
שלב 4 — כשיש שם מלא → FINAL_ORDER|{phone}|[שם]|[כתובת/איסוף]|[מוצרים]|[סוג]"""

    if not groq_client:
        send_whatsapp(phone, f"שלום! המלאי שלנו:\n{inventory}")
        return

    try:
        completion = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": system_prompt}] + history,
            temperature=0.2,
            max_tokens=300,
            timeout=15,
        )
        bot_reply = completion.choices[0].message.content.strip()
        log.info("AI reply for %s: %s", phone, bot_reply[:100])

        # ── UPDATE_ITEMS ──
        if "UPDATE_ITEMS|" in bot_reply:
            parts     = bot_reply.split("|")
            clean_msg = bot_reply.split("UPDATE_ITEMS|")[0].strip()
            if len(parts) >= 3:
                try:
                    order_id   = int(parts[1].strip())
                    new_items  = parts[2].strip()
                    success = update_order_items(order_id, new_items)
                    if success:
                        if clean_msg:
                            send_whatsapp(phone, clean_msg)
                        send_whatsapp(phone, f"✅ עדכנתי את ההזמנה #{order_id}!\n🛍️ מוצרים: {new_items}\n\nממתינים לאישור הבוס 😊")
                    else:
                        send_whatsapp(phone, "אופס, לא הצלחתי לעדכן. נסה שוב? 🙏")
                except (ValueError, IndexError) as e:
                    log.error("UPDATE_ITEMS parse error: %s", e)
                    send_whatsapp(phone, "לא הצלחתי לעדכן. תנסח שוב? 🙏")

        # ── UPDATE_ADDRESS ──
        elif "UPDATE_ADDRESS|" in bot_reply:
            parts     = bot_reply.split("|")
            clean_msg = bot_reply.split("UPDATE_ADDRESS|")[0].strip()
            if len(parts) >= 3:
                try:
                    order_id    = int(parts[1].strip())
                    new_address = parts[2].strip()
                    success = update_order_address(order_id, new_address, phone)
                    if success:
                        if clean_msg:
                            send_whatsapp(phone, clean_msg)
                        send_whatsapp(phone, f"✅ עדכנתי את הכתובת להזמנה #{order_id}:\n📍 {new_address}\n\nממתינים לאישור הבוס!")
                    else:
                        send_whatsapp(phone, "אופס, לא הצלחתי לעדכן. נסה שוב? 🙏")
                except (ValueError, IndexError) as e:
                    log.error("UPDATE_ADDRESS parse error: %s", e)
                    send_whatsapp(phone, "לא הצלחתי לעדכן את הכתובת. תנסח שוב? 🙏")

        # ── CANCEL_ORDER ──
        elif "CANCEL_ORDER|" in bot_reply:
            parts     = bot_reply.split("|")
            clean_msg = bot_reply.split("CANCEL_ORDER|")[0].strip()
            if len(parts) >= 2:
                try:
                    order_id = int(parts[1].strip())
                    success  = cancel_order_by_customer(order_id)
                    if clean_msg:
                        send_whatsapp(phone, clean_msg)
                    if success:
                        send_whatsapp(phone, f"✅ ההזמנה #{order_id} בוטלה. אם תרצה להזמין שוב — אני כאן! 😊")
                    else:
                        send_whatsapp(phone, "לא הצלחתי לבטל — יכול להיות שההזמנה כבר אושרה. צור קשר עם הבוס ישירות.")
                    clear_history(phone)
                except (ValueError, IndexError) as e:
                    log.error("CANCEL_ORDER parse error: %s", e)

        # ── FINAL_COMPLAINT ──
        elif "FINAL_COMPLAINT|" in bot_reply:
            parts     = bot_reply.split("|")
            clean_msg = bot_reply.split("FINAL_COMPLAINT|")[0].strip()
            if clean_msg:
                send_whatsapp(phone, clean_msg)
            if len(parts) >= 4:
                save_complaint(parts[2], parts[1], parts[3])
                send_whatsapp(phone, "העברתי את התלונה ישירות לבוס לבדיקה דחופה! 🤕")
                clear_history(phone)

        # ── FINAL_ORDER ──
        elif "FINAL_ORDER|" in bot_reply:
            parts     = bot_reply.split("|")
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
                            msg = f"פרפקט {name}! הזמנה #{order_id} התקבלה 📦\nמתחילים לארוז — נעדכן מתי לבוא 🛒\n\n💡 שכחת משהו? רוצה לשנות כתובת? פשוט כתוב לי!"
                        else:
                            msg = f"יופי {name}! הזמנה #{order_id} הועברה לבוס ⏳\nנעדכן כשהמשלוח ייצא 🛵\n\n💡 שכחת לציין מספר דירה? רוצה לשנות כתובת? פשוט כתוב לי!"
                        send_whatsapp(phone, msg)
                        clear_history(phone)
                    else:
                        send_whatsapp(phone, "אופס, הייתה בעיה טכנית. נסה שוב? 🙏")

        # ── תשובה רגילה ──
        else:
            clean_reply = (
                bot_reply
                .replace("FINAL_ORDER", "")
                .replace("FINAL_COMPLAINT", "")
                .replace("UPDATE_ADDRESS", "")
                .replace("UPDATE_ITEMS", "")
                .replace("CANCEL_ORDER", "")
                .strip()
            )
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
        "status":  "running",
        "service": "WhatsApp Bot — המכולת של הצדיק",
        "version": "5.1"
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    log.info("Bot starting on port %d", port)
    app.run(host='0.0.0.0', port=port, debug=False)
