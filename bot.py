import os
import telebot
from telebot.types import InputMediaVideo, InputMediaPhoto

# 🔑 سحب التوكن بأمان من متغيرات البيئة في Render
BOT_TOKEN = os.environ.get("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("⚠️ خطأ: لم يتم العثور على التوكن! تأكد من إضافته في إعدادات البيئة (Environment) في Render.")

bot = telebot.TeleBot(BOT_TOKEN)

# ذاكرة مؤقتة لحفظ المقاطع قبل الإرسال
user_media_store = {}

@bot.message_handler(commands=['start'])
def start_cmd(message):
    bot.reply_to(
        message, 
        "👋 أهلاً بك!\n\n"
        "1️⃣ أرسل لي المقاطع والصور مفرقة واحد تلو الآخر.\n"
        "2️⃣ بعد الانتهاء أرسل `/send` لدمجها في ألبوم واحد وإرسالها.\n"
        "3️⃣ إذا أردت إلغاء القائمة والبدء من جديد أرسل `/clear`."
    )

# 📥 استقبال الصور والفيديوهات وحفظها
@bot.message_handler(content_types=['photo', 'video'])
def handle_media(message):
    user_id = message.from_user.id
    
    if user_id not in user_media_store:
        user_media_store[user_id] = []
        
    if len(user_media_store[user_id]) >= 10:
        bot.reply_to(message, "⚠️ وصلت للحد الأقصى (10 مقاطع)! أرسل `/send` للدمج الآن.")
        return

    if message.photo:
        file_id = message.photo[-1].file_id
        user_media_store[user_id].append(InputMediaPhoto(file_id))
    elif message.video:
        file_id = message.video.file_id
        user_media_store[user_id].append(InputMediaVideo(file_id))
        
    current_count = len(user_media_store[user_id])
    bot.reply_to(message, f"📥 تم حفظ المقطع ({current_count}/10). أرسل التالي أو اكتب `/send` للدمج.")

# 🚀 دمج وإرسال الألبوم
@bot.message_handler(commands=['send'])
def send_album(message):
    user_id = message.from_user.id
    media_list = user_media_store.get(user_id, [])
    
    if len(media_list) < 2:
        bot.reply_to(message, "⚠️ يجب إرسال مقطعين أو صورتين على الأقل لتشكيل ألبوم!")
        return
        
    try:
        bot.send_message(message.chat.id, "⏳ جاري دمج المقاطع وإرسال الألبوم...")
        bot.send_media_group(message.chat.id, media_list)
        user_media_store[user_id] = []
        bot.send_message(message.chat.id, "✅ تم الإرسال بنجاح وتفريغ الذاكرة!")
        
    except Exception as e:
        bot.reply_to(message, f"❌ حدث خطأ أثناء الإرسال: {e}")

# 🗑️ مسح القائمة الحالية
@bot.message_handler(commands=['clear'])
def clear_media(message):
    user_id = message.from_user.id
    user_media_store[user_id] = []
    bot.reply_to(message, "🗑️ تم مسح جميع المقاطع المحفوظة.")

print("🤖 البوت يعمل الآن بأمان من خلال Render...")
bot.infinity_polling()
