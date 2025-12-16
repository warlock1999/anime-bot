import logging
import os
import requests
import easywebdav
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler, 
    ConversationHandler, filters, ContextTypes, Defaults, PicklePersistence
)

# --- IMPORT KEEP_ALIVE ---
# Ensure keep_alive.py is in the same folder
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

# --- 1. NOTIFICATION & EXPIRY LOGIC (FREE TIER OPTIMIZATION) ---

async def expiry_job(context: ContextTypes.DEFAULT_TYPE):
    """Runs 8 hours after login to notify user and delete data."""
    job = context.job
    user_id = job.user_id
    
    try:
        await context.bot.send_message(
            chat_id=job.chat_id,
            text="⚠️ **Session Expired** ⚠️\n\nYour 8-hour login session has ended to save resources. Your credentials and settings have been wiped.\n\nPlease run /setup to login again."
        )
    except Exception as e:
        logger.warning(f"Could not send expiry message to {user_id}: {e}")

    if user_id in context.application.user_data:
        del context.application.user_data[user_id]
        logger.info(f"Wiped data for user {user_id}")

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

# --- 2. NEW CUSTOM FEATURES ---

async def search_anime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Scrapes Nyaa.si for magnet links."""
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("🔎 Usage: `/search One Piece`")
        return

    status_msg = await update.message.reply_text(f"🔍 Searching Nyaa for: <b>{query}</b>...", parse_mode='HTML')
    
    # Scrape Nyaa.si (sorted by seeders desc)
    url = f"https://nyaa.si/?f=0&c=0_0&q={query}&s=seeders&o=desc"
    try:
        response = requests.get(url, timeout=10)
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
        await status_msg.edit_text("⚠️ Error searching Nyaa (Site might be blocked or down).")

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
        response = requests.get(url, timeout=5).json()
        if response.get('data'):
            anime = response['data'][0]
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

async def set_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sets a custom renaming template."""
    template = " ".join(context.args)
    if not template:
        await update.message.reply_text("✏️ Usage: `/template {Series} - S{Season}E{Episode}`")
        return
        
    context.user_data['rename_template'] = template
    await update.message.reply_text(f"✅ Template Saved:\n<code>{template}.mkv</code>", parse_mode='HTML')

# --- 3. CORE SETUP HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if context.user_data.get('configured'):
        await update.message.reply_text("✅ You are logged in.\nTimer is running. Use /search or /info.")
        return ConversationHandler.END
        
    await update.message.reply_text(
        "👋 **Anime Assistant Bot**\n\n"
        "To save free resources, your session will last **8 hours**.\n"
        "Where do you store your anime?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Local Storage", callback_data='local')],
            [InlineKeyboardButton("Cloud (WebDAV)", callback_data='cloud')]
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
        await query.edit_message_text("✅ **Setup Complete!**\nUse /search, /info, or /template.")
        return ConversationHandler.END
    else:
        context.user_data['storage'] = 'cloud'
        keyboard = [["TeraBox"], ["MEGA"], ["Other (WebDAV)"]]
        reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton(col, callback_data=col) for col in row] for row in keyboard])
        await query.edit_message_text("Choose provider:", reply_markup=reply_markup)
        return CLOUD_PROVIDER

async def cloud_provider(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['provider'] = query.data
    if query.data == "Other (WebDAV)":
        await query.edit_message_text("Send WebDAV URL:")
        return WEBDAV_URL
    else:
        context.user_data['configured'] = True
        restart_expiry_timer(update.effective_user.id, update.effective_chat.id, context)
        await query.edit_message_text(f"✅ **Setup Complete!** ({query.data})")
        return ConversationHandler.END

async def webdav_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['webdav_url'] = update.message.text.strip()
    await update.message.reply_text("Send Username:")
    return WEBDAV_USER

async def webdav_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['webdav_user'] = update.message.text
    await update.message.reply_text("Send Password:")
    return WEBDAV_PASS

async def webdav_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['webdav_pass'] = update.message.text
    await update.message.reply_text("Optional: Base folder? Send 'none' for root.")
    return WEBDAV_BASE

async def webdav_base(update: Update, context: ContextTypes.DEFAULT_TYPE):
    base = update.message.text.strip()
    context.user_data['webdav_base'] = '/' if base.lower() == 'none' else base
    context.user_data['configured'] = True
    restart_expiry_timer(update.effective_user.id, update.effective_chat.id, context)
    await update.message.reply_text("✅ **WebDAV Connected!**\nSession active for 8 hours.")
    return ConversationHandler.END

async def simulate_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('configured'):
        await update.message.reply_text("Please /setup first.")
        return
    await update.message.reply_text("⏳ **Simulating Download...**")
    context.job_queue.run_once(download_notification_job, 5, chat_id=update.effective_chat.id, data={'filename': 'One Piece - 1080.mkv', 'folder': '/Anime/One Piece/'})

def main():
    persistence = PicklePersistence(filepath="bot_data.pickle")
    app = Application.builder().token(TOKEN).persistence(persistence).defaults(Defaults(parse_mode='HTML')).build()

    # Conversation Handler (Setup)
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
    
    # Custom Feature Handlers
    app.add_handler(CommandHandler("search", search_anime))
    app.add_handler(CommandHandler("info", anime_info))
    app.add_handler(CommandHandler("template", set_template))
    app.add_handler(CommandHandler("simulate", simulate_task))
    app.add_handler(CallbackQueryHandler(magnet_button, pattern="^magnet_"))

    # Start Fake Server for Render
    keep_alive()

    # Run Bot
    app.run_polling()

if __name__ == '__main__':
    main()
