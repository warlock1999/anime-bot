# 🤖 Anime Assistant Bot (Free Tier Optimized)

A Python-based Telegram bot designed to help manage and organize anime collections. 

**Key Feature:** This bot is optimized to run **24/7 for FREE** on services like Render.com. It uses a "Session" system where user data and preferences are automatically wiped after **8 hours** to minimize storage usage and server costs.

## ✨ Features

* **📂 Dual Storage Modes:** * **Local:** Manages files on the device running the bot.
    * **Cloud (WebDAV):** Connects to cloud storage (Nextcloud, 4shared, etc.) to manage files remotely.
* **⏱️ Auto-Cleanup:** Automatically deletes user credentials and settings from memory 8 hours after login to save resources.
* **🔔 Smart Notifications:** Alerts users when their session expires or when downloads/tasks are complete.
* **🟢 Always On:** Includes a `keep_alive.py` module to trick free hosting providers (like Render) into keeping the bot running 24/7.

## 🛠️ Prerequisites

* Python 3.9+
* A Telegram Bot Token (Get one from [@BotFather](https://t.me/BotFather))

## 📦 Project Structure

```text
├── main.py           # The main bot logic (Telegram handlers, 8h timer)
├── keep_alive.py     # A fake web server to keep the bot awake on free tiers
├── requirements.txt  # List of required Python libraries
└── README.md         # Documentation
