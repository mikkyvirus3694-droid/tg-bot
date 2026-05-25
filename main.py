import os
from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))

# message_id -> user_id mapping (admin ke message id ko track karne ke liye)
message_map = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Kya promote karvana hai? Media ya text bhejo."
    )

async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.message

    # Admin ka reply handling
    if user.id == ADMIN_ID and msg.reply_to_message:
        replied_msg_id = msg.reply_to_message.message_id

        if replied_msg_id in message_map:
            target_user = message_map[replied_msg_id]

            # text
            if msg.text:
                await context.bot.send_message(
                    chat_id=target_user,
                    text=msg.text
                )

            # photo
            elif msg.photo:
                await context.bot.send_photo(
                    chat_id=target_user,
                    photo=msg.photo[-1].file_id,
                    caption=msg.caption or ""
                )

            # video
            elif msg.video:
                await context.bot.send_video(
                    chat_id=target_user,
                    video=msg.video.file_id,
                    caption=msg.caption or ""
                )
            
            # document
            elif msg.document:
                await context.bot.send_document(
                    chat_id=target_user,
                    document=msg.document.file_id,
                    caption=msg.caption or ""
                )

            await msg.reply_text("✅ Message user ko bhej diya gaya!")
            return

    # User message admin ko copy (identity hidden - better approach)
    sent = await context.bot.copy_message(
        chat_id=ADMIN_ID,
        from_chat_id=msg.chat_id,
        message_id=msg.message_id
    )

    # mapping save - admin ke paas jo message gaya uska ID track karo
    message_map[sent.message_id] = user.id

    await msg.reply_text(
        "✅ Request admin ko bhej di gayi hai. Jaldi reply mil jayega!"
    )

def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN environment variable not set!")
    
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.ALL, handle_user_message))

    print("🤖 Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
