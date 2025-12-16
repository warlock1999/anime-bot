import logging
import os
import easywebdav
import dropbox
import httpx  # <--- NEW: Async requests library
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
    # Check if JobQueue is available
    if not context.job_queue:
        logger.error("JobQueue not available. Install APScheduler.")
        return

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

# --- 2. SEARCH & EXTRAS (FIXED) ---

async def search_anime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Scrapes Nyaa.si for magnet links using Async HTTP."""
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("🔎 Usage: `/search One Piece`")
        return

    status_msg = await update.message.reply_text(f"🔍 Searching Nyaa for: <b>{query}</b>...", parse_mode='HTML')
    
    # URL and Headers to look like a real browser (Bypasses 403 errors)
    url = f"https://nyaa.si/?f=0&c=0_0&q={query}&s=seeders&o=desc"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }

    try:
        # Use httpx for non-blocking async request
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url, headers=headers, follow_redirects=True)
        
        if response.status_code != 200:
            await status_msg.edit_text(f"⚠️ Search failed (Status: {response.status_code}). Site might be blocked.")
            return

        soup = BeautifulSoup(response.text, 'html.parser')
        rows = soup.select('tr.default, tr.success')[:5] # Top 5 results

        if not rows:
            await status_msg.edit_text("❌ No results found.")
            return

        message = f"<b>Top 5 Results for '{query}':</b>\n\n"
        keyboard = []

        for i, row in enumerate(rows):
            cols = row.find_all('td')
            # Extract Title
            title_tag = cols[1].find('a', class_=lambda x: x != 'comments')
            title = title_tag.text.strip() if title_tag else "Unknown"
            
            # Extract Size
            size = cols[3].text.strip()
            
            # Extract Magnet
            magnet_links = cols[2].find_all('a')
            magnet = magnet_links[1]['href'] if len(magnet_links) > 1 else None
            
            if magnet:
                short_title = (title[:25] + '..') if len(title) > 25 else title
                message += f"{i+1}. <b>{short_title}</b> [{size}]\n"
                
                # Save magnet to user_data for button retrieval
                magnet_key = f"magnet_{update.effective_user.id}_{i}"
                context.user_data[magnet_key] = magnet
                keyboard.append([InlineKeyboardButton(f"🧲 Get Magnet {i+1}", callback_data=magnet_key)])

        reply_markup = InlineKeyboardMarkup(keyboard)
        await status_msg.edit_text(message, reply_markup=reply_markup, parse_mode='HTML')

    except Exception as e:
        logger.error(f"Search error: {e}")
        await status_msg.edit_text(f"⚠️ Error: {str(e)}")

async def magnet_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles clicks on 'Get Magnet' buttons."""
    query = update.callback_query
    magnet_key = query.data
    magnet_link = context.user_data.get(magnet_key)
    
    await query.answer()
    if magnet_link:
        await query.message.reply_text(f"🧲 <b>Magnet Link:</b>\n\n<code>{magnet_link}</code>", parse_mode='HTML')
    else:
        await query.message.reply_text("⚠️ Link expired or not found.")

async def anime_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fetches anime details from Jikan API."""
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
            caption = (
                f"🎬 <b>{anime['title']}</b>\n"
                f"⭐ Score: {anime.get('score', 'N/A')}\n"
                f"📺 Episodes: {anime.get('episodes', '?')}\n\n"
                f"<i>{anime.get('synopsis', '')[:250]}...</i>\n\n"
                f"<a href='{anime['url']}'>View on MyAnimeList</a>"
            )
            image_url = anime['images']['jpg']['image_url']
            await update.message.reply_photo(photo=image_url, caption=caption, parse_mode='HTML')
        else:
            await update.message.reply_text("❌ Anime not found.")
    except Exception as e:
        await update.message.reply_text(f"⚠️ API Error: {e}")

async def simulate_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('configured'):
        await update.message.reply_text("Please /setup first.")
        return
    
    if not context.job_queue:
        await update.message.reply_text("⚠️ JobQueue failed. Check requirements.txt for APScheduler.")
        return

    await update.message.reply_text("⏳ **Simulating Download...** (Wait 5s)")
    context.job_queue.run_once(
        download_notification_job, 
        5, 
        chat_id=update.effective_chat.id, 
        data={'filename': 'One Piece - 1080.mkv', 'folder': '/Anime/One Piece/'}
    )

# --- 3. CLOUD LOGIC HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('configured'):
        await update.message.reply_text("✅ You are logged in.\nUse /search or /setup to change.")
        return ConversationHandler.END
        
    await update.message.reply_text(
        "👋 **Anime Assistant Bot**\n"
        "I support multiple storage providers.\n"
        "Where should I save your anime?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📂 Local Storage", callback_data='local')],
            [InlineKeyboardButton("☁️ Cloud / Telegram", callback_data='cloud')]
        ])
    )
    return STORAGE

async def storage_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == 'local':
        context.user_data['storage'] = 'local'
        context.user_data['configured'] = True
        restart_expiry_timer(update.effective_user.id, update.effective_chat.id, context)
        await query.edit_message_text("✅ **Setup Complete!**\nMode: Local Storage")
        return ConversationHandler.END
    else:
        keyboard = [
            [InlineKeyboardButton("🔴 MEGA", callback_data='mega')],
            [InlineKeyboardButton("🔵 Dropbox", callback_data='dropbox')],
            [InlineKeyboardButton("📢 Telegram Channel", callback_data='telegram')],
            [InlineKeyboardButton("🌐 WebDAV (Nextcloud)", callback_data='webdav')],
            [InlineKeyboardButton("🔙 Back", callback_data='back')]
        ]
        await query.edit_message_text("Select your Cloud Provider:", reply_markup=InlineKeyboardMarkup(keyboard))
        return CLOUD_PROVIDER

async def cloud_provider(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    provider = query.data
    context.user_data['provider'] = provider

    if provider == 'back':
        return await start(update, context)

    prompts = {
        'mega': "🔴 **Selected MEGA**\n\nPlease send your **Email Address**:",
        'dropbox': "🔵 **Selected Dropbox**\n\nPlease send your **Access Token**:",
        'telegram': "📢 **Selected Telegram Channel**\n\n1. Add me as Admin to your Channel.\n2. Forward a message from it to me.",
        'webdav': "🌐 **Selected WebDAV**\n\nSend your WebDAV URL:"
    }
    
    await query.edit_message_text(prompts.get(provider, "Error"))
    return CREDENTIALS_1

async def handle_credentials_1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    provider = context.user_data.get('provider')
    text = update.message.text.strip() if update.message.text else ""
    
    if provider == 'mega':
        context.user_data['mega_email'] = text
        await update.message.reply_text("🔒 Now send your **MEGA Password**:")
        return CREDENTIALS_2
        
    elif provider == 'dropbox':
        try:
            dbx = dropbox.Dropbox(text)
            dbx.users_get_current_account() 
            context.user_data['dropbox_token'] = text
            context.user_data['configured'] = True
            restart_expiry_timer(update.effective_user.id, update.effective_chat.id, context)
            await update.message.reply_text("✅ **Dropbox Connected!**")
            return ConversationHandler.END
        except Exception as e:
            await update.message.reply_text(f"❌ Connection failed: {e}\nTry again.")
            return CREDENTIALS_1

    elif provider == 'telegram':
        if update.message.forward_from_chat:
            context.user_data['channel_id'] = update.message.forward_from_chat.id
            context.user_data['configured'] = True
            restart_expiry_timer(update.effective_user.id, update.effective_chat.id, context)
            await update.message.reply_text(f"✅ **Linked to Channel!**")
            return ConversationHandler.END
        else:
            await update.message.reply_text("❌ Please forward a message from the channel.")
            return CREDENTIALS_1

    elif provider == 'webdav':
        context.user_data['webdav_url'] = text
        await update.message.reply_text("Send **Username**:")
        return CREDENTIALS_2

async def handle_credentials_2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    provider = context.user_data.get('provider')
    text = update.message.text.strip()

    if provider == 'mega':
        email = context.user_data['mega_email']
        try:
            m = Mega()
            m.login(email, text)
            context.user_data['configured'] = True
            restart_expiry_timer(update.effective_user.id, update.effective_chat.id, context)
            await update.message.reply_text(f"✅ **MEGA Connected!**\nSpace: {m.get_storage_space(giga=True)} GB")
            return ConversationHandler.END
        except Exception as e:
            await update.message.reply_text(f"❌ Login failed: {e}")
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
