#!/usr/bin/env python3
"""
Helper: find your Telegram chat ID.

Before running this:
  1. Put your bot token in /opt/stacks/jobagent/.env as TELEGRAM_TOKEN=...
  2. Open Telegram, find YOUR bot by its username, press Start and send it "hello".

Run (on the server, inside the agent container):
  docker exec jobagent python /data/tools/get_chat_id.py

It prints the chat ID of everyone who recently messaged the bot. Copy YOUR
number into .env as TELEGRAM_CHAT_ID=... and restart the agent.
This script only reads; it does not send anything.
"""
import os
import sys

import requests

TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
BASE = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")

if not TOKEN or "YOUR_" in TOKEN:
    sys.exit("TELEGRAM_TOKEN is not set in .env yet. Add it, then run: "
             "cd /opt/stacks/jobagent && docker compose up -d --force-recreate")

try:
    r = requests.get(f"{BASE}/bot{TOKEN}/getUpdates", timeout=30)
except requests.RequestException as exc:
    sys.exit(f"Could not reach Telegram ({type(exc).__name__}). Check the internet connection.")

if r.status_code == 401:
    sys.exit("Telegram says the token is wrong (401 Unauthorized). Copy it again from @BotFather.")
if r.status_code == 409:
    sys.exit("Telegram says another program is reading this bot's messages (409 Conflict). "
             "Stop that program (or remove its webhook) and try again.")
r.raise_for_status()

found = {}
for update in r.json().get("result", []):
    msg = update.get("message") or update.get("edited_message") or {}
    chat = msg.get("chat") or {}
    if "id" in chat:
        who = chat.get("username") or chat.get("first_name") or chat.get("title") or "?"
        found[chat["id"]] = (who, chat.get("type", "?"))

if not found:
    print("No messages found. Open your bot in Telegram, press Start, send 'hello', "
          "wait 5 seconds and run this again.")
else:
    print("Chats that recently messaged your bot:")
    for chat_id, (who, kind) in found.items():
        print(f"  chat ID: {chat_id:<16} name: {who:<20} type: {kind}")
    print("\nPut YOUR chat ID (type 'private') into .env as TELEGRAM_CHAT_ID=...")
