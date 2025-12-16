# ... (all your imports) ...
from keep_alive import keep_alive  # <--- IMPORT THIS

# ... (your existing code) ...

def main():
    persistence_path = "bot_data.pickle" # Render doesn't support /data volume on free tier easily, so we use local file (it will wipe on restart, but that fits your 8-hour limit logic!)
    persistence = PicklePersistence(filepath=persistence_path)

    app = Application.builder().token(TOKEN).persistence(persistence).defaults(Defaults(parse_mode='HTML')).build()

    # ... (your handlers) ...

    # START THE FAKE WEB SERVER
    keep_alive()  # <--- ADD THIS LINE BEFORE RUNNING POLLING

    app.run_polling()

if __name__ == '__main__':
    main()
