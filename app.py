                if not name or len(name) < 2 or "[שם]" in name:
                    send_whatsapp(phone, "אופס, לא קלטתי את השם. איך קוראים לך? 😊")
                else:
                    order_id = save_order(name, phone, address, items, order_type)
                    if order_id:
                        if "איסוף" in order_type.lower():
                            msg = f"פרפקט {name}! הזמנה #{order_id} התקבלה 📦\nמתחילים לארוז — נעדכן מתי לבוא 🛒\n\n💡 שכחת משהו? רוצה לשנות משהו? פשוט כתוב לי!"
                        else:
                            msg = f"יופי {name}! הזמנה #{order_id} הועברה לבוס ⏳\nנעדכן כשהמשלוח ייצא 🛵\n\n💡 שכחת לציין מספר דירה? רוצה לשנות כתובת? פשוט כתוב לי!"
                        
                        if clean_msg:
                            send_long_whatsapp(phone, clean_msg)
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
                send_long_whatsapp(phone, clean_reply)

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
                    msg_id = msg['id']
                    sender = msg['from']
                    msg_type = msg.get('type')

                    if is_message_processed(msg_id):
                        log.info("Duplicate msg %s — skipping", msg_id)
                        continue

                    # ── חסימת הודעות קוליות / מדיה ──
                    if msg_type in ['audio', 'image', 'video', 'document', 'sticker']:
                        log.info("Blocked media msg from %s", sender)
                        send_whatsapp(sender, "סליחה, אני בוט ולא יודע לראות תמונות או לשמוע הודעות קוליות. אפשר לכתוב לי במילים? 🙏")
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
        "version": "6.6"
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    log.info("Bot starting on port %d", port)
    app.run(host='0.0.0.0', port=port, debug=False)
