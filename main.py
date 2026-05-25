import os
import logging
from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN environment variable not set!")

# message_id -> user_id mapping
message_map = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command handler"""
    await update.message.reply_text(
        "🎯 Kya promote karvana hai? Media ya text bhejo."
    )

async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all user messages"""
    user = update.effective_user
    msg = update.message

    if not msg:
        return

    # Admin ka reply handling
    if user.id == ADMIN_ID and msg.reply_to_message:
        replied_msg_id = msg.reply_to_message.message_id

        if replied_msg_id in message_map:
            target_user = message_map[replied_msg_id]

            try:
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

                logger.info(f"✅ Message sent to user {target_user}")
                await msg.reply_text("✅ Message user ko bhej diya gaya!")
                
            except Exception as e:
                logger.error(f"❌ Error sending message: {e}")
                await msg.reply_text(f"❌ Error: {str(e)}")
            
            return

    # User message admin ko copy karo (identity hidden)
    try:
        sent = await context.bot.copy_message(
            chat_id=ADMIN_ID,
            from_chat_id=msg.chat_id,
            message_id=msg.message_id
        )

        # mapping save
        message_map[sent.message_id] = user.id
        logger.info(f"📨 Message from {user.id} copied to admin")

        await msg.reply_text(
            "✅ Request admin ko bhej di gayi hai!\n⏳ Jaldi reply mil jayega!"
        )

    except Exception as e:
        logger.error(f"❌ Error copying message: {e}")
        await msg.reply_text(f"❌ Error: {str(e)}")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a telegram message to notify the developer."""
    logger.error(f"Exception while handling an update: {context.error}")

def main():
    """Start the bot."""
    logger.info("🤖 Starting bot...")
    
    # Create the Application
    app = Application.builder().token(BOT_TOKEN).build()

    # Add handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.ALL, handle_user_message))
    
    # Add error handler
    app.add_error_handler(error_handler)

    # Start polling
    logger.info("🚀 Bot started! Listening for messages...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
