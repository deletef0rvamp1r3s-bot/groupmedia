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

# قواميس لتخزين الميديا ومعلومات الرسائل الأصلية ومرحلة الكابشن
media_groups = {}
timers = {}
waiting_for_caption = {}

def ask_for_caption(chat_id):
    if chat_id in media_groups:
        media_list = [item['media'] for item in media_groups[chat_id]]
        message_ids_to_delete = [item['message_id'] for item in media_groups[chat_id]]
        
        # تنظيف الذاكرة المبدئية
        del media_groups[chat_id]
        if chat_id in timers:
            del timers[chat_id]

        # سؤال المستخدم عن الكابشن (تم تعديل الرسالة)
        msg = bot.send_message(chat_id, "وش تبي تحط كلام بالكابشن؟\n(أرسل 'بدون' لو تبي العبارة الأساسية بس)")
        message_ids_to_delete.append(msg.message_id)

        # نقل البيانات لقائمة الانتظار
        waiting_for_caption[chat_id] = {
            'media_list': media_list,
            'messages_to_delete': message_ids_to_delete
        }
        
        # توجيه الرد القادم من المستخدم لدالة الكابشن
        bot.register_next_step_handler(msg, process_group_caption)

def process_group_caption(message):
    chat_id = message.chat.id
    
    if chat_id not in waiting_for_caption:
        return
        
    state = waiting_for_caption[chat_id]
    media_list = state['media_list']
    messages_to_delete = state['messages_to_delete']
    
    # إضافة رسالة المستخدم (النص) لقائمة الحذف
    messages_to_delete.append(message.message_id)

    # التأكد أن المستخدم أرسل نص
    if not message.text:
        msg = bot.send_message(chat_id, "⚠️ الرجاء إرسال نص فقط. وش تبي تكتب بالكابشن؟")
        messages_to_delete.append(msg.message_id)
        bot.register_next_step_handler(msg, process_group_caption)
        return

    custom_text = message.text
    
    # الكابشن المعتمد بدون هاشتاق
    base_caption = "حصريات_@vamp1r3s"
    
    if custom_text != 'بدون':
        # وضع 3 نزولات سطر (\n\n\n) عشان يعطيك سطرين فاضية (سبيس x2) بين العبارتين
        final_caption = f"{base_caption}\n\n\n{custom_text}"
    else:
        final_caption = base_caption

    # تقسيم القائمة إلى مجموعات (10 مقاطع كحد أقصى)
    chunks = [media_list[i:i + 10] for i in range(0, len(media_list), 10)]
    
    for chunk in chunks:
        # وضع الكابشن على أول مقطع/صورة في كل مجموعة ميديا يتم إرسالها
        chunk[0].caption = final_caption
        
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
            print(f"Error sending media group: {e}")
        
        time.sleep(1.5)

    # 🧹 حذف كل الرسائل المفرقة (المقاطع الأصلية + سؤال البوت + النص حقك)
    try:
        for i in range(0, len(messages_to_delete), 100):
            bot.delete_messages(chat_id, messages_to_delete[i:i + 100])
    except Exception as e:
        print(f"Error deleting original messages: {e}")

    # تفريغ الذاكرة
    del waiting_for_caption[chat_id]

@bot.message_handler(content_types=['photo', 'video'])
def handle_media(message):
    chat_id = message.chat.id
    message_id = message.message_id

    if message.photo:
        file_id = message.photo[-1].file_id
        media_item = InputMediaPhoto(file_id)
    elif message.video:
        file_id = message.video.file_id
        media_item = InputMediaVideo(file_id)
    else:
        return

    if chat_id not in media_groups:
        media_groups[chat_id] = []

    media_groups[chat_id].append({'media': media_item, 'message_id': message_id})

    if chat_id in timers:
        timers[chat_id].cancel()

    # انتظار 3 ثواني لتجميع المقاطع، بعدها يطلب الكابشن
    timers[chat_id] = threading.Timer(3.0, ask_for_caption, args=[chat_id])
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
