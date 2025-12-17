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
from mega import Mega
import dropbox
import easywebdav

from keep_alive import keep_alive

# Enable logging
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_TOKEN")

# Conversation states
SEEDR_LOGIN, SEEDR_PASS, MANUAL_TOKEN, STORAGE_SELECT, CLOUD_MENU, CLOUD_AUTH_1, CLOUD_AUTH_2, CLOUD_AUTH_3, FOLDER_SELECT = range(9)

# --- 1. API HELPERS ---

class SeedrAPI:
    def __init__(self, email=None, password=None, token=None):
        self.base_url = "https://www.seedr.cc/oauth_test/resource.php"
        self.email = email
        self.password = password
        self.token = token
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }

    def login(self):
        """Attempts login with headers to bypass blocks."""
        if self.token: return True # Already has token
        
        try:
            url = "https://www.seedr.cc/rest/login"
            data = {'username': self.email, 'password': self.password}
            # verify=False prevents SSL errors on some free servers
            r = requests.post(url, data=data, headers=self.headers, timeout=10).json()
            
            if 'access_token' in r:
                self.token = r['access_token']
                return True
            else:
                logger.error(f"Seedr Login Error: {r}")
        except Exception as e:
            logger.error(f"Seedr Connection Error: {e}")
        return False

    def get_direct_link(self, magnet):
        if not self.token: return None
        
        # Add Magnet
        requests.get(f"{self.base_url}?method=add_torrent&access_token={self.token}&torrent_magnet={magnet}", headers=self.headers)
        
        # Wait for conversion (Poll)
        import time
        for _ in range(8): # increased wait time
            time.sleep(2)
            try:
                r = requests.get(f"{self.base_url}?method=GetFolder&access_token={self.token}&folder_id=0", headers=self.headers).json()
                
                # Check Root Files
                if 'files' in r and r['files']: return r['files'][0]['download_url']
                
                # Check Folders (Torrents usually make a folder)
                if 'folders' in r:
                    for f in r['folders']:
                        sub = requests.get(f"{self.base_url}?method=GetFolder&access_token={self.token}&folder_id={f['id']}", headers=self.headers).json()
                        if 'files' in sub and sub['files']: return sub['files'][0]['download_url']
            except:
                pass
        return None

# --- 2. SETUP WIZARD (Updated) ---

async def start_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛠 **Bot Setup Wizard**\n\n"
        "**Step 1: Torrent Engine**\n"
        "I need a **Seedr.cc** account.\n\n"
        "Please enter your **Seedr Email**:",
    )
    return SEEDR_LOGIN

async def seedr_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['seedr_email'] = update.message.text.strip()
    await update.message.reply_text("🔑 Enter your **Seedr Password**:")
    return SEEDR_PASS

async def seedr_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['seedr_pass'] = update.message.text.strip()
    msg = await update.message.reply_text("⏳ Verifying with Anti-Block Headers...")
    
    s = SeedrAPI(context.user_data['seedr_email'], context.user_data['seedr_pass'])
    
    if s.login():
        context.user_data['seedr_token'] = s.token
        await msg.edit_text("✅ **Seedr Connected!**\n\n**Step 2: Storage Location**")
        return await ask_storage(update, context)
    else:
        # FAILOVER: Ask for Manual Token
        await msg.edit_text(
            "❌ **Auto-Login Failed.**\n"
            "Seedr might be blocking the bot's IP or you used Google Login.\n\n"
            "**Alternative:** Please enter your **Seedr Device Token** manually.\n"
            "*(Reply /cancel to quit)*"
        )
        return MANUAL_TOKEN

async def manual_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token = update.message.text.strip()
    if len(token) < 10:
        await update.message.reply_text("❌ Invalid Token. Please try logging in normally: /setup")
        return ConversationHandler.END
        
    context.user_data['seedr_token'] = token
    await update.message.reply_text("✅ **Token Saved!**\n\n**Step 2: Storage Location**")
    return await ask_storage(update, context)

async def ask_storage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📱 My Device (Direct Link)", callback_data='local')],
        [InlineKeyboardButton("☁️ Cloud Drive (Auto-Upload)", callback_data='cloud')]
    ]
    await update.message.reply_text("Where should files be saved?", reply_markup=InlineKeyboardMarkup(keyboard))
    return STORAGE_SELECT

async def storage_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == 'local':
        context.user_data['storage'] = 'local'
        context.user_data['configured'] = True
        await query.edit_message_text("✅ **Setup Complete!**\nDirect Links enabled.")
        return ConversationHandler.END
    else:
        context.user_data['storage'] = 'cloud'
        keyboard = [
            [InlineKeyboardButton("🔴 MEGA", callback_data='mega')],
            [InlineKeyboardButton("🔵 Dropbox", callback_data='dropbox')],
            [InlineKeyboardButton("🌐 WebDAV (Nextcloud/NAS)", callback_data='webdav')]
        ]
        await query.edit_message_text("☁️ **Select Cloud Provider:**", reply_markup=InlineKeyboardMarkup(keyboard))
        return CLOUD_MENU

# --- CLOUD AUTH & MENUS (Standard) ---

async def cloud_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    provider = query.data
    context.user_data['provider'] = provider
    prompts = {
        'mega': "🔴 **MEGA Login**\n\nEnter your **Email**:",
        'dropbox': "🔵 **Dropbox Setup**\n\nEnter your **Access Token**:",
        'webdav': "⚠️ **Bandwidth Warning** ⚠️\n\nWebDAV uses server data. Enter **WebDAV URL**:"
    }
    await query.edit_message_text(prompts.get(provider))
    return CLOUD_AUTH_1

async def cloud_auth_1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    provider = context.user_data.get('provider')
    text = update.message.text.strip()
    if provider == 'mega':
        context.user_data['mega_email'] = text
        await update.message.reply_text("🔑 Enter **MEGA Password**:")
        return CLOUD_AUTH_2
    elif provider == 'dropbox':
        try:
            dbx = dropbox.Dropbox(text)
            account = dbx.users_get_current_account()
            context.user_data['dropbox_token'] = text
            await update.message.reply_text(f"✅ **Connected:** {account.name.display_name}\n📂 Enter **Folder Path** (e.g. /Anime):")
            return FOLDER_SELECT
        except: return await retry_auth(update)
    elif provider == 'webdav':
        context.user_data['webdav_url'] = text
        await update.message.reply_text("👤 Enter **Username**:")
        return CLOUD_AUTH_2

async def cloud_auth_2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    provider = context.user_data.get('provider')
    text = update.message.text.strip()
    if provider == 'mega':
        try:
            m = Mega()
            m.login(context.user_data['mega_email'], text)
            context.user_data['mega_pass'] = text
            await update.message.reply_text("✅ **MEGA Connected!**\n📂 Enter **Folder Path**:")
            return FOLDER_SELECT
        except: return await retry_auth(update)
    elif provider == 'webdav':
        context.user_data['webdav_user'] = text
        await update.message.reply_text("🔑 Enter **Password**:")
        return CLOUD_AUTH_3

async def cloud_auth_3(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['webdav_pass'] = update.message.text.strip()
    # Simple check
    await update.message.reply_text("✅ **WebDAV Credentials Saved.**\n📂 Enter **Folder Path** (e.g. /Anime):")
    return FOLDER_SELECT

async def retry_auth(update):
    await update.message.reply_text("❌ **Failed.** Try again or /start to restart.")
    return ConversationHandler.END

async def save_folder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    folder = update.message.text.strip()
    if not folder.startswith("/"): folder = "/" + folder
    context.user_data['cloud_folder'] = folder
    context.user_data['configured'] = True
    await update.message.reply_text(f"✅ **Setup Complete!**\nSaving to: `{folder}`")
    return ConversationHandler.END

# --- 3. DOWNLOAD LOGIC ---

async def process_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("🚀 Fetching...")
    
    data = context.user_data.get(query.data)
    if not data: return
    
    # Use stored Token instead of re-logging in
    token = context.user_data.get('seedr_token')
    if not token: return await query.message.reply_text("⚠️ Run /setup first.")

    msg = await query.message.reply_text("🔄 **Converting Magnet...**")
    
    # Init with Token
    s = SeedrAPI(token=token) 
    link = s.get_direct_link(data['magnet'])
    
    if not link: return await msg.edit_text("❌ Seedr Error (Storage Full?)")
        
    mode = context.user_data.get('storage', 'local')
    
    if mode == 'local':
        await msg.edit_text(f"✅ **Ready!**\n🎬 `{data['name']}`\n🔗 <a href='{link}'>Click to Download</a>", parse_mode='HTML')
    
    elif mode == 'cloud':
        provider = context.user_data.get('provider')
        if provider == 'webdav':
            await msg.edit_text("☁️ **Streaming to WebDAV...**")
            try:
                # Stream Logic
                r = requests.get(link, stream=True)
                url = f"{context.user_data['webdav_url']}{context.user_data['cloud_folder']}/{data['name']}.mkv"
                requests.put(url, data=r.iter_content(4096), auth=(context.user_data['webdav_user'], context.user_data['webdav_pass']))
                await msg.edit_text("✅ **Upload Complete!**")
            except Exception as e: await msg.edit_text(f"❌ Upload Error: {e}")
        elif provider == 'dropbox':
            # Dropbox Save URL logic (Simplified for brevity)
            await msg.edit_text("☁️ **Sending to Dropbox...**")
            try:
                dbx = dropbox.Dropbox(context.user_data['dropbox_token'])
                dbx.files_save_url(f"{context.user_data['cloud_folder']}/{data['name']}.mkv", link)
                await msg.edit_text("✅ **Saved to Dropbox!**")
            except Exception as e: await msg.edit_text(f"❌ Error: {e}")

# --- 4. SEARCH (Same) ---
def clean_name(text):
    return re.sub(r'\[.*?\]|\(.*?\)', '', text).strip()

async def search_anime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args)
    if not query: return await update.message.reply_text("Usage: /search Name")
    msg = await update.message.reply_text(f"🔍 Searching: {query}...")
    
    # Search Nyaa
    mirrors = ["https://nyaa.si", "https://nyaa.iss.one"]
    headers = {"User-Agent": "Mozilla/5.0"}
    
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        resp = None
        for m in mirrors:
            try:
                resp = await client.get(f"{m}/?f=0&c=0_0&q={query}&s=seeders&o=desc", headers=headers)
                if resp.status_code == 200: break
            except: continue
            
    if not resp or resp.status_code != 200: return await msg.edit_text("❌ Search Failed.")
    
    # Parse
    soup = BeautifulSoup(resp.text, 'html.parser')
    rows = soup.select('tr.default, tr.success')[:5]
    if not rows: return await msg.edit_text("❌ No results.")
    
    txt = f"<b>Results for '{query}':</b>\n\n"
    kb = []
    for i, row in enumerate(rows):
        cols = row.find_all('td')
        title = clean_name(cols[1].find('a', class_=lambda x: x!='comments').text)[:30]
        size = cols[3].text.strip()
        magnet = cols[2].find_all('a')[1]['href']
        
        key = f"dl_{update.effective_user.id}_{i}"
        context.user_data[key] = {'magnet': magnet, 'name': title}
        txt += f"{i+1}. <b>{title}</b> [{size}]\n"
        kb.append([InlineKeyboardButton(f"⬇️ Download {i+1}", callback_data=key)])
        
    await msg.edit_text(txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')

# --- MAIN ---

async def post_init(application: Application):
    await application.bot.set_my_commands([BotCommand("start", "Setup"), BotCommand("search", "Find")])

def main():
    persistence = PicklePersistence(filepath="bot_data.pickle")
    app = Application.builder().token(TOKEN).persistence(persistence).post_init(post_init).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start_setup), CommandHandler('setup', start_setup)],
        states={
            SEEDR_LOGIN: [MessageHandler(filters.TEXT, seedr_email)],
            SEEDR_PASS: [MessageHandler(filters.TEXT, seedr_pass)],
            MANUAL_TOKEN: [MessageHandler(filters.TEXT, manual_token)], # New State
            STORAGE_SELECT: [CallbackQueryHandler(storage_choice)],
            CLOUD_MENU: [CallbackQueryHandler(cloud_menu)],
            CLOUD_AUTH_1: [MessageHandler(filters.TEXT, cloud_auth_1)],
            CLOUD_AUTH_2: [MessageHandler(filters.TEXT, cloud_auth_2)],
            CLOUD_AUTH_3: [MessageHandler(filters.TEXT, cloud_auth_3)],
            FOLDER_SELECT: [MessageHandler(filters.TEXT, save_folder)],
        },
        fallbacks=[CommandHandler('start', start_setup)],
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("search", search_anime))
    app.add_handler(CallbackQueryHandler(process_download, pattern="^dl_"))
    
    keep_alive()
    app.run_polling()

if __name__ == '__main__':
    main()
