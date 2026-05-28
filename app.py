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
    """מחזיר הזמנה פתוחה של הלקוח אם קיימת במצב ממתין לאישור."""
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


def get_approved_order(phone: str) -> dict | None:
    """מחזיר הזמנה שכבר אושרה, בדרך או הושלמה (נעולה לשינויים)"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, customer_name, items, address, order_type, status
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
            return {
                "id":            row[0],
                "customer_name": row[1],
                "items":         row[2],
                "address":       row[3],
                "order_type":    row[4],
                "status":        row[5],
            }
        return None
    except Exception as e:
        log.error("get_approved_order error: %s", e)
        return None
    finally:
        release_conn(conn)


def get_last_order(phone: str) -> dict | None:
    """מחזיר את ההזמנה האחרונה ביותר מכל סטטוס לצורך מעקב, שחזור מוצרים ושמירת כתובת"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, customer_name, items, address, order_type, status
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
                "id":            row[0],
                "customer_name": row[1],
                "items":         row[2],
                "address":       row[3],
                "order_type":    row[4],
                "status":        row[5],
            }
        return None
    except Exception as e:
        log.error("get_last_order error: %s", e)
        return None
    finally:
        release_conn(conn)


def update_order_address(order_id: int, new_address: str, phone: str) -> bool:
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
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE orders SET items = %s WHERE id = %s AND status = 'ממתיン לאישור'",
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

    # ── טעינת סטטוס הזמנות מהדאטה-בייס לקונטקסט של הבוט ──
    pending_order  = get_pending_order(phone)
    approved_order = get_approved_order(phone)
    last_order     = get_last_order(phone)

    # קונטקסט להזמנה פתוחה/ממתינה
    if pending_order:
        address_clean = pending_order['address'].split('|')[0].strip()
        pending_ctx = f"""
ℹ️ ללקוח יש הזמנה פתוחה שממתינה לאישור הבעל הבית:
  מספר הזמנה: #{pending_order['id']}
  שם: {pending_order['customer_name']}
  מוצרים כרגע: {pending_order['items']}
  כתובת: {address_clean}
  סוג: {pending_order['order_type']}

פעולות מותרות על הזמנה זו:
✅ להוסיף מוצרים: UPDATE_ITEMS|{pending_order['id']}|[רשימה מלאה של הישנים והחדשים]
✅ לשנות כתובת: UPDATE_ADDRESS|{pending_order['id']}|[הכתובת החדשה]
✅ לבטל את ההזמנה: לחץ על חוק אישור ביטול כפול למטה!
✅ לפתוח הזמנה חדשה במקביל - מותר לחלוטין!
"""
    else:
        pending_ctx = "אין כרגע הזמנות פתוחות במצב ממתין לאישור ללקוח זה."

    # קונטקסט להזמנה מאושרת/נעולה
    if approved_order:
        approved_ctx = f"""
🛑 שים לב חיוני: ללקוח יש הזמנה קודמת שכבר אושרה ומטופלת (סטטוס נוכחי: {approved_order['status']}), מספר הזמנה #{approved_order['id']}.
ההזמנה הזו נעולה לחלוטין! אי אפשר לשנות אותה, אי אפשר לבטל אותה ואי אפשר להוסיף לה מוצרים בשום אופן.
אם הלקוח מבקש לשנות או להוסיף מוצרים להזמנה הזו, תגיד לו במפורש ובנימוס: "ההזמנה הקודמת שלך כבר אושרה ויצאה לדרך, ולכן אי אפשר לשנות אותה. פתחתי לך הזמנה חדשה עבור המוצרים הנוספים!"
לאחר מכן, תמשיך איתו כרגיל לתהליך של הזמנה חדשה לחלוטין.
"""
    else:
        approved_ctx = ""

    # קונטקסט לפיצ'רים החדשים: מעקב, הזמנה חוזרת ושמירת כתובת
    if last_order:
        last_address_clean = last_order['address'].split('|')[0].strip()
        history_ctx = f"""
📦 מידע על ההזמנה האחרונה ביותר של הלקוח במערכת (לצורך מעקב, הזמנה חוזרת ושמירת כתובת):
  מספר הזמנה אחרונה: #{last_order['id']}
  סטטוס נוכחי במערכת: {last_order['status']}
  מוצרים מהפעם הקודמת: {last_order['items']}
  כתובת מהפעם הקודמת: {last_address_clean}
  סוג קבלה מהפעם הקודמת: {last_order['order_type']}

חוקים לפיצ'רים אלו:
1. מעקב הזמנה: אם הלקוח שואל "מה עם ההזמנה שלי?" או משהו דומה, תגיד לו בדיוק מה הסטטוס שלה המופיע למעלה (ממתין לאישור / אושר / בדרך / הושלם / בוטל).
2. הזמנה חוזרת: אם הלקוח כותב "תן לי אותו דבר כמו פעם קונמת" או "אותו דבר כמו פעם שעברה", תציע לו את רשימת המוצרים המדויקת מהפעם האחרונה המופיעה למעלה.
3. שמירת כתובת: אם הלקוח מבצע הזמנה חדשה (במשלוח), אל תבקש ממנו להקליד כתובת מחדש! שאל אותו: "רוצה שנשלח לאותה כתובת כמו פעם שעברה ({last_address_clean})?". רק אם הוא אומר שלא או רוצה לשנות, תבקש כתובת חדשה.
"""
    else:
        history_ctx = "זהו לקוח חדש לחלוטין במערכת, אין לו היסטוריית הזמנות קודמת."

    system_prompt = f"""אתה "חיים", המוכר האדיב במכולת "המכולת של הצדיק".
المלאי הזמין בחנות: {inventory}

{pending_ctx}

{approved_ctx}

{history_ctx}

חוקים קשיחים לניהול השיחה:
1. ענה בעברית פשוטה וטבעית. ענה על הכל בתשובה אחת בלבד ואל תחזור על עצמך.
2. לעולם אל תגיד ללקוח שהוא "לא יכול" לעשות הזמנה חדשה — הוא תמיד יכול!
3. 🚨 תלונות (מגעיל / רקוב / קרוע / זבל): התנצל, אל תציע קניות, ורשום: FINAL_COMPLAINT|{phone}|[שם]|[תיאור]

4. 📞 תמיכה אנושית / בעיות: אם הלקוח מתעקש לדבר עם נציג/אדם, או שיש בעיה קריטית שאינך מצליח לפתור, תן לו את מספר הטלפון של הבוס: 052-2025346 בצורה אדיבה. אל תפסיק את הבוט ואל תשתק את השיחה! הבוט ממשיך לענות כרגיל.

5. ⚠️ חוק מגבלת כמות הגיונית: אם לקוח מבקש כמות חריגה וגדולה מאוד של מוצרים (למשל: 50 בקבוקי שמן, או מעל 15 יחידות מכל מוצר), אל תפלוט ישר את פקודת FINAL_ORDER! עצור ושאל אותו במפורש: "בטוח? זה נשמע כמות גדולה". רק אחרי שהוא מאשר ואומר שכן, תתקדם לרישום ההזמנה.

6. 🛑 חוק אישור ביטול כפול: אם הלקוח מבקש לבטל הזמנה פתוחה, אל תפלוט ישר את הפקודה CANCEL_ORDER. שאל אותו תחילה: "בטוח שתרצה לבטל את הזמנה #[מספר ההזמנה]?". רק אם הוא עונה במפורש "כן", "בטוח" או "מאשר", פלוט את הפקודה: CANCEL_ORDER|[מספר ההזמנה].

🛒 קניות:
שלב 1 — בחירת מוצרים → "תרצה להוסיף עוד משהו?"
שלב 2 — כשסיים → "משלוח 🛵 או איסוף 🛒?"
שלב 3 — פרטים: משלוח=שם+כתובת (או אישור הכתובת הישנה), איסוף=שם בלבד
שלב 4 — כשיש פרטים מלאים ומאושרים → FINAL_ORDER|{phone}|[שם]|[כתובת/איסוף]|[מוצרים]|[סוג]"""

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
            parts = bot_reply.split("|")
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

        # ── UPDATE_ADDRESS ──
        elif "UPDATE_ADDRESS|" in bot_reply:
            parts = bot_reply.split("|")
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

        # ── CANCEL_ORDER ──
        elif "CANCEL_ORDER|" in bot_reply:
            parts = bot_reply.split("|")
            clean_msg = bot_reply.split("CANCEL_ORDER|")[0].strip()
            if len(parts) >= 2:
                try:
                    order_id = int(parts[1].strip())
                    success  = cancel_order_by_customer(order_id)
                    if clean_msg:
                        send_whatsapp(phone, clean_msg)
                    if success:
                        send_whatsapp(phone, f"✅ ההזמנה #{order_id} בבוטלה לבקשתך. אם תרצה להזמין משהו חדש — אני כאן! 😊")
                    else:
                        send_whatsapp(phone, "לא הצלחתי לבטל — יכול להיות שההזמנה כבר אושרה על ידי הבוס.")
                    clear_history(phone)
                except (ValueError, IndexError) as e:
                    log.error("CANCEL_ORDER parse error: %s", e)

        # ── FINAL_COMPLAINT ──
        elif "FINAL_COMPLAINT|" in bot_reply:
            parts = bot_reply.split("|")
            clean_msg = bot_reply.split("FINAL_COMPLAINT|")[0].strip()
            if clean_msg:
                send_whatsapp(phone, clean_msg)
            if len(parts) >= 4:
                save_complaint(parts[2], parts[1], parts[3])
                send_whatsapp(phone, "העברתי את התלונה ישירות לבוס לבדיקה דחופה! 🤕")
                clear_history(phone)

        # ── FINAL_ORDER ──
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
                            msg = f"פרפקט {name}! הזמנה #{order_id} התקבלה 📦\nמתחילים לארוז — נעדכן מתי לבוא 🛒\n\n💡 שכחת משהו? רוצה לשנות? פשוט כתוב לי!"
                        else:
                            msg = f"יופי {name}! הזמנה #{order_id} הועברה לבוס ⏳\nנעדכן כשהמשלוח ייצא 🛵\n\n💡 שכחת פרט כלשהו או דירה? רוצה לשנות? פשוט כתוב לי!"
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
# מנגנון תזכורת שתיקה ותפוגת שיחה (רקע)
# ─────────────────────────────────────────────

def handle_timeouts():
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        
        # 1. מחיקה אוטומטית של היסטוריית שיחה שלא הייתה בה פעילות 24 שעות
        cur.execute("""
            DELETE FROM conversation_history 
            WHERE phone IN (
                SELECT phone FROM conversation_history 
                GROUP BY phone 
                HAVING MAX(created_at) < NOW() - INTERVAL '24 hours'
            )
        """)
        conn.commit()
        
        # 2. שליחת תזכורת אחרי שעה של שתיקה (ורק אם ההודעה האחרונה אינה הודעת התזכורת עצמה)
        cur.execute("""
            WITH last_msgs AS (
                SELECT DISTINCT ON (phone) phone, content, role, created_at
                FROM conversation_history
                ORDER BY phone, created_at DESC
            )
            SELECT phone FROM last_msgs
            WHERE created_at < NOW() - INTERVAL '1 hour'
              AND content != 'היי, לא גמרנו — אתה עדיין צריך משהו?'
        """)
        nudge_candidates = cur.fetchall()
        cur.close()
        
        for row in nudge_candidates:
            phone = row[0]
            log.info("Sending silence nudge to %s", phone)
            send_whatsapp(phone, "היי, לא גמרנו — אתה עדיין צריך משהו?")
            
    except Exception as e:
        log.error("handle_timeouts background error: %s", e)
    finally:
        if conn:
            release_conn(conn)


def run_timeout_loop():
    import time
    while True:
        time.sleep(300)  # ריצה כל 5 דקות לבדיקת שרת
        handle_timeouts()


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
                    msg_id   = msg.get('id')
                    sender   = msg.get('from')
                    msg_type = msg.get('type')

                    if not msg_id or not sender:
                        continue

                    # טיפול מובנה ומניעת כפילויות להודעות קוליות ותמונות (חדש)
                    if msg_type in ['audio', 'image']:
                        if is_message_processed(msg_id):
                            log.info("Duplicate media msg %s — skipping", msg_id)
                            continue
                        
                        if msg_type == 'audio':
                            send_whatsapp(sender, "סליחה, אני לא יכול להאזין להקלטות, כתוב לי בטקסט 😊")
                        elif msg_type == 'image':
                            send_whatsapp(sender, "סליחה, אני לא רואה תמונות, כתוב לי מה אתה צריך 😊")
                        continue

                    if msg_type != 'text':
                        continue

                    text = msg['text']['body']

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
        "version": "6.0"
    })


if __name__ == '__main__':
    # הפעלת תהליכון הרקע לבדיקת שתיקה וזמני תפוגה
    t = threading.Thread(target=run_timeout_loop, daemon=True)
    t.start()

    port = int(os.environ.get('PORT', 10000))
    log.info("Bot starting on port %d", port)
    app.run(host='0.0.0.0', port=port, debug=False)
