import os
import asyncio
import logging
from pyrogram import Client, filters
from pyrogram.errors import SessionPasswordNeeded, FloodWait, PhoneNumberInvalid, PhoneCodeInvalid
from fastapi import FastAPI, Request
import uvicorn

# --- Configuration ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Hardcoded details (Render Environment Variables se override bhi ho sakte hain)
API_ID = int(os.environ.get("API_ID", "123456")) # Apni API_ID environment variable me set karein
API_HASH = os.environ.get("API_HASH", "YOUR_API_HASH_HERE") # Apni API_HASH set karein
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8865692650:AAG9T_ekQYmgjkl2Y3HWOtfAI-0HiGSJMuE")
MASTER_USER_ID = int(os.environ.get("MASTER_USER_ID", "8865692650"))

# In-memory storage for sessions
session_store = {}

# --- Pyrogram Client Initialization ---
app = Client("bot_session", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, in_memory=True)
web_app = FastAPI()

# --- Bot Command Handlers ---
@app.on_message(filters.command("start") & filters.user(MASTER_USER_ID))
async def start_command(client, message):
    await message.reply("Bot online hai! Naya account add karne ke liye `/addaccount <phone>` use karein.")

@app.on_message(filters.command("addaccount") & filters.user(MASTER_USER_ID))
async def add_account_command(client, message):
    try:
        phone_number = message.command[1]
    except IndexError:
        await message.reply("Usage: `/addaccount <phone_number>`\nExample: `/addaccount +1234567890`")
        return

    async with Client("temp_session_for_code", api_id=API_ID, api_hash=API_HASH, in_memory=True) as temp_client:
        try:
            await temp_client.send_code(phone_number)
            await message.reply(f"✅ Code sent to {phone_number}.\n\nNow use: `/verifyaccount {phone_number} <otp_code>`")
        except PhoneNumberInvalid:
            await message.reply(f"❌ Phone number `{phone_number}` invalid hai.")
        except Exception as e:
            await message.reply(f"❌ Error: {str(e)}")

@app.on_message(filters.command("verifyaccount") & filters.user(MASTER_USER_ID))
async def verify_account_command(client, message):
    try:
        phone_number = message.command[1]
        otp_code = message.command[2]
    except IndexError:
        await message.reply("Usage: `/verifyaccount <phone_number> <otp_code>`\nExample: `/verifyaccount +1234567890 12345`")
        return

    async with Client("temp_session_for_verification", api_id=API_ID, api_hash=API_HASH, in_memory=True, phone_number=phone_number) as temp_client:
        try:
            await temp_client.sign_in(phone_number, code=otp_code)
            session_string = await temp_client.export_session_string()
            session_store[phone_number] = session_string
            await message.reply(f"✅ Account {phone_number} save ho gaya hai!")
        except PhoneCodeInvalid:
            await message.reply("❌ Code galat hai.")
        except SessionPasswordNeeded:
            await message.reply("❌ 2-Step Verification enabled hai. Ye support nahi karta.")
        except Exception as e:
            await message.reply(f"❌ Verification Error: {str(e)}")

@app.on_message(filters.command("listaccounts") & filters.user(MASTER_USER_ID))
async def list_accounts_command(client, message):
    session_phone_numbers = list(session_store.keys())
    if not session_phone_numbers:
        await message.reply("Koi account login nahi hai.")
        return
    
    account_list = "\n".join([f"- {phone}" for phone in session_phone_numbers])
    await message.reply(f"Logged in accounts:\n{account_list}")

# --- Render Web Health Check Server ---
@web_app.get("/")
async def root():
    return {"status": "Bot is running"}

# --- Bot Startup Routine ---
async def main():
    await app.start()
    config = uvicorn.Config(web_app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    asyncio.run(main())

