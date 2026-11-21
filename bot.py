import os
import asyncio
import threading
import time
import logging
import queue

from flask import Flask
from pyrogram import Client, filters, idle
from pyrogram.types import InputMediaPhoto, InputMediaVideo
from pyrogram.errors import FloodWait

# ----- لوقات خفيفة -----
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
for noisy in ("urllib3", "waitress", "pyrogram", "requests"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
logger = logging.getLogger("bot")

# 🔑 الثلاثة من متغيرات البيئة في Render
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")

if not (API_ID and API_HASH and BOT_TOKEN):
    raise ValueError("⚠️ تأكد من إضافة API_ID و API_HASH و BOT_TOKEN في Environment في Render.")

# ----- عميل تليجرام (MTProto) بجلسة داخل الذاكرة -----
tg = Client(
    name="vampires_bot",
    api_id=int(API_ID),
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
    workers=2
)

web = Flask(__name__)

# ----- الحالة + الحماية -----
media_groups = {}
timers = {}
waiting_for_caption = {}
session_timers = {}
last_activity = {}
warned_overflow = set()
LOCK = threading.Lock()

send_queue = queue.Queue()

GROUP_DELAY = 3.0
SESSION_TIMEOUT = 1200
MAX_MEDIA_PER_GROUP = 100
BASE_CAPTION = "#حصريات_VAMP1R3S"
START_TIME = time.time()


# ---------- 🌐 نقطة الصحة (UptimeRobot) ----------
@web.route('/')
def index():
    return f"OK - uptime {int(time.time() - START_TIME)}s", 200


def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    try:
        from waitress import serve
        serve(web, host="0.0.0.0", port=port, threads=2)
    except ImportError:
        web.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)


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


def safe_call(func, *args, **kwargs):
    """محاولات مع احترام FloodWait."""
    for _ in range(3):
        try:
            return func(*args, **kwargs)
        except FloodWait as fw:
            logger.warning(f"FloodWait: انتظار {fw.value} ثانية...")
            time.sleep(fw.value + 1)
        except Exception as e:
            logger.error(f"خطأ API: {e}")
            return None
    return None


# ---------- 🧠 إدارة الجلسات (20 دقيقة) ----------
def touch_session(chat_id):
    with LOCK:
        last_activity[chat_id] = time.time()
        old = session_timers.pop(chat_id, None)
        if old:
            old.cancel()
        t = threading.Timer(SESSION_TIMEOUT, purge_chat, args=[chat_id])
        session_timers[chat_id] = t
    t.start()


def purge_chat(chat_id, notify=True):
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
        logger.info(f"🧠 شات {chat_id}: 20 دقيقة بدون نشاط → الذاكرة صفر، خامل.")
        if notify:
            safe_call(tg.send_message, chat_id,
                      "⏰ انتهت جلسة الحفظ (20 دقيقة بدون نشاط) وتم تفريغ الذاكرة.\nأرسل مقاطعك من جديد متى ما تبي.")


def close_session(chat_id):
    with LOCK:
        st = session_timers.pop(chat_id, None)
        if st:
            st.cancel()
        last_activity.pop(chat_id, None)
        warned_overflow.discard(chat_id)


def cleanup_loop():
    while True:
        time.sleep(300)
        now = time.time()
        with LOCK:
            stale = [cid for cid, ts in last_activity.items()
                     if now - ts > SESSION_TIMEOUT + 120]
        for cid in stale:
            purge_chat(cid, notify=False)


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

    msg = safe_call(tg.send_message, chat_id,
                    "وش تبي تحط كلام بالكابشن؟\n(أرسل 'بدون' لو تبي الهاشتاق بس)")
    if not msg:
        return

    messages_to_delete.append(msg.id)
    with LOCK:
        waiting_for_caption[chat_id] = {
            'media_list': media_list,
            'messages_to_delete': messages_to_delete,
            'created_at': time.time()
        }


def process_group_caption(message):
    chat_id = message.chat.id
    with LOCK:
        state = waiting_for_caption.get(chat_id)
    if not state:
        return

    touch_session(chat_id)
    state['messages_to_delete'].append(message.id)
    text = message.text

    if text and text.strip() in ('إلغاء', '/cancel'):
        with LOCK:
            waiting_for_caption.pop(chat_id, None)
        m = safe_call(tg.send_message, chat_id, "❌ تم إلغاء العملية.")
        if m:
            state['messages_to_delete'].append(m.id)
        delete_quietly(chat_id, state['messages_to_delete'])
        close_session(chat_id)
        return

    if not text:
        msg = safe_call(tg.send_message, chat_id, "⚠️ الرجاء إرسال نص فقط. وش تبي تكتب بالكابشن؟")
        if msg:
            state['messages_to_delete'].append(msg.id)
        return

    custom_text = text.strip()
    final_caption = BASE_CAPTION if custom_text == 'بدون' else f"{BASE_CAPTION}\n\n\n{custom_text}"

    if len(final_caption) > 1024:
        msg = safe_call(tg.send_message, chat_id, "⚠️ الكابشن طويل جدًا (الحد 1024 حرف). أرسل أقصر أو 'بدون'.")
        if msg:
            state['messages_to_delete'].append(msg.id)
        return

    with LOCK:
        waiting_for_caption.pop(chat_id, None)

    ack = safe_call(tg.send_message, chat_id, "⏳ ثواني وأرسلها لك مرتبة...")
    if ack:
        state['messages_to_delete'].append(ack.id)

    media_list = state['media_list']
    msgs = list(state['messages_to_delete'])

    def job():
        send_chunks(chat_id, media_list, final_caption)
        delete_quietly(chat_id, msgs)
        close_session(chat_id)

    send_queue.put(job)


def send_chunks(chat_id, media_list, caption):
    chunks = [media_list[i:i + 10] for i in range(0, len(media_list), 10)]
    for chunk in chunks:
        chunk[0].caption = caption
        if len(chunk) > 1:
            safe_call(tg.send_media_group, chat_id, chunk)
        elif len(chunk) == 1:
            item = chunk[0]
            if isinstance(item, InputMediaPhoto):
                safe_call(tg.send_photo, chat_id, item.media, caption=item.caption)
            elif isinstance(item, InputMediaVideo):
                safe_call(tg.send_video, chat_id, item.media, caption=item.caption)
        time.sleep(1.5)


def delete_quietly(chat_id, message_ids):
    for i in range(0, len(message_ids), 100):
        batch = message_ids[i:i + 100]
        if safe_call(tg.delete_messages, chat_id, batch) is None:
            for mid in batch:
                safe_call(tg.delete_messages, chat_id, mid)


# ---------- 📥 مستقبلات الرسائل ----------
@tg.on_message(filters.photo | filters.video)
def handle_media(client, message):
    chat_id = message.chat.id

    if message.photo:
        item = InputMediaPhoto(message.photo.file_id)
    elif message.video:
        item = InputMediaVideo(message.video.file_id)
    else:
        return

    with LOCK:
        state = waiting_for_caption.get(chat_id)
        if state:
            if len(state['media_list']) < MAX_MEDIA_PER_GROUP:
                state['media_list'].append(item)
                state['messages_to_delete'].append(message.id)
            return

        grp = media_groups.setdefault(chat_id, [])
        if len(grp) >= MAX_MEDIA_PER_GROUP:
            overflow = chat_id not in warned_overflow
            if overflow:
                warned_overflow.add(chat_id)
        else:
            grp.append({'media': item, 'message_id': message.id})
            overflow = False

        if chat_id in timers:
            timers[chat_id].cancel()
        t = threading.Timer(GROUP_DELAY, ask_for_caption, args=[chat_id])
        timers[chat_id] = t

    touch_session(chat_id)
    t.start()

    if overflow:
        safe_call(tg.send_message, chat_id,
                  f"⚠️ الحد الأقصى للتجميعة {MAX_MEDIA_PER_GROUP} عنصر. الباقي أرسله بتجميعة جديدة.")


@tg.on_message(filters.text & ~filters.bot)
def handle_text(client, message):
    with LOCK:
        waiting = message.chat.id in waiting_for_caption
    if waiting:
        process_group_caption(message)


# ----- 🚀 التشغيل -----
if __name__ == "__main__":
    # 🔧 إصلاح مشكلة Event Loop في Python 3.10+ على Render
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    threading.Thread(target=run_web_server, daemon=True).start()
    threading.Thread(target=cleanup_loop, daemon=True).start()
    threading.Thread(target=sender_worker, daemon=True).start()

    tg.start()
    logger.info("🤖 البوت يعمل الآن (Pyrogram MTProto + جلسات 20 دقيقة).")

    idle()
    tg.stop()
    logger.info("توقف البوت بشكل نظيف.")
