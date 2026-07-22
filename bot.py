import os
import threading
from flask import Flask
import telebot
from telebot.types import InputMediaVideo, InputMediaPhoto

# 🔑 سحب التوكن بأمان من متغيرات البيئة في Render
BOT_TOKEN = os.environ.get("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("⚠️ خطأ: لم يتم العثور على التوكن! تأكد من إضافته في إعدادات البيئة (Environment) في Render.")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# قاموس لتخزين الميديا حسب الـ media_group_id أو الـ chat_id
media_groups = {}
timers = {}

def send_buffered_media(chat_id, group_id):
    if group_id in media_groups:
        media_list = media_groups[group_id]
        try:
            if len(media_list) > 1:
                bot.send_media_group(chat_id, media_list)
            elif len(media_list) == 1:
                # إذا طلع مقطع واحد فقط يرسله بالطريقة العادية
                item = media_list[0]
                if isinstance(item, InputMediaPhoto):
                    bot.send_photo(chat_id, item.media)
                elif isinstance(item, InputMediaVideo):
                    bot.send_video(chat_id, item.media)
        except Exception as e:
            print(f"Error sending media group: {e}")
        
        # تنظيف الذاكرة بعد الإرسال
        del media_groups[group_id]
        if group_id in timers:
            del timers[group_id]

@bot.message_handler(content_types=['photo', 'video'])
def handle_media(message):
    chat_id = message.chat.id
    # إذا كانت الرسالة أصلًا جزءاً من قروب ميديا في تيليجرام
    group_id = message.media_group_id if message.media_group_id else f"single_{message.chat.id}_{message.message_id}"

    if message.photo:
        file_id = message.photo[-1].file_id
        media_item = InputMediaPhoto(file_id)
    elif message.video:
        file_id = message.video.file_id
        media_item = InputMediaVideo(file_id)
    else:
        return

    if group_id not in media_groups:
        media_groups[group_id] = []

    media_groups[group_id].append(media_item)

    # إلغاء المؤقت القديم وإعادة ضبطه (انتظار ثانيتين لتجميع كل المقاطع المرسلة دفعة واحدة)
    if group_id in timers:
        timers[group_id].cancel()

    timers[group_id] = threading.Timer(2.0, send_buffered_media, args=[chat_id, group_id])
    timers[group_id].start()

# ----- إعدادات السيرفر الوهمي لـ Render -----
@app.route('/')
def index():
    return "البوت يعمل 24 ساعة بأمان!"

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, use_reloader=False)

if __name__ == "__main__":
    t = threading.Thread(target=run_web_server)
    t.daemon = True
    t.start()
    print("🤖 بوت تجميع القروب ميديا يعمل الآن...")
    bot.infinity_polling()
