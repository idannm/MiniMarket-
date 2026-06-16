import os
import logging
import threading
import time
import requests
import psycopg2
from psycopg2 import pool
from flask import Flask, request, jsonify
from google import genai

# --- לוגים ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

app = Flask(__name__)

# --- הגדרות ---
DB_URL          = os.environ.get("DB_URL")
GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY")
WHATSAPP_TOKEN  = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
VERIFY_TOKEN    = os.environ.get("VERIFY_TOKEN", "my_secret_password")
INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET", "123")
DEBOUNCE_SECS   = 6.0 

gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# --- Connection Pool ---
db_pool = pool.ThreadedConnectionPool(minconn=2, maxconn=10, dsn=DB_URL)

def get_conn():
    return db_pool.getconn()

def release_conn(conn):
    db_pool.putconn(conn)

# --- Debounce state ---
pending_messages = {}
pending_timers = {}
debounce_lock = threading.Lock()

# ─────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────

def is_message_processed(message_id: str) -> bool:
    conn = None
    try:
        conn = get_conn()
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
        if conn:
            conn.rollback()
        return True
    finally:
        if conn:
            release_conn(conn)


def save_message(phone: str, role: str, content: str) -> None:
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO conversation_history (phone, role, content) VALUES (%s, %s, %s)",
            (phone, role, content)
        )
        conn.commit()
        cur.close()
    except Exception as e:
        log.error("save_message error: %s", e)
        if conn:
            conn.rollback()
    finally:
        if conn:
            release_conn(conn)


def get_history(phone: str, limit: int = 8):
    conn = None
    try:
        conn = get_conn()
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
        if conn:
            release_conn(conn)


def clear_history(phone: str) -> None:
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM conversation_history WHERE phone = %s", (phone,))
        conn.commit()
        cur.close()
        log.info("Successfully cleared history in DB for %s", phone)
    except Exception as e:
        log.error("clear_history error: %s", e)
        if conn:
            conn.rollback()
    finally:
        if conn:
            release_conn(conn)


def get_inventory() -> str:
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT name, price FROM products WHERE stock > 0 ORDER BY name")
        items = cur.fetchall()
        cur.close()
        return ", ".join(f"{i[0]} ({i[1]}₪)" for i in items) if items else "המלאי כרגע ריק"
    except Exception as e:
        log.error("get_inventory error: %s", e)
        return "שגיאה בטעינת המלאי"
    finally:
        if conn:
            release_conn(conn)


def get_pending_order(phone: str):
    conn = None
    try:
        conn = get_conn()
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
        if conn:
            release_conn(conn)


def get_approved_order(phone: str):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, status
            FROM orders
            WHERE address LIKE %s AND status IN ('אושר', 'בדרך', 'הושלם')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (f"%WA_ID:{phone}%",)
        )
        row = cur.fetchone()
        cur.close()
        if row:
            return {"id": row[0], "status": row[1]}
        return None
    except Exception as e:
        log.error("get_approved_order error: %s", e)
        return None
    finally:
        if conn:
            release_conn(conn)


def get_last_order(phone: str):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, items, address, status
            FROM orders
            WHERE address LIKE %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (f"%WA_ID:{phone}%",)
        )
        row = cur.fetchone()
        cur.close()
        if row:
            return {
                "id":      row[0],
                "items":   row[1],
                "address": row[2],
                "status":  row[3]
            }
        return None
    except Exception as e:
        log.error("get_last_order error: %s", e)
        return None
    finally:
        if conn:
            release_conn(conn)


def update_order_address(order_id: int, new_address: str, phone: str) -> bool:
    conn = None
    try:
        conn = get_conn()
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
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            release_conn(conn)


def update_order_items(order_id: int, new_items: str) -> bool:
    conn = None
    try:
        conn = get_conn()
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
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            release_conn(conn)


def cancel_order_by_customer(order_id: int) -> bool:
    if order_id == 0:
        return True
        
    conn = None
    try:
        conn = get_conn()
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
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            release_conn(conn)


def save_order(name: str, phone: str, address: str, items: str, order_type: str):
    conn = None
    try:
        conn = get_conn()
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
        if conn:
            conn.rollback()
        return None
    finally:
        if conn:
            release_conn(conn)


def save_complaint(name: str, phone: str, description: str) -> bool:
    conn = None
    try:
        conn = get_conn()
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
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            release_conn(conn)


# ─────────────────────────────────────────────
# Background Task (Timeout & Cleanup)
# ─────────────────────────────────────────────

def background_tasks_loop():
    REMINDER_TEXT = "היי, ראיתי שהפסקת באמצע. יש משהו שאפשר לעזור בו? (כתוב לי 'לא' אם תרצה שאפסיק להציק 😅)"
    while True:
        conn = None
        try:
            conn = get_conn()
            cur = conn.cursor()
            
            cur.execute('''
                SELECT phone, MAX(created_at)
                FROM conversation_history
                GROUP BY phone
                HAVING EXTRACT(EPOCH FROM (NOW() - MAX(created_at))) > 3600
                   AND EXTRACT(EPOCH FROM (NOW() - MAX(created_at))) < 86400
            ''')
            remind_phones = cur.fetchall()
            for row in remind_phones:
                p = row[0]
                cur.execute("SELECT 1 FROM conversation_history WHERE phone = %s AND content = %s LIMIT 1", (p, REMINDER_TEXT))
                already_sent = cur.fetchone()
                if not already_sent:
                    send_whatsapp(p, REMINDER_TEXT)

            cur.execute('''
                SELECT phone
                FROM conversation_history
                GROUP BY phone
                HAVING EXTRACT(EPOCH FROM (NOW() - MAX(created_at))) > 86400
            ''')
            clear_phones = cur.fetchall()
            for row in clear_phones:
                p = row[0]
                cur.execute("DELETE FROM conversation_history WHERE phone = %s", (p,))
                conn.commit()
                log.info("Cleared history for %s after 24 hours of silence.", p)
                
            cur.close()
        except Exception as e:
            log.error("background_tasks_loop error: %s", e)
        finally:
            if conn:
                release_conn(conn)
            
        time.sleep(300)

# Start the background thread
bg_thread = threading.Thread(target=background_tasks_loop, daemon=True)
bg_thread.start()


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
# Debounce & Process
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

    # ── הזמנה פתוחה / סגורה קיימת? ──
    pending_order = get_pending_order(phone)
    approved_order = get_approved_order(phone)
    last_order = get_last_order(phone)

    context_lines = []

    if approved_order:
        status_str = approved_order['status']
        context_lines.append(f"🔒 שימו לב: ללקוח יש הזמנה פעילה בסטטוס '{status_str}'. לא ניתן לשנות או לבטל אותה. אם הלקוח מתעקש לשנות, הסבר לו באדיבות שהיא כבר ננעלה לטיפול, ושהוא יכול ליצור קשר עם הבוס או לעשות הזמנה חדשה נוספת.")

    if pending_order:
        address_clean = pending_order['address'].split('|')[0].strip()
        p_id = pending_order['id']
        p_name = pending_order['customer_name']
        p_items = pending_order['items']
        p_type = pending_order['order_type']
        
        context_lines.append(f"""
ℹ️ ללקוח יש הזמנה פתוחה שממתינה לאישור הבעל הבית:
  מספר הזמנה: #{p_id}
  שם: {p_name}
  מוצרים כרגע: {p_items}
  כתובת: {address_clean}
  סוג: {p_type}

מה הלקוח יכול לעשות עם ההזמנה הפתוחה (פקודות מערכת):
- להוסיף/לשנות מוצרים: הבוט חייב להחזיר טקסט חופשי ובסוף להצמיד: UPDATE_ITEMS|{p_id}|[רשימה מעודכנת מלאה]
- לשנות כתובת: הבוט יחזיר תשובה חיובית ובסוף יצמיד: UPDATE_ADDRESS|{p_id}|[כתובת חדשה]
- לבטל את ההזמנה: אם הוא מבקש פעם ראשונה, נסה לשכנע אותו באדיבות. אם הוא מתעקש ואומר "סגור", "בטוח", "תבטל לי" בפעם השנייה - החזר בסוף התשובה: CANCEL_ORDER|{p_id}
""")
    else:
        context_lines.append("אין הזמנות פתוחות ללקוח זה — ניתן לקבל הזמנה חדשה.")

    if last_order:
        last_address_clean = last_order['address'].split('|')[0].strip()
        l_id = last_order['id']
        l_items = last_order['items']
        l_status = last_order['status']
        
        context_lines.append(f"""
📜 מידע מהזמנה אחרונה של הלקוח:
  מספר הזמנה אחרונה: #{l_id}
  מוצרים: {l_items}
  כתובת אחרונה: {last_address_clean}
  סטטוס: {l_status}

השתמש בזה כדי להציע 'הזמנה חוזרת' או לשאול אם לשלוח לאותה כתובת.
""")

    pending_ctx = "\n".join(context_lines)

    system_prompt = f"""אתה "חיים", המוכר האדיב במכולת "המכולת של הצדיק".
המלאי הזמין כרגע בחנות למכירה: {inventory}

{pending_ctx}

חוקים מוחלטים להתנהגות הבוט:
1. ענה תמיד בעברית פשוטה, חמה, זורמת ואנושית (כמו מוכר במכולת).
2. אם הלקוח שלח כמה הודעות במקביל, התייחס לכולן יחד בתשובה אחת מרוכזת ויפה.
3. 📞 טלפון של הבוס לבירורים מיוחדים: 052-2025346.
4. ⚠️ כמויות גדולות: אם לקוח מבקש מעל 15 יחידות ממוצר מסוים, שאל קודם "בטוח? זו כמות גדולה".
5. ⏰ שעות פעילות: החנות פתוחה פיזית מ-08:00 עד 20:00. הבוט עונה ומקבל הזמנות 24/7.

🚫 חוק ביטול הזמנה קיימת (שכנוע קל):
- אם הלקוח מבקש לבטל את ההזמנה הפתוחה שלו בפעם הראשונה (או משתמש במילים כמו "תבטל", "עזוב לא צריך", "סגור את זה (במובן של לבטל)"), אל תבטל מיד! נסה לשכנע אותו להישאר (למשל: "בטוח נשמה? חבל, יש לנו היום דברים טריים ומצוינים").
- רק אם הוא מתעקש פעם שנייה ואומר "כן תבטל", "בטוח", או "תסגור לי את זה" בהקשר של ביטול - שלח בסוף ההודעה שלך את המבנה: CANCEL_ORDER|[ID]. (אם אין לו בכלל הזמנה והוא סתם אומר "עזוב לא משנה", רשום CANCEL_ORDER|0).

🚨 תלונות ובעיות (מוצר מקולקל / חסר / משלוח שהתרסק):
- אל תעתיק את המילים של הלקוח! נסח בעצמך תקציר חכם, מקצועי וענייני ב-2 משפטים שמסביר לבוס מה הבעיה (למשל: "הלקוח קיבל חלב פג תוקף והקוטג' היה פתוח").
- רשום בסוף ההודעה: FINAL_COMPLAINT|{phone}|[שם הלקוח]|[הניסוח המדויק והמתומצת שכתבת עבור הבוס]

🛒 תהליך רכישה חדשה:
שלב 1 — בחירת מוצרים מהמלאי בלבד.
שלב 2 — שאל: "משלוח או איסוף עצמי?"
שלב 3 — איסוף פרטים: משלוח מחייב שם מלא + כתובת מדויקת. איסוף עצמי דורש שם בלבד.
שלב 4 — כשהכל מוכן ומאושר לחלוטין על ידי הלקוח, שלח בסוף ההודעה: FINAL_ORDER|{phone}|[שם]|[כתובת או איסוף עצמי]|[רשימת מוצרים סופית]|[סוג: משלוח/איסוף]"""

    if not gemini_client:
        send_whatsapp(phone, f"שלום! המלאי שלנו:\n{inventory}")
        return

    try:
        # מיזוג הודעות רצופות למניעת קריסות של חילופי תורות ב-Gemini
        gemini_history = []
        for msg in history:
            role = "model" if msg["role"] == "assistant" else "user"
            content = msg["content"]
            if gemini_history and gemini_history[-1]["role"] == role:
                gemini_history[-1]["parts"][0]["text"] += f"\n\n{content}"
            else:
                gemini_history.append({"role": role, "parts": [{"text": content}]})

        completion = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=gemini_history,
            config={
                "system_instruction": system_prompt,
                "temperature": 0.2,
                "max_output_tokens": 1000,
            }
        )
        
        if not completion.text:
            log.warning("Empty response from Gemini")
            send_whatsapp(phone, "סליחה, לא הצלחתי להבין. תוכל לכתוב שוב בשבילי? 🙏")
            return
            
        bot_reply = completion.text.strip()
        log.info("AI raw reply for %s: %s", phone, bot_reply[:120])

        # ── ניתוח חכם של פקודות מערכת למניעת קריסות (Safe Parsing) ──
        
        # 1. עדכון מוצרים
        if "UPDATE_ITEMS|" in bot_reply:
            clean_msg = bot_reply.split("UPDATE_ITEMS|")[0].strip()
            parts = bot_reply.split("UPDATE_ITEMS|")[1].split("|")
            try:
                order_id = int(parts[0].strip())
                new_items = parts[1].strip() if len(parts) > 1 else "מוצרים מעודכנים"
                update_order_items(order_id, new_items)
                if clean_msg: send_whatsapp(phone, clean_msg)
                send_whatsapp(phone, f"✅ ההזמנה שלך עודכנה בהצלחה (הזמנה #{order_id})!\n🛍️ המוצרים המעודכנים: {new_items}")
            except Exception as e:
                log.error("Fail to parse UPDATE_ITEMS: %s", e)
                send_whatsapp(phone, clean_msg if clean_msg else "עדכנתי את המוצרים לבקשתך!")

        # 2. עדכון כתובת
        elif "UPDATE_ADDRESS|" in bot_reply:
            clean_msg = bot_reply.split("UPDATE_ADDRESS|")[0].strip()
            parts = bot_reply.split("UPDATE_ADDRESS|")[1].split("|")
            try:
                order_id = int(parts[0].strip())
                new_address = parts[1].strip() if len(parts) > 1 else "כתובת חדשה"
                update_order_address(order_id, new_address, phone)
                if clean_msg: send_whatsapp(phone, clean_msg)
                send_whatsapp(phone, f"✅ עדכנתי את הכתובת החדשה למשלוח עבור הזמנה #{order_id}!\n📍 הכתובת: {new_address}")
            except Exception as e:
                log.error("Fail to parse UPDATE_ADDRESS: %s", e)
                send_whatsapp(phone, clean_msg if clean_msg else "הכתובת עודכנה במערכת!")

        # 3. ביטול הזמנה קיימת
        elif "CANCEL_ORDER|" in bot_reply:
            clean_msg = bot_reply.split("CANCEL_ORDER|")[0].strip()
            parts = bot_reply.split("CANCEL_ORDER|")[1].split("|")
            try:
                order_id = int(parts[0].strip())
                if order_id != 0:
                    success = cancel_order_by_customer(order_id)
                    if clean_msg: send_whatsapp(phone, clean_msg)
                    if success:
                        send_whatsapp(phone, f"🛑 לבקשתך, הזמנה #{order_id} בוטלה במערכת ומופיעה כ'בוטלה' בדשבורד של המנהל. נשמח לשרת אותך בפעם הבאה! 🙏")
                    else:
                        send_whatsapp(phone, "לא הצלחתי לבטל אוטומטית — יכול להיות שהיא כבר אושרה או ננעלה. לבירור מהיר דבר עם הבוס: 052-2025346.")
                else:
                    if clean_msg: send_whatsapp(phone, clean_msg)
                
                # מחיקת היסטוריה בבטחה לאחר שליחת ההודעות
                clear_history(phone)
            except Exception as e:
                log.error("Fail to parse CANCEL_ORDER: %s", e)
                if clean_msg: send_whatsapp(phone, clean_msg)

        # 4. רישום תלונה חכם
        elif "FINAL_COMPLAINT|" in bot_reply:
            clean_msg = bot_reply.split("FINAL_COMPLAINT|")[0].strip()
            parts = bot_reply.split("FINAL_COMPLAINT|")[1].split("|")
            try:
                c_phone = parts[0].strip() if len(parts) > 0 else phone
                c_name = parts[1].strip() if len(parts) > 1 else "לקוח ללא שם"
                c_desc = parts[2].strip() if len(parts) > 2 else "תלונה כללית"
                
                save_complaint(c_name, c_phone, c_desc)
                if clean_msg: send_whatsapp(phone, clean_msg)
                send_whatsapp(phone, "⚠️ תלונתך נוסחה בצורה מסודרת והועברה ישירות לטיפול אישי ודחוף של הבוס בדשבורד המנהל! (לבירור נוסף: 052-2025346)")
                
                clear_history(phone)
            except Exception as e:
                log.error("Fail to parse FINAL_COMPLAINT: %s", e)
                if clean_msg: send_whatsapp(phone, clean_msg)

        # 5. סגירת הזמנה חדשה
        elif "FINAL_ORDER|" in bot_reply:
            clean_msg = bot_reply.split("FINAL_ORDER|")[0].strip()
            parts = bot_reply.split("FINAL_ORDER|")[1].split("|")
            try:
                name       = parts[1].strip() if len(parts) > 1 else "לקוח"
                address    = parts[2].strip() if len(parts) > 2 else "איסוף"
                items      = parts[3].strip() if len(parts) > 3 else "מוצרים"
                order_type = parts[4].strip() if len(parts) > 4 else "לא צוין"

                if not name or len(name) < 2 or "[שם]" in name:
                    send_whatsapp(phone, "רגע נשמה, לא קלטתי טוב את השם שלך. איך קוראים לך כדי שאוכל לרשום את זה? 😊")
                else:
                    order_id = save_order(name, phone, address, items, order_type)
                    if order_id:
                        if "איסוף" in order_type.lower():
                            msg = f"אש עליך {name}! הזמנה #{order_id} נשמרה בהצלחה 📦\nהצוות כבר מתחיל לארוז אותה — נעדכן אותך כאן ברגע שהכל מוכן ומחכה לך בחנות! 🛒"
                        else:
                            msg = f"מעולה {name}! הזמנה #{order_id} הועברה ישירות לבוס לאישור ⏳\nנשלח לך הודעה ברגע שהשליח יוצא לכיוון הכתובת שלך! 🛵"
                        
                        if clean_msg: send_whatsapp(phone, clean_msg)
                        send_whatsapp(phone, msg)
                        clear_history(phone)
                    else:
                        send_whatsapp(phone, "אופס, הייתה בעיה קטנה בשמירת ההזמנה במסד הנתונים. תוכל לנסות לאשר שוב? 🙏")
            except Exception as e:
                log.error("Fail to parse FINAL_ORDER: %s", e)
                send_whatsapp(phone, "ההזמנה נקלטה, אך חסרים פרטים. בוא נעשה סדר קטן!")

        # 6. שיחה רגילה
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
        send_whatsapp(phone, "סליחה, יש לי עומס קטן במערכת. תוכל לכתוב לי שוב בעוד שנייה? 😊")


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
                    msg_id = msg['id']
                    sender = msg['from']
                    msg_type = msg.get('type')

                    if is_message_processed(msg_id):
                        log.info("Duplicate msg %s — skipping", msg_id)
                        continue

                    # ── חסימת הודעות קוליות / מדיה ──
                    if msg_type in ['audio', 'image', 'video', 'document', 'sticker']:
                        log.info("Blocked media msg from %s", sender)
                        send_whatsapp(sender, "סליחה, אני בוט ואני עדיין לא יודע לראות תמונות או לשמוע הודעות קוליות. תוכל לכתוב לי את זה במילים? 🙏")
                        continue
                        
                    if msg_type != 'text':
                        continue

                    text = msg['text']['body']
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
        "version": "7.0"
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    log.info("Bot starting on port %d", port)
    app.run(host='0.0.0.0', port=port, debug=False)
