import os
import threading
import time
from flask import Flask
import telebot
from telebot.types import InputMediaVideo, InputMediaPhoto

# 🔑 سحب التوكن بأمان من متغيرات البيئة في Render
BOT_TOKEN = os.environ.get("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("⚠️ خطأ: لم يتم العثور على التوكن! تأكد من إضافته في إعدادات البيئة (Environment) في Render.")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# قاموس لتخزين الميديا حسب الـ chat_id
media_groups = {}
timers = {}

def send_buffered_media(chat_id):
    if chat_id in media_groups:
        media_list = media_groups[chat_id]
        
        # تنظيف الذاكرة فوراً عشان البوت يقدر يستقبل دفعات جديدة
        del media_groups[chat_id]
        if chat_id in timers:
            del timers[chat_id]

        # تقسيم القائمة الكبيرة إلى مجموعات، كل مجموعة فيها 10 مقاطع كحد أقصى
        chunks = [media_list[i:i + 10] for i in range(0, len(media_list), 10)]
        
        for chunk in chunks:
            try:
                if len(chunk) > 1:
                    bot.send_media_group(chat_id, chunk)
                elif len(chunk) == 1:
                    # إذا كان الباقي مقطع واحد فقط يرسله بالطريقة العادية
                    item = chunk[0]
                    if isinstance(item, InputMediaPhoto):
                        bot.send_photo(chat_id, item.media)
                    elif isinstance(item, InputMediaVideo):
                        bot.send_video(chat_id, item.media)
            except Exception as e:
                print(f"Error sending media group: {e}")
            
            # 🛑 مهم جداً: تأخير ثانية ونص بين كل ألبوم عشان تيليجرام ما يحظر البوت (Flood Control)
            time.sleep(1.5)

@bot.message_handler(content_types=['photo', 'video'])
def handle_media(message):
    chat_id = message.chat.id

    if message.photo:
        file_id = message.photo[-1].file_id
        media_item = InputMediaPhoto(file_id)
    elif message.video:
        file_id = message.video.file_id
        media_item = InputMediaVideo(file_id)
    else:
        return

    # إضافة المقطع لقائمة المستخدم
    if chat_id not in media_groups:
        media_groups[chat_id] = []

    media_groups[chat_id].append(media_item)

    # إلغاء المؤقت القديم وإعادة ضبطه (انتظار 3 ثواني لتجميع كل المقاطع مهما كان عددها)
    if chat_id in timers:
        timers[chat_id].cancel()

    timers[chat_id] = threading.Timer(3.0, send_buffered_media, args=[chat_id])
    timers[chat_id].start()

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
