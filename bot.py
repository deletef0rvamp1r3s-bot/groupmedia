import sys
import asyncio

if sys.version_info >= (3, 10):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

import os
import threading
import time
import logging
import queue

from flask import Flask
from pyrogram import Client, filters
from pyrogram.types import InputMediaPhoto, InputMediaVideo, InputMediaAnimation
from pyrogram.errors import FloodWait

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
for noisy in ("urllib3", "waitress", "pyrogram", "requests"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
logger = logging.getLogger("bot")

API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")

if not (API_ID and API_HASH and BOT_TOKEN):
    raise ValueError("⚠️ تأكد من إضافة المتغيرات الثلاثة في Environment في Render.")

tg = Client(
    name="vampires_bot",
    api_id=int(API_ID),
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
    workers=2
)

web = Flask(__name__)

# ----- الحالة -----
media_groups = {}        # chat_id -> قائمة العناصر المجمعة
kind_counts = {}         # chat_id -> عدادات الأنواع
status_msg = {}          # chat_id -> معرف رسالة الحالة
last_status_edit = {}    # chat_id -> آخر تحديث للحالة (منع الفيض)
waiting_for_caption = {}
session_timers = {}
last_activity = {}
warned_overflow = set()
LOCK = threading.Lock()

send_queue = queue.Queue()      # طابور الإرسال (دمج + رفع)
ingest_queue = queue.Queue()    # 🛡️ طابور الاستقبال التسلسلي (حماية السيرفر)

SESSION_TIMEOUT = 1200
MAX_MEDIA_PER_GROUP = 100
BASE_CAPTION = "#حصريات_VAMP1R3S"
START_TIME = time.time()


# ---------- 🌐 الصحة ----------
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


# ---------- أدوات آمنة ----------
def safe_call(func, *args, **kwargs):
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


# ---------- 🧠 الجلسات (20 دقيقة خمول) ----------
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
        had_state = bool(media_groups.get(chat_id)) or chat_id in waiting_for_caption
        items = media_groups.pop(chat_id, None)
        waiting_for_caption.pop(chat_id, None)
        kind_counts.pop(chat_id, None)
        st_id = status_msg.pop(chat_id, None)
        last_status_edit.pop(chat_id, None)
        st = session_timers.pop(chat_id, None)
        if st:
            st.cancel()
        last_activity.pop(chat_id, None)
        warned_overflow.discard(chat_id)
    if had_state:
        if st_id:
            safe_call(tg.delete_messages, chat_id, st_id)
        logger.info(f"🧠 شات {chat_id}: 20 دقيقة بدون نشاط → الذاكرة صفر.")
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
            stale = [cid for cid, ts in last_activity.items() if now - ts > SESSION_TIMEOUT + 120]
        for cid in stale:
            purge_chat(cid, notify=False)


# ---------- 🛡️ طابور الاستقبال التسلسلي ----------
def ingest_worker():
    """يتعرف على أول مقطع، والباقي ينتظرون دوره... واحد واحد."""
    while True:
        chat_id, kind, file_id, mid = ingest_queue.get()
        try:
            process_ingest(chat_id, kind, file_id, mid)
        except Exception as e:
            logger.error(f"خطأ بالاستقبال: {e}")
        finally:
            ingest_queue.task_done()


def status_text(chat_id, count):
    with LOCK:
        c = dict(kind_counts.get(chat_id, {}))
    return (f"✅ تم التعرف على {count} عنصر\n"
            f"📸 صور: {c.get('photo', 0)} | 🎬 مقاطع: {c.get('video', 0)} | 🎞 GIF: {c.get('gif', 0)}\n\n"
            f"➕ أرسل المزيد وسيُضاف تلقائيًا\n"
            f"🔀 /merge — دمج وإرسال\n"
            f"🗑 /clear — مسح القائمة")


def process_ingest(chat_id, kind, file_id, mid):
    maker = {'photo': InputMediaPhoto, 'video': InputMediaVideo, 'gif': InputMediaAnimation}[kind]
    item = maker(file_id)

    with LOCK:
        if chat_id in waiting_for_caption:
            mode = 'caption_wait'
        else:
            grp = media_groups.setdefault(chat_id, [])
            if len(grp) >= MAX_MEDIA_PER_GROUP:
                mode = 'overflow' if chat_id not in warned_overflow else 'full'
                if mode == 'overflow':
                    warned_overflow.add(chat_id)
            else:
                grp.append({'media': item, 'message_id': mid})
                counts = kind_counts.setdefault(chat_id, {'photo': 0, 'video': 0, 'gif': 0})
                counts[kind] += 1
                mode = 'ok'
            count = len(grp)
            st_id = status_msg.get(chat_id)
            last = last_status_edit.get(chat_id, 0)

    if mode == 'caption_wait':
        safe_call(tg.send_message, chat_id, "⚠️ أنت الآن بوضع كتابة الكابشن. أرسل الكابشن أو /cancel أول.")
        return
    if mode == 'overflow':
        safe_call(tg.send_message, chat_id, f"⚠️ وصلت الحد الأقصى ({MAX_MEDIA_PER_GROUP} عنصر). أرسل /merge أو /clear.")
        return
    if mode == 'full':
        return

    touch_session(chat_id)
    now = time.time()
    text = status_text(chat_id, count)

    if st_id is None:
        m = safe_call(tg.send_message, chat_id, text)
        if m:
            with LOCK:
                status_msg[chat_id] = m.id
                last_status_edit[chat_id] = now
    elif now - last >= 2 or ingest_queue.empty():
        # تحديث مباشر للعدد (مع حماية من الفيض + تحديث نهائي دقيق)
        if safe_call(tg.edit_message_text, chat_id, st_id, text) is not None:
            with LOCK:
                last_status_edit[chat_id] = now


# ---------- 🔀 الأوامر ----------
def handle_merge(message):
    chat_id = message.chat.id
    with LOCK:
        items = media_groups.pop(chat_id, None)
        kind_counts.pop(chat_id, None)
        st_id = status_msg.pop(chat_id, None)
        last_status_edit.pop(chat_id, None)
        warned_overflow.discard(chat_id)
    if not items:
        safe_call(tg.send_message, chat_id, "🤷 ما عندك قائمة حاليًا. أرسل صور/مقاطع/GIF أول.")
        return

    msgs = [it['message_id'] for it in items]
    if st_id:
        msgs.append(st_id)
    msgs.append(message.id)

    q = safe_call(tg.send_message, chat_id,
                  f"✅ جاهز للدمج: {len(items)} عنصر.\nوش تبي تحط كلام بالكابشن؟\n(أرسل 'بدون' لو تبي الهاشتاق بس)")
    if not q:
        return
    msgs.append(q.id)

    with LOCK:
        waiting_for_caption[chat_id] = {
            'media_list': [it['media'] for it in items],
            'messages_to_delete': msgs,
            'created_at': time.time()
        }
    touch_session(chat_id)


def handle_clear(message):
    chat_id = message.chat.id
    with LOCK:
        items = media_groups.pop(chat_id, None)
        kind_counts.pop(chat_id, None)
        st_id = status_msg.pop(chat_id, None)
        last_status_edit.pop(chat_id, None)
        warned_overflow.discard(chat_id)
    n = len(items) if items else 0
    ids = [it['message_id'] for it in items] if items else []
    if st_id:
        ids.append(st_id)
    ids.append(message.id)
    delete_quietly(chat_id, ids)
    safe_call(tg.send_message, chat_id, f"🗑️ تم مسح القائمة ({n} عنصر). تقدر ترسل من جديد.")
    close_session(chat_id)


# ---------- 📝 الكابشن والإرسال ----------
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
        delete_quietly(chat_id, state['messages_to_delete'])
        safe_call(tg.send_message, chat_id, "❌ تم إلغاء العملية.")
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

    # رسالة التقدم (تتحول لشريط حي أثناء الدمج)
    ack = safe_call(tg.send_message, chat_id, "⏳ جاري تجهيز الدمج...")
    if not ack:
        return

    media_list = state['media_list']
    msgs = list(state['messages_to_delete'])

    def job():
        send_chunks(chat_id, media_list, final_caption, ack.id)
        delete_quietly(chat_id, msgs)
        close_session(chat_id)

    send_queue.put(job)


def build_bar(done, total):
    pct = int(done * 100 / total) if total else 100
    filled = int(20 * done / total) if total else 20
    bar = '█' * filled + '░' * (20 - filled)
    return (f"📤 **جاري الدمج والإرسال...**\n`{bar}`\n📊 **{pct}%** | 📦 {done} من {total}")


def send_chunks(chat_id, media_list, caption, progress_msg_id):
    total = len(media_list)
    done = 0
    chunks = [media_list[i:i + 10] for i in range(0, total, 10)]

    for chunk in chunks:
        chunk[0].caption = caption
        ok = None
        if len(chunk) > 1:
            ok = safe_call(tg.send_media_group, chat_id, chunk)
            if ok:
                done += len(chunk)
        elif len(chunk) == 1:
            item = chunk[0]
            if isinstance(item, InputMediaPhoto):
                ok = safe_call(tg.send_photo, chat_id, item.media, caption=item.caption)
            elif isinstance(item, InputMediaVideo):
                ok = safe_call(tg.send_video, chat_id, item.media, caption=item.caption)
            elif isinstance(item, InputMediaAnimation):
                ok = safe_call(tg.send_animation, chat_id, item.media, caption=item.caption)
            if ok:
                done += 1
        # 📊 تحديث الشريط الحي مع كل دفعة تخلص
        safe_call(tg.edit_message_text, chat_id, progress_msg_id, build_bar(done, total))
        time.sleep(1.5)

    safe_call(tg.edit_message_text, chat_id, progress_msg_id,
              f"✅ **اكتمل الدمج والإرسال بنجاح!**\n📦 عدد العناصر: {total}")


def delete_quietly(chat_id, message_ids):
    for i in range(0, len(message_ids), 100):
        batch = message_ids[i:i + 100]
        if safe_call(tg.delete_messages, chat_id, batch) is None:
            for mid in batch:
                safe_call(tg.delete_messages, chat_id, mid)


def sender_worker():
    while True:
        job = send_queue.get()
        try:
            job()
        except Exception as e:
            logger.error(f"خطأ في مهمة إرسال: {e}")
        finally:
            send_queue.task_done()


# ---------- 📥 المستقبلات ----------
@tg.on_message(filters.media)
def handle_media(client, message):
    """خفيف جدًا: يحط بالطابور ويرجع فورًا — الحماية من الانهيار."""
    if message.photo:
        kind, fid = 'photo', message.photo.file_id
    elif message.video:
        kind, fid = 'video', message.video.file_id
    elif message.animation:
        kind, fid = 'gif', message.animation.file_id
    else:
        return
    ingest_queue.put((message.chat.id, kind, fid, message.id))


@tg.on_message(filters.text & ~filters.bot)
def handle_text(client, message):
    t = (message.text or '').strip().lower()
    if t.startswith('/merge') or t in ('دمج', 'ادمج'):
        handle_merge(message)
        return
    if t.startswith('/clear') or t in ('مسح',):
        handle_clear(message)
        return
    with LOCK:
        waiting = message.chat.id in waiting_for_caption
    if waiting:
        process_group_caption(message)


# ---------- 🚀 التشغيل ----------
if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    threading.Thread(target=cleanup_loop, daemon=True).start()
    threading.Thread(target=ingest_worker, daemon=True).start()
    threading.Thread(target=sender_worker, daemon=True).start()

    async def main():
        await tg.start()
        logger.info("🤖 البوت يعمل الآن (طابور تسلسلي + /merge + /clear + Progress Bar).")
        await asyncio.Event().wait()

    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        loop.run_until_complete(tg.stop())
