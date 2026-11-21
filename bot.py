import os
import signal
import threading
import time
import logging
import queue

from flask import Flask
import telebot
from telebot.types import InputMediaVideo, InputMediaPhoto

# ----- لوقات خفيفة -----
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
for noisy in ("urllib3", "waitress", "telebot", "requests"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
logger = logging.getLogger("bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("⚠️ خطأ: لم يتم العثور على التوكن! أضفه في Environment في Render.")

bot = telebot.TeleBot(BOT_TOKEN, num_threads=2)
app = Flask(__name__)

# ----- الحالة + الحماية -----
media_groups = {}
timers = {}
waiting_for_caption = {}
session_timers = {}     # مؤقّتات الخمول 20 دقيقة لكل شات
last_activity = {}      # آخر نشاط لكل شات (شبكة أمان للتنظيف الدوري)
warned_overflow = set()
LOCK = threading.Lock()

send_queue = queue.Queue()

GROUP_DELAY = 3.0             # تجميع الألبوم
SESSION_TIMEOUT = 1200        # 🔑 20 دقيقة: بعدها خامل تمامًا
MAX_MEDIA_PER_GROUP = 100
BASE_CAPTION = "#حصريات_VAMP1R3S"
START_TIME = time.time()


# ---------- 🌐 نقطة الصحة ----------
@app.route('/')
def index():
    return f"OK - uptime {int(time.time() - START_TIME)}s", 200


def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=port, threads=2)
    except ImportError:
        app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)


# ---------- 📨 خيط الإرسال الخلفي ----------
def sender_worker():
    while True:
        job = send_queue.get()
        try:
            job()
        except Exception as e:
            logger.error(f"خطأ في مهمة إرسال: {e}")
        finally:
            send_queue.task_done()


# ---------- 🧠 إدارة الجلسات (20 دقيقة) ----------
def touch_session(chat_id):
    """أي نشاط من الشات يجدّد عدّاد الـ20 دقيقة."""
    with LOCK:
        last_activity[chat_id] = time.time()
        old = session_timers.pop(chat_id, None)
        if old:
            old.cancel()
        t = threading.Timer(SESSION_TIMEOUT, purge_chat, args=[chat_id])
        session_timers[chat_id] = t
    t.start()


def purge_chat(chat_id, notify=True):
    """انتهت الـ20 دقيقة: تفريغ كامل للذاكرة → خامل."""
    with LOCK:
        had_state = chat_id in media_groups or chat_id in waiting_for_caption
        media_groups.pop(chat_id, None)
        waiting_for_caption.pop(chat_id, None)
        t = timers.pop(chat_id, None)
        if t:
            t.cancel()
        st = session_timers.pop(chat_id, None)
        if st:
            st.cancel()
        last_activity.pop(chat_id, None)
        warned_overflow.discard(chat_id)
    if had_state:
        logger.info(f"🧠 شات {chat_id}: انتهت 20 دقيقة بدون نشاط → الذاكرة صفر، خامل.")
        if notify:
            try:
                bot.send_message(chat_id, "⏰ انتهت جلسة الحفظ (20 دقيقة بدون نشاط) وتم تفريغ الذاكرة.\nأرسل مقاطعك من جديد متى ما تبي.")
            except Exception:
                pass


def close_session(chat_id):
    """إغلاق فوري بعد إكمال العملية أو الإلغاء (ما ننتظر 20 دقيقة)."""
    with LOCK:
        st = session_timers.pop(chat_id, None)
        if st:
            st.cancel()
        last_activity.pop(chat_id, None)
        warned_overflow.discard(chat_id)


# ---------- 🧹 شبكة أمان: تنظيف دوري كل 5 دقائق ----------
def cleanup_loop():
    while True:
        time.sleep(300)
        now = time.time()
        with LOCK:
            stale = [cid for cid, ts in last_activity.items()
                     if now - ts > SESSION_TIMEOUT + 120]
        for cid in stale:
            purge_chat(cid, notify=False)


# ---------- 📴 إيقاف آمن ----------
def handle_sigterm(signum, frame):
    logger.info("📴 SIGTERM - إيقاف نظيف...")
    threading.Thread(target=bot.stop_bot, daemon=True).start()


# ---------- منطق البوت ----------
def ask_for_caption(chat_id):
    with LOCK:
        items = media_groups.pop(chat_id, None)
        timers.pop(chat_id, None)
        warned_overflow.discard(chat_id)
    if not items:
        return

    media_list = [it['media'] for it in items]
    messages_to_delete = [it['message_id'] for it in items]

    try:
        msg = bot.send_message(chat_id, "وش تبي تحط كلام بالكابشن؟\n(أرسل 'بدون' لو تبي الهاشتاق بس)")
    except Exception as e:
        logger.error(f"فشل سؤال الكابشن: {e}")
        return

    messages_to_delete.append(msg.message_id)
    with LOCK:
        waiting_for_caption[chat_id] = {
            'media_list': media_list,
            'messages_to_delete': messages_to_delete,
            'created_at': time.time()
        }
    bot.register_next_step_handler(msg, process_group_caption)


def process_group_caption(message):
    chat_id = message.chat.id
    with LOCK:
        state = waiting_for_caption.get(chat_id)
    if not state:
        return  # الجلسة انتهت (خامل) → نتجاهل بهدوء

    touch_session(chat_id)  # ردّه = نشاط يجدّد العداد
    state['messages_to_delete'].append(message.message_id)
    text = message.text

    # إلغاء
    if text and text.strip() in ('إلغاء', '/cancel'):
        with LOCK:
            waiting_for_caption.pop(chat_id, None)
        m = bot.send_message(chat_id, "❌ تم إلغاء العملية.")
        state['messages_to_delete'].append(m.message_id)
        delete_quietly(chat_id, state['messages_to_delete'])
        close_session(chat_id)
        return

    if not text:
        msg = bot.send_message(chat_id, "⚠️ الرجاء إرسال نص فقط. وش تبي تكتب بالكابشن؟")
        state['messages_to_delete'].append(msg.message_id)
        bot.register_next_step_handler(msg, process_group_caption)
        return

    custom_text = text.strip()
    final_caption = BASE_CAPTION if custom_text == 'بدون' else f"{BASE_CAPTION}\n\n\n{custom_text}"

    if len(final_caption) > 1024:
        msg = bot.send_message(chat_id, "⚠️ الكابشن طويل جدًا (الحد 1024 حرف). أرسل أقصر أو 'بدون'.")
        state['messages_to_delete'].append(msg.message_id)
        bot.register_next_step_handler(msg, process_group_caption)
        return

    with LOCK:
        waiting_for_caption.pop(chat_id, None)

    ack = bot.send_message(chat_id, "⏳ ثواني وأرسلها لك مرتبة...")
    state['messages_to_delete'].append(ack.message_id)

    media_list = state['media_list']
    msgs = list(state['messages_to_delete'])

    def job():
        send_chunks(chat_id, media_list, final_caption)
        delete_quietly(chat_id, msgs)
        close_session(chat_id)  # ✅ خلصنا → تفريغ فوري للذاكرة

    send_queue.put(job)


def send_chunks(chat_id, media_list, caption):
    chunks = [media_list[i:i + 10] for i in range(0, len(media_list), 10)]
    for chunk in chunks:
        chunk[0].caption = caption
        try:
            if len(chunk) > 1:
                bot.send_media_group(chat_id, chunk)
            elif len(chunk) == 1:
                item = chunk[0]
                if isinstance(item, InputMediaPhoto):
                    bot.send_photo(chat_id, item.media, caption=item.caption)
                elif isinstance(item, InputMediaVideo):
                    bot.send_video(chat_id, item.media, caption=item.caption)
        except Exception as e:
            logger.error(f"خطأ إرسال ميديا: {e}")
        time.sleep(1.5)


def delete_quietly(chat_id, message_ids):
    for i in range(0, len(message_ids), 100):
        batch = message_ids[i:i + 100]
        try:
            bot.delete_messages(chat_id, batch)
        except Exception:
            for mid in batch:
                try:
                    bot.delete_message(chat_id, mid)
                except Exception:
                    pass


@bot.message_handler(content_types=['photo', 'video'])
def handle_media(message):
    chat_id = message.chat.id

    if message.photo:
        media_item = InputMediaPhoto(message.photo[-1].file_id)
    elif message.video:
        media_item = InputMediaVideo(message.video.file_id)
    else:
        return

    with LOCK:
        grp = media_groups.setdefault(chat_id, [])
        if len(grp) >= MAX_MEDIA_PER_GROUP:
            overflow = chat_id not in warned_overflow
            if overflow:
                warned_overflow.add(chat_id)
        else:
            grp.append({'media': media_item, 'message_id': message.message_id})
            overflow = False

        if chat_id in timers:
            timers[chat_id].cancel()
        t = threading.Timer(GROUP_DELAY, ask_for_caption, args=[chat_id])
        timers[chat_id] = t

    touch_session(chat_id)  # 🔑 ميديا جديدة = جلسة/تجديد 20 دقيقة
    t.start()

    if overflow:
        try:
            bot.send_message(chat_id, f"⚠️ الحد الأقصى للتجميعة {MAX_MEDIA_PER_GROUP} عنصر. الباقي أرسله بتجميعة جديدة.")
        except Exception:
            pass


# ----- 🚀 التشغيل -----
if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_sigterm)

    threading.Thread(target=run_web_server, daemon=True).start()
    threading.Thread(target=cleanup_loop, daemon=True).start()
    threading.Thread(target=sender_worker, daemon=True).start()

    logger.info("🤖 البوت يعمل الآن (جلسات حفظ 20 دقيقة + أداء خفيف).")
    bot.infinity_polling(timeout=60, long_polling=50, allowed_updates=["message"])
    logger.info("توقف البوت بشكل نظيف.")
