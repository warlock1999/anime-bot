import logging
import os
import re
import httpx
import requests
import asyncio
from bs4 import BeautifulSoup
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler, 
    ConversationHandler, filters, ContextTypes, Defaults, PicklePersistence
)

from keep_alive import keep_alive

# Enable logging
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_TOKEN")

# Conversation states
STORAGE, CLOUD_PROVIDER, CREDENTIALS, FOLDER_SELECT = range(4)

# --- 1. SEEDR API (The Engine) ---

class SeedrAPI:
    def __init__(self, email=None, password=None):
        self.base_url = "https://www.seedr.cc/oauth_test/resource.php"
        self.email = email
        self.password = password
        self.token = None

    def login(self):
        """Get Access Token."""
        url = "https://www.seedr.cc/rest/login"
        data = {'username': self.email, 'password': self.password}
        try:
            r = requests.post(url, data=data).json()
            if 'access_token' in r:
                self.token = r['access_token']
                return True
        except:
            pass
        return False

    def get_direct_link(self, magnet):
        """Adds magnet and waits for the direct download link."""
        if not self.token and not self.login(): return None
        
        # 1. Add Magnet
        requests.get(f"{self.base_url}?method=add_torrent&access_token={self.token}&torrent_magnet={magnet}")
        
        # 2. Poll for file (Wait up to 10 seconds)
        import time
        for _ in range(5):
            time.sleep(2)
            # Check Root Folder
            list_url = f"{self.base_url}?method=GetFolder&access_token={self.token}&folder_id=0"
            r = requests.get(list_url).json()
            
            # Check inside folders (Torrents usually create a folder)
            if 'folders' in r:
                for folder in r['folders']:
                    # Look inside this folder
                    sub_url = f"{self.base_url}?method=GetFolder&access_token={self.token}&folder_id={folder['id']}"
                    sub_r = requests.get(sub_url).json()
                    if 'files' in sub_r and sub_r['files']:
                        # Found the video file!
                        return sub_r['files'][0]['download_url']
            
            # Check if it's a loose file
            if 'files' in r and r['files']:
                return r['files'][0]['download_url']
                
        return None

# --- 2. SEARCH & PARSING ---

def clean_name(text):
    """Makes the filename look nice."""
    # Removes [SubsPlease], (1080p), [Hash]
    clean = re.sub(r'\[.*?\]', '', text)
    clean = re.sub(r'\(.*?\)', '', clean)
    return clean.strip().replace('.mkv', '').replace('.mp4', '')

async def search_anime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("🔎 Usage: `/search One Piece`")
        return

    status_msg = await update.message.reply_text(f"🔍 Searching: <b>{query}</b>...", parse_mode='HTML')
    
    mirrors = ["https://nyaa.si", "https://nyaa.iss.one", "https://nyaa.land"]
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://google.com"}

    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        response = None
        for domain in mirrors:
            try:
                response = await client.get(f"{domain}/?f=0&c=0_0&q={query}&s=seeders&o=desc", headers=headers)
                if response.status_code == 200: break
            except: continue

    if not response or response.status_code != 200:
        await status_msg.edit_text("⚠️ **Search Failed.** Mirrors blocked.")
        return

    try:
        soup = BeautifulSoup(response.text, 'html.parser')
        rows = soup.select('tr.default, tr.success')[:5]

        if not rows:
            await status_msg.edit_text("❌ No results found.")
            return

        message = f"<b>Results for '{query}':</b>\n\n"
        keyboard = []

        for i, row in enumerate(rows):
            cols = row.find_all('td')
            raw_title = cols[1].find('a', class_=lambda x: x != 'comments').text.strip()
            size = cols[3].text.strip()
            magnet = cols[2].find_all('a')[1]['href']
            
            # Detect Quality
            quality = "720p"
            if "1080" in raw_title: quality = "1080p"
            elif "4k" in raw_title.lower(): quality = "4K"
            
            display_name = clean_name(raw_title)[:30] # Shorten for display
            
            message += f"{i+1}. <b>{display_name}</b>\n   └ 📼 {quality} | 📦 {size}\n"
            
            # Store data
            key = f"dl_{update.effective_user.id}_{i}"
            context.user_data[key] = {'magnet': magnet, 'name': display_name}
            
            # Button
            keyboard.append([InlineKeyboardButton(f"⬇️ Download {quality}", callback_data=key)])

        reply_markup = InlineKeyboardMarkup(keyboard)
        await status_msg.edit_text(message, reply_markup=reply_markup, parse_mode='HTML')

    except Exception as e:
        logger.error(f"Search error: {e}")
        await status_msg.edit_text("⚠️ Parsing Error.")

# --- 3. DOWNLOAD HANDLER (THE MAGIC) ---

async def download_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("🚀 Fetching Link...")
    
    data = context.user_data.get(query.data)
    if not data:
        await query.message.reply_text("❌ Session expired. Search again.")
        return
    
    # Check Credentials
    email = context.user_data.get('seedr_email')
    password = context.user_data.get('seedr_pass')
    
    if not email:
        await query.message.reply_text("⚠️ **Setup Required**\nRun /setup to connect your account.")
        return

    await query.message.reply_text(f"⏳ **Processing:** `{data['name']}`\nPlease wait while I fetch the direct link...", parse_mode='Markdown')
    
    # Get Link
    seedr = SeedrAPI(email, password)
    link = seedr.get_direct_link(data['magnet'])
    
    if link:
        # We send the link with HTML formatting
        # This allows the user's device to handle the "Save As" logic
        await query.message.reply_text(
            f"✅ **Download Ready!**\n\n"
            f"🎬 <b>{data['name']}</b>\n"
            f"🔗 <a href='{link}'>Click Here to Download</a>\n\n"
            f"<i>Tip: On PC, right-click and choose 'Save Link As' to pick a folder. On Android, it saves to Downloads.</i>",
            parse_mode='HTML'
        )
    else:
        await query.message.reply_text("❌ **Error:** Could not generate link. Storage might be full.")

# --- 4. SETUP WIZARD (WITH FOLDER SELECT) ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 **Anime Bot Setup**\n\n"
        "To download files, I need to connect to a **Seedr** account (Free).\n"
        "This acts as the engine to convert Torrents -> Direct Links.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚡ Connect Seedr", callback_data='seedr')]])
    )
    return CREDENTIALS

async def ask_creds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("📧 Enter your **Seedr Email**:")
    return CREDENTIALS

async def save_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['seedr_email'] = update.message.text
    await update.message.reply_text("🔑 Enter your **Password**:")
    return FOLDER_SELECT

async def folder_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Save password
    context.user_data['seedr_pass'] = update.message.text
    
    # Ask for Cloud Folder Preference
    await update.message.reply_text(
        "📂 **Cloud Storage Preference**\n\n"
        "If you use this bot to upload to cloud (Google Drive/MEGA), where should files go?\n\n"
        "Type a folder path (e.g., `/Anime/One Piece/`) OR type `root` to save in main folder.",
    )
    return STORAGE

async def finish_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    folder = update.message.text
    if folder.lower() == 'root':
        context.user_data['cloud_folder'] = '/'
    else:
        context.user_data['cloud_folder'] = folder
        
    context.user_data['configured'] = True
    await update.message.reply_text("✅ **Setup Complete!**\n\nTry `/search One Piece` to start downloading.")
    return ConversationHandler.END

# --- MAIN ---

async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("start", "Setup Bot"),
        BotCommand("search", "Find Anime"),
        BotCommand("disconnect", "Logout"),
    ])

def main():
    persistence = PicklePersistence(filepath="bot_data.pickle")
    app = Application.builder().token(TOKEN).persistence(persistence).post_init(post_init).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start), CommandHandler('setup', start)],
        states={
            CREDENTIALS: [
                CallbackQueryHandler(ask_creds, pattern='seedr'),
                MessageHandler(filters.TEXT, save_email)
            ],
            FOLDER_SELECT: [MessageHandler(filters.TEXT, folder_select)],
            STORAGE: [MessageHandler(filters.TEXT, finish_setup)],
        },
        fallbacks=[CommandHandler('start', start)],
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("search", search_anime))
    app.add_handler(CallbackQueryHandler(download_button, pattern="^dl_"))
    
    keep_alive()
    app.run_polling()

if __name__ == '__main__':
    main()
