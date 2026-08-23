import os
import asyncio
import logging
from pyrogram import Client, filters
from fastapi import FastAPI
import uvicorn

# --- Python 3.14 Event Loop Fix ---
try:
    asyncio.get_running_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

API_ID = int(os.environ.get("API_ID", "123456"))
API_HASH = os.environ.get("API_HASH", "YOUR_API_HASH_HERE")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8865692650:AAG9T_ekQYmgjkl2Y3HWOtfAI-0HiGSJMuE")
MASTER_USER_ID = int(os.environ.get("MASTER_USER_ID", "8865692650"))

session_store = {}

app = Client("bot_session", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, in_memory=True)
web_app = FastAPI()

@app.on_message(filters.command("start") & filters.user(MASTER_USER_ID))
async def start_command(client, message):
    await message.reply("Bot online hai! Use /addaccount <phone>")

@web_app.get("/")
async def root():
    return {"status": "Bot is running"}

async def main():
    async with app:
        logger.info("Bot Started!")
        config = uvicorn.Config(web_app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
        server = uvicorn.Server(config)
        await server.serve()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
