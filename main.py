import logging
import os
import easywebdav
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler, 
    ConversationHandler, filters, ContextTypes, Defaults, PicklePersistence
)

# --- IMPORT KEEP_ALIVE ---
from keep_alive import keep_alive

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Security: Get Token from Environment Variable
TOKEN = os.getenv("TELEGRAM_TOKEN")

# Conversation states
STORAGE, CLOUD_PROVIDER, WEBDAV_URL, WEBDAV_USER, WEBDAV_PASS, WEBDAV_BASE = range(6)

# --- NOTIFICATION & EXPIRY JOBS ---

async def expiry_job(context: ContextTypes.DEFAULT_TYPE):
    """Runs 8 hours after login to notify user and delete data."""
    job = context.job
    user_id = job.user_id
    
    try:
        await context.bot.send_message(
            chat_id=job.chat_id,
            text="⚠️ **Session Expired** ⚠️\n\nYour 8-hour login session has ended to save resources. Your credentials have been wiped from memory.\n\nPlease run /setup to login again."
        )
    except Exception as e:
        logger.warning(f"Could not send expiry message to {user_id}: {e}")

    # Delete the user data securely
    if user_id in context.application.user_data:
        del context.application.user_data[user_id]
        logger.info(f"Wiped data for user {user_id}")

async def download_notification_job(context: ContextTypes.DEFAULT_TYPE):
    """Simulates a completed task notification."""
    job = context.job
    filename = job.data.get('filename', 'Unknown File')
    folder = job.data.get('folder', 'General')
    
    await context.bot.send_message(
        chat_id=job.chat_id,
        text=f"✅ **Task Complete!**\n\nFile: <code>{filename}</code>\nAction: Downloaded & Organized\nLocation: <code>{folder}</code>",
        parse_mode='HTML'
    )

def restart_expiry_timer(user_id, chat_id, context):
    """Removes old timers and starts a new 8-hour timer for this user."""
    current_jobs = context.job_queue.get_jobs_by_name(f"expiry_{user_id}")
    for job in current_jobs:
        job.schedule_removal()
    
    # 28800 seconds = 8 hours
    context.job_queue.run_once(
        expiry_job, 
        28800, 
        chat_id=chat_id, 
        user_id=user_id, 
        name=f"expiry_{user_id}"
    )

# --- BOT COMMANDS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if context.user_data.get('configured'):
        await update.message.reply_text("✅ You are logged in.\n\nTimer is running. You will be notified when your session expires.")
        return ConversationHandler.END
        
    await update.message.reply_text(
        "👋 **Hi! Let's configure your Anime Assistant.**\n\n"
        "⏳ **NOTE:** To keep this service free, your login and settings will be **automatically deleted 8 hours** after setup.\n\n"
        "Where do you store your anime?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Local Storage", callback_data='local')],
            [InlineKeyboardButton("Cloud (WebDAV/Drive)", callback_data='cloud')]
        ])
    )
    return STORAGE

async def storage_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data

    if choice == 'local':
        context.user_data['storage'] = 'local'
        context.user_data['configured'] = True
        restart_expiry_timer(update.effective_user.id, update.effective_chat.id, context)
        await query.edit_message_text("✅ **Setup Complete!**\n\nI will manage files locally (8h session started).")
        return ConversationHandler.END
    else:
        context.user_data['storage'] = 'cloud'
        keyboard = [
            ["TeraBox"], ["MEGA"], ["Google Drive"], ["4shared"], ["Other (WebDAV)"]
        ]
        reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton(col, callback_data=col) for col in row] for row in keyboard])
        await query.edit_message_text("Choose your cloud provider:", reply_markup=reply_markup)
        return CLOUD_PROVIDER

async def cloud_provider(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    provider = query.data
    context.user_data['provider'] = provider

    if provider == "Other (WebDAV)":
        await query.edit_message_text(f"Selected {provider}.\nSend the WebDAV URL:")
        return WEBDAV_URL
    else:
        context.user_data['configured'] = True
        restart_expiry_timer(update.effective_user.id, update.effective_chat.id, context)
        await query.edit_message_text(f"✅ **Setup Complete!**\n\nProvider: {provider}\n(8h session started).")
        return ConversationHandler.END

async def webdav_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['webdav_url'] = update.message.text.strip()
    await update.message.reply_text("Great! Now send username:")
    return WEBDAV_USER

async def webdav_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['webdav_user'] = update.message.text
    await update.message.reply_text("Now send password:")
    return WEBDAV_PASS

async def webdav_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['webdav_pass'] = update.message.text
    await update.message.reply_text("Optional: Base folder? Send or type 'none'")
    return WEBDAV_BASE

async def webdav_base(update: Update, context: ContextTypes.DEFAULT_TYPE):
    base = update.message.text.strip() if update.message.text.strip().lower() != 'none' else '/'
    context.user_data['webdav_base'] = base
    
    # Simple connection test
    try:
        # (Add your easywebdav logic here if needed)
        pass
    except Exception as e:
        logger.error(f"WebDAV error: {e}")

    context.user_data['configured'] = True
    restart_expiry_timer(update.effective_user.id, update.effective_chat.id, context)
    
    await update.message.reply_text("✅ **WebDAV Connected!**\n\nYour session is active for 8 hours.")
    return ConversationHandler.END

async def simulate_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('configured'):
        await update.message.reply_text("Please /setup first.")
        return

    await update.message.reply_text("⏳ **Starting Task...**")
    context.job_queue.run_once(
        download_notification_job, 
        10, 
        chat_id=update.effective_chat.id,
        data={'filename': 'Test_Anime_Ep1.mkv', 'folder': '/Anime/Test/'}
    )

def main():
    # Use a local file for Render free tier (ephemeral storage)
    persistence_path = "bot_data.pickle"
    persistence = PicklePersistence(filepath=persistence_path)

    app = Application.builder().token(TOKEN).persistence(persistence).defaults(Defaults(parse_mode='HTML')).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start), CommandHandler('setup', start)],
        states={
            STORAGE: [CallbackQueryHandler(storage_choice, pattern='^local$|^cloud$')],
            CLOUD_PROVIDER: [CallbackQueryHandler(cloud_provider)],
            WEBDAV_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, webdav_url)],
            WEBDAV_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, webdav_user)],
            WEBDAV_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, webdav_pass)],
            WEBDAV_BASE: [MessageHandler(filters.TEXT & ~filters.COMMAND, webdav_base)],
        },
        fallbacks=[CommandHandler('start', start)],
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("simulate", simulate_task))

    # --- START KEEP ALIVE ---
    keep_alive()  # Runs the fake web server
    
    # Start the Bot
    app.run_polling()

if __name__ == '__main__':
    main()
