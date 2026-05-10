import asyncio
import os
from telegram import Bot

TOKEN = os.getenv("TOKEN")

async def reset():
    bot = Bot(token=TOKEN)
    await bot.delete_webhook(drop_pending_updates=True)
    print("✅ Webhook supprimé, polling propre.")

asyncio.run(reset())
