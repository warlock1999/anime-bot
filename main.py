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
# Added CLOUD_AUTH_3 specifically for WebDAV Password
SEEDR_LOGIN, SEEDR_PASS, STORAGE_SELECT, CLOUD_MENU, CLOUD_AUTH_1, CLOUD_AUTH_2, CLOUD_AUTH_3, FOLDER_SELECT = range(8)

# --- 1. API HELPERS ---

class SeedrAPI:
    def __init__(self, email, password):
        self.base_url = "https://www.seedr.cc/oauth_test/resource.php"
        self.email = email
        self.password = password
        self.token = None

    def login(self):
        try:
            r = requests.post("https://www.seedr.cc/rest/login", data={'username': self.email, 'password': self.password}).json()
            if 'access_token' in r:
                self.token = r['access_token']
                return True
        except: pass
        return False

    def get_direct_link(self, magnet):
        if not self.token and not self.login(): return None
        requests.get(f"{self.base_url}?method=add_torrent&access_token={self.token}&torrent_magnet={magnet}")
        import time
        for _ in range(5):
            time.sleep(2)
            r = requests.get(f"{self.base_url}?method=GetFolder&access_token={self.token}&folder_id=0").json()
            if 'files' in r and r['files']: return r['files'][0]['download_url']
            if 'folders' in r:
                for f in r['folders']:
                    sub = requests.get(f"{self.base_url}?method=GetFolder&access_token={self.token}&folder_id={f['id']}").json()
                    if 'files' in sub and sub['files']: return sub['files'][0]['download_url']
        return None

# --- 2. SETUP WIZARD ---

async def start_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛠 **Bot Setup Wizard**\n\n"
        "**Step 1: Torrent Engine**\n"
        "I need a **Seedr.cc** account (Free) to convert Magnets into Download Links.\n\n"
        "Please enter your **Seedr Email**:",
    )
    return SEEDR_LOGIN

async def seedr_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['seedr_email'] = update.message.text.strip()
    await update.message.reply_text("🔑 Enter your **Seedr Password**:")
    return SEEDR_PASS

async def seedr_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['seedr_pass'] = update.message.text.strip()
    msg = await update.message.reply_text("⏳ Verifying Seedr credentials...")
    s = SeedrAPI(context.user_data['seedr_email'], context.user_data['seedr_pass'])
    
    if s.login():
        await msg.edit_text("✅ **Seedr Connected!**\n\n**Step 2: Storage Location**")
        keyboard = [
            [InlineKeyboardButton("📱 My Device (Direct Link)", callback_data='local')],
            [InlineKeyboardButton("☁️ Cloud Drive (Auto-Upload)", callback_data='cloud')]
        ]
        await update.message.reply_text("Where should files be saved?", reply_markup=InlineKeyboardMarkup(keyboard))
        return STORAGE_SELECT
    else:
        await msg.edit_text("❌ **Login Failed.** Check email/password.")
        return SEEDR_LOGIN

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
        # Added WebDAV to the menu
        keyboard = [
            [InlineKeyboardButton("🔴 MEGA", callback_data='mega')],
            [InlineKeyboardButton("🔵 Dropbox", callback_data='dropbox')],
            [InlineKeyboardButton("🌐 WebDAV (Nextcloud/NAS)", callback_data='webdav')]
        ]
        await query.edit_message_text("☁️ **Select Cloud Provider:**", reply_markup=InlineKeyboardMarkup(keyboard))
        return CLOUD_MENU

async def cloud_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    provider = query.data
    context.user_data['provider'] = provider
    
    prompts = {
        'mega': "🔴 **MEGA Login**\n\nEnter your **Email**:",
        'dropbox': "🔵 **Dropbox Setup**\n\nEnter your **Access Token**:",
        'webdav': "🌐 **WebDAV Setup**\n\nEnter your **WebDAV URL** (e.g., https://cloud.example.com/remote.php/dav/files/user/):"
    }
    await query.edit_message_text(prompts.get(provider))
    return CLOUD_AUTH_1

# --- CLOUD AUTHENTICATION STEPS ---

async def cloud_auth_1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    provider = context.user_data.get('provider')
    text = update.message.text.strip()
    
    if provider == 'mega':
        context.user_data['mega_email'] = text
        await update.message.reply_text("🔑 Enter your **MEGA Password**:")
        return CLOUD_AUTH_2
        
    elif provider == 'dropbox':
        try:
            dbx = dropbox.Dropbox(text)
            account = dbx.users_get_current_account()
            context.user_data['dropbox_token'] = text
            await update.message.reply_text(f"✅ **Connected:** {account.name.display_name}\n\n📂 Enter **Folder Path** (e.g., `/Anime`):")
            return FOLDER_SELECT
        except Exception as e:
            await update.message.reply_text(f"❌ Invalid Token. Try again:\n{e}")
            return CLOUD_AUTH_1

    elif provider == 'webdav':
        context.user_data['webdav_url'] = text
        await update.message.reply_text("👤 Enter **WebDAV Username**:")
        return CLOUD_AUTH_2

async def cloud_auth_2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    provider = context.user_data.get('provider')
    text = update.message.text.strip()
    
    if provider == 'mega':
        # MEGA Logic
        try:
            m = Mega()
            m.login(context.user_data['mega_email'], text)
            context.user_data['mega_pass'] = text
            await update.message.reply_text(f"✅ **MEGA Connected!**\n\n📂 Enter **Folder Path** (e.g., `/Anime`):")
            return FOLDER_SELECT
        except Exception as e:
            await update.message.reply_text(f"❌ MEGA Login Failed: {e}")
            return CLOUD_AUTH_1

    elif provider == 'webdav':
        context.user_data['webdav_user'] = text
        await update.message.reply_text("🔑 Enter **WebDAV Password**:")
        return CLOUD_AUTH_3 # Go to Step 3 for WebDAV

async def cloud_auth_3(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # This step is specifically for WebDAV Password
    provider = context.user_data.get('provider')
    text = update.message.text.strip()
    
    if provider == 'webdav':
        context.user_data['webdav_pass'] = text
        
        # TEST WEBDAV CONNECTION
        msg = await update.message.reply_text("⏳ Testing WebDAV Connection...")
        try:
            # Clean URL for easywebdav (it expects protocol, host, path separated or specific format)
            # Simplest way: just try to list root dir
            # Note: easywebdav is a bit old, using requests is often more robust, but we will try standard init
            
            # Extract parts for easywebdav
            url = context.user_data['webdav_url']
            protocol = 'https' if url.startswith('https') else 'http'
            clean_url = url.replace('https://', '').replace('http://', '')
            host = clean_url.split('/')[0]
            path = clean_url.replace(host, '')
            
            webdav = easywebdav.connect(
                host=host,
                protocol=protocol,
                path=path,
                username=context.user_data['webdav_user'],
                password=context.user_data['webdav_pass']
            )
            webdav.ls() # Try to list directory
            
            await msg.edit_text("✅ **WebDAV Connected!**\n\n📂 Enter **Folder Path** (e.g., `/Anime`):")
            return FOLDER_SELECT
        except Exception as e:
            await msg.edit_text(f"❌ **Connection Failed.**\nError: {str(e)}\n\nCheck your URL and try entering URL again:")
            return CLOUD_AUTH_1

async def save_folder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    folder = update.message.text.strip()
    if not folder.startswith("/"): folder = "/" + folder
    if folder.endswith("/"): folder = folder[:-1]
    
    context.user_data['cloud_folder'] = folder
    context.user_data['configured'] = True
    await update.message.reply_text(f"✅ **Setup Complete!**\nSaving to: `{folder}` on {context.user_data['provider']}.")
    return ConversationHandler.END

# --- 3. DOWNLOAD / UPLOAD HANDLER ---

async def process_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("🚀 Processing...")
    
    data = context.user_data.get(query.data)
    if not data: return
    
    email = context.user_data.get('seedr_email')
    password = context.user_data.get('seedr_pass')
    if not email: return await query.message.reply_text("⚠️ Run /setup first.")

    msg = await query.message.reply_text("🔄 **Fetch Link...**")
    s = SeedrAPI(email, password)
    link = s.get_direct_link(data['magnet'])
    
    if not link: return await msg.edit_text("❌ Seedr Error.")
        
    mode = context.user_data.get('storage', 'local')
    
    if mode == 'local':
        await msg.edit_text(f"✅ **Download Ready!**\n\n🎬 `{data['name']}`\n🔗 <a href='{link}'>Click to Download</a>", parse_mode='HTML')
        
    elif mode == 'cloud':
        provider = context.user_data.get('provider')
        
        if provider == 'dropbox':
            await msg.edit_text("☁️ **Sending to Dropbox...**")
            try:
                dbx = dropbox.Dropbox(context.user_data['dropbox_token'])
                path = f"{context.user_data['cloud_folder']}/{data['name']}.mkv"
                dbx.files_save_url(path, link)
                await msg.edit_text(f"✅ **Saved to Dropbox!**")
            except Exception as e: await msg.edit_text(f"❌ Error: {e}")

        elif provider == 'webdav':
            await msg.edit_text("☁️ **Uploading to WebDAV...**\n(Note: This uses server bandwidth)")
            # WebDAV Upload Logic (Streaming)
            try:
                # 1. Download Stream
                r = requests.get(link, stream=True)
                # 2. Upload Stream
                webdav_url = context.user_data['webdav_url']
                target_url = f"{webdav_url}{context.user_data['cloud_folder']}/{data['name']}.mkv"
                
                # Using requests.put to stream data directly
                requests.put(
                    target_url, 
                    data=r.iter_content(chunk_size=4096), 
                    auth=(context.user_data['webdav_user'], context.user_data['webdav_pass'])
                )
                await msg.edit_text("✅ **Upload to WebDAV Complete!**")
            except Exception as e:
                await msg.edit_text(f"❌ Upload Failed: {e}\nHere is the link instead:\n{link}")

# --- 4. SEARCH (Placeholder logic for integration) ---
async def search_anime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # (Insert the Search Logic from previous turn here: search Nyaa -> Show buttons)
    # Ensure button callback is: InlineKeyboardButton("⬇️ Download", callback_data=key)
    # And key saves to context.user_data
    await update.message.reply_text("Search functionality active. (Reuse search code block)")

# --- MAIN ---

def main():
    persistence = PicklePersistence(filepath="bot_data.pickle")
    app = Application.builder().token(TOKEN).persistence(persistence).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start_setup), CommandHandler('setup', start_setup)],
        states={
            SEEDR_LOGIN: [MessageHandler(filters.TEXT, seedr_email)],
            SEEDR_PASS: [MessageHandler(filters.TEXT, seedr_pass)],
            STORAGE_SELECT: [CallbackQueryHandler(storage_choice)],
            CLOUD_MENU: [CallbackQueryHandler(cloud_menu)],
            CLOUD_AUTH_1: [MessageHandler(filters.TEXT, cloud_auth_1)],
            CLOUD_AUTH_2: [MessageHandler(filters.TEXT, cloud_auth_2)],
            CLOUD_AUTH_3: [MessageHandler(filters.TEXT, cloud_auth_3)], # Added Step 3
            FOLDER_SELECT: [MessageHandler(filters.TEXT, save_folder)],
        },
        fallbacks=[CommandHandler('start', start_setup)],
    )

    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(process_download, pattern="^dl_"))
    
    keep_alive()
    app.run_polling()

if __name__ == '__main__':
    main()
