import logging
import os
import easywebdav
import dropbox
import httpx
from mega import Mega
from bs4 import BeautifulSoup
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

TOKEN = os.getenv("TELEGRAM_TOKEN")

# Conversation states
STORAGE, CLOUD_PROVIDER, CREDENTIALS_1, CREDENTIALS_2 = range(4)

# --- 1. NOTIFICATION & EXPIRY LOGIC ---

async def expiry_job(context: ContextTypes.DEFAULT_TYPE):
    """Runs 8 hours after login to delete data."""
    job = context.job
    user_id = job.user_id
    try:
        await context.bot.send_message(
            chat_id=job.chat_id,
            text="⚠️ **Session Expired** ⚠️\n\nYour 8-hour login session has ended. Credentials wiped."
        )
    except Exception:
        pass

    if user_id in context.application.user_data:
        del context.application.user_data[user_id]
        logger.info(f"Wiped data for user {user_id}")

def restart_expiry_timer(user_id, chat_id, context):
    """Starts 8-hour timer."""
    if not context.job_queue:
        return # Timer system offline

    current_jobs = context.job_queue.get_jobs_by_name(f"expiry_{user_id}")
    for job in current_jobs:
        job.schedule_removal()
    
    context.job_queue.run_once(expiry_job, 28800, chat_id=chat_id, user_id=user_id, name=f"expiry_{user_id}")

async def download_notification_job(context: ContextTypes.DEFAULT_TYPE):
    """Simulates a completed task notification."""
    job = context.job
    filename = job.data.get('filename', 'Unknown File')
    folder = job.data.get('folder', 'General')
    
    await context.bot.send_message(
        chat_id=job.chat_id,
        text=f"✅ **Task Complete!**\n\nFile: <code>{filename}</code>\nLocation: <code>{folder}</code>",
        parse_mode='HTML'
    )

async def simulate_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Test command for notifications."""
    # 1. Check if user is logged in
    if not context.user_data.get('configured'):
        await update.message.reply_text("Please /setup first.")
        return
    
    # 2. Check if JobQueue is active (Fixes the 'not working' issue)
    if not context.job_queue:
        await update.message.reply_text("❌ **System Error:** The Timer module is missing.\n\nPlease add `APScheduler` to your requirements.txt file.")
        return

    await update.message.reply_text("⏳ **Simulating Download...** (I will notify you in 5 seconds)")
    
    # 3. Schedule the fake notification
    context.job_queue.run_once(
        download_notification_job, 
        5, 
        chat_id=update.effective_chat.id, 
        data={'filename': 'One Piece - 1080.mkv', 'folder': '/Anime/One Piece/'}
    )

# --- 2. SEARCH (MIRROR ROTATION FIX) ---

async def search_anime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Scrapes Nyaa.si mirrors for magnet links."""
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("🔎 Usage: `/search One Piece`")
        return

    status_msg = await update.message.reply_text(f"🔍 Searching: <b>{query}</b>...", parse_mode='HTML')
    
    # 3 Mirrors to try if one is blocked
    mirrors = ["https://nyaa.si", "https://nyaa.iss.one", "https://nyaa.land"]
    
    # Browser Headers
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/91.0.4472.124 Safari/537.36",
        "Referer": "https://google.com"
    }

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        response = None
        success_url = ""

        # Try mirrors one by one
        for domain in mirrors:
            try:
                url = f"{domain}/?f=0&c=0_0&q={query}&s=seeders&o=desc"
                response = await client.get(url, headers=headers)
                if response.status_code == 200:
                    success_url = domain
                    break
            except Exception:
                continue

    if not response or response.status_code != 200:
        await status_msg.edit_text("⚠️ **Search Failed.**\nAll mirrors are currently blocking the bot's IP.")
        return

    # Parse Results
    try:
        soup = BeautifulSoup(response.text, 'html.parser')
        rows = soup.select('tr.default, tr.success')[:5]

        if not rows:
            await status_msg.edit_text(f"❌ No results found on {success_url}.")
            return

        message = f"<b>Top 5 Results for '{query}':</b>\n\n"
        keyboard = []

        for i, row in enumerate(rows):
            cols = row.find_all('td')
            title = cols[1].find('a', class_=lambda x: x != 'comments').text.strip()
            size = cols[3].text.strip()
            magnet = cols[2].find_all('a')[1]['href']
            
            short_title = (title[:25] + '..') if len(title) > 25 else title
            message += f"{i+1}. <b>{short_title}</b> [{size}]\n"
            
            magnet_key = f"magnet_{update.effective_user.id}_{i}"
            context.user_data[magnet_key] = magnet
            keyboard.append([InlineKeyboardButton(f"🧲 Magnet {i+1}", callback_data=magnet_key)])

        reply_markup = InlineKeyboardMarkup(keyboard)
        await status_msg.edit_text(message, reply_markup=reply_markup, parse_mode='HTML')

    except Exception as e:
        logger.error(f"Search error: {e}")
        await status_msg.edit_text("⚠️ Error parsing results.")

async def magnet_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    magnet_key = query.data
    magnet_link = context.user_data.get(magnet_key)
    await query.answer()
    if magnet_link:
        await query.message.reply_text(f"🧲 <b>Magnet:</b>\n\n<code>{magnet_link}</code>", parse_mode='HTML')
    else:
        await query.message.reply_text("⚠️ Link expired.")

async def anime_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("ℹ️ Usage: `/info Naruto`")
        return
    url = f"https://api.jikan.moe/v4/anime?q={query}&limit=1"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
             response = await client.get(url)
             data = response.json()
        if data.get('data'):
            anime = data['data'][0]
            caption = f"🎬 <b>{anime['title']}</b>\n⭐ {anime.get('score', 'N/A')} | 📺 {anime.get('episodes', '?')} eps\n\n{anime.get('synopsis', '')[:200]}..."
            await update.message.reply_photo(photo=anime['images']['jpg']['image_url'], caption=caption, parse_mode='HTML')
        else:
            await update.message.reply_text("❌ Not found.")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}")

# --- 3. SETUP & CREDENTIALS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('configured'):
        await update.message.reply_text("✅ You are logged in.")
        return ConversationHandler.END
    await update.message.reply_text(
        "👋 **Anime Assistant Bot**\nSelect Storage:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📂 Local", callback_data='local')],
            [InlineKeyboardButton("☁️ Cloud", callback_data='cloud')]
        ])
    )
    return STORAGE

async def storage_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == 'local':
        context.user_data['configured'] = True
        restart_expiry_timer(update.effective_user.id, update.effective_chat.id, context)
        await query.edit_message_text("✅ **Local Setup Complete!**")
        return ConversationHandler.END
    else:
        keyboard = [
            [InlineKeyboardButton("🔴 MEGA", callback_data='mega')],
            [InlineKeyboardButton("🔵 Dropbox", callback_data='dropbox')],
            [InlineKeyboardButton("📢 Telegram", callback_data='telegram')],
            [InlineKeyboardButton("🌐 WebDAV", callback_data='webdav')]
        ]
        await query.edit_message_text("Select Cloud:", reply_markup=InlineKeyboardMarkup(keyboard))
        return CLOUD_PROVIDER

async def cloud_provider(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    provider = query.data
    context.user_data['provider'] = provider
    
    prompts = {
        'mega': "Send **MEGA Email**:",
        'dropbox': "Send **Dropbox Token**:",
        'telegram': "Forward a message from your channel:",
        'webdav': "Send **WebDAV URL**:"
    }
    await query.edit_message_text(prompts.get(provider, "Error"))
    return CREDENTIALS_1

async def handle_credentials_1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    provider = context.user_data.get('provider')
    text = update.message.text.strip() if update.message.text else ""
    
    if provider == 'mega':
        context.user_data['mega_email'] = text
        await update.message.reply_text("Send **MEGA Password**:")
        return CREDENTIALS_2
    elif provider == 'dropbox':
        try:
            dropbox.Dropbox(text).users_get_current_account()
            context.user_data['configured'] = True
            restart_expiry_timer(update.effective_user.id, update.effective_chat.id, context)
            await update.message.reply_text("✅ **Dropbox Connected!**")
            return ConversationHandler.END
        except:
            await update.message.reply_text("❌ Invalid Token. Try again.")
            return CREDENTIALS_1
    elif provider == 'telegram':
        if update.message.forward_from_chat:
            context.user_data['configured'] = True
            restart_expiry_timer(update.effective_user.id, update.effective_chat.id, context)
            await update.message.reply_text("✅ **Channel Linked!**")
            return ConversationHandler.END
        else:
            await update.message.reply_text("❌ Forward a message from the channel.")
            return CREDENTIALS_1
    elif provider == 'webdav':
        context.user_data['webdav_url'] = text
        await update.message.reply_text("Send **Username**:")
        return CREDENTIALS_2

async def handle_credentials_2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    provider = context.user_data.get('provider')
    text = update.message.text.strip()
    
    if provider == 'mega':
        try:
            Mega().login(context.user_data['mega_email'], text)
            context.user_data['configured'] = True
            restart_expiry_timer(update.effective_user.id, update.effective_chat.id, context)
            await update.message.reply_text("✅ **MEGA Connected!**")
            return ConversationHandler.END
        except:
            await update.message.reply_text("❌ Login failed.")
            return ConversationHandler.END
    elif provider == 'webdav':
        if 'webdav_user' not in context.user_data:
            context.user_data['webdav_user'] = text
            await update.message.reply_text("Send **Password**:")
            return CREDENTIALS_2
        else:
            context.user_data['webdav_pass'] = text
            context.user_data['configured'] = True
            restart_expiry_timer(update.effective_user.id, update.effective_chat.id, context)
            await update.message.reply_text("✅ **WebDAV Connected!**")
            return ConversationHandler.END

def main():
    persistence = PicklePersistence(filepath="bot_data.pickle")
    app = Application.builder().token(TOKEN).persistence(persistence).defaults(Defaults(parse_mode='HTML')).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start), CommandHandler('setup', start)],
        states={
            STORAGE: [CallbackQueryHandler(storage_choice)],
            CLOUD_PROVIDER: [CallbackQueryHandler(cloud_provider)],
            CREDENTIALS_1: [MessageHandler(filters.TEXT & ~filters.COMMAND | filters.FORWARDED, handle_credentials_1)],
            CREDENTIALS_2: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_credentials_2)],
        },
        fallbacks=[CommandHandler('start', start)],
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("search", search_anime))
    app.add_handler(CommandHandler("info", anime_info))
    app.add_handler(CommandHandler("simulate", simulate_task))
    app.add_handler(CallbackQueryHandler(magnet_button, pattern="^magnet_"))

    keep_alive()
    app.run_polling()

if __name__ == '__main__':
    main()
